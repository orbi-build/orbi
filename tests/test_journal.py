"""Unit tests for the `orbi.journal` leaf (Issue #785).

`orbi.journal` is the Runner kernel the extracted leaf modules share: the
`orbi.bootstrap` logger with the `[run_id]` filter, the run-id binding, and
the single subprocess seam (`run_command` + its bounded git network retry).
The tests fake the seam with injected fakes or tiny real subprocesses —
they never patch `runner` internals.
"""
import io
import logging
import subprocess
import sys

import pytest

from orbi import journal
import orbi.journal as journal
from orbi.delivery_scene import RunContext


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


def test_run_command_check_false_returns_completed_process(caplog):
    result = journal.run_command(
        [sys.executable, "-c", "print('probe')"], check=False,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == "probe\n"


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
    journal.set_active_run(RunContext(run_id="a1b2c3d4", issue=785, branch="branch", worktree="/tmp/wt", source_repo="owner/repo"), "title")
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


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ("Bearer fabricated-token-123456", "Bearer "),
        ("ghp_" + "a" * 40, "ghp_"),
        ("sk-proj-" + "a" * 40, "sk-proj-"),
        ("sk-proj-" + "a" * 20 + "_" + "b" * 20, "sk-proj-"),
        ("sk-ant-api03-" + "a" * 40, "sk-ant-api03-"),
        ("sk-or-v1-" + "a" * 40, "sk-or-v1-"),
        ("gsk_" + "a" * 40, "gsk_"),
        ("AIza" + "a" * 35, "AIza"),
        ("github_pat_" + "a" * 40, "github_pat_"),
        ("xoxb-" + "a" * 30, "xoxb-"),
        ("AKIA" + "A" * 16, "AKIA"),
        ("Authorization: fabricated", "Authorization: "),
        ("Authorization:fabricated", "Authorization:"),
        ("Authorization:Bearer fabricated", "Authorization:Bearer "),
        ("x-api-key: fabricated", "x-api-key: "),
        ("x-api-key:fabricated", "x-api-key:"),
        ("api-key: fabricated", "api-key: "),
        ("api-key:fabricated", "api-key:"),
    ],
)
def test_redact_secrets_covers_current_formats(value, prefix):
    redacted = journal.redact_secrets(value)
    assert redacted == prefix + "<redacted>"
    assert value not in redacted


def test_redact_secrets_leaves_identifiers_and_scene_markers_unchanged():
    scene = '<!-- orbi:scene:v1 {"schema": 1} -->'
    values = (
        "a" * 40,
        "a1b2c3d4",
        "123e4567-e89b-12d3-a456-426614174000",
        scene,
        "<!-- orbi:fail=0123456789abcdef -->",
    )
    assert all(journal.redact_secrets(value) == value for value in values)


def test_configured_logging_redacts_messages_and_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    original = {item: item.formatter for item in root.handlers}
    original_level = root.level
    root.addHandler(handler)
    journal.configure_logging()
    logger = logging.getLogger("orbi.bootstrap")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("key=%s", "sk-ant-api03-" + "a" * 40)
        try:
            raise RuntimeError("gsk_" + "b" * 40)
        except RuntimeError:
            logger.exception("failed")
    finally:
        logger.setLevel(previous_level)
        root.removeHandler(handler)
        root.setLevel(original_level)
        for item, formatter in original.items():
            item.setFormatter(formatter)
    output = stream.getvalue()
    assert "<redacted>" in output
    assert "sk-ant-api03-" in output
    assert "gsk_" in output
    assert "a" * 40 not in output
    assert "b" * 40 not in output


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
