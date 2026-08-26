"""CI failure triage (Issue #106).

Runs from the `CI Failure Issue` workflow (`.github/workflows/
ci-failure-issue.yml`), which is triggered by `on: workflow_run` of the CI
workflow (`.github/workflows/ci.yml`) with `types: [completed]`.

Behavior:

- The CI run finished `failure`, `cancelled` or `timed_out` on a
  `pull_request` or `push` to `main`: create a `bug` + `ai-ready` Issue
  (Muyan Pilot picks it up via its ready scan) with the full evidence
  (workflow, job, event, branch/PR, commit SHA, run id, run URL, failed
  steps, trigger time). A re-run of the SAME failure (same fingerprint)
  updates the existing Issue instead of creating a new one.
- The CI run finished `success` for the same scenario: the recovery
  fingerprints (the scenario under every failure conclusion) are matched
  against open Issues; a match gets the recovery run evidence appended
  and is closed with `state_reason: completed`.
- Anything else (other workflow, other event, push to another branch,
  run not completed yet) is logged as `ci_failure_issue_ignored` and the
  script exits 0 — no action, no error.

Fail fast: every `gh api` failure, an unknown conclusion, or a malformed
event payload logs `ci_failure_issue_error stage=… error=…` and exits
non-zero, so the triage job goes red instead of faking success.

Dedup fingerprint (stable across re-runs of the same failure):

    v1|<workflow path>|<job name>|<event>|<target>|<head sha>|<conclusion>

`target` is `pull:<number>` for pull_request runs (the PR number resolved
from the head SHA) and `branch:<name>` for push runs. The run id is NOT in
the fingerprint: re-running the same commit must update the existing
Issue, not create a storm. Run ids are recorded in the Issue body as
evidence lines instead. Recovery matches the scenario under every failure
conclusion, since the Issue was created with the failing conclusion.

Anti-recursion: the triage workflow only reacts to `workflow_run` of the
CI workflow. Creating or updating an Issue emits no `pull_request`/`push`
event, so it can never re-trigger this workflow.

No secrets ever leave the runner: the Issue body is built only from the
event payload and the jobs API response.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import NoReturn

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_WORKFLOW_NAME = "CI"
TARGET_EVENTS = ("pull_request", "push")
PUSH_TARGET_BRANCH = "main"
FAILURE_CONCLUSIONS = ("failure", "cancelled", "timed_out")
SUCCESS_CONCLUSION = "success"
ISSUE_LABELS = ("bug", "ai-ready")
FINGERPRINT_VERSION = "v1"
FINGERPRINT_MARKER = "muyan-ci-failure-fingerprint:"
RUN_EVIDENCE_MARKER = "muyan-ci-failure-run:"
RECOVERY_MARKER = "muyan-ci-failure-recovered:"


class GhApiError(Exception):
    """A `gh api` call failed (non-zero exit code)."""

    def __init__(self, endpoint: str, detail: str):
        super().__init__(f"gh api {endpoint} failed: {detail}")
        self.endpoint = endpoint
        self.detail = detail


def log(message: str) -> None:
    """One structured line to stderr (the job log is the record)."""
    print(f"ci_failure_issue {message}", file=sys.stderr, flush=True)


def fail(stage: str, error: str) -> NoReturn:
    """Fail fast with a structured error; the triage job goes red."""
    log(f"ci_failure_issue_error stage={stage} error={error}")
    raise SystemExit(1)


def gh_api(endpoint: str, *, method: str = "GET", payload: dict | None = None,
           paginate: bool = False):
    """Call `gh api` (auth via GITHUB_TOKEN) and parse the JSON response.

    Verified against the gh CLI (2.97): `--input -` reads the request
    body from stdin, `-X` overrides the method, `--paginate` fetches all
    pages. A non-zero exit raises GhApiError with the gh stderr.
    """
    cmd = ["gh", "api", endpoint]
    if method != "GET":
        cmd += ["-X", method]
    if paginate:
        cmd.append("--paginate")
    stdin_text: str | None = None
    if payload is not None:
        cmd += ["--input", "-"]
        stdin_text = json.dumps(payload)
    proc = subprocess.run(
        cmd, input=stdin_text, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GhApiError(endpoint, detail or f"exit code {proc.returncode}")
    stdout = proc.stdout.strip()
    return json.loads(stdout) if stdout else {}


def load_event() -> dict:
    """Read the workflow_run event payload from GITHUB_EVENT_PATH."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        fail("load_event", "GITHUB_EVENT_PATH is not set")
    try:
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        fail("load_event", f"cannot read event payload {path}: {exc}")
    if not isinstance(event, dict):
        fail("load_event", "event payload is not a JSON object")
    return event


def validate_event(event: dict) -> dict:
    """Return the observed CI run, or exit 0 when the event is not ours.

    Only a COMPLETED run of the CI workflow on a target event is
    actionable; everything else is a no-op (logged, exit 0).
    """
    run = event.get("workflow_run")
    if not isinstance(run, dict):
        fail("validate_event", "event has no workflow_run object")
    path = run.get("path")
    if path != CI_WORKFLOW_PATH:
        log(f"ci_failure_issue_ignored reason=not_ci_workflow path={path}")
        raise SystemExit(0)
    event_name = run.get("event")
    if event_name not in TARGET_EVENTS:
        log(f"ci_failure_issue_ignored reason=not_target_event event={event_name}")
        raise SystemExit(0)
    if event_name == "push" and run.get("head_branch") != PUSH_TARGET_BRANCH:
        log(
            "ci_failure_issue_ignored reason=push_not_main "
            f"branch={run.get('head_branch')}"
        )
        raise SystemExit(0)
    if run.get("status") != "completed":
        log(f"ci_failure_issue_ignored reason=not_completed status={run.get('status')}")
        raise SystemExit(0)
    return run


def repo_parts() -> tuple[str, str]:
    """(owner, repo) from GITHUB_REPOSITORY (e.g. `xqliu/muyan-pilot`)."""
    value = os.environ.get("GITHUB_REPOSITORY", "")
    owner, sep, repo = value.partition("/")
    if not sep or not owner or not repo:
        fail("repo_parts", f"GITHUB_REPOSITORY is not owner/repo: {value!r}")
    return owner, repo


def fetch_jobs(owner: str, repo: str, run_id: int) -> list[dict]:
    """The jobs of the COMPLETED attempt (default `filter=latest`).

    The endpoint is the documented full path
    `repos/{owner}/{repo}/actions/runs/{run_id}/jobs` (verified against
    the OpenAPI spec and a real call — the bare `actions/...` form is
    not a valid API path).
    """
    data = gh_api(
        f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs", paginate=True,
    )
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        fail("fetch_jobs", f"jobs API did not return a jobs list: {data!r}")
    return jobs


def resolve_pr_number(owner: str, repo: str, head_sha: str) -> int | None:
    """The OPEN PR whose head is `head_sha` (None when there is none)."""
    query = (
        "query { repository(owner: $owner, name: $repo) { "
        "pullRequests(states: [OPEN], last: 10) { nodes { "
        "number headRefOid state } } } }"
    )
    data = gh_api(
        "graphql",
        payload={
            "query": query,
            "variables": {"owner": owner, "repo": repo},
        },
    )
    nodes = (
        data.get("data", {}).get("repository", {})
        .get("pullRequests", {}).get("nodes", [])
    )
    if not isinstance(nodes, list):
        fail("resolve_pr_number", f"GraphQL did not return nodes: {data!r}")
    for node in nodes:
        if isinstance(node, dict) and node.get("headRefOid") == head_sha:
            number = node.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                return number
    return None


def fingerprint(*, workflow_path: str, job_name: str, event: str,
                target: str, head_sha: str, conclusion: str) -> str:
    """Stable failure fingerprint (see module docstring)."""
    raw = "|".join([
        FINGERPRINT_VERSION, workflow_path, job_name, event, target,
        head_sha, conclusion,
    ])
    return f"{FINGERPRINT_VERSION}|{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def extract_fingerprints(body: str) -> list[str]:
    """All fingerprint marker values in an Issue body (order preserved,
    deduplicated, empty when the body carries none)."""
    values: list[str] = []
    for line in body.splitlines():
        line = line.strip().lstrip("-").strip()
        if line.startswith(FINGERPRINT_MARKER):
            value = line[len(FINGERPRINT_MARKER):].strip()
            # The marker line is ``<fingerprint>`` (job ``<name>``) — keep
            # only the backtick-quoted fingerprint itself.
            value = value.split("`")[1] if value.count("`") >= 2 else value
            if value and value not in values:
                values.append(value)
    return values


def run_evidence_lines(body: str) -> list[str]:
    """Existing run evidence contents (marker line without the list
    prefix), in order, deduplicated."""
    seen: list[str] = []
    for line in body.splitlines():
        line = line.strip().lstrip("-").strip()
        if line.startswith(RUN_EVIDENCE_MARKER) and line not in seen:
            seen.append(line)
    return seen


def target_of(run: dict, pr_number: int | None) -> str:
    """`pull:<number>` for pull_request runs, else `branch:<name>`."""
    if run.get("event") == "pull_request" and pr_number is not None:
        return f"pull:{pr_number}"
    return f"branch:{run.get('head_branch')}"


def target_display(run: dict, pr_number: int | None) -> str:
    """Human-readable target for the Issue body."""
    if run.get("event") == "pull_request":
        if pr_number is not None:
            return f"PR #{pr_number} (head branch `{run.get('head_branch')}`)"
        return f"head branch `{run.get('head_branch')}` (no open PR found)"
    return f"branch: `{run.get('head_branch')}`"


def failed_steps(job: dict) -> list[str]:
    """Step names whose conclusion is a failure conclusion."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    names: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("conclusion") in FAILURE_CONCLUSIONS:
            name = step.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def job_lines(jobs: list[dict], conclusion: str) -> list[str]:
    """One evidence block per job with the run's failing conclusion."""
    lines: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("conclusion") != conclusion:
            continue
        name = job.get("name")
        if not isinstance(name, str) or not name:
            continue
        url = job.get("html_url")
        steps = failed_steps(job)
        lines.append(f"- job: `{name}` — conclusion: `{conclusion}`")
        if url:
            lines.append(f"  - job URL: {url}")
        if steps:
            lines.append("  - failed steps:")
            lines.extend(f"    - `{step}`" for step in steps)
        else:
            lines.append("  - failed steps: (none reported by the jobs API)")
    return lines


def build_body(run: dict, pr_number: int | None, jobs: list[dict],
               conclusion: str, job_fingerprints: dict[str, str],
               existing_body: str | None) -> str:
    """The Issue body: identity, evidence history, fingerprint, rule.

    `existing_body` (update/recovery case) contributes its previous run
    evidence lines so the history is never lost.
    """
    run_id = run.get("id")
    run_url = run.get("html_url")
    head_sha = run.get("head_sha")
    started = run.get("run_started_at")
    attempt = run.get("run_attempt")
    lines: list[str] = []
    lines.append("## CI failure (auto-created by the CI Failure Issue workflow)")
    lines.append("")
    lines.append(f"- workflow: `{CI_WORKFLOW_NAME}` (`{CI_WORKFLOW_PATH}`)")
    lines.append(f"- event: `{run.get('event')}`")
    lines.append(f"- target: {target_display(run, pr_number)}")
    lines.append(f"- commit: `{head_sha}`")
    lines.append(f"- run id: `{run_id}` (attempt {attempt})")
    if run_url:
        lines.append(f"- run URL: {run_url}")
    lines.append(f"- triggered at: `{started}`")
    lines.append(f"- run conclusion: `{conclusion}`")
    lines.append("")
    lines.append("### Failed jobs")
    lines.extend(job_lines(jobs, conclusion))
    lines.append("")
    lines.append("### Run history")
    evidence = run_evidence_lines(existing_body or "")
    current = (
        f"{RUN_EVIDENCE_MARKER} run id `{run_id}` (attempt {attempt}), "
        f"conclusion `{conclusion}`, started `{started}`"
    )
    if current not in evidence:
        evidence.append(current)
    lines.extend(f"- {line}" for line in evidence)
    lines.append("")
    for job_name, value in job_fingerprints.items():
        lines.append(f"- {FINGERPRINT_MARKER} `{value}` (job `{job_name}`)")
    lines.append("")
    lines.append("### Recovery rule")
    lines.append(
        "When a later successful CI run has the same fingerprint, the CI "
        "Failure Issue workflow appends the recovery run evidence and "
        "closes this Issue (`state_reason: completed`)."
    )
    lines.append("")
    lines.append(
        "Auto-created by the `CI Failure Issue` workflow "
        "(`.github/workflows/ci-failure-issue.yml`) — no secrets are "
        "included; see the run URL for the full logs."
    )
    return "\n".join(lines) + "\n"


def build_recovery_body(run: dict, existing_body: str) -> str:
    """Append the recovery evidence to the existing body and keep it."""
    run_id = run.get("id")
    started = run.get("run_started_at")
    attempt = run.get("run_attempt")
    recovery = (
        f"{RECOVERY_MARKER} run id `{run_id}` (attempt {attempt}), "
        f"conclusion `{SUCCESS_CONCLUSION}`, started `{started}`, "
        f"run URL {run.get('html_url')}"
    )
    lines = existing_body.rstrip("\n").splitlines()
    if f"- {recovery}" not in lines:
        # Insert after the run history block (or at the end).
        insert_at = len(lines)
        for index, line in enumerate(lines):
            if line.strip() == "### Run history":
                insert_at = index + 1
                break
        lines.insert(insert_at, f"- {recovery}")
    return "\n".join(lines) + "\n"


def find_matching_issue(issues: list[dict], fingerprints: set[str]) -> dict | None:
    """The first OPEN Issue whose body carries one of the fingerprints."""
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("state") != "open":
            continue
        body = issue.get("body")
        if not isinstance(body, str):
            continue
        if extract_fingerprints(body) and set(extract_fingerprints(body)) & fingerprints:
            return issue
    return None


def fetch_open_issues(owner: str, repo: str) -> list[dict]:
    """All open Issues carrying the exact label pair (paginated)."""
    data = gh_api(
        f"repos/{owner}/{repo}/issues?labels={','.join(ISSUE_LABELS)}"
        "&state=open",
        paginate=True,
    )
    if not isinstance(data, list):
        fail("fetch_open_issues", f"issues API did not return a list: {data!r}")
    return data


def create_issue(owner: str, repo: str, title: str, body: str) -> None:
    gh_api(
        f"repos/{owner}/{repo}/issues",
        method="POST",
        payload={"title": title, "body": body, "labels": list(ISSUE_LABELS)},
    )


def update_issue(owner: str, repo: str, number: int, body: str,
                 *, close: bool = False) -> None:
    payload: dict = {"body": body}
    if close:
        payload["state"] = "closed"
        payload["state_reason"] = "completed"
    gh_api(
        f"repos/{owner}/{repo}/issues/{number}",
        method="PATCH",
        payload=payload,
    )


def triage_failure(run: dict, owner: str, repo: str, jobs: list[dict],
                   conclusion: str) -> None:
    """Create or update the bug Issue for the failing run."""
    pr_number = (
        resolve_pr_number(owner, repo, run.get("head_sha", ""))
        if run.get("event") == "pull_request" else None
    )
    target = target_of(run, pr_number)
    fingerprints: dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("conclusion") != conclusion:
            continue
        name = job.get("name")
        if not isinstance(name, str) or not name:
            continue
        fingerprints[name] = fingerprint(
            workflow_path=CI_WORKFLOW_PATH, job_name=name,
            event=run.get("event"), target=target,
            head_sha=run.get("head_sha", ""), conclusion=conclusion,
        )
    if not fingerprints:
        fail(
            "triage_failure",
            f"run conclusion is {conclusion!r} but no job carries it "
            f"(jobs: {[job.get('name') if isinstance(job, dict) else job for job in jobs]!r})",
        )
    issues = fetch_open_issues(owner, repo)
    match = find_matching_issue(issues, set(fingerprints.values()))
    if match is None:
        title = (
            f"CI failure: {CI_WORKFLOW_NAME} / "
            + " + ".join(sorted(fingerprints))
            + f" ({run.get('event')} {target} @ {str(run.get('head_sha'))[:12]})"
        )
        body = build_body(run, pr_number, jobs, conclusion, fingerprints, None)
        create_issue(owner, repo, title, body)
        log(
            f"ci_failure_issue_created run_id={run.get('id')} "
            f"conclusion={conclusion} target={target}"
        )
        return
    number = match.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        fail("triage_failure", f"matched Issue has no number: {match!r}")
    body = build_body(
        run, pr_number, jobs, conclusion, fingerprints,
        match.get("body") if isinstance(match.get("body"), str) else None,
    )
    update_issue(owner, repo, number, body)
    log(
        f"ci_failure_issue_updated issue={number} run_id={run.get('id')} "
        f"conclusion={conclusion} target={target}"
    )


def triage_recovery(run: dict, owner: str, repo: str, jobs: list[dict]) -> None:
    """Close the matching bug Issue with recovery evidence (if any)."""
    pr_number = (
        resolve_pr_number(owner, repo, run.get("head_sha", ""))
        if run.get("event") == "pull_request" else None
    )
    target = target_of(run, pr_number)
    # The Issue was created with the FAILURE conclusion in its fingerprint;
    # recovery must match the same scenario under every failure conclusion
    # (failure / cancelled / timed_out), not the success conclusion.
    fingerprints: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        if not isinstance(name, str) or not name:
            continue
        for conclusion in FAILURE_CONCLUSIONS:
            fingerprints.add(fingerprint(
                workflow_path=CI_WORKFLOW_PATH, job_name=name,
                event=run.get("event"), target=target,
                head_sha=run.get("head_sha", ""), conclusion=conclusion,
            ))
    issues = fetch_open_issues(owner, repo)
    match = find_matching_issue(issues, fingerprints)
    if match is None:
        log(
            f"ci_failure_issue_recovery_no_match run_id={run.get('id')} "
            f"target={target}"
        )
        return
    number = match.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        fail("triage_recovery", f"matched Issue has no number: {match!r}")
    # find_matching_issue guarantees a str body carrying the fingerprint.
    body = match.get("body")
    update_issue(
        owner, repo, number, build_recovery_body(run, body), close=True,
    )
    log(
        f"ci_failure_issue_recovered issue={number} run_id={run.get('id')} "
        f"target={target}"
    )


def main() -> None:
    try:
        event = load_event()
        run = validate_event(event)
        conclusion = run.get("conclusion")
        if conclusion == SUCCESS_CONCLUSION:
            mode = "recovery"
        elif conclusion in FAILURE_CONCLUSIONS:
            mode = "failure"
        else:
            fail("classify_run", f"unknown conclusion: {conclusion!r}")
        owner, repo = repo_parts()
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            fail("classify_run", f"workflow_run has no integer id: {run!r}")
        jobs = fetch_jobs(owner, repo, run_id)
        if mode == "failure":
            triage_failure(run, owner, repo, jobs, conclusion)
        else:
            triage_recovery(run, owner, repo, jobs)
    except GhApiError as exc:
        # Fail fast: the triage job goes red with a structured log line;
        # a failed triage must never look like a successful no-op.
        fail("gh_api", f"endpoint={exc.endpoint} {exc.detail}")


if __name__ == "__main__":
    main()
