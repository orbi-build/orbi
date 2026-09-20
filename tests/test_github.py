"""Unit tests for the `orbi.github` leaf (Issue #785).

One test per GitHub read/write contract: the fakes sit at the single
`run_command` seam (injected `command_runner` or a patched module seam) and
assert the real `gh` command line — the argv IS the contract (Article 5.2).
No test patches `runner` internals.
"""
import json
import subprocess

import pytest

from orbi import github
from seam import seam


def test_module_is_a_leaf_and_never_imports_runner_release_pi_process():
    source = (github.__file__ and
              open(github.__file__, encoding="utf-8").read())
    for forbidden in ("from orbi.runner", "from orbi import runner",
                      "import orbi.runner", "from orbi.release",
                      "from orbi import release", "from orbi.pi_process",
                      "from orbi import pi_process"):
        assert forbidden not in source, forbidden


def test_run_gh_read_command_retries_transient_read_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(
                1, command,
                stderr="HTTP 429: Too Many Requests",
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
        "--paginate", "--slurp",
    ]]


def test_milestone_issues_keeps_the_release_scope_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '[[{"number": 2}]]')[1])

    assert github.milestone_issues("o/r", 5, "closed") == [{"number": 2}]
    assert captured == [[
        "gh", "api", "repos/o/r/issues?state=closed&milestone=5&per_page=100",
        "--paginate", "--slurp",
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
        "--paginate", "--slurp",
    ]]


def test_list_milestones_forwards_the_callers_timeout(monkeypatch):
    # Issue #95: the idle milestone-advance sweep bounds this network
    # read at 30 s — the bound must survive the seam (run_gh_read_command
    # forwards only the set options; None is run_command's default).
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(k), "[]")[1])

    github.list_milestones("o/r", timeout=30)
    assert captured == [{"timeout": 30}]
    github.list_milestones("o/r")
    assert captured[1] == {}


def test_milestone_open_issue_count_reads_githubs_own_counter(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "2")
    assert github.milestone_open_issue_count("o/r", "v1") == 2

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    with pytest.raises(RuntimeError, match="not found"):
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
        captured.append(c), '[{"name": "ci"}]')[1])

    checks = github.commit_check_runs("o/r", "abc")
    assert checks == [{"name": "ci"}]
    assert captured == [[
        "gh", "api", "repos/o/r/commits/abc/check-runs",
        "--jq", ".check_runs",
    ]]

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "{}")
    with pytest.raises(ValueError, match="array"):
        github.commit_check_runs("o/r", "abc")


def test_release_view_create_and_edit_notes(monkeypatch):
    captured = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), '{"tagName": "v1", "url": "u", "body": "b"}')[1])

    release = github.release_view("o/r", "v1", fields="tagName,url,body")
    assert release["url"] == "u"
    assert captured[-1] == [
        "gh", "release", "view", "v1", "--repo", "o/r",
        "--json", "tagName,url,body",
    ]

    github.release_edit_notes("o/r", "v1", notes="notes text")
    assert captured[-1] == [
        "gh", "release", "edit", "v1", "--repo", "o/r",
        "--notes", "notes text",
    ]

    github.release_create("o/r", tag="v1", version="v1", notes="notes text")
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
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        captured.append(c), "")[1])

    # ai-ready + claim -> ai-in-progress added; the ai-ready residue
    # is kept by contract (delivery_labels.label_patch).
    github.apply_label_patch(
        4, repo="o/r", event="claim",
        current_labels={"ai-ready"},
    )
    assert captured == [
        ["gh", "issue", "edit", "4", "--repo", "o/r",
         "--add-label", "ai-in-progress"],
    ]

    # A stale fix-needed residue is removed in the SAME edit call.
    github.apply_label_patch(
        4, repo="o/r", event="claim",
        current_labels={"ai-ready", "ai-fix-needed"},
    )
    assert captured[-1] == [
        "gh", "issue", "edit", "4", "--repo", "o/r",
        "--add-label", "ai-in-progress", "--remove-label", "ai-fix-needed",
    ]

    # A no-op patch (no add, no remove) applies nothing. No real event
    # produces one today — the guard is the contract, so the patch
    # computation is stubbed to the empty patch here.
    monkeypatch.setattr(github, "label_patch", lambda event, labels: ([], []))
    captured.clear()
    github.apply_label_patch(4, repo="o/r", event="claim", current_labels=set())
    assert captured == []


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
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        '{"comments": []}'))
    assert github.pr_comments(4, repo="o/r") == []


def test_trusted_issue_comments_block_keeps_newest_and_states_omissions(
        monkeypatch):
    # Pin the credential read: the NONE-association comment reaches the
    # authenticated-login fallback and CI runs unauthenticated.
    monkeypatch.setattr(seam, "_authenticated_github_login",
                        lambda: "ci-runner[bot]")
    comments = [
        {"author": {"login": "a"}, "authorAssociation": "OWNER",
         "createdAt": "t1", "body": "one"},
        {"author": {"login": "b"}, "authorAssociation": "NONE",
         "createdAt": "t2", "body": "injected"},
        {"author": {"login": "c"}, "authorAssociation": "MEMBER",
         "createdAt": "t3", "body": "two"},
    ]
    block = github.trusted_issue_comments_block(comments, limit=1)
    assert "1 older trusted comment omitted" in block
    assert "c (MEMBER)" in block
    assert "two" in block
    assert "injected" not in block
    assert "one" not in block

    assert github.trusted_issue_comments_block([], limit=3) == \
        "(no trusted comments)"


def test_comment_is_trusted_by_association_and_by_bot_login(monkeypatch):
    assert github._comment_is_trusted(
        {"authorAssociation": "MAINTAINER"}) is True
    assert github._comment_is_trusted("not-a-comment") is False

    status = "account orbi-bot\nActive account: true\n"

    def fake_status(command, **kwargs):
        return status

    monkeypatch.setattr(seam, "run_gh_read_command", fake_status)
    assert github._comment_is_trusted(
        {"author": {"login": "orbi-bot"}, "authorAssociation": "NONE"},
    ) is True
    assert github._comment_is_trusted(
        {"author": {"login": "someone-else"},
         "authorAssociation": "NONE"},
    ) is False


def test_merge_gate_preflight_reports_unreadable_protection(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
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
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": true}'
        if "protection" in command[-1]:
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
    assert any("collaborative" in line or "maintainer" in line for line in report)
    assert all("required_approving_review_count=0" not in line for line in report)
    assert all("--method DELETE" not in line for line in report)


def test_merge_gate_preflight_reports_ruleset_and_no_writes(monkeypatch):
    from orbi import github
    commands = []

    def fake(command, **kwargs):
        commands.append(command)
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": true}'
        if "protection" in command[-1]:
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
    assert any("UNKNOWN classic protection returned 404" in line
               for line in report)
    assert all("--method" not in command for command in commands)


def test_merge_gate_preflight_passes_when_no_protection(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
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
        assert "release/v1" not in command[-1]
        if (command[-1].endswith("branches/release%2Fv1")
                and "/rules/" not in command[-1]):
            return '{"protected": true}'
        if "protection" in command[-1]:
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
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": true}'
        return rules if "rules/branches/" in command[-1] else classic

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
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": true}'
        if failed_path in command[-1]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: Forbidden",
            )
        return "{}" if "protection" in command[-1] else "[]"

    monkeypatch.setattr(github, "run_command", fake)
    assert message in github.merge_gate_preflight("acme/project", "main")[0]


def test_merge_gate_preflight_rules_without_approvals(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": false}'
        return json.dumps([{"type": "required_status_checks"}])

    monkeypatch.setattr(github, "run_command", fake)
    assert github.merge_gate_preflight("acme/project", "main")[0].startswith(
        "merge_gate: PASS"
    )


def test_merge_gate_preflight_ruleset_without_id_names_settings(monkeypatch):
    from orbi import github

    def fake(command, **kwargs):
        if (command[-1].endswith("branches/main")
                and "/rules/" not in command[-1]):
            return '{"protected": false}'
        return json.dumps([{
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 1},
        }])

    monkeypatch.setattr(github, "run_command", fake)
    report = github.merge_gate_preflight("acme/project", "main")
    assert report[0].endswith("https://github.com/acme/project/settings/rules")


def test_authenticated_github_login_requires_an_active_account(monkeypatch):
    monkeypatch.setattr(seam, "run_gh_read_command",
        lambda c, **k: "account a\nActive account: true\n")
    assert github._authenticated_github_login() == "a"

    monkeypatch.setattr(seam, "run_gh_read_command",
        lambda c, **k: "account a\n")
    with pytest.raises(ValueError, match="active account"):
        github._authenticated_github_login()


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
    state, summaries = github.pr_delivery_status(
        "https://github.com/o/r/pull/9", "o/r")
    assert state == "OPEN"
    assert summaries == ["ci=COMPLETED/SUCCESS", "legacy=PENDING"]

    bad = json.dumps({"state": "WEIRD", "statusCheckRollup": None})
    monkeypatch.setattr(seam, "run_command", lambda c, **k: bad)
    with pytest.raises(ValueError, match="unexpected PR state"):
        github.pr_delivery_status("https://github.com/o/r/pull/9", "o/r")


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
    monkeypatch.setattr(github, "run_command", lambda c, **k: "[]")
    with pytest.raises(ValueError, match="release view must be a JSON object"):
        github.release_view("o/r", "v1", fields="tagName,url")


def test_authenticated_github_login_fails_fast_when_auth_status_fails(monkeypatch):
    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="no auth")

    monkeypatch.setattr(github, "run_gh_read_command", failing)
    with pytest.raises(ValueError, match="identity resolution failed"):
        github._authenticated_github_login()


def test_authenticated_github_login_requires_an_account_line(monkeypatch):
    # An Active-account marker without any account name resolves nothing.
    monkeypatch.setattr(
        github, "run_gh_read_command",
        lambda c, **k: "Active account: true\n")
    with pytest.raises(ValueError, match="active account"):
        github._authenticated_github_login()


# ------------------------------------------------- line run markers (Issue #825)

def test_line_run_markers_collects_every_trusted_marker():
    """Issue #825: the delivery line's marker set is EVERY run marker in
    the trusted comments — the creating run's marker stays accepted
    after a resume binds a new run id."""
    comments = [
        {
            "body": "<!-- orbi:run=0e4b1923 -->\nOrbi opened PR: x",
            "authorAssociation": "OWNER",
        },
        {
            "body": "<!-- orbi:run=be3c1434 -->\nOrbi started Pi",
            "authorAssociation": "MEMBER",
        },
    ]
    assert github.line_run_markers(comments) == frozenset({
        "<!-- orbi:run=0e4b1923 -->",
        "<!-- orbi:run=be3c1434 -->",
    })


def test_line_run_markers_ignores_public_comments_and_non_string_bodies():
    """Issue #825: a public comment must never widen the line's marker
    set (the copied-marker rule of the trusted filter); a non-string
    body carries nothing."""
    comments = [
        {
            "body": "<!-- orbi:run=deadbeef -->\nplease review PR 7",
            "authorAssociation": "NONE",
        },
        {"body": None, "authorAssociation": "OWNER"},
        {
            "body": "<!-- orbi:run=aaaaaaaa -->\nOrbi opened PR: x",
            "authorAssociation": "OWNER",
        },
    ]
    assert github.line_run_markers(comments) == frozenset({
        "<!-- orbi:run=aaaaaaaa -->",
    })


def test_line_run_markers_empty_without_trusted_markers():
    assert github.line_run_markers([]) == frozenset()
    assert github.line_run_markers([
        {"body": "plain text", "authorAssociation": "OWNER"},
    ]) == frozenset()


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
        ],
    ]
