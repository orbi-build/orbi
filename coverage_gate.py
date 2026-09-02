#!/usr/bin/env python3
"""Global tiered coverage gate (Issue #234).

The repository contract is tiered (docs/testing.mdx): the whole
repository keeps line >= 95% AND branch >= 95% — checked SEPARATELY,
never a single merged percentage — while the changed Python code and
the core state machines keep 100% (the diff gate and the existing test
suite enforce those tiers).

Usage:  python3 coverage_gate.py [data_file]

Reads the coverage.py JSON totals (`coverage json` — the same numbers
the report shows) and checks the two tiers SEPARATELY:

- line:   `totals.percent_statements_covered` (covered lines /
  statements — NOT `totals.percent_covered`, which is the merged
  line+branch percentage the old gate checked);
- branch: `totals.percent_branches_covered` (covered branches /
  branches).

It prints both real numbers and exits non-zero when EITHER tier is
below 95%. A missing data file fails fast: the gate must never pass on
the absence of coverage evidence.
"""
import json
import os
import subprocess
import sys

LINE_GATE = 95.0
BRANCH_GATE = 95.0


def main(argv: list[str]) -> int:
    data_file = argv[1] if len(argv) > 1 else ".coverage"
    if not os.path.exists(data_file):
        print(
            f"coverage gate: no coverage data at {data_file!r} — run the "
            "contract test command first "
            "(`coverage run --branch -m pytest tests/ -q`)"
        )
        return 1
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        capture_output=True, text=True,
        env=dict(os.environ, COVERAGE_FILE=data_file),
    )
    if proc.returncode != 0:
        print(
            f"coverage gate: `coverage json` failed (exit "
            f"{proc.returncode}):\n{proc.stderr}",
            file=sys.stderr,
        )
        return 1
    totals = json.loads(proc.stdout)["totals"]
    # percent_statements_covered is the pure LINE percentage; the JSON
    # field `percent_covered` is the merged line+branch percentage and
    # must not be used (Issue #234: the tiers are checked separately).
    line = totals["percent_statements_covered"]
    branch = totals["percent_branches_covered"]
    print(
        f"coverage gate (Issue #234): line={line:.2f}% "
        f"branch={branch:.2f}% "
        f"(gates: line>={LINE_GATE:.0f}% and branch>={BRANCH_GATE:.0f}%, "
        "checked separately)"
    )
    failures = []
    if line < LINE_GATE:
        failures.append(f"line {line:.2f}% < {LINE_GATE:.0f}%")
    if branch < BRANCH_GATE:
        failures.append(f"branch {branch:.2f}% < {BRANCH_GATE:.0f}%")
    if failures:
        print("coverage gate FAILED: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
