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
an Issue, and neither may import the other. Who authored the Issue is
`orbi.draft_author`'s rule, and the marker is public text, so anyone could
paste one.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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
    """The drafted text: everything BEFORE the newest draft marker line.

    The fingerprint covers the body as the drafter wrote it, so it has to end
    where the marker starts: `orbi draft` appends the Attachments section
    AFTER the marker (Issue #2364) and a gh upload rewrites the image
    addresses inside that section, so counting it would re-check a submitted
    draft at claim. A body without a marker hashes whole, as before.
    """
    text = body if isinstance(body, str) else ""
    lines = text.splitlines()
    markers = [
        index for index, line in enumerate(lines)
        if _DRAFT_MARKER_RE.search(line) is not None
    ]
    return "\n".join(lines if not markers else lines[:markers[-1]]).strip()


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


def with_marker(
    body: str, *, key: str, position: int, section: str = "",
) -> str:
    """One created body: the text as drafted, its hidden marker, then the
    Attachments section a creation writes below it (Issue #2364).

    The marker sits between the drafted text and the section, so the
    fingerprint covers the drafted text alone: what a creation appends below
    the marker — and what gh rewrites there — never changes it.
    """
    text = (body or "").strip()
    created = f"{text}\n\n{draft_marker(key, position, text)}"
    return f"{created}\n\n{section}" if section else created


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
