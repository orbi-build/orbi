#!/usr/bin/env python3
"""Idle-stall recovery for a running Pi session.

A Pi session can stall while a tool call hangs forever (a `while True`
test in the TDD red phase, a `next(generator)` that never returns, ...):
the Pi process waits for its child, the session JSONL freezes, and the
slot is held forever. The runner's existing poll loop detects the stall
(`stale_seconds >= idle_warn_seconds`, not `model_wait`) and this module
gives it the /proc-based primitives to recover instead of only warning:

- `descendant_pids` — the live process tree of the Pi process, built
  from the ppid chain in `/proc/<pid>/stat` (never a name guess);
- `snapshot_descendants` — the descendants that already exist when the
  idle window opens, recorded as pid -> start ticks (stat field 22): a
  process spawned after the window is a new tool call, never a target;
- `find_idle_descendants` — the recorded descendants that are still the
  SAME process (same start ticks, not a zombie): those are the hung
  tools, and ONLY they may be signaled;
- `signal_pid` / `pid_alive` — SIGTERM first (the scene is preserved and
  the tool gets a non-zero exit, so the failure signal reaches the
  model), SIGKILL only for a target that survived.

Everything here reads ONLY the per-process directories. The start time is
the kernel's own unit (stat field 22, ticks since boot) compared as-is —
no `/proc/stat` (`btime`), no clock conversion — so the recovery works on
a `/proc` mounted with `subset=pid`, where no file outside the process
directories exists (Issue #1808).

It also owns the child-shutdown rule both callers of a live Pi process
share (`shutdown_child`): the session's exit path (`stream_pi`) and the
Runner's SIGTERM stop handler end a child with TERM -> bounded grace ->
KILL -> reap, never with an unbounded wait and never without reaping
(Issue #1995 — one rule, one grace constant). That rule is here because
`pi_process` may not import `runner` (CONSTITUTION 3.3).

This module never spawns anything and owns no background thread: it is
called from the existing `stream_pi` poll loop and its exit path.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import urllib
import urllib.request
from pathlib import Path

# Child-shutdown grace: when a live Pi child must end — a steering
# restart, a timeout/idle/model-wait kill, the Runner's SIGTERM stop —
# the caller must not wait forever for it. systemd gives `TimeoutStopSec`
# (default 90s) before it SIGKILLs the whole cgroup, so an unbounded
# `child.wait()` on a child stuck in a model/network call leaves
# `Result=timeout` after the grace. TERM, wait at most this long, then
# KILL and reap: the caller always gets the child back, before systemd's
# own deadline on the stop path and before the next session's spawn on the
# delivery path.
STOP_CHILD_GRACE_SECONDS = 15.0


def shutdown_child(child: subprocess.Popen | None,
                   grace: float = STOP_CHILD_GRACE_SECONDS) -> None:
    """Terminate and reap a live child without blocking past `grace`.

    A plain ``child.wait()`` with no timeout lets a Pi child stuck in a
    model/network call block the caller — the stop handler, or the
    session's exit path that must reap before the next session starts in
    the same worktree (Issue #1995). This TERMs the child, waits at most
    ``grace`` seconds, then KILLs and reaps it. A child that exits on TERM
    (cooperative) is unaffected; a child that already exited is a no-op.
    """
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


# The real procfs; the unit tests point this at a fake directory.
PROC = Path("/proc")

# The /slots probe timeout: the probe runs inside the poll
# loop and must never block it — a bounded, short request. The model
# endpoint is local (127.0.0.1) in the documented deployment, so 5 s is
# generous for a healthy endpoint and short compared to the dead-request
# threshold it protects.
SLOTS_PROBE_TIMEOUT = 5.0


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

    The evidence for a stalled-but-running tool: a process
    in S/R is alive and working, Z is gone for good. None when the
    process is gone or the line is malformed.
    """
    raw = _read_stat(pid)
    if raw is None:
        return None
    fields = _stat_fields(raw)
    if fields is None or not fields:
        return None
    return fields[0]


def process_start_ticks(pid: int) -> int | None:
    """The process start time in the kernel's own unit: stat field 22,
    clock ticks since boot.

    The value is compared AS-IS (start ticks are unique per pid
    lifetime), so the recovery never needs the boot time or a clock
    conversion: a `/proc` mounted with `subset=pid` carries no
    `/proc/stat`, and reading it would make every start time unknown
    (Issue #1808). None when the process is gone or the line is
    malformed.
    """
    raw = _read_stat(pid)
    if raw is None:
        return None
    fields = _stat_fields(raw)
    if fields is None or len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


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
    timeout.

    The wrapper must BE the command: the prompt contract form
    `timeout <seconds> ...`, or the `bash -c` payload form the Pi bash
    tool actually spawns (`bash -c timeout <seconds> ...`). Anywhere
    else the `timeout <number>` pair is data, not a deadline — a commit
    message (`git commit -m "fix timeout 300 regression"` joins to
    `... -m fix timeout 300 regression` on the real cmdline), an echo
    argument, a pytest -k expression — and reading a deadline off it
    would make the runner WAIT for a wrapper that does not exist.
    Anything not matching the contract (options in between, a
    missing/unparseable duration, a wrapper buried deeper in a compound
    command) is not a clear timeout: the existing recovery behavior
    applies (fail-safe, never a fabricated deadline).
    """
    tokens = cmdline.split()
    wrapper_index: int | None = None
    if tokens and tokens[0].rsplit("/", 1)[-1] == "timeout":
        wrapper_index = 0
    elif (
        len(tokens) >= 3
        and tokens[0].rsplit("/", 1)[-1] == "bash"
        and tokens[1] == "-c"
        and tokens[2].rsplit("/", 1)[-1] == "timeout"
    ):
        wrapper_index = 2
    if wrapper_index is None or wrapper_index + 1 >= len(tokens):
        return None
    return _parse_duration(tokens[wrapper_index + 1])


# /proc/net/tcp socket states that still hold a live connection to a
# remote peer: ESTABLISHED (01), SYN_SENT (02), SYN_RECV (03). A
# CLOSE_WAIT (08) socket has already been closed by the peer — the
# request is dead even though the remote address is still printed.
_LIVE_TCP_STATES: frozenset[str] = frozenset({"01", "02", "03"})


def upstream_alive(pid: int) -> bool:
    """True when the process holds an open TCP socket in a live
    connection state (ESTABLISHED / SYN_SENT / SYN_RECV) with a remote
    address — the evidence that a model request to the upstream
    (llama/proxy) is still connected.

    This is EVIDENCE ONLY: a live connection proves
    the request is connected, not that the model is answering (the
    #183 scene: llama-server alive, the request hung, the slot held
    for hours). The `model_wait_dead` kill of a frozen model_wait past
    the threshold never vetoes on this value — it records it on the
    structured `model_wait_dead` line (process alive ≠ responding).

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


def snapshot_descendants(root_pid: int) -> dict[int, int]:
    """The live descendants of `root_pid` right now -> start ticks.

    Called when the idle window opens: a descendant that already exists
    at that moment is a recovery candidate, and the recorded start ticks
    are its identity. A process spawned later (a new tool call) is never
    recorded, so it can never be a target.

    No /proc (macOS) yields an empty snapshot — the caller must keep its
    poll loop alive, never see an exception from the recovery probe
    itself.
    """
    snapshot: dict[int, int] = {}
    for pid in descendant_pids(root_pid):
        ticks = process_start_ticks(pid)
        if ticks is not None:
            snapshot[pid] = ticks
    return snapshot


def find_idle_descendants(recorded: dict[int, int]) -> list[dict]:
    """The recorded descendants that are still the SAME process — the
    hung tools of the stalled scene.

    `recorded` is the window-open snapshot (pid -> start ticks). A
    target is a recorded pid that is still alive with the SAME start
    ticks: a pid the kernel reused in the meantime carries different
    ticks and belongs to an innocent process, and a pid whose directory
    is gone has exited. Each result is
    `{"pid": int, "cmdline": str, "start_ticks": int}`; the ticks ride
    along as the identity the signal path re-checks before delivering.
    """
    targets: list[dict] = []
    for pid, ticks in recorded.items():
        # A zombie (state Z) is already dead — the parent just has not
        # reaped it yet: signaling it is meaningless and its empty
        # cmdline would fall back to the comm name, a lie.
        if process_state(pid) == "Z":
            continue
        if process_start_ticks(pid) != ticks:
            continue
        targets.append({
            "pid": pid,
            "cmdline": cmdline_of(pid),
            "start_ticks": ticks,
        })
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


def slots_idle(url: str, timeout: float = SLOTS_PROBE_TIMEOUT) -> bool | None:
    """Probe the model's `/slots` endpoint.

    The fast path for the #231 swallow scene: the model service process is
    alive and the connection is ESTABLISHED, but the request was accepted
    and never scheduled into the slot — the slot reports idle while Pi waits
    forever. The probe asks the model endpoint directly whether any slot is
    actually generating, which `upstream_alive` (a socket-state check) cannot
    see (process alive ≠ responding).

    The endpoint contract is verified against the real llama-server: `GET
    /slots` returns a JSON **list** of slot objects, each carrying an
    `is_processing` bool (the local-llm-kv-cache proxy passes it through
    unchanged). The result:

    - `True`  — the endpoint is reachable and EVERY slot has
      `is_processing=false`: the model is NOT generating (the swallow
      evidence).
    - `False` — at least one slot has `is_processing=true`: the model is
      generating (a slow model, NOT a swallow).
    - `None`  — inconclusive: a probe error (network / non-200 / timeout),
      invalid JSON, or a payload that is not a non-empty list of slot
      objects — INCLUDING slots whose `is_processing` is missing or not
      a bool (schema drift): a vacuous "every slot is false" over slots
      that carry no flag would fabricate the swallow evidence and send
      the #231 recovery after a model that is in fact generating. The
      caller treats `None` as "no evidence" — the probe is a pure
      bypass and never fails the delivery.
    """
    try:
        # urlopen raises HTTPError for a non-2xx status (caught below),
        # so a reachable endpoint here returned a 2xx body.
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception:
        # Any probe failure (URLError / HTTPError / timeout / JSON / I/O)
        # is inconclusive — the probe is a bypass, never an error.
        return None
    if not isinstance(payload, list) or not payload:
        return None
    for slot in payload:
        if not isinstance(slot, dict):
            return None
        flag = slot.get("is_processing")
        if flag is True:
            return False
        if flag is not False:
            return None
    return True


def signal_pid(pid: int, sig: int, *,
               expected_start_ticks: int | None = None) -> str:
    """Send `sig` to `pid`; return the outcome for the journal.

    With `expected_start_ticks` (the target's start ticks from
    `find_idle_descendants`) the identity is re-checked on the stat
    line right before the delivery: the discovery-to-signal gap can
    span a full idle window, and a pid the kernel reused in the gap
    belongs to an innocent process that must never be signaled
    (TOCTOU).

    `sent` (delivered), `already_dead` (ESRCH: it exited between the
    discovery and the signal — or the start-tick recheck finds the
    stat line gone), `pid_reused` (the recheck finds different start
    ticks on the pid: not the discovered process, nothing signaled) or
    `failed: <error>` (any other OS error — logged, never raised: the
    recovery must not take the delivery down).
    """
    if expected_start_ticks is not None:
        current = process_start_ticks(pid)
        if current is None:
            return "already_dead"
        if current != expected_start_ticks:
            return "pid_reused"
    try:
        os.kill(pid, sig)
        return "sent"
    except ProcessLookupError:
        return "already_dead"
    except OSError as exc:
        return f"failed: {exc}"
