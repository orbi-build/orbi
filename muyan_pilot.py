#!/usr/bin/env python3
"""Muyan Pilot task dispatch and status CLI.

`add` creates an Issue in a configured source repo and labels it `ai-ready`.
`status` reports the current in-progress Issue (with its live Pi activity:
phase, last activity time, the last meaningful action, the newest tool
call result, session file and worktree), the next ready Issue, and the
most recent result (`ai-pr-opened` / `ai-fix-needed` / `ai-blocked`) per
source repo.

GitHub Issues and labels are the only state store. There is no database,
queue, or web UI. Command failures are logged and raised by the reused
bootstrap_runner.run_command; there is no fallback.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path

from bootstrap_runner import (
    RunIdFilter,
    freeze_base,
    load_config,
    log_format,
    parse_issue_array,
    run_command,
    validate_config,
)
from pi_activity import activity_snapshot

LOGGER = logging.getLogger("muyan_pilot.cli")
# Same run correlation mechanism as the runner (Issue #41): when a run id is
# bound, every journal line of this process starts with `[run_id]`.
LOGGER.addFilter(RunIdFilter())

ISSUE_URL_PATTERN = re.compile(r"/issues/(\d+)$")
READY_LABEL = "ai-ready"
IN_PROGRESS_LABEL = "ai-in-progress"
# `ai-pr-opened` (awaiting review), `ai-fix-needed` (Fixer pending) and
# `ai-blocked` are all result states of an opened delivery (Issue #45).
RESULT_LABELS = ("ai-pr-opened", "ai-fix-needed", "ai-blocked")


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
                        search: str | None = None) -> list[dict]:
    """Return the newest Issues matching a label search (gh lists newest first)."""
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", state,
        "--search", search or f"label:{label}",
        "--json", "number,title,url,state", "--limit", "1",
    ])
    return parse_issue_array(raw)


def current_issue(repo: str) -> dict | None:
    issues = list_labeled_issues(repo, IN_PROGRESS_LABEL)
    return issues[0] if issues else None


def ready_issue(repo: str) -> dict | None:
    issues = list_labeled_issues(
        repo, READY_LABEL, search=f"label:{READY_LABEL} -label:{IN_PROGRESS_LABEL}",
    )
    return issues[0] if issues else None


def recent_result(repo: str) -> dict | None:
    """Return the newest delivery result Issue, any state.

    Result states: `ai-pr-opened` (awaiting review), `ai-fix-needed`
    (Fixer pending) and `ai-blocked` (needs human attention).
    """
    newest = None
    for label in RESULT_LABELS:
        for issue in list_labeled_issues(repo, label, state="all"):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    return f"#{issue['number']} {issue['title']} {issue['url']}"


def latest_task_worktree(repo_dir: Path, source_repo: str,
                         number: int) -> Path | None:
    """Return the newest task worktree for an Issue, or None."""
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/muyan-pilot-{slug}-issue-{number}-*"
    candidates = [
        path for path in repo_dir.glob(pattern) if path.is_dir()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def live_activity_lines(repo_dir: Path, source_repo: str,
                        issue: dict) -> list[str]:
    """Return the live Pi activity lines for an in-progress Issue."""
    worktree = latest_task_worktree(repo_dir, source_repo, int(issue["number"]))
    if worktree is None:
        return ["    live: no task worktree found"]
    snapshot = activity_snapshot(worktree / ".pi-session")
    if snapshot is None:
        return [
            "    live: no pi session yet",
            f"    worktree: {worktree}",
        ]
    return [
        (
            f"    live: phase={snapshot['phase']} "
            f"last_activity={snapshot['last_activity'] or '-'} "
            f"action={snapshot['action'] or '-'} "
            f"result={snapshot['result'] or '-'}"
        ),
        f"    session: {snapshot['session_file']}",
        f"    worktree: {worktree}",
    ]


def status_report(config: dict) -> str:
    lines = []
    for repo in config["source_repos"]:
        lines.append(f"source: {repo}")
        base_sha = freeze_base(config["repo_dir"], config["base_branch"])
        lines.append(f"  base: {config['base_branch']} {base_sha}")
        current = current_issue(repo)
        lines.append(f"  current: {format_issue(current) if current else '-'}")
        if current is not None:
            lines.extend(
                live_activity_lines(config["repo_dir"], repo, current),
            )
        for name, lookup in (
            ("ready", ready_issue),
            ("result", recent_result),
        ):
            issue = lookup(repo)
            lines.append(f"  {name}: {format_issue(issue) if issue else '-'}")
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
        help="show current Issue (with live Pi activity), ready queue and recent result",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=log_format())

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
