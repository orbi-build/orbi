"""Systemd deployment consistency tests (Issue #103).

The repo templates (``systemd/muyan-pilot.service`` and
``muyan-pilot.timer``) are the single source of truth. The install
command is idempotent and must never start/stop/restart the service:
a currently running Runner keeps running, and the new config takes
effect at the next service start. The pre-start check compares the
installed units against the templates and fails fast with a
structured ``unit_drift`` line when they drift.
"""
import hashlib
import subprocess
from pathlib import Path

import pytest

import systemd_deploy


def make_repo(tmp_path: Path) -> Path:
    """A deployment checkout carrying the two unit templates."""
    repo = tmp_path / "repo"
    systemd = repo / "systemd"
    systemd.mkdir(parents=True)
    (systemd / "muyan-pilot.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n",
        encoding="utf-8",
    )
    (systemd / "muyan-pilot.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    return repo


def make_installed(
    tmp_path: Path, repo: Path, mutate: str | None = None,
) -> Path:
    """An installed unit dir holding copies of the repo templates."""
    installed = tmp_path / "home" / ".config" / "systemd" / "user"
    installed.mkdir(parents=True)
    for name in systemd_deploy.UNIT_NAMES:
        (installed / name).write_bytes(
            (repo / "systemd" / name).read_bytes(),
        )
    if mutate is not None:
        (installed / mutate).write_text(
            (installed / mutate).read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        )
    return installed


def test_unit_names_cover_service_and_timer():
    # The check must cover BOTH units (Issue #103 requirement).
    assert systemd_deploy.UNIT_NAMES == (
        "muyan-pilot.service", "muyan-pilot.timer",
    )


def test_repo_unit_dir_points_at_the_systemd_directory(tmp_path):
    repo = tmp_path / "repo"
    assert systemd_deploy.repo_unit_dir(repo) == repo / "systemd"


def test_installed_unit_dir_defaults_to_the_user_config_dir(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("MUYAN_PILOT_UNIT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert systemd_deploy.installed_unit_dir() == (
        tmp_path / ".config" / "systemd" / "user"
    )


def test_installed_unit_dir_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MUYAN_PILOT_UNIT_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert systemd_deploy.installed_unit_dir() == (
        xdg / "systemd" / "user"
    )


def test_installed_unit_dir_explicit_argument_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MUYAN_PILOT_UNIT_DIR", raising=False)
    target = tmp_path / "elsewhere"
    assert systemd_deploy.installed_unit_dir(str(target)) == target


def test_installed_unit_dir_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "override"
    monkeypatch.setenv("MUYAN_PILOT_UNIT_DIR", str(target))
    assert systemd_deploy.installed_unit_dir() == target


def test_sha256_hex_matches_the_file_content(tmp_path):
    path = tmp_path / "unit.service"
    path.write_bytes(b"[Service]\n")
    assert systemd_deploy.sha256_hex(path) == hashlib.sha256(
        b"[Service]\n",
    ).hexdigest()


def test_unit_status_reports_clean_when_templates_match(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    status = systemd_deploy.unit_status(repo, installed)
    assert [entry["unit"] for entry in status] == list(
        systemd_deploy.UNIT_NAMES,
    )
    for entry in status:
        assert entry["drifted"] is False
        assert entry["missing"] is False
        assert entry["repo_sha256"] == entry["installed_sha256"]
        assert entry["repo_path"] == repo / "systemd" / entry["unit"]
        assert entry["installed_path"] == installed / entry["unit"]


def test_unit_status_reports_drift_for_a_changed_unit(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="muyan-pilot.timer")
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["muyan-pilot.service"]["drifted"] is False
    assert by_unit["muyan-pilot.timer"]["drifted"] is True
    assert by_unit["muyan-pilot.timer"]["missing"] is False
    assert (
        by_unit["muyan-pilot.timer"]["repo_sha256"]
        != by_unit["muyan-pilot.timer"]["installed_sha256"]
    )


def test_unit_status_reports_missing_installed_unit_as_drift(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    (installed / "muyan-pilot.service").unlink()
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["muyan-pilot.service"]["drifted"] is True
    assert by_unit["muyan-pilot.service"]["missing"] is True
    assert by_unit["muyan-pilot.service"]["installed_sha256"] is None
    assert by_unit["muyan-pilot.timer"]["drifted"] is False


def test_unit_status_reports_missing_template_as_drift(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    (repo / "systemd" / "muyan-pilot.timer").unlink()
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["muyan-pilot.timer"]["drifted"] is True
    assert by_unit["muyan-pilot.timer"]["repo_sha256"] is None
    assert by_unit["muyan-pilot.service"]["drifted"] is False


def test_drift_lines_carry_paths_hashes_and_fix_command(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="muyan-pilot.service")
    status = systemd_deploy.unit_status(repo, installed)
    lines = systemd_deploy.drift_lines(status)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("unit_drift unit=muyan-pilot.service ")
    assert f"repo={repo / 'systemd' / 'muyan-pilot.service'}" in line
    assert f"installed={installed / 'muyan-pilot.service'}" in line
    drifted = [e for e in status if e["drifted"]][0]
    assert f"repo_sha256={drifted['repo_sha256']}" in line
    assert f"installed_sha256={drifted['installed_sha256']}" in line
    assert "fix=" in line
    assert "install-units" in line


def test_drift_lines_quote_values_with_spaces(tmp_path):
    # A repo path that really carries a space: the field must be
    # quoted (the pi_activity.quote_value convention) so the line
    # stays parseable.
    spaced = tmp_path / "my repo"
    repo = make_repo(spaced)
    installed = make_installed(spaced, repo, mutate="muyan-pilot.timer")
    status = systemd_deploy.unit_status(repo, installed)
    lines = systemd_deploy.drift_lines(status)
    assert f'repo="{repo / "systemd" / "muyan-pilot.timer"}"' in lines[0]
    assert f'installed="{installed / "muyan-pilot.timer"}"' in lines[0]


def test_drift_lines_is_empty_when_clean(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    status = systemd_deploy.unit_status(repo, installed)
    assert systemd_deploy.drift_lines(status) == []


def test_check_unit_drift_raises_with_all_drifted_units(tmp_path, caplog):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="muyan-pilot.service")
    (installed / "muyan-pilot.timer").unlink()
    with caplog.at_level("ERROR"):
        with pytest.raises(
            systemd_deploy.UnitDriftError, match="unit_drift",
        ) as excinfo:
            systemd_deploy.check_unit_drift(repo, installed)
    message = str(excinfo.value)
    assert "muyan-pilot.service" in message
    assert "muyan-pilot.timer" in message
    assert "install-units" in message
    # Every drifted unit is logged as a structured line.
    assert "unit_drift unit=muyan-pilot.service" in caplog.text
    assert "unit_drift unit=muyan-pilot.timer" in caplog.text


def test_check_unit_drift_logs_clean_and_returns(tmp_path, caplog):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    with caplog.at_level("INFO"):
        systemd_deploy.check_unit_drift(repo, installed)
    assert "unit_drift clean" in caplog.text


def test_check_unit_drift_uses_the_default_installed_dir(monkeypatch,
                                                        tmp_path):
    """Without an explicit dir the check reads the standard user dir
    (the MUYAN_PILOT_UNIT_DIR override is honored)."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    monkeypatch.setenv("MUYAN_PILOT_UNIT_DIR", str(installed))
    systemd_deploy.check_unit_drift(repo)  # clean: must not raise


def test_install_units_copies_templates_and_reloads(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    result = systemd_deploy.install_units(
        repo, installed, run_command=fake_run,
    )
    # Both templates are installed (overwritten) at the target dir.
    for name in systemd_deploy.UNIT_NAMES:
        assert (installed / name).read_bytes() == (
            repo / "systemd" / name
        ).read_bytes()
    # daemon-reload runs, the timer is enabled (idempotent), and the
    # service is NEVER started/stopped/restarted by the install.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl", "--user", "enable", "--now", "muyan-pilot.timer",
    ] in calls
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable")
    # The deployed commit is the deployment checkout's HEAD.
    assert result["commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert result["installed_dir"] == installed
    assert sorted(result["units"]) == sorted(systemd_deploy.UNIT_NAMES)
    for name in systemd_deploy.UNIT_NAMES:
        entry = result["units"][name]
        assert entry["installed_path"] == installed / name
        assert entry["sha256"] == systemd_deploy.sha256_hex(
            repo / "systemd" / name,
        )


def test_install_units_is_idempotent_and_overwrites_drift(
    monkeypatch, tmp_path,
):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    first = systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    # Simulate drift after the first install.
    (installed / "muyan-pilot.service").write_text(
        "tampered\n", encoding="utf-8",
    )
    second = systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    # The repo template wins again: the hashes are identical across
    # installs (idempotent) and the drift is gone.
    assert first["units"] == second["units"]
    status = systemd_deploy.unit_status(repo, installed)
    assert all(entry["drifted"] is False for entry in status)


def test_install_units_defaults_to_the_standard_installed_dir(
    monkeypatch, tmp_path,
):
    """Without an explicit dir the install targets the standard user
    dir (here pointed at the test world via MUYAN_PILOT_UNIT_DIR)."""
    repo = make_repo(tmp_path)
    target = tmp_path / "std"
    monkeypatch.setenv("MUYAN_PILOT_UNIT_DIR", str(target))
    result = systemd_deploy.install_units(
        repo, run_command=lambda command, **kwargs: "",
    )
    assert result["installed_dir"] == target
    for name in systemd_deploy.UNIT_NAMES:
        assert (target / name).is_file()


def test_install_units_fails_fast_when_a_systemctl_step_fails(
    monkeypatch, tmp_path,
):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"

    def fake_run(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "enable"]:
            raise subprocess.CalledProcessError(1, command, stderr="nope")
        return ""

    with pytest.raises(subprocess.CalledProcessError):
        systemd_deploy.install_units(
            repo, installed, run_command=fake_run,
        )


def test_install_units_fails_fast_on_missing_template(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "systemd" / "muyan-pilot.timer").unlink()
    with pytest.raises(FileNotFoundError, match="muyan-pilot.timer"):
        systemd_deploy.install_units(
            repo, tmp_path / "install",
            run_command=lambda command, **kwargs: "",
        )


def test_unit_drift_error_is_a_runtime_error():
    assert issubclass(systemd_deploy.UnitDriftError, RuntimeError)
