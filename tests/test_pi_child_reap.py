"""The Pi child is reaped on every termination path (Issue #1995).

Every termination point of one Pi session — the steering restart, the
timeout kill, the idle-recovery kill, the swallowed-request kill and the
model-wait-dead kill — falls through the single `finally` of the session
loop. That exit path TERMs, waits a bounded grace, KILLs and reaps the
child BEFORE the active-process record is cleared: the next session starts
in the SAME worktree, so a child that survived a `terminate()` would work
next to the new agent in the same directory.

The observable fact is the old child's exit status. `journal.set_active_pi`
is a no-op without a bound run, so the tests bind the in-flight scene
through the journal's own API (`set_active_run`) and read the live child
back from it inside the run — no module global is patched.

The grace is a `PiWatchOptions` field: the tests pass a short value
instead of waiting out (or patching) the production constant.
"""
import signal
import subprocess

import pytest

import orbi.journal as journal
import orbi.pi_session as pi_session
from orbi.delivery_scene import RunContext
from orbi.pi_process import (
    PiWatchOptions,
    RecoverablePiTimeoutError,
    SteeringRequest,
)
from tests.fakes.bootstrap_pi import make_fake_pi, make_steering_restart_pi

# A short reap grace: the bounded wait is the only place the production
# 15 s would be spent.
REAP_GRACE_SECONDS = 0.3


def _ctx(tmp_path) -> RunContext:
    return RunContext(
        run_id="deadbeef", issue=1995, branch="b",
        worktree=tmp_path, source_repo="xqliu/orbi",
    )


def _live_child() -> subprocess.Popen | None:
    """The child the session recorded as active, read through the
    journal's own accessor (`active_run`)."""
    run = journal.active_run()
    return None if run is None else run["pi"]


@pytest.fixture
def bound_run(tmp_path):
    """Bind the in-flight delivery scene so `set_active_pi` records the
    live child, and unbind it afterwards."""
    journal.set_active_run(_ctx(tmp_path), "Pi child is never reaped")
    try:
        yield
    finally:
        journal.clear_active_run()


def _steering_restart(tmp_path, *, ignore_sigterm: bool):
    """Run one steering restart against the fake and report what the
    restarted session observed about the previous child (Issue #1995).

    ``returncode`` is read, never ``poll()``: only a reaping caller sets
    it, while a lazy ``poll()`` would reap the abandoned child itself and
    hide the defect.

    Returns `(result, child, alive_before, status_in_restart, armed)`.
    """
    armed = tmp_path / ".pi-armed"
    command = make_steering_restart_pi(
        ignore_sigterm=ignore_sigterm, ready=armed,
    )
    children: list[subprocess.Popen] = []
    # The first child was still running when the steering request was
    # made (no exit status yet): the scene the abandon would leave.
    alive_before: list[bool] = []
    # Its exit status observed from inside the restarted session: by
    # then the next child has already been spawned.
    status_in_restart: list[int | None] = []

    def check():
        if not children:
            child = _live_child()
            assert child is not None
            children.append(child)
        if not armed.exists():
            # The fake has not installed its own SIGTERM handling yet.
            return None
        if not alive_before:
            alive_before.append(children[0].returncode is None)
            return SteeringRequest("corrected", ("c1",), "alice")
        status_in_restart.append(children[0].returncode)
        return None

    result = pi_session.stream_pi(
        command, ctx=_ctx(tmp_path),
        watch=PiWatchOptions(
            poll_interval=0.01, steering_poll_seconds=0.01,
            child_grace_seconds=REAP_GRACE_SECONDS, steering_check=check,
        ),
        cwd=tmp_path,
    )
    assert children
    return result, children[0], alive_before, status_in_restart, armed


def test_steering_restart_reaps_a_child_that_ignores_sigterm(
    tmp_path, bound_run,
):
    """Acceptance 1: a fake Pi that ignores SIGTERM, steering requested —
    at the time the next session is spawned the previous child is already
    reaped, and the run completes."""
    result, child, alive_before, status_in_restart, armed = (
        _steering_restart(tmp_path, ignore_sigterm=True)
    )
    assert armed.exists()
    assert result == "ok"
    # The scene is real: the child was alive when the steering request
    # killed it and it ignored SIGTERM, so only the reap's SIGKILL could
    # end it — and it had ended before the next session was spawned.
    assert alive_before == [True]
    assert status_in_restart
    assert all(status is not None for status in status_in_restart)
    assert child.returncode == -signal.SIGKILL
    assert child.poll() is not None


def test_steering_restart_reaps_a_child_that_exits_on_sigterm(
    tmp_path, bound_run,
):
    """Acceptance 2: a fake Pi that exits on SIGTERM is reaped without the
    kill path — the exit code reflects SIGTERM."""
    result, child, alive_before, status_in_restart, _armed = (
        _steering_restart(tmp_path, ignore_sigterm=False)
    )
    assert result == "ok"
    assert alive_before == [True]
    assert status_in_restart
    assert all(status is not None for status in status_in_restart)
    # SIGTERM, not SIGKILL: the child exited inside the grace and the
    # reap never escalated.
    assert child.returncode == -signal.SIGTERM


def test_timeout_kill_path_reaps_the_child(tmp_path, bound_run):
    """Acceptance 3: the timeout-kill path also reaps — no zombie, the
    child's `poll()` is not None after `stream_pi` returns."""
    command = make_fake_pi(tmp_path, session_records=[], sleep=10.0)
    seen: list[subprocess.Popen] = []

    def progress(activity):
        child = _live_child()
        if child is not None and not seen:
            seen.append(child)

    with pytest.raises(RecoverablePiTimeoutError):
        pi_session.stream_pi(
            command, ctx=_ctx(tmp_path),
            watch=PiWatchOptions(
                poll_interval=0.01,
                child_grace_seconds=REAP_GRACE_SECONDS,
            ),
            cwd=tmp_path, timeout=0.3, progress=progress,
        )
    assert seen
    # Read (not `poll()`) first: the child was SIGKILLed by the timeout
    # path, so a set `returncode` is the exit path's own reap.
    assert seen[0].returncode == -signal.SIGKILL
    assert seen[0].poll() is not None
