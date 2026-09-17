"""CLI packaging contract (Issue #140).

The official usage is the installed `orbi` console script
(`orbi = orbi:main` in the PEP 621 `pyproject.toml`),
not a hand-written `python3 orbi.py`. These tests pin:

- the packaging file (console script, version, Python floor, the
  intentional zero runtime dependencies, and no hardcoded user
  directories/tokens in the release packaging);
- the systemd service template's CLI entry (ExecStart = the installed
  `orbi`, the uv-tool bin dir on the unit PATH, the unchanged
  WorkingDirectory and ExecStartPre);
- the CLI command strings the failure scenes carry (the unit_drift
  fix command, the HTTPS-remote migration entry);
- the documentation contract: the README quickstart, AGENTS.md and
  the EN/ZH docs use `orbi` and no longer require the user to
  hand-write `python3 orbi.py`;
- the direct-execution path: `python3 -m orbi.cli` runs the
  exact same code as the console script (development/compatibility,
  asserted against one real call).
"""
import importlib
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from orbi import delivery_labels
from orbi import git_transport
from orbi import pilot_setup
from orbi import scheduler, systemd_deploy

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVICE_FILE = REPO_ROOT / "systemd" / "orbi@.service"
README_FILE = REPO_ROOT / "README.md"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"

# The runtime package (Issue #168 src layout): the installed console
# script imports `orbi.cli`, and every runtime module lives in
# this package — setuptools discovers it automatically, no hand-
# maintained module list.
RUNTIME_PACKAGE = "src/orbi"
RUNTIME_MODULES = (
    "cli",
    "runner",
    "git_transport",
    "systemd_deploy",
    "pilot_setup",
    "pilot_slots",
    "pi_activity",
    "pi_recovery",
    "progress",
    # Issue #152: the CLI source consistency check (doctor/setup).
    "cli_source",
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


def test_delivery_label_consumers_follow_the_authoritative_source(monkeypatch):
    """Delivery consumers must read labels from delivery_labels, not literals."""
    monkeypatch.setattr(delivery_labels, "READY_LABEL", "changed-ready")
    monkeypatch.setattr(delivery_labels, "IN_PROGRESS_LABEL", "changed-progress")
    monkeypatch.setattr(delivery_labels, "PR_OPENED_LABEL", "changed-pr")
    monkeypatch.setattr(delivery_labels, "FIX_NEEDED_LABEL", "changed-fix")
    monkeypatch.setattr(delivery_labels, "MERGED_LABEL", "changed-merged")
    monkeypatch.setattr(delivery_labels, "BLOCKED_LABEL", "changed-blocked")

    cli = importlib.import_module("orbi.cli")
    importlib.reload(cli)
    importlib.reload(pilot_setup)

    assert cli.READY_LABEL == "changed-ready"
    assert cli.IN_PROGRESS_LABEL == "changed-progress"
    assert cli.RESULT_LABELS == (
        "changed-pr", "changed-fix", "changed-merged", "changed-blocked",
    )
    assert pilot_setup.REQUIRED_LABELS[:6] == (
        "changed-ready", "changed-progress", "changed-pr",
        "changed-fix", "changed-merged", "changed-blocked",
    )

    # Keep the imported modules usable by the remaining tests after the
    # monkeypatch is reverted.
    monkeypatch.undo()
    importlib.reload(cli)
    importlib.reload(pilot_setup)


def test_pyproject_declares_the_orbi_console_script():
    """Issue #140/#168: the console script is exactly
    `orbi = orbi.cli:main` (the src-layout package)."""
    data = load_pyproject()
    assert data["project"]["scripts"]["orbi"] == (
        "orbi.cli:main"
    )


def test_pyproject_project_metadata():
    """Issue #874: the PyPI distribution is the approved `orbi-cli`
    name; the console script below stays `orbi` (pinned separately)."""
    import orbi

    data = load_pyproject()
    project = data["project"]
    assert project["name"] == "orbi-cli"
    assert project["version"] == orbi.__version__
    # The package must not claim to run on an older minor version.
    assert project["requires-python"] == ">=3.14"
    # The bootstrap intentionally has no third-party runtime
    # dependency: the release package must not hardcode dependencies.
    assert project.get("dependencies", []) == []


def test_version_matches_the_packaging_metadata():
    """The `--version` output and the PEP 621 `version` must agree
    (one source of truth, no drift between the module and the
    packaging file)."""
    import orbi

    assert orbi.__version__ == load_pyproject()["project"]["version"]


def test_pyproject_builds_with_the_setuptools_backend():
    data = load_pyproject()
    assert "setuptools" in data["build-system"]["requires"][0]
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"


def test_pyproject_discovers_the_src_package():
    """Issue #168: setuptools discovers the runtime package from
    `src/` automatically — the hand-maintained `py-modules` list is
    gone (the #158 stale-finder root cause)."""
    data = load_pyproject()
    find = data["tool"]["setuptools"]["packages"]["find"]
    assert find["where"] == ["src"]
    assert "py-modules" not in data["tool"]["setuptools"], (
        "the flat-module py-modules list must stay removed (Issue #168)"
    )


def test_example_config_ships_inside_the_package():
    """Issue #163: the example config is packaged data (`package-data`
    declares exactly the one shipped file) so a PyPI install can create
    an orbi.toml without any checkout-adjacent file. Single source: the
    checkout-root copy is gone with the move into `src/orbi/`."""
    data = load_pyproject()
    package_data = data["tool"]["setuptools"].get("package-data")
    assert package_data is not None, "no package-data declared"
    assert package_data.get("orbi") == ["example_config.toml"]
    packaged = REPO_ROOT / "src" / "orbi" / "example_config.toml"
    assert packaged.is_file(), f"missing packaged example: {packaged}"
    assert not (REPO_ROOT / ".orbi.example.toml").exists(), (
        "the checkout-root example copy must stay removed (single source)"
    )


def test_required_python_matches_the_check_gate():
    """Issue #163: the runtime check gate (`orbi check`) and the PEP 621
    `requires-python` floor are the same requirement — pip enforces it at
    install time, the gate re-states it at runtime; the test pins the
    two together so they cannot drift. Issue #861: the interpreter
    selection (cli_source) shares the SAME floor — pilot_setup's
    constant is an alias, never a second copy."""
    from orbi import cli_source, pilot_setup

    assert load_pyproject()["project"]["requires-python"] == (
        ">=" + ".".join(str(part) for part in pilot_setup.REQUIRED_PYTHON)
    )
    assert pilot_setup.REQUIRED_PYTHON == cli_source.REQUIRED_PYTHON


def test_every_runtime_module_lives_in_the_package():
    """The installed console script imports the runtime package; every
    runtime module must exist under `src/orbi/` so the `uv
    tool` install is complete (no module left behind at the repo
    root)."""
    package_dir = REPO_ROOT / RUNTIME_PACKAGE
    assert package_dir.is_dir(), f"missing package dir: {package_dir}"
    for module in RUNTIME_MODULES:
        path = package_dir / f"{module}.py"
        assert path.is_file(), f"runtime module missing: {path}"


def test_packaging_files_hardcode_no_user_dirs_or_tokens():
    """Issue #140: no dependency, token or user directory is hardcoded
    into the release packaging (the config path and the user unit dir
    stay machine-local, provided by orbi.toml / the user's
    systemd dir)."""
    forbidden = (
        "/home/",
        "/Users/",
        "C:\\\\Users",
        "ghp_",
        "github_pat_",
        "ORBI_CONFIG",
        "ORBI_UNIT_DIR",
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
    """Issue #140: the service starts the installed `orbi` CLI
    (the uv-tool console script in the user bin dir, as an explicit
    deployable absolute entry), not a hand-written
    `python3 .../bootstrap_runner.py`."""
    service = parse_unit(SERVICE_FILE)
    assert service["Service"]["ExecStart"] == ["%h/.local/bin/orbi"]


def test_service_keeps_working_directory_and_preflight():
    """The unit's WorkingDirectory (the deployment checkout, where
    ExecStartPre syncs the engine source channel and the config lives)
    and the Issue #52 preflight are unchanged by the CLI switch."""
    service = parse_unit(SERVICE_FILE)
    section = service["Service"]
    # Issue #262: the deployment checkout path is NOT hardcoded; the
    # template carries the {{ORBI_REPO_DIR}} placeholder, substituted
    # with the checkout's resolved path at install time.
    assert section["WorkingDirectory"] == [
        "{{ORBI_REPO_DIR}}",
    ]
    # Issue #535: the SECOND preflight step syncs the deploy home to the
    # configured engine source channel (the first step heals the CLI
    # the sync runs through).
    pre = section["ExecStartPre"][1]
    assert pre.startswith("/usr/bin/timeout 90s /usr/bin/flock ")
    assert "{{ORBI_REPO_DIR}}/.orbi/base-sync.lock" in pre
    assert "%h/.local/bin/orbi sync-engine-source" in pre


def test_service_preflight_self_heals_the_editable_cli():
    """Issue #248: the #158 in-Runner refresh is unreachable when the
    console script cannot even `import orbi` (the src-layout
    migration of #168 left the installed editable finder stale, so the
    Runner died at the import stage before the refresh could run). The
    fix is an ExecStartPre line that self-heals OUTSIDE the Runner: it
    probes the installed CLI (`orbi --version` succeeds iff the package
    imports) and, on probe failure, runs the exact editable
    force-reinstall (cli_source.reinstall_command) under the SAME
    base-sync flock the engine-source sync uses. Issue #535: this step
    runs FIRST — the sync subcommand needs a working CLI."""
    service = parse_unit(SERVICE_FILE)
    pre = service["Service"]["ExecStartPre"]
    # Two preflight steps: the CLI self-heal, then the engine-source sync.
    assert len(pre) == 2
    heal = pre[0]
    # The same timeout wrapper + shared lock as the git sync step.
    assert heal.startswith("/usr/bin/timeout 300s /usr/bin/flock ")
    assert (
        "{{ORBI_REPO_DIR}}/.orbi/base-sync.lock"
        in heal
    )
    # The probe: the installed console script's `--version` succeeds iff
    # the package imports; its stdout/stderr are discarded.
    assert "%h/.local/bin/orbi --version >/dev/null 2>&1" in heal
    # The `||` fallback fires ONLY when the probe fails.
    assert " || " in heal
    # The fallback is the exact editable force-reinstall with the
    # shell-level interpreter variable (Issue #861: the conditional
    # selection rule — system `python3` when it satisfies the floor,
    # otherwise `3.14` so uv provisions it). The systemd `$$` escape
    # (systemd.service(5)) passes a literal `$` to the shell, which
    # expands `$I` AFTER the probe picked the branch.
    from orbi import cli_source

    # The systemd `%h` specifier is expanded by systemd before the
    # command runs, so the template carries the specifier, not the
    # expanded home dir — assert the reinstall argv minus the path.
    heal_argv = heal.split("'")[1]
    reinstall_part = heal_argv.split("||", 1)[1].strip()
    assert reinstall_part == (
        "uv tool install --force --reinstall --editable "
        "--python $$I {{ORBI_REPO_DIR}}"
    )
    # The Python-side source of truth resolves the SAME two shapes:
    # reinstall_args embeds the selection result where the template
    # embeds `$I` (the shell-behavior test below proves the rule's
    # semantics through the real shell).
    py_args = cli_source.reinstall_args(Path("{{ORBI_REPO_DIR}}"))
    assert " ".join(py_args) == (
        "uv tool install --force --reinstall --editable "
        f"--python {cli_source.PYTHON_INTERPRETER} {{{{ORBI_REPO_DIR}}}}"
    )
    # The interpreter probe IS cli_source's probe code (built from
    # REQUIRED_PYTHON — the floor pinned to pyproject's
    # requires-python), so the template and `orbi setup` can never
    # disagree about "compatible".
    assert f'python3 -c "{cli_source.PYTHON_PROBE_CODE}"' in heal_argv
    assert "then I=python3; else I=3.14; fi" in heal_argv


def test_service_self_heal_selects_the_interpreter_like_setup(
    tmp_path_factory,
):
    """Issue #861 acceptance: the self-heal payload applies the SAME
    conditional rule as `orbi setup`, proven through the REAL shell
    with fake interpreters. The systemd expansions are applied the way
    systemd applies them (documented in systemd.service(5)): `$$` is a
    literal `$`, `%h` is the home dir, `{{ORBI_REPO_DIR}}` was already
    substituted at install time."""
    import subprocess

    service = parse_unit(SERVICE_FILE)
    heal = service["Service"]["ExecStartPre"][0]
    raw_payload = heal.split("'")[1]

    def run_world(name: str, python3_body: str | None, interpreter: str):
        tmp_path = tmp_path_factory.mktemp(name)
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        (home / ".local" / "bin").mkdir(parents=True)
        shim = tmp_path / "shim"
        shim.mkdir()
        # The installed console script probe: exit 1 forces the
        # reinstall branch.
        probe = home / ".local" / "bin" / "orbi"
        probe.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)
        # The fake uv records the arguments it was called with.
        args_file = tmp_path / "uv_args.txt"
        uv = shim / "uv"
        uv.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$UV_ARGS_FILE\"\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        if python3_body is not None:
            py = shim / "python3"
            py.write_text(f"#!/bin/sh\n{python3_body}\n", encoding="utf-8")
            py.chmod(0o755)
        payload = (
            raw_payload
            .replace("$$", "$")
            .replace("%h", str(home))
            .replace("{{ORBI_REPO_DIR}}", str(repo))
        )
        subprocess.run(
            ["/bin/sh", "-c", payload],
            env={
                "PATH": str(shim),
                "UV_ARGS_FILE": str(args_file),
            },
            timeout=30, check=True, capture_output=True,
        )
        assert args_file.exists(), "the reinstall branch never ran"
        assert args_file.read_text(encoding="utf-8").splitlines() == [
            "tool", "install", "--force", "--reinstall", "--editable",
            "--python", interpreter, str(repo),
        ]

    # Compatible system python3 -> used DIRECTLY (Fedora 43 / Arch).
    run_world("heal-py314", "exit 0", "python3")
    # System python3 below the floor (Ubuntu 24.04: 3.12) -> uv
    # provisions 3.14.
    run_world("heal-py312", "exit 1", "3.14")
    # No system python3 at all (fresh uv-only host) -> 3.14.
    run_world("heal-no-py", None, "3.14")


def test_service_path_carries_the_uv_tool_bin_dir():
    """The installed console script lives in the uv tool bin dir
    (`~/.local/bin`); the unit PATH must carry it so the Runner's
    child processes (gh, pi, git) resolve like on the interactive
    shell."""
    service = parse_unit(SERVICE_FILE)
    path_value = service["Service"]["Environment"][0]
    assert "%h/.local/bin" in path_value


# `systemd-analyze` is a Linux tool: a host with no systemd (the hosted
# macOS runner) cannot run this check at all — there the skip IS the
# honest boundary, and the Ubuntu CI keeps the check authoritative. On
# a Linux host the check always runs: a missing executable is exactly
# the failure it must catch, never skippable noise.
@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze needs systemd (Linux); this host has none",
)
def test_service_template_passes_systemd_analyze_verify():
    """Issue #140 acceptance: the service template starts the Runner
    via the CLI entry — verified with the REAL `systemd-analyze
    --user verify` (the user manager context, where the `%h` specifier
    and the installed CLI resolve). The declared runtimes both put the
    installed CLI at the unit's ExecStart path: the production machine
    via `uv tool install` and CI via the workflow's
    `pip install --prefix $HOME/.local .` (both land the executable at
    `~/.local/bin/orbi`), so `systemd-analyze --user verify`
    resolves the unit's absolute ExecStart against a real executable
    on both."""
    import subprocess
    import tempfile

    # Issue #262: the template carries the {{ORBI_REPO_DIR}} placeholder,
    # so it is rendered with a real checkout path before verify — the raw
    # template is a machine-independent source, not a runnable unit.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".service", delete=False,
    ) as handle:
        handle.write(systemd_deploy.render_unit_template(
            SERVICE_FILE.read_text(encoding="utf-8"), REPO_ROOT,
        ))
        rendered_path = handle.name
    try:
        result = subprocess.run(
            ["systemd-analyze", "--user", "verify", rendered_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        Path(rendered_path).unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"systemd-analyze verify failed rc={result.returncode} "
        f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
    )


# --- the CLI command strings --------------------------------------------------


def test_unit_drift_fix_command_is_the_cli():
    assert scheduler.FIX_COMMAND == "orbi install-units"


def test_https_remote_migration_entry_is_the_cli():
    assert git_transport.MIGRATION_ENTRY == "orbi setup"


def test_setup_requires_the_installed_cli():
    """`setup` verifies the machine prerequisites; the installed CLI is
    one of them (the systemd entry it documents must exist)."""
    assert "orbi" in pilot_setup.REQUIRED_COMMANDS
    for name in ("git", "gh"):
        assert name in pilot_setup.REQUIRED_COMMANDS
    # Issue #156: setup calls `uv tool install` (the CLI editable step,
    # Issue #152), so the prerequisite check must verify uv explicitly.
    assert "uv" in pilot_setup.REQUIRED_COMMANDS


# --- the documentation contract -----------------------------------------------


def test_readme_uses_the_cli_and_the_editable_uv_tool_install():
    """Issue #241: the README homepage quickstart uses the installed
    CLI (setup + doctor) and the EDITABLE uv tool install (Issue #152:
    the tool env imports the source from the deployment checkout, so
    ordinary source/template changes need no reinstall); the full
    command set (add, install-units, session, ...) is documented in
    the docs pages."""
    readme = README_FILE.read_text(encoding="utf-8")
    # The quickstart commands are the installed CLI.
    assert "orbi setup" in readme
    assert "orbi doctor" in readme
    assert "orbi add" in readme
    # Issue #152: the official local deployment is the EDITABLE uv
    # tool install.
    assert "uv tool install" in readme
    assert "--editable" in readme
    assert "uv tool install --force --reinstall --editable" in readme
    # The user is no longer required to hand-write the Python entry.
    assert "python3 orbi.py" not in readme
    # Issue #168: the runtime package lives in `src/orbi/` and
    # the checkout root carries no `orbi.py` that could shadow
    # the installed package.
    assert "src/orbi" in readme


def test_docs_document_the_full_cli_command_set():
    """Issue #241: the full CLI command set (add, status, session,
    install-units, doctor, check, setup) is documented in the docs
    pages — the README homepage keeps the quickstart plus a one-sentence
    summary. `orbi check` is the Issue #163 prerequisite gate."""
    operations = (REPO_ROOT / "docs" / "operations.mdx").read_text(encoding="utf-8")
    for command in ("orbi add", "orbi status",
                    "orbi session", "orbi install-units",
                    "orbi doctor", "orbi check", "orbi setup"):
        assert command in operations, (
            f"docs/operations.mdx must document the {command} command"
        )


def test_docs_make_the_editable_install_the_official_deployment():
    """Issue #152: README, AGENTS and the EN/ZH docs make the
    editable uv tool install the official local deployment and never
    require `uv tool upgrade` for ordinary Python source or systemd
    template/migration changes (with the editable install the
    ExecStartPre checkout sync is picked up automatically — the
    upgrade command is gone from the documented flow). The exact
    force-reinstall command is documented as the fix for a
    non-editable/stale CLI source."""
    pages = {
        "AGENTS.md": AGENTS_FILE,
    }
    for slug in DOC_PAGES:
        pages[slug] = REPO_ROOT / "docs" / slug
    for name, path in pages.items():
        text = path.read_text(encoding="utf-8")
        assert "uv tool upgrade" not in text, (
            f"{name} still requires `uv tool upgrade`: with the "
            "editable install ordinary source/template changes need "
            "no reinstall"
        )
    # The install/upgrade pages (EN + ZH) name the editable install.
    for slug in ("getting-started.mdx", "zh/getting-started.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "--editable" in page, (
            f"docs/{slug} must document the editable uv tool install"
        )


def test_agents_md_uses_the_cli():
    text = AGENTS_FILE.read_text(encoding="utf-8")
    assert "orbi install-units" in text
    assert "orbi doctor" in text
    assert "orbi setup" in text
    assert "python3 orbi.py" not in text


def test_docs_use_the_cli_and_never_require_the_python_entry():
    for slug in DOC_PAGES:
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "python3 orbi.py" not in page, (
            f"docs/{slug} still requires the hand-written Python entry"
        )


def test_docs_getting_started_documents_the_cli_install():
    """A new user must find the `uv tool install` command and the
    `orbi` CLI in the getting-started pages (EN + ZH)."""
    for slug in ("getting-started.mdx", "zh/getting-started.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "uv tool install" in page, (
            f"docs/{slug} must document the uv tool install"
        )
        assert "orbi" in page, (
            f"docs/{slug} must name the installed CLI"
        )


# Issue #874: the PyPI distribution is `orbi-cli`; every user-facing
# install/uninstall path must name THAT distribution and never the old
# bare `orbi` name (which would fail with PackageNotFoundError).
DIST_INSTALL_PAGES = (
    "README.md",
    "README.zh-CN.md",
    "docs/getting-started.mdx",
    "docs/zh/getting-started.mdx",
)

OLD_DIST_NAME_RE = re.compile(
    r"(pip install|uv tool install|uv tool uninstall)( --reinstall| --upgrade)?"
    r" orbi(?![-\w])"
)


def test_docs_install_commands_use_the_orbi_cli_distribution():
    """Issue #874: the user-facing install/uninstall commands carry the
    distribution name `orbi-cli` (the uv TOOL name follows the
    distribution — verified with a real `uv tool install` probe whose
    dist and script names differ). The console script stays `orbi`,
    so the documented `orbi --version` / `orbi --help` never change."""
    for slug in DIST_INSTALL_PAGES:
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        assert "orbi-cli" in text, (
            f"{slug} must carry the orbi-cli distribution name (Issue #874)"
        )
        stale = OLD_DIST_NAME_RE.findall(text)
        assert not stale, (
            f"{slug} still installs/uninstalls the old distribution "
            f"name `orbi`: {stale}"
        )
    for slug in ("docs/operations.mdx", "docs/zh/operations.mdx"):
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        assert "uv tool uninstall orbi-cli" in text, (
            f"{slug} must uninstall the tool by its distribution "
            "name orbi-cli (Issue #874)"
        )
        stale = OLD_DIST_NAME_RE.findall(text)
        assert not stale, (
            f"{slug} still uninstalls the old distribution name "
            f"`orbi`: {stale}"
        )


# Issue #892: orbi-cli 0.5.6 IS published on PyPI (verified live on the
# package page and by installing the exact release in isolated
# environments). The authoritative CURRENT docs — the two READMEs
# (README.md is also the PyPI project description, `readme =
# "README.md"` in pyproject.toml) and the two getting-started pages —
# must state that truth and the real commands; the historical release
# notes (release-v0.5.5.mdx keeps the #852 failure record) are
# deliberately NOT scanned: they may keep historically accurate claims.
PYPI_AVAILABILITY_PAGES = (
    "README.md",
    "README.zh-CN.md",
    "docs/getting-started.mdx",
    "docs/zh/getting-started.mdx",
)

STALE_AVAILABILITY_RES = (
    # EN availability claims (the pre-0.5.6 state).
    re.compile(r"not on PyPI", re.IGNORECASE),
    re.compile(r"not published", re.IGNORECASE),
    re.compile(r"currently fail", re.IGNORECASE),
    # EN "real commands only after publication" promise.
    re.compile(r"only after a release is actually published", re.IGNORECASE),
    # ZH availability claims (还没上 PyPI / 尚未发布到 PyPI).
    re.compile(r"还没上\s*PyPI"),
    re.compile(r"尚未发布到\s*PyPI"),
    # ZH "commands are only added after the real publication" promise.
    re.compile(r"真正发布之后[，,]?这一节才会补上"),
)

PYPI_SECTION_RE = re.compile(
    r"^## PyPI (?:installation|安装)\s*?\n(.*?)(?=^## )",
    re.MULTILINE | re.DOTALL,
)


def test_current_docs_do_not_carry_stale_pypi_availability_claims():
    """Issue #892: no authoritative current page (including the
    README the PyPI project page renders as its description) may say
    orbi-cli is unavailable — the live package page and the isolated
    install of the exact release are the acceptance evidence."""
    for slug in PYPI_AVAILABILITY_PAGES:
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        for pattern in STALE_AVAILABILITY_RES:
            match = pattern.search(text)
            assert not match, (
                f"{slug} carries the stale availability claim "
                f"{match.group(0)!r} — orbi-cli IS published on PyPI "
                "(https://pypi.org/project/orbi-cli/); update the "
                "current docs, never re-ship a false PyPI description"
            )


def test_pypi_installation_sections_carry_the_real_commands():
    """Issue #892: the getting-started PyPI section documents the
    verified reality — both install commands (`pip install orbi-cli`
    inside a Python >= 3.14 environment, `uv tool install orbi-cli`
    for the isolated tool install), the `orbi --version` check whose
    output prints `orbi <version>`, the `uv tool uninstall orbi-cli`
    removal, and the >= 3.14 floor with its repair. The uv resolver
    failure message is pinned version-free (Issue #909: the docs
    never quote a release number); the `orbi-cli==<version> ...`
    shape is what uv prints for ANY released version."""
    for slug in ("docs/getting-started.mdx", "docs/zh/getting-started.mdx"):
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        section = PYPI_SECTION_RE.search(text)
        assert section is not None, f"{slug} lost its PyPI section"
        body = section.group(1)
        for needle in (
            "https://pypi.org/project/orbi-cli/",
            "pip install orbi-cli",
            "uv tool install orbi-cli",
            "orbi --version",
            "uv tool uninstall orbi-cli",
            "depends on Python>=3.14",
            "No matching distribution found for orbi-cli",
            "3.14",
        ):
            assert needle in body, (
                f"{slug} PyPI section must document {needle!r} "
                "(Issue #892: the verified real commands and floor)"
            )


# Issue #909: the release number is an OUTPUT (the user reads it with
# `orbi --version`), not a docs claim — a hardcoded literal turned every
# release commit red until a human edited four files (Issue #903). The
# guard matches only the orbi-release shapes, never the legitimate
# three-part numbers these pages keep (gh 2.94.0, Ubuntu's 2.45.0
# package, 127.0.0.1): the availability claims, the `orbi --version`
# output example, and the uv resolver message example.
RELEASE_CLAIM_RE = re.compile(
    r"(?:release|当前版本|版本)\s+\d+\.\d+\.\d+"
    r"|→\s*orbi\s+\d+\.\d+\.\d+"
    r"|orbi-cli==\d+\.\d+\.\d+"
)


def test_docs_carry_no_hardcoded_release_version():
    """Issue #909: none of the four authoritative pages quotes an orbi
    release number in any shape (`release X.Y.Z` / `当前版本 X.Y.Z`,
    `→ orbi X.Y.Z`, `orbi-cli==X.Y.Z`). Reintroducing one fails here —
    write `orbi <version>` / `orbi-cli==<version>` instead.
    `requires Python ≥ 3.14` stays: it is a user INPUT that decides
    which command to copy, pinned to the packaging floor elsewhere."""
    for slug in PYPI_AVAILABILITY_PAGES:
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        match = RELEASE_CLAIM_RE.search(text)
        assert not match, (
            f"{slug} hardcodes the release version {match.group(0)!r} "
            "(Issue #909): the release number is read with "
            "`orbi --version`, never quoted in the docs"
        )


def test_readme_is_the_packaged_pypi_description():
    """Issue #892: the PyPI project description is the README (the
    published page renders it), so the description source must be
    pinned and truthful — a stale availability claim in README.md is
    a stale claim ON PyPI."""
    data = load_pyproject()
    assert data["project"]["readme"] == "README.md"


def test_install_docs_state_the_conditional_interpreter_rule():
    """Issue #861: `requires-python >= 3.14` is a compatibility floor,
    not a demand to replace every distro Python. No live page may
    present `--python /usr/bin/python3` as universally valid (Ubuntu
    24.04 ships Python 3.12 and the hard pin fails there), and the
    install pages state BOTH branches of the conditional rule: the
    system `python3` when it satisfies the floor (Fedora 43, current
    Arch), `3.14` (provisioned by uv) otherwise."""
    without_pin = [
        "docs/setup.mdx", "docs/zh/setup.mdx",
        "docs/getting-started.mdx", "docs/zh/getting-started.mdx",
        "docs/operations.mdx", "docs/zh/operations.mdx",
        "README.md", "README.zh-CN.md",
    ]
    for slug in without_pin:
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        assert "--python /usr/bin/python3" not in text, (
            f"{slug} still presents the universal /usr/bin/python3 pin "
            "(fails on Ubuntu 24.04)"
        )
    with_both_branches = [
        "docs/setup.mdx", "docs/zh/setup.mdx",
        "docs/getting-started.mdx", "docs/zh/getting-started.mdx",
        "README.md", "README.zh-CN.md",
    ]
    for slug in with_both_branches:
        text = (REPO_ROOT / slug).read_text(encoding="utf-8")
        assert "--python python3" in text, (
            f"{slug} must state the system-python3 branch of the "
            "interpreter rule (Issue #861)"
        )
        assert "--python 3.14" in text, (
            f"{slug} must state the uv-provisioned 3.14 branch of the "
            "interpreter rule (Issue #861)"
        )


def test_docs_operations_name_the_cli():
    for slug in ("operations.mdx", "zh/operations.mdx"):
        page = (REPO_ROOT / "docs" / slug).read_text(encoding="utf-8")
        assert "orbi" in page, f"docs/{slug} must name the CLI"
        for command in ("status", "session", "add"):
            assert command in page, (
                f"docs/{slug} must document the {command} command"
            )


# --- the bare entry IS the Runner (the systemd ExecStart) ----------------------


def test_bare_cli_runs_the_runner_tick(monkeypatch, tmp_path):
    """Issue #140 acceptance: `systemd 使用 CLI 入口可启动 Runner`.

    The service's `ExecStart` is the bare installed CLI (no
    subcommand), so `orbi` with NO arguments must run one
    Runner tick — exactly like the legacy `python3
    bootstrap_runner.py` — and must NOT die with the argparse error
    `the following arguments are required: command` (the pre-fix
    failure mode that made every timer tick exit 2 without ever
    starting the Runner)."""
    import orbi.cli as orbi

    calls = []

    def fake_runner_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(
        orbi.runner, "main", fake_runner_main,
    )
    config = tmp_path / "orbi.toml"
    assert orbi.main(["--config", str(config)]) == 0
    assert calls == [["--config", str(config)]], (
        "the bare CLI must delegate to the Runner's main with the "
        f"same --config, got: {calls!r}"
    )


def test_bare_cli_default_config_delegates_to_the_runner(monkeypatch):
    """`orbi` with no arguments at all uses the default config
    path (ORBI_CONFIG / orbi.toml) and still delegates
    to the Runner tick."""
    import orbi.cli as orbi

    calls = []
    monkeypatch.setattr(
        orbi.runner, "main",
        lambda argv: calls.append(argv) or 7,
    )
    monkeypatch.delenv("ORBI_CONFIG", raising=False)
    assert orbi.main([]) == 7
    assert calls == [["--config", "orbi.toml"]]


def test_bare_cli_real_call_reaches_the_runner_not_argparse(tmp_path):
    """Real call of the ExecStart shape (bare installed CLI): the
    failure must come from the RUNNER's fail-fast path (config
    loading), never from argparse rejecting the bare entry. Skipped
    when the CLI is not installed on this machine."""
    import shutil
    import subprocess

    cli = shutil.which("orbi")
    if cli is None:
        pytest.skip("orbi CLI is not installed on this machine")
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
    assert "config_not_found" in result.stderr, (
        "the bare CLI must report the actionable missing-config error, "
        f"got: {result.stderr.strip()}"
    )


# --- the compatibility path ----------------------------------------------------


def test_direct_execution_entry_stays():
    """Issue #140/#168: the direct-execution compatibility entry is
    `python3 -m orbi.cli` — the exact same code the console
    script runs (the src-layout package, no fallback copy at the
    checkout root that could shadow the installed package)."""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "orbi.cli", "--help"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"-m orbi.cli --help failed rc={result.returncode} "
        f"stderr={result.stderr.strip()}"
    )
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in result.stdout


def test_installed_cli_help_and_version_match_real_calls():
    """Issue #140 acceptance: in a clean environment the installed
    `orbi --help` and `orbi --version` succeed — asserted
    against real calls when the CLI is installed on this machine
    (skipped otherwise: the test suite must not require an install).
    The version output must carry the PEP 621 version."""
    import shutil
    import subprocess

    cli = shutil.which("orbi")
    if cli is None:
        pytest.skip("orbi CLI is not installed on this machine")
    help_result = subprocess.run(
        [cli, "--help"], capture_output=True, text=True, timeout=60,
    )
    assert help_result.returncode == 0, (
        f"orbi --help failed rc={help_result.returncode} "
        f"stderr={help_result.stderr.strip()}"
    )
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in help_result.stdout
    version_result = subprocess.run(
        [cli, "--version"], capture_output=True, text=True, timeout=60,
    )
    assert version_result.returncode == 0, (
        f"orbi --version failed rc={version_result.returncode} "
        f"stderr={version_result.stderr.strip()}"
    )
    assert load_pyproject()["project"]["version"] in version_result.stdout


def test_compat_entry_help_matches_one_real_call():
    """The compatibility entry is asserted against one real call
    (`python3 -m orbi.cli --help`), not a guessed shape: it must
    expose the same subcommands as the installed CLI."""
    import subprocess
    import sys

    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "orbi.cli", "--help"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
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


