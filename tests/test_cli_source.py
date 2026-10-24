"""CLI source consistency tests (Issue #152).

The official local deployment is the EDITABLE uv tool install: the tool
env's Python imports `orbi` directly from the deployment checkout
(a setuptools editable finder), so the ExecStartPre checkout sync is
picked up by the NEXT CLI process automatically — there is no second
copy of the source in site-packages and no per-version reinstall.

These tests pin:

- the exact editable force-reinstall command (verified against the real
  `uv tool install --help`: `--force`, `--reinstall`, `--editable` and
  `--python` all exist);
- the read-only source check: the running process's `orbi`
  import file must sit directly inside the configured `repo_dir`
  (a non-editable site-packages source or a stale other-checkout
  source is drift);
- the structured `cli_source_drift` line (actual path, expected
  repo_dir, the exact reinstall command) and its absence when clean.
"""
from pathlib import Path
from types import ModuleType

import pytest

from orbi import cli_source
from orbi.journal import quote_value


def _fake_module_file(path: str) -> ModuleType:
    """A stand-in for the imported `orbi` module."""
    module = ModuleType("orbi")
    module.__file__ = path
    return module


def _patch_module_file(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        cli_source, "orbi", _fake_module_file(path),
    )


# --- the exact reinstall command -------------------------------------------


def test_reinstall_command_is_the_editable_force_reinstall():
    """The fix command is the editable force reinstall from the
    configured checkout (the Issue's recovery command, verbatim):
    `--force` replaces the existing tool env, `--reinstall` bypasses
    the build cache, `--editable` points the tool env at the checkout,
    `--python` pins the production interpreter."""
    command = cli_source.reinstall_command(
        Path("/home/xqianliu/Documents/orbi/orbi"),
    )
    assert command == (
        "uv tool install --force --reinstall --editable "
        f"--python {cli_source.PYTHON_INTERPRETER} "
        "/home/xqianliu/Documents/orbi/orbi"
    )


# --- the read-only source check ---------------------------------------------


def test_cli_source_clean_for_the_checkout_source(tmp_path, monkeypatch):
    """An editable install (or the compat entry run inside the
    checkout) imports `orbi` from the checkout root: clean."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _patch_module_file(monkeypatch, str(repo / "src" / "orbi" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert source["editable"] is True
    assert source["actual"] == (repo / "src" / "orbi" / "__init__.py").resolve()
    assert source["expected"] == repo.resolve()
    assert source["fix"] == cli_source.reinstall_command(repo)


def test_cli_source_clean_when_the_expected_dir_is_a_symlink(
    tmp_path, monkeypatch,
):
    """The comparison resolves both sides: a symlinked checkout path
    (e.g. `/home/xqianliu/Documents/orbi/orbi` reached through
    a link) is the same source as the resolved one."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _patch_module_file(monkeypatch, str(real / "src" / "orbi" / "__init__.py"))
    source = cli_source.cli_source(link)
    assert source["editable"] is True
    assert source["actual"] == (real / "src" / "orbi" / "__init__.py").resolve()


def test_cli_source_drifts_for_a_site_packages_source(tmp_path, monkeypatch):
    """A NON-EDITABLE uv tool install copies the source into the tool
    env's site-packages: the import source is outside the checkout, so
    the ExecStartPre sync can never reach it (the #152 deadlock)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    site_packages = (
        tmp_path / "uv" / "tools" / "orbi"
        / "lib" / "python3.14" / "site-packages"
    )
    site_packages.mkdir(parents=True)
    _patch_module_file(
        monkeypatch, str(site_packages / "orbi" / "__init__.py"),
    )
    source = cli_source.cli_source(repo)
    assert source["editable"] is False
    assert source["actual"] == (site_packages / "orbi" / "__init__.py").resolve()
    assert source["fix"] == cli_source.reinstall_command(repo)


def test_cli_source_drifts_for_a_stale_other_checkout(tmp_path, monkeypatch):
    """An editable install of a DIFFERENT (stale) checkout also drifts:
    the import source is not inside the configured repo_dir."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    stale = tmp_path / "old-clone"
    stale.mkdir()
    _patch_module_file(monkeypatch, str(stale / "src" / "orbi" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert source["editable"] is False
    assert source["actual"] == (stale / "src" / "orbi" / "__init__.py").resolve()


def test_cli_source_drifts_for_a_nested_copy(tmp_path, monkeypatch):
    """A copy of the source nested INSIDE the checkout (e.g. a
    worktree's own `orbi.py` shadowing the checkout root file)
    is not the configured source either: only the checkout ROOT
    `orbi.py` counts."""
    repo = tmp_path / "checkout"
    (repo / ".worktrees" / "wt").mkdir(parents=True)
    _patch_module_file(
        monkeypatch, str(repo / ".worktrees" / "wt" / "src" / "orbi" / "__init__.py"),
    )
    source = cli_source.cli_source(repo)
    assert source["editable"] is False


# --- the structured drift line ----------------------------------------------


def test_drift_line_carries_source_expected_and_fix(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    stale = tmp_path / "old-clone"
    stale.mkdir()
    _patch_module_file(monkeypatch, str(stale / "src" / "orbi" / "__init__.py"))
    source = cli_source.cli_source(repo)
    line = cli_source.drift_line(source)
    assert line is not None
    assert line.startswith("cli_source_drift ")
    assert f"source={quote_value(str(source['actual']))}" in line
    assert f"expected={quote_value(str(repo.resolve()))}" in line
    # The fix command carries spaces, so the field is quoted (the
    # journal.quote_value convention, like every unit_drift line).
    assert (
        f"fix={quote_value(cli_source.reinstall_command(repo))}"
    ) in line


def test_drift_line_is_none_when_clean(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _patch_module_file(monkeypatch, str(repo / "src" / "orbi" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert cli_source.drift_line(source) is None


# --- the real module file ----------------------------------------------------


def test_module_file_is_the_running_orbi_file():
    """`module_file()` is the running process's `orbi` import
    source (asserted against the real imported module — one real call,
    not a guessed shape)."""
    import orbi

    assert cli_source.module_file() == Path(
        orbi.__file__,
    ).resolve()


def test_module_file_fails_fast_without_a_file_attribute(monkeypatch):
    """A module without `__file__` cannot be located: the check fails
    fast with the concrete reason (never a guessed path)."""
    module = ModuleType("orbi")
    monkeypatch.setattr(cli_source, "orbi", module)

    with pytest.raises(RuntimeError, match="no __file__"):
        cli_source.module_file()


# --- the compatible interpreter selection (Issue #861) -----------------------
#
# Orbi's `requires-python = ">=3.14"` is a compatibility FLOOR, not a
# demand to replace every distro Python: when the system `python3`
# satisfies the floor (Fedora 43, current Arch) it is used directly;
# when it does not (Ubuntu 24.04 ships 3.12.3) the selection returns
# `3.14` so uv provisions/selects a compatible interpreter. The rule
# lives ONLY here — `orbi setup`, the Runner pre-start refresh and the
# systemd self-heal template all apply it.


def _fake_python3(tmp_path: Path, body: str) -> str:
    """One fake `python3` executable with the given shell body."""
    exe = tmp_path / "python3"
    exe.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def test_probe_accepts_a_compatible_interpreter():
    """One real call: the interpreter running this test suite
    satisfies the packaging floor (requires-python >= 3.14 gates the
    test env itself), so the probe reports compatible."""
    import sys

    assert cli_source.system_python3_compatible(sys.executable) is True


def test_probe_rejects_an_interpreter_below_the_floor(tmp_path):
    """A system `python3` older than the floor (the Ubuntu 24.04
    scene: 3.12.3) is NOT compatible — uv must provision instead."""
    assert cli_source.system_python3_compatible(
        _fake_python3(tmp_path, "exit 1"),
    ) is False


def test_probe_rejects_a_broken_interpreter(tmp_path):
    """A probe that crashes (non-0/1 exit) is not compatible: the
    selection fails safe to the uv-provisioned interpreter."""
    assert cli_source.system_python3_compatible(
        _fake_python3(tmp_path, "exit 7"),
    ) is False


def test_probe_rejects_an_empty_executable():
    """An empty interpreter path (the `which` -> None sentinel passed
    straight through) is not compatible — no guess, no fallback to a
    nameless executable."""
    assert cli_source.system_python3_compatible("") is False


def test_probe_rejects_a_missing_executable(tmp_path):
    """An unresolvable interpreter path (OSError) is not compatible."""
    assert cli_source.system_python3_compatible(
        str(tmp_path / "no-such-python"),
    ) is False


def test_probe_rejects_a_hanging_interpreter(tmp_path, monkeypatch):
    """A wedged interpreter must not wedge the selection: the probe
    carries a timeout (a blocking command never runs bare)."""
    monkeypatch.setattr(cli_source, "PROBE_TIMEOUT_SECONDS", 1)
    assert cli_source.system_python3_compatible(
        _fake_python3(tmp_path, "sleep 30"),
    ) is False


def test_probe_code_pins_the_packaging_floor():
    """The probe source is built from REQUIRED_PYTHON — the same floor
    the packaging gate pins to pyproject's requires-python (one
    version floor everywhere, no second constant)."""
    assert f">= {cli_source.REQUIRED_PYTHON}" in cli_source.PYTHON_PROBE_CODE


def test_selection_uses_system_python3_when_compatible(
    tmp_path, monkeypatch,
):
    """A compatible system `python3` (Fedora 43, current Arch) is used
    DIRECTLY — no uv-managed download on a distro that already ships
    the floor."""
    monkeypatch.setattr(
        cli_source.shutil, "which",
        lambda name: _fake_python3(tmp_path, "exit 0"),
    )
    assert cli_source.select_python_interpreter() == "python3"


def test_selection_provisions_3_14_when_system_python3_is_old(
    tmp_path, monkeypatch,
):
    """The Ubuntu 24.04 scene: the system `python3` exists but is
    below the floor — the selection returns `3.14` so uv provisions
    or selects a compatible interpreter."""
    monkeypatch.setattr(
        cli_source.shutil, "which",
        lambda name: _fake_python3(tmp_path, "exit 1"),
    )
    assert cli_source.select_python_interpreter() == "3.14"


def test_selection_provisions_3_14_without_a_system_python3(monkeypatch):
    """A fresh uv-only host (no `python3` on PATH at all) keeps the
    pre-#861 behavior: uv provisions the interpreter."""
    monkeypatch.setattr(cli_source.shutil, "which", lambda name: None)
    assert cli_source.select_python_interpreter() == "3.14"


def test_python_interpreter_constant_follows_the_selection():
    """The module constant the reinstall argv (and every fix string)
    embeds is THE selection result — one rule, no divergent copy."""
    assert (
        cli_source.PYTHON_INTERPRETER
        == cli_source.select_python_interpreter()
    )
