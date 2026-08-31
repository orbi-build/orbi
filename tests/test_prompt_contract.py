"""Guard the prompt contract for blocking commands (Issue #95) and the
KISS/LEAN minimal-implementation contract (Issue #118).

Two real hangs (run cd855188 review session, run 9240f1e4 implement
session) had the same pattern: a TDD red test driving a `while True`
loop function spun at 99% CPU instead of failing fast, and an ad-hoc
verification generator waited forever on `next(g)`. The model learns to
wrap commands in `timeout` after a failure, but the contract must say
it up front. These tests fail when the rule text is removed from
`prompt.md`, `prompt_review.md` or the `AGENTS.md` TDD section, so the
contract cannot silently drift away.

Issue #118 adds the same guard for the KISS/LEAN contract: with stronger
reasoning models an Issue gets expanded into extra architecture,
abstractions, state or future features, so the implementer prompt must
forbid scope expansion up front, the reviewer prompt must be able to
report it as an out-of-scope finding, and the key wording is locked
here so the constraint cannot be silently deleted or weakened.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = REPO_ROOT / "prompt.md"
PROMPT_REVIEW = REPO_ROOT / "prompt_review.md"
CONTRACT = REPO_ROOT / "AGENTS.md"


def _text(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    # Collapse whitespace so prose re-wrapping (a line break inside a
    # sentence) can never break a needle match.
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def _missing(text: str, items: tuple) -> list:
    return [name for name, needle in items if needle.lower() not in text]


# --- prompt.md (implementer) -------------------------------------------------

PROMPT_ITEMS = (
    # 1. Blocking shell commands must be wrapped in `timeout <seconds>`.
    ("blocking-section", "hard rules for blocking commands"),
    ("timeout-wrap", "timeout <seconds>"),
    ("timeout-tests", "running tests"),
    ("timeout-polling", "polling"),
    ("timeout-network", "network waits"),
    ("timeout-interactive", "interactive tools"),
    # 2. A timeout is a fix-needed signal, never ignorable noise.
    ("timeout-signal", "signal that the path needs a fix"),
    ("timeout-noise", "ignorable noise"),
    # 3. Unbounded-loop tests need a termination guard.
    ("loop-unbounded", "unbounded-loop"),
    ("loop-while-true", "while true"),
    ("loop-guard-sleep", "time.sleep"),
    ("loop-guard-cap", "iteration cap"),
    ("loop-guard-pytest-timeout", "pytest-timeout"),
    # 4. The TDD red phase must fail fast, never hang.
    ("red-fail-fast", "fail fast"),
    ("red-no-hang", "hung test"),
)


def test_prompt_md_exists_at_repository_root():
    assert PROMPT.is_file(), f"missing implementer prompt: {PROMPT}"


def test_prompt_md_keeps_the_blocking_command_rules():
    missing = _missing(_text(PROMPT), PROMPT_ITEMS)
    assert not missing, f"prompt.md is missing blocking-command rules: {missing}"


# --- prompt.md (implementer): KISS/LEAN minimal implementation (Issue #118) ---

PROMPT_KISS_ITEMS = (
    # 1. The rule names itself and pins the scope to the acceptance
    #    criteria: the smallest complete change, nothing more.
    ("kiss-section", "minimal implementation"),
    ("kiss-kiss", "kiss"),
    ("kiss-lean", "lean"),
    ("kiss-acceptance-only", "implement only the issue's acceptance criteria"),
    ("kiss-smallest", "smallest complete"),
    # 2. The forbidden expansion list is explicit.
    ("kiss-no-speculative", "speculative features"),
    ("kiss-no-abstraction", "no-benefit abstractions"),
    ("kiss-no-framework", "extra framework layers"),
    ("kiss-no-fallback", "fallback"),
    ("kiss-no-future", "future-proofing"),
    ("kiss-no-scope", "scope expansion"),
    # 3. 如无必要勿增实体: every new entity must map to an acceptance
    #    criterion (the full entity list is locked in one needle so it
    #    cannot be quietly shortened).
    ("kiss-occam", "如无必要勿增实体"),
    ("kiss-entity-mapping",
     "every new file, dependency, state, label, command and abstraction "
     "must map to an acceptance criterion"),
    # 4. When two designs both satisfy the requirements, the simpler one
    #    wins: fewer concepts, fewer files.
    ("kiss-two-designs", "when two designs both satisfy the requirements"),
    ("kiss-fewer", "fewer concepts, fewer files"),
    # 5. The MVP boundary is restated, not relaxed.
    ("kiss-mvp-boundary", "no database, queue, dag, daemon, risk engine or fallback"),
)


def test_prompt_md_keeps_the_kiss_lean_rules():
    missing = _missing(_text(PROMPT), PROMPT_KISS_ITEMS)
    assert not missing, f"prompt.md is missing KISS/LEAN rules: {missing}"


# --- prompt_review.md (reviewer) ----------------------------------------------

REVIEW_ITEMS = (
    # The review check names the rule and its severity.
    ("review-blocking", "blocking commands"),
    ("review-issue", "issue #95"),
    ("review-timeout", "timeout"),
    ("review-loop-guard", "termination guard"),
    ("review-blocker", "blocker"),
)


def test_prompt_review_md_exists_at_repository_root():
    assert PROMPT_REVIEW.is_file(), f"missing reviewer prompt: {PROMPT_REVIEW}"


def test_prompt_review_md_keeps_the_blocking_command_check():
    missing = _missing(_text(PROMPT_REVIEW), REVIEW_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the blocking-command check: {missing}"
    )


# --- prompt_review.md (reviewer): R8 scope/over-engineering (Issue #118) ------

REVIEW_KISS_ITEMS = (
    # The check names itself (R8), the Issue and the scope boundary.
    ("review-kiss-name", "out-of-scope and over-engineering"),
    ("review-kiss-issue", "issue #118"),
    ("review-kiss-acceptance", "acceptance criteria"),
    ("review-kiss-smallest", "smallest complete"),
    # The forbidden expansion list is explicit, so a diff that adds an
    # extra future feature is named as a finding, not waved through.
    ("review-kiss-speculative", "speculative feature"),
    ("review-kiss-abstraction", "no-benefit abstraction"),
    ("review-kiss-framework", "extra framework layer"),
    ("review-kiss-future", "future-proofing"),
    ("review-kiss-scope", "scope expansion"),
    # 如无必要勿增实体: an entity without an acceptance-criterion mapping
    # is a finding.
    ("review-kiss-occam", "如无必要勿增实体"),
    ("review-kiss-mapping", "map to an acceptance criterion"),
    # The simpler design is the contract.
    ("review-kiss-fewer", "fewer concepts, fewer files"),
    # Every such finding states the minimal fix direction.
    ("review-kiss-minimal-fix", "minimal fix direction"),
    ("review-kiss-shrink", "delete or shrink"),
)


def test_prompt_review_md_keeps_the_kiss_lean_check():
    missing = _missing(_text(PROMPT_REVIEW), REVIEW_KISS_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the R8 KISS/LEAN check: {missing}"
    )


# --- Issue #171: shared-ref fetches under the base-sync lock ------------------

LOCK_FETCH_ITEMS = (
    # The fetch instruction names the lock placeholder and the exact
    # command shape (flock <lock> git fetch origin <base>): the worktree
    # shares the deployment checkout's common dir, so an unlocked
    # concurrent fetch races on the shared remote-tracking ref.
    ("lock-fetch-flock",
     "flock {{base_sync_lock}} git fetch origin {{base_branch}}"),
    # A fetch error or lock timeout fails fast; no bare-fetch retry and
    # no lock bypass.
    ("lock-fetch-fail-fast", "fails fast"),
    ("lock-fetch-no-bypass", "bypass"),
)


def test_prompt_md_does_not_make_the_agent_run_the_git_github_lifecycle():
    """Issue #186: the deterministic Git/GitHub lifecycle (base fetch and
    absorb, push, PR creation) belongs to the Runner, not the agent. The
    implementer prompt must state that the Runner owns the closeout and
    must NOT carry the locked base fetch instruction or tell the agent to
    push or create the PR — the agent's job ends at the committed
    delivery. The PR body contract (`Fixes #<issue>`) stays, as the
    Runner's obligation."""
    text = _text(PROMPT)
    # The Runner owns the closeout (the contract wording the agent sees).
    assert "the runner owns" in text
    # The agent must not be told to run the lifecycle operations itself.
    assert "flock {{base_sync_lock}} git fetch origin {{base_branch}}" not in text
    assert "git push" not in text
    assert "gh pr create" not in text
    # The PR body contract still exists — as the Runner's obligation.
    assert "fixes #{{issue_number}}" in text


def test_prompt_review_md_fetches_the_base_under_the_base_sync_lock():
    missing = _missing(_text(PROMPT_REVIEW), LOCK_FETCH_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the locked base fetch (Issue #171): "
        f"{missing}"
    )


# --- AGENTS.md (TDD section) ---------------------------------------------------

AGENTS_TDD_ITEMS = (
    ("tdd-timeout", "timeout <seconds>"),
    ("tdd-blocking", "blocking"),
    ("tdd-loop-guard", "termination guard"),
    ("tdd-while-true", "while true"),
    ("tdd-fail-fast", "fail fast"),
)


def test_agents_md_tdd_section_keeps_the_blocking_command_rule():
    text = _text(CONTRACT)
    # The rule must live in the TDD section itself, not just somewhere.
    tdd = text.split("## tdd and coverage", 1)
    assert len(tdd) == 2, "AGENTS.md is missing the TDD section"
    section = tdd[1].split("## ui work", 1)[0]
    missing = _missing(section, AGENTS_TDD_ITEMS)
    assert not missing, (
        f"AGENTS.md TDD section is missing the blocking-command rule: {missing}"
    )
