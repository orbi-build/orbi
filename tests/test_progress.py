"""Unit tests for progress: automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker so a restarted process
finds the same comment and keeps PATCHing it — no database. Milestones are
short standalone comments so GitHub Mobile pushes a notification.
"""
import json
import subprocess

import pytest

from orbi import progress, scene
from orbi.delivery_scene import RunContext


def test_quote_value_quotes_only_values_with_spaces():
    assert progress.quote_value("test") == "test"
    assert progress.quote_value("bash pytest tests/") == (
        '"bash pytest tests/"'
    )


def test_quote_value_escapes_embedded_quotes():
    assert progress.quote_value('git commit -m "feat: x"') == (
        '"git commit -m \\"feat: x\\""'
    )
    assert progress.quote_value('a"b') == '"a\\"b"'


def test_format_status_comment_non_string_is_unchanged():
    assert progress.format_status_comment(None) is None


def test_failure_comment_redacts_evidence_and_preserves_resume_scene():
    key = "sk-ant-api03-" + "a" * 40
    rendered = progress.format_status_comment(
        "Orbi failed: boom\n\nstderr_tail:\n" + key,
    )
    assert key not in rendered
    assert "sk-ant-api03-<redacted>" in rendered

    scene_body = scene.render(scene.Scene(
        run_id="a1b2c3d4", base_branch="main", base_sha="abc123",
        pr_url="https://github.com/owner/repo/pull/9",
    ))
    comment = progress.format_status_comment(scene_body)
    from orbi import runner
    assert runner.parse_pr_comment(comment)["run_id"] == "a1b2c3d4"


def test_format_status_comment_expands_marked_failure():
    body = progress.format_status_comment(
        "<!-- orbi:run=abc12345 -->\nOrbi failed: boom (run_id=abc12345)",
    )
    assert "Orbi failed: boom" in body
    assert "- run_id=abc12345" in body


def test_run_marker_is_hidden_html_comment_with_run_id():
    marker = progress.run_marker("abc12345")
    assert marker == "<!-- orbi:run=abc12345 -->"


def test_run_marker_pattern_extracts_the_run_id():
    match = progress.RUN_MARKER_PATTERN.search(
        "started <!-- orbi:run=abc12345 -->"
    )
    assert match is not None
    assert match.group(1) == "abc12345"


def test_runner_fingerprint_uses_editable_checkout_head(monkeypatch, tmp_path):
    package = tmp_path / "checkout" / "src" / "orbi"
    package.mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    monkeypatch.setattr(progress, "__file__", str(package / "progress.py"))
    monkeypatch.setattr(
        progress.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="8a12fb1c\n", stderr="",
        ),
    )
    assert progress.runner_fingerprint() == "8a12fb1c"


def test_runner_fingerprint_uses_package_version_when_not_editable(
    monkeypatch, tmp_path,
):
    package = tmp_path / "site-packages" / "orbi"
    package.mkdir(parents=True)
    monkeypatch.setattr(progress, "__file__", str(package / "progress.py"))
    seen: list[str] = []
    monkeypatch.setattr(
        progress.metadata, "version",
        lambda name: seen.append(name) or "0.3.4",
    )
    assert progress.runner_fingerprint() == "0.3.4"
    # Issue #874: the distribution is installed as `orbi-cli` — the
    # fingerprint must read THAT install metadata, never the old name.
    assert seen == ["orbi-cli"]


def test_runner_fingerprint_returns_unknown_for_invalid_git_output(
    monkeypatch, tmp_path,
):
    package = tmp_path / "checkout" / "src" / "orbi"
    package.mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    monkeypatch.setattr(progress, "__file__", str(package / "progress.py"))
    monkeypatch.setattr(
        progress.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="not-a-sha\n", stderr="",
        ),
    )
    assert progress.runner_fingerprint() == "unknown"


def test_runner_fingerprint_returns_unknown_when_detection_fails(
    monkeypatch, tmp_path,
):
    package = tmp_path / "checkout" / "src" / "orbi"
    package.mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    monkeypatch.setattr(progress, "__file__", str(package / "progress.py"))
    monkeypatch.setattr(
        progress.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(128, args[0]),
        ),
    )
    assert progress.runner_fingerprint() == "unknown"


def test_structured_comment_places_runner_marker_at_end(monkeypatch):
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    body = progress.field_block("abc12345", "Orbi: started", {"run_id": "abc12345"})
    assert body.endswith("<!-- runner=8a12fb1c -->")
    assert body.count("runner=") == 1


def test_legacy_comment_places_runner_marker_at_end(monkeypatch):
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    body = progress.format_status_comment(
        "<!-- orbi:run=abc12345 -->\nOrbi failed: boom (run_id=abc12345)",
    )
    assert body.endswith("<!-- runner=8a12fb1c -->")


def test_progress_body_places_runner_marker_at_end(monkeypatch):
    # The live progress comment (and the blocked / fix-needed scenes it
    # becomes) is the run's main observability surface, so it carries the
    # runner fingerprint like every other Orbi comment (Issue #526).
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    body = progress.progress_body({
        "run_id": "abc12345",
        "issue": 18,
        "issue_title": "Publish progress",
        "role": "implement",
        "phase": "test",
        "elapsed": "3m 12s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    })
    assert body.endswith("\n\n<!-- runner=8a12fb1c -->")
    assert body.count("runner=") == 1


def test_comment_rendering_degrades_to_runner_unknown(monkeypatch):
    # Issue #526 acceptance: when the fingerprint probe fails, every
    # Orbi comment still renders — with `runner=unknown`, never a
    # fabricated value, and never a publishing failure (Issue #79).
    # The probe's own catch-all owns the degradation (runner_fingerprint
    # returns ``unknown``), so the failure is injected one layer below.
    def _raise(*args, **kwargs):
        raise RuntimeError("git unavailable")
    monkeypatch.setattr(progress.subprocess, "run", _raise)
    for body in (
        progress.progress_body({
            "run_id": "abc12345", "issue": 18,
            "issue_title": "Publish progress", "role": "implement",
            "phase": "test", "elapsed": "1s", "last_activity": None,
            "last_action": None, "tests": None, "review_round": 0,
            "branch": "b", "pr": None, "session": None,
        }),
        progress.field_block("abc12345", "Orbi: tests passed", {}),
        progress.format_status_comment("Orbi failed: boom"),
    ):
        assert body.endswith("<!-- runner=unknown -->")


def test_run_marker_rejects_missing_or_invalid_run_id():
    for bad in ("", "run1", None):
        with pytest.raises(ValueError, match="invalid run id"):
            progress.run_marker(bad)





@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (59.9, "59s"),
        (60, "1m 0s"),
        (192, "3m 12s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3723, "1h 2m 3s"),
        (7325, "2h 2m 5s"),
    ],
)
def test_format_elapsed_omits_zero_units(seconds, expected):
    assert progress.format_elapsed(seconds) == expected


def test_issue_field_renders_number_and_title():
    # Issue #100: the progress comment's issue line shows the number
    # AND the title; the `#<number>` prefix is preserved so existing
    # log/scene parsing keeps working.
    assert progress.issue_field(89, "ship it") == "#89 ship it"
    assert progress.issue_field(18, "Publish progress") == \
        "#18 Publish progress"


def test_issue_field_keeps_the_hash_number_prefix():
    # A grep for `#18` (the existing journal/scene convention) must
    # still find the issue line.
    field = progress.issue_field(18, "Publish progress")
    assert field.startswith("#18")


def test_issue_field_flattens_spaces_and_newlines_to_one_line():
    # Titles with spaces, internal newlines and repeated whitespace
    # stay single-line and readable (Issue #100 requirement).
    assert progress.issue_field(7, "a  b\n\nc\t\td") == "#7 a b c d"


def test_issue_field_keeps_markdown_characters_readable():
    # Markdown in the title is not escaped or stripped: the comment is
    # Markdown too, so the title renders as-is on one line.
    title = "Bug: `resume` uses **PR URL**, no more verify_pr (#45)"
    assert progress.issue_field(7, title) == f"#7 {title}"


def test_issue_field_handles_a_long_title():
    # A long title is kept in full (no silent truncation): the field
    # stays one line and readable.
    title = "x" * 300
    field = progress.issue_field(7, title)
    assert field == f"#7 {title}"
    assert "\n" not in field


def test_issue_field_fails_fast_on_missing_or_empty_title():
    # A missing or blank title violates the GitHub issue data contract
    # (issues always carry a non-empty string title): fail fast, never
    # fabricate one (Issue #100 requirement).
    with pytest.raises(ValueError, match="title"):
        progress.issue_field(7, None)
    with pytest.raises(ValueError, match="title"):
        progress.issue_field(7, "")
    with pytest.raises(ValueError, match="title"):
        progress.issue_field(7, "   \n  ")


def test_issue_field_fails_fast_on_non_string_title():
    with pytest.raises(ValueError, match="title"):
        progress.issue_field(7, 123)


def test_issue_field_fails_fast_on_non_int_issue():
    with pytest.raises(ValueError, match="issue"):
        progress.issue_field("7", "t")
    # bool is an int subclass but not an issue number.
    with pytest.raises(ValueError, match="issue"):
        progress.issue_field(True, "t")


def test_progress_body_issue_line_carries_number_and_title():
    # Issue #100: every progress scene renders `#<number> <title>`.
    body = progress.progress_body({
        "run_id": "abc12345",
        "issue": 89,
        "issue_title": "Bug: resume 使用评论里的 PR URL，不再 verify_pr",
        "role": "implement",
        "phase": "test",
        "elapsed": "3m 12s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    })
    assert (
        "- issue: #89 Bug: resume 使用评论里的 PR URL，不再 verify_pr"
        in body
    )
    # The `#89` prefix is preserved for existing parsing.
    assert "- issue: #89" in body


def test_progress_body_issue_line_is_single_line_for_any_title():
    state = {
        "run_id": "abc12345",
        "issue": 7,
        "issue_title": "a\n\nb  c",
        "role": "review",
        "phase": "test",
        "elapsed": "1s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 2,
        "branch": "b",
        "pr": None,
        "session": None,
    }
    body = progress.progress_body(state)
    lines = body.splitlines()
    issue_lines = [line for line in lines if line.startswith("- issue:")]
    assert issue_lines == ["- issue: #7 a b c"]


def test_progress_body_fails_fast_without_issue_title():
    # A state without the title is a contract violation: fail fast,
    # never render a bare `#<number>` (that would hide the violation).
    state = {
        "run_id": "abc12345",
        "issue": 18,
        "role": "implement",
        "phase": "starting",
        "elapsed": "0s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    }
    with pytest.raises(KeyError):
        progress.progress_body(state)


def test_progress_body_starts_with_hidden_run_marker():
    body = progress.progress_body({
        "run_id": "abc12345",
        "issue": 18,
        "issue_title": "Publish progress",
        "role": "implement",
        "phase": "test",
        "elapsed": "3m 12s",
        "last_activity": "2026-08-25T02:30:00Z",
        "last_action": "bash pytest tests/",
        "tests": "156 passed",
        "review_round": 0,
        "branch": "orbi/xqliu-orbi-issue-18",
        "pr": None,
        "session": "sess-1",
    })
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=abc12345 -->"
    assert "**Orbi progress**" in body
    assert "- issue: #18 Publish progress" in body
    assert "- role: implement" in body
    assert "- phase: test" in body
    assert "- elapsed: 3m 12s" in body
    assert "- last activity: 2026-08-25T02:30:00Z" in body
    assert "- last action: bash pytest tests/" in body
    assert "- tests: 156 passed" in body
    assert "- review/fix round: 0" not in body
    assert "- review:" not in body
    assert "- branch: orbi/xqliu-orbi-issue-18" in body
    assert "- PR: -" in body
    assert "- session: sess-1" in body


def test_progress_body_marks_missing_values_as_dash():
    body = progress.progress_body({
        "run_id": "abc12345",
        "issue": 18,
        "issue_title": "Publish progress",
        "role": "implement",
        "phase": "starting",
        "elapsed": "0s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    })
    assert "- last activity: -" in body
    assert "- last action: -" in body
    assert "- tests: -" in body
    assert "- session: -" in body


def test_progress_body_shows_pr_url_when_present():
    state = {
        "run_id": "abc12345",
        "issue": 18,
        "issue_title": "Publish progress",
        "role": "implement",
        "phase": "pr",
        "elapsed": "1h 0m",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 1,
        "branch": "b",
        "pr": "https://github.com/xqliu/orbi/pull/40",
        "session": None,
    }
    body = progress.progress_body(state)
    assert (
        "- PR: https://github.com/xqliu/orbi/pull/40" in body
    )
    assert "- review/fix round: 1" not in body
    body = progress.progress_body({**state, "review_round": 3})
    assert body.index("- review/fix round: 3") < body.index("<details>")


def test_progress_body_shows_priority_field():
    """The progress details retain the pickup priority value."""
    state = {
        "run_id": "abc12345",
        "issue": 7,
        "issue_title": "p0 outage",
        "role": "implement",
        "phase": "test",
        "elapsed": "3m 12s",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    }
    body = progress.progress_body({**state, "priority": "p0"})
    # Priority is bookkeeping and is deliberately inside the fold.
    assert body.index("- priority: p0") > body.index("<details>")
    body = progress.progress_body({**state, "priority": "normal"})
    assert "- priority: normal" in body


def test_progress_body_assigns_status_fields_to_visible_or_folded_sections():
    """Issue #1165: the live comment keeps the user-facing status visible
    and folds identifiers/bookkeeping only.

    The marker assertions deliberately check positions relative to the fold,
    not merely that the marker strings occur somewhere in the body.
    """
    body = progress.progress_body({
        "run_id": "abc12345", "issue": 1165,
        "issue_title": "Progress comment fold",
        "role": "implement", "priority": "normal", "phase": "test",
        "elapsed": "3m", "last_activity": "now",
        "last_action": "pytest", "tests": "10 passed",
        "review_round": 3, "review": "pending", "pr": "-",
        "branch": "orbi/task", "session": "sess-1", "recovery": "term",
    })
    open_tag = "<details><summary>Run details</summary>"
    close_tag = "</details>"
    open_at = body.index(open_tag)
    close_at = body.index(close_tag)
    assert body.index("<!-- orbi:run=") < open_at
    assert body.index("<!-- runner=") > close_at
    assert body[close_at + len(close_tag):].startswith("\n\n")

    def field_names(text):
        return {
            line[2:].split(":", 1)[0].split("=", 1)[0]
            for line in text.splitlines() if line.startswith("- ")
        }

    visible = field_names(body[:open_at])
    folded = field_names(body[open_at:close_at])
    assert visible == {
        "role", "last activity", "tests", "PR", "recovery",
        "review/fix round",
    }
    assert folded == {
        "issue", "run_id", "priority", "phase", "elapsed", "last action",
        "branch", "session",
    }


def test_progress_body_shows_recovery_field_only_when_active():
    """Issue #94: the live progress comment shows the idle-recovery
    state (`term` / `kill`) at the end of the body while the runner
    is recovering a stalled session; without it the body is exactly
    the pre-#94 shape (no empty recovery line)."""
    state = {
        "run_id": "abc12345",
        "issue": 94,
        "issue_title": "Idle recovery",
        "role": "implement",
        "phase": "test",
        "elapsed": "6m",
        "last_activity": None,
        "last_action": None,
        "tests": None,
        "review_round": 0,
        "branch": "b",
        "pr": None,
        "session": None,
    }
    body = progress.progress_body(state)
    assert "- recovery" not in body
    body = progress.progress_body({**state, "recovery": "term"})
    lines = body.splitlines()
    details_start = lines.index("<details><summary>Run details</summary>")
    close_index = lines.index("</details>")
    assert lines.index("- recovery: term") < details_start
    assert lines[close_index + 1] == ""
    for anchor in ("<!-- orbi:run=abc12345 -->", "<!-- runner="):
        anchor_index = next(i for i, line in enumerate(lines)
                            if line.startswith(anchor))
        assert not details_start < anchor_index < close_index
    assert lines[-1].startswith("<!-- runner=")
    body = progress.progress_body({**state, "recovery": "kill"})
    assert body.index("- recovery: kill") < body.index("<details>")


def make_publisher(run_command=None, comments=None, posted=None,
                   post_response=None):
    """Build a ProgressPublisher over a fake gh layer.

    `post_response` mimics real `gh api`: a POST of a comment replies with
    the full comment JSON object (not a bare id).
    """
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        # Only the plain GET of the comment list returns the payload; POST
        # replies with the new comment object, PATCH replies empty.
        if (command[:2] == ["gh", "api"] and "--method" not in command
                and command[2].endswith("/comments")):
            return json.dumps(comments or [])
        if "--method" in command and "POST" in command:
            return post_response if post_response is not None else (
                json.dumps({"id": 42, "body": "created", "url": "u"})
            )
        return ""

    publisher = progress.ProgressPublisher(
        18, "xqliu/orbi", "abc12345",
        run_command=fake_run_command,
    )
    return publisher, calls


def test_progress_comment_writes_use_body_free_log_commands():
    commands = []
    logged = []

    def fake_run_command(command, **kwargs):
        commands.append(command)
        if "log_command" in kwargs:
            logged.append(kwargs["log_command"])
        if "--method" not in command:
            return json.dumps([])
        if "POST" in command:
            return json.dumps({"id": 42})
        return ""

    publisher = progress.ProgressPublisher(
        18, "xqliu/orbi", "abc12345", run_command=fake_run_command,
    )
    publisher.ensure("body with\nfull markdown")
    publisher.patch("updated body with\nfull markdown")
    publisher.milestone("tests passed")

    assert logged == [
        ["gh", "api", "repos/xqliu/orbi/issues/18/comments", "--method", "POST"],
        ["gh", "api", "repos/xqliu/orbi/issues/comments/42", "--method", "PATCH"],
        ["gh", "api", "repos/xqliu/orbi/issues/18/comments", "--method", "POST"],
    ]
    assert all("--field" not in command for command in logged)
    assert commands[2][-1] == "body=updated body with\nfull markdown"


def test_publisher_ensure_creates_comment_when_marker_missing():
    publisher, calls = make_publisher()
    comment_id = publisher.ensure("initial body")
    assert comment_id == 42
    assert publisher.comment_id == 42
    assert calls[0] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--paginate",
    ]
    assert calls[1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field", "body=initial body",
    ]


def test_publisher_ensure_patches_existing_progress_comment():
    existing = {
        "id": 7,
        "body": (
            "<!-- orbi:run=abc12345 -->\n\n"
            "**Orbi progress**\n\n- issue: #18"
        ),
    }
    publisher, calls = make_publisher(comments=[
        {"id": 1, "body": "unrelated"},
        existing,
    ])
    comment_id = publisher.ensure("new body")
    assert comment_id == 7
    assert publisher.comment_id == 7
    assert calls == [
        [
            "gh", "api", "repos/xqliu/orbi/issues/18/comments",
            "--paginate",
        ],
        [
            "gh", "api", "repos/xqliu/orbi/issues/comments/7",
            "--method", "PATCH", "--field", "body=new body",
        ],
    ]


def test_publisher_ensure_never_hijacks_scene_comments():
    # The run's scene comments (started Pi / opened PR) and milestones
    # carry the run marker too: ensure must create a fresh progress
    # comment instead of PATCHing one of them (Issue #18).
    scene = {"id": 3, "body": "<!-- orbi:run=abc12345 -->started Pi"}
    milestone = {
        "id": 4,
        "body": "<!-- orbi:run=abc12345 -->Orbi: started",
    }
    publisher, calls = make_publisher(comments=[scene, milestone])
    comment_id = publisher.ensure("initial body")
    assert comment_id == 42
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field", "body=initial body",
    ]


def test_find_progress_comment_requires_marker_and_header():
    comments = [
        {"id": 1, "body": "<!-- orbi:run=abc12345 -->scene"},
        {"id": 2, "body": "**Orbi progress**"},
        {
            "id": 3,
            "body": (
                "<!-- orbi:run=abc12345 -->\n\n"
                "**Orbi progress**"
            ),
        },
        {"id": 4},
    ]
    found = progress.find_progress_comment(comments, "abc12345")
    assert found["id"] == 3
    assert progress.find_progress_comment(comments, "deadbeef") is None


def test_publisher_ensure_rejects_non_list_comment_payload():
    publisher, _ = make_publisher(comments="not a list")
    with pytest.raises(ValueError, match="must be a JSON array"):
        publisher.ensure("body")


def test_publisher_patch_updates_the_tracked_comment():
    publisher, calls = make_publisher(comments=[
        {
            "id": 7,
            "body": (
                "<!-- orbi:run=abc12345 -->\n\n"
                "**Orbi progress**"
            ),
        },
    ])
    publisher.ensure("old")
    publisher.patch("updated body")
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/7",
        "--method", "PATCH", "--field", "body=updated body",
    ]


def test_publisher_patch_uses_the_github_update_comment_endpoint():
    # Issue #58: the production PATCH 404s because the comment id was
    # appended to the list/create URL (repos/{repo}/issues/{issue}/
    # comments/{id}), which is not a GitHub REST route. Update an issue
    # comment is PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}
    # — no issue number.
    publisher, calls = make_publisher(comments=[
        {
            "id": 7,
            "body": (
                "<!-- orbi:run=abc12345 -->\n\n"
                "**Orbi progress**"
            ),
        },
    ])
    publisher.ensure("old")
    publisher.patch("updated body")
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/7",
        "--method", "PATCH", "--field", "body=updated body",
    ]
    # List/create keep the issue-scoped endpoint (that route is correct
    # for GET and POST).
    assert calls[0] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--paginate",
    ]


def test_publisher_patch_fails_fast_without_tracked_comment():
    publisher, calls = make_publisher()
    with pytest.raises(RuntimeError, match="no progress comment"):
        publisher.patch("body")
    assert calls == []


def test_publisher_milestone_omits_result_for_only_key_value_fields():
    publisher, calls = make_publisher()
    publisher.milestone("started: base_branch=main")
    body = calls[0][-1]
    assert "- base_branch: main" in body
    assert "- result:" not in body


def test_publisher_merged_milestone_keeps_result_visible_and_hash_collapsed():
    publisher, calls = make_publisher()
    publisher.milestone("merged: https://example.test merge_commit=m1")
    body = calls[0][-1]
    visible, details = body.split("<details><summary>Run details</summary>", 1)
    assert "- result: https://example.test" in visible
    assert "merge_commit" not in visible
    assert "- merge_commit: m1" in details


def test_publisher_milestone_keeps_prose_and_key_value_fields():
    publisher, calls = make_publisher()
    publisher.milestone("blocked: base_branch=develop base_branch=main")
    body = calls[0][-1]
    assert "- result: base_branch=develop base_branch=main" in body
    assert "- base_branch: main" in body
    assert "<details>" not in body


def test_publisher_milestone_folds_long_result_but_keeps_short_fields_visible():
    publisher, calls = make_publisher()
    diagnosis = "x" * 201
    publisher.milestone(f"blocked: enabled=false {diagnosis}")
    body = calls[0][-1]
    visible, details = body.split(
        "<details><summary>Run details</summary>", 1,
    )
    assert "- enabled: false" in visible
    assert f"- result: enabled=false {diagnosis}" in details
    assert "- result:" not in visible


def test_publisher_milestone_keeps_result_inline_at_threshold():
    publisher, calls = make_publisher()
    diagnosis = "x" * 200
    publisher.milestone(f"blocked: {diagnosis}")
    body = calls[0][-1]
    assert f"- result: {diagnosis}" in body
    assert "<details>" not in body


def test_publisher_milestone_folds_short_multiline_result():
    publisher, calls = make_publisher()
    publisher.milestone("blocked: first line\nsecond line")
    body = calls[0][-1]
    visible, details = body.split(
        "<details><summary>Run details</summary>", 1,
    )
    assert "- result:" not in visible
    assert "- result: first line\nsecond line" in details


def test_publisher_milestone_posts_multiline_field_block(monkeypatch):
    # The runner fingerprint is the executing checkout's HEAD, so the
    # expected marker must not hardcode one checkout's sha (Issue #529:
    # the literal 0531f7da failed on every other checkout, e.g. the CI
    # pull_request merge commit).
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    publisher, calls = make_publisher()
    publisher.milestone("tests passed: 156 passed in 4.43s")
    assert calls == [
        [
            "gh", "api", "repos/xqliu/orbi/issues/18/comments",
            "--method", "POST",
            "--field",
            "body=<!-- orbi:run=abc12345 -->\n"
            "Orbi: tests passed\n"
            "- result: 156 passed in 4.43s\n"
            "- run_id=abc12345\n"
            "\n<!-- runner=8a12fb1c -->",
        ],
    ]
    assert publisher.comment_id is None


def test_publisher_milestone_places_a_failure_block_under_the_run_marker():
    # Issue #1322: a blocked/fix-needed milestone is a failure comment, so
    # its hidden `orbi:failure:v1` block sits directly under the run marker
    # where a status reader finds it without parsing the visible prose.
    publisher, calls = make_publisher()
    block = '<!-- orbi:failure:v1 {"schema":1} -->'
    publisher.milestone("blocked: boom", block=block)
    body = calls[0][-1]
    assert body.startswith(
        f"body=<!-- orbi:run=abc12345 -->\n{block}\nOrbi: blocked\n"
    )
    assert body.count(block) == 1
    # The block never replaces the milestone's own content.
    assert "\n- result: boom\n- run_id=abc12345\n" in body
    # A milestone without a block is byte-identical to the pre-#1322 shape.
    publisher.milestone("tests passed: ok")
    assert calls[1][-1].startswith(
        "body=<!-- orbi:run=abc12345 -->\nOrbi: tests passed\n"
    )


def test_publisher_post_parses_full_comment_object_response():
    # Real `gh api` replies with the full comment object, not a bare id.
    publisher, _ = make_publisher(
        post_response=json.dumps({
            "id": 5405315184, "body": "x", "url": "https://x/5405315184",
        }),
    )
    assert publisher.ensure("body") == 5405315184
    assert publisher.comment_id == 5405315184


def test_publisher_post_rejects_response_without_integer_id():
    publisher, _ = make_publisher(post_response="{}")
    with pytest.raises(ValueError, match="integer id"):
        publisher.ensure("body")


def test_publisher_post_rejects_non_json_response():
    publisher, _ = make_publisher(post_response="not json")
    with pytest.raises(json.JSONDecodeError):
        publisher.ensure("body")


def test_publisher_finish_patches_final_summary_into_tracked_comment():
    publisher, calls = make_publisher(comments=[
        {
            "id": 7,
            "body": (
                "<!-- orbi:run=abc12345 -->\n\n"
                "**Orbi progress**"
            ),
        },
    ])
    publisher.ensure("old")
    publisher.finish("final delivery summary")
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/7",
        "--method", "PATCH", "--field", "body=final delivery summary",
    ]


def test_publisher_finish_creates_final_comment_without_tracked_one():
    # Issue #474: a run that fails before `ensure` (release declaration
    # parse error) still publishes its final result. `finish` creates
    # the progress comment instead of raising "no progress comment".
    publisher, calls = make_publisher()
    publisher.finish("final delivery summary")
    assert publisher.comment_id == 42
    assert calls == [
        [
            "gh", "api", "repos/xqliu/orbi/issues/18/comments",
            "--paginate",
        ],
        [
            "gh", "api", "repos/xqliu/orbi/issues/18/comments",
            "--method", "POST",
            "--field", "body=final delivery summary",
        ],
    ]


def test_publisher_finish_resumes_existing_comment_without_tracked_one():
    # Issue #474: a resumed run whose publisher has not run `ensure` yet
    # must PATCH the run's existing progress comment, never duplicate it.
    publisher, calls = make_publisher(comments=[
        {
            "id": 7,
            "body": (
                "<!-- orbi:run=abc12345 -->\n\n"
                "**Orbi progress**"
            ),
        },
    ])
    publisher.finish("final delivery summary")
    assert publisher.comment_id == 7
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/7",
        "--method", "PATCH", "--field", "body=final delivery summary",
    ]
    assert not any(
        "POST" in command for command in calls
    ), "a resumed finish must not post a duplicate comment"


def test_publisher_failure_scene_updates_identical_failure_comment():
    """Issue #645: the same run's identical recoverable-failure scene
    comment is updated in place (the progress patch path), never
    appended a second time."""
    body = (
        "<!-- orbi:run=abc12345 -->\n"
        "Orbi Pi failure recovered: 429 quota; the run is recoverable"
    )
    # The stored comment went through the same rendering as the new one
    # (the hidden runner fingerprint is appended by every publish).
    publisher, calls = make_publisher(comments=[
        {"id": 5, "body": progress.format_status_comment(body)},
    ])
    publisher.failure_scene(body)
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/5",
        "--method", "PATCH", "--field",
        f"body={progress.format_status_comment(body)}",
    ]


def test_publisher_failure_scene_posts_when_failure_content_differs():
    """A DIFFERENT failure (or a comment without a body) never matches:
    the new scene is posted as its own comment — no failure evidence is
    lost or rewritten."""
    existing = progress.format_status_comment(
        "<!-- orbi:run=abc12345 -->\n"
        "Orbi Pi failure recovered: old error; the run is recoverable",
    )
    publisher, calls = make_publisher(comments=[
        {"id": 5, "body": existing},
        {"id": 6},
    ])
    new_body = (
        "<!-- orbi:run=abc12345 -->\n"
        "Orbi Pi failure recovered: new error; the run is recoverable"
    )
    publisher.failure_scene(new_body)
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field",
        f"body={progress.format_status_comment(new_body)}",
    ]


def test_publisher_failure_scene_never_touches_another_run():
    """The update is scoped to THIS run's comments: an identical-looking
    scene comment of another run is never hijacked."""
    other_run = progress.format_status_comment(
        "<!-- orbi:run=deadbeef -->\n"
        "Orbi Pi failure recovered: 429 quota; the run is recoverable",
    )
    publisher, calls = make_publisher(comments=[{"id": 5, "body": other_run}])
    new_body = (
        "<!-- orbi:run=abc12345 -->\n"
        "Orbi Pi failure recovered: 429 quota; the run is recoverable"
    )
    publisher.failure_scene(new_body)
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field",
        f"body={progress.format_status_comment(new_body)}",
    ]


def test_progress_state_survives_an_activity_snapshot_failure(monkeypatch, tmp_path):
    """The snapshot is best-effort: a read failure is logged and reported
    as "no session yet", it never blocks the task (Issue #18)."""
    import orbi.progress as progress

    def boom(_path):
        raise RuntimeError("unreadable session")

    monkeypatch.setattr(progress, "activity_snapshot", boom)
    state = progress._progress_state(RunContext(run_id="a1b2c3d4", issue=4, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="review", started=0.0, pr_url=None, review_round=0, priority="normal")
    assert state["phase"] == "starting"
    assert state["last_activity"] is None
    assert state["session"] is None


def test_failure_marker_renders_and_validates_the_fingerprint():
    """Issue #825: the hidden failure-fingerprint marker renders from a
    16-hex fingerprint and rejects anything else."""
    marker = progress.failure_marker("01e1f4a35a2fe1e5")
    assert marker == "<!-- orbi:fail=01e1f4a35a2fe1e5 -->"
    with pytest.raises(ValueError, match="invalid failure fingerprint"):
        progress.failure_marker("nothex")
    with pytest.raises(ValueError, match="invalid failure fingerprint"):
        progress.failure_marker("0" * 17)


def test_failure_repeat_count_and_bump_round_trip():
    """Issue #825: the optional `:<count>` suffix is the deduped
    failure's occurrence count — a count-less marker counts 1, the bump
    raises it in place, a body without the marker counts 0 and refuses
    to bump."""
    body = (
        "<!-- orbi:run=01e1f4a3 -->\n<!-- orbi:fail=01e1f4a35a2fe1e5 -->\n"
        "Orbi needs a fix: boom"
    )
    assert progress.failure_repeat_count(body) == 1
    once = progress.bump_failure_repeat(body, "01e1f4a35a2fe1e5")
    assert once == body.replace(
        "<!-- orbi:fail=01e1f4a35a2fe1e5 -->",
        "<!-- orbi:fail=01e1f4a35a2fe1e5:2 -->",
    )
    assert progress.failure_repeat_count(once) == 2
    twice = progress.bump_failure_repeat(once, "01e1f4a35a2fe1e5")
    assert "<!-- orbi:fail=01e1f4a35a2fe1e5:3 -->" in twice
    assert progress.failure_repeat_count(
        "<!-- orbi:fail=ffffffffffffffff -->\nx") == 1
    assert progress.failure_repeat_count("no marker") == 0
    assert progress.failure_repeat_count(None) == 0
    with pytest.raises(ValueError, match="does not carry the failure marker"):
        progress.bump_failure_repeat("no marker", "01e1f4a35a2fe1e5")
    with pytest.raises(ValueError, match="does not carry the failure marker"):
        progress.bump_failure_repeat(
            body, "ffffffffffffffff")
    with pytest.raises(ValueError, match="must be a string"):
        progress.bump_failure_repeat(None, "01e1f4a35a2fe1e5")
