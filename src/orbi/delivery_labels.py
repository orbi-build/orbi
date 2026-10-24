"""Delivery label lifecycle and scheduling policy.

The single source of truth for the delivery label states, the event →
label patch transition rules, and the pickup/resume/human-intervention
decisions. GitHub Issues/labels are the only state store; the GitHub
adapter (`orbi.github.edit_issue`) reads and applies the patches.

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
AWAITING_MERGE_LABEL = "ai-awaiting-merge"

LIFECYCLE_STATES = frozenset({
    READY_LABEL, IN_PROGRESS_LABEL, PR_OPENED_LABEL,
    FIX_NEEDED_LABEL, MERGED_LABEL, BLOCKED_LABEL,
    AWAITING_MERGE_LABEL,
})

# --- Scheduling metadata (NOT delivery lifecycle states) ---
# `p0` and `bug` only order the ready pickup; `ai-epic` marks a
# coordination Issue the claim scan never touches; `ai-release` routes
# to the deterministic release state machine; `ai-ops-only` routes to
# the full-execution ops session (shell/gh/network like a
# dev ticket, the ops playbook instead of the dev one, the deliverable
# is evidence posted to the Issue); `ai-content-only` marks a pure
# content task (the content agent, no execution, the deliverable is
# posted to the Issue); `ai-human-review` is the human acceptance gate
# — the Runner never adds or removes it (the `ai-release`
# shape: no event's patch names it in either direction);
# `ai-needs-detail` is the thin-ticket clarification gate's terminal
# state (Issue #1088) — the delivery is stopped and the Issue waits for
# a human to add the missing pieces, exactly like `ai-blocked`. None of
# them is a delivery state, and `blockedBy` (a GitHub relation, not a
# label) is handled by the dependency scan.
P0_LABEL = "p0"
BUG_LABEL = "bug"
EPIC_LABEL = "ai-epic"
RELEASE_LABEL = "ai-release"
CONTENT_ONLY_LABEL = "ai-content-only"
OPS_LABEL = "ai-ops-only"
HUMAN_REVIEW_LABEL = "ai-human-review"
NEEDS_DETAIL_LABEL = "ai-needs-detail"

# --- Reading an Issue's label names ---

def label_names(issue: dict) -> list[str]:
    """The Issue's label names, in the order the Issue carries them.

    The one label-name parser (Issue #1853): every Issue-dict call site
    reads its labels through it, so the label-payload rule lives in one
    place. The scans fetch `labels` as an array of `{name, ...}` nodes.
    A missing or malformed `labels` field — and any entry that is not a
    named label — yields no label: a shape the payload cannot have is
    an unlabelled Issue, never a crash.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    return [
        label["name"] for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    ]


def label_name_set(issue: dict) -> frozenset[str]:
    """The Issue's label names as a set (the label-state comparisons)."""
    return frozenset(label_names(issue))


# --- Events that drive label transitions ---
EVENT_CLAIM = "claim"
EVENT_PR_OPENED = "pr_opened"
EVENT_FIX_NEEDED = "fix_needed"
EVENT_MERGED = "merged"
EVENT_RELEASE_WAITING = "release_waiting"
EVENT_HUMAN_REVIEW_WAITING = "human_review_waiting"
EVENT_REQUEUE = "requeue"
EVENT_BLOCKED = "blocked"
EVENT_AWAITING_MERGE = "awaiting_merge"

# The delivery-state labels a terminal failure must clear so the
# terminal state is `ai-blocked` ALONE (never `ai-pr-opened` +
# `ai-blocked`, never `ai-in-progress` + `ai-blocked`, ...).
_DELIVERY_STATE_LABELS = (
    IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL,
)


def label_patch(event: str, current_labels,
                ready_label: str = READY_LABEL) -> tuple[list[str], list[str]]:
    """Return the (to_add, to_remove) label patch for one event.

    A pure function of the event and the current labels: the same
    inputs always produce the same deterministic, idempotent patch.
    `ready_label` is the repository's RESOLVED queue entry — the same
    label the claim scanned (`dispatch_label`, default `ai-ready`) —
    so a custom-label repository's blocked/merged/waiting terminal never
    keeps a queue-label residue (Issue #1775).

    - claim: add `ai-in-progress`, remove a stale `ai-fix-needed` from
      a resumed fix round and a stale `ai-needs-detail` left by the
      thin-ticket gate once the author re-readied the ticket (the
      `ai-ready` residue is kept).
    - pr_opened: add `ai-pr-opened`, remove `ai-in-progress`.
    - fix_needed: add `ai-fix-needed`, remove any stale opened-PR or
      in-flight label. A TERMINAL Issue (`ai-merged` or `ai-blocked`
      present) changes nothing: the delivery is over or waiting for a
      human, and a fix-needed label beside it would let a later terminal
      label removal resume an automatic fix nobody asked for (Issue
      #1786). The engine never sends this event to a terminal Issue; the
      CI triage does, from a late run on an already-merged PR.
    - merged: add `ai-merged`, remove the current delivery-state label
      (the one present in `current_labels`): `ai-pr-opened` for the
      normal PR delivery, `ai-in-progress` for the release state
      machine (which never enters the PR states). The resolved queue
      label (`ready_label`, default `ai-ready`) is cleared too: it is
      the queue entry, not a delivery state, so a merged Issue must not
      keep showing it (Issue #1526) — the terminal state is
      `ai-merged` ALONE.
    - blocked: add `ai-blocked`, remove every delivery-state label that
      is present AND the resolved queue label (`ready_label`), so the
      terminal state is `ai-blocked` ALONE — even in a repository
      whose claim label is a custom `dispatch_label` (Issue #1775).
    - awaiting_merge: add `ai-awaiting-merge`, remove every delivery-state
      label and the resolved queue label (a maintainer action is
      required before Orbi retries).

    An unknown event raises `ValueError` (fail fast — never a guessed
    patch).
    """
    current = set(current_labels)
    if event == EVENT_CLAIM:
        # The thin-ticket gate's `ai-needs-detail` is not a lifecycle
        # state: a ticket the author edited and re-readied is claimed
        # normally, so the stop label must go with the claim or the
        # ticket is delivered while every status surface still says a
        # human must supply detail (Issue #1379).
        to_remove = [
            label for label in (FIX_NEEDED_LABEL, NEEDS_DETAIL_LABEL)
            if label in current
        ]
        return ([IN_PROGRESS_LABEL], to_remove)
    if event == EVENT_PR_OPENED:
        return ([PR_OPENED_LABEL], [IN_PROGRESS_LABEL])
    if event == EVENT_FIX_NEEDED:
        if MERGED_LABEL in current or BLOCKED_LABEL in current:
            return ([], [])
        to_remove = [
            label for label in (
                IN_PROGRESS_LABEL, PR_OPENED_LABEL, AWAITING_MERGE_LABEL,
            )
            if label in current
        ]
        return ([FIX_NEEDED_LABEL], to_remove)
    if event == EVENT_RELEASE_WAITING:
        return ([ready_label], [IN_PROGRESS_LABEL])
    if event == EVENT_HUMAN_REVIEW_WAITING:
        # The human acceptance gate holds the delivery — the
        # same clean, recoverable tick as the release waiting: the
        # ticket returns to `ai-ready` and a stale in-flight label is
        # cleared. The opened-PR state anchor (`ai-pr-opened` /
        # `ai-fix-needed`) STAYS: the resume scan finds waiting
        # deliveries through it, so each next tick costs one label read
        # and no Pi session; removing the anchor would drop the Issue
        # into the fresh-claim queue for a full re-implement. Never
        # EVENT_FIX_NEEDED (that would burn the review-round budget).
        to_remove = (
            [IN_PROGRESS_LABEL] if IN_PROGRESS_LABEL in current else []
        )
        return ([ready_label], to_remove)
    if event == EVENT_REQUEUE:
        # The external takeover delivery ended without a merge
        # (the contributor withdrew the PR, or a maintainer closed it) —
        # the Issue returns to the ready queue and the next claim redoes
        # the fix internally. Every delivery-state label is cleared so the
        # Issue is `ai-ready` alone.
        to_remove = [
            label for label in _DELIVERY_STATE_LABELS if label in current
        ]
        return ([ready_label], to_remove)
    if event == EVENT_MERGED:
        to_remove = [
            label for label in (*_DELIVERY_STATE_LABELS, AWAITING_MERGE_LABEL)
            if label in current
        ]
        # The ready label is the queue entry, not a delivery state: a
        # merged terminal must clear it too, or the merged Issue still
        # shows `ai-ready` and reads as "still queued" (Issue #1526).
        # The terminal state is `ai-merged` ALONE. Existing merged Issues
        # are not backfilled.
        if ready_label in current:
            to_remove.append(ready_label)
        return ([MERGED_LABEL], to_remove)
    if event == EVENT_BLOCKED:
        # A handoff writes its terminal label before its required Issue
        # comment. If that later write fails, classified failure reporting
        # must still be able to replace the partial handoff rather than leave
        # two lifecycle states on the Issue.
        to_remove = [
            label for label in (
                *_DELIVERY_STATE_LABELS, AWAITING_MERGE_LABEL,
            )
            if label in current
        ]
        # The ready label is the queue entry, not a delivery state: a
        # blocked terminal must clear it too, or the pickup scan
        # re-claims the already-terminal Issue every tick (the #14
        # ai-in-progress add/remove loop, 2026-09-17). The terminal
        # state is BLOCKED ALONE.
        if ready_label in current:
            to_remove.append(ready_label)
        return ([BLOCKED_LABEL], to_remove)
    if event == EVENT_AWAITING_MERGE:
        to_remove = [
            label for label in _DELIVERY_STATE_LABELS if label in current
        ]
        if ready_label in current:
            to_remove.append(ready_label)
        return ([AWAITING_MERGE_LABEL], to_remove)
    raise ValueError(f"unknown delivery event: {event!r}")


def has_delivery_state(current_labels,
                       ready_label: str = READY_LABEL) -> bool:
    """True when the labels carry a delivery state, not just the queue
    entry.

    The one answer to "is this Issue a delivery?" — any lifecycle state
    other than the `ready_label` queue entry counts, terminal states
    included. The CI triage uses it to own the open Issue a PR's
    `Fixes #N` names, so the script carries no lifecycle label set of its
    own (Issue #1786).
    """
    return bool(set(current_labels) & (LIFECYCLE_STATES - {ready_label}))


def is_pickup_eligible(current_labels) -> bool:
    """True when the Issue is fresh-ready: `ai-ready` and not yet in any
    OTHER delivery state (the ready scan's code-layer guard, Issue
    #93/#101).

    `ai-ready` itself is the queue entry, not a delivery state — it is
    excluded from the check. `p0`/`bug` are scheduling metadata, not
    lifecycle states — they never affect eligibility (they only order the
    pickup). The "other delivery state" check is `has_delivery_state`'s
    single decision (Issue #1795), never a second set expression here.
    """
    current = set(current_labels)
    if READY_LABEL not in current:
        return False
    return not has_delivery_state(current)


def is_resumable(current_labels) -> bool:
    """True when the Issue is in an opened-PR state awaiting review or the
    next review session (`ai-pr-opened` or `ai-fix-needed`).

    `ai-in-progress` alone is NOT resumable here: an implement-phase Issue
    has neither opened-PR label, so it never matches (the in-flight restart
    scan handles it separately).
    """
    current = set(current_labels)
    return (
        PR_OPENED_LABEL in current
        or FIX_NEEDED_LABEL in current
        or AWAITING_MERGE_LABEL in current
    )


def needs_human_intervention(current_labels) -> bool:
    """True when the Issue is terminally blocked (`ai-blocked`): it needs a
    human decision before any automatic resume."""
    return BLOCKED_LABEL in set(current_labels)
