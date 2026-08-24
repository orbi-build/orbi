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
import shutil
import subprocess
import tempfile
from pathlib import Path


DEFAULT_SOURCE_REPO = "xqliu/muyan-ceo"
DEFAULT_REPO_DIR = Path("/home/xqianliu/Documents/muyan/muyan-pilot")
DEFAULT_PROMPT = Path(__file__).with_name("prompt.md")
COMMAND_TIMEOUT = 1800
LOGGER = logging.getLogger("muyan_pilot.bootstrap")


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int = COMMAND_TIMEOUT) -> str:
    """Run one external command; log context and fail fast on any error."""
    LOGGER.info("command=%s cwd=%s", " ".join(command), cwd or Path.cwd())
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    return result.stdout.strip()


def parse_issue_list(raw: str) -> dict | None:
    """Return the first issue from gh's JSON array, or None when idle."""
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("issue list must be a JSON array")
    return issues[0] if issues else None


def pick_issue(repo: str) -> dict | None:
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened -label:ai-blocked",
        "--json", "number,title,body", "--limit", "1",
    ])
    return parse_issue_list(raw)


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


def worktree_path(number: int) -> Path:
    return Path(tempfile.gettempdir()) / f"muyan-pilot-issue-{number}"


def create_worktree(repo_dir: Path, number: int) -> Path:
    path = worktree_path(number)
    if path.exists():
        raise RuntimeError(f"worktree path already exists: {path}")
    branch = f"muyan-pilot/issue-{number}"
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), "HEAD",
    ], cwd=repo_dir)
    return path


def run_pi(issue: dict, worktree: Path, prompt_path: Path, *,
           timeout: int = COMMAND_TIMEOUT) -> str:
    system_prompt = prompt_path.read_text(encoding="utf-8")
    context = (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"Issue body:\n{issue.get('body', '')}\n\n"
        f"Worktree: {worktree}\n"
        "Complete the delivery process in the system prompt."
    )
    return run_command([
        "pi", "--print", "--session-dir", str(worktree / ".pi-session"),
        "--system-prompt", system_prompt, context,
    ], cwd=worktree, timeout=timeout)


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


def process_issue(issue: dict, repo_dir: Path, prompt_path: Path,
                  source_repo: str, *, timeout: int = COMMAND_TIMEOUT) -> str:
    number = int(issue["number"])
    branch = f"muyan-pilot/issue-{number}"
    edit_issue(number, repo=source_repo, add="ai-in-progress")
    try:
        worktree = create_worktree(repo_dir, number)
        run_pi(issue, worktree, prompt_path, timeout=timeout)
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
        edit_issue(
            number, repo=source_repo, add="ai-blocked",
            remove="ai-in-progress",
        )
        comment_issue(
            number, repo=source_repo,
            body=f"Muyan Pilot failed: {exc}",
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO)
    parser.add_argument("--timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    issue = pick_issue(args.source_repo)
    if issue is None:
        LOGGER.info("source_repo=%s outcome=no_ready_issue", args.source_repo)
        return 0
    if not args.prompt.is_file():
        raise FileNotFoundError(args.prompt)
    process_issue(
        issue, args.repo_dir, args.prompt, args.source_repo,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
