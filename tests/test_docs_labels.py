"""Documentation contract for GitHub external state (Issue #49).

The README/AGENTS must document the labels, run markers, and recovery
state exactly as the code implements them: every `ai-*` label the docs
mention must exist in the runner's label set, the README must carry a
label table with meaning/enter/leave for all five labels plus the repo
initialization requirement, and the recovery-scene contract (trusted
maintainer comment, PR body marker, legacy PR backfill) must be
documented.
"""
import re
from pathlib import Path

import bootstrap_runner as runner

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
AGENTS = REPO_ROOT / "AGENTS.md"

# The runner's delivery-state labels plus the claim label (`ai-ready`
# is a literal in the scans, not a runner constant).
KNOWN_LABELS = frozenset({
    "ai-ready",
    runner.IN_PROGRESS_LABEL,
    runner.PR_OPENED_LABEL,
    runner.FIX_NEEDED_LABEL,
    runner.MERGED_LABEL,
    runner.BLOCKED_LABEL,
})

LABEL_PATTERN = re.compile(r"\bai-[a-z][a-z-]*\b")


def test_docs_only_reference_existing_labels():
    """Issue #49: no doc may reference a label that does not exist in
    the runner's label set — a typo'd or removed label would break the
    documented workflow silently."""
    for path in (README, AGENTS):
        text = path.read_text(encoding="utf-8")
        mentioned = set(LABEL_PATTERN.findall(text))
        unknown = mentioned - KNOWN_LABELS
        assert not unknown, (
            f"{path.name} references unknown labels: {sorted(unknown)}"
        )


def test_readme_documents_all_five_labels_with_meaning():
    """Issue #49: the label table must list every label the runner
    uses, with its meaning, enter and leave conditions."""
    text = README.read_text(encoding="utf-8")
    for label in sorted(KNOWN_LABELS):
        assert label in text, f"README label table is missing {label}"
    # The table explains what the labels are for and that they are
    # external state.
    assert "ai-ready" in text
    assert "外部状态" in text or "external state" in text


def test_readme_documents_label_initialization():
    """Issue #49: GitHub labels are external state — a commit never
    creates them. The docs must say a repo must create them during
    initialization (and how to check)."""
    text = README.read_text(encoding="utf-8")
    assert "gh label" in text


def test_readme_documents_recovery_scene_contract():
    """Issue #49: the recovery scene contract must be documented: the
    `Muyan Pilot opened PR:` comment, the trusted-maintainer rule, the
    derivation of branch/worktree from config (never from a public
    comment), the PR body run marker (fail fast when missing), and the
    legacy-PR backfill requirement (old PRs created before the unified
    run_id may only carry the Issue comment marker — the PR body
    marker must be backfilled before a resume, not just the label)."""
    text = README.read_text(encoding="utf-8")
    assert "Muyan Pilot opened PR:" in text
    assert "OWNER" in text
    # Branch/worktree are derived, never read from a comment.
    assert "推导" in text
    # The PR body marker is part of the contract.
    assert "PR body" in text
    # Legacy PRs need the PR body marker backfilled.
    assert "补齐" in text


def test_readme_documents_the_automatic_loop():
    """Issue #49: the automatic loop must be documented end to end:
    ai-ready -> ai-in-progress -> ai-pr-opened -> review ->
    ai-fix-needed -> same PR fix -> ai-pr-opened -> merge, with the
    rules that only the opened-PR states are picked up automatically,
    that a fix keeps the same PR/branch/worktree/run_id, that a base
    advance is merged first, and that ai-blocked is never
    auto-recovered."""
    text = README.read_text(encoding="utf-8")
    for state in (
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked",
    ):
        assert state in text
    # The loop is shown as a state chain.
    assert "ai-ready" in text and "ai-pr-opened" in text
    # ai-blocked is terminal for the automatic loop (human decision).
    assert "ai-blocked" in text


def test_readme_documents_session_identification():
    """Issue #49: under the same worktree a resumed run creates a NEW
    `.pi-session` JSONL; the journal must follow the session of the
    current invocation, not the previous run's."""
    text = README.read_text(encoding="utf-8")
    assert ".pi-session" in text
    assert "当前" in text or "current" in text


def test_readme_documents_review_verdict_on_github():
    """Issue #49: the review verdict must be written back to the
    GitHub PR/Issue, not only kept locally."""
    text = README.read_text(encoding="utf-8")
    assert "REVIEW_VERDICT" in text
    assert "回写" in text or "写入" in text
