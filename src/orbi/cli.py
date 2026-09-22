#!/usr/bin/env python3
"""Orbi task dispatch and status CLI.

With NO subcommand the CLI IS the Runner entry: it runs one
tick, exactly like `python3 -m orbi.runner` — this is what the
scheduler entry (the systemd service's `ExecStart` on Linux, the
launchd agent on macOS; the installed `orbi`) invokes on
every trigger. The named subcommands are the dispatch and debug
entries on top of that:

`add` creates an Issue in a configured source repo and labels it
`ai-ready`.
`status` reports the current in-progress Issue (with its live Pi
activity: phase, last activity time, the last meaningful action, the
newest tool call result, session file and worktree), the next ready
Issue, and the most recent result (`ai-pr-opened` / `ai-fix-needed` /
`ai-merged` / `ai-blocked`) per source repo.

GitHub Issues and labels are the only state store. There is no database,
queue, or web UI. Command failures are logged and raised by the reused
runner.run_command; there is no fallback.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from orbi import __version__, cli_source, config as config_domain, engine_source, git_transport, runner, scheduler, milestone
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)

from orbi.github import list_milestones, merge_gate_preflight
from orbi.runner import (
    RunIdFilter,
    configure_logging,
    freeze_base,
    list_issues,
    log_format,
    run_command,
    validate_config,
    validate_execution_source_repos,
)
from orbi import pilot_setup
from orbi.pilot_slots import HELD_PID_UNKNOWN, slot_occupancy
from orbi.pi_activity import activity_snapshot

LOGGER = logging.getLogger("orbi.cli")
# Same run correlation mechanism as the runner: when a run id is
# bound, every journal line of this process starts with `[run_id]`.
LOGGER.addFilter(RunIdFilter())

# The CLI version: the single source of truth is the
# package `orbi.__version__` (imported above). tests/
# test_cli_packaging.py pins it against the PEP 621 `version` in
# pyproject.toml, so the two cannot drift.

ISSUE_URL_PATTERN = re.compile(r"/issues/(\d+)$")

DEFAULT_ISSUE_BODY = """## User outcome
Describe the observable result the user should get.

## Preconditions
- Configuration, access, or setup required:
- Real command or interaction:

## Acceptance
### Success path
- System action:
- User sees:

### Failure path
- Trigger:
- User sees:
- Repair action:

## Evidence
- Real entry point:
- Test, log, journal, Issue, PR, or UI evidence:
"""
# `ai-pr-opened` (awaiting review), `ai-fix-needed` (awaiting the next
# review session), `ai-merged` (the Runner merged the PR
# itself) and `ai-blocked` are all result states of an
# opened delivery.
RESULT_LABELS = (PR_OPENED_LABEL, FIX_NEEDED_LABEL, MERGED_LABEL, BLOCKED_LABEL)


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
    """Create an English-first Issue and mark it `ai-ready`."""
    url = create_issue(repo, title, body or DEFAULT_ISSUE_BODY)
    run_command([
        "gh", "issue", "edit", str(issue_number(url)), "--repo", repo,
        "--add-label", READY_LABEL,
    ])
    return url


def list_labeled_issues(repo: str, label: str, state: str = "open",
                        search: str | None = None) -> list[dict]:
    """Return the newest Issues matching a label search (gh lists newest first)."""
    return list_issues(
        repo, state=state, search=search or f"label:{label}",
        json_fields="number,title,url,state", limit=1,
    )


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
    (awaiting the next review session), `ai-merged` (success
    terminal, the Runner merged the PR itself) and `ai-blocked` (needs
    human attention).
    """
    newest = None
    for label in RESULT_LABELS:
        for issue in list_labeled_issues(repo, label, state="all"):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    return f"#{issue['number']} {issue['title']} {issue['url']}"


# Live Pi session following: the session subcommand is a
# debug attachment (the journal and GitHub remain the daily entry
# points). It finds the newest `.pi-session/*.jsonl` under the
# configured repo's `.worktrees` directory and prints its path, or
# follows it like `tail -f`. There is no tmux, no daemon, no new
# binary; a missing session file is a fail-fast non-zero exit (no Pi
# is running), never a guessed path.
FOLLOW_POLL_SECONDS = 0.5
SESSION_LINE_MAX = 200


def find_session_file(repo_dir: Path) -> Path | None:
    """Return the newest `.pi-session/*.jsonl` under `repo_dir/.worktrees`.

    Task worktrees live in `<repo_dir>/.worktrees/orbi-...` and
    each Pi session appends its JSONL under the worktree's
    `.pi-session` directory; the newest file by mtime is the live one.
    Returns None when no session file exists (no Pi is running).
    """
    worktrees = repo_dir / ".worktrees"
    if not worktrees.is_dir():
        return None
    files = [
        path for path in worktrees.glob("*/.pi-session/*.jsonl")
        if path.is_file()
    ]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def follow_session_file(path: Path,
                        poll_interval: float = FOLLOW_POLL_SECONDS) -> Iterator[str]:
    """Yield the lines of `path`, then its new lines as they appear.

    `tail -f` semantics for ONE file: the generator follows the file it
    was given and never switches to a newer file that appears mid-run.
    A file that disappears (worktree cleanup) stops the
    generator — fail fast, no fallback. A file that shrank is re-read
    from the start (the same rule as the session watcher). Only
    complete lines are yielded: a trailing partial line (the writer is
    still flushing it) is left for the next read, so a record is never
    split into fragments or dropped by the `--pretty` parser (the same
    rule as the session watcher).
    """
    offset = 0
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size < offset:
            offset = 0
        if size > offset:
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
            end = data.rfind("\n")
            if end < 0:
                # No complete line yet: the writer is still flushing
                # the current line; re-read it on the next poll.
                time.sleep(poll_interval)
                continue
            complete = data[: end + 1]
            offset += end + 1
            for line in complete.splitlines():
                if line:
                    yield line
        time.sleep(poll_interval)


def _session_content_summary(message: dict) -> str:
    """One short summary of a message record's content."""
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "toolCall":
                name = item.get("name")
                name = name if isinstance(name, str) and name else "tool"
                arguments = item.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                detail = arguments.get("command")
                if not isinstance(detail, str):
                    for key in ("path", "file_path", "tool", "query"):
                        value = arguments.get(key)
                        if isinstance(value, str) and value:
                            detail = value
                            break
                detail = detail if isinstance(detail, str) else ""
                parts.append(f"tool:{name} {detail}".strip())
            elif kind == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(f"text:{text}")
            elif kind == "thinking":
                text = item.get("thinking")
                if isinstance(text, str) and text:
                    parts.append(f"thinking:{text}")
        return "; ".join(parts)
    return ""


def format_session_line(record: dict) -> str:
    """Render one session JSONL record as a one-line summary.

    The summary carries the timestamp, the record kind and a short
    role/content digest (tool name + first argument, text or thinking
    truncated) — never the full prompt (user messages are summarized
    without their content). Long content is truncated so one record
    stays on one line.
    """
    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else "-"
    record_type = record.get("type")
    if record_type == "session":
        session_id = record.get("id")
        session_id = session_id if isinstance(session_id, str) else "-"
        return f"{timestamp} session {session_id}"
    if record_type != "message":
        return f"{timestamp} {record_type}"
    message = record.get("message")
    if not isinstance(message, dict):
        return f"{timestamp} message -"
    role = message.get("role")
    role = role if isinstance(role, str) and role else "-"
    if role == "toolResult":
        name = message.get("toolName")
        name = name if isinstance(name, str) and name else "tool"
        outcome = "error" if message.get("isError") else "ok"
        return f"{timestamp} toolResult {name} {outcome}"
    summary = _session_content_summary(message)
    if not summary:
        return f"{timestamp} {role}"
    if len(summary) > SESSION_LINE_MAX:
        summary = summary[:SESSION_LINE_MAX].rstrip() + "..."
    return f"{timestamp} {role} {summary}"


def latest_task_worktree(repo_dir: Path, source_repo: str,
                         number: int) -> Path | None:
    """Return the newest task worktree for an Issue, or None."""
    slug = source_repo.replace("/", "-")
    pattern = f".worktrees/orbi-{slug}-issue-{number}-*"
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


def slot_lines(state_dir: Path, capacity: int) -> list[str]:
    """Return the status lines for the configured concurrency capacity."""
    occupancy = slot_occupancy(state_dir, capacity)
    taken = sum(1 for _, pid in occupancy if pid is not None)
    lines = [f"slots: {taken}/{capacity}"]
    lines.extend(
        f"  slot-{index}: pid="
        + ("unknown (held)" if pid == HELD_PID_UNKNOWN else str(pid))
        for index, pid in occupancy
        if pid is not None
    )
    return lines


# Deployment consistency: the repo templates
# (systemd/orbi@.service + @.timer on Linux, launchd/org.orbi.runner.plist
# on macOS) are the single source of
# truth. `install-units` deploys them idempotently (it never
# starts/stops/restarts the service — a running Runner keeps running,
# the new config takes effect at the next service start) and enables
# the timer instances through max_concurrency (orbi@1..N.timer,
# Issue #827); `doctor`
# is the read-only report (repo commit, unit drift, timer/service
# instance state, slots, Pi session, current Issue, recent journal).
JOURNAL_LINES = 20


def deploy_home_dirty_files(repo_dir: Path, *, run_command) -> list[str]:
    """Return tracked files changed in the deployment home.

    The scheduler preflight cannot start Python when this checkout is dirty,
    so doctor uses the same porcelain status contract read-only. Untracked
    files are deliberately excluded, matching the preflight's own
    tracked-change contract (note an untracked file in the way of a
    merge CAN still abort the fast-forward — the exclusion is about
    contract parity, not a guarantee they are harmless).
    """
    status = run_command(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=repo_dir,
    )
    # run_command strips the stdout, so the first porcelain line can lose
    # its leading X field: slice the path from column 2 and strip, never
    # from column 3 of the raw line (the engine_source sync shares this
    # contract).
    return [
        line[2:].strip()
        for line in status.splitlines()
        if len(line) >= 3 and line[:2] != "??"
    ]


def install_units_command(config: config_domain.RunnerConfig, installed_dir: Path | None) -> str:
    """Run the idempotent unit install and return the deployment report.

    The report carries the deployed commit (the deployment checkout's
    HEAD — the commit the installed templates came from) and the
    installed sha256 of each unit.
    """
    # The unit templates live in the deployment home (they
    # render the home path into {{ORBI_REPO_DIR}}), never in the delivery
    # checkout.
    sched = scheduler.detect()
    result = scheduler.install_units(
        config.deploy_home, installed_dir,
        max_concurrency=config.max_concurrency,
        unit_name=config.unit_name, run_command=run_command, sched=sched,
    )
    lines = [
        (
            f"deployed commit={result['commit']} "
            f"installed_dir={result['installed_dir']}"
        ),
    ]
    for name, entry in result["units"].items():
        lines.append(f"unit={name} sha256={entry['sha256']}")
    return "\n".join(lines)


def doctor_report(config: config_domain.RunnerConfig, installed_dir: Path | None) -> str:
    """Read-only deployment and health report.

    Checks: repo commit, unit drift (the same comparison
    the pre-start check uses), timer/service active state, Runner
    slots, the live Pi session, the current Issue per source repo and
    the recent journal activity. Read-only: no labels, no units, no
    git mutation. A failed command fails fast (run_command).
    """
    sched = scheduler.detect()
    repo_dir = config.repo_dir
    if installed_dir is None:
        installed_dir = sched.installed_unit_dir()
    lines = [f"repo: {repo_dir}"]
    lines.append(
        f"commit: {run_command(['git', 'rev-parse', 'HEAD'], cwd=repo_dir)}"
    )
    # Engine source update channel: the configured track,
    # the resolved ref/tag and the deployment home's HEAD SHA. Read-only
    # local git reads; an unresolvable channel is REPORTED (FAILED) with
    # its structured reason while the rest of the report stays readable.
    status = engine_source.engine_source_status(
        config.deploy_home, config.engine_source_track,
        run_command=run_command,
    )
    head_text = status["head"] or "-"
    if status["error"] is not None:
        lines.append(f"engine_source: FAILED {status['error']}")
    elif status["ok"]:
        lines.append(
            f"engine_source: track={status['track']} "
            f"resolved={status['resolved']} head={head_text}"
        )
    else:
        lines.append(
            f"engine_source: DRIFT track={status['track']} "
            f"resolved={status['resolved']} head={head_text} "
            f"expected={status['expected']} "
            "(the next service start syncs it)"
        )
    dirty_files = deploy_home_dirty_files(
        config.deploy_home, run_command=run_command,
    )
    if dirty_files:
        lines.append("deploy_home: DRIFT")
        lines.append(f"  files: {', '.join(dirty_files)}")
        lines.append(
            "  fix: git -C "
            f"{config.deploy_home} stash && "
            f"{sched.restart_hint(config.unit_name)}"
        )
    else:
        lines.append("deploy_home: clean")
    # Git transport: the checkout's origin
    # protocol, the expected URL of the first configured source repo
    # for the CONFIGURED transport (orbi.toml git_transport) and its
    # reachability probe. doctor is the diagnostic report: a failed
    # transport is REPORTED with the structured reason (the rest of
    # the report stays readable) — the fail-fast gate is the pre-start
    # check.
    try:
        transport = git_transport.check_transport(
            repo_dir, config.source_repos,
            run_command=run_command, migrate=False,
            mode=config.git_transport,
        )
        reachable = transport["transport_reachable"]
        reachable_text = "-" if reachable is None else (
            "true" if reachable else "false"
        )
        ssh_reachable = transport["ssh_reachable"]
        ssh_text = "-" if ssh_reachable is None else (
            "true" if ssh_reachable else "false"
        )
        lines.append(
            f"transport: remote={transport['remote']} "
            f"url={transport['url']} protocol={transport['protocol']} "
            f"expected={transport['expected']} "
            f"ssh_reachable={ssh_text} "
            f"transport_reachable={reachable_text}"
        )
    except git_transport.TransportError as exc:
        lines.append(f"transport: FAILED {exc}")
    # Unit drift is compared against the deployment home's
    # templates (the same comparison the pre-start check uses).
    status = scheduler.unit_status(
        config.deploy_home, installed_dir, config.unit_name,
        max_concurrency=config.max_concurrency, sched=sched,
    )
    drifted = [entry for entry in status if entry["drifted"]]
    if drifted:
        lines.append("unit_drift: DRIFT")
        for entry in drifted:
            lines.append(
                f"  {entry['unit']}: repo={entry['repo_path']} "
                f"installed={entry['installed_path']} "
                f"repo_sha256={entry['repo_sha256'] or '-'} "
                f"installed_sha256={entry['installed_sha256'] or '-'}"
            )
        lines.append(f"  fix: {scheduler.FIX_COMMAND}")
    else:
        lines.append("unit_drift: clean")
        for entry in status:
            lines.append(
                f"  {entry['unit']}: sha256={entry['installed_sha256']}"
            )
    # Hand-written orbi units without the managed template form
    # are invisible to every deployment's check_unit_drift — the drift
    # self-heal never reaches them. Doctor surfaces them read-only so
    # the bypass becomes visible instead of silently failing.
    unmanaged = sched.unmanaged_entries(installed_dir)
    lines.append(f"unmanaged_units: {len(unmanaged)}")
    for entry in unmanaged:
        lines.append(
            f"  {entry['unit']} (ORBI_CONFIG="
            f"{entry['config'] if entry['config'] else '-'})"
        )
    if unmanaged:
        lines.append(f"  fix: {scheduler.UNMANAGED_FIX}")
    finding = config.pi_provider_key_finding
    if finding and finding.get("variable") != "-":
        lines.append(
            "model_endpoint: provider="
            f"{finding['provider']} key={finding['variable']} "
            f"{finding['state']} (file: {finding['path']})"
        )
    provider = pilot_setup.model_provider_status(config)
    if provider["state"] == "ok":
        lines.append(
            f"model_provider: ok provider={provider['provider']} "
            f"model={provider['model']} key={provider['key']}"
        )
    else:
        lines.append(
            "model_provider: NOT CONFIGURED "
            f"provider_file={provider['provider_file']} "
            f"key={provider['env_variable']} "
            "(edit orbi.toml pi_providers/pi_provider/pi_model and "
            f"{provider['env_file']})"
        )
    # CLI source: the official local deployment is the
    # editable uv tool install — the tool env imports `orbi`
    # from the deployment checkout, so the ExecStartPre sync is picked
    # up by the next CLI process. A non-editable (site-packages) or
    # stale (different checkout) source is REPORTED with the
    # structured `cli_source_drift` line (actual path, expected
    # repo_dir, the exact editable reinstall command — the fix leads
    # with the editable reinstall, never with
    # `orbi install-units` alone). Read-only: the report stays
    # readable and the rest of the health report is still produced.
    # The editable CLI source is expected in the deployment
    # home, not the delivery checkout.
    source = cli_source.cli_source(config.deploy_home)
    line = cli_source.drift_line(source)
    if line is None:
        lines.append(f"cli_source: clean source={source['actual']}")
    else:
        lines.append("cli_source: DRIFT")
        lines.append(f"  {line}")
    # Report the INSTANCES (verified against the real CLIs: `systemctl
    # show` rejects the bare template name, and `journalctl
    # -u` with a template-name glob fails when no instance exists —
    # instance names always work).
    for unit in dict.fromkeys((
            *sched.timer_instances(config.unit_name, config.max_concurrency),
            *sched.service_instances(
                config.unit_name, config.max_concurrency))):
        state = sched.unit_state(run_command, unit)
        lines.append(f"{unit}: {state}")
    lines.extend(slot_lines(config.slot_dir, config.max_concurrency))
    session = find_session_file(repo_dir)
    lines.append(f"pi: {session if session else 'none'}")
    for repo in config.source_repos:
        lines.append(f"source: {repo}")
        lines.extend(f"  {line}" for line in merge_gate_preflight(
            repo, config.base_branch,
        ))
        current = current_issue(repo)
        lines.append(f"  current: {format_issue(current) if current else '-'}")
    journal = "\n".join(sched.journal_lines(
        run_command, config.deploy_home, config.unit_name,
        max_concurrency=config.max_concurrency, lines=JOURNAL_LINES,
    ))
    lines.append("journal:")
    for line in journal.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


def status_report(config: config_domain.RunnerConfig) -> str:
    lines = [
        f"capacity: {config.max_concurrency}",
        *slot_lines(config.slot_dir, config.max_concurrency),
    ]
    for repo in config.source_repos:
        lines.append(f"source: {repo}")
        base_sha = freeze_base(config.repo_dir, config.base_branch)
        lines.append(f"  base: {config.base_branch} {base_sha}")
        current = current_issue(repo)
        lines.append(f"  current: {format_issue(current) if current else '-'}")
        if current is not None:
            lines.extend(
                live_activity_lines(config.repo_dir, repo, current),
            )
        for name, lookup in (
            ("ready", ready_issue),
            ("result", recent_result),
        ):
            issue = lookup(repo)
            lines.append(f"  {name}: {format_issue(issue) if issue else '-'}")
    return "\n".join(lines)


class MilestoneSetError(Exception):
    """The `milestone set` fail-fast error: one structured line (reason + fix)."""


def milestone_set(config: config_domain.RunnerConfig, config_path: Path,
                  title: str) -> tuple[str, str]:
    """Advance `active_milestone` to one exact Milestone title (Issue #895).

    The manual advance behind the `auto_next_milestone = false`
    confirmation flow. The claim scope the Runner reads is set the same
    way it is resolved: the title must exist as exactly ONE Milestone
    on the source repo (GitHub Milestone titles are not unique, so a
    duplicate exact title is a hard error, never a guess), then ONLY
    the `active_milestone` line is rewritten — comments, blank lines
    and every other field stay byte-identical. The variable sync is NOT
    part of this command: the Runner's next tick publishes
    `ORBI_ACTIVE_MILESTONE` (the bypass contract). Returns (old, new);
    every failure raises MilestoneSetError with the config untouched.
    """
    repo = config.source_repos[0]
    if config.active_milestone is None:
        raise MilestoneSetError(
            f"milestone_set_failed reason=no active_milestone line in "
            f"{config_path}; fix=add "
            '`active_milestone = "<current>"` to the config first '
            "(the field is never created implicitly)"
        )
    try:
        milestones = list_milestones(repo, timeout=30)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # The docstring promises one structured line for EVERY failure:
        # a hung gh raises TimeoutExpired, a missing gh raises OSError,
        # a malformed payload raises ValueError — same collapse.
        detail = (getattr(exc, "stderr", "") or "").strip() or str(exc)
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone lookup failed: {detail}; "
            "fix=check `gh auth status` and Milestone read access to "
            f"{repo}"
        ) from exc
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict) and milestone.get("title") == title
    ]
    if not matches:
        open_list = ", ".join(
            f"{milestone.get('title')}({milestone.get('open_issues')})"
            for milestone in milestones
            if isinstance(milestone, dict) and milestone.get("state") == "open"
        ) or "(none)"
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone_not_found title={title!r} "
            f"repo={repo} open milestones: {open_list}; fix=use one exact "
            "title from `gh api "
            f"repos/{repo}/milestones?state=open --jq '.[].title'`"
        )
    if len(matches) > 1:
        raise MilestoneSetError(
            f"milestone_set_failed reason=milestone_ambiguous title={title!r} "
            f"repo={repo}: the exact title matches {len(matches)} "
            "Milestones; fix=rename or close the duplicate Milestone first"
        )
    try:
        milestone.rewrite_active_milestone_line(config_path, title)
    except (OSError, RuntimeError) as exc:
        raise MilestoneSetError(
            f"milestone_set_failed reason={exc}; "
            f"fix=repair the config file at {config_path}"
        ) from exc
    return config.active_milestone, title


def _installed_unit_configs(
    installed_dir: Path | None = None,
) -> tuple[tuple[Path, str], ...]:
    """Return the ORBI_CONFIG values declared by installed units.

    This is deliberately a filesystem read: resolving a missing default must
    not invoke a scheduler command or mutate the user's installation.
    """
    sched = scheduler.detect()
    installed_dir = installed_dir or sched.installed_unit_dir()
    if not installed_dir.is_dir():
        return ()
    found: dict[Path, list[str]] = {}
    for unit in sorted(installed_dir.iterdir(), key=lambda path: path.name):
        # Only inspect files belonging to Orbi's scheduler namespace.  User
        # unit directories commonly contain unrelated services; accepting an
        # arbitrary service's ORBI_CONFIG could select the wrong deployment.
        is_systemd_unit = (
            unit.name.startswith("orbi")
            and unit.name.endswith((".service", ".timer"))
        )
        is_launchd_unit = (
            unit.name.startswith("org.orbi.") and unit.name.endswith(".plist")
        )
        if not unit.is_file() or not (is_systemd_unit or is_launchd_unit):
            continue
        config = sched.unit_config(unit)
        if config is not None:
            found.setdefault(config, []).append(unit.name)
    return tuple(
        (path, ", ".join(units))
        for path, units in sorted(found.items(), key=lambda item: str(item[0]))
    )


def _config_was_explicit(argv: list[str] | None) -> bool:
    values = sys.argv[1:] if argv is None else argv
    return any(value == "--config" or value.startswith("--config=")
               for value in values)


def _missing_config_message(
    path: Path, candidates: tuple[tuple[Path, str], ...],
) -> str:
    candidate_lines = "".join(
        f"; candidate={candidate} units={units}"
        for candidate, units in candidates
    )
    return (
        f"config_not_found path={path.resolve()}; "
        "reason=no Orbi config at this path "
        "(the default is `orbi.toml` in the current directory); "
        "fix=run from the deployment directory, or point at the config: "
        "`ORBI_CONFIG=~/orbi-deploy/<dir>/orbi.toml orbi <command>` "
        "(`--config <path>` also works, after the subcommand). "
        "The source checkout is not a deployment directory and has no "
        f"orbi.toml.{candidate_lines}"
    )


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("ORBI_CONFIG", "orbi.toml")),
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    parser.add_argument(
        "--version", action="version",
        version=f"orbi {__version__}",
    )
    # The subcommand is OPTIONAL — with no subcommand the
    # installed CLI runs one Runner tick (the systemd ExecStart is the
    # bare `orbi`), exactly like `python3 -m
    # orbi.runner`.
    subparsers = parser.add_subparsers(dest="command")
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
    session_parser = subparsers.add_parser(
        "session", parents=[common],
        help="print the live Pi session JSONL path, or follow it like tail -f",
    )
    session_parser.add_argument(
        "--follow", action="store_true",
        help="keep printing new lines of the selected file (tail -f)",
    )
    session_parser.add_argument(
        "--pretty", action="store_true",
        help="print one-line summaries instead of raw JSONL",
    )
    install_parser = subparsers.add_parser(
        "install-units", parents=[common],
        help="idempotently install the repo's scheduler units (systemd "
             "on Linux, launchd on macOS; never restarts a running "
             "Runner)",
    )
    install_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory (default: the standard user dir)",
    )
    subparsers.add_parser(
        "sync-engine-source", parents=[common],
        help="sync the deploy home checkout to the configured "
             "engine_source_track (the service ExecStartPre runs this "
             "on every tick; Issue #535)",
    )
    doctor_parser = subparsers.add_parser(
        "doctor", parents=[common],
        help="read-only report: repo commit, unit drift, timer/service, "
             "slots, Pi, current Issue, recent journal",
    )
    doctor_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory to check (default: the standard dir)",
    )
    subparsers.add_parser(
        "check", parents=[common],
        help="read-only prerequisite gate: python, commands, the "
             "scheduler user session, gh auth, pi, config, repo access, "
             "transport, model provider — fails fast with reason, fix and "
             "official docs link (Issue #163)",
    )
    setup_parser = subparsers.add_parser(
        "setup", parents=[common],
        help="one-time, idempotent initialization: gh auth + repo "
             "permissions, platform labels, scheduler units, checkout "
             "check and the optional model proxy (Issue #117)",
    )
    setup_parser.add_argument(
        "--repo", default=None,
        help="initialize exactly this source repo (default: every "
             "configured source repo)",
    )
    setup_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory (default: the standard user dir)",
    )
    setup_parser.add_argument(
        "--json", action="store_true",
        help="print the result as JSON instead of key=value lines",
    )
    milestone_parser = subparsers.add_parser(
        "milestone", parents=[common],
        help="advance the config's active_milestone (the manual command "
             "behind the auto_next_milestone=false confirmation flow)",
    )
    milestone_subparsers = milestone_parser.add_subparsers(
        dest="milestone_command", required=True,
    )
    milestone_set_parser = milestone_subparsers.add_parser(
        "set", parents=[common],
        help="set active_milestone to one exact GitHub Milestone title",
    )
    milestone_set_parser.add_argument(
        "title",
        help="the exact Milestone title, e.g. v0.6.0 (must be unique)",
    )
    args = parser.parse_args(argv)
    configure_logging()

    # A missing default in a read-only command can be resolved from the
    # installed units.  Never do this for an explicit path or a mutating
    # command: selecting another deployment must remain the user's decision.
    candidates: tuple[tuple[Path, str], ...] = ()
    read_only = args.command in {"status", "doctor", "session", "check"}
    explicit = _config_was_explicit(argv) or "ORBI_CONFIG" in os.environ
    if read_only and not explicit and not args.config.exists():
        candidates = _installed_unit_configs(
            getattr(args, "installed_dir", None),
        )
        existing = tuple(item for item in candidates if item[0].is_file())
        if len(existing) == 1:
            args.config = existing[0][0]
            LOGGER.info(
                "config_resolved_from_unit path=%s unit=%s",
                existing[0][0], existing[0][1],
            )

    if args.command == "check" and not args.config.exists():
        # `check` owns the prerequisite gate, but a missing file must still
        # use the same actionable diagnostic whether the path was implicit or
        # explicitly supplied.  Explicit paths are never replaced by a unit
        # candidate.
        check_candidates = candidates or _installed_unit_configs(
            getattr(args, "installed_dir", None),
        )
        # Keep the actionable config hint, but do not let it hide the
        # independent machine failures on a first run.  The collection
        # mode deliberately reports every machine prerequisite before the
        # config diagnostic.
        _, machine_failures = pilot_setup.run_machine_checks(
            run_command=run_command, collect_failures=True,
        )
        for failure in machine_failures:
            print(pilot_setup.format_check_failure(failure), file=sys.stderr)
        print(
            _missing_config_message(args.config, check_candidates),
            file=sys.stderr,
        )
        return 1

    if args.command is None:
        # No subcommand = the Runner tick. Delegate to the
        # Runner's own main: it re-parses `--config` and owns the whole
        # tick contract (unit-drift preflight, transport preflight,
        # slot, claim, fail-fast) — the same behavior the
        # `python3 -m orbi.runner` entry has.
        return runner.main(["--config", str(args.config)])

    if args.command == "check":
        # The prerequisite gate owns its config handling — a
        # missing or invalid orbi.toml is a `config` finding with the
        # repair action, never a traceback. Read-only: no config is
        # created here (`orbi setup` owns that).
        try:
            lines = pilot_setup.run_checks(
                args.config, run_command=run_command,
            )
        except pilot_setup.CheckError as exc:
            print(pilot_setup.format_check_failure(exc), file=sys.stderr)
            return 1
        print("\n".join(lines))
        return 0

    try:
        if args.command == "setup":
            pilot_setup.ensure_config(args.config)
        config = config_domain.load_config(
            args.config,
            # The engine-source sync and the milestone advance are
            # deployment maintenance operations — a missing provider
            # key must never block them (the Runner's own start
            # enforces the key), so they load the config with the
            # doctor's lenient provider flags.
            check_provider_api_keys=args.command not in (
                "doctor", "sync-engine-source", "milestone",
            ),
            allow_missing_pi_providers=args.command in (
                "setup", "doctor", "sync-engine-source", "milestone",
            ),
        )
        validate_config(config)
        # Every command path (doctor included) enforces the
        # same single-source-repo contract as runner.main, so a config
        # the Runner will reject is reported instead of all-green.
        validate_execution_source_repos(config.source_repos)
    except (ValueError, pilot_setup.SetupError) as exc:
        if args.command == "setup":
            print(f"setup_failed reason={exc}", file=sys.stderr)
        else:
            LOGGER.error("config_invalid reason=%s", exc)
        return 1
    except config_domain.ConfigFileMissingError as exc:
        if not candidates:
            candidates = _installed_unit_configs(
                getattr(args, "installed_dir", None),
            )
        message = _missing_config_message(exc.path, candidates)
        if args.command == "setup":
            print(f"setup_failed reason={message}", file=sys.stderr)
        else:
            LOGGER.error(message)
        return 1
    except FileNotFoundError as exc:
        # A PyPI first run (`orbi setup` in a fresh dir with
        # the just-created example config) fails validation on a missing
        # deployment path (prompts/, deploy_home, ...); the user gets the
        # structured failure line, never a traceback.
        if args.command == "setup":
            print(
                f"setup_failed reason=required path missing: {exc}",
                file=sys.stderr,
            )
        else:
            LOGGER.error("config_invalid reason=required path missing: %s", exc)
        return 1
    if args.command == "add":
        repo = args.repo or config.source_repos[0]
        if repo not in config.source_repos:
            parser.error(
                f"--repo must be one of: {', '.join(config.source_repos)}"
            )
        LOGGER.info("dispatch repo=%s title=%s", repo, args.title)
        url = dispatch_issue(repo, args.title, args.body)
        print(f"created: {url}")
        print(f"label: {READY_LABEL}")
    elif args.command == "session":
        path = find_session_file(config.repo_dir)
        if path is None:
            print(
                f"no pi session under {config.repo_dir / '.worktrees'}: "
                "no Pi is running",
                file=sys.stderr,
            )
            return 1
        if not args.follow:
            if args.pretty:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict):
                            print(format_session_line(record))
            else:
                print(path)
            return 0
        for line in follow_session_file(path):
            if args.pretty:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    print(format_session_line(record))
            else:
                print(line)
            sys.stdout.flush()
    elif args.command == "install-units":
        print(install_units_command(config, args.installed_dir))
    elif args.command == "sync-engine-source":
        try:
            engine_source.sync_engine_source(
                config.deploy_home, config.engine_source_track,
                run_command=run_command,
            )
        except engine_source.EngineSourceError as exc:
            # The structured line (reason + fix) is the message; the
            # non-zero exit fails the ExecStartPre, so the service does
            # not start (fail closed).
            LOGGER.error("%s", exc)
            return 1
    elif args.command == "doctor":
        print(doctor_report(config, args.installed_dir))
    elif args.command == "setup":
        try:
            result = pilot_setup.run_setup(
                config, args.installed_dir,
                repos=[args.repo] if args.repo else None,
                run_command=run_command,
            )
        except pilot_setup.SetupError as exc:
            print(
                f"setup_failed reason={exc}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(pilot_setup.to_json(result))
        else:
            print("\n".join(pilot_setup.format_setup(result)))
    elif args.command == "milestone":
        try:
            old, new = milestone_set(config, args.config, args.title)
        except MilestoneSetError as exc:
            # One structured line with the actual reason and the repair
            # action; the config file is untouched on every failure path.
            print(exc, file=sys.stderr)
            return 1
        repo = config.source_repos[0]
        print(f"active_milestone: {old} -> {new}")
        print(f"repo: {repo}")
        print(f"config: {config.config_path}")
        print(
            "next: the Runner's next tick claims Issues from Milestone "
            f'"{new}" and syncs the ORBI_ACTIVE_MILESTONE variable '
            f"(verify: gh api repos/{repo}/actions/variables/"
            "ORBI_ACTIVE_MILESTONE --jq .value)"
        )
    else:
        print(status_report(config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
