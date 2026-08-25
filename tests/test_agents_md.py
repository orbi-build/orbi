"""Guard the repository development contract.

`AGENTS.md` is the stable contract every local Pi bootstrap run reads before
changing code (see `prompt.md`). These tests fail when the file is missing or
when a required contract item is removed, so the contract cannot silently
drift away from the rules this repo actually runs under.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "AGENTS.md"

REQUIRED_ITEMS = (
    # 1. Read first: Issue, context files, README, relevant code.
    ("read-first", "read the GitHub Issue"),
    ("read-first-context", "context files"),
    ("read-first-readme", "README.md"),
    # 2. TDD: test before implementation.
    ("tdd", "write a failing test first"),
    # 3. 100% line and branch coverage for Python code.
    ("coverage", "100% line and branch coverage"),
    # 4. UI work: real Playwright interaction, assertions, console/network
    #    checks, screenshots.
    ("playwright", "Playwright"),
    ("playwright-assert", "assert"),
    ("playwright-console", "console"),
    ("playwright-screenshot", "screenshot"),
    # 5. Fail fast on command errors and keep the log scene.
    ("fail-fast", "fail fast"),
    ("log-scene", "log"),
    # 6. No merge, no push of main/master, deliver through PRs only.
    ("no-merge", "merge"),
    ("no-protected-push", "main"),
    ("no-protected-push-master", "master"),
    ("pr-only", "PR"),
    # 6b. Base freshness: worktrees start from the frozen origin/<base> SHA,
    #     runs carry a unique run id, the base is re-fetched before the PR,
    #     behind deliveries are rejected, no auto conflict resolution or
    #     force push.
    ("base-freshness", "base freshness"),
    ("base-frozen-sha", "frozen"),
    ("base-run-id", "run id"),
    ("base-refetch", "re-fetch"),
    ("base-reject-behind", "rejects"),
    ("base-no-auto-resolve", "auto conflict resolution"),
    ("base-no-force-push", "force push"),
    # 6c. Run correlation: one run_id per attempt is the single
    #     end-to-end correlation id; every journal line and GitHub text of
    #     the run carries it; a run-scoped event without a run id fails fast.
    ("run-single-id", "one run_id"),
    ("run-journal-prefix", "[run_id]"),
    ("run-github-marker", "muyan-pilot:run="),
    ("run-no-new-id", "no new id"),
    ("run-fail-fast", "fails fast"),
    # 7. No database, queue, daemon loop, risk engine, or fallback.
    ("no-database", "database"),
    ("no-queue", "queue"),
    ("no-daemon", "daemon"),
    ("no-risk-engine", "risk engine"),
    ("no-fallback", "fallback"),
    # 8. No business task timeout; systemd only schedules and owns the run
    #    lifecycle.
    ("no-timeout", "timeout"),
    ("systemd-scope", "systemd"),
)


def test_agents_md_exists_at_repository_root():
    assert CONTRACT.is_file(), f"missing development contract: {CONTRACT}"


def test_agents_md_keeps_every_required_contract_item():
    text = CONTRACT.read_text(encoding="utf-8").lower()
    missing = [
        name for name, needle in REQUIRED_ITEMS if needle.lower() not in text
    ]
    assert not missing, f"AGENTS.md is missing contract items: {missing}"
