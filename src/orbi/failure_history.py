"""The failure-comment history scans (Issues #825 and #1351).

The classified failure path reads the Issue's TRUSTED comment history to
answer two questions:

- is this exact `(run_id, failure fingerprint)` failure already reported
  (the #825 dedup), and how many CONSECUTIVE times has it recurred (the
  #825 dead-loop streak)?
- has a `github_transient` failure already happened on this Issue (the
  #1351 one-shot automatic-retry budget)?

Every scan is a pure function of the already-fetched comment list: the
caller owns the `gh` read, and a malformed record is skipped, never read
as a reason. Extracted from `runner.py` so that module stays under its
Article 3 size ceiling (Issue #1229).
"""
from __future__ import annotations

from orbi import failure, scene
from orbi.github import _comment_is_trusted
from orbi.progress import (
    FAILURE_MARKER_PATTERN,
    RUN_MARKER_PATTERN,
    failure_repeat_count,
)


def _is_line_failure_comment(body: str, run_id: str,
                             fingerprint: str) -> bool:
    """True when one comment is the failure record of this exact
    (run_id, fingerprint) pair — the hidden `orbi:fail` marker plus the
    run marker (Issue #825)."""
    fail = FAILURE_MARKER_PATTERN.search(body)
    return (
        fail is not None
        and fail.group(1) == fingerprint
        and run_id in set(RUN_MARKER_PATTERN.findall(body))
    )


def _reported_failure_comment(comments: list, run_id: str,
                              fingerprint: str) -> dict | None:
    """The trusted comment already reporting this exact
    (run_id, fingerprint) failure, or None — the #825 dedup key. A pure
    scan over the already-fetched comment list."""
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if isinstance(body, str) and _is_line_failure_comment(
                body, run_id, fingerprint):
            return comment
    return None


def _transient_failure_seen(comments: list) -> bool:
    """True when the Issue already carries a trusted `github_transient`
    failure record (Issue #1351).

    The one-shot automatic-retry budget is read back from the Issue's
    comments — every failure comment carries the `orbi:failure:v1` block
    and the reason code — so it survives a host restart and is shared by
    every runner instance, with no local state. A malformed block is
    skipped, never counted as a retry already spent. A pure scan over
    the already-fetched comment list."""
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        try:
            record = failure.parse(comment.get("body"))
        except failure.FailureError:
            continue
        if record is not None and record.reason_code == "github_transient":
            return True
    return False


def _failure_streak(comments: list, run_id: str,
                    fingerprint: str) -> int:
    """The number of CONSECUTIVE identical failures at the tail of the
    trusted comment history (Issue #825). A pure scan over the
    already-fetched comment list.

    A matching failure comment adds its repeat count — the dedup keeps
    ONE comment per (run_id, fingerprint) and bumps its counter in
    place, so the counter IS the occurrence count. A DIFFERENT failure
    ends the streak; a scene block (an opened PR, a completed review
    round) ends it too — the delivery line advanced, the premises
    changed. Publisher milestones and human chatter in between are
    skipped: they change no premise."""
    streak = 0
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _is_line_failure_comment(body, run_id, fingerprint):
            streak += failure_repeat_count(body)
            continue
        if FAILURE_MARKER_PATTERN.search(body) or scene.carries_scene_block(
                body):
            break
    return streak
