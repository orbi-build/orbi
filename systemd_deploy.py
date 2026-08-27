"""Systemd deployment consistency for Muyan Pilot (Issue #103).

The repo templates ``systemd/muyan-pilot.service`` and
``systemd/muyan-pilot.timer`` are the single source of truth for the
user-level units. This module provides:

- an idempotent install (overwrite-copy the templates into the user
  unit directory, ``systemctl --user daemon-reload``, enable the
  timer) that NEVER starts/stops/restarts the service: a currently
  running Runner keeps running, and the new config takes effect at
  the next service start;
- a pre-start consistency check that compares BOTH installed units
  against the templates and fails fast with a structured
  ``unit_drift`` line (repo path, installed path, hashes, fix
  command) when they drift.

No database, queue, daemon or second state store: the installed files
and systemd itself are the only state.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from pi_activity import quote_value

LOGGER = logging.getLogger("muyan_pilot.systemd_deploy")

SERVICE_UNIT = "muyan-pilot.service"
TIMER_UNIT = "muyan-pilot.timer"
UNIT_NAMES = (SERVICE_UNIT, TIMER_UNIT)

# The idempotent install command that repairs any drift (carried on
# every unit_drift line as the fix command). Issue #140: the official
# entry is the installed `muyan-pilot` CLI (the uv-tool console
# script), not a hand-written Python file entry.
FIX_COMMAND = "muyan-pilot install-units"


class UnitDriftError(RuntimeError):
    """The installed units have drifted from the repo templates."""


def repo_unit_dir(repo_dir: Path) -> Path:
    """The deployment checkout's unit template directory."""
    return Path(repo_dir) / "systemd"


def installed_unit_dir(unit_dir: str | None = None) -> Path:
    """The user unit directory of this machine.

    An explicit ``unit_dir`` (or ``$MUYAN_PILOT_UNIT_DIR``, the
    test/e2e seam) wins; then ``$XDG_CONFIG_HOME/systemd/user``; then
    ``~/.config/systemd/user`` (the standard systemd user unit
    location).
    """
    override = unit_dir or os.environ.get("MUYAN_PILOT_UNIT_DIR")
    if override:
        return Path(override).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def sha256_hex(path: Path) -> str:
    """The sha256 of one file's content (the unit's identity)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unit_status(repo_dir: Path, installed_dir: Path) -> list[dict]:
    """Compare the installed units against the repo templates.

    One entry per unit (service and timer, in order): the repo and
    installed paths, both sha256s (None when the file is missing) and
    whether the unit drifted. A missing template or a missing
    installed unit is drift: the deployment is not verifiable.
    """
    repo_dir = Path(repo_dir)
    installed_dir = Path(installed_dir)
    entries: list[dict] = []
    for name in UNIT_NAMES:
        repo_path = repo_unit_dir(repo_dir) / name
        installed_path = installed_dir / name
        repo_sha = sha256_hex(repo_path) if repo_path.is_file() else None
        installed_sha = (
            sha256_hex(installed_path) if installed_path.is_file() else None
        )
        entries.append({
            "unit": name,
            "repo_path": repo_path,
            "installed_path": installed_path,
            "repo_sha256": repo_sha,
            "installed_sha256": installed_sha,
            "missing": installed_sha is None,
            "drifted": (
                repo_sha is None
                or installed_sha is None
                or repo_sha != installed_sha
            ),
        })
    return entries


def drift_lines(status: list[dict]) -> list[str]:
    """One structured ``unit_drift`` line per drifted unit.

    Every line carries the repo path, the installed path, both hashes
    and the idempotent fix command (Issue #103). Values containing
    spaces are quoted (the pi_activity.quote_value convention) so the
    line stays parseable.
    """
    lines: list[str] = []
    for entry in status:
        if not entry["drifted"]:
            continue
        lines.append(
            "unit_drift "
            f"unit={entry['unit']} "
            f"repo={quote_value(str(entry['repo_path']))} "
            f"installed={quote_value(str(entry['installed_path']))} "
            f"repo_sha256={entry['repo_sha256'] or '-'} "
            f"installed_sha256={entry['installed_sha256'] or '-'} "
            f"fix={FIX_COMMAND}"
        )
    return lines


def check_unit_drift(repo_dir: Path,
                     installed_dir: Path | None = None) -> None:
    """Pre-start deployment check (Issue #103).

    Compares BOTH installed units against the repo templates. Clean:
    logs ``unit_drift clean`` and returns. Drift: logs one structured
    ``unit_drift`` line per drifted unit and raises
    ``UnitDriftError`` — the caller fails fast and claims no Issue
    until the units are synced with the idempotent install command.
    """
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    status = unit_status(repo_dir, installed_dir)
    lines = drift_lines(status)
    if not lines:
        LOGGER.info("unit_drift clean installed_dir=%s", installed_dir)
        return
    for line in lines:
        LOGGER.error(line)
    raise UnitDriftError(
        "installed systemd units have drifted from the repo templates; "
        f"sync with: {FIX_COMMAND}\n" + "\n".join(lines)
    )


def install_units(repo_dir: Path, installed_dir: Path | None = None,
                  *, run_command) -> dict:
    """Idempotently install the repo templates as the user units.

    Overwrites BOTH installed units with the repo templates (the repo
    is the single source of truth), runs ``systemctl --user
    daemon-reload`` and enables the timer (``enable --now`` is
    idempotent and activates only the timer, never the service). The
    service is NEVER started, stopped or restarted: a currently
    running Runner keeps running, and the new config takes effect at
    the next service start. Returns the deployed commit (the
    deployment checkout's HEAD) and the installed units' hashes.
    """
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    installed_dir = Path(installed_dir)
    for name in UNIT_NAMES:
        template = repo_unit_dir(repo_dir) / name
        if not template.is_file():
            raise FileNotFoundError(
                f"unit template missing: {template} (the repo "
                "templates are the single source of truth)"
            )
    installed_dir.mkdir(parents=True, exist_ok=True)
    for name in UNIT_NAMES:
        (installed_dir / name).write_bytes(
            (repo_unit_dir(repo_dir) / name).read_bytes(),
        )
    run_command(["systemctl", "--user", "daemon-reload"])
    run_command(["systemctl", "--user", "enable", "--now", TIMER_UNIT])
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    units = {
        name: {
            "installed_path": installed_dir / name,
            "sha256": sha256_hex(installed_dir / name),
        }
        for name in UNIT_NAMES
    }
    LOGGER.info(
        "units_installed commit=%s installed_dir=%s units=%s",
        commit, installed_dir, ",".join(UNIT_NAMES),
    )
    return {
        "commit": commit,
        "installed_dir": installed_dir,
        "units": units,
    }
