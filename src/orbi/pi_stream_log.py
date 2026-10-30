#!/usr/bin/env python3
"""Pi stream logging and exit classification (extracted from
`orbi.pi_process`, Issue #1860).

The lines the Pi watcher writes while a session runs (`activity`,
`heartbeat`, the startup phases, `provider_config_loaded`,
`startup_failed`, `run_failed`) and the classification behind them: the
early-exit reason, the `startup_failed` reason and the provider 429 marker
set they share, plus the child's output streams themselves (`_drain_stream`
/`_decode_chunks`: the bounded drain that turns those pipes into the text
the classification reads). `orbi.pi_process` drives them; nothing here
imports it.
"""
from __future__ import annotations

import logging
import os
import select
from pathlib import Path
from typing import TYPE_CHECKING

from orbi.failure import PROVIDER_AUTH_WORDS
from orbi.journal import event
from orbi.pi_activity import format_duration, format_run_scene

if TYPE_CHECKING:
    # Annotation-only: the provider identifiers of the loaded config.
    from orbi.config import RunnerConfig


# Provider rate-limit markers (the free-tier burst scene): a Pi exit whose
# stderr carries one of these is TRANSIENT throttling, not a task failure —
# the classifier below reports `provider_rate_limited` and the retry loop in
# `orbi.pi_process` backs off (`PI_RATE_LIMIT_RETRIES`).
PI_RATE_LIMIT_MARKERS = ("429", "quota", "resource_exhausted", "retry in")


def _log_activity(activity: dict, *, issue_ref: str,
                  role: str, state: str | None = None) -> None:
    """Log one short activity line with the changed fields only.

    No `run=` field: the `[run_id]` prefix added by
    `RunIdFilter` is the single run-id carrier on the
    high-frequency lines, so the id appears exactly once per line.
    """
    event(
        "activity", issue=issue_ref, role=role, phase=activity["phase"],
        action=activity["action"] or "-",
        result=activity["result"] or "-",
        state=state or "-",
        idle=format_duration(activity["stale_seconds"]),
    )


def _log_heartbeat(activity: dict, *, issue_ref: str,
                   role: str, elapsed: float,
                   state: str | None = None) -> None:
    """Log one heartbeat line when nothing changed since the last poll.

    `state` carries the model_wait flag while the model is expected to
    reply next, so a slow active model is not reported as idle (Issue
    #40). No `run=` field: the `[run_id]` prefix is the
    single run-id carrier on the high-frequency lines.
    """
    event(
        "heartbeat", issue=issue_ref, role=role, phase=activity["phase"],
        state=state or "-", elapsed=format_duration(elapsed),
        idle=format_duration(activity["stale_seconds"]),
    )


def _log_startup(kind: str, *, issue_ref: str, role: str, activity: dict,
                 elapsed: float, extra: str = "") -> None:
    """Log one startup phase line.

    Every startup phase (`process_spawned`, `session_created`,
    `first_request_started`, `first_response_received`,
    `startup_failed`) is one stable `key=value` line carrying the issue,
    role, the provider/model Pi selected (`-` until the session's
    `model_change` record says otherwise), the elapsed time since the
    Pi process was spawned, and any extra fields of the phase (`pid=`,
    `reason=`, `session_created=`, `first_request=`). No `run=` field:
    the `[run_id]` prefix is the single run-id carrier.
    Identifiers only — never a key, the prompt or model output.
    """
    event(
        kind, extra or None, issue=issue_ref, role=role,
        provider=activity.get("provider") or "-",
        model=activity.get("model") or "-",
        elapsed=format_duration(elapsed),
    )


def _classify_startup_exit(stderr: str, returncode: int) -> str:
    """The distinguishable `startup_failed` reason for an early Pi exit.

    The classification is evidence-based on Pi's own stderr (the
    minimal correlation the Issue asks for): a provider rate limit
    (`429` / `quota` / `RESOURCE_EXHAUSTED` / `retry in`)
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
    if ("401" in lowered or "403" in lowered
            or any(word in lowered for word in PROVIDER_AUTH_WORDS)):
        return "auth_failure"
    if ("timed out" in lowered or "timeout" in lowered
            or "etimedout" in lowered or "econnrefused" in lowered
            or "econnreset" in lowered or "enotfound" in lowered):
        return "network_timeout"
    return f"pi_exit_{returncode}"


def _is_rate_limited(stderr: str) -> bool:
    """Whether Pi's stderr carries a provider 429 marker."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in PI_RATE_LIMIT_MARKERS)


def log_provider_config_loaded(*, issue_ref: str, role: str, config: RunnerConfig,
                               elapsed: float) -> None:
    """Log the `provider_config_loaded` startup line.

    The provider file has been loaded and validated (at config load)
    and materialized for this run — or resolved to Pi's own agent dir
    when unconfigured. The provider/model fields are the configured
    identifiers (the same non-sensitive values already on the redacted
    command line) or `-` when Pi keeps its own defaults.
    """
    if role == "review":
        provider = config.review_pi_provider or config.pi_provider
        model = config.review_pi_model or config.pi_model
    else:
        provider = config.pi_provider
        model = config.pi_model
    _log_startup(
        "provider_config_loaded", issue_ref=issue_ref, role=role,
        activity={"provider": provider, "model": model},
        elapsed=elapsed,
    )


def _log_startup_failed(*, issue_ref: str, role: str, activity: dict,
                        elapsed: float, returncode: int, stderr: str,
                        timed_out: bool, model_wait_dead: bool,
                        model_wait_swallowed: bool,
                        idle_recovery_failed: bool) -> None:
    """Log one `startup_failed` line: the run failed
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
    response: the kill-path class first, then the
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


def _drain_stream(stream, chunks: list[bytes],
                  silence_s: float = 30.0) -> bool:
    """Read a pipe to EOF, appending chunks (process must be finished).

    Pi's tool subprocesses inherit this pipe's write end and
    can outlive Pi — after Pi is reaped, EOF then never arrives and an
    unbounded blocking read would wedge the tick (and its slot) forever,
    past every timeout mechanism. Each received chunk restarts the
    `silence_s` window (silence, not total, time: a busy legitimate
    writer must never be cut off mid-verdict); when the stream stays
    silent for the whole window the tail is abandoned. Returns False
    when the drain was abandoned, True on EOF.
    """
    fd = stream.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], silence_s)
        if not ready:
            return False
        data = os.read(fd, 65536)
        if not data:
            return True
        chunks.append(data)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")


def _log_run_failed(activity: dict, *, run_id: str, issue_ref: str,
                    role: str, branch: str, cwd: Path, reason: str) -> None:
    """The single `run_failed` log skeleton: the
    six-argument `format_run_scene` plus the reason. The `_fail_run`
    closure of `_stream_pi_once` and the exhausted-retries terminal
    failure (`_fail_rate_limited`) share it, so a new log
    field is still added in exactly one place."""
    event(
        "run_failed",
        format_run_scene(
            activity, run_id=run_id, issue=issue_ref,
            role=role, branch=branch, worktree=str(cwd),
        ),
        level=logging.ERROR, reason=reason,
    )
