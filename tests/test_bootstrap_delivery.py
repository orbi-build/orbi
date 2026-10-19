"""Delivery and claim domain (Issue #1648).

Fourth domain split out of `test_bootstrap_runner.py` (after
`test_runner_config.py` Issue #789 and the release, Pi session and
configuration/preflight suites): the claim scans and the delivery they
prepare, moved here verbatim with no behavior change.

- Claim: the P0/bug/plain ready scans and their exclusions, the
  `blockedBy` guard, the active-milestone scope, the release fallback and
  external-takeover claim scans, the milestone-completeness gate, the
  in-flight and resumable selection order, the claim route, and the
  delivery scene a claim creates (frozen base, task worktree, run state,
  resume context).
- Delivery: `verify_pr` and the external-takeover PR it resolves,
  `deliver_pr` and the worktree closeout, the delivery-step label state
  machine, the review round and the finish-progress comment.

The scaffolding lives in `tests/fakes/bootstrap_delivery.py`. Cross-domain
runner ticks (`process_issue`, `main`, milestone bookkeeping,
reconciliation, failure reporting, the GitHub adapter and the tracker
comments) stay in `test_bootstrap_runner.py`.
"""
from orbi import config as config_domain
from orbi import language

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.review_merge as review_merge
import orbi.run_state as run_state
import orbi.gitops as gitops
import orbi.pi_session as pi_session
import orbi.milestone as milestone
from orbi import progress
from tests.fakes.gitops import remote_head_answer
import orbi.journal as journal
from seam import resume_deps, seam
from orbi.delivery_labels import EVENT_BLOCKED
from orbi.delivery_scene import RunContext

from tests.fakes.bootstrap_delivery import (
    BLOCKED_REVIEW_TEXT,
    DELIVER_BRANCH,
    FAKE_BASE_TREE_SHA,
    FAKE_HEAD_SHA,
    FAKE_PR_REPO,
    FAKE_PR_URL,
    FAKE_RUN_ID,
    PR_URL,
    RELEASE_663,
    _blocked_line,
    _blocked_review_body,
    _fake_takeover_view,
    _make_linked_worktree,
    _probe_lock_free,
    _release_scan_fake,
    _review_round_env,
    _seed_deliver_run_state,
    fake_deliver_run,
    fake_pr_view,
    fake_verify_pr_payload,
    fake_verify_run,
    make_local_remote_pair,
)


def test_pick_issue_uses_github_queue(monkeypatch):
    issue = {
        "number": 9, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi-backlog") == issue
    # Issue #101: the P0 scan runs first, then the bug scan (Issue
    # #71); with nothing in either queue the plain ready scan decides.
    # All three keep the same exclusions.
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready label:p0 -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready label:bug -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
            "-label:ai-fix-needed -label:ai-merged -label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
    ]


def test_open_blocker_numbers_returns_only_open_blockers():
    # GitHub keeps a relation listed after its blocker closes (the
    # node carries `state: "CLOSED"` and is inert — verified against
    # the live API, Issue #54): only OPEN blockers block.
    issue = {"blockedBy": {"nodes": [
        {"number": 31, "state": "OPEN", "title": "a"},
        {"number": 32, "state": "CLOSED", "title": "b"},
    ], "totalCount": 2}}
    assert runner.open_blocker_numbers(issue) == [31]


def test_open_blocker_numbers_counts_missing_state_as_open():
    # A node without an explicit state counts as open: claiming a
    # possibly-blocked Issue costs a full run, waiting one tick does
    # not (Issue #54).
    issue = {"blockedBy": {"nodes": [{"number": 31}], "totalCount": 1}}
    assert runner.open_blocker_numbers(issue) == [31]


def test_open_blocker_numbers_fails_open_on_missing_or_malformed_field():
    # A missing or malformed `blockedBy` field must never block the
    # queue (Issue #54 fail open): no known blockers, not an error.
    assert runner.open_blocker_numbers({"number": 9}) == []
    assert runner.open_blocker_numbers({"blockedBy": "nope"}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": "nope"}}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": ["nope"]}}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": [
        {"number": 31, "state": "OPEN"},
        {"title": "no number", "state": "OPEN"},
        {"number": "32", "state": "OPEN"},
        {"number": True, "state": "OPEN"},
    ]}}) == [31]


def test_pick_issue_skips_blocked_issue_and_claims_next(monkeypatch, caplog):
    """A ready Issue with open native blockers (Issue #54) is never
    claimed: no label change, no worktree — the runner logs a
    structured `blocked_by` line with the blocker list and moves on to
    the next ready Issue of the same repo."""
    blocked = {
        "number": 54, "title": "blocked task", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "title": "base"},
            {"number": 32, "title": "retry"},
        ], "totalCount": 2},
    }
    ready = {
        "number": 55, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == ready
    assert "blocked_by" in caplog.text
    assert "54" in caplog.text
    assert "31" in caplog.text
    assert "32" in caplog.text


def test_pick_issue_returns_none_when_all_ready_issues_blocked(
    monkeypatch, caplog,
):
    blocked_a = {
        "number": 1, "title": "a", "body": "",
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    blocked_b = {
        "number": 2, "title": "b", "body": "",
        "blockedBy": {"nodes": [{"number": 8}], "totalCount": 1},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked_a, blocked_b]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert caplog.text.count("blocked_by") >= 2


def test_pick_issue_claims_issue_whose_blocker_is_closed(monkeypatch):
    """A closed blocker no longer blocks (Issue #54): GitHub keeps the
    relation listed with `state: "CLOSED"` (verified against the live
    API), and the runner counts only open blockers — the next tick
    claims the Issue without any runner-side bookkeeping."""
    issue = {
        "number": 54, "title": "unblocked task", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "state": "CLOSED", "title": "done"},
        ], "totalCount": 1},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_claims_issue_with_empty_blocked_by(monkeypatch):
    issue = {
        "number": 54, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_stays_blocked_while_any_blocker_is_open(monkeypatch):
    blocked = {
        "number": 54, "title": "mixed", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "state": "CLOSED", "title": "done"},
            {"number": 32, "state": "OPEN", "title": "pending"},
        ], "totalCount": 2},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked]),
    )
    assert claim.pick_issue("xqliu/orbi") is None


def test_pick_issue_fails_open_when_blocked_by_field_missing(monkeypatch):
    # Older gh versions or a changed API shape omit the field: the
    # Issue must still be claimable (fail open, Issue #54).
    issue = {"number": 9, "title": "task", "body": "body"}
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_fails_open_when_blocked_by_query_fails(
    monkeypatch, caplog,
):
    """A failed blockedBy query must not deadlock the queue (Issue
    #54 fail open): the tick claims nothing this round and the error
    is logged, never raised — the next tick retries the query."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="boom", stderr="rate limited",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    # Issue #738: the read now retries the transient stderr; skip the
    # real backoff sleeps like the git/gh retry tests do.
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_reraises_a_failed_scan_for_the_observation(
    monkeypatch, caplog,
):
    """`orbi status` reads this same scan (Issue #1773), and an
    observation must not turn an unreadable queue into an empty one
    (Issue #1668): with `raise_on_read_failure` the failed read is
    named, while the claim keeps failing open above."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="", stderr="error: HTTP 422: Validation Failed",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"), pytest.raises(subprocess.CalledProcessError):
        claim.pick_issue("xqliu/orbi", raise_on_read_failure=True)
    assert "blocked_by_check_failed" in caplog.text


def test_pick_ready_issue_reraises_the_release_scan_failure(
    monkeypatch,
):
    """The flag reaches every scan, not only the three ordinary ones: a
    failed release-fallback read is named too instead of ending the
    observation as an empty queue."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if any("label:ai-release" in arg for arg in command):
            raise subprocess.CalledProcessError(
                1, ["gh"], output="", stderr="error: HTTP 422: Validation",
            )
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        claim.pick_ready_issue("xqliu/orbi", raise_on_read_failure=True)
    assert any(
        "label:ai-release" in arg for command in calls for arg in command
    )


def test_validate_active_milestone_none_is_unchanged(monkeypatch):
    monkeypatch.setitem(runner.__dict__, "list_milestones", lambda *args, **kwargs: pytest.fail("no lookup"))
    milestone.validate_active_milestone("owner/repo", None)


def test_validate_active_milestone_missing_fails_with_repair_facts(monkeypatch, caplog):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "open"},
    ])
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="state=absent"):
        milestone.validate_active_milestone("owner/repo", "v9")
    assert "active_milestone_missing" in caplog.text
    assert "open_milestones=v1" in caplog.text


@pytest.mark.parametrize("ready_issues", [[], [{"number": 7}]])
def test_validate_active_milestone_closed_is_informational(
    monkeypatch, caplog, ready_issues,
):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "closed"},
    ])
    seen = {}

    def fake_list_issues(repo, **kwargs):
        seen["repo"] = repo
        seen.update(kwargs)
        return ready_issues

    monkeypatch.setitem(milestone.__dict__, "list_issues", fake_list_issues)
    with caplog.at_level(logging.INFO):
        milestone.validate_active_milestone("owner/repo", "v1")
    assert "active_milestone_closed" in caplog.text
    assert f"ai_ready_count={len(ready_issues)}" in caplog.text
    assert seen == {
        "repo": "owner/repo", "state": "open", "label": "ai-ready",
        "milestone": "v1", "json_fields": "number", "limit": 1000,
        "timeout": 30,
    }


def test_validate_active_milestone_open_is_valid(monkeypatch):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "open"},
    ])
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: pytest.fail("no ready scan"))
    milestone.validate_active_milestone("owner/repo", "v1")


def test_pick_issue_scopes_all_three_ready_scans_to_active_milestone(
    monkeypatch,
):
    """Issue #139: with `active_milestone = "v0.2.0"` the ready scans
    only see the active Milestone: every one of the three gh searches
    (p0, bug, plain) carries the `milestone:"v0.2.0"` qualifier (the
    quoted form is the contract — milestone titles may contain spaces
    or special characters; verified against the live API). A
    `v0.1.2 + ai-ready` Issue therefore never enters the queue of a
    `v0.2.0` Runner, and a `v0.2.0` Issue without `ai-ready` never
    enters it either (the `label:ai-ready` qualifier stays)."""
    issue = {
        "number": 139, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue(
        "xqliu/orbi-backlog", active_milestone="v0.2.0",
    ) == issue
    scope = ' milestone:"v0.2.0"'
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready label:p0{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready label:bug{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
    ]


def test_pick_issue_p0_scan_keeps_the_milestone_scope():
    """Issue #139 (explicit decision): P0 does NOT cross milestones.
    The active milestone is the claim scope of every fresh claim —
    `p0` only orders the pickup inside the active milestone, so a P0
    sitting in an old milestone never enters the current version's
    queue (one uniform rule, no special case). The scan order itself
    is unchanged: the p0 scan is still the FIRST one."""
    p0_search, bug_search, plain_search = claim.ready_searches("v0.2.0")
    for search in (p0_search, bug_search, plain_search):
        assert 'milestone:"v0.2.0"' in search
    assert "label:p0" in p0_search
    assert "label:p0" not in bug_search
    assert "label:p0" not in plain_search


def test_ready_searches_without_milestone_are_the_compat_scans():
    """Issue #139 compat: without a configured Milestone the three
    ready scans are byte-identical to the pre-#139 scans — a config
    without `active_milestone` behaves exactly like before."""
    p0_search, bug_search, plain_search = claim.ready_searches(None)
    assert p0_search == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert bug_search == (
        "label:ai-ready label:bug -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert plain_search == (
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked"
    )
    # The default (no argument) is the same compat behavior.
    assert claim.ready_searches() == claim.ready_searches(None)


def test_pick_issue_keeps_epic_and_blocked_by_guards_with_milestone(
    monkeypatch, caplog,
):
    """Issue #139: the milestone scope never weakens the existing
    guards — an `ai-epic` Issue is still skipped by the code layer
    (the query results can still contain it), and an Issue with open
    native blockers is still skipped, inside the active Milestone."""
    epic = {
        "number": 141, "title": "epic", "body": "body",
        "labels": [{"name": "ai-ready"}, {"name": "ai-epic"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    blocked = {
        "number": 142, "title": "blocked", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    ready = {
        "number": 143, "title": "free", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic, blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.2.0",
        ) == ready
    assert "epic_not_claimed" in caplog.text
    assert "blocked_by" in caplog.text


def test_pick_issue_fails_open_when_milestone_query_fails(
    monkeypatch, caplog,
):
    """Issue #139: a failed ready scan with the milestone qualifier
    follows the existing fail-open contract (Issue #54) — the tick
    claims nothing (never the wrong version), logs the structured
    error, and the next tick retries."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="boom", stderr="rate limited",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    # Issue #738: the read now retries the transient stderr; skip the
    # real backoff sleeps like the git/gh retry tests do.
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.2.0",
        ) is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_prefers_bug_labeled_issues(monkeypatch):
    """Issue #71: when an `ai-ready`+`bug` Issue and a plain `ai-ready`
    Issue exist at the same time, the runner claims the bug first —
    if the delivery loop is broken, claiming enhancements only piles
    up unreviewed PRs. No priority numbers, no new state: the bug
    scan runs (after the P0 scan, Issue #101) with the same
    exclusions, and the plain ready scan only runs when the bug scan
    found nothing claimable."""
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:bug" in searches[-1]:
            return json.dumps([bug])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == bug
    # The P0 scan ran first and found nothing; the bug scan found the
    # bug: the plain ready scan never ran.
    assert len(searches) == 2
    assert "label:p0" in searches[0]
    assert "label:bug" in searches[1]
    assert "label:ai-ready" in searches[1]


def test_pick_issue_bug_scan_keeps_existing_exclusions(monkeypatch):
    """Issue #71: the bug scan keeps the exact same exclusions as the
    ready scan (in-flight, opened-PR, fix-needed, merged, blocked),
    and runs after the P0 scan (Issue #101)."""
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:bug" in searches[-1]:
            return json.dumps([bug])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == bug
    assert searches[0] == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert searches[1] == (
        "label:ai-ready label:bug -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )


def test_pick_issue_falls_back_to_ready_scan_when_no_bug(monkeypatch):
    """Issue #71: with no claimable P0 or bug, behavior is exactly as
    before (the plain ready scan runs last and decides)."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1] or "label:bug" in searches[-1]:
            return json.dumps([])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3
    assert "label:p0" in searches[0]
    assert "label:bug" in searches[1]
    assert "label:bug" not in searches[2]
    assert "label:p0" not in searches[2]


def test_pick_issue_bug_blocked_by_open_blocker_falls_back(monkeypatch):
    """Issue #71: a bug with open native blockers is skipped by the bug
    scan (same blockedBy semantics as the ready scan); the plain ready
    scan then decides."""
    blocked_bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1]:
            return json.dumps([])
        if "label:bug" in searches[-1]:
            return json.dumps([blocked_bug])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3


def test_pick_issue_bug_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #71: a failed bug scan fails open like the ready scan
    (Issue #54) — the tick claims nothing from this repo, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


# --- Issue #101: P0 priority ---------------------------------------------------


def test_pick_issue_prefers_p0_over_bug_and_plain(monkeypatch, caplog):
    """Issue #101: when an `ai-ready`+`p0` Issue, an `ai-ready`+`bug`
    Issue and a plain `ai-ready` Issue exist at the same time, the
    runner claims the P0 first — the P0 scan runs first with the exact
    same exclusions, and the bug/plain scans only run when the P0 scan
    found nothing claimable. The journal line carries `priority=p0`."""
    p0 = {
        "number": 7, "title": "p0 outage", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # All three Issues exist in the repo; the P0 scan (the only
        # scan that runs) sees them all and must pick the P0.
        return json.dumps([p0, bug, feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == p0
    # The P0 scan ran first and found the P0: the bug and plain ready
    # scans never ran.
    assert len(searches) == 1
    assert "label:p0" in searches[0]
    assert "label:ai-ready" in searches[0]
    # The pickup log carries the explicit priority field.
    assert "priority=p0" in caplog.text
    assert "issue=7" in caplog.text


def test_pick_issue_p0_scan_keeps_existing_exclusions(monkeypatch):
    """Issue #101: the P0 scan keeps the exact same exclusions as the
    bug and ready scans (in-flight, opened-PR, fix-needed, merged,
    blocked) — a P0 in any delivery state is never claimed by the
    ready scan (single-slot and resume semantics are unchanged)."""
    p0 = {
        "number": 7, "title": "p0", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # Only the P0 scan runs (it finds the P0): it must carry the
        # exact exclusions.
        return json.dumps([p0])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == p0
    assert searches[0] == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )


# --- Issue #255: release fallback scan ------------------------------------


def test_release_fallback_search_query():
    """Issue #255: the release fallback scan keeps `label:ai-ready`
    (the human execution switch) plus `label:ai-release` and the same
    five delivery-state exclusions as the ordinary scans. Without a
    configured Milestone it is the plain query; with one it carries the
    same quoted `milestone:"<title>"` scope."""
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    assert claim.release_fallback_search(None) == (
        f"label:ai-ready label:ai-release {exclusions}"
    )
    assert claim.release_fallback_search() == (
        f"label:ai-ready label:ai-release {exclusions}"
    )
    assert claim.release_fallback_search("v0.3.0") == (
        f'label:ai-ready label:ai-release milestone:"v0.3.0" {exclusions}'
    )


def test_release_issue_not_claimed_when_ordinary_delivery_exists(
    monkeypatch, caplog,
):
    """Issue #255: with an ordinary delivery ready (here in the plain
    scan), the release Issue is skipped there (`release_not_claimed`) —
    the ordinary delivery is claimed first and the release fallback
    scan never runs (only three gh searches)."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1] or "label:bug" in searches[-1]:
            return json.dumps([])
        # The plain scan sees both (release listed first); the release
        # is skipped and the ordinary delivery wins. The release
        # fallback scan never runs (only three searches).
        return json.dumps([release, feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3
    assert "release_not_claimed" in caplog.text
    assert "issue=254" in caplog.text


def test_release_issue_claimed_when_no_ordinary_delivery(
    monkeypatch, caplog,
):
    """Issue #255: when all three ordinary ready scans are empty, the
    release fallback scan runs and claims the release Issue — it then
    flows into the release state machine downstream."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:ai-release" in searches[-1]:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == release
    # Three ordinary scans + one release fallback scan.
    assert len(searches) == 4
    assert "label:ai-release" in searches[3]
    assert "label:ai-ready" in searches[3]
    assert "picked issue=254" in caplog.text


def test_release_fallback_scoped_to_active_milestone(monkeypatch):
    """Issue #255: with a configured `active_milestone` the release
    fallback scan carries the same `milestone:"<title>"` scope as the
    ordinary scans — a release of another Milestone is never claimed."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        # Issue #663: with `active_milestone` the release candidate now
        # passes the Milestone completeness gate; this Milestone has only
        # the release ticket itself.
        if command[1] == "api":
            return "1"
        searches.append(command[command.index("--search") + 1])
        if "label:ai-release" in searches[-1]:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue(
        "xqliu/orbi", active_milestone="v0.3.0",
    ) == release
    assert 'milestone:"v0.3.0"' in searches[3]
    assert "label:ai-release" in searches[3]


def test_release_fallback_excludes_epic_and_blocked(
    monkeypatch, caplog,
):
    """Issue #255: the release fallback scan keeps the Epic and
    blockedBy guards — an `ai-epic` release Issue is skipped with
    `epic_not_claimed`, and a release Issue with open native blockers
    is skipped with `blocked_by` (nothing is claimed)."""
    epic_release = {
        "number": 260, "title": "epic release", "body": "",
        "labels": [
            {"name": "ai-ready"}, {"name": "ai-release"},
            {"name": "ai-epic"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    blocked_release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([epic_release, blocked_release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "epic_not_claimed" in caplog.text
    assert "blocked_by" in caplog.text


def test_release_fallback_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #255: a failed release fallback query fails open like any
    other scan (Issue #54) — the tick claims nothing, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            raise error
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


# --- Issue #842 (D1): external takeover scan -------------------------------


def test_external_takeover_search_query():
    """Issue #842 (D1): the fifth scan is the repository's FIRST scan
    WITHOUT the `milestone:"<title>"` qualifier — an external
    contribution is not part of any version's delivery scope, and
    writing it into the active milestone would let an external PR block
    the release gate (`release_milestone_incomplete`). The query keeps
    `label:ai-ready` (the human execution switch), the same five
    delivery-state exclusions, and a body-text term for the triage
    marker. The text term is a candidate PREFILTER only (verified
    against the live search: GitHub tokenizes the marker loosely) —
    the exact marker check is the code-layer guard in
    `_pick_from_scan`."""
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    query = claim.external_takeover_search()
    assert query == f'label:ai-ready "orbi:external-pr" in:body {exclusions}'
    assert "milestone:" not in query
    # The default (no argument) is the same query; a repository's
    # custom dispatch label keys the scan the same way.
    assert claim.external_takeover_search() \
        == claim.external_takeover_search(None)
    assert claim.external_takeover_search("go") == (
        f'label:go "orbi:external-pr" in:body {exclusions}'
    )


def test_pick_issue_claims_milestone_less_external_ticket(
    monkeypatch, caplog,
):
    """Issue #842 (D1), the root-cause scene: `active_milestone`
    configured, the external triage ticket requeued to `ai-ready` with
    NO milestone (an external contributor cannot set one — and the
    runner must never write one). The three ordinary scans are
    milestone-scoped and see nothing, the release fallback sees
    nothing, and the FIFTH scan (no milestone qualifier) claims the
    ticket so the claim's takeover probe can review the external PR."""
    ticket = {
        "number": 842, "title": "external contribution", "body": (
            "please review\n\n<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        searches.append(search)
        if "orbi:external-pr" in search:
            return json.dumps([ticket])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) == ticket
    # p0, bug, plain, release fallback — all scoped; the fifth scan is
    # not.
    assert len(searches) == 5
    for scoped in searches[:4]:
        assert 'milestone:"v0.5.4"' in scoped
    assert "milestone:" not in searches[4]
    assert "orbi:external-pr" in searches[4]
    assert "picked issue=842" in caplog.text


def test_pick_issue_external_scan_never_claims_a_plain_ticket(
    monkeypatch, caplog,
):
    """Issue #842: the fifth scan is milestone-free, so the code layer
    must keep the claim scope: a loose body-text prefilter match
    WITHOUT the exact `<!-- orbi:external-pr:N -->` marker is skipped
    with `claim_yield reason=no_external_marker` — never claimed. A
    plain milestone-less Issue must never enter the queue of a runner
    with a configured `active_milestone`."""
    plain = {
        "number": 10, "title": "task",
        "body": "talking about an external PR review",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "orbi:external-pr" in search:
            return json.dumps([plain])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) is None
    assert "claim_yield" in caplog.text
    assert "no_external_marker" in caplog.text


def test_pick_issue_external_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #842: a failed fifth-scan query fails open like any other
    scan (Issue #54) — the tick claims nothing, the structured error is
    logged, never raised, and the next tick retries."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "orbi:external-pr" in search:
            raise error
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_without_milestone_claims_external_ticket_first(
    monkeypatch,
):
    """Issue #842 regression: without a configured `active_milestone`
    the plain ready scan is already milestone-free and claims the
    external ticket like any other — the fifth scan is not even
    reached, so repositories without `active_milestone` behave exactly
    as before."""
    ticket = {
        "number": 842, "title": "external contribution", "body": (
            "<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        searches.append(search)
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([ticket])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == ticket
    assert len(searches) == 3
    assert all("orbi:external-pr" not in search for search in searches)


def test_pick_issue_never_scopes_the_takeover_scan_to_a_milestone():
    """Issue #842, the release-gate reverse assertion: the takeover scan
    must not carry ANY `milestone:` qualifier (with or without a
    configured active milestone) — an external ticket entering the
    active milestone's queue would let an external contributor block
    `release_milestone_incomplete` forever."""
    for search in (
        claim.external_takeover_search(None),
        claim.external_takeover_search("go"),
    ):
        assert "milestone:" not in search


def test_gather_claim_facts_routes_marker_ticket_to_takeover(
    monkeypatch, tmp_path, caplog,
):
    """Issue #842, the user path's middle link: the milestone-less
    external triage ticket the fifth scan picked walks the SAME claim
    machinery — the takeover probe resolves the open external PR (the
    structured `external_takeover` event) and the scene classifies
    EXTERNAL_TAKEOVER, whose handler reviews the contributor's head
    (no run_pi, and downstream never an auto-merge, decision D2)."""
    issue = {
        "number": 842, "title": "external contribution", "body": (
            "please review\n\n<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "has_in_progress_label",
                        lambda *a, **k: False)
    monkeypatch.setattr(seam, "open_pr_for_branch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "stable_branch_exists",
                        lambda *a, **k: False)
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN",
        "url": "https://github.com/xqliu/orbi/pull/838",
        "baseRefName": "main",
        "headRefName": "contributor-patch",
        "headRefOid": "abc123def456abc123def456abc123def456abcd",
    })
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    with caplog.at_level("INFO"):
        facts = runner._gather_claim_facts(
            issue, config, "xqliu/orbi", None,
        )
    assert facts.external_takeover is True
    assert facts.takeover_pr["headRefName"] == "contributor-patch"
    assert facts.pr_state == "OPEN"
    assert facts.classify_scene() is runner.DeliveryScene.EXTERNAL_TAKEOVER
    assert "external_takeover" in caplog.text


# --- Issue #663: release Milestone completeness gate ----------------------


def test_milestone_open_issue_count_reads_github_counter(monkeypatch):
    """Issue #663: the completeness judgment is GitHub's own Milestone
    `open_issues` counter, read with the one API call documented in the
    Issue (title-filtered jq) — not the search index, not a label."""
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return "3"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.milestone_open_issue_count("xqliu/orbi", "v0.4.2") == 3
    assert commands == [[
        "gh", "api", "repos/xqliu/orbi/milestones",
        "--jq", '.[] | select(.title=="v0.4.2") | .open_issues',
        "--include",
    ]]


def test_milestone_open_issue_count_missing_milestone_raises(monkeypatch):
    """Issue #663: an empty result (the Milestone is gone) is a failed
    check, not a silent zero — the caller must never claim on it."""
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "")
    with pytest.raises(RuntimeError, match="v0.4.2"):
        runner.milestone_open_issue_count("xqliu/orbi", "v0.4.2")


def test_release_target_milestone_prefers_issue_then_active():
    """Issue #663: the release's Milestone is the Issue's GitHub
    Milestone, falling back to the configured `active_milestone` (the
    fallback scan is scoped to it). No Milestone ⇒ no gate."""
    assert runner.release_target_milestone(
        {"milestone": {"title": "v0.4.2"}}, None,
    ) == "v0.4.2"
    assert runner.release_target_milestone({}, "v0.4.2") == "v0.4.2"
    assert runner.release_target_milestone({}, None) is None
    assert runner.release_target_milestone(
        {"milestone": {"title": ""}}, "v0.4.2",
    ) == "v0.4.2"
    assert runner.release_target_milestone({"milestone": "bad"}, None) is None
    assert runner.release_target_milestone(
        {"milestone": {"title": 7}}, None,
    ) is None


def test_release_not_claimed_when_milestone_incomplete(monkeypatch, caplog):
    """Issue #663: a release ticket is not claimed while its Milestone
    still has open Issues — here an `ai-blocked` P0 that no ready scan
    sees. The gate reads GitHub's own `open_issues` counter, so a
    temporarily un-claimable Issue (ai-blocked / ai-pr-opened / not yet
    indexed) can no longer let the release run ahead."""
    monkeypatch.setattr(seam, "run_command", _release_scan_fake("3"))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "release_milestone_incomplete" in caplog.text
    assert "milestone=v0.4.2" in caplog.text
    assert "open_issues=3" in caplog.text
    # The Issue stays `ai-ready`: it was never claimed.
    assert "picked issue=254" not in caplog.text


def test_release_claimed_when_milestone_only_has_the_release(
    monkeypatch, caplog,
):
    """Issue #663: with only the release ticket itself left
    (`open_issues=1`) the release is claimed exactly as before."""
    monkeypatch.setattr(seam, "run_command", _release_scan_fake("1"))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == RELEASE_663
    assert "picked issue=254" in caplog.text


def test_release_not_claimed_when_milestone_check_fails(monkeypatch, caplog):
    """Issue #663: a milestone query that cannot be evaluated never
    claims — a bad release is irreversible, so the check fails safe and
    the next tick retries (recoverable, never `ai-blocked`)."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command", _release_scan_fake(error))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "release_milestone_check_failed" in caplog.text
    assert "picked issue=254" not in caplog.text


def test_release_without_milestone_claimed_without_api_call(
    monkeypatch, caplog,
):
    """Issue #663: a release with no Milestone (and no configured
    `active_milestone`) keeps the pre-#663 behavior — claimed with no
    extra API call."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    commands_seen = []

    def fake_run(command, **kwargs):
        commands_seen.append(command)
        if "label:ai-release" in " ".join(command):
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == release
    assert not [c for c in commands_seen if c[1] == "api"]
    assert "picked issue=254" in caplog.text


def test_release_milestone_falls_back_to_active_milestone(
    monkeypatch, caplog,
):
    """Issue #663: without a Milestone on the release Issue, the
    configured `active_milestone` (the fallback scan's scope) is the
    Milestone whose completeness is checked."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    api_calls = []

    def fake_run(command, **kwargs):
        if command[1] == "api":
            api_calls.append(command)
            return "2"
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.3.0",
        ) is None
    assert api_calls
    assert "milestone=v0.3.0" in caplog.text
    assert "open_issues=2" in caplog.text


def test_release_fallback_scan_fetches_milestone(monkeypatch):
    """Issue #663: the release fallback scan fetches `milestone` so the
    completeness gate reads the release's GitHub Milestone from the same
    query — no extra `gh issue view`."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    scans = []

    def fake_run(command, **kwargs):
        scans.append(command)
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == release
    fields = scans[-1][scans[-1].index("--json") + 1]
    assert "milestone" in fields.split(",")


def test_pick_issue_p0_blocked_by_open_blocker_falls_back_to_bug(
    monkeypatch, caplog,
):
    """Issue #101: a P0 with open native blockers is skipped by the P0
    scan (same blockedBy semantics as the other scans — no claim, no
    label change, no worktree); the bug scan then decides."""
    blocked_p0 = {
        "number": 7, "title": "p0 blocked", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 3}], "totalCount": 1},
    }
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # Only the P0 and bug scans run (the bug scan claims the bug):
        # the P0 scan sees the blocked P0, the bug scan the bug.
        if "label:p0" in searches[-1]:
            return json.dumps([blocked_p0])
        return json.dumps([bug])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == bug
    # The blocked P0 was skipped with the structured blocked_by line;
    # the bug was claimed with priority=normal (it is not a P0).
    assert "blocked_by" in caplog.text
    assert "issue=7" in caplog.text
    assert "3" in caplog.text
    assert len(searches) == 2
    assert "priority=normal" in caplog.text


def test_pick_issue_p0_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #101: a failed P0 scan fails open like the other scans
    (Issue #54) — the tick claims nothing from this repo, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_logs_priority_normal_for_plain_pickup(
    monkeypatch, caplog,
):
    """Issue #101: a non-P0 pickup (bug or plain) logs
    `priority=normal` — the explicit field is present on every
    pickup, not only for P0."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert "priority=normal" in caplog.text
    assert "issue=10" in caplog.text


def test_issue_priority_reads_the_p0_label():
    """Issue #101: `issue_priority` is a pure function of the issue's
    `labels` (the scans fetch `labels`, so no extra gh call): `p0`
    when the label is present, `normal` otherwise."""
    assert runner.issue_priority({
        "number": 7,
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
    }) == "p0"
    assert runner.issue_priority({
        "number": 10,
        "labels": [{"name": "ai-ready"}, {"name": "bug"}],
    }) == "normal"
    assert runner.issue_priority({"number": 10}) == "normal"
    # Malformed label fields never break the pickup (fail to normal,
    # like the blockedBy field fails open): a P0 misread as normal
    # only loses its ordering for one run, never the delivery.
    assert runner.issue_priority({"number": 10, "labels": "nope"}) \
        == "normal"
    assert runner.issue_priority({
        "number": 10, "labels": ["p0", {"name": 7}],
    }) == "normal"


# --- Issue #93: Epic handling (ai-epic) --------------------------------------


def test_is_epic_reads_the_ai_epic_label():
    """Issue #93: `is_epic` is a pure function of the issue's `labels`
    (the scans fetch `labels`, so no extra gh call): True when the
    `ai-epic` label is present. A missing or malformed `labels` field
    fails open to "not an epic" (the same style as `issue_priority`
    failing to normal): the scan always requests `labels`, so a shape
    change only loses the Epic guard for one run — it must never
    deadlock the queue."""
    assert claim.is_epic({
        "number": 80,
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
    }) is True
    assert claim.is_epic({
        "number": 10,
        "labels": [{"name": "ai-ready"}, {"name": "bug"}],
    }) is False
    assert claim.is_epic({"number": 10}) is False
    assert claim.is_epic({"number": 10, "labels": "nope"}) is False
    assert claim.is_epic({
        "number": 10, "labels": ["ai-epic", {"name": 7}],
    }) is False


def test_pick_issue_skips_epic_and_claims_next(monkeypatch, caplog):
    """Issue #93: an `ai-ready` Issue carrying `ai-epic` is never
    claimed — no label change, no worktree, no run: the runner logs a
    structured `epic_not_claimed` line with the Issue number and repo
    and moves on to the next ready Issue of the same repo."""
    epic = {
        "number": 80, "title": "v0.1 release checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    ready = {
        "number": 81, "title": "sub task", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == ready
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text
    assert "repo=xqliu/orbi" in caplog.text
    # The Epic was skipped for being an Epic — not for blockers — so
    # no `blocked_by` line is logged for it.
    assert "blocked_by" not in caplog.text


def test_pick_issue_returns_none_when_only_epics_are_ready(
    monkeypatch, caplog,
):
    """Issue #93: a ready queue that only contains Epics yields no
    claim — the tick ends idle upstream (`no_ready_issue`), so an
    Epic never occupies a delivery slot."""
    epic_a = {
        "number": 80, "title": "v0.1 checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    epic_b = {
        "number": 133, "title": "0.2.0 workspace", "body": "",
        "labels": [{"name": "ai-epic"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        # The Epics carry no p0/bug/ai-release label: only the plain
        # ready scan sees them; the release fallback scan (Issue #255)
        # matches nothing.
        if (
            "label:p0" in search or "label:bug" in search
            or "label:ai-release" in search
        ):
            return json.dumps([])
        return json.dumps([epic_a, epic_b])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert caplog.text.count("epic_not_claimed") == 2
    assert "issue=80" in caplog.text
    assert "issue=133" in caplog.text


def test_pick_issue_epic_check_precedes_blocker_check(monkeypatch, caplog):
    """Issue #93: an Epic is never claimed regardless of its blockedBy
    state — the `epic_not_claimed` reason (it is an Epic) is more
    fundamental than its blocker list, so the Epic check runs first
    and no `blocked_by` line is logged for it."""
    epic = {
        "number": 80, "title": "epic", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {
            "nodes": [{"number": 9}, {"number": 10}], "totalCount": 2,
        },
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text
    assert "blocked_by" not in caplog.text


def test_pick_issue_skips_epic_in_p0_and_bug_scans(monkeypatch, caplog):
    """Issue #93: the Epic skip applies to EVERY ready scan (P0, bug,
    plain) — an `ai-epic` Issue is never claimed no matter which
    priority queue it sits in; the next non-Epic ready Issue is
    claimed instead."""
    epic_p0 = {
        "number": 80, "title": "epic p0", "body": "",
        "labels": [
            {"name": "ai-epic"}, {"name": "ai-ready"}, {"name": "p0"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    epic_bug = {
        "number": 81, "title": "epic bug", "body": "",
        "labels": [
            {"name": "ai-epic"}, {"name": "ai-ready"}, {"name": "bug"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:p0" in search:
            return json.dumps([epic_p0])
        if "label:bug" in search:
            return json.dumps([epic_bug])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert caplog.text.count("epic_not_claimed") == 2
    assert "issue=80" in caplog.text
    assert "issue=81" in caplog.text


def test_pick_in_progress_issue_scan_excludes_epics(monkeypatch, tmp_path):
    """Issue #93: the restart-resume scan excludes `ai-epic` — a
    legacy Epic left behind with `ai-in-progress` (the #80 scene,
    before the Epic mechanism existed) must never be resumed into
    `run_pi`: an Epic is coordination, not an executable task."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_scan_fetches_labels(monkeypatch, tmp_path):
    """Issue #101: the in-flight restart scan fetches `labels` too, so
    a P0 a killed runner left behind keeps its priority in the
    progress comment on resume (the scan's exclusions are unchanged)."""
    issue = {
        "number": 18, "title": "task", "body": "body",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_scan_fetches_milestone(monkeypatch, tmp_path):
    """Issue #671: the in-flight restart scan fetches `milestone` too, so
    a release run killed mid-release and resumed by `process_release`
    scopes its leftover-delivery gate to the release's OWN Milestone —
    never to the config's `active_milestone`, which must not gate a
    resume (Issue #139)."""
    issue = {
        "number": 664, "title": "Release v0.4.3", "body": "body",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "milestone": {"title": "v0.4.3"},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    resumed = claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    )
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]
    # The resumed release's gate scope is its own Milestone, not the
    # config value.
    assert runner.release_target_milestone(resumed, "v0.4.4") == "v0.4.3"


def test_pick_in_progress_issue_scans_in_flight_issues(monkeypatch, tmp_path):
    """A killed runner leaves `ai-ready`+`ai-in-progress` behind (Issue
    #18): the claim scan must recover such in-flight Issues, so the
    restart resume (run id / worktree / progress comment reuse) is
    reachable in the production flow — the ready scan alone excludes
    `ai-in-progress` and would strand the run forever."""
    issue = {
        "number": 18, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_uses_the_repo_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #527: a repository that replaced `ai-ready` with its own
    claim label keeps the restart-resume path — the in-flight scan must
    search the SAME queue entry the ready scan used, otherwise a killed
    (or model-wait-recovered) run is stranded forever."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
        dispatch_label="repo-ready",
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:repo-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_returns_none_when_idle(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]",
    )
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None


def test_pick_in_progress_issue_skips_when_another_runner_is_live(
    monkeypatch, tmp_path,
):
    """A slot held by ANOTHER process proves a live runner is working
    (Issue #39 slot semantics): the `ai-in-progress` label is in
    flight, not orphaned, so no second Pi may be started for it. This
    runner's own slot (its own PID) does not block the scan."""
    gh_calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: gh_calls.append(command) or "[]",
    )
    monkeypatch.setattr(seam, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, 4242)])
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert gh_calls == [], "no gh traffic while another runner is live"
    # Own PID: the scan still runs (this runner holds its own slot).
    monkeypatch.setattr(seam, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, os.getpid())])
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert len(gh_calls) == 1


def test_pick_next_delivery_recovers_in_flight_issue_before_ready(
    monkeypatch, tmp_path,
):
    """Claim order (Issue #18): resumable PRs first, then in-flight
    restarts (killed runner, `ai-in-progress` left behind), then ready
    Issues — a dead run is resumed, never skipped by the ready scan."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    ready = {"number": 3, "title": "ready", "body": ""}
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: (
            in_flight if repo == "r1" else None
        ),
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: ready)
    assert claim.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r1", in_flight, None)


def test_pick_next_delivery_keeps_resumable_delivery_first(
    monkeypatch, tmp_path,
):
    """An `ai-fix-needed` PR resume always beats an in-flight restart:
    the PR already exists and must not be re-implemented."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    resumable = {"number": 5, "title": "fix needed", "body": ""}
    scene = config_domain.RunnerConfig(run_id="a1b2c3d4")
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: (
            (resumable, scene) if repo == "r2" else None
        ),
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: in_flight,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: in_flight)
    assert claim.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r2", resumable, scene)


def test_pick_next_delivery_falls_through_to_ready_when_no_in_flight(
    monkeypatch, tmp_path,
):
    ready = {"number": 3, "title": "ready", "body": ""}
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: ready)
    assert claim.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r1", ready, None)


def test_pick_next_delivery_returns_none_when_all_scans_empty(
    monkeypatch, tmp_path,
):
    """All four scans empty; the fourth is Issue #1745's deferred
    `ai-awaiting-merge` re-check (see test_resume_pr for its behavior
    against the GitHub fake)."""
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: None)
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    assert claim.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) is None


def test_pick_next_issue_returns_first_ready_source(monkeypatch):
    issue = {"number": 1, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None, **_kwargs):
        calls.append(repo)
        return issue if repo == "xqliu/orbi-backlog" else None

    monkeypatch.setattr(claim, "pick_issue", pick)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) == (
        "xqliu/orbi-backlog", issue,
    )
    assert calls == ["xqliu/orbi-backlog"]


def test_pick_next_issue_falls_through_to_second_source(monkeypatch):
    issue = {"number": 2, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None, **_kwargs):
        calls.append(repo)
        return issue if repo == "xqliu/orbi" else None

    monkeypatch.setattr(claim, "pick_issue", pick)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) == (
        "xqliu/orbi", issue,
    )
    assert calls == ["xqliu/orbi-backlog", "xqliu/orbi"]


def test_pick_next_issue_returns_none_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: None)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) is None


def test_freeze_base_fetches_remote_and_returns_exact_sha(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return "abc123def456"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert calls == [(
        ["git", "fetch", "origin", "main"], {
            "cwd": tmp_path,
            "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
        },
    ), (
        ["git", "rev-parse", "origin/main"], {"cwd": tmp_path},
    )]


def test_freeze_base_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the freeze fetch updates the shared remote-tracking
    # ref, so it must run under the SAME base-sync lock (a concurrent
    # probe must not acquire it while the fetch is in flight) and the
    # lock must be free again afterwards.
    held = []

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            held.append(not _probe_lock_free(tmp_path))
        return "abc123def456"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


def test_freeze_base_fails_fast_when_remote_base_is_missing(monkeypatch, tmp_path):
    error = subprocess.CalledProcessError(
        128, ["git", "rev-parse", "origin/main"],
        stderr="fatal: ambiguous argument",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(subprocess.CalledProcessError):
        runner.freeze_base(tmp_path, "main")


def test_create_worktree_reuses_existing_path_for_a_resumed_run(
    monkeypatch, tmp_path,
):
    """Only a resumed run (same run id after a process restart) reaches
    an existing worktree path; it is the scene the run continues in
    (Issue #18), so it is reused, never recreated."""
    existing = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    existing.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command),
    )
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "abc123def456",
    ) == existing
    assert calls == [], "a resumed worktree must never be recreated"


def test_create_worktree_adds_branch_from_frozen_base_sha(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "",
    )
    assert runner.create_worktree(tmp_path, "owner/repo", 3, "run1", "abc123def456") == path
    assert calls == [
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "-b", "orbi/owner-repo-issue-3", str(path), "abc123def456"],
         {"cwd": tmp_path}),
    ]


def test_create_worktree_reuses_orphan_local_branch_never_exits_255(
    monkeypatch, tmp_path,
):
    """Issue #662 (the #655 incident): a SIGKILLed run can leave the stable
    branch behind with no worktree and no remote counterpart (a pure orphan).
    `worktree add -b` cannot re-create it (the exit-255 that burned the Issue
    into terminal ai-blocked), so the orphan branch is reused by identity."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return "orbi/owner-repo-issue-3"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
    ) == path
    assert calls == [
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", str(path), "orbi/owner-repo-issue-3"],
         {"cwd": tmp_path}),
    ]


def test_create_worktree_reuses_orphan_local_branch_real_git(tmp_path):
    """Real-git acceptance (Issue #662, the #655 scene): the local orphan
    branch is checked out into the new worktree where the bare `worktree add
    -b` exited 255 (`fatal: a branch named ... already exists`)."""
    work, head = make_local_remote_pair(tmp_path)
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(repo_dir)],
        check=True, capture_output=True,
    )
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", key, value],
            check=True, capture_output=True,
        )
    # The killed claim left the branch at the base commit; it was never
    # pushed, so origin has no counterpart (the #655 orphan).
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch",
         "orbi/owner-repo-issue-3", head],
        check=True, capture_output=True,
    )
    remote = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-remote", "--heads", "origin",
         "refs/heads/orbi/owner-repo-issue-3"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert remote == "", "the orphan branch must not exist on origin"
    # The bare `git worktree add -b` of the incident fails (exit 255).
    incident = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b",
         "orbi/owner-repo-issue-3", str(tmp_path / "incident"), head],
        capture_output=True, text=True,
    )
    assert incident.returncode != 0, incident
    assert "already exists" in incident.stderr
    # The orphan-aware path reuses the branch instead.
    path = runner.create_worktree(repo_dir, "owner/repo", 3, "run1", head)
    current = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == "orbi/owner-repo-issue-3"


def test_create_worktree_reuses_existing_remote_branch(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base", existing_branch=True,
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/orbi/owner-repo-issue-3:refs/remotes/origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", "-b", "orbi/owner-repo-issue-3", str(path), "origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
    ]


def test_create_worktree_existing_local_branch_is_reused_never_re_created(
    monkeypatch, tmp_path,
):
    """An equal local delivery branch remains the checked-out identity."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return "orbi/owner-repo-issue-3"
        if command == ["git", "rev-parse", "orbi/owner-repo-issue-3"]:
            return "same-sha"
        if command == ["git", "rev-parse", "origin/orbi/owner-repo-issue-3"]:
            return "same-sha"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base", existing_branch=True,
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/orbi/owner-repo-issue-3:refs/remotes/origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "rev-parse", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "rev-parse", "origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", str(path), "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
    ]


def test_create_worktree_branch_override_checks_out_the_external_head(
    monkeypatch, tmp_path,
):
    """Issue #608: an external takeover checks out the contributor's own
    head branch — the identity the takeover PR is frozen on."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
        existing_branch=True, branch="fix/outer",
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/fix/outer:refs/remotes/origin/fix/outer"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "fix/outer"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", "-b", "fix/outer", str(path), "origin/fix/outer"], {"cwd": tmp_path}),
    ]


def test_create_worktree_fork_takeover_fetches_pull_head_ref(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
        existing_branch=True, branch="fix/outer", pr_number=592,
    ) == path
    fetch = calls[0]
    assert fetch[0] == [
        "git", "fetch", "origin",
        "+pull/592/head:refs/remotes/origin/fix/outer",
    ]
    assert len(fetch[0]) == 4
    assert fetch[1] == {
        "cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    }
    assert calls[-1][0][-1] == "origin/fix/outer"


def test_create_worktree_fork_takeover_repeats_with_existing_target(tmp_path):
    """Real-git acceptance: a fork PR refspec can overwrite its existing
    remote-tracking destination on a repeated takeover."""
    work, first_head = make_local_remote_pair(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin",
         "HEAD:refs/pull/592/head"],
        check=True, capture_output=True, timeout=30,
    )
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(repo_dir)],
        check=True, capture_output=True, timeout=30,
    )
    path = runner.create_worktree(
        repo_dir, "owner/repo", 3, "first", first_head,
        existing_branch=True, branch="fix/outer", pr_number=592,
    )
    assert path.is_dir()
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(path)],
        check=True, capture_output=True, timeout=30,
    )

    (work / "f.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "f.txt"],
        check=True, capture_output=True, timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "second"],
        check=True, capture_output=True, timeout=30,
    )
    second_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "--force", "origin",
         "HEAD:refs/pull/592/head"],
        check=True, capture_output=True, timeout=30,
    )
    repeated = runner.create_worktree(
        repo_dir, "owner/repo", 3, "second", first_head,
        existing_branch=True, branch="fix/outer", pr_number=592,
    )
    assert repeated.is_dir()
    fetched = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse",
         "refs/remotes/origin/fix/outer"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    assert fetched == second_head


def test_create_worktree_takeover_with_existing_branch_never_exits_255(tmp_path):
    """Real-git acceptance (Issue #608 scenario a): the stable branch
    already exists on origin — the takeover path succeeds where the bare
    `worktree add -b <branch>` of the #600 incident exited 255."""
    work, head = make_local_remote_pair(tmp_path)
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(repo_dir)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "t@t"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "t"],
        check=True, capture_output=True,
    )
    # The stable branch already exists on origin (left by the previous
    # run) — pushed from the working clone of the shared fixture.
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin",
         "main:refs/heads/orbi/owner-repo-issue-3"],
        check=True, capture_output=True,
    )
    # The previous run also left the LOCAL branch in the deployment
    # checkout (the incident scene: `-b` cannot re-create it).
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch",
         "orbi/owner-repo-issue-3", head],
        check=True, capture_output=True,
    )
    # The bare `git worktree add -b` of the incident scene fails (the
    # runner journal recorded it as exit 255): `-b` never re-creates an
    # existing branch.
    incident = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b",
         "orbi/owner-repo-issue-3", str(tmp_path / "incident"), head],
        capture_output=True, text=True,
    )
    assert incident.returncode != 0, incident
    assert "already exists" in incident.stderr
    # The takeover path reuses the existing branch and succeeds.
    path = runner.create_worktree(
        repo_dir, "owner/repo", 3, "run1", head, existing_branch=True,
    )
    current = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == "orbi/owner-repo-issue-3"


def test_create_release_worktree_resets_leftover_worktree_to_current_commit(
    monkeypatch, tmp_path,
):
    old_worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-oldrun"
    branch = "orbi/owner-repo-issue-3"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return f"worktree {old_worktree}\nHEAD old\nbranch refs/heads/{branch}\n"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == old_worktree
    assert calls[-1] == (
        ["git", "reset", "--hard", "new-release-commit"],
        {"cwd": old_worktree},
    )
    assert not any(command[:3] == ["git", "worktree", "add"]
                   for command, _ in calls)


def test_create_release_worktree_handles_leftover_local_branch(
    monkeypatch, tmp_path,
):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-newrun"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return ""
        if command == ["git", "branch", "--list", "orbi/owner-repo-issue-3"]:
            return "  orbi/owner-repo-issue-3"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == path
    assert calls[-2:] == [
        (["git", "worktree", "add", "--force", str(path),
          "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "reset", "--hard", "new-release-commit"],
         {"cwd": path}),
    ]


def test_create_release_worktree_creates_from_release_commit_when_branch_is_new(
    monkeypatch, tmp_path,
):
    calls = []
    expected = tmp_path / "created"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return ""
        if command == ["git", "branch", "--list", "orbi/owner-repo-issue-3"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *args: expected)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == expected
    assert calls[-1] == (
        ["git", "reset", "--hard", "new-release-commit"],
        {"cwd": expected},
    )


def test_latest_run_id_returns_none_without_task_worktrees(tmp_path):
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) is None
    # Unrelated directories are not task worktrees.
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "other").mkdir()
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) is None


def test_latest_run_id_returns_the_run_id_of_the_newest_worktree(tmp_path):
    slug = "owner-repo"
    first = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-0d111111"
    second = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-2e222222"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    # The newest by mtime wins, even when it was created first by name.
    os.utime(first, (200, 200))
    os.utime(second, (100, 100))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "0d111111"
    os.utime(second, (300, 300))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "2e222222"
    # A worktree of another issue (or another source repo) never counts.
    other_issue = tmp_path / ".worktrees" / f"orbi-{slug}-issue-4-ffff0000"
    other_repo = tmp_path / ".worktrees" / f"orbi-other-other-issue-3-ffff1111"
    other_issue.mkdir()
    other_repo.mkdir()
    os.utime(other_issue, (400, 400))
    os.utime(other_repo, (400, 400))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "2e222222"


# --- Issue #219: continue the interrupted run, never a fresh redo ---------


def test_run_state_path_lives_in_the_gitignored_orbi_dir(tmp_path):
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    assert run_state.run_state_path(worktree) == (
        worktree / ".orbi" / "run-state.json"
    )


def test_write_run_state_writes_the_run_identity(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = json.loads(run_state.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"
    assert state["branch"] == "orbi/owner-repo-issue-3"
    assert state["worktree"] == str(worktree)
    assert isinstance(state["created_at"], str) and state["created_at"]


def test_write_run_state_is_idempotent_for_a_resumed_run(tmp_path):
    """A resumed run refreshes the SAME state file (same run id) — the
    file is the same-run marker, never a per-session artifact."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = json.loads(run_state.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"


def test_read_run_state_returns_none_without_the_file(tmp_path):
    assert run_state.read_run_state(tmp_path) is None


def test_read_run_state_round_trips_the_written_state(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"


def test_read_run_state_fails_fast_on_unreadable_or_malformed_state(tmp_path):
    """A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run (Issue #219)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    path = run_state.run_state_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)
    path.write_text(json.dumps({"run_id": "a1b2c3d4"}), encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)


def test_write_run_state_preserves_the_push_history(tmp_path):
    """The push-history fields (Issue #833) survive a resume's state
    refresh — the same delivery line keeps its recorded heads."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    assert review_merge.read_pushed_head(worktree) == "h1"
    assert review_merge.read_pushed_base(worktree) == "b1"


def test_record_pushed_base_is_set_once_per_run(tmp_path):
    """A re-claim of the same run re-derives a moved HEAD, which is
    not the push line's origin — the first recorded base wins."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.record_pushed_base(worktree, "b1")
    run_state.record_pushed_base(worktree, "b2")
    assert review_merge.read_pushed_base(worktree) == "b1"


def test_record_pushed_head_degrades_without_the_state_file(
        tmp_path, caplog):
    """Recording is bypass-safe (Issue #73): a recreated worktree lost
    `.orbi/` — the record logs `pushed_head_unrecorded` and continues;
    the merge record degrades to `unknown`, the delivery does not
    fail."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    caplog.set_level("WARNING")
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    assert review_merge.read_pushed_head(worktree) is None
    assert review_merge.read_pushed_base(worktree) is None
    assert "pushed_head_unrecorded" in caplog.text


def test_record_pushed_head_degrades_on_a_corrupt_state_file(
        tmp_path, caplog):
    """The same bypass safety for a corrupt record: the resume readers
    fail fast on it before any record point, and the recorder itself
    only logs and degrades the merge record to `unknown`."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    path = run_state.run_state_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    caplog.set_level("WARNING")
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    assert review_merge.read_pushed_head(worktree) is None
    assert "pushed_head_unrecorded" in caplog.text


def test_changed_files_lists_uncommitted_changes(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        # The third line has a blank XY column: it is not a change.
        return " M src/a.py\n?? src/b.py\n   src/c.py\n"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert pi_session.changed_files(tmp_path) == ["src/a.py", "src/b.py"]
    assert calls == [(
        ["git", "status", "--porcelain"], {"cwd": tmp_path},
    )]


def test_resume_context_is_none_for_a_fresh_worktree(tmp_path, monkeypatch):
    """No uncommitted changes and no previous session: the agent starts
    from the Issue alone (the exact pre-#219 prompt)."""
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(pi_session, "activity_snapshot", lambda *a, **k: None)
    assert pi_session.resume_context(tmp_path) is None


def test_resume_context_carries_changes_and_session_progress(
    tmp_path, monkeypatch,
):
    """The new session starts from the existing work: the instruction to
    continue, the previous session's progress and the changed files —
    never a fresh redo (Issue #219)."""
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: " M src/a.py\n?? src/b.py\n",
    )
    monkeypatch.setattr(
        pi_session, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-1", "session_file": "x.jsonl",
            "events": 7, "phase": "test", "last_activity": "2026-09-02T04:01:00Z",
            "action": "bash pytest tests/", "result": "ok",
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = pi_session.resume_context(tmp_path)
    assert "Continue" in context or "continue" in context
    assert "redo" in context.lower() or "scratch" in context.lower()
    assert "sess-1" in context
    assert "7" in context
    assert "test" in context
    assert "bash pytest tests/" in context
    assert "src/a.py" in context
    assert "src/b.py" in context
    assert "2" in context


def test_resume_context_without_changes_carries_the_session_progress(
    tmp_path, monkeypatch,
):
    """A clean worktree with a previous session (the agent committed
    mid-run before the interruption) still continues the same run."""
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(
        pi_session, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-2", "session_file": "x.jsonl",
            "events": 3, "phase": "commit", "last_activity": None,
            "action": "git commit -m x", "result": None,
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = pi_session.resume_context(tmp_path)
    assert context is not None
    assert "sess-2" in context
    assert "src/a.py" not in context


def test_resume_run_id_returns_none_without_candidates(tmp_path):
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "other").mkdir()
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None


def test_resume_run_id_matches_by_issue_and_repo_name_across_a_rename(
    tmp_path,
):
    """The repo was renamed (slug `xqliu-orbi` -> `orbi-build-orbi`):
    the old worktree is found by issue number + repo NAME (the stable
    identity) and the SAME run continues — no second run/branch/
    worktree (Issue #219)."""
    old = tmp_path / ".worktrees" / "orbi-xqliu-orbi-issue-42-aaaa1111"
    old.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="aaaa1111", issue=42, branch="orbi/xqliu-orbi-issue-42", worktree=old, source_repo="xqliu/orbi"))
    assert runner.resume_run_id(
        tmp_path, "orbi-build/orbi", 42,
    ) == "aaaa1111"
    # A different repo name never matches, even with the same issue.
    assert runner.resume_run_id(
        tmp_path, "orbi-build/other", 42,
    ) is None


def test_resume_run_id_returns_newest_matching_worktree(tmp_path):
    slug = "owner-repo"
    first = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-0d111111"
    second = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-2e222222"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="0d111111", issue=3, branch="b1", worktree=first, source_repo="owner/repo"))
    run_state.write_run_state(RunContext(run_id="2e222222", issue=3, branch="b2", worktree=second, source_repo="owner/repo"))
    os.utime(first, (300, 300))
    os.utime(second, (100, 100))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "0d111111"
    os.utime(second, (400, 400))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "2e222222"


def test_resume_run_id_excludes_other_issues_and_repos(tmp_path):
    slug = "owner-repo"
    other_issue = tmp_path / ".worktrees" / f"orbi-{slug}-issue-4-ffff0000"
    other_repo = tmp_path / ".worktrees" / f"orbi-other-other-issue-3-ffff1111"
    other_issue.mkdir(parents=True)
    other_repo.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="ffff0000", issue=4, branch="b1", worktree=other_issue, source_repo="owner/repo"))
    run_state.write_run_state(RunContext(run_id="ffff1111", issue=3, branch="b2", worktree=other_repo, source_repo="other/other"))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None


def test_resume_run_id_fails_fast_on_missing_state_file(tmp_path):
    """A worktree that claims the issue number but has NO run state file
    cannot be verified as the same run: fail fast with the exact reason,
    never a silent fresh redo (Issue #219)."""
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    worktree.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="run state"):
        runner.resume_run_id(tmp_path, "owner/repo", 3)


def test_resume_run_id_fails_fast_on_corrupt_state_file(tmp_path):
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    worktree.mkdir(parents=True)
    state = run_state.run_state_path(worktree)
    state.parent.mkdir(parents=True)
    state.write_text("corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="run state"):
        runner.resume_run_id(tmp_path, "owner/repo", 3)


def test_resume_run_id_skips_unrelated_worktrees_without_a_state_file(
    tmp_path,
):
    """A worktree of ANOTHER issue without a state file (a legacy
    completed run) is not the resume scene of this issue — it is
    skipped, not a fail-fast (only a worktree claiming THIS issue
    number must be verifiable)."""
    other = tmp_path / ".worktrees" / "orbi-owner-repo-issue-4-ffff0000"
    other.mkdir(parents=True)
    mine = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    mine.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="aaaa1111", issue=3, branch="b1", worktree=mine, source_repo="owner/repo"))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "aaaa1111"


def test_has_in_progress_label_checks_the_issue_label(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps({"labels": [{"name": "ai-in-progress"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.has_in_progress_label(4, "owner/repo") is True
    assert calls == [[
        "gh", "issue", "view", "4", "--repo", "owner/repo",
        "--json", "labels",
    ]]


def test_has_in_progress_label_is_false_without_the_label(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps(
            {"labels": [{"name": "ai-fix-needed"}]},
        ),
    )
    assert runner.has_in_progress_label(4, "owner/repo") is False


def test_has_in_progress_label_fails_fast_on_malformed_output(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    # Issue #785: the shared `issue_view` validates the object shape, so
    # the message wording is the view's ("must be"), not the caller's.
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.has_in_progress_label(4, "owner/repo")


def test_has_in_progress_label_fails_fast_on_malformed_labels(monkeypatch):
    """view 返回 JSON 对象但 labels 字段不是数组（Issue #658）：解析
    失败必须 fail-fast，绝不落入默认 False（那会让竞态守卫失明）。"""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"labels": "ai-in-progress"}),
    )
    with pytest.raises(ValueError, match="labels must be a JSON array"):
        runner.has_in_progress_label(4, "owner/repo")


def test_has_in_progress_label_reads_directly_not_via_the_search_index(
    monkeypatch,
):
    """Issue #658：认领前复查必须直读（gh issue view，强一致）——不能走
    与扫描同一套最终一致的搜索索引：另一实例几秒前刚打上的
    ai-in-progress，索引可能尚未收录，复查必须看到它才能让路。"""
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    assert runner.has_in_progress_label(4, "owner/repo") is True
    # The fixed path reads the Issue directly — the search index (which
    # has not ingested the other instance's claim yet) is never queried.
    assert calls == [[
        "gh", "issue", "view", "4", "--repo", "owner/repo",
        "--json", "labels",
    ]]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run_command(["gh", "issue", "list", "--search",
                          "label:ai-in-progress"])


def test_worktree_path_lives_inside_repo_worktrees_and_includes_run_id():
    repo_dir = Path("/srv/orbi/orbi")
    path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    assert path == repo_dir / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    assert Path(tempfile.gettempdir()) not in path.parents


def test_worktree_path_keeps_source_repo_in_name_to_avoid_same_number_collision():
    repo_dir = Path("/srv/orbi/orbi")
    pilot = runner.worktree_path(repo_dir, "xqliu/orbi", 14, "run1")
    ceo = runner.worktree_path(repo_dir, "xqliu/orbi-backlog", 14, "run1")
    assert pilot == repo_dir / ".worktrees" / "orbi-xqliu-orbi-issue-14-run1"
    assert ceo == repo_dir / ".worktrees" / "orbi-xqliu-orbi-backlog-issue-14-run1"
    assert pilot != ceo


def test_worktree_path_and_task_branch_differ_per_run_for_same_issue():
    repo_dir = Path("/srv/orbi/orbi")
    first_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    retry_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run2")
    assert first_path != retry_path
    assert runner.task_branch("owner/repo", 3, "run1") == "orbi/owner-repo-issue-3"
    assert runner.task_branch("owner/repo", 3, "run2") == runner.task_branch("owner/repo", 3, "run1")


def test_task_branch_includes_source_repo_to_avoid_same_number_collision():
    assert runner.task_branch("owner/pilot", 1, "run1") == "orbi/owner-pilot-issue-1"
    assert runner.task_branch("owner/pilot", 1, "run1") != runner.task_branch("owner/ceo", 1, "run1")


def test_claim_route_decision_table():
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=False, open_pr=False) == "implement"
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=True, open_pr=False) == "implement"
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=True, open_pr=True) == "review"
    assert runner.claim_route({runner.PR_OPENED_LABEL}, branch_exists=True, open_pr=True) == "review"
    assert runner.claim_route({runner.FIX_NEEDED_LABEL}, branch_exists=True, open_pr=True) == "review"


def test_verify_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "other-branch")
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.verify_pr(RunContext(run_id="e07383c2", issue=4, branch="orbi/issue-4", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_delivery_behind_latest_remote_base(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command, stderr="not an ancestor")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "base_branch=main" in caplog.text


def test_fake_verify_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_verify_run(["gh", "release", "list"])


def test_verify_pr_rejects_missing_pr(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[]",
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_non_array(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "{}",
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_returns_url_when_delivery_contains_latest_base(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == "https://github.com/orbi-build/orbi/pull/4"
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls


def test_verify_pr_requires_the_repo_dir_lock_location(
    monkeypatch, tmp_path,
):
    # Issue #171: the verify fetch updates the shared remote-tracking
    # ref, so the lock location (the deployment checkout's shared state
    # dir) must be explicit — there is no bypass path.
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    with pytest.raises(TypeError):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main")


def test_verify_pr_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the verify fetch runs under the SAME base-sync lock
    # (a concurrent probe must not acquire it while the fetch is in
    # flight) and the lock must be free again afterwards.
    held = []

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            held.append(not _probe_lock_free(tmp_path))
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


def test_verify_pr_rejects_pr_without_url(monkeypatch, tmp_path):
    # Issue #291: the shared base check runs first, so the fixture must
    # carry a valid base to actually reach the URL check.
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA,
        json.dumps([{"baseRefName": "main"}]),
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="open PR has no URL"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_pr_based_on_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(baseRefName="develop")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR base is develop, expected main",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_diverged_remote_pr_head(monkeypatch, tmp_path,
                                                   caplog):
    """Issue #50: a remote PR head that is NOT an ancestor of the local
    HEAD (the branch diverged) is still a failure: a plain push would
    be rejected and a force push is forbidden, so the resume must not
    continue on this branch."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            if command[3] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef":
                raise subprocess.CalledProcessError(
                    1, command, stderr="not an ancestor",
                )
            return ""
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRefOid="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError,
        match="PR head deadbeef.* is not local HEAD 01234567.*diverged",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_head_diverged" in caplog.text


def test_verify_pr_passes_through_when_local_head_ahead_of_pr_head(
        monkeypatch, tmp_path, caplog,
):
    """Issue #50 (the #158 `d13b0c56` scene): the local HEAD is AHEAD of
    the remote PR head (a commit made by a killed session that was
    never pushed). The verification logs the exact heads and PASSES
    THROUGH — the unpushed commit is preserved and the next review
    session pushes the task branch on the same PR (it never fails here,
    never force pushes and never creates a replacement PR)."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # The PR head IS an ancestor of the local HEAD (local is
            # ahead) — the #158 scene.
            return ""
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRefOid="ed72915ed72915ed72915ed72915ed72915ed7291",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        url = runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert url == FAKE_PR_URL
    assert "local_head_ahead_of_pr_head" in caplog.text
    assert "ed72915ed72915ed72915ed72915ed72915ed7291" in caplog.text
    assert FAKE_HEAD_SHA in caplog.text


def test_verify_pr_rejects_pr_body_without_run_marker(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body="no run marker here")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_run_marker_missing" in caplog.text


def test_verify_pr_rejects_pr_body_missing_field(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body=None)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


# ------------------------------------------- Fixes #N (Issue #53)


def test_verify_pr_accepts_pr_body_with_fixes_keyword(monkeypatch, tmp_path):
    """The contract keyword `Fixes #<issue>` in the body is accepted, so
    GitHub closes the source Issue natively when the PR merges."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\n\nFixes #4",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


@pytest.mark.parametrize("reference", ["Fixes #4", "Fixes 4"])
def test_verify_pr_accepts_hash_and_hashless_fixes_reference(
    monkeypatch, tmp_path, reference,
):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\\n\\n{reference}",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_rejects_pr_body_without_fixes_keyword(
    monkeypatch, tmp_path, caplog,
):
    """A body without `Fixes #<issue>` would leave the source Issue open
    after the merge: the delivery is rejected."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\n\nPlan",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text
    assert "issue=4" in caplog.text


def test_verify_pr_rejects_pr_body_with_wrong_issue_number(
    monkeypatch, tmp_path, caplog,
):
    """`Fixes #9` does not close Issue #4: the keyword must point at the
    source Issue of this delivery."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=(
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #9"
                ),
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text


def test_verify_pr_rejects_pr_body_with_longer_issue_number(
    monkeypatch, tmp_path, caplog,
):
    """`Fixes #41` closes Issue 41, not Issue 4: the keyword must match
    the source Issue number exactly, not as a digit prefix (review F1)."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=(
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #41"
                ),
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text


def test_verify_pr_external_mode_skips_marker_and_fixes_checks(
    monkeypatch, tmp_path,
):
    """Issue #608: an external takeover PR carries neither the run marker
    nor a `Fixes #<issue>` keyword (the contributor wrote the body) —
    `external_pr=True` verifies the same delivery WITHOUT those two body
    checks; the Runner closes the triage Issue itself after the merge."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return "fix/outer"
        if command[:3] == ["gh", "pr", "view"]:
            payload = json.loads(fake_verify_pr_payload(
                headRefName="fix/outer",
                body="please review my fix, thanks",
                isCrossRepository=True,
                state="OPEN",
                headRepository={"name": "orbi-fork"},
                headRepositoryOwner={"login": "contributor"},
            ))[0]
            return json.dumps(payload)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(
        RunContext(
            run_id=FAKE_RUN_ID, issue=4, branch="fix/outer",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO,
        expected_url=FAKE_PR_URL, external_pr=True,
    ) == FAKE_PR_URL


def test_verify_pr_normal_mode_still_requires_marker_and_fixes(
    monkeypatch, tmp_path,
):
    """The external-mode skip is opt-in: a Runner-owned PR without the
    body contract still fails the verification."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body="please review my fix")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="run marker"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_external_takeover_pr_fails_fast_on_a_malformed_view(
    monkeypatch, tmp_path,
):
    """A `gh pr view` response that is not an object is a corrupted
    scene: the claim fails fast (the same contract as the open-PR
    query), it never silently proceeds."""
    _fake_takeover_view(monkeypatch, ["not", "an", "object"])
    with pytest.raises(RuntimeError, match="did not return an object"):
        runner.external_takeover_pr(
            tmp_path, "<!-- orbi:external-pr:592 -->",
            "xqliu/orbi", "main",
        )


def test_external_takeover_pr_resolves_the_marked_pr(monkeypatch, tmp_path):
    """Issue #608: the triage marker routes the claim to the external PR —
    open and against the configured base, the takeover proceeds."""
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN",
        "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "main",
        "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    pr = runner.external_takeover_pr(
        tmp_path,
        "body text\n<!-- orbi:external-pr:592 -->\n",
        "xqliu/orbi", "main",
    )
    assert pr is not None
    assert pr["headRefName"] == "fix/outer"
    assert pr["url"] == "https://github.com/xqliu/orbi/pull/592"


def test_external_takeover_pr_ignores_a_closed_pr(monkeypatch, tmp_path):
    """The external PR is merged/closed already: no takeover — the claim
    proceeds as a fresh internal delivery."""
    _fake_takeover_view(monkeypatch, {
        "state": "CLOSED", "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "main", "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    assert runner.external_takeover_pr(
        tmp_path, "<!-- orbi:external-pr:592 -->",
        "xqliu/orbi", "main",
    ) is None


def test_external_takeover_pr_ignores_a_pr_against_another_base(
    monkeypatch, tmp_path,
):
    """The delivery loop only merges the configured protected base: a
    PR against another branch is not a takeover."""
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN", "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "develop", "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    assert runner.external_takeover_pr(
        tmp_path, "<!-- orbi:external-pr:592 -->",
        "xqliu/orbi", "main",
    ) is None


def test_external_takeover_pr_returns_none_without_a_marker(monkeypatch,
                                                            tmp_path):
    """A body without the triage marker never reaches `gh pr view`."""
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no marker: gh must not be called"),
        ),
    )
    assert runner.external_takeover_pr(
        tmp_path, "an ordinary issue body", "xqliu/orbi", "main",
    ) is None
    assert runner.external_takeover_pr(
        tmp_path, None, "xqliu/orbi", "main",
    ) is None


def test_opened_pr_comment_round_trips_the_external_flag():
    """The takeover scene comment carries `external: true` and the parser
    restores it; a Runner-owned scene parses as non-external."""
    body = runner.opened_pr_comment_body(
        "a1b2c3d4",
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 "
        "priority=normal",
        "https://github.com/xqliu/orbi/pull/592", external=True, language_code="en",
    )
    scene = run_state.parse_pr_comment(body)
    assert scene["pr_url"] == "https://github.com/xqliu/orbi/pull/592"
    assert scene["run_id"] == "a1b2c3d4"
    assert bool(scene["external"]) is True

    own = run_state.parse_pr_comment(runner.opened_pr_comment_body(
        "a1b2c3d4",
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 "
        "priority=normal",
        "https://github.com/xqliu/orbi/pull/40", language_code="en",
    ))
    assert bool(own["external"]) is False


def test_verify_pr_queries_base_head_and_accepts_matching_pr(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == "https://github.com/orbi-build/orbi/pull/4"
    assert ["git", "rev-parse", "HEAD"] in calls
    # Issue #291: verify_pr issues the ONE shared PR query contract.
    assert [
        "gh", "pr", "list", "--state", "open", "--head",
        f"orbi/issue-4-{FAKE_RUN_ID}",
        "--json", (
            "number,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner,"
            "isCrossRepository,body"
        ),
        "--limit", "100",
    ] in calls


# ------------------------------------------- repo + expected URL (F1, F3)


def test_verify_pr_accepts_pr_in_expected_repo_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO, expected_url=FAKE_PR_URL) == FAKE_PR_URL


def test_verify_pr_rejects_pr_head_in_another_repo(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={"name": "other"},
                headRepositoryOwner={"login": "attacker"},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="PR head repo is attacker/other, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)
    assert "pr_repo_mismatch" in caplog.text


def test_verify_pr_rejects_pr_head_repo_missing_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository=None, headRepositoryOwner=None,
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)


def test_verify_pr_rejects_pr_head_repo_empty_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={}, headRepositoryOwner={},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)


def test_verify_pr_skips_repo_check_when_pr_repo_not_given(monkeypatch,
                                                            tmp_path):
    """The fresh-claim path (process_issue) does not pass pr_repo: a PR
    payload without head repo fields still passes."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefOid": FAKE_HEAD_SHA,
                "body": (
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_rejects_url_different_from_expected(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                url="https://github.com/orbi-build/orbi/pull/99",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=(
            "PR URL https://github.com/orbi-build/orbi/pull/99 is "
            "not the recovered original PR "
            "https://github.com/orbi-build/orbi/pull/4"
        ),
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, expected_url=FAKE_PR_URL)
    assert "pr_url_mismatch" in caplog.text


def test_verify_pr_skips_url_check_when_expected_url_not_given(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_skips_latest_base_check_when_not_required(
    monkeypatch, tmp_path,
):
    """The resume pre-validation runs before the base merge, when being
    behind the latest base is the expected state: no fetch, no ancestry
    check."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, require_latest_base=False) == FAKE_PR_URL
    assert not any(c[:3] == ["git", "fetch", "origin"] for c in calls)
    assert not any(
        c[:3] == ["git", "merge-base", "--is-ancestor"] for c in calls
    )


def test_fake_pr_view_rejects_unexpected_commands(monkeypatch):
    seen, fake_run = fake_pr_view(monkeypatch, "OPEN")
    assert seen == []
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_pr_delivery_status_rejects_non_array_check_rollup(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({"state": "OPEN", "statusCheckRollup": {}}),
    )
    with pytest.raises(ValueError, match="statusCheckRollup must be a JSON array"):
        runner.pr_delivery_status(PR_URL, "owner/repo")


def test_pr_delivery_status_ignores_malformed_check_entry(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({
            "state": "OPEN", "statusCheckRollup": [None],
        }),
    )
    assert runner.pr_delivery_status(PR_URL, "owner/repo") == ("OPEN", [])


def test_finish_progress_blocked_is_a_noop_without_run_id(monkeypatch):
    """Without a bound run id there is no tracked comment to update
    (the failure comment simply carries no marker)."""
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )
    runner._finish_progress(
        39, None, "owner/repo", None, None, "https://x/pull/46",
        "failure", "next step", title="Blocked task", outcome="blocked", language_code="en",
    )
    assert calls == []


def test_finish_progress_blocked_creates_the_comment_when_missing(
    monkeypatch,
):
    """A run that never reached a progress comment (the runner died
    before `ensure`) still ends with the blocked scene: the comment is
    created with it."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        # Only the progress comment POST reaches the api here.
        assert command[command.index("--method") + 1] == "POST"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in body
    assert "the failure" in body
    assert "the next step" in body
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    # Issue #100: the blocked scene shows the number AND the title.
    assert "- issue: #39 Blocked task" in body


def test_finish_progress_carries_the_actual_role_and_round(
    monkeypatch,
):
    """The terminal scene must show the role and the review/fix round
    the run was actually in, not the hardcoded `fix`/`0` (review round
    2, PR #42)."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        assert command[command.index("--method") + 1] == "POST"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", role=runner.ROLE_REVIEW, review_round=2, language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 2" not in body


def test_finish_progress_defaults_to_review_round_zero(monkeypatch):
    """Without explicit role/round the default is the only post-PR role
    (Issue #82: the review session fixes findings in the same session,
    so a blocked delivery is always a review failure)."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 0" not in body


def test_finish_progress_renders_the_fix_needed_scene(monkeypatch):
    """Issue #293: one function serves both terminal scenes — the
    fix-needed scene (Issue #50) renders the same tracked comment with
    the fix-needed outcome and its next step."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure",
        "the next tick resumes the same run, branch, worktree and PR "
        "automatically (the Issue stays ai-fix-needed)",
        title="Fix task", outcome="fix needed", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "Orbi fix needed" in body
    assert "What happened: Orbi found a problem" in body
    assert "What you need to do: Nothing" in body
    assert ("What Orbi will do next: the next tick resumes the same run, "
            "branch, worktree and PR automatically (the Issue stays "
            "ai-fix-needed)") in body
    assert "<details><summary>Raw error</summary>\nthe failure\n</details>" in body
    # The role default is the only post-PR role (Issue #82).
    assert "- role: review" in body
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "- issue: #39 Fix task" in body


def test_delivery_step_returns_when_pr_merged(monkeypatch, caplog):
    seen, _ = fake_pr_view(monkeypatch, "MERGED")
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    assert len(seen) == 1
    assert "delivery_merged" in caplog.text
    assert f"pr={PR_URL}" in caplog.text


def test_delivery_step_runs_one_review_per_tick(
        monkeypatch, tmp_path,
):
    """Issue #788: one tick, ONE review round, ONE PR state read. The old
    wait loop re-polled and re-reviewed in-process until merge; the step
    returns after the round and the next tick's resume scan continues."""
    calls = {"pr": 0, "labels": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            calls["pr"] += 1
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            calls["labels"] += 1
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # Awaiting review triggers the independent review (Issue #34); the
    # mock reports findings so the next tick runs the next round.
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # Exactly ONE PR state read and ONE review round — the step returns
    # afterwards (no loop, no second poll, no sleep).
    assert calls == {"pr": 1, "labels": 1}
    assert len(reviews) == 1
    # The review ran on the derived worktree/branch of the same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_delivery_step_defers_when_ci_pending(
        monkeypatch, tmp_path, caplog,
):
    """Issue #788: pending CI on the PR head defers the whole delivery to
    the next tick — one journal line, NO review session, NO label read,
    NO label change, no sleep. "pending" is a state, not a wait."""
    calls = {"pr": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            calls["pr"] += 1
            return json.dumps({
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "tests", "status": "IN_PROGRESS",
                     "conclusion": None},
                ],
            })
        # The defer path performs exactly ONE PR read: no Issue read
        # (comments or labels), no mutation — the fake rejects the rest.
        raise AssertionError(f"unexpected command: {command}")

    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "issue", "view"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # One PR read, one journal line, then return: no review, no label
    # read, no mutation.
    assert calls == {"pr": 1}
    assert reviews == []
    assert edits == []
    assert "delivery_ci_pending" in caplog.text


def test_delivery_step_auto_merges_on_clean_review(
        monkeypatch, caplog, tmp_path,
):
    """A clean independent verdict merges the PR itself (Issue #34): the
    wait returns as soon as the review reports the merge, without any
    further polling."""
    states = ["OPEN"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            pr_calls["n"] += 1
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: True,
    )
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # One OPEN poll, one review, then the merge is terminal: no second
    # PR state poll.
    assert pr_calls["n"] == 1
    assert "delivery_auto_merged" in caplog.text


def test_delivery_step_passes_p0_priority_to_the_review(
        monkeypatch, caplog, tmp_path,
):
    """Issue #101: a P0 delivery in an opened-PR state (the resumable
    scan fetches `labels`) keeps its priority end to end: the awaiting
    log line carries `priority=p0` and the independent review receives
    it, so the review/merge progress comment shows `p0` too."""
    states = ["OPEN"]
    review_calls = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [
                {"name": "ai-awaiting-merge"}, {"name": "p0"},
            ]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(seam, "run_command", fake_run)

    def fake_review(*args, **kwargs):
        review_calls.append(kwargs)
        return True

    monkeypatch.setattr(review_merge, "review_and_merge_if_clean", fake_review)
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {
        "number": 39, "title": "p0 task", "body": "",
        "labels": [{"name": "ai-awaiting-merge"}, {"name": "p0"}],
    }
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # The review receives the priority, the recovered scene (the round
    # budget reads the scene, Issue #788) and the PR head's check rollup
    # (Issue #1613: the tests field comes from CI, never a local log).
    assert review_calls == [{
        "title": "p0 task", "priority": "p0",
        "scene": {
            "run_id": "a1b2c3d4", "base_branch": "main",
            "base_sha": "abc123def456", "pr_url": PR_URL,
            "external": "", "review_round": 0, "scene_at": None,
        },
        "checks": [],
        "merge_only": True,
    }]
    # The awaiting log line carries the explicit priority field.
    awaiting = [m for m in caplog.messages if "delivery_awaiting" in m]
    assert any("priority=p0" in m for m in awaiting)


def test_delivery_step_marks_blocked_when_review_fails(
        monkeypatch, caplog, tmp_path,
):
    """A review that cannot run (unrecoverable scene, missing worktree,
    malformed verdict, exhausted rounds) is a real failure: the Issue is
    marked ai-blocked with a failure comment, and the wait returns so
    the slot is released (the Issue is never stranded awaiting review).
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # No trusted `Orbi opened PR:` comment: the scene
            # cannot be recovered. The trusted review-round comments
            # still count for the blocked scene's round field (review
            # round 2, PR #42).
            return json.dumps({"comments": [
                {"body": "public comment", "authorAssociation": "NONE"},
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        return json.dumps({"labels": [{"name": "ai-pr-opened"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("the independent review of" in body for body in posted_bodies),
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "the independent review of" in blocked
    assert "What Orbi will do next:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (the failure happened
    # during the independent review) and the completed review rounds
    # (review round 2, PR #42) — not the hardcoded fix/0.
    assert "- role: review" in blocked
    assert "- review/fix round: 2" not in blocked
    # Issue #1724: a review blocked on the missing trusted scene names
    # ONE working resume path — restore the scene comment, then the
    # shared unblock step — never the "or relabel the Issue" alternative
    # that does not work on its own.
    assert "must restore the trusted 'Orbi opened PR' scene comment" in body
    assert "remove `ai-blocked`, then add `ai-fix-needed`" in body
    assert "or relabel" not in body


def test_delivery_step_marks_blocked_when_review_fails_while_fix_needed(
        monkeypatch, caplog, tmp_path,
):
    """A review failure while the Issue is `ai-fix-needed` (awaiting the
    next review session) must leave the terminal state `ai-blocked`
    ALONE: this PR routes both opened-PR states into the same review, so
    the leftover `ai-fix-needed` label is removed too (Issue #82).
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # No trusted `Orbi opened PR:` comment: the scene
            # cannot be recovered, so the review fails fast.
            return json.dumps({"comments": [
                {"body": "public comment", "authorAssociation": "NONE"},
            ]})
        # The delivery is in the `ai-fix-needed` state (awaiting the
        # next review session).
        return json.dumps({"labels": [{"name": "ai-fix-needed"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # The Issue is marked ai-blocked; the blocked patch clears every
    # delivery-state label that is present — here only `ai-fix-needed`
    # (the delivery was awaiting the next review session) — so the
    # terminal state is ai-blocked alone (never ai-blocked + ai-fix-needed).
    # One deterministic patch, not two hardcoded removes.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-fix-needed",
    }
    assert len(edits) == 1
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone AND the
    # tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("the independent review of" in body for body in posted_bodies),
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "the independent review of" in blocked
    assert "- role: review" in blocked


def test_delivery_step_blocks_when_scene_base_differs_from_config(
        monkeypatch, caplog, tmp_path,
):
    """Issue #91: the scene freezes the base the PR was opened against.
    When the configured base differs (config changed, or the comment is
    stale), the resume must fail fast BEFORE any git/Pi mutation: no
    review is started, nothing is merged, and the Issue is marked
    ai-blocked with BOTH base values named — never silently switch to
    the configured base for a PR frozen on another one.
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    states = ["OPEN", "MERGED"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            pr_calls["n"] += 1
            return json.dumps({"state": states[pr_calls["n"] - 1]})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # The trusted scene freezes base_branch=develop.
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=develop "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        return json.dumps({"labels": [{"name": "ai-pr-opened"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the base mismatch is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    # The configured base is main; the scene froze develop.
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started and nothing was merged.
    assert reviews == []
    # The mismatch is terminal on the FIRST poll: the wait returns
    # instead of re-checking the PR state.
    assert pr_calls["n"] == 1
    # The Issue is marked ai-blocked (removing ai-pr-opened) ...
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    # ... with a failure comment that names BOTH base values and carries
    # the run marker.
    body = comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "base_branch=develop" in body
    assert "base_branch=main" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any("- result:" in body for body in posted_bodies)
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "base_branch=develop" in blocked
    assert "base_branch=main" in blocked
    assert "What Orbi will do next:" in blocked


def test_delivery_step_worktree_missing_stays_fix_needed(
        monkeypatch, caplog, tmp_path,
):
    """Issue #90 + #50: the resume derives the worktree from the repo,
    Issue and run id (never from a comment). When that directory does
    not exist the delivery must fail fast BEFORE any git/Pi mutation:
    no review is started, no command is spawned against the missing
    scene — but the failure is RECOVERABLE (the branch still exists on
    the remote and the worktree can be recreated on the next resume):
    the Issue is marked ai-fix-needed (never ai-blocked) with the PR
    and branch preserved (the pre-#82 `resume_delivery` fail-fast,
    restored)."""
    api_calls: list = []
    pr_comments: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        # The fake rejects anything that is not a pr/issue view, the
        # PR failure comment or the progress API: a missing worktree
        # must not spawn git/gh.
        if command[:2] == ["gh", "pr"]:
            if command[2] == "comment":
                pr_comments.append(command[-1])
                return ""
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # The trusted scene derives run_id=a1b2c3d4; the derived
            # worktree does not exist under tmp_path.
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    # The fake rejects anything that is not a pr/issue view or the
    # progress API.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "fetch"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the missing worktree is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started and nothing was merged.
    assert reviews == []
    # The Issue is marked ai-fix-needed (removing ai-pr-opened) —
    # never ai-blocked (Issue #50) ...
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
        "remove": "ai-pr-opened",
    }
    # ... and it is the only label edit.
    assert len(edits) == 1
    # The failure comment names the missing worktree and carries the
    # run marker.
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body
    # The full scene carries the ACTUAL branch (derived before the
    # worktree check, Issue #50) — never a `branch=None` placeholder.
    assert "branch=orbi/owner-repo-issue-39" in body
    assert "branch=None" not in body
    assert "delivery_review_failed" in caplog.text
    # The failure comment is written to the Issue AND the PR
    # (Issue #50); the PR copy is the same formatted comment and
    # additionally carries the hidden runner fingerprint (Issue #526).
    assert pr_comments[0].startswith("<!-- orbi:run=a1b2c3d4 -->")
    assert pr_comments[0].endswith("<!-- runner=8a12fb1c -->")
    # The recoverable failure posts the fix-needed milestone (Issue
    # #18) AND the tracked progress comment becomes the fix-needed
    # scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted_bodies)
    # The milestone names the failing scene through the same bounded
    # summary as the failure comment — the missing worktree is still
    # named (a local runner path is redacted where the platform naming
    # allows it) — and it carries the machine-readable record too
    # (Issue #1322: the milestone is a failure comment).
    assert any(
        "worktree missing:" in body and
        "<!-- orbi:failure:v1 " in body
        for body in posted_bodies
    )
    assert not any(
        "Orbi: blocked" in body for body in posted_bodies
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the fix-needed scene in place.
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    fix_needed = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi fix needed" in fix_needed
    # Issue #1721: the "What happened" sentence keeps the failure, but the
    # worktree path on the runner's disk is redacted; the branch (the
    # public name) still says which worktree is missing. pytest's tmp_path
    # sits under a covered prefix on Ubuntu CI (/tmp) but not on hosted
    # macOS (/private/var — the gap Issue #1721 records as out of scope),
    # so the expectation is derived with the same helper instead of
    # hard-coding the redacted form.
    missing_worktree = gitops.worktree_path(tmp_path, "owner/repo", 39, "a1b2c3d4")
    assert progress._redact_local_paths(
        f"worktree missing: {missing_worktree}",
    ) in fix_needed
    assert "branch: orbi/owner-repo-issue-39" in fix_needed
    assert "What Orbi will do next:" in fix_needed


def test_delivery_step_worktree_missing_while_fix_needed_keeps_label(
        monkeypatch, tmp_path,
):
    """Issue #90 + #82 + #50: a missing resume worktree while the
    Issue is `ai-fix-needed` (awaiting the next review session) keeps
    the `ai-fix-needed` label (the opened-PR state label is removed,
    the fix-needed label is the one the next tick scans for) and no
    review is started."""
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            if command[2] == "comment":
                return ""
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        # The delivery is in the `ai-fix-needed` state (awaiting the
        # next review session).
        return json.dumps({"labels": [{"name": "ai-fix-needed"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started.
    assert reviews == []
    # The Issue already carries only ai-fix-needed, so no absent label is
    # removed; the state remains in the automatic fix loop.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
    }
    assert len(edits) == 1
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body


def test_delivery_step_runs_review_when_fix_needed(
    monkeypatch, caplog, tmp_path,
):
    """A fix-needed delivery runs the SAME independent review as an
    awaiting-review delivery (Issue #82: the review session fixes
    findings in the same session — no cold-start fixer). Issue #788: the
    step runs the round ONCE and returns; the next tick resumes."""

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "labels":
                return json.dumps({
                    "labels": [{"name": "ai-fix-needed"}],
                })
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # The independent review runs for the fix-needed state too (Issue
    # #82) and reports findings; the next tick runs the next round.
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": "stale body"}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # ONE review round per tick (Issue #788): the step returns after it.
    assert len(reviews) == 1
    # No fixer: the review ran on the derived worktree/branch of the
    # same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39


def test_delivery_step_marks_blocked_when_pr_closed_unmerged(
    monkeypatch, caplog,
):
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"]:
            # The blocked scene derives the role from the delivery label
            # and the round from the trusted review-round comments
            # (review round 2, PR #42): no more hardcoded fix/0.
            if command[-1] == "labels":
                return json.dumps({"labels": [{"name": "ai-fix-needed"}]})
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        api_calls.append(command)
        if "--method" not in command:
            # The run's live progress comment exists.
            return json.dumps([existing])
        method = command[command.index("--method") + 1]
        if method == "POST":
            body = command[command.index("--field") + 1]
            return json.dumps({"id": 78, "body": body[len("body="):],
                               "url": "https://x/78"})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    # The Issue is marked ai-blocked; the blocked patch clears every
    # delivery-state label that is present — here only `ai-fix-needed`
    # (the delivery was awaiting the next review session) — so the
    # terminal state is ai-blocked alone. One deterministic patch,
    # not two hardcoded removes.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-fix-needed",
    }
    assert len(edits) == 1
    # ... with a failure comment carrying the run marker and run_id and,
    # from the ONE blocked exit, the community help line last (Issue #1730).
    body = comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    help_line = language.text("en", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1
    assert f"pr={PR_URL}" in caplog.text
    assert "delivery_closed_unmerged" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("closed without a merge" in body for body in posted_bodies),
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "closed without a merge" in blocked
    assert "What Orbi will do next:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (Issue #82: both
    # opened-PR states are review states, so always `review`) and the
    # completed review rounds (review round 2, PR #42).
    assert "- role: review" in blocked
    assert "- review/fix round: 2" not in blocked


def test_delivery_step_pr_closed_comment_follows_the_language(monkeypatch):
    """Issue #1730: the PR-closed blocked comment ends with the community
    help line in the run's bound language — the zh scene goes through the
    same blocked exit as the en one."""
    def fake_run(command, **kwargs):
        # Only the two reads this scene makes; a third command fails fast
        # with a KeyError instead of being answered by a catch-all arm.
        answers = {
            ("gh", "pr", "view"): {"state": "CLOSED"},
            ("gh", "issue", "view"): {"labels": [{"name": "ai-fix-needed"}]},
        }
        return json.dumps(answers[tuple(command[:3])])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "_CURRENT_RUN_ID", None)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, **kwargs: comments.append(kwargs))
    monkeypatch.setattr(seam, "apply_label_patch",
        lambda number, **kwargs: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(message_language="zh"),
        "owner/repo",
    )
    body = comments[0]["body"]
    help_line = language.text("zh", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1
    assert language.text(
        "zh", "runner.pr_closed_comment", pr_url=PR_URL,
    ) in body


def test_delivery_step_review_failure_without_bound_run_id(
        monkeypatch, tmp_path,
):
    """When no run id is bound the review-failure comment simply carries
    no marker (the Issue is still marked ai-blocked)."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": []})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert "orbi:run=" not in body


def test_delivery_step_repairs_in_progress_label_and_logs_ci(
        monkeypatch, caplog, tmp_path,
):
    """An open PR proves implementation reached delivery: repair a lost
    label transition and expose CI state before entering review."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "tests", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                    {"name": "lint", "status": "COMPLETED",
                     "conclusion": "SKIPPED"},
                ],
            })
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [{
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                }]})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    monkeypatch.setattr(review_merge, "review_and_merge_if_clean", lambda *a, **k: True)
    (tmp_path / ".worktrees" /
     "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert any("--add-label" in call and "ai-pr-opened" in call
               and "--remove-label" in call and "ai-in-progress" in call
               for call in calls)
    assert "tests=COMPLETED/SUCCESS" in caplog.text
    assert "lint=COMPLETED/SKIPPED" in caplog.text


def test_delivery_step_blocks_when_in_progress_label_repair_fails(
        monkeypatch, caplog,
):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        return json.dumps({"labels": [{"name": "ai-in-progress"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    patches = []
    def fake_patch(number, **kwargs):
        patches.append(kwargs)
        if kwargs["event"] == runner.EVENT_PR_OPENED:
            raise RuntimeError("label API unavailable")
    monkeypatch.setattr(seam, "apply_label_patch", fake_patch)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append(kwargs))
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(), "owner/repo",
    )
    assert patches[-1]["event"] == EVENT_BLOCKED
    assert "delivery_label_repair_failed" in caplog.text
    # The blocked comment ends with the community help line (Issue #1730).
    help_line = language.text("en", "help.community")
    assert comments[0]["body"].endswith(f"\n\n{help_line}")
    assert comments[0]["body"].count(help_line) == 1


def test_delivery_step_keeps_holding_when_no_delivery_label(
        monkeypatch, caplog,
):
    """An OPEN PR whose Issue carries no delivery state label (neither
    awaiting review nor fix-needed) is simply re-checked: the slot stays
    held and no review or fix is started."""
    states = ["OPEN", "MERGED"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            pr_calls["n"] += 1
            return json.dumps({"state": states[pr_calls["n"] - 1]})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "edit"]:
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    issue = {"number": 39, "title": "task", "body": ""}
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    # An open PR with no resumable delivery label is unrecoverable: it is
    # blocked immediately instead of retaining the slot indefinitely.
    assert pr_calls["n"] == 1
    assert "delivery_label_inconsistent" in caplog.text


def test_delivery_step_label_repair_failure_comment_follows_the_language(
        monkeypatch, caplog,
):
    """Issue #1660: the terminal "labels could not be repaired" comment is
    a Runner-owned failure comment, so its prose follows the bound
    language while the label, the PR URL and the machine fields stay
    verbatim."""
    def fake_run(command, **kwargs):
        # Dispatch on the joined argv: the same fake answers both reads
        # and the writes, so no command-shape assertion is added.
        shape = " ".join(str(part) for part in command)
        if shape.startswith("gh pr view "):
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if shape.startswith("gh issue view "):
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append(kwargs),
    )
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            message_language="zh",
        ),
        "owner/repo",
    )
    assert "delivery_label_inconsistent" in caplog.text
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "Orbi 失败：PR" in body
    assert "ai-blocked" in body
    assert PR_URL in body
    # The blocked comment ends with the language's help line (#1730).
    help_line = language.text("zh", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1


def test_delivery_step_logs_awaiting_without_bound_run_id(monkeypatch, caplog):
    """When no run id is bound the wait still works: the failure comment
    simply carries no marker."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"] and command[-1] == "labels":
            # No leftover fix-needed label: only ai-pr-opened is removed.
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "t", "body": ""},
        config_domain.RunnerConfig(), "owner/repo",
    )
    assert "Orbi failed:" in comments[0][1]["body"]
    assert "orbi:run=" not in comments[0][1]["body"]


def test_run_review_round_returns_true_when_review_merges(
        monkeypatch, tmp_path,
):
    """Issue #289: True = the round merged the PR; the caller releases
    the slot. No label edit and no failure comment on this path."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, review_result=True,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is True
    # One review on the frozen scene of the same run.
    assert len(reviews) == 1
    worktree, branch, base_branch, review_config, repo, number = \
        reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39
    assert edits == []
    assert comments == []


def test_run_review_round_returns_false_on_findings(
        monkeypatch, tmp_path,
):
    """Issue #289: False = findings remain (the ai-fix-needed
    transition happened inside the review itself); the caller yields
    the cadence and polls the next round."""
    edits, comments, reviews, _ = _review_round_env(monkeypatch, tmp_path)
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is False
    assert len(reviews) == 1
    assert edits == []
    assert comments == []


def test_run_review_round_passes_prior_comments_after_first_round(
        monkeypatch, tmp_path,
):
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_round=1,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    ) is False
    assert "previous_comments" in reviews[0][1]
    assert reviews[0][1]["previous_comments"]


def _recovered_review_round_env(monkeypatch, tmp_path):
    """Issue #1779: a maintainer recovery AFTER a five-round scene.

    Restores the REAL review loop (the shared env stubs it), freezes the
    PR at an already-recorded engine push, and returns the env captures
    plus `rounds`, the `review_round` every finish scene is built with.
    """
    from tests.fakes.progress import capture_finish_rounds

    real_round = review_merge.review_and_merge_if_clean
    real_publish = progress._safe_publish
    edits, comments, reviews, publishes = _review_round_env(
        monkeypatch, tmp_path, scene_round=review_merge.MAX_REVIEW_ROUNDS,
    )
    monkeypatch.setattr(seam, "review_and_merge_if_clean", real_round)

    def fake_run_review(*args, **kwargs):
        reviews.append((args, kwargs))
        raise RuntimeError("review session died")

    monkeypatch.setattr(seam, "run_review", fake_run_review)
    # The bypass stub hides the built scene; the real one runs it.
    monkeypatch.setattr(seam, "_safe_publish", real_publish)
    monkeypatch.setattr(seam, "human_review_recovery_at",
                        lambda *a, **k: "2026-02-01T00:00:00Z")
    monkeypatch.setattr(seam, "pr_view",
                        lambda *a, **k: {"mergeable": "MERGEABLE"})
    frozen = {"number": 46, "url": PR_URL, "base_ref": "main",
              "base_oid": "abc", "head_ref": "b", "head_oid": "def"}
    monkeypatch.setattr(seam, "freeze_pr", lambda *a, **k: frozen)
    worktree = (tmp_path / ".worktrees"
                / "orbi-owner-repo-issue-39-a1b2c3d4")
    run_state.write_run_state(RunContext(
        run_id=FAKE_RUN_ID, issue=39, branch="orbi/owner-repo-issue-39",
        worktree=worktree, source_repo="owner/repo",
    ))
    run_state.record_pushed_head(worktree, "def")
    return edits, comments, reviews, publishes, capture_finish_rounds(
        monkeypatch)


def test_run_review_round_reports_the_round_the_review_used(
        monkeypatch, tmp_path,
):
    """Issue #1779 acceptance 1: five completed rounds, a recovery after
    the scene, and a failure before the next round comment. The review
    runs round 1 and the published failure scene says round 1 — one
    number, never the exhausted 5."""
    _edits, _comments, reviews, _publishes, rounds = \
        _recovered_review_round_env(monkeypatch, tmp_path)
    assert review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is None
    # The round the review loop used ...
    assert reviews[0][0][3] == 1
    # ... and the round the failure report published.
    assert rounds == [1]


def test_run_review_round_reports_the_recovered_round_on_a_freeze_failure(
        monkeypatch, tmp_path,
):
    """Issue #1779 acceptance 1 with the freeze itself failing: the round
    is already decided, so the failure scene reports round 1, not 5."""
    _edits, _comments, _reviews, _publishes, rounds = \
        _recovered_review_round_env(monkeypatch, tmp_path)

    def freeze_fails(*args, **kwargs):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(seam, "freeze_pr", freeze_fails)
    assert review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is None
    assert rounds == [1]


def test_run_review_round_reports_five_when_no_recovery_happened(
        monkeypatch, tmp_path,
):
    """Issue #1779 acceptance 2: five completed rounds and NO recovery —
    the review never starts and the failure scene still reports 5."""
    from tests.fakes.progress import capture_finish_rounds

    real_round = review_merge.review_and_merge_if_clean
    real_publish = progress._safe_publish
    _review_round_env(
        monkeypatch, tmp_path, scene_round=review_merge.MAX_REVIEW_ROUNDS,
    )
    monkeypatch.setattr(seam, "review_and_merge_if_clean", real_round)
    monkeypatch.setattr(seam, "_safe_publish", real_publish)
    monkeypatch.setattr(seam, "human_review_recovery_at",
                        lambda *a, **k: None)
    rounds = capture_finish_rounds(monkeypatch)
    assert review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is None
    assert rounds == [5]


def _executing_review_round_env(monkeypatch, tmp_path, *, scene_round=2):
    """Issue #1783: the REAL loop writes round comments through the
    shared comment store, so a failure report after those writes reads
    the scene the round just advanced."""
    from types import SimpleNamespace

    from tests.fakes.progress import capture_finish_rounds

    real_round = review_merge.review_and_merge_if_clean
    real_publish = progress._safe_publish
    _review_round_env(monkeypatch, tmp_path, scene_round=scene_round)
    monkeypatch.setattr(seam, "review_and_merge_if_clean", real_round)
    monkeypatch.setattr(seam, "_safe_publish", real_publish)
    worktree = (tmp_path / ".worktrees"
                / "orbi-owner-repo-issue-39-a1b2c3d4")
    run_state.write_run_state(RunContext(
        run_id=FAKE_RUN_ID, issue=39, branch="orbi/owner-repo-issue-39",
        worktree=worktree, source_repo="owner/repo",
    ))
    run_state.record_pushed_head(worktree, "def")
    store = [{
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n"
            "<!-- orbi:scene:v1 " + json.dumps({
                "base_branch": "main", "base_sha": "abc123def456",
                "external": "", "pr_url": PR_URL,
                "review_round": scene_round, "run_id": "a1b2c3d4",
                "schema": 1,
            }) + " -->\n"
            f"Orbi opened PR: {PR_URL} (base_branch=main "
            "base_sha=abc123def456 run_id=a1b2c3d4)"
        ),
        "authorAssociation": "OWNER",
    }]

    def fake_run(command, **kwargs):
        if command[-1] == "comments":
            return json.dumps({"comments": store})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)

    def comment_issue(number, *, repo, body):
        store.append({"body": body, "authorAssociation": "OWNER"})

    monkeypatch.setattr(seam, "comment_issue", comment_issue)
    pr_posts: list[str] = []

    def comment_pr(number, *, repo, body):
        pr_posts.append(body)
        if "<!-- orbi:comment=failure -->" not in body:
            raise RuntimeError("PR round comment post failed")

    monkeypatch.setattr(seam, "comment_pr", comment_pr)
    monkeypatch.setattr(seam, "pr_view",
                        lambda *a, **k: {"mergeable": "MERGEABLE"})
    frozen = {"number": 46, "url": PR_URL, "base_ref": "main",
              "base_oid": "abc", "head_ref": "b", "head_oid": "def"}
    monkeypatch.setattr(seam, "freeze_pr", lambda *a, **k: frozen)
    monkeypatch.setattr(
        seam, "run_review",
        lambda *a, **k: SimpleNamespace(
            output=(
                'REVIEW_VERDICT {"verdict": "findings", "head": "def", '
                '"blockers": 0, "majors": 1, "minors": 0, "findings": '
                '[{"level": "Major", "note": "n", "fix": ""}]}'
            ),
            issue_body_sha256=None,
        ),
    )
    return store, pr_posts, capture_finish_rounds(monkeypatch)


def test_run_review_round_reports_the_round_it_ran_after_its_comment(
        monkeypatch, tmp_path,
):
    """Issue #1783 acceptance 1: two completed rounds, round 3 writes its
    Issue round comment and then the PR round comment post fails. The
    failure scene reports the round that ran (3), never the scene-derived
    next round (4)."""
    store, pr_posts, rounds = _executing_review_round_env(
        monkeypatch, tmp_path, scene_round=2,
    )
    assert review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is None
    # The loop ran round 3 and its Issue round comment advanced the scene.
    assert '"review_round": 3' in store[1]["body"]
    # The failed round post and the failure report's own PR comment.
    assert len(pr_posts) == 2
    assert rounds == [3]


def test_run_review_round_reports_the_round_it_ran_on_a_freeze_failure(
        monkeypatch, tmp_path,
):
    """Issue #1783 acceptance 2: round 3 fails in freeze_pr before any
    comment; it still reports round 3."""
    _store, _pr_posts, rounds = _executing_review_round_env(
        monkeypatch, tmp_path, scene_round=2,
    )

    def freeze_fails(*args, **kwargs):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(seam, "freeze_pr", freeze_fails)
    assert review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is None
    assert rounds == [3]


def test_run_review_round_passes_a_recorded_pass(monkeypatch, tmp_path):
    """Issue #1626: a recorded pass with the same rendered Issue body is
    passed to the round, which merges without a session."""
    prompt = tmp_path / "prompt_review.md"
    prompt.write_text("Review.\n{{ISSUE_BODY}}\n", encoding="utf-8")
    body = "the issue body"
    body_sha = pi_session.issue_body_sha256(body)
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, record=("h1", body_sha),
    )
    issue = {"number": 39, "title": "task", "body": body}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", prompt_review=prompt,
        ),
        "owner/repo",
    )
    assert outcome is False
    assert reviews[0][1]["review_record"] == ("h1", body_sha)
    assert reviews[0][1]["issue_body_sha256"] == body_sha


def test_run_review_round_skips_the_record_for_an_external_takeover(
        monkeypatch, tmp_path):
    """Issue #1626: an external takeover is never merge-only, so the
    record is not passed."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_round=1, external="true",
        record=("h1", "b" * 64),
    )
    issue = {"number": 39, "title": "task", "body": "body"}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is False
    assert "review_record" not in reviews[0][1]


def test_run_review_round_marks_awaiting_merge_as_merge_only(
        monkeypatch, tmp_path):
    """Issue #1216/#1626: the ai-awaiting-merge retry stays merge_only."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, label="ai-awaiting-merge",
    )
    issue = {"number": 39, "title": "task", "body": "body"}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is False
    assert reviews[0][1]["merge_only"] is True


def test_run_review_round_returns_none_when_scene_base_differs(
        monkeypatch, tmp_path,
):
    """Issue #289: None = terminal state handled. An unrecoverable
    precondition (the scene froze another base, Issue #91) marks the
    Issue ai-blocked with the failure comment; the review never runs
    and the caller releases the slot."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_base="develop",
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    assert "Orbi failed:" in comments[0][1]["body"]
    assert "base_branch=develop" in comments[0][1]["body"]
    assert "base_branch=main" in comments[0][1]["body"]


def test_run_review_round_returns_none_when_worktree_missing(
        monkeypatch, tmp_path,
):
    """Issue #289: None also covers the RECOVERABLE failure (the
    missing worktree, Issue #90): the round writes the ai-fix-needed
    full scene (the next tick resumes) and the caller releases the
    slot — the failure never keeps re-running inside one process."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, with_worktree=False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body


def test_run_review_round_failure_comment_follows_the_config_language(
        monkeypatch, tmp_path,
):
    """Issue #1687 acceptance 1: a delivery that fails in review posts its
    blocked comment in the bound message language. The review path used to
    call the failure reporter without a language, so a zh host still got an
    English blocked comment on its most common failure."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_base="develop",
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language="zh",
        ),
        "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1]["add"] == "ai-blocked"
    body = comments[0][1]["body"]
    assert "Orbi：已阻塞 —— 等待人工决定" in body
    assert "**原因：**" in body
    assert "Orbi failed:" not in body


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_rounds_exhausted_says_how_to_resume(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 1: the exhausted round budget names the PR,
    says the review used all its rounds without a mergeable result (no raw
    exception text), and tells the human to remove `ai-blocked` before
    adding `ai-fix-needed`, which buys a fresh review-round budget."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.ReviewRoundsExhausted("raw budget line"),
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    assert action.index("`ai-blocked`") < action.index("`ai-fix-needed`")
    assert text["rounds_action"] in action
    assert text["rounds_reason"] in reason
    assert PR_URL in reason
    assert "raw budget line" not in reason


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_human_decision_keeps_the_reviewer_fix(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 2: with a reviewer fix the action is the
    reviewer's own text plus the resume step and no budget clause — the
    budget resets only after all rounds are used."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.HumanDecisionRequired(
            "review requires human decision: note: same failure twice; "
            "fix: choose the authoritative address source",
            action="Choose the authoritative address source.",
        ),
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    # The reviewer's own text is kept verbatim; only its trailing
    # punctuation is dropped so the step sentence does not double it.
    assert "Choose the authoritative address source" in action
    assert "source.。" not in action
    assert text["step"] in action
    assert text["budget"] not in action
    assert text["human_reason"] in reason
    assert PR_URL in reason
    assert "same failure twice" not in reason


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_human_decision_without_fix_uses_generic_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 2: the reviewer gave no fix — the handler
    supplies the generic catalog action instead of an English literal."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.HumanDecisionRequired(
            "review requires human decision: note: decide the base",
            action="",
        ),
    )
    action = _blocked_line(body, text["action"])
    assert text["generic_action"] in action
    assert text["step"] in action
    assert text["close"] in action
    assert "Decide how this PR should proceed." not in body


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_base_mismatch_gets_the_generic_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 3: a base mismatch has no case-specific
    remedy (the base check reads the scene comment, not the PR) — it gets
    the generic action in the language while the reason still names both
    base values."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code, scene_base="develop",
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    assert text["generic_action"] in action
    assert text["close"] in action
    assert "base_branch=develop" in reason
    assert "base_branch=main" in reason
    assert language.text(code, "failure.headline_blocked") in body


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_recoverable_failure_keeps_no_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 4: a recoverable review failure stays
    ai-fix-needed with an empty action — the next tick resumes it."""
    text = BLOCKED_REVIEW_TEXT[code]
    edits, comments, _, _ = _review_round_env(
        monkeypatch, tmp_path, with_worktree=False,
    )
    outcome = review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language=code,
        ),
        "owner/repo",
    )
    assert outcome is None
    assert edits[0][1]["add"] == "ai-fix-needed"
    body = comments[0][1]["body"]
    assert text["action"] not in body
    assert text["reason"] in body
    assert "worktree missing" in body


def test_fake_deliver_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_deliver_run(["gh", "release", "list"])


def test_deliver_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command[:3] == ["git", "branch", "--show-current"], command
        return "other-branch"

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", FAKE_HEAD_SHA, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_uncommitted_changes(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M leaked.py\n?? junk.txt"
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(runner.RecoverablePiFailure, match="uncommitted changes"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_delivery_without_a_commit(monkeypatch, tmp_path):
    # HEAD is still the frozen base: the agent delivered no commit.
    base_sha = "9" * 40

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return base_sha
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="no commit"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", base_sha, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_an_empty_commit_as_no_file_change(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1581: HEAD advanced past the frozen base, but the delivered
    tree equals the base tree (an empty commit). The commit boundary logs
    delivery_no_file_change and raises before any fetch, push or PR — the
    same terminal path as delivery_no_commit, so the run ends ai-blocked
    with the last commit subject in the reason."""
    base_sha = "9" * 40
    subject = (
        "Blocked: home page (index.html) is committed empty — "
        "no page to measure"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["git", "rev-parse", "HEAD^{tree}"]:
            return FAKE_BASE_TREE_SHA
        if command[0:3] == ["git", "rev-parse", f"{base_sha}^{{tree}}"]:
            return FAKE_BASE_TREE_SHA
        if command[0:4] == ["git", "log", "-1", "--format=%s"]:
            return subject
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="no file change") as failure:
            runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", base_sha, issue_title="t", repo_dir=tmp_path, language_code="en")
    # The reason names both SHAs and quotes the last commit subject.
    assert FAKE_HEAD_SHA in str(failure.value)
    assert base_sha in str(failure.value)
    assert subject in str(failure.value)
    # The event carries the branch/head/base scene.
    assert "delivery_no_file_change" in caplog.text
    assert f"head={FAKE_HEAD_SHA}" in caplog.text
    assert f"base={base_sha}" in caplog.text
    # Nothing was pushed and no PR was touched: the stop is terminal.
    assert not any(
        command[0:2] == ["git", "push"] for command in calls
    )
    assert not any(command[0:2] == ["gh", "pr"] for command in calls)
    assert ["git", "fetch", "origin", "main"] not in calls


def test_deliver_pr_completes_the_closeout(monkeypatch, tmp_path):
    """The happy path: clean commit boundary, base not advanced, plain
    push, the PR of the branch is verified, the URL is returned."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    # The closeout order: commit boundary, locked base fetch, ancestry,
    # plain push, remote-head verification, PR list.
    assert ["git", "status", "--porcelain"] in calls
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls
    assert ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"] in calls
    # Issue #898: the pushed head is resolved with ls-remote, never with
    # `git rev-parse origin/<branch>` — that local ref does not exist in
    # a checkout whose fetch refspec does not cover the pushed branch.
    assert ["git", "ls-remote", "--heads", "origin",
            f"refs/heads/{DELIVER_BRANCH}"] in calls
    # The pushed head is the delivery's first recorded engine-pushed
    # head (Issue #833): the merge record subtracts exactly these.
    assert review_merge.read_pushed_head(tmp_path) == FAKE_HEAD_SHA
    assert any(
        command[:2] == ["gh", "pr"] and command[2] == "list"
        for command in calls
    )
    # The PR already exists (the fake answers pr list with it): the
    # Runner never creates a second PR.
    assert not any(
        command[:3] == ["gh", "pr", "create"] for command in calls
    )
    # No merge: the base did not advance.
    assert not any(
        command[:3] == ["git", "merge", "origin/main"] for command in calls
    )


def test_deliver_pr_rolls_back_a_conflicting_base_absorb(
    monkeypatch, tmp_path, caplog,
):
    """Base advanced and the plain merge conflicts: the Runner aborts
    the merge (the worktree returns to the agent's exact commit
    boundary), logs `base_merge_conflict`, and continues — the PR opens
    on the agent's head and the existing review loop absorbs the base
    in-session (the state machine is unchanged)."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["git", "merge", "origin/main"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="CONFLICT (content): Merge conflict",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    assert ["git", "merge", "origin/main"] in calls
    assert ["git", "merge", "--abort"] in calls
    # The abort happens before the push: the pushed head is the
    # agent's exact commit boundary.
    assert calls.index(["git", "merge", "--abort"]) < calls.index(
        ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"],
    )
    assert "base_merge_conflict" in caplog.text


def test_deliver_pr_absorbs_an_advanced_base(monkeypatch, tmp_path, caplog):
    """Base advanced and the plain merge succeeds: the absorbed base is
    pushed with the delivery (`base_absorbed`), so the PR head contains
    the latest remote base."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    assert calls.index(["git", "merge", "origin/main"]) < calls.index(
        ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"],
    )
    assert "base_absorbed" in caplog.text


@pytest.mark.parametrize(
    "attribution_footer, attribution_link, expected_footer, foreign_pr",
    [
        (True, "https://github.com/orbi-build/orbi",
         "Built by [Orbi](https://github.com/orbi-build/orbi) from Issue #4",
         False),
        (True, "https://example.com/x",
         "Built by [Orbi](https://example.com/x) from Issue #4", False),
        # Issue #1590: a link carrying its ?ref= query stays byte-identical.
        (True, "https://orbi.build/?ref=prfoot-owner",
         "Built by [Orbi](https://orbi.build/?ref=prfoot-owner)"
         " from Issue #4", False),
        (False, "https://github.com/orbi-build/orbi", None, False),
        (False, "https://github.com/orbi-build/orbi", None, True),
    ],
)
def test_deliver_pr_creates_the_pr_when_absent(
    monkeypatch, tmp_path, attribution_footer, attribution_link,
    expected_footer, foreign_pr,
):
    """No open PR of the branch: the Runner creates it with the run
    marker and `Fixes #<issue>` in the body (the PR body contract is
    the Runner's obligation now, Issue #186)."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    created = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            # Stateful: empty until the Runner creates the PR, then the
            # created PR of the task branch (like the real GitHub state).
            # A fork PR with the same head name is unrelated and must not
            # suppress creation of this Runner-owned PR.
            prs = ([{
                "number": 99,
                "url": "https://github.com/fork/repo/pull/99",
                "isCrossRepository": True,
            }] if foreign_pr else [])
            if created:
                prs.append(created[0])
            return json.dumps(prs)
        if command[:2] == ["gh", "pr"] and command[2] == "create":
            created.append({
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefName": DELIVER_BRANCH,
                "headRefOid": FAKE_HEAD_SHA,
                "body": command[command.index("--body") + 1],
            })
            return FAKE_PR_URL
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="Closeout title", repo_dir=tmp_path, attribution_footer=attribution_footer, attribution_link=attribution_link, language_code="en") == FAKE_PR_URL
    create = [
        command for command in calls
        if command[:3] == ["gh", "pr", "create"]
    ]
    assert len(create) == 1
    command = create[0]
    assert command[command.index("--base") + 1] == "main"
    assert command[command.index("--head") + 1] == DELIVER_BRANCH
    assert command[command.index("--title") + 1] == "Closeout title"
    body = command[command.index("--body") + 1]
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert "Fixes #4" in body
    if expected_footer is None:
        assert "Built by Orbi" not in body
    else:
        assert expected_footer in body
    if "?ref=" not in attribution_link:
        assert "orbi.build" not in body
        assert "?ref=" not in body


def test_deliver_pr_uses_the_bound_language_for_the_attribution_footer(
    monkeypatch, tmp_path,
):
    """Issue #1729: the PR body's one Orbi-authored prose line follows the
    run's message language; the marker, `Fixes` and `(run_id=)` do not."""
    from orbi import language

    calls = []
    _seed_deliver_run_state(tmp_path)
    created = []
    link = "https://orbi.build/?ref=prfoot-owner"

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "gh" and command[1] == "pr" and command[2] == "list":
            return json.dumps(created)
        if command[0] == "gh" and command[1] == "pr" and command[2] == "create":
            created.append({
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefName": DELIVER_BRANCH,
                "headRefOid": FAKE_HEAD_SHA,
                "body": command[command.index("--body") + 1],
            })
            return FAKE_PR_URL
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.deliver_pr(
        RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH,
                   worktree=tmp_path, source_repo="o/r"),
        "main", "9" * 40, issue_title="Closeout title", repo_dir=tmp_path,
        attribution_link=link, language_code="zh",
    ) == FAKE_PR_URL
    body = created[0]["body"]
    assert language.text(
        "zh", "runner.pr_attribution", link=link, issue=4,
    ) in body
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert "Fixes #4" in body
    assert f"(run_id={FAKE_RUN_ID})" in body
    assert link in body


def test_deliver_pr_fails_fast_when_pr_create_fails(monkeypatch, tmp_path):
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            return "[]"
        if command[:2] == ["gh", "pr"] and command[2] == "create":
            raise subprocess.CalledProcessError(
                1, command, stderr="no matching pull request",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_remote_head_mismatch_after_push(
    monkeypatch, tmp_path,
):
    def fake_run(command, **kwargs):
        # Issue #898: the remote head is read with ls-remote.
        answer = remote_head_answer(command, "f" * 40)
        if answer is not None:
            return answer
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="remote head"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_verifies_the_pr_with_the_latest_base_check_skipped(
    monkeypatch, tmp_path,
):
    """deliver_pr just fetched and merged the base itself, so the
    verify step must not fetch it again (require_latest_base=False):
    no second `git fetch` after the push, and a delivery that is behind
    the base (the conflict-rollback scene) still passes verification."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["git", "merge", "origin/main"]:
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    fetches = [
        index for index, command in enumerate(calls)
        if command[:3] == ["git", "fetch", "origin"]
    ]
    push = calls.index(["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"])
    # Exactly one fetch (the deliver fetch); nothing after the push.
    assert len(fetches) == 1
    assert fetches[0] < push


def test_deliver_pr_reports_a_closed_issue_and_skips_the_pr(
    monkeypatch, tmp_path, caplog,
):
    """Issue #746 (direction A): the Issue was closed while the delivery
    was in flight — the pushed branch keeps the work, the closed Issue
    gets one explanatory comment, and NO PR is created (nothing dangles
    behind a closed Issue)."""
    caplog.set_level("INFO")
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"] and command[-1] == "state":
            return json.dumps({"state": "CLOSED"})
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    comments = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)),
    )
    monkeypatch.setattr(
        runner, "verify_pr",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("verify_pr must not run for a closed Issue")),
    )
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") is None
    # The Issue state is read AFTER the push: the work stays on origin.
    assert calls.index(
        ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"],
    ) > calls.index(["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"])
    assert not any(command[:2] == ["gh", "pr"] for command in calls)
    assert len(comments) == 1
    number, repo, body = comments[0]
    assert (number, repo) == (4, "o/r")
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert DELIVER_BRANCH in body
    assert "delivery_issue_closed branch=" in caplog.text


def test_exclude_path_resolves_the_common_gitdir(tmp_path):
    repo, wt = _make_linked_worktree(tmp_path)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    assert exclude.name == "exclude"
    assert exclude.parts[-2] == "info"
    # Git applies the exclude of the COMMON gitdir to every worktree of
    # the repo (the worktree-specific gitdir carries none of its own):
    # the exclude is the shared repo's `.git/info/exclude`, never inside
    # the worktree checkout.
    assert exclude == repo / ".git" / "info" / "exclude"
    assert not exclude.is_relative_to(wt)


def test_apply_runner_runtime_excludes_writes_all_patterns(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    pi_session.apply_runner_runtime_excludes(wt)
    lines = pi_session.runner_runtime_exclude_path(wt).read_text(
        encoding="utf-8",
    ).splitlines()
    for pattern in pi_session.RUNNER_RUNTIME_EXCLUDES:
        assert pattern in lines


def test_apply_runner_runtime_excludes_is_idempotent(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    pi_session.apply_runner_runtime_excludes(wt)
    first = pi_session.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    pi_session.apply_runner_runtime_excludes(wt)
    second = pi_session.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    assert first == second
    lines = second.splitlines()
    assert len(lines) == len(set(lines)), "patterns must not be duplicated"


def test_apply_runner_runtime_excludes_preserves_user_content(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# my custom exclude\nsecrets-local/\n", encoding="utf-8")
    pi_session.apply_runner_runtime_excludes(wt)
    text = exclude.read_text(encoding="utf-8")
    assert "# my custom exclude" in text
    assert "secrets-local/" in text
    assert ".orbi/" in text
    assert ".pi-session/" in text


def test_apply_runner_runtime_excludes_without_git_is_a_noop(tmp_path):
    wt = tmp_path / "not-a-worktree"
    wt.mkdir()
    pi_session.apply_runner_runtime_excludes(wt)  # must not raise
    assert not (wt / ".git").exists()
    assert not list(wt.iterdir())


def test_run_pi_applies_runner_runtime_excludes_before_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    applied: list[Path] = []
    monkeypatch.setattr(
        pi_session, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(
        {"number": 5, "title": "t", "body": "b"},
        runner.RunContext(
            run_id=config.run_id, issue=5, branch="orbi/owner-repo-issue-5",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        config,
    )
    assert applied == [tmp_path]
    # Issue #302: the run artifact dir exists before the session — the
    # contract commands write `.orbi/plan.md` / `.orbi/test.log` into it.
    assert (tmp_path / ".orbi").is_dir()


def test_run_review_applies_runner_runtime_excludes_before_pi(
    monkeypatch, tmp_path,
):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW", encoding="utf-8")
    applied: list[Path] = []
    monkeypatch.setattr(
        pi_session, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "ok")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    assert applied == [tmp_path]
    # Issue #302: the review session reads/writes the same .orbi/ run
    # artifacts — the run dir exists here too.
    assert (tmp_path / ".orbi").is_dir()


def test_deliver_pr_repairs_runner_runtime_leftovers(monkeypatch, tmp_path,
                                                     caplog):
    """The #246 scene: the task renamed the tracked .gitignore away from
    `.orbi/`, but the parent Runner still created it. The delivery
    applies the local exclude, repairs, logs
    `runner_runtime_exclude_repaired`, and continues — no
    `delivery_uncommitted_changes`."""
    status_calls = {"n": 0}
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            # First check: the Runner's own state looks untracked. After
            # the exclude repair the re-check is clean.
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    assert url == FAKE_PR_URL
    assert "runner_runtime_exclude_repaired" in caplog.text
    assert "delivery_uncommitted_changes" not in caplog.text
    assert status_calls["n"] == 2


def test_deliver_pr_repairs_the_renamed_state_dir_too(monkeypatch, tmp_path,
                                                      caplog):
    """Migration window: the renamed state dir `.orbi/` is Runner-owned
    as well and must be repaired, not failed."""
    status_calls = {"n": 0}
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    assert url == FAKE_PR_URL
    assert "runner_runtime_exclude_repaired" in caplog.text


def test_deliver_pr_still_fails_on_agent_leftovers_alongside_runner_state(
    monkeypatch, tmp_path,
):
    """Runner state plus a REAL agent leftover: the repair must not mask
    the agent's uncommitted code — `delivery_uncommitted_changes` stands."""

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M src/app.py\n?? .orbi/\n?? foo.py\n"
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_cleanup_task_worktree_removes_the_scene_and_prunes(tmp_path):
    repo, wt = _make_linked_worktree(tmp_path)
    (wt / ".orbi").mkdir(parents=True)
    (wt / ".pi-session").mkdir(parents=True)
    runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=wt, source_repo="owner/repo"), repo)
    assert not wt.exists()
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(wt) not in listing


def test_cleanup_task_worktree_logs_failure_and_keeps_the_scene(
    monkeypatch, tmp_path, caplog,
):
    wt = tmp_path / "wt"
    wt.mkdir()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner.shutil, "rmtree", boom)
    with caplog.at_level(logging.ERROR, logger="orbi.bootstrap"):
        runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=wt, source_repo="owner/repo"), tmp_path)
    assert "worktree_cleanup_failed" in caplog.text
    assert wt.exists(), "a failed cleanup must never claim the scene is gone"


def test_exclude_path_rejects_a_pointer_without_gitdir(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").parent.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("# not a real pointer\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no gitdir entry"):
        pi_session.runner_runtime_exclude_path(wt)


def test_runner_runtime_only_accepts_quoted_runner_paths():
    # Porcelain quotes paths with special characters; the runner paths
    # are plain ASCII, so the unquoted match is exact.
    assert pi_session._is_runner_runtime_only('?? ".orbi/"\n')
    assert pi_session._is_runner_runtime_only('?? ".pi-session/"\n')
    # Issue #302: the orbi contract artifact set (pi-loop state, per-run
    # plan/test/verify artifacts) is runner-owned too — a porcelain entry
    # carrying only them must keep the repair path open, never the fail
    # fast.
    assert pi_session._is_runner_runtime_only('?? ".pi/"\n')
    assert pi_session._is_runner_runtime_only("?? plan.md\n")
    assert pi_session._is_runner_runtime_only("?? test.log\n")
    assert pi_session._is_runner_runtime_only("?? verify.md\n")
    # An agent-owned leftover is NEVER runner-owned, mixed or alone.
    assert pi_session._is_runner_runtime_only("?? notes.md\n") is False
    assert pi_session._is_runner_runtime_only("?? unexpected.py\n") is False
    assert pi_session._is_runner_runtime_only("?? plan.md\n?? notes.md\n") is False


def test_exclude_path_for_a_plain_checkout_uses_its_own_git_dir(tmp_path):
    # A worktree whose `.git` is a DIRECTORY (a plain checkout, not a
    # linked worktree): the exclude is its own `.git/info/exclude`.
    wt = tmp_path / "checkout"
    (wt / ".git").mkdir(parents=True)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    assert exclude == wt / ".git" / "info" / "exclude"


def test_runner_runtime_only_rejects_an_empty_status():
    # An empty status is not "runner-only" — the repair must not fire on
    # a clean worktree.
    assert pi_session._is_runner_runtime_only("") is False
    assert pi_session._is_runner_runtime_only("\n") is False


def test_cleanup_task_worktree_prunes_without_a_scene(tmp_path):
    # The worktree directory is already gone (a partial failure): the
    # cleanup still prunes the git metadata and logs success.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    missing = tmp_path / "already-gone"
    runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=missing, source_repo="owner/repo"), repo)
    assert not missing.exists()


def test_apply_runner_runtime_excludes_creates_a_missing_exclude_file(
    tmp_path,
):
    # The common gitdir exists but has no info/exclude yet: the helper
    # creates the file with exactly the runner-owned patterns.
    repo, wt = _make_linked_worktree(tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    # `git init` ships a default exclude file (comments only); remove it
    # so this drives the file-creation branch.
    exclude.unlink()
    pi_session.apply_runner_runtime_excludes(wt)
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines == list(pi_session.RUNNER_RUNTIME_EXCLUDES)


def test_runtime_excludes_exempt_the_orbi_contract_artifacts(tmp_path):
    """Issue #302 acceptance: the four historical dirty-gate scenes are
    exempted by the RUNTIME local exclude alone — with NO tracked
    .gitignore at all (the #246 renamed/broken .gitignore scene), every
    orbi-owned artifact is invisible to `git status --porcelain` while
    real agent leftovers still fail the gate. No new .gitignore entry
    is needed for any of them."""
    repo, wt = _make_linked_worktree(tmp_path)
    # The tracked .gitignore is gone entirely: the exemption must not
    # depend on a file a task can legally rename or break.
    assert not (wt / ".gitignore").exists()
    pi_session.apply_runner_runtime_excludes(wt)
    # Scene #215: pi-loop state written at session shutdown.
    (wt / ".pi").mkdir()
    (wt / ".pi" / "loops.json").write_text("{}\n", encoding="utf-8")
    # Scene #256: runner runtime state (the brand dir and the pi
    # session dir).
    (wt / ".orbi").mkdir()
    (wt / ".orbi" / "run-state.json").write_text("{}\n", encoding="utf-8")
    (wt / ".pi-session").mkdir()
    (wt / ".pi-session" / "s.jsonl").write_text("{}\n", encoding="utf-8")
    # The per-run contract artifacts (the pre-#302 prompt wrote them at
    # the worktree root) and the coverage gate output in the run dir.
    for name in ("plan.md", "test.log", "verify.md"):
        (wt / name).write_text("artifact\n", encoding="utf-8")
    (wt / ".orbi" / "coverage.json").write_text(
        '{"totals": {}}\n', encoding="utf-8",
    )
    (wt / ".orbi" / "test.log").write_text("156 passed\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout == ""
    # The gate is NOT weakened: real agent leftovers are still reported.
    (wt / "unexpected.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "notes.md").write_text("notes\n", encoding="utf-8")
    lines = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "?? unexpected.py" in lines
    assert "?? notes.md" in lines


def test_runtime_excludes_never_hide_a_modified_tracked_file(tmp_path):
    # Issue #302 reverse verification: the runtime exclude only ever
    # hides UNTRACKED orbi artifacts — a modified tracked source file is
    # never exempted (excludes cannot hide tracked changes), so the
    # delivery gate still fails fast on it.
    repo, wt = _make_linked_worktree(tmp_path)
    tracked = wt / "src.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "src.py"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "src"], check=True,
    )
    pi_session.apply_runner_runtime_excludes(wt)
    tracked.write_text("x = 2\n", encoding="utf-8")
    (wt / "plan.md").write_text("artifact\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout == " M src.py\n"


def test_pick_resumable_routes_marker_ticket_instead_of_blocking(
    monkeypatch, tmp_path,
):
    """Issue #726 gap 1：triage workflow 给被外部 PR 关联的票贴了
    ai-pr-opened，resumable 扫描捡到它却找不到 runner 场景评论——旧码
    直接烧成 ai-blocked，而接管入口对这些票永不可达。带 external 标记
    且 PR 仍 OPEN 的票必须转投：EVENT_REQUEUE 回 ready 队列，下一个
    fresh claim 的接管探针接手评审。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    patches = []
    monkeypatch.setattr(seam, "apply_label_patch",
        lambda number, *, repo, event, current_labels:
            patches.append(event),
    )
    comments_posted = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments_posted.append(body),
    )

    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix the thing",
             "labels": [{"name": "ai-pr-opened"}]}

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "OPEN"})
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert patches == [runner.EVENT_REQUEUE]
    assert any("external contribution PR #55" in body
               for body in comments_posted)
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_pick_resumable_closes_marker_ticket_when_pr_already_merged(
    monkeypatch, tmp_path,
):
    """Issue #726 gap 1 的合并分支：标记票的外部 PR 已被人工合并——
    修复已交付，triage 票按接管合并的同款簿记关票，而不是 block、
    也不是重做。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    closes = []
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix the thing",
             "labels": [{"name": "ai-pr-opened"}]}

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        if command[:3] == ["gh", "pr", "view"]:
            return closes.append(command) or json.dumps({"state": "MERGED"})
        if command[:3] == ["gh", "issue", "close"]:
            return closes.append(command) or ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert any(c[:3] == ["gh", "issue", "close"] for c in closes)
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_delivery_step_never_closes_triage_issue_after_review(
    monkeypatch, tmp_path,
):
    """Issue #842 (D2)：外部接管的评审不再自动合并——clean verdict 在
    `review_and_merge_if_clean` 内部停在分诊态（ai-blocked，评审结论
    留在 Issue 上），`delivery_step` 因此永不因评审结束去关 triage 票
    （旧的 auto-merge 关票分支已随自动合并一起移除）；关票只发生在
    轮询发现人工已合并（MERGED）的分支。"""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command[:3] == ["gh", "issue", "close"]:
            closes.append(command)
            return ""
        if command[:3] == ["gh", "issue", "edit"]:
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        if command[:2] == ["gh", "api"]:
            return ""
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    closes: list = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    # False is the D2 triage stop's return (the round ends without a
    # merge — the ticket is now the maintainer's).
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: False,
    )
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo", external_takeover=True,
    )
    assert closes == [], (
        "the triage Issue must NOT be closed by the review round — "
        "the engine never merges an external PR (Issue #842 D2)"
    )
    # The answered arms return their canned values; anything unmodeled
    # fails loud.
    assert fake_run(["gh", "issue", "edit", "1", "--repo", "o/r"]) == ""
    assert fake_run(["gh", "issue", "comment", "1", "--repo", "o/r"]) == ""
    assert fake_run(["gh", "issue", "close", "1", "--repo", "o/r"]) == ""
    assert fake_run(["gh", "api", "x"]) == ""
    assert fake_run(
        ["git", "branch", "--show-current"],
    ) == "orbi/owner-repo-issue-39"
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_route_external_pr_probe_failure_falls_through_to_block(
    monkeypatch, tmp_path,
):
    """Issue #726/#786：接管探针自身失败（gh 异常）时绝不瞎猜路由——
    落回 missing-scene 分支自己的 block 路径，让失败被看见。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue])
        if command[:3] == ["gh", "issue", "list"]
        else (_ for _ in ()).throw(RuntimeError("gh down")),
    )
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix",
             "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((100,), {"repo": "owner/repo",
                               "add": "ai-blocked",
                               "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]


def test_route_external_pr_unknown_state_falls_through_to_block(
    monkeypatch, tmp_path,
):
    """Issue #726：PR 状态是意外值（非 OPEN/MERGED/CLOSED）时同样落回
    block 路径——路由器只承诺三种已知世界。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue])
        if command[:3] == ["gh", "issue", "list"] else (
        json.dumps({"state": "DRAFT"})
        if command[:3] == ["gh", "pr", "view"] else (_ for _ in ()).throw(
            AssertionError(f"unexpected command: {command}"))),
    )
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix",
             "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((100,), {"repo": "owner/repo",
                               "add": "ai-blocked",
                               "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]


def test_route_external_pr_ignores_tickets_without_marker(
    monkeypatch, tmp_path,
):
    """Issue #726 的边界：body 没有外部标记的普通无现场票照走 block
    路径——路由器零介入（连 pr view 都不发生）。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 41, "title": "dev", "state": "OPEN",
             "body": "plain ticket", "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((41,), {"repo": "owner/repo",
                              "add": "ai-blocked",
                              "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_delivery_step_closes_triage_issue_on_merged_poll(
    monkeypatch, tmp_path,
):
    """Issue #726 的既有行为钉子：轮询直接发现 PR 已 MERGED（重启后的
    首询）且是外部接管时，triage 票被关闭——:8023 的旁路调用在旧码里
    从未有测试覆盖。"""
    states = ["MERGED"]

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": states[0]})
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        if command[:3] == ["gh", "issue", "close"]:
            closes.append(command)
            return ""
        raise AssertionError(f"unexpected command: {command}")

    closes: list = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo", external_takeover=True,
    )
    assert len(closes) == 1
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_close_external_triage_issue_swallows_close_failure(
    monkeypatch, caplog, tmp_path,
):
    """关票是已合并事实的簿记旁路：失败只记日志，永不改写合并事实、
    永不炸 tick（#608 语义，#726 抽取后必须保持）。"""
    monkeypatch.setattr(seam, "comment_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh down")),
    )
    with caplog.at_level("ERROR"):
        runner._close_external_triage_issue(
            39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
            "a1b2c3d4", language_code="en",
        )
    assert "external_takeover_close_failed" in caplog.text


def test_external_triage_comments_follow_the_config_language(monkeypatch):
    """Issue #1687 item 3: the closed/merged external-PR comments were
    English-only literals; they render in the bound message language now."""
    comments: list[str] = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append(body))
    monkeypatch.setattr(
        seam, "comment_pr",
        lambda number, *, repo, body: comments.append(body),
    )
    monkeypatch.setitem(runner.__dict__, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setitem(runner.__dict__, "issue_labels", lambda *a, **k: [])
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")

    runner._requeue_closed_external_takeover(
        39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
        "a1b2c3d4", language_code="zh",
    )
    assert "Orbi：外部 PR" in comments[0]
    assert "未合并就被关闭" in comments[0]
    assert "run_id=a1b2c3d4" in comments[0]

    runner._close_external_triage_issue(
        39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
        "a1b2c3d4", language_code="zh",
    )
    assert "Orbi 已合并外部 PR" in comments[-1]
