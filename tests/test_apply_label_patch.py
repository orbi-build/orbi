"""Direct unit tests for `github.apply_label_patch` (Issues #175, #1865).

`apply_label_patch` computes the deterministic label patch via
`delivery_labels.label_patch` and applies it through `edit_issue`. These
tests cover the branches the delivery-flow tests do not reach: the no-op
patch (nothing changes on the Issue), the add-only patch, the remove-only
patch, the multi-remove patch (two delivery-state labels cleared by one
terminal), the blocked terminal and the terminal fix-needed case.

Issue #1865: the state lives in `FakeGh` and every assertion is the label
set the Issue is left with — the observable result — instead of a captured
`seam.edit_issue` call list. `FakeGh` answers the `gh` argv at the ONE
subprocess seam (`seam.run_command`), so the real adapter runs and the
fake applies the labels the way GitHub does.

Issue #2357: the terminal transitions (merged / blocked) must END on the
new state label — every removal written before the terminal add — and put
the already removed labels back when any write fails.
"""
import subprocess

import pytest

import orbi.github as github
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_FIX_NEEDED,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)
from seam import seam

from tests.fakes.github import FakeGh

NUMBER = 7
REPO = "o/r"


@pytest.fixture
def fake_gh(monkeypatch):
    """The one subprocess seam patched: every `gh` call is the fake."""
    fake = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", fake)
    return fake


def labels(fake_gh, number: int = NUMBER) -> set[str]:
    """The Issue's label set, read back through the real adapter."""
    return set(github.issue_labels(number, REPO))


def apply(fake_gh, event: str, number: int = NUMBER, **kwargs) -> None:
    """Apply `event` to the Issue the fake holds, reading its labels the
    way the caller does: one read, then one deterministic patch."""
    github.apply_label_patch(
        number, repo=REPO, event=event,
        current_labels=github.issue_labels(number, REPO), **kwargs,
    )


def test_apply_label_patch_no_op_patch_applies_nothing(fake_gh):
    """`ai-blocked` on an Issue that already carries it alone: the patch
    adds nothing the Issue lacks and removes nothing, so the label set is
    unchanged — the idempotent terminal (the #14 loop cannot restart)."""
    fake_gh.add_issue(NUMBER, labels=(BLOCKED_LABEL,))
    apply(fake_gh, EVENT_BLOCKED)
    assert labels(fake_gh) == {BLOCKED_LABEL}


def test_apply_label_patch_add_only_patch(fake_gh):
    """A claim on an unlabeled Issue adds `ai-in-progress` and removes
    nothing, so the Issue ends with exactly that label."""
    fake_gh.add_issue(NUMBER)
    apply(fake_gh, EVENT_CLAIM)
    assert labels(fake_gh) == {IN_PROGRESS_LABEL}


def test_apply_label_patch_claim_removes_the_ready_label(fake_gh):
    """Issue #2309: the queue entry means "not started", so the claim
    removes it — the Issue ends `ai-in-progress` alone (the stale
    `ai-fix-needed` goes too)."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, FIX_NEEDED_LABEL))
    apply(fake_gh, EVENT_CLAIM)
    assert labels(fake_gh) == {IN_PROGRESS_LABEL}


def test_apply_label_patch_remove_only_patch_removes_without_adding(
        fake_gh, monkeypatch,
):
    """The human-review wait is a real remove-only patch (Issue #2309);
    the guard is pinned by handing `apply_label_patch` one directly: the
    Issue loses the label and gains nothing."""
    fake_gh.add_issue(NUMBER, labels=(IN_PROGRESS_LABEL,))
    monkeypatch.setattr(
        github, "label_patch",
        lambda event, current_labels, ready_label=None: (
            [], [IN_PROGRESS_LABEL],
        ),
    )
    github.apply_label_patch(
        NUMBER, repo=REPO, event=EVENT_BLOCKED,
        current_labels={IN_PROGRESS_LABEL},
    )
    assert labels(fake_gh) == set()


def test_apply_label_patch_blocked_with_only_non_delivery_labels_is_add_only(
        fake_gh,
):
    """The stale `ai-ready` (the #14 loop) is cleared with the blocked
    add: the terminal state is `ai-blocked` ALONE."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, BLOCKED_LABEL))
    apply(fake_gh, EVENT_BLOCKED)
    assert labels(fake_gh) == {BLOCKED_LABEL}


def test_apply_label_patch_single_remove_patch(fake_gh):
    """Opening the PR moves the Issue from in-flight to awaiting review:
    `ai-in-progress` goes, `ai-pr-opened` is added."""
    fake_gh.add_issue(NUMBER, labels=(IN_PROGRESS_LABEL,))
    apply(fake_gh, EVENT_PR_OPENED)
    assert labels(fake_gh) == {PR_OPENED_LABEL}


def test_apply_label_patch_threads_the_resolved_queue_label(fake_gh):
    """Issue #1775: the resolved repository `dispatch_label` reaches the
    one label decision, so the blocked terminal clears the label the claim
    scanned instead of the host default."""
    fake_gh.add_issue(NUMBER, labels=("ship", IN_PROGRESS_LABEL))
    apply(fake_gh, EVENT_BLOCKED, ready_label="ship")
    assert labels(fake_gh) == {BLOCKED_LABEL}


def test_apply_label_patch_multi_remove_patch_clears_every_state(fake_gh):
    """A blocked terminal on an Issue carrying two delivery-state labels
    clears both (one `gh issue edit` per extra remove) and finishes on
    `ai-blocked` alone."""
    fake_gh.add_issue(NUMBER, labels=(PR_OPENED_LABEL, FIX_NEEDED_LABEL))
    apply(fake_gh, EVENT_BLOCKED)
    assert labels(fake_gh) == {BLOCKED_LABEL}


def test_apply_label_patch_fix_needed_on_a_terminal_issue_applies_nothing(
        fake_gh,
):
    """Issue #1786: the terminal transition is really empty, so applying
    fix-needed to a merged or blocked Issue writes no label at all — the
    engine's single transition decides, and the caller applies it."""
    fake_gh.add_issue(NUMBER, labels=(MERGED_LABEL,))
    apply(fake_gh, EVENT_FIX_NEEDED)
    assert labels(fake_gh) == {MERGED_LABEL}
    fake_gh.add_issue(NUMBER + 1, labels=(BLOCKED_LABEL, PR_OPENED_LABEL))
    apply(fake_gh, EVENT_FIX_NEEDED, number=NUMBER + 1)
    assert labels(fake_gh, NUMBER + 1) == {BLOCKED_LABEL, PR_OPENED_LABEL}



def _label_edits(fake_gh) -> list[list[str]]:
    """The label writes the fake answered, in order."""
    return [command for command in fake_gh.commands
            if command[1] == "issue" and command[2] == "edit"]


def test_merged_terminal_removes_before_adding_the_new_state(fake_gh):
    """Issue #2357: a merge ends the timeline on "added ai-merged".

    Every removal is its own write before the terminal add, so GitHub's
    unordered single-call handling can never leave a removal last."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, PR_OPENED_LABEL))
    apply(fake_gh, EVENT_MERGED)
    writes = _label_edits(fake_gh)
    assert [write[-2:] for write in writes] == [
        ["--remove-label", PR_OPENED_LABEL],
        ["--remove-label", READY_LABEL],
        ["--add-label", MERGED_LABEL],
    ]
    # The terminal label is the last label written and ends alone.
    assert writes[-1] == [
        "gh", "issue", "edit", str(NUMBER), "--repo", REPO,
        "--add-label", MERGED_LABEL,
    ]
    assert labels(fake_gh) == {MERGED_LABEL}


def test_blocked_terminal_removes_before_adding_the_new_state(fake_gh):
    """Issue #2357: a block ends the timeline on "added ai-blocked"."""
    fake_gh.add_issue(NUMBER, labels=(PR_OPENED_LABEL, FIX_NEEDED_LABEL))
    apply(fake_gh, EVENT_BLOCKED)
    writes = _label_edits(fake_gh)
    assert [write[-2:] for write in writes] == [
        ["--remove-label", PR_OPENED_LABEL],
        ["--remove-label", FIX_NEEDED_LABEL],
        ["--add-label", BLOCKED_LABEL],
    ]
    assert labels(fake_gh) == {BLOCKED_LABEL}


def test_failed_terminal_add_restores_the_labels_it_removed(fake_gh):
    """Issue #2357: a failed terminal add puts the removed labels back,
    so the Issue keeps its state label and the next tick recovers it."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, PR_OPENED_LABEL))
    fake_gh.fail_label_edit("add", MERGED_LABEL)
    with pytest.raises(subprocess.CalledProcessError):
        apply(fake_gh, EVENT_MERGED)
    assert labels(fake_gh) == {READY_LABEL, PR_OPENED_LABEL}


def test_synthetic_current_labels_are_not_restored_on_a_failed_add(
        fake_gh,
):
    """Issue #2357: the release close-out passes a hard-coded superset
    (ai-ready + ai-in-progress); only the labels actually on the Issue are
    removed, so the restore never invents ai-ready."""
    fake_gh.add_issue(NUMBER, labels=(IN_PROGRESS_LABEL,))
    fake_gh.fail_label_edit("add", MERGED_LABEL)
    with pytest.raises(subprocess.CalledProcessError):
        github.apply_label_patch(
            NUMBER, repo=REPO, event=EVENT_MERGED,
            current_labels={READY_LABEL, IN_PROGRESS_LABEL},
        )
    assert labels(fake_gh) == {IN_PROGRESS_LABEL}
    assert READY_LABEL not in labels(fake_gh)


def test_failed_second_removal_restores_the_first_removed_label(fake_gh):
    """Issue #2357: when the second removal fails, the first removed
    label is back on the Issue and the terminal label is not added."""
    fake_gh.add_issue(NUMBER, labels=(PR_OPENED_LABEL, FIX_NEEDED_LABEL))
    fake_gh.fail_label_edit("remove", FIX_NEEDED_LABEL)
    with pytest.raises(subprocess.CalledProcessError):
        apply(fake_gh, EVENT_BLOCKED)
    # The first removed label is back; the failed removal and the
    # terminal add never landed.
    assert labels(fake_gh) == {PR_OPENED_LABEL, FIX_NEEDED_LABEL}
    assert BLOCKED_LABEL not in labels(fake_gh)



def test_double_failure_leaves_the_issue_without_a_state_label(
        fake_gh, caplog,
):
    """Issue #2357: when putting the removed labels back fails too, the
    Issue is left without a state label (the accepted double failure) and
    the original write failure still propagates."""
    fake_gh.add_issue(NUMBER, labels=(PR_OPENED_LABEL,))
    fake_gh.fail_label_edit("add", MERGED_LABEL)
    fake_gh.fail_label_edit("add", PR_OPENED_LABEL)
    with caplog.at_level("ERROR"):
        with pytest.raises(subprocess.CalledProcessError):
            apply(fake_gh, EVENT_MERGED)
    assert labels(fake_gh) == set()
    assert "label_restore_failed" in caplog.text
