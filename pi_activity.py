#!/usr/bin/env python3
"""Live Pi session activity tracking (Issues #24, #40).

Pi writes its session as an append-only JSONL file under the task worktree
(`.pi-session/*.jsonl`) while it runs. This module follows that file and
reduces it to a small, redacted activity snapshot: session id, event count,
current phase, last activity time, the last meaningful action (the newest
tool call or assistant statement), and the result of the newest tool call
(`ok` / `error`). A tool result reports the outcome without overwriting the
action that produced it.

The module also formats the journal lines (Issue #40): the full invariant
scene (branch, worktree, session file) is only ever built for the
run_start / run_failed / run_end lines; activity and heartbeat lines carry
short changed fields only. All lines are stable `key=value` pairs (values
with spaces are double-quoted) so an agent can parse them without a log
framework.

Only assistant and toolResult messages are agent activity. User messages
carry the full prompt and Issue body and are never summarized. The module
never writes anything; the runner and the status command decide what to log
or print. No database, queue, or daemon — just reading a local file.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_SUMMARY_LENGTH = 200

# Ordered: the first matching rule wins for a bash command.
PHASE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(pytest|coverage)\b"), "test"),
    (re.compile(r"\bgh\s+pr\b"), "pr"),
    (re.compile(r"\bgit\s+push\b"), "push"),
    (re.compile(r"\bgit\s+commit\b"), "commit"),
    (re.compile(r"\bgit\s+(fetch|merge)\b"), "base"),
    (re.compile(r"\bgit\s+worktree\b"), "worktree"),
    (re.compile(r"\bplaywright\b"), "ui"),
)

# Obvious token shapes that must never reach the journal or a status output.
TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "ghp_<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "sk-<redacted>"),
)


def latest_session_file(session_dir: Path) -> Path | None:
    """Return the newest `*.jsonl` in the session dir, or None."""
    if not session_dir.is_dir():
        return None
    files = [path for path in session_dir.glob("*.jsonl") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def read_new_events(path: Path, offset: int) -> tuple[list[dict], int]:
    """Read complete JSONL records from `offset`.

    Returns `(records, new_offset)`. A trailing partial line (Pi is still
    writing it) is left for the next read. Unparseable or non-object lines
    are skipped. A file that shrank below `offset` is re-read from the
    start.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], offset
    if size < offset:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    complete = data[: end + 1]
    records: list[dict] = []
    for line in complete.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, offset + len(complete)


def parse_iso_utc(value: str) -> float | None:
    """Parse an ISO-8601 timestamp (UTC assumed when naive); None if bad."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def sanitize(text: str) -> str:
    """Single-line, truncated, token-redacted summary safe for logs."""
    text = " ".join(text.split())
    for pattern, replacement in TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_SUMMARY_LENGTH:
        text = text[:MAX_SUMMARY_LENGTH].rstrip() + "..."
    return text


def tool_args_summary(name: str, arguments: dict) -> str:
    """Return the most informative single argument of a tool call."""
    if name == "bash":
        command = arguments.get("command")
        return command if isinstance(command, str) else ""
    for key in ("path", "file_path", "tool", "query"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def phase_for(name: str, arguments: dict) -> str:
    """Map a tool call to a human-readable work phase."""
    if name == "bash":
        command = arguments.get("command")
        command = command if isinstance(command, str) else ""
        for pattern, phase in PHASE_RULES:
            if pattern.search(command):
                return phase
        return "bash"
    return name


class SessionWatcher:
    """Follow the newest Pi session JSONL and track the latest activity.

    With `known_files` (the session files that existed before the
    tracked Pi process started) the watcher never binds to a known file:
    it follows the newest file that appears, and switches to a newer
    file whenever one appears, resetting its state. A resumed Fixer run
    creates a NEW JSONL in the same `.pi-session` directory, so this is
    what makes the journal report the session of the current invocation
    instead of the previous run's (Issue #45). `known_files=None` keeps
    the original bind-once semantics used by full-scan snapshots.
    """

    def __init__(self, session_dir: Path,
                 now: Callable[[], float] = time.time,
                 known_files: set[Path] | None = None) -> None:
        self.session_dir = session_dir
        self._now = now
        self._known_files = known_files
        self.session_file: Path | None = None
        self.session_id: str | None = None
        self.phase: str | None = None
        self.last_activity: str | None = None
        self.action: str | None = None
        self.result: str | None = None
        # The role of the newest message record: a `toolResult` means the
        # model is expected to reply next (model_wait, Issue #40).
        self.last_role: str | None = None
        self.events = 0
        self.start_time = now()
        self._offset = 0
        self._last_activity_epoch: float | None = None

    def poll(self) -> dict:
        """Read new session records; return the current activity state."""
        if self.session_file is None:
            self.session_file = self._next_session_file()
        elif self._known_files is not None:
            newest = self._next_session_file()
            if newest is not None and newest != self.session_file:
                self._switch_to(newest)
        changed = False
        if self.session_file is not None:
            records, self._offset = read_new_events(
                self.session_file, self._offset,
            )
            for record in records:
                self._apply(record)
            changed = bool(records)
        return self.state(changed=changed)

    def _next_session_file(self) -> Path | None:
        """The file to follow: the newest file, unless it was already
        present before the tracked process started (then: none yet)."""
        newest = latest_session_file(self.session_dir)
        if newest is None:
            return None
        if self._known_files is not None and newest in self._known_files:
            return None
        return newest

    def _switch_to(self, path: Path) -> None:
        """Bind to a newer session file and reset all tracked state."""
        self.session_file = path
        self.session_id = None
        self.phase = None
        self.last_activity = None
        self.action = None
        self.result = None
        self.last_role = None
        self.events = 0
        self.start_time = self._now()
        self._offset = 0
        self._last_activity_epoch = None

    def state(self, changed: bool = False) -> dict:
        """Return the activity state as a plain dict (no file access)."""
        now = self._now()
        if self._last_activity_epoch is not None:
            stale = now - self._last_activity_epoch
        else:
            stale = now - self.start_time
        return {
            "session_id": self.session_id,
            "session_file": str(self.session_file) if self.session_file
            else None,
            "events": self.events,
            "phase": self.phase or "starting",
            "last_activity": self.last_activity,
            "action": self.action,
            "result": self.result,
            # True only while the newest session event is a tool result:
            # the model is expected to reply next, so a long silence is a
            # slow model, not a stalled agent (Issue #40).
            "model_wait": self.last_role == "toolResult",
            "changed": changed,
            "stale_seconds": max(0.0, stale),
        }

    def _apply(self, record: dict) -> None:
        self.events += 1
        record_type = record.get("type")
        if record_type == "session":
            session_id = record.get("id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
            return
        if record_type != "message":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        role = message.get("role")
        if role == "assistant":
            self._apply_assistant(message)
        elif role == "toolResult":
            self._apply_tool_result(message)
        self.last_role = role
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            epoch = parse_iso_utc(timestamp)
            if epoch is not None:
                self.last_activity = timestamp
                self._last_activity_epoch = epoch

    def _apply_assistant(self, message: dict) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "toolCall":
                name = item.get("name")
                name = name if isinstance(name, str) and name else "tool"
                arguments = item.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                self.phase = phase_for(name, arguments)
                self.action = sanitize(
                    f"{name} {tool_args_summary(name, arguments)}",
                ).strip()
                # The previous tool's result no longer describes this call.
                self.result = None
            elif kind == "text":
                self.action = "assistant text"
                self.result = None
            elif kind == "thinking":
                self.action = "thinking"
                self.result = None

    def _apply_tool_result(self, message: dict) -> None:
        name = message.get("toolName")
        name = name if isinstance(name, str) and name else "tool"
        # The result reports the outcome only; the action that produced it
        # stays visible (Issue #40).
        self.result = "error" if message.get("isError") else "ok"
        if name != "bash":
            self.phase = name


def activity_snapshot(session_dir: Path) -> dict | None:
    """Full-scan the newest session file; None when no session exists yet."""
    watcher = SessionWatcher(session_dir)
    watcher.poll()
    if watcher.session_file is None:
        return None
    return watcher.state()


def format_duration(seconds: float) -> str:
    """Compact human duration: `0.5s`, `6s`, `14m`, `1h5m`."""
    if seconds <= 0:
        return "0s"
    if seconds < 1:
        return f"{seconds:.1f}s"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def quote_value(value: str) -> str:
    """Double-quote a key=value field when it needs quoting.

    Values containing spaces or double quotes are quoted; embedded double
    quotes are escaped as `\\"` so the field stays parseable by
    `parse_scene`.
    """
    if " " in value or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def format_run_scene(snapshot: dict, *, run_id: str, issue: str, role: str,
                     branch: str, worktree: str) -> str:
    """Full invariant scene for run_start / run_failed lines.

    This is the only place the full branch, worktree and session file are
    emitted; activity/heartbeat lines must not repeat them (Issue #40).
    """
    return (
        f"run={run_id} issue={issue} role={role} "
        f"branch={branch} worktree={worktree} "
        f"session={snapshot['session_id'] or '-'} "
        f"session_file={snapshot['session_file'] or '-'} "
        f"phase={snapshot['phase']} "
        f"last_activity={snapshot['last_activity'] or '-'} "
        f"action={quote_value(snapshot['action'] or '-')} "
        f"result={snapshot['result'] or '-'}"
    )


def format_end_scene(*, run_id: str, issue: str, role: str, result: str,
                     elapsed: float, pr: str, commit: str) -> str:
    """run_end line: the result plus the full debug entry (PR, commit)."""
    return (
        f"run={run_id} issue={issue} role={role} "
        f"result={result} elapsed={format_duration(elapsed)} "
        f"pr={pr} commit={commit}"
    )


def parse_scene(line: str) -> dict[str, str | None]:
    """Parse a `key=value` scene line into a dict.

    Values may be double-quoted (when they contain spaces) and may contain
    `=`. Bare words without `=` (such as the line-kind prefix `activity`,
    `heartbeat`, `run_start`, ...) are skipped. `key=` without a value
    parses to None.
    """
    fields: dict[str, str | None] = {}
    index = 0
    length = len(line)
    while index < length:
        while index < length and line[index] == " ":
            index += 1
        if index >= length:
            break
        # Read the key up to '=' or the next space.
        key_start = index
        while index < length and line[index] != "=" and line[index] != " ":
            index += 1
        key = line[key_start:index]
        if not key or index >= length or line[index] != "=":
            continue  # bare word (line-kind prefix or garbage): skip it
        index += 1  # skip '='
        if index < length and line[index] == '"':
            index += 1
            start = index
            while index < length:
                if (line[index] == "\\"
                        and index + 1 < length
                        and line[index + 1] == '"'):
                    index += 2  # escaped quote: part of the value
                    continue
                if line[index] == '"':
                    break
                index += 1
            raw = line[start:index]
            if index < length:  # closing quote found
                index += 1
            else:  # unterminated quote: keep the rest of the line
                index = length
            fields[key] = raw.replace('\\"', '"')
        else:
            start = index
            while index < length and line[index] != " ":
                index += 1
            fields[key] = line[start:index] or None
    return fields
