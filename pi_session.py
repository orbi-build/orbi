#!/usr/bin/env python3
"""Parse Pi session JSONL files into sanitized live activity events.

Pi writes one JSON object per line to `<worktree>/.pi-session/*.jsonl` while a
session runs. This module turns those lines into short, redacted summaries that
the runner can log live to the journal and that `muyan_pilot.py status` can
display. Full prompts, issue bodies, tool outputs and reasoning are never
captured in the events.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SUMMARY_LIMIT = 160
REDACTED = "<redacted>"

# Key phases surfaced to the user (journal + status + Issue comments).
PHASE_TEST = "test"
PHASE_VERIFY = "verify"
PHASE_COMMIT = "commit"
PHASE_PUSH = "push"
PHASE_PR = "pr"
PHASE_ISSUE_COMMENT = "issue_comment"
PHASE_WORKTREE = "worktree"
PHASE_BRANCH = "branch"
PHASE_SETUP = "setup"
PHASE_UI_TEST = "ui_test"
PHASE_READ = "read"
PHASE_EDIT = "edit"
PHASE_SEARCH = "search"
PHASE_COMMAND = "command"
PHASE_THINKING = "thinking"
PHASE_REPLY = "reply"
PHASE_TOOL_RESULT = "tool_result"
PHASE_SESSION_START = "session_start"
PHASE_SESSION_END = "session_end"

_GITHUB_TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_FLAG = re.compile(r"(--token[= ])\S+")
_API_KEY = re.compile(
    r"([\"']?[A-Za-z0-9_.-]*api[_-]?key[A-Za-z0-9_.-]*[\"']?\s*[=:]\s*)"
    r"([\"']?)[A-Za-z0-9._~+/=-]+\2",
    re.IGNORECASE,
)

_BASH_PHASES = (
    (("pip install", "npm install", "apt install"), PHASE_SETUP),
    (("pytest", "coverage", "npm test", "unittest"), PHASE_TEST),
    (("muyan_pilot.py status",), PHASE_VERIFY),
    (("git commit",), PHASE_COMMIT),
    (("git push",), PHASE_PUSH),
    (("gh pr",), PHASE_PR),
    (("gh issue comment",), PHASE_ISSUE_COMMENT),
    (("git worktree",), PHASE_WORKTREE),
    (("git branch",), PHASE_BRANCH),
    (("playwright",), PHASE_UI_TEST),
)

_TOOL_PHASES = {
    "read": PHASE_READ,
    "write": PHASE_EDIT,
    "edit": PHASE_EDIT,
    "grep": PHASE_SEARCH,
    "glob": PHASE_SEARCH,
    "search": PHASE_SEARCH,
    "playwright": PHASE_UI_TEST,
    "mcp": "mcp",
}

_SUMMARY_KEYS = ("command", "path", "file", "pattern", "query", "glob")


def redact(text: str) -> str:
    """Replace credentials in a string before it is logged or shown."""
    text = _GITHUB_TOKEN.sub(REDACTED, text)
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _TOKEN_FLAG.sub(r"\1" + REDACTED, text)
    text = _API_KEY.sub(r"\1\2" + REDACTED + r"\2", text)
    return text


def _truncate(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 1] + "…"


def classify_phase(name: str, arguments: object) -> str:
    """Map a tool call to a key phase (test, commit, push, pr, ...)."""
    if name == "bash":
        if isinstance(arguments, dict):
            command = arguments.get("command")
            if isinstance(command, str):
                for needles, phase in _BASH_PHASES:
                    if any(needle in command for needle in needles):
                        return phase
        return PHASE_COMMAND
    return _TOOL_PHASES.get(name, name)


def summarize_tool(name: str, arguments: object) -> str:
    """Build a short sanitized summary for one tool call."""
    if isinstance(arguments, dict):
        for key in _SUMMARY_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value:
                if name == "bash" and key == "command":
                    value = value.splitlines()[0].strip()
                return _truncate(redact(value))
        return _truncate(redact(json.dumps(arguments, ensure_ascii=False)))
    return name


def _event(kind: str, phase: str, timestamp: str, summary: str) -> dict:
    return {
        "kind": kind,
        "phase": phase,
        "timestamp": timestamp,
        "summary": _truncate(redact(summary)),
    }


def _assistant_events(message: dict, timestamp: str) -> list[dict]:
    """Summarize an assistant message: each tool call, then its text."""
    events = []
    for part in message.get("content", []):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "toolCall":
            name = part.get("name")
            if not isinstance(name, str):
                continue
            events.append(_event(
                "assistant", classify_phase(name, part.get("arguments")),
                timestamp, summarize_tool(name, part.get("arguments")),
            ))
    for part in message.get("content", []):
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                events.append(_event("assistant", PHASE_REPLY, timestamp, text))
                break
    if not events:
        events.append(_event("assistant", PHASE_THINKING, timestamp, "thinking"))
    return events


def _tool_result_event(message: dict, timestamp: str) -> dict:
    name = message.get("toolName")
    if not isinstance(name, str) or not name:
        name = "tool"
    status = "failed" if message.get("isError") else "ok"
    return _event("toolResult", PHASE_TOOL_RESULT, timestamp, f"{name} {status}")


def parse_session_events(path: Path) -> list[dict]:
    """Read a session JSONL file and return sanitized activity events.

    Malformed lines are skipped: Pi may still be writing the last line when
    the file is read, and a crashed session may end mid-line.
    """
    if not path.is_file():
        return []
    events = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "session":
            timestamp = data.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp:
                continue
            event = _event(
                "session_start", PHASE_SESSION_START, timestamp, "session started",
            )
            session_id = data.get("id")
            if isinstance(session_id, str) and session_id:
                event["session_id"] = session_id
            events.append(event)
        elif kind in ("model_change", "thinking_level_change"):
            if kind == "model_change":
                provider = data.get("provider")
                model = data.get("modelId")
                summary = "model " + "/".join(
                    value for value in (provider, model)
                    if isinstance(value, str) and value
                )
            else:
                level = data.get("thinkingLevel")
                summary = "thinking level " + (
                    level if isinstance(level, str) else "unknown"
                )
            events.append(_event(kind, kind, str(data.get("timestamp")), summary))
        elif kind == "session_end":
            timestamp = data.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                events.append(_event(
                    "session_end", PHASE_SESSION_END, timestamp, "session ended",
                ))
        elif kind == "message":
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            timestamp = data.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp:
                continue
            if role == "assistant":
                events.extend(_assistant_events(message, timestamp))
            elif role == "toolResult":
                events.append(_tool_result_event(message, timestamp))
    return events


def latest_activity(events: list[dict]) -> tuple[str, str, str] | None:
    """Return (phase, summary, timestamp) of the newest event, or None."""
    if not events:
        return None
    newest = max(events, key=lambda event: event["timestamp"])
    return newest["phase"], newest["summary"], newest["timestamp"]


def newest_session_file(worktree: Path) -> Path | None:
    """Return the newest session JSONL in the worktree, or None."""
    session_dir = worktree / ".pi-session"
    if not session_dir.is_dir():
        return None
    candidates = list(session_dir.glob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def session_file_for(worktree: Path, started: float) -> Path | None:
    """Return the newest session JSONL created at or after `started`."""
    newest = newest_session_file(worktree)
    if newest is None or newest.stat().st_mtime < started:
        return None
    return newest


def summarize_session_file(path: Path) -> tuple[str | None, tuple[str, str, str] | None]:
    """Return (session id, latest activity) for one session file."""
    events = parse_session_events(path)
    session_id = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == "session":
            value = data.get("id")
            if isinstance(value, str):
                session_id = value
            break
    return session_id, latest_activity(events)
