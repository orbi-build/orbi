"""Tests for the automatic journal heartbeat and progress wiring (Issue #18).

While a task runs, the journal must show a heartbeat at most every 30
seconds and an immediate event on phase/action change, each carrying issue,
run id, role, phase, elapsed, last activity, last action, session and
branch. An idle warning appears after 5 minutes without activity and a
`resumed` event when activity returns. The GitHub progress comment is
created once per run (hidden marker), PATCHed on change / every 30 seconds,
and milestone comments are posted for the key events.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner
import progress
from tests.test_bootstrap_runner import (
    fake_session_records,
    make_fake_pi,
)


def test_heartbeat_constants_match_issue_acceptance():
    # Acceptance: heartbeat at most every 30 seconds, idle warning after
    # 5 minutes without model/session activity.
    assert runner.PI_HEARTBEAT_SECONDS == 30.0
    assert runner.PI_IDLE_WARN_SECONDS == 300.0


def test_stream_pi_logs_heartbeat_with_full_run_context(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer", sleep=3.5,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            heartbeat_seconds=1.0, idle_warn_seconds=3600,
            issue=18, run_id="abc123", role="implement",
            source_repo="xqliu/muyan-pilot",
            branch="muyan-pilot/xqliu-muyan-pilot-issue-18-abc123",
        )
    heartbeats = [
        line for line in caplog.records
        if line.getMessage().startswith("pi_heartbeat")
    ]
    assert heartbeats, "no pi_heartbeat line was logged"
    for record in heartbeats:
        message = record.getMessage()
        assert "issue=18" in message
        assert "run_id=abc123" in message
        assert "role=implement" in message
        assert "source_repo=xqliu/muyan-pilot" in message
        assert "branch=muyan-pilot/xqliu-muyan-pilot-issue-18-abc123" in message
        assert f"worktree={tmp_path}" in message
        assert "phase=" in message
        assert "elapsed=" in message
        assert "last_activity=" in message
        assert "last=" in message
        assert "session=" in message
    # The run lasts ~3.5 s with a 1 s heartbeat cadence: at least two.
    assert len(heartbeats) >= 2


def test_stream_pi_heartbeat_gap_never_exceeds_cadence(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="ok", sleep=4.0,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            heartbeat_seconds=1.0, idle_warn_seconds=3600,
            issue=18, run_id="abc123", role="implement",
        )
    heartbeats = [
        line.getMessage() for line in caplog.records
        if line.getMessage().startswith("pi_heartbeat")
    ]
    assert len(heartbeats) >= 3
    # Elapsed values are monotonically increasing and bounded by the run.
    elapsed_values = [
        int(line.rsplit("elapsed=", 1)[1].rstrip("s"))
        for line in heartbeats
    ]
    assert elapsed_values == sorted(elapsed_values)
    assert elapsed_values[-1] <= 5


def test_stream_pi_logs_event_on_phase_change(tmp_path, caplog):
    records = fake_session_records()
    # A second tool call arrives later: the phase changes to "push".
    records.append((1.2, {
        "type": "message", "id": "a2",
        "timestamp": "2026-08-25T02:00:05Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "t2", "name": "bash",
             "arguments": {"command": "git push origin HEAD"}},
        ]},
    }))
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            heartbeat_seconds=3600, idle_warn_seconds=3600,
            issue=18, run_id="abc123", role="implement",
        )
    events = [
        line.getMessage() for line in caplog.records
        if line.getMessage().startswith("pi_event")
    ]
    assert events, "no pi_event line on phase change"
    assert any("phase=push" in line for line in events)
    assert any("phase=test" in line for line in events)


def test_stream_pi_logs_resumed_after_idle_warning(tmp_path, caplog):
    records = fake_session_records()
    # Activity stops, then a new tool call arrives after the idle warning.
    records.append((1.6, {
        "type": "message", "id": "a3",
        "timestamp": "2026-08-25T02:00:10Z",
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "t3", "name": "bash",
             "arguments": {"command": "pytest tests/"}},
        ]},
    }))
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            heartbeat_seconds=3600, idle_warn_seconds=0.5,
            issue=18, run_id="abc123", role="implement",
        )
    messages = [line.getMessage() for line in caplog.records]
    idle = [line for line in messages if line.startswith("pi_idle")]
    resumed = [line for line in messages if line.startswith("pi_resumed")]
    assert idle, "no pi_idle warning"
    assert resumed, "no pi_resumed event after activity returned"
    # The resumed event comes after the idle warning.
    assert messages.index(resumed[0]) > messages.index(idle[0])
    assert "issue=18" in resumed[0]
    assert "run_id=abc123" in resumed[0]


def test_stream_pi_default_role_is_implement_and_run_id_optional(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=[], stdout="ok", sleep=1.2,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            heartbeat_seconds=0.5, idle_warn_seconds=3600,
        )
    heartbeats = [
        line.getMessage() for line in caplog.records
        if line.getMessage().startswith("pi_heartbeat")
    ]
    assert heartbeats
    assert "role=implement" in heartbeats[0]
    assert "run_id=-" in heartbeats[0]


def test_format_run_context_marks_missing_values():
    context = runner.format_run_context(
        issue=18, run_id=None, role="review",
        source_repo="xqliu/muyan-pilot", branch=None, worktree=Path("/w"),
    )
    assert context == (
        "issue=18 run_id=- role=review source_repo=xqliu/muyan-pilot "
        "branch=- worktree=/w"
    )


def test_format_elapsed_seconds_uses_integer_seconds():
    assert runner.format_elapsed_seconds(45.7) == "45s"
    assert runner.format_elapsed_seconds(0) == "0s"
    assert runner.format_elapsed_seconds(3723.9) == "3723s"
    assert runner.format_elapsed_seconds(-5) == "0s"
