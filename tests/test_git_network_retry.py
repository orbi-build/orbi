import subprocess

import pytest

import orbi.runner as runner
import orbi.journal as journal


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
        assert journal.run_git_network_command(
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
    assert journal.run_git_network_command(
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
        journal.run_git_network_command(
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
        assert journal.run_git_network_command(
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
        journal.run_git_network_command(
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
        journal.run_git_network_command(
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
        journal.run_git_network_command(["gh", "pr", "create"], command_runner=fake_run)
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
        journal.run_git_network_command(["gh", "pr", "create"], command_runner=fake_run)
    assert len(calls) == 1
