"""Guard the prompt contract for blocking commands (Issue #95).

Two real hangs (run cd855188 review session, run 9240f1e4 implement
session) had the same pattern: a TDD red test driving a `while True`
loop function spun at 99% CPU instead of failing fast, and an ad-hoc
verification generator waited forever on `next(g)`. The model learns to
wrap commands in `timeout` after a failure, but the contract must say
it up front. These tests fail when the rule text is removed from
`prompt.md`, `prompt_review.md` or the `AGENTS.md` TDD section, so the
contract cannot silently drift away.
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
