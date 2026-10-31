"""The ticket commands are declared in one explicit list (Issue #2000).

Before this ticket the registered set depended on which unrelated module
happened to be imported: `claim.py` imported `amend_command` and
`split_command` only for their `register_command` side effect, and
`/orbi milestone` was registered only because `claim.py` imported
`orbi.milestone`, which imported `milestone_command`. Removing one such
import silently unregistered a command from BOTH the comment dispatch and
the CLI, with no error anywhere — the user's `/orbi split` comment simply
got no receipt.

So the set is read from ONE explicit list
(`ticket_command_registry.ticket_commands()`), and these tests read it from
a FRESH interpreter: a command missing from the list, or a registration
that still depends on an unrelated import, fails here. The CLI assertions
build the real argparse tree the host runs.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orbi import ticket_command, ticket_command_registry
from orbi.cli_parser import build_parser

REPO = "owner/repo"
# The four in-ticket commands, by machine id: `amend` (`/orbi amend`),
# `fix_base` (`/orbi fix-base`), `milestone` (`/orbi milestone <version>`)
# and `split` (`/orbi split`).
COMMAND_NAMES = "amend,fix_base,milestone,split"
# Their subcommands, minus the arity-1 one that keeps `orbi milestone set`
# instead of a generated entry (`ticket_command_registry.cli_specs`).
GENERATED_SUBCOMMANDS = "amend,fix-base,split"

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str) -> list[str]:
    """Run `code` in a fresh interpreter and return its stdout lines."""
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=ROOT, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_the_explicit_list_yields_the_four_commands():
    """Acceptance 1: the explicit list IS the comment dispatch's command set.

    The fresh process imports only the dispatch layer and the list — never
    `orbi.claim` or `orbi.cli`. The registry is empty until the list module
    is imported (no unrelated import fills it), and the list names all four
    commands.
    """
    lines = _run(
        "import orbi.ticket_command as ticket_command\n"
        "print(len(ticket_command.TICKET_COMMANDS))\n"
        "from orbi import ticket_command_registry\n"
        "print(','.join(sorted("
        "spec.name for spec in ticket_command_registry.ticket_commands())))\n"
    )
    assert lines == ["0", COMMAND_NAMES]


def test_each_dispatch_entry_point_alone_yields_the_four_commands():
    """Acceptance 1, verbatim: importing the dispatch alone sees all four.

    `orbi.claim` is what the tick imports to run the stopped-ticket comment
    scan, and `orbi.milestone` what it imports to run the Milestone
    confirmation notice — the two comment dispatch call sites
    `process_commands(...)`/`process_labeled_commands(...)`. A fresh
    process imports each one alone and reads the registry: neither may come
    up short because of which unrelated import happened to run.
    """
    lines = _run(
        "import orbi.claim\n"
        "from orbi import ticket_command\n"
        "print(','.join(sorted(ticket_command.TICKET_COMMANDS)))\n"
        "import orbi.milestone\n"
        "print(','.join(sorted(ticket_command.TICKET_COMMANDS)))\n"
    )
    assert lines == [COMMAND_NAMES, COMMAND_NAMES]


def test_registration_does_not_depend_on_the_imports_that_used_to_do_it():
    """Acceptance 2: the imports this ticket removed cannot change the set.

    `claim.py` no longer imports `amend_command`/`split_command` for
    registration, and `milestone.py`'s `milestone_command` import is no
    longer what registers `/orbi milestone`. The fresh process never
    imports `orbi.claim` or `orbi.milestone` and still gets the four.
    """
    lines = _run(
        "import sys\n"
        "from orbi import ticket_command_registry\n"
        "names = ','.join(sorted("
        "spec.name for spec in ticket_command_registry.ticket_commands()))\n"
        "print(names)\n"
        "print('orbi.claim' in sys.modules)\n"
        "print('orbi.milestone' in sys.modules)\n"
    )
    assert lines == [COMMAND_NAMES, "False", "False"]


def test_the_comment_dispatch_has_no_ambient_default():
    """The stopped-ticket scan is handed the list, never a hidden global.

    `process_labeled_commands` used to default to "whatever the registry
    holds", which is exactly the import-order dependence Issue #2000
    removes: its caller now passes the explicit list.
    """
    parameter = inspect.signature(
        ticket_command.process_labeled_commands,
    ).parameters["commands"]
    assert parameter.default is inspect.Parameter.empty


def test_the_cli_exposes_the_same_four_commands():
    """Acceptance 1, second half: `orbi <command>` covers the same four.

    A fresh process builds the real parser: the arity-0 commands are the
    generated `orbi <subcommand>` entries, and the arity-1
    `/orbi milestone <version>` keeps its explicit `orbi milestone set`.
    """
    lines = _run(
        "from orbi import cli_commands\n"
        "from orbi.cli_parser import build_parser\n"
        "parser = build_parser('test')\n"
        "written = sorted(\n"
        "    name for name, handler in cli_commands.COMMAND_HANDLERS.items()\n"
        "    if handler is cli_commands.command_ticket_command\n"
        ")\n"
        "print(','.join(written))\n"
        "print(parser.parse_args("
        "['milestone', 'set', 'v9.9.9']).milestone_command)\n"
    )
    assert lines == [GENERATED_SUBCOMMANDS, "set"]


def test_an_unknown_command_name_fails_fast():
    """The CLI reaches a command through the list, never the raw registry:
    an unknown name is a named registry error, not a bare KeyError."""
    with pytest.raises(
        ticket_command.TicketCommandRegistryError, match="no ticket command",
    ):
        ticket_command_registry.command_by_name("no-such-command")


def test_the_cli_subcommands_are_the_registered_arity_zero_commands():
    """The generated CLI entries ARE the explicit list, not a hand copy."""
    parser = build_parser("test")
    generated = sorted(
        spec.subcommand
        for spec in ticket_command_registry.ticket_commands()
        if spec.arity == 0
    )
    assert ",".join(generated) == GENERATED_SUBCOMMANDS
    for subcommand in generated:
        args = parser.parse_args([
            subcommand, "--repo", REPO, "--issue", "7",
        ])
        assert args.command == subcommand
