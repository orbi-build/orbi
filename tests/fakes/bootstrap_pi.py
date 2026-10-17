"""Shared scaffolding for the Pi session domain suite (Issue #1646).

Moved verbatim out of `tests/test_bootstrap_runner.py` when the Pi session
start, resume and recovery tests moved to `tests/test_bootstrap_pi.py`: the
fake Pi commands, the session-record builders and the recovery fixtures
those tests share. Single owner: the domain suite and the remaining
`test_bootstrap_runner.py` scenes (live progress, stop handler) import them
from here instead of keeping a copy of each.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orbi import pi_process


def make_fake_pi(tmp_path: Path, *, session_records: list[tuple[float, dict]],
                 stdout: str = "", stderr: str = "", exit_code: int = 0,
                 sleep: float = 0.0, wait_after_record: int | None = None,
                 release_file: Path | None = None) -> list[str]:
    """Build a command that mimics pi: appends session records over time."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records_literal = repr(session_records)
    wait = ""
    if wait_after_record is not None and release_file is not None:
        wait = (
            f"    if index == {wait_after_record}:\n"
            f"        deadline = time.monotonic() + 5\n"
            f"        while not os.path.exists({str(release_file)!r}):\n"
            "            if time.monotonic() >= deadline: raise RuntimeError('release timeout')\n"
            "            time.sleep(0.001)\n"
        )
    script = (
        "import json, os, sys, time\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        f"records = {records_literal}\n"
        "for index, (delay, record) in enumerate(records):\n"
        "    time.sleep(delay)\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        f"{wait}"
        f"time.sleep({sleep!r})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code!r})\n"
    )
    return [sys.executable, "-c", script]


def fresh_timestamp(offset: float = 0.0) -> str:
    """A session timestamp at (real) now + offset.

    The watcher computes `stale_seconds` against the real clock, so the
    fake session records must carry fresh timestamps: a fixed 2026 date
    would look years stale and trip the 5-minute idle warning (Issue
    #18) in every stream_pi test.
    """
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset)
    ).isoformat()


def fake_session_records():
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.2, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]


def make_rate_limit_pi(tmp_path: Path, *, fail_times: int, stderr: str,
                       stdout: str = "",
                       records: list[tuple[float, dict]] | None = None) -> list[str]:
    """A fake pi whose first `fail_times` invocations exit 1 with a 429
    stderr, then succeed (Issue #321). Every invocation writes `records`
    (the `make_fake_pi` shape) to its OWN session file — real Pi creates
    a new JSONL per session, so a retried invocation is never bound to
    the failed attempt's file."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records_literal = repr(records or [])
    script = (
        "import json, os, sys\n"
        f"session_dir = {str(session_dir)!r}\n"
        f"counter = {str(tmp_path / '.pi-invocations')!r}\n"
        "n = (int(open(counter).read()) if os.path.exists(counter) else 0) + 1\n"
        "open(counter, 'w').write(str(n))\n"
        f"records = {records_literal}\n"
        "for delay, record in records:\n"
        "    with open(os.path.join(session_dir, 'sess-%d.jsonl' % n),"
        " 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        f"if n <= {fail_times}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        "    sys.exit(1)\n"
        f"sys.stdout.write({stdout!r})\n"
    )
    return [sys.executable, "-c", script]


def startup_records():
    """A full healthy startup: session, the model Pi selected, the first
    request (user message) and the first response (assistant message)."""
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.05, {"type": "model_change", "id": "m1",
                "timestamp": fresh_timestamp(),
                "provider": "local-qwen", "modelId": "qwen3.8:27b"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.2, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]


def _frozen_model_wait_records(stale_seconds: float):
    """Session records whose newest event (a toolResult) is
    `stale_seconds` in the PAST: the session JSONL is frozen in
    model_wait for that long without any real waiting (the watcher
    measures stale against the record timestamps, Issue #169)."""
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(-stale_seconds), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(-stale_seconds),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.0, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(-stale_seconds),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]


def make_upstream_listener(drop: bool = False):
    """A local TCP listener that mimics the upstream (llama/proxy).

    Returns `(port, stop)`: the listener accepts ONE connection and
    either holds it (a live upstream) or closes it immediately (the
    upstream died: the client socket goes CLOSE_WAIT). `stop()` closes
    the listener.
    """
    import socket as _socket
    import threading as _threading
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        if drop:
            conn.close()
        else:
            time.sleep(30)
        conn.close()  # idempotent: safe after the drop close

    thread = _threading.Thread(target=serve, daemon=True)
    thread.start()

    def stop():
        listener.close()

    return port, stop


def make_slow_model_pi(tmp_path, *, port: int, hold_seconds: float = 2.5,
                       drop_upstream: bool = False) -> list[str]:
    """Build a command that mimics a Pi with a HUNG model request
    (Issue #218): it writes the session toolCall + toolResult (the
    model is expected to reply next: model_wait), opens a TCP
    connection to the local upstream listener (the live model request)
    and holds it for `hold_seconds` without ever replying — the model
    service process is alive, the connection is alive, but the request
    never completes (the session JSONL stays frozen past the dead
    threshold). When `drop_upstream` the listener closes the connection
    immediately (the old #169 dead-upstream scene: the client socket
    leaves the live TCP states). After the hold it writes the assistant
    reply and exits — a runner that does not kill it gets to finish."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    script = (
        "import json, socket, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-slow', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'pytest tests/'}}]}})\n"
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': False,\n"
        "                   'content': [{'type': 'text', 'text': 'ok'}]}})\n"
        f"sock = socket.create_connection(('127.0.0.1', {port!r}))\n"
        f"time.sleep({hold_seconds!r})\n"
        "try:\n"
        "    sock.close()\n"
        "except OSError:\n"
        "    pass\n"
        "write({'type': 'message', 'id': 'a2', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'text', 'text': 'slow answer'}]}})\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def make_timeout_tool_pi(tmp_path, *, tool_seconds: float = 0.6,
                         react_on_tool_done: bool = True,
                         wrapper_honors_deadline: bool = True) -> list[str]:
    """Build a command that mimics a Pi running a LONG tool with an
    explicit `timeout` (Issue #169, the #105 regression): it writes the
    session toolCall, spawns `bash -c 'timeout <tool_seconds> sleep 300'`
    (the tool self-terminates at its deadline — no session event until
    then) and waits for it. When `wrapper_honors_deadline` is False the
    fake simulates a wrapper that FAILED to end its command (a fake
    `timeout` on PATH that ignores the duration: the sleep outlives the
    nominal deadline, the evidence flips back to stalled). When
    `react_on_tool_done` the tool's exit (deadline reached or killed)
    makes the fake Pi write the toolResult and exit; otherwise it stays
    silent (Pi itself is stuck)."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    react = (
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'tool finished'}]}})\n"
        if react_on_tool_done else "time.sleep(10)\n"
    )
    if wrapper_honors_deadline:
        tool_command = f"timeout {tool_seconds!r} sleep 300"
    else:
        # The nominal deadline is in the command line (what the runner
        # sees), but the wrapper never ends the command: a fake
        # `timeout` on PATH that ignores the duration and just runs the
        # command — the sleep runs on past the deadline.
        fake_bin = session_dir / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_timeout = fake_bin / "timeout"
        fake_timeout.write_text(
            "#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8",
        )
        fake_timeout.chmod(0o755)
        tool_command = (
            f"PATH={fake_bin!s}:/usr/bin:/bin "
            f"timeout {tool_seconds!r} sleep 30000"
        )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-timeout', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'timeout 0.6 sleep 300'}}]}})\n"
        f"child = subprocess.Popen(['bash', '-c', {tool_command!r}])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        f"{react}"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def make_timeout_tool_pi_release_gated(tmp_path, *, release: Path) -> list[str]:
    """A `timeout 300 sleep 300` tool whose death the TEST gates
    (Issue #741). The wrapper is the REAL coreutils `timeout` (exec'd by
    `bash -c`, so the /proc cmdline is `timeout 300 sleep 300` — exactly
    what `timeout_duration` parses) with a 300 s nominal deadline: the
    tool is INSIDE its deadline at every poll, whatever the scheduling.
    The fake Pi kills the tool's process group only once the `release`
    file appears, then reports the tool result. A test that creates
    `release` exactly when the runner logs its wait decision (the
    progress callback's `recovery=wait`) makes the runner-observes-tool
    ordering deterministic BY CONSTRUCTION — the tool cannot die before
    the inside-deadline observation — instead of by wall-clock luck (the
    #741 flake: under load one poll-loop stall skipped the whole
    real-time detection window, the wait decision never fired and the
    test failed on healthy code). The fake Pi self-terminates after
    60 s: a broken release path fails the assertions fast, it never
    hangs."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    script = (
        "import json, os, signal, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        f"release = {str(release)!r}\n"
        "guard = 60.0\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-timeout', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'timeout 300 sleep 300'}}]}})\n"
        "child = subprocess.Popen(\n"
        "    ['bash', '-c', 'timeout 300 sleep 300'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "start = time.monotonic()\n"
        "while child.poll() is None:\n"
        "    if os.path.exists(release):\n"
        "        try:\n"
        "            os.killpg(os.getpgid(child.pid), signal.SIGTERM)\n"
        "        except ProcessLookupError:\n"
        "            pass\n"
        "    elif time.monotonic() - start > guard:\n"
        "        os.killpg(os.getpgid(child.pid), signal.SIGKILL)\n"
        "        sys.exit(3)\n"
        "    time.sleep(0.02)\n"
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'tool finished'}]}})\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def make_timeout_tool_pi_controlled(tmp_path, *, tool_seconds: float,
                                    control: Path) -> list[str]:
    """A timeout tool whose (delayed) deadline fire is controlled by
    the TEST, not by scheduling (Issue #181): the command line carries
    the nominal `timeout <tool_seconds>` (what the runner reads), but
    a fake `timeout` on PATH first waits for `control` to appear — the
    test's "the wrapper's deadline handling finished" signal — and
    only then enforces the nominal duration. The tool's lifetime is
    therefore `control_appeared + tool_seconds`, fully determined by
    the test (no real-scheduling coincidence). The test that never
    creates `control` gets a broken wrapper: the deadline handling
    never finishes, the tool outlives the nominal deadline forever."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    import shutil as _shutil
    # The real coreutils `timeout` is exec'd by ABSOLUTE path — the
    # fake shadows it on PATH, so a bare `timeout` would re-enter the
    # fake and loop forever. Coreutils `timeout` is a hard system
    # dependency (the prompt contract requires it); a missing binary
    # fails the generated script with a clear shell error.
    real_timeout = _shutil.which("timeout")
    fake_bin = session_dir / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_timeout = fake_bin / "timeout"
    fake_timeout.write_text(
        "#!/bin/sh\n"
        "duration=\"$1\"; shift\n"
        f"while [ ! -e {str(control)!r} ]; do sleep 0.01; done\n"
        f'exec {real_timeout!r} "$duration" "$@"\n', encoding="utf-8",
    )
    fake_timeout.chmod(0o755)
    tool_command = (
        f"PATH={fake_bin!s}:/usr/bin:/bin "
        f"timeout {tool_seconds!r} sleep 300"
    )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-timeout', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        f"            'arguments': {{'command': 'timeout {tool_seconds!r} sleep 300'}}}}]}}}})\n"
        f"child = subprocess.Popen(['bash', '-c', {tool_command!r}])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'tool finished'}]}})\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def make_idle_recovery_tracker(monkeypatch, *, window=0.3, targets=None,
                               pending=None, alive=True):
    """A `IdleRecoveryTracker` wired to fake /proc primitives (Issue
    #287): `find_idle_descendants` returns `targets`, the pending-
    timeout computation returns `pending` (a list of
    `(target, deadline)`), signals are recorded instead of delivered,
    and the monotonic clock is a mutable dict the test advances."""
    targets = [{"pid": 4242, "cmdline": "sleep 300", "start_epoch": 1.0}] \
        if targets is None else targets
    pending = [] if pending is None else pending
    state = {
        "signals": [],           # [(pid, signal)]
        "mono": 1000.0,          # the fake monotonic clock
        "pending_calls": 0,
    }
    monkeypatch.setattr(
        pi_process, "find_idle_descendants",
        lambda pid, epoch: targets,
    )
    monkeypatch.setattr(
        pi_process, "_pending_timeout_targets",
        lambda targets: (state.__setitem__(
            "pending_calls", state["pending_calls"] + 1
        ) or pending),
    )
    monkeypatch.setattr(
        pi_process, "signal_pid",
        lambda pid, sig, *, expected_start_epoch=None, btime=None, hz=None:
        state["signals"].append((pid, sig)) or "sent",
    )
    monkeypatch.setattr(pi_process, "pid_alive", lambda pid: alive)
    monkeypatch.setattr(
        pi_process.time, "monotonic", lambda: state["mono"],
    )
    state["tracker"] = pi_process.IdleRecoveryTracker(
        window, run_id="deadbeef", issue_ref="xqliu/orbi#94",
        role="implement",
    )
    return state


def make_hung_pi(tmp_path, *, child_sleep=10.0, ignore_sigterm=False,
                 react_on_child_death=True, exit_code=0,
                 model_wait=False):
    """Build a command that mimics a HUNG Pi (Issue #94): it writes the
    session toolCall, spawns a long-running child (the hung bash tool)
    and waits for it. When `react_on_child_death`, the child's death
    (the runner's TERM/KILL) makes the fake Pi write the toolResult
    error — the failure signal to the model — and exit with
    `exit_code` (the session continues on its own). Otherwise it stays
    silent (Pi itself is stuck). `model_wait` writes a toolResult
    first (the model is expected to reply next: the Issue #75
    territory, never the idle-recovery territory).

    The sleeps are short on purpose (Issue #95 termination guard):
    WITHOUT the recovery implementation the child exits naturally and
    the run ends, so the red phase fails on the missing recovery
    lines instead of hanging for the full child sleep."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    ignore = (
        "import signal as _sig\n"
        "_sig.signal(_sig.SIGTERM, _sig.SIG_IGN)\n"
        if ignore_sigterm else ""
    )
    tool_result = (
        "write({'type': 'message', 'id': 'r0', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': False,\n"
        "                   'content': [{'type': 'text', 'text': 'ok'}]}})\n"
        if model_wait else ""
    )
    react = (
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'killed by runner'}]}})\n"
        if react_on_child_death else "time.sleep(10)\n"
    )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-hung', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'sleep 300'}}]}})\n"
        f"{tool_result}"
        f"{ignore}"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        f"     'import time; time.sleep({child_sleep!r})'])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        f"{react}"
        f"sys.exit({exit_code!r})\n"
    )
    return [sys.executable, "-c", script]
