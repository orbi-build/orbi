"""The merge execution/closeout extraction (Issue #1857).

`merge_gate`, `confirm_merged`, `merge_commit_metrics`,
`sync_base_checkout` and the human-review checklist pieces moved out of
the 1831-line `orbi.review_merge` into `orbi.merge_exec`, unchanged
(Article 3.2: an extraction changes no behaviour). The review loop, the
gate order and the merge behaviour stay exactly as they were — the
closeout golden asserted in `tests/test_review_merge.py` pins the
visible outcome (the exact comment bodies and label transitions).

These guards keep the extraction real: the new module owns the moved
names and never reaches back into the orchestration, and the source
module keeps no second definition and stays below the size the Issue
asked for.
"""
import ast
from pathlib import Path

import orbi.merge_exec as merge_exec
import orbi.review_merge as review_merge

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

# The region the Issue moved. The three exception types are also bound in
# `review_merge`, because the review loop catches them — the identity
# check below proves they are still ONE definition, owned here.
MOVED_NAMES = (
    "RecoverableMergeGateError",
    "DeliveryDeferred",
    "MergeBlockedByIssueLabel",
    "MERGE_METHOD_FIELDS",
    "MERGEABLE_UNKNOWN_RETRY_DELAYS",
    "select_merge_method",
    "merge_gate",
    "confirm_merged",
    "merge_commit_metrics",
    "sync_base_checkout",
    "_sync_base_checkout_locked",
    "delivered_changed_files",
    "_delivery_checklist",
    "human_review_checklist",
)

# The moved names the review loop catches (it imports them, so they stay
# reachable through `review_merge` without a second definition).
BOUND_BACK = (
    "RecoverableMergeGateError",
    "DeliveryDeferred",
    "MergeBlockedByIssueLabel",
)


def _lines(module_name: str) -> int:
    path = PACKAGE_DIR / f"{module_name}.py"
    return len(path.read_text(encoding="utf-8").splitlines())


def _imported_modules(module_name: str) -> set:
    """Every module `module_name` imports, at any scope."""
    path = PACKAGE_DIR / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def test_merge_exec_owns_the_moved_merge_execution_and_closeout():
    missing = [name for name in MOVED_NAMES if not hasattr(merge_exec, name)]
    assert missing == [], f"merge_exec must own the moved names: {missing}"
    assert _lines("merge_exec") < 700


def test_merge_exec_never_imports_the_runner_or_the_review_loop():
    """Article 3.3: the merge execution never reaches back into the
    orchestration it was extracted from, and never imports `runner`."""
    imported = _imported_modules("merge_exec")
    assert "orbi.runner" not in imported
    assert "orbi.review_merge" not in imported


def test_review_merge_keeps_one_owner_and_stays_below_1500_lines():
    """Issue #1857 acceptance: one owner per moved concept, and the source
    module is back below the 1500-line line the Issue asked for."""
    redefined = [
        name for name in MOVED_NAMES
        if name not in BOUND_BACK and hasattr(review_merge, name)
    ]
    assert redefined == [], f"review_merge still defines {redefined}"
    for name in BOUND_BACK:
        assert getattr(review_merge, name) is getattr(merge_exec, name)
    assert _lines("review_merge") < 1500
