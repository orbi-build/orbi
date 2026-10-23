"""CLI source consistency for Orbi.

The official local deployment is the EDITABLE uv tool install:

    uv tool install --force --reinstall --editable \\
        --python <interpreter> <deployment checkout>

where ``<interpreter>`` follows the compatible interpreter-selection
rule (Issue #861): the system ``python3`` when it satisfies the
packaging floor (``requires-python >= 3.14`` — Fedora 43 and current
Arch ship it), otherwise the bare ``3.14`` so uv provisions or
selects a compatible interpreter (Ubuntu 24.04 ships Python 3.12; a
hard pin to its ``/usr/bin/python3`` exits 1 with a dependency
resolution failure).

The tool env's Python imports the ``orbi`` package directly from
the deployment checkout (the setuptools editable finder maps the WHOLE
package directory ``src/orbi/`` onto the checkout),
so the ``ExecStartPre`` checkout sync (``orbi sync-engine-source``:
fetch plus fast-forward of the configured engine source track, #535)
is
picked up by the NEXT CLI process automatically: there is no second
copy of the source in site-packages and no per-version reinstall.

A NON-EDITABLE install (the pre-#152 flow) copies the source into the
tool env's site-packages at install time; the checkout then advances
underneath it and the running CLI keeps executing the stale copy —
with the #149 unit migration that deadlocked the deployment (the old
CLI checked the old non-templated unit paths and could never run the
new migration code). An editable install of a DIFFERENT (stale)
checkout drifts the same way.

This module is READ-ONLY: it reports the running process's
``orbi`` import source against the configured ``repo_dir`` and
the exact fix command. The fix is a HUMAN/setup step (``orbi
setup`` runs it idempotently). The Runner ALSO
refreshes the editable install at start (``refresh_cli_install``)
when the packaging inputs changed — under the SAME base-sync flock
the ``ExecStartPre`` preflight takes, so two concurrent service
instances still never race the tool env.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import orbi
from orbi.progress import quote_value

# The Python floor (Issue #861): the same floor as the PEP 621
# `requires-python` — pinned together by tests/test_cli_packaging.py
# (setup_checks.REQUIRED_PYTHON is an alias of this constant, so pip,
# `orbi check` and the interpreter selection can never disagree).
REQUIRED_PYTHON = (3, 14)

# The interpreter probe the selection (and the systemd self-heal
# template — the same source, pinned by tests) runs on the system
# `python3`: exit 0 iff the interpreter satisfies REQUIRED_PYTHON.
PYTHON_PROBE_CODE = (
    "import sys; raise SystemExit("
    f"0 if sys.version_info[:2] >= {REQUIRED_PYTHON} else 1)"
)

# A blocking command never runs bare (Issue #95): a wedged system
# interpreter must fail the probe, not hang the CLI start.
PROBE_TIMEOUT_SECONDS = 10


def system_python3_compatible(exe: str) -> bool:
    """Whether one concrete interpreter satisfies REQUIRED_PYTHON.

    Runs the :data:`PYTHON_PROBE_CODE` probe on ``exe`` (with a
    timeout — a blocking command never runs bare). Any failure
    (missing executable, non-zero probe exit, timeout) is NOT
    compatible: the selection fails safe to the uv-provisioned
    interpreter, which uv resolves against system AND managed
    interpreters either way.
    """
    if not exe:
        return False
    try:
        probe = subprocess.run(
            [exe, "-S", "-c", PYTHON_PROBE_CODE],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def select_python_interpreter() -> str:
    """The compatible interpreter-selection rule (Issue #861).

    The PATH ``python3`` when it exists AND satisfies the floor
    (Fedora 43, current Arch), otherwise the bare ``3.14`` — uv then
    provisions or selects a compatible interpreter (Ubuntu 24.04
    ships 3.12.3; on a fresh uv-only host there is no system python3
    at all). ``orbi setup``, the Runner pre-start refresh and the
    systemd self-heal template all apply this one rule.
    """
    exe = shutil.which("python3")
    if exe is not None and system_python3_compatible(exe):
        return "python3"
    return "3.14"


# The selection result the reinstall argv (and every fix string)
# embeds — computed once per process.
PYTHON_INTERPRETER = select_python_interpreter()

# The runtime package directory inside a checkout (the src
# layout): the editable install maps this WHOLE directory, so a newly
# added package module is importable without regenerating any module
# list (the #158 stale-finder root cause).
PACKAGE_DIR = Path("src") / "orbi"


def reinstall_args(repo_dir: Path) -> list[str]:
    """The editable force reinstall as an argv list (no shell).

    Verified against the real ``uv tool install --help``: ``--force``
    replaces the existing tool env, ``--reinstall`` bypasses the build
    cache, ``--editable`` points the tool env at the checkout ("changes
    in the package's source directory are reflected without
    reinstallation") and ``--python`` carries the compatible
    interpreter selection (Issue #861: the system ``python3`` when it
    satisfies the floor, otherwise the bare ``3.14`` so uv provisions
    or selects one).
    """
    return [
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", PYTHON_INTERPRETER, str(Path(repo_dir)),
    ]


def reinstall_command(repo_dir: Path) -> str:
    """The EXACT editable force reinstall command (one line, for a
    human or the fix field of a ``cli_source_drift`` line).

    It leads the repair, never ``orbi install-units`` alone
    (the unit files are only half of the #152 scene). A checkout path
    containing spaces is quoted so the line stays shell-executable.
    """
    args = reinstall_args(repo_dir)
    return " ".join(
        quote_value(arg) if arg is args[-1] else arg for arg in args
    )


def module_file() -> Path:
    """The running process's ``orbi`` package import source
    (resolved).

    This is the ground truth for "which source is this CLI process
    executing": the console script imports ``orbi`` at start,
    so ``__file__`` is the file the interpreter actually loaded —
    ``<checkout>/src/orbi/__init__.py`` for an editable install,
    a site-packages copy for a non-editable one.
    """
    file = getattr(orbi, "__file__", None)
    if not isinstance(file, str) or not file:
        raise RuntimeError(
            "cannot determine the orbi import source: the "
            "module has no __file__ (the CLI source check must never "
            "guess a path)"
        )
    return Path(file).resolve()


def cli_source(expected_repo_dir: Path) -> dict:
    """Read-only check of the CLI source against the configured repo.

    ``actual`` is the running process's import source
    (:func:`module_file`); ``expected`` is the configured ``repo_dir``
    (both resolved: a symlinked checkout path is the same source as
    the resolved one). ``editable`` is True exactly when the import
    source sits INSIDE the checkout's package directory — an editable
    install imports ``<repo_dir>/src/orbi/__init__.py``; a
    non-editable install imports a site-packages copy, a stale install
    a different checkout, and a nested copy (e.g. a worktree's own
    package) is not the configured source either. ``fix`` is the exact
    reinstall command for the expected checkout.
    """
    expected = Path(expected_repo_dir).resolve()
    actual = module_file()
    return {
        "actual": actual,
        "expected": expected,
        "editable": actual.is_relative_to(expected / PACKAGE_DIR),
        "fix": reinstall_command(expected_repo_dir),
    }


def drift_line(source: dict) -> str | None:
    """One structured ``cli_source_drift`` line, or None when clean.

    The line carries the actual import path, the expected repo_dir and
    the exact fix command (the editable force reinstall — the repair
    that makes the ExecStartPre sync reachable by the next CLI
    process). Values containing spaces are quoted (the
    progress.quote_value convention, like every unit_drift line).
    """
    if source["editable"]:
        return None
    return (
        "cli_source_drift "
        f"source={quote_value(str(source['actual']))} "
        f"expected={quote_value(str(source['expected']))} "
        f"fix={quote_value(source['fix'])}"
    )


# --- the editable CLI install refresh (the install domain belongs to
# --- the CLI-source module) ---

import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402

from orbi.gitops import acquire_base_sync_lock  # noqa: E402

CLI_INSTALL_LOGGER = logging.getLogger("orbi.cli_install")

# The uv install timeout (seconds): a local editable build of this
# zero-dependency package takes seconds; a hang (a wedged uv or a
# full disk) must fail the start, never block it forever (blocking
# commands carry a timeout).
UV_INSTALL_TIMEOUT_SECONDS = 300


class CliInstallError(RuntimeError):
    """The editable CLI install refresh failed (fail fast)."""


def packaging_fingerprint(repo_dir: Path) -> str:
    """The sha256 of the checkout's `pyproject.toml`.

    `pyproject.toml` is the packaging input that decides the editable
    metadata (the entry points, the version, the dependencies) — so
    its content hash is the refresh trigger. Ordinary Python source
    content is NOT part of it: since the src layout the
    editable finder maps the WHOLE `src/orbi/` package
    directory, so a newly added package module needs no reinstall
    (the whole point of the editable install). A checkout
    without `pyproject.toml` cannot be tool-installed: fail fast,
    never guess a fingerprint.
    """
    pyproject = Path(repo_dir) / "pyproject.toml"
    if not pyproject.is_file():
        raise CliInstallError(
            f"packaging file missing: {pyproject} (the deployment "
            "checkout must carry the packaging input of the editable "
            "install)"
        )
    return hashlib.sha256(pyproject.read_bytes()).hexdigest()


def install_state_path(repo_dir: Path) -> Path:
    """The last-install fingerprint record in the shared state dir.

    `<repo_dir>/.orbi/cli-install.json` — the EXISTING shared
    state dir (gitignored, next to `base-sync.lock` and the slots;
    it survives the `git merge --ff-only` checkout sync). Not a second
    release state and not a per-process temp file.
    """
    return Path(repo_dir) / ".orbi" / "cli-install.json"


def read_install_state(repo_dir: Path) -> str | None:
    """The stored last-install fingerprint, or None.

    Missing file (first install / fresh checkout) -> None. A
    malformed file (a torn write) is treated as "no state" and heals
    in the SAFE direction: one extra idempotent `--force
    --reinstall` runs — never a wedged start.
    """
    path = install_state_path(repo_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fingerprint = data.get("pyproject_sha256") if isinstance(data, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    return fingerprint


def write_install_state(repo_dir: Path, fingerprint: str) -> None:
    """Record the last-install fingerprint (atomic: tmp + replace).

    Only called AFTER a successful install, under the base-sync flock
    (no concurrent writer; the atomic replace guards a torn write on
    a crash mid-install).
    """
    path = install_state_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"pyproject_sha256": fingerprint}), encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def refresh_cli_install(
    repo_dir: Path, *, run_command,
    lock_timeout_seconds: float = 300.0,
    lock_repo_dir: Path | None = None,
) -> str:
    """Refresh the editable CLI install when the packaging inputs
    changed; return `"unchanged"` or `"installed"`. ``repo_dir`` is
    the checkout to install; ``lock_repo_dir`` optionally names the
    shared deployment checkout whose base-sync lock also protects the
    tool environment.

    The pre-start gate (called by the Runner tick before any slot or
    claim):

    - the current packaging fingerprint equals the stored last-install
      fingerprint -> `"unchanged"` and NO uv call (no per-tick
      reinstall);
    - otherwise (changed, or no state yet — the first install): take
      the base-sync flock (the SAME lock the service template's
      `ExecStartPre` and the checkout sync use), re-check the state
      UNDER the lock (a concurrent instance may have refreshed while
      we waited — reuse its result, never run a second install), run
      the exact verified editable force reinstall from
      `cli_source.reinstall_args`, and record the fingerprint only
      after success.

    A failing install logs the structured `cli_install_failed` line
    (reason + the exact fix command) and raises `CliInstallError`:
    the service does not start (fail fast), no state is recorded (the
    next start retries) and the lock is released (success or
    failure).
    """
    repo_dir = Path(repo_dir)
    lock_repo_dir = (
        Path(lock_repo_dir) if lock_repo_dir is not None else repo_dir
    )
    fingerprint = packaging_fingerprint(repo_dir)
    if read_install_state(repo_dir) == fingerprint:
        CLI_INSTALL_LOGGER.info(
            "cli_install_unchanged repo_dir=%s pyproject_sha256=%s",
            repo_dir, fingerprint,
        )
        return "unchanged"
    fd = acquire_base_sync_lock(lock_repo_dir, lock_timeout_seconds)
    try:
        # Re-check UNDER the lock: a concurrent instance may have
        # refreshed the tool env while we waited for the flock —
        # reuse its result, never run a second install.
        if read_install_state(repo_dir) == fingerprint:
            CLI_INSTALL_LOGGER.info(
                "cli_install_reused repo_dir=%s pyproject_sha256=%s",
                repo_dir, fingerprint,
            )
            return "unchanged"
        reason = "first_install" if (
            read_install_state(repo_dir) is None
        ) else "packaging_changed"
        try:
            run_command(
                reinstall_args(repo_dir),
                timeout=UV_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            CLI_INSTALL_LOGGER.error(
                "cli_install_failed repo_dir=%s reason=%s fix=%s",
                repo_dir, quote_value(str(exc)),
                quote_value(reinstall_command(repo_dir)),
            )
            raise CliInstallError(
                f"editable CLI install failed for {repo_dir}: {exc} "
                f"(fix: {reinstall_command(repo_dir)})"
            ) from exc
        write_install_state(repo_dir, fingerprint)
        CLI_INSTALL_LOGGER.info(
            "cli_install_refreshed repo_dir=%s reason=%s "
            "pyproject_sha256=%s",
            repo_dir, reason, fingerprint,
        )
        return "installed"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
