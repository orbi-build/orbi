#!/usr/bin/env python3
"""Muyan Pilot task dispatch and status CLI.

`add` creates an Issue in a configured source repo and labels it `ai-ready`.
`status` is the one-command daily check: systemd timer/service state and next
trigger, the current in-progress Issue, the ready queue, the most recent
result (`ai-pr-opened` / `ai-blocked`), the active worktree/stage/Pi session,
and journal/session troubleshooting entry points.

GitHub Issues and labels are the only state store. There is no database,
queue, or web UI. Command failures are logged and raised by the reused
bootstrap_runner.run_command; there is no fallback.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from bootstrap_runner import (
    PLAN_FILE,
    TEST_LOG_FILE,
    VERIFY_FILE,
    latest_session_file,
    load_config,
    parse_issue_array,
    run_command,
    validate_config,
)

LOGGER = logging.getLogger("muyan_pilot.cli")

ISSUE_URL_PATTERN = re.compile(r"/issues/(\d+)$")
READY_LABEL = "ai-ready"
IN_PROGRESS_LABEL = "ai-in-progress"
RESULT_LABELS = ("ai-pr-opened", "ai-blocked")


def issue_number(url: str) -> int:
    """Extract the Issue number from a GitHub Issue URL."""
    match = ISSUE_URL_PATTERN.search(url)
    if not match:
        raise ValueError(f"no issue number in URL: {url}")
    return int(match.group(1))


def create_issue(repo: str, title: str, body: str) -> str:
    """Create an Issue via gh and return its URL."""
    return run_command([
        "gh", "issue", "create", "--repo", repo,
        "--title", title, "--body", body,
    ])


def dispatch_issue(repo: str, title: str, body: str) -> str:
    """Create an Issue and mark it `ai-ready`; return the Issue URL."""
    url = create_issue(repo, title, body)
    run_command([
        "gh", "issue", "edit", str(issue_number(url)), "--repo", repo,
        "--add-label", READY_LABEL,
    ])
    return url


def list_labeled_issues(repo: str, label: str, state: str = "open",
                        search: str | None = None, limit: int = 1) -> list[dict]:
    """Return the newest Issues matching a label search (gh lists newest first)."""
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", state,
        "--search", search or f"label:{label}",
        "--json", "number,title,url,state", "--limit", str(limit),
    ])
    return parse_issue_array(raw)


def current_issue(repo: str) -> dict | None:
    issues = list_labeled_issues(repo, IN_PROGRESS_LABEL)
    return issues[0] if issues else None


def ready_issues(repo: str) -> list[dict]:
    """Return the ready queue (newest first), excluding in-progress Issues."""
    return list_labeled_issues(
        repo, READY_LABEL, search=f"label:{READY_LABEL} -label:{IN_PROGRESS_LABEL}",
        limit=10,
    )


def recent_result(repo: str) -> dict | None:
    """Return the newest `ai-pr-opened` or `ai-blocked` Issue, any state."""
    newest = None
    for label in RESULT_LABELS:
        for issue in list_labeled_issues(repo, label, state="all"):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    return f"#{issue['number']} {issue['title']} {issue['url']}"


def systemd_status() -> list[str]:
    """Report the pilot timer/service state and the next timer trigger."""
    try:
        timer_state = run_command([
            "systemctl", "--user", "show", "muyan-pilot.timer",
            "--property=ActiveState", "--value",
        ])
        service_state = run_command([
            "systemctl", "--user", "show", "muyan-pilot.service",
            "--property=ActiveState", "--value",
        ])
        raw_next = run_command([
            "systemctl", "--user", "list-timers", "muyan-pilot.timer",
            "--no-legend", "--plain",
        ])
    except (subprocess.CalledProcessError, OSError) as exc:
        return [f"  systemd: unavailable ({exc})"]
    if service_state == "active":
        # A timer does not schedule the next elapse while its service runs.
        next_trigger = "waiting for service to finish"
    else:
        parts = raw_next.split()
        next_trigger = " ".join(parts[:4]) if len(parts) >= 4 else "-"
    return [
        f"  timer: {timer_state or '-'}",
        f"  service: {service_state or '-'}",
        f"  next trigger: {next_trigger}",
    ]


def worktree_list(config: dict) -> str:
    """Return `git worktree list --porcelain` for the configured repo."""
    return run_command(
        ["git", "worktree", "list", "--porcelain"], cwd=config["repo_dir"],
    )


def _worktree_entries(raw: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into path/branch entries."""
    entries = []
    current = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            current = {"path": Path(line.split(" ", 1)[1]), "branch": ""}
            entries.append(current)
        elif line.startswith("branch ") and current is not None:
            current["branch"] = line.split(" ", 1)[1]
    return entries


def session_info(session_file: str) -> dict[str, str]:
    """Read the first Pi session record; '-' when missing or unreadable."""
    if session_file == "-":
        return {"id": "-", "cwd": "-"}
    try:
        first_line = Path(session_file).read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return {"id": "-", "cwd": "-"}
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError:
        return {"id": "-", "cwd": "-"}
    return {"id": record.get("id", "-"), "cwd": record.get("cwd", "-")}


def stage_for(worktree: Path) -> str:
    """Derive the current stage from worktree artifacts (newest wins)."""
    if (worktree / VERIFY_FILE).is_file():
        return "verify"
    if (worktree / TEST_LOG_FILE).is_file():
        return "testing"
    if (worktree / PLAN_FILE).is_file():
        return "planning"
    return "started"


def current_run(config: dict) -> list[str]:
    """Describe pilot worktrees on disk: branch, stage and Pi session."""
    entries = _worktree_entries(worktree_list(config))
    lines = []
    for entry in entries:
        if not entry["path"].name.startswith("muyan-pilot-"):
            continue
        lines.append(f"  worktree: {entry['path']}")
        lines.append(f"  branch: {entry['branch'] or '-'}")
        lines.append(f"  stage: {stage_for(entry['path'])}")
        session_dir = entry["path"] / ".pi-session"
        session_file = latest_session_file(session_dir)
        info = session_info(session_file)
        lines.append(f"  pi_session: {session_dir}")
        lines.append(f"  pi_session_file: {session_file}")
        lines.append(f"  session id: {info['id']}")
        lines.append(f"  session cwd: {info['cwd']}")
    return lines or ["  worktree: -"]


def newest_session_file(config: dict) -> str:
    """Return the newest Pi session file across pilot worktrees, '-' if none."""
    entries = _worktree_entries(worktree_list(config))
    files = [
        latest_session_file(entry["path"] / ".pi-session")
        for entry in entries
        if entry["path"].name.startswith("muyan-pilot-")
    ]
    files = [item for item in files if item != "-"]
    if not files:
        return "-"
    # Session file names start with an ISO timestamp, so the newest file has
    # the largest name (worktree paths differ and must not decide the order).
    return max(files, key=lambda item: Path(item).name)


def troubleshooting(config: dict) -> list[str]:
    """Point at the journal and the newest Pi session for debugging."""
    return [
        "  journal: journalctl --user -u muyan-pilot.service --since today",
        f"  session: {newest_session_file(config)}",
    ]


def status_report(config: dict) -> str:
    lines = []
    for repo in config["source_repos"]:
        lines.append(f"source: {repo}")
        current = current_issue(repo)
        lines.append(f"  current: {format_issue(current) if current else '-'}")
        queue = ready_issues(repo)
        if queue:
            for issue in queue:
                lines.append(f"  ready: {format_issue(issue)}")
        else:
            lines.append("  ready: -")
        result = recent_result(repo)
        lines.append(f"  result: {format_issue(result) if result else '-'}")
    lines.append("systemd:")
    lines.extend(systemd_status())
    lines.append("current run:")
    lines.extend(current_run(config))
    lines.append("troubleshooting:")
    lines.extend(troubleshooting(config))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("MUYAN_PILOT_CONFIG", "muyan-pilot.toml")),
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser(
        "add", parents=[common],
        help="create an Issue in a source repo and add ai-ready",
    )
    add_parser.add_argument("title")
    add_parser.add_argument("--body", default="")
    add_parser.add_argument(
        "--repo", default=None,
        help="source repo (default: first configured source)",
    )
    subparsers.add_parser(
        "status", parents=[common],
        help="one-command check: systemd, current run, ready queue, recent result, troubleshooting",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    validate_config(config)
    if args.command == "add":
        repo = args.repo or config["source_repos"][0]
        if repo not in config["source_repos"]:
            parser.error(
                f"--repo must be one of: {', '.join(config['source_repos'])}"
            )
        LOGGER.info("dispatch repo=%s title=%s", repo, args.title)
        url = dispatch_issue(repo, args.title, args.body)
        print(f"created: {url}")
        print(f"label: {READY_LABEL}")
    else:
        print(status_report(config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
