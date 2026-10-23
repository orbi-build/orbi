#!/usr/bin/env python3
"""Patch ratchet gate (Issues #789, #1230, #1856).

The test corpus may only SHRINK. The gate separates the patches that
simulate a real external boundary from the patches that reach into an
orbi module or adapter method:

* ``boundary_total`` — the subprocess/process/time/filesystem seam
  (``run_command``, ``run_gh_read_command``, ``new_run_id``, ``PROC``,
  ``Path``, ...). Reported, not failed: a boundary fake is legitimate.
* ``internal_total`` — every other ``monkeypatch.setattr`` site: an
  adapter method (``seam.edit_issue``, ``seam.comment_issue``, ...) or a
  domain function (``claim.pick_next_delivery``, ``runner.verify_pr``,
  ...). This is the coupling the ratchet hard-caps. A pure move between
  modules does not change it; a new internal patch does.
* ``command_shape_asserts`` — ``command[:N] ==`` assertions, hard-capped.

``per_target`` is printed for visibility only. Moving a patch from one
module to another never fails the gate and never creates a per-target
ceiling; only a rise in the internal total or in the command-shape count
does.

Usage: python3 tools/patch_ratchet.py [--update] [tests_dir]
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
BASELINE_FILE = Path(__file__).resolve().parent / "patch_ratchet_baseline.json"

# The historical metric: direct `setattr(runner, ...)` sites, unchanged
# so its history stays comparable. It is a reported subset, not a gate.
SETATTR_RUNNER_RE = re.compile(r"monkeypatch\.setattr\(\s*runner\s*,")
SETATTR_RUNNER_STRING_RE = re.compile(
    r"monkeypatch\.setattr\(\s*[\"']orbi\.runner\."
)
COMMAND_SHAPE_RE = re.compile(r"command\[\s*:\s*\d+\s*\]\s*==")

# One `monkeypatch.setattr(target, "name", ...)` site. The target is the
# last dotted segment (`runner.github` -> `github`); the string form is
# handled separately. Wrapped calls (target on the next line) match the
# `\s*` spans.
SETATTR_DIRECT_RE = re.compile(
    r"monkeypatch\.setattr\(\s*(?:[A-Za-z_]\w*\.)*"
    r"([A-Za-z_]\w*)\s*,\s*[\"']([^\"']+)[\"']"
)
SETATTR_STRING_RE = re.compile(
    r"monkeypatch\.setattr\(\s*[\"']orbi\.([A-Za-z_]\w*)\.([^\"']+)[\"']"
)

# Names that denote a real external boundary, whichever module binds
# them. A patch on one of these is a boundary fake, not internal
# coupling.
BOUNDARY_NAMES = frozenset({
    "run_command",
    "run_gh_read_command",
    "run_gh_write_command",
    "run_git_network_command",
    "new_run_id",
    "PROC",
    "detect",
})
# Targets that are standard-library boundaries: patch them in one
# fixture, never to reach into orbi logic.
BOUNDARY_TARGETS = frozenset({"Path", "os", "subprocess", "time"})


def counts(tests_dir: Path) -> dict[str, object]:
    """Count internal patches, boundary patches and command shapes.

    ``tests/fakes/`` is excluded: it is the adapter contract itself.
    ``*_fakes.py`` files may assert the argv contract and are excluded
    from the command-shape metric, but their runner patches still count
    internally (a runner patch dressed as a fake is still a patch).
    """
    tests_dir = Path(tests_dir)
    fakes_dir = tests_dir / "fakes"
    totals: dict[str, object] = {
        "internal_total": 0,
        "boundary_total": 0,
        "monkeypatch_setattr_runner": 0,
        "monkeypatch_setattr_runner_string": 0,
        "per_target": {},
        "command_shape_asserts": 0,
    }
    per_target: dict[str, int] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        if path.parent == fakes_dir:
            continue
        text = path.read_text(encoding="utf-8")
        totals["monkeypatch_setattr_runner"] += len(
            SETATTR_RUNNER_RE.findall(text)
        )
        totals["monkeypatch_setattr_runner_string"] += len(
            SETATTR_RUNNER_STRING_RE.findall(text)
        )
        for match in SETATTR_DIRECT_RE.finditer(text):
            target, name = match.group(1), match.group(2)
            per_target[target] = per_target.get(target, 0) + 1
            if target in BOUNDARY_TARGETS or name in BOUNDARY_NAMES:
                totals["boundary_total"] += 1
            else:
                totals["internal_total"] += 1
        for match in SETATTR_STRING_RE.finditer(text):
            target, name = match.group(1), match.group(2)
            per_target[target] = per_target.get(target, 0) + 1
            if target in BOUNDARY_TARGETS or name in BOUNDARY_NAMES:
                totals["boundary_total"] += 1
            else:
                totals["internal_total"] += 1
        if path.name.endswith("_fakes.py"):
            continue
        totals["command_shape_asserts"] += len(
            COMMAND_SHAPE_RE.findall(text)
        )
    totals["per_target"] = dict(sorted(per_target.items()))
    return totals


def read_baseline(baseline_file: Path) -> dict | None:
    """Read a valid baseline, returning ``None`` for a malformed/old one."""
    if not baseline_file.exists():
        return None
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    scalar_metrics = (
        "internal_total",
        "boundary_total",
        "monkeypatch_setattr_runner",
        "monkeypatch_setattr_runner_string",
        "command_shape_asserts",
    )
    if not isinstance(baseline, dict) or any(
        not isinstance(baseline.get(metric), int)
        for metric in scalar_metrics
    ):
        return None
    per_target = baseline.get("per_target")
    if not isinstance(per_target, dict) or any(
        not isinstance(value, int) for value in per_target.values()
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
        print(
            f"patch ratchet: tests directory not found: {tests}",
            file=sys.stderr,
        )
        return 1
    current = counts(tests)
    if update:
        baseline_file.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
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
    failures = [
        metric for metric in ("internal_total", "command_shape_asserts")
        if current[metric] > baseline[metric]
    ]
    print(
        "patch ratchet (Issues #789/#1230/#1856): "
        f"internal={current['internal_total']}/{baseline['internal_total']} "
        f"boundary={current['boundary_total']} "
        f"command[:N]=={current['command_shape_asserts']}"
        f"/{baseline['command_shape_asserts']} "
        f"runner={current['monkeypatch_setattr_runner']}"
        f"/{baseline['monkeypatch_setattr_runner']} "
        "(current/baseline — internal and command shapes may only shrink; "
        "boundary and per-target are reported)"
    )
    print(
        "patch ratchet per-target (reported, not gated): "
        + " ".join(
            f"{target}={count}"
            for target, count in sorted(
                current["per_target"].items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
    )
    if failures:
        print(
            "patch ratchet FAILED: new internal test patches are "
            "ratcheted shut (Issue #789). Move the test to tests/fakes/ "
            "or remove the patch; `--update` is only for a PR that "
            "LOWERS the internal total: "
            + ", ".join(
                f"{metric} {current[metric]} > {baseline[metric]}"
                for metric in failures
            )
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
