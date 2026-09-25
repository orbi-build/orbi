"""The launchd scheduler implementation (macOS).

The macOS member of the scheduler layer (:mod:`orbi.scheduler` owns
the interface, the platform dispatch and the platform-independent
orchestration); every ``launchctl`` literal in ``src/orbi/`` lives
here. One agent plist per Runner instance — the plist IS the
``.service`` and the ``.timer`` in one. A single instance keeps
``StartInterval`` (the five-minute cadence, counted from the job's load
time); at ``max_concurrency > 1`` every instance switches to
``StartCalendarInterval`` on the wall clock, shifted by its whole-minute
offset, so the instances are staggered instead of firing together
(Issue #1320). The templates
live in ``launchd/`` beside ``systemd/``.

Command contract (modern launchctl(1) / launchd.plist(5), the
``gui/<uid>`` user domain):

- ``launchctl enable gui/<uid>/<label>`` then ``launchctl bootstrap
  gui/<uid> <plist>`` — enable FIRST: a stale record in the persistent
  disabled database makes bootstrap fail; bootstrap loads the plist
  into the logged-in GUI domain (the ``load`` successor);
- ``launchctl disable gui/<uid>/<label>`` — persists across logins
  (unlike ``bootout`` alone, which launchd undoes at the next login);
- ``launchctl bootout gui/<uid>/<label>`` — unload. It KILLS a running
  job, so it is only issued for idle instances: an active Runner is
  never stopped or restarted (the systemd install contract). A loaded
  instance whose plist was just rewritten is booted out and
  re-bootstrapped ONLY when idle (the daemon-reload equivalent); a
  running instance keeps the old config until it finishes, and the
  pending reload is RECORDED (Issue #1347): a ``.reload-pending``
  marker beside its plist — see ``reload_pending`` — makes the drift
  check flag it, so the next idle tick reloads it instead of keeping
  the stale schedule forever;
- ``launchctl print gui/<uid>/<label>`` — state queries; exit 113
  ``Could not find service`` is the documented not-loaded answer;
- ``launchctl print-disabled gui/<uid>`` — the persistent disabled
  records (the ``is-enabled`` equivalent);
- there is no next-fire introspection: the reports carry ``-``,
  honestly.

The drift identity is the CANONICAL plist: both sides are parsed and
re-serialized with sorted keys before hashing, so whitespace and key
order in the installed XML never fabricate drift (a raw textual
comparison would).

There is no launchctl command that makes a LOADED job re-read its
plist (Issue #1347): ``bootstrap`` loads a plist, ``bootout`` unloads
and kills a running job, and ``kickstart -k`` restarts the process
without re-reading the plist. The deferred reload is therefore carried
by the ``.reload-pending`` marker beside the plist (launchd ignores
sibling non-plist files).

Verified limits (no macOS runner in CI): the command sequences and
plist shape follow the man pages and are unit-tested here; a real-Mac
smoke run is the remaining acceptance step before relying on it.
Crash-loop detection reads the systemd journal line shapes and is
therefore inert on macOS — the runner output lands in
``<deploy_home>/.orbi/<label>.log`` for human inspection instead.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
from pathlib import Path
from xml.parsers.expat import ExpatError

from orbi.scheduler import (
    MAX_RUNNER_INSTANCES,
    REPO_DIR_PLACEHOLDER,
    USER_HOME_PLACEHOLDER,
    instance_offset_seconds,
)

TEMPLATE_NAME = "org.orbi.runner.plist"
LABEL_BASE = "org.orbi.runner"
LOG_TAIL_LINES = 400
# ``<plist name>.reload-pending`` beside an installed plist records that
# the RUNNING instance still holds the plist it was loaded with, so the
# drift check flags it and the next idle tick reloads it (Issue #1347).
RELOAD_PENDING_SUFFIX = ".reload-pending"


def calendar_minute_offset(instance: int, max_concurrency: int) -> int:
    """The instance's offset in whole minutes inside the 5-minute tick.

    ``StartCalendarInterval`` has whole-minute granularity:
    launchd.plist(5) documents exactly Minute, Hour, Day, Weekday and
    Month, and launchd's parser (``calendarinterval_new_from_obj_dict
    _walk``) reads those five only — an undocumented ``Second`` key is
    silently ignored, so a sub-minute offset cannot be expressed (the
    run then lands on the grid minute at second 0). The deterministic
    offset therefore truncates: N = 2 spreads instance 2 by 2 minutes.
    """
    return instance_offset_seconds(instance, max_concurrency) // 60


def calendar_interval_entries(instance: int,
                              max_concurrency: int) -> list[dict[str, int]]:
    """StartCalendarInterval entries on the wall-clock 5-minute grid.

    One entry per grid minute (a bare ``{"Minute": m}`` fires every
    hour at minute m), shifted by the instance's whole-minute offset.
    """
    offset = calendar_minute_offset(instance, max_concurrency)
    return [{"Minute": (base + offset) % 60} for base in range(0, 60, 5)]


def calendar_schedule(instance: int, max_concurrency: int) -> str:
    """The instance's launchd schedule in the report's calendar spelling."""
    return f"*-*-* *:{calendar_minute_offset(instance, max_concurrency):02d}/5"


def label_base(unit_name: str | None = None) -> str:
    return LABEL_BASE if unit_name is None else f"org.orbi.{unit_name}.runner"


def label_for(unit_name: str | None, instance: int) -> str:
    return f"{label_base(unit_name)}.{instance}"


def plist_name(label: str) -> str:
    return f"{label}.plist"


def log_path(repo_dir: Path, label: str) -> Path:
    """Where the rendered StandardOutPath puts one instance's output.

    Must stay consistent with the template's
    ``{{ORBI_REPO_DIR}}/.orbi/{{ORBI_LABEL}}.log``.
    """
    return Path(repo_dir) / ".orbi" / f"{label}.log"


def _normalized(value):
    """Sort dict keys recursively so re-serialization is canonical."""
    if isinstance(value, dict):
        return {key: _normalized(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def _canonical_bytes(data: bytes) -> bytes | None:
    """The canonical plist form of ``data`` (None when unparseable).

    plistlib documents ValueError, but a truncated document leaks the
    underlying ExpatError — both mean "not a plist we can compare".
    """
    try:
        parsed = plistlib.loads(data)
    except (ValueError, ExpatError):
        return None
    return plistlib.dumps(_normalized(parsed), fmt=plistlib.FMT_XML)


class LaunchdScheduler:
    """The macOS member of the scheduler layer (launchd agents)."""

    name = "launchd"
    display = "launchd"
    template_dir = "launchd"

    def template_units(self) -> tuple[str, ...]:
        return (TEMPLATE_NAME,)

    def unit_pairs(self, unit_name: str | None,
                   count: int) -> list[tuple[str, str]]:
        return [
            (TEMPLATE_NAME, plist_name(label_for(unit_name, instance)))
            for instance in range(1, count + 1)
        ]

    def timer_instances(self, unit_name: str | None = None,
                        count: int = 1) -> tuple[str, ...]:
        # On launchd ONE label is both the schedule and the process.
        return tuple(
            label_for(unit_name, instance)
            for instance in range(1, count + 1)
        )

    def service_instances(self, unit_name: str | None = None,
                          count: int = 1) -> tuple[str, ...]:
        return self.timer_instances(unit_name, count)

    def installed_unit_dir(self, override: str | None = None) -> Path:
        override = override or os.environ.get("ORBI_UNIT_DIR")
        if override:
            return Path(override).expanduser()
        return Path.home() / "Library" / "LaunchAgents"

    def render_unit(self, template_text: str, repo_dir: Path,
                    unit_name: str | None = None,
                    instance: int = 1,
                    max_concurrency: int = 1) -> str:
        # launchd expands nothing (no ~, no %h): the render substitutes
        # every machine-specific value as an absolute path.
        rendered = (
            template_text
            .replace(REPO_DIR_PLACEHOLDER, str(Path(repo_dir).resolve()))
            .replace(USER_HOME_PLACEHOLDER, str(Path.home()))
            .replace("{{ORBI_LABEL}}", label_for(unit_name, instance))
        )
        if max_concurrency > 1:
            # Issue #1320: StartInterval counts from the job's load
            # time, so instances bootstrapped together fire in the SAME
            # second. With more than one instance the whole deployment
            # moves to the wall clock: instance 1 sits on the
            # ``*:00/5`` grid and instance i is shifted by its
            # deterministic offset (truncated to whole minutes — see
            # calendar_minute_offset), which is the offset the systemd
            # drop-in carries too. A single instance keeps the template's
            # StartInterval: nothing to stagger, nothing changes.
            parsed = plistlib.loads(rendered.encode("utf-8"))
            parsed.pop("StartInterval", None)
            parsed["StartCalendarInterval"] = calendar_interval_entries(
                instance, max_concurrency,
            )
            return plistlib.dumps(parsed, fmt=plistlib.FMT_XML).decode("utf-8")
        return rendered

    def content_sha(self, rendered: str) -> str:
        data = _canonical_bytes(rendered.encode("utf-8"))
        if data is None:
            data = rendered.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def installed_sha(self, path: Path) -> str | None:
        path = Path(path)
        if not path.is_file():
            return None
        data = path.read_bytes()
        # A plist that no longer parses can never equal a canonical
        # rendered form: fall back to the raw bytes' hash (drift).
        canonical = _canonical_bytes(data) or data
        return hashlib.sha256(canonical).hexdigest()

    def unit_config(self, path: Path) -> Path | None:
        try:
            plist = plistlib.loads(Path(path).read_bytes())
        except (ValueError, ExpatError, OSError):
            return None
        env = plist.get("EnvironmentVariables")
        if not isinstance(env, dict):
            return None
        value = env.get("ORBI_CONFIG")
        if not isinstance(value, str) or not value:
            return None
        return Path(value).expanduser().resolve()

    def reload_marker(self, installed_dir: Path,
                      name: str) -> Path:
        """Where the deferred-reload record for one plist lives."""
        return Path(installed_dir) / f"{name}{RELOAD_PENDING_SUFFIX}"

    def reload_pending(self, installed_dir: Path, name: str) -> bool:
        """Whether a rewritten plist waits for its running instance.

        True exactly while the (running) instance holds the config it
        was bootstrapped with: ``activate_instances`` writes the marker
        for a running changed instance and clears it on every
        (re)bootstrap, so the drift check flags the stale schedule
        instead of the running tick being killed.
        """
        return self.reload_marker(installed_dir, name).is_file()

    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def target(self, label: str) -> str:
        return f"{self.domain()}/{label}"

    def probe_args(self, unit_name: str | None = None) -> list[str]:
        # The DOMAIN print: exits 0 whenever the logged-in GUI domain
        # exists, before any agent is installed.
        return ["launchctl", "print", self.domain()]

    def unit_enabled(self, run_command, instance: str) -> bool:
        """True unless the label sits in the persistent disabled DB."""
        output = run_command(["launchctl", "print-disabled", self.domain()])
        return not any(
            "=> disabled" in line and instance in line
            for line in output.splitlines()
        )

    def _print_state(self, run_command, label: str) -> str | None:
        """The `print` state value, or None when the label is not loaded.

        Exit 113 with ``Could not find service`` is launchctl's
        documented not-loaded answer (the is-enabled non-zero
        equivalent), not a tool failure; anything else re-raises.
        """
        try:
            output = run_command(["launchctl", "print", self.target(label)])
        except subprocess.CalledProcessError as exc:
            if "Could not find service" in (exc.stderr or ""):
                return None
            raise
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("state ="):
                return stripped.split("=", 1)[1].strip()
        return None

    def unit_state(self, run_command, instance: str) -> str:
        state = self._print_state(run_command, instance)
        return "active" if state == "running" else "inactive"

    def schedule_text(self, instance: int, max_concurrency: int) -> str:
        """The schedule this platform actually deploys (whole minutes)."""
        return calendar_schedule(instance, max_concurrency)

    def instances_status(self, run_command, unit_name: str | None = None,
                         *, max_concurrency: int) -> dict[str, dict]:
        instances: dict[str, dict] = {}
        for index, label in enumerate(self.timer_instances(unit_name, max_concurrency), start=1):
            instances[label] = {
                "enabled": self.unit_enabled(run_command, label),
                "active": self.unit_state(run_command, label) == "active",
                # launchd exposes no next-fire time; honest dash.
                "next": "-",
                "schedule": self.schedule_text(index, max_concurrency),
            }
        return instances

    def journal_lines(self, run_command, repo_dir: Path | None = None,
                      unit_name: str | None = None, *,
                      max_concurrency: int,
                      since_minutes: int | None = None,
                      lines: int | None = None) -> list[str]:
        """Tail the per-instance log files the plist renders.

        ``since_minutes`` has no file equivalent — the bound is the
        tail length (``lines``, defaulting to LOG_TAIL_LINES).
        """
        tail = lines or LOG_TAIL_LINES
        collected: list[str] = []
        repo_dir = Path(repo_dir) if repo_dir is not None else Path.cwd()
        for label in self.timer_instances(unit_name, max_concurrency):
            path = log_path(repo_dir, label)
            if not path.is_file():
                continue
            file_lines = path.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()
            collected.extend(file_lines[-tail:])
        return collected

    def restart_hint(self, unit_name: str | None = None) -> str:
        return (
            f"launchctl kickstart -k {self.target(label_for(unit_name, 1))}"
        )

    def activate_instances(self, run_command, installed_dir: Path,
                           unit_name: str | None = None, *,
                           max_concurrency: int,
                           changed: frozenset[str] = frozenset()) -> None:
        """Enable + bootstrap instances 1..max_concurrency, disable the
        surplus up to MAX_RUNNER_INSTANCES. A live instance is never
        booted out (bootout kills the job); an idle loaded instance is
        re-bootstrapped so the rewritten plist takes effect. A RUNNING
        instance whose plist is in ``changed`` is not reloaded — it
        keeps the tick it is executing — but its deferred reload is
        recorded (``reload_pending``) for the next idle cycle
        (Issue #1347)."""
        labels = self.timer_instances(unit_name, MAX_RUNNER_INSTANCES)
        installed_dir = Path(installed_dir)
        for label in labels[:max_concurrency]:
            # Enable FIRST: a stale disabled record fails the bootstrap.
            run_command(["launchctl", "enable", self.target(label)])
            name = plist_name(label)
            marker = self.reload_marker(installed_dir, name)
            if self._print_state(run_command, label) is None:
                run_command([
                    "launchctl", "bootstrap", self.domain(),
                    str(installed_dir / name),
                ])
                marker.unlink(missing_ok=True)
            elif self.unit_state(run_command, label) != "active":
                # Idle: reload the rewritten plist (daemon-reload
                # equivalent). Running: never — it would kill the task.
                run_command(["launchctl", "bootout", self.target(label)])
                run_command([
                    "launchctl", "bootstrap", self.domain(),
                    str(installed_dir / name),
                ])
                marker.unlink(missing_ok=True)
            elif name in changed:
                # Running with a rewritten plist: no launchctl command
                # re-reads a loaded plist (kickstart -k restarts the
                # process, not the config), so record the pending
                # reload for the next idle tick instead of killing it.
                marker.write_text(
                    "this instance is running the plist it was loaded "
                    "with; the next idle tick reloads it\n",
                    encoding="utf-8",
                )
        for label in labels[max_concurrency:]:
            # Disable persists across logins; bootout only unloads an
            # idle instance (bootout on a running job kills it).
            run_command(["launchctl", "disable", self.target(label)])
            self.reload_marker(
                installed_dir, plist_name(label),
            ).unlink(missing_ok=True)
            if (
                self._print_state(run_command, label) is not None
                and self.unit_state(run_command, label) != "active"
            ):
                run_command(["launchctl", "bootout", self.target(label)])

    def pre_install(self, run_command, installed_dir: Path,
                    unit_name: str | None = None) -> None:
        # No launchd-era legacy units exist to migrate.
        return None

    def unmanaged_entries(self, installed_dir: Path) -> list[dict]:
        """Orbi agent plists no deployment's drift check can see.

        Managed plists are the rendered instances
        ``org.orbi[.<unit_name>].runner.<N>.plist``; the shape match is
        deliberately conservative (a hand-written plist mimicking the
        managed shape stays invisible — the same trade the systemd
        ``@``-form convention makes).
        """
        installed_dir = Path(installed_dir)
        if not installed_dir.is_dir():
            return []
        managed_re = re.compile(r"org\.orbi(?:\..+)?\.runner\.\d+\.plist")
        entries: list[dict] = []
        for path in sorted(installed_dir.iterdir(), key=lambda p: p.name):
            name = path.name
            if not (path.is_file() and name.startswith("org.orbi")
                    and name.endswith(".plist")):
                continue
            if managed_re.fullmatch(name):
                continue
            entries.append({"unit": name, "config": self.unit_config(path)})
        return entries
