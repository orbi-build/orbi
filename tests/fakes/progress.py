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


def bind_run_id(monkeypatch, run_id: str = "a1b2c3d4") -> str:
    """Bind the run id the progress run marker carries.

    A delivery step locates the run's tracked progress comment by this
    marker, so a test asserting on the finish scene binds it first.
    """
    from orbi import journal

    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", run_id)
    return run_id


def patched_comment_bodies(api_calls: list[list[str]]) -> list[str]:
    """The bodies of the `gh api --method PATCH` calls, in call order."""
    return [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "PATCH" in command
    ]
