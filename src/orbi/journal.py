"""Runner journal kernel: logging, run binding, and the subprocess seam.

Every journal line of a task attempt starts with `[run_id]` (Issue #41);
this module owns the logger that enforces it, the run-id binding the
filter reads, and the in-flight delivery scene the stop handler reports.
It also owns the ONE subprocess seam of Article 3.4: ``run_command`` and
its bounded git network retry.

This module imports nothing from the `orbi` package — it is the lowest
leaf, so `github` / `gitops` / `progress` / the extracted modules can all
share one seam and one logger without importing `runner` (Issue #785).
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger("orbi.bootstrap")

# Run correlation (Issue #41): one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
_CURRENT_RUN_ID: str | None = None


def validate_run_id(run_id: object) -> str:
    """Fail fast unless ``run_id`` identifies exactly one task attempt."""
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


class RunIdFilter(logging.Filter):
    """Prefix every log message with the current `[run_id]`, if bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT_RUN_ID is not None:
            record.msg = f"[{_CURRENT_RUN_ID}] {record.msg}"
        return True


LOGGER.addFilter(RunIdFilter())


def set_run_id(run_id: str) -> None:
    """Bind one task attempt: every later journal line carries `[run_id]`."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = validate_run_id(run_id)


def current_run_id() -> str | None:
    """Return the run id bound to this tick, or None before the claim."""
    return _CURRENT_RUN_ID


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


def quote_value(value: str) -> str:
    """Double-quote a key=value field value when it needs quoting.

    Values containing spaces or double quotes are quoted; embedded double
    quotes are escaped as ``\\"`` so the field stays parseable as a single
    ``key=value`` token.
    """
    if " " in value or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int | None = None,
                log_command: list[str] | None = None,
                log_stdout: bool = False,
                failure_log_level: int = logging.ERROR) -> str:
    """Run one external command; log context and fail fast on any error.

    ``failure_log_level`` is INFO for probes whose failure is an expected
    status result, such as an optional component health check. The command
    still raises, so callers retain control over whether the failure blocks.
    """
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
        LOGGER.log(
            failure_log_level,
            "command_failed returncode=%s stdout=%s stderr=%s",
            exc.returncode, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        LOGGER.log(
            failure_log_level,
            "command_timeout timeout=%s stdout=%s stderr=%s",
            timeout, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except OSError as exc:
        LOGGER.log(failure_log_level, "command_spawn_failed error=%s", exc)
        raise
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    if log_stdout and result.stdout:
        LOGGER.info("stdout=%s", result.stdout.rstrip())
    return result.stdout.strip()


GIT_NETWORK_MAX_ATTEMPTS = 3
GIT_NETWORK_TIMEOUT_SECONDS = 30
GIT_NETWORK_BACKOFF_SECONDS = 1
GIT_TRANSIENT_ERROR_MARKERS = (
    "connection timed out",
    "operation timed out",
    "connection reset",
    "connection refused",
    "temporary failure in name resolution",
    "network is unreachable",
    "network unreachable",
)


def _is_retryable_git_network_failure(
    command: list[str], exc: subprocess.CalledProcessError,
) -> bool:
    """Return whether a Git fetch/push failed with a known transient error."""
    if len(command) < 2 or command[:1] != ["git"]:
        return False
    if command[1] not in {"fetch", "push"}:
        return False
    stderr = (exc.stderr or "").lower()
    return any(marker in stderr for marker in GIT_TRANSIENT_ERROR_MARKERS)


def _is_git_network_command(command: list[str]) -> bool:
    return (
        len(command) >= 2
        and command[:1] == ["git"]
        and command[1] in {"fetch", "push"}
    )


def run_git_network_command(
    command: list[str], *, cwd: Path | str | None = None,
    command_runner: Callable[..., str] | None = None,
) -> str:
    """Run an Orbi-controlled Git fetch/push with bounded network retries.

    Only explicitly recognized transient network messages are retried. The
    last ``CalledProcessError`` is re-raised unchanged so its stderr remains
    available to the existing failure handling.
    """
    execute = command_runner or run_command
    attempt = 0
    while True:
        attempt += 1
        try:
            return execute(
                command, cwd=cwd, timeout=GIT_NETWORK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            retryable = _is_git_network_command(command)
            detail = f"command timed out after {GIT_NETWORK_TIMEOUT_SECONDS}s"
            if attempt >= GIT_NETWORK_MAX_ATTEMPTS or not retryable:
                raise
        except subprocess.CalledProcessError as exc:
            retryable = _is_retryable_git_network_failure(command, exc)
            detail = (exc.stderr or "").strip()
            if attempt >= GIT_NETWORK_MAX_ATTEMPTS or not retryable:
                raise
        delay = GIT_NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1))
        LOGGER.warning(
            "git_network_retry command=%s attempt=%s max_attempts=%s "
            "delay_seconds=%s stderr=%s",
            single_line(" ".join(command)), attempt + 1,
            GIT_NETWORK_MAX_ATTEMPTS, delay, single_line(detail),
        )
        time.sleep(delay)


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


def active_run() -> dict | None:
    """The in-flight delivery scene, or None (the stop handler reads it)."""
    return _ACTIVE_RUN
