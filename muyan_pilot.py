#!/usr/bin/env python3
"""Muyan Pilot task dispatch and status CLI.

`add` creates an Issue in a configured source repo and labels it `ai-ready`.
`status` reports the current in-progress Issue (including its live Pi session
activity: phase, last activity time and a sanitized summary), the next ready
Issue, and the most recent result (`ai-pr-opened` / `ai-blocked`) per source
repo.

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
    load_config,
    parse_issue_array,
    run_command,
    validate_config,
    worktree_path,
)
from pi_session import newest_session_file, summarize_session_file

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
    """Return the newest `ai-pr-opened` or `ai-blocked` Issue, any state."""
    newest = None
    for label in RESULT_LABELS:
        for issue in list_labeled_issues(repo, label, state="all"):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    return f"#{issue['number']} {issue['title']} {issue['url']}"


def live_status(repo: str, issue: dict) -> str:
    """One-line live scene for an in-progress Issue from its Pi session."""
    worktree = worktree_path(repo, int(issue["number"]))
    if not worktree.is_dir():
        return "live: worktree missing"
    session_file = newest_session_file(worktree)
    if session_file is None:
        return "live: no session file yet"
    _, activity = summarize_session_file(session_file)
    if activity is None:
        return f"live: session={session_file.name} (no events yet)"
    phase, summary, at = activity
    return (
        f"live: phase={phase} at={at} summary={summary} "
        f"session={session_file.name}"
    )


def status_report(config: dict) -> str:
    lines = []
    for repo in config["source_repos"]:
        lines.append(f"source: {repo}")
        current = current_issue(repo)
        for name, issue in (
            ("current", current),
            ("ready", ready_issue(repo)),
            ("result", recent_result(repo)),
        ):
            lines.append(f"  {name}: {format_issue(issue) if issue else '-'}")
        if current is not None:
            lines.append(f"  {live_status(repo, current)}")
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
