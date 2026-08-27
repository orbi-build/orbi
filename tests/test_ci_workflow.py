"""Regression tests for the GitHub Actions CI workflow (Issue #56).

The repository contract (AGENTS.md) says the full pytest suite must run
with 100% line/branch coverage via the coverage commands. Until now that
only happened on the local Runner machine. The workflow in
`.github/workflows/ci.yml` runs the same contract remotely on every
`pull_request` and `push` to `main`, so the gate is visible on GitHub.

These tests fail when the workflow file is missing, when it stops
enforcing the contract (triggers, pinned Python, requirements install,
contract test commands, 100% coverage), or when it drifts into the extras
the Issue explicitly forbids (lint, matrix, cache). The README must keep
documenting what the remote CI is and when it runs.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README_FILE = REPO_ROOT / "README.md"

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


def test_ci_workflow_runs_the_contract_test_command():
    commands = step_commands(steps_of(load_workflow()))
    assert any(
        CONTRACT_RUN in command for command in commands
    ), f"CI must run the contract test command {CONTRACT_RUN!r}, steps run: {commands!r}"


def test_ci_workflow_enforces_full_line_and_branch_coverage():
    commands = step_commands(steps_of(load_workflow()))
    enforcing = [
        command for command in commands
        if CONTRACT_REPORT in command and "--fail-under=100" in command
    ]
    assert enforcing, (
        "CI must enforce 100% line/branch coverage "
        f"({CONTRACT_REPORT} --fail-under=100), steps run: {commands!r}"
    )
    assert any("--show-missing" in command for command in enforcing), (
        "coverage report must keep the local --show-missing shape"
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


def test_readme_documents_remote_ci():
    readme = README_FILE.read_text(encoding="utf-8")
    assert "GitHub Actions" in readme, "README must name the remote CI"
    assert "pull_request" in readme, "README must say CI runs on pull requests"
    assert re.search(r"push[^\n]*main|main[^\n]*push", readme), (
        "README must say CI runs on push to main"
    )
    assert "3.14" in readme, "README must document the pinned CI Python version"
