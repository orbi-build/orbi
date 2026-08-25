#!/usr/bin/env python3
"""Automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker
(`<!-- muyan-pilot:run=<run_id> -->`) so a restarted process finds the same
comment again and keeps PATCHing it in place — no database, no new
heartbeat comments. Milestone events (started, plan ready, tests
passed/failed, review findings, fix pushed, PR opened, merged, blocked)
are published as short standalone comments so GitHub Mobile pushes a
notification for each one.

All GitHub traffic goes through the reused `bootstrap_runner.run_command`
(`gh api`), which logs the command and fails fast on any error. There is
no fallback or retry.
"""
from __future__ import annotations

import json
from typing import Callable

# One marker per run: hidden in the rendered comment, exact for lookup.
RUN_MARKER_TEMPLATE = "<!-- muyan-pilot:run={run_id} -->"
# Standalone milestone comments share this prefix so they are recognizable.
MILESTONE_PREFIX = "Muyan Pilot:"


def run_marker(run_id: str) -> str:
    """Return the hidden HTML marker that identifies one run's comment."""
    return RUN_MARKER_TEMPLATE.format(run_id=run_id)


def find_run_comment(comments: list[dict], run_id: str) -> dict | None:
    """Return the first comment carrying this run's marker, or None."""
    marker = run_marker(run_id)
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str) and marker in body:
            return comment
    return None


def format_elapsed(seconds: float) -> str:
    """Format seconds as `45s`, `3m 12s` or `1h 2m 3s` (zero units omitted)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    if not hours or minutes or secs:
        parts.append(f"{secs}s")
    return " ".join(parts)


def progress_body(state: dict) -> str:
    """Render the live progress comment body (marker first, then fields)."""
    def value(key: str) -> str:
        item = state.get(key)
        return str(item) if item not in (None, "") else "-"

    lines = [
        run_marker(state["run_id"]),
        "",
        "**Muyan Pilot progress**",
        "",
        f"- issue: #{state['issue']}",
        f"- role: {value('role')}",
        f"- phase: {value('phase')}",
        f"- elapsed: {value('elapsed')}",
        f"- last activity: {value('last_activity')}",
        f"- last action: {value('last_action')}",
        f"- tests: {value('tests')}",
        f"- review/fix round: {value('review_round')}",
        f"- branch: {value('branch')}",
        f"- PR: {value('pr')}",
        f"- session: {value('session')}",
    ]
    return "\n".join(lines)


class ProgressPublisher:
    """Create, find and update the single per-run progress comment.

    `ensure` locates the run's comment by its hidden marker (PATCHing it
    when it exists, POSTing it when it does not) and tracks its id.
    `patch` and `finish` update the tracked comment in place and fail fast
    when no comment is tracked yet. `milestone` posts a short standalone
    comment. Every call goes through `run_command` (gh api) and raises on
    any error.
    """

    def __init__(self, issue: int, repo: str, run_id: str,
                 run_command: Callable[..., str]) -> None:
        self.issue = issue
        self.repo = repo
        self.run_id = run_id
        self._run_command = run_command
        self.comment_id: int | None = None

    def _endpoint(self) -> str:
        return f"repos/{self.repo}/issues/{self.issue}/comments"

    def _list_comments(self) -> list[dict]:
        raw = self._run_command([
            "gh", "api", self._endpoint(), "--paginate",
        ])
        comments = json.loads(raw)
        if not isinstance(comments, list):
            raise ValueError("issue comments must be a JSON array")
        return comments

    def _post_comment(self, body: str) -> int:
        raw = self._run_command([
            "gh", "api", self._endpoint(),
            "--method", "POST", "--field", f"body={body}",
        ])
        return int(raw)

    def _patch_comment(self, comment_id: int, body: str) -> None:
        self._run_command([
            "gh", "api", f"{self._endpoint()}/{comment_id}",
            "--method", "PATCH", "--field", f"body={body}",
        ])

    def ensure(self, body: str) -> int:
        """Create or resume the run's progress comment; return its id."""
        existing = find_run_comment(self._list_comments(), self.run_id)
        if existing is not None:
            comment_id = int(existing["id"])
            self.comment_id = comment_id
            self._patch_comment(comment_id, body)
            return comment_id
        self.comment_id = self._post_comment(body)
        return self.comment_id

    def patch(self, body: str) -> None:
        """Update the tracked progress comment in place (fail fast)."""
        if self.comment_id is None:
            raise RuntimeError("no progress comment to update")
        self._patch_comment(self.comment_id, body)

    def milestone(self, text: str) -> None:
        """Post a short standalone milestone comment (mobile notification)."""
        self._post_comment(f"{MILESTONE_PREFIX} {text}")

    def finish(self, body: str) -> None:
        """Replace the tracked comment with the final outcome body."""
        self.patch(body)
