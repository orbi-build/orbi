#!/usr/bin/env python3
"""The delivery PR contract: `verify_pr` and its typed failures.

Every delivery is verified against ONE contract. The deliver path
(`deliver_pr` in `runner`) verifies the PR it just created; the resume
path (`resume_verification.verify_resumed_pr`) re-verifies the PR
recovered from the scene before any git/Pi mutation. Both ask the same
questions of the same facts: the task branch is checked out, the
delivery contains the latest remote base (except on the resume, where
being behind is the expected state the review session absorbs),
exactly one open PR exists for the branch, its base is the configured
base, its head is the local HEAD (a local HEAD AHEAD of the PR head is
the recoverable unpushed-commit scene; a diverged head is a failure),
its body carries a run marker of the delivery line and the
`Fixes #<issue>` keyword, and its URL is the one the scene recovered.
The typed failures live here because they ARE the outcomes of that
contract; the resume handler that routes them is
`resume_verification`, which imports this module and never the reverse.

This module imports no `runner` (CONSTITUTION Article 3.3): `runner`
calls it through the module object, so a stale `runner.<name>` patch
fails loudly instead of silently testing nothing.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from orbi import run_state
from orbi.comment_trust import line_run_markers
from orbi.delivery_scene import RunContext
from orbi.failure_report import UnrecoverableDeliveryError
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    _pr_number,
    issue_comments,
    pr_view,
    run_gh_read_command,
)
from orbi.gitops import _is_ancestor, fetch_base_ref
from orbi.journal import LOGGER, event, run_command
from orbi.progress import run_marker


class ResumeVerificationError(UnrecoverableDeliveryError):
    """A terminal resume precondition was handled and reported for this tick.

    The verification handler has already written the label and failure
    evidence. Keeping a distinct type lets the tick boundary end normally
    without swallowing unrelated Runner bugs.
    """


class ReportedMissingFixesError(RuntimeError):
    """A missing-Fixes delivery failure was successfully reported.

    Only this typed outcome is handled as a successful tick. The unwrapped
    error still means its label/comment transition failed and must propagate.
    """


class MissingFixesError(RuntimeError):
    """A delivery PR body does not carry its native-close keyword."""


class ResumePrClosedError(UnrecoverableDeliveryError):
    """The resumed delivery's scene PR is no longer open.

    Carries the scene PR's GitHub state (`CLOSED` or `MERGED`) so the
    resume handler routes the ALREADY-DECIDED fact — a merged PR
    delivered the fix, a closed external PR was withdrawn — instead of
    blocking every closed scene alike. The routing lives with the
    caller because only it knows the delivery's takeover flag; the
    typed error keeps the zero-open-PR classification testable at the
    `verify_pr` seam.
    """

    def __init__(self, message: str, *, scene_pr_state: str) -> None:
        super().__init__(message)
        self.scene_pr_state = scene_pr_state


class ResumeBranchGoneError(UnrecoverableDeliveryError):
    """The resume worktree is missing AND the delivery branch is gone
    from the remote.

    The remote state (branch + PR) is the delivery's record; with the
    branch destroyed there is nothing to recreate the local worktree
    cache from — an external precondition, terminal (`ai-blocked`).
    The typed error also tells the resume handler to drop the
    preserved-objects suffix: nothing is left to preserve.
    """


def _pr_head_repo(pr: dict) -> str:
    """Return `owner/name` of the PR head repo, or '<missing>' if absent."""
    owner = pr.get("headRepositoryOwner")
    repo = pr.get("headRepository")
    if not isinstance(owner, dict) or not isinstance(repo, dict):
        return "<missing>"
    login = owner.get("login")
    name = repo.get("name")
    if not isinstance(login, str) or not login \
            or not isinstance(name, str) or not name:
        return "<missing>"
    return f"{login}/{name}"


def verify_pr(ctx: RunContext, base_branch: str, *,
              repo_dir: Path,
              pr_repo: str | None = None,
              expected_url: str | None = None,
              require_latest_base: bool = True,
              external_pr: bool = False) -> str:
    """Verify that exactly one open PR of the task branch is the delivery.

    Checks, in order: current branch, latest remote base ancestry (unless
    `require_latest_base` is False — the resume pre-validation runs
    before the base merge, when being behind is the expected state),
    exactly one open PR for the head branch, PR base, PR head vs local
    HEAD (a local HEAD AHEAD of the PR head is the #158 unpushed-commit
    scene: it is logged and passed through — the next review
    session pushes the task branch on the same PR; a diverged head is a
    failure), the run marker in the PR body, and the `Fixes #<issue>`
    keyword in the PR body (GitHub closes the source Issue
    natively only when the body carries the keyword, so a PR without it
    would leave the Issue open after the merge). With `external_pr`

    the two body checks are skipped — an external PR body carries
    neither the run marker nor a `Fixes` keyword for this Issue; the
    Issue is closed by the Runner after the merge instead. When
    `pr_repo` is given (resume path), a Runner-owned PR's head repo must
    be that repo; an external takeover is selected by its trusted marker
    number and may have a fork head. When `expected_url` is given, the
    verified PR URL must exactly
    equal the recovered original PR URL (the resume must keep the
    same PR number). Issue #825/#1300: the marker check accepts ANY run
    marker of the delivery line — the marker set is read from the
    Issue's trusted comments when the current attempt's marker misses,
    because the PR body is written once by the creating run while a
    later attempt of the same line may bind a new run id. Both paths
    read the same source (`pr_repo` on the resume, the delivery's own
    source repo on the deliver path); the deliver path creates the PR
    only when none is open and verifies unconditionally, so a re-claim
    of an Issue whose PR an earlier run opened must not be rejected for
    owning its own PR.
    """
    worktree = ctx.worktree
    branch: str = ctx.branch
    run_id: str = ctx.run_id
    issue: int = ctx.issue
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        error_type = ResumeVerificationError if expected_url is not None else RuntimeError
        raise error_type(
            f"resume PR validation: run_id={run_id} branch={branch} "
            f"open_pr_count=unknown open_prs=[] "
            f"scene_pr={expected_url or '-'} scene_pr_state=unknown; "
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    if require_latest_base:
        # Re-fetch before judging: the delivery must contain the latest
        # remote base, otherwise it is behind and the PR is rejected
        # (fail fast). The fetch updates the shared remote-tracking
        # ref, so it runs under the base-sync lock with
        # the deployment checkout as the lock location.
        fetch_base_ref(repo_dir, base_branch, cwd=worktree)
        freshness = run_state.assess_base_freshness(
            worktree, base_branch, head="HEAD",
        )
        if freshness is not run_state.BaseFreshness.FRESH:
            event(
                "delivery_behind_base", level=logging.ERROR,
                base_branch=base_branch, branch=branch,
                freshness=freshness.value,
            )
            raise RuntimeError(
                f"delivery HEAD is behind latest remote base "
                f"origin/{base_branch}; merge the latest base, rerun full "
                "tests and review, then retry"
            )
    local_head = run_command(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
    )
    if expected_url is not None:
        # A resume cannot safely select a replacement PR, so it keeps its
        # own exactly-one policy: the FULL open list is the
        # failure audit record and zero open PRs is
        # classified against the scene PR's state.
        if external_pr:
            external = pr_view(
                _pr_number(expected_url),
                (
                    "number,url,state,baseRefName,baseRefOid,headRefName,"
                    "headRefOid,headRepository,headRepositoryOwner,body"
                ),
                repo=pr_repo, cwd=worktree,
                timeout=RESUME_PR_STATE_TIMEOUT_SECONDS,
            )
            prs = [external] if external.get("state") == "OPEN" else []
        else:
            prs = run_state._query_open_prs(worktree, branch)
        if len(prs) != 1:
            # A resume cannot safely select a replacement PR. Query the scene
            # PR separately so zero open PRs (a closed/merged or missing
            # scene PR) have a different outcome from an ambiguous branch.
            scene_state = "unknown"
            try:
                scene_pr = run_gh_read_command([
                    "gh", "pr", "view", str(_pr_number(expected_url)),
                    "--repo", pr_repo or "", "--json", "state,mergedAt",
                ], cwd=worktree, timeout=RESUME_PR_STATE_TIMEOUT_SECONDS)
                state = json.loads(scene_pr)
                if isinstance(state, dict):
                    scene_state = str(state.get("state", "unknown"))
                    if state.get("mergedAt"):
                        scene_state = "MERGED"
            except Exception:
                LOGGER.exception("resume_scene_pr_state_lookup_failed")
            evidence = (
                f"run_id={run_id} branch={branch} "
                f"open_pr_count={len(prs)} "
                f"open_prs={json.dumps(prs, sort_keys=True)} "
                f"scene_pr={expected_url} scene_pr_state={scene_state}"
            )
            if len(prs) == 0:
                if scene_state in ("CLOSED", "MERGED"):
                    event(
                        "resume_pr_closed", level=logging.ERROR,
                        issue=issue, branch=branch, pr=expected_url,
                        state=scene_state,
                    )
                    # The typed error carries the state so the resume
                    # handler routes the decided fact (delivered /
                    # withdrawn) instead of blocking (the
                    # resumed path is the ONLY path, so these scenes are
                    # normal between-ticks states).
                    raise ResumePrClosedError(
                        f"resume PR is {scene_state.lower()} and cannot be "
                        f"resumed or replaced: {evidence}",
                        scene_pr_state=scene_state,
                    )
                event(
                    "resume_pr_missing", level=logging.ERROR,
                    issue=issue, branch=branch, pr=expected_url,
                    state=scene_state,
                )
                raise ResumeVerificationError(
                    f"resume PR is not open and was not found as closed or "
                    f"merged: {evidence}; the scene must be repaired"
                )
            event(
                "resume_pr_multiple_open", level=logging.ERROR,
                issue=issue, branch=branch, count=len(prs),
            )
            raise ResumeVerificationError(
                f"resume has multiple open PRs for the task branch: {evidence}; "
                "the runner will not choose one"
            )
        pr = prs[0]
    else:
        pr = run_state._single_open_pr(
            worktree, branch, base_branch, scene="verify_pr",
        )
    url = pr.get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    if pr_repo is not None and not external_pr:
        head_repo = _pr_head_repo(pr)
        if head_repo != pr_repo:
            event(
                "pr_repo_mismatch", level=logging.ERROR,
                expected=pr_repo, actual=head_repo, branch=branch,
            )
            error_type = ResumeVerificationError if expected_url is not None else RuntimeError
            raise error_type(
                f"resume PR validation: run_id={run_id} branch={branch} "
                f"open_pr_count=1 open_prs={[url]} "
                f"scene_pr={expected_url or '-'} scene_pr_state=OPEN; "
                f"PR head repo is {head_repo}, expected {pr_repo}; the "
                "resume must keep the PR of the configured source repo"
            )
    # The non-resume base validation lives in _single_open_pr;
    # the resume keeps its typed failure with the full run evidence.
    base_ref = pr.get("baseRefName")
    if expected_url is not None and base_ref != base_branch:
        event(
            "pr_base_mismatch", level=logging.ERROR,
            scene="verify_pr_resume", expected=base_branch,
            actual=base_ref, branch=branch,
        )
        raise ResumeVerificationError(
            f"resume PR validation: run_id={run_id} branch={branch} "
            f"open_pr_count=1 open_prs={[url]} "
            f"scene_pr={expected_url} scene_pr_state=OPEN; "
            f"PR base is {base_ref}, expected {base_branch}; recreate the "
            "PR against the configured base branch"
        )
    head_oid = pr.get("headRefOid")
    if head_oid != local_head:
        # The local HEAD may be
        # AHEAD of the remote PR head — a commit made by a killed
        # session (implementer or reviewer) that was never pushed. The
        # local commit, branch, worktree and PR stay intact and the
        # state is RECOVERABLE: failing here would re-raise on every
        # tick and the review session — which pushes the task branch
        # before its verdict (prompts/prompt_review.md) — could never run. Log
        # the exact heads (the commit/push phase the journal must
        # carry) and continue the verification: the next review round
        # pushes the task branch on the same PR and the merge gate
        # re-freezes the advanced head. A remote head that is NOT an
        # ancestor of the local HEAD (diverged) is still a failure: a
        # plain push would be rejected and only a force push or a
        # human decision could continue it.
        if not _is_ancestor(head_oid, "HEAD", cwd=worktree):
            event(
                "pr_head_diverged", level=logging.ERROR,
                pr_head=head_oid, local_head=local_head, branch=branch,
            )
            raise RuntimeError(
                f"PR head {head_oid} is not local HEAD {local_head} "
                "and is not an ancestor of it (the branch diverged); "
                "a plain push would be rejected and a force push is "
                "forbidden, so the resume must not continue on this "
                "branch"
            )
        event(
            "local_head_ahead_of_pr_head", pr_head=head_oid,
            local_head=local_head, branch=branch,
        )
    marker = run_marker(run_id)
    body = pr.get("body")
    if not external_pr and (
        not isinstance(body, str) or marker not in body
    ):
        # Issue #825: a delivery may run under a NEW run id of the SAME
        # line (one recoverable failure is enough to rebind it), while the
        # PR body is written once by the creating run. The check therefore
        # accepts ANY marker the Issue's TRUSTED comment history knows.
        #
        # Issue #1300: this used to be gated on `expected_url is not None`,
        # i.e. the resume path only. But the deliver path creates the PR
        # only when none is open and verifies UNCONDITIONALLY, so a re-claim
        # of an Issue whose PR an earlier run already opened hit the strict
        # current-attempt marker and fell to `ai-blocked` — a healthy
        # delivery rejected for owning its own PR (production #914: body
        # carried `orbi:run=675f38a0`, the re-claim ran as `f70987dd`). The
        # line identity is the same on both paths, so the source is too.
        #
        # The delivery line's repo is the resume's `pr_repo` and, on the
        # deliver path (`deliver_pr` passes no `pr_repo`: the head-repo pin
        # is a resume-only check), the delivery's own source repo — the
        # Issue's repo on both paths. The trusted-only source keeps the
        # #45/#89 posture — a copied marker in a public comment widens
        # nothing — and the branch, base, head and (on resume) exact-URL
        # checks above still pin the PR.
        line_repo = pr_repo or ctx.source_repo
        line_markers = (
            line_run_markers(
                issue_comments(issue, repo=line_repo), repo=line_repo,
            )
            if isinstance(body, str) and line_repo else frozenset()
        )
        if not isinstance(body, str) or not any(
            candidate in body for candidate in line_markers
        ):
            event(
                "pr_run_marker_missing", level=logging.ERROR,
                expected=marker, branch=branch,
            )
            raise RuntimeError(
                f"PR body is missing the stable run marker {marker}; the PR "
                "must carry the run marker of this delivery line (the "
                "creating run or any later run of this Issue)"
            )
    fixes = f"Fixes #{issue}"
    # Accept GitHub-style `Fixes #N` and the common `Fixes N` variant.
    # The number must match exactly, not as a digit prefix: `Fixes #41`
    # closes Issue 41, not Issue 4 (review F1).
    if not external_pr and not re.search(rf"Fixes #?{issue}(?!\d)", body):
        event(
            "pr_fixes_missing", level=logging.ERROR,
            issue=issue, branch=branch,
        )
        raise MissingFixesError(
            f"PR body is missing `{fixes}`; the keyword must point at the "
            "source Issue so GitHub closes it natively when the PR merges "
            "into the default branch"
        )
    if expected_url is not None and url != expected_url:
        event(
            "pr_url_mismatch", level=logging.ERROR,
            expected=expected_url, actual=url, branch=branch,
        )
        raise ResumeVerificationError(
            f"resume PR validation: run_id={run_id} branch={branch} "
            f"open_pr_count=1 open_prs={[url]} scene_pr_state=OPEN; "
            f"scene_pr={expected_url}; PR URL {url} is not the "
            f"recovered original PR {expected_url}; the "
            "resume must keep the same PR number"
        )
    return url
