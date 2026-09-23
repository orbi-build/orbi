from orbi import config as config_domain
import time
from pathlib import Path
import pytest
import orbi.runner as runner
import orbi.pi_session as pi_session
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions


def _config(tmp_path: Path, **kwargs):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system", encoding="utf-8")
    return config_domain.RunnerConfig(prompt=prompt, repo_dir=tmp_path,
        source_repos=("owner/repo",), workspace_root=tmp_path,
        context_files=(), skills=(), base_branch="main", base_sha="abc123def456",
        run_id="run1", **kwargs)


def _snapshot(comments, body="body"):
    """The widened per-poll payload the steering check reads (Issue #1094):
    the same `gh issue view` that lists the comments also returns the body."""
    snapshot = {"comments": comments}
    if body is not None:
        snapshot["body"] = body
    return snapshot


def test_run_pi_steering_callback_filters_and_deduplicates(monkeypatch, tmp_path):
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    comments = [
        {"id": "old", "createdAt": "2000-01-01T00:00:00Z", "authorAssociation": "MEMBER", "author": {"login": "old"}, "body": "old"},
        {"id": "bad", "createdAt": future, "authorAssociation": "NONE", "author": {"login": "bad"}, "body": "ignore"},
        {"id": "new", "createdAt": future, "authorAssociation": "MEMBER", "author": {"login": "alice"}, "body": "correct this"},
    ]
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot(comments))
    monkeypatch.setitem(pi_session.__dict__, "_comment_is_trusted", lambda c: c["id"] == "new")
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    request = captured["watch"].steering_check()
    assert request.comment_ids == ("new",)
    assert "alice: correct this" in request.context
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_poll_failure_is_bypassed(monkeypatch, tmp_path):
    monkeypatch.setitem(pi_session.__dict__, "issue_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
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
    result = pi_session.stream_pi(command, ctx=RunContext("deadbeef", 24, "branch", tmp_path, "owner/repo"),
        watch=PiWatchOptions(poll_interval=0.01, steering_poll_seconds=0.01, steering_check=check), cwd=tmp_path)
    assert result == "ok"
    assert len(calls) >= 2


def test_steering_validation_and_limit(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="must be a number"):
        config_domain._positive_seconds({"x": "bad"}, "x", 1.0)
    with pytest.raises(ValueError, match="must be a positive number of seconds"):
        config_domain._positive_seconds({"x": 0}, "x", 1.0)
    assert config_domain._positive_seconds({}, "x", 2.0) == 2.0
    config = _config(tmp_path, steering_max_rounds=0)
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot([]))
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 3, "title": "t", "body": "b"}
    pi_session.run_pi(issue, RunContext("run1", 3, "branch", tmp_path, "owner/repo"), config)
    assert captured["watch"].steering_check() is None
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_body_edit_restarts(monkeypatch, tmp_path):
    """A body edit between claim and poll steers exactly like a new
    comment (Issue #1094): the request carries the 「正文已更新」 header and
    the edited text, and the SAME edited body on the next poll is inert —
    the recorded body became the edited one."""
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot([], body="edited body"))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "original body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    request = captured["watch"].steering_check()
    assert request is not None
    assert request.comment_ids == ()
    assert request.body_revision is True
    assert "正文已更新" in request.context
    assert "edited body" in request.context
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_unchanged_body_stays_silent(monkeypatch, tmp_path):
    """Body unchanged and no new comments: no request, no restart
    (today's behaviour, Issue #1094)."""
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot([]))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_body_edit_and_comment_is_one_request(monkeypatch, tmp_path):
    """A body edit and a trusted new comment in the same poll produce ONE
    request carrying both — one restart, never two (Issue #1094)."""
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    comments = [
        {"id": "new", "createdAt": future, "authorAssociation": "MEMBER", "author": {"login": "alice"}, "body": "and this"},
    ]
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot(comments, body="edited body"))
    monkeypatch.setitem(pi_session.__dict__, "_comment_is_trusted", lambda c: c["id"] == "new")
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "original body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    request = captured["watch"].steering_check()
    assert request is not None
    assert request.comment_ids == ("new",)
    assert request.body_revision is True
    assert "正文已更新" in request.context
    assert "edited body" in request.context
    assert "alice: and this" in request.context
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_body_edit_respects_round_limit(monkeypatch, tmp_path):
    """At `steering_max_rounds` a body edit is ignored with the existing
    `steering_limit_reached` event — no restart (Issue #1094)."""
    polls = iter([
        _snapshot([], body="first edit"),
        _snapshot([], body="second edit"),
        _snapshot([], body="second edit"),
    ])
    monkeypatch.setitem(pi_session.__dict__, "issue_view", lambda *a, **k: next(polls))
    events = []
    monkeypatch.setitem(pi_session.__dict__, "event", lambda kind, **kw: events.append(kind))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "original body"}
    config = _config(tmp_path, steering_max_rounds=1)
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), config) == "done"
    check = captured["watch"].steering_check
    first = check()
    assert first is not None and first.body_revision is True
    assert check() is None
    assert "steering_limit_reached" in events


def test_run_pi_steering_limit_notifies_once_for_late_comment(monkeypatch, tmp_path):
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    polls = iter([
        _snapshot([{
            "id": "late-1", "createdAt": future,
            "authorAssociation": "MEMBER", "author": {"login": "alice"},
            "body": "please use the new direction",
        }]),
        _snapshot([{
            "id": "late-1", "createdAt": future,
            "authorAssociation": "MEMBER", "author": {"login": "alice"},
            "body": "please use the new direction",
        }, {
            "id": "late-2", "createdAt": future,
            "authorAssociation": "MEMBER", "author": {"login": "bob"},
            "body": "another correction",
        }]),
    ])
    monkeypatch.setitem(pi_session.__dict__, "issue_view", lambda *a, **k: next(polls))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    comments = []
    monkeypatch.setitem(pi_session.__dict__, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)))
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    config = _config(tmp_path, steering_max_rounds=0)
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), config) == "done"
    check = captured["watch"].steering_check
    assert check() is None
    assert check() is None
    assert len(comments) == 1
    assert "<!-- orbi:run=run1 -->" in comments[0][2]
    assert "steering limit" in comments[0][2]
    assert "not applied" in comments[0][2]
    assert "run_id=run1" in comments[0][2]


def test_run_pi_steering_limit_notice_failure_is_bypassed(monkeypatch, tmp_path):
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    monkeypatch.setitem(pi_session.__dict__, "issue_view", lambda *a, **k: _snapshot([{
        "id": "late", "createdAt": future, "authorAssociation": "MEMBER",
        "author": {"login": "alice"}, "body": "correction",
    }]))
    events = []
    monkeypatch.setitem(pi_session.__dict__, "event", lambda kind, **kw: events.append(kind))
    monkeypatch.setitem(pi_session.__dict__, "comment_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    config = _config(tmp_path, steering_max_rounds=0)
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), config) == "done"
    assert captured["watch"].steering_check() is None
    assert "steering_limit_notice_failed" in events


def test_run_pi_steering_snapshot_without_body_stays_inert(monkeypatch, tmp_path):
    """A payload without a body field cannot prove a body edit: body
    steering stays inert for that poll, comment steering unchanged."""
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: _snapshot([], body=None))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    assert captured["watch"].steering_check() is None


def test_run_pi_steering_malformed_payload_is_bypassed(monkeypatch, tmp_path):
    """A payload whose comments field is not an array fails the poll the
    same way a fetch error does: the steering_poll_failed bypass, never
    a restart (Issue #1094 keeps #1002's bypass shape)."""
    events = []
    monkeypatch.setitem(pi_session.__dict__, "issue_view",
        lambda *a, **k: {"comments": None, "body": "body"})
    monkeypatch.setitem(pi_session.__dict__, "event", lambda kind, **kw: events.append(kind))
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    captured = {}
    monkeypatch.setitem(pi_session.__dict__, "stream_pi", lambda command, **kw: captured.update(kw) or "done")
    issue = {"number": 2, "title": "title", "body": "body"}
    assert pi_session.run_pi(issue, RunContext("run1", 2, "branch", tmp_path, "owner/repo"), _config(tmp_path)) == "done"
    assert captured["watch"].steering_check() is None
    assert "steering_poll_failed" in events


def test_resume_context_renders_body_revision(tmp_path, monkeypatch):
    """The body revision renders under the 「正文已更新」 header with the full
    edited body; a comment rides the same resume context under its own
    「新评论」 header (Issue #1094)."""
    from seam import seam
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setitem(pi_session.__dict__, "activity_snapshot", lambda *a, **k: None)
    context = pi_session.resume_context(
        tmp_path, body_revision={"issue": 9, "body": "new direction"},
    )
    assert context is not None
    assert "正文已更新" in context
    assert "Issue #9" in context
    assert "new direction" in context
    combined = pi_session.resume_context(
        tmp_path,
        [{"issue": 9, "author": {"login": "alice"}, "body": "note"}],
        body_revision={"issue": 9, "body": "new direction"},
    )
    assert "正文已更新" in combined
    assert "新评论" in combined
    assert "new direction" in combined
    assert "alice: note" in combined


def test_load_config_rejects_invalid_steering_values(tmp_path):
    path = tmp_path / "orbi.toml"
    path.write_text('source_repos = ["owner/repo"]\nsteering_enabled = "yes"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="steering_enabled"):
        config_domain.load_config(path)
    path.write_text('source_repos = ["owner/repo"]\nsteering_max_rounds = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match="steering_max_rounds"):
        config_domain.load_config(path)
