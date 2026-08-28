"""CLI packaging contract (Issue #140).

The official usage is the installed `muyan-pilot` console script
(`muyan-pilot = muyan_pilot:main` in the PEP 621 `pyproject.toml`),
not a hand-written `python3 muyan_pilot.py`. These tests pin:

- the packaging file (console script, version, Python floor, the
  intentional zero runtime dependencies, and no hardcoded user
  directories/tokens in the release packaging);
- the systemd service template's CLI entry (ExecStart = the installed
  `muyan-pilot`, the uv-tool bin dir on the unit PATH, the unchanged
  WorkingDirectory and ExecStartPre);
- the CLI command strings the failure scenes carry (the unit_drift
  fix command, the HTTPS-remote migration entry);
- the documentation contract: README, AGENTS.md and the EN/ZH docs
  use `muyan-pilot` and no longer require the user to hand-write
  `python3 muyan_pilot.py`;
- the compatibility path: `muyan_pilot.py` keeps its direct-execution
  entry (development/compatibility, still asserted against one real
  call).
"""
import re
import tomllib
from pathlib import Path

import pytest

import git_transport
import pilot_setup
import systemd_deploy

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVICE_FILE = REPO_ROOT / "systemd" / "muyan-pilot@.service"
README_FILE = REPO_ROOT / "README.md"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"

# Every runtime module the installed console script imports (the
# flat-module layout of this repository).
RUNTIME_MODULES = (
    "muyan_pilot",
    "bootstrap_runner",
    "git_transport",
    "systemd_deploy",
    "pilot_setup",
    "pilot_slots",
    "pi_activity",
    "pi_recovery",
    "progress",
)

# The docs pages that document user-facing commands (EN + ZH parity).
DOC_PAGES = (
    "getting-started.mdx",
    "operations.mdx",
    "setup.mdx",
    "contributing.mdx",
    "zh/getting-started.mdx",
    "zh/operations.mdx",
    "zh/setup.mdx",
    "zh/contributing.mdx",
)


def load_pyproject() -> dict:
    assert PYPROJECT.is_file(), f"missing packaging file: {PYPROJECT}"
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


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


# --- the packaging file -------------------------------------------------------


def test_pyproject_declares_the_muyan_pilot_console_script():
    """Issue #140: the console script is exactly
    `muyan-pilot = muyan_pilot:main`."""
    data = load_pyproject()
    assert data["project"]["scripts"]["muyan-pilot"] == "muyan_pilot:main"


def test_pyproject_project_metadata():
    data = load_pyproject()
    project = data["project"]
    assert project["name"] == "muyan-pilot"
    assert project["version"] == "0.2.0"
    # The production interpreter is /usr/bin/python3 (3.14.6); the
    # package must not claim to run on an older minor version.
    assert project["requires-python"] == ">=3.14"
    # The bootstrap intentionally has no third-party runtime
    # dependency: the release package must not hardcode dependencies.
    assert project.get("dependencies", []) == []


def test_version_matches_the_packaging_metadata():
    """The `--version` output and the PEP 621 `version` must agree
    (one source of truth, no drift between the module and the
    packaging file)."""
    import muyan_pilot

    assert muyan_pilot.__version__ == load_pyproject()["project"]["version"]


def test_pyproject_builds_with_the_setuptools_backend():
    data = load_pyproject()
    assert "setuptools" in data["build-system"]["requires"][0]
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"


def test_pyproject_ships_every_runtime_module():
    """The installed console script imports the flat runtime modules;
    all of them must be listed so the `uv tool` install is complete."""
    data = load_pyproject()
    modules = data["tool"]["setuptools"]["py-modules"]
    for module in RUNTIME_MODULES:
        assert module in modules, f"runtime module missing: {module}"


def test_packaging_files_hardcode_no_user_dirs_or_tokens():
    """Issue #140: no dependency, token or user directory is hardcoded
    into the release packaging (the config path and the user unit dir
    stay machine-local, provided by muyan-pilot.toml / the user's
    systemd dir)."""
    forbidden = (
        "/home/",
        "/Users/",
        "C:\\\\Users",
        "ghp_",
        "github_pat_",
        "MUYAN_PILOT_CONFIG",
        "MUYAN_PILOT_UNIT_DIR",
    )
    for path in (PYPROJECT, REPO_ROOT / "requirements.txt"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, (
                f"{path.name} hardcodes {needle!r}: the release "
                "packaging must stay machine-independent"
            )


# --- the systemd service template ---------------------------------------------


def test_service_exec_start_uses_the_installed_cli():
    """Issue #140: the service starts the installed `muyan-pilot` CLI
    (the uv-tool console script in the user bin dir, as an explicit
    deployable absolute entry), not a hand-written
    `python3 .../bootstrap_runner.py`."""
    service = parse_unit(SERVICE_FILE)
    assert service["Service"]["ExecStart"] == ["%h/.local/bin/muyan-pilot"]


def test_service_keeps_working_directory_and_preflight():
    """The unit's WorkingDirectory (the deployment checkout, where
    ExecStartPre syncs origin/main and the config lives) and the
    Issue #52 preflight are unchanged by the CLI switch."""
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    assert section["WorkingDirectory"] == [
        "%h/Documents/muyan/muyan-pilot",
    ]
    pre = section["ExecStartPre"][0]
    assert "git fetch origin main" in pre
    assert "git merge --ff-only origin/main" in pre


def test_service_path_carries_the_uv_tool_bin_dir():
    """The installed console script lives in the uv tool bin dir
    (`~/.local/bin`); the unit PATH must carry it so the Runner's
    child processes (gh, pi, git) resolve like on the interactive
    shell."""
    service = parse_unit(SERVICE_FILE)
    path_value = service["Service"]["Environment"][0]
    assert "%h/.local/bin" in path_value


def test_service_template_passes_systemd_analyze_verify():
    """Issue #140 acceptance: the service template starts the Runner
    via the CLI entry — verified with the REAL `systemd-analyze
    --user verify` (the user manager context, where the `%h` specifier
    and the installed CLI resolve). The declared runtimes both put the
    installed CLI at the unit's ExecStart path: the production machine
    via `uv tool install` and CI via the workflow's
    `pip install --prefix $HOME/.local .` (both land the executable at
    `~/.local/bin/muyan-pilot`), so `systemd-analyze --user verify`
    resolves the unit's absolute ExecStart against a real executable
    on both — no skip needed (a missing executable is exactly the
    failure this check must catch, never skippable noise)."""
    import os
    import subprocess

    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(SERVICE_FILE)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"systemd-analyze verify failed rc={result.returncode} "
        f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
    )


# --- the CLI command strings --------------------------------------------------


def test_unit_drift_fix_command_is_the_cli():
    assert systemd_deploy.FIX_COMMAND == "muyan-pilot install-units"


def test_https_remote_migration_entry_is_the_cli():
    assert git_transport.MIGRATION_ENTRY == "muyan-pilot setup"


def test_setup_requires_the_installed_cli():
    """`setup` verifies the machine prerequisites; the installed CLI is
    one of them (the systemd entry it documents must exist)."""
    assert "muyan-pilot" in pilot_setup.REQUIRED_COMMANDS
    for name in ("git", "gh", "python3"):
        assert name in pilot_setup.REQUIRED_COMMANDS


# --- the documentation contract -----------------------------------------------


def test_readme_uses_the_cli_and_the_uv_tool_install():
    readme = README_FILE.read_text(encoding="utf-8")
    # The official commands are the installed CLI.
    assert "muyan-pilot install-units" in readme
    assert "muyan-pilot doctor" in readme
    assert "muyan-pilot setup" in readme
    assert "muyan-pilot add" in readme
    # The install/upgrade flow is the verifiable uv tool flow.
    assert "uv tool install" in readme
    assert "uv tool upgrade" in readme
    # The user is no longer required to hand-write the Python entry.
    assert "python3 muyan_pilot.py" not in readme
    # The compatibility path stays documented (development use only).
    assert "muyan_pilot.py" in readme


def test_agents_md_uses_the_cli():
    text = AGENTS_FILE.read_text(encoding="utf-8")
    assert "muyan-pilot install-units" in text
    assert "muyan-pilot doctor" in text
    assert "muyan-pilot setup" in text
    assert "python3 muyan_pilot.py" not in text


def test_docs_use_the_cli_and_never_require_the_python_entry():
    for slug in DOC_PAGES:
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "python3 muyan_pilot.py" not in page, (
            f"docs/{slug} still requires the hand-written Python entry"
        )


def test_docs_getting_started_documents_the_cli_install():
    """A new user must find the `uv tool install` command and the
    `muyan-pilot` CLI in the getting-started pages (EN + ZH)."""
    for slug in ("getting-started.mdx", "zh/getting-started.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "uv tool install" in page, (
            f"docs/{slug} must document the uv tool install"
        )
        assert "muyan-pilot" in page, (
            f"docs/{slug} must name the installed CLI"
        )


def test_docs_operations_name_the_cli():
    for slug in ("operations.mdx", "zh/operations.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "muyan-pilot" in page, f"docs/{slug} must name the CLI"
        for command in ("status", "session", "add"):
            assert command in page, (
                f"docs/{slug} must document the {command} command"
            )


# --- the bare entry IS the Runner (the systemd ExecStart) ----------------------


def test_bare_cli_runs_the_runner_tick(monkeypatch, tmp_path):
    """Issue #140 acceptance: `systemd 使用 CLI 入口可启动 Runner`.

    The service's `ExecStart` is the bare installed CLI (no
    subcommand), so `muyan-pilot` with NO arguments must run one
    Runner tick — exactly like the legacy `python3
    bootstrap_runner.py` — and must NOT die with the argparse error
    `the following arguments are required: command` (the pre-fix
    failure mode that made every timer tick exit 2 without ever
    starting the Runner)."""
    import muyan_pilot

    calls = []

    def fake_runner_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(
        muyan_pilot.bootstrap_runner, "main", fake_runner_main,
    )
    config = tmp_path / "muyan-pilot.toml"
    assert muyan_pilot.main(["--config", str(config)]) == 0
    assert calls == [["--config", str(config)]], (
        "the bare CLI must delegate to the Runner's main with the "
        f"same --config, got: {calls!r}"
    )


def test_bare_cli_default_config_delegates_to_the_runner(monkeypatch):
    """`muyan-pilot` with no arguments at all uses the default config
    path (MUYAN_PILOT_CONFIG / muyan-pilot.toml) and still delegates
    to the Runner tick."""
    import muyan_pilot

    calls = []
    monkeypatch.setattr(
        muyan_pilot.bootstrap_runner, "main",
        lambda argv: calls.append(argv) or 7,
    )
    monkeypatch.delenv("MUYAN_PILOT_CONFIG", raising=False)
    assert muyan_pilot.main([]) == 7
    assert calls == [["--config", "muyan-pilot.toml"]]


def test_bare_cli_real_call_reaches_the_runner_not_argparse(tmp_path):
    """Real call of the ExecStart shape (bare installed CLI): the
    failure must come from the RUNNER's fail-fast path (config
    loading), never from argparse rejecting the bare entry. Skipped
    when the CLI is not installed on this machine."""
    import shutil
    import subprocess

    cli = shutil.which("muyan-pilot")
    if cli is None:
        pytest.skip("muyan-pilot CLI is not installed on this machine")
    result = subprocess.run(
        [cli, "--config", str(tmp_path / "missing.toml")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0, (
        f"bare CLI with a missing config must fail fast, got rc=0 "
        f"stdout={result.stdout.strip()}"
    )
    assert "the following arguments are required: command" not in (
        result.stderr + result.stdout
    ), (
        "the bare CLI (the systemd ExecStart shape) was rejected by "
        f"argparse instead of starting the Runner: "
        f"{result.stderr.strip()}"
    )
    assert "FileNotFoundError" in result.stderr, (
        "the bare CLI must reach the Runner's config loading "
        f"(fail-fast on the missing config), got: {result.stderr.strip()}"
    )


# --- the compatibility path ----------------------------------------------------


def test_direct_execution_entry_stays():
    """Issue #140: `muyan_pilot.py` keeps its `__main__` entry as the
    development/compatibility path."""
    text = (REPO_ROOT / "muyan_pilot.py").read_text(encoding="utf-8")
    assert re.search(r'if __name__ == "__main__":', text)


def test_installed_cli_help_and_version_match_real_calls():
    """Issue #140 acceptance: in a clean environment the installed
    `muyan-pilot --help` and `muyan-pilot --version` succeed — asserted
    against real calls when the CLI is installed on this machine
    (skipped otherwise: the test suite must not require an install).
    The version output must carry the PEP 621 version."""
    import shutil
    import subprocess

    cli = shutil.which("muyan-pilot")
    if cli is None:
        pytest.skip("muyan-pilot CLI is not installed on this machine")
    help_result = subprocess.run(
        [cli, "--help"], capture_output=True, text=True, timeout=60,
    )
    assert help_result.returncode == 0, (
        f"muyan-pilot --help failed rc={help_result.returncode} "
        f"stderr={help_result.stderr.strip()}"
    )
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in help_result.stdout
    version_result = subprocess.run(
        [cli, "--version"], capture_output=True, text=True, timeout=60,
    )
    assert version_result.returncode == 0, (
        f"muyan-pilot --version failed rc={version_result.returncode} "
        f"stderr={version_result.stderr.strip()}"
    )
    assert load_pyproject()["project"]["version"] in version_result.stdout


def test_compat_entry_help_matches_one_real_call():
    """The compatibility entry is asserted against one real call
    (`python3 muyan_pilot.py --help`), not a guessed shape: it must
    still expose the same subcommands as the installed CLI."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "muyan_pilot.py"), "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"compat --help failed rc={result.returncode} "
        f"stderr={result.stderr.strip()}"
    )
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in result.stdout


# --- helper and skip-branch coverage -----------------------------------------


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


def test_real_call_tests_skip_without_the_cli(monkeypatch, tmp_path):
    """The real-call tests must skip (not fail) on a machine without
    the installed CLI — the suite must not require an install."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception):
        test_installed_cli_help_and_version_match_real_calls()
    with pytest.raises(pytest.skip.Exception):
        test_bare_cli_real_call_reaches_the_runner_not_argparse(tmp_path)


