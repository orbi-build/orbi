"""Direct unit tests for `runner.apply_label_patch` (Issue #175).

`apply_label_patch` computes the deterministic label patch via
`delivery_labels.label_patch` and applies it through `edit_issue`. These
tests cover the branches the delivery-flow tests do not reach: the no-op
patch (nothing to add or remove), the add-only patch, the remove-only
patch, and the multi-remove patch (one `edit_issue` call per extra remove).
"""
import orbi.runner as runner


def test_apply_label_patch_no_op_patch_applies_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    # `label_patch` never returns an empty patch for a known event, so
    # the no-op guard is defensive: exercise it by stubbing the patch to
    # (nothing to add, nothing to remove) — `apply_label_patch` must
    # apply nothing (no `edit_issue` call).
    monkeypatch.setattr(
        runner, "label_patch",
        lambda event, current_labels: ([], []),
    )
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_BLOCKED,
        current_labels={"ai-ready", "ai-blocked"},
    )
    assert calls == []


def test_apply_label_patch_blocked_with_only_non_delivery_labels_is_add_only(
        monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    # `ai-blocked` is not a delivery-state label, so the remove list is
    # empty: one add-only call.
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_BLOCKED,
        current_labels={"ai-ready", "ai-blocked"},
    )
    assert calls == [
        ((7,), {"repo": "o/r", "add": "ai-blocked"}),
    ]


def test_apply_label_patch_add_only_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_CLAIM, current_labels=(),
    )
    assert calls == [
        ((7,), {"repo": "o/r", "add": "ai-in-progress"}),
    ]


def test_apply_label_patch_single_remove_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_PR_OPENED, current_labels=(),
    )
    assert calls == [
        ((7,), {"repo": "o/r", "add": "ai-pr-opened",
                "remove": "ai-in-progress"}),
    ]


def test_apply_label_patch_remove_only_patch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    # A remove-only patch (nothing to add) must emit a single remove-only
    # `edit_issue` call — no `add` kwarg.
    monkeypatch.setattr(
        runner, "label_patch",
        lambda event, current_labels: ([], ["ai-in-progress"]),
    )
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_BLOCKED,
        current_labels={"ai-in-progress"},
    )
    assert calls == [
        ((7,), {"repo": "o/r", "remove": "ai-in-progress"}),
    ]


def test_apply_label_patch_multi_remove_patch_one_call_per_remove(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    # A blocked patch with two present delivery-state labels produces
    # one add+first-remove call, then one remove-only call per extra.
    runner.apply_label_patch(
        7, repo="o/r", event=runner.EVENT_BLOCKED,
        current_labels={"ai-pr-opened", "ai-fix-needed"},
    )
    assert calls == [
        ((7,), {"repo": "o/r", "add": "ai-blocked",
                "remove": "ai-pr-opened"}),
        ((7,), {"repo": "o/r", "remove": "ai-fix-needed"}),
    ]
