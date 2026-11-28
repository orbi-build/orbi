"""The installed unit files' on-disk contract (Issue #2455).

The scheduler install (:func:`orbi.scheduler.install_units`) renders the
repo templates into the platform's unit directory. This leaf owns what
that installed file must BE on disk, independent of the caller:

- the explicit mode the install writes (`INSTALLED_UNIT_MODE`): the
  invoking shell's `umask` must never decide a unit's mode. Kit's fleet
  refresh runs the engine with `umask 002` and refuses group- or
  other-writable units before changing scheduling
  (TrebuchetDynamics/kit#354);
- the mode part of the drift decision: identical content that any group
  member (or any user) can edit is not a trusted scheduler input, so the
  SAME idempotent install — pre-start self-heal included — normalizes it.
  Only the managed (template, installed) pairs are examined or written;
  no unrelated file's permissions are read as drift or changed;
- the one `unit_drift` field set the journal event and the failure
  message line share.

This module imports nothing from `orbi`: it is a leaf of the scheduler
layer, like :mod:`orbi.scheduler_units`.
"""
from __future__ import annotations

import os
from pathlib import Path

# The explicit mode of every installed managed unit: owner-writable,
# everyone-readable — the conventional systemd/launchd unit mode, and
# never group- or other-writable.
INSTALLED_UNIT_MODE = 0o644

# The write bits that make an installed unit untrusted: anyone in the
# group, or anyone at all, could edit what the scheduler loads.
UNSAFE_UNIT_MODE_BITS = 0o022


def write_unit(path: Path, text: str) -> None:
    """Write one installed unit with the explicit installed mode.

    `write_bytes` creates the file with `0666 & ~umask`, so on its own
    it inherits the caller's umask (`0664` under `umask 002`). The chmod
    is umask-independent: it sets the safe mode on a new file and
    normalizes a legacy mode on the same write.
    """
    path = Path(path)
    path.write_bytes(text.encode("utf-8"))
    os.chmod(path, INSTALLED_UNIT_MODE)


def installed_mode(path: Path) -> int | None:
    """The installed unit's permission bits, or None when it is missing."""
    path = Path(path)
    return path.stat().st_mode & 0o777 if path.is_file() else None


def mode_field(path: Path) -> str:
    """The `mode=` value of a structured unit line: octal, "-" if missing."""
    mode = installed_mode(path)
    return "-" if mode is None else f"{mode:04o}"


def unit_drifted(repo_sha: str | None, installed_sha: str | None,
                 installed_path: Path) -> bool:
    """The whole drift decision for one managed unit.

    A missing template (`repo_sha` None), a missing installed unit
    (`installed_sha` None), a different content identity, or an installed
    mode that lets someone else edit the unit (Issue #2455) is drift: the
    deployment is not verifiable as the repo's.
    """
    mode = installed_mode(installed_path)
    return (
        repo_sha is None
        or installed_sha is None
        or repo_sha != installed_sha
        or (mode is not None and bool(mode & UNSAFE_UNIT_MODE_BITS))
    )


def drift_fields(entry: dict) -> dict[str, str]:
    """The one `unit_drift` field set (journal event and message line).

    `entry` is a :func:`orbi.scheduler.unit_status` entry. The values
    are raw: the journal event quotes them itself and
    :func:`orbi.scheduler.drift_lines` quotes them with
    `journal.quote_value` so the line stays parseable. The fix command is
    not a field here — both callers append it as the trailing
    `fix=<command>` (never quoted: it is a command).
    """
    return {
        "unit": entry["unit"],
        "repo": str(entry["repo_path"]),
        "installed": str(entry["installed_path"]),
        "repo_sha256": entry["repo_sha256"] or "-",
        "installed_sha256": entry["installed_sha256"] or "-",
        "mode": mode_field(entry["installed_path"]),
    }
