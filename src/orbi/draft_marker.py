#!/usr/bin/env python3
"""The hidden marker a drafted Issue carries (Issue #2041).

`orbi draft` judges every Issue it drafts with the pre-work check BEFORE a
person sees it, and this marker is what makes that judgment stand at claim
time: it names the caller's draft key, the Issue's 1-based position in that
draft and the sha256 of the body AS CREATED. "Nobody edited this body
since the check" is therefore a hash comparison, never a guess — an edited
body hashes differently and is judged exactly as before.

The module is deliberately tiny and dependency-light: `clarify.enforce`
reads the marker at every claim and `orbi.draft` writes it when it creates
an Issue, and neither may import the other. The author rule is the split
child's own rule (only an Issue THIS runner authored counts): the marker is
public text on a public Issue, so anyone could paste one.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from orbi.github import authenticated_github_login, strip_bot_suffix
from orbi.journal import event

DRAFT_MARKER = "orbi:draft:v1"
_DRAFT_MARKER_RE = re.compile(
    r"<!-- " + re.escape(DRAFT_MARKER)
    + r":(?P<key>[A-Za-z0-9_-]{1,64}):(?P<position>[1-9][0-9]*)"
    + r":(?P<fingerprint>[0-9a-f]{64}) -->"
)
# GitHub's view API reports an App's Issue author as "app/<login>" while the
# same credential's comments and auth status say "<login>"/"<login>[bot]"
# (the normalization `ticket_command.login_key` owns; repeated here because
# that module imports `clarify`, which imports this one).
_API_LOGIN_PREFIX = "app/"


@dataclass(frozen=True)
class DraftMark:
    """One parsed draft marker: the draft key, position and fingerprint."""

    key: str
    position: int
    fingerprint: str


def draft_body_text(body: object) -> str:
    """The body with every draft marker line removed.

    The marker carries its body's fingerprint, so it must be out of the
    hashed text or the digest would depend on itself.
    """
    text = body if isinstance(body, str) else ""
    return "\n".join(
        line for line in text.splitlines()
        if _DRAFT_MARKER_RE.search(line) is None
    ).strip()


def draft_fingerprint(body: object) -> str:
    """The fingerprint of one body as created (its draft marker excluded)."""
    return hashlib.sha256(
        draft_body_text(body).encode("utf-8")
    ).hexdigest()


def draft_marker(key: str, position: int, body: object) -> str:
    """The hidden marker line one drafted Issue ends with."""
    return (
        f"<!-- {DRAFT_MARKER}:{key}:{position}"
        f":{draft_fingerprint(body)} -->"
    )


def with_marker(body: str, *, key: str, position: int) -> str:
    """One created body: the text as drafted, then its hidden marker."""
    text = (body or "").strip()
    return f"{text}\n\n{draft_marker(key, position, text)}"


def parse_draft_marker(body: object) -> DraftMark | None:
    """The newest draft marker in one body, or None when it carries none."""
    if not isinstance(body, str):
        return None
    matches = list(_DRAFT_MARKER_RE.finditer(body))
    if not matches:
        return None
    match = matches[-1]
    return DraftMark(
        key=match.group("key"), position=int(match.group("position")),
        fingerprint=match.group("fingerprint"),
    )


def _account_key(login: object) -> str | None:
    """The comparable login of one account, or None when unreadable."""
    if not isinstance(login, str) or not login:
        return None
    if login.startswith(_API_LOGIN_PREFIX):
        login = login[len(_API_LOGIN_PREFIX):]
    return strip_bot_suffix(login)


def own_draft_unchanged(issue: dict) -> bool:
    """True when this runner drafted the body and nobody edited it.

    Every failure — no marker, an edited body, an unreadable login, a
    marker on another account's Issue — returns False, so the gate judges
    the ticket exactly as it did before Issue #2041. The check never fails
    OPEN into delivering an unjudged body; it fails toward the judgment.
    """
    mark = parse_draft_marker(issue.get("body"))
    if mark is None:
        return False
    if mark.fingerprint != draft_fingerprint(issue.get("body")):
        return False
    try:
        own = _account_key(authenticated_github_login())
    except Exception as exc:
        event(
            "clarify_draft_untrusted", level=logging.WARNING,
            issue=issue.get("number"), reason=type(exc).__name__,
        )
        return False
    if own is None:
        return False
    author = issue.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if _account_key(login) != own:
        event(
            "clarify_draft_untrusted", level=logging.WARNING,
            issue=issue.get("number"),
        )
        return False
    return True
