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

Throughout the whole lifecycle the Runner publishes live progress
automatically (Issue #18): one per-run GitHub progress comment carrying a
hidden run marker is PATCHed in place on every activity change and at most
every 30 seconds while any Pi session (implementer, reviewer or fixer)
runs, and short milestone comments (started, plan ready, tests
passed/failed, review findings, fix pushed, PR opened, merged, blocked)
notify GitHub Mobile. No human command, poll or status check is part of
the normal workflow.
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
from collections.abc import Callable
from pathlib import Path

from pilot_slots import acquire_slot, slot_dir_for, slot_occupancy
from pi_activity import (
    SessionWatcher,
    activity_snapshot,
    format_duration,
    format_end_scene,
    format_run_scene,
    quote_value,
    sanitize,
)
from progress import ProgressPublisher, format_elapsed, progress_body


LOGGER = logging.getLogger("muyan_pilot.bootstrap")

# Machine-readable verdict line the reviewer session must end with, and the
# bounded size of the review/fix loop (see review-fix-loop skill: max 5 rounds).
VERDICT_MARKER = "REVIEW_VERDICT"
MAX_REVIEW_ROUNDS = 5

# Live activity polling while Pi runs (Issue #24): every poll the journal
# gets either an `activity` line (something changed) or a `heartbeat` line
# (nothing changed; the idle time is carried on the line itself).
PI_POLL_INTERVAL = 15.0
# Automatic observability (Issue #18): the GitHub progress comment is
# PATCHed on every activity change and at most every 30 seconds while a
# Pi session runs, so a mobile user sees live progress without any
# command. The journal cadence is the poll interval above.
PI_HEARTBEAT_SECONDS = 30.0
# Idle warning (Issue #18): no model/session activity for 5 minutes
# (and the model is not expected to reply next) logs one `pi_idle`
# warning; the first new session event after it logs `pi_resumed`.
# A slow active model (model_wait, Issue #40) is never reported idle.
PI_IDLE_WARN_SECONDS = 300.0

# The bootstrap runner streams every Pi session of a run through the same
# live activity pipeline (Issue #24/#40); implement/review/fix share the
# same line format and carry their role (Issue #41: one run_id end to
# end, the roles are steps of the same run).
ROLE_IMPLEMENT = "implement"
ROLE_REVIEW = "review"
ROLE_FIX = "fix"

# Run correlation (Issue #41): one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_CURRENT_RUN_ID: str | None = None

# GitHub labels are the only state store (Issue #45). After a PR is
# opened the Issue is in a recoverable review/fix state: `ai-pr-opened`
# means awaiting review, and only the explicit `ai-fix-needed` state
# (a review finding or a base conflict) is scanned for Fixer work. The
# next tick resumes that same run on the same branch, worktree and PR
# instead of claiming a new Issue. `ai-merged` is the success terminal
# state the Runner sets after it merges the PR itself (Issue #34).
IN_PROGRESS_LABEL = "ai-in-progress"
PR_OPENED_LABEL = "ai-pr-opened"
FIX_NEEDED_LABEL = "ai-fix-needed"
MERGED_LABEL = "ai-merged"
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
    # Concurrency cap (Issue #39): the local machine can only serve a
    # limited number of concurrent tasks, so the default is 1. Any other
    # value must be a positive integer; fail fast on anything else.
    max_concurrency = data.get("max_concurrency", 1)
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency < 1
    ):
        raise ValueError("max_concurrency must be a positive integer")
    repo_dir = _config_path(data.get("repo_dir", "."), base)
    return {
        "source_repos": source_repos,
        "repo_dir": repo_dir,
        "workspace_root": _config_path(data.get("workspace_root", ".."), base),
        "prompt": _config_path(data.get("prompt", "prompt.md"), base),
        "prompt_review": _config_path(
            data.get("prompt_review", "prompt_review.md"), base,
        ),
        "skills": [_config_path(item, base) for item in data.get("skills", [])],
        "context_files": [
            _config_path(item, base) for item in data.get("context_files", [])
        ],
        "base_branch": base_branch,
        "max_concurrency": max_concurrency,
        "slot_dir": slot_dir_for(repo_dir),
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
        config["prompt"], config["prompt_review"],
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


def open_blocker_numbers(issue: dict) -> list[int]:
    """Return the numbers of the issue's OPEN native GitHub blockers.

    `gh issue list --json blockedBy` (gh 2.94+) carries the native
    dependency relation as `{"nodes": [...], "totalCount": N}`. GitHub
    keeps a relation listed after its blocker closes (the node then
    carries `state: "CLOSED"` and is inert — verified against the live
    API, Issue #54), so only OPEN blockers actually block: a closed
    blocker clears the dependency without any runner-side bookkeeping,
    and the next tick claims the Issue. A node without an explicit
    `state` counts as open (claiming a possibly-blocked Issue costs a
    full run; waiting one tick does not). A missing or malformed field
    means "no known blockers" (fail open): an API shape change must
    never deadlock the queue.
    """
    blocked_by = issue.get("blockedBy")
    if not isinstance(blocked_by, dict):
        return []
    nodes = blocked_by.get("nodes")
    if not isinstance(nodes, list):
        return []
    numbers: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        number = node.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if node.get("state", "OPEN") != "OPEN":
            continue
        numbers.append(number)
    return numbers


def pick_issue(repo: str) -> dict | None:
    # A merged delivery keeps `ai-ready` + `ai-merged` on the (still
    # open) Issue; `ai-merged` is the success terminal state, so it is
    # excluded from the ready scan like every other delivery state.
    # The scan fetches the ready queue (not just the first Issue) and
    # reads the native GitHub dependency per Issue (Issue #54): an
    # Issue with open blockers is skipped — no claim, no label change,
    # no worktree — and the next ready Issue is considered instead.
    try:
        raw = run_command([
            "gh", "issue", "list", "--repo", repo, "--state", "open",
            "--search",
            "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
            f"-label:{FIX_NEEDED_LABEL} -label:{MERGED_LABEL} "
            f"-label:{BLOCKED_LABEL}",
            "--json", "number,title,body,blockedBy", "--limit", "200",
        ])
        issues = parse_issue_array(raw)
    except Exception as exc:
        # Fail open (Issue #54): a failed blockedBy query must never
        # deadlock the queue. This tick claims nothing from this repo
        # and the next tick retries the query; the error is logged,
        # never raised, and no label is touched.
        LOGGER.error(
            "blocked_by_check_failed repo=%s error=%s",
            repo, exc,
        )
        return None
    for issue in issues:
        blockers = open_blocker_numbers(issue)
        if blockers:
            LOGGER.info(
                "blocked_by issue=%s repo=%s blockers=%s",
                issue.get("number"), repo,
                ",".join(str(number) for number in blockers),
            )
            continue
        return issue
    return None


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> dict | None:
    """Return one in-flight Issue a killed runner left behind.

    A SIGKILLed runner leaves the task worktree and the `ai-in-progress`
    claim label behind (the failure path never ran); the Issue keeps
    `ai-ready` too (a claim never removes it). The ready scan excludes
    `ai-in-progress`, so without this scan the run is never resumed and
    the Issue is stuck forever — the Issue #18 acceptance "restart finds
    the same progress comment by run marker" would be unreachable in the
    production flow. `process_issue`'s resume block (newest worktree's
    run id) then reuses the run instead of starting a second one. Every
    other delivery state is excluded: those Issues are owned by the
    resumable-PR scan (`ai-fix-needed`) or are terminal.

    The scan runs only when no OTHER runner is live: a slot held by
    another process proves a live runner is working (on this or another
    Issue), so the `ai-in-progress` label is in flight, not orphaned —
    resuming it here would start a second Pi for a run that is alive
    (Issue #39 slot semantics: the flock lock is the source of truth).
    This runner's own slot is excluded: `main` took it before the claim
    scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        "label:ai-ready label:ai-in-progress "
        f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
        f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL}",
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


def pick_resumable_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
) -> tuple[dict, dict] | None:
    """Return the newest opened-PR delivery and its resume scene.

    Both opened-PR states are scanned (Issue #70): `ai-fix-needed` (a
    review finding or a base conflict — the next tick runs the Fixer on
    the same branch, worktree and PR, Issue #45) and `ai-pr-opened`
    (awaiting review — the next tick runs the independent review; a
    clean PR is still never sent to the Fixer, Issue #45 round-5
    contract). The `ai-pr-opened` scan exists because the delivery that
    opened the PR can be gone: the progress-publishing failure behind
    Issue #70 used to label the delivered Issue `ai-blocked` before the
    review started, and a killed runner can die inside the delivery
    wait loop, leaving a valid MERGEABLE PR with no owner. Without the
    scan such a delivery is picked up by no other scan (`pick_issue`
    excludes `ai-pr-opened`) and is stranded forever. `ai-blocked`
    Issues are excluded (they need a human decision first), as are
    merged and in-flight Issues and closed Issues. A scene that cannot
    be recovered is an unresolvable state: the Issue is marked
    `ai-blocked` with the concrete reason and the error re-raised, so
    the tick stops instead of silently skipping the delivery while a
    fresh task starts ahead of it.

    The scan runs only when no OTHER runner is live (the same guard as
    `pick_in_progress_issue`, Issue #39 slot semantics): a slot held by
    another process proves a live runner is working, so an opened-PR
    delivery is in flight, not stranded — resuming it here would start
    a second review Pi in the same worktree/branch/run, and the second
    `gh pr merge --match-head-commit` on the already-merged PR would
    fail and mark the merged Issue `ai-blocked` (Issue #70 review
    round 1). This runner's own slot is excluded: `main` took it
    before the claim scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--search",
        # `label:a,b` is GitHub's OR within one label qualifier
        # (verified live: repeating the qualifier matches only the
        # first label).
        f"label:{FIX_NEEDED_LABEL},{PR_OPENED_LABEL} "
        f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL} "
        f"-label:{IN_PROGRESS_LABEL}",
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
    except ValueError as exc:
        block_scene_failure(issue, exc, repo, comments)
    # The fixer works from the original task, so the resumable issue
    # carries its body like a freshly claimed one.
    issue["body"] = issue_body(int(issue["number"]), repo=repo)
    return issue, scene


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an `ai-fix-needed` Issue `ai-blocked` when its scene is
    malformed, then re-raise so the tick stops (Issue #45).

    The failure comment carries the run marker recovered from a trusted
    comment when it is present — the same run id, never a new or
    guessed one. The PR, branch and worktree stay intact.
    """
    number = int(issue["number"])
    LOGGER.error(
        "issue=%s resume scene is malformed: %s", number, error,
    )
    marker = ""
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = re.search(r"<!-- muyan-pilot:run=([0-9a-f]{8}) -->", body)
        if match:
            marker = run_marker(match.group(1))
            break
    try:
        edit_issue(
            number, repo=repo, add=BLOCKED_LABEL, remove=FIX_NEEDED_LABEL,
        )
        comment_issue(
            number, repo=repo,
            body=(
                f"{marker}\n" if marker else ""
            ) + f"Muyan Pilot failed: {error}",
        )
    except Exception:
        LOGGER.exception("issue=%s failure reporting failed", number)
    raise error


def pick_next_delivery(
    repos: list[str], slot_dir: Path, max_concurrency: int,
) -> tuple[str, dict, dict | None] | None:
    """Scan sources in order: resumable PRs, in-flight restarts, ready.

    Returns `(source_repo, issue, scene)` where `scene` is None for a
    fresh claim or a restart resume. Resuming an open PR keeps the
    single concurrency slot occupied by the same run (implement →
    review → fix → merge), so a second Pi is never started for a run
    that already has a PR. An in-flight Issue (a killed runner left
    `ai-in-progress` behind, Issue #18) is recovered before the ready
    scan: `process_issue`'s resume block reuses the newest worktree's
    run id, so the same progress comment is kept instead of a second
    run being started on an Issue that is already in flight.
    """
    for repo in repos:
        selected = pick_resumable_delivery(
            repo, slot_dir, max_concurrency,
        )
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        issue = pick_in_progress_issue(
            repo, slot_dir, max_concurrency,
        )
        if issue is not None:
            return repo, issue, None
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
    marker) instead of creating a second one (Issue #18).
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
    failure path, so it is the marker of a run that is (or was, when
    the runner died) in flight — as opposed to the preserved worktrees
    of completed runs (Issue #18).
    """
    raw = run_command([
        "gh", "issue", "list", "--repo", repo, "--state", "all",
        "--search", f"label:{IN_PROGRESS_LABEL}",
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


def _log_activity(activity: dict, *, issue_ref: str,
                  role: str, state: str | None = None) -> None:
    """Log one short activity line with the changed fields only.

    No `run=` field (Issue #57): the `[run_id]` prefix added by
    `RunIdFilter` (Issue #41) is the single run-id carrier on the
    high-frequency lines, so the id appears exactly once per line.
    """
    LOGGER.info(
        "activity issue=%s role=%s phase=%s action=%s result=%s "
        "state=%s idle=%s",
        issue_ref, role, activity["phase"],
        quote_value(activity["action"] or "-"),
        activity["result"] or "-",
        state or "-",
        format_duration(activity["stale_seconds"]),
    )


def _log_heartbeat(activity: dict, *, issue_ref: str,
                   role: str, elapsed: float,
                   state: str | None = None) -> None:
    """Log one heartbeat line when nothing changed since the last poll.

    `state` carries the model_wait flag while the model is expected to
    reply next, so a slow active model is not reported as idle (Issue
    #40). No `run=` field (Issue #57): the `[run_id]` prefix is the
    single run-id carrier on the high-frequency lines.
    """
    LOGGER.info(
        "heartbeat issue=%s role=%s phase=%s state=%s elapsed=%s "
        "idle=%s",
        issue_ref, role, activity["phase"], state or "-",
        format_duration(elapsed), format_duration(activity["stale_seconds"]),
    )


def stream_pi(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    poll_interval: float = PI_POLL_INTERVAL,
    idle_warn_seconds: float = PI_IDLE_WARN_SECONDS,
    run_id: str,
    issue: int,
    source_repo: str,
    branch: str,
    role: str = ROLE_IMPLEMENT,
    log_command: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> str:
    """Run Pi and stream concise live activity into the journal (Issue #40).

    The full invariant scene (branch, worktree, session file) is logged
    once as `run_start`. While Pi runs, only short changed fields are
    logged: `activity` when phase/action/result change, `heartbeat` at
    the poll interval otherwise (the idle time rides on the line). A slow
    active model is not reported as idle: when the newest session event
    is a tool result the state is `model_wait` (one transition line on
    entry, one `resumed` line when the next session event arrives, and
    only configured-interval heartbeats while waiting — no warning
    spam). On a non-zero exit or timeout a `run_failed` line carries the
    full scene again as the debug entry. The caller logs `run_end` once
    the PR and commit are known. The session JSONL stays in the worktree
    as the complete local record; the full prompt and Issue body are
    never logged.

    `progress` (Issue #18) is invoked on EVERY poll — an activity change
    or a heartbeat — with the current activity state, while the Pi
    process is still running: the caller renders the live GitHub
    progress comment and PATCHes the same run-marker comment in place,
    so mobile users never see a static starting comment for the whole
    run. A callback error is logged and never interrupts the task
    (observability is best-effort, the delivery is not).

    Idle warning (Issue #18): when no model/session event arrives for
    `idle_warn_seconds` (default 5 minutes) and the state is NOT
    model_wait, ONE `pi_idle` WARNING carries `stale_seconds`; the
    first new session event after it logs `pi_resumed`. A slow active
    model (model_wait) is never reported idle (Issue #40).
    """
    # The raw pi command embeds the full prompt and Issue body; only the
    # redacted form may ever reach the journal or an exception message.
    safe_command = log_command or ["<redacted>"]
    LOGGER.info("command=%s cwd=%s", " ".join(safe_command), cwd)
    issue_ref = issue_context(source_repo, issue)
    session_dir = cwd / ".pi-session"
    # Session files that already exist before this Pi process starts are
    # never followed: a resumed run (same worktree) creates a NEW JSONL,
    # and the journal must report the session of the current invocation,
    # not the previous run's (Issue #45 round-5 review, Major 3).
    known_files = (
        {path for path in session_dir.glob("*.jsonl") if path.is_file()}
        if session_dir.is_dir() else set()
    )
    watcher = SessionWatcher(session_dir, known_files=known_files)
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
            role=role, branch=branch, worktree=str(cwd),
        ),
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    activity = watcher.poll()
    timed_out = False
    # model_wait transitions (Issue #40): one line when the state is
    # entered and one when it is left; unchanged polls are heartbeats
    # that carry the state, so a slow model never looks idle and no
    # warning is ever escalated from a slow response.
    last_model_wait = activity["model_wait"]
    # Idle warning state (Issue #18): at most one `pi_idle` warning per
    # stall; the first new session event after it logs `pi_resumed`.
    idle_warned = False
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
            if progress is not None:
                try:
                    progress(activity)
                except Exception:
                    LOGGER.exception(
                        "progress_publish_failed run=%s issue=%s role=%s",
                        run_id, issue_ref, role,
                    )
            visible = (
                activity["phase"], activity["action"], activity["result"],
            )
            if visible != last_visible:
                # Only changed fields are repeated; an unchanged poll is a
                # heartbeat (Issue #40).
                _log_activity(
                    activity, issue_ref=issue_ref,
                    role=role,
                    state="model_wait" if activity["model_wait"] else None,
                )
                last_visible = visible
            else:
                _log_heartbeat(
                    activity, issue_ref=issue_ref,
                    role=role, elapsed=time.monotonic() - start,
                    state="model_wait" if activity["model_wait"] else None,
                )
            if activity["model_wait"] != last_model_wait:
                # One transition line per state change: entering model_wait
                # (the model is expected to reply next) or leaving it
                # (the next session event arrived: resumed). No `run=`
                # field: the `[run_id]` prefix carries the run id
                # (Issue #57).
                LOGGER.info(
                    "%s issue=%s role=%s phase=%s state=%s",
                    "model_wait" if activity["model_wait"] else "resumed",
                    issue_ref, role,
                    activity["phase"],
                    "model_wait" if activity["model_wait"] else "resumed",
                )
                last_model_wait = activity["model_wait"]
            # Idle warning (Issue #18): a stalled session (no model/
            # session event for `idle_warn_seconds`, and the model is
            # not expected to reply next) logs ONE `pi_idle` warning
            # with the stale time; the first new session event after it
            # logs `pi_resumed`. A slow active model (model_wait) never
            # warns (Issue #40).
            if idle_warned and activity["changed"]:
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.info(
                    "pi_resumed issue=%s role=%s phase=%s",
                    issue_ref, role, activity["phase"],
                )
                idle_warned = False
            elif (
                not activity["model_wait"]
                and not idle_warned
                and activity["stale_seconds"] >= idle_warn_seconds
            ):
                # No `run=` field: the `[run_id]` prefix carries the run
                # id (Issue #57).
                LOGGER.warning(
                    "pi_idle issue=%s role=%s phase=%s "
                    "stale_seconds=%s",
                    issue_ref, role, activity["phase"],
                    format_duration(activity["stale_seconds"]),
                )
                idle_warned = True
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
                role=role, branch=branch, worktree=str(cwd),
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
                role=role, branch=branch, worktree=str(cwd),
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
           *, timeout: int | None = None, branch: str | None = None,
           pr_url: str | None = None,
           progress: Callable[[dict], None] | None = None) -> str:
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
            "SKILLS": "\n".join(
                str(path)
                for path in _skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)
            ),
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
    command = [
        "pi", *_skill_args(_skills_for(config, IMPLEMENT_EXCLUDED_SKILLS)),
        "--print", "--session-dir",
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
        progress=progress,
    )


def verify_pr(worktree: Path, branch: str, base_branch: str,
              run_id: str, *, issue: int,
              pr_repo: str | None = None,
              expected_url: str | None = None,
              require_latest_base: bool = True) -> str:
    """Verify that exactly one open PR of the task branch is the delivery.

    Checks, in order: current branch, latest remote base ancestry (unless
    `require_latest_base` is False — the resume pre-validation runs
    before the base merge, when being behind is the expected state),
    exactly one open PR for the head branch, PR base, PR head == local
    HEAD, the run marker in the PR body, and the `Fixes #<issue>`
    keyword in the PR body (Issue #53: GitHub closes the source Issue
    natively only when the body carries the keyword, so a PR without it
    would leave the Issue open after the merge). When `pr_repo` is
    given (resume path), the PR's head repo must be that repo; when
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
    fixes = f"Fixes #{issue}"
    # The number must match exactly, not as a digit prefix: `Fixes #41`
    # closes Issue 41, not Issue 4 (review F1, Issue #53).
    if not re.search(rf"Fixes #{issue}(?!\d)", body):
        LOGGER.error(
            "pr_fixes_missing issue=%s branch=%s", issue, branch,
        )
        raise RuntimeError(
            f"PR body is missing `{fixes}`; the keyword must point at the "
            "source Issue so GitHub closes it natively when the PR merges "
            "into the default branch"
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
    blockers = verdict["blockers"]
    majors = verdict["majors"]
    if verdict["verdict"] == "pass" and (blockers > 0 or majors > 0):
        raise ValueError("pass verdict cannot have blockers or majors")
    if verdict["verdict"] == "findings" and blockers == 0 and majors == 0:
        raise ValueError("findings verdict requires blockers or majors")
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


# Role-specific skill filtering (Issue #83): the review session is
# read-only and ends with a single REVIEW_VERDICT line, so the
# delivery-oriented skills must not be loaded there (tdd-dev would
# steer it into the implement/test/PR flow, review-fix-loop would
# open another fix/review round). The implementer/fixer keeps
# tdd-dev and code-review but not review-fix-loop: the Runner itself
# runs the independent review/fix loop once the PR is open.
REVIEW_EXCLUDED_SKILLS = frozenset({"tdd-dev", "review-fix-loop"})
IMPLEMENT_EXCLUDED_SKILLS = frozenset({"review-fix-loop"})


def _skill_name(entry: str | Path) -> str:
    """Return the skill name of one configured skill entry.

    Entries point at the SKILL.md file inside the skill directory
    (e.g. .../skills/tdd-dev/SKILL.md); the skill name is the parent
    directory. A bare markdown entry (e.g. my-skill.md) or a skill
    directory is named after its own stem.
    """
    path = Path(entry)
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def _skills_for(config: dict, excluded: frozenset[str]) -> list[str | Path]:
    """Return one role's configured skills, dropping excluded names."""
    return [
        skill for skill in config["skills"]
        if _skill_name(skill) not in excluded
    ]


def _skill_args(skills: list[str | Path]) -> list[str]:
    """Return the --skill command args for one role's skill list."""
    return [
        item for skill in skills
        for item in ("--skill", str(skill))
    ]


def run_review(worktree: Path, pr: dict, config: dict, source_repo: str,
               issue: int, branch: str, round: int,
               timeout: int | None = None,
               progress: Callable[[dict], None] | None = None) -> str:
    """Run one independent, read-only review session for a frozen PR.

    The review streams live activity through the same pipeline as the
    implementer and fixer (role=review; Issue #41: one run_id end to end,
    the roles are steps of the same run).
    """
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
    command = [
        "pi", *_skill_args(_skills_for(config, REVIEW_EXCLUDED_SKILLS)),
        "--print", "--session-dir",
        str(worktree / ".pi-session"), "--system-prompt", system_prompt,
        context,
    ]
    return stream_pi(
        command,
        cwd=worktree,
        timeout=timeout,
        log_command=[
            "pi", "--print", "--session-dir",
            str(worktree / ".pi-session"),
            "--system-prompt", "<redacted>", "<review-context-redacted>",
        ],
        run_id=config["run_id"],
        issue=issue,
        source_repo=source_repo,
        branch=branch,
        role=ROLE_REVIEW,
        progress=progress,
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


def review_rounds_so_far(comments: list[dict]) -> int:
    """Count the review rounds already recorded on the Issue.

    Each round with Blocker/Major findings posts one
    `Muyan Pilot review round N for PR #...` comment, so the GitHub
    record alone bounds the loop (GitHub Issues are the only state
    store; a runner restart never loses the count). Only trusted
    maintainer comments count: a public comment cannot exhaust the
    round budget or skip review (same filter as resume_scene).
    """
    rounds = 0
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            if line.startswith("Muyan Pilot review round "):
                rounds += 1
                break
    return rounds


def sync_base_checkout(repo_dir: Path, base_branch: str) -> None:
    """Fast-forward the configured repo_dir base checkout to origin/<base>.

    systemd executes the runner from this checkout: after a merge lands
    on origin/<base>, the next tick must load the newly merged code, so
    the deployment checkout is synced here and verified to equal the
    remote base. A checkout that cannot fast-forward (local drift) fails
    fast; the merge itself already landed on GitHub.
    """
    run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
    local_head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    remote_head = run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
    )
    if local_head == remote_head:
        return
    try:
        run_command(
            ["git", "merge", "--ff-only", f"origin/{base_branch}"],
            cwd=repo_dir,
        )
    except subprocess.CalledProcessError:
        LOGGER.error(
            "base_checkout_not_fast_forwardable repo_dir=%s base=%s "
            "local=%s remote=%s",
            repo_dir, base_branch, local_head, remote_head,
        )
        raise RuntimeError(
            f"deployment checkout {repo_dir} cannot fast-forward to "
            f"origin/{base_branch} (local={local_head} "
            f"remote={remote_head}); the merged code cannot be loaded "
            "by the next tick"
        ) from None
    synced = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if synced != remote_head:
        raise RuntimeError(
            f"deployment checkout {repo_dir} is at {synced} after the "
            f"sync, expected origin/{base_branch} at {remote_head}"
        )
    LOGGER.info(
        "base_checkout_synced repo_dir=%s base=%s head=%s",
        repo_dir, base_branch, synced,
    )


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: dict, source_repo: str,
                              number: int) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    The delivery wait loop (which holds the slot) calls this while the
    PR is open and the Issue awaits review (`ai-pr-opened`). It freezes
    the PR, runs the independent review (streamed, role=review), and
    then:

    - clean verdict -> merge gate (latest-base ancestor, mergeable,
      head match, `gh pr merge --match-head-commit`), confirm the merge
      landed on origin/<base>, sync the deployment checkout, label the
      Issue `ai-merged`; returns True;
    - Blocker/Major findings -> comment them to Issue and PR and label
      the Issue `ai-fix-needed`; the #45 fix loop repairs the same PR
      and returns the Issue to `ai-pr-opened`, where the next wait
      iteration re-reviews; returns False;
    - a gate failure because the head is behind the latest base ->
      label the Issue `ai-fix-needed` with the absorb-base finding
      (the fixer merges the latest base); returns False;
    - missing/malformed verdict or an exhausted round budget -> raise;
      the caller marks the Issue `ai-blocked`.
    """
    marker = run_marker(config["run_id"])
    comments = issue_comments(number, repo=source_repo)
    rounds = review_rounds_so_far(comments)
    if rounds >= MAX_REVIEW_ROUNDS:
        LOGGER.error(
            "review_rounds_exhausted issue=%s rounds=%s",
            number, rounds,
        )
        raise RuntimeError(
            f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
            "without a clean verdict; needs human review"
        )
    round = rounds + 1
    pr = freeze_pr(worktree, branch, base_branch)
    publisher = ProgressPublisher(
        number, source_repo, config["run_id"], run_command=run_command,
    )
    started = time.monotonic()
    publisher.ensure(_progress_body(_progress_state(
        issue=number, run_id=config["run_id"], role=ROLE_REVIEW,
        branch=branch, worktree=worktree, started=started,
        pr_url=pr["url"], review_round=round,
    )))
    output = run_review(
        worktree, pr, config, source_repo, number, branch, round,
        progress=LiveProgressThrottle(
            publisher, issue=number, run_id=config["run_id"],
            role=ROLE_REVIEW, branch=branch, worktree=worktree,
            started=started, pr_url=pr["url"], review_round=round,
        ),
    )
    verdict = parse_review_verdict(output)
    LOGGER.info(
        "review pr=%s round=%s verdict=%s blockers=%s majors=%s",
        pr["number"], round, verdict["verdict"], verdict["blockers"],
        verdict["majors"],
    )
    if review_has_findings(verdict):
        body = (
            f"{marker}\n"
            f"Muyan Pilot review round {round} for PR #{pr['number']}: "
            f"{verdict['blockers']} blocker(s), {verdict['majors']} "
            "major(s). Findings: "
            + json.dumps(verdict["findings"], ensure_ascii=False)
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], body=body)
        publisher.milestone(
            f"review findings: round {round}, {verdict['blockers']} "
            f"blocker(s), {verdict['majors']} major(s) for "
            f"PR #{pr['number']}"
        )
        publisher.finish(_progress_body(_progress_state(
            issue=number, run_id=config["run_id"], role=ROLE_REVIEW,
            branch=branch, worktree=worktree, started=started,
            pr_url=pr["url"], review_round=round,
        ), outcome=(
            "**Muyan Pilot review findings**\n\n"
            f"round {round}: {verdict['blockers']} blocker(s), "
            f"{verdict['majors']} major(s); the fix loop repairs the "
            "same PR automatically"
        )))
        edit_issue(
            number, repo=source_repo, add=FIX_NEEDED_LABEL,
            remove=PR_OPENED_LABEL,
        )
        return False
    try:
        merged = merge_gate(worktree, pr, base_branch)
    except RuntimeError as exc:
        message = str(exc)
        # Behind and merge-conflict are the same fixer job: absorb
        # origin/<base>, resolve, retest. Other gate failures
        # (head moved, etc.) fail fast.
        if (
            "behind latest remote base" not in message
            and "not mergeable" not in message
        ):
            raise
        body = (
            f"{marker}\n"
            f"Muyan Pilot review round {round} for PR #{pr['number']}: "
            "the PR is behind the latest base or has a merge conflict; "
            f"merge the latest origin/{base_branch} into the branch, "
            "resolve conflicts, and rerun the full test suite"
        )
        comment_issue(number, repo=source_repo, body=body)
        comment_pr(pr["number"], body=body)
        edit_issue(
            number, repo=source_repo, add=FIX_NEEDED_LABEL,
            remove=PR_OPENED_LABEL,
        )
        return False
    confirmed = confirm_merged(worktree, merged, base_branch)
    publisher.milestone(
        f"merged: {merged['url']} "
        f"(merge_commit={confirmed['merge_commit']} review_rounds={round})"
    )
    publisher.finish(_progress_body(_progress_state(
        issue=number, run_id=config["run_id"], role=ROLE_REVIEW,
        branch=branch, worktree=worktree, started=started,
        pr_url=merged["url"], review_round=round,
    ), outcome=(
        "**Muyan Pilot delivered**\n\n"
        f"PR {merged['url']} merged "
        f"(merge_commit={confirmed['merge_commit']} "
        f"review_rounds={round})"
    )))
    # The GitHub merge already landed. Record ai-merged before touching
    # the local systemd checkout: a checkout that cannot fast-forward is
    # runner ops, not a failed delivery (must not become ai-blocked).
    edit_issue(
        number, repo=source_repo, add=MERGED_LABEL,
        remove=PR_OPENED_LABEL,
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{marker}\n"
            f"Muyan Pilot merged PR: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"base_branch={base_branch} run_id={config['run_id']})"
        ),
    )
    try:
        sync_base_checkout(config["repo_dir"], base_branch)
    except RuntimeError:
        LOGGER.exception(
            "base_checkout_sync_failed after merge pr=%s repo_dir=%s; "
            "the delivery already landed on origin/%s",
            merged["url"], config["repo_dir"], base_branch,
        )
    return True


def comment_pr(number: int, *, body: str) -> None:
    """Comment on a PR (used to record each review round's findings)."""
    run_command(["gh", "pr", "comment", str(number), "--body", body])

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




# pytest's final summary line: `1 failed, 155 passed in 4.43s` (the
# counts and the `in <seconds>` part are optional; the line is NOT
# wrapped in `=` section padding).
_Pytest_SUMMARY_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors|skipped|xfailed|xpassed"
    r"|deselected)(?:, \d+ \w+)*(?: in [\d.]+s)?$")


def _is_section_header(line: str) -> bool:
    """True for pytest section headers like `=== FAILURES ===`.

    A header is `=`-delimited at both ends (padding may be absent on
    one side for short titles) and its title carries no digits; the
    real summary line (`1 failed, 155 passed in 4.43s`, bare or
    `=`-padded) and the `FAILED`/`ERROR` evidence lines never match.
    """
    if not line.startswith("="):
        return False
    core = line.strip("=").strip()
    return core == "" or not any(ch.isdigit() for ch in core)


# A pytest summary line reports an outcome only when its FIRST count
# category is failed/passed/error(s): pytest orders the counts
# failed, passed, skipped, errors, xfailed, xpassed, deselected, so a
# run that collected tests always leads with failed or passed (or a
# collection error). `no tests ran in 0.01s` matches no summary regex
# at all; `3 deselected in 0.02s` / `2 skipped in 0.01s` match but
# carry no outcome — reporting them as a pass is a false notification
# (review round 3, PR #42).
_OUTCOME_FIRST_RE = re.compile(
    r"^\d+ (?:failed|passed|error|errors)\b",
)
_NO_TESTS_RE = re.compile(r"no tests (?:ran|collected)")


def _is_no_result(line: str) -> bool:
    """True for pytest lines that verified nothing (no tests ran)."""
    if _NO_TESTS_RE.search(line):
        return True
    stripped = line.strip("=").strip()
    return bool(_Pytest_SUMMARY_RE.match(stripped)) \
        and not _OUTCOME_FIRST_RE.match(stripped)


def read_test_result(worktree: Path) -> str | None:
    """Summarize the worktree's `test.log`, or None when it does not exist.

    Prefers the pytest summary line (`1 failed, 155 passed in 4.43s`):
    the LAST one with an outcome when the log holds several runs (TDD
    red, then green), so the progress comment and the `tests
    passed/failed` milestone report the most recent run that actually
    collected tests. Section headers (`=== FAILURES ===`) are never
    reported: the fallback takes the first `FAILED`/`ERROR` evidence
    line or the last non-empty line instead, and a log holding nothing
    but headers yields None (no result info to report). Lines that
    verified nothing (`no tests ran`, `N deselected`, `N skipped`) are
    never reported either: a run that collected no tests is no result,
    and posting `tests passed` for it is a false notification (review
    round 3, PR #42).
    """
    path = worktree / "test.log"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        # The summary line is bare in pytest 9; some runners wrap it in
        # `=` padding, so the stripped form is matched as well.
        stripped = line.strip("=").strip()
        if _Pytest_SUMMARY_RE.match(line) \
                or _Pytest_SUMMARY_RE.match(stripped):
            if _is_no_result(line):
                # No tests collected in this run: keep looking for an
                # earlier run that did (or report nothing at all).
                continue
            return sanitize(stripped)
    for line in lines:
        if _is_section_header(line) or _is_no_result(line):
            continue
        if line.startswith(("FAILED", "ERROR")) or "passed" in line:
            return sanitize(line)
    last = lines[-1] if lines else None
    if last is not None and (_is_section_header(last)
                             or _is_no_result(last)):
        return None
    return sanitize(last) if last is not None else None


def delivery_head_advanced(worktree: Path, base_sha: str) -> bool:
    """True when the task branch has commits beyond the frozen base."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=worktree)
    return head != base_sha


def _progress_state(*, issue: int, run_id: str, role: str, branch: str,
                    worktree: Path, started: float, pr_url: str | None,
                    review_round: int,
                    activity: dict | None = None) -> dict:
    """Collect the current run state for the GitHub progress comment.

    `activity` is the live state from the `stream_pi` watcher while a Pi
    session runs (fresh and already read); without it the newest session
    file is full-scanned. Activity snapshotting is best-effort
    observability: a read failure is logged and reported as "no session
    yet", it never blocks the task.
    """
    if activity is None:
        try:
            activity = activity_snapshot(worktree / ".pi-session")
        except Exception:
            LOGGER.exception("issue=%s activity snapshot failed", issue)
            activity = None
    return {
        "run_id": run_id,
        "issue": issue,
        "role": role,
        "phase": (activity or {}).get("phase") or "starting",
        "elapsed": format_elapsed(time.monotonic() - started),
        "last_activity": (activity or {}).get("last_activity"),
        "last_action": (activity or {}).get("action"),
        "tests": read_test_result(worktree),
        "review_round": review_round,
        "branch": branch,
        "pr": pr_url,
        "session": (activity or {}).get("session_id"),
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
    """Post `tests passed` / `tests failed` from the worktree's test.log.

    The failure check is case-insensitive: pytest evidence lines carry
    uppercase markers (`FAILED`, `FAILURES`) that a lowercase-only check
    would misreport as a pass (review round 2, PR #42).
    """
    result = read_test_result(worktree)
    if result is None:
        return
    lowered = result.lower()
    if "fail" in lowered or "error" in lowered:
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


def _live_progress(publisher: ProgressPublisher, *, issue: int, run_id: str,
                   role: str, branch: str, worktree: Path, started: float,
                   pr_url: str | None, review_round: int,
                   activity: dict | None = None) -> None:
    """One live GitHub progress update while a Pi session is running.

    Called from the `stream_pi` poll loop (every activity change or
    heartbeat, Issue #18): the same run-marker comment is PATCHed in
    place at most every `PI_HEARTBEAT_SECONDS` or when the visible
    activity changed. `activity` is the watcher state of that poll, so
    the live comment shows the session exactly as the journal reports
    it. The publisher already knows the comment id after `ensure`, so a
    callback before it would fail fast here — and the wiring always
    ensures first.
    """
    state = _progress_state(
        issue=issue, run_id=run_id, role=role, branch=branch,
        worktree=worktree, started=started, pr_url=pr_url,
        review_round=review_round, activity=activity,
    )
    publisher.patch(_progress_body(state))


class LiveProgressThrottle:
    """Throttle live GitHub PATCHes to change-driven or <=30-second cadence.

    The `stream_pi` poll loop fires on every poll (15 s default); PATCHing
    GitHub on every poll would double the traffic for no visible gain.
    The throttle passes an update through when the visible activity
    (phase, action, result, model_wait) changed since the last PATCH or
    when at least `PI_HEARTBEAT_SECONDS` passed since it.
    """

    def __init__(self, publisher: ProgressPublisher, *, issue: int,
                 run_id: str, role: str, branch: str, worktree: Path,
                 started: float, pr_url: str | None,
                 review_round: int) -> None:
        def publish(activity: dict) -> None:
            _live_progress(
                publisher, issue=issue, run_id=run_id, role=role,
                branch=branch, worktree=worktree, started=started,
                pr_url=pr_url, review_round=review_round,
                activity=activity,
            )

        self._publish = publish
        self._last_visible: tuple | None = None
        self._last_patch = 0.0

    def __call__(self, activity: dict) -> None:
        visible = (
            activity["phase"], activity["action"], activity["result"],
            activity["model_wait"],
        )
        now = time.monotonic()
        if visible == self._last_visible and \
                now - self._last_patch < PI_HEARTBEAT_SECONDS:
            return
        self._last_visible = visible
        self._last_patch = now
        self._publish(activity)


def process_issue(issue: dict, config: dict, source_repo: str) -> str:
    number = int(issue["number"])
    base_branch = config["base_branch"]
    # The run id is generated once per attempt and bound BEFORE any
    # other step is logged, so every journal line of the attempt
    # carries it — including the claim-time lines of the restart resume
    # scan below (Issue #41; review round 3, PR #42).
    run_id = new_run_id()
    set_run_id(run_id)
    # Restart resume (Issue #18): a killed runner leaves the task
    # worktree and the `ai-in-progress` label behind. Only in that state
    # the newest worktree's run id is reused, so the same hidden-marker
    # progress comment is found and kept instead of a second one.
    # Completed runs keep their worktrees as evidence but lose the
    # label, so re-claiming an issue always starts a fresh run.
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config["repo_dir"], source_repo, number,
        )
        if existing_run_id is not None:
            run_id = existing_run_id
            # The attempt continues the dead run: re-bind the reused
            # id so every later line (including resuming_run) carries
            # it.
            set_run_id(run_id)
            LOGGER.info(
                "issue=%s resuming_run run_id=%s",
                number, run_id,
            )
    base_sha = freeze_base(config["repo_dir"], base_branch)
    branch = task_branch(source_repo, number, run_id)
    run_info = (
        f"base_branch={base_branch} base_sha={base_sha} run_id={run_id}"
    )
    LOGGER.info(
        "issue=%s %s", number, run_info,
    )
    edit_issue(number, repo=source_repo, add=IN_PROGRESS_LABEL)
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
        comment_issue(
            number, repo=source_repo,
            body=started_pi_comment_body(run_id, run_info, branch, worktree),
        )
        publisher.ensure(_progress_body(_progress_state(
            issue=number, run_id=run_id, role=ROLE_IMPLEMENT,
            branch=branch, worktree=worktree, started=started,
            pr_url=None, review_round=0,
        )))
        publisher.milestone(
            f"started: {run_info} branch={branch} worktree={worktree}"
        )
        run_pi(
            issue, worktree, config, source_repo, branch=branch,
            progress=LiveProgressThrottle(
                publisher, issue=number, run_id=run_id,
                role=ROLE_IMPLEMENT, branch=branch, worktree=worktree,
                started=started, pr_url=None, review_round=0,
            ),
        )
        _publish_plan_milestone(publisher, worktree)
        _publish_test_milestone(publisher, worktree)
        pr_url = verify_pr(
            worktree, branch, base_branch, run_id, issue=number,
        )
        commit = run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        )
        # No `fix pushed` milestone on a fresh claim: the implementer
        # always commits the delivery on top of the frozen base, so the
        # head always advanced — the PR opened milestone below announces
        # the delivery. `fix pushed` is the fixer's milestone
        # (resume_delivery), where it marks a real fix on an opened PR.
        edit_issue(
            number, repo=source_repo, add=PR_OPENED_LABEL,
            remove=IN_PROGRESS_LABEL,
        )
        # The delivery is complete here: the PR is verified and the
        # Issue has left the claim state for the review/fix loop. From
        # here on only the delivery-record publishing remains, and a
        # failure there must NOT fail the delivery (Issue #60: the
        # #57 delivered PATCH 404'd and the runner labeled the Issue
        # ai-blocked, skipping the review of a valid PR). Each step is
        # best-effort and independent — like the in-stream callback,
        # an error is logged (`progress_publish_failed`) and the next
        # step still runs — and the run continues into the
        # review/merge wait loop either way.
        for step in (
            lambda: comment_issue(
                number, repo=source_repo,
                body=opened_pr_comment_body(run_id, run_info, pr_url),
            ),
            lambda: publisher.milestone(
                f"PR opened: {pr_url} ({run_info})"
            ),
            lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, run_id=run_id, role=ROLE_IMPLEMENT,
                branch=branch, worktree=worktree, started=started,
                pr_url=pr_url, review_round=0,
            ), outcome="**Muyan Pilot delivered**")),
        ):
            try:
                step()
            except Exception:
                LOGGER.exception(
                    "progress_publish_failed run=%s issue=%s role=%s",
                    run_id, issue_context(source_repo, number),
                    ROLE_IMPLEMENT,
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
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=IN_PROGRESS_LABEL,
            )
            detail = _failure_detail(exc)
            body = (
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot failed: {detail} ({run_info})"
            )
            if scene:
                body += f" {scene}"
            comment_issue(number, repo=source_repo, body=body)
            # The blocked milestone is posted even when the worktree was
            # never created or the progress comment was never ensured:
            # the mobile notification of the terminal failure must not
            # depend on local state.
            publisher.milestone(
                f"blocked: {sanitize(detail)} ({run_info})"
            )
            if worktree is not None and publisher.comment_id is not None:
                publisher.finish(_progress_body(_progress_state(
                    issue=number, run_id=run_id, role=ROLE_IMPLEMENT,
                    branch=branch, worktree=worktree, started=started,
                    pr_url=None, review_round=0,
                ), outcome=(
                    "**Muyan Pilot blocked**\n\n"
                    f"failure: {detail}\n"
                    "next step: fix the failure above and re-run "
                    "this Issue (a new run id is created automatically)"
                )))
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
    started = time.monotonic()
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
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
            issue=number, pr_repo=source_repo, expected_url=pr_url,
            require_latest_base=False,
        )
        merge_latest_base(worktree, base_branch)
        config = {**config, "base_sha": scene["base_sha"], "run_id": run_id}
        publisher.ensure(_progress_body(_progress_state(
            issue=number, run_id=run_id, role=ROLE_FIX,
            branch=branch, worktree=worktree, started=started,
            pr_url=verified_url, review_round=0,
        )))
        run_pi(
            issue, worktree, config, source_repo,
            branch=branch, pr_url=verified_url,
            progress=LiveProgressThrottle(
                publisher, issue=number, run_id=run_id,
                role=ROLE_FIX, branch=branch, worktree=worktree,
                started=started, pr_url=verified_url, review_round=0,
            ),
        )
        # Re-verify the SAME PR after the fixer pushed: the verified URL
        # must still exactly equal the recovered original PR URL.
        verified_url = verify_pr(
            worktree, branch, base_branch, run_id,
            issue=number, pr_repo=source_repo, expected_url=pr_url,
        )
        # The state transition comes first: the Issue leaves the
        # fix-needed state before the progress comment is recorded, so
        # an observer never sees a "fixed PR" comment on an Issue that
        # is still fix-needed (a crash in between re-runs the fixer,
        # which is idempotent; the comment is a record, the label is
        # the state).
        edit_issue(
            number, repo=source_repo, add=PR_OPENED_LABEL,
            remove=FIX_NEEDED_LABEL,
        )
        # The fix is delivered here: the PR is re-verified and the
        # Issue is back in `ai-pr-opened`. From here on only the
        # delivery-record publishing remains, and a failure there must
        # NOT fail the delivery (Issue #60, same contract as the fresh
        # claim): each step is best-effort and independent — an error
        # is logged (`progress_publish_failed`) and the next step
        # still runs — and the run continues into the review/merge
        # wait loop either way.
        for step in (
            lambda: comment_issue(
                number, repo=source_repo,
                body=(
                    f"{run_marker(run_id)}\n"
                    f"Muyan Pilot fixed PR: {verified_url} ({scene_info})"
                ),
            ),
            lambda: publisher.milestone(
                f"fix pushed: {verified_url} ({scene_info})"
            ),
            lambda: publisher.finish(_progress_body(_progress_state(
                issue=number, run_id=run_id, role=ROLE_FIX,
                branch=branch, worktree=worktree, started=started,
                pr_url=verified_url, review_round=0,
            ), outcome=(
                "**Muyan Pilot fix pushed**\n\n"
                "the same PR is awaiting review again; the independent "
                "review and merge happen automatically"
            ))),
        ):
            try:
                step()
            except Exception:
                LOGGER.exception(
                    "progress_publish_failed run=%s issue=%s role=%s",
                    run_id, issue_context(source_repo, number), ROLE_FIX,
                )
    except Exception as exc:
        LOGGER.exception("issue=%s resume failed", number)
        activity_scene = ""
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
            activity_scene = format_run_scene(
                snapshot,
                run_id=run_id, issue=issue_context(source_repo, number),
                role=ROLE_IMPLEMENT, branch=branch, worktree=str(worktree),
            )
        except Exception:
            LOGGER.exception("issue=%s activity scene failed", number)
        try:
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=FIX_NEEDED_LABEL,
            )
            detail = _failure_detail(exc)
            body = (
                f"{run_marker(run_id)}\n"
                f"Muyan Pilot failed: {detail} ({scene_info})"
            )
            if activity_scene:
                body += f" {activity_scene}"
            comment_issue(number, repo=source_repo, body=body)
            # The blocked milestone is posted even when the progress
            # comment was never created (a failure before `ensure`):
            # the mobile notification of the terminal failure must not
            # depend on it.
            publisher.milestone(
                f"blocked: {sanitize(detail)} ({scene_info})"
            )
            if publisher.comment_id is not None:
                publisher.finish(_progress_body(_progress_state(
                    issue=number, run_id=run_id, role=ROLE_FIX,
                    branch=branch, worktree=worktree,
                    started=started, pr_url=pr_url,
                    review_round=0,
                ), outcome=(
                    "**Muyan Pilot blocked**\n\n"
                    f"failure: {detail}\n"
                    "next step: fix the failure above and resume this "
                    "same PR (branch, worktree and PR are preserved)"
                )))
        except Exception:
            LOGGER.exception("issue=%s failure reporting failed", number)
        raise
    # The fix succeeded: the Issue is back in awaiting review (the
    # transition happened before the progress comment), so the next
    # tick does not re-run the Fixer (a clean PR is simply waiting for
    # review now).
    return verified_url


def pr_state(pr_url: str) -> str:
    """Return the GitHub state of one PR: `OPEN`, `MERGED` or `CLOSED`.

    The delivery-wait loop (Issue #39) uses it to tell a delivery that is
    still awaiting review from one that is done: only `MERGED` or
    `CLOSED` ends the slot hold. Anything else is a corrupted state and
    fails fast.
    """
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    raw = run_command([
        "gh", "pr", "view", number, "--json", "state",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    return state


def issue_labels(number: int, repo: str) -> list[str]:
    """Return the current label names of one Issue."""
    raw = run_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "labels",
    ])
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError("issue labels must be a JSON array")
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _finish_blocked_progress(
    number: int, run_id: str | None, source_repo: str,
    worktree: Path | None, branch: str | None, pr_url: str,
    detail: str, next_step: str,
    role: str = ROLE_FIX, review_round: int = 0,
) -> None:
    """Finish the tracked progress comment with the blocked scene.

    The contract (Issue #18): on failure the progress comment becomes
    the blocked scene with the next-step reason — the same terminal
    body the `process_issue` and `resume_delivery` failure paths write.
    `ensure` finds the run's existing progress comment by its hidden
    marker (PATCHing it in place) or creates it when the run never
    reached one; either way the blocked scene is the final state.
    `role` and `review_round` are the actual role and completed review
    rounds of the blocked run (review round 2, PR #42): the caller
    derives them from the Issue's delivery label and trusted
    review-round comments, so the terminal comment never shows a stale
    hardcoded `fix`/`0`.
    """
    if run_id is None:
        return
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    publisher.ensure(_progress_body(_progress_state(
        issue=number, run_id=run_id, role=role,
        branch=branch or "-", worktree=worktree or Path("-"),
        started=time.monotonic(), pr_url=pr_url,
        review_round=review_round,
    ), outcome=(
        "**Muyan Pilot blocked**\n\n"
        f"failure: {detail}\n"
        f"next step: {next_step}"
    )))


def wait_for_delivery(pr_url: str, issue: dict, config: dict,
                      source_repo: str, poll_interval: float = PI_POLL_INTERVAL) -> None:
    """Own the delivery lifecycle: hold the slot until merge or failure.

    The slot is acquired by `main` before the claim and must stay
    occupied through implement -> review -> fix -> merge (Issue #39): a
    delivery whose PR is open still needs the machine, and no other
    Runner may start a second Pi while it is held. The Runner is the
    owner of that lifecycle, so it re-checks the delivery every
    `poll_interval` seconds (the same cadence as the idle timer and the
    Pi activity poll):

    - PR `MERGED` -> terminal: the delivery is done, the slot is
      released by the caller and the next tick may claim new work;
    - PR `CLOSED` without merge -> terminal failure: the Issue is
      marked `ai-blocked` (removing `ai-pr-opened`/`ai-fix-needed`) with
      a failure comment carrying the run marker, then the slot is
      released by the caller;
    - Issue in the explicit `ai-fix-needed` state (a review finding or
      a base conflict) -> the Runner runs the SAME-PR fix (Issue #45)
      itself, on the same run, in the same worktree, while still holding
      the slot; the fix returns the Issue to `ai-pr-opened` and the
      wait continues. A new Runner can never take the slot to fix this
      delivery while it is held, so the fix must happen here;
    - Issue awaiting review (`ai-pr-opened`) -> the Runner runs the
      independent review of the frozen PR (Issue #34) itself, on the
      same run, while still holding the slot: a clean verdict merges
      the PR via the merge gate, confirms the merge, syncs the
      deployment checkout and labels the Issue `ai-merged` (terminal,
      the slot is released); findings label the Issue `ai-fix-needed`
      and the fix step above repairs the same PR before the next
      iteration re-reviews;
    - otherwise -> keep holding the slot and re-check.

    There is no timeout: systemd owns the run lifecycle and kills the
    service on stop, which releases the slot via the kernel (flock on
    the open descriptor). No polling shim, no daemon loop, no queue.
    """
    number = int(issue["number"])
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    LOGGER.info(
        "issue=%s delivery_awaiting pr=%s; holding the slot until the "
        "PR is merged or terminally failed",
        number, pr_url,
    )
    while True:
        state = pr_state(pr_url)
        if state == "MERGED":
            LOGGER.info(
                "issue=%s delivery_merged pr=%s; releasing the slot",
                number, pr_url,
            )
            return
        if state == "CLOSED":
            LOGGER.info(
                "issue=%s delivery_closed_unmerged pr=%s; marking the "
                "Issue ai-blocked and releasing the slot",
                number, pr_url,
            )
            edit_issue(
                number, repo=source_repo, add=BLOCKED_LABEL,
                remove=PR_OPENED_LABEL,
            )
            body = (
                f"Muyan Pilot failed: PR {pr_url} was closed without "
                "a merge; the delivery is terminally failed"
            )
            if marker:
                body = f"{marker}\n{body}"
            comment_issue(number, repo=source_repo, body=body)
            if run_id:
                ProgressPublisher(
                    number, source_repo, run_id,
                    run_command=run_command,
                ).milestone(
                    f"blocked: PR {pr_url} was closed without a merge; "
                    "the delivery is terminally failed"
                )
                # The blocked scene carries the actual role and the
                # completed review rounds (review round 2, PR #42):
                # the delivery label says which role was in flight
                # (ai-fix-needed -> fix, otherwise the awaiting-review
                # review), and the trusted review-round comments bound
                # the round count (GitHub is the only state store).
                blocked_role = (
                    ROLE_FIX if FIX_NEEDED_LABEL in
                    issue_labels(number, source_repo)
                    else ROLE_REVIEW
                )
                blocked_round = review_rounds_so_far(
                    issue_comments(number, repo=source_repo),
                )
                # The tracked progress comment becomes the blocked scene
                # (Issue #18): the same terminal body the other failure
                # paths write, with the next-step reason.
                _finish_blocked_progress(
                    number, run_id, source_repo, None, None, pr_url,
                    f"PR {pr_url} was closed without a merge; the "
                    "delivery is terminally failed",
                    "investigate why the PR was closed and re-open the "
                    "delivery or start a fresh run on the Issue",
                    role=blocked_role, review_round=blocked_round,
                )
            return
        labels = issue_labels(number, source_repo)
        if FIX_NEEDED_LABEL in labels and BLOCKED_LABEL not in labels:
            # The review found a problem (or the base advanced): fix the
            # SAME PR on the SAME run while still holding the slot.
            LOGGER.info(
                "issue=%s delivery_fix_needed pr=%s; resuming the fix "
                "loop of the same PR",
                number, pr_url,
            )
            scene = resume_scene(issue_comments(number, repo=source_repo))
            fix_issue = dict(issue)
            fix_issue["body"] = issue_body(number, repo=source_repo)
            resume_delivery(fix_issue, scene, config, source_repo)
            continue  # the fix returned the Issue to awaiting review
        if (PR_OPENED_LABEL in labels and BLOCKED_LABEL not in labels
                and MERGED_LABEL not in labels):
            # The PR is awaiting review: run the independent review of
            # the frozen PR on the same run (Issue #34). A clean
            # verdict merges and returns True (terminal); findings
            # label the Issue `ai-fix-needed` and the fix step above
            # repairs the same PR before the next iteration re-reviews.
            # A review that cannot run (unrecoverable scene, missing
            # worktree, missing/malformed verdict, exhausted rounds)
            # is a real failure: the Issue is marked `ai-blocked` so
            # it is never stranded in awaiting review without an owner.
            worktree = None
            branch = None
            try:
                scene = resume_scene(
                    issue_comments(number, repo=source_repo),
                )
                worktree = worktree_path(
                    config["repo_dir"], source_repo, number,
                    scene["run_id"],
                )
                branch = task_branch(source_repo, number, scene["run_id"])
                review_config = {
                    **config,
                    "base_sha": scene["base_sha"],
                    "run_id": scene["run_id"],
                }
                merged = review_and_merge_if_clean(
                    worktree, branch, config["base_branch"],
                    review_config, source_repo, number,
                )
            except Exception as exc:
                LOGGER.exception(
                    "issue=%s delivery_review_failed pr=%s", number, pr_url,
                )
                edit_issue(
                    number, repo=source_repo, add=BLOCKED_LABEL,
                    remove=PR_OPENED_LABEL,
                )
                body = (
                    f"Muyan Pilot failed: the independent review of "
                    f"PR {pr_url} failed: {exc}"
                )
                if marker:
                    body = f"{marker}\n{body}"
                comment_issue(number, repo=source_repo, body=body)
                if run_id:
                    ProgressPublisher(
                        number, source_repo, run_id,
                        run_command=run_command,
                    ).milestone(
                        f"blocked: the independent review of PR {pr_url} "
                        f"failed: {exc}"
                    )
                    # The blocked scene carries the actual role and the
                    # completed review rounds (review round 2, PR #42):
                    # the failure happened during the independent
                    # review, and the trusted review-round comments
                    # bound the round count (GitHub is the only state
                    # store).
                    _finish_blocked_progress(
                        number, run_id, source_repo, worktree, branch,
                        pr_url,
                        f"the independent review of PR {pr_url} failed: "
                        f"{exc}",
                        "fix the review failure above and resume the "
                        "delivery of this same PR",
                        role=ROLE_REVIEW,
                        review_round=review_rounds_so_far(
                            issue_comments(number, repo=source_repo),
                        ),
                    )
                return
            if merged:
                LOGGER.info(
                    "issue=%s delivery_auto_merged pr=%s; releasing the "
                    "slot",
                    number, pr_url,
                )
                return
            continue  # findings: the fix loop repairs, then re-review
        LOGGER.info(
            "issue=%s delivery_awaiting pr=%s state=%s",
            number, pr_url, state,
        )
        time.sleep(poll_interval)


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
    # Concurrency cap (Issue #39): take one slot BEFORE claiming anything.
    # The slot is held for the whole delivery lifecycle (implement ->
    # review -> fix -> merge) and released only after the delivery is
    # merged or terminally failed — or when this process exits for any
    # reason, which the kernel handles (flock on an open descriptor).
    slot = acquire_slot(
        config["slot_dir"], config["max_concurrency"], os.getpid(),
    )
    if slot is None:
        LOGGER.info(
            "capacity_full max_concurrency=%s slot_dir=%s",
            config["max_concurrency"], config["slot_dir"],
        )
        return 0
    try:
        selected = pick_next_delivery(
            config["source_repos"], config["slot_dir"],
            config["max_concurrency"],
        )
        if selected is None:
            LOGGER.info(
                "source_repos=%s outcome=no_ready_issue",
                config["source_repos"],
            )
            return 0
        source_repo, issue, scene = selected
        if scene is not None:
            # An open PR is a recoverable review/fix state: resume the
            # same run on the same branch, worktree and PR (Issue #45).
            # Bind the scene's run id first so every journal line and
            # GitHub comment of the resumed delivery carries it
            # (Issue #41). The explicit `ai-fix-needed` state runs the
            # Fixer on the same PR; a delivery that is simply awaiting
            # review (`ai-pr-opened`, stranded by a dead runner or the
            # progress failure of Issue #70) goes straight to the
            # independent review — a clean PR is never sent to the
            # Fixer (Issue #45 round-5 contract).
            set_run_id(scene["run_id"])
            labels = issue_labels(int(issue["number"]), source_repo)
            if FIX_NEEDED_LABEL in labels:
                pr_url = resume_delivery(
                    issue, scene, config, source_repo,
                )
            else:
                pr_url = scene["pr_url"]
        else:
            pr_url = process_issue(issue, config, source_repo)
        # The delivery is not done when the PR is open: hold the slot
        # through review -> fix -> merge and release it only after the
        # PR is merged or terminally failed (Issue #39).
        wait_for_delivery(pr_url, issue, config, source_repo)
    finally:
        slot.release()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
