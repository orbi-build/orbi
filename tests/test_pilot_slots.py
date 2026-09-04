"""Unit tests for cross-process concurrency slots (Issue #39).

Slots are one file per allowed task whose exclusive ``flock(2)`` lock is
the ownership token: the kernel grants the lock to at most one process
at a time, and closing the descriptor — or the process exiting for ANY
reason (normal exit, SIGTERM, SIGKILL) — releases it automatically. The
holder PID in the file is observational metadata only; there is no
stale-PID/age heuristic and no atexit/signal cleanup protocol. The
subprocess tests drive the real module in real child processes so the
kernel lock behavior is exercised for real.
"""
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from orbi import pilot_slots

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_slot_script(code: str) -> subprocess.Popen:
    """Start a child process that imports pilot_slots from the
    checkout's `src/` layout (Issue #168). The cwd is NEUTRAL: with the
    checkout root as cwd, the repo-root `orbi.py` compat shim
    would shadow the `orbi` package on sys.path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        cwd="/",
    )


def slot_script_code(state: Path, extra: str = "") -> str:
    return (
        "import os, sys\n"
        "from orbi import pilot_slots\n"
        f"state = {str(state)!r}\n"
        "slot = pilot_slots.acquire_slot(state, 1, os.getpid())\n"
        "if slot is None:\n"
        "    print('denied', flush=True)\n"
        "    sys.exit(1)\n"
        "print('ready', flush=True)\n"
        f"{extra}\n"
    )


# --- acquire_slot -----------------------------------------------------------


def test_acquire_slot_takes_free_slot_and_records_holder_pid(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    assert slot.path == state / "slot-1"
    assert slot.path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_acquire_slot_creates_state_dir_when_missing(tmp_path):
    state = tmp_path / "nested" / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    assert state.is_dir()
    assert slot.path == state / "slot-1"


def test_acquire_slot_denied_while_another_fd_holds_the_lock(tmp_path):
    """The lock, not the PID, is the token: a second take (even in this
    process, even with the same PID) is denied while the first fd holds
    the exclusive lock."""
    state = tmp_path / "slots"
    first = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert first is not None
    assert pilot_slots.acquire_slot(state, 1, os.getpid()) is None
    first.release()
    # After the release the same process can take the slot again.
    second = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert second is not None
    assert second.path == state / "slot-1"
    second.release()


def test_acquire_slot_denied_while_live_child_holds_the_lock(tmp_path):
    """A live holder's lock is never stolen: a second process is denied."""
    state = tmp_path / "slots"
    holder = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert holder.stdout.readline().strip() == "ready"
    try:
        assert pilot_slots.acquire_slot(state, 1, os.getpid()) is None
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_acquire_slot_takes_next_free_slot(tmp_path):
    state = tmp_path / "slots"
    first = pilot_slots.acquire_slot(state, 2, os.getpid())
    assert first is not None
    second = pilot_slots.acquire_slot(state, 2, os.getpid())
    assert second is not None
    assert first.path == state / "slot-1"
    assert second.path == state / "slot-2"
    assert pilot_slots.acquire_slot(state, 2, os.getpid()) is None
    first.release()
    second.release()


def test_acquire_slot_releases_slot_file_lock_on_release(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()
    # The lock is gone: a new take succeeds and rewrites the PID.
    again = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert again is not None
    again.release()


# --- the paused-live-holder race (review Major 2) ----------------------------


def test_paused_live_holder_never_loses_the_slot_to_a_second_runner(
    tmp_path,
):
    """The rejected race: holder A opens the slot file and pauses for far
    longer than the old 30-second stale window before taking the lock.
    Runner B must be denied while A is live, and A must still hold the
    slot when it resumes — one slot, never two live owners."""
    state = tmp_path / "slots"
    code = (
        "import os, sys, time, fcntl\n"
        "from pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "path = state / 'slot-1'\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "print('opened', flush=True)\n"
        "time.sleep(2.0)  # pause far beyond any stale window\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('denied', flush=True)\n"
        "    sys.exit(0)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = run_slot_script(code)
    assert holder.stdout.readline().strip() == "opened"
    # A has the file open but not the lock yet: B may take the slot ...
    b_slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert b_slot is not None
    # ... while A is still live. A now takes the lock: it must be denied,
    # because B holds it. One slot, one live owner — never both.
    assert holder.stdout.readline().strip() == "denied"
    holder.wait(timeout=10)
    b_slot.release()


def test_paused_holder_resuming_after_second_runner_exits(tmp_path):
    """A pauses before the lock; B takes and later releases; A resumes and
    takes the now-free slot. Ownership transfers, it never overlaps."""
    state = tmp_path / "slots"
    code = (
        "import os, sys, time, fcntl\n"
        "from pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "path = state / 'slot-1'\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "print('opened', flush=True)\n"
        "time.sleep(2.0)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = run_slot_script(code)
    assert holder.stdout.readline().strip() == "opened"
    b_slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert b_slot is not None
    b_slot.release()  # B exits: the lock is free again
    # A resumes: the slot is free, so A takes it.
    assert holder.stdout.readline().strip() == "locked"
    holder.kill()
    holder.wait(timeout=10)


# --- exit release (normal / SIGTERM / SIGKILL) -------------------------------


def test_slot_released_on_normal_exit(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "import time; time.sleep(0.2)"))
    out, _ = process.communicate(timeout=30)
    assert process.returncode == 0
    assert "ready" in out
    # The kernel released the lock when the process exited: a new take
    # succeeds without any cleanup protocol.
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()


def test_slot_released_on_sigterm(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()


def test_slot_released_on_sigint(tmp_path):
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.send_signal(signal.SIGINT)
    process.wait(timeout=10)
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()


def test_killed_process_never_keeps_the_slot(tmp_path):
    """SIGKILL cannot run any cleanup; the kernel still releases the
    lock, so the next runner takes the slot back — no permanent lock."""
    state = tmp_path / "slots"
    process = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert process.stdout.readline().strip() == "ready"
    process.kill()  # SIGKILL
    process.wait(timeout=10)
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    assert slot.path == state / "slot-1"
    slot.release()


# --- concurrent takes ---------------------------------------------------------


def test_two_processes_cannot_take_the_same_slot(tmp_path):
    """Concurrent real-process takes: at most one process wins the slot."""
    state = tmp_path / "slots"
    first = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert first.stdout.readline().strip() == "ready"
    try:
        second = pilot_slots.acquire_slot(state, 1, os.getpid())
        assert second is None
    finally:
        first.terminate()
        first.wait(timeout=10)


def test_concurrent_thread_takes_one_winner_per_slot(tmp_path):
    """Concurrent takes in threads (one fd each): exactly one winner."""
    state = tmp_path / "slots"
    results: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(pilot_slots.acquire_slot(state, 1, os.getpid()))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    taken = [slot for slot in results if slot is not None]
    assert len(taken) == 1
    assert taken[0].path == state / "slot-1"
    taken[0].release()


# --- slot_occupancy -----------------------------------------------------------


def test_slot_occupancy_reports_free_slots_for_missing_dir(tmp_path):
    assert pilot_slots.slot_occupancy(tmp_path / "missing", 2) == [
        (1, None), (2, None),
    ]


def test_slot_occupancy_reports_live_holder_with_pid(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 2, os.getpid())
    assert slot is not None
    # The lock is held by this process: slot 1 is occupied with our PID,
    # slot 2 is free.
    assert pilot_slots.slot_occupancy(state, 2) == [
        (1, os.getpid()), (2, None),
    ]
    slot.release()


def test_slot_occupancy_reports_free_slot_after_release(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()
    assert pilot_slots.slot_occupancy(state, 1) == [(1, None)]


def test_slot_occupancy_reports_live_child_holder(tmp_path):
    state = tmp_path / "slots"
    holder = run_slot_script(slot_script_code(state, "import time; time.sleep(30)"))
    assert holder.stdout.readline().strip() == "ready"
    try:
        assert pilot_slots.slot_occupancy(state, 1) == [
            (1, holder.pid),
        ]
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_slot_occupancy_ignores_slot_file_with_corrupted_pid(tmp_path):
    """The lock decides occupancy; a corrupted PID is display metadata
    only and must not break the report."""
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text("garbage", encoding="utf-8")
    assert pilot_slots.slot_occupancy(state, 1) == [(1, None)]


def test_slot_occupancy_reports_none_pid_for_live_holder_without_pid(tmp_path):
    """A live holder that never wrote a PID (e.g. an old process) is
    still reported as occupied; the PID is just missing."""
    state = tmp_path / "slots"
    state.mkdir()
    code = (
        "import os, sys, time, fcntl\n"
        "from pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "path = state / 'slot-1'\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = run_slot_script(code)
    assert holder.stdout.readline().strip() == "ready"
    try:
        assert pilot_slots.slot_occupancy(state, 1) == [(1, None)]
    finally:
        holder.terminate()
        holder.wait(timeout=10)


# --- Slot.release -------------------------------------------------------------


def test_release_is_idempotent(tmp_path):
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None
    slot.release()
    slot.release()  # must not raise
    again = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert again is not None
    again.release()


def test_release_ignores_close_error(monkeypatch, tmp_path):
    """A close that fails (e.g. fd already gone) must not raise: the
    kernel releases the lock with the process either way."""
    state = tmp_path / "slots"
    slot = pilot_slots.acquire_slot(state, 1, os.getpid())
    assert slot is not None

    def failing_close(fd):
        raise OSError("already closed")

    monkeypatch.setattr(pilot_slots.os, "close", failing_close)
    slot.release()  # must not raise
    assert slot.fd == -1


def test_acquire_slot_fails_closed_when_open_fails(monkeypatch, tmp_path):
    """An unusable slot dir fails closed: no slot is granted, so the
    capacity can never be exceeded by a broken state dir."""
    state = tmp_path / "slots"
    state.mkdir()

    def failing_open(path, flags, mode=0o777):
        raise OSError("permission denied")

    monkeypatch.setattr(pilot_slots.os, "open", failing_open)
    assert pilot_slots.acquire_slot(state, 1, os.getpid()) is None


def test_acquire_slot_fails_closed_when_write_fails(monkeypatch, tmp_path):
    """A write failure after the lock is taken is not swallowed: the
    slot is not granted (the lock is released with the closed fd)."""
    state = tmp_path / "slots"
    state.mkdir()

    def failing_ftruncate(fd, length):
        raise OSError("write failed")

    monkeypatch.setattr(pilot_slots.os, "ftruncate", failing_ftruncate)
    assert pilot_slots.acquire_slot(state, 1, os.getpid()) is None


def test_slot_occupancy_treats_unopenable_slot_file_as_free(
    monkeypatch, tmp_path,
):
    state = tmp_path / "slots"
    state.mkdir()
    (state / "slot-1").write_text(str(os.getpid()), encoding="utf-8")

    def failing_open(path, flags, mode=0o777):
        raise OSError("permission denied")

    monkeypatch.setattr(pilot_slots.os, "open", failing_open)
    assert pilot_slots.slot_occupancy(state, 1) == [(1, None)]


def test_read_pid_returns_none_when_file_unreadable(monkeypatch, tmp_path):
    path = tmp_path / "slot-1"
    path.write_text(str(os.getpid()), encoding="utf-8")

    def failing_read(self, *args, **kwargs):
        raise OSError("vanished")

    monkeypatch.setattr(Path, "read_text", failing_read)
    assert pilot_slots._read_pid(path) is None


def test_read_pid_returns_none_for_non_numeric_content(tmp_path):
    path = tmp_path / "slot-1"
    path.write_text("garbage", encoding="utf-8")
    assert pilot_slots._read_pid(path) is None


def test_slot_dir_for_derives_state_directory():
    assert (
        pilot_slots.slot_dir_for(Path("/srv/repo"))
        == Path("/srv/repo") / ".orbi" / "slots"
    )


def test_slot_path_is_one_based(tmp_path):
    assert pilot_slots.slot_path(tmp_path, 3) == tmp_path / "slot-3"
