#!/usr/bin/env python3
"""Pi process execution subsystem (moved from `runner.py`, Issue #300).

Spawns a Pi session and streams its live activity into the journal, owns the
Pi-process failure classes and the poll/idle/model-wait/recovery tuning
constants. It imports `orbi.pi_activity` and `orbi.pi_recovery` only; the
runner-side lifecycle hooks it needs (`issue_context`, `set_active_pi`) are
imported lazily inside `stream_pi` so this module never imports `runner`
(the runner imports this module — Issue #266 circular-import rule).
"""
from __future__ import annotations

import logging
import os
import re
import select
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from orbi.pi_activity import (
    SessionWatcher,
    format_duration,
    format_run_scene,
    session_state,
)
from orbi.progress import quote_value
from orbi.pi_recovery import (
    clk_tck,
    find_idle_descendants,
    pid_alive,
    process_ppid,
    process_start_monotonic,
    signal_pid,
    slots_idle,
    timeout_duration,
    upstream_alive,
)

LOGGER = logging.getLogger("orbi.pi_process")

# Live activity polling while Pi runs (Issue #24): every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0


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


# Provider rate-limit retry (Issue #321): a Pi session killed by a
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
# consecutive 429 exits at the default 5) does the EXISTING failure
# path run — marked `reason=provider_rate_limited` so a human (or the
# #313 rotation semantics) can take over. Long-term quota exhaustion is
# OUT of scope here (#313): this loop only bridges short burst windows.
PI_RATE_LIMIT_MARKERS = ("429", "quota", "resource_exhausted", "retry in")
PI_RATE_LIMIT_RETRIES = 5
PI_RATE_LIMIT_BACKOFF_SECONDS = 30.0
PI_RATE_LIMIT_BACKOFF_MAX_SECONDS = 300.0


# The bootstrap runner streams every Pi session of a run through the same
# live activity pipeline (Issue #24/#40); implement/review share the same
# line format and carry their role (Issue #41: one run_id end to end, the
# roles are steps of the same run). Issue #82 removed the cold-start fixer
# role: the review session fixes findings in the same session, so a run
# has at most two Pi sessions (implement, then review).
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


class ProviderRateLimitedError(RuntimeError):
    """The Pi session exited because the provider throttled it (a 429
    marker on the session's stderr, Issue #321): TRANSIENT rate limiting,
    not a task failure.

    Internal to `stream_pi`'s retry loop: the loop either re-spawns the
    session after the backoff or replays the EXISTING terminal
    classification through `_fail_rate_limited` — it never escapes
    `stream_pi`. The exception carries the exit scene (returncode,
    captured streams, the refreshed session activity) so the terminal
    replay raises exactly the error type a non-retried exit would. The
    stderr text itself never reaches the message (only the class — the
    same no-leak rule as the startup reasons)."""

    def __init__(self, *, returncode: int, stdout: str, stderr: str,
                 activity: dict) -> None:
        super().__init__(
            f"provider rate limited the Pi session (exit {returncode})"
        )
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.activity = activity


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
    minimal correlation the Issue asks for): a provider rate limit
    (`429` / `quota` / `RESOURCE_EXHAUSTED` / `retry in` — Issue #321)
    is `provider_rate_limited` (checked FIRST: a throttling response
    that happens to mention the key is still a rate limit); a provider
    authentication failure (`401`/`403`, `unauthorized`, `forbidden`,
    `api key`) is `auth_failure`; a network timeout (`timed out`,
    `timeout`, `etimedout`, `econnrefused`, `econnreset`, `enotfound`)
    is `network_timeout`; anything else is the raw exit code
    (`pi_exit_<N>`). The stderr text itself is NOT echoed into the
    reason — only the class — so no sensitive response content can
    leak into the journal.
    """
    if _is_rate_limited(stderr):
        return "provider_rate_limited"
    lowered = stderr.lower()
    if ("401" in lowered or "403" in lowered or "unauthorized" in lowered
            or "forbidden" in lowered or "api key" in lowered):
        return "auth_failure"
    if ("timed out" in lowered or "timeout" in lowered
            or "etimedout" in lowered or "econnrefused" in lowered
            or "econnreset" in lowered or "enotfound" in lowered):
        return "network_timeout"
    return f"pi_exit_{returncode}"


# The 429 response's own retry hint, in the two shapes observed in the
# wild (Issue #321, the #302 scene: the Google 429 text "Please retry in
# 36.123456s" — measured 2s -> 19s -> 36s — and the google.rpc.RetryInfo
# JSON field `"retryDelay": "36s"`).
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"retrydelay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s", re.IGNORECASE),
)


def _is_rate_limited(stderr: str) -> bool:
    """Whether Pi's stderr carries a provider 429 marker (Issue #321)."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in PI_RATE_LIMIT_MARKERS)


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
    root-cause evidence from Pi's stderr (`provider_rate_limited` /
    `auth_failure` / `network_timeout` — the missing session file is
    usually the CONSEQUENCE of the rate-limit/auth/network failure,
    never the cause), then WHERE the startup was stuck
    (`session_not_created` / `no_first_request` / the raw early exit).
    `first_response_timeout` is the frozen `model_wait` killed before
    any response (the hung first request)."""
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


def _refresh_session_evidence(activity: dict, session_dir: Path,
                              known_files: set[Path]) -> dict:
    """Refresh the journal-derived fields of `activity` from disk (#656).

    The live watcher polls on `PI_POLL_INTERVAL`, so its LAST poll can
    run while Pi is still alive: Pi flushes its session journal while
    dying, and the exit decision then used a state that never saw the
    request (the #655 usage-limit scene — the exit was classified as a
    startup failure and the terminal `ai-blocked` burned the in-flight
    delivery). Once the process is dead the journal on disk is
    authoritative: re-read it with the SAME `known_files` baseline (a
    resumed run's previous sessions are never counted) and overwrite
    the journal-derived fields — the session identity, the startup
    milestones (`first_request` / `first_response`), the selected
    provider/model and the scene fields (`phase`, `last_activity`,
    `action`, `result`) the exit lines render. The scene must describe
    the SAME journal the decision used; the LIVE-only fields
    (`stale_seconds`, `model_wait`, `recovery`) keep their last-poll
    value, because they carry the kill decisions already taken.
    """
    final = session_state(session_dir, known_files)
    if final is None:
        return activity
    for key in (
        "session_id", "session_file", "first_request", "first_response",
        "provider", "model", "phase", "last_activity", "action", "result",
    ):
        if final.get(key):
            activity[key] = final[key]
    return activity


def _pending_timeout_targets(targets: list[dict]) -> list[tuple[dict, float]]:
    """The pre-idle descendants still INSIDE an explicit `timeout`
    deadline (Issue #169): `[(target, deadline_epoch), ...]`.

    A descendant whose command line carries a coreutils
    `timeout <seconds>` wrapper and whose deadline is still in the
    future is a legitimately running tool — the runner waits for the
    deadline instead of signaling it. The age is measured CLOCK
    CONSISTENTLY (Issue #169): the process's boot-time start offset
    (stat field 22) against CLOCK_BOOTTIME, never the
    realtime-flavoured `process_start_epoch` — a realtime step after
    boot (NTP) must not make a tool look older than it is. A
    descendant without a clear timeout, whose start time is unreadable,
    or whose deadline already passed is not pending: the existing
    escalation applies to it.
    """
    if not targets:
        return []
    hz = clk_tck()
    boot_clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    now_mono = time.clock_gettime(boot_clock)
    now = time.time()
    starts = {
        target["pid"]: process_start_monotonic(target["pid"], hz=hz)
        for target in targets
    }
    durations = {
        target["pid"]: timeout_duration(target["cmdline"] or "")
        for target in targets
    }
    # coreutils timeout forks the actual tool. The child has no `timeout`
    # token in its command line, but it is still governed by the wrapper's
    # deadline and must not be mistaken for a hung tool.
    wrapper_deadlines = {
        pid: (start, durations[pid])
        for pid, start in starts.items()
        if start is not None and durations[pid] is not None
    }
    pending: list[tuple[dict, float]] = []
    for target in targets:
        pid = target["pid"]
        start_mono = starts[pid]
        duration = durations[pid]
        if duration is None:
            parent = process_ppid(pid)
            if parent not in wrapper_deadlines:
                continue
            # The wrapper itself is the single wait record. Keeping the
            # delegated child out of the result avoids duplicate evidence;
            # the non-empty wrapper result also protects the child from
            # escalation while its wrapper is inside the deadline.
            continue
        if start_mono is None:
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
    `network_timeout`, `provider_rate_limited`, `pi_exit_<N>`,
    `timeout`, `first_response_timeout`, `model_wait_swallowed`,
    `idle_recovery_stale`); the existing `run_failed` line and the
    raised failure are unchanged (fail-fast semantics preserved).

    Provider rate-limit retry (Issue #321): when Pi exits non-zero and
    its stderr carries a provider 429 marker (`429` / `quota` /
    `RESOURCE_EXHAUSTED` / `retry in`), the exit is classified as
    TRANSIENT throttling — not a task failure — and the SAME Pi session
    is re-spawned IN THIS RUN after a backoff, the slot held for the
    whole wait (the #233 positioning: no new Issue is claimed while
    this run waits). The backoff honors the response's own retry hint
    (`retry in Ns` / `retryDelay: Ns`) capped at 5 minutes; without a
    hint it doubles from 30 s to the same cap. Every retry logs one
    `pi_retry_429` line (run id, attempt, next_retry_in). Only after
    `PI_RATE_LIMIT_RETRIES` (default 5) backoff retries still end in a
    429 exit does the EXISTING failure path run — the same error type a
    non-retried exit raises, so the terminal semantics (pre-session
    `ai-blocked` / recoverable interrupted session) are unchanged —
    with the `run_failed` scene marked `reason=provider_rate_limited`.
    Long-term quota exhaustion stays out of scope (#313); non-429
    failures are untouched.

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
    # Issue #300: the issue-ref formatting lives in `runner`; import it
    # lazily so this module never imports `runner` at module load (the
    # runner imports this module — Issue #266 circular-import rule).
    from orbi.runner import issue_context

    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    issue_ref = issue_context(source_repo, issue)
    session_dir = cwd / ".pi-session"
    attempt = 0
    while True:
        # Session files that already exist before this Pi process starts
        # are never followed (Issue #45 round-5 review, Major 3): a
        # resumed run — and, since Issue #321, a retried invocation —
        # re-snapshots the baseline so the previous invocation's JSONL is
        # never reported as this attempt's session.
        known_files = (
            {path for path in session_dir.glob("*.jsonl") if path.is_file()}
            if session_dir.is_dir() else set()
        )
        try:
            return _stream_pi_once(
                command, cwd=cwd, timeout=timeout,
                poll_interval=poll_interval,
                idle_warn_seconds=idle_warn_seconds,
                model_wait_dead_seconds=model_wait_dead_seconds,
                model_wait_probe_url=model_wait_probe_url,
                model_wait_probe_seconds=model_wait_probe_seconds,
                run_id=run_id, issue_ref=issue_ref, branch=branch,
                role=role, safe_command=safe_command, progress=progress,
                pi_env=pi_env, session_dir=session_dir,
                known_files=known_files,
            )
        except ProviderRateLimitedError as exc:
            if attempt >= PI_RATE_LIMIT_RETRIES:
                _fail_rate_limited(
                    exc, run_id=run_id, issue_ref=issue_ref, role=role,
                    branch=branch, cwd=cwd, safe_command=safe_command,
                )
            delay = _backoff_seconds(attempt, exc.stderr)
            LOGGER.warning(
                "pi_retry_429 run_id=%s issue=%s role=%s attempt=%d "
                "next_retry_in=%ds limit=%d session=%s",
                run_id, issue_ref, role, attempt + 1, int(delay),
                PI_RATE_LIMIT_RETRIES,
                exc.activity.get("session_id") or "-",
            )
            time.sleep(delay)
            attempt += 1


def _log_run_failed(activity: dict, *, run_id: str, issue_ref: str,
                    role: str, branch: str, cwd: Path, reason: str) -> None:
    """The single `run_failed` log skeleton (Issue #292): the
    six-argument `format_run_scene` plus the reason. The `_fail_run`
    closure of `_stream_pi_once` and the exhausted-retries terminal
    failure (`_fail_rate_limited`, Issue #321) share it, so a new log
    field is still added in exactly one place."""
    LOGGER.error(
        "run_failed %s reason=%s",
        format_run_scene(
            activity, run_id=run_id, issue=issue_ref,
            role=role, branch=branch, worktree=str(cwd),
        ),
        reason,
    )


def _fail_rate_limited(
    exc: ProviderRateLimitedError, *, run_id: str, issue_ref: str,
    role: str, branch: str, cwd: Path, safe_command: list[str],
) -> NoReturn:
    """The exhausted-retries terminal failure (Issue #321): the EXISTING
    failure path — the same error type a non-retried exit raises, so the
    terminal semantics (pre-session `ai-blocked` / recoverable
    interrupted session) are unchanged — with the `run_failed` scene
    marked `reason=provider_rate_limited`. The per-attempt
    `startup_failed` lines are already in the journal (one per 429 exit
    before the first response), so this is the single terminal log."""
    activity = exc.activity
    _log_run_failed(
        activity, run_id=run_id, issue_ref=issue_ref, role=role,
        branch=branch, cwd=cwd, reason="provider_rate_limited",
    )
    error_type = (
        RecoverablePiProcessError
        if activity["first_request"]
        else subprocess.CalledProcessError
    )
    raise error_type(
        exc.returncode, safe_command, output=exc.stdout, stderr=exc.stderr,
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
    run_id: str,
    issue_ref: str,
    branch: str,
    role: str,
    safe_command: list[str],
    progress: Callable[[dict], None] | None,
    pi_env: dict[str, str] | None,
    session_dir: Path,
    known_files: set[Path],
) -> str:
    """Spawn and stream ONE Pi session attempt (Issue #321): the whole
    pre-#321 `stream_pi` body — the `run_start` scene, the live
    activity/heartbeat lines, the idle/model-wait kill paths and the
    evidence-based exit classification. A 429-classified non-zero exit
    raises `ProviderRateLimitedError` so the `stream_pi` retry loop can
    back off and re-spawn; every other failure raises exactly as
    before. The full streamer contract lives on `stream_pi`."""
    # Issue #300: the stop-handler state `_ACTIVE_RUN` lives in `runner`;
    # import it lazily so this module never imports `runner` at module
    # load (the runner imports this module — Issue #266 rule).
    from orbi.runner import set_active_pi

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
                                    expected_start_epoch=target["start_epoch"],
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
                        result = signal_pid(
                            target["pid"], signal.SIGKILL,
                            expected_start_epoch=target["start_epoch"],
                        )
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
    # Issue #656: the last live poll can predate the journal Pi flushed
    # while dying — refresh the journal evidence before the startup
    # line and the exit classification read it.
    activity = _refresh_session_evidence(
        activity, session_dir, known_files,
    )
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
    # The single `run_failed` site (Issue #292): every terminal branch
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
        # Issue #227: the classified hung-model-request failure —
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
        # Issue #321: a provider 429 exit is TRANSIENT throttling, not a
        # task failure — the `stream_pi` retry loop backs off and
        # re-spawns this session. The refreshed activity rides on the
        # exception so the exhausted-retries terminal failure replays
        # the EXISTING classification below unchanged.
        if _is_rate_limited(stderr):
            raise ProviderRateLimitedError(
                returncode=process.returncode,
                stdout=stdout, stderr=stderr, activity=activity,
            )
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
            # and that is judged from the journal on disk (Issue #656),
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


