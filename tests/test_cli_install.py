"""Auto-refresh tests for the editable CLI install (Issue #158).

The official local deployment is the EDITABLE uv tool install (Issue
#152): the tool env imports the runtime modules from the deployment
checkout through a setuptools editable finder whose module MAPPING is
generated at INSTALL time from the checkout's ``pyproject.toml``.
When the packaging inputs change (a new runtime module is added to
``py-modules``, a dependency or entry point changes), the installed
finder is stale and the next CLI process dies with
``ModuleNotFoundError`` before the Runner can even start (the #158
incident: ``cli_source`` merged to main, the installed finder still
mapped the pre-#152 module set, systemd start failed).

These tests pin the pre-start refresh contract:

- the packaging fingerprint is the sha256 of the checkout's
  ``pyproject.toml`` (the packaging input that decides the editable
  metadata) — ordinary Python source content is NOT part of it;
- the last-install fingerprint lives in the shared state dir
  (``.muyan-pilot/cli-install.json``, the same gitignored dir as
  ``base-sync.lock`` and the slots) — not a second release state;
- unchanged fingerprint: NO uv call at all (no per-tick reinstall);
- changed or missing state (first install): ONE lock-protected
  ``uv tool install --force --reinstall --editable --python
  /usr/bin/python3 <repo_dir>`` (the exact verified argv from
  ``cli_source.reinstall_args``), then the state is recorded;
- two instances starting in the same tick: the SAME base-sync flock
  (the lock file the service template's ``ExecStartPre`` uses) — the
  second instance reuses the first's result and never runs a second
  install;
- a failing install fails fast with the structured
  ``cli_install_failed`` line (reason + the exact fix command) and
  records NO state (the next start retries).
"""
import fcntl
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

import cli_install
import cli_source

# `cli_install` is a thin re-export of the implementation in
# `bootstrap_runner` (see the NOTE there): these tests exercise the
# real refresh through that single import point. The suite's default
# no-op stub (conftest) patches `bootstrap_runner.refresh_cli_install`
# — the call `main()` makes — and never touches this import point.


def _write_pyproject(repo: Path, text: str) -> Path:
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    path = repo / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _recorder(calls):
    """A run_command stand-in matching the real contract: it receives
    the argv AND the timeout keyword (Issue #95: the install is a
    blocking command and must carry a timeout)."""

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return ""

    return run


# --- the packaging fingerprint ---------------------------------------------


def test_packaging_fingerprint_is_the_sha256_of_pyproject(tmp_path):
    """The fingerprint is the sha256 of the checkout's
    ``pyproject.toml`` content — the packaging input that decides the
    editable metadata (py-modules, entry points, version, deps)."""
    repo = tmp_path / "checkout"
    content = b'[project]\nname = "muyan-pilot"\nversion = "0.2.0"\n'
    _write_pyproject(repo, content.decode("utf-8"))
    assert cli_install.packaging_fingerprint(repo) == (
        hashlib.sha256(content).hexdigest()
    )


def test_packaging_fingerprint_changes_with_pyproject_content(tmp_path):
    """A changed ``pyproject.toml`` (e.g. a new runtime module added to
    ``py-modules``) changes the fingerprint — that is the refresh
    trigger."""
    repo = tmp_path / "checkout"
    _write_pyproject(repo, '[project]\nname = "a"\n')
    first = cli_install.packaging_fingerprint(repo)
    _write_pyproject(
        repo, '[project]\nname = "a"\n[tool.setuptools]\npy-modules = ["x"]\n',
    )
    assert cli_install.packaging_fingerprint(repo) != first


def test_packaging_fingerprint_ignores_python_source_content(tmp_path):
    """Acceptance: ONLY the packaging input is fingerprinted — a
    change to an existing Python file's content must NOT trigger a
    reinstall (the editable finder maps the live file anyway)."""
    repo = tmp_path / "checkout"
    _write_pyproject(repo, '[project]\nname = "a"\n')
    (repo / "muyan_pilot.py").write_text("old\n", encoding="utf-8")
    first = cli_install.packaging_fingerprint(repo)
    (repo / "muyan_pilot.py").write_text("new content\n", encoding="utf-8")
    assert cli_install.packaging_fingerprint(repo) == first


def test_packaging_fingerprint_fails_fast_without_pyproject(tmp_path):
    """A checkout without ``pyproject.toml`` cannot be tool-installed:
    the pre-start check fails fast, never guesses a fingerprint."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(
        cli_install.CliInstallError, match="pyproject.toml",
    ):
        cli_install.packaging_fingerprint(repo)


# --- the install state (the last-install fingerprint) -----------------------


def test_install_state_path_is_in_the_shared_state_dir(tmp_path):
    """The state lives in ``<repo_dir>/.muyan-pilot/`` — the EXISTING
    shared state dir (gitignored, next to ``base-sync.lock`` and the
    slots; it survives the ``git merge --ff-only`` checkout sync).
    Not a second release state, not a per-process temp file."""
    assert cli_install.install_state_path(tmp_path) == (
        tmp_path / ".muyan-pilot" / "cli-install.json"
    )


def test_read_install_state_missing_file_is_none(tmp_path):
    """No state yet (first install / fresh checkout) -> None (the
    refresh runs)."""
    assert cli_install.read_install_state(tmp_path) is None


def test_read_install_state_returns_the_stored_fingerprint(tmp_path):
    path = cli_install.install_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"pyproject_sha256": "abc123"}), encoding="utf-8",
    )
    assert cli_install.read_install_state(tmp_path) == "abc123"


def test_read_install_state_malformed_file_is_none(tmp_path):
    """A corrupted state file (a torn write) heals in the SAFE
    direction: it is treated as "no state" and one extra idempotent
    ``--force --reinstall`` runs — never a wedged start."""
    path = cli_install.install_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert cli_install.read_install_state(tmp_path) is None
    path.write_text(json.dumps({"wrong": "shape"}), encoding="utf-8")
    assert cli_install.read_install_state(tmp_path) is None


def test_write_install_state_is_atomic_and_readable(tmp_path):
    cli_install.write_install_state(tmp_path, "deadbeef")
    data = json.loads(
        cli_install.install_state_path(tmp_path).read_text(encoding="utf-8"),
    )
    assert data == {"pyproject_sha256": "deadbeef"}
    assert cli_install.read_install_state(tmp_path) == "deadbeef"
    # No temp file is left behind (the atomic replace cleaned up).
    leftovers = [
        p.name for p in cli_install.install_state_path(tmp_path).parent.iterdir()
        if p.name != "cli-install.json"
    ]
    assert leftovers == []


def test_write_install_state_overwrites_the_previous_fingerprint(tmp_path):
    cli_install.write_install_state(tmp_path, "first")
    cli_install.write_install_state(tmp_path, "second")
    assert cli_install.read_install_state(tmp_path) == "second"


# --- the refresh decision ----------------------------------------------------


def test_refresh_is_a_noop_without_any_uv_call_when_unchanged(tmp_path):
    """Acceptance: unchanged packaging input -> NO uv call (no
    per-tick unconditional install)."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    cli_install.write_install_state(
        tmp_path, cli_install.packaging_fingerprint(tmp_path),
    )
    calls = []
    result = cli_install.refresh_cli_install(
        tmp_path, run_command=_recorder(calls),
    )
    assert result == "unchanged"
    assert calls == []


def test_refresh_installs_on_the_first_install(tmp_path, caplog):
    """Acceptance: no state yet (first install) -> the EXACT editable
    force reinstall runs once and the state is recorded."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    calls = []
    with caplog.at_level("INFO"):
        result = cli_install.refresh_cli_install(
            tmp_path, run_command=_recorder(calls),
        )
    assert result == "installed"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == cli_source.reinstall_args(tmp_path)
    assert kwargs["timeout"] == cli_install.UV_INSTALL_TIMEOUT_SECONDS
    assert cli_install.read_install_state(tmp_path) == (
        cli_install.packaging_fingerprint(tmp_path)
    )
    assert "cli_install_refreshed" in caplog.text
    assert "reason=first_install" in caplog.text


def test_refresh_installs_when_the_packaging_input_changed(tmp_path, caplog):
    """Acceptance: ``pyproject.toml`` changed relative to the last
    install (e.g. a new runtime module was added to ``py-modules``) ->
    the editable metadata is refreshed and the next CLI process
    imports the new module."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    cli_install.write_install_state(
        tmp_path, "stale-fingerprint-from-the-last-install",
    )
    calls = []
    with caplog.at_level("INFO"):
        result = cli_install.refresh_cli_install(
            tmp_path, run_command=_recorder(calls),
        )
    assert result == "installed"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == cli_source.reinstall_args(tmp_path)
    assert kwargs["timeout"] == cli_install.UV_INSTALL_TIMEOUT_SECONDS
    assert "reason=packaging_changed" in caplog.text
    # The state now records the CURRENT fingerprint: the very next
    # start is a no-op again.
    assert cli_install.read_install_state(tmp_path) == (
        cli_install.packaging_fingerprint(tmp_path)
    )


def test_refresh_reuses_a_concurrent_instances_result_under_the_lock(
    tmp_path,
):
    """The decision is re-checked UNDER the lock: while this instance
    waits for the flock, the other instance (holding the lock) does
    the refresh and records the state; on acquiring the lock, this
    instance re-reads the state, sees it is current, and reuses the
    result — no second install, no uv call at all.

    Deterministic choreography: the test holds the flock, so the
    refresh thread (a) reads the state BEFORE the lock (absent — the
    test has not written it yet) and (b) blocks on the flock; only
    then does the test, as the lock holder, record the state. The
    refresh's under-lock re-read must see it and skip the install."""
    import time

    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    fingerprint = cli_install.packaging_fingerprint(tmp_path)
    lock_path = cli_install.base_sync_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the flock BEFORE the refresh starts: the refresh's pre-lock
    # state read (absent state — nothing was written) provably
    # happens while the test holds the lock, and the refresh parks on
    # the flock behind it. No timing assumption.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    calls = []
    result = []
    done = threading.Event()

    def start():
        result.append(
            cli_install.refresh_cli_install(
                tmp_path, run_command=_recorder(calls),
            ),
        )
        done.set()

    thread = threading.Thread(target=start)
    thread.start()
    # The refresh thread's steps are: fingerprint -> pre-lock state
    # read -> open(lock) -> flock (blocks). The first three are
    # microseconds of pure local work (no I/O beyond two tiny file
    # reads); one beat is orders of magnitude more than enough for
    # the thread to be parked on the flock before the state is
    # written — its pre-lock read has already seen the absent state.
    time.sleep(0.2)
    # As the lock holder, play the other instance: record the state
    # (what a real concurrent refresh leaves behind after its
    # install), then release the flock. The refresh's under-lock
    # re-read must see it and skip the install.
    cli_install.write_install_state(tmp_path, fingerprint)
    # Release the flock so the parked refresh can proceed (its
    # under-lock re-read must now see the recorded state), then wait
    # for the result.
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    thread.join(timeout=30)
    assert done.is_set()
    assert result == ["unchanged"]
    assert calls == []


def test_refresh_writes_the_state_only_after_a_successful_install(
    tmp_path,
):
    """A failing install records NO state: the next start retries the
    refresh (a half-recorded state would wedge the machine on a broken
    install forever)."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')

    def boom(command, **kwargs):
        raise RuntimeError("uv exploded")

    with pytest.raises(cli_install.CliInstallError, match="uv exploded"):
        cli_install.refresh_cli_install(tmp_path, run_command=boom)
    assert cli_install.read_install_state(tmp_path) is None


# --- failure propagation ------------------------------------------------------


def test_refresh_failure_logs_the_structured_line_with_the_fix_command(
    tmp_path, caplog,
):
    """Acceptance: a uv install failure fails fast and the journal
    carries the concrete reason AND the exact fix command (the
    editable force reinstall from the deployment checkout)."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')

    def boom(command, **kwargs):
        raise RuntimeError("boom: the tool env is broken")

    with caplog.at_level("ERROR"):
        with pytest.raises(
            cli_install.CliInstallError, match="the tool env is broken",
        ):
            cli_install.refresh_cli_install(tmp_path, run_command=boom)
    line = next(
        record.message for record in caplog.records
        if record.message.startswith("cli_install_failed")
    )
    assert "reason=" in line
    assert "the tool env is broken" in line
    assert cli_source.reinstall_command(tmp_path) in line


def test_refresh_failure_releases_the_lock(tmp_path):
    """A failed refresh must not wedge the shared lock: the next tick
    (or the other instance) must be able to take it."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')

    def boom(command, **kwargs):
        raise RuntimeError("nope")

    with pytest.raises(cli_install.CliInstallError):
        cli_install.refresh_cli_install(tmp_path, run_command=boom)
    lock_path = cli_install.base_sync_lock_path(tmp_path)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


def test_refresh_fails_fast_when_the_lock_is_held(tmp_path):
    """The refresh takes the SAME base-sync flock the service
    template's ``ExecStartPre`` uses: while it is held (the other
    instance is syncing the checkout), the refresh waits and then
    fails fast with the useful error — never a concurrent write."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    lock_path = cli_install.base_sync_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            cli_install.CliInstallError, match="base-sync.lock",
        ):
            cli_install.refresh_cli_install(
                tmp_path, run_command=lambda *a, **k: "",
                lock_timeout_seconds=0.3,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- concurrency: two instances, one install ---------------------------------


def test_two_concurrent_starts_run_exactly_one_install(tmp_path):
    """Acceptance: two instances starting in the same tick never run
    concurrent uv installs and never corrupt the tool env: the
    base-sync flock serializes them, the second reuses the first's
    result. The counting run_command stands in for the real
    ``uv tool install`` (its body is the exact verified argv)."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    results = []
    errors = []
    lock = threading.Lock()
    installs = []

    def run(command, **kwargs):
        with lock:
            installs.append(1)
            active = len(installs)
        # Simulate the install body; the flock must keep `active` at 1.
        assert active == 1, "concurrent uv tool installs raced"
        # The state lands only after a successful install.
        cli_install.write_install_state(
            tmp_path, cli_install.packaging_fingerprint(tmp_path),
        )
        return ""

    def start():
        try:
            results.append(
                cli_install.refresh_cli_install(tmp_path, run_command=run),
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    # The winner installed; the loser waited for the flock, re-read
    # the state under the lock and reused the result (no second
    # install).
    assert sorted(results) == ["installed", "unchanged"]
    assert len(installs) == 1
    assert cli_install.read_install_state(tmp_path) == (
        cli_install.packaging_fingerprint(tmp_path)
    )


def test_base_sync_lock_path_is_the_shared_state_dir_file(tmp_path):
    """The refresh serializes on the SAME lock file the ExecStartPre
    flock in the service template uses (the shared state dir, next to
    the slots — never a per-process temp file)."""
    assert cli_install.base_sync_lock_path(tmp_path) == (
        tmp_path / ".muyan-pilot" / "base-sync.lock"
    )


def test_reinstall_argv_is_the_verified_editable_force_reinstall(tmp_path):
    """The refresh runs the EXACT verified argv (Issue #152 contract,
    asserted against the real `uv tool install --help` in
    test_cli_source.py): `--force`, `--reinstall`, `--editable`,
    `--python /usr/bin/python3`, the deployment checkout."""
    _write_pyproject(tmp_path, '[project]\nname = "a"\n')
    calls = []
    cli_install.refresh_cli_install(tmp_path, run_command=_recorder(calls))
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", "/usr/bin/python3", str(tmp_path),
    ]
    assert kwargs["timeout"] == cli_install.UV_INSTALL_TIMEOUT_SECONDS
