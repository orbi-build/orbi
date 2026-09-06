import subprocess
from pathlib import Path

import pytest

from orbi import pilot_setup, runner, systemd_deploy


def test_named_units_are_distinct_and_install_without_touching_default(tmp_path):
    repo = tmp_path / "repo"
    systemd = repo / "systemd"
    systemd.mkdir(parents=True)
    (systemd / "orbi@.service").write_text(
        '[Service]\nEnvironment="ORBI_CONFIG={{ORBI_REPO_DIR}}/orbi.toml"\n'
    )
    (systemd / "orbi@.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    installed = tmp_path / "units"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return "deadbeef" if command[:3] == ["git", "rev-parse", "HEAD"] else ""

    systemd_deploy.install_units(repo, installed, unit_name="website", run_command=run)
    assert (installed / "orbi-website@.service").is_file()
    assert (installed / "orbi-website@.timer").is_file()
    assert not (installed / "orbi@.service").exists()
    assert ["systemctl", "--user", "enable", "--now", "orbi-website@1.timer"] in calls
    systemd_deploy.check_unit_drift(repo, installed, "website")


def test_named_setup_unit_step_passes_instance_name(tmp_path):
    repo = tmp_path / "repo"
    systemd = repo / "systemd"
    systemd.mkdir(parents=True)
    (systemd / "orbi@.service").write_text("[Service]\n")
    (systemd / "orbi@.timer").write_text("[Timer]\n")
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", unit_name="web",
        run_command=lambda command, **kwargs: "active" if command[:3] == ["systemctl", "--user", "show"] else "",
    )
    assert "orbi-web@1.timer" in result["timer"]["instances"]


def test_unit_name_is_optional_and_validated(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos=["owner/repo"]\n')
    assert runner.load_config(config)["unit_name"] is None
    config.write_text('source_repos=["owner/repo"]\nunit_name="web site"\n')
    with pytest.raises(ValueError, match="unit_name"):
        runner.load_config(config)


def test_runner_runtime_excludes_cover_worktrees():
    assert ".worktrees/" in runner.RUNNER_RUNTIME_EXCLUDES


def test_setup_excludes_runner_worktrees_and_reports_structured_change(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".worktrees").mkdir()
    info = repo / "git" / "info"
    info.mkdir(parents=True)
    exclude = info / "exclude"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "check-ignore", "--quiet"]:
            raise subprocess.CalledProcessError(1, command)
        if command[:4] == ["git", "rev-parse", "--git-path", "info/exclude"]:
            return "git/info/exclude"
        return ""

    assert pilot_setup.ensure_worktrees_ignored(repo, run_command=run) is True
    assert exclude.read_text() == ".worktrees/\n"
    assert run(["noop"]) == ""


def test_setup_does_not_duplicate_a_local_exclude_entry(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".worktrees").mkdir(parents=True)
    exclude = repo / "git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text(".worktrees/\n")
    def run(command, **kwargs):
        if command[:3] == ["git", "check-ignore", "--quiet"]:
            raise subprocess.CalledProcessError(1, command)
        return str(exclude)
    assert pilot_setup.ensure_worktrees_ignored(repo, run_command=run) is True
    assert exclude.read_text() == ".worktrees/\n"


def test_setup_keeps_an_already_ignored_worktrees_directory(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".worktrees").mkdir(parents=True)
    assert pilot_setup.ensure_worktrees_ignored(
        repo, run_command=lambda command, **kwargs: "ignored",
    ) is False
