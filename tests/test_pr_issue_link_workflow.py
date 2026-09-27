"""Contract tests for the external PR -> Issue state transition."""
import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "pr-issue-link.yml"

# GitHub's closing keywords, an optional colon and a mandatory `#`:
# https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
CLOSING_KEYWORD = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b"

LINKAGE_CASES = [
    ("Fix #12", "12"),
    ("Fixed #12", "12"),
    ("Fixes #12", "12"),
    ("Closes: #12", "12"),
    ("close #12", "12"),
    ("RESOLVED #12", "12"),
    ("Fixes 12", None),
    ("See #12", None),
    ("Issue #12", None),
    ("(#12)", None),
    ("prefixes #12", None),
    ("Fixes #12abc", None),
]


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


def js_regex_literals(code: str) -> list[str]:
    """Source of every JS regex literal in `code` (single-line comments aside)."""
    without_comments = re.sub(r"//[^\n]*", "", code)
    return [
        body
        for body, _flags in re.findall(r"/((?:[^/\\\n]|\\.)+)/([a-z]*)", without_comments)
    ]


def closing_keyword_regexes(code: str) -> list[str]:
    return [
        body
        for body in js_regex_literals(code)
        if re.search(r"close|fix|resolve", body, re.IGNORECASE) and "#" in body
    ]


def compiled_closing_keyword_regex() -> re.Pattern:
    regexes = closing_keyword_regexes(script())
    assert len(regexes) == 1, regexes
    return re.compile(regexes[0], re.IGNORECASE)


@pytest.mark.parametrize(("body", "expected"), LINKAGE_CASES)
def test_closing_keyword_regex_links_only_github_closing_references(body, expected):
    match = compiled_closing_keyword_regex().search(body)
    if expected is None:
        assert match is None
    else:
        assert match is not None
        assert match.group(1) == expected


def test_workflow_defines_one_closing_keyword_regex_and_one_comment_call():
    code = script()
    assert len(closing_keyword_regexes(code)) == 1
    assert code.count("issues.createComment") == 1


def test_prompt_is_correct_with_or_without_an_existing_issue():
    match = re.search(r'const prompt = "((?:[^"\\]|\\.)*)";', script())
    assert match, "prompt literal not found"
    prompt = match.group(1)
    assert "Please open an Issue first" not in prompt
    assert "orbi:pr-issue-link" in prompt
    assert "Fixes #" in prompt


def test_workflow_triggers_pr_lifecycle_events_and_has_write_permissions():
    trigger = on_section(workflow())["pull_request_target"]
    assert trigger["types"] == ["opened", "reopened", "edited"]
    assert workflow()["permissions"] == {
        "issues": "write", "pull-requests": "write",
    }


def test_marker_and_label_writes_are_gated_by_a_linkable_issue():
    code = script()
    # The guard that admits an external PR onto an Issue: it is open, it is
    # not a pull request, and it carries no delivery state.
    guard = re.search(r'if \((issue\.data\.state === "open".*?)\) \{', code, re.DOTALL)
    assert guard, "linkable-Issue guard not found"
    condition = guard.group(1)
    assert "issue.data.pull_request" in condition
    assert "states" in condition and "labels" in condition
    # Both the label write and the marker write live inside that guard's block.
    label_at = code.index("github.rest.issues.addLabels")
    marker_at = code.index("github.rest.issues.update")
    block_end = code.index("\n}", guard.end())
    assert guard.end() < label_at < marker_at < block_end


def test_workflow_parses_closing_keyword_reference_and_reuses_existing_state():
    code = script()
    assert CLOSING_KEYWORD in code
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
