"""The one explicit list of in-ticket command registrants (Issue #2000).

Before this module the set of registered commands depended on which
unrelated module happened to be imported: `claim.py` imported
`amend_command` and `split_command` only for their `register_command`
side effect, and `/orbi milestone` was registered by an import chain
through `orbi.milestone`. Removing one such import silently unregistered a
command from BOTH the comment dispatch and the CLI, with no error
anywhere — the maintainer's `/orbi split` comment simply got no receipt.

Now the registrant modules are named exactly once, here, and every reader
of the command set goes through `ticket_commands()`: the stopped-ticket
comment scan (`ticket_command.process_labeled_commands`, passed by
`claim.py`), the pending-Milestone notice (`orbi.milestone`) and the CLI
(`orbi.cli_ticket_command.register_cli`, `orbi.cli_commands`). So a command
cannot be missing from one spelling because an import somewhere else
changed.

The list is explicit — no plugin, no entry point, no discovery, no dynamic
import: a new command is one `CommandSpec` in its own module plus one name
in the import below. It lives in its own module rather than in
`orbi.ticket_command` because every registrant imports that layer for
`CommandSpec` / `register_command`; naming them from there would make the
package's runtime import graph cyclic (`tests/test_import_graph.py`).
"""
from __future__ import annotations

# The explicit list, in registration order: base CI red (`/orbi fix-base`),
# the clarification gate's two replies (`/orbi amend`, `/orbi split`) and
# the Milestone confirmation (`/orbi milestone <version>`). Imported for
# their registration, which `register_command` performs at import time.
from orbi import (  # noqa: F401
    base_ci_red, amend_command, milestone_command, split_command,
)
from orbi.ticket_command import (
    TICKET_COMMANDS, CommandSpec, TicketCommandRegistryError,
)


def ticket_commands() -> tuple[CommandSpec, ...]:
    """Every in-ticket command, from the one explicit list of registrants."""
    return tuple(TICKET_COMMANDS.values())


def command_by_name(name: str) -> CommandSpec:
    """The registered command with machine id ``name``."""
    for spec in ticket_commands():
        if spec.name == name:
            return spec
    raise TicketCommandRegistryError(f"no ticket command named {name!r}")


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
