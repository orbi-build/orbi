#!/usr/bin/env python3
"""Delivery label lifecycle and scheduling policy.

The single source of truth for the delivery label states, the event ->
label patch transition rules, and the pickup/resume/human-intervention
decisions. GitHub Issues/labels are the only state store; the GitHub
adapter (`orbi.github.edit_issue`) reads and applies the patches.

The module is pure: every function is a deterministic function of its
inputs, with no I/O. The same current labels and event always produce
the same idempotent label patch.

No label NAME is written here (Issue #2347): every state constant below
is a `role` key resolved through `orbi.labels`, the role -> name table
read from the engine's `labels.toml` (with the repository's queue
override applied) at call time. A release that renames a label in that
file changes these answers with no code change.
"""
from __future__ import annotations

from orbi import labels as label_data

# Role constants (one key per platform label) -> the labels.toml role.
_ROLE_ATTRIBUTES = {
    "READY_LABEL": "queue",
    "IN_PROGRESS_LABEL": "in_progress",
    "PR_OPENED_LABEL": "pr_opened",
    "FIX_NEEDED_LABEL": "fix_needed",
    "MERGED_LABEL": "merged",
    "BLOCKED_LABEL": "blocked",
    "AWAITING_MERGE_LABEL": "awaiting_merge",
    "NEEDS_DETAIL_LABEL": "needs_detail",
    "EPIC_LABEL": "epic",
    "RELEASE_LABEL": "release",
    "P0_LABEL": "priority",
    "BUG_LABEL": "bug",
    "CONTENT_ONLY_LABEL": "content_only",
    "OPS_LABEL": "ops_only",
    "HUMAN_REVIEW_LABEL": "human_review",
}


def __getattr__(name: str):
    """Resolve a label constant from the active role -> name table.

    A function, not a module-level value, is what makes the table the one
    answer: the name is read when the engine asks, never frozen at import.
    """
    role = _ROLE_ATTRIBUTES.get(name)
    if role is not None:
        return label_data.active().name(role)
    if name == "LIFECYCLE_STATES":
        return label_data.active().lifecycle_states()
    if name == "NON_LIFECYCLE_LABELS":
        return label_data.active().non_lifecycle_labels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def queue_label(ready_label: str | None = None) -> str:
    """The queue name this decision uses: the repository's resolved one."""
    return label_data.active().with_queue(ready_label).name("queue")


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


def _delivery_state_labels(table) -> tuple[str, ...]:
    """The delivery-state labels a terminal event must clear."""
    return (table.name("in_progress"), table.name("pr_opened"),
            table.name("fix_needed"))


def label_patch(event: str, current_labels,
                ready_label: str | None = None) -> tuple[list[str], list[str]]:
    """Return the (to_add, to_remove) label patch for one event.

    A pure function of the event and the current labels (the same inputs
    always produce the same deterministic, idempotent patch).

    `ready_label` is the repository's RESOLVED queue entry (`dispatch_label`
    from the repository file over the host config, default: the queue role
    in `labels.toml`). It means "queued, work has not started" (Issue
    #2309): the claim REMOVES it and every non-terminal stage event after
    it removes it too, so an old-rule claim converges without migration.

    - claim: add in-progress; remove the queue label and a stale
      fix-needed / needs-detail (a resumed fix round, the thin-ticket gate
      once the author re-readied the ticket).
    - pr_opened: add the opened-PR state, remove in-progress, fix-needed
      and the queue label. A TERMINAL Issue changes nothing on fix_needed:
      the delivery is over or waits for a human, and a stale label would
      resume an automatic fix nobody asked for (Issue #1786).
    - human_review_waiting: add NOTHING, clear in-progress and the queue
      label; the opened-PR anchor stays (the resume scan needs it).
    - requeue / release_waiting: add the queue label back (the ticket is
      queued again) and clear the in-flight/opened-PR labels.
    - merged / blocked / awaiting_merge: the terminals — the resolved
      queue label and every delivery state go, so the terminal label ends
      ALONE (Issues #1526, #1775).

    An unknown event raises `ValueError` (fail fast — never a guessed patch).
    """
    table = label_data.active()
    queue = queue_label(ready_label)
    in_progress = table.name("in_progress")
    pr_opened = table.name("pr_opened")
    fix_needed = table.name("fix_needed")
    merged = table.name("merged")
    blocked = table.name("blocked")
    awaiting_merge = table.name("awaiting_merge")
    needs_detail = table.name("needs_detail")
    current = set(current_labels)
    if event == EVENT_CLAIM:
        # The thin-ticket gate's needs-detail label is not a lifecycle
        # state: a re-readied ticket is claimed normally, so the stop
        # label goes with the claim or the ticket is delivered while every
        # status surface still says a human must supply detail (#1379).
        to_remove = [
            label for label in (fix_needed, needs_detail, queue)
            if label in current
        ]
        return ([in_progress], to_remove)
    if event == EVENT_PR_OPENED:
        return ([pr_opened], [
            label for label in (in_progress, fix_needed, queue)
            if label in current])
    if event == EVENT_FIX_NEEDED:
        if merged in current or blocked in current:
            return ([], [])
        to_remove = [
            label for label in (in_progress, pr_opened, awaiting_merge, queue)
            if label in current
        ]
        return ([fix_needed], to_remove)
    if event == EVENT_RELEASE_WAITING:
        return ([queue], [in_progress])
    if event == EVENT_HUMAN_REVIEW_WAITING:
        # The human acceptance gate holds the delivery: the stale
        # in-flight label and the queue label are cleared and NOTHING is
        # added (Issue #2309) — a waiting ticket is not queued, so the
        # ready scan must not see it. The opened-PR state anchor (pr-opened
        # / fix-needed) STAYS: the resume scan finds waiting deliveries
        # through it, so each next tick costs one label read and no Pi
        # session; removing the anchor would drop the Issue into the
        # fresh-claim queue for a full re-implement. Never EVENT_FIX_NEEDED
        # (that would burn the review-round budget).
        return ([], [
            label for label in (in_progress, queue) if label in current
        ])
    if event == EVENT_REQUEUE:
        # The external takeover delivery ended without a merge
        # (the contributor withdrew the PR, or a maintainer closed it) —
        # the Issue returns to the ready queue and the next claim redoes
        # the fix internally. Every delivery-state label is cleared so the
        # Issue is queued alone.
        to_remove = [
            label for label in _delivery_state_labels(table)
            if label in current
        ]
        return ([queue], to_remove)
    if event == EVENT_MERGED:
        to_remove = [
            label for label in (*_delivery_state_labels(table), awaiting_merge)
            if label in current
        ]
        # The queue label is the queue entry, not a delivery state: a
        # merged terminal must clear it too, or the merged Issue still
        # reads as "still queued" (Issue #1526). The terminal state is
        # merged ALONE. Existing merged Issues are not backfilled.
        if queue in current:
            to_remove.append(queue)
        return ([merged], to_remove)
    if event == EVENT_BLOCKED:
        # A handoff writes its terminal label before its required Issue
        # comment. If that later write fails, classified failure reporting
        # must still be able to replace the partial handoff rather than leave
        # two lifecycle states on the Issue.
        to_remove = [
            label for label in (*_delivery_state_labels(table), awaiting_merge)
            if label in current
        ]
        # The queue label is the queue entry, not a delivery state: a
        # blocked terminal must clear it too, or the pickup scan
        # re-claims the already-terminal Issue every tick (the #14
        # in-progress add/remove loop, 2026-09-17). The terminal state is
        # BLOCKED ALONE.
        if queue in current:
            to_remove.append(queue)
        return ([blocked], to_remove)
    if event == EVENT_AWAITING_MERGE:
        to_remove = [
            label for label in _delivery_state_labels(table)
            if label in current
        ]
        if queue in current:
            to_remove.append(queue)
        return ([awaiting_merge], to_remove)
    raise ValueError(f"unknown delivery event: {event!r}")


def has_delivery_state(current_labels,
                       ready_label: str | None = None) -> bool:
    """True when the labels carry a delivery state, not just the queue
    entry: any lifecycle state other than the queue label, terminals
    included. The CI triage's `Fixes #N` ownership lookup (Issue #1786).
    """
    table = label_data.active()
    return bool(
        set(current_labels)
        & (table.lifecycle_states() - {queue_label(ready_label)})
    )


def is_pickup_eligible(current_labels,
                       ready_label: str | None = None) -> bool:
    """True when the Issue is fresh-ready: in the queue label's role and in
    no OTHER delivery state (the ready scan's code-layer guard, Issue
    #93/#101). The queue name is the repository's resolved one (data,
    never a constant) and the "other delivery state" answer is
    `has_delivery_state`'s single decision (Issue #1795), never a second
    set expression here.
    """
    current = set(current_labels)
    if queue_label(ready_label) not in current:
        return False
    return not has_delivery_state(current, ready_label)


def is_resumable(current_labels) -> bool:
    """True when the Issue is in an opened-PR state awaiting review or the
    next review session. The in-progress label alone is NOT resumable: an
    implement-phase Issue has neither opened-PR label (the in-flight
    restart scan handles it separately).
    """
    table = label_data.active()
    current = set(current_labels)
    return (
        table.name("pr_opened") in current
        or table.name("fix_needed") in current
        or table.name("awaiting_merge") in current
    )


def needs_human_intervention(current_labels) -> bool:
    """True when the Issue is terminally blocked (the blocked role): it
    needs a human decision before any automatic resume."""
    return label_data.active().name("blocked") in set(current_labels)
