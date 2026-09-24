"""The scheduler layer: one interface, one implementation per platform.

Orbi schedules its Runner through the hosting platform's scheduler:
systemd user units on Linux, launchd agents on macOS (Issue #849).
Every module that used to assemble scheduler commands by hand
(``pilot_setup``, ``cli``, ``runner``, ``runner_health``) goes through
this layer instead; the ``systemctl``/``launchctl`` literals live only
inside the platform implementations.

The layer carries:

- the :class:`Scheduler` interface (the hooks a platform implements,
  typed as a :class:`typing.Protocol` so both implementations and the
  test fakes are checked against one contract);
- the platform dispatch (:func:`detect`) — Linux →
  :class:`orbi.systemd_deploy.SystemdScheduler`, macOS →
  :class:`orbi.launchd_deploy.LaunchdScheduler`, anything else fails
  fast with an honest platform-limitation error naming the issue;
- the platform-INDEPENDENT orchestration that used to live beside the
  systemd implementation: the idempotent install, the drift status /
  check / self-heal flow and the deployment-conflict rejection. It is
  written against the hooks only, which is what makes the launchd
  branch unit-testable on a Linux CI runner (inject a fake or the
  launchd implementation — no macOS required).

No database, queue, daemon or second state store: the installed files
and the platform scheduler itself are the only state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from orbi.journal import event
from orbi.progress import quote_value

if TYPE_CHECKING:
    # Annotation-only: ``orbi.config`` imports this module at runtime,
    # so this module never imports it back.
    from orbi import config as config_domain

# The honest-failure link carried by every platform-limitation error
# (install.sh, the dispatch below): platform support status lives here.
ISSUE_URL = "https://github.com/orbi-build/orbi/issues/849"

# The official per-platform scheduler documentation the check-gate
# failures name (the layer owns the platform facts; callers only
# reference them).
DOCS = {
    "systemd": (
        "https://www.freedesktop.org/software/systemd/man/systemctl.html"
    ),
    "launchd": (
        "https://developer.apple.com/library/archive/documentation/"
        "MacOSX/Conceptual/BPSystemStartup/Chapters/Introduction.html"
    ),
}

FIX_COMMAND = "orbi install-units"

UNMANAGED_FIX = (
    "set unit_name in each deployment config and run `orbi install-units`"
)

# The config declaration cap (Issue #827): a deployment may declare up to
# MAX_RUNNER_INSTANCES concurrent Runner instances. It is NOT a machine-
# capacity assertion and NOT derived from any unit-name list — the names
# are generated per count below, and the real concurrency boundary stays
# the flock slots in the Runner (max_concurrency).
MAX_RUNNER_INSTANCES = 5

# The unit templates are machine-independent. The machine-specific
# values (the deployment checkout path and the user home) are carried
# as placeholders and substituted at install time; the templates never
# hardcode machine paths.
REPO_DIR_PLACEHOLDER = "{{ORBI_REPO_DIR}}"
USER_HOME_PLACEHOLDER = "{{ORBI_USER_HOME}}"


class UnitDriftError(RuntimeError):
    """The installed units have drifted from the repo templates."""


class UnitConflictError(RuntimeError):
    """An existing unit belongs to a different deployment checkout."""


class UnsupportedPlatformError(RuntimeError):
    """This machine has no scheduler implementation (not Linux/macOS)."""


def unit_prefix(unit_name: str | None = None) -> str:
    """The systemd-shape name prefix shared by every generated name."""
    return "orbi" if unit_name is None else f"orbi-{unit_name}"


def unit_names(unit_name: str | None = None) -> tuple[str, str]:
    prefix = unit_prefix(unit_name)
    return f"{prefix}@.service", f"{prefix}@.timer"


def timer_instances(unit_name: str | None = None,
                    count: int = 1) -> tuple[str, ...]:
    prefix = unit_prefix(unit_name)
    return tuple(f"{prefix}@{index}.timer" for index in range(1, count + 1))


def service_instances(unit_name: str | None = None,
                      count: int = 1) -> tuple[str, ...]:
    prefix = unit_prefix(unit_name)
    return tuple(f"{prefix}@{index}.service" for index in range(1, count + 1))


def instance_offset_seconds(instance: int, max_concurrency: int) -> int:
    """The deterministic stagger offset in seconds for one runner instance.

    offset(i) = (i - 1) * (300 // max_concurrency) for i = 1..N.
    """
    if max_concurrency <= 1 or instance <= 1:
        return 0
    return (instance - 1) * (300 // max_concurrency)


def instance_schedule(instance: int, max_concurrency: int) -> str:
    """The schedule string for one runner instance."""
    offset = instance_offset_seconds(instance, max_concurrency)
    if offset == 0:
        return "*-*-* *:00/5"
    m, s = divmod(offset, 60)
    return f"*-*-* *:{m:02d}/5:{s:02d}"


def schedule_spellings(sched: Scheduler,
                       config: config_domain.RunnerConfig,
                       installed_dir: Path) -> list[str]:
    """``<instance>=<schedule>`` for every configured instance.

    The expected schedule is spelled the way THIS platform deploys it
    (:meth:`Scheduler.schedule_text`). The INSTALLED schedule is read
    from disk (:meth:`Scheduler.installed_schedule`) and a mismatch
    reports both values plus the fix command (Issue #1344): the report
    never claims a schedule that is not deployed.
    """
    spellings: list[str] = []
    for index, unit in enumerate(
        sched.timer_instances(config.unit_name, config.max_concurrency),
        start=1,
    ):
        expected = sched.schedule_text(index, config.max_concurrency)
        installed = sched.installed_schedule(
            installed_dir, config.unit_name, index, config.max_concurrency,
        )
        if installed == expected:
            spellings.append(f"{unit}={expected}")
        else:
            shown = installed if installed is not None else "?"
            spellings.append(
                f"{unit}={shown} "
                f"(expected {expected}; run: {FIX_COMMAND})"
            )
    return spellings


def instance_report_lines(sched: Scheduler, run_command,
                          config: config_domain.RunnerConfig,
                          installed_dir: Path) -> list[str]:
    """The doctor's per-unit state lines and per-instance schedule lines.

    One ``<unit>: <state>`` line per managed unit (the timer names
    first, then the service names; a name in both sets is listed once),
    then one ``schedule: <instance>=<schedule>`` line per configured
    timer instance. The schedule spelling belongs to the scheduler
    contract, and ``cli`` is frozen by the size ratchet (Issue #1229),
    so the rendering lives here (Issue #1320). ``installed_dir`` makes
    the schedule lines read the INSTALLED value (Issue #1344).
    """
    lines = [
        f"{unit}: {sched.unit_state(run_command, unit)}"
        for unit in dict.fromkeys((
            *sched.timer_instances(config.unit_name, config.max_concurrency),
            *sched.service_instances(config.unit_name, config.max_concurrency),
        ))
    ]
    lines.extend(
        f"schedule: {spelling}"
        for spelling in schedule_spellings(sched, config, installed_dir)
    )
    return lines


@runtime_checkable
class Scheduler(Protocol):
    """The hooks one platform scheduler implements.

    Implementations: :class:`orbi.systemd_deploy.SystemdScheduler`
    (Linux) and :class:`orbi.launchd_deploy.LaunchdScheduler` (macOS).
    ``unit_name`` is the optional per-deployment rename prefix;
    ``count`` is the configured ``max_concurrency``. Every hook is
    deterministic given its inputs so the drift comparison can
    re-render the templates.
    """

    #: short machine name used in error messages and output fields
    name: str
    #: human name for messages ("systemd" / "launchd")
    display: str
    #: repo directory holding this platform's unit templates
    template_dir: str

    def template_units(self) -> tuple[str, ...]:
        """The template file names inside ``template_dir``."""

    def unit_pairs(self, unit_name: str | None,
                   count: int) -> list[tuple[str, str]]:
        """The managed (template name, installed name) file pairs.

        This IS the drift-checked set: one entry per installed file.
        The pair position (1-based) is the instance index handed to
        :meth:`render_unit`.
        """

    def timer_instances(self, unit_name: str | None,
                        count: int) -> tuple[str, ...]:
        """The schedule instance names (@1..@count)."""

    def service_instances(self, unit_name: str | None,
                          count: int) -> tuple[str, ...]:
        """The process instance names (== timer names on launchd)."""

    def installed_unit_dir(self, override: str | None = None) -> Path:
        """The installed-units directory ($ORBI_UNIT_DIR wins)."""

    def render_unit(self, template_text: str, repo_dir: Path,
                    unit_name: str | None = None,
                    instance: int = 1,
                    max_concurrency: int = 1) -> str:
        """Render one template for this deployment and instance."""

    def content_sha(self, rendered: str) -> str:
        """The content identity of rendered unit text.

        This is the drift-comparison hash: raw bytes on systemd, the
        CANONICAL plist form on launchd (whitespace and key order in
        the XML must never fabricate drift).
        """

    def installed_sha(self, path: Path) -> str | None:
        """The installed file's content identity (None when missing)."""

    def unit_config(self, path: Path) -> Path | None:
        """The ORBI_CONFIG an installed unit points at (conflict check)."""

    def probe_args(self, unit_name: str | None = None) -> list[str]:
        """The read-only probe proving the scheduler session exists."""

    def unit_enabled(self, run_command, instance: str) -> bool:
        """Whether the schedule instance is enabled."""

    def unit_state(self, run_command, instance: str) -> str:
        """``active`` / ``inactive`` for one instance."""

    def schedule_text(self, instance: int, max_concurrency: int) -> str:
        """The instance's deployed schedule, spelled for this platform.

        The two schedulers cannot always express the same offset
        (launchd's ``StartCalendarInterval`` has whole-minute
        granularity), so the reports read the spelling the platform
        actually deploys instead of one shared string.
        """

    def instances_status(self, run_command, unit_name: str | None = None,
                         *, max_concurrency: int) -> dict[str, dict]:
        """One {instance: {enabled, active, next}} report entry each."""

    def journal_lines(self, run_command, repo_dir: Path | None = None,
                      unit_name: str | None = None, *,
                      max_concurrency: int,
                      since_minutes: int | None = None,
                      lines: int | None = None) -> list[str]:
        """Recent Runner output lines (crash scan + doctor tail).

        ``repo_dir`` anchors where a file-backed implementation reads
        (launchd's per-instance logs); the journal-backed
        implementation ignores it.
        """

    def restart_hint(self, unit_name: str | None = None) -> str:
        """The operator command that starts/restarts instance 1."""

    def reload_pending(self, installed_dir: Path, name: str) -> bool:
        """Whether a rewritten unit waits for its running instance.

        ``True`` only on launchd: a running agent keeps the plist it
        was loaded with, so a plist rewritten under it is flagged and
        reloaded at its next idle tick (Issue #1347). systemd's
        ``daemon-reload`` applies the new unit to the next service
        start, so its answer is always ``False``.
        """

    def extra_drift(self, installed_dir: Path, unit_name: str | None,
                    max_concurrency: int) -> list[dict]:
        """Installed files outside the template set that drift applies to.

        The entries use the :func:`unit_status` shape (``unit``,
        ``repo_path``, ``installed_path``, both hashes, ``missing``,
        ``drifted``, ``reload_pending``) so the same drift report and
        self-heal path handles them. systemd returns one entry per
        expected/extra stagger drop-in; launchd returns ``[]``.
        """

    def installed_schedule(self, installed_dir: Path, unit_name: str | None,
                           instance: int,
                           max_concurrency: int) -> str | None:
        """The instance's schedule as INSTALLED on disk (or ``None``).

        The report reads this instead of the computed
        :meth:`schedule_text`, so an uninstalled or stale schedule is
        never reported as deployed (Issue #1344).
        """

    def activate_instances(self, run_command, installed_dir: Path,
                           unit_name: str | None = None, *,
                           max_concurrency: int,
                           changed: frozenset[str] = frozenset()) -> None:
        """Converge the instance schedules onto ``max_concurrency``.

        Activates instances 1..max_concurrency, deactivates the
        surplus up to MAX_RUNNER_INSTANCES. A live Runner instance is
        NEVER stopped or restarted. ``changed`` names the installed
        units whose bytes the install just rewrote; an implementation
        whose platform defers a live instance's reload (launchd)
        records it through :meth:`reload_pending`.
        """

    def pre_install(self, run_command, installed_dir: Path,
                    unit_name: str | None = None) -> None:
        """One-time migrations before the files are written."""

    def unmanaged_entries(self, installed_dir: Path) -> list[dict]:
        """Orbi unit files no deployment's drift check can see."""


def detect(system: str | None = None) -> Scheduler:
    """The scheduler implementation for this (or the given) platform.

    The ``system`` override is the test seam that lets the launchd
    branch run on a Linux CI runner. An unsupported platform fails
    fast with the honest limitation message and the issue link.
    """
    from platform import system as platform_system

    name = system or platform_system()
    if name == "Linux":
        from orbi.systemd_deploy import SystemdScheduler

        return SystemdScheduler()
    if name == "Darwin":
        from orbi.launchd_deploy import LaunchdScheduler

        return LaunchdScheduler()
    raise UnsupportedPlatformError(
        f"orbi has no scheduler support for platform {name!r}: it runs "
        "on Linux (systemd) and macOS (launchd); see " + ISSUE_URL
    )


def repo_template_dir(repo_dir: Path, sched: Scheduler) -> Path:
    """The deployment checkout's template directory for this platform."""
    return Path(repo_dir) / sched.template_dir


def reject_different_deployment(repo_dir: Path, installed_dir: Path,
                                unit_name: str | None = None, *,
                                sched: Scheduler | None = None) -> None:
    """Refuse to overwrite units owned by another checkout."""
    sched = sched or detect()
    first_installed = sched.unit_pairs(unit_name, 1)[0][1]
    existing = sched.unit_config(Path(installed_dir) / first_installed)
    if existing is None:
        return
    expected = (Path(repo_dir).resolve() / "orbi.toml").resolve()
    if existing == expected:
        return
    message = (
        f"existing {sched.display} deployment points at a different "
        f"ORBI_CONFIG: {existing} (this checkout uses {expected}); "
        "uninstall the existing deployment before installing this checkout"
    )
    event(
        "unit_conflict", level=logging.ERROR,
        unit=first_installed, installed_config=existing,
        expected_config=expected, action="uninstall_existing_deployment",
    )
    raise UnitConflictError(message)


def unit_status(repo_dir: Path, installed_dir: Path,
                unit_name: str | None = None, *,
                max_concurrency: int = 1,
                sched: Scheduler | None = None) -> list[dict]:
    """Compare the installed units against the repo templates.

    One entry per managed unit (the impl's ``unit_pairs`` set) plus the
    platform's non-template installed files (:meth:`Scheduler.extra_drift`,
    systemd's stagger drop-ins): the repo and installed paths, both
    content identities (None when the file is missing) and whether the
    unit drifted. A missing template or a missing installed unit is
    drift: the deployment is not verifiable.
    """
    sched = sched or detect()
    repo_dir = Path(repo_dir)
    installed_dir = Path(installed_dir)
    entries: list[dict] = []
    for index, (template_name, name) in enumerate(
        sched.unit_pairs(unit_name, max_concurrency), start=1,
    ):
        repo_path = repo_template_dir(repo_dir, sched) / template_name
        installed_path = installed_dir / name
        if repo_path.is_file():
            # The installed unit is the RENDERED template
            # (the checkout path substituted), so the drift check must
            # compare against the rendered form — otherwise a clean
            # install would always look drifted.
            rendered = sched.render_unit(
                repo_path.read_text(encoding="utf-8"),
                repo_dir, unit_name, instance=index,
                max_concurrency=max_concurrency,
            )
            repo_sha = sched.content_sha(rendered)
        else:
            repo_sha = None
        installed_sha = sched.installed_sha(installed_path)
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
            # A rewritten unit a running instance still holds (launchd)
            # is not file drift, but the next idle tick must reload it.
            "reload_pending": sched.reload_pending(installed_dir, name),
        })
    # The platform's non-template installed files (systemd's stagger
    # drop-ins) are part of the SAME drift set (Issue #1344).
    entries.extend(
        sched.extra_drift(installed_dir, unit_name, max_concurrency),
    )
    return entries


def drift_lines(status: list[dict]) -> list[str]:
    """One ``unit_drift`` report line per drifted unit.

    Builds the lines carried by the ``UnitDriftError`` message: the
    repo path, the installed path, both hashes and the idempotent fix
    command. Values containing spaces are quoted (the
    progress.quote_value convention) so the line stays parseable.
    This helper never logs — the journal emission goes through
    ``event()`` (:func:`_log_drifted_units`).
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


def _log_drifted_units(status: list[dict]) -> None:
    """Emit one structured ``unit_drift`` failure line per drifted unit
    through the single journal emission point: same fields
    as ``drift_lines``, minus the report-only message role."""
    for entry in status:
        if not entry["drifted"]:
            continue
        event(
            "unit_drift", level=logging.ERROR, unit=entry["unit"],
            repo=entry["repo_path"], installed=entry["installed_path"],
            repo_sha256=entry["repo_sha256"] or "-",
            installed_sha256=entry["installed_sha256"] or "-",
            fix=FIX_COMMAND,
        )


def check_unit_drift(repo_dir: Path,
                     installed_dir: Path | None = None,
                     unit_name: str | None = None, *,
                     max_concurrency: int = 1,
                     sched: Scheduler | None = None) -> None:
    """Pre-start deployment check.

    Compares the managed installed units against the repo templates.
    Clean: logs ``unit_drift clean`` and returns. Drift: logs one
    structured ``unit_drift`` line per drifted unit and raises
    ``UnitDriftError`` — the caller fails fast and claims no Issue
    until the units are synced with the idempotent install command.
    A deferred reload (a running launchd instance holding a rewritten
    plist, Issue #1347) is not file drift, but it raises the same way
    so the caller's self-heal runs and the next idle tick reloads it.
    """
    sched = sched or detect()
    if installed_dir is None:
        installed_dir = sched.installed_unit_dir()
    status = unit_status(repo_dir, installed_dir, unit_name,
                         max_concurrency=max_concurrency, sched=sched)
    lines = drift_lines(status)
    if not lines:
        pending = [entry for entry in status if entry["reload_pending"]]
        if not pending:
            event("unit_drift", result="clean", installed_dir=installed_dir)
            return
        for entry in pending:
            event(
                "unit_drift", result="reload_pending", unit=entry["unit"],
                installed=entry["installed_path"], fix=FIX_COMMAND,
            )
        raise UnitDriftError(
            f"installed {sched.display} units hold a rewritten plist on a "
            "running instance; the reload is deferred to the next idle "
            f"tick (sync with: {FIX_COMMAND})\n" + "\n".join(
                f"unit_drift unit={entry['unit']} "
                f"installed={quote_value(str(entry['installed_path']))} "
                f"reload_pending=true fix={FIX_COMMAND}"
                for entry in pending
            )
        )
    _log_drifted_units(status)
    raise UnitDriftError(
        f"installed {sched.display} units have drifted from the repo "
        f"templates; sync with: {FIX_COMMAND}\n" + "\n".join(lines)
    )


def install_units(repo_dir: Path, installed_dir: Path | None = None,
                  *, max_concurrency: int,
                  unit_name: str | None = None, run_command,
                  sched: Scheduler | None = None) -> dict:
    """Idempotently install the repo templates as the platform units.

    Overwrites every managed installed unit with its rendered repo
    template (the repo is the single source of truth), unless the
    installed deployment belongs to a different config, which fails
    before any migration or write. Runs the impl's one-time
    ``pre_install`` migrations, then converges the instance schedules
    onto ``max_concurrency`` (``activate_instances`` — surplus
    instances deactivate, a live Runner is never restarted). Returns
    the deployed commit (the deployment checkout's HEAD) and the
    installed units' content identities.
    """
    sched = sched or detect()
    if not 1 <= max_concurrency <= MAX_RUNNER_INSTANCES:
        # Issue #829: the error names the CURRENT value too — the
        # operator must see what the config wrote, not just the range.
        raise ValueError(
            "max_concurrency must be a positive integer no greater than "
            f"{MAX_RUNNER_INSTANCES} (MAX_RUNNER_INSTANCES); "
            f"got {max_concurrency!r}"
        )
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = sched.installed_unit_dir()
    installed_dir = Path(installed_dir)
    pairs = sched.unit_pairs(unit_name, max_concurrency)
    reject_different_deployment(repo_dir, installed_dir, unit_name,
                                sched=sched)
    for template_name, _ in pairs:
        template = repo_template_dir(repo_dir, sched) / template_name
        if not template.is_file():
            raise FileNotFoundError(
                f"unit template missing: {template} (the repo "
                "templates are the single source of truth)"
            )
    installed_dir.mkdir(parents=True, exist_ok=True)
    sched.pre_install(run_command, installed_dir, unit_name)
    changed: set[str] = set()
    for index, (template_name, name) in enumerate(pairs, start=1):
        # Render the template (substitute the deployment-specific
        # placeholders) so the installed unit points at THIS checkout
        # regardless of where it lives.
        template_text = (
            repo_template_dir(repo_dir, sched) / template_name
        ).read_text(encoding="utf-8")
        rendered = sched.render_unit(
            template_text, repo_dir, unit_name, instance=index,
            max_concurrency=max_concurrency,
        )
        # The overwrite is idempotent: only a REAL content change is
        # handed to the activation hook, which uses it to record a
        # deferred reload for a live instance (launchd, Issue #1347).
        if sched.installed_sha(installed_dir / name) != sched.content_sha(rendered):
            changed.add(name)
        (installed_dir / name).write_bytes(rendered.encode("utf-8"))
    sched.activate_instances(
        run_command, installed_dir, unit_name,
        max_concurrency=max_concurrency, changed=frozenset(changed),
    )
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    units = {
        name: {
            "installed_path": installed_dir / name,
            "sha256": sched.installed_sha(installed_dir / name),
        }
        for _, name in pairs
    }
    event(
        "units_installed", commit=commit, installed_dir=installed_dir,
        units=",".join(name for _, name in pairs),
        instances=",".join(
            sched.timer_instances(unit_name, max_concurrency),
        ),
    )
    return {
        "commit": commit,
        "installed_dir": installed_dir,
        "units": units,
    }


def sync_drifted_units(repo_dir: Path,
                       installed_dir: Path | None = None,
                       *, max_concurrency: int,
                       unit_name: str | None = None, run_command,
                       sched: Scheduler | None = None) -> list[dict]:
    """Pre-start self-heal for drifted units.

    The normal scene: a template change merged to main, the
    ExecStartPre-synced checkout carries the new templates, and the
    installed units are still the old ones. Runs the SAME idempotent
    install (:func:`install_units` — never stops or restarts a live
    Runner) and re-verifies with the SAME comparison
    (:func:`unit_status`). Clean after the sync: logs one structured
    ``unit_drift auto_synced`` line per unit (unit, before/after
    sha256, deployed commit) and returns the per-unit report. Still
    drifted after the sync: logs the structured ``unit_drift`` lines
    and raises ``UnitDriftError`` (fail fast — the caller claims no
    Issue). A failing install step propagates unchanged. No drift:
    returns ``[]`` without touching anything.
    """
    sched = sched or detect()
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = sched.installed_unit_dir()
    installed_dir = Path(installed_dir)
    before = unit_status(repo_dir, installed_dir, unit_name,
                         max_concurrency=max_concurrency, sched=sched)
    if not any(
        entry["drifted"] or entry["reload_pending"] for entry in before
    ):
        return []
    result = install_units(
        repo_dir, installed_dir, max_concurrency=max_concurrency,
        unit_name=unit_name, run_command=run_command, sched=sched,
    )
    after = unit_status(repo_dir, installed_dir, unit_name,
                        max_concurrency=max_concurrency, sched=sched)
    lines = drift_lines(after)
    if lines:
        _log_drifted_units(after)
        raise UnitDriftError(
            f"installed {sched.display} units still drift after the "
            f"pre-start sync; sync with: {FIX_COMMAND}\n"
            + "\n".join(lines)
        )
    report: list[dict] = []
    for entry_before, entry_after in zip(before, after):
        event(
            "unit_drift", result="auto_synced", unit=entry_after["unit"],
            before_sha256=entry_before["installed_sha256"] or "-",
            after_sha256=entry_after["installed_sha256"],
            commit=result["commit"],
        )
        report.append({
            "unit": entry_after["unit"],
            "before_sha256": entry_before["installed_sha256"],
            "after_sha256": entry_after["installed_sha256"],
            "commit": result["commit"],
        })
    return report
