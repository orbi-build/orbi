"""One handler per `orbi` subcommand (Issue #1862).

Extracted from `cli.py`: `main` parses, resolves the config path and
hands the parsed namespace to the handler that owns the command, each
returning an exit code. The behavior — stdout, stderr, exit codes and the
`runner.main` call of the bare tick — is unchanged; `tests/test_cli_golden.py`
pins the help/version surface byte for byte.

The handlers are the command layer only: config path resolution and
loading stay in the entry module, which owns the diagnostics.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from orbi import (
    cli_status,
    config as config_domain,
    engine_source,
    notify,
    pilot_setup,
    scheduler,
    status_observe,
    suggest,
)
from orbi.cli_doctor import doctor_report
from orbi.delivery_labels import READY_LABEL
from orbi.milestone_command import MilestoneSetError, milestone_set
from orbi.runner import freeze_base, run_command
from orbi.session_command import (
    find_session_file,
    follow_session_file,
    format_session_line,
)

LOGGER = logging.getLogger("orbi.cli")

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

# The read-only commands may resolve a missing default config from the
# installed units; a mutating command never does.
READ_ONLY_COMMANDS = frozenset({"status", "doctor", "session", "check"})

# A missing provider key must never block these deployment maintenance
# operations (the Runner's own start enforces the key), so they load the
# config with the doctor's lenient provider flags.
LENIENT_PROVIDER_COMMANDS = frozenset(
    {"setup", "doctor", "sync-engine-source", "milestone", "notify"},
)
NO_PROVIDER_KEY_CHECK_COMMANDS = frozenset(
    {"doctor", "sync-engine-source", "milestone", "notify"},
)


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


def _selected_repo(config: config_domain.RunnerConfig, args,
                   parser) -> str:
    """The explicit --repo when given, else the first configured source."""
    repo = args.repo or config.source_repos[0]
    if repo not in config.source_repos:
        parser.error(
            f"--repo must be one of: {', '.join(config.source_repos)}"
        )
    return repo


def command_check(args) -> int:
    """The prerequisite gate — it owns its config handling.

    A missing or invalid orbi.toml is a `config` finding with the repair
    action, never a traceback. Read-only: no config is created here
    (`orbi setup` owns that).
    """
    try:
        lines = pilot_setup.run_checks(
            args.config, run_command=run_command,
        )
    except pilot_setup.CheckError as exc:
        print(pilot_setup.format_check_failure(exc), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def command_add(config: config_domain.RunnerConfig, args, parser) -> int:
    """Create an Issue in a source repo and label it `ai-ready`."""
    repo = _selected_repo(config, args, parser)
    LOGGER.info("dispatch repo=%s title=%s", repo, args.title)
    url = dispatch_issue(repo, args.title, args.body)
    print(f"created: {url}")
    print(f"label: {READY_LABEL}")
    return 0


def command_suggest(config: config_domain.RunnerConfig, args, parser) -> int:
    repo = _selected_repo(config, args, parser)
    return suggest.run_suggest(
        config, repo, json_output=args.json, locale=args.locale,
        count=args.count, dispatch_issue=dispatch_issue,
    )


def command_session(config: config_domain.RunnerConfig, args, parser) -> int:
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
    return 0


def command_install_units(config: config_domain.RunnerConfig, args,
                          parser) -> int:
    print(install_units_command(config, args.installed_dir))
    return 0


def command_sync_engine_source(config: config_domain.RunnerConfig, args,
                               parser) -> int:
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
    return 0


def command_doctor(config: config_domain.RunnerConfig, args, parser) -> int:
    print(doctor_report(config, args.installed_dir))
    return 0


def command_notify(config: config_domain.RunnerConfig, args, parser) -> int:
    return notify.test_command(config)


def command_setup(config: config_domain.RunnerConfig, args, parser) -> int:
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
        return 0
    print("\n".join(pilot_setup.format_setup(result)))
    return suggest.offer_setup_suggest(
        config, result, dispatch_issue=dispatch_issue,
    )


def command_milestone(config: config_domain.RunnerConfig, args,
                      parser) -> int:
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
    return 0


def command_status(config: config_domain.RunnerConfig, args, parser) -> int:
    try:
        scope = status_observe.parse_scope(
            config.source_repos, args.repo, args.issue,
        )
    except status_observe.StatusSelectorError as exc:
        print(f"status_failed reason={exc}", file=sys.stderr)
        return 1
    observer = status_observe.StatusObserver(
        config, scope,
        read_ready=lambda repo: cli_status.ready_issue(repo, config),
        read_result=cli_status.recent_result,
        read_base=freeze_base,
    )

    def render() -> str:
        return cli_status.status_report(config, scope, observer)

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


# The dispatch table: one handler per subcommand, all returning an exit
# code. Two commands are not in it: the bare tick (no subcommand) is the
# Runner entry, handed to `runner.main` by the entry module, and `check`
# runs `command_check` before any config is loaded (it owns its own
# prerequisite gate).
COMMAND_HANDLERS = {
    "add": command_add,
    "suggest": command_suggest,
    "status": command_status,
    "session": command_session,
    "install-units": command_install_units,
    "sync-engine-source": command_sync_engine_source,
    "doctor": command_doctor,
    "notify": command_notify,
    "setup": command_setup,
    "milestone": command_milestone,
}
