"""The Issue ticket shape states BOTH forms everywhere it appears (Issue #1078).

A first-time user must be able to write a feature ticket, not only a bug
ticket: every place that states the ticket shape states both forms, in
this order — a **fix** (`when X, should Y, actually Z`) and a **change**
(`when X, the user should be able to Y; today they cannot because Z`).
The check is a SET comparison of the stated forms per file — a file that
states one form must state both — so a later edit cannot drop the change
form, and the failure names the file and the missing form. The scan is
not limited to the named entry points: any docs page a later edit gives
a form marker to must carry both.

`.github/ISSUE_TEMPLATE/user-outcome.md` is deliberately shape-neutral
(User outcome / Preconditions / Acceptance / Evidence) and stays out of
the scan; the runner and its prompts state no shape and are out of scope.
Markers match whitespace-normalized text (line wraps do not matter, the
same convention as the single-source contract); the zh Docker pages
quote the EN literal inline while the zh workflow/contributing pages
phrase the shapes in Chinese — either language counts as stating it.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / "docs" / "workflow.mdx"

FORM_MARKERS = {
    "fix": (
        "when X, should Y, actually Z",
        "当 X，应该 Y，实际 Z",
    ),
    "change": (
        "when X, the user should be able to Y; today they cannot because Z",
        "当 X，用户应该能够 Y；今天不能，因为 Z",
    ),
}
ALL_MARKERS = tuple(
    marker for markers in FORM_MARKERS.values() for marker in markers
)

# The entry points the Issue names, plus the two Docker README mirrors
# of docs/docker.mdx that the quick-guide contract keeps in lockstep.
SHAPE_FILES = (
    "docs/workflow.mdx",
    "docs/contributing.mdx",
    "docs/docker.mdx",
    "AGENTS.md",
    "3rd/docker/README.md",
    "docs/zh/workflow.mdx",
    "docs/zh/contributing.mdx",
    "docs/zh/docker.mdx",
    "3rd/docker/README.zh-CN.md",
)

ISSUE_LINK = re.compile(
    r"\[#\d+\]\(https://github\.com/orbi-build/orbi/issues/\d+\)"
)


def norm(text: str) -> str:
    """Collapse every whitespace run to one space (wrap-proof match)."""
    return re.sub(r"\s+", " ", text)


def stated_forms(text: str) -> set[str]:
    """The forms a file states (either language marker counts)."""
    collapsed = norm(text)
    return {
        form for form, markers in FORM_MARKERS.items()
        if any(marker in collapsed for marker in markers)
    }


def missing_forms_report(name: str, text: str) -> str | None:
    """The failure line for a file that misses a form, or None."""
    present = stated_forms(text)
    missing = set(FORM_MARKERS) - present
    if not missing:
        return None
    return (
        f"{name}: states only {sorted(present)} — missing the "
        f"{sorted(missing)} form(s); state both: fix "
        f"({FORM_MARKERS['fix'][0]}) and change "
        f"({FORM_MARKERS['change'][0]})"
    )


def test_every_shape_entry_point_states_both_forms():
    for name in SHAPE_FILES:
        path = REPO_ROOT / name
        assert path.is_file(), f"missing shape entry point: {name}"
        failure = missing_forms_report(name, path.read_text(encoding="utf-8"))
        assert failure is None, failure


def test_every_shape_stating_docs_page_states_both_forms():
    """Any docs page that states one form states both — a later edit
    cannot reintroduce a one-form statement on a new page."""
    for path in sorted((REPO_ROOT / "docs").rglob("*.mdx")):
        text = path.read_text(encoding="utf-8")
        if not any(marker in norm(text) for marker in ALL_MARKERS):
            continue
        failure = missing_forms_report(
            str(path.relative_to(REPO_ROOT)), text
        )
        assert failure is None, failure


def test_missing_forms_report_names_the_file_and_the_missing_form():
    """A one-form statement produces the failure line the contract
    promises — the file and the missing form (Issue #1078 failure
    path)."""
    report = missing_forms_report(
        "docs/example.mdx", "when X, should Y, actually Z"
    )
    assert report is not None
    assert "docs/example.mdx" in report
    assert "missing the ['change'] form" in report


def test_missing_forms_report_passes_a_both_forms_file():
    assert missing_forms_report(
        "docs/example.mdx",
        "when X, should Y, actually Z — and "
        "when X, the user should be able to Y; today they cannot because Z",
    ) is None


def bullet_block(lines: list[str], start: str) -> list[str]:
    """A markdown bullet and its indented continuation lines."""
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    block = [lines[first]]
    for line in lines[first + 1:]:
        if line.startswith(("  ", "\t")):
            block.append(line)
        else:
            break
    return block


def test_bullet_block_takes_a_block_running_to_end_of_file():
    """A bullet whose continuations run to the last line exits the
    loop exhausted (no terminating line after the block)."""
    assert bullet_block(["- **Fix**", "  more"], "- **Fix**") == [
        "- **Fix**",
        "  more",
    ]


def test_workflow_page_has_one_short_linked_example_per_form():
    """Issue #1078 acceptance 2: docs/workflow.mdx carries one worked
    example per form, each four lines or fewer, modeled on a real merged
    Issue from this repository (linked)."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    for form in ("- **Fix**", "- **Change**"):
        block = bullet_block(lines, form)
        assert len(block) <= 4, (
            f"workflow.mdx: the {form} example is {len(block)} lines "
            f"(max 4)"
        )
        joined = " ".join(block)
        assert ISSUE_LINK.search(joined), (
            f"workflow.mdx: the {form} example must link its model Issue"
        )
