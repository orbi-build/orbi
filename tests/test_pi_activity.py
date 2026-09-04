"""Unit tests for pi_activity: live Pi session JSONL tracking (Issue #24).

The session JSONL is append-only: `session` / `model_change` /
`thinking_level_change` / `message` records. Only assistant and toolResult
messages are agent activity; user messages carry the full prompt and Issue
body and must never be summarized. The watcher also tracks the startup
milestones (Issue #176): the selected provider/model (`model_change`
record), the first request (first user message) and the first response
(first assistant message).
"""
import json
import os
from pathlib import Path

import pytest

from orbi import pi_activity


def write_records(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


SESSION_RECORD = {
    "type": "session", "id": "sess-1",
    "timestamp": "2026-01-01T00:00:00Z", "cwd": "/work",
}
MODEL_RECORD = {
    "type": "model_change", "id": "m1",
    "timestamp": "2026-01-01T00:00:00Z",
}
USER_RECORD = {
    "type": "message", "id": "u1",
    "timestamp": "2026-01-01T00:00:01Z",
    "message": {
        "role": "user",
        "content": [{"type": "text", "text": "SECRET ISSUE BODY"}],
    },
}
ASSISTANT_TOOL_CALL = {
    "type": "message", "id": "a1",
    "timestamp": "2026-01-01T00:00:02Z",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "let me test"},
            {"type": "toolCall", "id": "t1", "name": "bash",
             "arguments": {"command": "pytest tests/"}},
        ],
    },
}
ASSISTANT_TEXT = {
    "type": "message", "id": "a2",
    "timestamp": "2026-01-01T00:00:04Z",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
    },
}
TOOL_RESULT = {
    "type": "message", "id": "r1",
    "timestamp": "2026-01-01T00:00:03Z",
    "message": {
        "role": "toolResult", "toolCallId": "t1", "toolName": "bash",
        "content": [{"type": "text", "text": "ok"}],
    },
}
TOOL_RESULT_ERROR = {
    "type": "message", "id": "r2",
    "timestamp": "2026-01-01T00:00:05Z",
    "message": {
        "role": "toolResult", "toolCallId": "t2", "toolName": "bash",
        "content": [{"type": "text", "text": "boom"}], "isError": True,
    },
}


def test_latest_session_file_returns_none_when_directory_missing(tmp_path):
    assert pi_activity.latest_session_file(tmp_path / "nope") is None


def test_latest_session_file_returns_none_when_directory_empty(tmp_path):
    assert pi_activity.latest_session_file(tmp_path) is None


def test_latest_session_file_ignores_non_jsonl_files(tmp_path):
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert pi_activity.latest_session_file(tmp_path) is None


def test_latest_session_file_returns_newest_by_mtime(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    old_time = os.stat(old).st_mtime
    os.utime(old, (old_time - 100, old_time - 100))
    assert pi_activity.latest_session_file(tmp_path) == new


def test_read_new_events_returns_empty_when_file_missing(tmp_path):
    records, offset = pi_activity.read_new_events(
        tmp_path / "missing.jsonl", 0,
    )
    assert records == []
    assert offset == 0


def test_read_new_events_keeps_partial_last_line_for_next_read(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "session", "id": "a"}\n{"type": "sess',
                    encoding="utf-8")
    records, offset = pi_activity.read_new_events(path, 0)
    assert records == [{"type": "session", "id": "a"}]
    # The partial line is not consumed: the next read starts where it began.
    records2, offset2 = pi_activity.read_new_events(path, offset)
    assert records2 == []
    assert offset2 == offset


def test_read_new_events_consumes_complete_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    first = '{"type": "session", "id": "a"}\n'
    path.write_text(first, encoding="utf-8")
    records, offset = pi_activity.read_new_events(path, 0)
    assert records == [{"type": "session", "id": "a"}]
    assert offset == len(first.encode("utf-8"))
    # No new data: nothing to read.
    records2, offset2 = pi_activity.read_new_events(path, offset)
    assert records2 == []
    assert offset2 == offset


def test_read_new_events_skips_unparseable_and_non_object_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        'not json\n[1, 2]\n{"type": "session", "id": "a"}\n\n',
        encoding="utf-8",
    )
    records, _ = pi_activity.read_new_events(path, 0)
    assert records == [{"type": "session", "id": "a"}]


def test_read_new_events_rereads_from_start_when_file_shrank(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "session", "id": "a"}\n' * 3, encoding="utf-8")
    _, offset = pi_activity.read_new_events(path, 0)
    path.write_text('{"type": "session", "id": "b"}\n', encoding="utf-8")
    records, new_offset = pi_activity.read_new_events(path, offset)
    assert records == [{"type": "session", "id": "b"}]
    assert new_offset == len('{"type": "session", "id": "b"}\n'.encode("utf-8"))


def test_sanitize_collapses_whitespace_and_truncates_long_text():
    text = "git commit -m 'line one\nline two\ttabbed' " + "x" * 300
    sanitized = pi_activity.sanitize(text)
    assert "\n" not in sanitized and "\t" not in sanitized
    assert len(sanitized) <= pi_activity.MAX_SUMMARY_LENGTH + 3
    assert sanitized.endswith("...")


def test_sanitize_keeps_short_text_unchanged():
    assert pi_activity.sanitize("bash: pytest tests/") == "bash: pytest tests/"


@pytest.mark.parametrize("raw,needle", [
    ("curl -H 'Authorization: Bearer abcDEF123._-'", "Bearer <redacted>"),
    ("gh api -H 'Authorization: token ghp_AbCdEf1234567890xyz'",
     "ghp_<redacted>"),
    ("export OPENAI_KEY=sk-AbCdEf1234567890xyz", "sk-<redacted>"),
])
def test_sanitize_redacts_token_shapes(raw, needle):
    assert needle in pi_activity.sanitize(raw)


def test_tool_args_summary_returns_bash_command():
    assert pi_activity.tool_args_summary(
        "bash", {"command": "pytest tests/"},
    ) == "pytest tests/"


def test_tool_args_summary_returns_first_known_path_key():
    assert pi_activity.tool_args_summary(
        "read", {"path": "/tmp/a.md", "offset": 3},
    ) == "/tmp/a.md"
    assert pi_activity.tool_args_summary(
        "mcp", {"tool": "sentry_tool", "args": {}},
    ) == "sentry_tool"


def test_tool_args_summary_returns_empty_without_known_keys():
    assert pi_activity.tool_args_summary("bash", {}) == ""


def test_phase_for_maps_bash_commands_to_key_phases():
    assert pi_activity.phase_for(
        "bash", {"command": "/usr/bin/python3 -m pytest tests/ -q"},
    ) == "test"
    assert pi_activity.phase_for(
        "bash", {"command": "python3 -m coverage run -m pytest"},
    ) == "test"
    assert pi_activity.phase_for(
        "bash", {"command": "gh pr create --fill"},
    ) == "pr"
    assert pi_activity.phase_for(
        "bash", {"command": "git push origin HEAD"},
    ) == "push"
    assert pi_activity.phase_for(
        "bash", {"command": "git commit -m feat"},
    ) == "commit"
    assert pi_activity.phase_for(
        "bash", {"command": "git fetch origin main"},
    ) == "base"
    assert pi_activity.phase_for(
        "bash", {"command": "git merge origin/main"},
    ) == "base"
    assert pi_activity.phase_for(
        "bash", {"command": "git worktree add .worktrees/w"},
    ) == "worktree"
    assert pi_activity.phase_for(
        "bash", {"command": "npx playwright test"},
    ) == "ui"


def test_phase_for_falls_back_to_bash_for_plain_commands():
    assert pi_activity.phase_for("bash", {"command": "ls -la"}) == "bash"


def test_phase_for_uses_tool_name_for_non_bash_tools():
    assert pi_activity.phase_for("read", {"path": "/a"}) == "read"
    assert pi_activity.phase_for("write", {"path": "/a"}) == "write"


def test_parse_iso_utc_returns_epoch_for_zulu_timestamp():
    assert pi_activity.parse_iso_utc("2026-01-01T00:00:00Z") == (
        pi_activity.datetime(2026, 1, 1, tzinfo=pi_activity.timezone.utc)
        .timestamp()
    )


def test_parse_iso_utc_assumes_utc_for_naive_timestamp():
    assert pi_activity.parse_iso_utc("2026-01-01T00:00:00") == (
        pi_activity.datetime(2026, 1, 1, tzinfo=pi_activity.timezone.utc)
        .timestamp()
    )


def test_parse_iso_utc_returns_none_for_invalid_timestamp():
    assert pi_activity.parse_iso_utc("not a timestamp") is None


def test_watcher_tracks_session_activity_and_phase(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD, MODEL_RECORD, USER_RECORD,
        ASSISTANT_TOOL_CALL, TOOL_RESULT,
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["session_id"] == "sess-1"
    assert state["session_file"] == str(session)
    assert state["events"] == 5
    assert state["phase"] == "test"
    assert state["last_activity"] == "2026-01-01T00:00:03Z"
    # The tool result reports the outcome without hiding the real action.
    assert state["action"] == "bash pytest tests/"
    assert state["result"] == "ok"
    assert state["changed"] is True
    # The user message (full prompt / Issue body) is never summarized.
    assert "SECRET ISSUE BODY" not in (state["action"] or "")
    # Second poll: no new records, not changed.
    again = watcher.poll()
    assert again["changed"] is False
    assert again["events"] == 5


def test_watcher_phase_follows_latest_tool_call(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        ASSISTANT_TOOL_CALL,
        TOOL_RESULT,
        {
            "type": "message", "id": "a3",
            "timestamp": "2026-01-01T00:00:06Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t3", "name": "bash",
                     "arguments": {"command": "gh pr create --fill"}},
                ],
            },
        },
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["phase"] == "pr"
    assert state["action"] == "bash gh pr create --fill"


def test_watcher_tool_result_error_keeps_action_and_sets_result(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {
            "type": "message", "id": "a0",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t0", "name": "bash",
                     "arguments": {"command": "gh pr create --fill"}},
                ],
            },
        },
        TOOL_RESULT_ERROR,
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    # The error is visible via result; the real action is not overwritten.
    assert state["action"] == "bash gh pr create --fill"
    assert state["result"] == "error"


def test_watcher_new_tool_call_clears_stale_result(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {
            "type": "message", "id": "a0",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t0", "name": "bash",
                     "arguments": {"command": "pytest tests/"}},
                ],
            },
        },
        TOOL_RESULT,
        {
            "type": "message", "id": "a1",
            "timestamp": "2026-01-01T00:00:04Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t1", "name": "edit",
                     "arguments": {"path": "progress.py"}},
                ],
            },
        },
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    # The new call is pending: the previous result must not be shown as if
    # it described the new action.
    assert state["action"] == "edit progress.py"
    assert state["result"] is None
    assert state["phase"] == "edit"


def test_watcher_tool_result_without_call_keeps_action_none(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, TOOL_RESULT_ERROR])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["action"] is None
    assert state["result"] == "error"


def test_watcher_keeps_phase_for_non_bash_tool_results(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {
            "type": "message", "id": "a4",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t4", "name": "read",
                     "arguments": {"path": "/a.md"}},
                ],
            },
        },
        {
            "type": "message", "id": "r4",
            "timestamp": "2026-01-01T00:00:03Z",
            "message": {
                "role": "toolResult", "toolCallId": "t4", "toolName": "read",
                "content": [],
            },
        },
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["phase"] == "read"
    assert state["action"] == "read /a.md"
    assert state["result"] == "ok"


def test_watcher_reports_assistant_text_as_activity(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TEXT])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["action"] == "assistant text"
    assert state["result"] is None
    assert state["phase"] == "starting"


def test_watcher_reads_new_records_incrementally(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["events"] == 1
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ASSISTANT_TOOL_CALL) + "\n")
    state = watcher.poll()
    assert state["events"] == 2
    assert state["phase"] == "test"
    assert state["changed"] is True


def test_watcher_starts_following_session_file_when_it_appears(tmp_path):
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["session_file"] is None
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD])
    state = watcher.poll()
    assert state["session_file"] == str(session)
    assert state["session_id"] == "sess-1"


# --------------------------------------------- known_files (Issue #45 F3)

def test_watcher_known_files_ignores_pre_existing_session_and_follows_new(
    tmp_path,
):
    """Regression (Issue #45 round-5 review, Major 3): a resumed Fixer
    starts a NEW JSONL in the existing .pi-session directory. With
    `known_files` (the files that existed before Popen) the watcher must
    NOT bind to the old implementer session; it follows the new file
    created by the current invocation."""
    old = tmp_path / "old.jsonl"
    write_records(old, [
        {"type": "session", "id": "old-session",
         "timestamp": "2026-01-01T00:00:00Z", "cwd": "/w"},
        ASSISTANT_TOOL_CALL,
    ])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files={old},
    )
    # First poll: the pre-existing file is known, so nothing is bound.
    state = watcher.poll()
    assert state["session_file"] is None
    assert state["session_id"] is None
    assert state["events"] == 0
    # The current Pi process creates its new session file.
    new = tmp_path / "new.jsonl"
    write_records(new, [
        {"type": "session", "id": "fixer-session",
         "timestamp": "2026-01-01T01:00:00Z", "cwd": "/w"},
        {
            "type": "message", "id": "a90",
            "timestamp": "2026-01-01T01:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "t90", "name": "bash",
                     "arguments": {"command": "git merge origin/main"}},
                ],
            },
        },
    ])
    state = watcher.poll()
    assert state["session_file"] == str(new)
    assert state["session_id"] == "fixer-session"
    assert state["events"] == 2
    assert state["phase"] == "base"
    assert state["changed"] is True
    # The old session's records never leak into the resumed activity.
    assert "pytest" not in (state["action"] or "")


def test_watcher_known_files_switches_to_newer_file_and_resets_state(
    tmp_path,
):
    """If a newer session file appears while an unknown file is already
    bound, the watcher switches to it and resets its state (offset,
    events, session id, phase, last activity)."""
    first = tmp_path / "first.jsonl"
    write_records(first, [
        {"type": "session", "id": "sess-first",
         "timestamp": "2026-01-01T00:00:00Z", "cwd": "/w"},
        ASSISTANT_TOOL_CALL,
    ])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files=set(),
    )
    state = watcher.poll()
    assert state["session_file"] == str(first)
    assert state["session_id"] == "sess-first"
    assert state["events"] == 2
    # A second Pi invocation (same worktree) creates a newer file.
    second = tmp_path / "second.jsonl"
    write_records(second, [
        {"type": "session", "id": "sess-second",
         "timestamp": "2026-01-01T02:00:00Z", "cwd": "/w"},
    ])
    state = watcher.poll()
    assert state["session_file"] == str(second)
    assert state["session_id"] == "sess-second"
    # The state was reset: the old file's events/phase/action/result are
    # gone... (Issue #176: the new session exists but has no first
    # response yet, so the sub-phase is request_pending)
    assert state["events"] == 1
    assert state["phase"] == "request_pending"
    assert state["action"] is None
    assert state["result"] is None
    assert state["last_activity"] is None
    # ...and the new file is read from the start (its records counted).
    assert state["changed"] is True


def test_watcher_known_files_keeps_binding_to_same_new_file(tmp_path):
    """Once bound to the new file, a later poll keeps following it (no
    re-switch, no re-read) as long as no newer file appears."""
    new = tmp_path / "new.jsonl"
    write_records(new, [SESSION_RECORD])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files=set(),
    )
    assert watcher.poll()["session_file"] == str(new)
    again = watcher.poll()
    assert again["session_file"] == str(new)
    assert again["changed"] is False
    assert again["events"] == 1


def test_watcher_without_known_files_keeps_legacy_bind_once(tmp_path):
    """`known_files=None` (the default, used by `activity_snapshot`) keeps
    the original bind-once semantics: the newest pre-existing file is
    bound immediately and never switched."""
    old = tmp_path / "old.jsonl"
    write_records(old, [SESSION_RECORD])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["session_file"] == str(old)
    assert state["session_id"] == "sess-1"
    new = tmp_path / "new.jsonl"
    write_records(new, [{"type": "session", "id": "sess-new",
                         "timestamp": "2026-01-01T00:00:00Z"}])
    again = watcher.poll()
    assert again["session_file"] == str(old)
    assert again["session_id"] == "sess-1"


def test_watcher_known_files_ignores_newer_known_file(tmp_path):
    """A file that was already present before Popen is never bound, even
    when it is the newest file in the directory."""
    old = tmp_path / "old.jsonl"
    write_records(old, [SESSION_RECORD])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files={old},
    )
    state = watcher.poll()
    assert state["session_file"] is None
    assert state["events"] == 0


def test_watcher_stale_seconds_from_last_activity(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TOOL_CALL])
    last_epoch = pi_activity.parse_iso_utc("2026-01-01T00:00:02Z")
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: last_epoch + 42.0,
    )
    state = watcher.poll()
    assert state["stale_seconds"] == pytest.approx(42.0)


def test_watcher_stale_seconds_from_start_without_activity(tmp_path):
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_050.0,
    )
    watcher.start_time = 1_000_000.0
    state = watcher.poll()
    assert state["stale_seconds"] == pytest.approx(50.0)
    # Issue #176: no session file yet -> the session_pending sub-phase.
    assert state["phase"] == "session_pending"
    assert state["action"] is None
    assert state["result"] is None


def test_watcher_clamps_negative_stale_to_zero(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TOOL_CALL])
    last_epoch = pi_activity.parse_iso_utc("2026-01-01T00:00:02Z")
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: last_epoch - 10.0,
    )
    assert watcher.poll()["stale_seconds"] == 0.0


def test_watcher_state_reports_model_wait_after_tool_result(tmp_path):
    """After a tool result the model is expected to reply: state reports
    `model_wait` so the journal can distinguish a slow active model from a
    stalled agent (Issue #40)."""
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TOOL_CALL, TOOL_RESULT])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["model_wait"] is True


def test_watcher_state_no_model_wait_after_assistant_text(tmp_path):
    # An assistant text is not a pending model request: no model_wait.
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TEXT])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["model_wait"] is False


def test_watcher_state_no_model_wait_after_tool_call(tmp_path):
    # After a tool call the tool is running, not the model: no model_wait.
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, ASSISTANT_TOOL_CALL])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["model_wait"] is False


def test_watcher_state_no_model_wait_without_session(tmp_path):
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["model_wait"] is False


# --------------------------------------- startup milestones (Issue #176)

def test_watcher_state_tracks_selected_provider_and_model(tmp_path):
    """The `model_change` record carries the provider/model Pi ACTUALLY
    selected (written right after the session record): the identifiers are
    non-sensitive and must be visible on the state (Issue #176)."""
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {"type": "model_change", "id": "m1",
         "timestamp": "2026-01-01T00:00:00Z",
         "provider": "local-qwen", "modelId": "qwen3.8:27b"},
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["provider"] == "local-qwen"
    assert state["model"] == "qwen3.8:27b"


def test_watcher_state_model_change_without_fields_stays_unselected(tmp_path):
    # A model_change record without usable provider/modelId fields must
    # not fabricate a selection (the fields stay None, not empty).
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {"type": "model_change", "id": "m1",
         "timestamp": "2026-01-01T00:00:00Z",
         "provider": 7, "modelId": ""},
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["provider"] is None
    assert state["model"] is None


def test_watcher_state_first_request_on_first_user_message(tmp_path):
    """The first user message record is written when the first request to
    the model goes out: `first_request` flips on it, and the prompt
    content itself is NEVER summarized (Issue #176)."""
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    assert watcher.poll()["first_request"] is False
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(USER_RECORD) + "\n")
    state = watcher.poll()
    assert state["first_request"] is True
    # The user message is a milestone, not an activity: no action.
    assert state["action"] is None
    assert "SECRET ISSUE BODY" not in json.dumps(state)


def test_watcher_state_first_response_on_first_assistant_message(tmp_path):
    """The first assistant message record is the first response from the
    model: `first_response` flips on it (Issue #176)."""
    session = tmp_path / "s.jsonl"
    write_records(session, [SESSION_RECORD, USER_RECORD])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["first_request"] is True
    assert state["first_response"] is False
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ASSISTANT_TEXT) + "\n")
    state = watcher.poll()
    assert state["first_response"] is True


def test_watcher_state_milestones_absent_before_any_session(tmp_path):
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["provider"] is None
    assert state["model"] is None
    assert state["first_request"] is False
    assert state["first_response"] is False


def test_watcher_switch_resets_startup_milestones(tmp_path):
    """Switching to a newer session file resets the startup milestones
    with the rest of the tracked state (a resumed invocation's first
    request/response are the NEW session's, Issue #176)."""
    first = tmp_path / "first.jsonl"
    write_records(first, [
        SESSION_RECORD,
        {"type": "model_change", "id": "m1",
         "timestamp": "2026-01-01T00:00:00Z",
         "provider": "p1", "modelId": "m1"},
        USER_RECORD,
        ASSISTANT_TEXT,
    ])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files=set(),
    )
    state = watcher.poll()
    assert state["provider"] == "p1"
    assert state["model"] == "m1"
    assert state["first_request"] is True
    assert state["first_response"] is True
    second = tmp_path / "second.jsonl"
    write_records(second, [{
        "type": "session", "id": "sess-second",
        "timestamp": "2026-01-01T02:00:00Z", "cwd": "/w",
    }])
    state = watcher.poll()
    assert state["session_id"] == "sess-second"
    assert state["provider"] is None
    assert state["model"] is None
    assert state["first_request"] is False
    assert state["first_response"] is False


def test_watcher_switch_reset_clears_model_wait(tmp_path):
    # Switching to a newer session file resets the model_wait flag with the
    # rest of the tracked state.
    first = tmp_path / "first.jsonl"
    write_records(first, [SESSION_RECORD, ASSISTANT_TOOL_CALL, TOOL_RESULT])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0, known_files=set(),
    )
    assert watcher.poll()["model_wait"] is True
    second = tmp_path / "second.jsonl"
    write_records(second, [{
        "type": "session", "id": "sess-second",
        "timestamp": "2026-01-01T02:00:00Z", "cwd": "/w",
    }])
    state = watcher.poll()
    assert state["session_id"] == "sess-second"
    assert state["model_wait"] is False


def test_watcher_ignores_empty_session_id(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        {"type": "session", "id": "", "timestamp": "2026-01-01T00:00:00Z"},
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["session_id"] is None


def test_watcher_ignores_assistant_content_that_is_not_a_list(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {"type": "message", "id": "a8",
         "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "content": "plain string"}},
        {"type": "message", "id": "a9",
         "timestamp": "2026-01-01T00:00:03Z",
         "message": {"role": "assistant"}},
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["action"] is None
    assert state["result"] is None
    assert state["phase"] == "starting"


def test_watcher_ignores_records_without_message_object(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        # A real Pi record type the watcher does not track (Issue #176):
        # it must be ignored, not treated as a message.
        {"type": "thinking_level_change", "id": "t1",
         "timestamp": "2026-01-01T00:00:01Z", "thinkingLevel": "medium"},
        {"type": "message", "id": "x1", "timestamp": "2026-01-01T00:00:02Z"},
        {"type": "message", "id": "x2", "timestamp": "2026-01-01T00:00:03Z",
         "message": "not a dict"},
        {"type": "message", "id": "x3", "timestamp": "2026-01-01T00:00:04Z",
         "message": {"role": "user", "content": "plain string"}},
        # A message with a role the watcher does not track: no milestone.
        {"type": "message", "id": "x4", "timestamp": "2026-01-01T00:00:05Z",
         "message": {"role": "system", "content": "note"}},
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["events"] == 6
    assert state["action"] is None
    assert state["result"] is None
    # Issue #176: the user record (even with malformed content) is the
    # first-request milestone; the session exists, no first response.
    assert state["first_request"] is True
    assert state["first_response"] is False
    assert state["phase"] == "request_pending"


def test_watcher_handles_malformed_tool_call_items(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {
            "type": "message", "id": "a5",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    "not a dict",
                    {"type": "toolCall"},
                    {"type": "toolCall", "name": 7,
                     "arguments": "not a dict"},
                    {"type": "unknown-kind"},
                ],
            },
        },
    ])
    watcher = pi_activity.SessionWatcher(tmp_path, now=lambda: 1_000_000.0)
    state = watcher.poll()
    assert state["action"] == "tool"
    assert state["phase"] == "tool"


def test_watcher_handles_missing_or_invalid_timestamps(tmp_path):
    session = tmp_path / "s.jsonl"
    write_records(session, [
        SESSION_RECORD,
        {"type": "message", "id": "a6", "timestamp": "bad",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "x"}]}},
        {"type": "message", "id": "a7",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "y"}]}},
    ])
    watcher = pi_activity.SessionWatcher(
        tmp_path, now=lambda: 1_000_000.0,
    )
    watcher.start_time = 999_900.0
    state = watcher.poll()
    assert state["last_activity"] is None
    assert state["action"] == "assistant text"
    # No usable activity epoch: stale falls back to the start time.
    assert state["stale_seconds"] == pytest.approx(100.0)


def test_activity_snapshot_returns_none_without_session(tmp_path):
    assert pi_activity.activity_snapshot(tmp_path) is None


def test_activity_snapshot_scans_newest_session_file(tmp_path):
    write_records(tmp_path / "s.jsonl", [SESSION_RECORD, ASSISTANT_TOOL_CALL])
    snapshot = pi_activity.activity_snapshot(tmp_path)
    assert snapshot["session_id"] == "sess-1"
    assert snapshot["phase"] == "test"
    assert snapshot["action"] == "bash pytest tests/"
    assert snapshot["result"] is None
    assert snapshot["changed"] is False


def test_format_duration_uses_seconds_minutes_and_hours():
    assert pi_activity.format_duration(0) == "0s"
    assert pi_activity.format_duration(0.5) == "0.5s"
    assert pi_activity.format_duration(59) == "59s"
    assert pi_activity.format_duration(60) == "1m"
    assert pi_activity.format_duration(840) == "14m"
    assert pi_activity.format_duration(3600) == "1h0m"
    assert pi_activity.format_duration(15300) == "4h15m"


def test_quote_value_quotes_only_values_with_spaces():
    assert pi_activity.quote_value("test") == "test"
    assert pi_activity.quote_value("bash pytest tests/") == (
        '"bash pytest tests/"'
    )


def test_quote_value_escapes_embedded_quotes():
    assert pi_activity.quote_value('git commit -m "feat: x"') == (
        '"git commit -m \\"feat: x\\""'
    )
    assert pi_activity.quote_value('a"b') == '"a\\"b"'


def test_format_run_scene_is_the_full_scene_logged_once():
    snapshot = {
        "session_id": "sess-1",
        "session_file": "/w/.pi-session/sess-1.jsonl",
        "phase": "test",
        "last_activity": "2026-01-01T00:00:02Z",
        "action": "bash pytest tests/",
        "result": "ok",
    }
    scene = pi_activity.format_run_scene(
        snapshot, run_id="e07383c2",
        issue="xqliu/orbi#18", role="implement",
        branch="orbi/xqliu-orbi-issue-18-e07383c2",
        worktree="/w",
    )
    assert scene == (
        "run=e07383c2 issue=xqliu/orbi#18 role=implement "
        "branch=orbi/xqliu-orbi-issue-18-e07383c2 worktree=/w "
        "session=sess-1 session_file=/w/.pi-session/sess-1.jsonl phase=test "
        "last_activity=2026-01-01T00:00:02Z action=\"bash pytest tests/\" "
        "result=ok"
    )


def test_format_run_scene_marks_missing_fields():
    scene = pi_activity.format_run_scene(
        {
            "session_id": None,
            "session_file": None,
            "phase": "starting",
            "last_activity": None,
            "action": None,
            "result": None,
        },
        run_id="run1", issue="owner/repo#1", role="implement",
        branch="b", worktree="/w",
    )
    assert scene == (
        "run=run1 issue=owner/repo#1 role=implement branch=b worktree=/w "
        "session=- session_file=- phase=starting last_activity=- "
        "action=- result=-"
    )


def test_format_end_scene_carries_result_and_pr_entry():
    scene = pi_activity.format_end_scene(
        run_id="e07383c2", issue="xqliu/orbi#18", role="implement",
        result="pr_opened", elapsed=2520,
        pr="https://github.com/xqliu/orbi/pull/40",
        commit="0123456789abcdef0123456789abcdef01234567",
    )
    assert scene == (
        "run=e07383c2 issue=xqliu/orbi#18 role=implement "
        "result=pr_opened elapsed=42m "
        "pr=https://github.com/xqliu/orbi/pull/40 "
        "commit=0123456789abcdef0123456789abcdef01234567"
    )


def test_parse_scene_handles_quoted_values_and_equals_inside_values():
    scene = (
        'run=e07383c2 issue=xqliu/orbi#18 role=implement phase=test '
        'action="git commit -m \'a=b c\'" result=ok idle=6s'
    )
    fields = pi_activity.parse_scene(scene)
    assert fields["run"] == "e07383c2"
    assert fields["issue"] == "xqliu/orbi#18"
    assert fields["action"] == "git commit -m 'a=b c'"
    assert fields["result"] == "ok"
    assert fields["idle"] == "6s"


def test_parse_scene_skips_line_kind_prefix():
    # Journal lines start with the line kind (activity / heartbeat /
    # run_start / run_failed / run_end) before the key=value fields.
    fields = pi_activity.parse_scene(
        'activity run=e07383c2 issue=xqliu/orbi#18 role=implement '
        'phase=test action="bash pytest tests/" result=ok idle=6s'
    )
    assert fields["run"] == "e07383c2"
    assert fields["action"] == "bash pytest tests/"
    assert fields["idle"] == "6s"


def test_parse_scene_marks_missing_values_and_ignores_garbage():
    fields = pi_activity.parse_scene("run=a-1 phase= idle= x=1")
    assert fields["run"] == "a-1"
    assert fields["phase"] is None
    assert fields["idle"] is None
    assert fields["x"] == "1"
    assert pi_activity.parse_scene("no key here at all") == {}


def test_parse_scene_stops_at_trailing_whitespace():
    assert pi_activity.parse_scene("run=a-1 ") == {"run": "a-1"}


def test_parse_scene_keeps_rest_of_line_for_unterminated_quote():
    fields = pi_activity.parse_scene('run=a-1 action="unclosed value')
    assert fields["run"] == "a-1"
    assert fields["action"] == "unclosed value"


def test_parse_scene_unescapes_embedded_quotes():
    fields = pi_activity.parse_scene(
        'action="git commit -m \\"feat: x\\"" phase=commit'
    )
    assert fields["action"] == 'git commit -m "feat: x"'
    assert fields["phase"] == "commit"


def test_parse_scene_round_trips_escaped_quotes():
    value = 'git commit -m "feat: a=b" && push'
    field = f"action={pi_activity.quote_value(value)}"
    assert pi_activity.parse_scene(field)["action"] == value


def test_parse_scene_round_trips_run_scene():
    snapshot = {
        "session_id": "sess-1",
        "session_file": "/w/.pi-session/sess-1.jsonl",
        "phase": "test",
        "last_activity": "2026-01-01T00:00:02Z",
        "action": "bash pytest tests/",
        "result": "ok",
    }
    scene = pi_activity.format_run_scene(
        snapshot, run_id="e07383c2",
        issue="xqliu/orbi#18", role="implement",
        branch="orbi/xqliu-orbi-issue-18-e07383c2",
        worktree="/w",
    )
    fields = pi_activity.parse_scene(scene)
    assert fields["run"] == "e07383c2"
    assert fields["issue"] == "xqliu/orbi#18"
    assert fields["branch"] == (
        "orbi/xqliu-orbi-issue-18-e07383c2"
    )
    assert fields["session_file"] == "/w/.pi-session/sess-1.jsonl"
    assert fields["action"] == "bash pytest tests/"
    assert fields["result"] == "ok"
