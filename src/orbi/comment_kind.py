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

Every versioned record block (`orbi:failure:v1`, `orbi:scene:v1`,
`orbi:base-ci-red:v1`) rides the same envelope: at most one block per
comment body, a JSON object payload, no unknown field and no missing
required field. `parse_versioned_block` owns those rules here, and each
domain keeps its own exception type, field types and validators.

The module is pure (the `scene` model): every function is a
deterministic function of its input string, with no I/O, so the writer
and every reader share one definition.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

# The run-id format belongs to `journal.validate_run_id` (Issue #2003):
# the anchored prefix below interpolates that pattern instead of spelling
# the id out, so a reader can never accept an id the writer refuses.
from orbi.journal import RUN_ID_PATTERN

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
    r"\A<!-- orbi:run=" + RUN_ID_PATTERN.pattern
    + r" -->\r?\n<!-- orbi:comment=([a-z-]+) -->"
)


def comment_kind_marker(kind: str) -> str:
    """Render the hidden canonical kind marker for one comment kind."""
    if kind not in COMMENT_KINDS:
        raise ValueError(f"unknown comment kind: {kind!r}")
    return COMMENT_KIND_TEMPLATE.format(kind=kind)


def versioned_block_pattern(marker: str) -> re.Pattern[str]:
    """The one hidden block pattern for a versioned comment marker.

    The capture is non-greedy to the first `-->`, so a truncated or
    mangled payload still matches the marker and fails as corrupted
    instead of being silently ignored.
    """
    return re.compile(
        r"<!--\s*" + re.escape(marker) + r"\s+(.*?)-->", re.DOTALL,
    )


def parse_versioned_block(
    body: object,
    marker: str,
    fields: Sequence[str],
    error: Callable[[str], Exception],
    *,
    required: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Decode the one versioned block of `marker` from a comment body.

    `fields` is the record's complete field set and `required` the
    subset a block must carry (default: all of them). `error` builds the
    caller's own exception, so every record keeps its exception type and
    the messages stay identical across them.

    Returns the decoded object, or `None` when the body carries no block
    at all (an old or other-kind comment). Raises through `error` when a
    block is present but corrupted: more than one block, invalid JSON, a
    non-object payload, an unknown field or a missing required one.
    """
    if not isinstance(body, str):
        return None
    blocks = versioned_block_pattern(marker).findall(body)
    if len(blocks) > 1:
        raise error(f"multiple {marker} blocks in one comment body")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise error(f"{marker} block is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise error(f"{marker} block payload must be a JSON object")
    unknown = sorted(set(data) - set(fields))
    if unknown:
        raise error(f"{marker} block has unknown fields: {unknown}")
    missing = sorted(
        name for name in (fields if required is None else required)
        if name not in data
    )
    if missing:
        raise error(f"{marker} block is missing fields: {missing}")
    return data


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
