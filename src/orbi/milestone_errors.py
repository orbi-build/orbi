#!/usr/bin/env python3
"""Milestone failures read as the HTTP status GitHub answered with (#1743).

A git or `gh` failure inside the milestone bookkeeping is either a
permanent rejection (401 bad credentials, 404 no such milestone) or a
transient one the caller retries. This module owns that classification and
the one exception it raises, so `github`, `milestone` and the delivery
scene agree on it. The missing-path decision is
`gh_response.not_found` — the ONE 404 rule every caller shares (Issue
#2394), so a 403 or 5xx whose message happens to say "not found" is never
read as a missing Milestone.

A leaf module: `journal.py` owns the logger and the subprocess seam and
sits at its size-ratchet ceiling, so the milestone domain's error shape
lives beside it instead of inside it.
"""
from __future__ import annotations

import re

from orbi import gh_response


class MilestoneReconcileError(RuntimeError):
    def __init__(self, *, status: int, operation: str, stderr: str) -> None:
        self.status, self.operation, self.stderr = status, operation, stderr
        super().__init__(f"{operation} failed with HTTP {status}: {stderr}")


def classify_milestone_error(exc, operation: str) -> None:
    """Re-raise a permanent GitHub rejection as this module's own error."""
    status = milestone_error_status(exc)
    if status is not None:
        raise MilestoneReconcileError(
            status=status, operation=operation,
            stderr=str(exc.stderr or exc.stdout or ""),
        ) from exc
    raise exc


def milestone_error_status(exc) -> int | None:
    """The permanent HTTP status a failed request carries, or None."""
    detail = " ".join(
        str(part or "") for part in (getattr(exc, "stderr", ""),
                                     getattr(exc, "stdout", ""))
    )
    if (gh_response.authentication_required(exc)
            or re.search(r"(?:http|status) ?401|bad credentials", detail, re.I)):
        return 401
    return 404 if gh_response.not_found(exc) else None
