"""Read-only task-pool overlap diagnosis across installed deployments.

Issue #1519: a Runner has no cross-deployment lock, so two differently
named Orbi deployments that serve the same GitHub repository would
compete for the same Issues. ``orbi doctor`` diagnoses that topology from
the Orbi units already installed for the current user, without loading
another deployment's env/provider secrets and without mutating anything.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import scheduler

if TYPE_CHECKING:
    from orbi import config as config_domain


def installed_unit_configs(
    installed_dir: Path | None = None,
    sched: scheduler.Scheduler | None = None,
) -> tuple[tuple[Path, str], ...]:
    """Return the ORBI_CONFIG values declared by installed units.

    This is deliberately a filesystem read: resolving a missing default must
    not invoke a scheduler command or mutate the user's installation. The
    optional ``sched`` is a test seam; production callers let it default to
    the detected scheduler.
    """
    sched = sched or scheduler.detect()
    installed_dir = installed_dir or sched.installed_unit_dir()
    if not installed_dir.is_dir():
        return ()
    found: dict[Path, list[str]] = {}
    for unit in sorted(installed_dir.iterdir(), key=lambda path: path.name):
        # Only inspect files belonging to Orbi's scheduler namespace.  User
        # unit directories commonly contain unrelated services; accepting an
        # arbitrary service's ORBI_CONFIG could select the wrong deployment.
        is_systemd_unit = (
            unit.name.startswith("orbi")
            and unit.name.endswith((".service", ".timer"))
        )
        is_launchd_unit = (
            unit.name.startswith("org.orbi.") and unit.name.endswith(".plist")
        )
        if not unit.is_file() or not (is_systemd_unit or is_launchd_unit):
            continue
        config = sched.unit_config(unit)
        if config is not None:
            found.setdefault(config, []).append(unit.name)
    return tuple(
        (path, ", ".join(units))
        for path, units in sorted(found.items(), key=lambda item: str(item[0]))
    )


# The repair direction for a discovered config this comparison cannot
# read: the operator either makes it readable or drops the unit's
# ORBI_CONFIG reference (Issue #1519).
SOURCE_POOL_CONFIG_FIX = (
    "fix or remove the ORBI_CONFIG reference in the installed unit; the "
    "doctor cannot read this deployment's task pool"
)
# The one-deployment-per-pool rule: concurrency belongs to the deployment
# chosen for the pool.
SOURCE_POOL_CONFLICT_FIX = (
    "choose one deployment for that pool and use its max_concurrency "
    "for concurrency"
)


def _discovered_source_repos(path: Path) -> tuple[str, ...]:
    """Read ONLY the task-pool declaration from another deployment config.

    The pool comparison must not load another deployment's env file,
    resolve its model secrets or validate its providers: that would
    mutate this process and require credentials the read-only diagnostic
    does not need (Issue #1519). A plain TOML read of ``source_repos`` is
    the whole contract, so this is deliberately NOT ``load_config``.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    repos = data.get("source_repos")
    if (
        not isinstance(repos, list)
        or not repos
        or not all(isinstance(repo, str) and repo for repo in repos)
    ):
        raise ValueError("source_repos must be a non-empty list of strings")
    return tuple(repos)


def source_pool_lines(
    config: config_domain.RunnerConfig,
    installed_dir: Path | None = None,
    sched: scheduler.Scheduler | None = None,
) -> list[str]:
    """Read-only overlap diagnosis across the installed deployments.

    The current deployment's ``source_repos`` are compared,
    case-insensitively, with the other configs discovered from the Orbi
    units visible to the current user (the existing
    :func:`installed_unit_configs` abstraction, including the
    ``--installed-dir`` surface). Config paths are canonicalized so
    repeated service/timer slots, symlink aliases and the current config
    are never counted as separate deployments.

    Deterministic output: one ``FAILED`` line per overlapping repository
    (repository, both config paths, the repair action), one
    ``INCOMPLETE`` line per discovered config that cannot be read (path,
    reason, repair direction), or one ``healthy`` line. Read-only: no
    labels, units, git, GitHub or model call, and no other deployment's
    env file is loaded.
    """
    this_config = Path(config.config_path).resolve()
    discovered: dict[Path, None] = {}
    for path, _units in installed_unit_configs(installed_dir, sched):
        canonical = Path(path).resolve()
        if canonical != this_config:
            discovered[canonical] = None
    others = sorted(discovered, key=str)
    this_repos: dict[str, str] = {}
    for repo in config.source_repos:
        this_repos.setdefault(repo.lower(), repo)
    conflicts: list[str] = []
    incomplete: list[str] = []
    for other in others:
        try:
            other_repos = _discovered_source_repos(other)
        except FileNotFoundError:
            reason = "missing"
        except tomllib.TOMLDecodeError:
            reason = "malformed"
        except OSError:
            reason = "unreadable"
        except ValueError:
            reason = "invalid_source_repos"
        else:
            seen: set[str] = set()
            for other_repo in other_repos:
                key = other_repo.lower()
                if key in seen:
                    continue
                seen.add(key)
                this_repo = this_repos.get(key)
                if this_repo is not None:
                    conflicts.append(
                        f"source_pool: FAILED repo={this_repo} "
                        f"this_config={this_config} "
                        f"other_config={other} "
                        f"fix={SOURCE_POOL_CONFLICT_FIX}"
                    )
            continue
        incomplete.append(
            f"source_pool: INCOMPLETE config={other} reason={reason} "
            f"fix={SOURCE_POOL_CONFIG_FIX}"
        )
    if conflicts or incomplete:
        return [*conflicts, *incomplete]
    return [f"source_pool: healthy deployments={1 + len(others)}"]
