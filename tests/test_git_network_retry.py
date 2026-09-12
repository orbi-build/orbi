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


def test_git_network_command_does_not_retry_deterministic_git_failure(
    monkeypatch,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="Permission denied (publickey).",
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
