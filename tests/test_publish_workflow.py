"""Publish workflow contract (Issue #163).

`.github/workflows/publish.yml` is the packaging side of the release
contract: it reacts to the `v*` tag the ai-release state machine pushes
and owns build -> verify -> publish -> PyPI verification. These tests
fail when the workflow stops enforcing that chain — the tag/version
consistency check, the artifact content + secret verification, the
clean-venv wheel smoke (the artifact shape users install), the
structured failure-path smoke, the Trusted-Publishing-only upload, and
the post-publish `orbi==X.Y.Z` installation from PyPI — or when it
drifts into unpinned actions.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_FILE = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def load_workflow() -> dict:
    assert PUBLISH_FILE.is_file(), f"missing publish workflow: {PUBLISH_FILE}"
    workflow = yaml.safe_load(PUBLISH_FILE.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "workflow file is not a YAML mapping"
    return workflow


def on_section(workflow: dict) -> dict:
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict), "workflow has no `on:` trigger section"
    return section


def steps_of(workflow: dict, job_name: str) -> list[dict]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and job_name in jobs, (
        f"missing job {job_name}: {sorted(jobs or {})}"
    )
    steps = jobs[job_name].get("steps")
    assert isinstance(steps, list) and steps, f"job {job_name} has no steps"
    return steps


def step_commands(steps: list[dict]) -> list[str]:
    return [
        str(step.get("run", "")).strip()
        for step in steps
        if step.get("run")
    ]


def test_publish_workflow_triggers_on_version_tags_only():
    section = on_section(load_workflow())
    assert section.get("push", {}).get("tags") == ["v*"], (
        "the publish pipeline reacts to the release machine's v* tag"
    )
    assert "pull_request" not in section, (
        "publishing is a tag event, never a PR event"
    )


def test_publish_workflow_verifies_tag_matches_packaging_version():
    commands = " ".join(step_commands(steps_of(load_workflow(), "build")))
    assert "GITHUB_REF_NAME" in commands, "the pushed tag must be read"
    assert 'pyproject.toml"["project"]["version"]' in commands.replace(
        "open(", "(",
    ) or '["project"]["version"]' in commands, (
        "the PEP 621 version must be compared against the tag"
    )


def test_publish_workflow_builds_the_sdist_and_the_wheel():
    commands = step_commands(steps_of(load_workflow(), "build"))
    assert any(
        re.search(r"python3 -m pip install .*build", command)
        for command in commands
    ), "the build tool must be installed by the workflow itself"
    assert any(
        re.search(r"python3 -m build\b", command) for command in commands
    ), "python3 -m build must produce both artifacts"


def test_publish_workflow_verifies_complete_and_clean_artifacts():
    """The wheel carries every runtime module + the shipped example
    config; both artifacts are scanned for run artifacts and secret
    shapes (full token shapes — the bare `ghp_` prefix is a legitimate
    redaction literal in pi_activity.py)."""
    script = "\n".join(step_commands(steps_of(load_workflow(), "build")))
    for needle in (
        "src/orbi",  # every runtime module walked from the package dir
        "orbi/example_config.toml",  # in the wheel AND the sdist
        ".pi-session/",
        "github_pat_",
        "ghp_",
        ".pyc",
    ):
        assert needle in script, f"artifact verification must check {needle!r}"


def test_publish_workflow_smokes_the_wheel_in_a_clean_venv():
    commands = "\n".join(step_commands(steps_of(load_workflow(), "build")))
    assert "python3 -m venv" in commands, "a clean venv, not the workflow env"
    assert "dist/*.whl" in commands, "the WHEEL is the installed artifact"
    assert "orbi --version" in commands and "--help" in commands
    assert "site-packages" in commands, (
        "the import source must be site-packages (no checkout import)"
    )


def test_publish_workflow_smokes_the_structured_failure_paths():
    commands = "\n".join(step_commands(steps_of(load_workflow(), "build")))
    assert "check_failed check=" in commands, (
        "the prerequisite gate failure must stay a structured line"
    )
    assert "setup_failed reason=" in commands, (
        "the setup failure must stay a structured line"
    )
    assert "Traceback" in commands, (
        "the smoke must prove no traceback reaches the user"
    )
    assert "setup-orbi.toml" in commands, (
        "the setup smoke must prove the config comes from the SHIPPED example"
    )


def test_publish_workflow_publishes_via_trusted_publishing_only():
    workflow = load_workflow()
    publish = workflow["jobs"]["publish"]
    permissions = publish.get("permissions") or {}
    assert permissions.get("id-token") == "write", (
        "the publish job needs the OIDC token permission"
    )
    uses = [
        str(step.get("uses", "")) for step in publish.get("steps", [])
    ]
    pypi = [u for u in uses if u.startswith("pypa/gh-action-pypi-publish@")]
    assert len(pypi) == 1, f"exactly one publish action: {uses!r}"
    assert re.fullmatch(r"pypa/gh-action-pypi-publish@v\d+\.\d+\.\d+", pypi[0]), (
        f"the publish action must be pinned to an exact release, got {pypi[0]!r}"
    )
    assert not any("secret" in str(step).lower() for step in publish["steps"]), (
        "Trusted Publishing replaces the API token secret"
    )


def test_publish_workflow_verifies_the_published_pypi_version():
    commands = "\n".join(step_commands(steps_of(load_workflow(), "verify-pypi")))
    assert "timeout 300" in commands, (
        "the PyPI propagation wait is bounded (Issue #95), then fails fast"
    )
    assert 'orbi==$2' in commands or 'orbi=="' in commands or 'orbi==$version' in commands, (
        "the exact published version must be installed from PyPI"
    )
    assert 'orbi --version' in commands, (
        "the installed CLI must report the tagged version"
    )


def test_publish_workflow_uses_the_pinned_current_actions():
    text = PUBLISH_FILE.read_text(encoding="utf-8")
    assert "actions/checkout@v5" in text
    assert "actions/setup-python@v6" in text
    assert "actions/upload-artifact@v4" in text
    assert "actions/download-artifact@v4" in text
    assert "actions/checkout@v4" not in text
    assert "actions/setup-python@v5" not in text
