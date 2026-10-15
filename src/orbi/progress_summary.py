"""The versioned progress summary value (Issue #1657).

The single safe value the progress comment renders twice: as the
readable status lines and as the hidden orbi:progress:v1 block.
The documented contract (docs/public-contracts.mdx) owns the format:
step is a Runner-confirmed stage or null, reported_at is the report
generation time (a heartbeat advances it without advancing activity.at),
and activity is null or {at, kind, target} with a whitelisted kind.
Prose and block share this check; nothing absolute, outside the
worktree, Runner-owned or secret can be published.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from orbi.pi_activity import (
    ACTIVITY_KIND_BY_TOOL,
    RUNNER_RUNTIME_EXCLUDES,
    parse_iso_utc,
)

# The versioned machine-readable summary block: the run marker followed
# immediately by this block start the progress comment body, including at
# finish (Issue #1657).
PROGRESS_BLOCK_VERSION = "orbi:progress:v1"
PROGRESS_BLOCK_TEMPLATE = f"<!-- {PROGRESS_BLOCK_VERSION} {{payload}} -->"

# The documented stage vocabulary (shared with the status observation
# contract). The writer publishes only what its bound Runner action
# confirms: STEP_BY_ROLE below, or the explicit stage of a Runner
# action that entered it (checks / merge). `starting` belongs to the
# shared vocabulary without a bound role of its own, so no delivery
# publishes it as its stage.
PROGRESS_STEPS = (
    "starting",
    "implement",
    "review",
    "checks",
    "merge",
    "release",
)

# The stages the Runner's bound role confirms. A task type without a
# stage of its own (ticket — content/ops/ticket-only) is deliberately
# absent: its step stays null, never a fictitious PR lifecycle.
STEP_BY_ROLE = {
    "implement": "implement",
    "review": "review",
    "release": "release",
}

# The documented activity kinds: the tool whitelist's kinds plus the
# assistant's own text/thinking ("analysis"). An unknown tool publishes
# no activity at all.
ACTIVITY_KINDS = frozenset(ACTIVITY_KIND_BY_TOOL.values()) | {"analysis"}

# A published target is never longer than a plausible repository path;
# the cap keeps a hostile argument from bloating the comment.
_MAX_TARGET_LENGTH = 500


def _format_utc(moment: datetime) -> str:
    """One UTC ISO-8601 timestamp (seconds precision, the documented form)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """The report generation time: UTC ISO-8601, never null."""
    return _format_utc(datetime.now(timezone.utc))


def utc_timestamp(value: object) -> str | None:
    """Normalize one session event timestamp to UTC ISO-8601.

    An unparseable value is unknown (None): the summary never publishes
    a timestamp it cannot attribute to an event.
    """
    if not isinstance(value, str) or not value:
        return None
    epoch = parse_iso_utc(value)
    if epoch is None:
        return None
    return _format_utc(datetime.fromtimestamp(epoch, timezone.utc))


def _is_runtime_relative_path(relative: str) -> bool:
    """True for a repository-relative Runner-owned runtime path.

    The same shared list `pi_session` pins in the worktree exclude and
    the dirty-worktree gate reads: the runtime directories and the three
    root run artifacts, plus `.git` metadata.
    """
    if relative == ".git" or relative.startswith(".git/"):
        return True
    for pattern in RUNNER_RUNTIME_EXCLUDES:
        if pattern.endswith("/"):
            if relative.startswith(pattern):
                return True
        elif relative == pattern:
            return True
    return False


def safe_target(worktree: Path, raw: object) -> str | None:
    """The repository-relative target a published read/edit may carry.

    The target must normalize to a repository-relative path inside the
    worktree. An absolute path inside the worktree is published as its
    repository-relative form — nothing absolute is ever published; an
    escaping, outside, unattributable (or non-string) target, `.git`
    metadata, a Runner-owned runtime path and a target carrying an HTML
    comment character all become None. Prose and the hidden block share
    this check: the block escapes those characters while it serializes,
    the readable prose publishes the same value verbatim, so the value
    itself must be safe for BOTH representations.
    """
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    try:
        relative = candidate.resolve().relative_to(worktree.resolve())
    except (ValueError, OSError):
        return None
    text = relative.as_posix()
    if not text or text == "." or len(text) > _MAX_TARGET_LENGTH:
        return None
    # The block escapes `<`/`>` in its serialization; the readable prose
    # publishes the value verbatim, where `<!--` would swallow the rest of
    # the comment and `-->` would terminate it early. A target carrying
    # either character is therefore dropped in both representations. The
    # control characters go for the same reason (one value, one line).
    if any(character in text for character in "\r\n\x00<>"):
        return None
    if _is_runtime_relative_path(text):
        return None
    return text


def safe_activity(worktree: Path, activity: dict | None) -> dict | None:
    """The single safe activity value both representations render.

    `kind` comes from the publication whitelist (an unknown tool
    publishes no activity at all); `target` is a validated
    repository-relative path and is null for command/analysis and for an
    unsafe read/edit target; `at` is the newest event time or null when
    unknown. No known activity is `None`, never an empty object.
    """
    if not isinstance(activity, dict):
        return None
    kind = activity.get("kind")
    if kind not in ACTIVITY_KINDS:
        return None
    target = None
    if kind in ("read", "edit"):
        target = safe_target(worktree, activity.get("target"))
    return {
        "at": utc_timestamp(activity.get("last_activity")),
        "kind": kind,
        "target": target,
    }


def serialize_summary(summary: dict) -> str:
    """Serialize the summary for the hidden block.

    `<` and `>` are escaped with the JSON `\u003c` / `\u003e` escapes:
    a reader decodes the exact same value, while no value — a path in
    particular — can terminate the HTML comment or open a nested one.
    """
    payload = json.dumps(summary, separators=(",", ":"), ensure_ascii=True)
    return payload.replace("<", "\\u003c").replace(">", "\\u003e")


def progress_block(summary: dict) -> str:
    """The versioned machine-readable block, one line, marker-form."""
    return PROGRESS_BLOCK_TEMPLATE.format(payload=serialize_summary(summary))


def step_for(state: dict) -> str | None:
    """The documented stage the Runner's bound action confirms.

    An explicit `step` (a Runner action that entered checks/merge) wins;
    otherwise the bound role maps through STEP_BY_ROLE (a task type
    without a stage of its own stays null); anything outside the
    documented vocabulary is null.
    """
    step = state.get("step")
    if step is None:
        step = STEP_BY_ROLE.get(state.get("role"))
    return step if step in PROGRESS_STEPS else None


def progress_summary(state: dict) -> dict:
    """The one safe summary both the prose and the block render.

    `reported_at` is generated here: a heartbeat advances it without
    advancing `activity.at`.
    """
    activity = state.get("activity")
    return {
        "step": step_for(state),
        "reported_at": utc_now_iso(),
        "activity": activity if isinstance(activity, dict) else None,
    }


def activity_text(activity: dict | None) -> str:
    """The readable activity: `kind target`, the kind alone, or `-`."""
    if activity is None:
        return "-"
    target = activity.get("target")
    return f"{activity['kind']} {target}" if target else activity["kind"]
