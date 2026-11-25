#!/usr/bin/env python3
"""The stop record the clarification gate leaves on a ticket (Issue #1904).

A stop is recorded ONCE per ticket text: the stop comment carries a hidden
digest of the text the gate judged, and a re-readied ticket whose text still
hashes to the newest stop is not judged, commented or notified again — it is
simply stopped again (the claim label is removed, so it leaves the queue).
Editing the ticket changes the digest and the next claim judges it again.

Because that marker decides a claim, it is read only from the formal prefix
of THIS runner's own comments: the digest is derived from public ticket
text, so anyone could compute a forged one, a hidden marker never
authenticates its author, and marker-shaped text quoted in prose, a raw log
or a code fence is not official metadata (docs/public-contracts.mdx).

The module is a leaf of `clarify` (which imports it) so the gate keeps
room for its judgment and rendering: it owns the marker, the digest and the
one locator of the authoritative stop, nothing else.
"""
from __future__ import annotations

import hashlib
import logging
import re

from orbi import issue_material
from orbi.github import (
    authenticated_github_login, issue_comments, strip_bot_suffix,
)
from orbi.journal import event
from orbi.progress import RUN_MARKER_PATTERN

# The stop comment carries the digest of the exact text the gate judged
# (Issue #1904). A re-readied ticket whose text still hashes to the newest
# stop's digest is not judged again: the comment and notification already
# on the Issue cover it. Hidden because it is machine state, never prose.
CLARIFY_STOP_MARKER = "orbi:clarify-stop"
# The marker counts only in the formal prefix `render_comment()` writes
# (Issue #1908): the optional `@author` mention line, then the comment's
# run marker and then the stop marker. Marker-shaped text quoted deeper in
# a comment — prose, a raw log or a code fence such as a failure comment's
# `stdout_tail` — is not official metadata (docs/public-contracts.mdx),
# and honoring it would stop a ticket the gate never rejected.
_CLARIFY_STOP_RE = re.compile(
    r"\A(?:@[^\s\n]+\n)?"
    + RUN_MARKER_PATTERN.pattern
    + r"\n<!-- " + re.escape(CLARIFY_STOP_MARKER)
    + r":(?P<digest>[0-9a-f]{64}) -->"
)


def build_context(issue: dict) -> str:
    """Return the judgment payload: the one shared ticket text renderer."""
    return issue_material.text(issue)


def judged_digest(issue: dict) -> str:
    """The digest of the exact text one judgment reads (Issue #1904)."""
    return hashlib.sha256(build_context(issue).encode("utf-8")).hexdigest()


def stop_marker(issue: dict) -> str:
    """The hidden marker a stop comment carries for its judged text."""
    return f"<!-- {CLARIFY_STOP_MARKER}:{judged_digest(issue)} -->"


def _runner_login(issue: dict) -> str | None:
    """The login this runner posts with, or None when it cannot be read.

    The stop marker steers a claim, so it is only accepted from this
    runner's own comments: the digest is computed from public ticket text,
    so any commenter could forge one, and a hidden marker never
    authenticates its author (docs/public-contracts.mdx). The identity
    read fails OPEN — an unreadable login means the gate judges the
    ticket exactly as before Issue #1904.
    """
    try:
        return strip_bot_suffix(authenticated_github_login())
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return None


def authoritative_stop(
    issue: dict, source_repo: str, *, comments: list[dict] | None = None,
) -> dict | None:
    """The newest stop comment THIS runner wrote, or None.

    The one locator of the authoritative stop (Issue #1926), so the digest
    check and every reader of the proposal select the same comment.

    Only the formal prefix counts (Issue #1908): the optional author
    mention line, the run marker and then the stop marker. Marker text
    quoted anywhere else — including a failure comment's fenced stdout —
    is not stop state, so a ticket that was never stopped keeps its claim.

    A marker on somebody else's comment is ignored (and journaled): it is
    a forged or copied marker, never a stop this engine recorded. The
    read fails open: an unreadable comment history means the gate judges
    the ticket exactly as it did before Issue #1904, so a broken read
    never loses a delivery.

    Selection is comment creation order (oldest first), never the edit
    time: a stop keeps its position when a person edits it. The newest
    trusted stop wins even when it carries no proposal, and an older
    stop's proposal is never reused.

    ``comments`` lets a caller that already read the ticket's comments
    (the command dispatcher) reuse that read instead of paying a second
    one; the selection itself is unchanged.
    """
    try:
        if comments is None:
            comments = issue_comments(issue["number"], repo=source_repo)
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return None
    own: str | None = None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _CLARIFY_STOP_RE.match(body) is None:
            continue
        if own is None:
            own = _runner_login(issue)
            if own is None:
                return None
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(login, str) or strip_bot_suffix(login) != own:
            event(
                "clarify_stop_untrusted", level=logging.WARNING,
                issue=issue["number"],
            )
            continue
        return comment
    return None


def last_judged_digest(issue: dict, source_repo: str) -> str | None:
    """Return the digest of the newest stop by THIS runner, or None."""
    comment = authoritative_stop(issue, source_repo)
    if comment is None:
        return None
    match = _CLARIFY_STOP_RE.match(comment["body"])
    return match.group("digest") if match is not None else None


def already_stopped(issue: dict, source_repo: str) -> bool:
    """True when the newest stop judged exactly this ticket text.

    Compares against `build_context`, the text the model reads: a title
    or body change is a new judgment, an unchanged ticket is not.
    """
    previous = last_judged_digest(issue, source_repo)
    return previous is not None and previous == judged_digest(issue)
