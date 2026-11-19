#!/usr/bin/env python3
"""Reconcile the local delivery branch with the PR head GitHub reports.

`pr_verification.verify_pr` requires the PR head to be the local HEAD.
Two differences are RECOVERABLE and both continue the delivery:

- the local HEAD is AHEAD of the PR head — a commit made by a killed
  session (implementer or reviewer) that was never pushed. The local
  commit, branch, worktree and PR stay intact; the journal records
  `local_head_ahead_of_pr_head` and the next review session pushes the
  task branch on the same PR (prompts/prompt_review.md).
- the PR head is AHEAD of the local HEAD — someone advanced the PR
  branch on GitHub (most commonly the PR page's "Update branch", which
  merges the base into the branch). The remote head is a descendant of
  the local head, so the delivery branch is FAST-FORWARDED to it
  (Issue #2261).

A head that is neither an ancestor nor a descendant is a divergence:
each side carries commits the other lacks — most often the leftover local
commits of a push Git rejected, against the head the earlier session
pushed (Issue #2273). It is ABSORBED with a plain merge (both sides kept)
so the delivery continues instead of blocking every later run of the
Issue on the leftover branch; a file conflict is rolled back and stops
with the carried plain-words failure. Nothing here ever force pushes,
resets or rewrites a commit.

This module imports no `runner` (CONSTITUTION Article 3.3) and calls no
`subprocess` directly (Article 3.4): every git command goes through the
one seam in `orbi.journal`.
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

from orbi.failure import Failure
from orbi.gitops import acquire_base_sync_lock, is_ancestor
from orbi.journal import event, run_command, run_git_network_command

# The base-sync lock is held only for the length of one fetch; the
# timeout matches `gitops.fetch_base_ref` (a held lock means another
# Runner or the ExecStartPre preflight is syncing right now).
BASE_SYNC_LOCK_TIMEOUT_SECONDS = 300.0


class DeliveryBranchChangedError(RuntimeError):
    """The delivery branch carries work this checkout could not absorb.

    Three scenes reach it: the commits GitHub already has conflict with
    this run's own (the merge was rolled back, whether the closeout or the
    resume reconciliation merged them), and the closeout's plain push is
    still rejected after the absorb — the branch moved again between the
    fetch and the push, or a repository rule refused it. All of them mean
    the branch changed outside this run, so a person decides; the failure
    report owns the reader-facing cause and next step (Issue #2273).
    """

    def __init__(self, message: str, *, branch: str,
                 stderr: str | None = None):
        super().__init__(message)
        self.branch = branch
        # Git's own output is evidence for the run details, never the
        # reader-facing reason.
        self.stderr = stderr
        # Decided at the source (#1756): the branch changed outside Orbi,
        # so the repair is a human decision, never an automatic re-queue
        # that would hit the same branch state again.
        self.failure_record = Failure(
            reason_code="human_decision_required",
            action_code="decide",
            retry_safe=False,
            outcome="blocked",
        )


def reconcile_pr_head(head_oid: str, local_head: str, *, branch: str,
                      worktree: Path, repo_dir: Path) -> None:
    """Make the local delivery branch consistent with the PR head.

    Called by `verify_pr` once the PR's head differs from the local HEAD:
    the three outcomes above are decided here, in that order, because a
    missing object must be fetched BEFORE any ancestry probe runs (a
    probe on a commit the worktree never fetched exits 128 and used to
    crash the whole tick). `repo_dir` is the deployment checkout that
    owns the base-sync lock; `worktree` is the task worktree the git
    commands run in.
    """
    if not _commit_present(head_oid, cwd=worktree):
        fetch_delivery_branch(repo_dir, branch, cwd=worktree)
        if not _commit_present(head_oid, cwd=worktree):
            event(
                "pr_head_unreachable", level=logging.ERROR,
                pr_head=head_oid, local_head=local_head, branch=branch,
            )
            raise RuntimeError(
                f"PR head {head_oid} is not in the local object store "
                f"even after fetching origin/{branch}; the remote "
                "branch no longer carries it (the branch was rewritten) "
                "and a force push is forbidden, so the resume must not "
                "continue on this branch"
            )
    if is_ancestor(head_oid, "HEAD", cwd=worktree):
        event(
            "local_head_ahead_of_pr_head", pr_head=head_oid,
            local_head=local_head, branch=branch,
        )
        return
    if not is_ancestor("HEAD", head_oid, cwd=worktree):
        # Each side carries commits the other lacks: a rejected push left
        # its own commits on the local branch while the pushed head moved
        # on (Issue #2273). A plain merge keeps both, so the run continues
        # instead of blocking every later run on the leftover branch — and
        # the closeout's push is a fast-forward again.
        event(
            "pr_head_diverged", pr_head=head_oid, local_head=local_head,
            branch=branch,
        )
        try:
            run_command(["git", "merge", head_oid], cwd=worktree)
        except subprocess.CalledProcessError as exc:
            run_command(["git", "merge", "--abort"], cwd=worktree)
            event(
                "delivery_branch_merge_conflict", level=logging.ERROR,
                branch=branch, local_head=local_head, remote_head=head_oid,
                # Git reports a merge conflict on stdout and a push
                # rejection on stderr: the event carries whichever spoke.
                output=((exc.stderr or "") + (exc.stdout or "")).strip(),
            )
            raise DeliveryBranchChangedError(
                f"the delivery branch {branch} changed on GitHub and its "
                "changes conflict with the commits this run carries; the "
                f"merge was rolled back and the local head {local_head} is "
                "unchanged",
                branch=branch, stderr=exc.stderr,
            ) from exc
        return
    # `--ff-only`: the update is a fast-forward or nothing at all, so a
    # head that moved between the ancestry probe and this line can never
    # turn into a merge commit or a rewrite.
    run_command(
        ["git", "merge", "--ff-only", head_oid], cwd=worktree,
    )
    event(
        "pr_head_fast_forwarded", pr_head=head_oid,
        previous_head=local_head, branch=branch,
    )


def _commit_present(commit: str, *, cwd: Path) -> bool:
    """Whether *commit* names a commit in the local object store.

    A task worktree only holds the objects something fetched into it, so
    a commit pushed to the delivery branch by another actor is absent
    until it is fetched. The probe is a Git object-existence check
    (`git cat-file -e`): a missing object is the normal negative answer,
    never a command failure.
    """
    probe = run_command(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=cwd, check=False, timeout=10,
    )
    return probe.returncode == 0


def fetch_delivery_branch(repo_dir: Path, branch: str, *, cwd: Path) -> None:
    """Fetch ``refs/heads/<branch>`` into its remote-tracking ref.

    The fetch updates a SHARED remote-tracking ref, so it runs under the
    base-sync lock like every other fetch. The destination is explicit: a
    bare ``git fetch origin <branch>`` writes FETCH_HEAD only and creates
    no ``refs/remotes/origin/<branch>`` when the clone's fetch refspec
    does not cover that branch (Issue #898). The closeout's delivery-branch
    absorb (`orbi.delivery_push`, Issue #2273) reads the same ref,
    so it shares this one fetch instead of a second copy.
    """
    with _base_sync_lock(repo_dir):
        run_git_network_command(
            ["git", "fetch", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=cwd,
        )


@contextmanager
def _base_sync_lock(repo_dir: Path):
    """Hold the base-sync lock the fetch's shared ref write requires."""
    fd = acquire_base_sync_lock(repo_dir, BASE_SYNC_LOCK_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
