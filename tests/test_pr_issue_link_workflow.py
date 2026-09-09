"""Contract tests for the external PR -> Issue state transition."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "pr-issue-link.yml"


def workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def on_section(data: dict) -> dict:
    return data.get("on", data.get(True))


def script() -> str:
    jobs = workflow()["jobs"]
    return "\n".join(
        str(step.get("with", {}).get("script", ""))
        for job in jobs.values() for step in job["steps"]
    )


def test_workflow_triggers_pr_lifecycle_events_and_has_write_permissions():
    trigger = on_section(workflow())["pull_request_target"]
    assert trigger["types"] == ["opened", "reopened", "edited"]
    assert workflow()["permissions"] == {
        "issues": "write", "pull-requests": "write",
    }


def test_workflow_parses_both_reference_forms_and_reuses_existing_state():
    code = script()
    assert "fixes|closes|resolves" in code
    assert "#?(\\d+)" in code
    assert '"ai-pr-opened"' in code
    assert "states" in code
    assert "orbi:external-pr:" in code
    assert "orbi:run=" in code


def test_workflow_is_non_blocking_and_comment_is_idempotent():
    code = script()
    assert "issues.createComment" in code
    assert "orbi:pr-issue-link" in code
    assert "alreadyPrompted" in code
    assert "issues.create({" not in code
