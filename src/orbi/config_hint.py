"""The repair hint a config diagnostic prints (Issue #2190).

`cli.py` owns config path resolution and loading; this module owns the
installed-unit repair hint those diagnostics carry: which config files the
Orbi units installed for this user reference, and how a read-only command
is pointed at one of them. It is deliberately pure text — the scan is
`source_pool.installed_unit_configs` — and selecting a deployment stays the
user's decision (Issue #1001): an invalid local config names the
candidates next to itself, it is never replaced by one of them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from orbi import cli_commands
from orbi.source_pool import installed_unit_configs


def candidate_lines(candidates: tuple[tuple[Path, str], ...]) -> str:
    """The `candidate=<path> units=<units>` tail a config hint carries."""
    return "".join(
        f"; candidate={candidate} units={units}"
        for candidate, units in candidates
    )


def config_selected_explicitly(argv: list[str] | None) -> bool:
    """The caller already chose a config: the `--config` flag or the variable."""
    values = sys.argv[1:] if argv is None else argv
    flagged = any(
        value == "--config" or value.startswith("--config=")
        for value in values
    )
    return flagged or "ORBI_CONFIG" in os.environ


def invalid_config_candidates(
    args, argv: list[str] | None,
) -> tuple[tuple[Path, str], ...]:
    """The installed-unit configs an INVALID implicit config names.

    Only a read-only command that picked up an implicit `./orbi.toml` names
    them: an explicit `--config` / `ORBI_CONFIG` already made that choice,
    and a mutating command never resolves another deployment.
    """
    if (config_selected_explicitly(argv)
            or args.command not in cli_commands.READ_ONLY_COMMANDS):
        return ()
    return installed_unit_configs(getattr(args, "installed_dir", None))


def invalid_config_message(args, argv: list[str] | None, detail: str) -> str:
    """The `config_invalid` line, plus the unit hint when one is installed.

    Without an installed candidate the line is exactly the diagnosis a
    build before Issue #2190 printed.
    """
    candidates = invalid_config_candidates(args, argv)
    if not candidates:
        return f"config_invalid {detail}"
    return (
        f"config_invalid {detail}; "
        "an invalid config is never replaced by another deployment; "
        "fix=select the installed deployment config explicitly: "
        "`--config <path>` (after the subcommand) or "
        "`ORBI_CONFIG=<path> orbi <command>`"
        f"{candidate_lines(candidates)}"
    )
