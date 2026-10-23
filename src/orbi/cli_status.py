"""The `orbi status` reads and the one scoped render (Issue #1862).

Extracted verbatim from `cli.py` so the entry module stays a thin
dispatcher: the ready/result lookups the status observer cycles through
and the render over its last complete reads. The label IS the query —
the status view never assembles a search of its own, and "which Issue is
ready" stays the claim's decision (#1773), read under the status bound
(#1668).
"""
from __future__ import annotations

import logging
from pathlib import Path

from orbi import claim, journal as journal_domain, status_observe
from orbi import config as config_domain
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
)
from orbi.pilot_slots import HELD_PID_UNKNOWN, slot_occupancy
from orbi.runner import list_issues

# `ai-pr-opened` (awaiting review), `ai-fix-needed` (awaiting the next
# review session), `ai-merged` (the Runner merged the PR
# itself) and `ai-blocked` are all result states of an
# opened delivery.
RESULT_LABELS = (PR_OPENED_LABEL, FIX_NEEDED_LABEL, MERGED_LABEL, BLOCKED_LABEL)


def list_labeled_issues(repo: str, label: str, state: str = "open", *,
                        timeout: int | None = None) -> list[dict]:
    """Return the newest Issue carrying one label (gh lists newest first).

    The label IS the query: the status view never assembles a search of
    its own. "Which Issue is ready" is the claim's decision (#1773).
    """
    return list_issues(
        repo, state=state, search=f"label:{label}",
        json_fields="number,title,url,state", limit=1, timeout=timeout,
    )


def current_issue(repo: str) -> dict | None:
    issues = list_labeled_issues(repo, IN_PROGRESS_LABEL)
    return issues[0] if issues else None


def ready_issue(repo: str,
                config: config_domain.RunnerConfig) -> dict | None:
    """The Issue the claim would pick, read under the status bound (#1668).

    The ready decision lives on the claim side (Issue #1773): an Issue in
    a delivery state (ai-pr-opened/ai-fix-needed/ai-merged/ai-blocked) is
    never ready, and the claim's pick/skip lines stay out of the view; an
    unreadable scan fails the observation (#1668), not an empty queue.
    The held set and the Milestone read arrive from the claim (#1782).
    """
    journal = journal_domain.LOGGER
    level = journal.level
    journal.setLevel(max(level, logging.WARNING))
    try:
        return claim.pick_ready_issue(
            repo, config.active_milestone, config,
            timeout=status_observe.STATUS_READ_TIMEOUT_SECONDS,
            raise_on_read_failure=True,
        )
    finally:
        journal.setLevel(level)


def recent_result(repo: str) -> dict | None:
    """Return the newest delivery result Issue, any state.

    Result states: `ai-pr-opened` (awaiting review), `ai-fix-needed`
    (awaiting the next review session), `ai-merged` (success
    terminal, the Runner merged the PR itself) and `ai-blocked` (needs
    human attention).
    """
    newest = None
    for label in RESULT_LABELS:
        for issue in list_labeled_issues(
            repo, label, state="all",
            timeout=status_observe.STATUS_READ_TIMEOUT_SECONDS,
        ):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    """Render `#N title url`; a claim-scan record carries no url (#1773)."""
    url = issue.get("url")
    return f"#{issue['number']} {issue['title']}" + (f" {url}" if url else "")


def slot_lines(state_dir: Path, capacity: int) -> list[str]:
    """Return the status lines for the configured concurrency capacity."""
    occupancy = slot_occupancy(state_dir, capacity)
    taken = sum(1 for _, pid in occupancy if pid is not None)
    lines = [f"slots: {taken}/{capacity}"]
    lines.extend(
        f"  slot-{index}: pid="
        + ("unknown (held)" if pid == HELD_PID_UNKNOWN else str(pid))
        for index, pid in occupancy
        if pid is not None
    )
    return lines


def status_report(config: config_domain.RunnerConfig,
                  scope: status_observe.StatusScope,
                  observer: status_observe.StatusObserver) -> str:
    """Render one scoped status view from the observer's complete reads.

    Capacity/slots, the frozen base and the queue/result summary are
    kept; the active-delivery section enumerates every concurrent
    delivery in scope with its attributed local observations. An
    explicit --issue scope shows that Issue's confirmed GitHub state and
    its retained, timed observations instead. Every GitHub-backed value
    comes from the observer's last complete GitHub cycle, so a local
    render reads no GitHub state.
    """
    lines = [
        f"capacity: {config.max_concurrency}",
        *slot_lines(config.slot_dir, config.max_concurrency),
    ]
    for repo in scope.repos:
        lines.append(f"source: {repo}")
        base_sha = observer.base.get(repo) or "-"
        lines.append(f"  base: {config.base_branch} {base_sha}")
        if scope.issue is not None:
            delivery = observer.confirmed.get(repo)
            if delivery is not None:
                lines.append(
                    f"  issue: {status_observe.format_delivery(delivery)}"
                )
            lines.extend(status_observe.observation_lines(
                observer.observations(repo, {scope.issue}).get(scope.issue, []),
            ))
        else:
            deliveries = observer.deliveries.get(repo, [])
            if not deliveries:
                lines.append("  active: -")
            for delivery in deliveries:
                lines.append(
                    f"  active: {status_observe.format_delivery(delivery)}"
                )
                lines.extend(status_observe.observation_lines(
                    observer.observations(repo, {delivery.number}).get(
                        delivery.number, [],
                    ),
                ))
        ready = observer.ready.get(repo)
        result = observer.result.get(repo)
        lines.append(f"  ready: {format_issue(ready) if ready else '-'}")
        lines.append(f"  result: {format_issue(result) if result else '-'}")
    lines.extend(observer.ended)
    return "\n".join(lines)
