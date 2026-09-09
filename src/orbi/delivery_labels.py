"""Delivery label lifecycle and scheduling policy (Issue #175).

The single source of truth for the delivery label states, the event →
label patch transition rules, and the pickup/resume/human-intervention
decisions. GitHub Issues/labels are the only state store; the GitHub
adapter (`orbi.runner.edit_issue`) reads and applies the patches.

The module is pure: every function is a deterministic function of its
inputs, with no I/O. The same current labels and event always produce
the same idempotent label patch.
"""

# --- Delivery lifecycle states (the only state store is GitHub labels) ---
READY_LABEL = "ai-ready"
IN_PROGRESS_LABEL = "ai-in-progress"
PR_OPENED_LABEL = "ai-pr-opened"
FIX_NEEDED_LABEL = "ai-fix-needed"
MERGED_LABEL = "ai-merged"
BLOCKED_LABEL = "ai-blocked"

LIFECYCLE_STATES = frozenset({
    READY_LABEL, IN_PROGRESS_LABEL, PR_OPENED_LABEL,
    FIX_NEEDED_LABEL, MERGED_LABEL, BLOCKED_LABEL,
})

# --- Scheduling metadata (NOT delivery lifecycle states) ---
# `p0` and `bug` only order the ready pickup; `ai-epic` marks a
# coordination Issue the claim scan never touches; `ai-release` routes
# to the deterministic release state machine; `ai-ticket-only` is a
# content marker. None of them is a delivery state, and `blockedBy`
# (a GitHub relation, not a label) is handled by the dependency scan.
P0_LABEL = "p0"
BUG_LABEL = "bug"
EPIC_LABEL = "ai-epic"
RELEASE_LABEL = "ai-release"
TICKET_ONLY_LABEL = "ai-ticket-only"

# --- Events that drive label transitions ---
EVENT_CLAIM = "claim"
EVENT_PR_OPENED = "pr_opened"
EVENT_FIX_NEEDED = "fix_needed"
EVENT_MERGED = "merged"
EVENT_RELEASE_WAITING = "release_waiting"
EVENT_REQUEUE = "requeue"
EVENT_BLOCKED = "blocked"

# The delivery-state labels a terminal failure must clear so the
# terminal state is `ai-blocked` ALONE (never `ai-pr-opened` +
# `ai-blocked`, never `ai-in-progress` + `ai-blocked`, ...).
_DELIVERY_STATE_LABELS = (
    IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL,
)


def label_patch(event: str, current_labels) -> tuple[list[str], list[str]]:
    """Return the (to_add, to_remove) label patch for one event.

    A pure function of the event and the current labels: the same
    inputs always produce the same deterministic, idempotent patch.

    - claim: add `ai-in-progress`, remove a stale `ai-fix-needed` from
      a resumed fix round (the `ai-ready` residue is kept).
    - pr_opened: add `ai-pr-opened`, remove `ai-in-progress`.
    - fix_needed: add `ai-fix-needed`, remove any stale opened-PR or
      in-flight label.
    - merged: add `ai-merged`, remove the current delivery-state label
      (the one present in `current_labels`): `ai-pr-opened` for the
      normal PR delivery, `ai-in-progress` for the release state
      machine (which never enters the PR states).
    - blocked: add `ai-blocked`, remove every delivery-state label that
      is present (so the terminal state is `ai-blocked` ALONE).

    An unknown event raises `ValueError` (fail fast — never a guessed
    patch).
    """
    current = set(current_labels)
    if event == EVENT_CLAIM:
        to_remove = [
            FIX_NEEDED_LABEL,
        ] if FIX_NEEDED_LABEL in current else []
        return ([IN_PROGRESS_LABEL], to_remove)
    if event == EVENT_PR_OPENED:
        return ([PR_OPENED_LABEL], [IN_PROGRESS_LABEL])
    if event == EVENT_FIX_NEEDED:
        to_remove = [
            label for label in (IN_PROGRESS_LABEL, PR_OPENED_LABEL)
            if label in current
        ]
        return ([FIX_NEEDED_LABEL], to_remove)
    if event == EVENT_RELEASE_WAITING:
        return ([READY_LABEL], [IN_PROGRESS_LABEL])
    if event == EVENT_REQUEUE:
        # Issue #608: the external takeover delivery ended without a merge
        # (the contributor withdrew the PR, or a maintainer closed it) —
        # the Issue returns to the ready queue and the next claim redoes
        # the fix internally. Every delivery-state label is cleared so the
        # Issue is `ai-ready` alone.
        to_remove = [
            label for label in _DELIVERY_STATE_LABELS if label in current
        ]
        return ([READY_LABEL], to_remove)
    if event == EVENT_MERGED:
        to_remove = [
            label for label in _DELIVERY_STATE_LABELS if label in current
        ]
        return ([MERGED_LABEL], to_remove)
    if event == EVENT_BLOCKED:
        to_remove = [
            label for label in _DELIVERY_STATE_LABELS if label in current
        ]
        return ([BLOCKED_LABEL], to_remove)
    raise ValueError(f"unknown delivery event: {event!r}")


def is_pickup_eligible(current_labels) -> bool:
    """True when the Issue is fresh-ready: `ai-ready` and not yet in any
    OTHER delivery state (the ready scan's code-layer guard, Issue
    #93/#101).

    `ai-ready` itself is the queue entry, not a delivery state — it is
    excluded from the check. `p0`/`bug` are scheduling metadata, not
    lifecycle states — they never affect eligibility (they only order the
    pickup).
    """
    current = set(current_labels)
    if READY_LABEL not in current:
        return False
    other_states = LIFECYCLE_STATES - {READY_LABEL}
    return not (current & other_states)


def is_resumable(current_labels) -> bool:
    """True when the Issue is in an opened-PR state awaiting review or the
    next review session (`ai-pr-opened` or `ai-fix-needed`, Issue #70/#82).

    `ai-in-progress` alone is NOT resumable here: an implement-phase Issue
    has neither opened-PR label, so it never matches (the in-flight restart
    scan handles it separately).
    """
    current = set(current_labels)
    return PR_OPENED_LABEL in current or FIX_NEEDED_LABEL in current


def needs_human_intervention(current_labels) -> bool:
    """True when the Issue is terminally blocked (`ai-blocked`): it needs a
    human decision before any automatic resume (Issue #50)."""
    return BLOCKED_LABEL in set(current_labels)
