#!/usr/bin/env python3
"""The `orbi session` command helpers (Issue #1576).

The live Pi session viewer is a debug attachment (the journal and GitHub
remain the daily entry points): it finds the newest
`.pi-session/*.jsonl` under the configured repo's `.worktrees`
directory and prints its path, or follows it like `tail -f`. The
helpers and their constants live here so `cli.py` stays inside the
Article 3 size ratchet; `cli.py` imports them unchanged.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path


# Live Pi session following: the session subcommand is a
# debug attachment (the journal and GitHub remain the daily entry
# points). It finds the newest `.pi-session/*.jsonl` under the
# configured repo's `.worktrees` directory and prints its path, or
# follows it like `tail -f`. There is no tmux, no daemon, no new
# binary; a missing session file is a fail-fast non-zero exit (no Pi
# is running), never a guessed path.
FOLLOW_POLL_SECONDS = 0.5
SESSION_LINE_MAX = 200


def find_session_file(repo_dir: Path) -> Path | None:
    """Return the newest `.pi-session/*.jsonl` under `repo_dir/.worktrees`.

    Task worktrees live in `<repo_dir>/.worktrees/orbi-...` and
    each Pi session appends its JSONL under the worktree's
    `.pi-session` directory; the newest file by mtime is the live one.
    Returns None when no session file exists (no Pi is running).
    """
    worktrees = repo_dir / ".worktrees"
    if not worktrees.is_dir():
        return None
    files = [
        path for path in worktrees.glob("*/.pi-session/*.jsonl")
        if path.is_file()
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def follow_session_file(path: Path,
                        poll_interval: float = FOLLOW_POLL_SECONDS) -> Iterator[str]:
    """Yield the lines of `path`, then its new lines as they appear.

    `tail -f` semantics for ONE file: the generator follows the file it
    was given and never switches to a newer file that appears mid-run.
    A file that disappears (worktree cleanup) stops the
    generator — fail fast, no fallback. A file that shrank is re-read
    from the start (the same rule as the session watcher). Only
    complete lines are yielded: a trailing partial line (the writer is
    still flushing it) is left for the next read, so a record is never
    split into fragments or dropped by the `--pretty` parser (the same
    rule as the session watcher).
    """
    offset = 0
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size < offset:
            offset = 0
        if size > offset:
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
            end = data.rfind("\n")
            if end < 0:
                # No complete line yet: the writer is still flushing
                # the current line; re-read it on the next poll.
                time.sleep(poll_interval)
                continue
            complete = data[: end + 1]
            offset += end + 1
            for line in complete.splitlines():
                if line:
                    yield line
        time.sleep(poll_interval)


def _session_content_summary(message: dict) -> str:
    """One short summary of a message record's content."""
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "toolCall":
                name = item.get("name")
                name = name if isinstance(name, str) and name else "tool"
                arguments = item.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                detail = arguments.get("command")
                if not isinstance(detail, str):
                    for key in ("path", "file_path", "tool", "query"):
                        value = arguments.get(key)
                        if isinstance(value, str) and value:
                            detail = value
                            break
                detail = detail if isinstance(detail, str) else ""
                parts.append(f"tool:{name} {detail}".strip())
            elif kind == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(f"text:{text}")
            elif kind == "thinking":
                text = item.get("thinking")
                if isinstance(text, str) and text:
                    parts.append(f"thinking:{text}")
        return "; ".join(parts)
    return ""


def format_session_line(record: dict) -> str:
    """Render one session JSONL record as a one-line summary.

    The summary carries the timestamp, the record kind and a short
    role/content digest (tool name + first argument, text or thinking
    truncated) — never the full prompt (user messages are summarized
    without their content). Long content is truncated so one record
    stays on one line.
    """
    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else "-"
    record_type = record.get("type")
    if record_type == "session":
        session_id = record.get("id")
        session_id = session_id if isinstance(session_id, str) else "-"
        return f"{timestamp} session {session_id}"
    if record_type != "message":
        return f"{timestamp} {record_type}"
    message = record.get("message")
    if not isinstance(message, dict):
        return f"{timestamp} message -"
    role = message.get("role")
    role = role if isinstance(role, str) and role else "-"
    if role == "toolResult":
        name = message.get("toolName")
        name = name if isinstance(name, str) and name else "tool"
        outcome = "error" if message.get("isError") else "ok"
        return f"{timestamp} toolResult {name} {outcome}"
    summary = _session_content_summary(message)
    if not summary:
        return f"{timestamp} {role}"
    if len(summary) > SESSION_LINE_MAX:
        summary = summary[:SESSION_LINE_MAX].rstrip() + "..."
    return f"{timestamp} {role} {summary}"
