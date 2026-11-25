"""The runner-health state FILE: its path, its read, its write, its lock.

The one state file the health check and the notify watermark share
(`health.json` beside its `.lock`). It is a leaf: `orbi.notify` needs the
serialization and the atomic write without the health CHECKS — and the
checks read `orbi.github`, so a `notify -> runner_health` edge would close
the `github -> notify` cycle the import graph forbids (the extraction that
moved these helpers out of `runner_health`, Issue #2394).

Health.json is the one state file two runner instances both read and write
with no other synchronization — their load..save spans interleave and the
last writer rolls the other's updates back. The lock here is a LEAF lock:
while holding it the code never acquires another one.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path

from orbi.journal import event

STATE_FILENAME = "health.json"
HEALTH_LOCK_TIMEOUT_S = 5.0


def health_state_path(repo_dir: Path) -> Path:
    """Return the health state file of one configured repo."""
    return repo_dir / ".orbi" / STATE_FILENAME


def fresh_state() -> dict:
    return {"runs": [], "last_pickup_ts": None, "alerted": []}


def load_health_state(path: Path) -> dict:
    """Load the health state; a missing or corrupt file is a fresh state.

    The health check is a bypass: corrupt observability state
    must never fail the delivery — it is logged and replaced with fresh
    state on the next save.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # No state file yet: the normal first-tick case, not an error.
        return fresh_state()
    except (OSError, ValueError):
        event("health_state_unreadable", level=logging.WARNING, path=path)
        return fresh_state()
    if not isinstance(data, dict):
        event("health_state_unreadable", level=logging.WARNING, path=path)
        return fresh_state()
    state = fresh_state()
    runs = data.get("runs")
    if isinstance(runs, list):
        state["runs"] = runs
    pickup = data.get("last_pickup_ts")
    if isinstance(pickup, (int, float)) and not isinstance(pickup, bool):
        state["last_pickup_ts"] = pickup
    alerted = data.get("alerted")
    if isinstance(alerted, list) and all(
        isinstance(item, str) for item in alerted
    ):
        state["alerted"] = alerted
    return state


def lock_path(state_path: Path) -> Path:
    return state_path.parent / (state_path.name + ".lock")


def acquire_health_lock(state_path: Path, *, blocking: bool) -> int | None:
    """Take the cross-instance health-state lock; None when unavailable.

    The returned fd owns the flock — closing it releases. `blocking=False`
    bounds the wait at HEALTH_LOCK_TIMEOUT_S and gives up for the tick: the
    check is a documented pure bypass, so a busy lock skips this tick's
    check with the same semantics as a check failure.
    """
    lock = lock_path(state_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    if blocking:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    deadline = time.monotonic() + HEALTH_LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def save_health_state(path: Path, state) -> None:
    """Write the health state atomically (tmp file + rename).

    The tmp name is pid-scoped: a shared `health.tmp` let two instances
    interleave truncate/write into torn JSON that the read side took as
    fresh state (a silent reset of every alert/counter). A unique tmp
    makes each writer's rename all-or-nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)
