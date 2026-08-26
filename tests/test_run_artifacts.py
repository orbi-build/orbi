"""Run artifacts stay out of version control (Issue #80 review round 1).

`plan.md` and `test.log` are per-run artifacts written into the task
worktree (prompt.md steps 2 and 5). A worktree is created from the
frozen base SHA (`git worktree add ... <base_sha>`): if these files
were tracked in the base, every new worktree would inherit the
PREVIOUS run's plan and test log, and a run that did not overwrite
them would post the previous run's results as its own `plan ready` /
`tests passed` milestones. They are therefore gitignored (like
`.pi-session/`), and these tests guard the contract.
"""
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def test_run_artifacts_are_not_tracked_at_repository_root():
    """A tracked plan.md/test.log would be checked out into every new
    task worktree (created from the base SHA) as the previous run's
    stale artifact."""
    tracked = git("ls-files", "plan.md", "test.log")
    assert tracked == "", f"run artifacts are tracked: {tracked}"


def test_run_artifacts_are_gitignored():
    """The ignore patterns keep the artifacts out of future delivery
    commits (an accidental `git add -A` in a task worktree must not
    re-track them)."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    patterns = [
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "plan.md" in patterns
    assert "test.log" in patterns


def test_git_helper_fails_fast_on_nonzero_exit():
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git("rev-parse", "no-such-ref")
