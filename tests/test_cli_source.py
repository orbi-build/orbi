"""CLI source consistency tests (Issue #152).

The official local deployment is the EDITABLE uv tool install: the tool
env's Python imports `muyan_pilot` directly from the deployment checkout
(a setuptools editable finder), so the ExecStartPre checkout sync is
picked up by the NEXT CLI process automatically — there is no second
copy of the source in site-packages and no per-version reinstall.

These tests pin:

- the exact editable force-reinstall command (verified against the real
  `uv tool install --help`: `--force`, `--reinstall`, `--editable` and
  `--python` all exist);
- the read-only source check: the running process's `muyan_pilot`
  import file must sit directly inside the configured `repo_dir`
  (a non-editable site-packages source or a stale other-checkout
  source is drift);
- the structured `cli_source_drift` line (actual path, expected
  repo_dir, the exact reinstall command) and its absence when clean.
"""
from pathlib import Path
from types import ModuleType

import pytest

from muyan_pilot import cli_source
from muyan_pilot.pi_activity import quote_value


def _fake_module_file(path: str) -> ModuleType:
    """A stand-in for the imported `muyan_pilot` module."""
    module = ModuleType("muyan_pilot")
    module.__file__ = path
    return module


def _patch_module_file(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        cli_source, "muyan_pilot", _fake_module_file(path),
    )


# --- the exact reinstall command -------------------------------------------


def test_reinstall_command_is_the_editable_force_reinstall():
    """The fix command is the editable force reinstall from the
    configured checkout (the Issue's recovery command, verbatim):
    `--force` replaces the existing tool env, `--reinstall` bypasses
    the build cache, `--editable` points the tool env at the checkout,
    `--python` pins the production interpreter."""
    command = cli_source.reinstall_command(
        Path("/home/xqianliu/Documents/muyan/muyan-pilot"),
    )
    assert command == (
        "uv tool install --force --reinstall --editable "
        "--python /usr/bin/python3 "
        "/home/xqianliu/Documents/muyan/muyan-pilot"
    )


# --- the read-only source check ---------------------------------------------


def test_cli_source_clean_for_the_checkout_source(tmp_path, monkeypatch):
    """An editable install (or the compat entry run inside the
    checkout) imports `muyan_pilot` from the checkout root: clean."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _patch_module_file(monkeypatch, str(repo / "src" / "muyan_pilot" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert source["editable"] is True
    assert source["actual"] == (repo / "src" / "muyan_pilot" / "__init__.py").resolve()
    assert source["expected"] == repo.resolve()
    assert source["fix"] == cli_source.reinstall_command(repo)


def test_cli_source_clean_when_the_expected_dir_is_a_symlink(
    tmp_path, monkeypatch,
):
    """The comparison resolves both sides: a symlinked checkout path
    (e.g. `/home/xqianliu/Documents/muyan/muyan-pilot` reached through
    a link) is the same source as the resolved one."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _patch_module_file(monkeypatch, str(real / "src" / "muyan_pilot" / "__init__.py"))
    source = cli_source.cli_source(link)
    assert source["editable"] is True
    assert source["actual"] == (real / "src" / "muyan_pilot" / "__init__.py").resolve()


def test_cli_source_drifts_for_a_site_packages_source(tmp_path, monkeypatch):
    """A NON-EDITABLE uv tool install copies the source into the tool
    env's site-packages: the import source is outside the checkout, so
    the ExecStartPre sync can never reach it (the #152 deadlock)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    site_packages = (
        tmp_path / "uv" / "tools" / "muyan-pilot"
        / "lib" / "python3.14" / "site-packages"
    )
    site_packages.mkdir(parents=True)
    _patch_module_file(
        monkeypatch, str(site_packages / "muyan_pilot" / "__init__.py"),
    )
    source = cli_source.cli_source(repo)
    assert source["editable"] is False
    assert source["actual"] == (site_packages / "muyan_pilot" / "__init__.py").resolve()
    assert source["fix"] == cli_source.reinstall_command(repo)


def test_cli_source_drifts_for_a_stale_other_checkout(tmp_path, monkeypatch):
    """An editable install of a DIFFERENT (stale) checkout also drifts:
    the import source is not inside the configured repo_dir."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    stale = tmp_path / "old-clone"
    stale.mkdir()
    _patch_module_file(monkeypatch, str(stale / "src" / "muyan_pilot" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert source["editable"] is False
    assert source["actual"] == (stale / "src" / "muyan_pilot" / "__init__.py").resolve()


def test_cli_source_drifts_for_a_nested_copy(tmp_path, monkeypatch):
    """A copy of the source nested INSIDE the checkout (e.g. a
    worktree's own `muyan_pilot.py` shadowing the checkout root file)
    is not the configured source either: only the checkout ROOT
    `muyan_pilot.py` counts."""
    repo = tmp_path / "checkout"
    (repo / ".worktrees" / "wt").mkdir(parents=True)
    _patch_module_file(
        monkeypatch, str(repo / ".worktrees" / "wt" / "src" / "muyan_pilot" / "__init__.py"),
    )
    source = cli_source.cli_source(repo)
    assert source["editable"] is False


# --- the structured drift line ----------------------------------------------


def test_drift_line_carries_source_expected_and_fix(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    stale = tmp_path / "old-clone"
    stale.mkdir()
    _patch_module_file(monkeypatch, str(stale / "src" / "muyan_pilot" / "__init__.py"))
    source = cli_source.cli_source(repo)
    line = cli_source.drift_line(source)
    assert line is not None
    assert line.startswith("cli_source_drift ")
    assert f"source={quote_value(str(source['actual']))}" in line
    assert f"expected={quote_value(str(repo.resolve()))}" in line
    # The fix command carries spaces, so the field is quoted (the
    # pi_activity.quote_value convention, like every unit_drift line).
    assert (
        f"fix={quote_value(cli_source.reinstall_command(repo))}"
    ) in line


def test_drift_line_is_none_when_clean(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _patch_module_file(monkeypatch, str(repo / "src" / "muyan_pilot" / "__init__.py"))
    source = cli_source.cli_source(repo)
    assert cli_source.drift_line(source) is None


# --- the real module file ----------------------------------------------------


def test_module_file_is_the_running_muyan_pilot_file():
    """`module_file()` is the running process's `muyan_pilot` import
    source (asserted against the real imported module — one real call,
    not a guessed shape)."""
    import muyan_pilot

    assert cli_source.module_file() == Path(
        muyan_pilot.__file__,
    ).resolve()


def test_module_file_fails_fast_without_a_file_attribute(monkeypatch):
    """A module without `__file__` cannot be located: the check fails
    fast with the concrete reason (never a guessed path)."""
    module = ModuleType("muyan_pilot")
    monkeypatch.setattr(cli_source, "muyan_pilot", module)

    with pytest.raises(RuntimeError, match="no __file__"):
        cli_source.module_file()
