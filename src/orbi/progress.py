#!/usr/bin/env python3
"""Automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker
(`<!-- orbi:run=<run_id> -->`) so a restarted process finds the same
comment again and keeps PATCHing it in place — no database, no new
heartbeat comments. Milestone events without a resume scene (plan
ready, tests passed/failed, review findings, merged, blocked) are
published as short standalone comments so GitHub Mobile pushes a
notification for each one.

All GitHub traffic goes through the single subprocess seam
(`orbi.journal.run_command`, `gh api`), which logs the command and fails
fast on any error. There is no fallback or retry.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from importlib import metadata
from pathlib import Path
from typing import Callable

from orbi.journal import (
    LOGGER,
    RUN_ID_PATTERN,
    issue_context,
    quote_value,
    validate_run_id,
)
from orbi.pi_activity import activity_snapshot, sanitize

# One marker per run: hidden in the rendered comment, exact for lookup.
# The run-id pattern and its validator live in `orbi.journal` (the run
# binding owns the contract); they are re-exported here for callers.
RUN_MARKER_PATTERN = re.compile(r"<!-- orbi:run=([0-9a-f]{8}) -->")
RUN_MARKER_TEMPLATE = "<!-- orbi:run={run_id} -->"
RUNNER_MARKER_TEMPLATE = "<!-- runner={fingerprint} -->"
_RUNNER_MARKER_PATTERN = re.compile(r"<!-- runner=[^>]+ -->")
# Standalone milestone comments share this prefix so they are recognizable.
MILESTONE_PREFIX = "Orbi:"
# The live progress comment carries this header; together with the run
# marker it identifies the run's progress comment among the run's other
# marker-carrying comments (started Pi / opened PR scenes, milestones).
PROGRESS_HEADER = "**Orbi progress**"


def _checkout_root(path: Path) -> Path | None:
    """Return the nearest checkout containing a Git marker."""
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def runner_fingerprint() -> str:
    """Identify the code executing the Runner without fabricating a value.

    An editable install points at a package inside its checkout, whose HEAD
    is the useful deployment identity. A copied/non-editable package has no
    checkout, so its distribution version is the only available identity.
    Any probe failure is deliberately reported as ``unknown``.
    """
    try:
        checkout = _checkout_root(Path(__file__).resolve().parent)
        if checkout is not None:
            result = subprocess.run(
                ["git", "rev-parse", "--short=8", "HEAD"],
                cwd=checkout, check=True, capture_output=True,
                text=True, timeout=5,
            )
            fingerprint = result.stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", fingerprint):
                return fingerprint
            return "unknown"
        version = metadata.version("orbi")
        return version if isinstance(version, str) and version else "unknown"
    except Exception:
        return "unknown"


def _with_runner_marker(body: str) -> str:
    """Append the runner identity as a hidden, machine-readable marker."""
    body = _RUNNER_MARKER_PATTERN.sub("", body).rstrip()
    marker = RUNNER_MARKER_TEMPLATE.format(fingerprint=runner_fingerprint())
    return body + "\n\n" + marker


def _without_runner_marker(body: str) -> str:
    """Return the body with the hidden runner fingerprint marker removed."""
    return _RUNNER_MARKER_PATTERN.sub("", body).rstrip()


def field_block(run_id: str, headline: str, fields: dict[str, object]) -> str:
    """Render a marker-first status comment with one field per line."""
    lines = [run_marker(run_id), headline]
    lines.extend(
        f"- {key}={value}" if key == "run_id"
        else f"- {key}: {value}" for key, value in fields.items()
    )
    return _with_runner_marker("\n".join(lines))


def format_status_comment(body: str) -> str:
    """Expand legacy one-line Orbi status comments into field blocks."""
    if not isinstance(body, str):
        return body
    marker = ""
    if body.startswith("<!-- orbi:run=") and "\n" in body:
        marker, _, body = body.partition("\n")
    first, separator, remainder = body.partition("\n")
    prefixes = (
        "Orbi failed:", "Orbi needs a fix:", "Orbi merged PR:",
        "Orbi release failed", "Orbi release waiting",
    )
    if not first.startswith(prefixes):
        return _with_runner_marker((marker + "\n" + body) if marker else body)
    fields: dict[str, object] = {}
    for key, value in re.findall(r"([A-Za-z_][\w-]*)=([^\s)]+)", first):
        fields[key] = value
    detail = re.sub(r"\s*\((?=[^)]*=)[^)]*\)", "", first).strip()
    run_id = fields.get("run_id")
    if not run_id and marker:
        match = re.search(r"orbi:run=([0-9a-f]{8})", marker)
        run_id = match.group(1) if match else None
    fields.setdefault("detail", detail)
    if isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id):
        rendered = field_block(run_id, detail, fields)
        if remainder:
            rendered += "\n" + remainder
        return _with_runner_marker(
            (marker + "\n" + rendered.split("\n", 1)[1])
            if marker else rendered
        )
    return _with_runner_marker((marker + "\n" + body) if marker else body)


def run_marker(run_id: object) -> str:
    """Return the hidden HTML marker that identifies one valid run."""
    return RUN_MARKER_TEMPLATE.format(run_id=validate_run_id(run_id))


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
    # Issue #526: the progress comment (and the blocked / fix-needed
    # scene it becomes) carries the runner fingerprint like every other
    # Orbi comment.
    return _with_runner_marker("\n".join(lines))


class ProgressPublisher:
    """Create, find and update the single per-run progress comment.

    `ensure` locates the run's progress comment by its hidden run marker
    plus the progress header (PATCHing it when it exists, POSTing it when
    it does not) and tracks its id; the run's other marker-carrying
    comments (scene comments, milestones) are never touched.
    `patch` updates the tracked comment in place and fails fast when no
    comment is tracked yet; `finish` publishes the final outcome on the
    tracked comment, or locates/creates it like `ensure` when the run
    never got that far (Issue #474). `milestone` posts a short
    standalone comment. `failure_scene` updates the run's identical
    recoverable-failure comment in place instead of appending a duplicate
    (Issue #645). Every call goes through `run_command` (gh api)
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

    def _post_comment(self, body: str) -> int:
        endpoint = self._endpoint()
        raw = self._run_command(
            [
                "gh", "api", endpoint,
                "--method", "POST", "--field", f"body={body}",
            ],
            log_command=["gh", "api", endpoint, "--method", "POST"],
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
            [
                "gh", "api", endpoint,
                "--method", "PATCH", "--field", f"body={body}",
            ],
            log_command=["gh", "api", endpoint, "--method", "PATCH"],
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
        headline, separator, detail = text.partition(": ")
        fields: dict[str, object] = {}
        if separator:
            pairs = re.findall(r"([A-Za-z_][\w-]*)=([^\s)]+)", detail)
            fields.update(pairs)
            prose = re.sub(r"\s*\(?[A-Za-z_][\w-]*=[^\s)]+", "", detail)
            prose = prose.strip(" ()")
            if prose or len({key for key, _ in pairs}) != len(pairs):
                fields["result"] = detail
        fields["run_id"] = self.run_id
        self._post_comment(field_block(
            self.run_id, f"{MILESTONE_PREFIX} {headline}", fields,
        ))

    def failure_scene(self, body: str) -> None:
        """Update this run's identical recoverable-failure comment in place.

        Issue #645: a recoverable failure retries every tick while the
        provider quota window lasts (hours), so re-posting the identical
        scene comment each tick buries the delivery progress. When this
        run already has a comment carrying the exact same rendered body
        (only the hidden runner fingerprint is ignored), that comment is
        PATCHed; otherwise the scene is posted as a new comment. The
        comparison is exact, so a genuinely different failure always
        gets its own comment and the provider's original error string is
        preserved verbatim.
        """
        rendered = format_status_comment(body)
        key = _without_runner_marker(rendered)
        for comment in self._list_comments():
            existing = comment.get("body")
            if (isinstance(existing, str)
                    and _without_runner_marker(existing) == key):
                self._patch_comment(int(comment["id"]), rendered)
                return
        self._post_comment(rendered)

    def finish(self, body: str) -> None:
        """Publish the final outcome body on the run's progress comment.

        When no comment is tracked yet — a run that fails before
        `ensure` (Issue #474: a release declaration parse error calls
        `finish` first) — the final outcome is still published: the
        run's progress comment is located or created exactly like
        `ensure`, never a `RuntimeError` and never a duplicate comment
        for a resumed run.
        """
        if self.comment_id is None:
            self.ensure(body)
            return
        self.patch(body)


# --- test evidence and run-scene helpers (moved from `orbi.runner`,
# --- Issue #785: the progress state belongs to the progress module) ----


# pytest's final summary line: `1 failed, 155 passed in 4.43s` (the
# counts and the `in <seconds>` part are optional; the line is NOT
# wrapped in `=` section padding).
_Pytest_SUMMARY_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors|skipped|xfailed|xpassed"
    r"|deselected)(?:, \d+ \w+)*(?: in [\d.]+s)?$")


def _is_section_header(line: str) -> bool:
    """True for pytest section headers like `=== FAILURES ===`.

    A header is `=`-delimited at both ends (padding may be absent on
    one side for short titles) and its title carries no digits; the
    real summary line (`1 failed, 155 passed in 4.43s`, bare or
    `=`-padded) and the `FAILED`/`ERROR` evidence lines never match.
    """
    if not line.startswith("="):
        return False
    core = line.strip("=").strip()
    return core == "" or not any(ch.isdigit() for ch in core)


# A pytest summary line reports an outcome only when its FIRST count
# category is failed/passed/error(s): pytest orders the counts
# failed, passed, skipped, errors, xfailed, xpassed, deselected, so a
# run that collected tests always leads with failed or passed (or a
# collection error). `no tests ran in 0.01s` matches no summary regex
# at all; `3 deselected in 0.02s` / `2 skipped in 0.01s` match but
# carry no outcome — reporting them as a pass is a false notification
# (review round 3, PR #42).
_OUTCOME_FIRST_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors)\b",
)
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)")


def _is_no_result(line: str) -> bool:
    """True for pytest lines that verified nothing (no tests ran)."""
    if _NO_TESTS_RE.search(line):
        return True
    stripped = line.strip("=").strip()
    return bool(_Pytest_SUMMARY_RE.match(stripped)) \
        and not _OUTCOME_FIRST_RE.match(stripped)


def read_test_result(worktree: Path) -> str | None:
    """Summarize the worktree's `.orbi/test.log`, or None when it does
    not exist (Issue #302: the contract test command writes the log
    into the excluded run dir, never at the worktree root).

    Prefers the pytest summary line (`1 failed, 155 passed in 4.43s`):
    the LAST one with an outcome when the log holds several runs (TDD
    red, then green), so the progress comment and the `tests
    passed/failed` milestone report the most recent run that actually
    collected tests. Section headers (`=== FAILURES ===`) are never
    reported: the fallback takes the first `FAILED`/`ERROR` evidence
    line or the last non-empty line instead, and a log holding nothing
    but headers yields None (no result info to report). Lines that
    verified nothing (`no tests ran`, `N deselected`, `N skipped`) are
    never reported either: a run that collected no tests is no result,
    and posting `tests passed` for it is a false notification (review
    round 3, PR #42).
    """
    path = worktree / ".orbi" / "test.log"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        # The summary line is bare in pytest 9; some runners wrap it in
        # `=` padding, so the stripped form is matched as well.
        stripped = line.strip("=").strip()
        if _Pytest_SUMMARY_RE.match(line) \
                or _Pytest_SUMMARY_RE.match(stripped):
            if _is_no_result(line):
                # No tests collected in this run: keep looking for an
                # earlier run that did (or report nothing at all).
                continue
            return sanitize(stripped)
    for line in lines:
        if _is_section_header(line) or _is_no_result(line):
            continue
        if line.startswith(("FAILED", "ERROR")) or "passed" in line:
            return sanitize(line)
    last = lines[-1] if lines else None
    if last is not None and (_is_section_header(last)
                             or _is_no_result(last)):
        return None
    return sanitize(last) if last is not None else None


def _run_info_fields(run_info: str) -> dict[str, str]:
    """Extract the runner-owned key/value fields for comment rendering."""
    return dict(
        part.split("=", 1) for part in run_info.split()
        if "=" in part
    )


def _progress_state(*, issue: int, title: str, run_id: str, role: str,
                    branch: str, worktree: Path, started: float,
                    pr_url: str | None, review_round: int, priority: str,
                    activity: dict | None = None) -> dict:
    """Collect the current run state for the GitHub progress comment.

    `title` is the issue's GitHub title (Issue #100): the progress
    comment's issue line shows `#<number> <title>` in every scene. It
    is required — the GitHub issue data contract guarantees a
    non-empty string title (every runner scan fetches it), and a
    missing title fails fast in `progress.issue_field` instead of
    fabricating one.
    `priority` is the pickup priority of the issue (`p0` or `normal`,
    Issue #101), derived from the issue's labels at claim/resume time.
    `activity` is the live state from the `stream_pi` watcher while a Pi
    session runs (fresh and already read); without it the newest session
    file is full-scanned. Activity snapshotting is best-effort
    observability: a read failure is logged and reported as "no session
    yet", it never blocks the task.
    """
    if activity is None:
        try:
            activity = activity_snapshot(worktree / ".pi-session")
        except Exception:
            LOGGER.exception("issue=%s activity snapshot failed", issue)
            activity = None
    return {
        "run_id": run_id,
        "issue": issue,
        "issue_title": title,
        "role": role,
        "priority": priority,
        "phase": (activity or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (activity or {}).get("last_activity"),
        "last_action": (activity or {}).get("action"),
        "tests": read_test_result(worktree),
        "review_round": review_round,
        "branch": branch,
        "pr": pr_url,
        "session": (activity or {}).get("session_id"),
        # Idle-stall recovery state (Issue #94): `term` / `kill` while
        # the runner recovers a stalled session, absent/None otherwise
        # (the body renders the line only while it is active).
        "recovery": (activity or {}).get("recovery"),
    }


def _progress_body(state: dict, *, outcome: str | None = None) -> str:
    """Render the progress body, optionally with a final outcome header."""
    body = progress_body(state)
    if outcome is None:
        return body
    return f"{outcome}\n\n{body}"


def _safe_publish(*, run_id: str, issue: int, source_repo: str,
                  role: str, action: Callable[[], None]) -> None:
    """Run one progress-publishing step as a pure bypass (Issue #79).

    The main delivery path is claim -> worktree -> Pi -> verify PR ->
    review -> fix -> merge; the GitHub progress comment is observability
    on the side. A publishing failure (404, rate limit, API shape
    change) is logged as `progress_publish_failed` and never fails the
    delivery, never marks the Issue `ai-blocked`, and never skips
    `run_pi` / `wait_for_delivery`. This is the same semantics as the
    in-stream live-PATCH callback; Issue #60 already applied it to the
    post-PR record, Issue #79 extends it to the whole
    `ProgressPublisher` path (ensure / milestone / finish).
    """
    try:
        action()
    except Exception:
        LOGGER.exception(
            "progress_publish_failed run=%s issue=%s role=%s",
            run_id, issue_context(source_repo, issue), role,
        )
