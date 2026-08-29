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


def process_state(pid: int) -> str | None:
    """The process state char (stat field 3: R/S/D/Z/T/...).

    The evidence for a stalled-but-running tool (Issue #169): a process
    in S/R is alive and working, Z is gone for good. None when the
    process is gone or the line is malformed.
    """
    raw = _read_stat(pid)
    if raw is None:
        return None
    fields = _stat_fields(raw)
    if fields is None or not fields[0]:
        return None
    return fields[0]


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


def process_start_monotonic(pid: int, *, hz: float) -> float | None:
    """The process age offset in MONOTONIC seconds since boot.

    stat field 22 (starttime, ticks since boot) converted with `hz`.
    The value is compared against `time.monotonic()`, never against the
    realtime epoch (Issue #169): a realtime step after boot (NTP)
    must not skew a process's age — `process_start_epoch` (btime plus
    the same ticks) is the realtime-flavoured view used by the
    pre-idle check, this is the clock-consistent one.
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
    return starttime / hz


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


# coreutils `timeout` duration suffixes (GNU coreutils, `timeout(1)`):
# a bare number is seconds; s/m/h/d multiply it.
_DURATION_SUFFIX_SECONDS: dict[str, float] = {
    "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0,
}


def _parse_duration(token: str) -> float | None:
    """A coreutils duration token (`240`, `4m`, `1.5m`, `1h`, `2d`).

    None when the token is not a plain number with an optional known
    suffix — a duration that cannot be parsed is NOT a clear timeout.
    (Callers pass tokens from `cmdline.split()`, which are never
    empty.)
    """
    suffix = ""
    if token[-1] in _DURATION_SUFFIX_SECONDS:
        suffix = token[-1]
        token = token[:-1]
    if not token or not all(ch.isdigit() or ch == "." for ch in token):
        return None
    if token.count(".") > 1:
        return None
    try:
        seconds = float(token)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return seconds * _DURATION_SUFFIX_SECONDS.get(suffix, 1.0)


def timeout_duration(cmdline: str) -> float | None:
    """The duration (seconds) of a coreutils `timeout <duration>`
    wrapper in the command line, or None when the command has no clear
    timeout (Issue #169).

    The wrapper word must stand alone (`timeout`, or an absolute path
    to it) and the duration must be the IMMEDIATE next token — the
    prompt contract form `timeout <seconds> ...`. Anything else
    (options in between, a missing/unparseable duration, the word
    inside a longer token) is not a clear timeout: the existing
    recovery behavior applies (fail-safe, never a fabricated deadline).
    """
    tokens = cmdline.split()
    for index, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] != "timeout":
            continue
        if index + 1 >= len(tokens):
            return None
        return _parse_duration(tokens[index + 1])
    return None


# /proc/net/tcp socket states that still hold a live connection to a
# remote peer: ESTABLISHED (01), SYN_SENT (02), SYN_RECV (03). A
# CLOSE_WAIT (08) socket has already been closed by the peer — the
# request is dead even though the remote address is still printed.
_LIVE_TCP_STATES: frozenset[str] = frozenset({"01", "02", "03"})


def upstream_alive(pid: int) -> bool:
    """True when the process holds an open TCP socket in a live
    connection state (ESTABLISHED / SYN_SENT / SYN_RECV) with a remote
    address — the evidence that a model request to the upstream
    (llama/proxy) is still connected (Issue #169).

    The process's fd table is scanned for `socket:[<inode>]` entries and
    each inode is matched against `/proc/net/tcp` and `/proc/net/tcp6`
    (the real kernel format: the state is field 4, the remote address
    field 3, the inode field 10). A process with no such socket has no
    live upstream connection: a cleanly closed connection (CLOSE_WAIT
    or gone) and a silently dropped one (no socket left) both leave
    nothing in a live state behind.
    """
    fd_dir = PROC / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return False
    inodes: set[str] = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target[len("socket:["):-1])
    if not inodes:
        return False
    for table in ("tcp", "tcp6"):
        try:
            lines = (PROC / "net" / table).read_text().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            fields = line.split()
            # Real format: sl local rem st tx:rx tr:tm retrnsmt uid
            # timeout inode ... — the remote address is index 2, the
            # state index 3, the inode index 9.
            if len(fields) < 10 or fields[9] not in inodes:
                continue
            if fields[3] not in _LIVE_TCP_STATES:
                continue
            remote = fields[2]
            ip, _, _port = remote.rpartition(":")
            if ip and set(ip) != {"0"}:
                return True
    return False


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
        # A zombie (state Z) is already dead — the parent just has not
        # reaped it yet: signaling it is meaningless and its empty
        # cmdline would fall back to the comm name, a lie (Issue #169).
        if process_state(pid) == "Z":
            continue
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
