import os
import subprocess

import pytest

import orbi.runner as runner
import orbi.journal as journal


# Stand-in for git: a C-locale git prints the English network message,
# a translated one does not, and the fake reproduces exactly that split so
# the locale plumbing itself is what the test exercises (Issue #1758).
_FAKE_GIT_SCRIPT = '''#!/usr/bin/env python3
import os
import sys

if os.environ.get("LC_ALL") == "C":
    print("fatal: unable to access 'https://github.com/o/r.git/': "
          "Connection timed out", file=sys.stderr)
else:
    print("fatal: [%s] lianjie chaoshi" % os.environ.get("LC_ALL"),
          file=sys.stderr)
sys.exit(128)
'''


def _install_fake_git(tmp_path, monkeypatch) -> None:
    """Put a fake `git` first on PATH for one test."""
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    script = binary_dir / "git"
    script.write_text(_FAKE_GIT_SCRIPT)
    script.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}",
    )


def test_chinese_host_retries_a_transient_git_network_failure(
    tmp_path, monkeypatch, caplog,
):
    """Issue #1758 acceptance 2: the same retry as on an English host.

    The host exports a Chinese locale; only the child's `LC_ALL=C` makes git
    print the English message Orbi recognises. Without it the fake prints the
    translated sentence, no marker matches, and the failure falls through as
    "not retried" — the bug this ticket fixes.
    """
    _install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            journal.run_git_network_command(["git", "fetch", "origin", "main"])

    assert caplog.text.count("git_network_retry") == 2
    assert "attempt=2" in caplog.text and "attempt=3" in caplog.text
    # The real C-locale stderr stays available to the failure handling.
    assert "Connection timed out" in caught.value.stderr


def test_git_network_command_retries_transient_failure_then_succeeds(
    monkeypatch, caplog,
):
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                128, command, stderr="ssh: connect to host github.com port 22: Connection timed out",
            )
        return "ok"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with caplog.at_level("INFO"):
        assert runner.run_git_network_command(
            ["git", "push", "origin", "HEAD:branch"],
            cwd="worktree", command_runner=fake_run,
        ) == "ok"

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert "git_network_retry" in caplog.text
    assert "attempt=2" in caplog.text


def test_git_network_command_retries_bounded_timeouts_then_succeeds(
    monkeypatch,
):
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return "ok"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    assert runner.run_git_network_command(
        ["git", "fetch", "origin", "main"], command_runner=fake_run,
    ) == "ok"
    assert len(calls) == 3
    assert all(
        call["timeout"] == journal.GIT_NETWORK_TIMEOUT_SECONDS
        for call in calls
    )
    assert sleeps == [1, 2]


def test_git_network_command_exhausts_transient_failures_and_preserves_stderr(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="fatal: unable to connect: Connection reset by peer",
        )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.run_git_network_command(
            ["git", "fetch", "origin", "main"], command_runner=fake_run,
        )

    assert len(calls) == 3
    assert caught.value.stderr == "fatal: unable to connect: Connection reset by peer"


def test_git_network_command_retries_transient_ssh_auth_fetch_then_succeeds(
    monkeypatch, caplog,
):
    calls = []
    sleeps = []
    stderr = (
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(128, command, stderr=stderr)
        return "ok"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with caplog.at_level("WARNING"):
        assert runner.run_git_network_command(
            ["git", "fetch", "origin", "main"], command_runner=fake_run,
        ) == "ok"

    assert len(calls) == 2
    assert sleeps == [1]
    assert caplog.text.count("git_network_retry") == 1


def test_git_network_command_exhausts_ssh_auth_failures_and_preserves_stderr(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    stderr = (
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    )

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(128, command, stderr=stderr)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.run_git_network_command(
            ["git", "push", "origin", "HEAD:branch"], command_runner=fake_run,
        )

    assert len(calls) == 3
    assert caught.value.stderr == stderr


def test_git_network_command_does_not_retry_ssh_auth_error_for_other_commands():
    command = ["git", "status"]
    error = subprocess.CalledProcessError(
        128, command,
        stderr="fatal: Could not read from remote repository.",
    )
    assert not journal._is_retryable_git_network_failure(command, error)


def test_git_network_command_does_not_retry_deterministic_git_failure(
    monkeypatch,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="fatal: not a git repository",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_git_network_command(
            ["git", "push", "origin", "HEAD:branch"], command_runner=fake_run,
        )
    assert len(calls) == 1


def test_git_network_command_does_not_retry_other_git_commands():
    command = ["git", "status"]
    error = subprocess.CalledProcessError(
        1, command, stderr="Connection timed out",
    )
    assert not journal._is_retryable_git_network_failure(command, error)


def test_git_network_command_does_not_retry_non_git_timeout(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_git_network_command(["gh", "pr", "create"], command_runner=fake_run)
    assert len(calls) == 1


def test_git_network_command_does_not_retry_non_git_commands(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="Connection timed out",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_git_network_command(["gh", "pr", "create"], command_runner=fake_run)
    assert len(calls) == 1
