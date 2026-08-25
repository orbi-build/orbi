"""Regression tests for the systemd scheduling files (Issue #21).

The idle polling interval is 15 minutes inside the 01:00-06:55 night window.
The timer must not add a task duration limit, must not queue catch-up ticks,
and the README must document the same schedule as the unit files.
"""
import re
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


def test_timer_polls_ready_issues_every_15_minutes_in_night_window():
    timer = parse_unit(TIMER_FILE)
    assert timer["Timer"]["OnCalendar"] == ["*-*-* 01..06:00/15:00"]
    # Triggers: 01:00, 01:15, ..., 06:45 — every tick inside 01:00-06:55.


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


def test_readme_documents_same_schedule_as_timer():
    timer = parse_unit(TIMER_FILE)
    on_calendar = timer["Timer"]["OnCalendar"][0]
    match = re.match(r"\*-\*-\* \d+\.\.\d+:\d+/(?P<minutes>\d+):\d+$", on_calendar)
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
