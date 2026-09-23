#!/usr/bin/env python3
"""One-shot bootstrap runner for Orbi.

This is intentionally small. It claims one ready GitHub Issue, gives it to
Pi in an isolated worktree, and accepts success only when one open PR exists.
After the implementer opens the PR, the Runner closes the loop itself: it
freezes the exact PR base/head SHA, runs one independent review session that
reviews the diff AND fixes Blocker/Major findings in the same session
(modify code, run tests, push the task branch — no cold-start
fixer, no third review), re-freezes the head after a clean verdict,
re-checks the merge gate against the latest remote base, and merges via
`gh pr merge --match-head-commit`. Pi never pushes the protected branch;
the Runner is the only merge actor. Any command failure is logged and
raised. There is no fallback, queue, daemon, or multi-agent framework.

Throughout the whole lifecycle the Runner publishes live progress
automatically: one per-run GitHub progress comment carrying a
hidden run marker is PATCHed in place on every activity change and at most
every 30 seconds while any Pi session (implementer or reviewer) runs, and
short milestone comments (plan ready, tests passed/failed, review findings,
merged, blocked) notify GitHub Mobile; started and PR-opened scene comments
also notify while remaining available for resume parsing. No human
command, poll or status check is part of the normal workflow.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
import uuid
from enum import Enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

# NOTE: the editable finder maps
# the WHOLE package directory `src/orbi/`, so a newly added package
# module is importable WITHOUT any reinstall — the #158 incident class
# is fixed at the root. `refresh_cli_install` lives in `orbi.cli_source`; `runner` imports it like any caller and the
# preflight stubs keep patching the module global below.
from orbi import engine_source
from orbi import milestone as milestone_bookkeeping
from orbi import claim
from orbi import config as config_domain
from orbi.engine_source import EngineSourceError
from orbi.git_transport import TransportError, check_transport
from orbi.pilot_slots import (
    acquire_slot, mark_slot_delivery, slot_dir_for, slot_held_deliveries,
    slot_occupancy,
)
from orbi.pi_activity import (
    activity_snapshot,
    format_duration,
    format_end_scene,
    format_run_scene,
    sanitize,
)
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    AWAITING_MERGE_LABEL,
    CONTENT_ONLY_LABEL,
    EPIC_LABEL,
    FIX_NEEDED_LABEL,
    HUMAN_REVIEW_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    OPS_LABEL,
    P0_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
    EVENT_BLOCKED,
    EVENT_AWAITING_MERGE,
    EVENT_CLAIM,
    EVENT_FIX_NEEDED,
    EVENT_HUMAN_REVIEW_WAITING,
    EVENT_REQUEUE,
    EVENT_RELEASE_WAITING,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    is_resumable,
    label_patch,
    needs_human_intervention,
)
from orbi import human_review
from orbi.delivery_scene import (
    EXTERNAL_PR_MARKER,
    EXTERNAL_PR_RE,
    DeliveryFacts,
    DeliveryScene,
    RunContext,
    body_markers,
    classify,
)
from orbi.repo_config import (
    REPO_CONFIG_PATH,
    RepoConfigError,
    RepoPolicy,
    load_repo_policy,
    read_repo_config,
    read_repo_config_at,
    repo_config_audit,
    resolve_policy,
)
from orbi.progress import (
    RUN_MARKER_PATTERN,
    FAILURE_MARKER_PATTERN,
    ProgressPublisher,
    _progress_body,
    _progress_state,
    _run_info_fields,
    _safe_publish,
    bump_failure_repeat,
    failure_marker,
    failure_repeat_count,
    field_block,
    format_status_comment,
    format_elapsed,
    progress_body,
    quote_value,
    read_test_result,
    run_marker,
    validate_run_id,
)
from orbi import runner_health
from orbi.scheduler import (
    MAX_RUNNER_INSTANCES,
    UnitDriftError,
    check_unit_drift,
    sync_drifted_units,
)


from orbi import pi_process
from orbi.pi_process import (
    PI_IDLE_RECOVERY_CYCLES,
    PI_IDLE_WARN_SECONDS,
    PI_MODEL_WAIT_DEAD_SECONDS,
    PI_MODEL_WAIT_PROBE_SECONDS,
    ROLE_IMPLEMENT,
    ModelWaitDeadError,
    RateLimitExhaustedError,
    RecoverablePiFailure,
    RecoverablePiProcessError,
    RecoverablePiTimeoutError,
)

# The agent's command-line grammar is one pure module (Issue #1231):
# `pi_command` owns the argv pair for every session role.
from orbi.pi_command import (
    ROLE_REVIEW,
    ROLE_TICKET,
)

# Launching a Pi session is its own module (Issue #1262): the
# launchers, their prompt/context helpers, the per-run agent dir and
# the Runner-owned runtime excludes live in `pi_session`. `runner`
# calls them through the module and never imports a launcher name into
# its own namespace (Article 3.3: `pi_session` imports no runner).
from orbi import pi_session

# The shared primitives live in the leaf modules now — the
# journal kernel (logger, run binding, subprocess seam), the GitHub
# data-access layer, the git operations layer, and the CLI-install domain.
# `runner` consumes them like any other caller; only `cli` and
# `pilot_setup` import `runner` itself.
from orbi import github, gitops, journal, release, scene
from orbi.merge_handoff import MergeHandoffRequired, is_maintainer_actionable
from orbi.cli_source import CliInstallError, refresh_cli_install
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    run_gh_read_command,
    run_gh_write_command,
    _comment_is_trusted,
    _pr_number,
    _epic_audit,
    _verify_epic_complete,
    apply_label_patch,
    close_issue,
    close_milestone,
    commit_check_runs,
    comment_issue,
    edit_issue,
    epic_issue_with_blockers,
    has_in_progress_label,
    issue_comments,
    issue_comment_rest_id,
    issue_labels,
    issue_priority,
    issue_view,
    latest_run_marker,
    line_run_markers,
    list_issues,
    list_milestones,
    milestone_issues,
    milestone_open_issue_count,
    milestone_open_issues,
    open_blocker_numbers,
    open_pr_for_branch,
    update_issue_comment,
    parse_issue_array,
    parse_issue_list,
    parse_paginated_issue_array,
    pr_comments,
    pr_delivery_status,
    pr_delivery_rollup,
    _check_summaries,
    pr_view,
)
from orbi.gitops import (
    acquire_base_sync_lock,
    create_release_worktree,
    create_worktree,
    fetch_base_ref,
    freeze_base,
    latest_run_id,
    stable_branch_exists,
    task_branch,
    worktree_path,
    _is_ancestor,
)
from orbi.journal import (
    LOGGER,
    RunIdFilter,
    clear_active_run,
    configure_logging,
    current_run_id,
    event,
    issue_context,
    log_format,
    new_run_id,
    run_command,
    run_git_network_command,
    set_active_pi,
    set_active_run,
    set_run_id,
    single_line,
    validate_run_id,
)
from orbi.release import (
    RELEASE_CI_WAIT_SECONDS,
    RELEASE_DELIVERIES_WAIT_SECONDS,
    RELEASE_SECTION,
    ROLE_RELEASE,
    ReleaseDeliveriesWaiting,
    release_target_milestone,
)


# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (see review-fix-loop skill: max 5 rounds).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_REVIEW_ROUNDS = 5
MAX_BASE_ADVANCE_ROUNDS = 5

# Automatic observability: the GitHub progress comment is
# PATCHed on every activity change and at most every 30 seconds while a
# Pi session runs, so a mobile user sees live progress without any
# command. The journal cadence is the poll interval above.
PI_HEARTBEAT_SECONDS = 30.0
# Stop-handler grace: when the Runner is stopped with SIGTERM
# while a Pi delivery is in flight, the handler must not wait forever for
# the Pi child to exit. systemd gives `TimeoutStopSec` (default 90s) before
# it SIGKILLs the whole cgroup, so an unbounded `child.wait()` on a child
# stuck in a model/network call leaves `Result=timeout` after the grace.
# The handler TERMs the child, waits at most this long, then KILLs it so
# it always reaps the child and exits with 128+SIGTERM before systemd's
# own deadline — a clean signal stop, never `failed`/`timeout`.
STOP_CHILD_GRACE_SECONDS = 15.0


# {{ISSUE_COMMENTS}} injects the Issue's trusted-comment
# timeline into the implementer/review prompt. This cap bounds how many
# trusted comments a long-discussed Issue may contribute to the task
# context; the NEWEST are kept (the latest decision lives there) and a
# dropped-older-comments count is stated inside the injected block —
# the truncation is never silent.
#
# The cap exists to bound the prompt, not to curate it: dropping a
# decision the maintainer wrote is the expensive failure, a longer
# prompt is the cheap one. Measured on this repo, the busiest Issues
# reach 18 comments — the old default of 20 truncated them at the
# margin, and the review path now shares this one bound between the
# Issue timeline and the delivery PR's trusted feedback, so the
# combined stream passes 20 routinely.

# Task-worktree reclamation: the tick-start pass removes at
# most this many worktrees per tick (oldest-closed first), so a large
# backlog drains over ticks and one tick never spends unbounded time on
# `rm -rf`.
WORKTREE_RECLAIM_MAX_PER_TICK = 25
# The default retention window (hours): a closed Issue's scene stays
# inspectable for three days before the reclamation removes it — the
# Issue's conservative option (宁可不删，不可误删).
# A closed Issue still wearing one of these was closed by a human while
# a run was (or may still be) working in its scene — never reclaim such
# a worktree.
_WORKTREE_INFLIGHT_LABELS = frozenset({
    IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL,
})
# Terminal state takes precedence over stale in-flight labels when deciding
# whether a closed Issue's unreachable worktree can be reclaimed.
_WORKTREE_TERMINAL_LABELS = frozenset({MERGED_LABEL, BLOCKED_LABEL})
# The task worktree name `worktree_path` derives: orbi-{slug}-issue-{N}-{run_id}.
_WORKTREE_NAME_PATTERN = re.compile(
    r"^orbi-(?P<slug>.+)-issue-(?P<number>\d+)-(?P<run_id>[0-9a-f]{8})$",
)


class BaseFreshness(Enum):
    """Classification shared by delivery and merge freshness checks."""

    FRESH = "fresh"
    ABSORBABLE = "absorbable"
    CONFLICTED = "conflicted"


def assess_base_freshness(worktree: Path, base_branch: str, *,
                          head: str = "HEAD",
                          reviewed_head: str | None = None,
                          mergeable: str | None = None) -> BaseFreshness:
    """Classify a delivery head against the fetched base and PR head.

    A moved reviewed head is always conflicted.  Otherwise ancestry is the
    authoritative base decision; a behind head is absorbable unless the
    already-read GitHub mergeability says that absorbing it is conflicted.
    The helper deliberately does not perform a merge or change merge policy.
    """
    if reviewed_head is not None and head != reviewed_head:
        return BaseFreshness.CONFLICTED
    if _is_ancestor(f"origin/{base_branch}", head, cwd=worktree):
        return BaseFreshness.FRESH
    if mergeable is not None and mergeable != "MERGEABLE":
        return BaseFreshness.CONFLICTED
    return BaseFreshness.ABSORBABLE


class RecoverableMergeGateError(RuntimeError):
    """A merge-gate failure the next review session can repair.

    This type deliberately identifies only merge-gate outcomes that require
    absorbing the latest base or resolving merge conflicts. Callers must not
    infer delivery control flow from the human-readable error message.
    """


class UnrecoverableDeliveryError(RuntimeError):
    """A delivery failure that is an EXTERNAL precondition the AI cannot
    safely judge or fix.

    `ai-blocked` is not the result of "one run failed": it is the
    terminal state the Runner reaches only after reading the full task
    context (code, logs, existing artifacts) and deciding that it cannot
    safely continue. The message carries the explicit reason why
    automatic recovery is impossible (missing external resource/permission/
    credential, a human decision, or a bounded loop the AI may not exceed);
    the failure comment renders it so a human sees exactly what to do.
    Every other delivery failure is recoverable and stays in the automatic
    fix loop (`ai-fix-needed`, the next timer resumes the same run, branch,
    worktree and PR).
    """


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


class PreExistingCIFailure(UnrecoverableDeliveryError):
    """A failed delivery check is already failing on the PR's base.

    Retrying review/fix rounds cannot change a failure that the PR did not
    introduce, so this external precondition fails the delivery quickly.
    """


class GateCIFailure(RuntimeError):
    """A failed CI check on the reviewed PR head itself.

    Unlike `PreExistingCIFailure` this is recoverable: the review/fix
    loop repairs the head until the check passes or the round budget
    exhausts. Routing to that recoverable path is by THIS type, never by
    matching text inside the message — rewording the message must not
    change control flow (the rule at `RecoverableMergeGateError`).
    """


class ReviewRoundsExhausted(UnrecoverableDeliveryError):
    """Expected terminal stop after the bounded review/fix budget.

    This remains an exception internally so the existing delivery cleanup
    path performs the terminal label/comment transition, but it is not a
    Runner failure and must not be logged with a traceback.
    """


class HumanDecisionRequired(UnrecoverableDeliveryError):
    """The reviewer found a decision that only a maintainer can make."""

    def __init__(self, message: str, *, action: str) -> None:
        super().__init__(message)
        self.action = action


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


def is_unrecoverable_failure(exc: BaseException) -> bool:
    """Classify one delivery failure.

    True for an explicit `UnrecoverableDeliveryError` (an external
    precondition the AI cannot safely judge or fix) and for the #698
    provider-quota exhaustion (`RateLimitExhaustedError`): the backoff
    budget of the delivery attempt is spent on an external condition,
    and a recoverable classification would resume the open-PR review
    with the persisted counter already at the limit — one 429 exit per
    tick, forever, the unbounded loop the issue bans. Every other
    failure — Pi execution failure (pi exit, upstream dead, idle
    recovery), timeout, runner exception, missing/malformed verdict,
    missing worktree, unpushed local commit, gate failure — is
    recoverable: the Issue goes to `ai-fix-needed` and the next timer
    resumes the same run, branch, worktree and PR. A single failure
    must never permanently stop an Issue.
    """
    return isinstance(
        exc, (UnrecoverableDeliveryError, RateLimitExhaustedError),
    )


# The health check's journal lines carry the same `[run_id]`
# prefix as every other Runner line (the RunIdFilter is attached per
# logger; the health module must not import this one — circular).


def _stop_delivery(signum: int) -> None:
    """Log the stop scene, shut down the live Pi child, exit.

    Runs from the SIGTERM handler. With no run in flight the stop is
    idle: one `run_stopped result=idle` line, no invented Issue fields.
    With a run in flight the `run_stopping` line carries the full scene
    (issue, title, signal, phase, branch, worktree, session —
    phase/session from the existing activity snapshot, `-` when absent)
    BEFORE the stop, the live Pi child is TERMed and waited for, then
    the `run_stopped issue=N result=interrupted` line. The process then
    exits with 128+signum (143 for SIGTERM) — the same value systemd
    records for a signal-caused stop.
    """
    run = journal.active_run()
    if run is None:
        event("run_stopped", result="idle")
    else:
        phase = "-"
        session = "-"
        try:
            snapshot = activity_snapshot(Path(run["worktree"]) / ".pi-session")
            if snapshot is not None:
                phase = snapshot["phase"] or "-"
                session = snapshot["session_id"] or "-"
        except Exception:
            LOGGER.exception("stop scene activity snapshot failed")
        event(
            "run_stopping", issue=run["issue"], title=run["title"],
            signal=signal.Signals(signum).name, phase=phase,
            branch=run["branch"], worktree=run["worktree"],
            session=session,
        )
        child = run["pi"]
        _shutdown_child(child)
        event(
            "run_stopped", issue=run["issue"], result="interrupted",
        )
    _die_from_signal(signum)


def _shutdown_child(child: subprocess.Popen | None,
                   grace: float = STOP_CHILD_GRACE_SECONDS) -> None:
    """Terminate and reap a live child without blocking past `grace`.

    A plain ``child.wait()`` with no timeout lets a Pi child stuck in a
    model/network call block the stop handler; systemd then SIGKILLs the
    whole unit after ``TimeoutStopSec`` and records
    ``Result=timeout``/failed. This TERMs the child, waits at most
    ``grace`` seconds, then KILLs and reaps it, so the Runner always
    exits with the ORIGINAL signal (128+signum) before systemd's own
    deadline. A child that exits on TERM (cooperative) is unaffected; a
    child that already exited is a no-op."""
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _die_from_signal(signum: int) -> None:
    """Exit the process from the ORIGINAL signal.

    `os._exit` never returns in production; the two lines after it
    exist so a handler crash can never swallow the stop: restore the
    default disposition and re-raise the ORIGINAL signal at ourselves,
    so the process dies from the signal itself (systemd sees a
    signal-caused stop, exit 128+signum).
    """
    os._exit(128 + signum)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _handle_stop(signum: int, frame: object) -> None:
    """SIGTERM handler: log the active Issue context, then exit."""
    _stop_delivery(signum)


def repository_base_branch(config: config_domain.RunnerConfig, source_repo: str) -> str:
    """The fallback base branch of one source repo.

    A repository config that omits `base_branch` falls back to its
    `[[repositories]]` entry's `base_branch` when one matches the source
    repo, else the host `base_branch`.
    """
    for repo in config.repositories:
        if repo.get("github") == source_repo:
            return repo["base_branch"]
    return config.base_branch


def resolve_source_base_branch(config: config_domain.RunnerConfig, source_repo: str,
                               policy: RepoPolicy | None) -> config_domain.RunnerConfig:
    """Fuse one source repo's base branch unconditionally: the
    `[[repositories]]` entry fallback first, then the repository
    policy's override when a policy file exists. Every consumer
    (dev path AND the release state machine) reads `config.base_branch`
    and must never re-derive it from the raw entry — the raw entry
    skips the policy layer."""
    fused = replace(
        config,
        base_branch=repository_base_branch(config, source_repo),
    )
    if policy is None:
        return fused
    return resolve_policy(fused, policy)


def apply_repo_policy(config: config_domain.RunnerConfig, source_repo: str,
                      policy: RepoPolicy) -> config_domain.RunnerConfig:
    """Resolve one repo's policy over the host fallback."""
    fallback = replace(
        config,
        base_branch=repository_base_branch(config, source_repo),
    )
    return resolve_policy(fallback, policy)


def previous_repo_config_sha(number: int, source_repo: str) -> str | None:
    """The sha recorded by the previous run of this Issue.

    Scans the trusted Orbi comments for the `repo_config` field of the
    newest run. Best-effort audit: a read failure is logged and returns
    `None` (the change annotation is then omitted, never a delivery
    failure).
    """
    try:
        comments = issue_comments(number, repo=source_repo)
    except Exception:
        event(
            "repo_config_previous_lookup_failed", level=logging.WARNING,
            issue=number,
        )
        return None
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = re.search(r"(?m)^-\s*repo_config:\s*([0-9a-f]{7,64})\s*$", body)
        if match:
            return match.group(1)
    return None


def validate_execution_source_repos(source_repos: Sequence[str]) -> None:
    """Reject task-pool fan-out until execution has per-repo checkouts."""
    if len(source_repos) > 1:
        raise ValueError(
            "multiple source_repos are not supported with one checkout; "
            "configure exactly one source repository until multi-repo "
            "workspaces are available"
        )


def validate_config(config: config_domain.RunnerConfig) -> None:
    if not config.repo_dir.is_dir():
        raise FileNotFoundError(config.repo_dir)
    # The deployment home (CLI install source, unit templates,
    # labels.toml, prompt defaults) must exist too — a missing home fails
    # the start fast, like a missing delivery checkout.
    if not config.deploy_home.is_dir():
        raise FileNotFoundError(config.deploy_home)
    for path in [
        config.prompt, config.prompt_review,
        *config.skills, *config.context_files,
    ]:
        if not path.is_file():
            raise FileNotFoundError(path)
    # Multi-repo registry: every registered path must exist
    # and be a Git checkout — a `.git` directory (a plain checkout) or a
    # `.git` file (a linked worktree). Absent key -> no registry, so a
    # single-repo config keeps its exact flow.
    for repo in config.repositories:
        path = repo["path"]
        if not path.is_dir():
            raise FileNotFoundError(path)
        if not (path / ".git").exists():
            raise ValueError(
                f"repositories entry {repo['name']!r}: {path} "
                "is not a git checkout"
            )


def claim_route(labels: set[str], *, branch_exists: bool,
                open_pr: bool, ready_label: str = READY_LABEL) -> str:
    """Choose the fresh-claim action from the physical GitHub scene.

    This is deliberately pure: labels are the event and branch/PR existence
    is the observed physical state.  The existing review loop handles the
    returned ``review`` route. `ready_label` is the repository's dispatch
    label.
    """
    if open_pr and ready_label in labels:
        return "review"
    if open_pr and (PR_OPENED_LABEL in labels or FIX_NEEDED_LABEL in labels):
        return "review"
    # An existing branch without an open PR is resumed by implementation;
    # a missing branch is the same implementation path.
    return "implement"


def external_takeover_pr(repo_dir: Path, body: str | None,
                         source_repo: str, base_branch: str) -> dict | None:
    """Resolve the external PR an Issue body routes to, or None.

    The marker is written by the triage workflow; a claim of
    that Issue must review the external PR before any internal redo. A
    PR that is no longer open — merged by a human, closed by the
    contributor (withdrawn) or closed as rejected — is NOT a takeover:
    the claim proceeds as a fresh internal delivery. A PR against
    another base is skipped the same way (the delivery loop only merges
    the configured protected base). A real `gh` failure propagates —
    a takeover that cannot be resolved fails the claim fail-fast, the
    same contract as `open_pr_for_branch`.
    """
    if not isinstance(body, str):
        return None
    numbers = EXTERNAL_PR_RE.findall(body)
    if not numbers:
        return None
    number = numbers[0]
    raw = run_gh_read_command([
        "gh", "pr", "view", number, "--repo", source_repo,
        "--json", "state,url,baseRefName,headRefName,headRefOid",
    ], cwd=repo_dir, timeout=RESUME_PR_STATE_TIMEOUT_SECONDS)
    pr = json.loads(raw)
    if not isinstance(pr, dict):
        raise RuntimeError(
            f"external PR view for #{number} did not return an object"
        )
    state = pr.get("state")
    base_ref = pr.get("baseRefName")
    if state != "OPEN":
        event(
            "external_takeover_skipped", pr=number,
            reason=f"pr_state={state}",
        )
        return None
    if base_ref != base_branch:
        event(
            "external_takeover_skipped", pr=number,
            reason="base_mismatch", pr_base=base_ref,
            configured_base=base_branch,
        )
        return None
    event(
        "external_takeover", pr=number, head=pr.get("headRefName"),
    )
    pr["number"] = int(number)
    return pr


def started_pi_comment_body(ctx: RunContext, run_info: str,
                            extra_fields: dict | None = None) -> str:
    """The start comment doubles as the recoverable run scene.

    `extra_fields` carries the repo-config audit fields;
    their values may contain spaces, so they are rendered by the field
    block and never spliced into the space-separated `run_info`.
    """
    fields = _run_info_fields(run_info)
    fields["branch"] = str(ctx.branch)
    fields["worktree"] = str(ctx.worktree)
    if extra_fields:
        fields.update(extra_fields)
    info = _run_info_fields(run_info)
    headline = "Orbi started Pi: " + " ".join(
        f"{key}={info[key]}" for key in ("run_id", "priority")
        if key in info
    )
    return field_block(
        ctx.run_id, headline, fields,
        detail_keys={"base_sha", "repo_config", "branch", "worktree", "session"},
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str,
                           external: bool = False) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR. The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id — a comment must never be able to name a local path. An
    external takeover marks the scene `external`: the PR is
    the contributor's own (no run marker, no `Fixes` keyword in its
    body), and the delivery branch is the PR's head branch — derived
    from the takeover worktree, never from the comment.

    The machine-readable record is the single hidden `orbi:scene:v1`
    block rendered from the `Scene`; the field lines below
    the headline stay for humans only.
    """
    fields = _run_info_fields(run_info)
    if external:
        fields["external"] = "true"
    headline = "Orbi opened PR: " + pr_url
    scene_record = scene.Scene(
        run_id=validate_run_id(run_id),
        base_branch=fields["base_branch"],
        base_sha=fields["base_sha"],
        pr_url=pr_url,
        external="true" if external else "",
    )
    body = field_block(
        run_id, headline, fields,
        detail_keys={"base_sha", "repo_config", "branch", "worktree", "session"},
    )
    lines = body.splitlines()
    lines.insert(1, scene.render(scene_record))
    return "\n".join(lines)


def merged_pr_comment_body(run_id: str, pr_url: str, merge_commit: str,
                           review_rounds: int, external_commits: object,
                           commits: object, base_branch: str,
                           review: str) -> str:
    """Render the final delivery record without exposing detail rows."""
    return (
        f"{run_marker(run_id)}\n"
        f"Orbi merged PR: {pr_url} "
        f"(merge_commit={merge_commit} "
        f"review_rounds={review_rounds} "
        f"external_commits={external_commits} "
        f"commits={commits} "
        f"base_branch={base_branch} run_id={run_id})\n"
        f"review: {review}\n\n"
        "<details><summary>Run details</summary>\n\n"
        f"- merge_commit: {merge_commit}\n"
        f"- base_branch: {base_branch}\n\n"
        "</details>"
    )


def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Orbi opened PR:` comment into a resume scene.

    The v1 scene block (orbi.scene) is the machine protocol; the
    human-readable text is the legacy fallback kept for one transition
    version. Returns None when the body is not an
    opened-PR comment. Fails fast when the comment is malformed:
    resuming must recover the exact run (run id, base, PR URL), never a
    guess. Branch and worktree are not parsed: the runner
    derives them from its own config, the Issue number and the run id,
    so a comment can never name an arbitrary local path.
    """
    found = scene.parse(body)
    if found is None:
        return None
    return {
        "run_id": found.run_id,
        "base_branch": found.base_branch,
        "base_sha": found.base_sha,
        "pr_url": found.pr_url,
        "external": found.external,
        # The review-round counter travels with the scene —
        # the round comments carry the updated scene block, so the next
        # resume reads the advanced count instead of re-counting text.
        "review_round": found.review_round,
        # Keep the projection shape of pre-#902 scenes stable when the
        # counter is still zero. A non-zero value is the persisted state
        # needed by the next review session.
        **({"base_advance_round": found.base_advance_round}
           if found.base_advance_round else {}),
        # An unknown verdict head is counted across timer ticks. Preserve
        # the non-zero counter in the projection so the next review cannot
        # silently restart at attempt 1.
        **({"verdict_head_unknown_round": found.verdict_head_unknown_round}
           if found.verdict_head_unknown_round else {}),
    }


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only trusted comments are considered: a comment is trusted when its
    author has maintainer association (OWNER, MAINTAINER, MEMBER or
    COLLABORATOR) or is the account the `gh` credentials resolve to
    (`_comment_is_trusted`, github.py). A public comment can never
    become the recovery scene. The two
    failure shapes stay distinct for the caller:
    `scene.SceneError` when a trusted scene comment is corrupted,
    `scene.SceneMissingError` when no trusted comment carries a scene
    at all. Neither may be guessed at. The projection adds `scene_at`
    (the comment's `createdAt`): the #483 human-recovery budget reset
    compares it against the recovery transition time.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        found = parse_pr_comment(comment.get("body"))
        if found is not None:
            found["scene_at"] = comment.get("createdAt")
            return found
    raise scene.SceneMissingError(
        "no 'Orbi opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )


def _route_external_pr_ticket(issue: dict, repo: str) -> bool:
    """Route a marker-bearing opened-PR ticket through external
    integration instead of `ai-blocked`. Returns True when handled.

    The triage workflow (#621) labels the linked Issue
    `ai-pr-opened` — the resumable scan then picks it, finds no trusted
    runner scene comment, and the old code burned the ticket to
    `ai-blocked` while the takeover entry (#608) stayed unreachable for
    exactly these tickets. The body marker decides the route:

    - PR OPEN -> requeue to the ready queue (`EVENT_REQUEUE`); the next
      fresh claim's takeover probe finds the marker plus the open PR and
      reviews the contribution first — the #608 main path;
    - PR MERGED -> the contribution already delivered the fix: close the
      triage Issue with the same bookkeeping as the takeover merge path;
    - PR CLOSED without merge -> requeue for the documented internal
      redo fallback (#608).

    Only a probe failure falls through to the caller's block path (the
    pre-#726 behavior) — never a guess.
    """
    number = int(issue["number"])
    body = issue.get("body")
    match = EXTERNAL_PR_RE.search(body) if isinstance(body, str) else None
    if match is None:
        return False
    pr_number = int(match.group(1))
    try:
        raw = run_gh_read_command(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "state"],
        )
        state = (json.loads(raw) or {}).get("state")
    except Exception:
        LOGGER.exception(
            "issue=%s external_pr_state_probe_failed pr=#%s",
            number, pr_number,
        )
        return False
    if state == "MERGED":
        _close_external_triage_issue(
            number, repo, f"https://github.com/{repo}/pull/{pr_number}",
            f"<!-- orbi:external-pr:{pr_number} -->", "external-merge",
        )
        event(
            "external_pr_already_merged", issue=number,
            pr=f"#{pr_number}",
        )
        return True
    if state in ("OPEN", "CLOSED"):
        labels = issue_labels(number, repo=repo)
        apply_label_patch(
            number, repo=repo, event=EVENT_REQUEUE,
            current_labels=labels,
        )
        comment_issue(
            number, repo=repo,
            body=(
                f"<!-- orbi:external-pr:{pr_number} -->\n"
                f"Orbi: the `ai-pr-opened` state of this triage Issue "
                f"comes from external contribution PR #{pr_number} "
                "(state: "
                f"{state}), not from a runner scene; routing it through "
                "the external takeover review on the next claim instead "
                "of blocking it."
            ),
        )
        event(
            "external_pr_routed_takeover", issue=number,
            pr=f"#{pr_number}", state=state,
        )
        return True
    return False


def _recover_missing_pr_scene(
    issue: dict, repo: str, repo_dir: Path,
) -> dict | None:
    """Retry a lost opened-PR scene from the durable local run state."""
    number = int(issue["number"])
    try:
        resume = worktree_resume_scene(repo_dir, repo, number)
        if resume is None:
            return None
        run_id, worktree = resume
        state = read_run_state(worktree)
        if state is None:
            return None
        pr = open_pr_for_branch(repo_dir, state["branch"])
        if not pr:
            return None
        base_branch = pr.get("baseRefName")
        base_sha = pr.get("baseRefOid")
        url = pr.get("url")
        if not all(isinstance(value, str) and value for value in
                   (base_branch, base_sha, url)):
            return None
        body = opened_pr_comment_body(
            run_id, f"base_branch={base_branch} base_sha={base_sha} "
            f"run_id={run_id} priority=normal", url,
        )
        comment_issue(number, repo=repo, body=body)
        found = parse_pr_comment(body)
        if found is not None:
            found["scene_at"] = None
        return found
    except Exception:
        LOGGER.warning(
            "issue=%s missing PR scene retry failed", number, exc_info=True,
        )
        return None


def _has_recoverable_pr_scene(
    issue: dict, repo: str, repo_dir: Path,
) -> bool:
    """Return whether a local run state and open PR anchor a retry.

    A confirmed absence of the PR is not a recoverable notification
    failure: otherwise a deleted/closed PR would leave the Issue in
    ``ai-pr-opened`` forever.  Probe failures remain recoverable because
    they may be transient GitHub/API failures.
    """
    try:
        resume = worktree_resume_scene(repo_dir, repo, int(issue["number"]))
        if resume is None:
            return False
        state = read_run_state(resume[1])
        if state is None:
            return False
        try:
            return open_pr_for_branch(repo_dir, state["branch"]) is not None
        except Exception:
            # A transient failure while probing the PR must not turn the
            # recovery retry itself into a terminal decision.
            return True
    except Exception:
        return False


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an opened-PR Issue `ai-blocked` when its scene is malformed.

    The blocked transition is scoped to this one Issue: the
    caller continues the tick, so a malformed scene never crashes the
    whole runner. The failure comment carries the run marker recovered
    from a trusted comment when it is present — the same run id, never a
    new or guessed one. The PR, branch and worktree stay intact. A
    failure of the reporting itself is logged, never raised: the Issue
    then stays in its opened-PR state and the next tick retries the
    block.
    """
    number = int(issue["number"])
    LOGGER.error(
        "issue=%s resume scene is malformed: %s", number, error,
    )
    marker = latest_run_marker(comments)
    try:
        apply_label_patch(
            number, repo=repo, event=EVENT_BLOCKED,
            current_labels={FIX_NEEDED_LABEL},
        )
        # A scene that cannot be recovered is an external
        # precondition the AI cannot fix by itself (the runner cannot
        # derive run_id, branch, worktree or PR without the trusted
        # scene comment, so it cannot start a review session): the
        # comment states the EXPLICIT reason why automatic recovery is
        # impossible plus the human next step (restore the scene or
        # relabel).
        comment_issue(
            number, repo=repo,
            body=(
                f"{marker}\n" if marker else ""
            ) + (
                f"Orbi failed: {error}; this is an external "
                "precondition the AI cannot safely judge or fix, so "
                "it cannot be recovered automatically (the Issue "
                "stays ai-blocked until a human decides) — restore "
                "the trusted 'Orbi opened PR' scene comment or "
                "relabel the Issue ai-fix-needed to resume this same PR"
            ),
        )
    except Exception:
        LOGGER.exception("issue=%s failure reporting failed", number)


def block_repo_config_failure(number: int, source_repo: str,
                              error: ValueError, run_id: str,
                              current_labels=()) -> None:
    """Mark an Issue `ai-blocked` when its repo config is invalid (#527).

    The strict repository schema is a fail-fast precondition: a file that
    exists on the default branch but carries an unknown/host-only key, a
    wrong type or invalid TOML blocks the claim with the concrete reason
    and the offending key names. The failure is scoped to this repository
    only (a sibling pool's valid config is unaffected). A failure of the
    reporting itself is logged, never raised, so the tick still ends
    cleanly (`main` releases the slot in its `finally`).
    """
    try:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=set(current_labels),
        )
        comment_issue(
            number, repo=source_repo,
            body=(
                f"{run_marker(run_id)}\n"
                f"Orbi failed: repository config is invalid: {error}; "
                "this is a fail-fast repository precondition the AI does "
                "not fix "
                "itself — correct the file on the default branch "
                "(remove the host-only/unknown key or fix the value) and "
                "relabel the Issue ai-ready for a new run"
            ),
        )
    except Exception:
        LOGGER.exception("issue=%s repo_config_failure_report_failed", number)


def _parse_version_title(title: object) -> tuple[int, int, int] | None:
    """Parse a strict ``v<major>.<minor>.<patch>`` milestone title."""
    if not isinstance(title, str):
        return None
    match = re.fullmatch(
        r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", title,
    )
    return tuple(map(int, match.groups())) if match else None


def run_state_path(worktree: Path) -> Path:
    """The run state file of one task worktree.

    It lives in the gitignored `.orbi/` directory, so it never
    dirties the commit boundary and never reaches the
    delivery commit.
    """
    return worktree / ".orbi" / "run-state.json"


def write_run_state(ctx: RunContext) -> None:
    """Write (or refresh) the run state file of one task worktree.

    The file is the explicit "same run" marker: the
    worktree directory name alone is not stable across a repo rename
    (the slug changes), but the state file carries the issue number
    and the repo — the identity the next tick matches on. A resumed
    run refreshes the SAME file (same run id): the file is per-run,
    never per-session.

    An existing `pushed_head`/`pushed_base` (Issue #833: the merge
    record's `external_commits` inputs — the last engine-pushed head
    and the foreign head the engine's push line started from) survive
    the refresh: the resume continues the same delivery line, so its
    push history is not the refresh's business to drop.
    """
    state: dict = {
        "run_id": ctx.run_id,
        "issue": ctx.issue,
        "repo": ctx.source_repo,
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = run_state_path(ctx.worktree)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        state.update({
            key: existing[key]
            for key in ("pushed_head", "pushed_base")
            if isinstance(existing.get(key), str) and existing[key]
        })
    _write_run_state_file(ctx.worktree, state)


def _write_run_state_file(worktree: Path, state: dict) -> None:
    path = run_state_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def record_pushed_head(worktree: Path, head: str) -> None:
    """Record `head` as the last engine-pushed head of this delivery.

    The three write points are the Runner's own branch-push evidence:
    the delivery push in `deliver_pr` (the PR-open head), the review
    session's fix push (the re-frozen advanced head), and the
    round-start adoption of a head a previous session pushed but whose
    round ended before the re-freeze record (a findings verdict or a
    malformed verdict head). An external push never passes through any
    of them, which is exactly the distinction the merge record's
    `external_commits` needs. Recording is bypass-safe (Issue #73):
    the fields only feed the merge record, so a missing or corrupt
    state file — a recreated worktree, for instance — logs
    `pushed_head_unrecorded` and continues; the merge record degrades
    to `unknown` and the delivery is never re-failed by its own
    observability input.
    """
    state = _recordable_state(worktree)
    if state is None:
        return
    state["pushed_head"] = head
    _write_run_state_file(worktree, state)


def record_pushed_base(worktree: Path, base: str) -> None:
    """Record `base` as the head the engine's push line started from.

    The one write point is the external-takeover claim (Issue #608):
    the taken-over branch carries the contributor's commits, so the
    merge record's `external_commits` must subtract only what the
    engine pushes on top of this foreign head, never the head itself.
    Set once per run: a re-claim of the same run re-derives a moved
    HEAD (an interrupted delivery), which is not the line's origin.
    Bypass-safe like `record_pushed_head`.
    """
    state = _recordable_state(worktree)
    if state is None:
        return
    if isinstance(state.get("pushed_base"), str) and state["pushed_base"]:
        return
    state["pushed_base"] = base
    _write_run_state_file(worktree, state)


def _recordable_state(worktree: Path) -> dict | None:
    """The run state to record into, or None when recording must skip.

    A missing file is a normal state here (the #90/#50 worktree
    recreation loses `.orbi/`), a corrupt one fails the resume readers
    before any record point can run — neither may fail a push or a
    merge for the sake of the merge record's own input.
    """
    try:
        state = read_run_state(worktree)
    except ValueError as exc:
        state = None
        error = str(exc)
    else:
        error = "the run state file is missing"
    if state is None:
        event(
            "pushed_head_unrecorded", level=logging.WARNING,
            state_path=str(run_state_path(worktree)), error=error,
        )
    return state


def _read_pushed(worktree: Path, key: str) -> str | None:
    """One recorded push-history field, or None when unusable.

    The merge record reads these AFTER the merge landed: a missing or
    corrupt record must degrade the metric to `unknown`, never fail a
    landed delivery and never fabricate a count.
    """
    try:
        state = read_run_state(worktree)
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


def read_run_state(worktree: Path) -> dict | None:
    """Read the run state file; None when absent, fail fast when corrupt.

    A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run, and a wrong continuation is
    worse than a blocked Issue.
    """
    path = run_state_path(worktree)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"run state file {path} is unreadable: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise ValueError(
            f"run state file {path} must be a JSON object"
        )
    required: dict[str, type] = {
        "run_id": str, "issue": int, "repo": str,
        "branch": str, "worktree": str,
    }
    for key, expected in required.items():
        value = state.get(key)
        if expected is int:
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, expected) and bool(value)
        if not valid:
            raise ValueError(
                f"run state file {path} is malformed: missing or "
                f"invalid field {key!r}"
            )
    return state


def worktree_resume_scene(repo_dir: Path, source_repo: str,
                 number: int) -> tuple[str, Path] | None:
    """Return the resume scene `(run_id, worktree)` for one Issue, or None.

    The worktrees are matched by the RUN STATE FILE, not
    by the directory name alone: the directory name carries the
    source-repo slug, which changes when the repo is renamed, while
    the state file carries the issue number and the repo NAME (the
    part after the slash — stable across a rename). The newest
    matching worktree (by mtime) wins, as before. The
    scene's worktree path may carry the OLD slug (a rename): it is
    the scene the run continues in, never a reason for a second
    worktree.

    A worktree that claims THIS issue number but has a MISSING or
    CORRUPT run state file cannot be verified as the same run: it
    fails fast with the exact reason — never a silent fresh redo.
    A worktree of another issue without a state file
    (a legacy completed run) is unrelated and skipped.
    """
    name = source_repo.rsplit("/", 1)[-1]
    pattern = re.compile(
        r"^orbi-.+-issue-" + str(number) + r"-[0-9a-f]{8}$",
    )
    candidates: list[Path] = []
    worktrees = repo_dir / ".worktrees"
    if worktrees.is_dir():
        for path in worktrees.iterdir():
            if not path.is_dir() or not pattern.match(path.name):
                continue
            try:
                state = read_run_state(path)
            except ValueError as exc:
                raise RuntimeError(
                    f"worktree {path} has a corrupt run state file "
                    f"({exc}): the same run cannot be verified "
                    "(Issue #219)"
                ) from exc
            if state is None:
                raise RuntimeError(
                    f"worktree {path} has no run state file "
                    f"({run_state_path(path)}): the same run cannot "
                    "be verified (Issue #219)"
                )
            if str(state["repo"]).rsplit("/", 1)[-1] != name:
                continue
            candidates.append(path)
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(read_run_state(newest)["run_id"]), newest


def resume_run_id(repo_dir: Path, source_repo: str,
                  number: int) -> str | None:
    """Return the run id to resume for one Issue, or None.

    Delegates to `worktree_resume_scene` (the worktree is matched by its run
    state file, not the directory name).
    """
    scene = worktree_resume_scene(repo_dir, source_repo, number)
    return scene[0] if scene is not None else None


def _tree_size(path: Path) -> int:
    """The byte size of one worktree tree (informational `freed=` field).

    Unreadable entries are skipped, never raised: the size is journal
    metadata, not a gate.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def reclaim_released_worktrees(config: config_domain.RunnerConfig, *,
                               now: datetime | None = None) -> None:
    """Remove the task worktrees of closed Issues past the retention
    window.

    Called at every tick start beside `check_unit_drift` — idempotent,
    bounded (at most `WORKTREE_RECLAIM_MAX_PER_TICK` removals,
    oldest-closed first) and never fatal: a GitHub read failure removes
    nothing, a single removal failure is a `worktree_reclaim_failed`
    warning and the pass continues. Safety, in order:

    - only REGISTERED worktrees under `repo_dir/.worktrees` are
      considered, and only names matching the `worktree_path` pattern
      for the CONFIGURED source repos (a foreign worktree or an
      old-slug scene left by a repo rename is never touched);
    - the worktree of this process's bound run is never a candidate —
      matched by run id and by the stop-scene path (an
      external takeover checks out a head branch whose directory name
      is not run-id-derived);
    - the Issue must be closed (ONE batched `gh issue list` per involved
      repo), past `worktree_retain_hours` since its `closedAt`, and either
      have a terminal label or be free of in-flight labels — a human
      closing an in-flight Issue leaves the label, and the scene survives
      until the delivery path resolves it. Terminal labels (`ai-merged` or
      `ai-blocked`) take precedence over stale in-flight labels.

    A closed Issue is never resumed (`pick_resumable_delivery` scans
    open Issues only) and nothing reads the task worktree after the
    merge, so a reclaimed scene is unreachable garbage — never recovery
    state. The structured `worktree_reclaimed count=N freed=<bytes>`
    line lands in the journal only when something was removed.
    """
    repo_dir = Path(config.repo_dir)
    worktrees_root = repo_dir / ".worktrees"
    if not worktrees_root.is_dir():
        return
    repo_of_slug = {
        repo.replace("/", "-"): repo for repo in config.source_repos
    }
    # (slug, issue number, path) per orbi-named registered worktree.
    candidates: list[tuple[str, int, Path]] = []
    active = journal.active_run() or {}
    listing = run_command(
        ["git", "worktree", "list", "--porcelain"], cwd=repo_dir,
    )
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree "))
        match = _WORKTREE_NAME_PATTERN.match(path.name)
        if (
            worktrees_root not in path.parents
            or match is None
            or match["slug"] not in repo_of_slug
            or match["run_id"] == current_run_id()
            or str(path) == active.get("worktree")
        ):
            continue
        candidates.append((match["slug"], int(match["number"]), path))
    if not candidates:
        return
    # ONE batched closed-Issue read per involved source repo, keyed by
    # (repo, number): two source repos can carry the same Issue number,
    # and one repo's closed Issue never answers for the other's. Any
    # read or parse failure removes NOTHING (the safe direction: 宁可
    # 不删).
    closed: dict[tuple[str, int], dict] = {}
    try:
        for repo in sorted({
            repo_of_slug[slug] for slug, _number, _path in candidates
        }):
            for issue in list_issues(
                repo, state="closed",
                json_fields="number,closedAt,labels", limit=1000,
            ):
                closed[(repo, int(issue["number"]))] = issue
    except Exception as exc:
        event(
            "worktree_reclaim_failed", level=logging.WARNING,
            reason=f"{exc} (removing nothing)",
        )
        return
    now = now or datetime.now(timezone.utc)
    retain = timedelta(hours=config.worktree_retain_hours)
    reclaimable: list[tuple[datetime, Path]] = []
    for slug, number, path in candidates:
        issue = closed.get((repo_of_slug[slug], number))
        if issue is None:
            continue
        try:
            closed_at = datetime.fromisoformat(str(issue["closedAt"]))
        except (TypeError, ValueError):
            continue
        if closed_at.tzinfo is None:
            continue
        if now - closed_at < retain:
            continue
        raw_labels = issue.get("labels")
        labels = {
            label.get("name") for label in raw_labels
            if isinstance(label, dict)
        } if isinstance(raw_labels, list) else set()
        if (not labels & _WORKTREE_TERMINAL_LABELS
                and labels & _WORKTREE_INFLIGHT_LABELS):
            continue
        reclaimable.append((closed_at, path))
    reclaimable.sort(key=lambda item: item[0])
    removed = 0
    freed = 0
    for _closed_at, path in reclaimable[:WORKTREE_RECLAIM_MAX_PER_TICK]:
        try:
            size = _tree_size(path)
            run_command(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=repo_dir,
            )
        except Exception as exc:
            event(
                "worktree_reclaim_failed", level=logging.WARNING,
                path=path, reason=exc,
            )
            continue
        removed += 1
        freed += size
    if removed:
        event("worktree_reclaimed", count=removed, freed=freed)


def _another_live_runner(slot_dir: Path, max_concurrency: int) -> bool:
    """True when a slot is held by another pid — a live co-runner.

    The #39 liveness rule `pick_in_progress_issue` applies to the orphan
    scan: a slot held by another process proves a live
    runner is working, so state it owns is in flight, not orphaned.
    The same rule extends to the release dispatch — an in-progress
    release found while another runner is live is being released right
    now; only a runner that is alone may resume it — and to the
    claim-window yield guard.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return True
    return False


def is_content_only(issue: dict) -> bool:
    """Return True only for the explicit content-only task marker.

    The content path dispatches ONLY on the explicit
    `ai-content-only` marker, never on derived state.
    """
    labels = issue.get("labels", [])
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("name") == CONTENT_ONLY_LABEL
        for label in labels
    )


def is_ops(issue: dict) -> bool:
    """Return True only for the explicit ops task marker.

    An ops ticket runs the SAME full-execution session as a dev ticket
    (worktree, shell, network — no command whitelist exists); the ops
    playbook replaces the dev one and the deliverable is the evidence
    posted on the Issue, unless the session commits code (then the
    delivery takes the normal PR ceremony).
    """
    labels = issue.get("labels", [])
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("name") == OPS_LABEL
        for label in labels
    )


def process_ticket_only(issue: dict, config: config_domain.RunnerConfig, source_repo: str) -> str:
    """Deliver explicit ticket-only Agent output to the source Issue (#209)."""
    number = int(issue["number"])
    title = issue["title"]
    run_id = new_run_id()
    set_run_id(run_id)
    priority = issue_priority(issue)
    run_info = f"run_id={run_id} priority={priority} task_type=ticket-only"
    publisher = ProgressPublisher(number, source_repo, run_id, run_command=run_command)
    publish = functools.partial(
        _safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_TICKET,
    )
    started = time.monotonic()
    apply_label_patch(
        number, repo=source_repo, event=EVENT_CLAIM,
        current_labels={label.get("name") for label in issue.get(
            "labels", []) if isinstance(label, dict)
            and isinstance(label.get("name"), str)},
    )
    ticket_ctx = RunContext(
        run_id=run_id, issue=number, branch="-", worktree=Path("-"),
        source_repo=source_repo,
    )
    set_active_run(ticket_ctx, title)
    try:
        publish(
            action=lambda: publisher.ensure(_progress_body(_progress_state(
                ticket_ctx, title=title, role=ROLE_TICKET, started=started,
                pr_url=None, review_round=0, priority=priority,
            ))),
        )
        output = pi_session.run_ticket_agent(
            issue, replace(config, run_id=run_id), source_repo,
            progress=LiveProgressThrottle(
                ticket_ctx, publisher, title=title, role=ROLE_TICKET,
                started=started, pr_url=None, review_round=0,
                priority=priority,
            ),
        )
        if not output:
            raise RuntimeError("ticket-only Agent returned no content")
        comment_issue(
            number, repo=source_repo,
            body=(f"{run_marker(run_id)}\n"
                  f"Orbi ticket-only delivery (run_id={run_id}):\n\n"
                  f"{output}"),
        )
        close_issue(int(number), repo=source_repo)
        # The ticket-only delivery never enters the PR/review states: it
        # clears the claim label directly (no `ai-merged` terminal state —
        # the Issue is closed, not merged).
        edit_issue(number, repo=source_repo, remove=IN_PROGRESS_LABEL)
        publish(
            action=lambda: publisher.milestone(f"ticket-only delivered: {run_info}"),
        )
        publish(
            action=lambda: publisher.finish(_progress_body(_progress_state(
                ticket_ctx, title=title, role=ROLE_TICKET, started=started,
                pr_url=None, review_round=0, priority=priority,
            ), outcome="**Orbi ticket-only delivered**")),
        )
        event(
            "run_end", run=run_id, issue=issue_context(source_repo, number),
            role=ROLE_TICKET, result="ticket_only",
            elapsed=format_duration(time.monotonic() - started),
        )
        return "ticket-only"
    except Exception as exc:
        LOGGER.exception("issue=%s ticket-only failed", number)
        detail = _failure_detail(exc)
        try:
            apply_label_patch(
                number, repo=source_repo, event=EVENT_BLOCKED,
                current_labels={IN_PROGRESS_LABEL},
            )
            comment_issue(
                number, repo=source_repo,
                body=(f"{run_marker(run_id)}\n"
                      f"Orbi ticket-only failed: {detail} ({run_info})\n"
                      f"run_id={run_id}\n"
                      "No Git branch, commit, or PR was created."),
            )
            publish(
                action=lambda: publisher.milestone(
                    f"ticket-only blocked: {sanitize(detail)} ({run_info})"
                ),
            )
        except Exception:
            LOGGER.exception("issue=%s ticket-only failure reporting failed", number)
        raise


def _query_open_prs(worktree: Path, branch: str) -> list:
    """Return the task branch's open PRs as the raw `gh pr list` list.

    The ONE PR-query contract shared by verify_pr and freeze_pr:
    a single field set, a single ambiguity-guard limit and
    a single parse. The limit is wide enough that the failure evidence
    lists every ambiguous open PR (the resume audit record);
    the "exactly one" decision needs no tighter bound. A non-array
    payload is a broken `gh` response, never "zero PRs" — fail fast
    instead of guessing.
    """
    raw = run_gh_read_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", (
            "number,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner,"
            "isCrossRepository,body"
        ),
        "--limit", "100",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list):
        raise RuntimeError(
            "gh pr list --json returned a non-array payload "
            "(expected exactly one open PR)"
        )
    return github.filter_same_repository_prs(prs, branch)


def _single_open_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    *,
    scene: str,
    external_pr_url: str | None = None,
    source_repo: str | None = None,
) -> dict:
    """Return the one open delivery PR of the task branch, base validated.

    The "exactly one open PR + configured base" decision shared by
    verify_pr and freeze_pr; callers add their own extra
    validations on the returned raw PR dict. `scene` names the calling
    path: the same externally-closed-PR failure used to raise the
    identical sentence from both paths and the log could not tell them
    apart.
    """
    if external_pr_url is not None:
        if source_repo is None:
            raise ValueError("external PR lookup requires source_repo")
        external = pr_view(
            _pr_number(external_pr_url),
            (
                "number,url,state,baseRefName,baseRefOid,headRefName,"
                "headRefOid,headRepository,headRepositoryOwner,body"
            ),
            repo=source_repo, cwd=worktree,
            timeout=RESUME_PR_STATE_TIMEOUT_SECONDS,
        )
        prs = [external] if external.get("state") == "OPEN" else []
    else:
        prs = _query_open_prs(worktree, branch)
    if len(prs) == 0:
        raise RuntimeError(
            f"{scene}: no open PR for the task branch "
            "(expected exactly one open PR)"
        )
    if len(prs) != 1:
        raise RuntimeError(
            f"{scene}: multiple open PRs for the task branch "
            "(expected exactly one open PR)"
        )
    pr = prs[0]
    base_ref = pr.get("baseRefName")
    if base_ref != base_branch:
        event(
            "pr_base_mismatch", level=logging.ERROR, scene=scene,
            expected=base_branch, actual=base_ref, branch=branch,
        )
        raise RuntimeError(
            f"{scene}: PR base is {base_ref}, expected {base_branch}; "
            "recreate the PR against the configured base branch"
        )
    return pr


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
        freshness = assess_base_freshness(
            worktree, base_branch, head="HEAD",
        )
        if freshness is not BaseFreshness.FRESH:
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
            prs = _query_open_prs(worktree, branch)
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
        pr = _single_open_pr(
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
            line_run_markers(issue_comments(issue, repo=line_repo))
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


def cleanup_task_worktree(ctx: RunContext, repo_dir: Path) -> None:
    """Remove a terminally failed task's worktree and Runner state.

    Called ONLY on the terminal `ai-blocked` outcome AFTER the Issue
    evidence (journal line + `Orbi failed` comment) is recorded.
    A retry generates a NEW run id and a NEW worktree, so the terminal
    scene is never needed again; the recoverable `ai-fix-needed` /
    model_wait paths keep the worktree for the same-run resume and must
    never call this. A cleanup failure is logged as
    `worktree_cleanup_failed` — never swallowed, never re-raised (the
    tick already handled the delivery failure).
    """
    try:
        if ctx.worktree.is_dir():
            shutil.rmtree(ctx.worktree)
        run_command(["git", "worktree", "prune"], cwd=repo_dir)
        event(
            "worktree_cleaned", issue=ctx.issue, run_id=ctx.run_id,
            worktree=ctx.worktree,
        )
    except Exception as exc:
        LOGGER.exception(
            "worktree_cleanup_failed issue=%s run_id=%s worktree=%s: %s",
            ctx.issue, ctx.run_id, ctx.worktree, exc,
        )


def _agent_delivery_boundary(worktree: Path) -> tuple[str, str]:
    """Return the agent's commit boundary as (HEAD, dirty status).

    The Runner-owned runtime paths are pinned in the worktree's LOCAL
    exclude BEFORE the check, so a task that renamed the
    tracked .gitignore (the #246 scene) cannot make the Runner's own
    state look like agent leftovers. Only Runner-owned runtime paths
    that remain (the exclude write raced or the path appeared after it)
    are repaired by re-writing the exclude — deterministic, no git add,
    no deletion, no arbitrary whitelisting. Shared by the dev closeout
    (`deliver_pr`) and the ops closeout.
    """
    pi_session.apply_runner_runtime_excludes(worktree)
    dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    if dirty and pi_session._is_runner_runtime_only(dirty):
        pi_session.apply_runner_runtime_excludes(worktree)
        event(
            "runner_runtime_exclude_repaired",
            status=" ".join(dirty.splitlines()),
        )
        dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head, dirty


def deliver_pr(ctx: RunContext, base_branch: str, base_sha: str, *,
               issue_title: str, repo_dir: Path,
               attribution_footer: bool = True) -> str | None:
    """The Runner completes the deterministic delivery closeout.

    The Agent stops at the committed delivery (code, tests,
    commit on the task branch). Everything after is deterministic and
    owned by the Runner — the Agent no longer fetches the base, merges
    it, pushes or creates the PR:

    1. commit boundary: the worktree is clean and HEAD advanced past
       the frozen base — the Runner never commits uncommitted changes
       or expands the Agent's commit boundary (fail fast);
    2. base freshness: fetch under the base-sync lock; when
       the base advanced, a plain `git merge origin/<base>` absorbs it;
       a conflict is aborted (the worktree returns to the Agent's exact
       commit boundary) and the PR opens on the Agent's head — the
       existing review loop absorbs the base in-session, the state
       machine is unchanged;
    3. push: a plain push of the task branch (never a force push),
       verified against the remote head;
    4. PR: exactly one open PR of the branch — created by the Runner
       with the run marker and `Fixes #<issue>` in the body when absent,
       verified by `verify_pr` with `require_latest_base=False` (this
       function just fetched and merged the base itself).

    A human may close the Issue while the delivery is in
    flight (labels/state only affect the next scan, so the in-flight
    session correctly keeps running). The Issue state is read directly
    right before the PR creation; a CLOSED Issue returns None — the
    pushed branch keeps the work, the closed Issue gets one explanatory
    comment, and no PR is opened (nothing dangles behind a closed
    Issue). Returns the PR URL, or None when the delivery stopped
    because the Issue was closed.
    """
    worktree = ctx.worktree
    branch: str = ctx.branch
    run_id: str = ctx.run_id
    issue: int = ctx.issue
    source_repo: str = ctx.source_repo
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    # Commit boundary: the Agent's delivery is the
    # committed worktree state.
    local_head, dirty = _agent_delivery_boundary(worktree)
    if dirty:
        event(
            "delivery_uncommitted_changes", level=logging.ERROR,
            branch=branch, status=" ".join(dirty.splitlines()),
        )
        raise RecoverablePiFailure(
            f"the agent left uncommitted changes in the worktree "
            f"({dirty.strip()}); the runner never commits uncommitted "
            "changes or expands the agent's commit boundary"
        )
    if local_head == base_sha:
        event(
            "delivery_no_commit", level=logging.ERROR,
            branch=branch, head=local_head,
        )
        raise RuntimeError(
            f"the agent delivered no commit on the task branch (HEAD "
            f"{local_head} is still the frozen base {base_sha})"
        )
    # Base freshness: the fetch updates the shared
    # remote-tracking ref, so it runs under the base-sync lock with the
    # deployment checkout as the lock location. A lock timeout or a
    # fetch error fails fast — no retry, no lock bypass.
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    freshness = assess_base_freshness(
        worktree, base_branch, head="HEAD",
    )
    if freshness is not BaseFreshness.FRESH:
        # The base advanced while the agent worked: absorb it with a
        # plain merge (the same base update the old agent prompt
        # required). A conflict is rolled back: the worktree returns
        # to the agent's exact commit boundary, the PR opens on the
        # agent's head, and the existing review loop (ai-fix-needed ->
        # the review session absorbs the base in-session) handles the
        # rest — the state machine is unchanged.
        try:
            run_command(
                ["git", "merge", f"origin/{base_branch}"], cwd=worktree,
            )
            event(
                "base_absorbed", base_branch=base_branch, branch=branch,
            )
        except subprocess.CalledProcessError as exc:
            run_command(["git", "merge", "--abort"], cwd=worktree)
            event(
                "base_merge_conflict", level=logging.ERROR,
                base_branch=base_branch, branch=branch,
                returncode=exc.returncode,
                stderr=(exc.stderr or "").strip(),
            )
    # Plain push of the task branch (never a force push), then verify
    # the remote head: the PR must be created from exactly this head.
    # The head is re-read after the absorb step: a successful base
    # merge advanced it to the merge commit. The head is resolved from
    # the remote itself (ls-remote, refspec independent): the pushed
    # branch has no local remote-tracking ref in a checkout whose fetch
    # refspec does not cover it, e.g. a `--single-branch` clone
    # (Issue #898).
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    run_git_network_command(
        ["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree,
    )
    remote_head = gitops.remote_branch_head(branch, cwd=worktree)
    if remote_head != local_head:
        event(
            "remote_head_mismatch", level=logging.ERROR,
            expected=local_head, actual=remote_head, branch=branch,
        )
        raise RuntimeError(
            f"remote head {remote_head} does not match the local head "
            f"{local_head} after push origin {branch}"
        )
    # This delivery's first engine-pushed head (Issue #833): the merge
    # record's external_commits subtracts exactly the heads the Runner
    # pushed, and this is the first of them.
    record_pushed_head(worktree, local_head)
    # The closed-Issue guard runs AFTER the push (the work
    # stays on the branch for the human) and BEFORE the PR creation.
    # `gh issue view` is a direct, strongly consistent read — the same
    # property `has_in_progress_label` relies on.
    details = issue_view(issue, "state", repo=source_repo,
                         cwd=worktree, timeout=30)
    state = details.get("state") if isinstance(details, dict) else None
    if state not in ("OPEN", "CLOSED"):
        raise ValueError("issue view state must be OPEN or CLOSED")
    if state == "CLOSED":
        comment_issue(
            issue, repo=source_repo,
            body=(
                f"{run_marker(run_id)}\n"
                f"Orbi delivery stopped: the Issue was closed while the "
                f"delivery was in flight; the completed work stays on "
                f"branch `{branch}` and no PR was opened.\n"
                f"run_id={run_id}"
            ),
        )
        event(
            "delivery_issue_closed", branch=branch, issue=issue,
            repo=source_repo,
        )
        return None
    # Exactly one open PR of the branch: create it when absent (the PR
    # body contract is the Runner's obligation now) and
    # verify it with the full PR contract (exactly one open PR, base,
    # head, run marker, `Fixes #<issue>`, URL). The verify step skips
    # its own base re-fetch: this function just fetched and merged it.
    if open_pr_for_branch(worktree, branch) is None:
        body = (
            f"{run_marker(run_id)}\n\n"
            f"Fixes #{issue}\n\n"
            f"{issue_title} (run_id={run_id})\n"
        )
        if attribution_footer:
            body += (
                "\nBuilt by Orbi from Issue #"
                f"{issue} · https://github.com/orbi-build/orbi\n"
            )
        run_command([
            "gh", "pr", "create", "--base", base_branch, "--head", branch,
            "--title", issue_title, "--body", body,
        ], cwd=worktree)
        event(
            "pr_created", branch=branch, base_branch=base_branch,
            issue=issue,
        )
    return verify_pr(
        ctx, base_branch, repo_dir=repo_dir, require_latest_base=False,
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
        verified_url = verify_pr(
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
                )
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
            report_delivery_failure(
                exc, issue=issue, source_repo=source_repo,
                run_id=current_run_id(), pr_url=scene["pr_url"],
                worktree=worktree, branch=branch, role=ROLE_REVIEW,
                action=(
                    (
                        f"Update PR {scene['pr_url']} to include `Fixes #{number}` "
                        "so GitHub closes the source Issue when it merges; "
                        "for staged work, split the remaining phases into "
                        "child Issues with one PR per Issue."
                    )
                    if isinstance(exc, MissingFixesError)
                    else (
                        "Review the preserved PR and decide how to repair or "
                        "replace its delivery state."
                        if isinstance(exc, UnrecoverableDeliveryError) else ""
                    )
                ),
                reason=(
                    f"The resume verification of PR {scene['pr_url']} "
                    f"failed: {_failure_detail(exc)}"
                ),
                diagnosis=_failure_detail(exc),
                # The branch-gone scene destroyed the
                # delivery state — nothing is left to preserve, and the
                # preserved-objects note would contradict the reason.
                blocked_suffix=(
                    "" if isinstance(exc, ResumeBranchGoneError) else
                    f"; the PR, branch {branch} and worktree {worktree} "
                    "are preserved"
                ),
            )
            reported = True
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        if reported and isinstance(exc, MissingFixesError):
            raise ReportedMissingFixesError(str(exc)) from exc
        raise


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
    pr = _single_open_pr(
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


def _main_ci_triage_url(repo: str, check_name: str) -> str | None:
    """Find the existing auto-created main CI issue, when present."""
    try:
        issues = list_issues(
            repo, state="all",
            search=f"CI failure: {check_name} on branch main",
            json_fields="number,url,title", limit=20,
        )
    except Exception:
        LOGGER.exception("delivery_ci_triage_lookup_failed repo=%s check=%s",
                         repo, check_name)
        return None
    prefix = f"CI failure: {check_name} on branch main"
    for issue in issues:
        if isinstance(issue, dict) and str(issue.get("title", "")).startswith(prefix):
            url = issue.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _raise_if_preexisting_ci_failure(
        repo: str, failed_names: list[str], base_commit: str | None) -> None:
    """Raise a fast, explicit error when base has the same failed check."""
    if not base_commit:
        return
    base_checks = commit_check_runs(repo, base_commit)
    base_failures = {
        check.get("name") for check in base_checks
        if check.get("status") == "completed"
        and check.get("conclusion") not in ("success", "neutral", "skipped")
    }
    for name in failed_names:
        if name not in base_failures:
            continue
        triage_url = _main_ci_triage_url(repo, name)
        suffix = f"; triage: {triage_url}" if triage_url else ""
        raise PreExistingCIFailure(
            f"delivery gate: main is already red on check '{name}' — "
            f"fix main first{suffix}"
        )


def _render_check(entry: dict) -> str:
    """Render one classified check entry for humans, from the data.

    The rendered string is presentation only — never parsed back
    (Issue #906): callers that need the name/status read the entry.
    """
    if entry["status"] == "COMPLETED":
        return (f"check '{entry['name']}' is "
                f"{entry['status']}/{entry['conclusion']}")
    return f"check '{entry['name']}' is {entry['status'] or 'UNKNOWN'}"


def _classify_rollup(rollup: list) -> tuple[list[dict], list[dict]]:
    """Split one PR status check rollup into (pending, failed) evidence.

    Returns STRUCTURED entries carrying `name`, `status` and `conclusion`
    (Issue #906) — callers read the fields; `_render_check` derives the
    human wording from them. A check is pending while its status is
    anything but a final one (GitHub recomputes mergeability and
    registers new CheckRuns asynchronously); a completed check with a
    non-passing conclusion — or a legacy status-context FAILURE/ERROR —
    is failed. Pure: the pre-review CI gate and the merge gate classify
    the same rollup the same way.
    """
    pending: list[dict] = []
    failed: list[dict] = []
    for check in rollup:
        status = str(check.get("status", check.get("state", "")) or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        name = check.get("name", check.get("context", "check"))
        entry = {"name": name, "status": status, "conclusion": conclusion}
        if status not in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR"):
            pending.append(entry)
        elif status == "COMPLETED" and conclusion not in (
            "SUCCESS", "NEUTRAL", "SKIPPED",
        ):
            failed.append(entry)
        elif status in ("FAILURE", "ERROR"):
            failed.append(entry)
    return pending, failed


class DeliveryDeferred(Exception):
    """An intermediate GitHub state asked the delivery to wait.

    Pending CI checks or a still-UNKNOWN mergeability are transient
    states, never failures: the caller journals the
    observation and returns — the next tick re-reads the state. The
    delivery labels stay untouched and the review-round budget does not
    advance, so a deferred tick costs a couple of read calls only.
    """


def merge_gate(worktree: Path, pr: dict, base_branch: str,
               *, repo_dir: Path,
               source_repo: str | None = None) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, the remote head to still be the reviewed head, and the
    exact head's GitHub CI checks to be completed successfully. If the base
    advanced but GitHub reports a conflict-free PR, absorb it with a plain
    merge and push the task branch, then read only the absorbed head's CI gate
    again; this does not start another review round. Every state is read once
    per gate pass: a pending check or an UNKNOWN mergeability is not a failure
    but an intermediate state — the gate raises `DeliveryDeferred`, the caller
    returns, and the next tick re-reads. A failed check or a not-mergeable PR
    prevents the merge. Then merge with `--match-head-commit` so only that
    exact head can land. No force push, no direct push of the protected branch.
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
    if mergeable != "MERGEABLE" and mergeable != "UNKNOWN":
        # Keep the existing mergeability policy and message. The assessment
        # below still classifies the reviewed-head/base combination, while
        # GitHub's mergeability remains the source of this recovery action.
        freshness = assess_base_freshness(
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
    freshness = assess_base_freshness(
        worktree, base_branch, head=pr["head_oid"],
        reviewed_head=state.get("headRefOid"),
        # UNKNOWN is transient, not evidence of a merge conflict. It must
        # reach the normal deferred mergeability path rather than absorb.
        mergeable=mergeable if mergeable == "MERGEABLE" else None,
    )
    if freshness is BaseFreshness.ABSORBABLE and mergeable == "MERGEABLE":
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
        # The push starts a new CI run. Read the state once more, but do not
        # start another review round: only the absorbed head's CI gate is
        # needed before the exact-head merge below.
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
        freshness = assess_base_freshness(
            worktree, base_branch, head=absorbed_head,
            reviewed_head=absorbed_head,
        )
    if freshness is BaseFreshness.CONFLICTED:
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
        _raise_if_preexisting_ci_failure(
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
    try:
        run_command([
            "gh", "pr", "merge", str(pr["number"]),
            "--match-head-commit", pr["head_oid"], "--merge",
        ], cwd=worktree)
    except subprocess.CalledProcessError as exc:
        preflight = github.merge_gate_preflight(
            source_repo or pr.get("_source_repo", ""), base_branch,
        )
        stderr = str(exc.stderr or "")
        if is_maintainer_actionable(stderr, preflight):
            failed_preflight = [
                line for line in preflight
                if line.startswith("merge_gate: FAILED")
            ]
            raise MergeHandoffRequired(
                f"PR #{pr['number']} is ready for the named maintainer action",
                preflight=failed_preflight,
            ) from None
        raise
    event("merged", pr=pr["number"], head=pr["head_oid"])
    return {**pr, "merged": True}


def confirm_merged(worktree: Path, pr: dict, base_branch: str,
                   *, repo_dir: Path) -> dict:
    """Confirm the PR is MERGED and origin/<base> contains the merge commit.

    The base fetch updates the shared remote-tracking ref, so it runs
    under the base-sync lock with the deployment checkout
    as the lock location.
    """
    state = pr_view(pr["number"], "state,mergedAt,mergeCommit", cwd=worktree)
    if state.get("state") != "MERGED" or not state.get("mergedAt"):
        event(
            "confirm_merged_not_merged", level=logging.ERROR,
            pr=pr["number"], state=state.get("state"),
        )
        raise RuntimeError(
            f"PR #{pr['number']} is not merged (state={state.get('state')})"
        )
    merge_commit = (state.get("mergeCommit") or {}).get("oid")
    if not merge_commit:
        raise RuntimeError(
            f"PR #{pr['number']} is merged but has no merge commit oid"
        )
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
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
                         pushed_base: str | None) -> tuple[str, str]:
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
    """
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


def review_rounds_so_far(
    comments: list[dict], *, after: str | None = None,
    run_id: str | None = None, pr_number: int | None = None,
) -> int:
    """Count trusted review rounds for the selected delivery identity.

    With ``run_id`` supplied, only comments carrying that attempt's stable
    marker count. This is essential when an Issue is picked up again after a
    closed PR: historical rounds remain visible evidence, but cannot consume
    the new PR's budget. ``after`` retains the existing human-recovery
    boundary for resumes of the same PR. Calls without an identity filter
    remain available for legacy evidence rendering.
    """
    rounds = 0
    marker = run_marker(run_id) if run_id is not None else None
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        if after is not None and comment.get("createdAt", "") <= after:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if marker is not None and marker not in body:
            continue
        if pr_number is None:
            matches = any(line.startswith("Orbi review round ")
                          for line in body.splitlines())
        else:
            matches = any(
                line.startswith(f"Orbi review round ") and
                f" for PR #{pr_number}:" in line
                for line in body.splitlines()
            )
        if matches:
            rounds += 1
    return rounds


def human_review_recovery_at(number: int, repo: str) -> str | None:
    """Return the latest explicit blocked -> fix-needed recovery time.

    Label history is used rather than the current projection: the current
    ``ai-fix-needed`` label alone cannot distinguish a normal retry from a
    human decision after terminal blocking.
    """
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/issues/{number}/timeline",
        "--paginate", "--jq", ".[]",
    ])
    events: list[dict] = []
    decoded = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(decoded) == 1 and isinstance(decoded[0], list):
        decoded = decoded[0]
    for event in decoded:
        if isinstance(event, dict):
            events.append(event)
    blocked_removed = False
    recovery_at = None
    for event in events:
        label = event.get("label")
        label_name = label.get("name") if isinstance(label, dict) else None
        if event.get("event") == "labeled" and label_name == BLOCKED_LABEL:
            blocked_removed = False
        elif event.get("event") == "unlabeled" and label_name == BLOCKED_LABEL:
            blocked_removed = True
        elif (blocked_removed and event.get("event") == "labeled"
              and label_name == FIX_NEEDED_LABEL):
            recovery_at = event.get("created_at")
            blocked_removed = False
    return recovery_at if isinstance(recovery_at, str) else None


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


# ---------------------------------------------------------------------------
# Startup source freshness: the 2026-09-07 incident — the
# editable install resolved into an OLD issue worktree while the
# ExecStartPre preflight kept the deployment checkout fresh — showed
# that "the checkout gets synced" and "the process executes that
# checkout" are different facts. This gate judges the IMPORT SOURCE of
# the running process (cli_source.module_file), never the
# WorkingDirectory. All probes are LOCAL git reads: the freshness of
# ``refs/remotes/origin/main`` is supplied by the ExecStartPre
# fetch (a linked worktree shares the deployment checkout's refs), so a
# fresh checkout costs zero network requests. The self-check can only protect
# versions that carry it — the outermost defense stays the shell-layer
# ExecStartPre preflight, which does not depend on the runner version
# (docs/operations.mdx, the defense-layer section).
RUNNER_SOURCE_TIMEOUT_SECONDS = 30


class RunnerSourceStaleError(RuntimeError):
    """The running CLI source is not proven to be the configured engine
    source channel's commit (fail fast, before any slot or claim)."""


def _resolve_engine_source(track: str, cwd: Path, *,
                           run_command) -> dict | None:
    """Resolve one lock/release channel locally; None when unresolvable
    (an expected probe result inside the freshness gate)."""
    try:
        return engine_source.resolve_expected_head(
            track, cwd, run_command=run_command,
        )
    except engine_source.EngineSourceError:
        return None


def _parse_release_version(value: str) -> tuple[int, ...] | None:
    """Parse a ``vX.Y.Z`` release version into a comparable tuple.

    Returns None for anything else (the comparison then cannot prove
    freshness and the gate fails — never guesses).
    """
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts = text.split(".")
    if not text or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _orbi_distribution_version() -> str:
    """The installed ``orbi-cli`` distribution version (install
    metadata, not the code's self-reported ``__version__``; Issue #874
    renamed the PyPI distribution — the console script stays ``orbi``).
    Test seam: the non-editable form monkeypatches this module global."""
    import importlib.metadata
    return importlib.metadata.version("orbi-cli")


def _runner_source_git(args: list[str], cwd: Path, *, run_command) -> str | None:
    """One LOCAL read-only git probe; None when git cannot answer
    (an expected probe result, logged at DEBUG by run_command)."""
    try:
        return run_command(
            ["git", *args], cwd=cwd,
            timeout=RUNNER_SOURCE_TIMEOUT_SECONDS,
            failure_log_level=logging.DEBUG,
        )
    except Exception:
        return None


def _runner_source_stale_line(facts: dict, *, allowed: bool, fix: str) -> str:
    fields = " ".join(
        f"{key}={quote_value(str(value))}" for key, value in facts.items()
    )
    return (
        f"runner_source_stale {fields} "
        f"allowed={str(allowed).lower()} fix={quote_value(fix)}"
    )


def check_runner_source_freshness(config: config_domain.RunnerConfig, *, run_command) -> dict:
    """Startup invariant: prove that the code THIS process
    executes is the configured engine source channel's exact commit
    BEFORE any slot or claim (the channel is the
    ``engine_source_track`` — ``origin/main`` by default — never the
    delivery target's ``base_branch``). A stale (or unverifiable) source
    fails fast with the structured ``runner_source_stale`` line (facts +
    the exact fix command, the ``deploy_home_dirty`` style); the explicit
    ``allow_stale_runner`` config downgrades the same line to a warning.

    Two install forms, both judged from git/install metadata facts:

    - editable: ``git rev-parse --show-toplevel`` at the import source
      resolves a checkout, and the checkout carries the src-layout
      package path (a $HOME dotfiles repo never matches ``src/orbi``,
      so it cannot fake an editable install) — then the checkout's
      ``HEAD`` must equal the channel's expected commit: the fetched
      ``refs/remotes/origin/<branch>`` head for the main/branch tracks,
      or the resolved tag commit / exact SHA for the lock tracks. The
      09-07 scene (an editable install bound to an old issue worktree)
      fails here: worktrees share the fetched refs.
    - non-editable: the installed distribution version
      (importlib.metadata) must not be older than the channel's release
      tag — the latest tag reachable from the tracked branch ref, or the
      locked release/tag itself (resolved in the deployment home). A
      ``sha:`` lock cannot be mapped to a version and fails closed as
      unverifiable. Version equality or newer passes (a dev install
      ahead of the tags is not stale).

    Whatever cannot be PROVEN fresh (missing origin ref, no release tag,
    unresolvable import source) fails the same way with a ``reason=``
    field — never a silent pass. Returns the fresh-facts dict; raises
    ``RunnerSourceStaleError`` unless ``allow_stale_runner`` is set.
    """
    # The engine checkout follows the configured ENGINE source channel;
    # ``base_branch`` belongs to the delivery target and must not affect
    # this independent freshness check.
    track = engine_source.normalize_engine_source_track(
        config.engine_source_track,
    )
    kind, argument = engine_source.split_track(track)
    delivery_base_branch = config.base_branch
    deploy_home = Path(config.deploy_home)
    from orbi import cli_source  # lazy: the single cross-module dependency
    module_path = cli_source.module_file()
    package_dir = module_path.parent
    fix = cli_source.reinstall_command(deploy_home)

    toplevel_raw = _runner_source_git(
        ["rev-parse", "--show-toplevel"], package_dir, run_command=run_command,
    )
    toplevel = Path(toplevel_raw).resolve() if toplevel_raw else None
    editable = (
        toplevel is not None
        and toplevel / cli_source.PACKAGE_DIR == package_dir.resolve()
    )

    # For the plain main track the facts keep the pre-#535 field names
    # (engine_source_branch / origin_main); every other channel names
    # what it resolved (expected ref, tag or SHA).
    def _branch_facts(extra: dict) -> dict:
        fields = {"engine_source_branch": argument}
        if argument == "main":
            fields["origin_main"] = extra["origin_main"]
        else:
            fields["expected_ref"] = extra["expected_ref"]
            fields["expected"] = extra["expected"]
        return fields

    if editable:
        head = _runner_source_git(
            ["rev-parse", "HEAD"], toplevel, run_command=run_command,
        )
        if kind == "branch":
            expected_ref = f"refs/remotes/origin/{argument}"
            expected = _runner_source_git(
                ["rev-parse", "--verify", expected_ref], toplevel,
                run_command=run_command,
            )
            if head and expected:
                facts = {
                    "install": "editable", "source": str(toplevel),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "head": head,
                    **_branch_facts({
                        "origin_main": expected,
                        "expected_ref": expected_ref,
                        "expected": expected,
                    }),
                }
                stale_reason = (
                    None if head == expected else "head_is_not_engine_source"
                )
            else:
                facts = {
                    "install": "editable",
                    "source": str(toplevel or package_dir),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "reason": "unverifiable_git_state",
                }
                stale_reason = "unverifiable_git_state"
        else:
            resolved = _resolve_engine_source(
                track, toplevel, run_command=run_command,
            )
            if resolved is not None and head:
                facts = {
                    "install": "editable", "source": str(toplevel),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "head": head,
                    "resolved": resolved["resolved"],
                    "expected": resolved["expected"],
                }
                stale_reason = (
                    None if head == resolved["expected"]
                    else "head_is_not_engine_source"
                )
            else:
                facts = {
                    "install": "editable",
                    "source": str(toplevel or package_dir),
                    "engine_source_track": track,
                    "delivery_base_branch": delivery_base_branch,
                    "reason": "unverifiable_engine_source",
                }
                stale_reason = "unverifiable_engine_source"
    else:
        try:
            version = _orbi_distribution_version()
        except Exception:
            version = None
        parsed_version = (
            _parse_release_version(version) if version else None
        )
        resolved = None
        if kind == "branch":
            expected_tag = _runner_source_git(
                [
                    "describe", "--tags", "--match", "v*", "--abbrev=0",
                    f"refs/remotes/origin/{argument}",
                ],
                deploy_home, run_command=run_command,
            )
        elif kind in ("release", "tag"):
            resolved = _resolve_engine_source(
                track, deploy_home, run_command=run_command,
            )
            expected_tag = resolved["tag"] if resolved else None
        else:
            # A sha: lock has no version mapping: unverifiable -> fail
            # closed.
            expected_tag = None
        parsed_tag = (
            _parse_release_version(expected_tag) if expected_tag else None
        )
        if parsed_version and parsed_tag:
            if kind == "branch" and argument == "main":
                comparison = {"origin_main": expected_tag}
            else:
                comparison = {
                    "resolved": (
                        resolved["resolved"] if resolved else expected_tag
                    ),
                }
            facts = {
                "install": "non_editable", "source": str(module_path),
                "engine_source_track": track,
                "delivery_base_branch": delivery_base_branch,
                "version": version,
                **comparison,
            }
            stale_reason = (
                None if parsed_version >= parsed_tag
                else "version_older_than_latest_tag"
            )
        else:
            facts = {
                "install": "non_editable", "source": str(module_path),
                "engine_source_track": track,
                "delivery_base_branch": delivery_base_branch,
                "reason": "unverifiable_version_state",
            }
            stale_reason = "unverifiable_version_state"

    if stale_reason is None:
        event("runner_source", result="fresh", **facts)
        return facts
    allowed = bool(config.allow_stale_runner)
    event(
        "runner_source_stale",
        **facts, allowed=str(allowed).lower(), fix=fix,
        level=logging.WARNING if allowed else logging.ERROR,
    )
    if not allowed:
        raise RunnerSourceStaleError(
            _runner_source_stale_line(facts, allowed=allowed, fix=fix),
        )
    return facts


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


def _shorten_review_shas(text: str) -> str:
    """Keep full object IDs in the scene, but readable IDs in prose."""
    return re.sub(
        r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
        lambda match: match.group(1)[:10], text, flags=re.IGNORECASE,
    )


def _review_findings_markdown(findings: list[dict]) -> str:
    """Render the human part of a review verdict, never as JSON."""
    items = []
    for finding in findings:
        items.append(
            "- **Level:** " + str(finding.get("level", "")) + "\n"
            "  **Location:** " + str(finding.get("location", "")) + "\n"
            "  **Note:** " + str(finding.get("note", "")) + "\n"
            "  **Fix:** " + str(finding.get("fix", ""))
        )
    return "\n".join(items)


def review_round_comment_body(
    marker: str, round: int, pr_number: int, blockers: int, majors: int,
    findings: list[dict], scene_block: str, *,
    previous_comments: list[dict] | None = None,
    messages: list[str] | None = None,
    heading: str | None = None,
) -> str:
    """Build a readable, resumable review-round comment.

    The first human line is deliberately stable: ``review_rounds_so_far``
    counts it. ``scene_block`` is appended untouched because it is the
    machine-readable recovery protocol.
    """
    rendered = _review_findings_markdown(findings)
    same_as = None
    if rendered and previous_comments:
        previous_prefix = f"Orbi review round {round - 1} for PR #{pr_number}:"
        for comment in previous_comments:
            if not _comment_is_trusted(comment):
                continue
            body = comment.get("body", "")
            if (marker in body and previous_prefix in body
                    and rendered in body):
                same_as = round - 1
                break
    lines = [
        f"{marker}",
        heading or (
            f"Orbi review round {round} for PR #{pr_number}: "
            f"{blockers} blocker(s), {majors} major(s)."
        ),
    ]
    if messages:
        lines.extend(["", "\n\n".join(
            _shorten_review_shas(message) for message in messages
        )])
    if rendered:
        lines.extend(["", "### Findings", ""])
        if same_as is not None:
            lines.append(f"Findings are the same as round {same_as}.")
            lines.append("")
        lines.append(_shorten_review_shas(rendered))
    return "\n".join(lines) + "\n" + scene_block


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: config_domain.RunnerConfig, source_repo: str,
                              number: int, title: str, priority: str,
                              *, scene: dict,
                              previous_comments: list[dict] | None = None,
                              merge_only: bool = False) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    `title` is the issue's GitHub title: the review
    progress scenes (ensure, findings, merged) show `#<number> <title>`
    like every other scene; it is required, never fabricated.

    `scene` is the delivery's recovered resume scene (run_id, base,
    PR URL, `review_round`, `scene_at`): the round budget reads the
    scene's `review_round` field (the scene is the
    waiting primitive's state anchor), and every round comment carries
    the updated scene block, so the next resume continues the count.

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
    # The round budget lives in the scene: `review_round`
    # counts the COMPLETED rounds, each recorded by the round comment
    # that carried the updated scene block.
    rounds = int(scene["review_round"])
    base_advance_rounds = int(scene.get("base_advance_round", 0))
    recovery_at = None
    if rounds >= MAX_REVIEW_ROUNDS and not merge_only:
        # A maintainer may repair an external prerequisite and
        # explicitly move the terminal Issue back to ai-fix-needed. That
        # transition establishes a new budget for this same PR; old review
        # comments remain immutable evidence and are not counted again.
        recovery_at = human_review_recovery_at(number, source_repo)
        scene_at = scene.get("scene_at")
        if recovery_at is not None and (
            not isinstance(scene_at, str) or recovery_at > scene_at
        ):
            rounds = 0
            base_advance_rounds = 0
            event(
                "review_budget_recovered", issue=number,
                recovery_at=recovery_at, rounds=rounds,
            )
        if rounds >= MAX_REVIEW_ROUNDS:
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
    external_pr_url = scene["pr_url"] if scene.get("external") else None
    pr = freeze_pr(
        worktree, branch, base_branch,
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    # Issue #877: a round that STARTS behind the base is under the absorb
    # contract — the session must end with the branch containing
    # origin/<base> or with a findings verdict reporting the abandoned
    # absorb, never a bare `pass` over an unrelated push. An unreadable
    # local ref leaves the round unarmed (the gate's own ancestor check
    # against the fresh fetch still guards the merge either way).
    try:
        absorb_required = not merge_only and not _is_ancestor(
            f"origin/{base_branch}", pr["head_oid"], cwd=worktree,
        )
    except subprocess.CalledProcessError:
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
        record_pushed_head(worktree, pr["head_oid"])
        event(
            "pushed_head_recorded", pr=pr["number"],
            head=pr["head_oid"], round=round,
        )
    if recovery_at is not None:
        # Only the explicit recovery path reaches this branch. Check the
        # latest PR CI before spending the newly granted review budget.
        log_recovery_ci_status(pr, source_repo)
    publisher = ProgressPublisher(
        number, source_repo, config.run_id, run_command=run_command,
    )
    publish = functools.partial(
        _safe_publish, run_id=config.run_id, issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
    )
    started = time.monotonic()
    ctx = RunContext(
        run_id=config.run_id, issue=number, branch=branch,
        worktree=worktree, source_repo=source_repo,
    )
    # Ensure is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    publish(
        action=lambda: publisher.ensure(_progress_body(_progress_state(
            ctx, title=title, role=ROLE_REVIEW, started=started,
            pr_url=pr["url"], review_round=round, priority=priority,
        ))),
    )
    if merge_only:
        verdict = {
            "verdict": "pass", "head": pr["head_oid"],
            "findings": [], "blockers": 0, "majors": 0,
        }
        review_summary = "maintainer-actionable merge retry"
    else:
        output = pi_session.run_review(
            ctx, pr, config, round,
            progress=LiveProgressThrottle(
                ctx, publisher, title=title, role=ROLE_REVIEW,
                started=started, pr_url=pr["url"], review_round=round,
                priority=priority,
            ),
        )
        verdict = parse_review_verdict(output)
        fixed_findings = len(verdict["findings"])
        review_summary = (
            "pass, no findings"
            if fixed_findings == 0
            else f"pass, {fixed_findings} findings fixed in-session"
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
        raise HumanDecisionRequired(
            "review requires human decision: " + decisions,
            action=actions or "Decide how this PR should proceed.",
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
            previous_comments=previous_comments,
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], repo=source_repo, body=body)
        # The findings publishing is bypass — a 404 here
        # must not stop the `ai-fix-needed` transition below (the next
        # review session retries the same PR either way).
        publish(
            action=lambda: publisher.milestone(
                f"review findings: round {round}, "
                f"{verdict['blockers']} blocker(s), "
                f"{verdict['majors']} major(s) for PR #{pr['number']}"
            ),
        )
        publish(
            action=lambda: publisher.finish(_progress_body(
                _progress_state(
                    ctx, title=title, role=ROLE_REVIEW, started=started,
                    pr_url=pr["url"], review_round=round,
                    priority=priority,
                ), outcome=(
                    "**Orbi review findings**\n\n"
                    f"round {round}: {verdict['blockers']} blocker(s), "
                    f"{verdict['majors']} major(s); the next review "
                    "session retries the same PR automatically"
                ),
            )),
        )
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
        record_pushed_head(worktree, refrozen["head_oid"])
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
        body = (
            f"{marker}\n"
            f"Orbi external PR review round {round} for "
            f"PR #{pr['number']}: verdict={verdict['verdict']}, "
            f"blockers={verdict['blockers']}, majors={verdict['majors']}, "
            f"minors={verdict['minors']}; CI={state} ({checks}).\n\n"
            "<details>\n<summary>Review report</summary>\n\n"
            "````\n" + output.rstrip("\n") + "\n````\n\n</details>\n\n"
            "The engine does not merge external PRs and does not set a "
            "Milestone: a maintainer decides whether to accept this "
            "contribution and which version it ships in. The Issue "
            "waits at ai-blocked until then "
            f"(run_id={config.run_id})"
        )
        comment_issue(number, repo=source_repo, body=body)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=issue_labels(number, source_repo),
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
                " Absorb contract violated (machine-checked): the round "
                f"started with the head already behind origin/{base_branch} "
                "and ended with verdict=pass while the head still does not "
                f"contain origin/{base_branch} — the session neither merged "
                "the base in-session nor reported the abandoned absorb as "
                "findings; the next review session must merge "
                f"origin/{base_branch} into the branch or emit findings "
                "stating the attempted-and-abandoned absorb with the "
                "concrete reason, never an unrelated push"
            )
        next_base_advance_round = base_advance_rounds + (0 if ci_failure else 1)
        base_advance_exhausted = (
            not ci_failure and next_base_advance_round > MAX_BASE_ADVANCE_ROUNDS
        )
        if ci_failure:
            gate_messages = [
                f"CI merge gate blocked: {message} (run_id={config.run_id})",
                f"The next review session merges the latest origin/{base_branch} "
                "into the branch in-session, resolves conflicts, and reruns "
                "the full test suite",
            ]
        else:
            gate_messages = [
                f"Merge gate blocked: {message} (run_id={config.run_id})",
                ("base-advance retry budget exhausted; a human must resolve "
                 "the hot base before this PR can continue."
                 if base_advance_exhausted else
                 f"The next review session merges the latest origin/{base_branch} "
                 "into the branch in-session, resolves conflicts, and reruns "
                 "the full test suite"),
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
            messages=gate_messages,
            heading=(None if ci_failure else
                     f"Orbi base advance retry {next_base_advance_round} for "
                     f"PR #{pr['number']}:"),
        )
        # CI evidence is best-effort observability.  A GitHub comment
        # outage must not prevent the required ai-fix-needed transition.
        try:
            comment_issue(number, repo=source_repo, body=body)
            comment_pr(pr["number"], repo=source_repo, body=body)
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

    try:
        merged = merge_gate(
            worktree,
            {**refrozen, "_source_repo": source_repo},
            base_branch, repo_dir=config.repo_dir,
        )
    except MergeHandoffRequired as exc:
        # A known policy blocker is a successful, resumable handoff.
        handoff_head = refrozen["head_oid"]
        failed_lines = "\n".join(exc.preflight)
        handoff_body = (
            f"{marker}\n"
            f"Orbi: PR #{refrozen['number']} is delivered and waiting for "
            "a maintainer action.\n\n"
            f"PR #{refrozen['number']} at head `{handoff_head}` is blocked "
            "by this repository policy:\n````\n"
            f"{failed_lines}\n````\n\n"
            "After the named policy action, Orbi will resume and merge the PR."
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_AWAITING_MERGE,
            current_labels=issue_labels(number, source_repo),
        )
        comment_issue(number, repo=source_repo, body=handoff_body)
        event(
            "delivery_awaiting_human_merge", issue=number,
            pr=refrozen["number"], head=handoff_head,
        )
        return False
    except DeliveryDeferred as exc:
        # A pending check or an UNKNOWN mergeability on the
        # reviewed head is an intermediate state, not a failure — the
        # next tick re-reads it. No comment, no label change, no round
        # consumed: the scene's counter only advances on round comments.
        event(
            "review_merge_deferred", pr=refrozen["number"], round=round,
            reason=str(exc),
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
    confirmed = confirm_merged(
        worktree, merged, base_branch, repo_dir=config.repo_dir,
    )
    # The merged-as-is fact (Issue #833): every count failure degrades
    # to `unknown` inside merge_commit_metrics — a landed merge is
    # never re-failed by its own record.
    external_commits, pr_commits = merge_commit_metrics(
        worktree, confirmed["merge_commit"], read_pushed_head(worktree),
        read_pushed_base(worktree),
    )
    # The merged publishing is bypass — the GitHub merge
    # already landed; a 404 here must not stop the `ai-merged`
    # transition and the merged PR scene comment below.
    publish(
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"external_commits={external_commits} "
            f"commits={pr_commits})"
        ),
    )
    publish(
        action=lambda: publisher.finish(_progress_body(
            _progress_state(
                ctx, title=title, role=ROLE_REVIEW, started=started,
                pr_url=merged["url"], review_round=round,
                priority=priority, review=review_summary,
            ), outcome=(
                "**Orbi delivered**\n\n"
                f"PR {merged['url']} merged "
                f"(merge_commit={confirmed['merge_commit']} "
                f"review_rounds={round})"
            ),
        )),
    )
    # The GitHub merge already landed. Record ai-merged before touching
    # the local systemd checkout: a checkout that cannot fast-forward is
    # runner ops, not a failed delivery (must not become ai-blocked).
    # Read the label projection after the merge: a resumed fix round may
    # have both `ai-in-progress` and `ai-fix-needed`.
    # The merged patch clears every delivery-state label actually present.
    apply_label_patch(
        number, repo=source_repo, event=EVENT_MERGED,
        current_labels=issue_labels(number, source_repo),
    )
    comment_issue(
        number, repo=source_repo,
        body=merged_pr_comment_body(
            config.run_id, merged["url"], confirmed["merge_commit"], round,
            external_commits, pr_commits, base_branch, review_summary,
        ),
    )
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


def comment_pr(number: int, *, repo: str, body: str) -> None:
    """Comment on a PR in the configured source repository.

    The same `format_status_comment` rendering as `comment_issue`: the
    PR-side copy of a round / finding / blocked comment carries the run
    marker and the runner fingerprint like its Issue twin.
    """
    rendered = format_status_comment(body)
    run_gh_write_command(
        ["gh", "pr", "comment", str(number), "--repo", repo,
         "--body", rendered],
        command_runner=run_command,
        already_applied=lambda: any(
            comment.get("body") == rendered
            for comment in pr_comments(number, repo=repo)
        ),
    )


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


def delivery_head_advanced(worktree: Path, base_sha: str) -> bool:
    """True when the task branch has commits beyond the frozen base."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head != base_sha


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


def human_review_checklist(
    worktree: Path, config: config_domain.RunnerConfig, *, run_id: str, pr_url: str,
) -> str:
    """Build the human acceptance checklist comment for one delivery.

    Evidence is read from the delivery worktree only (the test log plus
    the committed diff against the frozen base) — no session, no extra
    GitHub read. `base_sha` is always present on the same run that
    posts the checklist; a resumed re-derivation falls back to the
    configured base branch.
    """
    base = config.base_sha or config.base_branch
    return human_review.render_checklist_comment(
        run_id=run_id,
        pr_url=pr_url,
        checklist=human_review.build_checklist(
            test_result=read_test_result(worktree),
            changed_files=delivered_changed_files(worktree, base),
        ),
    )


def _human_review_column2(worktree: Path, config: config_domain.RunnerConfig) -> list[str]:
    """Recompute the checklist's column 2 from local delivery evidence.

    The review-round gate's per-tick cost: local file reads only — no
    session, no GitHub write. Missing evidence lands IN column 2 (the
    gate holds), so only real evidence can pass it.
    """
    base = config.base_sha or config.base_branch
    return human_review.build_checklist(
        test_result=read_test_result(worktree),
        changed_files=delivered_changed_files(worktree, base),
    )["column2"]


_TEST_EXIT_RE = re.compile(r"\bexit\s*[:=]\s*(-?\d+)\b", re.IGNORECASE)
_TEST_OUTCOME_COUNT_RE = re.compile(
    r"\b(\d+)\s+(?:failed|failures?|errors?)\b", re.IGNORECASE,
)
_TEST_FAILURE_EVIDENCE_RE = re.compile(
    r"^\s*(?:FAILED|ERROR)\b", re.IGNORECASE,
)


def _test_result_failed(result: str) -> bool:
    """Classify a test result without treating prose or zero counts as errors."""
    exits = [int(value) for value in _TEST_EXIT_RE.findall(result)]
    if any(value != 0 for value in exits):
        return True

    counts = _TEST_OUTCOME_COUNT_RE.findall(result)
    if any(int(count) > 0 for count in counts):
        return True
    return bool(_TEST_FAILURE_EVIDENCE_RE.search(result))


def _failure_detail(exc: BaseException) -> str:
    """One-line failure description; keeps bounded subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()[:1000]}"
    return detail


_LOCAL_PATH_RE = re.compile(
    r"(?:/home/[^\s`),]+|/Users/[^\s`),]+|/tmp/[^\s`),]+|"
    r"/workspace/[^\s`),]+|/workspaces/[^\s`),]+)"
)


def _redact_local_paths(value: str) -> str:
    """Keep GitHub comments free of host-specific filesystem paths."""
    return _LOCAL_PATH_RE.sub("local runner path", value)


def _failure_summary(reason: str) -> str:
    """Turn a runner exception description into a short reader summary."""
    stderr_match = re.search(r"\s+stderr=(.*)$", reason,
                             flags=re.IGNORECASE | re.DOTALL)
    stderr = stderr_match.group(1).strip() if stderr_match else ""
    summary = re.sub(r"\s+stderr=.*$", "", reason,
                     flags=re.IGNORECASE | re.DOTALL)
    # A CalledProcessError renders the entire argv between ``Command`` and
    # ``returned``. Match that semantic boundary rather than the first closing
    # bracket: an argv value may itself contain ``]``.
    summary = re.sub(
        r"Command\s+.+?\s+returned non-zero exit status\s+\d+",
        "the delivery command failed", summary,
        flags=re.IGNORECASE | re.DOTALL,
    )
    summary = re.sub(r"CalledProcessError\([^)]*\)",
                     "the delivery command failed", summary)
    summary = _redact_local_paths(summary)
    summary = re.sub(r"\s+", " ", summary).strip(" .;:")
    if not summary:
        summary = "the delivery command failed"
    if stderr:
        # Setup tools often write informational lines before the provider's
        # concrete error. The final non-empty line is the actionable result.
        stderr_summary = _redact_local_paths(
            stderr.rstrip().rsplit("\n", 1)[-1].strip()
        )
        if stderr_summary and stderr_summary not in summary:
            summary = f"{summary}: {stderr_summary}"
    return summary[:500]


_SGR_RE = re.compile(r"(?:\x1b\[[0-9;]*m|\^\[\[[0-9;]*m)")


def _tail_text(path: Path, *, lines: int = 20, chars: int = 4000) -> str:
    """Read a bounded, de-coloured tail for failure evidence."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # UTF-8 characters are at most four bytes.  This is enough to
            # retain the requested character tail without reading a huge
            # test log or session file into memory.
            window = min(size, chars * 4 + 1)
            handle.seek(size - window)
            content = handle.read(window).decode(
                "utf-8", errors="replace",
            )
    except (OSError, UnicodeError):
        return "<unavailable>"
    tail = "\n".join(content.splitlines()[-lines:])
    tail = _SGR_RE.sub("", tail)
    return tail[-chars:] if len(tail) > chars else tail


def _latest_session_file(worktree: Path | None) -> Path | None:
    """The most recent pi session log in the worktree, or None."""
    if worktree is None:
        return None
    session_files = sorted(
        (p for p in (worktree / ".pi-session").glob("*.jsonl")
         if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    return session_files[-1] if session_files else None


def _fenced(content: str, *, cap: int = 4000) -> str:
    """One raw-output segment inside a CommonMark code fence.

    GitHub renders fenced content literally and monospaced, so coverage
    tables and escaped JSONL survive a failure comment unread by the
    markdown parser. The fence is one backtick longer than every run in
    the content, so a payload containing markdown fences can never close
    it; content past `cap` is cut and the cut is noted after the fence.
    """
    omitted = 0
    if len(content) > cap:
        omitted = len(content) - cap
        content = content[:cap]
    fence = "`" * max(
        3,
        1 + max((len(run) for run in re.findall(r"`+", content)), default=0),
    )
    block = f"{fence}\n{content}\n{fence}"
    if omitted:
        block += f"\n_[truncated, {omitted} chars omitted]_"
    return block


# The number of session records a failure comment summarizes.
SESSION_SUMMARY_LIMIT = 20


def _session_summary(session_file: Path,
                     *, limit: int = SESSION_SUMMARY_LIMIT) -> str:
    """Structured summary of the last session records.

    One line per parsed record — timestamp, type, role, tool name and
    content-block kinds; the record text itself never enters the comment
    (the full log path is named next to the segment). A record whose
    text blocks repeat the previous record's is skipped: pi logs the
    final assistant text twice (once inside the full message, once
    standalone), which used to duplicate whole paragraphs in the
    comment. Unparsable tail lines are skipped; an unreadable file is
    the `<unavailable>` placeholder.
    """
    try:
        with session_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 65536)
            handle.seek(size - window)
            content = handle.read(window).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return "<unavailable>"
    summary: list[str] = []
    previous_text: str | None = None
    for line in reversed(content.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        text = "".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text and text == previous_text:
            continue
        previous_text = text or None
        parts = [str(record.get("type", "?"))]
        if message.get("role"):
            parts.append(f"role={message['role']}")
        if message.get("toolName"):
            parts.append(f"tool={message['toolName']}")
        kinds = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "?"))
            if kind == "toolCall" and block.get("name"):
                kind = f"toolCall:{block['name']}"
            kinds.append(kind)
        if kinds:
            parts.append(f"content={','.join(kinds)}")
        summary.append(f"{record.get('timestamp', '-')} {' '.join(parts)}")
        if len(summary) >= limit:
            break
    if not summary:
        return "<unparsable>"
    return "\n".join(reversed(summary))


def _failure_scene(snapshot: dict, *, run_id: str, issue: str, role: str,
                   branch: str, worktree: str, pr_url: str | None = None) -> str:
    """Render run facts as wrapping Markdown, never as a host-path dump."""
    fields = [
        ("run", run_id), ("issue", issue), ("role", role),
        ("branch", branch),
        ("worktree", "local runner worktree"),
        ("session", snapshot.get("session_id") or "-"),
        ("session log", "local session log"),
        ("phase", snapshot.get("phase") or "-"),
        ("last activity", snapshot.get("last_activity") or "-"),
        ("action", snapshot.get("action") or "-"),
        ("result", snapshot.get("result") or "-"),
    ]
    if snapshot.get("session_file"):
        fields[6] = ("session log", "local session log")
    else:
        fields[6] = ("session log", "<unavailable>")
    rendered_fields = [
        f"- {key}: `{_redact_local_paths(str(value))}`"
        for key, value in fields
    ]
    if pr_url:
        rendered_fields.insert(1, f"- PR: [{pr_url}]({pr_url})")
    rendered = "\n".join(rendered_fields)
    return rendered + (
        "\n- legacy correlation: "
        f"`run={run_id} branch={branch} session="
        f"{snapshot.get('session_id') or '-'} phase={snapshot.get('phase') or '-'} "
        f"last_activity={snapshot.get('last_activity') or '-'}`"
    )


def _failure_comment_body(*, outcome: str, action: str, reason: str,
                          diagnosis: str, scene: str, evidence: str,
                          pr_url: str | None, issue: str, run_id: str) -> str:
    """Build the reader-facing hierarchy shared by classified failures."""
    retrying = outcome == "fix needed"
    headline = (
        "Orbi: fix needed — the engine will retry"
        if retrying else "Orbi: blocked — waiting on a human decision"
    )
    parts = [headline]
    if pr_url:
        parts.append(f"PR: [{pr_url}]({pr_url})")
    parts.append(f"Issue: `{issue}` · run_id={run_id}")
    if action:
        parts.append(f"**Action:** {_redact_local_paths(action)}")
    parts.append(f"**Reason:** {_failure_summary(reason)}")
    disposition = "Orbi needs a fix" if retrying else "Orbi failed"
    branch_match = re.search(r"- branch: `([^`]+)`", scene)
    session_match = re.search(r"- session: `([^`]+)`", scene)
    correlation = (
        f"run={run_id} branch={branch_match.group(1) if branch_match else '-'} "
        f"session={session_match.group(1) if session_match else '-'}"
    )
    details = [
        "<details><summary>Diagnosis</summary>",
        f"- disposition: `{disposition}: see the reason above`",
        scene,
        # Stable correlation keys retain the old journal lookup shape while
        # local path fields above remain reader-safe labels.
        f"- correlation: `{correlation}`",
    ]
    if diagnosis:
        reason_summary = _failure_summary(reason)
        diagnosis_summary = _failure_summary(diagnosis)
        if diagnosis_summary == reason_summary or diagnosis_summary in reason_summary:
            label = "Orbi needs a fix" if retrying else "Orbi failed"
            details.append(f"- failure detail: `{label}: see the reason above`")
        else:
            details.append(f"- failure detail: `{diagnosis_summary}`")
    if evidence:
        details.append(evidence.lstrip())
    details.append("</details>")
    return "\n\n".join(parts + ["\n".join(details)])


def _failure_evidence(worktree: Path | None, exc: BaseException) -> str:
    """Render the bounded evidence that survives terminal worktree cleanup.

    Pi subprocess streams come from ``CalledProcessError``. The session and
    test log are read before cleanup and only bounded, fenced segments are
    copied into the failure comment: raw output stays literal on GitHub,
    the session tail is a structural summary whose duplicates are removed,
    and the full session log is named for the deep-dive. The
    failure reason itself stays the readable head of the comment.
    """
    stderr = getattr(exc, "stderr", None)
    stdout = getattr(exc, "output", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    stderr = str(stderr) if stderr else "<empty>"
    stdout = str(stdout) if stdout else "<empty>"
    return_code = getattr(exc, "returncode", None)
    session_file = _latest_session_file(worktree)
    session = (
        _session_summary(session_file) if session_file else "<unavailable>"
    )
    session_label = "local session log" if session_file else "<unavailable>"
    test_log = "<unavailable>"
    if worktree is not None:
        test_path = worktree / ".orbi" / "test.log"
        if test_path.is_file():
            test_log = _tail_text(test_path)
    return (
        "\n\nFailure evidence (captured before cleanup):\n"
        f"exit_code={return_code if return_code is not None else '<unknown>'}\n"
        f"\nstderr_tail:\n{_fenced(_redact_local_paths(stderr))}\n"
        f"\nstdout_tail:\n{_fenced(_redact_local_paths(stdout))}\n"
        "\nsession_last_events "
        f"(last {SESSION_SUMMARY_LIMIT} records; full log: "
        f"{session_label}):\n{_fenced(_redact_local_paths(session))}\n"
        f"\ntest_log_tail:\n{_fenced(_redact_local_paths(test_log))}"
    )


def _live_progress(ctx: RunContext, publisher: ProgressPublisher, *,
                   title: str, role: str, started: float,
                   pr_url: str | None, review_round: int, priority: str,
                   activity: dict | None = None) -> None:
    """One live GitHub progress update while a Pi session is running.

    Called from the `stream_pi` poll loop (every activity change or
    heartbeat): the same run-marker comment is PATCHed in
    place at most every `PI_HEARTBEAT_SECONDS` or when the visible
    activity changed. `activity` is the watcher state of that poll, so
    the live comment shows the session exactly as the journal reports
    it. The publisher already knows the comment id after `ensure`, so a
    callback before it would fail fast here — and the wiring always
    ensures first.
    """
    state = _progress_state(
        ctx, title=title, role=role, started=started,
        pr_url=pr_url, review_round=review_round, priority=priority,
        activity=activity,
    )
    publisher.patch(_progress_body(state))


class LiveProgressThrottle:
    """Throttle live GitHub PATCHes to change-driven or <=30-second cadence.

    The `stream_pi` poll loop fires on every poll (15 s default); PATCHing
    GitHub on every poll would double the traffic for no visible gain.
    The throttle passes an update through when the visible activity
    (phase, action, result, model_wait) changed since the last PATCH or
    when at least `PI_HEARTBEAT_SECONDS` passed since it.
    """

    def __init__(self, ctx: RunContext, publisher: ProgressPublisher, *,
                 title: str, role: str, started: float,
                 pr_url: str | None, review_round: int,
                 priority: str) -> None:
        def publish(activity: dict) -> None:
            _live_progress(
                ctx, publisher, title=title, role=role,
                started=started, pr_url=pr_url,
                review_round=review_round, priority=priority,
                activity=activity,
            )

        self._publish = publish
        self._last_visible: tuple | None = None
        self._last_patch = 0.0

    def __call__(self, activity: dict) -> None:
        visible = (
            activity["phase"], activity["action"], activity["result"],
            activity["model_wait"],
            # The idle-recovery state is visible progress:
            # entering/leaving it PATCHes the live comment immediately.
            activity.get("recovery"),
        )
        now = time.monotonic()
        if visible == self._last_visible and \
                now - self._last_patch < PI_HEARTBEAT_SECONDS:
            return
        self._last_visible = visible
        self._last_patch = now
        self._publish(activity)


def _report_resume_failure(*, number: int, source_repo: str, run_id: str,
                           error: Exception) -> None:
    """Report a failed resume decision through the terminal path.

    The worktree of this issue exists but its run state cannot be
    verified: continuing on a guessed identity risks a
    silent fresh redo on top of unknown work, and a fresh run would
    lose the existing work. The Issue goes `ai-blocked` ALONE (the
    claim label removed) with the exact reason in the failure comment
    — a human decides (restore or remove the worktree, then re-label).
    """
    apply_label_patch(
        number, repo=source_repo, event=EVENT_BLOCKED,
        current_labels={IN_PROGRESS_LABEL},
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{run_marker(run_id)}\n"
            f"Orbi failed: cannot continue the interrupted run: "
            f"{_failure_detail(error)} (run_id={run_id})"
        ),
    )


class IssueResult(NamedTuple):
    """The single outcome contract returned by :func:`process_issue`."""

    kind: str
    url: str | None


def _dispatch_release(issue: dict, config: config_domain.RunnerConfig,
                      source_repo: str) -> IssueResult:
    """Deliver a RELEASE-scene ticket through the deterministic release
    state machine: a first-class task type that NEVER enters
    the normal `run_pi` development path (scope verification, gates,
    tests, tag, GitHub Release)."""
    number = int(issue["number"])
    # `orbi.release` imports the runner primitives back, so the
    # dispatch imports it lazily here — a module-level import would
    # be circular.
    #
    # The ready scan's stale snapshot can hand an
    # already-claimed release ticket to a second live runner. The
    # dev path got the direct-read yield in #658; the release path
    # needs the same semantics — an in-progress release owned by a
    # LIVE co-runner is yielded this tick (never run the state
    # machine concurrently); an orphaned one (this runner is alone)
    # still resumes inside process_release (#98 restart resume).
    # The millisecond truly-simultaneous window remains, exactly as
    # documented for #658 — the label write is not a CAS.
    if has_in_progress_label(number, source_repo):
        slot_dir = config.slot_dir
        max_concurrency = config.max_concurrency
        if (slot_dir is not None and max_concurrency is not None
                and _another_live_runner(slot_dir, max_concurrency)):
            event(
                "claim_yield",
                issue=number,
                reason="release_in_progress_live_runner",
            )
            return IssueResult("claim-yielded", None)
    from orbi import release

    return IssueResult(
        "release", release.process_release(issue, config, source_repo),
    )


def _dispatch_content_only(issue: dict, config: config_domain.RunnerConfig,
                           source_repo: str) -> IssueResult:
    """Deliver a CONTENT_ONLY-scene ticket through the ticket-only
    content agent: no execution, the deliverable posted to the Issue."""
    process_ticket_only(issue, config, source_repo)
    return IssueResult("ticket-only", None)


def _gather_claim_facts(issue: dict, config: config_domain.RunnerConfig,
                        source_repo: str,
                        repo_policy: RepoPolicy | None) -> DeliveryFacts:
    """Gather the claim facts of one attempt (the dispatch's first step).

    The probe sequence is `process_issue`'s prologue, order unchanged:
    the attempt binds its run id before any other step is logged,
    the repository policy applies, the live
    `ai-in-progress` state is read directly, then the
    fresh-claim probes (the stable branch's open PR, the branch
    existence, the external takeover of a marker ticket) or the
    in-flight probes (the external takeover, the resume worktree) run.

    A resume worktree that cannot be verified is RECORDED as
    `resume_error`, not raised here: the handler reports it after its
    claim-yield guard, so a live co-runner's claim is never blocked
    from under it — the order the classification refactor inherited.
    """
    number = int(issue["number"])
    # The run id is generated once per attempt and bound BEFORE any
    # other step is logged, so every journal line of the attempt
    # carries it — including the claim-time lines of the restart resume
    # scan below. It is bound before
    # the repository-policy read so a forbidden/malformed
    # repository file blocks the claim with a run-marked comment.
    run_id = new_run_id()
    set_run_id(run_id)
    # Repository-level config-as-code: the caller resolved
    # `.github/orbi.toml` (or the entry's `config_path`) from the source
    # repo's default branch tip ONCE for the whole delivery (a missing
    # file is the None no-op) and it is applied here per key over the host
    # config (D3, idempotent — the tick already applied it). A file that
    # exists but violates the schema never reaches this point: the caller
    # blocked the claim fast with the offending keys.
    repo_config_fields: dict = {}
    if repo_policy is not None:
        config = apply_repo_policy(config, source_repo, repo_policy)
        # D4 change visibility: the previous run's sha is read from the
        # trusted Orbi comments (best-effort audit), and the previous
        # policy is re-read from its blob for the effective diff summary.
        previous_sha = previous_repo_config_sha(number, source_repo)
        previous_policy = (
            read_repo_config_at(
                source_repo, previous_sha,
                path=config_domain.repository_config_path(config, source_repo),
                run_command=run_command,
            ) if previous_sha else None
        )
        repo_config_fields = repo_config_audit(
            repo_policy.sha, repo_policy,
            previous_sha=previous_sha,
            previous_policy=previous_policy,
        )
    base_branch = config.base_branch
    # The claim label is a delivery-policy key; the lifecycle
    # labels stay host constants.
    dispatch_label = (config.dispatch_label or READY_LABEL)
    claim_labels = claim._issue_label_set(issue)
    stable_branch = task_branch(source_repo, number)
    in_progress = has_in_progress_label(number, source_repo)
    takeover_pr: dict | None = None
    external_takeover = False
    stable_branch_present = False
    resume_scene: tuple[str, Path] | None = None
    resume_error: Exception | None = None
    if (not in_progress
            and (dispatch_label in claim_labels
                 or FIX_NEEDED_LABEL in claim_labels)):
        # A scene-less fix-needed delivery selected by the resumable scan
        # reaches this same stable-branch takeover as a queued fresh claim.
        # The open PR skips implementation and a new trusted scene is written
        # before review; an absent PR keeps classification unclaimable.
        takeover_pr = open_pr_for_branch(config.repo_dir, stable_branch)
        stable_branch_present = stable_branch_exists(
            config.repo_dir, stable_branch,
        )
        if takeover_pr is None:
            # A triage Issue whose body routes to an EXTERNAL
            # contributor PR takes that PR over for review FIRST — the
            # same takeover primitive as a stable-branch PR (run_pi is
            # skipped, the PR goes straight to the delivery wait loop).
            takeover_pr = external_takeover_pr(
                config.repo_dir, issue.get("body"), source_repo,
                base_branch,
            )
            external_takeover = takeover_pr is not None
        route = claim_route(
            claim_labels,
            branch_exists=stable_branch_present,
            open_pr=takeover_pr is not None,
            ready_label=dispatch_label,
        )
        event(
            "fresh_claim_route", issue=number, branch=stable_branch,
            route=route, open_pr=takeover_pr is not None,
        )
        if route == "review":
            event("delivery_takeover", issue=number, branch=stable_branch,
                  pr=takeover_pr.get("url"))
    if in_progress:
        # An in-flight external takeover (the run died between
        # the worktree creation and the opened-PR transition) must NOT
        # resume into `run_pi` on the contributor's branch — the external
        # PR, when still open, is the takeover delivery.
        takeover_pr = external_takeover_pr(
            config.repo_dir, issue.get("body"), source_repo, base_branch,
        )
        external_takeover = takeover_pr is not None
        try:
            resume_scene = worktree_resume_scene(
                config.repo_dir, source_repo, number,
            )
        except Exception as exc:
            # The worktree of this issue exists but its run
            # state is missing or corrupt: the same run cannot be
            # verified. Recorded here; the handler fails fast through
            # the terminal failure path (`ai-blocked` + the reason
            # comment) after its yield guard — never a silent fresh
            # redo on top of unknown work.
            LOGGER.exception(
                "issue=%s resume_continue_failed", number,
            )
            resume_error = exc
    return DeliveryFacts(
        # The classification merges the direct read into the query-time
        # labels: a claim that landed in the scan window classifies as
        # RESTART_IN_FLIGHT and the handler's yield guard decides.
        labels=claim_labels
        | ({IN_PROGRESS_LABEL} if in_progress else frozenset()),
        pr_state="OPEN" if takeover_pr is not None else None,
        body_markers=body_markers(issue.get("body")),
        ready_label=dispatch_label,
        config=config,
        repo_policy=repo_policy,
        run_id=run_id,
        base_branch=base_branch,
        dispatch_label=dispatch_label,
        claim_labels=claim_labels,
        in_progress=in_progress,
        stable_branch=stable_branch,
        takeover_pr=takeover_pr,
        external_takeover=external_takeover,
        resume_scene=resume_scene,
        resume_error=resume_error,
        repo_config_fields=repo_config_fields,
    )


def _dispatch_implementation(issue: dict, source_repo: str,
                             facts: DeliveryFacts,
                             *, ops: bool) -> IssueResult:
    """Run one implementation scene to its delivery outcome.

    The handler of FRESH_CLAIM (the normal dev delivery),
    RESTART_IN_FLIGHT (the restart resume), EXTERNAL_TAKEOVER
    (the contributor-PR takeover) and OPS (the full-execution ops
    session: the same claim, worktree and run
    machinery — no command whitelist exists anywhere — with the ops
    playbook instead of the dev one and an evidence-on-the-Issue
    closeout when the session delivers no commit). `facts` carries the
    gathered probe results; the classified scene chose this handler.
    """
    number = int(issue["number"])
    title = issue["title"]
    config = facts.config
    run_id = facts.run_id
    base_branch = facts.base_branch
    dispatch_label = facts.dispatch_label
    claim_labels = facts.claim_labels
    in_progress = facts.in_progress
    stable_branch = facts.stable_branch
    stable_branch_present = facts.stable_branch_present
    takeover_pr = facts.takeover_pr
    external_takeover = facts.external_takeover
    existing_worktree = facts.resume_scene[1] if facts.resume_scene else None
    repo_config_fields = facts.repo_config_fields
    # The scene-less #1216 takeover was selected only because the scan saw
    # an open stable-branch PR. Recheck it in the dispatch gather: if the PR
    # closed in that window, yield without claiming or re-implementing the
    # branch. A later human re-open/requeue supplies a new claimable fact.
    if (FIX_NEEDED_LABEL in claim_labels
            and dispatch_label not in claim_labels
            and takeover_pr is None):
        event(
            "claim_yield", issue=number,
            reason="scene_less_fix_pr_not_open",
        )
        return IssueResult("claim-yielded", None)
    if in_progress:
        # The claim-race window has two halves. The scan
        # snapshot lacking the label while THIS direct read sees it
        # means the claim landed between the scan and the read. Whether
        # that claimant is still ALIVE decides the semantics: with a
        # live co-runner holding a slot, resuming here would reuse the
        # live run's worktree/run_id under a second Pi (or fork a
        # duplicate delivery) — yield. With nobody else on the slots
        # (the #18 single-instance restart, the #668 second tick) the
        # run is an orphan and resumes below exactly as before. A
        # wrongly-yielded orphan is picked up next tick by the
        # slot-guarded in-flight scan — one cadence late, never
        # stranded.
        slot_dir = config.slot_dir
        max_concurrency = config.max_concurrency
        if (IN_PROGRESS_LABEL not in claim_labels
                and slot_dir is not None and max_concurrency is not None
                and _another_live_runner(slot_dir, max_concurrency)):
            event(
                "claim_yield", issue=number,
                reason="label_landed_in_scan_window",
            )
            return IssueResult("claim-yielded", None)
        if facts.resume_error is not None:
            # The worktree of this issue exists but its run
            # state is missing or corrupt: the same run cannot be
            # verified. Fail fast through the terminal failure path
            # (`ai-blocked` + the reason comment) — never a silent
            # fresh redo on top of unknown work.
            _report_resume_failure(
                number=number, source_repo=source_repo, run_id=run_id,
                error=facts.resume_error,
            )
            raise facts.resume_error
        if facts.resume_scene is not None:
            run_id = facts.resume_scene[0]
            # The attempt continues the dead run: re-bind the reused
            # id so every later line (including resuming_run) carries
            # it.
            set_run_id(run_id)
            event(
                "resuming_run", issue=number, run_id=run_id,
            )
    base_sha = freeze_base(config.repo_dir, base_branch)
    branch = task_branch(source_repo, number, run_id)
    if existing_worktree is not None:
        # The resumed run keeps its ORIGINAL branch —
        # after a repo rename the re-derived name would carry the NEW
        # slug and no longer match the branch the worktree is on (a
        # second branch would be a second delivery). The worktree's
        # current branch IS the scene's branch.
        branch = run_command(
            ["git", "branch", "--show-current"],
            cwd=existing_worktree,
        )
    elif external_takeover:
        # The external takeover delivers the contributor's own
        # branch — the identity the takeover PR is frozen on.
        branch = takeover_pr["headRefName"]
    # Pickup priority: derived from the scanned issue's
    # labels (no extra gh call) and carried on every journal line and
    # scene comment of the attempt via `run_info`.
    priority = issue_priority(issue)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id} "
        f"priority={priority}"
    )
    if ops:
        run_info += " task_type=ops"
    if facts.repo_policy is not None and facts.repo_policy.sha is not None:
        # The run comment carries the repository config sha
        # (the file blob at the default branch tip) so a policy change is
        # always visible on the run.
        run_info += f" repo_config={facts.repo_policy.sha}"
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    if not in_progress and dispatch_label in claim_labels \
            and takeover_pr is None:
        # The pickup scan and the in-progress recheck above
        # both predate freeze_base (a seconds-long network round trip).
        # A label — or the stable branch — appearing inside that window
        # means another instance claimed this Issue while we were
        # preparing: yield. No label writes, no comments, nothing that
        # could interrupt the winner's in-flight delivery; the next
        # tick's scan picks work up again on its own. The
        # predicate keys on the repository's dispatch label (#527), not
        # the default constant — a custom-label repository's tickets
        # never carry `ai-ready`, and the guard must guard them too.
        if has_in_progress_label(number, source_repo):
            event(
                "claim_yield", issue=number, reason="in_progress_label",
            )
            return IssueResult("claim-yielded", None)
        if not stable_branch_present and stable_branch_exists(
                config.repo_dir, stable_branch,
        ):
            event(
                "claim_yield", issue=number,
                reason="stable_branch_appeared",
            )
            return IssueResult("claim-yielded", None)
    apply_label_patch(
        number, repo=source_repo, event=EVENT_CLAIM,
        current_labels=claim_labels,
    )
    # The successful pickup resets the stale-pickup clock in
    # the health state file (bypass — a state-write failure never fails
    # the claim).
    try:
        runner_health.record_pickup(config.repo_dir)
    except Exception:
        LOGGER.exception("issue=%s health_pickup_record_failed", number)
    # The Issue is in flight from the claim label on: bind the stop
    # scene so a SIGTERM during this tick logs the active
    # Issue context, not only systemd's generic "Stopped" line. The
    # branch and worktree path are the same derived values the
    # worktree creation below uses (bound before the worktree exists);
    # the run identity is bound once as a frozen RunContext and
    # the failure/finish paths unpack it from there.
    ctx = RunContext(
        run_id=run_id, issue=number, branch=branch,
        worktree=existing_worktree or worktree_path(
            config.repo_dir, source_repo, number, run_id,
        ),
        source_repo=source_repo,
    )
    set_active_run(ctx, title)
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publish = functools.partial(
        _safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_IMPLEMENT,
    )
    worktree: Path | None = None
    started = time.monotonic()
    # The PR label is authoritative once the PR exists. A scene-comment
    # notification is recoverable reporting: if GitHub still rejects it
    # after bounded retries, never replace the real PR-ready state with
    # ai-blocked.
    pr_opened = False
    try:
        worktree = create_worktree(
            config.repo_dir, source_repo, number, run_id, base_sha,
            existing=existing_worktree,
            # A stable branch without an open PR is the interrupted push/
            # create gap: continue from that branch rather than trying to
            # create a second local branch with the same name.
            existing_branch=stable_branch_present or takeover_pr is not None,
            # An external takeover checks out the
            # contributor's own head branch — the identity the takeover
            # PR is frozen on.
            branch=(
                takeover_pr["headRefName"] if external_takeover else None
            ),
            pr_number=(
                takeover_pr["number"] if external_takeover else None
            ),
        )
        # The run state file is the same-run marker —
        # written for EVERY run (a fresh one included, so a later
        # interruption can be verified and resumed), refreshed for a
        # resumed one (same run id, never a second marker).
        ctx = replace(ctx, worktree=worktree)
        write_run_state(ctx)
        if external_takeover:
            # The takeover's engine push line starts on the
            # contributor's own head (Issue #608): the merge record's
            # external_commits (Issue #833) must subtract only what the
            # engine pushes on top of this foreign head, never the head
            # itself. No session has run yet, so HEAD is exactly that
            # head; the record is set once per run.
            record_pushed_base(worktree, run_command(
                ["git", "rev-parse", "HEAD"], cwd=worktree,
            ))
        # The new session starts from the existing work —
        # the uncommitted changes and the previous session's progress —
        # instead of a fresh redo. A clean worktree without a previous
        # session is a fresh scene (None, the pre-#219 prompt).
        resume_ctx = pi_session.resume_context(worktree)
        if resume_ctx is not None:
            session_dir = worktree / ".pi-session"
            previous_sessions = (
                len([p for p in session_dir.glob("*.jsonl")
                    if p.is_file()])
                if session_dir.is_dir() else 0
            )
            snapshot = activity_snapshot(session_dir)
            event(
                "resume_continue", issue=number, worktree=worktree,
                changed_files=len(pi_session.changed_files(worktree)),
                reused_runs=previous_sessions,
                previous_session=(
                    snapshot.get("session_id") if snapshot else None
                ) or "-",
            )
        config = replace(config, base_sha=base_sha, run_id=run_id)
        if ops:
            # The ops session runs the ops playbook — the
            # sibling of the configured dev prompt (a custom prompt
            # deployment carries prompt_ops.md next to it). A missing
            # file fails the run fast through the delivery failure
            # path with the exact path in the comment.
            config = replace(
                config, prompt=config.prompt.with_name("prompt_ops.md"),
            )
        comment_issue(
            number, repo=source_repo,
            body=started_pi_comment_body(
                ctx, run_info,
                extra_fields=repo_config_fields,
            ),
        )
        # The whole ProgressPublisher path is a bypass — a
        # failure here (404, rate limit) is logged and never skips
        # `run_pi` or fails the delivery.
        publish(
            action=lambda: publisher.ensure(_progress_body(
                _progress_state(
                    ctx, title=title, role=ROLE_IMPLEMENT,
                    started=started, pr_url=None, review_round=0,
                    priority=priority,
                ),
            )),
        )
        if takeover_pr is None:
            pi_session.run_pi(
                issue, ctx, config,
                resume_context=resume_ctx,
                progress=LiveProgressThrottle(
                    ctx, publisher, title=title, role=ROLE_IMPLEMENT,
                    started=started, pr_url=None, review_round=0,
                    priority=priority,
                ),
            )
        publish(
            action=lambda: milestone_bookkeeping._publish_plan_milestone(publisher, worktree),
        )
        publish(
            action=lambda: milestone_bookkeeping._publish_test_milestone(
                publisher, worktree, _test_result_failed,
            ),
        )
        if ops:
            # An ops delivery without a commit is COMPLETE —
            # the evidence the session posted on the Issue (per-step real
            # command output / API responses, the #526 fact culture) is
            # the deliverable. Pure ops actions take no PR ceremony; the
            # terminal transition mirrors the content path (the claim
            # label is removed, the Issue is closed; the worktree stays
            # as the run's evidence). Committed code (or uncommitted
            # leftovers) falls through to the deterministic closeout
            # below: committed ops code takes the normal PR ceremony,
            # leftovers fail fast there (the runner never commits
            # uncommitted changes).
            head, dirty = _agent_delivery_boundary(worktree)
            if head == base_sha and not dirty:
                run_command([
                    "gh", "issue", "close", str(number),
                    "--repo", source_repo,
                ])
                edit_issue(
                    number, repo=source_repo, remove=IN_PROGRESS_LABEL,
                )
                publish(
                    action=lambda: publisher.milestone(
                        f"ops delivered: {run_info}",
                    ),
                )
                publish(
                    action=lambda: publisher.finish(_progress_body(
                        _progress_state(
                            ctx, title=title, role=ROLE_IMPLEMENT,
                            started=started, pr_url=None, review_round=0,
                            priority=priority,
                        ),
                        outcome="**Orbi ops delivered**",
                    )),
                )
                event(
                    "run_end",
                    format_end_scene(
                        run_id=run_id,
                        issue=issue_context(source_repo, number),
                        role=ROLE_IMPLEMENT, result="ops_delivered",
                        elapsed=time.monotonic() - started,
                        pr="-", commit=head,
                    ),
                )
                # The delivered outcome breaks any failure
                # streak of this Issue in the health history. Pure
                # bypass: a state-write failure never changes the
                # delivery outcome.
                try:
                    runner_health.record_run_attempt(
                        runner_health.health_state_path(config.repo_dir),
                        repo=source_repo, issue=number, run_id=run_id,
                        outcome="ops_delivered", fingerprint="",
                    )
                except Exception:
                    LOGGER.exception(
                        "issue=%s health_success_record_failed", number,
                    )
                return IssueResult("ops", None)
        # The deterministic closeout (commit boundary, base
        # freshness + absorb, plain push, PR creation, PR verification)
        # is the Runner's job — the agent stopped at the committed
        # delivery.
        pr_url = (
            takeover_pr["url"] if takeover_pr is not None else deliver_pr(
                ctx, base_branch, base_sha,
                issue_title=title, repo_dir=config.repo_dir,
                attribution_footer=config.attribution_footer,
            )
        )
        ctx = replace(ctx, pr=pr_url)
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        if pr_url is None:
            # The Issue was closed during delivery — the
            # delivery is complete with no PR (deliver_pr already left
            # the explanatory comment). No label patch (labels are moot
            # on a closed Issue, and a later reopen resumes the run
            # naturally), no scene comment, nothing to wait for: the
            # tick ends cleanly.
            publish(
                action=lambda: publisher.finish(_progress_body(
                    _progress_state(
                        issue=number, title=title, run_id=run_id,
                        role=ROLE_IMPLEMENT, branch=branch,
                        worktree=worktree, started=started,
                        pr_url=None, review_round=0, priority=priority,
                    ),
                    outcome="**Orbi stopped: Issue closed during delivery**",
                )),
            )
            event(
                "run_end",
                format_end_scene(
                    run_id=run_id, issue=issue_context(source_repo, number),
                    role=ROLE_IMPLEMENT, result="issue_closed",
                    elapsed=time.monotonic() - started,
                    pr="-", commit=commit,
                ),
            )
            # The delivered outcome breaks any failure
            # streak of this Issue in the health history. Pure bypass.
            try:
                runner_health.record_run_attempt(
                    runner_health.health_state_path(config.repo_dir),
                    repo=source_repo, issue=number, run_id=run_id,
                    outcome="issue_closed", fingerprint="",
                )
            except Exception:
                LOGGER.exception(
                    "issue=%s health_success_record_failed", number,
                )
            return IssueResult("issue-closed", None)
        # The implementer always commits the delivery on top of the
        # frozen base, so the head always advanced.
        apply_label_patch(
            number, repo=source_repo, event=EVENT_PR_OPENED,
            current_labels={IN_PROGRESS_LABEL},
        )
        # The label transition has landed and must remain the state
        # reported if the following notification write exhausts.
        pr_opened = True
        try:
            comment_issue(
                number, repo=source_repo,
                body=opened_pr_comment_body(
                    run_id, run_info, pr_url, external=external_takeover,
                ),
            )
        except Exception as exc:
            # The PR is the delivery boundary: notification failure must
            # never turn an already-open PR into a blocked delivery. The
            # next tick can retry the notification from the PR scene.
            LOGGER.warning(
                "issue=%s post-PR notification failed pr=%s: %s",
                number, pr_url, exc,
            )
            event(
                "progress_comment_failed", level=logging.WARNING,
                issue=issue_context(source_repo, number), pr=pr_url,
                reason=str(exc),
            )
        if config.human_review_gate:
            # The human acceptance checklist — the readable
            # face of the gate — posts ONCE per delivery, at the moment
            # the delivery completes (the PR opens). Bypass:
            # a failed checklist never fails the delivery; the gate
            # itself is the label check in the review rounds, and a
            # checklist-less delivery still holds there when column 2
            # is non-empty.
            try:
                comment_issue(
                    number, repo=source_repo,
                    body=human_review_checklist(
                        worktree, config,
                        run_id=run_id, pr_url=pr_url,
                    ),
                )
            except Exception as exc:
                LOGGER.warning(
                    "issue=%s post-PR notification failed pr=%s: %s",
                    number, pr_url, exc,
                )
                event(
                    "progress_comment_failed", level=logging.WARNING,
                    issue=issue_context(source_repo, number), pr=pr_url,
                    reason=str(exc),
                )
        publish(
            action=lambda: publisher.finish(_progress_body(_progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT, started=started,
                pr_url=pr_url, review_round=0, priority=priority,
            ), outcome="**Orbi delivered**")),
        )
        event(
            "run_end",
            format_end_scene(
                run_id=run_id, issue=issue_context(source_repo, number),
                role=ROLE_IMPLEMENT, result="pr_opened",
                elapsed=time.monotonic() - started,
                pr=pr_url, commit=commit,
            ),
        )
        # The delivered outcome breaks any failure streak of
        # this Issue in the health history (the streak-break contract of
        # `repeated_failure_findings`). Pure bypass: a state-write
        # failure never changes the delivery outcome.
        try:
            runner_health.record_run_attempt(
                runner_health.health_state_path(config.repo_dir),
                repo=source_repo, issue=number, run_id=run_id,
                outcome="pr_opened", fingerprint="",
            )
        except Exception:
            LOGGER.exception("issue=%s health_success_record_failed", number)
        # An external takeover reports its own kind — the
        # delivery wait then closes the triage Issue itself after the
        # merge (the external PR body carries no `Fixes #N` for this
        # Issue, so GitHub never closes it natively).
        return IssueResult(
            "external-pr" if external_takeover else "pr", ctx.pr,
        )
    except (ModelWaitDeadError, RecoverablePiFailure) as exc:
        # Classified Pi/model infrastructure failures are
        # recoverable. Keep the claim, worktree and run-state file so the
        # next in-flight scan resumes this same run.
        recoverable_name = (
            "model_wait recovered"
            if isinstance(exc, ModelWaitDeadError)
            else "Pi failure recovered"
        )
        # The hung-model-request recovery is a CLASSIFIED,
        # AI-recoverable failure — NOT the terminal `ai-blocked`. The
        # worktree keeps the interrupted work and the run state file is
        # intact, so the Issue keeps `ai-in-progress`: the next tick's
        # in-flight restart scan (`pick_in_progress_issue`) resumes the
        # SAME run (same run id, branch, worktree, progress comment).
        # The recovery stays fail-fast (Pi was killed, the tick ends
        # cleanly below, the slot is released by `main`'s `finally`);
        # only the label outcome changes — never `ai-blocked`.
        LOGGER.exception("issue=%s %s", number, recoverable_name)
        detail = _failure_detail(exc)
        body = (
            f"{run_marker(run_id)}\n"
            f"Orbi {recoverable_name}: {detail}; the run is "
            "recoverable — the Issue stays ai-in-progress and the next "
            f"tick resumes the same run ({run_info})"
        )
        # The recovery comment is the delivery record, but the resume
        # does not parse it (the run state file, the worktree and the
        # `ai-in-progress` label carry the resume —
        # `worktree_resume_scene`): a failure here must only log.
        # Falling through to the generic handler below would mark the
        # Issue `ai-blocked` — exactly the unrecoverable state Issue
        # #227 forbids for this recovery.
        # An identical repeated failure of this run updates
        # the existing scene comment in place (the progress patch path)
        # instead of appending a duplicate on every retry tick.
        try:
            publisher.failure_scene(body)
        except Exception:
            LOGGER.exception(
                "issue=%s model_wait_recovered_comment_failed", number,
            )
        publish(
            action=lambda: publisher.finish(_progress_body(_progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT, started=started,
                pr_url=None, review_round=0, priority=priority,
            ), outcome=(
                f"**Orbi {recoverable_name}**\n\n"
                f"failure: {detail}\n"
                "next step: nothing — the Issue stays ai-in-progress "
                "and the next tick resumes the same run (same run id, "
                "branch, worktree)"
            ))),
        )
        # The recoverable failure reaches the health history
        # exactly like the terminal one — the resume loop retries the
        # same dead end, which IS the repeating-dead-end scene the
        # self-health check exists to catch (#246). Without this record
        # the #227 recovery path is invisible to it. Pure bypass: a
        # state-write failure never changes the delivery outcome.
        try:
            runner_health.record_run_attempt(
                runner_health.health_state_path(config.repo_dir),
                repo=source_repo, issue=number, run_id=run_id,
                outcome="failed",
                fingerprint=runner_health.failure_fingerprint(exc),
            )
        except Exception:
            LOGGER.exception("issue=%s health_failure_record_failed", number)
        return IssueResult("failed", None)
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        # Record the failed run attempt (conservative failure
        # fingerprint) so the next tick's self-health check can detect a
        # repeating dead end (the #246 scene). Pure bypass: a state-write
        # failure never changes the delivery outcome.
        try:
            runner_health.record_run_attempt(
                runner_health.health_state_path(config.repo_dir),
                repo=source_repo, issue=number, run_id=run_id,
                outcome="failed",
                fingerprint=runner_health.failure_fingerprint(exc),
            )
        except Exception:
            LOGGER.exception("issue=%s health_failure_record_failed", number)
        try:
            # The shared terminal reporter: every failure
            # reaching this handler is terminal by design (the
            # recoverable Pi failures have their own handler above), so
            # it never classifies — `ai-blocked` with the plain
            # `Orbi failed` template. The current delivery-state label
            # is derived from the `pr_opened` flag (the only label
            # present at this point): `ai-pr-opened` when the PR
            # transition landed, otherwise `ai-in-progress` (the claim
            # label) — docs/workflow.mdx label lifecycle:
            # `ai-pr-opened` is removed on terminal failure.
            report_delivery_failure(
                exc, issue=issue, source_repo=source_repo,
                run_id=run_id, pr_url=None,
                worktree=worktree, branch=branch, role=ROLE_IMPLEMENT,
                action=(
                    "Fix the failure described below and re-run this Issue."
                ),
                reason=(
                    "The delivery stopped before it could be completed: "
                    f"{_failure_detail(exc)}"
                ),
                diagnosis=(
                    f"{_failure_detail(exc)}\n"
                    f"- base_branch: {base_branch}\n"
                    f"- base_sha: {base_sha}\n"
                    f"run_id={run_id}\n"
                    f"- priority: {priority}"
                ),
                classify=False, evidence=True,
                current_labels=(
                    {PR_OPENED_LABEL} if pr_opened else {IN_PROGRESS_LABEL}
                ),
                review_round=0,
                finish=(
                    worktree is not None
                    and publisher.comment_id is not None
                ),
                publisher=publisher,
            )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        else:
            # The terminal evidence is recorded (journal +
            # `Orbi failed` comment) and the Issue is genuinely
            # `ai-blocked` — the scene is never needed again (a retry
            # gets a new run id and worktree), so clean it up. The
            # recoverable paths (ModelWaitDeadError, ai-fix-needed) and
            # the simulated-kill scene (blocked transition never landed)
            # never reach this branch — the worktree is kept for the
            # same-run resume.
            if worktree is not None:
                cleanup_task_worktree(ctx, config.repo_dir)
        # The failure is terminal — the Issue is `ai-blocked`
        # and the `Orbi failed` comment is posted above. Returning
        # `None` ends the tick cleanly: `main` skips the delivery wait
        # (there is no PR) and the slot is released by its `finally`.
        # Re-raising here would escape `main` and crash the service on an
        # already-handled delivery failure (the #239 scene: the
        # `delivery_no_commit` RuntimeError killed the tick). When the
        # reporting itself failed the Issue keeps `ai-in-progress`, and
        # the next tick's restart-resume scan recovers it — no crash
        # needed for either outcome.
        return IssueResult("failed", None)


# The scene dispatch tables. Phase one routes the
# task-type scenes off the ticket-face facts alone — before any attempt
# state is bound: a release or content ticket never binds a run id and
# never applies the repository policy, exactly as before. Phase two
# routes the implementation family off the gathered facts.
_TASK_TYPE_DISPATCH: dict[DeliveryScene, Callable[..., IssueResult]] = {
    DeliveryScene.RELEASE: _dispatch_release,
    DeliveryScene.CONTENT_ONLY: _dispatch_content_only,
}
_DELIVERY_DISPATCH: dict[DeliveryScene, Callable[..., IssueResult]] = {
    DeliveryScene.FRESH_CLAIM: _dispatch_implementation,
    DeliveryScene.RESTART_IN_FLIGHT: _dispatch_implementation,
    DeliveryScene.EXTERNAL_TAKEOVER: _dispatch_implementation,
    DeliveryScene.OPS: _dispatch_implementation,
}


def process_issue(issue: dict, config: config_domain.RunnerConfig, source_repo: str,
                  repo_policy: RepoPolicy | None = None) -> IssueResult:
    """Deliver one claimed Issue by its explicit delivery scene.

    Three steps, at two fact depths: the ticket-face facts
    classify first — the task-type scenes dispatch immediately — then
    the probes gather the claim facts, the classification runs again on
    the full fact set, and the scene's handler is looked up and run.
    The classification is the same pure `classify` the scans run, so
    the pickup and the dispatch cannot disagree about the scene (the
    #726 class of two-layer inconsistency).

    A classified scene outside the dispatch tables means the ticket's
    state is one only a scan can own (the opened-PR resumes, or a
    terminal state the pickup guards exclude): the implementation
    handler is the fallthrough, exactly the flow the pre-classification
    code ran for every non-task-type ticket — the claim decision itself
    stays with the scans.
    """
    number = int(issue["number"])
    # The progress comment's issue line shows the number
    # AND the title in every scene. The scanned issue dict always
    # carries the GitHub title (every scan fetches `title`); a missing
    # or non-string title fails fast here (KeyError / ValueError in
    # `progress.issue_field`) — it is never fabricated.
    title = issue["title"]
    ticket_scene = classify(
        labels=claim._issue_label_set(issue), scene=None, pr_state=None,
        body_markers=body_markers(issue.get("body")),
    )
    task_handler = _TASK_TYPE_DISPATCH.get(ticket_scene)
    if task_handler is not None:
        return task_handler(issue, config, source_repo)
    facts = _gather_claim_facts(issue, config, source_repo, repo_policy)
    delivery = facts.classify_scene()
    handler = _DELIVERY_DISPATCH.get(delivery)
    if handler is None:
        handler = _dispatch_implementation
    return handler(issue, source_repo, facts,
                   ops=delivery is DeliveryScene.OPS)


def _finish_outcome_body(*, outcome: str, detail: str,
                         next_step: str, pr_url: str | None,
                         number: int, source_repo: str,
                         action: str | None = None,
                         reason: str | None = None,
                         diagnosis: str | None = None) -> str:
    """Render a terminal outcome with action, reason, and diagnosis."""
    if not next_step:
        event(
            "progress_finish_missing_next_step", level=logging.WARNING,
            call_point="_finish_progress_body", issue=number,
            repo=source_repo, outcome=outcome,
        )
    legacy_action = action is None
    blocked = outcome == "blocked"
    disposition = (
        "waiting on a human decision" if blocked
        else "the engine will retry"
    )
    if reason is None:
        reason = (
            "Orbi could not complete the delivery because a required "
            "operation failed." if blocked else
            "Orbi found a problem that must be fixed before it can continue."
        )
    if diagnosis is None:
        diagnosis = detail
    paragraphs = [f"**Orbi {outcome} — {disposition}**"]
    if legacy_action:
        user_action = next_step if blocked and next_step else "Nothing"
        paragraphs.append(f"What you need to do: {user_action}")
    elif action:
        paragraphs.append(f"What you need to do: {action}")
    if reason:
        paragraphs.append(f"What happened: {reason}")
    else:
        paragraphs.append("What happened: No reason was provided.")
    if legacy_action:
        if blocked and pr_url:
            engine_action = (
                "Orbi will wait for the required action; the open PR is "
                f"{pr_url}."
            )
        elif blocked:
            engine_action = "Orbi will wait for a human to resolve this Issue."
        else:
            engine_action = (
                next_step or "Orbi will wait for a human to resolve this Issue."
            )
        paragraphs.append(f"What Orbi will do next: {engine_action}")
    elif not action and next_step:
        paragraphs.append(f"What Orbi will do next: {next_step}")
    paragraphs.append(
        "<details><summary>Raw error</summary>\n"
        f"{diagnosis}\n"
        "</details>"
    )
    return "\n\n".join(paragraphs)


def _finish_progress_body(*, number: int, title: str, run_id: str,
                          role: str, branch: str | None,
                          worktree: Path | None, pr_url: str | None,
                          review_round: int, priority: str, detail: str,
                          next_step: str, outcome: str,
                          source_repo: str, action: str | None = None,
                          reason: str | None = None,
                          diagnosis: str | None = None) -> str:
    """Render the terminal progress scene shared by every finish path."""
    return _progress_body(_progress_state(
        RunContext(
            run_id=run_id, issue=number, branch=branch or "-",
            worktree=worktree or Path("-"), source_repo=source_repo,
        ),
        title=title, role=role, started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=_finish_outcome_body(
        outcome=outcome, detail=detail, next_step=next_step, pr_url=pr_url,
        number=number, source_repo=source_repo, action=action,
        reason=reason, diagnosis=diagnosis,
    ))


def _finish_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, next_step: str, title: str, outcome: str,
    role: str = ROLE_REVIEW, review_round: int = 0,
    priority: str = "normal",
) -> None:
    """Finish the tracked progress comment with the terminal scene.

    One function for both terminal scenes: `outcome` is
    the scene headline — `blocked` (the terminal failure,
    the same body the `process_issue` failure path writes) or
    `fix needed` (the recoverable failure that keeps the
    Issue in the automatic fix loop, the next timer resuming the same
    run, branch, worktree and PR). `title` is the issue's GitHub title:
    the scene shows `#<number> <title>` like every other
    progress scene; it is required, never fabricated.

    `ensure` finds the run's existing progress comment by its hidden
    marker (PATCHing it in place) or creates it when the run never
    reached one; either way the scene is the final state. `role`,
    `review_round` and `priority` are the actual role, completed
    review rounds and pickup priority of the run: the caller derives
    them from the Issue's trusted review-round comments and labels,
    so the terminal comment never shows a stale hardcoded role/round
    (review round 2, PR #42). The only post-PR role is
    `review` (the review session fixes findings in the same session),
    so the default is `ROLE_REVIEW`.
    """
    if run_id is None:
        return
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publisher.ensure(_finish_progress_body(
        number=number, title=title, run_id=run_id, role=role,
        branch=branch, worktree=worktree, pr_url=pr_url,
        review_round=review_round, priority=priority, detail=detail,
        next_step=next_step, outcome=outcome, source_repo=source_repo,
    ))


# The failure-scene snapshot placeholder — the fields a
# failure comment shows when no session file exists yet (the Pi never
# started or the session dir is gone). One constant for every reporter.
_SNAPSHOT_PLACEHOLDER: dict = {
    "session_id": None, "session_file": None,
    "phase": "starting",
    "last_activity": None, "action": None,
    "result": None,
}


def _snapshot_or_placeholder(session_dir: Path, *, number: int) -> dict:
    """Best-effort activity snapshot for a failure scene.

    The watcher state when a session file exists, the placeholder scene
    when none does yet, and the placeholder again — with the read
    failure logged — when the snapshot itself fails: best-effort
    observability, never a second failure.
    """
    try:
        snapshot = activity_snapshot(session_dir)
    except Exception:
        LOGGER.exception("issue=%s activity scene failed", number)
        return dict(_SNAPSHOT_PLACEHOLDER)
    return snapshot if snapshot is not None else dict(_SNAPSHOT_PLACEHOLDER)


# The fixed phrases of the two failure templates: the
# classified blocked branch names WHY automatic recovery is impossible,
# the recoverable branch names the automatic next step.
_BLOCKED_PRECONDITION_PHRASE = (
    "; this is an external precondition the AI cannot safely judge or "
    "fix, so it cannot be recovered automatically (the Issue stays "
    "ai-blocked until a human decides)"
)
_FIX_NEEDED_PHRASE = (
    "; the Issue stays ai-fix-needed and the next tick resumes the "
    "same run, branch, worktree and PR"
)
# The whole failure-comment body is capped well below the
# GitHub comment limit; the cut is noted with the full session log path.
FAILURE_COMMENT_MAX_CHARS = 20000

# The dead-loop limit (Issue #825): the same (run_id, failure
# fingerprint) recurring to this many CONSECUTIVE failures escalates the
# recoverable classification to `ai-blocked` — the premises never
# changed, so the next tick would only repeat the same failure (the
# orbi-cloud#360 scene: 82 identical failures, one slot pinned all day).
FAILURE_STREAK_LIMIT = 3


def _is_line_failure_comment(body: str, run_id: str,
                             fingerprint: str) -> bool:
    """True when one comment is the failure record of this exact
    (run_id, fingerprint) pair — the hidden `orbi:fail` marker plus the
    run marker (Issue #825)."""
    fail = FAILURE_MARKER_PATTERN.search(body)
    return (
        fail is not None
        and fail.group(1) == fingerprint
        and run_id in set(RUN_MARKER_PATTERN.findall(body))
    )


def _reported_failure_comment(comments: list, run_id: str,
                              fingerprint: str) -> dict | None:
    """The trusted comment already reporting this exact
    (run_id, fingerprint) failure, or None — the #825 dedup key. A pure
    scan over the already-fetched comment list."""
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if isinstance(body, str) and _is_line_failure_comment(
                body, run_id, fingerprint):
            return comment
    return None


def _failure_streak(comments: list, run_id: str,
                    fingerprint: str) -> int:
    """The number of CONSECUTIVE identical failures at the tail of the
    trusted comment history (Issue #825). A pure scan over the
    already-fetched comment list.

    A matching failure comment adds its repeat count — the dedup keeps
    ONE comment per (run_id, fingerprint) and bumps its counter in
    place, so the counter IS the occurrence count. A DIFFERENT failure
    ends the streak; a scene block (an opened PR, a completed review
    round) ends it too — the delivery line advanced, the premises
    changed. Publisher milestones and human chatter in between are
    skipped: they change no premise."""
    streak = 0
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _is_line_failure_comment(body, run_id, fingerprint):
            streak += failure_repeat_count(body)
            continue
        if FAILURE_MARKER_PATTERN.search(body) or scene.carries_scene_block(
                body):
            break
    return streak


def report_delivery_failure(
    exc: BaseException, *, issue: dict, source_repo: str,
    run_id: str | None, pr_url: str | None, worktree: Path | None,
    branch: str | None, role: str, cause: str | None = None,
    action: str = "", reason: str | None = None,
    diagnosis: str | None = None, evidence: bool = False,
    classify: bool = True, current_labels: set[str] | None = None,
    blocked_suffix: str = "", review_round: int | None = None,
    review_scene_block: str | None = None,
    finish: bool = True, publisher: ProgressPublisher | None = None,
) -> str:
    """Report one delivery failure through the classified-failure flow.

    The single implementation of the flow previously hand-copied in
    `verify_resumed_pr`, `_run_review_round` and `process_issue`:
    classify the exception, transition the label (`ai-blocked` ALONE
    for an explicit `UnrecoverableDeliveryError`, `ai-fix-needed` for
    every recoverable failure), assemble the failure body (fixed
    phrase + `cause` + run scene + evidence), prefix the run marker,
    comment the Issue (the PR too on the recoverable branch — the
    terminal blocked state is Issue-only), then publish the milestone
    and the terminal progress scene as a pure bypass. The carried
    failure is named `action`, `reason`, and `diagnosis`; the milestone
    uses the reason so its mobile notification remains useful. Returns
    the outcome, `"blocked"` or `"fix needed"`.

    `classify=False` forces the terminal branch (the implement-phase
    handler: every failure reaching it is terminal by design — the
    recoverable Pi failures have their own handler); the body is then
    the plain `Orbi failed:` template and the run scene is
    space-joined to the FIRST body line, where `format_status_comment`
    lifts its fields into the structured-alert field block.
    `current_labels` pins the label-patch source (the implement-phase
    handler derives the current delivery label locally); None reads
    the labels from GitHub. `blocked_suffix` extends the classified
    blocked body (the resume verification's preserved-PR note).
    `review_round` pins the terminal scene's round (the implement
    phase has none); None computes `review_rounds_so_far` lazily
    inside the finish publish (a history-read failure there is bypass,
    never a second failure). `finish=False` skips the progress scene
    (the tracked progress comment does not exist and the milestone
    alone carries the notification). `publisher` is the run's LIVE
    progress publisher when the caller holds one: its `finish` then
    PATCHes the tracked comment directly instead of relocating it by
    the run marker. `evidence` appends the bounded
    `_failure_evidence` block after the scene.

    Label and ordinary comment failures retain their existing caller
    semantics. A malformed deduplicated comment URL posts a fresh failure
    comment and emits `failure_comment_id_unavailable`; an update failure
    is logged as `failure_comment_update_failed`. Neither escapes this
    function, so one malformed comment cannot stop the tick.

    The #825 dead-loop guard (classified recoverable failures only):
    the same (run_id, failure fingerprint) recurring to
    `FAILURE_STREAK_LIMIT` consecutive failures escalates to the
    terminal `ai-blocked` outcome with the count and the
    unchanged-precondition verdict in the comment, and an identical
    failure that was already reported bumps the hidden repeat counter
    of its existing comment in place (the streak scan reads that
    counter as the occurrence count) instead of posting a second
    comment; the journal records the repeat. The history read fails
    open — a read failure degrades to the plain classified report,
    never a second failure of the reporting path.
    """
    number = int(issue["number"])
    title = issue["title"]
    priority = issue_priority(issue)
    # Keep `cause` as a compatibility input for callers outside the three
    # production paths; new callers provide the three named parts.
    if reason is None:
        reason = cause or _failure_detail(exc)
    if diagnosis is None:
        diagnosis = cause or _failure_detail(exc)
    blocked = not classify or is_unrecoverable_failure(exc)
    # The #825 dead-loop guard, recoverable failures only (blocked is
    # already terminal; `classify=False` is the implement handler's
    # terminal template): the same (run_id, failure fingerprint)
    # recurring to `FAILURE_STREAK_LIMIT` consecutive failures is a
    # dead loop, not a transient error — the escalation turns it
    # `ai-blocked`, the human decision point. The history read fails
    # open: the guard must never break the failure report itself.
    fingerprint = runner_health.failure_fingerprint(exc)
    reported_failure: dict | None = None
    if classify and not blocked and run_id:
        try:
            history = issue_comments(number, repo=source_repo)
        except Exception:
            LOGGER.exception("issue=%s failure history read failed", number)
            event(
                "failure_history_read_failed", issue=number,
                run_id=run_id,
            )
        else:
            reported_failure = _reported_failure_comment(
                history, run_id, fingerprint,
            )
            streak = _failure_streak(history, run_id, fingerprint)
            if streak + 1 >= FAILURE_STREAK_LIMIT:
                blocked = True
                reason = (
                    f"{reason}; the same failure has now occurred "
                    f"{streak + 1} consecutive times for run_id={run_id} "
                    f"(fingerprint {fingerprint}) with unchanged "
                    "preconditions — a dead loop, not a transient error"
                )
                event(
                    "failure_streak_escalated", level=logging.ERROR,
                    issue=number, run_id=run_id, streak=streak + 1,
                    fingerprint=fingerprint,
                )

    def scene_line() -> str | None:
        """The run scene for the body, or None when omitted.

        The classified reporters degrade a failed snapshot read to the
        '-' placeholder (the debug entry still carries worktree and
        branch); the implement-phase first-line scene is
        omitted entirely on a failed read — the structured-alert field
        block then shows only the failure's own fields (the pinned
        #256 isolation).
        """
        if classify:
            snapshot = (
                _snapshot_or_placeholder(worktree / ".pi-session", number=number)
                if worktree is not None else dict(_SNAPSHOT_PLACEHOLDER)
            )
        else:
            if worktree is None:
                return None
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
                return None
            if snapshot is None:
                snapshot = dict(_SNAPSHOT_PLACEHOLDER)
        return _failure_scene(
            snapshot, run_id=run_id or "-",
            issue=issue_context(source_repo, number), role=role,
            branch=branch or "-", worktree=str(worktree), pr_url=pr_url,
        )

    labels = (
        current_labels if current_labels is not None
        else issue_labels(number, source_repo)
    )
    evidence_detail = _failure_evidence(worktree, exc) if evidence else ""
    if blocked:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=labels,
        )
        scene = scene_line() or "- run: `-`"
        body = _failure_comment_body(
            outcome="blocked", action=action, reason=reason,
            diagnosis=diagnosis, scene=scene, evidence=evidence_detail,
            pr_url=pr_url, issue=issue_context(source_repo, number),
            run_id=run_id or "-",
        )
        if classify:
            body = body.replace(
                "</details>",
                f"\n{_BLOCKED_PRECONDITION_PHRASE.lstrip('; ')}\n</details>",
                1,
            )
        if blocked_suffix:
            body = body.replace(
                "</details>",
                f"\n{blocked_suffix.lstrip('; ')}\n</details>", 1,
            )
        outcome = "blocked"
    else:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=labels,
        )
        scene = scene_line() or "- run: `-`"
        if review_scene_block is not None:
            scene += f"\n{_redact_local_paths(review_scene_block)}"
        body = _failure_comment_body(
            outcome="fix needed", action=action, reason=reason,
            diagnosis=diagnosis, scene=scene, evidence=evidence_detail,
            pr_url=pr_url, issue=issue_context(source_repo, number),
            run_id=run_id or "-",
        )
        outcome = "fix needed"
    if len(body) > FAILURE_COMMENT_MAX_CHARS:
        # The comment body is capped; the cut preserves the
        # cause-first head and names the full session log for the part
        # that was dropped.
        session_file = _latest_session_file(worktree)
        body = (
            body[:FAILURE_COMMENT_MAX_CHARS]
            + f"\n\n_[comment truncated at {FAILURE_COMMENT_MAX_CHARS} "
            f"chars; full session log: "
            f"{'local session log' if session_file else '<unavailable>'}]_"
        )
    if run_id:
        # The hidden fingerprint marker rides under the run marker on
        # the recoverable path only: a blocked Issue is terminal, so
        # its comment never joins a future streak scan (a human's
        # blocked -> ai-fix-needed recovery starts a fresh streak).
        fail_marker_line = "" if blocked else (
            f"{failure_marker(fingerprint)}\n"
        )
        body = f"{run_marker(run_id)}\n{fail_marker_line}{body}"
    post_new_comment = True
    if run_id and reported_failure is not None and not blocked:
        # The #825 dedup: `gh issue view --json comments` returns a GraphQL
        # node id, but the PATCH route requires the REST id. The comment URL
        # already carries that id, so do not make a second API request.
        comment_id = reported_failure.get("id")
        if isinstance(comment_id, int) and not isinstance(comment_id, bool):
            rest_comment_id = comment_id
        else:
            url = reported_failure.get("url")
            match = (
                re.search(r"#issuecomment-(\d+)$", url)
                if isinstance(url, str) else None
            )
            rest_comment_id = int(match.group(1)) if match is not None else None
        if rest_comment_id is None:
            # A malformed or missing URL is data loss in the dedup metadata,
            # not a delivery failure. Preserve the pre-#825 behavior so the
            # current failure is still recorded and the tick continues.
            unavailable_reason = (
                "comment URL has no #issuecomment-<digits> suffix"
            )
            LOGGER.warning(
                "issue=%s failure comment id unavailable: %s",
                number, unavailable_reason,
            )
            event(
                "failure_comment_id_unavailable", level=logging.ERROR,
                issue=number, run_id=run_id, reason=unavailable_reason,
            )
        else:
            # A resolved existing comment owns this occurrence even if its
            # PATCH fails. Preserve the existing update-failure behavior:
            # log the failure without attempting a second ordinary comment.
            post_new_comment = False
            try:
                update_issue_comment(
                    rest_comment_id, repo=source_repo,
                    # Apply the increment to the newly assembled body, not the
                    # stale first report. This preserves scene/retry fields
                    # added by this attempt while retaining the dedup counter.
                    body=bump_failure_repeat(body, fingerprint),
                )
            except Exception:
                LOGGER.exception(
                    "issue=%s failure comment update failed", number,
                )
                event(
                    "failure_comment_update_failed", level=logging.ERROR,
                    issue=number, run_id=run_id,
                )
            else:
                event(
                    "failure_comment_deduplicated", issue=number,
                    run_id=run_id, fingerprint=fingerprint,
                )
    if post_new_comment:
        comment_issue(number, repo=source_repo, body=body)
        if pr_url and not blocked:
            # The recoverable scene is written to the PR too:
            # the next review session and any human watcher see it where
            # the delivery lives. A blocked Issue is terminal — Issue only.
            comment_pr(_pr_number(pr_url), repo=source_repo, body=body)
    if run_id:
        # One publisher for the milestone and the terminal scene: the
        # run's LIVE publisher when the caller holds one (its `finish`
        # PATCHes the tracked comment directly, comment id already
        # known), a fresh one otherwise (`finish` then locates-or-
        # creates the tracked comment by the run marker).
        target = (
            publisher if publisher is not None
            else ProgressPublisher(
                number, source_repo, run_id, run_command=run_command,
            )
        )
        publish = functools.partial(
            _safe_publish, run_id=run_id, issue=number,
            source_repo=source_repo, role=role,
        )
        if outcome == "blocked":
            publish(action=lambda: target.milestone(
                f"blocked: {reason}",
            ))
            if classify:
                finish_failure = reason
                next_step = (
                    "fix the precondition above (see the reason) and "
                    "relabel the Issue ai-fix-needed to resume this "
                    "same PR"
                )
            else:
                finish_failure = reason
                next_step = (
                    "fix the failure above and re-run this Issue (a "
                    "new run id is created automatically)"
                )
            finish_outcome = "blocked"
        else:
            publish(action=lambda: target.milestone(
                f"fix needed: {reason}",
            ))
            finish_failure = reason
            next_step = (
                "the next tick resumes the same run, branch, worktree "
                "and PR automatically (the Issue stays ai-fix-needed)"
            )
            finish_outcome = "fix needed"
        if finish:
            publish(action=lambda: target.finish(_finish_progress_body(
                number=number, title=title, run_id=run_id, role=role,
                branch=branch, worktree=worktree, pr_url=pr_url,
                review_round=(
                    review_round if review_round is not None
                    else review_rounds_so_far(
                        issue_comments(number, repo=source_repo),
                    )
                ),
                priority=priority, detail=finish_failure,
                next_step=next_step, outcome=finish_outcome,
                source_repo=source_repo, action=action,
                reason=reason, diagnosis=diagnosis,
            )))
    return outcome


def _run_review_round(
    pr_url: str, issue: dict, config: config_domain.RunnerConfig, source_repo: str,
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

    def block_label_inconsistency(labels: list[str], reason: str) -> None:
        event(
            "delivery_label_inconsistent", level=logging.ERROR,
            issue=number, pr=pr_url, reason=reason,
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=labels,
        )
        body = (
            f"Orbi failed: PR {pr_url} is open but the delivery labels "
            f"could not be repaired ({reason}); the Issue is ai-blocked"
        )
        if marker:
            body = f"{marker}\n{body}"
        comment_issue(number, repo=source_repo, body=body)

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
        if gate_worktree.is_dir() and _human_review_column2(
            gate_worktree, config,
        ):
            if READY_LABEL not in labels:
                apply_label_patch(
                    number, repo=source_repo,
                    event=EVENT_HUMAN_REVIEW_WAITING,
                    current_labels=labels,
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
            scene = resume_scene(comments)
        except ValueError as scene_exc:
            # Without the trusted scene the runner
            # cannot derive run_id, branch, worktree or PR and
            # cannot start a review session — an external
            # precondition the AI cannot fix by itself (the
            # same terminal state as the scan-time
            # `block_scene_failure`), so the handler below
            # marks the Issue ai-blocked with the explicit
            # reason.
            raise UnrecoverableDeliveryError(
                f"the resume scene is unrecoverable "
                f"({scene_exc}); the runner cannot derive "
                "run_id, branch, worktree or PR without the "
                "trusted 'Orbi opened PR' comment, so "
                "it cannot start a review session; a human "
                "must restore the scene comment or relabel "
                "the Issue"
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
        merged = review_and_merge_if_clean(
            worktree, branch, config.base_branch,
            review_config, source_repo, number,
            title=title, priority=priority, scene=scene,
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
        # The shared classified reporter: recoverable ->
        # `ai-fix-needed` with the full scene on Issue AND PR,
        # unrecoverable -> `ai-blocked` ALONE. No wrapper: a reporting
        # failure here fails the tick fast (the slot is released by
        # `main`'s `finally`), exactly like any other Runner bug.
        report_delivery_failure(
            exc, issue=issue, source_repo=source_repo,
            run_id=run_id, pr_url=pr_url,
            worktree=worktree, branch=branch, role=ROLE_REVIEW,
            action=(
                exc.action
                if isinstance(exc, HumanDecisionRequired)
                else (
                    "Review the prior findings and decide whether to continue "
                    "this PR."
                    if isinstance(exc, ReviewRoundsExhausted) else ""
                )
            ),
            reason=(
                f"The independent review of PR {pr_url} requires a human "
                "decision."
                if isinstance(exc, HumanDecisionRequired)
                else f"the independent review of PR {pr_url} failed: {detail}"
            ),
            diagnosis=detail,
            evidence=True,
            review_scene_block=(
                _round_scene_block(
                    scene, pr_url, int(scene["review_round"]),
                )
                if isinstance(scene, dict)
                and scene.get("verdict_head_unknown_round", 0)
                else None
            ),
        )
        return None
    if merged:
        event(
            "delivery_auto_merged", issue=number, pr=pr_url,
        )
        return True
    return False


def _requeue_closed_external_takeover(
    number: int, source_repo: str, pr_url: str, marker: str, run_id: str,
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
    )
    body = (
        f"{marker}\n"
        f"Orbi: the external PR {pr_url} was closed without "
        f"a merge; the triage Issue #{number} returns to the "
        "ready queue and the next claim delivers the fix "
        f"internally (run_id={run_id})"
    )
    comment_issue(number, repo=source_repo, body=body)
    comment_pr(_pr_number(pr_url), repo=source_repo, body=body)


def _close_external_triage_issue(
    number: int, source_repo: str, pr_url: str, marker: str, run_id: str,
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
            f"Orbi merged the external PR {pr_url}; closing "
            "this triage Issue as delivered by the external "
            f"contribution (run_id={run_id})"
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


def delivery_step(pr_url: str, issue: dict, config: config_domain.RunnerConfig,
                  source_repo: str, *, external_takeover: bool = False) -> None:
    """Run ONE step of an opened-PR delivery: the resume path IS the path.

    The old `wait_for_delivery` held the slot in a sleep loop until the
    PR merged or failed, and the resume logic ran only when that process
    died — the most important path was the least-travelled one (Issue
    #788). The waiting primitive (#381/#763) is now the ONLY mechanism:
    this function performs at most ONE state transition per tick and
    returns; `main` releases the slot in its `finally`, and the next
    tick's `pick_resumable_delivery` classifies the same delivery
    RESUME_REVIEW and runs the next step. A crash loses at most the
    current Pi session, and the slot is held only while a Pi actually
    runs. No sleep exists on this path:

    - PR `MERGED` -> terminal: the delivery is done. An EXTERNAL
      takeover additionally closes the triage Issue with
      the merge evidence — the contributor's PR body carries no
      `Fixes #N` for this Issue, so GitHub never closes it natively
      (合并外部 PR 即关票); the close is bookkeeping of an already
      merged fact and its failure is logged, never a rewrite;
    - PR `CLOSED` without merge -> terminal failure: the Issue is
      marked `ai-blocked` (removing `ai-pr-opened`/`ai-fix-needed`)
      with a failure comment carrying the run marker. An EXTERNAL
      takeover is the exception: the contributor withdrew
      the PR or a maintainer rejected it — that is the 放弃/不可修
      fallback, so the Issue is requeued to `ai-ready` and the next
      claim redoes the fix internally;
    - CI pending on the open PR -> one `delivery_ci_pending` journal
      line, then return: "pending" is a state, not a sleep. The next
      tick re-reads it with one PR call (under the gh read retry and
      rate-limit guards); no label changes, no round consumed;
    - otherwise -> ONE review round (`_run_review_round`): the label
      read/repair, the resumable gate, the human acceptance gate
      (the waiting primitive returns the ticket to
      `ai-ready` while column 2 is non-empty), the independent review
      of the frozen PR — the only blocking phase, a Pi subprocess —
      and the merge gate, whose own intermediate states (pending CI,
      UNKNOWN mergeability) defer the merge to a later tick the same
      way. Findings or a failed gate leave `ai-fix-needed`; the next
      tick resumes the same run, branch, worktree and PR.
    """
    number = int(issue["number"])
    # The progress comment's issue line shows the number
    # AND the title in every scene; the scanned issue dict always
    # carries the GitHub title (every scan fetches `title`) — a
    # missing or non-string title fails fast, never fabricated.
    title = issue["title"]
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    # Pickup priority: derived from the scanned issue's
    # labels (the resumable/in-flight scans fetch `labels`), so the
    # progress comment of a resumed P0 delivery keeps showing `p0`
    # through review/merge.
    priority = issue_priority(issue)
    event(
        "delivery_awaiting", issue=number, pr=pr_url, priority=priority,
    )
    publish = functools.partial(
        _safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
    )

    state, rollup = pr_delivery_rollup(pr_url, source_repo)
    if state == "OPEN":
        event(
            "delivery_ci", issue=number, pr=pr_url,
            checks=",".join(_check_summaries(rollup)) or "none",
        )
    if state == "MERGED":
        event(
            "delivery_merged", issue=number, pr=pr_url,
        )
        if external_takeover:
            # Merging the external PR closes the triage
            # Issue (the PR body has no `Fixes #N` for it).
            _close_external_triage_issue(
                number, source_repo, pr_url, marker, run_id,
            )
        return
    if state == "CLOSED":
        if external_takeover:
            # The external PR was closed without a merge
            # (contributor withdrew, or a maintainer rejected it) —
            # the 放弃/不可修 fallback. The Issue returns to the
            # ready queue and the next claim redoes the fix
            # internally; the closed PR keeps the supersession story
            # in its thread.
            event(
                "external_takeover_closed", issue=number, pr=pr_url,
            )
            _requeue_closed_external_takeover(
                number, source_repo, pr_url, marker, run_id,
            )
            return
        # The current labels are read ONCE before the transition:
        # the terminal patch clears every delivery-state label that
        # is present (`ai-pr-opened`, and `ai-fix-needed` when the
        # PR was closed while awaiting the next review session).
        labels = issue_labels(number, source_repo)
        event(
            "delivery_closed_unmerged", issue=number, pr=pr_url,
        )
        # The blocked patch leaves the terminal state `ai-blocked`
        # alone.
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=labels,
        )
        body = (
            f"Orbi failed: PR {pr_url} was closed without "
            "a merge; the delivery is terminally failed"
        )
        if marker:
            body = f"{marker}\n{body}"
        comment_issue(number, repo=source_repo, body=body)
        if run_id:
            # The blocked-scene progress publishing is
            # bypass — a 404 here must not escape the step (the
            # terminal bookkeeping above already completed and the
            # slot must be released).
            publish(
                action=lambda: ProgressPublisher(
                    number, source_repo, run_id,
                    run_command=run_command,
                ).milestone(
                    f"blocked: PR {pr_url} was closed without a "
                    "merge; the delivery is terminally failed"
                ),
            )
            # The blocked scene carries the actual role and the
            # completed review rounds (review round 2, PR #42):
            # Both opened-PR states are review states
            # (the review session fixes findings in the same
            # session), so the role is always `review`, and the
            # trusted review-round comments bound the round count
            # (GitHub is the only state store).
            blocked_round = review_rounds_so_far(
                issue_comments(number, repo=source_repo),
            )
            # The tracked progress comment becomes the blocked scene:
            # the same terminal body the other failure
            # paths write, with the next-step reason.
            publish(
                action=lambda: _finish_progress(
                    number, run_id, source_repo, None, None,
                    pr_url,
                    f"PR {pr_url} was closed without a merge; the "
                    "delivery is terminally failed",
                    "investigate why the PR was closed and re-open "
                    "the delivery or start a fresh run on the "
                    "Issue",
                    title=title,
                    outcome="blocked",
                    role=ROLE_REVIEW, review_round=blocked_round,
                    priority=priority,
                ),
            )
        return
    # The pre-review CI gate. Pending checks defer the whole
    # delivery to the next tick — the review never starts against a head
    # whose CI has not concluded, so no Pi session is spent on a state
    # that a later read replaces. A failed check still runs the review:
    # the review session is the fixer, and the merge gate
    # re-reads the CI of the verdict's head one-shot.
    pending, _failed = _classify_rollup(rollup)
    if pending:
        event(
            "delivery_ci_pending", issue=number, pr=pr_url,
            pending="; ".join(_render_check(entry) for entry in pending),
        )
        return
    # One OPEN round — the label read/repair, the
    # resumable gate, one independent review of the frozen PR and
    # the whole failure classification — lives in
    # `_run_review_round`. True (merged this round), False (findings or
    # a deferred merge, the next tick resumes) and None (a terminal
    # state was already handled: ai-blocked, or the recoverable
    # ai-fix-needed scene the next tick resumes) all end the step here;
    # the next tick's resume scan runs the next step. An EXTERNAL
    # takeover never produces True (Issue #842, decision D2): its clean
    # verdict ends at the triage stop inside
    # `review_and_merge_if_clean` — `ai-blocked`, the maintainer
    # decides the merge.
    _run_review_round(pr_url, issue, config, source_repo)


def _preflight(config: config_domain.RunnerConfig) -> None:
    """Run the Runner tick's pre-slot startup checks (fail fast).

    Every pre-claim check lives here as one reusable unit, so the tick
    entry (`main`) stays a thin `argparse -> preflight -> slot ->
    dispatch -> finally` spine and the same checks can be exercised
    independently (doctor and other callers) without re-inlining them.
    Order is significant: the editable CLI refresh and the
    source-freshness gate precede the unit-drift and transport checks,
    and all of them precede any slot or claim.
    """
    # Resolve every effective per-repository title before taking a slot. A
    # repository policy may override the host value, so validating only the
    # host value (or only the first source repository) can still leave a fresh
    # claim silently scoped to a title that does not exist.
    for repo in config.source_repos:
        milestone, _ = claim._repo_scan_keys(config, repo, config.active_milestone)
        milestone_bookkeeping.validate_active_milestone(repo, milestone)
    # Publish the configured active milestone for the CI
    # triage workflow. This is a bypass; delivery must continue when the
    # variable API is unavailable.
    milestone_bookkeeping.sync_active_milestone_variable(
        config.source_repos[0], config.active_milestone,
        run_command=run_command,
    )
    # Editable CLI install refresh: BEFORE any slot or
    # claim the tool env's editable metadata must match the checkout's
    # packaging inputs — a merged packaging change (entry point,
    # version or dependency in `pyproject.toml`) would otherwise make
    # the NEXT CLI process die before the Runner can start (the #158
    # incident shape; since the src layout, a new package
    # module needs no reinstall). Unchanged: no uv call (no
    # per-tick reinstall); changed or first install: ONE lock-
    # protected editable force reinstall (the SAME base-sync flock the
    # service template's ExecStartPre uses — two instances starting in
    # the same tick serialize, the second reuses the first's result).
    # A failing install fails the start with the structured
    # `cli_install_failed` line (reason + fix command): no slot, no
    # claim, no label change. Runs ONLY in the Runner tick entry (the
    # bare CLI) — the subcommands never install. The implementation
    # lives in THIS module (see the NOTE at the top): a separate new
    # module would not be importable in the stale-finder tool env, and
    # the refresh that repairs the finder could never run.
    # The CLI self-update acts on the deployment home, never
    # on the delivery checkout (repo_dir may be a foreign repo X without
    # any orbi packaging input).
    refresh_cli_install(
        config.deploy_home, run_command=run_command,
    )
    # Startup source freshness: BEFORE any slot or claim,
    # prove that the code THIS process executes is the fetched
    # origin/main head (the import source's checkout HEAD for
    # an editable install, the installed version vs the latest release
    # tag for a non-editable one — all local git reads). The 09-07
    # incident: the editable install resolved into an old issue
    # worktree, so the preflight synced a checkout the process never
    # executed and the stale engine failed deliveries invisibly. A stale
    # or unverifiable source logs the structured `runner_source_stale`
    # line (facts + fix) and fails the start: no slot, no claim, no
    # label change. `allow_stale_runner: true` downgrades the same line
    # to a warning (explicit offline escape hatch, never silent).
    check_runner_source_freshness(config, run_command=run_command)
    # Deployment consistency: BEFORE any slot or
    # claim the installed scheduler units must match the repo templates
    # (the templates the ExecStartPre-synced checkout just loaded).
    # Drift is self-healed with the SAME idempotent install (copy the
    # templates, reload the scheduler, enable the schedules — never
    # start/stop/restart the service: a currently RUNNING task is never
    # interrupted) and re-verified with the SAME comparison. Drift
    # that survives the sync — or a failing install step — logs a
    # structured `unit_drift` line per unit and fails fast: this
    # start takes no slot, claims no Issue and changes no label.
    try:
        check_unit_drift(
            config.deploy_home, unit_name=config.unit_name,
            max_concurrency=config.max_concurrency,
        )
    except UnitDriftError:
        sync_drifted_units(
            config.deploy_home,
            unit_name=config.unit_name,
            max_concurrency=config.max_concurrency,
            run_command=run_command,
        )
    # Task-worktree reclamation: closed-Issue worktrees past
    # the retention window are bounded garbage — reclaim them at the tick
    # start beside the drift check, NOT through a manual command nobody
    # remembers to run. Idempotent, bounded and never fatal: a reclaim
    # failure is a `worktree_reclaim_failed` line, never a failed start.
    try:
        reclaim_released_worktrees(config)
    except Exception:
        LOGGER.exception("worktree_reclaim_failed")
    # Git transport preflight: BEFORE any slot or
    # claim the deployment checkout's git transport must be the
    # CONFIGURED one (orbi.toml `git_transport`, default "ssh") and
    # reachable (the task worktrees share the checkout's single
    # `origin` remote, so the worktree's fetch/push — including
    # `.github/workflows/*.yml` — uses it). A broken transport fails
    # the start with the structured reason: no slot, no claim, no
    # label change, no fallback. The `gh` token (GitHub API) is
    # untouched.
    try:
        transport = check_transport(
            config.repo_dir, config.source_repos,
            run_command=run_command, migrate=False,
            mode=config.git_transport,
        )
    except TransportError as exc:
        event(
            "transport_check_failed", level=logging.ERROR,
            repo_dir=config.repo_dir, source_repos=config.source_repos,
            reason=exc,
        )
        raise
    event(
        "transport", result="clean",
        remote=transport.get("remote", "-"),
        protocol=transport.get("protocol", "-"),
        url=transport.get("url", "-"),
        ssh_reachable=transport.get("ssh_reachable", "-"),
        transport_reachable=transport.get("transport_reachable", "-"),
    )
    # Self-health check: BEFORE any slot or claim the Runner
    # actively looks for the incident patterns of 2026-09-04 — a service
    # crash loop (>= 3 crashes in 60 min, the #262 scene), repeated
    # same-fingerprint run failures on one Issue (>= 3, the #246 scene) and
    # a stale pickup while the ai-ready queue is non-empty. It is a pure
    # bypass: a check failure logs `health_check_failed` and
    # never fails the delivery, takes no slot and changes no label.
    try:
        runner_health.run_health_check(config, run_command=run_command)
    except Exception:
        LOGGER.exception("health_check_failed")


def log_ready_outside_milestone(
    repos: Sequence[str], active_milestone: str | None,
    config: config_domain.RunnerConfig | None = None,
) -> bool:
    """Report ready Issues excluded by the configured milestone scope.

    This is idle-path diagnostics only. A failed query must preserve the
    existing ``no_ready_issue`` outcome so observability cannot change the
    delivery decision.
    """
    if active_milestone is None:
        return False
    outside_by_repo: list[tuple[str, str, int]] = []
    for repo in repos:
        milestone, dispatch_label = claim._repo_scan_keys(
            config, repo, active_milestone,
        )
        try:
            issues = list_issues(
                repo, state="open", label=dispatch_label,
                json_fields="number,milestone", limit=1000, timeout=30,
            )
            outside = [
                issue for issue in issues
                if not isinstance(issue.get("milestone"), dict)
                or issue["milestone"].get("title") != milestone
            ]
        except Exception as exc:
            event(
                "ready_outside_milestone_check_failed", level=logging.ERROR,
                repo=repo, active_milestone=milestone, error=exc,
            )
            return False
        if outside:
            outside_by_repo.append((repo, milestone, len(outside)))
    for repo, milestone, count in outside_by_repo:
        event(
            "ready_outside_milestone", repo=repo,
            active_milestone=milestone, count=count,
        )
    return bool(outside_by_repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("ORBI_CONFIG", "orbi.toml")),
    )
    args = parser.parse_args(argv)
    configure_logging()
    # Stop scene: install the SIGTERM handler BEFORE any
    # other step so every phase of the tick (pre-claim, claim,
    # implement, delivery wait) stops with the active Issue context
    # logged and the live Pi child shut down — never an orphan Pi and
    # never only systemd's generic "Stopped" line. Python only allows
    # signal handlers in the main thread (the CLI entry point always
    # is; in-thread `main()` test calls skip the install).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_stop)

    try:
        config = config_domain.load_config(args.config)
        validate_config(config)
        validate_execution_source_repos(config.source_repos)
    except config_domain.ConfigFileMissingError as exc:
        LOGGER.error(
            "config_not_found path=%s; reason=no Orbi config at this path "
            "(the default is `orbi.toml` in the current directory); "
            "fix=run from the deployment directory, or point at the config: "
            "`ORBI_CONFIG=~/orbi-deploy/<dir>/orbi.toml orbi <command>` "
            "(`--config <path>` also works, after the subcommand). "
            "The source checkout is not a deployment directory and has no "
            "orbi.toml.",
            exc.path,
        )
        return 1
    except ValueError as exc:
        event("config_invalid", level=logging.ERROR, reason=exc)
        return 1
    _preflight(config)
    # Concurrency cap: take one slot BEFORE claiming anything.
    # The slot is held for the whole delivery lifecycle (implement ->
    # review -> fix -> merge) and released only after the delivery is
    # merged or terminally failed — or when this process exits for any
    # reason, which the kernel handles (flock on an open descriptor).
    slot = acquire_slot(
        config.slot_dir, config.max_concurrency, os.getpid(),
    )
    if slot is None:
        event(
            "capacity_full", max_concurrency=config.max_concurrency,
            slot_dir=config.slot_dir,
        )
        return 0
    try:
        selected = claim.pick_next_delivery(
            config.source_repos, config.slot_dir,
            config.max_concurrency,
            config.active_milestone,
            config=config,
            hooks=claim.ResumeHooks(
                resume_scene=resume_scene,
                route_external_pr_ticket=_route_external_pr_ticket,
                block_scene_failure=block_scene_failure,
                recover_missing_pr_scene=_recover_missing_pr_scene,
                has_recoverable_pr_scene=_has_recoverable_pr_scene,
                comment_pr=comment_pr,
            ),
        )
        if selected is None:
            ready_outside_milestone = log_ready_outside_milestone(
                config.source_repos, config.active_milestone, config=config,
            )
            if not ready_outside_milestone:
                LOGGER.info(
                    "source_repos=%s outcome=no_ready_issue",
                    config.source_repos,
                )
            # Milestone bookkeeping as a pure bypass (Issues #856/#186):
            # ONE entry point arms an existing release ticket, classifies
            # the active Milestone's waiting state and advances/opens the
            # decision notice. A thrown `gh` call or a renamed Milestone
            # must not turn an idle tick into a non-zero exit.
            idle_repo = config.source_repos[0]
            effective_milestone, dispatch_label, repo_policy = claim._repo_scan_context(
                config, idle_repo, config.active_milestone,
            )
            if effective_milestone is not None:
                # The base branch is the repo's FUSED value (entry
                # fallback, then the policy override): the command writes
                # it into the release ticket, and the release state
                # machine freezes the DECLARED branch — the raw host value
                # would release the wrong branch for any repository whose
                # entry or policy overrides it.
                try:
                    milestone_bookkeeping.reconcile_milestone_on_idle(
                        idle_repo,
                        effective_milestone,
                        config.config_path,
                        config.repo_dir,
                        auto_next_milestone=config.auto_next_milestone,
                        release_confirmation=(
                            repo_policy.release_confirmation
                            if repo_policy is not None
                            and repo_policy.release_confirmation is not None
                            else False
                        ),
                        parse_version_title=_parse_version_title,
                        policy=repo_policy,
                        policy_path=config_domain.repository_config_path(
                            config, idle_repo,
                        ),
                        base_branch=resolve_source_base_branch(
                            config, idle_repo, repo_policy,
                        ).base_branch,
                        dispatch_label=dispatch_label,
                        version_file=config.version_file,
                    )
                except Exception:
                    LOGGER.exception(
                        "active_milestone_advance_failed repo=%s milestone=%s",
                        idle_repo,
                        effective_milestone,
                    )
            return 0
        source_repo, issue, scene = selected
        # Name THIS delivery in the held slot file — the
        # earliest point after selection, before any verification work.
        # The other runners' resume scans then skip exactly this
        # (repo, issue) while it is in flight (implement, review, or the
        # implement→opened-PR boundary) instead of abandoning their whole
        # scan; a write failure propagates (fail fast, the delivery has
        # not started).
        mark_slot_delivery(slot, source_repo, int(issue["number"]))
        # Resolve the repository-level policy ONCE for the whole
        # delivery. The effective base branch/milestone must drive the
        # resume verification, the claim and the review/merge loop, and a
        # malformed repository file blocks the claim fast with the offending
        # keys (one repository's bad file never affects another pool).
        try:
            repo_policy = load_repo_policy(config, source_repo)
        except RepoConfigError as exc:
            run_id = (
                scene["run_id"] if scene is not None
                else (current_run_id() or new_run_id())
            )
            event(
                "repo_config_invalid", level=logging.ERROR,
                source_repo=source_repo, reason=exc,
            )
            block_repo_config_failure(
                int(issue["number"]), source_repo, exc, run_id,
                current_labels={
                    label.get("name") for label in issue.get("labels", [])
                    if isinstance(label, dict)
                    and isinstance(label.get("name"), str)
                },
            )
            return 0
        # Fuse the entry fallback UNCONDITIONALLY: with no policy file
        # the dev path used to read the host base_branch while the
        # release path re-derived the entry value — two paths, two base
        # branches for the same repo.
        config = resolve_source_base_branch(
            config, source_repo, repo_policy,
        )
        if scene is not None:
            # An open PR is a recoverable review state: resume the
            # same run on the same branch, worktree and PR.
            # Bind the scene's run id first so every journal line and
            # GitHub comment of the resumed delivery carries it
            # Both opened-PR states go straight to the
            # delivery step: `ai-pr-opened` awaits review, and
            # `ai-fix-needed` awaits the next review session —
            # The review session itself fixes findings in the
            # same session, so there is no cold-start fixer to run
            # here (a stranded `ai-pr-opened` delivery or a dead
            # runner is reviewed the same way).
            set_run_id(scene["run_id"])
            # The resumed delivery is in flight: bind the stop scene
            # with the same derived branch/worktree the
            # delivery wait uses (never read from a comment).
            set_active_run(RunContext(
                run_id=scene["run_id"], issue=int(issue["number"]),
                branch=task_branch(
                    source_repo, int(issue["number"]), scene["run_id"],
                ),
                worktree=worktree_path(
                    config.repo_dir, source_repo,
                    int(issue["number"]), scene["run_id"],
                ),
                source_repo=source_repo,
            ), issue["title"])
            # Verify the open PR BEFORE any git/Pi mutation
            # (head repo, base, run marker, exact URL of the recovered
            # scene — the pre-#82 resume_delivery check, restored):
            # the step receives the VERIFIED URL, never the comment
            # string, so a comment can never steer the runner into the
            # wrong PR. A mismatch is terminal: the Issue
            # is marked ai-blocked and the tick stops.
            try:
                pr_url = verify_resumed_pr(
                    scene, issue, config, source_repo,
                )
            except UnrecoverableDeliveryError as exc:
                # Resume verification has already performed the audited
                # label/comment transition. This is an expected external
                # scene condition, not a failed Runner tick.
                event(
                    "resume_pr_handled", level=logging.ERROR,
                    issue=issue["number"], scene_pr=scene["pr_url"],
                    reason=exc,
                )
                return 0
            except ReportedMissingFixesError as exc:
                # verify_resumed_pr has already classified and reported the
                # ticket failure. A malformed PR is data belonging to this
                # delivery, not a Runner failure: release the slot and let
                # the next tick handle another Issue. Catch only the typed,
                # successfully reported outcome; unrelated Runner bugs and
                # reporting failures must still propagate.
                event(
                    "resume_pr_verification_failed", level=logging.ERROR,
                    issue=issue["number"], scene_pr=scene["pr_url"],
                    reason=exc,
                )
                return 0
        else:
            result = process_issue(
                issue, config, source_repo, repo_policy,
            )
            # `process_issue` owns task dispatch and reports its outcome;
            # do not repeat task-type predicates here.
            if result.kind not in ("pr", "external-pr"):
                return 0
            # The delivery's PR is open and its scene comment
            # is written — the tick ends here and releases the slot. The
            # review is the next tick's RESUME_REVIEW step (the waiting
            # primitive generalized): the resume path is the ONLY review
            # path, a crash loses at most the current Pi session, and the
            # slot is never held waiting for CI or mergeability.
            return 0
        # An external takeover closes the triage Issue
        # itself after the merge — the contributor's PR carries no
        # `Fixes #N` for it. The resumed delivery derives the scene from
        # its trusted comment, which carries the external marker for an
        # external takeover.
        external_takeover = bool(scene.get("external"))
        delivery_step(
            pr_url, issue, config, source_repo,
            external_takeover=external_takeover,
        )
    finally:
        slot.release()
        # The delivery is over (merged, terminally failed, or the tick
        # found no work): a stop from here on is idle again.
        clear_active_run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
