import time
from pathlib import Path
import pytest
import orbi.runner as runner
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions


def _config(tmp_path: Path, **kwargs):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system", encoding="utf-8")
    return runner.RunnerConfig(prompt=prompt, repo_dir=tmp_path,
        source_repos=("owner/repo",), workspace_root=tmp_path,
        context_files=(), skills=(), base_branch="main", base_sha="abc123def456",
        run_id="run1", **kwargs)


def test_run_pi_steering_callback_filters_and_deduplicates(monkeypatch, tmp_path):
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    comments = [
        {"id": "old", "createdAt": "2000-01-01T00:00:00Z", "authorAssociation": "MEMBER", "author": {"login": "old"}, "body": "old"},
        {"id": "bad", "createdAt": future, "authorAssociation": "NONE", "author": {"login": "bad"}, "body": "ignore"},
        {"id": "new", "createdAt": future, "authorAssociation": "MEMBER", "author": {"login": "alice"}, "body": "correct this"},
    ]
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: comments)
    monkeypatch.setitem(runner.__dict__, "_comment_is_trusted", lambda c: c["id"] == "new")
    monkeypatch.setitem(runner.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(runner.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(runner.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert runner.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    request = captured["watch"].steering_check()
    assert request.comment_ids == ("new",)
    assert "alice: correct this" in request.context
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_poll_failure_is_bypassed(monkeypatch, tmp_path):
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    captured = {}
    monkeypatch.setitem(runner.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert runner.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    assert captured["watch"].steering_check() is None


def test_stream_pi_steers_and_restarts(tmp_path):
    from tests.test_bootstrap_runner import make_fake_pi
    from orbi.pi_process import SteeringRequest
    calls = []
    command = make_fake_pi(tmp_path, session_records=[], stdout="ok", sleep=0.2)
    command.append("initial context")
    def check():
        calls.append(1)
        if len(calls) == 1:
            return SteeringRequest("corrected", ("c1",), "alice")
        if len(calls) == 2:
            raise RuntimeError("offline")
        return None
    result = runner.stream_pi(command, ctx=RunContext("deadbeef", 24, "branch", tmp_path, "owner/repo"),
        watch=PiWatchOptions(poll_interval=0.01, steering_poll_seconds=0.01, steering_check=check), cwd=tmp_path)
    assert result == "ok"
    assert len(calls) >= 2


def test_steering_validation_and_limit(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="positive finite"):
        runner._positive_seconds({"x": "bad"}, "x", 1.0)
    with pytest.raises(ValueError, match="positive finite"):
        runner._positive_seconds({"x": 0}, "x", 1.0)
    assert runner._positive_seconds({}, "x", 2.0) == 2.0
    config = _config(tmp_path, steering_max_rounds=0)
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: [])
    captured = {}
    monkeypatch.setitem(runner.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 3, "title": "t", "body": "b"}
    runner.run_pi(issue, RunContext("run1", 3, "branch", tmp_path, "owner/repo"), config)
    assert captured["watch"].steering_check() is None
    assert captured["watch"].steering_check() is None


def test_load_config_rejects_invalid_steering_values(tmp_path):
    path = tmp_path / "orbi.toml"
    path.write_text('source_repos = ["owner/repo"]\nsteering_enabled = "yes"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="steering_enabled"):
        runner.load_config(path)
    path.write_text('source_repos = ["owner/repo"]\nsteering_max_rounds = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match="steering_max_rounds"):
        runner.load_config(path)
