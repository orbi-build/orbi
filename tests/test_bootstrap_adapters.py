"""Command execution and GitHub adapter domain (Issue #1649).

Domain split out of `tests/test_bootstrap_runner.py` (after
`test_runner_config.py` Issue #789 and the release, Pi session,
configuration/preflight and delivery/claim suites), moved verbatim with no
behavior change:

- Command execution: the single subprocess seam `run_command` (Article 3.4)
  and `single_line`, the formatting that keeps its journal line one line.
- GitHub adapter: the `orbi.github` functions the runner calls - the
  issue-array parsers, `list_issues`, `edit_issue`, `comment_issue`,
  `open_pr_for_branch`, `pr_comments`, `issue_labels`, the trusted-comment
  block and the Epic verification helpers.

The tests own no shared helper: each fakes the one `run_command` seam or
asserts a parser directly, so there is no `tests/fakes/bootstrap_adapters.py`.
Cross-domain runner ticks (`process_issue`, `main`, claim/milestone
bookkeeping, reconciliation, failure reporting) stay in
`test_bootstrap_runner.py`.
"""
import json
import subprocess
from unittest.mock import Mock

import pytest

import orbi.github as github
import orbi.journal as journal
import orbi.runner as runner
from orbi import progress
from seam import seam


# --- command execution: the single run_command subprocess seam --------------

def test_run_command_returns_stdout(monkeypatch, tmp_path):
    completed = Mock(stdout=" output \n", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    assert runner.run_command(["git", "status"], cwd=tmp_path) == "output"
    runner.subprocess.run.assert_called_once_with(
        ["git", "status"], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=None,
    )


def test_run_command_logs_stderr(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="ok", stderr="warning\n")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["gh", "--version"], cwd=tmp_path) == "ok"
    assert "stderr=warning" in caplog.text


def test_run_command_can_log_success_stdout(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="agent output\n", stderr="")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["pi"], cwd=tmp_path, log_stdout=True) == "agent output"
    assert "stdout=agent output" in caplog.text


def test_run_command_logs_called_process_error_and_reraises(tmp_path, caplog):
    error = subprocess.CalledProcessError(
        2, ["gh", "issue", "list"], output="out", stderr="bad",
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
            runner.run_command(["gh", "issue", "list"], cwd=tmp_path)
    assert "command_failed returncode=2 stdout=out stderr=bad" in caplog.text


def test_run_command_logs_spawn_error_and_reraises(tmp_path, caplog):
    error = FileNotFoundError("pi")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(FileNotFoundError):
            runner.run_command(["pi"], cwd=tmp_path)
    assert "command_spawn_failed error=pi" in caplog.text


def test_run_command_logs_optional_timeout_and_reraises(tmp_path, caplog):
    error = subprocess.TimeoutExpired(["command"], 7, output="partial", stderr="wait")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.TimeoutExpired):
            runner.run_command(["command"], cwd=tmp_path, timeout=7)
    assert "command_timeout timeout=7 stdout=partial stderr=wait" in caplog.text


def test_single_line_flattens_line_breaks_to_visible_escapes():
    assert runner.single_line("a\nb") == "a\\nb"
    assert runner.single_line("a\r\nb") == "a\\nb"
    assert runner.single_line("a\rb") == "a\\nb"
    assert runner.single_line("no breaks") == "no breaks"


def test_run_command_logs_multiline_body_as_one_journal_line(
        monkeypatch, tmp_path, caplog):
    # The progress comment body (progress.py) is multi-line; it reaches
    # the journal through the `command=` log of the gh api call.
    body = (
        "<!-- orbi:run=e6f5ec8a -->\n"
        "**Orbi progress**\n"
        "- run_id=e6f5ec8a\n"
        "- role: implement\n"
        "- priority: normal\n"
        "- phase: read\n"
        "- elapsed: 8m 2s"
    )
    command = [
        "gh", "api", "repos/xqliu/orbi/issues/143/comments",
        "--method", "POST", "--field", f"body={body}",
    ]
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    completed = Mock(stdout='{"id": 1}', stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    with caplog.at_level("INFO"):
        assert runner.run_command(command, cwd=tmp_path) == '{"id": 1}'
    # Exactly one command= record and it is a single journal line.
    command_records = [
        record for record in caplog.records
        if record.getMessage().startswith("command=")
    ]
    assert len(command_records) == 1
    message = command_records[0].getMessage()
    assert "\n" not in message
    # The body fields stay fully visible on that one line.
    for field in (
        "- run_id=e6f5ec8a", "- role: implement", "- priority: normal",
        "- phase: read", "- elapsed: 8m 2s",
    ):
        assert field in message
    # The real command (and thus the GitHub comment body) is unchanged.
    runner.subprocess.run.assert_called_once_with(
        command, cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=None,
    )


# --- GitHub adapter: the orbi.github leaf functions --------------------------

def test_parse_issue_list_returns_first_issue():
    issue = {"number": 7, "title": "ship", "body": "do it"}
    assert runner.parse_issue_list(json.dumps([issue])) == issue


def test_parse_issue_list_returns_none_for_empty_list():
    assert runner.parse_issue_list("[]") is None


def test_parse_issue_list_rejects_non_list():
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.parse_issue_list("{}")


def test_parse_paginated_issue_array_rejects_malformed_items():
    with pytest.raises(ValueError, match="non-object item"):
        runner.parse_paginated_issue_array('[[{"number": 1}, "bad"], []]')
    with pytest.raises(ValueError, match="array of arrays"):
        runner.parse_paginated_issue_array("[{}]")


def test_list_issues_builds_each_parameter_combination(monkeypatch):
    """Issue #299: `list_issues` emits the exact `gh issue list` command
    for each parameter combination the call sites use (search / label +
    milestone / timeout) and returns the parsed array."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps([{"number": 1}])

    monkeypatch.setattr(seam, "run_command", fake_run)

    # state + search: the common ready/in-flight scan shape.
    assert runner.list_issues(
        "owner/repo", state="open", search="label:ai-ready",
        json_fields="number,title", limit=200,
    ) == [{"number": 1}]

    # label + state + milestone: the release gate shape (milestone last).
    assert runner.list_issues(
        "owner/repo", label="ai-in-progress", state="open",
        milestone="v0.4.3", json_fields="number", limit=50,
    ) == [{"number": 1}]

    # state + search + timeout: the idle-advance shape (timeout passed on).
    assert runner.list_issues(
        "owner/repo", state="all", search='in:body "marker"',
        json_fields="number", limit=1, timeout=30,
    ) == [{"number": 1}]

    # search only (state omitted): gh defaults the state to open.
    assert runner.list_issues(
        "owner/repo", search="label:ai-ready",
        json_fields="number", limit=1,
    ) == [{"number": 1}]

    assert calls == [
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--state", "open", "--search", "label:ai-ready",
            "--json", "number,title", "--limit", "200",
        ], {}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--label", "ai-in-progress", "--state", "open",
            "--json", "number", "--limit", "50", "--milestone", "v0.4.3",
        ], {}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--state", "all", "--search", 'in:body "marker"',
            "--json", "number", "--limit", "1",
        ], {"timeout": 30}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--search", "label:ai-ready",
            "--json", "number", "--limit", "1",
        ], {}),
    ]


def test_edit_issue_builds_add_and_remove_command(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/orbi-backlog", add="ai-in-progress", remove="ai-ready")
    assert calls == [[
        "gh", "issue", "edit", "3", "--repo", "xqliu/orbi-backlog",
        "--add-label", "ai-in-progress", "--remove-label", "ai-ready",
    ]]


def test_edit_issue_allows_no_label_change(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/orbi-backlog")
    assert calls == [["gh", "issue", "edit", "3", "--repo", "xqliu/orbi-backlog"]]


def test_comment_issue_runs_gh_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.comment_issue(3, repo="xqliu/orbi-backlog", body="done")
    assert calls == [[
        "gh", "issue", "comment", "3", "--repo", "xqliu/orbi-backlog",
        "--body", "done\n\n<!-- runner="
        + progress.runner_fingerprint() + " -->",
    ]]


def test_open_pr_for_branch_rejects_malformed_and_ambiguous_results(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: "{}")
    with pytest.raises(RuntimeError, match="must return an array"):
        runner.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-3")

    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: "[{}, {}]",
    )
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        runner.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-3")


def test_pr_comments_reads_and_validates(monkeypatch):
    """`pr_comments` is the PR-side twin of `issue_comments`: same JSON
    shape contract, same fail-fast on a malformed answer."""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"comments": [{"body": "x"}]}),
    )
    assert github.pr_comments(9, repo="o/r") == [{"body": "x"}]
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: json.dumps([{"comments": []}]),
    )
    with pytest.raises(ValueError, match="pr view must be a JSON object"):
        github.pr_comments(9, repo="o/r")
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({"comments": "bad"}),
    )
    with pytest.raises(ValueError, match="pr comments must be a JSON array"):
        github.pr_comments(9, repo="o/r")


def test_issue_labels_returns_names_and_fails_fast(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "issue", "view"]
        assert command[-2:] == ["--json", "labels"]
        # Corrupted entries (non-dict, missing name) are skipped; only
        # well-formed names are returned.
        return json.dumps({"labels": [
            {"name": "ai-ready"}, "garbage",
            {"name": "ai-fix-needed"}, {"other": 1},
        ]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.issue_labels(39, "owner/repo") == [
        "ai-ready", "ai-fix-needed",
    ]

    def bad_run(command, **kwargs):
        return json.dumps({"labels": "nope"})

    monkeypatch.setattr(seam, "run_command", bad_run)
    with pytest.raises(ValueError, match="issue labels must be a JSON array"):
        runner.issue_labels(39, "owner/repo")

    def bad_run2(command, **kwargs):
        return json.dumps(["ai-ready"])

    monkeypatch.setattr(seam, "run_command", bad_run2)
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_labels(39, "owner/repo")


def test_trusted_issue_comments_block_filters_untrusted_and_keeps_order(
    monkeypatch,
):
    """Issue #745: only trusted authors (the #45 authorAssociation set
    or the runner's own App) enter the task context — a public repo
    lets anyone comment, and an unfiltered injection would be a prompt
    injection surface. The timeline order is preserved (oldest first)."""
    # A NONE-association comment carrying a login reaches the
    # authenticated-login fallback; pin it so the test never depends on
    # the host's real gh login state (CI runs unauthenticated).
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "first decision"},
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "second decision"},
    ]
    block = github.trusted_issue_comments_block(comments, limit=20)
    assert "public drive-by" not in block
    assert "mallory" not in block
    first = block.index("- alice (OWNER) at 2026-09-12T01:00:00Z:")
    second = block.index("- carol (MEMBER) at 2026-09-12T03:00:00Z:")
    assert block.index("first decision") < second < block.index(
        "second decision",
    )
    # Below the limit: no omission note, the timeline speaks for itself.
    assert "omitted" not in block


def test_trusted_issue_comments_block_truncates_to_the_newest_with_a_visible_note():
    """Issue #745: over the limit the OLDEST trusted comments are
    dropped (the newest carry the latest decision) and the omission is
    stated in the block — the agent must know it did not see the full
    history, never a silent truncation."""
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "oldest decision"},
        {"author": {"login": "bob"}, "authorAssociation": "MAINTAINER",
         "createdAt": "2026-09-12T02:00:00Z", "body": "middle decision"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "newest decision"},
    ]
    block = github.trusted_issue_comments_block(comments, limit=2)
    assert block.startswith(
        "(1 older trusted comment omitted; showing the 2 most recent)",
    )
    assert "newest decision" in block
    assert "middle decision" in block
    assert "oldest decision" not in block


def test_trusted_issue_comments_block_states_when_nothing_is_trusted(
    monkeypatch,
):
    """Issue #745: zero trusted comments produce an explicit marker, not
    an empty string — the agent can tell an empty timeline apart from a
    missing section."""
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    block = github.trusted_issue_comments_block([
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
    ], limit=20)
    assert block == "(no trusted comments)"
    block = github.trusted_issue_comments_block([], limit=20)
    assert block == "(no trusted comments)"


def test_epic_issue_with_blockers_rejects_invalid_details(monkeypatch):
    with pytest.raises(ValueError, match="number is missing"):
        runner.epic_issue_with_blockers("o/r", {"number": "20"})
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(ValueError, match="details are not an object"):
        runner.epic_issue_with_blockers("o/r", {"number": 20})


def test_verify_epic_complete_uses_native_sub_issue_shapes(monkeypatch):
    base = {"number": 20, "blockedBy": {"nodes": []}}
    monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: {"number": "bad", "blockedBy": {"nodes": []}})
    with pytest.raises(ValueError, match="Epic number is missing"):
        github._verify_epic_complete("o/r", base)
    cases = [
        ({"number": "bad"}, "invalid number"),
        ({"number": 21, "repository": {}, "state": "closed"}, "repository state is malformed"),
        ({"number": 21, "repository": {"full_name": "other/r"}, "state": "closed"}, "cross-repository"),
        ({"number": 21, "repository": {"full_name": "o/r"}}, "state is malformed"),
    ]
    for child, message in cases:
        monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: base)
        monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([[child]]))
        with pytest.raises(ValueError, match=message):
            github._verify_epic_complete("o/r", base)
    pr = {"number": 21, "repository": {"full_name": "o/r"},
          "state": "closed", "pull_request": {}}
    def pr_run(command, **kwargs):
        if "sub_issues?per_page=100" in command[2]:
            return json.dumps([[pr]])
        if command[-1] == "repos/o/r/issues/21":
            return json.dumps({"pull_request": {}, "state": "closed"})
        if command[-1] == "repos/o/r/pulls/21":
            return json.dumps({"merged": False})
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", pr_run)
    with pytest.raises(ValueError, match="not merged"):
        github._verify_epic_complete("o/r", base)
    with pytest.raises(AssertionError):
        pr_run(["x", "y", "z"])


def test_verify_epic_complete_rejects_malformed_native_blockers(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "number": 20, "body": "## Children\n- #21",
    }))
    with pytest.raises(ValueError, match="native blocker/dependency state is unavailable"):
        github._verify_epic_complete("o/r", {"number": 20})


def test_verify_epic_complete_rejects_malformed_native_blocker_node():
    with pytest.raises(ValueError, match="native blocker/dependency state is malformed"):
        github._verify_epic_complete("o/r", {
            "number": 20, "body": "## Children\n- #21",
            "blockedBy": {"nodes": [{"state": "OPEN"}]},
        })


def test_epic_child_evidence_validates_issue_and_pr_shapes(monkeypatch):
    responses = {
        "repos/o/r/issues/1": json.dumps({"state": "closed"}),
        "repos/o/r/issues/2": json.dumps({"pull_request": {}, "state": "open"}),
        "repos/o/r/pulls/2": json.dumps({"merged": True}),
    }
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: responses[command[-1]])
    assert github._epic_child_evidence("o/r", "issue", 1) == "Issue #1 closed"
    assert github._epic_child_evidence("o/r", "pr", 2) == "PR #2 merged"
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(ValueError, match="not an object"):
        github._epic_child_evidence("o/r", "issue", 3)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}))
    with pytest.raises(ValueError, match="declared as Issue"):
        github._epic_child_evidence("o/r", "issue", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"state": "closed"}))
    with pytest.raises(ValueError, match="declared as PR"):
        github._epic_child_evidence("o/r", "pr", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}) if "issues" in command[-1] else json.dumps({"merged": False}))
    with pytest.raises(ValueError, match="not merged"):
        github._epic_child_evidence("o/r", "pr", 2)
