"""The recovered PR's CI status, logged without deciding anything (Issue #2304).

Extracted from `review_merge` when the waiting-for-checks line landed in
that module (Issue #2304): the module may only shrink under the size
ratchet (Issue #2167), and this observability helper is a self-contained
leaf — one read, one journal line, no control flow.

It runs when a restart scan recovers a delivery whose review round already
has a budget: the PR's current check status is recorded (never its output)
so an operator can see what the recovered session was looking at.
"""
from __future__ import annotations

import logging

from orbi.github import commit_check_runs
from orbi.journal import event


def log_recovery_ci_status(pr: dict, repo: str) -> None:
    """Record the recovered PR's current check status without check output.

    This is observability only: a GitHub status lookup must never decide
    whether the recovered review runs.
    """
    try:
        checks = commit_check_runs(repo, pr["head_oid"])
        summary = [
            f"{check.get('name', '?')}={check.get('status', '?')}/"
            f"{check.get('conclusion', '?')}"
            for check in checks if isinstance(check, dict)
        ]
    except Exception as exc:
        event(
            "review_recovery_ci_status_failed", level=logging.WARNING,
            pr=pr.get("number", "?"), error=str(exc),
        )
        return
    event(
        "review_recovery_ci_status", pr=pr["number"],
        checks=",".join(summary) or "none",
    )
