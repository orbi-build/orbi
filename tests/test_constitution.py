"""Guard the governance split between the Constitution and the public contracts.

Issue #1655: Article 2 used to hold the detailed interface inventory, so
every added interface row was an amendment — that coupled ordinary feature
work to constitutional changes. The authoritative inventory now lives in
`docs/public-contracts.mdx` (Article 2 keeps the principle and points at
it); the inventory-vs-CLI drift guard moved there with the row
(`tests/test_public_contracts.py`). These tests pin the split: the
Constitution keeps invariants, Article 2 keeps the consumer-reliance
principle, the inventory is not an amendment trigger, and Article 7 keeps
its separate amendment procedure.
"""
import re

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION = REPO_ROOT / "CONSTITUTION.md"


def _article(number: int) -> str:
    """The text of one article, from its heading to the next one."""
    text = CONSTITUTION.read_text(encoding="utf-8")
    match = re.search(
        rf"^## Article {number} —.*?(?=^## Article |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"CONSTITUTION.md has no Article {number}"
    return match.group(0)


def test_article_2_keeps_the_principle_and_points_at_the_public_contracts_page():
    """Consumers may rely only on officially documented public contracts;
    the authoritative inventory of those contracts lives in the docs page,
    and a contract change still needs docs + a release note in the same PR.
    """
    article = _article(2)
    assert "officially documented" in article, (
        "Article 2 must keep the officially-documented-contract principle"
    )
    assert "docs/public-contracts.mdx" in article, (
        "Article 2 must point at the public contracts inventory"
    )
    assert "release note" in article, (
        "Article 2 must keep the release-note requirement"
    )
    assert "private implementation" in article, (
        "Article 2 must keep private implementation unsupported"
    )


def test_article_2_no_longer_carries_the_inventory_or_the_amendment_rule():
    """Issue #1655: the detailed interface inventory and the
    "adding a row is an amendment" rule moved out of the Constitution,
    so ordinary interface evolution is not a constitutional change."""
    article = _article(2)
    assert "| Surface |" not in article, (
        "Article 2 must not carry the interface inventory table"
    )
    assert "| CLI |" not in article, (
        "Article 2 must not carry the moved CLI inventory row"
    )
    assert "Adding a row is an amendment" not in CONSTITUTION.read_text(
        encoding="utf-8"
    ), "the adding-a-row-is-an-amendment rule must be removed"


def test_article_7_keeps_the_separate_amendment_procedure():
    """An actual invariant change still follows Article 7 (a Constitution:
    Issue, a PR that changes this file, evidence the old rule failed),
    never the ordinary docs update path."""
    article = _article(7)
    assert "Constitution:" in article, (
        "Article 7 must keep the Constitution: Issue title rule"
    )
    assert "cannot be bundled with a feature" in article, (
        "Article 7 must keep the no-bundling rule"
    )
    assert "evidence" in article, (
        "Article 7 must keep the evidence requirement for an amendment"
    )
