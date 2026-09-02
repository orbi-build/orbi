#!/usr/bin/env python3
"""Changed-code coverage gate (Issue #234).

The Python lines and branches this PR adds or modifies must be 100%
covered (line AND branch). The gate diffs HEAD against the base ref
(`git diff -U0 <base>...HEAD -- '*.py'` — three-dot: the changes on the
HEAD side since the merge base), reads the coverage.py JSON
(`coverage json`, the same numbers the report shows) and fails on any
changed statement line that is missing and on any branch arc that
starts at a changed line and is missing. A change with no modified
Python file (doc-only) passes: the gate must not invent a Python
coverage requirement for it.

Usage:  python3 diff_coverage_gate.py [base_ref]   (default: origin/main)
"""
import json
import os
import re
import subprocess
import sys

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_python_lines(base_ref: str) -> dict[str, set[int]] | None:
    """Map of repo-relative path -> set of added line numbers, from
    `git diff -U0 <base>...HEAD -- '*.py'`. Returns None when git fails
    (unknown base ref, not a repository) — the gate must fail fast,
    not pass on an unreadable diff."""
    proc = subprocess.run(
        ["git", "diff", "-U0", f"{base_ref}...HEAD", "--", "*.py"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(
            f"diff gate: `git diff -U0 {base_ref}...HEAD -- '*.py'` "
            f"failed (exit {proc.returncode}):\n{proc.stderr}",
            file=sys.stderr,
        )
        return None
    changed: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for line in proc.stdout.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        if line.startswith("--- "):
            current = None
            continue
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.setdefault(current, set()).add(new_line)
            new_line += 1
        elif not line.startswith("-") and not line.startswith("\\"):
            new_line += 1
    return changed


def coverage_files() -> dict | None:
    """The per-file section of `coverage json` (the same numbers the
    report shows). Returns None when the report cannot be produced."""
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        capture_output=True, text=True,
        env=dict(os.environ),
    )
    if proc.returncode != 0:
        print(
            f"diff gate: `coverage json` failed (exit "
            f"{proc.returncode}):\n{proc.stderr}",
            file=sys.stderr,
        )
        return None
    return json.loads(proc.stdout).get("files", {})


def main(argv: list[str]) -> int:
    base_ref = argv[1] if len(argv) > 1 else "origin/main"
    changed = changed_python_lines(base_ref)
    if changed is None:
        return 1
    if not changed:
        print(
            f"diff gate (Issue #234): no changed Python files "
            f"({base_ref}...HEAD) — doc-only change, gate passes"
        )
        return 0
    files = coverage_files()
    if files is None:
        return 1
    failures: list[str] = []
    for path in sorted(changed):
        file_report = files.get(path)
        if file_report is None:
            failures.append(
                f"{path}: no coverage data — all "
                f"{len(changed[path])} changed lines uncovered"
            )
            continue
        missing_lines = set(file_report.get("missing_lines", []))
        changed_lines = changed[path]
        for line_no in sorted(changed_lines):
            if line_no in missing_lines:
                failures.append(f"{path}:{line_no}: changed line not covered")
        for arc in file_report.get("missing_branches", []):
            from_line, to_line = arc
            if from_line in changed_lines:
                failures.append(
                    f"{path}:{from_line}: changed branch to line "
                    f"{to_line} not covered"
                )
    if failures:
        print(
            "diff gate FAILED: changed Python code is not 100% "
            f"covered ({base_ref}...HEAD):"
        )
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(
        f"diff gate (Issue #234): all changed Python lines and branches "
        f"are 100% covered ({base_ref}...HEAD)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
