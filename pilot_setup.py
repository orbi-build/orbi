"""One-time setup for Muyan Pilot (Issue #117).

`muyan_pilot.py setup` is the config-driven, idempotent, fail-fast
initialization entry for a new machine or new task-pool repository:

- verifies ``gh auth status`` and the viewer's read/write permission on
  every target repo;
- verifies the required commands (``git``, ``gh``, ``python3``, ``uv``,
  the ``muyan-pilot`` CLI — a missing one fails fast with its
  actionable install guidance) and the ``systemctl --user`` user bus;
- aligns the platform labels (``ai-ready``, ``ai-in-progress``,
  ``ai-pr-opened``, ``ai-fix-needed``, ``ai-merged``, ``ai-blocked``,
  ``p0``, ``ai-epic``) declaratively from the repo-managed
  ``labels.toml`` — the
  single source of truth for label name, color and description. A
  missing label is created, a drifted label is updated, nothing is
  deleted, and no business label (``bug``, ``enhancement``, ...) is
  ever touched;
- installs the repo's user systemd service/timer templates idempotently
  (reusing ``systemd_deploy.install_units``: copy, daemon-reload,
  enable the two timer instances ``muyan-pilot@1.timer`` /
  ``muyan-pilot@2.timer`` — never start/stop/restart the service) and
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
"""
from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import cli_source
import git_transport
import systemd_deploy
from pi_activity import quote_value

# Bumped whenever the setup output contract changes shape.
# Issue #152 added the `cli=` line (the editable install step).
SETUP_VERSION = 2

# The repo-managed single source of truth for the platform labels.
LABELS_FILE = "labels.toml"
REQUIRED_LABELS = (
    "ai-ready",
    "ai-in-progress",
    "ai-pr-opened",
    "ai-fix-needed",
    "ai-merged",
    "ai-blocked",
    "p0",
    # Epic marker (Issue #93): the claim scan skips `ai-epic` Issues
    # (`epic_not_claimed`), so the label is platform state the setup
    # entry must guarantee — same as every delivery-state label.
    "ai-epic",
)
COLOR_PATTERN = re.compile(r"^[0-9a-fA-F]{6}$")

# Permissions that may create/edit labels (GitHub viewerPermission).
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})

# Required local commands (checked with shutil.which). Issue #140:
# the installed `muyan-pilot` CLI is a prerequisite too — the systemd
# entry it documents (and the service's ExecStart) must exist.
# Issue #156: `uv` is a prerequisite too — the CLI editable step
# (Issue #152) calls `uv tool install`, so a machine that has the CLI
# but no uv must fail HERE (with actionable install guidance), not
# mid-install with an indirect error.
REQUIRED_COMMANDS = ("git", "gh", "python3", "uv", "muyan-pilot")

# Actionable install guidance per required command (Issue #156): the
# missing-command error must tell the user how to install the
# prerequisite. The uv entry is the official installer command, verified
# against https://docs.astral.sh/uv/getting-started/installation/.
COMMAND_INSTALL_HINTS = {
    "git": (
        "install git (e.g. `apt-get install git` / `dnf install git` / "
        "`brew install git`)"
    ),
    "gh": (
        "install the GitHub CLI (https://cli.github.com/) and log in "
        "with `gh auth login` (setup verifies the login, it never runs "
        "it for you)"
    ),
    "python3": (
        "install Python 3.14 (the production minor version pinned by CI)"
    ),
    "uv": (
        "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh "
        "(https://docs.astral.sh/uv/getting-started/installation/)"
    ),
    "muyan-pilot": (
        "install the editable uv tool CLI from the deployment checkout: "
        "uv tool install --force --reinstall --editable --python "
        "/usr/bin/python3 <repo_dir>"
    ),
}

# The optional local-llm-kv-cache proxy health endpoint (docs/
# optional-kv-cache.mdx: the committed user unit listens on 18082).
OPTIONAL_PROXY_URL = "http://127.0.0.1:18082/health"
OPTIONAL_PROXY_TIMEOUT = 3


class SetupError(RuntimeError):
    """A core setup prerequisite or step failed (fail fast)."""


def load_label_defs(path: Path) -> list[dict]:
    """Parse and validate the repo-managed label definitions.

    The file must define EXACTLY the 8 platform labels (no more, no
    less, no duplicates), each with a non-empty name, a 6-hex color and
    a non-empty description. Any deviation is a fail-fast SetupError:
    a typo'd or missing label would silently skip a delivery state.
    """
    path = Path(path)
    if not path.is_file():
        raise SetupError(
            f"label definitions missing: {path} (the repo-managed "
            "single source of truth for the platform labels)"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"malformed {LABELS_FILE}: {path}: {exc}") from exc
    entries = data.get("label")
    if not isinstance(entries, list):
        raise SetupError(
            f"malformed {LABELS_FILE}: {path}: expected a [[label]] "
            "array of name/color/description entries"
        )
    defs: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SetupError(
                f"malformed {LABELS_FILE}: {path}: each label entry "
                "must be a table with name/color/description"
            )
        name = entry.get("name")
        color = entry.get("color")
        description = entry.get("description")
        if not isinstance(name, str) or not name:
            raise SetupError(
                f"malformed {LABELS_FILE}: {path}: a label entry has "
                "no non-empty name"
            )
        if name in seen:
            raise SetupError(
                f"malformed {LABELS_FILE}: {path}: duplicate label "
                f"name: {name}"
            )
        seen.add(name)
        if not isinstance(color, str) or not COLOR_PATTERN.fullmatch(color):
            raise SetupError(
                f"malformed {LABELS_FILE}: {path}: label {name} has an "
                "invalid color (6-character hex required): "
                f"{color!r}"
            )
        if not isinstance(description, str) or not description:
            raise SetupError(
                f"malformed {LABELS_FILE}: {path}: label {name} has no "
                "non-empty description"
            )
        defs.append({
            "name": name,
            "color": color,
            "description": description,
        })
    missing = [name for name in REQUIRED_LABELS if name not in seen]
    unknown = [name for name in seen if name not in REQUIRED_LABELS]
    if missing or unknown:
        raise SetupError(
            f"malformed {LABELS_FILE}: {path}: must define exactly the "
            f"platform labels {list(REQUIRED_LABELS)}; "
            f"missing={missing} unknown={unknown}"
        )
    return defs


def install_cli_step(repo_dir: Path, module_file: Path, *,
                     run_command) -> dict:
    """Install or verify the editable uv tool install (Issue #152).

    The official local deployment is the EDITABLE tool install: the
    tool env imports ``muyan_pilot`` from the deployment checkout, so
    the ``ExecStartPre`` checkout sync is picked up by the NEXT CLI
    process automatically (no per-version reinstall, no second copy
    of the source in site-packages). ``module_file`` is the RUNNING
    process's import source (``cli_source.module_file()`` in the real
    CLI): when it already sits directly inside ``repo_dir`` the
    editable install is verified WITHOUT any uv call (idempotent
    re-run); otherwise the exact editable force reinstall from
    ``repo_dir`` runs via ``run_command`` (``uv tool install --force
    --reinstall --editable --python /usr/bin/python3 <repo_dir>``).
    A failing install raises ``SetupError`` (fail fast, no fallback,
    no half-initialized state). The step NEVER touches a running
    Runner process — the new source is loaded by the next CLI start.
    """
    repo_dir = Path(repo_dir).resolve()
    actual = Path(module_file).resolve()
    if actual.parent == repo_dir:
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
        "source": str(repo_dir / "muyan_pilot.py"),
    }


def check_commands(run_command) -> dict:
    """Verify the required commands and the systemctl --user bus.

    ``git``, ``gh``, ``python3``, ``uv`` and the installed
    ``muyan-pilot`` CLI must be on the PATH (Issue #156: ``uv`` is
    checked explicitly because the CLI editable step calls
    ``uv tool install``); a missing command fails fast with the
    actionable install guidance for that command (``COMMAND_INSTALL_
    HINTS``). The systemd user bus must be reachable (a probe
    ``systemctl --user show`` must succeed — a container or a headless
    session without a user bus fails fast with the concrete reason).
    No mutation happens here.
    """
    paths: dict[str, str] = {}
    for name in REQUIRED_COMMANDS:
        path = shutil.which(name)
        if path is None:
            raise SetupError(
                f"required command missing: {name} (not on PATH) — "
                f"{COMMAND_INSTALL_HINTS[name]}"
            )
        paths[name] = path
    # Probe an INSTANCE name (verified against the real CLI: `systemctl
    # show` rejects the bare template name `muyan-pilot@.timer` but
    # accepts instance names, exiting 0 with `not-found` before the
    # units are installed — the probe only needs the user bus).
    probe = [
        "systemctl", "--user", "show", "-p", "LoadState", "--value",
        systemd_deploy.TIMER_INSTANCES[0],
    ]
    try:
        run_command(probe)
    except Exception as exc:
        detail = str(exc)
        raise SetupError(
            "systemctl --user user bus unavailable (is a systemd user "
            f"session running?): {detail}"
        ) from exc
    paths["systemctl"] = "user-bus-ok"
    return paths


def check_auth(run_command) -> None:
    """Verify ``gh auth status`` (logged in with a usable token)."""
    try:
        run_command(["gh", "auth", "status"])
    except Exception as exc:
        raise SetupError(
            f"gh auth status failed (log in with `gh auth login`): {exc}"
        ) from exc


def check_repo(repo: str, run_command) -> dict:
    """Verify the target repo exists and the viewer may write to it.

    ``gh repo view`` (verified against the real CLI: unknown repo exits
    non-zero, a readable repo returns ``nameWithOwner``,
    ``viewerPermission`` and ``defaultBranchRef``). The viewer
    permission must allow label mutation (WRITE/MAINTAIN/ADMIN).
    """
    try:
        raw = run_command([
            "gh", "repo", "view", repo,
            "--json", "nameWithOwner,viewerPermission,defaultBranchRef",
        ])
    except Exception as exc:
        raise SetupError(
            f"repo not accessible: {repo} (gh repo view failed: {exc})"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"cannot parse gh repo view output for {repo}: {raw!r}"
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(
            f"cannot parse gh repo view output for {repo}: {raw!r}"
        )
    permission = data.get("viewerPermission")
    if permission not in WRITE_PERMISSIONS:
        raise SetupError(
            f"insufficient permission for {repo}: viewerPermission="
            f"{permission!r} (one of {sorted(WRITE_PERMISSIONS)} "
            "required to manage labels)"
        )
    name_with_owner = data.get("nameWithOwner")
    if not isinstance(name_with_owner, str) or not name_with_owner:
        raise SetupError(
            f"cannot parse gh repo view output for {repo}: "
            "nameWithOwner missing"
        )
    default_branch = (data.get("defaultBranchRef") or {}).get("name")
    if not isinstance(default_branch, str) or not default_branch:
        raise SetupError(
            f"cannot parse gh repo view output for {repo}: "
            "defaultBranchRef.name missing"
        )
    return {
        "repo": name_with_owner,
        "permission": permission,
        "default_branch": default_branch,
    }


def align_labels(repo: str, defs: list[dict], run_command) -> dict:
    """Declaratively align the repo's platform labels with `defs`.

    Reads the current labels (``gh label list --json name,color,
    description``) and, for every definition, creates the label when
    missing or updates color/description when drifted (``gh label
    create NAME --color C --description D --force`` — verified against
    the CLI help: with ``--force`` the command updates an existing
    label instead of failing). Existing labels whose name, color and
    description already match are left untouched, and labels outside
    the platform set (business labels) are never read-modified or
    deleted. Returns ``{"repo", "aligned", "total"}``.
    """
    raw = run_command([
        "gh", "label", "list", "--repo", repo,
        "--json", "name,color,description", "--limit", "100",
    ])
    try:
        existing = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"cannot parse gh label list output for {repo}: {raw!r}"
        ) from exc
    if not isinstance(existing, list):
        raise SetupError(
            f"cannot parse gh label list output for {repo}: {raw!r}"
        )
    by_name = {
        entry.get("name"): entry
        for entry in existing
        if isinstance(entry, dict)
    }
    aligned = 0
    for entry in defs:
        current = by_name.get(entry["name"])
        matches = (
            isinstance(current, dict)
            and (current.get("color") or "").lower() == entry["color"].lower()
            and current.get("description") == entry["description"]
        )
        if matches:
            aligned += 1
            continue
        try:
            run_command([
                "gh", "label", "create", entry["name"],
                "--repo", repo,
                "--color", entry["color"],
                "--description", entry["description"],
                "--force",
            ])
        except Exception as exc:
            raise SetupError(
                f"label alignment failed for {repo}: {entry['name']} "
                f"(gh label create failed: {exc})"
            ) from exc
        aligned += 1
    return {"repo": repo, "aligned": aligned, "total": len(defs)}


def timer_next_trigger(list_timers_output: str, unit_name: str) -> str:
    """The NEXT column of the given timer instance's row, or ``-``.

    ``systemctl --user list-timers --no-pager`` prints a header line
    (``NEXT  LEFT ...``) followed by one row per timer; the row whose
    UNIT column is the given instance (e.g. ``muyan-pilot@1.timer``)
    carries the next trigger time.
    """
    for line in list_timers_output.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[-2] == unit_name:
            # NEXT is the first fixed-width column and itself contains
            # spaces ("Thu 2026-08-27 10:00:00 +08"): it ends where the
            # all-whitespace column separator begins, so take the line
            # up to the first run of two or more spaces.
            match = re.match(r"^(\S+(?: \S+)*?)  ", line)
            if match:
                return match.group(1)
            return columns[0]
    return "-"


def install_units_step(repo_dir: Path, installed_dir: Path | None,
                       *, run_command) -> dict:
    """Install the repo's user units and report their live state.

    Reuses the idempotent ``systemd_deploy.install_units`` (copy the
    repo templates, migrate the pre-#149 non-templated units away,
    ``daemon-reload``, enable the two timer instances — never
    start/stop/restart the service), then reports EACH timer
    instance's enabled state (``systemctl --user is-enabled``),
    active state (``show -p ActiveState``) and next trigger time
    (``list-timers``).
    """
    try:
        result = systemd_deploy.install_units(
            repo_dir, installed_dir, run_command=run_command,
        )
    except Exception as exc:
        raise SetupError(
            f"systemd units install failed: {exc}"
        ) from exc
    list_timers = run_command([
        "systemctl", "--user", "list-timers", "--no-pager",
    ])
    instances = {}
    for instance in systemd_deploy.TIMER_INSTANCES:
        instances[instance] = {
            "enabled": run_command([
                "systemctl", "--user", "is-enabled", instance,
            ]) == "enabled",
            "active": run_command([
                "systemctl", "--user", "show", "-p", "ActiveState",
                "--value", instance,
            ]) == "active",
            "next": timer_next_trigger(list_timers, instance),
        }
    service = result["units"][systemd_deploy.SERVICE_UNIT]
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


def check_checkout(repo_dir: Path, base_branch: str,
                   source_repos: list[str], *,
                   run_command) -> dict:
    """Local checkout check including the git transport (Issue #114).

    Reports the ``origin`` remote, its transport, the current branch
    and whether the local HEAD equals the freshly fetched
    ``origin/<base_branch>`` (base freshness — the same comparison
    the delivery gate uses). The git transport step (``git_transport``
    contract): the remote must be SSH for the first configured source
    repo (the worktrees share the checkout's single remote) and SSH
    must be reachable (``git ls-remote`` exits 0 — verified against
    the real CLI). The setup entry is the human-authorized migration
    path: an existing HTTPS ``origin`` is migrated with the plain
    ``git remote set-url origin <ssh-url>`` (never a remote read from
    a comment or Issue, never a silent rewrite outside setup). A
    missing remote, an unreachable SSH (no HTTPS fallback), a dirty
    worktree (the timer's ``ExecStartPre`` fast-forward would refuse
    it) or any git error fails fast with the concrete reason.
    """
    try:
        transport = git_transport.check_transport(
            repo_dir, source_repos, run_command=run_command,
            migrate=True,
        )
        branch = run_command(
            ["git", "branch", "--show-current"], cwd=repo_dir,
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
        run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir)
        head = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
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
        run_command([
            "curl", "-fsS", "-m", str(OPTIONAL_PROXY_TIMEOUT),
            OPTIONAL_PROXY_URL,
        ])
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


def run_setup(config: dict, installed_dir: Path | None, *,
              repos: list[str] | None = None,
              run_command) -> dict:
    """Run the full one-time setup and return its result document.

    Order (fail fast, no mutation before the prerequisites pass):
    commands -> CLI editable install (verify or reinstall, Issue
    #152) -> auth -> per target repo (permission check, then label
    alignment) -> unit install -> read-only checkout check -> optional
    proxy health (warning only). ``repos`` overrides the target set
    (the ``--repo`` flag); it must be a non-empty subset of the
    configured ``source_repos``.
    """
    if repos is None:
        targets = list(config["source_repos"])
    else:
        if not repos:
            raise SetupError("--repo requires a non-empty repo list")
        configured = list(config["source_repos"])
        for repo in repos:
            if repo not in configured:
                raise SetupError(
                    f"--repo must be one of: {', '.join(configured)}"
                )
        targets = list(repos)
    repo_dir = config["repo_dir"]
    defs = load_label_defs(repo_dir / LABELS_FILE)
    check_commands(run_command)
    # Issue #152: the CLI source step precedes every other step — the
    # running CLI must import from the deployment checkout, otherwise
    # the unit migration below (and the pre-start self-heal it
    # repairs) could never run the new code (the #152 deadlock).
    cli = install_cli_step(
        repo_dir, cli_source.module_file(), run_command=run_command,
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
    units = install_units_step(repo_dir, installed_dir, run_command=run_command)
    checkout = check_checkout(
        repo_dir, config["base_branch"], config["source_repos"],
        run_command=run_command,
    )
    optional_proxy = check_optional_proxy(run_command)
    return {
        "setup": "ok",
        "version": SETUP_VERSION,
        "base_branch": config["base_branch"],
        "repos": repo_results,
        "cli": cli,
        "service": units["service"],
        "timer": units["timer"],
        "checkout": checkout,
        "optional_proxy": optional_proxy,
    }


def format_setup(result: dict) -> list[str]:
    """Render the result document as stable key=value lines.

    One line per concern; values containing spaces are quoted (the
    ``pi_activity.quote_value`` convention) so the output stays
    parseable.
    """
    lines = [
        (
            f"setup={result['setup']} version={result['version']} "
            f"base_branch={result['base_branch']}"
        ),
        # Issue #152: the editable install step result (verified = the
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
    for instance in systemd_deploy.TIMER_INSTANCES:
        entry = timer["instances"][instance]
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
    lines.append(
        f"checkout=remote={checkout['remote']} "
        f"branch={quote_value(checkout['branch'])} "
        f"clean={'true' if checkout['clean'] else 'false'} "
        f"base_fresh={'true' if checkout['base_fresh'] else 'false'} "
        f"remote_url={checkout['remote_url']} "
        f"protocol={checkout['remote_protocol']} "
        f"migrated={'true' if checkout['migrated'] else 'false'} "
        f"ssh_reachable={reachable_text}"
    )
    proxy = result["optional_proxy"]
    lines.append(
        f"model_endpoint=optional optional_proxy={proxy['proxy']} "
        f"url={proxy['url']}"
    )
    return lines


def to_json(result: dict) -> str:
    """The result document as JSON (equivalent to the key=value lines)."""
    return json.dumps(result, indent=2, ensure_ascii=False)
