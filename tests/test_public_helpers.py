"""Cross-module helpers are imported by their public names (Issue #1849).

A shared helper is a module's public surface. When one module imported a
helper through another module's `_private` name, renaming or moving that
helper broke an import at process start, in a module that had no
business owning the name. This guard pins the progress / failure /
delivery_labels / pi_session cluster: every helper the cluster shares is
exported under a public name and nothing imports it privately.

It also pins the second defect: `progress` re-exported
`journal.quote_value`, so four modules took the journal's quoting rule
from the progress comment renderer. `journal` is the ONE source.
"""
import ast
from pathlib import Path

from orbi import progress

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

# owner module -> the public name every importer must use.
PUBLIC_HELPERS = {
    "progress": {
        "fenced",
        "run_info_fields",
        "progress_state",
        "progress_body_with_outcome",
        "safe_publish",
        "finish_progress_body",
    },
    "failure": {
        "failure_detail",
        "redact_local_paths",
        "failure_summary",
        "failure_comment_body",
    },
    "delivery_labels": {"label_names", "label_name_set"},
    "pi_session": {"pi_extension_env"},
    "pi_command": {"skills_for"},
    "pi_stream_log": {"log_provider_config_loaded"},
}


def _sources() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(SRC_DIR.glob("*.py"))
    ]


def _imported_names(tree: ast.Module) -> list[tuple[str, str]]:
    """Every (module, name) pair an intra-package `from` import binds."""
    return [
        (node.module.removeprefix("orbi."), alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("orbi.")
        for alias in node.names
    ]


def test_shared_helpers_are_exported_under_their_public_names():
    for owner, names in PUBLIC_HELPERS.items():
        path = SRC_DIR / f"{owner}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef))
        }
        assert names <= defined, f"{owner} does not export {names - defined}"


def test_no_module_imports_a_shared_helper_privately():
    private = {
        owner: {f"_{name}" for name in names}
        for owner, names in PUBLIC_HELPERS.items()
    }
    offenders = [
        f"{path.name}: from orbi.{owner} import {name}"
        for path, tree in _sources()
        for owner, name in _imported_names(tree)
        if name in private.get(owner, set())
    ]
    assert offenders == []


def test_the_only_quote_value_source_is_journal():
    offenders = [
        f"{path.name}: from orbi.progress import {name}"
        for path, tree in _sources()
        for owner, name in _imported_names(tree)
        if owner == "progress" and name == "quote_value"
    ]
    assert offenders == []
    assert not hasattr(progress, "quote_value")
