"""Shared test fixtures for the Muyan Pilot suite.

The deployment preflight (Issue #103) reads the REAL user unit
directory on this machine; the in-process dispatch tests use tmp
repo_dirs that carry no ``systemd/`` templates, so they run with the
preflight stubbed to a no-op by default. The drift-wiring tests and
the e2e suites stub or exercise the real check explicitly (a
``monkeypatch`` always wins over this default).
"""
import pytest

import bootstrap_runner as runner


@pytest.fixture(autouse=True)
def _default_unit_drift_preflight(monkeypatch):
    """Default: the unit drift preflight passes (no-op)."""
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
