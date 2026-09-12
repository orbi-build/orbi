"""Git data operations: delivery naming, worktrees, fetch, ancestor checks.

One leaf for the git data operations the Runner and the extracted modules
share (Issue #785): the stable delivery naming (`task_branch`,
`worktree_path`, `latest_run_id`), worktree creation, the base fetch that
freezes `origin/<base>`, the ancestor checks of the delivery gates, and
the base-sync lock every fetch that updates the shared remote-tracking
ref must hold (Issue #171).

The only seam is `orbi.journal.run_command` (Article 3.4); this module
imports no other `orbi` module besides `journal`.
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from orbi.journal import (
    GIT_NETWORK_TIMEOUT_SECONDS,
    LOGGER,
    run_command,
    run_git_network_command,
)

LOGGER = logging.getLogger("orbi.gitops")

BASE_SYNC_LOCK_NAME = "base-sync.lock"


class BaseSyncLockError(RuntimeError):
    """The base-sync flock could not be taken within the timeout.

    The lock orders every writer of the shared remote-tracking ref
    ``refs/remotes/origin/<base>`` and of the deployment tool env
    (Issue #171); a timeout means another Runner instance or the
    ExecStartPre preflight is syncing right now — fail fast, no retry.
    """

    def __init__(self, lock_path: Path, lock_timeout_seconds: float):
        super().__init__(
            f"could not take the base-sync lock {lock_path} "
            f"within {lock_timeout_seconds}s (another Runner "
            "instance or the ExecStartPre preflight is syncing "
            "the deployment checkout)"
        )


def task_branch(source_repo: str, number: int, run_id: str | None = None) -> str:
    """Return the stable remote delivery identity for one Issue.

    ``run_id`` remains accepted for callers and compatibility, but is not
    part of the branch identity: retries must converge on one GitHub branch
    and therefore one open PR.
    """
    return f"orbi/{source_repo.replace('/', '-')}-issue-{number}"


def worktree_path(repo_dir: Path, source_repo: str, number: int,
                  run_id: str) -> Path:
    """Task worktrees live in the configured repo's .worktrees/ directory."""
    slug = source_repo.replace("/", "-")
    return (
        repo_dir / ".worktrees"
        / f"orbi-{slug}-issue-{number}-{run_id}"
    )


def latest_run_id(repo_dir: Path, source_repo: str, number: int) -> str | None:
    """Return the run id of the newest task worktree for the issue.

    The worktree directory name carries the run id — the state the
    release state machine (Issue #98) reuses to resume the same run
    after a restart. Development runs use the stricter state-file
    discovery (`worktree_resume_scene`, Issue #219) instead.
    """
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/orbi-{slug}-issue-{number}-*"
    candidates = [
        path for path in repo_dir.glob(pattern) if path.is_dir()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return newest.name.rsplit("-", 1)[-1]


def _is_ancestor(commit: str, base: str, *, cwd: Path) -> bool:
    """Return whether *commit* is reachable from *base*.

    Git uses exit code 1 for the normal negative answer. Any other failure
    means the check itself could not be performed and must be surfaced.
    """
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", commit, base], cwd=cwd,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        return False
    return True


def stable_branch_exists(repo_dir: Path, branch: str) -> bool:
    """Return whether the stable delivery branch exists on origin."""
    raw = run_command(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=repo_dir, timeout=GIT_NETWORK_TIMEOUT_SECONDS,
    )
    return bool(raw.strip())


def create_worktree(repo_dir: Path, source_repo: str, number: int,
                    run_id: str, base_sha: str,
                    existing: Path | None = None,
                    existing_branch: bool = False,
                    branch: str | None = None) -> Path:
    """Create the task worktree from the frozen base SHA, never HEAD.

    An existing path is reused: only a resumed run (same run id after a
    process restart) reaches that state, and its worktree is the scene
    the run continues in (Issue #18). `existing` is the VERIFIED resume
    scene (Issue #219): after a repo rename the scene's path carries
    the OLD slug, so the derived path would miss it and a second
    worktree would be created — the verified scene is returned as-is.

    `branch` overrides the stable delivery branch name: an EXTERNAL
    takeover (Issue #608) checks out the contributor's own head branch,
    the identity the takeover PR is frozen on. With `existing_branch`
    the named branch is fetched and reused (a local branch is reused
    with `--force`, never a second `-b` — the exit-255 claim failure of
    Issue #608); without it the branch is created from the frozen base.

    A local branch that already exists (the orphan a SIGKILLed run
    leaves with no worktree and no remote counterpart, Issue #662) is
    reused as-is rather than re-created with `-b`: git exits 255 on an
    existing branch, which used to burn the re-claimed Issue into
    terminal `ai-blocked`.
    """
    if existing is not None and existing.is_dir():
        return existing
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        return path
    branch = branch or task_branch(source_repo, number, run_id)
    if existing_branch:
        # The branch is the delivery identity.  Fetch it, then create the
        # run-isolated worktree from its remote HEAD rather than the base.
        run_git_network_command(
            ["git", "fetch", "origin", branch], cwd=repo_dir,
        )
        local = run_command(
            ["git", "branch", "--list", branch], cwd=repo_dir,
        )
        if local.strip():
            run_command([
                "git", "worktree", "add", "--force", str(path), branch,
            ], cwd=repo_dir)
        else:
            run_command([
                "git", "worktree", "add", "-b", branch, str(path),
                f"origin/{branch}",
            ], cwd=repo_dir)
    else:
        # Issue #662 (the #655 incident): a SIGKILLed run can leave the
        # stable branch behind with no worktree and no remote counterpart
        # (a pure orphan).  `worktree add -b` cannot re-create it — git
        # exits 255 (`fatal: a branch named ... already exists`) and the
        # claim used to be burned into terminal `ai-blocked`.  The branch
        # is the delivery identity, so a local one is reused as-is; only
        # a missing branch is created from the frozen base SHA.
        local = run_command(
            ["git", "branch", "--list", branch], cwd=repo_dir,
        )
        if local.strip():
            run_command([
                "git", "worktree", "add", str(path), branch,
            ], cwd=repo_dir)
        else:
            run_command([
                "git", "worktree", "add", "-b", branch, str(path), base_sha,
            ], cwd=repo_dir)
    return path


def create_release_worktree(repo_dir: Path, source_repo: str, number: int,
                            run_id: str, release_commit: str) -> Path:
    """Prepare the release worktree at this attempt's verified commit.

    Release worktrees have no resumable session semantics.  A failed release
    may leave the stable branch checked out in an older, run-specific
    worktree, so reusing ``create_worktree(..., existing_branch=True)`` would
    incorrectly preserve that old position.  Find that worktree when it is
    registered, otherwise create one from the local stable branch (or the
    release commit), then hard-reset it to the commit whose gates just passed.
    """
    branch = task_branch(source_repo, number, run_id)
    listing = run_command(
        ["git", "worktree", "list", "--porcelain"], cwd=repo_dir,
    )
    current_path: Path | None = None
    current_branch = f"branch refs/heads/{branch}"
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        if current_branch in lines and lines:
            current_path = Path(lines[0].removeprefix("worktree "))
            break
    if current_path is None:
        local = run_command(
            ["git", "branch", "--list", branch], cwd=repo_dir,
        )
        if local.strip():
            current_path = worktree_path(
                repo_dir, source_repo, number, run_id,
            )
            run_command([
                "git", "worktree", "add", "--force", str(current_path),
                branch,
            ], cwd=repo_dir)
        else:
            current_path = create_worktree(
                repo_dir, source_repo, number, run_id, release_commit,
            )
    run_command(["git", "reset", "--hard", release_commit], cwd=current_path)
    return current_path


def base_sync_lock_path(repo_dir: Path) -> Path:
    """The lock file serializing ALL writers of the deployment base
    checkout and its tool env.

    Two timer instances may start in the same tick, so the service
    template's `ExecStartPre` wraps the fetch + fast-forward in a
    short-lived `flock` on this SAME file, and the Python-side
    checkout sync and the CLI install refresh take the same lock: the
    main worktree and the tool env are never written concurrently.
    The lock lives in the shared state dir (next to the slot files),
    never in a per-process temp dir.

    Issue #171 extended the same lock to EVERY fetch that updates the
    shared remote-tracking ref ``refs/remotes/origin/<base>``: task
    worktrees share the deployment checkout's common dir, so an
    unlocked concurrent fetch (Runner verify/gate/confirm, the Pi
    prompt-side fetch) races on that one ref and fails with
    ``cannot lock ref ... is at <X> but expected <Y>``.
    """
    return Path(repo_dir) / ".orbi" / BASE_SYNC_LOCK_NAME


def acquire_base_sync_lock(
    repo_dir: Path, lock_timeout_seconds: float,
) -> int:
    """Take the base-sync flock; fail fast when it is still held after
    the timeout. The kernel releases an flock when its holder exits,
    so a dead holder can never wedge the lock."""
    lock_path = base_sync_lock_path(repo_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + lock_timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, InterruptedError, PermissionError):
            if time.monotonic() >= deadline:
                os.close(fd)
                LOGGER.error(
                    "base_sync_lock_timeout repo_dir=%s lock=%s "
                    "timeout_seconds=%s",
                    repo_dir, lock_path, lock_timeout_seconds,
                )
                raise BaseSyncLockError(lock_path, lock_timeout_seconds) from None
            time.sleep(0.1)


def fetch_base_ref(repo_dir: Path, base_branch: str,
                   *, cwd: Path | None = None,
                   lock_timeout_seconds: float = 300.0,
                   command_runner: Callable[[list[str]], str] | None = None,
                   ) -> None:
    """Fetch ``origin/<base>`` under the base-sync lock (Issue #171).

    Every command that updates the shared remote-tracking ref
    ``refs/remotes/origin/<base>`` must run under the SAME lock in the
    deployment checkout's shared state dir (the one the ExecStartPre
    flock and ``sync_base_checkout`` use): worktrees share the common
    dir, so an unlocked concurrent fetch races on the ref and fails
    the session. ``repo_dir`` is the deployment checkout (the lock
    location); ``cwd`` is where the fetch runs (the task worktree for
    the Runner verify/gate/confirm paths, the checkout itself by
    default). ``command_runner`` overrides the command executor (the
    setup entry injects its own); by default the module's
    ``run_command`` is used. A lock timeout or a fetch error fails
    fast — no retry, no lock bypass.
    """
    if command_runner is None:
        command_runner = run_command
    fd = acquire_base_sync_lock(repo_dir, lock_timeout_seconds)
    try:
        run_git_network_command(
            ["git", "fetch", "origin", base_branch],
            cwd=cwd if cwd is not None else repo_dir,
            command_runner=command_runner,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def freeze_base(repo_dir: Path, base_branch: str) -> str:
    """Fetch the remote and freeze the exact SHA of origin/<base_branch>.

    The fetch runs under the base-sync lock (Issue #171): it updates
    the shared remote-tracking ref, so it must not race the other
    Runner/Pi fetches on that ref.
    """
    fetch_base_ref(repo_dir, base_branch)
    return run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )
