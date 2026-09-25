"""The systemd scheduler implementation (Linux).

The repo templates ``systemd/orbi@.service`` and
``systemd/orbi@.timer`` are the single source of truth for the
user-level units on Linux. This module is the systemd member of the
scheduler layer (:mod:`orbi.scheduler` owns the interface, the platform
dispatch and the platform-independent orchestration); every
``systemctl``/``journalctl`` literal in ``src/orbi/`` lives here.

Platform hooks (see :class:`orbi.scheduler.Scheduler`):

- an idempotent install (copy the templates into the user unit
  directory after confirming an existing deployment uses the same
  config, ``systemctl --user daemon-reload``, enable timer
  instances through the configured ``max_concurrency`` and disable
  surplus timers) that NEVER starts/stops/restarts the service: a currently running
  Runner keeps running, and the new config takes effect at the next
  service start. The install also migrates the pre-#149
  non-templated units away once (stop the legacy timer — a timer stop
  never touches the service — and remove the legacy files), so the
  old single-instance schedule cannot keep firing the old service;
- a pre-start consistency check (the layer's ``check_unit_drift``
  running on this class's raw-byte content identity) that fails fast
  with a structured ``unit_drift`` line when the installed units
  drift from the templates.

No database, queue, daemon or second state store: the installed files
and systemd itself are the only state.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from orbi.scheduler import (
    MAX_RUNNER_INSTANCES,
    REPO_DIR_PLACEHOLDER,
    instance_schedule,
    service_instances,
    timer_instances,
    unit_names,
)
from orbi.journal import event

SERVICE_UNIT = "orbi@.service"
TIMER_UNIT = "orbi@.timer"
UNIT_NAMES = (SERVICE_UNIT, TIMER_UNIT)
# The pre-#149 non-templated units: the install migrates them away
# once (a template change is a deployment change, no human step).
LEGACY_TIMER_UNIT = "orbi.timer"
LEGACY_UNIT_NAMES = ("orbi.service", "orbi.timer")


def repo_unit_dir(repo_dir: Path) -> Path:
    """The deployment checkout's unit template directory."""
    return Path(repo_dir) / "systemd"


def installed_unit_dir(unit_dir: str | None = None) -> Path:
    """The user unit directory of this machine.

    An explicit ``unit_dir`` (or ``$ORBI_UNIT_DIR``, the
    test/e2e seam) wins; then ``$XDG_CONFIG_HOME/systemd/user``; then
    ``~/.config/systemd/user`` (the standard systemd user unit
    location).
    """
    override = unit_dir or os.environ.get("ORBI_UNIT_DIR")
    if override:
        return Path(override).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def sha256_hex(path: Path) -> str:
    """The sha256 of one file's content (the unit's identity)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_unit_template(template_text: str, repo_dir: Path,
                         unit_name: str | None = None) -> str:
    """Render a template for the deployment's installed unit name."""
    rendered = template_text.replace(
        REPO_DIR_PLACEHOLDER, str(Path(repo_dir).resolve()),
    )
    if unit_name is not None:
        rendered = rendered.replace(
            "orbi@%i.service", f"orbi-{unit_name}@%i.service",
        )
    return rendered


def installed_config(unit_path: Path) -> Path | None:
    """Return the ORBI_CONFIG value from an installed service unit."""
    if not unit_path.is_file():
        return None
    text = unit_path.read_text(encoding="utf-8")
    match = re.search(
        r"\bORBI_CONFIG=(?:\"([^\"]+)\"|([^\s\"]+))", text,
    )
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    # Older templates used systemd's %h specifier in ORBI_CONFIG.  Resolve
    # it before comparing with the absolute path used by current templates;
    # otherwise a reinstall of the same deployment is mistaken for a conflict.
    value = value.replace("%h", str(Path.home()))
    return Path(value).expanduser().resolve()


def unmanaged_units(installed_dir: Path) -> list[dict]:
    """List the orbi unit files no deployment's drift check can see.

    Every managed orbi unit is an installed template (``orbi@.service``,
    ``orbi-<unit_name>@.service``, and their ``@`` instances):
    the layer's drift check compares exactly those names. A hand-written
    orbi unit WITHOUT the ``@`` template form (``orbi-core.service``,
    the pre-#149 ``orbi.timer``, ...) is invisible to every
    ``unit_names()`` set — it never drift-checks and never self-heals.
    One entry per such file, sorted by name: the unit
    name and the ``ORBI_CONFIG`` the unit points at (``None`` when the
    file carries none — timers never do). A missing unit dir has
    nothing to scan (the missing managed units are ``unit_drift``'s
    report, not this one).
    """
    installed_dir = Path(installed_dir)
    if not installed_dir.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(installed_dir.iterdir(), key=lambda p: p.name):
        name = path.name
        if not (
            path.is_file()
            and name.startswith("orbi")
            and (name.endswith(".service") or name.endswith(".timer"))
        ):
            continue
        if "@" in name:
            continue
        entries.append({"unit": name, "config": installed_config(path)})
    return entries


def timer_next_trigger(list_timers_output: str, unit_name: str) -> str:
    """The NEXT column of the given timer instance's row, or ``-``.

    ``systemctl --user list-timers --no-pager`` prints a header line
    (``NEXT  LEFT ...``) followed by one row per timer; the row whose
    UNIT column is the given instance (e.g. ``orbi@1.timer``)
    carries the next trigger time.
    """
    for line in list_timers_output.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[-2] == unit_name:
            # NEXT is the first fixed-width column and itself contains
            # spaces ("Thu 2026-08-27 10:00:00 +08"): it ends where the
            # all-whitespace column separator begins, so take the line
            # up to the first run of two or more spaces.
            match = re.match(r"^(\S+(?: \S+)*?)  ", line)
            if match:
                return match.group(1)
            return columns[0]
    return "-"


def migrate_legacy_units(installed_dir: Path, *, run_command) -> bool:
    """One-time migration away from the pre-#149 non-templated units.

    Returns True when a legacy timer unit file was present (and
    migrated), False when there was nothing to migrate (a fresh
    install or an already-migrated machine — idempotent).

    The legacy ``orbi.timer`` is stopped with ``disable --now``
    (a TIMER stop: it never starts, stops or restarts the SERVICE — a
    currently running Runner keeps running) and the legacy
    ``orbi.service``/``orbi.timer`` files are removed,
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
    event(
        "legacy_units_migrated", installed_dir=installed_dir,
        removed=",".join(LEGACY_UNIT_NAMES),
    )
    return True


def stagger_dropin(installed_dir: Path, timer: str) -> Path:
    """The per-instance stagger drop-in path for one timer instance."""
    return Path(installed_dir) / f"{timer}.d" / "stagger.conf"


def stagger_dropin_text(instance: int) -> str:
    """The exact stagger.conf content for one runner instance.

    A bare ``OnCalendar=`` line resets the inherited list, so the
    drop-in REPLACES the template's schedule instead of appending to
    it (systemd drop-in semantics). The content depends on the
    instance index alone, so co-located deployments write the same
    bytes (Issue #1362).
    """
    return (
        "[Timer]\nOnCalendar=\n"
        f"OnCalendar={instance_schedule(instance)}\n"
    )


def parse_oncalendar(text: str) -> str | None:
    """The effective ``OnCalendar`` value of unit text (or ``None``).

    Systemd applies the LAST setting, so the last non-empty
    ``OnCalendar=`` line wins (a bare ``OnCalendar=`` clears the list
    and carries no value).
    """
    value: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("OnCalendar="):
            setting = stripped[len("OnCalendar="):]
            if setting:
                value = setting
    return value


def sync_stagger_dropins(installed_dir: Path, unit_name: str | None = None,
                         *, max_concurrency: int) -> None:
    """Manage THIS deployment's timer stagger drop-ins.

    Instances 2..max_concurrency get a stagger.conf drop-in with the
    instance's own OnCalendar; instance 1 keeps the template value, so
    a stray drop-in under ``orbi@1.timer.d`` is removed. Instances
    above ``max_concurrency`` are NOT touched: they belong to a
    co-located deployment sharing the unit directory (Issue #1362 —
    removing them is what made two deployments ping-pong forever). A
    drop-in is only rewritten when its content differs, so a
    converged round touches nothing on disk.
    """
    instances = timer_instances(unit_name, MAX_RUNNER_INSTANCES)
    # Write (idempotently) the drop-ins for instances 2..max_concurrency.
    for idx in range(2, max_concurrency + 1):
        instance = instances[idx - 1]
        dropin = stagger_dropin(installed_dir, instance)
        text = stagger_dropin_text(idx)
        if dropin.is_file() and dropin.read_text(encoding="utf-8") == text:
            continue
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(text, encoding="utf-8")

    # Instance 1 keeps the template value: remove a stray drop-in.
    first = instances[0]
    dropin_dir = installed_dir / f"{first}.d"
    dropin_file = stagger_dropin(installed_dir, first)
    if dropin_file.is_file():
        dropin_file.unlink()
    if dropin_dir.is_dir() and not any(dropin_dir.iterdir()):
        dropin_dir.rmdir()


class SystemdScheduler:
    """The Linux member of the scheduler layer (systemd user units).

    The bare ``timer_instances``/``service_instances`` names used in
    the methods below resolve to the MODULE functions (a class
    namespace is not in the lookup chain of its methods' bodies).
    """

    name = "systemd"
    display = "systemd"
    template_dir = "systemd"

    def template_units(self) -> tuple[str, ...]:
        return UNIT_NAMES

    def unit_pairs(self, unit_name: str | None,
                   count: int) -> list[tuple[str, str]]:
        # systemd ships TWO count-independent template files; the
        # instances are systemd-side instantiations of them.
        return list(zip(UNIT_NAMES, unit_names(unit_name)))

    def timer_instances(self, unit_name: str | None = None,
                        count: int = 1) -> tuple[str, ...]:
        return timer_instances(unit_name, count)

    def service_instances(self, unit_name: str | None = None,
                          count: int = 1) -> tuple[str, ...]:
        return service_instances(unit_name, count)

    def installed_unit_dir(self, override: str | None = None) -> Path:
        return installed_unit_dir(override)

    def render_unit(self, template_text: str, repo_dir: Path,
                    unit_name: str | None = None,
                    instance: int = 1,
                    max_concurrency: int = 1) -> str:
        # systemd instantiates instances from one template (%i); the
        # install renders the same text for every instance.
        return render_unit_template(template_text, repo_dir, unit_name)

    def content_sha(self, rendered: str) -> str:
        # The systemd drift identity is the rendered text's raw bytes.
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def installed_sha(self, path: Path) -> str | None:
        path = Path(path)
        return sha256_hex(path) if path.is_file() else None

    def unit_config(self, path: Path) -> Path | None:
        return installed_config(path)

    def probe_args(self, unit_name: str | None = None) -> list[str]:
        """The `systemctl --user` probe command proving the user bus.

        Probes an INSTANCE name (verified against the real CLI: `systemctl
        show` rejects the bare template name `orbi@.timer` but accepts
        instance names, exiting 0 with `not-found` before the units are
        installed — the probe only needs the user bus).
        """
        return [
            "systemctl", "--user", "show", "-p", "LoadState", "--value",
            timer_instances(unit_name, 1)[0],
        ]

    def unit_enabled(self, run_command, instance: str) -> bool:
        """Whether the systemd unit is enabled.

        ``systemctl --user is-enabled`` exits non-zero with the state word on
        stdout (``disabled``, ``masked``, ...) for a unit that is NOT enabled —
        that non-zero exit is documented systemd behavior, not a command
        failure. A genuine systemctl failure (no user bus: empty
        stdout, error on stderr) re-raises so setup fails fast.
        """
        try:
            state = run_command(["systemctl", "--user", "is-enabled", instance])
        except subprocess.CalledProcessError as exc:
            state = (exc.stdout or "").strip()
            if state not in ("disabled", "masked", "static", "indirect"):
                raise
        return state == "enabled"

    def unit_state(self, run_command, instance: str) -> str:
        """The instance's ActiveState via `systemctl --user show`."""
        return run_command([
            "systemctl", "--user", "show", "-p", "ActiveState",
            "--value", instance,
        ])

    def schedule_text(self, instance: int, max_concurrency: int) -> str:
        """The ``OnCalendar`` spelling of the deployed instance schedule.

        The offset depends on the instance index alone: deployments
        sharing a unit directory agree on it (Issue #1362).
        """
        return instance_schedule(instance)

    def extra_drift(self, installed_dir: Path, unit_name: str | None,
                    max_concurrency: int) -> list[dict]:
        """The stagger drop-ins the drift check must also compare.

        The expected set for ``max_concurrency`` is one
        ``orbi@<i>.timer.d/stagger.conf`` for instances 2..N with the
        exact :func:`stagger_dropin_text` content, and a clean
        instance 1 (it keeps the template value). A surplus instance
        (index > ``max_concurrency``) is NOT part of this
        deployment's set — the drop-in may belong to a co-located
        deployment sharing the unit directory, so flagging it as
        drift is what started the ping-pong (Issue #1362).
        """
        installed_dir = Path(installed_dir)
        expected = {
            index: stagger_dropin_text(index)
            for index in range(2, max_concurrency + 1)
        }
        entries: list[dict] = []
        for index, timer in enumerate(
            timer_instances(unit_name, MAX_RUNNER_INSTANCES), start=1,
        ):
            if index > max_concurrency:
                # Owned by another deployment: not this one's drift.
                continue
            installed_path = stagger_dropin(installed_dir, timer)
            installed_sha = self.installed_sha(installed_path)
            wanted = expected.get(index)
            if wanted is None and installed_sha is None:
                # No drop-in is expected here and none is installed.
                continue
            repo_sha = self.content_sha(wanted) if wanted is not None else None
            entries.append({
                "unit": f"{timer}.d/stagger.conf",
                # The drop-in is generated, not a repo template: the
                # expected file is named by its unit-relative path.
                "repo_path": Path(f"{timer}.d") / "stagger.conf",
                "installed_path": installed_path,
                "repo_sha256": repo_sha,
                "installed_sha256": installed_sha,
                "missing": installed_sha is None,
                "drifted": (
                    repo_sha is None
                    or installed_sha is None
                    or repo_sha != installed_sha
                ),
                "reload_pending": False,
            })
        return entries

    def installed_schedule(self, installed_dir: Path, unit_name: str | None,
                           instance: int,
                           max_concurrency: int) -> str | None:
        """The instance's OnCalendar as INSTALLED on disk (Issue #1344).

        The drop-in value wins for instances 2..N; instance 1 (and an
        instance whose drop-in vanished) falls back to the installed
        template, so the report never echoes the computed schedule as
        if it were deployed. ``None`` means the installed unit cannot
        be read at all.
        """
        installed_dir = Path(installed_dir)
        timer = timer_instances(unit_name, instance)[-1]
        dropin = stagger_dropin(installed_dir, timer)
        if dropin.is_file():
            value = parse_oncalendar(dropin.read_text(encoding="utf-8"))
            if value is not None:
                return value
        template = installed_dir / unit_names(unit_name)[1]
        if template.is_file():
            return parse_oncalendar(template.read_text(encoding="utf-8"))
        return None

    def instances_status(self, run_command, unit_name: str | None = None,
                         *, max_concurrency: int) -> dict[str, dict]:
        """One report entry per configured timer instance.

        The enabled state (``is-enabled``), the active state
        (``show -p ActiveState``), the next trigger time
        (``list-timers``, read once), and the instance schedule
        (``OnCalendar`` spelling).
        """
        list_timers = run_command([
            "systemctl", "--user", "list-timers", "--no-pager",
        ])
        instances: dict[str, dict] = {}
        for index, instance in enumerate(timer_instances(unit_name, max_concurrency), start=1):
            instances[instance] = {
                "enabled": self.unit_enabled(run_command, instance),
                "active": self.unit_state(run_command, instance) == "active",
                "next": timer_next_trigger(list_timers, instance),
                "schedule": self.schedule_text(index, max_concurrency),
            }
        return instances

    def journal_lines(self, run_command, repo_dir: Path | None = None,
                      unit_name: str | None = None, *,
                      max_concurrency: int,
                      since_minutes: int | None = None,
                      lines: int | None = None) -> list[str]:
        """One bounded `journalctl` query per service instance.

        ``since_minutes`` bounds the window (the crash scan);
        ``lines`` bounds the tail (the doctor report). ``repo_dir``
        is the file-backed implementations' anchor; journalctl needs
        nothing from it.
        """
        collected: list[str] = []
        for unit in service_instances(unit_name, max_concurrency):
            command = ["timeout", "30", "journalctl", "--user", "-u", unit]
            if since_minutes is not None:
                command += ["--since", f"-{since_minutes}min"]
            if lines is not None:
                command += ["-n", str(lines)]
            output = run_command(command + ["--no-pager", "-q"])
            collected.extend(output.splitlines())
        return collected

    def restart_hint(self, unit_name: str | None = None) -> str:
        return f"systemctl --user start {service_instances(unit_name, 1)[0]}"

    def reload_pending(self, installed_dir: Path, name: str) -> bool:
        # systemd has no deferred reload: the daemon-reload below makes
        # the new unit effective at the next service start, so there is
        # never a running instance holding a stale unit file.
        return False

    def activate_instances(self, run_command, installed_dir: Path,
                           unit_name: str | None = None, *,
                           max_concurrency: int,
                           changed: frozenset[str] = frozenset(),
                           enable: bool = True) -> None:
        """daemon-reload, then converge the timer instances.

        Enables instances through ``max_concurrency`` and disables the
        surplus up to ``MAX_RUNNER_INSTANCES`` (Issue #827: the whole
        1..MAX universe converges onto the configured capacity, so a
        downscale disables the surplus instance). These operations
        activate or stop only timers, never services. The services are
        NEVER started, stopped or restarted: a currently running
        Runner keeps running, and the new config takes effect at the
        next service start.

        ``enable=False`` is the pre-start self-heal (Issue #1362): the
        drop-ins are synced and the daemon reloaded, but no
        ``enable``/``disable`` runs — a timer the operator disabled
        stays disabled instead of being re-enabled (and started) by
        the self-heal.
        """
        sync_stagger_dropins(installed_dir, unit_name, max_concurrency=max_concurrency)
        run_command(["systemctl", "--user", "daemon-reload"])
        if not enable:
            return
        instances = timer_instances(unit_name, MAX_RUNNER_INSTANCES)
        for instance in instances[:max_concurrency]:
            run_command(["systemctl", "--user", "enable", "--now", instance])
        for instance in instances[max_concurrency:]:
            run_command(["systemctl", "--user", "disable", "--now", instance])

    def pre_install(self, run_command, installed_dir: Path,
                    unit_name: str | None = None) -> None:
        # The pre-#149 legacy units belong to the default deployment only.
        if unit_name is None:
            migrate_legacy_units(installed_dir, run_command=run_command)

    def unmanaged_entries(self, installed_dir: Path) -> list[dict]:
        return unmanaged_units(installed_dir)
