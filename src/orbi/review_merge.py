"""Review and merge: the delivery's independent review loop and merge gate.

This module owns the domain that closed the delivery in `runner.py`: the
independent review round (`_run_review_round`, `review_and_merge_if_clean`),
the machine-readable verdict (`parse_review_verdict`, `_validated_verdict`,
`review_has_findings`), the round comment (`review_round_comment_body`,
`_review_findings_markdown`) and the merge gate (`merge_gate`,
`confirm_merged`, `merge_commit_metrics`), all moved here unchanged
(Article 3.2: an extraction changes no behaviour).

It imports nothing from `orbi.runner` (Article 3.3). The review/merge
domain pulls in the helpers it cannot travel without — the PR freeze, the
base-freshness assessment, the run-state push record the merge metrics
read, the live progress throttle, the scene resume and the human review
column — as its own definitions or as imports from the leaf module that
owns them; `runner` reaches the moved names through this module.
"""
from __future__ import annotations

import fcntl
import functools
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import replace
from enum import Enum
from pathlib import Path

from orbi import config as config_domain
from orbi import failure_report, github, gitops, human_review, pi_session, run_state, scene
from orbi.blocked_comment import post_blocked_comment
from orbi.branch_reclaim import delete_merged_delivery_branch
from orbi.checks import _classify_rollup, _render_check, test_status
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    EVENT_FIX_NEEDED,
    EVENT_HUMAN_REVIEW_WAITING,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    HUMAN_REVIEW_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    is_resumable,
    needs_human_intervention,
)
from orbi.delivery_scene import RunContext
from orbi.failure import _failure_detail
from orbi.failure_report import (
    GateCIFailure,
    HumanDecisionRequired,
    ReviewRoundsExhausted,
    UnrecoverableDeliveryError,
)
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    _check_summaries,
    apply_label_patch,
    comment_issue,
    commit_check_runs,
    issue_comments,
    issue_labels,
    issue_priority,
    pr_delivery_rollup,
    pr_view,
)
from orbi import language, user_summary
from orbi.delivery_comment import merged_pr_comment_body  # noqa: F401
from orbi.gitops import (
    _is_ancestor,
    acquire_base_sync_lock,
    fetch_base_ref,
    task_branch,
    worktree_path,
)
from orbi.journal import (
    LOGGER,
    current_run_id,
    event,
    run_command,
    run_git_network_command,
    validate_run_id,
)
from orbi.merge_handoff import (
    MergeHandoffRequired,
    handle_merge_handoff,
    is_maintainer_actionable,
)
from orbi.pi_command import ROLE_REVIEW
from orbi.comment_kind import (
    COMMENT_KIND_FAILURE,
    COMMENT_KIND_MERGED,
    COMMENT_KIND_REVIEW_ROUND,
    comment_kind_marker,
)
from orbi.progress import (
    LiveProgressThrottle,
    ProgressPublisher,
    _progress_body,
    _progress_state,
    _safe_publish,
    run_marker,
    step_publisher,
)
from orbi.review_rounds import (
    MAX_REVIEW_ROUNDS,
    effective_review_round,
    review_round_comment_body,
)
# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (review_rounds.MAX_REVIEW_ROUNDS, the
# same value the reported round caps at — Issue #1779).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_BASE_ADVANCE_ROUNDS = 5
# GitHub computes mergeability asynchronously; a CI-concluded UNKNOWN read is
# re-read after these bounded delays (≈15 s of slot time, Issue #1736).
MERGEABLE_UNKNOWN_RETRY_DELAYS = (2.0, 4.0, 8.0)


class RecoverableMergeGateError(RuntimeError):
    """A merge-gate failure the next review session can repair.

    This type deliberately identifies only merge-gate outcomes that require
    absorbing the latest base or resolving merge conflicts. Callers must not
    infer delivery control flow from the human-readable error message.
    """


def _read_pushed(worktree: Path, key: str) -> str | None:
    """One recorded push-history field, or None when unusable.

    The merge record reads these AFTER the merge landed: a missing or
    corrupt record must degrade the metric to `unknown`, never fail a
    landed delivery and never fabricate a count.
    """
    try:
        state = run_state.read_run_state(worktree)
    except ValueError:
        return None
    if not state:
        return None
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def read_pushed_head(worktree: Path) -> str | None:
    """The recorded last engine-pushed head, or None when unusable."""
    return _read_pushed(worktree, "pushed_head")


def read_pushed_base(worktree: Path) -> str | None:
    """The recorded engine push-line base, or None when absent.

    None means the engine's first push created the branch, so the
    merge record's engine interval starts at the merge's base parent.
    """
    return _read_pushed(worktree, "pushed_base")


def _is_code_fence_line(line: str) -> bool:
    """True when a stripped line is only a Markdown code fence.

    Reviewers commonly wrap the machine-readable verdict in a fence
    (```` ``` ````, ```` ```json ```` or `~~~`); a fence line carries no
    review content, so the tail scan skips it without relaxing the verdict checks.
    """
    stripped = line.strip()
    for fence_char in ("`", "~"):
        if stripped.startswith(fence_char * 3):
            remainder = stripped.lstrip(fence_char)
            if fence_char == "`":
                # A backtick fence's info string must not contain backticks.
                return "`" not in remainder
            return True
    return False


def _json_dict_span(segment: str) -> dict | None:
    """The `{...}` dict embedded in a text segment, or None.

    Wrapper noise around a verdict payload — a leading text
    prefix, Markdown inline-code backticks, trailing CJK/Western
    punctuation — all sit OUTSIDE the braces, so the first-`{`…last-`}`
    span isolates the JSON. A segment whose braces are reversed or whose
    span does not parse (code snippets, prose examples) returns None.
    """
    start = segment.find("{")
    end = segment.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        parsed = json.loads(segment[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _validated_verdict(parsed: dict) -> dict:
    """The semantic checks every verdict payload must pass."""
    if parsed.get("verdict") not in (
        "pass", "findings", "blocked_on_human_decision",
    ):
        raise ValueError(
            "verdict must be 'pass', 'findings' or "
            "'blocked_on_human_decision'"
        )
    head = parsed.get("head")
    if not isinstance(head, str) or not head:
        raise ValueError("head must be the reviewed commit SHA")
    for key in ("blockers", "majors", "minors"):
        value = parsed.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not isinstance(parsed.get("findings", []), list):
        raise ValueError("findings must be a list")
    blockers = parsed["blockers"]
    majors = parsed["majors"]
    if parsed["verdict"] == "pass" and (blockers > 0 or majors > 0):
        raise ValueError("pass verdict cannot have blockers or majors")
    verdict = parsed["verdict"]
    if verdict in ("findings", "blocked_on_human_decision") \
            and blockers == 0 and majors == 0:
        raise ValueError(f"{verdict} verdict requires blockers or majors")
    if verdict == "blocked_on_human_decision":
        findings = parsed["findings"]
        if blockers != 0 or majors != 1 or len(findings) != 1:
            raise ValueError(
                "blocked_on_human_decision verdict requires exactly one "
                "Major finding and no blockers"
            )
        finding = findings[0]
        if (not isinstance(finding, dict)
                or finding.get("level") != "Major"
                or not isinstance(finding.get("note"), str)
                or not finding["note"].strip()
                or not isinstance(finding.get("fix"), str)
                or not finding["fix"].strip()):
            raise ValueError(
                "blocked_on_human_decision finding must include a non-empty "
                "Major note and fix"
            )
    return parsed


def parse_review_verdict(text: str) -> dict:
    """Extract the REVIEW_VERDICT JSON from a review session's output.

    The output is scanned BACKWARDS: the verdict may be the
    last line, wrapped in the reviewer's natural-language phrasing
    (leading CJK prefix, inline-code backticks, trailing punctuation —
    the orbi-cloud#287 scene), or followed by trailing prose. The
    semantics do not relax with the shape: a line that only
    MENTIONS `REVIEW_VERDICT` without starting it (a quote from the
    Issue body, a diff hunk, an echo) is never adopted, a verdict-shaped
    but invalid JSON blob in prose is never adopted, every payload must
    pass the full semantic validation, and the verdict must still name
    the head it covers (`head`); the merge gate checks it against the PR
    head. A `REVIEW_VERDICT`-marked line is the explicit verdict channel:
    a malformed or semantically invalid payload there fails fast instead
    of being skipped. Two DIFFERENT verdicts in one output are ambiguous
    and fail without picking one; identical duplicates agree and are
    accepted. Missing or malformed verdicts fail fast; a review that
    cannot be read as a pass is never treated as a pass.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    while lines and _is_code_fence_line(lines[-1]):
        lines.pop()
    candidates = []
    last_index = len(lines) - 1
    for index in range(last_index, -1, -1):
        stripped = lines[index].strip()
        marked = stripped.startswith(VERDICT_MARKER)
        if not marked and VERDICT_MARKER in stripped:
            continue  # a marker mention, never a verdict
        payload = stripped[len(VERDICT_MARKER):] if marked else stripped
        parsed = _json_dict_span(payload)
        if marked:
            # The explicit verdict channel: a malformed payload fails
            # fast, it is never silently skipped.
            if parsed is None:
                raise ValueError("malformed REVIEW_VERDICT JSON")
            candidates.append(_validated_verdict(parsed))
        elif parsed is not None:
            # Issue #837: an unmarked verdict-shaped JSON is adoptable only
            # from the LAST non-empty line embedded in natural-language
            # phrasing (the orbi-cloud#287 scene). Anywhere else — a
            # mid-body quote, a fenced display block, a bare payload line —
            # it is quotation, never the reviewer's own conclusion: it
            # enters no candidate pool, so it can neither be adopted nor
            # kill the real verdict by conflict.
            if index != last_index:
                continue
            if stripped.startswith("{") and stripped.endswith("}"):
                continue  # a bare payload line, not phrased prose
            try:
                candidates.append(_validated_verdict(parsed))
            except ValueError:
                continue  # verdict-shaped prose, not the channel
    if not candidates:
        raise ValueError("no REVIEW_VERDICT line in review output")
    if len({json.dumps(v, sort_keys=True) for v in candidates}) > 1:
        raise ValueError("conflicting REVIEW_VERDICT verdicts in review "
                         "output")
    return candidates[0]


def review_has_findings(verdict: dict) -> bool:
    """True when a verdict still blocks the merge gate (Blocker or Major)."""
    return verdict["blockers"] > 0 or verdict["majors"] > 0


def freeze_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    *,
    external_pr_url: str | None = None,
    source_repo: str | None = None,
) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    pr = run_state._single_open_pr(
        worktree, branch, base_branch, scene="freeze_pr",
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": pr.get("baseRefName"),
        "base_oid": pr["baseRefOid"],
        "head_ref": pr["headRefName"],
        "head_oid": pr["headRefOid"],
    }


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
        rollup = _classify_rollup(state.get("statusCheckRollup") or [])
        if mergeable != "UNKNOWN" or any(rollup):
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
    pending, failed = _classify_rollup(state.get("statusCheckRollup") or [])
    if failed:
        failed_names = [entry["name"] for entry in failed]
        failure_report._raise_if_preexisting_ci_failure(
            pr.get("_source_repo", source_repo or ""), failed_names,
            pr.get("base_oid"),
        )
        raise GateCIFailure(
            f"delivery gate: CI check '{failed_names[0]}' "
            f"failed on PR #{pr['number']}: "
            + ", ".join(_render_check(entry) for entry in failed)
        )
    if pending:
        detail = ", ".join(_render_check(entry) for entry in pending)
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
    if not _is_ancestor(merge_commit, f"origin/{base_branch}", cwd=worktree):
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
        if not _is_ancestor(pushed_head, f"{merge_commit}^2", cwd=worktree):
            return "unknown", str(commits)
        command = ["git", "rev-list", "--count", pushed_head,
                   "--not", f"{merge_commit}^1"]
        if pushed_base is not None:
            if not _is_ancestor(pushed_base, f"{merge_commit}^2",
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


def log_recovery_ci_status(pr: dict, repo: str) -> None:
    """Record the recovered PR's current check status without check output.

    This is observability only: a GitHub status lookup must never decide
    whether the recovered review runs.
    """
    try:
        checks = commit_check_runs(repo, pr["head_oid"])
        summary = [
            f"{check.get('name', '?')}={check.get('status', '?')}/"
            f"{check.get('conclusion', '?')}"
            for check in checks if isinstance(check, dict)
        ]
    except Exception as exc:
        event(
            "review_recovery_ci_status_failed", level=logging.WARNING,
            pr=pr.get("number", "?"), error=str(exc),
        )
        return
    event(
        "review_recovery_ci_status", pr=pr["number"],
        checks=",".join(summary) or "none",
    )


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


def _round_scene_block(resumed: dict, pr_url: str, round: int,
                       *, base_advance_round: int | None = None,
                       verdict_head_unknown_round: int | None = None) -> str:
    """The updated scene block a completed round carries to the next resume.

    The round comment is the budget's write path: embedding
    the scene with `review_round=round` makes THAT comment the latest
    scene, so the next tick's `resume_scene` reads the advanced count —
    GitHub stays the only state store, no second record exists.
    """
    return scene.render(scene.Scene(
        run_id=validate_run_id(resumed["run_id"]),
        base_branch=resumed["base_branch"],
        base_sha=resumed["base_sha"],
        pr_url=pr_url,
        external=resumed.get("external", ""),
        review_round=round,
        base_advance_round=(
            resumed.get("base_advance_round", 0)
            if base_advance_round is None else base_advance_round
        ),
        verdict_head_unknown_round=(
            resumed.get("verdict_head_unknown_round", 0)
            if verdict_head_unknown_round is None
            else verdict_head_unknown_round
        ),
    ))


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: config_domain.RunnerConfig, source_repo: str,
                              number: int, title: str, priority: str,
                              *, scene: dict,
                              previous_comments: list[dict] | None = None,
                              merge_only: bool = False,
                              checks: list | None = None,
                              review_record: tuple[str, str] | None = None,
                              issue_body_sha256: str | None = None) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    Issue #1626: the review record (reviewed_head plus the hash of the
    Issue body the session rendered) authorizes exactly one later
    merge-only tick. Every exit drops it except a deferral (pending CI,
    UNKNOWN mergeability, or a head pushed in this tick): findings, a
    malformed verdict, an exception, a gate failure, the ai-blocked skip
    and the completed merge all clear it, so a stale pass can never
    authorize a later merge.
    """
    keep_review_record: list[bool] = []
    try:
        return _review_and_merge_round(
            worktree, branch, base_branch, config, source_repo, number,
            title, priority, scene=scene,
            previous_comments=previous_comments, merge_only=merge_only,
            checks=checks, review_record=review_record,
            issue_body_sha256=issue_body_sha256,
            keep_review_record=keep_review_record,
        )
    finally:
        if not keep_review_record:
            run_state.clear_reviewed(worktree)


def _review_and_merge_round(worktree: Path, branch: str, base_branch: str,
                            config: config_domain.RunnerConfig, source_repo: str,
                            number: int, title: str, priority: str,
                            *, scene: dict,
                            previous_comments: list[dict] | None = None,
                            merge_only: bool = False,
                            checks: list | None = None,
                            review_record: tuple[str, str] | None = None,
                            issue_body_sha256: str | None = None,
                            keep_review_record: list[bool]) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    `title` is the issue's GitHub title: the review
    progress scenes (ensure, findings, merged) show `#<number> <title>`
    like every other scene; it is required, never fabricated.

    `scene` is the delivery's recovered resume scene (run_id, base,
    PR URL, `review_round`, `scene_at`): the round budget reads
    `review_rounds.effective_review_round` of it — the scene's count with
    a maintainer recovery after the scene reset (Issue #483) — so the
    budget, the round in progress and every failure report show one
    number (Issue #1779). The scene is the waiting primitive's state
    anchor, and every round comment carries the updated scene block, so
    the next resume continues the count.

    The delivery step calls this while the PR is open and the Issue
    awaits review (`ai-pr-opened`) or awaits the next review session
    (`ai-fix-needed`). It freezes the PR, runs the independent review
    (streamed, role=review), and then:

    - clean verdict -> the reviewer may have fixed findings IN THE SAME
      SESSION and pushed the task branch, so the PR is
      RE-FROZEN before the merge gate: the gate (latest-base ancestor,
      one-shot CI read, mergeable, head match,
      `gh pr merge --match-head-commit`) then runs against the head the
      verdict actually covers; confirm the merge landed on
      origin/<base>, sync the deployment checkout, label the Issue
      `ai-merged`; returns True. An EXTERNAL takeover (the scene marks
      `external`) NEVER merges: its clean verdict ends at the triage
      stop below — the review conclusion stays on the Issue and the
      ticket is labeled `ai-blocked` for the maintainer's acceptance
      decision (Issue #842, decision D2); returns False;
    - an intermediate gate state (CI still pending on the reviewed head,
      or mergeability still UNKNOWN) -> one journal line and return
      without a comment or a label change: "pending" is a
      state, not a failure — the next tick re-reads it, the round budget
      does not advance; returns False;
    - `blocked_on_human_decision` -> raise `HumanDecisionRequired` with
      the finding note and fix direction; the caller marks the Issue
      `ai-blocked` immediately, without recording another review round;
    - Blocker/Major findings the reviewer could not fix in-session ->
      comment them to Issue and PR (the comment carries the updated
      scene) and label the Issue `ai-fix-needed`; the next tick resumes
      the same PR with the next round — no cold-start fixer, no third
      review; returns False;
    - a gate failure because the head is behind the latest base, has a
      merge conflict, or its CI is red -> label the Issue
      `ai-fix-needed` with the finding (the next review session absorbs
      the latest base in-session or repairs the red CI); returns False;
    - missing/malformed verdict -> raise; the caller keeps the Issue in
      the automatic fix loop (`ai-fix-needed`). A mismatched head is
      recoverable when it names another real commit, and an unknown
      object gets two retries per review run before becoming terminal
      (`ai-blocked`);
    - an exhausted round budget -> raise `UnrecoverableDeliveryError`
      (the bounded loop is a human decision, not a
      recoverable failure); the caller marks the Issue `ai-blocked`
      with the explicit reason.
    """
    marker = run_marker(config.run_id)
    # The queue entry the claim scanned: the repository's resolved
    # `dispatch_label` (default `ai-ready`), never a second constant
    # (Issue #1775).
    ready_label = config.dispatch_label or READY_LABEL
    # The round budget lives in the scene: `review_round` counts the
    # COMPLETED rounds recorded by the round comments' scene blocks, with
    # a maintainer's blocked -> ai-fix-needed recovery reset applied — ONE
    # decision owned by `review_rounds` (Issue #1779): the budget, the
    # round in progress and failure reports share it (Issue #1783).
    scene_round = int(scene["review_round"])
    rounds = effective_review_round(scene, number=number, repo=source_repo)
    recovered = rounds != scene_round
    base_advance_rounds = (
        0 if recovered else int(scene.get("base_advance_round", 0))
    )
    if rounds >= MAX_REVIEW_ROUNDS and not merge_only:
        event(
            "review_rounds_exhausted", level=logging.ERROR,
            issue=number, rounds=rounds,
            terminal="expected_human_decision",
        )
        # The loop is bounded by MAX_REVIEW_ROUNDS on purpose
        # — after 5 rounds without a clean verdict the remaining findings
        # need a human decision, so the AI cannot safely continue this PR.
        raise ReviewRoundsExhausted(
            f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
            "without a clean verdict; the bounded loop is a human "
            "decision, so the AI cannot safely continue this PR"
        )
    round = rounds if merge_only else rounds + 1
    scene["review_round_running"] = round  # the failure report's source
    external_pr_url = scene["pr_url"] if scene.get("external") else None
    pr = freeze_pr(
        worktree, branch, base_branch,
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    # Issue #1626: a record whose reviewed head and rendered-Issue-body hash
    # both still match authorizes THIS tick to merge without a session.
    merge_only_from_record = (
        review_record is not None
        and not merge_only
        and not scene.get("external")
        and issue_body_sha256 == review_record[1]
        and pr["head_oid"] == review_record[0]
    )
    # Issue #877: the absorb contract is armed only when GitHub reports the
    # PR CONFLICTING at round start (one mergeable read, Issue #1626). A
    # head that is merely behind is absorbed by the Runner's gate, not by
    # the session; UNKNOWN does not arm, and an unreadable read leaves the
    # round unarmed (the gate's own fresh-fetch ancestor check still guards
    # the merge).
    absorb_required = False
    if not merge_only and not merge_only_from_record:
        try:
            absorb_required = pr_view(
                pr["number"], "mergeable", cwd=worktree,
            ).get("mergeable") == "CONFLICTING"
        except (subprocess.CalledProcessError, ValueError, TypeError):
            absorb_required = False
    # A fix push whose round ended before the re-freeze record (a
    # findings verdict, or a malformed verdict head) still left an
    # engine-pushed head on the remote (Issue #833). When the frozen
    # remote head is exactly this worktree's checked-out head, only
    # this worktree's own sessions push it — adopt it into the push
    # history. The adoption is internal-only: an external takeover's
    # worktree legitimately starts on the contributor's foreign head,
    # whose base the claim recorded instead; an external push leaves
    # the local head behind and is never adopted. The recorded-head
    # check runs first: the common already-recorded round never pays a
    # git call.
    if (not scene.get("external")
            and read_pushed_head(worktree) != pr["head_oid"]
            and pr["head_oid"] == run_command(
                ["git", "rev-parse", "HEAD"], cwd=worktree)):
        run_state.record_pushed_head(worktree, pr["head_oid"])
        event(
            "pushed_head_recorded", pr=pr["number"],
            head=pr["head_oid"], round=round,
        )
    if recovered:
        # Only the explicit recovery reset reaches this branch. Check the
        # latest PR CI before spending the newly granted review budget.
        log_recovery_ci_status(pr, source_repo)
    publisher = ProgressPublisher(
        number, source_repo, config.run_id, run_command=run_command,
        language_code=language.of(config), status_url=config.status_url,
    )
    publish = functools.partial(
        _safe_publish, run_id=config.run_id, issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
    )
    started = time.monotonic()
    # The whole review/merge phase PATCHes the run's live progress
    # comment, so the publisher above carries the bound message language
    # too (#1660/#1687): the header and field labels follow it exactly
    # like the implementation phase's.
    ctx = RunContext(
        run_id=config.run_id, issue=number, branch=branch,
        worktree=worktree, source_repo=source_repo,
    )
    # The step's own updates (this ensure, the merge gate's `checks`
    # stage, the findings/merged finishes) render through one call shape
    # (Issue #1657). Publishing is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    round_update = step_publisher(
        publisher, ctx, role=ROLE_REVIEW, title=title,
        started=started, priority=priority, checks=checks,
    )
    publish(action=lambda: round_update(pr["url"], round))
    if merge_only or merge_only_from_record:
        verdict = {
            "verdict": "pass", "head": pr["head_oid"],
            "findings": [], "blockers": 0, "majors": 0,
        }
        review_summary = (
            language.text(
                language.of(config), "review.summary_human_action",
            ) if merge_only else language.text(
                language.of(config), "review.summary_pass_recorded",
                head=pr["head_oid"],
            )
        )
        reviewed_body_sha256 = None
    else:
        # Issue #1626: a record must never outlive the session that
        # replaces it, even if the process dies mid-session.
        run_state.clear_reviewed(worktree)
        result = pi_session.run_review(
            ctx, pr, config, round,
            progress=LiveProgressThrottle(
                ctx, publisher, title=title, role=ROLE_REVIEW,
                started=started, pr_url=pr["url"], review_round=round,
                priority=priority, checks=checks,
            ),
        )
        # Drop any record the session wrote: only the Runner's own record
        # (written below on a pass) may authorize a later merge.
        run_state.clear_reviewed(worktree)
        output = result.output
        reviewed_body_sha256 = result.issue_body_sha256
        verdict = parse_review_verdict(output)
        fixed_findings = len(verdict["findings"])
        review_summary = (
            language.text(language.of(config), "review.summary_pass_clean")
            if fixed_findings == 0
            else language.text(
                language.of(config), "review.summary_pass_fixed",
                findings=fixed_findings,
            )
        )
        event(
            "review", pr=pr["number"], round=round,
            verdict=verdict["verdict"], blockers=verdict["blockers"],
            majors=verdict["majors"],
        )
    if verdict["verdict"] == "blocked_on_human_decision":
        decisions = "; ".join(
            f"note: {finding.get('note', '')}; "
            f"fix: {finding.get('fix', '')}"
            for finding in verdict["findings"]
        )
        actions = "; ".join(
            str(finding.get("fix", "")).strip()
            for finding in verdict["findings"]
            if str(finding.get("fix", "")).strip()
        )
        # No fix: the handler supplies the catalog action (Issue #1701).
        raise HumanDecisionRequired(
            "review requires human decision: " + decisions,
            action=actions,
        )
    if review_has_findings(verdict):
        # The reviewer could not make the PR mergeable in this session
        # (findings are fixed in the same session; reaching
        # this branch means the fix was not verifiable or not this
        # session's to decide). The Issue moves to the explicit
        # fix-needed state: the next review session retries the same PR
        # (no cold-start fixer), and the round budget bounds the loop.
        event(
            "review_findings_unfixed", pr=pr["number"], round=round,
        )
        body = review_round_comment_body(
            marker, round, pr["number"], verdict["blockers"],
            verdict["majors"], verdict["findings"],
            _round_scene_block(
                scene, pr["url"], round,
                verdict_head_unknown_round=0,
            ),
            repo=source_repo, previous_comments=previous_comments,
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        )
        comment_issue(number, repo=source_repo, body=body)
        failure_report.comment_pr(pr["number"], repo=source_repo, body=body)
        # The findings publishing is bypass — a 404 here
        # must not stop the `ai-fix-needed` transition below (the next
        # review session retries the same PR either way).
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "review.findings_milestone",
                    round=round, blockers=verdict["blockers"],
                    majors=verdict["majors"], pr_number=pr["number"],
                )
            ),
        )
        publish(action=lambda: round_update(
            pr["url"], round, outcome=language.text(
                language.of(config), "review.findings_outcome",
                round=round, blockers=verdict["blockers"],
                majors=verdict["majors"],
            ),
        ))
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=issue_labels(number, source_repo),
        )
        return False
    # The reviewer fixes findings in the same session and
    # pushes the task branch, so the head the verdict covers may be
    # NEWER than the frozen head. Re-freeze before the merge gate: the
    # gate then checks the latest-base ancestor, mergeability and the
    # exact reviewed head against the current remote head, and merges
    # only that head via --match-head-commit.
    refrozen = freeze_pr(
        worktree, branch, base_branch,
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    if refrozen["head_oid"] != pr["head_oid"]:
        event(
            "review_head_advanced", pr=pr["number"], round=round,
            frozen=pr["head_oid"], reviewed=refrozen["head_oid"],
        )
    # The clean verdict is bound to the head it covers. The
    # gate below merges exactly `refrozen["head_oid"]`, so a verdict
    # naming any other head (forged by injected text, replayed from an
    # older round, or stale after a fix the reviewer forgot to state)
    # never merges: it is a malformed verdict. The object probe below
    # distinguishes a recoverable branch race from a terminal unknown head.
    if verdict["head"] != refrozen["head_oid"]:
        # A real alternate commit means the branch moved during review and
        # remains recoverable. An object that is not a commit cannot be a
        # race: it is a malformed/model-invented verdict and retrying the
        # same review would only reproduce the dead loop (Issue #988).
        object_probe = run_command(
            ["git", "cat-file", "-e", f"{verdict['head']}^{{commit}}"],
            cwd=worktree, check=False, timeout=10,
        )
        if object_probe.returncode != 0:
            unknown_round = (
                int(scene.get("verdict_head_unknown_round", 0)) + 1
            )
            scene["verdict_head_unknown_round"] = unknown_round
            event(
                "review_verdict_head_unknown", level=logging.WARNING,
                pr=pr["number"], verdict_head=verdict["head"],
                round=unknown_round,
            )
            if unknown_round >= 3:
                raise UnrecoverableDeliveryError(
                    f"review verdict points to unknown object {verdict['head']} "
                    f"on attempt {unknown_round}; the verdict head is not a "
                    "commit in the delivery repository; human intervention "
                    "is required"
                )
            raise ValueError(
                f"review verdict points to unknown object {verdict['head']} "
                f"(unknown-head attempt {unknown_round}/3); retrying review"
            )
        raise ValueError(
            f"review verdict head {verdict['head']} does not match the "
            f"PR head {refrozen['head_oid']}; the merge gate only merges "
            "the head the verdict covers"
        )
    if refrozen["head_oid"] != pr["head_oid"]:
        # The advanced head is verdict-covered (checked above), so it
        # is this round's own review/fix output — an engine-pushed
        # head (Issue #833): the merge record's external_commits must
        # not count the session's own fixes as external commits.
        run_state.record_pushed_head(worktree, refrozen["head_oid"])
    if (not scene.get("external")
            and not merge_only and not merge_only_from_record
            and refrozen["head_oid"] != pr["head_oid"]):
        # Issue #1626: a head pushed in this tick is never merged in this
        # tick — its CI has not registered yet. Record the pass and defer;
        # the next tick with green CI on that head merges without a session.
        run_state.record_reviewed(
            worktree, refrozen["head_oid"], reviewed_body_sha256,
        )
        keep_review_record.append(True)
        event(
            "review_merge_deferred", pr=refrozen["number"], round=round,
            reason=(
                f"the review session pushed head {refrozen['head_oid']}; "
                "its CI is not read in this tick"
            ),
        )
        return False
    if scene.get("external"):
        # D2 (Issue #842): an external contribution's clean verdict is
        # the engine's FINAL output — the PR is NEVER auto-merged and
        # no Milestone is ever written. The review conclusion stays on
        # the Issue and the ticket stops at `ai-blocked` (the human
        # decision point): code quality is the engine's judgment, but
        # accepting the contribution and picking its version is the
        # maintainer's.
        state, rollup = pr_delivery_rollup(pr["url"], source_repo)
        checks = ", ".join(_check_summaries(rollup)) or "none"
        bound = language.of(config)
        body = (
            f"{marker}\n"
            + language.text(
                bound, "review.external_pr_round", round=round,
                pr_number=pr["number"], verdict=verdict["verdict"],
                blockers=verdict["blockers"], majors=verdict["majors"],
                minors=verdict["minors"], state=state, checks=checks,
            ) + "\n"
            + user_summary.summary_text(
                status_url=config.status_url, issue=number, title=title,
                language_code=bound,
            ) + "\n"
            + language.text(bound, "review.review_report_open")
            + "````\n" + output.rstrip("\n") + "\n````\n\n</details>\n\n"
            + language.text(
                bound, "review.external_conclusion", run_id=config.run_id,
            )
        )
        post_blocked_comment(
            number, repo=source_repo, body=body,
            current_labels=issue_labels(number, source_repo),
            language_code=bound, ready_label=ready_label,
        )
        event(
            "external_takeover_triage", pr=pr["number"], round=round,
            verdict=verdict["verdict"],
        )
        return False
    def handle_gate_failure(message: str, *, ci_failure: bool,
                            absorb_abandoned: bool = False) -> None:
        # Issue #877: the machine-checked absorb violation rides the same
        # counted round comment, so the next session and the human both see
        # that the round neither merged the base nor reported the
        # abandonment — the gate now names the silent abandon instead of
        # showing only the final behind/conflict state.
        violation = ""
        if absorb_abandoned:
            violation = (
                language.text(
                    language.of(config), "review.absorb_violation",
                    base_branch=base_branch,
                )
            )
        next_base_advance_round = base_advance_rounds + (0 if ci_failure else 1)
        base_advance_exhausted = (
            not ci_failure and next_base_advance_round > MAX_BASE_ADVANCE_ROUNDS
        )
        if ci_failure:
            gate_messages = [
                language.text(
                    language.of(config), "review.gate_ci_blocked",
                    message=message, run_id=config.run_id,
                ),
                language.text(
                    language.of(config), "review.gate_next_step",
                    base_branch=base_branch,
                ),
            ]
        else:
            gate_messages = [
                language.text(
                    language.of(config), "review.gate_blocked",
                    message=message, run_id=config.run_id,
                ),
                (language.text(
                    language.of(config), "review.gate_exhausted",
                ) if base_advance_exhausted else language.text(
                    language.of(config), "review.gate_next_step",
                    base_branch=base_branch,
                )),
            ]
        if violation:
            gate_messages.append(violation.strip())
        body = review_round_comment_body(
            marker, round, pr["number"], 0, 0, [],
            _round_scene_block(
                scene, pr["url"],
                round if ci_failure else int(scene["review_round"]),
                base_advance_round=(
                    scene.get("base_advance_round", 0)
                    if ci_failure else min(
                        next_base_advance_round, MAX_BASE_ADVANCE_ROUNDS,
                    )
                ),
                verdict_head_unknown_round=0,
            ),
            messages=gate_messages, repo=source_repo,
            heading=(None if ci_failure else language.text(
                language.of(config), "review.base_advance_heading",
                round=next_base_advance_round, pr_number=pr["number"],
            )),
            # A base-advance retry reuses the SAME round scene: it is a
            # review failure, not a completed round, so it must not
            # consume a round record (Issue #1659).
            kind=(COMMENT_KIND_REVIEW_ROUND if ci_failure
                  else COMMENT_KIND_FAILURE),
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        )
        # CI evidence is best-effort observability.  A GitHub comment
        # outage must not prevent the required ai-fix-needed transition.
        try:
            comment_issue(number, repo=source_repo, body=body)
            failure_report.comment_pr(pr["number"], repo=source_repo, body=body)
        except Exception:
            LOGGER.exception(
                "delivery_ci_evidence_publish_failed pr=%s run_id=%s",
                pr["number"], config.run_id,
            )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=issue_labels(number, source_repo),
        )
        if base_advance_exhausted:
            event(
                "base_advance_rounds_exhausted", level=logging.ERROR,
                issue=number, rounds=next_base_advance_round,
                terminal="expected_human_decision",
            )
            raise UnrecoverableDeliveryError(
                "base-advance retry loop exhausted after "
                f"{MAX_BASE_ADVANCE_ROUNDS} base advances; the bounded loop "
                "is a human decision, so the AI cannot safely continue this PR"
            )

    # Issue #1626: record the pass before the gate, so the gate's own base
    # absorb can point the record at the pushed head and defer.
    if not merge_only and not merge_only_from_record:
        run_state.record_reviewed(
            worktree, refrozen["head_oid"], reviewed_body_sha256,
        )
    # The Runner has entered the merge gate's CI-check action on the
    # reviewed head: the one live comment reports that stage before the
    # gate waits for CI and merges (Issue #1657). Best effort like every
    # progress update - a publication failure cannot affect the merge.
    publish(action=lambda: round_update(
        pr["url"], round, step="checks",
    ))
    try:
        merged = merge_gate(
            worktree,
            {**refrozen, "_source_repo": source_repo},
            base_branch, repo_dir=config.repo_dir,
            issue_number=number, run_id=config.run_id,
            language_code=language.of(config),
        )
    except MergeHandoffRequired as exc:
        # A known policy blocker is a successful, resumable handoff.
        return handle_merge_handoff(
            number=number, repo=source_repo, pr_number=refrozen["number"],
            head=refrozen["head_oid"], preflight=exc.preflight, marker=marker,
            language_code=language.of(config), ready_label=ready_label,
            status_url=config.status_url, title=title,
        )
    except DeliveryDeferred as exc:
        # A pending check or an UNKNOWN mergeability on the
        # reviewed head is an intermediate state, not a failure — the
        # next tick re-reads it. No comment, no label change, no round
        # consumed: the scene's counter only advances on round comments.
        event(
            "review_merge_deferred", pr=refrozen["number"], round=round,
            reason=str(exc),
        )
        keep_review_record.append(True)
        return False
    except MergeBlockedByIssueLabel as exc:
        # Issue #1504: a maintainer labelled the source Issue `ai-blocked`
        # while the PR was in review. The gate already posted the skip
        # comment; the labels stay as the human set them and the PR stays
        # open (no `ai-merged`, no `ai-fix-needed`). The Issue is terminal
        # for the engine, so no next tick re-claims it.
        event(
            "review_merge_skipped_issue_blocked",
            pr=refrozen["number"], round=round, reason=str(exc),
        )
        return False
    except RecoverableMergeGateError as exc:
        # Issue #877 machine check: an armed round whose verdict was `pass`
        # and whose head STILL lacks the latest base (re-read against the
        # gate's fresh fetch) neither absorbed nor reported findings — log
        # the structured fact and name the violation in the round comment.
        absorb_abandoned = False
        if absorb_required:
            try:
                absorb_abandoned = not _is_ancestor(
                    f"origin/{base_branch}", refrozen["head_oid"], cwd=worktree,
                )
            except subprocess.CalledProcessError:
                absorb_abandoned = False
        if absorb_abandoned:
            event(
                "review_absorb_abandoned", level=logging.ERROR,
                pr=pr["number"], round=round, head=refrozen["head_oid"],
                base_branch=base_branch,
            )
        handle_gate_failure(str(exc), ci_failure=False,
                            absorb_abandoned=absorb_abandoned)
        return False
    except GateCIFailure as exc:
        # Issue #906: the recoverable CI failure is identified by TYPE,
        # never by matching text inside the message — rewording a gate
        # message cannot change which recovery path runs. `GateCIFailure`
        # is a sibling of `PreExistingCIFailure` and
        # `RecoverableMergeGateError`, so an already-red main and a
        # behind-base/conflict head propagate untouched and fail fast.
        handle_gate_failure(str(exc), ci_failure=True)
        return False
    # Issue #1802: use the gate's confirmation; a success reads it here.
    confirmed = merged.get("confirmation") or confirm_merged(
        worktree, merged, base_branch, repo_dir=config.repo_dir,
    )
    # The merged-as-is fact (Issue #833): every count failure degrades
    # to `unknown` inside merge_commit_metrics — a landed merge is
    # never re-failed by its own record.
    external_commits, pr_commits = merge_commit_metrics(
        worktree, confirmed["merge_commit"], read_pushed_head(worktree),
        read_pushed_base(worktree),
        merge_method=merged.get("merge_method", "--merge"),
    )
    # The merged publishing is bypass — the GitHub merge already landed; a
    # 404 here must not stop `ai-merged` and the merged record below.
    publish(
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"external_commits={external_commits} "
            f"commits={pr_commits})"
        ),
    )
    publish(action=lambda: round_update(
        merged["url"], round, step="merge", review=review_summary,
        outcome=language.text(
            language.of(config), "review.delivered_outcome",
            pr_url=merged["url"],
            merge_commit=confirmed["merge_commit"], round=round,
        ),
    ))
    # The GitHub merge already landed. Read the labels after it (a resumed
    # fix round may carry `ai-in-progress` and `ai-fix-needed`), record
    # `ai-merged` — clearing every state label present — before touching the
    # systemd checkout: a checkout that cannot fast-forward is runner ops.
    apply_label_patch(
        number, repo=source_repo, event=EVENT_MERGED,
        current_labels=issue_labels(number, source_repo),
        ready_label=ready_label,
    )
    comment_issue(
        number, repo=source_repo,
        body=merged_pr_comment_body(
            config.run_id, merged["url"], confirmed["merge_commit"], round,
            external_commits, pr_commits, base_branch, review_summary,
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        ),
    )
    if confirmed["state"] != "MERGED":  # Issue #1801: nothing else closes it
        run_state.close_issue_after_landed_merge(
            number, repo=source_repo, merge_commit=confirmed["merge_commit"])
    try:
        # A config built by config_domain.load_config always carries both paths (the
        # deploy home defaults to the repo dir). A split layout does not
        # use the delivery checkout for the next tick; same-checkout
        # configs retain the existing engine-channel guard below.
        if (
            # ``Path(".")`` is the placeholder on hand-built partial
            # configs; only config_domain.load_config's resolved path represents an
            # explicitly configured split deployment.
            config.deploy_home != Path(".")
            and config.repo_dir != config.deploy_home
        ):
            # The delivery checkout is not the engine source in a split
            # deployment layout. The next tick loads code from deploy_home,
            # so syncing this delivery checkout has no runtime effect.
            event(
                "base_checkout_sync_skipped", repo_dir=config.repo_dir,
                base_branch=base_branch,
                reason="repo_dir_is_not_deploy_home",
            )
        elif (
            config.engine_source_track is not None
            and config.deploy_home is not None
            and config.repo_dir == config.deploy_home
            and config.engine_source_track != "main"
        ):
            # The delivery checkout IS the engine source in
            # the dogfood layout, and the engine channel is locked (or
            # tracks a non-main branch) — fast-forwarding it to
            # origin/<base_branch> would break the lock. The next tick's
            # ExecStartPre engine sync owns this checkout instead.
            event(
                "base_checkout_sync_skipped", repo_dir=config.repo_dir,
                engine_source_track=config.engine_source_track,
                base_branch=base_branch,
            )
        else:
            sync_base_checkout(config.repo_dir, base_branch)
    except RuntimeError:
        LOGGER.exception(
            "base_checkout_sync_failed after merge pr=%s repo_dir=%s; "
            "the delivery already landed on origin/%s",
            merged["url"], config.repo_dir, base_branch,
        )
    return True


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


def _run_review_round(
    pr_url: str, issue: dict, config: config_domain.RunnerConfig, source_repo: str,
    *, checks: list | None = None,
) -> bool | None:
    """Run ONE review round of an open-PR delivery.

    The delivery step's round body: read the delivery labels ONCE per
    round, repair a lost `ai-in-progress` transition, gate on the
    resumable opened-PR states, then recover the trusted scene,
    validate the frozen base, derive the worktree/branch, run the
    independent review and classify any failure.

    Returns True when this round merged the PR (terminal success);
    False when the delivery stays open for a LATER TICK (`ai-fix-needed`
    after findings or a failed gate, or a deferred merge — the label
    transition happens inside the review itself, a defer writes none);
    and None when a terminal state was already handled and the caller
    must release the slot and return: an unrecoverable precondition
    (`ai-blocked`), a recoverable failure's full `ai-fix-needed` scene
    (the next tick resumes the same run, branch, worktree and PR), a
    failed `ai-in-progress` label repair, or an open PR without a
    resumable delivery label (both `ai-blocked`).
    """
    number = int(issue["number"])
    title = issue["title"]
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    priority = issue_priority(issue)
    ready_label = config.dispatch_label or READY_LABEL

    def block_label_inconsistency(labels: list[str], reason: str) -> None:
        event(
            "delivery_label_inconsistent", level=logging.ERROR,
            issue=number, pr=pr_url, reason=reason,
        )
        body = language.text(
            language.of(config), "review.label_repair_failed",
            pr_url=pr_url, reason=reason,
        )
        if marker:
            body = f"{marker}\n{body}"
        # The blocked scene carries the readable summary before the
        # implementation diagnostics (Issue #1826).
        body = user_summary.append_summary(
            body, status_url=config.status_url, issue=number, title=title,
            language_code=language.of(config),
        )
        post_blocked_comment(
            number, repo=source_repo, body=body,
            current_labels=labels, language_code=language.of(config),
            ready_label=ready_label,
        )

    labels = issue_labels(number, source_repo)
    if IN_PROGRESS_LABEL in labels:
        try:
            apply_label_patch(
                number, repo=source_repo, event=EVENT_PR_OPENED,
                current_labels=labels,
            )
        except Exception as exc:
            LOGGER.exception(
                "issue=%s delivery_label_repair_failed pr=%s",
                number, pr_url,
            )
            block_label_inconsistency(labels, str(exc))
            return None
        labels = [label for label in labels if label != IN_PROGRESS_LABEL]
        labels.append(PR_OPENED_LABEL)
        event(
            "delivery_label_repaired", issue=number, pr=pr_url,
            **{"from": IN_PROGRESS_LABEL, "to": PR_OPENED_LABEL},
        )
    if not (is_resumable(labels)
            and not needs_human_intervention(labels)
            and MERGED_LABEL not in labels):
        block_label_inconsistency(
            labels,
            "open PR has no resumable delivery label",
        )
        return None
    # The human acceptance gate — checked BEFORE the scene,
    # the worktree or any review session. One label read decides; the
    # column-2 re-derivation is local evidence reads only. With the
    # gate on and no `ai-human-review` label, a non-empty column 2
    # holds the delivery: the waiting primitive returns the ticket to
    # `ai-ready` (the opened-PR anchor stays, so the resume scan keeps
    # finding it), the round ends here, the caller releases the slot
    # and every next tick costs one label read — never a review
    # session, never an `ai-fix-needed` round (the review-round budget
    # is not consumed by waiting). An empty column 2 needs no human and
    # falls through to the normal review; a missing worktree falls
    # through too so the existing recovery semantics stay intact.
    if config.human_review_gate and HUMAN_REVIEW_LABEL not in labels:
        gate_worktree = worktree_path(
            config.repo_dir, source_repo, number, run_id,
        )
        if gate_worktree.is_dir() and _delivery_checklist(
            gate_worktree, config, test_status=test_status(checks),
        )["column2"]:
            # The waiting guard asks for the repository RESOLVED claim
            # label, never the host default (Issue #1775): in a repository
            # whose dispatch_label is a custom queue label the ticket
            # already carries it, so a second tick must not rewrite the
            # waiting patch.
            if ready_label not in labels:
                apply_label_patch(
                    number, repo=source_repo,
                    event=EVENT_HUMAN_REVIEW_WAITING,
                    current_labels=labels, ready_label=ready_label,
                )
            event(
                "human_review_waiting", issue=number, pr=pr_url,
            )
            return None
    # The PR is in an opened-PR review state: run the
    # independent review of the frozen PR on the same run
    # `ai-pr-opened` awaits review; `ai-fix-needed`
    # awaits the next review session after a finding or a base
    # conflict (the review session fixes findings in
    # the same session, so both states run the same review). A
    # clean verdict re-freezes the head, merges and returns
    # True (terminal); unfixed findings or a behind/conflict
    # gate label the Issue `ai-fix-needed` and the next
    # iteration re-runs the same independent review. A review
    # that cannot run is classified: a RECOVERABLE
    # failure (Pi execution failure, model wait, runner
    # exception, missing/malformed verdict, missing worktree,
    # unpushed local commit) keeps the Issue in the automatic
    # fix loop — `ai-fix-needed` with the full scene (run_id,
    # PR, branch, worktree, session, phase, last activity,
    # concrete error) on Issue AND PR, and the next timer
    # resumes the same run, branch, worktree and PR. Only an
    # explicit `UnrecoverableDeliveryError` (an external
    # precondition the AI cannot safely judge or fix: an
    # unrecoverable scene, a base-branch config change,
    # exhausted rounds) is terminal: the Issue is marked
    # `ai-blocked` ALONE (the opened-PR state label,
    # `ai-pr-opened` or `ai-fix-needed`, is removed) with the
    # explicit reason why automatic recovery is impossible.
    worktree = None
    branch = None
    scene = None
    try:
        try:
            comments = issue_comments(number, repo=source_repo)
            scene = run_state.resume_scene(comments, repo=source_repo)
        except ValueError as scene_exc:
            # Without the trusted scene the runner
            # cannot derive run_id, branch, worktree or PR and
            # cannot start a review session — an external
            # precondition the AI cannot fix by itself (the
            # same terminal state as the scan-time
            # `block_scene_failure`), so the handler below
            # marks the Issue ai-blocked with the explicit
            # reason.
            # The reason names the condition and the ONE missing
            # precondition; the resume step itself lives only in the
            # catalog (Issue #1724: a second copy here once offered the
            # reader a non-working alternative).
            raise UnrecoverableDeliveryError(
                f"the resume scene is unrecoverable "
                f"({scene_exc}); the runner cannot derive "
                "run_id, branch, worktree or PR without the "
                "trusted 'Orbi opened PR' comment, so "
                "it cannot start a review session; a human "
                "must restore the trusted 'Orbi opened PR' "
                "scene comment"
            ) from scene_exc
        # The scene freezes the base the PR
        # was opened against. The config may have moved on (or
        # the comment is stale): reviewing or merging a PR
        # frozen on another base against the configured one
        # would run the freeze/merge gate on the wrong base,
        # so fail fast before any git/Pi mutation instead of
        # silently switching bases. A base-branch change is a
        # human decision: the runner must not
        # auto-retry a PR frozen on another base, so the
        # handler below marks the Issue ai-blocked with the
        # explicit reason and both base values named.
        if scene["base_branch"] != config.base_branch:
            raise UnrecoverableDeliveryError(
                f"resume scene base_branch={scene['base_branch']} "
                f"differs from configured base_branch="
                f"{config.base_branch}; the PR is frozen on a "
                "different base and must not be reviewed or "
                "merged against the configured one — a base "
                "change is a human decision, so auto-retrying "
                "would keep failing on the same mismatch"
            )
        worktree = worktree_path(
            config.repo_dir, source_repo, number,
            scene["run_id"],
        )
        # The worktree is derived from the
        # configured repo_dir, source repo, Issue number and run id
        # (never read from a comment). A missing directory is a
        # RECOVERABLE failure: the branch still exists on the
        # remote and the worktree can be recreated (git worktree
        # add) on the next resume, so the handler below keeps the
        # Issue in the automatic fix loop (ai-fix-needed) with the
        # PR and branch preserved.
        if not worktree.is_dir():
            # The failure comment must carry the full scene
            # including the branch: the stable
            # derivation is the best available guess when the
            # worktree is gone.
            branch = task_branch(
                source_repo, number, scene["run_id"],
            )
            raise RuntimeError(f"worktree missing: {worktree}")
        # The delivery branch is a local git fact of the derived
        # worktree — the stable naming for the Runner's own
        # deliveries, the contributor's head branch for an
        # external takeover. Deriving it from the
        # worktree keeps the whole review/merge loop
        # branch-identity agnostic while the worktree path itself
        # stays comment-independent.
        branch = run_command(
            ["git", "branch", "--show-current"], cwd=worktree,
        ) or task_branch(source_repo, number, scene["run_id"])
        review_config = replace(
            config,
            base_sha=scene["base_sha"],
            run_id=scene["run_id"],
        )
        review_kwargs = {}
        if scene["review_round"] > 0:
            review_kwargs["previous_comments"] = comments
        if AWAITING_MERGE_LABEL in labels:
            review_kwargs["merge_only"] = True
        elif not scene.get("external"):
            # Issue #1626: a recorded pass on this head and this Issue
            # body lets this round merge without starting a session.
            review_record = run_state.read_reviewed(worktree)
            if review_record is not None:
                review_kwargs["review_record"] = review_record
                review_kwargs["issue_body_sha256"] = (
                    pi_session.rendered_issue_body_sha256(
                        config.prompt_review, issue.get("body") or "",
                    )
                )
        merged = review_and_merge_if_clean(
            worktree, branch, config.base_branch,
            review_config, source_repo, number,
            title=title, priority=priority, scene=scene,
            checks=checks,
            **review_kwargs,
        )
    except Exception as exc:
        detail = _failure_detail(exc)
        if isinstance(exc, (ReviewRoundsExhausted, HumanDecisionRequired)):
            # These are intentional human decision points, not Runner
            # bugs. Keep structured evidence without a traceback.
            event(
                "review_human_decision_required"
                if isinstance(exc, HumanDecisionRequired)
                else "review_rounds_exhausted_expected_terminal",
                level=logging.ERROR, issue=number, pr=pr_url,
                reason=detail,
            )
        else:
            # Real delivery failures retain traceback evidence for
            # health monitoring and diagnosis.
            LOGGER.exception(
                "issue=%s delivery_review_failed pr=%s", number, pr_url,
            )
        # The blocked comment says why and how to resume (Issue #1701).
        language_code = language.of(config)
        text = functools.partial(language.text, language_code, pr_url=pr_url)
        action = ""
        if isinstance(exc, ReviewRoundsExhausted):
            reason, action = (text("review.blocked_rounds_reason"),
                              text("review.blocked_rounds_action"))
        elif isinstance(exc, HumanDecisionRequired):
            reason = text("review.blocked_human_reason")
            if exc.action:
                fix = exc.action.strip().rstrip(".。;")
                action = text("review.blocked_human_action", fix=fix)
        else:
            reason = text("review.blocked_failed_reason", detail=detail)
        if not action and failure_report.is_unrecoverable_failure(exc):
            action = text("review.blocked_generic_action")
        # The shared classified reporter: recoverable ->
        # `ai-fix-needed` with the full scene on Issue AND PR,
        # unrecoverable -> `ai-blocked` ALONE. No wrapper: a reporting
        # failure here fails the tick fast (the slot is released by
        # `main`'s `finally`), exactly like any other Runner bug.
        failure_report.report_delivery_failure(
            exc, issue=issue, source_repo=source_repo,
            run_id=run_id, pr_url=pr_url,
            worktree=worktree, branch=branch, role=ROLE_REVIEW,
            language_code=language_code, ready_label=ready_label,
            status_url=config.status_url,
            action=action,
            reason=reason,
            diagnosis=detail, evidence=True,
            review_round=(scene or {}).get("review_round_running"),
            review_scene_block=(
                _round_scene_block(scene, pr_url, int(scene["review_round"]))
                if isinstance(scene, dict)
                and scene.get("verdict_head_unknown_round", 0) else None
            ),
        )
        return None
    if merged:
        event(
            "delivery_auto_merged", issue=number, pr=pr_url,
        )
        return True
    return False
