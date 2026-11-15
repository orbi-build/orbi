"""The safety matrix names only tests that exist (Issue #1114).

`docs/safety-matrix.mdx` and `docs/zh/safety-matrix.mdx` map each delivery
invariant to the layer that catches its failure, what the Runner does, and
the test that proves it. Prose drifts; a test id either collects or it does
not. So the Proof column is checked against the real pytest collection: an
id that no longer exists fails here, a row without proof must say
`(no test)`, and the page must keep a non-empty Known gaps section.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
PAGES = {
    "en": (DOCS_DIR / "safety-matrix.mdx", "## Known gaps"),
    "zh": (DOCS_DIR / "zh" / "safety-matrix.mdx", "## 已知缺口"),
}
SECURITY_LINKS = {
    "en": (DOCS_DIR / "security.mdx", "(/safety-matrix)"),
    "zh": (DOCS_DIR / "zh" / "security.mdx", "(/zh/safety-matrix)"),
}
NAVIGATION = {"en": ("security", "safety-matrix"),
              "zh": ("zh/security", "zh/safety-matrix")}
NO_TEST = "(no test)"
MINIMUM_ROWS = 7
TEST_ID_RE = re.compile(r"`(tests/[\w/]+\.py::[\w\[\].:-]+)`")
SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
COLLECT_TIMEOUT_SECONDS = 120


def matrix_rows(text: str) -> list[list[str]]:
    """The data rows of the first four-column table on the page.

    The header row and the `| --- |` separator are skipped; the table ends
    at the first line that is not a pipe row. Every data row must carry
    exactly four non-empty cells.
    """
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("|")),
        None,
    )
    if start is None:
        raise AssertionError("the page has no safety matrix table")
    rows: list[list[str]] = []
    for offset, line in enumerate(lines[start:]):
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if offset == 0 or all(SEPARATOR_RE.match(cell) for cell in cells):
            continue  # the header row, then the separator row
        if len(cells) != 4 or not all(cells):
            raise AssertionError(
                f"matrix row needs four non-empty cells, got {cells!r}"
            )
        rows.append(cells)
    return rows


def proof_ids(cell: str) -> list[str]:
    """The pytest ids a Proof cell names; `(no test)` names none."""
    if cell == NO_TEST:
        return []
    ids = TEST_ID_RE.findall(cell)
    if not ids:
        raise AssertionError(
            f"a Proof cell must name `tests/<file>.py::<test>` ids or be "
            f"exactly {NO_TEST!r}, got {cell!r}"
        )
    return ids


def uncollected(ids, collected: set[str]) -> list[str]:
    """The ids pytest does not collect.

    An id names one collected test, or a parametrized test whose cases
    collect as `<id>[<case>]`.
    """
    return sorted(
        test_id for test_id in set(ids)
        if test_id not in collected
        and not any(item.startswith(f"{test_id}[") for item in collected)
    )


def known_gaps(text: str, heading: str) -> str:
    """The body of the Known gaps section, up to the next `## ` heading."""
    lines = text.splitlines()
    if heading not in lines:
        raise AssertionError(f"missing section heading: {heading!r}")
    body: list[str] = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def page_rows(language: str) -> list[list[str]]:
    path, _heading = PAGES[language]
    return matrix_rows(path.read_text(encoding="utf-8"))


def collect(files: list[str]) -> set[str]:
    """Every test id pytest collects from `files`, in one bounded run."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *files],
        cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=COLLECT_TIMEOUT_SECONDS, check=False,
    )
    assert run.returncode == 0, (
        f"pytest could not collect {files}:\n{run.stdout}\n{run.stderr}"
    )
    return {line for line in run.stdout.splitlines() if "::" in line}


# --- the parsers, on small inputs --------------------------------------------

TABLE = (
    "intro\n"
    "\n"
    "| Failure mode | Layer | Runner | Proof |\n"
    "| --- | :---: | --- | ---: |\n"
    "| a | b | c | `tests/test_x.py::test_one` |\n"
    "| d | e | f | (no test) |\n"
    "\n"
    "| not | part | of | it |\n"
)


def test_matrix_rows_skip_the_header_and_separator_and_stop_at_the_table_end():
    assert matrix_rows(TABLE) == [
        ["a", "b", "c", "`tests/test_x.py::test_one`"],
        ["d", "e", "f", NO_TEST],
    ]


def test_matrix_rows_read_a_table_that_ends_the_page():
    assert matrix_rows("| h | h | h | h |\n| --- | --- | --- | --- |") == []
    assert matrix_rows("| h | h | h | h |\n| a | b | c | d |") == [
        ["a", "b", "c", "d"],
    ]


def test_matrix_rows_reject_a_page_without_a_table():
    with pytest.raises(AssertionError, match="no safety matrix table"):
        matrix_rows("prose only\n")


@pytest.mark.parametrize("row", [
    "| a | b | c |",
    "| a | b | c | d | e |",
    "| a |  | c | d |",
])
def test_matrix_rows_reject_a_row_without_four_filled_cells(row):
    with pytest.raises(AssertionError, match="four non-empty cells"):
        matrix_rows(f"| h | h | h | h |\n| --- | --- | --- | --- |\n{row}\n")


def test_proof_ids_read_every_id_and_the_no_test_sentinel():
    cell = "`tests/test_a.py::test_one`, `tests/test_b.py::test_two[case-1]`"
    assert proof_ids(cell) == [
        "tests/test_a.py::test_one", "tests/test_b.py::test_two[case-1]",
    ]
    assert proof_ids(NO_TEST) == []


@pytest.mark.parametrize("cell", [
    "existing tests", "tests/test_a.py::test_one", "`test_one`", "(no tests)",
])
def test_proof_ids_reject_a_cell_that_names_no_collectable_id(cell):
    with pytest.raises(AssertionError, match="must name"):
        proof_ids(cell)


def test_uncollected_names_exactly_the_ids_pytest_does_not_know():
    collected = {
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_cases[first]",
        "tests/test_a.py::test_cases[second]",
    }
    assert uncollected(
        ["tests/test_a.py::test_one", "tests/test_a.py::test_cases",
         "tests/test_a.py::test_cases[second]"],
        collected,
    ) == []
    assert uncollected(
        ["tests/test_a.py::test_gone", "tests/test_a.py::test_on",
         "tests/test_a.py::test_cases[third]", "tests/test_a.py::test_one"],
        collected,
    ) == [
        "tests/test_a.py::test_cases[third]",
        "tests/test_a.py::test_gone",
        "tests/test_a.py::test_on",
    ]


def test_known_gaps_reads_the_section_body_up_to_the_next_heading():
    text = "## Known gaps\n\n- one\n- two\n\n## Next\n\nother\n"
    assert known_gaps(text, "## Known gaps") == "- one\n- two"
    assert known_gaps("## Known gaps\n- last section", "## Known gaps") == (
        "- last section"
    )


def test_known_gaps_rejects_a_page_without_the_section():
    with pytest.raises(AssertionError, match="missing section heading"):
        known_gaps("## Something else\n", "## Known gaps")


# --- the pages -----------------------------------------------------------------

@pytest.mark.parametrize("language", sorted(PAGES))
def test_the_matrix_has_at_least_seven_complete_rows(language):
    rows = page_rows(language)
    assert len(rows) >= MINIMUM_ROWS, (
        f"{language}: the safety matrix has {len(rows)} rows, "
        f"needs at least {MINIMUM_ROWS}"
    )
    for row in rows:
        proof_ids(row[3])


def test_both_languages_name_the_same_proof_for_every_row():
    english = [row[3] for row in page_rows("en")]
    chinese = [row[3] for row in page_rows("zh")]
    assert english == chinese, (
        "the English and Chinese matrices must name the same proof, "
        "row by row"
    )


def test_every_test_id_on_the_page_is_a_test_pytest_collects():
    """The Proof column and the Known gaps prose alike: no id on either
    page may name a test that does not exist."""
    ids = sorted({
        test_id
        for path, _heading in PAGES.values()
        for test_id in TEST_ID_RE.findall(path.read_text(encoding="utf-8"))
    })
    proof = {
        test_id
        for language in PAGES
        for row in page_rows(language)
        for test_id in proof_ids(row[3])
    }
    assert proof and proof <= set(ids), "the matrix names no test at all"
    files = sorted({test_id.split("::", 1)[0] for test_id in ids})
    missing_files = [name for name in files if not (REPO_ROOT / name).is_file()]
    assert not missing_files, (
        f"the safety matrix names test files that do not exist: {missing_files}"
    )
    missing = uncollected(ids, collect(files))
    assert not missing, (
        "the safety matrix names tests pytest does not collect — rename the "
        f"id in docs/safety-matrix.mdx and docs/zh/safety-matrix.mdx, or "
        f"write {NO_TEST!r} and list the row under Known gaps: {missing}"
    )


@pytest.mark.parametrize("language", sorted(PAGES))
def test_known_gaps_is_not_empty_and_owns_every_row_without_proof(language):
    path, heading = PAGES[language]
    gaps = known_gaps(path.read_text(encoding="utf-8"), heading)
    assert gaps, f"{language}: the Known gaps section is empty"
    unproven = [row for row in page_rows(language) if row[3] == NO_TEST]
    assert not unproven or NO_TEST in gaps, (
        f"{language}: {len(unproven)} row(s) say {NO_TEST!r}; the Known "
        "gaps section must list them again"
    )


# --- discovery -----------------------------------------------------------------

def navigation_pages(entries) -> list[str]:
    """Every page slug below one navigation `pages`/`groups` list, in order."""
    pages: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            pages.append(entry)
        else:
            pages.extend(navigation_pages(entry["pages"]))
    return pages


def test_navigation_pages_flattens_nested_groups_in_order():
    assert navigation_pages([
        "a", {"group": "g", "pages": ["b", {"group": "h", "pages": ["c"]}]}, "d",
    ]) == ["a", "b", "c", "d"]


def test_the_matrix_follows_security_once_per_language_in_the_navigation():
    config = json.loads((DOCS_DIR / "docs.json").read_text(encoding="utf-8"))
    languages = {
        entry["language"]: navigation_pages(entry["groups"])
        for entry in config["navigation"]["languages"]
    }
    for language, (security, matrix) in NAVIGATION.items():
        pages = languages[language]
        assert pages.count(matrix) == 1, (
            f"{language}: {matrix} must appear exactly once in docs.json"
        )
        assert pages[pages.index(security) + 1] == matrix, (
            f"{language}: {matrix} must follow {security} in docs.json"
        )


@pytest.mark.parametrize("language", sorted(SECURITY_LINKS))
def test_the_security_page_links_the_matrix_from_its_first_paragraph(language):
    path, link = SECURITY_LINKS[language]
    lines = path.read_text(encoding="utf-8").splitlines()
    body = lines[lines.index("---", 1) + 1:]
    first_paragraph = next(line for line in body if line.strip())
    assert link in first_paragraph, (
        f"{path.name}: the first paragraph must link the safety matrix {link}"
    )
