"""Fake-based tests for the git operations surface (Issue #789).

These tests NEVER patch a runner-internal name — zero
``setattr(runner, ...)``. They arrange state in the
``FakeGit`` in-memory repository (a commit DAG, local + origin
branches, registered worktrees), patch only the ONE subprocess seam
(``seam.run_command``), run the real ``orbi.gitops`` public entry
points, and assert the public surface: returned paths and SHAs, the
branch a worktree was created from, the calls the adapter issued.
"""
import subprocess

import pytest

import orbi.gitops as gitops
from seam import seam

from tests.fakes.gitops import FakeGit


@pytest.fixture
def fake_git(monkeypatch, tmp_path):
    """The FakeGit wired into the ONE subprocess seam, seeded with the
    frozen base the way a deployment checkout starts (`origin/main` at
    the base commit)."""
    fake = FakeGit(tmp_path, base_branch="main")
    monkeypatch.setattr(seam, "run_command", fake)
    return fake


def test_stable_branch_exists_reads_the_remote_head(fake_git):
    fake_git.origin["orbi/owner-repo-issue-7"] = fake_git.base_sha
    assert gitops.stable_branch_exists(
        fake_git.repo_dir, "orbi/owner-repo-issue-7"
    ) is True
    assert gitops.stable_branch_exists(
        fake_git.repo_dir, "orbi/owner-repo-issue-8"
    ) is False


def test_create_worktree_creates_the_branch_from_the_frozen_base(fake_git):
    base = fake_git.base_sha
    path = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", base,
    )
    assert path == gitops.worktree_path(
        fake_git.repo_dir, "owner/repo", 7, "338f3484",
    )
    assert path.is_dir()
    branch = gitops.task_branch("owner/repo", 7)
    assert fake_git.local[branch] == base
    assert fake_git.worktrees[str(path)]["branch"] == branch
    assert fake_git.worktrees[str(path)]["head"] == base


def test_create_worktree_reuses_a_local_orphan_branch(fake_git):
    # The Issue #662 scene: a SIGKILLed run left the stable branch
    # behind with no worktree and no remote counterpart.
    branch = gitops.task_branch("owner/repo", 7)
    orphan_head = fake_git.commit([fake_git.base_sha])
    fake_git.branch(branch, orphan_head)
    path = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", fake_git.base_sha,
    )
    assert fake_git.local[branch] == orphan_head
    assert fake_git.worktrees[str(path)]["head"] == orphan_head
    assert not any(
        "-b" in command for command in fake_git.calls
    )


def test_create_worktree_returns_the_existing_verified_path(fake_git):
    existing = fake_git.repo_dir / "resumed-worktree"
    existing.mkdir()
    path = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", fake_git.base_sha,
        existing=existing,
    )
    assert path == existing
    assert fake_git.calls == []


def test_create_worktree_takes_over_an_external_branch(fake_git):
    # Issue #608: an external contributor PR is taken over on ITS OWN
    # head branch — fetched from origin, never re-created.
    head_branch = "contributor-patch"
    external_head = fake_git.commit([fake_git.base_sha])
    fake_git.origin[head_branch] = external_head
    path = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", fake_git.base_sha,
        existing_branch=True, branch=head_branch,
    )
    assert fake_git.local[head_branch] == external_head
    assert fake_git.worktrees[str(path)]["head"] == external_head
    assert any(
        command[:2] == ["git", "fetch"] for command in fake_git.calls
    )


def test_create_worktree_reuses_a_local_external_branch(fake_git):
    head_branch = "contributor-patch"
    external_head = fake_git.commit([fake_git.base_sha])
    fake_git.local[head_branch] = external_head
    fake_git.origin[head_branch] = external_head
    path = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", fake_git.base_sha,
        existing_branch=True, branch=head_branch,
    )
    # The `--force` reuse: the branch is the delivery identity, never a
    # second `-b` (the exit-255 claim failure of Issue #608).
    assert fake_git.worktrees[str(path)]["head"] == external_head
    assert fake_git.local[head_branch] == external_head
    assert not any("-b" in command for command in fake_git.calls)


def test_freeze_base_fetches_and_returns_the_remote_sha(fake_git):
    advanced = fake_git.commit([fake_git.base_sha])
    fake_git.origin["main"] = advanced
    sha = gitops.freeze_base(fake_git.repo_dir, "main")
    assert sha == advanced
    assert any(
        command[:2] == ["git", "fetch"] for command in fake_git.calls
    )
    assert any(
        command[:2] == ["git", "rev-parse"] for command in fake_git.calls
    )


def test_fetch_base_ref_fails_fast_on_a_missing_remote_branch(fake_git):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        gitops.fetch_base_ref(fake_git.repo_dir, "no-such-branch")
    assert "couldn't find remote ref" in excinfo.value.stderr


def test_is_ancestor_distinguishes_reachable_unrelated_and_unknown(
    fake_git,
):
    root = fake_git.base_sha
    linear = fake_git.commit([root])
    child = fake_git.commit([linear])
    side = fake_git.commit([root])
    merge = fake_git.commit([linear, side])  # diamond: two paths to root
    unrelated = fake_git.commit([])
    assert gitops._is_ancestor(root, child, cwd=fake_git.repo_dir) is True
    assert gitops._is_ancestor(child, root, cwd=fake_git.repo_dir) is False
    assert gitops._is_ancestor(root, merge, cwd=fake_git.repo_dir) is True
    # The diamond walk revisits `root` via both parents — the fake must
    # terminate (the seen-set) and answer False for an unrelated root.
    assert gitops._is_ancestor(unrelated, merge, cwd=fake_git.repo_dir) \
        is False
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        gitops._is_ancestor(
            "unknown-sha", child, cwd=fake_git.repo_dir,
        )
    assert excinfo.value.returncode == 128


def test_create_release_worktree_reuses_the_registered_worktree(fake_git):
    branch = gitops.task_branch("owner/repo", 7)
    first = gitops.create_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", fake_git.base_sha,
    )
    release_commit = fake_git.commit([fake_git.base_sha])
    path = gitops.create_release_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", release_commit,
    )
    assert path == first
    assert fake_git.worktrees[str(path)]["head"] == release_commit
    assert fake_git.worktrees[str(path)]["branch"] == branch
    assert any(
        command[:2] == ["git", "reset"] for command in fake_git.calls
    )


def test_create_release_worktree_reuses_a_local_branch(fake_git):
    branch = gitops.task_branch("owner/repo", 7)
    stale_head = fake_git.commit([fake_git.base_sha])
    fake_git.branch(branch, stale_head)
    release_commit = fake_git.commit([fake_git.base_sha])
    path = gitops.create_release_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", release_commit,
    )
    assert path == gitops.worktree_path(
        fake_git.repo_dir, "owner/repo", 7, "338f3484",
    )
    assert fake_git.worktrees[str(path)]["head"] == release_commit


def test_create_release_worktree_creates_fresh_from_the_release_commit(
    fake_git,
):
    release_commit = fake_git.commit([fake_git.base_sha])
    path = gitops.create_release_worktree(
        fake_git.repo_dir, "owner/repo", 7, "338f3484", release_commit,
    )
    assert fake_git.worktrees[str(path)]["head"] == release_commit
    assert fake_git.local[gitops.task_branch("owner/repo", 7)] \
        == release_commit


# --- the failure paths (fail fast, never a silent pass) ----------------------


def test_fake_git_fails_fast_on_unsupported_commands(fake_git):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "status"])
    assert "unsupported command: git status" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["bash", "-c", "echo hi"])
    assert "unsupported command" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git"])
    assert "unsupported command: git" in excinfo.value.stderr


def test_fake_git_fails_fast_on_malformed_known_verbs(fake_git):
    cases = (
        ["git", "ls-remote", "--heads", "origin"],
        ["git", "branch", "list", "main"],
        ["git", "fetch", "upstream", "main"],
        ["git", "rev-parse", "main", "main"],
        ["git", "merge-base", "c0001", "c0001"],
        ["git", "worktree", "list", "--format=json"],
        ["git", "worktree", "add"],
        ["git", "worktree", "add", "-b", "b"],
        ["git", "worktree", "add", "p1", "p2", "p3"],
        ["git", "reset", "--soft", "c0001"],
    )
    for command in cases:
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            fake_git(command)
        assert "unsupported command" in excinfo.value.stderr, command


def test_fake_git_fails_fast_on_unknown_refs(fake_git, tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "rev-parse", "origin/absent"])
    assert "ambiguous argument" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "rev-parse", "unknown-sha"])
    assert "ambiguous argument" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "worktree", "add", str(tmp_path / "w"), "absent"])
    assert "invalid reference: absent" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "worktree", "add", "-b", "fresh", "w2",
                  "not-a-commit"])
    assert "invalid reference: not-a-commit" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "worktree", "add", "-b", "fresh", "w3",
                  "origin/absent"])
    assert "invalid reference: origin/absent" in excinfo.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(["git", "reset", "--hard", "not-a-commit"], cwd=tmp_path)
    assert "ambiguous argument" in excinfo.value.stderr


def test_fake_git_fails_fast_on_reset_outside_a_worktree(fake_git,
                                                         tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git(
            ["git", "reset", "--hard", fake_git.base_sha], cwd=tmp_path,
        )
    assert "not a git repository" in excinfo.value.stderr


def test_fake_git_fails_fast_on_a_second_branch_creation(fake_git):
    # The stable branch already exists locally (the orphan a SIGKILLed
    # run leaves, or a previous attempt): `worktree add -b` must fail
    # with git's exit 255, never re-create the delivery identity.
    branch = gitops.task_branch("owner/repo", 7)
    fake_git.branch(branch, fake_git.base_sha)
    path = gitops.worktree_path(
        fake_git.repo_dir, "owner/repo", 7, "338f3484",
    )
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        fake_git([
            "git", "worktree", "add", "-b", branch, str(path),
            fake_git.base_sha,
        ])
    assert "already exists" in excinfo.value.stderr
    assert excinfo.value.returncode == 255
