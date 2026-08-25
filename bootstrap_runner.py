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
    format_duration,
    format_end_scene,
    format_run_scene,
    quote_value,
)


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Live activity polling while Pi runs (Issue #24): every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0

# The bootstrap runner drives a single implementation session per run
# (Issue #40: implement/review/fix/merge share the same line format and
# carry their role; the MVP runs one role).
ROLE_IMPLEMENT = "implement"


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


def issue_context(source_repo: str, number: int) -> str:
    """Issue reference used on every journal line: `owner/repo#number`."""
    return f"{source_repo}#{number}"


def log_format() -> str:
    """Journal log format without a Python timestamp (Issue #40).

    systemd journal already provides time, host and process on every
    line; printing `%(asctime)s` again only duplicates information.
    """
    return "%(levelname)s %(message)s"


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


def _log_activity(activity: dict, *, run_id: str, issue_ref: str,
                  role: str) -> None:
    """Log one short activity line with the changed fields only."""
    LOGGER.info(
        "activity run=%s issue=%s role=%s phase=%s action=%s result=%s "
        "idle=%s",
        run_id, issue_ref, role, activity["phase"],
        quote_value(activity["action"] or "-"),
        activity["result"] or "-",
        format_duration(activity["stale_seconds"]),
    )


def _log_heartbeat(activity: dict, *, run_id: str, issue_ref: str,
                   role: str, elapsed: float) -> None:
    """Log one heartbeat line when nothing changed since the last poll."""
    LOGGER.info(
        "heartbeat run=%s issue=%s role=%s phase=%s elapsed=%s idle=%s",
        run_id, issue_ref, role, activity["phase"],
        format_duration(elapsed), format_duration(activity["stale_seconds"]),
    )


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    run_id: str,
    issue: int,
    source_repo: str,
    branch: str,
    log_command: list[str] | None = None,
) -> str:
    """Run Pi and stream concise live activity into the journal (Issue #40).

    The full invariant scene (branch, worktree, session file) is logged
    once as `run_start`. While Pi runs, only short changed fields are
    logged: `activity` when phase/action/result change, `heartbeat` at
    the poll interval otherwise (the idle time rides on the line). On a
    non-zero exit or timeout a `run_failed` line carries the full scene
    again as the debug entry. The caller logs `run_end` once the PR and
    commit are known. The session JSONL stays in the worktree as the
    complete local record; the full prompt and Issue body are never
    logged.
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    issue_ref = issue_context(source_repo, issue)
    watcher = SessionWatcher(cwd / ".pi-session")
    start = time.monotonic()
    # The initial state is what run_start already reported; activity lines
    # are only emitted when the visible fields actually change.
    initial = watcher.poll()
    last_visible = (initial["phase"], initial["action"], initial["result"])
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    LOGGER.info(
        "run_start %s",
        format_run_scene(
            initial, run_id=run_id, issue=issue_ref,
            role=ROLE_IMPLEMENT, branch=branch, worktree=str(cwd),
        ),
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
            visible = (
                activity["phase"], activity["action"], activity["result"],
            )
            if visible != last_visible:
                # Only changed fields are repeated; an unchanged poll is a
                # heartbeat (Issue #40).
                _log_activity(
                    activity, run_id=run_id, issue_ref=issue_ref,
                    role=ROLE_IMPLEMENT,
                )
                last_visible = visible
            else:
                _log_heartbeat(
                    activity, run_id=run_id, issue_ref=issue_ref,
                    role=ROLE_IMPLEMENT, elapsed=time.monotonic() - start,
                )
            if process.poll() is not None:
                break
    finally:
        _drain_stream(process.stdout, stdout_chunks)
        _drain_stream(process.stderr, stderr_chunks)
    stdout = _decode_chunks(stdout_chunks)
    stderr = _decode_chunks(stderr_chunks)
    if timed_out:
        reason = f"timeout_{format_duration(timeout)}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=ROLE_IMPLEMENT, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.TimeoutExpired(
            safe_command, timeout, output=stdout, stderr=stderr,
        )
    if process.returncode != 0:
        reason = f"pi_exit_{process.returncode}"
        LOGGER.error(
            "run_failed %s reason=%s",
            format_run_scene(
                activity, run_id=run_id, issue=issue_ref,
                role=ROLE_IMPLEMENT, branch=branch, worktree=str(cwd),
            ),
            reason,
        )
        raise subprocess.CalledProcessError(
            process.returncode, safe_command, output=stdout, stderr=stderr,
        )
    if stderr:
        LOGGER.info("stderr=%s", stderr.rstrip())
    LOGGER.info("stdout=%s", stdout.rstrip())
    return stdout.strip()


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           branch: str, timeout: int | None = None) -> str:
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
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", "--print", "--session-dir", str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=int(issue["number"]),
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


def process_issue(issue: dict, config: dict, source_repo: str) -> str:
    number = int(issue["number"])
    base_branch = config["base_branch"]
    base_sha = freeze_base(config["repo_dir"], base_branch)
    run_id = new_run_id()
    branch = task_branch(source_repo, number, run_id)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    edit_issue(number, repo=source_repo, add="ai-in-progress")
    worktree: Path | None = None
    started = time.monotonic()
    try:
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, base_sha,
        )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        comment_issue(
            number, repo=source_repo,
            body=(
                f"Muyan Pilot started Pi: {run_info} branch={branch} "
                f"worktree={worktree}"
            ),
        )
        run_pi(issue, worktree, config, source_repo, branch=branch)
        pr_url = verify_pr(worktree, branch, base_branch)
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        edit_issue(
            number, repo=source_repo, add="ai-pr-opened",
            remove="ai-in-progress",
        )
        comment_issue(
            number, repo=source_repo,
            body=f"Muyan Pilot opened PR: {pr_url} ({run_info})",
        )
        LOGGER.info(
            "run_end %s",
            format_end_scene(
                run_id=run_id, issue=issue_context(source_repo, number),
                role=ROLE_IMPLEMENT, result="pr_opened",
                elapsed=time.monotonic() - started,
                pr=pr_url, commit=commit,
            ),
        )
        return pr_url
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        scene = ""
        if worktree is not None:
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
                if snapshot is None:
                    # No session file yet: the scene still carries the full
                    # debug entry (worktree, branch) with '-' session fields.
                    snapshot = {
                        "session_id": None, "session_file": None,
                        "phase": "starting", "last_activity": None,
                        "action": None, "result": None,
                    }
                scene = format_run_scene(
                    snapshot,
                    run_id=run_id, issue=issue_context(source_repo, number),
                    role=ROLE_IMPLEMENT, branch=branch, worktree=str(worktree),
                )
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
        try:
            edit_issue(
                number, repo=source_repo, add="ai-blocked",
                remove="ai-in-progress",
            )
            body = f"Muyan Pilot failed: {exc} ({run_info})"
            if scene:
                body += f" {scene}"
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
    logging.basicConfig(level=logging.INFO, format=log_format())

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
