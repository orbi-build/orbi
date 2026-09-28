#!/usr/bin/env python3
"""Automatic GitHub progress publishing.

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
import logging
import re
import subprocess
import time
from importlib import metadata
from pathlib import Path
from typing import Callable

from orbi.delivery_scene import RunContext
from orbi.failure import Failure, REASON_CODES, _redact_local_paths
from orbi.journal import (
    LOGGER,
    RUN_ID_PATTERN,
    event,
    issue_context,
    quote_value,
    redact_secrets,
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
# The started scene carries this headline; together with the run marker
# it identifies the run's single `Orbi started Pi:` comment, so a resume
# PATCHes it in place instead of appending a near-identical duplicate
# (Issue #1369).
STARTED_HEADER = "Orbi started Pi:"
# Values this long are diagnosis/details rather than concise status fields.
DETAIL_VALUE_LENGTH = 200


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
        version = metadata.version("orbi-cli")
        return version if isinstance(version, str) and version else "unknown"
    except Exception:
        return "unknown"


def _with_runner_marker(body: str) -> str:
    """Append the runner identity as a hidden, machine-readable marker."""
    body = _RUNNER_MARKER_PATTERN.sub("", body).rstrip()
    marker = RUNNER_MARKER_TEMPLATE.format(fingerprint=runner_fingerprint())
    return body + "\n\n" + marker


def _fenced(content: str, *, cap: int = 4000) -> str:
    """One raw-output segment inside a CommonMark code fence.

    GitHub renders fenced content literally and monospaced, so coverage
    tables and escaped JSONL survive a failure comment unread by the
    markdown parser. The fence is one backtick longer than every run in
    the content, so a payload containing markdown fences can never close
    it; content past `cap` is cut and the cut is noted after the fence.
    """
    omitted = 0
    if len(content) > cap:
        omitted = len(content) - cap
        content = content[:cap]
    fence = "`" * max(
        3,
        1 + max((len(run) for run in re.findall(r"`+", content)), default=0),
    )
    block = f"{fence}\n{content}\n{fence}"
    if omitted:
        block += f"\n_[truncated, {omitted} chars omitted]_"
    return block


def field_block(run_id: str, headline: str, fields: dict[str, object], *,
                detail_keys: set[str] | None = None) -> str:
    """Render a marker-first status comment with optional collapsed details."""
    detail_keys = detail_keys or set()
    lines = [run_marker(run_id), headline]
    visible = []
    details = []
    for key, value in fields.items():
        line = f"- {key}={value}" if key == "run_id" else f"- {key}: {value}"
        rendered_value = str(value)
        is_long = (len(rendered_value) > DETAIL_VALUE_LENGTH
                   or "\n" in rendered_value)
        (details if key in detail_keys or is_long else visible).append(line)
    lines.extend(visible)
    if details:
        lines.extend(["", "<details><summary>Run details</summary>", "", *details,
                      "", "</details>"])
    return _with_runner_marker("\n".join(lines))


def format_status_comment(body: str) -> str:
    """Expand legacy one-line Orbi status comments into field blocks."""
    if not isinstance(body, str):
        return body
    body = redact_secrets(body)
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
    if (
        first.startswith("Orbi merged PR:")
        # A pre-#1431 one-liner carries its fields in parentheses and is
        # still expanded below; the new merged body has no such group
        # and is already in its final, idempotent shape (Issue #1431).
        and not re.search(r"\((?=[^)]*=)[^)]*\)", first)
    ):
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


# One hidden marker per FAILURE FINGERPRINT (Issue #825): rides under
# the run marker on a recoverable failure comment and is the key the
# comment-dedup and the dead-loop streak scan match on. The optional
# `:<count>` suffix is the repeat counter the in-place dedup bumps —
# the number of identical failures this one comment reports.
FAILURE_MARKER_PATTERN = re.compile(
    r"<!-- orbi:fail=([0-9a-f]{16})(?::(\d+))? -->")
FAILURE_MARKER_TEMPLATE = "<!-- orbi:fail={fingerprint} -->"


def failure_marker(fingerprint: str) -> str:
    """Return the hidden marker for one failure fingerprint."""
    if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{16}", fingerprint):
        raise ValueError(
            f"invalid failure fingerprint: {fingerprint!r}"
        )
    return FAILURE_MARKER_TEMPLATE.format(fingerprint=fingerprint)


def failure_repeat_count(body: str) -> int:
    """The repeat count of the body's failure marker (a count-less
    marker counts 1); 0 when the body carries no marker at all."""
    if not isinstance(body, str):
        return 0
    match = FAILURE_MARKER_PATTERN.search(body)
    if match is None:
        return 0
    return int(match.group(2) or 1)


def bump_failure_repeat(body: str, fingerprint: str) -> str:
    """Return the body with the failure marker's repeat count raised by
    one — the in-place update of the identical-failure comment (Issue
    #825). A count-less marker counts 1, so the first bump renders
    `:2`."""
    if not isinstance(body, str):
        raise ValueError("failure comment body must be a string")
    match = FAILURE_MARKER_PATTERN.search(body)
    if match is None or match.group(1) != fingerprint:
        raise ValueError(
            f"body does not carry the failure marker for {fingerprint!r}"
        )
    return (
        f"{body[:match.start()]}"
        f"<!-- orbi:fail={fingerprint}:{int(match.group(2) or 1) + 1} -->"
        f"{body[match.end():]}"
    )


# One hidden marker per RECOVERABLE FAILURE CAUSE (Issue #1465): the fold
# key `failure_scene` matches is `(run_id, reason_code)`. This marker
# carries the reason_code and an optional `:<count>` repeat suffix. It is
# deliberately NOT `orbi:fail=`: that marker feeds the #825 streak
# escalation, and a recoverable failure never escalates.
RECOVERED_MARKER_PATTERN = re.compile(
    r"<!-- orbi:recovered=([a-z_]+)(?::(\d+))? -->")
RECOVERED_MARKER_TEMPLATE = "<!-- orbi:recovered={reason_code} -->"


def recovered_marker(reason_code: str) -> str:
    """Return the hidden fold marker for one recoverable-failure cause."""
    if (not isinstance(reason_code, str)
            or reason_code not in REASON_CODES):
        raise ValueError(
            f"invalid recoverable reason code: {reason_code!r}"
        )
    return RECOVERED_MARKER_TEMPLATE.format(reason_code=reason_code)


def recovered_repeat_count(body: str) -> int:
    """The repeat count of the body's recovered marker (a count-less
    marker counts 1); 0 when the body carries no marker at all."""
    if not isinstance(body, str):
        return 0
    match = RECOVERED_MARKER_PATTERN.search(body)
    if match is None:
        return 0
    return int(match.group(2) or 1)


def bump_recovered_repeat(body: str, reason_code: str, previous: int) -> str:
    """Return the body with the recovered marker's repeat count set to
    `previous + 1` — the same cause's next occurrence (Issue #1465)."""
    if not isinstance(body, str):
        raise ValueError("recovered comment body must be a string")
    match = RECOVERED_MARKER_PATTERN.search(body)
    if match is None or match.group(1) != reason_code:
        raise ValueError(
            f"body does not carry the recovered marker for {reason_code!r}"
        )
    return (
        f"{body[:match.start()]}"
        f"<!-- orbi:recovered={reason_code}:{previous + 1} -->"
        f"{body[match.end():]}"
    )


def recoverable_scene_body(
    *, run_id: str, recoverable_name: str, record: Failure,
    detail: str, run_info: str,
) -> str:
    """Render the classified recoverable-failure comment (Issue #1465).

    A recoverable failure retries on the next tick under the same run
    id; the headline names the CLASSIFIED reason — the same
    `failure.classify` code the blocked path uses — plus the retry, and
    the raw provider detail moves into a collapsed `<details>`. The
    comment carries the hidden `<!-- orbi:recovered=<reason_code> -->`
    fold marker and deliberately no `orbi:failure:v1` block and no
    `orbi:fail=` marker: a recoverable failure never escalates, whatever
    the count, so the Issue stays `ai-in-progress` (#1455/#227).
    `failure_scene` folds the same `(run_id, reason_code)` into one
    comment and gives a different cause its own.
    """
    return (
        f"{run_marker(run_id)}\n"
        f"{recovered_marker(record.reason_code)}\n"
        f"Orbi {recoverable_name}: {record.reason_code}; retrying on the "
        "next tick\n\n"
        "The Issue stays ai-in-progress and the next tick resumes the "
        f"same run ({run_info}).\n\n"
        "<details><summary>Diagnosis</summary>\n\n"
        f"- reason_code: {record.reason_code}\n"
        f"- action: {record.action_code}\n"
        f"- retry_safe: {str(record.retry_safe).lower()}\n\n"
        f"{_fenced(_redact_local_paths(detail))}\n\n"
        "</details>"
    )


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


def find_started_comment(
    comments: list[dict], run_id: str,
) -> dict | None:
    """Return this run's started scene comment, or None.

    The run marker alone is not enough: the run's other comments
    (progress, opened PR scene, milestones) carry it too. The started
    comment is the one that also carries the `Orbi started Pi:`
    headline. A missing comment (deleted by a human) returns None so the
    caller recreates it once (Issue #1369).
    """
    marker = run_marker(run_id)
    for comment in comments:
        body = comment.get("body")
        if (isinstance(body, str) and marker in body
                and STARTED_HEADER in body):
            return comment
    return None


def issue_field(issue: int, title: str) -> str:
    """Render the progress comment's issue value: `#<number> <title>`.

    The issue line shows the number AND the title so a
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
    """Format seconds as `45s`, `3m 12s` or `1h 2m 3s` (hours are omitted
    when zero; once a larger unit renders, a zero unit stays — except the
    seconds at the whole-hour boundary: 3600 renders `1h 0m`, not
    `1h 0m 0s`)."""
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
        f"- role: {value('role')}",
        f"- last activity: {value('last_activity')}",
        f"- tests: {value('tests')}",
        f"- PR: {value('pr')}",
    ]
    # Exceptional state belongs with the at-a-glance status. A normal
    # first or second review round carries no useful signal on its own.
    if state.get("recovery"):
        lines.append(f"- recovery: {state['recovery']}")
    if state.get("review_round", 0) >= 3:
        lines.append(f"- review/fix round: {value('review_round')}")

    # Identifiers and routine bookkeeping remain available on demand.
    # Both hidden protocol markers stay outside this fold.
    details = [
        f"- issue: {issue_field(state['issue'], state['issue_title'])}",
        f"- run_id={state['run_id']}",
        f"- priority: {value('priority')}",
        f"- phase: {value('phase')}",
        f"- elapsed: {value('elapsed')}",
        f"- last action: {value('last_action')}",
        f"- branch: {value('branch')}",
        f"- session: {value('session')}",
    ]
    lines.extend(["", "<details><summary>Run details</summary>", "", *details,
                  "", "</details>"])
    # The progress comment (and the blocked / fix-needed
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
    never got that far. `milestone` posts a short
    standalone comment. `failure_scene` updates the run's identical
    recoverable-failure comment in place instead of appending a duplicate.
    Every call goes through `run_command` (gh api)
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
        # the list/create endpoint is not a GitHub REST route and 404s.
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

    def started(self, body: str) -> int:
        """Create or resume the run's started scene comment; return its id.

        A run has exactly one `Orbi started Pi:` comment (Issue #1369):
        a resumed run PATCHes the existing comment (located by run marker
        plus headline) instead of posting a duplicate; a comment that
        cannot be found (deleted by a human) is created once and later
        resumes update that new one.
        """
        existing = find_started_comment(
            self._list_comments(), self.run_id,
        )
        if existing is not None:
            comment_id = int(existing["id"])
            self._patch_comment(comment_id, body)
            return comment_id
        return self._post_comment(body)

    def ensure(self, body: str) -> int:
        """Create or resume the run's progress comment; return its id.

        The run's other marker-carrying comments (scene comments,
        milestones) are never touched: only the comment that also
        carries the progress header is resumed.
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

    def milestone(self, text: str, *, block: str | None = None) -> None:
        """Post a short standalone milestone comment (mobile notification).

        The milestone carries the hidden run marker and the visible
        `run_id=` field like every other comment of the run (
        one run_id end to end, every comment of the attempt carries
        both). The visible field is appended when the text does not
        carry it already, so the contract holds for every milestone
        without repeating the field.

        `block` is an optional hidden machine-readable block (the
        `orbi:failure:v1` record, Issue #1322). A blocked/fix-needed
        milestone IS a failure comment, so the record sits directly
        under the run marker and a status reader never parses the
        visible text.
        """
        headline, separator, detail = text.partition(": ")
        fields: dict[str, object] = {}
        if separator:
            pairs = re.findall(r"([A-Za-z_][\w-]*)=([^\s)]+)", detail)
            fields.update(pairs)
            prose = re.sub(r"\s*\(?[A-Za-z_][\w-]*=[^\s)]+", "", detail)
            prose = prose.strip(" ()")
            if prose or len({key for key, _ in pairs}) != len(pairs):
                fields["result"] = prose if headline == "merged" else detail
        fields["run_id"] = self.run_id
        body = field_block(
            self.run_id, f"{MILESTONE_PREFIX} {headline}", fields,
            detail_keys={"merge_commit"} if headline == "merged" else set(),
        )
        if block:
            marker, _, rest = body.partition("\n")
            body = f"{marker}\n{block}\n{rest}"
        self._post_comment(body)

    def failure_scene(self, body: str) -> None:
        """Create or update this run's recoverable-failure comment.

        A recoverable failure (a provider 401, a timeout, a hung model
        request) retries on the next tick under the same run id. The fold
        key is `(run_id, reason_code)`, carried by the hidden
        `<!-- orbi:recovered=<reason_code> -->` marker: the same cause
        PATCHes ITS comment in place (the marker's repeat count rises and
        the body carries the newest occurrence — the last-seen detail),
        while a different reason_code posts its own comment. The run
        marker scopes the fold, so another run's comment is never
        hijacked. A body without a marker always posts, so a failure
        comment is never lost.
        """
        rendered = format_status_comment(body)
        match = RECOVERED_MARKER_PATTERN.search(rendered)
        if match is None:
            self._post_comment(rendered)
            return
        reason_code = match.group(1)
        for comment in self._list_comments():
            existing = comment.get("body")
            if not isinstance(existing, str):
                continue
            if run_marker(self.run_id) not in existing:
                continue
            existing_match = RECOVERED_MARKER_PATTERN.search(existing)
            if (existing_match is None
                    or existing_match.group(1) != reason_code):
                continue
            self._patch_comment(
                int(comment["id"]),
                bump_recovered_repeat(
                    rendered, reason_code,
                    recovered_repeat_count(existing),
                ),
            )
            return
        self._post_comment(rendered)

    def finish(self, body: str) -> None:
        """Publish the final outcome body on the run's progress comment.

        When no comment is tracked yet — a run that fails before
        `ensure` (a release declaration parse error calls
        `finish` first) — the final outcome is still published: the
        run's progress comment is located or created exactly like
        `ensure`, never a `RuntimeError` and never a duplicate comment
        for a resumed run.
        """
        if self.comment_id is None:
            self.ensure(body)
            return
        self.patch(body)


# --- test evidence and run-scene helpers (the progress state belongs
# --- to the progress module) ----


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
# category is failed/passed/errors: pytest always renders the outcome
# counts (failed/passed) before the auxiliary ones (skipped,
# deselected, xfailed, xpassed, warnings, errors last), so a
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
    not exist (the contract test command writes the log
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


def _progress_state(ctx: RunContext, *, title: str, role: str,
                    started: float, pr_url: str | None,
                    review_round: int, priority: str,
                    activity: dict | None = None,
                    review: str = "pending") -> dict:
    """Collect the current run state for the GitHub progress comment.

    `title` is the issue's GitHub title: the progress
    comment's issue line shows `#<number> <title>` in every scene. It
    is required — the GitHub issue data contract guarantees a
    non-empty string title (every runner scan fetches it), and a
    missing title fails fast in `progress.issue_field` instead of
    fabricating one.
    `priority` is the pickup priority of the issue (`p0` or `normal`)
    derived from the issue's labels at claim/resume time.
    `activity` is the live state from the `stream_pi` watcher while a Pi
    session runs (fresh and already read); without it the newest session
    file is full-scanned. Activity snapshotting is best-effort
    observability: a read failure is logged and reported as "no session
    yet", it never blocks the task.
    """
    if activity is None:
        try:
            activity = activity_snapshot(ctx.worktree / ".pi-session")
        except Exception:
            LOGGER.exception("issue=%s activity snapshot failed", ctx.issue)
            activity = None
    return {
        "run_id": ctx.run_id,
        "issue": ctx.issue,
        "issue_title": title,
        "role": role,
        "priority": priority,
        "phase": (activity or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (activity or {}).get("last_activity"),
        "last_action": (activity or {}).get("action"),
        "tests": read_test_result(ctx.worktree),
        "review_round": review_round,
        "review": review,
        "branch": ctx.branch,
        "pr": pr_url,
        "session": (activity or {}).get("session_id"),
        # Idle-stall recovery state: `term` / `kill` while
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
    """Run one progress-publishing step as a pure bypass.

    The main delivery path is claim -> worktree -> Pi -> verify PR ->
    review -> fix -> merge; the GitHub progress comment is observability
    on the side. A publishing failure (404, rate limit, API shape
    change) is logged as `progress_publish_failed` and never fails the
    delivery, never marks the Issue `ai-blocked`, and never skips
    `run_pi` / `delivery_step`. This is the same semantics as the
    in-stream live-PATCH callback; the bypass covers the whole
    `ProgressPublisher` path (ensure / milestone / finish).
    """
    try:
        action()
    except Exception:
        LOGGER.exception(
            "progress_publish_failed run=%s issue=%s role=%s",
            run_id, issue_context(source_repo, issue), role,
        )


def _finish_outcome_body(*, outcome: str, detail: str,
                         next_step: str, pr_url: str | None,
                         number: int, source_repo: str,
                         action: str | None = None,
                         reason: str | None = None,
                         diagnosis: str | None = None) -> str:
    """Render a terminal outcome with action, reason, and diagnosis."""
    if not next_step:
        event(
            "progress_finish_missing_next_step", level=logging.WARNING,
            call_point="_finish_progress_body", issue=number,
            repo=source_repo, outcome=outcome,
        )
    legacy_action = action is None
    blocked = outcome == "blocked"
    disposition = (
        "waiting on a human decision" if blocked
        else "the engine will retry"
    )
    if reason is None:
        reason = (
            "Orbi could not complete the delivery because a required "
            "operation failed." if blocked else
            "Orbi found a problem that must be fixed before it can continue."
        )
    if diagnosis is None:
        diagnosis = detail
    paragraphs = [f"**Orbi {outcome} — {disposition}**"]
    if legacy_action:
        user_action = next_step if blocked and next_step else "Nothing"
        paragraphs.append(f"What you need to do: {user_action}")
    elif action:
        paragraphs.append(f"What you need to do: {action}")
    if reason:
        paragraphs.append(f"What happened: {reason}")
    else:
        paragraphs.append("What happened: No reason was provided.")
    if legacy_action:
        if blocked and pr_url:
            engine_action = (
                "Orbi will wait for the required action; the open PR is "
                f"{pr_url}."
            )
        elif blocked:
            engine_action = "Orbi will wait for a human to resolve this Issue."
        else:
            engine_action = (
                next_step or "Orbi will wait for a human to resolve this Issue."
            )
        paragraphs.append(f"What Orbi will do next: {engine_action}")
    elif not action and next_step:
        paragraphs.append(f"What Orbi will do next: {next_step}")
    paragraphs.append(
        "<details><summary>Raw error</summary>\n"
        f"{diagnosis}\n"
        "</details>"
    )
    return "\n\n".join(paragraphs)


def _finish_progress_body(*, number: int, title: str, run_id: str,
                          role: str, branch: str | None,
                          worktree: Path | None, pr_url: str | None,
                          review_round: int, priority: str, detail: str,
                          next_step: str, outcome: str,
                          source_repo: str, action: str | None = None,
                          reason: str | None = None,
                          diagnosis: str | None = None) -> str:
    """Render the terminal progress scene shared by every finish path."""
    return _progress_body(_progress_state(
        RunContext(
            run_id=run_id, issue=number, branch=branch or "-",
            worktree=worktree or Path("-"), source_repo=source_repo,
        ),
        title=title, role=role, started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=_finish_outcome_body(
        outcome=outcome, detail=detail, next_step=next_step, pr_url=pr_url,
        number=number, source_repo=source_repo, action=action,
        reason=reason, diagnosis=diagnosis,
    ))
