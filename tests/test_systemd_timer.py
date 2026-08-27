"""Regression tests for the systemd scheduling files (Issues #21, #33, #51).

The scheduler runs 24 hours a day: the idle polling interval is 5 minutes
across the full day (00:00, 00:05, ..., 23:55). The timer must not add a
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
AGENTS_FILE = REPO_ROOT / "AGENTS.md"


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


def test_timer_polls_ready_issues_every_5_minutes_all_day():
    timer = parse_unit(TIMER_FILE)
    on_calendar = timer["Timer"]["OnCalendar"]
    # Triggers: 00:00, 00:05, ..., 23:55 — every tick across the full day.
    assert on_calendar == ["*-*-* *:00/5"]
    # Semantic guard: the hour field must be open (no night-window range such
    # as 01..06) and the minute field must step by 5 minutes.
    date_part, time_part = on_calendar[0].split(" ")
    assert date_part == "*-*-*"
    hour_field, minute_field = time_part.split(":")
    assert hour_field == "*"
    assert minute_field == "00/5"


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


# --- deployment consistency contract (Issue #103) ---------------------------


def test_readme_documents_the_idempotent_install_command():
    """Issue #103: the README must document the idempotent install
    command (repo templates -> user systemd dir, daemon-reload,
    enable timer, deployed commit/hash output) and the guarantee that
    it never kills or restarts a running Runner (the new config takes
    effect at the next service start)."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "muyan_pilot.py install-units" in readme
    assert "daemon-reload" in readme
    # The manual cp is no longer the documented install path.
    assert "cp systemd/muyan-pilot.service" not in readme
    # Deployed commit/hash output.
    assert "commit" in readme
    assert "sha256" in readme
    # The no-kill guarantee.
    assert "不会" in readme
    assert "重启" in readme


def test_readme_documents_the_unit_drift_fail_fast():
    """Issue #103: the README must document the pre-start drift check:
    both units are compared against the repo templates, drift logs a
    structured `unit_drift` line (repo path, installed path, hashes,
    fix command) and fails fast without claiming any Issue until the
    units are synced."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "unit_drift" in readme
    # Both units are covered.
    assert "muyan-pilot.service" in readme
    assert "muyan-pilot.timer" in readme
    # The structured line's fields.
    assert "repo_sha256=" in readme
    assert "installed_sha256=" in readme
    assert "fix=python3 muyan_pilot.py install-units" in readme
    # No claim while drifted.
    assert "不领取" in readme
    # The repo templates are the single source of truth.
    assert "唯一事实源" in readme


def test_readme_documents_the_doctor_command():
    """Issue #103: the README must document the read-only `doctor`
    report: repo commit, unit drift, timer/service active state,
    current Issue, Runner/Pi and recent journal activity."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "muyan_pilot.py doctor" in readme
    assert "journal" in readme
    # doctor is read-only.
    assert "只读" in readme


def test_readme_documents_the_full_deployment_sequence():
    """Issue #103: the README must show the complete sequence from code
    merge to the next Runner start: merge -> install units ->
    daemon-reload -> timer next trigger -> ExecStartPre syncs
    origin/main -> Runner starts one Issue."""
    readme = README_FILE.read_text(encoding="utf-8")
    sequence = readme.split("完整部署时序", 1)[-1]
    assert "git merge 到 main" in sequence
    assert "install units" in sequence
    assert "daemon-reload" in sequence
    assert "timer 下一次触发" in sequence
    assert "ExecStartPre 同步 origin/main" in sequence
    assert "Runner 启动并执行一个 Issue" in sequence


def test_agents_md_documents_the_deployment_consistency_contract():
    """Issue #103: AGENTS.md must carry the same contract: repo
    templates as single source of truth, the idempotent install that
    never kills/restarts a running Runner, the pre-start drift check
    (structured `unit_drift`, fail fast, no claim), and the read-only
    doctor."""
    text = AGENTS_FILE.read_text(encoding="utf-8")
    assert "single source of truth" in text
    assert "install-units" in text
    assert "unit_drift" in text
    assert "daemon-reload" in text
    assert "doctor" in text
    # The install never touches a running Runner.
    assert "never" in text
    assert "restart" in text
    # Drift blocks the start before any claim.
    assert "no claim" in text or "no slot" in text
