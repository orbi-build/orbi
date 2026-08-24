import json
from pathlib import Path

import pytest

import bootstrap_runner as runner
import muyan_pilot


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


def test_ready_issues_returns_queue_in_github_order(monkeypatch):
    issues = [
        {"number": 15, "title": "t15", "url": "u15"},
        {"number": 14, "title": "t14", "url": "u14"},
    ]
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append(command) or json.dumps(issues),
    )
    assert muyan_pilot.ready_issues("xqliu/muyan-pilot") == issues
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/muyan-pilot",
        "--state", "open", "--search", "label:ai-ready -label:ai-in-progress",
        "--json", "number,title,url,state", "--limit", "10",
    ]]


def test_ready_issues_returns_empty_list_when_idle(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "run_command", lambda command, **kwargs: "[]")
    assert muyan_pilot.ready_issues("xqliu/muyan-pilot") == []


def test_list_labeled_issues_uses_custom_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )
    muyan_pilot.list_labeled_issues("owner/repo", "ai-ready", limit=3)
    assert calls[0][-2:] == ["--limit", "3"]


def test_systemd_status_reports_waiting_while_service_active(monkeypatch):
    outputs = iter([
        "active",
        "active",
        "- - Tue 2026-08-25 01:23:24 +08 15min ago muyan-pilot.timer muyan-pilot.service",
    ])
    monkeypatch.setattr(muyan_pilot, "run_command", lambda command, **kwargs: next(outputs))
    assert muyan_pilot.systemd_status() == [
        "  timer: active",
        "  service: active",
        "  next trigger: waiting for service to finish",
    ]


def test_systemd_status_reports_next_trigger_when_service_idle(monkeypatch):
    outputs = iter([
        "active",
        "inactive",
        "Tue 2026-08-25 01:30:00 +08  5min  Tue 2026-08-25 01:25:00 +08  5min  muyan-pilot.timer  muyan-pilot.service",
    ])
    monkeypatch.setattr(muyan_pilot, "run_command", lambda command, **kwargs: next(outputs))
    assert muyan_pilot.systemd_status() == [
        "  timer: active",
        "  service: inactive",
        "  next trigger: Tue 2026-08-25 01:30:00 +08",
    ]


def test_systemd_status_marks_empty_states_and_missing_next_trigger(monkeypatch):
    outputs = iter(["", "", ""])
    monkeypatch.setattr(muyan_pilot, "run_command", lambda command, **kwargs: next(outputs))
    assert muyan_pilot.systemd_status() == [
        "  timer: -",
        "  service: -",
        "  next trigger: -",
    ]


def test_systemd_status_marks_unavailable_when_systemctl_missing(monkeypatch):
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(FileNotFoundError("systemctl")),
    )
    assert muyan_pilot.systemd_status() == ["  systemd: unavailable (systemctl)"]


def test_systemd_status_marks_unavailable_when_systemctl_fails(monkeypatch):
    error = runner.subprocess.CalledProcessError(3, ["systemctl"], stderr="no")
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    assert muyan_pilot.systemd_status() == [
        f"  systemd: unavailable ({error})",
    ]


def test_worktree_list_runs_git_in_configured_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(
        muyan_pilot, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "raw",
    )
    assert muyan_pilot.worktree_list({"repo_dir": Path("/repo")}) == "raw"
    assert calls == [(
        ["git", "worktree", "list", "--porcelain"], {"cwd": Path("/repo")},
    )]


def test_worktree_entries_parses_porcelain_output():
    raw = (
        "worktree /home/x/muyan-pilot\n"
        "HEAD fb8ead2\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/muyan-pilot-xqliu-muyan-pilot-issue-18\n"
        "HEAD d0bbc08\n"
        "branch refs/heads/muyan-pilot/xqliu-muyan-pilot-issue-18\n"
    )
    assert muyan_pilot._worktree_entries(raw) == [
        {"path": Path("/home/x/muyan-pilot"), "branch": "refs/heads/main"},
        {
            "path": Path("/tmp/muyan-pilot-xqliu-muyan-pilot-issue-18"),
            "branch": "refs/heads/muyan-pilot/xqliu-muyan-pilot-issue-18",
        },
    ]


def test_worktree_entries_keeps_detached_head_without_branch():
    raw = "worktree /tmp/wt\nHEAD abc\n"
    assert muyan_pilot._worktree_entries(raw) == [
        {"path": Path("/tmp/wt"), "branch": ""},
    ]


def test_worktree_entries_ignores_branch_line_without_worktree():
    assert muyan_pilot._worktree_entries("branch refs/heads/main\n") == []


def test_session_info_reads_first_record(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text(
        '{"type":"session","id":"abc","cwd":"/tmp/wt"}\n'
        '{"type":"message"}\n',
        encoding="utf-8",
    )
    assert muyan_pilot.session_info(str(session)) == {"id": "abc", "cwd": "/tmp/wt"}


def test_session_info_defaults_missing_fields(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text('{"type":"session"}\n', encoding="utf-8")
    assert muyan_pilot.session_info(str(session)) == {"id": "-", "cwd": "-"}


def test_session_info_marks_dash_placeholder():
    assert muyan_pilot.session_info("-") == {"id": "-", "cwd": "-"}


def test_session_info_marks_missing_file(tmp_path):
    assert muyan_pilot.session_info(str(tmp_path / "nope.jsonl")) == {"id": "-", "cwd": "-"}


def test_session_info_marks_empty_file(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text("", encoding="utf-8")
    assert muyan_pilot.session_info(str(session)) == {"id": "-", "cwd": "-"}


def test_session_info_marks_invalid_json(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text("not json\n", encoding="utf-8")
    assert muyan_pilot.session_info(str(session)) == {"id": "-", "cwd": "-"}


def test_stage_for_reports_verify_when_verify_md_exists(tmp_path):
    (tmp_path / "verify.md").write_text("v", encoding="utf-8")
    (tmp_path / "plan.md").write_text("p", encoding="utf-8")
    assert muyan_pilot.stage_for(tmp_path) == "verify"


def test_stage_for_reports_testing_when_test_log_exists(tmp_path):
    (tmp_path / "test.log").write_text("t", encoding="utf-8")
    (tmp_path / "plan.md").write_text("p", encoding="utf-8")
    assert muyan_pilot.stage_for(tmp_path) == "testing"


def test_stage_for_reports_planning_when_plan_md_exists(tmp_path):
    (tmp_path / "plan.md").write_text("p", encoding="utf-8")
    assert muyan_pilot.stage_for(tmp_path) == "planning"


def test_stage_for_reports_started_when_no_artifacts(tmp_path):
    assert muyan_pilot.stage_for(tmp_path) == "started"


def test_current_run_describes_pilot_worktree(monkeypatch, tmp_path):
    worktree = tmp_path / "muyan-pilot-xqliu-muyan-pilot-issue-18"
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True)
    session = session_dir / "2026-08-25T01-23-26-000Z_abc.jsonl"
    session.write_text(
        '{"type":"session","id":"abc","cwd":"' + str(worktree) + '"}\n',
        encoding="utf-8",
    )
    (worktree / "plan.md").write_text("p", encoding="utf-8")
    other = tmp_path / "other-worktree"
    other.mkdir()
    raw = (
        f"worktree {other}\nHEAD a\nbranch refs/heads/main\n\n"
        f"worktree {worktree}\nHEAD b\n"
        "branch refs/heads/muyan-pilot/xqliu-muyan-pilot-issue-18\n"
    )
    monkeypatch.setattr(muyan_pilot, "worktree_list", lambda config: raw)
    assert muyan_pilot.current_run({"repo_dir": tmp_path}) == [
        f"  worktree: {worktree}",
        "  branch: refs/heads/muyan-pilot/xqliu-muyan-pilot-issue-18",
        "  stage: planning",
        f"  pi_session: {session_dir}",
        f"  pi_session_file: {session}",
        "  session id: abc",
        f"  session cwd: {worktree}",
    ]


def test_current_run_marks_missing_pilot_worktree(monkeypatch):
    monkeypatch.setattr(
        muyan_pilot, "worktree_list",
        lambda config: "worktree /home/x/muyan-pilot\nHEAD a\nbranch refs/heads/main\n",
    )
    assert muyan_pilot.current_run({"repo_dir": Path("/home/x/muyan-pilot")}) == [
        "  worktree: -",
    ]


def test_current_run_marks_detached_branch_and_missing_session(monkeypatch, tmp_path):
    worktree = tmp_path / "muyan-pilot-owner-repo-issue-3"
    worktree.mkdir()
    monkeypatch.setattr(
        muyan_pilot, "worktree_list",
        lambda config: f"worktree {worktree}\nHEAD a\n",
    )
    lines = muyan_pilot.current_run({"repo_dir": tmp_path})
    assert "  branch: -" in lines
    assert "  pi_session_file: -" in lines
    assert "  session id: -" in lines


def test_newest_session_file_orders_by_file_name_not_path(monkeypatch, tmp_path):
    newer = tmp_path / "muyan-pilot-owner-repo-issue-1"
    (newer / ".pi-session").mkdir(parents=True)
    (newer / ".pi-session" / "2026-08-25T01-23-26-000Z_a.jsonl").write_text(
        "{}", encoding="utf-8",
    )
    older = tmp_path / "muyan-pilot-owner-repo-issue-9"
    (older / ".pi-session").mkdir(parents=True)
    (older / ".pi-session" / "2026-08-25T01-10-16-000Z_b.jsonl").write_text(
        "{}", encoding="utf-8",
    )
    without_session = tmp_path / "muyan-pilot-owner-repo-issue-2"
    without_session.mkdir()
    raw = (
        f"worktree {older}\nHEAD a\nbranch refs/heads/b1\n"
        f"worktree {newer}\nHEAD b\nbranch refs/heads/b2\n"
        f"worktree {without_session}\nHEAD c\nbranch refs/heads/b3\n"
    )
    monkeypatch.setattr(muyan_pilot, "worktree_list", lambda config: raw)
    assert muyan_pilot.newest_session_file({"repo_dir": tmp_path}) == str(
        newer / ".pi-session" / "2026-08-25T01-23-26-000Z_a.jsonl",
    )


def test_newest_session_file_marks_missing_session(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "worktree_list", lambda config: "")
    assert muyan_pilot.newest_session_file({"repo_dir": Path("/repo")}) == "-"


def test_troubleshooting_points_at_journal_and_newest_session(monkeypatch, tmp_path):
    worktree = tmp_path / "muyan-pilot-owner-repo-issue-3"
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True)
    (session_dir / "2026-08-25T01-23-26-000Z_a.jsonl").write_text("{}", encoding="utf-8")
    (session_dir / "2026-08-25T01-24-00-000Z_b.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        muyan_pilot, "worktree_list",
        lambda config: f"worktree {worktree}\nHEAD a\nbranch refs/heads/b\n",
    )
    assert muyan_pilot.troubleshooting({"repo_dir": tmp_path}) == [
        "  journal: journalctl --user -u muyan-pilot.service --since today",
        "  session: " + str(session_dir / "2026-08-25T01-24-00-000Z_b.jsonl"),
    ]


def test_troubleshooting_marks_missing_session(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "worktree_list", lambda config: "")
    lines = muyan_pilot.troubleshooting({"repo_dir": Path("/repo")})
    assert lines[1] == "  session: -"


def test_recent_result_returns_newest_pr_opened_or_blocked_issue(monkeypatch):
    pr_opened = {"number": 2, "title": "done", "url": "u2", "state": "CLOSED"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}
    calls = []

    def fake_list(repo, label, state="open"):
        calls.append((label, state))
        if label == "ai-pr-opened":
            return [pr_opened]
        if label == "ai-blocked":
            return [blocked]
        return []

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == blocked
    assert calls == [("ai-pr-opened", "all"), ("ai-blocked", "all")]


def test_recent_result_prefers_pr_opened_when_newer(monkeypatch):
    pr_opened = {"number": 9, "title": "done", "url": "u9", "state": "OPEN"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}

    def fake_list(repo, label, state="open"):
        if label == "ai-pr-opened":
            return [pr_opened]
        if label == "ai-blocked":
            return [blocked]
        return []

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
    ready = [
        {"number": 11, "title": "next", "url": "u11"},
        {"number": 12, "title": "next2", "url": "u12"},
    ]
    result = {"number": 2, "title": "done", "url": "u2"}
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: current)
    monkeypatch.setattr(muyan_pilot, "ready_issues", lambda repo: ready)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: result)
    monkeypatch.setattr(
        muyan_pilot, "systemd_status", lambda: ["  timer: active (running)"],
    )
    monkeypatch.setattr(muyan_pilot, "current_run", lambda config: ["  worktree: /tmp/wt"])
    monkeypatch.setattr(
        muyan_pilot, "troubleshooting",
        lambda config: ["  journal: journalctl --user -u muyan-pilot.service --since today"],
    )
    report = muyan_pilot.status_report({"source_repos": ["xqliu/muyan-pilot"]})
    assert "source: xqliu/muyan-pilot" in report
    assert "current: #3 now u3" in report
    assert "ready: #11 next u11" in report
    assert "ready: #12 next2 u12" in report
    assert "result: #2 done u2" in report
    assert "systemd:" in report
    assert "  timer: active (running)" in report
    assert "current run:" in report
    assert "  worktree: /tmp/wt" in report
    assert "troubleshooting:" in report
    assert "  journal: journalctl --user -u muyan-pilot.service --since today" in report


def test_status_report_marks_empty_lookups(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issues", lambda repo: [])
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "systemd_status", lambda: ["  timer: -"])
    monkeypatch.setattr(muyan_pilot, "current_run", lambda config: ["  worktree: -"])
    monkeypatch.setattr(muyan_pilot, "troubleshooting", lambda config: ["  session: -"])
    report = muyan_pilot.status_report({"source_repos": ["xqliu/muyan-pilot"]})
    assert "current: -" in report
    assert "ready: -" in report
    assert "result: -" in report


def test_main_add_dispatches_to_selected_source_repo(monkeypatch, tmp_path, capsys):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text(
        "source_repos = [\"xqliu/muyan-pilot\", \"xqliu/muyan-ceo\"]\n",
        encoding="utf-8",
    )
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
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
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
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
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    with pytest.raises(SystemExit):
        muyan_pilot.main([
            "add", "T", "--repo", "other/repo", "--config", str(config),
        ])


def test_main_status_prints_report(monkeypatch, tmp_path, capsys):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\"]\n", encoding="utf-8")
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
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


def test_main_requires_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        muyan_pilot.main(["status", "--config", str(tmp_path / "missing.toml")])
