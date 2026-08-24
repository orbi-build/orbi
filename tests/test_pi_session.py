import json
import re
from pathlib import Path

import pytest

import pi_session


def _session_line(**overrides):
    data = {
        "type": "session", "version": 3,
        "id": "01a034e9-caab-7921-8228-5b2b584065be",
        "timestamp": "2026-08-24T17:55:32.139Z",
        "cwd": "/tmp/muyan-pilot-xqliu-muyan-pilot-issue-24",
    }
    data.update(overrides)
    return json.dumps(data)


def _message_line(role, content, timestamp="2026-08-24T17:55:40.223Z", **extra):
    data = {
        "type": "message", "id": "15bea09c", "parentId": "2f5bc939",
        "timestamp": timestamp,
        "message": {"role": role, "content": content, **extra},
    }
    return json.dumps(data)


def _write_session(tmp_path, *lines):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def test_parse_session_events_reads_session_header():
    path = _write_session(Path("/tmp"), _session_line())
    events = pi_session.parse_session_events(path)
    assert events == [{
        "kind": "session_start",
        "phase": "session_start",
        "timestamp": "2026-08-24T17:55:32.139Z",
        "session_id": "01a034e9-caab-7921-8228-5b2b584065be",
        "summary": "session started",
    }]


def test_parse_session_events_reads_model_and_thinking_changes():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        json.dumps({"type": "model_change", "id": "a", "timestamp": "t1",
                    "provider": "local-qwen", "modelId": "qwen3.8:27b"}),
        json.dumps({"type": "thinking_level_change", "id": "b", "timestamp": "t2",
                    "thinkingLevel": "high"}),
    )
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == [
        "session_start", "model_change", "thinking_level_change",
    ]
    assert events[1]["summary"] == "model local-qwen/qwen3.8:27b"
    assert events[2]["summary"] == "thinking level high"


def test_parse_session_events_summarizes_assistant_tool_calls():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "toolCall", "id": "c1", "name": "bash",
             "arguments": {"command": "pytest tests/ -q"}},
            {"type": "toolCall", "id": "c2", "name": "read",
             "arguments": {"path": "/tmp/repo/README.md"}},
        ]),
    )
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == [
        "session_start", "assistant", "assistant",
    ]
    assert events[1]["phase"] == "test"
    assert events[1]["summary"] == "pytest tests/ -q"
    assert events[2]["phase"] == "read"
    assert events[2]["summary"] == "/tmp/repo/README.md"
    assert "secret reasoning" not in json.dumps(events)


def test_parse_session_events_summarizes_assistant_text_as_reply():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Now I will open the pull request."},
        ]),
    )
    events = pi_session.parse_session_events(path)
    assert events[1] == {
        "kind": "assistant", "phase": "reply",
        "timestamp": "2026-08-24T17:55:40.223Z",
        "summary": "Now I will open the pull request.",
    }


def test_parse_session_events_ignores_non_string_or_blank_text():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [
            {"type": "text", "text": 123},
            {"type": "text", "text": "   "},
        ]),
    )
    events = pi_session.parse_session_events(path)
    assert events[1]["phase"] == "thinking"


def test_parse_session_events_marks_thinking_only_assistant_message():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [{"type": "thinking", "thinking": "hmm"}]),
    )
    events = pi_session.parse_session_events(path)
    assert events[1] == {
        "kind": "assistant", "phase": "thinking",
        "timestamp": "2026-08-24T17:55:40.223Z",
        "summary": "thinking",
    }


def test_parse_session_events_summarizes_tool_results():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("toolResult", [
            {"type": "text", "text": "full output should not be logged"},
        ], toolCallId="c1", toolName="bash", isError=False),
    )
    events = pi_session.parse_session_events(path)
    assert events[1] == {
        "kind": "toolResult", "phase": "tool_result",
        "timestamp": "2026-08-24T17:55:40.223Z",
        "summary": "bash ok",
    }
    assert "full output" not in json.dumps(events)


def test_parse_session_events_marks_failed_tool_result():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("toolResult", [], toolCallId="c1", toolName="bash",
                      isError=True),
    )
    events = pi_session.parse_session_events(path)
    assert events[1]["phase"] == "tool_result"
    assert events[1]["summary"] == "bash failed"


def test_parse_session_events_skips_non_dict_content_parts_and_bad_tool_names():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [
            "not-a-dict",
            {"type": "toolCall", "id": "c1"},
            {"type": "toolCall", "id": "c2", "name": 5,
             "arguments": {"command": "pytest"}},
        ]),
    )
    events = pi_session.parse_session_events(path)
    # No valid tool call or text -> falls back to a single thinking event.
    assert events[1] == {
        "kind": "assistant", "phase": "thinking",
        "timestamp": "2026-08-24T17:55:40.223Z", "summary": "thinking",
    }


def test_parse_session_events_tool_result_without_name_uses_tool():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("toolResult", [], toolCallId="c1", isError=False),
    )
    events = pi_session.parse_session_events(path)
    assert events[1]["summary"] == "tool ok"


def test_parse_session_events_session_without_id_has_no_session_id_field():
    path = _write_session(
        Path("/tmp"),
        json.dumps({"type": "session", "timestamp": "2026-08-24T17:55:32.139Z"}),
    )
    events = pi_session.parse_session_events(path)
    assert "session_id" not in events[0]


def test_parse_session_events_skips_session_and_message_without_timestamp():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        json.dumps({"type": "session", "id": "no-ts"}),
        json.dumps({"type": "message", "id": "m1"}),
        json.dumps({"type": "message", "id": "m2",
                    "message": {"role": "assistant", "content": []}}),
    )
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == ["session_start"]


def test_parse_session_events_skips_user_messages_and_unknown_types():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("user", [{"type": "text", "text": "issue body secret"}]),
        json.dumps({"type": "compaction", "timestamp": "t9"}),
    )
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == ["session_start"]
    assert "issue body secret" not in json.dumps(events)


def test_parse_session_events_skips_malformed_and_partial_lines():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        '{"type": "message", "message": {"role": "assistant", "cont',
        "not json at all",
        "",
        _message_line("assistant", [{"type": "text", "text": "ok"}]),
    )
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == ["session_start", "assistant"]


def test_parse_session_events_skips_non_object_lines():
    path = _write_session(Path("/tmp"), "42", "null", _session_line())
    events = pi_session.parse_session_events(path)
    assert [event["kind"] for event in events] == ["session_start"]


def test_parse_session_events_returns_empty_for_missing_file():
    assert pi_session.parse_session_events(Path("/tmp/does-not-exist.jsonl")) == []


def test_parse_session_events_appends_session_end_when_file_closed():
    path = _write_session(
        Path("/tmp"),
        _session_line(),
        _message_line("assistant", [{"type": "text", "text": "done"}]),
        json.dumps({"type": "session_end", "timestamp": "2026-08-24T18:00:00.000Z"}),
    )
    events = pi_session.parse_session_events(path)
    assert events[-1] == {
        "kind": "session_end", "phase": "session_end",
        "timestamp": "2026-08-24T18:00:00.000Z", "summary": "session ended",
    }


def test_parse_session_events_ignores_session_end_without_timestamp():
    path = _write_session(Path("/tmp"), _session_line(),
                          json.dumps({"type": "session_end"}))
    assert [event["kind"] for event in pi_session.parse_session_events(path)] == [
        "session_start",
    ]


def test_classify_phase_maps_bash_commands_to_key_phases():
    cases = {
        "pytest tests/ -q": "test",
        "python3 -m pytest tests/": "test",
        "coverage run -m pytest": "test",
        "python3 -m coverage report": "test",
        "npm test": "test",
        "python3 muyan_pilot.py status": "verify",
        "git commit -m 'feat: x'": "commit",
        "git push origin branch": "push",
        "gh pr create --title x": "pr",
        "gh pr list --state open": "pr",
        "gh issue comment 3 --body x": "issue_comment",
        "git worktree add -b x /tmp/wt HEAD": "worktree",
        "git branch --show-current": "branch",
        "pip install pytest": "setup",
        "npx playwright test": "ui_test",
        "ls -la": "command",
    }
    for command, phase in cases.items():
        assert pi_session.classify_phase("bash", {"command": command}) == phase, command


def test_classify_phase_maps_tool_names():
    assert pi_session.classify_phase("read", {}) == "read"
    assert pi_session.classify_phase("write", {"path": "a"}) == "edit"
    assert pi_session.classify_phase("edit", {"path": "a"}) == "edit"
    assert pi_session.classify_phase("grep", {"pattern": "x"}) == "search"
    assert pi_session.classify_phase("glob", {"pattern": "x"}) == "search"
    assert pi_session.classify_phase("search", {"query": "x"}) == "search"
    assert pi_session.classify_phase("playwright", {}) == "ui_test"
    assert pi_session.classify_phase("mcp", {}) == "mcp"
    assert pi_session.classify_phase("unknown_tool", {}) == "unknown_tool"


def test_classify_phase_ignores_non_dict_arguments():
    assert pi_session.classify_phase("bash", None) == "command"
    assert pi_session.classify_phase("bash", "pytest") == "command"


def test_classify_phase_ignores_non_string_bash_command():
    assert pi_session.classify_phase("bash", {"command": 5}) == "command"
    assert pi_session.classify_phase("bash", {}) == "command"


def test_summarize_tool_uses_first_line_of_bash_command():
    summary = pi_session.summarize_tool(
        "bash", {"command": "cd /tmp/repo && pytest\n\n# comment"},
    )
    assert summary == "cd /tmp/repo && pytest"


def test_summarize_tool_truncates_long_summaries():
    command = "echo " + "a" * 500
    summary = pi_session.summarize_tool("bash", {"command": command})
    assert len(summary) <= pi_session.SUMMARY_LIMIT
    assert summary.endswith("…")


def test_summarize_tool_uses_path_or_pattern_for_other_tools():
    assert pi_session.summarize_tool("read", {"path": "/tmp/a.md"}) == "/tmp/a.md"
    assert pi_session.summarize_tool("write", {"file": "/tmp/a.md"}) == "/tmp/a.md"
    assert pi_session.summarize_tool("grep", {"pattern": "def main"}) == "def main"
    assert pi_session.summarize_tool("glob", {"glob": "**/*.py"}) == "**/*.py"
    assert pi_session.summarize_tool("search", {"query": "status"}) == "status"


def test_summarize_tool_falls_back_to_redacted_arguments_json():
    summary = pi_session.summarize_tool("mcp", {"key": "value"})
    assert summary == '{"key": "value"}'
    summary = pi_session.summarize_tool("mcp", None)
    assert summary == "mcp"


def test_summarize_tool_redacts_credentials():
    summary = pi_session.summarize_tool(
        "bash", {"command": "curl -H 'Authorization: Bearer gho_abcdefghijklmnop' x"},
    )
    assert "gho_abcdefghijklmnop" not in summary
    assert "<redacted>" in summary


def test_redact_replaces_github_tokens():
    text = "token ghp_abcdefghijklmnopqrstuvwxyz123456 end"
    assert pi_session.redact(text) == "token <redacted> end"


def test_redact_replaces_bearer_headers():
    text = "Authorization: Bearer abc12345.def-ghi"
    assert pi_session.redact(text) == "Authorization: Bearer <redacted>"


def test_redact_replaces_token_flags_and_api_keys():
    assert pi_session.redact("gh api --token abc123456789") == "gh api --token <redacted>"
    assert pi_session.redact("export API_KEY=sk-1234567890") == "export API_KEY=<redacted>"
    assert pi_session.redact("api_key = 'sk-1234567890'") == "api_key = '<redacted>'"


def test_redact_leaves_normal_text_unchanged():
    text = "pytest tests/ -q passed 65 tests"
    assert pi_session.redact(text) == text


def test_latest_activity_returns_newest_event():
    events = [
        {"kind": "session_start", "phase": "session_start",
         "timestamp": "2026-08-24T17:55:32.139Z", "summary": "session started"},
        {"kind": "assistant", "phase": "test",
         "timestamp": "2026-08-24T17:56:01.728Z", "summary": "pytest tests/ -q"},
    ]
    assert pi_session.latest_activity(events) == (
        "test", "pytest tests/ -q", "2026-08-24T17:56:01.728Z",
    )


def test_latest_activity_returns_none_for_empty_events():
    assert pi_session.latest_activity([]) is None


def test_session_file_for_picks_newest_file_created_after_start(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    old = session_dir / "old.jsonl"
    old.write_text("{}", encoding="utf-8")
    new = session_dir / "new.jsonl"
    new.write_text("{}", encoding="utf-8")
    import os
    old_time = 1000.0
    os.utime(old, (old_time, old_time))
    assert pi_session.session_file_for(tmp_path, 2000.0) == new


def test_session_file_for_returns_none_when_no_file_or_dir_missing(tmp_path):
    assert pi_session.session_file_for(tmp_path, 0.0) is None
    assert pi_session.session_file_for(tmp_path / "missing", 0.0) is None


def test_session_file_for_returns_none_when_no_file_created_after_start(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    old = session_dir / "old.jsonl"
    old.write_text("{}", encoding="utf-8")
    import os
    os.utime(old, (1000.0, 1000.0))
    assert pi_session.session_file_for(tmp_path, 2000.0) is None


def test_newest_session_file_returns_none_for_empty_session_dir(tmp_path):
    (tmp_path / ".pi-session").mkdir()
    assert pi_session.newest_session_file(tmp_path) is None


def test_newest_session_file_picks_most_recent_file(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    first = session_dir / "first.jsonl"
    second = session_dir / "second.jsonl"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    import os
    os.utime(first, (1000.0, 1000.0))
    assert pi_session.newest_session_file(tmp_path) == second


def test_summarize_session_file_returns_session_id_and_latest_activity(tmp_path):
    path = _write_session(
        tmp_path,
        _session_line(),
        _message_line("assistant", [
            {"type": "toolCall", "id": "c1", "name": "bash",
             "arguments": {"command": "pytest tests/ -q"}},
        ]),
    )
    assert pi_session.summarize_session_file(path) == (
        "01a034e9-caab-7921-8228-5b2b584065be",
        ("test", "pytest tests/ -q", "2026-08-24T17:55:40.223Z"),
    )


def test_summarize_session_file_handles_header_only_session(tmp_path):
    path = _write_session(tmp_path, _session_line())
    assert pi_session.summarize_session_file(path) == (
        "01a034e9-caab-7921-8228-5b2b584065be",
        ("session_start", "session started", "2026-08-24T17:55:32.139Z"),
    )


def test_summarize_session_file_handles_unparseable_file(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    assert pi_session.summarize_session_file(path) == (None, None)


def test_summarize_session_file_finds_session_line_after_other_lines(tmp_path):
    path = _write_session(
        tmp_path,
        json.dumps({"type": "message", "id": "m0"}),
        _session_line(),
    )
    assert pi_session.summarize_session_file(path)[0] == "01a034e9-caab-7921-8228-5b2b584065be"


def test_summarize_session_file_ignores_non_string_session_id(tmp_path):
    path = _write_session(
        tmp_path,
        json.dumps({"type": "session", "id": 5, "timestamp": "t"}),
    )
    assert pi_session.summarize_session_file(path) == (
        None, ("session_start", "session started", "t"),
    )
