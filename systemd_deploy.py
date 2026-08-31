"""Systemd deployment consistency for Muyan Pilot (Issue #103, #149).

The repo templates ``systemd/muyan-pilot@.service`` and
``systemd/muyan-pilot@.timer`` are the single source of truth for the
user-level units. This module provides:

- an idempotent install (overwrite-copy the templates into the user
  unit directory, ``systemctl --user daemon-reload``, enable timer
  instances through the configured ``max_concurrency`` and disable
  surplus timers) that NEVER starts/stops/restarts the service: a currently running
  Runner keeps running, and the new config takes effect at the next
  service start. The install also migrates the pre-#149
  non-templated units away once (stop the legacy timer — a timer stop
  never touches the service — and remove the legacy files), so the
  old single-instance schedule cannot keep firing the old service;
- a pre-start consistency check that compares BOTH installed template
  units against the templates and fails fast with a structured
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

SERVICE_UNIT = "muyan-pilot@.service"
TIMER_UNIT = "muyan-pilot@.timer"
UNIT_NAMES = (SERVICE_UNIT, TIMER_UNIT)
# Issue #149: the two enabled timer instances. Each instance triggers
# its own service instance (muyan-pilot@1.timer ->
# muyan-pilot@1.service, ...@2 -> ...@2), so two independent Runner
# instances can run concurrently; the capacity is still the flock
# slots in the Runner (max_concurrency), never the instance count.
TIMER_INSTANCES = ("muyan-pilot@1.timer", "muyan-pilot@2.timer")
SERVICE_INSTANCES = (
    "muyan-pilot@1.service", "muyan-pilot@2.service",
)
# The pre-#149 non-templated units: install_units migrates them away
# once (a template change is a deployment change, no human step).
LEGACY_TIMER_UNIT = "muyan-pilot.timer"
LEGACY_UNIT_NAMES = ("muyan-pilot.service", "muyan-pilot.timer")

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


def sync_drifted_units(repo_dir: Path,
                       installed_dir: Path | None = None,
                       *, max_concurrency: int = len(TIMER_INSTANCES),
                       run_command) -> list[dict]:
    """Pre-start self-heal for drifted units (Issue #142).

    The normal scene: a template change merged to main, the
    ExecStartPre-synced checkout carries the new templates, and the
    installed units are still the old ones. Runs the SAME idempotent
    install (``install_units``: copy the templates, daemon-reload,
    sync the configured timer instances — never start/stop/restart the service, so a
    currently running Runner is untouched) and re-verifies with the
    SAME hash check (``unit_status``). Clean after the sync: logs one
    structured ``unit_drift auto_synced`` line per unit (unit,
    before/after sha256, deployed commit) and returns the per-unit
    report. Still drifted after the sync: logs the structured
    ``unit_drift`` lines and raises ``UnitDriftError`` (fail fast —
    the caller claims no Issue). A failing install step propagates
    unchanged. No drift: returns ``[]`` without touching anything.
    """
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    installed_dir = Path(installed_dir)
    before = unit_status(repo_dir, installed_dir)
    if not any(entry["drifted"] for entry in before):
        return []
    result = install_units(
        repo_dir, installed_dir, max_concurrency=max_concurrency,
        run_command=run_command,
    )
    after = unit_status(repo_dir, installed_dir)
    lines = drift_lines(after)
    if lines:
        for line in lines:
            LOGGER.error(line)
        raise UnitDriftError(
            "installed systemd units still drift after the pre-start "
            f"sync; sync with: {FIX_COMMAND}\n" + "\n".join(lines)
        )
    report: list[dict] = []
    for entry_before, entry_after in zip(before, after):
        LOGGER.info(
            "unit_drift auto_synced unit=%s "
            "before_sha256=%s after_sha256=%s commit=%s",
            entry_after["unit"],
            entry_before["installed_sha256"] or "-",
            entry_after["installed_sha256"],
            result["commit"],
        )
        report.append({
            "unit": entry_after["unit"],
            "before_sha256": entry_before["installed_sha256"],
            "after_sha256": entry_after["installed_sha256"],
            "commit": result["commit"],
        })
    return report


def migrate_legacy_units(installed_dir: Path, *, run_command) -> bool:
    """One-time migration away from the pre-#149 non-templated units.

    Returns True when a legacy timer unit file was present (and
    migrated), False when there was nothing to migrate (a fresh
    install or an already-migrated machine — idempotent).

    The legacy ``muyan-pilot.timer`` is stopped with ``disable --now``
    (a TIMER stop: it never starts, stops or restarts the SERVICE — a
    currently running Runner keeps running) and the legacy
    ``muyan-pilot.service``/``muyan-pilot.timer`` files are removed,
    so the old single-instance schedule cannot keep firing the old
    service (a third Runner without the ExecStartPre flock, Issue
    #149). A failing step propagates unchanged (fail fast).
    """
    if not (installed_dir / LEGACY_TIMER_UNIT).is_file():
        return False
    run_command([
        "systemctl", "--user", "disable", "--now", LEGACY_TIMER_UNIT,
    ])
    for name in LEGACY_UNIT_NAMES:
        legacy = installed_dir / name
        if legacy.is_file():
            legacy.unlink()
    LOGGER.info(
        "legacy_units_migrated installed_dir=%s removed=%s",
        installed_dir, ",".join(LEGACY_UNIT_NAMES),
    )
    return True


def install_units(repo_dir: Path, installed_dir: Path | None = None,
                  *, max_concurrency: int = len(TIMER_INSTANCES),
                  run_command) -> dict:
    """Idempotently install the repo templates as the user units.

    Overwrites BOTH installed template units with the repo templates
    (the repo is the single source of truth), migrates the pre-#149
    non-templated units away once (see ``migrate_legacy_units``), runs
    ``systemctl --user daemon-reload``, enables instances through
    ``max_concurrency`` and disables surplus timers. These operations
    activate or stop only timers, never services. The services are NEVER started,
    stopped or restarted: a currently running Runner keeps running,
    and the new config takes effect at the next service start.
    Returns the deployed commit (the deployment checkout's HEAD) and
    the installed units' hashes.
    """
    if not 1 <= max_concurrency <= len(TIMER_INSTANCES):
        raise ValueError(
            "max_concurrency must have a matching Runner timer instance "
            f"(1..{len(TIMER_INSTANCES)})"
        )
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
    migrate_legacy_units(installed_dir, run_command=run_command)
    for name in UNIT_NAMES:
        (installed_dir / name).write_bytes(
            (repo_unit_dir(repo_dir) / name).read_bytes(),
        )
    run_command(["systemctl", "--user", "daemon-reload"])
    for instance in TIMER_INSTANCES[:max_concurrency]:
        run_command(["systemctl", "--user", "enable", "--now", instance])
    for instance in TIMER_INSTANCES[max_concurrency:]:
        run_command(["systemctl", "--user", "disable", "--now", instance])
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    units = {
        name: {
            "installed_path": installed_dir / name,
            "sha256": sha256_hex(installed_dir / name),
        }
        for name in UNIT_NAMES
    }
    LOGGER.info(
        "units_installed commit=%s installed_dir=%s units=%s "
        "instances=%s",
        commit, installed_dir, ",".join(UNIT_NAMES),
        ",".join(TIMER_INSTANCES[:max_concurrency]),
    )
    return {
        "commit": commit,
        "installed_dir": installed_dir,
        "units": units,
    }
