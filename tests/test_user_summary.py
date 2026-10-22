"""The readable summary and the optional fixed project status link (#1826).

The module is the ONE renderer of the notification summary section: the
config loader validates the operator value through it, every task comment
renderer asks it for the summary lines, and no caller formats a URL
itself. These tests pin the validation contract (safe warning, never the
raw value, never a failed load), the unchanged URL rendering and the
language handling.
"""
import logging
from pathlib import Path

import pytest

from orbi import config as config_domain
from orbi import language, user_summary

VALID_URL = "https://tasks.example.org/project-status?repo=example%2Fwidget"


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.WARNING, logger="orbi.bootstrap")
    return caplog


def test_absent_status_url_is_quiet_and_has_no_link(warnings):
    assert user_summary.normalize_status_url(None, source="orbi.toml") is None
    assert warnings.records == []
    assert user_summary.project_status_line(None, language.LANGUAGE_EN) is None


def test_valid_url_is_kept_unchanged_including_its_query():
    assert (
        user_summary.normalize_status_url(VALID_URL, source="orbi.toml")
        == VALID_URL
    )


def test_valid_url_renders_a_clearly_labeled_link_in_both_languages():
    english = user_summary.project_status_line(VALID_URL, language.LANGUAGE_EN)
    chinese = user_summary.project_status_line(VALID_URL, language.LANGUAGE_ZH)

    assert english == f"**Project status:** [Open project status]({VALID_URL})"
    assert chinese == f"**项目状态：**[查看项目状态]({VALID_URL})"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("http://tasks.example.org/status", "scheme_not_https"),
        ("tasks.example.org/status", "scheme_not_https"),
        ("https://user:pass@tasks.example.org/status", "userinfo_not_allowed"),
        ("https:///status", "missing_host"),
        # Issue #1830: an authority without a hostname (a bare port, or a
        # bare port plus a query) is nonempty but has hostname None.
        ("https://:443/status", "missing_host"),
        ("https://:443?repo=owner%2Frepo", "missing_host"),
        ("https://tasks.example.org/a b", "unsafe_characters"),
        ("https://tasks.example.org/a)b", "unsafe_characters"),
        ('https://tasks.example.org/"x"', "unsafe_characters"),
        ("", "not_a_non_empty_string"),
        (7, "not_a_non_empty_string"),
        (True, "not_a_non_empty_string"),
        (["https://tasks.example.org"], "not_a_non_empty_string"),
        ("https://tasks.example.org:notaport/status", "malformed_url"),
    ],
)
def test_invalid_url_is_ignored_with_one_safe_warning(
    warnings, value, reason,
):
    assert user_summary.normalize_status_url(value, source="orbi.toml") is None
    assert len(warnings.records) == 1
    message = warnings.records[0].getMessage()
    assert "key=status_url ignored" in message
    assert "source=" in message
    assert f"reason={reason}" in message
    # The raw value must never reach the log, whatever it was.
    if isinstance(value, str) and value:
        assert value not in message


def test_invalid_value_never_fails_the_config_load(warnings, tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'status_url = "http://tasks.example.org/status"\n',
        encoding="utf-8",
    )

    config = config_domain.load_config(config_path)

    assert config.status_url is None
    assert config.source_repos == ("owner/repo",)
    assert any(
        "reason=scheme_not_https" in record.getMessage()
        for record in warnings.records
    )


def test_config_reads_the_configured_status_url_unchanged(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f'status_url = "{VALID_URL}"\n',
        encoding="utf-8",
    )
    assert config_domain.load_config(config_path).status_url == VALID_URL


def test_config_without_status_url_stays_quiet(warnings, tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    assert config_domain.load_config(config_path).status_url is None
    assert not [
        record for record in warnings.records
        if "status_url" in record.getMessage()
    ]


def test_summary_lines_carry_identity_action_and_link_in_order():
    lines = user_summary.summary_lines(
        issue=1826,
        title="  Add a fixed project status link\n and readable summaries  ",
        action="remove `ai-blocked`, then add `ai-fix-needed`",
        status_url=VALID_URL,
        language_code=language.LANGUAGE_EN,
    )

    assert lines == [
        "**#1826 Add a fixed project status link and readable summaries**",
        "**Action:** remove `ai-blocked`, then add `ai-fix-needed`",
        f"**Project status:** [Open project status]({VALID_URL})",
    ]


def test_summary_lines_without_identity_action_or_link_are_empty():
    assert user_summary.summary_lines(
        language_code=language.LANGUAGE_EN,
    ) == []


def test_summary_text_renders_an_empty_segment_when_nothing_applies():
    assert user_summary.summary_text(language_code=language.LANGUAGE_EN) == ""


def test_summary_text_ends_with_one_newline_for_template_insertion():
    text = user_summary.summary_text(
        title="A task", status_url=VALID_URL,
        language_code=language.LANGUAGE_ZH,
    )
    assert text == f"**A task**\n**项目状态：**[查看项目状态]({VALID_URL})\n"


def test_notice_keeps_its_marker_prefix_and_appends_the_summary():
    body = user_summary.notice(
        "<!-- orbi:run=abc12345 -->", "claim.scene_missing", error="gone",
        language_code=language.LANGUAGE_EN, status_url=VALID_URL, issue=7,
        title="Start the work",
    )
    assert body.startswith("<!-- orbi:run=abc12345 -->\nOrbi failed:")
    assert body.endswith(
        "**#7 Start the work**\n"
        f"**Project status:** [Open project status]({VALID_URL})"
    )


def test_notice_without_a_marker_or_summary_keeps_only_its_sentence():
    body = user_summary.notice(
        "", "claim.scene_missing", error="gone",
        language_code=language.LANGUAGE_EN,
    )
    assert body.startswith("Orbi failed:")
    assert "Project status" not in body


# --- the callers: every task notification renders the same summary ------
#
# These call the public renderers of the audited caller list, not the
# helper: each body is what GitHub (and the notification email) carries.

def _link(url=VALID_URL, language_code=language.LANGUAGE_EN):
    label = (
        "查看项目状态" if language_code == language.LANGUAGE_ZH
        else "Open project status"
    )
    prefix = "**项目状态：**" if language_code == language.LANGUAGE_ZH else (
        "**Project status:** "
    )
    return f"{prefix}[{label}]({url})"


def _assert_summary_above(body, *, title):
    """The summary section precedes the first diagnostics row."""
    assert title in body
    assert body.index(title) < body.index("\n- ")
    assert "</details>" not in body or body.index(_link()) < body.index(
        "</details>"
    )


def test_progress_field_block_renders_identity_action_and_link():
    body = progress_field_block_example()
    assert body.splitlines()[:3] == [
        "<!-- orbi:run=abc12345 -->",
        "<!-- orbi:comment=started -->",
        "Orbi started Pi: run=abc12345",
    ]
    assert "**#7 Start the work**" in body
    assert "**Action:** continue" in body
    _assert_summary_above(body, title="**#7 Start the work**")


def progress_field_block_example():
    from orbi import progress

    return progress.field_block(
        "abc12345", "Orbi started Pi: run=abc12345",
        {"run_id": "abc12345", "priority": "normal"},
        kind="started", language_code=language.LANGUAGE_EN,
        status_url=VALID_URL, issue=7, title="Start the work",
        action="continue",
    )


def test_progress_body_shows_title_near_the_top_and_the_link():
    from orbi import progress

    body = progress.progress_body({
        "language": "en", "run_id": "abc12345", "issue": 7,
        "issue_title": "Start the work", "role": "implement",
        "phase": "test", "elapsed": "1s", "tests": None,
        "review_round": 0, "branch": "b", "pr": None, "session": None,
        "status_url": VALID_URL,
    })
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=abc12345 -->"
    assert lines[1].startswith("<!-- orbi:progress:v1 ")
    header_at = lines.index("**Orbi progress**")
    assert lines[header_at + 2] == "**#7 Start the work**"
    assert lines[header_at + 3] == _link()
    assert lines[header_at + 5] == "- role: implement"


def test_progress_body_in_chinese_uses_the_chinese_label():
    from orbi import progress

    body = progress.progress_body({
        "language": "zh", "run_id": "abc12345", "issue": 7,
        "issue_title": "开始工作", "role": "implement", "phase": "test",
        "elapsed": "1s", "tests": None, "review_round": 0, "branch": "b",
        "pr": None, "session": None, "status_url": VALID_URL,
    })
    assert "**#7 开始工作**" in body
    assert _link(language_code=language.LANGUAGE_ZH) in body


def test_progress_body_without_status_url_has_no_link():
    from orbi import progress

    body = progress.progress_body({
        "language": "en", "run_id": "abc12345", "issue": 7,
        "issue_title": "Start the work", "role": "implement",
        "phase": "test", "elapsed": "1s", "tests": None,
        "review_round": 0, "branch": "b", "pr": None, "session": None,
    })
    assert "Open project status" not in body
    assert "**#7 Start the work**" in body


def test_started_and_opened_pr_scenes_carry_the_summary():
    from orbi.delivery_comment import (
        opened_pr_comment_body, started_pi_comment_body,
    )
    from orbi.delivery_scene import RunContext

    ctx = RunContext(
        run_id="abc12345", issue=7, branch="orbi/o-r-issue-7",
        worktree=Path("/tmp/wt"), source_repo="o/r",
    )
    started = started_pi_comment_body(
        ctx, "run_id=abc12345 priority=normal",
        language_code=language.LANGUAGE_EN, status_url=VALID_URL,
        issue_title="Start the work",
    )
    assert "**#7 Start the work**" in started
    assert _link() in started
    opened = opened_pr_comment_body(
        "abc12345", "base_branch=main base_sha=abc run_id=abc12345",
        "https://github.com/o/r/pull/1", language_code=language.LANGUAGE_EN,
        status_url=VALID_URL, issue=7, issue_title="Start the work",
    )
    assert opened.splitlines()[1].startswith("<!-- orbi:scene:v1 ")
    assert "**#7 Start the work**" in opened
    assert _link() in opened


def test_clarification_comment_carries_the_summary():
    from orbi import clarify

    body = clarify.render_comment(
        {"number": 7, "title": "Start the work", "author": {"login": "dev"}},
        clarify.ClarifyVerdict(passed=False, missing=("observable_result",)),
        "abc12345", language_code=language.LANGUAGE_EN,
        status_url=VALID_URL,
    )
    assert "@dev" in body
    assert body.startswith("@dev\n<!-- orbi:run=abc12345 -->\n")
    assert "**#7 Start the work**" in body
    assert _link() in body
    assert body.index(_link()) < body.index("No observable result")


def test_blocked_failure_body_carries_the_summary():
    from orbi import failure

    body = failure._failure_comment_body(
        outcome="blocked", action="Repair access.", reason="provider failed",
        diagnosis="provider failed", scene="- run: `abc12345`",
        evidence="", pr_url=None, issue="o/r#7", run_id="abc12345",
        language_code=language.LANGUAGE_EN, issue_number=7,
        issue_title="Start the work", status_url=VALID_URL,
    )
    assert body.index("**#7 Start the work**") < body.index("**Action:**")
    assert _link() in body
    assert body.index(_link()) < body.index("<details>")


def test_recoverable_scene_carries_the_summary_and_keeps_its_markers():
    from orbi import failure, progress

    body = progress.recoverable_scene_body(
        run_id="abc12345", recoverable_name="Pi failure recovered",
        record=failure.Failure(
            reason_code="credential_missing", action_code="refresh_credentials",
            retry_safe=True, outcome="fix_needed",
        ),
        detail="401", run_info="run=abc12345", status_url=VALID_URL,
        issue=7, title="Start the work", language_code=language.LANGUAGE_EN,
    )
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=abc12345 -->"
    assert lines[1] == "<!-- orbi:recovered=credential_missing -->"
    assert body.index("retrying on the next tick") < body.index("**#7 Start")
    assert _link() in body


def test_review_round_comment_carries_the_summary():
    from orbi import review_rounds

    body = review_rounds.review_round_comment_body(
        "<!-- orbi:run=abc12345 -->", 1, 42, 1, 0, [], "SCENE",
        repo="o/r", language_code=language.LANGUAGE_EN,
        status_url=VALID_URL, issue=7, title="Start the work",
    )
    assert body.splitlines()[2].startswith("Orbi review round 1")
    assert "**#7 Start the work**" in body
    assert _link() in body
    assert body.endswith("SCENE")


def test_merge_handoff_carries_identity_action_and_link():
    from orbi import merge_handoff

    body = merge_handoff.handoff_body(
        "<!-- orbi:run=abc12345 -->", pr_number=42, head="abc",
        preflight=["review required"], language_code=language.LANGUAGE_EN,
        status_url=VALID_URL, issue=7, title="Start the work",
    )
    assert "**#7 Start the work**" in body
    assert body.count("After the named policy action") == 1
    assert "**Action:** After the named policy action" in body
    assert _link() in body
    assert body.index(_link()) < body.index("- pr: #42")


def test_human_review_checklist_carries_the_summary():
    from orbi import human_review

    body = human_review.render_checklist_comment(
        run_id="abc12345", pr_url="https://github.com/o/r/pull/1",
        checklist={"column1": ["CI passed"], "column2": []},
        language_code=language.LANGUAGE_EN, status_url=VALID_URL,
        issue=7, title="Start the work",
    )
    assert "**#7 Start the work**" in body
    assert _link() in body
    assert body.index(_link()) < body.index("Column 1")


def test_merged_receipt_keeps_one_details_element_and_its_rows():
    from orbi.delivery_comment import merged_pr_comment_body

    body = merged_pr_comment_body(
        "abc12345", "https://github.com/o/r/pull/1", "deadbeef", 1, 0, 1,
        "main", "pass, no findings", language_code=language.LANGUAGE_EN,
        status_url=VALID_URL, issue=7, title="Start the work",
    )
    assert body.count("<details>") == 1
    for row in ("merge_commit=deadbeef", "review_rounds=1", "commits=1",
                "external_commits=0", "run_id=abc12345",
                "pr_url=https://github.com/o/r/pull/1"):
        assert body.count(row) == 1
    assert "**#7 Start the work**" in body
    assert _link() in body
    assert body.index(_link()) < body.index("- review:")


def test_release_comments_carry_the_summary():
    from orbi import release_comments

    success = release_comments.release_success_comment_body(
        "abc12345", "run_id=abc12345", "https://x/releases/v1", "v1.0.0",
        "deadbeef", ["#1 done"], ["gates ok"], "156 passed", "v1.0.0",
        language_code=language.LANGUAGE_EN, status_url=VALID_URL,
        issue=7, title="Start the work",
    )
    assert "**#7 Start the work**" in success
    assert _link() in success
    failed = release_comments.release_failure_comment_body(
        "abc12345", "run_id=abc12345", RuntimeError("boom"),
        language_code=language.LANGUAGE_EN, status_url=VALID_URL,
        issue=7, title="Start the work",
    )
    assert "**#7 Start the work**" in failed
    assert _link() in failed
