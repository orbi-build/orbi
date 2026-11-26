"""Merge the latest base into a delivery branch (Issues #877, #1626, #2423).

A delivery branch sits behind `origin/<base>` whenever the base moved after
its head was pushed, and the engine merges that base in with ONE plain
`git merge` plus a plain push — never a force push, never a resolved
conflict. The callers are:

- the merge gate, after a clean review verdict (Issue #1626);
- the review tick, BEFORE its session, when the head's checks are red and
  the head is merely behind (`absorb_red_behind_head`, Issue #2423): a red
  check cannot pass review and the review session may not merge the base for
  a conflict-free head, so nothing else would ever absorb it — the delivery
  repeated the same red result on the same old head until it stopped;
- the closeout's pre-push absorb (`orbi.delivery_push`), which shares the
  merge primitive and differs only in its failure contract (it lets the PR
  open on the agent's head).

One operation, one entry point: this module owns the merge, the rollback and
the push. It imports no `orbi.review_merge` and no `orbi.merge_exec`
(Article 3.3) — the exception type each caller classifies stays with it.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orbi import gitops, run_state
from orbi.checks import classify_checks
from orbi.github import pr_view
from orbi.gitops import fetch_base_ref
from orbi.journal import event, run_command, run_git_network_command


@dataclass(frozen=True)
class Absorbed:
    """What the review tick's ONE PR read decided (Issues #2423, #2442).

    `head` is the absorbed head when this tick merged and pushed the base;
    `conflicting` is GitHub's CONFLICTING mergeability, the flag the
    review round arms its in-session absorb on (Issue #877). `failed` and
    `base_tip` are the PR's failing check names and the fetched base tip
    the head CONTAINS — both stay empty unless the read established a base
    commit the head actually carries, which is what the base-branch-red
    decision compares (Issue #2442).
    """

    head: str | None = None
    conflicting: bool = False
    failed: tuple[str, ...] = ()
    base_tip: str | None = None


class BaseMergeConflict(RuntimeError):
    """The base merge conflicted; it was aborted and the head is unchanged.

    The rollback and the `base_merge_conflict` journal line already
    happened. The branch and base SHA travel on the exception so the
    caller's message can name them without a second guess.
    """

    def __init__(self, base_branch: str, base_sha: str) -> None:
        super().__init__(
            f"cannot absorb origin/{base_branch} ({base_sha}); "
            "resolve the merge conflict and retry"
        )
        self.base_branch = base_branch
        self.base_sha = base_sha


def merge_origin_base(worktree: Path, base_branch: str, *,
                      base_sha: str | None = None, **fields) -> str:
    """Plain-merge `origin/<base>` into the worktree head; return that head.

    `fields` ride the journal line (`pr`, `head`, `branch`). A conflict
    rolls the worktree back to the exact commit it had with
    `git merge --abort`, logs `base_merge_conflict` and raises
    `BaseMergeConflict`; no conflict is ever resolved here.
    """
    try:
        run_command(["git", "merge", f"origin/{base_branch}"], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        run_command(["git", "merge", "--abort"], cwd=worktree)
        conflict_sha = base_sha or run_command(
            ["git", "rev-parse", f"origin/{base_branch}"], cwd=worktree,
        )
        event(
            "base_merge_conflict", level=logging.ERROR,
            base_branch=base_branch, base_sha=conflict_sha,
            returncode=exc.returncode,
            output=((exc.stderr or "") + (exc.stdout or "")).strip(),
            **fields,
        )
        raise BaseMergeConflict(base_branch, conflict_sha) from None
    return run_command(["git", "rev-parse", "HEAD"], cwd=worktree)


def absorb_origin_base(worktree: Path, base_branch: str, head_ref: str, *,
                       pr: int, head: str) -> tuple[str, str]:
    """Merge the latest base into the head and push it.

    Returns `(absorbed head, base SHA)`. The push is a plain
    `git push origin HEAD:<head_ref>` (never a force push) and the remote
    head is read back, so a push that did not land is a loud failure
    instead of a silent defer.
    """
    base_sha = run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=worktree,
    )
    merged = merge_origin_base(
        worktree, base_branch, base_sha=base_sha, pr=pr, head=head,
    )
    run_git_network_command(
        ["git", "push", "origin", f"HEAD:{head_ref}"], cwd=worktree,
    )
    remote_head = gitops.remote_branch_head(head_ref, cwd=worktree)
    if remote_head != merged:
        raise RuntimeError(
            f"remote head {remote_head} does not match absorbed head "
            f"{merged} after push origin {head_ref}"
        )
    event(
        "base_absorbed", base_branch=base_branch, base_sha=base_sha,
        pr=pr, old_head=head, head=merged,
    )
    return merged, base_sha


def absorb_red_behind_head(
    worktree: Path, pr: dict, base_branch: str, *, repo_dir: Path,
) -> Absorbed:
    """Absorb the base into a red, behind-only, conflict-free PR head.

    Issue #2423: ONE condition set — the PR is OPEN on the frozen head,
    GitHub reports it MERGEABLE (never CONFLICTING), its rollup holds at
    least one failure, and the head does not contain `origin/<base>`. Such
    a head can never pass review, so the review tick merges and pushes the
    base BEFORE any session runs and the next tick reviews the new head.
    Nothing here counts a round, posts a comment or writes a label.

    The ONE PR read (state, mergeability, head, rollup) also answers the
    review tick's CONFLICTING arm, so the tick keeps a single read; an
    unreadable answer authorizes no absorb, exactly as before. Every other
    case — a moved head, no failing check, a fresh head — returns a record
    with no absorbed head, and the caller decides from the answer: with a
    base tip the head contains, the failing names are the base-branch-red
    comparison's input (Issue #2442); without one it reviews as it always
    did. `BaseMergeConflict` propagates: the merge was rolled back and
    nothing was pushed.
    """
    try:
        state = pr_view(
            pr["number"], "state,mergeable,headRefOid,statusCheckRollup",
            cwd=worktree,
        )
    except (subprocess.CalledProcessError, ValueError, TypeError):
        return Absorbed()
    conflicting = state.get("mergeable") == "CONFLICTING"
    if (conflicting or state.get("state") != "OPEN"
            or state.get("headRefOid") != pr["head_oid"]):
        return Absorbed(conflicting=conflicting)
    _pending, failed, _passed = classify_checks(
        state.get("statusCheckRollup") or [],
    )
    if not failed:
        return Absorbed()
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    failed_names = tuple(entry["name"] for entry in failed)
    if run_state.assess_base_freshness(
        worktree, base_branch, head=pr["head_oid"],
    ) is not run_state.BaseFreshness.ABSORBABLE:
        # FRESH (a CONFLICTING head was refused above): the head already
        # contains the fetched base tip, so that tip is the base commit
        # this red PR is compared with (Issue #2442).
        return Absorbed(failed=failed_names, base_tip=run_command(
            ["git", "rev-parse", f"origin/{base_branch}"], cwd=worktree,
        ))
    head, base_sha = absorb_origin_base(
        worktree, base_branch, pr["head_ref"],
        pr=pr["number"], head=pr["head_oid"],
    )
    # This tick's own push joins the engine push history (Issue #833), so
    # the merge record never counts the base merge as an outside commit.
    run_state.record_pushed_head(worktree, head)
    event(
        "review_base_absorbed", pr=pr["number"], base_branch=base_branch,
        head=head, failed=", ".join(failed_names),
    )
    return Absorbed(
        head=head, failed=failed_names, base_tip=base_sha,
    )
