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
"""
import pytest

import orbi.github as github
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_FIX_NEEDED,
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


def test_apply_label_patch_claim_keeps_the_ready_label(fake_gh):
    """`ai-ready` is the queue entry, not a delivery state: a claim adds
    `ai-in-progress` beside it and clears the stale `ai-fix-needed`."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, FIX_NEEDED_LABEL))
    apply(fake_gh, EVENT_CLAIM)
    assert labels(fake_gh) == {READY_LABEL, IN_PROGRESS_LABEL}


def test_apply_label_patch_remove_only_patch_removes_without_adding(
        fake_gh, monkeypatch,
):
    """No event produces a remove-only patch today — every transition adds
    its own state — so the "nothing to add" guard is reached by handing
    `apply_label_patch` the patch the pure transition would return: the
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
