"""Contract tests for Issue #173's user-centered acceptance workflow."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "AGENTS.md"
PROMPT = ROOT / "prompts" / "prompt.md"
REVIEW = ROOT / "prompts" / "prompt_review.md"
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "user-outcome.md"
DOCS = (ROOT / "docs" / "contributing.mdx", ROOT / "docs" / "zh" / "contributing.mdx")


REQUIRED_CONTRACT = (
    "User-centered acceptance",
    "User outcome",
    "Preconditions",
    "Acceptance",
    "Evidence",
    "success path",
    "failure path",
    "user sees",
    "repair action",
)


def text(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    return path.read_text(encoding="utf-8").lower()


def test_agents_defines_user_centered_acceptance_contract():
    content = text(AGENTS)
    missing = [item for item in REQUIRED_CONTRACT if item.lower() not in content]
    assert not missing, f"AGENTS.md misses user acceptance rules: {missing}"


def test_prompts_make_implementer_and_reviewer_follow_the_user_journey():
    for path in (PROMPT, REVIEW):
        content = text(path)
        for item in ("user journey", "real user path", "evidence", "failure path"):
            assert item in content, f"{path.name} misses {item!r}"
    assert "define" in text(PROMPT)
    assert "review the same" in text(REVIEW)


def test_issue_template_has_the_unified_user_facing_shape():
    content = text(TEMPLATE)
    for heading in ("user outcome", "preconditions", "acceptance", "evidence"):
        assert f"## {heading}" in content
    for item in ("success", "failure", "repair"):
        assert item in content


def test_bilingual_guidance_has_matching_contract_and_impact_examples():
    english, chinese = (text(path) for path in DOCS)
    for content in (english, chinese):
        for item in ("user outcome", "preconditions", "acceptance", "evidence"):
            assert item in content
        for item in ("provider", "concurrency", "setup", "ui", "refactor"):
            assert item in content
        assert "success" in content and "failure" in content
    # The same impact categories must be present in both languages; this
    # prevents the translated page from silently weakening the contract.
    for item in ("provider", "concurrency", "setup", "ui", "refactor"):
        assert item in english and item in chinese
