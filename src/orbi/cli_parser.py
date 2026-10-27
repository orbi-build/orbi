"""The `orbi` argparse tree (Issue #1862).

Extracted from `cli.py` so the entry module is a dispatcher: the one
place that knows every command, flag and help string. No I/O — building
the parser reads `ORBI_CONFIG` for the `--config` default (the
documented environment entry) and nothing else; the description is passed
in so `orbi --help` keeps the entry module's own docstring, byte for
byte (the golden test pins it).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from orbi import __version__, cli_ticket_command, notify, suggest


def build_parser(description: str) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("ORBI_CONFIG", "orbi.toml")),
    )
    parser = argparse.ArgumentParser(description=description, parents=[common])
    parser.add_argument(
        "--version", action="version",
        version=f"orbi {__version__}",
    )
    # The subcommand is OPTIONAL — with no subcommand the
    # installed CLI runs one Runner tick (the systemd ExecStart is the
    # bare `orbi`), exactly like `python3 -m
    # orbi.runner`.
    subparsers = parser.add_subparsers(dest="command")
    add_parser = subparsers.add_parser(
        "add", parents=[common],
        help="create an Issue in a source repo and add ai-ready",
    )
    add_parser.add_argument("title")
    add_parser.add_argument("--body", default="")
    add_parser.add_argument(
        "--repo", default=None,
        help="source repo (default: first configured source)",
    )
    suggest.register_cli(subparsers, common)
    status_parser = subparsers.add_parser(
        "status", parents=[common],
        help="show every active delivery with attributed local activity, "
             "ready queue and recent result per source repo",
    )
    status_parser.add_argument(
        "--repo", default=None,
        help="observe exactly this configured source repository",
    )
    status_parser.add_argument(
        "--issue", type=int, default=None,
        help="observe exactly this Issue (requires --repo when several "
             "repositories are configured)",
    )
    status_parser.add_argument(
        "--follow", action="store_true",
        help="keep observing: local events every 0.5s, GitHub state every "
             "15s, until Ctrl-C",
    )
    session_parser = subparsers.add_parser(
        "session", parents=[common],
        help="print the live Pi session JSONL path, or follow it like tail -f",
    )
    session_parser.add_argument(
        "--follow", action="store_true",
        help="keep printing new lines of the selected file (tail -f)",
    )
    session_parser.add_argument(
        "--pretty", action="store_true",
        help="print one-line summaries instead of raw JSONL",
    )
    install_parser = subparsers.add_parser(
        "install-units", parents=[common],
        help="idempotently install the repo's scheduler units (systemd "
             "on Linux, launchd on macOS; never restarts a running "
             "Runner)",
    )
    install_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory (default: the standard user dir)",
    )
    subparsers.add_parser(
        "sync-engine-source", parents=[common],
        help="sync the deploy home checkout to the configured "
             "engine_source_track (the service ExecStartPre runs this "
             "on every tick; Issue #535)",
    )
    doctor_parser = subparsers.add_parser(
        "doctor", parents=[common],
        help="read-only report: repo commit, unit drift, timer/service, "
             "slots, Pi, current Issue, recent journal",
    )
    doctor_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory to check (default: the standard dir)",
    )
    subparsers.add_parser(
        "check", parents=[common],
        help="read-only prerequisite gate: python, commands, the "
             "scheduler user session, gh auth, pi, config, repo access, "
             "transport, model provider — fails fast with reason, fix and "
             "official docs link (Issue #163)",
    )
    setup_parser = subparsers.add_parser(
        "setup", parents=[common],
        help="one-time, idempotent initialization: gh auth + repo "
             "permissions, platform labels, scheduler units, checkout "
             "check and the optional model proxy (Issue #117)",
    )
    setup_parser.add_argument(
        "--repo", default=None,
        help="initialize exactly this source repo (default: every "
             "configured source repo)",
    )
    setup_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory (default: the standard user dir)",
    )
    setup_parser.add_argument(
        "--json", action="store_true",
        help="print the result as JSON instead of key=value lines",
    )
    # One subcommand per registered ticket command (Issue #1930): the
    # registry is the source, so a new command gets its terminal spelling
    # without an edit here. `/orbi milestone <version>` is arity 1 and
    # keeps the explicit `orbi milestone set` below.
    cli_ticket_command.register_cli(subparsers, common)
    milestone_parser = subparsers.add_parser(
        "milestone", parents=[common],
        help="advance active_milestone (the manual command behind the "
             "auto_next_milestone=false confirmation flow)",
    )
    milestone_subparsers = milestone_parser.add_subparsers(
        dest="milestone_command", required=True,
    )
    milestone_set_parser = milestone_subparsers.add_parser(
        "set", parents=[common],
        help="set active_milestone to one exact GitHub Milestone title: "
             "the repository policy (.github/orbi.toml) when it declares "
             "the key, otherwise the host config; a CLOSED target is "
             "refused while auto_next_milestone is true (the next idle "
             "tick would advance it away)",
    )
    milestone_set_parser.add_argument(
        "title",
        help="the exact Milestone title, e.g. v0.6.0 (must be unique)",
    )
    notify.register_cli(subparsers, common)
    return parser
