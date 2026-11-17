"""The three terminal stops that used to read as `unclassified` (Issue #2222).

A failed release, a delivery whose agent session ended without a commit and one
whose commits change no file all ended as "the cause could not be determined",
although each cause was known exactly where it happened.

The two empty deliveries are decided at the source: the commit boundary
(`run_state.delivery_commit_guard`) refuses a HEAD still on the frozen base and
a tree equal to the base tree, and raises the matching type below. Each carries
its `failure_record`, the pattern `BaseBranchMissingError` already uses (Issue
#1756), so `failure_report` reads the record the source decided instead of
re-deriving a cause from the message — the message the reader knows stays
exactly as it was. The records are closed `orbi.failure` codes, so an unknown
one fails fast here.

The failed release keeps its own classifier fallback (`failure.classify` with
`release=True`) and gets its visible reason from `release_failure_reason_line`
below: the code says a release task failed, that line says at which step.

The types live beside `run_state` and `release_comments` rather than inside
them because a recorded module never grows (`tools/size_ratchet.py`,
Article 3.5).
"""
from __future__ import annotations

from orbi import failure, language


def _record(reason_code: str) -> failure.Failure:
    """The pinned record of one empty-delivery stop."""
    return failure.Failure(reason_code, *failure.DISPOSITIONS[reason_code])


class NoCommitDelivered(RuntimeError):
    """The agent delivered no commit: HEAD is still the frozen base."""

    failure_record = _record("no_commit_delivered")


class NoFileChange(RuntimeError):
    """The delivered commits change no file: the tree equals the base tree."""

    failure_record = _record("no_file_change")


def release_failure_reason_line(step: str, detail: str, body: str, *,
                                language_code: str) -> str:
    """Insert the visible release-failure reason into its stop comment.

    `release_failed` says a release task failed, but not where: the state
    machine has idempotent steps (declaration, base, gates, scope, version,
    CI gate, tag, publish) and a person needs the one that failed to act.
    `step` is that step's machine name and stays verbatim; the sentence
    around it is catalog prose. The reason is a body line, never a field
    row: a long detail would fold a row into the comment's details block,
    and the reason must stay visible.
    """
    reason = language.text(
        language_code, "failure.release_failed_reason", step=step,
        detail=failure.failure_summary(detail, language_code),
    )
    label = language.text(language_code, "failure.reason_label")
    # The reason sits after the readable summary (the headline and the task
    # identity) and before the fixed field rows, the same place a delivery
    # failure comment puts it. A field row starts with "- "; the summary
    # lines never do.
    lines = body.splitlines()
    first_field = next(
        (index for index, line in enumerate(lines) if line.startswith("- ")),
        len(lines),
    )
    lines[first_field:first_field] = ["", f"{label} {reason}"]
    return "\n".join(lines)
