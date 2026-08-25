import json

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
        return [pr_opened] if label == "ai-pr-opened" else [blocked]

    monkeypatch.setattr(muyan_pilot, "list_labeled_issues", fake_list)
    assert muyan_pilot.recent_result("xqliu/muyan-pilot") == blocked
    assert calls == [("ai-pr-opened", "all"), ("ai-blocked", "all")]


def test_recent_result_prefers_pr_opened_when_newer(monkeypatch):
    pr_opened = {"number": 9, "title": "done", "url": "u9", "state": "OPEN"}
    blocked = {"number": 5, "title": "stuck", "url": "u5", "state": "OPEN"}

    def fake_list(repo, label, state="open"):
        return [pr_opened] if label == "ai-pr-opened" else [blocked]

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
    report = muyan_pilot.status_report({"source_repos": ["xqliu/muyan-pilot"]})
    assert "source: xqliu/muyan-pilot" in report
    assert "current: #3 now u3" in report
    assert "ready: #11 next u11" in report
    assert "result: #2 done u2" in report


def test_status_report_marks_empty_lookups(monkeypatch):
    monkeypatch.setattr(muyan_pilot, "current_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "ready_issue", lambda repo: None)
    monkeypatch.setattr(muyan_pilot, "recent_result", lambda repo: None)
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
