"""Regression tests for the GitHub Actions CI workflow (Issue #56).

The repository contract (AGENTS.md) says the full pytest suite must run
with the tiered coverage gate (Issue #234: whole repository line >= 95%
and branch >= 95% checked separately, changed Python code at 100%
line/branch). Until now that only happened on the local Runner machine.
The workflow in `.github/workflows/ci.yml` runs the same contract
remotely on every `pull_request` and `push` to `main`, so the gate is
visible on GitHub.

These tests fail when the workflow file is missing, when it stops
enforcing the contract (triggers, pinned Python, requirements install,
contract test commands, tiered coverage gate), or when it drifts into
the extras the Issue explicitly forbids (lint, matrix, cache). The
docs (docs/testing.mdx — the README homepage keeps only the summary
plus the link, Issue #241) must keep documenting what the remote CI is
and when it runs.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The contract commands (AGENTS.md) run through the pinned interpreter on
# PATH in CI; the local machine uses the same commands with /usr/bin/python3.
CONTRACT_RUN = "python3 -m coverage run --branch -m pytest tests/ -q"
CONTRACT_REPORT = "python3 -m coverage report"


def load_workflow() -> dict:
    """Parse the workflow YAML; fail fast when it is missing or invalid."""
    assert WORKFLOW_FILE.is_file(), f"missing CI workflow: {WORKFLOW_FILE}"
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


def test_ci_workflow_exists_at_repository_root():
    assert WORKFLOW_FILE.is_file(), f"missing CI workflow: {WORKFLOW_FILE}"


def test_ci_workflow_runs_on_pull_request_and_push_to_main():
    section = on_section(load_workflow())
    assert "pull_request" in section, "CI must run on every pull_request"
    push = section.get("push")
    assert push is not None, "CI must run on push to the protected branch"
    assert push.get("branches") == ["main"], (
        f"push trigger must be limited to main, got: {push!r}"
    )


def test_ci_workflow_keeps_one_job_without_lint_matrix_or_cache():
    workflow = load_workflow()
    jobs = workflow["jobs"]
    assert len(jobs) == 1, f"KISS: exactly one job, got {len(jobs)}: {sorted(jobs)}"
    job = next(iter(jobs.values()))
    assert "strategy" not in job, "KISS: no test matrix"
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        assert "actions/cache" not in uses, "KISS: no cache step"
    for command in step_commands(steps_of(workflow)):
        assert not re.search(r"\b(ruff|flake8|pylint|bandit|lint)\b", command), (
            f"KISS: no lint in CI, got: {command!r}"
        )


def test_ci_workflow_checkout_fetches_full_history_and_tags():
    """Issue #126: the release reconciliation tests
    (tests/test_release_v01.py) verify the REAL annotated tag object
    (v0.1.0 → 912631d3) and its commit relationships with `git
    cat-file` / `git rev-parse` / `git merge-base --is-ancestor`
    against the checkout. The default shallow checkout (fetch-depth: 1)
    runs `git fetch --no-tags` (verified against the official
    actions/checkout@v5 source: git-command-manager.ts), so the tag
    object is absent in the CI environment and the test fails with
    `could not get object info`. `fetch-depth: 0` makes the action
    fetch all branches and `+refs/tags/*:refs/tags/*` without `--depth`
    (full history plus every tag object), so the release verification
    uses the real remote objects."""
    steps = steps_of(load_workflow())
    checkout = [
        step for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkout, "CI must check out the repository via actions/checkout"
    assert len(checkout) == 1, f"exactly one checkout step, got {len(checkout)}"
    with_options = checkout[0].get("with", {})
    assert str(with_options.get("fetch-depth")) == "0", (
        "CI checkout must fetch full history and all tags "
        f"(fetch-depth: 0) so the annotated tag objects exist for the "
        f"release reconciliation tests, got: {with_options!r}"
    )


def test_workflows_use_node_24_compatible_actions():
    workflow_dir = REPO_ROOT / ".github"
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in workflow_dir.rglob("*")
        if path.is_file()
    )
    assert "actions/checkout@v4" not in workflow_text
    assert "actions/setup-python@v5" not in workflow_text
    assert "actions/checkout@v5" in workflow_text
    assert "actions/setup-python@v6" in workflow_text


def test_ci_workflow_pins_python_3_14_like_production():
    steps = steps_of(load_workflow())
    setup = [
        step for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert setup, "CI must install Python via actions/setup-python"
    assert len(setup) == 1, f"exactly one setup-python step, got {len(setup)}"
    with_options = setup[0].get("with", {})
    assert str(with_options.get("python-version")) == "3.14", (
        f"CI must pin the production minor version 3.14, got: {with_options!r}"
    )


def test_ci_workflow_installs_requirements_txt():
    commands = step_commands(steps_of(load_workflow()))
    assert any(
        "pip install -r requirements.txt" in command for command in commands
    ), f"CI must install requirements.txt, steps run: {commands!r}"


def test_ci_workflow_installs_the_cli_and_verifies_the_entry():
    """Issue #140: the CI gate includes a clean-environment install of
    the console script and a real entry check (`orbi --help` /
    `orbi --version`, exit 0) — the acceptance `orbi
    --help` in a clean environment is a permanent gate, not a one-time
    local check. The install must use the DOCUMENTED production flow
    (`uv tool install`, the uv-tool console script): it places the
    self-contained executable at the unit's ExecStart path
    (`~/.local/bin/orbi`), so the real `systemd-analyze --user
    verify` test resolves the unit's absolute ExecStart against the
    real executable on the runner (a plain `pip install .` would put
    the script in the venv bin, where the unit's %h path does not
    point)."""
    commands = step_commands(steps_of(load_workflow()))
    uv_installs = [
        command for command in commands
        if any(
            line.strip() == "python3 -m pip install uv"
            for line in command.splitlines()
        )
    ]
    assert uv_installs, (
        "CI must install uv (the documented CLI installer) in a clean "
        f"environment, steps run: {commands!r}"
    )
    cli_installs = [
        command for command in commands
        if any(
            line.lstrip().startswith("uv tool install")
            for line in command.splitlines()
        )
    ]
    assert cli_installs, (
        "CI must install the CLI with the documented `uv tool install` "
        f"flow, steps run: {commands!r}"
    )
    assert any(
        line.lstrip().startswith("uv tool install") and line.rstrip().endswith(" .")
        for command in cli_installs
        for line in command.splitlines()
    ), (
        "CI must install the CLI from the checkout (uv tool install .) "
        f"so the console script is built from the PEP 621 packaging, "
        f"steps run: {cli_installs!r}"
    )
    entry = [
        command for command in commands
        if "orbi --help" in command
    ]
    assert entry, (
        "CI must verify the installed CLI entry (orbi --help), "
        f"steps run: {commands!r}"
    )
    assert any(
        "orbi --version" in command for command in entry
    ), "CI must also check the version entry (orbi --version)"
    assert any(
        "~/.local/bin/orbi" in command for command in entry
    ), (
        "CI must verify the entry at the unit's ExecStart path "
        "(~/.local/bin/orbi), not a venv bin copy: "
        f"{entry!r}"
    )


def test_ci_workflow_installs_the_cli_editable_and_verifies_the_import_source():
    """Issue #152: the CI install is the REAL editable uv tool install
    (`--editable`: the tool env imports `orbi` from the
    checkout, the official local deployment) and the CI verifies the
    IMPORT SOURCE (`orbi.__file__` inside the checkout path) —
    a non-editable install would silently copy the source into
    site-packages and the ExecStartPre sync could never reach it."""
    commands = step_commands(steps_of(load_workflow()))
    editable_installs = [
        command for command in commands
        if any(
            line.lstrip().startswith("uv tool install")
            and "--editable" in line
            for line in command.splitlines()
        )
    ]
    assert editable_installs, (
        "CI must install the CLI with the EDITABLE uv tool install "
        "(uv tool install --editable), steps run: {commands!r}"
    )
    # The import-source verification: the tool env's python must report
    # `orbi.__file__` and the step must check it against the
    # checkout path (GITHUB_WORKSPACE).
    source_checks = [
        command for command in commands
        if "orbi.__file__" in command
    ]
    assert source_checks, (
        "CI must verify the editable import source "
        "(orbi.__file__), steps run: {commands!r}"
    )
    assert any(
        "GITHUB_WORKSPACE" in command for command in source_checks
    ), (
        "the import-source check must compare against the checkout "
        f"path (GITHUB_WORKSPACE), got: {source_checks!r}"
    )
    # The probe must run from a NEUTRAL cwd: `python -c` puts the cwd
    # first on sys.path, so run from the checkout the checkout's own
    # `orbi.py` shadows the tool env's import (editable finder
    # and site-packages alike) and the check would pass even for a
    # non-editable install (verified against a real non-editable
    # install). The probe line must `cd /` before the interpreter.
    probe_lines = [
        line
        for command in source_checks
        for line in command.splitlines()
        if "orbi.__file__" in line
    ]
    assert probe_lines, (
        f"no probe line for orbi.__file__ in: {source_checks!r}"
    )
    for line in probe_lines:
        assert "cd / &&" in line, (
            "the import-source probe must run from a neutral cwd "
            "(`cd / &&`), otherwise the checkout's own orbi.py "
            f"shadows the tool env's import and the check is a no-op: "
            f"{line!r}"
        )


def test_ci_workflow_runs_the_contract_test_command():
    commands = step_commands(steps_of(load_workflow()))
    assert any(
        CONTRACT_RUN in command for command in commands
    ), f"CI must run the contract test command {CONTRACT_RUN!r}, steps run: {commands!r}"


def test_ci_workflow_enforces_the_tiered_coverage_gate():
    """Issue #234: the CI gate is tiered — the whole repository keeps
    line >= 95% and branch >= 95% (coverage_gate.py checks the two tiers
    SEPARATELY from the coverage JSON totals, never a merged single
    percentage), and the changed Python code keeps 100% line/branch
    (diff_coverage_gate.py against origin/main; a doc-only PR has no
    changed Python and passes). The old --fail-under=100 gate checked
    only the merged percentage and is gone."""
    commands = step_commands(steps_of(load_workflow()))
    global_gate = [
        command for command in commands
        if "coverage_gate.py" in command
    ]
    assert global_gate, (
        "CI must enforce the tiered global gate (coverage_gate.py: line "
        f">= 95% and branch >= 95% checked separately), steps run: "
        f"{commands!r}"
    )
    diff_gate = [
        command for command in commands
        if "diff_coverage_gate.py origin/main" in command
    ]
    assert diff_gate, (
        "CI must enforce the changed-code gate (diff_coverage_gate.py "
        f"origin/main: changed Python at 100% line/branch), steps run: "
        f"{commands!r}"
    )
    assert not any("--fail-under" in command for command in commands), (
        "CI must not keep the old --fail-under merged-percentage gate "
        f"(Issue #234), steps run: {commands!r}"
    )
    assert any(
        CONTRACT_REPORT in command and "--show-missing" in command
        for command in commands
    ), (
        "CI must keep the coverage report with --show-missing as "
        f"evidence (both real numbers), steps run: {commands!r}"
    )


def test_ci_workflow_runs_the_mintlify_docs_build_smoke():
    """Issue #116: the docs build smoke runs in CI with the official
    Mintlify CLI — `mint validate` (strict build validation, non-zero
    exit on any warning or error) in the SAME single job (KISS: no
    second job, no cache, no matrix)."""
    commands = step_commands(steps_of(load_workflow()))
    assert any(
        re.search(r"npm (i|install)( -g)? mint\b", command) for command in commands
    ), (
        "CI must install the official Mintlify CLI (mint), "
        f"steps run: {commands!r}"
    )
    assert any(
        re.search(r"^mint validate$|\bmint validate\b", command) and "--" not in command
        for command in commands
    ), (
        "CI must run the Mintlify build smoke `mint validate`, "
        f"steps run: {commands!r}"
    )


def test_testing_documents_remote_ci():
    """Issue #241: the remote CI contract lives in docs/testing.mdx
    (the README homepage keeps only the summary plus the docs link):
    GitHub Actions runs the same contract on every pull request and
    every push to main, with the pinned Python 3.14."""
    testing = (REPO_ROOT / "docs" / "testing.mdx").read_text(encoding="utf-8")
    assert "GitHub Actions" in testing, "testing must name the remote CI"
    assert "pull_request" in testing, "testing must say CI runs on pull requests"
    assert re.search(r"push[^\n]*main|main[^\n]*push", testing), (
        "testing must say CI runs on push to main"
    )
    assert "3.14" in testing, "testing must document the pinned CI Python version"
