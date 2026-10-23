"""Pi session domain: start, resume and recovery (Issue #1646).

Third domain split out of `test_bootstrap_runner.py` (after
`test_runner_config.py`, Issue #789, and `test_bootstrap_release.py`,
Issue #1645), moved verbatim with no behavior change: the `stream_pi`
lifecycle scenes — process start and startup phases, session-file following
and resume, model_wait/idle stall recovery, rate-limit retries, timeouts and
the session pipe drain. The scaffolding they share lives in
`tests/fakes/bootstrap_pi.py`; helpers that several domains use keep their
single owner there and are imported here.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.journal as journal
import orbi.release as release
import orbi.runner as runner
from orbi import failure, pi_activity, pi_process, pi_session, progress
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from tests.fakes.bootstrap_pi import (
    _frozen_model_wait_records,
    fake_session_records,
    fresh_timestamp,
    make_fake_pi,
    make_hung_pi,
    make_idle_recovery_tracker,
    make_rate_limit_pi,
    make_slow_model_pi,
    make_timeout_tool_pi,
    make_timeout_tool_pi_controlled,
    make_timeout_tool_pi_release_gated,
    make_upstream_listener,
    startup_records,
)

# Hosted-macOS boundaries; the Ubuntu CI keeps every scene here
# authoritative. The idle-recovery scenes discover hung tools through
# /proc/<pid>/stat (Linux-only by pi_recovery's contract) and need
# nothing outside the per-process directories — a container whose /proc
# is mounted `subset=pid` (no /proc/stat) runs them for real too
# (Issue #1808).
_proc_based_recovery = sys.platform != "darwin" and Path("/proc").is_dir()
_stream_pi_linux_only = pytest.mark.skipif(
    not _proc_based_recovery,
    reason=(
        "pi_recovery discovers idle descendants through /proc/<pid>/stat; "
        "hosted macOS has no /proc at all, so these real-subprocess "
        "recovery scenes stay Linux-authoritative"
    ),
)
# The upstream_alive connection evidence reads /proc/net/tcp, a file
# outside the per-process directories: hosted macOS and a /proc mounted
# `subset=pid` do not expose it (the probe is a bypass, never a veto, so
# it honestly reports false there). The scene asserting the live-socket
# evidence needs the socket tables.
_upstream_evidence = sys.platform != "darwin" and Path("/proc/net/tcp").is_file()
_stream_pi_upstream_only = pytest.mark.skipif(
    not _upstream_evidence,
    reason=(
        "the upstream_alive evidence reads /proc/net/tcp, which hosted "
        "macOS and a /proc mounted `subset=pid` do not expose"
    ),
)


def test_stream_pi_logs_run_start_once_with_full_scene(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="orbi/xqliu-orbi-issue-24", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "final answer"
    # Without an explicit log_command the raw command is never logged.
    assert "command=<redacted>" in caplog.text
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    start = starts[0]
    assert "run=deadbeef" in start
    assert "issue=xqliu/orbi#24" in start
    assert "role=implement" in start
    assert "branch=orbi/xqliu-orbi-issue-24" in start
    assert f"worktree={tmp_path}" in start
    # The session fields are part of the scene; before Pi writes its first
    # record they are '-' (the full entry reappears on run_failed).
    assert "session=-" in start
    assert "session_file=-" in start
    # Issue #176: the scene at start shows the startup sub-phase — no
    # session file yet, so `session_pending` (not the generic
    # `starting`).
    assert "phase=session_pending" in start
    # The user message (full prompt / Issue body) never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_run_start_never_follows_pre_existing_session_file(
    tmp_path, caplog,
):
    # A resumed run starts in a worktree where the previous invocation's
    # session JSONL already exists (Issue #45): the watcher never binds to
    # it, so run_start reports no session until the current invocation
    # creates its own JSONL.
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    (session_dir / "sess.jsonl").write_text(
        json.dumps({"type": "session", "id": "sess-1"}) + "\n",
        encoding="utf-8",
    )
    command = make_fake_pi(tmp_path, session_records=[], stdout="ok")
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    # The pre-existing file belongs to the previous invocation.
    assert "session=-" in starts[0]
    assert "session_file=-" in starts[0]
    assert "sess-1" not in caplog.text


def test_stream_pi_logs_activity_and_heartbeat_lines(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer", sleep=0.3,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    heartbeats = [line for line in lines if " heartbeat " in line]
    # Issue #176: the visible fields change twice (request_pending ->
    # bash): the startup sub-phase line, then the tool-call line;
    # unchanged polls must not repeat them (Issue #40).
    assert len(activities) == 2
    assert "phase=request_pending" in activities[0]
    line = activities[1]
    # No redundant `run=` field on the high-frequency lines (Issue #57);
    # the `[run_id]` prefix is the run-id carrier (bound-run tests and
    # the e2e suite cover the prefix itself).
    assert "run=deadbeef" not in line
    assert "issue=xqliu/orbi#24" in line
    assert "role=implement" in line
    assert "phase=bash" in line
    assert 'action="bash pytest tests/"' in line
    assert "result=-" in line  # no tool result in this fake session
    assert "idle=" in line
    # No full scene on activity lines (Issue #40).
    assert "branch=" not in line
    assert f"worktree={tmp_path}" not in line
    assert "session_file=" not in line
    assert "source_repo=" not in line
    # The idle tail produced heartbeats at the poll interval.
    assert len(heartbeats) >= 1
    for line in heartbeats:
        assert "run=deadbeef" not in line
        assert "role=implement" in line
        assert ("phase=request_pending" in line
                or "phase=bash" in line)
        assert "elapsed=" in line
        assert "idle=" in line
        assert "branch=" not in line
        assert f"worktree={tmp_path}" not in line
    # The legacy verbose line is gone.
    assert "pi_activity" not in caplog.text
    assert "pi_idle" not in caplog.text


def test_stream_pi_high_frequency_lines_carry_run_id_exactly_once(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: the `[run_id]` prefix (Issue #41) is the single
    run-id carrier on the high-frequency lines; the redundant `run=`
    field is gone, so the 8-hex id appears exactly once per line and a
    single grep still reconstructs the whole timeline. The run is
    bound like in the real journal (`process_issue` binds it before
    starting Pi)."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        (1.2, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(3),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="a1b2c3d4", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    kinds = (
        "activity", "heartbeat", "model_wait", "resumed",
        "pi_idle", "pi_resumed",
    )
    high_freq = [
        message for message in caplog.messages
        if any(f" {kind} " in message for kind in kinds)
    ]
    # The record set produces every high-frequency kind except the idle
    # pair (activity + heartbeats, one model_wait, one resumed).
    assert any(" activity " in m for m in high_freq)
    assert any(" heartbeat " in m for m in high_freq)
    assert any(" model_wait " in m for m in high_freq)
    assert any(" resumed " in m for m in high_freq)
    for message in high_freq:
        assert "run=" not in message, message
        assert message.startswith("[a1b2c3d4]"), message
        assert message.count("a1b2c3d4") == 1, message


def test_stream_pi_idle_lines_carry_run_id_exactly_once(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: `pi_idle` / `pi_resumed` repeat the same rule as the
    other high-frequency lines: prefix only, no `run=` field."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    # The injected clock makes staleness deterministic: a1 is ten seconds
    # stale and a2 is current. The fake Pi waits for the streamer's
    # progress callback after a1, so a slow CI runner cannot coalesce the
    # two records into one poll.
    fake_now = lambda: 10.0
    release_a2 = tmp_path / "release-a2"
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": "1970-01-01T00:00:10+00:00", "cwd": "/w"}),
        (0.05, {"type": "message", "id": "a1",
               "timestamp": "1970-01-01T00:00:00+00:00",
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "one"}]}}),
        (0.3, {"type": "message", "id": "a2",
               "timestamp": "1970-01-01T00:00:10+00:00",
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "two"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
        wait_after_record=1, release_file=release_a2,
    )

    def release_after_a1(activity):
        if activity["action"] == "assistant text" and not release_a2.exists():
            release_a2.touch()

    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="a1b2c3d4", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=10.0, activity_clock=fake_now), cwd=tmp_path, progress=release_after_a1)
    idles = [m for m in caplog.messages if " pi_idle " in m]
    resumed = [m for m in caplog.messages if " pi_resumed " in m]
    assert len(idles) == 1
    assert len(resumed) == 1
    for message in idles + resumed:
        assert "run=" not in message, message
        assert message.startswith("[a1b2c3d4]"), message
        assert message.count("a1b2c3d4") == 1, message


def test_stream_pi_scene_lines_keep_run_field_for_parse_scene(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: the low-frequency scene lines (run_start / run_failed)
    keep `run=` so `pi_activity.parse_scene` still returns the run id
    from the lines that need to be parsed. The run is bound like in
    the real journal."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stderr="pi exploded", exit_code=3,
    )
    with caplog.at_level("INFO"), pytest.raises(
        subprocess.CalledProcessError,
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="a1b2c3d4", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    starts = [m for m in caplog.messages if " run_start " in m]
    failures = [m for m in caplog.messages if " run_failed " in m]
    assert len(starts) == 1 and len(failures) == 1
    for message in starts + failures:
        assert "run=a1b2c3d4" in message, message
        fields = pi_activity.parse_scene(message)
        assert fields["run"] == "a1b2c3d4"
        assert fields["issue"] == "xqliu/orbi#24"


def test_stream_pi_activity_keeps_action_after_tool_result(tmp_path, caplog):
    """A tool result updates result only; the action line is not repeated."""
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    # Issue #176: one activity line for the startup sub-phase, one for
    # the tool call, one for the result=ok update; the action (the real
    # command) is preserved on the tool lines.
    assert len(activities) == 3
    assert "phase=request_pending" in activities[0]
    assert all('action="bash pytest tests/"' in line
               for line in activities[1:])
    assert "result=-" in activities[1]
    assert "result=ok" in activities[2]
    assert "tool_result" not in caplog.text


def test_stream_pi_heartbeat_interval_is_stable(tmp_path, caplog):
    """A silent session emits one heartbeat per poll interval, no activity."""
    command = make_fake_pi(
        tmp_path, session_records=[], sleep=1.0,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    heartbeats = [line for line in lines if " heartbeat " in line]
    activities = [line for line in lines if " activity " in line]
    assert activities == []
    # ~1s of idleness at a 0.1s interval: several heartbeats, one per poll.
    # Issue #176: with no session file the sub-phase is session_pending.
    assert len(heartbeats) >= 4
    for line in heartbeats:
        assert "phase=session_pending" in line
        assert "session=-" not in line  # session fields are not repeated


def test_stream_pi_success_logs_no_run_end(tmp_path, caplog):
    # run_end is logged by process_issue once the PR and commit are known;
    # stream_pi must not emit it (it cannot know them).
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert " run_end " not in caplog.text


def test_stream_pi_logs_command_redacted_and_stderr(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=[],
        stdout="ok", stderr="warning line",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, log_command=["pi", "--print", "<redacted>"])
    assert "command=pi --print <redacted>" in caplog.text
    assert "stderr=warning line" in caplog.text


def test_stream_pi_logs_run_failed_with_full_scene_and_reraises(
    tmp_path, caplog,
):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stderr="pi exploded", exit_code=3,
    )
    with caplog.at_level("ERROR"), pytest.raises(
        subprocess.CalledProcessError,
    ) as excinfo:
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert excinfo.value.returncode == 3
    assert "pi exploded" in (excinfo.value.stderr or "")
    # The exception must not carry the raw command (prompt / Issue body).
    assert "SECRET ISSUE BODY" not in str(excinfo.value)
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    failure = failures[0]
    assert "run=deadbeef" in failure
    assert "issue=xqliu/orbi#24" in failure
    assert "role=implement" in failure
    assert "phase=bash" in failure
    assert "reason=pi_exit_3" in failure
    # The full scene is the debug entry again: worktree and session file.
    assert f"worktree={tmp_path}" in failure
    assert f"session_file={tmp_path / '.pi-session' / 'sess.jsonl'}" in failure
    assert "session=sess-1" in failure
    # The session JSONL stays in the worktree as the local record.
    session_files = list((tmp_path / ".pi-session").glob("*.jsonl"))
    assert len(session_files) == 1
    assert len(session_files[0].read_text(encoding="utf-8").splitlines()) == 3


def test_stream_pi_heartbeats_when_session_is_idle(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        sleep=1.0,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    # Idle duration is visible on the heartbeat line itself.
    assert any("idle=" in line for line in heartbeats)


def test_stream_pi_heartbeats_when_no_session_file_appears(tmp_path, caplog):
    # Issue #176: with no session file the startup sub-phase is
    # `session_pending` (Pi is spawned but has not created its session
    # yet) — the generic `starting` is gone from the live lines.
    command = make_fake_pi(tmp_path, session_records=[], sleep=1.0)
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    assert all("phase=session_pending" in line for line in heartbeats)


# ------------------------------------ startup phases (Issue #176)

def test_stream_pi_logs_process_spawned_after_popen(tmp_path, caplog):
    """`process_spawned` is logged exactly once, right after the Pi
    process is spawned, and carries the pid (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(), stdout="ok",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = [line for line in caplog.text.splitlines()
             if " process_spawned " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "issue=xqliu/orbi#24" in line
    assert "role=implement" in line
    # Before the session file exists the selection is unknown.
    assert "provider=-" in line
    assert "model=-" in line
    assert "elapsed=" in line
    # The pid is a real integer and the raw prompt never reaches the log.
    pid = int(line.split("pid=")[1].split()[0])
    assert pid > 0
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_logs_startup_milestones_in_order(tmp_path, caplog):
    """A healthy startup logs `session_created`, `first_request_started`
    and `first_response_received` exactly once each, in order, and the
    lines carry the provider/model Pi actually selected (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(), stdout="ok",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    created = [line for line in lines if " session_created " in line]
    requested = [line for line in lines if " first_request_started " in line]
    responded = [line for line in lines if " first_response_received " in line]
    assert len(created) == 1
    assert len(requested) == 1
    assert len(responded) == 1
    # The milestones appear in the order the session records arrive.
    assert (lines.index(created[0]) < lines.index(requested[0])
            < lines.index(responded[0]))
    # The real selection (from the session's model_change record) is
    # visible on the request/response lines.
    assert "provider=local-qwen" in requested[0]
    assert "model=qwen3.8:27b" in requested[0]
    assert "provider=local-qwen" in responded[0]
    assert "model=qwen3.8:27b" in responded[0]
    for line in (created[0], requested[0], responded[0]):
        assert "issue=xqliu/orbi#24" in line
        assert "role=implement" in line
        assert "elapsed=" in line


def test_stream_pi_activity_lines_show_startup_sub_phase(tmp_path, caplog):
    """The live activity/heartbeat lines show the startup sub-phase
    instead of the generic `starting` (Issue #176): `session_pending`
    until the session file exists, `request_pending` until the first
    response, then the tool-based phase. The records are delayed so
    each sub-phase is visible for at least one poll."""
    records = [
        (0.3, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.45, {"type": "model_change", "id": "m1",
                "timestamp": fresh_timestamp(),
                "provider": "local-qwen", "modelId": "qwen3.8:27b"}),
        (0.6, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.9, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    live = [line for line in lines
            if " activity " in line or " heartbeat " in line]
    assert any("phase=session_pending" in line for line in live)
    assert any("phase=request_pending" in line for line in live)
    # After the first response the tool-based phase applies as before.
    assert any("phase=bash" in line for line in live)


def test_stream_pi_startup_failed_without_session_file(tmp_path, caplog):
    """Pi exits before creating its session file: `startup_failed`
    carries `reason=session_not_created` (and the existing `run_failed`
    line is unchanged, Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[], stdout="", stderr="boom", exit_code=3,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "reason=session_not_created" in line
    assert "session_created=false" in line
    assert "first_request=false" in line
    assert "issue=xqliu/orbi#24" in line
    assert "role=implement" in line
    assert "elapsed=" in line
    # The existing fail-fast scene line is unchanged.
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=pi_exit_3" in failed[0]


def test_stream_pi_startup_failed_without_first_request(tmp_path, caplog):
    """The session file exists but Pi never sent its first request:
    `reason=no_first_request` (Issue #176)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert not isinstance(excinfo.value, runner.RecoverablePiProcessError)
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=no_first_request" in lines[0]
    assert "session_created=true" in lines[0]
    assert "first_request=false" in lines[0]


def test_stream_pi_early_exit_uses_the_flushed_journal(
        tmp_path, caplog, monkeypatch,
):
    """Issue #656: Pi flushes its session journal while dying, so the
    live watcher's LAST poll can predate it. The exit classification
    must use the journal on disk, never that stale poll: in the #655
    usage-limit scene the journal held the request and the provider
    error, yet the exit was raised as a terminal startup failure and
    the in-flight delivery was burned to `ai-blocked`."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {
                   "role": "assistant", "content": [],
                   "stopReason": "error",
                   "errorMessage": "Codex error: The usage limit "
                                   "has been reached"}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, exit_code=1,
        stderr="Codex error: The usage limit has been reached",
    )

    class StaleWatcher(pi_process.SessionWatcher):
        """The live watcher of the #655 scene: its last poll ran while
        Pi was still alive, before the dying process flushed the
        journal, so it never bound to the session file."""

        def _next_session_file(self):
            return None

    monkeypatch.setattr(pi_process, "SessionWatcher", StaleWatcher)
    with caplog.at_level("INFO"):
        with pytest.raises(runner.RecoverablePiProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    # The journal on disk proves the request went out (its response
    # arrived), so this is NOT a startup failure.
    assert " startup_failed " not in caplog.text
    # The exit scene reports the SAME journal the classification used:
    # a `phase=session_pending` / `last_activity=-` next to a real
    # `session_file=` would describe the session as not-created while
    # the recoverable decision says a request went out.
    scene = [line for line in caplog.text.splitlines()
             if " run_failed " in line]
    assert len(scene) == 1
    assert "session=sess-1" in scene[0]
    assert "session_file=" in scene[0]
    assert "phase=starting" in scene[0]
    assert "last_activity=- " not in scene[0]
    # The prompt never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_logs_the_codex_usage_limit_as_provider_quota(
        tmp_path, caplog,
):
    """Issue #1374: a Codex quota stop is written only in the session
    journal's newest `errorMessage`, never on stderr. The exit logs one
    WARNING `provider_quota` line and still raises the recoverable
    `RecoverablePiProcessError` carrying the message, so a quota stop is
    not mistaken for a crash and the run keeps retrying."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {
                   "role": "assistant", "content": [],
                   "stopReason": "error",
                   "errorMessage": "Codex error: The usage limit "
                                   "has been reached"}}),
    ]
    # Empty stderr: the ONLY place the message exists is the journal.
    command = make_fake_pi(
        tmp_path, session_records=records, exit_code=1, stderr="",
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(runner.RecoverablePiProcessError) as excinfo:
            pi_session.stream_pi(
                command,
                ctx=RunContext(
                    run_id="deadbeef", issue=24, branch="b",
                    worktree=tmp_path, source_repo="xqliu/orbi",
                ),
                watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
            )
    quota = [line for line in caplog.text.splitlines()
             if "provider_quota" in line]
    assert len(quota) == 1, caplog.text
    assert (
        'detail="Codex error: The usage limit has been reached"'
        in quota[0]
    )
    detail = runner._failure_detail(excinfo.value)
    assert "usage limit has been reached" in detail.lower()
    assert runner._classify_failure(
        excinfo.value, outcome="blocked",
    ).reason_code == "provider_quota"
    # The prompt never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_startup_failed_early_exit_after_first_request(
        tmp_path, caplog,
):
    """The first request went out but Pi exited early without a first
    response (unclassifiable stderr): `reason=pi_exit_<N>` with the
    stuck-point fields (Issue #176)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=pi_exit_1" in lines[0]
    assert "session_created=true" in lines[0]
    assert "first_request=true" in lines[0]
    # The prompt never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_startup_failed_reason_maps_hung_first_request_to_timeout():
    """A frozen model_wait killed before the first response is the
    first-response timeout (Issue #176): the request was in flight and
    the response never arrived."""
    activity = {
        "session_file": "/w/.pi-session/s.jsonl",
        "first_request": True,
        "first_response": False,
    }
    assert pi_process._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=True,
        model_wait_swallowed=False, idle_recovery_failed=False,
    ) == "first_response_timeout"
    assert pi_process._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=False,
        model_wait_swallowed=True, idle_recovery_failed=False,
    ) == "model_wait_swallowed"
    # The other failure classes keep their own reasons.
    assert pi_process._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=True, model_wait_dead=False,
        model_wait_swallowed=False, idle_recovery_failed=False,
    ) == "timeout"
    assert pi_process._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=False,
        model_wait_swallowed=False, idle_recovery_failed=True,
    ) == "idle_recovery_stale"


def test_rate_limit_marker_classification():
    """Issue #321: the 429 evidence classes are Pi's own stderr markers —
    `429` / `quota` / `RESOURCE_EXHAUSTED` / `retry in` — and anything
    else stays unclassified."""
    assert pi_process._is_rate_limited("Error 429: too many requests")
    assert pi_process._is_rate_limited(
        "Resource has been exhausted (e.g. check quota)")
    assert pi_process._is_rate_limited("status: RESOURCE_EXHAUSTED")
    assert pi_process._is_rate_limited("please RETRY IN 2s")
    assert not pi_process._is_rate_limited("boom")
    assert not pi_process._is_rate_limited(
        "Error: connect ETIMEDOUT (request timed out)")
    # The existing classes win on their own evidence: a 401 without a
    # rate-limit marker is still an auth failure.
    assert pi_process._classify_startup_exit(
        "Error: 401 Unauthorized", 1) == "auth_failure"


def test_retry_after_parsing_and_backoff():
    """Issue #321: the backoff honors the 429 response's own retry hint
    (`retry in Ns` / `retryDelay: Ns`, capped at ~5 minutes); without a
    hint the default escalates 30/60/120/240 and stays at the cap."""
    assert pi_process._retry_after_seconds(
        "Please retry in 36.123456s. ID: abc") == 36.123456
    assert pi_process._retry_after_seconds('"retryDelay": "37s"') == 37.0
    assert pi_process._retry_after_seconds("retryDelay:19s") == 19.0
    assert pi_process._retry_after_seconds("no hint here") is None
    assert pi_process._backoff_seconds(0, "Please retry in 2s") == 2.0
    assert pi_process._backoff_seconds(3, '"retryDelay": "36s"') == 36.0
    # A hint longer than the cap is capped (the slot must move on).
    assert pi_process._backoff_seconds(0, "retry in 9999s") == 300.0
    assert pi_process._backoff_seconds(0, "") == 30.0
    assert pi_process._backoff_seconds(1, "") == 60.0
    assert pi_process._backoff_seconds(2, "") == 120.0
    assert pi_process._backoff_seconds(3, "") == 240.0
    assert pi_process._backoff_seconds(4, "") == 300.0
    assert pi_process._backoff_seconds(9, "") == 300.0


def test_stream_pi_rate_limited_exit_retries_then_succeeds(
    tmp_path, monkeypatch, caplog,
):
    """Issue #321 acceptance: a fake Pi 429 exit is retried IN THE SAME
    RUN after the response's own retry hint — the delivery succeeds and
    no failure is ever raised (so the Issue can never go ai-blocked on a
    transient burst limit)."""
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=1, stdout="final answer",
        stderr="Error: 429 RESOURCE_EXHAUSTED. Please retry in 36.5s",
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=321, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert result == "final answer"
    # The backoff honored the response's hint.
    assert sleeps == [36.5]
    retries = [line for line in caplog.text.splitlines()
               if " pi_retry_429 " in line]
    assert len(retries) == 1
    assert "run_id=deadbeef" in retries[0]
    assert "attempt=1" in retries[0]
    assert "next_retry_in=36s" in retries[0]
    # The same run re-spawned the session: two run_start scenes.
    assert len([line for line in caplog.text.splitlines()
                if " run_start " in line]) == 2
    assert "run_failed" not in caplog.text


def test_stream_pi_rate_limited_exhausted_takes_existing_failure_path(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698 acceptance: 429 through every backoff retry (5) is a
    TERMINAL delivery failure — `RateLimitExhaustedError` (never a
    recoverable resume), marked `reason=provider_rate_limited` on the
    startup and run_failed lines, with the cumulative count and the
    quota-not-task repair action in the operator-facing message."""
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99,
        stderr='{"error":{"code":429,"message":"Resource has been '
               'exhausted (e.g. check quota)","status":'
               '"RESOURCE_EXHAUSTED"}}',
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError) as exc_info:
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=321, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    # The stderr carries no retry hint: the default escalation, then the
    # cap. 5 retries = 6 sessions total.
    assert sleeps == [30.0, 60.0, 120.0, 240.0, 300.0]
    retries = [line for line in caplog.text.splitlines()
               if " pi_retry_429 " in line]
    assert [f"attempt={attempt}" for attempt in (1, 2, 3, 4, 5)] == [
        field for line in retries for field in line.split()
        if field.startswith("attempt=")
    ]
    assert "next_retry_in=30s" in retries[0]
    assert len([line for line in caplog.text.splitlines()
                if " run_start " in line]) == 6
    starts = [line for line in caplog.text.splitlines()
              if " startup_failed " in line]
    assert len(starts) == 6
    for line in starts:
        assert "reason=provider_rate_limited" in line
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=provider_rate_limited" in failed[0]
    # Terminal, never recoverable (Issue #698: the exhausted limit must
    # not resume the same run). The message is the operator-facing cause:
    # the cumulative count and the repair action.
    assert not isinstance(exc_info.value, runner.RecoverablePiFailure)
    message = str(exc_info.value)
    assert "provider rate limit retries exhausted" in message
    assert "6 consecutive 429 exits" in message
    assert "limit=5" in message
    assert "relabel the Issue ai-ready" in message


def test_stream_pi_rate_limit_retries_zero_fails_on_the_first_429(
    tmp_path, monkeypatch, caplog,
):
    """Issue #1600: a session with a zero retry budget — the short
    auxiliary understanding session of `orbi suggest` — fails on its
    FIRST 429 exit: no backoff sleep and no re-spawn, so the bypass can
    never hold the command past its own timeout."""
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99,
        stderr="Error: 429 RESOURCE_EXHAUSTED. Please retry in 36.5s",
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError) as exc_info:
            pi_session.stream_pi(
                command,
                ctx=RunContext(
                    run_id="deadbeef", issue=0, branch="",
                    worktree=tmp_path, source_repo="xqliu/orbi",
                ),
                rate_limit_retries=0,
                watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path,
            )
    # The response's own 36.5 s hint was never waited out.
    assert sleeps == []
    assert "pi_retry_429" not in caplog.text
    assert len([line for line in caplog.text.splitlines()
                if " run_start " in line]) == 1
    message = str(exc_info.value)
    assert "1 consecutive 429 exits" in message
    assert "limit=0" in message


def test_stream_pi_rate_limited_exhausted_mid_session_terminates(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698: exhausting the retries after the session sent its
    first request is TERMINAL too — the exhausted limit must never resume
    the same run, whatever the session had already produced."""
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99, records=startup_records(),
        stderr="Error 429: rate limited, retry in 2s",
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError) as exc_info:
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=321, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert sleeps == [2.0] * 5
    assert not isinstance(exc_info.value, runner.RecoverablePiFailure)
    assert "provider rate limit retries exhausted" in str(exc_info.value)
    # After the first response this is a mid-run failure: no
    # startup_failed line, the run_failed scene carries the reason.
    assert not any(" startup_failed " in line
                   for line in caplog.text.splitlines())
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=provider_rate_limited" in failed[0]


def test_stream_pi_429_attempts_persist_across_invocations(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698 acceptance: the `pi_retry_429` attempt count is
    cumulative per run_id — a resumed (restarted) runner invocation
    continues where the killed one stopped, never back at 1. The
    termination guard (sleep raising on the Nth call) simulates the
    runner dying mid-backoff, Issue #95."""
    command = make_rate_limit_pi(
        tmp_path, fail_times=99, stderr="Error 429: rate limited, retry in 2s",
    )

    def guard(after: int):
        """A time.sleep that lets `after` calls pass, then raises: the
        runner dies mid-backoff (termination guard, Issue #95)."""
        calls = {"n": 0}

        def _sleep(seconds):
            calls["n"] += 1
            if calls["n"] > after:
                raise RuntimeError("test termination guard")

        return _sleep

    # Invocation 1 (the original runner process): two backoffs survive,
    # the process dies during the third wait — AFTER the third retry was
    # persisted (the file below proves persist-before-sleep).
    monkeypatch.setattr(pi_process.time, "sleep", guard(2))
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="termination guard"):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert json.loads(
        pi_process.pi_429_attempts_path(tmp_path).read_text()
    ) == {"run_id": "deadbeef", "attempts": 3}

    # Invocation 2 (the restart scan resumed the same run): the counter
    # continues — the next retry line says attempt=4, never attempt=1.
    monkeypatch.setattr(pi_process.time, "sleep", guard(0))
    caplog.clear()
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="termination guard"):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    retries = [line for line in caplog.text.splitlines()
               if " pi_retry_429 " in line]
    assert [field for line in retries for field in line.split()
            if field.startswith("attempt=")] == ["attempt=4"]
    assert json.loads(
        pi_process.pi_429_attempts_path(tmp_path).read_text()
    ) == {"run_id": "deadbeef", "attempts": 4}


def test_stream_pi_429_exhaustion_across_restarts_is_terminal(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698 acceptance: the CUMULATIVE count reaches the limit —
    a resumed invocation whose counter already sits at limit-1 terminates
    on its next 429 exit instead of restarting a full backoff cycle."""
    pi_process._record_429_attempts(tmp_path, "deadbeef", 4)
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99, stderr="Error 429: rate limited, retry in 2s",
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError) as exc_info:
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    # One backoff retry (the 5th cumulative), then the 6th 429 exit is
    # terminal — one session, not a fresh 5-retry cycle.
    assert sleeps == [2.0]
    retries = [line for line in caplog.text.splitlines()
               if " pi_retry_429 " in line]
    assert [field for line in retries for field in line.split()
            if field.startswith("attempt=")] == ["attempt=5"]
    assert not isinstance(exc_info.value, runner.RecoverablePiFailure)
    assert "6 consecutive 429 exits" in str(exc_info.value)


def test_stream_pi_429_terminal_resets_counter_for_human_retry(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698: the terminal exhaustion spent this run's backoff
    budget and CLEARS the persisted counter, so the documented repair
    (relabel ai-ready) starts a FULL fresh budget even when the retry
    reuses the SAME worktree and run_id — the open-PR review resume
    does exactly that. Without the reset the repair would terminal-exit
    on the first new 429 (zero budget) and the Issue would block
    again on its first transient hiccup."""
    pi_process._record_429_attempts(tmp_path, "deadbeef", 5)
    sleeps: list[float] = []
    monkeypatch.setattr(pi_process.time, "sleep", sleeps.append)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99, stderr="Error 429: rate limited, retry in 2s",
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    # Terminal on the first 429 (the budget was already spent), and the
    # counter file is gone.
    assert sleeps == []
    assert not pi_process.pi_429_attempts_path(tmp_path).exists()
    # The human-requeued retry (same worktree, same run_id) has the full
    # budget again: five backoff retries before the next terminal.
    sleeps.clear()
    with pytest.raises(pi_process.RateLimitExhaustedError):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert sleeps == [2.0] * 5


def test_stream_pi_429_terminal_survives_a_failed_counter_clear(
    tmp_path, monkeypatch, caplog,
):
    """Issue #698: the terminal decision never depends on its own
    artifact cleanup — a failing counter unlink is one warning line and
    the RateLimitExhaustedError still raises."""
    pi_process._record_429_attempts(tmp_path, "deadbeef", 5)
    monkeypatch.setattr(pi_process.time, "sleep", [])

    def broken_unlink(self, *args, **kwargs):
        raise OSError("read-only fs")

    monkeypatch.setattr(pi_process.Path, "unlink", broken_unlink)
    command = make_rate_limit_pi(
        tmp_path, fail_times=99, stderr="Error 429: rate limited, retry in 2s",
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RateLimitExhaustedError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=698, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert "pi_429_attempts_clear_failed" in caplog.text


def test_429_attempt_counter_file_round_trip(tmp_path, monkeypatch, caplog):
    """The persisted counter is per-run and fails safe: absent, corrupt,
    foreign-run or invalid files all read as 0 (worst case = today's
    behavior, never a false terminal), and a failed write never breaks
    the retry loop."""
    assert pi_process._load_429_attempts(tmp_path, "deadbeef") == 0
    pi_process._record_429_attempts(tmp_path, "deadbeef", 3)
    assert pi_process._load_429_attempts(tmp_path, "deadbeef") == 3
    # Another run never inherits this run's count.
    assert pi_process._load_429_attempts(tmp_path, "c0ffee00") == 0
    path = pi_process.pi_429_attempts_path(tmp_path)
    with caplog.at_level("INFO"):
        for corrupt in (
            "{not json",
            json.dumps({"run_id": "deadbeef", "attempts": "many"}),
            json.dumps({"run_id": "deadbeef", "attempts": -2}),
            json.dumps({"attempts": 3}),
            json.dumps(["deadbeef", 3]),
        ):
            path.write_text(corrupt)
            assert pi_process._load_429_attempts(tmp_path, "deadbeef") == 0
        assert "pi_429_attempts" in caplog.text

        def broken_write(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pi_process.Path, "write_text", broken_write)
        pi_process._record_429_attempts(tmp_path, "deadbeef", 4)
        assert "pi_429_attempts" in caplog.text


def test_429_attempt_counter_write_is_atomic(tmp_path, monkeypatch, caplog):
    """Issue #1851: the 429 counter is written tmp+rename, so a kill
    between truncate and write cannot reset the backoff. The bytes stay
    the same single-line JSON, and a failure at the rename keeps the
    previous count — one warning, never a broken retry loop."""
    pi_process._record_429_attempts(tmp_path, "deadbeef", 3)
    path = pi_process.pi_429_attempts_path(tmp_path)
    golden = '{"run_id": "deadbeef", "attempts": 3}\n'
    assert path.read_text(encoding="utf-8") == golden

    def killed(src, dst):
        raise OSError("killed before the rename")

    # The stdlib rename boundary itself, never a src-module symbol.
    monkeypatch.setattr(os, "replace", killed)
    with caplog.at_level("INFO"):
        pi_process._record_429_attempts(tmp_path, "deadbeef", 4)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == golden
    assert pi_process._load_429_attempts(tmp_path, "deadbeef") == 3
    assert "pi_429_attempts_write_failed" in caplog.text


def test_stream_pi_non_rate_limited_exit_never_retries(tmp_path, caplog):
    """Issue #321: the business failure semantics are untouched — a non-
    429 exit fails exactly once, with no retry line."""
    command = make_fake_pi(
        tmp_path, session_records=[], stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=321, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path)
    assert "pi_retry_429" not in caplog.text
    assert len([line for line in caplog.text.splitlines()
                if " run_start " in line]) == 1


def test_stream_pi_startup_failed_auth_failure(tmp_path, caplog):
    """A provider authentication failure in Pi's stderr is classified as
    `reason=auth_failure` (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[], stderr="Error: 401 Unauthorized",
        exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=auth_failure" in lines[0]
    assert "session_created=false" in lines[0]


def test_stream_pi_midrun_provider_401_stays_recoverable(tmp_path, caplog):
    """Issue #1455 acceptance: a provider 401 after the first request
    (the rejected/expired model API key) is again a RECOVERABLE Pi
    failure — `RecoverablePiProcessError`, so `process_issue` keeps the
    Issue `ai-in-progress` and the next tick resumes the same run. It is
    never the terminal `ai-blocked` of the reverted #1450/#1453."""
    stderr = (
        '401: {"message":"Authentication Fails, Your api key: '
        '****lder is invalid (request_id: abc)"}'
    )
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(), stderr=stderr,
        exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(pi_process.RecoverablePiProcessError) as excinfo:
            pi_session.stream_pi(
                command,
                ctx=RunContext(
                    run_id="deadbeef", issue=24, branch="b",
                    worktree=tmp_path, source_repo="xqliu/orbi",
                ),
                watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
            )
    # Recoverable by type: the next tick resumes the same run.
    assert isinstance(excinfo.value, pi_process.RecoverablePiFailure)
    # The provider's own wording is preserved on the raised failure.
    assert "Authentication Fails" in excinfo.value.stderr


def test_stream_pi_startup_failed_network_timeout(tmp_path, caplog):
    """A network timeout in Pi's stderr is classified as
    `reason=network_timeout` (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[],
        stderr='Error: connect ETIMEDOUT (request timed out)',
        exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=network_timeout" in lines[0]


def test_stream_pi_no_startup_failed_after_first_response(tmp_path, caplog):
    """A failure AFTER the first response is a mid-run failure, not a
    startup failure: no `startup_failed` line (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(),
        stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert not any(" startup_failed " in line
                   for line in caplog.text.splitlines())
    # The existing run_failed scene line still carries the reason.
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=pi_exit_1" in failed[0]


def test_stream_pi_model_wait_then_resumed_no_warning_spam(
    tmp_path, caplog,
):
    """Regression (Issue #40): a long model response after a tool result
    must be reported as `state=model_wait`, not as idle. Exactly one
    transition line when entering model_wait, one `resumed` line when the
    next assistant event arrives, and only configured-interval heartbeats
    while waiting — no WARNING/ERROR spam (a slow model is not a stalled
    agent)."""
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        (1.2, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(3),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    # One transition into model_wait, one transition back on resume.
    waits = [line for line in lines if " model_wait " in line]
    resumed = [line for line in lines if " resumed " in line]
    assert len(waits) == 1
    assert len(resumed) == 1
    wait = waits[0]
    # The transition lines carry no redundant `run=` field (Issue #57);
    # the `[run_id]` prefix is the run-id carrier.
    assert "run=deadbeef" not in wait
    assert "issue=xqliu/orbi#24" in wait
    assert "role=implement" in wait
    assert "phase=bash" in wait
    assert "state=model_wait" in wait
    # The full scene never rides on the transition lines.
    assert "branch=" not in wait
    assert f"worktree={tmp_path}" not in wait
    resume = resumed[0]
    assert "run=deadbeef" not in resume
    assert "state=resumed" in resume
    assert "phase=bash" in resume
    # While waiting, the heartbeats carry the model_wait state and the
    # idle time (the wait duration is visible on the line itself)...
    wait_heartbeats = [
        line for line in lines
        if " heartbeat " in line and "state=model_wait" in line
    ]
    assert len(wait_heartbeats) >= 1
    for line in wait_heartbeats:
        assert "idle=" in line
        assert "elapsed=" in line
    # ...and nothing is escalated: no WARNING/ERROR while the model
    # responds (no warning spam, no stalled inference).
    assert "WARNING" not in caplog.text
    assert "ERROR" not in caplog.text


def test_stream_pi_no_model_wait_after_assistant_text(tmp_path, caplog):
    # model_wait is only entered when the last session event is a tool
    # result; an assistant text (e.g. the final answer) must not trigger
    # the transition.
    records = fake_session_records() + [
        (0.4, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.5,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert " model_wait " not in caplog.text
    assert " resumed " not in caplog.text


def test_stream_pi_idle_warn_default_is_five_minutes():
    # The Issue #18 contract: no model/session activity for 5 minutes
    # logs an idle warning.
    assert runner.PI_IDLE_WARN_SECONDS == 300.0


def test_stream_pi_logs_idle_warning_once_when_session_stalls(
    tmp_path, caplog,
):
    """Issue #18 acceptance: a stalled session (no model/session
    activity past the threshold, not waiting on the model) logs ONE
    `pi_idle` WARNING carrying `stale_seconds` — never a warning per
    heartbeat."""
    command = make_fake_pi(tmp_path, session_records=[], sleep=1.2)
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.5), cwd=tmp_path)
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    assert len(idles) == 1, f"exactly one idle warning: {lines}"
    idle = idles[0]
    # No redundant `run=` field (Issue #57); the `[run_id]` prefix is
    # the run-id carrier.
    assert "run=deadbeef" not in idle
    assert "issue=xqliu/orbi#24" in idle
    assert "role=implement" in idle
    # Issue #176: no session file was ever created, so the sub-phase is
    # session_pending (the stall is visible with its stuck point).
    assert "phase=session_pending" in idle
    assert "stale_seconds=" in idle
    # The warning is a WARNING (visible in journalctl without -p info).
    # No leading space: the `[run_id]` prefix (RunIdFilter, Issue #41)
    # is not part of the message when no run id is bound in this test.
    assert any(
        record.levelno == logging.WARNING
        and "pi_idle " in record.getMessage()
        for record in caplog.records
    )


def test_stream_pi_logs_pi_resumed_after_idle_warning(tmp_path, caplog):
    """The first new session event after an idle warning logs
    `pi_resumed` (Issue #18: 恢复后输出 resumed)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.8, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "back"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.4), cwd=tmp_path)
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(idles) == 1
    assert len(resumed) == 1
    # resumed comes after the warning and carries no redundant `run=`
    # field (Issue #57).
    assert lines.index(resumed[0]) > lines.index(idles[0])
    assert "run=deadbeef" not in resumed[0]
    assert "issue=xqliu/orbi#24" in resumed[0]
    assert "role=implement" in resumed[0]
    assert "phase=starting" in resumed[0]


def test_stream_pi_no_idle_warning_during_model_wait(tmp_path, caplog):
    """Issue #40 semantics: while the newest session event is a tool
    result the model is expected to reply next — a long silence is a
    slow model, never an idle warning (no warning spam)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, sleep=1.2,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.5), cwd=tmp_path)
    assert " pi_idle " not in caplog.text
    # The silence is visible as the model_wait state instead.
    waits = [line for line in caplog.text.splitlines()
             if " model_wait " in line]
    assert len(waits) == 1


def test_stream_pi_resumed_run_follows_new_session_file(tmp_path, caplog):
    """Regression (Issue #45 round-5 review, Major 3): a resumed Fixer
    starts in the original worktree where the previous implementer's
    session JSONL already exists. The activity journal must follow the
    NEW session file created by the current Pi process, not the old
    one (which would report the previous session as idle)."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    old = session_dir / "old-session.jsonl"
    with old.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "session", "id": "old-session",
            "timestamp": "2026-08-25T02:00:00Z", "cwd": "/w",
        }) + "\n")
    command = make_fake_pi(
        tmp_path,
        session_records=[
            (0.0, {"type": "session", "id": "new-session",
                   "timestamp": fresh_timestamp(), "cwd": "/w"}),
            (0.1, {"type": "message", "id": "a1",
                   "timestamp": fresh_timestamp(1),
                   "message": {"role": "assistant", "content": [
                       {"type": "toolCall", "id": "t1", "name": "bash",
                        "arguments": {"command": "pytest tests/"}}]}}),
        ],
        stdout="fixed",
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="run45", issue=45, branch="orbi/xqliu-orbi-issue-45-run45", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "fixed"
    # The journal follows the NEW session created by this invocation: its
    # tool call is reported (the old session has no messages, so binding to
    # it would show no phase/action at all)...
    assert "phase=bash" in caplog.text
    assert 'action="bash pytest tests/"' in caplog.text
    # ...and never reports the pre-existing session as the live one.
    assert "old-session" not in caplog.text


def test_stream_pi_drains_pipe_data_written_after_exit(
    monkeypatch, tmp_path, caplog,
):
    """Data left in the pipes after the process exits is drained and kept."""

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self._out_r, self._out_w = os.pipe()
            self._err_r, self._err_w = os.pipe()
            self.stdout = os.fdopen(self._out_r, "rb")
            self.stderr = os.fdopen(self._err_r, "rb")
            self._out_writer = os.fdopen(self._out_w, "wb")
            self._err_writer = os.fdopen(self._err_w, "wb")
            self.returncode = 0
            # A real Popen always carries the child pid (the
            # process_spawned line, Issue #176, logs it).
            self.pid = os.getpid()

        def poll(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)

        def writer():
            time.sleep(0.3)
            process._out_writer.write(b"late stdout data")
            process._err_writer.write(b"late stderr data")
            process._out_writer.close()
            process._err_writer.close()

        threading.Thread(target=writer, daemon=True).start()
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(["fake"], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "late stdout data"
    assert "stderr=late stderr data" in caplog.text


def test_stream_pi_spawns_pi_with_stdin_devnull(
    monkeypatch, tmp_path,
):
    """Issue #1562: the Pi child must never inherit the Runner's stdin.

    Pi's print/JSON mode reads piped stdin until it closes and prepends it
    to the prompt; started from an open pipe (ssh host orbi without -t,
    a shell pipeline into orbi, a wrapper that forgets stdin) an inherited
    stdin makes the session wait forever with no output, no error and no
    model request. The session Popen therefore has to pass
    stdin=subprocess.DEVNULL."""
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    captured: list[dict] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        captured.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(pi_process.subprocess, "Popen", recording_popen)
    result = pi_session.stream_pi(
        command,
        ctx=RunContext(run_id="deadbeef", issue=1562, branch="b",
                       worktree=tmp_path, source_repo="xqliu/orbi"),
        watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
    )
    assert result == "final answer"
    assert captured, "stream_pi must start the Pi session through Popen"
    assert captured[0]["stdin"] is subprocess.DEVNULL


def test_stream_pi_hung_model_request_killed_when_upstream_gone(
    tmp_path, caplog,
):
    """Issue #218 (subsumes #75): the model is expected to reply next
    (model_wait) and the session JSONL is frozen past the dead
    threshold — the model request is hung (here the connection is even
    gone: the old #75 dead-upstream scene). The runner must kill Pi and
    fail fast (the normal failure path releases the slot): an infinite
    `model_wait` hang is not acceptable. This is not a business timeout:
    it only fires while the session file is frozen (stale seconds),
    never while events keep arriving."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ) as excinfo:
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=75, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    # The failure message names the hung model request and the stale
    # time.
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#75" in failures[0]
    assert f"worktree={tmp_path}" in failures[0]
    # The structured model_wait_dead line carries the evidence: no
    # live connection here.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=false" in dead[0]
    assert "reason=hung_model_request" in dead[0]
    # The kill happened fast (well before the child's 10 s sleep).
    assert "pi_idle" not in caplog.text


def test_stream_pi_model_wait_dead_default_is_thirty_minutes():
    # Issue #228: the default dead-request threshold is 30 minutes
    # (1800 s) — a slow local model (27B Q4 GGUF, llama-server request
    # timeout 1200 s: ~57 tokens/s at 12K context on an RX 7900 XTX
    # with all layers on GPU, well under 20 tokens/s on partial GPU
    # offload or CPU-only) must survive a 10-minute complete-message
    # silence under the default; a genuinely frozen request is still
    # bounded and releases the slot.
    assert runner.PI_MODEL_WAIT_DEAD_SECONDS == 1800.0


def test_stream_pi_frozen_model_wait_just_before_default_survives(
    tmp_path, caplog,
):
    """Issue #228: a frozen model_wait of just under the 1800 s default
    (the #176/#175/#173/#168 scene: 10-minute complete messages on a
    slow local model) must NOT be killed under the default
    configuration — the Runner keeps waiting and the session finishes
    normally."""
    records = _frozen_model_wait_records(1799.0)
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=228, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text
    assert "model_wait_slow" not in caplog.text


def test_stream_pi_frozen_model_wait_at_default_kills_with_configured_threshold(
    tmp_path, caplog,
):
    """Issue #228: a frozen model_wait AT the 1800 s default still
    kills (the bound is inclusive) — and the structured line and the
    failure scene report the ACTUAL configured threshold (1800)."""
    records = _frozen_model_wait_records(1800.5)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=228, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "threshold=1800" in dead[0]
    assert "upstream_alive=" in dead[0]
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


def test_stream_pi_explicit_short_override_kills_before_default(
    tmp_path, caplog,
):
    """Issue #228: an explicit short override (a test setting) kills at
    the configured value, well before the 1800 s default — no real
    waiting, and the line reports the configured threshold."""
    records = _frozen_model_wait_records(1.5)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=228, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=1.0), cwd=tmp_path)
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "threshold=1" in dead[0]


def test_stream_pi_frozen_model_wait_at_ten_minutes_survives_default(
    tmp_path, caplog,
):
    """Issue #228 regression: the #176/#175/#173/#168 scene — a frozen
    model_wait at 600 seconds (10 minutes, the pre-#228 default that
    marked those Issues ai-blocked) survives under the new 1800 s
    default: the Runner keeps waiting and does not signal Pi."""
    records = _frozen_model_wait_records(600.0)
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=228, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text


def test_stream_pi_slow_model_still_generating_is_not_killed(
    tmp_path, caplog,
):
    """Issue #75: the kill only fires while the session file is FROZEN
    (stale seconds past the threshold in model_wait). A model that
    keeps producing session events is not dead: every new record
    resets the stale time, so a long slow generation — even with a
    model_wait window that would have crossed the threshold had the
    file stayed frozen — survives without a kill."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        # A new event arrives BEFORE the 0.4 s dead threshold: the
        # model is slow, not dead (the stale time resets).
        (0.3, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.8,
    )
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=75, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.4), cwd=tmp_path)
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text


def test_stream_pi_no_upstream_kill_before_model_wait(tmp_path, caplog):
    """Issue #75: the kill only applies while the model is expected to
    reply next (model_wait). A frozen session that is NOT waiting on
    the model (e.g. Pi itself stalled after an assistant message) is
    the existing `pi_idle` warning's territory, not a kill: the
    business task keeps running (no artificial timeout)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "thinking out loud"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=1.2)
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=75, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    assert "model_wait_dead" not in caplog.text


# --- Issue #169: evidence-based stall detection ------------------------------


@_stream_pi_linux_only
@_stream_pi_upstream_only
def test_stream_pi_hung_model_request_killed_despite_live_upstream(
    tmp_path, caplog,
):
    """Issue #218 (the #183 scene; supersedes the #169/#158
    connection-alive exemption): the model_wait silence crosses the
    dead threshold while the model request is still CONNECTED to the
    upstream (a live TCP connection) — but the session JSONL is frozen
    and the model service process is alive without ever answering
    ("process alive ≠ responding"). A live connection is evidence for
    the journal, never a veto: the runner kills Pi and fails fast with
    the `model_wait_dead` reason — the slot is never held forever."""
    port, stop = make_upstream_listener(drop=False)
    try:
        command = make_slow_model_pi(
            tmp_path, port=port, hold_seconds=30.0,
        )
        with caplog.at_level("WARNING"), pytest.raises(
            RuntimeError, match="hung",
        ) as excinfo:
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=218, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    finally:
        stop()
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#218" in failures[0]
    # The structured model_wait_dead line carries the evidence: the
    # connection was still alive (upstream_alive=true) — process
    # alive ≠ responding.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=true" in dead[0]
    assert "reason=hung_model_request" in dead[0]
    # The kill is the failure path: no idle warning, no recovery.
    assert " pi_idle " not in caplog.text
    assert " pi_idle_term " not in caplog.text


def test_stream_pi_model_wait_dead_line_fields(tmp_path, caplog):
    """Issue #218 acceptance: the recovery logs ONE structured,
    grep-able `model_wait_dead` line with the full scene (like
    `unit_drift` / `transport_check_failed`): issue, idle seconds, the
    threshold, the action, the session, the run id, the upstream
    connection evidence and the reason. The line is parseable with
    `pi_activity.parse_scene`."""
    records = [
        (0.0, {"type": "session", "id": "sess-218",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        pi_session.stream_pi(command, ctx=RunContext(run_id="ab12cd34", issue=218, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    fields = pi_activity.parse_scene(dead[0])
    assert fields["issue"] == "xqliu/orbi#218"
    assert fields["role"] == "implement"
    assert fields["action"] == "kill_pi"
    assert fields["session"] == "sess-218"
    assert fields["run_id"] == "ab12cd34"
    assert fields["upstream_alive"] == "false"
    assert fields["reason"] == "hung_model_request"
    assert fields["threshold"] == "0"
    assert int(fields["idle_seconds"]) >= 0


def test_stream_pi_dropped_connection_model_wait_dead_upstream_false(
    tmp_path, caplog,
):
    """Issue #218: the upstream closed the connection (the client
    socket left the live TCP states — the old #169 dead-upstream
    scene) and the model_wait silence crossed the threshold: the same
    `model_wait_dead` path kills Pi and fails fast, with the
    connection evidence recorded as `upstream_alive=false`."""
    port, stop = make_upstream_listener(drop=True)
    try:
        command = make_slow_model_pi(
            tmp_path, port=port, hold_seconds=3.0, drop_upstream=True,
        )
        with caplog.at_level("WARNING"), pytest.raises(
            RuntimeError, match="hung",
        ):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=169, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    finally:
        stop()
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#169" in failures[0]
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=false" in dead[0]
    # The kill is the failure path: no idle warning, no recovery.
    assert " pi_idle " not in caplog.text
    assert " pi_idle_term " not in caplog.text


# --- Issue #233: the /slots swallow probe kills fast -------------------------

def test_stream_pi_swallowed_model_request_killed_fast(tmp_path, caplog,
                                                        monkeypatch):
    """Issue #233 (the #231 scene): the model is expected to reply next
    (model_wait) and the /slots probe reports EVERY slot idle for the
    sustained grace — the request was accepted but never scheduled (the
    model process is alive, the connection ESTABLISHED, nothing
    generating). The runner kills Pi FAST (well before the
    model_wait_dead_seconds bound) and fails fast with the
    model_wait_swallowed reason."""
    monkeypatch.setattr(pi_process, "slots_idle", lambda url: True)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="swallowed",
    ) as excinfo:
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=10.0, model_wait_probe_url="http://127.0.0.1:18082/slots", model_wait_probe_seconds=0.5), cwd=tmp_path)
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_swallowed_idle_" in failures[0]
    assert "issue=xqliu/orbi#233" in failures[0]
    # The structured model_wait_swallowed line carries the evidence.
    swallowed = [line for line in lines if " model_wait_swallowed " in line]
    assert len(swallowed) == 1
    assert "reason=swallowed_model_request" in swallowed[0]
    assert "action=kill_pi" in swallowed[0]
    assert "probe_seconds=0" in swallowed[0]
    assert "upstream_alive=" in swallowed[0]
    # The fast path fired: the dead-request bound never did.
    assert " model_wait_dead " not in caplog.text


def test_stream_pi_swallow_line_fields(tmp_path, caplog, monkeypatch):
    """Issue #233 acceptance: the recovery logs ONE structured,
    grep-able `model_wait_swallowed` line with the full scene (like
    `model_wait_dead`): issue, idle seconds, the probe grace, the action,
    the session, the run id, the upstream connection evidence and the
    reason. The line is parseable with `pi_activity.parse_scene`."""
    monkeypatch.setattr(pi_process, "slots_idle", lambda url: True)
    records = [
        (0.0, {"type": "session", "id": "sess-233",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        pi_session.stream_pi(command, ctx=RunContext(run_id="ab12cd34", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=10.0, model_wait_probe_url="http://127.0.0.1:18082/slots", model_wait_probe_seconds=1.0), cwd=tmp_path)
    lines = caplog.text.splitlines()
    swallowed = [line for line in lines if " model_wait_swallowed " in line]
    assert len(swallowed) == 1
    fields = pi_activity.parse_scene(swallowed[0])
    assert fields["issue"] == "xqliu/orbi#233"
    assert fields["role"] == "implement"
    assert fields["action"] == "kill_pi"
    assert fields["session"] == "sess-233"
    assert fields["run_id"] == "ab12cd34"
    assert fields["reason"] == "swallowed_model_request"
    assert fields["probe_seconds"] == "1"
    assert int(fields["idle_seconds"]) >= 0


def test_stream_pi_swallow_not_fired_while_a_slot_is_processing(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233: the /slots probe reports a slot is processing
    (is_processing=true — the model is generating, a slow model, NOT a
    swallow): the swallow path never fires and the existing
    model_wait_dead_seconds bound still applies."""
    monkeypatch.setattr(pi_process, "slots_idle", lambda url: False)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5, model_wait_probe_url="http://127.0.0.1:18082/slots", model_wait_probe_seconds=0.2), cwd=tmp_path)
    lines = caplog.text.splitlines()
    # The dead-request bound fired, not the swallow probe.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


def test_stream_pi_swallow_window_restarts_when_events_arrive(
    tmp_path, caplog, monkeypatch,
):
    """事件持续到达的会话绝不能判 swallow（本函数自身的契约："Never
    fires while events keep arriving"）。turn 完整落在两次 poll 之间
    时，持续空闲窗口此前不重置——跨轮携带的窗口把"刚完成一轮健康
    请求"的活会话误杀（复现日志里 idle_seconds 远小于 probe_seconds）。
    事件每 0.4s 到达一次（< probe_seconds=0.6），窗口必须在每次事件
    后重启，探测永不触发，会话自然跑完。"""
    monkeypatch.setattr(pi_process, "slots_idle", lambda url: True)

    def tool_result(n):
        return {"type": "message", "id": f"r{n}",
                "timestamp": fresh_timestamp(n),
                "message": {"role": "toolResult", "toolCallId": f"t{n}",
                            "toolName": "bash",
                            "content": [{"type": "text", "text": "ok"}]}}
    # make_fake_pi 的 delay 是逐条累加的 sleep：0.3s 间隔的事件流
    # （< probe_seconds=0.6），结尾 0.4s 收尾——任何无事件间隙都小于
    # 探针宽限，窗口在每次事件后重启，永不触发。
    records = [
        (0.0, {"type": "session", "id": "sess-live",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.3, tool_result(1)),
        (0.3, tool_result(2)),
        (0.3, tool_result(3)),
        (0.3, tool_result(4)),
        (0.3, tool_result(5)),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=0.4)
    pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=10.0, model_wait_probe_url="http://127.0.0.1:18082/slots", model_wait_probe_seconds=0.6), cwd=tmp_path)
    assert " model_wait_swallowed " not in caplog.text
    assert " run_failed " not in caplog.text


def test_stream_pi_swallow_probe_failure_is_inconclusive(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233 (bypass, Issue #79): the /slots probe fails (returns
    None — network/JSON error): the probe is inconclusive, never an
    error, and the existing model_wait_dead_seconds bound still applies
    (the delivery is not failed by the probe)."""
    monkeypatch.setattr(pi_process, "slots_idle", lambda url: None)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5, model_wait_probe_url="http://127.0.0.1:18082/slots", model_wait_probe_seconds=0.2), cwd=tmp_path)
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text


def test_stream_pi_unconfigured_probe_keeps_dead_bound(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233: with no probe URL configured the swallow path is a
    no-op (the exact pre-#233 behavior) — the run is bounded by
    model_wait_dead_seconds only, and the probe is never called."""
    probe = Mock(return_value=True)
    monkeypatch.setattr(pi_process, "slots_idle", probe)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=233, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    # The probe was never called (no URL configured).
    probe.assert_not_called()
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text


@_stream_pi_linux_only
def test_stream_pi_timeout_tool_inside_deadline_not_killed(
    tmp_path, caplog,
):
    """Issue #169 acceptance 2 (the #105 regression): a pre-idle
    descendant running `timeout <seconds> ...` INSIDE its deadline is
    a legitimately running tool, not a hung one — the runner logs
    `pi_idle_wait` (the evidence: pid, cmdline, deadline) instead of
    TERMed it, and the run SUCCEEDS when the tool reaches its deadline
    on its own."""
    # Issue #741: the earlier real-clock version ran a 2.5 s tool
    # against a 1.0 s idle window and needed a poll to land inside the
    # [1.0, 2.5] real-time detection window; under load one poll-loop
    # stall skipped the whole window, `pi_idle_wait` never fired (the
    # escalation correctly logged `no_target` instead) and the test
    # failed on healthy code. The release-gated tool makes the
    # ordering deterministic: the tool cannot die before the runner's
    # wait decision, because the test releases it exactly when the
    # progress callback first sees `recovery=wait`.
    release = tmp_path / "release-tool"
    command = make_timeout_tool_pi_release_gated(tmp_path, release=release)
    released = []

    def progress(activity):
        if activity.get("recovery") == "wait" and not released:
            released.append(True)
            release.write_text("go", encoding="utf-8")

    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=105, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.2, idle_warn_seconds=1.0), cwd=tmp_path, progress=progress)
    assert result == ""
    assert released, "the wait decision reached the progress callback"
    lines = caplog.text.splitlines()
    waits = [line for line in lines if " pi_idle_wait " in line]
    assert len(waits) == 1, f"exactly one wait decision: {lines}"
    wait = waits[0]
    assert "run=deadbeef" in wait
    assert "issue=xqliu/orbi#105" in wait
    assert "pid=" in wait
    assert "cmdline=" in wait
    assert "deadline=" in wait
    # No signal was ever DELIVERED: the tool stayed inside its nominal
    # deadline until the release, and the escalation only ever observed
    # the pending target (a `no_target` record is fine — it means the
    # tool was already gone when a window re-evaluated).
    terms = [line for line in lines if " pi_idle_term " in line]
    assert not any("result=sent" in line for line in terms), lines
    assert " pi_idle_kill " not in caplog.text
    assert "run_failed" not in caplog.text
    # The session resumed when the tool's result arrived.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1


@_stream_pi_linux_only
def test_stream_pi_timeout_tool_deadline_grace_window_not_killed(
    tmp_path, caplog,
):
    """Issue #181 regression (deterministic): the nominal `timeout`
    deadline is BEST-EFFORT — the wrapper's alarm fires at
    `start + duration`, but the signal delivery and the exit take a
    finite, under load unbounded, time. A single "past deadline and
    still alive" observation must NOT escalate: the runner records the
    target in the window its nominal deadline passes (the grace
    window, nothing is signaled) and re-evaluates in the next window.

    Deterministic margins (no real-scheduling coincidence, poll
    0.1 s, idle window 0.3 s): the nominal deadline (0.2 s) has
    passed at least 0.1 s before the first escalation window (the
    window opens at 0.3 s of stale, the first escalation poll is at
    t∈[0.35, 0.45)), so the grace window always sees a past-deadline
    target that is still alive (the wrapper's deadline handling never
    finishes: the control file is never created). The grace window
    signals NOTHING; the flip window (one full idle window later, the
    target still alive) TERMs it — the wrapper failed to end the
    command, the evidence is confirmed, the escalation runs to the
    bounded session kill. Without the grace window the runner TERMs
    the tool in the FIRST escalation window (the CI flake this test
    pins): the assertion below distinguishes the two — the first
    `pi_idle_term` line must be the flip window's, i.e. at least one
    full idle window after the window opened."""
    # The control file is never created: the wrapper's deadline
    # handling never finishes (the wrapper is broken), the tool
    # outlives the nominal deadline forever.
    control = tmp_path / "tool-deadline-fired"
    command = make_timeout_tool_pi_controlled(
        tmp_path, tool_seconds=0.2, control=control,
    )
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=105, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path, progress=progress)
    # The tool was TERMed by the runner (the wrapper failed to end it:
    # still alive one full idle window after the deadline), the fake
    # Pi resumed when the tool died, and the run SUCCEEDED.
    assert result == ""
    # The `recovery` progress state is observable per poll: the grace
    # window reports `wait` (nothing was signaled) BEFORE the flip
    # window reports `term`. Without the grace window the first
    # escalation window would report `term` directly (the CI flake
    # this test pins).
    assert "wait" in seen, seen
    first_wait = seen.index("wait")
    assert "term" in seen, seen
    first_term = seen.index("term")
    assert first_wait < first_term, seen
    # No `term` before the first `wait` (the grace window never
    # signaled).
    assert not any(state == "term" for state in seen[:first_wait]), seen
    lines = caplog.text.splitlines()
    # The deadline had clearly passed when the window opened: no wait
    # decision at all.
    assert " pi_idle_wait " not in caplog.text
    # The flip window delivered the signal (the wrapper failed to end
    # the command: still alive one full idle window after the
    # deadline).
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) >= 1, lines
    assert "result=sent" in terms[0], lines
    # The session resumed when the tool's death reached the fake Pi.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) >= 1, lines


@_stream_pi_linux_only
def test_stream_pi_timeout_tool_past_deadline_still_terminated(
    tmp_path, caplog,
):
    """Issue #169 constraint + Issue #181 grace: the wait is bounded —
    when the `timeout` deadline passed but the descendant is STILL
    alive one full idle window later (the wrapper failed to end the
    command), the evidence is confirmed: the TERM → KILL →
    session-kill escalation runs (the slot is never held forever, no
    silent ignoring of the timeout). The tool deadline (0.1 s) lands
    clearly BEFORE the idle window opens (0.3 s of stale), so the
    first escalation window already sees a past deadline: it is the
    grace window (no signal, Issue #181), and the NEXT window — the
    target still alive — delivers the TERM."""
    command = make_timeout_tool_pi(
        tmp_path, tool_seconds=0.1, react_on_tool_done=False,
        wrapper_honors_deadline=False,
    )
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=105, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
    lines = caplog.text.splitlines()
    # The first escalation window already saw a past deadline (the
    # wrapper failed to end the command): no wait decision at all.
    assert " pi_idle_wait " not in caplog.text
    # The escalation ran: the grace window signaled nothing, and the
    # next window (the target still alive one full idle window after
    # the deadline) TERMed the pre-idle descendants (one line per
    # target, at least one delivered)...
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) >= 1
    assert any("result=sent" in line for line in terms)
    # ...and after the bounded cycles the session was killed.
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_state_wait_visible_in_progress_callback(
    tmp_path,
):
    """Issue #169: the live GitHub progress comment shows the wait
    state — the activity state passed to the progress callback carries
    `recovery=wait` while the runner waits for the tool's deadline."""
    command = make_timeout_tool_pi(tmp_path, tool_seconds=0.6)
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=105, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path, progress=progress)
    assert "wait" in seen
    assert "term" not in seen
    assert seen[-1] is None


@_stream_pi_linux_only
def test_stream_pi_wait_state_cleared_when_waited_tool_exits(
    tmp_path, caplog,
):
    """Issue #169: the wait evidence FLIPS when the waited tool reaches
    its own deadline and exits while Pi stays silent (Pi itself is
    stuck): the next window finds no pre-idle descendants (`no_target`),
    the stale `wait` state is cleared (the progress callback must not
    keep reporting `recovery=wait` after the tool is gone), and the
    escalation continues to the bounded session kill."""
    command = make_timeout_tool_pi(
        tmp_path, tool_seconds=0.6, react_on_tool_done=False,
    )
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=105, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path, progress=progress)
    lines = caplog.text.splitlines()
    waits = [line for line in lines if " pi_idle_wait " in line]
    assert len(waits) == 1, lines
    # The tool exited on its own deadline: the next window finds no
    # pre-idle descendants (`no_target`, nothing was signaled)...
    terms = [line for line in lines if " pi_idle_term " in line]
    assert any("result=no_target" in line for line in terms), lines
    assert not any("result=sent" in line for line in terms), lines
    # ...and the stale wait state is cleared: no `wait` is reported
    # after the no_target window (the escalation keeps running).
    no_target_index = next(
        i for i, line in enumerate(lines)
        if " pi_idle_term " in line and "result=no_target" in line
    )
    wait_after = [
        line for line in lines[no_target_index:]
        if " pi_idle_wait " in line
    ]
    assert not wait_after, lines
    # The poll right after the last `wait` is the no_target window:
    # the stale state is cleared (None), and the escalation continues
    # (`kill`) — no `wait` is ever reported after the tool is gone.
    last_wait = max(i for i, state in enumerate(seen) if state == "wait")
    assert seen[last_wait + 1] is None, seen
    assert all(state != "wait" for state in seen[last_wait + 1:]), seen
    assert "kill" in seen[last_wait + 1:], seen
    # The escalation still ends in the bounded session kill.
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


def test_pending_timeout_targets_unit(monkeypatch):
    """Issue #169: the clock-consistent pending computation — a target
    without a clear timeout is not pending, a target inside its deadline
    is pending with the remaining time, and a target whose deadline
    already passed is not pending. The age comes from the target's
    recorded start ticks (stat field 22, Issue #1808): no /proc read and
    no other clock than CLOCK_BOOTTIME."""
    hz = pi_process.clk_tck()
    now_mono = time.monotonic()
    ago = (now_mono - 1.0) * hz  # started 1 s ago: inside a timeout 5
    old = (now_mono - 9.0) * hz  # started 9 s ago: past a timeout 5

    monkeypatch.setattr(pi_process, "process_ppid", lambda pid: None)
    boot_clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    monkeypatch.setattr(
        pi_process.time, "clock_gettime",
        lambda clock: now_mono if clock == boot_clock else pytest.fail(
            f"unexpected clock: {clock}"
        ),
    )
    targets = [
        {"pid": 2, "cmdline": "timeout 5 pytest", "start_ticks": ago},
        {"pid": 3, "cmdline": "timeout 5 pytest", "start_ticks": old},
        {"pid": 4, "cmdline": "pytest", "start_ticks": ago},
    ]
    pending = pi_process._pending_timeout_targets(targets)
    assert [target["pid"] for target, _ in pending] == [2]
    # The deadline is the realtime now plus the remaining time.
    (target, deadline) = pending[0]
    assert target["pid"] == 2
    assert time.time() + 3.0 < deadline < time.time() + 5.0
    # A child process delegated to the timeout wrapper inherits its
    # wrapper's deadline, even though its own command line has no
    # `timeout` token.
    monkeypatch.setattr(
        pi_process, "process_ppid", lambda pid: 10 if pid == 11 else None,
    )
    pending = pi_process._pending_timeout_targets([
        {"pid": 10, "cmdline": "timeout 5 sleep 300", "start_ticks": ago},
        {"pid": 11, "cmdline": "sleep 300", "start_ticks": ago},
    ])
    assert [target["pid"] for target, _ in pending] == [10]

    # An empty target list is a no-op.
    assert pi_process._pending_timeout_targets([]) == []


def test_idle_recovery_cycles_match_absolute_cap():
    """The watchdog exhaustion window and declared deadline share one cap."""
    assert (pi_process.PI_IDLE_RECOVERY_CYCLES
            * pi_process.PI_IDLE_WARN_SECONDS
            == pi_process.PI_IDLE_WAIT_MAX_SECONDS)


def test_idle_recovery_tracker_escalation_pacing(monkeypatch, caplog):
    """Issue #287 (the extracted strategy, window pacing): a surviving
    target's first observation is the grace window (recorded, nothing
    signaled — Issue #181), the flip window TERMs it, the SAME window
    never escalates further, the next window SIGKILLs it, and the
    absolute-cap window flips `exhausted` — the loop's signal to kill
    the Pi session."""
    state = make_idle_recovery_tracker(monkeypatch)
    tracker = state["tracker"]
    with caplog.at_level("WARNING"):
        assert tracker.state is None
        assert tracker.exhausted is False
        tracker.open_window(7)
        # Window 1: the first observation — the grace window, no signal
        # (Issue #181: one "alive" observation is not evidence).
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        # Same window (silence still below the window length): the
        # target is inside its grace cycle — still nothing signaled.
        state["mono"] += 0.1
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        # Window 2: the evidence flips (still alive one full window) —
        # the TERM.
        state["mono"] += 0.25
        tracker.escalate()
        assert tracker.state == "term"
        assert state["signals"] == [(4242, signal.SIGTERM)]
        # Window 3: the KILL; exhaustion remains false until the
        # absolute-cap window. The clock moves a hair past one
        # window: an exact `+0.3` floors back into window 1 after the
        # floating-point subtraction in `escalate`.
        state["mono"] += 0.31
        tracker.escalate()
        assert tracker.state == "kill"
        assert state["signals"][-1] == (4242, signal.SIGKILL)
        assert tracker.exhausted is False
        for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 3):
            state["mono"] += 0.31
            tracker.escalate()
        assert tracker.exhausted is True
    # The journal lines carry the run correlation fields.
    term = [line for line in caplog.text.splitlines()
            if " pi_idle_term " in line]
    assert len(term) == 1 and "result=sent" in term[0]
    assert "run=deadbeef" in term[0]
    assert "issue=xqliu/orbi#94" in term[0]
    assert "pid=4242" in term[0]
    assert 'cmdline="sleep 300"' in term[0]
    kill = [line for line in caplog.text.splitlines()
            if " pi_idle_kill " in line]
    assert len(kill) == 1 and "result=sent" in kill[0]


def test_idle_recovery_tracker_kill_step_skipped_same_window(monkeypatch):
    """Issue #287: after a no_target TERM step (`result=no_target`
    advances the step without signaling) the KILL step still waits for
    the NEXT window — the escalation is paced in idle windows, never in
    polls."""
    state = make_idle_recovery_tracker(monkeypatch, targets=[])
    tracker = state["tracker"]
    tracker.open_window(7)
    tracker.escalate()  # window 1: no_target, step -> 1
    assert tracker.state is None
    tracker.escalate()  # still window 1: the KILL step must not run
    assert tracker.state is None
    state["mono"] += 0.31  # a hair past one window (see the pacing test)
    tracker.escalate()  # window 2: the (empty) KILL step
    assert tracker.state == "kill"


def test_idle_recovery_tracker_kill_reports_already_dead(
    monkeypatch, caplog,
):
    """Issue #287: a target the TERM already killed between polls is
    reported `result=already_dead` in the KILL window and is not
    signaled again."""
    state = make_idle_recovery_tracker(monkeypatch)
    tracker = state["tracker"]
    monkeypatch.setattr(pi_process, "pid_alive", lambda pid: False)
    with caplog.at_level("WARNING"):
        tracker.open_window(7)
        tracker.escalate()  # window 1: the grace observation
        state["mono"] += 0.35
        tracker.escalate()  # window 2: the flip TERM
        state["mono"] += 0.3
        tracker.escalate()  # window 3: the KILL step
        assert tracker.state == "kill"
        assert tracker.exhausted is False
        for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 3):
            state["mono"] += 0.31
            tracker.escalate()
        assert tracker.exhausted is True
    assert state["signals"] == [(4242, signal.SIGTERM)]
    kill = [line for line in caplog.text.splitlines()
            if " pi_idle_kill " in line]
    assert len(kill) == 1
    assert "result=already_dead" in kill[0]


def test_idle_recovery_tracker_reset_clears_state(monkeypatch):
    """Issue #287: `reset()` (the loop calls it when a session event
    ends the stall) clears the window, the displayed state, the tracked
    targets AND the exhaustion decision — a later stall starts a fresh
    escalation at the grace window, not at the session kill."""
    state = make_idle_recovery_tracker(monkeypatch)
    tracker = state["tracker"]
    tracker.open_window(7)
    state["mono"] += 0.9  # straight to cycle 3
    tracker.escalate()
    assert tracker.exhausted is False
    for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 3):
        state["mono"] += 0.31
        tracker.escalate()
    assert tracker.exhausted is True
    tracker.reset()
    assert tracker.state is None
    assert tracker.exhausted is False
    state["signals"].clear()
    # A fresh window escalates from the grace window again (no signal
    # — the old escalation left no residue).
    tracker.open_window(7)
    tracker.escalate()
    assert tracker.state == "wait"
    assert state["signals"] == []
    assert tracker.exhausted is False


def test_idle_recovery_tracker_wait_decision_logged_once_then_flip(
    monkeypatch, caplog,
):
    """Issue #287 (the #105/#169/#181 evidence chain, strategy level):
    a target inside its `timeout` deadline is WAITED for — one
    `pi_idle_wait` decision line, no signal; past the deadline the
    first observation is the grace window (recorded, no signal, no
    second wait line); one full window later the flip delivers the
    TERM."""
    target = {"pid": 7, "cmdline": "timeout 5 sleep 300",
              "start_ticks": 1}
    pending = [(target, 9999999999.0)]  # inside the deadline
    state = make_idle_recovery_tracker(
        monkeypatch, targets=[target], pending=pending,
    )
    tracker = state["tracker"]
    with caplog.at_level("WARNING"):
        tracker.open_window(7)
        # Window 1: inside the deadline — the wait decision, once.
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        # Same window, deadline now passed: the grace window — the
        # target is recorded, nothing is signaled, no second wait line.
        pending.clear()
        state["mono"] += 0.1
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        # One full window later, still alive: the evidence flips, TERM.
        state["mono"] += 0.25
        tracker.escalate()
        assert tracker.state == "term"
        assert state["signals"] == [(7, signal.SIGTERM)]
    waits = [line for line in caplog.text.splitlines()
             if " pi_idle_wait " in line]
    assert len(waits) == 1, caplog.text
    assert "pid=7" in waits[0]
    assert "deadline=" in waits[0]


def test_idle_recovery_tracker_wait_holds_exhaustion(monkeypatch):
    """Issue #1089: while a pre-idle descendant is inside its `timeout`
    deadline the idle windows pass but the session is NOT exhausted —
    the wait pauses the exhaustion counter too, not only the signals.
    A review tool wrapped in `timeout 1200` (longer than
    `PI_IDLE_RECOVERY_CYCLES` windows) must survive every one of them."""
    target = {"pid": 9,
              "cmdline": "timeout 1200 go test -c ./pkg/cli/server/",
              "start_ticks": 1}
    pending = [(target, 9999999999.0)]  # inside the deadline
    state = make_idle_recovery_tracker(
        monkeypatch, targets=[target], pending=pending,
    )
    tracker = state["tracker"]
    tracker.open_window(7)
    for _ in range(2 * pi_process.PI_IDLE_RECOVERY_CYCLES):
        state["mono"] += 0.31  # a hair past one window (see the pacing test)
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        assert tracker.exhausted is False


def test_idle_recovery_tracker_wait_releases_to_escalation(monkeypatch):
    """Issue #1089: when the `timeout` deadline passes and the
    descendant is still alive, the evidence flip is unchanged — the
    grace window records it, one full window later the TERM lands, the
    next window the KILL lands and `exhausted` flips (the slot is never
    held past the wrapper deadline + two windows)."""
    target = {"pid": 9,
              "cmdline": "timeout 1200 go test -c ./pkg/cli/server/",
              "start_ticks": 1}
    pending = [(target, 9999999999.0)]  # inside the deadline
    state = make_idle_recovery_tracker(
        monkeypatch, targets=[target], pending=pending,
    )
    tracker = state["tracker"]
    tracker.open_window(7)
    # Three full windows inside the deadline: wait, no signal, no kill.
    for _ in range(3):
        state["mono"] += 0.31
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        assert tracker.exhausted is False
    # The deadline passes, the tool is still alive: the grace window
    # (recorded, nothing signaled, still no exhaustion).
    pending.clear()
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "wait"
    assert state["signals"] == []
    assert tracker.exhausted is False
    # One full window later: the evidence flips — the TERM.
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "term"
    assert state["signals"] == [(9, signal.SIGTERM)]
    # The next window: the KILL, and the same exhausted decision — the
    # loop kills the Pi session.
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "kill"
    assert state["signals"][-1] == (9, signal.SIGKILL)
    assert tracker.exhausted is False
    for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 3):
        state["mono"] += 0.31
        tracker.escalate()
    assert tracker.exhausted is True


def test_idle_recovery_tracker_wait_capped(monkeypatch):
    """Issue #1089: the wait is bounded — a declared `timeout` longer
    than `PI_IDLE_WAIT_MAX_SECONDS` is honored only up to the cap since
    the wait began, then the existing grace → TERM → KILL escalation
    runs unchanged: a wrapper like `timeout 86400` can never hold the
    concurrency slot for a day."""
    target = {"pid": 11,
              "cmdline": "timeout 86400 sleep 300",
              "start_ticks": 1}
    pending = [(target, 9999999999.0)]  # the declared deadline: far out
    state = make_idle_recovery_tracker(
        monkeypatch, targets=[target], pending=pending,
    )
    monkeypatch.setattr(pi_process, "PI_IDLE_WAIT_MAX_SECONDS", 0.9)
    tracker = state["tracker"]
    tracker.open_window(7)
    # Inside the cap: the wait holds — no signal, no exhaustion.
    for _ in range(3):
        state["mono"] += 0.31
        tracker.escalate()
        assert tracker.state == "wait"
        assert state["signals"] == []
        assert tracker.exhausted is False
    # The cap is consumed: the declared deadline is no longer honored —
    # the grace window records the still-alive target (nothing
    # signaled, still no exhaustion).
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "wait"
    assert state["signals"] == []
    assert tracker.exhausted is False
    # One full window later: the TERM — the wrapper failed to end the
    # command inside the capped deadline.
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "term"
    assert state["signals"] == [(11, signal.SIGTERM)]
    # The next window: the KILL and the exhaustion flip — the slot is
    # released.
    state["mono"] += 0.31
    tracker.escalate()
    assert tracker.state == "kill"
    assert state["signals"][-1] == (11, signal.SIGKILL)
    assert tracker.exhausted is False
    for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 6):
        state["mono"] += 0.31
        tracker.escalate()
    assert tracker.exhausted is True


def test_idle_recovery_tracker_no_target_clears_state(monkeypatch, caplog):
    """Issue #287: with NO pre-idle descendants (Pi itself is stuck)
    the TERM step logs `result=no_target` without a pid, the displayed
    state is cleared (not `wait`/`term`), and the escalation still
    advances to the bounded session kill."""
    state = make_idle_recovery_tracker(monkeypatch, targets=[])
    tracker = state["tracker"]
    with caplog.at_level("WARNING"):
        tracker.open_window(7)
        tracker.escalate()
        assert tracker.state is None
        assert state["signals"] == []
        state["mono"] += 0.31  # a hair past one window
        tracker.escalate()
        assert tracker.state == "kill"
        state["mono"] += 0.31
        tracker.escalate()
        assert tracker.exhausted is False
        for _ in range(pi_process.PI_IDLE_RECOVERY_CYCLES - 2):
            state["mono"] += 0.31
            tracker.escalate()
        assert tracker.exhausted is True
    terms = [line for line in caplog.text.splitlines()
             if " pi_idle_term " in line]
    assert len(terms) == 1
    assert "result=no_target" in terms[0]
    assert " pid=" not in terms[0]


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_terms_hung_descendant_and_resumes(
    tmp_path, caplog,
):
    """Issue #94 acceptance 1 (TERM 成功恢复): a stalled session
    (no model/session activity past `idle_warn_seconds`, not
    model_wait) with a hung descendant that existed before the idle
    window gets SIGTERM'd — the journal line carries run_id, pid,
    cmdline and result — the fake Pi gets the non-zero exit, writes
    the failure toolResult and the run RESUMES and SUCCEEDS (the
    failure signal reached the model)."""
    command = make_hung_pi(tmp_path)
    with caplog.at_level("INFO"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
    assert result == ""
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    assert len(idles) == 1  # the existing warning, once
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) == 1, f"exactly one TERM step: {lines}"
    term = terms[0]
    assert "run=deadbeef" in term
    assert "issue=xqliu/orbi#94" in term
    assert "role=implement" in term
    assert "pid=" in term
    assert "cmdline=" in term
    assert "result=sent" in term
    # The TERM came after the warning...
    assert lines.index(term) > lines.index(idles[0])
    # ...and the model got the failure signal: the fake Pi wrote the
    # error toolResult (visible as result=error) and resumed.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1
    assert lines.index(resumed[0]) > lines.index(term)
    assert "result=error" in caplog.text
    # The run SUCCEEDED: no failure, no session kill.
    assert "run_failed" not in caplog.text
    # No KILL escalation: the TERM worked within one idle cycle.
    assert " pi_idle_kill " not in caplog.text


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_kills_descendant_that_ignores_term(
    tmp_path, caplog,
):
    """Issue #94 acceptance 2 (TERM 后仍存活升级 KILL): a descendant
    that ignores SIGTERM is still alive one idle cycle later — the
    runner SIGKILLs it (journal line with pid, cmdline, result), the
    fake Pi reacts to the death and the run resumes."""
    command = make_hung_pi(
        tmp_path, ignore_sigterm=True, react_on_child_death=True,
    )
    with caplog.at_level("INFO"):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    kills = [line for line in lines if " pi_idle_kill " in line]
    assert len(terms) == 1 and "result=sent" in terms[0]
    assert len(kills) == 1, f"exactly one KILL step: {lines}"
    kill = kills[0]
    assert "run=deadbeef" in kill
    assert "pid=" in kill
    assert "cmdline=" in kill
    assert "result=sent" in kill  # the SIGKILL was delivered
    # The KILL came one idle cycle after the TERM...
    assert lines.index(kill) > lines.index(terms[0])
    # ...and the run still recovered (the fake Pi reacted to the death).
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1
    assert "run_failed" not in caplog.text


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_kills_pi_session_after_three_idle_cycles(
    tmp_path, caplog,
):
    """Issue #94 acceptance 3 (连续 idle 升级终止会话): the hung tool
    died from the TERM but the session NEVER resumes (Pi itself is
    stuck) — after three idle cycles the runner kills the Pi session
    itself and fails fast: `run_failed` carries the full scene and
    `reason=idle_recovery_stale_...`, the error names the idle
    recovery, and the normal `ai-blocked` flow takes over (the slot is
    never held forever)."""
    command = make_hung_pi(tmp_path, react_on_child_death=False)
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    kills = [line for line in lines if " pi_idle_kill " in line]
    assert len(terms) == 1 and "result=sent" in terms[0]
    # The TERM worked (the child died) — the KILL step reports the
    # target as already dead instead of signaling again.
    assert len(kills) == 1
    assert "result=already_dead" in kills[0]
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    failure = failures[0]
    assert "reason=idle_recovery_stale_" in failure
    assert "run=deadbeef" in failure
    assert "issue=xqliu/orbi#94" in failure
    assert f"worktree={tmp_path}" in failure


def test_stream_pi_idle_recovery_without_descendants_still_terminates(
    tmp_path, caplog,
):
    """Issue #94: a stalled session with NO pre-idle descendants (Pi
    itself is stuck, no hung tool) still escalates: the TERM step logs
    `result=no_target` (nothing was signaled — no pid), there is no
    KILL step, and after three idle cycles the session is killed and
    the run fails fast."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "stuck thinking"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) == 1
    assert "result=no_target" in terms[0]
    assert " pid=" not in terms[0]
    assert " pi_idle_kill " not in caplog.text
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_never_signals_non_descendants(
    tmp_path, caplog,
):
    """Issue #94 constraint (非 pi 后代不被误杀): a long-running
    process that is NOT a descendant of the Pi process (parented by
    the test itself) is never signaled — the ppid chain is the only
    target criterion."""
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        command = make_hung_pi(tmp_path)
        with caplog.at_level("INFO"):
            pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path)
        # The bystander is untouched: still running, and its pid never
        # appears on a recovery line.
        assert bystander.poll() is None
        for line in caplog.text.splitlines():
            if " pi_idle_term " in line or " pi_idle_kill " in line:
                assert f"pid={bystander.pid}" not in line
    finally:
        bystander.kill()
        bystander.wait()


def test_stream_pi_idle_recovery_never_fires_during_model_wait(
    tmp_path, caplog,
):
    """Issue #94: the recovery is the non-model_wait territory (same
    gate as the `pi_idle` warning). A frozen model_wait with a hung
    descendant is the Issue #218 hung-model-request case: the runner
    kills Pi via the `model_wait_dead` path and never TERMs the
    descendant."""
    command = make_hung_pi(tmp_path, model_wait=True)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="hung",
    ):
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, model_wait_dead_seconds=0.5), cwd=tmp_path)
    assert " pi_idle_term " not in caplog.text
    assert " pi_idle_kill " not in caplog.text
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


@_stream_pi_linux_only
def test_stream_pi_idle_recovery_state_visible_in_progress_callback(
    tmp_path,
):
    """Issue #94: the live GitHub progress comment is synced with the
    recovery — the activity state passed to the progress callback
    carries `recovery` (None -> `term` while the TERMed tool is being
    waited on -> back to None when the session resumes)."""
    command = make_hung_pi(tmp_path)
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=94, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1, idle_warn_seconds=0.3), cwd=tmp_path, progress=progress)
    assert "term" in seen
    # Before the stall and after the resume the state is None again.
    assert seen[0] is None
    assert seen[-1] is None
    assert seen.index("term") > 0


def test_stream_pi_times_out_and_kills_process(tmp_path, caplog):
    command = make_fake_pi(tmp_path, session_records=[], sleep=10.0)
    with caplog.at_level("ERROR"), pytest.raises(
        subprocess.TimeoutExpired,
    ) as excinfo:
        pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, timeout=0.5)
    assert excinfo.value.timeout == 0.5
    # The exception must not carry the raw command (prompt / Issue body).
    assert "sleep" not in str(excinfo.value)
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=timeout_0.5s" in failures[0]
    assert "issue=xqliu/orbi#24" in failures[0]


def test_drain_stream_returns_true_at_eof_and_captures_content():
    """Issue #709 contract, happy arm: EOF ends the drain with True and
    every written chunk is captured."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"chunk-one")
    os.write(write_fd, b" chunk-two")
    os.close(write_fd)
    chunks: list[bytes] = []
    try:
        with os.fdopen(read_fd, "rb") as stream:
            drained = pi_process._drain_stream(stream, chunks, silence_s=5.0)
    finally:
        pass
    assert drained is True
    assert b"".join(chunks) == b"chunk-one chunk-two"


def test_drain_stream_abandons_when_grandchild_holds_write_end():
    """Issue #709 contract, abandonment arm: Pi's tool grandchild
    inherits the pipe write end and outlives Pi — the drain must give up
    after the silence bound (capturing what arrived) instead of blocking
    until the grandchild happens to exit. On the pre-#709 code this test
    blocks for the holder's entire lifetime."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"partial-verdict")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import time; time.sleep(40)  # orbi709-holder-one"],
        pass_fds=(write_fd,),
    )
    os.close(write_fd)  # the writer (Pi) is dead; only the holder remains
    chunks: list[bytes] = []
    try:
        with os.fdopen(read_fd, "rb") as stream:
            started = time.monotonic()
            drained = pi_process._drain_stream(stream, chunks, silence_s=1.0)
            elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()
    assert b"partial-verdict" in b"".join(chunks)
    assert drained is False
    assert elapsed < 15, elapsed


def test_stream_pi_journals_drain_abandonment_when_grandchild_holds_pipe(
    tmp_path, caplog, monkeypatch,
):
    """Issue #709 end to end: the fake Pi spawns a tool grandchild that
    inherits stdout and outlives it — the run still completes with the
    captured verdict and the abandonment is journaled instead of the
    tick wedging inside the finally block."""
    monkeypatch.setattr(pi_process, "_DRAIN_SILENCE_S", 1.0)
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    wrapper = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "    'import time; time.sleep(40)  # orbi709-holder-two'])\n"
        f"exec({command[2]!r})\n"
    )
    try:
        with caplog.at_level("WARNING"):
            result = pi_session.stream_pi([sys.executable, "-c", wrapper], ctx=RunContext(run_id="deadbeef", issue=24, branch="orbi/xqliu-orbi-issue-24", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    finally:
        subprocess.run(["pkill", "-f", "orbi709-holder-two"], check=False)
    assert result == "final answer"
    assert "pi_drain_abandoned" in caplog.text
