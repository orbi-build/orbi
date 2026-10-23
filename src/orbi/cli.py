#!/usr/bin/env python3
"""Orbi task dispatch and status CLI.

With NO subcommand the CLI IS the Runner entry: it runs one
tick, exactly like `python3 -m orbi.runner` — this is what the
scheduler entry (the systemd service's `ExecStart` on Linux, the
launchd agent on macOS; the installed `orbi`) invokes on
every trigger. The named subcommands are the dispatch and debug
entries on top of that:

`add` creates an Issue in a configured source repo and labels it
`ai-ready`.
`status` reports the current in-progress Issue (with its live Pi
activity: phase, last activity time, the last meaningful action, the
newest tool call result, session file and worktree), the next ready
Issue, and the most recent result (`ai-pr-opened` / `ai-fix-needed` /
`ai-merged` / `ai-blocked`) per source repo.

GitHub Issues and labels are the only state store. There is no database,
queue, or web UI. Command failures are logged and raised by the reused
runner.run_command; there is no fallback.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from orbi import cli_commands, config as config_domain, pilot_setup, runner
from orbi.cli_parser import build_parser
from orbi.runner import (
    RunIdFilter,
    configure_logging,
    validate_config,
    validate_execution_source_repos,
)
from orbi.source_pool import installed_unit_configs as _installed_unit_configs

LOGGER = logging.getLogger("orbi.cli")
# Same run correlation mechanism as the runner: when a run id is
# bound, every journal line of this process starts with `[run_id]`.
LOGGER.addFilter(RunIdFilter())

# The CLI version: the single source of truth is the
# package `orbi.__version__` (read by the parser builder). tests/
# test_cli_packaging.py pins it against the PEP 621 `version` in
# pyproject.toml, so the two cannot drift.


def _config_was_explicit(argv: list[str] | None) -> bool:
    values = sys.argv[1:] if argv is None else argv
    return any(value == "--config" or value.startswith("--config=")
               for value in values)


def _missing_config_message(
    path: Path, candidates: tuple[tuple[Path, str], ...],
) -> str:
    candidate_lines = "".join(
        f"; candidate={candidate} units={units}"
        for candidate, units in candidates
    )
    return (
        f"config_not_found path={path.resolve()}; "
        "reason=no Orbi config at this path "
        "(the default is `orbi.toml` in the current directory); "
        "fix=run from the deployment directory, or point at the config: "
        "`ORBI_CONFIG=~/orbi-deploy/<dir>/orbi.toml orbi <command>` "
        "(`--config <path>` also works, after the subcommand). "
        "The source checkout is not a deployment directory and has no "
        f"orbi.toml.{candidate_lines}"
    )


def missing_config_check(args,
                         candidates: tuple[tuple[Path, str], ...]) -> int:
    """`check` on a missing config: every machine failure, then the hint.

    The actionable config hint must not hide the independent machine
    failures on a first run, so the collection mode reports every machine
    prerequisite before the config diagnostic. Explicit paths are never
    replaced by a unit candidate.
    """
    check_candidates = candidates or _installed_unit_configs(
        getattr(args, "installed_dir", None),
    )
    _, machine_failures = pilot_setup.run_machine_checks(
        run_command=cli_commands.run_command, collect_failures=True,
    )
    for failure in machine_failures:
        print(pilot_setup.format_check_failure(failure), file=sys.stderr)
    print(
        _missing_config_message(args.config, check_candidates),
        file=sys.stderr,
    )
    return 1


def resolve_config_path(args,
                        argv: list[str] | None) -> tuple[tuple[Path, str], ...]:
    """Resolve a missing default config from the installed units.

    A missing default in a read-only command can be resolved from the
    installed units.  Never do this for an explicit path or a mutating
    command: selecting another deployment must remain the user's decision.
    Returns the candidates the diagnostics name.
    """
    candidates: tuple[tuple[Path, str], ...] = ()
    read_only = args.command in cli_commands.READ_ONLY_COMMANDS
    explicit = _config_was_explicit(argv) or "ORBI_CONFIG" in os.environ
    if read_only and not explicit and not args.config.exists():
        candidates = _installed_unit_configs(
            getattr(args, "installed_dir", None),
        )
        existing = tuple(item for item in candidates if item[0].is_file())
        if len(existing) == 1:
            args.config = existing[0][0]
            LOGGER.info(
                "config_resolved_from_unit path=%s unit=%s",
                existing[0][0], existing[0][1],
            )
    return candidates


def load_command_config(args, candidates: tuple[tuple[Path, str], ...]):
    """Load and validate the config one subcommand needs.

    Returns the config, or None after printing the diagnostic (the caller
    then exits 1). Every command path (doctor included) enforces the same
    single-source-repo contract as runner.main, so a config the Runner
    will reject is reported instead of all-green; status is the read-only
    observer and covers every configured repository, so it does not
    enforce the execution fan-out contract (Issue #1656).
    """
    try:
        if args.command == "setup":
            pilot_setup.ensure_config(args.config)
        config = config_domain.load_config(
            args.config,
            # The engine-source sync and the milestone advance are
            # deployment maintenance operations — a missing provider
            # key must never block them (the Runner's own start
            # enforces the key), so they load the config with the
            # doctor's lenient provider flags.
            check_provider_api_keys=(
                args.command not in cli_commands.NO_PROVIDER_KEY_CHECK_COMMANDS
            ),
            allow_missing_pi_providers=(
                args.command in cli_commands.LENIENT_PROVIDER_COMMANDS
            ),
        )
        validate_config(config)
        if args.command != "status":
            validate_execution_source_repos(config.source_repos)
    except (ValueError, pilot_setup.SetupError) as exc:
        if args.command == "setup":
            print(f"setup_failed reason={exc}", file=sys.stderr)
        else:
            LOGGER.error("config_invalid reason=%s", exc)
        return None
    except config_domain.ConfigFileMissingError as exc:
        if not candidates:
            candidates = _installed_unit_configs(
                getattr(args, "installed_dir", None),
            )
        message = _missing_config_message(exc.path, candidates)
        if args.command == "setup":
            print(f"setup_failed reason={message}", file=sys.stderr)
        else:
            LOGGER.error(message)
        return None
    except FileNotFoundError as exc:
        # A PyPI first run (`orbi setup` in a fresh dir with
        # the just-created example config) fails validation on a missing
        # deployment path (prompts/, deploy_home, ...); the user gets the
        # structured failure line, never a traceback.
        if args.command == "setup":
            print(
                f"setup_failed reason=required path missing: {exc}",
                file=sys.stderr,
            )
        else:
            LOGGER.error("config_invalid reason=required path missing: %s", exc)
        return None
    return config


def main(argv: list[str] | None = None) -> int:
    """Parse one invocation and hand it to the command that owns it.

    The bare tick (no subcommand) IS the Runner entry: it delegates to
    `runner.main` with the same `--config`, which owns the whole tick
    contract (unit-drift preflight, transport preflight, slot, claim,
    fail-fast) — exactly like `python3 -m orbi.runner`.
    `check` runs before any config is loaded (it owns its own
    prerequisite gate); every other subcommand loads the config here and
    then dispatches to its handler.
    """
    parser = build_parser(__doc__)
    args = parser.parse_args(argv)
    configure_logging()
    candidates = resolve_config_path(args, argv)
    if args.command == "check" and not args.config.exists():
        return missing_config_check(args, candidates)
    if args.command is None:
        return runner.main(["--config", str(args.config)])
    if args.command == "check":
        return cli_commands.command_check(args)
    config = load_command_config(args, candidates)
    if config is None:
        return 1
    return cli_commands.COMMAND_HANDLERS[args.command](config, args, parser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
