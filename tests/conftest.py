"""Shared test fixtures for the Orbi suite.

The deployment preflights (Issue #158 CLI install refresh, Issue #103
unit drift, Issue #114 git transport) read the REAL machine state (the
tool env, the user unit directory, the checkout's ``origin`` remote,
SSH connectivity); the in-process dispatch tests use tmp repo_dirs
that carry no tool env, no ``systemd/`` templates and no git checkout,
so they run with all three preflights stubbed to a passing no-op by
default. The preflight tests and the wiring/e2e suites stub or
exercise the real checks explicitly (a ``monkeypatch`` always wins
over this default).
"""
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner
from seam import seam


def rendered_prompt(command: list[str]) -> str:
    """The system prompt Pi loads from one built argv (Issue #1634).

    `build_pi_command` hands Pi a PATH — the rendered prompt is written
    to `<session_dir>/system-prompt.md` because Linux caps one argv
    string at 131072 bytes — and Pi's own `resolvePromptInput` reads the
    file when the flag value is an existing path, falling back to the
    string itself when it is not. This helper mirrors that contract for
    the tests that inspect the prompt CONTENT.
    """
    value = command[command.index("--system-prompt") + 1]
    path = Path(value)
    return path.read_text(encoding="utf-8") if path.is_file() else value


def git(repo: Path, *args: str) -> str:
    """The one fail-fast git scaffold for the smoke/e2e suites (Issue
    #908): a non-zero exit is an AssertionError carrying the rc, stdout
    and stderr, never a silent pass. Guarded by
    ``tests/test_suite_hygiene.py``."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _default_cli_install_preflight(monkeypatch):
    """Default: the editable CLI install refresh (Issue #158) is a
    no-op that reports "unchanged" — the in-process dispatch tests use
    tmp repo_dirs that carry no tool env, and the real `uv tool
    install` must never run in them. The refresh's own tests and the
    wiring tests stub or exercise it explicitly (a ``monkeypatch``
    always wins over this default). The implementation lives in
    `orbi.runner` itself (see the NOTE there), so the stub
    patches ITS module global — the call `main()` makes."""
    import orbi.release as release

    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged",
    )
    # Issue #286: `process_release` now lives in `orbi.release` and reads
    # the refresh through the release module global — stub that binding
    # with the same default no-op.
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged",
    )


@pytest.fixture(autouse=True)
def _default_unit_drift_preflight(monkeypatch):
    """Default: the unit drift preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _default_transport_preflight(monkeypatch):
    """Default: the git transport preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})


@pytest.fixture(autouse=True)
def _default_runner_source_preflight(monkeypatch):
    """Default: the runner source freshness gate (Issue #525) passes.

    The in-process dispatch tests run inside THIS worktree, whose HEAD
    is a task branch — exactly the scene the real gate must reject — so
    the gate's own tests stub or exercise it explicitly (a
    `monkeypatch` always wins over this default)."""
    monkeypatch.setattr(
        runner, "check_runner_source_freshness", lambda *a, **k: {},
    )


@pytest.fixture(autouse=True)
def _default_health_check_preflight(monkeypatch):
    """Default: the tick-start self-health check (Issue #266) is a no-op.

    The in-process dispatch tests use tmp repo_dirs and strict
    `run_command` fakes that reject anything but their own traffic; the
    health check's own tests (`tests/test_runner_health.py`) and the
    wiring tests exercise it explicitly (a `monkeypatch` always wins over
    this default)."""
    import orbi.runner_health as runner_health

    monkeypatch.setattr(
        runner_health, "run_health_check", lambda *a, **k: [],
    )


@pytest.fixture(autouse=True)
def _reset_notify_binding(monkeypatch, tmp_path):
    """Default: unbind Telegram notification state between tests.

    `notify.tick_start` binds one tick's config and bot token in module
    globals (Issue #1673), so a test that bound a live configuration
    would otherwise make every later label test read a title or reach an
    endpoint. The reset is the adapter's own entry point — a tick with no
    chat id — so no test writes `notify` internals (Issue #1698); a
    test-level `monkeypatch` still wins over it."""
    from tests.fakes.telegram import reset_notify_state

    reset_notify_state(monkeypatch, tmp_path)


@pytest.fixture
def systemd_scheduler(monkeypatch):
    """Pin `scheduler.detect()` to the systemd impl on any host.

    The deployment/health suites contract the SYSTEMD behavior over
    systemd-shaped fixtures (fake `run_command`, tmp unit dirs). Left
    to the running platform, a macOS host dispatches to the launchd
    impl (Issue #849) and the same fixtures miss its plist template.
    `detect(system)` is the documented override seam, and `detect` is
    bound only in `orbi.scheduler` (every consumer reads the module
    attribute), so one patch here pins install/drift/doctor/setup/
    health checks alike. The launchd impl keeps its own explicit tests
    (`tests/fakes/launchd.py`); a test-level `monkeypatch` always wins
    over this pin.
    """
    from orbi import scheduler

    real_detect = scheduler.detect
    monkeypatch.setattr(
        scheduler, "detect", lambda system=None: real_detect("Linux"),
    )


@pytest.fixture
def permissions(monkeypatch):
    """Author login -> repository permission for the trust gates.

    Issue #1806: a comment is trusted — and an in-ticket command accepted
    — from the repository permission GitHub reports for its author, so a
    test that needs a maintainer grants one here. An ungranted login reads
    as `"none"` (GitHub's answer for a user with no access), so nothing
    is trusted by accident. The authenticated identity is pinned to a bot
    nobody's comment names, so the own-account fallback of
    `_comment_is_trusted` stays out of these tests (a test that exercises
    it patches the login itself, which wins over this default).
    """
    grants: dict[str, str] = {}
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: grants.get(login, "none"),
    )
    monkeypatch.setattr(seam, "_authenticated_github_login",
                        lambda: "orbi-bot[bot]")
    return grants
