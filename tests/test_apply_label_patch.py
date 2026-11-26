"""Direct unit tests for `github.apply_label_patch` (Issues #175, #1865, #2426).

`apply_label_patch` computes the deterministic label patch via
`delivery_labels.label_patch` and applies it as ONE whole-set replacement
(GitHub's "Set labels for an issue") computed from the Issue's real labels.

Issue #1865: the state lives in `FakeGh` and every assertion is the label
set the Issue is left with — the observable result — instead of a captured
`seam.edit_issue` call list. `FakeGh` answers the `gh` argv at the ONE
subprocess seam (`seam.run_command`), so the real adapter runs and the
fake applies the labels the way GitHub does.

Issue #2426 (the ticket's Outcome): every delivery-label change is
all-or-nothing. A removal and an add sent as several writes — or as one
`gh issue edit` carrying both flags — can apply one and fail the other,
which stranded four deliveries during GitHub's 2026-11-25 incidents. So:

- the change is ONE `PUT .../issues/{n}/labels` replacement;
- after a failed write the Issue holds exactly the labels it had before
  the change or exactly the labels the change intended;
- non-delivery labels (`bug`, `p0`, a customer's own label) survive.
"""
import subprocess

import pytest

import orbi.github as github
from orbi import delivery_labels
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EVENT_AWAITING_MERGE,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_FIX_NEEDED,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    EVENT_REQUEUE,
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
    """The one subprocess seam patched: every `gh` call is the fake.

    Retries sleep for real backoff seconds; the tests never do.
    """
    fake = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(github.time, "sleep", lambda _seconds: None)
    return fake


def labels(fake_gh, number: int = NUMBER) -> set[str]:
    """The Issue's label set, read back through the real adapter."""
    return set(github.issue_labels(number, REPO))


def apply(fake_gh, event: str, number: int = NUMBER, **kwargs):
    """Apply `event` to the Issue the fake holds; return nothing."""
    return github.apply_label_patch(
        number, repo=REPO, event=event, **kwargs,
    )


def _label_replacements(fake_gh) -> list[list[str]]:
    """The whole-set label writes the fake answered, in order."""
    return [
        command for command in fake_gh.commands
        if command[0:4] == ["gh", "api", "--method", "PUT"]
        and command[4].endswith("/labels")
    ]


def _label_edits(fake_gh) -> list[list[str]]:
    """The single-label `gh issue edit` writes the fake answered."""
    return [command for command in fake_gh.commands
            if command[1] == "issue" and command[2] == "edit"]


def test_apply_label_patch_no_op_patch_applies_nothing(fake_gh):
    """`ai-blocked` on an Issue that already carries it alone: the patch
    adds nothing the Issue lacks and removes nothing, so the label set is
    unchanged — the idempotent terminal (the #14 loop cannot restart)."""
    fake_gh.add_issue(NUMBER, labels=(BLOCKED_LABEL,))
    apply(fake_gh, EVENT_BLOCKED)
    assert labels(fake_gh) == {BLOCKED_LABEL}
    assert _label_replacements(fake_gh) == []


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
    apply(fake_gh, EVENT_BLOCKED)
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
    clears both in ONE write and finishes on `ai-blocked` alone."""
    fake_gh.add_issue(NUMBER, labels=(PR_OPENED_LABEL, FIX_NEEDED_LABEL))
    apply(fake_gh, EVENT_BLOCKED)
    assert labels(fake_gh) == {BLOCKED_LABEL}
    assert _label_replacements(fake_gh) == [[
        "gh", "api", "--method", "PUT",
        f"repos/{REPO}/issues/{NUMBER}/labels",
        "-f", f"labels[]={BLOCKED_LABEL}", "--include",
    ]]


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
    assert _label_replacements(fake_gh) == []


def test_the_whole_set_comes_from_the_issue_not_the_callers_snapshot(fake_gh):
    """Issue #2369 acceptance 1: the change reads the Issue's real labels,
    so a label the caller does not know about survives and a stale delivery
    state the caller does know about still goes."""
    fake_gh.add_issue(
        NUMBER, labels=("bug", READY_LABEL, FIX_NEEDED_LABEL),
    )
    apply(fake_gh, EVENT_CLAIM)
    assert labels(fake_gh) == {"bug", IN_PROGRESS_LABEL}
    assert _label_replacements(fake_gh) == [[
        "gh", "api", "--method", "PUT",
        f"repos/{REPO}/issues/{NUMBER}/labels",
        "-f", "labels[]=bug", "-f", f"labels[]={IN_PROGRESS_LABEL}",
        "--include",
    ]]


def test_pr_opened_replacement_ends_on_the_new_state_alone(fake_gh):
    """Issue #2369 acceptance 2: a resumed `ai-fix-needed` round is one
    whole-set replacement that ends on `ai-pr-opened`; every other label
    on the Issue survives."""
    fake_gh.add_issue(NUMBER, labels=(FIX_NEEDED_LABEL, "bug"))
    apply(fake_gh, EVENT_PR_OPENED)
    assert labels(fake_gh) == {"bug", PR_OPENED_LABEL}
    assert len(_label_replacements(fake_gh)) == 1


# --- Issue #2426 acceptance 1: all-or-nothing ------------------------------
#
# One table of (event, labels before the change): each row is a real
# delivery transition whose patch adds and removes at least one label, and
# every row carries non-delivery labels (`bug`, `p0`, a customer's own
# label) that must survive whatever GitHub does with the write.

TRANSITIONS = [
    pytest.param(
        EVENT_CLAIM, (READY_LABEL, "bug", "p0"), id="claim",
    ),
    pytest.param(
        EVENT_PR_OPENED, (IN_PROGRESS_LABEL, "bug", "customer"), id="pr-opened",
    ),
    pytest.param(
        EVENT_FIX_NEEDED, (PR_OPENED_LABEL, "bug", "p0"), id="fix-needed",
    ),
    pytest.param(
        EVENT_BLOCKED, (PR_OPENED_LABEL, FIX_NEEDED_LABEL, "bug", "p0"),
        id="blocked",
    ),
    pytest.param(
        EVENT_MERGED, (PR_OPENED_LABEL, "bug", "p0"), id="merged",
    ),
    pytest.param(
        EVENT_REQUEUE, (IN_PROGRESS_LABEL, "bug", "p0"), id="requeue",
    ),
    pytest.param(
        EVENT_AWAITING_MERGE, (FIX_NEEDED_LABEL, "bug"), id="awaiting-merge",
    ),
]


def _intended(event: str, before) -> set[str]:
    """The labels the engine's ONE transition decision intends."""
    to_add, to_remove = delivery_labels.label_patch(event, set(before))
    return (set(before) - set(to_remove)) | set(to_add)


@pytest.mark.parametrize("event, before", TRANSITIONS)
def test_a_failed_change_leaves_either_the_before_or_the_intended_labels(
        fake_gh, event, before,
):
    """Acceptance 1: GitHub fails the change. The Issue ends with EXACTLY
    the labels it had (the write never landed) and a single whole-set
    request carried the change — never a removal without its add."""
    fake_gh.add_issue(NUMBER, labels=before)
    fake_gh.fail_label_replace(status=502)
    with pytest.raises(subprocess.CalledProcessError):
        apply(fake_gh, event)
    assert labels(fake_gh) == set(before)
    writes = _label_replacements(fake_gh)
    assert writes and all(
        command[4] == f"repos/{REPO}/issues/{NUMBER}/labels"
        for command in writes
    )
    assert _label_edits(fake_gh) == []


@pytest.mark.parametrize("event, before", TRANSITIONS)
def test_a_change_that_reports_an_error_after_applying_is_done(
        fake_gh, caplog, event, before,
):
    """Acceptance 2: the write fails but GitHub applied it. The read after
    the failure shows the intended labels, so the runner treats the change
    as done — ONE write, no retry write, no compensating write."""
    fake_gh.add_issue(NUMBER, labels=before)
    fake_gh.fail_label_replace(status=502, applied=True)
    with caplog.at_level("WARNING"):
        apply(fake_gh, event)
    assert labels(fake_gh) == _intended(event, before)
    assert len(_label_replacements(fake_gh)) == 1
    assert _label_edits(fake_gh) == []
    assert "already applied" in caplog.text


# --- the unblock primitive (Issue #2426) ---------------------------------


def test_swap_issue_labels_keeps_every_other_label(fake_gh):
    """The recovered-delivery unblock: `ai-blocked` out, `ai-fix-needed`
    in, ONE write, and every non-delivery label kept."""
    fake_gh.add_issue(NUMBER, labels=(BLOCKED_LABEL, "bug", "p0"))
    applied = github.swap_issue_labels(
        NUMBER, repo=REPO, add=[FIX_NEEDED_LABEL], remove=[BLOCKED_LABEL],
    )
    assert applied == ["bug", "p0", FIX_NEEDED_LABEL]
    assert labels(fake_gh) == {"bug", "p0", FIX_NEEDED_LABEL}
    assert len(_label_replacements(fake_gh)) == 1


def test_swap_issue_labels_writes_nothing_when_the_set_is_unchanged(fake_gh):
    """A swap that changes no label writes no label: the Issue already
    carries the state the transition wants."""
    fake_gh.add_issue(NUMBER, labels=("bug", FIX_NEEDED_LABEL))
    applied = github.swap_issue_labels(
        NUMBER, repo=REPO, add=[FIX_NEEDED_LABEL], remove=[BLOCKED_LABEL],
    )
    assert applied == ["bug", FIX_NEEDED_LABEL]
    assert _label_replacements(fake_gh) == []


def test_a_failed_replacement_leaves_the_labels_as_they_were(fake_gh):
    """Issue #2369 acceptance 3: the replacement is ONE write, so a
    failed one changes nothing and the step fails as today."""
    fake_gh.add_issue(NUMBER, labels=(READY_LABEL, "bug"))
    fake_gh.fail_label_replace(status=403)
    with pytest.raises(subprocess.CalledProcessError):
        apply(fake_gh, EVENT_CLAIM)
    assert labels(fake_gh) == {READY_LABEL, "bug"}
