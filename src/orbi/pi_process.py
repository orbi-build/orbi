#!/usr/bin/env python3
"""Pi process execution subsystem (moved from `runner.py`).

Spawns a Pi session and streams its live activity into the journal, owns the
Pi-process failure classes and the poll/model-wait/429 retry tuning
constants, and drives the two pieces extracted in Issue #1860: the idle
recovery strategy (`orbi.pi_idle`) and the stream logging/exit
classification (`orbi.pi_stream_log`). Neither extracted module imports this
one; the lifecycle hooks it needs (`issue_context`, `set_active_pi`) come
from `orbi.journal` at module level, so this module never imports `runner`
(the runner imports this module — the circular-import rule).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import select
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from orbi.delivery_scene import RunContext
from orbi.journal import (
    RunIdFilter,
    event,
    issue_context,
    set_active_pi,
)
from orbi.pi_activity import (
    SessionWatcher,
    format_duration,
    format_run_scene,
    refresh_session_evidence,
)
# The extracted modules (Issue #1860). The names below that this module does
# not call itself stay imported on purpose: `runner`, `pi_session` and the
# tests read them from here, exactly as before the split.
from orbi.pi_idle import (
    PI_IDLE_RECOVERY_CYCLES,
    PI_IDLE_WARN_SECONDS,
    PI_IDLE_WAIT_MAX_SECONDS,
    IdleRecoveryTracker,
)
from orbi.pi_recovery import (
    STOP_CHILD_GRACE_SECONDS,
    shutdown_child,
    slots_idle,
    upstream_alive,
)
from orbi.pi_stream_log import (
    _classify_startup_exit,
    _decode_chunks,
    _drain_stream,
    _is_rate_limited,
    _log_activity,
    _log_heartbeat,
    _log_run_failed,
    _log_startup,
    _log_startup_failed,
    _startup_failed_reason,
    log_provider_config_loaded,
)


LOGGER = logging.getLogger("orbi.pi_process")

LOGGER.addFilter(RunIdFilter())

# Live activity polling while Pi runs: every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0


# Hung-model-request detection (safe recovery): while the newest
# session event is a tool result (model_wait)
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
# The TOML field `model_wait_dead_seconds` overrides this
# default (1800 s, 30 minutes — a slow local model (e.g. a 27B Q4 GGUF
# behind a llama-server with a 1200 s request timeout: ~57 tokens/s at
# 12K context on an RX 7900 XTX with all layers on GPU, well under 20
# tokens/s on partial GPU offload or CPU-only) must survive a
# 10-minute complete message; the pre-#228 default of 600 s killed
# them at exactly 10 minutes, #176/#175/#173/#168).
PI_MODEL_WAIT_DEAD_SECONDS = 1800.0


# Swallowed-model-request probe: while Pi is frozen in
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
# a pure bypass: a probe failure is inconclusive and the
# `model_wait_dead_seconds` bound still applies. The TOML field
# `model_wait_probe_seconds` overrides this default.
PI_MODEL_WAIT_PROBE_SECONDS = 60.0


@dataclasses.dataclass(frozen=True)
class SteeringRequest:
    """A trusted Issue correction discovered during an active session.

    `body_revision` marks a correction that comes from an edited Issue
    body rather than a new comment (Issue #1094) — the journal event
    names the difference.
    """

    context: str
    comment_ids: tuple[str, ...]
    author: str
    body_revision: bool = False


@dataclasses.dataclass(frozen=True)
class PiWatchOptions:
    """The `stream_pi` watcher's cadence and threshold knobs.

    The five values always travel together and change together (they
    tune ONE watcher); one frozen bundle replaces the five loose
    keyword parameters. The defaults are the module constants — the
    exact values every caller passed before the bundle existed.
    """

    poll_interval: float = PI_POLL_INTERVAL
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS
    model_wait_dead_seconds: float = PI_MODEL_WAIT_DEAD_SECONDS
    model_wait_probe_url: str | None = None
    model_wait_probe_seconds: float = PI_MODEL_WAIT_PROBE_SECONDS
    # The bounded wait between the TERM and the KILL when the session's
    # exit path reaps a child that does not exit on TERM (Issue #1995):
    # the same grace the Runner's stop handler uses.
    child_grace_seconds: float = STOP_CHILD_GRACE_SECONDS
    # Session activity timestamps are normally compared with wall clock
    # time. Tests can provide a deterministic clock without changing the
    # watcher's production defaults.
    activity_clock: Callable[[], float] | None = None
    steering_poll_seconds: float = 60.0
    steering_check: Callable[[], SteeringRequest | None] | None = None


class SteeringRequested(RuntimeError):
    """Internal control flow for restarting after a trusted correction."""

    def __init__(self, request: SteeringRequest, round: int, phase: str):
        super().__init__(request.context)
        self.context = request.context
        self.comment_ids = request.comment_ids
        self.author = request.author
        self.body_revision = request.body_revision
        self.round = round
        self.killed_phase = phase


# Provider rate-limit retry: a Pi session killed by a
# provider 429 (the free-tier burst scene — the stderr carries `429` /
# `quota` / `RESOURCE_EXHAUSTED` / `retry in`) is TRANSIENT throttling,
# not a task failure: the SAME session is re-spawned in the same run
# after a backoff instead of failing into the terminal path. The backoff
# honors the response's own retry hint (`retry in Ns` / `retryDelay:
# Ns`) clamped to `PI_RATE_LIMIT_BACKOFF_MAX_SECONDS`; without a hint it
# doubles from `PI_RATE_LIMIT_BACKOFF_SECONDS` (30 s) up to the same
# cap. The slot is held for the whole wait (the #233 positioning: the
# Runner never claims a new Issue while this run waits). Only after
# PI_RATE_LIMIT_RETRIES backoff retries still end in a 429 exit (6
# consecutive 429 exits at the default 5) does the terminal failure run
# — `RateLimitExhaustedError` (`ai-blocked`), with the cumulative count
# and the quota repair action in the message (the count is
# the RUN's, persisted across restarts, so the limit actually bites).
# Long-term quota exhaustion is OUT of scope here (#313): this loop
# only bridges short burst windows. The marker set the exit classifier
# matches lives with the classifier in `orbi.pi_stream_log` (Issue #1860).
PI_RATE_LIMIT_RETRIES = 5
PI_RATE_LIMIT_BACKOFF_SECONDS = 30.0
PI_RATE_LIMIT_BACKOFF_MAX_SECONDS = 300.0


# The 429 retry counter is per-DELIVERY-ATTEMPT state, not per-process
# state: the pre-#698 counter lived in a `stream_pi` local
# and reset on every runner restart, so `PI_RATE_LIMIT_RETRIES` was
# unreachable as a terminal outcome (the beta incident: 22 restarts,
# 19×429, zero output, the Issue stuck `ai-in-progress`). The count is
# persisted in the task worktree's gitignored `.orbi/` run dir — a
# resumed run (same run_id, same worktree) continues where the killed
# process stopped, and the worktree dies with the terminal cleanup, so a
# NEW attempt (new run_id, new worktree) starts from 0. The terminal
# itself also clears the file: the documented repair (relabel ai-ready)
# must get a full fresh budget even when the retry reuses the SAME
# worktree and run_id — the open-PR review resume does exactly that.
PI_429_ATTEMPTS_FILENAME = "pi-429-attempts.json"


def pi_429_attempts_path(cwd: Path) -> Path:
    """The persisted 429 retry counter file of one task worktree."""
    return cwd / ".orbi" / PI_429_ATTEMPTS_FILENAME


def _load_429_attempts(cwd: Path, run_id: str) -> int:
    """Return the cumulative 429 retry count of this run, or 0.

    An unreadable, malformed or foreign-run file counts as 0 with one
    warning line: the worst case is the pre-#698 behavior (the count
    restarts), never a false terminal — the delivery must not fail on
    its own observability artifact.
    """
    path = pi_429_attempts_path(cwd)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        event(
            "pi_429_attempts_reset", level=logging.WARNING,
            path=path, reason="unreadable",
        )
        return 0
    valid = (
        isinstance(state, dict)
        and state.get("run_id") == run_id
        and isinstance(state.get("attempts"), int)
        and not isinstance(state.get("attempts"), bool)
        and state["attempts"] >= 0
    )
    if not valid:
        event(
            "pi_429_attempts_reset", level=logging.WARNING,
            path=path, reason="foreign_or_invalid",
        )
        return 0
    return state["attempts"]


def _record_429_attempts(cwd: Path, run_id: str, attempts: int) -> None:
    """Persist the cumulative count BEFORE the backoff sleep (Issue
    #698): a kill during the wait keeps every retry already spent. The
    replace is atomic (Issue #1851), never a torn counter, and a failed
    write never breaks the retry loop (the in-memory count bounds it)."""
    path = pi_429_attempts_path(cwd)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"run_id": run_id, "attempts": attempts}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        event(
            "pi_429_attempts_write_failed", level=logging.WARNING,
            path=path,
        )


# The bootstrap runner streams every Pi session of a run through the same
# live activity pipeline; implement/review share the same
# line format and carry their role (one run_id end to end, the
# roles are steps of the same run). There is no cold-start fixer
# role: the review session fixes findings in the same session, so a
# run has one implement session and up to MAX_REVIEW_ROUNDS review
# sessions (one per review round; the runner owns the budget).
ROLE_IMPLEMENT = "implement"


class RecoverablePiFailure(RuntimeError):
    """A Pi process failure that leaves the run resumable.

    The session may have failed because the model/process infrastructure was
    unavailable (including idle recovery).  The worktree and run-state file
    must survive so the next pickup resumes this run instead of starting a
    fresh attempt.
    """


class RecoverablePiProcessError(subprocess.CalledProcessError, RecoverablePiFailure):
    """Pi exited non-zero; retain CalledProcessError diagnostics."""


class RecoverablePiTimeoutError(subprocess.TimeoutExpired, RecoverablePiFailure):
    """Pi exceeded its bounded execution window."""


class ModelWaitDeadError(RuntimeError):
    """The hung-model-request recovery killed the Pi
    session: the model request is HUNG (the model service process is alive
    but the request never completes, the session JSONL froze in
    `model_wait` past `model_wait_dead_seconds`).

    This is a CLASSIFIED, AI-recoverable delivery failure:
    the worktree keeps the interrupted work and the run state file is
    intact, so `process_issue` keeps the Issue `ai-in-progress` and the
    next tick's in-flight restart scan resumes the SAME run (same run id,
    branch, worktree, progress comment). It is never `ai-blocked` and
    never an unclassified top-level exception: the recovery stays
    fail-fast (Pi killed, the slot released by the tick ending) but its
    terminal outcome goes through the recoverable delivery path."""


class ProviderRateLimitedError(RuntimeError):
    """The Pi session exited because the provider throttled it (a 429
    marker on the session's stderr): TRANSIENT rate limiting,
    not a task failure.

    Internal to `stream_pi`'s retry loop: the loop either re-spawns the
    session after the backoff or raises the terminal
    `RateLimitExhaustedError` through `_fail_rate_limited` — it never
    escapes `stream_pi` itself. The exception carries the exit scene
    (returncode, captured streams, the refreshed session activity) for
    the retry logs and the terminal failure. The stderr text itself
    never reaches a message (only the class — the same no-leak rule as
    the startup reasons)."""

    def __init__(self, *, returncode: int, stdout: str, stderr: str,
                 activity: dict) -> None:
        super().__init__(
            f"provider rate limited the Pi session (exit {returncode})"
        )
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.activity = activity


class RateLimitExhaustedError(RuntimeError):
    """The rate-limit backoff budget of one delivery attempt is
    exhausted: `PI_RATE_LIMIT_RETRIES` cumulative 429 exits
    across every restart of the same run_id — TERMINAL, never a
    recoverable resume.

    Deliberately NOT a `RecoverablePiFailure`: the pre-#698 recoverable
    replay resumed the same run with a reset counter — the unbounded
    loop this issue fixes. A persistently throttled provider is an
    external condition the runner must stop retrying, and it must stop
    in BOTH phases: the implement phase's generic handler terminates
    the delivery (`ai-blocked` — the documented no-PR failure terminal;
    `ai-fix-needed` is an opened-PR state a no-PR Issue cannot resume
    from), and the opened-PR review phase classifies it through
    `is_unrecoverable_failure` — the recoverable `ai-fix-needed` there
    would resume the review with the persisted counter already at the
    limit: one 429 exit per tick, forever. The failure message is the
    operator-facing cause and repair: relabel `ai-ready` to retry after
    the quota window resets (the terminal clears the persisted counter,
    so the human-requeued retry starts with a full budget — in the
    review phase it reuses the same worktree and run_id). The operator
    must see the provider quota, not the task, as the cause."""


# The drain window of the child's output pipes (the drain itself lives
# in `orbi.pi_stream_log`, which owns the session's streams).
_DRAIN_SILENCE_S = 30.0


# The 429 response's own retry hint, in the two shapes observed in the
# wild (the Google 429 text "Please retry in
# 36.123456s" — measured 2s -> 19s -> 36s — and the google.rpc.RetryInfo
# JSON field `"retryDelay": "36s"`).
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"retrydelay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s", re.IGNORECASE),
)


def _retry_after_seconds(stderr: str) -> float | None:
    """The 429 response's own retry hint in seconds, or None (Issue
    #321)."""
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(stderr)
        if match:
            return float(match.group(1))
    return None


def _backoff_seconds(attempt: int, stderr: str) -> float:
    """The wait before retry `attempt + 1` (`attempt` is 0-based, Issue
    #321): the response's own hint when present (capped at
    `PI_RATE_LIMIT_BACKOFF_MAX_SECONDS`), else the default escalation —
    `PI_RATE_LIMIT_BACKOFF_SECONDS` doubling per attempt, same cap."""
    hinted = _retry_after_seconds(stderr)
    if hinted is not None:
        return min(hinted, PI_RATE_LIMIT_BACKOFF_MAX_SECONDS)
    return min(
        PI_RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt),
        PI_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    )


# A provider usage-limit stop (Issue #1374): Pi writes it only into the
# session journal's assistant `errorMessage` (`Codex error: The usage
# limit has been reached`) and exits 1. The run stays recoverable — the
# quota recovers on its own and the normal tick cadence retries — but the
# journal must show it as a quota stop, not a crash.
_PROVIDER_QUOTA_MESSAGE = "usage limit has been reached"


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    ctx: RunContext,
    role: str = ROLE_IMPLEMENT,
    timeout: int | None = None, rate_limit_retries: int = PI_RATE_LIMIT_RETRIES,
    watch: PiWatchOptions = PiWatchOptions(),
    log_command: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
    pi_env: dict[str, str] | None = None,
) -> str:
    """Run Pi and stream concise live activity into the journal.

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

    Startup phases: `process_spawned` is logged right
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
    `network_timeout`, `provider_rate_limited`, `pi_exit_<N>`,
    `timeout`, `first_response_timeout`, `model_wait_swallowed`,
    `idle_recovery_stale`); the existing `run_failed` line and the
    raised failure are unchanged (fail-fast semantics preserved).

    Provider rate-limit retry: when Pi exits non-zero and
    its stderr carries a provider 429 marker (`429` / `quota` /
    `RESOURCE_EXHAUSTED` / `retry in`), the exit is classified as
    TRANSIENT throttling — not a task failure — and the SAME Pi session
    is re-spawned IN THIS RUN after a backoff, the slot held for the
    whole wait (the #233 positioning: no new Issue is claimed while
    this run waits). The backoff honors the response's own retry hint
    (`retry in Ns` / `retryDelay: Ns`) capped at 5 minutes; without a
    hint it doubles from 30 s to the same cap. Every retry logs one
    `pi_retry_429` line (run id, attempt, next_retry_in). Only after
    the session's backoff budget `rate_limit_retries` (default 5;
    `orbi suggest`'s auxiliary session passes 0, #1600) is exhausted —
    `RateLimitExhaustedError`, never a recoverable resume (the attempt count is persisted per run in the worktree's `.orbi/`
    dir, so it keeps rising across runner restarts and the limit
    actually bites) — with the `run_failed` scene marked
    `reason=provider_rate_limited`. Long-term quota exhaustion stays out
    of scope (#313) — no wait window is added. The one annotated non-429
    exception is a usage-limit stop carried in the session journal's
    newest `errorMessage`: it logs one `provider_quota` WARNING and rides
    into the raised failure's detail (Issue #1374); every other non-429
    exit is untouched.

    `progress` is invoked on EVERY poll — an activity change
    or a heartbeat — with the current activity state, while the Pi
    process is still running: the caller renders the live GitHub
    progress comment and PATCHes the same run-marker comment in place,
    so mobile users never see a static starting comment for the whole
    run. A callback error is logged and never interrupts the task
    (observability is best-effort, the delivery is not).

    Idle warning: when no model/session event arrives for
    `idle_warn_seconds` (default 5 minutes) and the state is NOT
    model_wait, ONE `pi_idle` WARNING carries `stale_seconds`; the
    first new session event after it logs `pi_resumed`. A slow active
    model (model_wait) is never reported idle.

    Idle-stall recovery: the warning is no longer the end
    of the story. While the session stays stalled (no new activity,
    not model_wait) the runner recovers it, one step per idle window
    of `idle_warn_seconds` since the stall was first seen:
    window 1 SIGTERMs the Pi descendants that already existed before
    the window (the hung tools — found by the ppid chain in
    `/proc/<pid>/stat` plus their start time, never a name guess) so
    the tool gets a non-zero exit and the failure signal reaches the
    model; window 2 SIGKILLs a target that survived; after
    `PI_IDLE_RECOVERY_CYCLES` consecutive idle windows the
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
    run_id: str = ctx.run_id
    branch: str = ctx.branch
    issue_ref = issue_context(ctx.source_repo, ctx.issue)
    session_dir = cwd / ".pi-session"
    # The counter is the RUN's, not this invocation's — a
    # resumed run continues where the killed process stopped.
    attempt = _load_429_attempts(cwd, run_id)
    steering_round = 0
    while True:
        # Session files that already exist before this Pi process starts
        # are never followed: a
        # resumed run — and a retried invocation —
        # re-snapshots the baseline so the previous invocation's JSONL is
        # never reported as this attempt's session.
        known_files = (
            {path for path in session_dir.glob("*.jsonl") if path.is_file()}
            if session_dir.is_dir() else set()
        )
        try:
            return _stream_pi_once(
                command, cwd=cwd, timeout=timeout,
                poll_interval=watch.poll_interval,
                idle_warn_seconds=watch.idle_warn_seconds,
                model_wait_dead_seconds=watch.model_wait_dead_seconds,
                model_wait_probe_url=watch.model_wait_probe_url,
                model_wait_probe_seconds=watch.model_wait_probe_seconds,
                child_grace_seconds=watch.child_grace_seconds,
                run_id=run_id, issue_ref=issue_ref, branch=branch,
                role=role, safe_command=safe_command, progress=progress,
                pi_env=pi_env, session_dir=session_dir,
                known_files=known_files,
                activity_clock=watch.activity_clock,
                steering_poll_seconds=watch.steering_poll_seconds,
                steering_check=watch.steering_check,
                steering_round=steering_round,
            )
        except SteeringRequested as exc:
            command[-1] = exc.context
            steering_round = exc.round
            event(
                "delivery_steered", issue=issue_ref, role=role,
                comment_ids=exc.comment_ids, author=exc.author,
                body_revision=exc.body_revision,
                round=exc.round, killed_phase=exc.killed_phase,
            )
            continue
        except ProviderRateLimitedError as exc:
            if attempt >= rate_limit_retries:
                _fail_rate_limited(
                    exc, attempts=attempt, limit=rate_limit_retries,
                    run_id=run_id, issue_ref=issue_ref, role=role,
                    branch=branch, cwd=cwd,
                )
            delay = _backoff_seconds(attempt, exc.stderr)
            attempt += 1
            # Persisted BEFORE the sleep: a kill during the wait keeps
            # every retry already spent.
            _record_429_attempts(cwd, run_id, attempt)
            event(
                "pi_retry_429", level=logging.WARNING,
                run_id=run_id, issue=issue_ref, role=role,
                attempt=attempt, next_retry_in=f"{int(delay)}s",
                limit=rate_limit_retries,
                session=exc.activity.get("session_id") or "-",
            )
            time.sleep(delay)


def _fail_rate_limited(
    exc: ProviderRateLimitedError, *, attempts: int, limit: int,
    run_id: str, issue_ref: str, role: str, branch: str, cwd: Path,
) -> NoReturn:
    """The exhausted-retries terminal failure: `attempts` cumulative 429 exits (the RUN's count, persisted
    across restarts) have burned the session's `limit`-retry backoff
    budget, so the delivery terminates — `RateLimitExhaustedError`, never
    a recoverable resume (terminal in both phases, implement and
    opened-PR review). The terminal also CLEARS the persisted counter:
    this attempt's budget is spent and reported on the Issue, and the
    documented repair (relabel ai-ready) must start a FULL fresh budget
    even when the retry reuses the same worktree and run_id. A failed
    clear is one warning line — the terminal decision never depends on
    its own artifact cleanup. The per-attempt
    `startup_failed` lines are already in the journal (one per 429 exit
    before the first response), so this is the single terminal log."""
    activity = exc.activity
    _log_run_failed(
        activity, run_id=run_id, issue_ref=issue_ref, role=role,
        branch=branch, cwd=cwd, reason="provider_rate_limited",
    )
    try:
        pi_429_attempts_path(cwd).unlink(missing_ok=True)
    except OSError:
        event(
            "pi_429_attempts_clear_failed", level=logging.WARNING,
            path=pi_429_attempts_path(cwd),
        )
    raise RateLimitExhaustedError(
        f"provider rate limit retries exhausted: {attempts + 1} "
        f"consecutive 429 exits (limit={limit} retries); "
        "the cause is the provider quota, not the task; repair: relabel "
        "the Issue ai-ready to retry after the quota window resets"
    )


def _stream_pi_once(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None,
    poll_interval: float,
    idle_warn_seconds: float,
    model_wait_dead_seconds: float,
    model_wait_probe_url: str | None,
    model_wait_probe_seconds: float,
    child_grace_seconds: float,
    run_id: str,
    issue_ref: str,
    branch: str,
    role: str,
    safe_command: list[str],
    progress: Callable[[dict], None] | None,
    pi_env: dict[str, str] | None,
    session_dir: Path,
    known_files: set[Path],
    activity_clock: Callable[[], float] | None,
    steering_poll_seconds: float = 60.0,
    steering_check: Callable[[], SteeringRequest | None] | None = None,
    steering_round: int = 0,
) -> str:
    """Spawn and stream ONE Pi session attempt: the whole
    pre-#321 `stream_pi` body — the `run_start` scene, the live
    activity/heartbeat lines, the idle/model-wait kill paths and the
    evidence-based exit classification. A 429-classified non-zero exit
    raises `ProviderRateLimitedError` so the `stream_pi` retry loop can
    back off and re-spawn; every other failure raises exactly as
    before. The full streamer contract lives on `stream_pi`."""
    watcher = SessionWatcher(
        session_dir,
        now=time.time if activity_clock is None else activity_clock,
        known_files=known_files,
    )
    start = time.monotonic()
    # The initial state is what run_start already reported; activity lines
    # are only emitted when the visible fields actually change.
    initial = watcher.poll()
    last_visible = (initial["phase"], initial["action"], initial["result"])
    # `pi_env` carries the per-run Pi agent dir
    # (`PI_CODING_AGENT_DIR`, verified against real Pi 0.84.3) so the
    # configured provider file is visible to Pi. Absent -> the child gets
    # the Runner's environment minus the notification token.
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Pi's print mode reads piped stdin until it closes and prepends
        # it to the prompt (Issue #1562): the child must get /dev/null,
        # never the Runner's possibly-open stdin pipe.
        stdin=subprocess.DEVNULL,
        env={k: v for k, v in {**os.environ, **(pi_env or {})}.items() if k != "ORBI_TELEGRAM_BOT_TOKEN"},
    )
    # Track the live Pi child for the stop handler: a
    # SIGTERM during this window must shut the child down, never
    # orphan it. Cleared again once the child is reaped (finally).
    set_active_pi(process)
    # Startup phase: the process is spawned — the first
    # sub-phase of `starting` is now observable (pid, elapsed since
    # spawn). The run_start scene line below carries the same
    # pre-session state (phase=session_pending); from here the live
    # lines and the milestone lines carry the startup sub-phases.
    _log_startup(
        "process_spawned", issue_ref=issue_ref, role=role,
        activity=initial, elapsed=0.0, extra=f"pid={process.pid}",
    )
    event(
        "run_start",
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
    # Startup milestones already reported: each flips once
    # per session file (a resumed run creates a NEW file, and its
    # first request/response are the new session's — the watcher
    # resets the flags on the switch, so the lines fire again for the
    # new session, exactly once each).
    startup_seen = (False, False, False)
    # Swallowed-model-request probe state: the monotonic
    # moment the /slots probe first reported every slot idle while Pi was
    # in model_wait (None until then). Reset whenever a slot is processing,
    # the probe is inconclusive, or model_wait is left. When the idle
    # state has been sustained for `model_wait_probe_seconds` the request
    # is declared swallowed and Pi is killed fast (well before the
    # model_wait_dead_seconds bound).
    probe_first_idle: float | None = None
    steering_checked_at = time.monotonic()
    model_wait_swallowed = False
    # model_wait transitions: one line when the state is
    # entered and one when it is left; unchanged polls are heartbeats
    # that carry the state, so a slow model never looks idle and no
    # warning is ever escalated from a slow response.
    last_model_wait = activity["model_wait"]
    # Idle warning state: at most one `pi_idle` warning per
    # stall; the first new session event after it logs `pi_resumed`.
    idle_warned = False
    # Idle-stall recovery state (the strategy
    # class): the escalation (window bookkeeping, TERM →
    # KILL → session-kill, the timeout evidence chain) lives in the
    # tracker — the loop mirrors the warning machine's transitions to
    # it and reads the state back for the progress comment and the
    # session-kill decision.
    idle_recovery = IdleRecoveryTracker(
        idle_warn_seconds, run_id=run_id, issue_ref=issue_ref, role=role,
    )
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
            if (
                steering_check is not None
                and time.monotonic() - steering_checked_at >= steering_poll_seconds
            ):
                steering_checked_at = time.monotonic()
                try:
                    request = steering_check()
                except Exception as exc:
                    event(
                        "steering_poll_failed", level=logging.WARNING,
                        issue=issue_ref, reason=type(exc).__name__,
                    )
                    request = None
                if request is not None:
                    phase = "model_wait" if activity["model_wait"] else activity["phase"]
                    process.terminate()
                    raise SteeringRequested(request, steering_round + 1, phase)
            # Startup milestones: one line per flip — the
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
            # Once the model_wait silence crosses the dead
            # threshold the wait is DEAD, not slow: the
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
                # heartbeat.
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
                # field: the `[run_id]` prefix carries the run id.
                event(
                    "model_wait" if activity["model_wait"] else "resumed",
                    issue=issue_ref, role=role,
                    phase=activity["phase"],
                    state="model_wait" if activity["model_wait"]
                    else "resumed",
                )
                last_model_wait = activity["model_wait"]
                # Leaving model_wait (the next session event arrived):
                # the swallow-probe window is over — reset it so a later
                # model_wait starts a fresh window.
                if not activity["model_wait"]:
                    probe_first_idle = None
            # Idle warning: a stalled session (no model/
            # session event for `idle_warn_seconds`, and the model is
            # not expected to reply next) logs ONE `pi_idle` warning
            # with the stale time; the first new session event after it
            # logs `pi_resumed`. A slow active model (model_wait) never
            # warns.
            if idle_warned and activity["changed"]:
                # No `run=` field: the `[run_id]` prefix carries the run
                # id.
                event(
                    "pi_resumed", issue=issue_ref, role=role,
                    phase=activity["phase"],
                )
                idle_warned = False
                # The stall is over: the whole recovery state resets
                # — a later stall starts a fresh window.
                idle_recovery.reset()
            elif (
                not activity["model_wait"]
                and not idle_warned
                and activity["stale_seconds"] >= idle_warn_seconds
            ):
                # No `run=` field: the `[run_id]` prefix carries the run
                # id.
                event(
                    "pi_idle", level=logging.WARNING,
                    issue=issue_ref, role=role, phase=activity["phase"],
                    stale_seconds=format_duration(activity["stale_seconds"]),
                )
                idle_warned = True
                # The idle window starts now: the descendants that
                # already exist are recorded as the only recovery
                # targets.
                idle_recovery.open_window(process.pid)
            # Idle-stall recovery (strategy in
            # `IdleRecoveryTracker`): a stalled session (no
            # model/session activity for idle windows, and the model is
            # NOT expected to reply next) is recovered instead of only
            # warning. Escalation, one step per idle window: window 1
            # SIGTERMs the pre-idle descendants (the hung tools),
            # window 2 SIGKILLs a TERMed target that survived, and
            # after `PI_IDLE_RECOVERY_CYCLES` consecutive
            # idle windows the tracker reports `exhausted` — the loop
            # kills the Pi session itself and fails fast through the
            # normal `ai-blocked` path (the slot is never held
            # forever). Every step logs a `pi_idle_term` /
            # `pi_idle_kill` line (run id, pid, cmdline, result) and
            # the live progress comment shows the recovery state via
            # the `recovery` activity field.
            if (
                idle_warned
                and not activity["model_wait"]
                and activity["stale_seconds"] >= idle_warn_seconds
            ):
                idle_recovery.escalate()
                if idle_recovery.exhausted:
                    process.kill()
                    idle_recovery_failed = True
                    break
            # The live progress comment shows the recovery state while
            # it is active; the watcher state is a fresh
            # dict per poll, so the field never leaks into other polls.
            activity["recovery"] = idle_recovery.state
            if progress is not None:
                try:
                    progress(activity)
                except Exception:
                    LOGGER.exception(
                        "progress_publish_failed run=%s issue=%s role=%s",
                        run_id, issue_ref, role,
                    )
            # Swallowed-model-request detection: the model
            # is expected to reply next (model_wait) and the /slots probe
            # (when configured) reports that EVERY slot is idle for the
            # sustained grace — the request was accepted by the upstream
            # but never scheduled into the slot (the #231 scene: process
            # alive, connection ESTABLISHED, slot idle, nothing
            # generating). This is a real hang that the
            # model_wait_dead_seconds bound would only catch minutes
            # later, so the runner kills Pi FAST and fails fast through
            # the normal failure path. The probe is a pure bypass:
            # an inconclusive probe (None) is simply "no
            # evidence" and the model_wait_dead_seconds bound still
            # applies; a slot that is processing (False) resets the idle
            # window (a slow model is not a swallow). Never fires while
            # events keep arriving (a slow generation is not a swallow).
            if (
                model_wait_probe_url is not None
                and activity["model_wait"]
            ):
                if activity["changed"]:
                    # Events arrived since the last poll — a turn may
                    # have completed entirely inside the poll gap. The
                    # sustained-idle window must RESTART: a window
                    # carried across a completed turn killed healthy
                    # sessions (the journal showed idle_seconds far
                    # below the probe grace). This is the "never fires
                    # while events keep arriving" contract enforced
                    # across poll gaps, not just within one poll.
                    probe_first_idle = None
                idle = slots_idle(model_wait_probe_url)
                if idle is True:
                    if probe_first_idle is None:
                        probe_first_idle = time.monotonic()
                    elif (
                        time.monotonic() - probe_first_idle
                        >= model_wait_probe_seconds
                    ):
                        alive = upstream_alive(process.pid)
                        event(
                            "model_wait_swallowed", level=logging.WARNING,
                            issue=issue_ref, role=role,
                            idle_seconds=int(activity["stale_seconds"]),
                            probe_seconds=int(model_wait_probe_seconds),
                            action="kill_pi",
                            session=activity["session_id"] or "-",
                            run_id=run_id,
                            upstream_alive="true" if alive else "false",
                            reason="swallowed_model_request",
                        )
                        process.kill()
                        model_wait_swallowed = True
                        break
                else:
                    # A slot is processing (False) or the probe is
                    # inconclusive (None): no swallow evidence — reset
                    # the idle window so it must be sustained again.
                    probe_first_idle = None
            # Hung-model-request detection: the model is expected to reply next
            # (model_wait) and the session file has been frozen for the
            # dead threshold: the model request is HUNG. A live
            # connection to the upstream (a TCP socket in the live
            # states ESTABLISHED/SYN_SENT/SYN_RECV) is evidence for the
            # journal, never a veto (process alive ≠
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
                event(
                    "model_wait_dead", level=logging.WARNING,
                    issue=issue_ref, role=role,
                    idle_seconds=int(activity["stale_seconds"]),
                    threshold=int(model_wait_dead_seconds),
                    action="kill_pi",
                    session=activity["session_id"] or "-",
                    run_id=run_id,
                    upstream_alive="true" if alive else "false",
                    reason="hung_model_request",
                )
                process.kill()
                model_wait_dead = True
                break
            if process.poll() is not None:
                break
    finally:
        # Reap the child HERE, in the session's single exit path, before
        # the active-process record is cleared (Issue #1995): every
        # termination point (the steering restart's terminate, the
        # timeout/idle/model-wait kills) falls through this block, and
        # the next session starts in the SAME worktree — a child that
        # survived its terminate() would work next to the new agent.
        # The rule is the stop handler's (TERM, bounded grace, KILL,
        # reap); the grace is the watcher's `child_grace_seconds`
        # (same default as the stop handler).
        shutdown_child(process, grace=child_grace_seconds)
        # The child is reaped (or dead): the stop handler must never
        # signal an already-exited process.
        set_active_pi(None)
        # A tool grandchild may hold the pipe write end past
        # Pi's death — abandon the silent tail instead of wedging the
        # tick inside this finally block.
        drained = _drain_stream(process.stdout, stdout_chunks,
                                silence_s=_DRAIN_SILENCE_S)
        drained = _drain_stream(process.stderr, stderr_chunks,
                                silence_s=_DRAIN_SILENCE_S) and drained
        if not drained:
            event(
                "pi_drain_abandoned", level=logging.WARNING,
                issue=issue_ref, role=role,
                reason="pipe_write_end_outlives_pi",
            )
    stdout = _decode_chunks(stdout_chunks)
    stderr = _decode_chunks(stderr_chunks)
    # The last live poll can predate the journal Pi flushed
    # while dying — refresh the journal evidence before the startup
    # line and the exit classification read it.
    activity = refresh_session_evidence(
        activity, session_dir, known_files,
    )
    # Startup failure: a failure before the first response
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
    # The single `run_failed` site: every terminal branch
    # computes its reason and exception and lands here, so a new log
    # field is added once, not per branch. The skeleton itself lives in
    # `_log_run_failed` (shared with the #321 exhausted-retries terminal
    # failure).
    def _fail_run(reason: str, exc: BaseException) -> NoReturn:
        _log_run_failed(
            activity, run_id=run_id, issue_ref=issue_ref, role=role,
            branch=branch, cwd=cwd, reason=reason,
        )
        raise exc
    if idle_recovery_failed:
        stale = format_duration(activity["stale_seconds"])
        _fail_run(
            f"idle_recovery_stale_{stale}",
            RecoverablePiFailure(
                f"Pi session stayed idle for {stale} after idle recovery "
                f"(TERM/KILL of pre-idle descendants); Pi was killed "
                "(Issue #94)"
            ),
        )
    if model_wait_swallowed:
        idle = format_duration(activity["stale_seconds"])
        _fail_run(
            f"model_wait_swallowed_idle_{idle}",
            RecoverablePiFailure(
                f"Pi is stuck in model_wait and the model /slots probe "
                f"reported every slot idle for the sustained grace "
                f"(session frozen {idle}): the model request was swallowed "
                "(the model service process is alive and the connection is "
                "established, but nothing is generating); Pi was killed "
                "(Issue #233)"
            ),
        )
    if model_wait_dead:
        stale = format_duration(activity["stale_seconds"])
        # The classified hung-model-request failure —
        # `process_issue` keeps the Issue `ai-in-progress` (the next tick
        # resumes the same run) instead of the terminal `ai-blocked`.
        _fail_run(
            f"model_wait_dead_stale_{stale}",
            ModelWaitDeadError(
                f"Pi is stuck in model_wait with a frozen session for {stale}: "
                "the model request is hung (the model service process is "
                "alive but the request never completes); Pi was killed "
                "(Issue #218)"
            ),
        )
    if timed_out:
        _fail_run(
            f"timeout_{format_duration(timeout)}",
            RecoverablePiTimeoutError(
                safe_command, timeout, output=stdout, stderr=stderr,
            ),
        )
    if process.returncode != 0:
        # A provider 429 exit is TRANSIENT throttling, not a
        # task failure — the `stream_pi` retry loop backs off and
        # re-spawns this session. The refreshed activity rides on the
        # exception so the retry logs and the exhausted-retries terminal
        # failure carry the exit scene.
        if _is_rate_limited(stderr):
            raise ProviderRateLimitedError(
                returncode=process.returncode,
                stdout=stdout, stderr=stderr, activity=activity,
            )
        # A provider quota stop is carried only in the session journal's
        # newest `errorMessage` (stderr stays empty), so without this the
        # journal reads `returned non-zero exit status 1` and a quota stop
        # looks like a crash. Log it, put the message into the exception
        # detail, and keep the existing recoverable raise (Issue #1374).
        quota_message = activity.get("error_message")
        if (quota_message is not None
                and _PROVIDER_QUOTA_MESSAGE in quota_message.lower()):
            event(
                "provider_quota", level=logging.WARNING,
                issue=issue_ref, detail=quota_message,
            )
            stderr = f"{stderr}\n{quota_message}".strip()
        # A Pi exit after it created a session is an interrupted run: its
        # work is resumable, including exits after idle recovery.  A
        # pre-session startup failure remains terminal unless it reaches
        # one of the explicit recovery classifications above.
        error_type = (
            # A session file alone only proves that Pi initialized its
            # journal; an exit before the first request is still a startup
            # failure (for example provider initialization) and must keep
            # the existing terminal failure behavior.  Only an interrupted
            # session after a request has actually started is resumable —
            # and that is judged from the journal on disk,
            # never from a live poll that can predate it.
            RecoverablePiProcessError
            if activity["first_request"]
            else subprocess.CalledProcessError
        )
        _fail_run(
            f"pi_exit_{process.returncode}",
            error_type(
                process.returncode, safe_command, output=stdout, stderr=stderr,
            ),
        )
    if stderr:
        LOGGER.info("stderr=%s", stderr.rstrip())
    LOGGER.info("stdout=%s", stdout.rstrip())
    return stdout.strip()
