"""GitHub New Issue chooser configuration contract for Issue #918."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
USER_OUTCOME = ROOT / ".github" / "ISSUE_TEMPLATE" / "user-outcome.md"


def test_issue_chooser_guides_questions_and_documentation():
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))

    assert config["blank_issues_enabled"] is True
    assert config["contact_links"] == [
        {
            "name": "Installation or runtime question?",
            "url": "https://github.com/orbi-build/orbi/discussions/new?category=q-a",
            "about": "Ask about environments, model access, or workflows in Discussions.",
        },
        {
            "name": "Read the documentation first",
            "url": "https://docs.orbi.build/",
            "about": "Check the complete installation and configuration guide before asking.",
        },
    ]


def test_internal_user_outcome_template_remains_available():
    assert USER_OUTCOME.is_file()
    text = USER_OUTCOME.read_text(encoding="utf-8")
    assert all(section in text for section in (
        "## User outcome", "## Preconditions", "## Acceptance", "## Evidence",
    ))


def test_issue_templates_are_english_first():
    for path in (CONFIG, USER_OUTCOME):
        assert not any("\u4e00" <= char <= "\u9fff" for char in path.read_text(encoding="utf-8"))
