"""Every ai-blocked stop carries the failure kind and ONE record (#2245).

A reader that identifies a stop by its machine-readable failure record —
Orbi Cloud's status page among them — must find EVERY terminal stop, not
only the ones whose scene rendered a classified report. The scenes hand
their own prose to the ONE blocked exit; this module adds what they all
share and no scene should remember: the `orbi:comment=failure` kind right
after the run marker, and the terminal `orbi:failure:v1` record with
outcome `blocked` when the scene wrote none of its own.
"""
from __future__ import annotations

from orbi import failure
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker

# The record a terminal stop writes when its scene rendered none: the
# Issue is ai-blocked and a human decides.
STOP_RECORD = failure.Failure(
    reason_code="human_decision_required", action_code="decide",
    retry_safe=False, outcome="blocked",
)

# The record block's opening bytes, derived from the one template the
# renderer writes, so a presence check never spells the marker itself.
_RECORD_PREFIX = failure.FAILURE_BLOCK_TEMPLATE.split("{", 1)[0]


def with_stop_record(body: str) -> str:
    """The body with the failure kind and exactly one terminal record.

    A scene that already wrote its own record (the classified delivery
    report, the permission and release stops) keeps it untouched; the kind
    marker only rides a run-scoped comment, where it is the anchored line
    right after the run marker.
    """
    lines = body.split("\n")
    if lines[0].startswith("<!-- orbi:run="):
        kind = comment_kind_marker(COMMENT_KIND_FAILURE)
        if lines[1:2] != [kind]:
            lines.insert(1, kind)
        insertion = 2
    else:
        insertion = 0
    if _RECORD_PREFIX not in body:
        lines.insert(insertion, failure.render(STOP_RECORD))
    return "\n".join(lines)
