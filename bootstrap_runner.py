#!/usr/bin/env python3
"""One-shot bootstrap runner for Muyan Pilot.

This is intentionally small. It claims one ready GitHub Issue, gives it to
Pi in an isolated worktree, and accepts success only when one open PR exists.
After the implementer opens the PR, the Runner closes the loop itself: it
freezes the exact PR base/head SHA, runs an independent review session, loops
a fixer session on the same feature branch/worktree while Blocker/Major
findings exist, re-checks the merge gate against the latest remote base, and
merges via `gh pr merge --match-head-commit`. Pi never pushes the protected
branch; the Runner is the only merge actor. Any command failure is logged and
raised. There is no fallback, queue, daemon, or multi-agent framework.
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

# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (see review-fix-loop skill: max 5 rounds).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_REVIEW_ROUNDS = 5


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
        "prompt_review": _config_path(
            data.get("prompt_review", "prompt_review.md"), base,
        ),
        "prompt_fix": _config_path(
            data.get("prompt_fix", "prompt_fix.md"), base,
        ),
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
    for path in [
        config["prompt"], config["prompt_review"], config["prompt_fix"],
        *config["skills"], *config["context_files"],
    ]:
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


def parse_review_verdict(text: str) -> dict:
    """Extract the last REVIEW_VERDICT JSON line from a review session.

    The reviewer must end with a machine-readable verdict so the Runner can
    decide without parsing prose. Missing or malformed verdicts fail fast; a
    review that cannot be read as a pass is never treated as a pass.
    """
    payload = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(VERDICT_MARKER):
            payload = stripped[len(VERDICT_MARKER):].strip()
    if payload is None:
        raise ValueError("no REVIEW_VERDICT line in review output")
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError:
        raise ValueError("malformed REVIEW_VERDICT JSON") from None
    if not isinstance(verdict, dict):
        raise ValueError("malformed REVIEW_VERDICT JSON")
    if verdict.get("verdict") not in ("pass", "findings"):
        raise ValueError("verdict must be 'pass' or 'findings'")
    for key in ("blockers", "majors", "minors"):
        value = verdict.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if not isinstance(verdict.get("findings", []), list):
        raise ValueError("findings must be a list")
    return verdict


def review_has_findings(verdict: dict) -> bool:
    """True when a verdict still blocks the merge gate (Blocker or Major)."""
    return verdict["blockers"] > 0 or verdict["majors"] > 0


def freeze_pr(worktree: Path, branch: str, base_branch: str) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    raw = run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "number,url,baseRefName,baseRefOid,headRefName,headRefOid",
        "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    if not isinstance(prs, list) or len(prs) != 1:
        raise RuntimeError("expected exactly one open PR for the task branch")
    pr = prs[0]
    base_ref = pr.get("baseRefName")
    if base_ref != base_branch:
        LOGGER.error(
            "pr_base_mismatch expected=%s actual=%s branch=%s",
            base_branch, base_ref, branch,
        )
        raise RuntimeError(
            f"PR base is {base_ref}, expected {base_branch}; the merge gate "
            "only accepts the configured protected branch"
        )
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": base_ref,
        "base_oid": pr["baseRefOid"],
        "head_ref": pr["headRefName"],
        "head_oid": pr["headRefOid"],
    }


def _agent_session_dir(worktree: Path, role: str, round: int) -> Path:
    """Each review/fix round gets its own session dir (independent sessions)."""
    return worktree / ".pi-session" / f"{role}-round-{round}"


def _agent_skill_args(config: dict) -> list[str]:
    return [
        item for skill in config["skills"]
        for item in ("--skill", str(skill))
    ]


def run_review(worktree: Path, pr: dict, config: dict, source_repo: str,
               round: int, timeout: int | None = None) -> str:
    """Run one independent, read-only review session for a frozen PR."""
    system_prompt = render_prompt(
        config["prompt_review"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "PR_NUMBER": str(pr["number"]),
            "PR_URL": pr["url"],
            "BASE_BRANCH": config["base_branch"],
            "BASE_SHA": pr["base_oid"],
            "HEAD_SHA": pr["head_oid"],
            "HEAD_REF": pr["head_ref"],
            "ROUND": str(round),
        },
    )
    context = (
        f"Independently review PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} against base {config['base_branch']}@{pr['base_oid']} "
        f"and head {pr['head_oid']} (round {round}). Follow code-review R1-R9 "
        "and end with a single REVIEW_VERDICT line."
    )
    session_dir = _agent_session_dir(worktree, "review", round)
    command = [
        "pi", *_agent_skill_args(config), "--print", "--session-dir",
        str(session_dir), "--system-prompt", system_prompt, context,
    ]
    LOGGER.info("review_session=%s pr=%s round=%s", session_dir,
                pr["number"], round)
    return run_command(
        command, cwd=worktree, timeout=timeout, log_stdout=True,
        log_command=[
            "pi", "--print", "--session-dir", str(session_dir),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
    )


def run_fix(worktree: Path, pr: dict, config: dict, source_repo: str,
            findings: list[dict], round: int,
            timeout: int | None = None) -> str:
    """Run one fixer session on the same branch/worktree to clear findings."""
    system_prompt = render_prompt(
        config["prompt_fix"].read_text(encoding="utf-8"),
        {
            "SOURCE_REPO": source_repo,
            "PR_NUMBER": str(pr["number"]),
            "PR_URL": pr["url"],
            "BASE_BRANCH": config["base_branch"],
            "HEAD_REF": pr["head_ref"],
            "ROUND": str(round),
        },
    )
    context = (
        f"Fix the review findings for PR #{pr['number']} ({pr['url']}) of "
        f"{source_repo} on branch {pr['head_ref']} (round {round}). "
        "Findings: " + json.dumps(findings) +
        ". Make the smallest fix, run the full test suite, commit and push "
        "to the same branch. Do not merge or push the protected branch."
    )
    session_dir = _agent_session_dir(worktree, "fix", round)
    command = [
        "pi", *_agent_skill_args(config), "--print", "--session-dir",
        str(session_dir), "--system-prompt", system_prompt, context,
    ]
    LOGGER.info("fix_session=%s pr=%s round=%s", session_dir, pr["number"],
                round)
    return run_command(
        command, cwd=worktree, timeout=timeout, log_stdout=True,
        log_command=[
            "pi", "--print", "--session-dir", str(session_dir),
            "--system-prompt", "<redacted>", "<issue-context-redacted>",
        ],
    )


def merge_gate(worktree: Path, pr: dict, base_branch: str) -> dict:
    """Merge the reviewed PR only if the gate still holds against latest base.

    Re-fetch the latest remote base, require the PR head to contain it, the PR
    to be mergeable, and the remote head to still be the reviewed head. Then
    merge with `--match-head-commit` so only that exact head can land. No force
    push, no direct push of the protected branch.
    """
    run_command(["git", "fetch", "origin", base_branch], cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor",
             f"origin/{base_branch}", pr["head_oid"]],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "merge_gate_behind_base base_branch=%s pr=%s head=%s",
            base_branch, pr["number"], pr["head_oid"],
        )
        raise RuntimeError(
            f"PR #{pr['number']} head {pr['head_oid']} is behind latest "
            f"remote base origin/{base_branch}; absorb the latest base, rerun "
            "tests and review, then retry"
        ) from None
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergeable,headRefOid",
    ], cwd=worktree)
    state = json.loads(raw)
    mergeable = state.get("mergeable")
    if mergeable != "MERGEABLE":
        LOGGER.error(
            "merge_gate_not_mergeable pr=%s mergeable=%s",
            pr["number"], mergeable,
        )
        raise RuntimeError(
            f"PR #{pr['number']} is not mergeable (mergeable={mergeable}); "
            "resolve conflicts and retry"
        )
    remote_head = state.get("headRefOid")
    if remote_head != pr["head_oid"]:
        LOGGER.error(
            "merge_gate_head_moved pr=%s reviewed=%s remote=%s",
            pr["number"], pr["head_oid"], remote_head,
        )
        raise RuntimeError(
            f"PR #{pr['number']} head moved since review "
            f"(reviewed={pr['head_oid']} remote={remote_head}); re-review "
            "before merging"
        )
    run_command([
        "gh", "pr", "merge", str(pr["number"]),
        "--match-head-commit", pr["head_oid"], "--merge",
    ], cwd=worktree)
    LOGGER.info("merged pr=%s head=%s", pr["number"], pr["head_oid"])
    return {**pr, "merged": True}


def confirm_merged(worktree: Path, pr: dict, base_branch: str) -> dict:
    """Confirm the PR is MERGED and origin/<base> contains the merge commit."""
    raw = run_command([
        "gh", "pr", "view", str(pr["number"]),
        "--json", "state,mergedAt,mergeCommit",
    ], cwd=worktree)
    state = json.loads(raw)
    if state.get("state") != "MERGED" or not state.get("mergedAt"):
        LOGGER.error("confirm_merged_not_merged pr=%s state=%s",
                     pr["number"], state.get("state"))
        raise RuntimeError(
            f"PR #{pr['number']} is not merged (state={state.get('state')})"
        )
    merge_commit = (state.get("mergeCommit") or {}).get("oid")
    if not merge_commit:
        raise RuntimeError(
            f"PR #{pr['number']} is merged but has no merge commit oid"
        )
    run_command(["git", "fetch", "origin", base_branch], cwd=worktree)
    try:
        run_command(
            ["git", "merge-base", "--is-ancestor", merge_commit,
             f"origin/{base_branch}"],
            cwd=worktree,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "confirm_merged_missing_on_base pr=%s merge_commit=%s",
            pr["number"], merge_commit,
        )
        raise RuntimeError(
            f"merge commit {merge_commit} is not on origin/{base_branch}; "
            "the merge did not land on the protected branch"
        ) from None
    return {"state": "MERGED", "merge_commit": merge_commit}


def review_fix_merge(worktree: Path, branch: str, base_branch: str,
                     config: dict, source_repo: str, number: int,
                     max_rounds: int = MAX_REVIEW_ROUNDS) -> dict:
    """Independent review, bounded fix loop, merge gate, and merge confirm.

    Loop: freeze the PR, run an independent review, and parse the verdict. If
    there are Blocker/Major findings, comment them to the Issue and PR, run a
    fixer on the same branch/worktree, and re-freeze/re-review. When the
    verdict is clean, run the merge gate (which re-checks the latest remote
    base) and merge, then confirm the merge landed on origin/<base>.
    """
    last_verdict = None
    for round in range(1, max_rounds + 1):
        pr = freeze_pr(worktree, branch, base_branch)
        output = run_review(worktree, pr, config, source_repo, round)
        verdict = parse_review_verdict(output)
        last_verdict = verdict
        LOGGER.info(
            "review pr=%s round=%s verdict=%s blockers=%s majors=%s",
            pr["number"], round, verdict["verdict"], verdict["blockers"],
            verdict["majors"],
        )
        if not review_has_findings(verdict):
            try:
                merged = merge_gate(worktree, pr, base_branch)
            except RuntimeError as exc:
                if "behind latest remote base" not in str(exc):
                    raise
                # The base moved after the review: absorb it, resolve
                # conflicts, rerun the suite, and re-review on the next round.
                _comment_fix_needed(
                    number, source_repo, pr, round,
                    "PR is behind the latest base; merge the latest "
                    f"origin/{base_branch} into the branch, resolve "
                    "conflicts, and rerun the full test suite",
                )
                run_fix(worktree, pr, config, source_repo, [
                    {"level": "Major", "location": "base",
                     "note": "absorb the latest base before merging"},
                ], round)
                continue
            confirmed = confirm_merged(worktree, merged, base_branch)
            return {
                "pr": pr, "rounds": round, "verdict": verdict,
                "merge_commit": confirmed["merge_commit"],
            }
        body = (
            f"Muyan Pilot review round {round} for PR #{pr['number']}: "
            f"{verdict['blockers']} blocker(s), {verdict['majors']} major(s). "
            "Findings: " + json.dumps(verdict["findings"], ensure_ascii=False)
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], body=body)
        run_fix(worktree, pr, config, source_repo, verdict["findings"], round)
    LOGGER.error(
        "review_fix_merge_exhausted pr_branch=%s rounds=%s last=%s",
        branch, max_rounds, last_verdict,
    )
    raise RuntimeError(
        f"review/fix loop exhausted after {max_rounds} rounds with "
        f"{last_verdict['blockers']} blocker(s) and "
        f"{last_verdict['majors']} major(s) remaining; needs human review"
    )


def _comment_fix_needed(number: int, source_repo: str, pr: dict,
                        round: int, reason: str) -> None:
    """Record why a fixer round is needed (behind base) on Issue and PR."""
    body = (
        f"Muyan Pilot review round {round} for PR #{pr['number']}: {reason}."
    )
    comment_issue(number, repo=source_repo, body=body)
    comment_pr(pr["number"], body=body)


def comment_pr(number: int, *, body: str) -> None:
    """Comment on a PR (used to record each review round's findings)."""
    run_command(["gh", "pr", "comment", str(number), "--body", body])


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
        comment_issue(
            number, repo=source_repo,
            body=f"Muyan Pilot opened PR: {pr_url} ({run_info}); running "
                 "independent review/fix loop, then merging",
        )
        result = review_fix_merge(
            worktree, branch, base_branch, config, source_repo, number,
        )
        merged_pr = result["pr"]
        edit_issue(
            number, repo=source_repo, add="ai-merged",
            remove="ai-in-progress",
        )
        comment_issue(
            number, repo=source_repo,
            body=(
                f"Muyan Pilot merged PR: {merged_pr['url']} "
                f"(merge_commit={result['merge_commit']} "
                f"review_rounds={result['rounds']} {run_info})"
            ),
        )
        return merged_pr["url"]
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
