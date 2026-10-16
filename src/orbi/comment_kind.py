"""Canonical, display-independent comment kinds (Issue #1659).

Every run-scoped comment starts with the hidden run marker
(`<!-- orbi:run=<run_id> -->`). The four KINDED comments carry one more
hidden marker on the line right after it:

    <!-- orbi:comment=started -->
    <!-- orbi:comment=failure -->
    <!-- orbi:comment=review-round -->
    <!-- orbi:comment=merged -->

so a consumer identifies the comment without parsing a translatable
heading. The kind is read only from that anchored prefix; a
marker-shaped string quoted in prose, a raw log or a code fence is not
official metadata. Progress comments stay deliberately kindless: their
documented formal prefix (the run marker followed immediately by the
`orbi:progress:v1` block) identifies them.

The module is pure (the `scene` model): every function is a
deterministic function of its input string, with no I/O, so the writer
and every reader share one definition.
"""
from __future__ import annotations

import re

COMMENT_KIND_STARTED = "started"
COMMENT_KIND_FAILURE = "failure"
COMMENT_KIND_REVIEW_ROUND = "review-round"
COMMENT_KIND_MERGED = "merged"
COMMENT_KINDS = (
    COMMENT_KIND_STARTED, COMMENT_KIND_FAILURE,
    COMMENT_KIND_REVIEW_ROUND, COMMENT_KIND_MERGED,
)
COMMENT_KIND_TEMPLATE = "<!-- orbi:comment={kind} -->"
COMMENT_KIND_PATTERN = re.compile(r"<!-- orbi:comment=([a-z-]+) -->")

# The official metadata prefix, anchored at the body's start: the run
# marker on the first line and, on a kinded comment, the kind marker on
# the line immediately after. Anchoring means a marker-shaped string
# quoted in prose, a raw log or a code fence is never read as metadata.
_KIND_PREFIX_RE = re.compile(
    r"\A<!-- orbi:run=[0-9a-f]{8} -->\r?\n<!-- orbi:comment=([a-z-]+) -->"
)


def comment_kind_marker(kind: str) -> str:
    """Render the hidden canonical kind marker for one comment kind."""
    if kind not in COMMENT_KINDS:
        raise ValueError(f"unknown comment kind: {kind!r}")
    return COMMENT_KIND_TEMPLATE.format(kind=kind)


def comment_kind(body: object) -> str | None:
    """The canonical kind from the body's official metadata prefix.

    None means the body carries no kind marker in that prefix: a
    historical comment (read through the existing English fallback) or a
    kindless comment (progress, opened PR, short milestone). The kind is
    read only from the anchored prefix, so a marker-shaped string inside
    a raw log, a details fold or a code fence is never official
    metadata. An unsupported kind is returned verbatim, never
    reinterpreted through the prose next to it.
    """
    if not isinstance(body, str):
        return None
    match = _KIND_PREFIX_RE.match(body)
    return match.group(1) if match is not None else None
