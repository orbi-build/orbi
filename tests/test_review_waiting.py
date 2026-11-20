"""A delivery that only waits for its checks holds the fresh claim (#2305).

The one condition is the #1626 review record in `.orbi/run-state.json`: its
reviewed head equals the PR's current head, at least one check is in
progress, none failed, and the record's own `reviewed_at` is younger than
`REVIEW_WAITING_SECONDS`. Only the FRESH claim is held, and only when the
claim would leave the machine without a free slot; the restart resume, the
awaiting-merge scan, an empty rollup, a failed check and a second free slot
all behave exactly as before.
"""
import json
import os
import time

import pytest

import orbi.claim as claim
import orbi.pilot_slots as pilot_slots
import orbi.review_wait as review_wait
import orbi.run_state as run_state
from orbi.config import RunnerConfig
from orbi.delivery_labels import PR_OPENED_LABEL
from orbi.delivery_scene import RunContext
from seam import resume_deps
from tests.fakes.review_wait import (
    Scene, bind_awaiting_merge, bind_claim_tick, bind_gh, bind_pr_read,
    bind_pr_read_failure,
)
from tests.test_resume_pr import (
    FAKE_PR_URL, gh_comments_payload, issue_payload, make_pick_fake,
    opened_pr_comment,
)

RUN_ID = "8066a2db"
REVIEW_HEAD = "a" * 40
MOVED_HEAD = "c" * 40
BODY_SHA = "b" * 64
PENDING = {"name": "tests", "status": "IN_PROGRESS", "conclusion": None}
FAILED = {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}


# --------------------------------------------------------------------------
# Fixtures: a delivery worktree with a review record, and the tick's scans
# --------------------------------------------------------------------------


def _branch(repo: str, number: int) -> str:
    return f"orbi/{repo.replace('/', '-')}-issue-{number}"


def _delivery(
    tmp_path, *, number: int = 9, repo: str = "owner/repo",
    reviewed_head: str = REVIEW_HEAD, reviewed_at: float | None = None,
):
    """This deployment's worktree for `number`, with a #1626 pass record."""
    worktree = (
        tmp_path / ".worktrees"
        / f"orbi-{repo.replace('/', '-')}-issue-{number}-{RUN_ID}"
    )
    run_state.write_run_state(RunContext(
        run_id=RUN_ID, issue=number, branch=_branch(repo, number),
        worktree=worktree, source_repo=repo,
    ))
    review_wait.record_reviewed(
        worktree, reviewed_head, BODY_SHA, now=reviewed_at,
    )
    return worktree


def _config(tmp_path, *, max_concurrency: int = 1) -> RunnerConfig:
    return RunnerConfig(
        config_path=tmp_path / "orbi.toml",
        source_repos=("owner/repo",),
        repo_dir=tmp_path,
        slot_dir=tmp_path / ".orbi" / "slots",
        workspace_root=tmp_path,
        prompt=tmp_path / "prompts" / "prompt.md",
        prompt_review=tmp_path / "prompts" / "prompt_review.md",
        max_concurrency=max_concurrency,
    )


def _stub_ready(monkeypatch) -> list:
    """`pick_ready_issue` records its calls instead of reading GitHub."""
    calls: list = []

    def ready(*args, **kwargs):
        calls.append((args, kwargs))
        return {"number": 10, "title": "next"}

    monkeypatch.setitem(claim.__dict__, "pick_ready_issue", ready)
    return calls


def _opened_pr_reads(monkeypatch, *, head=REVIEW_HEAD, rollup=None) -> None:
    """The opened-PR scan's two reads: the candidate query and the PR.

    The PR read is the REAL one (`review_wait.pr_delivery_head_rollup` ->
    `gh pr view`), answered by the fake `gh`: the same single request
    carries the head and the rollup, so the test proves the shipped read.
    """
    bind_gh(monkeypatch, make_pick_fake(
        issue_payload(labels=[PR_OPENED_LABEL]),
        gh_comments_payload([opened_pr_comment()]),
        pr_head=head,
        pr_rollup=[PENDING] if rollup is None else rollup,
    ))


def _tick(tmp_path, config, monkeypatch) -> tuple:
    """One claim scan with this tick's slot taken (as `main` takes it)."""
    slot = pilot_slots.acquire_slot(
        config.slot_dir, config.max_concurrency, os.getpid(),
    )
    assert slot is not None
    try:
        return claim.pick_next_delivery(
            list(config.source_repos), config.slot_dir,
            config.max_concurrency, config=config, hooks=resume_deps(),
        )
    finally:
        slot.release()


# --------------------------------------------------------------------------
# The record's clock (orbi.review_wait)
# --------------------------------------------------------------------------


def test_the_review_record_carries_its_local_clock(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    assert review_wait.read_review_record(worktree) == (REVIEW_HEAD, 1000.0)


def test_the_absorb_writer_restamps_the_record_clock(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    review_wait.update_reviewed_head(worktree, MOVED_HEAD, now=2000.0)
    assert review_wait.read_review_record(worktree) == (MOVED_HEAD, 2000.0)


def test_clearing_the_record_drops_the_clock(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    review_wait.clear_reviewed(worktree)
    assert review_wait.read_review_record(worktree) is None
    state = json.loads(run_state.run_state_path(worktree).read_text())
    assert "reviewed_at" not in state


def test_a_refreshed_run_state_keeps_the_record_clock(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    run_state.write_run_state(RunContext(
        run_id=RUN_ID, issue=9, branch=_branch("owner/repo", 9),
        worktree=worktree, source_repo="owner/repo",
    ))
    assert review_wait.read_review_record(worktree) == (REVIEW_HEAD, 1000.0)


def test_a_record_written_before_issue_2305_times_nothing(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    path = run_state.run_state_path(worktree)
    state = json.loads(path.read_text())
    state.pop("reviewed_at")
    path.write_text(json.dumps(state), encoding="utf-8")
    # The #1626 record still authorizes a merge-only resume, but it authors
    # no wait: the claim scans exactly as it did before #2305.
    assert review_wait.read_reviewed(worktree) == (REVIEW_HEAD, BODY_SHA)
    assert review_wait.read_review_record(worktree) is None


def test_the_record_lifecycle_waits_for_no_state_file(tmp_path):
    """A recreated worktree (no state file) records, clears and updates
    nothing — the safe direction is always "no record"."""
    worktree = tmp_path / "recreated"
    worktree.mkdir()
    review_wait.record_reviewed(worktree, REVIEW_HEAD, BODY_SHA, now=1.0)
    review_wait.update_reviewed_head(worktree, MOVED_HEAD, now=2.0)
    review_wait.clear_reviewed(worktree)
    assert run_state.run_state_path(worktree).exists() is False
    assert review_wait.read_review_record(worktree) is None


def test_a_corrupt_record_authors_no_wait(tmp_path):
    worktree = _delivery(tmp_path, reviewed_at=1000.0)
    run_state.run_state_path(worktree).write_text("{not json", encoding="utf-8")
    assert review_wait.read_review_record(worktree) is None
    assert review_wait.read_reviewed(worktree) is None
    review_wait.clear_reviewed(worktree)
    assert run_state.run_state_path(worktree).read_text() == "{not json"


def test_the_pr_read_carries_the_head_in_the_same_request(monkeypatch):
    reads = bind_pr_read(monkeypatch, {
        "state": "OPEN", "headRefOid": REVIEW_HEAD,
        "statusCheckRollup": [PENDING],
    })
    assert review_wait.pr_delivery_head_rollup(
        FAKE_PR_URL, "owner/repo",
    ) == ("OPEN", REVIEW_HEAD, [PENDING])
    assert reads == [(9, "state,headRefOid,statusCheckRollup", "owner/repo")]


# --------------------------------------------------------------------------
# The wait age: the one condition, its bound, and what is never a wait
# --------------------------------------------------------------------------


def test_a_pending_check_on_the_reviewed_head_is_a_wait(tmp_path):
    _delivery(tmp_path, reviewed_at=1000.0)
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD, now=1500.0,
    ) == 500.0


def test_the_bound_is_the_record_age(tmp_path):
    _delivery(tmp_path, reviewed_at=1000.0)
    bound = review_wait.REVIEW_WAITING_SECONDS
    assert review_wait.REVIEW_WAITING_SECONDS == 15 * 60
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD, now=1000.0 + bound,
    ) == bound
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD, now=1000.0 + bound + 1,
    ) is None


def test_a_moved_head_is_no_wait(tmp_path):
    _delivery(tmp_path)
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, MOVED_HEAD,
    ) is None


def test_another_deployments_delivery_is_no_wait(tmp_path):
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD,
    ) is None


def test_an_unverifiable_own_worktree_is_no_wait(tmp_path, caplog):
    """A matching worktree with no state file proves nothing: the wait is
    unproven, so the claim proceeds as it did before #2305."""
    (tmp_path / ".worktrees" / f"orbi-owner-repo-issue-9-{RUN_ID}").mkdir(
        parents=True,
    )
    caplog.set_level("WARNING")
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD,
    ) is None
    assert "issue=9" in caplog.text


def test_a_delivery_without_a_review_record_is_no_wait(tmp_path):
    worktree = _delivery(tmp_path)
    review_wait.clear_reviewed(worktree)
    assert review_wait.review_wait_age(
        tmp_path, "owner/repo", 9, REVIEW_HEAD,
    ) is None


# --------------------------------------------------------------------------
# The opened-PR scan's one read: what is, and is never, a waiting candidate
# --------------------------------------------------------------------------


def _pending_scan(monkeypatch, state="OPEN", rollup=None, head=REVIEW_HEAD):
    reads = bind_pr_read(monkeypatch, {
        "state": state, "headRefOid": head,
        "statusCheckRollup": [PENDING] if rollup is None else rollup,
    })
    return reads


def _skip(waiting, tmp_path):
    """Run the scan's one skip decision for issue 9 of the fake PR."""
    return review_wait.skip_pending_candidate(
        waiting, tmp_path, "owner/repo", {"number": 9},
        Scene(FAKE_PR_URL), "en",
    )


def test_a_pending_candidate_with_a_failed_check_is_skipped_not_waited(
    monkeypatch, tmp_path, caplog,
):
    """Acceptance: a failed check is no wait — the candidate is still
    skipped for this tick (Issue #1473) but never holds the last free slot,
    and the skip still journals the pending state (Issue #2304)."""
    _delivery(tmp_path)
    _pending_scan(monkeypatch, rollup=[PENDING, FAILED])
    waiting: list = []
    caplog.set_level("INFO")
    assert _skip(waiting, tmp_path) is True
    assert waiting == []
    assert "delivery_ci_pending" in caplog.text


def test_a_pending_candidate_without_a_wait_list_is_just_skipped(
    monkeypatch, tmp_path,
):
    """The awaiting-merge scan shares the read and reports no wait."""
    _delivery(tmp_path)
    _pending_scan(monkeypatch)
    assert _skip(None, tmp_path) is True


def test_a_terminal_pr_is_never_skipped(monkeypatch, tmp_path):
    _delivery(tmp_path)
    _pending_scan(monkeypatch, state="MERGED")
    waiting: list = []
    assert _skip(waiting, tmp_path) is False
    assert waiting == []


def test_a_failed_pr_read_returns_the_candidate(monkeypatch, tmp_path):
    _delivery(tmp_path)
    bind_pr_read_failure(monkeypatch)
    assert _skip([], tmp_path) is False


def test_a_pending_candidate_this_deployment_cannot_time_is_no_wait(
    monkeypatch, tmp_path,
):
    """Another deployment's pending delivery is still skipped (Issue #1473)
    but holds no slot: with no local worktree its wait is unproven."""
    _pending_scan(monkeypatch)
    waiting: list = []
    assert _skip(waiting, tmp_path) is True
    assert waiting == []


def test_the_read_without_a_head_carries_none(monkeypatch):
    """A rollup with no `headRefOid` authors no wait: the record can never
    match a head the read did not report."""
    bind_pr_read(monkeypatch, {"state": "OPEN", "statusCheckRollup": [PENDING]})
    assert review_wait.pr_delivery_head_rollup(
        FAKE_PR_URL, "owner/repo",
    ) == ("OPEN", None, [PENDING])


def test_an_empty_wait_list_holds_nothing(tmp_path):
    assert review_wait.hold_fresh_claim([], tmp_path / "slots", 1) is False


# --------------------------------------------------------------------------
# The claim: the last free slot waits, everything else is unchanged
# --------------------------------------------------------------------------


def test_the_scan_reports_the_delivery_that_only_waits_for_its_checks(
    monkeypatch, tmp_path,
):
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    waiting: list = []
    assert claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, repo_dir=tmp_path,
        hooks=resume_deps(), language_code="en", review_waiting=waiting,
    ) is None
    assert len(waiting) == 1
    assert waiting[0]["issue"] == 9
    assert waiting[0]["pr"] == FAKE_PR_URL
    assert waiting[0]["head"] == REVIEW_HEAD
    assert waiting[0]["age"] >= 0


def test_the_last_free_slot_is_held_for_a_waiting_delivery(
    monkeypatch, tmp_path, caplog,
):
    """Acceptance: record present, head matches, a check in progress, no
    failure, within 15 minutes — `pick_ready_issue` is not called and the
    tick ends without a claim."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    ready = _stub_ready(monkeypatch)
    config = _config(tmp_path)
    caplog.set_level("INFO")
    assert _tick(tmp_path, config, monkeypatch) is None
    assert ready == []
    assert f"review_waiting_delivery issue=9 pr={FAKE_PR_URL}" in caplog.text


def test_an_awaiting_merge_delivery_still_merges_while_the_claim_is_held(
    monkeypatch, tmp_path,
):
    """Issue #2305 holds the FRESH claim only: a delivery a maintainer
    already answered (`ai-awaiting-merge`) is re-checked on this very
    tick instead of waiting the hold out."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    answered = ({"number": 5, "title": "answered"}, {"merge_only": True})
    bind_awaiting_merge(monkeypatch, answered)
    ready = _stub_ready(monkeypatch)
    assert _tick(tmp_path, _config(tmp_path), monkeypatch) == (
        "owner/repo", answered[0], answered[1],
    )
    assert ready == []


def test_past_the_bound_the_ready_issue_is_claimed(monkeypatch, tmp_path):
    """Acceptance: past 15 minutes a ready Issue is claimed."""
    _delivery(
        tmp_path,
        reviewed_at=time.time() - review_wait.REVIEW_WAITING_SECONDS - 1,
    )
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    ready = _stub_ready(monkeypatch)
    selected = _tick(tmp_path, _config(tmp_path), monkeypatch)
    assert selected is not None and selected[1]["number"] == 10
    assert len(ready) == 1


def test_a_failed_check_keeps_the_delivery_on_its_failure_path(
    monkeypatch, tmp_path,
):
    """Acceptance: a failed check is no wait — the original delivery is
    selected (its merge gate owns the failure), never held."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch, rollup=[FAILED])
    bind_claim_tick(monkeypatch)
    ready = _stub_ready(monkeypatch)
    selected = _tick(tmp_path, _config(tmp_path), monkeypatch)
    assert selected is not None
    assert selected[1]["number"] == 9
    assert selected[2] is not None
    assert ready == []


def test_an_empty_check_list_is_not_held(monkeypatch, tmp_path):
    """Acceptance: an empty rollup never counts (repositories without CI
    are never held)."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch, rollup=[])
    bind_claim_tick(monkeypatch)
    ready = _stub_ready(monkeypatch)
    selected = _tick(tmp_path, _config(tmp_path), monkeypatch)
    assert selected is not None and selected[1]["number"] == 9
    assert ready == []


def test_another_free_slot_claims_as_today(monkeypatch, tmp_path):
    """Acceptance: with max_concurrency > 1 a free slot remains, so the
    ready Issue is claimed as today."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    ready = _stub_ready(monkeypatch)
    selected = _tick(tmp_path, _config(tmp_path, max_concurrency=2), monkeypatch)
    assert selected is not None and selected[1]["number"] == 10
    assert len(ready) == 1


def test_a_restart_resume_is_not_held(monkeypatch, tmp_path):
    """Acceptance: resuming this deployment's in-flight Issue is never
    held by a waiting delivery."""
    _delivery(tmp_path)
    _opened_pr_reads(monkeypatch)
    bind_claim_tick(monkeypatch)
    in_flight = {"number": 7, "title": "killed run"}
    monkeypatch.setitem(
        claim.__dict__, "pick_in_progress_issue", lambda *a, **k: in_flight,
    )
    ready = _stub_ready(monkeypatch)
    assert _tick(tmp_path, _config(tmp_path), monkeypatch) == (
        "owner/repo", in_flight, None,
    )
    assert ready == []
