"""Real git smoke tests for Issue #31 base-freeze behavior.

These tests execute actual git commands inside a temporary local repository
(no network, no remote push, no protected branch) and prove the acceptance
criteria:

- a task worktree is created from the latest ``origin/<base>`` even when the
  main worktree is checked out on a side branch;
- a retry of the same Issue gets a new independent branch/worktree (a new
  run id), and only a resumed run reuses its existing worktree;
- ``verify_pr`` rejects a delivery that does not contain the latest remote
  base and accepts one that does.
"""
import json
import subprocess
from pathlib import Path

import pytest

import bootstrap_runner as runner


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """Local bare origin plus a clone with two commits on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    (clone / "a.txt").write_text("a", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "first")
    (clone / "b.txt").write_text("b", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "second")
    git(clone, "push", "origin", "main")
    return clone


def commit_file(clone: Path, name: str, message: str) -> str:
    (clone / name).write_text(message, encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", message)
    return git(clone, "rev-parse", "HEAD")


def install_fake_gh(monkeypatch, pr_json: str) -> None:
    """Run real git commands; answer `gh` with a fixed JSON payload."""
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"]:
            return pr_json
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)


def test_task_created_from_latest_origin_base_when_main_worktree_on_side_branch(clone):
    # Main worktree drifts to a side branch that is not pushed anywhere.
    git(clone, "checkout", "-b", "side-branch")
    commit_file(clone, "side.txt", "side work")
    side_sha = git(clone, "rev-parse", "HEAD")
    origin_main = git(clone, "rev-parse", "origin/main")
    assert side_sha != origin_main

    base_sha = runner.freeze_base(clone, "main")
    assert base_sha == origin_main

    path = runner.create_worktree(clone, "owner/repo", 3, "run1", base_sha)
    assert path.exists()
    # The worktree HEAD is the frozen origin/main, not the side-branch HEAD.
    assert git(path, "rev-parse", "HEAD") == origin_main
    assert git(path, "branch", "--show-current") == "muyan-pilot/owner-repo-issue-3-run1"


def test_retry_of_same_issue_gets_new_independent_run(clone):
    base_sha = runner.freeze_base(clone, "main")
    first = runner.create_worktree(clone, "owner/repo", 3, "run1", base_sha)
    commit_file(first, "first-run.txt", "first run work")
    second = runner.create_worktree(clone, "owner/repo", 3, "run2", base_sha)
    assert second != first
    assert git(second, "rev-parse", "HEAD") == base_sha
    # The old scene is preserved untouched.
    assert git(first, "rev-parse", "HEAD") != base_sha
    assert (first / "first-run.txt").is_file()


def test_verify_pr_rejects_delivery_behind_latest_remote_base(clone, caplog):
    # Delivery branch is based on the first commit only.
    git(clone, "checkout", "-b", "muyan-pilot/owner-repo-issue-3-a1b2c3d4",
        git(clone, "rev-parse", "origin/main~1"))
    commit_file(clone, "delivery.txt", "delivery")
    # Remote main advances after the delivery was created.
    git(clone, "checkout", "main")
    commit_file(clone, "advance.txt", "main advanced")
    git(clone, "push", "origin", "main")
    # The delivery worktree stays on the task branch.
    git(clone, "checkout", "muyan-pilot/owner-repo-issue-3-a1b2c3d4")

    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.verify_pr(
            clone, "muyan-pilot/owner-repo-issue-3-a1b2c3d4", "main",
            "a1b2c3d4", issue=3, repo_dir=clone,
        )
    assert "base_branch=main" in caplog.text


def test_verify_pr_accepts_delivery_that_contains_latest_remote_base(clone, monkeypatch):
    git(clone, "checkout", "-b", "muyan-pilot/owner-repo-issue-3-a1b2c3d4")
    commit_file(clone, "delivery.txt", "delivery")
    # Remote main is unchanged, so the delivery contains it.
    local_head = git(clone, "rev-parse", "HEAD")
    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": local_head,
            "body": (
                "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
                "Fixes #3\n\nPlan"
            ),
        }]),
    )
    assert runner.verify_pr(
        clone, "muyan-pilot/owner-repo-issue-3-a1b2c3d4", "main",
        "a1b2c3d4", issue=3, repo_dir=clone,
    ) == "https://github.com/owner/repo/pull/3"


def test_verify_pr_rejects_pr_head_newer_than_local_head(clone, monkeypatch):
    # Commit A is pushed (the PR points at it); commit B is local only.
    git(clone, "checkout", "-b", "muyan-pilot/owner-repo-issue-3-a1b2c3d4")
    commit_file(clone, "delivery-a.txt", "commit A")
    pushed_head = git(clone, "rev-parse", "HEAD")
    commit_file(clone, "delivery-b.txt", "commit B")
    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": pushed_head,
            "body": (
                "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
                "Fixes #3\n\nPlan"
            ),
        }]),
    )
    with pytest.raises(RuntimeError, match="is not local HEAD"):
        runner.verify_pr(
            clone, "muyan-pilot/owner-repo-issue-3-a1b2c3d4", "main",
            "a1b2c3d4", issue=3, repo_dir=clone,
        )


def test_git_helper_fails_fast_on_nonzero_exit(clone):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(clone, "rev-parse", "no-such-ref")
