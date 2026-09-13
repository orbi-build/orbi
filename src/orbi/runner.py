#!/usr/bin/env python3
"""One-shot bootstrap runner for Orbi.

This is intentionally small. It claims one ready GitHub Issue, gives it to
Pi in an isolated worktree, and accepts success only when one open PR exists.
After the implementer opens the PR, the Runner closes the loop itself: it
freezes the exact PR base/head SHA, runs one independent review session that
reviews the diff AND fixes Blocker/Major findings in the same session
(modify code, run tests, push the task branch — Issue #82: no cold-start
fixer, no third review), re-freezes the head after a clean verdict,
re-checks the merge gate against the latest remote base, and merges via
`gh pr merge --match-head-commit`. Pi never pushes the protected branch;
the Runner is the only merge actor. Any command failure is logged and
raised. There is no fallback, queue, daemon, or multi-agent framework.

Throughout the whole lifecycle the Runner publishes live progress
automatically (Issue #18): one per-run GitHub progress comment carrying a
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
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

# NOTE (Issue #158, root-caused by Issue #168): the editable finder maps
# the WHOLE package directory `src/orbi/`, so a newly added package
# module is importable WITHOUT any reinstall — the #158 incident class
# is fixed at the root. `refresh_cli_install` lives in `orbi.cli_source`
# since Issue #785; `runner` imports it like any caller and the
# preflight stubs keep patching the module global below.
from orbi import engine_source
from orbi.engine_source import EngineSourceError
from orbi.git_transport import TransportError, check_transport
from orbi.pilot_slots import acquire_slot, slot_dir_for, slot_occupancy
from orbi.pi_activity import (
    activity_snapshot,
    format_duration,
    format_end_scene,
    format_run_scene,
    sanitize,
)
from orbi.delivery_labels import (
    BLOCKED_LABEL,
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
    EXTERNAL_PR_RE,
    DeliveryContext,
    DeliveryFacts,
    DeliveryScene,
    body_markers,
    classify,
)
from orbi.repo_config import (
    REPO_CONFIG_PATH,
    RepoConfigError,
    RepoPolicy,
    read_repo_config,
    read_repo_config_at,
    repo_config_audit,
    resolve_policy,
    validate_context_file,
)
from orbi.progress import (
    RUN_MARKER_PATTERN,
    ProgressPublisher,
    _progress_body,
    _progress_state,
    _run_info_fields,
    _safe_publish,
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
from orbi.systemd_deploy import (
    TIMER_INSTANCES,
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
    PI_POLL_INTERVAL,
    ROLE_IMPLEMENT,
    ModelWaitDeadError,
    RateLimitExhaustedError,
    RecoverablePiFailure,
    RecoverablePiProcessError,
    RecoverablePiTimeoutError,
    _log_provider_config_loaded,
    stream_pi,
)

# Issue #785: the shared primitives live in the leaf modules now — the
# journal kernel (logger, run binding, subprocess seam), the GitHub
# data-access layer, the git operations layer, and the CLI-install domain.
# `runner` consumes them like any other caller; only `cli` and
# `pilot_setup` import `runner` itself.
from orbi import github, gitops, journal, release, scene
from orbi.cli_source import CliInstallError, refresh_cli_install
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    run_gh_read_command,
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
    issue_labels,
    issue_priority,
    issue_view,
    latest_run_marker,
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
    pr_comments,
    pr_delivery_status,
    pr_state,
    pr_view,
    trusted_issue_comments_block,
)
from orbi.gitops import (
    acquire_base_sync_lock,
    base_sync_lock_path,
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
    RELEASE_CI_POLL_INTERVAL,
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

# Automatic observability (Issue #18): the GitHub progress comment is
# PATCHed on every activity change and at most every 30 seconds while a
# Pi session runs, so a mobile user sees live progress without any
# command. The journal cadence is the poll interval above.
PI_HEARTBEAT_SECONDS = 30.0
# Stop-handler grace (Issue #48): when the Runner is stopped with SIGTERM
# while a Pi delivery is in flight, the handler must not wait forever for
# the Pi child to exit. systemd gives `TimeoutStopSec` (default 90s) before
# it SIGKILLs the whole cgroup, so an unbounded `child.wait()` on a child
# stuck in a model/network call leaves `Result=timeout` after the grace.
# The handler TERMs the child, waits at most this long, then KILLs it so
# it always reaps the child and exits with 128+SIGTERM before systemd's
# own deadline — a clean signal stop, never `failed`/`timeout`.
STOP_CHILD_GRACE_SECONDS = 15.0

# Non-implement Pi session roles (Issue #41/#82). `ROLE_IMPLEMENT` is the
# default role of a delivery Pi session and lives in `orbi.pi_process`.
ROLE_REVIEW = "review"
ROLE_TICKET = "ticket"


# Mergeability is recomputed asynchronously after a push. Poll the PR after
# its checks settle instead of treating the transient UNKNOWN value as a
# conflict.
MERGEABLE_WAIT_SECONDS = 120.0
MERGEABLE_POLL_INTERVAL = 5.0


# Issue #745: {{ISSUE_COMMENTS}} injects the Issue's trusted-comment
# timeline into the implementer/review prompt. This cap bounds how many
# trusted comments a long-discussed Issue may contribute to the task
# context; the NEWEST are kept (the latest decision lives there) and a
# dropped-older-comments count is stated inside the injected block —
# the truncation is never silent.
ISSUE_COMMENTS_LIMIT = 20

# Task-worktree reclamation (Issue #760): the tick-start pass removes at
# most this many worktrees per tick (oldest-closed first), so a large
# backlog drains over ticks and one tick never spends unbounded time on
# `rm -rf`.
WORKTREE_RECLAIM_MAX_PER_TICK = 25
# The default retention window (hours): a closed Issue's scene stays
# inspectable for three days before the reclamation removes it — the
# Issue's conservative option (宁可不删，不可误删).
WORKTREE_RETAIN_HOURS = 72
# A closed Issue still wearing one of these was closed by a human while
# a run was (or may still be) working in its scene — never reclaim such
# a worktree.
_WORKTREE_INFLIGHT_LABELS = frozenset({
    IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL,
})
# The task worktree name `worktree_path` derives: orbi-{slug}-issue-{N}-{run_id}.
_WORKTREE_NAME_PATTERN = re.compile(
    r"^orbi-(?P<slug>.+)-issue-(?P<number>\d+)-(?P<run_id>[0-9a-f]{8})$",
)


class RecoverableMergeGateError(RuntimeError):
    """A merge-gate failure the next review session can repair.

    This type deliberately identifies only merge-gate outcomes that require
    absorbing the latest base or resolving merge conflicts. Callers must not
    infer delivery control flow from the human-readable error message.
    """


class UnrecoverableDeliveryError(RuntimeError):
    """A delivery failure that is an EXTERNAL precondition the AI cannot
    safely judge or fix (Issue #50).

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
    """A resume precondition was handled and reported for this tick.

    The verification handler has already written the label and failure
    evidence. Keeping a distinct type lets the tick boundary end normally
    without swallowing unrelated Runner bugs.
    """


class PreExistingCIFailure(UnrecoverableDeliveryError):
    """A failed delivery check is already failing on the PR's base.

    Retrying review/fix rounds cannot change a failure that the PR did not
    introduce, so this external precondition fails the delivery quickly.
    """


class ReviewRoundsExhausted(UnrecoverableDeliveryError):
    """Expected terminal stop after the bounded review/fix budget.

    This remains an exception internally so the existing delivery cleanup
    path performs the terminal label/comment transition, but it is not a
    Runner failure and must not be logged with a traceback.
    """


def is_unrecoverable_failure(exc: BaseException) -> bool:
    """Issue #50: classify one delivery failure.

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


# Issue #266: the health check's journal lines carry the same `[run_id]`
# prefix as every other Runner line (the RunIdFilter is attached per
# logger; the health module must not import this one — circular).


def _stop_delivery(signum: int) -> None:
    """Log the stop scene, shut down the live Pi child, exit (Issue #48).

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

    Issue #48 root cause: the stop handler previously called
    ``child.wait()`` with no timeout. A Pi child stuck in a model/network
    call that does not exit on SIGTERM left the handler blocked; systemd
    then SIGKILLed the whole unit after ``TimeoutStopSec`` and recorded
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
    """Exit the process from the ORIGINAL signal (Issue #48).

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
    """SIGTERM handler: log the active Issue context, then exit (Issue #48)."""
    _stop_delivery(signum)


def _config_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def _prompt_config_path(value: str, base: Path, legacy_name: str) -> Path:
    """Resolve a prompt path, retaining explicit legacy basename configs."""
    path = _config_path(value, base)
    if Path(value).as_posix() == legacy_name and not path.exists():
        migrated = _config_path(f"prompts/{legacy_name}", base)
        if migrated.exists():
            return migrated
    return path


def _load_deploy_env_file(deploy_home: Path) -> None:
    """Merge `<deploy_home>/.orbi/env` into the process environment (Issue #348).

    The documented env-file-first flow (getting-started step 4) writes the
    provider key to this gitignored file, and the installed unit loads it
    via `EnvironmentFile` at service start — the CLI process does not, so
    `orbi setup` and friends must read it themselves or the documented
    step 4 -> 5 flow fails verbatim. Plain systemd EnvironmentFile syntax:
    `KEY=VALUE` lines, optional `export ` prefix, matching single/double
    quotes stripped, blank lines and `#` comments skipped. A variable
    already exported in the shell wins (`setdefault`): the shell export is
    the documented override for manual ticks. A missing file is a no-op
    (a keyless local server needs no env file at all); a line without `=`
    is a misconfiguration and fails fast.
    """
    env_file = deploy_home / ".orbi" / "env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        if "=" not in stripped:
            raise ValueError(
                f"malformed line in env file {env_file}: {stripped!r} "
                "(expected KEY=VALUE)"
            )
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('"', "'")
        ):
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class RunnerConfig:
    """The typed host configuration (Issue #790).

    :func:`load_config` is the ONLY constructor from raw TOML; derived
    instances (the per-run values and the repository-policy overlay) are
    produced with :func:`dataclasses.replace`, never by re-assembly.
    Every module consumes it through attribute access. Fields the host
    config does not carry carry the same defaults the former bare dict's
    ``config.get(key, default)`` fallbacks used, so a hand-built (test)
    config keeps the exact pre-#790 behavior.
    """

    # Per-run delivery values, bound by the tick/process_issue with
    # `replace` right before the implementer/review session runs; ""
    # is the unbound placeholder (never read before the binding).
    run_id: str = ""
    base_sha: str = ""
    # Repository-policy overlay (Issue #527): written only by
    # `repo_config.resolve_policy` — `test_command` and `dispatch_label`
    # are repository-declared keys with no host equivalent.
    test_command: str | None = None
    repo_context_files: tuple[str, ...] = ()
    dispatch_label: str | None = None
    # Host config (load_config output). The Path fields and the two
    # string identity fields below carry placeholder defaults ("." /
    # "main" / "ssh") ONLY so a hand-built partial config stays
    # constructible: load_config — the sole real constructor — sets every
    # one of them explicitly, and a path that reads a field a hand-built
    # config never set failed with a KeyError before #790.
    config_path: Path = Path(".")
    source_repos: tuple[str, ...] = ()
    repo_dir: Path = Path(".")
    deploy_home: Path = Path(".")
    unit_name: str | None = None
    health_alert_repo: str | None = None
    workspace_root: Path = Path(".")
    prompt: Path = Path(".")
    prompt_review: Path = Path(".")
    skills: tuple[Path, ...] = ()
    context_files: tuple[Path, ...] = ()
    base_branch: str = "main"
    git_transport: str = "ssh"
    engine_source_track: str | None = None
    active_milestone: str | None = None
    auto_next_milestone: bool = True
    max_concurrency: int = 1
    allow_stale_runner: bool = False
    human_review_gate: bool = False
    slot_dir: Path | None = None
    pi_provider: str | None = None
    pi_model: str | None = None
    pi_thinking: str | None = None
    pi_extensions: tuple[dict, ...] = ()
    model_wait_dead_seconds: float = PI_MODEL_WAIT_DEAD_SECONDS
    issue_comments_limit: int = ISSUE_COMMENTS_LIMIT
    worktree_retain_hours: float = WORKTREE_RETAIN_HOURS
    model_wait_probe_url: str | None = None
    model_wait_probe_seconds: float = PI_MODEL_WAIT_PROBE_SECONDS
    release_ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS
    mergeable_wait_seconds: float = MERGEABLE_WAIT_SECONDS
    release_deliveries_wait_seconds: float = RELEASE_DELIVERIES_WAIT_SECONDS
    pi_providers: Path | None = None
    pi_providers_data: dict | None = None
    pi_provider_key_finding: dict | None = None
    # Multi-repo registry (Issue #134): the explicit per-repo entries
    # (name, path, github, base_branch). Empty -> the single-repo config.
    repositories: tuple[dict, ...] = ()


def load_config(path: Path, *, check_provider_api_keys: bool = True,
                allow_missing_pi_providers: bool = False) -> RunnerConfig:
    """Load the human-maintained TOML config and resolve its paths.

    ``doctor`` disables the selected provider-key gate so it can report the
    configuration finding instead of being stopped by it.
    """
    base = path.resolve().parent
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    source_repos = data.get("source_repos")
    if not isinstance(source_repos, list) or not source_repos:
        raise ValueError("source_repos must be a non-empty list")
    if not all(isinstance(repo, str) and repo for repo in source_repos):
        raise ValueError("source_repos must contain non-empty strings")
    unit_name = data.get("unit_name")
    if unit_name is not None and (
        not isinstance(unit_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", unit_name)
    ):
        raise ValueError("unit_name must contain only letters, numbers, '-' or '_'")
    base_branch = data.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("base_branch must be a non-empty string")
    # Delivery transport (Issue #580): how the checkout's git data
    # operations (fetch/push) authenticate. Absent -> "ssh" (the exact
    # pre-#580 contract); "https" keeps the origin on the HTTPS URL and
    # authenticates via the gh credential helper (the token-only
    # sandbox path). Anything else is a misconfiguration: fail fast.
    git_transport = data.get("git_transport", "ssh")
    if git_transport not in ("ssh", "https"):
        raise ValueError("git_transport must be 'ssh' or 'https'")
    # Claim scope (Issue #139): the active Milestone is an EXPLICIT
    # version scope for the fresh-claim scans — it is never guessed
    # from the repo's Milestone list. Absent (None) keeps the current
    # behavior exactly (compat); present it must be a non-empty
    # string, otherwise the config is a misconfiguration and the start
    # fails fast.
    active_milestone = data.get("active_milestone")
    if active_milestone is not None and (
        not isinstance(active_milestone, str) or not active_milestone
    ):
        raise ValueError("active_milestone must be a non-empty string")
    auto_next_milestone = data.get("auto_next_milestone", True)
    if not isinstance(auto_next_milestone, bool):
        raise ValueError("auto_next_milestone must be a boolean")
    # Startup source freshness (Issue #525): the Runner refuses to claim
    # when the code it executes is not the origin/main head.
    # This flag is the EXPLICIT degraded mode for offline/restricted-
    # network deployments — it only downgrades the gate to a warning,
    # never skips it. Default False = fail fast.
    allow_stale_runner = data.get("allow_stale_runner", False)
    if not isinstance(allow_stale_runner, bool):
        raise ValueError("allow_stale_runner must be a boolean")
    # Human acceptance gate (Issue #763): when true, every delivery's
    # PR carries an acceptance checklist on the Issue and the review
    # round waits for the human-only `ai-human-review` label while the
    # checklist's column 2 (the machine-unverifiable minimum) is
    # non-empty. HOST-only like `allow_stale_runner`: the gate is the
    # deployment operator's trust decision (who pays for the pipeline
    # decides when a person must look), never a repository-writable
    # policy. Default False = the exact pre-#763 behavior.
    human_review_gate = data.get("human_review_gate", False)
    if not isinstance(human_review_gate, bool):
        raise ValueError("human_review_gate must be a boolean")
    # Engine source update channel (Issue #535): what the deploy home
    # checkout follows at the next start — origin/main by default (the
    # exact pre-#535 dogfood behavior), a branch, the newest official
    # release tag, one exact tag or one exact commit. Host/deploy-only:
    # a repository's .github/orbi.toml can never carry it. An invalid
    # value fails the config load fast.
    engine_source_track = engine_source.normalize_engine_source_track(
        data.get("engine_source_track"),
    )
    # Concurrency cap (Issue #39): the local machine can only serve a
    # limited number of concurrent tasks, so the default is 1. Any other
    # value must be a positive integer; fail fast on anything else.
    max_concurrency = data.get("max_concurrency", 1)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= len(TIMER_INSTANCES)
    ):
        raise ValueError(
            "max_concurrency must be a positive integer with a matching "
            f"Runner timer instance (1..{len(TIMER_INSTANCES)})"
        )
    # Optional Pi model selection (Issue #119): each key is absent -> None
    # (the Pi flag is not passed, Pi keeps its own default) or a non-empty
    # string passed to Pi verbatim. Anything else fails fast.
    pi_provider = _optional_pi_string(data, "pi_provider")
    pi_model = _optional_pi_string(data, "pi_model")
    pi_thinking = _optional_pi_string(data, "pi_thinking")
    pi_extensions = _load_pi_extensions(data.get("pi_extensions"), base)
    # Hung-model-request threshold (Issue #228): the model_wait dead
    # silence is configurable; omitted -> PI_MODEL_WAIT_DEAD_SECONDS
    # (default 1800 s, 30 minutes). It measures silence between
    # complete session events, never token-level model progress.
    model_wait_dead_seconds = _model_wait_dead_seconds(data)
    # Trusted-comment injection cap (Issue #745): how many of the
    # Issue's trusted comments enter the agent's task context.
    issue_comments_limit = _issue_comments_limit(data)
    # Task-worktree reclamation (Issue #760): how long a closed Issue's
    # worktree stays inspectable before the tick start removes it.
    worktree_retain_hours = _worktree_retain_hours(data)
    # Swallowed-model-request probe (Issue #233): the /slots endpoint
    # (optional) and its sustained-idle grace (default 60 s). Absent URL
    # -> the probe is disabled (the exact pre-#233 behavior: the run is
    # bounded by model_wait_dead_seconds only).
    model_wait_probe_url = _model_wait_probe_url(data)
    model_wait_probe_seconds = _model_wait_probe_seconds(data)
    # Release CI wait (Issue #268): how long the release gate waits for
    # pending checks on the release commit before failing with its own
    # timeout reason.
    release_ci_wait_seconds = _release_ci_wait_seconds(data)
    mergeable_wait_seconds = _mergeable_wait_seconds(data)
    release_deliveries_wait_seconds = _release_deliveries_wait_seconds(data)
    # Runner-self health alert routing (Issue #345): the orbi repo that
    # receives the watchdog's crash_loop / stale_pickup Issues. Absent ->
    # None (the Runner derives the orbi repo from the deploy home's git
    # origin); present -> must be a non-empty `owner/repo` string, used
    # verbatim for fork/private deployments.
    health_alert_repo = _optional_pi_string(data, "health_alert_repo")
    # Optional Pi provider file (Issue #157): the provider metadata
    # (baseUrl / api / apiKey / models) lives in a separate JSON file in
    # Pi's own `models.json` shape; `orbi.toml` only selects the
    # provider/model/thinking used at runtime. Absent key -> None (Pi
    # keeps using its own agent dir, the exact pre-#157 behavior).
    repo_dir = _config_path(data.get("repo_dir", "."), base)
    # Deployment home (Issue #330): the orbi source checkout — the editable
    # CLI install source, the systemd/ unit templates, labels.toml and the
    # prompt defaults. Absent -> repo_dir (the orbi-bootstrap deployment,
    # home == delivery checkout, keeps its exact behavior). Present -> must
    # be a non-empty string, resolved like every other config path; the
    # delivery checkout (repo_dir) is then decoupled from the CLI
    # self-update and the startup gates act on the home only.
    deploy_home_raw = data.get("deploy_home")
    if deploy_home_raw is not None and (
        not isinstance(deploy_home_raw, str) or not deploy_home_raw
    ):
        raise ValueError("deploy_home must be a non-empty string")
    deploy_home = (
        _config_path(deploy_home_raw, base)
        if deploy_home_raw is not None
        else repo_dir
    )
    # The deploy-home env file (Issue #348): step 4 of getting-started
    # writes the provider key to `<deploy_home>/.orbi/env` and the
    # installed unit loads it via `EnvironmentFile` at service start —
    # the CLI process does not. Load it here so `orbi setup` (and every
    # other CLI entry) validates the key exactly like the unit would.
    _load_deploy_env_file(deploy_home)
    # Optional Pi provider file (Issue #157): the provider metadata
    # (baseUrl / api / apiKey / models) lives in a separate JSON file in
    # Pi's own `models.json` shape; `orbi.toml` only selects the
    # provider/model/thinking used at runtime. Absent key -> None (Pi
    # keeps using its own agent dir, the exact pre-#157 behavior).
    pi_providers = _optional_pi_string(data, "pi_providers")
    pi_providers_path = (
        _config_path(pi_providers, base) if pi_providers is not None
        else None
    )
    _load_pi_providers.last_key_finding = None
    pi_providers_data = None
    if pi_providers_path is not None:
        try:
            pi_providers_data = _load_pi_providers(
                pi_providers_path, pi_provider, pi_model,
                deploy_home / ".orbi" / "env",
                check_api_key=check_provider_api_keys,
            )
        except FileNotFoundError:
            # A missing path is the setup/doctor diagnostic case.  Preserve
            # fail-fast behavior for an existing non-file path (for example,
            # a directory), which is an invalid provider configuration.
            if not allow_missing_pi_providers or pi_providers_path.exists():
                raise
            _load_pi_providers.last_key_finding = {
                "provider": pi_provider or "-",
                "variable": "-",
                "path": pi_providers_path,
                "env_file": deploy_home / ".orbi" / "env",
                "state": "file missing",
            }
    return RunnerConfig(
        config_path=path.resolve(),
        source_repos=tuple(source_repos),
        repo_dir=repo_dir,
        deploy_home=deploy_home,
        unit_name=unit_name,
        health_alert_repo=health_alert_repo,
        workspace_root=_config_path(data.get("workspace_root", ".."), base),
        # Issue #330: when deploy_home is EXPLICIT the prompt defaults
        # live in the deployment home (the delivery checkout may be a
        # foreign repo without them); an explicit prompt path still
        # resolves against the config file dir. deploy_home absent ->
        # the original config-file-dir resolution (bootstrap unchanged).
        prompt=_prompt_config_path(
            data.get("prompt", "prompts/prompt.md"),
            base if "prompt" in data
            else (deploy_home if deploy_home_raw is not None else base),
            "prompt.md",
        ),
        prompt_review=_prompt_config_path(
            data.get("prompt_review", "prompts/prompt_review.md"),
            base if "prompt_review" in data
            else (deploy_home if deploy_home_raw is not None else base),
            "prompt_review.md",
        ),
        skills=tuple(_config_path(item, base) for item in data.get("skills", [])),
        context_files=tuple(
            _config_path(item, base) for item in data.get("context_files", [])
        ),
        base_branch=base_branch,
        git_transport=git_transport,
        engine_source_track=engine_source_track,
        active_milestone=active_milestone,
        auto_next_milestone=auto_next_milestone,
        max_concurrency=max_concurrency,
        allow_stale_runner=allow_stale_runner,
        human_review_gate=human_review_gate,
        slot_dir=slot_dir_for(repo_dir),
        pi_provider=pi_provider,
        pi_model=pi_model,
        pi_thinking=pi_thinking,
        pi_extensions=tuple(pi_extensions),
        model_wait_dead_seconds=model_wait_dead_seconds,
        issue_comments_limit=issue_comments_limit,
        worktree_retain_hours=worktree_retain_hours,
        model_wait_probe_url=model_wait_probe_url,
        model_wait_probe_seconds=model_wait_probe_seconds,
        release_ci_wait_seconds=release_ci_wait_seconds,
        mergeable_wait_seconds=mergeable_wait_seconds,
        release_deliveries_wait_seconds=release_deliveries_wait_seconds,
        pi_providers=pi_providers_path,
        pi_providers_data=pi_providers_data,
        pi_provider_key_finding=getattr(
            _load_pi_providers, "last_key_finding", None,
        ),
        # Multi-repo registry (Issue #134): the explicit per-repo entries
        # (name, path, github, base_branch). Absent section -> () so the
        # single-repo config keeps its exact shape and flow.
        repositories=tuple(
            parse_repositories(data.get("repositories", []), base)
        ),
    )


def _optional_pi_string(data: dict, key: str) -> str | None:
    """Read one optional Pi model key (Issue #119).

    Absent -> None (the corresponding `pi --provider/--model/--thinking`
    flag is not passed and Pi keeps its own default). Present -> must be a
    non-empty string, passed to Pi verbatim; anything else fails fast.
    """
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _load_pi_extensions(value: object, base: Path) -> list[dict]:
    """Validate the extensions owned by an Orbi Pi run.

    Package sources must be reproducible: npm sources end in a concrete
    semver and git sources carry a non-empty ref after ``#``.  Other sources
    are repository-relative local files/directories.  Values are normalized
    once at config load so implement and review cannot diverge.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("pi_extensions must be an array of tables")
    result: list[dict] = []
    seen: set[str] = set()
    env_values: dict[str, str] = {}
    semver = re.compile(r"@[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"pi_extensions[{index}] must be a table")
        source = item.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError(f"pi_extensions[{index}].source must be a non-empty string")
        if source.startswith("npm:"):
            if not semver.search(source):
                raise ValueError(f"pi_extensions[{index}] npm source must pin a version")
            normalized = source
        elif source.startswith("git:") or source.startswith(
            ("http://", "https://", "ssh://", "git://")
        ):
            # Pi's documented git source syntax uses an @ separator for
            # the pinned ref (for example git:github.com/org/repo@v1).
            # Require that same syntax here so validation does not accept a
            # source that Pi cannot resolve as a git package.
            if "@" not in source or not source.rsplit("@", 1)[1]:
                raise ValueError(
                    f"pi_extensions[{index}] git source must pin a ref with @"
                )
            normalized = source
        else:
            local = _config_path(source, base)
            if not local.exists():
                raise ValueError(f"pi_extensions[{index}] local source does not exist: {local}")
            normalized = str(local)
        if normalized in seen:
            raise ValueError(f"pi_extensions contains duplicate source: {source}")
        seen.add(normalized)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"pi_extensions[{index}].enabled must be a boolean")
        env = item.get("env", {})
        if not isinstance(env, dict):
            raise ValueError(f"pi_extensions[{index}].env must be a table")
        clean_env: dict[str, str] = {}
        for name, env_value in env.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"pi_extensions[{index}].env has invalid variable name {name!r}")
            if not isinstance(env_value, str):
                raise ValueError(f"pi_extensions[{index}].env.{name} must be a string")
            previous = env_values.get(name)
            if previous is not None and previous != env_value:
                raise ValueError(f"pi_extensions env conflict for {name}")
            env_values[name] = env_value
            clean_env[name] = env_value
        result.append({"source": normalized, "enabled": enabled, "env": clean_env})
    return result


def _pi_extension_args(config: RunnerConfig) -> list[str]:
    """Return the isolated extension flags for every Pi role."""
    args = ["--no-extensions"]
    for extension in config.pi_extensions:
        if extension["enabled"]:
            args.extend(("--extension", extension["source"]))
    return args


def _pi_extension_env(config: RunnerConfig) -> dict[str, str]:
    """Return extension variables for the Pi child only; never log them."""
    values: dict[str, str] = {}
    for extension in config.pi_extensions:
        if extension["enabled"]:
            values.update(extension["env"])
    return values


def _model_wait_probe_url(data: dict) -> str | None:
    """Load and validate the optional `model_wait_probe_url`
    (Issue #233).

    Omitted -> None (the /slots probe is disabled: the run is bounded by
    `model_wait_dead_seconds` only, the exact pre-#233 behavior). Present
    -> must be a non-empty `http://` or `https://` URL (the model's
    `/slots` endpoint, e.g. `http://127.0.0.1:18082/slots`); anything else
    fails fast at config load with the field name and the concrete reason.
    """
    value = data.get("model_wait_probe_url")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(
            "model_wait_probe_url must be a non-empty string "
            f"(got {type(value).__name__} {value!r})"
        )
    if not value.startswith(("http://", "https://")):
        raise ValueError(
            "model_wait_probe_url must be an http:// or https:// URL "
            f"(got {value!r})"
        )
    return value


def _model_wait_probe_seconds(data: dict) -> float:
    """Load and validate the optional `model_wait_probe_seconds`
    (Issue #233).

    Omitted -> `PI_MODEL_WAIT_PROBE_SECONDS` (default 60 s). Present ->
    must be a finite positive number (int or float); booleans, zero,
    negative, NaN/infinity and non-numeric values fail fast at config
    load with the field name and the concrete reason.
    """
    value = data.get(
        "model_wait_probe_seconds", PI_MODEL_WAIT_PROBE_SECONDS,
    )
    if isinstance(value, bool):
        raise ValueError(
            "model_wait_probe_seconds must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "model_wait_probe_seconds must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            "model_wait_probe_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if math.isinf(number):
        raise ValueError(
            "model_wait_probe_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "model_wait_probe_seconds must be a positive number of seconds "
            f"(got {value!r})"
        )
    return number


def _release_ci_wait_seconds(data: dict) -> float:
    """Load and validate the optional `release_ci_wait_seconds`
    (Issue #268).

    Omitted -> `RELEASE_CI_WAIT_SECONDS` (default 1800 s). Present ->
    must be a finite positive number (int or float); booleans, zero,
    negative, NaN/infinity and non-numeric values fail fast at config
    load with the field name and the concrete reason.
    """
    value = data.get("release_ci_wait_seconds", RELEASE_CI_WAIT_SECONDS)
    if isinstance(value, bool):
        raise ValueError(
            "release_ci_wait_seconds must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "release_ci_wait_seconds must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            "release_ci_wait_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if math.isinf(number):
        raise ValueError(
            "release_ci_wait_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "release_ci_wait_seconds must be a positive number of seconds "
            f"(got {value!r})"
        )
    return number


def _mergeable_wait_seconds(data: dict) -> float:
    """Load and validate the merge gate's mergeable wait limit."""
    value = data.get("mergeable_wait_seconds", MERGEABLE_WAIT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "mergeable_wait_seconds must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(
            "mergeable_wait_seconds must be a finite positive number of "
            f"seconds (got {value!r})"
        )
    return number


def _release_deliveries_wait_seconds(data: dict) -> float:
    """Load the Issue #381 release delivery wait limit."""
    value = data.get(
        "release_deliveries_wait_seconds", RELEASE_DELIVERIES_WAIT_SECONDS,
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "release_deliveries_wait_seconds must be a positive number "
            f"(got {value!r})"
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(
            "release_deliveries_wait_seconds must be a positive finite "
            f"number (got {value!r})"
        )
    return number


def _model_wait_dead_seconds(data: dict) -> float:
    """Load and validate the optional `model_wait_dead_seconds`
    (Issue #228).

    Omitted -> `PI_MODEL_WAIT_DEAD_SECONDS` (default 1800 s, 30
    minutes). Present -> must be a finite positive number (int or
    float); booleans, zero, negative, NaN/infinity and non-numeric
    values fail fast at config load with the field name and the
    concrete reason.
    """
    value = data.get("model_wait_dead_seconds", PI_MODEL_WAIT_DEAD_SECONDS)
    if isinstance(value, bool):
        raise ValueError(
            "model_wait_dead_seconds must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "model_wait_dead_seconds must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            "model_wait_dead_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if math.isinf(number):
        raise ValueError(
            "model_wait_dead_seconds must be a finite number of seconds "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "model_wait_dead_seconds must be a positive number of seconds "
            f"(got {value!r})"
        )
    return number


def _worktree_retain_hours(data: dict) -> float:
    """Load and validate the optional `worktree_retain_hours` (Issue
    #760).

    Omitted -> `WORKTREE_RETAIN_HOURS` (default 72 hours): a closed
    Issue's scene stays inspectable for three days before the tick-start
    reclamation removes it. Present -> must be a finite positive number
    (int or float); booleans, zero, negative, NaN/infinity and
    non-numeric values fail fast at config load with the field name and
    the concrete reason.
    """
    value = data.get("worktree_retain_hours", WORKTREE_RETAIN_HOURS)
    if isinstance(value, bool):
        raise ValueError(
            "worktree_retain_hours must be a number, not a boolean "
            f"(got {value!r})"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "worktree_retain_hours must be a number "
            f"(got {type(value).__name__} {value!r})"
        )
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            "worktree_retain_hours must be a finite number of hours "
            f"(got {value!r})"
        )
    if math.isinf(number):
        raise ValueError(
            "worktree_retain_hours must be a finite number of hours "
            f"(got {value!r})"
        )
    if number <= 0:
        raise ValueError(
            "worktree_retain_hours must be a positive number of hours "
            f"(got {value!r})"
        )
    return number


def _issue_comments_limit(data: dict) -> int:
    """Load and validate the optional `issue_comments_limit` (Issue
    #745).

    Omitted -> `ISSUE_COMMENTS_LIMIT` (default 20). Present -> must be
    a positive integer; booleans, fractional and non-numeric values
    fail fast at config load with the field name and the concrete
    reason.
    """
    value = data.get("issue_comments_limit", ISSUE_COMMENTS_LIMIT)
    if isinstance(value, bool):
        raise ValueError(
            "issue_comments_limit must be a positive integer, not a "
            f"boolean (got {value!r})"
        )
    if not isinstance(value, int):
        raise ValueError(
            "issue_comments_limit must be a positive integer "
            f"(got {type(value).__name__} {value!r})"
        )
    if value <= 0:
        raise ValueError(
            "issue_comments_limit must be a positive integer "
            f"(got {value!r})"
        )
    return value


def _load_pi_providers(path: Path, pi_provider: str | None,
                       pi_model: str | None, env_file: Path, *,
                       check_api_key: bool = True) -> dict:
    """Load and validate the Pi provider file (Issue #157).

    The file uses Pi's own `models.json` shape (`{"providers": {id:
    {baseUrl, api, apiKey, models: [...]}}}`) — verified against the
    installed Pi 0.84.3 docs (`docs/models.md`). Fail fast with a
    specific message: file missing, invalid JSON, missing `providers`
    object, a provider entry with `models` but no `baseUrl` or no
    `api` (provider- or model-level — Pi's own schema requirement),
    the selected provider/model not defined in the file, or an
    `apiKey` env-var reference (`$VAR` / `${VAR}`) whose variable is
    missing or empty. The key value itself is never logged; only the
    variable name is named in the error.
    """
    _load_pi_providers.last_key_finding = None
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"pi_providers file {path} is not valid JSON: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError(
            f"pi_providers file {path} must have a 'providers' object"
        )
    for provider_id, entry in providers.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"pi_providers file {path}: provider {provider_id!r} "
                "must be an object"
            )
        models = entry.get("models")
        if models is not None:
            if not isinstance(models, list) or not models:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "models must be a non-empty list"
                )
            if not isinstance(entry.get("baseUrl"), str) or not entry["baseUrl"]:
                raise ValueError(
                    f"pi_providers file {path}: provider {provider_id!r} "
                    "is missing baseUrl"
                )
            if not isinstance(entry.get("api"), str) or not entry["api"]:
                if not all(
                    isinstance(model, dict)
                    and isinstance(model.get("api"), str)
                    and model["api"]
                    for model in models
                ):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} is missing api"
                    )
            for model in models:
                if not isinstance(model, dict) or not model.get("id"):
                    raise ValueError(
                        f"pi_providers file {path}: provider "
                        f"{provider_id!r} has a model without an id"
                    )
    if pi_provider is not None:
        if pi_provider not in providers:
            raise ValueError(
                f"pi_provider {pi_provider!r} is not defined in "
                f"pi_providers file {path}"
            )
        entry = providers[pi_provider]
        # Only the SELECTED provider's key must resolve: an unselected
        # provider with a missing key just stays unavailable in Pi
        # (verified against real Pi 0.84.3), it never breaks the run.
        finding = _pi_provider_api_key_finding(
            path, pi_provider, entry, env_file,
        )
        _load_pi_providers.last_key_finding = finding
        if finding and check_api_key:
            raise ValueError(finding["error"])
        if pi_model is not None:
            model_ids = [
                model["id"] for model in entry.get("models", [])
            ] if isinstance(entry.get("models"), list) else []
            if pi_model not in model_ids:
                raise ValueError(
                    f"pi_model {pi_model!r} is not defined for provider "
                    f"{pi_provider!r} in pi_providers file {path}"
                )
    return data


def _pi_provider_api_key_finding(path: Path, provider_id: str,
                                  entry: dict, env_file: Path) -> dict | None:
    """Return an unresolved selected-provider key finding, without key data."""
    api_key = entry.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        return None
    for match in re.finditer(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        api_key,
    ):
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            state = "is not set"
        elif os.environ[name] == "":
            state = "is set but empty"
        else:
            continue
        return {
            "provider": provider_id, "variable": name, "path": path,
            "env_file": env_file,
            "state": state,
            "error": (
                f"API key for provider {provider_id!r} references "
                f"environment variable {name} {state} "
                f"(pi_providers file {path}). Export {name} in your "
                f"shell or add it to {env_file} (the unit's EnvironmentFile)."
            ),
        }
    return None


def _expand_pi_api_key_refs(api_key: str) -> str:
    """Resolve `$VAR` / `${VAR}` references in an `apiKey` (Issue #303).

    Same reference syntax `_pi_provider_api_key_finding` validates (Pi's
    `docs/models.md`): every reference whose environment variable is
    set and non-empty is replaced by the real value; a reference whose
    variable is missing or empty — only possible for a non-selected
    provider, the selected one already failed config load otherwise —
    stays verbatim (that provider stays unavailable in Pi, the exact
    pre-#303 behavior). The value itself is never logged.
    """
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: os.environ.get(match.group(1) or match.group(2))
        or match.group(0),
        api_key,
    )


def prepare_pi_agent_dir(worktree: Path, config: RunnerConfig) -> Path | None:
    """Materialize the per-run Pi agent dir (Issue #157).

    Returns None when no provider file is configured — the Pi command
    and environment keep their exact pre-#157 shape (Pi uses its own
    agent dir). Otherwise creates `<worktree>/.orbi/pi-agent/`
    (gitignored, per-run) and returns it:

    - `models.json`: the user agent dir's providers merged with the
      configured file's providers (the file wins on id collision) —
      the user's existing providers keep working, the file adds or
      overrides; the merged catalog is what Pi loads via
      `PI_CODING_AGENT_DIR` (verified against real Pi 0.84.3);
    - `settings.json` / `auth.json`: SYMLINKS to the user agent dir's
      files when they exist, so Pi's other behavior (settings, stored
      auth) is unchanged apart from the provider catalog.

    The per-run `settings.json` is a REAL file (Issue #172), consistent
    with the per-run catalog:

    - base: the user agent dir's settings when it exists (the user's
      other settings are preserved), `{}` otherwise — the user's global
      `~/.pi/agent/settings.json` is never modified;
    - `pi_provider`/`pi_model` configured: `defaultProvider` /
      `defaultModel` point at the selected provider/model and
      `enabledModels` is exactly that model, so the initial model
      selection (CLI flags, then scoped models, then settings defaults)
      can only land on a model of the merged catalog;
    - not configured: `enabledModels` keeps only the patterns that
      resolve in the merged catalog (Pi's exact reference match:
      canonical `provider/modelId` or unambiguous bare model id,
      case-insensitive); a pattern that resolves to nothing would make
      Pi warn `No models match pattern` at startup and could steer the
      initial model to a provider the run cannot use — an empty result
      drops the key entirely (Pi falls back to the full catalog);
    - a user `httpIdleTimeoutMs` of `0` (Pi's documented "disabled")
      is dropped: with it, a first response that never arrives hangs
      forever; without the key Pi applies its built-in default (300s)
      and the request fails with a concrete timeout error instead.

    `auth.json` stays a SYMLINK to the user agent dir's file when it
    exists: stored auth for providers present in the merged catalog is
    still valid.

    `apiKey` env-var references (`$VAR` / `${VAR}`) are resolved into
    the per-run copy (Issue #303): config load already required the
    SELECTED provider's references to resolve, so the materialized
    catalog carries a usable real credential — without it Pi would
    hold the literal `$VAR` string and the request could never
    authenticate. References whose variable is missing or empty (only
    possible for non-selected providers) stay verbatim. The user's
    provider file and user agent dir are never modified, and the
    resolved key never reaches the journal, a comment, or a commit:
    the per-run dir is the gitignored `<worktree>/.orbi/pi-agent/`.
    """
    providers_data = config.pi_providers_data
    if providers_data is None:
        return None
    agent_dir = worktree / ".orbi" / "pi-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    user_agent = Path(os.path.expanduser("~")) / ".pi" / "agent"
    merged_providers: dict = {}
    user_models = user_agent / "models.json"
    if user_models.is_file():
        try:
            user_data = json.loads(user_models.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"user agent dir models.json {user_models} is not valid "
                f"JSON: {exc}"
            ) from None
        user_providers = user_data.get("providers") if isinstance(
            user_data, dict
        ) else None
        if not isinstance(user_providers, dict):
            user_providers = {}
        merged_providers.update(user_providers)
    merged_providers.update(providers_data["providers"])
    # Issue #303: the per-run copy carries the resolved `apiKey` values
    # (entries with a string key are copied, so the loaded config data
    # keeps its literal references).
    resolved_providers: dict = {}
    for provider_id, entry in merged_providers.items():
        api_key = entry.get("apiKey") if isinstance(entry, dict) else None
        if isinstance(api_key, str) and api_key:
            entry = {**entry, "apiKey": _expand_pi_api_key_refs(api_key)}
        resolved_providers[provider_id] = entry
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": resolved_providers}, indent=2),
        encoding="utf-8",
    )
    # Per-run settings.json (Issue #172): a real file consistent with
    # the merged catalog above, never a symlink to the user's global
    # settings (whose defaults/enabledModels may reference models this
    # run's catalog cannot resolve). Idempotent for a resumed run in
    # the same worktree: a stale file or symlink from an earlier
    # attempt is replaced, never kept.
    user_settings = user_agent / "settings.json"
    base_settings: dict = {}
    if user_settings.is_file():
        try:
            loaded = json.loads(user_settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"user agent dir settings.json {user_settings} is not "
                f"valid JSON: {exc}"
            ) from None
        if not isinstance(loaded, dict):
            raise ValueError(
                f"user agent dir settings.json {user_settings} must be "
                f"a JSON object"
            )
        base_settings = loaded
    settings = dict(base_settings)
    pi_provider = config.pi_provider
    pi_model = config.pi_model
    if pi_provider is not None and pi_model is not None:
        settings["defaultProvider"] = pi_provider
        settings["defaultModel"] = pi_model
        settings["enabledModels"] = [f"{pi_provider}/{pi_model}"]
    else:
        patterns = settings.get("enabledModels")
        if isinstance(patterns, list):
            resolved = _resolve_enabled_models(patterns, merged_providers)
            if resolved:
                settings["enabledModels"] = resolved
            else:
                settings.pop("enabledModels", None)
    if settings.get("httpIdleTimeoutMs") == 0:
        settings.pop("httpIdleTimeoutMs")
    stale = agent_dir / "settings.json"
    if stale.is_symlink() or stale.is_file():
        stale.unlink()
    stale.write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )
    # auth.json keeps its pre-#172 shape: a symlink to the user's
    # stored auth (valid for the merged catalog's providers).
    auth_source = user_agent / "auth.json"
    auth_link = agent_dir / "auth.json"
    if auth_link.is_symlink():
        auth_link.unlink()
    if auth_source.is_file():
        auth_link.symlink_to(auth_source)
    return agent_dir


def _resolve_enabled_models(patterns: list, providers: dict) -> list:
    """Filter `enabledModels` patterns to the merged catalog (Issue #172).

    Mirrors Pi's exact reference match (`model-resolver.js`
    `findExactModelReferenceMatch`, verified against Pi 0.84.3): the
    canonical `provider/modelId` form, or a bare model id that is
    unambiguous across the catalog — case-insensitive. A pattern that
    resolves to nothing would make Pi warn `No models match pattern`
    at startup and could steer the initial model selection to a model
    the run cannot use, so the per-run settings.json keeps only what
    the per-run models.json can resolve.
    """
    canonical: set = set()
    bare_counts: dict = {}
    for provider_id, entry in providers.items():
        models = entry.get("models") if isinstance(entry, dict) else None
        if not isinstance(models, list):
            continue
        for model in models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str) or not model_id:
                continue
            canonical.add(f"{provider_id}/{model_id}".lower())
            key = model_id.lower()
            bare_counts[key] = bare_counts.get(key, 0) + 1
    resolved = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        reference = pattern.strip().lower()
        if reference in canonical:
            resolved.append(pattern)
        elif bare_counts.get(reference, 0) == 1:
            resolved.append(pattern)
    return resolved


def _pi_model_args(config: RunnerConfig) -> list[str]:
    """Return the configured Pi model flags (Issue #119).

    One `--flag value` pair per configured key, in the fixed order
    provider, model, thinking; an unset key contributes nothing, so a
    config without any of the three keys returns [] and the Pi command
    keeps its exact pre-#119 shape. The values are non-sensitive model
    identifiers (never keys or tokens) and are part of the redacted
    `log_command`, so the journal run scene records what was launched.
    """
    args: list[str] = []
    for flag, key in (
        ("--provider", "pi_provider"),
        ("--model", "pi_model"),
        ("--thinking", "pi_thinking"),
    ):
        value = getattr(config, key)
        if value is not None:
            args.extend((flag, value))
    return args


def parse_repositories(entries: object, base: Path) -> list[dict]:
    """Parse the explicit multi-repo registry (Issue #134).

    Each entry is a TOML table with the required string fields `name`,
    `path`, `github` and `base_branch`; `path` is resolved relative to
    the config file's directory. A missing/empty/non-string field, a
    non-table entry or a duplicate `name` fails fast — the existence and
    Git-checkout checks happen in `validate_config`.
    """
    if not isinstance(entries, list):
        raise ValueError("repositories must be a list of tables")
    repos: list[dict] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"repositories[{index}] must be a table")
        missing = [
            field for field in ("name", "path", "github", "base_branch")
            if not isinstance(entry.get(field), str) or not entry.get(field)
        ]
        if missing:
            raise ValueError(
                f"repositories[{index}] is missing required field(s): "
                + ", ".join(missing)
            )
        if any(repo["name"] == entry["name"] for repo in repos):
            raise ValueError(
                f"duplicate repositories name: {entry['name']!r}"
            )
        # Issue #527: the optional repository config path (default
        # `.github/orbi.toml`) — a string, no path resolution (it is a
        # repository-relative path read through the GitHub contents API,
        # never the local checkout).
        config_path = entry.get("config_path", REPO_CONFIG_PATH)
        if not isinstance(config_path, str) or not config_path:
            raise ValueError(
                f"repositories[{index}].config_path must be a non-empty "
                "string"
            )
        repos.append({
            "name": entry["name"],
            "path": _config_path(entry["path"], base),
            "github": entry["github"],
            "base_branch": entry["base_branch"],
            "config_path": config_path,
        })
    return repos


def repository_config_path(config: RunnerConfig, source_repo: str) -> str:
    """The repository config path of one source repo (Issue #527).

    The optional `[[repositories]].config_path` wins when its `github`
    entry matches the source repo; otherwise the single default location
    `.github/orbi.toml` applies.
    """
    for repo in config.repositories:
        if repo.get("github") == source_repo:
            return repo.get("config_path", REPO_CONFIG_PATH)
    return REPO_CONFIG_PATH


def repository_base_branch(config: RunnerConfig, source_repo: str) -> str:
    """The fallback base branch of one source repo (Issue #527, D3).

    A repository config that omits `base_branch` falls back to its
    `[[repositories]]` entry's `base_branch` when one matches the source
    repo, else the host `base_branch`.
    """
    for repo in config.repositories:
        if repo.get("github") == source_repo:
            return repo["base_branch"]
    return config.base_branch


def load_repo_policy(config: RunnerConfig,
                     source_repo: str) -> RepoPolicy | None:
    """Read and validate one source repo's policy file (Issue #527).

    Returns the validated :class:`RepoPolicy` with the file's blob sha
    bound, or `None` when the repository has no policy file. A
    malformed/forbidden file raises :class:`RepoConfigError` — the caller
    fails the claim fast.
    """
    return read_repo_config(
        source_repo,
        path=repository_config_path(config, source_repo),
        run_command=run_command,
    )


def apply_repo_policy(config: RunnerConfig, source_repo: str,
                      policy: RepoPolicy) -> RunnerConfig:
    """Resolve one repo's policy over the host fallback (Issue #527, D3)."""
    fallback = replace(
        config,
        base_branch=repository_base_branch(config, source_repo),
    )
    return resolve_policy(fallback, policy)


def previous_repo_config_sha(number: int, source_repo: str) -> str | None:
    """The sha recorded by the previous run of this Issue (Issue #527, D4).

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


def render_prompt(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def validate_execution_source_repos(source_repos: Sequence[str]) -> None:
    """Reject task-pool fan-out until execution has per-repo checkouts."""
    if len(source_repos) > 1:
        raise ValueError(
            "multiple source_repos are not supported with one checkout; "
            "configure exactly one source repository until multi-repo "
            "workspaces are available"
        )


def validate_config(config: RunnerConfig) -> None:
    if not config.repo_dir.is_dir():
        raise FileNotFoundError(config.repo_dir)
    # Issue #330: the deployment home (CLI install source, unit templates,
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
    # Multi-repo registry (Issue #134): every registered path must exist
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


def sync_active_milestone_variable(
    repo: str, milestone: str | None, *,
    run_command: Callable[..., str] | None = None,
) -> None:
    """Keep the triage workflow's read-only milestone variable current.

    This is deliberately a bypass: a GitHub variable outage must not stop
    the Runner's issue delivery tick.  The workflow consumes this value via
    ``vars.ORBI_ACTIVE_MILESTONE`` and the triage script resolves its title.
    """
    command_runner = run_command or globals()["run_command"]
    endpoint = f"repos/{repo}/actions/variables/ORBI_ACTIVE_MILESTONE"
    try:
        raw = command_runner(["gh", "api", endpoint], timeout=30)
        current = json.loads(raw)
        if not isinstance(current, dict):
            raise ValueError("variable response is not an object")
        if milestone is None:
            command_runner(
                ["gh", "api", "-X", "DELETE", endpoint], timeout=30,
            )
            event("active_milestone_variable_removed", repo=repo)
            return
        if current.get("value") == milestone:
            event("active_milestone_variable_unchanged", repo=repo)
            return
        command_runner(
            ["gh", "api", "-X", "PATCH", endpoint, "-f", f"name=ORBI_ACTIVE_MILESTONE",
             "-f", f"value={milestone}"], timeout=30,
        )
        event("active_milestone_variable_updated", repo=repo)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1 or "404" not in (exc.stderr or ""):
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
            return
        if milestone is None:
            event("active_milestone_variable_absent", repo=repo)
            return
        try:
            command_runner(
                ["gh", "api", "-X", "POST", "repos/{}/actions/variables".format(repo),
                 "-f", "name=ORBI_ACTIVE_MILESTONE", "-f", f"value={milestone}"],
                timeout=30,
            )
            event("active_milestone_variable_created", repo=repo)
        except Exception:
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
    except Exception:
        LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)


# Ready scans (Issue #71/#101): P0 urgent Issues are claimed before
# bugs, bugs before new features — if the delivery loop is broken,
# claiming enhancements only piles up unreviewed PRs, and a production
# outage (P0) must not wait behind ordinary work. The P0 scan runs
# first, then the bug scan, then the plain ready scan — each with the
# exact same exclusions. No priority numbers, no separate queue, no
# new state machine: three `gh issue list` searches with the same
# blockedBy semantics (Issue #54). `p0` is a plain label, not a
# delivery state: it only orders the pickup (Issue #101).
READY_SCAN_EXCLUSIONS = (
    f"-label:{IN_PROGRESS_LABEL} -label:{PR_OPENED_LABEL} "
    f"-label:{FIX_NEEDED_LABEL} -label:{MERGED_LABEL} "
    f"-label:{BLOCKED_LABEL}"
)


def ready_searches(active_milestone: str | None = None,
                   dispatch_label: str = READY_LABEL) -> tuple[str, str, str]:
    """Return the three ready scans (p0, bug, plain) in pickup order.

    With a configured `active_milestone` (Issue #139) every scan
    carries the `milestone:"<title>"` qualifier — the quoted form is
    the contract because milestone titles may contain spaces or
    special characters (verified against the live API). The scope is
    part of the QUERY, so an Issue of another Milestone (or of no
    Milestone) never enters the result set, and a `v0.2.0` Issue
    without `ai-ready` never does either: the `label:ai-ready`
    qualifier stays. The Milestone is a version scope, not a
    replacement for the `ai-ready` execution switch. P0 does NOT
    cross milestones (Issue #139 decision): the active Milestone is
    the claim scope of EVERY fresh claim, and `p0` only orders the
    pickup inside it — one uniform rule, no special case. Without a
    configured Milestone the searches are byte-identical to the
    pre-#139 scans (compat).
    """
    scope = (
        f' milestone:"{active_milestone}"' if active_milestone else ""
    )
    # Issue #527: the claim label is a delivery-policy key; the lifecycle
    # labels (`ai-in-progress`/`ai-merged`/...) stay host constants.
    label = dispatch_label or READY_LABEL
    return (
        f"label:{label} label:{P0_LABEL}{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:{label} label:bug{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:{label}{scope} {READY_SCAN_EXCLUSIONS}",
    )


def release_fallback_search(active_milestone: str | None = None,
                            dispatch_label: str = READY_LABEL) -> str:
    """Return the release fallback scan query (Issue #255).

    A Release task is a closing action and must never compete with an
    ordinary delivery for the slot: it is claimed only after the three
    ordinary ready scans (p0/bug/plain) found nothing claimable. The
    query keeps `label:ai-ready` (the human execution switch — a release
    Issue without it stays waiting, as before) plus `label:ai-release`
    and the same five delivery-state exclusions as the ordinary scans
    (`READY_SCAN_EXCLUSIONS`). With a configured `active_milestone` it
    carries the same `milestone:"<title>"` scope (the quoted form is the
    contract, same as `ready_searches`). `ai-epic` is excluded by the
    code-layer Epic guard in `_pick_from_scan`, not the query.
    """
    scope = (
        f' milestone:"{active_milestone}"' if active_milestone else ""
    )
    label = dispatch_label or READY_LABEL
    return (
        f"label:{label} label:{RELEASE_LABEL}{scope} "
        f"{READY_SCAN_EXCLUSIONS}"
    )


def _issue_label_set(issue: dict) -> frozenset[str]:
    """The Issue's label names (the scans fetch `labels`).

    The fact shape the scene classification (`orbi.delivery_scene`)
    reads; a missing or malformed `labels` field yields the empty set —
    the classification then sees an unlabelled ticket, never a crash.
    """
    return frozenset(
        label.get("name") for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    )


def is_epic(issue: dict) -> bool:
    """Return True when one issue carries the `ai-epic` label (Issue #93).

    A pure function of the issue's `labels` (the scans fetch `labels`,
    so no extra gh call), the same style as `issue_priority`. A
    missing or malformed `labels` field fails open to "not an epic":
    the scan always requests `labels`, so a shape change only loses
    the Epic guard for one run — it must never deadlock the queue.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == EPIC_LABEL:
            return True
    return False


def is_release(issue: dict) -> bool:
    """Return True when one issue carries the `ai-release` label (#98).

    A pure function of the issue's `labels` (the scans fetch `labels`,
    so no extra gh call), the same style as `is_epic` and
    `issue_priority`. A missing or malformed `labels` field fails open
    to "not a release task": the scan always requests `labels`, so a
    shape change only loses the release routing for one run — it must
    never deadlock the queue.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == RELEASE_LABEL:
            return True
    return False


def reconcile_open_epics(repo: str, run_id: str) -> list[str]:
    """Sweep open Epics once per tick; ordinary pickup must not depend on it."""
    epics = list_issues(
        repo, state="open", search=f"label:{EPIC_LABEL}",
        json_fields="number,body,labels", limit=200,
    )
    evidence: list[str] = []
    for listed_epic in epics:
        number = listed_epic.get("number")
        try:
            child_evidence = _verify_epic_complete(repo, listed_epic)
        except (ValueError, json.JSONDecodeError) as exc:
            reason = str(exc)
            event("epic_kept_open", issue=number, repo=repo, reason=reason)
            evidence.append(f"Epic #{number} kept open: {reason}")
            continue
        audit = _epic_audit(child_evidence)
        comments = issue_comments(int(number), repo=repo)
        if not any(audit in str(comment.get("body", "")) for comment in comments):
            comment_issue(int(number), repo=repo,
                         body=f"<!-- orbi:run={run_id} -->\n{audit}\nrun_id={run_id}")
        close_issue(int(number), repo=repo)
        event("epic_closed", issue=number, repo=repo)
        evidence.append(f"Epic #{number} closed after verification ({'; '.join(child_evidence)})")
    return evidence


def reconcile_release_milestones(repo: str, run_id: str) -> list[str]:
    """Close published-release Milestones that now have no open Issues.

    This is a tick-level, fail-open sweep: release publication and the exact
    Milestone title are independent GitHub facts, so a late-closing Issue is
    reconciled on a later tick without requiring a new release run.
    """
    all_milestones = list_milestones(repo)
    milestones = [m for m in all_milestones if m.get("state") == "open"]
    for milestone in all_milestones:
        if not isinstance(milestone, dict) or milestone.get("state") not in {"open", "closed"}:
            event(
                "milestone_kept_open", number=milestone.get("number")
                if isinstance(milestone, dict) else None,
                repo=repo, reason="malformed",
            )
    releases_raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/releases?per_page=100",
        "--paginate", "--slurp",
    ])
    releases = parse_paginated_issue_array(releases_raw)
    published_tags = {
        release.get("tag_name") for release in releases
        if isinstance(release.get("tag_name"), str)
        and release.get("draft") is False
    }
    evidence: list[str] = []
    for milestone in milestones:
        number = milestone.get("number")
        title = milestone.get("title")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(title, str):
            event("milestone_kept_open", number=number, repo=repo, reason="malformed")
            continue
        # Closed duplicates still make the title ambiguous; do not guess
        # which milestone a release belongs to.
        matches = [m for m in all_milestones if m.get("title") == title]
        if len(matches) != 1:
            reason = "ambiguous title" if len(matches) > 1 else "missing title"
            event("milestone_kept_open", number=number, repo=repo, reason=reason)
            continue
        if title not in published_tags:
            event("milestone_kept_open", number=number, repo=repo,
                  reason="no published release")
            continue
        open_issues = milestone_open_issues(repo, number)
        if open_issues:
            event("milestone_kept_open", number=number, repo=repo, reason="open issues")
            continue
        close_milestone(repo, int(number))
        event("milestone_closed", number=number, repo=repo)
        evidence.append(f"Milestone #{number} ({title}) closed")
    return evidence


# Issue #746: the hidden marker of the orphan-PR report — the sweep's
# idempotency key (one report per PR, never one per tick).
ORPHAN_PR_MARK = "<!-- orbi:orphan-pr -->"

_ORPHAN_PR_BRANCH_RE = re.compile(r"orbi/.+-issue-(\d+)\Z")


def orphan_pr_branch_issue(head_ref: object) -> int | None:
    """The source Issue number of a stable delivery branch, or None.

    `task_branch` names every Runner delivery branch
    `orbi/<source_repo slug>-issue-<N>`; any other head (a human's or an
    external contributor's branch) is never the Runner's business.
    """
    if not isinstance(head_ref, str):
        return None
    match = _ORPHAN_PR_BRANCH_RE.fullmatch(head_ref)
    return int(match.group(1)) if match else None


def reconcile_orphan_prs(repo: str, run_id: str) -> list[str]:
    """Report open delivery PRs whose source Issue is closed (Issue #746).

    A human may close an Issue while its delivery is in flight, and the
    close can land before OR after the PR is opened — every resumable
    scan reads `state=open` Issues only, so without this sweep nothing
    would ever look at the resulting PR again. Tick-level and fail-open
    like the Epic/Milestone reconciliations: each orphan PR gets ONE
    idempotent report comment (the `ORPHAN_PR_MARK` marker); the
    merge/close decision stays with the human — the Runner never
    auto-merges and never auto-closes across a human's close decision.
    """
    raw = run_gh_read_command([
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName", "--limit", "200",
    ], timeout=30)
    prs = json.loads(raw)
    if not isinstance(prs, list):
        raise ValueError("pr list must return a JSON array")
    evidence: list[str] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        pr_number = pr.get("number")
        issue_number = orphan_pr_branch_issue(pr.get("headRefName"))
        if not isinstance(pr_number, int) or issue_number is None:
            continue
        details = issue_view(issue_number, "state", repo=repo, timeout=30)
        state = details.get("state") if isinstance(details, dict) else None
        if state == "OPEN":
            continue
        if state != "CLOSED":
            raise ValueError("issue view state must be OPEN or CLOSED")
        if any(
            isinstance(comment, dict)
            and ORPHAN_PR_MARK in str(comment.get("body", ""))
            for comment in pr_comments(pr_number, repo=repo)
        ):
            continue
        comment_pr(
            pr_number, repo=repo,
            body=(
                f"{ORPHAN_PR_MARK}\n{run_marker(run_id)}\n"
                f"Orbi orphan PR: the source Issue #{issue_number} is "
                f"closed while this PR is still open, so it will never "
                f"be merged automatically. Human decision: merge this "
                f"PR to keep the work, or close it.\n"
                f"run_id={run_id}"
            ),
        )
        event(
            "orphan_pr_reported", pr=pr_number, issue=issue_number,
            repo=repo,
        )
        evidence.append(
            f"PR #{pr_number} reported: source Issue #{issue_number} is closed"
        )
    return evidence


# The scenes the ready scans claim: the fresh-claim family of the
# delivery-scene classification (Issue #787). A release candidate is
# claimable only through the release fallback scan's `allow_release`
# gate above; the classification still names its scene. A candidate
# that classifies elsewhere is skipped — a terminal state the query's
# index lagged behind, or an in-flight/opened-PR state another scan
# owns — and a candidate carrying NO readable labels fails open (the
# is_epic/is_release convention): the query is the claim authority.
_FRESH_CLAIM_SCENES = frozenset({
    DeliveryScene.FRESH_CLAIM,
    DeliveryScene.RELEASE,
    DeliveryScene.CONTENT_ONLY,
    DeliveryScene.OPS,
})
_SCAN_CLAIMABLE_SCENES = _FRESH_CLAIM_SCENES | {
    DeliveryScene.RESTART_IN_FLIGHT,
}


def _pick_from_scan(
    issues: list[dict], repo: str, allow_release: bool = False,
    active_milestone: str | None = None,
    ready_label: str = READY_LABEL,
) -> dict | None:
    """Return the first claimable Issue of one scan result, else None.

    The per-Issue guards, shared by the three ordinary ready scans and
    the release fallback scan (Issue #255). An `ai-epic` Issue is never
    claimed — no label change, no worktree, no run, no slot — with the
    structured `epic_not_claimed` line (the check precedes the blockedBy
    check: "it is an Epic" is the more fundamental reason). An Issue
    with open native blockers is skipped with the structured
    `blocked_by` line (Issue #54). Otherwise the Issue is picked and the
    pickup log carries the explicit priority field (Issue #101): `p0`
    for urgent Issues, `normal` otherwise.

    `allow_release` controls the release skip (Issue #255): the three
    ordinary scans skip an `ai-release` Issue (`release_not_claimed`) so
    a release never competes with an ordinary delivery for the slot; the
    release fallback scan passes `allow_release=True` and claims it.

    A release candidate additionally passes the Milestone completeness
    gate (Issue #663): while the Milestone still counts any other open
    Issue (`open_issues > 1`), the release is skipped with the
    structured `release_milestone_incomplete` line and the Issue stays
    `ai-ready` for the next tick — a recoverable wait, never
    `ai-blocked`. A Milestone query that cannot be evaluated skips the
    release too (`release_milestone_check_failed`): a bad release is
    irreversible, so the check fails safe and the next tick retries.

    The last guard is the delivery-scene classification itself (Issue
    #787): the ready scans claim only the fresh-claim family, decided
    by the same pure `classify` the dispatch layer runs, so a stale
    index handing over a ticket that no longer carries the queue label
    is skipped, never claimed into a scene it is no longer in.
    """
    for issue in issues:
        if is_epic(issue):
            event(
                "epic_not_claimed", issue=issue.get("number"), repo=repo,
            )
            continue
        if not allow_release and is_release(issue):
            event(
                "release_not_claimed", issue=issue.get("number"), repo=repo,
            )
            continue
        if allow_release and is_release(issue):
            target_milestone = release_target_milestone(
                issue, active_milestone,
            )
            if target_milestone is not None:
                try:
                    open_issues = milestone_open_issue_count(
                        repo, target_milestone,
                    )
                except Exception as exc:
                    event(
                        "release_milestone_check_failed", level=logging.ERROR,
                        issue=issue.get("number"), repo=repo,
                        milestone=target_milestone, error=exc,
                    )
                    return None
                if open_issues > 1:
                    event(
                        "release_milestone_incomplete",
                        issue=issue.get("number"), repo=repo,
                        milestone=target_milestone, open_issues=open_issues,
                    )
                    continue
        blockers = open_blocker_numbers(issue)
        if blockers:
            event(
                "blocked_by", issue=issue.get("number"), repo=repo,
                blockers=",".join(str(number) for number in blockers),
            )
            continue
        current_labels = _issue_label_set(issue)
        if current_labels:
            scene_value = classify(
                labels=current_labels, scene=None, pr_state=None,
                worktree_present=False, branch_present=False,
                body_markers=body_markers(issue.get("body")),
                ready_label=ready_label,
            )
            if scene_value not in _SCAN_CLAIMABLE_SCENES:
                event(
                    "claim_yield", issue=issue.get("number"),
                    reason=f"scene_{scene_value.value}",
                )
                continue
        event(
            "picked", issue=issue.get("number"), repo=repo,
            priority=issue_priority(issue),
        )
        return issue
    return None


def pick_issue(repo: str, active_milestone: str | None = None,
               dispatch_label: str = READY_LABEL) -> dict | None:
    # A merged delivery keeps `ai-ready` + `ai-merged` on the (still
    # open) Issue; `ai-merged` is the success terminal state, so it is
    # excluded from the ready scan like every other delivery state.
    # The scan fetches the ready queue (not just the first Issue) and
    # reads the native GitHub dependency per Issue (Issue #54): an
    # Issue with open blockers is skipped — no claim, no label change,
    # no worktree — and the next ready Issue is considered instead.
    # Issue #93: an `ai-epic` Issue is skipped too — no claim, no
    # label change, no worktree, no run, no slot — with the structured
    # `epic_not_claimed` line (the Epic check precedes the blockedBy
    # check). Issue #71/#101: the P0 scan runs first, then the bug
    # scan; a P0 or bug with open blockers is skipped there and the
    # next scan still decides. The scan also fetches `labels` so the
    # picked issue's priority (and Epic-ness) is visible without an
    # extra gh call. Issue #139: with a configured `active_milestone`
    # all three scans are scoped to that Milestone in the query itself
    # (see `ready_searches`) — the Epic skip and the blockedBy skip
    # above are the unchanged second (code) layer, and a failed scan
    # still fails open (never a silent claim of the wrong version).
    # Issue #255: an `ai-release` Issue is skipped by the three ordinary
    # scans (`release_not_claimed`) and claimed only by the release
    # fallback scan that runs AFTER all three found nothing claimable —
    # a release is a closing action and must never take the slot ahead
    # of an ordinary delivery.
    for search in ready_searches(active_milestone, dispatch_label):
        try:
            issues = list_issues(
                repo, state="open", search=search,
                json_fields="number,title,body,labels,blockedBy", limit=200,
            )
        except Exception as exc:
            # Fail open (Issue #54): a failed blockedBy query must
            # never deadlock the queue. This tick claims nothing from
            # this repo and the next tick retries the query; the error
            # is logged, never raised, and no label is touched.
            event(
                "blocked_by_check_failed", level=logging.ERROR,
                repo=repo, error=exc,
            )
            return None
        picked = _pick_from_scan(
            issues, repo, ready_label=dispatch_label,
        )
        if picked is not None:
            return picked
    # Release fallback (Issue #255): only when no ordinary delivery
    # (p0/bug/plain) is claimable. The query keeps `label:ai-ready` +
    # `label:ai-release` and the same delivery-state exclusions; the Epic
    # and blockedBy guards still apply via `_pick_from_scan`. Issue
    # #663: the release candidate also passes the Milestone completeness
    # gate inside `_pick_from_scan`, so the query fetches `milestone`.
    # A failed fallback query fails open exactly like any other scan.
    try:
        issues = list_issues(
            repo, state="open",
            search=release_fallback_search(active_milestone, dispatch_label),
            json_fields="number,title,body,labels,blockedBy,milestone",
            limit=200,
        )
    except Exception as exc:
        event(
            "blocked_by_check_failed", level=logging.ERROR,
            repo=repo, error=exc,
        )
        return None
    return _pick_from_scan(
        issues, repo, allow_release=True,
        active_milestone=active_milestone,
        ready_label=dispatch_label,
    )


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
    dispatch_label: str = READY_LABEL,
) -> dict | None:
    """Return one in-flight Issue a killed runner left behind.

    `dispatch_label` is the repository's claim label (Issue #527): a
    repository policy may replace `ai-ready` with its own label, and the
    in-flight scan must find the SAME queue entry the ready scan used —
    otherwise a killed (or model-wait-recovered) run would be stranded
    forever in a repository with a custom dispatch label.

    A SIGKILLed runner leaves the task worktree and the `ai-in-progress`
    claim label behind (the failure path never ran); the Issue keeps
    `ai-ready` too (a claim never removes it). The ready scan excludes
    `ai-in-progress`, so without this scan the run is never resumed and
    the Issue is stuck forever — the Issue #18 acceptance "restart finds
    the same progress comment by run marker" would be unreachable in the
    production flow. `process_issue`'s resume block (newest worktree's
    run id) then reuses the run instead of starting a second one. Every
    other delivery state is excluded: those Issues are owned by the
    resumable-PR scan (`ai-fix-needed`) or are terminal. `ai-epic` is
    excluded too (Issue #93): a legacy Epic left behind with
    `ai-in-progress` (the #80 scene, before the Epic mechanism existed)
    must never be resumed into a run — an Epic is coordination, not an
    executable task.

    The scan runs only when no OTHER runner is live: a slot held by
    another process proves a live runner is working (on this or another
    Issue), so the `ai-in-progress` label is in flight, not orphaned —
    resuming it here would start a second Pi for a run that is alive
    (Issue #39 slot semantics: the flock lock is the source of truth).
    This runner's own slot is excluded: `main` took it before the claim
    scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    label = dispatch_label or READY_LABEL
    # `labels` (Issue #101): a P0 a killed runner left behind
    # keeps its priority in the progress comment on resume.
    # `milestone` (Issue #671): a release run killed mid-release is
    # resumed with THIS dict, and `process_release` scopes its
    # leftover-delivery gate to the release's own Milestone — the
    # Issue is the authority (never `active_milestone`: a resume is
    # not gated by a Milestone change, Issue #139).
    issues = list_issues(
        repo, state="open",
        search=(
            f"label:{label} label:{IN_PROGRESS_LABEL} "
            f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
            f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL} "
            f"-label:{EPIC_LABEL}"
        ),
        json_fields="number,title,body,labels,milestone", limit=1,
    )
    candidate = issues[0] if issues else None
    if candidate is not None:
        # Issue #787: the same pure classification the dispatch runs.
        # This scan hands the candidate to `process_issue`, whose full
        # fact set decides the handler, so the scan skips only a
        # candidate that left EVERY claimable state (a relabel inside
        # the query's staleness window); a candidate with no readable
        # labels fails open (the is_epic/is_release convention).
        current_labels = _issue_label_set(candidate)
        if current_labels:
            scene_value = classify(
                labels=current_labels, scene=None, pr_state=None,
                worktree_present=False, branch_present=False,
                body_markers=body_markers(candidate.get("body")),
            )
            if scene_value not in _SCAN_CLAIMABLE_SCENES:
                event(
                    "claim_yield", issue=int(candidate["number"]),
                    reason=f"scene_{scene_value.value}",
                )
                return None
    return candidate


def pick_next_issue(
    repos: list[str], active_milestone: str | None = None,
) -> tuple[str, dict] | None:
    """Scan sources in order; return the first ready issue and its source."""
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue
    return None


def claim_route(labels: set[str], *, branch_exists: bool,
                open_pr: bool, ready_label: str = READY_LABEL) -> str:
    """Choose the fresh-claim action from the physical GitHub scene.

    This is deliberately pure: labels are the event and branch/PR existence
    is the observed physical state.  The existing review loop handles the
    returned ``review`` route. `ready_label` is the repository's dispatch
    label (Issue #527; default `ai-ready`).
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

    The marker is written by the triage workflow (Issue #608); a claim of
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
    return pr


def started_pi_comment_body(run_id: str, run_info: str, branch: str,
                            worktree: Path,
                            extra_fields: dict | None = None) -> str:
    """The start comment doubles as the recoverable run scene (Issue #45).

    `extra_fields` carries the repo-config audit fields (Issue #527, D4);
    their values may contain spaces, so they are rendered by the field
    block and never spliced into the space-separated `run_info`.
    """
    fields = _run_info_fields(run_info)
    fields["branch"] = str(branch)
    fields["worktree"] = str(worktree)
    if extra_fields:
        fields.update(extra_fields)
    info = _run_info_fields(run_info)
    headline = "Orbi started Pi: " + " ".join(
        f"{key}={info[key]}" for key in ("run_id", "priority")
        if key in info
    )
    return field_block(run_id, headline, fields)


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str,
                           external: bool = False) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR (Issue #45). The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id — a comment must never be able to name a local path. An
    external takeover (Issue #608) marks the scene `external`: the PR is
    the contributor's own (no run marker, no `Fixes` keyword in its
    body), and the delivery branch is the PR's head branch — derived
    from the takeover worktree, never from the comment.

    The machine-readable record is the single hidden `orbi:scene:v1`
    block rendered from the `Scene` (Issue #786); the field lines below
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
    body = field_block(run_id, headline, fields)
    lines = body.splitlines()
    lines.insert(1, scene.render(scene_record))
    return "\n".join(lines)


def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Orbi opened PR:` comment into a resume scene.

    The v1 scene block (orbi.scene) is the machine protocol; the
    human-readable text is the legacy fallback kept for one transition
    version (Issue #786). Returns None when the body is not an
    opened-PR comment. Fails fast when the comment is malformed:
    resuming must recover the exact run (run id, base, PR URL), never a
    guess (Issue #45). Branch and worktree are not parsed: the runner
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
    }


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only comments posted by a trusted maintainer (OWNER, MAINTAINER,
    MEMBER or COLLABORATOR) are considered: a public comment can never
    become the recovery scene (Issue #45 review, BLOCKER). The two
    failure shapes stay distinct for the caller (Issue #786):
    `scene.SceneError` when a trusted scene comment is corrupted,
    `scene.SceneMissingError` when no trusted comment carries a scene
    at all. Neither may be guessed at.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        found = parse_pr_comment(comment.get("body"))
        if found is not None:
            return found
    raise scene.SceneMissingError(
        "no 'Orbi opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )


def _route_external_pr_ticket(issue: dict, repo: str) -> bool:
    """Route a marker-bearing opened-PR ticket through external
    integration instead of `ai-blocked`. Returns True when handled.

    Issue #726: the triage workflow (#621) labels the linked Issue
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


def pick_resumable_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> tuple[dict, dict] | None:
    """Return the newest opened-PR delivery and its resume scene.

    Both opened-PR states are scanned (Issue #70): `ai-fix-needed`
    (awaiting the next review session after a finding or a base
    conflict — Issue #82: the review session fixes findings in the same
    session, so the next tick runs the same independent review on the
    same branch, worktree and PR) and `ai-pr-opened` (awaiting review —
    the next tick runs the independent review). The `ai-pr-opened`
    scan exists because the delivery that opened the PR can be gone: the
    progress-publishing failure behind Issue #70 used to label the
    delivered Issue `ai-blocked` before the review started, and a killed
    runner can die inside the delivery wait loop, leaving a valid
    MERGEABLE PR with no owner. Without the scan such a delivery is
    picked up by no other scan (`pick_issue` excludes `ai-pr-opened`)
    and is stranded forever. `ai-blocked` Issues are excluded (they need
    a human decision first), as are merged Issues and closed Issues.
    `ai-in-progress` is NOT excluded (Issue #178): a runner killed
    during review leaves the backfilled in-flight label behind on the
    opened-PR delivery, and the same scan must pick it back up — the
    positive `label:ai-fix-needed,ai-pr-opened` qualifier already
    restricts the scan to opened-PR Issues (an implement-phase Issue
    has `ai-ready`+`ai-in-progress` but neither opened-PR label, so it
    never matches). A scene that cannot be recovered is a SINGLE-Issue
    failure (Issue #672): the Issue is marked `ai-blocked` with the
    concrete reason (`block_scene_failure`) and the scan reports no
    resumable delivery, so the tick continues with the in-flight and
    ready scans and exits 0 — one corrupted Issue must never make every
    tick crash while the whole queue waits.

    The scan runs only when no OTHER runner is live (the same guard as
    `pick_in_progress_issue`, Issue #39 slot semantics): a slot held by
    another process proves a live runner is working, so an opened-PR
    delivery is in flight, not stranded — resuming it here would start
    a second review Pi in the same worktree/branch/run, and the second
    `gh pr merge --match-head-commit` on the already-merged PR would
    fail and mark the merged Issue `ai-blocked` (Issue #70 review
    round 1). This runner's own slot is excluded: `main` took it
    before the claim scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    # `label:a,b` is GitHub's OR within one label qualifier
    # (verified live: repeating the qualifier matches only the
    # first label). `ai-in-progress` is intentionally NOT excluded
    # (Issue #178): a killed review runner leaves the backfilled
    # in-flight label behind, and the positive qualifier above
    # already keeps implement-phase Issues out.
    # `labels` (Issue #101): a resumed P0 delivery keeps its
    # priority in the progress comment through review/merge.
    # `body` (Issue #787): the scene classification reads the
    # delivery markers — without it the #726 external routing of a
    # marker ticket with no trusted scene comment is unreachable in
    # production (the probe read a body the query never fetched).
    issues = list_issues(
        repo, state="open",
        search=(
            f"label:{FIX_NEEDED_LABEL},{PR_OPENED_LABEL} "
            f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL}"
        ),
        json_fields="number,title,state,url,labels,body", limit=1,
    )
    if not issues:
        return None
    issue = issues[0]
    if issue.get("state") != "OPEN":
        return None
    comments = issue_comments(int(issue["number"]), repo=repo)
    try:
        found = resume_scene(comments)
    except scene.SceneError as exc:
        # A trusted scene comment exists but is corrupted (Issue #786):
        # probe the #726 external route first; otherwise this is the
        # ONLY trigger of `block_scene_failure` — a present-but-broken
        # scene is a writer bug or tampering and needs a human.
        if _route_external_pr_ticket(issue, repo):
            return None
        block_scene_failure(issue, exc, repo, comments)
        return None
    except scene.SceneMissingError as exc:
        # No trusted comment carries a scene at all — a distinct branch
        # from corruption (Issue #786). The original #726 incident was
        # exactly this shape, so the external route is probed first;
        # un-routed, the same Issue #50 terminal contract applies
        # through its OWN reporting (explicit reason + human next
        # step), never `block_scene_failure`. The failure is scoped to
        # this one Issue (Issue #672): the tick continues.
        if _route_external_pr_ticket(issue, repo):
            return None
        number = int(issue["number"])
        LOGGER.error("issue=%s resume scene is missing: %s", number, exc)
        marker = latest_run_marker(comments)
        try:
            apply_label_patch(
                number, repo=repo, event=EVENT_BLOCKED,
                current_labels={FIX_NEEDED_LABEL},
            )
            comment_issue(
                number, repo=repo,
                body=(f"{marker}\n" if marker else "") + (
                    f"Orbi failed: {exc}; no trusted 'Orbi opened PR' "
                    "scene comment exists on this Issue, so the "
                    "opened-PR delivery cannot be resumed — this is an "
                    "external precondition the AI cannot safely judge "
                    "or fix, so it cannot be recovered automatically "
                    "(the Issue stays ai-blocked until a human "
                    "decides) — restore the trusted 'Orbi opened PR' "
                    "scene comment or relabel the Issue ai-fix-needed"
                ),
            )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        return None
    # Issue #787: the scan and the dispatch classify with the same pure
    # function. This scan owns the resumable route only: a candidate
    # that classifies elsewhere left the opened-PR state between the
    # query and this read (a relabel race) — claim nothing this tick.
    # A candidate with no readable labels fails open (the is_epic /
    # is_release convention): the trusted scene is the authority.
    current_labels = _issue_label_set(issue)
    if current_labels:
        found_scene = classify(
            labels=current_labels, scene=found, pr_state=None,
            worktree_present=False, branch_present=False,
            body_markers=body_markers(issue.get("body")),
        )
        if found_scene is not DeliveryScene.RESUME_REVIEW:
            event(
                "claim_yield", issue=int(issue["number"]),
                reason=f"scene_{found_scene.value}",
            )
            return None
    return issue, found


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an opened-PR Issue `ai-blocked` when its scene is malformed.

    The blocked transition is scoped to this one Issue (Issue #672): the
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
        # Issue #50: a scene that cannot be recovered is an external
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


def rewrite_active_milestone_line(config_path: Path, new_value: str) -> None:
    """Replace only the configured active_milestone line, byte-for-byte."""
    text = config_path.read_bytes().decode("utf-8")
    pattern = re.compile(r"(?m)^[ \t]*active_milestone[ \t]*=[ \t]*[^\r\n]+")
    if not pattern.search(text):
        raise RuntimeError(
            f"active_milestone line not found in {config_path}"
        )
    updated, _ = pattern.subn(
        f'active_milestone = "{new_value}"', text, count=1,
    )
    config_path.write_bytes(updated.encode("utf-8"))


def arm_release_ticket(repo: str, active_milestone: str) -> None:
    """Arm one open release ticket for the current milestone on idle.

    This is an idle-path bypass: callers deliberately catch failures so a
    GitHub label operation cannot change the outcome of the main tick.
    """
    search = (
        f"label:{RELEASE_LABEL} -label:{READY_LABEL} "
        f'milestone:"{active_milestone}"'
    )
    issues = list_issues(
        repo, state="open", search=search,
        json_fields="number", limit=200, timeout=30,
    )
    if not issues:
        return
    number = issues[0].get("number")
    if not isinstance(number, int):
        raise RuntimeError(f"release ticket has invalid issue number: {number!r}")
    run_command([
        "gh", "issue", "edit", str(number), "--repo", repo,
        "--add-label", READY_LABEL,
    ], timeout=30)
    event(
        "release_ticket_armed", issue=f"#{number}",
        milestone=active_milestone,
    )


def _pending_milestone_issue(
    repo: str, old: str, candidates: list[dict],
) -> None:
    """Create one idempotent human-confirmation issue for a milestone advance."""
    titles = [str(candidate["title"]) for candidate in candidates]
    fingerprint = f"orbi-milestone-advance old={old} candidates={','.join(titles)}"
    existing = list_issues(
        repo, state="all", search=f'in:body "{fingerprint}"',
        json_fields="number", limit=1, timeout=30,
    )
    if existing:
        return
    lines = [
        "## Milestone 自动推进待人工确认",
        "",
        fingerprint,
        "",
        f"当前 milestone `{old}` 已完成，等待确认推进到以下候选版本：",
        "",
    ]
    lines.extend(
        f"- `{candidate['title']}`：{candidate.get('open_issues', 0)} open issues"
        for candidate in candidates
    )
    lines.extend([
        "",
        "请人工将 `active_milestone` 改为目标版本（或恢复自动推进），然后关闭本 Issue。",
    ])
    run_command([
        "gh", "issue", "create", "--repo", repo,
        "--title", f"Milestone {old} 已完成，等待确认推进到 {titles[0]}",
        "--body", "\n".join(lines),
    ], timeout=30)


def _close_stale_milestone_issues(repo: str, active_milestone: str) -> None:
    """Close manual advance notices that no longer match the config."""
    issues = list_issues(
        repo, state="open", search='in:body "orbi-milestone-advance"',
        json_fields="number,body", limit=200, timeout=30,
    )
    pattern = re.compile(r"orbi-milestone-advance old=([^ ]+)")
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        body = issue.get("body")
        match = pattern.search(body) if isinstance(body, str) else None
        if match is None or match.group(1) == active_milestone:
            continue
        run_command([
            "gh", "issue", "close", str(issue["number"]), "--repo", repo,
            "--comment", (
                f"已收敛：当前配置 active_milestone = `{active_milestone}`。"
            ),
        ], timeout=30)
        event(
            "stale_milestone_issue_closed", issue=f"#{issue['number']}",
            active=active_milestone,
        )


def advance_active_milestone_on_idle(
    repo: str, active_milestone: str, config_path: Path,
    *, auto_next_milestone: bool = True,
) -> tuple[str, str | None]:
    """Check and advance a configured milestone after no_ready_issue."""
    milestones = list_milestones(repo, timeout=30)
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and milestone.get("title") == active_milestone
    ]
    if not matches:
        open_list = ", ".join(
            f"{milestone.get('title')}({milestone.get('open_issues')})"
            for milestone in milestones
            if isinstance(milestone, dict) and milestone.get("state") == "open"
        ) or "(none)"
        raise RuntimeError(
            f"active_milestone_missing current={active_milestone}; "
            f"open milestones: {open_list}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"active_milestone {active_milestone}: ambiguous exact-title "
            f"match in {repo}, refusing to guess"
        )
    if matches[0].get("state") == "open":
        try:
            _close_stale_milestone_issues(repo, active_milestone)
        except Exception:
            # Closing an obsolete confirmation is notification maintenance;
            # it must not turn an otherwise successful idle tick into a
            # delivery failure.
            LOGGER.exception(
                "stale_milestone_issue_close_failed repo=%s active=%s",
                repo, active_milestone,
            )
        return "open", None
    current = _parse_version_title(active_milestone)
    candidates = []
    for milestone in milestones:
        if not isinstance(milestone, dict) or milestone.get("state") != "open":
            continue
        version = _parse_version_title(milestone.get("title"))
        if version is not None and current is not None and version > current:
            candidates.append((version, milestone.get("title")))
    if not candidates:
        event(
            "active_milestone_advance_none", current=active_milestone,
            closed=active_milestone, repo=repo,
        )
        return "closed", None
    candidates.sort()
    candidate_details = [
        milestone for _, title in candidates
        for milestone in milestones
        if isinstance(milestone, dict) and milestone.get("title") == title
    ]
    if not auto_next_milestone:
        candidate_titles = ",".join(title for _, title in candidates)
        event(
            "active_milestone_advance_pending", level=logging.WARNING,
            old=active_milestone, candidates=candidate_titles,
            auto_next_milestone="false",
        )
        try:
            _pending_milestone_issue(
                repo, active_milestone, candidate_details,
            )
        except Exception:
            # The confirmation Issue is an idle-path notification. Its
            # failure must not turn an otherwise successful no-ready tick
            # into a delivery failure (Issue #73/#79).
            LOGGER.exception(
                "pending_milestone_issue_failed repo=%s old=%s",
                repo, active_milestone,
            )
        return "closed", None
    new_value = candidates[0][1]
    rewrite_active_milestone_line(config_path, new_value)
    event(
        "active_milestone_advanced", old=active_milestone, new=new_value,
        closed=active_milestone, repo=repo,
    )
    return "closed", new_value


def pick_next_delivery(
    repos: Sequence[str], slot_dir: Path, max_concurrency: int,
    active_milestone: str | None = None, config: RunnerConfig | None = None,
) -> tuple[str, dict, dict | None] | None:
    """Scan sources in order: resumable PRs, in-flight restarts, ready.

    Returns `(source_repo, issue, scene)` where `scene` is None for a
    fresh claim or a restart resume. Resuming an open PR keeps the
    single concurrency slot occupied by the same run (implement →
    review → fix → merge), so a second Pi is never started for a run
    that already has a PR. An in-flight Issue (a killed runner left
    `ai-in-progress` behind, Issue #18) is recovered before the ready
    scan: `process_issue`'s resume block reuses the newest worktree's
    run id, so the same progress comment is kept instead of a second
    run being started on an Issue that is already in flight.

    Issue #139: `active_milestone` scopes only the FRESH claim
    (`pick_issue`) — the resumable-PR and in-flight restart scans are
    resume states, and running an in-flight or opened-PR delivery to
    completion is never gated by a Milestone change.
    """
    # The sweep runs before a delivery is selected, so a fresh tick has no
    # task attempt yet.  Give its auditable comments one tick-scoped marker;
    # a selected delivery will replace this binding with its own attempt id.
    tick_run_id = current_run_id()
    if tick_run_id is None:
        tick_run_id = new_run_id()
        set_run_id(tick_run_id)
    for repo in repos:
        # Epic reconciliation is a per-tick bypass: a broken GitHub query or
        # mutation must never prevent the ordinary delivery scans.
        try:
            reconcile_open_epics(repo, tick_run_id)
        except Exception:
            LOGGER.exception("epic_reconcile_failed repo=%s", repo)
        # Milestone reconciliation intentionally follows the Epic sweep so a
        # just-closed final Epic can make its Milestone eligible this tick.
        try:
            reconcile_release_milestones(repo, tick_run_id)
        except Exception:
            LOGGER.exception("milestone_reconcile_failed repo=%s", repo)
        # Orphan-PR reconciliation (Issue #746) follows the same bypass
        # pattern: a broken GitHub query must never prevent the ordinary
        # delivery scans.
        try:
            reconcile_orphan_prs(repo, tick_run_id)
        except Exception:
            LOGGER.exception("orphan_pr_reconcile_failed repo=%s", repo)
        selected = pick_resumable_delivery(
            repo, slot_dir, max_concurrency,
        )
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        _, dispatch_label = _repo_scan_keys(config, repo, active_milestone)
        issue = pick_in_progress_issue(
            repo, slot_dir, max_concurrency,
            dispatch_label=dispatch_label,
        )
        if issue is not None:
            return repo, issue, None
    for repo in repos:
        issue = _pick_issue_with_repo_policy(repo, active_milestone, config)
        if issue is not None:
            return repo, issue, None
    return None


def _repo_scan_keys(
    config: RunnerConfig | None, repo: str, active_milestone: str | None,
) -> tuple[str | None, str]:
    """Resolve one source repo's scan keys from its policy (Issue #527).

    Returns `(active_milestone, dispatch_label)`. The read is fail-open and
    a malformed repository file is ignored here (host keys keep the scan
    alive): the claim blocks the Issue with the readable reason instead of
    silently claiming nothing.
    """
    if config is None:
        return active_milestone, READY_LABEL
    try:
        policy = load_repo_policy(config, repo)
    except RepoConfigError as exc:
        event(
            "repo_config_invalid", level=logging.ERROR,
            repo=repo, reason=exc,
        )
        policy = None
    if policy is None:
        return active_milestone, READY_LABEL
    return (
        policy.active_milestone
        if policy.active_milestone is not None
        else active_milestone,
        policy.dispatch_label or READY_LABEL,
    )


def _pick_issue_with_repo_policy(
    repo: str, active_milestone: str | None, config: RunnerConfig | None,
) -> dict | None:
    """Fresh ready scan with the repo's scan keys (Issue #527).

    `dispatch_label` and `active_milestone` are resolved from the
    repository policy before the scan (see `_repo_scan_keys`).
    """
    milestone, dispatch_label = _repo_scan_keys(
        config, repo, active_milestone,
    )
    if dispatch_label == READY_LABEL:
        return pick_issue(repo, milestone)
    return pick_issue(repo, milestone, dispatch_label=dispatch_label)


def run_state_path(worktree: Path) -> Path:
    """The run state file of one task worktree (Issue #219).

    It lives in the gitignored `.orbi/` directory, so it never
    dirties the commit boundary (Issue #186) and never reaches the
    delivery commit.
    """
    return worktree / ".orbi" / "run-state.json"


def write_run_state(worktree: Path, *, run_id: str, issue: int,
                    source_repo: str, branch: str) -> None:
    """Write (or refresh) the run state file of one task worktree.

    The file is the explicit "same run" marker (Issue #219): the
    worktree directory name alone is not stable across a repo rename
    (the slug changes), but the state file carries the issue number
    and the repo — the identity the next tick matches on. A resumed
    run refreshes the SAME file (same run id): the file is per-run,
    never per-session.
    """
    state = {
        "run_id": run_id,
        "issue": issue,
        "repo": source_repo,
        "branch": branch,
        "worktree": str(worktree),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = run_state_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def read_run_state(worktree: Path) -> dict | None:
    """Read the run state file; None when absent, fail fast when corrupt.

    A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run, and a wrong continuation is
    worse than a blocked Issue (Issue #219).
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


def changed_files(worktree: Path) -> list[str]:
    """The worktree's uncommitted changes (tracked + untracked paths)."""
    raw = run_command(["git", "status", "--porcelain"], cwd=worktree)
    files: list[str] = []
    for line in raw.splitlines():
        if len(line) > 3 and line[:2].strip():
            files.append(line[3:].strip())
    return files


def resume_context(worktree: Path) -> str | None:
    """The resume context for a continued run (Issue #219), or None.

    A worktree without uncommitted changes and without a previous
    session is a fresh scene: the agent starts from the Issue alone
    (the exact pre-#219 prompt). Otherwise the new session must
    continue the existing work: the context carries the instruction
    to continue (never redo, never discard), the previous session's
    progress and the list of changed files — the agent inspects the
    actual diff itself, it runs inside the worktree.
    """
    files = changed_files(worktree)
    snapshot = activity_snapshot(worktree / ".pi-session")
    if not files and snapshot is None:
        return None
    lines = [
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.",
    ]
    if snapshot is not None:
        lines.append(
            "Previous session progress: "
            f"session={snapshot.get('session_id') or '-'} "
            f"events={snapshot.get('events', 0)} "
            f"phase={snapshot.get('phase') or '-'} "
            f"last_action={snapshot.get('action') or '-'} "
            f"last_result={snapshot.get('result') or '-'}"
        )
    if files:
        lines.append(
            f"Uncommitted changed files ({len(files)}):"
        )
        lines.extend(f"- {path}" for path in files)
    return "\n".join(lines)


def worktree_resume_scene(repo_dir: Path, source_repo: str,
                 number: int) -> tuple[str, Path] | None:
    """Return the resume scene `(run_id, worktree)` for one Issue, or None.

    The worktrees are matched by the RUN STATE FILE (Issue #219), not
    by the directory name alone: the directory name carries the
    source-repo slug, which changes when the repo is renamed, while
    the state file carries the issue number and the repo NAME (the
    part after the slash — stable across a rename). The newest
    matching worktree (by mtime) wins, as before (Issue #18). The
    scene's worktree path may carry the OLD slug (a rename): it is
    the scene the run continues in, never a reason for a second
    worktree.

    A worktree that claims THIS issue number but has a MISSING or
    CORRUPT run state file cannot be verified as the same run: it
    fails fast with the exact reason — never a silent fresh redo
    (Issue #219). A worktree of another issue without a state file
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
    state file, not the directory name alone — Issue #219).
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


def reclaim_released_worktrees(config: RunnerConfig, *,
                               now: datetime | None = None) -> None:
    """Remove the task worktrees of closed Issues past the retention
    window (Issue #760).

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
      matched by run id and by the Issue #48 stop-scene path (an
      external takeover checks out a head branch whose directory name
      is not run-id-derived);
    - the Issue must be closed (ONE batched `gh issue list` per involved
      repo), past `worktree_retain_hours` since its `closedAt`, and free
      of in-flight labels — a human closing an in-flight Issue leaves
      the label, and the scene survives until the delivery path
      resolves it.

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
        if labels & _WORKTREE_INFLIGHT_LABELS:
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
    scan (runner.py:2412): a slot held by another process proves a live
    runner is working, so state it owns is in flight, not orphaned.
    Issue #708 reuses the same rule for the release dispatch — an
    in-progress release found while another runner is live is being
    released right now; only a runner that is alone may resume it.
    Issue #724 reuses it once more for the claim-window yield guard.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return True
    return False


def is_content_only(issue: dict) -> bool:
    """Return True only for the explicit content-only task marker.

    Issue #209 introduced the content agent; #530 renamed its label and
    #537 gave that name to the full-execution ops path — the content
    path dispatches on `ai-content-only` now.
    """
    labels = issue.get("labels", [])
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("name") == CONTENT_ONLY_LABEL
        for label in labels
    )


def is_ops(issue: dict) -> bool:
    """Return True only for the explicit ops task marker (Issue #537).

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


def run_ticket_agent(issue: dict, config: RunnerConfig, source_repo: str,
                     *, progress: Callable[[dict], None] | None = None) -> str:
    """Generate one ticket-only deliverable without using Git state (#209)."""
    started = time.monotonic()
    system_prompt = (
        "You are a ticket-only content agent. Produce the requested final "
        "content as your complete stdout response. Do not create or modify "
        "files, branches, commits, pull requests, tests, or use git/gh tools."
    )
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        "Return only the final content to post on this Issue."
    )
    # Pi's session is transient OS state, not a task worktree or repository
    # artifact. Its output and all terminal evidence are kept on the Issue.
    with tempfile.TemporaryDirectory(prefix="orbi-ticket-") as directory:
        ticket_dir = Path(directory)
        session_dir = ticket_dir / ".pi-session"
        command = [
            "pi", "--no-tools",
            *_skill_args(_skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)),
            *_pi_model_args(config), "--print", "--session-dir", str(session_dir),
            "--system-prompt", system_prompt, context,
        ]
        # Startup phase (Issue #176): the ticket-only session keeps Pi's
        # own agent dir (no per-run materialization) — the provider
        # config is still loaded and resolved before the spawn.
        _log_provider_config_loaded(
            issue_ref=issue_context(source_repo, int(issue["number"])),
            role=ROLE_TICKET, config=config,
            elapsed=time.monotonic() - started,
        )
        return stream_pi(
            command, cwd=ticket_dir,
            log_command=[
                "pi", *_pi_model_args(config), "--print", "--session-dir",
                str(session_dir), "--system-prompt", "<redacted>",
                "<issue-context-redacted>",
            ],
            run_id=config.run_id, issue=int(issue["number"]),
            source_repo=source_repo, branch="-", role=ROLE_TICKET,
            progress=progress,
        )


def process_ticket_only(issue: dict, config: RunnerConfig, source_repo: str) -> str:
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
    set_active_run(number, title, "-", "-")
    try:
        publish(
            action=lambda: publisher.ensure(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id, role=ROLE_TICKET,
                branch="-", worktree=Path("-"), started=started, pr_url=None,
                review_round=0, priority=priority,
            ))),
        )
        output = run_ticket_agent(
            issue, replace(config, run_id=run_id), source_repo,
            progress=LiveProgressThrottle(
                publisher, issue=number, title=title, run_id=run_id,
                role=ROLE_TICKET, branch="-", worktree=Path("-"),
                started=started, pr_url=None, review_round=0, priority=priority,
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
                issue=number, title=title, run_id=run_id, role=ROLE_TICKET,
                branch="-", worktree=Path("-"), started=started, pr_url=None,
                review_round=0, priority=priority,
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


def run_pi(issue: dict, worktree: Path, config: RunnerConfig, source_repo: str,
           *, timeout: int | None = None, branch: str | None = None,
           progress: Callable[[dict], None] | None = None,
           resume_context: str | None = None) -> str:
    """Run the implementer Pi session for a freshly claimed Issue.

    Issue #82 removed the fixer reuse of this function: findings are
    fixed by the review session in the same session, so the implementer
    is the only user of `prompts/prompt.md` now.

    `resume_context` (Issue #219): when the worktree already carries
    the interrupted run's work (uncommitted changes and/or a previous
    session), the context argument gains the resume section so the NEW
    session continues the existing work instead of a fresh redo. The
    prompt template itself is untouched; absent -> the exact
    pre-#219 context.
    """
    # Issue #256: pin the Runner-owned runtime paths in the worktree's
    # local exclude BEFORE Pi starts (covers create, resume and
    # implement) — the tracked .gitignore is the agent's to rename.
    apply_runner_runtime_excludes(worktree)
    # Issue #302: the run artifact dir exists BEFORE the session starts,
    # so the contract commands write `.orbi/plan.md`, `.orbi/test.log`
    # and the coverage artifacts without a mkdir step (a shell redirect
    # into a missing directory fails the command outright).
    (worktree / ".orbi").mkdir(exist_ok=True)
    started = time.monotonic()
    # Issue #527: the repository policy's context files are
    # repository-relative; resolve them against the delivery worktree and
    # enforce existence + the size cap before injection (D2).
    context_files = list(config.context_files)
    for relative in config.repo_context_files:
        context_files.append(validate_context_file(worktree, relative))
    template = config.prompt.read_text(encoding="utf-8")
    prompt_values = {
        "SOURCE_REPO": source_repo,
        "SOURCE_REPOS": ", ".join(config.source_repos),
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": issue.get("body", ""),
        "WORKSPACE_ROOT": str(config.workspace_root),
        "CONTEXT_FILES": "\n".join(str(path) for path in context_files),
        "SKILLS": "\n".join(
            str(path)
            for path in _skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)
        ),
        "BASE_BRANCH": config.base_branch,
        "BASE_SHA": config.base_sha,
        # Issue #527: a repository-declared test command (absent ->
        # the agent follows its own test contract, as before #527).
        "TEST_COMMAND": (
            (config.test_command or "").strip()
            or "(not declared)"
        ),
        "RUN_ID": config.run_id,
        # Issue #186: the implementer prompt no longer carries the
        # base-sync lock (the base fetch is the Runner's operation);
        # the value stays available for custom prompt templates.
        "BASE_SYNC_LOCK": str(base_sync_lock_path(config.repo_dir)),
    }
    # Issue #745: the trusted-comment timeline enters the task context
    # only when the template carries the placeholder — a template
    # without it keeps the exact pre-#745 behavior (no extra GitHub
    # read, no new failure mode).
    if "{{ISSUE_COMMENTS}}" in template:
        prompt_values["ISSUE_COMMENTS"] = trusted_issue_comments_block(
            issue_comments(int(issue["number"]), repo=source_repo),
            config.issue_comments_limit,
        )
    system_prompt = render_prompt(template, prompt_values)
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
    )
    context += "Complete the delivery process in the system prompt."
    if resume_context:
        context += f"\n{resume_context}"
    command = [
        "pi", *_pi_extension_args(config),
        *_skill_args(_skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)),
        *_pi_model_args(config),
        "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt, context,
    ]
    # Issue #157: the provider file (baseUrl / api / apiKey / models)
    # reaches Pi through the materialized per-run agent dir, never
    # through the command line or the log (the redacted command keeps
    # only the #119 provider/model/thinking identifiers). Unconfigured
    # -> the stream_pi call keeps its exact pre-#157 shape.
    agent_dir = prepare_pi_agent_dir(worktree, config)
    # Startup phase (Issue #176): the provider config is loaded and
    # materialized for this run (or resolved to Pi's own agent dir when
    # unconfigured) — the first startup line, before the process is
    # spawned.
    _log_provider_config_loaded(
        issue_ref=issue_context(source_repo, int(issue["number"])),
        role=ROLE_IMPLEMENT, config=config,
        elapsed=time.monotonic() - started,
    )
    extra = {}
    pi_env = _pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    if pi_env:
        extra["pi_env"] = pi_env
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_extension_args(config), *_pi_model_args(config),
            "--print", "--session-dir", str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
        run_id=config.run_id,
        issue=int(issue["number"]),
        source_repo=source_repo,
        branch=branch,
        progress=progress,
        # Issue #228: the configured model_wait dead threshold (the
        # real load_config always provides the key; the module
        # constant stays the fallback for hand-built configs).
        model_wait_dead_seconds=config.model_wait_dead_seconds,
        # Issue #233: the /slots swallow probe (absent URL -> disabled,
        # the exact pre-#233 behavior).
        model_wait_probe_url=config.model_wait_probe_url,
        model_wait_probe_seconds=config.model_wait_probe_seconds,
        **extra,
    )


def _query_open_prs(worktree: Path, branch: str) -> list:
    """Return the task branch's open PRs as the raw `gh pr list` list.

    The ONE PR-query contract shared by verify_pr and freeze_pr
    (Issue #291): a single field set, a single ambiguity-guard limit and
    a single parse. The limit is wide enough that the failure evidence
    lists every ambiguous open PR (the resume audit record, Issue #495);
    the "exactly one" decision needs no tighter bound. A non-array
    payload is a broken `gh` response, never "zero PRs" — fail fast
    instead of guessing.
    """
    raw = run_gh_read_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", (
            "number,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner,body"
        ),
        "--limit", "100",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list):
        raise RuntimeError(
            "gh pr list --json returned a non-array payload "
            "(expected exactly one open PR)"
        )
    return prs


def _single_open_pr(worktree: Path, branch: str, base_branch: str,
                    *, scene: str) -> dict:
    """Return the one open delivery PR of the task branch, base validated.

    The "exactly one open PR + configured base" decision shared by
    verify_pr and freeze_pr (Issue #291); callers add their own extra
    validations on the returned raw PR dict. `scene` names the calling
    path: the same externally-closed-PR failure used to raise the
    identical sentence from both paths and the log could not tell them
    apart.
    """
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


def verify_pr(worktree: Path, branch: str, base_branch: str,
              run_id: str, *, issue: int, repo_dir: Path,
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
    scene, Issue #50: it is logged and passed through — the next review
    session pushes the task branch on the same PR; a diverged head is a
    failure), the run marker in the PR body, and the `Fixes #<issue>`
    keyword in the PR body (Issue #53: GitHub closes the source Issue
    natively only when the body carries the keyword, so a PR without it
    would leave the Issue open after the merge). With `external_pr`
    (Issue #608: the delivery is a takeover of a contributor's own PR)
    the two body checks are skipped — an external PR body carries
    neither the run marker nor a `Fixes` keyword for this Issue; the
    Issue is closed by the Runner after the merge instead. When
    `pr_repo` is given (resume path), the PR's head repo must be that
    repo; when `expected_url` is given, the verified PR URL must exactly
    equal the recovered original PR URL (Issue #45 review: the resume
    must keep the same PR number).
    """
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
        # ref, so it runs under the base-sync lock (Issue #171) with
        # the deployment checkout as the lock location.
        fetch_base_ref(repo_dir, base_branch, cwd=worktree)
        if not _is_ancestor(f"origin/{base_branch}", "HEAD", cwd=worktree):
            event(
                "delivery_behind_base", level=logging.ERROR,
                base_branch=base_branch, branch=branch,
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
        # own exactly-one policy (Issue #291): the FULL open list is the
        # failure audit record (Issue #495) and zero open PRs is
        # classified against the scene PR's state (Issue #494).
        prs = _query_open_prs(worktree, branch)
        if len(prs) != 1:
            # A resume cannot safely select a replacement PR. Query the scene
            # PR separately so zero open PRs (a closed/merged or missing
            # scene PR) have a different outcome from an ambiguous branch
            # (Issue #494).
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
                    raise UnrecoverableDeliveryError(
                        f"resume PR is {scene_state.lower()} and cannot be "
                        f"resumed or replaced: {evidence}; a human must "
                        "decide whether to reopen or create a new delivery"
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
        pr = _single_open_pr(worktree, branch, base_branch, scene="verify_pr")
    url = pr.get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    if pr_repo is not None:
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
    # The non-resume base validation lives in _single_open_pr (Issue #291);
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
        # Issue #50 (the #158 `d13b0c56` scene): the local HEAD may be
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
        event(
            "pr_run_marker_missing", level=logging.ERROR,
            expected=marker, branch=branch,
        )
        raise RuntimeError(
            f"PR body is missing the stable run marker {marker}; the PR "
            "must carry the machine-readable run id of this attempt"
        )
    fixes = f"Fixes #{issue}"
    # Accept GitHub-style `Fixes #N` and the common `Fixes N` variant.
    # The number must match exactly, not as a digit prefix: `Fixes #41`
    # closes Issue 41, not Issue 4 (review F1, Issue #53).
    if not external_pr and not re.search(rf"Fixes #?{issue}(?!\d)", body):
        event(
            "pr_fixes_missing", level=logging.ERROR,
            issue=issue, branch=branch,
        )
        raise RuntimeError(
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


# Runner-owned runtime paths inside a task worktree (Issue #256): created
# by the parent Runner and the Pi session machinery, never by the agent's
# delivery. The task branch's tracked `.gitignore` must NOT be the thing
# that keeps them out of the delivery commit boundary — a task may legally
# rename that file (the #246 brand-rename scene, run `b879a88c`), so the
# Runner pins its own runtime paths in the worktree's LOCAL git exclude
# (`.git/info/exclude`): git metadata that never enters an agent commit and
# never depends on the task branch's content. The #246 rename converged the
# legacy state dir onto `.orbi/`, so the migration window is closed and a
# single pattern covers it.
#
# Issue #302 extends the set with the ORBI CONTRACT ARTIFACTS: the pi-loop
# plugin state (#215) and the per-run plan/test/verify artifacts the
# Runner's own prompt tells the agent to write (pre-#302 at the worktree
# root, now under the excluded `.orbi/` run dir). The four historical
# dirty-gate incidents (#215/#235/#256/#301) were all orbi-owned artifacts
# blocking a finished delivery — the exemption is now the Runner's runtime
# behavior, not a hand-maintained tracked blacklist. Excludes hide only
# untracked paths, so a modified tracked file or a committed artifact
# still fails the gate; coverage command artifacts are NOT in this set —
# the contract commands write them into the excluded `.orbi/` run dir and
# the tracked `.gitignore` stays as the fallback layer.
RUNNER_RUNTIME_EXCLUDES = (
    ".orbi/",
    ".worktrees/",
    ".pi-session/",
    ".pi/",
    "plan.md",
    "test.log",
    "verify.md",
)


def runner_runtime_exclude_path(worktree: Path) -> Path:
    """The task worktree's local git exclude file (`.git/info/exclude`).

    A linked worktree's `.git` is a pointer file (`gitdir: <path>`) that
    resolves to `<common-gitdir>/worktrees/<name>`. Git applies the
    exclude file of the COMMON gitdir to every worktree of the repo (the
    worktree-specific gitdir carries no exclude of its own — verified
    against real git), so the exclude is written to
    `<common-gitdir>/info/exclude`. That is repository-local metadata:
    it never enters an agent commit and never touches the user's global
    excludes (`core.excludesFile`).
    """
    git_entry = worktree / ".git"
    if git_entry.is_file():
        for line in git_entry.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                git_dir = Path(line.split(":", 1)[1].strip())
                # <common-gitdir>/worktrees/<name> -> <common-gitdir>
                common_gitdir = git_dir.parent.parent
                return common_gitdir / "info" / "exclude"
        raise ValueError(
            f"worktree .git pointer {git_entry} has no gitdir entry"
        )
    return git_entry / "info" / "exclude"


def apply_runner_runtime_excludes(worktree: Path) -> None:
    """Idempotently pin the Runner-owned runtime paths in the worktree's
    local git exclude (Issue #256).

    Existing exclude content (including user-written patterns) is
    preserved verbatim; a pattern already present is never written twice.
    Called before every Pi launch (implement, resume, review) and before
    the delivery commit-boundary check. A directory without a `.git`
    entry (unit-test tmp dirs) is a no-op: the delivery commit boundary
    still fails fast on a real corrupted scene.
    """
    git_entry = worktree / ".git"
    if not git_entry.exists():
        event(
            "runner_runtime_exclude_skipped", level=logging.DEBUG,
            worktree=worktree, reason="no .git entry",
        )
        return
    exclude_path = runner_runtime_exclude_path(worktree)
    existing = ""
    if exclude_path.is_file():
        existing = exclude_path.read_text(encoding="utf-8")
    present = {line.strip() for line in existing.splitlines()}
    missing = [p for p in RUNNER_RUNTIME_EXCLUDES if p not in present]
    if not missing:
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    exclude_path.write_text(
        existing + prefix + "\n".join(missing) + "\n", encoding="utf-8",
    )


def _is_runner_runtime_only(status: str) -> bool:
    """True when EVERY non-empty porcelain entry is a Runner-owned runtime
    path (Issue #256): the delivery repair may then continue; any
    agent-owned entry keeps the `delivery_uncommitted_changes` fail fast."""
    entries = [line for line in status.splitlines() if line.strip()]
    if not entries:
        return False
    for line in entries:
        path = line[3:].strip()
        if path.startswith('"') and path.endswith('"'):
            # Porcelain quotes paths with special characters; the runner
            # paths are plain ASCII, so an unquoted match is exact.
            path = path[1:-1]
        if not any(
            path == pattern.strip("/") or path.startswith(pattern)
            for pattern in RUNNER_RUNTIME_EXCLUDES
        ):
            return False
    return True


def cleanup_task_worktree(worktree: Path, repo_dir: Path, *, run_id: str,
                          issue: int) -> None:
    """Remove a terminally failed task's worktree and Runner state
    (Issue #256).

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
        if worktree.is_dir():
            shutil.rmtree(worktree)
        run_command(["git", "worktree", "prune"], cwd=repo_dir)
        event(
            "worktree_cleaned", issue=issue, run_id=run_id,
            worktree=worktree,
        )
    except Exception as exc:
        LOGGER.exception(
            "worktree_cleanup_failed issue=%s run_id=%s worktree=%s: %s",
            issue, run_id, worktree, exc,
        )


def _agent_delivery_boundary(worktree: Path) -> tuple[str, str]:
    """Return the agent's commit boundary as (HEAD, dirty status).

    The Runner-owned runtime paths are pinned in the worktree's LOCAL
    exclude BEFORE the check (Issue #256), so a task that renamed the
    tracked .gitignore (the #246 scene) cannot make the Runner's own
    state look like agent leftovers. Only Runner-owned runtime paths
    that remain (the exclude write raced or the path appeared after it)
    are repaired by re-writing the exclude — deterministic, no git add,
    no deletion, no arbitrary whitelisting. Shared by the dev closeout
    (`deliver_pr`) and the ops closeout (Issue #537).
    """
    apply_runner_runtime_excludes(worktree)
    dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    if dirty and _is_runner_runtime_only(dirty):
        apply_runner_runtime_excludes(worktree)
        event(
            "runner_runtime_exclude_repaired",
            status=" ".join(dirty.splitlines()),
        )
        dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head, dirty


def deliver_pr(worktree: Path, branch: str, base_branch: str,
               base_sha: str, run_id: str, *, issue: int,
               issue_title: str, repo_dir: Path,
               source_repo: str) -> str | None:
    """The Runner completes the deterministic delivery closeout.

    Issue #186: the Agent stops at the committed delivery (code, tests,
    commit on the task branch). Everything after is deterministic and
    owned by the Runner — the Agent no longer fetches the base, merges
    it, pushes or creates the PR:

    1. commit boundary: the worktree is clean and HEAD advanced past
       the frozen base — the Runner never commits uncommitted changes
       or expands the Agent's commit boundary (fail fast);
    2. base freshness: fetch under the base-sync lock (Issue #171); when
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

    Issue #746: a human may close the Issue while the delivery is in
    flight (labels/state only affect the next scan, so the in-flight
    session correctly keeps running). The Issue state is read directly
    right before the PR creation; a CLOSED Issue returns None — the
    pushed branch keeps the work, the closed Issue gets one explanatory
    comment, and no PR is opened (nothing dangles behind a closed
    Issue). Returns the PR URL, or None when the delivery stopped
    because the Issue was closed.
    """
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    # Commit boundary (Issue #186 + #256): the Agent's delivery is the
    # committed worktree state.
    local_head, dirty = _agent_delivery_boundary(worktree)
    if dirty:
        event(
            "delivery_uncommitted_changes", level=logging.ERROR,
            branch=branch, status=" ".join(dirty.splitlines()),
        )
        raise RuntimeError(
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
    # Base freshness (Issue #171): the fetch updates the shared
    # remote-tracking ref, so it runs under the base-sync lock with the
    # deployment checkout as the lock location. A lock timeout or a
    # fetch error fails fast — no retry, no lock bypass.
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    if not _is_ancestor(f"origin/{base_branch}", "HEAD", cwd=worktree):
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
    # merge advanced it to the merge commit.
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    run_git_network_command(
        ["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree,
    )
    remote_head = run_command(
        ["git", "rev-parse", f"origin/{branch}"], cwd=worktree,
    )
    if remote_head != local_head:
        event(
            "remote_head_mismatch", level=logging.ERROR,
            expected=local_head, actual=remote_head, branch=branch,
        )
        raise RuntimeError(
            f"remote head {remote_head} does not match the local head "
            f"{local_head} after push origin {branch}"
        )
    # Issue #746: the closed-Issue guard runs AFTER the push (the work
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
    # body contract is the Runner's obligation now, Issue #186) and
    # verify it with the full PR contract (exactly one open PR, base,
    # head, run marker, `Fixes #<issue>`, URL). The verify step skips
    # its own base re-fetch: this function just fetched and merged it.
    raw = run_gh_read_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "url",
    ], cwd=worktree)
    if not json.loads(raw):
        body = (
            f"{run_marker(run_id)}\n\n"
            f"Fixes #{issue}\n\n"
            f"{issue_title} (run_id={run_id})\n"
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
        worktree, branch, base_branch, run_id, issue=issue,
        repo_dir=repo_dir, require_latest_base=False,
    )


def verify_resumed_pr(scene: dict, issue: dict, config: RunnerConfig,
                      source_repo: str) -> str:
    """Verify the PR of a resumed delivery BEFORE any git/Pi mutation.

    Issue #89: #82 removed the cold-start fixer together with the
    pre-Pi `verify_pr` of the old `resume_delivery` — the resume passed
    the comment's PR URL straight to the delivery wait while the review
    froze the PR derived from the run id, so the two lines could be
    different PRs (a comment must never steer the runner into the wrong
    PR, Issue #45). Restored: branch and worktree are DERIVED from the
    configured repo_dir, source repo, Issue number and run id (never
    read from the comment), the scene base must still equal the
    configured base (Issue #91) and the worktree must exist (Issue #90)
    — both checked BEFORE any command runs — and the existing
    `verify_pr` then validates exactly one open PR of the derived
    branch in the configured source repo, on the configured base,
    carrying the run marker and the `Fixes` keyword, with the EXACT URL
    of the recovered scene. `require_latest_base=False`: being behind
    the latest base is the expected state the review session absorbs
    in-session (Issue #82), so the base merge never returns to the
    runner. The returned URL is the one verify_pr verified — the
    delivery wait only ever sees verified URLs, never the comment
    string.

    A failure is classified (Issue #50): a RECOVERABLE failure
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
    # Issue #608: an external takeover scene delivers the contributor's own
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
            # Issue #91 + #50: a base-branch change is a human
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
            # Issue #90 + #50: a missing worktree is a RECOVERABLE
            # failure (the branch still exists on the remote and the
            # worktree can be recreated on the next resume), so the
            # handler below keeps the Issue in the automatic fix loop.
            raise RuntimeError(f"worktree missing: {worktree}")
        if external:
            branch = run_command(
                ["git", "branch", "--show-current"], cwd=worktree,
            )
        verified_url = verify_pr(
            worktree, branch, config.base_branch, run_id,
            issue=number, repo_dir=config.repo_dir,
            pr_repo=source_repo,
            expected_url=scene["pr_url"], require_latest_base=False,
            external_pr=external,
        )
        # Issue #178: the resumed delivery is in flight from here on —
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
        LOGGER.exception(
            "issue=%s resume_pr_verification_failed pr=%s branch=%s",
            number, scene["pr_url"], branch,
        )
        try:
            # The shared classified reporter (Issue #288): recoverable
            # -> `ai-fix-needed` with the full scene on Issue AND PR,
            # unrecoverable -> `ai-blocked` ALONE — the PR, branch and
            # worktree stay intact either way. `run_id` is the id the
            # tick BOUND (Issue #41): without it the comment carries no
            # marker and the progress publishing is skipped, whatever
            # the recovered scene says.
            report_delivery_failure(
                exc, issue=issue, source_repo=source_repo,
                run_id=current_run_id(), pr_url=scene["pr_url"],
                worktree=worktree, branch=branch, role=ROLE_REVIEW,
                cause=(
                    f"the resume verification of PR {scene['pr_url']} "
                    f"failed: {_failure_detail(exc)}"
                ),
                blocked_suffix=(
                    f"; the PR, branch {branch} and worktree {worktree} "
                    "are preserved"
                ),
            )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def _is_code_fence_line(line: str) -> bool:
    """True when a stripped line is only a Markdown code fence.

    Reviewers commonly wrap the machine-readable verdict in a fence
    (```` ``` ````, ```` ```json ```` or `~~~`); a fence line carries no
    review content, so the tail scan skips it without relaxing Issue #591.
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

    Issue #774: wrapper noise around a verdict payload — a leading text
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
    """The Issue #591 semantic checks every verdict payload must pass."""
    if parsed.get("verdict") not in ("pass", "findings"):
        raise ValueError("verdict must be 'pass' or 'findings'")
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
    if parsed["verdict"] == "findings" and blockers == 0 and majors == 0:
        raise ValueError("findings verdict requires blockers or majors")
    return parsed


def parse_review_verdict(text: str) -> dict:
    """Extract the REVIEW_VERDICT JSON from a review session's output.

    The output is scanned BACKWARDS (Issue #774): the verdict may be the
    last line, wrapped in the reviewer's natural-language phrasing
    (leading CJK prefix, inline-code backticks, trailing punctuation —
    the orbi-cloud#287 scene), or followed by trailing prose. The
    semantics do not relax with the shape (Issue #591): a line that only
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
    for line in reversed(lines):
        stripped = line.strip()
        marked = stripped.startswith(VERDICT_MARKER)
        if not marked and VERDICT_MARKER in stripped:
            continue  # a marker mention, never a verdict (Issue #591)
        parsed = _json_dict_span(
            stripped[len(VERDICT_MARKER):] if marked else stripped)
        if marked:
            # The explicit verdict channel: a malformed payload fails
            # fast, it is never silently skipped.
            if parsed is None:
                raise ValueError("malformed REVIEW_VERDICT JSON")
            candidates.append(_validated_verdict(parsed))
        elif parsed is not None:
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


def freeze_pr(worktree: Path, branch: str, base_branch: str) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    pr = _single_open_pr(worktree, branch, base_branch, scene="freeze_pr")
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": pr.get("baseRefName"),
        "base_oid": pr["baseRefOid"],
        "head_ref": pr["headRefName"],
        "head_oid": pr["headRefOid"],
    }


# Role-specific skill filtering (Issue #83): the review session ends
# with a single REVIEW_VERDICT line and its job is to review this one
# diff and fix it until it can merge (Issue #82) — not to open another
# full delivery — so the delivery-oriented skills must not be loaded
# there (tdd-dev would steer it into the implement/test/PR flow,
# review-fix-loop would open another fix/review round). The
# implementer keeps tdd-dev and code-review but not review-fix-loop:
# the Runner itself runs the independent review loop once the PR is
# open.
REVIEW_EXCLUDED_SKILLS = frozenset({"tdd-dev", "review-fix-loop"})
IMPLEMENT_EXCLUDED_SKILLS = frozenset({"review-fix-loop"})


def _skill_name(entry: str | Path) -> str:
    """Return the skill name of one configured skill entry.

    Entries point at the SKILL.md file inside the skill directory
    (e.g. .../skills/tdd-dev/SKILL.md); the skill name is the parent
    directory. A bare markdown entry (e.g. my-skill.md) or a skill
    directory is named after its own stem.
    """
    path = Path(entry)
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def _skills_for(config: RunnerConfig, excluded: frozenset[str]) -> list[str | Path]:
    """Return one role's configured skills, dropping excluded names."""
    return [
        skill for skill in config.skills
        if _skill_name(skill) not in excluded
    ]


def _skill_args(skills: list[str | Path]) -> list[str]:
    """Return the --skill command args for one role's skill list."""
    return [
        item for skill in skills
        for item in ("--skill", str(skill))
    ]


def run_review(worktree: Path, pr: dict, config: RunnerConfig, source_repo: str,
               issue: int, branch: str, round: int,
               timeout: int | None = None,
               progress: Callable[[dict], None] | None = None) -> str:
    """Run one independent review session for a frozen PR.

    The session is independent (new process, `prompts/prompt_review.md`, a new
    session JSONL) and reviews the exact frozen base/head. Issue #82:
    when it finds Blocker/Major issues it fixes them IN THIS SAME
    SESSION (modify code, run the full test suite with coverage, commit
    and push the task branch) and re-emits the final verdict — there is
    no cold-start fixer and no third review. The review streams live
    activity through the same pipeline as the implementer (role=review;
    Issue #41: one run_id end to end, the roles are steps of the same
    run).
    """
    # Issue #256: the review/fix session gets the SAME local-exclude
    # preflight as the implementer (one idempotent helper, Pi 前).
    apply_runner_runtime_excludes(worktree)
    # Issue #302: same run-dir guarantee as the implementer — the
    # review session reads/writes the same `.orbi/` artifacts.
    (worktree / ".orbi").mkdir(exist_ok=True)
    started = time.monotonic()
    review_template = config.prompt_review.read_text(encoding="utf-8")
    review_values = {
        "SOURCE_REPO": source_repo,
        "PR_NUMBER": str(pr["number"]),
        "PR_URL": pr["url"],
        "BASE_BRANCH": config.base_branch,
        "BASE_SHA": pr["base_oid"],
        "HEAD_SHA": pr["head_oid"],
        "HEAD_REF": pr["head_ref"],
        "ROUND": str(round),
        # Issue #171: the SAME shared base-sync lock as the
        # implementer — the review session's base-absorb fetch must
        # run under it (flock <lock> git fetch origin <base>).
        "BASE_SYNC_LOCK": str(base_sync_lock_path(config.repo_dir)),
    }
    # Issue #745: the review path sees the Issue's decision evolution
    # too — same trusted timeline, same placeholder gate (a template
    # without it keeps the exact pre-#745 behavior).
    if "{{ISSUE_COMMENTS}}" in review_template:
        review_values["ISSUE_COMMENTS"] = trusted_issue_comments_block(
            issue_comments(issue, repo=source_repo),
            config.issue_comments_limit,
        )
    system_prompt = render_prompt(review_template, review_values)
    context = (
        f"Independently review PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} against base {config.base_branch}@{pr['base_oid']} "
        f"and head {pr['head_oid']} (round {round}). Follow code-review R1-R9; "
        "fix Blocker/Major findings in this same session (push only the "
        "task branch) and end with a single REVIEW_VERDICT line carrying "
        "the head it covers."
    )
    command = [
        "pi", *_pi_extension_args(config),
        *_skill_args(_skills_for(config, REVIEW_EXCLUDED_SKILLS)),
        *_pi_model_args(config),
        "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt,
        context,
    ]
    # Issue #157: the review session uses the SAME provider config as
    # the implementer (one materialized dir per worktree, re-used).
    agent_dir = prepare_pi_agent_dir(worktree, config)
    # Startup phase (Issue #176): the review session's provider config
    # is loaded and materialized too (same line shape, role=review).
    _log_provider_config_loaded(
        issue_ref=issue_context(source_repo, issue),
        role=ROLE_REVIEW, config=config,
        elapsed=time.monotonic() - started,
    )
    extra = {}
    pi_env = _pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    if pi_env:
        extra["pi_env"] = pi_env
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_extension_args(config), *_pi_model_args(config),
            "--print", "--session-dir", str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<review-context-redacted>",
        ],
        run_id=config.run_id,
        issue=issue,
        source_repo=source_repo,
        branch=branch,
        role=ROLE_REVIEW,
        progress=progress,
        # Issue #228: the review session uses the SAME configured
        # model_wait dead threshold as the implementer (the real
        # load_config always provides the key; the module constant
        # stays the fallback for hand-built configs).
        model_wait_dead_seconds=config.model_wait_dead_seconds,
        # Issue #233: the review session uses the SAME /slots swallow
        # probe as the implementer (absent URL -> disabled).
        model_wait_probe_url=config.model_wait_probe_url,
        model_wait_probe_seconds=config.model_wait_probe_seconds,
        **extra,
    )


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


def check_review_ci(repo: str, commit: str, *, wait_seconds: float) -> str:
    """Gate a clean review on the PR head's current GitHub check runs.

    Review-local tests are advisory; the repository's own CI is the only
    acceptance gate. An absent check list is intentionally fail-open, matching
    the release gate, while pending checks are polled with the shared release
    wait configuration and cadence.
    """
    def fetch() -> list[dict]:
        return commit_check_runs(repo, commit)

    waited = 0.0
    check_runs = fetch()
    while True:
        pending = [
            f"check '{check.get('name')}' is {check.get('status')}/"
            f"{check.get('conclusion')}"
            for check in check_runs if check.get("status") != "completed"
        ]
        if not pending:
            break
        detail = ", ".join(pending)
        event(
            "review_waiting_ci", head=commit, pending=detail,
            waited=f"{int(waited)}s", limit=f"{int(wait_seconds)}s",
        )
        if waited >= wait_seconds:
            raise RuntimeError(
                f"review gate: waiting for CI on PR head {commit} timed out "
                f"after {int(wait_seconds)}s (still pending: {detail})"
            )
        step = min(RELEASE_CI_POLL_INTERVAL, wait_seconds - waited)
        time.sleep(step)
        waited += step
        check_runs = fetch()

    failed = [
        check for check in check_runs
        if check.get("conclusion") not in ("success", "neutral", "skipped")
    ]
    if failed:
        check = failed[0]
        reference = check.get("html_url") or check.get("details_url") or "no run URL"
        raise RuntimeError(
            f"review gate: CI check '{check.get('name')}' failed on PR head "
            f"{commit} ({reference})"
        )
    if not check_runs:
        evidence = f"CI on review head {commit}: no check runs (nothing to gate)"
    else:
        evidence = (
            f"CI on review head {commit}: {len(check_runs)} check(s) all "
            "success/neutral/skipped"
        )
    LOGGER.info("%s", evidence)
    return evidence


def merge_gate(worktree: Path, pr: dict, base_branch: str,
               *, repo_dir: Path, ci_wait_seconds: float | None = None,
               mergeable_wait_seconds: float | None = None,
               source_repo: str | None = None) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, the remote head to still be the reviewed head, and the
    exact head's GitHub CI checks to be completed successfully. Pending checks
    are polled with a deadline; failures and timeouts prevent merging. Then
    merge with `--match-head-commit` so only that exact head can land. No force
    push, no direct push of the protected branch. The base fetch updates the
    shared remote-tracking ref, so it runs under the base-sync lock
    (Issue #171) with the deployment checkout as the lock location.
    """
    ci_wait_seconds = (ci_wait_seconds if ci_wait_seconds is not None else
                       pr.get("_ci_wait_seconds", RELEASE_CI_WAIT_SECONDS))
    mergeable_wait_seconds = (
        mergeable_wait_seconds if mergeable_wait_seconds is not None else
        pr.get("_mergeable_wait_seconds", MERGEABLE_WAIT_SECONDS)
    )
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    if not _is_ancestor(f"origin/{base_branch}", pr["head_oid"], cwd=worktree):
        event(
            "merge_gate_behind_base", level=logging.ERROR,
            base_branch=base_branch, pr=pr["number"], head=pr["head_oid"],
        )
        raise RecoverableMergeGateError(
            f"PR #{pr['number']} head {pr['head_oid']} is behind latest "
            f"remote base origin/{base_branch}; absorb the latest base, rerun "
            "tests and review, then retry"
        )
    def fetch_state() -> dict:
        return pr_view(pr["number"],
                       "state,mergeable,headRefOid,statusCheckRollup",
                       cwd=worktree)

    def check_rollup(state: dict) -> tuple[list[str], list[str]]:
        rollup = state.get("statusCheckRollup") or []
        pending: list[str] = []
        failed: list[str] = []
        for check in rollup:
            status = str(check.get("status", check.get("state", ""))).upper()
            conclusion = str(check.get("conclusion", "")).upper()
            name = check.get("name", check.get("context", "check"))
            if status not in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR"):
                pending.append(f"check '{name}' is {status or 'UNKNOWN'}")
            elif status == "COMPLETED" and conclusion not in (
                "SUCCESS", "NEUTRAL", "SKIPPED",
            ):
                failed.append(f"check '{name}' is {status}/{conclusion}")
            elif status in ("FAILURE", "ERROR"):
                failed.append(f"check '{name}' is {status}")
        return pending, failed

    waited = 0.0
    state = fetch_state()
    while True:
        pending, failed = check_rollup(state)
        if failed:
            failed_names = [item.split(chr(39))[1] for item in failed]
            _raise_if_preexisting_ci_failure(
                pr.get("_source_repo", source_repo or ""), failed_names,
                pr.get("base_oid"),
            )
            raise RuntimeError(
                f"delivery gate: CI check '{failed_names[0]}' "
                f"failed on PR #{pr['number']}: " + ", ".join(failed)
            )
        if not pending:
            break
        detail = ", ".join(pending)
        event(
            "merge_gate_waiting_ci", pr=pr["number"], pending=detail,
            waited=f"{int(waited)}s", limit=f"{int(ci_wait_seconds)}s",
        )
        if waited >= ci_wait_seconds:
            raise RuntimeError(
                f"delivery gate: waiting for CI on PR #{pr['number']} timed "
                f"out after {int(ci_wait_seconds)}s (still pending: {detail})"
            )
        step = min(RELEASE_CI_POLL_INTERVAL, ci_wait_seconds - waited)
        time.sleep(step)
        waited += step
        state = fetch_state()

    mergeable_waited = 0.0
    while state.get("mergeable") == "UNKNOWN":
        event(
            "merge_gate_waiting_mergeable", pr=pr["number"],
            waited=f"{int(mergeable_waited)}s",
            limit=f"{int(mergeable_wait_seconds)}s",
        )
        if mergeable_waited >= mergeable_wait_seconds:
            raise RecoverableMergeGateError(
                f"PR #{pr['number']} not mergeable: mergeable state timed "
                f"out after {int(mergeable_wait_seconds)}s"
            )
        step = min(MERGEABLE_POLL_INTERVAL,
                   mergeable_wait_seconds - mergeable_waited)
        time.sleep(step)
        mergeable_waited += step
        state = fetch_state()

    mergeable = state.get("mergeable")
    if mergeable != "MERGEABLE":
        event(
            "merge_gate_not_mergeable", level=logging.ERROR,
            pr=pr["number"], mergeable=mergeable,
        )
        raise RecoverableMergeGateError(
            f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
            "resolve conflicts and retry"
        )
    remote_head = state.get("headRefOid")
    if remote_head != pr["head_oid"]:
        event(
            "merge_gate_head_moved", level=logging.ERROR,
            pr=pr["number"], reviewed=pr["head_oid"], remote=remote_head,
        )
        raise RuntimeError(
            f"PR #{pr['number']} head moved since review "
            f"(reviewed={pr['head_oid']} remote={remote_head}); re-review "
            "before merging"
        )
    run_command([
        "gh", "pr", "merge", str(pr["number"]),
        "--match-head-commit", pr["head_oid"], "--merge",
    ], cwd=worktree)
    event("merged", pr=pr["number"], head=pr["head_oid"])
    return {**pr, "merged": True}


def confirm_merged(worktree: Path, pr: dict, base_branch: str,
                   *, repo_dir: Path) -> dict:
    """Confirm the PR is MERGED and origin/<base> contains the merge commit.

    The base fetch updates the shared remote-tracking ref, so it runs
    under the base-sync lock (Issue #171) with the deployment checkout
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
    whether the recovered review runs (Issue #79).
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
# Startup source freshness (Issue #525): the 2026-09-07 incident — the
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
    """The installed ``orbi`` distribution version (install metadata,
    not the code's self-reported ``__version__``). Test seam: the
    non-editable form monkeypatches this module global."""
    import importlib.metadata
    return importlib.metadata.version("orbi")


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


def check_runner_source_freshness(config: RunnerConfig, *, run_command) -> dict:
    """Startup invariant (Issue #525): prove that the code THIS process
    executes is the configured engine source channel's exact commit
    BEFORE any slot or claim (Issue #535: the channel is the
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
            # closed (Issue #535: an unverifiable source never runs).
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

    Issue #149: the whole sync runs under the short-lived base-sync
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


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: RunnerConfig, source_repo: str,
                              number: int, title: str, priority: str) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    `title` is the issue's GitHub title (Issue #100): the review
    progress scenes (ensure, findings, merged) show `#<number> <title>`
    like every other scene; it is required, never fabricated.

    The delivery wait loop (which holds the slot) calls this while the
    PR is open and the Issue awaits review (`ai-pr-opened`) or awaits the
    next review session (`ai-fix-needed`). It freezes the PR, runs the
    independent review (streamed, role=review), and then:

    - clean verdict -> the reviewer may have fixed findings IN THE SAME
      SESSION and pushed the task branch (Issue #82), so the PR is
      RE-FROZEN before the merge gate: the gate (latest-base ancestor,
      mergeable, head match, `gh pr merge --match-head-commit`) then
      runs against the head the verdict actually covers; confirm the
      merge landed on origin/<base>, sync the deployment checkout,
      label the Issue `ai-merged`; returns True;
    - Blocker/Major findings the reviewer could not fix in-session ->
      comment them to Issue and PR and label the Issue `ai-fix-needed`;
      the next wait iteration (or the next tick after a restart) runs
      the same independent review again — no cold-start fixer, no
      third review; returns False;
    - a gate failure because the head is behind the latest base or has
      a merge conflict -> label the Issue `ai-fix-needed` with the
      absorb-base finding (the next review session absorbs the latest
      base in-session); returns False;
    - missing/malformed verdict (including a verdict whose `head` does
      not match the PR head, Issue #591) -> raise; the caller keeps the
      Issue in the automatic fix loop (`ai-fix-needed`, Issue #50: the
      next review session re-runs the same review on the same PR);
    - an exhausted round budget -> raise `UnrecoverableDeliveryError`
      (Issue #50: the bounded loop is a human decision, not a
      recoverable failure); the caller marks the Issue `ai-blocked`
      with the explicit reason.
    """
    marker = run_marker(config.run_id)
    comments = issue_comments(number, repo=source_repo)
    # The run marker is the delivery-attempt boundary. Do not count review
    # comments from a previous PR/run on the same Issue (Issue #508).
    rounds = review_rounds_so_far(comments, run_id=config.run_id)
    recovery_at = None
    if rounds >= MAX_REVIEW_ROUNDS:
        # Issue #483: a maintainer may repair an external prerequisite and
        # explicitly move the terminal Issue back to ai-fix-needed. That
        # transition establishes a new budget for this same PR; old review
        # comments remain immutable evidence and are not counted again.
        recovery_at = human_review_recovery_at(number, source_repo)
        if recovery_at is not None:
            rounds = review_rounds_so_far(
                comments, after=recovery_at, run_id=config.run_id,
            )
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
            # Issue #50: the loop is bounded by MAX_REVIEW_ROUNDS on purpose
            # — after 5 rounds without a clean verdict the remaining findings
            # need a human decision, so the AI cannot safely continue this PR.
            raise ReviewRoundsExhausted(
                f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
                "without a clean verdict; the bounded loop is a human "
                "decision, so the AI cannot safely continue this PR"
            )
    round = rounds + 1
    pr = freeze_pr(worktree, branch, base_branch)
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
    # Issue #79: ensure is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    publish(
        action=lambda: publisher.ensure(_progress_body(_progress_state(
            issue=number, title=title, run_id=config.run_id,
            role=ROLE_REVIEW, branch=branch, worktree=worktree,
            started=started, pr_url=pr["url"], review_round=round,
            priority=priority,
        ))),
    )
    output = run_review(
        worktree, pr, config, source_repo, number, branch, round,
        progress=LiveProgressThrottle(
            publisher, issue=number, title=title,
            run_id=config.run_id, role=ROLE_REVIEW, branch=branch,
            worktree=worktree, started=started, pr_url=pr["url"],
            review_round=round, priority=priority,
        ),
    )
    verdict = parse_review_verdict(output)
    event(
        "review", pr=pr["number"], round=round,
        verdict=verdict["verdict"], blockers=verdict["blockers"],
        majors=verdict["majors"],
    )
    if review_has_findings(verdict):
        # The reviewer could not make the PR mergeable in this session
        # (Issue #82: findings are fixed in the same session; reaching
        # this branch means the fix was not verifiable or not this
        # session's to decide). The Issue moves to the explicit
        # fix-needed state: the next review session retries the same PR
        # (no cold-start fixer), and the round budget bounds the loop.
        event(
            "review_findings_unfixed", pr=pr["number"], round=round,
        )
        body = (
            f"{marker}\n"
            f"Orbi review round {round} for PR #{pr['number']}: "
            f"{verdict['blockers']} blocker(s), {verdict['majors']} "
            "major(s). Findings: "
            + json.dumps(verdict["findings"], ensure_ascii=False)
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], repo=source_repo, body=body)
        # Issue #79: the findings publishing is bypass — a 404 here
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
                    issue=number, title=title, run_id=config.run_id,
                    role=ROLE_REVIEW, branch=branch,
                    worktree=worktree, started=started,
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
    # Issue #82: the reviewer fixes findings in the same session and
    # pushes the task branch, so the head the verdict covers may be
    # NEWER than the frozen head. Re-freeze before the merge gate: the
    # gate then checks the latest-base ancestor, mergeability and the
    # exact reviewed head against the current remote head, and merges
    # only that head via --match-head-commit.
    refrozen = freeze_pr(worktree, branch, base_branch)
    if refrozen["head_oid"] != pr["head_oid"]:
        event(
            "review_head_advanced", pr=pr["number"], round=round,
            frozen=pr["head_oid"], reviewed=refrozen["head_oid"],
        )
    # Issue #591: the clean verdict is bound to the head it covers. The
    # gate below merges exactly `refrozen["head_oid"]`, so a verdict
    # naming any other head (forged by injected text, replayed from an
    # older round, or stale after a fix the reviewer forgot to state)
    # never merges: it is a malformed verdict — the recoverable loop
    # re-reviews the same PR.
    if verdict["head"] != refrozen["head_oid"]:
        raise ValueError(
            f"review verdict head {verdict['head']} does not match the "
            f"PR head {refrozen['head_oid']}; the merge gate only merges "
            "the head the verdict covers"
        )
    def handle_gate_failure(message: str, *, ci_failure: bool) -> None:
        body = (
            f"{marker}\n"
            # Both gate-failure scenes carry the counted `Orbi review
            # round` prefix (Issue #588): it is the only carrier
            # `review_rounds_so_far` counts, so a persistently red CI
            # must consume the budget and exhaust into the bounded
            # human decision instead of looping forever.
            + (f"Orbi review round {round} for PR #{pr['number']}: "
               "CI merge gate blocked: "
               f"{message} (run_id={config.run_id})" if ci_failure else
               f"Orbi review round {round} for PR #{pr['number']}: "
            "the PR is behind the latest base or has a merge conflict; "
            f"the next review session merges the latest "
            f"origin/{base_branch} into the branch in-session, resolves "
            "conflicts, and reruns the full test suite"
        ))
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

    try:
        ci_evidence = check_review_ci(
            source_repo, refrozen["head_oid"],
            wait_seconds=config.release_ci_wait_seconds,
        )
        event(
            "review_ci_gate_passed", pr=refrozen["number"],
            head=refrozen["head_oid"], evidence=ci_evidence,
        )
    except RuntimeError as exc:
        handle_gate_failure(str(exc), ci_failure=True)
        return False
    try:
        merged = merge_gate(
            worktree,
            {**refrozen, "_source_repo": source_repo,
             "_ci_wait_seconds": config.release_ci_wait_seconds,
             "_mergeable_wait_seconds": config.mergeable_wait_seconds},
            base_branch, repo_dir=config.repo_dir,
        )
    except RecoverableMergeGateError as exc:
        handle_gate_failure(str(exc), ci_failure=False)
        return False
    except RuntimeError as exc:
        # CI failures retain their existing recoverable path. All other
        # unclassified gate failures (including a moved head) fail fast.
        message = str(exc)
        if "delivery gate: CI" not in message:
            raise
        handle_gate_failure(message, ci_failure=True)
        return False
    confirmed = confirm_merged(
        worktree, merged, base_branch, repo_dir=config.repo_dir,
    )
    # Issue #79: the merged publishing is bypass — the GitHub merge
    # already landed; a 404 here must not stop the `ai-merged`
    # transition and the merged PR scene comment below.
    publish(
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round})"
        ),
    )
    publish(
        action=lambda: publisher.finish(_progress_body(
            _progress_state(
                issue=number, title=title, run_id=config.run_id,
                role=ROLE_REVIEW, branch=branch,
                worktree=worktree, started=started,
                pr_url=merged["url"], review_round=round,
                priority=priority,
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
    # have both `ai-in-progress` and `ai-fix-needed` (Issue #423/#330).
    # The merged patch clears every delivery-state label actually present.
    apply_label_patch(
        number, repo=source_repo, event=EVENT_MERGED,
        current_labels=issue_labels(number, source_repo),
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{marker}\n"
            f"Orbi merged PR: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"base_branch={base_branch} run_id={config.run_id})"
        ),
    )
    try:
        # A config built by load_config always carries both keys (the
        # deploy home defaults to the repo dir); a hand-built legacy
        # dict without them keeps the pre-#535 sync behavior.
        if (
            config.engine_source_track is not None
            and config.deploy_home is not None
            and config.repo_dir == config.deploy_home
            and config.engine_source_track != "main"
        ):
            # Issue #535: the delivery checkout IS the engine source in
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
    marker and the runner fingerprint like its Issue twin (Issue #526).
    """
    run_command([
        "gh", "pr", "comment", str(number), "--repo", repo,
        "--body", format_status_comment(body),
    ])


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

    The checklist's classification input (Issue #763). A git failure
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
    worktree: Path, config: RunnerConfig, *, run_id: str, pr_url: str,
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
        test_command=config.test_command,
        checklist=human_review.build_checklist(
            test_result=read_test_result(worktree),
            changed_files=delivered_changed_files(worktree, base),
            test_command=config.test_command,
        ),
    )


def _human_review_column2(worktree: Path, config: RunnerConfig) -> list[str]:
    """Recompute the checklist's column 2 from local delivery evidence.

    The review-round gate's per-tick cost: local file reads only — no
    session, no GitHub write. Missing evidence lands IN column 2 (the
    gate holds), so only real evidence can pass it.
    """
    base = config.base_sha or config.base_branch
    return human_review.build_checklist(
        test_result=read_test_result(worktree),
        changed_files=delivered_changed_files(worktree, base),
        test_command=config.test_command,
    )["column2"]


def _publish_plan_milestone(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the `plan ready` milestone once the worktree has the plan
    artifact (Issue #302: `.orbi/plan.md`, the excluded run dir)."""
    if (worktree / ".orbi" / "plan.md").is_file():
        publisher.milestone("plan ready")


def _publish_test_milestone(publisher: ProgressPublisher,
                            worktree: Path) -> None:
    """Post `tests passed` / `tests failed` from the worktree's test.log.

    The failure check is case-insensitive: pytest evidence lines carry
    uppercase markers (`FAILED`, `FAILURES`) that a lowercase-only check
    would misreport as a pass (review round 2, PR #42).
    """
    result = read_test_result(worktree)
    if result is None:
        return
    lowered = result.lower()
    if "fail" in lowered or "error" in lowered:
        publisher.milestone(f"tests failed: {result}")
    else:
        publisher.milestone(f"tests passed: {result}")


def _failure_detail(exc: BaseException) -> str:
    """One-line failure description; keeps bounded subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()[:1000]}"
    return detail


def _tail_text(path: Path, *, lines: int = 20, chars: int = 4000) -> str:
    """Read a bounded tail for failure evidence without blocking cleanup."""
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
    """One raw-output segment inside a CommonMark code fence (Issue #775).

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


# The number of session records a failure comment summarizes (Issue #775).
SESSION_SUMMARY_LIMIT = 20


def _session_summary(session_file: Path,
                     *, limit: int = SESSION_SUMMARY_LIMIT) -> str:
    """Structured summary of the last session records (Issue #775).

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


def _failure_evidence(worktree: Path | None, exc: BaseException) -> str:
    """Render the bounded evidence that survives terminal worktree cleanup.

    Pi subprocess streams come from ``CalledProcessError``. The session and
    test log are read before cleanup and only bounded, fenced segments are
    copied into the failure comment: raw output stays literal on GitHub,
    the session tail is a structural summary whose duplicates are removed,
    and the full session log is named for the deep-dive (Issue #775). The
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
    test_log = "<unavailable>"
    if worktree is not None:
        test_path = worktree / ".orbi" / "test.log"
        if test_path.is_file():
            test_log = _tail_text(test_path)
    return (
        "\n\nFailure evidence (captured before cleanup):\n"
        f"exit_code={return_code if return_code is not None else '<unknown>'}\n"
        f"\nstderr_tail:\n{_fenced(stderr)}\n"
        f"\nstdout_tail:\n{_fenced(stdout)}\n"
        "\nsession_last_events "
        f"(last {SESSION_SUMMARY_LIMIT} records; full log: "
        f"{session_file or '<unavailable>'}):\n{_fenced(session)}\n"
        f"\ntest_log_tail:\n{_fenced(test_log)}"
    )


def _live_progress(publisher: ProgressPublisher, *, issue: int,
                   title: str, run_id: str, role: str, branch: str,
                   worktree: Path, started: float, pr_url: str | None,
                   review_round: int, priority: str,
                   activity: dict | None = None) -> None:
    """One live GitHub progress update while a Pi session is running.

    Called from the `stream_pi` poll loop (every activity change or
    heartbeat, Issue #18): the same run-marker comment is PATCHed in
    place at most every `PI_HEARTBEAT_SECONDS` or when the visible
    activity changed. `activity` is the watcher state of that poll, so
    the live comment shows the session exactly as the journal reports
    it. The publisher already knows the comment id after `ensure`, so a
    callback before it would fail fast here — and the wiring always
    ensures first.
    """
    state = _progress_state(
        issue=issue, title=title, run_id=run_id, role=role,
        branch=branch, worktree=worktree, started=started,
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

    def __init__(self, publisher: ProgressPublisher, *, issue: int,
                 title: str, run_id: str, role: str, branch: str,
                 worktree: Path, started: float, pr_url: str | None,
                 review_round: int, priority: str) -> None:
        def publish(activity: dict) -> None:
            _live_progress(
                publisher, issue=issue, title=title, run_id=run_id,
                role=role, branch=branch, worktree=worktree,
                started=started,
                pr_url=pr_url, review_round=review_round,
                priority=priority, activity=activity,
            )

        self._publish = publish
        self._last_visible: tuple | None = None
        self._last_patch = 0.0

    def __call__(self, activity: dict) -> None:
        visible = (
            activity["phase"], activity["action"], activity["result"],
            activity["model_wait"],
            # The idle-recovery state is visible progress (Issue #94):
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
    verified (Issue #219): continuing on a guessed identity risks a
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


def _dispatch_release(issue: dict, config: RunnerConfig,
                      source_repo: str) -> IssueResult:
    """Deliver a RELEASE-scene ticket through the deterministic release
    state machine (Issue #98): a first-class task type that NEVER enters
    the normal `run_pi` development path (scope verification, gates,
    tests, tag, GitHub Release)."""
    number = int(issue["number"])
    # `orbi.release` imports the runner primitives back, so the
    # dispatch imports it lazily here — a module-level import would
    # be circular (Issue #286).
    #
    # Issue #708: the ready scan's stale snapshot can hand an
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


def _dispatch_content_only(issue: dict, config: RunnerConfig,
                           source_repo: str) -> IssueResult:
    """Deliver a CONTENT_ONLY-scene ticket through the ticket-only
    content agent: no execution, the deliverable posted to the Issue."""
    process_ticket_only(issue, config, source_repo)
    return IssueResult("ticket-only", None)


def _gather_claim_facts(issue: dict, config: RunnerConfig,
                        source_repo: str,
                        repo_policy: RepoPolicy | None) -> DeliveryFacts:
    """Gather the claim facts of one attempt (the dispatch's first step).

    The probe sequence is `process_issue`'s prologue, order unchanged:
    the attempt binds its run id before any other step is logged
    (Issue #41), the repository policy applies, the live
    `ai-in-progress` state is read directly (Issue #658), then the
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
    # scan below (Issue #41; review round 3, PR #42). It is bound before
    # the repository-policy read (Issue #527) so a forbidden/malformed
    # repository file blocks the claim with a run-marked comment.
    run_id = new_run_id()
    set_run_id(run_id)
    # Repository-level config-as-code (Issue #527): the caller resolved
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
                path=repository_config_path(config, source_repo),
                run_command=run_command,
            ) if previous_sha else None
        )
        repo_config_fields = repo_config_audit(
            repo_policy.sha, repo_policy,
            previous_sha=previous_sha,
            previous_policy=previous_policy,
        )
    base_branch = config.base_branch
    # The claim label is a delivery-policy key (Issue #527); the lifecycle
    # labels stay host constants.
    dispatch_label = (config.dispatch_label or READY_LABEL)
    claim_labels = _issue_label_set(issue)
    stable_branch = task_branch(source_repo, number)
    in_progress = has_in_progress_label(number, source_repo)
    takeover_pr: dict | None = None
    external_takeover = False
    stable_branch_present = False
    resume_scene: tuple[str, Path] | None = None
    resume_error: Exception | None = None
    if not in_progress and dispatch_label in claim_labels:
        takeover_pr = open_pr_for_branch(config.repo_dir, stable_branch)
        stable_branch_present = stable_branch_exists(
            config.repo_dir, stable_branch,
        )
        if takeover_pr is None:
            # Issue #608: a triage Issue whose body routes to an EXTERNAL
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
        # Issue #608: an in-flight external takeover (the run died between
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
            # Issue #219: the worktree of this issue exists but its run
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
        worktree_present=resume_scene is not None,
        branch_present=stable_branch_present,
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
    RESTART_IN_FLIGHT (the Issue #18 restart resume), EXTERNAL_TAKEOVER
    (the Issue #608 contributor-PR takeover) and OPS (the Issue #537
    full-execution ops session: the same claim, worktree and run
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
    if in_progress:
        # Issue #724: the claim-race window has two halves. The scan
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
            # Issue #219: the worktree of this issue exists but its run
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
        # Issue #219: the resumed run keeps its ORIGINAL branch —
        # after a repo rename the re-derived name would carry the NEW
        # slug and no longer match the branch the worktree is on (a
        # second branch would be a second delivery). The worktree's
        # current branch IS the scene's branch.
        branch = run_command(
            ["git", "branch", "--show-current"],
            cwd=existing_worktree,
        )
    elif external_takeover:
        # Issue #608: the external takeover delivers the contributor's own
        # branch — the identity the takeover PR is frozen on.
        branch = takeover_pr["headRefName"]
    # Pickup priority (Issue #101): derived from the scanned issue's
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
        # Issue #527 D4: the run comment carries the repository config sha
        # (the file blob at the default branch tip) so a policy change is
        # always visible on the run.
        run_info += f" repo_config={facts.repo_policy.sha}"
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    if not in_progress and dispatch_label in claim_labels \
            and takeover_pr is None:
        # Issue #658: the pickup scan and the in-progress recheck above
        # both predate freeze_base (a seconds-long network round trip).
        # A label — or the stable branch — appearing inside that window
        # means another instance claimed this Issue while we were
        # preparing: yield. No label writes, no comments, nothing that
        # could interrupt the winner's in-flight delivery; the next
        # tick's scan picks work up again on its own. Issue #724: the
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
    # Issue #266: the successful pickup resets the stale-pickup clock in
    # the health state file (bypass — a state-write failure never fails
    # the claim).
    try:
        runner_health.record_pickup(config.repo_dir)
    except Exception:
        LOGGER.exception("issue=%s health_pickup_record_failed", number)
    # The Issue is in flight from the claim label on: bind the stop
    # scene (Issue #48) so a SIGTERM during this tick logs the active
    # Issue context, not only systemd's generic "Stopped" line. The
    # branch and worktree path are the same derived values the
    # worktree creation below uses (bound before the worktree exists);
    # the run identity is bound once as a frozen DeliveryContext and
    # the failure/finish paths unpack it from there.
    ctx = DeliveryContext(
        run_id=run_id, issue=number, branch=branch,
        worktree=existing_worktree or worktree_path(
            config.repo_dir, source_repo, number, run_id,
        ),
    )
    set_active_run(
        number, title, branch,
        # The verified resume scene keeps its own path (after a repo
        # rename it carries the OLD slug — Issue #219); otherwise the
        # derived path (the same value the worktree creation uses).
        str(ctx.worktree),
    )
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publish = functools.partial(
        _safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_IMPLEMENT,
    )
    worktree: Path | None = None
    started = time.monotonic()
    # Issue #79: the `Orbi opened PR:` scene comment is the first
    # delivery step AFTER the opened-PR label transition that can still
    # fail; when it does, the failure path below must leave the Issue in
    # the terminal state `ai-blocked` ALONE (docs/workflow.mdx label
    # lifecycle: `ai-pr-opened` is removed on terminal failure) — the same
    # convention as every other terminal failure path (verify_resumed_pr,
    # wait_for_delivery).
    pr_opened = False
    try:
        worktree = create_worktree(
            config.repo_dir, source_repo, number, run_id, base_sha,
            existing=existing_worktree,
            # A stable branch without an open PR is the interrupted push/
            # create gap: continue from that branch rather than trying to
            # create a second local branch with the same name.
            existing_branch=stable_branch_present or takeover_pr is not None,
            # Issue #608: an external takeover checks out the
            # contributor's own head branch — the identity the takeover
            # PR is frozen on.
            branch=(
                takeover_pr["headRefName"] if external_takeover else None
            ),
        )
        # Issue #219: the run state file is the same-run marker —
        # written for EVERY run (a fresh one included, so a later
        # interruption can be verified and resumed), refreshed for a
        # resumed one (same run id, never a second marker).
        write_run_state(
            worktree, run_id=run_id, issue=number,
            source_repo=source_repo, branch=branch,
        )
        ctx = replace(ctx, worktree=worktree)
        # Issue #219: the new session starts from the existing work —
        # the uncommitted changes and the previous session's progress —
        # instead of a fresh redo. A clean worktree without a previous
        # session is a fresh scene (None, the pre-#219 prompt).
        resume_ctx = resume_context(worktree)
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
                changed_files=len(changed_files(worktree)),
                reused_runs=previous_sessions,
                previous_session=(
                    snapshot.get("session_id") if snapshot else None
                ) or "-",
            )
        config = replace(config, base_sha=base_sha, run_id=run_id)
        if ops:
            # Issue #537: the ops session runs the ops playbook — the
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
                run_id, run_info, branch, worktree,
                extra_fields=repo_config_fields,
            ),
        )
        # Issue #79: the whole ProgressPublisher path is a bypass — a
        # failure here (404, rate limit) is logged and never skips
        # `run_pi` or fails the delivery.
        publish(
            action=lambda: publisher.ensure(_progress_body(
                _progress_state(
                    issue=number, title=title, run_id=run_id,
                    role=ROLE_IMPLEMENT, branch=branch,
                    worktree=worktree, started=started,
                    pr_url=None, review_round=0, priority=priority,
                ),
            )),
        )
        if takeover_pr is None:
            run_pi(
                issue, worktree, config, source_repo, branch=branch,
                resume_context=resume_ctx,
                progress=LiveProgressThrottle(
                    publisher, issue=number, title=title, run_id=run_id,
                    role=ROLE_IMPLEMENT, branch=branch, worktree=worktree,
                    started=started, pr_url=None, review_round=0,
                    priority=priority,
                ),
            )
        publish(
            action=lambda: _publish_plan_milestone(publisher, worktree),
        )
        publish(
            action=lambda: _publish_test_milestone(publisher, worktree),
        )
        if ops:
            # Issue #537: an ops delivery without a commit is COMPLETE —
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
                            issue=number, title=title, run_id=run_id,
                            role=ROLE_IMPLEMENT, branch=branch,
                            worktree=worktree, started=started,
                            pr_url=None, review_round=0, priority=priority,
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
                # Issue #266: the delivered outcome breaks any failure
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
        # Issue #186: the deterministic closeout (commit boundary, base
        # freshness + absorb, plain push, PR creation, PR verification)
        # is the Runner's job — the agent stopped at the committed
        # delivery.
        pr_url = (
            takeover_pr["url"] if takeover_pr is not None else deliver_pr(
                worktree, branch, base_branch, base_sha, run_id,
                issue=number, issue_title=title,
                repo_dir=config.repo_dir, source_repo=source_repo,
            )
        )
        ctx = replace(ctx, pr=pr_url)
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        if pr_url is None:
            # Issue #746: the Issue was closed during delivery — the
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
            # Issue #266: the delivered outcome breaks any failure
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
        # frozen base, so the head always advanced. (Issue #82 removed
        # the fixer's `fix pushed` milestone: findings are fixed by the
        # review session, which records its own round comments.)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_PR_OPENED,
            current_labels={IN_PROGRESS_LABEL},
        )
        pr_opened = True
        # The scene comment is NOT a bypass (Issue #79): the next
        # tick's resume (Issue #45/#89) parses it to recover run_id,
        # base and PR, so a failure here is a real delivery failure —
        # it propagates into the failure path below (ai-blocked, the
        # `Orbi failed` comment, re-raise). The `ProgressPublisher`
        # steps around it stay bypass: a failure there (Issue #60: the
        # #57 delivered PATCH 404'd and the runner labeled the Issue
        # ai-blocked, skipping the review of a valid PR) is logged as
        # `progress_publish_failed` and the run continues into the
        # review/merge wait loop.
        comment_issue(
            number, repo=source_repo,
            body=opened_pr_comment_body(
                run_id, run_info, pr_url, external=external_takeover,
            ),
        )
        if config.human_review_gate:
            # Issue #763: the human acceptance checklist — the readable
            # face of the gate — posts ONCE per delivery, at the moment
            # the delivery completes (the PR opens). Bypass (Issue #79):
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
            except Exception:
                LOGGER.exception(
                    "issue=%s human_review_checklist_failed", number,
                )
        publish(
            action=lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch,
                worktree=worktree, started=started,
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
        # Issue #266: the delivered outcome breaks any failure streak of
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
        # Issue #608: an external takeover reports its own kind — the
        # delivery wait then closes the triage Issue itself after the
        # merge (the external PR body carries no `Fixes #N` for this
        # Issue, so GitHub never closes it natively).
        return IssueResult(
            "external-pr" if external_takeover else "pr", ctx.pr,
        )
    except (ModelWaitDeadError, RecoverablePiFailure) as exc:
        # Issue #227/#325: classified Pi/model infrastructure failures are
        # recoverable. Keep the claim, worktree and run-state file so the
        # next in-flight scan resumes this same run.
        recoverable_name = (
            "model_wait recovered"
            if isinstance(exc, ModelWaitDeadError)
            else "Pi failure recovered"
        )
        # Issue #227: the hung-model-request recovery is a CLASSIFIED,
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
        # Issue #645: an identical repeated failure of this run updates
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
                issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch,
                worktree=worktree, started=started,
                pr_url=None, review_round=0, priority=priority,
            ), outcome=(
                f"**Orbi {recoverable_name}**\n\n"
                f"failure: {detail}\n"
                "next step: nothing — the Issue stays ai-in-progress "
                "and the next tick resumes the same run (same run id, "
                "branch, worktree)"
            ))),
        )
        # Issue #266: the recoverable failure reaches the health history
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
        # Issue #266: record the failed run attempt (conservative failure
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
            # The shared terminal reporter (Issue #288): every failure
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
                cause=f"{_failure_detail(exc)} ({run_info})",
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
            # Issue #256: the terminal evidence is recorded (journal +
            # `Orbi failed` comment) and the Issue is genuinely
            # `ai-blocked` — the scene is never needed again (a retry
            # gets a new run id and worktree), so clean it up. The
            # recoverable paths (ModelWaitDeadError, ai-fix-needed) and
            # the simulated-kill scene (blocked transition never landed)
            # never reach this branch — the worktree is kept for the
            # same-run resume.
            if worktree is not None:
                cleanup_task_worktree(
                    ctx.worktree, config.repo_dir, run_id=ctx.run_id,
                    issue=ctx.issue,
                )
        # Issue #239: the failure is terminal — the Issue is `ai-blocked`
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


# The scene dispatch tables (Issue #787). Phase one routes the
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


def process_issue(issue: dict, config: RunnerConfig, source_repo: str,
                  repo_policy: RepoPolicy | None = None) -> IssueResult:
    """Deliver one claimed Issue by its explicit delivery scene.

    Three steps, at two fact depths (Issue #787): the ticket-face facts
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
    # Issue #100: the progress comment's issue line shows the number
    # AND the title in every scene. The scanned issue dict always
    # carries the GitHub title (every scan fetches `title`); a missing
    # or non-string title fails fast here (KeyError / ValueError in
    # `progress.issue_field`) — it is never fabricated.
    title = issue["title"]
    ticket_scene = classify(
        labels=_issue_label_set(issue), scene=None, pr_state=None,
        worktree_present=False, branch_present=False,
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


def _finish_progress_body(*, number: int, title: str, run_id: str,
                          role: str, branch: str | None,
                          worktree: Path | None, pr_url: str | None,
                          review_round: int, priority: str, detail: str,
                          next_step: str, outcome: str) -> str:
    """Render the terminal progress scene shared by every finish path."""
    return _progress_body(_progress_state(
        issue=number, title=title, run_id=run_id, role=role,
        branch=branch or "-", worktree=worktree or Path("-"),
        started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=(
        f"**Orbi {outcome}**\n\n"
        f"failure: {detail}\n"
        f"next step: {next_step}"
    ))


def _finish_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, next_step: str, title: str, outcome: str,
    role: str = ROLE_REVIEW, review_round: int = 0,
    priority: str = "normal",
) -> None:
    """Finish the tracked progress comment with the terminal scene.

    One function for both terminal scenes (Issue #293): `outcome` is
    the scene headline — `blocked` (Issue #18: the terminal failure,
    the same body the `process_issue` failure path writes) or
    `fix needed` (Issue #50: the recoverable failure that keeps the
    Issue in the automatic fix loop, the next timer resuming the same
    run, branch, worktree and PR). `title` is the issue's GitHub title
    (Issue #100): the scene shows `#<number> <title>` like every other
    progress scene; it is required, never fabricated.

    `ensure` finds the run's existing progress comment by its hidden
    marker (PATCHing it in place) or creates it when the run never
    reached one; either way the scene is the final state. `role`,
    `review_round` and `priority` are the actual role, completed
    review rounds and pickup priority of the run: the caller derives
    them from the Issue's trusted review-round comments and labels,
    so the terminal comment never shows a stale hardcoded role/round
    (review round 2, PR #42). Issue #82: the only post-PR role is
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
        next_step=next_step, outcome=outcome,
    ))


# Issue #288: the failure-scene snapshot placeholder — the fields a
# failure comment shows when no session file exists yet (the Pi never
# started or the session dir is gone). One constant for every reporter.
_SNAPSHOT_PLACEHOLDER: dict = {
    "session_id": None, "session_file": None,
    "phase": "starting",
    "last_activity": None, "action": None,
    "result": None,
}


def _snapshot_or_placeholder(session_dir: Path, *, number: int) -> dict:
    """Best-effort activity snapshot for a failure scene (Issue #288).

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


# The fixed phrases of the two failure templates (Issue #50): the
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
# Issue #775: the whole failure-comment body is capped well below the
# GitHub comment limit; the cut is noted with the full session log path.
FAILURE_COMMENT_MAX_CHARS = 20000


def report_delivery_failure(
    exc: BaseException, *, issue: dict, source_repo: str,
    run_id: str | None, pr_url: str | None, worktree: Path | None,
    branch: str | None, role: str, cause: str, evidence: bool = False,
    classify: bool = True, current_labels: set[str] | None = None,
    blocked_suffix: str = "", review_round: int | None = None,
    finish: bool = True, publisher: ProgressPublisher | None = None,
) -> str:
    """Report one delivery failure through the Issue #50 flow (Issue #288).

    The single implementation of the flow previously hand-copied in
    `verify_resumed_pr`, `_run_review_round` and `process_issue`:
    classify the exception, transition the label (`ai-blocked` ALONE
    for an explicit `UnrecoverableDeliveryError`, `ai-fix-needed` for
    every recoverable failure), assemble the failure body (fixed
    phrase + `cause` + run scene + evidence), prefix the run marker,
    comment the Issue (the PR too on the recoverable branch — the
    terminal blocked state is Issue-only), then publish the milestone
    and the terminal progress scene as a pure bypass (Issue #79). The
    milestone text is `blocked|fix needed: {cause}` — untruncated, so
    the concrete reason (a missing worktree path, a round-exhaustion
    reason) stays visible in the mobile notification. Returns the
    outcome, `"blocked"` or `"fix needed"`.

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

    The function raises on its own failures (label patch, comment):
    the callers keep their reporting-error semantics (the review loop
    fails fast; the other two log `failure reporting failed` and
    continue to their terminal return / re-raise).
    """
    number = int(issue["number"])
    title = issue["title"]
    priority = issue_priority(issue)
    blocked = not classify or is_unrecoverable_failure(exc)

    def scene_line() -> str | None:
        """The run scene for the body, or None when omitted.

        The classified reporters degrade a failed snapshot read to the
        '-' placeholder (the debug entry still carries worktree and
        branch, Issue #50); the implement-phase first-line scene is
        omitted entirely on a failed read — the structured-alert field
        block then shows only the failure's own fields (the pinned
        #256 isolation).
        """
        if classify:
            snapshot = _snapshot_or_placeholder(
                worktree / ".pi-session", number=number,
            )
        else:
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
                return None
            if snapshot is None:
                snapshot = dict(_SNAPSHOT_PLACEHOLDER)
        return format_run_scene(
            snapshot,
            run_id=run_id or "-",
            issue=issue_context(source_repo, number),
            role=role, branch=branch or "-", worktree=str(worktree),
        )

    labels = (
        current_labels if current_labels is not None
        else issue_labels(number, source_repo)
    )
    if blocked:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=labels,
        )
        body = f"Orbi failed: {cause}"
        if classify:
            body += _BLOCKED_PRECONDITION_PHRASE + blocked_suffix
        elif worktree is not None:
            scene = scene_line()
            if scene is not None:
                # The scene fields join the FIRST body line, where
                # `format_status_comment` lifts them into the
                # structured-alert field block.
                body += f" {scene}"
        outcome = "blocked"
    else:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=labels,
        )
        body = f"Orbi needs a fix: {cause}{_FIX_NEEDED_PHRASE}"
        # The full scene is always appended: a recoverable failure
        # happens after the worktree was derived (a failure before the
        # derivation is unrecoverable and never reaches this branch).
        body += f"\n{scene_line()}"
        outcome = "fix needed"
    if evidence:
        body += _failure_evidence(worktree, exc)
    if len(body) > FAILURE_COMMENT_MAX_CHARS:
        # Issue #775: the comment body is capped; the cut preserves the
        # cause-first head and names the full session log for the part
        # that was dropped.
        session_file = _latest_session_file(worktree)
        body = (
            body[:FAILURE_COMMENT_MAX_CHARS]
            + f"\n\n_[comment truncated at {FAILURE_COMMENT_MAX_CHARS} "
            f"chars; full session log: "
            f"{session_file or '<unavailable>'}]_"
        )
    if run_id:
        body = f"{run_marker(run_id)}\n{body}"
    comment_issue(number, repo=source_repo, body=body)
    if pr_url and not blocked:
        # The recoverable scene is written to the PR too (Issue #50):
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
                f"blocked: {cause}",
            ))
            if classify:
                finish_failure = f"{cause}{_BLOCKED_PRECONDITION_PHRASE}"
                next_step = (
                    "fix the precondition above (see the reason) and "
                    "relabel the Issue ai-fix-needed to resume this "
                    "same PR"
                )
            else:
                finish_failure = cause
                next_step = (
                    "fix the failure above and re-run this Issue (a "
                    "new run id is created automatically)"
                )
            finish_outcome = "blocked"
        else:
            publish(action=lambda: target.milestone(
                f"fix needed: {cause}",
            ))
            finish_failure = cause
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
            )))
    return outcome


def _run_review_round(
    pr_url: str, issue: dict, config: RunnerConfig, source_repo: str,
) -> bool | None:
    """Run ONE review round of an open-PR delivery (Issue #289).

    Extracted from `wait_for_delivery`'s loop body so the wait stays a
    plain polling skeleton and one round is readable on its own: read
    the delivery labels ONCE per round, repair a lost `ai-in-progress`
    transition, gate on the resumable opened-PR states, then recover
    the trusted scene, validate the frozen base, derive the
    worktree/branch, run the independent review and classify any
    failure (Issue #50).

    Returns True when this round merged the PR (terminal success);
    False when findings remain (`ai-fix-needed`, the label transition
    happens inside the review itself) and the caller may poll into the
    next round; and None when a terminal state was already handled and
    the caller must release the slot and return: an unrecoverable
    precondition (`ai-blocked`), a recoverable failure's full
    `ai-fix-needed` scene (the next tick resumes the same run, branch,
    worktree and PR), a failed `ai-in-progress` label repair, or an
    open PR without a resumable delivery label (both `ai-blocked`).
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
    # Issue #763: the human acceptance gate — checked BEFORE the scene,
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
    # (Issue #34). `ai-pr-opened` awaits review; `ai-fix-needed`
    # awaits the next review session after a finding or a base
    # conflict (Issue #82: the review session fixes findings in
    # the same session, so both states run the same review). A
    # clean verdict re-freezes the head, merges and returns
    # True (terminal); unfixed findings or a behind/conflict
    # gate label the Issue `ai-fix-needed` and the next
    # iteration re-runs the same independent review. A review
    # that cannot run is classified (Issue #50): a RECOVERABLE
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
    try:
        try:
            scene = resume_scene(
                issue_comments(number, repo=source_repo),
            )
        except ValueError as scene_exc:
            # Issue #50: without the trusted scene the runner
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
        # Issue #91 + #50: the scene freezes the base the PR
        # was opened against. The config may have moved on (or
        # the comment is stale): reviewing or merging a PR
        # frozen on another base against the configured one
        # would run the freeze/merge gate on the wrong base,
        # so fail fast before any git/Pi mutation instead of
        # silently switching bases. A base-branch change is a
        # human decision (Issue #50): the runner must not
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
        # Issue #90 + #50: the worktree is derived from the
        # configured repo_dir, source repo, Issue number and run id
        # (never read from a comment). A missing directory is a
        # RECOVERABLE failure: the branch still exists on the
        # remote and the worktree can be recreated (git worktree
        # add) on the next resume, so the handler below keeps the
        # Issue in the automatic fix loop (ai-fix-needed) with the
        # PR and branch preserved.
        if not worktree.is_dir():
            # The failure comment must carry the full scene
            # including the branch (Issue #50): the stable
            # derivation is the best available guess when the
            # worktree is gone.
            branch = task_branch(
                source_repo, number, scene["run_id"],
            )
            raise RuntimeError(f"worktree missing: {worktree}")
        # The delivery branch is a local git fact of the derived
        # worktree — the stable naming for the Runner's own
        # deliveries, the contributor's head branch for an
        # external takeover (Issue #608). Deriving it from the
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
        merged = review_and_merge_if_clean(
            worktree, branch, config.base_branch,
            review_config, source_repo, number,
            title=title, priority=priority,
        )
    except Exception as exc:
        detail = _failure_detail(exc)
        if isinstance(exc, ReviewRoundsExhausted):
            # The bounded budget is an intentional human decision
            # point, not a Runner bug. Keep the structured event and
            # terminal handling below, but do not emit a traceback.
            event(
                "review_rounds_exhausted_expected_terminal",
                level=logging.ERROR, issue=number, pr=pr_url,
                reason=detail,
            )
        else:
            # Real delivery failures retain traceback evidence for
            # health monitoring and diagnosis.
            LOGGER.exception(
                "issue=%s delivery_review_failed pr=%s", number, pr_url,
            )
        # The shared classified reporter (Issue #288): recoverable ->
        # `ai-fix-needed` with the full scene on Issue AND PR,
        # unrecoverable -> `ai-blocked` ALONE. No wrapper: a reporting
        # failure here fails the tick fast (the slot is released by
        # `main`'s `finally`), exactly like any other Runner bug.
        report_delivery_failure(
            exc, issue=issue, source_repo=source_repo,
            run_id=run_id, pr_url=pr_url,
            worktree=worktree, branch=branch, role=ROLE_REVIEW,
            cause=f"the independent review of PR {pr_url} failed: {detail}",
            evidence=True,
        )
        return None
    if merged:
        event(
            "delivery_auto_merged", issue=number, pr=pr_url,
        )
        return True
    return False


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


def wait_for_delivery(pr_url: str, issue: dict, config: RunnerConfig,
                      source_repo: str,
                      poll_interval: float = PI_POLL_INTERVAL,
                      external_takeover: bool = False) -> None:
    """Own the delivery lifecycle: hold the slot until merge or failure.

    The slot is acquired by `main` before the claim and must stay
    occupied through implement -> review -> merge (Issue #39): a
    delivery whose PR is open still needs the machine, and no other
    Runner may start a second Pi while it is held. The Runner is the
    owner of that lifecycle, so it re-checks the delivery every
    `poll_interval` seconds (the same cadence as the Pi activity poll):

    - PR `MERGED` -> terminal: the delivery is done, the slot is
      released by the caller and the next tick may claim new work. An
      EXTERNAL takeover (Issue #608) additionally closes the triage
      Issue with the merge evidence — the contributor's PR body carries
      no `Fixes #N` for this Issue, so GitHub never closes it natively
      (合并外部 PR 即关票); the close is bookkeeping of an already
      merged fact and its failure is logged, never a rewrite;
    - PR `CLOSED` without merge -> terminal failure: the Issue is
      marked `ai-blocked` (removing `ai-pr-opened`/`ai-fix-needed`) with
      a failure comment carrying the run marker, then the slot is
      released by the caller. An EXTERNAL takeover is the exception
      (Issue #608): the contributor withdrew the PR or a maintainer
      rejected it — that is the 放弃/不可修 fallback, so the Issue is
      requeued to `ai-ready` and the next claim redoes the fix
      internally;
    - Issue in an opened-PR state (`ai-pr-opened` awaiting review, or
      `ai-fix-needed` awaiting the next review session after a finding
      or a base conflict) -> the Runner runs the independent review of
      the frozen PR (Issue #34) itself, on the same run, while still
      holding the slot: the review session fixes Blocker/Major findings
      IN THE SAME SESSION (Issue #82 — no cold-start fixer, no third
      review), a clean verdict re-freezes the head and merges the PR
      via the merge gate, confirms the merge, syncs the deployment
      checkout and labels the Issue `ai-merged` (terminal, the slot is
      released); unfixed findings or a behind/conflict gate label the
      Issue `ai-fix-needed` and the next iteration re-runs the same
      independent review;
    - an open PR with `ai-in-progress` -> repair the lost transition to
      `ai-pr-opened`, then review immediately;
    - the human acceptance gate (Issue #763, `human_review_gate: true`):
      while the checklist's column 2 is non-empty and no human has
      applied `ai-human-review`, the round ends BEFORE any review
      session — the waiting primitive returns the ticket to `ai-ready`
      (the opened-PR anchor stays), the wait returns and the slot is
      released; the next tick re-checks with one label read. The wait
      never consumes the review-round budget;
    - any other unrecoverable label inconsistency -> mark the Issue
      `ai-blocked` and release the slot. It must never hold the slot by
      polling forever.

    CI status is included in every open-PR heartbeat so pending and failed
    checks remain visible while the review gate is being reached.
    """
    number = int(issue["number"])
    # Issue #100: the progress comment's issue line shows the number
    # AND the title in every scene; the scanned issue dict always
    # carries the GitHub title (every scan fetches `title`) — a
    # missing or non-string title fails fast, never fabricated.
    title = issue["title"]
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    # Pickup priority (Issue #101): derived from the scanned issue's
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

    while True:
        state, ci_checks = pr_delivery_status(pr_url, source_repo)
        if state == "OPEN":
            event(
                "delivery_ci", issue=number, pr=pr_url,
                checks=",".join(ci_checks) or "none",
            )
        if state == "MERGED":
            event(
                "delivery_merged", issue=number, pr=pr_url,
            )
            if external_takeover:
                # Issue #608: merging the external PR closes the triage
                # Issue (the PR body has no `Fixes #N` for it).
                _close_external_triage_issue(
                    number, source_repo, pr_url, marker, run_id,
                )
            return
        if state == "CLOSED":
            # The current labels are read ONCE before the transition:
            # the terminal patch clears every delivery-state label that
            # is present (`ai-pr-opened`, and `ai-fix-needed` when the
            # PR was closed while awaiting the next review session).
            labels = issue_labels(number, source_repo)
            if external_takeover:
                # Issue #608: the external PR was closed without a merge
                # (contributor withdrew, or a maintainer rejected it) —
                # the 放弃/不可修 fallback. The Issue returns to the
                # ready queue and the next claim redoes the fix
                # internally; the closed PR keeps the supersession story
                # in its thread.
                event(
                    "external_takeover_closed", issue=number, pr=pr_url,
                )
                apply_label_patch(
                    number, repo=source_repo, event=EVENT_REQUEUE,
                    current_labels=labels,
                )
                body = (
                    f"{marker}\n"
                    f"Orbi: the external PR {pr_url} was closed without "
                    f"a merge; the triage Issue #{number} returns to the "
                    "ready queue and the next claim delivers the fix "
                    f"internally (run_id={run_id})"
                )
                comment_issue(number, repo=source_repo, body=body)
                # The supersession is explained on the closed PR thread
                # too: the contributor watches their PR, never the
                # triage Issue (docs/contributing.mdx, Issue #608).
                comment_pr(_pr_number(pr_url), repo=source_repo, body=body)
                return
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
                # Issue #79: the blocked-scene progress publishing is
                # bypass — a 404 here must not escape the wait loop
                # (the terminal bookkeeping above already completed and
                # the slot must be released).
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
                # Issue #82 — both opened-PR states are review states
                # (the review session fixes findings in the same
                # session), so the role is always `review`, and the
                # trusted review-round comments bound the round count
                # (GitHub is the only state store).
                blocked_round = review_rounds_so_far(
                    issue_comments(number, repo=source_repo),
                )
                # The tracked progress comment becomes the blocked scene
                # (Issue #18): the same terminal body the other failure
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
        # Issue #289: one OPEN round — the label read/repair, the
        # resumable gate, one independent review of the frozen PR and
        # the whole failure classification — lives in
        # `_run_review_round`. True (merged this round) and None (a
        # terminal state was already handled: ai-blocked, or the
        # recoverable ai-fix-needed scene the next tick resumes) both
        # end the delivery here; only False (findings) keeps polling.
        merged_this_round = _run_review_round(
            pr_url, issue, config, source_repo,
        )
        if merged_this_round is not False:
            if merged_this_round is True and external_takeover:
                # Issue #726: the SUCCESSFUL auto-merge path must close
                # the triage Issue exactly like the MERGED polling branch
                # above — previously only the "someone else merged" poll
                # reached it, so every auto-merged external contribution
                # leaked a zombie triage ticket.
                _close_external_triage_issue(
                    number, source_repo, pr_url, marker, run_id,
                )
            return
        # Back to the next review round: yield the cadence first
        # (Issue #588). This tail previously had NO sleep — a red CI
        # or unfixed findings re-ran the full reviewer session
        # back-to-back in a hot loop while holding the slot, and
        # poll_interval was a dead parameter.
        time.sleep(poll_interval)


def _preflight(config: RunnerConfig) -> None:
    """Run the Runner tick's pre-slot startup checks (fail fast).

    Every pre-claim check lives here as one reusable unit, so the tick
    entry (`main`) stays a thin `argparse -> preflight -> slot ->
    dispatch -> finally` spine and the same checks can be exercised
    independently (doctor and other callers) without re-inlining them.
    Order is significant: the editable CLI refresh and the
    source-freshness gate precede the unit-drift and transport checks,
    and all of them precede any slot or claim.
    """
    # Issue #399: publish the configured active milestone for the CI
    # triage workflow. This is a bypass; delivery must continue when the
    # variable API is unavailable.
    sync_active_milestone_variable(
        config.source_repos[0], config.active_milestone,
        run_command=run_command,
    )
    # Editable CLI install refresh (Issue #158): BEFORE any slot or
    # claim the tool env's editable metadata must match the checkout's
    # packaging inputs — a merged packaging change (entry point,
    # version or dependency in `pyproject.toml`) would otherwise make
    # the NEXT CLI process die before the Runner can start (the #158
    # incident shape; since the src layout, Issue #168, a new package
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
    # Issue #330: the CLI self-update acts on the deployment home, never
    # on the delivery checkout (repo_dir may be a foreign repo X without
    # any orbi packaging input).
    refresh_cli_install(
        config.deploy_home, run_command=run_command,
    )
    # Startup source freshness (Issue #525): BEFORE any slot or claim,
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
    # Deployment consistency (Issue #103, #142): BEFORE any slot or
    # claim the installed systemd units must match the repo templates
    # (the templates the ExecStartPre-synced checkout just loaded).
    # Drift is self-healed with the SAME idempotent install (copy the
    # templates, daemon-reload, enable the timer — never start/stop/
    # restart the service: a currently RUNNING task is never
    # interrupted) and re-verified with the SAME hash check. Drift
    # that survives the sync — or a failing install step — logs a
    # structured `unit_drift` line per unit and fails fast: this
    # start takes no slot, claims no Issue and changes no label.
    try:
        check_unit_drift(config.deploy_home, unit_name=config.unit_name)
    except UnitDriftError:
        sync_drifted_units(
            config.deploy_home,
            unit_name=config.unit_name,
            max_concurrency=config.max_concurrency,
            run_command=run_command,
        )
    # Task-worktree reclamation (Issue #760): closed-Issue worktrees past
    # the retention window are bounded garbage — reclaim them at the tick
    # start beside the drift check, NOT through a manual command nobody
    # remembers to run. Idempotent, bounded and never fatal: a reclaim
    # failure is a `worktree_reclaim_failed` line, never a failed start.
    try:
        reclaim_released_worktrees(config)
    except Exception:
        LOGGER.exception("worktree_reclaim_failed")
    # Git transport preflight (Issue #114, #580): BEFORE any slot or
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
    # Self-health check (Issue #266): BEFORE any slot or claim the Runner
    # actively looks for the incident patterns of 2026-09-04 — a service
    # crash loop (>= 3 crashes in 60 min, the #262 scene), repeated
    # same-fingerprint run failures on one Issue (>= 3, the #246 scene) and
    # a stale pickup while the ai-ready queue is non-empty. It is a pure
    # bypass (Issue #79): a check failure logs `health_check_failed` and
    # never fails the delivery, takes no slot and changes no label.
    try:
        runner_health.run_health_check(config, run_command=run_command)
    except Exception:
        LOGGER.exception("health_check_failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("ORBI_CONFIG", "orbi.toml")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=log_format())
    # Stop scene (Issue #48): install the SIGTERM handler BEFORE any
    # other step so every phase of the tick (pre-claim, claim,
    # implement, delivery wait) stops with the active Issue context
    # logged and the live Pi child shut down — never an orphan Pi and
    # never only systemd's generic "Stopped" line. Python only allows
    # signal handlers in the main thread (the CLI entry point always
    # is; in-thread `main()` test calls skip the install).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_stop)

    try:
        config = load_config(args.config)
        validate_config(config)
        validate_execution_source_repos(config.source_repos)
    except ValueError as exc:
        event("config_invalid", level=logging.ERROR, reason=exc)
        return 1
    _preflight(config)
    # Concurrency cap (Issue #39): take one slot BEFORE claiming anything.
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
        selected = pick_next_delivery(
            config.source_repos, config.slot_dir,
            config.max_concurrency,
            config.active_milestone,
            config=config,
        )
        if selected is None:
            LOGGER.info(
                "source_repos=%s outcome=no_ready_issue",
                config.source_repos,
            )
            # Issue #385: arm a release ticket as a pure bypass. A failed
            # label operation must not change the idle outcome.
            if config.active_milestone is not None:
                try:
                    arm_release_ticket(
                        config.source_repos[0],
                        config.active_milestone,
                    )
                except Exception:
                    LOGGER.exception(
                        "release_ticket_arm_failed repo=%s milestone=%s",
                        config.source_repos[0],
                        config.active_milestone,
                    )
                # Issue #274: validate and advance only after the arm attempt.
                # Issue #614: like the arm above, the advance is an idle-path
                # pure bypass — a renamed/deleted milestone or a failed `gh`
                # call must not turn an idle tick into a non-zero exit.
                try:
                    advance_active_milestone_on_idle(
                        config.source_repos[0],
                        config.active_milestone,
                        config.config_path,
                        auto_next_milestone=config.auto_next_milestone,
                    )
                except Exception:
                    LOGGER.exception(
                        "active_milestone_advance_failed repo=%s milestone=%s",
                        config.source_repos[0],
                        config.active_milestone,
                    )
            return 0
        source_repo, issue, scene = selected
        # Issue #527: resolve the repository-level policy ONCE for the whole
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
        if repo_policy is not None:
            config = apply_repo_policy(config, source_repo, repo_policy)
        result = None
        if scene is not None:
            # An open PR is a recoverable review state: resume the
            # same run on the same branch, worktree and PR (Issue #45).
            # Bind the scene's run id first so every journal line and
            # GitHub comment of the resumed delivery carries it
            # (Issue #41). Both opened-PR states go straight to the
            # delivery wait: `ai-pr-opened` awaits review, and
            # `ai-fix-needed` awaits the next review session —
            # Issue #82: the review session itself fixes findings in the
            # same session, so there is no cold-start fixer to run
            # here (a stranded `ai-pr-opened` delivery, dead runner or
            # the progress failure of Issue #70, is reviewed the same
            # way).
            set_run_id(scene["run_id"])
            # The resumed delivery is in flight: bind the stop scene
            # (Issue #48) with the same derived branch/worktree the
            # delivery wait uses (never read from a comment).
            set_active_run(
                int(issue["number"]), issue["title"],
                task_branch(
                    source_repo, int(issue["number"]), scene["run_id"],
                ),
                str(worktree_path(
                    config.repo_dir, source_repo,
                    int(issue["number"]), scene["run_id"],
                )),
            )
            # Issue #89: verify the open PR BEFORE any git/Pi mutation
            # (head repo, base, run marker, exact URL of the recovered
            # scene — the pre-#82 resume_delivery check, restored):
            # the wait receives the VERIFIED URL, never the comment
            # string, so a comment can never steer the runner into the
            # wrong PR (Issue #45). A mismatch is terminal: the Issue
            # is marked ai-blocked and the tick stops.
            try:
                pr_url = verify_resumed_pr(
                    scene, issue, config, source_repo,
                )
            except UnrecoverableDeliveryError as exc:
                # Resume verification has already performed the audited
                # label/comment transition. This is an expected external
                # scene condition, not a failed Runner tick (Issue #495).
                event(
                    "resume_pr_handled", level=logging.ERROR,
                    issue=issue["number"], scene_pr=scene["pr_url"],
                    reason=exc,
                )
                return 0
        else:
            result = process_issue(
                issue, config, source_repo, repo_policy,
            )
            # `process_issue` owns task dispatch and reports its outcome;
            # do not repeat task-type predicates here (Issue #281).
            if result.kind not in ("pr", "external-pr"):
                return 0
            pr_url = result.url
            assert pr_url is not None
        # The delivery is not done when the PR is open: hold the slot
        # through review -> merge and release it only after the PR is
        # merged or terminally failed (Issue #39). An external takeover
        # (Issue #608) closes the triage Issue itself after the merge —
        # the contributor's PR carries no `Fixes #N` for it. A resumed
        # delivery derives the scene from its trusted comment, which
        # carries the external marker for an external takeover.
        external_takeover = (
            result.kind == "external-pr" if result is not None
            else bool(scene.get("external"))
        )
        wait_for_delivery(
            pr_url, issue, config, source_repo,
            external_takeover=external_takeover,
        )
    finally:
        slot.release()
        # The delivery is over (merged, terminally failed, or the tick
        # found no work): a stop from here on is idle again (Issue #48).
        clear_active_run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
