"""Regression tests for the macOS compatibility workflow (Issue #894).

The Ubuntu workflow (`.github/workflows/ci.yml`) is the repository's
coverage/release contract; it runs on Linux only. The macOS compatibility
surface — the installer's Darwin branch (Issues #849/#868), the launchd
adapter, the wheel smoke, the public CLI test path — had no hosted check,
so a macOS regression reached `main` unnoticed.

`.github/workflows/ci-macos.yml` adds that check as a SEPARATE serial
workflow (never a matrix on ci.yml): one job on the pinned `macos-15`
image, the full test suite under a bounded `timeout` with the real exit
code, the package build + clean-venv wheel smoke, and installer
syntax/entry checks that exercise the Darwin branch without performing a
real installation. The hosted boundary is deliberate and documented in
docs/testing.mdx: hosted CI verifies the macOS code paths and the
runner-level launchd prerequisite — it cannot prove a real user's
logged-in GUI session, credentials, model provider, or the full
Issue-to-merge journey, and it never replaces a real-Mac launchd smoke.

These tests fail when the workflow is missing, drops a trigger, moves to
`macos-latest`, loses the timeout bound or a probe, swallows a failure,
or mutates the runner's own launchd deployment.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci-macos.yml"
LINUX_WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INSTALLER = REPO_ROOT / "install.sh"
PLIST_TEMPLATE = REPO_ROOT / "launchd" / "org.orbi.runner.plist"

RUNNER_LABEL = "macos-15"
SUITE_COMMAND = "timeout 3600 python3 -m pytest tests/ -q"


def load_workflow() -> dict:
    """Parse the workflow YAML; fail fast when it is missing or invalid."""
    assert WORKFLOW_FILE.is_file(), f"missing macOS workflow: {WORKFLOW_FILE}"
    workflow = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "workflow file is not a YAML mapping"
    return workflow


def on_section(workflow: dict) -> dict:
    """The `on:` key (YAML 1.1 parses the bare word `on` as boolean True)."""
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict), "workflow has no `on:` trigger section"
    return section


def steps_of(workflow: dict) -> list[dict]:
    """All steps of the (single) job, flattened."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "workflow has no jobs"
    flattened: list[dict] = []
    for job in jobs.values():
        assert isinstance(job, dict) and job.get("steps"), f"job without steps: {job!r}"
        flattened.extend(job["steps"])
    return flattened


def step_commands(steps: list[dict]) -> list[str]:
    """The shell commands of all steps (setup steps have no `run:`)."""
    return [
        str(step.get("run", "")).strip()
        for step in steps
        if step.get("run")
    ]


def commands_of(workflow: dict) -> list[str]:
    return step_commands(steps_of(workflow))


def test_macos_workflow_exists_as_a_separate_file():
    """Issue #894: a separate macOS compatibility workflow — the single
    Ubuntu CI job must NOT become a matrix (test_linux_workflow_keeps_
    the_authoritative_gates pins ci.yml's shape)."""
    assert WORKFLOW_FILE.is_file(), f"missing macOS workflow: {WORKFLOW_FILE}"
    assert LINUX_WORKFLOW_FILE.is_file()


def test_macos_workflow_filters_pull_requests_but_keeps_main_push_and_dispatch():
    section = on_section(load_workflow())
    pull_request = section.get("pull_request")
    assert isinstance(pull_request, dict), "the macOS workflow must configure pull_request"
    assert pull_request.get("paths") == [
        "install.sh",
        "launchd/**",
        "pyproject.toml",
        "requirements.txt",
        "src/orbi/**",
        ".github/workflows/ci-macos.yml",
    ], f"unexpected macOS pull_request paths: {pull_request!r}"
    push = section.get("push")
    assert push is not None, "the macOS job must run on push to the protected branch"
    assert push == {"branches": ["main"]}, (
        f"push trigger must stay unfiltered and limited to main, got: {push!r}"
    )
    assert "workflow_dispatch" in section, (
        "the macOS job must be manually dispatchable (workflow_dispatch)"
    )


def test_single_serial_job_on_a_pinned_macos_runner():
    """One job, no strategy (serial — no matrix, no containers), and the
    runner label is an explicit pinned image, never the moving
    `macos-latest` (verified against actions/runner-images: `macos-15`
    arm64 is a stable non-deprecated label; `macos-14` is deprecated)."""
    workflow = load_workflow()
    jobs = workflow["jobs"]
    assert len(jobs) == 1, f"serial: exactly one job, got {len(jobs)}: {sorted(jobs)}"
    job = next(iter(jobs.values()))
    assert "strategy" not in job, "serial: no test matrix"
    assert "container" not in job, "no containers are needed for this workflow"
    assert job.get("runs-on") == RUNNER_LABEL, (
        f"the macOS job must pin {RUNNER_LABEL!r} explicitly, got: {job.get('runs-on')!r}"
    )
    assert "macos-latest" not in WORKFLOW_FILE.read_text(encoding="utf-8"), (
        "the moving macos-latest label must not appear anywhere in the workflow"
    )


def test_workflow_permissions_are_least_privilege():
    workflow = load_workflow()
    assert workflow.get("permissions") == {"contents": "read"}, (
        f"least privilege: contents: read only, got: {workflow.get('permissions')!r}"
    )


def test_checkout_fetches_full_history():
    """The repository test contract needs full history + all tags: the
    release reconciliation tests verify the real annotated tag object
    (same reason as the Ubuntu workflow, Issue #126)."""
    checkout = [
        step for step in steps_of(load_workflow())
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkout, "the macOS job must check out via actions/checkout"
    assert len(checkout) == 1, f"exactly one checkout step, got {len(checkout)}"
    assert str(checkout[0].get("with", {}).get("fetch-depth")) == "0", (
        "the macOS checkout must fetch full history and all tags (fetch-depth: 0), "
        f"got: {checkout[0].get('with')!r}"
    )


def test_python_3_14_is_pinned_like_production():
    setup = [
        step for step in steps_of(load_workflow())
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert setup, "the macOS job must install Python via actions/setup-python"
    assert len(setup) == 1, f"exactly one setup-python step, got {len(setup)}"
    assert str(setup[0].get("with", {}).get("python-version")) == "3.14", (
        f"the macOS job must pin the production minor version 3.14, "
        f"got: {setup[0].get('with')!r}"
    )


def test_timeout_shim_covers_the_missing_coreutils_timeout():
    """The hosted macOS image ships no GNU coreutils `timeout` (verified
    against actions/runner-images toolset-15.json — the exact limitation
    install.sh already handles, Issue #868), so the workflow provides a
    shim and publishes it via GITHUB_PATH: every later command keeps the
    documented `timeout <seconds> <cmd>` contract form. The shim is
    `perl alarm + exec` — perl ships with the image, the alarm survives
    exec, and the real exit code propagates (the bound kills with 142)."""
    shims = [
        command for command in commands_of(load_workflow())
        if "GITHUB_PATH" in command
    ]
    assert shims, (
        "the workflow must provide a timeout shim on PATH (the macOS image "
        f"ships no coreutils timeout), steps run: {commands_of(load_workflow())!r}"
    )
    assert any(
        "alarm shift; exec @ARGV" in command for command in shims
    ), f"the shim must bound commands with perl alarm + exec, got: {shims!r}"


def test_runner_probe_reports_macos_and_the_launchd_domain():
    """The real runner probe the Issue pins: macOS version (`sw_vers`),
    the Python version, `launchctl`, and the USER's launchd domain
    (`launchctl print gui/$(id -u)`). A missing domain must fail the
    step (no fallback — the no-swallow test pins that)."""
    probes = [
        command for command in commands_of(load_workflow())
        if "sw_vers" in command
    ]
    assert probes, (
        "the workflow must probe the real macOS runner (sw_vers), "
        f"steps run: {commands_of(load_workflow())!r}"
    )
    assert any(
        'launchctl print "gui/$(id -u)"' in command for command in probes
    ), (
        "the probe must verify the user's launchd GUI domain "
        f"(launchctl print gui/$(id -u)), got: {probes!r}"
    )
    assert any(
        "python3 --version" in command for command in probes
    ), f"the probe must report the Python version, got: {probes!r}"
    assert any(
        "launchctl" in command for command in probes
    ), f"the probe must exercise launchctl, got: {probes!r}"


def test_workflow_installs_its_own_test_requirements():
    commands = commands_of(load_workflow())
    assert any("-r requirements.txt" in command for command in commands), (
        "the workflow must install the declared requirements itself, "
        f"steps run: {commands!r}"
    )
    assert any(
        "pip install pytest coverage pyyaml" in command for command in commands
    ), (
        "the workflow must install its own test toolchain (pytest coverage "
        "pyyaml — the same toolchain as ci.yml; tests/test_coverage_gate.py "
        "imports coverage at module level), "
        f"steps run: {commands!r}"
    )


def test_full_suite_runs_bounded_with_the_real_exit_code():
    """The full suite, one bounded command, the real exit code: no pipe
    may sit between pytest and the step (Issue #180 — a pipeline exits
    with the LAST command's code, so `pytest | tail` disguises a red
    suite as green)."""
    suite = [
        command for command in commands_of(load_workflow())
        if "pytest tests/" in command
    ]
    assert any(SUITE_COMMAND in command for command in suite), (
        f"the macOS job must run the full suite as {SUITE_COMMAND!r}, "
        f"steps run: {commands_of(load_workflow())!r}"
    )
    for command in suite:
        assert "|" not in command, (
            "the suite command must keep the real pytest exit code — no "
            f"pipes (Issue #180), got: {command!r}"
        )


def test_package_build_and_clean_venv_wheel_smoke():
    """Issue #163 contract on macOS: build the package, install the wheel
    into a clean venv, verify `orbi --version` / `orbi --help` and that
    the import source is site-packages (the drift branch exits 1)."""
    commands = commands_of(load_workflow())
    assert any(
        re.search(r"python3 -m build\b", command) for command in commands
    ), f"the macOS job must build the package, steps run: {commands!r}"
    assert any("dist/*.whl" in command for command in commands), (
        f"the macOS job must install the built WHEEL, steps run: {commands!r}"
    )
    assert any("python3 -m venv" in command for command in commands), (
        "the wheel smoke runs in a clean venv, not the workflow environment"
    )
    entry = [command for command in commands if "orbi --help" in command]
    assert entry, f"the wheel smoke must check the entry, steps run: {commands!r}"
    assert any("orbi --version" in command for command in entry), (
        "the wheel smoke must also check orbi --version"
    )
    source_checks = [command for command in commands if "site-packages" in command]
    assert source_checks, (
        "the wheel smoke must verify the import source is site-packages, "
        f"steps run: {commands!r}"
    )
    assert any("exit 1" in command for command in source_checks), (
        "a wheel importing outside site-packages must fail the step (exit 1)"
    )


def test_wheel_smoke_probe_runs_from_a_neutral_cwd():
    """`python -c` puts the cwd first on sys.path: run from the checkout,
    the checkout's own package shadows the venv's import and the
    site-packages check would be a no-op. The probe must `cd /` first
    (the same neutral-cwd rule as the Ubuntu workflow)."""
    probes = [
        command for command in commands_of(load_workflow())
        if "orbi.__file__" in command
    ]
    assert probes, (
        "the wheel smoke must probe the import source (orbi.__file__), "
        f"steps run: {commands_of(load_workflow())!r}"
    )
    for probe in probes:
        assert "cd / &&" in probe, (
            f"the import-source probe must run from a neutral cwd (cd /), "
            f"got: {probe!r}"
        )


def test_installer_checks_cover_syntax_and_the_darwin_entry():
    """The installer checks exercise the Darwin branch WITHOUT a real
    installation: `bash -n` syntax, `plutil -lint` on the launchd plist
    template, and a PATH-stubbed entry run in a temp ORBI_HOME where the
    real macOS `uname`/`launchctl` pass the scheduler gate and a stubbed
    `git` stops the script at the clone step (marker file proves the
    entry walked the whole Darwin path)."""
    commands = commands_of(load_workflow())
    assert any("bash -n install.sh" in command for command in commands), (
        f"the workflow must syntax-check install.sh, steps run: {commands!r}"
    )
    assert any(
        "plutil -lint launchd/org.orbi.runner.plist" in command for command in commands
    ), (
        "the workflow must lint the launchd plist template with plutil "
        f"(a real Darwin check), steps run: {commands!r}"
    )
    sandbox = [
        command for command in commands
        if "ORBI_HOME" in command and "install.sh" in command
    ]
    assert sandbox, (
        "the workflow must run the installer entry in a sandboxed "
        f"ORBI_HOME, steps run: {commands!r}"
    )
    for command in sandbox:
        assert "timeout" in command, (
            f"the sandboxed installer run must be bounded with timeout, got: {command!r}"
        )
        assert "git-reached" in command, (
            "the sandbox run must prove the installer reaches the clone "
            f"step (the marker after the launchctl gate), got: {command!r}"
        )
    assert INSTALLER.is_file() and PLIST_TEMPLATE.is_file()


def test_workflow_never_mutates_the_runner_launchd_deployment():
    """The hosted boundary: the workflow probes `launchctl print` (read)
    but must never enable/bootstrap/bootout anything — no step may
    install a real agent into the runner's own LaunchAgents domain."""
    for command in commands_of(load_workflow()):
        assert not re.search(
            r"launchctl (enable|disable|bootstrap|bootout|load|unload|kickstart)",
            command,
        ), f"the workflow must not mutate the runner's launchd deployment: {command!r}"


def test_no_step_swallows_a_failure():
    """Failure path (Issue #894): a failing test, package smoke, installer
    check, probe or unsupported command must surface as a failed check —
    no step may convert a failure into a pass."""
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "|| echo" not in text


def test_linux_workflow_keeps_the_authoritative_gates():
    """Scope guard: the macOS job adds compatibility coverage; the Ubuntu
    workflow stays the single-job coverage/release contract (its own test
    file pins the full shape — this pins the two gates and the runner)."""
    workflow = yaml.safe_load(LINUX_WORKFLOW_FILE.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    assert job.get("runs-on") == "ubuntu-latest"
    commands = step_commands(job["steps"])
    joined = "\n".join(commands)
    assert "tools/coverage_gate.py" in joined, "the global coverage gate must stay on Linux CI"
    assert "tools/diff_coverage_gate.py origin/main" in joined, (
        "the changed-code coverage gate must stay on Linux CI"
    )


def test_docs_document_the_macos_boundary():
    """The honest boundary is documented in the testing page (EN and the
    Chinese mirror): the hosted macOS job verifies the macOS code paths
    and the runner-level launchd prerequisite — not a real user's GUI
    session, credentials, provider, or the full Issue-to-merge journey,
    and it never replaces a real-Mac launchd smoke."""
    en = (REPO_ROOT / "docs" / "testing.mdx").read_text(encoding="utf-8")
    zh = (REPO_ROOT / "docs" / "zh" / "testing.mdx").read_text(encoding="utf-8")
    for text in (en, zh):
        assert "ci-macos.yml" in text, "testing must name the macOS workflow file"
        assert RUNNER_LABEL in text, "testing must document the pinned runner image"
        assert "gui/$(id -u)" in text, "testing must document the launchd domain probe"
        assert "Issue #868" in text, "testing must link the no-coreutils-timeout fact"
    assert "launchd smoke" in en, "the EN page must state the real-Mac smoke boundary"
    assert "launchd" in zh and "macos-15" in zh, (
        "the Chinese page must carry the same macOS facts"
    )
