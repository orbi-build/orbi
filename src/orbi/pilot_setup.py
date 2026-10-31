"""One-time setup for Orbi.

`orbi setup` is the config-driven, idempotent, fail-fast
initialization entry for a new machine or new task-pool repository:

- verifies ``gh auth status`` and the viewer's read/write permission on
  every target repo;
- verifies the required commands (``git``, ``gh``, ``uv``, the ``orbi``
  CLI — a missing one fails fast with its
  actionable install guidance) and the platform scheduler's user
  session (systemd on Linux, launchd on macOS — Issue #849);
- aligns the platform labels (``ai-ready``, ``ai-in-progress``,
  ``ai-pr-opened``, ``ai-fix-needed``, ``ai-merged``, ``ai-blocked``,
  ``p0``, ``ai-epic``) declaratively from the repo-managed
  ``labels.toml`` — the
  single source of truth for label name, color and description. A
  missing label is created, a drifted label is updated, nothing is
  deleted, and no business label (``bug``, ``enhancement``, ...) is
  ever touched;
- installs the repo's scheduler units idempotently
  (systemd service/timer templates on Linux, launchd agent plists on
  macOS; copy, converge the instance schedules onto
  ``max_concurrency`` — never start/stop/restart
  the service) and
  reports each instance's enable/active state plus next trigger time;
- checks the local checkout read-only (remote, current branch, clean
  status, base freshness);
- checks the optional ``local-llm-kv-cache`` proxy health and reports it
  as a warning only — an optional component never blocks the core
  GitHub/Pilot setup.

Core failures raise :class:`SetupError` with the concrete reason before
any later mutation; there is no fallback path. The output is stable
``key=value`` lines (or an equivalent JSON document with ``--json``) so
agents and scripts can parse it.

Issue #1861 split the module by concern without changing a word of what
the operator sees: the config, label and provider scaffolding lives in
:mod:`orbi.setup_config`, the prerequisite checks and their sequence in
:mod:`orbi.setup_checks`. What stays here is the orchestration — the
mutating steps that need ``runner`` (Constitution Article 3.3), the
config-dependent ``orbi check`` steps and the result rendering
(``format_setup``, ``format_check_failure``, ``to_json``).
"""
from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

LOGGER = logging.getLogger("orbi.pilot_setup")

from orbi import runner, config as config_domain
from orbi import gitops
from orbi import cli_source
from orbi import git_transport
from orbi import scheduler
from orbi.journal import event, quote_value
from orbi.setup_checks import (
    DOCS_LINKS,
    CheckError,
    check_auth,
    check_commands,
    check_repo,
    run_machine_checks,
)
from orbi.setup_config import (
    LABELS_FILE,
    PROVIDER_ENV_NAME,
    PROVIDER_GUIDE,
    SetupError,
    align_labels,
    load_label_defs,
    model_provider_status,
    scaffold_model_config,
)

if TYPE_CHECKING:  # annotation-only: the callers import it at runtime
    from orbi.config import RunnerConfig


# Bumped whenever the setup output contract changes shape.
# 4 (Issue #849): the scheduler session check and its fields are
# platform-named (`systemd_session` / `launchd_session`, the `paths`
# key is the scheduler name), no longer systemd-only.
SETUP_VERSION = 4


# The optional local-llm-kv-cache proxy health endpoint (docs/
# optional-kv-cache.mdx: the committed user unit listens on 18082).
OPTIONAL_PROXY_URL = "http://127.0.0.1:18082/health"
OPTIONAL_PROXY_TIMEOUT = 3


def format_check_failure(exc: CheckError) -> str:
    """The one parseable stderr line for a failed gate."""
    return (
        f"check_failed check={exc.check} "
        f"reason={quote_value(exc.reason)} "
        f"fix={quote_value(exc.fix)} docs={exc.docs}"
    )


def install_cli_step(repo_dir: Path, module_file: Path, *,
                     run_command) -> dict:
    """Install or verify the editable uv tool install.

    The official local deployment is the EDITABLE tool install: the
    tool env imports the ``orbi`` package from the deployment
    checkout (the editable finder maps the whole ``src/orbi/``
    package directory), so the ``ExecStartPre`` checkout
    sync is picked up by the NEXT CLI process automatically (no
    per-version reinstall, no second copy of the source in
    site-packages). ``module_file`` is the RUNNING process's import
    source (``cli_source.module_file()`` in the real CLI): when it
    already sits inside ``repo_dir``'s package directory the editable
    install is verified WITHOUT any uv call (idempotent re-run);
    otherwise the exact editable force reinstall from ``repo_dir``
    runs via ``run_command`` (``uv tool install --force --reinstall
    --editable --python <interpreter> <repo_dir>``, the compatible
    selection of Issue #861). A failing
    install raises ``SetupError`` (fail fast, no fallback, no
    half-initialized state). The step NEVER touches a running Runner
    process — the new source is loaded by the next CLI start.
    """
    repo_dir = Path(repo_dir).resolve()
    actual = Path(module_file).resolve()
    if actual.is_relative_to(repo_dir / cli_source.PACKAGE_DIR):
        return {
            "action": "verified",
            "source": str(actual),
        }
    try:
        run_command(cli_source.reinstall_args(repo_dir))
    except Exception as exc:
        raise SetupError(
            f"editable tool install failed for {repo_dir}: {exc} "
            f"(fix: {cli_source.reinstall_command(repo_dir)})"
        ) from exc
    return {
        "action": "installed",
        "source": str(repo_dir / cli_source.PACKAGE_DIR),
    }


def install_units_step(repo_dir: Path, installed_dir: Path | None,
                       *, max_concurrency: int,
                       unit_name: str | None = None, run_command,
                       sched=None) -> dict:
    """Install the repo's user units and report their live state.

    Runs the scheduler layer's idempotent install (copy the repo
    templates, converge the instance schedules onto ``max_concurrency``
    — never start/stop/restart a live Runner), then reports each
    configured instance (@1..@max_concurrency, Issue #827)'s enabled
    state, active state and next trigger time (``-`` on launchd,
    which exposes no next-fire time).
    """
    sched = sched or scheduler.detect()
    try:
        result = scheduler.install_units(
            repo_dir, installed_dir, max_concurrency=max_concurrency,
            unit_name=unit_name, run_command=run_command, sched=sched,
        )
    except Exception as exc:
        raise SetupError(
            f"{sched.display} units install failed: {exc}"
        ) from exc
    try:
        instances = sched.instances_status(
            run_command, unit_name, max_concurrency=max_concurrency,
        )
    except subprocess.CalledProcessError as exc:
        raise SetupError(
            f"{sched.display} status query failed for the instances: {exc}"
        ) from exc
    service = result["units"][sched.unit_pairs(unit_name, 1)[0][1]]
    return {
        "service": {
            "installed": True,
            # str: the result document must stay JSON-serializable
            # (--json output contract).
            "installed_path": str(service["installed_path"]),
            "sha256": service["sha256"],
        },
        "timer": {
            "instances": instances,
        },
    }


def ensure_worktrees_ignored(repo_dir: Path, *, run_command) -> bool:
    """Keep Orbi-owned runtime dirs out of the main checkout status.

    ``.worktrees/`` holds Runner-created delivery worktrees, ``.orbi/``
    Orbi's runtime state (``base-sync.lock``, ``cli-install.json``,
    ``health.json``, ``slots/``). Untracked, either makes the checkout
    dirty and blocks every later ``orbi setup`` retry (Issue #1367).
    ``.orbi/`` is pinned whether or not it exists yet, so the Runner's
    later writes can never dirty the checkout; ``.worktrees/`` keeps its
    existence gate.
    """
    entries = [(".orbi/", "orbi_exclude_added")]
    if (Path(repo_dir) / ".worktrees").is_dir():
        entries.insert(0, (".worktrees/", "worktrees_exclude_added"))
    added = False
    for pattern, kind in entries:
        exclude = gitops.pin_git_exclude(
            repo_dir, pattern, run_command=run_command)
        if exclude is not None:
            event(kind, repo=repo_dir, path=exclude)
            added = True
    return added


def check_checkout(repo_dir: Path, base_branch: str,
                   source_repos: Sequence[str], *,
                   run_command, mode: str = "ssh") -> dict:
    """Local checkout check including the git transport.

    Reports the ``origin`` remote, its transport, the current branch
    and whether the local HEAD equals the freshly fetched
    ``origin/<base_branch>`` (base freshness — the same comparison
    the delivery gate uses). The git transport step (``git_transport``
    contract): the remote must match the CONFIGURED transport
    (``mode`` — orbi.toml ``git_transport``, default ``ssh``; Issue
    #580 adds ``https``: the origin stays on the HTTPS URL and the
    ``git ls-remote`` probe authenticates via the gh credential
    helper) for the first configured source repo (the worktrees share
    the checkout's single remote) and that transport must be
    reachable (``git ls-remote`` exits 0 — verified against the real
    CLI). The setup entry is the human-authorized migration path: an
    ``origin`` on the opposite transport is migrated with the plain
    ``git remote set-url origin <expected-url>`` (never a remote read
    from a comment or Issue, never a silent rewrite outside setup). A
    missing remote, an unreachable transport (no fallback), a dirty
    worktree (the timer's ``ExecStartPre`` fast-forward would refuse
    it) or any git error fails fast with the concrete reason.
    """
    try:
        transport = git_transport.check_transport(
            repo_dir, source_repos, run_command=run_command,
            migrate=True, mode=mode,
        )
        branch = run_command(
            ["git", "branch", "--show-current"], cwd=repo_dir,
        )
        worktrees_exclude_added = ensure_worktrees_ignored(
            repo_dir, run_command=run_command,
        )
        dirty = run_command(
            ["git", "status", "--porcelain"], cwd=repo_dir,
        )
        if dirty:
            raise SetupError(
                f"checkout is not clean: {repo_dir} (uncommitted "
                f"changes: {dirty.strip()!r}) — commit or stash them "
                "first: the timer's ExecStartPre fast-forward refuses "
                "a dirty worktree, so the Runner could never start"
            )
        # The checkout check's fetch updates the shared
        # remote-tracking ref, so it runs under the SAME base-sync
        # lock the Runner and the Pi prompt-side fetches use (no
        # unlocked fetch path exists).
        runner.fetch_base_ref(
            repo_dir, base_branch, command_runner=run_command,
        )
        try:
            head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        except subprocess.CalledProcessError as exc:
            raise SetupError(
                f"{repo_dir} must be a git checkout of {source_repos[0]} "
                "with a resolvable HEAD — clone it there: "
                f"{git_transport.clone_command(source_repos[0], repo_dir, mode=mode)}"
            ) from exc
        base = run_command(
            ["git", "rev-parse", f"origin/{base_branch}"], cwd=repo_dir,
        )
    except SetupError:
        raise
    except git_transport.TransportError as exc:
        raise SetupError(
            f"checkout check failed for {repo_dir}: {exc}"
        ) from exc
    except Exception as exc:
        raise SetupError(
            f"checkout check failed for {repo_dir}: {exc}"
        ) from exc
    return {
        "remote": transport["remote"],
        "branch": branch,
        "clean": True,
        "base_fresh": head == base,
        "remote_url": transport["url"],
        "remote_protocol": transport["protocol"],
        "migrated": transport["migrated"],
        "ssh_reachable": transport["ssh_reachable"],
        "transport_reachable": transport["transport_reachable"],
        **({"worktrees_exclude_added": True} if worktrees_exclude_added else {}),
    }


def check_optional_proxy(run_command) -> dict:
    """Health-check the optional local-llm-kv-cache proxy (never raises).

    The proxy (docs/optional-kv-cache.mdx) is an optional enhancement:
    its absence or unhealthiness is reported as a warning and never
    blocks the core GitHub/Pilot setup. ``healthy`` on a 2xx health
    response, ``unhealthy`` on a command/HTTP failure, ``unavailable``
    when curl itself is missing.
    """
    try:
        run_command(
            [
                "curl", "-fsS", "-m", str(OPTIONAL_PROXY_TIMEOUT),
                OPTIONAL_PROXY_URL,
            ],
            failure_log_level=logging.INFO,
        )
        status = "healthy"
    except FileNotFoundError:
        status = "unavailable"
    except Exception:
        status = "unhealthy"
    return {
        "optional": True,
        "proxy": status,
        "url": OPTIONAL_PROXY_URL,
    }


def run_checks(config_path: Path, *, run_command) -> list[str]:
    """The `orbi check` gate: every prerequisite, fail fast.

    Read-only: no label, no unit, no git mutation, and NO config
    creation (`orbi setup` owns that) — a missing or invalid orbi.toml
    is a ``config`` finding with the repair action, not a traceback.
    Order: the machine-level checks that need no config first (python,
    required commands, the scheduler user session, gh auth, pi), then
    the config
    (existence, parse, validation), then the config-dependent probes
    (per-source-repo access + permission, git transport, model
    provider — the status is value-free, never a secret). The first
    failure raises :class:`CheckError`; success returns one
    ``check=<step> ok ...`` line per step plus the final
    ``prerequisites=ok checks=<n>``.
    """
    lines, _ = run_machine_checks(run_command=run_command)

    config_path = Path(config_path)
    if not config_path.is_file():
        raise CheckError(
            "config",
            f"config file not found: {config_path}",
            "run `orbi setup` (creates orbi.toml from the shipped "
            "example when absent) or point --config at it",
            DOCS_LINKS["config"],
        )
    try:
        config = config_domain.load_config(
            config_path,
            # The provider key gate would stop the whole gate on a
            # missing key; the provider step below reports the state
            # value-free instead (the doctor's lenient flags).
            check_provider_api_keys=False,
            allow_missing_pi_providers=True,
        )
        runner.validate_config(config)
        runner.validate_execution_source_repos(config.source_repos)
    except FileNotFoundError as exc:
        raise CheckError(
            "config",
            f"invalid orbi.toml: required path missing: {exc}",
            "create the missing path or fix orbi.toml (a repo_dir must "
            "be a clone of the first source repo — the reason names the "
            "exact git clone command)",
            DOCS_LINKS["config"],
        ) from exc
    except ValueError as exc:
        raise CheckError(
            "config",
            f"invalid orbi.toml: {exc}",
            "fix the reported problem in orbi.toml",
            DOCS_LINKS["config"],
        ) from exc
    lines.append(f"check=config ok path={quote_value(str(config_path))}")

    for repo in config.source_repos:
        try:
            check_repo(repo, run_command)
        except SetupError as exc:
            raise CheckError(
                "repo_access", str(exc),
                "verify the repository name and your write permission "
                "on it (label management needs WRITE or more)",
                DOCS_LINKS["gh"],
            ) from exc
        lines.append(f"check=repo ok repo={repo}")
    try:
        transport = git_transport.check_transport(
            config.repo_dir, config.source_repos,
            run_command=run_command, migrate=False,
            mode=config.git_transport,
        )
    except git_transport.TransportError as exc:
        raise CheckError(
            "transport", str(exc),
            "repair the origin remote (the human-run migration entry "
            "is `orbi setup`)",
            DOCS_LINKS["ssh"],
        ) from exc
    lines.append(
        f"check=transport ok protocol={transport['protocol']} "
        f"url={quote_value(transport['url'])}"
    )
    provider = model_provider_status(config)
    if provider["state"] != "ok":
        variable = provider.get("env_variable", PROVIDER_ENV_NAME)
        raise CheckError(
            "model_provider",
            f"model provider not configured: edit orbi.toml "
            f"(pi_providers/pi_provider/pi_model), fill "
            f"{provider.get('provider_file', '-')} and put the key in "
            f"{provider['env_file']} ({variable})",
            "configure the provider and the key (values are never "
            "printed)",
            DOCS_LINKS["provider"],
        )
    lines.append(
        f"check=model_provider ok provider={provider['provider']} "
        f"model={provider['model']} key={provider['key']}"
    )
    lines.append(f"prerequisites=ok checks={len(lines)}")
    return lines


def run_setup(config: RunnerConfig, installed_dir: Path | None, *,
              repos: list[str] | None = None,
              run_command) -> dict:
    """Run the full one-time setup and return its result document.

    Order (fail fast, no mutation before the prerequisites pass):
    commands -> CLI editable install (verify or reinstall, Issue
    #152) -> auth -> per target repo (permission check, then label
    alignment) -> unit install -> read-only checkout check -> model
    provider starter scaffold -> optional proxy health (warning
    only). ``repos`` overrides the target set
    (the ``--repo`` flag); it must be a non-empty subset of the
    configured ``source_repos``.
    """
    if repos is None:
        targets = list(config.source_repos)
    else:
        if not repos:
            raise SetupError("--repo requires a non-empty repo list")
        configured = list(config.source_repos)
        for repo in repos:
            if repo not in configured:
                raise SetupError(
                    f"--repo must be one of: {', '.join(configured)}"
                )
        targets = list(repos)
    repo_dir = config.repo_dir
    # Labels.toml, the CLI editable install and the unit
    # templates live in the deployment home; the delivery checkout
    # (repo_dir) may be a foreign repo without any of them.
    deploy_home = config.deploy_home
    defs = load_label_defs(deploy_home / LABELS_FILE)
    check_commands(run_command, config.unit_name)
    # The CLI source step precedes every mutating step — the
    # running CLI must import from the deployment checkout, otherwise
    # the unit migration below (and the pre-start self-heal it
    # repairs) could never run the new code (the #152 deadlock).
    cli = install_cli_step(
        deploy_home, cli_source.module_file(), run_command=run_command,
    )
    check_auth(run_command)
    repo_results = []
    for repo in targets:
        info = check_repo(repo, run_command)
        labels = align_labels(repo, defs, run_command)
        repo_results.append({
            **info,
            "labels": {"aligned": labels["aligned"], "total": labels["total"]},
        })
    unit_kwargs = {
        "max_concurrency": config.max_concurrency,
        "run_command": run_command,
    }
    if config.unit_name is not None:
        unit_kwargs["unit_name"] = config.unit_name
    units = install_units_step(deploy_home, installed_dir, **unit_kwargs)
    checkout = check_checkout(
        repo_dir, config.base_branch, config.source_repos,
        run_command=run_command, mode=config.git_transport,
    )
    optional_proxy = check_optional_proxy(run_command)
    scaffold = scaffold_model_config(config)
    return {
        "setup": "ok",
        "version": SETUP_VERSION,
        "base_branch": config.base_branch,
        "unit_name": config.unit_name,
        "repos": repo_results,
        "cli": cli,
        "service": units["service"],
        "timer": units["timer"],
        "checkout": checkout,
        "optional_proxy": optional_proxy,
        "model_provider": {
            **model_provider_status(config),
            **scaffold,
        },
    }


def format_setup(result: dict) -> list[str]:
    """Render the result document as stable key=value lines.

    One line per concern; values containing spaces are quoted (the
    ``journal.quote_value`` convention) so the output stays
    parseable.
    """
    lines = [
        (
            f"setup={result['setup']} version={result['version']} "
            f"base_branch={result['base_branch']}"
        ),
        # The editable install step result (verified = the
        # running CLI already imports from the checkout; installed =
        # the editable force reinstall ran).
        (
            f"cli={result['cli']['action']} "
            f"source={quote_value(result['cli']['source'])}"
        ),
    ]
    for entry in result["repos"]:
        lines.append(
            f"repo={entry['repo']} permission={entry['permission']} "
            f"default_branch={entry['default_branch']} "
            f"labels={entry['labels']['aligned']}/{entry['labels']['total']}"
        )
    service = result["service"]
    lines.append(
        f"service=installed path={quote_value(str(service['installed_path']))} "
        f"sha256={service['sha256']}"
    )
    timer = result["timer"]
    for instance, entry in timer["instances"].items():
        lines.append(
            f"timer={instance} "
            f"{'enabled' if entry['enabled'] else 'disabled'} "
            f"active={'true' if entry['active'] else 'false'} "
            f"next={quote_value(entry['next'])}"
        )
    checkout = result["checkout"]
    reachable = checkout["ssh_reachable"]
    reachable_text = "-" if reachable is None else (
        "true" if reachable else "false"
    )
    transport = checkout["transport_reachable"]
    transport_text = "-" if transport is None else (
        "true" if transport else "false"
    )
    lines.append(
        f"checkout=remote={checkout['remote']} "
        f"branch={quote_value(checkout['branch'])} "
        f"clean={'true' if checkout['clean'] else 'false'} "
        f"base_fresh={'true' if checkout['base_fresh'] else 'false'} "
        f"remote_url={checkout['remote_url']} "
        f"protocol={checkout['remote_protocol']} "
        f"migrated={'true' if checkout['migrated'] else 'false'} "
        f"ssh_reachable={reachable_text} "
        f"transport_reachable={transport_text}"
        + (" worktrees_exclude_added=true"
           if checkout.get("worktrees_exclude_added") else "")
    )
    provider = result.get("model_provider")
    if provider and provider["state"] == "ok":
        lines.append(
            f"model_provider=ok provider={provider['provider']} "
            f"model={provider['model']} key={provider['key']}"
        )
    elif provider:
        lines.append(
            "model_provider=NOT CONFIGURED — edit orbi.toml "
            "(pi_providers/pi_provider/pi_model), fill "
            f"{provider['provider_file']} and put the key in "
            f"{provider['env_file']} ({provider['env_variable']})"
        )
        lines.append(f"model_provider_guide={PROVIDER_GUIDE}")
    proxy = result["optional_proxy"]
    lines.append(
        f"model_endpoint=optional optional_proxy={proxy['proxy']} "
        f"url={proxy['url']}"
    )
    return lines


def to_json(result: dict) -> str:
    """The result document as JSON (equivalent to the key=value lines)."""
    return json.dumps(result, indent=2, ensure_ascii=False)
