"""Cross-process concurrency slots for Muyan Pilot (Issue #39).

The local machine can only serve a limited number of concurrent Pilot tasks,
so the configured ``max_concurrency`` is enforced with one slot file per
allowed task under ``<repo_dir>/.muyan-pilot/slots/``:

- Take: ``os.open(O_CREAT|O_EXCL)`` is atomic across processes, so at most
  one Runner ever wins a given slot. No in-process counter, no GitHub label
  and no distributed lock — just one file per slot.
- Holder: the slot file carries the PID of the process that holds it.
- Release: the holder removes the file on normal exit (``atexit``) and on
  SIGTERM/SIGINT (signal handlers re-raise the default action afterwards).
- Stale: a SIGKILLed process cannot run handlers, so a slot whose holder PID
  no longer runs is stale and the next Runner takes it back. A slot file is
  created empty and filled with the holder PID in the same microsecond; a
  fresh empty file is live (the writer is still running), but an empty file
  older than ``EMPTY_SLOT_STALE_SECONDS`` is stale, so even a holder killed
  in that window can never occupy a slot forever. A killed runner therefore
  never deadlocks the machine.

No database, queue, daemon, or fallback.
"""
from __future__ import annotations

import atexit
import os
import signal
import time
from pathlib import Path

SLOT_DIRNAME = ".muyan-pilot/slots"

# A slot file is created empty and filled with the holder PID in the same
# microsecond; if the holder dies in that window the file stays empty. An
# empty file is live while it is fresh (the writer is still running) and
# stale once it is older than this, so an orphaned empty file can never
# occupy a slot forever.
EMPTY_SLOT_STALE_SECONDS = 30.0


def slot_dir_for(repo_dir: Path) -> Path:
    """Return the slot state directory of one configured repo."""
    return repo_dir / SLOT_DIRNAME


def slot_path(state_dir: Path, index: int) -> Path:
    """Return the slot file path for one 1-based slot index."""
    return state_dir / f"slot-{index}"


def acquire_slot(state_dir: Path, capacity: int, pid: int) -> Path | None:
    """Atomically take one free slot, or return None when all are live.

    ``capacity`` is the configured ``max_concurrency``. Each slot index is
    tried in order: a missing slot is created with O_EXCL (the atomic
    cross-process take) and filled with the holder PID; a slot whose holder
    is dead is unlinked and retried; a live slot is skipped.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    for index in range(1, capacity + 1):
        path = slot_path(state_dir, index)
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not _slot_is_stale(path):
                    break  # live slot: try the next index
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass  # another runner freed it first
                continue  # retry this index: the slot is (or was) free
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(pid))
            return path
    return None


def release_slot(path: Path) -> None:
    """Remove one slot file; a missing file is not an error."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def hold_slot(path: Path) -> None:
    """Keep a slot until this process exits, however it exits.

    Normal exit releases it via atexit; SIGTERM (systemd stop) and SIGINT
    release it via signal handlers that then restore the default action and
    re-raise the signal, so the process still dies the standard way.
    """
    atexit.register(release_slot, path)
    signal.signal(signal.SIGTERM, _make_signal_handler(path))
    signal.signal(signal.SIGINT, _make_signal_handler(path))


def _make_signal_handler(path: Path):
    def handler(signum: int, frame) -> None:
        release_slot(path)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return handler


def slot_occupancy(state_dir: Path, capacity: int) -> list[tuple[int, int | None]]:
    """Return ``(index, holder_pid)`` per slot; None when the slot is free."""
    state_dir = Path(state_dir)
    occupancy: list[tuple[int, int | None]] = []
    for index in range(1, capacity + 1):
        path = slot_path(state_dir, index)
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            occupancy.append((index, None))
            continue
        occupancy.append((index, int(raw)) if raw.isdigit() else (index, None))
    return occupancy


def _slot_is_stale(path: Path) -> bool:
    """True when the slot's holder is gone and the slot can be taken back."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False  # unreadable: assume a live holder, never steal
    if not raw:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True  # file vanished: the next take will create it
        return age > EMPTY_SLOT_STALE_SECONDS
    if not raw.isdigit():
        return True  # corrupted content: no live holder can own it
    return _pid_is_dead(int(raw))


def _pid_is_dead(pid: int) -> bool:
    """True when no process with this PID is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OverflowError:
        return True  # pid beyond C int range: can never exist
    except PermissionError:
        return False  # a process we cannot signal is still alive
    return False
