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
    # 2. TDD: test before implementation; assert externals against docs.
    ("tdd", "write a failing test first"),
    ("tdd-docs", "official docs"),
    # 2b. Issue granularity: one runtime outcome per Issue.
    ("issue-granularity", "issue granularity"),
    ("issue-one-outcome", "one runtime outcome"),
    ("issue-pin-root-cause", "root cause is pinned"),
    # 2c. Implement vs review: new review session after the PR.
    ("implement-then-pr", "plan, tdd, tests, push one pr"),
    ("review-after-pr", "after the pr exists"),
    ("review-new-jsonl", "new jsonl"),
    ("review-prompt-review", "prompt_review.md"),
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
    # 6. Implementer delivers through one PR; Runner merges main/master.
    ("merge-actor", "the runner is the only merge actor"),
    ("protected-main", "main"),
    ("protected-master", "master"),
    ("pr-only", "PR"),
    # 6a. PR body: must carry `Fixes #<issue-number>` pointing at the
    #     source Issue so GitHub natively closes the Issue when the PR
    #     merges into the default branch (GitHub reads body, not PR title).
    ("pr-fixes-keyword", "fixes #<issue-number>"),
    ("pr-fixes-auto-close", "closes the issue"),
    ("pr-fixes-default-branch", "default branch"),
    ("pr-fixes-title", "pr title"),
    # 6b. Base freshness: worktrees start from the frozen origin/<base> SHA,
    #     runs carry a unique run id, the base is re-fetched before the PR,
    #     delivery HEAD contains the latest remote base.
    ("base-freshness", "base freshness"),
    ("base-frozen-sha", "frozen"),
    ("base-run-id", "run id"),
    ("base-refetch", "re-fetch"),
    ("base-contains-latest", "contains the latest remote base"),
    ("base-plain-merge", "plain `git merge`"),
    # 6b2. Git transport (Issue #114): git data operations go over SSH,
    #      GitHub API operations stay on the gh token; the pre-start
    #      check fails fast without an HTTPS fallback; only the
    #      human-run setup entry migrates an HTTPS remote.
    ("git-transport", "git transport"),
    ("git-transport-ssh-url", "git@github.com:owner/repo.git"),
    ("git-transport-workflow", ".github/workflows/*.yml"),
    ("git-transport-gh-token", "`gh` token"),
    ("git-transport-no-mix", "ssh is never used as api authentication"),
    ("git-transport-preflight", "transport_check_failed"),
    ("git-transport-no-fallback", "no https fallback"),
    ("git-transport-probe", "git ls-remote"),
    ("git-transport-migration-entry", "muyan_pilot.py setup"),
    ("git-transport-migration-command", "git remote set-url origin"),
    # 6c. Run correlation: one run_id per attempt is the single
    #     end-to-end correlation id; every journal line and GitHub text of
    #     the run carries it; a run-scoped event includes a valid run id.
    ("run-single-id", "one run_id"),
    ("run-journal-prefix", "[run_id]"),
    ("run-github-marker", "muyan-pilot:run="),
    ("run-no-new-id", "no new id"),
    ("run-fail-fast", "fails fast"),
    # 7. Keep the MVP scope explicit: no extra stateful infrastructure.
    ("no-database", "database"),
    ("no-queue", "queue"),
    ("no-daemon", "daemon"),
    ("no-risk-engine", "risk engine"),
    ("no-fallback", "fallback"),
    ("no-timeout", "no business task timeout"),
    # 8. State is GitHub Issues and labels.
    ("state-issues", "github issues"),
    ("state-labels", "labels"),
    # 9. systemd schedules the tick and owns the run lifecycle.
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


def test_agents_md_does_not_require_implementer_review_fix_loop():
    """Issue #78: the implementer only does plan -> TDD -> tests -> push
    one PR; the independent review runs AFTER the PR is opened (the
    Runner's review session). The implement-phase wording must not demand
    a complete review — the old base-freshness bullet ("rerun the full
    tests and the complete review-fix loop, then push the task branch")
    made the local Pi self-review for hours (#18/#34)."""
    text = CONTRACT.read_text(encoding="utf-8").lower()
    assert "complete review" not in text
    # The implementer session is pinned to the delivery-only flow, and the
    # review is explicitly positioned after the PR exists.
    assert "plan, tdd, tests, push one pr" in text
    assert "after the pr exists" in text
