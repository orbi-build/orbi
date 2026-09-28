#!/usr/bin/env python3
"""Bounded remote delivery-branch reclamation (Issue #1479).

A merged delivery leaves its `orbi/<owner>-<repo>-issue-<n>` branch on the
remote forever; on 2026-09-28 `orbi-build/orbi` carried 255 such branches.
Issue #1478 stops NEW ones at merge time; this tick-start pass drains the
BACKLOG. It lives in its own module (not `runner.py`) because the source
size ratchet froze `runner.py`'s line count (Article 3): `runner` imports
`reclaim_merged_delivery_branches` and calls it beside
`reclaim_released_worktrees`. `delete_remote_branch` is the single
`gh api -X DELETE` helper the merge-time cleanup (#1478) shares.

The pass is a pure bypass: a read failure deletes nothing and a single
delete failure warns and continues — it never fails the tick.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from orbi import config as config_domain
from orbi.journal import GIT_NETWORK_TIMEOUT_SECONDS, event, run_command

# Remote delivery-branch reclamation: the tick-start pass deletes at most
# this many merged-PR `orbi/*` branches per tick, so a large backlog (255
# branches on 2026-09-28) drains over ticks and one tick never spends
# unbounded time on `gh api` deletes. The same bound as the worktree pass.
BRANCH_RECLAIM_MAX_PER_TICK = 25


def delete_remote_branch(source_repo: str, branch: str) -> None:
    """Delete one remote branch through the GitHub REST API.

    `gh api -X DELETE repos/<repo>/git/refs/heads/<ref>` is the single
    delete call shared with the merge-time cleanup (#1478); the helper
    exists so the endpoint never drifts between the two passes. A failed
    DELETE raises so the caller can warn and continue with the others;
    a delete failure is always a warning, never a failed delivery.
    """
    endpoint = (
        f"repos/{source_repo}/git/refs/heads/{quote(branch, safe='/')}"
    )
    run_command(
        ["gh", "api", "-X", "DELETE", endpoint],
        timeout=GIT_NETWORK_TIMEOUT_SECONDS,
        failure_log_level=logging.WARNING,
    )


def _reference_missing(exc: subprocess.CalledProcessError) -> bool:
    """Whether a failed DELETE means the ref is already gone (404/422).

    The repository's own "Automatically delete head branches" can remove
    the head before this call; GitHub answers 404 / 422 ("Reference does
    not exist") in that case, which is the desired end state, not a
    failure.
    """
    text = " ".join(str(part or "") for part in (exc.stderr, exc.stdout))
    return re.search(
        r"reference does not exist|(?:http|status) ?(?:404|422)|not found",
        text, re.IGNORECASE,
    ) is not None


def delete_merged_delivery_branch(
    source_repo: str, pr_number: int, branch: str,
) -> None:
    """Delete a MERGED delivery PR's remote head branch (Issue #1478).

    Called right after `gh pr merge` lands. Only an `orbi/`-prefixed
    head (a branch Orbi itself created) is deleted; an external takeover
    PR's head is left to its owner. A 404/422 (the reference is already
    gone) counts as success. Any other failure emits a warning event and
    returns — the merge has already landed, so this cleanup never fails
    the delivery.
    """
    if not branch.startswith("orbi/"):
        return
    try:
        delete_remote_branch(source_repo, branch)
    except subprocess.CalledProcessError as exc:
        if not _reference_missing(exc):
            event(
                "delivery_branch_delete_failed", level=logging.WARNING,
                pr=pr_number, branch=branch,
                error=str(exc.stderr or exc.stdout or exc).strip(),
            )
            return
    except Exception as exc:
        event(
            "delivery_branch_delete_failed", level=logging.WARNING,
            pr=pr_number, branch=branch, error=str(exc).strip(),
        )
        return
    event("delivery_branch_deleted", pr=pr_number, branch=branch)


def _parse_remote_heads(raw: str) -> dict[str, str]:
    """Map `git ls-remote --heads` output to {branch: oid}."""
    heads: dict[str, str] = {}
    for line in raw.splitlines():
        oid, tab, ref = line.partition("\t")
        if not tab or not ref.startswith("refs/heads/"):
            continue
        heads[ref.removeprefix("refs/heads/")] = oid
    return heads


def _merged_pr_heads(prs: list[dict]) -> dict[str, set[str]]:
    """Map a merged-PR `gh pr list` payload to {headRefName: {headRefOid}}.

    A merged PR keeps reporting its head ref after the branch is gone, and
    one branch name can belong to several merged PRs (an Issue reopened and
    merged again), so the oids are a set.
    """
    heads: dict[str, set[str]] = {}
    for pr in prs:
        head = pr.get("headRefName")
        oid = pr.get("headRefOid")
        if isinstance(head, str) and isinstance(oid, str):
            heads.setdefault(head, set()).add(oid)
    return heads


def reclaim_merged_delivery_branches(config: config_domain.RunnerConfig) -> None:
    """Delete remote `orbi/*` branches whose delivery PR has MERGED.

    A merged delivery leaves its `orbi/<slug>-issue-<n>` branch on the
    remote forever; #1478 stops NEW ones, this pass drains the BACKLOG.
    Called at the tick start beside `reclaim_released_worktrees`:
    idempotent, bounded (at most `BRANCH_RECLAIM_MAX_PER_TICK` deletes)
    and never fatal.

    Only a branch whose CURRENT remote oid equals the `headRefOid` a
    MERGED PR reports is deleted. The delivery branch name is fixed per
    Issue, so a reopened Issue's new attempt pushes to the same name
    while the old merged PR still reports it: without the oid match the
    pass would delete the live branch and GitHub would close its PR. A
    branch whose only PR is OPEN, CLOSED-unmerged or absent is never in
    the merged map and is never touched. A read failure deletes nothing;
    one delete failure is a warning and the pass continues.
    """
    repo_dir = Path(config.repo_dir)
    source_repo = config.source_repos[0]
    try:
        raw = run_command(
            ["git", "ls-remote", "--heads", "origin", "orbi/*"],
            cwd=repo_dir, timeout=GIT_NETWORK_TIMEOUT_SECONDS,
        )
        heads = {
            branch: oid
            for branch, oid in _parse_remote_heads(raw).items()
            if branch.startswith("orbi/")
        }
        if not heads:
            return
        merged_raw = run_command(
            [
                "gh", "pr", "list", "--repo", source_repo,
                "--state", "merged",
                "--json", "number,headRefName,headRefOid",
                "--limit", "1000",
            ],
            timeout=GIT_NETWORK_TIMEOUT_SECONDS,
        )
        merged = _merged_pr_heads(json.loads(merged_raw))
    except Exception as exc:
        event(
            "branch_reclaim_failed", level=logging.WARNING,
            reason=f"{exc} (deleting nothing)",
        )
        return
    reclaimable = sorted(
        branch for branch, oid in heads.items()
        if oid in merged.get(branch, ())
    )
    deleted = 0
    for branch in reclaimable[:BRANCH_RECLAIM_MAX_PER_TICK]:
        try:
            delete_remote_branch(source_repo, branch)
        except Exception as exc:
            event(
                "branch_reclaim_failed", level=logging.WARNING,
                branch=branch, reason=exc,
            )
            continue
        deleted += 1
    if deleted:
        event("branches_reclaimed", count=deleted)
