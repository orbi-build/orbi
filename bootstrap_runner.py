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
import tomllib
import uuid
from pathlib import Path


LOGGER = logging.getLogger("muyan_pilot.bootstrap")


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
    """Create the task worktree from the frozen base SHA, never HEAD."""
    path = worktree_path(repo_dir, source_repo, number, run_id)
    if path.exists():
        raise RuntimeError(f"worktree path already exists: {path}")
    branch = task_branch(source_repo, number, run_id)
    run_command([
        "git", "worktree", "add", "-b", branch, str(path), base_sha,
    ], cwd=repo_dir)
    return path


def run_pi(issue: dict, worktree: Path, config: dict, source_repo: str,
           timeout: int | None = None) -> str:
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
    return run_command(
        command,
        cwd=worktree,
        timeout=timeout,
        log_stdout=True,
        log_command=[
            "pi", "--print", "--session-dir", str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
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
    try:
        worktree = create_worktree(
            config["repo_dir"], source_repo, number, run_id, base_sha,
        )
        config = {**config, "base_sha": base_sha, "run_id": run_id}
        run_pi(issue, worktree, config, source_repo)
        pr_url = verify_pr(worktree, branch, base_branch)
        edit_issue(
            number, repo=source_repo, add="ai-pr-opened",
            remove="ai-in-progress",
        )
        comment_issue(
            number, repo=source_repo,
            body=f"Muyan Pilot opened PR: {pr_url} ({run_info})",
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
                body=f"Muyan Pilot failed: {exc} ({run_info})",
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
