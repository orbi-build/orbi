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
"""
from __future__ import annotations

import importlib.resources
import json
import logging
import re
import subprocess
import sys
from uuid import uuid4
import shutil
import tomllib
from collections.abc import Sequence
from pathlib import Path

LOGGER = logging.getLogger("orbi.pilot_setup")

from orbi import runner, config as config_domain
from orbi import cli_source
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    AWAITING_MERGE_LABEL,
    FIX_NEEDED_LABEL,
    HUMAN_REVIEW_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    NEEDS_DETAIL_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)
from orbi import git_transport
from orbi import scheduler
from orbi.progress import quote_value
from orbi.journal import event

# Bumped whenever the setup output contract changes shape.
# 4 (Issue #849): the scheduler session check and its fields are
# platform-named (`systemd_session` / `launchd_session`, the `paths`
# key is the scheduler name), no longer systemd-only.
SETUP_VERSION = 4

PROVIDER_FILE_NAME = "pi-providers.json"
PROVIDER_ENV_NAME = "PROVIDER_API_KEY"
PROVIDER_STARTER = {
    "_comment": "Edit this OpenAI-compatible provider and select it in orbi.toml.",
    "providers": {
        "openai": {
            "baseUrl": "https://api.openai.com/v1",
            "api": "openai-completions",
            "apiKey": "$PROVIDER_API_KEY",
            "models": [{
                "id": "your-model",
                "name": "Your model",
                "contextWindow": 131072,
                "maxTokens": 16384,
            }],
        },
    },
}
PROVIDER_GUIDE = (
    "See https://docs.orbi.build/getting-started"
    "#4-configure-the-model-provider"
)

# The repo-managed single source of truth for the platform labels.
LABELS_FILE = "labels.toml"
REQUIRED_LABELS = (
    READY_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
    FIX_NEEDED_LABEL,
    MERGED_LABEL,
    BLOCKED_LABEL,
    AWAITING_MERGE_LABEL,
    "p0",
    # Epic marker: the claim scan skips `ai-epic` Issues (`epic_not_claimed`),
    # so the label is platform state the setup entry must guarantee.
    "ai-epic",
    # Release task marker: the ready scan picks up `ai-ready`+`ai-release`
    # Issues and routes them to the deterministic release state machine.
    "ai-release",
    # Content-only marker: the pure content agent delivers text directly in
    # the Issue (no execution, no git).
    "ai-content-only",
    # Ops marker: a full-execution session (shell/gh/network, the ops
    # playbook) whose deliverable is evidence posted to the Issue.
    "ai-ops-only",
    # Human acceptance gate: the label only a human applies
    # to confirm a delivery's acceptance checklist — the Runner never
    # adds or removes it, but setup provisions it so the gate is ready
    # before the operator turns `human_review_gate` on.
    HUMAN_REVIEW_LABEL,
    # Thin-ticket gate (Issue #1088): a stopped ticket waits here.
    NEEDS_DETAIL_LABEL,
)
COLOR_PATTERN = re.compile(r"^[0-9a-fA-F]{6}$")

# Permissions that may create/edit labels (GitHub viewerPermission).
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})

# Required local commands (checked with shutil.which). The
# the installed `orbi` CLI is a prerequisite too — the scheduler
# entry it documents (and the service's ExecStart) must exist.
# `uv` is a prerequisite too — the CLI editable step
# calls `uv tool install`, so a machine that has the CLI
# but no uv must fail HERE (with actionable install guidance), not
# mid-install with an indirect error.
REQUIRED_COMMANDS = ("git", "gh", "uv", "orbi")

# Actionable install guidance per required command: the
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
    "orbi": (
        "install the editable uv tool CLI from the deployment checkout: "
        # The SAME compatible interpreter selection the CLI editable
        # step passes (Issue #861): the system python3 when it
        # satisfies the floor, the uv-provisioned 3.14 otherwise.
        f"uv tool install --force --reinstall --editable --python "
        f"{cli_source.PYTHON_INTERPRETER} <repo_dir>"
    ),
}

# The optional local-llm-kv-cache proxy health endpoint (docs/
# optional-kv-cache.mdx: the committed user unit listens on 18082).
OPTIONAL_PROXY_URL = "http://127.0.0.1:18082/health"
OPTIONAL_PROXY_TIMEOUT = 3


class SetupError(RuntimeError):
    """A core setup prerequisite or step failed (fail fast)."""


class CheckError(RuntimeError):
    """One failed `orbi check` prerequisite (read-only gate).

    ``check`` names the failed step, ``reason`` the concrete finding,
    ``fix`` the repair action and ``docs`` the official documentation
    link (never a secret value — the provider status stays value-free).
    """

    def __init__(self, check: str, reason: str, fix: str, docs: str):
        super().__init__(f"{check}: {reason}")
        self.check = check
        self.reason = reason
        self.fix = fix
        self.docs = docs


def format_check_failure(exc: CheckError) -> str:
    """The one parseable stderr line for a failed gate."""
    return (
        f"check_failed check={exc.check} "
        f"reason={quote_value(exc.reason)} "
        f"fix={quote_value(exc.fix)} docs={exc.docs}"
    )


# The Python floor the `orbi check` gate enforces. An alias of the
# interpreter-selection floor (Issue #861) in cli_source — pinned
# against the PEP 621 `requires-python` by tests/test_cli_packaging.py:
# pip enforces the same floor at install time, the gate re-states it at
# runtime so a hand-rolled interpreter cannot silently run the CLI.
REQUIRED_PYTHON = cli_source.REQUIRED_PYTHON

# Official documentation links the check failures carry:
# every failure names its repair action AND the official doc.
DOCS_LINKS = {
    "python": "https://www.python.org/downloads/",
    "git": "https://git-scm.com/downloads",
    "gh": "https://cli.github.com/",
    "gh_auth": "https://docs.github.com/en/authentication",
    "uv": "https://docs.astral.sh/uv/getting-started/installation/",
    **scheduler.DOCS,
    "ssh": (
        "https://docs.github.com/en/authentication/"
        "connecting-to-github-with-ssh"
    ),
    "pi": "https://github.com/earendil-works/pi",
    "config": "https://docs.orbi.build/getting-started",
    "provider": (
        "https://docs.orbi.build/getting-started"
        "#4-configure-the-model-provider"
    ),
}

# The per-command official link for the gate's command step (the SET and
# the hints stay single-sourced in REQUIRED_COMMANDS /
# COMMAND_INSTALL_HINTS — setup and check cannot disagree).
CHECK_COMMAND_DOCS = {
    "git": DOCS_LINKS["git"],
    "gh": DOCS_LINKS["gh"],
    "uv": DOCS_LINKS["uv"],
    "orbi": DOCS_LINKS["config"],
}


def packaged_example_bytes() -> bytes:
    """The example config SHIPPED IN THE PACKAGE.

    A PyPI install carries no checkout, so the example travels inside
    the wheel (`src/orbi/example_config.toml`, declared as package
    data). Raises OSError when the install is broken (the file is
    missing from the environment).
    """
    return (
        importlib.resources.files("orbi")
        .joinpath("example_config.toml")
        .read_bytes()
    )


def ensure_config(path: Path) -> Path:
    """Create a local config when absent; never overwrite.

    The ADJACENT `.orbi.example.toml` wins (a deployment home may keep
    its own example); without one the example shipped in the package is
    used.
    Both unavailable is a broken install: fail fast, no partial config.
    """
    path = Path(path)
    if path.exists():
        return path
    example = path.parent / ".orbi.example.toml"
    if example.is_file():
        payload = example.read_bytes()
    else:
        try:
            payload = packaged_example_bytes()
        except OSError as exc:
            raise SetupError(
                f"example config unavailable for {path}: no adjacent "
                f"{example} and the packaged example failed: {exc}"
            ) from exc
    try:
        path.write_bytes(payload)
    except OSError as exc:
        raise SetupError(f"config creation failed for {path}: {exc}") from exc
    return path


def load_label_defs(path: Path) -> list[dict]:
    """Parse and validate the repo-managed label definitions.

    The file must define EXACTLY the platform labels (no more, no
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


# The scheduler-session step's per-platform repair hint (the
# launchctl/systemctl literals live in the scheduler implementations;
# only the operator-facing wording lives here).
SESSION_FIX = {
    "systemd": (
        "run orbi inside a systemd user session (log in locally or "
        "start the user session)"
    ),
    "launchd": (
        "log in to the macOS GUI session (launchd's gui domain must "
        "exist for this user)"
    ),
}


def session_unavailable_message(sched, detail: str) -> str:
    """The one human-readable line for an unreachable scheduler session."""
    return (
        f"{sched.display} user session unavailable (is a {sched.display} "
        f"session running?): {detail}"
    )


def check_commands(run_command, unit_name: str | None = None,
                   *, sched=None) -> dict:
    """Verify the required commands and the scheduler user session.

    ``git``, ``gh``, ``uv`` and the installed ``orbi`` CLI must be on
    the PATH (``uv`` is
    checked explicitly because the CLI editable step calls
    ``uv tool install``); a missing command fails fast with the
    actionable install guidance for that command (``COMMAND_INSTALL_
    HINTS``). The platform scheduler's user session must be reachable
    (the scheduler's ``probe_args`` must succeed — a machine without
    systemd/launchd or a headless session without a user session fails
    fast with the concrete reason).
    No mutation happens here.
    """
    try:
        sched = sched or scheduler.detect()
    except scheduler.UnsupportedPlatformError as exc:
        raise SetupError(str(exc)) from exc
    paths: dict[str, str] = {}
    for name in REQUIRED_COMMANDS:
        path = shutil.which(name)
        if path is None:
            raise SetupError(
                f"required command missing: {name} (not on PATH) — "
                f"{COMMAND_INSTALL_HINTS[name]}"
            )
        paths[name] = path
    try:
        run_command(sched.probe_args(unit_name))
    except Exception as exc:
        raise SetupError(
            session_unavailable_message(sched, str(exc))
        ) from exc
    paths[sched.name] = "session-ok"
    return paths


def check_auth(run_command) -> None:
    """Verify ``gh auth status`` (logged in with a usable token)."""
    try:
        run_command(["gh", "auth", "status"])
    except Exception as exc:
        raise SetupError(
            f"gh auth status failed (log in with `gh auth login`): {exc}"
        ) from exc


def token_is_installation(run_command) -> bool:
    """Is the gh credential a GitHub App installation token (``ghs_``)?

    ``viewerPermission`` is only meaningful for a user
    token; an installation token reports an empty field there. The
    token prefix (verified against gh 2.100.0 ``gh auth token``) is the
    credential shape's ground truth. An unreadable token is NOT an
    installation token: the caller keeps its existing fail-fast.
    """
    try:
        token = run_command(["gh", "auth", "token"]).strip()
    except Exception:
        return False
    return token.startswith("ghs_")


def probe_label_capability(repo: str, run_command) -> None:
    """Prove the credential can manage labels with a one-shot probe.

    Create a uniquely-named probe label and delete it again
    (verified against the real API on 2026-09-09: create → 201 JSON,
    delete → 204, both for a user token here and for the installation
    token in the Issue scene). A create failure is a readable fail
    fast with the repair action; a delete failure still fails fast and
    names the label a human may have to remove.
    """
    name = f"orbi-setup-probe-{uuid4().hex[:8]}"
    try:
        run_command([
            "gh", "api", "-X", "POST", f"repos/{repo}/labels",
            "-f", f"name={name}", "-f", "color=cccccc",
            "-f", "description=orbi setup capability probe",
        ])
    except Exception as exc:
        raise SetupError(
            f"installation token cannot manage labels in {repo}: {exc} "
            "(the GitHub App needs the Issues read/write repository "
            f"permission and must be installed on {repo})"
        ) from exc
    try:
        run_command([
            "gh", "api", "-X", "DELETE", f"repos/{repo}/labels/{name}",
        ])
    except Exception as exc:
        raise SetupError(
            f"installation token label probe cleanup failed for {repo}: "
            f"the probe label {name!r} may be left behind: {exc}"
        ) from exc


def check_repo(repo: str, run_command) -> dict:
    """Verify the target repo exists and the viewer may write to it.

    ``gh repo view`` (verified against the real CLI: unknown repo exits
    non-zero, a readable repo returns ``nameWithOwner``,
    ``viewerPermission`` and ``defaultBranchRef``). The viewer
    permission must allow label mutation (WRITE/MAINTAIN/ADMIN).
    An installation token (``ghs_``) reports an empty
    ``viewerPermission`` although its issues:write manages labels, so
    the empty field on an installation token is decided by a one-shot
    create+delete label probe instead of the meaningless field; a user
    token keeps the exact prior behavior.
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
        data = None
        for line in raw.splitlines():
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                data = candidate
                break
        if data is None:
            raise SetupError(
                f"cannot parse gh repo view output for {repo}: {raw!r}"
            ) from exc
    if not isinstance(data, dict):
        raise SetupError(
            f"cannot parse gh repo view output for {repo}: {raw!r}"
        )
    permission = data.get("viewerPermission")
    if permission not in WRITE_PERMISSIONS:
        if not permission and token_is_installation(run_command):
            probe_label_capability(repo, run_command)
            permission = "INSTALLATION"
        else:
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
    """Keep Runner-created worktrees out of the main checkout status."""
    if not (Path(repo_dir) / ".worktrees").is_dir():
        return False
    try:
        run_command(["git", "check-ignore", "--quiet", "--", ".worktrees/"], cwd=repo_dir)
        return False
    except subprocess.CalledProcessError:
        exclude = Path(run_command(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=repo_dir,
        ))
        if not exclude.is_absolute():
            exclude = Path(repo_dir) / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".worktrees/" not in existing.splitlines():
            exclude.write_text(
                existing + ("\n" if existing and not existing.endswith("\n") else "")
                + ".worktrees/\n", encoding="utf-8",
            )
        event("worktrees_exclude_added", repo=repo_dir, path=exclude)
        return True


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
                "with a resolvable HEAD — mount the checkout with: "
                "docker run -v <path>:/work (or mount an empty volume so "
                "the task pool can be cloned)"
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


def scaffold_model_config(config: RunnerConfig) -> dict:
    """Create the non-secret provider starter and key slot idempotently."""
    deploy_home = Path(config.deploy_home)
    state_dir = deploy_home / ".orbi"
    state_dir.mkdir(parents=True, exist_ok=True)
    provider_path = config.pi_providers or state_dir / PROVIDER_FILE_NAME
    provider_path = Path(provider_path)
    provider_created = False
    if not provider_path.exists():
        provider_path.write_text(
            json.dumps(PROVIDER_STARTER, indent=2) + "\n", encoding="utf-8",
        )
        provider_created = True
    env_path = state_dir / "env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_created = PROVIDER_ENV_NAME not in {
        line.split("=", 1)[0].removeprefix("export ").strip()
        for line in env_text.splitlines() if "=" in line
    }
    if env_created:
        with env_path.open("a", encoding="utf-8") as handle:
            if env_text and not env_text.endswith("\n"):
                handle.write("\n")
            handle.write(f"# API key for the starter provider\n{PROVIDER_ENV_NAME}=\n")
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    return {
        "provider_file": str(provider_path),
        "provider_created": provider_created,
        "env_file": str(env_path),
        "env_variable": PROVIDER_ENV_NAME,
        "env_created": env_created,
    }


def model_provider_status(config: RunnerConfig) -> dict:
    """Return a safe, value-free provider status for setup and doctor."""
    provider = config.pi_provider
    model = config.pi_model
    path = config.pi_providers
    if path is None:
        path = Path(config.deploy_home) / ".orbi" / PROVIDER_FILE_NAME
    finding = config.pi_provider_key_finding
    env_file = Path(config.deploy_home) / ".orbi" / "env"
    if not provider or not model or not config.pi_providers_data:
        return {
            "state": "NOT CONFIGURED", "provider_file": str(path),
            "env_file": str(env_file), "env_variable": PROVIDER_ENV_NAME,
        }
    if finding:
        return {
            "state": "NOT CONFIGURED", "provider": provider, "model": model,
            "key": f"{finding['variable']}={finding['state']}",
            "provider_file": str(path), "env_file": str(env_file),
            "env_variable": finding["variable"],
        }
    entry = config.pi_providers_data["providers"][provider]
    key = entry.get("apiKey") if isinstance(entry, dict) else None
    variable = (re.fullmatch(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)\}?", key or ""))
    key_name = variable.group(1) if variable else "literal"
    return {
        "state": "ok", "provider": provider, "model": model,
        "key": f"{key_name}=set", "provider_file": str(path),
        "env_file": str(env_file), "env_variable": key_name,
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


def check_python_version() -> None:
    """The running interpreter satisfies the packaging floor."""
    if sys.version_info[:2] < REQUIRED_PYTHON:
        floor = ".".join(str(part) for part in REQUIRED_PYTHON)
        raise CheckError(
            "python",
            f"python {sys.version_info[0]}.{sys.version_info[1]} is too "
            f"old (orbi requires >= {floor})",
            f"install Python {floor} and run orbi in that environment",
            DOCS_LINKS["python"],
        )


# Pi's Node floor (Issue #1079): declared in Pi's `engines` but NOT
# enforced by npm — an `npm install` on an older Node reports success
# and every `pi` invocation then crashes with a bundle-level SyntaxError.
# The docs state the floor; the check names it in `fix=` when the
# version probe fails.
PI_NODE_FLOOR = "22.19.0"
PI_INSTALL_COMMAND = (
    "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
)


def check_pi_command(run_command) -> None:
    """The `pi` CLI (one Pi session per task) is on the PATH and executes.

    Deliberately NOT part of the setup ``REQUIRED_COMMANDS`` gate: setup
    provisions GitHub and the scheduler state, while Pi is the model-facing
    runtime the prerequisite gate verifies. A PATH hit alone is not
    enough (Issue #1079): npm does not enforce Pi's ``engines`` floor,
    so the probe executes ``pi --version``; a failure names the Node
    floor in the fix.
    """
    if shutil.which("pi") is None:
        raise CheckError(
            "pi",
            "required command missing: pi (not on PATH) — the Runner "
            "starts one Pi session per task",
            f"install Node >= {PI_NODE_FLOOR}, then {PI_INSTALL_COMMAND} "
            "and put it on the PATH (see the linked repository)",
            DOCS_LINKS["pi"],
        )
    try:
        run_command(["pi", "--version"], timeout=30)
    except Exception as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = " ".join(stderr.split()) or str(exc)
        raise CheckError(
            "pi",
            f"pi --version failed: {detail}",
            f"Pi needs Node >= {PI_NODE_FLOOR} and npm does not enforce "
            "that floor: an install on an older Node reports success "
            "but every pi call then crashes with a bundle SyntaxError — "
            f"install Node >= {PI_NODE_FLOOR} and reinstall with "
            f"{PI_INSTALL_COMMAND}",
            DOCS_LINKS["pi"],
        ) from exc


def run_machine_checks(*, run_command, collect_failures: bool = False) -> tuple[list[str], list[CheckError]]:
    """Run the config-independent checks, optionally collecting failures.

    The normal gate remains fail-fast.  The missing-config CLI path uses the
    collection mode so independent machine problems can be shown together.
    """
    lines: list[str] = []
    failures: list[CheckError] = []

    def record(check):
        try:
            check()
        except CheckError as exc:
            if not collect_failures:
                raise
            failures.append(exc)
            return False
        return True

    if record(check_python_version):
        lines.append(
            f"check=python ok version="
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
    try:
        sched = scheduler.detect()
    except scheduler.UnsupportedPlatformError as exc:
        failure = CheckError(
            "platform", str(exc),
            "run orbi on Linux (systemd) or macOS (launchd)",
            scheduler.ISSUE_URL,
        )
        if not collect_failures:
            raise failure from exc
        failures.append(failure)
        sched = None

    for name in REQUIRED_COMMANDS:
        if shutil.which(name) is None:
            failure = CheckError(
                "commands",
                f"required command missing: {name} (not on PATH)",
                COMMAND_INSTALL_HINTS[name], CHECK_COMMAND_DOCS[name],
            )
            if not collect_failures:
                raise failure
            failures.append(failure)
        else:
            lines.append(f"check=command ok name={name}")

    if sched is not None:
        def check_session():
            try:
                run_command(sched.probe_args(None))
            except Exception as exc:
                raise CheckError(
                    f"{sched.name}_session",
                    session_unavailable_message(sched, str(exc)),
                    SESSION_FIX[sched.name], DOCS_LINKS[sched.name],
                ) from exc

        if record(check_session):
            lines.append(f"check={sched.name}_session ok")

    def check_gh_auth():
        try:
            check_auth(run_command)
        except SetupError as exc:
            raise CheckError(
                "gh_auth", str(exc), "log in with `gh auth login`",
                DOCS_LINKS["gh_auth"],
            ) from exc

    if record(check_gh_auth):
        lines.append("check=gh_auth ok")
    if record(lambda: check_pi_command(run_command)):
        lines.append("check=pi ok")
    return lines, failures


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
            "create the missing path or fix orbi.toml",
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
    ``progress.quote_value`` convention) so the output stays
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
