"""Fakes for the runner-fingerprint probe tests (Issue #1229/#1826).

The probe lives in its own leaf module (`orbi.runner_marker`), and the
patch ratchet (Issue #789) keeps new tests from coupling to package
module internals through `monkeypatch.setattr(<module>, ...)`; this
directory is the sanctioned home for the coupling a test still needs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def use_package(monkeypatch, package: Path, *, module_file: str) -> None:
    """Pretend the package lives at `package/<module_file>`."""
    from orbi import runner_marker

    monkeypatch.setattr(runner_marker, "__file__", str(package / module_file))


def fake_git(monkeypatch, stdout: str) -> None:
    """Answer the `git rev-parse` probe with `stdout`."""
    from orbi import runner_marker

    monkeypatch.setattr(
        runner_marker.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=stdout, stderr="",
        ),
    )


def failing_git(monkeypatch, error: BaseException | None = None) -> None:
    """Make the `git rev-parse` probe raise (the probe degrades)."""
    from orbi import runner_marker

    def _raise(*args, **kwargs):
        raise error or RuntimeError("git unavailable")

    monkeypatch.setattr(runner_marker.subprocess, "run", _raise)


def fake_version(monkeypatch, seen: list[str], version: str = "0.3.4") -> None:
    """Answer the distribution-metadata probe and record the name read."""
    from orbi import runner_marker

    monkeypatch.setattr(
        runner_marker.metadata, "version",
        lambda name: seen.append(name) or version,
    )
