"""The prerequisite checks of `orbi setup` and `orbi check`.

Extracted from :mod:`orbi.pilot_setup` (Issue #1861): every independent
gate step lives here — the required commands and the scheduler user
session, `gh auth`, the `pi` probe with its Node floor, the per-repo
access/permission check (including the installation-token label probe),
and the machine check sequence that `orbi check` runs first.

Each step raises :class:`CheckError` (the structured finding with its
repair action and the official documentation link) or the shared
:class:`orbi.setup_config.SetupError` for a failed setup prerequisite.
The wording, the order and the `check=<step> ok` lines are the public
contract: `orbi setup` and `orbi check` must never disagree about what a
prerequisite is. This module never imports `orbi.pilot_setup` (no
reverse import) and never imports `runner` (Constitution Article 3.3).
"""
from __future__ import annotations

import json
import shutil
import sys
from uuid import uuid4

from orbi import cli_source, scheduler
from orbi.setup_config import SetupError


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
