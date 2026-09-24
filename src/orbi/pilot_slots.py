"""Cross-process concurrency slots for Orbi.

The local machine can only serve a limited number of concurrent Pilot
tasks, so the configured ``max_concurrency`` is enforced with one slot
file per allowed task under ``<repo_dir>/.orbi/slots/``.

Each slot file is a plain file whose exclusive ``flock(2)`` lock is the
ownership token:

- Take: open the slot file (created if missing), try
  ``flock(fd, LOCK_EX | LOCK_NB)``, and only once the lock is held write
  the holder PID as observational metadata. The
  kernel grants the lock to at most one process at a time, so at most one
  Runner ever owns a given slot. No in-process counter, no GitHub label,
  no distributed lock, no stale-PID or age heuristic.
- Holder: the lock is owned by the open file descriptor, not by the PID.
  A live holder can never lose its slot based on elapsed time or a
  missing write — even if it pauses arbitrarily long before or after
  taking the lock.
- Delivery identity: after selecting a delivery the holder
  rewrites its slot file to ``<pid>\\n<repo>#<issue>`` — line 1 stays the
  holder PID, line 2 names the (repo, issue) the holder is working on.
  Like the PID, the identity is observational metadata; the flock stays
  the only ownership token. It lets another runner's resume scan skip
  exactly the deliveries that are in flight instead of abandoning the
  whole scan (the pre-#809 whole-slot-dir guard starved every review
  while any delivery was in flight).
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
CLAIM_LOCK_FILENAME = "claim.lock"

#: Sentinel for a slot whose flock is held while the holder PID is not
#: readable (a torn rewrite window, a corrupted PID line, or a slot file
#: that cannot even be opened for probing). The lock is the truth, so
#: such a slot is reported as held — never as free: every liveness
#: consumer (`pick_in_progress_issue`, `_another_live_runner`) reads any
#: non-``None`` foreign holder as a live co-runner, and folding a held
#: slot into ``None`` would let a second Pi start on a live run (#39).
HELD_PID_UNKNOWN = -1


class ClaimLock:
    """Exclusive flock lock that serializes the claim window.

    Held across ``pick_next_delivery`` -> ``mark_slot_delivery`` so
    two concurrent runners never claim the same Issue in the same tick.
    """

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    def release(self) -> None:
        """Release the claim lock; closing the descriptor releases the flock."""
        if self.fd >= 0:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def __enter__(self) -> ClaimLock:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.release()


def acquire_claim_lock(state_dir: Path, blocking: bool = True) -> ClaimLock | None:
    """Acquire the host-local claim serialization lock.

    Serializes the window from ``pick_next_delivery`` through
    ``mark_slot_delivery`` so that a second runner scans only after the
    first has written its identity, allowing ``slot_held_deliveries``
    to skip the claimed delivery.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / CLAIM_LOCK_FILENAME
    # Both calls fail fast: a claim lock that cannot be taken would silently
    # return the machine to the unserialized claim window this lock exists
    # to close, and the double claim would look like a fresh bug.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError:
        # The non-blocking probe found the lock held by another process.
        os.close(fd)
        return None
    return ClaimLock(path, fd)


def is_claim_lock_held(state_dir: Path) -> bool:
    """Return True if another process holds the claim lock."""
    probe = acquire_claim_lock(state_dir, blocking=False)
    if probe is None:
        return True
    probe.release()
    return False


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
    """Return ``(index, holder_pid)`` per slot.

    ``None`` means the slot is provably free (the probe took and
    immediately released the lock). A positive pid means the lock is
    held by that process. :data:`HELD_PID_UNKNOWN` means the lock is
    held — or cannot be probed at all — while the holder PID is not
    readable: the lock is the source of truth, so such a slot is
    reported as held, never as free (fail closed, mirroring
    ``acquire_slot``'s fail-closed on the same open error).

    Occupancy is probed with a non-blocking ``flock`` on the slot file:
    the lock itself is the source of truth, so a probe that succeeds
    proves the slot is free and a probe that fails proves a live
    process holds it. The PID is read from the file only as
    observational metadata for `status`.
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
            occupancy.append((index, HELD_PID_UNKNOWN))
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                pid = _read_pid(path)
                occupancy.append(
                    (index, HELD_PID_UNKNOWN if pid is None else pid)
                )
                continue
            occupancy.append((index, None))
        finally:
            os.close(fd)
    return occupancy


def mark_slot_delivery(slot: Slot, repo: str, issue: int) -> None:
    """Record in the held slot file which delivery the holder is working.

    Called by the runner immediately after selecting a
    delivery — a fresh claim (the implement phase is in flight), a
    resume (the review is in flight) and the implement→opened-PR
    boundary are all covered by this one write. The identity is what
    another runner's resume scan matches on (`slot_held_deliveries`):
    skip exactly this (repo, issue), never the whole scan. The file is
    rewritten under the slot's own flock; a write failure propagates
    (fail fast — the delivery has not started, the next tick re-picks).
    """
    os.ftruncate(slot.fd, 0)
    os.lseek(slot.fd, 0, os.SEEK_SET)
    os.write(
        slot.fd, f"{os.getpid()}\n{repo}#{int(issue)}\n".encode("ascii"),
    )


def slot_held_deliveries(state_dir: Path, capacity: int) -> set[tuple[str, int]]:
    """Return the (repo, issue) deliveries held by LIVE other runners.

    Only slots whose flock is held by another pid are read —
    the lock is the truth, so a free slot's leftover identity is stale by
    definition and a live holder without a readable identity (not yet
    selected, or an old runner) contributes nothing (fail open: the scan
    skips only a delivery that is provably held by someone else). The
    holder's own pid never appears: a runner holds a delivery only after
    its scan has finished.
    """
    state_dir = Path(state_dir)
    mine = os.getpid()
    held: set[tuple[str, int]] = set()
    for index, holder in slot_occupancy(state_dir, capacity):
        if holder is None or holder == mine:
            continue
        identity = _read_delivery(slot_path(state_dir, index))
        if identity is not None:
            held.add(identity)
    return held


def _read_delivery(path: Path) -> tuple[str, int] | None:
    """Parse the ``<repo>#<issue>`` identity line from one slot file.

    Line 1 is the holder pid, line 2 the optional identity; anything
    else (a missing, empty or malformed line) means no identity.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    repo, sep, number = lines[1].strip().rpartition("#")
    if not sep or not repo or not number.isdigit():
        return None
    return repo, int(number)


def _read_pid(path: Path) -> int | None:
    """Return the observational holder PID from one slot file, if present.

    The PID is the file's FIRST line; an identity line or
    any other trailing content never breaks the parse.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    first = raw.split("\n", 1)[0].strip() if raw else ""
    return int(first) if first.isdigit() else None
