"""One rule for the Issue/PR/milestone calls, seen from the CALLERS (#2394).

The github module owns the three `gh` verbs the ticket names. These tests
take the callers' side of that contract:

1. no module outside `orbi.github` runs `gh issue create`, `gh issue
   close` or `gh pr list` (the structural half — a second implementation
   fails this test);
2. every closer gets the ONE retried close under the ONE bounded timeout;
3. every PR listing shares the one query, and a fork PR is never this
   repository's in the branch/orphan queries while `orbi suggest` keeps it
   as context;
4. a Milestone that is only present CLOSED is not a ready target for
   draft, suggest and `/orbi milestone` alike;
5. a 403 that merely SAYS "not found" is not a 404 anywhere, a status-only
   404 is missing for all, and a ref delete answered 422 is already gone.

Every fake sits at the ONE subprocess seam (`seam.run_command`) — the
callers' real argv and the adapter's real parsing are exercised.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from orbi import (
    branch_reclaim, claim, draft, gh_response, github, milestone,
    milestone_command, milestone_errors, repo_config, resume_verification,
    suggest,
)
from orbi.journal import LOGGER as JOURNAL_LOGGER
from seam import resume_deps, seam

SRC = Path(__file__).resolve().parent.parent / "src" / "orbi"
REPO = "owner/repo"
DELIVERY_BRANCH = "orbi/owner-repo-issue-4"

# The one argv each verb may have (the github module's own command line).
GH_VERB_PATTERNS = (
    r'"issue",\s*"create"',
    r'"issue",\s*"close"',
    r'"pr",\s*"list"',
)

MILESTONES_PATH = re.compile(r"repos/[^/]+/[^/]+/milestones[?]state=(\w+)")


def _starts_with(command: list[str], prefix: list[str]) -> bool:
    """True when `command` begins with `prefix`."""
    return command[: len(prefix)] == prefix


def _transient(status: str = "gh: Server Error (HTTP 502)"):
    return subprocess.CalledProcessError(1, ["gh"], stderr=status)


# --- 1. one implementation: the verbs live in the github module only ---------

def test_the_issue_and_pr_verbs_live_only_in_the_github_module():
    offenders = {}
    for path in sorted(SRC.glob("*.py")):
        if path.name == "github.py":
            continue
        source = path.read_text(encoding="utf-8")
        found = [
            pattern for pattern in GH_VERB_PATTERNS if re.search(pattern, source)
        ]
        if found:
            offenders[path.name] = found
    assert offenders == {}, (
        "these modules assemble a gh Issue/PR command instead of calling "
        f"orbi.github: {offenders}"
    )


# --- 2. every closer shares the ONE retried close ----------------------------

def test_the_external_triage_close_retries_under_the_one_timeout(monkeypatch):
    """The merged external-PR close is the same call: a transient 502 is
    retried with `github.ISSUE_TIMEOUT_SECONDS`."""
    attempts: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        if _starts_with(command, ["gh", "issue", "close"]):
            attempts.append((list(command), dict(kwargs)))
            if len(attempts) == 1:
                raise _transient()
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    resume_verification._close_external_triage_issue(
        7, REPO, "https://github.com/owner/repo/pull/9",
        "<!-- orbi:run=abc12345 -->", "abc12345", language_code="en",
    )

    assert [command for command, _ in attempts] == [
        ["gh", "issue", "close", "7", "--repo", REPO],
    ] * 2
    assert {kwargs["timeout"] for _, kwargs in attempts} == {
        github.ISSUE_TIMEOUT_SECONDS,
    }


def test_the_stale_notice_close_retries_under_the_one_timeout(monkeypatch):
    notice = {"number": 5, "body": "<!-- orbi-milestone-advance old=v1 -->"}
    attempts: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        if _starts_with(command, ["gh", "issue", "list"]):
            return json.dumps([notice])
        if _starts_with(command, ["gh", "issue", "close"]):
            attempts.append((list(command), dict(kwargs)))
            if len(attempts) == 1:
                raise _transient()
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(github.time, "sleep", lambda _delay: None)
    milestone._close_stale_milestone_issues(
        REPO, "v2", keep_release_notice=False, language_code="en",
    )

    assert [command[:5] for command, _ in attempts] == [
        ["gh", "issue", "close", "5", "--repo"],
    ] * 2
    assert {kwargs["timeout"] for _, kwargs in attempts} == {
        github.ISSUE_TIMEOUT_SECONDS,
    }


# --- 3. PR listing: one query, and a fork is never Orbi's -------------------

def _pr_list_fake(prs, calls=None):
    def fake_run(command, **kwargs):
        if calls is not None:
            calls.append(list(command))
        return json.dumps(prs)
    return fake_run


def test_the_branch_query_ignores_a_fork_pr_on_a_delivery_branch(
    monkeypatch, tmp_path,
):
    fork = {"number": 9, "url": "u9", "headRefName": DELIVERY_BRANCH,
            "isCrossRepository": True}
    own = {"number": 10, "url": "u10", "headRefName": DELIVERY_BRANCH,
           "isCrossRepository": False}
    monkeypatch.setattr(seam, "run_command", _pr_list_fake([fork, own]))

    assert github.open_pr_for_branch(tmp_path, DELIVERY_BRANCH) == own


def test_the_orphan_sweep_ignores_a_fork_pr_on_a_delivery_branch(monkeypatch):
    posted: list[tuple] = []
    hooks = resume_deps()
    hooks = claim.ResumeHooks(
        resume_scene=hooks.resume_scene,
        route_external_pr_ticket=hooks.route_external_pr_ticket,
        block_scene_failure=hooks.block_scene_failure,
        recover_missing_pr_scene=hooks.recover_missing_pr_scene,
        has_recoverable_pr_scene=hooks.has_recoverable_pr_scene,
        comment_pr=lambda number, *, repo, body: posted.append((number, body)),
    )
    monkeypatch.setattr(seam, "run_command", _pr_list_fake([
        {"number": 9, "headRefName": DELIVERY_BRANCH,
         "isCrossRepository": True},
    ]))

    assert claim.reconcile_orphan_prs(
        REPO, "abc12345", hooks=hooks, language_code="en",
    ) == []
    assert posted == []


def test_suggest_keeps_a_fork_pr_among_the_repositorys_open_prs(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if _starts_with(command, ["gh", "issue", "list"]):
            return "[]"
        return json.dumps([
            {"number": 9, "title": "from a fork", "body": "b",
             "isCrossRepository": True},
            {"number": 10, "title": "ours", "body": "b"},
        ])

    monkeypatch.setattr(seam, "run_command", fake_run)
    context = suggest._gather_context(REPO)

    assert [pr["number"] for pr in context["prs"]] == [9, 10]
    assert calls[-1][calls[-1].index("--json") + 1] == "number,title,body"


# --- 4. a CLOSED Milestone is not a ready target ----------------------------

def _milestone_fake(closed_only: bool = True, calls: list | None = None):
    """GitHub's own state filter: the OPEN list never carries a closed
    Milestone, the ALL list carries everything."""
    def fake_run(command, **kwargs):
        if calls is not None:
            calls.append(list(command))
        match = MILESTONES_PATH.search(command[2])
        if match is None:
            return "{}"
        state = match.group(1)
        milestones = [{"title": "v1.2.3", "state": "closed", "number": 1}]
        if not closed_only:
            milestones = [{"title": "v1.2.3", "state": "open", "number": 1}]
        if state == "open":
            milestones = [
                m for m in milestones if m["state"] == "open"
            ]
        return json.dumps([milestones])
    return fake_run


@pytest.mark.parametrize("caller", [
    lambda: draft._open_milestone(REPO, "v1.2.3"),
    lambda: suggest._open_milestone_arg(REPO, "v1.2.3"),
])
def test_draft_and_suggest_never_take_a_closed_milestone(monkeypatch, caller):
    calls: list[list[str]] = []
    monkeypatch.setattr(seam, "run_command", _milestone_fake(calls=calls))
    assert caller() is None
    assert [MILESTONES_PATH.search(c[2]).group(1) for c in calls] == ["open"]


def test_draft_and_suggest_take_the_open_milestone(monkeypatch):
    monkeypatch.setattr(
        seam, "run_command", _milestone_fake(closed_only=False),
    )
    assert draft._open_milestone(REPO, "v1.2.3") == "v1.2.3"
    assert suggest._open_milestone_arg(REPO, "v1.2.3") == "v1.2.3"


def test_the_milestone_command_creates_over_a_closed_milestone(monkeypatch):
    created: list[list[str]] = []
    base = _milestone_fake()

    def fake_run(command, **kwargs):
        if _starts_with(command, ["gh", "api", "--method", "POST"]):
            created.append(list(command))
            return "{}"
        return base(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    milestone_command._ensure_command_milestone(REPO, "v1.2.3")

    assert created == [[
        "gh", "api", "--method", "POST", f"repos/{REPO}/milestones",
        "-f", "title=v1.2.3",
    ]]


def test_the_milestone_command_reuses_an_open_milestone(monkeypatch):
    created: list[list[str]] = []

    def fake_run(command, **kwargs):
        created.append(list(command))
        return "{}"

    monkeypatch.setattr(
        seam, "run_command", _milestone_fake(closed_only=False, calls=created),
    )
    milestone_command._ensure_command_milestone(REPO, "v1.2.3")
    assert len(created) == 1


# --- 5. the one 404 rule ----------------------------------------------------

def _read_failure(code: int, message: str, *, status: int | None = None):
    def fail(command, **kwargs):
        output = "" if status is None else (
            f"HTTP/2.0 {status} Error\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
        )
        raise subprocess.CalledProcessError(
            code, command, output=output, stderr=message,
        )
    return fail


@pytest.mark.parametrize("failure", [
    _read_failure(1, "gh: Resource not found for this token (HTTP 403)"),
    _read_failure(1, "gh: Forbidden", status=403),
])
def test_a_403_that_says_not_found_is_not_a_missing_file(
    monkeypatch, caplog, failure,
):
    """The designed no-op is a REAL 404 only: a 403 whose sentence happens
    to say "not found" stays a reported read failure."""
    with caplog.at_level("ERROR", logger=JOURNAL_LOGGER.name):
        assert repo_config.read_repo_config(REPO, run_command=failure) is None
    assert "repo_config_read_failed" in caplog.text


@pytest.mark.parametrize("failure", [
    _read_failure(1, "gh: Not Found (HTTP 404)"),
    _read_failure(1, "", status=404),
    _read_failure(1, '{"message": "Not Found", "status": "404"}'),
])
def test_a_404_is_the_missing_file_whatever_carried_it(
    monkeypatch, caplog, failure,
):
    with caplog.at_level("ERROR", logger=JOURNAL_LOGGER.name):
        assert repo_config.read_repo_config(REPO, run_command=failure) is None
    assert "repo_config_read_failed" not in caplog.text


def test_a_403_that_says_not_found_is_not_a_missing_milestone():
    with pytest.raises(subprocess.CalledProcessError) as caught:
        milestone_errors.classify_milestone_error(
            subprocess.CalledProcessError(
                1, ["gh", "api"],
                stderr="gh: Resource not found for this token (HTTP 403)",
            ),
            "list_milestones",
        )
    assert not isinstance(caught.value, milestone_errors.MilestoneReconcileError)


def test_a_403_that_says_not_found_is_not_a_missing_login(monkeypatch):
    monkeypatch.setattr(
        seam, "run_command",
        _read_failure(1, "gh: Resource not found (HTTP 403)"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        github.repository_permission("someone", repo=REPO)


def test_a_ref_delete_answered_422_is_already_gone():
    """GitHub answers 422 ("Reference does not exist") to a ref that is
    already deleted — in the response status or in the message."""
    assert branch_reclaim._reference_missing(
        gh_response.GitHubRequestFailed(1, ["gh", "api"], status=422),
    ) is True
    assert branch_reclaim._reference_missing(
        gh_response.GitHubRequestFailed(1, ["gh", "api"], status=404),
    ) is True
    assert branch_reclaim._reference_missing(subprocess.CalledProcessError(
        1, ["gh", "api"], stderr="gh: Reference does not exist (HTTP 422)",
    )) is True
    assert branch_reclaim._reference_missing(subprocess.CalledProcessError(
        1, ["gh", "api"], stderr="gh: Resource not found (HTTP 403)",
    )) is False
