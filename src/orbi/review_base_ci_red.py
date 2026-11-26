"""The review tick's base-branch-red stop (Issue #2442).

`base_ci_red` (Issue #1825) already owns the pause: one fix Issue per base
branch, the machine-readable record, the automatic recovery. It used to be
reachable from ONE place only — the merge gate, which runs AFTER a review
`pass`, and a review cannot `pass` on red checks. So with a red base the
comparison was never made: the reviewer found the base's own failure, may not
fix unrelated code, and the delivery stopped at
`human_decision_required` with no record, no fix Issue and no way back
without a person relabelling all of that repository's paused PRs.

This module is the second caller, placed BEFORE the session in the tick's
own order (absorb a red, behind-only head -> this stop -> review):

1. The question "is this red PR red because of its base?" has ONE answer:
   `failure_report._raise_if_preexisting_ci_failure` -> `base_ci_red.detect`,
   the same detection the merge gate gets, on the base commit the head
   actually contains at that moment (never only the `base_oid` frozen when
   the round opened, so a base that turned red afterwards is still seen).
2. A base whose matching check has not concluded yet is not green: the tick
   neither records the pause nor starts a session on that evidence, it
   defers and looks again on the next tick.
3. A failed read of the base's checks is left to propagate: the tick's
   classified reporter names the read failure, and it never becomes
   `base_ci_red` and never silently becomes "the base is green".

Nothing here posts, labels or merges: the pause is recorded by the existing
classified failure reporter, so the record, the comment, the fix Issue and
the recovery keep exactly one implementation.
"""
from __future__ import annotations

from orbi import base_ci_red, failure_report
from orbi.journal import event


def stop_before_session(
    pr: dict, base_branch: str, *, source_repo: str,
    failed: tuple[str, ...], base_tip: str | None,
) -> bool:
    """End the tick when this red PR is red because its base is.

    `failed` is the PR's failing check names and `base_tip` the base commit
    the head contains; both come from the tick's ONE PR read, and either
    being empty means the read did not establish them — the caller then
    reviews as it always did.

    True means the tick ends here without a session: the base shares a
    failed check (the shared detection raises `PreExistingCIFailure`, which
    the review round reports as the `base_ci_red` pause, its comment saying
    that no review ran yet), or the base's matching check is still running
    and the next tick decides. False means the base is green on every failed
    check and the review runs as today.
    """
    if not failed or not base_tip:
        return False
    failure_report._raise_if_preexisting_ci_failure(
        source_repo, list(failed), base_tip,
        base_branch=base_branch, pr_number=pr["number"], reviewed=False,
    )
    pending = base_ci_red.pending_checks(
        source_repo, list(failed), base_tip,
    )
    if not pending:
        return False
    event(
        "review_base_checks_pending", pr=pr["number"],
        base_branch=base_branch, base_sha=base_tip,
        checks=", ".join(pending),
    )
    return True
