#!/usr/bin/env python3
"""Patch ratchet gate (Issue #789).

The test corpus may only SHRINK. The gate counts forbidden internal patches
and command-shape assertions. Besides the historical runner metric, package
module targets are discovered from ``src/orbi/*.py`` at runtime (excluding
``runner`` and ``__init__``), so moving code cannot hide test coupling.

Usage: python3 tools/patch_ratchet.py [--update] [tests_dir]
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
BASELINE_FILE = Path(__file__).resolve().parent / "patch_ratchet_baseline.json"

# Kept unchanged so the historical metric remains comparable.
SETATTR_RUNNER_RE = re.compile(r"monkeypatch\.setattr\(\s*runner\s*,")
SETATTR_RUNNER_STRING_RE = re.compile(
    r"monkeypatch\.setattr\(\s*[\"']orbi\.runner\."
)
COMMAND_SHAPE_RE = re.compile(r"command\[\s*:\s*\d+\s*\]\s*==")
MODULE_STEMS = tuple(sorted(
    path.stem for path in (REPO_ROOT / "src" / "orbi").glob("*.py")
    if path.stem not in {"runner", "__init__"}
))
MODULE_ATTRIBUTE_RES = {
    stem: re.compile(
        rf"monkeypatch\.setattr\(\s*(?:[A-Za-z_]\w*\.)*{re.escape(stem)}\s*,"
    )
    for stem in MODULE_STEMS
}
MODULE_STRING_RES = {
    stem: re.compile(
        rf"monkeypatch\.setattr\(\s*[\"']orbi\.{re.escape(stem)}\."
    )
    for stem in MODULE_STEMS
}


def counts(tests_dir: Path) -> dict[str, int | dict[str, int]]:
    """Count ratcheted patterns, excluding the top-level ``tests/fakes/``."""
    tests_dir = Path(tests_dir)
    fakes_dir = tests_dir / "fakes"
    totals = {
        "monkeypatch_setattr_runner": 0,
        "monkeypatch_setattr_runner_string": 0,
        "monkeypatch_setattr_modules": {stem: 0 for stem in MODULE_STEMS},
        "command_shape_asserts": 0,
    }
    for path in sorted(tests_dir.rglob("*.py")):
        if path.parent == fakes_dir:
            continue
        text = path.read_text(encoding="utf-8")
        totals["monkeypatch_setattr_runner"] += len(SETATTR_RUNNER_RE.findall(text))
        totals["monkeypatch_setattr_runner_string"] += len(
            SETATTR_RUNNER_STRING_RE.findall(text)
        )
        modules = totals["monkeypatch_setattr_modules"]
        for stem in MODULE_STEMS:
            modules[stem] += (
                len(MODULE_ATTRIBUTE_RES[stem].findall(text))
                + len(MODULE_STRING_RES[stem].findall(text))
            )
        if path.name.endswith("_fakes.py"):
            continue
        totals["command_shape_asserts"] += len(COMMAND_SHAPE_RE.findall(text))
    return totals


def read_baseline(baseline_file: Path) -> dict | None:
    """Read a valid nested baseline, returning ``None`` if malformed."""
    if not baseline_file.exists():
        return None
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    scalar_metrics = (
        "monkeypatch_setattr_runner",
        "monkeypatch_setattr_runner_string",
        "command_shape_asserts",
    )
    if not isinstance(baseline, dict) or any(
        not isinstance(baseline.get(metric), int) for metric in scalar_metrics
    ):
        return None
    modules = baseline.get("monkeypatch_setattr_modules")
    if not isinstance(modules, dict) or any(
        not isinstance(value, int) for value in modules.values()
    ):
        return None
    return baseline


def main(argv: list[str], *, tests_dir: Path | None = None,
         baseline_file: Path = BASELINE_FILE) -> int:
    arguments = [argument for argument in argv[1:] if argument != "--update"]
    update = "--update" in argv[1:]
    if len(arguments) > 1:
        print(
            "patch ratchet: expected at most one tests_dir argument, "
            f"got {arguments!r}", file=sys.stderr,
        )
        return 1
    tests = Path(arguments[0]) if arguments else (tests_dir or TESTS_DIR)
    if not tests.is_dir():
        print(f"patch ratchet: tests directory not found: {tests}", file=sys.stderr)
        return 1
    current = counts(tests)
    if update:
        baseline_file.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"patch ratchet baseline frozen at {baseline_file}: {current}")
        return 0
    baseline = read_baseline(baseline_file)
    if baseline is None:
        print(
            f"patch ratchet: no readable baseline at {baseline_file} — "
            "freeze one with `python3 tools/patch_ratchet.py --update` "
            "(the current counts become the ceiling)"
        )
        return 1
    scalar_failures = [
        metric for metric in (
            "monkeypatch_setattr_runner",
            "monkeypatch_setattr_runner_string",
            "command_shape_asserts",
        ) if current[metric] > baseline[metric]
    ]
    module_failures = [
        stem for stem, count in current["monkeypatch_setattr_modules"].items()
        if count > baseline["monkeypatch_setattr_modules"].get(stem, 0)
    ]
    print(
        "patch ratchet (Issue #789): "
        f"monkeypatch.setattr(runner,)={current['monkeypatch_setattr_runner']}"
        f"/{baseline['monkeypatch_setattr_runner']} "
        f"runner strings={current['monkeypatch_setattr_runner_string']}"
        f"/{baseline['monkeypatch_setattr_runner_string']} "
        f"command[:N]==={current['command_shape_asserts']}"
        f"/{baseline['command_shape_asserts']} "
        "(current/baseline — the corpus may only shrink)"
    )
    if scalar_failures or module_failures:
        failures = [
            f"{metric} {current[metric]} > {baseline[metric]}"
            for metric in scalar_failures
        ] + [
            f"{stem} {current['monkeypatch_setattr_modules'][stem]} > "
            f"{baseline['monkeypatch_setattr_modules'].get(stem, 0)}"
            for stem in module_failures
        ]
        print(
            "patch ratchet FAILED: new test patches are ratcheted shut "
            "(Issue #789). Migrate to tests/fakes/ or remove the pattern; "
            "`--update` is only for a PR that LOWERS the counts: "
            + ", ".join(failures)
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
