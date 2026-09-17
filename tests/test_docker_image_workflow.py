"""Contract tests for the release Docker image workflow (Issue #1031)."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-image.yml"


def load_workflow() -> dict:
    assert WORKFLOW.is_file(), f"missing Docker workflow: {WORKFLOW}"
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def on_section(workflow: dict) -> dict:
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict)
    return section


def steps(workflow: dict) -> list[dict]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs
    all_steps = []
    for job in jobs.values():
        all_steps.extend(job.get("steps", []))
    return all_steps


def test_docker_workflow_triggers_on_published_releases_and_manual_dispatch():
    trigger = on_section(load_workflow())
    assert trigger.get("release") == {"types": ["published"]}
    assert "workflow_dispatch" in trigger


def test_docker_workflow_targets_both_registries_and_required_platforms():
    workflow = load_workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io/orbi-build/orbi" in text
    assert "docker.io/orbibuild/orbi" in text
    assert "linux/amd64,linux/arm64" in text
    assert any("docker/build-push-action@" in str(step.get("uses", "")) for step in steps(workflow))
    assert any("3rd/docker" in str(step) for step in steps(workflow))


def test_build_push_context_matches_dockerfile_directory():
    workflow = load_workflow()
    build_push_steps = [
        step for step in steps(workflow)
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    ]

    assert build_push_steps
    dockerfile_directory = Path("3rd/docker")
    for step in build_push_steps:
        context = Path(str(step["with"]["context"]))
        dockerfile = Path(str(step["with"]["file"]))
        assert context == dockerfile_directory
        assert dockerfile.parent == Path(".")


def test_docker_hub_steps_skip_without_both_credentials():
    workflow = load_workflow()
    docker_job = workflow["jobs"]["docker"]
    enabled = str(docker_job.get("env", {}).get("DOCKERHUB_ENABLED", ""))
    assert "secrets.DOCKERHUB_USERNAME != ''" in enabled
    assert "secrets.DOCKERHUB_TOKEN != ''" in enabled
    dockerhub_steps = [
        step for step in docker_job["steps"]
        if "docker.io/orbibuild/orbi" in str(step)
        or "dockerhub-description" in str(step)
    ]
    assert dockerhub_steps
    for step in dockerhub_steps:
        assert step.get("if") == "env.DOCKERHUB_ENABLED == 'true'"


def test_docker_hub_overview_contains_required_links_and_run_command():
    overview = REPO_ROOT / "3rd/docker/README.dockerhub.md"
    text = overview.read_text(encoding="utf-8")
    assert "https://github.com/orbi-build/orbi" in text
    assert "https://docs.orbi.build/docker" in text
    assert "community-maintained" in text
    assert "unmodified official deployment" in text
    assert "docker run -d --name orbi" in text
