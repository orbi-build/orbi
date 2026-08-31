"""Regression tests for the systemd scheduling files (Issues #21, #33,
#51, #149).

The scheduler runs 24 hours a day: the idle polling interval is 5 minutes
across the full day (00:00, 00:05, ..., 23:55). The timer must not add a
task duration limit, must not queue catch-up ticks, and the README must
document the same schedule as the unit files.

Issue #149: the units are TEMPLATES (`muyan-pilot@.service` /
`muyan-pilot@.timer`) and the deployment enables two timer instances
(`muyan-pilot@1.timer`, `muyan-pilot@2.timer`), each triggering its own
service instance, so two independent Runner instances can run
concurrently. The service `ExecStartPre` wraps the fetch +
fast-forward in a short-lived `flock` so two instances starting in the
same tick never write the main worktree concurrently.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMER_FILE = REPO_ROOT / "systemd" / "muyan-pilot@.timer"
SERVICE_FILE = REPO_ROOT / "systemd" / "muyan-pilot@.service"
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
    # While one service instance is active systemd does not start a
    # second instance of the SAME unit; Persistent=false additionally
    # means a missed tick is dropped instead of queued for a second
    # task.
    assert timer["Timer"]["Persistent"] == ["false"]
    assert timer["Timer"]["AccuracySec"] == ["30s"]
    assert timer["Install"]["WantedBy"] == ["timers.target"]


def test_timer_template_triggers_its_own_service_instance():
    """Issue #149: the timer template must name the service instance of
    the SAME instance argument (`%i`): `muyan-pilot@1.timer` starts
    `muyan-pilot@1.service`, `muyan-pilot@2.timer` starts
    `muyan-pilot@2.service` — two independent Runner instances, no
    dispatcher."""
    timer = parse_unit(TIMER_FILE)
    assert timer["Timer"]["Unit"] == ["muyan-pilot@%i.service"]


def test_service_keeps_running_task_without_duration_limit():
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert section["Type"] == ["simple"]
    # Deployment adapter only (systemd startup kill limit), not a task timeout.
    assert section["TimeoutStartSec"] == ["infinity"]
    assert "RuntimeMaxSec" not in section
    assert "TimeoutStopSec" not in section
    # Issue #140: the service starts the installed `muyan-pilot` CLI
    # (the uv-tool console script, explicit deployable absolute entry),
    # not a hand-written Python file entry.
    assert section["ExecStart"] == ["%h/.local/bin/muyan-pilot"]


def test_service_loads_optional_provider_env_file():
    """Issue #172: the systemd-launched Runner must be able to obtain
    the provider API keys referenced by the provider file. The service
    loads the user-local env file from the gitignored state dir
    (`.muyan-pilot/env`, next to `base-sync.lock`) with the `-` optional
    prefix: a deployment without provider files starts unchanged, and
    the key itself lives only in that local file — never in the
    template, the repo, or the journal."""
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert section["EnvironmentFile"] == [
        "-%h/Documents/muyan/muyan-pilot/.muyan-pilot/env"
    ]


def test_service_fast_forwards_main_before_runner_starts():
    """Issue #52: the service must fast-forward the local main checkout
    to the latest origin/main BEFORE the Runner starts (ExecStartPre,
    outside the Python process). A dirty checkout, a failed fetch or a
    non-fast-forwardable state makes the preflight command fail: the
    service does not start and the reason lands in the systemd journal
    (fail fast). No new refresh service, worker or dispatcher: only the
    existing Runner service and timer exist. The independent exporter
    service is not a Runner unit."""
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert "ExecStartPre" in section
    pre = section["ExecStartPre"][0]
    assert pre.startswith("/usr/bin/timeout 90s /usr/bin/flock ")
    assert "git fetch --no-auto-maintenance origin main" in pre
    assert "git merge --ff-only origin/main" in pre
    # The preflight runs in the main checkout (the unit's
    # WorkingDirectory), before the Python Runner.
    assert "WorkingDirectory" in section
    runner_units = sorted(
        path.name for path in (REPO_ROOT / "systemd").iterdir()
        if path.is_file() and path.name.startswith("muyan-pilot@")
    )
    assert runner_units == ["muyan-pilot@.service", "muyan-pilot@.timer"]


def test_service_preflight_is_serialized_with_a_short_lived_flock():
    """Issue #149: two instances may run ExecStartPre in the same tick,
    so the fetch + fast-forward must be wrapped in a short-lived
    `flock` on the shared state-dir lock file (the Python-side sync in
    bootstrap_runner takes the SAME lock): the main worktree is never
    written concurrently. The flock must run the git commands (the
    fail-fast semantics are unchanged: a failed fetch or merge is a
    non-zero preflight)."""
    service = parse_unit(SERVICE_FILE)
    pre = service["Service"]["ExecStartPre"][0]
    assert pre.startswith("/usr/bin/timeout 90s /usr/bin/flock ")
    assert "/.muyan-pilot/base-sync.lock" in pre
    assert (
        " -c 'git fetch --no-auto-maintenance origin main && "
        "git merge --ff-only origin/main'"
    ) in pre


def test_templates_and_instances_pass_systemd_analyze_verify(
    monkeypatch, tmp_path,
):
    """Issue #149 acceptance: `systemd-analyze --user verify` must pass
    for the service/timer TEMPLATES and the INSTANCES (verified against
    the real CLI on a machine with a user systemd). The templates are
    copied into a throwaway user unit dir (XDG_CONFIG_HOME) so the real
    user units are never touched."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not available on this machine")
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("muyan-pilot@.service", "muyan-pilot@.timer"):
        shutil.copyfile(REPO_ROOT / "systemd" / name, unit_dir / name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in ("muyan-pilot@.service", "muyan-pilot@.timer",
                 "muyan-pilot@1.service", "muyan-pilot@1.timer",
                 "muyan-pilot@2.timer"):
        result = subprocess.run(
            [analyze, "--user", "verify", str(unit_dir / name)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"systemd-analyze --user verify failed for {name}: "
            f"{result.stdout} {result.stderr}"
        )


def test_analyze_verify_skips_without_systemd_analyze(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception):
        test_templates_and_instances_pass_systemd_analyze_verify(
            monkeypatch, Path("/tmp"),
        )


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
    assert "muyan-pilot install-units" in readme
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
    units are synced. Issue #149: the templates are the INSTANTIATED
    units and the README documents the one-time legacy migration away
    from the pre-#149 non-templated units."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "unit_drift" in readme
    # Both template units are covered.
    assert "muyan-pilot@.service" in readme
    assert "muyan-pilot@.timer" in readme
    # The two enabled timer instances.
    assert "muyan-pilot@1.timer" in readme
    assert "muyan-pilot@2.timer" in readme
    # The one-time legacy migration names the old units.
    assert "muyan-pilot.service" in readme
    assert "muyan-pilot.timer" in readme
    # The structured line's fields.
    assert "repo_sha256=" in readme
    assert "installed_sha256=" in readme
    assert "fix=muyan-pilot install-units" in readme
    # No claim while drifted.
    assert "不领取" in readme
    # The repo templates are the single source of truth.
    assert "唯一事实源" in readme


def test_readme_documents_the_doctor_command():
    """Issue #103: the README must document the read-only `doctor`
    report: repo commit, unit drift, timer/service active state,
    current Issue, Runner/Pi and recent journal activity."""
    readme = README_FILE.read_text(encoding="utf-8")
    assert "muyan-pilot doctor" in readme
    assert "journal" in readme
    # doctor is read-only.
    assert "只读" in readme


def test_readme_documents_the_full_deployment_sequence():
    """Issue #103/#142: the README must show the complete sequence from
    code merge to the next Runner start: merge (template changes need
    no human step) -> timer next trigger -> ExecStartPre syncs
    origin/main -> pre-start drift check (self-heal + re-verify when
    drifted) -> Runner starts one Issue."""
    readme = README_FILE.read_text(encoding="utf-8")
    sequence = readme.split("完整部署时序", 1)[-1]
    assert "git merge 到 main" in sequence
    assert "timer 下一次触发" in sequence
    assert "ExecStartPre 同步 origin/main" in sequence
    assert "启动前 unit 漂移检查" in sequence
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


def test_template_change_pins_the_self_healing_contract():
    """Issue #131/#142 regression: a `systemd/` template change is a
    deployment change. The 2026-08-27/#140 incidents: the timer
    template change (PR #130) and the service `ExecStart` change
    (#140) merged to main while the installed units stayed stale, so
    every service start failed the pre-start `unit_drift` check until
    a human ran the fix command — a per-tick drift loop.

    The contract must therefore be pinned in EVERY place it is
    documented — README, AGENTS.md and the EN/ZH operations pages —
    so a future template change cannot merge and strand the
    deployment again: the change takes effect WITHOUT a human step
    (the pre-start drift check self-heals with the same idempotent
    install and re-verifies, `unit_drift auto_synced`), `install-units`
    stays the manual entry, and the fail-fast canary is unchanged for
    a drift the self-heal cannot resolve."""
    def template_change_contract(text: str) -> None:
        # The trigger: a change of either repo unit template
        # (templated units since Issue #149).
        assert "systemd/muyan-pilot@.timer" in text
        assert "systemd/muyan-pilot@.service" in text
        # The self-heal: the structured auto_synced line.
        assert "unit_drift auto_synced" in text
        # install-units stays the manual entry (setup, immediate sync).
        assert "install-units" in text
        # The drift check stays fail-fast for an unresolvable drift.
        assert "unit_drift" in text
        assert "fail fast" in text

    readme = README_FILE.read_text(encoding="utf-8")
    # The README pins it in the deployment-consistency section (the
    # section that carries the full deployment sequence).
    section = readme.split("部署一致性", 1)[-1]
    template_change_contract(section)

    agents = AGENTS_FILE.read_text(encoding="utf-8")
    # AGENTS.md pins it inside the Base freshness / deployment
    # consistency contract.
    agents_section = agents.split("## Base freshness", 1)[-1]
    template_change_contract(agents_section)

    # EN/ZH operations pages carry the same contract (the i18n parity
    # contract: both languages document the same fact).
    for slug in ("operations.mdx", "zh/operations.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        template_change_contract(page)
