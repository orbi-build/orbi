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
import sys
from pathlib import Path

from orbi import (
    __version__,
    cli_source,
    config as config_domain,
    engine_source,
    git_transport,
    notify,
    runner,
    scheduler,
    suggest,
)
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)

from orbi.github import merge_gate_preflight
from orbi.milestone_command import MilestoneSetError, milestone_set
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
from orbi import pilot_setup, status_observe
from orbi.pilot_slots import HELD_PID_UNKNOWN, slot_occupancy
from orbi.source_pool import (
    installed_unit_configs as _installed_unit_configs,
    source_pool_lines,
)
from orbi.session_command import (
    find_session_file,
    follow_session_file,
    format_session_line,
)

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


def create_issue(repo: str, title: str, body: str, *,
                 milestone: str | None = None) -> str:
    """Create an Issue via gh and return its URL.

    `milestone` is the optional exact Milestone title; when given it is
    passed on the `gh issue create` call (`--milestone`), never guessed.
    """
    command = [
        "gh", "issue", "create", "--repo", repo,
        "--title", title, "--body", body,
    ]
    if milestone is not None:
        command += ["--milestone", milestone]
    return run_command(command)


def dispatch_issue(repo: str, title: str, body: str, *,
                   label: str = READY_LABEL,
                   milestone: str | None = None) -> str:
    """Create an English-first Issue, file it and mark it `label`.

    The ONE Issue-creation path (Article 4.2): `orbi add` uses the
    default `ai-ready`, `orbi suggest` passes the repository's resolved
    `dispatch_label` and its optional open Milestone.
    """
    url = create_issue(repo, title, body or DEFAULT_ISSUE_BODY,
                       milestone=milestone)
    run_command([
        "gh", "issue", "edit", str(issue_number(url)), "--repo", repo,
        "--add-label", label,
    ])
    return url


def list_labeled_issues(repo: str, label: str, state: str = "open",
                        search: str | None = None, *,
                        timeout: int | None = None) -> list[dict]:
    """Return the newest Issues matching a label search (gh lists newest first)."""
    return list_issues(
        repo, state=state, search=search or f"label:{label}",
        json_fields="number,title,url,state", limit=1, timeout=timeout,
    )


def current_issue(repo: str) -> dict | None:
    issues = list_labeled_issues(repo, IN_PROGRESS_LABEL)
    return issues[0] if issues else None


def ready_issue(repo: str) -> dict | None:
    """The ready queue, read under the status cycle's bound.

    The status view owns this read, so a stalled `gh issue list` fails
    the observation within the bound instead of hanging it (Issue #1668).
    """
    issues = list_labeled_issues(
        repo, READY_LABEL, search=f"label:{READY_LABEL} -label:{IN_PROGRESS_LABEL}",
        timeout=status_observe.STATUS_READ_TIMEOUT_SECONDS,
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
        for issue in list_labeled_issues(
            repo, label, state="all",
            timeout=status_observe.STATUS_READ_TIMEOUT_SECONDS,
        ):
            if newest is None or int(issue["number"]) > int(newest["number"]):
                newest = issue
    return newest


def format_issue(issue: dict) -> str:
    return f"#{issue['number']} {issue['title']} {issue['url']}"


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
    git mutation; the task-pool overlap check (Issue #1519) reads only
    the other deployments' `source_repos`. A failed command fails fast
    (run_command).
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
    # Task-pool overlap across the installed deployments (Issue #1519):
    # two differently named deployments that serve the same GitHub
    # repository are a topology the scheduler cannot coordinate, so the
    # operator must pick one for the pool. Read-only: only `source_repos`
    # is read from the other configs (never their env/provider secrets).
    lines.extend(source_pool_lines(config, installed_dir))
    finding = config.pi_provider_key_finding
    if finding and finding.get("variable") != "-":
        lines.append(
            "model_endpoint: provider="
            f"{finding['provider']} key={finding['variable']} "
            f"{finding['state']} (file: {finding['path']})"
        )
    lines.extend(notify.doctor_lines(config))
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


def status_report(config: config_domain.RunnerConfig,
                  scope: status_observe.StatusScope,
                  observer: status_observe.StatusObserver) -> str:
    """Render one scoped status view from the observer's complete reads.

    Capacity/slots, the frozen base and the queue/result summary are
    kept; the active-delivery section enumerates every concurrent
    delivery in scope with its attributed local observations. An
    explicit --issue scope shows that Issue's confirmed GitHub state and
    its retained, timed observations instead. Every GitHub-backed value
    comes from the observer's last complete GitHub cycle, so a local
    render reads no GitHub state.
    """
    lines = [
        f"capacity: {config.max_concurrency}",
        *slot_lines(config.slot_dir, config.max_concurrency),
    ]
    for repo in scope.repos:
        lines.append(f"source: {repo}")
        base_sha = observer.base.get(repo) or "-"
        lines.append(f"  base: {config.base_branch} {base_sha}")
        if scope.issue is not None:
            delivery = observer.confirmed.get(repo)
            if delivery is not None:
                lines.append(
                    f"  issue: {status_observe.format_delivery(delivery)}"
                )
            lines.extend(status_observe.observation_lines(
                observer.observations(repo, {scope.issue}).get(scope.issue, []),
            ))
        else:
            deliveries = observer.deliveries.get(repo, [])
            if not deliveries:
                lines.append("  active: -")
            for delivery in deliveries:
                lines.append(
                    f"  active: {status_observe.format_delivery(delivery)}"
                )
                lines.extend(status_observe.observation_lines(
                    observer.observations(repo, {delivery.number}).get(
                        delivery.number, [],
                    ),
                ))
        ready = observer.ready.get(repo)
        result = observer.result.get(repo)
        lines.append(f"  ready: {format_issue(ready) if ready else '-'}")
        lines.append(f"  result: {format_issue(result) if result else '-'}")
    lines.extend(observer.ended)
    return "\n".join(lines)


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
    suggest.register_cli(subparsers, common)
    status_parser = subparsers.add_parser(
        "status", parents=[common],
        help="show every active delivery with attributed local activity, "
             "ready queue and recent result per source repo",
    )
    status_parser.add_argument(
        "--repo", default=None,
        help="observe exactly this configured source repository",
    )
    status_parser.add_argument(
        "--issue", type=int, default=None,
        help="observe exactly this Issue (requires --repo when several "
             "repositories are configured)",
    )
    status_parser.add_argument(
        "--follow", action="store_true",
        help="keep observing: local events every 0.5s, GitHub state every "
             "15s, until Ctrl-C",
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
        help="advance active_milestone (the manual command behind the "
             "auto_next_milestone=false confirmation flow)",
    )
    milestone_subparsers = milestone_parser.add_subparsers(
        dest="milestone_command", required=True,
    )
    milestone_set_parser = milestone_subparsers.add_parser(
        "set", parents=[common],
        help="set active_milestone to one exact GitHub Milestone title: "
             "the repository policy (.github/orbi.toml) when it declares "
             "the key, otherwise the host config; a CLOSED target is "
             "refused while auto_next_milestone is true (the next idle "
             "tick would advance it away)",
    )
    milestone_set_parser.add_argument(
        "title",
        help="the exact Milestone title, e.g. v0.6.0 (must be unique)",
    )
    notify.register_cli(subparsers, common)
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
                "doctor", "sync-engine-source", "milestone", "notify",
            ),
            allow_missing_pi_providers=args.command in (
                "setup", "doctor", "sync-engine-source", "milestone",
                "notify",
            ),
        )
        validate_config(config)
        # Every command path (doctor included) enforces the
        # same single-source-repo contract as runner.main, so a config
        # the Runner will reject is reported instead of all-green.
        # status is the read-only observer: it must cover every
        # configured repository, so it does not enforce the execution
        # fan-out contract (Issue #1656).
        if args.command != "status":
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
    elif args.command == "suggest":
        repo = args.repo or config.source_repos[0]
        if repo not in config.source_repos:
            parser.error(
                f"--repo must be one of: {', '.join(config.source_repos)}"
            )
        return suggest.run_suggest(
            config, repo, json_output=args.json, locale=args.locale,
            count=args.count, dispatch_issue=dispatch_issue,
        )
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
    elif args.command == "notify":
        return notify.test_command(config)
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
            return suggest.offer_setup_suggest(
                config, result, dispatch_issue=dispatch_issue,
            )
    elif args.command == "milestone":
        try:
            old, new, target = milestone_set(
                config, args.config, args.title,
            )
        except MilestoneSetError as exc:
            # One structured line with the actual reason and the repair
            # action; the written target is untouched on every failure
            # path.
            print(exc, file=sys.stderr)
            return 1
        repo = config.source_repos[0]
        print(f"active_milestone: {old} -> {new}")
        print(f"repo: {repo}")
        # Name WHERE the value landed: the repository policy overrides the
        # host config, so the two targets are not interchangeable
        # (Issue #1306).
        print(f"wrote: {target}")
        print(
            "next: the Runner's next tick claims Issues from Milestone "
            f'"{new}" and syncs the ORBI_ACTIVE_MILESTONE variable '
            f"(verify: gh api repos/{repo}/actions/variables/"
            "ORBI_ACTIVE_MILESTONE --jq .value)"
        )
    else:
        # status is the only remaining subcommand.
        try:
            scope = status_observe.parse_scope(
                config.source_repos, args.repo, args.issue,
            )
        except status_observe.StatusSelectorError as exc:
            print(f"status_failed reason={exc}", file=sys.stderr)
            return 1
        observer = status_observe.StatusObserver(
            config, scope,
            read_ready=ready_issue, read_result=recent_result,
            read_base=freeze_base,
        )

        def render() -> str:
            return status_report(config, scope, observer)

        def fail(exc: BaseException, last_view: str | None = None) -> None:
            print(
                f"status_failed scope={status_observe.scope_label(scope)} "
                f"reason={exc}",
                file=sys.stderr,
            )
            if last_view:
                print(last_view)

        # One failure line for the whole observation: the first read, a
        # render and the follow loop all end the same way with the scope
        # named and the last complete view kept. Ctrl-C ends cleanly
        # wherever it lands — the first read and a render are synchronous
        # too, so the handler covers them and not only the follow loop.
        try:
            observer.refresh()
            if not args.follow:
                print(render())
                return 0
            return status_observe.follow(observer, render, on_error=fail)
        except KeyboardInterrupt:
            return 0
        except status_observe.READ_ERRORS as exc:
            fail(exc)
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
