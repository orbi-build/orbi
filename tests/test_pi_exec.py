"""The Pi CLI prerequisite (Issues #2182, #2188): `orbi.pi_exec`.

One probe and one validity rule for the key: `pi_exec.check_pi_command`
is the only body that executes `<executable> --version` and formats its
stderr, and `pi_exec.configured` reads `pi_executable` through
`config.read_pi_executable`. Real files and the real PATH lookup: a fake
executable is a small script in `tmp_path`, addressed by its absolute
path. The default `pi` tests put a real `pi` script first on the
PATH, so they never depend on whether the host carries one.
"""
import os
import subprocess
from pathlib import Path

import pytest

from orbi import pi_exec, setup_checks

ERROR = setup_checks.CheckError
DOCS = setup_checks.DOCS_LINKS


def _fake_executable(tmp_path: Path) -> str:
    path = tmp_path / "pi-bolt"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _probe(run_command, executable: str = "pi"):
    return pi_exec.check_pi_command(run_command, executable, ERROR, DOCS)


@pytest.fixture
def pi_on_path(tmp_path, monkeypatch):
    """A real `pi` first on the PATH: the default-executable tests go
    through the real lookup and never depend on the host's own `pi`."""
    path = tmp_path / "pi"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    )


def test_probe_executes_the_version_and_keeps_the_node_floor_fix(
    pi_on_path,
):
    """Pi on the PATH is not enough (Issue #1079): npm does not enforce
    Pi's `engines` floor, so an install on a distro-default Node reports
    success and then every `pi` invocation crashes with a bundle-level
    SyntaxError. The ONE probe executes `pi --version` and the default
    `pi` fix names the Node >= 22.19 floor."""
    calls: list[list[str]] = []

    def broken(command, **kwargs):
        calls.append(list(command))
        raise subprocess.CalledProcessError(
            1, command,
            stderr="/usr/lib/node_modules/@earendil-works/pi/dist/"
                   "bundle.js:1\nSyntaxError: Unexpected token 'export'",
        )

    with pytest.raises(ERROR) as excinfo:
        _probe(broken)
    assert calls == [["pi", "--version"]]
    failure = excinfo.value
    assert failure.check == "pi"
    assert failure.reason == (
        "pi --version failed: /usr/lib/node_modules/@earendil-works/pi/"
        "dist/bundle.js:1 SyntaxError: Unexpected token 'export'"
    )
    assert "22.19" in failure.fix


def test_probe_ok_when_the_version_probe_succeeds(pi_on_path):
    calls = []
    assert _probe(lambda command, **kwargs: calls.append(command)) is None
    assert calls == [["pi", "--version"]]


def test_probe_uses_the_configured_executable(tmp_path):
    """Issue #2188: one probe — a configured `pi_executable` only changes
    its finding text."""
    executable = _fake_executable(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(list(command))
        return "1.5.2"

    assert _probe(run, executable) is None
    assert calls == [[executable, "--version"]]


def test_probe_names_a_missing_configured_executable(tmp_path):
    """Never the Node/npm advice for `pi`, and never a fallback to `pi`."""
    missing = str(tmp_path / "pi-bolt")
    calls = []
    with pytest.raises(ERROR) as excinfo:
        _probe(lambda command, **kwargs: calls.append(command), missing)
    failure = excinfo.value
    assert calls == []
    assert failure.check == "pi"
    assert missing in failure.reason and "pi_executable" in failure.reason
    assert "pi_executable" in failure.fix
    assert "Node" not in failure.fix and "npm" not in failure.fix


def test_probe_reports_a_failing_configured_executable(tmp_path):
    executable = _fake_executable(tmp_path)

    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(
            126, command, stderr="cannot execute binary file",
        )

    with pytest.raises(ERROR) as excinfo:
        _probe(broken, executable)
    failure = excinfo.value
    assert failure.reason == (
        f"{executable} --version failed: cannot execute binary file"
    )
    assert "pi_executable" in failure.fix
    assert "Node" not in failure.fix and "npm" not in failure.fix


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('pi_executable = "pi-bolt"\n', "pi-bolt"),
        ('source_repos = ["owner/repo"]\n', "pi"),
        ("not toml [", "pi"),
    ],
)
def test_configured_reads_the_key_and_defaults_when_it_cannot(
    tmp_path, body, expected,
):
    """A missing, unreadable or unparseable config is the config step's
    finding, so the machine-level read keeps the default `pi`."""
    config_file = tmp_path / "orbi.toml"
    config_file.write_text(body, encoding="utf-8")
    assert pi_exec.configured(config_file, ERROR, DOCS) == expected


def test_configured_defaults_when_the_config_is_missing(tmp_path):
    assert pi_exec.configured(tmp_path / "absent.toml", ERROR, DOCS) == "pi"


@pytest.mark.parametrize(
    "body",
    ['pi_executable = ""\n', "pi_executable = 5\n", "pi_executable = true\n"],
)
def test_configured_rejects_an_invalid_value_with_the_config_rule(
    tmp_path, body,
):
    """Issue #2188: the machine-level read applies the config's own rule
    (`config.read_pi_executable`), so an invalid `pi_executable` is the
    config finding instead of a silent probe of the default `pi`."""
    config_file = tmp_path / "orbi.toml"
    config_file.write_text(body, encoding="utf-8")
    with pytest.raises(ERROR) as excinfo:
        pi_exec.configured(config_file, ERROR, DOCS)
    failure = excinfo.value
    assert failure.check == "config"
    assert failure.reason == (
        "invalid orbi.toml: pi_executable must be a non-empty string"
    )
    assert failure.fix == "fix the reported problem in orbi.toml"
    assert failure.docs == DOCS["config"]


def test_ok_line_names_only_a_configured_executable():
    assert pi_exec.ok_line("pi") == "check=pi ok"
    assert pi_exec.ok_line("pi-bolt") == "check=pi ok executable=pi-bolt"
