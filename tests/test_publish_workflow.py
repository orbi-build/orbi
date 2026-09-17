"""Publish workflow contract (Issue #163, #852).

`.github/workflows/publish.yml` is the packaging side of the release
contract: it reacts to the `v*` tag the ai-release state machine pushes
and owns build -> verify -> publish -> PyPI verification. These tests
fail when the workflow stops enforcing that chain — the tag/version
consistency check, the artifact content + secret verification, the
clean-venv wheel smoke (the artifact shape users install), the
structured failure-path smoke, the Trusted-Publishing-only upload, and
the post-publish `orbi-cli==X.Y.Z` installation from PyPI (Issue #874: the
PyPI distribution is `orbi-cli`, artifact filenames normalize to
`orbi_cli_<version>` per PEP 625/PEP 427) — or when it
drifts into unpinned actions.

Issue #852 adds the manual (re)publish path: the PyPI-side trusted
publisher entry is configured by a human against an exact claim tuple,
so the workflow must (a) carry a `workflow_dispatch` trigger with a
`tag` input so an already-tagged release whose publish failed can be
re-published without a new release cycle, (b) resolve every
tag/version consumer from one `release_ref` mapping, and (c) declare
the publish job's environment — the OIDC `environment` claim the PyPI
entry must match exactly (a MISSING claim forced a blank field and a
fragile match).
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


def test_publish_workflow_supports_manual_republish():
    """Issue #852: a failed publish of an EXISTING tag must be
    re-publishable without a new release cycle. The trigger must live
    in the file AT the dispatched ref, so the re-publish dispatches
    from the default branch and checks out the input tag explicitly —
    dispatching the old tag itself cannot work (its file predates the
    trigger)."""
    workflow = load_workflow()
    dispatch = on_section(workflow).get("workflow_dispatch")
    assert isinstance(dispatch, dict), "workflow_dispatch trigger missing"
    inputs = dispatch.get("inputs") or {}
    tag = inputs.get("tag") or {}
    assert tag.get("required") is True, (
        "the tag input is required: it names the release to (re)publish"
    )
    env = workflow.get("env") or {}
    assert env.get("release_ref") == "${{ inputs.tag || github.ref_name }}", (
        "every tag/version consumer must read ONE release_ref mapping "
        "(dispatch input, else the pushed tag)"
    )
    checkout = [
        step for step in steps_of(workflow, "build")
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkout) == 1, checkout
    assert checkout[0].get("with", {}).get("ref") == (
        "${{ inputs.tag || github.ref }}"
    ), "the build must package the input TAG, not the dispatched branch head"


def test_publish_workflow_publishes_from_one_release_ref():
    """The tag/version consistency chain must consume the same
    release_ref in build (tag vs pyproject version) and verify-pypi
    (PyPI install), for both the tag push and the manual re-publish."""
    build = "\n".join(step_commands(steps_of(load_workflow(), "build")))
    verify = "\n".join(step_commands(steps_of(load_workflow(), "verify-pypi")))
    assert 'tag="${release_ref#v}"' in build, (
        "the pushed/dispatched tag must be compared against the packaging version"
    )
    assert '["project"]["version"]' in build, (
        "the PEP 621 version must be compared against the tag"
    )
    assert 'version="${release_ref#v}"' in verify, (
        "the exact published version must be installed from PyPI"
    )


def test_publish_workflow_declares_the_trusted_publisher_environment():
    """The publish job's environment becomes the OIDC `environment`
    claim (Issue #852): the PyPI entry must carry exactly `pypi` — a
    MISSING claim used to force a blank, fragile match."""
    publish = load_workflow()["jobs"]["publish"]
    assert publish.get("environment") == "pypi", publish.get("environment")


def test_publish_workflow_documents_the_pypi_trusted_publisher_setup():
    """The PyPI-side entry is a human step against an exact claim tuple
    (Issue #852): the workflow header is its single source of truth —
    the four form values plus the re-publish command."""
    text = PUBLISH_FILE.read_text(encoding="utf-8")
    for needle in (
        "orbi-build",
        "publish.yml",
        "environment=pypi",
        "gh workflow run publish.yml",
    ):
        assert needle in text, f"the trusted publisher runbook must carry {needle!r}"


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
        "orbi_cli-",  # Issue #874: the orbi-cli artifact filename prefix
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
    assert 'check_failed check=[^ ]+ ' in commands, (
        "the prerequisite gate must retain its structured prerequisite shape"
    )
    assert "config_not_found path=[^;]+; reason=[^;]+; fix=" in commands, (
        "the prerequisite gate must accept the current missing-config shape"
    )
    assert "config_not_found path=" in commands, (
        "the prerequisite gate must accept the current missing-config shape"
    )
    assert "reason=" in commands and "fix=" in commands, (
        "the missing-config shape must retain actionable fields"
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
    assert "timeout 960" in commands, (
        "the PyPI propagation wait is bounded (Issue #95), then fails fast"
    )
    assert "for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15;" in commands, (
        "the PyPI propagation retry budget must allow 15 attempts"
    )
    assert "sleep 60" in commands, (
        "the PyPI propagation retry interval must allow index propagation"
    )
    assert 'orbi-cli==$2' in commands or 'orbi-cli=="' in commands or 'orbi-cli==$version' in commands, (
        "the exact published version must be installed from PyPI "
        "under the orbi-cli distribution name (Issue #874)"
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
