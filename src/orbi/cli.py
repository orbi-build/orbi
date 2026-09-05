#!/usr/bin/env python3
"""Orbi task dispatch and status CLI.

With NO subcommand the CLI IS the Runner entry (Issue #140): it runs one
tick, exactly like `python3 -m orbi.runner` — this is what the
systemd service's `ExecStart` (the installed `orbi`) invokes on
every timer trigger. The named subcommands are the dispatch and debug
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
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from orbi import __version__, cli_source, git_transport, runner, systemd_deploy

from orbi.runner import (
    RunIdFilter,
    freeze_base,
    load_config,
    log_format,
    parse_issue_array,
    run_command,
    validate_config,
)
from orbi import pilot_setup
from orbi.pilot_slots import slot_occupancy
from orbi.pi_activity import activity_snapshot

LOGGER = logging.getLogger("orbi.cli")
# Same run correlation mechanism as the runner (Issue #41): when a run id is
# bound, every journal line of this process starts with `[run_id]`.
LOGGER.addFilter(RunIdFilter())

# The CLI version (Issue #140): the single source of truth is the
# package `orbi.__version__` (imported above). tests/
# test_cli_packaging.py pins it against the PEP 621 `version` in
# pyproject.toml, so the two cannot drift.

ISSUE_URL_PATTERN = re.compile(r"/issues/(\d+)$")
READY_LABEL = "ai-ready"
IN_PROGRESS_LABEL = "ai-in-progress"
# `ai-pr-opened` (awaiting review), `ai-fix-needed` (awaiting the next
# review session, Issue #82), `ai-merged` (the Runner merged the PR
# itself, Issue #34) and `ai-blocked` are all result states of an
# opened delivery (Issue #45).
RESULT_LABELS = ("ai-pr-opened", "ai-fix-needed", "ai-merged", "ai-blocked")


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
    (awaiting the next review session, Issue #82), `ai-merged` (success
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


# Live Pi session following (Issue #74): the session subcommand is a
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
    was given and never switches to a newer file that appears mid-run
    (Issue #74). A file that disappears (worktree cleanup) stops the
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
    """One short summary of a message record's content (Issue #74)."""
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
    """Render one session JSONL record as a one-line summary (Issue #74).

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
        f"  slot-{index}: pid={pid}"
        for index, pid in occupancy
        if pid is not None
    )
    return lines


# Deployment consistency (Issue #103, #149): the repo templates
# (systemd/orbi@.service + @.timer) are the single source of
# truth. `install-units` deploys them idempotently (it never
# starts/stops/restarts the service — a running Runner keeps running,
# the new config takes effect at the next service start) and enables
# the two timer instances (orbi@1.timer / @2.timer); `doctor`
# is the read-only report (repo commit, unit drift, timer/service
# instance state, slots, Pi session, current Issue, recent journal).
JOURNAL_LINES = 20


def deploy_home_dirty_files(repo_dir: Path, *, run_command) -> list[str]:
    """Return tracked files changed in the deployment home.

    The systemd preflight cannot start Python when this checkout is dirty,
    so doctor uses the same porcelain status contract read-only. Untracked
    files are deliberately excluded: they cannot block the fast-forward.
    """
    status = run_command(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=repo_dir,
    )
    return [
        line[3:]
        for line in status.splitlines()
        if len(line) >= 3 and line[:2] != "??"
    ]


def install_units_command(config: dict, installed_dir: Path | None) -> str:
    """Run the idempotent unit install and return the deployment report.

    The report carries the deployed commit (the deployment checkout's
    HEAD — the commit the installed templates came from) and the
    installed sha256 of each unit (Issue #103).
    """
    # Issue #330: the unit templates live in the deployment home (they
    # render the home path into {{ORBI_REPO_DIR}}), never in the delivery
    # checkout.
    result = systemd_deploy.install_units(
        config["deploy_home"], installed_dir,
        max_concurrency=config["max_concurrency"], run_command=run_command,
    )
    lines = [
        (
            f"deployed commit={result['commit']} "
            f"installed_dir={result['installed_dir']}"
        ),
    ]
    for name in systemd_deploy.UNIT_NAMES:
        entry = result["units"][name]
        lines.append(f"unit={name} sha256={entry['sha256']}")
    return "\n".join(lines)


def doctor_report(config: dict, installed_dir: Path | None) -> str:
    """Read-only deployment and health report (Issue #103).

    Checks: repo commit, unit drift (both units; the same comparison
    the pre-start check uses), timer/service active state, Runner
    slots, the live Pi session, the current Issue per source repo and
    the recent journal activity. Read-only: no labels, no units, no
    git mutation. A failed command fails fast (run_command).
    """
    repo_dir = config["repo_dir"]
    if installed_dir is None:
        installed_dir = systemd_deploy.installed_unit_dir()
    lines = [f"repo: {repo_dir}"]
    lines.append(
        f"commit: {run_command(['git', 'rev-parse', 'HEAD'], cwd=repo_dir)}"
    )
    dirty_files = deploy_home_dirty_files(
        config["deploy_home"], run_command=run_command,
    )
    if dirty_files:
        lines.append("deploy_home: DRIFT")
        lines.append(f"  files: {', '.join(dirty_files)}")
        lines.append(
            "  fix: git -C "
            f"{config['deploy_home']} stash && "
            "systemctl --user start orbi@1.service"
        )
    else:
        lines.append("deploy_home: clean")
    # Git transport (Issue #114): the checkout's origin protocol, the
    # expected SSH URL of the first configured source repo and the SSH
    # probe. doctor is the diagnostic report: a failed transport is
    # REPORTED with the structured reason (the rest of the report stays
    # readable) — the fail-fast gate is the pre-start check.
    try:
        transport = git_transport.check_transport(
            repo_dir, config["source_repos"],
            run_command=run_command, migrate=False,
        )
        reachable = transport["ssh_reachable"]
        reachable_text = "-" if reachable is None else (
            "true" if reachable else "false"
        )
        lines.append(
            f"transport: remote={transport['remote']} "
            f"url={transport['url']} protocol={transport['protocol']} "
            f"expected={transport['expected']} "
            f"ssh_reachable={reachable_text}"
        )
    except git_transport.TransportError as exc:
        lines.append(f"transport: FAILED {exc}")
    # Issue #330: unit drift is compared against the deployment home's
    # templates (the same comparison the pre-start check uses).
    status = systemd_deploy.unit_status(config["deploy_home"], installed_dir)
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
        lines.append(f"  fix: {systemd_deploy.FIX_COMMAND}")
    else:
        lines.append("unit_drift: clean")
        for entry in status:
            lines.append(
                f"  {entry['unit']}: sha256={entry['installed_sha256']}"
            )
    finding = config.get("pi_provider_key_finding")
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
    # CLI source (Issue #152): the official local deployment is the
    # editable uv tool install — the tool env imports `orbi`
    # from the deployment checkout, so the ExecStartPre sync is picked
    # up by the next CLI process. A non-editable (site-packages) or
    # stale (different checkout) source is REPORTED with the
    # structured `cli_source_drift` line (actual path, expected
    # repo_dir, the exact editable reinstall command — the fix leads
    # with the editable reinstall, never with
    # `orbi install-units` alone). Read-only: the report stays
    # readable and the rest of the health report is still produced.
    # Issue #330: the editable CLI source is expected in the deployment
    # home, not the delivery checkout.
    source = cli_source.cli_source(config["deploy_home"])
    line = cli_source.drift_line(source)
    if line is None:
        lines.append(f"cli_source: clean source={source['actual']}")
    else:
        lines.append("cli_source: DRIFT")
        lines.append(f"  {line}")
    # Issue #149: report the INSTANCES (verified against the real CLI:
    # `systemctl show` rejects the bare template name, and `journalctl
    # -u` with a template-name glob fails when no instance exists —
    # instance names always work).
    for unit in (*systemd_deploy.TIMER_INSTANCES,
                 *systemd_deploy.SERVICE_INSTANCES):
        state = run_command([
            "systemctl", "--user", "show", "-p", "ActiveState",
            "--value", unit,
        ])
        lines.append(f"{unit}: {state}")
    lines.extend(slot_lines(config["slot_dir"], config["max_concurrency"]))
    session = find_session_file(repo_dir)
    lines.append(f"pi: {session if session else 'none'}")
    for repo in config["source_repos"]:
        lines.append(f"source: {repo}")
        current = current_issue(repo)
        lines.append(f"  current: {format_issue(current) if current else '-'}")
    journal_args: list[str] = ["journalctl", "--user"]
    for unit in systemd_deploy.SERVICE_INSTANCES:
        journal_args.extend(["-u", unit])
    journal = run_command(journal_args + [
        "-n", str(JOURNAL_LINES), "--no-pager",
    ])
    lines.append("journal:")
    for line in journal.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


def status_report(config: dict) -> str:
    lines = [
        f"capacity: {config['max_concurrency']}",
        *slot_lines(config["slot_dir"], config["max_concurrency"]),
    ]
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
        default=Path(os.environ.get("ORBI_CONFIG", "orbi.toml")),
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    parser.add_argument(
        "--version", action="version",
        version=f"orbi {__version__}",
    )
    # Issue #140: the subcommand is OPTIONAL — with no subcommand the
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
        help="idempotently install the repo's systemd units (never "
             "restarts a running Runner)",
    )
    install_parser.add_argument(
        "--installed-dir", type=Path, default=None,
        help="user unit directory (default: the standard user dir)",
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
    setup_parser = subparsers.add_parser(
        "setup", parents=[common],
        help="one-time, idempotent initialization: gh auth + repo "
             "permissions, platform labels, systemd user units, checkout "
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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=log_format())

    if args.command is None:
        # Issue #140: no subcommand = the Runner tick. Delegate to the
        # Runner's own main: it re-parses `--config` and owns the whole
        # tick contract (unit-drift preflight, transport preflight,
        # slot, claim, fail-fast) — the same behavior the
        # `python3 -m orbi.runner` entry has.
        return runner.main(["--config", str(args.config)])

    try:
        if args.command == "setup":
            pilot_setup.ensure_config(args.config)
        config = load_config(
            args.config,
            check_provider_api_keys=args.command != "doctor",
            allow_missing_pi_providers=args.command in ("setup", "doctor"),
        )
        validate_config(config)
    except (ValueError, pilot_setup.SetupError) as exc:
        if args.command == "setup":
            print(f"setup_failed reason={exc}", file=sys.stderr)
        else:
            LOGGER.error("config_invalid reason=%s", exc)
        return 1
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
    elif args.command == "session":
        path = find_session_file(config["repo_dir"])
        if path is None:
            print(
                f"no pi session under {config['repo_dir'] / '.worktrees'}: "
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
    else:
        print(status_report(config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
