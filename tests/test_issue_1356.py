"""Issue #1356: a Codex usage-limit stop is a `provider_quota` WAIT.

The scene the ticket describes: Pi exits 1 with
`Codex error: The usage limit has been reached` written ONLY into its
session journal (the assistant record's `errorMessage`). The runner saw a
bare `returned non-zero exit status 1`, classified nothing, and resumed Pi
every 5-minute tick for two hours.

These tests drive the REAL `pi_session.stream_pi` classification (only the
`pi` command itself is faked), then the following ticks through
`process_issue`:
1. the failure classifies `provider_quota` and its detail carries the
   provider's wording from the journal;
2. the next tick logs one `provider_quota_wait` line, posts nothing and
   never starts Pi;
3. an expired wait starts Pi again and the delivery continues.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import orbi.pi_session as pi_session
import orbi.runner as runner
from orbi import config as config_domain
from orbi import failure
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from orbi.pi_activity import stderr_with_session_error
from test_progress_wiring import (
    derived_wt,
    make_config,
    make_fake_gh,
    make_issue,
    patch_process_deps,
)

CODEX_USAGE_LIMIT = "Codex error: The usage limit has been reached"
CODEX_PROVIDER = "openai-codex"
CODEX_MODEL = "gpt-5-codex"


def _fresh_timestamp(offset: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset)
    ).isoformat()


def usage_limit_records() -> list[dict]:
    """The Codex usage-limit session: journal-only, no stderr."""
    return [
        {"type": "session", "id": "sess-1",
         "timestamp": _fresh_timestamp(), "cwd": "/w"},
        {"type": "model_change", "id": "mc-1",
         "timestamp": _fresh_timestamp(1),
         "provider": CODEX_PROVIDER, "modelId": CODEX_MODEL},
        {"type": "message", "id": "u1",
         "timestamp": _fresh_timestamp(1),
         "message": {"role": "user", "content": [
             {"type": "text", "text": "hi"}]}},
        {"type": "message", "id": "a1",
         "timestamp": _fresh_timestamp(2),
         "message": {"role": "assistant", "content": [],
                     "stopReason": "error",
                     "errorMessage": CODEX_USAGE_LIMIT}},
    ]


def write_session_script(session_dir: Path,
                         records: list[dict]) -> list[str]:
    """A fake `pi` that writes the journal and exits 1 WITHOUT stderr.

    The journal is the ONLY carrier of the provider's wording, so the
    test proves the runner reads it instead of guessing from stderr.
    """
    script = (
        "import json, pathlib, sys\n"
        f"d = pathlib.Path({str(session_dir)!r})\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        f"recs = {records!r}\n"
        "(d / 'sess.jsonl').write_text("
        "''.join(json.dumps(r) + '\\n' for r in recs))\n"
        "sys.exit(1)\n"
    )
    return [sys.executable, "-c", script]


def quota_run_pi(issue, ctx, config, **kwargs):
    """The `pi_session.run_pi` seam for a real usage-limit exit."""
    command = write_session_script(
        ctx.worktree / ".pi-session", usage_limit_records(),
    )
    return pi_session.stream_pi(
        command, ctx=ctx, cwd=ctx.worktree,
        watch=PiWatchOptions(poll_interval=0.05),
    )


def make_quota_config(tmp_path) -> config_domain.RunnerConfig:
    """The launch configuration the wait line reports (Issue #1356)."""
    return config_domain.RunnerConfig(
        repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
        base_branch="main",
        pi_provider=CODEX_PROVIDER, pi_model=CODEX_MODEL,
    )


def test_usage_limit_written_only_in_the_journal_classifies_provider_quota(
    tmp_path,
):
    """Acceptance 1: the journal's `errorMessage` reaches the detail and
    the classifier (Issue #1356)."""
    (tmp_path / ".pi-session").mkdir()
    command = write_session_script(
        tmp_path / ".pi-session", usage_limit_records(),
    )
    ctx = RunContext(
        run_id="deadbeef", issue=24, branch="b",
        worktree=tmp_path, source_repo="xqliu/orbi",
    )
    with pytest.raises(runner.RecoverablePiProcessError) as raised:
        pi_session.stream_pi(
            command, ctx=ctx, cwd=tmp_path,
            watch=PiWatchOptions(poll_interval=0.05),
        )

    assert CODEX_USAGE_LIMIT in failure._failure_detail(raised.value)
    record = runner._classify_failure(raised.value, outcome="fix_needed")
    assert record.reason_code == "provider_quota"
    assert record.action_code == "wait_quota"
    assert record.retry_safe is True


def test_the_next_tick_waits_quietly_on_a_recorded_provider_quota(
    monkeypatch, tmp_path, caplog,
):
    """Acceptance 2: the quota failure records the wait; the next tick
    starts no Pi, logs one `provider_quota_wait` line and posts nothing."""
    calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    patch_process_deps(monkeypatch, tmp_path, run_pi_side_effect=quota_run_pi)
    worktree = derived_wt(tmp_path)
    assert runner.process_issue(
        make_issue(), make_quota_config(tmp_path), "xqliu/orbi",
    ).kind == "failed"
    state = json.loads(
        (worktree / ".orbi" / "run-state.json").read_text(),
    )
    assert state["provider_quota_wait"]["provider"] == CODEX_PROVIDER
    assert state["provider_quota_wait"]["model"] == CODEX_MODEL
    assert state["provider_quota_wait"]["until"] > time.time()
    posted_after_the_failure = len(posted)

    # The next tick: the recorded wait is still active, so the Pi seam
    # must not be reached, and no comment may be written at all.
    pi_session.run_pi.side_effect = AssertionError("Pi must not start")
    writes_before = sum("--method" in call for call in calls)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        result = runner.process_issue(
            make_issue(), make_quota_config(tmp_path), "xqliu/orbi",
        )

    assert result.kind == "failed"
    assert pi_session.run_pi.call_count == 1
    assert "Pi must not start" not in caplog.text
    assert (
        f"provider_quota_wait issue=18 provider={CODEX_PROVIDER} "
        f"model={CODEX_MODEL}" in caplog.text
    )
    assert len(posted) == posted_after_the_failure
    assert sum("--method" in call for call in calls) == writes_before


def test_a_quota_wait_is_recorded_even_when_the_provider_is_unknown(
    monkeypatch, tmp_path,
):
    """A recreated worktree (or a wiped session dir) leaves no provider
    name and the config may carry none either: the wait must still be
    armed, named `-`."""
    make_fake_gh(monkeypatch, in_progress=True)
    patch_process_deps(monkeypatch, tmp_path, run_pi_side_effect=quota_run_pi)
    worktree = derived_wt(tmp_path)

    assert runner.process_issue(
        make_issue(), make_config(tmp_path), "xqliu/orbi",
    ).kind == "failed"

    state = json.loads(
        (worktree / ".orbi" / "run-state.json").read_text(),
    )
    assert state["provider_quota_wait"]["provider"] == "-"
    assert state["provider_quota_wait"]["model"] == "-"
    assert runner.provider_quota_wait(worktree) is not None


def test_only_an_active_well_formed_wait_defers_a_tick(tmp_path):
    """A wait record is read fail-open (Issue #1356): anything absent,
    corrupt or malformed is simply no wait, never a broken tick."""
    worktree = tmp_path / "wt"
    (worktree / ".orbi").mkdir(parents=True)
    state_path = worktree / ".orbi" / "run-state.json"
    identifier = {
        "run_id": "a1b2c3d4", "issue": 18, "repo": "xqliu/orbi",
        "branch": "b", "worktree": str(worktree),
    }

    assert runner.provider_quota_wait(worktree) is None
    state_path.write_text(json.dumps(identifier))
    assert runner.provider_quota_wait(worktree) is None
    state_path.write_text("{ not json")
    assert runner.provider_quota_wait(worktree) is None
    for malformed in (
        [], True, "until", {"until": True}, {"until": "soon"},
        {"until": 1.0}, {},
    ):
        state_path.write_text(json.dumps({
            **identifier, "provider_quota_wait": malformed,
        }))
        assert runner.provider_quota_wait(worktree) is None, malformed

    state_path.write_text(json.dumps({
        **identifier,
        "provider_quota_wait": {"until": time.time() + 60.0},
    }))
    wait = runner.provider_quota_wait(worktree)
    assert wait is not None
    assert wait["until"] > time.time()


def test_a_wait_that_cannot_be_recorded_only_logs_the_bypass(
    tmp_path, caplog,
):
    """A missing or corrupt run state file never fails the delivery being
    recorded (Issue #73): the wait is simply not armed and the bypass is
    logged (Issue #1356)."""
    worktree = tmp_path / "wt"
    (worktree / ".orbi").mkdir(parents=True)
    state_path = worktree / ".orbi" / "run-state.json"

    with caplog.at_level(logging.WARNING, logger="orbi.bootstrap"):
        runner.record_provider_quota_wait(
            worktree, provider=CODEX_PROVIDER, model=CODEX_MODEL,
        )
        assert not state_path.exists()
        assert "provider_quota_wait_unrecorded" in caplog.text
        caplog.clear()
        state_path.write_text("{ not json")
        runner.record_provider_quota_wait(
            worktree, provider=CODEX_PROVIDER, model=CODEX_MODEL,
        )
        assert "provider_quota_wait_unrecorded" in caplog.text

    assert runner.provider_quota_wait(worktree) is None


def test_the_session_error_is_appended_only_when_it_is_new():
    """The exit detail gains the journal's provider error (Issue #1356)
    without losing Pi's own stderr or duplicating the message."""
    assert stderr_with_session_error("", {
        "last_error": CODEX_USAGE_LIMIT,
    }) == CODEX_USAGE_LIMIT
    assert stderr_with_session_error("pi died", {
        "last_error": CODEX_USAGE_LIMIT,
    }) == f"pi died\n{CODEX_USAGE_LIMIT}"
    assert stderr_with_session_error(f"x {CODEX_USAGE_LIMIT}", {
        "last_error": CODEX_USAGE_LIMIT,
    }) == f"x {CODEX_USAGE_LIMIT}"
    assert stderr_with_session_error("pi died", {}) == "pi died"
    assert stderr_with_session_error("pi died", {
        "last_error": "   ",
    }) == "pi died"


def test_an_expired_wait_lets_the_next_tick_start_pi_and_deliver(
    monkeypatch, tmp_path,
):
    """Acceptance 3: once the window passes the same tick starts Pi and
    the delivery continues (the wait is a deferral, never a terminal)."""
    calls, _ = make_fake_gh(monkeypatch, in_progress=True)
    patch_process_deps(monkeypatch, tmp_path)
    worktree = derived_wt(tmp_path)
    (worktree / ".orbi").mkdir(parents=True, exist_ok=True)
    (worktree / ".orbi" / "run-state.json").write_text(json.dumps({
        "run_id": "a1b2c3d4", "issue": 18, "repo": "xqliu/orbi",
        "branch": "orbi/xqliu-orbi-issue-18",
        "worktree": str(worktree),
        "provider_quota_wait": {
            "until": time.time() - 1.0,
            "provider": CODEX_PROVIDER, "model": CODEX_MODEL,
        },
    }))

    result = runner.process_issue(
        make_issue(), make_quota_config(tmp_path), "xqliu/orbi",
    )

    assert result.kind == "pr"
    assert pi_session.run_pi.called
    remaining = json.loads(
        (worktree / ".orbi" / "run-state.json").read_text(),
    )
    assert "provider_quota_wait" not in remaining
    # The delivery really continued: the progress comment was closed out
    # instead of the tick returning the quiet wait.
    final = [
        command[command.index("--field") + 1][len("body="):]
        for command in calls
        if "--method" in command
        and command[command.index("--method") + 1] == "PATCH"
    ]
    assert any("Orbi delivered" in body for body in final)
