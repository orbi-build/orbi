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
short milestone comments (started, plan ready, tests passed/failed, review
findings, PR opened, merged, blocked) notify GitHub Mobile. No human
command, poll or status check is part of the normal workflow.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path

# NOTE (Issue #158, root-caused by Issue #168): the editable CLI
# install refresh below lives in THIS module: the bootstrap chain
# (`orbi.cli` -> `orbi.runner`) must still LOAD in a
# tool env whose installed editable finder predates a packaging
# change. Since the src layout (Issue #168) the finder maps the WHOLE
# package directory `src/orbi/`, so a newly added package
# module is importable WITHOUT any reinstall — the #158 incident
# class is fixed at the root. The refresh remains the safety net for
# packaging-metadata changes (version, dependencies, entry points in
# `pyproject.toml`). `cli_install` is a thin re-export of this
# implementation for the tests only — the bootstrap chain never
# imports it.
from orbi.git_transport import TransportError, check_transport
from orbi.pilot_slots import acquire_slot, slot_dir_for, slot_occupancy
from orbi.pi_recovery import (
    clk_tck,
    find_idle_descendants,
    pid_alive,
    process_start_monotonic,
    signal_pid,
    slots_idle,
    timeout_duration,
    upstream_alive,
)
from orbi.pi_activity import (
    SessionWatcher,
    activity_snapshot,
    format_duration,
    format_end_scene,
    format_run_scene,
    quote_value,
    sanitize,
)
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EPIC_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    P0_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
    TICKET_ONLY_LABEL,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_FIX_NEEDED,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    is_resumable,
    label_patch,
    needs_human_intervention,
)
from orbi.progress import ProgressPublisher, format_elapsed, progress_body
from orbi import runner_health
from orbi.systemd_deploy import (
    TIMER_INSTANCES,
    UnitDriftError,
    check_unit_drift,
    sync_drifted_units,
)


LOGGER = logging.getLogger("orbi.bootstrap")

# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (see review-fix-loop skill: max 5 rounds).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_REVIEW_ROUNDS = 5

# Live activity polling while Pi runs (Issue #24): every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0
# Automatic observability (Issue #18): the GitHub progress comment is
# PATCHed on every activity change and at most every 30 seconds while a
# Pi session runs, so a mobile user sees live progress without any
# command. The journal cadence is the poll interval above.
PI_HEARTBEAT_SECONDS = 30.0
# Idle warning (Issue #18): no model/session activity for 5 minutes
# (and the model is not expected to reply next) logs one `pi_idle`
# warning; the first new session event after it logs `pi_resumed`.
# A slow active model (model_wait, Issue #40) is never reported idle.
PI_IDLE_WARN_SECONDS = 300.0
# Hung-model-request detection (Issue #75, safe recovery since Issue
# #218): while the newest session event is a tool result (model_wait)
# the model is expected to reply next. A real (slow) model keeps
# producing session events; a HUNG model request (the model service
# process alive but the request never completes, or the upstream dead:
# llama/proxy timeout, connection drop) freezes the session JSONL while
# Pi sits in epoll_wait. Once the silence in model_wait reaches this
# threshold the request is declared hung: Pi is killed (the connection
# state is logged as `upstream_alive` evidence, never a veto — process
# alive ≠ responding) and the run fails fast through the normal
# failure path (the slot is released by the kernel when the Runner
# exits, the next tick resumes the same run or claims the next
# Issue). This is NOT a business timeout: it only fires while the
# session file is frozen (stale seconds), never while events keep
# arriving — a slow generation survives. It measures silence between
# COMPLETE session events (Pi does not stream token-level progress
# into the JSONL), not token-level model progress. Configurable since
# Issue #228: the TOML field `model_wait_dead_seconds` overrides this
# default (1800 s, 30 minutes — a slow local model, e.g. Qwen 27B at
# ~17 tokens/s behind a llama-server with a 1200 s request timeout,
# must survive a 10-minute complete message; the pre-#228 default of
# 600 s killed them at exactly 10 minutes, #176/#175/#173/#168).
PI_MODEL_WAIT_DEAD_SECONDS = 1800.0
# Swallowed-model-request probe (Issue #233): while Pi is frozen in
# model_wait the Runner probes the model's /slots endpoint (the
# `model_wait_probe_url` config). When EVERY slot reports idle
# (is_processing=false) for this sustained grace the request was
# SWALLOWED (the model process is alive and the connection ESTABLISHED,
# but nothing is generating — the #231 scene) and the Pi session is
# killed fast, well before the `model_wait_dead_seconds` bound. The
# grace is short (60 s) compared to the dead-request threshold (30 min
# default) so a swallow is recovered in ~1 minute, not ~30; it is long
# enough that a request still being scheduled into the slot (the brief
# accept->schedule window) is never misread as a swallow. The probe is
# a pure bypass (Issue #79): a probe failure is inconclusive and the
# `model_wait_dead_seconds` bound still applies. The TOML field
# `model_wait_probe_seconds` overrides this default.
PI_MODEL_WAIT_PROBE_SECONDS = 60.0
# Idle-stall recovery (Issue #94): a stalled (non-model_wait) session
# is recovered automatically instead of only warning. Measured in idle
# windows of `idle_warn_seconds`: at the first window the pre-idle
# descendants (the hung tools) get SIGTERM (the failure signal reaches
# the model), at the second window a target that survived gets
# SIGKILL, and after `PI_IDLE_RECOVERY_CYCLES` consecutive idle windows
# the Pi session itself is killed and the run fails fast through the
# normal `ai-blocked` path — the slot is never held forever.
PI_IDLE_RECOVERY_CYCLES = 3
# Stop-handler grace (Issue #48): when the Runner is stopped with SIGTERM
# while a Pi delivery is in flight, the handler must not wait forever for
# the Pi child to exit. systemd gives `TimeoutStopSec` (default 90s) before
# it SIGKILLs the whole cgroup, so an unbounded `child.wait()` on a child
# stuck in a model/network call leaves `Result=timeout` after the grace.
# The handler TERMs the child, waits at most this long, then KILLs it so
# it always reaps the child and exits with 128+SIGTERM before systemd's
# own deadline — a clean signal stop, never `failed`/`timeout`.
STOP_CHILD_GRACE_SECONDS = 15.0

# The bootstrap runner streams every Pi session of a run through the same
# live activity pipeline (Issue #24/#40); implement/review share the same
# line format and carry their role (Issue #41: one run_id end to end, the
# roles are steps of the same run). Issue #82 removed the cold-start fixer
# role: the review session fixes findings in the same session, so a run
# has at most two Pi sessions (implement, then review).
ROLE_IMPLEMENT = "implement"
ROLE_REVIEW = "review"
ROLE_RELEASE = "release"
ROLE_TICKET = "ticket"

# Run correlation (Issue #41): one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_CURRENT_RUN_ID: str | None = None

# GitHub labels are the only state store (Issue #45). The delivery
# lifecycle states (`ai-in-progress`, `ai-pr-opened`, `ai-fix-needed`,
# `ai-merged`, `ai-blocked`), the scheduling-metadata labels (`p0`,
# `bug`, `ai-epic`, `ai-release`, `ai-ticket-only`), the event → label
# patch transition rules, and the pickup/resume/human-intervention
# decisions all live in `orbi.delivery_labels` (Issue #175) — the single
# source of truth. They are imported above; `p0`/`bug`/`ai-epic` are
# scheduling metadata, never delivery lifecycle states.
# The machine-readable section a release Issue body must carry (Issue
# #98): `- version:`, `- base_branch:`, `- test_command:` and
# `- scope:` with `  - #N` items. Parsed strictly — a missing or
# malformed declaration fails fast, never guessed.
RELEASE_SECTION = "## Release"
# The declared `test_command` runs in a clean worktree at the release
# commit wrapped in `timeout <seconds> bash -c ...` (Issue #95): a test
# that does not terminate within the deadline is a broken path, never
# ignorable noise.
RELEASE_TEST_TIMEOUT_SECONDS = 1800
# Release CI wait (Issue #268): the release commit is born from the last
# delivery PR merge, so its CI is almost always still running when the
# gate checks it — a pending check (queued/in_progress) is an
# intermediate state, not a failure. The gate waits for completion up to
# this limit and decides on the FINAL conclusions; a wait timeout is its
# own failure reason, never reported as a CI failure. The TOML field
# `release_ci_wait_seconds` overrides the default (the #228 pattern);
# the default matches RELEASE_TEST_TIMEOUT_SECONDS, the existing release
# timeout convention.
RELEASE_CI_WAIT_SECONDS = 1800
# Poll cadence while waiting: one `release_waiting_ci` journal line plus
# one progress-comment PATCH per poll — the same 30s GitHub cadence as
# the live progress heartbeat (PI_HEARTBEAT_SECONDS).
RELEASE_CI_POLL_INTERVAL = 30.0
# Repair-Issue GitHub operations are a convenience path, but they still run
# during terminal failure handling. Bound them so an unavailable API cannot
# hold the Runner indefinitely (Issue #95).
REPAIR_ISSUE_TIMEOUT_SECONDS = 30

# Only comments posted by a repo maintainer are trusted to carry the
# recovery scene: a public comment (authorAssociation=NONE) must never
# steer the runner into an arbitrary local worktree, branch or PR
# (Issue #45 review, BLOCKER). A missing association is never trusted.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({
    "OWNER", "MAINTAINER", "MEMBER", "COLLABORATOR",
})


class ModelWaitDeadError(RuntimeError):
    """The hung-model-request recovery (Issue #218/#228) killed the Pi
    session: the model request is HUNG (the model service process is alive
    but the request never completes, the session JSONL froze in
    `model_wait` past `model_wait_dead_seconds`).

    This is a CLASSIFIED, AI-recoverable delivery failure (Issue #227):
    the worktree keeps the interrupted work and the run state file is
    intact, so `process_issue` keeps the Issue `ai-in-progress` and the
    next tick's in-flight restart scan resumes the SAME run (same run id,
    branch, worktree, progress comment). It is never `ai-blocked` and
    never an unclassified top-level exception: the recovery stays
    fail-fast (Pi killed, the slot released by the tick ending) but its
    terminal outcome goes through the recoverable delivery path."""


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


def is_unrecoverable_failure(exc: BaseException) -> bool:
    """Issue #50: classify one delivery failure.

    True ONLY for an explicit `UnrecoverableDeliveryError` (an external
    precondition the AI cannot safely judge or fix). Every other
    failure — Pi execution failure (pi exit, upstream dead, idle
    recovery), timeout, runner exception, missing/malformed verdict,
    missing worktree, unpushed local commit, gate failure — is
    recoverable: the Issue goes to `ai-fix-needed` and the next timer
    resumes the same run, branch, worktree and PR. A single failure
    must never permanently stop an Issue.
    """
    return isinstance(exc, UnrecoverableDeliveryError)


class RunIdFilter(logging.Filter):
    """Prefix every log message with the current `[run_id]`, if bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT_RUN_ID is not None:
            record.msg = f"[{_CURRENT_RUN_ID}] {record.msg}"
        return True


LOGGER.addFilter(RunIdFilter())
# Issue #266: the health check's journal lines carry the same `[run_id]`
# prefix as every other Runner line (the RunIdFilter is attached per
# logger; the health module must not import this one — circular).
runner_health.LOGGER.addFilter(RunIdFilter())


def validate_run_id(run_id: object) -> str:
    """Fail fast unless `run_id` is the 8-hex id of one task attempt."""
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


def set_run_id(run_id: str) -> None:
    """Bind one task attempt: every later journal line carries `[run_id]`."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = validate_run_id(run_id)


def current_run_id() -> str | None:
    """Return the run id bound to this tick, or None before the claim."""
    return _CURRENT_RUN_ID


def run_marker(run_id: str) -> str:
    """Return the stable machine-readable run marker for GitHub text."""
    return f"<!-- orbi:run={validate_run_id(run_id)} -->"

# Stop scene (Issue #48): when systemd (or any caller) stops the Runner
# with SIGTERM, the journal must show which Issue context was active
# BEFORE systemd's generic "Stopped" line, and the live Pi child must be
# shut down (no orphan Pi). The context is bound while a delivery is in
# flight (after the claim, or after a resumed scene is bound) and cleared
# when the delivery ends. It carries no new id: the run id is the
# existing `_CURRENT_RUN_ID`, and the phase/session come from the
# existing activity snapshot of the worktree's `.pi-session`.
_ACTIVE_RUN: dict | None = None


def set_active_run(issue: int, title: str, branch: str, worktree: str) -> None:
    """Bind the in-flight delivery scene for the stop handler (Issue #48)."""
    global _ACTIVE_RUN
    _ACTIVE_RUN = {
        "issue": int(issue),
        "title": title,
        "branch": branch,
        "worktree": worktree,
        "pi": None,
    }


def set_active_pi(process: subprocess.Popen | None) -> None:
    """Track the live Pi child of the in-flight delivery (Issue #48).

    `stream_pi` calls it after the child is spawned (and again with None
    after the child is reaped), so the stop handler signals exactly the
    child that is alive — never an already-exited process. Without a
    bound run (unit tests call `stream_pi` directly) it is a no-op.
    """
    if _ACTIVE_RUN is not None:
        _ACTIVE_RUN["pi"] = process


def clear_active_run() -> None:
    """No delivery in flight anymore (Issue #48)."""
    global _ACTIVE_RUN
    _ACTIVE_RUN = None


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
    run = _ACTIVE_RUN
    if run is None:
        LOGGER.info("run_stopped result=idle")
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
        LOGGER.info(
            "run_stopping issue=%s title=%s signal=%s phase=%s "
            "branch=%s worktree=%s session=%s",
            run["issue"], quote_value(run["title"]),
            signal.Signals(signum).name, quote_value(phase),
            quote_value(run["branch"]), quote_value(run["worktree"]),
            quote_value(session),
        )
        child = run["pi"]
        _shutdown_child(child)
        LOGGER.info(
            "run_stopped issue=%s result=interrupted", run["issue"],
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


def load_config(path: Path, *, check_provider_api_keys: bool = True) -> dict:
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
    base_branch = data.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("base_branch must be a non-empty string")
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
    # Repair-Issue creation (Issue #202) is deliberately opt-in. A
    # release remains blocked either way; this merely dispatches a normal
    # ai-ready bug Issue when a release test command reports evidence.
    auto_repair_issues = data.get("auto_repair_issues", False)
    if not isinstance(auto_repair_issues, bool):
        raise ValueError("auto_repair_issues must be a boolean")
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
    # Hung-model-request threshold (Issue #228): the model_wait dead
    # silence is configurable; omitted -> PI_MODEL_WAIT_DEAD_SECONDS
    # (default 1800 s, 30 minutes). It measures silence between
    # complete session events, never token-level model progress.
    model_wait_dead_seconds = _model_wait_dead_seconds(data)
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
    pi_providers_data = (
        _load_pi_providers(
            pi_providers_path, pi_provider, pi_model,
            deploy_home / ".orbi" / "env",
            check_api_key=check_provider_api_keys,
        )
        if pi_providers_path is not None else None
    )
    return {
        "source_repos": source_repos,
        "repo_dir": repo_dir,
        "deploy_home": deploy_home,
        "health_alert_repo": health_alert_repo,
        "workspace_root": _config_path(data.get("workspace_root", ".."), base),
        # Issue #330: when deploy_home is EXPLICIT the prompt defaults
        # live in the deployment home (the delivery checkout may be a
        # foreign repo without them); an explicit prompt path still
        # resolves against the config file dir. deploy_home absent ->
        # the original config-file-dir resolution (bootstrap unchanged).
        "prompt": _config_path(
            data.get("prompt", "prompt.md"),
            base if "prompt" in data
            else (deploy_home if deploy_home_raw is not None else base),
        ),
        "prompt_review": _config_path(
            data.get("prompt_review", "prompt_review.md"),
            base if "prompt_review" in data
            else (deploy_home if deploy_home_raw is not None else base),
        ),
        "skills": [_config_path(item, base) for item in data.get("skills", [])],
        "context_files": [
            _config_path(item, base) for item in data.get("context_files", [])
        ],
        "base_branch": base_branch,
        "active_milestone": active_milestone,
        "auto_repair_issues": auto_repair_issues,
        "max_concurrency": max_concurrency,
        "slot_dir": slot_dir_for(repo_dir),
        "pi_provider": pi_provider,
        "pi_model": pi_model,
        "pi_thinking": pi_thinking,
        "model_wait_dead_seconds": model_wait_dead_seconds,
        "model_wait_probe_url": model_wait_probe_url,
        "model_wait_probe_seconds": model_wait_probe_seconds,
        "release_ci_wait_seconds": release_ci_wait_seconds,
        "pi_providers": pi_providers_path,
        "pi_providers_data": pi_providers_data,
        "pi_provider_key_finding": getattr(
            _load_pi_providers, "last_key_finding", None,
        ),
        # Multi-repo registry (Issue #134): the explicit per-repo entries
        # (name, path, github, base_branch). Absent section -> [] so the
        # single-repo config keeps its exact shape and flow.
        "repositories": parse_repositories(data.get("repositories", []), base),
    }


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

    Omitted -> `RELEASE_CI_WAIT_SECONDS` (default 1800 s, matching the
    RELEASE_TEST_TIMEOUT_SECONDS convention). Present -> must be a
    finite positive number (int or float); booleans, zero, negative,
    NaN/infinity and non-numeric values fail fast at config load with
    the field name and the concrete reason.
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


def _check_pi_provider_api_key(path: Path, provider_id: str,
                               entry: dict, env_file: Path) -> None:
    """Fail fast when an `apiKey` env-var reference is unresolved."""
    finding = _pi_provider_api_key_finding(path, provider_id, entry, env_file)
    if finding:
        raise ValueError(finding["error"])


def _expand_pi_api_key_refs(api_key: str) -> str:
    """Resolve `$VAR` / `${VAR}` references in an `apiKey` (Issue #303).

    Same reference syntax `_check_pi_provider_api_key` validates (Pi's
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


def prepare_pi_agent_dir(worktree: Path, config: dict) -> Path | None:
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
    providers_data = config.get("pi_providers_data")
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
    pi_provider = config.get("pi_provider")
    pi_model = config.get("pi_model")
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


def _pi_model_args(config: dict) -> list[str]:
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
        value = config.get(key)
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
        repos.append({
            "name": entry["name"],
            "path": _config_path(entry["path"], base),
            "github": entry["github"],
            "base_branch": entry["base_branch"],
        })
    return repos


def render_prompt(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def validate_config(config: dict) -> None:
    if not config["repo_dir"].is_dir():
        raise FileNotFoundError(config["repo_dir"])
    # Issue #330: the deployment home (CLI install source, unit templates,
    # labels.toml, prompt defaults) must exist too — a missing home fails
    # the start fast, like a missing delivery checkout.
    if not config["deploy_home"].is_dir():
        raise FileNotFoundError(config["deploy_home"])
    for path in [
        config["prompt"], config["prompt_review"],
        *config["skills"], *config["context_files"],
    ]:
        if not path.is_file():
            raise FileNotFoundError(path)
    # Multi-repo registry (Issue #134): every registered path must exist
    # and be a Git checkout — a `.git` directory (a plain checkout) or a
    # `.git` file (a linked worktree). Absent key -> no registry, so a
    # single-repo config keeps its exact flow.
    for repo in config.get("repositories", []):
        path = repo["path"]
        if not path.is_dir():
            raise FileNotFoundError(path)
        if not (path / ".git").exists():
            raise ValueError(
                f"repositories entry {repo['name']!r}: {path} "
                "is not a git checkout"
            )


def single_line(value: str) -> str:
    """Flatten a log value to one journal line (Issue #143).

    A command argument may carry line breaks (the multi-line progress
    comment body behind `gh api ... --field body=...`); emitted verbatim,
    they split one `command=` log into several systemd journal lines with
    the same timestamp and PID. Escape each line break to the visible
    two-character sequence `\\n` so the field content stays readable on
    one line. This only changes the log display — the real command is
    never modified.
    """
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int | None = None,
                log_command: list[str] | None = None,
                log_stdout: bool = False) -> str:
    """Run one external command; log context and fail fast on any error."""
    LOGGER.info(
        "command=%s cwd=%s",
        single_line(" ".join(log_command or command)), cwd or Path.cwd(),
    )
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "command_failed returncode=%s stdout=%s stderr=%s",
            exc.returncode, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        LOGGER.error(
            "command_timeout timeout=%s stdout=%s stderr=%s",
            timeout, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except OSError as exc:
        LOGGER.error("command_spawn_failed error=%s", exc)
        raise
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    if log_stdout and result.stdout:
        LOGGER.info("stdout=%s", result.stdout.rstrip())
    return result.stdout.strip()


def parse_issue_array(raw: str) -> list[dict]:
    """Return the issue array from gh's JSON output."""
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("issue list must be a JSON array")
    return issues


def parse_issue_list(raw: str) -> dict | None:
    """Return the first issue from gh's JSON array, or None when idle."""
    issues = parse_issue_array(raw)
    return issues[0] if issues else None


def open_blocker_numbers(issue: dict) -> list[int]:
    """Return the numbers of the issue's OPEN native GitHub blockers.

    `gh issue list --json blockedBy` (gh 2.94+) carries the native
    dependency relation as `{"nodes": [...], "totalCount": N}`. GitHub
    keeps a relation listed after its blocker closes (the node then
    carries `state: "CLOSED"` and is inert — verified against the live
    API, Issue #54), so only OPEN blockers actually block: a closed
    blocker clears the dependency without any runner-side bookkeeping,
    and the next tick claims the Issue. A node without an explicit
    `state` counts as open (claiming a possibly-blocked Issue costs a
    full run; waiting one tick does not). A missing or malformed field
    means "no known blockers" (fail open): an API shape change must
    never deadlock the queue.
    """
    blocked_by = issue.get("blockedBy")
    if not isinstance(blocked_by, dict):
        return []
    nodes = blocked_by.get("nodes")
    if not isinstance(nodes, list):
        return []
    numbers: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        number = node.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if node.get("state", "OPEN") != "OPEN":
            continue
        numbers.append(number)
    return numbers


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


def ready_searches(active_milestone: str | None = None) -> tuple[str, str, str]:
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
    return (
        f"label:ai-ready label:{P0_LABEL}{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:ai-ready label:bug{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:ai-ready{scope} {READY_SCAN_EXCLUSIONS}",
    )


def release_fallback_search(active_milestone: str | None = None) -> str:
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
    return (
        f"label:{READY_LABEL} label:{RELEASE_LABEL}{scope} "
        f"{READY_SCAN_EXCLUSIONS}"
    )


def issue_priority(issue: dict) -> str:
    """Return the pickup priority of one issue (Issue #101).

    `p0` when the issue carries the `p0` label, `normal` otherwise.
    The ready/in-flight/resumable scans fetch `labels` (verified
    against `gh issue list --help`: `labels` is a supported JSON
    field, an array of `{name, ...}` nodes), so this is a pure
    function of the scanned issue — no extra gh call. A missing or
    malformed `labels` field fails to `normal` (like the blockedBy
    field fails open): a P0 misread as normal only loses its ordering
    for one run, never the delivery.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return "normal"
    for label in labels:
        if isinstance(label, dict) and label.get("name") == P0_LABEL:
            return "p0"
    return "normal"


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


def parse_release_declaration(body: str) -> dict:
    """Strictly parse the `## Release` section of a release Issue body.

    The declaration is the machine-readable contract of a Release task
    (Issue #98) — the only state a release run reads from the Issue
    body (checkboxes are never parsed):

    ```markdown
    ## Release

    - version: v0.3.0
    - base_branch: main
    - test_command: <shell command>
    - scope:
      - #123
      - #124
    ```

    or, instead of the hand-listed `scope`, the scope derived from the
    Milestone whose title is the release version (Issue #253):

    ```markdown
    - scope_from_milestone: v0.3.0
    ```

    `version` is the exact tag name (no spaces), `base_branch` the
    branch the release commit is frozen from, `test_command` the
    shell command that must pass in a clean worktree at the release
    commit, and `scope` the Issue/PR numbers verified one by one.
    Exactly one of `scope` / `scope_from_milestone` must be present:
    both (conflict) or neither fails fast. `scope_from_milestone` is
    the Milestone TITLE (no spaces); its scope is derived later by
    `derive_release_scope_from_milestone`. Every other deviation fails
    fast with the concrete field: missing section, missing or
    duplicated field, unknown key, empty value, empty scope or a
    malformed scope item. No guessing.
    """
    if not isinstance(body, str):
        raise ValueError("release declaration body must be a string")
    lines = body.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.strip() == RELEASE_SECTION
        )
    except StopIteration:
        raise ValueError(
            f"release Issue body is missing the `{RELEASE_SECTION}` "
            "section with version, base_branch, test_command and scope "
            "or scope_from_milestone"
        ) from None
    section: list[str] = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith("## "):
            break
        section.append(line)
    fields: dict[str, str] = {}
    scope: list[int] = []
    scope_open = False
    for line in section:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            content = stripped[2:].strip()
            if scope_open and re.fullmatch(r"#\d+", content):
                number = int(content[1:])
                if number < 1:
                    raise ValueError(
                        f"release declaration scope item {content!r} "
                        "must be a positive Issue or PR number"
                    )
                scope.append(number)
                continue
            if scope_open and ":" not in content:
                raise ValueError(
                    f"release declaration scope item {content!r} is "
                    "malformed (expected `  - #N`)"
                )
            # A `- key: value` line closes the scope list (and a
            # duplicated or unknown key is caught below).
            scope_open = False
            key, sep, value = content.partition(":")
            if not sep:
                raise ValueError(
                    f"release declaration field {key.strip()!r} is "
                    "malformed (expected `- key: value`)"
                )
            key = key.strip()
            value = value.strip()
            if key in fields:
                raise ValueError(
                    f"release declaration field {key!r} is duplicated"
                )
            if key == "scope":
                if value:
                    raise ValueError(
                        "release declaration `scope` must be a list of "
                        "`  - #N` items, not an inline value"
                    )
                scope_open = True
                fields["scope"] = ""
            elif key in ("version", "base_branch", "test_command",
                         "scope_from_milestone"):
                fields[key] = value
            else:
                raise ValueError(
                    f"release declaration has the unknown field {key!r} "
                    "(expected version, base_branch, test_command, "
                    "scope or scope_from_milestone)"
                )
        elif scope_open:
            raise ValueError(
                f"release declaration scope item {stripped!r} is "
                "malformed (expected `  - #N`)"
            )
        else:
            raise ValueError(
                f"release declaration line {stripped!r} is not a "
                "`- key: value` field or a scope item"
            )
    for key in ("version", "base_branch", "test_command"):
        if key not in fields:
            raise ValueError(
                f"release declaration is missing the `{key}` field"
            )
        if not fields[key]:
            raise ValueError(
                f"release declaration field `{key}` is empty"
            )
    if "scope" in fields and "scope_from_milestone" in fields:
        raise ValueError(
            "release declaration must use exactly one of `scope` or "
            "`scope_from_milestone`, not both"
        )
    if "scope" not in fields and "scope_from_milestone" not in fields:
        raise ValueError(
            "release declaration is missing the `scope` field or the "
            "`scope_from_milestone` field (exactly one of the two)"
        )
    if "scope" in fields and not scope:
        raise ValueError(
            "release declaration `scope` must list at least one "
            "`  - #N` Issue or PR number"
        )
    for key in ("version", "base_branch"):
        if any(ch.isspace() for ch in fields[key]):
            raise ValueError(
                f"release declaration field `{key}` must not contain "
                "spaces"
            )
    if "scope_from_milestone" in fields:
        if not fields["scope_from_milestone"]:
            raise ValueError(
                "release declaration field `scope_from_milestone` is "
                "empty"
            )
        if any(ch.isspace() for ch in fields["scope_from_milestone"]):
            raise ValueError(
                "release declaration field `scope_from_milestone` must "
                "not contain spaces"
            )
    return {
        "version": fields["version"],
        "base_branch": fields["base_branch"],
        "test_command": fields["test_command"],
        "scope": scope,
        "scope_from_milestone": fields.get("scope_from_milestone"),
    }


def verify_release_scope(repo: str, scope: list[int], repo_dir: Path,
                         release_commit: str) -> list[str]:
    """Verify every release scope item ONE BY ONE (Issue #98).

    The scope is verified against GitHub, never by parsing Issue-body
    checkboxes: each number is probed as a PR first (`gh pr view` —
    which exits 1 with the real "Could not resolve to a PullRequest"
    error when the number is an Issue, verified against the live CLI)
    and, failing that, as an Issue (`gh issue view`). A PR must be
    `MERGED` and its merge commit must be an ancestor of the frozen
    release commit; an Issue must be `CLOSED`. This ancestor check proves
    the scoped PR is actually contained in the tag, rather than merely
    having been merged into some other branch. An item that is neither, a
    PR that is not merged/in the release base, or an Issue that is not
    closed fails fast with the concrete number and state. A real `gh` or
    git failure (auth, rate limit, missing object) is re-raised, never
    misread as "not a PR".
    """
    evidence: list[str] = []
    for number in scope:
        try:
            raw = run_command([
                "gh", "pr", "view", str(number), "--repo", repo,
                "--json", "number,state,mergeCommit",
            ])
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to a PullRequest" not in (exc.stderr or ""):
                raise
            raw = None
        if raw is not None:
            pr = json.loads(raw)
            state = pr.get("state")
            if state != "MERGED":
                raise RuntimeError(
                    f"release scope PR #{number} is not merged "
                    f"(state={state})"
                )
            merge_commit = (pr.get("mergeCommit") or {}).get("oid")
            if not merge_commit:
                raise RuntimeError(
                    f"release scope PR #{number} is merged but has no "
                    "merge commit evidence"
                )
            try:
                run_command(
                    ["git", "merge-base", "--is-ancestor", merge_commit,
                     release_commit],
                    cwd=repo_dir,
                )
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 1:
                    raise
                raise RuntimeError(
                    f"release scope PR #{number} merge commit {merge_commit} "
                    f"is not contained in release commit {release_commit}"
                ) from exc
            evidence.append(
                f"PR #{number} merged (mergeCommit={merge_commit})"
            )
            continue
        try:
            raw = run_command([
                "gh", "issue", "view", str(number), "--repo", repo,
                "--json", "number,state",
            ])
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to an Issue" not in (exc.stderr or ""):
                raise
            raise RuntimeError(
                f"release scope item #{number} is neither a PR nor an "
                "Issue"
            ) from exc
        issue = json.loads(raw)
        state = issue.get("state")
        if state != "CLOSED":
            raise RuntimeError(
                f"release scope Issue #{number} is not closed "
                f"(state={state})"
            )
        evidence.append(f"Issue #{number} closed")
    return evidence


def derive_release_scope_from_milestone(repo: str,
                                        milestone_title: str) -> tuple[list[int], list[str]]:
    """Derive the release scope from a Milestone (Issue #253).

    The release scope is the Milestone's COMPLETED deliveries: every
    Issue with state=closed and every PR with state=merged under the
    Milestone whose title is EXACTLY `milestone_title` (the same
    exact-title rule as `close_release_milestone` — never guessed,
    never fuzzy-matched, never a different Milestone):

    - no Milestone with that exact title -> fail fast;
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - open Issues/PRs are NEVER part of the scope (unfinished work is a
      human decision point) but are returned as a separate evidence
      list so the release run surfaces them instead of swallowing them.

    Returns (scope numbers sorted ascending, open-item evidence
    strings). A real `gh` failure (auth, rate limit, API error)
    propagates unchanged — a scope that cannot be derived is a failed
    release, never a guessed one.
    """
    raw = run_command([
        "gh", "api", f"repos/{repo}/milestones?state=all", "--paginate",
    ])
    milestones = parse_issue_array(raw)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == milestone_title
    ]
    if not matches:
        raise RuntimeError(
            f"release milestone derivation: no Milestone with the exact "
            f"title {milestone_title!r} in {repo} — never guessed or "
            "fuzzy-matched"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"release milestone derivation: {len(matches)} Milestones "
            f"share the exact title {milestone_title!r} in {repo} — "
            "ambiguous, refusing to guess which to derive from"
        )
    number = matches[0].get("number")

    def items(kind: str, state: str) -> list[dict]:
        raw = run_command([
            "gh", "api",
            f"repos/{repo}/{kind}?state={state}&milestone={number}",
            "--paginate",
        ])
        return [item for item in parse_issue_array(raw)
                if isinstance(item, dict)]

    closed_issues = [
        item for item in items("issues", "closed")
        if "pull_request" not in item
    ]
    open_issues = [
        item for item in items("issues", "open")
        if "pull_request" not in item
    ]
    # The `pulls` list query carries `merged_at` at the top level (a
    # closed-but-unmerged PR has `merged_at: null`) — verified against
    # the live REST contract; there is no nested `pull_request` key.
    merged_prs = [
        item for item in items("pulls", "closed")
        if item.get("merged_at")
    ]
    open_prs = items("pulls", "open")

    scope = sorted(
        int(item["number"])
        for item in closed_issues + merged_prs
        if isinstance(item.get("number"), int)
    )
    open_evidence = [
        f"open Issue #{item.get('number')} {item.get('title')}"
        for item in open_issues
    ] + [
        f"open PR #{item.get('number')} {item.get('title')}"
        for item in open_prs
    ]
    return scope, open_evidence


RELEASE_CHANGELOG_CATEGORIES = (
    "Features", "Reliability and recovery", "Deployment and operations",
    "Observability", "Documentation", "Bug fixes",
)


def release_changelog_category(item: dict) -> str:
    """Classify one live scoped Issue into a stable reader-facing group."""
    labels = item.get("labels")
    label_names = {
        label.get("name", "").lower() for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    } if isinstance(labels, list) else set()
    title = item.get("title")
    text = title.lower() if isinstance(title, str) else ""
    if "documentation" in label_names or any(
        term in text for term in ("documentation", "docs", "readme", "文档")
    ):
        return "Documentation"
    if any(term in text for term in (
        "deploy", "deployment", "systemd", "install", " cli", "ssh",
        "service", "timer", "packaging", "setup",
    )):
        return "Deployment and operations"
    if any(term in text for term in (
        "recovery", "recover", "resume", "timeout", "concurren", "reliab",
        "stale", "dead", "hang", "lock",
    )):
        return "Reliability and recovery"
    if any(term in text for term in (
        "observability", "prometheus", "grafana", "dashboard", "metrics",
        "exporter", "journal", "progress",
    )):
        return "Observability"
    if "bug" in label_names:
        return "Bug fixes"
    return "Features"


def build_release_changelog(repo: str, scope: list[int]) -> str:
    """Render deterministic readable notes from live scoped Issue evidence.

    The official ``gh issue view --json`` contract supplies each Issue's
    title, body, URL, labels, and closing PR references.  A title is the
    concise change description; when it is absent, the first non-empty body
    line is usable summary evidence.  Missing or malformed evidence is an
    unsafe release input and fails before a tag or Release is created.
    """
    grouped: dict[str, list[tuple[int, str]]] = {
        category: [] for category in RELEASE_CHANGELOG_CATEGORIES
    }
    for number in scope:
        raw = run_command([
            "gh", "issue", "view", str(number), "--repo", repo, "--json",
            "number,title,body,url,labels,closedByPullRequestsReferences",
        ])
        item = json.loads(raw)
        issue_url = item.get("url")
        issue_path = f"https://github.com/{repo}/issues/{number}"
        pull_path = f"https://github.com/{repo}/pull/{number}"
        if item.get("number") != number or issue_url not in (issue_path, pull_path):
            raise ValueError(
                f"release changelog Issue #{number} has malformed Issue evidence"
            )
        title = item.get("title")
        body = item.get("body")
        summary = title.strip() if isinstance(title, str) else ""
        if not summary and isinstance(body, str):
            summary = next((line.strip() for line in body.splitlines()
                            if line.strip()), "")
        if not summary or re.fullmatch(r"Issue #\d+ closed", summary, re.I):
            raise ValueError(
                f"release changelog Issue #{number} has no usable title/summary evidence"
            )
        source_kind = "PR" if issue_url == pull_path else "Issue"
        links = [f"[{source_kind} #{number}]({issue_url})"]
        pull_requests = item.get("closedByPullRequestsReferences")
        if not isinstance(pull_requests, list):
            raise ValueError(
                f"release changelog Issue #{number} has malformed PR evidence"
            )
        for pull_request in sorted(pull_requests, key=lambda pr: pr.get("number", 0)
                                   if isinstance(pr, dict) else 0):
            pr_number = pull_request.get("number") if isinstance(pull_request, dict) else None
            pr_url = pull_request.get("url") if isinstance(pull_request, dict) else None
            if (not isinstance(pr_number, int) or pr_number < 1 or
                    pr_url != f"https://github.com/{repo}/pull/{pr_number}"):
                raise ValueError(
                    f"release changelog Issue #{number} has malformed PR evidence"
                )
            links.append(f"[PR #{pr_number}]({pr_url})")
        grouped[release_changelog_category(item)].append(
            (number, f"- {summary} ({'; '.join(links)})")
        )
    sections = ["## Changelog"]
    for category in RELEASE_CHANGELOG_CATEGORIES:
        entries = grouped[category]
        if entries:
            sections.extend(["", f"### {category}", "",
                             *(entry for _, entry in sorted(entries))])
    return "\n".join(sections)


def check_release_gates(repo: str, base_branch: str, release_commit: str,
                        release_number: int, *,
                        ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS,
                        on_wait: Callable[[str], None] | None = None,
                        ) -> list[str]:
    """Enforce the pre-release gates (Issue #98) and return their evidence.

    Three gates, each checked against GitHub (never against local
    state), each failure raising with the concrete offender:

    1. No open Issue still carries `ai-in-progress`, `ai-pr-opened`
       or `ai-fix-needed` — the release Issue itself is excluded (it
       carries `ai-in-progress` while the release runs).
    2. CI on the release commit is green: every check run reported by
       the GitHub API for the commit is `completed` with a
       `success`/`neutral`/`skipped` conclusion (a failing, cancelled
       or error check fails the gate; no check runs at all is recorded
       as such, not invented). A PENDING check (queued/in_progress —
       the release commit is born from the last delivery merge, so its
       CI is almost always still running, Issue #268) is not a
       conclusion: the gate polls until every check completes (one
       `release_waiting_ci` journal line per poll, the wait reflected
       through `on_wait`), then decides on the final conclusions.
       Waiting past `ci_wait_seconds` fails with its own timeout
       reason, explicitly distinct from a CI failure.
    3. No open PR targets the base branch — an open PR against the
       release base would make the released commit non-final.

    A real `gh` failure (auth, rate limit, API error) propagates
    unchanged — a gate that cannot be checked is a failed gate.
    """
    evidence: list[str] = []
    for label in (IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL):
        raw = run_command([
            "gh", "issue", "list", "--repo", repo, "--label", label,
            "--state", "open", "--json", "number", "--limit", "50",
        ])
        for item in json.loads(raw):
            number = int(item["number"])
            if number == release_number:
                continue
            raise RuntimeError(
                f"release gate: Issue #{number} still carries "
                f"{label} — finish or block that delivery before "
                "releasing"
            )
    evidence.append(
        "no open Issue carries "
        f"{IN_PROGRESS_LABEL} / {PR_OPENED_LABEL} / {FIX_NEEDED_LABEL}"
    )
    def fetch_check_runs() -> list[dict]:
        return json.loads(run_command([
            "gh", "api", f"repos/{repo}/commits/{release_commit}/check-runs",
            "--jq", ".check_runs",
        ]))

    check_runs = fetch_check_runs()
    waited = 0.0
    while True:
        pending = [
            f"check '{check.get('name')}' is "
            f"{check.get('status')}/{check.get('conclusion')}"
            for check in check_runs if check.get("status") != "completed"
        ]
        if not pending:
            break
        detail = ", ".join(pending)
        LOGGER.info(
            "issue=%s release_waiting_ci commit=%s pending=%s "
            "waited=%ds limit=%ds",
            release_number, release_commit, detail,
            int(waited), int(ci_wait_seconds),
        )
        if on_wait is not None:
            on_wait(
                f"{detail}; waited {int(waited)}s / "
                f"{int(ci_wait_seconds)}s"
            )
        if waited >= ci_wait_seconds:
            raise RuntimeError(
                f"release gate: waiting for CI on the release commit "
                f"{release_commit} timed out after "
                f"{int(ci_wait_seconds)}s (still pending: {detail}) "
                "— wait timeout, not a CI failure"
            )
        step = min(RELEASE_CI_POLL_INTERVAL, ci_wait_seconds - waited)
        time.sleep(step)
        waited += step
        check_runs = fetch_check_runs()
    for check in check_runs:
        name = check.get("name")
        status = check.get("status")
        conclusion = check.get("conclusion")
        if status != "completed" or conclusion not in (
            "success", "neutral", "skipped",
        ):
            raise RuntimeError(
                f"release gate: CI check '{name}' is {status}/{conclusion} "
                f"on the release commit {release_commit}"
            )
    if check_runs:
        evidence.append(
            f"CI on the release commit: {len(check_runs)} check(s) all "
            "success/neutral/skipped"
            + (f" (waited {int(waited)}s for pending checks)" if waited
               else "")
        )
    else:
        evidence.append(
            f"CI on the release commit: no check runs on "
            f"{release_commit} (nothing to gate)"
        )
    raw = run_command([
        "gh", "pr", "list", "--repo", repo,
        "--search", f"is:pr is:open base:{base_branch}",
        "--json", "number,headRefName", "--limit", "50",
    ])
    for pr in json.loads(raw):
        raise RuntimeError(
            f"release gate: PR #{pr.get('number')} "
            f"(head {pr.get('headRefName')}) is still open against "
            f"{base_branch}"
        )
    evidence.append(f"no open PR targets {base_branch}")
    return evidence


def run_release_tests(worktree: Path, test_command: str,
                      timeout_seconds: int) -> None:
    """Run the declared release test command in the release worktree.

    The command is a shell string (it may chain steps with `&&`), so
    it runs through `bash -c` wrapped in `timeout <seconds>` (Issue
    #95). Its success is followed by the repository's tiered coverage
    gate (Issue #234: the report shows both real numbers, and
    coverage_gate.py enforces line >= 95% and branch >= 95% checked
    separately): a declaration such as `true` or a bare `pytest` must
    not let a release claim the coverage contract. A test that does not
    terminate within the deadline fails fast with `timeout`'s exit 124
    — never ignorable noise or a second unbounded attempt.
    """
    coverage_gate = (
        "/usr/bin/python3 -m coverage report --show-missing && "
        "/usr/bin/python3 coverage_gate.py"
    )
    run_command(
        ["timeout", str(timeout_seconds), "bash", "-c",
         f"{test_command} && {coverage_gate}"],
        cwd=worktree,
    )


def repair_signature(source_issue: int, run_id: str, release_commit: str,
                     command: str, evidence: str) -> str:
    """Return the stable identity for one evidenced release-test failure."""
    scene = "\0".join((str(source_issue), run_id, release_commit, command, evidence))
    return hashlib.sha256(scene.encode("utf-8")).hexdigest()[:16]


def create_repair_issue(*, repo: str, source_issue: int, run_id: str,
                        release_commit: str, command: str,
                        evidence: str) -> str:
    """Create or find one normal repair Issue for a release test failure.

    GitHub Issue search's documented ``in:body`` qualifier searches the
    stable signature written into every generated body. This makes the
    deduplication external, auditable, and safe across process restarts.
    """
    signature = repair_signature(
        source_issue, run_id, release_commit, command, evidence,
    )
    marker = f"orbi-repair-signature={signature}"
    raw = run_command([
        "timeout", str(REPAIR_ISSUE_TIMEOUT_SECONDS),
        "gh", "issue", "list", "--repo", repo, "--state", "all",
        "--search", f'in:body "{marker}"', "--json", "number,url", "--limit", "1",
    ])
    existing = parse_issue_array(raw)
    if existing:
        return existing[0]["url"]
    body = "\n".join([
        "## 自动生成的 Release 测试修复",
        "",
        f"- source Issue: #{source_issue}",
        f"- run_id={run_id}",
        f"- commit: `{release_commit}`",
        f"- {marker}",
        "",
        "## Reproduce",
        "",
        "```bash",
        command,
        "```",
        "",
        "## Captured evidence",
        "",
        "```text",
        evidence,
        "```",
        "",
        "该 Issue 由正常 `ai-ready` → PR → review → merge 流程处理；原 Release "
        "保持 `ai-blocked`，必须在修复合并后显式重新运行 Release gate。",
    ])
    return run_command([
        "timeout", str(REPAIR_ISSUE_TIMEOUT_SECONDS),
        "gh", "issue", "create", "--repo", repo,
        "--title", f"修复 Release #{source_issue} 测试门禁失败",
        "--body", body, "--label", "ai-ready", "--label", "bug",
    ])


def release_test_evidence(exc: subprocess.CalledProcessError) -> str | None:
    """Extract every concrete output stream from a failed release test."""
    streams = [
        ("stdout", exc.stdout),
        ("stderr", exc.stderr),
    ]
    evidence = "\n\n".join(
        f"[{name}]\n{output.strip()}"
        for name, output in streams
        if output and output.strip()
    )
    return evidence or None


def release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return the commit the tag points to on the remote, or None.

    (Issue #98) The tag is fetched under the base-sync lock — a task
    worktree shares the checkout's common dir, so an unlocked
    concurrent fetch would race on `refs/tags/<tag>` exactly like the
    shared remote-tracking ref of Issue #171. When the remote has no
    such tag the fetch exits 128 (verified against the real CLI) and
    None is returned; after a successful fetch the tag is resolved to
    the commit it points to locally (annotated tags are peeled with
    `^{commit}`). Any other fetch failure fails fast.
    """
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        try:
            run_command(
                ["git", "fetch", "origin",
                 f"refs/tags/{tag}:refs/tags/{tag}"],
                cwd=repo_dir,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 128:
                raise
            return None
        return run_command(
            ["git", "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}^{{commit}}"],
            cwd=repo_dir,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def tag_commit_is_ancestor_of_base(tag_commit: str, base_commit: str,
                                   repo_dir: Path) -> bool:
    """True when `tag_commit` is reachable from `base_commit`.

    (Issue #275) The docs-sync step (release state machine step 8) commits
    and pushes the release notes to the base branch, advancing
    `origin/<base>` past the tag commit. On a resume the frozen base is the
    docs commit; this check distinguishes that expected advance (the tag
    commit is an ancestor of the base — recover the tag commit as the
    canonical release commit) from a genuine tag mismatch (fail fast — an
    existing tag is never moved or overwritten).
    """
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", tag_commit, base_commit],
            cwd=repo_dir,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        return False
    return True


def publish_release(*, repo: str, tag: str, version: str,
                    release_commit: str, changelog: str,
                    scope_evidence: list[str], gate_evidence: list[str], test_evidence: str,
                    run_id: str, issue_number: int) -> str:
    """Create the GitHub Release for the tag — idempotently (Issue #98).

    When a Release for the tag already exists (a restart after a
    successful `gh release create`) its URL is reused, never a second
    Release is created. A legacy existing Release without this changelog
    is upgraded in place; a Release that already contains it is unchanged.
    Otherwise the Release is created from the EXISTING tag (the tag was
    created and pushed by the caller first — `gh release create` never
    creates or moves the tag itself) with notes carrying the full
    verification evidence: version, tag,
    release commit, the per-item scope evidence, the gate evidence, the
    test evidence and the run marker (the same stable machine-readable
    marker every run comment carries).
    """
    notes = "\n".join([
        f"# {version}",
        "",
        changelog,
        "",
        f"- tag: `{tag}`",
        f"- release commit: `{release_commit}`",
        f"- release task: Issue #{issue_number}",
        "",
        "## Scope (verified item by item)",
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        "## Pre-release gates",
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        "## Tests",
        "",
        f"- {test_evidence}",
        "",
        f"<!-- orbi:run={run_id} -->",
        f"run_id={run_id}",
    ])
    try:
        raw = run_command([
            "gh", "release", "view", tag, "--repo", repo,
            "--json", "tagName,url,body",
        ])
        release = json.loads(raw)
        if changelog not in release.get("body", ""):
            run_command([
                "gh", "release", "edit", tag, "--repo", repo,
                "--notes", notes,
            ])
        return release["url"]
    except subprocess.CalledProcessError as exc:
        if "not found" not in (exc.stderr or ""):
            raise
    run_command([
        "gh", "release", "create", tag, "--repo", repo,
        "--verify-tag", "--title", version, "--notes", notes,
    ])
    raw = run_command([
        "gh", "release", "view", tag, "--repo", repo,
        "--json", "tagName,url",
    ])
    return json.loads(raw)["url"]


def close_release_milestone(repo: str, version: str) -> str:
    """Close the Milestone whose title is exactly `version` (Issue #214).

    Runs on the release success path (after the tag is pushed, the
    GitHub Release is published and the release Issue is closed with
    `ai-merged`). The Milestone is matched by EXACT title — never
    guessed, never fuzzy-matched, never a different Milestone:

    - no Milestone with that exact title -> fail fast (the release
      must not be reported as fully successful);
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - already `closed` -> idempotent success (no mutation, no reopen);
    - `open` with open issues -> fail fast with the version, the
      Milestone number/url and the open issue list;
    - `open` with 0 open issues -> closed via the official REST
      contract `PATCH /repos/{owner}/{repo}/milestones/{number}`
      with `state=closed` (OpenAPI `issues/update-milestone`).

    The list query asks for `state=all`: the default `state=open`
    would hide already-closed Milestones and break the idempotent
    case. Returns a short evidence string for the success path. A
    real `gh` failure (auth, rate limit, API error) propagates
    unchanged — like the release gates, a check that cannot be made
    is a failed check.
    """
    raw = run_command([
        "gh", "api", f"repos/{repo}/milestones?state=all", "--paginate",
    ])
    milestones = parse_issue_array(raw)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == version
    ]
    if not matches:
        raise RuntimeError(
            f"release {version}: no Milestone with the exact title "
            f"{version!r} in {repo} — the Milestone is missing, never "
            "guessed or fuzzy-matched"
        )
    if len(matches) > 1:
        numbers = ", ".join(
            f"#{m.get('number')} ({m.get('html_url') or m.get('url')})"
            for m in matches
        )
        raise RuntimeError(
            f"release {version}: {len(matches)} Milestones share the "
            f"exact title {version!r} in {repo} — ambiguous, refusing "
            f"to guess which to close: {numbers}"
        )
    milestone = matches[0]
    number = milestone.get("number")
    html_url = milestone.get("html_url") or milestone.get("url")
    if milestone.get("state") == "closed":
        return (
            f"Milestone #{number} ({html_url}) already closed — "
            "idempotent success, nothing to do"
        )
    open_issues = milestone.get("open_issues")
    if not isinstance(open_issues, int) or open_issues > 0:
        raw = run_command([
            "gh", "issue", "list", "--repo", repo, "--milestone", version,
            "--state", "open", "--json", "number,title", "--limit", "100",
        ])
        issues = parse_issue_array(raw)
        listing = ", ".join(
            f"#{i.get('number')} {i.get('title')}" for i in issues
        ) or "(the API returned no list)"
        raise RuntimeError(
            f"release {version}: Milestone #{number} ({html_url}) still "
            f"has {open_issues} open issue(s) — closing it would hide "
            f"unfinished work; open issues: {listing}"
        )
    run_command([
        "gh", "api", f"repos/{repo}/milestones/{number}",
        "--method", "PATCH", "-f", "state=closed",
    ])
    return (
        f"Milestone #{number} ({html_url}) closed after release "
        f"{version} (0 open issues)"
    )


RELEASE_DOCS_LATEST_MARKER_EN = " (latest)"
RELEASE_DOCS_LATEST_MARKER_ZH = "（最新）"


def release_docs_page(*, version: str, tag_object: str,
                      release_commit: str, published_at: str,
                      release_url: str, issue_number: int,
                      body: str, language: str) -> str:
    """Build one docs-site Release notes page for a published release.

    (Issue #275) The page content is the published GitHub Release body
    (no changelog re-implementation — #204 owns that) plus the meta the
    existing release pages share: the tag/release-commit mapping, the
    publish time and the release task Issue number. Two mechanical
    adaptations only: the body's own leading `# <version>` heading is
    dropped because the page carries its own title with the `(latest)`
    marker, and HTML comment lines (`<!-- ... -->`, the run markers) are
    dropped because the Mintlify MDX parser rejects them — the visible
    `run_id=` line stays, so the correlation is kept.
    """
    notes = body.strip()
    lines = notes.splitlines()
    if lines and lines[0].strip() == f"# {version}":
        lines = lines[1:]
    lines = [
        line for line in lines
        if not line.strip().startswith("<!--")
    ]
    notes = "\n".join(lines).strip()
    if language == "en":
        title = f"# {version} release (latest)"
        intro = (
            f"`{version}` was published {published_at} as the GitHub "
            f"Release [{version}]({release_url}) (release task: "
            f"Issue #{issue_number})."
        )
        heading = "## Tag state (verified against origin)"
        table = (
            "| Ref | Object | Points at |\n"
            "|---|---|---|\n"
            f"| `{version}` | annotated tag `{tag_object}` "
            f"| commit `{release_commit}` |"
        )
    elif language == "zh":
        title = f"# {version} 发布（最新）"
        intro = (
            f"`{version}` 于 {published_at} 发布为 GitHub Release "
            f"[{version}]({release_url})（release task："
            f"Issue #{issue_number}）。"
        )
        heading = "## Tag 状态（对 origin 验证）"
        table = (
            "| Ref | 对象 | 指向 |\n"
            "|---|---|---|\n"
            f"| `{version}` | 注解 tag `{tag_object}` "
            f"| 提交 `{release_commit}` |"
        )
    else:
        raise ValueError(
            f"release docs page language {language!r} is not supported "
            "(use 'en' or 'zh')"
        )
    return "\n".join([
        title, "",
        intro, "",
        heading, "",
        table, "",
        "## Release notes", "",
        notes, "",
    ])


def current_latest_release_slug(config_text: str) -> str:
    """The first page of the English `Releases` group — the current
    latest release (the groups are latest-first, Issue #154)."""
    config = json.loads(config_text)
    for lang in config["navigation"]["languages"]:
        if lang.get("language") != "en":
            continue
        for group in lang["groups"]:
            if group.get("group") == "Releases":
                pages = group["pages"]
                if not pages:
                    raise RuntimeError(
                        "release docs sync: the Releases group has no "
                        "pages — cannot determine the current latest "
                        "release"
                    )
                return str(pages[0])
    raise RuntimeError(
        "release docs sync: docs.json has no English Releases group — "
        "cannot determine the current latest release"
    )


def update_release_navigation(config_text: str, slug: str) -> tuple[str, bool]:
    """Insert `slug` at the head of both release navigation groups.

    (Issue #275) The `Releases` (en) and `发布` (zh) groups list the
    releases latest-first; a new release goes FIRST in both (the zh
    entries carry the `zh/` prefix). A slug already listed in both
    groups leaves the config untouched (idempotent). Exactly one group
    updated means a broken config — fail fast, never guess.
    """
    config = json.loads(config_text)
    updated = 0
    for lang in config["navigation"]["languages"]:
        for group in lang["groups"]:
            if group.get("group") not in ("Releases", "发布"):
                continue
            pages = group["pages"]
            entry = f"zh/{slug}" if group["group"] == "发布" else slug
            if entry in pages:
                continue
            pages.insert(0, entry)
            updated += 1
    if updated == 0:
        return config_text, False
    if updated != 2:
        raise RuntimeError(
            f"release docs sync: expected exactly two release groups "
            f"(Releases + 发布) but updated {updated} — the docs.json "
            "navigation is not the expected Mintlify i18n layout"
        )
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n", True


def move_latest_marker(worktree: Path, old_slug: str,
                       new_slug: str, *, resume: bool) -> list[str]:
    """Move the `(latest)` title marker off the previous latest page.

    (Issue #275) Only the newest release page may carry the marker:
    ` (latest)` (en) / `（最新）` (zh) is stripped from the previous
    latest page's H1. When the old page already lacks the marker the
    move is only accepted on a resume (`resume=True`: the new page
    already carries the marker — a partial step of an earlier attempt
    already moved it); otherwise the invariant is broken and the step
    fails fast — a broken state is never silently repaired. Returns the
    changed relative paths.
    """
    changed: list[str] = []
    for directory, marker in (("docs", RELEASE_DOCS_LATEST_MARKER_EN),
                              ("docs/zh", RELEASE_DOCS_LATEST_MARKER_ZH)):
        path = worktree / directory / f"{old_slug}.mdx"
        if not path.is_file():
            raise RuntimeError(
                f"release docs sync: previous latest page {path} is "
                "missing — cannot move the (latest) marker"
            )
        text = path.read_text(encoding="utf-8")
        first_line, _, rest = text.partition("\n")
        if marker in first_line:
            path.write_text(
                first_line.replace(marker, "", 1) + "\n" + rest,
                encoding="utf-8",
            )
            changed.append(f"{directory}/{old_slug}.mdx")
            continue
        if resume:
            new_path = worktree / directory / f"{new_slug}.mdx"
            new_first = (
                new_path.read_text(encoding="utf-8").splitlines()[0]
                if new_path.is_file() else ""
            )
            if marker in new_first:
                continue
        raise RuntimeError(
            f"release docs sync: {path} does not carry the (latest) "
            "marker in its title and the move did not happen yet — "
            "the latest-marker invariant is broken, refusing to guess"
        )
    return changed


def sync_release_docs(*, source_repo: str, repo_dir: Path,
                      worktree: Path, base_branch: str, tag: str,
                      release_commit: str, issue_number: int) -> str:
    """Sync the docs-site Release notes for one published release.

    (Issue #275) Release state machine step 8 — runs AFTER the GitHub
    Release exists (step 7) and BEFORE the Milestone is closed (step 9):

    - fetches the published Release (`gh release view`, the same call
      `publish_release` uses) — the page content is that body, no
      changelog re-implementation;
    - generates `docs/release-<tag>.mdx` and
      `docs/zh/release-<tag>.mdx` in the release worktree with the meta
      the existing release pages share (tag/release-commit mapping,
      publish time, release task Issue number);
    - moves the `(latest)` title marker from the previous latest page;
    - inserts the new version at the head of the `Releases`/`发布`
      navigation groups in `docs/docs.json` (both languages);
    - commits exactly those docs paths in the release worktree and
      pushes `HEAD:refs/heads/<base_branch>` under the base-sync lock
      (the release path has no PR — a direct commit to the base branch,
      never a force push).

    Idempotent: an existing page with identical content is neither
    regenerated nor overwritten, and a run with nothing to change
    commits nothing. An existing page with DIFFERENT content fails fast
    (never overwritten). Any failure propagates so the release fails
    fast and enters `ai-blocked` like every other step.
    """
    raw = run_command([
        "gh", "release", "view", tag, "--repo", source_repo,
        "--json", "tagName,publishedAt,url,body",
    ])
    release = json.loads(raw)
    body = release.get("body")
    if not isinstance(body, str) or not body.strip():
        raise RuntimeError(
            f"release {tag}: the GitHub Release body is empty — the "
            "docs page would be fabricated, refusing"
        )
    published_at = release["publishedAt"]
    release_url = release["url"]
    tag_object = run_command(
        ["git", "rev-parse", f"refs/tags/{tag}"], cwd=repo_dir,
    )
    new_slug = f"release-{tag}"
    en_path = worktree / "docs" / f"{new_slug}.mdx"
    zh_path = worktree / "docs" / "zh" / f"{new_slug}.mdx"
    en_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=published_at, release_url=release_url,
        issue_number=issue_number, body=body, language="en",
    )
    zh_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=published_at, release_url=release_url,
        issue_number=issue_number, body=body, language="zh",
    )
    # A pre-existing identical page means this is a resume after a
    # partial step — the marker move may then be lenient (it already
    # happened). A page created by THIS run demands the strict move.
    new_page_preexisting = (
        en_path.is_file()
        and en_path.read_text(encoding="utf-8") == en_content
    )
    for path, content in ((en_path, en_content), (zh_path, zh_content)):
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                continue
            raise RuntimeError(
                f"release {tag}: {path} already exists with different "
                "content — an existing release page is never overwritten"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    docs_config = worktree / "docs" / "docs.json"
    if not docs_config.is_file():
        raise RuntimeError(
            f"release {tag}: {docs_config} is missing — the docs "
            "navigation cannot be updated"
        )
    config_text = docs_config.read_text(encoding="utf-8")
    old_slug = current_latest_release_slug(config_text)
    if old_slug != new_slug:
        move_latest_marker(
            worktree, old_slug, new_slug, resume=new_page_preexisting,
        )
    new_config_text, nav_changed = update_release_navigation(
        config_text, new_slug,
    )
    if nav_changed:
        docs_config.write_text(new_config_text, encoding="utf-8")
    expected_paths = [
        f"docs/{new_slug}.mdx",
        f"docs/zh/{new_slug}.mdx",
        "docs/docs.json",
    ]
    if old_slug != new_slug:
        expected_paths += [
            f"docs/{old_slug}.mdx",
            f"docs/zh/{old_slug}.mdx",
        ]
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        run_command(["git", "add", *expected_paths], cwd=worktree)
        try:
            run_command(["git", "diff", "--cached", "--quiet"],
                        cwd=worktree)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
        else:
            return (
                f"docs release notes for {tag} already in sync — "
                "idempotent no-op, nothing committed"
            )
        run_command([
            "git", "commit", "-m",
            f"docs: release notes for {tag} (Issue #{issue_number})",
        ], cwd=worktree)
        run_command([
            "git", "push", "origin", f"HEAD:refs/heads/{base_branch}",
        ], cwd=worktree)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return (
        f"docs release notes for {tag} committed and pushed to "
        f"{base_branch}"
    )


def release_success_comment_body(run_id: str, run_info: str,
                                 release_url: str, tag: str,
                                 release_commit: str,
                                 scope_evidence: list[str],
                                 gate_evidence: list[str],
                                 test_evidence: str,
                                 docs_evidence: str,
                                 milestone_evidence: str) -> str:
    """The terminal success comment: the full verification evidence.

    (Issue #98) The comment is the auditable record of the release:
    run marker, release URL, version/tag/release commit, the
    per-item scope evidence, the gate evidence, the test evidence, the
    docs-site Release notes evidence (Issue #275) and the Milestone
    evidence (Issue #214: the Milestone whose title is the released
    version is closed on the success path).
    """
    return "\n".join([
        run_marker(run_id),
        f"Orbi released: {release_url}",
        run_info,
        f"tag={tag} release_commit={release_commit}",
        "",
        "## Scope (verified item by item)",
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        "## Pre-release gates",
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        "## Tests",
        "",
        f"- {test_evidence}",
        "",
        "## Release notes (docs site)",
        "",
        f"- {docs_evidence}",
        "",
        "## Milestone",
        "",
        f"- {milestone_evidence}",
        "",
        f"run_id={run_id}",
    ])


def release_failure_comment_body(run_id: str, run_info: str,
                                 error: str) -> str:
    """The terminal failure comment: the blocked scene (Issue #98).

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker and the concrete
    reason, so the recoverable scene is on GitHub, not only in the
    journal.
    """
    return "\n".join([
        run_marker(run_id),
        "Orbi release failed (ai-blocked)",
        run_info,
        "",
        f"failure: {error}",
        "",
        f"run_id={run_id}",
    ])


def process_release(issue: dict, config: dict, source_repo: str) -> str:
    """Run the deterministic release state machine for one release Issue.

    (Issue #98) A release task NEVER enters the normal `run_pi`
    development path: the Runner executes the state machine itself,
    step by step, each step idempotent so a restart resumes the same
    run (same run id, same worktree) from the top:

    1. Claim (`ai-in-progress`; a restart reuses the existing run id
       from the worktree — the same resume rule as normal tasks).
    2. Strictly parse the `## Release` declaration from the Issue
       body (version, base_branch, test_command, scope or
       scope_from_milestone — exactly one of the two, Issue #253).
    3. Freeze the base — the release commit is exactly
       `origin/<base_branch>` (fetched under the base-sync lock).
    4. Enforce the pre-release gates (`check_release_gates`).
    5. When `scope_from_milestone` is declared, derive the scope from
       the Milestone (`derive_release_scope_from_milestone`): closed
       Issues + merged PRs; open items are surfaced as evidence, never
       released. Then verify the scope item by item
       (`verify_release_scope`).
    6. Run the declared test command in a clean worktree at the
       release commit (`timeout`-wrapped, Issue #95).
    7. Tag: the remote tag must not exist or must point EXACTLY at
       the release commit (a mismatch fails — an existing tag is
       never moved); otherwise create an annotated tag at the release
       commit and push it with a plain push (never `--force`).
    8. Publish the GitHub Release (idempotent) with the full
       verification evidence.
    9. Sync the docs-site Release notes (Issue #275): generate
       `docs/release-<version>.mdx` + `docs/zh/release-<version>.mdx`
       from the published Release body, insert the new version at the
       head of the `Releases`/`发布` navigation groups in
       `docs/docs.json` (both languages), move the `(latest)` title
       marker from the previous latest page, and commit + push those
       docs changes to the base branch directly (the release path has
       no PR). Idempotent: an identical existing page is neither
       regenerated nor overwritten; anything else fails fast.
    10. Success: `ai-merged` (terminal, the Issue is closed, the
        success comment carries the evidence).
    11. Close the Milestone whose title is EXACTLY the released
        version (Issue #214): closed when it has 0 open Issues
        (idempotent when already closed); fail fast — never a silent
        skip, never a different Milestone — when no Milestone carries
        that exact title or open Issues remain.
    12. Any failure: `ai-blocked` ALONE (no automatic retry — a
         release is a human decision point), the failure comment
         carries the run marker and the concrete reason, and the
         exception propagates so the tick fails fast.
    """
    number = int(issue["number"])
    title = issue["title"]
    run_id = new_run_id()
    set_run_id(run_id)
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config["repo_dir"], source_repo, number,
        )
        if existing_run_id is not None:
            run_id = existing_run_id
            set_run_id(run_id)
            LOGGER.info(
                "issue=%s release_resuming_run run_id=%s", number, run_id,
            )
    priority = issue_priority(issue)
    started = time.monotonic()
    # Bound before the try: the failure comment needs it even when the
    # declaration parse fails on the very first step.
    run_info = f"run_id={run_id} priority={priority}"
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config["repo_dir"], source_repo, number, run_id,
    )

    def progress() -> dict:
        return _progress_state(
            issue=number, title=title, run_id=run_id, role=ROLE_RELEASE,
            branch=branch, worktree=worktree, started=started,
            pr_url=None, review_round=0, priority=priority,
            activity={},
        )

    def on_ci_wait(detail: str) -> None:
        # Issue #268: the CI wait is reflected in the live progress
        # comment (pure bypass, Issue #79); the gate itself emits the
        # `release_waiting_ci` journal line.
        state = progress()
        state["phase"] = f"waiting CI: {detail}"
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.patch(_progress_body(state)),
        )

    release_commit: str | None = None
    release_test_error: subprocess.CalledProcessError | None = None
    declaration: dict | None = None
    open_milestone_evidence: list[str] = []
    try:
        declaration = parse_release_declaration(issue["body"])
        base_branch = declaration["base_branch"]
        run_info = (
            f"base_branch={base_branch} run_id={run_id} "
            f"priority={priority}"
        )
        LOGGER.info(
            "issue=%s release_task %s", number, run_info,
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM, current_labels=(),
        )
        set_active_run(
            number, title, branch, str(worktree),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.ensure(progress_body(progress())),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi release started**: {run_info}",
            ),
        )
        release_commit = freeze_base(config["repo_dir"], base_branch)
        run_info = (
            f"base_branch={base_branch} base_sha={release_commit} "
            f"run_id={run_id} priority={priority}"
        )
        gate_evidence = check_release_gates(
            source_repo, base_branch, release_commit, number,
            # Issue #268: the gate waits out pending CI checks on the
            # release commit; the real load_config always provides the
            # key, the module constant stays the fallback for hand-built
            # configs.
            ci_wait_seconds=config.get(
                "release_ci_wait_seconds", RELEASE_CI_WAIT_SECONDS,
            ),
            on_wait=on_ci_wait,
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi release gates passed**: "
                f"{'; '.join(gate_evidence)}",
            ),
        )
        if declaration.get("scope_from_milestone") is not None:
            # Issue #253: the scope is derived from the Milestone, then
            # verified item by item exactly like a hand-listed scope.
            derived_scope, open_milestone_evidence = (
                derive_release_scope_from_milestone(
                    source_repo, declaration["scope_from_milestone"],
                )
            )
            if not derived_scope:
                raise RuntimeError(
                    f"release {declaration['version']}: Milestone "
                    f"{declaration['scope_from_milestone']!r} has no "
                    "closed Issue or merged PR — the derived scope is "
                    "empty and a release needs at least one delivery"
                )
            declaration["scope"] = derived_scope
            if open_milestone_evidence:
                LOGGER.warning(
                    "issue=%s release_milestone_open_items "
                    "milestone=%s open_items=%s",
                    number, declaration["scope_from_milestone"],
                    "; ".join(open_milestone_evidence),
                )
        scope_evidence = verify_release_scope(
            source_repo, declaration["scope"], config["repo_dir"],
            release_commit,
        )
        if open_milestone_evidence:
            # Open items are NOT released; they are surfaced in the
            # auditable evidence instead of being silently swallowed.
            scope_evidence = scope_evidence + [
                f"NOT released (still open in milestone "
                f"{declaration['scope_from_milestone']}): {item}"
                for item in open_milestone_evidence
            ]
        changelog = build_release_changelog(source_repo, declaration["scope"])
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi release scope verified**: "
                f"{'; '.join(scope_evidence)}",
            ),
        )
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, release_commit,
        )
        try:
            run_release_tests(
                worktree, declaration["test_command"],
                RELEASE_TEST_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            release_test_error = exc
            raise
        test_evidence = (
            f"declared test command and tiered coverage gate "
            f"(line/branch >= 95%, Issue #234) passed in a clean "
            f"worktree at {release_commit} "
            f"(timeout {RELEASE_TEST_TIMEOUT_SECONDS}s)"
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi release tests passed**: {test_evidence}",
            ),
        )
        tag = declaration["version"]
        existing_tag_commit = release_tag_commit(config["repo_dir"], tag)
        if existing_tag_commit is not None:
            if existing_tag_commit == release_commit:
                LOGGER.info(
                    "issue=%s release_tag_exists tag=%s commit=%s",
                    number, tag, existing_tag_commit,
                )
            elif tag_commit_is_ancestor_of_base(
                    existing_tag_commit, release_commit,
                    config["repo_dir"]):
                # Issue #275: the docs-sync step (step 8) pushed the release
                # notes to the base branch, advancing origin/<base> past the
                # tag commit. On a resume the frozen base is the docs commit;
                # the tag commit is the canonical release commit — recover it
                # so the release resumes instead of deadlocking on the tag
                # check.
                LOGGER.info(
                    "issue=%s release_base_advanced_past_tag tag=%s "
                    "tag_commit=%s base_commit=%s",
                    number, tag, existing_tag_commit, release_commit,
                )
                release_commit = existing_tag_commit
            else:
                raise RuntimeError(
                    f"release tag {tag} already exists on the remote "
                    f"and points at {existing_tag_commit}, not the "
                    f"release commit {release_commit} — an existing "
                    "tag is never moved or overwritten"
                )
        else:
            run_command(
                ["git", "tag", "-a", tag, "-m", f"Release {tag}",
                 release_commit],
                cwd=config["repo_dir"],
            )
            run_command(
                ["git", "push", "origin", f"refs/tags/{tag}"],
                cwd=config["repo_dir"],
            )
            LOGGER.info(
                "issue=%s release_tag_pushed tag=%s commit=%s",
                number, tag, release_commit,
            )
        release_url = publish_release(
            repo=source_repo, tag=tag, version=tag,
            release_commit=release_commit, changelog=changelog,
            scope_evidence=scope_evidence, gate_evidence=gate_evidence,
            test_evidence=test_evidence, run_id=run_id, issue_number=number,
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi released**: {release_url}",
            ),
        )
        docs_evidence = sync_release_docs(
            source_repo=source_repo, repo_dir=config["repo_dir"],
            worktree=worktree, base_branch=base_branch, tag=tag,
            release_commit=release_commit, issue_number=number,
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.milestone(
                f"**Orbi release docs synced**: {docs_evidence}",
            ),
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_MERGED,
            current_labels={IN_PROGRESS_LABEL},
        )
        run_command(
            ["gh", "issue", "close", str(number), "--repo", source_repo],
        )
        milestone_evidence = close_release_milestone(
            source_repo, tag,
        )
        comment_issue(
            number, repo=source_repo,
            body=release_success_comment_body(
                run_id, run_info, release_url, tag, release_commit,
                scope_evidence, gate_evidence, test_evidence,
                docs_evidence, milestone_evidence,
            ),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.finish(progress_body(progress())),
        )
        LOGGER.info(
            "issue=%s run_end release_success tag=%s url=%s "
            "elapsed=%.1fs", number, tag, release_url,
            time.monotonic() - started,
        )
        return release_url
    except Exception as exc:
        LOGGER.exception("issue=%s release_failed", number)
        evidence = (
            release_test_evidence(release_test_error)
            if release_test_error is not None else None
        )
        if config.get("auto_repair_issues") and evidence is not None:
            try:
                repair_url = create_repair_issue(
                    repo=source_repo, source_issue=number, run_id=run_id,
                    release_commit=release_commit or "unknown",
                    command=declaration["test_command"] if declaration else "unknown",
                    evidence=evidence,
                )
                LOGGER.info(
                    "repair_issue source_issue=%s run_id=%s url=%s",
                    number, run_id, repair_url,
                )
            except Exception:
                # A repair Issue is an auditable convenience only: failure to
                # create it must be visible but cannot replace the original
                # release-test failure or unblock/publish the release.
                LOGGER.exception(
                    "repair_issue_failed source_issue=%s run_id=%s",
                    number, run_id,
                )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels={IN_PROGRESS_LABEL},
        )
        comment_issue(
            number, repo=source_repo,
            body=release_failure_comment_body(run_id, run_info, str(exc)),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_RELEASE,
            action=lambda: publisher.finish(progress_body(progress())),
        )
        raise


def _pick_from_scan(
    issues: list[dict], repo: str, allow_release: bool = False,
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
    """
    for issue in issues:
        if is_epic(issue):
            LOGGER.info(
                "epic_not_claimed issue=%s repo=%s",
                issue.get("number"), repo,
            )
            continue
        if not allow_release and is_release(issue):
            LOGGER.info(
                "release_not_claimed issue=%s repo=%s",
                issue.get("number"), repo,
            )
            continue
        blockers = open_blocker_numbers(issue)
        if blockers:
            LOGGER.info(
                "blocked_by issue=%s repo=%s blockers=%s",
                issue.get("number"), repo,
                ",".join(str(number) for number in blockers),
            )
            continue
        LOGGER.info(
            "picked issue=%s repo=%s priority=%s",
            issue.get("number"), repo, issue_priority(issue),
        )
        return issue
    return None


def pick_issue(repo: str, active_milestone: str | None = None) -> dict | None:
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
    for search in ready_searches(active_milestone):
        try:
            raw = run_command([
                "gh", "issue", "list", "--repo", repo, "--state", "open",
                "--search", search,
                "--json", "number,title,body,labels,blockedBy",
                "--limit", "200",
            ])
            issues = parse_issue_array(raw)
        except Exception as exc:
            # Fail open (Issue #54): a failed blockedBy query must
            # never deadlock the queue. This tick claims nothing from
            # this repo and the next tick retries the query; the error
            # is logged, never raised, and no label is touched.
            LOGGER.error(
                "blocked_by_check_failed repo=%s error=%s",
                repo, exc,
            )
            return None
        picked = _pick_from_scan(issues, repo)
        if picked is not None:
            return picked
    # Release fallback (Issue #255): only when no ordinary delivery
    # (p0/bug/plain) is claimable. The query keeps `label:ai-ready` +
    # `label:ai-release` and the same delivery-state exclusions; the Epic
    # and blockedBy guards still apply via `_pick_from_scan`. A failed
    # fallback query fails open exactly like any other scan.
    try:
        raw = run_command([
            "gh", "issue", "list", "--repo", repo, "--state", "open",
            "--search", release_fallback_search(active_milestone),
            "--json", "number,title,body,labels,blockedBy",
            "--limit", "200",
        ])
        issues = parse_issue_array(raw)
    except Exception as exc:
        LOGGER.error(
            "blocked_by_check_failed repo=%s error=%s",
            repo, exc,
        )
        return None
    return _pick_from_scan(issues, repo, allow_release=True)


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> dict | None:
    """Return one in-flight Issue a killed runner left behind.

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
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        "label:ai-ready label:ai-in-progress "
        f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
        f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL} "
        f"-label:{EPIC_LABEL}",
        # `labels` (Issue #101): a P0 a killed runner left behind
        # keeps its priority in the progress comment on resume.
        "--json", "number,title,body,labels", "--limit", "1",
    ])
    return parse_issue_list(raw)


def pick_next_issue(
    repos: list[str], active_milestone: str | None = None,
) -> tuple[str, dict] | None:
    """Scan sources in order; return the first ready issue and its source."""
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue
    return None


def edit_issue(number: int, *, repo: str, add: str | None = None,
               remove: str | None = None) -> None:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if add:
        command += ["--add-label", add]
    if remove:
        command += ["--remove-label", remove]
    run_command(command)


def apply_label_patch(number: int, *, repo: str, event: str,
                      current_labels) -> None:
    """Compute the deterministic label patch for `event` and apply it.

    `current_labels` is the Issue's current label names (read once by the
    caller). The patch comes from `delivery_labels.label_patch` — the
    single source of truth for the transition rules (Issue #175) — so the
    same current labels and event always produce the same idempotent
    patch. The patch is applied through `edit_issue`: one call for the
    add plus the first remove, then one call per extra remove (the exact
    same `edit_issue` kwargs the pre-#175 code emitted). A no-op patch
    (nothing to add or remove) applies nothing.
    """
    to_add, to_remove = label_patch(event, current_labels)
    if not to_add and not to_remove:
        return
    first_remove = to_remove[0] if to_remove else None
    kwargs: dict = {"repo": repo}
    if to_add:
        kwargs["add"] = to_add[0]
    if first_remove is not None:
        kwargs["remove"] = first_remove
    edit_issue(number, **kwargs)
    for label in to_remove[1:]:
        edit_issue(number, repo=repo, remove=label)


def comment_issue(number: int, *, repo: str, body: str) -> None:
    run_command(["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", body])


def issue_comments(number: int, *, repo: str) -> list[dict]:
    """Return the Issue's comment history (oldest first) from GitHub.

    ``gh issue view --json comments`` returns a top-level object with a
    ``comments`` array; each comment carries the author and the
    ``authorAssociation`` of the viewer, which is how the runner tells
    its own trusted comments apart from public ones (Issue #45).
    """
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("issue comments must be a JSON array")
    return comments


def new_run_id() -> str:
    """Return a unique short run identifier for one task attempt."""
    return uuid.uuid4().hex[:8]


def issue_context(source_repo: str, number: int) -> str:
    """Issue reference used on every journal line: `owner/repo#number`."""
    return f"{source_repo}#{number}"


def log_format() -> str:
    """Journal log format without a Python timestamp (Issue #40).

    systemd journal already provides time, host and process on every
    line; printing `%(asctime)s` again only duplicates information.
    """
    return "%(levelname)s %(message)s"


def freeze_base(repo_dir: Path, base_branch: str) -> str:
    """Fetch the remote and freeze the exact SHA of origin/<base_branch>.

    The fetch runs under the base-sync lock (Issue #171): it updates
    the shared remote-tracking ref, so it must not race the other
    Runner/Pi fetches on that ref.
    """
    fetch_base_ref(repo_dir, base_branch)
    return run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )


def task_branch(source_repo: str, number: int, run_id: str) -> str:
    return (
        f"orbi/{source_repo.replace('/', '-')}-issue-{number}-{run_id}"
    )


def started_pi_comment_body(run_id: str, run_info: str, branch: str,
                            worktree: Path) -> str:
    """The start comment doubles as the recoverable run scene (Issue #45)."""
    return (
        f"{run_marker(run_id)}\n"
        f"Orbi started Pi: {run_info} branch={branch} "
        f"worktree={worktree}"
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR (Issue #45). The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id — a comment must never be able to name a local path.
    """
    return (
        f"{run_marker(run_id)}\n"
        f"Orbi opened PR: {pr_url} ({run_info})"
    )


OPENED_PR_PREFIX = "Orbi opened PR: "


def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Orbi opened PR:` comment into a resume scene.

    Returns None when the body is not an opened-PR comment. Fails fast
    when the comment is malformed: resuming must recover the exact run
    (run id, base, PR URL), never a guess (Issue #45). Branch and
    worktree are not parsed: the runner derives them from its own
    config, the Issue number and the run id, so a comment can never
    name an arbitrary local path.
    """
    if not isinstance(body, str) or OPENED_PR_PREFIX not in body:
        return None
    head = body.split(OPENED_PR_PREFIX, 1)[1]
    pr_url, _, fields_part = head.partition(" (")
    fields: dict[str, str] = {}
    for part in fields_part.rstrip(")").split():
        key, _, value = part.partition("=")
        if key:
            fields[key] = value
    scene = {
        "pr_url": pr_url.strip(),
        "base_branch": fields.get("base_branch", ""),
        "base_sha": fields.get("base_sha", ""),
        "run_id": fields.get("run_id", ""),
    }
    for key, value in scene.items():
        if not value:
            raise ValueError(f"opened PR comment is missing {key}")
    scene["run_id"] = validate_run_id(scene["run_id"])
    return scene


def _comment_is_trusted(comment: object) -> bool:
    """True only when the comment carries a trusted maintainer association."""
    if not isinstance(comment, dict):
        return False
    return comment.get("authorAssociation") in TRUSTED_COMMENT_ASSOCIATIONS


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only comments posted by a trusted maintainer (OWNER, MAINTAINER,
    MEMBER or COLLABORATOR) are considered: a public comment can never
    become the recovery scene (Issue #45 review, BLOCKER). Fails fast
    when no trusted comment carries the scene: such an Issue cannot be
    resumed and must not be guessed at.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        scene = parse_pr_comment(comment.get("body"))
        if scene is not None:
            return scene
    raise ValueError(
        "no 'Orbi opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )


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
    never matches). A scene that cannot
    be recovered is an unresolvable state: the Issue is marked
    `ai-blocked` with the concrete reason and the error re-raised, so
    the tick stops instead of silently skipping the delivery while a
    fresh task starts ahead of it.

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
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        # `label:a,b` is GitHub's OR within one label qualifier
        # (verified live: repeating the qualifier matches only the
        # first label). `ai-in-progress` is intentionally NOT excluded
        # (Issue #178): a killed review runner leaves the backfilled
        # in-flight label behind, and the positive qualifier above
        # already keeps implement-phase Issues out.
        f"label:{FIX_NEEDED_LABEL},{PR_OPENED_LABEL} "
        f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL}",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        "--json", "number,title,state,url,labels", "--limit", "1",
    ])
    issues = parse_issue_array(raw)
    if not issues:
        return None
    issue = issues[0]
    if issue.get("state") != "OPEN":
        return None
    comments = issue_comments(int(issue["number"]), repo=repo)
    try:
        scene = resume_scene(comments)
    except ValueError as exc:
        block_scene_failure(issue, exc, repo, comments)
    return issue, scene


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an `ai-fix-needed` Issue `ai-blocked` when its scene is
    malformed, then re-raise so the tick stops (Issue #45).

    The failure comment carries the run marker recovered from a trusted
    comment when it is present — the same run id, never a new or
    guessed one. The PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    LOGGER.error(
        "issue=%s resume scene is malformed: %s", number, error,
    )
    marker = ""
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = re.search(r"<!-- orbi:run=([0-9a-f]{8}) -->", body)
        if match:
            marker = run_marker(match.group(1))
            break
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
    raise error


def pick_next_delivery(
    repos: list[str], slot_dir: Path, max_concurrency: int,
    active_milestone: str | None = None,
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
    for repo in repos:
        selected = pick_resumable_delivery(
            repo, slot_dir, max_concurrency,
        )
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        issue = pick_in_progress_issue(
            repo, slot_dir, max_concurrency,
        )
        if issue is not None:
            return repo, issue, None
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue, None
    return None


def worktree_path(repo_dir: Path, source_repo: str, number: int,
                  run_id: str) -> Path:
    """Task worktrees live in the configured repo's .worktrees/ directory."""
    slug = source_repo.replace("/", "-")
    return (
        repo_dir / ".worktrees"
        / f"orbi-{slug}-issue-{number}-{run_id}"
    )


def create_worktree(repo_dir: Path, source_repo: str, number: int,
                    run_id: str, base_sha: str,
                    existing: Path | None = None) -> Path:
    """Create the task worktree from the frozen base SHA, never HEAD.

    An existing path is reused: only a resumed run (same run id after a
    process restart) reaches that state, and its worktree is the scene
    the run continues in (Issue #18). `existing` is the VERIFIED resume
    scene (Issue #219): after a repo rename the scene's path carries
    the OLD slug, so the derived path would miss it and a second
    worktree would be created — the verified scene is returned as-is.
    """
    if existing is not None and existing.is_dir():
        return existing
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        return path
    branch = task_branch(source_repo, number, run_id)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), base_sha,
    ], cwd=repo_dir)
    return path


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


def latest_run_id(repo_dir: Path, source_repo: str, number: int) -> str | None:
    """Return the run id of the newest task worktree for the issue.

    The worktree directory name carries the run id — the state the
    release state machine (Issue #98) reuses to resume the same run
    after a restart. Development runs use the stricter state-file
    discovery (`worktree_resume_scene`, Issue #219) instead.
    """
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/orbi-{slug}-issue-{number}-*"
    candidates = [
        path for path in repo_dir.glob(pattern) if path.is_dir()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return newest.name.rsplit("-", 1)[-1]


def has_in_progress_label(number: int, repo: str) -> bool:
    """True when the Issue still carries `ai-in-progress`.

    The label is added at claim time and removed only by the success or
    failure path, so it is the marker of a run that is (or was, when
    the runner died) in flight — as opposed to the preserved worktrees
    of completed runs (Issue #18).
    """
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "all",
        "--search", f"label:{IN_PROGRESS_LABEL}",
        "--json", "number", "--limit", "50",
    ])
    issues = parse_issue_array(raw)
    return any(int(issue.get("number", -1)) == number for issue in issues)


def _drain_stream(stream, chunks: list[bytes]) -> None:
    """Read a pipe to EOF, appending chunks (process must be finished)."""
    while True:
        data = os.read(stream.fileno(), 65536)
        if not data:
            return
        chunks.append(data)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")


def _log_activity(activity: dict, *, issue_ref: str,
                  role: str, state: str | None = None) -> None:
    """Log one short activity line with the changed fields only.

    No `run=` field (Issue #57): the `[run_id]` prefix added by
    `RunIdFilter` (Issue #41) is the single run-id carrier on the
    high-frequency lines, so the id appears exactly once per line.
    """
    LOGGER.info(
        "activity issue=%s role=%s phase=%s action=%s result=%s "
        "state=%s idle=%s",
        issue_ref, role, activity["phase"],
        quote_value(activity["action"] or "-"),
        activity["result"] or "-",
        state or "-",
        format_duration(activity["stale_seconds"]),
    )


def _log_heartbeat(activity: dict, *, issue_ref: str,
                   role: str, elapsed: float,
                   state: str | None = None) -> None:
    """Log one heartbeat line when nothing changed since the last poll.

    `state` carries the model_wait flag while the model is expected to
    reply next, so a slow active model is not reported as idle (Issue
    #40). No `run=` field (Issue #57): the `[run_id]` prefix is the
    single run-id carrier on the high-frequency lines.
    """
    LOGGER.info(
        "heartbeat issue=%s role=%s phase=%s state=%s elapsed=%s "
        "idle=%s",
        issue_ref, role, activity["phase"], state or "-",
        format_duration(elapsed), format_duration(activity["stale_seconds"]),
    )


def _log_startup(event: str, *, issue_ref: str, role: str, activity: dict,
                 elapsed: float, extra: str = "") -> None:
    """Log one startup phase line (Issue #176).

    Every startup phase (`process_spawned`, `session_created`,
    `first_request_started`, `first_response_received`,
    `startup_failed`) is one stable `key=value` line carrying the issue,
    role, the provider/model Pi selected (`-` until the session's
    `model_change` record says otherwise), the elapsed time since the
    Pi process was spawned, and any extra fields of the phase (`pid=`,
    `reason=`, `session_created=`, `first_request=`). No `run=` field
    (Issue #57): the `[run_id]` prefix is the single run-id carrier.
    Identifiers only — never a key, the prompt or model output.
    """
    LOGGER.info(
        "%s issue=%s role=%s provider=%s model=%s elapsed=%s%s",
        event, issue_ref, role, activity.get("provider") or "-",
        activity.get("model") or "-", format_duration(elapsed),
        f" {extra}" if extra else "",
    )


def _classify_startup_exit(stderr: str, returncode: int) -> str:
    """The distinguishable `startup_failed` reason for an early Pi exit
    (Issue #176).

    The classification is evidence-based on Pi's own stderr (the
    minimal correlation the Issue asks for): a provider authentication
    failure (`401`/`403`, `unauthorized`, `forbidden`, `api key`) is
    `auth_failure`; a network timeout (`timed out`, `timeout`,
    `etimedout`, `econnrefused`, `econnreset`, `enotfound`) is
    `network_timeout`; anything else is the raw exit code
    (`pi_exit_<N>`). The stderr text itself is NOT echoed into the
    reason — only the class — so no sensitive response content can
    leak into the journal.
    """
    lowered = stderr.lower()
    if ("401" in lowered or "403" in lowered or "unauthorized" in lowered
            or "forbidden" in lowered or "api key" in lowered):
        return "auth_failure"
    if ("timed out" in lowered or "timeout" in lowered
            or "etimedout" in lowered or "econnrefused" in lowered
            or "econnreset" in lowered or "enotfound" in lowered):
        return "network_timeout"
    return f"pi_exit_{returncode}"


def _log_provider_config_loaded(*, issue_ref: str, role: str, config: dict,
                                elapsed: float) -> None:
    """Log the `provider_config_loaded` startup line (Issue #176).

    The provider file has been loaded and validated (at config load)
    and materialized for this run — or resolved to Pi's own agent dir
    when unconfigured. The provider/model fields are the configured
    identifiers (the same non-sensitive values already on the redacted
    command line, Issue #119) or `-` when Pi keeps its own defaults.
    """
    _log_startup(
        "provider_config_loaded", issue_ref=issue_ref, role=role,
        activity={"provider": config.get("pi_provider"),
                  "model": config.get("pi_model")},
        elapsed=elapsed,
    )


def _log_startup_failed(*, issue_ref: str, role: str, activity: dict,
                        elapsed: float, returncode: int, stderr: str,
                        timed_out: bool, model_wait_dead: bool,
                        model_wait_swallowed: bool,
                        idle_recovery_failed: bool) -> None:
    """Log one `startup_failed` line (Issue #176): the run failed
    BEFORE the first response, so the line says WHERE the startup was
    stuck (`session_created=`, `first_request=`) plus the
    distinguishable `reason=`. The existing `run_failed` scene line and
    the raised exception are unchanged (fail-fast semantics preserved).
    """
    reason = _startup_failed_reason(
        activity, returncode=returncode, stderr=stderr,
        timed_out=timed_out, model_wait_dead=model_wait_dead,
        model_wait_swallowed=model_wait_swallowed,
        idle_recovery_failed=idle_recovery_failed,
    )
    _log_startup(
        "startup_failed", issue_ref=issue_ref, role=role,
        activity=activity, elapsed=elapsed,
        extra=(
            f"session_created="
            f"{'true' if activity['session_file'] else 'false'} "
            f"first_request="
            f"{'true' if activity['first_request'] else 'false'} "
            f"reason={reason}"
        ),
    )


def _startup_failed_reason(activity: dict, *, returncode: int,
                           stderr: str, timed_out: bool,
                           model_wait_dead: bool,
                           model_wait_swallowed: bool,
                           idle_recovery_failed: bool) -> str:
    """The `startup_failed` reason for a failure before the first
    response (Issue #176): the kill-path class first, then the
    root-cause evidence from Pi's stderr (`auth_failure` /
    `network_timeout` — the missing session file is usually the
    CONSEQUENCE of the auth/network failure, never the cause), then
    WHERE the startup was stuck (`session_not_created` /
    `no_first_request` / the raw early exit). `first_response_timeout`
    is the frozen `model_wait` killed before any response (the hung
    first request)."""
    if idle_recovery_failed:
        return "idle_recovery_stale"
    if model_wait_swallowed:
        return "model_wait_swallowed"
    if model_wait_dead:
        return "first_response_timeout"
    if timed_out:
        return "timeout"
    classified = _classify_startup_exit(stderr, returncode)
    if classified != f"pi_exit_{returncode}":
        return classified
    if not activity["session_file"]:
        return "session_not_created"
    if not activity["first_request"]:
        return "no_first_request"
    return classified


def _pending_timeout_targets(targets: list[dict]) -> list[tuple[dict, float]]:
    """The pre-idle descendants still INSIDE an explicit `timeout`
    deadline (Issue #169): `[(target, deadline_epoch), ...]`.

    A descendant whose command line carries a coreutils
    `timeout <seconds>` wrapper and whose deadline is still in the
    future is a legitimately running tool — the runner waits for the
    deadline instead of signaling it. The age is measured CLOCK
    CONSISTENTLY (Issue #169): the process's monotonic start offset
    (stat field 22) against `time.monotonic()`, never the
    realtime-flavoured `process_start_epoch` — a realtime step after
    boot (NTP) must not make a tool look older than it is. A
    descendant without a clear timeout, whose start time is unreadable,
    or whose deadline already passed is not pending: the existing
    escalation applies to it.
    """
    if not targets:
        return []
    hz = clk_tck()
    now_mono = time.monotonic()
    now = time.time()
    pending: list[tuple[dict, float]] = []
    for target in targets:
        start_mono = process_start_monotonic(target["pid"], hz=hz)
        if start_mono is None:
            continue
        duration = timeout_duration(target["cmdline"] or "")
        if duration is None:
            continue
        remaining = duration - (now_mono - start_mono)
        if remaining > 0:
            pending.append((target, now + remaining))
    return pending


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS,
    model_wait_dead_seconds: float = PI_MODEL_WAIT_DEAD_SECONDS,
    model_wait_probe_url: str | None = None,
    model_wait_probe_seconds: float = PI_MODEL_WAIT_PROBE_SECONDS,
    run_id: str,
    issue: int,
    source_repo: str,
    branch: str,
    role: str = ROLE_IMPLEMENT,
    log_command: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
    pi_env: dict[str, str] | None = None,
) -> str:
    """Run Pi and stream concise live activity into the journal (Issue #40).

    The full invariant scene (branch, worktree, session file) is logged
    once as `run_start`. While Pi runs, only short changed fields are
    logged: `activity` when phase/action/result change, `heartbeat` at
    the poll interval otherwise (the idle time rides on the line). A slow
    active model is not reported as idle: when the newest session event
    is a tool result the state is `model_wait` (one transition line on
    entry, one `resumed` line when the next session event arrives, and
    only configured-interval heartbeats while waiting — no warning
    spam). On a non-zero exit or timeout a `run_failed` line carries the
    full scene again as the debug entry. The caller logs `run_end` once
    the PR and commit are known. The session JSONL stays in the worktree
    as the complete local record; the full prompt and Issue body are
    never logged.

    Startup phases (Issue #176): `process_spawned` is logged right
    after the spawn (with the pid); `session_created`,
    `first_request_started` and `first_response_received` are logged
    once each as the session JSONL crosses the milestones (the session
    record, the first user message, the first assistant message) — each
    line carries the provider/model Pi selected by that point and the
    elapsed time since the spawn. The live lines' `phase` is the
    startup sub-phase while the first response is outstanding
    (`session_pending` / `request_pending`), so a run stuck at
    `starting` is locatable to its startup phase from the journal and
    the progress comment alone. A failure before the first response
    additionally logs `startup_failed` with the distinguishable reason
    (`session_not_created`, `no_first_request`, `auth_failure`,
    `network_timeout`, `pi_exit_<N>`, `timeout`,
    `first_response_timeout`, `model_wait_swallowed`,
    `idle_recovery_stale`); the existing `run_failed` line and the
    raised failure are unchanged (fail-fast semantics preserved).

    `progress` (Issue #18) is invoked on EVERY poll — an activity change
    or a heartbeat — with the current activity state, while the Pi
    process is still running: the caller renders the live GitHub
    progress comment and PATCHes the same run-marker comment in place,
    so mobile users never see a static starting comment for the whole
    run. A callback error is logged and never interrupts the task
    (observability is best-effort, the delivery is not).

    Idle warning (Issue #18): when no model/session event arrives for
    `idle_warn_seconds` (default 5 minutes) and the state is NOT
    model_wait, ONE `pi_idle` WARNING carries `stale_seconds`; the
    first new session event after it logs `pi_resumed`. A slow active
    model (model_wait) is never reported idle (Issue #40).

    Idle-stall recovery (Issue #94): the warning is no longer the end
    of the story. While the session stays stalled (no new activity,
    not model_wait) the runner recovers it, one step per idle window
    of `idle_warn_seconds` since the stall was first seen:
    window 1 SIGTERMs the Pi descendants that already existed before
    the window (the hung tools — found by the ppid chain in
    `/proc/<pid>/stat` plus their start time, never a name guess) so
    the tool gets a non-zero exit and the failure signal reaches the
    model; window 2 SIGKILLs a target that survived; after
    `PI_IDLE_RECOVERY_CYCLES` (default 3) consecutive idle windows the
    Pi session itself is killed and the run fails fast through the
    normal failure path (`ai-blocked`, the slot released) — the slot
    is never held forever. Every step logs a `pi_idle_term` /
    `pi_idle_kill` line (run id, pid, cmdline, result) and the live
    progress comment shows the recovery state via the `recovery`
    activity field. The first new session event resets the whole
    recovery state (`pi_resumed`).
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    issue_ref = issue_context(source_repo, issue)
    session_dir = cwd / ".pi-session"
    # Session files that already exist before this Pi process starts are
    # never followed: a resumed run (same worktree) creates a NEW JSONL,
    # and the journal must report the session of the current invocation,
    # not the previous run's (Issue #45 round-5 review, Major 3).
    known_files = (
        {path for path in session_dir.glob("*.jsonl") if path.is_file()}
        if session_dir.is_dir() else set()
    )
    watcher = SessionWatcher(session_dir, known_files=known_files)
    start = time.monotonic()
    # The initial state is what run_start already reported; activity lines
    # are only emitted when the visible fields actually change.
    initial = watcher.poll()
    last_visible = (initial["phase"], initial["action"], initial["result"])
    # Issue #157: `pi_env` carries the per-run Pi agent dir
    # (`PI_CODING_AGENT_DIR`, verified against real Pi 0.84.3) so the
    # configured provider file is visible to Pi. Absent -> the process
    # inherits the Runner's environment unchanged (pre-#157 shape).
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=None if pi_env is None else {**os.environ, **pi_env},
    )
    # Track the live Pi child for the stop handler (Issue #48): a
    # SIGTERM during this window must shut the child down, never
    # orphan it. Cleared again once the child is reaped (finally).
    set_active_pi(process)
    # Startup phase (Issue #176): the process is spawned — the first
    # sub-phase of `starting` is now observable (pid, elapsed since
    # spawn). The run_start scene line below carries the same
    # pre-session state (phase=session_pending); from here the live
    # lines and the milestone lines carry the startup sub-phases.
    _log_startup(
        "process_spawned", issue_ref=issue_ref, role=role,
        activity=initial, elapsed=0.0, extra=f"pid={process.pid}",
    )
    LOGGER.info(
        "run_start %s",
        format_run_scene(
            initial, run_id=run_id, issue=issue_ref,
            role=role, branch=branch, worktree=str(cwd),
        ),
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    activity = watcher.poll()
    timed_out = False
    model_wait_dead = False
    # Startup milestones already reported (Issue #176): each flips once
    # per session file (a resumed run creates a NEW file, and its
    # first request/response are the new session's — the watcher
    # resets the flags on the switch, so the lines fire again for the
    # new session, exactly once each).
    startup_seen = (False, False, False)
    # Swallowed-model-request probe state (Issue #233): the monotonic
    # moment the /slots probe first reported every slot idle while Pi was
    # in model_wait (None until then). Reset whenever a slot is processing,
    # the probe is inconclusive, or model_wait is left. When the idle
    # state has been sustained for `model_wait_probe_seconds` the request
    # is declared swallowed and Pi is killed fast (well before the
    # model_wait_dead_seconds bound).
    probe_first_idle: float | None = None
    model_wait_swallowed = False
    # model_wait transitions (Issue #40): one line when the state is
    # entered and one when it is left; unchanged polls are heartbeats
    # that carry the state, so a slow model never looks idle and no
    # warning is ever escalated from a slow response.
    last_model_wait = activity["model_wait"]
    # Idle warning state (Issue #18): at most one `pi_idle` warning per
    # stall; the first new session event after it logs `pi_resumed`.
    idle_warned = False
    # Idle-stall recovery state (Issue #94): `idle_start_epoch` marks
    # the start of the current idle window (only descendants that
    # started no later than it are targets — a process spawned after
    # the window began is a new tool call, never a target); `recovery`
    # is the live state shown in the GitHub progress comment (None /
    # `term` / `kill`); `recovery_targets` are the TERMed descendants
    # tracked for the KILL escalation; `recovery_step` is the highest
    # escalation step already executed (0 none, 1 TERM, 2 KILL).
    idle_start_epoch: float | None = None
    # The monotonic moment the idle window opened: escalation is
    # measured in idle windows of NEW silence since the runner first
    # saw the stall (never in absolute stale seconds — a session that
    # is already stale when the window opens, e.g. old record
    # timestamps, starts at step one, not at the session kill).
    idle_start_monotonic: float | None = None
    recovery: str | None = None
    recovery_targets: list[dict] = []
    recovery_step = 0
    # Past-deadline `timeout` targets first observed alive, mapped to
    # the idle cycle they were first observed (Issue #181): the
    # wrapper's own deadline handling (alarm -> signal delivery ->
    # exit) is best-effort and can be delayed by scheduling, so ONE
    # "past deadline and still alive" observation is not evidence the
    # wrapper failed. The target is recorded in the cycle its nominal
    # deadline passes (the grace cycle — nothing is signaled) and
    # signaled only if it is STILL alive one full idle window later
    # (cycle > recorded cycle); a pid that exits in the meantime is
    # dropped (it simply stops being a target).
    deadline_passed: dict[int, int] = {}
    # The `pi_idle_wait` decision is logged once per stall (Issue #169):
    # the escalation re-evaluates every window while the tool is inside
    # its `timeout` deadline, but the journal carries one decision line.
    idle_wait_logged = False
    idle_recovery_failed = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                process.kill()
                timed_out = True
                break
            ready, _, _ = select.select(
                [process.stdout, process.stderr], [], [], poll_interval,
            )
            for stream in ready:
                data = os.read(stream.fileno(), 65536)
                if data:
                    if stream is process.stdout:
                        stdout_chunks.append(data)
                    else:
                        stderr_chunks.append(data)
            activity = watcher.poll()
            # Startup milestones (Issue #176): one line per flip — the
            # session file appeared, the first request went out, the
            # first response arrived. Each carries the provider/model
            # selected by that point and the elapsed time since spawn.
            # A session switch resets the watcher flags, so a resumed
            # invocation reports its NEW session's milestones once each.
            seen = (
                activity["session_file"] is not None,
                activity["first_request"],
                activity["first_response"],
            )
            if seen != startup_seen:
                if seen[0] and not startup_seen[0]:
                    _log_startup(
                        "session_created", issue_ref=issue_ref,
                        role=role, activity=activity,
                        elapsed=time.monotonic() - start,
                    )
                if seen[1] and not startup_seen[1]:
                    _log_startup(
                        "first_request_started", issue_ref=issue_ref,
                        role=role, activity=activity,
                        elapsed=time.monotonic() - start,
                    )
                if seen[2] and not startup_seen[2]:
                    _log_startup(
                        "first_response_received", issue_ref=issue_ref,
                        role=role, activity=activity,
                        elapsed=time.monotonic() - start,
                    )
                startup_seen = seen
            visible = (
                activity["phase"], activity["action"], activity["result"],
            )
            # The wait state rides on the activity/heartbeat lines
            # (Issue #40). Once the model_wait silence crosses the dead
            # threshold the wait is DEAD, not slow (Issue #218): the
            # state is `model_wait_slow` on the last heartbeat before
            # the kill below — visible, and the kill fires on this same
            # poll regardless of the connection state.
            if activity["model_wait"]:
                wait_state = (
                    "model_wait_slow"
                    if activity["stale_seconds"] >= model_wait_dead_seconds
                    else "model_wait"
                )
            else:
                wait_state = None
            if visible != last_visible:
                # Only changed fields are repeated; an unchanged poll is a
                # heartbeat (Issue #40).
                _log_activity(
                    activity, issue_ref=issue_ref,
                    role=role,
                    state=wait_state,
                )
                last_visible = visible
            else:
                _log_heartbeat(
                    activity, issue_ref=issue_ref,
                    role=role, elapsed=time.monotonic() - start,
                    state=wait_state,
                )
            if activity["model_wait"] != last_model_wait:
                # One transition line per state change: entering model_wait
                # (the model is expected to reply next) or leaving it
                # (the next session event arrived: resumed). No `run=`
                # field: the `[run_id]` prefix carries the run id
                # (Issue #57).
                LOGGER.info(
                    "%s issue=%s role=%s phase=%s state=%s",
                    "model_wait" if activity["model_wait"] else "resumed",
                    issue_ref, role,
                    activity["phase"],
                    "model_wait" if activity["model_wait"] else "resumed",
                )
                last_model_wait = activity["model_wait"]
                # Leaving model_wait (the next session event arrived):
                # the swallow-probe window is over — reset it so a later
                # model_wait starts a fresh window (Issue #233).
                if not activity["model_wait"]:
                    probe_first_idle = None
            # Idle warning (Issue #18): a stalled session (no model/
            # session event for `idle_warn_seconds`, and the model is
            # not expected to reply next) logs ONE `pi_idle` warning
            # with the stale time; the first new session event after it
            # logs `pi_resumed`. A slow active model (model_wait) never
            # warns (Issue #40).
            if idle_warned and activity["changed"]:
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.info(
                    "pi_resumed issue=%s role=%s phase=%s",
                    issue_ref, role, activity["phase"],
                )
                idle_warned = False
                # The stall is over: the whole recovery state resets
                # (Issue #94) — a later stall starts a fresh window.
                idle_start_epoch = None
                idle_start_monotonic = None
                recovery = None
                recovery_targets = []
                recovery_step = 0
                idle_wait_logged = False
                deadline_passed.clear()
            elif (
                not activity["model_wait"]
                and not idle_warned
                and activity["stale_seconds"] >= idle_warn_seconds
            ):
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.warning(
                    "pi_idle issue=%s role=%s phase=%s "
                    "stale_seconds=%s",
                    issue_ref, role, activity["phase"],
                    format_duration(activity["stale_seconds"]),
                )
                idle_warned = True
                # The idle window starts now (Issue #94): only
                # descendants that already existed before this moment
                # are recovery targets.
                idle_start_epoch = time.time()
                idle_start_monotonic = time.monotonic()
            # Idle-stall recovery (Issue #94): a stalled session (no
            # model/session activity for idle windows, and the model is
            # NOT expected to reply next) is recovered instead of only
            # warning. Escalation, one step per idle window:
            #   window 1: SIGTERM the pre-idle descendants (the hung
            #             tools) — the tool gets a non-zero exit, the
            #             failure signal reaches the model, the session
            #             continues on its own;
            #   window 2: SIGKILL a TERMed target that is still alive;
            #   window N (PI_IDLE_RECOVERY_CYCLES, default 3): kill the
            #             Pi session itself and fail fast through the
            #             normal `ai-blocked` path (the slot is never
            #             held forever). Only pi descendants (ppid
            #             chain) that started no later than the idle
            #             start are ever signaled — never other system
            #             processes, never a process spawned after the
            #             window began. The progress comment is synced
            #             via the `recovery` activity field.
            if (
                idle_warned
                and not activity["model_wait"]
                and activity["stale_seconds"] >= idle_warn_seconds
                and idle_start_epoch is not None
            ):
                # Escalation is measured in idle windows of NEW silence
                # since the window opened (never in absolute stale
                # seconds): a session that is already stale when the
                # window opens (e.g. old record timestamps) starts at
                # step one, not at the session kill.
                silence = time.monotonic() - idle_start_monotonic
                cycle = int(silence // idle_warn_seconds) + 1
                if cycle >= 1 and recovery_step == 0:
                    targets = find_idle_descendants(
                        process.pid, idle_start_epoch,
                    )
                    # Evidence-based wait (Issue #169, the #105
                    # regression): a pre-idle descendant that runs a
                    # coreutils `timeout <seconds> ...` wrapper INSIDE
                    # its deadline is a legitimately running tool, not a
                    # hung one — the runner waits for the deadline
                    # instead of TERMed it. The wait decision is logged
                    # once and the escalation pauses (recovery_step stays
                    # 0): every later window re-evaluates, and when the
                    # deadline passes with the descendant still alive the
                    # evidence flips and the TERM → KILL → session-kill
                    # escalation runs unchanged (the slot is never held
                    # forever).
                    pending = _pending_timeout_targets(targets)
                    if pending:
                        if not idle_wait_logged:
                            for target, deadline in pending:
                                LOGGER.warning(
                                    "pi_idle_wait run=%s issue=%s role=%s "
                                    "pid=%s cmdline=%s deadline=%s",
                                    run_id, issue_ref, role, target["pid"],
                                    quote_value(target["cmdline"] or "-"),
                                    time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(deadline),
                                    ),
                                )
                            idle_wait_logged = True
                        recovery = "wait"
                    else:
                        # Past-deadline grace (Issue #181): a target
                        # whose nominal `timeout` deadline passed is
                        # NOT escalated in the window it is first
                        # observed alive — the wrapper's own deadline
                        # handling (alarm -> signal delivery -> exit)
                        # is best-effort and can be delayed by
                        # scheduling, so one "past deadline and still
                        # alive" observation is not evidence the
                        # wrapper failed. The pid is recorded (the
                        # grace window, nothing is signaled) and
                        # signaled only if it is STILL alive in a
                        # later escalation window; a pid that exits in
                        # the meantime is dropped. The `recovery=wait`
                        # state stays visible while the grace runs
                        # (the tool is still inside its own deadline
                        # handling, not hung).
                        # Issue #181: the grace is measured in idle
                        # windows, not polls — a target first observed
                        # past its deadline in cycle N is signaled only
                        # if it is still alive in a LATER cycle (one
                        # full idle window of grace for the wrapper's
                        # own deadline handling).
                        flipped = [
                            target for target in targets
                            if target["pid"] in deadline_passed
                            and deadline_passed[target["pid"]] < cycle
                        ]
                        newly_passed = [
                            target for target in targets
                            if target["pid"] not in deadline_passed
                        ]
                        if flipped:
                            # Still alive one full idle window after
                            # the nominal deadline: the wrapper failed
                            # to end the command — the evidence is
                            # confirmed, the TERM -> KILL ->
                            # session-kill escalation runs unchanged
                            # (the slot is never held forever). A
                            # target first observed past its deadline
                            # in this same cycle is NOT recorded here:
                            # it starts its own grace cycle next cycle
                            # (one extra window of grace in this rare
                            # concurrent case is harmless).
                            for target in flipped:
                                deadline_passed.pop(target["pid"], None)
                            recovery_targets = flipped
                            for target in flipped:
                                result = signal_pid(
                                    target["pid"], signal.SIGTERM,
                                )
                                LOGGER.warning(
                                    "pi_idle_term run=%s issue=%s "
                                    "role=%s pid=%s cmdline=%s "
                                    "result=%s",
                                    run_id, issue_ref, role,
                                    target["pid"],
                                    quote_value(target["cmdline"] or "-"),
                                    result,
                                )
                            recovery = "term"
                            # The TERM step ran (the grace window does
                            # NOT advance the step: nothing was
                            # signaled there, so the KILL escalation
                            # still lands one full window after the
                            # TERM, as before).
                            recovery_step = 1
                        elif newly_passed:
                            # First observation past the deadline: the
                            # grace window (no signal, the wait state
                            # stays visible, the step does NOT advance
                            # — nothing was signaled).
                            for target in newly_passed:
                                deadline_passed[target["pid"]] = cycle
                            recovery = "wait"
                        elif [t for t in targets
                              if t["pid"] in deadline_passed]:
                            # The target is inside its grace cycle
                            # (recorded, still alive, one full idle
                            # window not yet up): no signal, the wait
                            # state stays visible, the step does NOT
                            # advance.
                            recovery = "wait"
                        else:
                            # No hung tool found (Pi itself is stuck):
                            # the escalation continues, nothing is
                            # signaled.
                            LOGGER.warning(
                                "pi_idle_term run=%s issue=%s role=%s "
                                "result=no_target",
                                run_id, issue_ref, role,
                            )
                            # The pre-idle descendants are gone (a
                            # waited tool reached its own deadline):
                            # the wait state is stale — clear it so the
                            # progress comment does not keep showing
                            # `recovery: wait` while the escalation
                            # runs (Issue #169).
                            recovery = None
                            deadline_passed.clear()
                            recovery_step = 1
                elif cycle >= 2 and recovery_step == 1:
                    for target in recovery_targets:
                        if not pid_alive(target["pid"]):
                            # The TERM worked between polls: record it,
                            # signal nothing.
                            LOGGER.warning(
                                "pi_idle_kill run=%s issue=%s role=%s "
                                "pid=%s cmdline=%s result=already_dead",
                                run_id, issue_ref, role, target["pid"],
                                quote_value(target["cmdline"] or "-"),
                            )
                            continue
                        result = signal_pid(target["pid"], signal.SIGKILL)
                        LOGGER.warning(
                            "pi_idle_kill run=%s issue=%s role=%s "
                            "pid=%s cmdline=%s result=%s",
                            run_id, issue_ref, role, target["pid"],
                            quote_value(target["cmdline"] or "-"),
                            result,
                        )
                    recovery = "kill"
                    recovery_step = 2
                if cycle >= PI_IDLE_RECOVERY_CYCLES:
                    process.kill()
                    idle_recovery_failed = True
                    break
            # The live progress comment shows the recovery state while
            # it is active (Issue #94); the watcher state is a fresh
            # dict per poll, so the field never leaks into other polls.
            activity["recovery"] = recovery
            if progress is not None:
                try:
                    progress(activity)
                except Exception:
                    LOGGER.exception(
                        "progress_publish_failed run=%s issue=%s role=%s",
                        run_id, issue_ref, role,
                    )
            # Swallowed-model-request detection (Issue #233): the model
            # is expected to reply next (model_wait) and the /slots probe
            # (when configured) reports that EVERY slot is idle for the
            # sustained grace — the request was accepted by the upstream
            # but never scheduled into the slot (the #231 scene: process
            # alive, connection ESTABLISHED, slot idle, nothing
            # generating). This is a real hang that the
            # model_wait_dead_seconds bound would only catch minutes
            # later, so the runner kills Pi FAST and fails fast through
            # the normal failure path. The probe is a pure bypass
            # (Issue #79): an inconclusive probe (None) is simply "no
            # evidence" and the model_wait_dead_seconds bound still
            # applies; a slot that is processing (False) resets the idle
            # window (a slow model is not a swallow). Never fires while
            # events keep arriving (a slow generation is not a swallow).
            if (
                model_wait_probe_url is not None
                and activity["model_wait"]
            ):
                idle = slots_idle(model_wait_probe_url)
                if idle is True:
                    if probe_first_idle is None:
                        probe_first_idle = time.monotonic()
                    elif (
                        time.monotonic() - probe_first_idle
                        >= model_wait_probe_seconds
                    ):
                        alive = upstream_alive(process.pid)
                        LOGGER.warning(
                            "model_wait_swallowed issue=%s role=%s "
                            "idle_seconds=%s probe_seconds=%s "
                            "action=kill_pi session=%s run_id=%s "
                            "upstream_alive=%s reason=swallowed_model_request",
                            issue_ref, role,
                            int(activity["stale_seconds"]),
                            int(model_wait_probe_seconds),
                            activity["session_id"] or "-",
                            run_id,
                            "true" if alive else "false",
                        )
                        process.kill()
                        model_wait_swallowed = True
                        break
                else:
                    # A slot is processing (False) or the probe is
                    # inconclusive (None): no swallow evidence — reset
                    # the idle window so it must be sustained again.
                    probe_first_idle = None
            # Hung-model-request detection (Issue #75, safe recovery
            # since Issue #218): the model is expected to reply next
            # (model_wait) and the session file has been frozen for the
            # dead threshold: the model request is HUNG. A live
            # connection to the upstream (a TCP socket in the live
            # states ESTABLISHED/SYN_SENT/SYN_RECV) is evidence for the
            # journal, never a veto (Issue #218: process alive ≠
            # responding — the #183 scene: llama-server alive, the
            # request hung, the slot held for hours). The runner kills
            # the Pi session and fails fast through the normal failure
            # path (the slot is released by the kernel when the tick
            # exits, the next tick resumes the same run or claims the
            # next Issue). Never fires while events keep arriving (a
            # slow generation is not a hung request).
            if (
                activity["model_wait"]
                and activity["stale_seconds"] >= model_wait_dead_seconds
            ):
                alive = upstream_alive(process.pid)
                LOGGER.warning(
                    "model_wait_dead issue=%s role=%s idle_seconds=%s "
                    "threshold=%s action=kill_pi session=%s run_id=%s "
                    "upstream_alive=%s reason=hung_model_request",
                    issue_ref, role,
                    int(activity["stale_seconds"]),
                    int(model_wait_dead_seconds),
                    activity["session_id"] or "-",
                    run_id,
                    "true" if alive else "false",
                )
                process.kill()
                model_wait_dead = True
                break
            if process.poll() is not None:
                break
    finally:
        # The child is reaped (or dead): the stop handler must never
        # signal an already-exited process (Issue #48).
        set_active_pi(None)
        _drain_stream(process.stdout, stdout_chunks)
        _drain_stream(process.stderr, stderr_chunks)
    stdout = _decode_chunks(stdout_chunks)
    stderr = _decode_chunks(stderr_chunks)
    # Startup failure (Issue #176): a failure before the first response
    # is a STARTUP failure — the line says where the startup was stuck
    # with a distinguishable reason. After the first response the
    # existing `run_failed` scene line alone describes the mid-run
    # failure (no `startup_failed` line).
    if not activity["first_response"]:
        _log_startup_failed(
            issue_ref=issue_ref, role=role, activity=activity,
            elapsed=time.monotonic() - start,
            returncode=process.returncode or 0, stderr=stderr,
            timed_out=timed_out, model_wait_dead=model_wait_dead,
            model_wait_swallowed=model_wait_swallowed,
            idle_recovery_failed=idle_recovery_failed,
        )
    if idle_recovery_failed:
        stale = format_duration(activity["stale_seconds"])
        LOGGER.error(
            "run_failed %s reason=idle_recovery_stale_%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            stale,
        )
        raise RuntimeError(
            f"Pi session stayed idle for {stale} after idle recovery "
            f"(TERM/KILL of pre-idle descendants); Pi was killed "
            "(Issue #94)"
        )
    if model_wait_swallowed:
        idle = format_duration(activity["stale_seconds"])
        LOGGER.error(
            "run_failed %s reason=model_wait_swallowed_idle_%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            idle,
        )
        raise RuntimeError(
            f"Pi is stuck in model_wait and the model /slots probe "
            f"reported every slot idle for the sustained grace "
            f"(session frozen {idle}): the model request was swallowed "
            "(the model service process is alive and the connection is "
            "established, but nothing is generating); Pi was killed "
            "(Issue #233)"
        )
    if model_wait_dead:
        stale = format_duration(activity["stale_seconds"])
        LOGGER.error(
            "run_failed %s reason=model_wait_dead_stale_%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            stale,
        )
        # Issue #227: the classified hung-model-request failure —
        # `process_issue` keeps the Issue `ai-in-progress` (the next tick
        # resumes the same run) instead of the terminal `ai-blocked`.
        raise ModelWaitDeadError(
            f"Pi is stuck in model_wait with a frozen session for {stale}: "
            "the model request is hung (the model service process is "
            "alive but the request never completes); Pi was killed "
            "(Issue #218)"
        )
    if timed_out:
        reason = f"timeout_{format_duration(timeout)}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.TimeoutExpired(
            safe_command, timeout, output=stdout, stderr=stderr,
        )
    if process.returncode != 0:
        reason = f"pi_exit_{process.returncode}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=role, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.CalledProcessError(
            process.returncode, safe_command, output=stdout, stderr=stderr,
        )
    if stderr:
        LOGGER.info("stderr=%s", stderr.rstrip())
    LOGGER.info("stdout=%s", stdout.rstrip())
    return stdout.strip()


def is_ticket_only(issue: dict) -> bool:
    """Return True only for the explicit ticket-only task marker (#209)."""
    labels = issue.get("labels", [])
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("name") == TICKET_ONLY_LABEL
        for label in labels
    )


def run_ticket_agent(issue: dict, config: dict, source_repo: str,
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
            run_id=config["run_id"], issue=int(issue["number"]),
            source_repo=source_repo, branch="-", role=ROLE_TICKET,
            progress=progress,
        )


def process_ticket_only(issue: dict, config: dict, source_repo: str) -> str:
    """Deliver explicit ticket-only Agent output to the source Issue (#209)."""
    number = int(issue["number"])
    title = issue["title"]
    run_id = new_run_id()
    set_run_id(run_id)
    priority = issue_priority(issue)
    run_info = f"run_id={run_id} priority={priority} task_type=ticket-only"
    publisher = ProgressPublisher(number, source_repo, run_id, run_command=run_command)
    started = time.monotonic()
    apply_label_patch(
        number, repo=source_repo, event=EVENT_CLAIM, current_labels=(),
    )
    set_active_run(number, title, "-", "-")
    try:
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_TICKET,
            action=lambda: publisher.ensure(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id, role=ROLE_TICKET,
                branch="-", worktree=Path("-"), started=started, pr_url=None,
                review_round=0, priority=priority,
            ))),
        )
        output = run_ticket_agent(
            issue, {**config, "run_id": run_id}, source_repo,
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
        run_command(["gh", "issue", "close", str(number), "--repo", source_repo])
        # The ticket-only delivery never enters the PR/review states: it
        # clears the claim label directly (no `ai-merged` terminal state —
        # the Issue is closed, not merged).
        edit_issue(number, repo=source_repo, remove=IN_PROGRESS_LABEL)
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_TICKET,
            action=lambda: publisher.milestone(f"ticket-only delivered: {run_info}"),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_TICKET,
            action=lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id, role=ROLE_TICKET,
                branch="-", worktree=Path("-"), started=started, pr_url=None,
                review_round=0, priority=priority,
            ), outcome="**Orbi ticket-only delivered**")),
        )
        LOGGER.info("run_end run=%s issue=%s role=%s result=ticket_only elapsed=%s",
                    run_id, issue_context(source_repo, number), ROLE_TICKET,
                    format_duration(time.monotonic() - started))
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
            _safe_publish(
                run_id=run_id, issue=number, source_repo=source_repo,
                role=ROLE_TICKET,
                action=lambda: publisher.milestone(
                    f"ticket-only blocked: {sanitize(detail)} ({run_info})"
                ),
            )
        except Exception:
            LOGGER.exception("issue=%s ticket-only failure reporting failed", number)
        raise


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           *, timeout: int | None = None, branch: str | None = None,
           progress: Callable[[dict], None] | None = None,
           resume_context: str | None = None) -> str:
    """Run the implementer Pi session for a freshly claimed Issue.

    Issue #82 removed the fixer reuse of this function: findings are
    fixed by the review session in the same session, so the implementer
    is the only user of `prompt.md` now.

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
    system_prompt = render_prompt(
        config["prompt"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "SOURCE_REPOS": ", ".join(config["source_repos"]),
            "ISSUE_NUMBER": str(issue["number"]),
            "ISSUE_TITLE": issue["title"],
            "ISSUE_BODY": issue.get("body", ""),
            "WORKSPACE_ROOT": str(config["workspace_root"]),
            "CONTEXT_FILES": "\n".join(str(path) for path in config["context_files"]),
            "SKILLS": "\n".join(
                str(path)
                for path in _skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)
            ),
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": config["base_sha"],
            "RUN_ID": config["run_id"],
            # Issue #186: the implementer prompt no longer carries the
            # base-sync lock (the base fetch is the Runner's operation);
            # the value stays available for custom prompt templates.
            "BASE_SYNC_LOCK": str(base_sync_lock_path(config["repo_dir"])),
        },
    )
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
    )
    context += "Complete the delivery process in the system prompt."
    if resume_context:
        context += f"\n{resume_context}"
    command = [
        "pi", *_skill_args(_skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)),
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
    if agent_dir is not None:
        extra["pi_env"] = {"PI_CODING_AGENT_DIR": str(agent_dir)}
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_model_args(config), "--print", "--session-dir",
            str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=int(issue["number"]),
        source_repo=source_repo,
        branch=branch,
        progress=progress,
        # Issue #228: the configured model_wait dead threshold (the
        # real load_config always provides the key; the module
        # constant stays the fallback for hand-built configs).
        model_wait_dead_seconds=config.get(
            "model_wait_dead_seconds", PI_MODEL_WAIT_DEAD_SECONDS,
        ),
        # Issue #233: the /slots swallow probe (absent URL -> disabled,
        # the exact pre-#233 behavior).
        model_wait_probe_url=config.get("model_wait_probe_url"),
        model_wait_probe_seconds=config.get(
            "model_wait_probe_seconds", PI_MODEL_WAIT_PROBE_SECONDS,
        ),
        **extra,
    )


def verify_pr(worktree: Path, branch: str, base_branch: str,
              run_id: str, *, issue: int, repo_dir: Path,
              pr_repo: str | None = None,
              expected_url: str | None = None,
              require_latest_base: bool = True) -> str:
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
    would leave the Issue open after the merge). When `pr_repo` is
    given (resume path), the PR's head repo must be that repo; when
    `expected_url` is given, the verified PR URL must exactly equal the
    recovered original PR URL (Issue #45 review: the resume must keep
    the same PR number).
    """
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    if require_latest_base:
        # Re-fetch before judging: the delivery must contain the latest
        # remote base, otherwise it is behind and the PR is rejected
        # (fail fast). The fetch updates the shared remote-tracking
        # ref, so it runs under the base-sync lock (Issue #171) with
        # the deployment checkout as the lock location.
        fetch_base_ref(repo_dir, base_branch, cwd=worktree)
        try:
            run_command(
                ["git", "merge-base", "--is-ancestor",
                 f"origin/{base_branch}", "HEAD"],
                cwd=worktree,
            )
        except subprocess.CalledProcessError:
            LOGGER.error(
                "delivery_behind_base base_branch=%s branch=%s",
                base_branch, branch,
            )
            raise RuntimeError(
                f"delivery HEAD is behind latest remote base "
                f"origin/{base_branch}; merge the latest base, rerun full "
                "tests and review, then retry"
            ) from None
    local_head = run_command(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
    )
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json",
        "url,baseRefName,headRefName,headRefOid,"
        "headRepository,headRepositoryOwner,body",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    url = prs[0].get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    if pr_repo is not None:
        head_repo = _pr_head_repo(prs[0])
        if head_repo != pr_repo:
            LOGGER.error(
                "pr_repo_mismatch expected=%s actual=%s branch=%s",
                pr_repo, head_repo, branch,
            )
            raise RuntimeError(
                f"PR head repo is {head_repo}, expected {pr_repo}; the "
                "resume must keep the PR of the configured source repo"
            )
    base_ref = prs[0].get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; recreate the "
            "PR against the configured base branch"
        )
    head_oid = prs[0].get("headRefOid")
    if head_oid != local_head:
        # Issue #50 (the #158 `d13b0c56` scene): the local HEAD may be
        # AHEAD of the remote PR head — a commit made by a killed
        # session (implementer or reviewer) that was never pushed. The
        # local commit, branch, worktree and PR stay intact and the
        # state is RECOVERABLE: failing here would re-raise on every
        # tick and the review session — which pushes the task branch
        # before its verdict (prompt_review.md) — could never run. Log
        # the exact heads (the commit/push phase the journal must
        # carry) and continue the verification: the next review round
        # pushes the task branch on the same PR and the merge gate
        # re-freezes the advanced head. A remote head that is NOT an
        # ancestor of the local HEAD (diverged) is still a failure: a
        # plain push would be rejected and only a force push or a
        # human decision could continue it.
        try:
            run_command(
                ["git", "merge-base", "--is-ancestor", head_oid,
                 "HEAD"],
                cwd=worktree,
            )
        except subprocess.CalledProcessError:
            LOGGER.error(
                "pr_head_diverged pr_head=%s local_head=%s branch=%s",
                head_oid, local_head, branch,
            )
            raise RuntimeError(
                f"PR head {head_oid} is not local HEAD {local_head} "
                "and is not an ancestor of it (the branch diverged); "
                "a plain push would be rejected and a force push is "
                "forbidden, so the resume must not continue on this "
                "branch"
            ) from None
        LOGGER.info(
            "local_head_ahead_of_pr_head pr_head=%s local_head=%s "
            "branch=%s; the unpushed local commit is preserved and the "
            "next review session pushes the task branch on the same PR "
            "(Issue #50)",
            head_oid, local_head, branch,
        )
    marker = run_marker(run_id)
    body = prs[0].get("body")
    if not isinstance(body, str) or marker not in body:
        LOGGER.error(
            "pr_run_marker_missing expected=%s branch=%s", marker, branch,
        )
        raise RuntimeError(
            f"PR body is missing the stable run marker {marker}; the PR "
            "must carry the machine-readable run id of this attempt"
        )
    fixes = f"Fixes #{issue}"
    # The number must match exactly, not as a digit prefix: `Fixes #41`
    # closes Issue 41, not Issue 4 (review F1, Issue #53).
    if not re.search(rf"Fixes #{issue}(?!\d)", body):
        LOGGER.error(
            "pr_fixes_missing issue=%s branch=%s", issue, branch,
        )
        raise RuntimeError(
            f"PR body is missing `{fixes}`; the keyword must point at the "
            "source Issue so GitHub closes it natively when the PR merges "
            "into the default branch"
        )
    if expected_url is not None and url != expected_url:
        LOGGER.error(
            "pr_url_mismatch expected=%s actual=%s branch=%s",
            expected_url, url, branch,
        )
        raise RuntimeError(
            f"PR URL {url} is not the recovered original PR "
            f"{expected_url}; the resume must keep the same PR number"
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
        LOGGER.debug(
            "runner_runtime_exclude_skipped worktree=%s (no .git entry)",
            worktree,
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
        LOGGER.info(
            "worktree_cleaned issue=%s run_id=%s worktree=%s",
            issue, run_id, worktree,
        )
    except Exception as exc:
        LOGGER.exception(
            "worktree_cleanup_failed issue=%s run_id=%s worktree=%s: %s",
            issue, run_id, worktree, exc,
        )


def deliver_pr(worktree: Path, branch: str, base_branch: str,
               base_sha: str, run_id: str, *, issue: int,
               issue_title: str, repo_dir: Path) -> str:
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
    """
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    # Commit boundary (Issue #186 + #256): the Agent's delivery is the
    # committed worktree state. The Runner-owned runtime paths are
    # pinned in the worktree's LOCAL exclude BEFORE the check, so a task
    # that renamed the tracked .gitignore (the #246 scene) cannot make
    # the Runner's own state look like agent leftovers.
    apply_runner_runtime_excludes(worktree)
    dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    if dirty and _is_runner_runtime_only(dirty):
        # Only Runner-owned runtime paths remain (the exclude write raced
        # or the path appeared after it): re-write the exclude and
        # re-check. The repair is deterministic — no git add, no
        # deletion, no arbitrary whitelisting.
        apply_runner_runtime_excludes(worktree)
        LOGGER.info(
            "runner_runtime_exclude_repaired branch=%s status=%s",
            branch, " ".join(dirty.splitlines()),
        )
        dirty = run_command(["git", "status", "--porcelain"], cwd=worktree)
    if dirty:
        LOGGER.error(
            "delivery_uncommitted_changes branch=%s status=%s",
            branch, " ".join(dirty.splitlines()),
        )
        raise RuntimeError(
            f"the agent left uncommitted changes in the worktree "
            f"({dirty.strip()}); the runner never commits uncommitted "
            "changes or expands the agent's commit boundary"
        )
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    if local_head == base_sha:
        LOGGER.error(
            "delivery_no_commit branch=%s head=%s",
            branch, local_head,
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
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor",
             f"origin/{base_branch}", "HEAD"],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
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
            LOGGER.info(
                "base_absorbed base_branch=%s branch=%s", base_branch,
                branch,
            )
        except subprocess.CalledProcessError as exc:
            run_command(["git", "merge", "--abort"], cwd=worktree)
            LOGGER.error(
                "base_merge_conflict base_branch=%s branch=%s "
                "returncode=%s stderr=%s; the merge was aborted and the "
                "PR opens on the agent's head — the review session "
                "absorbs the base in-session",
                base_branch, branch, exc.returncode,
                (exc.stderr or "").strip(),
            )
    # Plain push of the task branch (never a force push), then verify
    # the remote head: the PR must be created from exactly this head.
    # The head is re-read after the absorb step: a successful base
    # merge advanced it to the merge commit.
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    run_command(["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree)
    remote_head = run_command(
        ["git", "rev-parse", f"origin/{branch}"], cwd=worktree,
    )
    if remote_head != local_head:
        LOGGER.error(
            "remote_head_mismatch expected=%s actual=%s branch=%s",
            local_head, remote_head, branch,
        )
        raise RuntimeError(
            f"remote head {remote_head} does not match the local head "
            f"{local_head} after push origin {branch}"
        )
    # Exactly one open PR of the branch: create it when absent (the PR
    # body contract is the Runner's obligation now, Issue #186) and
    # verify it with the full PR contract (exactly one open PR, base,
    # head, run marker, `Fixes #<issue>`, URL). The verify step skips
    # its own base re-fetch: this function just fetched and merged it.
    raw = run_command([
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
        LOGGER.info(
            "pr_created branch=%s base_branch=%s issue=%s",
            branch, base_branch, issue,
        )
    return verify_pr(
        worktree, branch, base_branch, run_id, issue=issue,
        repo_dir=repo_dir, require_latest_base=False,
    )


def verify_resumed_pr(scene: dict, issue: dict, config: dict,
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
    recovery is impossible. Either way the error is re-raised so the
    tick stops — no review Pi is started, nothing is merged, and the
    PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    run_id = scene["run_id"]
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config["repo_dir"], source_repo, number, run_id,
    )
    try:
        if scene["base_branch"] != config["base_branch"]:
            # Issue #91 + #50: a base-branch change is a human
            # decision: the runner must not auto-retry a PR frozen on
            # another base, so the handler below marks the Issue
            # ai-blocked with the explicit reason and both base values
            # named.
            raise UnrecoverableDeliveryError(
                f"resume scene base_branch={scene['base_branch']} "
                f"differs from configured base_branch="
                f"{config['base_branch']}; the PR is frozen on a "
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
        verified_url = verify_pr(
            worktree, branch, config["base_branch"], run_id,
            issue=number, repo_dir=config["repo_dir"],
            pr_repo=source_repo,
            expected_url=scene["pr_url"], require_latest_base=False,
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
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM, current_labels=(),
        )
        return verified_url
    except Exception as exc:
        LOGGER.exception(
            "issue=%s resume_pr_verification_failed pr=%s branch=%s",
            number, scene["pr_url"], branch,
        )
        detail = _failure_detail(exc)
        try:
            if is_unrecoverable_failure(exc):
                # Issue #50: an external precondition the AI cannot
                # safely judge or fix is terminal: `ai-blocked` ALONE
                # (the opened-PR state label is removed, and a leftover
                # `ai-fix-needed` too) with the explicit reason why
                # automatic recovery is impossible.
                # The current labels are read ONCE before the
                # transition: the blocked patch clears every
                # delivery-state label that is present (`ai-pr-opened`,
                # and a leftover `ai-fix-needed` too), so the terminal
                # state is `ai-blocked` alone.
                labels = issue_labels(number, source_repo)
                apply_label_patch(
                    number, repo=source_repo, event=EVENT_BLOCKED,
                    current_labels=labels,
                )
                body = (
                    f"Orbi failed: the resume verification of "
                    f"PR {scene['pr_url']} failed: {detail}; this is an "
                    "external precondition the AI cannot safely judge "
                    "or fix, so it cannot be recovered automatically "
                    "(the Issue stays ai-blocked until a human "
                    "decides); the PR, branch "
                    f"{branch} and worktree {worktree} are preserved"
                )
            else:
                # Issue #50: a RECOVERABLE failure (the #158 `d13b0c56`
                # scene: the reviewer committed a fix locally but was
                # killed before `git push`, so the remote PR head is
                # still the old one) keeps the Issue in the automatic
                # fix loop: `ai-fix-needed` (the next timer pushes the
                # local commit and continues on the same PR), never
                # `ai-blocked`. The failure comment carries the full
                # scene and is written to the Issue AND the PR.
                apply_label_patch(
                    number, repo=source_repo, event=EVENT_FIX_NEEDED,
                    current_labels=(),
                )
                body = (
                    f"Orbi needs a fix: the resume verification "
                    f"of PR {scene['pr_url']} failed: {detail}; the "
                    "Issue stays ai-fix-needed and the next tick "
                    "resumes the same run, branch, worktree and PR"
                )
                # The session fields are '-' when no session file
                # exists yet (the Pi never started or the dir is
                # gone); a snapshot read failure is logged and
                # reported as "no session yet" (best-effort
                # observability, never a second failure).
                snapshot = None
                try:
                    snapshot = activity_snapshot(worktree / ".pi-session")
                except Exception:
                    LOGGER.exception(
                        "issue=%s activity scene failed", number,
                    )
                if snapshot is None:
                    snapshot = {
                        "session_id": None, "session_file": None,
                        "phase": "starting",
                        "last_activity": None, "action": None,
                        "result": None,
                    }
                body += "\n" + format_run_scene(
                    snapshot,
                    run_id=run_id, issue=issue_context(
                        source_repo, number,
                    ),
                    role=ROLE_REVIEW, branch=branch,
                    worktree=str(worktree),
                )
            if current_run_id():
                body = f"{run_marker(current_run_id())}\n{body}"
            comment_issue(number, repo=source_repo, body=body)
            comment_pr(
                _pr_number(scene["pr_url"]), repo=source_repo, body=body,
            )
            bound_run_id = current_run_id()
            if bound_run_id:
                # Issue #79: the fix-needed/blocked-scene progress
                # publishing is bypass — a 404 here must not abort the
                # failure reporting (the label transition and the
                # failure comment above already completed, and the
                # original error is re-raised below either way).
                if is_unrecoverable_failure(exc):
                    _safe_publish(
                        run_id=bound_run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: ProgressPublisher(
                            number, source_repo, bound_run_id,
                            run_command=run_command,
                        ).milestone(
                            f"blocked: the resume verification of "
                            f"PR {scene['pr_url']} failed: {sanitize(detail)}"
                        ),
                    )
                    _safe_publish(
                        run_id=bound_run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: _finish_blocked_progress(
                            number, bound_run_id, source_repo, worktree,
                            branch, scene["pr_url"],
                            f"the resume verification of PR "
                            f"{scene['pr_url']} failed: {detail}; this "
                            "is an external precondition the AI cannot "
                            "safely judge or fix, so it cannot be "
                            "recovered automatically (the Issue stays "
                            "ai-blocked until a human decides)",
                            "fix the precondition above (see the "
                            "reason) and relabel the Issue "
                            "ai-fix-needed to resume this same PR",
                            title=issue["title"],
                            role=ROLE_REVIEW,
                            review_round=review_rounds_so_far(
                                issue_comments(number, repo=source_repo),
                            ),
                            priority=issue_priority(issue),
                        ),
                    )
                else:
                    _safe_publish(
                        run_id=bound_run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: ProgressPublisher(
                            number, source_repo, bound_run_id,
                            run_command=run_command,
                        ).milestone(
                            f"fix needed: the resume verification of "
                            f"PR {scene['pr_url']} failed: {sanitize(detail)}"
                        ),
                    )
                    _safe_publish(
                        run_id=bound_run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: _finish_fix_needed_progress(
                            number, bound_run_id, source_repo, worktree,
                            branch, scene["pr_url"],
                            f"the resume verification of PR "
                            f"{scene['pr_url']} failed: {detail}",
                            title=issue["title"],
                            review_round=review_rounds_so_far(
                                issue_comments(number, repo=source_repo),
                            ),
                            priority=issue_priority(issue),
                        ),
                    )
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def parse_review_verdict(text: str) -> dict:
    """Extract the last REVIEW_VERDICT JSON line from a review session.

    The reviewer must end with a machine-readable verdict so the Runner can
    decide without parsing prose. Missing or malformed verdicts fail fast; a
    review that cannot be read as a pass is never treated as a pass.
    """
    payload = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(VERDICT_MARKER):
            payload = stripped[len(VERDICT_MARKER):].strip()
    if payload is None:
        raise ValueError("no REVIEW_VERDICT line in review output")
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError:
        raise ValueError("malformed REVIEW_VERDICT JSON") from None
    if not isinstance(verdict, dict):
        raise ValueError("malformed REVIEW_VERDICT JSON")
    if verdict.get("verdict") not in ("pass", "findings"):
        raise ValueError("verdict must be 'pass' or 'findings'")
    for key in ("blockers", "majors", "minors"):
        value = verdict.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not isinstance(verdict.get("findings", []), list):
        raise ValueError("findings must be a list")
    blockers = verdict["blockers"]
    majors = verdict["majors"]
    if verdict["verdict"] == "pass" and (blockers > 0 or majors > 0):
        raise ValueError("pass verdict cannot have blockers or majors")
    if verdict["verdict"] == "findings" and blockers == 0 and majors == 0:
        raise ValueError("findings verdict requires blockers or majors")
    return verdict


def review_has_findings(verdict: dict) -> bool:
    """True when a verdict still blocks the merge gate (Blocker or Major)."""
    return verdict["blockers"] > 0 or verdict["majors"] > 0


def freeze_pr(worktree: Path, branch: str, base_branch: str) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "number,url,baseRefName,baseRefOid,headRefName,headRefOid",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    pr = prs[0]
    base_ref = pr.get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; the merge gate "
            "only accepts the configured protected branch"
        )
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": base_ref,
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


def _skills_for(config: dict, excluded: frozenset[str]) -> list[str | Path]:
    """Return one role's configured skills, dropping excluded names."""
    return [
        skill for skill in config["skills"]
        if _skill_name(skill) not in excluded
    ]


def _skill_args(skills: list[str | Path]) -> list[str]:
    """Return the --skill command args for one role's skill list."""
    return [
        item for skill in skills
        for item in ("--skill", str(skill))
    ]


def run_review(worktree: Path, pr: dict, config: dict, source_repo: str,
               issue: int, branch: str, round: int,
               timeout: int | None = None,
               progress: Callable[[dict], None] | None = None) -> str:
    """Run one independent review session for a frozen PR.

    The session is independent (new process, `prompt_review.md`, a new
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
    system_prompt = render_prompt(
        config["prompt_review"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "PR_NUMBER": str(pr["number"]),
            "PR_URL": pr["url"],
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": pr["base_oid"],
            "HEAD_SHA": pr["head_oid"],
            "HEAD_REF": pr["head_ref"],
            "ROUND": str(round),
            # Issue #171: the SAME shared base-sync lock as the
            # implementer — the review session's base-absorb fetch must
            # run under it (flock <lock> git fetch origin <base>).
            "BASE_SYNC_LOCK": str(base_sync_lock_path(config["repo_dir"])),
        },
    )
    context = (
        f"Independently review PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} against base {config['base_branch']}@{pr['base_oid']} "
        f"and head {pr['head_oid']} (round {round}). Follow code-review R1-R9; "
        "fix Blocker/Major findings in this same session (push only the "
        "task branch) and end with a single REVIEW_VERDICT line."
    )
    command = [
        "pi", *_skill_args(_skills_for(config, REVIEW_EXCLUDED_SKILLS)),
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
    if agent_dir is not None:
        extra["pi_env"] = {"PI_CODING_AGENT_DIR": str(agent_dir)}
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", *_pi_model_args(config), "--print", "--session-dir",
            str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<review-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=issue,
        source_repo=source_repo,
        branch=branch,
        role=ROLE_REVIEW,
        progress=progress,
        # Issue #228: the review session uses the SAME configured
        # model_wait dead threshold as the implementer (the real
        # load_config always provides the key; the module constant
        # stays the fallback for hand-built configs).
        model_wait_dead_seconds=config.get(
            "model_wait_dead_seconds", PI_MODEL_WAIT_DEAD_SECONDS,
        ),
        # Issue #233: the review session uses the SAME /slots swallow
        # probe as the implementer (absent URL -> disabled).
        model_wait_probe_url=config.get("model_wait_probe_url"),
        model_wait_probe_seconds=config.get(
            "model_wait_probe_seconds", PI_MODEL_WAIT_PROBE_SECONDS,
        ),
        **extra,
    )


def merge_gate(worktree: Path, pr: dict, base_branch: str,
               *, repo_dir: Path) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, and the remote head to still be the reviewed head. Then
    merge with `--match-head-commit` so only that exact head can land. No force
    push, no direct push of the protected branch. The base fetch updates the
    shared remote-tracking ref, so it runs under the base-sync lock
    (Issue #171) with the deployment checkout as the lock location.
    """
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor",
             f"origin/{base_branch}", pr["head_oid"]],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "merge_gate_behind_base base_branch=%s pr=%s head=%s",
            base_branch, pr["number"], pr["head_oid"],
        )
        raise RuntimeError(
            f"PR #{pr['number']} head {pr['head_oid']} is behind latest "
            f"remote base origin/{base_branch}; absorb the latest base, rerun "
            "tests and review, then retry"
        ) from None
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergeable,headRefOid",
    ], cwd=worktree)
    state = json.loads(raw)
    mergeable = state.get("mergeable")
    if mergeable != "MERGEABLE":
        LOGGER.error(
            "merge_gate_not_mergeable pr=%s mergeable=%s",
            pr["number"], mergeable,
        )
        raise RuntimeError(
            f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
            "resolve conflicts and retry"
        )
    remote_head = state.get("headRefOid")
    if remote_head != pr["head_oid"]:
        LOGGER.error(
            "merge_gate_head_moved pr=%s reviewed=%s remote=%s",
            pr["number"], pr["head_oid"], remote_head,
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
    LOGGER.info("merged pr=%s head=%s", pr["number"], pr["head_oid"])
    return {**pr, "merged": True}


def confirm_merged(worktree: Path, pr: dict, base_branch: str,
                   *, repo_dir: Path) -> dict:
    """Confirm the PR is MERGED and origin/<base> contains the merge commit.

    The base fetch updates the shared remote-tracking ref, so it runs
    under the base-sync lock (Issue #171) with the deployment checkout
    as the lock location.
    """
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergedAt,mergeCommit",
    ], cwd=worktree)
    state = json.loads(raw)
    if state.get("state") != "MERGED" or not state.get("mergedAt"):
        LOGGER.error("confirm_merged_not_merged pr=%s state=%s",
                     pr["number"], state.get("state"))
        raise RuntimeError(
            f"PR #{pr['number']} is not merged (state={state.get('state')})"
        )
    merge_commit = (state.get("mergeCommit") or {}).get("oid")
    if not merge_commit:
        raise RuntimeError(
            f"PR #{pr['number']} is merged but has no merge commit oid"
        )
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", merge_commit,
             f"origin/{base_branch}"],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "confirm_merged_missing_on_base pr=%s merge_commit=%s",
            pr["number"], merge_commit,
        )
        raise RuntimeError(
            f"merge commit {merge_commit} is not on origin/{base_branch}; "
            "the merge did not land on the protected branch"
        ) from None
    return {"state": "MERGED", "merge_commit": merge_commit}


def review_rounds_so_far(comments: list[dict]) -> int:
    """Count the review rounds already recorded on the Issue.

    Each round with Blocker/Major findings posts one
    `Orbi review round N for PR #...` comment, so the GitHub
    record alone bounds the loop (GitHub Issues are the only state
    store; a runner restart never loses the count). Only trusted
    maintainer comments count: a public comment cannot exhaust the
    round budget or skip review (same filter as resume_scene).
    """
    rounds = 0
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            if line.startswith("Orbi review round "):
                rounds += 1
                break
    return rounds


# ---------------------------------------------------------------------------
# Editable CLI install refresh (Issue #158)
# ---------------------------------------------------------------------------
#
# The official local deployment is the EDITABLE uv tool install (Issue
# #152): the tool env imports the runtime package directly from the
# deployment checkout through a setuptools editable finder. Since the
# src layout (Issue #168) the finder maps the WHOLE package directory
# `src/orbi/` — a newly added package module needs NO reinstall
# (the #158 stale-module-list incident class is gone at the root). The
# remaining packaging inputs come from the checkout's `pyproject.toml`
# (entry points, version, dependencies): when THEY change the installed
# tool env is STALE and the next CLI process can die before the Runner
# starts (the #158 incident shape: `cli_source` merged to main, the
# installed finder still mapped the pre-#152 module set, the systemd
# start failed).
#
# This section refreshes the editable install at the Runner start,
# BEFORE any slot or claim:
#
# - the packaging fingerprint (sha256 of the checkout's
#   `pyproject.toml` — the packaging input that decides the editable
#   metadata) is compared against the fingerprint of the LAST
#   successful install, stored in the shared state dir
#   (`.orbi/cli-install.json` — the same gitignored dir as
#   `base-sync.lock` and the slots, which survives the
#   `git merge --ff-only` checkout sync). It is NOT a second release
#   state: it only records which packaging input the installed tool
#   env was built from;
# - unchanged: NO uv call at all (no per-tick reinstall);
# - changed, or no state yet (first install): ONE lock-protected
#   `uv tool install --force --reinstall --editable --python
#   /usr/bin/python3 <repo_dir>` (the exact verified argv from
#   `cli_source.reinstall_args`);
# - two instances starting in the same tick: the SAME base-sync flock
#   (the lock file the service template's `ExecStartPre` also takes)
#   serializes them; the second instance re-reads the state UNDER the
#   lock and reuses the first's result — no concurrent uv install, no
#   corrupted tool env;
# - a failing install fails fast with the structured
#   `cli_install_failed` line (reason + the exact fix command) and
#   records NO state (the next start retries).
#
# The implementation lives in `runner` itself (see the NOTE at the top
# of this file): the bootstrap chain must stay loadable in a tool env
# whose installed finder predates the packaging change. `cli_source` is
# imported lazily inside `refresh_cli_install`: the reinstall argv is
# the single cross-module dependency (Issue #152's verified command).

CLI_INSTALL_LOGGER = logging.getLogger("orbi.cli_install")

# The uv install timeout (seconds): a local editable build of this
# zero-dependency package takes seconds; a hang (a wedged uv or a
# full disk) must fail the start, never block it forever (Issue #95:
# blocking commands carry a timeout).
UV_INSTALL_TIMEOUT_SECONDS = 300

# The base-sync lock: one home for the concurrency primitive — the
# checkout sync, the ExecStartPre preflight and the CLI install
# refresh all serialize on the SAME lock file.
BASE_SYNC_LOCK_NAME = "base-sync.lock"


class CliInstallError(RuntimeError):
    """The editable CLI install refresh failed (fail fast)."""


def base_sync_lock_path(repo_dir: Path) -> Path:
    """The lock file serializing ALL writers of the deployment base
    checkout and its tool env.

    Two timer instances may start in the same tick, so the service
    template's `ExecStartPre` wraps the fetch + fast-forward in a
    short-lived `flock` on this SAME file, and the Python-side
    checkout sync and the CLI install refresh take the same lock: the
    main worktree and the tool env are never written concurrently.
    The lock lives in the shared state dir (next to the slot files),
    never in a per-process temp dir.

    Issue #171 extended the same lock to EVERY fetch that updates the
    shared remote-tracking ref ``refs/remotes/origin/<base>``: task
    worktrees share the deployment checkout's common dir, so an
    unlocked concurrent fetch (Runner verify/gate/confirm, the Pi
    prompt-side fetch) races on that one ref and fails with
    ``cannot lock ref ... is at <X> but expected <Y>``.
    """
    return Path(repo_dir) / ".orbi" / BASE_SYNC_LOCK_NAME


def acquire_base_sync_lock(
    repo_dir: Path, lock_timeout_seconds: float,
) -> int:
    """Take the base-sync flock; fail fast when it is still held after
    the timeout. The kernel releases an flock when its holder exits,
    so a dead holder can never wedge the lock."""
    lock_path = base_sync_lock_path(repo_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + lock_timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, InterruptedError, PermissionError):
            if time.monotonic() >= deadline:
                os.close(fd)
                CLI_INSTALL_LOGGER.error(
                    "base_sync_lock_timeout repo_dir=%s lock=%s "
                    "timeout_seconds=%s",
                    repo_dir, lock_path, lock_timeout_seconds,
                )
                raise CliInstallError(
                    f"could not take the base-sync lock {lock_path} "
                    f"within {lock_timeout_seconds}s (another Runner "
                    "instance or the ExecStartPre preflight is syncing "
                    "the deployment checkout)"
                ) from None
            time.sleep(0.1)


def fetch_base_ref(repo_dir: Path, base_branch: str,
                   *, cwd: Path | None = None,
                   lock_timeout_seconds: float = 300.0,
                   command_runner: Callable[[list[str]], str] | None = None,
                   ) -> None:
    """Fetch ``origin/<base>`` under the base-sync lock (Issue #171).

    Every command that updates the shared remote-tracking ref
    ``refs/remotes/origin/<base>`` must run under the SAME lock in the
    deployment checkout's shared state dir (the one the ExecStartPre
    flock and ``sync_base_checkout`` use): worktrees share the common
    dir, so an unlocked concurrent fetch races on the ref and fails
    the session. ``repo_dir`` is the deployment checkout (the lock
    location); ``cwd`` is where the fetch runs (the task worktree for
    the Runner verify/gate/confirm paths, the checkout itself by
    default). ``command_runner`` overrides the command executor (the
    setup entry injects its own); by default the module's
    ``run_command`` is used. A lock timeout or a fetch error fails
    fast — no retry, no lock bypass.
    """
    if command_runner is None:
        command_runner = run_command
    fd = acquire_base_sync_lock(repo_dir, lock_timeout_seconds)
    try:
        command_runner(
            ["git", "fetch", "origin", base_branch],
            cwd=cwd if cwd is not None else repo_dir,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def packaging_fingerprint(repo_dir: Path) -> str:
    """The sha256 of the checkout's `pyproject.toml`.

    `pyproject.toml` is the packaging input that decides the editable
    metadata (the entry points, the version, the dependencies) — so
    its content hash is the refresh trigger. Ordinary Python source
    content is NOT part of it: since the src layout (Issue #168) the
    editable finder maps the WHOLE `src/orbi/` package
    directory, so a newly added package module needs no reinstall
    (the whole point of the editable install, Issue #152). A checkout
    without `pyproject.toml` cannot be tool-installed: fail fast,
    never guess a fingerprint.
    """
    pyproject = Path(repo_dir) / "pyproject.toml"
    if not pyproject.is_file():
        raise CliInstallError(
            f"packaging file missing: {pyproject} (the deployment "
            "checkout must carry the packaging input of the editable "
            "install)"
        )
    return hashlib.sha256(pyproject.read_bytes()).hexdigest()


def install_state_path(repo_dir: Path) -> Path:
    """The last-install fingerprint record in the shared state dir.

    `<repo_dir>/.orbi/cli-install.json` — the EXISTING shared
    state dir (gitignored, next to `base-sync.lock` and the slots;
    it survives the `git merge --ff-only` checkout sync). Not a second
    release state and not a per-process temp file.
    """
    return Path(repo_dir) / ".orbi" / "cli-install.json"


def read_install_state(repo_dir: Path) -> str | None:
    """The stored last-install fingerprint, or None.

    Missing file (first install / fresh checkout) -> None. A
    malformed file (a torn write) is treated as "no state" and heals
    in the SAFE direction: one extra idempotent `--force
    --reinstall` runs — never a wedged start.
    """
    path = install_state_path(repo_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fingerprint = data.get("pyproject_sha256") if isinstance(data, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    return fingerprint


def write_install_state(repo_dir: Path, fingerprint: str) -> None:
    """Record the last-install fingerprint (atomic: tmp + replace).

    Only called AFTER a successful install, under the base-sync flock
    (no concurrent writer; the atomic replace guards a torn write on
    a crash mid-install).
    """
    path = install_state_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"pyproject_sha256": fingerprint}), encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def refresh_cli_install(
    repo_dir: Path, *, run_command,
    lock_timeout_seconds: float = 300.0,
) -> str:
    """Refresh the editable CLI install when the packaging inputs
    changed; return `"unchanged"` or `"installed"`.

    The pre-start gate (called by the Runner tick before any slot or
    claim):

    - the current packaging fingerprint equals the stored last-install
      fingerprint -> `"unchanged"` and NO uv call (no per-tick
      reinstall);
    - otherwise (changed, or no state yet — the first install): take
      the base-sync flock (the SAME lock the service template's
      `ExecStartPre` and the checkout sync use), re-check the state
      UNDER the lock (a concurrent instance may have refreshed while
      we waited — reuse its result, never run a second install), run
      the exact verified editable force reinstall from
      `cli_source.reinstall_args`, and record the fingerprint only
      after success.

    A failing install logs the structured `cli_install_failed` line
    (reason + the exact fix command) and raises `CliInstallError`:
    the service does not start (fail fast), no state is recorded (the
    next start retries) and the lock is released (success or
    failure).
    """
    repo_dir = Path(repo_dir)
    fingerprint = packaging_fingerprint(repo_dir)
    if read_install_state(repo_dir) == fingerprint:
        CLI_INSTALL_LOGGER.info(
            "cli_install_unchanged repo_dir=%s pyproject_sha256=%s",
            repo_dir, fingerprint,
        )
        return "unchanged"
    fd = acquire_base_sync_lock(repo_dir, lock_timeout_seconds)
    try:
        # Re-check UNDER the lock: a concurrent instance may have
        # refreshed the tool env while we waited for the flock —
        # reuse its result, never run a second install.
        if read_install_state(repo_dir) == fingerprint:
            CLI_INSTALL_LOGGER.info(
                "cli_install_reused repo_dir=%s pyproject_sha256=%s",
                repo_dir, fingerprint,
            )
            return "unchanged"
        reason = "first_install" if (
            read_install_state(repo_dir) is None
        ) else "packaging_changed"
        from orbi import cli_source  # lazy: the single cross-module dependency
        try:
            run_command(
                cli_source.reinstall_args(repo_dir),
                timeout=UV_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            CLI_INSTALL_LOGGER.error(
                "cli_install_failed repo_dir=%s reason=%s fix=%s",
                repo_dir, quote_value(str(exc)),
                quote_value(cli_source.reinstall_command(repo_dir)),
            )
            raise CliInstallError(
                f"editable CLI install failed for {repo_dir}: {exc} "
                f"(fix: {cli_source.reinstall_command(repo_dir)})"
            ) from exc
        write_install_state(repo_dir, fingerprint)
        CLI_INSTALL_LOGGER.info(
            "cli_install_refreshed repo_dir=%s reason=%s "
            "pyproject_sha256=%s",
            repo_dir, reason, fingerprint,
        )
        return "installed"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
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
        LOGGER.error(
            "base_checkout_not_fast_forwardable repo_dir=%s base=%s "
            "local=%s remote=%s",
            repo_dir, base_branch, local_head, remote_head,
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
    LOGGER.info(
        "base_checkout_synced repo_dir=%s base=%s head=%s",
        repo_dir, base_branch, synced,
    )


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: dict, source_repo: str,
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
    - missing/malformed verdict -> raise; the caller keeps the Issue in
      the automatic fix loop (`ai-fix-needed`, Issue #50: the next review
      session re-runs the same review on the same PR);
    - an exhausted round budget -> raise `UnrecoverableDeliveryError`
      (Issue #50: the bounded loop is a human decision, not a
      recoverable failure); the caller marks the Issue `ai-blocked`
      with the explicit reason.
    """
    marker = run_marker(config["run_id"])
    comments = issue_comments(number, repo=source_repo)
    rounds = review_rounds_so_far(comments)
    if rounds >= MAX_REVIEW_ROUNDS:
        LOGGER.error(
            "review_rounds_exhausted issue=%s rounds=%s",
            number, rounds,
        )
        # Issue #50: the loop is bounded by MAX_REVIEW_ROUNDS on purpose
        # — after 5 rounds without a clean verdict the remaining findings
        # need a human decision, so the AI cannot safely continue this PR
        # (the explicit reason the blocked comment must carry).
        raise UnrecoverableDeliveryError(
            f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
            "without a clean verdict; the bounded loop is a human "
            "decision, so the AI cannot safely continue this PR"
        )
    round = rounds + 1
    pr = freeze_pr(worktree, branch, base_branch)
    publisher = ProgressPublisher(
        number, source_repo, config["run_id"], run_command=run_command,
    )
    started = time.monotonic()
    # Issue #79: ensure is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.ensure(_progress_body(_progress_state(
            issue=number, title=title, run_id=config["run_id"],
            role=ROLE_REVIEW, branch=branch, worktree=worktree,
            started=started, pr_url=pr["url"], review_round=round,
            priority=priority,
        ))),
    )
    output = run_review(
        worktree, pr, config, source_repo, number, branch, round,
        progress=LiveProgressThrottle(
            publisher, issue=number, title=title,
            run_id=config["run_id"], role=ROLE_REVIEW, branch=branch,
            worktree=worktree, started=started, pr_url=pr["url"],
            review_round=round, priority=priority,
        ),
    )
    verdict = parse_review_verdict(output)
    LOGGER.info(
        "review pr=%s round=%s verdict=%s blockers=%s majors=%s",
        pr["number"], round, verdict["verdict"], verdict["blockers"],
        verdict["majors"],
    )
    if review_has_findings(verdict):
        # The reviewer could not make the PR mergeable in this session
        # (Issue #82: findings are fixed in the same session; reaching
        # this branch means the fix was not verifiable or not this
        # session's to decide). The Issue moves to the explicit
        # fix-needed state: the next review session retries the same PR
        # (no cold-start fixer), and the round budget bounds the loop.
        LOGGER.info(
            "review_findings_unfixed pr=%s round=%s; the Issue moves to "
            "ai-fix-needed, the next review session retries the same PR",
            pr["number"], round,
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
        _safe_publish(
            run_id=config["run_id"], issue=number,
            source_repo=source_repo, role=ROLE_REVIEW,
            action=lambda: publisher.milestone(
                f"review findings: round {round}, "
                f"{verdict['blockers']} blocker(s), "
                f"{verdict['majors']} major(s) for PR #{pr['number']}"
            ),
        )
        _safe_publish(
            run_id=config["run_id"], issue=number,
            source_repo=source_repo, role=ROLE_REVIEW,
            action=lambda: publisher.finish(_progress_body(
                _progress_state(
                    issue=number, title=title, run_id=config["run_id"],
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
            current_labels=(),
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
        LOGGER.info(
            "review_head_advanced pr=%s round=%s frozen=%s reviewed=%s",
            pr["number"], round, pr["head_oid"], refrozen["head_oid"],
        )
    try:
        merged = merge_gate(
            worktree, refrozen, base_branch,
            repo_dir=config["repo_dir"],
        )
    except RuntimeError as exc:
        message = str(exc)
        # Behind and merge-conflict are the same next-session job:
        # absorb origin/<base>, resolve, retest. Other gate failures
        # (head moved, etc.) fail fast.
        if (
            "behind latest remote base" not in message
            and "not mergeable" not in message
        ):
            raise
        body = (
            f"{marker}\n"
            f"Orbi review round {round} for PR #{pr['number']}: "
            "the PR is behind the latest base or has a merge conflict; "
            f"the next review session merges the latest "
            f"origin/{base_branch} into the branch in-session, resolves "
            "conflicts, and reruns the full test suite"
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], repo=source_repo, body=body)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=(),
        )
        return False
    confirmed = confirm_merged(
        worktree, merged, base_branch, repo_dir=config["repo_dir"],
    )
    # Issue #79: the merged publishing is bypass — the GitHub merge
    # already landed; a 404 here must not stop the `ai-merged`
    # transition and the merged PR scene comment below.
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round})"
        ),
    )
    _safe_publish(
        run_id=config["run_id"], issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
        action=lambda: publisher.finish(_progress_body(
            _progress_state(
                issue=number, title=title, run_id=config["run_id"],
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
    # The current delivery-state label is `ai-pr-opened` (set by the PR
    # opened transition) — the merged patch clears it.
    apply_label_patch(
        number, repo=source_repo, event=EVENT_MERGED,
        current_labels={PR_OPENED_LABEL},
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{marker}\n"
            f"Orbi merged PR: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"base_branch={base_branch} run_id={config['run_id']})"
        ),
    )
    try:
        sync_base_checkout(config["repo_dir"], base_branch)
    except RuntimeError:
        LOGGER.exception(
            "base_checkout_sync_failed after merge pr=%s repo_dir=%s; "
            "the delivery already landed on origin/%s",
            merged["url"], config["repo_dir"], base_branch,
        )
    return True


def comment_pr(number: int, *, repo: str, body: str) -> None:
    """Comment on a PR in the configured source repository."""
    run_command([
        "gh", "pr", "comment", str(number), "--repo", repo,
        "--body", body,
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




# pytest's final summary line: `1 failed, 155 passed in 4.43s` (the
# counts and the `in <seconds>` part are optional; the line is NOT
# wrapped in `=` section padding).
_Pytest_SUMMARY_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors|skipped|xfailed|xpassed"
    r"|deselected)(?:, \d+ \w+)*(?: in [\d.]+s)?$")


def _is_section_header(line: str) -> bool:
    """True for pytest section headers like `=== FAILURES ===`.

    A header is `=`-delimited at both ends (padding may be absent on
    one side for short titles) and its title carries no digits; the
    real summary line (`1 failed, 155 passed in 4.43s`, bare or
    `=`-padded) and the `FAILED`/`ERROR` evidence lines never match.
    """
    if not line.startswith("="):
        return False
    core = line.strip("=").strip()
    return core == "" or not any(ch.isdigit() for ch in core)


# A pytest summary line reports an outcome only when its FIRST count
# category is failed/passed/error(s): pytest orders the counts
# failed, passed, skipped, errors, xfailed, xpassed, deselected, so a
# run that collected tests always leads with failed or passed (or a
# collection error). `no tests ran in 0.01s` matches no summary regex
# at all; `3 deselected in 0.02s` / `2 skipped in 0.01s` match but
# carry no outcome — reporting them as a pass is a false notification
# (review round 3, PR #42).
_OUTCOME_FIRST_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors)\b",
)
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)")


def _is_no_result(line: str) -> bool:
    """True for pytest lines that verified nothing (no tests ran)."""
    if _NO_TESTS_RE.search(line):
        return True
    stripped = line.strip("=").strip()
    return bool(_Pytest_SUMMARY_RE.match(stripped)) \
        and not _OUTCOME_FIRST_RE.match(stripped)


def read_test_result(worktree: Path) -> str | None:
    """Summarize the worktree's `.orbi/test.log`, or None when it does
    not exist (Issue #302: the contract test command writes the log
    into the excluded run dir, never at the worktree root).

    Prefers the pytest summary line (`1 failed, 155 passed in 4.43s`):
    the LAST one with an outcome when the log holds several runs (TDD
    red, then green), so the progress comment and the `tests
    passed/failed` milestone report the most recent run that actually
    collected tests. Section headers (`=== FAILURES ===`) are never
    reported: the fallback takes the first `FAILED`/`ERROR` evidence
    line or the last non-empty line instead, and a log holding nothing
    but headers yields None (no result info to report). Lines that
    verified nothing (`no tests ran`, `N deselected`, `N skipped`) are
    never reported either: a run that collected no tests is no result,
    and posting `tests passed` for it is a false notification (review
    round 3, PR #42).
    """
    path = worktree / ".orbi" / "test.log"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        # The summary line is bare in pytest 9; some runners wrap it in
        # `=` padding, so the stripped form is matched as well.
        stripped = line.strip("=").strip()
        if _Pytest_SUMMARY_RE.match(line) \
                or _Pytest_SUMMARY_RE.match(stripped):
            if _is_no_result(line):
                # No tests collected in this run: keep looking for an
                # earlier run that did (or report nothing at all).
                continue
            return sanitize(stripped)
    for line in lines:
        if _is_section_header(line) or _is_no_result(line):
            continue
        if line.startswith(("FAILED", "ERROR")) or "passed" in line:
            return sanitize(line)
    last = lines[-1] if lines else None
    if last is not None and (_is_section_header(last)
                             or _is_no_result(last)):
        return None
    return sanitize(last) if last is not None else None


def delivery_head_advanced(worktree: Path, base_sha: str) -> bool:
    """True when the task branch has commits beyond the frozen base."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head != base_sha


def _progress_state(*, issue: int, title: str, run_id: str, role: str,
                    branch: str, worktree: Path, started: float,
                    pr_url: str | None, review_round: int, priority: str,
                    activity: dict | None = None) -> dict:
    """Collect the current run state for the GitHub progress comment.

    `title` is the issue's GitHub title (Issue #100): the progress
    comment's issue line shows `#<number> <title>` in every scene. It
    is required — the GitHub issue data contract guarantees a
    non-empty string title (every runner scan fetches it), and a
    missing title fails fast in `progress.issue_field` instead of
    fabricating one.
    `priority` is the pickup priority of the issue (`p0` or `normal`,
    Issue #101), derived from the issue's labels at claim/resume time.
    `activity` is the live state from the `stream_pi` watcher while a Pi
    session runs (fresh and already read); without it the newest session
    file is full-scanned. Activity snapshotting is best-effort
    observability: a read failure is logged and reported as "no session
    yet", it never blocks the task.
    """
    if activity is None:
        try:
            activity = activity_snapshot(worktree / ".pi-session")
        except Exception:
            LOGGER.exception("issue=%s activity snapshot failed", issue)
            activity = None
    return {
        "run_id": run_id,
        "issue": issue,
        "issue_title": title,
        "role": role,
        "priority": priority,
        "phase": (activity or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (activity or {}).get("last_activity"),
        "last_action": (activity or {}).get("action"),
        "tests": read_test_result(worktree),
        "review_round": review_round,
        "branch": branch,
        "pr": pr_url,
        "session": (activity or {}).get("session_id"),
        # Idle-stall recovery state (Issue #94): `term` / `kill` while
        # the runner recovers a stalled session, absent/None otherwise
        # (the body renders the line only while it is active).
        "recovery": (activity or {}).get("recovery"),
    }


def _progress_body(state: dict, *, outcome: str | None = None) -> str:
    """Render the progress body, optionally with a final outcome header."""
    body = progress_body(state)
    if outcome is None:
        return body
    return f"{outcome}\n\n{body}"


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
    """One-line failure description; keeps subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()}"
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


def _failure_evidence(worktree: Path | None, exc: BaseException) -> str:
    """Render the bounded evidence that survives terminal worktree cleanup.

    Pi subprocess streams come from ``CalledProcessError``. The session and
    test log are read before cleanup and only their tails are copied into the
    failure comment, keeping the GitHub comment useful and bounded.
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
    session = "<unavailable>"
    test_log = "<unavailable>"
    if worktree is not None:
        session_files = sorted(
            (p for p in (worktree / ".pi-session").glob("*.jsonl")
             if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        if session_files:
            session = _tail_text(session_files[-1])
        test_path = worktree / ".orbi" / "test.log"
        if test_path.is_file():
            test_log = _tail_text(test_path)
    return (
        "\n\nFailure evidence (captured before cleanup):\n"
        f"exit_code={return_code if return_code is not None else '<unknown>'}\n"
        f"stderr={stderr[-4000:]}\n"
        f"stdout_tail={stdout[-4000:]}\n"
        f"session_last_events={session[-4000:]}\n"
        f"test_log_tail={test_log[-4000:]}"
    )


def _safe_publish(*, run_id: str, issue: int, source_repo: str,
                  role: str, action: Callable[[], None]) -> None:
    """Run one progress-publishing step as a pure bypass (Issue #79).

    The main delivery path is claim -> worktree -> Pi -> verify PR ->
    review -> fix -> merge; the GitHub progress comment is observability
    on the side. A publishing failure (404, rate limit, API shape
    change) is logged as `progress_publish_failed` and never fails the
    delivery, never marks the Issue `ai-blocked`, and never skips
    `run_pi` / `wait_for_delivery`. This is the same semantics as the
    in-stream live-PATCH callback; Issue #60 already applied it to the
    post-PR record, Issue #79 extends it to the whole
    `ProgressPublisher` path (ensure / milestone / finish).
    """
    try:
        action()
    except Exception:
        LOGGER.exception(
            "progress_publish_failed run=%s issue=%s role=%s",
            run_id, issue_context(source_repo, issue), role,
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


def process_issue(issue: dict, config: dict, source_repo: str) -> str | None:
    number = int(issue["number"])
    # Issue #100: the progress comment's issue line shows the number
    # AND the title in every scene. The scanned issue dict always
    # carries the GitHub title (every scan fetches `title`); a missing
    # or non-string title fails fast here (KeyError / ValueError in
    # `progress.issue_field`) — it is never fabricated.
    title = issue["title"]
    # Release task (Issue #98): a first-class task type that NEVER
    # enters the normal `run_pi` development path. The Runner executes
    # its own deterministic release state machine instead (scope
    # verification, gates, tests, tag, GitHub Release).
    if is_release(issue):
        return process_release(issue, config, source_repo)
    if is_ticket_only(issue):
        return process_ticket_only(issue, config, source_repo)
    base_branch = config["base_branch"]
    # The run id is generated once per attempt and bound BEFORE any
    # other step is logged, so every journal line of the attempt
    # carries it — including the claim-time lines of the restart resume
    # scan below (Issue #41; review round 3, PR #42).
    run_id = new_run_id()
    set_run_id(run_id)
    # Restart resume (Issue #18): a killed runner leaves the task
    # worktree and the `ai-in-progress` label behind. Only in that state
    # the newest worktree's run id is reused, so the same hidden-marker
    # progress comment is found and kept instead of a second one.
    # Completed runs keep their worktrees as evidence but lose the
    # label, so re-claiming an issue always starts a fresh run.
    existing_worktree: Path | None = None
    if has_in_progress_label(number, source_repo):
        try:
            scene = worktree_resume_scene(
                config["repo_dir"], source_repo, number,
            )
        except Exception as exc:
            # Issue #219: the worktree of this issue exists but its run
            # state is missing or corrupt: the same run cannot be
            # verified. Fail fast through the terminal failure path
            # (`ai-blocked` + the reason comment) — never a silent
            # fresh redo on top of unknown work.
            LOGGER.exception(
                "issue=%s resume_continue_failed", number,
            )
            _report_resume_failure(
                number=number, source_repo=source_repo, run_id=run_id,
                error=exc,
            )
            raise
        if scene is not None:
            run_id, existing_worktree = scene
            # The attempt continues the dead run: re-bind the reused
            # id so every later line (including resuming_run) carries
            # it.
            set_run_id(run_id)
            LOGGER.info(
                "issue=%s resuming_run run_id=%s",
                number, run_id,
            )
    base_sha = freeze_base(config["repo_dir"], base_branch)
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
    # Pickup priority (Issue #101): derived from the scanned issue's
    # labels (no extra gh call) and carried on every journal line and
    # scene comment of the attempt via `run_info`.
    priority = issue_priority(issue)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id} "
        f"priority={priority}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    apply_label_patch(
        number, repo=source_repo, event=EVENT_CLAIM, current_labels=(),
    )
    # Issue #266: the successful pickup resets the stale-pickup clock in
    # the health state file (bypass — a state-write failure never fails
    # the claim).
    try:
        runner_health.record_pickup(config["repo_dir"])
    except Exception:
        LOGGER.exception("issue=%s health_pickup_record_failed", number)
    # The Issue is in flight from the claim label on: bind the stop
    # scene (Issue #48) so a SIGTERM during this tick logs the active
    # Issue context, not only systemd's generic "Stopped" line. The
    # branch and worktree path are the same derived values the
    # worktree creation below uses (bound before the worktree exists).
    set_active_run(
        number, title, branch,
        # The verified resume scene keeps its own path (after a repo
        # rename it carries the OLD slug — Issue #219); otherwise the
        # derived path (the same value the worktree creation uses).
        str(existing_worktree or worktree_path(
            config["repo_dir"], source_repo, number, run_id,
        )),
    )
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
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
            config["repo_dir"], source_repo, number, run_id, base_sha,
            existing=existing_worktree,
        )
        # Issue #219: the run state file is the same-run marker —
        # written for EVERY run (a fresh one included, so a later
        # interruption can be verified and resumed), refreshed for a
        # resumed one (same run id, never a second marker).
        write_run_state(
            worktree, run_id=run_id, issue=number,
            source_repo=source_repo, branch=branch,
        )
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
            LOGGER.info(
                "issue=%s resume_continue worktree=%s changed_files=%s "
                "reused_runs=%s previous_session=%s",
                number, worktree,
                len(changed_files(worktree)),
                previous_sessions,
                (snapshot.get("session_id") if snapshot else None) or "-",
            )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        comment_issue(
            number, repo=source_repo,
            body=started_pi_comment_body(run_id, run_info, branch, worktree),
        )
        # Issue #79: the whole ProgressPublisher path is a bypass — a
        # failure here (404, rate limit) is logged and never skips
        # `run_pi` or fails the delivery.
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.ensure(_progress_body(
                _progress_state(
                    issue=number, title=title, run_id=run_id,
                    role=ROLE_IMPLEMENT, branch=branch,
                    worktree=worktree, started=started,
                    pr_url=None, review_round=0, priority=priority,
                ),
            )),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.milestone(
                f"started: {run_info} branch={branch} worktree={worktree}"
            ),
        )
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
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: _publish_plan_milestone(publisher, worktree),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: _publish_test_milestone(publisher, worktree),
        )
        # Issue #186: the deterministic closeout (commit boundary, base
        # freshness + absorb, plain push, PR creation, PR verification)
        # is the Runner's job — the agent stopped at the committed
        # delivery.
        pr_url = deliver_pr(
            worktree, branch, base_branch, base_sha, run_id,
            issue=number, issue_title=title,
            repo_dir=config["repo_dir"],
        )
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        # The PR opened milestone announces the delivery: the
        # implementer always commits the delivery on top of the frozen
        # base, so the head always advanced. (Issue #82 removed the
        # fixer's `fix pushed` milestone: findings are fixed by the
        # review session, which records its own round comments.)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_PR_OPENED,
            current_labels=(),
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
            body=opened_pr_comment_body(run_id, run_info, pr_url),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.milestone(
                f"PR opened: {pr_url} ({run_info})"
            ),
        )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch,
                worktree=worktree, started=started,
                pr_url=pr_url, review_round=0, priority=priority,
            ), outcome="**Orbi delivered**")),
        )
        LOGGER.info(
            "run_end %s",
            format_end_scene(
                run_id=run_id, issue=issue_context(source_repo, number),
                role=ROLE_IMPLEMENT, result="pr_opened",
                elapsed=time.monotonic() - started,
                pr=pr_url, commit=commit,
            ),
        )
        return pr_url
    except ModelWaitDeadError as exc:
        # Issue #227: the hung-model-request recovery is a CLASSIFIED,
        # AI-recoverable failure — NOT the terminal `ai-blocked`. The
        # worktree keeps the interrupted work and the run state file is
        # intact, so the Issue keeps `ai-in-progress`: the next tick's
        # in-flight restart scan (`pick_in_progress_issue`) resumes the
        # SAME run (same run id, branch, worktree, progress comment).
        # The recovery stays fail-fast (Pi was killed, the tick ends
        # cleanly below, the slot is released by `main`'s `finally`);
        # only the label outcome changes — never `ai-blocked`.
        LOGGER.exception("issue=%s model_wait_dead_recovered", number)
        detail = _failure_detail(exc)
        body = (
            f"{run_marker(run_id)}\n"
            f"Orbi model_wait recovered: {detail}; the run is "
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
        try:
            comment_issue(number, repo=source_repo, body=body)
        except Exception:
            LOGGER.exception(
                "issue=%s model_wait_recovered_comment_failed", number,
            )
        _safe_publish(
            run_id=run_id, issue=number, source_repo=source_repo,
            role=ROLE_IMPLEMENT,
            action=lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, title=title, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch,
                worktree=worktree, started=started,
                pr_url=None, review_round=0, priority=priority,
            ), outcome=(
                "**Orbi model_wait recovered**\n\n"
                f"failure: {detail}\n"
                "next step: nothing — the Issue stays ai-in-progress "
                "and the next tick resumes the same run (same run id, "
                "branch, worktree)"
            ))),
        )
        return None
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        # Issue #266: record the failed run attempt (conservative failure
        # fingerprint) so the next tick's self-health check can detect a
        # repeating dead end (the #246 scene). Pure bypass: a state-write
        # failure never changes the delivery outcome.
        try:
            runner_health.record_run_attempt(
                runner_health.health_state_path(config["repo_dir"]),
                repo=source_repo, issue=number, run_id=run_id,
                outcome="failed",
                fingerprint=runner_health.failure_fingerprint(exc),
            )
        except Exception:
            LOGGER.exception("issue=%s health_failure_record_failed", number)
        scene = ""
        if worktree is not None:
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
                if snapshot is None:
                    # No session file yet: the scene still carries the full
                    # debug entry (worktree, branch) with '-' session fields.
                    snapshot = {
                        "session_id": None, "session_file": None,
                        "phase": "starting", "last_activity": None,
                        "action": None, "result": None,
                    }
                scene = format_run_scene(
                    snapshot,
                    run_id=run_id, issue=issue_context(source_repo, number),
                    role=ROLE_IMPLEMENT, branch=branch, worktree=str(worktree),
                )
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
        # Issue #256: the terminal worktree cleanup is gated on the
        # `ai-blocked` transition ACTUALLY reaching GitHub — a simulated
        # kill (the failure path's label edit never lands) leaves the
        # Issue recoverable (ai-in-progress), so the scene must be kept
        # for the same-run resume.
        blocked_transition_done = False
        try:
            # The claim label is removed on every failure; when the
            # delivery already made the opened-PR transition (the
            # scene-comment failure of Issue #79), the opened-PR label
            # is removed too, so the terminal state is `ai-blocked`
            # ALONE — never `ai-pr-opened` + `ai-blocked` (docs/workflow.mdx
            # label lifecycle: `ai-pr-opened` is removed on terminal failure).
            # The current delivery-state label is derived from the
            # `pr_opened` flag (the only label present at this point):
            # `ai-pr-opened` when the PR transition landed, otherwise
            # `ai-in-progress` (the claim label).
            apply_label_patch(
                number, repo=source_repo, event=EVENT_BLOCKED,
                current_labels=(
                    {PR_OPENED_LABEL} if pr_opened else {IN_PROGRESS_LABEL}
                ),
            )
            blocked_transition_done = True
            detail = _failure_detail(exc)
            evidence = _failure_evidence(worktree, exc)
            body = (
                f"{run_marker(run_id)}\n"
                f"Orbi failed: {detail} ({run_info})"
            )
            if scene:
                body += f" {scene}"
            body += evidence
            comment_issue(number, repo=source_repo, body=body)
            # The blocked milestone is posted even when the worktree was
            # never created or the progress comment was never ensured:
            # the mobile notification of the terminal failure must not
            # depend on local state. Both steps are bypass (Issue #79):
            # a progress 404 here must not abort the `ai-blocked`
            # transition above or the re-raise below.
            _safe_publish(
                run_id=run_id, issue=number, source_repo=source_repo,
                role=ROLE_IMPLEMENT,
                action=lambda: publisher.milestone(
                    f"blocked: {sanitize(detail)} ({run_info})"
                ),
            )
            if worktree is not None and publisher.comment_id is not None:
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_IMPLEMENT,
                    action=lambda: publisher.finish(_progress_body(
                        _progress_state(
                            issue=number, title=title, run_id=run_id,
                            role=ROLE_IMPLEMENT, branch=branch,
                            worktree=worktree, started=started,
                            pr_url=None, review_round=0,
                            priority=priority,
                        ), outcome=(
                            "**Orbi blocked**\n\n"
                            f"failure: {detail}\n"
                            "next step: fix the failure above and "
                            "re-run this Issue (a new run id is "
                            "created automatically)"
                        ),
                    )),
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
            if worktree is not None and blocked_transition_done:
                cleanup_task_worktree(
                    worktree, config["repo_dir"], run_id=run_id,
                    issue=number,
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
        return None


def _pr_number(pr_url: str) -> int:
    """Extract the PR number from its URL (the last path segment)."""
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])


def pr_state(pr_url: str, source_repo: str) -> str:
    """Return a PR's state from the configured source repository.

    The delivery-wait loop (Issue #39) uses it to tell a delivery that is
    still awaiting review from one that is done: only `MERGED` or
    `CLOSED` ends the slot hold. Anything else is a corrupted state and
    fails fast.
    """
    number = _pr_number(pr_url)
    raw = run_command([
        "gh", "pr", "view", str(number), "--repo", source_repo,
        "--json", "state",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    return state


def issue_labels(number: int, repo: str) -> list[str]:
    """Return the current label names of one Issue."""
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "labels",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError("issue labels must be a JSON array")
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _finish_blocked_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, next_step: str, title: str,
    role: str = ROLE_REVIEW, review_round: int = 0,
    priority: str = "normal",
) -> None:
    """Finish the tracked progress comment with the blocked scene.

    `title` is the issue's GitHub title (Issue #100): the blocked scene
    shows `#<number> <title>` like every other progress scene; it is
    required, never fabricated.

    The contract (Issue #18): on failure the progress comment becomes
    the blocked scene with the next-step reason — the same terminal
    body the `process_issue` failure path writes. `ensure` finds the
    run's existing progress comment by its hidden marker (PATCHing it
    in place) or creates it when the run never reached one; either way
    the blocked scene is the final state. `role` and `review_round`
    are the actual role and completed review rounds of the blocked run
    (review round 2, PR #42): the caller derives them from the
    Issue's trusted review-round comments, so the terminal comment
    never shows a stale hardcoded role/round. Issue #82: the only
    post-PR role is `review` (the review session fixes findings in the
    same session), so the default is `ROLE_REVIEW`.
    """
    if run_id is None:
        return
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publisher.ensure(_progress_body(_progress_state(
        issue=number, title=title, run_id=run_id, role=role,
        branch=branch or "-", worktree=worktree or Path("-"),
        started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=(
        "**Orbi blocked**\n\n"
        f"failure: {detail}\n"
        f"next step: {next_step}"
    )))


def _finish_fix_needed_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, title: str,
    review_round: int = 0, priority: str = "normal",
) -> None:
    """Finish the tracked progress comment with the fix-needed scene
    (Issue #50).

    The contract (Issue #18): on a RECOVERABLE failure the progress
    comment becomes the fix-needed scene with the next-step reason —
    the Issue stays in the automatic fix loop (`ai-fix-needed`) and the
    next timer resumes the same run, branch, worktree and PR. `ensure`
    finds the run's existing progress comment by its hidden marker
    (PATCHing it in place) or creates it when the run never reached
    one; either way the fix-needed scene is the final state of this
    tick. `title` is required — the GitHub issue data contract
    guarantees a non-empty string title (every runner scan fetches it),
    never fabricated. `review_round` and `priority` are the actual
    completed review rounds and pickup priority of the run (the caller
    derives them from the Issue's trusted review-round comments and
    labels), so the scene never shows a stale hardcoded value.
    """
    if run_id is None:
        return
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publisher.ensure(_progress_body(_progress_state(
        issue=number, title=title, run_id=run_id, role=ROLE_REVIEW,
        branch=branch or "-", worktree=worktree or Path("-"),
        started=time.monotonic(), pr_url=pr_url,
        review_round=review_round, priority=priority,
    ), outcome=(
        "**Orbi fix needed**\n\n"
        f"failure: {detail}\n"
        "next step: the next tick resumes the same run, branch, "
        "worktree and PR automatically (the Issue stays ai-fix-needed)"
    )))


def wait_for_delivery(pr_url: str, issue: dict, config: dict,
                      source_repo: str, poll_interval: float = PI_POLL_INTERVAL) -> None:
    """Own the delivery lifecycle: hold the slot until merge or failure.

    The slot is acquired by `main` before the claim and must stay
    occupied through implement -> review -> merge (Issue #39): a
    delivery whose PR is open still needs the machine, and no other
    Runner may start a second Pi while it is held. The Runner is the
    owner of that lifecycle, so it re-checks the delivery every
    `poll_interval` seconds (the same cadence as the Pi activity poll):

    - PR `MERGED` -> terminal: the delivery is done, the slot is
      released by the caller and the next tick may claim new work;
    - PR `CLOSED` without merge -> terminal failure: the Issue is
      marked `ai-blocked` (removing `ai-pr-opened`/`ai-fix-needed`) with
      a failure comment carrying the run marker, then the slot is
      released by the caller;
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
    - otherwise -> keep holding the slot and re-check.

    There is no timeout: systemd owns the run lifecycle and kills the
    service on stop, which releases the slot via the kernel (flock on
    the open descriptor). No polling shim, no daemon loop, no queue.
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
    LOGGER.info(
        "issue=%s delivery_awaiting pr=%s priority=%s; holding the "
        "slot until the PR is merged or terminally failed",
        number, pr_url, priority,
    )
    while True:
        state = pr_state(pr_url, source_repo)
        if state == "MERGED":
            LOGGER.info(
                "issue=%s delivery_merged pr=%s; releasing the slot",
                number, pr_url,
            )
            return
        if state == "CLOSED":
            LOGGER.info(
                "issue=%s delivery_closed_unmerged pr=%s; marking the "
                "Issue ai-blocked and releasing the slot",
                number, pr_url,
            )
            # The current labels are read ONCE before the transition:
            # the blocked patch clears every delivery-state label that
            # is present (`ai-pr-opened`, and `ai-fix-needed` when the PR
            # was closed while awaiting the next review session), so the
            # terminal state is `ai-blocked` alone.
            labels = issue_labels(number, source_repo)
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
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
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
                _safe_publish(
                    run_id=run_id, issue=number,
                    source_repo=source_repo, role=ROLE_REVIEW,
                    action=lambda: _finish_blocked_progress(
                        number, run_id, source_repo, None, None,
                        pr_url,
                        f"PR {pr_url} was closed without a merge; the "
                        "delivery is terminally failed",
                        "investigate why the PR was closed and re-open "
                        "the delivery or start a fresh run on the "
                        "Issue",
                        title=title,
                        role=ROLE_REVIEW, review_round=blocked_round,
                        priority=priority,
                    ),
                )
            return
        labels = issue_labels(number, source_repo)
        if (is_resumable(labels)
                and not needs_human_intervention(labels)
                and MERGED_LABEL not in labels):
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
                if scene["base_branch"] != config["base_branch"]:
                    raise UnrecoverableDeliveryError(
                        f"resume scene base_branch={scene['base_branch']} "
                        f"differs from configured base_branch="
                        f"{config['base_branch']}; the PR is frozen on a "
                        "different base and must not be reviewed or "
                        "merged against the configured one — a base "
                        "change is a human decision, so auto-retrying "
                        "would keep failing on the same mismatch"
                    )
                worktree = worktree_path(
                    config["repo_dir"], source_repo, number,
                    scene["run_id"],
                )
                # Derived from the same trusted inputs (never read from
                # a comment) BEFORE the worktree check: a missing
                # worktree is a recoverable failure whose comment must
                # carry the full scene including the branch (Issue
                # #50), so the branch must be set even when the
                # directory does not exist.
                branch = task_branch(source_repo, number, scene["run_id"])
                # Issue #90 + #50: the worktree is derived from the
                # configured repo_dir, source repo, Issue number and
                # run id (never read from a comment). A missing
                # directory is a RECOVERABLE failure: the branch still
                # exists on the remote and the worktree can be
                # recreated (git worktree add) on the next resume, so
                # the handler below keeps the Issue in the automatic
                # fix loop (ai-fix-needed) with the PR and branch
                # preserved.
                if not worktree.is_dir():
                    raise RuntimeError(f"worktree missing: {worktree}")
                review_config = {
                    **config,
                    "base_sha": scene["base_sha"],
                    "run_id": scene["run_id"],
                }
                merged = review_and_merge_if_clean(
                    worktree, branch, config["base_branch"],
                    review_config, source_repo, number,
                    title=title, priority=priority,
                )
            except Exception as exc:
                LOGGER.exception(
                    "issue=%s delivery_review_failed pr=%s", number, pr_url,
                )
                detail = _failure_detail(exc)
                evidence = _failure_evidence(worktree, exc)
                if is_unrecoverable_failure(exc):
                    # Issue #50: the ONLY opened-PR failure that leaves
                    # the automatic loop is an external precondition
                    # the AI cannot safely judge or fix: the Issue is
                    # marked ai-blocked ALONE (the opened-PR state
                    # label, `ai-pr-opened` or `ai-fix-needed`, is
                    # removed) and the failure comment states the
                    # explicit reason why automatic recovery is
                    # impossible.
                    # The current labels are read ONCE before the
                    # transition: the blocked patch clears every
                    # delivery-state label that is present (`ai-pr-opened`,
                    # and `ai-fix-needed` when the failure happened while
                    # awaiting the next review session — Issue #82 routes
                    # both opened-PR states into the same review), so the
                    # terminal state is `ai-blocked` alone.
                    labels = issue_labels(number, source_repo)
                    apply_label_patch(
                        number, repo=source_repo, event=EVENT_BLOCKED,
                        current_labels=labels,
                    )
                    body = (
                        f"Orbi failed: the independent review of "
                        f"PR {pr_url} failed: {detail}; this is an "
                        "external precondition the AI cannot safely "
                        "judge or fix, so it cannot be recovered "
                        "automatically (the Issue stays ai-blocked "
                        "until a human decides)"
                    )
                    body += evidence
                    if marker:
                        body = f"{marker}\n{body}"
                    comment_issue(number, repo=source_repo, body=body)
                    if run_id:
                        # Issue #79: the blocked-scene progress
                        # publishing is bypass — a 404 here must not
                        # escape the wait loop (the terminal
                        # bookkeeping above already completed and the
                        # slot must be released).
                        _safe_publish(
                            run_id=run_id, issue=number,
                            source_repo=source_repo, role=ROLE_REVIEW,
                            action=lambda: ProgressPublisher(
                                number, source_repo, run_id,
                                run_command=run_command,
                            ).milestone(
                                f"blocked: the independent review of "
                                f"PR {pr_url} failed: {detail}"
                            ),
                        )
                        # The blocked scene carries the actual role and
                        # the completed review rounds (review round 2,
                        # PR #42): the failure happened during the
                        # independent review, and the trusted
                        # review-round comments bound the round count
                        # (GitHub is the only state store).
                        _safe_publish(
                            run_id=run_id, issue=number,
                            source_repo=source_repo, role=ROLE_REVIEW,
                            action=lambda: _finish_blocked_progress(
                                number, run_id, source_repo, worktree,
                                branch, pr_url,
                                f"the independent review of PR {pr_url} "
                                f"failed: {detail}; this is an external "
                                "precondition the AI cannot safely "
                                "judge or fix, so it cannot be "
                                "recovered automatically (the Issue "
                                "stays ai-blocked until a human "
                                "decides)",
                                "fix the precondition above (see the "
                                "reason) and relabel the Issue "
                                "ai-fix-needed to resume this same PR",
                                title=title,
                                role=ROLE_REVIEW,
                                review_round=review_rounds_so_far(
                                    issue_comments(number, repo=source_repo),
                                ),
                                priority=priority,
                            ),
                        )
                    return
                # Issue #50: a RECOVERABLE failure (Pi execution
                # failure, model wait, runner exception,
                # missing/malformed verdict, missing worktree,
                # unpushed local commit) keeps the Issue in the
                # automatic fix loop: `ai-fix-needed` (the next timer
                # resumes the same run, branch, worktree and PR — never
                # ai-blocked, never a replacement PR). The failure
                # comment carries the full scene and is written to the
                # Issue AND the PR.
                apply_label_patch(
                    number, repo=source_repo, event=EVENT_FIX_NEEDED,
                    current_labels=(),
                )
                body = (
                    f"Orbi needs a fix: the independent review of "
                    f"PR {pr_url} failed: {detail}; the Issue stays "
                    "ai-fix-needed and the next tick resumes the same "
                    "run, branch, worktree and PR"
                )
                body += evidence
                # The full scene (run_id, branch, worktree, session,
                # phase, last activity) is always appended: a
                # recoverable failure always happens after the scene
                # was recovered and the worktree derived (a failure
                # before the derivation is unrecoverable and never
                # reaches this branch), so worktree, branch and run_id
                # are set here. The session fields are '-' when no
                # session file exists yet (the Pi never started or the
                # dir is gone); a snapshot read failure is logged and
                # reported as "no session yet" (best-effort
                # observability, never a second failure).
                snapshot = None
                try:
                    snapshot = activity_snapshot(
                        worktree / ".pi-session",
                    )
                except Exception:
                    LOGGER.exception(
                        "issue=%s activity scene failed", number,
                    )
                if snapshot is None:
                    snapshot = {
                        "session_id": None, "session_file": None,
                        "phase": "starting",
                        "last_activity": None, "action": None,
                        "result": None,
                    }
                body += "\n" + format_run_scene(
                    snapshot,
                    run_id=run_id or "-",
                    issue=issue_context(source_repo, number),
                    role=ROLE_REVIEW, branch=branch,
                    worktree=str(worktree),
                )
                if marker:
                    body = f"{marker}\n{body}"
                comment_issue(number, repo=source_repo, body=body)
                comment_pr(
                    _pr_number(pr_url), repo=source_repo, body=body,
                )
                if run_id:
                    # Issue #79: the fix-needed-scene progress
                    # publishing is bypass — a 404 here must not escape
                    # the wait loop (the label transition above already
                    # completed and the slot must be released).
                    _safe_publish(
                        run_id=run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: ProgressPublisher(
                            number, source_repo, run_id,
                            run_command=run_command,
                        ).milestone(
                            f"fix needed: the independent review of "
                            f"PR {pr_url} failed: {sanitize(detail)}"
                        ),
                    )
                    _safe_publish(
                        run_id=run_id, issue=number,
                        source_repo=source_repo, role=ROLE_REVIEW,
                        action=lambda: _finish_fix_needed_progress(
                            number, run_id, source_repo, worktree,
                            branch, pr_url,
                            f"the independent review of PR {pr_url} "
                            f"failed: {detail}",
                            title=title,
                            review_round=review_rounds_so_far(
                                issue_comments(number, repo=source_repo),
                            ),
                            priority=priority,
                        ),
                    )
                return
            if merged:
                LOGGER.info(
                    "issue=%s delivery_auto_merged pr=%s; releasing the "
                    "slot",
                    number, pr_url,
                )
                return
            continue  # findings: the next iteration re-runs the review
        LOGGER.info(
            "issue=%s delivery_awaiting pr=%s state=%s",
            number, pr_url, state,
        )
        time.sleep(poll_interval)


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
    except ValueError as exc:
        LOGGER.error("config_invalid reason=%s", exc)
        return 1
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
        config["deploy_home"], run_command=run_command,
    )
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
        check_unit_drift(config["deploy_home"])
    except UnitDriftError:
        sync_drifted_units(
            config["deploy_home"],
            max_concurrency=config["max_concurrency"],
            run_command=run_command,
        )
    # Git transport preflight (Issue #114): BEFORE any slot or claim
    # the deployment checkout's git transport must be SSH and reachable
    # (the task worktrees share the checkout's single `origin` remote,
    # so the worktree's fetch/push — including `.github/workflows/*.yml`
    # — uses it). A broken transport fails the start with the
    # structured reason: no slot, no claim, no label change, no HTTPS
    # fallback. The `gh` token (GitHub API) is untouched.
    try:
        transport = check_transport(
            config["repo_dir"], config["source_repos"],
            run_command=run_command, migrate=False,
        )
    except TransportError as exc:
        LOGGER.error(
            "transport_check_failed repo_dir=%s source_repos=%s "
            "reason=%s",
            config["repo_dir"], config["source_repos"], exc,
        )
        raise
    LOGGER.info(
        "transport clean remote=%s protocol=%s url=%s "
        "ssh_reachable=%s",
        transport.get("remote", "-"), transport.get("protocol", "-"),
        transport.get("url", "-"), transport.get("ssh_reachable", "-"),
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
    # Concurrency cap (Issue #39): take one slot BEFORE claiming anything.
    # The slot is held for the whole delivery lifecycle (implement ->
    # review -> fix -> merge) and released only after the delivery is
    # merged or terminally failed — or when this process exits for any
    # reason, which the kernel handles (flock on an open descriptor).
    slot = acquire_slot(
        config["slot_dir"], config["max_concurrency"], os.getpid(),
    )
    if slot is None:
        LOGGER.info(
            "capacity_full max_concurrency=%s slot_dir=%s",
            config["max_concurrency"], config["slot_dir"],
        )
        return 0
    try:
        selected = pick_next_delivery(
            config["source_repos"], config["slot_dir"],
            config["max_concurrency"],
            config["active_milestone"],
        )
        if selected is None:
            LOGGER.info(
                "source_repos=%s outcome=no_ready_issue",
                config["source_repos"],
            )
            return 0
        source_repo, issue, scene = selected
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
                    config["repo_dir"], source_repo,
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
            pr_url = verify_resumed_pr(scene, issue, config, source_repo)
        else:
            pr_url = process_issue(issue, config, source_repo)
            # Ticket-only delivery is complete when its Agent output is
            # posted and the source Issue is closed; it has no PR to review
            # or merge (Issue #209).
            if is_ticket_only(issue):
                return 0
            # Issue #269: a release delivery returns a Release URL, not a
            # PR URL. `process_release` already closed the delivery (tag
            # pushed, GitHub Release published, Issue `ai-merged` and
            # closed), so the tick ends here — entering the PR wait made
            # `_pr_number` raise `ValueError` on the tag name and crashed
            # the Runner (run_id=37216af5).
            if is_release(issue):
                return 0
            # Issue #239: a terminal delivery failure — `process_issue`
            # already marked the Issue `ai-blocked` and posted the failure
            # comment; there is no PR to wait for, so the tick ends
            # cleanly instead of crashing the service on the handled
            # failure.
            if pr_url is None:
                return 0
        # The delivery is not done when the PR is open: hold the slot
        # through review -> merge and release it only after the PR is
        # merged or terminally failed (Issue #39).
        wait_for_delivery(pr_url, issue, config, source_repo)
    finally:
        slot.release()
        # The delivery is over (merged, terminally failed, or the tick
        # found no work): a stop from here on is idle again (Issue #48).
        clear_active_run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
