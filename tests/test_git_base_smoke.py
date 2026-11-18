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
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import git

import orbi.runner as runner
import orbi.pr_verification as pr_verification
from seam import seam
from orbi.delivery_scene import RunContext


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

    monkeypatch.setattr(seam, "run_command", fake_run)


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
    assert git(path, "branch", "--show-current") == "orbi/owner-repo-issue-3"


def test_retries_share_the_stable_delivery_branch(clone):
    assert runner.task_branch("owner/repo", 3, "run1") == (
        "orbi/owner-repo-issue-3"
    )
    assert runner.task_branch("owner/repo", 3, "run2") == (
        "orbi/owner-repo-issue-3"
    )


def test_verify_pr_rejects_delivery_behind_latest_remote_base(clone, caplog):
    # Delivery branch is based on the first commit only.
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-3",
        git(clone, "rev-parse", "origin/main~1"))
    commit_file(clone, "delivery.txt", "delivery")
    # Remote main advances after the delivery was created.
    git(clone, "checkout", "main")
    commit_file(clone, "advance.txt", "main advanced")
    git(clone, "push", "origin", "main")
    # The delivery worktree stays on the task branch.
    git(clone, "checkout", "orbi/owner-repo-issue-3")

    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        pr_verification.verify_pr(RunContext(run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3", worktree=clone, source_repo="owner/repo"), "main", repo_dir=clone)
    assert "base_branch=main" in caplog.text


def test_verify_pr_accepts_delivery_that_contains_latest_remote_base(clone, monkeypatch):
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-3")
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
                "<!-- orbi:run=a1b2c3d4 -->\n\n"
                "Fixes #3\n\nPlan"
            ),
        }]),
    )
    assert pr_verification.verify_pr(RunContext(run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3", worktree=clone, source_repo="owner/repo"), "main", repo_dir=clone) == "https://github.com/owner/repo/pull/3"


def test_verify_pr_passes_through_when_local_head_ahead_of_pr_head(
        clone, monkeypatch, caplog,
):
    """Issue #50 (the #158 `d13b0c56` scene, real git): commit A is
    pushed (the PR points at it), commit B is local only (a killed
    session never pushed it). The verification must NOT fail — it logs
    the exact heads (local ahead of the PR head) and returns the PR URL
    so the next review session can push the task branch on the same
    PR (no replacement PR, no discarded commit, no force push)."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-3")
    commit_file(clone, "delivery-a.txt", "commit A")
    pushed_head = git(clone, "rev-parse", "HEAD")
    commit_file(clone, "delivery-b.txt", "commit B")
    local_head = git(clone, "rev-parse", "HEAD")
    assert local_head != pushed_head
    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": pushed_head,
            "body": (
                "<!-- orbi:run=a1b2c3d4 -->\n\n"
                "Fixes #3\n\nPlan"
            ),
        }]),
    )
    with caplog.at_level("INFO"):
        url = pr_verification.verify_pr(RunContext(run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3", worktree=clone, source_repo="owner/repo"), "main", repo_dir=clone)
    assert url == "https://github.com/owner/repo/pull/3"
    assert "local_head_ahead_of_pr_head" in caplog.text
    assert f"pr_head={pushed_head}" in caplog.text
    assert f"local_head={local_head}" in caplog.text


def test_create_worktree_rebuilds_a_deleted_worktree_from_the_remote_branch(clone):
    """Issue #807: the worktree is a local cache of the remote delivery
    state. Deleting an in-flight delivery's worktree directory loses
    only the cache: the branch still on origin rebuilds it — from
    origin/<branch> when no local branch survived (a fresh sandbox
    clone), as-is from the surviving local branch after a plain
    deletion (which leaves the stale missing-but-registered entry), and
    from origin/<branch> again when only that stale entry remains."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    commit_file(clone, "delivery.txt", "delivery")
    git(clone, "push", "origin", branch)
    origin_head = git(clone, "rev-parse", f"origin/{branch}")

    def rebuild() -> Path:
        return runner.create_worktree(
            clone, "owner/repo", 3, "run1", "base123",
            existing_branch=True,
        )

    # Fresh-clone scene: no local branch, the worktree is built at the
    # remote head.
    path = rebuild()
    assert git(path, "rev-parse", "HEAD") == origin_head
    assert git(path, "branch", "--show-current") == branch

    # Plain-deletion scene: the directory is removed; the local branch
    # and the stale registration survive and the worktree is rebuilt
    # as-is from the branch.
    shutil.rmtree(path)
    rebuilt = rebuild()
    assert rebuilt == path
    assert git(rebuilt, "branch", "--show-current") == branch
    assert git(rebuilt, "rev-parse", "HEAD") == origin_head

    # Sandbox-rebuild-with-stale-state scene: the local branch is gone
    # too and only the stale registration remains — the rebuild clears
    # it and checks the branch out from origin/<branch>.
    shutil.rmtree(path)
    git(clone, "update-ref", "-d", f"refs/heads/{branch}")
    rebuilt_again = rebuild()
    assert git(rebuilt_again, "branch", "--show-current") == branch
    assert git(rebuilt_again, "rev-parse", "HEAD") == origin_head


def _pushed_from_another_clone(origin: Path, other: Path, branch: str,
                               commit_message: str) -> str:
    """Commit on *branch* in a second clone and push it to *origin*.

    The delivery checkout never fetches it, so the commit stays outside
    its local object store — exactly the GitHub side state after the PR
    page's "Update branch" (or any push made outside this worktree).
    """
    subprocess.run(
        ["git", "clone", str(origin), str(other)],
        capture_output=True, text=True, check=True,
    )
    git(other, "config", "user.email", "button@test.local")
    git(other, "config", "user.name", "Update branch")
    git(other, "checkout", branch)
    head = commit_file(other, "advance.txt", commit_message)
    git(other, "push", "origin", branch)
    return head


def test_verify_pr_fast_forwards_when_the_pr_head_is_ahead_of_local(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2261: GitHub's "Update branch" merges the base into the
    delivery branch, so the PR head is a merge commit on top of the
    delivery head. The resume must fast-forward the local branch to the
    PR head and continue — including when the merge commit was never
    fetched into this worktree (the old code crashed with exit 128 on
    the ancestry probe)."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    delivery_head = commit_file(clone, "delivery.txt", "delivery")
    git(clone, "push", "origin", branch)
    # The base moves on while the delivery is open (the state that makes
    # the PR page offer the button).
    git(clone, "checkout", "main")
    commit_file(clone, "base.txt", "main advanced")
    git(clone, "push", "origin", "main")
    git(clone, "checkout", branch)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        capture_output=True, text=True, check=True,
    )
    git(other, "config", "user.email", "button@test.local")
    git(other, "config", "user.name", "Update branch")
    git(other, "checkout", branch)
    git(other, "merge", "--no-ff", "-m",
        f"Merge branch 'main' into {branch}", "origin/main")
    pr_head = git(other, "rev-parse", "HEAD")
    git(other, "push", "origin", branch)
    # The "Update branch" shape: the merge commit's first parent is the
    # delivery's own head.
    assert git(other, "rev-parse", f"{pr_head}^1") == delivery_head
    # The commit is NOT in the delivery checkout before the resume.
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{pr_head}^{{commit}}"], cwd=clone,
        capture_output=True, text=True,
    ).returncode != 0

    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": pr_head,
            "headRepository": {"name": "repo"},
            "headRepositoryOwner": {"login": "owner"},
            "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #3\n\nPlan",
        }]),
    )
    with caplog.at_level("INFO"):
        # The resume's exact parameters: being behind the base is the
        # expected state the review session absorbs, and the PR is the
        # recovered scene's.
        url = pr_verification.verify_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", repo_dir=clone, pr_repo="owner/repo",
            expected_url="https://github.com/owner/repo/pull/3",
            require_latest_base=False,
        )
    assert url == "https://github.com/owner/repo/pull/3"
    assert git(clone, "rev-parse", "HEAD") == pr_head
    assert "pr_head_fast_forwarded" in caplog.text


def test_verify_pr_still_rejects_a_really_diverged_pr_head(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2261 keeps the old behaviour for a REAL divergence: the
    remote head and the local head each carry a commit the other lacks.
    The resume stops with `pr_head_diverged`, the local head is not
    moved and nothing is pushed (no force push, ever)."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    delivery_head = commit_file(clone, "delivery.txt", "delivery")
    git(clone, "push", "origin", branch)
    local_head = commit_file(clone, "local-only.txt", "local only")

    other = tmp_path / "other"
    remote_head = _pushed_from_another_clone(
        tmp_path / "origin.git", other, branch, "remote only",
    )
    assert remote_head != local_head
    assert git(other, "rev-parse", f"{remote_head}^") == delivery_head

    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": remote_head,
            "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #3\n\nPlan",
        }]),
    )
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="diverged"):
        pr_verification.verify_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", repo_dir=clone,
        )
    assert "pr_head_diverged" in caplog.text
    # Nothing moved, and the remote branch still ends at the foreign head.
    assert git(clone, "rev-parse", "HEAD") == local_head
    assert git(clone, "ls-remote", "--heads", "origin", branch).split()[0] == (
        remote_head
    )


def test_verify_pr_stops_when_the_pr_head_was_rewritten_away(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2261, the out-of-scope scene kept a clean stop: the PR
    names a head the remote branch no longer carries (it was
    force-pushed away). The fetch cannot produce the commit, so the
    resume must say so instead of crashing on a missing object — and it
    still never force pushes."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    delivery_head = commit_file(clone, "delivery.txt", "delivery")
    git(clone, "push", "origin", branch)

    other = tmp_path / "other"
    rewritten_head = _pushed_from_another_clone(
        tmp_path / "origin.git", other, branch, "later rewritten",
    )
    # The rewrite: the remote branch is pushed back to the delivery head,
    # so the commit the PR still names is unreachable from origin.
    git(other, "reset", "--hard", delivery_head)
    git(other, "push", "--force", "origin", branch)
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{rewritten_head}^{{commit}}"], cwd=clone,
        capture_output=True, text=True,
    ).returncode != 0

    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": rewritten_head,
            "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #3\n\nPlan",
        }]),
    )
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="no longer carries it",
    ):
        pr_verification.verify_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", repo_dir=clone, require_latest_base=False,
        )
    assert "pr_head_unreachable" in caplog.text
    assert git(clone, "rev-parse", "HEAD") == delivery_head
