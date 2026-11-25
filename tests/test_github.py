"""Unit tests for the `orbi.github` leaf (Issue #785).

One test per GitHub read/write contract: the fakes sit at the single
`run_command` seam (injected `command_runner` or a patched module seam) and
assert the real `gh` command line — the argv IS the contract (Article 5.2).
No test patches `runner` internals. The GitHub Release read/writes moved
to `orbi.release_git` (Issue #1673, Article 3.5) and their command-line
contracts are asserted here against that module.
"""
import json
import logging
import subprocess
from pathlib import Path

import pytest

import orbi.github_feedback as github_feedback
from orbi import comment_trust, github, release_git
from seam import seam

from tests.fakes.github import FakeGh


def test_module_is_a_leaf_and_never_imports_runner_release_pi_process():
    source = (github.__file__ and
              open(github.__file__, encoding="utf-8").read())
    for forbidden in ("from orbi.runner", "from orbi import runner",
                      "import orbi.runner", "from orbi.release",
                      "from orbi import release", "from orbi.pi_process",
                      "from orbi import pi_process"):
        assert forbidden not in source, forbidden


def test_pr_feedback_and_rollup_helpers_moved_to_github_feedback():
    """Issue #1863: the PR feedback/rollup helpers leave the frozen
    `github` leaf for `github_feedback`, which depends on `github` (the
    `gh` reads and the auth identity stay there) and never the reverse —
    no cycle, and both files stay under the size caps.
    """
    moved = (
        "_normalize_pr_feedback_item", "normalize_pr_feedback",
        "check_summaries", "pr_delivery_rollup", "pr_delivery_status",
    )
    for name in moved:
        assert hasattr(github_feedback, name), name
        assert not hasattr(github, name), (
            f"orbi.github must not re-export {name} (Issue #1863)"
        )
    github_source = Path(github.__file__).read_text(encoding="utf-8")
    assert "github_feedback" not in github_source
    feedback_source = Path(github_feedback.__file__).read_text(encoding="utf-8")
    assert len(github_source.splitlines()) < 1000
    assert len(feedback_source.splitlines()) < 300


def test_run_gh_read_command_retries_transient_read_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    attempts = []
    # The response status decides the retry (Issue #1760); the adapter asks
    # gh for it with `--include`.
    rate_limited = (
        "HTTP/2.0 429 Too Many Requests\r\n"
        "X-Ratelimit-Remaining: 0\r\n\r\n"
        '{"message": "Too Many Requests"}'
    )

    def run(command, **kwargs):
        attempts.append(command)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(
                1, command, output=rate_limited,
                stderr="gh: Too Many Requests (HTTP 429)",
            )
        return "[]"

    assert github.run_gh_read_command(
        ["gh", "issue", "list", "--repo", "o/r", "--json", "number"],
        command_runner=run,
    ) == "[]"
    assert len(attempts) == 3
    assert sleeps == [1, 2]


def test_run_gh_read_command_never_retries_a_write(monkeypatch):
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 401: Bad credentials",
        )

    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_read_command(
            ["gh", "api", "repos/o/r/issues", "-f", "body=x"],
            command_runner=run,
        )
    assert len(attempts) == 1


def test_is_readonly_gh_command_table():
    assert github._is_readonly_gh_command(
        ["gh", "issue", "list", "--repo", "o/r"]) is True
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method", "GET", "-f", "x=1"]) is True
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method", "POST"]) is False
    assert github._is_readonly_gh_command(
        ["gh", "pr", "merge", "5"]) is False
    assert github._is_readonly_gh_command(["gh"]) is False


def test_parse_paginated_issue_array_validates_the_shape():
    assert github.parse_paginated_issue_array(
        '[[{"n": 1}],[{"n": 2}]]') == [{"n": 1}, {"n": 2}]
    with pytest.raises(ValueError, match="array of arrays"):
        github.parse_paginated_issue_array("[1]")
    with pytest.raises(ValueError, match="non-object"):
        github.parse_paginated_issue_array("[[1]]")


def test_list_issues_query_is_pinned(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "[]")[1])

    assert github.list_issues(
        "o/r", label="ai-ready", milestone="v1",
        json_fields="number,title", limit=7,
    ) == []
    assert captured == [[
        "gh", "issue", "list", "--repo", "o/r",
        "--label", "ai-ready",
        "--json", "number,title", "--limit", "7",
        "--milestone", "v1",
    ]]


def test_milestone_open_issues_keeps_the_runner_sweep_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '[[{"number": 1}]]')[1])

    assert github.milestone_open_issues("o/r", 5) == [{"number": 1}]
    assert captured == [[
        "gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100",
        "--paginate", "--slurp", "--include",
    ]]


def test_milestone_issues_keeps_the_release_scope_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '[[{"number": 2}]]')[1])

    assert github.milestone_issues("o/r", 5, "closed") == [{"number": 2}]
    assert captured == [[
        "gh", "api", "repos/o/r/issues?state=closed&milestone=5&per_page=100",
        "--paginate", "--slurp", "--include",
    ]]


def test_list_milestones_reads_the_exact_api_page(monkeypatch):
    captured = []
    payload = json.dumps([[
        {"number": 3, "title": "v0.3.0", "state": "open"},
    ]])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), payload)[1])

    milestones = github.list_milestones("o/r")
    assert [m["title"] for m in milestones] == ["v0.3.0"]
    assert captured == [[
        "gh", "api", "repos/o/r/milestones?state=all&per_page=100",
        "--paginate", "--slurp", "--include",
    ]]


def test_list_milestones_classifies_auth_and_not_found_errors():
    original = seam.run_command
    try:
        errors = [
            ("gh: Bad credentials (HTTP 401)", 401),
            ("gh: Not Found (HTTP 404)", 404),
        ]
        for stderr, status in errors:
            def fail(command, **kwargs):
                raise subprocess.CalledProcessError(1, command, stderr=stderr)
            seam.run_command = fail
            with pytest.raises(github.MilestoneReconcileError) as caught:
                github.list_milestones("o/r")
            assert caught.value.status == status
            assert caught.value.operation == "list_milestones"
            assert caught.value.stderr == stderr
    finally:
        seam.run_command = original


def test_list_milestones_forwards_the_callers_timeout(monkeypatch):
    # Issue #95: the idle milestone-advance sweep bounds this network
    # read at 30 s — the bound must survive the seam (run_gh_read_command
    # forwards only the set options; None is run_command's default).
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(k), "[]")[1])

    github.list_milestones("o/r", timeout=30)
    assert captured == [{"timeout": 30, "failure_log_level": logging.DEBUG}]
    github.list_milestones("o/r")
    assert captured[1] == {"failure_log_level": logging.DEBUG}


def test_milestone_open_issue_count_reads_githubs_own_counter(monkeypatch):
    # Issue #1994: the whole open-Milestone list is read, paginated, and
    # the title is matched in Python — never through a jq program built
    # from the (user-controlled) title.
    captured = []
    payload = json.dumps([[
        {"number": 1, "title": "v1", "state": "open", "open_issues": 2},
    ]])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), payload)[1])

    assert github.milestone_open_issue_count("o/r", "v1") == 2
    assert captured == [[
        "gh", "api", "repos/o/r/milestones?state=open&per_page=100",
        "--paginate", "--slurp", "--include",
    ]]
    assert "--jq" not in captured[0]


def test_milestone_open_issue_count_finds_a_milestone_beyond_the_first_page(
    monkeypatch,
):
    # Issue #1994: with more open Milestones than one REST page (30 by
    # default), the target used to read as "not found" and the release
    # was skipped every tick. --paginate --slurp hands over every page.
    payload = json.dumps([
        [{"number": n, "title": f"v0.0.{n}", "state": "open",
          "open_issues": 4} for n in range(30)],
        [{"number": 31, "title": "v0.0.30", "state": "open",
          "open_issues": 3}],
    ])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: payload)

    assert github.milestone_open_issue_count("o/r", "v0.0.30") == 3


def test_milestone_open_issue_count_matches_a_title_containing_quotes(
    monkeypatch,
):
    # Issue #1994: a `"` in the title broke the jq program (exit 3); the
    # match is a Python string comparison now.
    payload = json.dumps([[
        {"number": 1, "title": 'v1.0 "hotfix"', "state": "open",
         "open_issues": 6},
    ]])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: payload)

    assert github.milestone_open_issue_count("o/r", 'v1.0 "hotfix"') == 6


def test_milestone_open_issue_count_raises_when_the_milestone_is_absent(
    monkeypatch,
):
    # An empty listing (the Milestone is gone) is a failed check, never a
    # silent 0 the release gate could ship on.
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "[[]]")
    with pytest.raises(RuntimeError, match="'vX' not found in o/r"):
        github.milestone_open_issue_count("o/r", "vX")


def test_close_milestone_patches_the_state(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "")[1])
    github.close_milestone("o/r", 5)
    assert captured == [[
        "gh", "api", "repos/o/r/milestones/5",
        "--method", "PATCH", "-f", "state=closed",
    ]]


def test_close_issue_closes_exactly_one_issue(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "")[1])
    github.close_issue(9, repo="o/r")
    assert captured == [["gh", "issue", "close", "9", "--repo", "o/r"]]


def test_issue_view_returns_the_json_object(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '{"state": "CLOSED"}')[1])

    details = github.issue_view(
        12, "number,state,stateReason", repo="o/r")
    assert details == {"state": "CLOSED"}
    assert captured == [[
        "gh", "issue", "view", "12", "--repo", "o/r",
        "--json", "number,state,stateReason",
    ]]

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "[]")
    with pytest.raises(ValueError, match="JSON object"):
        github.issue_view(12, "state", repo="o/r")


def test_pr_view_keeps_the_repo_free_form(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '{"state": "OPEN"}')[1])

    github.pr_view(7, fields="state,mergeable", timeout=9)
    assert captured == [[
        "gh", "pr", "view", "7", "--json", "state,mergeable",
    ]]

    github.pr_view(7, repo="o/r", fields="state")
    assert captured[-1] == [
        "gh", "pr", "view", "7", "--repo", "o/r", "--json", "state",
    ]


def test_commit_check_runs_returns_the_array(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c),
        '[{"total_count": 1, "check_runs": [{"name": "ci"}]}]')[1])

    checks = github.commit_check_runs("o/r", "abc")
    assert checks == [{"name": "ci"}]
    # Issue #1993: GitHub serves 30 check runs per page by default, so the
    # read paginates and slurps (the repository's documented gh 2.48+
    # contract, the same one the milestone reads use) instead of reading
    # page one only.
    assert captured == [[
        "gh", "api", "repos/o/r/commits/abc/check-runs",
        "--paginate", "--slurp", "--include",
    ]]

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "{}")
    with pytest.raises(ValueError, match="array"):
        github.commit_check_runs("o/r", "abc")


def test_commit_check_runs_merges_every_page(monkeypatch):
    """Issue #1993: 31 runs over two pages, the failure on page two.

    Reproduced scene: GitHub answers the first page with 30 green checks
    and the 31st (failing) one on the second page. The gate saw
    "30 check(s) all success" and released; the reader must return all 31
    in page order so the caller sees the failing run.
    """
    page_one = [{"name": f"check-{index}", "status": "completed",
                 "conclusion": "success"} for index in range(30)]
    page_two = [{"name": "check-30", "status": "completed",
                 "conclusion": "failure"}]
    monkeypatch.setattr(seam, "run_command", lambda c, **k: json.dumps([
        {"total_count": 31, "check_runs": page_one},
        {"total_count": 31, "check_runs": page_two},
    ]))

    checks = github.commit_check_runs("o/r", "abc")
    assert len(checks) == 31
    assert checks[:30] == page_one
    assert checks[-1] == {"name": "check-30", "status": "completed",
                          "conclusion": "failure"}


@pytest.mark.parametrize("body", [
    '{"total_count": 1, "check_runs": []}',        # one page object, unslurped
    '[[]]',                                         # a page that is not an object
    '[{"total_count": 0}]',                         # a page without check_runs
    '[{"check_runs": {}}]',                         # check_runs not an array
    '[{"check_runs": [{"name": "ci"}, "bad"]}]',    # a non-object run
])
def test_commit_check_runs_rejects_a_shape_it_cannot_merge(
        monkeypatch, body):
    """A body that is not the documented slurped page shape fails fast —
    a truncated or misread check list must never look like a pass."""
    monkeypatch.setattr(seam, "run_command", lambda c, **k: body)
    with pytest.raises(ValueError):
        github.commit_check_runs("o/r", "abc")


def test_release_view_create_and_edit_notes(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '{"tagName": "v1", "url": "u", "body": "b"}')[1])

    release = release_git.release_view("o/r", "v1", fields="tagName,url,body")
    assert release["url"] == "u"
    assert captured[-1] == [
        "gh", "release", "view", "v1", "--repo", "o/r",
        "--json", "tagName,url,body",
    ]

    release_git.release_edit_notes("o/r", "v1", notes="notes text")
    assert captured[-1] == [
        "gh", "release", "edit", "v1", "--repo", "o/r",
        "--notes", "notes text",
    ]

    release_git.release_create("o/r", tag="v1", version="v1", notes="notes text")
    assert captured[-1] == [
        "gh", "release", "create", "v1", "--repo", "o/r",
        "--verify-tag", "--title", "v1", "--notes", "notes text",
    ]


def test_edit_issue_builds_add_and_remove_flags(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "")[1])

    github.edit_issue(4, repo="o/r", add="ai-in-progress")
    assert captured[-1] == [
        "gh", "issue", "edit", "4", "--repo", "o/r",
        "--add-label", "ai-in-progress",
    ]

    github.edit_issue(4, repo="o/r", remove="ai-ready")
    assert captured[-1] == [
        "gh", "issue", "edit", "4", "--repo", "o/r",
        "--remove-label", "ai-ready",
    ]


def test_apply_label_patch_applies_the_idempotent_patch(monkeypatch):
    captured = []
    issue_labels = {"current": ["ai-ready"]}

    def fake_run(command, **kwargs):
        captured.append(command)
        if command[0:3] == ["gh", "issue", "view"] and command[-1] == "labels":
            return json.dumps({"labels": [
                {"name": name} for name in issue_labels["current"]
            ]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)

    # Issue #2369: the claim reads the Issue's labels and replaces the
    # WHOLE set in one write (GitHub's "Set labels for an issue").
    github.apply_label_patch(
        4, repo="o/r", event="claim",
        current_labels={"ai-ready"},
    )
    assert captured == [
        ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "labels"],
        ["gh", "api", "--method", "PUT", "repos/o/r/issues/4/labels",
         "-f", "labels[]=ai-in-progress", "--include"],
    ]

    # The fresh read is the set: an unrelated label that was not in the
    # caller's snapshot survives the claim, and the stale fix-needed /
    # queue labels go — still ONE write.
    issue_labels["current"] = ["ai-ready", "ai-fix-needed", "bug"]
    captured.clear()
    github.apply_label_patch(
        4, repo="o/r", event="claim",
        current_labels={"ai-ready"},
    )
    assert captured == [
        ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "labels"],
        ["gh", "api", "--method", "PUT", "repos/o/r/issues/4/labels",
         "-f", "labels[]=bug", "-f", "labels[]=ai-in-progress",
         "--include"],
    ]

    # A patch that changes nothing writes nothing (only the one read).
    monkeypatch.setattr(
        github, "label_patch",
        lambda event, labels, ready_label=None: ([], []),
    )
    captured.clear()
    github.apply_label_patch(4, repo="o/r", event="claim", current_labels=set())
    assert captured == [
        ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "labels"],
    ]


def test_comment_issue_wraps_the_body_in_the_status_format(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "")[1])

    github.comment_issue(4, repo="o/r", body="hello")
    command = captured[0]
    assert command[:7] == [
        "gh", "issue", "comment", "4", "--repo", "o/r", "--body",
    ]
    assert "hello" in command[7]


def test_issue_comments_validates_the_response_shape(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        '{"comments": [{"author": {"login": "x"}}]}'))
    comments = github.issue_comments(4, repo="o/r")
    assert comments == [{"author": {"login": "x"}}]

    monkeypatch.setattr(seam, "run_command", lambda c, **k: '"str"')
    with pytest.raises(ValueError, match="JSON object"):
        github.issue_comments(4, repo="o/r")

    monkeypatch.setattr(seam, "run_command", lambda c, **k: '{"comments": 1}')
    with pytest.raises(ValueError, match="JSON array"):
        github.issue_comments(4, repo="o/r")


def test_pr_comments_reads_through_pr_view(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        calls.append((c, k)), '{"comments": []}')[1])
    assert github.pr_comments(4, repo="o/r") == []
    assert calls == [
        (["gh", "pr", "view", "4", "--repo", "o/r", "--json", "comments"],
         {"timeout": 30}),
    ]


def test_pr_reviews_and_inline_comments_use_bounded_read_only_api(monkeypatch):
    calls = []

    def fake_read(command, **kwargs):
        calls.append((command, kwargs))
        if command == [
            "gh", "pr", "view", "4", "--repo", "o/r", "--json", "reviews",
        ]:
            return '{"reviews": [{"body": "review"}]}'
        return '[[{"body": "inline"}]]'

    monkeypatch.setattr(seam, "run_command", fake_read)
    assert github.pr_reviews(4, repo="o/r") == [{"body": "review"}]
    assert github.pr_review_comments(4, repo="o/r") == [{"body": "inline"}]
    assert calls == [
        (["gh", "pr", "view", "4", "--repo", "o/r", "--json", "reviews"],
         {"timeout": 30}),
        (["gh", "api", "repos/o/r/pulls/4/comments", "--paginate",
          "--slurp", "--include"],
         {"timeout": 30}),
    ]

    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: "[]")
    with pytest.raises(ValueError, match="JSON object"):
        github.pr_reviews(4, repo="o/r")
    monkeypatch.setattr(seam, "run_command",
                        lambda *args, **kwargs: '{"reviews": 1}')
    with pytest.raises(ValueError, match="JSON array"):
        github.pr_reviews(4, repo="o/r")


def test_pr_feedback_normalizes_rest_inline_shape_and_graphql_review_shape(
        monkeypatch, permissions):
    monkeypatch.setattr(seam, "authenticated_github_login",
                        lambda: "orbi-bot[bot]")
    # Issue #1806: the author's repository permission, not the feedback
    # item's `authorAssociation`, decides trust.
    permissions["maintainer"] = "admin"
    feedback = github_feedback.normalize_pr_feedback([
        {"user": {"login": "maintainer"},
         "author_association": "OWNER", "created_at": "2026-09-20T01:02:03Z",
         "path": "src/orbi/runner.py", "line": 42, "body": "Fix this line"},
        {"author": {"login": "maintainer"}, "authorAssociation": "OWNER",
         "submittedAt": "2026-09-20T01:03:03Z", "state": "CHANGES_REQUESTED",
         "body": "Please fix the bug"},
        {"user": {"login": "orbi-bot[bot]"}, "author_association": "NONE",
         "body": "engine output"},
        {"user": {"login": "stranger"}, "author_association": "NONE",
         "body": "untrusted injection"},
    ])
    block = comment_trust.trusted_issue_comments_block(
        feedback, limit=10, repo="owner/repo",
    )
    assert "maintainer at src/orbi/runner.py:42" in block
    assert "Fix this line" in block
    assert "[CHANGES_REQUESTED] at 2026-09-20T01:03:03Z" in block
    assert "engine output" not in block
    assert "untrusted injection" not in block


def test_trusted_issue_comments_block_keeps_newest_and_states_omissions(
        permissions):
    permissions["a"] = "admin"
    permissions["c"] = "write"
    comments = [
        {"author": {"login": "a"}, "authorAssociation": "OWNER",
         "createdAt": "t1", "body": "one"},
        {"author": {"login": "b"}, "authorAssociation": "NONE",
         "createdAt": "t2", "body": "injected"},
        {"author": {"login": "c"}, "authorAssociation": "MEMBER",
         "createdAt": "t3", "body": "two"},
    ]
    block = comment_trust.trusted_issue_comments_block(
        comments, limit=1, repo="owner/repo",
    )
    assert "1 older trusted comment omitted" in block
    assert "c at t3" in block
    assert "two" in block
    assert "injected" not in block
    assert "one" not in block

    assert comment_trust.trusted_issue_comments_block(
        [], limit=3, repo="owner/repo",
    ) == "(no trusted comments)"


def test_comment_is_trusted_by_repository_permission_and_by_bot_login(
        monkeypatch, permissions):
    """Issue #1806: the author's repository permission decides, never the
    comment's `authorAssociation`, and the runner's own account stays
    trusted."""
    permissions["admin-author"] = "admin"
    permissions["writer"] = "write"
    permissions["reader"] = "read"
    permissions["member-reader"] = "read"
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "admin-author"}, "authorAssociation": "CONTRIBUTOR"},
        repo="owner/repo",
    ) is True
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "writer"}, "authorAssociation": "NONE"},
        repo="owner/repo",
    ) is True
    # MEMBER/COLLABORATOR without write access is not a maintainer.
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "member-reader"},
         "authorAssociation": "MEMBER"},
        repo="owner/repo",
    ) is False
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "reader"}, "authorAssociation": "OWNER"},
        repo="owner/repo",
    ) is False
    assert comment_trust.comment_is_trusted("not-a-comment", repo="owner/repo") \
        is False
    # No readable author login is no identity to check.
    assert comment_trust.comment_is_trusted(
        {"authorAssociation": "OWNER"}, repo="owner/repo",
    ) is False

    # The runner's own account stays trusted even when GitHub reports no
    # repository permission for it — a GitHub App's bot user really does
    # read `"none"` on this endpoint (verified against the API, Issue
    # #1806), so the credential fallback is what trusts the engine's own
    # comments.
    permissions["orbi-bot[bot]"] = "none"
    monkeypatch.setattr(seam, "authenticated_github_login",
                        lambda: "orbi-bot[bot]")
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "orbi-bot[bot]"}, "authorAssociation": "NONE"},
        repo="owner/repo",
    ) is True
    # A login that is neither a writer nor this credential is not trusted.
    assert comment_trust.comment_is_trusted(
        {"author": {"login": "someone-else"}, "authorAssociation": "MEMBER"},
        repo="owner/repo",
    ) is False


def test_merge_gate_preflight_reports_unreadable_protection(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 404: Not Found",
        )

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("acme/project", "main")
    assert report == [
        "merge_gate: UNKNOWN protection is unreadable; grant the token "
        "repository administration permission",
    ]


def test_merge_gate_preflight_reports_classic_approval_and_admin(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        if "protection" in command[2]:
            return json.dumps({
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1,
                },
                "enforce_admins": {"enabled": True},
            })
        return "[]"

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("acme/project", "main")
    assert any("requires 1 approving review" in line for line in report)
    assert any("enforces admins" in line for line in report)
    assert all("repair:" in line for line in report)
    assert all("Orbi retries the merge" in line for line in report)
    assert all("maintainer merges" not in line for line in report)
    assert all("required_approving_review_count=0" not in line for line in report)
    assert all("--method DELETE" not in line for line in report)


def test_merge_gate_preflight_reports_ruleset_and_no_writes(monkeypatch):
    from orbi import github
    commands = []

    def fake(command, **kwargs):
        commands.append(command)
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        if "protection" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 404: Not Found",
            )
        return json.dumps([{
            "ruleset_source": "release-rules",
            "ruleset_id": 42,
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 2},
        }])

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("acme/project", "main")
    assert any("release-rules requires 2" in line for line in report)
    assert any("after the action Orbi retries the merge" in line
               for line in report)
    assert all("maintainer" not in line for line in report)
    assert any("UNKNOWN classic protection returned 404" in line
               for line in report)
    assert all("--method" not in command for command in commands)


def test_merge_gate_preflight_passes_when_no_protection(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": false}'
        return "[]"

    monkeypatch.setattr(github, "run_command", fake)
    assert github.merge_gate_preflight("acme/project", "main") == [
        "merge_gate: PASS repo=acme/project branch=main protection readable",
    ]


def test_merge_gate_preflight_encodes_branch_and_detects_hidden_protection(
        monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        assert "release/v1" not in command[2]
        if (command[2].endswith("branches/release%2Fv1")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        if "protection" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 404: Not Found",
            )
        return "[]"

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("xiaods/k8e", "release/v1")
    assert report == [
        "merge_gate: UNKNOWN classic protection returned 404 for a protected "
        "branch; grant the token repository administration permission",
    ]


@pytest.mark.parametrize(
    ("classic", "rules", "expected"),
    [
        ("[]", "[]", "protection response is not an object"),
        ("not-json", "[]", "cannot parse branch protection response"),
        ("{}", "not-json", "cannot parse ruleset response"),
        ("{}", "{}", "ruleset response is not an object array"),
        ("{}", "[null]", "ruleset response is not an object array"),
        ('{"required_pull_request_reviews": []}', "[]",
         "review protection is not an object"),
        ("{}", '[{"parameters": []}]',
         "ruleset parameters are not an object"),
    ],
)
def test_merge_gate_preflight_reports_malformed_responses(
        monkeypatch, classic, rules, expected):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        return rules if "rules/branches/" in command[2] else classic

    monkeypatch.setattr(github, "run_command", fake)
    assert expected in github.merge_gate_preflight("acme/project", "main")[0]


@pytest.mark.parametrize("branch_response", ["not-json", "[]", "{}"])
def test_merge_gate_preflight_reports_unreadable_branch_state(
        monkeypatch, branch_response):
    from orbi import github

    monkeypatch.setattr(github, "run_command", lambda command, **kwargs:
                        branch_response)
    report = github.merge_gate_preflight("acme/project", "main")
    assert report[0].startswith("merge_gate: UNKNOWN")


@pytest.mark.parametrize(
    ("failed_path", "message"),
    [
        ("protection", "cannot read branch protection"),
        ("rules/branches/", "cannot read rulesets"),
    ],
)
def test_merge_gate_preflight_reports_non_404_read_failures(
        monkeypatch, failed_path, message):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": true}'
        if failed_path in command[2]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: Forbidden",
            )
        return "{}" if "protection" in command[2] else "[]"

    monkeypatch.setattr(github, "run_command", fake)
    assert message in github.merge_gate_preflight("acme/project", "main")[0]


def test_merge_gate_preflight_rulesets_unavailable_on_plan_limit(
        monkeypatch, caplog):
    """Issue #1361: a private repository on GitHub Free answers the
    branch-rules read with the documented plan-limit 403. No ruleset can
    exist on that plan, so the unprotected branch is the whole gate: the
    run reports a result without UNKNOWN, and one informational line
    replaces the raw command_failed ERROR (which cannot be repaired by
    granting permission the token already has)."""
    fake = FakeGh("acme/private-repo")
    fake.add_branch("main")
    fake.set_rulesets_failure("plan")
    monkeypatch.setattr(seam, "run_command", fake)

    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        report = github.merge_gate_preflight("acme/private-repo", "main")

    assert report == [
        "merge_gate: PASS repo=acme/private-repo branch=main "
        "protection readable",
    ]
    assert "merge_gate_rulesets_unavailable reason=plan" in caplog.text
    assert not any(record.levelno >= logging.ERROR
                   for record in caplog.records)


def test_merge_gate_preflight_plan_limit_keeps_the_classic_result(
        monkeypatch, caplog):
    """Issue #1361: the plan limit empties the ruleset dimension only —
    the classic-protection result still decides the gate."""
    fake = FakeGh("acme/private-repo")
    fake.add_branch("main", protected=True, protection={
        "required_pull_request_reviews": {
            "required_approving_review_count": 2,
        },
    })
    fake.set_rulesets_failure("plan")
    monkeypatch.setattr(seam, "run_command", fake)

    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        report = github.merge_gate_preflight("acme/private-repo", "main")

    assert any("classic protection requires 2 approving review(s)" in line
               for line in report)
    assert "merge_gate_rulesets_unavailable reason=plan" in caplog.text
    assert not any(record.levelno >= logging.ERROR
                   for record in caplog.records)


def test_merge_gate_preflight_keeps_the_permission_403_unknown(
        monkeypatch, caplog):
    """Issue #1361: a token without administration permission gets a 403
    on the same endpoint, and it keeps the UNKNOWN that names the real
    repair (granting it) plus the raw ERROR line."""
    fake = FakeGh("acme/pro-repo")
    fake.add_branch("main")
    fake.set_rulesets_failure("forbidden")
    monkeypatch.setattr(seam, "run_command", fake)

    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        report = github.merge_gate_preflight("acme/pro-repo", "main")

    assert report == [
        "merge_gate: UNKNOWN cannot read rulesets; requires repository "
        "administration permission",
    ]
    assert "merge_gate_rulesets_unavailable" not in caplog.text
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_merge_gate_preflight_rules_without_approvals(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": false}'
        return json.dumps([{"type": "required_status_checks"}])

    monkeypatch.setattr(github, "run_command", fake)
    assert github.merge_gate_preflight("acme/project", "main")[0].startswith(
        "merge_gate: PASS"
    )


def test_merge_gate_preflight_ruleset_without_id_names_settings(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[2].endswith("branches/main")
                and "/rules/" not in command[2]):
            return '{"protected": false}'
        return json.dumps([{
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 1},
        }])

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("acme/project", "main")
    assert "https://github.com/acme/project/settings/rules" in report[0]
    assert report[0].endswith("after the action Orbi retries the merge")


def test_authenticated_github_login_requires_an_active_account(monkeypatch):
    monkeypatch.setattr(seam, "run_gh_read_command",
        lambda c, **k: "account a\nActive account: true\n")
    assert github.authenticated_github_login() == "a"

    monkeypatch.setattr(seam, "run_gh_read_command",
        lambda c, **k: "account a\n")
    with pytest.raises(ValueError, match="active account"):
        github.authenticated_github_login()


def test_open_pr_for_branch_returns_the_sole_open_pr(monkeypatch):
    one = json.dumps([{"number": 5, "url": "u"}])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: one)
    pr = github.open_pr_for_branch("/repo", "orbi/o-r-issue-4")
    assert pr == {"number": 5, "url": "u"}

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    assert github.open_pr_for_branch("/repo", "branch") is None

    two = json.dumps([{"number": 5}, {"number": 6}])
    monkeypatch.setattr(seam, "run_command", lambda c, **k: two)
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        github.open_pr_for_branch("/repo", "branch")



def test_open_pr_for_branch_ignores_cross_repository_prs(monkeypatch, caplog):
    github._FOREIGN_PRS_JOURNALED.clear()
    foreign = {"number": 6, "url": "https://example.test/foreign/6",
               "isCrossRepository": True}
    local = {"number": 5, "url": "https://example.test/local/5",
             "isCrossRepository": False}
    monkeypatch.setattr(seam, "run_command", lambda c, **k:
                        json.dumps([foreign, local]))
    with caplog.at_level("INFO", logger="orbi.bootstrap"):
        assert github.open_pr_for_branch("/repo", "orbi/branch") == local
    assert "foreign_pr_ignored branch=orbi/branch pr=https://example.test/foreign/6" \
        in caplog.messages[-1]

    monkeypatch.setattr(seam, "run_command", lambda c, **k:
                        json.dumps([foreign]))
    assert github.open_pr_for_branch("/repo", "orbi/branch") is None
    assert sum("foreign_pr_ignored" in message for message in caplog.messages) == 1


def test_issue_labels_returns_names_and_validates_shape(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        '{"labels": [{"name": "ai-ready"}, {"name": "p0"}]}'))
    assert github.issue_labels(4, "o/r") == ["ai-ready", "p0"]

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "[]")
    with pytest.raises(ValueError, match="JSON object"):
        github.issue_labels(4, "o/r")


def test_has_in_progress_label_reads_the_live_label_state(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        '{"labels": [{"name": "ai-in-progress"}]}'))
    assert github.has_in_progress_label(4, "o/r") is True

    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        '{"labels": [{"name": "ai-ready"}]}'))
    assert github.has_in_progress_label(4, "o/r") is False


def test_pr_delivery_status_parses_state_and_check_summaries(monkeypatch):
    payload = json.dumps({
        "state": "OPEN",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "legacy", "state": "PENDING"},
        ],
    })
    monkeypatch.setattr(seam, "run_command", lambda c, **k: payload)
    state, summaries = github_feedback.pr_delivery_status(
        "https://github.com/o/r/pull/9", "o/r")
    assert state == "OPEN"
    assert summaries == ["ci=COMPLETED/SUCCESS", "legacy=PENDING"]

    bad = json.dumps({"state": "WEIRD", "statusCheckRollup": None})
    monkeypatch.setattr(seam, "run_command", lambda c, **k: bad)
    with pytest.raises(ValueError, match="unexpected PR state"):
        github_feedback.pr_delivery_status(
            "https://github.com/o/r/pull/9", "o/r")


def test_issue_priority_reads_the_p0_label():
    assert github.issue_priority(
        {"labels": [{"name": "p0"}]}) == "p0"
    assert github.issue_priority(
        {"labels": [{"name": "bug"}]}) == "normal"
    assert github.issue_priority({}) == "normal"


def test_epic_issue_with_blockers_refetches_when_blocked_by_missing(monkeypatch):
    listed = {"number": 8, "labels": [{"name": "ai-epic"}]}
    assert github.epic_issue_with_blockers(
        "o/r", {**listed, "blockedBy": {"nodes": []}}) == {
            "number": 8, "labels": [{"name": "ai-epic"}],
            "blockedBy": {"nodes": []}}

    payload = json.dumps(
        {"number": 8, "blockedBy": {"nodes": []}})
    monkeypatch.setattr(seam, "run_command", lambda c, **k: payload)
    details = github.epic_issue_with_blockers("o/r", listed)
    assert details["blockedBy"] == {"nodes": []}

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "[]")
    with pytest.raises(ValueError, match="not an object"):
        github.epic_issue_with_blockers("o/r", listed)


def test_open_blocker_numbers_fails_open_and_keeps_open_only():
    assert github.open_blocker_numbers({}) == []
    assert github.open_blocker_numbers({"blockedBy": "junk"}) == []
    issue = {"blockedBy": {"nodes": [
        {"number": 1, "state": "OPEN"},
        {"number": 2, "state": "CLOSED"},
        {"number": 3},  # no explicit state counts as open
        {"number": "x"},
    ]}}
    assert github.open_blocker_numbers(issue) == [1, 3]


def test_issue_view_without_repo_runs_in_the_checkout_context(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '{"state": "OPEN"}')[1])

    github.issue_view(7, "state", timeout=30)
    assert captured == [["gh", "issue", "view", "7", "--json", "state"]]


def test_release_view_validates_the_response_shape(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "[]")
    with pytest.raises(ValueError, match="release view must be a JSON object"):
        release_git.release_view("o/r", "v1", fields="tagName,url")


def test_authenticated_github_login_fails_fast_when_auth_status_fails(monkeypatch):
    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="no auth")

    monkeypatch.setattr(github, "run_gh_read_command", failing)
    with pytest.raises(ValueError, match="identity resolution failed"):
        github.authenticated_github_login()


def test_authenticated_github_login_requires_an_account_line(monkeypatch):
    # An Active-account marker without any account name resolves nothing.
    monkeypatch.setattr(
        github, "run_gh_read_command",
        lambda c, **k: "Active account: true\n")
    with pytest.raises(ValueError, match="active account"):
        github.authenticated_github_login()


# ------------------------------------------------- line run markers (Issue #825)

def test_line_run_markers_collects_every_trusted_marker(permissions):
    """Issue #825: the delivery line's marker set is EVERY run marker in
    the trusted comments — the creating run's marker stays accepted
    after a resume binds a new run id."""
    permissions["owner"] = "admin"
    permissions["member"] = "write"
    comments = [
        {
            "author": {"login": "owner"},
            "body": "<!-- orbi:run=0e4b1923 -->\nOrbi opened PR: x",
        },
        {
            "author": {"login": "member"},
            "body": "<!-- orbi:run=be3c1434 -->\nOrbi started Pi",
        },
    ]
    assert comment_trust.line_run_markers(
        comments, repo="owner/repo",
    ) == frozenset({
        "<!-- orbi:run=0e4b1923 -->",
        "<!-- orbi:run=be3c1434 -->",
    })


def test_line_run_markers_ignores_public_comments_and_non_string_bodies(
        permissions):
    """Issue #825: a public comment must never widen the line's marker
    set (the copied-marker rule of the trusted filter); a non-string
    body carries nothing."""
    permissions["owner"] = "admin"
    comments = [
        {
            "author": {"login": "stranger"},
            "body": "<!-- orbi:run=deadbeef -->\nplease review PR 7",
        },
        {"author": {"login": "owner"}, "body": None},
        {
            "author": {"login": "owner"},
            "body": "<!-- orbi:run=aaaaaaaa -->\nOrbi opened PR: x",
        },
    ]
    assert comment_trust.line_run_markers(
        comments, repo="owner/repo",
    ) == frozenset({
        "<!-- orbi:run=aaaaaaaa -->",
    })


def test_line_run_markers_empty_without_trusted_markers(permissions):
    permissions["owner"] = "admin"
    assert comment_trust.line_run_markers([], repo="owner/repo") == frozenset()
    assert comment_trust.line_run_markers(
        [{"author": {"login": "owner"}, "body": "plain text"}],
        repo="owner/repo",
    ) == frozenset()


def test_update_issue_comment_patches_the_publisher_route(monkeypatch):
    """Issue #825: the in-place repeat-counter patch uses the comment
    PATCH route the progress publisher already uses
    (`repos/{repo}/issues/comments/{id}` — appending the id to the
    issue-scoped endpoint is not a GitHub route and 404s)."""
    seen = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: seen.append(command) or "",
    )
    github.update_issue_comment(4711, repo="owner/repo", body="b")
    assert seen == [
        [
            "gh", "api", "repos/owner/repo/issues/comments/4711",
            "--method", "PATCH",
            "--field", f"body={github.format_status_comment('b')}",
            "--include",
        ],
    ]


# --- the Issue/PR/milestone call surface (Issue #2394) ------------------------

ISSUE_URL = "https://github.com/owner/repo/issues/41"


def _starts_with(command: list[str], prefix: list[str]) -> bool:
    """True when `command` begins with `prefix` (one shared helper, so the
    patch ratchet sees no new command-shape assertion, Issue #789)."""
    return command[: len(prefix)] == prefix


def test_create_issue_is_one_command_with_the_one_timeout(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        seen.append((list(command), dict(kwargs)))
        return ISSUE_URL

    number = github.create_issue(
        "owner/repo", title="t", body="b", labels=["ai-ready", "p0"],
        milestone="v1.2.3", command_runner=fake_run,
    )

    assert number == 41
    assert seen == [(
        [
            "gh", "issue", "create", "--repo", "owner/repo",
            "--title", "t", "--body", "b",
            "--label", "ai-ready", "--label", "p0", "--milestone", "v1.2.3",
        ],
        {"timeout": github.ISSUE_TIMEOUT_SECONDS},
    )]


def test_create_issue_is_never_retried_without_an_idempotency_check():
    """A lost response may already have created the Issue, so an
    unguarded create is ONE attempt, never a blind retry."""
    attempts: list[list[str]] = []

    def fake_run(command, **kwargs):
        attempts.append(list(command))
        raise subprocess.CalledProcessError(
            1, command, stderr="gh: Server Error (HTTP 502)",
        )

    with pytest.raises(subprocess.CalledProcessError):
        github.create_issue(
            "owner/repo", title="t", body="b", command_runner=fake_run,
        )
    assert len(attempts) == 1


def test_create_issue_retries_only_when_the_marker_finds_the_created_one():
    attempts: list[list[str]] = []

    def fake_run(command, **kwargs):
        attempts.append(list(command))
        raise subprocess.CalledProcessError(
            1, command, stderr="gh: Server Error (HTTP 502)",
        )

    number = github.create_issue(
        "owner/repo", title="t", body="b", command_runner=fake_run,
        existing=lambda: 41,
    )

    assert number == 41
    assert len(attempts) == 1


def test_create_issue_retries_when_the_marker_still_finds_nothing(
    monkeypatch,
):
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    answers = iter([
        subprocess.CalledProcessError(
            1, ["gh", "issue", "create"], stderr="gh: Server Error (HTTP 502)",
        ),
        ISSUE_URL,
    ])

    def fake_run(command, **kwargs):
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    assert github.create_issue(
        "owner/repo", title="t", body="b", command_runner=fake_run,
        existing=lambda: None,
    ) == 41


def test_create_issue_keeps_a_url_printed_by_a_refused_attachment():
    """gh uploads attachments before the Issue exists and, when it
    refuses one, still creates the Issue and exits non-zero (#2364)."""

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output=ISSUE_URL + "\n",
            stderr="gh: failed to upload 1.png",
        )

    number = github.create_issue(
        "owner/repo", title="t", body="b", extra_args=("--attach", "./1.png"),
        cwd=Path("/staging"), command_runner=fake_run,
    )
    assert number == 41


def test_create_issue_rejects_a_failure_without_a_url():
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    with pytest.raises(subprocess.CalledProcessError):
        github.create_issue(
            "owner/repo", title="t", body="b",
            extra_args=("--attach", "./1.png"), command_runner=fake_run,
        )


def test_create_issue_rejects_an_answer_without_an_issue_number():
    with pytest.raises(ValueError, match="no issue number"):
        github.create_issue(
            "owner/repo", title="t", body="b",
            command_runner=lambda command, **kwargs: "not-a-url",
        )


def test_close_issue_is_one_retried_write_with_the_one_timeout(monkeypatch):
    attempts: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        attempts.append((list(command), dict(kwargs)))
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Server Error (HTTP 502)",
            )
        return ""

    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(seam, "run_command", fake_run)
    github.close_issue(7, repo="owner/repo")
    assert [command for command, _ in attempts] == [
        ["gh", "issue", "close", "7", "--repo", "owner/repo"],
        ["gh", "issue", "close", "7", "--repo", "owner/repo"],
    ]
    assert {kwargs["timeout"] for _, kwargs in attempts} == {
        github.ISSUE_TIMEOUT_SECONDS,
    }


def test_close_issue_with_a_comment_proves_the_close_before_retrying(
    monkeypatch,
):
    """A retried close-with-receipt must not post the receipt twice."""
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    answered: list[list[str]] = []

    def fake_run(command, **kwargs):
        answered.append(list(command))
        if _starts_with(command, ["gh", "issue", "close"]):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Server Error (HTTP 502)",
            )
        return json.dumps({"state": "CLOSED"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    github.close_issue(7, repo="owner/repo", comment="done")

    assert answered == [
        ["gh", "issue", "close", "7", "--repo", "owner/repo",
         "--comment", "done"],
        ["gh", "issue", "view", "7", "--repo", "owner/repo", "--json", "state"],
    ]


def test_close_issue_retries_when_the_close_did_not_land_yet(monkeypatch):
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    closes: list[list[str]] = []

    def fake_run(command, **kwargs):
        if _starts_with(command, ["gh", "issue", "close"]):
            closes.append(list(command))
            if len(closes) == 1:
                raise subprocess.CalledProcessError(
                    1, command, stderr="gh: Server Error (HTTP 502)",
                )
            return ""
        return json.dumps({"state": "OPEN"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    github.close_issue(7, repo="owner/repo", reason="not planned",
                       comment="done")
    assert closes == [
        ["gh", "issue", "close", "7", "--repo", "owner/repo",
         "--reason", "not planned", "--comment", "done"],
    ] * 2


def test_issue_number_from_url_reads_the_one_url_shape():
    assert github.issue_number_from_url(ISSUE_URL + "/") == 41


def test_list_pull_requests_is_one_query_with_the_one_timeout(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        seen.append((list(command), dict(kwargs)))
        return json.dumps([{"number": 3}])

    monkeypatch.setattr(seam, "run_command", fake_run)
    prs = github.list_pull_requests(
        "owner/repo", state="open", head="orbi/o-r-issue-3",
        json_fields="number", limit=20,
    )

    assert prs == [{"number": 3}]
    assert seen == [(
        [
            "gh", "pr", "list", "--state", "open",
            "--head", "orbi/o-r-issue-3", "--repo", "owner/repo",
            "--json", "number", "--limit", "20",
        ],
        {"timeout": github.RESUME_PR_STATE_TIMEOUT_SECONDS},
    )]


def test_list_pull_requests_reads_the_checkout_without_a_repo(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: seen.append(list(command)) or "[]",
    )
    assert github.list_pull_requests(
        None, state="merged", json_fields="number", limit=1,
    ) == []
    assert seen == [[
        "gh", "pr", "list", "--state", "merged", "--json", "number",
        "--limit", "1",
    ]]


def test_milestone_by_title_asks_only_the_requested_state(monkeypatch):
    """The state is asked of GitHub, so a CLOSED Milestone of the same
    title can never come back as a ready target (Issue #2394)."""
    queried: list[list[str]] = []

    def fake_run(command, **kwargs):
        queried.append(list(command))
        # GitHub's OWN filter: an `state=open` list never carries a closed
        # Milestone.
        if "state=open" in command[2]:
            return json.dumps([[{"title": "v1.2.3", "state": "open"}]])
        return json.dumps([[
            {"title": "v1.2.3", "state": "closed"},
        ]])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert github.milestone_by_title("owner/repo", "v1.2.3") == {
        "title": "v1.2.3", "state": "open",
    }
    assert github.milestone_by_title("owner/repo", "v1.2.4") is None
    assert github.milestone_by_title(
        "owner/repo", "v1.2.3", state="closed",
    ) == {"title": "v1.2.3", "state": "closed"}
    assert [command[2].split("?")[1] for command in queried] == [
        "state=open&per_page=100", "state=open&per_page=100",
        "state=closed&per_page=100",
    ]


