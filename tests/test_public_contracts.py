"""The public interface inventory and the two contract specifications (Issue #1655).

Article 2 of the Constitution keeps the principle (a consumer may rely
only on officially documented public contracts) and points at the docs
page; the authoritative inventory and the exact status/progress formats
live in `docs/public-contracts.mdx`, linked from both READMEs. These
tests pin the moved CLI row against the real argparse registrations (the
drift guard moved here with the row) and pin the status observation and
progress summary contracts, including the worked examples, so the
dependent runtime tickets (#1656/#1657) have an executable reference.
"""
import re

from orbi import cli

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_CONTRACTS = REPO_ROOT / "docs" / "public-contracts.mdx"
ZH_PUBLIC_CONTRACTS = REPO_ROOT / "docs" / "zh" / "public-contracts.mdx"
README = REPO_ROOT / "README.md"
ZH_README = REPO_ROOT / "README.zh-CN.md"

# The five worked examples the acceptance criteria require.
WORKED_EXAMPLES = (
    "Concurrent Issues",
    "Retained session",
    "New run without a summary",
    "Unsupported or corrupt report",
    "Terminal or manual state wins",
)


def public_contracts_text() -> str:
    assert PUBLIC_CONTRACTS.is_file(), f"missing docs page: {PUBLIC_CONTRACTS}"
    return PUBLIC_CONTRACTS.read_text(encoding="utf-8")


def _cli_row(text: str) -> str:
    row = next(
        (line for line in text.splitlines() if line.startswith("| CLI |")),
        None,
    )
    assert row is not None, (
        "docs/public-contracts.mdx lost the public CLI inventory row"
    )
    return row


def _documented_commands(text: str) -> set[str]:
    """Subcommand names documented in the public contracts CLI row."""
    found = re.findall(r"`orbi(?:\s+([a-z-]+))?`", _cli_row(text))
    assert "" in found, f"the CLI row lost the bare-tick `orbi` entry: {_cli_row(text)}"
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


def test_public_contracts_cli_row_matches_registered_subcommands(capsys):
    """The moved inventory must still name exactly the subcommands the
    real `orbi --help` offers (the drift guard that used to read
    CONSTITUTION.md Article 2)."""
    documented = _documented_commands(public_contracts_text())
    documented.discard("")  # the bare tick is the no-subcommand entry
    assert documented == _registered_subcommands(capsys)


def test_public_contracts_is_the_linked_inventory_entry_point():
    """Both READMEs link the authoritative page, one language each."""
    assert "docs/public-contracts.mdx" in README.read_text(encoding="utf-8"), (
        "the English README must link docs/public-contracts.mdx"
    )
    assert "docs/zh/public-contracts.mdx" in ZH_README.read_text(encoding="utf-8"), (
        "the Chinese README must link docs/zh/public-contracts.mdx"
    )


def test_public_contracts_page_distinguishes_invariants_from_specifications():
    """The page states the three responsibilities: invariants in the
    Constitution, interface specifications here, private implementation
    unsupported."""
    text = public_contracts_text()
    assert "architectural invariants" in text
    assert "interface specification" in text
    assert "private implementation" in text
    assert "not a constitutional amendment" in text


def test_status_contract_documents_scope_attribution_and_cycles():
    """Scope (--follow vs --repo/--issue), attribution (the full worktree
    naming convention, session/activity records) and the two read cycles
    (0.5 s local, 15 s GitHub) are specified."""
    text = public_contracts_text()
    for marker in (
        "`--follow`",
        "`--repo OWNER/REPO`",
        "`--issue N`",
        "observation key",
        ".worktrees/orbi-acme-shop-issue-42-86b57234",
        "86b57234",
        "mtime",
        "0.5-second",
        "15 seconds",
        "no arbitrary 1/50/200 total limit",
        "Ctrl-C",
        "exits nonzero",
        "`RUNNER_RUNTIME_EXCLUDES`",
    ):
        assert marker in text, f"status contract misses: {marker!r}"


def test_progress_contract_documents_block_activity_and_selection():
    """The progress block (marker + versioned payload), the required
    keys with their null rules, the activity kinds and the report
    selection classifications are specified."""
    text = public_contracts_text()
    for marker in (
        "orbi:progress:v1",
        "`step`",
        "`reported_at`",
        "`activity`",
        "UTC ISO-8601",
        "`read`, `edit`, `command` or `analysis`",
        '"activity": null',
        "`missing`",
        "`unsupported`",
        "`invalid`",
        "`available`",
        "best effort",
    ):
        assert marker in text, f"progress contract misses: {marker!r}"


def test_progress_contract_worked_examples_cover_the_required_scenes():
    """The acceptance requires worked examples for concurrent Issues, a
    retained session, a new run without a summary, an unsupported/corrupt
    report, and terminal/manual precedence."""
    text = public_contracts_text()
    for heading in WORKED_EXAMPLES:
        assert f"### {heading}" in text, (
            f"missing worked example: {heading!r}"
        )


def test_chinese_page_mirrors_the_contract_and_structure():
    """The ZH mirror carries the same headings, fenced-code languages and
    language-neutral machine facts as the English page (the i18n structure
    test covers `##` and fences; `###` is checked here because the worked
    examples are the translation-completeness risk)."""
    assert ZH_PUBLIC_CONTRACTS.is_file(), (
        f"missing Chinese docs page: {ZH_PUBLIC_CONTRACTS}"
    )
    english = public_contracts_text()
    chinese = ZH_PUBLIC_CONTRACTS.read_text(encoding="utf-8")
    assert len(re.findall(r"^### ", english, re.MULTILINE)) == len(
        re.findall(r"^### ", chinese, re.MULTILINE)
    ), "EN/ZH public-contracts `###` heading counts must match"
    for marker in (
        "orbi:progress:v1",
        "`--repo OWNER/REPO`",
        "`--issue N`",
        ".worktrees/orbi-acme-shop-issue-42-86b57234",
        "0.5",
        "15",
    ):
        assert marker in chinese, f"Chinese page misses: {marker!r}"
