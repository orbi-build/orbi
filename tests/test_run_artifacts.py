"""Run artifacts stay out of version control (Issue #80 review round 1).

`plan.md`, `test.log` and `verify.md` are per-run artifacts written
into the task worktree (prompt.md steps 2 and 5). A worktree is created from the
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
    assert "verify.md" in patterns


def test_git_helper_fails_fast_on_nonzero_exit():
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git("rev-parse", "no-such-ref")


def test_pi_loop_state_is_gitignored():
    """Issue #215: the pi-loop plugin writes `.pi/loops.json` into the
    task worktree cwd at session shutdown (the #214 scene: `deliver_pr`
    fail-fasted on `?? .pi/`). It is not part of the agent's commit
    boundary, so the ignore rules must keep it out of
    `git status --porcelain` — and `.pi-session/` stays ignored."""
    # `git check-ignore -v` prints `<source>:<line>:<pattern>\t<path>`
    # for an ignored path (exit 0); the git() helper fails fast on any
    # other outcome, so reaching the assertions IS the "ignored" proof.
    out = git("check-ignore", "-v", ".pi/loops.json")
    assert out.splitlines()[0].endswith("\t.pi/loops.json")
    # The matching pattern is the `.pi/` directory rule in .gitignore —
    # not a coincidental substring of another pattern.
    assert out.splitlines()[0].split("\t")[0].rsplit(":", 1)[-1] == ".pi/"
    # `.pi-session/` remains ignored as before.
    out2 = git("check-ignore", "-v", ".pi-session/sess.jsonl")
    assert out2.splitlines()[0].split("\t")[0].rsplit(":", 1)[-1] == ".pi-session/"


def test_worktree_with_only_pi_loop_state_is_clean_for_status(tmp_path):
    """Issue #215 acceptance: a worktree whose only untracked entry is
    the pi-loop state (`.pi/loops.json`) is clean for
    `git status --porcelain` — the `deliver_pr` dirty-worktree gate
    sees nothing. The repo's real `.gitignore` is inherited, exactly
    like a new worktree created from the base. The gate is NOT
    weakened: any other untracked file is still reported."""
    repo = tmp_path / "wt"
    repo.mkdir()

    def git_in(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed rc={result.returncode} "
                f"stdout={result.stdout.strip()} "
                f"stderr={result.stderr.strip()}"
            )
        return result.stdout.strip()

    git_in("init", "-q")
    git_in("config", "user.email", "pilot@test.local")
    git_in("config", "user.name", "Pilot")
    (repo / ".gitignore").write_text(
        (REPO_ROOT / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # The base commit carries the tracked .gitignore — exactly the
    # state of a new worktree created from the base SHA.
    git_in("add", ".gitignore")
    git_in("commit", "-q", "-m", "base")
    (repo / ".pi").mkdir()
    (repo / ".pi" / "loops.json").write_text('{"loops": []}\n', encoding="utf-8")
    # Only the pi-loop state is untracked: the gate sees a clean tree.
    assert git_in("status", "--porcelain") == ""
    # Another untracked file is still reported: the dirty-worktree
    # gate is not weakened for real leftovers.
    (repo / "junk.txt").write_text("x", encoding="utf-8")
    assert git_in("status", "--porcelain") == "?? junk.txt"
    # The local helper fails fast on a nonzero git exit (same contract
    # as the module-level git() helper).
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git_in("rev-parse", "no-such-ref")
