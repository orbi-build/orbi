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

The coverage configuration's `run.omit` patterns drive the measured set
(Issue #1501): a changed file the configuration omits (the
`bench/oss-upstream/` developer harness, never imported by the suite) is
skipped instead of demanding 100% for code the contract deliberately does
not measure. The decision lives in the configuration, not in a path list
hardcoded here.

Usage:  python3 tools/diff_coverage_gate.py [base_ref]   (default: origin/main)
"""
import fnmatch
import json
import os
import re
import subprocess
import sys

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_python_lines(base_ref: str) -> dict[str, set[int]] | None:
    """Map of repo-relative path -> set of added line numbers, from
    `git diff -U0 <base_ref>...HEAD -- '*.py'`. Returns None when git
    fails (unknown base ref, not a repository) — the gate must fail
    fast, not pass on an unreadable diff.

    The parser is a two-state machine over the diff grammar, because a
    content line may itself start with `+` (a docstring quoting the
    `+++ b/...` header syntax renders as `+++ note ...` in the diff):
    the `--- `/`+++ ` file headers are recognized only in the header
    region (right after a `--- ` line), and once a `@@` hunk starts
    every leading `+` is an added content line, never a header. The old
    prefix matching let one such line drop the rest of the file
    (silent pass) or credit it to a ghost file.
    """
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
    expect_target_header = False
    in_hunk = False
    new_line = 0
    for line in proc.stdout.splitlines():
        if in_hunk:
            if line.startswith("diff --git"):
                # The next file's header region starts here.
                in_hunk = False
                current = None
                expect_target_header = False
                continue
            if line.startswith("@@"):
                # A second hunk of the same file resets the numbering.
                match = HUNK_RE.match(line)
                if match:
                    new_line = int(match.group(1))
                continue
            if line.startswith("+"):
                if current is not None:
                    changed.setdefault(current, set()).add(new_line)
                new_line += 1
            elif line.startswith("-"):
                pass  # removed line: not recorded, does not advance
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                new_line += 1  # context line
            continue
        if line.startswith("--- "):
            expect_target_header = True
            current = None
            continue
        if line.startswith("+++ ") and expect_target_header:
            expect_target_header = False
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        match = HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            continue
    return changed


def omitted_patterns() -> list[str]:
    """The coverage configuration's `run.omit` patterns.

    Read through the coverage API so the gate and `coverage run` share one
    decision (pyproject.toml / .coveragerc — whatever coverage itself
    reads). A configuration that cannot be read fails fast: the gate must
    not silently fall back to requiring coverage for omitted code.
    """
    from coverage import Coverage
    return list(Coverage().config.run_omit)


def apply_omits(
    changed: dict[str, set[int]], patterns: list[str],
) -> tuple[dict[str, set[int]], list[str]]:
    """Drop changed files matching an `run.omit` pattern.

    Returns the remaining map and the sorted paths that were skipped, so
    the gate can name what it left out (evidence, never a silent pass).
    With no configured pattern the map is returned unchanged.
    """
    if not patterns:
        return changed, []
    kept: dict[str, set[int]] = {}
    skipped: list[str] = []
    for path, lines in changed.items():
        if any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
            skipped.append(path)
        else:
            kept[path] = lines
    return kept, sorted(skipped)


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
    changed, omitted = apply_omits(changed, omitted_patterns())
    if omitted:
        print(
            f"diff gate (Issue #234): skipped coverage-omitted files "
            f"({base_ref}...HEAD): " + ", ".join(omitted)
        )
    if not changed:
        detail = (
            "all changed Python files are coverage-omitted"
            if omitted else "doc-only change"
        )
        print(
            f"diff gate (Issue #234): no changed Python files "
            f"({base_ref}...HEAD) — {detail}, gate passes"
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
