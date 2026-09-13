#!/usr/bin/env python3
"""Patch ratchet gate (Issue #789).

The test corpus may only SHRINK. The gate counts the two patterns the
constitution forbids in new tests (Article 5.1/5.2) over the test
modules:

- ``monkeypatch.setattr(runner,`` — direct patches of the runner
  module: they pin the corpus to runner.py's internal layout and are
  the reason runner.py could freeze but never shrink (Issues
  #286/#300/#785-#788);
- ``command[:N] ==`` — argv shape assertions: the command line is the
  contract only at the adapter seam (Article 5.2), not inside the
  runner's tests.

The counts are frozen in ``tools/patch_ratchet_baseline.json``. A PR
that adds either pattern fails the gate; a PR that migrates tests to
the fakes (``tests/fakes/``) lowers the counts and passes. After a
migration lands, ``--update`` re-freezes the baseline to the current
counts — the ONE deliberate command that can lower it.

Scope: every ``*.py`` under ``tests/`` EXCEPT ``tests/fakes/`` — the
fakes ARE the sanctioned alternative, and their internal command
dispatch is implementation, not assertion. The ``command[:N] ==``
metric also skips the fake-based seam modules
(``tests/test_*_fakes.py``): at the adapter seam the command line IS
the contract (Article 5.2), so their argv asserts are sanctioned —
the ``monkeypatch.setattr(runner,`` metric still counts them, so a
runner patch dressed as a fake-based test still fails.

Usage:  python3 tools/patch_ratchet.py [--update] [tests_dir]
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
BASELINE_FILE = Path(__file__).resolve().parent / "patch_ratchet_baseline.json"

# The literal the runner patches are written with (monkeypatch's
# setattr call naming the runner module as the patch target).
SETATTR_RUNNER = "monkeypatch.setattr(runner,"
# The argv shape assertion: `command[:N] == [...]` (the exact form the
# Issue names; only `==` — a shape assert, not a shape dispatch).
COMMAND_SHAPE_RE = re.compile(r"command\[\s*:\s*\d+\s*\]\s*==")

METRICS = ("monkeypatch_setattr_runner", "command_shape_asserts")


def counts(tests_dir: Path) -> dict[str, int]:
    """Count both patterns over the test corpus, excluding fakes/ and
    (for the shape metric) the fake-based seam modules."""
    totals = {metric: 0 for metric in METRICS}
    for path in sorted(tests_dir.rglob("*.py")):
        if path.parent.name == "fakes":
            continue
        text = path.read_text(encoding="utf-8")
        totals["monkeypatch_setattr_runner"] += text.count(SETATTR_RUNNER)
        if path.name.endswith("_fakes.py"):
            continue
        totals["command_shape_asserts"] += len(
            COMMAND_SHAPE_RE.findall(text)
        )
    return totals


def read_baseline(baseline_file: Path) -> dict | None:
    """Read the frozen baseline; None when it is missing or malformed —
    the gate must fail fast, never pass on unreadable evidence."""
    if not baseline_file.exists():
        return None
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(baseline, dict) or any(
        not isinstance(baseline.get(metric), int) for metric in METRICS
    ):
        return None
    return baseline


def main(argv: list[str], *, tests_dir: Path | None = None,
         baseline_file: Path = BASELINE_FILE) -> int:
    arguments = [argument for argument in argv[1:] if argument != "--update"]
    update = "--update" in argv[1:]
    tests = Path(arguments[0]) if arguments else (tests_dir or TESTS_DIR)
    current = counts(tests)
    if update:
        baseline_file.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "patch ratchet baseline frozen at "
            f"{baseline_file}: {current}"
        )
        return 0
    baseline = read_baseline(baseline_file)
    if baseline is None:
        print(
            f"patch ratchet: no readable baseline at {baseline_file} — "
            "freeze one with `python3 tools/patch_ratchet.py --update` "
            "(the current counts become the ceiling)"
        )
        return 1
    failures = [
        metric for metric in METRICS
        if current[metric] > baseline[metric]
    ]
    print(
        "patch ratchet (Issue #789): "
        "monkeypatch.setattr(runner,)="
        f"{current['monkeypatch_setattr_runner']}"
        f"/{baseline['monkeypatch_setattr_runner']} "
        f"command[:N]==={current['command_shape_asserts']}"
        f"/{baseline['command_shape_asserts']} "
        "(current/baseline — the corpus may only shrink)"
    )
    if failures:
        print(
            "patch ratchet FAILED: new test patches are ratcheted shut "
            "(Issue #789). Migrate to tests/fakes/ (state + public entry "
            "+ public surface, zero `monkeypatch.setattr(runner, ...)`) "
            "or remove the pattern; `--update` is only for a PR that "
            "LOWERS the counts: " + ", ".join(
                f"{metric} {current[metric]} > {baseline[metric]}"
                for metric in failures
            )
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
