#!/usr/bin/env python3
"""Automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker
(`<!-- orbi:run=<run_id> -->`) so a restarted process finds the same
comment again and keeps PATCHing it in place — no database, no new
heartbeat comments. Milestone events (started, plan ready, tests
passed/failed, review findings, PR opened, merged, blocked)
are published as short standalone comments so GitHub Mobile pushes a
notification for each one.

All GitHub traffic goes through the reused `runner.run_command`
(`gh api`), which logs the command and fails fast on any error. There is
no fallback or retry. Progress POST/PATCH bodies (the full multi-KB
markdown) are never written to the journal: `run_command` gets a
redacted `log_command` with endpoint + method only (Issue #326), so the
live state stays readable while the full body lives in the GitHub
comment.
"""
from __future__ import annotations

import json
from typing import Callable

# One marker per run: hidden in the rendered comment, exact for lookup.
RUN_MARKER_TEMPLATE = "<!-- orbi:run={run_id} -->"
# Standalone milestone comments share this prefix so they are recognizable.
MILESTONE_PREFIX = "Orbi:"
# The live progress comment carries this header; together with the run
# marker it identifies the run's progress comment among the run's other
# marker-carrying comments (started Pi / opened PR scenes, milestones).
PROGRESS_HEADER = "**Orbi progress**"


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


def find_progress_comment(
    comments: list[dict], run_id: str,
) -> dict | None:
    """Return this run's live progress comment, or None.

    A run's marker alone is not enough: the run's other comments
    (started Pi / opened PR scene comments, milestones) carry the same
    marker. The progress comment is the one that also carries the
    progress header.
    """
    marker = run_marker(run_id)
    for comment in comments:
        body = comment.get("body")
        if (isinstance(body, str) and marker in body
                and PROGRESS_HEADER in body):
            return comment
    return None


def issue_field(issue: int, title: str) -> str:
    """Render the progress comment's issue value: `#<number> <title>`.

    Issue #100: the issue line shows the number AND the title so a
    mobile user sees what the agent works on without opening GitHub.
    The `#<number>` prefix is preserved so existing log/scene parsing
    keeps working. The title is flattened to a single line (whitespace
    collapsed) so spaces, Markdown and long titles stay readable.
    A missing or non-string title violates the GitHub issue data
    contract (issues always carry a non-empty string title) and fails
    fast — a title is never fabricated.
    """
    if not isinstance(issue, int) or isinstance(issue, bool):
        raise ValueError(f"issue number must be an int, got {issue!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(
            f"issue title must be a non-empty string, got {title!r}",
        )
    return f"#{issue} {' '.join(title.split())}"


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
        PROGRESS_HEADER,
        "",
        # Issue #100: number AND title, one consistent format in every
        # scene; both state keys are required (a state without the
        # title fails fast, never a fabricated or bare number).
        f"- issue: {issue_field(state['issue'], state['issue_title'])}",
        # The visible run_id field is `run_id=<id>` (key=value, Issue
        # #41 contract), so a grep for the value finds every comment
        # of the run.
        f"- run_id={state['run_id']}",
        f"- role: {value('role')}",
        # Pickup priority (Issue #101): `p0` for urgent Issues,
        # `normal` otherwise — visible at a glance on mobile.
        f"- priority: {value('priority')}",
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
    # Idle-stall recovery (Issue #94): the recovery state is shown only
    # while it is active (`term` / `kill`); an idle run keeps the
    # pre-#94 body shape exactly.
    if state.get("recovery"):
        lines.append(f"- recovery: {state['recovery']}")
    return "\n".join(lines)


class ProgressPublisher:
    """Create, find and update the single per-run progress comment.

    `ensure` locates the run's progress comment by its hidden run marker
    plus the progress header (PATCHing it when it exists, POSTing it when
    it does not) and tracks its id; the run's other marker-carrying
    comments (scene comments, milestones) are never touched.
    `patch` and `finish` update the tracked comment in place and fail
    fast when no comment is tracked yet. `milestone` posts a short
    standalone comment. Every call goes through `run_command` (gh api)
    and raises on any error.
    """

    def __init__(self, issue: int, repo: str, run_id: str,
                 run_command: Callable[..., str]) -> None:
        self.issue = issue
        self.repo = repo
        self.run_id = run_id
        self._run_command = run_command
        self.comment_id: int | None = None

    def _endpoint(self) -> str:
        # List/create route (GET and POST): issue-scoped.
        return f"repos/{self.repo}/issues/{self.issue}/comments"

    def _update_endpoint(self, comment_id: int) -> str:
        # Update an issue comment: PATCH /repos/{owner}/{repo}/issues/
        # comments/{comment_id} — no issue number. Appending the id to
        # the list/create endpoint is not a GitHub REST route and 404s
        # (Issue #58).
        return f"repos/{self.repo}/issues/comments/{comment_id}"

    def _list_comments(self) -> list[dict]:
        raw = self._run_command([
            "gh", "api", self._endpoint(), "--paginate",
        ])
        comments = json.loads(raw)
        if not isinstance(comments, list):
            raise ValueError("issue comments must be a JSON array")
        return comments

    @staticmethod
    def _redacted_body_command(method: str, endpoint: str) -> list[str]:
        """The journal form of one body-carrying `gh api` call.

        Issue #326: the live progress/milestone body is multi-KB and is
        PATCHed every ~30s; logging it verbatim floods the journal with
        near-duplicate giant lines. The journal records endpoint and
        method only; the body content stays in the GitHub comment.
        """
        return [
            "gh", "api", endpoint, "--method", method,
            "--field", "body=<redacted>",
        ]

    def _post_comment(self, body: str) -> int:
        endpoint = self._endpoint()
        raw = self._run_command(
            ["gh", "api", endpoint,
             "--method", "POST", "--field", f"body={body}"],
            log_command=self._redacted_body_command("POST", endpoint),
        )
        # `gh api` replies with the full comment object, not a bare id.
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("id"), int):
            raise ValueError(
                "comment post response must be an object with an integer id",
            )
        return data["id"]

    def _patch_comment(self, comment_id: int, body: str) -> None:
        endpoint = self._update_endpoint(comment_id)
        self._run_command(
            ["gh", "api", endpoint,
             "--method", "PATCH", "--field", f"body={body}"],
            log_command=self._redacted_body_command("PATCH", endpoint),
        )

    def ensure(self, body: str) -> int:
        """Create or resume the run's progress comment; return its id.

        The run's other marker-carrying comments (scene comments,
        milestones) are never touched: only the comment that also
        carries the progress header is resumed (Issue #18).
        """
        existing = find_progress_comment(
            self._list_comments(), self.run_id,
        )
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
        """Post a short standalone milestone comment (mobile notification).

        The milestone carries the hidden run marker and the visible
        `run_id=` field like every other comment of the run (Issue #41:
        one run_id end to end, every comment of the attempt carries
        both). The visible field is appended when the text does not
        carry it already, so the contract holds for every milestone
        without repeating the field.
        """
        if f"run_id={self.run_id}" not in text:
            text = f"{text} run_id={self.run_id}"
        self._post_comment(
            f"{run_marker(self.run_id)}\n{MILESTONE_PREFIX} {text}",
        )

    def finish(self, body: str) -> None:
        """Replace the tracked comment with the final outcome body."""
        self.patch(body)
