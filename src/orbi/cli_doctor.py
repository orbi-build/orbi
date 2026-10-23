"""The read-only `orbi doctor` report (Issue #1862).

Extracted verbatim from `cli.py`: one report over the deployment home
(the repo commit, the engine-source channel, the transport, unit drift,
slots, the live Pi session, the current Issue per source repo and the
recent journal) plus the dirty-checkout read it shares with the
scheduler preflight. Read-only: a failed check is REPORTED with its
structured reason and nothing is mutated or repaired.
"""
from __future__ import annotations

from pathlib import Path

from orbi import (
    cli_source,
    cli_status,
    engine_source,
    git_transport,
    notify,
    pilot_setup,
    scheduler,
)
from orbi import config as config_domain
from orbi.github import merge_gate_preflight
from orbi.runner import run_command
from orbi.session_command import find_session_file
from orbi.source_pool import source_pool_lines

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
    lines.extend(cli_status.slot_lines(
        config.slot_dir, config.max_concurrency,
    ))
    session = find_session_file(repo_dir)
    lines.append(f"pi: {session if session else 'none'}")
    for repo in config.source_repos:
        lines.append(f"source: {repo}")
        lines.extend(f"  {line}" for line in merge_gate_preflight(
            repo, config.base_branch,
        ))
        current = cli_status.current_issue(repo)
        lines.append(
            f"  current: {cli_status.format_issue(current) if current else '-'}"
        )
    journal = "\n".join(sched.journal_lines(
        run_command, config.deploy_home, config.unit_name,
        max_concurrency=config.max_concurrency, lines=JOURNAL_LINES,
    ))
    lines.append("journal:")
    for line in journal.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)
