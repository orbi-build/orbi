"""Guard the CONSTITUTION.md Article 2 CLI surface against drift.

Article 2 lists the public CLI commands consumers may depend on. The row
silently drifted from the registrations in `src/orbi/cli.py` (`check`,
Issue #163, and `sync-engine-source`, Issue #535, were missing). This
test asserts the one-to-one mapping at the real public surface — the
`orbi --help` output argparse generates from the registrations — so a
command added or removed in `cli.py` without amending Article 2 (or the
reverse) fails here.
"""
import re

from orbi import cli

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION = REPO_ROOT / "CONSTITUTION.md"


def _constitution_cli_commands() -> set[str]:
    """Subcommand names documented in the Article 2 CLI row."""
    text = CONSTITUTION.read_text(encoding="utf-8")
    row = next(
        line for line in text.splitlines() if line.startswith("| CLI |")
    )
    # `` `orbi` `` (bare, the Runner tick) and `` `orbi <name>` `` entries.
    found = re.findall(r"`orbi(?:\s+([a-z-]+))?`", row)
    assert "" in found, f"Article 2 lost the bare-tick `orbi` entry: {row}"
    return set(found)


def _registered_subcommands(capsys) -> set[str]:
    """Subcommands argparse actually registers, from a real `--help` call."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    usage = capsys.readouterr().out
    choices = re.search(r"\{([^}]*)\}", usage)
    assert choices, f"no subcommand choices in --help usage: {usage}"
    return set(choices.group(1).split(","))


def test_constitution_cli_row_matches_registered_subcommands(capsys):
    documented = _constitution_cli_commands()
    documented.discard("")  # the bare tick is the no-subcommand entry
    assert documented == _registered_subcommands(capsys)
