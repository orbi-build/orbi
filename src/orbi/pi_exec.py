"""The configured Pi executable (Issue #2182).

`pi_executable` in the host `orbi.toml` names the Pi-compatible CLI every
session starts (a command name on PATH or an absolute path, e.g.
`pi-bolt`); unset means `pi`. This module owns what is specific to a
non-default executable: reading the key for the machine-level probe that
runs before the config is validated, and that probe's own findings. The
default `pi` keeps its existing probe (`setup_checks.check_pi_command`).
There is no fallback from a configured executable to `pi`.
"""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from orbi.journal import quote_value

DEFAULT = "pi"


def configured(path: Path) -> str:
    """The `pi_executable` a config file names, else the default.

    A missing, unreadable or invalid config yields the default: the
    config step that follows the machine checks reports that problem.
    """
    try:
        value = tomllib.loads(
            Path(path).read_text(encoding="utf-8"),
        ).get("pi_executable")
    except (OSError, ValueError):
        return DEFAULT
    return value if isinstance(value, str) and value else DEFAULT


def check(run_command, executable: str, default_check, error, docs: str) -> None:
    """The Pi executable is found and answers `--version`.

    The default `pi` keeps its own probe (`default_check`). A configured
    executable gets findings (`error(check, reason, fix, docs)`) that name
    it and the `pi_executable` key; the Node/npm repair applies to `pi`
    only. The caller passes its probe and error type in, so this module
    imports nothing from the check layer.
    """
    if executable == DEFAULT:
        default_check(run_command)
        return
    fix = (
        f"install {executable}, correct `pi_executable` in orbi.toml, "
        "or remove the key to use the default `pi`"
    )
    if shutil.which(executable) is None:
        raise error(
            "pi",
            f"required command missing: {executable} (pi_executable in "
            "orbi.toml; not found) — the Runner starts one Pi session "
            "per task",
            fix, docs,
        )
    try:
        run_command([executable, "--version"], timeout=30)
    except Exception as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = " ".join(stderr.split()) or str(exc)
        raise error(
            "pi", f"{executable} --version failed: {detail}", fix, docs,
        ) from exc


def ok_line(executable: str) -> str:
    """The `check=pi ok` line; a configured executable is named."""
    if executable == DEFAULT:
        return "check=pi ok"
    return f"check=pi ok executable={quote_value(executable)}"
