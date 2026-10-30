"""Unit tests for progress: automatic GitHub progress publishing (Issue #18).

The runner keeps exactly one live progress comment per run on the source
Issue. The comment carries a hidden HTML run marker so a restarted process
finds the same comment and keeps PATCHing it — no database. Milestones are
short standalone comments so GitHub Mobile pushes a notification.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from orbi import progress, progress_summary, run_state, scene
from orbi.delivery_scene import RunContext

from tests.fakes import runner_marker as runner_marker_fakes
from tests.fakes.progress import freeze_report_time


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
    from orbi import review_merge
    assert run_state.parse_pr_comment(comment)["run_id"] == "a1b2c3d4"


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


def test_only_progress_renders_the_run_marker():
    """Issue #2003: `run_marker` is the one producer of the marker.

    A hand-written `f"<!-- orbi:run={run_id} -->"` in another module is a
    second definition of the public marker: it skips the run-id
    validation `run_marker` applies and drifts the moment the marker
    format changes. The only literal marker left in the package is
    `progress.RUN_MARKER_TEMPLATE`.
    """
    package = Path(progress.__file__).parent
    offenders = sorted(
        f"{path.relative_to(package)}:{number}: {line.strip()}"
        for path in package.rglob("*.py")
        if path.name != "progress.py"
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        )
        if "orbi:run={" in line
    )
    assert offenders == []


def test_the_run_marker_pattern_uses_the_journal_run_id_definition():
    """Issue #2003: the id format has one owner, `journal.validate_run_id`.

    `progress.RUN_MARKER_PATTERN` interpolates `journal.RUN_ID_PATTERN`
    instead of spelling the id out again, so a reader can never accept an
    id the writer refuses (or the reverse).
    """
    from orbi import journal

    assert progress.RUN_MARKER_PATTERN.pattern == (
        "<!-- orbi:run=(" + journal.RUN_ID_PATTERN.pattern + ") -->"
    )


def test_runner_fingerprint_uses_editable_checkout_head(monkeypatch, tmp_path):
    package = tmp_path / "checkout" / "src" / "orbi"
    package.mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    runner_marker_fakes.use_package(
        monkeypatch, package, module_file="runner_marker.py",
    )
    runner_marker_fakes.fake_git(monkeypatch, "8a12fb1c\n")
    assert progress.runner_fingerprint() == "8a12fb1c"


def test_runner_fingerprint_uses_package_version_when_not_editable(
    monkeypatch, tmp_path,
):
    package = tmp_path / "site-packages" / "orbi"
    package.mkdir(parents=True)
    runner_marker_fakes.use_package(
        monkeypatch, package, module_file="runner_marker.py",
    )
    seen: list[str] = []
    runner_marker_fakes.fake_version(monkeypatch, seen)
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
    runner_marker_fakes.use_package(
        monkeypatch, package, module_file="runner_marker.py",
    )
    runner_marker_fakes.fake_git(monkeypatch, "not-a-sha\n")
    assert progress.runner_fingerprint() == "unknown"


def test_runner_fingerprint_returns_unknown_when_detection_fails(
    monkeypatch, tmp_path,
):
    package = tmp_path / "checkout" / "src" / "orbi"
    package.mkdir(parents=True)
    (tmp_path / "checkout" / ".git").mkdir()
    runner_marker_fakes.use_package(
        monkeypatch, package, module_file="runner_marker.py",
    )
    runner_marker_fakes.failing_git(
        monkeypatch, subprocess.CalledProcessError(128, ["git"]),
    )
    assert progress.runner_fingerprint() == "unknown"


def test_structured_comment_places_runner_marker_at_end(monkeypatch):
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    body = progress.field_block("abc12345", "Orbi: started", {"run_id": "abc12345"}, language_code="en")
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
    body = progress.progress_body({"language": "en",
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
    runner_marker_fakes.failing_git(monkeypatch)
    for body in (
        progress.progress_body({"language": "en",
            "run_id": "abc12345", "issue": 18,
            "issue_title": "Publish progress", "role": "implement",
            "phase": "test", "elapsed": "1s", "last_activity": None,
            "last_action": None, "tests": None, "review_round": 0,
            "branch": "b", "pr": None, "session": None,
        }),
        progress.field_block("abc12345", "Orbi: tests passed", {}, language_code="en"),
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
    body = progress.progress_body({"language": "en",
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
    # Issue #1826: the identity is the summary's first line, above the
    # routine status rows; the `#89` prefix stays for existing parsing.
    lines = body.splitlines()
    header_at = lines.index("**Orbi progress**")
    assert lines[header_at + 2] == (
        "**#89 Bug: resume 使用评论里的 PR URL，不再 verify_pr**"
    )
    assert "- issue:" not in body


def test_progress_body_issue_line_is_single_line_for_any_title():
    state = {"language": "en",
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
    issue_lines = [
        line for line in body.splitlines() if line.startswith("**#7 ")
    ]
    assert issue_lines == ["**#7 a b c**"]


def test_progress_body_fails_fast_without_issue_title():
    # A state without the title is a contract violation: fail fast,
    # never render a bare `#<number>` (that would hide the violation).
    state = {"language": "en",
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
    """Issue #1657: the run marker is followed immediately by the
    versioned summary block; the readable fields render the same value."""
    body = progress.progress_body({"language": "en",
        "run_id": "abc12345",
        "issue": 18,
        "issue_title": "Publish progress",
        "role": "implement",
        "phase": "test",
        "elapsed": "3m 12s",
        "last_activity": "2026-08-25T02:30:00Z",
        "activity": {"at": "2026-08-25T02:30:00Z", "kind": "command",
                     "target": None},
        "tests": "156 passed",
        "review_round": 0,
        "branch": "orbi/xqliu-orbi-issue-18",
        "pr": None,
        "session": "sess-1",
    })
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=abc12345 -->"
    assert lines[1].startswith("<!-- orbi:progress:v1 {")
    assert lines[1].endswith(" -->")
    assert "**Orbi progress**" in body
    assert "**#18 Publish progress**" in body
    assert "- role: implement" in body
    assert "- phase: test" in body
    assert "- elapsed: 3m 12s" in body
    assert "- step: implement" in body
    assert "- reported at: " in body
    assert "- last activity: 2026-08-25T02:30:00Z" in body
    assert "- activity: command" in body
    # The raw command never reaches either representation.
    assert "bash pytest tests/" not in body
    assert "- tests: 156 passed" in body
    assert "- review/fix round: 0" not in body
    assert "- review:" not in body
    assert "- branch: orbi/xqliu-orbi-issue-18" in body
    assert "- PR: -" in body
    assert "- session: sess-1" in body


def test_progress_body_marks_missing_values_as_dash():
    body = progress.progress_body({"language": "en",
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
    assert "- step: implement" in body
    assert "- activity: -" in body
    assert "- tests: -" in body
    assert "- session: -" in body


def test_progress_body_shows_pr_url_when_present():
    state = {"language": "en",
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
    body = progress.progress_body({"language": "en",**state, "review_round": 3})
    assert body.index("- review/fix round: 3") < body.index("<details>")


def test_progress_body_shows_priority_field():
    """The progress details retain the pickup priority value."""
    state = {"language": "en",
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
    body = progress.progress_body({"language": "en",**state, "priority": "p0"})
    # Priority is bookkeeping and is deliberately inside the fold.
    assert body.index("- priority: p0") > body.index("<details>")
    body = progress.progress_body({"language": "en",**state, "priority": "normal"})
    assert "- priority: normal" in body


def test_progress_body_assigns_status_fields_to_visible_or_folded_sections():
    """Issue #1165: the live comment keeps the user-facing status visible
    and folds identifiers/bookkeeping only.

    The marker assertions deliberately check positions relative to the fold,
    not merely that the marker strings occur somewhere in the body.
    """
    body = progress.progress_body({"language": "en",
        "run_id": "abc12345", "issue": 1165,
        "issue_title": "Progress comment fold",
        "role": "implement", "priority": "normal", "phase": "test",
        "elapsed": "3m", "last_activity": "now",
        "activity": {"at": "2026-10-15T06:20:18Z", "kind": "read",
                     "target": "src/login.ts"},
        "tests": "10 passed",
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
    # Issue #1826: the task identity is the summary above the field rows,
    # not a folded identifier.
    assert "**#1165 Progress comment fold**" in body[:open_at]
    assert visible == {
        "role", "step", "reported at", "last activity", "activity",
        "tests", "PR", "recovery", "review/fix round",
    }
    assert folded == {
        "run_id", "priority", "phase", "elapsed",
        "branch", "session",
    }


def test_progress_body_shows_recovery_field_only_when_active():
    """Issue #94: the live progress comment shows the idle-recovery
    state (`term` / `kill`) at the end of the body while the runner
    is recovering a stalled session; without it the body is exactly
    the pre-#94 shape (no empty recovery line)."""
    state = {"language": "en",
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
    body = progress.progress_body({"language": "en",**state, "recovery": "term"})
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
    body = progress.progress_body({"language": "en",**state, "recovery": "kill"})
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
        run_command=fake_run_command, language_code="en",
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
        18, "xqliu/orbi", "abc12345", run_command=fake_run_command, language_code="en",
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


def make_stateful_publisher(comments=None):
    """A ProgressPublisher over a stateful fake comment store.

    GET lists, POST appends with a fresh id, PATCH updates the exact
    comment by id — the real `gh api` shape (Issue #1369).
    """
    comments = comments if comments is not None else []
    posts: list[dict] = []
    patches: list[tuple[int, str]] = []
    next_id = iter(range(500, 900))

    def fake_run_command(command, **kwargs):
        if command[0] == "gh" and command[1] == "api" and "--method" not in command:
            return json.dumps([dict(c) for c in comments])
        method = command[command.index("--method") + 1]
        body = command[command.index("--field") + 1][len("body="):]
        if method == "POST":
            comment = {"id": next(next_id), "body": body}
            comments.append(comment)
            posts.append(comment)
            return json.dumps(comment)
        comment_id = int(command[2].rsplit("/", 1)[1])
        patches.append((comment_id, body))
        for comment in comments:
            if comment["id"] == comment_id:
                comment["body"] = body
        return ""

    publisher = progress.ProgressPublisher(
        18, "xqliu/orbi", "abc12345", run_command=fake_run_command, language_code="en",
    )
    return publisher, comments, posts, patches


def _started_body(base_sha: str) -> str:
    return (
        f"{progress.run_marker('abc12345')}\n"
        "Orbi started Pi: run_id=abc12345 priority=normal\n"
        f"- base_sha: {base_sha}\n"
        "<!-- runner=deadbeef -->"
    )


def test_find_started_comment_requires_marker_and_headline():
    comments = [
        {"id": 1, "body": "<!-- orbi:run=abc12345 -->\n**Orbi progress**"},
        {"id": 2, "body": "Orbi started Pi: run_id=abc12345"},
        {
            "id": 3,
            "body": (
                "<!-- orbi:run=abc12345 -->\n"
                "Orbi started Pi: run_id=abc12345 priority=normal"
            ),
        },
        {"id": 4},
    ]
    found = progress.find_started_comment(comments, "abc12345")
    assert found["id"] == 3
    assert progress.find_started_comment(comments, "deadbeef") is None


def test_publisher_started_posts_once_then_patches_on_resume():
    """Issue #1369: the first tick POSTs the started comment, every
    resume PATCHes the same comment id (one POST, three PATCHes)."""
    publisher, comments, posts, patches = make_stateful_publisher()
    for round_no in range(4):
        publisher.started(_started_body(f"sha{round_no}"))

    assert len(posts) == 1
    assert len(patches) == 3
    assert {cid for cid, _ in patches} == {posts[0]["id"]}
    assert len(comments) == 1
    # The updated comment still carries the anchors the engine parses.
    assert "<!-- orbi:run=abc12345 -->" in comments[0]["body"]
    assert "<!-- runner=deadbeef -->" in comments[0]["body"]
    assert "base_sha: sha3" in comments[0]["body"]


def test_publisher_started_recreates_a_human_deleted_comment():
    """When the started comment could not be found (deleted by a human),
    the resume POSTs one new comment and later resumes update that one."""
    publisher, comments, posts, patches = make_stateful_publisher()
    publisher.started(_started_body("sha0"))
    publisher.started(_started_body("sha1"))
    # A human deletes the comment: the next resume finds nothing.
    comments.clear()
    publisher.started(_started_body("sha2"))
    publisher.started(_started_body("sha3"))

    assert len(posts) == 2, "a deleted comment must be recreated once"
    assert [cid for cid, _ in patches] == [
        posts[0]["id"], posts[1]["id"],
    ]
    assert len(comments) == 1
    assert "base_sha: sha3" in comments[0]["body"]


def test_publisher_started_patches_the_started_comment_among_run_comments():
    """The patch target is the started comment, not the run's other
    marker-carrying comments (Issue #1369)."""
    other = {
        "id": 700,
        "body": "<!-- orbi:run=abc12345 -->\n**Orbi progress**",
    }
    started = {
        "id": 701,
        "body": (
            "<!-- orbi:run=abc12345 -->\n"
            "Orbi started Pi: run_id=abc12345 priority=normal"
        ),
    }
    publisher, comments, posts, patches = make_stateful_publisher(
        comments=[other, started],
    )
    publisher.started(_started_body("sha9"))

    assert posts == []
    assert patches == [(701, _started_body("sha9"))]
    assert comments[0]["body"] == other["body"]
    assert comments[1]["body"] == _started_body("sha9")


def test_publisher_ensure_ignores_the_started_comment():
    """The started scene is not the live progress comment: `ensure`
    must create a fresh progress comment instead of hijacking it."""
    existing_started = {
        "id": 9,
        "body": (
            "<!-- orbi:run=abc12345 -->\n"
            "Orbi started Pi: run_id=abc12345 priority=normal"
        ),
    }
    publisher, comments, posts, patches = make_stateful_publisher(
        comments=[existing_started],
    )
    comment_id = publisher.ensure("<!-- orbi:run=abc12345 -->\n**Orbi progress**")
    assert comment_id == posts[0]["id"]
    assert patches == []
    assert len(comments) == 2


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


def _recovered_body(reason_code: str, detail: str = "boom") -> str:
    return (
        "<!-- orbi:run=abc12345 -->\n"
        f"<!-- orbi:recovered={reason_code} -->\n"
        f"Orbi Pi failure recovered: {reason_code}; retrying on the next "
        "tick\n\n"
        "<details><summary>Diagnosis</summary>\n\n"
        f"- failure detail: {detail}\n\n</details>"
    )


def test_publisher_failure_scene_updates_the_same_cause_comment():
    """Issue #1465: the same run's repeated SAME cause (the hidden
    `orbi:recovered` key) is PATCHed in place — the repeat count rises,
    the body carries the newest occurrence, and no second comment is
    appended."""
    body = _recovered_body("credential_missing")
    publisher, calls = make_publisher(comments=[
        {"id": 5, "body": progress.format_status_comment(body)},
    ])
    publisher.failure_scene(body)
    rendered = progress.format_status_comment(body)
    expected = progress.bump_recovered_repeat(
        rendered, "credential_missing", 1,
    )
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/comments/5",
        "--method", "PATCH", "--field", f"body={expected}",
    ]
    assert "<!-- orbi:recovered=credential_missing:2 -->" in expected


def test_publisher_failure_scene_posts_a_different_cause():
    """Issue #1465: a DIFFERENT reason_code is a different cause: it
    posts its own comment, so the previous cause's evidence is never
    rewritten or lost. A comment without a body is skipped, never
    matched."""
    existing = progress.format_status_comment(
        _recovered_body("provider_quota", "429 quota"),
    )
    publisher, calls = make_publisher(comments=[
        {"id": 5, "body": existing},
        {"id": 6},
    ])
    new_body = _recovered_body("credential_missing", "401 rejected")
    publisher.failure_scene(new_body)
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field",
        f"body={progress.format_status_comment(new_body)}",
    ]


def test_publisher_failure_scene_posts_a_body_without_a_cause_marker():
    """Issue #1465: a body that carries no `orbi:recovered` cause marker
    always posts — a failure comment is never lost."""
    publisher, calls = make_publisher(comments=[{"id": 5, "body": "x"}])
    body = "<!-- orbi:run=abc12345 -->\nOrbi something: legible"
    publisher.failure_scene(body)
    assert calls[-1] == [
        "gh", "api", "repos/xqliu/orbi/issues/18/comments",
        "--method", "POST", "--field",
        f"body={progress.format_status_comment(body)}",
    ]


def test_publisher_failure_scene_never_touches_another_run():
    """The fold is scoped by the run marker too: another run's comment
    with the same cause is never hijacked (Issue #1465)."""
    other_run = progress.format_status_comment(
        "<!-- orbi:run=deadbeef -->\n"
        "<!-- orbi:recovered=credential_missing -->\n"
        "Orbi Pi failure recovered: credential_missing; retrying on the "
        "next tick",
    )
    publisher, calls = make_publisher(comments=[{"id": 5, "body": other_run}])
    new_body = _recovered_body("credential_missing")
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
    state = progress.progress_state(RunContext(run_id="a1b2c3d4", issue=4, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="review", started=0.0, pr_url=None, review_round=0, priority="normal", language_code="en")
    assert state["phase"] == "starting"
    assert state["last_activity"] is None
    assert state["session"] is None


# --- Issue #1613: the tests field is the PR head's CI check status -----------


def _progress_state_for(tmp_path, **overrides):
    kwargs = dict(
        ctx=RunContext(
            run_id="a1b2c3d4", issue=4, branch="b",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        title="t", role="review", started=0.0, pr_url=None,
        review_round=0, priority="normal", language_code="en",
    )
    kwargs.update(overrides)
    return progress.progress_state(**kwargs)


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        (
            [{"name": "tests", "status": "COMPLETED",
              "conclusion": "SUCCESS"}],
            "passed",
        ),
        (
            [{"name": "tests", "status": "COMPLETED",
              "conclusion": "FAILURE"}],
            "failed: tests",
        ),
        (
            [
                {"name": "tests", "status": "COMPLETED",
                 "conclusion": "FAILURE"},
                {"name": "lint", "status": "COMPLETED",
                 "conclusion": "FAILURE"},
            ],
            "failed: tests, lint",
        ),
        (
            [{"name": "tests", "status": "IN_PROGRESS",
              "conclusion": None}],
            "pending",
        ),
        ([], "-"),
        (None, "-"),
    ],
)
def test_progress_state_tests_field_reports_the_ci_check_status(
    tmp_path, rollup, expected,
):
    """Issue #1613: the tests field comes only from the PR's GitHub
    Actions checks (the rollup the gates classify), never a local log
    parse; a failed check is decisive over a pending one."""
    state = _progress_state_for(tmp_path, checks=rollup)
    assert state["tests"] == expected


def test_progress_state_tests_field_is_dash_without_a_pr_or_checks(tmp_path):
    """No PR / no checks is no test result, not a fabricated pass."""
    assert _progress_state_for(tmp_path)["tests"] == "-"
    assert _progress_state_for(tmp_path, pr_url=None, checks=[])[
        "tests"] == "-"


def test_recovered_marker_renders_and_validates_the_reason_code():
    """Issue #1465: the recoverable-failure fold marker carries a closed
    reason_code and rejects anything else."""
    marker = progress.recovered_marker("credential_missing")
    assert marker == "<!-- orbi:recovered=credential_missing -->"
    with pytest.raises(ValueError, match="invalid recoverable reason code"):
        progress.recovered_marker("not a code")
    with pytest.raises(ValueError, match="invalid recoverable reason code"):
        progress.recovered_marker("made_up")


def test_recovered_repeat_count_and_bump_round_trip():
    """Issue #1465: the optional `:<count>` suffix is the recovered
    cause's occurrence count — a count-less marker counts 1, the bump
    sets it from the previous count, a body without the marker counts 0
    and refuses to bump."""
    body = (
        "<!-- orbi:run=01e1f4a3 -->\n"
        "<!-- orbi:recovered=credential_missing -->\n"
        "Orbi Pi failure recovered: credential_missing; retrying on the "
        "next tick"
    )
    assert progress.recovered_repeat_count(body) == 1
    once = progress.bump_recovered_repeat(body, "credential_missing", 1)
    assert once == body.replace(
        "<!-- orbi:recovered=credential_missing -->",
        "<!-- orbi:recovered=credential_missing:2 -->",
    )
    assert progress.recovered_repeat_count(once) == 2
    twice = progress.bump_recovered_repeat(once, "credential_missing", 2)
    assert "<!-- orbi:recovered=credential_missing:3 -->" in twice
    assert progress.recovered_repeat_count(
        "<!-- orbi:recovered=provider_quota -->\nx") == 1
    assert progress.recovered_repeat_count("no marker") == 0
    assert progress.recovered_repeat_count(None) == 0
    with pytest.raises(ValueError, match="does not carry the recovered"):
        progress.bump_recovered_repeat("no marker", "credential_missing", 1)
    with pytest.raises(ValueError, match="does not carry the recovered"):
        progress.bump_recovered_repeat(body, "provider_quota", 1)
    with pytest.raises(ValueError, match="must be a string"):
        progress.bump_recovered_repeat(None, "credential_missing", 1)


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


# --- Issue #1657: the versioned progress summary (prose + block) ---


def _summary_state(**overrides):
    """A complete state dict as `progress_state` produces it."""
    state = {"language": "en",
        "run_id": "abc12345",
        "issue": 1657,
        "issue_title": "Publish a safe versioned delivery summary",
        "role": "implement",
        "priority": "normal",
        "phase": "read",
        "elapsed": "1m 0s",
        "tests": None,
        "review_round": 0,
        "branch": "orbi/orbi-build-orbi-issue-1657",
        "pr": None,
        "session": "sess-1",
        "step": "implement",
        "activity": None,
    }
    state.update(overrides)
    return state


def _block_payload(body):
    """The parsed payload of the body's one progress block."""
    match = re.search(
        r"<!-- orbi:progress:v1 (\{.*?\}) -->", body,
    )
    assert match is not None, body
    return json.loads(match.group(1))


def test_progress_body_starts_with_the_run_marker_and_the_block(monkeypatch):
    freeze_report_time(monkeypatch)
    body = progress.progress_body(_summary_state(
        activity={"at": "2026-10-15T06:20:18Z", "kind": "read",
                  "target": "src/login.ts"},
    ))
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=abc12345 -->"
    assert lines[1] == (
        '<!-- orbi:progress:v1 {"step":"implement",'
        '"reported_at":"2026-10-15T06:20:20Z","activity":{'
        '"at":"2026-10-15T06:20:18Z","kind":"read",'
        '"target":"src/login.ts"}} -->'
    )
    assert body.count("<!-- orbi:progress:v1 ") == 1

# --- Issue #1667: a finish keeps the formal prefix in front of the outcome ---


class _FinishRecorder:
    """Capture the one body `finish` publishes."""

    language_code = "en"

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def finish(self, body: str) -> None:
        self.bodies.append(body)


def _step_publisher(tmp_path, recorder):
    ctx = RunContext(
        run_id="86b57234", issue=1667, branch="orbi/orbi-1667",
        worktree=tmp_path, source_repo="owner/repo",
    )
    return progress.step_publisher(
        recorder, ctx, role="review", title="Ship it",
        started=0.0, priority="normal",
    )


def test_merged_finish_keeps_the_formal_prefix_above_the_outcome(
        monkeypatch, tmp_path):
    """Acceptance: the first two lines are the run marker and the
    `"step":"merge"` block; `**Orbi delivered**` appears below them."""
    freeze_report_time(monkeypatch)
    recorder = _FinishRecorder()
    publish_step = _step_publisher(tmp_path, recorder)
    publish_step(
        "https://github.com/owner/repo/pull/1", 1, step="merge",
        outcome="**Orbi delivered**\n\nPR merged (merge_commit=m1)",
    )
    body = recorder.bodies[-1]
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=86b57234 -->"
    assert lines[1].startswith('<!-- orbi:progress:v1 {"step":"merge"')
    assert body.index("**Orbi delivered**") > body.index(
        "<!-- orbi:progress:v1",
    )
    assert "PR merged (merge_commit=m1)" in body


def test_blocked_finish_keeps_the_formal_prefix_above_the_explanation(
        monkeypatch, tmp_path):
    """Acceptance: a blocked finish starts with the run marker and the v1
    block; the blocked explanation follows them."""
    freeze_report_time(monkeypatch)
    body = progress.finish_progress_body(
        number=1667, title="Blocked task", run_id="86b57234",
        role="review", branch="orbi/orbi-1667", worktree=tmp_path,
        pr_url=None, review_round=0, priority="normal", detail="boom",
        next_step="", outcome="blocked", source_repo="owner/repo", language_code="en",
    )
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=86b57234 -->"
    assert lines[1].startswith("<!-- orbi:progress:v1 ")
    assert body.index("**Orbi blocked") > body.index(
        "<!-- orbi:progress:v1",
    )


CLOUD_PATH = (
    "/home/orbi-acme--app-1a2b3c4d/delivery/app/.worktrees/"
    "orbi-acme-app-issue-7-0a1b2c3d/.pi-session"
)


@pytest.mark.parametrize("outcome", ["blocked", "fix needed"])
@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_failed_finish_redacts_local_paths_in_reason_and_diagnosis(
        monkeypatch, tmp_path, outcome, language_code):
    """Issue #1721 acceptance 1: the final progress comment of a failed
    delivery never shows the runner's worktree path in "What happened"
    (and its zh twin) or the raw error, and keeps the rest of both
    sentences."""
    freeze_report_time(monkeypatch)
    body = progress.finish_progress_body(
        number=1721, title="Failed task", run_id="86b57234",
        role="implement", branch="orbi/orbi-1721", worktree=tmp_path,
        pr_url=None, review_round=0, priority="normal", detail="boom",
        next_step="", outcome=outcome, source_repo="owner/repo",
        reason=f"pi session exited 1 ({CLOUD_PATH})",
        diagnosis=f"FileNotFoundError: {CLOUD_PATH}",
        language_code=language_code,
    )
    assert CLOUD_PATH not in body
    assert "/home/" not in body
    assert "pi session exited 1 (local runner path)" in body
    assert "FileNotFoundError: local runner path" in body


def test_review_findings_finish_keeps_the_formal_prefix(
        monkeypatch, tmp_path):
    """Acceptance: a review-findings finish starts with the run marker and
    a v1 block carrying `"step":"review"`."""
    freeze_report_time(monkeypatch)
    recorder = _FinishRecorder()
    publish_step = _step_publisher(tmp_path, recorder)
    publish_step(
        "https://github.com/owner/repo/pull/1", 1,
        outcome=(
            "**Orbi review findings**\n\nround 1: 1 blocker(s); "
            "the next review session retries the same PR automatically"
        ),
    )
    body = recorder.bodies[-1]
    lines = body.splitlines()
    assert lines[0] == "<!-- orbi:run=86b57234 -->"
    assert lines[1].startswith('<!-- orbi:progress:v1 {"step":"review"')
    assert body.index("**Orbi review findings**") > body.index(
        "<!-- orbi:progress:v1",
    )

def test_progress_prose_and_block_render_the_same_activity(monkeypatch):
    freeze_report_time(monkeypatch)
    body = progress.progress_body(_summary_state(
        role="review", step="merge",
        activity={"at": "2026-10-15T06:20:18Z", "kind": "edit",
                  "target": "src/orbi/progress.py"},
    ))
    assert "- role: review" in body
    assert "- step: merge" in body
    assert "- reported at: 2026-10-15T06:20:20Z" in body
    assert "- last activity: 2026-10-15T06:20:18Z" in body
    assert "- activity: edit src/orbi/progress.py" in body
    assert _block_payload(body) == {
        "step": "merge",
        "reported_at": "2026-10-15T06:20:20Z",
        "activity": {"at": "2026-10-15T06:20:18Z", "kind": "edit",
                     "target": "src/orbi/progress.py"},
    }


def test_progress_body_makes_a_missing_activity_clear(monkeypatch):
    freeze_report_time(monkeypatch)
    body = progress.progress_body(_summary_state(activity=None))
    assert "- last activity: -" in body
    assert "- activity: -" in body
    assert _block_payload(body)["activity"] is None
    assert "command" not in body


def test_heartbeat_advances_the_report_time_not_the_activity_time(monkeypatch):
    activity = {"at": "2026-10-15T06:20:18Z", "kind": "read",
                "target": "src/login.ts"}
    freeze_report_time(monkeypatch)
    first = progress.progress_body(_summary_state(activity=activity))
    freeze_report_time(monkeypatch, "2026-10-15T06:21:10Z")
    second = progress.progress_body(_summary_state(activity=activity))
    assert _block_payload(first)["reported_at"] == "2026-10-15T06:20:20Z"
    assert _block_payload(second)["reported_at"] == "2026-10-15T06:21:10Z"
    assert _block_payload(first)["activity"] == _block_payload(second)["activity"]
    assert "- last activity: 2026-10-15T06:20:18Z" in second


def test_progress_step_comes_from_the_bound_runner_action():
    assert progress_summary.step_for(_summary_state(role="implement", step=None)) == "implement"
    assert progress_summary.step_for(_summary_state(role="review", step=None)) == "review"
    assert progress_summary.step_for(_summary_state(role="release", step=None)) == "release"
    # A content/ops/ticket-only task has no stage of its own.
    assert progress_summary.step_for(_summary_state(role="ticket", step=None)) is None
    # The Runner entered the merge gate's checks action, then merged.
    assert progress_summary.step_for(_summary_state(role="review", step="checks")) == "checks"
    assert progress_summary.step_for(_summary_state(role="review", step="merge")) == "merge"


def test_progress_step_rejects_an_undocumented_stage():
    assert progress_summary.step_for(_summary_state(step="deploy")) is None


def test_step_by_role_keys_are_the_runner_role_constants():
    """Drift guard: the map keys are the roles the Runner binds."""
    from orbi import pi_command, pi_process, release
    assert set(progress_summary.STEP_BY_ROLE) == {
        pi_process.ROLE_IMPLEMENT,
        pi_command.ROLE_REVIEW,
        release.ROLE_RELEASE,
    }
    assert pi_command.ROLE_TICKET not in progress_summary.STEP_BY_ROLE


def test_progress_steps_are_the_documented_vocabulary():
    assert set(progress_summary.PROGRESS_STEPS) == {
        "starting", "implement", "review", "checks", "merge", "release",
    }


def test_progress_block_escapes_a_value_that_cannot_terminate_the_comment(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    hostile = worktree / "src" / "a--b.py"
    activity = {"kind": "read", "target": str(hostile),
                "last_activity": "2026-10-15T06:20:18Z"}
    value = progress_summary.safe_activity(worktree, activity)
    assert value["target"] == "src/a--b.py"
    block = progress_summary.progress_block({
        "step": "implement", "reported_at": "2026-10-15T06:20:20Z",
        "activity": value,
    })
    assert block.startswith("<!-- orbi:progress:v1 {")
    assert block.endswith(" -->")
    assert block.count("<!--") == 1
    assert block.count("-->") == 1
    payload = json.loads(block[len("<!-- orbi:progress:v1 "):-4])
    assert payload["activity"] == value


def test_progress_block_escapes_an_html_terminator_in_a_value():
    """The hidden block's own guarantee: were a value ever to carry the
    characters, the serialized block still cannot be terminated early."""
    block = progress_summary.progress_block({
        "step": "implement", "reported_at": "2026-10-15T06:20:20Z",
        "activity": {"at": "2026-10-15T06:20:18Z", "kind": "read",
                     "target": "src/x-->y.py"},
    })
    assert block.count("-->") == 1
    assert "\\u003e" in block
    payload = json.loads(block[len("<!-- orbi:progress:v1 "):-4])
    assert payload["activity"]["target"] == "src/x-->y.py"


@pytest.mark.parametrize(
    "raw", ["src/x-->y.py", "src/x<!--y.py", "src/a<b.py"],
)
def test_an_html_comment_character_is_null_in_both_representations(
    tmp_path, raw,
):
    """Issue #1657: the prose publishes the same value verbatim where the
    block escapes it, so a target carrying an HTML comment character
    never reaches EITHER representation; the known kind and time stay."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    value = progress_summary.safe_activity(worktree, {
        "kind": "edit", "target": str(worktree / raw),
        "last_activity": "2026-10-15T06:20:18Z",
    })
    assert value == {
        "at": "2026-10-15T06:20:18Z", "kind": "edit", "target": None,
    }


def test_a_hostile_target_forges_no_block_in_the_rendered_body(
    monkeypatch, tmp_path,
):
    """The readable body of the trusted writer carries exactly one run
    marker, one progress block and one runner marker — never a second,
    forged block or a swallowed comment (Issue #1657)."""
    freeze_report_time(monkeypatch)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    forged = (
        'src/a<!-- orbi:progress:v1 {"step":"merge",'
        '"reported_at":"2099-01-01T00:00:00Z"} -->.py'
    )
    body = progress.progress_body(_summary_state(
        activity=progress_summary.safe_activity(worktree, {
            "kind": "read", "target": str(worktree / forged),
            "last_activity": "2026-10-15T06:20:18Z",
        }),
    ))
    assert body.count("<!--") == 3  # run marker, block, runner marker
    assert body.count("-->") == 3
    assert "2099-01-01" not in body
    assert "- activity: read\n" in body
    assert _block_payload(body)["activity"]["target"] is None


def test_safe_activity_publishes_only_the_documented_kinds(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert progress_summary.safe_activity(worktree, None) is None
    assert progress_summary.safe_activity(worktree, {}) is None
    assert progress_summary.safe_activity(worktree, {"kind": "deploy"}) is None
    assert progress_summary.safe_activity(
        worktree, {"kind": "command", "target": "/etc/passwd",
                  "last_activity": "2026-10-15T06:20:18Z"},
    ) == {
        "at": "2026-10-15T06:20:18Z",
        "kind": "command",
        "target": None,
    }
    assert progress_summary.safe_activity(
        worktree, {"kind": "analysis", "target": "/w/src/login.ts",
                  "last_activity": "not a timestamp"},
    ) == {"at": None, "kind": "analysis", "target": None}
    # A missing or non-string activity time is unknown, never invented.
    assert progress_summary.safe_activity(
        worktree, {"kind": "read", "target": "src/login.ts"},
    ) == {"at": None, "kind": "read", "target": "src/login.ts"}
    assert progress_summary.safe_activity(
        worktree, {"kind": "read", "target": "src/login.ts",
                  "last_activity": 7},
    ) == {"at": None, "kind": "read", "target": "src/login.ts"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/login.ts", "src/login.ts"),
        ("./src/login.ts", "src/login.ts"),
    ],
)
def test_safe_target_accepts_an_inside_worktree_path(tmp_path, raw, expected):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert progress_summary.safe_target(worktree, raw) == expected
    assert progress_summary.safe_target(
        worktree, str(worktree / raw),
    ) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "../../etc/passwd",
        "src/../../outside.py",
        "",
        None,
        7,
        "src/a\nb.py",
        "deep/" * 120 + "file.py",
    ],
)
def test_safe_target_rejects_outside_and_unattributable_targets(tmp_path, raw):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert progress_summary.safe_target(worktree, raw) is None
    outside = tmp_path / "outside" / "providers.json"
    assert progress_summary.safe_target(worktree, str(outside)) is None


@pytest.mark.parametrize(
    "raw",
    [
        ".git/config",
        ".orbi/plan.md",
        ".orbi/env",
        ".worktrees/other/src/x.py",
        ".pi-session/s.jsonl",
        ".pi/x",
        "plan.md",
        "test.log",
        "verify.md",
    ],
)
def test_safe_target_rejects_runtime_and_credential_paths(tmp_path, raw):
    """The shared runtime-path list, .git metadata and the
    worktree-local deploy env file are never published (Issue #1657)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert progress_summary.safe_target(worktree, raw) is None


def test_safe_target_keeps_a_nested_file_with_a_runtime_name(tmp_path):
    # Only the ROOT plan/test/verify artifacts are Runner-owned.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert progress_summary.safe_target(worktree, "docs/plan.md") == "docs/plan.md"


def test_progress_publication_patches_one_comment_with_the_block(monkeypatch):
    freeze_report_time(monkeypatch)
    publisher, comments, posts, patches = make_stateful_publisher()
    body = progress.progress_body(_summary_state(
        activity={"at": "2026-10-15T06:20:18Z", "kind": "read",
                  "target": "src/login.ts"},
    ))
    publisher.ensure(body)
    publisher.patch(body)
    assert len(posts) == 1
    assert len(patches) == 1
    assert len(comments) == 1
    assert comments[0]["body"].count("<!-- orbi:progress:v1 ") == 1
    assert _block_payload(comments[0]["body"])["step"] == "implement"
    assert progress.find_progress_comment(comments, "abc12345") is not None


def test_progress_publication_failure_stays_a_pure_bypass(monkeypatch, tmp_path):
    """A forced publication failure changes no delivery state: no
    exception, no relabel, no skipped step (Issue #79/#1657)."""
    calls = []
    def boom(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("gh api 404")
    publisher = progress.ProgressPublisher(
        18, "xqliu/orbi", "abc12345", run_command=boom, language_code="en",
    )
    progress.safe_publish(
        run_id="abc12345", issue=18, source_repo="xqliu/orbi",
        role="implement",
        action=lambda: publisher.ensure(progress.progress_body(
            _summary_state(),
        )),
    )
    assert calls
    assert publisher.comment_id is None


def test_documented_progress_fixture_matches_the_writer_output():
    """The public contract's example block is the writer's output shape."""
    text = (
        Path(__file__).resolve().parent.parent
        / "docs" / "public-contracts.mdx"
    ).read_text(encoding="utf-8")
    match = re.search(r"<!-- orbi:progress:v1 (\{.*?\}) -->", text)
    assert match is not None, "the docs page lost the progress example"
    payload = json.loads(match.group(1))
    assert set(payload) == {"step", "reported_at", "activity"}
    assert set(payload["activity"]) == {"at", "kind", "target"}
    assert payload["step"] in progress_summary.PROGRESS_STEPS
    assert progress_summary.serialize_summary(payload) == match.group(1)
    assert progress_summary.progress_block(payload) == match.group(0)


def test_progress_state_projects_the_watcher_activity_safely(tmp_path):
    """The raw session state is reduced to the one safe value where
    the worktree is known: kind/target/at, the path policy applied."""
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    ctx = RunContext(
        run_id="a1b2c3d4", issue=4, branch="b",
        worktree=worktree, source_repo="owner/repo",
    )
    state = progress.progress_state(
        ctx, title="t", role="review", started=0.0, pr_url=None,
        review_round=0, priority="normal",
        activity={
            "kind": "edit",
            "target": str(worktree / "src" / "login.ts"),
            "last_activity": "2026-10-15T06:20:18.761Z",
            "action": "edit /w/src/login.ts",
        }, language_code="en",
    )
    assert state["activity"] == {
        "at": "2026-10-15T06:20:18Z",
        "kind": "edit",
        "target": "src/login.ts",
    }
    assert state["step"] == "review"
    # An unsafe target keeps the known kind and time, target null.
    unsafe = progress.progress_state(
        ctx, title="t", role="implement", started=0.0, pr_url=None,
        review_round=0, priority="normal",
        activity={
            "kind": "read",
            "target": "/etc/passwd",
            "last_activity": "2026-10-15T06:20:18Z",
        }, language_code="en",
    )
    assert unsafe["activity"] == {
        "at": "2026-10-15T06:20:18Z", "kind": "read", "target": None,
    }
    # An unknown tool publishes no activity at all.
    unknown = progress.progress_state(
        ctx, title="t", role="implement", started=0.0, pr_url=None,
        review_round=0, priority="normal",
        activity={"kind": None, "target": "/w/secret"}, language_code="en",
    )
    assert unknown["activity"] is None


def test_progress_state_takes_the_explicit_runner_step(tmp_path):
    ctx = RunContext(
        run_id="a1b2c3d4", issue=4, branch="b",
        worktree=tmp_path, source_repo="owner/repo",
    )
    state = progress.progress_state(
        ctx, title="t", role="review", step="merge", started=0.0,
        pr_url=None, review_round=0, priority="normal", activity={}, language_code="en",
    )
    assert state["step"] == "merge"


