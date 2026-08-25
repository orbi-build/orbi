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
    sanitize,
)
from progress import ProgressPublisher, format_elapsed, progress_body


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Live activity polling while Pi runs (Issue #24). The journal gets one
# activity line per poll with new events, and an idle warning when the
# session stays silent for this long.
PI_POLL_INTERVAL = 15.0
PI_IDLE_WARN_SECONDS = 300.0
# Automatic observability (Issue #18): the journal gets a heartbeat at most
# every 30 seconds and the GitHub progress comment is PATCHed on change or
# at the same cadence. No human has to run a status command.
PI_HEARTBEAT_SECONDS = 30.0


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
    """Create the task worktree from the frozen base SHA, never HEAD.

    An existing path is reused: only a resumed run (same run id after a
    process restart) reaches that state, and its worktree is the scene
    the run continues in (Issue #18).
    """
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        return path
    branch = task_branch(source_repo, number, run_id)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), base_sha,
    ], cwd=repo_dir)
    return path


def latest_run_id(repo_dir: Path, source_repo: str, number: int) -> str | None:
    """Return the run id of the newest task worktree for the issue.

    The worktree directory name carries the run id — the only state
    needed to resume the same GitHub progress comment (hidden run
    marker) instead of creating a second one.
    """
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/muyan-pilot-{slug}-issue-{number}-*"
    candidates = [
        path for path in repo_dir.glob(pattern) if path.is_dir()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return newest.name.rsplit("-", 1)[-1]


def has_in_progress_label(number: int, repo: str) -> bool:
    """True when the Issue still carries `ai-in-progress`.

    The label is added at claim time and removed only by the success or
    failure path, so it is the marker of a run that is (or was, when the
    runner died) in flight — as opposed to the preserved worktrees of
    completed runs.
    """
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "all",
        "--search", "label:ai-in-progress",
        "--json", "number", "--limit", "50",
    ])
    issues = parse_issue_array(raw)
    return any(int(issue.get("number", -1)) == number for issue in issues)


def _drain_stream(stream, chunks: list[bytes]) -> None:
    """Read a pipe to EOF, appending chunks (process must be finished)."""
    while True:
        data = os.read(stream.fileno(), 65536)
        if not data:
            return
        chunks.append(data)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")


def format_run_context(issue: int | None, run_id: str | None,
                       role: str, source_repo: str | None,
                       branch: str | None, worktree: Path) -> str:
    """Format the run context carried by every journal line (Issue #18)."""
    return (
        f"issue={issue} run_id={run_id or '-'} role={role} "
        f"source_repo={source_repo or '-'} branch={branch or '-'} "
        f"worktree={worktree}"
    )


def format_elapsed_seconds(seconds: float) -> str:
    """Format an elapsed duration as whole seconds (`45s`)."""
    return f"{max(0, int(seconds))}s"


def _log_pi_activity(activity: dict, context: str, *,
                     idle_warn_seconds: float,
                     idle_warned: bool) -> bool:
    """Log activity events and idle warnings; track the idle state.

    A new session event logs a `pi_event` line immediately (and a
    `pi_resumed` line when it follows an idle warning). When no event
    arrives for `idle_warn_seconds`, a `pi_idle` warning with the full
    scene is logged once. Returns the updated idle-warning state.
    """
    scene = format_activity_scene(activity)
    if activity["changed"]:
        if idle_warned:
            LOGGER.info("pi_resumed %s %s", context, scene)
        LOGGER.info("pi_event %s %s", context, scene)
        return False
    if activity["stale_seconds"] >= idle_warn_seconds:
        if not idle_warned:
            LOGGER.warning(
                "pi_idle %s %s stale_seconds=%.0f",
                context, scene, activity["stale_seconds"],
            )
            return True
    return idle_warned


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS,
    heartbeat_seconds: float = PI_HEARTBEAT_SECONDS,
    log_command: list[str] | None = None,
    issue: int | None = None,
    run_id: str | None = None,
    role: str = "implement",
    source_repo: str | None = None,
    branch: str | None = None,
) -> str:
    """Run Pi and stream its live session activity into the journal.

    Every poll, the newest Pi session JSONL activity is checked against the
    run context (issue, run id, role, source repo, branch, worktree):

    - a new session event logs a `pi_event` line immediately (phase, last
      activity time, sanitized last action, session id);
    - a `pi_heartbeat` line is logged with the same fields plus elapsed
      time so that the gap between consecutive heartbeat/event lines never
      exceeds `heartbeat_seconds` (the default 30 s), even while the
      session is quiet, so an open `journalctl -f` always shows a live
      line;
    - when no new event arrives for `idle_warn_seconds`, a `pi_idle`
      warning with the full scene is logged once, and the first new event
      after it logs a `pi_resumed` line.

    On a non-zero exit the scene is logged before the error is raised. The
    session JSONL stays in the worktree as the complete local record; the
    full prompt and Issue body are never logged.
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    watcher = SessionWatcher(cwd / ".pi-session")
    context = format_run_context(
        issue, run_id, role, source_repo, branch, cwd,
    )
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    start_time = time.monotonic()
    activity = watcher.poll()
    idle_warned = False
    last_heartbeat = 0.0
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
            idle_warned = _log_pi_activity(
                activity, context,
                idle_warn_seconds=idle_warn_seconds,
                idle_warned=idle_warned,
            )
            elapsed = time.monotonic() - start_time
            # The poll runs every `poll_interval`, so firing at
            # `heartbeat_seconds - poll_interval` guarantees the gap
            # between consecutive heartbeat/event lines stays within
            # `heartbeat_seconds`.
            if elapsed - last_heartbeat >= heartbeat_seconds - poll_interval:
                last_heartbeat = elapsed
                LOGGER.info(
                    "pi_heartbeat %s %s elapsed=%s",
                    context, format_activity_scene(activity),
                    format_elapsed_seconds(elapsed),
                )
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
           role: str = "implement") -> str:
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
        "Complete the delivery process in the system prompt."
    )
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
        run_id=config["run_id"],
        role=role,
        source_repo=source_repo,
        branch=branch,
    )


def verify_pr(worktree: Path, branch: str, base_branch: str) -> str:
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    # Re-fetch before judging: the delivery must contain the latest remote
    # base, otherwise it is behind and the PR is rejected (fail fast).
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
            f"origin/{base_branch}; merge the latest base, rerun full tests "
            "and review, then retry"
        ) from None
    local_head = run_command(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
    )
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "url,baseRefName,headRefOid", "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    url = prs[0].get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
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
    return url


def delivery_head_advanced(worktree: Path, base_sha: str) -> bool:
    """True when the task branch has commits beyond the frozen base."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head != base_sha


def read_test_result(worktree: Path) -> str | None:
    """Summarize the worktree's `test.log`, or None when it does not exist."""
    path = worktree / "test.log"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("=", "FAILED", "ERROR")) or "passed" in line:
            return sanitize(line)
    return sanitize(text.splitlines()[-1]) if text.strip() else None


def _progress_state(*, issue: int, run_id: str, role: str, branch: str,
                    worktree: Path, started: float, pr_url: str | None,
                    review_round: int) -> dict:
    """Collect the current run state for the GitHub progress comment.

    Activity snapshotting is best-effort observability: a read failure is
    logged and reported as "no session yet", it never blocks the task.
    """
    try:
        snapshot = activity_snapshot(worktree / ".pi-session")
    except Exception:
        LOGGER.exception("issue=%s activity snapshot failed", issue)
        snapshot = None
    tests = read_test_result(worktree)
    return {
        "run_id": run_id,
        "issue": issue,
        "role": role,
        "phase": (snapshot or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (snapshot or {}).get("last_activity"),
        "last_action": (snapshot or {}).get("last"),
        "tests": tests,
        "review_round": review_round,
        "branch": branch,
        "pr": pr_url,
        "session": (snapshot or {}).get("session_id"),
    }


def _progress_body(state: dict, *, outcome: str | None = None) -> str:
    """Render the progress body, optionally with a final outcome header."""
    body = progress_body(state)
    if outcome is None:
        return body
    return f"{outcome}\n\n{body}"


def _publish_plan_milestone(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the `plan ready` milestone once the worktree has a plan.md."""
    if (worktree / "plan.md").is_file():
        publisher.milestone("plan ready")


def _publish_test_milestone(publisher: ProgressPublisher,
                            worktree: Path) -> None:
    """Post `tests passed` / `tests failed` from the worktree's test.log."""
    result = read_test_result(worktree)
    if result is None:
        return
    if "failed" in result or "error" in result:
        publisher.milestone(f"tests failed: {result}")
    else:
        publisher.milestone(f"tests passed: {result}")


def _failure_detail(exc: BaseException) -> str:
    """One-line failure description; keeps subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()}"
    return detail


def process_issue(issue: dict, config: dict, source_repo: str,
                  role: str = "implement") -> str:
    number = int(issue["number"])
    base_branch = config["base_branch"]
    base_sha = freeze_base(config["repo_dir"], base_branch)
    run_id = new_run_id()
    # Restart resume (Issue #18): a killed runner leaves the task
    # worktree and the `ai-in-progress` label behind. Only in that state
    # the newest worktree's run id is reused, so the same hidden-marker
    # progress comment is found and kept instead of a second one.
    # Completed runs keep their worktrees as evidence but lose the label,
    # so re-claiming an issue always starts a fresh run.
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config["repo_dir"], source_repo, number,
        )
        if existing_run_id is not None:
            LOGGER.info(
                "issue=%s resuming_run run_id=%s",
                number, existing_run_id,
            )
            run_id = existing_run_id
    branch = task_branch(source_repo, number, run_id)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    edit_issue(number, repo=source_repo, add="ai-in-progress")
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    worktree: Path | None = None
    started = time.monotonic()
    try:
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, base_sha,
        )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        state = _progress_state(
            issue=number, run_id=run_id, role=role, branch=branch,
            worktree=worktree, started=started, pr_url=None,
            review_round=0,
        )
        publisher.ensure(_progress_body(state))
        publisher.milestone(
            f"started: {run_info} branch={branch} worktree={worktree}"
        )
        run_pi(issue, worktree, config, source_repo, branch=branch, role=role)
        _publish_plan_milestone(publisher, worktree)
        _publish_test_milestone(publisher, worktree)
        pr_url = verify_pr(worktree, branch, base_branch)
        if delivery_head_advanced(worktree, base_sha):
            publisher.milestone(f"fix pushed: branch={branch}")
        edit_issue(
            number, repo=source_repo, add="ai-pr-opened",
            remove="ai-in-progress",
        )
        publisher.milestone(f"PR opened: {pr_url} ({run_info})")
        state = _progress_state(
            issue=number, run_id=run_id, role=role, branch=branch,
            worktree=worktree, started=started, pr_url=pr_url,
            review_round=0,
        )
        publisher.finish(_progress_body(
            state, outcome="**Muyan Pilot delivered**",
        ))
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
                number, repo=source_repo, add="ai-blocked",
                remove="ai-in-progress",
            )
            detail = _failure_detail(exc)
            body = f"Muyan Pilot failed: {detail} ({run_info})"
            if scene:
                body += f" {scene}"
            comment_issue(number, repo=source_repo, body=body)
            if worktree is not None:
                state = _progress_state(
                    issue=number, run_id=run_id, role=role,
                    branch=branch, worktree=worktree, started=started,
                    pr_url=None, review_round=0,
                )
                publisher.finish(_progress_body(
                    state,
                    outcome=(
                        "**Muyan Pilot blocked**\n\n"
                        f"failure: {detail}\n"
                        f"next step: fix the failure above and re-run "
                        "this Issue (a new run id is created automatically)"
                    ),
                ))
                publisher.milestone(
                    f"blocked: {sanitize(detail)} ({run_info})"
                )
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
    selected = pick_next_issue(config["source_repos"])
    if selected is None:
        LOGGER.info("source_repos=%s outcome=no_ready_issue", config["source_repos"])
        return 0
    source_repo, issue = selected
    process_issue(issue, config, source_repo)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
