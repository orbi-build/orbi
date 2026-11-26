"""The git/gh seam for Issue #2440 (a PR GitHub never refreshed).

The fakes live here, in the adapter-contract package, because the merge
gate's reads and the refresh request ARE a command contract: the tests
assert the argv the runner emits, and the patch/shape ratchet of Issue #789
keeps that kind of expectation out of the test modules.
"""
from __future__ import annotations

import json
import subprocess

from tests.fakes.github import install_run_command


def refresh_scene(monkeypatch, tip=None, *, refresh_error=None):
    """Install the branch-tip read and the refresh request of one tick.

    Returns (the fake, the recorded commands). The kwargs of every call are
    on `fake.kwargs_seen` (the request's bounded timeout is asserted from
    there). The patch lives here, not in the test (patch ratchet, Issue #789).
    """
    calls: list[list[str]] = []
    kwargs_seen: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        kwargs_seen.append(dict(kwargs))
        if command[0] == "git":
            if tip is None:
                raise subprocess.CalledProcessError(
                    128, command, output="", stderr="fatal: unreachable",
                )
            return f"{tip}\t{command[-1]}\n"
        if refresh_error is not None:
            raise refresh_error
        return json.dumps({"message": "Updating pull request branch."})
    install_run_command(monkeypatch, fake_run)
    fake_run.kwargs_seen = kwargs_seen
    return fake_run, calls


def gate_scene(monkeypatch, states, *, tip):
    """Install the merge gate's reads for a view that lags the branch tip.

    states are the gh pr view payloads in read order (the gate re-reads a
    CI-concluded UNKNOWN mergeability in-tick), tip the branch head the
    remote reports. The in-tick retry delays are frozen to zero so the test
    spends no slot time. Returns (the fake, the recorded commands).
    """
    views = iter(states)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps(next(views))
        if command[:3] == ["git", "ls-remote", "--heads"]:
            return f"{tip}\t{command[-1]}\n"
        if command[:2] == ["gh", "api"]:
            return json.dumps({"message": "Updating pull request branch."})
        return ""
    install_run_command(monkeypatch, fake_run)
    # The gate's own bounded mergeability re-read (Issue #1736).
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return fake_run, calls


def refresh_requests(calls) -> list[list[str]]:
    """The refresh requests (update-branch) among the recorded commands."""
    return [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and "update-branch" in " ".join(command)
    ]
