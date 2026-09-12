"""Unit tests for the `orbi.journal` leaf (Issue #785).

`orbi.journal` is the Runner kernel the extracted leaf modules share: the
`orbi.bootstrap` logger with the `[run_id]` filter, the run-id binding, and
the single subprocess seam (`run_command` + its bounded git network retry).
The tests fake the seam with injected fakes or tiny real subprocesses —
they never patch `runner` internals.
"""
import logging
import subprocess
import sys

import pytest

from orbi import journal
import orbi.journal as journal


@pytest.fixture(autouse=True)
def _reset_journal_state(monkeypatch):
    """Isolate the module-level run binding between tests."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)


def test_run_command_returns_stdout_and_logs_the_command(caplog):
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        out = journal.run_command(
            [sys.executable, "-c", "print('out' + '_42')"],
        )
    assert out == "out_42"
    assert "command=" in caplog.text
    # stdout is not logged unless log_stdout is requested.
    assert "out_42" not in caplog.text


def test_run_command_fail_fast_logs_and_raises(caplog):
    with caplog.at_level(logging.ERROR, logger="orbi.bootstrap"):
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            journal.run_command(
                [sys.executable, "-c",
                 "import sys; print('boom'); sys.exit(3)"],
            )
    assert excinfo.value.returncode == 3
    assert "command_failed" in caplog.text
    assert "returncode=3" in caplog.text
    assert "boom" in caplog.text


def test_run_command_timeout_is_logged_and_raised(caplog):
    with caplog.at_level(logging.ERROR, logger="orbi.bootstrap"):
        with pytest.raises(subprocess.TimeoutExpired):
            journal.run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=1,
            )
    assert "command_timeout" in caplog.text


def test_run_id_filter_prefixes_journal_lines_only_while_bound(caplog):
    logger = logging.getLogger("orbi.bootstrap")
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        logger.info("unbound line")
        journal.set_run_id("a1b2c3d4")
        logger.info("bound line")
    assert "unbound line" in caplog.text
    assert "[a1b2c3d4] bound line" in caplog.text
    assert journal.current_run_id() == "a1b2c3d4"


def test_set_run_id_rejects_an_invalid_id():
    with pytest.raises(ValueError, match="invalid run id"):
        journal.set_run_id("not-a-run-id")


def test_new_run_id_is_eight_hex_characters():
    value = journal.new_run_id()
    assert len(value) == 8
    int(value, 16)


def test_issue_context_formats_the_journal_reference():
    assert journal.issue_context("orbi-build/orbi", 785) == "orbi-build/orbi#785"


def test_validate_run_id_accepts_only_eight_hex_characters():
    assert journal.validate_run_id("a1b2c3d4") == "a1b2c3d4"
    with pytest.raises(ValueError, match="invalid run id"):
        journal.validate_run_id("zzzzzzzz")
    with pytest.raises(ValueError, match="invalid run id"):
        journal.validate_run_id(None)


def test_active_run_scene_binds_and_clears():
    journal.set_active_run(785, "title", "branch", "/tmp/wt")
    scene = journal.active_run()
    assert scene == {
        "issue": 785, "title": "title", "branch": "branch",
        "worktree": "/tmp/wt", "pi": None,
    }
    process = object()
    journal.set_active_pi(process)
    assert journal.active_run()["pi"] is process
    journal.set_active_pi(None)
    assert journal.active_run()["pi"] is None
    journal.clear_active_run()
    assert journal.active_run() is None


def test_set_active_pi_without_a_bound_run_is_a_no_op():
    journal.clear_active_run()
    journal.set_active_pi(object())  # must not raise
    assert journal.active_run() is None


def test_single_line_flattens_line_breaks():
    assert journal.single_line("a\r\nb\nc\rd") == "a\\nb\\nc\\nd"


def test_log_format_has_no_timestamp():
    assert journal.log_format() == "%(levelname)s %(message)s"


def test_git_network_command_retries_transient_failure_then_succeeds(
    monkeypatch, caplog,
):
    calls, sleeps = [], []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                128, command, stderr="fatal: unable to access "
                "'https://example.com': connection timed out",
            )
        return "ok"

    monkeypatch.setattr(journal.time, "sleep", sleeps.append)
    with caplog.at_level(logging.WARNING, logger="orbi.bootstrap"):
        assert journal.run_git_network_command(
            ["git", "fetch", "origin", "main"],
            command_runner=fake_run,
        ) == "ok"
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert "git_network_retry" in caplog.text


def test_git_network_command_never_retries_a_deterministic_failure(caplog):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="fatal: not a git repository",
        )

    with caplog.at_level(logging.WARNING, logger="orbi.bootstrap"):
        with pytest.raises(subprocess.CalledProcessError):
            journal.run_git_network_command(
                ["git", "fetch", "origin", "main"],
                command_runner=fake_run,
            )
    assert len(calls) == 1  # no transient marker -> no retry


def test_git_network_command_gives_up_after_max_attempts(monkeypatch):
    calls, sleeps = [], []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            128, command, stderr="connection reset",
        )

    monkeypatch.setattr(journal.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError):
        journal.run_git_network_command(
            ["git", "fetch", "origin", "main"],
            command_runner=fake_run,
        )
    assert len(calls) == 3
    assert sleeps == [1, 2]
