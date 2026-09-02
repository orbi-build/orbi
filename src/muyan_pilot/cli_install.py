"""Auto-refresh of the editable CLI install at Runner start (Issue #158).

The official local deployment is the EDITABLE uv tool install (Issue
#152): the tool env imports the runtime package directly from the
deployment checkout through a setuptools editable finder. Since the
src layout (Issue #168) the finder maps the WHOLE package directory
``src/muyan_pilot/`` — a newly added package module needs NO reinstall
(the #158 stale-module-list incident class is fixed at the root). The
remaining packaging inputs come from the checkout's ``pyproject.toml``
(entry points, version, dependencies): when they change the installed
tool env is STALE and the next CLI process can die before the Runner
even starts (the #158 incident shape).

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
``runner`` itself (see the NOTE there): the bootstrap chain
(``muyan_pilot.cli`` -> ``muyan_pilot.runner``) must still LOAD in a
tool env whose installed editable finder predates a packaging change.
The bootstrap chain never imports this module; the tests use it as the
single import point for the refresh contract.
"""
from muyan_pilot.runner import (
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
