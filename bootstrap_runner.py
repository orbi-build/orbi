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
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from pi_session import (
    PHASE_PR,
    parse_session_events,
    session_file_for,
)


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Live activity polling defaults (seconds).
POLL_INTERVAL = 1.0
STALL_TIMEOUT = 300.0


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
    return {
        "source_repos": source_repos,
        "repo_dir": _config_path(data.get("repo_dir", "."), base),
        "workspace_root": _config_path(data.get("workspace_root", ".."), base),
        "prompt": _config_path(data.get("prompt", "prompt.md"), base),
        "skills": [_config_path(item, base) for item in data.get("skills", [])],
        "context_files": [
            _config_path(item, base) for item in data.get("context_files", [])
        ],
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


def task_branch(source_repo: str, number: int) -> str:
    return f"muyan-pilot/{source_repo.replace('/', '-')}-issue-{number}"


def worktree_path(source_repo: str, number: int) -> Path:
    slug = source_repo.replace("/", "-")
    return Path(tempfile.gettempdir()) / f"muyan-pilot-{slug}-issue-{number}"


def create_worktree(repo_dir: Path, source_repo: str, number: int) -> Path:
    path = worktree_path(source_repo, number)
    if path.exists():
        raise RuntimeError(f"worktree path already exists: {path}")
    branch = task_branch(source_repo, number)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), "HEAD",
    ], cwd=repo_dir)
    return path


def watch_pi_session(process, *, worktree: Path, issue: dict, source_repo: str,
                     branch: str, started: float, command: list[str],
                     poll_interval: float = POLL_INTERVAL,
                     stall_timeout: float = STALL_TIMEOUT,
                     timeout: int | None = None,
                     on_phase=None) -> int:
    """Poll the Pi session JSONL and log live activity until Pi exits.

    Every poll re-reads the session file and logs each new event once
    (phase, timestamp, sanitized summary). Warns when no new event arrives
    within `stall_timeout` seconds and always logs a final scene line
    (return code, last activity, session file) so a failed or stalled run
    can be located from the journal alone. Returns the process return code.
    """
    number = issue["number"]
    seen = 0
    last_activity_at = started
    last_activity = None

    def log_new_events(session_file: Path | None) -> None:
        nonlocal seen, last_activity_at, last_activity
        if session_file is None:
            return
        for event in parse_session_events(session_file)[seen:]:
            seen += 1
            last_activity_at = time.time()
            last_activity = (event["phase"], event["summary"], event["timestamp"])
            LOGGER.info(
                "pi_activity issue=%s source_repo=%s branch=%s worktree=%s "
                "session=%s phase=%s at=%s summary=%s",
                number, source_repo, branch, worktree, session_file.name,
                event["phase"], event["timestamp"], event["summary"],
            )
            if on_phase is not None:
                on_phase(event)

    while True:
        now = time.time()
        if timeout is not None and now - started >= timeout:
            process.kill()
            process.wait()
            LOGGER.error("pi_timeout issue=%s timeout=%s", number, timeout)
            raise subprocess.TimeoutExpired(command, timeout)
        session_file = session_file_for(worktree, started)
        log_new_events(session_file)
        idle_for = now - last_activity_at
        if idle_for >= stall_timeout:
            LOGGER.warning(
                "pi_stalled issue=%s idle_seconds=%s last_activity=%s "
                "last_summary=%s last_at=%s session=%s",
                number, int(idle_for),
                last_activity[0] if last_activity else "none",
                last_activity[1] if last_activity else "-",
                last_activity[2] if last_activity else "-",
                session_file.name if session_file else "-",
            )
            last_activity_at = now
        returncode = process.poll()
        if returncode is not None:
            session_file = session_file_for(worktree, started)
            log_new_events(session_file)
            LOGGER.info(
                "pi_finished issue=%s source_repo=%s branch=%s worktree=%s "
                "session=%s returncode=%s last_activity=%s last_summary=%s last_at=%s",
                number, source_repo, branch, worktree,
                session_file.name if session_file else "-",
                returncode,
                last_activity[0] if last_activity else "none",
                last_activity[1] if last_activity else "-",
                last_activity[2] if last_activity else "-",
            )
            return returncode
        time.sleep(poll_interval)


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           timeout: int | None = None, on_phase=None) -> str:
    """Run one Pi session in the worktree, streaming live activity to the log.

    Pi stdout is not captured into the journal until the process exits; the
    live view comes from the session JSONL via watch_pi_session. A nonzero
    exit still raises CalledProcessError, exactly like run_command did.
    """
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
    branch = task_branch(source_repo, issue["number"])
    LOGGER.info(
        "pi_session=%s issue=%s source_repo=%s branch=%s worktree=%s",
        worktree / ".pi-session", issue["number"], source_repo, branch, worktree,
    )
    started = time.time()
    process = subprocess.Popen(
        command, cwd=worktree,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        returncode = watch_pi_session(
            process, worktree=worktree, issue=issue, source_repo=source_repo,
            branch=branch, started=started, command=command, timeout=timeout,
            on_phase=on_phase,
        )
    except BaseException:
        process.kill()
        process.wait()
        raise
    output = process.stdout.read() if process.stdout else ""
    if returncode != 0:
        LOGGER.error(
            "pi_failed issue=%s returncode=%s stdout=%s",
            issue["number"], returncode, (output or "").strip()[-2000:],
        )
        raise subprocess.CalledProcessError(returncode, command, output=output)
    return (output or "").strip()


def verify_pr(worktree: Path, branch: str) -> str:
    current_branch = run_command(
        ["git", "branch", "--show-current"], cwd=worktree,
    )
    if current_branch != branch:
        raise RuntimeError(
            f"Pi changed branch: expected={branch} actual={current_branch}"
        )
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "url", "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    url = prs[0].get("url")
    if not url:
        raise RuntimeError("open PR has no URL")
    return url


def process_issue(issue: dict, config: dict, source_repo: str) -> str:
    number = int(issue["number"])
    branch = task_branch(source_repo, number)
    edit_issue(number, repo=source_repo, add="ai-in-progress")
    milestone_commented = False

    def on_milestone(event: dict) -> None:
        nonlocal milestone_commented
        if milestone_commented or event["phase"] != PHASE_PR:
            return
        milestone_commented = True
        try:
            comment_issue(
                number, repo=source_repo,
                body=(
                    f"Muyan Pilot: key phase `{event['phase']}` reached "
                    f"({event['summary']}) at {event['timestamp']}."
                ),
            )
        except Exception:
            LOGGER.exception("issue=%s milestone comment failed", number)

    try:
        worktree = create_worktree(config["repo_dir"], source_repo, number)
        run_pi(issue, worktree, config, source_repo, on_phase=on_milestone)
        pr_url = verify_pr(worktree, branch)
        edit_issue(
            number, repo=source_repo, add="ai-pr-opened",
            remove="ai-in-progress",
        )
        comment_issue(
            number, repo=source_repo,
            body=f"Muyan Pilot opened PR: {pr_url}",
        )
        return pr_url
    except Exception as exc:
        LOGGER.exception("issue=%s failed", number)
        try:
            edit_issue(
                number, repo=source_repo, add="ai-blocked",
                remove="ai-in-progress",
            )
            comment_issue(
                number, repo=source_repo,
                body=f"Muyan Pilot failed: {exc}",
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
