"""Behavioral tests for the CI failure triage (Issue #106).

The triage script (`ci_failure_triage.py`) runs from the `CI Failure Issue`
workflow (`.github/workflows/ci-failure-issue.yml`, `on: workflow_run` of the
CI workflow). These tests drive `main()` end to end with a REAL event payload
file (the `workflow_run` object shape verified against the live API: run
33946530825 — `id, name, path, event, head_branch, head_sha, status,
conclusion, html_url, run_started_at, run_attempt`) and a mocked `gh api`
layer, asserting the real GitHub REST contract:

- failure/cancelled/timed_out on a `pull_request` or `push` to `main` →
  exactly one `bug` + `ai-ready` Issue per failed job, full evidence body,
  hidden fingerprint marker;
- the same fingerprint again → a re-occurrence comment on the existing Issue,
  never a second Issue (no storm);
- `success` → the matching Issue gets the recovery run evidence and is closed
  (`state_reason: completed`);
- every other conclusion/event/target is a logged no-op (exit 0);
- every environment/API failure is a structured error + exit 1 (fail fast,
  never a fake success).
"""
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ci_failure_triage.py"

OWNER_REPO = "orbi-run/test-repo"
HEAD_SHA = "abc123def456abc123def456abc123de"
RUN_URL = "https://github.com/orbi-run/test-repo/actions/runs/42"
JOB_URL = "https://github.com/orbi-run/test-repo/actions/runs/42/job/9"
TRIGGERED_AT = "2026-09-05T05:12:11Z"


def load_module():
    spec = importlib.util.spec_from_file_location("ci_failure_triage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_module()


class FakeGh:
    """The mocked `gh_api` layer: records every call, answers from an
    exact-endpoint route table, raises AssertionError on an unexpected call
    (the tests assert the exact REST endpoints, not a shape the code guessed).
    """

    def __init__(self, routes=None, error=None):
        self.calls: list[dict] = []
        self.routes = routes or {}
        # error: (endpoint_prefix, detail) — every call whose endpoint starts
        # with the prefix raises GhApiError (the API-failure paths).
        self.error = error

    def __call__(self, endpoint, *, method="GET", payload=None):
        self.calls.append(
            {"endpoint": endpoint, "method": method, "payload": payload}
        )
        if self.error is not None and endpoint.startswith(self.error[0]):
            raise mod.GhApiError(endpoint, self.error[1])
        if endpoint not in self.routes:
            if method in ("POST", "PATCH"):
                return {}  # write responses are unused; the calls are asserted
            raise AssertionError(f"unexpected gh api {method} {endpoint}")
        return self.routes[endpoint]

    def calls_to(self, endpoint, method):
        return [
            call for call in self.calls
            if call["endpoint"] == endpoint and call["method"] == method
        ]


def test_fake_gh_rejects_unrouted_get_calls():
    fake = FakeGh()
    with pytest.raises(AssertionError, match="unexpected gh api GET repos/x"):
        fake("repos/x")


def ep_jobs(run_id=42):
    return f"repos/{OWNER_REPO}/actions/runs/{run_id}/jobs?per_page=100"


def ep_pulls(sha=HEAD_SHA):
    return f"repos/{OWNER_REPO}/commits/{sha}/pulls"


def ep_issues_list():
    return f"repos/{OWNER_REPO}/issues?labels=bug,ai-ready&state=open&per_page=100"


def ep_create():
    return f"repos/{OWNER_REPO}/issues"


def ep_comment(number):
    return f"repos/{OWNER_REPO}/issues/{number}/comments"


def ep_patch(number):
    return f"repos/{OWNER_REPO}/issues/{number}"


def run_event(**over) -> dict:
    """A completed `workflow_run` event payload (real API field shape)."""
    run = {
        "id": 42,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "failure",
        "html_url": RUN_URL,
        "run_started_at": TRIGGERED_AT,
        "run_attempt": 1,
    }
    run.update(over)
    return {"workflow_run": run}


def job(name="tests", conclusion="failure", steps=None) -> dict:
    if steps is None:
        steps = [
            {"name": "Set up job", "conclusion": "success"},
            {
                "name": "Run the full test suite",
                "conclusion": "failure" if conclusion == "failure" else "success",
            },
        ]
    return {
        "name": name,
        "conclusion": conclusion,
        "html_url": JOB_URL,
        "steps": steps,
    }


def marker(fingerprint: str) -> str:
    return f"<!-- {mod.FINGERPRINT_MARKER}{fingerprint} -->"


def triage_issue(number: int, fingerprints: list[str], state="open") -> dict:
    """An Issue as the REST list endpoint returns it (auto-created shape)."""
    body = "CI failure body\n" + "\n".join(marker(fp) for fp in fingerprints)
    return {"number": number, "state": state, "body": body, "title": "CI failure"}


def write_event(monkeypatch, tmp_path, payload):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))


@pytest.fixture
def gh(monkeypatch):
    """Default environment; the returned FakeGh is installed as mod.gh_api."""
    fake = FakeGh()
    monkeypatch.setattr(mod, "gh_api", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", OWNER_REPO)
    return fake


# ---------------------------------------------------------------------------
# Fingerprint (the stable dedup key)
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_sha256_hex():
    first = mod.fingerprint("push", "main", "tests")
    second = mod.fingerprint("push", "main", "tests")
    assert first == second
    assert len(first) == 64
    int(first, 16)  # hex


def test_fingerprint_varies_by_job_branch_and_event():
    base = mod.fingerprint("push", "main", "tests")
    assert mod.fingerprint("push", "main", "docs") != base
    assert mod.fingerprint("push", "release", "tests") != base
    assert mod.fingerprint("pull_request", "main", "tests") != base


def test_issue_fingerprints_extracts_hidden_markers_in_order():
    fp_a = mod.fingerprint("push", "main", "tests")
    fp_b = mod.fingerprint("push", "main", "docs")
    body = f"intro\n{marker(fp_a)}\nmiddle\n{marker(fp_b)}\n{marker(fp_a)}\n"
    assert mod.issue_fingerprints(body) == [fp_a, fp_b]


def test_issue_fingerprints_empty_without_marker():
    assert mod.issue_fingerprints("just text with ci-failure-fingerprint: nope") == []


# ---------------------------------------------------------------------------
# Classification (triage_scope) — the explicit event/conclusion rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_scope_failure_conclusions_are_actionable(conclusion):
    mode, _ = mod.triage_scope(run_event(conclusion=conclusion)["workflow_run"])
    assert mode == "failure"


def test_scope_success_is_recovery():
    mode, _ = mod.triage_scope(run_event(conclusion="success")["workflow_run"])
    assert mode == "recovery"


@pytest.mark.parametrize("path", [
    ".github/workflows/ci-failure-issue.yml",  # the triage workflow itself
    ".github/workflows/other.yml",
])
def test_scope_ignores_every_non_ci_workflow_path(path):
    """Anti-recursion: only the CI workflow path is actionable, so Issue
    create/update/close (which emit no CI workflow_run) can never re-trigger
    the triage."""
    mode, reason = mod.triage_scope(run_event(path=path)["workflow_run"])
    assert mode == "ignore"
    assert "not_ci_workflow" in reason


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "release"])
def test_scope_ignores_non_target_events(event):
    mode, reason = mod.triage_scope(run_event(event=event)["workflow_run"])
    assert mode == "ignore"
    assert "not_target_event" in reason


def test_scope_ignores_push_to_non_main_branches():
    mode, reason = mod.triage_scope(
        run_event(event="push", head_branch="feature")["workflow_run"]
    )
    assert mode == "ignore"
    assert "push_not_main" in reason


@pytest.mark.parametrize("conclusion", ["skipped", "startup_failure", "neutral", None])
def test_scope_ignores_other_conclusions(conclusion):
    mode, reason = mod.triage_scope(
        run_event(conclusion=conclusion)["workflow_run"]
    )
    assert mode == "ignore"
    assert "conclusion_not_actionable" in reason


# ---------------------------------------------------------------------------
# main() — fail-fast environment handling
# ---------------------------------------------------------------------------


def test_missing_event_path_fails_fast(gh, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]"])
def test_malformed_event_payload_fails_fast(gh, monkeypatch, tmp_path, raw):
    path = tmp_path / "event.json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_event_without_workflow_run_object_fails_fast(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, {"noop": True})
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_invalid_repository_fails_fast(monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event())
    monkeypatch.setenv("GITHUB_REPOSITORY", "not-a-repo-pair")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_run_without_integer_id_fails_fast(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event(id="42"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_api_error_fails_fast_with_structured_error(gh, monkeypatch, tmp_path, capsys):
    write_event(monkeypatch, tmp_path, run_event())
    gh.error = (ep_jobs(), "HTTP 403 (rate limited)")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ci_triage error stage=gh_api" in err
    assert ep_jobs() in err
    assert "HTTP 403 (rate limited)" in err


# ---------------------------------------------------------------------------
# main() — ignored events are clean no-ops (exit 0, no API calls)
# ---------------------------------------------------------------------------


def test_ignored_event_exits_zero_without_api_calls(gh, monkeypatch, tmp_path, capsys):
    write_event(monkeypatch, tmp_path, run_event(event="workflow_dispatch"))
    mod.main()
    assert gh.calls == []
    assert "ci_triage ignored" in capsys.readouterr().err


def test_failure_run_without_failed_jobs_is_a_logged_noop(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job(conclusion="success")]}
    gh.routes[ep_issues_list()] = []
    mod.main()
    assert gh.calls_to(ep_create(), "POST") == []
    assert gh.calls_to(ep_comment(1), "POST") == []


def test_recovery_without_succeeded_jobs_is_a_logged_noop(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event(conclusion="success"))
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job(conclusion="skipped")]}
    gh.routes[ep_issues_list()] = []
    mod.main()
    assert gh.calls == [call for call in gh.calls if call["endpoint"] == ep_jobs()]


# ---------------------------------------------------------------------------
# Failure flow: create (one bug + ai-ready Issue per failed job)
# ---------------------------------------------------------------------------


def test_failure_creates_one_issue_with_full_evidence(gh, monkeypatch, tmp_path, capsys):
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = {
        "total_count": 2,
        "jobs": [job(), job(name="docs", conclusion="success")],
    }
    gh.routes[ep_issues_list()] = []
    mod.main()
    creates = gh.calls_to(ep_create(), "POST")
    assert len(creates) == 1
    payload = creates[0]["payload"]
    assert payload["labels"] == ["bug", "ai-ready"]
    assert payload["title"] == "CI failure: tests on branch main (push)"
    body = payload["body"]
    # The full evidence contract: workflow, job, event, branch, commit SHA,
    # run id, run URL, failed steps and trigger time.
    assert "## CI failure: tests" in body
    assert f"- workflow: `CI` (`.github/workflows/ci.yml`)" in body
    assert "- event: `push`" in body
    assert "- branch/PR: branch `main`" in body
    assert f"- commit: `{HEAD_SHA}`" in body
    assert f"- run id: `42` (attempt 1)" in body
    assert f"- run URL: {RUN_URL}" in body
    assert f"- triggered at: `{TRIGGERED_AT}`" in body
    assert "- run conclusion: `failure`" in body
    assert "### Failed job" in body
    assert f"- job `tests` — conclusion: `failure` — [job logs]({JOB_URL})" in body
    assert "- failed steps: `Run the full test suite`" in body
    assert marker(mod.fingerprint("push", "main", "tests")) in body
    # A push run must not spend the PR-resolution call.
    assert gh.calls_to(ep_pulls(), "GET") == []
    assert "ci_triage created" in capsys.readouterr().err


def test_failure_resolves_the_pr_number_for_pull_request_runs(
    gh, monkeypatch, tmp_path
):
    write_event(
        monkeypatch, tmp_path,
        run_event(event="pull_request", head_branch="feature"),
    )
    gh.routes[ep_pulls()] = [{"number": 328, "state": "open", "title": "Fix"}]
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = []
    mod.main()
    assert len(gh.calls_to(ep_pulls(), "GET")) == 1
    create = gh.calls_to(ep_create(), "POST")[0]["payload"]
    assert create["title"] == "CI failure: tests on PR #328 (pull_request)"
    assert create["body"].startswith("## CI failure: tests")
    assert "- branch/PR: PR #328 (head branch `feature`)" in create["body"]


def test_failure_without_a_resolved_pr_falls_back_to_the_head_branch(
    gh, monkeypatch, tmp_path
):
    write_event(
        monkeypatch, tmp_path,
        run_event(event="pull_request", head_branch="feature"),
    )
    gh.routes[ep_pulls()] = []
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = []
    mod.main()
    create = gh.calls_to(ep_create(), "POST")[0]["payload"]
    assert create["title"] == "CI failure: tests on branch feature (pull_request)"
    assert "- branch/PR: head branch `feature` (no PR resolved)" in create["body"]


def test_two_different_job_failures_create_two_independent_issues(
    gh, monkeypatch, tmp_path
):
    """Acceptance: two different failures create two independent Issues."""
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = {
        "total_count": 2,
        "jobs": [job(), job(name="docs", conclusion="timed_out")],
    }
    gh.routes[ep_issues_list()] = []
    mod.main()
    creates = gh.calls_to(ep_create(), "POST")
    assert len(creates) == 2
    titles = [call["payload"]["title"] for call in creates]
    assert titles == [
        "CI failure: docs on branch main (push)",
        "CI failure: tests on branch main (push)",
    ]
    bodies = [call["payload"]["body"] for call in creates]
    assert marker(mod.fingerprint("push", "main", "docs")) in bodies[0]
    assert marker(mod.fingerprint("push", "main", "tests")) in bodies[1]


# ---------------------------------------------------------------------------
# Failure flow: update (same fingerprint → comment, never a second Issue)
# ---------------------------------------------------------------------------


def test_same_failure_updates_the_existing_issue(gh, monkeypatch, tmp_path, capsys):
    """Acceptance: a re-run of the same failure only updates the original
    Issue instead of creating a storm."""
    write_event(monkeypatch, tmp_path, run_event(run_attempt=2))
    fp = mod.fingerprint("push", "main", "tests")
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = [triage_issue(7, [fp])]
    mod.main()
    assert gh.calls_to(ep_create(), "POST") == []
    comments = gh.calls_to(ep_comment(7), "POST")
    assert len(comments) == 1
    body = comments[0]["payload"]["body"]
    assert "CI failure re-occurred" in body
    assert f"- run id: `42` (attempt 2), conclusion: `failure`" in body
    assert f"- run URL: {RUN_URL}" in body
    assert f"- commit: `{HEAD_SHA}`" in body
    assert "- failed steps: `Run the full test suite`" in body
    assert "ci_triage updated issue=7" in capsys.readouterr().err


def test_cancelled_run_updates_the_existing_issue_with_the_run_evidence(
    gh, monkeypatch, tmp_path
):
    write_event(monkeypatch, tmp_path, run_event(conclusion="cancelled"))
    fp = mod.fingerprint("push", "main", "tests")
    gh.routes[ep_jobs()] = {
        "total_count": 1,
        "jobs": [job(conclusion="cancelled", steps=[
            {"name": "Set up job", "conclusion": "success"},
            {"name": "Run the full test suite", "conclusion": "cancelled"},
        ])],
    }
    gh.routes[ep_issues_list()] = [triage_issue(7, [fp])]
    mod.main()
    body = gh.calls_to(ep_comment(7), "POST")[0]["payload"]["body"]
    assert "conclusion: `cancelled`" in body
    # A cancelled step is not a failed step: the explicit rule reports none.
    assert "- failed steps: (none reported by the jobs API)" in body


# ---------------------------------------------------------------------------
# Failure flow: only auto-created open Issues are matches
# ---------------------------------------------------------------------------


def test_pr_entries_in_the_issues_response_are_never_matches(
    gh, monkeypatch, tmp_path
):
    """The issues endpoint also returns PRs; they must never be triaged."""
    write_event(monkeypatch, tmp_path, run_event())
    fp = mod.fingerprint("push", "main", "tests")
    pr = dict(triage_issue(99, [fp]), pull_request={"html_url": "pr"})
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = [pr]
    mod.main()
    # The PR entry must not swallow the match: the failure creates its Issue.
    assert len(gh.calls_to(ep_create(), "POST")) == 1


def test_malformed_issue_entries_are_skipped(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event())
    fp = mod.fingerprint("push", "main", "tests")
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = [
        "not-a-dict",
        {"number": "7", "body": marker(fp)},  # non-integer number
        {"number": 8, "body": None},  # null body (GitHub does this)
        triage_issue(7, [fp]),
    ]
    mod.main()
    # The well-formed Issue 7 is the match: comment, never create.
    assert len(gh.calls_to(ep_create(), "POST")) == 0
    assert len(gh.calls_to(ep_comment(7), "POST")) == 1


def test_malformed_job_entries_are_never_actionable(
    gh, monkeypatch, tmp_path, capsys
):
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = {
        "total_count": 4,
        "jobs": [
            "junk",                                # not a dict
            {"conclusion": "failure"},             # no name
            {"name": "", "conclusion": "failure"},  # empty name
            job(conclusion="success"),             # not a failing job
        ],
    }
    gh.routes[ep_issues_list()] = []
    mod.main()
    assert gh.calls_to(ep_create(), "POST") == []
    assert "ci_triage ignored reason=no_failed_jobs" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Recovery flow: success closes the matching Issue with run evidence
# ---------------------------------------------------------------------------


def test_recovery_closes_the_matching_issue_with_run_evidence(
    gh, monkeypatch, tmp_path, capsys
):
    """Acceptance: after CI recovers, the matching bug Issue is associated
    with the successful run and closed."""
    write_event(monkeypatch, tmp_path, run_event(conclusion="success", id=43))
    fp = mod.fingerprint("push", "main", "tests")
    gh.routes[ep_jobs(43)] = {
        "total_count": 1, "jobs": [job(conclusion="success")],
    }
    gh.routes[ep_issues_list()] = [triage_issue(7, [fp])]
    mod.main()
    comments = gh.calls_to(ep_comment(7), "POST")
    assert len(comments) == 1
    body = comments[0]["payload"]["body"]
    assert "CI recovered" in body
    assert f"- run id: `43` (attempt 1), conclusion: `success`" in body
    assert f"- run URL: {RUN_URL}" in body
    assert f"- commit: `{HEAD_SHA}`" in body
    patches = gh.calls_to(ep_patch(7), "PATCH")
    assert len(patches) == 1
    assert patches[0]["payload"] == {"state": "closed", "state_reason": "completed"}
    assert "ci_triage recovered issue=7" in capsys.readouterr().err


def test_recovery_without_a_matching_issue_is_a_logged_noop(
    gh, monkeypatch, tmp_path
):
    write_event(monkeypatch, tmp_path, run_event(conclusion="success", id=43))
    gh.routes[ep_jobs(43)] = {
        "total_count": 1, "jobs": [job(conclusion="success")],
    }
    gh.routes[ep_issues_list()] = []
    mod.main()
    assert gh.calls_to(ep_patch(7), "PATCH") == []
    assert gh.calls_to(ep_comment(7), "POST") == []


def test_recovery_only_considers_succeeded_jobs(gh, monkeypatch, tmp_path):
    """A skipped (never-run) job must not close its Issue."""
    write_event(monkeypatch, tmp_path, run_event(conclusion="success", id=43))
    fp = mod.fingerprint("push", "main", "tests")
    gh.routes[ep_jobs(43)] = {
        "total_count": 2,
        "jobs": [job(name="docs", conclusion="success"), job(conclusion="skipped")],
    }
    gh.routes[ep_issues_list()] = [triage_issue(7, [fp])]
    mod.main()
    assert gh.calls_to(ep_patch(7), "PATCH") == []
    assert gh.calls_to(ep_comment(7), "POST") == []


# ---------------------------------------------------------------------------
# The real `gh_api` layer (pinned against `gh api --help`, gh 2.97)
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gh_api_get_parses_the_json_response(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProc(stdout='{"ok": true}\n')

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.gh_api("repos/o/r/x") == {"ok": True}
    assert captured["command"] == ["gh", "api", "repos/o/r/x"]
    assert captured["kwargs"]["input"] is None


def test_gh_api_post_sends_the_payload_via_stdin(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProc(stdout='{"id": 1}')

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    payload = {"title": "t", "body": "b"}
    assert mod.gh_api("repos/o/r/issues", method="POST", payload=payload) == {"id": 1}
    assert captured["command"] == [
        "gh", "api", "repos/o/r/issues", "-X", "POST", "--input", "-",
    ]
    assert captured["kwargs"]["input"] == json.dumps(payload)


@pytest.mark.parametrize("stdout,stderr,expected", [
    ("", "HTTP 403: forbidden\n", "HTTP 403: forbidden"),
    ("server said no", "", "server said no"),  # gh writes some errors to stdout
    ("", "", "exit code 1"),
])
def test_gh_api_nonzero_exit_raises_a_structured_error(
    monkeypatch, stdout, stderr, expected
):
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda command, **kwargs: FakeProc(returncode=1, stdout=stdout, stderr=stderr),
    )
    with pytest.raises(mod.GhApiError) as exc:
        mod.gh_api("repos/o/r/x")
    assert exc.value.endpoint == "repos/o/r/x"
    assert exc.value.detail == expected
    assert "repos/o/r/x" in str(exc.value)


def test_gh_api_empty_success_body_is_an_empty_dict(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess, "run", lambda command, **kwargs: FakeProc(stdout="")
    )
    assert mod.gh_api("repos/o/r/x") == {}


def test_gh_api_non_json_success_body_is_a_structured_error(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess, "run", lambda command, **kwargs: FakeProc(stdout="<html>")
    )
    with pytest.raises(mod.GhApiError) as exc:
        mod.gh_api("repos/o/r/x")
    assert "non-JSON response" in exc.value.detail


# ---------------------------------------------------------------------------
# API response validation (a malformed response fails fast, never guesses)
# ---------------------------------------------------------------------------


def test_jobs_response_must_be_an_object_with_a_jobs_list(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = ["not-an-object"]
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    gh.routes[ep_jobs()] = {"total_count": 1}  # object without a jobs list
    with pytest.raises(SystemExit):
        mod.main()


def test_issues_response_must_be_a_list(gh, monkeypatch, tmp_path):
    write_event(monkeypatch, tmp_path, run_event())
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = {"unexpected": "object"}
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_pulls_response_must_be_a_list(gh, monkeypatch, tmp_path):
    write_event(
        monkeypatch, tmp_path,
        run_event(event="pull_request", head_branch="feature"),
    )
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_pulls()] = {"unexpected": "object"}
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def test_pulls_malformed_entries_are_skipped(gh, monkeypatch, tmp_path):
    """A commit without a resolvable PR falls back to the branch display."""
    write_event(
        monkeypatch, tmp_path,
        run_event(event="pull_request", head_branch="feature"),
    )
    gh.routes[ep_pulls()] = ["junk", {"number": "7"}]
    gh.routes[ep_jobs()] = {"total_count": 1, "jobs": [job()]}
    gh.routes[ep_issues_list()] = []
    mod.main()
    create = gh.calls_to(ep_create(), "POST")[0]["payload"]
    assert create["title"] == "CI failure: tests on branch feature (pull_request)"


def test_failed_steps_reports_only_failure_and_timeout_steps():
    steps = [
        "junk",                                    # not a dict
        {"conclusion": "failure"},                 # no name
        {"name": "Set up job", "conclusion": "success"},
        {"name": "Run tests", "conclusion": "failure"},
        {"name": "Validate docs", "conclusion": "timed_out"},
        {"name": "Cancelled step", "conclusion": "cancelled"},
    ]
    assert mod.failed_steps({"name": "tests", "steps": steps}) == [
        "Run tests", "Validate docs",
    ]
    assert mod.failed_steps({"name": "tests"}) == []  # no steps key


def test_unreadable_event_file_fails_fast(gh, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
