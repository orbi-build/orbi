"""Contract tests for the `CI Failure Issue` workflow (Issue #106).

The workflow (`.github/workflows/ci-failure-issue.yml`) is the runtime entry
of the CI failure triage: it reacts to the COMPLETED runs of the `CI`
workflow (`on: workflow_run`), holds the minimal explicit permissions
(`actions: read`, `contents: read`, `issues: write`, `pull-requests: read`)
and runs the triage
script with the workflow token. These tests fail when the workflow is
missing, stops watching the CI workflow, gains extra permissions, or can
fake success (continue-on-error / `|| true`) — the review of the previous
attempt (closed PR #108) rejected exactly that: a triage script without its
Action entry is no automation at all.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci-failure-issue.yml"
TRIAGE_SCRIPT = REPO_ROOT / "ci_failure_triage.py"


def load_workflow() -> dict:
    assert WORKFLOW_FILE.is_file(), f"missing triage workflow: {WORKFLOW_FILE}"
    workflow = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "workflow file is not a YAML mapping"
    return workflow


def on_section(workflow: dict) -> dict:
    """The `on:` key (YAML 1.1 parses the bare word `on` as boolean True)."""
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict), "workflow has no `on:` trigger section"
    return section


def steps_of(workflow: dict) -> list[dict]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "workflow has no jobs"
    flattened: list[dict] = []
    for job in jobs.values():
        assert isinstance(job, dict) and job.get("steps"), f"job without steps"
        flattened.extend(job["steps"])
    return flattened


def test_workflow_exists():
    assert WORKFLOW_FILE.is_file()


def test_triage_script_exists():
    assert TRIAGE_SCRIPT.is_file(), "the workflow must call a real script"


def test_triggers_only_on_the_completed_ci_workflow():
    """`on: workflow_run` for the CI workflow, `types: [completed]` — the
    anti-recursion anchor: the triage reacts ONLY to CI runs, and Issue
    create/update/close emit no CI workflow_run, so the triage can never
    re-trigger itself."""
    trigger = on_section(load_workflow()).get("workflow_run")
    assert trigger is not None, "the triage must be triggered by workflow_run"
    assert trigger.get("workflows") == ["CI"]
    assert trigger.get("types") == ["completed"]
    assert set(on_section(load_workflow())) == {"workflow_run"}, (
        "KISS: workflow_run is the only trigger"
    )


def test_workflow_name_is_not_ci():
    """The workflow must not be named `CI`: its own `workflows: ["CI"]`
    filter must never match itself."""
    name = load_workflow().get("name")
    assert isinstance(name, str) and name != "CI", f"workflow name: {name!r}"


def test_minimal_explicit_permissions():
    assert load_workflow().get("permissions") == {
        "actions": "read",        # read the completed CI run's jobs
        "contents": "read",       # check out the triage script
        "issues": "write",        # create/update/close the bug Issue
        "pull-requests": "read",  # resolve the run's PR (commits/{sha}/pulls)
    }


def test_single_job_runs_the_triage_script():
    workflow = load_workflow()
    assert len(workflow["jobs"]) == 1, "KISS: exactly one triage job"
    commands = [
        str(step.get("run", "")) for step in steps_of(workflow) if step.get("run")
    ]
    assert any(
        "python3 ci_failure_triage.py" in command for command in commands
    ), f"the job must run the triage script, steps run: {commands!r}"


def test_token_is_the_workflow_token():
    """The triage step authenticates with the workflow's own token
    (`github.token`) — never a PAT or another secret."""
    envs = [
        step.get("env", {}) for step in steps_of(load_workflow())
        if isinstance(step.get("env"), dict)
    ]
    assert any(
        env.get("GH_TOKEN") == "${{ github.token }}" for env in envs
    ), f"GH_TOKEN must be the workflow token, step envs: {envs!r}"


def test_no_step_can_fake_success():
    """Fail fast: no continue-on-error, no swallowed exit codes."""
    for step in steps_of(load_workflow()):
        assert not step.get("continue-on-error"), (
            f"continue-on-error would fake triage success: {step!r}"
        )
        run = str(step.get("run", ""))
        assert "|| true" not in run and "|| :" not in run, (
            f"a swallowed exit code would fake triage success: {run!r}"
        )


def test_checkout_and_pinned_python_like_ci():
    """Same interpreter convention as the CI workflow (Python 3.14) and a
    real checkout of the triage script, using Node 24-compatible actions."""
    steps = steps_of(load_workflow())
    uses = [str(step.get("uses", "")) for step in steps]
    assert uses.count("actions/checkout@v5") == 1, uses
    setup = [
        step for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert len(setup) == 1, uses
    assert uses.count("actions/setup-python@v6") == 1, uses
    assert str(setup[0].get("with", {}).get("python-version")) == "3.14"
