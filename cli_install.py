"""Auto-refresh of the editable CLI install at Runner start (Issue #158).

The official local deployment is the EDITABLE uv tool install (Issue
#152): the tool env imports the runtime modules directly from the
deployment checkout through a setuptools editable finder. The
finder's module MAPPING is generated at INSTALL time from the
checkout's ``pyproject.toml`` (``py-modules``, entry points, version,
dependencies) — so when the packaging inputs change (a new runtime
module is added to ``py-modules``, a dependency or entry point
changes), the installed finder is STALE: the next CLI process dies
with ``ModuleNotFoundError`` before the Runner can even start (the
#158 incident: ``cli_source`` merged to main, the installed finder
still mapped the pre-#152 module set, the systemd start failed).

This module refreshes the editable install at the Runner start,
BEFORE any slot or claim:

- the packaging fingerprint (sha256 of the checkout's
  ``pyproject.toml`` — the packaging input that decides the editable
  metadata) is compared against the fingerprint of the LAST
  successful install, stored in the shared state dir (``.muyan-pilot/
  cli-install.json`` — the same gitignored dir as ``base-sync.lock``
  and the slots, which survives the ``git merge --ff-only`` checkout
  sync). It is NOT a second release state: it only records which
  packaging input the installed tool env was built from;
- unchanged: NO uv call at all (no per-tick reinstall);
- changed, or no state yet (first install): ONE lock-protected
  ``uv tool install --force --reinstall --editable --python
  /usr/bin/python3 <repo_dir>`` (the exact verified argv from
  ``cli_source.reinstall_args``);
- two instances starting in the same tick: the SAME base-sync flock
  (the lock file the service template's ``ExecStartPre`` also takes)
  serializes them; the second instance re-reads the state UNDER the
  lock and reuses the first's result — no concurrent uv install, no
  corrupted tool env;
- a failing install fails fast with the structured
  ``cli_install_failed`` line (reason + the exact fix command) and
  records NO state (the next start retries).

No database, queue, daemon or second release state: the state file
lives in the existing shared state dir, and the flock is the kernel
primitive (released with the holder's exit).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from pi_activity import quote_value

# NOTE: `cli_source` is imported lazily inside `refresh_cli_install`
# (NOT at module level): cli_source -> muyan_pilot -> bootstrap_runner
# -> cli_install would be a circular import (bootstrap_runner imports
# this module before `muyan_pilot` is fully loaded). The reinstall
# argv is the single cross-module dependency (Issue #152's verified
# command), so the lazy import stays in one place.
LOGGER = logging.getLogger("muyan_pilot.cli_install")

# The uv install timeout (seconds): a local editable build of this
# zero-dependency package takes seconds; a hang (a wedged uv or a
# full disk) must fail the start, never block it forever (Issue #95:
# blocking commands carry a timeout).
UV_INSTALL_TIMEOUT_SECONDS = 300

# The base-sync lock (moved here from bootstrap_runner, Issue #158:
# one home for the concurrency primitive — the checkout sync, the
# ExecStartPre preflight and the CLI install refresh all serialize on
# the SAME lock file).
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
    """
    return Path(repo_dir) / ".muyan-pilot" / BASE_SYNC_LOCK_NAME


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
                LOGGER.error(
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


def packaging_fingerprint(repo_dir: Path) -> str:
    """The sha256 of the checkout's ``pyproject.toml``.

    ``pyproject.toml`` is the packaging input that decides the
    editable metadata (the finder's module mapping from
    ``py-modules``, the entry points, the version, the dependencies) —
    so its content hash is the refresh trigger. Ordinary Python source
    content is NOT part of it: the editable finder maps the live
    files, a content change needs no reinstall (the whole point of
    the editable install, Issue #152). A checkout without
    ``pyproject.toml`` cannot be tool-installed: fail fast, never
    guess a fingerprint.
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

    ``<repo_dir>/.muyan-pilot/cli-install.json`` — the EXISTING shared
    state dir (gitignored, next to ``base-sync.lock`` and the slots;
    it survives the ``git merge --ff-only`` checkout sync). Not a
    second release state and not a per-process temp file.
    """
    return Path(repo_dir) / ".muyan-pilot" / "cli-install.json"


def read_install_state(repo_dir: Path) -> str | None:
    """The stored last-install fingerprint, or None.

    Missing file (first install / fresh checkout) -> None. A
    malformed file (a torn write) is treated as "no state" and heals
    in the SAFE direction: one extra idempotent ``--force
    --reinstall`` runs — never a wedged start.
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
    changed; return ``"unchanged"`` or ``"installed"``.

    The pre-start gate (called by the Runner tick before any slot or
    claim):

    - the current packaging fingerprint equals the stored last-install
      fingerprint -> ``"unchanged"`` and NO uv call (no per-tick
      reinstall);
    - otherwise (changed, or no state yet — the first install): take
      the base-sync flock (the SAME lock the service template's
      ``ExecStartPre`` and the checkout sync use), re-check the state
      UNDER the lock (a concurrent instance may have refreshed while
      we waited — reuse its result, never run a second install), run
      the exact verified editable force reinstall from
      ``cli_source.reinstall_args``, and record the fingerprint only
      after success.

    A failing install logs the structured ``cli_install_failed`` line
    (reason + the exact fix command) and raises ``CliInstallError``:
    the service does not start (fail fast), no state is recorded (the
    next start retries) and the lock is released (success or
    failure).
    """
    repo_dir = Path(repo_dir)
    fingerprint = packaging_fingerprint(repo_dir)
    if read_install_state(repo_dir) == fingerprint:
        LOGGER.info(
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
            LOGGER.info(
                "cli_install_reused repo_dir=%s pyproject_sha256=%s",
                repo_dir, fingerprint,
            )
            return "unchanged"
        reason = "first_install" if (
            read_install_state(repo_dir) is None
        ) else "packaging_changed"
        import cli_source  # lazy: see the module-level note
        try:
            run_command(
                cli_source.reinstall_args(repo_dir),
                timeout=UV_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            LOGGER.error(
                "cli_install_failed repo_dir=%s reason=%s fix=%s",
                repo_dir, quote_value(str(exc)),
                quote_value(cli_source.reinstall_command(repo_dir)),
            )
            raise CliInstallError(
                f"editable CLI install failed for {repo_dir}: {exc} "
                f"(fix: {cli_source.reinstall_command(repo_dir)})"
            ) from exc
        write_install_state(repo_dir, fingerprint)
        LOGGER.info(
            "cli_install_refreshed repo_dir=%s reason=%s "
            "pyproject_sha256=%s",
            repo_dir, reason, fingerprint,
        )
        return "installed"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
