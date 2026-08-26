"""Regression tests for the systemd scheduling files (Issues #21, #33).

The scheduler runs 24 hours a day: the idle polling interval is 15 minutes
across the full day (00:00, 00:15, ..., 23:45). The timer must not add a
task duration limit, must not queue catch-up ticks, and the README must
document the same schedule as the unit files.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMER_FILE = REPO_ROOT / "systemd" / "muyan-pilot.timer"
SERVICE_FILE = REPO_ROOT / "systemd" / "muyan-pilot.service"
README_FILE = REPO_ROOT / "README.md"


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """Parse a systemd unit file into section -> key -> [values]."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, separator, value = line.partition("=")
        if not separator or current is None:
            raise ValueError(f"unparseable unit line: {raw_line!r}")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def test_timer_polls_ready_issues_every_15_minutes_all_day():
    timer = parse_unit(TIMER_FILE)
    on_calendar = timer["Timer"]["OnCalendar"]
    # Triggers: 00:00, 00:15, ..., 23:45 — every tick across the full day.
    assert on_calendar == ["*-*-* *:00/15"]
    # Semantic guard: the hour field must be open (no night-window range such
    # as 01..06) and the minute field must step by 15 minutes.
    date_part, time_part = on_calendar[0].split(" ")
    assert date_part == "*-*-*"
    hour_field, minute_field = time_part.split(":")
    assert hour_field == "*"
    assert minute_field == "00/15"


def test_timer_calendar_expression_parses_with_systemd_analyze():
    # Acceptance: the systemd calendar must be parseable by systemd-analyze.
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not available on this machine")
    on_calendar = parse_unit(TIMER_FILE)["Timer"]["OnCalendar"][0]
    result = subprocess.run(
        [analyze, "calendar", on_calendar],
        capture_output=True, text=True, check=True,
    )
    assert "Normalized form" in result.stdout


def test_timer_calendar_test_skips_without_systemd_analyze(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception):
        test_timer_calendar_expression_parses_with_systemd_analyze()


def test_timer_does_not_queue_catch_up_ticks_or_second_service():
    timer = parse_unit(TIMER_FILE)
    # While muyan-pilot.service is active systemd does not start a second
    # instance of the same unit; Persistent=false additionally means a
    # missed tick is dropped instead of queued for a second task.
    assert timer["Timer"]["Persistent"] == ["false"]
    assert timer["Timer"]["AccuracySec"] == ["30s"]
    assert timer["Timer"]["Unit"] == ["muyan-pilot.service"]
    assert timer["Install"]["WantedBy"] == ["timers.target"]


def test_service_keeps_running_task_without_duration_limit():
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert section["Type"] == ["simple"]
    # Deployment adapter only (systemd startup kill limit), not a task timeout.
    assert section["TimeoutStartSec"] == ["infinity"]
    assert "RuntimeMaxSec" not in section
    assert "TimeoutStopSec" not in section
    assert section["ExecStart"] == [
        "/usr/bin/python3 %h/Documents/muyan/muyan-pilot/bootstrap_runner.py",
    ]


def test_service_fast_forwards_main_before_runner_starts():
    """Issue #52: the service must fast-forward the local main checkout
    to the latest origin/main BEFORE the Runner starts (ExecStartPre,
    outside the Python process). A dirty checkout, a failed fetch or a
    non-fast-forwardable state makes the preflight command fail: the
    service does not start and the reason lands in the systemd journal
    (fail fast). No new refresh service, worker or dispatcher: only the
    existing service and timer exist."""
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert "ExecStartPre" in section
    pre = section["ExecStartPre"][0]
    assert "git fetch origin main" in pre
    assert "git merge --ff-only origin/main" in pre
    # The preflight runs in the main checkout (the unit's
    # WorkingDirectory), before the Python Runner.
    assert "WorkingDirectory" in section
    systemd_files = sorted(
        path.name for path in (REPO_ROOT / "systemd").iterdir()
        if path.is_file()
    )
    assert systemd_files == ["muyan-pilot.service", "muyan-pilot.timer"]


def test_readme_documents_code_update_at_next_runner_start():
    """Issue #52: the README must explain that code updates take effect
    at the next Runner start (the preflight fetch + ff-only merge), not
    while a task is running."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "ExecStartPre" in readme
    assert "git merge --ff-only origin/main" in readme


def test_readme_documents_same_schedule_as_timer():
    timer = parse_unit(TIMER_FILE)
    on_calendar = timer["Timer"]["OnCalendar"][0]
    match = re.match(r"\*-\*-\* \*:\d+/(?P<minutes>\d+)", on_calendar)
    assert match is not None, f"unexpected OnCalendar format: {on_calendar}"
    readme = README_FILE.read_text(encoding="utf-8")
    assert f"每 {match['minutes']} 分钟自动执行一次" in readme


def test_parse_unit_rejects_unparseable_line(tmp_path):
    bad = tmp_path / "bad.service"
    bad.write_text("[Service]\nnot a key value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unparseable unit line"):
        parse_unit(bad)


def test_parse_unit_rejects_key_before_any_section(tmp_path):
    bad = tmp_path / "bad.service"
    bad.write_text("Description=orphan\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unparseable unit line"):
        parse_unit(bad)
