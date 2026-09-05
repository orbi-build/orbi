#!/usr/bin/env python3
"""CI failure triage (Issue #106): file and close deduplicated bug Issues.

Runs from the `CI Failure Issue` workflow (`.github/workflows/
ci-failure-issue.yml`), which GitHub triggers with `on: workflow_run`
(`workflows: ["CI"]`, `types: [completed]`) after every completed run of the
CI workflow (`.github/workflows/ci.yml`) — the repository's remote pytest +
coverage gate on `pull_request` and `push` to `main`.

Behavior (external shapes verified against the live REST API — the run
object, the jobs object and `commits/{sha}/pulls` — and `gh api --help`,
gh 2.97):

- The CI run concluded `failure`, `cancelled` or `timed_out` on a target
  event: one `bug` + `ai-ready` Issue per FAILED JOB (the Muyan Pilot ready
  queue picks it up and delivers the fix through its normal single-Issue /
  single-PR / review contract), with the full evidence — workflow, job,
  event, branch/PR, commit SHA, run id, run URL, failed steps, trigger
  time. A re-occurrence of the same fingerprint appends a comment to the
  existing Issue instead of creating a second one.
- The CI run concluded `success`: every SUCCEEDED job's fingerprint is
  matched against the open triage Issues; a match gets the recovery run
  evidence as a comment and is closed (`state_reason: completed`).
- Anything else (another workflow, another event, a push off `main`, any
  other conclusion) is a logged no-op with exit 0.

Stable failure fingerprint: `sha256("CI|<event>|<head_branch>|<job name>")`,
embedded in the Issue body as the hidden marker
`<!-- ci-failure-fingerprint:<hex> -->`. It is deliberately STABLE across
commits — the recovery run carries a different head SHA, so a commit-scoped
fingerprint could never match the Issue it has to close. The failing
commit/run association is tracked as evidence (the Issue body plus one
re-occurrence comment per run) instead.

Classification rules: a job is failing iff its `conclusion` is in
(failure, cancelled, timed_out); its failed steps are the steps with
`failure` or `timed_out` conclusions (a cancelled step is a cancellation
symptom, not a step failure).

Anti-recursion: the workflow reacts only to `workflow_run` of the CI
workflow, the code re-checks `run.path`, and Issue create/update/close emit
no `pull_request`/`push`/CI-workflow_run event — the triage can never
re-trigger itself.

Fail fast: every environment or `gh api` failure is one structured
`ci_triage error stage=...` log line and exit 1 — the triage job goes red
instead of faking success. The Issue body is built only from the event
payload and the jobs API response; no secret, token or unrelated
environment variable ever reaches it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

CI_WORKFLOW_NAME = "CI"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
TRIAGE_WORKFLOW_PATH = ".github/workflows/ci-failure-issue.yml"
TARGET_EVENTS = ("pull_request", "push")
PUSH_TARGET_BRANCH = "main"
FAILURE_CONCLUSIONS = ("failure", "cancelled", "timed_out")
STEP_FAILURE_CONCLUSIONS = ("failure", "timed_out")
SUCCESS_CONCLUSION = "success"
ISSUE_LABELS = ("bug", "ai-ready")
FINGERPRINT_MARKER = "ci-failure-fingerprint:"
FINGERPRINT_RE = re.compile(
    r"<!--\s*" + re.escape(FINGERPRINT_MARKER) + r"([0-9a-f]{64})\s*-->"
)


class GhApiError(Exception):
    """A `gh api` call failed (non-zero exit code or a non-JSON body)."""

    def __init__(self, endpoint: str, detail: str):
        super().__init__(f"gh api {endpoint} failed: {detail}")
        self.endpoint = endpoint
        self.detail = detail


def log(message: str) -> None:
    """One structured line to stderr (the job log is the record)."""
    print(f"ci_triage {message}", file=sys.stderr, flush=True)


def fail(stage: str, error: str) -> None:
    """Fail fast with a structured error; the triage job goes red."""
    log(f"error stage={stage}: {error}")
    raise SystemExit(1)


def gh_api(endpoint: str, *, method: str = "GET", payload: dict | None = None):
    """Call `gh api` (GH_TOKEN auth) and parse the JSON response.

    Verified against `gh api --help` (gh 2.97): `-X` overrides the method,
    `--input -` reads the request body from stdin.
    """
    command = ["gh", "api", endpoint]
    stdin_text = None
    if method != "GET":
        command += ["-X", method]
    if payload is not None:
        command += ["--input", "-"]
        stdin_text = json.dumps(payload)
    proc = subprocess.run(command, input=stdin_text, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GhApiError(endpoint, detail or f"exit code {proc.returncode}")
    stdout = proc.stdout.strip()
    try:
        return json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise GhApiError(endpoint, f"non-JSON response: {exc}") from exc


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
        fail("load_event", f"event payload is not a JSON object: {type(event).__name__}")
    return event


def repo_parts() -> tuple[str, str]:
    """(owner, repo) from GITHUB_REPOSITORY."""
    value = os.environ.get("GITHUB_REPOSITORY", "")
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name:
        fail("repo_parts", f"GITHUB_REPOSITORY is not owner/repo: {value!r}")
    return owner, name


def fingerprint(event: str, head_branch: str, job_name: str) -> str:
    """The stable failure fingerprint (see the module docstring)."""
    raw = f"{CI_WORKFLOW_NAME}|{event}|{head_branch}|{job_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_fingerprints(body: str) -> list[str]:
    """All fingerprint marker values in an Issue body, in order, deduped."""
    seen: list[str] = []
    for value in FINGERPRINT_RE.findall(body):
        if value not in seen:
            seen.append(value)
    return seen


def triage_scope(run: dict) -> tuple[str, str]:
    """("failure" | "recovery" | "ignore", ignore reason) for the run."""
    if run.get("path") != CI_WORKFLOW_PATH:
        return "ignore", f"reason=not_ci_workflow path={run.get('path')!r}"
    event = run.get("event")
    if event not in TARGET_EVENTS:
        return "ignore", f"reason=not_target_event event={event!r}"
    if event == "push" and run.get("head_branch") != PUSH_TARGET_BRANCH:
        return "ignore", f"reason=push_not_main branch={run.get('head_branch')!r}"
    conclusion = run.get("conclusion")
    if conclusion == SUCCESS_CONCLUSION:
        return "recovery", ""
    if conclusion in FAILURE_CONCLUSIONS:
        return "failure", ""
    return "ignore", f"reason=conclusion_not_actionable conclusion={conclusion!r}"


def jobs_with_conclusion(jobs: list, conclusions: tuple[str, ...]) -> list[dict]:
    """Jobs whose conclusion is in `conclusions`, name-sorted (stable)."""
    selected = [
        item for item in jobs
        if isinstance(item, dict)
        and isinstance(item.get("name"), str) and item.get("name")
        and item.get("conclusion") in conclusions
    ]
    selected.sort(key=lambda item: item["name"])
    return selected


def failed_steps(job: dict) -> list[str]:
    """Names of the job's failed steps (failure/timed_out conclusions)."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        step["name"]
        for step in steps
        if isinstance(step, dict)
        and step.get("conclusion") in STEP_FAILURE_CONCLUSIONS
        and isinstance(step.get("name"), str)
    ]


def failed_steps_display(job: dict) -> str:
    """The failed step names as inline code, or the API-reported none."""
    names = failed_steps(job)
    if names:
        return ", ".join(f"`{name}`" for name in names)
    return "(none reported by the jobs API)"


def target_display(run: dict, pr_number: int | None) -> str:
    """Human-readable branch/PR target for the Issue body."""
    if run.get("event") == "pull_request":
        if pr_number is not None:
            return f"PR #{pr_number} (head branch `{run.get('head_branch')}`)"
        return f"head branch `{run.get('head_branch')}` (no PR resolved)"
    return f"branch `{run.get('head_branch')}`"


def issue_title(run: dict, job: dict, pr_number: int | None) -> str:
    if run.get("event") == "pull_request" and pr_number is not None:
        target = f"PR #{pr_number}"
    else:
        target = f"branch {run.get('head_branch')}"
    return f"CI failure: {job['name']} on {target} ({run.get('event')})"


def build_failure_body(
    run: dict, job: dict, pr_number: int | None, fingerprint_value: str,
) -> str:
    """The Issue body: full evidence, the dedup rule and the hidden marker."""
    return "\n".join([
        f"## CI failure: {job['name']}",
        "",
        "Auto-created by the `CI Failure Issue` workflow "
        "(`.github/workflows/ci-failure-issue.yml`) from the failed CI run.",
        "",
        f"- workflow: `{CI_WORKFLOW_NAME}` (`{CI_WORKFLOW_PATH}`)",
        f"- event: `{run.get('event')}`",
        f"- branch/PR: {target_display(run, pr_number)}",
        f"- commit: `{run.get('head_sha')}`",
        f"- run id: `{run.get('id')}` (attempt {run.get('run_attempt')})",
        f"- run URL: {run.get('html_url')}",
        f"- triggered at: `{run.get('run_started_at')}`",
        f"- run conclusion: `{run.get('conclusion')}`",
        "",
        "### Failed job",
        f"- job `{job['name']}` — conclusion: `{job.get('conclusion')}`"
        f" — [job logs]({job.get('html_url')})",
        f"- failed steps: {failed_steps_display(job)}",
        "",
        "### Dedup and recovery",
        "A re-run of the same failure (same workflow/job/branch fingerprint)",
        "updates this Issue instead of creating a new one; when a later",
        "successful CI run of the same workflow/job/branch matches, this",
        "Issue is closed as completed with the recovery run evidence.",
        "",
        f"<!-- {FINGERPRINT_MARKER}{fingerprint_value} -->",
        "",
    ])


def build_reoccurrence_comment(run: dict, job: dict) -> str:
    """The evidence appended to an existing Issue when the failure repeats."""
    return "\n".join([
        f"CI failure re-occurred (job `{job['name']}`, same fingerprint):",
        "",
        f"- commit: `{run.get('head_sha')}`",
        f"- run id: `{run.get('id')}` (attempt {run.get('run_attempt')}),"
        f" conclusion: `{run.get('conclusion')}`",
        f"- run URL: {run.get('html_url')}",
        f"- triggered at: `{run.get('run_started_at')}`",
        f"- failed steps: {failed_steps_display(job)}",
        "",
        "Updated by the `CI Failure Issue` workflow"
        " (`.github/workflows/ci-failure-issue.yml`).",
    ])


def build_recovery_comment(run: dict, job: dict) -> str:
    """The recovery evidence appended before the Issue is closed."""
    return "\n".join([
        f"CI recovered: job `{job['name']}` passed with the same fingerprint:",
        "",
        f"- commit: `{run.get('head_sha')}`",
        f"- run id: `{run.get('id')}` (attempt {run.get('run_attempt')}),"
        f" conclusion: `{SUCCESS_CONCLUSION}`",
        f"- run URL: {run.get('html_url')}",
        f"- triggered at: `{run.get('run_started_at')}`",
        "",
        "Closing as completed (recovery verified by the `CI Failure Issue`"
        " workflow, `.github/workflows/ci-failure-issue.yml`).",
    ])


def resolve_pr_number(owner: str, repo: str, head_sha: str) -> int | None:
    """The first PR associated with the head commit (None when unresolvable).

    Verified endpoint: `GET /repos/{owner}/{repo}/commits/{sha}/pulls`
    (resolves PR #328 from its head SHA on the live repo).
    """
    pulls = gh_api(f"repos/{owner}/{repo}/commits/{head_sha}/pulls")
    if not isinstance(pulls, list):
        fail("resolve_pr_number", f"commits/pulls did not return a list: {pulls!r}")
    for item in pulls:
        number = item.get("number") if isinstance(item, dict) else None
        if isinstance(number, int):
            return number
    return None


def fetch_jobs(owner: str, repo: str, run_id: int) -> list:
    """The completed run's jobs (verified shape: `{jobs: [...]}`)."""
    data = gh_api(f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        fail("fetch_jobs", f"jobs API did not return a jobs list: {data!r}")
    return jobs


def index_open_issues(owner: str, repo: str) -> dict[str, dict]:
    """Open Issues labeled bug+ai-ready, indexed by body fingerprint."""
    issues = gh_api(
        f"repos/{owner}/{repo}/issues"
        f"?labels={','.join(ISSUE_LABELS)}&state=open&per_page=100"
    )
    if not isinstance(issues, list):
        fail("list_issues", f"issues API did not return a list: {issues!r}")
    index: dict[str, dict] = {}
    for item in issues:
        if not isinstance(item, dict) or "pull_request" in item:
            continue  # the endpoint also returns PRs; never triage those
        number = item.get("number")
        body = item.get("body")
        if not isinstance(number, int) or not isinstance(body, str):
            continue
        for value in issue_fingerprints(body):
            index.setdefault(value, item)
    return index


def create_issue(owner: str, repo: str, title: str, body: str) -> None:
    gh_api(
        f"repos/{owner}/{repo}/issues",
        method="POST",
        payload={"title": title, "body": body, "labels": list(ISSUE_LABELS)},
    )


def comment_issue(owner: str, repo: str, number: int, body: str) -> None:
    gh_api(
        f"repos/{owner}/{repo}/issues/{number}/comments",
        method="POST",
        payload={"body": body},
    )


def close_issue(owner: str, repo: str, number: int) -> None:
    gh_api(
        f"repos/{owner}/{repo}/issues/{number}",
        method="PATCH",
        payload={"state": "closed", "state_reason": "completed"},
    )


def triage_failure(run: dict, owner: str, repo: str, jobs: list) -> None:
    """Create or update one bug Issue per failed job."""
    pr_number = None
    if run.get("event") == "pull_request":
        pr_number = resolve_pr_number(owner, repo, run.get("head_sha", ""))
    failed = jobs_with_conclusion(jobs, FAILURE_CONCLUSIONS)
    if not failed:
        log(f"ignored reason=no_failed_jobs run_id={run.get('id')}")
        return
    index = index_open_issues(owner, repo)
    for item in failed:
        value = fingerprint(
            run.get("event"), run.get("head_branch", ""), item["name"],
        )
        match = index.get(value)
        if match is None:
            create_issue(
                owner, repo,
                issue_title(run, item, pr_number),
                build_failure_body(run, item, pr_number, value),
            )
            log(f"created issue job={item['name']} run_id={run.get('id')}")
        else:
            comment_issue(
                owner, repo, match["number"],
                build_reoccurrence_comment(run, item),
            )
            log(
                f"updated issue={match['number']} job={item['name']}"
                f" run_id={run.get('id')}"
            )


def triage_recovery(run: dict, owner: str, repo: str, jobs: list) -> None:
    """Close the Issue of every succeeded job with the recovery evidence."""
    succeeded = jobs_with_conclusion(jobs, (SUCCESS_CONCLUSION,))
    if not succeeded:
        log(f"ignored reason=no_succeeded_jobs run_id={run.get('id')}")
        return
    index = index_open_issues(owner, repo)
    for item in succeeded:
        value = fingerprint(
            run.get("event"), run.get("head_branch", ""), item["name"],
        )
        match = index.get(value)
        if match is None:
            log(f"recovery_no_match job={item['name']} run_id={run.get('id')}")
            continue
        comment_issue(
            owner, repo, match["number"], build_recovery_comment(run, item),
        )
        close_issue(owner, repo, match["number"])
        log(
            f"recovered issue={match['number']} job={item['name']}"
            f" run_id={run.get('id')}"
        )


def main() -> None:
    try:
        event = load_event()
        run = event.get("workflow_run")
        if not isinstance(run, dict):
            fail("load_event", "event payload has no workflow_run object")
        mode, reason = triage_scope(run)
        if mode == "ignore":
            log(f"ignored {reason}")
            return
        owner, repo = repo_parts()
        run_id = run.get("id")
        if not isinstance(run_id, int):
            fail("classify_run", f"workflow_run has no integer id: {run_id!r}")
        jobs = fetch_jobs(owner, repo, run_id)
        if mode == "failure":
            triage_failure(run, owner, repo, jobs)
        else:
            triage_recovery(run, owner, repo, jobs)
    except GhApiError as exc:
        # Fail fast: the triage job goes red with a structured log line; a
        # failed triage must never look like a successful no-op.
        fail("gh_api", f"{exc.endpoint}: {exc.detail}")


if __name__ == "__main__":  # pragma: no cover
    main()
