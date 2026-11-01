"""The test suite's own contract (Issue #908).

Four guards keep the suite honest: no test module may bind the same
function name twice at module scope (the later def silently replaces
the earlier one and pytest collects the surviving name once — the
shadowed body never runs), the ONE shared fail-fast git scaffold in
``conftest.py`` must fail loudly on a non-zero exit (previously
asserted by 13 per-file copies of the same test), the fake ``pi``
executable has one owner under ``tests/fakes/``, and the autouse
CLI-install preflight patches the seam once (Issue #2009: the seam
fan-out already reaches every module that binds the name, so the second
patch was dead code the next reader had to explain).
"""
import ast
from pathlib import Path

import pytest

from conftest import git

TESTS_DIR = Path(__file__).resolve().parent


def _duplicate_module_level_defs(tree: ast.Module) -> list[tuple[str, int]]:
    """Module-scope def names bound more than once in one module: the
    later def silently replaces the earlier one, and pytest collects
    the surviving name once — the shadowed body never runs."""
    seen: set[str] = set()
    duplicates: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                duplicates.append((node.name, node.lineno))
            seen.add(node.name)
    return duplicates


def test_no_test_module_defines_a_function_name_twice():
    """Walk the AST of every test module and fail on any duplicate
    module-scope function name (Issue #908: the dict-era
    ``test_resolve_policy_context_files_are_additive`` copy had never
    executed)."""
    offenders = [
        f"{path.name}:{lineno} {name}"
        for path in sorted(TESTS_DIR.rglob("*.py"))
        for name, lineno in _duplicate_module_level_defs(
            ast.parse(path.read_text(encoding="utf-8"), str(path))
        )
    ]
    assert offenders == []


def test_the_walker_reports_a_shadowed_duplicate():
    """The guard's failure branch: a module that binds the same name
    twice is reported with the shadowing def's line."""
    tree = ast.parse(
        "def dup():\n    return 1\n\n\ndef dup():\n    return 2\n",
        "<synthetic>",
    )
    assert _duplicate_module_level_defs(tree) == [("dup", 5)]


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    """The one shared git scaffold (tests/conftest.py) must fail loudly
    on a git error, never pass a broken setup silently."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_install_fake_pi_is_defined_once_under_fakes():
    """The fake Pi executable has one owner (Issue #2009): every e2e suite
    imports it from `tests/fakes/`, so a behaviour fix lands once instead of
    in a copy per module."""
    definitions = [
        f"{path.relative_to(TESTS_DIR).as_posix()}:{node.lineno}"
        for path in sorted(TESTS_DIR.rglob("*.py"))
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), str(path))
        )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "install_fake_pi"
    ]
    assert [definition.split(":")[0] for definition in definitions] == [
        "fakes/pi.py",
    ]


def test_the_cli_install_fixture_patches_the_seam_once():
    """`_default_cli_install_preflight` installs the `refresh_cli_install`
    no-op exactly once (Issue #2009): the seam fan-out already reaches every
    module that binds the name, so a second patch is dead code that the next
    reader has to explain."""
    tree = ast.parse(
        (TESTS_DIR / "conftest.py").read_text(encoding="utf-8"),
    )
    fixture = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_default_cli_install_preflight"
    )
    patches = [
        node for node in ast.walk(fixture)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "seam"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "refresh_cli_install"
    ]
    assert len(patches) == 1
