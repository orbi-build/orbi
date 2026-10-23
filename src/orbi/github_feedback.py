"""PR feedback normalization and delivery-rollup helpers (Issue #1863).

Split out of `orbi.github` once that leaf crossed the 1,000-line cap. The
`gh` reads and the auth identity helpers (`pr_view`, `_pr_number`,
`_authenticated_github_login`, `_strip_bot_suffix`) stay in `orbi.github`;
this module imports them one way. `orbi.github` never imports this module
back, so the adapter stays a leaf and the runtime import graph stays acyclic.
"""
from __future__ import annotations

from orbi.github import (
    _authenticated_github_login,
    _pr_number,
    _strip_bot_suffix,
    pr_view,
)


def _normalize_pr_feedback_item(item: dict) -> dict:
    """Map GraphQL and REST PR feedback to the Issue-comment shape."""
    author = item.get("author")
    if not isinstance(author, dict):
        author = item.get("user")
    normalized = {
        "author": {"login": author.get("login") if isinstance(author, dict)
                    else None},
        "authorAssociation": item.get(
            "authorAssociation", item.get("author_association")),
        "createdAt": item.get(
            "createdAt", item.get("created_at", item.get(
                "submittedAt", item.get("submitted_at")))),
        "body": item.get("body"),
    }
    for field in ("state", "path", "line", "original_line"):
        if field in item:
            normalized[field] = item[field]
    return normalized


def normalize_pr_feedback(items: list[dict]) -> list[dict]:
    """Normalize PR feedback and exclude the delivery identity."""
    identity = _strip_bot_suffix(_authenticated_github_login())
    normalized = [_normalize_pr_feedback_item(item) for item in items]
    return [
        item for item in normalized
        if _strip_bot_suffix(item["author"]["login"] or "") != identity
    ]


def _check_summaries(rollup: list) -> list[str]:
    """Render one status check rollup as compact `name=STATUS[/CONCLUSION]`
    strings (delivery-journal evidence)."""
    summaries = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        name = check.get("name", check.get("context", "check"))
        status = check.get("status", check.get("state", "UNKNOWN"))
        conclusion = check.get("conclusion")
        detail = str(status)
        if conclusion:
            detail += f"/{conclusion}"
        summaries.append(f"{name}={detail}")
    return summaries


def pr_delivery_rollup(pr_url: str, source_repo: str) -> tuple[str, list]:
    """Return PR state and the raw status check rollup (ONE read).

    The delivery step classifies this rollup once per tick: pending
    checks defer the delivery to the next tick instead of sleeping.
    """
    number = _pr_number(pr_url)
    data = pr_view(number, "state,statusCheckRollup", repo=source_repo)
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    rollup = data.get("statusCheckRollup")
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list):
        raise ValueError("pr statusCheckRollup must be a JSON array")
    return state, rollup


def pr_delivery_status(pr_url: str, source_repo: str) -> tuple[str, list[str]]:
    """Return PR state and CI summaries for delivery-wait evidence."""
    state, rollup = pr_delivery_rollup(pr_url, source_repo)
    return state, _check_summaries(rollup)
