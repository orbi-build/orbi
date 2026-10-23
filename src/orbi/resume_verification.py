#!/usr/bin/env python3
"""The resume path: verify the recovered PR, then classify its failure.

`verify_resumed_pr` runs BEFORE any git/Pi mutation on a resumed
delivery. It derives branch and worktree from the configured
`repo_dir`, source repo, Issue number and run id (never from the
comment), recreates a missing worktree from the remote delivery branch
(the worktree is a local cache of the remote state — branch and PR live
on GitHub), verifies the PR through
`pr_verification.verify_pr` with the EXACT URL of the recovered scene,
and backfills the in-flight label before the delivery continues. The
returned URL is the one that was verified: the delivery wait never sees
the comment string.

A verification failure is classified here: a RECOVERABLE failure keeps
the Issue in the automatic fix loop (`ai-fix-needed` with the full scene
on Issue and PR), while an explicit `UnrecoverableDeliveryError` (an
external precondition the AI cannot safely judge or fix) is terminal
(`ai-blocked` alone). The external-takeover terminal scenes live here
too: they are the same scene arriving through
`ResumePrClosedError`, and `runner.delivery_step` reaches them through
this module so the two paths write identical comments.

This module imports no `runner` (CONSTITUTION Article 3.3): `runner`
calls it through the module object, so a stale `runner.<name>` patch
fails loudly instead of silently testing nothing.
"""
from __future__ import annotations

import json
import logging

from orbi import config as config_domain
from orbi import failure_report
from orbi import language
from orbi import pr_verification
from orbi.delivery_labels import (
    EVENT_CLAIM,
    EVENT_REQUEUE,
    PR_OPENED_LABEL,
    READY_LABEL,
)
from orbi.delivery_scene import RunContext
from orbi.failure import _failure_detail
from orbi.failure_report import UnrecoverableDeliveryError
from orbi.github import (
    _pr_number,
    apply_label_patch,
    comment_issue,
    issue_labels,
    run_gh_read_command,
)
from orbi.gitops import (
    create_worktree,
    stable_branch_exists,
    task_branch,
    worktree_path,
)
from orbi.journal import LOGGER, current_run_id, event, run_command
from orbi.pi_command import ROLE_REVIEW
from orbi.progress import run_marker
from orbi.pr_verification import (
    MissingFixesError,
    ReportedMissingFixesError,
    ResumeBranchGoneError,
    ResumePrClosedError,
)


def verify_resumed_pr(scene: dict, issue: dict, config: config_domain.RunnerConfig,
                      source_repo: str) -> str:
    """Verify the PR of a resumed delivery BEFORE any git/Pi mutation.

    #82 removed the cold-start fixer together with the
    pre-Pi `verify_pr` of the old `resume_delivery` — the resume passed
    the comment's PR URL straight to the delivery wait while the review
    froze the PR derived from the run id, so the two lines could be
    different PRs (a comment must never steer the runner into the wrong
    PR). Restored: branch and worktree are DERIVED from the
    configured repo_dir, source repo, Issue number and run id (never
    read from the comment), the scene base must still equal the
    configured base — checked BEFORE any command runs — and
    a missing worktree is RECREATED from the remote delivery branch
    (the worktree is a local cache of the remote state —
    branch + PR live on GitHub — so a sandbox rebuild or a deleted
    cache is restored instead of looping; only a branch gone from the
    remote is unrecoverable). The existing
    `verify_pr` then validates exactly one open PR of the derived
    branch in the configured source repo, on the configured base,
    carrying the run marker and the `Fixes` keyword, with the EXACT URL
    of the recovered scene. `require_latest_base=False`: being behind
    the latest base is the expected state the review session absorbs
    in-session, so the base merge never returns to the
    runner. The returned URL is the one verify_pr verified — the
    delivery wait only ever sees verified URLs, never the comment
    string.

    A failure is classified: a RECOVERABLE failure
    (unpushed local commit, runner exception, ...) keeps the Issue in
    the automatic fix loop — `ai-fix-needed` with a run-marked failure
    comment carrying the full scene (run_id, PR, branch, worktree,
    session, phase, last activity, concrete error) on Issue AND PR —
    while an explicit `UnrecoverableDeliveryError` (an external
    precondition the AI cannot safely judge or fix, e.g. a base-branch
    config change) is terminal: the Issue is marked `ai-blocked` ALONE
    (the opened-PR state label is removed, and a leftover
    `ai-fix-needed` too) with the explicit reason why automatic
    recovery is impossible. The error is re-raised after reporting so
    the tick boundary can distinguish this handled external scene from
    an unhandled Runner bug; no review Pi is started, nothing is merged,
    and the PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    run_id = scene["run_id"]
    # An external takeover scene delivers the contributor's own
    # PR — the delivery branch is the PR's head branch, read from the
    # takeover worktree (the worktree path stays derived from the trusted
    # inputs; the branch is a local git fact of that worktree).
    external = bool(scene.get("external"))
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config.repo_dir, source_repo, number, run_id,
    )
    try:
        if scene["base_branch"] != config.base_branch:
            # A base-branch change is a human
            # decision: the runner must not auto-retry a PR frozen on
            # another base, so the handler below marks the Issue
            # ai-blocked with the explicit reason and both base values
            # named.
            raise UnrecoverableDeliveryError(
                f"resume scene base_branch={scene['base_branch']} "
                f"differs from configured base_branch="
                f"{config.base_branch}; the PR is frozen on a "
                "different base and must not be resumed against the "
                "configured one — a base change is a human decision, "
                "so auto-retrying would keep failing on the same "
                "mismatch"
            )
        if not worktree.is_dir():
            # The worktree is a local cache of the remote
            # delivery state (the branch and the PR live on GitHub), so
            # a missing directory is recreated from the remote branch
            # and the resume continues — the recovery the #90/#50
            # comment promised. Only a branch that is gone from the
            # remote is unrecoverable: there is nothing left to
            # recreate from, a human decision.
            if external:
                # The takeover branch is the contributor's
                # head branch — the worktree that carried it is gone,
                # so the scene PR is the remaining authority.
                head = json.loads(run_gh_read_command(
                    ["gh", "pr", "view", str(_pr_number(scene["pr_url"])),
                     "--repo", source_repo, "--json", "headRefName"],
                    cwd=config.repo_dir,
                ))
                branch = str(head["headRefName"])
            if not external and not stable_branch_exists(
                    config.repo_dir, branch,
            ):
                raise ResumeBranchGoneError(
                    f"resume worktree {worktree} is missing and the "
                    f"delivery branch {branch} no longer exists on "
                    "origin; there is no remote state to recreate the "
                    "worktree from, so automatic recovery is impossible"
                )
            create_worktree(
                config.repo_dir, source_repo, number, run_id,
                scene["base_sha"], existing_branch=True, branch=branch,
                pr_number=_pr_number(scene["pr_url"]) if external else None,
            )
            event(
                "worktree_recreated", issue=number, branch=branch,
                worktree=str(worktree),
            )
        if external:
            branch = run_command(
                ["git", "branch", "--show-current"], cwd=worktree,
            )
        verified_url = pr_verification.verify_pr(
            RunContext(
                run_id=run_id, issue=number, branch=branch,
                worktree=worktree, source_repo=source_repo,
            ),
            config.base_branch, repo_dir=config.repo_dir,
            pr_repo=source_repo,
            expected_url=scene["pr_url"], require_latest_base=False,
            external_pr=external,
        )
        # The resumed delivery is in flight from here on —
        # the Runner holds the slot and continues the review/merge
        # work — so the Issue must carry the in-flight label BEFORE
        # the work continues. The backfill is an idempotent label
        # projection repair: the run, worktree and PR are the ones
        # verified above (nothing is recreated), and the opened-PR
        # state label (ai-pr-opened / ai-fix-needed) is untouched. A
        # label API failure falls into the failure handler below: it
        # is a recoverable resume failure (ai-fix-needed, failure
        # comment, tick stops) with the command evidence in the
        # journal.
        labels = {
            label.get("name") for label in issue.get("labels", [])
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        }
        # Resume scans include labels; preserve compatibility with callers
        # that provide a minimal issue object representing the normal
        # ai-pr-opened state.
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM,
            current_labels=labels or {PR_OPENED_LABEL},
        )
        return verified_url
    except Exception as exc:
        if isinstance(exc, ResumePrClosedError):
            # The resumed path IS the path, so a scene PR
            # that is no longer open is a NORMAL between-ticks state,
            # not a crash leftover. The fact is already decided on
            # GitHub — route it here, where the takeover flag is known;
            # blocking every closed scene alike would make the #608
            # requeue and the merged-delivery close unreachable (they
            # used to be the old wait loop's in-process branches).
            if external and exc.scene_pr_state == "MERGED":
                event(
                    "delivery_merged", issue=number, pr=scene["pr_url"],
                )
                _close_external_triage_issue(
                    number, source_repo, scene["pr_url"],
                    run_marker(run_id), run_id,
                    language_code=language.of(config),
                )
                raise
            if external:
                event(
                    "external_takeover_closed",
                    issue=number, pr=scene["pr_url"],
                )
                _requeue_closed_external_takeover(
                    number, source_repo, scene["pr_url"],
                    run_marker(run_id), run_id,
                    language_code=language.of(config), ready_label=config.dispatch_label or READY_LABEL)
                raise
            if exc.scene_pr_state == "MERGED":
                # A human merged the delivery PR (or a crash landed
                # between the merge and the label write): the `Fixes #N`
                # keyword closed the Issue natively — nothing to decide.
                event(
                    "delivery_merged", issue=number, pr=scene["pr_url"],
                )
                raise
        LOGGER.exception(
            "issue=%s resume_pr_verification_failed pr=%s branch=%s",
            number, scene["pr_url"], branch,
        )
        reported = False
        try:
            # The shared classified reporter: recoverable
            # -> `ai-fix-needed` with the full scene on Issue AND PR,
            # unrecoverable -> `ai-blocked` ALONE — the PR, branch and
            # worktree stay intact either way. `run_id` is the id the
            # tick BOUND: without it the comment carries no
            # marker and the progress publishing is skipped, whatever
            # the recovered scene says.
            lang = language.of(config)
            failure_report.report_delivery_failure(
                exc, issue=issue, source_repo=source_repo,
                run_id=current_run_id(), pr_url=scene["pr_url"],
                worktree=worktree, branch=branch, role=ROLE_REVIEW,
                language_code=lang, status_url=config.status_url,
                ready_label=config.dispatch_label or READY_LABEL, action=(
                    language.text(lang, "delivery.resume_missing_fixes_action",
                                  pr_url=scene["pr_url"], number=number)
                    if isinstance(exc, MissingFixesError)
                    else language.text(lang, "delivery.resume_review_pr_action")
                    if isinstance(exc, UnrecoverableDeliveryError) else ""
                ),
                reason=language.text(
                    lang, "delivery.resume_verify_reason",
                    pr_url=scene["pr_url"], detail=_failure_detail(exc),
                ),
                diagnosis=_failure_detail(exc),
                # The branch-gone scene destroyed the
                # delivery state — nothing is left to preserve, and the
                # preserved-objects note would contradict the reason.
                blocked_suffix=(
                    "" if isinstance(exc, ResumeBranchGoneError) else
                    language.text(
                        lang, "delivery.preserved_suffix", branch=branch,
                        worktree=language.text(
                            lang, "failure.scene.local_worktree"),
                    )
                ),
            )
            reported = True
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        if reported and isinstance(exc, MissingFixesError):
            raise ReportedMissingFixesError(str(exc)) from exc
        raise


def _requeue_closed_external_takeover(
    number: int, source_repo: str, pr_url: str, marker: str, run_id: str,
    *, language_code: str, ready_label: str = READY_LABEL,
    status_url: str | None = None,
) -> None:
    """The #608 放弃/不可修 fallback: the external PR was closed without
    a merge (the contributor withdrew it, or a maintainer rejected it).

    The triage Issue returns to the ready queue and the next claim
    delivers the fix internally; the supersession is explained on the
    closed PR thread too, because the contributor watches their PR,
    never the triage Issue (docs/contributing.mdx). Shared by the
    delivery step and the resume seam, where the same scene arrives
    through `ResumePrClosedError`.
    """
    apply_label_patch(
        number, repo=source_repo, event=EVENT_REQUEUE,
        current_labels=issue_labels(number, repo=source_repo),
        ready_label=ready_label,
    )
    body = (
        f"{marker}\n"
        + language.text(
            language_code, "runner.external_pr_closed",
            pr_url=pr_url, number=number, run_id=run_id,
        )
    )
    comment_issue(number, repo=source_repo, body=body)
    failure_report.comment_pr(_pr_number(pr_url), repo=source_repo, body=body)


def _close_external_triage_issue(
    number: int, source_repo: str, pr_url: str, marker: str, run_id: str,
    *, language_code: str,
) -> None:
    """Close the triage Issue of a merged external takeover (#608/#726).

    The external PR's body carries no `Fixes #N` for the triage Issue,
    so GitHub never closes it natively. The merge already landed; a
    failed close is bookkeeping that is logged (bypass) — it can never
    rewrite the merged fact.
    """
    try:
        body = (
            f"{marker}\n"
            + language.text(
                language_code, "runner.external_pr_merged",
                pr_url=pr_url, run_id=run_id,
            )
        )
        comment_issue(number, repo=source_repo, body=body)
        run_command(
            ["gh", "issue", "close", str(number),
             "--repo", source_repo],
        )
    except Exception:
        LOGGER.exception(
            "issue=%s external_takeover_close_failed pr=%s",
            number, pr_url,
        )
