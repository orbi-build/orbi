#!/usr/bin/env python3
"""Size ratchet for the Article 3 source-file limits (Issues #1229, #2007).

The frozen baseline records three metrics:

- the number of top-level definitions in ``runner.py``;
- the line count of every module in ``src/orbi`` (Issue #2167);
- the line counts of every function in ``src/orbi`` already over 100 lines.

A module that exists in the baseline is checked against its recorded line
count, so it may only shrink; the 1,000-line ``SIZE_CAP`` applies only to a
module absent from the baseline. Recording every module (not only the ones
over the cap) is what gives an existing module its own ceiling.

A function's length is measured over every ``ast.FunctionDef``/
``ast.AsyncFunctionDef`` with ``end_lineno - lineno + 1`` (decorators are
not counted, a nested def counts on its own and is also inside its parent)
and is recorded under ``<module>::<qualified function name>``. The
100-line function cap is this tool's own choice (Issue #2007), not a
CONSTITUTION article.

Every recorded value may only shrink. A new module may not cross the
1,000-line cap and a new function may not cross the 100-line cap without
being split first.

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
FUNCTION_CAP = 100


def _functions(node: ast.AST, prefix: str = ""):
    """Yield `(qualified name, line count)` for every function under `node`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}{child.name}"
            if not isinstance(child, ast.ClassDef):
                yield name, child.end_lineno - child.lineno + 1
            yield from _functions(child, f"{name}.")
        else:
            yield from _functions(child, prefix)


def counts(src_dir: Path) -> dict[str, object]:
    """Measure runner's direct definitions, module lengths and functions."""
    src_dir = Path(src_dir)
    runner = src_dir / "runner.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"), filename=str(runner))
    runner_top_level_defs = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in tree.body
    )
    module_lines = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(src_dir.glob("*.py"))
    }
    oversized_functions = {
        f"{path.name}::{name}": lines
        for path in sorted(src_dir.glob("*.py"))
        for name, lines in _functions(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if lines > FUNCTION_CAP
    }
    return {
        "runner_top_level_defs": runner_top_level_defs,
        "module_lines": module_lines,
        "oversized_functions": oversized_functions,
    }


def _recorded_lines(value: object) -> bool:
    """Whether a baseline entry is a `{name: line count}` mapping."""
    return isinstance(value, dict) and all(
        isinstance(name, str) and type(lines) is int for name, lines in value.items()
    )


def read_baseline(baseline_file: Path) -> dict[str, object] | None:
    """Read a valid baseline, returning None for missing or malformed input."""
    if not baseline_file.exists():
        return None
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        not isinstance(baseline, dict)
        or type(baseline.get("runner_top_level_defs")) is not int
        or not _recorded_lines(baseline.get("module_lines"))
        or not _recorded_lines(baseline.get("oversized_functions"))
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

    current_modules = current["module_lines"]
    baseline_modules = baseline["module_lines"]
    for name, value in current_modules.items():
        ceiling = baseline_modules.get(name, SIZE_CAP)
        if value > ceiling:
            failures.append((name, value, ceiling))

    current_functions = current["oversized_functions"]
    baseline_functions = baseline["oversized_functions"]
    for name, value in current_functions.items():
        ceiling = baseline_functions.get(name, FUNCTION_CAP)
        if value > ceiling:
            failures.append((name, value, ceiling))

    over_cap = [name for name, value in current_modules.items() if value > SIZE_CAP]
    print(
        "size ratchet (Issues #1229, #2007, #2167): "
        f"runner_top_level_defs={current_defs}/{baseline_defs} "
        f"module_lines={len(current_modules)} modules recorded "
        f"over_cap={over_cap!r} "
        f"oversized_functions={len(current_functions)} over {FUNCTION_CAP} lines "
        "(current/baseline ceilings)"
    )
    if failures:
        print("size ratchet FAILED:")
        for name, value, ceiling in failures:
            print(
                f"  {name}: current {value} > ceiling {ceiling}; "
                "split the code (or remove lines), or, for a deliberate "
                "reduction, run `python3 tools/size_ratchet.py --update`"
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
