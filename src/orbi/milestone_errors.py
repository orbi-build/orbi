#!/usr/bin/env python3
"""Milestone failures read as the HTTP status GitHub answered with (#1743).

A git or `gh` failure inside the milestone bookkeeping is either a
permanent rejection (401 bad credentials, 404 no such milestone) or a
transient one the caller retries. This module owns that classification and
the one exception it raises, so `github`, `milestone` and the delivery
scene agree on it.

A leaf module (`re` is its only import): `journal.py` owns the logger and
the subprocess seam and sits at its size-ratchet ceiling, so the milestone
domain's error shape lives beside it instead of inside it.
"""
from __future__ import annotations

import re


class MilestoneReconcileError(RuntimeError):
    def __init__(self, *, status: int, operation: str, stderr: str) -> None:
        self.status, self.operation, self.stderr = status, operation, stderr
        super().__init__(f"{operation} failed with HTTP {status}: {stderr}")


def classify_milestone_error(exc, operation: str) -> None:
    """Re-raise a permanent GitHub rejection as this module's own error."""
    status = milestone_error_status(str(exc.stderr or ""), str(exc.stdout or ""))
    if status is not None:
        raise MilestoneReconcileError(
            status=status, operation=operation,
            stderr=str(exc.stderr or exc.stdout or ""),
        ) from exc
    raise exc


def milestone_error_status(stderr: str, stdout: str = "") -> int | None:
    """The permanent HTTP status a failed request's output names, or None."""
    detail = " ".join((stderr, stdout))
    if re.search(r"(?:http|status) ?401|bad credentials", detail, re.I):
        return 401
    if re.search(r"(?:http|status) ?404|not found", detail, re.I):
        return 404
    return None
