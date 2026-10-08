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
from orbi import claim, clarify
from orbi import config as config_domain
from orbi.branch_reclaim import delete_merged_delivery_branch, reclaim_merged_delivery_branches
from orbi.engine_source import EngineSourceError
from orbi.git_identity import set_bot_git_identity
from orbi.git_transport import TransportError, check_transport
from orbi.pilot_slots import (
    acquire_claim_lock, acquire_slot, mark_slot_delivery, slot_dir_for,
    slot_held_deliveries, slot_occupancy,
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
    LiveProgressThrottle,
    ProgressPublisher,
    STARTED_HEADER,
    _progress_body,
    _progress_state,
    _run_info_fields,
    _safe_publish,
    field_block,
    format_elapsed,
    progress_body,
    quote_value,
    read_test_result,
    recoverable_scene_body,
    run_marker,
    validate_run_id,
)
from orbi import runner_health
from orbi.scheduler import (
    UnitDriftError,
    check_unit_drift,
    sync_drifted_units,
)
from orbi.scheduler_units import MAX_RUNNER_INSTANCES


from orbi import pi_process
from orbi.pi_process import (
    PI_IDLE_RECOVERY_CYCLES,
    PI_IDLE_WARN_SECONDS,
    PI_MODEL_WAIT_DEAD_SECONDS,
    PI_MODEL_WAIT_PROBE_SECONDS,
    ROLE_IMPLEMENT,
    ModelWaitDeadError,
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
from orbi import review_merge
from orbi import run_state

# The shared primitives live in the leaf modules now — the
# journal kernel (logger, run binding, subprocess seam), the GitHub
# data-access layer, the git operations layer, and the CLI-install domain.
# `runner` consumes them like any other caller; only `cli` and
# `pilot_setup` import `runner` itself.
from orbi import github, gitops, journal, progress, release, scene
# The failure record lives in its own lean module (Issue #1229); these
# helpers are re-exported so `runner.<name>` keeps working for callers.
from orbi.failure import _failure_detail
# The delivery failure report is its own module (Issue #1260): the runner
# reaches it as `failure_report.<name>` and imports back only the exception
# types it raises or catches, plus the classifier the delivery flow uses.
from orbi import failure_report
from orbi.failure_report import (
    GateCIFailure,
    HumanDecisionRequired,
    ReviewRoundsExhausted,
    UnrecoverableDeliveryError,
    _classify_failure,
)
from orbi.merge_handoff import (
    MergeHandoffRequired,
    handle_merge_handoff,
    is_maintainer_actionable,
)
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
    line_run_markers,
    list_issues,
    list_milestones,
    milestone_issues,
    milestone_open_issue_count,
    milestone_open_issues,
    open_blocker_numbers,
    open_pr_for_branch,
    parse_issue_array,
    parse_issue_list,
    parse_paginated_issue_array,
    pr_delivery_status,
    pr_delivery_rollup,
    _check_summaries,
    pr_view,
)
from orbi.checks import _classify_rollup, _render_check
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
    headline = f"{STARTED_HEADER} " + " ".join(
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
        state = run_state.read_run_state(worktree)
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
        found = run_state.parse_pr_comment(body)
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
        state = run_state.read_run_state(resume[1])
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


def _parse_version_title(title: object) -> tuple[int, int, int] | None:
    """Parse a strict ``v<major>.<minor>.<patch>`` milestone title."""
    if not isinstance(title, str):
        return None
    match = re.fullmatch(
        r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", title,
    )
    return tuple(map(int, match.groups())) if match else None






















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
                state = run_state.read_run_state(path)
            except ValueError as exc:
                raise RuntimeError(
                    f"worktree {path} has a corrupt run state file "
                    f"({exc}): the same run cannot be verified "
                    "(Issue #219)"
                ) from exc
            if state is None:
                raise RuntimeError(
                    f"worktree {path} has no run state file "
                    f"({run_state.run_state_path(path)}): the same run cannot "
                    "be verified (Issue #219)"
                )
            if str(state["repo"]).rsplit("/", 1)[-1] != name:
                continue
            candidates.append(path)
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(run_state.read_run_state(newest)["run_id"]), newest


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
               attribution_footer: bool = True,
               attribution_link: str = "https://github.com/orbi-build/orbi",
               ) -> str | None:
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
    freshness = run_state.assess_base_freshness(
        worktree, base_branch, head="HEAD",
    )
    if freshness is not run_state.BaseFreshness.FRESH:
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
    run_state.record_pushed_head(worktree, local_head)
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
                f"{issue} · {attribution_link}\n"
            )
        # A transient GitHub hiccup must not throw away a finished
        # delivery: the create goes through the bounded write retry, and
        # a response lost after GitHub actually created the PR is caught
        # by the idempotency hook instead of producing a duplicate.
        run_gh_write_command(
            [
                "gh", "pr", "create", "--base", base_branch,
                "--head", branch, "--title", issue_title, "--body", body,
            ],
            cwd=worktree,
            command_runner=run_command,
            already_applied=(
                lambda: open_pr_for_branch(worktree, branch) is not None
            ),
        )
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
            failure_report.report_delivery_failure(
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
            failure_report._report_resume_failure(
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
            event("claim_yield", issue=number, reason="in_progress_label")
            return IssueResult("claim-yielded", None)
        if not stable_branch_present and stable_branch_exists(
                config.repo_dir, stable_branch,
        ):
            event("claim_yield", issue=number, reason="stable_branch_appeared")
            return IssueResult("claim-yielded", None)
        if config.clarify_thin_tickets and not clarify.enforce(
                issue, config, source_repo, run_id):
            return IssueResult("needs-detail", None)
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
        run_state.write_run_state(ctx)
        if external_takeover:
            # The takeover's engine push line starts on the
            # contributor's own head (Issue #608): the merge record's
            # external_commits (Issue #833) must subtract only what the
            # engine pushes on top of this foreign head, never the head
            # itself. No session has run yet, so HEAD is exactly that
            # head; the record is set once per run.
            run_state.record_pushed_base(worktree, run_command(
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
        # One `Orbi started Pi:` comment per run; a resume PATCHes it (#1369).
        publish(
            action=lambda: publisher.started(started_pi_comment_body(
                ctx, run_info, extra_fields=repo_config_fields,
            )),
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
                attribution_link=config.attribution_link,
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
                    body=review_merge.human_review_checklist(
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
        # The recoverable path classifies its cause exactly like the
        # terminal one (Issue #1465), but the classification never
        # decides whether the run blocks: the recovery stays
        # `ai-in-progress` (#1455/#227).
        record = _classify_failure(exc, outcome="blocked")
        detail = _failure_detail(exc)
        body = recoverable_scene_body(
            run_id=run_id, recoverable_name=recoverable_name, record=record,
            detail=detail, run_info=run_info,
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
                f"- reason_code: `{record.reason_code}`\n"
                f"- action: `{record.action_code}`\n"
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
            failure_report.report_delivery_failure(
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
    publisher.ensure(progress._finish_progress_body(
        number=number, title=title, run_id=run_id, role=role,
        branch=branch, worktree=worktree, pr_url=pr_url,
        review_round=review_round, priority=priority, detail=detail,
        next_step=next_step, outcome=outcome, source_repo=source_repo,
    ))


# Unreferenced (dead) phrase, deliberately outside the Issue #1260 move.
_FIX_NEEDED_PHRASE = (
    "; the Issue stays ai-fix-needed and the next tick resumes the "
    "same run, branch, worktree and PR"
)




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
    failure_report.comment_pr(_pr_number(pr_url), repo=source_repo, body=body)


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
            blocked_round = failure_report.review_rounds_so_far(
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
    review_merge._run_review_round(pr_url, issue, config, source_repo)


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
    # Remote delivery-branch reclamation: a merged PR's `orbi/*` branch
    # is unreachable evidence and otherwise stays on the remote forever.
    # Bounded, idempotent and never fatal: a read failure deletes
    # nothing and a single delete failure is a warning, never a failed
    # start.
    try:
        reclaim_merged_delivery_branches(config)
    except Exception:
        LOGGER.exception("branch_reclaim_failed")
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
    set_bot_git_identity(os.environ)
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
        # No slot means no claim and no delivery: a runner that kept going
        # would work WITHOUT a slot (exceeding `max_concurrency`) and then
        # crash in the `finally` below on `None.release()`.
        return 0
    try:
        claim_lock = acquire_claim_lock(config.slot_dir)
        try:
            selected = claim.pick_next_delivery(
                config.source_repos, config.slot_dir,
                config.max_concurrency,
                config.active_milestone,
                config=config,
                hooks=claim.ResumeHooks(
                    resume_scene=run_state.resume_scene,
                    route_external_pr_ticket=_route_external_pr_ticket,
                    block_scene_failure=failure_report.block_scene_failure,
                    recover_missing_pr_scene=_recover_missing_pr_scene,
                    has_recoverable_pr_scene=_has_recoverable_pr_scene,
                    comment_pr=failure_report.comment_pr,
                ),
            )
            if selected is None:
                # Nothing was selected: the claim window is over, so release
                # it BEFORE the milestone bookkeeping below. That
                # bookkeeping takes the shared base-sync lock and makes
                # GitHub calls, and the hold stays what it is for — the
                # pick -> identity-write window — never a piece of work a
                # co-runner's claim would have to wait for.
                claim_lock.release()
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
                    # The base branch AND `auto_next_milestone` are the repo's
                    # FUSED values (entry fallback, then the policy override):
                    # the command writes the base branch into the release
                    # ticket, and the release state machine freezes the
                    # DECLARED branch — the raw host value would release the
                    # wrong branch for any repository whose entry or policy
                    # overrides it. `auto_next_milestone` follows the same
                    # per-key precedence (Issue #1342): a repository policy
                    # value wins over the host fallback, so a managed tenant
                    # can stop the engine overwriting `active_milestone`.
                    fused_idle = resolve_source_base_branch(
                        config, idle_repo, repo_policy,
                    )
                    try:
                        idle_outcome = (
                            milestone_bookkeeping.reconcile_milestone_on_idle(
                                idle_repo,
                                effective_milestone,
                                config.config_path,
                                config.repo_dir,
                                auto_next_milestone=(
                                    fused_idle.auto_next_milestone
                                ),
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
                                base_branch=fused_idle.base_branch,
                                dispatch_label=dispatch_label,
                                version_file=config.version_file,
                            )
                        )
                    except Exception:
                        LOGGER.exception(
                            "active_milestone_advance_failed repo=%s milestone=%s",
                            idle_repo,
                            effective_milestone,
                        )
                    else:
                        if milestone_bookkeeping.is_closed_unscoped_outcome(
                            idle_outcome,
                        ):
                            # Issue #1391: the active Milestone is closed and
                            # no newer open one exists, so this tick has no
                            # scope. Re-run the FRESH ready scan WITHOUT the
                            # Milestone filter — the repository policy's value
                            # included, since it wins over the host one —
                            # keeping the repo's own `dispatch_label` resolved
                            # above. The resume scans already ran in the first
                            # claim; the folded `selected` falls through to
                            # the ordinary delivery path below, and the
                            # configured value is never written or cleared.
                            # The lock is released by the same `finally` as the
                            # first scan, so the scan -> identity-write window
                            # stays serialized against a co-runner.
                            claim_lock = acquire_claim_lock(config.slot_dir)
                            issue = claim.pick_issue(
                                idle_repo, None,
                                dispatch_label=dispatch_label,
                                held=slot_held_deliveries(
                                    config.slot_dir, config.max_concurrency,
                                ),
                            )
                            if issue is not None:
                                selected = (idle_repo, issue, None)
                if selected is None:
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
        finally:
            # Blocking acquisition above: the lock was taken, never None
            # (the non-blocking probe is the only None path). Release is
            # idempotent, so the idle path above may release first.
            claim_lock.release()
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
            failure_report.block_repo_config_failure(
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
