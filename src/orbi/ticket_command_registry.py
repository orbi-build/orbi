"""The one explicit declaration of the in-ticket commands (Issues #2000, #2033).

Before #2000 the set of registered commands depended on which unrelated
module happened to be imported: `claim.py` imported `amend_command` and
`split_command` only for their `register_command` side effect, and
`/orbi milestone` was registered by an import chain through
`orbi.milestone`. Removing one such import silently unregistered a
command from BOTH the comment dispatch and the CLI, with no error
anywhere — the maintainer's `/orbi split` comment simply got no receipt.

#2000 named the registrant modules here but each module still registered
itself at import, so importing a command module for any other reason still
added a command. Now `COMMAND_SPECS` below IS the declaration: this module
imports the four `CommandSpec` objects and registers them in one loop, and
every reader of the command set goes through `ticket_commands()`: the
stopped-ticket comment scan (`ticket_command.process_labeled_commands`,
passed by `claim.py`), the pending-Milestone notice (`orbi.milestone`) and
the CLI (`orbi.cli_ticket_command.register_cli`, `orbi.cli_commands`). So a
command cannot be missing from one spelling because an import somewhere
else changed, and `command_by_name` is a dict lookup.

The list is explicit — no plugin, no entry point, no discovery, no dynamic
import: a new command is one `CommandSpec` in its own module plus one entry
in `COMMAND_SPECS`. It lives in its own module rather than in
`orbi.ticket_command` because every registrant imports that layer for
`CommandSpec`; naming them from there would make the package's runtime
import graph cyclic (`tests/test_import_graph.py`).
"""
from __future__ import annotations

from orbi.amend_command import AMEND_COMMAND
from orbi.base_ci_red import FIX_BASE_COMMAND
from orbi.milestone_command import MILESTONE_COMMAND
from orbi.split_command import SPLIT_COMMAND
from orbi.ticket_command import (
    TICKET_COMMANDS, CommandSpec, TicketCommandRegistryError, register_command,
)

# The declared command list, in registration order: base CI red
# (`/orbi fix-base`), the clarification gate's two replies (`/orbi amend`,
# `/orbi split`) and the Milestone confirmation
# (`/orbi milestone <version>`).
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    FIX_BASE_COMMAND,
    AMEND_COMMAND,
    MILESTONE_COMMAND,
    SPLIT_COMMAND,
)


def _register_all() -> None:
    """Register the declared specs; `register_command` is the validation."""
    for spec in COMMAND_SPECS:
        register_command(spec)


_register_all()


def ticket_commands() -> tuple[CommandSpec, ...]:
    """Every in-ticket command, from the one explicit declaration."""
    return tuple(TICKET_COMMANDS.values())


def command_by_name(name: str) -> CommandSpec:
    """The registered command with machine id ``name`` — a dict lookup."""
    try:
        return TICKET_COMMANDS[name]
    except KeyError:
        raise TicketCommandRegistryError(
            f"no ticket command named {name!r}"
        ) from None


def cli_specs() -> list[CommandSpec]:
    """The registered commands that also get a CLI subcommand.

    Arity-0 only (Issue #1930): the generated entry takes `--repo` and
    `--issue` and no argument, so a command that needs one — `/orbi
    milestone <version>` — keeps its own explicit CLI command (`orbi
    milestone set`). Registering a command therefore gives both spellings
    without a hand edit: this list and the comment dispatch read the same
    `ticket_commands()` (Issue #2000), and a caller needs no import of its
    own to see a command.
    """
    return [spec for spec in ticket_commands() if spec.arity == 0]
