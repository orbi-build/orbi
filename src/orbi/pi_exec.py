"""The Pi CLI prerequisite: the executable and its one probe.

`pi_executable` in the host `orbi.toml` names the Pi-compatible CLI every
session starts (a command name on PATH or an absolute path, e.g.
`pi-bolt`); unset means `pi`. This module owns the prerequisite so it
has ONE probe and ONE validity rule for the key (Issue #2188): the probe
`orbi check` and `orbi setup` run with its findings, the machine-level
read of the key through the config's own rule
(`config.read_pi_executable`), and the `check=pi ok` line naming the
executable. The default `pi` keeps the Node floor wording; a configured
executable gets the `pi_executable` repair instead. There is no fallback
from a configured executable to `pi`.

The check layer passes its error type and documentation links in, so this
module imports nothing from it.
"""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from orbi import config
from orbi.journal import quote_value

# Pi's Node floor (Issue #1079): declared in Pi's `engines` but NOT
# enforced by npm — an `npm install` on an older Node reports success
# and every `pi` invocation then crashes with a bundle-level SyntaxError.
# The docs state the floor; the check names it in `fix=` when the
# version probe fails.
PI_NODE_FLOOR = "22.19.0"
PI_INSTALL_COMMAND = (
    "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
)


def configured(path: Path, error, docs) -> str:
    """The `pi_executable` a config file names, else the default.

    A missing or unparseable config yields the default: the config step
    that follows the machine checks reports that problem. A present but
    invalid value is the `config` finding, raised through the ONE rule
    for the key (`config.read_pi_executable`) — never a probe of the
    default `pi`.
    """
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config.PI_EXECUTABLE_DEFAULT
    try:
        return config.read_pi_executable(data)
    except ValueError as exc:
        raise error(
            "config", f"invalid orbi.toml: {exc}",
            "fix the reported problem in orbi.toml", docs["config"],
        ) from exc


def _configured_fix(executable: str) -> str:
    """The repair for a configured `pi_executable` — never the Node one."""
    return (
        f"install {executable}, correct `pi_executable` in orbi.toml, "
        "or remove the key to use the default `pi`"
    )


def _missing(executable: str, error, docs):
    """The finding for an executable the PATH lookup cannot find."""
    if executable == config.PI_EXECUTABLE_DEFAULT:
        return error(
            "pi",
            "required command missing: pi (not on PATH) — the Runner "
            "starts one Pi session per task",
            f"install Node >= {PI_NODE_FLOOR}, then {PI_INSTALL_COMMAND} "
            "and put it on the PATH (see the linked repository)",
            docs["pi"],
        )
    return error(
        "pi",
        f"required command missing: {executable} (pi_executable in "
        "orbi.toml; not found) — the Runner starts one Pi session "
        "per task",
        _configured_fix(executable), docs["config"],
    )


def _probe_failed(executable: str, detail: str, error, docs):
    """The finding for a `--version` probe that exits non-zero."""
    if executable == config.PI_EXECUTABLE_DEFAULT:
        return error(
            "pi",
            f"pi --version failed: {detail}",
            f"Pi needs Node >= {PI_NODE_FLOOR} and npm does not enforce "
            "that floor: an install on an older Node reports success "
            "but every pi call then crashes with a bundle SyntaxError — "
            f"install Node >= {PI_NODE_FLOOR} and reinstall with "
            f"{PI_INSTALL_COMMAND}",
            docs["pi"],
        )
    return error(
        "pi", f"{executable} --version failed: {detail}",
        _configured_fix(executable), docs["config"],
    )


def check_pi_command(
    run_command, executable: str, error, docs,
) -> None:
    """The Pi CLI (one Pi session per task) is on the PATH and executes.

    Deliberately NOT part of the setup ``REQUIRED_COMMANDS`` gate: setup
    provisions GitHub and the scheduler state, while Pi is the model-facing
    runtime the prerequisite gate verifies. A PATH hit alone is not
    enough (Issue #1079): npm does not enforce Pi's ``engines`` floor,
    so the ONE probe executes ``<executable> --version`` (Issue #2188);
    a failure of the default ``pi`` names the Node floor in the fix, a
    configured ``pi_executable`` names the key instead.
    """
    if shutil.which(executable) is None:
        raise _missing(executable, error, docs)
    try:
        run_command([executable, "--version"], timeout=30)
    except Exception as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = " ".join(stderr.split()) or str(exc)
        raise _probe_failed(executable, detail, error, docs) from exc


def ok_line(executable: str) -> str:
    """The `check=pi ok` line; a configured executable is named."""
    if executable == config.PI_EXECUTABLE_DEFAULT:
        return "check=pi ok"
    return f"check=pi ok executable={quote_value(executable)}"
