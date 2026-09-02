"""Issue #168: the src-layout migration regression tests.

The #158 incident class: the uv editable finder's module mapping was
generated at install time from the hand-maintained ``pyproject.toml``
``py-modules`` list, so a newly added runtime module was invisible to
the installed finder until a reinstall. Since the src layout the
finder maps the WHOLE package directory ``src/muyan_pilot/`` — these
tests pin the acceptance against REAL installs (``uv venv`` +
``uv pip install --editable`` in a throwaway venv, never the machine's
tool env):

- a newly added package module is importable by the NEXT process with
  NO reinstall (the root-cause fix);
- the checkout root cannot shadow the installed package (no flat
  ``muyan_pilot.py`` at the root; the import source resolves to the
  checkout's ``src/`` package from a neutral cwd);
- the direct-execution entry ``python3 -m muyan_pilot.cli`` runs the
  exact console-script code and FAILS FAST (no fallback) when the
  package is not importable.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = "src/muyan_pilot"

UV = shutil.which("uv")


pytestmark = pytest.mark.skipif(
    UV is None, reason="uv is not installed on this machine",
)


def _run(command, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=120, **kwargs,
    )


def _editable_checkout(tmp_path: Path) -> Path:
    """A throwaway checkout carrying only the packaging inputs
    (pyproject.toml, the README it references and the package itself)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copytree(REPO_ROOT / "src", checkout / "src")
    shutil.copy2(REPO_ROOT / "pyproject.toml", checkout / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "README.md", checkout / "README.md")
    return checkout


def _venv_python(tmp_path: Path, checkout: Path) -> Path:
    """One real editable install into a throwaway venv (never the
    machine's uv tool env)."""
    venv = tmp_path / "venv"
    result = _run([UV, "venv", str(venv), "--python", "/usr/bin/python3"])
    assert result.returncode == 0, (
        f"uv venv failed rc={result.returncode} "
        f"stderr={result.stderr.strip()}"
    )
    python = venv / "bin" / "python"
    result = _run([
        UV, "pip", "install", "--python", str(python),
        "--editable", str(checkout),
    ])
    assert result.returncode == 0, (
        f"editable install failed rc={result.returncode} "
        f"stderr={result.stderr.strip()}"
    )
    return python


def _import_probe(python: Path, code: str, cwd: str = "/") -> subprocess.CompletedProcess:
    """A FRESH interpreter process (the next-process semantics)."""
    return _run([str(python), "-c", code], cwd=cwd)


# --- the root-cause fix: new package module, no reinstall -------------------


def test_new_package_module_is_importable_without_reinstall(tmp_path):
    """Acceptance: install ONCE, then add a package module — the next
    fresh process imports it (the editable finder maps the whole
    package directory; the #158 stale-module-list scene is gone)."""
    checkout = _editable_checkout(tmp_path)
    python = _venv_python(tmp_path, checkout)

    # The installed import source is the checkout's package.
    probe = _import_probe(
        python, "import muyan_pilot; print(muyan_pilot.__file__)",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == str(
        (checkout / PACKAGE_DIR / "__init__.py").resolve(),
    )

    # Add a NEW package module AFTER the install. No reinstall.
    new_module = checkout / PACKAGE_DIR / "issue168_sentinel.py"
    new_module.write_text(
        'SENTINEL = "issue-168-new-module"\n', encoding="utf-8",
    )
    probe = _import_probe(
        python,
        "from muyan_pilot.issue168_sentinel import SENTINEL; "
        "print(SENTINEL)",
    )
    assert probe.returncode == 0, (
        "a newly added package module must be importable by the next "
        f"process WITHOUT reinstall: {probe.stderr.strip()}"
    )
    assert probe.stdout.strip() == "issue-168-new-module"


def test_console_script_works_from_the_editable_install(tmp_path):
    """Acceptance: the formal editable install produces a working
    `muyan-pilot` console script (`muyan_pilot.cli:main`), asserted
    against one real call — the same entry the systemd ExecStart
    uses."""
    checkout = _editable_checkout(tmp_path)
    python = _venv_python(tmp_path, checkout)
    cli = python.parent / "muyan-pilot"
    assert cli.is_file(), "the editable install must create the console script"
    result = _run([str(cli), "--help"])
    assert result.returncode == 0, result.stderr
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in result.stdout
    result = _run([str(cli), "--version"])
    assert result.returncode == 0, result.stderr
    assert "muyan-pilot 0.2.0" in result.stdout


# --- the checkout root cannot shadow the installed package ------------------


def test_checkout_root_cannot_shadow_the_installed_package(tmp_path):
    """Acceptance: with the editable install in place, a fresh process
    from a NEUTRAL cwd imports the package from the checkout's
    ``src/`` — and the real checkout root carries no flat
    ``muyan_pilot.py`` that could shadow the package for processes
    with the checkout root on sys.path."""
    checkout = _editable_checkout(tmp_path)
    python = _venv_python(tmp_path, checkout)

    probe = _import_probe(
        python, "import muyan_pilot; print(muyan_pilot.__file__)",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().startswith(
        str(checkout / PACKAGE_DIR),
    ), (
        "the import source must be the checkout's src package, got: "
        f"{probe.stdout.strip()!r}"
    )

    assert not (REPO_ROOT / "muyan_pilot.py").is_file(), (
        "a flat muyan_pilot.py at the checkout root would shadow the "
        "installed package for every process with the checkout root on "
        "sys.path (Issue #168)"
    )


# --- the direct-execution entry: same code, fail fast -----------------------


def test_direct_execution_entry_runs_the_console_script_code(tmp_path):
    """`python3 -m muyan_pilot.cli --help` (the direct-execution
    compatibility entry) is the exact console-script code: it exposes
    the same subcommands (one real call)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = _run(
        [sys.executable, "-m", "muyan_pilot.cli", "--help"],
        cwd=str(REPO_ROOT), env=env,
    )
    assert result.returncode == 0, result.stderr
    for command in ("add", "status", "session", "install-units",
                    "doctor", "setup"):
        assert command in result.stdout


def test_direct_execution_entry_fails_fast_without_the_package(tmp_path):
    """Boundary: without the package on the import path the direct-
    execution entry FAILS FAST with the concrete ModuleNotFoundError —
    there is no fallback copy of the code anywhere."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = _run(
        [sys.executable, "-m", "muyan_pilot.cli", "--help"],
        cwd="/", env=env,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
