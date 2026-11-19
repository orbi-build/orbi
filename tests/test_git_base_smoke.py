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
from tests.fakes.github import (
    install_broken_push, install_live_pr_gh, install_rejected_push,
    live_pr_gh,
)

import orbi.runner as runner
import orbi.delivery_push as delivery_push
import orbi.pr_head_sync as pr_head_sync
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


def _diverged_resume_scene(clone, tmp_path, monkeypatch):
    """The rejected-push leftover: the local branch carries an unpushed
    commit, the pushed head carries another. Returns both heads."""
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
    return branch, local_head, remote_head


def test_verify_pr_absorbs_a_really_diverged_pr_head(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2273: the rejected push's leftover local commit and the
    pushed head each carry a commit the other lacks. The resume merges the
    pushed head in — both sides kept, nothing reset, nothing force-pushed
    — and continues instead of blocking every later run of the Issue on
    the leftover branch."""
    branch, local_head, remote_head = _diverged_resume_scene(
        clone, tmp_path, monkeypatch,
    )
    with caplog.at_level("INFO"):
        url = pr_verification.verify_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", repo_dir=clone,
        )
    assert url == "https://github.com/owner/repo/pull/3"
    assert "pr_head_diverged" in caplog.text
    # A plain merge commit: both heads are its parents, so neither side's
    # work is lost and the closeout's push is a fast-forward again.
    merged = git(clone, "rev-parse", "HEAD")
    assert git(clone, "rev-parse", f"{merged}^1") == local_head
    assert git(clone, "rev-parse", f"{merged}^2") == remote_head
    # Nothing was pushed: the remote branch still ends at the pushed head.
    assert git(clone, "ls-remote", "--heads", "origin", branch).split()[0] == (
        remote_head
    )


def test_verify_pr_rolls_a_diverged_merge_conflict_back(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2273 failure path: the pushed head changes the same lines as
    the leftover commit. The merge is aborted (the local head is exactly
    the run's commit again) and the typed outside-change failure stops the
    run with its plain-words cause and next step."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    shared = clone / "shared.txt"
    shared.write_text("base\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base file")
    git(clone, "push", "origin", branch)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        capture_output=True, text=True, check=True,
    )
    git(other, "config", "user.email", "other@test.local")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", branch)
    (other / "shared.txt").write_text("from GitHub\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote only")
    git(other, "push", "origin", branch)
    remote_head = git(other, "rev-parse", "HEAD")

    shared.write_text("from this run\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local only")
    local_head = git(clone, "rev-parse", "HEAD")
    install_fake_gh(
        monkeypatch,
        json.dumps([{
            "url": "https://github.com/owner/repo/pull/3",
            "baseRefName": "main",
            "headRefOid": remote_head,
            "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #3\n\nPlan",
        }]),
    )

    with caplog.at_level("ERROR"), pytest.raises(
        pr_head_sync.DeliveryBranchChangedError, match="conflict",
    ) as caught:
        pr_verification.verify_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", repo_dir=clone,
        )
    assert caught.value.failure_record.reason_code == (
        "human_decision_required"
    )
    # The worktree is exactly the run's commit again, nothing half-merged.
    assert git(clone, "rev-parse", "HEAD") == local_head
    assert git(clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert "delivery_branch_merge_conflict" in caplog.text


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


# --- Issue #2273: the closeout continues from the pushed commits -----------


PR_URL_3 = "https://github.com/owner/repo/pull/3"


def install_live_pr(monkeypatch, clone: Path, branch: str) -> None:
    """Answer `gh` with the branch's open PR, its head read at call time.

    The delivery branch absorb moves the local HEAD before the push, so the
    PR's `headRefOid` must be the head of the moment (the production scene:
    the earlier session's PR is open while the restarted session finishes).
    Every git command runs for real (`tests.fakes.github`).
    """
    install_live_pr_gh(
        monkeypatch, real_run=runner.run_command, pr_url=PR_URL_3,
        branch=branch, run_id="a1b2c3d4",
        head_of=lambda: git(clone, "rev-parse", "HEAD"),
    )


def _remote_head(clone: Path, branch: str) -> str:
    return git(clone, "ls-remote", "--heads", "origin", branch).split()[0]


def test_deliver_pr_continues_on_the_commits_a_restarted_session_pushed(
        clone, monkeypatch, caplog,
):
    """Issue #2273 acceptance 1: the earlier session pushed its commit and
    the steering restart's worktree does not hold it. The closeout absorbs
    the pushed commit and pushes the restarted session's work on top of it
    instead of failing `! [rejected] ... (fetch first)`."""
    branch = "orbi/owner-repo-issue-3"
    base_sha = git(clone, "rev-parse", "origin/main")
    # Session 1: its delivery commit is on the branch.
    git(clone, "checkout", "-b", branch, base_sha)
    first_session = commit_file(clone, "session-one.txt", "fix: first session")
    git(clone, "push", "origin", branch)
    # The restarted session works from the frozen base, so its worktree
    # lacks the commit the branch already carries — the rejection scene.
    git(clone, "reset", "--hard", base_sha)
    restarted = commit_file(
        clone, "session-two.txt", "fix: restarted session",
    )
    install_live_pr(monkeypatch, clone, branch)

    with caplog.at_level("INFO"):
        url = runner.deliver_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", base_sha, issue_title="task", repo_dir=clone,
            language_code="en",
        )

    assert url == "https://github.com/owner/repo/pull/3"
    pushed = _remote_head(clone, branch)
    assert pushed == git(clone, "rev-parse", "HEAD")
    # Both sessions' work is on the delivery branch: the absorbed commit is
    # an ancestor of the pushed head, and so is the restarted session's.
    git(clone, "merge-base", "--is-ancestor", first_session, pushed)
    git(clone, "merge-base", "--is-ancestor", restarted, pushed)
    assert "delivery_branch_synced" in caplog.text


def test_deliver_pr_fast_forwards_a_delivery_branch_advanced_on_github(
        clone, tmp_path, monkeypatch, caplog,
):
    """Issue #2273: the branch moved on GitHub after this worktree pushed
    (the PR page's "Update branch" merges the base in). The closeout
    fast-forwards onto the pushed head and finishes instead of rejecting
    its own push."""
    branch = "orbi/owner-repo-issue-3"
    base_sha = git(clone, "rev-parse", "origin/main")
    git(clone, "checkout", "-b", branch, base_sha)
    commit_file(clone, "delivery.txt", "fix: delivery")
    git(clone, "push", "origin", branch)

    other = tmp_path / "other"
    advanced = _pushed_from_another_clone(
        tmp_path / "origin.git", other, branch, "Merge branch 'main'",
    )
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{advanced}^{{commit}}"], cwd=clone,
        capture_output=True, text=True,
    ).returncode != 0
    install_live_pr(monkeypatch, clone, branch)

    with caplog.at_level("INFO"):
        url = runner.deliver_pr(
            RunContext(
                run_id="a1b2c3d4", issue=3, branch=branch, worktree=clone,
                source_repo="owner/repo",
            ),
            "main", base_sha, issue_title="task", repo_dir=clone,
            language_code="en",
        )

    assert url == "https://github.com/owner/repo/pull/3"
    # A fast-forward, not a merge commit: the local head IS the pushed head.
    assert git(clone, "rev-parse", "HEAD") == advanced
    assert _remote_head(clone, branch) == advanced
    assert "delivery_branch_synced" in caplog.text


def test_absorb_leaves_a_never_pushed_delivery_branch_alone(clone, caplog):
    """Issue #2273: the first push of a delivery reads no remote branch and
    merges nothing — the ordinary closeout stays exactly as it was."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    head = commit_file(clone, "delivery.txt", "fix: delivery")
    delivery_push.absorb_pushed_commits(clone, branch, repo_dir=clone)
    assert git(clone, "rev-parse", "HEAD") == head
    assert "delivery_branch_synced" not in caplog.text


def test_absorb_keeps_an_unpushed_local_commit_on_top(clone, caplog):
    """Issue #2273: a local commit the branch does not carry yet (a killed
    session's work, the #158 scene) is the ahead side — nothing is merged
    back and the closeout pushes it as usual."""
    branch = "orbi/owner-repo-issue-3"
    git(clone, "checkout", "-b", branch)
    commit_file(clone, "delivery.txt", "fix: delivery")
    git(clone, "push", "origin", branch)
    # The pushed head IS the local head: no merge either.
    delivery_push.absorb_pushed_commits(clone, branch, repo_dir=clone)
    local_only = commit_file(clone, "later.txt", "fix: local only")
    delivery_push.absorb_pushed_commits(clone, branch, repo_dir=clone)
    assert git(clone, "rev-parse", "HEAD") == local_only
    assert "delivery_branch_synced" not in caplog.text


def test_absorb_rolls_a_conflicting_merge_back_and_stops(
        clone, tmp_path, caplog,
):
    """Issue #2273 failure path: the pushed commits change the same lines as
    this session's work. The merge is aborted (the worktree returns to the
    session's exact commit), and the typed failure names the outside
    change."""
    branch = "orbi/owner-repo-issue-3"
    base_sha = git(clone, "rev-parse", "origin/main")
    git(clone, "checkout", "-b", branch, base_sha)
    shared = clone / "shared.txt"
    shared.write_text("base\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base file")
    git(clone, "push", "origin", branch)

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        capture_output=True, text=True, check=True,
    )
    git(other, "config", "user.email", "other@test.local")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", branch)
    (other / "shared.txt").write_text("from GitHub\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote only")
    git(other, "push", "origin", branch)

    shared.write_text("from this session\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "local only")
    local_head = git(clone, "rev-parse", "HEAD")

    with caplog.at_level("ERROR"), pytest.raises(
        delivery_push.DeliveryBranchChangedError,
        match="conflict",
    ):
        delivery_push.absorb_pushed_commits(
            clone, branch, repo_dir=clone,
        )
    # The worktree is back at the session's commit with nothing half-merged.
    assert git(clone, "rev-parse", "HEAD") == local_head
    assert git(clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert "delivery_branch_merge_conflict" in caplog.text


# The captured production rejection the customer saw (Issue #2273): Git's
# only readable line is a hint, never a cause.
PUSH_REJECTED_STDERR = (
    " ! [rejected]        HEAD -> orbi/owner-repo-issue-3 (fetch first)\n"
    "error: failed to push some refs to "
    "'https://github.com/owner/repo.git'\n"
    "hint: Updates were rejected because the remote contains work that you "
    "do not\n"
    "hint: have locally. This is usually caused by another repository "
    "pushing\n"
    "hint: See the 'Note about fast-forwards' in 'git push --help' for "
    "details."
)


def test_a_rejected_push_stops_with_the_typed_outside_change(
        clone, monkeypatch, caplog,
):
    """Issue #2273 acceptance 2: a push the absorb could not prevent (the
    branch moved again in the seconds before the push) is a typed failure
    with a human-decision record — the failure report owns the plain-words
    cause and next step."""
    branch = "orbi/owner-repo-issue-3"

    install_rejected_push(monkeypatch, stderr=PUSH_REJECTED_STDERR)
    with caplog.at_level("ERROR"), pytest.raises(
        delivery_push.DeliveryBranchChangedError,
        match="changed on GitHub while this session ran",
    ) as caught:
        delivery_push.push_delivery_branch(clone, branch)
    assert caught.value.branch == branch
    assert caught.value.stderr == PUSH_REJECTED_STDERR
    assert caught.value.failure_record.reason_code == (
        "human_decision_required"
    )
    assert caught.value.failure_record.outcome == "blocked"
    assert "delivery_branch_push_rejected" in caplog.text


def test_the_next_run_continues_from_the_branch_a_rejected_push_left(
        clone, tmp_path,
):
    """Issue #2273 (follow-up): a rejected push leaves the local delivery
    branch diverged from origin with the run's unpushed commits. The next
    run of the same Issue must NOT block on that leftover branch (the
    production scene: "cannot recreate worktree from remote HEAD: local
    branch ... is ahead of or diverged"): its worktree is created from the
    leftover branch, the closeout absorbs the already-pushed commits and the
    push lands."""
    branch = "orbi/owner-repo-issue-3"
    base_sha = git(clone, "rev-parse", "origin/main")
    # The branch exists on origin at the base; a later push moved it.
    git(clone, "push", "origin", f"{base_sha}:refs/heads/{branch}")
    other = tmp_path / "other"
    pushed = _pushed_from_another_clone(
        tmp_path / "origin.git", other, branch, "fix: pushed session",
    )

    # The rejected run: a worktree that never held the pushed commit, whose
    # own commit the rejected push left on the local branch. Its worktree
    # stays behind (the sandbox keeps it), so the next run creates a second
    # worktree for the same branch.
    leftover_worktree = runner.create_worktree(
        clone, "owner/repo", 3, "0c2a716c", base_sha,
    )
    leftover = commit_file(
        leftover_worktree, "unpushed.txt", "fix: rejected session",
    )
    rejected = subprocess.run(
        ["git", "push", "origin", f"HEAD:{branch}"],
        cwd=leftover_worktree, capture_output=True, text=True, timeout=30,
    )
    assert rejected.returncode != 0, rejected
    assert "[rejected]" in rejected.stderr
    assert git(clone, "rev-parse", branch) == leftover

    # The next run of the same Issue: the leftover branch is reused instead
    # of blocking worktree creation, and the run continues on its commits.
    worktree = runner.create_worktree(
        clone, "owner/repo", 3, "a04e4cad", base_sha, existing_branch=True,
    )
    assert git(worktree, "branch", "--show-current") == branch
    assert git(worktree, "rev-parse", "HEAD") == leftover
    # Nothing was rewritten: the unpushed commit is still on the branch.
    assert git(clone, "rev-parse", branch) == leftover

    # The new session's work, then the closeout: absorb what the branch
    # already pushed, and push — on top of the earlier commits, not a
    # rejection.
    session = commit_file(worktree, "session.txt", "fix: next run")
    delivery_push.absorb_pushed_commits(worktree, branch, repo_dir=clone)
    delivery_push.push_delivery_branch(worktree, branch)

    landed = _remote_head(clone, branch)
    assert landed == git(worktree, "rev-parse", "HEAD")
    git(clone, "merge-base", "--is-ancestor", pushed, landed)
    git(clone, "merge-base", "--is-ancestor", leftover, landed)
    git(clone, "merge-base", "--is-ancestor", session, landed)


def test_a_push_failure_that_is_not_a_rejection_is_re_raised(
        clone, monkeypatch,
):
    """Issue #2273: only the advanced-remote rejection has a cause to name;
    every other push failure (a credential, a network drop) keeps its own
    classification and its own evidence."""
    branch = "orbi/owner-repo-issue-3"

    install_broken_push(
        monkeypatch, stderr="fatal: could not read from remote",
    )
    with pytest.raises(subprocess.CalledProcessError):
        delivery_push.push_delivery_branch(clone, branch)


def test_the_live_pr_fake_answers_only_the_closeout_reads():
    """The real-git scene's `gh` responder delegates git and fails fast on
    any other GitHub read, so a scene that needs one more call is a fix,
    never a silent empty answer (the fakes' one contract)."""
    run = live_pr_gh(
        lambda command, **kwargs: "git-answer", pr_url=PR_URL_3,
        branch="orbi/owner-repo-issue-3", run_id="a1b2c3d4",
        head_of=lambda: "deadbeef",
    )
    assert run(["git", "rev-parse", "HEAD"]) == "git-answer"
    payload = json.loads(run(["gh", "pr", "list", "--state", "open"]))
    assert payload[0]["headRefOid"] == "deadbeef"
    with pytest.raises(AssertionError, match="unexpected gh command"):
        run(["gh", "api", "/repos/owner/repo"])

