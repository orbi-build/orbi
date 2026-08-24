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


def test_load_config_resolves_relative_paths_and_values(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text(
        """source_repos = [\"owner/pilot\", \"owner/backlog\"]\nrepo_dir = \"repo\"\nworkspace_root = \"..\"\nprompt = \"prompt.md\"\nskills = [\"skill.md\"]\ncontext_files = [\"context.md\"]\n""",
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["source_repos"] == ["owner/pilot", "owner/backlog"]
    assert config["repo_dir"] == (tmp_path / "repo").resolve()
    assert config["workspace_root"] == tmp_path.parent.resolve()
    assert config["prompt"] == (tmp_path / "prompt.md").resolve()
    assert config["skills"] == [(tmp_path / "skill.md").resolve()]
    assert config["context_files"] == [(tmp_path / "context.md").resolve()]


def test_load_config_requires_source_repos(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text("prompt = \"prompt.md\"\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must be a non-empty list"):
        runner.load_config(config_path)


def test_load_config_rejects_empty_source_repo_name(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text('source_repos = ["owner/repo", ""]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must contain non-empty strings"):
        runner.load_config(config_path)


def test_render_prompt_replaces_context_values():
    rendered = runner.render_prompt(
        "{{SOURCE_REPO}} #{{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{CONTEXT_FILES}}",
        {
            "SOURCE_REPO": "owner/repo",
            "ISSUE_NUMBER": "3",
            "ISSUE_TITLE": "Fix",
            "CONTEXT_FILES": "context.md",
        },
    )
    assert rendered == "owner/repo #3 Fix context.md"


def test_validate_config_accepts_existing_files(tmp_path):
    prompt = tmp_path / "prompt.md"
    skill = tmp_path / "skill.md"
    context = tmp_path / "context.md"
    for path in (prompt, skill, context):
        path.write_text("ok", encoding="utf-8")
    runner.validate_config({
        "repo_dir": tmp_path,
        "prompt": prompt,
        "skills": [skill],
        "context_files": [context],
    })


def test_validate_config_fails_before_issue_claim_when_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.md"):
        runner.validate_config({
            "repo_dir": tmp_path,
            "prompt": tmp_path / "missing.md",
            "skills": [],
            "context_files": [],
        })


def test_validate_config_rejects_missing_repo_dir(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing-repo"):
        runner.validate_config({
            "repo_dir": tmp_path / "missing-repo",
            "prompt": tmp_path / "prompt.md",
            "skills": [],
            "context_files": [],
        })


def test_run_command_returns_stdout(monkeypatch, tmp_path):
    completed = Mock(stdout=" output \n", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    assert runner.run_command(["git", "status"], cwd=tmp_path) == "output"
    runner.subprocess.run.assert_called_once_with(
        ["git", "status"], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=None,
    )


def test_run_command_logs_stderr(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="ok", stderr="warning\n")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["gh", "--version"], cwd=tmp_path) == "ok"
    assert "stderr=warning" in caplog.text


def test_run_command_can_log_success_stdout(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="agent output\n", stderr="")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["pi"], cwd=tmp_path, log_stdout=True) == "agent output"
    assert "stdout=agent output" in caplog.text


def test_run_command_logs_called_process_error_and_reraises(tmp_path, caplog):
    error = subprocess.CalledProcessError(
        2, ["gh", "issue", "list"], output="out", stderr="bad",
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
            runner.run_command(["gh", "issue", "list"], cwd=tmp_path)
    assert "command_failed returncode=2 stdout=out stderr=bad" in caplog.text


def test_run_command_logs_spawn_error_and_reraises(tmp_path, caplog):
    error = FileNotFoundError("pi")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(FileNotFoundError):
            runner.run_command(["pi"], cwd=tmp_path)
    assert "command_spawn_failed error=pi" in caplog.text


def test_run_command_logs_optional_timeout_and_reraises(tmp_path, caplog):
    error = subprocess.TimeoutExpired(["command"], 7, output="partial", stderr="wait")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.TimeoutExpired):
            runner.run_command(["command"], cwd=tmp_path, timeout=7)
    assert "command_timeout timeout=7 stdout=partial stderr=wait" in caplog.text


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


def test_pick_next_issue_returns_first_ready_source(monkeypatch):
    issue = {"number": 1, "title": "pilot", "body": ""}
    calls = []

    def pick(repo):
        calls.append(repo)
        return issue if repo == "xqliu/muyan-ceo" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) == (
        "xqliu/muyan-ceo", issue,
    )
    assert calls == ["xqliu/muyan-ceo"]


def test_pick_next_issue_falls_through_to_second_source(monkeypatch):
    issue = {"number": 2, "title": "pilot", "body": ""}
    calls = []

    def pick(repo):
        calls.append(repo)
        return issue if repo == "xqliu/muyan-pilot" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) == (
        "xqliu/muyan-pilot", issue,
    )
    assert calls == ["xqliu/muyan-ceo", "xqliu/muyan-pilot"]


def test_pick_next_issue_returns_none_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(runner, "pick_issue", lambda repo: None)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) is None


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
    monkeypatch.setattr(runner, "worktree_path", lambda repo, number: existing)
    with pytest.raises(RuntimeError, match="worktree path already exists"):
        runner.create_worktree(tmp_path, "owner/repo", 3)


def test_create_worktree_runs_git_add(monkeypatch, tmp_path):
    path = tmp_path / "issue-3"
    monkeypatch.setattr(runner, "worktree_path", lambda repo, number: path)
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append((command, kwargs)))
    assert runner.create_worktree(tmp_path, "owner/repo", 3) == path
    assert calls == [(
        ["git", "worktree", "add", "-b", "muyan-pilot/owner-repo-issue-3", str(path), "HEAD"],
        {"cwd": tmp_path},
    )]


def test_worktree_path_uses_temp_directory(monkeypatch):
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: "/tmp")
    assert runner.worktree_path("owner/repo", 3) == Path("/tmp/muyan-pilot-owner-repo-issue-3")


def test_task_branch_includes_source_repo_to_avoid_same_number_collision():
    assert runner.task_branch("owner/pilot", 1) == "muyan-pilot/owner-pilot-issue-1"
    assert runner.task_branch("owner/pilot", 1) != runner.task_branch("owner/ceo", 1)


def test_comment_issue_runs_gh_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.comment_issue(3, repo="xqliu/muyan-ceo", body="done")
    assert calls == [[
        "gh", "issue", "comment", "3", "--repo", "xqliu/muyan-ceo",
        "--body", "done",
    ]]


class FakePiProcess:
    """Stand-in for subprocess.Popen with scripted poll() results."""

    def __init__(self, command, cwd, **kwargs):
        self.command = command
        self.cwd = cwd
        self.kwargs = kwargs
        self.polls = list(getattr(type(self), "polls", [0]))
        self.stdout = FakeStdout(getattr(type(self), "output", ""))
        self.killed = False
        self.waited = False
        self._final = None

    def poll(self):
        if len(self.polls) > 1:
            self.polls.pop(0)
            return None
        self._final = self.polls.pop(0)
        return self._final

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True
        if self._final is None:
            self.poll()
        return self._final


class FakeStdout:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _write_session_file(worktree, *lines):
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _session_line(**overrides):
    data = {
        "type": "session", "version": 3, "id": "sess-1",
        "timestamp": "2026-08-24T17:55:32.139Z",
        "cwd": str(Path("/tmp")),
    }
    data.update(overrides)
    return json.dumps(data)


def _assistant_line(command, timestamp="2026-08-24T17:56:01.728Z"):
    return json.dumps({
        "type": "message", "id": "m1", "parentId": None, "timestamp": timestamp,
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c1", "name": "bash",
             "arguments": {"command": command}},
        ]},
    })


def test_run_pi_spawns_pi_and_streams_live_activity(monkeypatch, tmp_path, caplog):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{SOURCE_REPO}} {{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{ISSUE_BODY}} "
        "{{WORKSPACE_ROOT}} {{CONTEXT_FILES}} {{SKILLS}}",
        encoding="utf-8",
    )
    created = {}

    class PopenSpy(FakePiProcess):
        def __init__(self, command, cwd, **kwargs):
            super().__init__(command, cwd, **kwargs)
            created["process"] = self
            _write_session_file(
                cwd,
                _session_line(),
                _assistant_line("pytest tests/ -q"),
            )

    PopenSpy.polls = [None, 0]
    PopenSpy.output = "final output\n"
    monkeypatch.setattr(runner.subprocess, "Popen", PopenSpy)
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = {
        "prompt": prompt_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [tmp_path / "context.md"],
        "skills": [tmp_path / "skill.md"],
    }
    with caplog.at_level("INFO"):
        assert runner.run_pi(issue, tmp_path, config, "owner/repo") == "final output"
    process = created["process"]
    assert process.command[:4] == ["pi", "--skill", str(tmp_path / "skill.md"), "--print"]
    assert "owner/repo" in process.command[7]
    assert " 4 " in process.command[7]
    assert "Fix title" in process.command[7]
    assert "Fix body" in process.command[7]
    assert str(tmp_path / "context.md") in process.command[7]
    assert str(tmp_path / "skill.md") in process.command[7]
    assert process.command[8] == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert process.cwd == tmp_path
    assert process.kwargs == {
        "stdout": runner.subprocess.PIPE, "stderr": runner.subprocess.STDOUT,
        "text": True,
    }
    activity = [line for line in caplog.text.splitlines() if "pi_activity" in line]
    assert len(activity) == 2
    assert "phase=session_start" in activity[0]
    assert "phase=test" in activity[1]
    assert "summary=pytest tests/ -q" in activity[1]
    assert "issue=4" in activity[1]
    assert "source_repo=owner/repo" in activity[1]
    assert "branch=muyan-pilot/owner-repo-issue-4" in activity[1]
    assert "worktree=" + str(tmp_path) in activity[1]
    assert "session=session.jsonl" in activity[1]
    finished = [line for line in caplog.text.splitlines() if "pi_finished" in line]
    assert len(finished) == 1
    assert "returncode=0" in finished[0]
    assert "last_activity=test" in finished[0]
    assert "last_at=2026-08-24T17:56:01.728Z" in finished[0]


def test_run_pi_invokes_pi_with_rendered_prompt_and_issue_context(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")

    class PopenSpy(FakePiProcess):
        pass

    PopenSpy.polls = [0]
    PopenSpy.output = ""
    monkeypatch.setattr(runner.subprocess, "Popen", PopenSpy)
    created = {}
    original_init = PopenSpy.__init__

    def spy_init(self, command, cwd, **kwargs):
        original_init(self, command, cwd, **kwargs)
        created["command"] = command

    PopenSpy.__init__ = spy_init
    runner.run_pi(
        {"number": 5, "title": "secret", "body": "token"}, tmp_path,
        {"prompt": prompt_path, "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": []},
        "owner/repo",
    )
    command = created["command"]
    assert "PRIVATE SYSTEM" in command[5]
    assert "token" in command[5]


def test_run_pi_raises_called_process_error_on_nonzero_exit(monkeypatch, tmp_path, caplog):
    class PopenSpy(FakePiProcess):
        pass

    PopenSpy.polls = [None, 3]
    PopenSpy.output = "boom\n"
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setattr(runner.subprocess, "Popen", PopenSpy)
    with caplog.at_level("INFO"), pytest.raises(
        subprocess.CalledProcessError,
    ) as excinfo:
        runner.run_pi(
            {"number": 6, "title": "Fail", "body": ""}, tmp_path,
            {"prompt": tmp_path / "prompt.md", "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": []},
            "owner/repo",
        )
    assert excinfo.value.returncode == 3
    assert "boom" in (excinfo.value.output or "")
    assert "pi_finished" in caplog.text
    assert "returncode=3" in caplog.text
    assert "pi_failed" in caplog.text


def test_run_pi_kills_process_when_watcher_fails(monkeypatch, tmp_path):
    class PopenSpy(FakePiProcess):
        pass

    PopenSpy.polls = [None, 0]
    PopenSpy.output = ""
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setattr(runner.subprocess, "Popen", PopenSpy)
    created = {}
    original_init = PopenSpy.__init__

    def spy_init(self, command, cwd, **kwargs):
        original_init(self, command, cwd, **kwargs)
        created["process"] = self

    PopenSpy.__init__ = spy_init
    monkeypatch.setattr(
        runner, "session_file_for",
        lambda worktree, started: (_ for _ in ()).throw(RuntimeError("watch failed")),
    )
    with pytest.raises(RuntimeError, match="watch failed"):
        runner.run_pi(
            {"number": 7, "title": "Fail", "body": ""}, tmp_path,
            {"prompt": tmp_path / "prompt.md", "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": []},
            "owner/repo",
        )
    process = created["process"]
    assert process.killed is True
    assert process.waited is True


def test_run_pi_raises_timeout_when_pi_runs_too_long(monkeypatch, tmp_path, caplog):
    class PopenSpy(FakePiProcess):
        pass

    PopenSpy.polls = [None, None, 0]
    PopenSpy.output = ""
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setattr(runner.subprocess, "Popen", PopenSpy)
    created = {}
    original_init = PopenSpy.__init__

    def spy_init(self, command, cwd, **kwargs):
        original_init(self, command, cwd, **kwargs)
        created["process"] = self

    PopenSpy.__init__ = spy_init
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        runner.time, "sleep",
        lambda seconds: clock.update(now=clock["now"] + seconds + 30.0),
    )
    with caplog.at_level("ERROR"), pytest.raises(subprocess.TimeoutExpired):
        runner.run_pi(
            {"number": 8, "title": "Slow", "body": ""}, tmp_path,
            {"prompt": tmp_path / "prompt.md", "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": []},
            "owner/repo",
            timeout=50,
        )
    clock["now"] = clock["now"]  # no-op to keep linters quiet
    assert created["process"].killed is True
    assert "pi_timeout" in caplog.text


def test_watch_pi_session_logs_each_new_event_once(monkeypatch, tmp_path, caplog):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    session_file = session_dir / "session.jsonl"
    session_file.write_text(_session_line() + "\n", encoding="utf-8")
    process = FakePiProcess([], None)
    process.polls = [None, 0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])

    def fake_sleep(seconds):
        # A new event appears between polls and must be picked up next poll.
        session_file.write_text(
            session_file.read_text(encoding="utf-8")
            + _assistant_line("pytest tests/ -q") + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    phases = []
    with caplog.at_level("INFO"):
        assert runner.watch_pi_session(
            process, worktree=tmp_path, issue={"number": 9, "title": "t", "body": ""},
            source_repo="owner/repo", branch="b", started=900.0,
            command=["pi"], poll_interval=1.0, stall_timeout=300.0,
            on_phase=lambda event: phases.append(event["phase"]),
        ) == 0
    activities = [line for line in caplog.text.splitlines() if "pi_activity" in line]
    assert len(activities) == 2
    assert "phase=session_start" in activities[0]
    assert "phase=test" in activities[1]
    assert phases == ["session_start", "test"]


def test_watch_pi_session_invokes_on_phase_for_every_new_event(monkeypatch, tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    session_file = session_dir / "session.jsonl"
    session_file.write_text(
        _session_line() + "\n"
        + _assistant_line("pytest tests/ -q") + "\n"
        + _assistant_line("gh pr create --title x", timestamp="2026-08-24T17:57:00.000Z") + "\n",
        encoding="utf-8",
    )
    process = FakePiProcess([], None)
    process.polls = [0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    seen = []
    assert runner.watch_pi_session(
        process, worktree=tmp_path, issue={"number": 10, "title": "t", "body": ""},
        source_repo="owner/repo", branch="b", started=900.0,
        command=["pi"], poll_interval=1.0, stall_timeout=300.0,
        on_phase=seen.append,
    ) == 0
    assert [event["phase"] for event in seen] == ["session_start", "test", "pr"]
    assert seen[2]["summary"] == "gh pr create --title x"


def test_watch_pi_session_warns_when_no_new_activity(monkeypatch, tmp_path, caplog):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    session_file = session_dir / "session.jsonl"
    session_file.write_text(_session_line() + "\n", encoding="utf-8")
    process = FakePiProcess([], None)
    process.polls = [None, 0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        runner.time, "sleep",
        lambda seconds: clock.update(now=clock["now"] + seconds + 300.0),
    )
    with caplog.at_level("WARNING"):
        assert runner.watch_pi_session(
            process, worktree=tmp_path, issue={"number": 11, "title": "t", "body": ""},
            source_repo="owner/repo", branch="b", started=900.0,
            command=["pi"], poll_interval=1.0, stall_timeout=300.0,
        ) == 0
    stalled = [line for line in caplog.text.splitlines() if "pi_stalled" in line]
    assert len(stalled) == 1
    assert "idle_seconds=301" in stalled[0]
    assert "last_activity=session_start" in stalled[0]
    assert "session=session.jsonl" in stalled[0]


def test_watch_pi_session_reports_scene_when_no_session_file(monkeypatch, tmp_path, caplog):
    process = FakePiProcess([], None)
    process.polls = [0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    with caplog.at_level("INFO"):
        assert runner.watch_pi_session(
            process, worktree=tmp_path, issue={"number": 12, "title": "t", "body": ""},
            source_repo="owner/repo", branch="b", started=900.0,
            command=["pi"], poll_interval=1.0, stall_timeout=300.0,
        ) == 0
    finished = [line for line in caplog.text.splitlines() if "pi_finished" in line]
    assert len(finished) == 1
    assert "session=-" in finished[0]
    assert "last_activity=none" in finished[0]


def test_watch_pi_session_raises_timeout_and_kills_process(monkeypatch, tmp_path, caplog):
    process = FakePiProcess([], None)
    process.polls = [None, None, None, None]
    clock = {"now": 1000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["now"])
    monkeypatch.setattr(
        runner.time, "sleep",
        lambda seconds: clock.update(now=clock["now"] + seconds + 30.0),
    )
    with caplog.at_level("ERROR"), pytest.raises(subprocess.TimeoutExpired):
        runner.watch_pi_session(
            process, worktree=tmp_path, issue={"number": 13, "title": "t", "body": ""},
            source_repo="owner/repo", branch="b", started=900.0,
            command=["pi"], poll_interval=1.0, stall_timeout=300.0, timeout=50,
        )
    assert process.killed is True
    assert "pi_timeout" in caplog.text


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
    config = {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md"}
    assert runner.process_issue(issue, config, "xqliu/muyan-ceo") == "https://github.com/muyantech/muyan-pilot/pull/4"
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
        runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md"}, "xqliu/muyan-ceo")
    assert calls[1][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-blocked", "remove": "ai-in-progress"}
    assert calls[2][0] == "comment"


def test_process_issue_comments_once_when_pr_phase_reached(monkeypatch, tmp_path):
    calls = []

    def fake_run_pi(issue, worktree, config, source_repo, timeout=None, on_phase=None):
        calls.append(("run_pi", on_phase))
        if on_phase is not None:
            on_phase({"phase": "test", "summary": "pytest", "timestamp": "t1"})
            on_phase({"phase": "pr", "summary": "gh pr create --title x",
                      "timestamp": "2026-08-24T17:57:00.000Z"})
            on_phase({"phase": "pr", "summary": "gh pr list",
                      "timestamp": "2026-08-24T17:57:01.000Z"})
        return "done"

    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", kwargs)))
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    monkeypatch.setattr(runner, "verify_pr", lambda *args: "https://github.com/x/y/pull/4")
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", kwargs)))
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    assert runner.process_issue(issue, {"repo_dir": tmp_path}, "xqliu/muyan-ceo") == "https://github.com/x/y/pull/4"
    comments = [call for call in calls if call[0] == "comment"]
    assert len(comments) == 2
    milestone = comments[0][1]["body"]
    assert "pr" in milestone
    assert "gh pr create --title x" in milestone
    assert "2026-08-24T17:57:00.000Z" in milestone
    assert comments[1][1]["body"] == "Muyan Pilot opened PR: https://github.com/x/y/pull/4"


def test_process_issue_milestone_comment_failure_does_not_block_success(monkeypatch, tmp_path, caplog):
    calls = []

    def fake_run_pi(issue, worktree, config, source_repo, timeout=None, on_phase=None):
        if on_phase is not None:
            on_phase({"phase": "pr", "summary": "gh pr create --title x",
                      "timestamp": "t"})
        return "done"

    def fake_comment(number, *, repo, body):
        calls.append(body)
        if "key phase" in body:
            raise RuntimeError("github down")

    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    monkeypatch.setattr(runner, "verify_pr", lambda *args: "https://github.com/x/y/pull/4")
    monkeypatch.setattr(runner, "comment_issue", fake_comment)
    with caplog.at_level("ERROR"):
        assert runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path}, "xqliu/muyan-ceo",
        ) == "https://github.com/x/y/pull/4"
    assert "milestone comment failed" in caplog.text
    assert calls[-1] == "Muyan Pilot opened PR: https://github.com/x/y/pull/4"


def test_process_issue_preserves_original_failure_when_reporting_fails(monkeypatch, tmp_path, caplog):
    edit_calls = []

    def edit(*args, **kwargs):
        edit_calls.append(kwargs)
        if len(edit_calls) == 2:
            raise RuntimeError("github report failed")

    monkeypatch.setattr(runner, "edit_issue", edit)
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="git failed"):
        runner.process_issue({"number": 13, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md"}, "xqliu/muyan-ceo")
    assert "failure reporting failed" in caplog.text


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: None)
    config = tmp_path / "muyan-pilot.toml"
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    assert runner.main(["--config", str(config)]) == 0


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: ("xqliu/muyan-pilot", issue))
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or "https://github.com/x/y/pull/12")
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue


def test_main_accepts_repeated_source_repo(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    seen = []
    issue = {"number": 14, "title": "task"}
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\", \"xqliu/muyan-ceo\"]\n", encoding="utf-8")
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: seen.append(repos) or (repos[0], issue))
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: "https://github.com/x/y/pull/14")
    assert runner.main([
        "--config", str(config),
    ]) == 0
    assert seen == [["xqliu/muyan-pilot", "xqliu/muyan-ceo"]]


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.main(["--config", str(config)])
