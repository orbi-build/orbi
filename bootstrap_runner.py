#!/usr/bin/env python3
"""One-shot bootstrap runner for Muyan Pilot.

This is intentionally small. It claims one ready GitHub Issue, gives it to
Pi in an isolated worktree, and accepts success only when one open PR exists.
Any command failure is logged and raised. There is no fallback or recovery.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import select
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

from pi_activity import (
    SessionWatcher,
    activity_snapshot,
    format_activity_scene,
)


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Live activity polling while Pi runs (Issue #24). The journal gets one
# activity line per poll with new events, and an idle warning when the
# session stays silent for this long.
PI_POLL_INTERVAL = 15.0
PI_IDLE_WARN_SECONDS = 300.0

# Run correlation (Issue #41): one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_CURRENT_RUN_ID: str | None = None

# GitHub labels are the only state store (Issue #45). An open PR is a
# recoverable review/fix state: the next tick resumes that same run on the
# same branch, worktree and PR instead of claiming a new Issue.
IN_PROGRESS_LABEL = "ai-in-progress"
PR_OPENED_LABEL = "ai-pr-opened"
BLOCKED_LABEL = "ai-blocked"

# Only comments posted by a repo maintainer are trusted to carry the
# recovery scene: a public comment (authorAssociation=NONE) must never
# steer the runner into an arbitrary local worktree, branch or PR
# (Issue #45 review, BLOCKER). A missing association is never trusted.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({
    "OWNER", "MAINTAINER", "MEMBER", "COLLABORATOR",
})


class RunIdFilter(logging.Filter):
    """Prefix every log message with the current `[run_id]`, if bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT_RUN_ID is not None:
            record.msg = f"[{_CURRENT_RUN_ID}] {record.msg}"
        return True


LOGGER.addFilter(RunIdFilter())


def validate_run_id(run_id: object) -> str:
    """Fail fast unless `run_id` is the 8-hex id of one task attempt."""
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


def set_run_id(run_id: str) -> None:
    """Bind one task attempt: every later journal line carries `[run_id]`."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = validate_run_id(run_id)


def current_run_id() -> str | None:
    """Return the run id bound to this tick, or None before the claim."""
    return _CURRENT_RUN_ID


def run_marker(run_id: str) -> str:
    """Return the stable machine-readable run marker for GitHub text."""
    return f"<!-- muyan-pilot:run={validate_run_id(run_id)} -->"


def _config_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict:
    """Load the human-maintained TOML config and resolve its paths."""
    base = path.resolve().parent
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    source_repos = data.get("source_repos")
    if not isinstance(source_repos, list) or not source_repos:
        raise ValueError("source_repos must be a non-empty list")
    if not all(isinstance(repo, str) and repo for repo in source_repos):
        raise ValueError("source_repos must contain non-empty strings")
    base_branch = data.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise ValueError("base_branch must be a non-empty string")
    return {
        "source_repos": source_repos,
        "repo_dir": _config_path(data.get("repo_dir", "."), base),
        "workspace_root": _config_path(data.get("workspace_root", ".."), base),
        "prompt": _config_path(data.get("prompt", "prompt.md"), base),
        "skills": [_config_path(item, base) for item in data.get("skills", [])],
        "context_files": [
            _config_path(item, base) for item in data.get("context_files", [])
        ],
        "base_branch": base_branch,
    }


def render_prompt(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def validate_config(config: dict) -> None:
    if not config["repo_dir"].is_dir():
        raise FileNotFoundError(config["repo_dir"])
    for path in [config["prompt"], *config["skills"], *config["context_files"]]:
        if not path.is_file():
            raise FileNotFoundError(path)


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int | None = None,
                log_command: list[str] | None = None,
                log_stdout: bool = False) -> str:
    """Run one external command; log context and fail fast on any error."""
    LOGGER.info(
        "command=%s cwd=%s",
        " ".join(log_command or command), cwd or Path.cwd(),
    )
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "command_failed returncode=%s stdout=%s stderr=%s",
            exc.returncode, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        LOGGER.error(
            "command_timeout timeout=%s stdout=%s stderr=%s",
            timeout, (exc.stdout or "").rstrip(),
            (exc.stderr or "").rstrip(),
        )
        raise
    except OSError as exc:
        LOGGER.error("command_spawn_failed error=%s", exc)
        raise
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    if log_stdout and result.stdout:
        LOGGER.info("stdout=%s", result.stdout.rstrip())
    return result.stdout.strip()


def parse_issue_array(raw: str) -> list[dict]:
    """Return the issue array from gh's JSON output."""
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("issue list must be a JSON array")
    return issues


def parse_issue_list(raw: str) -> dict | None:
    """Return the first issue from gh's JSON array, or None when idle."""
    issues = parse_issue_array(raw)
    return issues[0] if issues else None


def pick_issue(repo: str) -> dict | None:
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened -label:ai-blocked",
        "--json", "number,title,body", "--limit", "1",
    ])
    return parse_issue_list(raw)


def pick_next_issue(repos: list[str]) -> tuple[str, dict] | None:
    """Scan sources in order; return the first ready issue and its source."""
    for repo in repos:
        issue = pick_issue(repo)
        if issue is not None:
            return repo, issue
    return None


def edit_issue(number: int, *, repo: str, add: str | None = None,
               remove: str | None = None) -> None:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if add:
        command += ["--add-label", add]
    if remove:
        command += ["--remove-label", remove]
    run_command(command)


def comment_issue(number: int, *, repo: str, body: str) -> None:
    run_command(["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", body])


def issue_comments(number: int, *, repo: str) -> list[dict]:
    """Return the Issue's comment history (oldest first) from GitHub.

    ``gh issue view --json comments`` returns a top-level object with a
    ``comments`` array; each comment carries the author and the
    ``authorAssociation`` of the viewer, which is how the runner tells
    its own trusted comments apart from public ones (Issue #45).
    """
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("issue comments must be a JSON array")
    return comments


def issue_body(number: int, *, repo: str) -> str:
    """Return the Issue body; the fixer works from the original task."""
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "body",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    body = data.get("body")
    return body if isinstance(body, str) else ""


def new_run_id() -> str:
    """Return a unique short run identifier for one task attempt."""
    return uuid.uuid4().hex[:8]


def freeze_base(repo_dir: Path, base_branch: str) -> str:
    """Fetch the remote and freeze the exact SHA of origin/<base_branch>."""
    run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
    return run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )


def task_branch(source_repo: str, number: int, run_id: str) -> str:
    return (
        f"muyan-pilot/{source_repo.replace('/', '-')}-issue-{number}-{run_id}"
    )


def started_pi_comment_body(run_id: str, run_info: str, branch: str,
                            worktree: Path) -> str:
    """The start comment doubles as the recoverable run scene (Issue #45)."""
    return (
        f"{run_marker(run_id)}\n"
        f"Muyan Pilot started Pi: {run_info} branch={branch} "
        f"worktree={worktree}"
    )


def opened_pr_comment_body(run_id: str, run_info: str, pr_url: str) -> str:
    """The PR-opened comment records the recoverable run scene.

    It is the single source the next tick parses to resume this run on
    the same branch, worktree and PR (Issue #45). The runner is the only
    writer of this comment, so the scene carries only what the runner
    cannot derive itself: run_id, base and PR URL. Branch and worktree
    are derived from the configured repo_dir, source_repo, Issue number
    and run_id — a comment must never be able to name a local path.
    """
    return (
        f"{run_marker(run_id)}\n"
        f"Muyan Pilot opened PR: {pr_url} ({run_info})"
    )


OPENED_PR_PREFIX = "Muyan Pilot opened PR: "


def parse_pr_comment(body: str) -> dict | None:
    """Parse one `Muyan Pilot opened PR:` comment into a resume scene.

    Returns None when the body is not an opened-PR comment. Fails fast
    when the comment is malformed: resuming must recover the exact run
    (run id, base, PR URL), never a guess (Issue #45). Branch and
    worktree are not parsed: the runner derives them from its own
    config, the Issue number and the run id, so a comment can never
    name an arbitrary local path.
    """
    if not isinstance(body, str) or OPENED_PR_PREFIX not in body:
        return None
    head = body.split(OPENED_PR_PREFIX, 1)[1]
    pr_url, _, fields_part = head.partition(" (")
    fields: dict[str, str] = {}
    for part in fields_part.rstrip(")").split():
        key, _, value = part.partition("=")
        if key:
            fields[key] = value
    scene = {
        "pr_url": pr_url.strip(),
        "base_branch": fields.get("base_branch", ""),
        "base_sha": fields.get("base_sha", ""),
        "run_id": fields.get("run_id", ""),
    }
    for key, value in scene.items():
        if not value:
            raise ValueError(f"opened PR comment is missing {key}")
    scene["run_id"] = validate_run_id(scene["run_id"])
    return scene


def _comment_is_trusted(comment: object) -> bool:
    """True only when the comment carries a trusted maintainer association."""
    if not isinstance(comment, dict):
        return False
    return comment.get("authorAssociation") in TRUSTED_COMMENT_ASSOCIATIONS


def resume_scene(comments: list[dict]) -> dict:
    """Return the scene of the latest trusted opened-PR comment of one Issue.

    Only comments posted by a trusted maintainer (OWNER, MAINTAINER,
    MEMBER or COLLABORATOR) are considered: a public comment can never
    become the recovery scene (Issue #45 review, BLOCKER). Fails fast
    when no trusted comment carries the scene: such an Issue cannot be
    resumed and must not be guessed at.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        scene = parse_pr_comment(comment.get("body"))
        if scene is not None:
            return scene
    raise ValueError(
        "no 'Muyan Pilot opened PR' comment from a trusted author; the "
        "Issue cannot be resumed"
    )


def pick_resumable_delivery(repo: str) -> tuple[dict, dict] | None:
    """Return the newest `ai-pr-opened` delivery and its resume scene.

    An open PR is a recoverable review/fix state, not a finished task
    (Issue #45): the next tick resumes it on the same run instead of
    claiming a new Issue. Issues already `ai-blocked` are excluded, as
    are closed Issues and Issues whose comment history carries no scene.
    """
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        f"label:{PR_OPENED_LABEL} -label:{BLOCKED_LABEL}",
        "--json", "number,title,state,url", "--limit", "1",
    ])
    issues = parse_issue_array(raw)
    if not issues:
        return None
    issue = issues[0]
    if issue.get("state") != "OPEN":
        return None
    comments = issue_comments(int(issue["number"]), repo=repo)
    try:
        scene = resume_scene(comments)
    except ValueError:
        return None
    # The fixer works from the original task, so the resumable issue
    # carries its body like a freshly claimed one.
    issue["body"] = issue_body(int(issue["number"]), repo=repo)
    return issue, scene


def pick_next_delivery(repos: list[str]) -> tuple[str, dict, dict | None] | None:
    """Scan sources in order: resumable PRs first, then ready Issues.

    Returns `(source_repo, issue, scene)` where `scene` is None for a
    fresh claim. Resuming an open PR keeps the single concurrency slot
    occupied by the same run (implement → review → fix → merge), so a
    second Pi is never started for a run that already has a PR.
    """
    for repo in repos:
        selected = pick_resumable_delivery(repo)
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        issue = pick_issue(repo)
        if issue is not None:
            return repo, issue, None
    return None


def merge_in_progress(worktree: Path) -> bool:
    """True when the worktree is mid-merge (conflicts staged for a commit)."""
    try:
        run_command(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=worktree,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def merge_latest_base(worktree: Path, base_branch: str) -> bool:
    """Fetch `origin/<base>` and merge it when the delivery is behind.

    Returns True when a merge was started. A conflicted merge is left
    staged for the fixer (Pi) to resolve: the runner never auto-resolves,
    force-pushes, or pushes the protected branch (Issue #45). A merge
    failure that is not a conflict fails fast.
    """
    run_command(["git", "fetch", "origin", base_branch], cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor",
             f"origin/{base_branch}", "HEAD"],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.info(
            "base_advanced base_branch=%s worktree=%s; merging into the "
            "task branch",
            base_branch, worktree,
        )
        try:
            run_command(
                ["git", "merge", f"origin/{base_branch}"], cwd=worktree,
            )
        except subprocess.CalledProcessError:
            if not merge_in_progress(worktree):
                raise
            LOGGER.warning(
                "base_merge_conflict base_branch=%s worktree=%s; the "
                "conflict is left staged for the fixer",
                base_branch, worktree,
            )
        return True
    return False


def worktree_path(repo_dir: Path, source_repo: str, number: int,
                  run_id: str) -> Path:
    """Task worktrees live in the configured repo's .worktrees/ directory."""
    slug = source_repo.replace("/", "-")
    return (
        repo_dir / ".worktrees"
        / f"muyan-pilot-{slug}-issue-{number}-{run_id}"
    )


def create_worktree(repo_dir: Path, source_repo: str, number: int,
                    run_id: str, base_sha: str) -> Path:
    """Create the task worktree from the frozen base SHA, never HEAD."""
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        raise RuntimeError(f"worktree path already exists: {path}")
    branch = task_branch(source_repo, number, run_id)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), base_sha,
    ], cwd=repo_dir)
    return path


def _drain_stream(stream, chunks: list[bytes]) -> None:
    """Read a pipe to EOF, appending chunks (process must be finished)."""
    while True:
        data = os.read(stream.fileno(), 65536)
        if not data:
            return
        chunks.append(data)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")


def _log_pi_activity(activity: dict, context: str,
                     idle_warn_seconds: float) -> None:
    """Log new session activity, or an idle warning with the full scene."""
    scene = format_activity_scene(activity)
    if activity["changed"]:
        LOGGER.info("pi_activity %s %s", context, scene)
        return
    if activity["stale_seconds"] >= idle_warn_seconds:
        LOGGER.warning(
            "pi_idle %s %s stale_seconds=%.0f",
            context, scene, activity["stale_seconds"],
        )


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS,
    log_command: list[str] | None = None,
    issue: int | None = None,
    source_repo: str | None = None,
    branch: str | None = None,
) -> str:
    """Run Pi and stream its live session activity into the journal.

    Every poll, the newest Pi session JSONL activity is logged with the task
    context (issue, source repo, branch, worktree): phase, last activity
    time and a sanitized tool summary. When no new event arrives for
    `idle_warn_seconds`, a warning with the full scene is logged. On a
    non-zero exit the scene is logged before the error is raised. The
    session JSONL stays in the worktree as the complete local record; the
    full prompt and Issue body are never logged.
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    watcher = SessionWatcher(cwd / ".pi-session")
    context = (
        f"issue={issue} source_repo={source_repo} branch={branch} "
        f"worktree={cwd}"
    )
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    activity = watcher.poll()
    timed_out = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                process.kill()
                timed_out = True
                break
            ready, _, _ = select.select(
                [process.stdout, process.stderr], [], [], poll_interval,
            )
            for stream in ready:
                data = os.read(stream.fileno(), 65536)
                if data:
                    if stream is process.stdout:
                        stdout_chunks.append(data)
                    else:
                        stderr_chunks.append(data)
            activity = watcher.poll()
            _log_pi_activity(activity, context, idle_warn_seconds)
            if process.poll() is not None:
                break
    finally:
        _drain_stream(process.stdout, stdout_chunks)
        _drain_stream(process.stderr, stderr_chunks)
    stdout = _decode_chunks(stdout_chunks)
    stderr = _decode_chunks(stderr_chunks)
    if timed_out:
        LOGGER.error(
            "pi_timeout timeout=%s %s %s",
            timeout, context, format_activity_scene(activity),
        )
        raise subprocess.TimeoutExpired(
            safe_command, timeout, output=stdout, stderr=stderr,
        )
    if process.returncode != 0:
        LOGGER.error(
            "pi_failed returncode=%s %s %s",
            process.returncode, context, format_activity_scene(activity),
        )
        raise subprocess.CalledProcessError(
            process.returncode, safe_command, output=stdout, stderr=stderr,
        )
    if stderr:
        LOGGER.info("stderr=%s", stderr.rstrip())
    LOGGER.info("stdout=%s", stdout.rstrip())
    return stdout.strip()


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           timeout: int | None = None,
           branch: str | None = None,
           pr_url: str | None = None) -> str:
    system_prompt = render_prompt(
        config["prompt"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "SOURCE_REPOS": ", ".join(config["source_repos"]),
            "ISSUE_NUMBER": str(issue["number"]),
            "ISSUE_TITLE": issue["title"],
            "ISSUE_BODY": issue.get("body", ""),
            "WORKSPACE_ROOT": str(config["workspace_root"]),
            "CONTEXT_FILES": "\n".join(str(path) for path in config["context_files"]),
            "SKILLS": "\n".join(str(path) for path in config["skills"]),
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": config["base_sha"],
            "RUN_ID": config["run_id"],
        },
    )
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
    )
    if pr_url is not None:
        context += (
            f"Existing PR: {pr_url}\n"
            "This run already opened that PR. Keep the same PR number, the "
            "same run id, branch and worktree: never close the PR, never "
            "create a new PR, never re-claim the Issue. Fix the review "
            "findings and/or resolve the merge conflicts left in the "
            "worktree, rerun the full test suite with coverage and the "
            "complete review, then commit and push ONLY the task branch "
            "so the same PR updates. No force push, no push of the "
            "protected branch.\n"
        )
    context += "Complete the delivery process in the system prompt."
    skill_args = [item for skill in config["skills"] for item in ("--skill", str(skill))]
    command = [
        "pi", *skill_args, "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt, context,
    ]
    LOGGER.info(
        "pi_session=%s issue=%s source_repo=%s",
        worktree / ".pi-session", issue["number"], source_repo,
    )
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", "--print", "--session-dir", str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
        issue=int(issue["number"]),
        source_repo=source_repo,
        branch=branch,
    )


def verify_pr(worktree: Path, branch: str, base_branch: str,
              run_id: str, *, pr_repo: str | None = None,
              expected_url: str | None = None,
              require_latest_base: bool = True) -> str:
    """Verify that exactly one open PR of the task branch is the delivery.

    Checks, in order: current branch, latest remote base ancestry (unless
    `require_latest_base` is False — the resume pre-validation runs
    before the base merge, when being behind is the expected state),
    exactly one open PR for the head branch, PR base, PR head == local
    HEAD, and the run marker in the PR body. When `pr_repo` is given
    (resume path), the PR's head repo must be that repo; when
    `expected_url` is given, the verified PR URL must exactly equal the
    recovered original PR URL (Issue #45 review: the resume must keep
    the same PR number).
    """
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    if require_latest_base:
        # Re-fetch before judging: the delivery must contain the latest
        # remote base, otherwise it is behind and the PR is rejected
        # (fail fast).
        run_command(
            ["git", "fetch", "origin", base_branch], cwd=worktree,
        )
        try:
            run_command(
                ["git", "merge-base", "--is-ancestor",
                 f"origin/{base_branch}", "HEAD"],
                cwd=worktree,
            )
        except subprocess.CalledProcessError:
            LOGGER.error(
                "delivery_behind_base base_branch=%s branch=%s",
                base_branch, branch,
            )
            raise RuntimeError(
                f"delivery HEAD is behind latest remote base "
                f"origin/{base_branch}; merge the latest base, rerun full "
                "tests and review, then retry"
            ) from None
    local_head = run_command(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
    )
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json",
        "url,baseRefName,headRefName,headRefOid,"
        "headRepository,headRepositoryOwner,body",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    url = prs[0].get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    if pr_repo is not None:
        head_repo = _pr_head_repo(prs[0])
        if head_repo != pr_repo:
            LOGGER.error(
                "pr_repo_mismatch expected=%s actual=%s branch=%s",
                pr_repo, head_repo, branch,
            )
            raise RuntimeError(
                f"PR head repo is {head_repo}, expected {pr_repo}; the "
                "resume must keep the PR of the configured source repo"
            )
    base_ref = prs[0].get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; recreate the "
            "PR against the configured base branch"
        )
    head_oid = prs[0].get("headRefOid")
    if head_oid != local_head:
        LOGGER.error(
            "pr_head_mismatch pr_head=%s local_head=%s branch=%s",
            head_oid, local_head, branch,
        )
        raise RuntimeError(
            f"PR head {head_oid} is not local HEAD {local_head}; the "
            "verified commit was not pushed, push the reviewed commit and retry"
        )
    marker = run_marker(run_id)
    body = prs[0].get("body")
    if not isinstance(body, str) or marker not in body:
        LOGGER.error(
            "pr_run_marker_missing expected=%s branch=%s", marker, branch,
        )
        raise RuntimeError(
            f"PR body is missing the stable run marker {marker}; the PR "
            "must carry the machine-readable run id of this attempt"
        )
    if expected_url is not None and url != expected_url:
        LOGGER.error(
            "pr_url_mismatch expected=%s actual=%s branch=%s",
            expected_url, url, branch,
        )
        raise RuntimeError(
            f"PR URL {url} is not the recovered original PR "
            f"{expected_url}; the resume must keep the same PR number"
        )
    return url


def _pr_head_repo(pr: dict) -> str:
    """Return `owner/name` of the PR head repo, or '<missing>' if absent."""
    owner = pr.get("headRepositoryOwner")
    repo = pr.get("headRepository")
    if not isinstance(owner, dict) or not isinstance(repo, dict):
        return "<missing>"
    login = owner.get("login")
    name = repo.get("name")
    if not isinstance(login, str) or not login \
            or not isinstance(name, str) or not name:
        return "<missing>"
    return f"{login}/{name}"


def process_issue(issue: dict, config: dict, source_repo: str) -> str:
    number = int(issue["number"])
    base_branch = config["base_branch"]
    # The run id is generated once per attempt, before any other step is
    # logged, so every journal line of the attempt carries it (Issue #41).
    run_id = new_run_id()
    set_run_id(run_id)
    base_sha = freeze_base(config["repo_dir"], base_branch)
    branch = task_branch(source_repo, number, run_id)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    edit_issue(number, repo=source_repo, add=IN_PROGRESS_LABEL)
    worktree: Path | None = None
    try:
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, base_sha,
        )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        comment_issue(
            number, repo=source_repo,
            body=started_pi_comment_body(run_id, run_info, branch, worktree),
        )
        run_pi(issue, worktree, config, source_repo, branch=branch)
        pr_url = verify_pr(worktree, branch, base_branch, run_id)
        edit_issue(
            number, repo=source_repo, add=PR_OPENED_LABEL,
            remove=IN_PROGRESS_LABEL,
        )
        comment_issue(
            number, repo=source_repo,
            body=opened_pr_comment_body(run_id, run_info, pr_url),
        )
        return pr_url
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        scene = ""
        if worktree is not None:
            try:
                scene = format_activity_scene(
                    activity_snapshot(worktree / ".pi-session"),
                )
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
        try:
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=IN_PROGRESS_LABEL,
            )
            body = (
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot failed: {exc} ({run_info})"
            )
            if scene:
                body += f" {scene}"
            comment_issue(number, repo=source_repo, body=body)
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def resume_delivery(issue: dict, scene: dict, config: dict,
                    source_repo: str) -> str:
    """Resume the fix loop of an opened PR on the same run (Issue #45).

    The scene (run id, base, PR URL) is recovered from the Issue's
    trusted `Muyan Pilot opened PR:` comment, so a restart of the runner
    or the service resumes from GitHub state alone. Branch and worktree
    are DERIVED from the configured repo_dir, source_repo, Issue number
    and run id — never read from the comment — so no comment can steer
    the runner into an arbitrary local path. Before any git or Pi
    mutation the configured base and the open PR (repo, head branch,
    base, run marker, exact URL) are validated; the latest remote base is
    then merged into the ORIGINAL branch (conflicts stay staged for the
    fixer), Pi fixes the PR in the ORIGINAL worktree, and the SAME PR is
    re-verified: the returned URL is the one verify_pr verified, which
    must exactly equal the recovered original PR URL. Any failure marks
    the Issue `ai-blocked` and preserves the PR, branch and worktree: the
    run is never re-claimed, the PR is never closed or replaced.
    """
    number = int(issue["number"])
    run_id = validate_run_id(scene["run_id"])
    # Derive the expected branch and worktree from the runner's own
    # config and the run id (the values the first run used when it
    # created them); a comment can never name an arbitrary local path.
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(config["repo_dir"], source_repo, number, run_id)
    base_branch = scene["base_branch"]
    pr_url = scene["pr_url"]
    set_run_id(run_id)
    scene_info = (
        f"base_branch={base_branch} base_sha={scene['base_sha']} "
        f"run_id={run_id} branch={branch} worktree={worktree} "
        f"pr_url={pr_url}"
    )
    LOGGER.info("issue=%s resuming opened PR %s", number, scene_info)
    try:
        # Validate the configured base before any git/Pi mutation: a
        # scene frozen on another base branch is never merged.
        if base_branch != config["base_branch"]:
            raise RuntimeError(
                f"base branch mismatch: scene has {base_branch}, config "
                f"has {config['base_branch']}; the resume must use the "
                "configured base branch"
            )
        if not worktree.is_dir():
            raise RuntimeError(f"worktree missing: {worktree}")
        # Validate the open PR before any git/Pi mutation: exactly one
        # open PR of the derived branch, in the configured source repo,
        # on the configured base, carrying the run marker, and with the
        # exact URL of the recovered original PR (same PR number). The
        # latest-base check is deferred to the post-fix verification:
        # being behind the base is the expected state the resume exists
        # to fix (merge_latest_base runs next).
        verified_url = verify_pr(
            worktree, branch, base_branch, run_id,
            pr_repo=source_repo, expected_url=pr_url,
            require_latest_base=False,
        )
        merge_latest_base(worktree, base_branch)
        config = {**config, "base_sha": scene["base_sha"], "run_id": run_id}
        run_pi(
            issue, worktree, config, source_repo,
            branch=branch, pr_url=verified_url,
        )
        # Re-verify the SAME PR after the fixer pushed: the verified URL
        # must still exactly equal the recovered original PR URL.
        verified_url = verify_pr(
            worktree, branch, base_branch, run_id,
            pr_repo=source_repo, expected_url=pr_url,
        )
        comment_issue(
            number, repo=source_repo,
            body=(
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot fixed PR: {verified_url} ({scene_info})"
            ),
        )
        return verified_url
    except Exception as exc:
        LOGGER.exception("issue=%s resume failed", number)
        activity_scene = ""
        try:
            activity_scene = format_activity_scene(
                activity_snapshot(worktree / ".pi-session"),
            )
        except Exception:
            LOGGER.exception("issue=%s activity scene failed", number)
        try:
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=PR_OPENED_LABEL,
            )
            body = (
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot failed: {exc} ({scene_info})"
            )
            if activity_scene:
                body += f" {activity_scene}"
            comment_issue(number, repo=source_repo, body=body)
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("MUYAN_PILOT_CONFIG", "muyan-pilot.toml")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    validate_config(config)
    selected = pick_next_delivery(config["source_repos"])
    if selected is None:
        LOGGER.info("source_repos=%s outcome=no_ready_issue", config["source_repos"])
        return 0
    source_repo, issue, scene = selected
    if scene is not None:
        # An open PR is a recoverable review/fix state: resume the same
        # run on the same branch, worktree and PR (Issue #45).
        resume_delivery(issue, scene, config, source_repo)
    else:
        process_issue(issue, config, source_repo)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
