"""Merge execution and closeout: the gate, the merge record and the
deployment checkout sync.

Extracted unchanged from the 1831-line `orbi.review_merge` (Issue #1857):
the merge gate and its exceptions, the merge-method selection, the merge
confirmation and record metrics, the deployment-checkout sync, and the
human-review closeout checklist. Article 3.2: an extraction changes no
behaviour — the gate order (failed -> `GateCIFailure`, pending ->
`DeliveryDeferred`, `mergeable=UNKNOWN` -> defer, otherwise merge with
`--match-head-commit`), the merge method selection, the comments, the
labels and the argv are exactly what they were.

The review loop in `orbi.review_merge` calls these functions and catches
the three exception types, so it imports them from here. This module
imports nothing from `orbi.runner` and nothing from `orbi.review_merge`
(Article 3.3): the merge execution never reaches back into the
orchestration that calls it.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from orbi import config as config_domain
from orbi import failure_report, github, gitops, human_review, language, run_state
from orbi.branch_reclaim import delete_merged_delivery_branch
from orbi.checks import classify_checks, render_check
from orbi.delivery_labels import BLOCKED_LABEL
from orbi.failure_report import GateCIFailure
from orbi.github import comment_issue, issue_labels, pr_view
from orbi.gitops import acquire_base_sync_lock, fetch_base_ref, is_ancestor
from orbi.journal import (
    LOGGER,
    event,
    run_command,
    run_git_network_command,
)
from orbi.merge_handoff import MergeHandoffRequired, is_maintainer_actionable
from orbi.progress import run_marker


class RecoverableMergeGateError(RuntimeError):
    """A merge-gate failure the next review session can repair.

    This type deliberately identifies only merge-gate outcomes that require
    absorbing the latest base or resolving merge conflicts. Callers must not
    infer delivery control flow from the human-readable error message.
    """


class DeliveryDeferred(Exception):
    """An intermediate GitHub state asked the delivery to wait.

    Pending CI checks or a still-UNKNOWN mergeability are transient
    states, never failures: the caller journals the
    observation and returns — the next tick re-reads the state. The
    delivery labels stay untouched and the review-round budget does not
    advance, so a deferred tick costs a couple of read calls only.
    """


class MergeBlockedByIssueLabel(Exception):
    """The gate refused to merge because the source Issue is `ai-blocked`.

    A maintainer's terminal label wins over a fully green gate: the skip
    comment is posted by `merge_gate`, the PR stays open and the labels
    stay as the human set them (no `ai-merged`), so the caller only stops
    the round.
    """


# GitHub computes mergeability asynchronously; a CI-concluded UNKNOWN read is
# re-read after these bounded delays (≈15 s of slot time, Issue #1736).
MERGEABLE_UNKNOWN_RETRY_DELAYS = (2.0, 4.0, 8.0)


# The repository's GitHub settings are the only source of the merge
# method (Issue #1480): the first enabled flag in GitHub's own
# precedence order wins, so an Orbi-side option can never disagree
# with the repository.
MERGE_METHOD_FIELDS = (
    ("allow_merge_commit", "--merge"),
    ("allow_squash_merge", "--squash"),
    ("allow_rebase_merge", "--rebase"),
)

def select_merge_method(repo: str) -> str:
    """Pick the `gh pr merge` flag from the repository's GitHub settings.

    Reads `gh api repos/<repo>` and takes the first enabled boolean in
    GitHub's precedence order (`allow_merge_commit`, then
    `allow_squash_merge`, then `allow_rebase_merge`). A failed read, a
    payload that is not an object, or a payload where none of the three
    fields is an enabled boolean falls back to `--merge` (today's
    behavior) with a `merge_method_unknown` warning — never a guessed
    method.
    """
    try:
        payload = json.loads(run_command(["gh", "api", f"repos/{repo}"]))
    except (subprocess.CalledProcessError, json.JSONDecodeError,
            TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for field, flag in MERGE_METHOD_FIELDS:
            if payload.get(field) is True:
                return flag
    event("merge_method_unknown", level=logging.WARNING, repo=repo)
    return "--merge"


def merge_gate(worktree: Path, pr: dict, base_branch: str,
               *, repo_dir: Path,
               source_repo: str | None = None,
               issue_number: int | None = None,
               run_id: str | None = None,
               language_code: str) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, the remote head to still be the reviewed head, and the
    exact head's GitHub CI checks to be completed successfully, and the
    source Issue to still NOT carry `ai-blocked` (Issue #1504: the label is
    re-read immediately before the merge, so a maintainer's terminal label
    added while the PR was in review stops the merge; one skip comment names
    the reviewed head and the PR/labels stay untouched). If the base
    advanced but GitHub reports a conflict-free PR, absorb it with a plain
    merge and push the task branch, then record the absorbed head and raise
    `DeliveryDeferred` (Issue #1626: never merge a head this tick
    pushed; its CI has not registered yet and this does not start another
    review round). Every state is read once
    per gate pass: a pending check is not a failure but an intermediate
    state — the gate raises `DeliveryDeferred` and the next tick re-reads.
    A CI-concluded UNKNOWN mergeability is re-read in-tick with
    `MERGEABLE_UNKNOWN_RETRY_DELAYS` (Issue #1736) and defers only when
    every read stays UNKNOWN. A failed check or a not-mergeable PR prevents
    the merge. Then merge with `--match-head-commit` so only that
    exact head can land, using the merge method the repository's own GitHub
    settings allow (Issue #1480: `gh api repos/<repo>` — merge commit, else
    squash, else rebase, else `--merge` with a `merge_method_unknown`
    warning). No force push, no direct push of the protected branch.
    The base fetch updates the shared remote-tracking ref, so it runs under the
    base-sync lock with the deployment checkout as the lock location.
    """
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    # Read GitHub mergeability before rejecting a stale head.  A clean PR can
    # absorb the base here without changing its reviewed diff; a conflicted
    # PR still follows the existing fix-needed path below.
    state = pr_view(pr["number"],
                    "state,mergeable,headRefOid,statusCheckRollup",
                    cwd=worktree)
    mergeable = state.get("mergeable")
    # Issue #1736: in-tick re-read; always the WHOLE state, no mixed facts.
    for delay in MERGEABLE_UNKNOWN_RETRY_DELAYS:
        rollup = classify_checks(state.get("statusCheckRollup") or [])
        if mergeable != "UNKNOWN" or any(rollup[:2]):  # (pending, failed)
            break
        time.sleep(delay)
        state = pr_view(pr["number"],
                        "state,mergeable,headRefOid,statusCheckRollup",
                        cwd=worktree)
        mergeable = state.get("mergeable")
    if mergeable != "MERGEABLE" and mergeable != "UNKNOWN":
        # Keep the existing mergeability policy and message. The assessment
        # below still classifies the reviewed-head/base combination, while
        # GitHub's mergeability remains the source of this recovery action.
        freshness = run_state.assess_base_freshness(
            worktree, base_branch, head=pr["head_oid"],
            reviewed_head=state.get("headRefOid"), mergeable=mergeable,
        )
        event(
            "merge_gate_not_mergeable", level=logging.ERROR,
            pr=pr["number"], mergeable=mergeable,
        )
        raise RecoverableMergeGateError(
            f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
            "resolve conflicts and retry"
        )
    freshness = run_state.assess_base_freshness(
        worktree, base_branch, head=pr["head_oid"],
        reviewed_head=state.get("headRefOid"),
        # UNKNOWN is transient, not evidence of a merge conflict. It must
        # reach the normal deferred mergeability path rather than absorb.
        mergeable=mergeable if mergeable == "MERGEABLE" else None,
    )
    if freshness is run_state.BaseFreshness.ABSORBABLE and mergeable == "MERGEABLE":
        base_sha = run_command(
            ["git", "rev-parse", f"origin/{base_branch}"], cwd=worktree,
        )
        try:
            run_command(["git", "merge", f"origin/{base_branch}"], cwd=worktree)
        except subprocess.CalledProcessError as exc:
            run_command(["git", "merge", "--abort"], cwd=worktree)
            event(
                "base_merge_conflict", level=logging.ERROR,
                base_branch=base_branch, base_sha=base_sha,
                pr=pr["number"], head=pr["head_oid"],
                returncode=exc.returncode,
                stderr=(exc.stderr or "").strip(),
            )
            raise RecoverableMergeGateError(
                f"PR #{pr['number']} cannot absorb origin/{base_branch} "
                f"({base_sha}); resolve the merge conflict and retry"
            ) from None
        absorbed_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
        run_git_network_command(
            ["git", "push", "origin", f"HEAD:{pr['head_ref']}"],
            cwd=worktree,
        )
        remote_head = gitops.remote_branch_head(pr["head_ref"], cwd=worktree)
        if remote_head != absorbed_head:
            raise RuntimeError(
                f"remote head {remote_head} does not match absorbed head "
                f"{absorbed_head} after push origin {pr['head_ref']}"
            )
        event(
            "base_absorbed", base_branch=base_branch,
            base_sha=base_sha, pr=pr["number"],
            old_head=pr["head_oid"], head=absorbed_head,
        )
        pr = {**pr, "head_oid": absorbed_head}
        # The push starts a new CI run whose checks may not be registered
        # yet, and an empty rollup counts as neither pending nor failed.
        # The state read still verifies GitHub sees the pushed head and
        # stays mergeable, then the gate defers (Issue #1626): never merge
        # a head this tick pushed. A merge-only resume merges after CI.
        state = pr_view(pr["number"],
                        "state,mergeable,headRefOid,statusCheckRollup",
                        cwd=worktree)
        mergeable = state.get("mergeable")
        if state.get("headRefOid") != absorbed_head:
            raise RuntimeError(
                f"PR #{pr['number']} head moved after base absorb "
                f"(absorbed={absorbed_head} remote={state.get('headRefOid')})"
            )
        if mergeable not in ("MERGEABLE", "UNKNOWN"):
            event(
                "merge_gate_not_mergeable", level=logging.ERROR,
                pr=pr["number"], mergeable=mergeable,
            )
            raise RecoverableMergeGateError(
                f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
                "resolve conflicts and retry"
            )
        run_state.update_reviewed_head(worktree, absorbed_head)
        event(
            "base_absorbed_deferred", pr=pr["number"],
            base_branch=base_branch, base_sha=base_sha, head=absorbed_head,
        )
        raise DeliveryDeferred(
            f"PR #{pr['number']} absorbed origin/{base_branch} ({base_sha}) "
            f"and pushed {absorbed_head}; the merge is deferred until that "
            "head's CI concludes"
        )
    if freshness is run_state.BaseFreshness.CONFLICTED:
        event(
            "merge_gate_head_moved", level=logging.ERROR,
            pr=pr["number"], reviewed=pr["head_oid"],
            remote=state.get("headRefOid"),
        )
        raise RuntimeError(
            f"PR #{pr['number']} head moved since review "
            f"(reviewed={pr['head_oid']} remote={state.get('headRefOid')}); "
            "re-review before merging"
        )
    pending, failed, _passed = classify_checks(state.get("statusCheckRollup") or [])
    if failed:
        failed_names = [entry["name"] for entry in failed]
        failure_report._raise_if_preexisting_ci_failure(
            pr.get("_source_repo", source_repo or ""), failed_names,
            pr.get("base_oid"), base_branch=base_branch,
            pr_number=pr["number"])
        raise GateCIFailure(
            f"delivery gate: CI check '{failed_names[0]}' "
            f"failed on PR #{pr['number']}: "
            + ", ".join(render_check(entry) for entry in failed)
        )
    if pending:
        detail = ", ".join(render_check(entry) for entry in pending)
        event(
            "merge_gate_ci_pending", pr=pr["number"], pending=detail,
        )
        raise DeliveryDeferred(
            f"PR #{pr['number']} CI is still running ({detail}); "
            "the merge is deferred to the next tick"
        )
    mergeable = state.get("mergeable")
    if mergeable == "UNKNOWN":
        event(
            "merge_gate_mergeable_unknown", pr=pr["number"],
        )
        raise DeliveryDeferred(
            f"PR #{pr['number']} mergeable state is UNKNOWN; "
            "the merge is deferred to the next tick"
        )
    # `assess_base_freshness` has already classified the mergeable and
    # reviewed-head states above; only a fresh, mergeable head reaches the
    # actual merge command.
    merge_repo = source_repo or pr.get("_source_repo", "")
    if issue_number is not None:
        # Issue #1504: re-read the Issue's labels at the last possible
        # moment. `ai-blocked` is otherwise only honoured at claim time, so
        # a maintainer label added while the run was in review used to be
        # merged over. It is a human decision: the PR stays open and the
        # labels stay as the human set them.
        labels = issue_labels(issue_number, merge_repo)
        if BLOCKED_LABEL in labels:
            marker = run_marker(run_id)
            event(
                "merge_gate_issue_blocked", level=logging.ERROR,
                issue=issue_number, pr=pr["number"], head=pr["head_oid"],
            )
            try:
                comment_issue(
                    issue_number, repo=merge_repo,
                    body=f"{marker}\n" + language.text(
                        language_code, "review.merge_skipped_blocked",
                        pr_number=pr["number"], label=BLOCKED_LABEL,
                        head=pr["head_oid"], run_id=run_id,
                    ),
                )
            except Exception:
                # The skip comment is a bypass: publishing it must never
                # turn the human's `ai-blocked` decision into a merge or a
                # label change. The gate still refuses the merge.
                LOGGER.exception(
                    "merge_skip_comment_publish_failed issue=%s pr=%s "
                    "run_id=%s", issue_number, pr["number"], run_id,
                )
            raise MergeBlockedByIssueLabel(
                f"Issue #{issue_number} is labelled {BLOCKED_LABEL}; the "
                f"reviewed head {pr['head_oid']} of PR #{pr['number']} was "
                "not merged"
            )
    merge_method = select_merge_method(merge_repo)
    confirmation = None
    try:
        run_command([
            "gh", "pr", "merge", str(pr["number"]),
            "--match-head-commit", pr["head_oid"], merge_method,
        ], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        # Issue #1798: GitHub can create the merge commit and this call still
        # fail (`502 Bad Gateway`), leaving the PR OPEN with `mergedAt: null`.
        # `confirm_merged` decides: landed continues like a success (never a
        # second merge call), "not landed" keeps today's path. Issue #1802:
        # the decision is made once and returned to the caller.
        try:
            confirmation = confirm_merged(worktree, pr, base_branch,
                                          repo_dir=repo_dir)
        except RuntimeError:
            pass
        if confirmation is None:
            preflight = github.merge_gate_preflight(merge_repo, base_branch)
            stderr = str(exc.stderr or "")
            if is_maintainer_actionable(stderr, preflight):
                failed_preflight = [
                    line for line in preflight
                    if line.startswith("merge_gate: FAILED")
                ]
                raise MergeHandoffRequired(
                    f"PR #{pr['number']} is ready for the named maintainer "
                    "action",
                    preflight=failed_preflight,
                ) from None
            raise
        event("merge_landed_after_error", level=logging.WARNING,
              pr=pr["number"], head=pr["head_oid"], method=merge_method,
              returncode=exc.returncode, stderr=str(exc.stderr or "").strip())
    event("merged", pr=pr["number"], head=pr["head_oid"],
          method=merge_method)
    delete_merged_delivery_branch(merge_repo, pr["number"], pr["head_ref"])
    return {**pr, "merged": True, "merge_method": merge_method,
            "confirmation": confirmation}


def confirm_merged(worktree: Path, pr: dict, base_branch: str,
                   *, repo_dir: Path) -> dict:
    """Confirm the reviewed PR's merge landed on origin/<base>.

    A MERGED PR answers with its merge commit, which must be on the base
    branch. Issue #1798: it can also land while the merge call errors,
    leaving the PR unmerged with `mergedAt: null` — the head on origin/<base>
    is that merge, answered with the PR's own state so the caller closes
    the Issue (Issue #1801). A rejection otherwise; a failed read propagates.

    The base fetch updates the shared remote-tracking ref, so it runs under
    the base-sync lock with the deployment checkout as the lock location.
    """
    state = pr_view(pr["number"], "state,mergedAt,mergeCommit", cwd=worktree)
    merged = state.get("state") == "MERGED" and bool(state.get("mergedAt"))
    merge_commit = (state.get("mergeCommit") or {}).get("oid")
    if merged and not merge_commit:
        raise RuntimeError(
            f"PR #{pr['number']} is merged but has no merge commit oid"
        )
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    if not merged:
        # Issue #1798: it can have landed while the merge call errored.
        merge_commit = run_state.landed_merge_commit(
            pr["head_oid"], f"origin/{base_branch}", cwd=worktree)
        if merge_commit is None:
            event("confirm_merged_not_merged", level=logging.ERROR,
                  pr=pr["number"], state=state.get("state"))
            raise RuntimeError(f"PR #{pr['number']} is not merged "
                               f"(state={state.get('state')})")
        event("confirm_merged_landed_on_base", pr=pr["number"],
              head=pr["head_oid"], merge_commit=merge_commit)
        return {"state": state.get("state"), "merge_commit": merge_commit}
    if not is_ancestor(merge_commit, f"origin/{base_branch}", cwd=worktree):
        event(
            "confirm_merged_missing_on_base", level=logging.ERROR,
            pr=pr["number"], merge_commit=merge_commit,
        )
        raise RuntimeError(
            f"merge commit {merge_commit} is not on origin/{base_branch}; "
            "the merge did not land on the protected branch"
        )
    return {"state": "MERGED", "merge_commit": merge_commit}


def merge_commit_metrics(worktree: Path, merge_commit: str,
                         pushed_head: str | None,
                         pushed_base: str | None,
                         *, merge_method: str = "--merge") -> tuple[str, str]:
    """The merge record's `(external_commits, commits)` field values.

    Issue #833: `commits` is the PR branch's total commit count
    relative to the base — `git rev-list base..head` at merge time,
    read after the merge through the merge commit's parent chain
    (`M^1` is the base tip the PR merged into, `M^2` the PR head), so
    the merged PR head's objects never need to exist locally.
    `external_commits` subtracts the engine's own push history: the
    recorded last engine-pushed head (`pushed_head`; GitHub rejects
    non-fast-forward pushes, so the engine's heads form an ancestry
    chain and the per-interval union collapses to the last one), with
    `pushed_base` (the foreign head an external takeover starts from)
    excluded so the taken-over commits stay external. `0` means merged
    as-is.

    `external_commits` is the literal string `"unknown"` whenever the
    engine's push history cannot be proven — a missing/corrupt push
    record, a recorded head or base that is not an ancestor of the
    merged head — while `commits` keeps its proven count; both values
    are `"unknown"` only when a git read itself fails (a missing
    object, any git failure). A degraded metric must never fail a
    landed merge and never fabricate a `0`.

    A squash or rebase merge (Issue #1480) has no second parent, so the
    `M^1..M^2` window does not exist: both metrics are recorded as
    `"unknown"` directly, without a doomed git read.
    """
    if merge_method != "--merge":
        return "unknown", "unknown"
    try:
        commits = int(run_command(
            ["git", "rev-list", "--count", f"{merge_commit}^1..{merge_commit}^2"],
            cwd=worktree,
        ))
        if pushed_head is None:
            return "unknown", str(commits)
        if not is_ancestor(pushed_head, f"{merge_commit}^2", cwd=worktree):
            return "unknown", str(commits)
        command = ["git", "rev-list", "--count", pushed_head,
                   "--not", f"{merge_commit}^1"]
        if pushed_base is not None:
            if not is_ancestor(pushed_base, f"{merge_commit}^2",
                                cwd=worktree):
                return "unknown", str(commits)
            command.append(pushed_base)
        engine = int(run_command(command, cwd=worktree))
    except (subprocess.CalledProcessError, ValueError):
        return "unknown", "unknown"
    # The engine count's positive set is reachable from `pushed_head`
    # (an ancestor of M^2) and every negation only shrinks it, so it is
    # a subset of the `commits` window and the subtraction cannot go
    # negative.
    return str(commits - engine), str(commits)


def sync_base_checkout(repo_dir: Path, base_branch: str,
                       *, lock_timeout_seconds: float = 300.0) -> None:
    """Fast-forward the configured repo_dir base checkout to origin/<base>.

    systemd executes the runner from this checkout: after a merge lands
    on origin/<base>, the next tick must load the newly merged code, so
    the deployment checkout is synced here and verified to equal the
    remote base. A checkout that cannot fast-forward (local drift) fails
    fast; the merge itself already landed on GitHub.

    The whole sync runs under the short-lived base-sync
    flock (the SAME lock the service template's `ExecStartPre` uses),
    so two instances starting in the same tick never write the main
    worktree concurrently; the lock is released when the sync finishes
    (success or failure).
    """
    fd = acquire_base_sync_lock(repo_dir, lock_timeout_seconds)
    try:
        _sync_base_checkout_locked(repo_dir, base_branch)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _sync_base_checkout_locked(repo_dir: Path, base_branch: str) -> None:
    """The actual fetch + fast-forward + verify, under the base-sync
    flock (see ``sync_base_checkout``)."""
    run_git_network_command(
        ["git", "fetch", "origin", base_branch], cwd=repo_dir,
    )
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    remote_head = run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )
    if local_head == remote_head:
        return
    try:
        run_command(
            ["git", "merge", "--ff-only", f"origin/{base_branch}"],
            cwd=repo_dir,
        )
    except subprocess.CalledProcessError:
        event(
            "base_checkout_not_fast_forwardable", level=logging.ERROR,
            repo_dir=repo_dir, base=base_branch, local=local_head,
            remote=remote_head,
        )
        raise RuntimeError(
            f"deployment checkout {repo_dir} cannot fast-forward to "
            f"origin/{base_branch} (local={local_head} "
            f"remote={remote_head}); the merged code cannot be loaded "
            "by the next tick"
        ) from None
    synced = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if synced != remote_head:
        raise RuntimeError(
            f"deployment checkout {repo_dir} is at {synced} after the "
            f"sync, expected origin/{base_branch} at {remote_head}"
        )
    event(
        "base_checkout_synced", repo_dir=repo_dir, base=base_branch,
        head=synced,
    )


def delivered_changed_files(worktree: Path, base: str) -> list[str] | None:
    """The paths the delivered commits changed against the frozen base.

    The checklist's classification input. A git failure
    returns None — unknown evidence is MISSING evidence, and the
    checklist treats missing evidence as a column-2 item so the gate
    holds (the safe direction: only real evidence can pass it).
    """
    try:
        raw = run_command(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=worktree,
        )
    except Exception:
        LOGGER.exception(
            "human_review_diff_read_failed worktree=%s base=%s",
            worktree, base,
        )
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]

def _delivery_checklist(
    worktree: Path, config: config_domain.RunnerConfig, *, test_status: str | None,
) -> dict:
    """Build the human acceptance checklist ONCE from delivery evidence.

    Evidence is the delivery worktree's committed diff against the frozen
    base plus the caller's PR head CI status (Issue #1613). The PR-open
    render and the gate's wait decision read this one build, so column 2
    is decided once; `base_sha` is present on the run that posts it, and a
    resumed re-derivation falls back to the configured base branch.
    Missing evidence lands IN column 2 (the gate holds).
    """
    base = config.base_sha or config.base_branch
    return human_review.build_checklist(
        test_status=test_status,
        changed_files=delivered_changed_files(worktree, base),
        language_code=language.of(config),
    )

def human_review_checklist(
    worktree: Path, config: config_domain.RunnerConfig, *, run_id: str, pr_url: str,
    test_status: str | None, title: str | None = None, issue: int | None = None,
) -> tuple[str, bool]:
    """Render the checklist comment and whether a person must confirm.

    `needs_person` is "column 2 is non-empty" (the gate's own decision);
    the caller sends its "waiting for your review" message next to the
    checklist it posts — once per delivery, when the PR opens.
    """
    checklist = _delivery_checklist(
        worktree, config, test_status=test_status,
    )
    return (
        human_review.render_checklist_comment(
            run_id=run_id, pr_url=pr_url,
            language_code=language.of(config), checklist=checklist,
            status_url=config.status_url, issue=issue, title=title,
        ),
        bool(checklist["column2"]),
    )
