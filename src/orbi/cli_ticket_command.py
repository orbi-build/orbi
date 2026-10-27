"""`orbi <command>` — a registered ticket command from the terminal (Issue #1930).

A ticket command has two spellings and ONE execution path: a
`/orbi <subcommand>` comment on the Issue, and `orbi <subcommand> --repo
<owner/name> --issue <N>` on the host. This module is the terminal one:

- `register_cli` adds one subcommand per registered arity-0 command
  (`ticket_command.cli_specs`), so registering a command gives both
  spellings and there is nothing to edit here. A command that needs an
  argument (`/orbi milestone <version>`) keeps its own explicit CLI command
  (`orbi milestone set`) and is never generated;
- `command_ticket_command` runs the SAME `ticket_command.execute_command`
  the comment scan runs, with the host's authenticated `gh` login as the
  one occurrence: the same repository-permission gate, the same
  `validate`, the same `apply` and exactly one receipt on the ticket. The
  per-repo context (message language, Milestone, dispatch label) is
  resolved exactly as the tick resolves it, so both spellings produce the
  same ticket result.

Exit code 0 when the command applied, non-zero with the receipt text on
stderr when it was rejected or failed at a step.
"""
from __future__ import annotations

import sys

# The registrants, imported for their registration side effect: a command
# is registered at import time and both the tree below and the CLI
# dispatch table are derived from the registry (Issue #1930). A new
# command module is added here once, exactly like the comment scan's call
# site imports it.
from orbi import base_ci_red, split_command, ticket_command  # noqa: F401
from orbi.claim import _repo_scan_keys
from orbi.github import authenticated_github_login
from orbi.repo_config import merged_repo_language


def register_cli(subparsers, common) -> None:
    """Add one subcommand per registered arity-0 ticket command."""
    for spec in ticket_command.cli_specs():
        command = subparsers.add_parser(
            spec.subcommand, parents=[common],
            help=f"run the registered `{spec.canonical}` ticket command on "
                 "one Issue: the same rules and one receipt as the comment",
        )
        command.add_argument(
            "--repo", required=True,
            help="the Issue's repository (owner/name)",
        )
        command.add_argument(
            "--issue", type=int, required=True,
            help="the number of the Issue the command acts on",
        )
        command.set_defaults(ticket_command=spec.name)


def command_ticket_command(config, args, parser) -> int:
    """Run one registered ticket command on one Issue (Issue #1930)."""
    spec = ticket_command.TICKET_COMMANDS[args.ticket_command]
    repo = args.repo
    milestone, dispatch_label = _repo_scan_keys(
        config, repo, config.active_milestone,
    )
    outcome = ticket_command.execute_command(
        repo, args.issue, spec,
        caller=ticket_command.cli_caller(authenticated_github_login()),
        language_code=merged_repo_language(config, repo),
        milestone=milestone, dispatch_label=dispatch_label,
    )
    if outcome.receipt:
        print(outcome.receipt, file=sys.stderr)
    return 0 if outcome.applied else 1
