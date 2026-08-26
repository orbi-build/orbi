"""Issue #106: CI failure triage — workflow contract + script behavior.

The repository contract (AGENTS.md) requires the full pytest suite with
100% line/branch coverage, so the triage script `ci_failure_issue.py` is
tested here with the `gh` subprocess mocked (no network), and the new
`.github/workflows/ci-failure-issue.yml` workflow is tested as a YAML
contract file, in the same style as `test_ci_workflow.py`.

External interfaces asserted below were verified against the official
GitHub docs / OpenAPI spec / one real API call (Issue #73):

- `on.workflow_run` trigger with `workflows: [CI]` and
  `types: [completed]` (docs: events that trigger workflows);
- `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs` returns
  `jobs[]` with `name`, `conclusion` and `steps[]` (OpenAPI `job` schema);
- `GET /repos/{owner}/{repo}/issues?labels=bug,ai-ready&state=open`
  (OpenAPI `labels` query param, comma separated names);
- `POST /repos/{owner}/{repo}/issues` `{title, body, labels}` and
  `PATCH /repos/{owner}/{repo}/issues/{n}` `{body, state, state_reason}`;
- `gh api` authenticates from `GITHUB_TOKEN` and accepts `--input -`
  (verified with real calls, gh 2.97).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import ci_failure_issue as cfi

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci-failure-issue.yml"
README_FILE = REPO_ROOT / "README.md"


# ---------------------------------------------------------------------------
# Workflow file contract
# ---------------------------------------------------------------------------

def load_failure_workflow() -> dict:
    assert WORKFLOW_FILE.is_file(), f"missing workflow: {WORKFLOW_FILE}"
    workflow = yaml.safe_load(WORKFLOW_FILE.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "workflow file is not a YAML mapping"
    return workflow


def on_section(workflow: dict) -> dict:
    """The `on:` key (YAML 1.1 parses the bare word `on` as boolean True)."""
    section = workflow.get("on", workflow.get(True))
    assert isinstance(section, dict), "workflow has no `on:` trigger section"
    return section


def job_of(workflow: dict) -> dict:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and len(jobs) == 1, (
        f"KISS: exactly one job, got: {jobs!r}"
    )
    return next(iter(jobs.values()))


def steps_of(workflow: dict) -> list[dict]:
    job = job_of(workflow)
    assert isinstance(job.get("steps"), list) and job["steps"], (
        "job has no steps"
    )
    return job["steps"]


def step_commands(steps: list[dict]) -> list[str]:
    return [str(step.get("run", "")).strip() for step in steps if step.get("run")]


def test_failure_workflow_reacts_only_to_ci_workflow_completion():
    section = on_section(load_failure_workflow())
    workflow_run = section.get("workflow_run")
    assert isinstance(workflow_run, dict), (
        "the triage workflow must be triggered by `on: workflow_run`"
    )
    assert workflow_run.get("workflows") == ["CI"], (
        f"must observe exactly the CI workflow, got: {workflow_run.get('workflows')!r}"
    )
    assert workflow_run.get("types") == ["completed"], (
        f"must react to completed runs only, got: {workflow_run.get('types')!r}"
    )
    # Anti-recursion: the trigger is workflow_run of CI only — there is no
    # pull_request/push trigger, so an Issue created by this workflow can
    # never re-trigger it.
    for key in ("pull_request", "push", "workflow_dispatch"):
        assert key not in section, (
            f"anti-recursion: no {key!r} trigger allowed, got: {sorted(section)}"
        )


def test_failure_workflow_declares_explicit_minimal_permissions():
    workflow = load_failure_workflow()
    permissions = workflow.get("permissions")
    assert isinstance(permissions, dict), (
        "the workflow must declare explicit permissions"
    )
    assert permissions.get("issues") == "write", (
        f"issues: write is required, got: {permissions!r}"
    )
    assert permissions.get("actions") == "read", (
        f"actions: read is required for the jobs API, got: {permissions!r}"
    )
    # No broader grant than the two declared scopes.
    assert set(permissions) == {"issues", "actions"}, (
        f"minimal permission set, got: {sorted(permissions)}"
    )


def test_failure_workflow_serializes_per_observed_run():
    workflow = load_failure_workflow()
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), (
        "concurrent triage runs must be serialized (concurrency group)"
    )
    group = str(concurrency.get("group", ""))
    assert "workflow_run.id" in group and "run_attempt" in group, (
        f"concurrency group must key on the observed run id and attempt, got: {group!r}"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "a second triage for the same run must wait, not cancel the first"
    )


def test_failure_workflow_uses_the_same_action_pins_as_ci():
    steps = steps_of(load_failure_workflow())
    uses = [str(step.get("uses", "")) for step in steps if step.get("uses")]
    assert any(u == "actions/checkout@v4" for u in uses), (
        f"checkout must stay pinned like ci.yml, got: {uses}"
    )
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1, f"exactly one setup-python step, got {setup!r}"
    assert str(setup[0].get("with", {}).get("python-version")) == "3.14", (
        "same pinned Python minor version as ci.yml"
    )


def test_failure_workflow_installs_gh_and_runs_the_script():
    commands = step_commands(steps_of(load_failure_workflow()))
    install = [c for c in commands if "cli.github.com/packages" in c]
    assert install, (
        "gh is not preinstalled on ubuntu-latest; install it with the "
        f"official apt package, steps run: {commands!r}"
    )
    assert any("sudo apt install gh" in c for c in install), (
        f"official apt install command expected, got: {install!r}"
    )
    assert any("ci_failure_issue.py" in c for c in commands), (
        f"the workflow must run the triage script, steps run: {commands!r}"
    )
    # The script must run even when the checkout/setup steps failed
    # (workflow-own failure must still be diagnosable, not silent).
    script_step = next(s for s in steps_of(load_failure_workflow())
                       if "ci_failure_issue.py" in str(s.get("run", "")))
    assert script_step.get("if") == "always()", (
        "the triage step must run unconditionally (if: always())"
    )


def test_failure_workflow_passes_the_github_token():
    job = job_of(load_failure_workflow())
    env = job.get("env") or {}
    assert env.get("GITHUB_TOKEN") == "${{ secrets.GITHUB_TOKEN }}", (
        f"gh authenticates via GITHUB_TOKEN, job env: {env!r}"
    )


# ---------------------------------------------------------------------------
# Script behavior (gh subprocess mocked)
# ---------------------------------------------------------------------------

def make_event(conclusion="failure", *, event="pull_request",
               path=cfi.CI_WORKFLOW_PATH, head_branch="feature-x",
               head_sha="a" * 40, run_id=111, run_number=7,
               run_attempt=1, started="2026-08-27T00:00:00Z",
               display="test title") -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "name": "CI",
            "event": event,
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "path": path,
            "run_attempt": run_attempt,
            "run_number": run_number,
            "run_started_at": started,
            "html_url": f"https://github.com/xqliu/muyan-pilot/actions/runs/{run_id}",
            "display_title": display,
        },
    }


def make_job(name="tests", conclusion="failure", run_id=111,
             run_attempt=1, steps=None) -> dict:
    if steps is None:
        steps = [
            {"name": "Check out the repository", "conclusion": "success"},
            {"name": "Run the full test suite with branch coverage (contract)",
             "conclusion": "failure"},
        ]
    return {
        "id": 9000,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "name": name,
        "conclusion": conclusion,
        "html_url": (
            f"https://github.com/xqliu/muyan-pilot/actions/runs/{run_id}/job/9000"
        ),
        "steps": steps,
    }


def make_issue(number=42, body="old body", state="open") -> dict:
    return {
        "number": number,
        "title": "CI failure: CI / tests (pull_request #5 on feature-x)",
        "body": body,
        "state": state,
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
    }


def pr_response(number=5, head_sha="a" * 40, state="OPEN") -> dict:
    """GraphQL response shape for the PR resolution query
    (`data.repository.pullRequests.nodes`)."""
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {"number": number, "state": state,
                         "headRefOid": head_sha},
                    ]
                }
            }
        }
    }


class GhMock:
    """Records `gh api` calls at the subprocess boundary and answers them.

    Patches `cfi.subprocess.run` (the real boundary), so the real
    `cfi.gh_api` command building, JSON parsing and error handling all
    execute. `handlers` maps an endpoint substring to a callable
    `(payload) -> response` (or a plain response). Anything unmatched
    answers `{}`. A handler may raise `GhFailure` to simulate a failed
    `gh` process (non-zero exit, stderr surfaced).
    """

    class GhFailure(Exception):
        def __init__(self, stderr: str):
            super().__init__(stderr)
            self.stderr = stderr

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: list[dict] = []

    def install(self, monkeypatch) -> "GhMock":
        def fake_run(cmd, **kwargs):
            # cmd: gh api <endpoint> [-X METHOD] [--paginate] [--input -]
            assert cmd[0] == "gh" and cmd[1] == "api", f"unexpected cmd: {cmd}"
            endpoint = cmd[2]
            method = "GET"
            if "-X" in cmd:
                method = cmd[cmd.index("-X") + 1]
            paginate = "--paginate" in cmd
            payload = None
            if "--input" in cmd:
                assert cmd[cmd.index("--input") + 1] == "-"
                payload = json.loads(kwargs["input"])
            for key, handler in self.handlers.items():
                if key in endpoint:
                    try:
                        response = (handler(payload) if callable(handler)
                                    else handler)
                    except GhMock.GhFailure as exc:
                        self.calls.append({
                            "endpoint": endpoint, "method": method,
                            "payload": payload, "paginate": paginate,
                            "response": None, "failed": True,
                        })
                        return type("R", (), {
                            "returncode": 1, "stdout": "",
                            "stderr": exc.stderr,
                        })()
                    self.calls.append({
                        "endpoint": endpoint, "method": method,
                        "payload": payload, "paginate": paginate,
                        "response": response, "failed": False,
                    })
                    return type("R", (), {
                        "returncode": 0,
                        "stdout": json.dumps(response), "stderr": "",
                    })()
            self.calls.append({
                "endpoint": endpoint, "method": method,
                "payload": payload, "paginate": paginate, "response": {},
                "failed": False,
            })
            return type("R", (), {
                "returncode": 0, "stdout": "{}", "stderr": "",
            })()

        monkeypatch.setattr(cfi.subprocess, "run", fake_run)
        return self

    def created_issue(self) -> dict | None:
        for call in self.calls:
            if (call["method"] == "POST"
                    and call["endpoint"].endswith("/issues")):
                return call["payload"]
        return None

    def patched_issue(self) -> dict | None:
        for call in self.calls:
            if call["method"] == "PATCH" and "/issues/" in call["endpoint"]:
                return call["payload"]
        return None


@pytest.fixture
def gh_env(monkeypatch):
    """GITHUB_REPOSITORY + event path for the script under test."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "xqliu/muyan-pilot")
    return monkeypatch


def write_event(monkeypatch, event: dict) -> Path:
    path = Path("/tmp/ci-failure-issue-event.json")
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    return path


def run_triage(monkeypatch, event: dict, mock: GhMock) -> int:
    """Run cfi.main with gh mocked; return the exit code (0 = no exit)."""
    write_event(monkeypatch, event)
    try:
        cfi.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_ignores_non_ci_workflow(gh_env, monkeypatch, capsys):
    mock = GhMock({}).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(path=".github/workflows/other.yml"), mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 0, "a non-CI workflow completion must be a no-op success"
    assert "ci_failure_issue_ignored" in out
    assert mock.calls == [], "no gh call for an ignored workflow"


def test_ignores_non_target_event(gh_env, monkeypatch, capsys):
    mock = GhMock({}).install(monkeypatch)
    # push to a non-main branch is not a CI trigger target: the CI workflow
    # itself only runs on push to main, so a push event on another branch
    # must be ignored.
    code = run_triage(monkeypatch,
                      make_event(event="push", head_branch="not-main"), mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 0
    assert "ci_failure_issue_ignored" in out


def test_ignores_incomplete_run(gh_env, monkeypatch, capsys):
    mock = GhMock({}).install(monkeypatch)
    event = make_event()
    event["workflow_run"]["status"] = "queued"
    code = run_triage(monkeypatch, event, mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 0
    assert "ci_failure_issue_ignored" in out


def test_failure_creates_bug_issue_with_full_evidence(gh_env, monkeypatch, capsys):
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": pr_response(),
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=111), mock)
    err = capsys.readouterr().err
    assert code == 0, err
    created = mock.created_issue()
    assert created is not None, "a bug Issue must have been created"
    body = created["body"]
    for needle in (
        "workflow: `CI`",
        "job: `tests`",
        "event: `pull_request`",
        "commit: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`",
        "run id: `111`",
        "https://github.com/xqliu/muyan-pilot/actions/runs/111",
        "https://github.com/xqliu/muyan-pilot/actions/runs/111/job/9000",
        "Run the full test suite with branch coverage (contract)",
        "2026-08-27T00:00:00Z",
        "fingerprint: `",
    ):
        assert needle in body, f"missing {needle!r} in the created Issue body"
    # The PR target must be resolved for pull_request runs.
    assert "PR #" in body, "pull_request runs must reference the PR number"
    assert created["labels"] == ["bug", "ai-ready"], (
        f"the auto Issue must be bug + ai-ready, got: {created['labels']!r}"
    )
    assert re.match(r"^CI failure: ", created["title"]), (
        f"title: {created['title']!r}"
    )
    # The Issue query must filter on the exact label pair.
    issue_calls = [c for c in mock.calls if "/issues" in c["endpoint"]]
    assert issue_calls and "labels=bug,ai-ready" in issue_calls[0]["endpoint"], (
        f"issue search must use labels=bug,ai-ready, got: {issue_calls!r}"
    )
    assert issue_calls[0]["paginate"] is True, (
        "the issue search must paginate (more than 100 open Issues)"
    )


def test_pull_request_target_resolves_the_pr_number(gh_env, monkeypatch, capsys):
    prs = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {"number": 5, "state": "OPEN",
                         "headRefOid": "a" * 40},
                        {"number": 6, "state": "CLOSED",
                         "headRefOid": "a" * 40},
                    ]
                }
            }
        }
    }
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": prs,
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=111), mock)
    assert code == 0, capsys.readouterr().err
    body = mock.created_issue()["body"]
    assert "PR #5" in body, f"the OPEN PR must be resolved, body: {body!r}"
    # The GraphQL query must fetch headRefOid (client-side match on the
    # head SHA — pullRequests has no headRefOid filter argument).
    gql_calls = [c for c in mock.calls if c["endpoint"] == "graphql"]
    assert gql_calls, "pull_request runs must resolve the PR via GraphQL"
    assert "headRefOid" in gql_calls[0]["payload"]["query"]
    assert gql_calls[0]["payload"]["variables"] == {
        "owner": "xqliu", "repo": "muyan-pilot",
    }


def test_same_failure_rerun_updates_existing_issue_not_create(
        gh_env, monkeypatch, capsys):
    # First run: no existing issue -> create.
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": pr_response(),
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=111,
                                 run_attempt=1), mock)
    assert code == 0, capsys.readouterr().err
    first_body = mock.created_issue()["body"]
    fingerprints = cfi.extract_fingerprints(first_body)
    assert fingerprints, "created body must carry the fingerprint"
    fingerprint = fingerprints[0]

    # Second run of the SAME failure (same commit, same target, same job,
    # same conclusion) but a NEW run id: must match the fingerprint and
    # PATCH the existing Issue instead of creating another one.
    mock2 = GhMock({
        "/jobs": {"jobs": [make_job(run_id=222)]},
        "/issues": [make_issue(body=first_body)],
        "graphql": pr_response(),
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=222,
                                 run_number=8), mock2)
    assert code == 0, capsys.readouterr().err
    assert mock2.created_issue() is None, (
        "re-running the same failure must NOT create a second Issue"
    )
    updated = mock2.patched_issue()
    assert updated is not None, "the existing Issue must be updated (PATCH)"
    body = updated["body"]
    assert fingerprint in cfi.extract_fingerprints(body), (
        "the updated body must keep the same stable fingerprint"
    )
    assert "run id: `222`" in body, "the update must carry the new run"
    assert "run id `111`" in body, (
        "the update must keep the previous run evidence line"
    )
    assert "run id `222`" in body, (
        "the update must add its own run evidence line"
    )


def test_distinct_failures_create_distinct_issues(gh_env, monkeypatch, capsys):
    prs = pr_response()
    # Failure A: job tests on PR #5, commit A.
    mock_a = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": prs,
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-a", head_sha="a" * 40,
                                 run_id=111), mock_a)
    assert code == 0, capsys.readouterr().err
    fingerprint_a = cfi.extract_fingerprints(mock_a.created_issue()["body"])[0]

    # Failure B: different commit (same job, same event, different branch):
    # different fingerprint -> a second, independent Issue.
    mock_b = GhMock({
        "/jobs": {"jobs": [make_job(run_id=333)]},
        "/issues": [],
        "graphql": prs,
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-b", head_sha="b" * 40,
                                 run_id=333), mock_b)
    assert code == 0, capsys.readouterr().err
    created_b = mock_b.created_issue()
    assert created_b is not None, "a distinct failure must create its own Issue"
    fingerprint_b = cfi.extract_fingerprints(created_b["body"])[0]
    assert fingerprint_a != fingerprint_b, (
        "different commits must produce different fingerprints"
    )


def test_recovery_closes_matching_issue_with_evidence(gh_env, monkeypatch, capsys):
    prs = pr_response()
    # First a failure created the Issue.
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": prs,
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", head_sha="a" * 40,
                                 run_id=111), mock)
    assert code == 0, capsys.readouterr().err
    body_fail = mock.created_issue()["body"]
    fingerprints = cfi.extract_fingerprints(body_fail)
    assert fingerprints
    fingerprint = fingerprints[0]

    # Then the same commit passes: recovery must find the Issue by
    # fingerprint, append the recovery evidence and close it.
    mock2 = GhMock({
        "/jobs": {"jobs": [make_job(conclusion="success")]},
        "/issues": [make_issue(body=body_fail)],
        "graphql": prs,
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="success", event="pull_request",
                                 head_branch="feature-x", head_sha="a" * 40,
                                 run_id=444, run_number=12), mock2)
    assert code == 0, capsys.readouterr().err
    assert mock2.created_issue() is None, "recovery must not create Issues"
    updated = mock2.patched_issue()
    assert updated is not None, "recovery must update the matching Issue"
    body = updated["body"]
    assert "recovered" in body.lower(), (
        "the recovery evidence must be visible in the body"
    )
    assert "run id `444`" in body, (
        "the recovery evidence line must carry the recovery run id"
    )
    assert "run id: `111`" in body, "recovery must keep the failure evidence"
    assert updated.get("state") == "closed", "recovery must close the Issue"
    assert updated.get("state_reason") == "completed", (
        "closed with state_reason completed (verified terminal state)"
    )
    assert fingerprint in cfi.extract_fingerprints(body)


def test_recovery_without_matching_issue_is_a_noop(gh_env, monkeypatch, capsys):
    mock = GhMock({
        "/jobs": {"jobs": [make_job(conclusion="success")]},
        "/issues": [],
        "graphql": pr_response(),
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="success", event="pull_request",
                                 head_branch="feature-x", head_sha="a" * 40,
                                 run_id=444), mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 0, out
    assert "ci_failure_issue_recovery_no_match" in out
    assert mock.created_issue() is None
    assert mock.patched_issue() is None


def test_api_failure_fails_fast_with_structured_log(gh_env, monkeypatch, capsys):
    def boom(payload):
        raise GhMock.GhFailure(
            "HTTP 403: Resource not accessible with the requested scope"
        )

    mock = GhMock({"/jobs": boom}).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=111), mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 1, "an API failure must exit non-zero"
    assert "ci_failure_issue_error" in out
    assert "stage=" in out
    assert "403" in out, "the gh stderr must surface in the log"
    assert "Resource not accessible" in out


def test_unknown_conclusion_fails_fast(gh_env, monkeypatch, capsys):
    mock = GhMock({}).install(monkeypatch)
    code = run_triage(monkeypatch, make_event(conclusion="stale"), mock)
    _c = capsys.readouterr()
    out = _c.out + _c.err
    assert code == 1, "an unknown conclusion must fail fast, not be ignored"
    assert "ci_failure_issue_error" in out
    assert mock.calls == [], "no gh call for an unknown conclusion"


def test_issue_body_never_carries_secrets(gh_env, monkeypatch, capsys):
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
        "graphql": pr_response(),
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="pull_request",
                                 head_branch="feature-x", run_id=111), mock)
    assert code == 0
    body = mock.created_issue()["body"]
    for secret in ("GITHUB_TOKEN", "ghp_", "gho_", "secrets."):
        assert secret not in body, f"secret material {secret!r} in body"


def test_fingerprint_is_stable_and_versioned():
    fingerprint = cfi.fingerprint(
        workflow_path=cfi.CI_WORKFLOW_PATH, job_name="tests",
        event="pull_request", target="pull:5", head_sha="a" * 40,
        conclusion="failure",
    )
    assert fingerprint.startswith("v1|"), f"fingerprint: {fingerprint!r}"
    again = cfi.fingerprint(
        workflow_path=cfi.CI_WORKFLOW_PATH, job_name="tests",
        event="pull_request", target="pull:5", head_sha="a" * 40,
        conclusion="failure",
    )
    assert again == fingerprint, "same inputs must give the same fingerprint"
    different = cfi.fingerprint(
        workflow_path=cfi.CI_WORKFLOW_PATH, job_name="tests",
        event="pull_request", target="pull:5", head_sha="b" * 40,
        conclusion="failure",
    )
    assert different != fingerprint


def test_push_to_main_uses_branch_target(gh_env, monkeypatch, capsys):
    mock = GhMock({
        "/jobs": {"jobs": [make_job()]},
        "/issues": [],
    }).install(monkeypatch)
    code = run_triage(monkeypatch,
                      make_event(conclusion="failure", event="push",
                                 head_branch="main", head_sha="a" * 40,
                                 run_id=111), mock)
    assert code == 0, capsys.readouterr().err
    body = mock.created_issue()["body"]
    assert "branch: `main`" in body, f"push runs must name the branch: {body!r}"
    assert "PR #" not in body


# ---------------------------------------------------------------------------
# Direct unit tests for the real gh_api / parsing / edge branches
# ---------------------------------------------------------------------------

class SubprocessMock:
    """Records subprocess.run calls with canned results."""

    def __init__(self, results: list):
        self.results = list(results)
        self.calls: list[tuple[list, dict]] = []

    def install(self, monkeypatch) -> "SubprocessMock":
        def fake_run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return self.results.pop(0)
        monkeypatch.setattr(cfi.subprocess, "run", fake_run)
        return self


def proc(returncode=0, stdout="{}", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout,
                          "stderr": stderr})()


def test_gh_api_builds_the_documented_command(monkeypatch):
    mock = SubprocessMock([
        proc(stdout=json.dumps({"ok": 1})),
        proc(stdout=""),
        proc(stdout=json.dumps([])),
    ]).install(monkeypatch)
    assert cfi.gh_api("repos/o/r/actions/runs/1/jobs") == {"ok": 1}
    cmd, kwargs = mock.calls[0]
    assert cmd == ["gh", "api", "repos/o/r/actions/runs/1/jobs"], cmd
    assert kwargs["input"] is None
    # POST with a JSON body: -X POST + --input - (body on stdin).
    assert cfi.gh_api("repos/o/r/issues", method="POST",
                      payload={"title": "t"}) == {}
    cmd, kwargs = mock.calls[1]
    assert cmd == ["gh", "api", "repos/o/r/issues", "-X", "POST",
                   "--input", "-"], cmd
    assert json.loads(kwargs["input"]) == {"title": "t"}
    # Paginated GET.
    assert cfi.gh_api("repos/o/r/issues?labels=bug,ai-ready&state=open",
                      paginate=True) == []
    cmd, _ = mock.calls[2]
    assert cmd == ["gh", "api",
                   "repos/o/r/issues?labels=bug,ai-ready&state=open",
                   "--paginate"], cmd


def test_gh_api_failure_raises_with_stderr(monkeypatch):
    SubprocessMock([proc(returncode=1, stdout="",
                         stderr="HTTP 403: no scope")]).install(monkeypatch)
    with pytest.raises(cfi.GhApiError) as exc:
        cfi.gh_api("repos/o/r/actions/runs/1/jobs")
    assert exc.value.endpoint == "repos/o/r/actions/runs/1/jobs"
    assert "HTTP 403" in exc.value.detail


def test_gh_api_failure_without_output_uses_exit_code(monkeypatch):
    SubprocessMock([proc(returncode=3, stdout="", stderr="")]).install(
        monkeypatch)
    with pytest.raises(cfi.GhApiError) as exc:
        cfi.gh_api("repos/o/r/actions/runs/1/jobs")
    assert "exit code 3" in exc.value.detail


def test_load_event_failures(monkeypatch, capsys):
    # GITHUB_EVENT_PATH unset.
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(SystemExit) as exc:
        cfi.load_event()
    assert exc.value.code == 1
    out = capsys.readouterr().err
    assert "stage=load_event" in out
    # Missing file.
    monkeypatch.setenv("GITHUB_EVENT_PATH", "/tmp/does-not-exist-ev.json")
    with pytest.raises(SystemExit):
        cfi.load_event()
    # Invalid JSON.
    bad = Path("/tmp/ci-failure-issue-bad.json")
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(bad))
    with pytest.raises(SystemExit):
        cfi.load_event()
    # JSON that is not an object.
    arr = Path("/tmp/ci-failure-issue-arr.json")
    arr.write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(arr))
    with pytest.raises(SystemExit):
        cfi.load_event()


def test_repo_parts_failures(monkeypatch, capsys):
    for value in ("", "noslash", "/repo", "owner/"):
        monkeypatch.setenv("GITHUB_REPOSITORY", value)
        with pytest.raises(SystemExit):
            cfi.repo_parts()
    out = capsys.readouterr().err
    assert "stage=repo_parts" in out
    monkeypatch.setenv("GITHUB_REPOSITORY", "xqliu/muyan-pilot")
    assert cfi.repo_parts() == ("xqliu", "muyan-pilot")


def test_validate_event_missing_workflow_run_object(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        cfi.validate_event({"action": "completed"})
    assert exc.value.code == 1
    out = capsys.readouterr().err
    assert "stage=validate_event" in out


def test_fetch_jobs_rejects_non_list(monkeypatch):
    mock = GhMock({"/jobs": {"unexpected": "shape"}}).install(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cfi.fetch_jobs("xqliu", "muyan-pilot", 111)
    assert exc.value.code == 1
    call = mock.calls[0]
    assert call["endpoint"] == (
        "repos/xqliu/muyan-pilot/actions/runs/111/jobs"
    ), f"documented full path required, got: {call['endpoint']!r}"
    assert call["paginate"] is True


def test_fetch_open_issues_rejects_non_list(monkeypatch):
    mock = GhMock({"/issues": {"unexpected": "shape"}}).install(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cfi.fetch_open_issues("xqliu", "muyan-pilot")
    assert exc.value.code == 1


def test_resolve_pr_number_edge_cases(monkeypatch):
    # nodes is not a list -> fail fast.
    mock = GhMock({"graphql": {"data": {"repository": {
        "pullRequests": {"nodes": "nope"}}}}}).install(monkeypatch)
    with pytest.raises(SystemExit):
        cfi.resolve_pr_number("xqliu", "muyan-pilot", "a" * 40)
    # non-dict nodes and a boolean number are skipped; no match -> None.
    mock = GhMock({"graphql": {"data": {"repository": {
        "pullRequests": {"nodes": [
            "not-a-dict",
            {"number": True, "headRefOid": "a" * 40},
            {"number": 7, "headRefOid": "other"},
        ]}}}}}).install(monkeypatch)
    assert cfi.resolve_pr_number("xqliu", "muyan-pilot", "a" * 40) is None


def test_job_lines_and_failed_steps_edges():
    # Non-dict job entries are skipped; a job without steps reports none.
    lines = cfi.job_lines(["junk", {"name": "j", "conclusion": "failure",
                                   "steps": "nope"}], "failure")
    assert lines, "a failing job without steps still gets an entry"
    assert "failed steps: (none reported by the jobs API)" in "\n".join(lines)
    # Steps: non-dict steps and unnamed steps are skipped.
    steps = [{"name": "good", "conclusion": "timed_out"}, "junk",
             {"conclusion": "cancelled"}, {"name": "ok",
                                           "conclusion": "success"}]
    job = {"name": "j", "conclusion": "failure", "steps": steps}
    assert cfi.failed_steps(job) == ["good"]
    # A job with a different conclusion is not listed.
    assert cfi.job_lines([job], "cancelled") == []


def failure_fingerprint(conclusion="failure") -> str:
    """The fingerprint of the default make_event() failure scene."""
    return cfi.fingerprint(
        workflow_path=cfi.CI_WORKFLOW_PATH, job_name="tests",
        event="pull_request", target="pull:5", head_sha="a" * 40,
        conclusion=conclusion,
    )


def test_triage_failure_no_job_with_conclusion_fails_fast(monkeypatch):
    mock = GhMock({
        "graphql": pr_response(),
    }).install(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cfi.triage_failure(
            make_event()["workflow_run"], "xqliu", "muyan-pilot",
            [{"name": "tests", "conclusion": "success"}], "failure",
        )
    assert exc.value.code == 1


def test_triage_matched_issue_without_number_fails_fast(monkeypatch):
    mock = GhMock({
        "/issues": [{"state": "open",
                     "body": f"{cfi.FINGERPRINT_MARKER} "
                             f"`{failure_fingerprint()}`"}],
        "graphql": pr_response(),
    }).install(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cfi.triage_failure(
            make_event()["workflow_run"], "xqliu", "muyan-pilot",
            [make_job()], "failure",
        )
    assert exc.value.code == 1


def test_triage_recovery_matched_issue_without_number_fails_fast(
        monkeypatch):
    run = make_event(conclusion="success")["workflow_run"]
    # Matched Issue (fingerprint in body) without a number.
    mock = GhMock({
        "/issues": [{"state": "open",
                     "body": f"{cfi.FINGERPRINT_MARKER} "
                             f"`{failure_fingerprint()}`"}],
        "graphql": pr_response(),
    }).install(monkeypatch)
    with pytest.raises(SystemExit):
        cfi.triage_recovery(run, "xqliu", "muyan-pilot",
                            [make_job(conclusion="success")])


def test_main_rejects_non_integer_run_id(gh_env, monkeypatch, capsys):
    event = make_event()
    event["workflow_run"]["id"] = "not-an-int"
    write_event(monkeypatch, event)
    with pytest.raises(SystemExit) as exc:
        cfi.main()
    assert exc.value.code == 1
    out = capsys.readouterr().err
    assert "stage=classify_run" in out


def test_extract_fingerprints_without_backticks(monkeypatch):
    # A marker line without backticks still yields its raw value.
    body = f"{cfi.FINGERPRINT_MARKER} v1|rawvalue"
    assert cfi.extract_fingerprints(body) == ["v1|rawvalue"]
    # Empty marker lines are dropped.
    assert cfi.extract_fingerprints(f"{cfi.FINGERPRINT_MARKER} ``") == []


def test_validate_event_non_target_event_type(monkeypatch, capsys):
    # A completed CI run for a non-target event (e.g. schedule) is ignored.
    with pytest.raises(SystemExit) as exc:
        cfi.validate_event({"workflow_run": {
            "path": cfi.CI_WORKFLOW_PATH, "event": "schedule",
            "status": "completed", "head_branch": "main",
        }})
    assert exc.value.code == 0
    out = capsys.readouterr().err
    assert "reason=not_target_event" in out


def test_job_lines_missing_name_and_url_edges():
    # A failing job without a usable name is skipped entirely.
    assert cfi.job_lines([{"conclusion": "failure"}], "failure") == []
    assert cfi.job_lines([{"name": "", "conclusion": "failure"}],
                         "failure") == []
    # A job without html_url gets no URL line.
    lines = cfi.job_lines([{"name": "j", "conclusion": "failure",
                            "steps": []}], "failure")
    assert not any("job URL" in line for line in lines)
    # Explicit steps (the make_job default is not used).
    job = make_job(steps=[{"name": "only", "conclusion": "timed_out"}])
    assert cfi.failed_steps(job) == ["only"]


def test_gh_mock_unmatched_endpoint_answers_empty(monkeypatch):
    mock = GhMock({}).install(monkeypatch)
    assert cfi.gh_api("some/unmatched/endpoint") == {}
    assert mock.calls and mock.calls[0]["response"] == {}


def test_build_body_run_without_url_and_duplicate_evidence():
    run = make_event()["workflow_run"]
    del run["html_url"]
    fingerprints = {"tests": "v1|x"}
    body = cfi.build_body(run, None, [], "failure", fingerprints, None)
    assert not any(line.startswith("- run URL:") for line in body.splitlines()), (
        "no html_url -> no run URL line"
    )
    # Rebuilding with the same run must not duplicate the evidence line.
    again = cfi.build_body(run, None, [], "failure", fingerprints, body)
    evidence = [line for line in again.splitlines()
                if line.startswith(f"- {cfi.RUN_EVIDENCE_MARKER}")]
    assert len(evidence) == 1, f"evidence duplicated: {evidence}"


def test_build_recovery_body_no_history_header_and_existing_line():
    run = make_event(conclusion="success")["workflow_run"]
    # No '### Run history' header: the line goes to the end.
    body = cfi.build_recovery_body(run, "plain body\n")
    last = body.rstrip().splitlines()[-1]
    assert last.startswith(f"- {cfi.RECOVERY_MARKER}"), last
    # A second recovery for the same run must not duplicate the line.
    again = cfi.build_recovery_body(run, body)
    assert again == body


def test_find_matching_issue_skips_non_matching_shapes(monkeypatch):
    good_body = f"{cfi.FINGERPRINT_MARKER} `v1|good`"
    issues = [
        "not-a-dict",
        {"state": "closed", "body": good_body},
        {"state": "open", "number": 2},  # no body
        {"state": "open", "number": 3, "body": "no fingerprint here"},
        {"state": "open", "number": 4, "body": good_body},
    ]
    match = cfi.find_matching_issue(issues, {"v1|good"})
    assert match is not None and match["number"] == 4
    # No fingerprint in the set -> no match at all.
    assert cfi.find_matching_issue(issues, {"v1|other"}) is None


def test_triage_failure_skips_non_dict_and_unnamed_jobs(monkeypatch):
    # Non-dict job entries and unnamed jobs are skipped; the named failing
    # job still drives the fingerprint and the create.
    mock = GhMock({
        "/issues": [],
        "graphql": pr_response(),
    }).install(monkeypatch)
    run = make_event()["workflow_run"]
    jobs = ["junk", {"conclusion": "failure"}, {"name": "tests",
               "conclusion": "failure"}]
    cfi.triage_failure(run, "xqliu", "muyan-pilot", jobs, "failure")
    created = mock.created_issue()
    assert created is not None
    assert cfi.extract_fingerprints(created["body"]) == [
        failure_fingerprint(),
    ]


def test_triage_recovery_skips_non_dict_and_unnamed_jobs(monkeypatch):
    # With only unnameable jobs there is no fingerprint -> no match.
    mock = GhMock({
        "/issues": [{"state": "open", "number": 4,
                     "body": f"{cfi.FINGERPRINT_MARKER} `{failure_fingerprint()}`"}],
        "graphql": pr_response(),
    }).install(monkeypatch)
    run = make_event(conclusion="success")["workflow_run"]
    cfi.triage_recovery(run, "xqliu", "muyan-pilot",
                        ["junk", {"conclusion": "success"}])
    assert mock.patched_issue() is None, (
        "without a job name the scenario cannot be identified"
    )


def test_script_entry_point_ignores_non_ci_event(tmp_path):
    # Run the real script as __main__ in a subprocess: the entry point
    # calls main(), and a non-CI event is a clean no-op (exit 0).
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps(make_event(path=".github/workflows/other.yml")),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "ci_failure_issue.py")],
        env={**os.environ, "GITHUB_EVENT_PATH": str(event_file)},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ci_failure_issue_ignored" in proc.stderr


def test_module_entry_point_block(tmp_path, monkeypatch, capsys):
    # Execute the file under the name `__main__` so the
    # `if __name__ == "__main__"` entry point block runs in-process.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "__main__", REPO_ROOT / "ci_failure_issue.py")
    module = importlib.util.module_from_spec(spec)
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps(make_event(path=".github/workflows/other.yml")),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    try:
        spec.loader.exec_module(module)
    except SystemExit as exc:
        assert int(exc.code or 0) == 0, "non-CI event must exit 0"
    out = capsys.readouterr().err
    assert "ci_failure_issue_ignored" in out


def test_readme_documents_failure_issue_loop():
    readme = README_FILE.read_text(encoding="utf-8")
    assert "ci-failure-issue" in readme, (
        "README must document the triage workflow"
    )
    assert "workflow_run" in readme, (
        "README must explain the workflow_run trigger"
    )
    assert "ai-ready" in readme and "bug" in readme, (
        "README must name the auto Issue labels"
    )
    assert "issues: write" in readme, (
        "README must document the declared permissions"
    )
