#!/usr/bin/env python3
"""A PR frozen on a base branch the repository no longer uses (Issue #2458).

The resume scene freezes the base branch the PR was opened against. A
repository's `base_branch` can move while that PR is still in review; from
then on the scene and the configuration disagree, and resuming, reviewing or
merging the PR against the configured base would run every later step
(verification, review, merge gate) on the wrong base. Stopping is the
decision and it does not change (terminal, `ai-blocked`, never an automatic
retry against the mismatch). What this module owns is that stop's IDENTITY:
its own `base_branch_changed` reason code, so a status reader can show the
specific sentence instead of "something needs your decision", and the ONE
reader-facing sentence pair that names both branches, the PR and the two
ways out, in the run's message language (Issue #2458).

`require_scene_base` is the single check both resume paths call — the resume
verification before any git/Pi mutation and the review round before its
session — so the two can never disagree on when a delivery stops, and
`wording` is the single sentence pair they report. Stopping keeps its
existing shape: the exception carries the record (the
`BaseBranchMissingError` pattern of Issue #1756) and stays an
`UnrecoverableDeliveryError`, so the shared reporter marks the Issue
`ai-blocked` alone and preserves the PR, branch and worktree.

It lives in its own module because a module recorded by the size ratchet may
only shrink and `failure`, `failure_report` and `review_merge` are at their
ceilings (`tools/size_ratchet.py` — the same reason `terminal_stops` and
`provider_balance` exist). It imports `failure_report` and nothing imports
it back, so there is no cycle.
"""
from __future__ import annotations

from orbi import failure, language
from orbi.failure_report import UnrecoverableDeliveryError

# Closed `failure` set member: why the delivery stopped, and the human
# decision it waits for (never a plain re-queue, which hits the mismatch).
REASON_CODE = "base_branch_changed"


class SceneBaseBranchChanged(UnrecoverableDeliveryError):
    """The PR is frozen on a base the repository is no longer configured with.

    `scene_base_branch` is the base recovered from the delivery scene (what
    the PR was opened against), `configured_base_branch` the base the
    repository config asks for now. Both are named in the stop comment, so
    the reader sees which branch is which.
    """

    def __init__(self, scene_base_branch: str,
                 configured_base_branch: str) -> None:
        super().__init__(
            f"resume scene base_branch={scene_base_branch} differs from "
            f"configured base_branch={configured_base_branch}; the PR is "
            "frozen on a different base and must not be resumed against the "
            "configured one — a base change is a human decision, so "
            "auto-retrying would keep failing on the same mismatch"
        )
        self.scene_base_branch = scene_base_branch
        self.configured_base_branch = configured_base_branch
        # Decided at the source (Issue #1756): the repair is a human's, so no
        # consumer re-derives it from the message text.
        self.failure_record = failure.Failure(
            REASON_CODE, *failure.DISPOSITIONS[REASON_CODE],
        )


def require_scene_base(scene_base_branch: str,
                       configured_base_branch: str) -> None:
    """Stop when the PR's frozen base is not the configured base branch."""
    if scene_base_branch != configured_base_branch:
        raise SceneBaseBranchChanged(
            scene_base_branch, configured_base_branch,
        )


def wording(error: SceneBaseBranchChanged, language_code: str, *,
            pr_url: str | None) -> tuple[str, str]:
    """The reader-facing `(action, reason)` pair, in the run's language.

    The reason names both branches and the PR; the action gives the two ways
    out — put the base back so this PR continues, or close the PR so the
    Issue is redone from the new base — so the reader never has to work out
    which branch is which (`failure.reason_label` / `failure.action_label`
    render them).
    """
    fields = {
        "pr_url": pr_url or "-",
        "scene_base_branch": error.scene_base_branch,
        "configured_base_branch": error.configured_base_branch,
    }
    cause = language.text(
        language_code, "failure.base_branch_changed_cause", **fields)
    return (
        language.text(
            language_code, "failure.base_branch_changed_action",
            cause=cause, **fields,
        ),
        cause,
    )
