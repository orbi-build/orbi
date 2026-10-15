"""Fakes for the progress summary tests (Issue #1657).

The patch ratchet (Issue #789) keeps new tests from coupling to
orbi.progress internals through monkeypatch.setattr(progress, ...);
this directory is the sanctioned home for the coupling a test still
needs. freeze_report_time pins the report generation clock of the
versioned summary so a rendered body is byte-comparable.
"""
from __future__ import annotations


def freeze_report_time(
    monkeypatch, moment: str = "2026-10-15T06:20:20Z",
) -> str:
    """Pin the summary report clock; return the pinned moment."""
    from orbi import progress_summary

    monkeypatch.setattr(progress_summary, "utc_now_iso", lambda: moment)
    return moment
