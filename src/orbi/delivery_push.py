#!/usr/bin/env python3
"""Make the delivery branch pushable, then push it (Issues #186, #2273).

The closeout's push (`deliver_pr`, step 3) is a plain push and nothing else:
no force push, no reset, no rewrite. Two things outside the worktree can make
that push a non-fast-forward, and both are absorbed HERE, right before it:

- the BASE advanced while the agent worked — a plain `git merge` of
  `origin/<base>` (the pre-#186 agent contract, now the Runner's), and
- the DELIVERY BRANCH itself already carries pushed commits the worktree does
  not hold (Issue #2273): a steering correction restarts the running session
  in the same run, a branch was advanced on GitHub (the PR page's "Update
  branch"), an earlier run of the same Issue pushed. Without the absorb that
  push was rejected —

      ! [rejected] HEAD -> orbi/<owner>-<repo>-issue-<n> (fetch first)

  — and the delivery ended `ai-blocked` with nothing but Git's "Note about
  fast-forwards" hint on the Issue.

`absorb_pushed_commits` fast-forwards onto those commits (or merges them when
the two sides diverged, keeping both). A file conflict rolls the merge back
and stops with `DeliveryBranchChangedError`, as does a push rejection the
absorb could not prevent; the failure report renders that failure's plain
cause and next step (`orbi.failure_report`), so the reader never has to read
Git's hint to know what happened.

This module imports no `runner` (CONSTITUTION Article 3.3) and calls no
`subprocess` directly (Article 3.4): every git command goes through the one
seam in `orbi.journal`.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from orbi import gitops, run_state
from orbi.gitops import fetch_base_ref
from orbi.journal import event, run_command, run_git_network_command
from orbi.pr_head_sync import DeliveryBranchChangedError, fetch_delivery_branch

# Markers Git prints when a push is rejected because the remote ref moved
# (`! [rejected] ... (fetch first)`, `non-fast-forward`). They only tell that
# rejection apart from every other push failure; the reader-facing cause never
# repeats Git's hint text (Issue #2273).
_PUSH_REJECTED_MARKERS = (
    "[rejected]", "fetch first", "non-fast-forward", "updates were rejected",
)


def absorb_advanced_base(worktree: Path, base_branch: str, branch: str,
                         *, repo_dir: Path) -> None:
    """Absorb an advanced base into the task branch before the push.

    The fetch updates the shared remote-tracking ref, so it runs under the
    base-sync lock with the deployment checkout as the lock location. A
    lock timeout or a fetch error fails fast — no retry, no lock bypass.

    The base advanced while the agent worked: absorb it with a plain merge
    (the same base update the old agent prompt required). A conflict is
    rolled back: the worktree returns to the agent's exact commit boundary,
    the PR opens on the agent's head, and the existing review loop
    (ai-fix-needed -> the review session absorbs the base in-session)
    handles the rest — the state machine is unchanged.
    """
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    freshness = run_state.assess_base_freshness(
        worktree, base_branch, head="HEAD",
    )
    if freshness is run_state.BaseFreshness.FRESH:
        return
    try:
        run_command(
            ["git", "merge", f"origin/{base_branch}"], cwd=worktree,
        )
        event("base_absorbed", base_branch=base_branch, branch=branch)
    except subprocess.CalledProcessError as exc:
        run_command(["git", "merge", "--abort"], cwd=worktree)
        event(
            "base_merge_conflict", level=logging.ERROR,
            base_branch=base_branch, branch=branch,
            returncode=exc.returncode,
            stderr=(exc.stderr or "").strip(),
        )


def absorb_pushed_commits(worktree: Path, branch: str, *,
                          repo_dir: Path) -> None:
    """Make the local delivery branch carry what `origin/<branch>` has.

    Called by the closeout before its plain push. `repo_dir` is the
    deployment checkout that owns the shared base-sync lock the fetch needs.
    An unpushed branch (the first delivery's push) and a local head that
    already contains the remote head (the PR's own earlier push, a killed
    session's unpushed commit) need no network read beyond the remote-head
    probe.
    """
    remote = gitops.remote_branch_head(branch, cwd=worktree)
    if remote is None or remote == run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree):
        # Never pushed yet, or the local head IS the pushed head.
        return
    fetch_delivery_branch(repo_dir, branch, cwd=worktree)
    if gitops.is_ancestor(remote, "HEAD", cwd=worktree):
        # The local head already contains the pushed commits: a killed
        # session's unpushed commit is the ahead side, which the push
        # below carries as usual.
        return
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    try:
        # A plain merge: a fast-forward when the local branch is behind,
        # a merge commit (both sides kept) when it diverged. Never
        # `--force`, never a reset.
        run_command(["git", "merge", f"origin/{branch}"], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        run_command(["git", "merge", "--abort"], cwd=worktree)
        event(
            "delivery_branch_merge_conflict", level=logging.ERROR,
            branch=branch, local_head=local_head, remote_head=remote,
            # Git reports a merge conflict on stdout and a push rejection
            # on stderr: the event carries whichever spoke.
            output=((exc.stderr or "") + (exc.stdout or "")).strip(),
        )
        raise DeliveryBranchChangedError(
            f"the delivery branch {branch} changed on GitHub and its "
            f"changes conflict with this session's work; the merge was "
            f"rolled back and the local head {local_head} is unchanged",
            branch=branch, stderr=exc.stderr,
        ) from exc
    event(
        "delivery_branch_synced", branch=branch, previous_head=local_head,
        remote_head=remote,
    )


def push_delivery_branch(worktree: Path, branch: str) -> None:
    """Push the task branch; a rejection names the outside change it is.

    The closeout's ONE push (never a force push). The absorbs above already
    brought the remote commits in, so a rejection here means the branch
    moved again in the seconds between the fetch and this push, or a
    repository rule refused it — both are outside the run's control.
    """
    try:
        run_git_network_command(
            ["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree,
        )
    except subprocess.CalledProcessError as exc:
        if not _push_was_rejected(exc):
            raise
        event(
            "delivery_branch_push_rejected", level=logging.ERROR,
            branch=branch, returncode=exc.returncode,
            output=((exc.stderr or "") + (exc.stdout or "")).strip(),
        )
        raise DeliveryBranchChangedError(
            f"the delivery branch {branch} changed on GitHub while this "
            "session ran, so the plain push was rejected: the branch "
            "carries a commit this worktree does not",
            branch=branch, stderr=exc.stderr,
        ) from exc


def _push_was_rejected(exc: subprocess.CalledProcessError) -> bool:
    """Whether a `git push` was rejected by an advanced remote ref."""
    stderr = (exc.stderr or "").lower()
    return any(marker in stderr for marker in _PUSH_REJECTED_MARKERS)
