#!/usr/bin/env python3
"""Pi idle-stall recovery (extracted from `orbi.pi_process`, Issue #1860).

`IdleRecoveryTracker` is the escalation strategy the Pi stream loop drives
one poll at a time: the idle window, the descendant TERM/KILL steps, the
`timeout`-deadline wait and the exhaustion decision — same journal lines,
TERM/KILL order and exhaustion reason as before the split.

Imports `orbi.pi_recovery` and `orbi.journal` only — never `orbi.pi_process`.
"""
from __future__ import annotations

import logging
import signal
import time

from orbi.journal import event
from orbi.pi_recovery import (
    clk_tck, find_idle_descendants, pid_alive, process_ppid, signal_pid,
    snapshot_descendants, timeout_duration,
)

# Idle warning: no model/session activity for 5 minutes (and the model is
# not expected to reply next) logs one `pi_idle` warning; the first new
# session event after it logs `pi_resumed`. model_wait is never idle.
PI_IDLE_WARN_SECONDS = 300.0

# The absolute cap and the single knob for how long one stall may hold a
# slot (Issue #1089): a declared deadline longer than this cap escalates
# anyway, so a wrapper like `timeout 86400` can never hold the slot all day.
PI_IDLE_WAIT_MAX_SECONDS = 3600.0

# Idle-stall recovery: a stalled (non-model_wait) session is recovered
# automatically instead of only warning. The exhaustion window is the cap
# in idle windows, so the same bound governs declared deadlines and an
# undeclared slow command.
PI_IDLE_RECOVERY_CYCLES = int(PI_IDLE_WAIT_MAX_SECONDS / PI_IDLE_WARN_SECONDS)


def _pending_timeout_targets(targets: list[dict]) -> list[tuple[dict, float]]:
    """The pre-idle descendants still INSIDE an explicit `timeout`
    deadline: `[(target, deadline_epoch),...]`.

    A descendant whose command line carries a coreutils
    `timeout <seconds>` wrapper and whose deadline is still in the
    future is a legitimately running tool — the runner waits for the
    deadline instead of signaling it. The age is measured CLOCK
    CONSISTENTLY: the target's recorded start ticks (stat field 22,
    converted with CLK_TCK) against CLOCK_BOOTTIME, never a realtime
    clock — a realtime step after boot (NTP) must not make a tool look
    older than it is. A descendant without a clear timeout or whose
    deadline already passed is not pending: the existing escalation
    applies to it.
    """
    if not targets:
        return []
    hz = clk_tck()
    boot_clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    now_mono = time.clock_gettime(boot_clock)
    now = time.time()
    starts = {
        target["pid"]: target["start_ticks"] / hz
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
        if durations[pid] is not None
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
        remaining = duration - (now_mono - start_mono)
        if remaining > 0:
            pending.append((target, now + remaining))
    return pending


class IdleRecoveryTracker:
    """The idle-stall escalation strategy: the mutable pieces of
    escalation state — once inline in the stream loop — live here
    (`_recorded` / `_window_monotonic` / `_state` / `_targets` /
    `_step` / `_deadline_passed` / `_wait_logged` / `_exhausted`), so
    the escalation is one replaceable strategy the loop
    drives with `reset` (the stall ended), `open_window` (the #18 idle
    warning opened the window: the descendants living at that moment are
    recorded with their start ticks) and one `escalate` call per idle
    poll, then reads `state` (the progress-comment value) and
    `exhausted` (the loop must kill the Pi session now). The rest of the
    loop's state machines are untouched.

    Escalation, one step per idle window of NEW silence since the
    window opened (never in absolute stale seconds — a session that is
    already stale when the window opens, e.g. old record timestamps,
    starts at step one, not at the session kill):
      window 1: SIGTERM the pre-idle descendants (the hung tools —
                found by the ppid chain in `/proc/<pid>/stat` plus
                their start time, never a name guess) so the tool gets
                a non-zero exit and the failure signal reaches the
                model;
      window 2: SIGKILL a TERMed target that survived;
      window `PI_IDLE_RECOVERY_CYCLES` (the absolute cap expressed in
                idle windows): `exhausted` flips — the loop kills the Pi
                session itself and fails fast
                through the normal `ai-blocked` path (the slot is
                never held forever).

    Only pi descendants (ppid chain) recorded when the window opened are
    ever signaled — never other system processes, never a process spawned
    after the window began, never a pid the kernel reused (the recorded
    start ticks are the identity). The evidence exceptions
    (Issues #105/#169/#181) are part of the strategy: a pre-idle
    descendant inside a coreutils `timeout` deadline is a legitimately
    running tool — the escalation waits (`pi_idle_wait` logged once
    per stall) and re-evaluates every window, and the wait pauses the
    session-kill exhaustion counter too (Issue #1089): the kill is
    bounded by the wrapper's own deadline (only up to the
    `PI_IDLE_WAIT_MAX_SECONDS` cap — a declared deadline longer than
    the cap escalates anyway), never by the idle windows;
    a target first observed
    PAST its nominal deadline (or with no deadline of its own) gets
    one full idle window of grace before the TERM — one "still alive"
    observation is not evidence the tool is hung, the wrapper's own
    deadline handling (alarm -> signal delivery -> exit) is
    best-effort and can be delayed by scheduling.
    """

    def __init__(self, window_seconds: float, *, run_id: str,
                 issue_ref: str, role: str) -> None:
        self._window_seconds = window_seconds
        self._run_id = run_id
        self._issue_ref = issue_ref
        self._role = role
        self._reset()

    def _reset(self) -> None:
        # The descendants that existed when the idle window opened,
        # recorded as pid -> start ticks: only they are targets — a
        # process spawned after the window began is a new tool call,
        # never a target, and the ticks are the identity.
        self._recorded: dict[int, int] = {}
        # The monotonic moment the window opened: escalation is
        # measured in idle windows of NEW silence since it (see the
        # class docstring).
        self._window_monotonic: float | None = None
        # The live state shown in the GitHub progress comment (None /
        # `wait` / `term` / `kill`).
        self._state: str | None = None
        # The TERMed descendants tracked for the KILL escalation.
        self._targets: list[dict] = []
        # The highest escalation step already executed (0 none, 1 TERM,
        # 2 KILL): the step advances only when something was signaled
        # (or nothing was left to signal), so the KILL still lands one
        # full idle window after the TERM.
        self._step = 0
        # Past-deadline `timeout` targets first observed alive, mapped
        # to the idle cycle they were first observed: the
        # target is signaled only if it is STILL alive one full idle
        # window later (cycle > recorded cycle); a pid that exits in
        # the meantime is dropped (it simply stops being a target).
        self._deadline_passed: dict[int, int] = {}
        # The `pi_idle_wait` decision is logged once per stall (Issue
        # #169): the escalation re-evaluates every window while the
        # tool is inside its `timeout` deadline, but the journal
        # carries one decision line.
        self._wait_logged = False
        # The monotonic moment the deadline wait for THIS stall began:
        # the wait is capped at `PI_IDLE_WAIT_MAX_SECONDS` since it
        # (Issue #1089) — a declared deadline longer than the cap
        # escalates anyway.
        self._wait_started_mono: float | None = None
        self._exhausted = False

    def reset(self) -> None:
        """The stall is over (the first new session event after the
        idle warning): the whole escalation state resets — a later
        stall starts a fresh window."""
        self._reset()

    def open_window(self, pid: int) -> None:
        """The idle window starts now (the #18 warning poll): `pid` is
        the Pi process whose living descendants are recorded."""
        self._recorded = snapshot_descendants(pid)
        self._window_monotonic = time.monotonic()

    @property
    def state(self) -> str | None:
        """The live recovery state for the progress comment."""
        return self._state

    @property
    def exhausted(self) -> bool:
        """True once `PI_IDLE_RECOVERY_CYCLES` idle windows have passed
        with the session still stalled AND the escalation is not paused
        waiting for a legitimately running tool (Issue #1089): the loop
        must kill the Pi session and fail fast (the slot is never held
        forever)."""
        return self._exhausted

    def escalate(self) -> None:
        """Advance the escalation by one poll (the loop calls this only
        on an idle poll with the window open; the targets are the
        descendants recorded by `open_window`)."""
        silence = time.monotonic() - self._window_monotonic
        cycle = int(silence // self._window_seconds) + 1
        # The exhaustion decision at the end of this method must not run
        # while the escalation is paused on a legitimately running tool
        # (Issue #1089): the `wait` state the PREVIOUS windows left
        # (inside a `timeout` deadline, or the past-deadline grace
        # window) holds the pause, and a deadline wait decided in THIS
        # poll holds it too. Captured BEFORE the branches run so the
        # TERM/KILL steps themselves stay paced exactly as before (the
        # exhaustion still lands one window after the TERM — the same
        # relative ordering as the no-wait escalation). Only the
        # evidence flip or a poll with no paused descendant lets the
        # cycle counter exhaust the session again: the wait is bounded
        # by the wrapper's own deadline (capped at
        # `PI_IDLE_WAIT_MAX_SECONDS`, Issue #1089), never by the idle
        # windows.
        waiting = self._state == "wait"
        if self._step == 0:
            targets = find_idle_descendants(self._recorded)
            # Evidence-based wait: a
            # pre-idle descendant that runs a coreutils
            # `timeout <seconds> ...` wrapper INSIDE its deadline is a
            # legitimately running tool, not a hung one — the runner
            # waits for the deadline instead of TERMed it. The wait
            # decision is logged once and the escalation pauses (the
            # step stays 0): every later window re-evaluates, and when
            # the deadline passes with the descendant still alive the
            # evidence flips and the TERM → KILL → session-kill
            # escalation runs unchanged (the slot is never held
            # forever).
            pending = _pending_timeout_targets(targets)
            if pending:
                if self._wait_started_mono is None:
                    # The wait for this stall begins now; the cap is
                    # measured from here (Issue #1089).
                    self._wait_started_mono = time.monotonic()
                elif (time.monotonic() - self._wait_started_mono
                        >= PI_IDLE_WAIT_MAX_SECONDS):
                    # The bounded wait (Issue #1089): the declared
                    # deadline is honored only up to the cap — a
                    # `timeout 86400` wrapper never holds the slot for
                    # a day. Past the cap the target escalates exactly
                    # like one whose declared deadline passed (the
                    # grace window below, then TERM → KILL → session
                    # kill).
                    pending = []
            if pending:
                if not self._wait_logged:
                    for target, deadline in pending:
                        event(
                            "pi_idle_wait", level=logging.WARNING,
                            run=self._run_id, issue=self._issue_ref,
                            role=self._role, pid=target["pid"],
                            cmdline=target["cmdline"] or "-",
                            deadline=time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(deadline),
                            ),
                        )
                    self._wait_logged = True
                self._state = "wait"
                waiting = True
            else:
                # Past-deadline grace: a target whose
                # nominal `timeout` deadline passed is NOT escalated in
                # the window it is first observed alive — the wrapper's
                # own deadline handling (alarm -> signal delivery ->
                # exit) is best-effort and can be delayed by
                # scheduling, so one "past deadline and still alive"
                # observation is not evidence the wrapper failed. The
                # pid is recorded (the grace window, nothing is
                # signaled) and signaled only if it is STILL alive in a
                # later escalation window; a pid that exits in the
                # meantime is dropped. The `recovery=wait` state stays
                # visible while the grace runs (the tool is still
                # inside its own deadline handling, not hung). The
                # grace is measured in idle windows, not polls — a
                # target first observed past its deadline in cycle N is
                # signaled only if it is still alive in a LATER cycle
                # (one full idle window of grace for the wrapper's own
                # deadline handling).
                flipped = [
                    target for target in targets
                    if target["pid"] in self._deadline_passed
                    and self._deadline_passed[target["pid"]] < cycle
                ]
                newly_passed = [
                    target for target in targets
                    if target["pid"] not in self._deadline_passed
                ]
                if flipped:
                    # Still alive one full idle window after the
                    # nominal deadline: the wrapper failed to end the
                    # command — the evidence is confirmed, the TERM ->
                    # KILL -> session-kill escalation runs unchanged
                    # (the slot is never held forever). A target first
                    # observed past its deadline in this same cycle is
                    # NOT recorded here: it starts its own grace cycle
                    # next cycle (one extra window of grace in this
                    # rare concurrent case is harmless).
                    for target in flipped:
                        self._deadline_passed.pop(target["pid"], None)
                    self._targets = flipped
                    for target in flipped:
                        result = signal_pid(
                            target["pid"], signal.SIGTERM,
                            expected_start_ticks=target["start_ticks"],
                        )
                        event(
                            "pi_idle_term", level=logging.WARNING,
                            run=self._run_id, issue=self._issue_ref,
                            role=self._role, pid=target["pid"],
                            cmdline=target["cmdline"] or "-", result=result,
                        )
                    self._state = "term"
                    # The TERM step ran (the grace window does NOT
                    # advance the step: nothing was signaled there, so
                    # the KILL escalation still lands one full window
                    # after the TERM, as before).
                    self._step = 1
                elif newly_passed:
                    # First observation past the deadline: the grace
                    # window (no signal, the wait state stays visible,
                    # the step does NOT advance — nothing was
                    # signaled).
                    for target in newly_passed:
                        self._deadline_passed[target["pid"]] = cycle
                    self._state = "wait"
                elif [t for t in targets
                      if t["pid"] in self._deadline_passed]:
                    # The target is inside its grace cycle (recorded,
                    # still alive, one full idle window not yet up): no
                    # signal, the wait state stays visible, the step
                    # does NOT advance.
                    self._state = "wait"
                else:
                    # No hung tool found (Pi itself is stuck): the
                    # escalation continues, nothing is signaled.
                    event(
                        "pi_idle_term", level=logging.WARNING,
                        run=self._run_id, issue=self._issue_ref,
                        role=self._role, result="no_target",
                    )
                    # The pre-idle descendants are gone (a waited tool
                    # reached its own deadline): the wait state is
                    # stale — clear it so the progress comment does not
                    # keep showing `recovery: wait` while the
                    # escalation runs.
                    self._state = None
                    self._deadline_passed.clear()
                    self._step = 1
        elif self._step == 1 and cycle >= 2:
            for target in self._targets:
                if not pid_alive(target["pid"]):
                    # The TERM worked between polls: record it,
                    # signal nothing.
                    event(
                        "pi_idle_kill", level=logging.WARNING,
                        run=self._run_id, issue=self._issue_ref,
                        role=self._role, pid=target["pid"],
                        cmdline=target["cmdline"] or "-",
                        result="already_dead",
                    )
                    continue
                result = signal_pid(
                    target["pid"], signal.SIGKILL,
                    expected_start_ticks=target["start_ticks"],
                )
                event(
                    "pi_idle_kill", level=logging.WARNING,
                    run=self._run_id, issue=self._issue_ref,
                    role=self._role, pid=target["pid"],
                    cmdline=target["cmdline"] or "-", result=result,
                )
            self._state = "kill"
            self._step = 2
        if cycle >= PI_IDLE_RECOVERY_CYCLES and not waiting:
            self._exhausted = True
