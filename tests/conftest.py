"""Shared test fixtures for the Muyan Pilot suite.

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
import pytest

import bootstrap_runner as runner
import cli_install


@pytest.fixture(autouse=True)
def _default_cli_install_preflight(monkeypatch):
    """Default: the editable CLI install refresh (Issue #158) is a
    no-op that reports "unchanged" — the in-process dispatch tests use
    tmp repo_dirs that carry no tool env, and the real `uv tool
    install` must never run in them. The refresh's own tests and the
    wiring tests stub or exercise it explicitly (a ``monkeypatch``
    always wins over this default)."""
    monkeypatch.setattr(
        cli_install, "refresh_cli_install", lambda *a, **k: "unchanged",
    )


@pytest.fixture(autouse=True)
def _default_unit_drift_preflight(monkeypatch):
    """Default: the unit drift preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _default_transport_preflight(monkeypatch):
    """Default: the git transport preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
