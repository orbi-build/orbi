import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import bootstrap_runner as runner
import muyan_pilot


def _write_prompts(tmp_path):
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")


def test_issue_number_extracts_number_from_github_url():
    assert muyan_pilot.issue_number(
        "https://github.com/xqliu/muyan-pilot/issues/3"
    ) == 3


def test_issue_number_rejects_url_without_issue_number():
    with pytest.raises(ValueError, match="no issue number"):
        muyan_pilot.issue_number("https://github.com/xqliu/muyan-pilot")


def test_create_issue_runs_gh_issue_create_and_returns_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append(command)
        or "https://github.com/xqliu/muyan-pilot/issues/13",
    )
    url = muyan_pilot.create_issue(
        "xqliu/muyan-pilot", "New task", "Do it",
    )
    assert url == "https://github.com/xqliu/muyan-pilot/issues/13"
    assert calls == [[
        "gh", "issue", "create", "--repo", "xqliu/muyan-pilot",
        "--title", "New task", "--body", "Do it",
    ]]


def test_dispatch_issue_creates_issue_and_adds_ai_ready(monkeypatch):
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append(command)
        or "https://github.com/xqliu/muyan-pilot/issues/13",
    )
    url = muyan_pilot.dispatch_issue("xqliu/muyan-pilot", "New task", "Do it")
    assert url == "https://github.com/xqliu/muyan-pilot/issues/13"
    assert calls == [
        [
            "gh", "issue", "create", "--repo", "xqliu/muyan-pilot",
            "--title", "New task", "--body", "Do it",
        ],
        [
            "gh", "issue", "edit", "13", "--repo", "xqliu/muyan-pilot",
            "--add-label", "ai-ready",
        ],
    ]


def test_dispatch_issue_propagates_create_failure(monkeypatch):
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(
            runner.subprocess.CalledProcessError(1, command, stderr="nope"),
        ),
    )
    with pytest.raises(runner.subprocess.CalledProcessError):
        muyan_pilot.dispatch_issue("xqliu/muyan-pilot", "t", "b")


def test_list_labeled_issues_queries_github_with_label_and_state(monkeypatch):
    issue = {"number": 3, "title": "task", "url": "u"}
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append(command)
        or json.dumps([issue]),
    )
    issues = muyan_pilot.list_labeled_issues(
        "xqliu/muyan-pilot", "ai-in-progress", state="open",
    )
    assert issues == [issue]
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/muyan-pilot",
        "--state", "open", "--search", "label:ai-in-progress",
        "--json", "number,title,url,state", "--limit", "1",
    ]]


def test_list_labeled_issues_returns_empty_list_when_idle(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "run_command", lambda command, **kwargs: "[]")
    assert muyan_pilot.list_labeled_issues("xqliu/muyan-pilot", "ai-ready") == []


def test_current_issue_returns_first_in_progress_issue(monkeypatch):
    issue = {"number": 3, "title": "task", "url": "u"}
    monkeypatch.setattr(
        muyan_pilot, "list_labeled_issues",
        lambda repo, label, state="open": [issue] if label == "ai-in-progress" else [],
    )
    assert muyan_pilot.current_issue("xqliu/muyan-pilot") == issue


def test_current_issue_returns_none_when_idle(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", lambda *a, **k: [])
    assert muyan_pilot.current_issue("xqliu/muyan-pilot") is None


def test_ready_issue_returns_first_ready_issue(monkeypatch):
    issue = {"number": 11, "title": "task", "url": "u"}
    calls = []

    def fake_list(repo, label, state="open", search=None):
        calls.append((label, search))
        return [issue] if label == "ai-ready" else []

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.ready_issue("xqliu/muyan-pilot") == issue
    assert calls == [("ai-ready", "label:ai-ready -label:ai-in-progress")]


def test_ready_issue_excludes_in_progress_issues(monkeypatch):
    ready = {"number": 12, "title": "next", "url": "u12"}
    calls = []

    def fake_list(repo, label, state="open", search=None):
        calls.append(search)
        return [ready] if search == "label:ai-ready -label:ai-in-progress" else []

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.ready_issue("xqliu/muyan-pilot") == ready
    assert calls == ["label:ai-ready -label:ai-in-progress"]


def test_ready_issue_returns_none_when_idle(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", lambda *a, **k: [])
    assert muyan_pilot.ready_issue("xqliu/muyan-pilot") is None


def test_recent_result_returns_newest_pr_opened_or_blocked_issue(monkeypatch):
    pr_opened = {"number": 2, "title": "done", "url": "u2", "state": "CLOSED"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}
    calls = []

    def fake_list(repo, label, state="open"):
        calls.append((label, state))
        if label == "ai-pr-opened":
            return [pr_opened]
        if label in ("ai-fix-needed", "ai-merged"):
            return []
        return [blocked]

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == blocked
    assert calls == [
        ("ai-pr-opened", "all"), ("ai-fix-needed", "all"),
        ("ai-merged", "all"), ("ai-blocked", "all"),
    ]


def test_recent_result_includes_fix_needed_issue(monkeypatch):
    """`ai-fix-needed` is a result state too: a delivery waiting for the
    Fixer shows up in the status report (Issue #45 round-5 review,
    Major 1)."""
    fix_needed = {"number": 7, "title": "fixing", "url": "u7", "state": "OPEN"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}

    def fake_list(repo, label, state="open"):
        if label == "ai-fix-needed":
            return [fix_needed]
        if label == "ai-blocked":
            return [blocked]
        return []

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == fix_needed


def test_recent_result_includes_merged_issue(monkeypatch):
    """`ai-merged` is the success terminal state: a delivery the Runner
    merged itself shows up in the status result (Issue #34 round-1
    review, Major 3)."""
    merged = {"number": 8, "title": "shipped", "url": "u8", "state": "CLOSED"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}

    def fake_list(repo, label, state="open"):
        if label == "ai-merged":
            return [merged]
        if label == "ai-blocked":
            return [blocked]
        return []

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == merged


def test_recent_result_prefers_pr_opened_when_newer(monkeypatch):
    pr_opened = {"number": 9, "title": "done", "url": "u9", "state": "OPEN"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}

    def fake_list(repo, label, state="open"):
        if label == "ai-pr-opened":
            return [pr_opened]
        if label in ("ai-fix-needed", "ai-merged"):
            return []
        return [blocked]

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == pr_opened


def test_recent_result_returns_none_when_no_result(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", lambda *a, **k: [])
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") is None


def test_format_issue_shows_number_title_and_url():
    issue = {"number": 3, "title": "task", "url": "https://x/3"}
    assert muyan_pilot.format_issue(issue) == "#3 task https://x/3"


def test_status_report_lists_sources_current_ready_and_result(monkeypatch):
    current = {"number": 3, "title": "now", "url": "u3"}
    ready = {"number": 11, "title": "next", "url": "u11"}
    result = {"number": 2, "title": "done", "url": "u2"}

    def fake_lookup(repo, name):
        assert repo == "xqliu/muyan-pilot"
        return {"current": current, "ready": ready, "result": result}[name]

    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: fake_lookup(repo, "current"))
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: fake_lookup(repo, "ready"))
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: fake_lookup(repo, "result"))
    monkeypatch.setattr(muyan_pilot, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    report = muyan_pilot.status_report({
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": Path("/srv/muyan/muyan-pilot"),
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": Path("/srv/muyan/muyan-pilot/.muyan-pilot/slots"),
    })
    assert "source: xqliu/muyan-pilot" in report
    assert "base: main abc123def456" in report
    assert "current: #3 now u3" in report
    assert "ready: #11 next u11" in report
    assert "result: #2 done u2" in report


def test_status_report_freezes_base_from_configured_repo_dir(monkeypatch):
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "freeze_base",
        lambda repo_dir, base_branch: calls.append((repo_dir, base_branch)) or "abc123def456",
    )
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    muyan_pilot.status_report({
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": Path("/srv/muyan/muyan-pilot"),
        "base_branch": "develop",
        "max_concurrency": 1,
        "slot_dir": Path("/srv/muyan/muyan-pilot/.muyan-pilot/slots"),
    })
    assert calls == [(Path("/srv/muyan/muyan-pilot"), "develop")]


def test_status_report_marks_empty_lookups(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    report = muyan_pilot.status_report({
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": Path("/srv/muyan/muyan-pilot"),
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": Path("/srv/muyan/muyan-pilot/.muyan-pilot/slots"),
    })
    assert "base: main abc123def456" in report
    assert "current: -" in report
    assert "ready: -" in report
    assert "result: -" in report


def test_main_add_dispatches_to_selected_source_repo(monkeypatch, tmp_path, capsys):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text(
        "source_repos = [\"xqliu/muyan-pilot\", \"xqliu/muyan-ceo\"]\n",
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "dispatch_issue",
        lambda repo, title, body: calls.append((repo, title, body))
        or "https://github.com/xqliu/muyan-pilot/issues/13",
    )
    assert muyan_pilot.main([
        "add", "New task", "--body", "Do it", "--config", str(config),
    ]) == 0
    assert calls == [("xqliu/muyan-pilot", "New task", "Do it")]
    out = capsys.readouterr().out
    assert "created: https://github.com/xqliu/muyan-pilot/issues/13" in out
    assert "label: ai-ready" in out


def test_main_add_uses_explicit_repo_override(monkeypatch, tmp_path, capsys):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text(
        "source_repos = [\"xqliu/muyan-pilot\", \"xqliu/muyan-ceo\"]\n",
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "dispatch_issue",
        lambda repo, title, body: calls.append(repo) or "https://github.com/x/y/issues/1",
    )
    assert muyan_pilot.main([
        "add", "T", "--repo", "xqliu/muyan-ceo", "--config", str(config),
    ]) == 0
    assert calls == ["xqliu/muyan-ceo"]


def test_main_add_rejects_repo_not_in_config(monkeypatch, tmp_path):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n", encoding="utf-8")
    _write_prompts(tmp_path)
    with pytest.raises(SystemExit):
        muyan_pilot.main([
            "add", "T", "--repo", "other/repo", "--config", str(config),
        ])


def test_main_status_prints_report(monkeypatch, tmp_path, capsys):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n", encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        muyan_pilot, "status_report",
        lambda config: "source: xqliu/muyan-pilot\ncurrent: -",
    )
    assert muyan_pilot.main(["status", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "source: xqliu/muyan-pilot" in out
    assert "current: -" in out


def test_main_rejects_unknown_command(tmp_path):
    with pytest.raises(SystemExit):
        muyan_pilot.main(["deploy", "--config", str(tmp_path / "c.toml")])


# --- session (Issue #74) ------------------------------------------------------


def _session_config(tmp_path):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text(
        "source_repos = [\"xqliu/muyan-pilot\"]\nrepo_dir = \".\"\n",
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    return config


def _write_session(tmp_path, name="sess.jsonl", records=None, run="run1"):
    session_dir = tmp_path / ".worktrees" / \
        f"muyan-pilot-xqliu-muyan-pilot-issue-3-{run}" / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in (records or [])),
        encoding="utf-8",
    )
    return path


def test_find_session_file_returns_newest_jsonl_under_worktrees(tmp_path):
    old = _write_session(tmp_path, name="old.jsonl", run="run1")
    new = _write_session(tmp_path, name="new.jsonl", run="run2")
    old_time = old.stat().st_mtime
    os.utime(old, (old_time - 100, old_time - 100))
    assert muyan_pilot.find_session_file(tmp_path) == new


def test_find_session_file_ignores_non_jsonl_and_missing_dir(tmp_path):
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "wt" / ".pi-session").mkdir(parents=True)
    (tmp_path / ".worktrees" / "wt" / ".pi-session" / "notes.txt").write_text(
        "nope\n", encoding="utf-8",
    )
    assert muyan_pilot.find_session_file(tmp_path) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert muyan_pilot.find_session_file(empty) is None


def test_session_prints_path_and_returns_zero(tmp_path, capsys):
    path = _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    assert muyan_pilot.main([
        "session", "--config", str(_session_config(tmp_path)),
    ]) == 0
    assert capsys.readouterr().out.strip() == str(path)


def test_session_without_session_file_fails_fast_nonzero(tmp_path, capsys):
    (tmp_path / ".worktrees").mkdir()
    assert muyan_pilot.main([
        "session", "--config", str(_session_config(tmp_path)),
    ]) == 1
    err = capsys.readouterr().err
    assert "no pi session" in err
    assert "no Pi is running" in err


def test_follow_session_file_yields_new_lines(tmp_path):
    """Issue #74: the follow generator yields the existing lines and
    then the new lines as they are appended (tail -f semantics). It
    follows the ONE file it was given: a newer file appearing in the
    same directory is never picked up mid-run."""
    path = _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    # A newer file appears while following: it must be ignored.
    def appear_later():
        time.sleep(0.3)
        _write_session(tmp_path, name="later.jsonl", run="run9")
        time.sleep(0.3)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "message", "id": "a1",
                "timestamp": "2026-08-26T00:00:00Z",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "hello"}]},
            }) + "\n")
        time.sleep(0.3)
        path.unlink()

    thread = threading.Thread(target=appear_later)
    thread.start()
    lines = list(muyan_pilot.follow_session_file(path, poll_interval=0.05))
    thread.join(timeout=5)
    assert len(lines) == 2
    assert "sess-1" in lines[0]
    assert "hello" in lines[1]


def test_follow_session_file_stops_when_file_is_gone(tmp_path):
    """Issue #74: the file disappearing (worktree cleanup) stops the
    follow — fail fast, no fallback to another file. A file that is
    gone before the first poll yields nothing."""
    path = _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    path.unlink()
    assert list(
        muyan_pilot.follow_session_file(path, poll_interval=0.05),
    ) == []


def test_session_follow_prints_new_lines_of_the_selected_file(
    tmp_path, capsys, monkeypatch,
):
    """Issue #74: `main session --follow` tails the file selected at
    start and prints each new line as it arrives (raw JSONL by
    default)."""
    _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    monkeypatch.setattr(
        muyan_pilot, "follow_session_file",
        lambda path, poll_interval=0.5: iter([
            json.dumps({"type": "session", "id": "sess-1", "cwd": "/w"}),
            json.dumps({"type": "message", "id": "a1",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "message": {"role": "assistant", "content": [
                            {"type": "text", "text": "hello"}]}}),
        ]),
    )
    assert muyan_pilot.main([
        "session", "--follow", "--config",
        str(_session_config(tmp_path)),
    ]) == 0
    out = capsys.readouterr().out
    assert "sess-1" in out
    assert "hello" in out


def test_session_follow_pretty_prints_summaries(tmp_path, capsys, monkeypatch):
    _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    monkeypatch.setattr(
        muyan_pilot, "follow_session_file",
        lambda path, poll_interval=0.5: iter([
            json.dumps({"type": "message", "id": "a1",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "message": {"role": "assistant", "content": [
                            {"type": "text", "text": "hello"}]}}),
        ]),
    )
    assert muyan_pilot.main([
        "session", "--follow", "--pretty", "--config",
        str(_session_config(tmp_path)),
    ]) == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "hello" in out
    assert "sess.jsonl" not in out


def test_follow_session_file_rereads_shrunk_file(tmp_path):
    """Issue #74: a file that shrank below the read offset (truncated
    and rewritten) is re-read from the start — the same rule as the
    session watcher."""
    path = _write_session(tmp_path, records=[
        {"type": "session", "id": "s1", "cwd": "/w"},
        {"type": "message", "id": "a1",
         "timestamp": "2026-08-26T00:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "one"}]}},
    ])
    generator = muyan_pilot.follow_session_file(path, poll_interval=0.05)
    first = next(generator)
    second = next(generator)
    assert "s1" in first
    assert "one" in second
    # Truncate below the offset and rewrite: re-read from the start.
    path.write_text(
        json.dumps({"type": "session", "id": "s2", "cwd": "/w"}) + "\n",
        encoding="utf-8",
    )
    third = next(generator)
    assert "s2" in third
    path.unlink()


def test_follow_session_file_skips_blank_lines(tmp_path):
    """Issue #74: blank lines between records (a writer flush leaving
    an empty line) are skipped, not yielded."""
    path = tmp_path / "blank.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "s1", "cwd": "/w"})
        + "\n\n"
        + json.dumps({"type": "session", "id": "s2", "cwd": "/w"})
        + "\n",
        encoding="utf-8",
    )
    generator = muyan_pilot.follow_session_file(path, poll_interval=0.05)
    first = next(generator)
    second = next(generator)
    assert "s1" in first
    assert "s2" in second
    path.unlink()


def test_follow_session_file_never_splits_a_record_being_written(
    tmp_path,
):
    """Issue #74: a record the writer is still flushing (no trailing
    newline yet) must not be yielded as fragments: only complete lines
    are yielded, the partial tail is left for the next read (the same
    rule as the session watcher). Without this a record written while
    following appears as split lines in raw mode and is silently
    dropped in `--pretty` mode (every fragment fails to parse)."""
    path = tmp_path / "partial.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": "s1", "cwd": "/w"}) + "\n",
        encoding="utf-8",
    )
    record = json.dumps({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "VISIBLE-RECORD"}]},
    })

    def write_in_chunks():
        time.sleep(0.2)
        with path.open("a", encoding="utf-8") as handle:
            for i in range(0, len(record), 8):
                handle.write(record[i:i + 8])
                handle.flush()
                time.sleep(0.05)
            handle.write("\n")
        time.sleep(0.5)
        path.unlink()

    thread = threading.Thread(target=write_in_chunks)
    thread.start()
    lines = list(
        muyan_pilot.follow_session_file(path, poll_interval=0.05),
    )
    thread.join(timeout=5)
    # The record is yielded exactly once, complete, never as fragments.
    assert lines.count(record) == 1
    assert len(lines) == 2
    assert "s1" in lines[0]
    # Every yielded line is a complete record (the pretty path keeps it).
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)


def test_session_follow_without_session_file_fails_fast(tmp_path, capsys):
    (tmp_path / ".worktrees").mkdir()
    assert muyan_pilot.main([
        "session", "--follow", "--config",
        str(_session_config(tmp_path)),
    ]) == 1
    assert "no pi session" in capsys.readouterr().err


def test_format_session_line_summarizes_message_roles(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "bash",
             "arguments": {"command": "pytest tests/"}},
        ]},
    })
    assert "2026-08-26T00:00:00Z" in line
    assert "assistant" in line
    assert "bash" in line
    assert "pytest tests/" in line


def test_format_session_line_summarizes_tool_result(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "r1",
        "timestamp": "2026-08-26T00:01:00Z",
        "message": {"role": "toolResult", "toolName": "bash",
                    "isError": True,
                    "content": [{"type": "text", "text": "boom"}]},
    })
    assert "toolResult" in line
    assert "bash" in line
    assert "error" in line.lower()


def test_format_session_line_summarizes_session_record(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "session", "id": "sess-1",
        "timestamp": "2026-08-26T00:00:00Z", "cwd": "/w",
    })
    assert "session" in line
    assert "sess-1" in line


def test_format_session_line_truncates_long_content(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "x" * 500},
        ]},
    })
    assert len(line) < 300


def test_format_session_line_tool_call_without_command_falls_back(
    tmp_path,
):
    """Issue #74: a tool call without a `command` argument shows the
    first path-like argument instead."""
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "read",
             "arguments": {"path": "/tmp/x.py"}},
        ]},
    })
    assert "read" in line
    assert "/tmp/x.py" in line


def test_format_session_line_skips_non_dict_content_items(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            "garbage",
            {"type": "unknown"},
            {"type": "text"},
            {"type": "text", "text": "ok"},
        ]},
    })
    assert "ok" in line
    assert "garbage" not in line


def test_format_session_line_summarizes_thinking(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
        ]},
    })
    assert "thinking" in line
    assert "hmm" in line


def test_format_session_line_handles_non_list_content(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": "no list"},
    })
    assert line.endswith("assistant")


def test_format_session_line_handles_unknown_record_kind(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "compaction",
        "timestamp": "2026-08-26T00:00:00Z",
    })
    assert "compaction" in line


def test_format_session_line_handles_missing_message(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message",
        "timestamp": "2026-08-26T00:00:00Z",
    })
    assert line.endswith("message -")


def test_format_session_line_tool_call_without_any_known_argument(
    tmp_path,
):
    """Issue #74: a tool call whose arguments carry none of the known
    keys shows the tool name only (no guessed detail)."""
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "mcpTool",
             "arguments": {"other": 1}},
        ]},
    })
    assert "mcpTool" in line
    assert "other" not in line


def test_format_session_line_tool_call_skips_empty_argument_values(
    tmp_path,
):
    """Issue #74: empty argument values are skipped, the next known
    key wins."""
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "search",
             "arguments": {"path": "", "query": "needle"}},
        ]},
    })
    assert "needle" in line


def test_format_session_line_thinking_without_text(tmp_path):
    line = muyan_pilot.format_session_line({
        "type": "message", "id": "a1",
        "timestamp": "2026-08-26T00:00:00Z",
        "message": {"role": "assistant", "content": [
            {"type": "thinking"},
        ]},
    })
    assert line.endswith("assistant")


def test_session_pretty_skips_blank_and_malformed_lines(
    tmp_path, capsys,
):
    """Issue #74: `--pretty` (one-shot) skips blank lines, unparseable
    lines and non-object JSON — only real records are summarized."""
    path = _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1",
         "timestamp": "2026-08-26T00:00:00Z", "cwd": "/w"},
    ])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\ngarbage-not-json\n\"just a string\"\n")
    assert muyan_pilot.main([
        "session", "--pretty", "--config", str(_session_config(tmp_path)),
    ]) == 0
    out = capsys.readouterr().out
    assert "sess-1" in out
    assert "garbage-not-json" not in out
    assert "just a string" not in out


def test_session_follow_pretty_skips_malformed_lines(
    tmp_path, capsys, monkeypatch,
):
    """Issue #74: `--follow --pretty` skips unparseable lines and
    non-object JSON; valid records are summarized."""
    _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1", "cwd": "/w"},
    ])
    monkeypatch.setattr(
        muyan_pilot, "follow_session_file",
        lambda path, poll_interval=0.5: iter([
            json.dumps({"type": "message", "id": "a1",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "message": {"role": "assistant", "content": [
                            {"type": "text", "text": "hello"}]}}),
            "garbage-not-json",
            "123",
        ]),
    )
    assert muyan_pilot.main([
        "session", "--follow", "--pretty", "--config",
        str(_session_config(tmp_path)),
    ]) == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "garbage-not-json" not in out
    assert "123" not in out


def test_session_pretty_prints_summaries_instead_of_raw_jsonl(
    tmp_path, capsys,
):
    _write_session(tmp_path, records=[
        {"type": "session", "id": "sess-1",
         "timestamp": "2026-08-26T00:00:00Z", "cwd": "/w"},
        {"type": "message", "id": "a1",
         "timestamp": "2026-08-26T00:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "hello"}]},
        }],
    )
    assert muyan_pilot.main([
        "session", "--pretty", "--config", str(_session_config(tmp_path)),
    ]) == 0
    out = capsys.readouterr().out
    # The path is not printed in pretty mode; summaries are.
    assert "sess-1" in out
    assert "assistant" in out
    assert "hello" in out
    assert "sess.jsonl" not in out


def test_main_requires_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        muyan_pilot.main(["status", "--config", str(tmp_path / "missing.toml")])


def test_latest_task_worktree_returns_none_when_missing(tmp_path):
    assert muyan_pilot.latest_task_worktree(
        tmp_path, "xqliu/muyan-pilot", 3,
    ) is None


def test_latest_task_worktree_returns_newest_by_mtime(tmp_path):
    old = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-3-run1"
    new = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-3-run2"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    old_time = old.stat().st_mtime
    os.utime(old, (old_time - 100, old_time - 100))
    assert muyan_pilot.latest_task_worktree(
        tmp_path, "xqliu/muyan-pilot", 3,
    ) == new


def test_latest_task_worktree_ignores_other_issues_and_repos(tmp_path):
    other_issue = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-4-run1"
    other_repo = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-ceo-issue-3-run1"
    other_issue.mkdir(parents=True)
    other_repo.mkdir(parents=True)
    assert muyan_pilot.latest_task_worktree(
        tmp_path, "xqliu/muyan-pilot", 3,
    ) is None


def test_live_activity_lines_without_worktree(tmp_path):
    lines = muyan_pilot.live_activity_lines(
        tmp_path, "xqliu/muyan-pilot",
        {"number": 3, "title": "task", "url": "u3"},
    )
    assert lines == ["    live: no task worktree found"]


def test_live_activity_lines_with_worktree_but_no_session(tmp_path):
    worktree = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-3-run1"
    worktree.mkdir(parents=True)
    lines = muyan_pilot.live_activity_lines(
        tmp_path, "xqliu/muyan-pilot",
        {"number": 3, "title": "task", "url": "u3"},
    )
    assert lines == [
        "    live: no pi session yet",
        f"    worktree: {worktree}",
    ]


def test_live_activity_lines_with_session(tmp_path):
    worktree = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-3-run1"
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True)
    with (session_dir / "s.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "session", "id": "sess-1",
            "timestamp": "2026-08-25T02:00:00Z", "cwd": str(worktree),
        }) + "\n")
        handle.write(json.dumps({
            "type": "message", "id": "a1",
            "timestamp": "2026-08-25T02:00:01Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "t1", "name": "bash",
                 "arguments": {"command": "pytest tests/"}}]},
        }) + "\n")
    lines = muyan_pilot.live_activity_lines(
        tmp_path, "xqliu/muyan-pilot",
        {"number": 3, "title": "task", "url": "u3"},
    )
    assert lines == [
        "    live: phase=test last_activity=2026-08-25T02:00:01Z "
        "action=bash pytest tests/ result=-",
        f"    session: {session_dir / 's.jsonl'}",
        f"    worktree: {worktree}",
    ]


def test_status_report_includes_live_lines_for_current_issue(monkeypatch, tmp_path):
    current = {"number": 3, "title": "now", "url": "u3"}
    worktree = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-3-run1"
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True)
    (session_dir / "s.jsonl").write_text(
        json.dumps({"type": "session", "id": "sess-1",
                    "timestamp": "2026-08-25T02:00:00Z",
                    "cwd": str(worktree)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: current)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    report = muyan_pilot.status_report({
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": tmp_path,
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": tmp_path / ".muyan-pilot" / "slots",
    })
    assert "current: #3 now u3" in report
    assert "    live: phase=starting last_activity=- action=- result=-" in report
    assert f"    session: {session_dir / 's.jsonl'}" in report
    assert f"    worktree: {worktree}" in report
    assert "ready: -" in report


def test_status_report_has_no_live_lines_without_current_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    report = muyan_pilot.status_report({
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": tmp_path,
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": tmp_path / ".muyan-pilot" / "slots",
    })
    assert "live:" not in report
    assert "current: -" in report


# --- capacity and slot status (Issue #39) ------------------------------------


def test_status_report_shows_capacity_and_free_slots(monkeypatch, tmp_path):
    monkeypatch.setattr(
        muyan_pilot, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    config = {
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": tmp_path,
        "base_branch": "main",
        "max_concurrency": 2,
        "slot_dir": tmp_path / ".muyan-pilot" / "slots",
    }
    report = muyan_pilot.status_report(config)
    assert "capacity: 2" in report
    assert "slots: 0/2" in report
    assert "slot-1" not in report


def test_status_report_shows_occupied_slots_with_pids(monkeypatch, tmp_path):
    import pilot_slots

    slot_dir = tmp_path / ".muyan-pilot" / "slots"
    # A slot is occupied only while its flock lock is held: hold it in
    # this process for the duration of the report.
    held = pilot_slots.acquire_slot(slot_dir, 1, os.getpid())
    assert held is not None
    monkeypatch.setattr(
        muyan_pilot, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    config = {
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": tmp_path,
        "base_branch": "main",
        "max_concurrency": 2,
        "slot_dir": slot_dir,
    }
    report = muyan_pilot.status_report(config)
    assert "capacity: 2" in report
    assert "slots: 1/2" in report
    assert f"slot-1: pid={os.getpid()}" in report
    assert "slot-2" not in report
    held.release()


def test_status_report_shows_free_slot_when_file_exists_without_lock(
    monkeypatch, tmp_path,
):
    """The lock, not the file, is the token: a leftover slot file from a
    dead process is reported as free."""
    slot_dir = tmp_path / ".muyan-pilot" / "slots"
    slot_dir.mkdir(parents=True)
    (slot_dir / "slot-1").write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(
        muyan_pilot, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    config = {
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": tmp_path,
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": slot_dir,
    }
    report = muyan_pilot.status_report(config)
    assert "slots: 0/1" in report


def test_slot_lines_ignores_corrupted_slot_file(tmp_path):
    slot_dir = tmp_path / "slots"
    slot_dir.mkdir()
    (slot_dir / "slot-1").write_text("garbage", encoding="utf-8")
    lines = muyan_pilot.slot_lines(slot_dir, 1)
    assert lines == ["slots: 0/1"]


# --- deployment consistency (Issue #103) ------------------------------------


def _deploy_world(tmp_path, drift: bool = False) -> tuple[dict, Path]:
    """A deployment checkout with templates plus an installed unit dir."""
    import shutil

    repo = tmp_path / "repo"
    (repo / "systemd").mkdir(parents=True)
    for name, body in (
        ("muyan-pilot@.service", "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n"),
        ("muyan-pilot@.timer", "[Timer]\nOnCalendar=*-*-* *:00/5\n"),
    ):
        (repo / "systemd" / name).write_text(body, encoding="utf-8")
    installed = tmp_path / "units"
    installed.mkdir()
    for name in ("muyan-pilot@.service", "muyan-pilot@.timer"):
        shutil.copyfile(repo / "systemd" / name, installed / name)
    if drift:
        (installed / "muyan-pilot@.service").write_text(
            "[Service]\n# drift\n", encoding="utf-8",
        )
    config = {
        "source_repos": ["xqliu/muyan-pilot"],
        "repo_dir": repo,
        "base_branch": "main",
        "max_concurrency": 1,
        "slot_dir": repo / ".muyan-pilot" / "slots",
    }
    return config, installed


def _fake_doctor_commands(monkeypatch, ssh_down: bool = False) -> list:
    calls: list = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
        if command[:2] == ["git", "ls-remote"]:
            if ssh_down:
                raise subprocess.CalledProcessError(
                    128, command,
                    stderr="git@github.com: Permission denied (publickey).",
                )
            return "abc\tHEAD"
        if command[:2] == ["systemctl", "--user"]:
            assert command[2:5] == ["show", "-p", "ActiveState"]
            return "active"
        if command[:1] == ["journalctl"]:
            return "line one\nline two"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(muyan_pilot, "run_command", fake_run)
    return calls


def test_install_units_command_reports_commit_and_hashes(monkeypatch,
                                                        tmp_path):
    import systemd_deploy

    config, _ = _deploy_world(tmp_path)
    installed = tmp_path / "elsewhere"
    captured = {}

    def fake_install(repo_dir, installed_dir, *, run_command):
        captured["repo_dir"] = repo_dir
        captured["installed_dir"] = installed_dir
        captured["run_command"] = run_command
        return {
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "installed_dir": installed,
            "units": {
                name: {"installed_path": installed / name,
                       "sha256": f"hash-{name}"}
                for name in systemd_deploy.UNIT_NAMES
            },
        }

    monkeypatch.setattr(systemd_deploy, "install_units", fake_install)
    report = muyan_pilot.install_units_command(config, installed)
    assert captured["repo_dir"] == config["repo_dir"]
    assert captured["installed_dir"] == installed
    assert captured["run_command"] is muyan_pilot.run_command
    lines = report.splitlines()
    assert lines[0] == (
        "deployed commit=0123456789abcdef0123456789abcdef01234567 "
        f"installed_dir={installed}"
    )
    assert f"unit=muyan-pilot@.service sha256=hash-muyan-pilot@.service" \
        in lines
    assert f"unit=muyan-pilot@.timer sha256=hash-muyan-pilot@.timer" \
        in lines


def test_main_install_units_prints_report_and_returns_zero(
    monkeypatch, tmp_path, capsys,
):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n",
                      encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}

    def fake_command(cfg, installed_dir):
        seen["installed_dir"] = installed_dir
        return "deployed commit=abc installed_dir=/x"

    monkeypatch.setattr(muyan_pilot, "install_units_command", fake_command)
    assert muyan_pilot.main([
        "install-units", "--config", str(config),
        "--installed-dir", str(tmp_path / "u"),
    ]) == 0
    assert seen["installed_dir"] == tmp_path / "u"
    out = capsys.readouterr().out
    assert "deployed commit=abc installed_dir=/x" in out


# --- setup (Issue #117) -------------------------------------------------------



def _setup_result() -> dict:
    """A complete run_setup result document (format_setup contract)."""
    return {
        "setup": "ok",
        "version": 2,
        "base_branch": "main",
        "cli": {
            "action": "verified",
            "source": "/repo/muyan_pilot.py",
        },
        "repos": [
            {
                "repo": "xqliu/muyan-pilot",
                "permission": "ADMIN",
                "default_branch": "main",
                "labels": {"aligned": 7, "total": 7},
            },
        ],
        "service": {
            "installed": True,
            "installed_path": "/u/.config/systemd/user/muyan-pilot@.service",
            "sha256": "ab" * 32,
        },
        "timer": {
            "instances": {
                "muyan-pilot@1.timer": {
                    "enabled": True, "active": True, "next": "-",
                },
                "muyan-pilot@2.timer": {
                    "enabled": True, "active": True, "next": "-",
                },
            },
        },
        "checkout": {
            "remote": "origin",
            "branch": "main",
            "clean": True,
            "base_fresh": True,
            "remote_url": "git@github.com:xqliu/muyan-pilot.git",
            "remote_protocol": "ssh",
            "migrated": False,
            "ssh_reachable": True,
        },
        "optional_proxy": {
            "optional": True,
            "proxy": "healthy",
            "url": "http://127.0.0.1:18082/health",
        },
    }

def _setup_world(tmp_path):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n",
                      encoding="utf-8")
    _write_prompts(tmp_path)
    return config


def test_main_setup_prints_key_value_lines_and_returns_zero(
    monkeypatch, tmp_path, capsys,
):
    config = _setup_world(tmp_path)
    seen = {}
    result = _setup_result()

    def fake_run_setup(cfg, installed_dir, **kwargs):
        seen["config"] = cfg
        seen["installed_dir"] = installed_dir
        seen.update(kwargs)
        return result

    monkeypatch.setattr(muyan_pilot.pilot_setup, "run_setup", fake_run_setup)
    assert muyan_pilot.main([
        "setup", "--config", str(config),
        "--installed-dir", str(tmp_path / "u"),
    ]) == 0
    assert seen["installed_dir"] == tmp_path / "u"
    assert seen["repos"] is None
    assert seen["config"]["source_repos"] == ["xqliu/muyan-pilot"]
    out = capsys.readouterr().out
    assert "setup=ok" in out


def test_main_setup_json_prints_the_equivalent_document(
    monkeypatch, tmp_path, capsys,
):
    config = _setup_world(tmp_path)
    result = {"setup": "ok", "version": 1, "base_branch": "main"}
    monkeypatch.setattr(
        muyan_pilot.pilot_setup, "run_setup",
        lambda cfg, installed_dir, **kwargs: result,
    )
    assert muyan_pilot.main([
        "setup", "--json", "--config", str(config),
    ]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == result


def test_main_setup_repo_override_is_passed_through(
    monkeypatch, tmp_path, capsys,
):
    config = _setup_world(tmp_path)
    seen = {}

    def fake_run_setup(cfg, installed_dir, **kwargs):
        seen.update(kwargs)
        return _setup_result()

    monkeypatch.setattr(muyan_pilot.pilot_setup, "run_setup", fake_run_setup)
    assert muyan_pilot.main([
        "setup", "--repo", "xqliu/muyan-pilot", "--config", str(config),
    ]) == 0
    assert seen["repos"] == ["xqliu/muyan-pilot"]


def test_main_setup_failure_prints_the_reason_and_returns_nonzero(
    monkeypatch, tmp_path, capsys,
):
    config = _setup_world(tmp_path)

    def failing(cfg, installed_dir, **kwargs):
        raise muyan_pilot.pilot_setup.SetupError(
            "insufficient permission for xqliu/muyan-pilot",
        )

    monkeypatch.setattr(muyan_pilot.pilot_setup, "run_setup", failing)
    assert muyan_pilot.main([
        "setup", "--config", str(config),
    ]) == 1
    err = capsys.readouterr().err
    assert "setup_failed" in err
    assert "insufficient permission" in err


def test_doctor_report_clean(tmp_path, monkeypatch):
    config, installed = _deploy_world(tmp_path, drift=False)
    calls = _fake_doctor_commands(monkeypatch)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, installed)
    lines = report.splitlines()
    assert lines[0] == f"repo: {config['repo_dir']}"
    assert lines[1] == "commit: 0123456789abcdef0123456789abcdef01234567"
    assert "unit_drift: clean" in lines
    # Both units are reported with their installed hash.
    import systemd_deploy
    status = systemd_deploy.unit_status(config["repo_dir"], installed)
    for entry in status:
        assert (
            f"  {entry['unit']}: sha256={entry['installed_sha256']}"
        ) in lines
    # The git transport (Issue #114): the checkout's origin is SSH for
    # the configured source repo and the SSH probe succeeded.
    assert (
        "transport: remote=origin "
        "url=git@github.com:xqliu/muyan-pilot.git protocol=ssh "
        "expected=git@github.com:xqliu/muyan-pilot.git ssh_reachable=true"
    ) in lines
    # The probe is the real read-only command (verified against the
    # live CLI: exit 0 = reachable + authenticated).
    assert [
        "git", "ls-remote", "git@github.com:xqliu/muyan-pilot.git",
    ] in calls
    # Issue #149: all FOUR instances are reported.
    assert "muyan-pilot@1.timer: active" in lines
    assert "muyan-pilot@2.timer: active" in lines
    assert "muyan-pilot@1.service: active" in lines
    assert "muyan-pilot@2.service: active" in lines
    assert "slots: 0/1" in lines
    assert "pi: none" in lines
    assert "source: xqliu/muyan-pilot" in lines
    assert "  current: -" in lines
    assert "journal:" in lines
    assert "  line one" in lines
    assert "  line two" in lines
    # The exact read-only commands (verified against the live machine:
    # instance names, never the bare template name).
    for unit in ("muyan-pilot@1.timer", "muyan-pilot@2.timer",
                 "muyan-pilot@1.service", "muyan-pilot@2.service"):
        assert [
            "systemctl", "--user", "show", "-p", "ActiveState",
            "--value", unit,
        ] in calls
    assert [
        "journalctl", "--user",
        "-u", "muyan-pilot@1.service",
        "-u", "muyan-pilot@2.service",
        "-n", "20", "--no-pager",
    ] in calls


def test_doctor_report_reports_a_failed_transport(tmp_path, monkeypatch):
    """Issue #114: doctor is the diagnostic report — a failed transport
    (SSH unreachable) is REPORTED with the structured reason, not
    raised: the rest of the health report stays readable. The
    fail-fast gate is the pre-start check, not doctor."""
    config, installed = _deploy_world(tmp_path, drift=False)
    _fake_doctor_commands(monkeypatch, ssh_down=True)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, installed)
    lines = report.splitlines()
    failed = [line for line in lines if line.startswith("transport: FAILED")]
    assert len(failed) == 1
    assert "ssh_unreachable" in failed[0]
    assert "Permission denied (publickey)" in failed[0]
    # The report continues past the transport failure.
    assert "slots: 0/1" in lines
    assert "journal:" in lines


def test_doctor_report_drift_carries_paths_hashes_and_fix(
    tmp_path, monkeypatch,
):
    config, installed = _deploy_world(tmp_path, drift=True)
    _fake_doctor_commands(monkeypatch)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    import systemd_deploy
    report = muyan_pilot.doctor_report(config, installed)
    lines = report.splitlines()
    assert "unit_drift: DRIFT" in lines
    drifted = [
        e for e in systemd_deploy.unit_status(
            config["repo_dir"], installed,
        ) if e["drifted"]
    ]
    assert len(drifted) == 1
    entry = drifted[0]
    assert (
        f"  {entry['unit']}: repo={entry['repo_path']} "
        f"installed={entry['installed_path']} "
        f"repo_sha256={entry['repo_sha256']} "
        f"installed_sha256={entry['installed_sha256']}"
    ) in lines
    assert f"  fix: {systemd_deploy.FIX_COMMAND}" in lines


def _fake_cli_source(monkeypatch, drifted: bool = False) -> dict:
    """Stub the read-only CLI source check (Issue #152) for the doctor
    tests: a clean editable source from the world's checkout, or a
    drifted site-packages source with the exact fix command."""
    import types

    if drifted:
        actual = Path(
            "/home/u/.local/share/uv/tools/muyan-pilot/"
            "lib/python3.14/site-packages/muyan_pilot.py",
        )
    else:
        actual = None  # filled per-world below
    state = {"actual": actual}

    def fake_cli_source(expected_repo_dir):
        if state["actual"] is None:
            state["actual"] = Path(expected_repo_dir) / "muyan_pilot.py"
        return {
            "actual": Path(state["actual"]),
            "expected": Path(expected_repo_dir).resolve(),
            "editable": not drifted,
            "fix": (
                "uv tool install --force --reinstall --editable "
                f"--python /usr/bin/python3 {Path(expected_repo_dir)}"
            ),
        }

    def fake_drift_line(source):
        if source["editable"]:
            return None
        from pi_activity import quote_value

        return (
            "cli_source_drift "
            f"source={quote_value(str(source['actual']))} "
            f"expected={quote_value(str(source['expected']))} "
            f"fix={quote_value(source['fix'])}"
        )

    stub = types.SimpleNamespace(
        cli_source=fake_cli_source, drift_line=fake_drift_line,
    )
    monkeypatch.setattr(muyan_pilot, "cli_source", stub)
    return state


def test_doctor_report_cli_source_clean(tmp_path, monkeypatch):
    """Issue #152: doctor read-only verifies that the running process
    imports `muyan_pilot` from the configured repo_dir (the editable
    uv tool install): the clean report names the import source."""
    config, installed = _deploy_world(tmp_path, drift=False)
    _fake_doctor_commands(monkeypatch)
    _fake_cli_source(monkeypatch, drifted=False)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, installed)
    lines = report.splitlines()
    assert any(
        line.startswith("cli_source: clean") for line in lines
    ), report
    assert (
        f"cli_source: clean source={config['repo_dir'] / 'muyan_pilot.py'}"
    ) in lines
    assert "cli_source_drift" not in report


def test_doctor_report_cli_source_drift_carries_source_expected_fix(
    tmp_path, monkeypatch,
):
    """Issue #152: a non-editable (site-packages) or stale source is
    REPORTED with the structured `cli_source_drift` line (actual path,
    expected repo_dir, the exact editable reinstall command) — the fix
    leads with the editable reinstall, never with
    `muyan-pilot install-units` alone. The report stays readable and
    the rest of the health report is still produced."""
    config, installed = _deploy_world(tmp_path, drift=False)
    _fake_doctor_commands(monkeypatch)
    _fake_cli_source(monkeypatch, drifted=True)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, installed)
    lines = report.splitlines()
    assert "cli_source: DRIFT" in lines
    assert any(
        line.startswith("  cli_source_drift ")
        and "source=/home/u/.local/share/uv/tools/muyan-pilot/"
        in line
        and f"expected={config['repo_dir'].resolve()}" in line
        and (
            'fix="uv tool install --force --reinstall --editable '
            '--python /usr/bin/python3 '
            f"{config['repo_dir']}\""
        ) in line
        for line in lines
    ), f"drift line missing or malformed in:\n{report}"
    # The report continues past the drift (doctor is read-only).
    assert "slots: 0/1" in lines
    assert "journal:" in lines


def test_doctor_report_missing_installed_unit_is_drift(tmp_path, monkeypatch):
    config, installed = _deploy_world(tmp_path, drift=False)
    (installed / "muyan-pilot@.timer").unlink()
    _fake_doctor_commands(monkeypatch)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, installed)
    assert "unit_drift: DRIFT" in report
    assert "muyan-pilot@.timer" in report
    assert "installed_sha256=-" in report


def test_doctor_report_defaults_to_the_standard_installed_dir(
    tmp_path, monkeypatch,
):
    """Without --installed-dir the report checks the standard user dir
    (here pointed at the test world via MUYAN_PILOT_UNIT_DIR)."""
    config, installed = _deploy_world(tmp_path, drift=False)
    monkeypatch.setenv("MUYAN_PILOT_UNIT_DIR", str(installed))
    _fake_doctor_commands(monkeypatch)
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    report = muyan_pilot.doctor_report(config, None)
    assert "unit_drift: clean" in report


def test_doctor_report_shows_current_issue_and_session(tmp_path, monkeypatch):
    config, installed = _deploy_world(tmp_path, drift=False)
    _fake_doctor_commands(monkeypatch)
    issue = {"number": 7, "title": "task",
             "url": "https://github.com/xqliu/muyan-pilot/issues/7"}
    monkeypatch.setattr(
        muyan_pilot, "current_issue", lambda repo: issue,
    )
    session = (config["repo_dir"] / ".worktrees" / "w" / ".pi-session"
               / "s.jsonl")
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    report = muyan_pilot.doctor_report(config, installed)
    assert "  current: #7 task https://github.com/xqliu/muyan-pilot/issues/7" in report
    assert f"pi: {session}" in report


def test_fake_doctor_commands_rejects_unexpected_commands(
    tmp_path, monkeypatch,
):
    """The doctor fake must fail loudly on any command it does not
    answer (no silent pass for an unexpected external call)."""
    _deploy_world(tmp_path)
    _fake_doctor_commands(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        muyan_pilot.run_command(["gh", "issue", "list"])


def test_doctor_report_fails_fast_when_a_command_fails(tmp_path, monkeypatch):
    import subprocess

    config, installed = _deploy_world(tmp_path, drift=False)
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, command, stderr="boom"),
        ),
    )
    with pytest.raises(subprocess.CalledProcessError):
        muyan_pilot.doctor_report(config, installed)


def test_main_doctor_prints_report_and_returns_zero(
    monkeypatch, tmp_path, capsys,
):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n",
                      encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}

    def fake_report(cfg, installed_dir):
        seen["installed_dir"] = installed_dir
        return "repo: /x\nunit_drift: clean"

    monkeypatch.setattr(muyan_pilot, "doctor_report", fake_report)
    assert muyan_pilot.main([
        "doctor", "--config", str(config),
        "--installed-dir", str(tmp_path / "u"),
    ]) == 0
    assert seen["installed_dir"] == tmp_path / "u"
    out = capsys.readouterr().out
    assert "unit_drift: clean" in out
