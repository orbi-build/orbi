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


def capture_finish_rounds(monkeypatch) -> list[int]:
    """Record the `review_round` every finish scene is published with.

    A round below 3 renders no visible `review/fix round` row, so a test
    asserting WHICH round a failure report carries reads the value the
    scene is built from (Issue #1779) instead of the folded body.
    """
    from orbi import failure_report

    rounds: list[int] = []
    real = failure_report._finish_progress_body

    def spy(**kwargs):
        rounds.append(kwargs["review_round"])
        return real(**kwargs)

    monkeypatch.setattr(failure_report, "_finish_progress_body", spy)
    return rounds


def patched_comment_bodies(api_calls: list[list[str]]) -> list[str]:
    """The bodies of the `gh api --method PATCH` calls, in call order."""
    return [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "PATCH" in command
    ]
