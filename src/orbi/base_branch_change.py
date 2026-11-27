#!/usr/bin/env python3
"""A PR still in review follows the repository to its new base (Issue #2465).

The resume scene freezes the base branch the PR was opened against. A
repository's `base_branch` can move while that PR is still in review, and
Issue #2458 made that scene a stop (`base_branch_changed`, `ai-blocked`,
`decide`). Issue #2465 reverses that call with the maintainer's own decision
("a base branch is something that keeps evolving: just merge the latest
base"): the PR follows the repository — it is moved to the configured base
and that base is merged into its branch — and review and merge continue the
way they already do whenever the base moves (Issues #877/#1626/#2423). The
person is never asked to set the base back or to close the PR.

`adopt_configured_base` is the single operation both resume paths call — the
resume verification before its git/Pi work and the review round before its
session — so the two can never disagree on when a PR moves or on how. Each
caller's own PR validation (`pr_verification.verify_pr`,
`run_state._single_open_pr`) runs AFTER it and then sees the configured base.

The move is decided from GitHub's own PR state, never from the frozen scene
alone: the scene keeps the base the PR was OPENED against (it is written
once), so a later round of the same delivery reads the PR, finds it already
on the configured base, and writes nothing — a second switch is never
needed. The PR moves BEFORE the new base is merged in, so a conflicting merge
leaves the next tick on the resumed (instead of the stopped) path.

What stays exactly as it was:

- the latest base is merged in by `base_absorb.absorb_origin_base` — the ONE
  plain merge plus plain push every base-move path uses, never a force push,
  never a resolved conflict;
- a conflict while merging aborts the merge, leaves the head untouched and
  raises `base_absorb.BaseMergeConflict`, the SAME failure the behind-base
  path raises, so the delivery follows the existing fix loop;
- a configured base branch that does not exist keeps its own
  `base_branch_missing` failure (`gitops.fetch_base_ref` decides it);
- a PR that is no longer OPEN is never retargeted and never merged into: an
  already-decided PR (merged or closed) cannot follow a base, and the
  caller's own closed-PR routing — the merged closeout, the withdrawn
  takeover — owns that state.

It lives in its own module because a module recorded by the size ratchet may
only shrink and `failure`, `failure_report` and `review_merge` are at their
ceilings (`tools/size_ratchet.py` — the same reason `terminal_stops` and
`provider_balance` exist). It imports no `review_merge` and nothing imports
it back, so there is no cycle.
"""
from __future__ import annotations

from pathlib import Path

from orbi import base_absorb, run_state
from orbi.github import pr_number, pr_view, run_gh_write_command
from orbi.gitops import fetch_base_ref
from orbi.journal import run_command


def adopt_configured_base(
    worktree: Path, *, branch: str, source_repo: str, frozen,
    configured_base_branch: str, repo_dir: Path,
) -> bool:
    """Move the delivery PR to the configured base and merge that base in.

    `frozen` is the recovered scene (`orbi.scene.Scene`): its `base_branch` is
    the base the PR was opened against. Returns True when this call moved the
    PR, False when there was nothing to do (the scene already names the
    configured base, the PR is already on it — the idempotent case of a
    delivery's second and later rounds, which then keeps going — or the PR is
    no longer OPEN).
    """
    if frozen.base_branch == configured_base_branch:
        return False
    number = pr_number(frozen.pr_url)
    current = pr_view(
        number, "state,baseRefName", repo=source_repo, cwd=worktree,
    )
    if current.get("state") != "OPEN":
        # A decided PR is not a PR still in review: a merged one refuses a
        # base change outright (the resume then looped on that refusal
        # instead of recording the merge), and retargeting a withdrawn PR
        # would merge a base into a branch nothing delivers any more. The
        # caller's closed routing owns it.
        return False
    if current.get("baseRefName") == configured_base_branch:
        return False
    # The base branch the repository does not have keeps its own failure:
    # `fetch_base_ref` decides `base_branch_missing` at the source.
    fetch_base_ref(repo_dir, configured_base_branch, cwd=worktree)
    # Move the PR FIRST: a conflicting merge below then leaves the PR
    # already on the configured base, so the next tick continues in the
    # normal fix loop instead of repeating the move.
    run_gh_write_command(
        ["gh", "pr", "edit", str(number), "--repo", source_repo,
         "--base", configured_base_branch],
        cwd=worktree,
    )
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    absorbed, _base_sha = base_absorb.absorb_origin_base(
        worktree, configured_base_branch, branch, pr=number, head=head,
    )
    # This tick's own push joins the engine push history (Issue #833), so
    # the merge record never counts the base merge as an outside push.
    run_state.record_pushed_head(worktree, absorbed)
    return True
