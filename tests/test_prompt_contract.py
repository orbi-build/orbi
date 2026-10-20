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
trigger pointless compactions, and a test command piped through `tail`
swallows the real exit code (the pipeline exits with `tail`'s 0), so a
failed run is reported as passed and the progress comment disagrees with
CI. The contract is now: task-relevant files first (Issue, AGENTS.md,
changed files + callers, related tests), a minimal post-compaction
recovery protocol from the run artifacts, a CI-failure-first test ladder
in the review session, and the test command's real exit code as the only
test result.
The key wording is locked here so it cannot be silently deleted or
weakened.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT = REPO_ROOT / "prompts" / "prompt.md"
PROMPT_REVIEW = REPO_ROOT / "prompts" / "prompt_review.md"
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
    ("loop-guard-per-test-timeout", "per-test timeout"),
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


# --- Issue #1813: the agent never edits the PR body ---------------------------

# orbi-cloud#2729 / run a60b123f: the acceptance list asked for `git grep`
# before/after results "in the PR", so the delivery agent rewrote the whole
# Runner-written PR body to include them. The rewrites dropped
# `<!-- orbi:run=a60b123f -->`; every later resume failed in the runner's
# `pr_run_marker_missing` check and the ticket went `ai-blocked` until a human
# restored the marker by hand. The cause is removed in the prompt: the PR body
# is the Runner's, and PR-bound evidence is a PR comment.
PR_BODY_ITEMS = (
    # The rule forbids editing the body outright.
    ("pr-body-no-edit", "never edit the pr body"),
    # Why: the Runner writes it once, with the run marker and `Fixes #N`.
    ("pr-body-runner-written", "the runner writes it once"),
    ("pr-body-marker", "carries the run marker"),
    # What to do instead: a PR comment, carrying the marker and run_id=.
    ("pr-body-post-comment", "post anything meant for the pr"),
    ("pr-body-evidence", "evidence, before/after output, lists"),
    ("pr-body-as-comment", "as a pr comment"),
    ("pr-body-comment-marker",
     "carries the run marker and the visible `run_id="),
)


def test_prompt_md_forbids_editing_the_pr_body():
    """Issue #1813: render the real implementer prompt (the template the
    Runner hands to Pi) with a real run id and Issue number, and assert the
    no-PR-body-edit rule is present — and that its rendered form names the
    concrete marker and `run_id=` field every other comment carries."""
    from orbi import pi_session

    rendered = pi_session.render_prompt(
        PROMPT.read_text(encoding="utf-8"),
        {"RUN_ID": "a60b123f", "ISSUE_NUMBER": "2729"},
    )
    text = " ".join(rendered.lower().split())
    missing = _missing(text, PR_BODY_ITEMS)
    assert not missing, (
        f"prompt.md does not forbid editing the PR body (Issue #1813): "
        f"{missing}"
    )
    # The rendered sentence names the real marker and the visible run_id.
    assert "<!-- orbi:run=a60b123f -->" in text
    assert "run_id=a60b123f" in text


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
    # The recovery reads the run artifacts, not the repository — in the
    # excluded run dir (Issue #302), never at the worktree root.
    ("compact-plan", ".orbi/plan.md"),
    ("compact-test-log", ".orbi/test.log"),
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

# a test command piped through `tail` exits with `tail`'s code (0):
# without this rule a failed run is disguised as a shell success and the
# progress comment disagrees with CI. The rule locks the wording in both
# prompts (Issue #180) without naming one framework (Issue #1616).
EXIT_CODE_ITEMS = (
    ("exit-section", "test evidence"),
    # The pipeline exit code is the LAST command's — the named bug.
    ("exit-pipeline-last", "exit code of the last command"),
    ("exit-tail-named", "test command piped through `tail`"),
    ("exit-test-command-code", "the test command's exit code"),
    # The forbidden pipe family is explicit.
    ("exit-no-pipe", "never pipe a test"),
    ("exit-pipe-tail", "tail"),
    ("exit-pipe-head", "head"),
    ("exit-pipe-grep", "grep"),
    # Truncation keeps the full output AND the real exit code.
    ("exit-redirect-file", "> .orbi/test.log 2>&1"),
    ("exit-record-code", "exit=$?"),
    ("exit-pipefail", "set -o pipefail"),
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
    # Step 4: the PR's GitHub Actions checks are the test authority
    # (Issue #1613) — no local full-suite evidence production.
    ("ladder-ci-authority",
     "the pr's github actions checks are the test authority"),
    ("ladder-read-checks", "read them with `gh pr checks {{pr_number}}`"),
    ("ladder-no-local-full",
     "do not run the full suite locally to produce evidence"),
    ("ladder-local-only",
     "run tests locally only to reproduce a failing check or to verify a "
     "fix you make in this session"),
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


# --- prompt_review.md: machine-checked absorb contract (Issue #877) ------------

ABSORB_CONTRACT_ITEMS = (
    ("absorb-contract", "absorb contract"),
    ("absorb-machine-checked", "machine-checked"),
    ("absorb-ancestor-check", "merge-base --is-ancestor"),
    ("absorb-findings-report",
     "attempted the absorb and abandoned it"),
    ("absorb-reason", "concrete reason"),
    ("absorb-pass-rejected", "machine-rejected"),
    ("absorb-no-unrelated", "unrelated to the merge"),
)


def test_prompt_review_md_keeps_the_absorb_contract():
    missing = _missing(_text(PROMPT_REVIEW), ABSORB_CONTRACT_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the machine-checked absorb contract "
        f"(Issue #877): {missing}"
    )


# --- Issue #1626: absorb only a CONFLICTING PR; never poll CI after push -------

# A deferred merge must not cost a second review session (Issue #1626): a head
# that is only behind is absorbed by the Runner's merge gate, so the session
# absorbs in-session only for a CONFLICTING PR and stops polling checks after
# it pushes — the Runner reads CI itself before it merges.
MERGE_DEFERRAL_PROMPT_ITEMS = (
    ("deferral-absorb-conflicting",
     "absorb `origin/{{base_branch}}` in-session only when github reports "
     "the pr"),
    ("deferral-absorb-mergeable-read",
     "gh pr view {{pr_number}} --json mergeable"),
    ("deferral-behind-gate",
     "a head that is merely behind is absorbed by the runner's merge gate"),
    ("deferral-no-poll", "poll or wait for the new head's checks"),
    ("deferral-runner-reads-checks",
     "the runner reads those checks itself before it merges"),
)


def test_prompt_review_md_absorbs_only_a_conflicting_pr_and_stops_polling():
    missing = _missing(_text(PROMPT_REVIEW), MERGE_DEFERRAL_PROMPT_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the merge-deferral contract "
        f"(Issue #1626): {missing}"
    )


def test_prompt_review_md_absorb_contract_is_conflicting_only():
    """Issue #1626: the machine-checked absorb contract is scoped to a round
    that starts CONFLICTING — a behind-only round is not armed."""
    text = _text(PROMPT_REVIEW)
    assert "a round that starts with the pr `conflicting` must end" in text
    assert ("a round that starts behind `origin/{{base_branch}}` or facing "
            "a merge conflict") not in text


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


# --- Issue #302: the orbi contract artifacts live in the excluded run dir -----

# The four dirty-gate incidents (#215/#235/#256/#301) were all orbi-owned
# artifacts landing at the worktree root. The prompts and the AGENTS.md
# TDD section now route every contract artifact into the excluded
# `.orbi/` run dir; the wording is locked here so it cannot drift back
# to the worktree root.
RUN_DIR_ITEMS = (
    ("run-dir-plan", ".orbi/plan.md"),
    ("run-dir-test-log", ".orbi/test.log"),
    ("run-dir-never-root", "never at the worktree root"),
)


def test_prompt_md_routes_the_run_artifacts_into_the_run_dir():
    missing = _missing(_text(PROMPT), RUN_DIR_ITEMS)
    assert not missing, (
        f"prompt.md is missing the .orbi/ run-dir contract (Issue #302): "
        f"{missing}"
    )


def test_prompt_review_md_reads_the_run_artifacts_from_the_run_dir():
    text = _text(PROMPT_REVIEW)
    missing = _missing(text, RUN_DIR_ITEMS[:2])
    assert not missing, (
        f"prompt_review.md is missing the .orbi/ run-dir contract "
        f"(Issue #302): {missing}"
    )


def test_agents_md_tdd_section_keeps_the_run_dir_coverage_rule():
    """Issue #302 once pinned the run-dir coverage COMMANDS inside
    AGENTS.md; Issue #910 made docs/testing.mdx their single source
    (enforced by tests/test_docs_single_source.py), so the TDD section
    keeps the run-dir RULE and the docs pointer — never the copied
    command block."""
    text = _text(CONTRACT)
    tdd = text.split("## tdd and coverage", 1)
    assert len(tdd) == 2, "AGENTS.md is missing the TDD section"
    section = tdd[1].split("## ui work", 1)[0]
    missing = _missing(section, (
        # The run-dir rule (the what) stays in the contract mirror.
        ("tdd-run-dir-rule",
         "the coverage data file lives in the excluded run dir"),
        # The commands (the how) are defined once in docs/testing.mdx.
        ("tdd-docs-pointer", "docs/testing.mdx"),
    ))
    assert not missing, (
        f"AGENTS.md TDD section is missing the run-dir coverage rule "
        f"(Issue #302): {missing}"
    )
    assert "coverage_file=.orbi/.coverage" not in section, (
        "AGENTS.md restates the coverage contract commands — they live "
        "once in docs/testing.mdx (Issue #910)"
    )


# --- Issue #878: the repeated-failure guard (read the round history) -----------

# orbi-cloud#450: the same failure text repeated across five consecutive
# review rounds (16:37/16:42/16:46/16:52/16:57 UTC), every round treating
# the wall as a first hit, until the round budget burned out into
# UnrecoverableDeliveryError / ai-blocked. The failure comments are ALREADY
# on GitHub (handle_gate_failure and the findings path write them to the
# Issue AND the PR with the round counter and the hidden run marker), so
# the guard is a reviewer-prompt contract: read the round history before the
# work, and on the same failure reason in >= 2 consecutive rounds emit the
# findings verdict naming the repetition instead of repeating the fix. The
# current Issue body is injected separately and is authoritative over this
# history, so the variable-set test below pins the template's placeholders.
REPEAT_FAILURE_GUARD_ITEMS = (
    # The guard runs BEFORE the review work starts.
    ("guard-before-work", "before starting the review work"),
    ("guard-never-walked", "never walked again"),
    # The round comments are the source: their recognizable prefix and the
    # run_id grouping (the criteria lives in the GitHub comments themselves).
    # The run id rides the HIDDEN marker (only the gate-failure shapes add
    # a visible (run_id=...) field; the findings shape carries none), so
    # the marker text is the one carrier every shape shares.
    ("guard-round-prefix", "orbi review round"),
    ("guard-run-marker", "<!-- orbi:run=<run_id> -->"),
    ("guard-run-id-grouping", "group the round comments by their"),
    # PR conversation, formal reviews, and inline comments are supplied by
    # the runner in the bounded PR feedback context; the prompt does not
    # assemble a second GitHub command.
    # The decision rule: >= 2 consecutive rounds with the same failure
    # reason (the same wall — semantic recurrence, not byte-identical text).
    ("guard-current-body", "current Issue body"),
    ("guard-criterion-revalidation", "cited acceptance criterion still exists"),
    ("guard-deleted-criterion", "criterion was deleted"),
    ("guard-changed-no-longer-applies", "finding no longer applies"),
    ("guard-same-reason", "same failure reason"),
    ("guard-two-consecutive", "two or more consecutive rounds"),
    ("guard-not-byte-identical", "not necessarily byte-identical text"),
    # A previous repetition report counts as the failure it named, so the
    # guard keeps firing instead of re-attempting the fix.
    ("guard-report-counts", "counts as the failure it named"),
    # The response: findings, not another fix attempt.
    ("guard-no-repeat-fix", "do not repeat the same fix path"),
    ("guard-findings-verdict", "the findings verdict"),
    ("guard-names-repetition", "same failure repeated in"),
    ("guard-human-judgment", "hands the decision to a human"),
    # The state contract: ai-fix-needed is kept, nothing is created or
    # closed (the runner publishes the findings).
    ("guard-fix-needed", "ai-fix-needed"),
    ("guard-creates-nothing", "create and close nothing"),
)


def test_prompt_review_md_keeps_the_repeated_failure_guard():
    prompt = _text(PROMPT_REVIEW)
    missing = _missing(prompt, REPEAT_FAILURE_GUARD_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the repeated-failure guard "
        f"(Issue #878): {missing}"
    )
    assert "the linked github issue (body and comments)" not in prompt, (
        "the reviewer must use the fresh injected Issue body rather than "
        "performing a second, nondeterministic body fetch"
    )


def test_prompt_review_md_keeps_the_variable_set_unchanged():
    # PR feedback reuses the existing bounded trusted-comment placeholder;
    # no parallel prompt channel or renderer is introduced.
    # The scan matches EVERY {{...}} token, not only uppercase names:
    # render_prompt substitutes the exact uppercase keys alone, so a
    # lowercase drift ({{head_sha}}) would ship as literal prompt text.
    found = set(re.findall(
        r"\{\{([^{}]*)\}\}",
        PROMPT_REVIEW.read_text(encoding="utf-8"),
    ))
    assert found == {
        "SOURCE_REPO", "PR_NUMBER", "PR_URL", "BASE_BRANCH", "BASE_SHA",
        "HEAD_SHA", "HEAD_REF", "ROUND", "BASE_SYNC_LOCK", "ISSUE_BODY",
        "ISSUE_COMMENTS",
    }, f"prompt_review.md prompt variables drifted: {found}"


# --- Issue #1613: test status comes from the PR's GitHub Actions checks -------

# The v0.6.12 wording (#1604/#1608) made the implementer maintain a
# `.orbi/test.log` layout (recorded tree, `>>` appends) and made the review
# ladder branch on CI presence and coverage gates. Issue #1613's direction
# correction drops that: test status comes only from the PR's GitHub Actions
# checks, the engine never parses local test output, and the prompts stop
# prescribing how or when the agent runs tests. The key sentences are locked
# here so they cannot be silently deleted.
IMPLEMENT_TEST_AUTHORITY_ITEMS = (
    ("impl-step5-ci-and-docs", "the way its ci and its docs run them"),
    ("impl-step5-local-not-evidence",
     "local runs are your own verification, not evidence the runner reads"),
    ("impl-step5-ci-authority",
     "the pr's github actions checks are the test authority"),
    ("impl-step5-no-rerun",
     "do not rerun the full suite on code you have already seen pass"),
    ("impl-step5-no-fail-commit",
     "do not commit a change whose tests you saw fail"),
)


def test_prompt_md_step_5_takes_the_test_status_from_ci():
    text = _text(PROMPT)
    steps = text.split(
        "5. verify the change with the repository's own tests", 1,
    )
    assert len(steps) == 2, "prompt.md is missing the new step 5"
    step5 = steps[1].split("6. verify the result yourself", 1)[0]
    missing = _missing(step5, IMPLEMENT_TEST_AUTHORITY_ITEMS)
    assert not missing, (
        f"prompt.md step 5 is missing the CI test-authority rule "
        f"(Issue #1613): {missing}"
    )
    # The dropped v0.6.x local-test-log wording.
    assert "git rev-parse head^{tree}" not in text
    assert "append every run with `>>`" not in text
    assert "`.orbi/test.log` is never overwritten" not in text
    assert "tests passed/failed" not in text


# The review ladder is section-scoped: `gh pr checks` already appears in step 1
# (CI failure logs first), so only a section assertion proves step 4 itself
# carries the test-authority rule rather than inheriting step 1's needle.
REVIEW_TEST_AUTHORITY_ITEMS = (
    ("review-ladder-ci-authority",
     "the pr's github actions checks are the test authority"),
    ("review-ladder-read-checks",
     "read them with `gh pr checks {{pr_number}}`"),
    ("review-ladder-no-local-full",
     "do not run the full suite locally to produce evidence"),
    ("review-ladder-local-only",
     "run tests locally only to reproduce a failing check or to verify a "
     "fix you make in this session"),
)


def test_prompt_review_md_test_ladder_takes_the_test_status_from_ci():
    text = _text(PROMPT_REVIEW)
    ladder = text.split("## test ladder", 1)
    assert len(ladder) == 2, "prompt_review.md is missing the test ladder"
    step4 = ladder[1].split("## fix in this session", 1)[0]
    missing = _missing(step4, REVIEW_TEST_AUTHORITY_ITEMS)
    assert not missing, (
        f"prompt_review.md Test ladder step 4 is missing the CI "
        f"test-authority rule (Issue #1613): {missing}"
    )
    # The dropped v0.6.x local-test-log wording, scoped to the ladder.
    assert "tiered coverage gate" not in step4
    assert "git rev-parse head^{tree}" not in step4
    assert "append (`>>`, never `>`)" not in step4


# Issue #1613: a fix committed in the review session takes the CI test
# authority on the new head — no unconditional local full-suite rerun.
REVIEW_FIX_IN_SESSION_ITEMS = (
    ("fix-related-tests", "rerun the tests related to the fix"),
    ("fix-commit", "and commit it"),
    ("fix-ci-authority", "ci on the new head is the test authority"),
)


def test_prompt_review_md_fix_in_session_takes_the_ci_test_authority():
    text = _text(PROMPT_REVIEW)
    section = text.split("## fix in this session", 1)
    assert len(section) == 2, "prompt_review.md is missing 'Fix in this session'"
    first_bullet = section[1].split("push only the task branch", 1)[0]
    missing = _missing(first_bullet, REVIEW_FIX_IN_SESSION_ITEMS)
    assert not missing, (
        f"prompt_review.md 'Fix in this session' first bullet is missing the "
        f"CI test-authority rule (Issue #1613): {missing}"
    )


# --- Issue #1617: coverage requirements come from the repository itself -------

# Issue #234 wrote orbi's OWN Python coverage policy (whole repository
# line/branch >= 95%, changed code and core paths 100%) into the SHARED prompts,
# so every customer repository was held to numbers nobody there agreed to.
# Issue #805 makes the repository's own CI/context files the test authority:
# the shared prompts must take the coverage requirement from the repository and
# must not invent a threshold.
PROMPT_COVERAGE_FROM_REPO_ITEMS = (
    ("coverage-from-repo-tdd", "use tdd"),
    ("coverage-from-repo-declared",
     "meet the coverage and test requirements the repository itself declares"),
    ("coverage-from-repo-ci", "its ci and its context files"),
    ("coverage-from-repo-context", "context files such as `agents.md`"),
    ("coverage-from-repo-no-invent", "do not invent a coverage threshold"),
)


def test_prompt_md_takes_coverage_from_the_repository():
    text = _text(PROMPT)
    missing = _missing(text, PROMPT_COVERAGE_FROM_REPO_ITEMS)
    assert not missing, (
        f"prompt.md must take coverage from the repository (Issue #1617): "
        f"{missing}"
    )


PROMPT_REVIEW_COVERAGE_FROM_REPO_ITEMS = (
    ("review-coverage-from-repo-declared",
     "tests meet the coverage and test requirements the repository itself "
     "declares"),
    ("review-coverage-from-repo-ci", "its ci and its context files"),
)


def test_prompt_review_md_takes_coverage_from_the_repository():
    text = _text(PROMPT_REVIEW)
    missing = _missing(text, PROMPT_REVIEW_COVERAGE_FROM_REPO_ITEMS)
    assert not missing, (
        f"prompt_review.md must take coverage from the repository "
        f"(Issue #1617): {missing}"
    )


# Issue #1613: the base-absorb bullet drops the unconditional local full-suite
# rerun; the PR's checks remain the test authority.
def test_prompt_review_md_absorb_bullet_drops_the_full_suite_rerun():
    text = _text(PROMPT_REVIEW)
    section = text.split("- if the head is behind the latest remote base", 1)
    assert len(section) == 2, "prompt_review.md is missing the base-absorb bullet"
    bullet = section[1].split("- absorb contract", 1)[0]
    assert "rerun the full test suite with coverage" not in bullet
    assert "resolve conflicts manually" in bullet


# --- Issue #1616: the shared test rules are framework-neutral -----------------

# The engine's general test rules apply to every repository (Issue #805), so
# they must not be written around one test framework: the red-phase rule names
# the runner's per-test timeout (not `pytest-timeout`), the exit-code rule
# names "a test command"/"the test command's exit code" (not `pytest ... | tail`
# / "the pytest exit code"), and "unit tests alone are not proof" replaces
# "Pytest alone is not proof." The positive wording is locked here and the old
# framework-specific wording must be gone, so the contract cannot drift back.

FRAMEWORK_NEUTRAL_ITEMS = (
    ("neutral-per-test-timeout", "per-test timeout"),
    ("neutral-test-command", "test command"),
    ("neutral-test-command-code", "the test command's exit code"),
    ("neutral-piped-tail", "test command piped through `tail`"),
)


def test_prompt_md_keeps_the_framework_neutral_test_rules():
    text = _text(PROMPT)
    missing = _missing(text, FRAMEWORK_NEUTRAL_ITEMS)
    assert not missing, (
        f"prompt.md is missing the framework-neutral test rules "
        f"(Issue #1616): {missing}"
    )
    assert "pytest" not in text, (
        "prompt.md still names pytest in a general test rule (Issue #1616)"
    )


def test_prompt_review_md_keeps_the_framework_neutral_test_rules():
    text = _text(PROMPT_REVIEW)
    missing = _missing(text, FRAMEWORK_NEUTRAL_ITEMS)
    assert not missing, (
        f"prompt_review.md is missing the framework-neutral test rules "
        f"(Issue #1616): {missing}"
    )
    assert "unit tests alone are not proof" in text
    assert "pytest" not in text, (
        "prompt_review.md still names pytest in a general test rule "
        "(Issue #1616)"
    )


def test_prompt_review_md_does_not_use_pytest_as_the_only_proof():
    text = _text(PROMPT_REVIEW)
    assert "unit tests alone are not proof" in text
    assert "pytest alone is not proof" not in text


def test_prompt_md_step_4_does_not_use_pytest_as_completion_evidence():
    text = _text(PROMPT)
    assert "unit tests alone are not completion evidence" in text
    assert "pytest alone is not completion evidence" not in text
