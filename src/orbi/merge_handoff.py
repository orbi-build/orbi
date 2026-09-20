"""Classification of merge failures that require maintainer handoff."""
from __future__ import annotations

import re


class MergeHandoffRequired(RuntimeError):
    """The reviewed PR is ready, but known repository policy needs a human."""

    def __init__(self, message: str, *, preflight: list[str] | None = None):
        super().__init__(message)
        self.preflight = list(preflight or [])


_POLICY_REJECTION = re.compile(
    r"the base branch policy prohibits the merge", re.IGNORECASE,
)
_FAILED_PREFLIGHT = re.compile(r"^merge_gate: FAILED\b")


def is_maintainer_actionable(stderr: str, preflight: list[str]) -> bool:
    """Return true when the merge rejection names a fixable policy blocker.

    PASS and UNKNOWN lines are deliberately excluded: the preflight must
    establish a concrete action a maintainer can take.
    """
    return bool(
        _POLICY_REJECTION.search(stderr)
        and any(_FAILED_PREFLIGHT.search(line) for line in preflight)
    )
