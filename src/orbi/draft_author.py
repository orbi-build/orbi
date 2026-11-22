#!/usr/bin/env python3
"""Which Issue this runner itself drafted and nobody edited (Issues #2041, #2364).

The draft marker is public text on a public Issue, so anyone could paste one:
only an Issue THIS runner authored, whose body still matches its marker's
fingerprint, counts as a body the claim-time pre-work check already judged.

The author rule lives here rather than beside the marker itself
(`orbi.draft_marker`) so that module stays one file about the marker text and
its fingerprint, and this one about the account — the same split-child rule
`orbi.split_apply` reads, with the API's `app/<login>` prefix normalized.
"""
from __future__ import annotations

import logging

from orbi import draft_marker
from orbi.github import authenticated_github_login, strip_bot_suffix
from orbi.journal import event

# GitHub's view API reports an App's Issue author as "app/<login>" while the
# same credential's comments and auth status say "<login>"/"<login>[bot]"
# (the normalization `ticket_command.login_key` owns; repeated here because
# that module imports `clarify`, which imports this one).
_API_LOGIN_PREFIX = "app/"


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
    mark = draft_marker.parse_draft_marker(issue.get("body"))
    if mark is None:
        return False
    if mark.fingerprint != draft_marker.draft_fingerprint(issue.get("body")):
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
