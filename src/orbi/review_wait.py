"""A delivery that passed review and only waits for its own checks (#2305).

The #1626 review record is the one condition: a clean verdict records the
reviewed head, and every deferral (the review session's own push, the
merge gate's base absorb, pending CI) keeps that record while it waits.
This module owns that record — moved here from `orbi.run_state` (Article
3.5: the domain that grows owns the state it grows with) — the clock the
record stamps (`reviewed_at`, the LOCAL clock: it measures this machine's
wait, never GitHub's timestamps), and the bounded claim hold Issue #2305
adds: while the record's head is the PR's current head, its checks are
running and none failed, and the record is younger than
`REVIEW_WAITING_SECONDS`, the claim scan keeps the runner's last free
slot free for that merge instead of starting a new implementation (the
bounded exception to Issue #1473).

The module imports `orbi.run_state` for the state file it lives in and
never the other way round (no cycle); `orbi.resume_scan` (the opened-PR
scan, whose CI-pending skip and Issue #2304 waiting line ride the same one
head + rollup read) and `orbi.claim` (the fresh-claim hold) read it.
"""
from __future__ import annotations

import time
from pathlib import Path

from orbi import restart_scan, run_state, waiting_checks
from orbi.checks import classify_checks
from orbi.github import pr_number, pr_view
from orbi.journal import LOGGER, event
from orbi.pilot_slots import slot_occupancy


#: Issue #2305: how long a delivery that passed review and only waits for
#: the checks on its latest head keeps the runner's last free slot free.
#: Measured from the record's `reviewed_at`; the last 60 PR check runs
#: (completion minus run creation) had a median of about 7 minutes and a
#: maximum of about 13, so with the 5-minute timer the hold is released on
#: about the fourth tick — the slot idles at most about 20 minutes before
#: the claim proceeds exactly as it did before.
REVIEW_WAITING_SECONDS = 15 * 60


# --------------------------------------------------------------------------
# The #1626 review record, now carrying its own clock (#2305)
# --------------------------------------------------------------------------


def _review_record_state(worktree: Path) -> dict | None:
    """The run state carrying the review record, or None when unreadable.

    Issue #1626: a missing file means no record exists and a corrupt one is
    already a delivery failure on the resume readers, so neither may fail
    the record's own lifecycle (the safe direction is always "no record":
    the next tick runs a full review).
    """
    try:
        return run_state.read_run_state(worktree)
    except ValueError:
        return None


def record_reviewed(worktree: Path, head: str,
                    issue_body_sha256: str, *,
                    now: float | None = None) -> None:
    """Record the reviewed head a deferred merge may resume against.

    Issue #1626: written ONCE per clean verdict, before the merge gate.
    A later tick with a green CI, the same PR head and the same rendered
    Issue body merges without another review session. A missing run state
    (a recreated worktree) records nothing, and the next tick reviews.

    `reviewed_at` is the record's own clock (Issue #2305): the local
    epoch second this deployment passed review, so the claim scan can
    bound how long a delivery that only waits for its checks keeps the
    runner's last free slot. `now` is injectable for tests only.
    """
    state = _review_record_state(worktree)
    if state is None:
        return
    state["reviewed_head"] = head
    state["reviewed_issue_body_sha256"] = issue_body_sha256
    state["reviewed_at"] = time.time() if now is None else now
    run_state.write_run_state_file(worktree, state)


def clear_reviewed(worktree: Path) -> None:
    """Delete the review record from the run state.

    Issue #1626: called before every review session and on every
    review_and_merge_if_clean exit except a DeliveryDeferred, so a stale
    pass (or one a session wrote) can never authorize a later merge.
    """
    state = _review_record_state(worktree)
    if state is None:
        return
    if ("reviewed_head" not in state
            and "reviewed_issue_body_sha256" not in state
            and "reviewed_at" not in state):
        return
    state.pop("reviewed_head", None)
    state.pop("reviewed_issue_body_sha256", None)
    state.pop("reviewed_at", None)
    run_state.write_run_state_file(worktree, state)


def update_reviewed_head(worktree: Path, head: str, *,
                         now: float | None = None) -> None:
    """Point an EXISTING review record at the merge gate's absorbed head.

    Issue #1626: the gate's own conflict-free base absorb pushes a new head.
    Only a record that already exists (a reviewed pass, not the
    ai-awaiting-merge path) is updated; with no record this is a no-op.
    The absorbed head starts a new CI run, so `reviewed_at` is stamped
    again (Issue #2305): the new head's wait is what the claim scan
    bounds. `now` is injectable for tests only.
    """
    state = _review_record_state(worktree)
    if state is None:
        return
    if not _has_reviewed(state):
        return
    state["reviewed_head"] = head
    state["reviewed_at"] = time.time() if now is None else now
    run_state.write_run_state_file(worktree, state)


def read_reviewed(worktree: Path) -> tuple[str, str] | None:
    """The recorded reviewed head and rendered-Issue-body hash, or None.

    Issue #1626: both fields must be present and non-empty; a partial or
    corrupt record authorizes nothing and the next tick reviews.
    """
    state = _review_record_state(worktree)
    if state is None or not _has_reviewed(state):
        return None
    return state["reviewed_head"], state["reviewed_issue_body_sha256"]


def read_review_record(worktree: Path) -> tuple[str, float] | None:
    """The reviewed head and when it was recorded (local clock), or None.

    Issue #2305: the same full record `read_reviewed` requires, plus its
    `reviewed_at` stamp. A record without the stamp (written before
    #2305) authors no wait: the claim scans as it always did.
    """
    state = _review_record_state(worktree)
    if state is None or not _has_reviewed(state):
        return None
    reviewed_at = state.get("reviewed_at")
    if not isinstance(reviewed_at, (int, float)) or isinstance(reviewed_at, bool):
        return None
    return state["reviewed_head"], float(reviewed_at)


def _has_reviewed(state: dict) -> bool:
    head = state.get("reviewed_head")
    body_sha = state.get("reviewed_issue_body_sha256")
    return (isinstance(head, str) and bool(head)
            and isinstance(body_sha, str) and bool(body_sha))


# --------------------------------------------------------------------------
# The one PR read and the waiting condition (#2305)
# --------------------------------------------------------------------------


def pr_delivery_head_rollup(
    pr_url: str, source_repo: str,
) -> tuple[str, str | None, list]:
    """PR state, the head the rollup belongs to, and the rollup (ONE read).

    Issue #2305: the claim scan compares a delivery's reviewed head with
    the CURRENT PR head to tell a delivery that only waits for its checks
    from one whose head moved. Asking the same `gh pr view` that already
    reads the rollup costs no second request, and the head and the checks
    it describes can never come from two different reads.
    """
    number = pr_number(pr_url)
    data = pr_view(
        number, "state,headRefOid,statusCheckRollup", repo=source_repo,
    )
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    rollup = data.get("statusCheckRollup")
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list):
        raise ValueError("pr statusCheckRollup must be a JSON array")
    head = data.get("headRefOid")
    return state, (head if isinstance(head, str) and head else None), rollup


def review_wait_age(
    repo_dir: Path | None, source_repo: str, number: int, head: str | None,
    *, now: float | None = None,
) -> float | None:
    """How long this CI-pending candidate has waited only for its checks.

    Issue #2305: the delivery has a #1626 review record in THIS
    deployment's worktree for this Issue, its reviewed head is the PR's
    current head, and the record is younger than `REVIEW_WAITING_SECONDS`.
    Returns the local-clock age in seconds, or None when the candidate is
    no such delivery: another deployment owns it (no local worktree), the
    head moved since the reviewed pass, no review record exists, or the
    record is past the bound. `now` is injectable for tests only.
    """
    if repo_dir is None or head is None:
        return None
    try:
        found = restart_scan.worktree_resume_scene(repo_dir, source_repo, number)
    except RuntimeError as exc:
        # This deployment holds a worktree of the Issue but cannot verify
        # its run: the wait is unproven, so the claim proceeds as it did
        # before #2305 rather than failing the tick.
        LOGGER.warning(
            "the review wait age of issue=%s is unknown: %s", number, exc,
        )
        return None
    if found is None:
        return None
    record = read_review_record(found[1])
    if record is None:
        return None
    reviewed_head, reviewed_at = record
    if reviewed_head != head:
        return None
    current = time.time() if now is None else now
    age = current - reviewed_at
    if age > REVIEW_WAITING_SECONDS:
        return None
    return age


def skip_pending_candidate(
    review_waiting: list[dict] | None, repo_dir: Path | None,
    source_repo: str, issue: dict, scene_value, language_code: str,
    status_url: str | None = None,
) -> bool:
    """Whether the opened-PR scan must skip this candidate as CI-pending.

    Issue #1473's skip, Issue #2305's observation and Issue #2304's waiting
    line ride the ONE head + rollup read: a pending check on an OPEN PR is
    what the scan skips and what the Issue says Orbi waits for, and — when
    no check failed and this deployment's review record waits on that same
    head — the wait artifact joins `review_waiting`, so the claim decides
    whether the last free slot is held for it. A terminal PR, a completed
    rollup or a failed read returns False, so the candidate still reaches
    the runner's own gate.
    """
    number = int(issue["number"])
    pr_url = scene_value.parsed.pr_url
    try:
        pr_state, head, rollup = pr_delivery_head_rollup(pr_url, source_repo)
    except Exception:
        return False
    if pr_state != "OPEN":
        return False
    pending, failed, _passed = classify_checks(rollup)
    if not pending:
        return False
    waiting_checks.defer_skipped(
        scene_value, pending, issue, source_repo, language_code, status_url,
        head=head,
    )
    if not failed and review_waiting is not None:
        age = review_wait_age(repo_dir, source_repo, number, head)
        if age is not None:
            review_waiting.append({
                "issue": number, "pr": pr_url, "head": head, "age": int(age),
            })
    return True


def hold_fresh_claim(
    review_waiting: list[dict], slot_dir: Path, max_concurrency: int,
) -> bool:
    """Whether this tick's fresh claim must be held for a waiting delivery.

    Issue #2305: only when a waiting delivery exists AND the claim would
    leave the machine without a free slot. `main` took this tick's slot
    before the claim scan (`pilot_slots.slot_occupancy` already counts
    it), so any slot reported free is capacity a later tick could merge
    the waiting delivery from; with none left, a fresh implementation
    would block that merge for its whole run. Every held delivery is
    journaled (`review_waiting_delivery`) so an idle tick is explainable,
    and the caller returns without claiming.
    """
    if not review_waiting:
        return False
    if any(
        holder is None
        for _index, holder in slot_occupancy(slot_dir, max_concurrency)
    ):
        return False
    for waiting in review_waiting:
        event("review_waiting_delivery", **waiting)
    return True
