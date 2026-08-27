#!/usr/bin/env python3
"""Idle-stall recovery for a running Pi session (Issue #94).

A Pi session can stall while a tool call hangs forever (a `while True`
test in the TDD red phase, a `next(generator)` that never returns, ...):
the Pi process waits for its child, the session JSONL freezes, and the
slot is held forever. The runner's existing poll loop detects the stall
(`stale_seconds >= idle_warn_seconds`, not `model_wait`) and this module
gives it the /proc-based primitives to recover instead of only warning:

- `descendant_pids` — the live process tree of the Pi process, built
  from the ppid chain in `/proc/<pid>/stat` (never a name guess);
- `find_idle_descendants` — the descendants that already existed before
  the idle window started (start time from stat field 22 converted with
  the boot time and CLK_TCK) and are still running: those are the hung
  tools, and ONLY they may be signaled;
- `signal_pid` / `pid_alive` — SIGTERM first (the scene is preserved and
  the tool gets a non-zero exit, so the failure signal reaches the
  model), SIGKILL only for a target that survived.

This module never spawns anything and owns no background thread: it is
called from the existing `stream_pi` poll loop.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

# The real procfs; the unit tests point this at a fake directory.
PROC = Path("/proc")


def _read_stat(pid: int) -> str | None:
    """The raw `/proc/<pid>/stat` line; None when the process is gone."""
    try:
        return (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None


def _stat_fields(raw: str) -> list[str] | None:
    """The fields after the parenthesized comm (index 0 = state).

    comm (field 2) may contain spaces and parentheses, so the split
    point is the LAST `)` of the line.
    """
    end = raw.rfind(")")
    if end < 0:
        return None
    return raw[end + 2:].split()


def _comm(raw: str) -> str:
    """The parenthesized comm name; empty when the line is malformed."""
    start = raw.find("(")
    end = raw.rfind(")")
    if start < 0 or end <= start:
        return ""
    return raw[start + 1:end]


def process_ppid(pid: int) -> int | None:
    """The parent pid (stat field 4); None when the process is gone."""
    raw = _read_stat(pid)
    if raw is None:
        return None
    fields = _stat_fields(raw)
    if fields is None or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def process_start_epoch(pid: int, *, btime: float, hz: float) -> float | None:
    """The process start time in epoch seconds.

    stat field 22 is the start time in clock ticks since boot; the boot
    time comes from `/proc/stat` (`btime`) and the tick rate from
    `sysconf(_SC_CLK_TCK)`. None when the process is gone or the line is
    malformed.
    """
    raw = _read_stat(pid)
    if raw is None:
        return None
    fields = _stat_fields(raw)
    if fields is None or len(fields) < 20:
        return None
    try:
        starttime = int(fields[19])
    except ValueError:
        return None
    return btime + starttime / hz


def boot_time() -> float:
    """The system boot time in epoch seconds (`/proc/stat` `btime`)."""
    for line in (PROC / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise RuntimeError("/proc/stat has no btime line")


def clk_tck() -> float:
    """The system clock tick rate (`sysconf(_SC_CLK_TCK)`)."""
    return float(os.sysconf("SC_CLK_TCK"))


def cmdline_of(pid: int) -> str:
    """The process command line (NUL-separated args joined by spaces).

    A kernel thread or a zombie has an empty cmdline: the comm name is
    the fallback so the journal line still names the process.
    """
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    if raw:
        return raw.decode("utf-8", "replace").replace("\x00", " ").strip()
    stat = _read_stat(pid)
    if stat is None:
        return ""
    return _comm(stat)


def descendant_pids(root_pid: int) -> list[int]:
    """All live descendants of `root_pid`, sorted by pid.

    The ppid chain from `/proc/<pid>/stat` is the only criterion: a
    process is a target candidate only when its parent chain reaches
    `root_pid`. Gone entries (no stat) and malformed lines are skipped.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = list(PROC.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        raw = _read_stat(pid)
        if raw is None:
            continue
        fields = _stat_fields(raw)
        if fields is None or len(fields) < 2:
            continue
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = [root_pid]
    seen = {root_pid}
    while queue:
        current = queue.pop()
        for child in children.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            found.append(child)
            queue.append(child)
    return sorted(found)


def find_idle_descendants(root_pid: int, idle_start_epoch: float,
                          *, btime: float | None = None,
                          hz: float | None = None) -> list[dict]:
    """The descendants of `root_pid` that are still running AND started
    no later than `idle_start_epoch` — the hung tools of the stalled
    scene.

    Each result is `{"pid": int, "cmdline": str}`. A process spawned
    after the idle window began (a new tool call) is never a target, and
    a process that is not a descendant (checked by the ppid chain) is
    never a target. `btime`/`hz` default to the real clock constants.
    """
    if btime is None:
        btime = boot_time()
    if hz is None:
        hz = clk_tck()
    targets: list[dict] = []
    for pid in descendant_pids(root_pid):
        start = process_start_epoch(pid, btime=btime, hz=hz)
        if start is None:
            continue
        if start <= idle_start_epoch:
            targets.append({"pid": pid, "cmdline": cmdline_of(pid)})
    return targets


def pid_alive(pid: int) -> bool:
    """True when a signal-0 reachability check finds the process."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user: alive.
        return True


def signal_pid(pid: int, sig: int) -> str:
    """Send `sig` to `pid`; return the outcome for the journal.

    `sent` (delivered), `already_dead` (ESRCH: it exited between the
    discovery and the signal) or `failed: <error>` (any other OS error —
    logged, never raised: the recovery must not take the delivery down).
    """
    try:
        os.kill(pid, sig)
        return "sent"
    except ProcessLookupError:
        return "already_dead"
    except OSError as exc:
        return f"failed: {exc}"
