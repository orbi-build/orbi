"""Fake-based tests for the pickup scan and the GitHub adapter surface
(Issue #789).

These tests NEVER patch a runner-internal name — zero
``setattr(runner, ...)``. They arrange state in the
``FakeGh`` in-memory GitHub, patch only the ONE subprocess seam
(``seam.run_command``, Article 3.4), run the real public entry points
(the claim scans through ``claim.pick_issue``; the label/comment/PR/
check-run reads and writes through ``orbi.github``), and assert the
public surface: which Issue the scan picks, the labels an edit leaves,
the comment order, and the returned PR/check data.

The list order mirrors ``gh issue list``: newest-created first (the
``sort:created-asc`` qualifier reverses it, as GitHub does), so tests
seed issues oldest-first.
"""
import base64
import fcntl
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

import orbi.comment_trust as comment_trust
import orbi.github as github
import orbi.github_feedback as github_feedback
import orbi.pilot_slots as pilot_slots
import orbi.repo_config as repo_config
import orbi.claim as claim
import orbi.runner as runner
import orbi.cli as cli
import orbi.cli_status as cli_status
import orbi.status_observe as observe
from orbi.config import RunnerConfig
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EPIC_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    P0_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
)
from orbi.progress import format_status_comment
from seam import resume_deps, seam

from tests.fakes.github import FakeGh


@pytest.fixture
def fake_gh(monkeypatch):
    """The FakeGh wired into the ONE subprocess seam — the only patch
    a fake-based test needs."""
    fake = FakeGh("owner/repo")
    monkeypatch.setattr(seam, "run_command", fake)
    return fake


# --- the claim scans (public entry: claim.pick_issue) ----------------------


def test_pickup_claims_the_p0_issue_first(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, P0_LABEL))
    picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 2
    assert github.issue_priority(picked) == "p0"


def test_pickup_prefers_a_bug_over_plain_ready(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, "bug"))
    assert claim.pick_issue("owner/repo")["number"] == 2


def test_pickup_yields_an_issue_a_live_co_runner_holds(fake_gh, caplog):
    """Issue #1319: the ready scan skips the delivery a live co-runner
    already holds (the first runner wrote its identity into its slot
    file) and claims the next free one, logging the yield. The scans
    read oldest-created first (Issue #1914), so the HELD Issue #8 is
    seeded first and heads this scan."""
    fake_gh.add_issue(8, labels=(READY_LABEL,))
    fake_gh.add_issue(7, labels=(READY_LABEL,))
    with caplog.at_level(logging.INFO):
        picked = claim.pick_issue(
            "owner/repo", held=frozenset({("owner/repo", 8)}),
        )
    assert picked["number"] == 7
    assert "claim_yield" in caplog.text
    assert "held_by_live_runner" in caplog.text


def test_pickup_returns_none_when_every_ready_issue_is_held(fake_gh):
    """Issue #1319: no candidate is claimed from a scan whose every
    Issue is already held by a live co-runner."""
    fake_gh.add_issue(7, labels=(READY_LABEL,))
    assert claim.pick_issue(
        "owner/repo", held=frozenset({("owner/repo", 7)}),
    ) is None


def test_pickup_claims_the_p0_scan_before_the_bug_scan(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL, "bug"))
    fake_gh.add_issue(2, labels=(READY_LABEL, P0_LABEL))
    assert claim.pick_issue("owner/repo")["number"] == 2


def test_pickup_skips_a_blocked_issue_and_claims_the_next(fake_gh, caplog):
    # The scan reads oldest-created first (Issue #1914), so #1 seeds
    # first and heads it: it is blocked, #2 is claimed.
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(3)
    fake_gh.add_blocker(1, 3, state="OPEN")
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    with caplog.at_level(logging.INFO):
        picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 2
    # A skip never touches labels: the blocked Issue stays claimable.
    assert github.issue_labels(1, "owner/repo") == [READY_LABEL]
    assert "blocked_by issue=1" in caplog.text


def test_pickup_claims_the_longest_waiting_ready_issue(fake_gh):
    """Issue #1914, the production scene: three plain `ai-ready` Issues
    queued at t1 < t2 < t3, nothing held and nothing blocked.

    The fake lists a scan newest-created first unless the query carries
    `sort:created-asc` (exactly as `gh issue list` does), so this test
    reads the REAL query `pick_issue` builds: the oldest Issue is the
    one claimed, never the newest. Before the fix the same three Issues
    picked #160 while #158 waited."""
    for number in (158, 159, 160):
        fake_gh.add_issue(number, labels=(READY_LABEL,))
    assert claim.pick_issue("owner/repo")["number"] == 158


def test_pickup_walks_past_the_oldest_when_it_has_an_open_blocker(
    fake_gh, caplog,
):
    """Issue #1914, the failure path: the longest-waiting Issue carries
    an open native `blockedBy` blocker, so the scan logs the structured
    `blocked_by` line and claims the next oldest claimable Issue
    instead of stalling the queue."""
    for number in (158, 159, 160):
        fake_gh.add_issue(number, labels=(READY_LABEL,))
    fake_gh.add_issue(200)  # the blocker: open, but not in the queue
    fake_gh.add_blocker(158, 200, state="OPEN")
    with caplog.at_level(logging.INFO):
        picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 159
    assert "blocked_by issue=158" in caplog.text
    # A skip never touches a label: the blocked Issue stays queued.
    assert github.issue_labels(158, "owner/repo") == [READY_LABEL]


def test_pickup_keeps_a_newer_p0_ahead_of_an_older_plain_issue(fake_gh):
    """Issue #1914, the unchanged priority: oldest-first orders the
    candidates INSIDE one scan. The pickup order between the scans is
    still P0 -> bug -> plain, so a P0 created after a plain Issue is
    claimed first."""
    fake_gh.add_issue(158, labels=(READY_LABEL,))
    fake_gh.add_issue(176, labels=(READY_LABEL, P0_LABEL))
    picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 176
    assert github.issue_priority(picked) == "p0"


def test_pickup_claims_an_issue_whose_blocker_is_closed(fake_gh):
    # #1 heads the (oldest-first, Issue #1914) scan; its blocker is
    # closed, so the CLOSED relation is inert and #1 is claimed.
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(3, state="closed")
    fake_gh.add_blocker(1, 3, state="CLOSED")
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    assert claim.pick_issue("owner/repo")["number"] == 1


def test_pickup_skips_an_epic(fake_gh, caplog):
    # The Epic is the oldest Issue here, so it heads the scan and is
    # skipped (Issue #1914's order) before #2 is claimed.
    fake_gh.add_issue(1, labels=(READY_LABEL, EPIC_LABEL))
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    with caplog.at_level(logging.INFO):
        picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 2
    assert "epic_not_claimed issue=1" in caplog.text


def test_pickup_excludes_terminal_and_in_flight_states(fake_gh):
    fake_gh.add_issue(3, labels=(READY_LABEL,))
    fake_gh.add_issue(1, labels=(READY_LABEL, MERGED_LABEL))
    fake_gh.add_issue(2, labels=(READY_LABEL, IN_PROGRESS_LABEL))
    assert claim.pick_issue("owner/repo")["number"] == 3


def test_pickup_scopes_the_scan_to_the_active_milestone(fake_gh):
    fake_gh.add_issue(7, title="v2 issue", labels=(READY_LABEL,),
                      milestone=2)
    fake_gh.add_issue(9, title="milestoneless", labels=(READY_LABEL,))
    fake_gh.add_issue(8, title="v1 issue", labels=(READY_LABEL,),
                      milestone=1)
    fake_gh.add_milestone(1, title="v1")
    fake_gh.add_milestone(2, title="v2")
    picked = claim.pick_issue("owner/repo", active_milestone="v1")
    assert picked["number"] == 8


def test_pickup_fails_open_when_the_scan_query_fails(
    fake_gh, monkeypatch, caplog,
):
    fake_gh.add_issue(1, labels=(READY_LABEL,))

    def down(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output="", stderr="gh: not found (fatal)"
        )

    monkeypatch.setattr(seam, "run_command", down)
    with caplog.at_level(logging.ERROR):
        assert claim.pick_issue("owner/repo") is None
    assert "blocked_by_check_failed" in caplog.text


# --- the release fallback scan (claimed only when nothing else is) ----------


def test_pickup_claims_the_release_from_the_fallback_scan(fake_gh, caplog):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)
    with caplog.at_level(logging.INFO):
        picked = claim.pick_issue("owner/repo")
    assert picked["number"] == 1
    # The ordinary scans logged the release skip before the fallback ran.
    assert "release_not_claimed issue=1" in caplog.text


def test_pickup_skips_the_release_while_the_milestone_has_open_work(
    fake_gh, caplog,
):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=2)
    fake_gh.add_issue(2, milestone=1)  # the other open Milestone Issue
    with caplog.at_level(logging.INFO):
        assert claim.pick_issue("owner/repo") is None
    assert "release_milestone_incomplete issue=1" in caplog.text


def test_pickup_release_with_a_failed_milestone_check_fails_safe(
    fake_gh, monkeypatch, caplog,
):
    # The milestone-counter read fails (an API outage scene): a bad
    # release must never ship on a failed check, so the scan returns
    # nothing this tick.
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)

    def api_outage(command, **kwargs):
        if command[1:2] == ["api"] and "milestones" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="gh: API error (fatal)"
            )
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", api_outage)
    with caplog.at_level(logging.ERROR):
        assert claim.pick_issue("owner/repo") is None
    assert "release_milestone_check_failed issue=1" in caplog.text


def test_pickup_release_scoped_to_the_active_milestone(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)
    fake_gh.add_milestone(2, title="v2.0", open_issues=1)
    # The scan scoped to v2.0 must not see the v1.0 release Issue.
    assert claim.pick_issue("owner/repo", active_milestone="v2.0") is None
    picked = claim.pick_issue("owner/repo", active_milestone="v1.0")
    assert picked["number"] == 1


# --- the label lifecycle (public entries: apply_label_patch etc.) -----------


def test_claim_label_patch_is_idempotent_on_the_fake(fake_gh):
    fake_gh.add_issue(5, labels=(READY_LABEL,))
    current = github.issue_labels(5, "owner/repo")
    github.apply_label_patch(5, repo="owner/repo", event="claim",
                             current_labels=current)
    labels = github.issue_labels(5, "owner/repo")
    assert labels == [READY_LABEL, IN_PROGRESS_LABEL]
    assert github.has_in_progress_label(5, "owner/repo") is True
    # Idempotent: re-applying the same event changes nothing.
    github.apply_label_patch(5, repo="owner/repo", event="claim",
                             current_labels=labels)
    assert github.issue_labels(5, "owner/repo") == labels
    github.edit_issue(5, repo="owner/repo",
                      remove=IN_PROGRESS_LABEL)
    github.edit_issue(5, repo="owner/repo",
                      remove=IN_PROGRESS_LABEL)
    assert github.issue_labels(5, "owner/repo") == [READY_LABEL]


def test_comments_append_in_order_and_read_back_oldest_first(fake_gh):
    fake_gh.add_issue(5)
    github.comment_issue(5, repo="owner/repo", body="first runner note")
    fake_gh.comment(5, "human decision", login="xqliu",
                    association="MEMBER")
    github.comment_issue(5, repo="owner/repo", body="second runner note")
    comments = github.issue_comments(5, repo="owner/repo")
    bodies = [comment["body"] for comment in comments]
    assert bodies == [
        format_status_comment("first runner note"),
        "human decision",
        format_status_comment("second runner note"),
    ]
    assert [comment["createdAt"] for comment in comments] == sorted(
        comment["createdAt"] for comment in comments
    )


def test_trusted_block_keeps_maintainer_and_bot_comments(fake_gh):
    """Issue #1806: the block's trust comes from the author's repository
    permission (read through the fake's permission endpoint), plus the
    runner's own account."""
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.comment(5, "drive-by noise", login="stranger",
                    association="NONE")
    fake_gh.comment(5, "the decision", login="xqliu", association="MEMBER")
    github.comment_issue(5, repo="owner/repo", body="runner note")
    block = comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo="owner/repo"), limit=20,
        repo="owner/repo",
    )
    assert "xqliu" in block and "the decision" in block
    # The runner's own comment is trusted by its authenticated login
    # (resolved through `gh auth status` on the fake).
    assert "orbi-bot" in block and "runner note" in block
    assert "stranger" not in block and "drive-by noise" not in block


def test_trusted_block_drops_the_oldest_over_the_limit(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.comment(5, "old decision", login="xqliu",
                    association="MEMBER")
    fake_gh.comment(5, "new decision", login="xqliu",
                    association="MEMBER")
    block = comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo="owner/repo"), limit=1,
        repo="owner/repo",
    )
    assert "new decision" in block
    assert "old decision" not in block
    assert "1 older trusted comment omitted" in block


# --- the PR and CI surfaces ---------------------------------------------------


def test_open_pr_for_branch_and_delivery_status(fake_gh, tmp_path):
    branch = "orbi/owner-repo-issue-5"
    fake_gh.add_pr(
        11, head=branch,
        checks=({"name": "CI", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},),
    )
    pr = github.open_pr_for_branch(tmp_path, branch)
    assert pr["number"] == 11
    state, summaries = github_feedback.pr_delivery_status(
        pr["url"], "owner/repo")
    assert (state, summaries) == ("OPEN", ["CI=COMPLETED/SUCCESS"])
    fake_gh.add_pr(12, head=branch)
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        github.open_pr_for_branch(tmp_path, branch)


def test_open_pr_for_branch_returns_none_without_a_pr(fake_gh, tmp_path):
    assert github.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-5") \
        is None


def test_commit_check_runs_reads_the_ci_evidence(fake_gh):
    runs = [{"name": "CI", "conclusion": "SUCCESS"}]
    fake_gh.add_check_runs("abc123", runs)
    assert github.commit_check_runs("owner/repo", "abc123") == runs
    with pytest.raises(subprocess.CalledProcessError):
        github.commit_check_runs("owner/repo", "missing")


def test_merge_gate_preflight_fails_closed_on_protection_it_cannot_read(
        fake_gh):
    """The preflight reads the branch state GitHub really serves: an
    unseeded branch, and a protected branch whose classic protection the
    token cannot see, both answer 404 — the fake never invents state,
    and the preflight reports the hidden protection instead of a PASS
    (Issue #1174)."""
    assert github.merge_gate_preflight("owner/repo", "main") == [
        "merge_gate: UNKNOWN cannot read target branch",
    ]
    fake_gh.add_branch("main", protected=True)
    assert github.merge_gate_preflight("owner/repo", "main") == [
        "merge_gate: UNKNOWN classic protection returned 404 for a "
        "protected branch; grant the token repository administration "
        "permission",
    ]


def test_milestone_counter_fails_on_an_unknown_title(fake_gh):
    # GitHub's own counter for a Milestone that does not exist yields
    # empty output: the adapter must raise (a failed check), never
    # return a silent 0 the release gate could ship on.
    fake_gh.add_milestone(1, title="v1.0")
    fake_gh.add_milestone(2, title="v2.0")
    with pytest.raises(RuntimeError, match="not found in owner/repo"):
        github.milestone_open_issue_count("owner/repo", "v9.9")


def test_close_issue_marks_the_terminal_state(fake_gh):
    fake_gh.add_issue(5, labels=(READY_LABEL,))
    github.close_issue(5, repo="owner/repo")
    assert github.issue_view(5, "state")["state"] == "CLOSED"
    assert github.issue_view(5, "url")["url"] == (
        "https://github.com/owner/repo/issues/5"
    )


def test_list_issues_label_and_state_filters(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, "bug"), state="closed")
    fake_gh.add_issue(3, labels=("bug",))
    # gh lists newest first; #2 is closed and must never match open.
    assert github.list_issues(
        "owner/repo", state="open", label="bug",
        json_fields="number", limit=200,
    ) == [{"number": 3}]
    # A milestoneless issue renders the milestone field as null (the
    # shape the release scope derivation reads).
    assert github.list_issues(
        "owner/repo", state="open", search=f"label:bug",
        json_fields="number,milestone", limit=200,
    ) == [{"number": 3, "milestone": None}]


# --- the failure paths (fail fast, never a silent pass) ----------------------


def assert_fails_with(command_callable, stderr_fragment: str) -> None:
    """Assert the fake failed like a real CLI: CalledProcessError whose
    stderr carries the reason (never a silent empty success)."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        command_callable()
    assert stderr_fragment in excinfo.value.stderr


def test_fake_fails_fast_on_unsupported_commands(fake_gh):
    assert_fails_with(
        lambda: fake_gh(["bogus", "argv"]), "unsupported command: bogus"
    )
    assert_fails_with(lambda: fake_gh(["gh"]), "unsupported command: gh")
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/milestones", "bare-token"]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/unknown-endpoint"]),
        "unsupported command: gh api repos/owner/repo/unknown-endpoint",
    )
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/milestones", "--jq", "."]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/commits/abc123/check-runs",
             "--jq", ".wrong"]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "release", "view", "v1"]),
        "unsupported command: gh release view v1",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "issue"]), "unsupported command: gh issue"
    )


def test_fake_fails_fast_on_repository_mismatch(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.add_check_runs("abc123", [])
    assert_fails_with(
        lambda: github.comment_issue(5, repo="other/repo", body="x"),
        "repository mismatch",
    )
    assert_fails_with(
        lambda: github.commit_check_runs("other/repo", "abc123"),
        "repository mismatch",
    )


def test_fake_fails_fast_on_an_unknown_issue(fake_gh):
    assert_fails_with(
        lambda: github.issue_labels(999, "owner/repo"),
        "Could not resolve to an Issue",
    )


def test_fake_fails_fast_on_unsupported_field_requests(fake_gh):
    fake_gh.add_issue(5)
    assert_fails_with(
        lambda: github.issue_view(5, "assignees"),
        "unsupported issue field: 'assignees'",
    )
    fake_gh.add_pr(6, head="b")
    assert_fails_with(
        lambda: github.pr_view(6, "mergeable"),
        "unsupported pr field: 'mergeable'",
    )


def test_fake_fails_fast_on_unimplemented_list_flags(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    # `--milestone` is a real gh filter the fake does not implement:
    # silently ignoring it would match issues the real scan excludes.
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open", milestone="v1",
            json_fields="number", limit=200,
        ),
        "unsupported command: gh issue list --milestone",
    )


def test_fake_fails_fast_on_bad_search_qualifiers(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open", search="label:ai-ready author:x",
            json_fields="number", limit=200,
        ),
        "unsupported search qualifier",
    )
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open",
            search='milestone:"a" milestone:"b"',
            json_fields="number", limit=200,
        ),
        "two milestone qualifiers",
    )


def test_fake_list_issues_scopes_a_quoted_phrase_to_the_title(fake_gh):
    """Issue #1825: the CI triage lookup searches a quoted TITLE phrase
    (`"CI failure: <check> on branch <base>" in:title` — the documented
    GitHub qualifier). Without the scope the phrase matches the body, as
    before; `in:body` pins the body explicitly."""
    fake_gh.add_issue(
        1, title="CI failure: tests on branch main (push)",
        body="unrelated",
    )
    fake_gh.add_issue(2, title="other", body="CI failure: tests on branch main")

    def search(term):
        return [
            issue["number"] for issue in github.list_issues(
                "owner/repo", state="open", search=term,
                json_fields="number", limit=200,
            )
        ]

    assert search('"CI failure: tests on branch main" in:title') == [1]
    assert search('"CI failure: tests on branch main" in:body') == [2]
    assert search('"CI failure: tests on branch main"') == [2]


def test_branch_head_sha_reads_the_live_head_and_fails_fast(fake_gh,
                                                            monkeypatch):
    """Issue #1825: the live base head is the branch resource's commit
    SHA (the PR's `baseRefOid` is a snapshot and never moves). A branch
    name with a slash travels URL-encoded; a response without a commit
    SHA fails fast instead of reading as a real head."""
    fake_gh.add_branch("release/7", head="abc1234")
    assert github.branch_head_sha("owner/repo", "release/7") == "abc1234"
    fake_gh.add_branch("release/7", head=None)
    with pytest.raises(ValueError):
        github.branch_head_sha("owner/repo", "release/7")
    monkeypatch.setattr(seam, "run_command", lambda *_a, **_k: "[]")
    with pytest.raises(ValueError):
        github.branch_head_sha("owner/repo", "main")


def test_fake_issue_comments_carry_an_id_and_a_permalink(fake_gh):
    """Every real comment has a REST id and a permalink; the in-place edit
    path reads the id back from that URL (Issue #825's PATCH route)."""
    fake_gh.add_issue(3)
    fake_gh.comment(3, "first")
    fake_gh(["gh", "issue", "comment", "3", "--repo", "owner/repo",
             "--body", "second"])
    comments = github.issue_comments(3, repo="owner/repo")
    assert [comment["body"] for comment in comments] == ["first", "second"]
    for comment in comments:
        assert "#issuecomment-" + str(comment["id"]) in comment["url"]


def test_fake_issue_edit_models_sub_issues_parents_and_blockers(fake_gh):
    """`gh issue edit --add-sub-issue` / `--add-blocked-by` (Issue #1927):
    the edited Issue is the parent of the listed numbers, each blocked-by
    number becomes one native edge, and `--json parent` reports the parent
    object (null when there is none). Both flags repeat, as gh's do."""
    for number in (1, 2, 3, 4, 5):
        fake_gh.add_issue(number, title=f"Issue {number}")
    fake_gh(["gh", "issue", "edit", "1", "--repo", "owner/repo",
             "--add-sub-issue", "2", "--add-sub-issue", "3",
             "--add-blocked-by", "4", "--add-blocked-by", "5"])
    # Re-adding an existing relation is idempotent, like the fake's labels.
    fake_gh(["gh", "issue", "edit", "1", "--repo", "owner/repo",
             "--add-blocked-by", "4"])
    assert github.issue_view(1, "parent", repo="owner/repo")["parent"] is None
    assert github.issue_view(2, "parent", repo="owner/repo")["parent"] == {
        "number": 1, "title": "Issue 1", "state": "OPEN",
    }
    assert [
        node["number"] for node in
        github.issue_view(1, "blockedBy", repo="owner/repo")["blockedBy"]["nodes"]
    ] == [4, 5]


def test_fake_issue_comment_patch_edits_in_place(fake_gh):
    fake_gh.add_issue(4)
    fake_gh.comment(4, "waiting")
    comment = github.issue_comments(4, repo="owner/repo")[0]
    github.update_issue_comment(
        comment["id"], repo="owner/repo", body="merged",
    )
    comments = github.issue_comments(4, repo="owner/repo")
    assert len(comments) == 1
    assert "merged" in comments[0]["body"]


def test_fake_issue_comment_patch_fails_fast_on_a_mistyped_write(fake_gh):
    """The PATCH route models exactly `gh api <comment path> --method
    PATCH --field body=...`; every other shape fails like the real CLI
    instead of silently editing the wrong thing."""
    fake_gh.add_issue(4)
    fake_gh.comment(4, "waiting")
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/issues/comments/1",
                         "--method", "PATCH"]),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/unknown",
                         "--method", "PATCH", "--field", "body=x"]),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/issues/comments/1",
                         "--method", "PATCH", "--field", "state=x"]),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/issues/comments/999",
                         "--method", "PATCH", "--field", "body=x"]),
        "Could not resolve an Issue comment",
    )


def test_fake_list_issues_honours_the_requested_creation_order(fake_gh):
    """Issue #1734: `gh issue list` returns the newest-created issues first
    unless the documented `sort:created-asc` advanced-search qualifier asks
    for the oldest first (`gh issue list --help` ships exactly that
    qualifier in its own example). The review scan depends on it to keep a
    page of "the deliveries that have waited longest"."""
    for number in (11, 12, 13):
        fake_gh.add_issue(number, labels=(READY_LABEL,))
    newest_first = github.list_issues(
        "owner/repo", state="open", search=f"label:{READY_LABEL}",
        json_fields="number", limit=2,
    )
    assert [issue["number"] for issue in newest_first] == [13, 12]
    oldest_first = github.list_issues(
        "owner/repo", state="open",
        search=f"label:{READY_LABEL} sort:created-asc",
        json_fields="number", limit=2,
    )
    assert [issue["number"] for issue in oldest_first] == [11, 12]


def test_repo_config_unseeded_is_a_designed_no_op(fake_gh):
    """A read of `.github/orbi.toml` with nothing seeded is GitHub's 404 —
    the real `read_repo_config` maps it to its designed silent no-op
    (the host-config fallback), never a generic error."""
    assert repo_config.read_repo_config(
        "owner/repo", run_command=fake_gh,
    ) is None


# --- the contents PUT surface (Issue #1306) ---------------------------------


def _contents_put(*fields: str, path: str = "repos/owner/repo/contents/.github/orbi.toml") -> list[str]:
    command = ["gh", "api", "--method", "PUT", path]
    for field in fields:
        command += ["-f", field]
    return command


def test_contents_put_updates_the_repo_config(fake_gh):
    """The write the `milestone set` policy path needs: a contents PUT
    replaces the in-memory policy blob and answers the new commit sha."""
    fake_gh.set_repo_config('base_branch = "main"\n')
    written = 'base_branch = "main"\nactive_milestone = "v0.6.0"\n'
    encoded = base64.b64encode(written.encode("utf-8")).decode("ascii")
    result = fake_gh(_contents_put(
        "message=set active_milestone",
        f"content={encoded}",
        "sha=" + "0" * 40,
    ))
    assert fake_gh.repo_config == written
    assert json.loads(result)["commit"]["sha"] == "0" * 40


def test_contents_put_rejects_any_other_path(fake_gh):
    """Only the repository policy path is modelled: a mistyped write
    fails fast instead of silently updating the wrong file."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_gh(_contents_put(
            "message=x", "content=eA==", "sha=abc",
            path="repos/owner/repo/contents/README.md",
        ))
    assert "unsupported" in excinfo.value.stderr


def test_contents_put_rejects_a_non_field_argument(fake_gh):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_gh([
            "gh", "api", "--method", "PUT",
            "repos/owner/repo/contents/.github/orbi.toml",
            "-f", "message=x", "stray", "content=eA==",
        ])
    assert "unsupported" in excinfo.value.stderr


def test_contents_put_requires_the_three_contents_fields(fake_gh):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_gh(_contents_put("message=x", "content=eA=="))
    assert "unsupported" in excinfo.value.stderr


def test_contents_put_rejects_non_base64_content(fake_gh):
    fake_gh.set_repo_config('base_branch = "main"\n')
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_gh(_contents_put(
            "message=x", "content=not base64!", "sha=abc",
        ))
    assert "base64" in excinfo.value.stderr

def test_issue_list_honours_the_in_body_search_term(fake_gh):
    """The external-takeover scan's query is a body-text term (Issue
    #842: `"orbi:external-pr" in:body`); the fake must answer it, so the
    five claim scans the status view now runs (Issue #1773) are all real
    reads instead of a fail-open masking an unsupported query."""
    fake_gh.add_issue(1, body="please review\n<!-- orbi:external-pr:9 -->")
    fake_gh.add_issue(2, body="no marker here")
    for number in (1, 2):
        fake_gh.issues[number]["labels"] = [READY_LABEL]
    issues = github.list_issues(
        "owner/repo", state="open",
        search=f'label:{READY_LABEL} "orbi:external-pr" in:body',
        json_fields="number", limit=200,
    )
    assert [issue["number"] for issue in issues] == [1]


# --- `orbi status` reads the claim's ONE ready decision (Issue #1773) ------


def _status_config(tmp_path) -> RunnerConfig:
    """A hand-built host config for the status render.

    Only the fields the ready read and the slot line touch are set: the
    rest of the report's GitHub reads are injected by the helper below.
    """
    return RunnerConfig(
        source_repos=("owner/repo",), repo_dir=tmp_path,
        base_branch="main", max_concurrency=1,
        slot_dir=tmp_path / ".orbi" / "slots",
    )


def _status_ready_line(config: RunnerConfig) -> str:
    """Render the real status report over whatever `gh` seam is patched.

    The unrelated GitHub reads (active deliveries, recent result, frozen
    base) are injected exactly as `main` wires them, while the ready
    queue goes through the REAL `cli_status.ready_issue` ->
    `claim.pick_ready_issue` path with the fake `gh` behind it.
    """
    scope = observe.parse_scope(config.source_repos)
    observer = observe.StatusObserver(
        config, scope,
        read_issues=lambda repo: [],
        read_ready=lambda repo: cli_status.ready_issue(repo, config),
        read_result=lambda repo: None,
        read_base=lambda repo_dir, base_branch: "0" * 40,
    )
    observer.refresh()
    return next(
        line
        for line in cli_status.status_report(config, scope, observer).splitlines()
        if line.startswith("  ready:")
    )


def test_status_ready_names_the_issue_the_claim_would_pick(fake_gh, tmp_path):
    """Acceptance #1773: an open Issue carrying `ai-ready` + `ai-pr-opened`
    (every delivery carries both once it opens its PR) is NOT ready, and
    the plain `ai-ready` Issue behind it is.

    The delivery Issue is seeded last so `gh` lists it FIRST — the order
    that made the old hand-built query report it as ready.
    """
    fake_gh.add_issue(2647, title="next", labels=(READY_LABEL,))
    fake_gh.add_issue(
        2646, title="delivery awaiting review",
        labels=(READY_LABEL, PR_OPENED_LABEL),
    )
    config = _status_config(tmp_path)
    assert _status_ready_line(config) == "  ready: #2647 next"


@pytest.mark.parametrize(
    "state_label",
    [PR_OPENED_LABEL, FIX_NEEDED_LABEL, BLOCKED_LABEL, MERGED_LABEL],
)
def test_status_ready_never_shows_an_issue_in_a_delivery_state(
        fake_gh, tmp_path, state_label):
    """Acceptance #1773: none of the four result states is ready — the
    claim's exclusions decide, not a second query."""
    fake_gh.add_issue(2646, title="delivery", labels=(READY_LABEL, state_label))
    config = _status_config(tmp_path)
    assert _status_ready_line(config) == "  ready: -"
    assert claim.pick_ready_issue("owner/repo") is None


def test_status_ready_equals_the_claim_pick(fake_gh, tmp_path):
    """Acceptance #1773, criterion 1: for the same open Issues the status
    ready Issue IS the one the claim scan picks — one decision, two
    readers."""
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, P0_LABEL))
    fake_gh.add_issue(3, labels=(READY_LABEL, "bug"))
    fake_gh.add_issue(4, labels=(READY_LABEL, PR_OPENED_LABEL))
    fake_gh.add_issue(5, labels=(READY_LABEL, MERGED_LABEL))
    config = _status_config(tmp_path)
    picked = claim.pick_ready_issue(
        "owner/repo", config.active_milestone, config,
    )
    assert picked is not None
    assert _status_ready_line(config) == (
        f"  ready: #{picked['number']} {picked['title']}"
    )
    assert picked["number"] == 2


def test_status_ready_still_shows_a_plain_ready_issue(fake_gh, tmp_path):
    """Acceptance #1773, criterion 2: a plain `ai-ready` Issue is still
    reported as ready, exactly as before."""
    fake_gh.add_issue(9, title="plain ready", labels=(READY_LABEL,))
    config = _status_config(tmp_path)
    assert _status_ready_line(config) == "  ready: #9 plain ready"


def test_status_ready_keeps_the_claim_journal_out_of_the_view(
        fake_gh, tmp_path, caplog):
    """The status read is an OBSERVATION, not a claim: the claim's
    pick/skip journal lines never reach the view's own output."""
    fake_gh.add_issue(2646, title="delivery", labels=(READY_LABEL, PR_OPENED_LABEL))
    fake_gh.add_issue(2647, title="next", labels=(READY_LABEL,))
    config = _status_config(tmp_path)
    with caplog.at_level(logging.INFO):
        assert cli_status.ready_issue("owner/repo", config)["number"] == 2647
        assert "picked" not in caplog.text
        # The claim's own tick still journals its decision.
        claim.pick_ready_issue("owner/repo")
    assert "picked issue=2647" in caplog.text


# --- status and the claim decide on the SAME inputs (Issue #1782) -----------


def _hold_foreign_slot(config: RunnerConfig, repo: str, number: int) -> int:
    """A live FOREIGN co-runner holds slot 1 for `<repo>#<number>`.

    The second open file description conflicts with this process's own
    probes even inside one process, and the file records a foreign pid —
    the same real scene `slot_held_deliveries` reads (the flock is the
    token, the identity line names the delivery).
    """
    path = pilot_slots.slot_path(config.slot_dir, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, f"{os.getpid() + 1}\n{repo}#{int(number)}\n".encode("ascii"))
    return fd


def test_status_ready_skips_the_delivery_a_live_co_runner_holds(
        fake_gh, tmp_path):
    """Acceptance #1782, criterion 1: while a co-runner holds #1 in a
    slot and has not labelled it `ai-in-progress` yet, `orbi status`
    names #2 — the Issue the claim picks — instead of the held #1.

    #2 seeds first so `gh` lists #1 (the held one) FIRST: the order that
    made status name the Issue the claim would skip."""
    fake_gh.add_issue(2, title="next", labels=(READY_LABEL,))
    fake_gh.add_issue(
        1, title="held by a co-runner", labels=(READY_LABEL,),
    )
    config = _status_config(tmp_path)
    holder = _hold_foreign_slot(config, "owner/repo", 1)
    try:
        selected = claim.pick_next_delivery(
            ["owner/repo"], config.slot_dir, config.max_concurrency,
            config=config, hooks=resume_deps(),
        )
        assert selected is not None
        assert selected[1]["number"] == 2
        assert _status_ready_line(config) == "  ready: #2 next"
    finally:
        os.close(holder)


def _status_config_file(tmp_path) -> Path:
    """A real config file for the CLI path (the same host keys
    `_status_config` hands the observer in process)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")
    path = tmp_path / "orbi.toml"
    path.write_text(
        'source_repos = ["owner/repo"]\n'
        'base_branch = "main"\n'
        "max_concurrency = 1\n",
        encoding="utf-8",
    )
    return path


def _status_cli_observer(monkeypatch) -> None:
    """Wire the REAL observer, injecting only the open-Issue listing.

    `FakeGh` models the claim's reads (`gh issue list`, the Milestones
    API) but not the status view's REST issue listing, so the
    active-delivery read is injected exactly as the report helper does;
    the ready decision under test stays the real
    `cli_status.ready_issue` -> `claim.pick_ready_issue` path.
    """
    real = observe.StatusObserver

    def factory(config, scope, **kwargs):
        kwargs.setdefault("read_issues", lambda repo: [])
        return real(config, scope, **kwargs)

    monkeypatch.setattr(observe, "StatusObserver", factory)


def _break_milestone_reads(fake_gh, monkeypatch) -> None:
    """The Milestones API is down: only the release milestone read fails."""
    def api_outage(command, **kwargs):
        if command[1:2] == ["api"] and "milestones" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="gh: API error (fatal)",
            )
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", api_outage)


def _release_only_queue(fake_gh) -> None:
    fake_gh.add_issue(
        1, title="release", labels=(READY_LABEL, RELEASE_LABEL), milestone=1,
    )
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)


def _empty_milestone_reads(fake_gh, monkeypatch) -> None:
    """The Milestones read answers nothing: the adapter's own failed check
    (the Milestone is not on the results page — never a silent 0), which
    is not a non-zero exit but is still an unevaluated read."""
    def vanished(command, **kwargs):
        if command[1:2] == ["api"] and "milestones" in command[2]:
            return ""
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", vanished)


def test_status_reports_a_failed_release_milestone_read(
        fake_gh, tmp_path, monkeypatch, caplog):
    """Acceptance #1782, criterion 2: with only the release ticket ready,
    a failed Milestone read is a FAILED READ in the view — never
    `ready: -` — while the claim still skips the release this tick."""
    _release_only_queue(fake_gh)
    _break_milestone_reads(fake_gh, monkeypatch)
    config = _status_config(tmp_path)
    with pytest.raises(observe.READ_ERRORS, match="milestones"):
        _status_ready_line(config)
    with caplog.at_level(logging.ERROR):
        assert claim.pick_ready_issue(
            "owner/repo", config.active_milestone, config,
        ) is None
    assert "release_milestone_check_failed issue=1" in caplog.text


def test_main_status_reports_a_failed_release_milestone_read(
        fake_gh, tmp_path, monkeypatch, capsys):
    """Acceptance #1782, criterion 2 through the user's own command:
    `orbi status` names the failed read and exits non-zero; it never
    renders the empty `ready: -` queue the failed read used to become."""
    _release_only_queue(fake_gh)
    _break_milestone_reads(fake_gh, monkeypatch)
    monkeypatch.setattr(
        seam, "freeze_base", lambda repo_dir, base_branch: "0" * 40,
    )
    _status_cli_observer(monkeypatch)
    config_file = _status_config_file(tmp_path)
    assert cli.main(["status", "--config", str(config_file)]) == 1
    captured = capsys.readouterr()
    assert "status_failed scope=owner/repo" in captured.err
    assert "milestones" in captured.err
    assert "ready:" not in captured.out


def test_main_status_reports_a_missing_release_milestone_as_a_failed_read(
        fake_gh, tmp_path, monkeypatch, capsys):
    """Acceptance #1782, criterion 2: the adapter's own failed check (the
    Milestone cannot be found — never a silent 0) is a failed read too,
    not a traceback and not an empty queue."""
    _release_only_queue(fake_gh)
    _empty_milestone_reads(fake_gh, monkeypatch)
    monkeypatch.setattr(
        seam, "freeze_base", lambda repo_dir, base_branch: "0" * 40,
    )
    _status_cli_observer(monkeypatch)
    config_file = _status_config_file(tmp_path)
    assert cli.main(["status", "--config", str(config_file)]) == 1
    captured = capsys.readouterr()
    assert "status_failed scope=owner/repo" in captured.err
    assert "not found" in captured.err
    assert "ready:" not in captured.out


def test_status_ready_and_the_claim_agree_without_a_holder(fake_gh, tmp_path):
    """Acceptance #1782, criterion 3: no live holder and healthy reads —
    status and the claim pick the same Issue, as before."""
    fake_gh.add_issue(2, title="next", labels=(READY_LABEL,))
    fake_gh.add_issue(1, title="first", labels=(READY_LABEL,))
    config = _status_config(tmp_path)
    selected = claim.pick_next_delivery(
        ["owner/repo"], config.slot_dir, config.max_concurrency,
        config=config, hooks=resume_deps(),
    )
    assert selected is not None
    assert _status_ready_line(config) == (
        f"  ready: #{selected[1]['number']} {selected[1]['title']}"
    )
