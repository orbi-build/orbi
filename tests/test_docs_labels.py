"""Documentation contract for GitHub external state (Issue #49, moved
off the README homepage by Issue #241).

The docs and AGENTS must document the labels, run markers, and recovery
state exactly as the code implements them: every `ai-*` label the docs
mention must exist in the runner's label set, docs/workflow.mdx carries
the label table (meaning per label, external state), docs/setup.mdx
carries the label initialization, and the recovery-scene contract
(trusted maintainer comment, derived branch/worktree, PR body run
marker) is documented in docs/security.mdx / docs/workflow.mdx.
"""
import re
from pathlib import Path

import bootstrap_runner as runner

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
AGENTS = REPO_ROOT / "AGENTS.md"
DOCS_DIR = REPO_ROOT / "docs"


def docs_page(slug: str) -> str:
    path = DOCS_DIR / f"{slug}.mdx"
    assert path.is_file(), f"missing docs page: {path}"
    return path.read_text(encoding="utf-8")

# The runner's delivery-state labels plus the claim label (`ai-ready`
# is a literal in the scans, not a runner constant) and the Epic marker
# (Issue #93: the claim scan skips `ai-epic` Issues).
KNOWN_LABELS = frozenset({
    "ai-ready",
    runner.IN_PROGRESS_LABEL,
    runner.PR_OPENED_LABEL,
    runner.FIX_NEEDED_LABEL,
    runner.MERGED_LABEL,
    runner.BLOCKED_LABEL,
    runner.EPIC_LABEL,
    runner.RELEASE_LABEL,
    runner.TICKET_ONLY_LABEL,
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


def test_workflow_documents_all_labels_with_meaning():
    """Issue #49/#241: the label table lives in docs/workflow.mdx (the
    single source of truth; the README homepage keeps only a summary
    plus the link): every delivery state label with its meaning, the
    Epic and Release type markers, and the statement that labels are
    external state a commit never creates."""
    text = docs_page("workflow")
    for label in ("ai-ready", "ai-in-progress", "ai-pr-opened",
                  "ai-fix-needed", "ai-merged", "ai-blocked"):
        assert label in text, f"workflow label table is missing {label}"
    assert "ai-epic" in text, "workflow must document the Epic marker"
    assert "ai-release" in text, "workflow must document the Release marker"
    assert "external state" in text, (
        "workflow must state that labels are external state"
    )


def test_setup_documents_label_initialization():
    """Issue #49/#241: GitHub labels are external state — a commit
    never creates them. docs/setup.mdx documents the initialization:
    the one-time setup aligns the platform labels declaratively from
    the repo-managed labels.toml (the single source of truth for label
    name, color and description)."""
    text = docs_page("setup")
    assert "gh label" in text
    assert "labels.toml" in text


def test_security_documents_recovery_scene_contract():
    """Issue #49/#241: the recovery scene contract is documented in
    docs/security.mdx (EN + ZH): the resume scene is only read from
    comments posted by a trusted maintainer (OWNER/MAINTAINER/MEMBER/
    COLLABORATOR), and branch/worktree paths are derived from the
    configured repo, Issue number and run id — a public comment can
    never steer the Runner into an arbitrary local path. The scene
    comment prefix itself is the code contract (bootstrap_runner).
    """
    for slug in ("security", "zh/security"):
        text = docs_page(slug)
        assert "trusted" in text, f"docs/{slug} must name the trusted-maintainer rule"
        assert "OWNER" in text and "COLLABORATOR" in text, (
            f"docs/{slug} must list the trusted author associations"
        )
        assert "derived" in text or "推导" in text, (
            f"docs/{slug} must say branch/worktree are derived"
        )
    # The scene comment prefix is the real code contract.
    assert runner.OPENED_PR_PREFIX == "Muyan Pilot opened PR: "
    # The PR body run marker is part of the documented contract.
    assert "muyan-pilot:run=" in docs_page("workflow")


def test_workflow_documents_the_automatic_loop():
    """Issue #49/#241: the automatic loop is documented end to end in
    docs/workflow.mdx: ai-ready -> ai-in-progress -> ai-pr-opened ->
    review -> ai-fix-needed -> same PR fix -> merge, with the rules
    that only the opened-PR states are picked up automatically and
    that ai-blocked is never auto-recovered."""
    text = docs_page("workflow")
    for state in (
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked",
    ):
        assert state in text
    # The loop is shown as a state chain.
    assert "ai-ready" in text and "ai-pr-opened" in text
    # ai-blocked is terminal for the automatic loop (human decision).
    assert "never auto-recovered" in text


def test_operations_documents_the_session_record():
    """Issue #49/#241: the session record is documented in
    docs/operations.mdx — the `muyan-pilot session` command prints the
    live Pi session JSONL path (fail fast when no session exists) —
    and the code keeps the session in the task worktree's
    `.pi-session` directory (a resumed run starts a new session file
    there; the journal tracks the current invocation's session).
    """
    text = docs_page("operations")
    assert "muyan-pilot session" in text
    assert "JSONL" in text
    # The real session directory the code passes to Pi.
    assert ".pi-session" in text or ".pi-session" in (
        (REPO_ROOT / "bootstrap_runner.py").read_text(encoding="utf-8")
    )


def test_workflow_documents_review_verdict_on_github():
    """Issue #49/#241: the review verdict and the failure scene are
    written back to the GitHub PR/Issue (not only kept locally) —
    docs/workflow.mdx documents the REVIEW_VERDICT and the failure
    comment written to the Issue AND the PR."""
    text = docs_page("workflow")
    assert "REVIEW_VERDICT" in text
    assert "written to the" in text and "PR" in text


def test_operations_documents_model_wait_dead_kill():
    """Issue #218/#241 (supersedes the #75/#169 contract): the
    hung-model-request contract is documented in docs/operations.mdx:
    while the newest session event is a tool result (model_wait) and
    the session JSONL is frozen past the configured dead threshold
    (`model_wait_dead_seconds`, default 1800 s,
    PI_MODEL_WAIT_DEAD_SECONDS), the Runner logs the structured
    `model_wait_dead` line (upstream_alive is evidence, never a veto —
    process alive ≠ responding), kills Pi and fails fast with
    `run_failed ... reason=model_wait_dead_stale_...`; the kill never
    fires while events keep arriving (a slow generation is not a hung
    request) and it is NOT a business-task timeout."""
    text = docs_page("operations")
    # The run_failed line carries the hung-model-request reason.
    assert "model_wait_dead_stale_" in text
    # The structured model_wait_dead line is documented with its
    # fields.
    assert "model_wait_dead" in text
    assert "action=kill_pi" in text
    assert "reason=hung_model_request" in text
    assert "upstream_alive" in text
    # The kill is documented with its trigger (model_wait + frozen
    # session) and the configurable threshold.
    assert "PI_MODEL_WAIT_DEAD_SECONDS" in text
    assert "model_wait" in text
    # The slow-generation boundary: events keep arriving -> no kill.
    assert "never trips it" in text
    # It is not a business-task timeout.
    assert "business task timeout" in text


def test_workflow_documents_p0_priority_and_terminal_state():
    """Issue #101/#241: docs/workflow.mdx documents the P0 contract:
    the plain `p0` label (not a delivery state), the fixed pickup
    order (`ai-ready`+`p0` → `ai-ready`+`bug` → plain `ai-ready`), the
    unchanged exclusion/single-slot semantics, and the terminal state
    of a failed P0 (`ai-blocked`, never re-claimed — no infinite
    retry)."""
    text = docs_page("workflow")
    assert "`p0`" in text
    # It is explicitly NOT a delivery state.
    assert "NOT a delivery state" in text
    # The fixed pickup order is documented.
    assert "`ai-ready`+`p0`" in text
    assert "`ai-ready`+`bug`" in text
    # The existing exclusion rules and single-slot constraint apply.
    assert "single-slot" in text
    # The terminal state of a failed P0: ai-blocked, no infinite retry.
    assert "no infinite retry" in text
    assert "ai-blocked" in text


def test_agents_md_documents_p0_priority():
    """Issue #101: the development contract must document the same P0
    semantics: plain label, fixed pickup order, unchanged exclusions
    and single-slot constraint, `priority=` log/progress field, and the
    `ai-blocked` terminal state without infinite retry."""
    text = AGENTS.read_text(encoding="utf-8")
    assert "pickup priority (p0)" in text.lower()
    assert "`p0`" in text
    assert "`ai-ready`+`p0`" in text
    assert "`ai-ready`+`bug`" in text
    # The explicit log/progress field.
    assert "priority=p0" in text
    # The terminal state without infinite retry.
    assert "ai-blocked" in text
    assert "no infinite retry" in text


def test_agents_md_documents_model_wait_dead_kill():
    """Issue #218 (supersedes the #75/#169 contract): the development
    contract (AGENTS.md) must document the same hung-model-request
    behavior in its observability section: a frozen model_wait past the
    dead threshold kills Pi and fails fast (slot released via the
    normal failure path); a live connection / alive model service
    process is evidence, never a veto (process alive ≠ responding); a
    slow generation that keeps producing events is never killed; this
    is not a business task timeout."""
    text = AGENTS.read_text(encoding="utf-8").lower()
    assert "model_wait_dead" in text
    assert "model_wait" in text
    assert "600" in text
    assert "hung model request" in text
    assert "upstream_alive" in text
    assert "slow" in text
    # The no-business-timeout scope rule must stay intact next to it.
    assert "no business task timeout" in text
