"""Unit tests for the `orbi.gitops` leaf (Issue #785).

fetch / push-ordering / worktree / ancestor checks at the single
`run_command` seam: fakes patch the module seam or inject a
`command_runner`, and the git argv is asserted where the argv is the
contract. No test patches `runner` internals.
"""
import fcntl
import os
import subprocess
import time

import pytest

from orbi import gitops
from seam import seam


def test_module_is_a_leaf_and_never_imports_runner_release_pi_process():
    with open(gitops.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("from orbi.runner", "from orbi import runner",
                      "import orbi.runner", "from orbi.release",
                      "from orbi import release", "from orbi.pi_process",
                      "from orbi import pi_process"):
        assert forbidden not in source, forbidden


def test_task_branch_is_the_stable_delivery_identity():
    assert gitops.task_branch("orbi-build/orbi", 785) == \
        "orbi/orbi-build-orbi-issue-785"
    # The run id is accepted for compatibility but never part of the name:
    # retries must converge on one branch and one PR.
    assert gitops.task_branch("o/r", 4, "a1b2c3d4") == "orbi/o-r-issue-4"


def test_worktree_path_names_the_run_isolated_worktree(tmp_path):
    path = gitops.worktree_path(tmp_path, "orbi-build/orbi", 785, "a1b2c3d4")
    assert path == (
        tmp_path / ".worktrees" / "orbi-orbi-build-orbi-issue-785-a1b2c3d4"
    )


def test_latest_run_id_reads_the_newest_worktree(tmp_path):
    base = tmp_path / ".worktrees"
    (base / "orbi-o-r-issue-4-aaaaaaaa").mkdir(parents=True)
    older = base / "orbi-o-r-issue-4-aaaaaaaa"
    newer = base / "orbi-o-r-issue-4-bbbbbbbb"
    newer.mkdir()
    os.utime(older, (1, 1))
    assert gitops.latest_run_id(tmp_path, "o/r", 4) == "bbbbbbbb"

    empty = tmp_path / ".worktrees-empty"
    empty.mkdir()
    assert gitops.latest_run_id(empty.parent, "o/r", 99) is None


def test_is_ancestor_maps_exit_1_to_false(monkeypatch):
    commands = []

    def ok(command, **kwargs):
        commands.append(command)
        return ""

    def negative(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)

    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(128, command)

    # exit 0 -> True
    monkeypatch.setattr(seam, "run_command", ok)
    assert gitops._is_ancestor("c1", "c2", cwd="/w") is True
    assert commands[-1] == ["git", "merge-base", "--is-ancestor", "c1", "c2"]

    monkeypatch.setattr(seam, "run_command", negative)
    assert gitops._is_ancestor("c1", "c2", cwd="/w") is False

    monkeypatch.setattr(seam, "run_command", broken)
    with pytest.raises(subprocess.CalledProcessError):
        gitops._is_ancestor("c1", "c2", cwd="/w")


def test_stable_branch_exists_probes_the_remote_head(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return "ref\toid\n"

    monkeypatch.setattr(seam, "run_command", run)
    assert gitops.stable_branch_exists("/repo", "orbi/o-r-issue-4") is True
    command, kwargs = commands[-1]
    assert command == [
        "git", "ls-remote", "--heads", "origin",
        "refs/heads/orbi/o-r-issue-4",
    ]
    assert kwargs["cwd"] == "/repo"
    assert kwargs["timeout"] == gitops.GIT_NETWORK_TIMEOUT_SECONDS

    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    assert gitops.stable_branch_exists("/repo", "branch") is False


def test_create_worktree_from_the_frozen_base_sha(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        commands.append(c), "")[1])

    path = gitops.create_worktree(
        tmp_path, "o/r", 4, "a1b2c3d4", "base123",
    )
    assert path == tmp_path / ".worktrees" / "orbi-o-r-issue-4-a1b2c3d4"
    assert commands == [
        ["git", "branch", "--list", "orbi/o-r-issue-4"],
        ["git", "worktree", "add", "-b", "orbi/o-r-issue-4", str(path),
         "base123"],
    ]


def test_create_worktree_reuses_a_local_orphan_branch(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        commands.append(c), "orbi/o-r-issue-4")[1])

    path = gitops.create_worktree(
        tmp_path, "o/r", 4, "a1b2c3d4", "base123",
    )
    assert commands == [
        ["git", "branch", "--list", "orbi/o-r-issue-4"],
        ["git", "worktree", "add", str(path), "orbi/o-r-issue-4"],
    ]


def test_create_worktree_fetches_an_existing_remote_branch(
    monkeypatch, tmp_path,
):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return ""

    monkeypatch.setattr(seam, "run_command", run)
    path = gitops.create_worktree(
        tmp_path, "o/r", 4, "a1b2c3d4", "base123",
        existing_branch=True,
    )
    assert commands == [
        ["git", "fetch", "origin", "orbi/o-r-issue-4"],
        ["git", "branch", "--list", "orbi/o-r-issue-4"],
        ["git", "worktree", "add", "-b", "orbi/o-r-issue-4", str(path),
         "origin/orbi/o-r-issue-4"],
    ]


def test_create_worktree_returns_the_existing_path(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        commands.append(c), "")[1])
    existing = tmp_path / ".worktrees" / "orbi-o-r-issue-4-a1b2c3d4"
    existing.mkdir(parents=True)
    assert gitops.create_worktree(
        tmp_path, "o/r", 4, "a1b2c3d4", "base123", existing=existing,
    ) == existing
    assert commands == []


def test_create_release_worktree_reuses_the_registered_worktree(
    monkeypatch, tmp_path,
):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "worktree", "list"]:
            return (
                f"worktree {tmp_path / '.worktrees' / 'orbi-o-r-issue-4-deadbeef'}\n"
                "HEAD abc\nbranch refs/heads/orbi/o-r-issue-4\n\n"
            )
        return ""

    monkeypatch.setattr(seam, "run_command", run)
    path = gitops.create_release_worktree(
        tmp_path, "o/r", 4, "deadbeef", "release123",
    )
    assert path == tmp_path / ".worktrees" / "orbi-o-r-issue-4-deadbeef"
    assert commands[-1] == ["git", "reset", "--hard", "release123"]


def test_freeze_base_fetches_under_the_lock_then_reads_the_sha(
    monkeypatch, tmp_path,
):
    commands = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        commands.append(c), "abc1234")[1])

    sha = gitops.freeze_base(tmp_path, "main")
    assert sha == "abc1234"
    assert commands == [
        ["git", "fetch", "origin", "main"],
        ["git", "rev-parse", "origin/main"],
    ]
    assert (tmp_path / ".orbi" / "base-sync.lock").is_file()


def test_base_sync_lock_is_released_after_the_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    gitops.fetch_base_ref(tmp_path, "main", lock_timeout_seconds=1.0)
    # The lock is exclusive while held; after the call another flock must
    # succeed immediately.
    probe = os.open(
        str(gitops.base_sync_lock_path(tmp_path)),
        os.O_RDWR | os.O_CREAT, 0o644,
    )
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


def test_acquire_base_sync_lock_times_out_fail_fast(tmp_path):
    lock_path = gitops.base_sync_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(gitops.BaseSyncLockError, match="base-sync.lock"):
            gitops.acquire_base_sync_lock(tmp_path, 0.2)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_fetch_base_ref_runs_the_fetch_in_the_requested_cwd(
    monkeypatch, tmp_path,
):
    commands = []
    monkeypatch.setattr(seam, "run_command", lambda c, **k: (
        commands.append((c, k)), "")[1])
    worktree = tmp_path / "wt"
    worktree.mkdir()
    gitops.fetch_base_ref(tmp_path, "main", cwd=worktree)
    assert commands[-1][0] == ["git", "fetch", "origin", "main"]
    assert commands[-1][1]["cwd"] == worktree
