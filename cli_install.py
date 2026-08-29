"""Auto-refresh of the editable CLI install at Runner start (Issue #158).

The official local deployment is the EDITABLE uv tool install (Issue
#152): the tool env imports the runtime modules directly from the
deployment checkout through a setuptools editable finder. The
finder's module MAPPING is generated at INSTALL time from the
checkout's ``pyproject.toml`` (``py-modules``, entry points, version,
dependencies) — so when the packaging inputs change (a new runtime
module is added to ``py-modules``, a dependency or entry point
changes), the installed finder is STALE: the next CLI process dies
with ``ModuleNotFoundError`` before the Runner can even start (the
#158 incident: ``cli_source`` merged to main, the installed finder
still mapped the pre-#152 module set, the systemd start failed).

The refresh runs at the Runner start, BEFORE any slot or claim:
unchanged packaging fingerprint -> NO uv call; changed or first
install -> ONE lock-protected ``uv tool install --force --reinstall
--editable --python /usr/bin/python3 <repo_dir>`` under the SAME
base-sync flock the service template's ``ExecStartPre`` takes (two
instances starting in the same tick serialize, the second reuses the
first's result); a failing install fails the start fast with the
structured ``cli_install_failed`` line (reason + the exact fix
command) and records no state (the next start retries).

This module is a THIN RE-EXPORT of the implementation, which lives in
``bootstrap_runner`` itself (see the NOTE there): the bootstrap chain
(``muyan_pilot`` -> ``bootstrap_runner``) must still LOAD in a tool
env whose installed editable finder predates this PR's packaging
change (the #158 stale-finder scene) — a separate new module for the
refresh would not be importable there, and the very refresh that
reinstalls the tool env could never run (the #158 incident, one
module later). The bootstrap chain never imports this module; the
tests use it as the single import point for the refresh contract.
"""
from bootstrap_runner import (
    UV_INSTALL_TIMEOUT_SECONDS,
    CliInstallError,
    acquire_base_sync_lock,
    base_sync_lock_path,
    install_state_path,
    packaging_fingerprint,
    read_install_state,
    refresh_cli_install,
    write_install_state,
)
