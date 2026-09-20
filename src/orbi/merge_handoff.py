"""Classification of merge failures that require maintainer handoff."""
from __future__ import annotations

import re


class MergeHandoffRequired(RuntimeError):
    """The reviewed PR is ready, but repository policy requires a human merge."""


_POLICY_REJECTION = re.compile(
    r"the base branch policy prohibits the merge", re.IGNORECASE,
)


def is_approval_handoff(stderr: str, preflight: list[str]) -> bool:
    """Return true only for the known policy rejection plus review blocker."""
    return bool(
        _POLICY_REJECTION.search(stderr)
        and any("approving review(s)" in line for line in preflight)
    )
