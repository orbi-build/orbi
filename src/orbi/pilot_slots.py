"""Cross-process concurrency slots for Orbi (Issue #39).

The local machine can only serve a limited number of concurrent Pilot
tasks, so the configured ``max_concurrency`` is enforced with one slot
file per allowed task under ``<repo_dir>/.orbi/slots/``.

Each slot file is a plain file whose exclusive ``flock(2)`` lock is the
ownership token:

- Take: open the slot file (created if missing), write the holder PID as
  observational metadata, then try ``flock(fd, LOCK_EX | LOCK_NB)``. The
  kernel grants the lock to at most one process at a time, so at most one
  Runner ever owns a given slot. No in-process counter, no GitHub label,
  no distributed lock, no stale-PID or age heuristic.
- Holder: the lock is owned by the open file descriptor, not by the PID.
  A live holder can never lose its slot based on elapsed time or a
  missing write — even if it pauses arbitrarily long before or after
  taking the lock.
- Release: closing the descriptor releases the lock, and the kernel
  releases it when the process exits for ANY reason (normal exit,
  SIGTERM, SIGKILL). There is no atexit hook, no signal handler and no
  unlink protocol: a dead holder can never keep a slot, so an abnormal
  exit never deadlocks the machine.

No database, queue, daemon, or fallback.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

SLOT_DIRNAME = ".orbi/slots"


class Slot:
    """One held slot: the open descriptor owns the exclusive flock lock."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    def release(self) -> None:
        """Release the slot; closing the descriptor releases the lock."""
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def slot_dir_for(repo_dir: Path) -> Path:
    """Return the slot state directory of one configured repo."""
    return repo_dir / SLOT_DIRNAME


def slot_path(state_dir: Path, index: int) -> Path:
    """Return the slot file path for one 1-based slot index."""
    return state_dir / f"slot-{index}"


def acquire_slot(state_dir: Path, capacity: int, pid: int) -> Slot | None:
    """Take one free slot, or return None when all are already held.

    ``capacity`` is the configured ``max_concurrency``. Each slot index
    is tried in order: the slot file is created if missing and the
    exclusive ``flock`` is attempted non-blocking. The kernel makes the
    take mutually exclusive across processes — a slot whose lock is held
    by another live process is skipped, and no heuristic can ever grant
    the same slot to two live holders.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, capacity + 1):
        path = slot_path(state_dir, index)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return None  # slot dir unusable: fail closed, never exceed capacity
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The lock is held: record the holder PID as observational
            # metadata (the lock, not the PID, is the ownership token).
            os.ftruncate(fd, 0)
            os.write(fd, f"{pid}\n".encode("ascii"))
        except (BlockingIOError, PermissionError):
            os.close(fd)
            continue  # held by another live process: try the next index
        except OSError:
            os.close(fd)
            return None
        return Slot(path, fd)
    return None


def slot_occupancy(state_dir: Path, capacity: int) -> list[tuple[int, int | None]]:
    """Return ``(index, holder_pid)`` per slot; None when the slot is free.

    Occupancy is probed with a non-blocking ``flock`` on the slot file:
    the lock itself is the source of truth, so a probe that succeeds
    proves the slot is free (the probe unlocks immediately) and a probe
    that fails proves a live process holds it. The PID is read from the
    file only as observational metadata for `status`.
    """
    state_dir = Path(state_dir)
    occupancy: list[tuple[int, int | None]] = []
    for index in range(1, capacity + 1):
        path = slot_path(state_dir, index)
        if not path.is_file():
            occupancy.append((index, None))
            continue
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            occupancy.append((index, None))
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                occupancy.append((index, _read_pid(path)))
                continue
            occupancy.append((index, None))
        finally:
            os.close(fd)
    return occupancy


def _read_pid(path: Path) -> int | None:
    """Return the observational holder PID from one slot file, if present."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None
