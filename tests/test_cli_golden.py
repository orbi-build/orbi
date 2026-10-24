"""The CLI help surface is a golden contract (Issue #1862).

`orbi.cli` was split into `cli_parser` (the argparse tree),
`cli_commands` (one handler per subcommand), `cli_status` (the status
read/render layer) and `cli_doctor` (the doctor report) with no behavior
change. This test pins the user-visible surface byte for byte: a real
`python3 -m orbi.cli` run for `--help` and every subcommand's `--help`.
`tests/golden/cli-output.txt` was captured from the unmodified base
commit (the tree before the move), so a reworded flag, a reordered
option, a changed description or a changed exit code fails here.

`--version` is NOT part of the golden capture (Issue #1912): its output
carries the package version, which every release bump changes through
`pyproject.toml` and `orbi/__init__.py`, so a recorded copy turns a
release into a red suite. The output format is asserted against the
package version instead, in `test_version_reports_the_package_version`.

The golden run pins `COLUMNS=80` so argparse's wrapping is deterministic
outside a terminal (`shutil.get_terminal_size` fallback).
"""
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from orbi import __version__, cli, cli_commands

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden" / "cli-output.txt"

CASE_HEADER = re.compile(r"^\$ orbi(?: (.*))?$", re.MULTILINE)


def golden_cases() -> list[tuple[tuple[str, ...], str]]:
    """(argv, expected stdout) for every case recorded in the golden file."""
    text = GOLDEN.read_text(encoding="utf-8")
    matches = list(CASE_HEADER.finditer(text))
    assert matches, f"the golden file has no cases: {GOLDEN}"
    cases = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        argv = tuple(match.group(1).split()) if match.group(1) else ()
        cases.append((argv, text[match.end():end].lstrip("\n")))
    return cases


def run_cli(argv: tuple[str, ...]) -> subprocess.CompletedProcess:
    """One real CLI call, the way the compatibility entry runs it."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["COLUMNS"] = "80"
    env.pop("ORBI_CONFIG", None)
    return subprocess.run(
        [sys.executable, "-m", "orbi.cli", *argv],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize(
    "argv,expected",
    golden_cases(),
    ids=lambda value: " ".join(value) if isinstance(value, tuple) else "output",
)
def test_cli_output_is_byte_identical_to_the_golden_capture(
        argv, expected):
    """Issue #1862: the move changed no output and no exit code."""
    result = run_cli(argv)
    assert result.returncode == 0, (
        f"`orbi {' '.join(argv)}` failed rc={result.returncode}: "
        f"{result.stderr.strip()}"
    )
    assert result.stdout == expected, (
        f"`orbi {' '.join(argv)}` output changed:\n"
        f"--- expected ---\n{expected}\n--- actual ---\n{result.stdout}"
    )
    assert result.stderr == ""


VERSION_OUTPUT_LINE = re.compile(r"^orbi \d+\.\d+\.\d+$", re.MULTILINE)


def test_the_golden_capture_records_no_package_version():
    """Issue #1912: release metadata never enters the golden capture —
    re-recording a `--version` case is what turned main red on the
    v0.7.16 release commit."""
    text = GOLDEN.read_text(encoding="utf-8")
    assert not VERSION_OUTPUT_LINE.search(text), (
        "the golden capture records an `orbi <version>` output line; "
        "drop it and let test_version_reports_the_package_version pin it"
    )


def test_version_reports_the_package_version():
    """Issue #1912: `--version` prints the package version, not a golden
    string — a release bump changes it without failing any test, while a
    changed output format still fails here."""
    result = run_cli(("--version",))
    assert result.returncode == 0, (
        f"`orbi --version` failed rc={result.returncode}: "
        f"{result.stderr.strip()}"
    )
    assert result.stdout == f"orbi {__version__}\n"
    assert result.stderr == ""


CLI_MODULES = (
    "cli.py",
    "cli_parser.py",
    "cli_commands.py",
    "cli_status.py",
    "cli_doctor.py",
)

# The Issue's structural acceptance: the entry module and every extracted
# module stay readable in one screen region, and `main` is a dispatcher.
MODULE_LINE_LIMIT = 400
MAIN_LINE_LIMIT = 100


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_the_cli_modules_stay_inside_the_size_limits():
    """Issue #1862 acceptance: `cli.py` < 400 lines, `main` < 100 lines,
    every extracted module < 400 lines — the move really is a split, not a
    rename."""
    package = REPO_ROOT / "src" / "orbi"
    for name in CLI_MODULES:
        size = line_count(package / name)
        assert size < MODULE_LINE_LIMIT, (
            f"{name} is {size} lines (limit {MODULE_LINE_LIMIT}); extract "
            "the next coherent domain instead of growing the module"
        )
    main_size = len(inspect.getsource(cli.main).splitlines())
    assert main_size < MAIN_LINE_LIMIT, (
        f"cli.main is {main_size} lines (limit {MAIN_LINE_LIMIT}); it is a "
        "dispatcher, not the command implementations"
    )


def test_the_golden_capture_covers_every_registered_subcommand(capsys):
    """A new subcommand cannot appear without a recorded help surface."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    usage = capsys.readouterr().out
    choices = re.search(r"\{([^}]*)\}", usage)
    assert choices, f"no subcommand choices in --help usage: {usage}"
    recorded = {argv[0] for argv, _ in golden_cases() if argv}
    missing = set(choices.group(1).split(",")) - recorded
    assert not missing, f"subcommands without a golden help capture: {missing}"


def test_every_subcommand_dispatches_to_exactly_one_handler(capsys):
    """The handler table is the dispatch contract: one handler per
    subcommand (`check` runs before the config load and is dispatched by
    the entry module, so it has no table entry)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    usage = capsys.readouterr().out
    choices = re.search(r"\{([^}]*)\}", usage)
    assert choices, f"no subcommand choices in --help usage: {usage}"
    registered = set(choices.group(1).split(","))
    assert set(cli_commands.COMMAND_HANDLERS) | {"check"} == registered
