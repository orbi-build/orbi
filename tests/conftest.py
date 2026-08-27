"""Shared test fixtures for the Muyan Pilot suite.

The deployment preflights (Issue #103 unit drift, Issue #114 git
transport) read the REAL machine state (the user unit directory, the
checkout's ``origin`` remote, SSH connectivity); the in-process
dispatch tests use tmp repo_dirs that carry no ``systemd/`` templates
and no git checkout, so they run with both preflights stubbed to a
passing no-op by default. The wiring tests and the e2e suites stub or
exercise the real checks explicitly (a ``monkeypatch`` always wins
over this default).
"""
import pytest

import bootstrap_runner as runner


@pytest.fixture(autouse=True)
def _default_unit_drift_preflight(monkeypatch):
    """Default: the unit drift preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _default_transport_preflight(monkeypatch):
    """Default: the git transport preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
