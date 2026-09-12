"""Tests for the delivery label lifecycle and scheduling policy
(`orbi.delivery_labels`, Issue #175).

The module is pure: every function is a deterministic function of its
inputs. These tests cover the normal transitions, repeated (idempotent)
transitions, illegal combinations, and the pickup/resume/human-intervention
decisions — including that `p0`/`bug`/`ai-epic`/`blockedBy` are NOT
modeled as delivery lifecycle states.
"""
import subprocess
from pathlib import Path

import pytest

from orbi import delivery_labels as dl

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- label_patch: normal transitions ---------------------------------

def test_label_patch_claim_adds_in_progress_only():
    to_add, to_remove = dl.label_patch(dl.EVENT_CLAIM, {"ai-ready"})
    assert to_add == ["ai-in-progress"]
    assert to_remove == []


def test_label_patch_claim_clears_fix_needed_from_fix_round():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_CLAIM, {"ai-ready", "ai-fix-needed"},
    )
    assert to_add == ["ai-in-progress"]
    assert to_remove == ["ai-fix-needed"]


def test_label_patch_release_waiting_returns_release_to_ready_queue():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_RELEASE_WAITING, {"ai-ready", "ai-in-progress"},
    )
    assert to_add == ["ai-ready"]
    assert to_remove == ["ai-in-progress"]


def test_label_patch_human_review_waiting_keeps_the_opened_pr_anchor():
    """Issue #763: the human-review waiting patch follows the release
    waiting shape — the ticket returns to `ai-ready` and a stale
    in-flight label is cleared — while the opened-PR state anchor
    STAYS: the resume scan finds waiting deliveries through
    `ai-pr-opened`/`ai-fix-needed`, so removing the anchor would drop
    the delivery into the fresh-claim queue for a full re-implement."""
    to_add, to_remove = dl.label_patch(
        dl.EVENT_HUMAN_REVIEW_WAITING, {"ai-pr-opened"},
    )
    assert to_add == ["ai-ready"]
    assert to_remove == []
    # A stale in-flight residue is still cleared (the release-waiting
    # shape), and a fix-round ticket keeps its own anchor.
    assert dl.label_patch(
        dl.EVENT_HUMAN_REVIEW_WAITING,
        {"ai-pr-opened", "ai-in-progress"},
    ) == (["ai-ready"], ["ai-in-progress"])
    assert dl.label_patch(
        dl.EVENT_HUMAN_REVIEW_WAITING, {"ai-fix-needed"},
    ) == (["ai-ready"], [])


def test_label_patch_never_adds_or_removes_the_human_review_label():
    """Issue #763 hard constraint: `ai-human-review` is a human-only
    label (the `ai-release` shape) — the Runner never applies or
    removes it, so no event's patch may name it in either direction."""
    events = (
        dl.EVENT_CLAIM, dl.EVENT_PR_OPENED, dl.EVENT_FIX_NEEDED,
        dl.EVENT_MERGED, dl.EVENT_RELEASE_WAITING, dl.EVENT_REQUEUE,
        dl.EVENT_BLOCKED, dl.EVENT_HUMAN_REVIEW_WAITING,
    )
    current_sets = (
        set(), {"ai-ready"}, {"ai-in-progress"}, {"ai-pr-opened"},
        {"ai-fix-needed"}, {"ai-ready", "ai-pr-opened"},
        {"ai-human-review"},
    )
    for event in events:
        for current in current_sets:
            to_add, to_remove = dl.label_patch(event, current)
            assert dl.HUMAN_REVIEW_LABEL not in to_add, (event, current)
            assert dl.HUMAN_REVIEW_LABEL not in to_remove, (event, current)


def test_label_patch_requeue_returns_external_takeover_to_ready_queue():
    """Issue #608: the external takeover delivery ended without a merge
    (contributor withdrew / maintainer rejected) — the Issue returns to
    `ai-ready` ALONE for the internal redo, whatever opened-PR state
    label it carried."""
    to_add, to_remove = dl.label_patch(
        dl.EVENT_REQUEUE, {"ai-pr-opened"},
    )
    assert to_add == ["ai-ready"]
    assert to_remove == ["ai-pr-opened"]
    to_add, to_remove = dl.label_patch(
        dl.EVENT_REQUEUE, {"ai-fix-needed", "ai-in-progress"},
    )
    assert to_add == ["ai-ready"]
    assert to_remove == ["ai-in-progress", "ai-fix-needed"]
    # Idempotent patch shape (the release_waiting convention: the add is
    # always named; the caller's no-op detection drops it when present).
    assert dl.label_patch(dl.EVENT_REQUEUE, {"ai-ready"}) == (
        ["ai-ready"], [],
    )


def test_label_patch_pr_opened_swaps_in_progress_for_pr_opened():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_PR_OPENED, {"ai-ready", "ai-in-progress"},
    )
    assert to_add == ["ai-pr-opened"]
    assert to_remove == ["ai-in-progress"]


def test_label_patch_fix_needed_swaps_pr_opened_for_fix_needed():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_FIX_NEEDED, {"ai-ready", "ai-pr-opened"},
    )
    assert to_add == ["ai-fix-needed"]
    assert to_remove == ["ai-pr-opened"]


def test_label_patch_fix_needed_clears_stale_in_progress_too():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_FIX_NEEDED,
        {"ai-ready", "ai-pr-opened", "ai-in-progress"},
    )
    assert to_add == ["ai-fix-needed"]
    assert to_remove == ["ai-in-progress", "ai-pr-opened"]


def test_label_patch_fix_round_sequence_leaves_only_merged():
    labels = {"ai-ready"}
    add, remove = dl.label_patch(dl.EVENT_CLAIM, labels)
    labels.update(add)
    labels.difference_update(remove)
    add, remove = dl.label_patch(dl.EVENT_PR_OPENED, labels)
    labels.update(add)
    labels.difference_update(remove)
    add, remove = dl.label_patch(dl.EVENT_FIX_NEEDED, labels)
    labels.update(add)
    labels.difference_update(remove)
    # A resumed fix round claims from the actual GitHub label set.
    add, remove = dl.label_patch(dl.EVENT_CLAIM, labels)
    labels.update(add)
    labels.difference_update(remove)
    add, remove = dl.label_patch(dl.EVENT_MERGED, labels)
    labels.update(add)
    labels.difference_update(remove)
    assert labels == {"ai-ready", "ai-merged"}


def test_label_patch_merged_removes_pr_opened():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_MERGED, {"ai-ready", "ai-pr-opened"},
    )
    assert to_add == ["ai-merged"]
    assert to_remove == ["ai-pr-opened"]


def test_label_patch_merged_removes_in_progress_for_release_flow():
    # The release state machine never enters the PR states: its merged
    # transition clears `ai-in-progress` (the claim label), not
    # `ai-pr-opened`.
    to_add, to_remove = dl.label_patch(
        dl.EVENT_MERGED, {"ai-ready", "ai-in-progress"},
    )
    assert to_add == ["ai-merged"]
    assert to_remove == ["ai-in-progress"]


def test_label_patch_blocked_clears_every_present_delivery_state_label():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_BLOCKED,
        {"ai-ready", "ai-pr-opened", "ai-fix-needed"},
    )
    assert to_add == ["ai-blocked"]
    assert to_remove == ["ai-pr-opened", "ai-fix-needed"]


def test_label_patch_blocked_clears_in_progress_when_only_claim_present():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_BLOCKED, {"ai-ready", "ai-in-progress"},
    )
    assert to_add == ["ai-blocked"]
    assert to_remove == ["ai-in-progress"]


# --- label_patch: idempotency ----------------------------------------

def test_label_patch_is_deterministic_for_repeated_inputs():
    labels = {"ai-ready", "ai-pr-opened"}
    first = dl.label_patch(dl.EVENT_BLOCKED, labels)
    second = dl.label_patch(dl.EVENT_BLOCKED, labels)
    assert first == second


def test_label_patch_blocked_is_idempotent_when_already_blocked():
    # Re-applying the blocked event to an already-blocked Issue produces
    # a no-op remove (no delivery-state label is present) plus the
    # `ai-blocked` add — never a second remove of an absent label.
    to_add, to_remove = dl.label_patch(
        dl.EVENT_BLOCKED, {"ai-ready", "ai-blocked"},
    )
    assert to_add == ["ai-blocked"]
    assert to_remove == []


# --- label_patch: illegal combinations -------------------------------

def test_label_patch_unknown_event_raises():
    with pytest.raises(ValueError):
        dl.label_patch("not-an-event", {"ai-ready"})


def test_label_patch_blocked_with_no_delivery_state_label_is_add_only():
    # An Issue with no delivery-state label (e.g. a fresh `ai-ready`)
    # gets `ai-blocked` added and nothing removed — the patch must not
    # invent a remove for an absent label.
    to_add, to_remove = dl.label_patch(dl.EVENT_BLOCKED, {"ai-ready"})
    assert to_add == ["ai-blocked"]
    assert to_remove == []


def test_label_patch_accepts_any_iterable_of_labels():
    to_add, to_remove = dl.label_patch(
        dl.EVENT_BLOCKED, ["ai-ready", "ai-pr-opened"],
    )
    assert to_add == ["ai-blocked"]
    assert to_remove == ["ai-pr-opened"]


# --- lifecycle states vs scheduling metadata -------------------------

def test_lifecycle_states_are_exactly_the_six_delivery_labels():
    assert dl.LIFECYCLE_STATES == frozenset({
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked",
    })


def test_scheduling_metadata_labels_are_not_lifecycle_states():
    for label in (dl.P0_LABEL, dl.BUG_LABEL, dl.EPIC_LABEL,
                  dl.RELEASE_LABEL, dl.CONTENT_ONLY_LABEL, dl.OPS_LABEL,
                  dl.HUMAN_REVIEW_LABEL):
        assert label not in dl.LIFECYCLE_STATES


def test_human_review_label_is_exactly_the_issue_fixed_name():
    """Issue #763 section 0: the label name is fixed — the carrier of
    the human acceptance gate is this one label, matching the `ai-*`
    family and the cloud control-plane display ticket."""
    assert dl.HUMAN_REVIEW_LABEL == "ai-human-review"


# --- pickup / resume / human-intervention decisions ------------------

def test_is_pickup_eligible_true_for_fresh_ready_issue():
    assert dl.is_pickup_eligible({"ai-ready"}) is True


def test_is_pickup_eligible_true_with_p0_and_bug_metadata():
    # `p0` and `bug` are scheduling metadata, not lifecycle states — they
    # never affect pickup eligibility.
    assert dl.is_pickup_eligible({"ai-ready", "p0"}) is True
    assert dl.is_pickup_eligible({"ai-ready", "bug"}) is True


def test_is_pickup_eligible_false_when_in_any_delivery_state():
    for state in ("ai-in-progress", "ai-pr-opened", "ai-fix-needed",
                  "ai-merged", "ai-blocked"):
        assert dl.is_pickup_eligible({"ai-ready", state}) is False


def test_is_pickup_eligible_false_without_ai_ready():
    assert dl.is_pickup_eligible({"ai-in-progress"}) is False
    assert dl.is_pickup_eligible(set()) is False


def test_is_resumable_true_for_opened_pr_states():
    assert dl.is_resumable({"ai-ready", "ai-pr-opened"}) is True
    assert dl.is_resumable({"ai-ready", "ai-fix-needed"}) is True


def test_is_resumable_false_for_implement_phase_and_terminals():
    # `ai-in-progress` alone is NOT resumable here (an implement-phase
    # Issue has neither opened-PR label); terminals are not resumable.
    assert dl.is_resumable({"ai-ready", "ai-in-progress"}) is False
    assert dl.is_resumable({"ai-ready", "ai-merged"}) is False
    assert dl.is_resumable({"ai-ready", "ai-blocked"}) is False
    assert dl.is_resumable({"ai-ready"}) is False


def test_needs_human_intervention_true_only_for_ai_blocked():
    assert dl.needs_human_intervention({"ai-ready", "ai-blocked"}) is True
    assert dl.needs_human_intervention({"ai-ready", "ai-pr-opened"}) is False
    assert dl.needs_human_intervention({"ai-ready", "ai-fix-needed"}) is False
    assert dl.needs_human_intervention({"ai-ready"}) is False


# --- the ops-only marker name (Issue #530) --------------------------------

# Composed so this guard file does not contain the very name it forbids
# (acceptance criterion: the old literal has zero hits in the repo).
_OLD_OPS_LABEL = "ai-" "ticket-only"


def test_ops_only_label_is_the_renamed_marker_with_no_stale_reference():
    """Issue #530/#537: `ai-ops-only` is the FULL-EXECUTION ops marker
    (the content path moved to `ai-content-only`); the old label name
    must survive nowhere in the tracked tree.

    `git grep` reads only tracked files, so run artifacts and Pi session
    logs can never decide this contract.
    """
    assert dl.OPS_LABEL == "ai-ops-only"
    assert dl.CONTENT_ONLY_LABEL == "ai-content-only"
    result = subprocess.run(
        ["git", "grep", "-n", _OLD_OPS_LABEL],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 1, result.stdout  # 1 = no match
