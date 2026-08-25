"""Unit tests for cross-process concurrency slots (Issue #39).

Slots are atomic files under one state directory: ``os.open(O_CREAT|O_EXCL)``
makes the take mutually exclusive across processes, the file content carries
the holder PID, and a slot whose holder PID no longer runs is stale and can
be taken again. Release happens on normal exit (atexit) and on SIGTERM/SIGINT
(signal handlers); SIGKILL is covered by the stale check. The subprocess tests
drive the real module in a real child process so atexit/signal handling is
exercised for real.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pilot_slots


def dead_pid() -> int:
    """Return a PID that is guaranteed to be dead (already reaped)."""
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    process.wait()
    return process.pid


def live_pid() -> int:
    return os.getpid()


# --- acquire_slot -----------------------------------------------------------


def test_acquire_slot_creates_slot_file_with_holder_pid(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, live_pid())
    assert slot == state / "slot-1"
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_acquire_slot_creates_state_dir_when_missing(tmp_path):
    state = tmp_path / "nested" / "slots"
    assert pilot_slots.acquire_slot(state, 1, live_pid()) == state / "slot-1"
    assert state.is_dir()


def test_acquire_slot_returns_none_when_all_slots_are_live(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(live_pid()), encoding="utf-8")
    assert pilot_slots.acquire_slot(state, 1, live_pid()) is None


def test_acquire_slot_takes_next_free_slot(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(live_pid()), encoding="utf-8")
    slot = pilot_slots.acquire_slot(state, 2, live_pid())
    assert slot == state / "slot-2"
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_acquire_slot_takes_stale_slot_left_by_dead_holder(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(dead_pid()), encoding="utf-8")
    slot = pilot_slots.acquire_slot(state, 1, live_pid())
    assert slot == state / "slot-1"
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_acquire_slot_treats_fresh_empty_slot_file_as_live(tmp_path):
    """A fresh empty file means the holder is still writing its PID."""
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text("", encoding="utf-8")
    assert pilot_slots.acquire_slot(state, 1, live_pid()) is None


def test_acquire_slot_takes_orphaned_empty_slot_file(tmp_path):
    """A holder killed between O_EXCL and the PID write leaves an empty file;
    once it is old, the next runner takes the slot instead of capacity_full."""
    state = tmp_path / "slots"
    state.mkdir()
    path = state / "slot-1"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)  # no PID written: the holder died before it
    age = pilot_slots.EMPTY_SLOT_STALE_SECONDS + 1
    past = time.time() - age
    os.utime(path, (past, past))
    slot = pilot_slots.acquire_slot(state, 1, live_pid())
    assert slot == path
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_acquire_slot_takes_slot_with_corrupted_content(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text("garbage", encoding="utf-8")
    assert pilot_slots.acquire_slot(state, 1, live_pid()) == state / "slot-1"


def test_acquire_slot_ignores_unlink_race_on_stale_slot(
    monkeypatch, tmp_path,
):
    """Two processes may unlink the same stale slot; only one wins the take."""
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(dead_pid()), encoding="utf-8")
    real_unlink = Path.unlink
    calls = {"count": 0}

    def flaky_unlink(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise FileNotFoundError
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    slot = pilot_slots.acquire_slot(state, 1, live_pid())
    assert slot == state / "slot-1"
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_acquire_slot_never_exceeds_capacity(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    for index in (1, 2):
        (state / f"slot-{index}").write_text(str(live_pid()), encoding="utf-8")
    assert pilot_slots.acquire_slot(state, 2, live_pid()) is None


# --- _slot_is_stale / _pid_is_dead ------------------------------------------


def test_slot_is_stale_is_false_for_unreadable_path(tmp_path):
    # A directory is unreadable as text (OSError) and must never be stolen.
    assert pilot_slots._slot_is_stale(tmp_path) is False


def test_slot_is_stale_is_false_for_fresh_empty_file(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text("", encoding="utf-8")
    assert pilot_slots._slot_is_stale(path) is False


def test_slot_is_stale_is_true_for_aged_empty_file(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text("", encoding="utf-8")
    past = time.time() - pilot_slots.EMPTY_SLOT_STALE_SECONDS - 1
    os.utime(path, (past, past))
    assert pilot_slots._slot_is_stale(path) is True


def test_slot_is_stale_is_true_when_empty_file_vanishes(monkeypatch, tmp_path):
    path = tmp_path / "slot-1"
    path.write_text("", encoding="utf-8")

    def flaky_stat(self, *args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert pilot_slots._slot_is_stale(path) is True


def test_slot_is_stale_is_true_for_corrupted_content(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text("garbage", encoding="utf-8")
    assert pilot_slots._slot_is_stale(path) is True


def test_slot_is_stale_is_true_for_dead_pid(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text(str(dead_pid()), encoding="utf-8")
    assert pilot_slots._slot_is_stale(path) is True


def test_slot_is_stale_is_false_for_live_pid(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text(str(live_pid()), encoding="utf-8")
    assert pilot_slots._slot_is_stale(path) is False


def test_pid_is_dead_is_true_when_process_lookup_fails(monkeypatch):
    def fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(pilot_slots.os, "kill", fake_kill)
    assert pilot_slots._pid_is_dead(4242) is True


def test_pid_is_dead_is_false_when_kill_succeeds(monkeypatch):
    monkeypatch.setattr(pilot_slots.os, "kill", lambda pid, sig: None)
    assert pilot_slots._pid_is_dead(4242) is False


def test_pid_is_dead_is_false_on_permission_error(monkeypatch):
    """A process we cannot signal is still alive."""

    def fake_kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(pilot_slots.os, "kill", fake_kill)
    assert pilot_slots._pid_is_dead(4242) is False


def test_pid_is_dead_is_true_for_pid_beyond_c_int_range():
    # os.kill raises OverflowError for such pids; they can never exist.
    assert pilot_slots._pid_is_dead(10**20) is True


# --- release_slot / hold_slot -------------------------------------------------


def test_release_slot_removes_slot_file(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text(str(live_pid()), encoding="utf-8")
    pilot_slots.release_slot(path)
    assert not path.exists()


def test_release_slot_ignores_missing_file(tmp_path):
    pilot_slots.release_slot(tmp_path / "slot-1")  # must not raise


REPO_ROOT = Path(__file__).resolve().parent.parent


def run_slot_script(code: str) -> subprocess.Popen:
    """Start a child process that imports pilot_slots from the repo root."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def slot_script_code(state: Path, extra: str = "") -> str:
    return (
        "import os, sys, time\n"
        "import pilot_slots\n"
        f"state = {str(state)!r}\n"
        "slot = pilot_slots.acquire_slot(state, 1, os.getpid())\n"
        "if slot is None:\n"
        "    print('denied')\n"
        "    sys.exit(1)\n"
        "pilot_slots.hold_slot(slot)\n"
        "print('ready', flush=True)\n"
        f"{extra}\n"
    )


def test_hold_slot_releases_on_normal_exit(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "time.sleep(0.2)"))
    out, _ = process.communicate(timeout=30)
    assert process.returncode == 0
    assert "ready" in out
    # atexit removed the slot file when the process exited normally.
    assert not (state / "slot-1").exists()


def test_hold_slot_releases_on_sigterm(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    # The signal handler removed the slot file before the process died.
    assert not (state / "slot-1").exists()


def test_hold_slot_releases_on_sigint(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.send_signal(signal.SIGINT)
    process.wait(timeout=10)
    assert not (state / "slot-1").exists()


def test_signal_handler_releases_slot_and_reraises_default(monkeypatch, tmp_path):
    """The handler releases the slot, restores SIG_DFL and re-raises."""
    path = tmp_path / "slot-1"
    path.write_text(str(live_pid()), encoding="utf-8")
    handler = pilot_slots._make_signal_handler(path)
    killed: list = []
    signaled: list = []
    monkeypatch.setattr(
        pilot_slots.os, "kill",
        lambda pid, sig: killed.append((pid, sig)),
    )
    monkeypatch.setattr(
        pilot_slots.signal, "signal",
        lambda signum, action: signaled.append((signum, action)),
    )
    handler(signal.SIGTERM, None)
    assert not path.exists()
    assert signaled == [(signal.SIGTERM, signal.SIG_DFL)]
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_killed_process_leaves_stale_slot_that_is_reclaimable(tmp_path):
    """SIGKILL cannot run handlers; the stale check must reclaim the slot."""
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.kill()  # SIGKILL: no atexit, no signal handler
    process.wait(timeout=10)
    # The slot file survives the kill ...
    assert (state / "slot-1").exists()
    # ... but the next runner takes it back (holder PID is dead).
    slot = pilot_slots.acquire_slot(state, 1, live_pid())
    assert slot == state / "slot-1"
    assert slot.read_text(encoding="utf-8") == str(live_pid())


def test_two_processes_cannot_take_the_same_slot(tmp_path):
    """Concurrent takes: O_EXCL lets at most one process win each slot."""
    state = tmp_path / "slots"
    results: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(
            pilot_slots.acquire_slot(state, 1, os.getpid()),
        )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    taken = [slot for slot in results if slot is not None]
    assert len(taken) == 1
    assert taken[0] == state / "slot-1"


# --- slot_occupancy -----------------------------------------------------------


def test_slot_occupancy_reports_free_slots_for_missing_dir(tmp_path):
    assert pilot_slots.slot_occupancy(tmp_path / "missing", 2) == [
        (1, None), (2, None),
    ]


def test_slot_occupancy_reports_live_and_free_slots(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(live_pid()), encoding="utf-8")
    assert pilot_slots.slot_occupancy(state, 2) == [
        (1, live_pid()), (2, None),
    ]


def test_slot_occupancy_reports_none_for_corrupted_slot(tmp_path):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text("garbage", encoding="utf-8")
    assert pilot_slots.slot_occupancy(state, 1) == [(1, None)]
