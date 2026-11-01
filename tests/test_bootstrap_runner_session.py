"""Live session scene: progress, model_wait and the stop handler (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: live GitHub progress while Pi runs
(the throttle, callback errors), the configured `model_wait_dead_seconds`
reaching `stream_pi`, the SIGTERM stop handler (`run_stopping`/`run_stopped`
ordering and fields, the real subprocess stop) and the shutdown of a Pi
child that ignores SIGTERM.
"""

from orbi import config as config_domain
import json

import os

import signal
import subprocess
import sys

import time
from pathlib import Path
from unittest.mock import Mock
import orbi.claim as claim

import orbi.runner as runner
import orbi.pi_session as pi_session
from orbi import progress
from tests.fakes.bootstrap_pi import (
    fake_session_records, fresh_timestamp, make_fake_pi,
)
import orbi.journal as journal
from seam import seam
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions


# --- live GitHub progress while Pi runs (Issue #18, review Major fix) ------


def test_stream_pi_invokes_progress_callback_while_child_is_running(
    tmp_path,
):
    """The review Major fix: the progress callback (which PATCHes the
    GitHub comment) must fire WHILE the Pi child is still running — on
    activity changes and on heartbeats — not only after it exits."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="final answer",
        sleep=1.2,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The callback fired many times during the run (polls at 0.1 s while
    # the child sleeps 1.2 s after the last session record)...
    assert len(seen) >= 4
    # ...and it saw the activity change (starting -> bash) live, before
    # the child exited: the tool call is visible in an early snapshot,
    # not only in a final state.
    # Issue #176: the live snapshots show the startup sub-phases
    # (request_pending: the session exists, the first response has not
    # arrived yet), not the generic `starting`.
    # Issue #741: the FIRST frame is load-dependent — on a busy machine
    # the first poll can predate the session file, so that frame is
    # `session_pending` (equally legal, pinned by the dedicated
    # regression test). This test pins the ORDER: request_pending must
    # appear, and before the tool phase.
    phases = [entry["phase"] for entry in seen]
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")
    assert phases.index("bash") < len(phases) - 1
    tool_entries = [entry for entry in seen if entry["phase"] == "bash"]
    assert any(
        entry["action"] == "bash pytest tests/"
        for entry in tool_entries
    )


def test_stream_pi_progress_first_frame_may_be_session_pending(tmp_path):
    """Issue #741 regression: on a busy machine the first progress frame
    fires BEFORE Pi creates its session file, so the startup sub-phase
    of that frame is `session_pending` — as legal as `request_pending`
    (Issue #176). The session record below is delayed by 1.0 s, so the
    forced scene is the exact #741 flake: the first frames predate the
    session file."""
    (_, session), (_, user), (_, assistant) = fake_session_records()
    command = make_fake_pi(
        tmp_path,
        session_records=[(1.0, session), (0.1, user), (0.1, assistant)],
        stdout="final answer",
        sleep=0.6,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The forced #741 scene: the first frame predates the session file,
    # so it is the `session_pending` startup sub-phase — never a reason
    # to fail (Issue #741).
    phases = [entry["phase"] for entry in seen]
    assert phases[0] == "session_pending"
    # The order contract (Issue #176): the startup sub-phases stay
    # visible and ordered — request_pending appears, before the tool
    # phase — regardless of which frame the sampling started in.
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")


def test_stream_pi_live_progress_patches_github_before_child_exits(
    tmp_path,
):
    """End-to-end shape of the fix: a publisher-style callback PATCHes
    the run-marker comment while the child is still running, so a mobile
    user never sees a static starting comment for the whole run."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="done",
        sleep=1.2,
    )
    patches = []

    class FakePublisher:
        language_code = "en"
        comment_id = 77

        def patch(self, body):
            patches.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="owner/repo"), publisher, title="Live progress", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=throttle)
    assert result == "done"
    # At least one PATCH happened while the child was still running
    # (all of them did: the callback only exists inside the poll loop).
    assert len(patches) >= 2
    # The live comment carries the run marker and the live phase.
    assert all(
        body.startswith("<!-- orbi:run=deadbeef -->")
        for body in patches
    )
    assert any("- phase: bash" in body for body in patches)
    # Issue #1657: the live body carries the versioned summary block and
    # the safe activity; the raw bash command reaches neither.
    assert all(body.count("<!-- orbi:progress:v1 ") == 1 for body in patches)
    assert any("- activity: command" in body for body in patches)
    assert not any("pytest tests/" in body for body in patches)


def test_stream_pi_progress_callback_error_never_interrupts_run(
    tmp_path, caplog,
):
    """Observability is best-effort: a failing progress callback (e.g. a
    transient gh failure) is logged and never interrupts the delivery."""
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )

    def boom(activity):
        raise RuntimeError("gh api failed")

    with caplog.at_level("ERROR"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=boom)
    assert result == "final answer"
    assert "progress_publish_failed" in caplog.text
    assert "run=deadbeef" in caplog.text
    assert "role=implement" in caplog.text


def test_live_progress_throttle_patches_on_change_and_cadence():
    """The throttle passes an update through on a visible activity change
    and at most every PI_HEARTBEAT_SECONDS (30 s) otherwise."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=1, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Throttle task", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "starting", "action": None, "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
    }
    throttle(first)
    assert len(calls) == 1  # the first update always passes
    # Unchanged activity within the cadence: suppressed.
    throttle(first)
    assert len(calls) == 1
    # A visible change: passed immediately.
    changed = dict(
        first, phase="test", action="bash pytest tests/", kind="command",
    )
    throttle(changed)
    assert len(calls) == 2
    # Unchanged again within the cadence: suppressed...
    throttle(changed)
    assert len(calls) == 2
    # ...until the 30 s cadence elapses: passed as a heartbeat update.
    throttle._last_patch -= progress.PI_HEARTBEAT_SECONDS + 1
    throttle(changed)
    assert len(calls) == 3
    # The rendered body carries the live state (Issue #1657: the readable
    # activity and the block share the safe value; the raw command never
    # reaches either).
    assert "- phase: test" in calls[-1]
    assert "- activity: command" in calls[-1]
    assert "pytest tests/" not in calls[-1]
    assert calls[-1].count("<!-- orbi:progress:v1 ") == 1


def test_live_progress_throttle_patches_on_recovery_change():
    """Issue #94: the recovery state is part of the visible progress —
    entering idle recovery (None -> `term`) PATCHes the live comment
    immediately even though phase/action/result did not change, and
    the rendered body carries the `recovery` line."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=94, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Idle recovery", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "test", "action": "bash pytest tests/", "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
        "recovery": None,
    }
    throttle(first)
    assert len(calls) == 1
    assert "- recovery" not in calls[0]  # no recovery line when idle-recovery is off
    # Entering recovery: visible change -> immediate PATCH with the line.
    throttle(dict(first, recovery="term"))
    assert len(calls) == 2
    assert "- recovery: term" in calls[1]
    # Escalating to KILL: another visible change -> another PATCH.
    throttle(dict(first, recovery="kill"))
    assert len(calls) == 3
    assert "- recovery: kill" in calls[2]
    # Back to None after the resume: visible change -> PATCH without the line.
    throttle(first)
    assert len(calls) == 4
    assert "- recovery" not in calls[3]


def test_run_pi_passes_progress_callback_to_stream_pi(monkeypatch, tmp_path):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()), progress=callback)
    assert seen["progress"] is callback
    assert seen["ctx"].run_id == "a1b2c3d4"
    assert seen["ctx"].branch == "b"


def test_run_review_passes_progress_callback_to_stream_pi(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1, progress=callback)
    assert seen["progress"] is callback
    assert seen["role"] == "review"


# --- Issue #228: the configured model_wait threshold reaches stream_pi ------

def test_run_pi_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the implement session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5))
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_review_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the review session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5), 1)
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_pi_keeps_module_default_without_config_key(
    monkeypatch, tmp_path,
):
    """Issue #228: a caller config without the key (the pre-#228 shape)
    keeps the module constant — the real `load_config` always provides
    the key, this only pins the fallback."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()))
    assert seen["watch"].model_wait_dead_seconds == (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


# ---------------------------------------------------------------------------
# Issue #48: log the active Issue context when the Runner is stopped
# (SIGTERM): `run_stopping` before the stop, `run_stopped` after the Pi
# child exits, `result=idle` when no Issue was claimed yet.
# ---------------------------------------------------------------------------

def _write_session_file(worktree: Path, session_id: str = "sess-1") -> None:
    """A session JSONL with a tool call so the snapshot shows phase=bash."""
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records = [
        {"type": "session", "id": session_id,
         "timestamp": fresh_timestamp(), "cwd": str(worktree)},
        {"type": "message", "id": "a1", "timestamp": fresh_timestamp(),
         "message": {"role": "assistant", "content": [
             {"type": "toolCall", "id": "t1", "name": "bash",
              "arguments": {"command": "pytest tests/"}}]}},
    ]
    with open(session_dir / f"{session_id}.jsonl", "w",
              encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_stop_handler_idle_logs_run_stopped_idle_and_exits(
    monkeypatch, caplog, bind_run_id,
):
    # No run in flight (before the claim): the stop logs result=idle
    # WITHOUT any invented issue fields and exits 143.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id(None)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = [line for line in caplog.text.splitlines()
             if " run_stopped " in line]
    assert len(lines) == 1
    assert "result=idle" in lines[0]
    assert "issue=" not in lines[0]
    # No run was in flight: no run_stopping line either.
    assert "run_stopping" not in caplog.text


def test_stop_handler_active_run_logs_stopping_then_stopped_and_exits(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # Run in flight: run_stopping carries the full scene BEFORE the
    # stop, the live Pi child is TERMed and waited for, then
    # run_stopped result=interrupted, then the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree=str(worktree), source_repo="owner/repo"), "Log the stop scene")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    runner.set_active_pi(child)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = caplog.text.splitlines()
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # run_stopping comes BEFORE run_stopped (the child exit is in
    # between).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # No session file yet: the snapshot fields are '-' (never invented).
    assert "phase=-" in line
    assert "session=-" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # The live Pi child was shut down: no orphan survives the stop.
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_stop_handler_active_run_uses_activity_snapshot_for_scene(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # With a session file the run_stopping line carries the phase and
    # session of the existing activity snapshot (reused, not re-parsed
    # ad hoc).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    line = [line for line in caplog.text.splitlines()
            if " run_stopping " in line][0]
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert 128 + died["signum"] == 143


def test_stop_handler_active_run_without_live_child_still_stops(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # Run in flight but no live Pi child (between sessions): both lines
    # are still logged and the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    lines = caplog.text.splitlines()
    assert any(" run_stopping " in line for line in lines)
    assert any(
        line.endswith("run_stopped issue=48 result=interrupted")
        for line in lines
    )
    assert 128 + died["signum"] == 143


def test_stop_handler_snapshot_failure_still_logs_and_exits(
    monkeypatch, caplog, bind_run_id,
):
    # A failing activity snapshot never swallows the stop: the scene
    # falls back to `-` for phase/session and the stop still exits.
    monkeypatch.setattr(journal, "_ACTIVE_RUN",
        {"issue": 48, "title": "t", "branch": "b", "worktree": "/w",
         "pi": None},
    )
    bind_run_id("a1b2c3d4")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    def boom(_dir):
        raise RuntimeError("snapshot exploded")
    monkeypatch.setattr(runner, "activity_snapshot", boom)
    with caplog.at_level("INFO"):
        runner._stop_delivery(signal.SIGTERM)
    assert died["signum"] == signal.SIGTERM
    stopping = [
        line for line in caplog.text.splitlines()
        if " run_stopping " in line
    ]
    assert len(stopping) == 1
    assert "phase=-" in stopping[0]
    assert "session=-" in stopping[0]
    assert "snapshot exploded" in caplog.text


def test_stop_handler_reinstalls_default_disposition_and_raises_signal(
    monkeypatch, caplog, bind_run_id,
):
    # After the stop work the handler restores SIGTERM's default
    # disposition and re-raises the signal at itself: the process dies
    # from the ORIGINAL signal (systemd sees a signal-caused stop, exit
    # 143) and a handler crash can never swallow the stop.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id(None)
    monkeypatch.setattr(runner.os, "_exit", lambda code: None)
    raised = {}
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: raised.setdefault("args", (pid, sig)),
    )
    runner._die_from_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert raised["args"] == (os.getpid(), signal.SIGTERM)


def test_set_active_run_binds_scene_and_pi_tracking(monkeypatch):
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=7, branch="b", worktree="/w", source_repo="owner/repo"), "t")
    assert journal._ACTIVE_RUN == {
        "issue": 7, "title": "t", "branch": "b", "worktree": "/w",
        "pi": None,
    }
    child = object()
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN["pi"] is child
    runner.set_active_pi(None)
    assert journal._ACTIVE_RUN["pi"] is None
    runner.clear_active_run()
    assert journal._ACTIVE_RUN is None
    # set_active_pi without a bound run is a no-op (never crashes).
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN is None


def test_main_installs_the_stop_handler(monkeypatch, tmp_path):
    # main() installs the SIGTERM handler right after logging is set up,
    # so every phase of the tick (pre-claim, claim, implement, delivery
    # wait) stops with the scene.
    installed = {}
    monkeypatch.setattr(
        signal, "signal",
        lambda sig, handler: installed.setdefault(sig, handler),
    )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'workspace_root = ".."\nprompt = "prompt.md"\n'
        'prompt_review = "prompt_review.md"\n',
        encoding="utf-8",
    )
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        claim, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "acquire_slot", lambda *args: Mock(release=lambda: None))
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged")
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
    assert runner.main(["--config", str(config_path)]) == 0
    assert installed[signal.SIGTERM] is runner._handle_stop


# Issue #48 acceptance: a REAL subprocess SIGTERM test — the Runner
# process is a real child of the test, stopped with a real SIGTERM, and
# the journal (captured from its stderr) proves the ordering and fields.
STOP_DRIVER = """
import logging
import signal
import subprocess
import sys
import time

sys.path.insert(0, {repo!r} + "/src")
import orbi.runner as runner
import orbi.milestone as milestone
from orbi.delivery_scene import RunContext

logging.basicConfig(level=logging.INFO, format=runner.log_format())
# Exactly what main() does (Issue #48): install the stop handler first.
signal.signal(signal.SIGTERM, runner._handle_stop)
runner.set_run_id("a1b2c3d4")
runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree={worktree!r}, source_repo="owner/repo"), "Log the stop scene")
# The real Pi-like child: a real long-running process (what stream_pi
# tracks via set_active_pi). It lingers briefly on SIGTERM before
# exiting (a slow shutdown), so the stop handler's child.wait() has a
# real wait to do.
child = subprocess.Popen(
    [sys.executable, "-c",
     "import os, signal, time\\n"
     "signal.signal(signal.SIGTERM, lambda s, f: "
     "(time.sleep(0.5), os._exit(0)))\\n"
     "time.sleep(60)\\n"],
)
runner.set_active_pi(child)
print("ready child=" + str(child.pid), flush=True)
# Sit in the poll loop (like stream_pi) until stopped.
time.sleep(120)
"""


def test_shutdown_child_kills_a_child_that_ignores_sigterm(tmp_path):
    """Root cause (Issue #48): the stop handler must not block past the
    grace when the Pi child ignores SIGTERM (e.g. a model/network call).
    It TERMs, waits at most `grace`, then KILLs and reaps the child so
    the Runner exits with the original signal before systemd's own
    stop deadline — never leaving `Result=timeout`/failed."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time, sys\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "print(\"ready\", flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    # Wait until the child has INSTALLED SIG_IGN: else a
    # terminate() before the handler is set would exit the child
    # on SIGTERM and never exercise the grace/kill path.
    assert child.stdout.readline().strip() == "ready"
    # The child ignores SIGTERM: a bare terminate()+wait() would
    # block forever. `_shutdown_child` with a short grace must
    # escalate to SIGKILL and reap it within that grace.
    runner._shutdown_child(child, grace=0.5)
    assert child.poll() is not None
    # Reaped with a kill signal, not a clean SIGTERM exit.
    assert child.returncode is not None and child.returncode < 0


def test_shutdown_child_noop_for_exited_or_none_child():
    """A None or already-exited child is a no-op (no hang, no signal)."""
    runner._shutdown_child(None)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    runner._shutdown_child(child)
    assert child.poll() is not None


def test_real_subprocess_sigterm_logs_stop_scene_and_shuts_down_pi(
    tmp_path,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    driver = tmp_path / "driver.py"
    driver.write_text(
        STOP_DRIVER.format(
            repo=str(Path(__file__).resolve().parent.parent),
            worktree=str(worktree),
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = proc.stdout.readline()
    assert ready.startswith("ready child=")
    child_pid = int(ready.strip().split("=")[1])
    # The Pi child is alive before the stop.
    time.sleep(0.2)
    assert _pid_alive(child_pid)
    # The real SIGTERM that systemd would send on stop.
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    # The process exits with 128+SIGTERM = 143 (the value systemd
    # records for a signal-caused stop).
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # Ordering: run_stopping BEFORE run_stopped (the Pi child exit is
    # in between), and both BEFORE the process termination (they are in
    # the captured stderr of the already-exited process).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    # The [run_id] prefix (the existing run correlation, reused); the
    # journal format is `LEVEL message`.
    assert stopping[0].startswith("INFO [a1b2c3d4] run_stopping ")
    assert stopped[0].startswith("INFO [a1b2c3d4] run_stopped ")
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # phase/session from the existing activity snapshot of the
    # worktree's .pi-session.
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # No orphan Pi: the stop handler waited for the child to exit
    # BEFORE the process exited (driver exit implies the child was
    # reaped), so the child must be gone now.
    assert not _pid_alive(child_pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user: alive.
        return True
    return True


def test_pid_alive_reports_zombie_and_foreign_pid(monkeypatch):
    # A killed-but-unreaped child is a zombie: os.kill(pid, 0) succeeds
    # on it, so it still counts as alive (the stop handler's
    # child.poll() check is what distinguishes a zombie).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    child.kill()
    time.sleep(0.2)
    assert _pid_alive(child.pid) is True
    child.wait(timeout=5)
    # A pid that no longer exists (reaped).
    assert _pid_alive(child.pid) is False
    # A foreign pid (owned by another user) reports alive via
    # PermissionError — exercise the branch by stubbing os.kill.
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError()),
    )
    assert _pid_alive(1) is True


def test_real_subprocess_sigterm_before_claim_logs_idle(tmp_path):
    # Stopped before claiming an Issue: run_stopped result=idle, no
    # invented Issue fields.
    driver = tmp_path / "driver_idle.py"
    driver.write_text(
        "import logging, signal, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        "import orbi.runner as runner\n"
        "logging.basicConfig(level=logging.INFO, format=runner.log_format())\n"
        "signal.signal(signal.SIGTERM, runner._handle_stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopped) == 1
    assert stopped[0] == "INFO run_stopped result=idle"
    assert "issue=" not in stderr
    assert "run_stopping" not in stderr
