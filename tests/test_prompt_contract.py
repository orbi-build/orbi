"""Guard the prompt contract for blocking commands (Issue #95), the
KISS/LEAN minimal-implementation contract (Issue #118) and the
context/test-evidence contract (Issue #180).

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

Issue #180 adds the guard for the context and test-evidence contract:
long implement/review sessions re-read the same large context files and
trigger pointless compactions, and `pytest ... | tail` swallows the real
exit code (the pipeline exits with `tail`'s 0), so a failed run is
reported as passed and the progress comment disagrees with CI. The
contract is now: task-relevant files first (Issue, AGENTS.md, changed
files + callers, related tests), a minimal post-compaction recovery
protocol from the run artifacts, a CI-failure-first test ladder in the
review session, and the real pytest exit code as the only test result.
The key wording is locked here so it cannot be silently deleted or
weakened.
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


# --- Issue #180: narrowed context reads (both prompts) ------------------------

# The old blanket instruction ("read every configured context file, the
# target repository's AGENTS.md, README, build files, tests, and relevant
# history") forced a full-context read on EVERY run and was the main
# driver of the pointless compactions of long sessions. The contract is
# now: the task-relevant files first, the rest only when the task is
# actually about them.
NARROW_READ_ITEMS = (
    # The priority list names the task-relevant files.
    ("read-priority-issue", "github issue"),
    ("read-priority-agents", "agents.md"),
    ("read-priority-changed", "plus their callers"),
    ("read-priority-tests", "related tests"),
    # The rest is read only when the task is actually about them.
    ("read-only-when-relevant", "only when the task is actually about them"),
    # A normal Issue never requires a full repository scan.
    ("read-no-full-scan", "never requires a full repository scan"),
)


def test_prompt_md_narrows_the_context_reads():
    text = _text(PROMPT)
    missing = _missing(text, NARROW_READ_ITEMS)
    assert not missing, (
        f"prompt.md is missing the narrowed read list (Issue #180): {missing}"
    )
    # The old blanket instruction must be gone: it is what drove the
    # full-context reads and the repeated compactions.
    assert "read every configured context file" not in text


def test_prompt_review_md_narrows_the_context_reads():
    text = _text(PROMPT_REVIEW)
    missing = _missing(text, NARROW_READ_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the narrowed read list (Issue #180): "
        f"{missing}"
    )
    # The reviewer judges the diff, not the whole repository: the old
    # "README.md, build files" blanket read must be gone.
    assert "read the linked github issue, the repository agents.md, " \
        "readme.md" not in text


# --- Issue #180: post-compaction recovery protocol (both prompts) --------------

COMPACT_RECOVERY_ITEMS = (
    # The protocol names itself.
    ("compact-section", "context recovery after compaction"),
    # The recovery reads the run artifacts, not the repository.
    ("compact-plan", "plan.md"),
    ("compact-test-log", "test.log"),
    ("compact-progress", "progress comment"),
    # The implementer recovers its own plan; the reviewer recovers the
    # findings of the run.
    ("compact-findings", "findings"),
    # No full re-scan of the repository and no re-read of every context
    # file.
    ("compact-no-rescan", "do not re-scan the whole repository"),
    ("compact-no-reread", "do not re-read every context file"),
)


def test_prompt_md_keeps_the_compact_recovery_protocol():
    missing = _missing(_text(PROMPT), COMPACT_RECOVERY_ITEMS)
    assert not missing, (
        f"prompt.md is missing the compact-recovery protocol (Issue #180): "
        f"{missing}"
    )


def test_prompt_review_md_keeps_the_compact_recovery_protocol():
    missing = _missing(_text(PROMPT_REVIEW), COMPACT_RECOVERY_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the compact-recovery protocol "
        f"(Issue #180): {missing}"
    )


# --- Issue #180: the real test exit code is the result (both prompts) ----------

# `pytest ... | tail` exits with `tail`'s code (0): without this rule a
# failed run is disguised as a shell success and the progress comment
# disagrees with CI. The rule locks the wording in both prompts.
EXIT_CODE_ITEMS = (
    ("exit-section", "test evidence"),
    # The pipeline exit code is the LAST command's — the named bug.
    ("exit-pipeline-last", "exit code of the last command"),
    ("exit-tail-named", "pytest ... | tail"),
    # The forbidden pipe family is explicit.
    ("exit-no-pipe", "never pipe a test"),
    ("exit-pipe-tail", "tail"),
    ("exit-pipe-head", "head"),
    ("exit-pipe-grep", "grep"),
    # Truncation keeps the full output AND the real exit code.
    ("exit-redirect-file", "> test.log 2>&1"),
    ("exit-record-code", "exit=$?"),
    ("exit-pipefail", "set -o pipefail"),
    # test.log carries the real pytest output, never a self-declared
    # "tests passed".
    ("exit-real-output", "real pytest output"),
    ("exit-no-claim", "self-declared"),
)


def test_prompt_md_keeps_the_test_exit_code_rule():
    missing = _missing(_text(PROMPT), EXIT_CODE_ITEMS)
    assert not missing, (
        f"prompt.md is missing the test-exit-code rule (Issue #180): {missing}"
    )


def test_prompt_review_md_keeps_the_test_exit_code_rule():
    missing = _missing(_text(PROMPT_REVIEW), EXIT_CODE_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the test-exit-code rule (Issue #180): "
        f"{missing}"
    )


# --- Issue #180: the review test ladder (CI failures first) ---------------------

# The review session used to re-run the full suite repeatedly instead of
# converging on the CI failures. The ladder is: CI failure logs first,
# then the failing cases, then the related tests, then EXACTLY ONE full
# suite + coverage run before the verdict.
TEST_LADDER_ITEMS = (
    ("ladder-section", "test ladder"),
    # Step 1: the CI failure logs are read BEFORE any local test run.
    ("ladder-ci-first", "before running any local test"),
    ("ladder-pr-checks", "gh pr checks"),
    ("ladder-log-failed", "gh run view"),
    ("ladder-log-failed-flag", "--log-failed"),
    # Step 2: the failing cases are reproduced locally.
    ("ladder-failing-cases", "failing cases"),
    # Step 3: after the fix, the related tests run.
    ("ladder-related-tests", "related tests"),
    # Step 4: exactly ONE full suite + coverage run before the verdict.
    ("ladder-full-once", "exactly one full"),
    ("ladder-coverage", "coverage"),
    ("ladder-before-verdict", "before emitting the verdict"),
    # Repeated full-suite runs are the named anti-pattern.
    ("ladder-no-repeat", "do not repeat the full suite"),
)


def test_prompt_review_md_keeps_the_test_ladder():
    missing = _missing(_text(PROMPT_REVIEW), TEST_LADDER_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the test ladder (Issue #180): {missing}"
    )


# --- Issue #180: the review round keeps the same PR (bounded loop) --------------

# The reviewer keeps its in-session fix duty (Issue #82); when the fix is
# not verifiable in this round the SAME PR continues into the existing
# ai-fix-needed / next-review-session flow — never a new PR, never a
# given-up fix.
REVIEW_ROUND_ITEMS = (
    # The in-session fix duty is unchanged.
    ("round-fix-in-session", "fix them here, in this same session"),
    # Not verifiable this round -> the findings verdict, not pass.
    ("round-findings-verdict", "emit `findings`"),
    # The same PR continues; the bounded loop retries it.
    ("round-same-pr", "same pr"),
    ("round-fix-needed", "ai-fix-needed"),
    ("round-next-session", "next review session"),
    # Never a replacement PR, never a given-up fix.
    ("round-no-new-pr", "never create or close a pr"),
    ("round-no-give-up", "do not give up"),
)


def test_prompt_review_md_keeps_the_same_pr_round_behavior():
    missing = _missing(_text(PROMPT_REVIEW), REVIEW_ROUND_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the same-PR round behavior "
        f"(Issue #180): {missing}"
    )


# --- AGENTS.md (TDD section) ---------------------------------------------------

AGENTS_TDD_ITEMS = (
    ("tdd-timeout", "timeout <seconds>"),
    ("tdd-blocking", "blocking"),
    ("tdd-loop-guard", "termination guard"),
    ("tdd-while-true", "while true"),
    ("tdd-fail-fast", "fail fast"),
    # Issue #180: the real test exit code is the result — a pipe through
    # `tail`/`head`/`grep` swallows it and disguises a failed run as a
    # shell success.
    ("tdd-exit-code", "exit code of the last command"),
    ("tdd-no-pipe", "never pipe a test"),
    ("tdd-pipefail", "set -o pipefail"),
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
