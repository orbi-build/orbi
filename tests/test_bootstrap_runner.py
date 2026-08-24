import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner


def test_parse_issue_list_returns_first_issue():
    issue = {"number": 7, "title": "ship", "body": "do it"}
    assert runner.parse_issue_list(json.dumps([issue])) == issue


def test_parse_issue_list_returns_none_for_empty_list():
    assert runner.parse_issue_list("[]") is None


def test_parse_issue_list_rejects_non_list():
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.parse_issue_list("{}")


def test_run_command_returns_stdout(monkeypatch, tmp_path):
    completed = Mock(stdout=" output \n", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    assert runner.run_command(["git", "status"], cwd=tmp_path) == "output"
    runner.subprocess.run.assert_called_once_with(
        ["git", "status"], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=runner.COMMAND_TIMEOUT,
    )


def test_run_command_logs_stderr(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="ok", stderr="warning\n")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["gh", "--version"], cwd=tmp_path) == "ok"
    assert "stderr=warning" in caplog.text


def test_pick_issue_uses_github_queue(monkeypatch):
    issue = {"number": 9, "title": "task", "body": "body"}
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/muyan-ceo") == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
        "--state", "open", "--search",
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened -label:ai-blocked",
        "--json", "number,title,body", "--limit", "1",
    ]]


def test_edit_issue_builds_add_and_remove_command(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/muyan-ceo", add="ai-in-progress", remove="ai-ready")
    assert calls == [[
        "gh", "issue", "edit", "3", "--repo", "xqliu/muyan-ceo",
        "--add-label", "ai-in-progress", "--remove-label", "ai-ready",
    ]]


def test_edit_issue_allows_no_label_change(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/muyan-ceo")
    assert calls == [["gh", "issue", "edit", "3", "--repo", "xqliu/muyan-ceo"]]


def test_create_worktree_rejects_existing_path(monkeypatch, tmp_path):
    existing = tmp_path / "issue-3"
    existing.mkdir()
    monkeypatch.setattr(runner, "worktree_path", lambda number: existing)
    with pytest.raises(RuntimeError, match="worktree path already exists"):
        runner.create_worktree(tmp_path, 3)


def test_create_worktree_runs_git_add(monkeypatch, tmp_path):
    path = tmp_path / "issue-3"
    monkeypatch.setattr(runner, "worktree_path", lambda number: path)
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append((command, kwargs)))
    assert runner.create_worktree(tmp_path, 3) == path
    assert calls == [(
        ["git", "worktree", "add", "-b", "muyan-pilot/issue-3", str(path), "HEAD"],
        {"cwd": tmp_path},
    )]


def test_worktree_path_uses_temp_directory(monkeypatch):
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: "/tmp")
    assert runner.worktree_path(3) == Path("/tmp/muyan-pilot-issue-3")


def test_comment_issue_runs_gh_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.comment_issue(3, repo="xqliu/muyan-ceo", body="done")
    assert calls == [[
        "gh", "issue", "comment", "3", "--repo", "xqliu/muyan-ceo",
        "--body", "done",
    ]]


def test_run_pi_loads_prompt_and_invokes_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    assert runner.run_pi(issue, tmp_path, prompt_path, timeout=99) == "done"
    command, kwargs = calls[0]
    assert command[:5] == ["pi", "--print", "--session-dir", str(tmp_path / ".pi-session"), "--system-prompt"]
    assert command[5] == "SYSTEM"
    assert "Issue #4: Fix title" in command[6]
    assert "Fix body" in command[6]
    assert kwargs == {"cwd": tmp_path, "timeout": 99}


def test_verify_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "other-branch")
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.verify_pr(tmp_path, "muyan-pilot/issue-4")


def test_verify_pr_rejects_missing_pr(monkeypatch, tmp_path):
    outputs = iter(["muyan-pilot/issue-4", "[]"])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(tmp_path, "muyan-pilot/issue-4")


def test_verify_pr_rejects_non_array(monkeypatch, tmp_path):
    outputs = iter(["muyan-pilot/issue-4", "{}"])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(tmp_path, "muyan-pilot/issue-4")


def test_verify_pr_returns_url(monkeypatch, tmp_path):
    outputs = iter([
        "muyan-pilot/issue-4",
        '[{"url":"https://github.com/muyantech/muyan-pilot/pull/4"}]',
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    assert runner.verify_pr(tmp_path, "muyan-pilot/issue-4") == "https://github.com/muyantech/muyan-pilot/pull/4"


def test_verify_pr_rejects_pr_without_url(monkeypatch, tmp_path):
    outputs = iter(["muyan-pilot/issue-4", "[{}]"])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="open PR has no URL"):
        runner.verify_pr(tmp_path, "muyan-pilot/issue-4")


def test_process_issue_success(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "verify_pr", lambda *args: "https://github.com/muyantech/muyan-pilot/pull/4")
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    assert runner.process_issue(issue, tmp_path, tmp_path / "prompt.md", "xqliu/muyan-ceo") == "https://github.com/muyantech/muyan-pilot/pull/4"
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/muyan-ceo", "add": "ai-in-progress"})
    assert calls[1][0] == "edit"
    assert calls[1][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-pr-opened", "remove": "ai-in-progress"}
    assert calls[2][0] == "comment"


def test_process_issue_failure_marks_blocked_and_reraises(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    with pytest.raises(RuntimeError, match="git failed"):
        runner.process_issue({"number": 8, "title": "Fail", "body": ""}, tmp_path, tmp_path / "prompt.md", "xqliu/muyan-ceo")
    assert calls[1][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-blocked", "remove": "ai-in-progress"}
    assert calls[2][0] == "comment"


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "pick_issue", lambda repo: None)
    assert runner.main(["--repo-dir", str(tmp_path), "--prompt", str(tmp_path / "prompt.md")]) == 0


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setattr(runner, "pick_issue", lambda repo: issue)
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or "https://github.com/x/y/pull/12")
    assert runner.main(["--repo-dir", str(tmp_path), "--prompt", str(tmp_path / "prompt.md")]) == 0
    assert calls[0][0][0] == issue


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "pick_issue", lambda repo: {"number": 1, "title": "task"})
    with pytest.raises(FileNotFoundError):
        runner.main(["--repo-dir", str(tmp_path), "--prompt", str(tmp_path / "missing.md")])
