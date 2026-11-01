"""The one fake ``pi`` executable the e2e suites install (Issue #2009).

The runner starts ``pi`` as a real subprocess, so an end-to-end suite puts a
fake first on PATH. The helper was copied into four modules; the single copy
lives here so a behaviour fix lands once.
"""
import os
from pathlib import Path


def install_fake_pi(monkeypatch, tmp_path: Path, script: str) -> None:
    """Put a fake ``pi`` executable first on PATH for the runner's Popen."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "pi"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
