"""Unit tests for progress: automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker so a restarted process
finds the same comment and keeps PATCHing it — no database. Milestones are
short standalone comments so GitHub Mobile pushes a notification.
"""
import json
import subprocess

import pytest

from orbi import progress


def test_format_status_comment_non_string_is_unchanged():
    assert progress.format_status_comment(None) is None


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
    monkeypatch.setattr(progress.metadata, "version", lambda name: "0.3.4")
    assert progress.runner_fingerprint() == "0.3.4"


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


def test_find_run_comment_returns_comment_carrying_the_marker():
    comments = [
        {"id": 1, "body": "Orbi started Pi: ..."},
        {"id": 2, "body": "<!-- orbi:run=abc12345 -->\n**progress**"},
        {"id": 3, "body": "another run <!-- orbi:run=other -->"},
    ]
    found = progress.find_run_comment(comments, "abc12345")
    assert found == comments[1]


def test_find_run_comment_returns_none_when_marker_absent():
    comments = [
        {"id": 1, "body": "Orbi started Pi: ..."},
        {"id": 2, "body": "<!-- orbi:run=other -->"},
    ]
    assert progress.find_run_comment(comments, "abc12345") is None


def test_find_run_comment_returns_first_match_for_duplicate_markers():
    comments = [
        {"id": 1, "body": "<!-- orbi:run=abc12345 -->first"},
        {"id": 2, "body": "<!-- orbi:run=abc12345 -->second"},
    ]
    assert progress.find_run_comment(comments, "abc12345")["id"] == 1


def test_find_run_comment_ignores_comments_without_body():
    assert progress.find_run_comment([{"id": 1}], "abc12345") is None


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
    assert "- review/fix round: 0" in body
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
    body = progress.progress_body({
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
    })
    assert (
        "- PR: https://github.com/xqliu/orbi/pull/40" in body
    )
    assert "- review/fix round: 1" in body


def test_progress_body_shows_priority_field():
    """Issue #101: the live progress comment shows the pickup priority
    (`p0` for urgent Issues, `normal` otherwise) right after the role,
    so a mobile user sees at a glance that this run is a P0."""
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
    assert "- priority: p0" in body
    # The priority line sits between role and phase.
    lines = body.splitlines()
    assert lines.index("- priority: p0") == \
        lines.index("- role: implement") + 1
    body = progress.progress_body({**state, "priority": "normal"})
    assert "- priority: normal" in body


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
    assert "- recovery: term" in body
    # The recovery line is the last field line; the hidden runner
    # fingerprint marker (Issue #526) closes the body.
    assert body.splitlines()[-3] == "- recovery: term"
    assert body.splitlines()[-1].startswith("<!-- runner=")
    body = progress.progress_body({**state, "recovery": "kill"})
    assert body.splitlines()[-3] == "- recovery: kill"


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


def test_publisher_milestone_keeps_prose_with_key_value_fields():
    publisher, calls = make_publisher()
    publisher.milestone("merged: https://example.test merge_commit=m1")
    body = calls[0][-1]
    assert "- result: https://example.test merge_commit=m1" in body
    assert "- merge_commit: m1" in body


def test_publisher_milestone_keeps_prose_and_key_value_fields():
    publisher, calls = make_publisher()
    publisher.milestone("blocked: base_branch=develop base_branch=main")
    body = calls[0][-1]
    assert "- result: base_branch=develop base_branch=main" in body
    assert "- base_branch: main" in body


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


def test_publisher_finish_fails_fast_without_tracked_comment():
    publisher, _ = make_publisher()
    with pytest.raises(RuntimeError, match="no progress comment"):
        publisher.finish("summary")


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
