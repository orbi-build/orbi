"""The bounded evidence of one failure comment (Issue #2202).

The failure report copies what survives a terminal worktree cleanup into
ONE comment: the tail of the failing command's streams, a structural
summary of the Pi session log and the tail of `.orbi/test.log`. This module
owns that rendering — the segment caps, the de-colouring, the session
summary and the shrink-to-fit loop that keeps an oversized comment inside
`FAILURE_COMMENT_MAX_CHARS` by shrinking ONLY the evidence, never its
structure (Issue #1711).

A verbatim extraction out of `orbi.failure_report` (Article 3.5, Issue
#2202): that module sits at the 1,000-line cap the size ratchet freezes, so
the next feature landing in it splits it by domain first. The extraction
changes no behaviour — the same functions and the same bytes, re-exported
by `failure_report` under their existing names.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from orbi import language
from orbi.failure import redact_local_paths
from orbi.pi_activity import latest_session_file
from orbi.progress import fenced


_SGR_RE = re.compile(r"(?:\x1b\[[0-9;]*m|\^\[\[[0-9;]*m)")


def _tail_text(path: Path, *, lines: int = 20, chars: int = 4000) -> str:
    """Read a bounded, de-coloured tail for failure evidence."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # UTF-8 characters are at most four bytes.  This is enough to
            # retain the requested character tail without reading a huge
            # test log or session file into memory.
            window = min(size, chars * 4 + 1)
            handle.seek(size - window)
            content = handle.read(window).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return "<unavailable>"
    tail = "\n".join(content.splitlines()[-lines:])
    tail = _SGR_RE.sub("", tail)
    return tail[-chars:] if len(tail) > chars else tail


# The number of session records a failure comment summarizes.
SESSION_SUMMARY_LIMIT = 20


def _latest_session_file(worktree: Path | None) -> Path | None:
    """The most recent pi session log in the worktree, or None.

    Only the root is resolved here: the "newest `*.jsonl` by mtime" rule
    is `pi_activity.latest_session_file`, the single implementation the
    live activity watcher also uses, so the rule cannot drift (Issue
    #1854).
    """
    if worktree is None:
        return None
    return latest_session_file(worktree / ".pi-session")


def _session_summary(session_file: Path,
                     *, limit: int = SESSION_SUMMARY_LIMIT) -> str:
    """Structured summary of the last session records.

    One line per parsed record — timestamp, type, role, tool name and
    content-block kinds; the record text itself never enters the comment
    (the full log path is named next to the segment). A record whose
    text blocks repeat the previous record's is skipped: pi logs the
    final assistant text twice (once inside the full message, once
    standalone), which used to duplicate whole paragraphs in the
    comment. Unparsable tail lines are skipped; an unreadable file is
    the `<unavailable>` placeholder.
    """
    try:
        with session_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 65536)
            handle.seek(size - window)
            content = handle.read(window).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return "<unavailable>"
    summary: list[str] = []
    previous_text: str | None = None
    for line in reversed(content.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        text = "".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text and text == previous_text:
            continue
        previous_text = text or None
        parts = [str(record.get("type", "?"))]
        if message.get("role"):
            parts.append(f"role={message['role']}")
        if message.get("toolName"):
            parts.append(f"tool={message['toolName']}")
        kinds = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "?"))
            if kind == "toolCall" and block.get("name"):
                kind = f"toolCall:{block['name']}"
            kinds.append(kind)
        if kinds:
            parts.append(f"content={','.join(kinds)}")
        summary.append(f"{record.get('timestamp', '-')} {' '.join(parts)}")
        if len(summary) >= limit:
            break
    if not summary:
        return "<unparsable>"
    return "\n".join(reversed(summary))


# One evidence segment's cap (Issue #1711, `fenced`'s default) and one
# fitting step: an oversized comment shrinks these, never its structure.
EVIDENCE_SEGMENT_CAP = 4000
_EVIDENCE_CAP_STEP = 500


def _failure_evidence(worktree: Path | None, exc: BaseException, *,
                      language_code: str,
                      cap: int = EVIDENCE_SEGMENT_CAP) -> str:
    """Render the bounded evidence that survives terminal worktree cleanup.

    Pi subprocess streams come from ``CalledProcessError``. The session and
    test log are read before cleanup and only bounded, fenced segments are
    copied into the failure comment: raw output stays literal on GitHub,
    the session tail is a structural summary whose duplicates are removed,
    and the full session log is named for the deep-dive. The
    failure reason itself stays the readable head of the comment.

    `cap` bounds each segment, so every fence closes (Issue #1711).
    """
    stderr = getattr(exc, "stderr", None)
    stdout = getattr(exc, "output", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    stderr = str(stderr) if stderr else "<empty>"
    stdout = str(stdout) if stdout else "<empty>"
    return_code = getattr(exc, "returncode", None)
    session_file = _latest_session_file(worktree)
    session = (
        _session_summary(session_file) if session_file else "<unavailable>"
    )
    session_label = (
        language.text(language_code, "failure.scene.local_session_log")
        if session_file else "<unavailable>"
    )
    test_log = "<unavailable>"
    if worktree is not None:
        test_path = worktree / ".orbi" / "test.log"
        if test_path.is_file():
            test_log = _tail_text(test_path)
    return (
        f"\n\n{language.text(language_code, 'failure.evidence.heading')}\n"
        f"exit_code={return_code if return_code is not None else '<unknown>'}\n"
        f"\nstderr_tail:\n{fenced(redact_local_paths(stderr), cap=cap)}\n"
        f"\nstdout_tail:\n{fenced(redact_local_paths(stdout), cap=cap)}\n"
        f"\n{language.text(language_code, 'failure.evidence.session_last_events', limit=SESSION_SUMMARY_LIMIT, label=session_label)}\n"
        f"{fenced(redact_local_paths(session), cap=cap)}\n"
        f"\ntest_log_tail:\n{fenced(redact_local_paths(test_log), cap=cap)}"
    )


# The failure comment's size limit: the body stays under it by shrinking
# only the evidence segments, never by cutting the comment (Issue #1711).
FAILURE_COMMENT_MAX_CHARS = 20000


def fit_to_limit(body: str, worktree: Path | None, exc: BaseException, *,
                 evidence: str, tail: str, language_code: str) -> str:
    """Shrink ONE rendered comment's evidence segments until it fits.

    An oversized comment shrinks ONLY its evidence segments, never its
    structure: a cut lands inside its own fence with its omission note, so
    the closer and the help line render as text (Issue #1711). `evidence`
    is the segment text the body already carries and `tail` the text
    appended after the body (the blocked exit's community help line) — both
    are inside the same budget, and the help line is never cut.
    """
    cap = EVIDENCE_SEGMENT_CAP
    while (evidence and cap
           and len(body) + len(tail) > FAILURE_COMMENT_MAX_CHARS):
        cap = max(0, cap - _EVIDENCE_CAP_STEP)
        shrunk = _failure_evidence(
            worktree, exc, language_code=language_code, cap=cap)
        body = body.replace(evidence.lstrip(), shrunk.lstrip(), 1)
        evidence = shrunk
    return body
