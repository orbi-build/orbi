"""CLI source consistency for Muyan Pilot (Issue #152).

The official local deployment is the EDITABLE uv tool install:

    uv tool install --force --reinstall --editable \\
        --python /usr/bin/python3 <deployment checkout>

The tool env's Python imports ``muyan_pilot`` directly from the
deployment checkout (the setuptools editable finder maps every runtime
module onto the checkout), so the ``ExecStartPre`` checkout sync
(``git fetch origin main && git merge --ff-only origin/main``) is
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
``muyan_pilot`` import source against the configured ``repo_dir`` and
the exact fix command. The fix is a HUMAN/setup step (``muyan-pilot
setup`` runs it idempotently); the Runner never installs the tool at
start, so two concurrent service instances cannot race the tool env.
"""
from __future__ import annotations

from pathlib import Path

import muyan_pilot
from pi_activity import quote_value

# The production interpreter pinned by the documented install command
# (docs/getting-started.mdx: `--python` pins the production
# interpreter, 3.14).
PYTHON_INTERPRETER = "/usr/bin/python3"


def reinstall_args(repo_dir: Path) -> list[str]:
    """The editable force reinstall as an argv list (no shell).

    Verified against the real ``uv tool install --help``: ``--force``
    replaces the existing tool env, ``--reinstall`` bypasses the build
    cache, ``--editable`` points the tool env at the checkout ("changes
    in the package's source directory are reflected without
    reinstallation") and ``--python`` pins the interpreter.
    """
    return [
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", PYTHON_INTERPRETER, str(Path(repo_dir)),
    ]


def reinstall_command(repo_dir: Path) -> str:
    """The EXACT editable force reinstall command (one line, for a
    human or the fix field of a ``cli_source_drift`` line).

    It leads the repair, never ``muyan-pilot install-units`` alone
    (the unit files are only half of the #152 scene). A checkout path
    containing spaces is quoted so the line stays shell-executable.
    """
    args = reinstall_args(repo_dir)
    return " ".join(
        quote_value(arg) if arg is args[-1] else arg for arg in args
    )


def module_file() -> Path:
    """The running process's ``muyan_pilot`` import source (resolved).

    This is the ground truth for "which source is this CLI process
    executing": the console script imports ``muyan_pilot`` at start,
    so ``__file__`` is the file the interpreter actually loaded —
    the checkout file for an editable install, a site-packages copy
    for a non-editable one.
    """
    file = getattr(muyan_pilot, "__file__", None)
    if not isinstance(file, str) or not file:
        raise RuntimeError(
            "cannot determine the muyan_pilot import source: the "
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
    source sits DIRECTLY inside the checkout root — an editable
    install (or the compat entry run inside the checkout) imports
    ``<repo_dir>/muyan_pilot.py``; a non-editable install imports a
    site-packages copy, a stale install a different checkout, and a
    nested copy (e.g. a worktree's own file) is not the configured
    source either. ``fix`` is the exact reinstall command for the
    expected checkout.
    """
    expected = Path(expected_repo_dir).resolve()
    actual = module_file()
    return {
        "actual": actual,
        "expected": expected,
        "editable": actual.parent == expected,
        "fix": reinstall_command(expected_repo_dir),
    }


def drift_line(source: dict) -> str | None:
    """One structured ``cli_source_drift`` line, or None when clean.

    The line carries the actual import path, the expected repo_dir and
    the exact fix command (the editable force reinstall — the repair
    that makes the ExecStartPre sync reachable by the next CLI
    process). Values containing spaces are quoted (the
    pi_activity.quote_value convention, like every unit_drift line).
    """
    if source["editable"]:
        return None
    return (
        "cli_source_drift "
        f"source={quote_value(str(source['actual']))} "
        f"expected={quote_value(str(source['expected']))} "
        f"fix={quote_value(source['fix'])}"
    )
