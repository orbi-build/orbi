#!/usr/bin/env python3
"""Size ratchet for the Article 3 source-file limits (Issue #1229).

The frozen baseline records the number of top-level definitions in
``runner.py`` and the line counts of modules already over 1,000 lines.
Those values may only shrink. A new module may not cross the 1,000-line
cap without first being split by domain.

Usage: python3 tools/size_ratchet.py [--update] [src_dir]
"""
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "orbi"
BASELINE_FILE = Path(__file__).resolve().parent / "size_ratchet_baseline.json"
SIZE_CAP = 1000


def counts(src_dir: Path) -> dict[str, object]:
    """Measure runner's direct definitions and every oversized Python module."""
    src_dir = Path(src_dir)
    runner = src_dir / "runner.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"), filename=str(runner))
    runner_top_level_defs = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in tree.body
    )
    oversized_modules = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(src_dir.glob("*.py"))
        if len(path.read_text(encoding="utf-8").splitlines()) > SIZE_CAP
    }
    return {
        "runner_top_level_defs": runner_top_level_defs,
        "oversized_modules": oversized_modules,
    }


def read_baseline(baseline_file: Path) -> dict[str, object] | None:
    """Read a valid baseline, returning None for missing or malformed input."""
    if not baseline_file.exists():
        return None
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    oversized = baseline.get("oversized_modules") if isinstance(baseline, dict) else None
    if (
        not isinstance(baseline, dict)
        or type(baseline.get("runner_top_level_defs")) is not int
        or not isinstance(oversized, dict)
        or any(
            not isinstance(name, str) or type(lines) is not int
            for name, lines in oversized.items()
        )
    ):
        return None
    return baseline


def main(argv: list[str], *, src_dir: Path | None = None,
         baseline_file: Path = BASELINE_FILE) -> int:
    arguments = [argument for argument in argv[1:] if argument != "--update"]
    update = "--update" in argv[1:]
    if len(arguments) > 1:
        print(
            "size ratchet: expected at most one src_dir argument, "
            f"got {arguments!r}",
            file=sys.stderr,
        )
        return 1
    source = Path(arguments[0]) if arguments else (src_dir or SRC_DIR)
    if not source.is_dir():
        print(f"size ratchet: source directory not found: {source}", file=sys.stderr)
        return 1
    try:
        current = counts(source)
    except (FileNotFoundError, OSError, SyntaxError) as error:
        print(f"size ratchet: could not measure {source}: {error}", file=sys.stderr)
        return 1
    if update:
        baseline_file.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"size ratchet baseline frozen at {baseline_file}: {current}")
        return 0
    baseline = read_baseline(baseline_file)
    if baseline is None:
        print(
            f"size ratchet: no readable baseline at {baseline_file} — "
            "freeze one with `python3 tools/size_ratchet.py --update` "
            "(the current values become the ceilings)"
        )
        return 1

    current_defs = current["runner_top_level_defs"]
    baseline_defs = baseline["runner_top_level_defs"]
    failures: list[tuple[str, int, int]] = []
    if current_defs > baseline_defs:
        failures.append(("runner_top_level_defs", current_defs, baseline_defs))

    current_modules = current["oversized_modules"]
    baseline_modules = baseline["oversized_modules"]
    for name, value in current_modules.items():
        ceiling = baseline_modules.get(name, SIZE_CAP)
        if value > ceiling:
            failures.append((name, value, ceiling))

    print(
        "size ratchet (Issue #1229): "
        f"runner_top_level_defs={current_defs}/{baseline_defs} "
        f"oversized_modules={current_modules!r} "
        "(current/baseline ceilings)"
    )
    if failures:
        print("size ratchet FAILED:")
        for name, value, ceiling in failures:
            print(
                f"  {name}: current {value} > ceiling {ceiling}; "
                "extract code into a sibling module, or, for a deliberate "
                "reduction, run `python3 tools/size_ratchet.py --update`"
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
