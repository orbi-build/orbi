"""The configured Pi executable (Issue #2182): `orbi.pi_exec`.

Real files and the real PATH lookup: a fake executable is a small script
in `tmp_path`, addressed by its absolute path.
"""
import subprocess
from pathlib import Path

import pytest

from orbi import pi_exec, setup_checks


def _fake_executable(tmp_path: Path) -> str:
    path = tmp_path / "pi-bolt"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _check(run_command, executable):
    return pi_exec.check(
        run_command, executable, setup_checks.check_pi_command,
        setup_checks.CheckError, "https://docs.example/config",
    )


def test_check_probes_the_configured_executable(tmp_path):
    executable = _fake_executable(tmp_path)
    calls = []
    assert _check(
        lambda command, **kwargs: calls.append(command) or "1.5.2",
        executable,
    ) is None
    assert calls == [[executable, "--version"]]


def test_check_names_a_missing_configured_executable(tmp_path):
    """Never the Node/npm advice for `pi`, and never a fallback to `pi`."""
    missing = str(tmp_path / "pi-bolt")
    calls = []
    with pytest.raises(setup_checks.CheckError) as excinfo:
        _check(lambda command, **kwargs: calls.append(command), missing)
    failure = excinfo.value
    assert calls == []
    assert failure.check == "pi"
    assert missing in failure.reason and "pi_executable" in failure.reason
    assert "pi_executable" in failure.fix
    assert "Node" not in failure.fix and "npm" not in failure.fix


def test_check_reports_a_failing_configured_executable(tmp_path):
    executable = _fake_executable(tmp_path)

    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(
            126, command, stderr="cannot execute binary file",
        )

    with pytest.raises(setup_checks.CheckError) as excinfo:
        _check(broken, executable)
    failure = excinfo.value
    assert f"{executable} --version failed" in failure.reason
    assert "cannot execute binary file" in failure.reason
    assert "pi_executable" in failure.fix
    assert "Node" not in failure.fix and "npm" not in failure.fix


def test_check_delegates_the_default_pi_to_its_own_probe():
    calls = []
    pi_exec.check(
        "run", "pi", calls.append, setup_checks.CheckError, "docs",
    )
    assert calls == ["run"]


def test_ok_line_names_only_a_configured_executable():
    assert pi_exec.ok_line("pi") == "check=pi ok"
    assert pi_exec.ok_line("pi-bolt") == "check=pi ok executable=pi-bolt"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('pi_executable = "pi-bolt"\n', "pi-bolt"),
        ('source_repos = ["owner/repo"]\n', "pi"),
        ('pi_executable = ""\n', "pi"),
        ("pi_executable = 123\n", "pi"),
        ("not toml [", "pi"),
    ],
)
def test_configured_reads_the_key_and_defaults_leniently(
    tmp_path, body, expected,
):
    config = tmp_path / "orbi.toml"
    config.write_text(body, encoding="utf-8")
    assert pi_exec.configured(config) == expected


def test_configured_defaults_when_the_config_is_missing(tmp_path):
    assert pi_exec.configured(tmp_path / "absent.toml") == "pi"
