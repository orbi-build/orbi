"""Reconciliation scene: epics, milestone state and orphan PRs (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: the per-tick reconciliation of open
Epics (single audit comment, bound language), the classified-failure
suppression state of milestone reconciliation, and the orphan-PR report for
a closed Issue.

`_gh_api` (the shared fake for the progress publisher's `gh api` traffic)
is owned by `tests/test_bootstrap_runner_outcome.py` and imported here.
"""

from orbi import config as config_domain
import json

import os
from pathlib import Path
import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.pi_session as pi_session
import orbi.milestone as milestone

import orbi.github as github
from tests.test_progress_wiring import make_fake_gh

from seam import resume_deps, seam
from tests.test_bootstrap_runner_outcome import _gh_api


def test_reconcile_open_epics_closes_and_deduplicates_audit(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return json.dumps([epic]) if command[:3] == ["gh", "issue", "list"] else ""
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete", lambda repo, item: ["Issue #21 closed"])
    comments = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue", lambda number, *, repo, body: comments.append({"body": body}))
    result = claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert result == ["Epic #20 closed after verification (Issue #21 closed)"]
    assert len(comments) == 1 and "run_id=abc12345" in comments[0]["body"]
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands
    # The same audit is idempotent on the next tick.
    commands.clear()
    claim.reconcile_open_epics("o/r", "def67890", "en")
    assert len(comments) == 1
    assert "epic_closed issue=20 repo=o/r" in caplog.text


def test_reconcile_open_epics_posts_the_audit_in_the_bound_language(monkeypatch):
    epic = {"number": 20}

    def fake_run(command, **kwargs):
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    comments = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    assert len(comments) == 1
    assert "Epic 对账：已完成" in comments[0]["body"]
    assert "Issue #21 closed" in comments[0]["body"]
    assert "run_id=abc12345" in comments[0]["body"]


def test_reconcile_open_epics_keeps_one_audit_across_languages(monkeypatch):
    """A close that failed after today's English audit landed posts no
    second comment when the next sweep renders Chinese; two Chinese
    sweeps after a failed close post one comment in total."""
    epic = {"number": 20}
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    english = ("Epic reconciliation: complete; children: Issue #21 closed; "
               "no open native blockers/dependencies.")
    comments = [{"body": f"<!-- orbi:run=old -->\n{english}\nrun_id=old"}]
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    claim.reconcile_open_epics("o/r", "def67890", "zh")
    assert len(comments) == 1
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands


def test_reconcile_open_epics_keeps_incomplete_without_comment(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([epic]))
    monkeypatch.setattr(seam, "verify_epic_complete", lambda repo, item: (_ for _ in ()).throw(ValueError("open blockers: #3")))
    comments = []
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: comments.append(True))
    claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert not comments
    assert "epic_kept_open issue=20 repo=o/r reason=\"open blockers: #3\"" in caplog.text


def test_milestone_reconcile_classified_failures_are_throttled_across_ticks(
    tmp_path, caplog,
):
    caplog.set_level("INFO")
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="Bad credentials (HTTP 401)",
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=1000)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)

    # A later systemd tick is a new Python process, so the filesystem marker —
    # not process memory — must prevent both another request and another log.
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r", now=1100)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    messages = [record.message for record in caplog.records]
    assert sum(message.endswith(
        'milestone_reconcile_auth_failed repo=o/r status=401 '
        'operation=list_milestones stderr="Bad credentials (HTTP 401)"'
    ) for message in messages) == 1
    assert "milestone_reconcile_failed" not in caplog.text


def test_milestone_reconcile_success_clears_suppression(tmp_path, caplog):
    error = github.MilestoneReconcileError(
        status=404, operation="list_milestones", stderr="Not Found (HTTP 404)",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert sum(record.message.endswith(
        'milestone_reconcile_not_found repo=o/r status=404 '
        'operation=list_milestones stderr="Not Found (HTTP 404)"'
    ) for record in caplog.records) == 2


def test_milestone_reconcile_failure_is_retried_after_cooldown(tmp_path):
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="HTTP 401",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    os.utime(marker, (1000, 1000))
    assert not milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS - 1,
    )
    assert milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS,
    )


def test_milestone_reconcile_state_io_failures_stay_fail_open(
    tmp_path, monkeypatch, caplog,
):
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    marker.parent.mkdir(parents=True)
    marker.touch()
    original_stat = Path.stat
    monkeypatch.setattr(
        Path, "stat",
        lambda self: (_ for _ in ()).throw(OSError("stat failed"))
        if self == marker else original_stat(self),
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    monkeypatch.setattr(Path, "stat", original_stat)

    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(OSError("unlink failed"))
        if self == marker else original_unlink(self),
    )
    os.utime(marker, (0, 0))
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=4000)
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert "milestone_reconcile_suppression_clear_failed repo=o/r" in caplog.text
    monkeypatch.setattr(Path, "unlink", original_unlink)

    marker.unlink()
    monkeypatch.setattr(
        os, "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )
    milestone.record_milestone_reconcile_failure(
        tmp_path, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    assert "milestone_reconcile_auth_failed repo=o/r status=401" in caplog.text


def test_reconcile_orphan_prs_reports_an_open_pr_of_a_closed_issue(monkeypatch, caplog):
    """Issue #746 (direction B): a delivery PR whose source Issue a human
    closed is reported ONCE on the PR — every delivery-state scan reads
    `state=open` Issues only, so nothing else would ever look at that PR
    again. The merge/close decision stays with the human: the Runner
    never auto-merges and never auto-closes across a human's close.

    Issue #1788 acceptance 2: `orbi/x-y-issue-3` is ANOTHER repository's
    delivery name, so the sweep never reads Issue #3 for it and never
    reports it — the fake raises on that read, so the old regex fails here
    even though this repository's #3 is closed."""
    caplog.set_level("INFO")
    pr_comments: list[dict] = []
    posted = []

    def fake_run(command, **kwargs):
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([
                {"number": 9, "headRefName": "orbi/o-r-issue-4"},
                {"number": 30, "headRefName": "orbi/x-y-issue-3"},
            ])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: pr_comments)
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda number, *, repo, body: posted.append((number, repo, body)),
    )
    result = claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    assert result == ["PR #9 reported: source Issue #4 is closed"]
    assert len(posted) == 1
    number, repo, body = posted[0]
    assert (number, repo) == (9, "o/r")
    assert "<!-- orbi:orphan-pr -->" in body
    assert "<!-- orbi:run=abc12345 -->" in body
    assert "#4" in body
    assert "orphan_pr_reported pr=9 issue=4 repo=o/r" in caplog.text
    # Issue #1826: the same optional fixed project status link rides on
    # this task message; omitting it (above call) stays GitHub-only.
    pr_comments.clear()
    claim.reconcile_orphan_prs(
        "o/r", "abc12345", hooks=resume_deps(), language_code="en",
        status_url="https://tasks.example.org/status?repo=o%2Fr",
    )
    assert len(posted) == 2
    assert (
        "**Project status:** [Open project status]"
        "(https://tasks.example.org/status?repo=o%2Fr)"
    ) in posted[1][2]
    posted.pop()
    pr_comments.clear()
    assert "pr=30" not in caplog.text
    # Idempotent: the marker on the PR means the next tick reports nothing.
    pr_comments.append({"body": posted[0][2]})
    assert claim.reconcile_orphan_prs("o/r", "def67890", hooks=resume_deps(), language_code="en") == []
    assert len(posted) == 1
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_orphan_prs_rejects_malformed_pr_list_and_state(monkeypatch):
    """A non-array `pr list` answer and a malformed Issue state fail fast
    inside the sweep — the per-tick try/except turns that into the
    `orphan_pr_reconcile_failed` bypass log, never a broken delivery."""
    def pr_list_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps({"comments": []})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", pr_list_run)
    with pytest.raises(ValueError, match="pr list must return a JSON array"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        pr_list_run(["unexpected"])

    def weird_state_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command[-1] == "state":
            return json.dumps({"state": "WEIRD"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", weird_state_run)
    with pytest.raises(ValueError, match="state must be OPEN or CLOSED"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        weird_state_run(["unexpected"])


def test_reconcile_orphan_prs_skips_open_issues_and_non_delivery_branches(monkeypatch):
    """An open source Issue is the normal delivery flow (no report); a
    head branch outside THIS repository's delivery naming (a human or
    external PR, another repository's `orbi/x-y-issue-3`) is never the
    Runner's business — no issue state is even read."""
    read_issues = []

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                "garbage",
                {"number": 9, "headRefName": "orbi/o-r-issue-4"},
                {"number": 10, "headRefName": "feature/human-branch"},
                {"number": 11, "headRefName": None},
                {"number": 12, "headRefName": "orbi/manual"},
            ])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            read_issues.append(4)
            return json.dumps({"state": "OPEN"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "comment_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no comment")))
    assert claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en") == []
    assert read_issues == [4]
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_the_orphan_branch_regex_is_gone():
    """Issue #1788 acceptance 3: the sweep no longer parses the Issue
    number with its own pattern — it reads the one delivery naming."""
    source = Path(claim.__file__).read_text(encoding="utf-8")
    assert "_ORPHAN_PR_BRANCH_RE" not in source
    assert "orbi/.+-issue-" not in source


def test_reconcile_orphan_prs_failure_is_fail_open(
    tmp_path, monkeypatch, caplog, bind_run_id,
):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a throttled milestone sweep must be skipped")
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    bind_run_id("abc12345")
    slot_dir = tmp_path / "slots"
    milestone.record_milestone_reconcile_failure(
        slot_dir.parent, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    result = claim.pick_next_delivery(["o/r"], slot_dir, 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "orphan_pr_reconcile_failed repo=o/r" in caplog.text


@pytest.mark.parametrize("policy_text,expected,unexpected", [
    ('message_language = "en"\n', "Orbi orphan PR:", "Orbi 孤儿 PR"),
    (None, "Orbi 孤儿 PR：", "Orbi orphan PR:"),
])
def test_claim_time_comment_follows_the_repository_language(
        monkeypatch, tmp_path, policy_text, expected, unexpected, bind_run_id,
):
    """Issue #1687 acceptance 2: the claim-time comments use the
    repository's merged language, not the host's. A zh host whose
    repository sets `message_language = "en"` posts English comments;
    with no repository file it keeps the host's Chinese."""
    import base64

    def fake_run(command, **kwargs):
        if command == ["gh", "api", "repos/o/r/contents/.github/orbi.toml"]:
            if policy_text is None:
                return json.dumps({})
            return json.dumps({
                "sha": "a" * 40,
                "content": base64.b64encode(
                    policy_text.encode("utf-8"),
                ).decode("ascii"),
            })
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r",
                       "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(f"unexpected command: {command}")

    posted: list[str] = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: [])
    monkeypatch.setattr(
        seam, "comment_pr",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setitem(
        claim.__dict__, "reconcile_open_epics", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.milestone_bookkeeping.__dict__, "milestone_reconcile_due",
        lambda *a, **k: False,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_resumable_delivery", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_in_progress_issue", lambda *a, **k: None,
    )
    monkeypatch.setitem(claim.__dict__, "pick_issue", lambda *a, **k: None)
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery", lambda *a, **k: None,
    )
    bind_run_id("abc12345")
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], tmp_path / "slots", 1, config=config, hooks=resume_deps(),
    ) is None
    assert len(posted) == 1
    assert expected in posted[0]
    assert unexpected not in posted[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "view", "9"])


def test_reconcile_open_epics_failure_is_fail_open(
    tmp_path, monkeypatch, caplog, bind_run_id,
):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            github.MilestoneReconcileError(
                status=404, operation="list_milestones", stderr="HTTP 404",
            )
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    bind_run_id("abc12345")
    result = claim.pick_next_delivery(["o/r"], tmp_path / "slots", 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "epic_reconcile_failed repo=o/r" in caplog.text
    assert "milestone_reconcile_not_found repo=o/r status=404" in caplog.text


def test_reconcile_open_epics_runs_on_a_fresh_tick(
    monkeypatch, tmp_path, bind_run_id,
):
    calls = []
    bind_run_id(None)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        claim, "reconcile_open_epics",
        lambda repo, run_id, language_code: calls.append(
            (repo, run_id, language_code)),
    )
    monkeypatch.setattr(milestone, "reconcile_release_milestones", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_issue", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery", lambda *a, **k: None,
    )
    assert claim.pick_next_delivery(["o/r"], Path("/tmp/slots"), 1, hooks=resume_deps()) is None
    assert calls == [("o/r", "a1b2c3d4", "en")]
    # A repository's zh message language reaches the Epic sweep (Issue #1705).
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], Path("/tmp/slots"), 1, config=config, hooks=resume_deps(),
    ) is None
    assert calls[-1] == ("o/r", "a1b2c3d4", "zh")


def test_process_issue_closed_issue_ends_without_pr_ceremony(
    monkeypatch, tmp_path, caplog,
):
    """Issue #746: `deliver_pr` returns None for an Issue closed during
    delivery — no `ai-pr-opened` label patch, no `Orbi opened PR:` scene
    comment; the run ends `issue_closed` and the result kind ends the
    tick before the delivery wait."""
    monkeypatch.setattr(seam, "freeze_base",
                        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: None)
    edits: list[tuple] = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    comments = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append(body),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #266: the health record is a pure bypass — a failed record
    # never changes the issue-closed outcome (the except branch runs).
    def dead_health(*args, **kwargs):
        raise RuntimeError("health state unwritable")

    monkeypatch.setattr(runner.runner_health, "record_run_attempt", dead_health)
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    assert result == ("issue-closed", None)
    assert "health_success_record_failed" in caplog.text
    assert not any(
        "ai-pr-opened" in str(args) + str(kwargs) for args, kwargs in edits
    ), "no ai-pr-opened label patch for a closed Issue"
    assert not any("Orbi opened PR" in body for body in comments)
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "result=issue_closed" in ends[0]
    assert "pr=-" in ends[0]
