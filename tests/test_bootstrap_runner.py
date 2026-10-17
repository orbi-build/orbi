from orbi import config as config_domain
from orbi import language

# The session-boundary message-language instruction every implementer/
# review prompt carries (Issue #1660). Prompt-content assertions compare
# the rendered template PLUS this instruction, so a custom template
# cannot silently lose it.
LANGUAGE_SUFFIX = language.model_instruction("en")

import ast
import dataclasses
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.review_merge as review_merge
import orbi.run_state as run_state
import orbi.gitops as gitops
import orbi.pi_session as pi_session
import orbi.milestone as milestone
import orbi.release as release
import orbi.github as github
from orbi import failure, pi_command, pi_process, progress
from tests.fakes.bootstrap_pi import (
    fake_session_records, fresh_timestamp, make_fake_pi,
)
from tests.fakes.github import FakeGh
from tests.fakes.gitops import remote_head_answer
from tests.test_progress_wiring import make_fake_gh
import orbi.journal as journal
from seam import resume_deps, seam
from orbi.delivery_labels import EVENT_BLOCKED
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from conftest import rendered_prompt

# Hosted-macOS boundaries; the Ubuntu CI keeps every scene here
# authoritative. The unit-drift preflight scenes contract the
# systemd deployment over systemd-shaped fixtures.
_unit_drift_systemd = pytest.mark.usefixtures("systemd_scheduler")


def test_runner_main_config_failure_is_one_structured_log_line(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        config_domain, "load_config",
        lambda path: (_ for _ in ()).throw(
            ValueError("API key for provider 'ollama' references environment variable OLLAMA_API_KEY is not set")
        ),
    )
    with caplog.at_level("ERROR"):
        assert runner.main(["--config", "orbi.toml"]) == 1
    assert [record.message for record in caplog.records] == [
        'config_invalid reason="API key for provider \'ollama\' '
        'references environment variable OLLAMA_API_KEY is not set"'
    ]


def test_parse_issue_list_returns_first_issue():
    issue = {"number": 7, "title": "ship", "body": "do it"}
    assert runner.parse_issue_list(json.dumps([issue])) == issue


def test_parse_issue_list_returns_none_for_empty_list():
    assert runner.parse_issue_list("[]") is None


def test_parse_issue_list_rejects_non_list():
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.parse_issue_list("{}")


def test_list_issues_builds_each_parameter_combination(monkeypatch):
    """Issue #299: `list_issues` emits the exact `gh issue list` command
    for each parameter combination the call sites use (search / label +
    milestone / timeout) and returns the parsed array."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps([{"number": 1}])

    monkeypatch.setattr(seam, "run_command", fake_run)

    # state + search: the common ready/in-flight scan shape.
    assert runner.list_issues(
        "owner/repo", state="open", search="label:ai-ready",
        json_fields="number,title", limit=200,
    ) == [{"number": 1}]

    # label + state + milestone: the release gate shape (milestone last).
    assert runner.list_issues(
        "owner/repo", label="ai-in-progress", state="open",
        milestone="v0.4.3", json_fields="number", limit=50,
    ) == [{"number": 1}]

    # state + search + timeout: the idle-advance shape (timeout passed on).
    assert runner.list_issues(
        "owner/repo", state="all", search='in:body "marker"',
        json_fields="number", limit=1, timeout=30,
    ) == [{"number": 1}]

    # search only (state omitted): gh defaults the state to open.
    assert runner.list_issues(
        "owner/repo", search="label:ai-ready",
        json_fields="number", limit=1,
    ) == [{"number": 1}]

    assert calls == [
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--state", "open", "--search", "label:ai-ready",
            "--json", "number,title", "--limit", "200",
        ], {}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--label", "ai-in-progress", "--state", "open",
            "--json", "number", "--limit", "50", "--milestone", "v0.4.3",
        ], {}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--state", "all", "--search", 'in:body "marker"',
            "--json", "number", "--limit", "1",
        ], {"timeout": 30}),
        ([
            "gh", "issue", "list", "--repo", "owner/repo",
            "--search", "label:ai-ready",
            "--json", "number", "--limit", "1",
        ], {}),
    ]


def test_render_prompt_replaces_context_values():
    rendered = pi_session.render_prompt(
        "{{SOURCE_REPO}} #{{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{CONTEXT_FILES}}",
        {
            "SOURCE_REPO": "owner/repo",
            "ISSUE_NUMBER": "3",
            "ISSUE_TITLE": "Fix",
            "CONTEXT_FILES": "context.md",
        },
    )
    assert rendered == "owner/repo #3 Fix context.md"


def test_prompt_requires_verifying_external_behavior_against_docs():
    """Issue #73: the implementer prompt must make it a hard rule to
    verify external behavior (APIs, CLIs, config, HTTP paths) against
    official docs or `--help` before writing it — no assembling from
    memory — and test assertions must assert the real contract, not
    the implementation's guessed shape. It must also state that
    bypass failures (progress, notifications) only log and never
    decide the delivery outcome."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "--help" in template
    assert "docs" in template.lower()
    # The bypass rule is explicit.
    assert "bypass" in template.lower()


def test_review_prompt_flags_unverified_external_behavior():
    """Issue #73: the review prompt must check that external behavior
    (paths, parameters, status codes) was verified against docs or a
    real call, and that a bypass failure cannot break the main
    delivery — the #57 guessed-PATCH-route class of bug must be
    caught by review, not shipped."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt_review.md"
    ).read_text(encoding="utf-8")
    assert "verify" in template.lower()
    assert "bypass" in template.lower()


def test_prompt_does_not_require_review_fix_loop_before_pr():
    """Issue #78: the implementer only does plan -> implement -> test
    -> push PR. The independent review happens AFTER the PR is open
    (the Runner runs it); the prompt must not demand a complete
    review-fix loop before opening the PR — the old wording made the
    local Pi self-review for hours (#18/#34)."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "complete review-fix loop" not in template
    # The review is explicitly positioned AFTER the PR is open, and the
    # implementer never reviews/fixes/merges.
    assert "you do not review, fix, or merge" in template


def test_prompt_template_requires_fixes_keyword_for_the_source_issue():
    """Issue #53: the Pi prompt must carry `Fixes #<issue>` (the PR body
    contract the Runner fulfils when it opens the PR, Issue #186) so
    GitHub closes the source Issue natively on merge; the requirement
    renders with the real issue number."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "Fixes #{{ISSUE_NUMBER}}" in template
    rendered = pi_session.render_prompt(template, {
        "SOURCE_REPO": "xqliu/orbi",
        "SOURCE_REPOS": "xqliu/orbi",
        "ISSUE_NUMBER": "53",
        "ISSUE_TITLE": "t",
        "ISSUE_BODY": "b",
        # Issue #745: the shipped template injects the trusted-comment
        # timeline; the render must fill it to prove no placeholder is
        # left unresolved.
        "ISSUE_COMMENTS": "(no trusted comments)",
        "WORKSPACE_ROOT": "/tmp",
        "CONTEXT_FILES": "",
        "SKILLS": "",
        "BASE_BRANCH": "main",
        "BASE_SHA": "abc123",
        "RUN_ID": "a2241189",
        "BASE_SYNC_LOCK": "/checkout/.orbi/base-sync.lock",
    })
    assert "Fixes #53" in rendered
    # Issue #186: the implementer prompt no longer carries the base-sync
    # lock path — the base fetch is the Runner's operation.
    assert "/checkout/.orbi/base-sync.lock" not in rendered
    assert "{{" not in rendered


def test_prompt_templates_carry_the_issue_comments_placeholder():
    """Issue #745: the shipped implementer and review templates inject
    the trusted-comment timeline by default — decisions written in
    Issue comments reach the agent without a human copying them into
    the body."""
    prompts = Path(__file__).resolve().parent.parent / "prompts"
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt_review.md"
    ).read_text(encoding="utf-8")


def test_run_command_returns_stdout(monkeypatch, tmp_path):
    completed = Mock(stdout=" output \n", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    assert runner.run_command(["git", "status"], cwd=tmp_path) == "output"
    runner.subprocess.run.assert_called_once_with(
        ["git", "status"], cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=None,
    )


def test_run_command_logs_stderr(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="ok", stderr="warning\n")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["gh", "--version"], cwd=tmp_path) == "ok"
    assert "stderr=warning" in caplog.text


def test_run_command_can_log_success_stdout(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(return_value=Mock(stdout="agent output\n", stderr="")),
    )
    with caplog.at_level("INFO"):
        assert runner.run_command(["pi"], cwd=tmp_path, log_stdout=True) == "agent output"
    assert "stdout=agent output" in caplog.text


def test_run_command_logs_called_process_error_and_reraises(tmp_path, caplog):
    error = subprocess.CalledProcessError(
        2, ["gh", "issue", "list"], output="out", stderr="bad",
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
            runner.run_command(["gh", "issue", "list"], cwd=tmp_path)
    assert "command_failed returncode=2 stdout=out stderr=bad" in caplog.text


def test_run_command_logs_spawn_error_and_reraises(tmp_path, caplog):
    error = FileNotFoundError("pi")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(FileNotFoundError):
            runner.run_command(["pi"], cwd=tmp_path)
    assert "command_spawn_failed error=pi" in caplog.text


def test_run_command_logs_optional_timeout_and_reraises(tmp_path, caplog):
    error = subprocess.TimeoutExpired(["command"], 7, output="partial", stderr="wait")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner.subprocess, "run", Mock(side_effect=error))
        with caplog.at_level("ERROR"), pytest.raises(subprocess.TimeoutExpired):
            runner.run_command(["command"], cwd=tmp_path, timeout=7)
    assert "command_timeout timeout=7 stdout=partial stderr=wait" in caplog.text


def test_sync_active_milestone_variable_skips_matching_value():
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v1"}'

    milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert len(calls) == 1
    # The read may 404 by design (no variable yet): its generic
    # command_failed line must stay at DEBUG (Issue #1085).
    assert calls[0][1] == {"timeout": 30, "failure_log_level": logging.DEBUG}


def test_sync_active_milestone_variable_patches_different_value():
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v0"}' if len(calls) == 1 else "{}"

    milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert calls[1][:5] == ["gh", "api", "-X", "PATCH", "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE"]
    assert "value=v1" in calls[1]


def test_sync_active_milestone_variable_creates_missing_value(caplog, monkeypatch):
    """Read 404 + a configured milestone: the real run_command path issues
    the POST and the generic command_failed line from the 404 read stays
    below INFO (Issue #1085)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, cmd, output="",
                stderr="gh: Not Found (HTTP 404)",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG):
        milestone.sync_active_milestone_variable("owner/repo", "v1")
    assert calls[1][:4] == ["gh", "api", "-X", "POST"]
    assert "value=v1" in calls[1]
    failed = [
        record for record in caplog.records
        if "command_failed" in record.getMessage()
    ]
    assert failed and all(record.levelno == logging.DEBUG for record in failed)
    assert "active_milestone_variable_created repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_create_failure_is_bypassed(caplog):
    calls = 0

    def command(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 404")
        raise RuntimeError("create denied")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable(
            "owner/repo", "v1", run_command=command,
        )

    assert calls == 2
    assert "active_milestone_variable_sync_failed repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_removes_stale_value():
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v0"}' if len(calls) == 1 else "{}"

    milestone.sync_active_milestone_variable("owner/repo", None, run_command=command)
    assert calls[1] == [
        "gh", "api", "-X", "DELETE",
        "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE",
    ]


def test_sync_active_milestone_variable_absent_on_404(caplog, monkeypatch):
    """Removing an already-absent variable is a scene, not a failure: the
    read goes through the REAL run_command, its 404's generic
    command_failed line stays at DEBUG, and the structured `absent` line
    owns the outcome (Issue #1085)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        raise subprocess.CalledProcessError(
            1, cmd,
            output=('{"message":"Not Found",'
                    '"documentation_url":"https://docs.github.com/rest/'
                    'actions/variables#get-a-repository-variable",'
                    '"status":"404"}'),
            stderr="gh: Not Found (HTTP 404)",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG):
        milestone.sync_active_milestone_variable("owner/repo", None)
    assert calls == [
        ["gh", "api", "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE"],
    ]
    failed = [
        record for record in caplog.records
        if "command_failed" in record.getMessage()
    ]
    assert failed and all(record.levelno == logging.DEBUG for record in failed)
    assert "active_milestone_variable_absent repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_read_403_logs_sync_failed_at_error(caplog):
    def command(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable(
            "owner/repo", None, run_command=command,
        )
    failed = [
        record for record in caplog.records
        if "active_milestone_variable_sync_failed" in record.getMessage()
    ]
    assert failed
    assert all(record.levelno == logging.ERROR for record in failed)


def test_sync_active_milestone_variable_failure_is_bypass(caplog):
    def command(args, **kwargs):
        raise RuntimeError("offline")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert "active_milestone_variable_sync_failed" in caplog.text


def test_single_line_flattens_line_breaks_to_visible_escapes():
    assert runner.single_line("a\nb") == "a\\nb"
    assert runner.single_line("a\r\nb") == "a\\nb"
    assert runner.single_line("a\rb") == "a\\nb"
    assert runner.single_line("no breaks") == "no breaks"


def test_run_command_logs_multiline_body_as_one_journal_line(
        monkeypatch, tmp_path, caplog):
    # The progress comment body (progress.py) is multi-line; it reaches
    # the journal through the `command=` log of the gh api call.
    body = (
        "<!-- orbi:run=e6f5ec8a -->\n"
        "**Orbi progress**\n"
        "- run_id=e6f5ec8a\n"
        "- role: implement\n"
        "- priority: normal\n"
        "- phase: read\n"
        "- elapsed: 8m 2s"
    )
    command = [
        "gh", "api", "repos/xqliu/orbi/issues/143/comments",
        "--method", "POST", "--field", f"body={body}",
    ]
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    completed = Mock(stdout='{"id": 1}', stderr="")
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=completed))
    with caplog.at_level("INFO"):
        assert runner.run_command(command, cwd=tmp_path) == '{"id": 1}'
    # Exactly one command= record and it is a single journal line.
    command_records = [
        record for record in caplog.records
        if record.getMessage().startswith("command=")
    ]
    assert len(command_records) == 1
    message = command_records[0].getMessage()
    assert "\n" not in message
    # The body fields stay fully visible on that one line.
    for field in (
        "- run_id=e6f5ec8a", "- role: implement", "- priority: normal",
        "- phase: read", "- elapsed: 8m 2s",
    ):
        assert field in message
    # The real command (and thus the GitHub comment body) is unchanged.
    runner.subprocess.run.assert_called_once_with(
        command, cwd=tmp_path, capture_output=True,
        text=True, check=True, timeout=None,
    )


def test_pick_issue_uses_github_queue(monkeypatch):
    issue = {
        "number": 9, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi-backlog") == issue
    # Issue #101: the P0 scan runs first, then the bug scan (Issue
    # #71); with nothing in either queue the plain ready scan decides.
    # All three keep the same exclusions.
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready label:p0 -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready label:bug -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
            "-label:ai-fix-needed -label:ai-merged -label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
    ]


def test_open_blocker_numbers_returns_only_open_blockers():
    # GitHub keeps a relation listed after its blocker closes (the
    # node carries `state: "CLOSED"` and is inert — verified against
    # the live API, Issue #54): only OPEN blockers block.
    issue = {"blockedBy": {"nodes": [
        {"number": 31, "state": "OPEN", "title": "a"},
        {"number": 32, "state": "CLOSED", "title": "b"},
    ], "totalCount": 2}}
    assert runner.open_blocker_numbers(issue) == [31]


def test_open_blocker_numbers_counts_missing_state_as_open():
    # A node without an explicit state counts as open: claiming a
    # possibly-blocked Issue costs a full run, waiting one tick does
    # not (Issue #54).
    issue = {"blockedBy": {"nodes": [{"number": 31}], "totalCount": 1}}
    assert runner.open_blocker_numbers(issue) == [31]


def test_open_blocker_numbers_fails_open_on_missing_or_malformed_field():
    # A missing or malformed `blockedBy` field must never block the
    # queue (Issue #54 fail open): no known blockers, not an error.
    assert runner.open_blocker_numbers({"number": 9}) == []
    assert runner.open_blocker_numbers({"blockedBy": "nope"}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": "nope"}}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": ["nope"]}}) == []
    assert runner.open_blocker_numbers({"blockedBy": {"nodes": [
        {"number": 31, "state": "OPEN"},
        {"title": "no number", "state": "OPEN"},
        {"number": "32", "state": "OPEN"},
        {"number": True, "state": "OPEN"},
    ]}}) == [31]


def test_pick_issue_skips_blocked_issue_and_claims_next(monkeypatch, caplog):
    """A ready Issue with open native blockers (Issue #54) is never
    claimed: no label change, no worktree — the runner logs a
    structured `blocked_by` line with the blocker list and moves on to
    the next ready Issue of the same repo."""
    blocked = {
        "number": 54, "title": "blocked task", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "title": "base"},
            {"number": 32, "title": "retry"},
        ], "totalCount": 2},
    }
    ready = {
        "number": 55, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == ready
    assert "blocked_by" in caplog.text
    assert "54" in caplog.text
    assert "31" in caplog.text
    assert "32" in caplog.text


def test_pick_issue_returns_none_when_all_ready_issues_blocked(
    monkeypatch, caplog,
):
    blocked_a = {
        "number": 1, "title": "a", "body": "",
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    blocked_b = {
        "number": 2, "title": "b", "body": "",
        "blockedBy": {"nodes": [{"number": 8}], "totalCount": 1},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked_a, blocked_b]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert caplog.text.count("blocked_by") >= 2


def test_pick_issue_claims_issue_whose_blocker_is_closed(monkeypatch):
    """A closed blocker no longer blocks (Issue #54): GitHub keeps the
    relation listed with `state: "CLOSED"` (verified against the live
    API), and the runner counts only open blockers — the next tick
    claims the Issue without any runner-side bookkeeping."""
    issue = {
        "number": 54, "title": "unblocked task", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "state": "CLOSED", "title": "done"},
        ], "totalCount": 1},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_claims_issue_with_empty_blocked_by(monkeypatch):
    issue = {
        "number": 54, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_stays_blocked_while_any_blocker_is_open(monkeypatch):
    blocked = {
        "number": 54, "title": "mixed", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "state": "CLOSED", "title": "done"},
            {"number": 32, "state": "OPEN", "title": "pending"},
        ], "totalCount": 2},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([blocked]),
    )
    assert claim.pick_issue("xqliu/orbi") is None


def test_pick_issue_fails_open_when_blocked_by_field_missing(monkeypatch):
    # Older gh versions or a changed API shape omit the field: the
    # Issue must still be claimable (fail open, Issue #54).
    issue = {"number": 9, "title": "task", "body": "body"}
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert claim.pick_issue("xqliu/orbi") == issue


def test_pick_issue_fails_open_when_blocked_by_query_fails(
    monkeypatch, caplog,
):
    """A failed blockedBy query must not deadlock the queue (Issue
    #54 fail open): the tick claims nothing this round and the error
    is logged, never raised — the next tick retries the query."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="boom", stderr="rate limited",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    # Issue #738: the read now retries the transient stderr; skip the
    # real backoff sleeps like the git/gh retry tests do.
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


def test_validate_active_milestone_none_is_unchanged(monkeypatch):
    monkeypatch.setitem(runner.__dict__, "list_milestones", lambda *args, **kwargs: pytest.fail("no lookup"))
    milestone.validate_active_milestone("owner/repo", None)


def test_validate_active_milestone_missing_fails_with_repair_facts(monkeypatch, caplog):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "open"},
    ])
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="state=absent"):
        milestone.validate_active_milestone("owner/repo", "v9")
    assert "active_milestone_missing" in caplog.text
    assert "open_milestones=v1" in caplog.text


@pytest.mark.parametrize("ready_issues", [[], [{"number": 7}]])
def test_validate_active_milestone_closed_is_informational(
    monkeypatch, caplog, ready_issues,
):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "closed"},
    ])
    seen = {}

    def fake_list_issues(repo, **kwargs):
        seen["repo"] = repo
        seen.update(kwargs)
        return ready_issues

    monkeypatch.setitem(milestone.__dict__, "list_issues", fake_list_issues)
    with caplog.at_level(logging.INFO):
        milestone.validate_active_milestone("owner/repo", "v1")
    assert "active_milestone_closed" in caplog.text
    assert f"ai_ready_count={len(ready_issues)}" in caplog.text
    assert seen == {
        "repo": "owner/repo", "state": "open", "label": "ai-ready",
        "milestone": "v1", "json_fields": "number", "limit": 1000,
        "timeout": 30,
    }


def test_validate_active_milestone_open_is_valid(monkeypatch):
    monkeypatch.setitem(milestone.__dict__, "list_milestones", lambda *args, **kwargs: [
        {"title": "v1", "state": "open"},
    ])
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: pytest.fail("no ready scan"))
    milestone.validate_active_milestone("owner/repo", "v1")


def test_missing_effective_repository_milestone_fails_before_slot(
    monkeypatch, tmp_path,
):
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "host-v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        seam, "load_repo_policy",
        lambda loaded, repo: runner.RepoPolicy(active_milestone="repo-v2"),
    )
    monkeypatch.setitem(
        milestone.__dict__, "list_milestones",
        lambda repo, **kwargs: [{"title": "host-v1", "state": "open"}],
    )
    monkeypatch.setitem(
        runner.__dict__, "acquire_slot",
        lambda *args: pytest.fail("missing milestone must not take a slot"),
    )

    with pytest.raises(RuntimeError, match="configured='repo-v2'"):
        runner.main(["--config", str(config)])


def test_pick_issue_scopes_all_three_ready_scans_to_active_milestone(
    monkeypatch,
):
    """Issue #139: with `active_milestone = "v0.2.0"` the ready scans
    only see the active Milestone: every one of the three gh searches
    (p0, bug, plain) carries the `milestone:"v0.2.0"` qualifier (the
    quoted form is the contract — milestone titles may contain spaces
    or special characters; verified against the live API). A
    `v0.1.2 + ai-ready` Issue therefore never enters the queue of a
    `v0.2.0` Runner, and a `v0.2.0` Issue without `ai-ready` never
    enters it either (the `label:ai-ready` qualifier stays)."""
    issue = {
        "number": 139, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue(
        "xqliu/orbi-backlog", active_milestone="v0.2.0",
    ) == issue
    scope = ' milestone:"v0.2.0"'
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready label:p0{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready label:bug{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/orbi-backlog",
            "--state", "open", "--search",
            f"label:ai-ready{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy,author",
            "--limit", "200",
        ],
    ]


def test_pick_issue_p0_scan_keeps_the_milestone_scope():
    """Issue #139 (explicit decision): P0 does NOT cross milestones.
    The active milestone is the claim scope of every fresh claim —
    `p0` only orders the pickup inside the active milestone, so a P0
    sitting in an old milestone never enters the current version's
    queue (one uniform rule, no special case). The scan order itself
    is unchanged: the p0 scan is still the FIRST one."""
    p0_search, bug_search, plain_search = claim.ready_searches("v0.2.0")
    for search in (p0_search, bug_search, plain_search):
        assert 'milestone:"v0.2.0"' in search
    assert "label:p0" in p0_search
    assert "label:p0" not in bug_search
    assert "label:p0" not in plain_search


def test_ready_searches_without_milestone_are_the_compat_scans():
    """Issue #139 compat: without a configured Milestone the three
    ready scans are byte-identical to the pre-#139 scans — a config
    without `active_milestone` behaves exactly like before."""
    p0_search, bug_search, plain_search = claim.ready_searches(None)
    assert p0_search == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert bug_search == (
        "label:ai-ready label:bug -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert plain_search == (
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked"
    )
    # The default (no argument) is the same compat behavior.
    assert claim.ready_searches() == claim.ready_searches(None)


def test_pick_issue_keeps_epic_and_blocked_by_guards_with_milestone(
    monkeypatch, caplog,
):
    """Issue #139: the milestone scope never weakens the existing
    guards — an `ai-epic` Issue is still skipped by the code layer
    (the query results can still contain it), and an Issue with open
    native blockers is still skipped, inside the active Milestone."""
    epic = {
        "number": 141, "title": "epic", "body": "body",
        "labels": [{"name": "ai-ready"}, {"name": "ai-epic"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    blocked = {
        "number": 142, "title": "blocked", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    ready = {
        "number": 143, "title": "free", "body": "body",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic, blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.2.0",
        ) == ready
    assert "epic_not_claimed" in caplog.text
    assert "blocked_by" in caplog.text


def test_pick_issue_fails_open_when_milestone_query_fails(
    monkeypatch, caplog,
):
    """Issue #139: a failed ready scan with the milestone qualifier
    follows the existing fail-open contract (Issue #54) — the tick
    claims nothing (never the wrong version), logs the structured
    error, and the next tick retries."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="boom", stderr="rate limited",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    # Issue #738: the read now retries the transient stderr; skip the
    # real backoff sleeps like the git/gh retry tests do.
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.2.0",
        ) is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_prefers_bug_labeled_issues(monkeypatch):
    """Issue #71: when an `ai-ready`+`bug` Issue and a plain `ai-ready`
    Issue exist at the same time, the runner claims the bug first —
    if the delivery loop is broken, claiming enhancements only piles
    up unreviewed PRs. No priority numbers, no new state: the bug
    scan runs (after the P0 scan, Issue #101) with the same
    exclusions, and the plain ready scan only runs when the bug scan
    found nothing claimable."""
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:bug" in searches[-1]:
            return json.dumps([bug])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == bug
    # The P0 scan ran first and found nothing; the bug scan found the
    # bug: the plain ready scan never ran.
    assert len(searches) == 2
    assert "label:p0" in searches[0]
    assert "label:bug" in searches[1]
    assert "label:ai-ready" in searches[1]


def test_pick_issue_bug_scan_keeps_existing_exclusions(monkeypatch):
    """Issue #71: the bug scan keeps the exact same exclusions as the
    ready scan (in-flight, opened-PR, fix-needed, merged, blocked),
    and runs after the P0 scan (Issue #101)."""
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:bug" in searches[-1]:
            return json.dumps([bug])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == bug
    assert searches[0] == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )
    assert searches[1] == (
        "label:ai-ready label:bug -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )


def test_pick_issue_falls_back_to_ready_scan_when_no_bug(monkeypatch):
    """Issue #71: with no claimable P0 or bug, behavior is exactly as
    before (the plain ready scan runs last and decides)."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1] or "label:bug" in searches[-1]:
            return json.dumps([])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3
    assert "label:p0" in searches[0]
    assert "label:bug" in searches[1]
    assert "label:bug" not in searches[2]
    assert "label:p0" not in searches[2]


def test_pick_issue_bug_blocked_by_open_blocker_falls_back(monkeypatch):
    """Issue #71: a bug with open native blockers is skipped by the bug
    scan (same blockedBy semantics as the ready scan); the plain ready
    scan then decides."""
    blocked_bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1]:
            return json.dumps([])
        if "label:bug" in searches[-1]:
            return json.dumps([blocked_bug])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3


def test_pick_issue_bug_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #71: a failed bug scan fails open like the ready scan
    (Issue #54) — the tick claims nothing from this repo, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


# --- Issue #101: P0 priority ---------------------------------------------------


def test_pick_issue_prefers_p0_over_bug_and_plain(monkeypatch, caplog):
    """Issue #101: when an `ai-ready`+`p0` Issue, an `ai-ready`+`bug`
    Issue and a plain `ai-ready` Issue exist at the same time, the
    runner claims the P0 first — the P0 scan runs first with the exact
    same exclusions, and the bug/plain scans only run when the P0 scan
    found nothing claimable. The journal line carries `priority=p0`."""
    p0 = {
        "number": 7, "title": "p0 outage", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # All three Issues exist in the repo; the P0 scan (the only
        # scan that runs) sees them all and must pick the P0.
        return json.dumps([p0, bug, feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == p0
    # The P0 scan ran first and found the P0: the bug and plain ready
    # scans never ran.
    assert len(searches) == 1
    assert "label:p0" in searches[0]
    assert "label:ai-ready" in searches[0]
    # The pickup log carries the explicit priority field.
    assert "priority=p0" in caplog.text
    assert "issue=7" in caplog.text


def test_pick_issue_p0_scan_keeps_existing_exclusions(monkeypatch):
    """Issue #101: the P0 scan keeps the exact same exclusions as the
    bug and ready scans (in-flight, opened-PR, fix-needed, merged,
    blocked) — a P0 in any delivery state is never claimed by the
    ready scan (single-slot and resume semantics are unchanged)."""
    p0 = {
        "number": 7, "title": "p0", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # Only the P0 scan runs (it finds the P0): it must carry the
        # exact exclusions.
        return json.dumps([p0])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == p0
    assert searches[0] == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )


# --- Issue #255: release fallback scan ------------------------------------


def test_release_fallback_search_query():
    """Issue #255: the release fallback scan keeps `label:ai-ready`
    (the human execution switch) plus `label:ai-release` and the same
    five delivery-state exclusions as the ordinary scans. Without a
    configured Milestone it is the plain query; with one it carries the
    same quoted `milestone:"<title>"` scope."""
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    assert claim.release_fallback_search(None) == (
        f"label:ai-ready label:ai-release {exclusions}"
    )
    assert claim.release_fallback_search() == (
        f"label:ai-ready label:ai-release {exclusions}"
    )
    assert claim.release_fallback_search("v0.3.0") == (
        f'label:ai-ready label:ai-release milestone:"v0.3.0" {exclusions}'
    )


def test_release_issue_not_claimed_when_ordinary_delivery_exists(
    monkeypatch, caplog,
):
    """Issue #255: with an ordinary delivery ready (here in the plain
    scan), the release Issue is skipped there (`release_not_claimed`) —
    the ordinary delivery is claimed first and the release fallback
    scan never runs (only three gh searches)."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:p0" in searches[-1] or "label:bug" in searches[-1]:
            return json.dumps([])
        # The plain scan sees both (release listed first); the release
        # is skipped and the ordinary delivery wins. The release
        # fallback scan never runs (only three searches).
        return json.dumps([release, feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3
    assert "release_not_claimed" in caplog.text
    assert "issue=254" in caplog.text


def test_release_issue_claimed_when_no_ordinary_delivery(
    monkeypatch, caplog,
):
    """Issue #255: when all three ordinary ready scans are empty, the
    release fallback scan runs and claims the release Issue — it then
    flows into the release state machine downstream."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        if "label:ai-release" in searches[-1]:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == release
    # Three ordinary scans + one release fallback scan.
    assert len(searches) == 4
    assert "label:ai-release" in searches[3]
    assert "label:ai-ready" in searches[3]
    assert "picked issue=254" in caplog.text


def test_release_fallback_scoped_to_active_milestone(monkeypatch):
    """Issue #255: with a configured `active_milestone` the release
    fallback scan carries the same `milestone:"<title>"` scope as the
    ordinary scans — a release of another Milestone is never claimed."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        # Issue #663: with `active_milestone` the release candidate now
        # passes the Milestone completeness gate; this Milestone has only
        # the release ticket itself.
        if command[1] == "api":
            return "1"
        searches.append(command[command.index("--search") + 1])
        if "label:ai-release" in searches[-1]:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue(
        "xqliu/orbi", active_milestone="v0.3.0",
    ) == release
    assert 'milestone:"v0.3.0"' in searches[3]
    assert "label:ai-release" in searches[3]


def test_release_fallback_excludes_epic_and_blocked(
    monkeypatch, caplog,
):
    """Issue #255: the release fallback scan keeps the Epic and
    blockedBy guards — an `ai-epic` release Issue is skipped with
    `epic_not_claimed`, and a release Issue with open native blockers
    is skipped with `blocked_by` (nothing is claimed)."""
    epic_release = {
        "number": 260, "title": "epic release", "body": "",
        "labels": [
            {"name": "ai-ready"}, {"name": "ai-release"},
            {"name": "ai-epic"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    blocked_release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [{"number": 9}], "totalCount": 1},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([epic_release, blocked_release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "epic_not_claimed" in caplog.text
    assert "blocked_by" in caplog.text


def test_release_fallback_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #255: a failed release fallback query fails open like any
    other scan (Issue #54) — the tick claims nothing, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            raise error
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


# --- Issue #842 (D1): external takeover scan -------------------------------


def test_external_takeover_search_query():
    """Issue #842 (D1): the fifth scan is the repository's FIRST scan
    WITHOUT the `milestone:"<title>"` qualifier — an external
    contribution is not part of any version's delivery scope, and
    writing it into the active milestone would let an external PR block
    the release gate (`release_milestone_incomplete`). The query keeps
    `label:ai-ready` (the human execution switch), the same five
    delivery-state exclusions, and a body-text term for the triage
    marker. The text term is a candidate PREFILTER only (verified
    against the live search: GitHub tokenizes the marker loosely) —
    the exact marker check is the code-layer guard in
    `_pick_from_scan`."""
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    query = claim.external_takeover_search()
    assert query == f'label:ai-ready "orbi:external-pr" in:body {exclusions}'
    assert "milestone:" not in query
    # The default (no argument) is the same query; a repository's
    # custom dispatch label keys the scan the same way.
    assert claim.external_takeover_search() \
        == claim.external_takeover_search(None)
    assert claim.external_takeover_search("go") == (
        f'label:go "orbi:external-pr" in:body {exclusions}'
    )


def test_pick_issue_claims_milestone_less_external_ticket(
    monkeypatch, caplog,
):
    """Issue #842 (D1), the root-cause scene: `active_milestone`
    configured, the external triage ticket requeued to `ai-ready` with
    NO milestone (an external contributor cannot set one — and the
    runner must never write one). The three ordinary scans are
    milestone-scoped and see nothing, the release fallback sees
    nothing, and the FIFTH scan (no milestone qualifier) claims the
    ticket so the claim's takeover probe can review the external PR."""
    ticket = {
        "number": 842, "title": "external contribution", "body": (
            "please review\n\n<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        searches.append(search)
        if "orbi:external-pr" in search:
            return json.dumps([ticket])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) == ticket
    # p0, bug, plain, release fallback — all scoped; the fifth scan is
    # not.
    assert len(searches) == 5
    for scoped in searches[:4]:
        assert 'milestone:"v0.5.4"' in scoped
    assert "milestone:" not in searches[4]
    assert "orbi:external-pr" in searches[4]
    assert "picked issue=842" in caplog.text


def test_pick_issue_external_scan_never_claims_a_plain_ticket(
    monkeypatch, caplog,
):
    """Issue #842: the fifth scan is milestone-free, so the code layer
    must keep the claim scope: a loose body-text prefilter match
    WITHOUT the exact `<!-- orbi:external-pr:N -->` marker is skipped
    with `claim_yield reason=no_external_marker` — never claimed. A
    plain milestone-less Issue must never enter the queue of a runner
    with a configured `active_milestone`."""
    plain = {
        "number": 10, "title": "task",
        "body": "talking about an external PR review",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "orbi:external-pr" in search:
            return json.dumps([plain])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) is None
    assert "claim_yield" in caplog.text
    assert "no_external_marker" in caplog.text


def test_pick_issue_external_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #842: a failed fifth-scan query fails open like any other
    scan (Issue #54) — the tick claims nothing, the structured error is
    logged, never raised, and the next tick retries."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "orbi:external-pr" in search:
            raise error
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.5.4",
        ) is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_without_milestone_claims_external_ticket_first(
    monkeypatch,
):
    """Issue #842 regression: without a configured `active_milestone`
    the plain ready scan is already milestone-free and claims the
    external ticket like any other — the fifth scan is not even
    reached, so repositories without `active_milestone` behave exactly
    as before."""
    ticket = {
        "number": 842, "title": "external contribution", "body": (
            "<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        searches.append(search)
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([ticket])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == ticket
    assert len(searches) == 3
    assert all("orbi:external-pr" not in search for search in searches)


def test_pick_issue_never_scopes_the_takeover_scan_to_a_milestone():
    """Issue #842, the release-gate reverse assertion: the takeover scan
    must not carry ANY `milestone:` qualifier (with or without a
    configured active milestone) — an external ticket entering the
    active milestone's queue would let an external contributor block
    `release_milestone_incomplete` forever."""
    for search in (
        claim.external_takeover_search(None),
        claim.external_takeover_search("go"),
    ):
        assert "milestone:" not in search


def test_gather_claim_facts_routes_marker_ticket_to_takeover(
    monkeypatch, tmp_path, caplog,
):
    """Issue #842, the user path's middle link: the milestone-less
    external triage ticket the fifth scan picked walks the SAME claim
    machinery — the takeover probe resolves the open external PR (the
    structured `external_takeover` event) and the scene classifies
    EXTERNAL_TAKEOVER, whose handler reviews the contributor's head
    (no run_pi, and downstream never an auto-merge, decision D2)."""
    issue = {
        "number": 842, "title": "external contribution", "body": (
            "please review\n\n<!-- orbi:external-pr:838 -->"
        ),
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "has_in_progress_label",
                        lambda *a, **k: False)
    monkeypatch.setattr(seam, "open_pr_for_branch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "stable_branch_exists",
                        lambda *a, **k: False)
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN",
        "url": "https://github.com/xqliu/orbi/pull/838",
        "baseRefName": "main",
        "headRefName": "contributor-patch",
        "headRefOid": "abc123def456abc123def456abc123def456abcd",
    })
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    with caplog.at_level("INFO"):
        facts = runner._gather_claim_facts(
            issue, config, "xqliu/orbi", None,
        )
    assert facts.external_takeover is True
    assert facts.takeover_pr["headRefName"] == "contributor-patch"
    assert facts.pr_state == "OPEN"
    assert facts.classify_scene() is runner.DeliveryScene.EXTERNAL_TAKEOVER
    assert "external_takeover" in caplog.text


# --- Issue #663: release Milestone completeness gate ----------------------


def test_milestone_open_issue_count_reads_github_counter(monkeypatch):
    """Issue #663: the completeness judgment is GitHub's own Milestone
    `open_issues` counter, read with the one API call documented in the
    Issue (title-filtered jq) — not the search index, not a label."""
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return "3"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.milestone_open_issue_count("xqliu/orbi", "v0.4.2") == 3
    assert commands == [[
        "gh", "api", "repos/xqliu/orbi/milestones",
        "--jq", '.[] | select(.title=="v0.4.2") | .open_issues',
    ]]


def test_milestone_open_issue_count_missing_milestone_raises(monkeypatch):
    """Issue #663: an empty result (the Milestone is gone) is a failed
    check, not a silent zero — the caller must never claim on it."""
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "")
    with pytest.raises(RuntimeError, match="v0.4.2"):
        runner.milestone_open_issue_count("xqliu/orbi", "v0.4.2")


def test_release_target_milestone_prefers_issue_then_active():
    """Issue #663: the release's Milestone is the Issue's GitHub
    Milestone, falling back to the configured `active_milestone` (the
    fallback scan is scoped to it). No Milestone ⇒ no gate."""
    assert runner.release_target_milestone(
        {"milestone": {"title": "v0.4.2"}}, None,
    ) == "v0.4.2"
    assert runner.release_target_milestone({}, "v0.4.2") == "v0.4.2"
    assert runner.release_target_milestone({}, None) is None
    assert runner.release_target_milestone(
        {"milestone": {"title": ""}}, "v0.4.2",
    ) == "v0.4.2"
    assert runner.release_target_milestone({"milestone": "bad"}, None) is None
    assert runner.release_target_milestone(
        {"milestone": {"title": 7}}, None,
    ) is None


RELEASE_663 = {
    "number": 254, "title": "release", "body": "",
    "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
    "blockedBy": {"nodes": [], "totalCount": 0},
    "milestone": {"title": "v0.4.2"},
}


def _release_scan_fake(api_result):
    """Return a `run_command` fake: empty ordinary scans, the release
    in the fallback scan, and `api_result` (a string or an exception)
    for the Milestone counter query."""

    def fake_run(command, **kwargs):
        if command[1] == "api":
            if isinstance(api_result, Exception):
                raise api_result
            return api_result
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([RELEASE_663])
        return json.dumps([])

    return fake_run


def test_release_not_claimed_when_milestone_incomplete(monkeypatch, caplog):
    """Issue #663: a release ticket is not claimed while its Milestone
    still has open Issues — here an `ai-blocked` P0 that no ready scan
    sees. The gate reads GitHub's own `open_issues` counter, so a
    temporarily un-claimable Issue (ai-blocked / ai-pr-opened / not yet
    indexed) can no longer let the release run ahead."""
    monkeypatch.setattr(seam, "run_command", _release_scan_fake("3"))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "release_milestone_incomplete" in caplog.text
    assert "milestone=v0.4.2" in caplog.text
    assert "open_issues=3" in caplog.text
    # The Issue stays `ai-ready`: it was never claimed.
    assert "picked issue=254" not in caplog.text


def test_release_claimed_when_milestone_only_has_the_release(
    monkeypatch, caplog,
):
    """Issue #663: with only the release ticket itself left
    (`open_issues=1`) the release is claimed exactly as before."""
    monkeypatch.setattr(seam, "run_command", _release_scan_fake("1"))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == RELEASE_663
    assert "picked issue=254" in caplog.text


def test_release_not_claimed_when_milestone_check_fails(monkeypatch, caplog):
    """Issue #663: a milestone query that cannot be evaluated never
    claims — a bad release is irreversible, so the check fails safe and
    the next tick retries (recoverable, never `ai-blocked`)."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command", _release_scan_fake(error))
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "release_milestone_check_failed" in caplog.text
    assert "picked issue=254" not in caplog.text


def test_release_without_milestone_claimed_without_api_call(
    monkeypatch, caplog,
):
    """Issue #663: a release with no Milestone (and no configured
    `active_milestone`) keeps the pre-#663 behavior — claimed with no
    extra API call."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    commands_seen = []

    def fake_run(command, **kwargs):
        commands_seen.append(command)
        if "label:ai-release" in " ".join(command):
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == release
    assert not [c for c in commands_seen if c[1] == "api"]
    assert "picked issue=254" in caplog.text


def test_release_milestone_falls_back_to_active_milestone(
    monkeypatch, caplog,
):
    """Issue #663: without a Milestone on the release Issue, the
    configured `active_milestone` (the fallback scan's scope) is the
    Milestone whose completeness is checked."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    api_calls = []

    def fake_run(command, **kwargs):
        if command[1] == "api":
            api_calls.append(command)
            return "2"
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue(
            "xqliu/orbi", active_milestone="v0.3.0",
        ) is None
    assert api_calls
    assert "milestone=v0.3.0" in caplog.text
    assert "open_issues=2" in caplog.text


def test_release_fallback_scan_fetches_milestone(monkeypatch):
    """Issue #663: the release fallback scan fetches `milestone` so the
    completeness gate reads the release's GitHub Milestone from the same
    query — no extra `gh issue view`."""
    release = {
        "number": 254, "title": "release", "body": "",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    scans = []

    def fake_run(command, **kwargs):
        scans.append(command)
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([release])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_issue("xqliu/orbi") == release
    fields = scans[-1][scans[-1].index("--json") + 1]
    assert "milestone" in fields.split(",")


def test_pick_issue_p0_blocked_by_open_blocker_falls_back_to_bug(
    monkeypatch, caplog,
):
    """Issue #101: a P0 with open native blockers is skipped by the P0
    scan (same blockedBy semantics as the other scans — no claim, no
    label change, no worktree); the bug scan then decides."""
    blocked_p0 = {
        "number": 7, "title": "p0 blocked", "body": "",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [{"number": 3}], "totalCount": 1},
    }
    bug = {
        "number": 11, "title": "bug", "body": "",
        "labels": [{"name": "bug"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    searches = []

    def fake_run(command, **kwargs):
        searches.append(command[command.index("--search") + 1])
        # Only the P0 and bug scans run (the bug scan claims the bug):
        # the P0 scan sees the blocked P0, the bug scan the bug.
        if "label:p0" in searches[-1]:
            return json.dumps([blocked_p0])
        return json.dumps([bug])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == bug
    # The blocked P0 was skipped with the structured blocked_by line;
    # the bug was claimed with priority=normal (it is not a P0).
    assert "blocked_by" in caplog.text
    assert "issue=7" in caplog.text
    assert "3" in caplog.text
    assert len(searches) == 2
    assert "priority=normal" in caplog.text


def test_pick_issue_p0_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #101: a failed P0 scan fails open like the other scans
    (Issue #54) — the tick claims nothing from this repo, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


def test_pick_issue_logs_priority_normal_for_plain_pickup(
    monkeypatch, caplog,
):
    """Issue #101: a non-P0 pickup (bug or plain) logs
    `priority=normal` — the explicit field is present on every
    pickup, not only for P0."""
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert "priority=normal" in caplog.text
    assert "issue=10" in caplog.text


def test_issue_priority_reads_the_p0_label():
    """Issue #101: `issue_priority` is a pure function of the issue's
    `labels` (the scans fetch `labels`, so no extra gh call): `p0`
    when the label is present, `normal` otherwise."""
    assert runner.issue_priority({
        "number": 7,
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
    }) == "p0"
    assert runner.issue_priority({
        "number": 10,
        "labels": [{"name": "ai-ready"}, {"name": "bug"}],
    }) == "normal"
    assert runner.issue_priority({"number": 10}) == "normal"
    # Malformed label fields never break the pickup (fail to normal,
    # like the blockedBy field fails open): a P0 misread as normal
    # only loses its ordering for one run, never the delivery.
    assert runner.issue_priority({"number": 10, "labels": "nope"}) \
        == "normal"
    assert runner.issue_priority({
        "number": 10, "labels": ["p0", {"name": 7}],
    }) == "normal"


# --- Issue #93: Epic handling (ai-epic) --------------------------------------


def test_is_epic_reads_the_ai_epic_label():
    """Issue #93: `is_epic` is a pure function of the issue's `labels`
    (the scans fetch `labels`, so no extra gh call): True when the
    `ai-epic` label is present. A missing or malformed `labels` field
    fails open to "not an epic" (the same style as `issue_priority`
    failing to normal): the scan always requests `labels`, so a shape
    change only loses the Epic guard for one run — it must never
    deadlock the queue."""
    assert claim.is_epic({
        "number": 80,
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
    }) is True
    assert claim.is_epic({
        "number": 10,
        "labels": [{"name": "ai-ready"}, {"name": "bug"}],
    }) is False
    assert claim.is_epic({"number": 10}) is False
    assert claim.is_epic({"number": 10, "labels": "nope"}) is False
    assert claim.is_epic({
        "number": 10, "labels": ["ai-epic", {"name": 7}],
    }) is False


def test_pick_issue_skips_epic_and_claims_next(monkeypatch, caplog):
    """Issue #93: an `ai-ready` Issue carrying `ai-epic` is never
    claimed — no label change, no worktree, no run: the runner logs a
    structured `epic_not_claimed` line with the Issue number and repo
    and moves on to the next ready Issue of the same repo."""
    epic = {
        "number": 80, "title": "v0.1 release checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    ready = {
        "number": 81, "title": "sub task", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic, ready]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == ready
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text
    assert "repo=xqliu/orbi" in caplog.text
    # The Epic was skipped for being an Epic — not for blockers — so
    # no `blocked_by` line is logged for it.
    assert "blocked_by" not in caplog.text


def test_pick_issue_returns_none_when_only_epics_are_ready(
    monkeypatch, caplog,
):
    """Issue #93: a ready queue that only contains Epics yields no
    claim — the tick ends idle upstream (`no_ready_issue`), so an
    Epic never occupies a delivery slot."""
    epic_a = {
        "number": 80, "title": "v0.1 checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    epic_b = {
        "number": 133, "title": "0.2.0 workspace", "body": "",
        "labels": [{"name": "ai-epic"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        # The Epics carry no p0/bug/ai-release label: only the plain
        # ready scan sees them; the release fallback scan (Issue #255)
        # matches nothing.
        if (
            "label:p0" in search or "label:bug" in search
            or "label:ai-release" in search
        ):
            return json.dumps([])
        return json.dumps([epic_a, epic_b])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert caplog.text.count("epic_not_claimed") == 2
    assert "issue=80" in caplog.text
    assert "issue=133" in caplog.text


def test_pick_issue_epic_check_precedes_blocker_check(monkeypatch, caplog):
    """Issue #93: an Epic is never claimed regardless of its blockedBy
    state — the `epic_not_claimed` reason (it is an Epic) is more
    fundamental than its blocker list, so the Epic check runs first
    and no `blocked_by` line is logged for it."""
    epic = {
        "number": 80, "title": "epic", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {
            "nodes": [{"number": 9}, {"number": 10}], "totalCount": 2,
        },
    }
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([epic]),
    )
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") is None
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text
    assert "blocked_by" not in caplog.text


def test_pick_issue_skips_epic_in_p0_and_bug_scans(monkeypatch, caplog):
    """Issue #93: the Epic skip applies to EVERY ready scan (P0, bug,
    plain) — an `ai-epic` Issue is never claimed no matter which
    priority queue it sits in; the next non-Epic ready Issue is
    claimed instead."""
    epic_p0 = {
        "number": 80, "title": "epic p0", "body": "",
        "labels": [
            {"name": "ai-epic"}, {"name": "ai-ready"}, {"name": "p0"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    epic_bug = {
        "number": 81, "title": "epic bug", "body": "",
        "labels": [
            {"name": "ai-epic"}, {"name": "ai-ready"}, {"name": "bug"},
        ],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    feature = {
        "number": 10, "title": "feature", "body": "",
        "labels": [{"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        search = command[command.index("--search") + 1]
        if "label:p0" in search:
            return json.dumps([epic_p0])
        if "label:bug" in search:
            return json.dumps([epic_bug])
        return json.dumps([feature])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert claim.pick_issue("xqliu/orbi") == feature
    assert caplog.text.count("epic_not_claimed") == 2
    assert "issue=80" in caplog.text
    assert "issue=81" in caplog.text


def test_pick_in_progress_issue_scan_excludes_epics(monkeypatch, tmp_path):
    """Issue #93: the restart-resume scan excludes `ai-epic` — a
    legacy Epic left behind with `ai-in-progress` (the #80 scene,
    before the Epic mechanism existed) must never be resumed into
    `run_pi`: an Epic is coordination, not an executable task."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_main_epic_only_queue_ends_idle_without_process_issue(
    monkeypatch, tmp_path, caplog,
):
    """Issue #93: when the ready queue contains only `ai-epic`
    Issues, the tick ends idle (`no_ready_issue`): no claim, no
    worktree, no `run_pi` — an Epic never enters the delivery
    pipeline and never occupies a slot through a delivery."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    epic = {
        "number": 80, "title": "v0.1 release checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            search = command[command.index("--search") + 1]
            # The resumable-PR and in-flight restart scans are idle;
            # the ready scans see the Epic.
            if search.startswith("label:ai-fix-needed") or search.startswith(
                "label:ai-ready label:ai-in-progress",
            ):
                return "[]"
            return json.dumps([epic])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not issue-list traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])

    def fail_process(*args, **kwargs):
        raise AssertionError("process_issue must not run for an Epic")

    monkeypatch.setattr(runner, "process_issue", fail_process)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="must not run for an Epic"):
        fail_process()
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "no_ready_issue" in caplog.text
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text


def test_pick_in_progress_issue_scan_fetches_labels(monkeypatch, tmp_path):
    """Issue #101: the in-flight restart scan fetches `labels` too, so
    a P0 a killed runner left behind keeps its priority in the
    progress comment on resume (the scan's exclusions are unchanged)."""
    issue = {
        "number": 18, "title": "task", "body": "body",
        "labels": [{"name": "p0"}, {"name": "ai-ready"}],
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_scan_fetches_milestone(monkeypatch, tmp_path):
    """Issue #671: the in-flight restart scan fetches `milestone` too, so
    a release run killed mid-release and resumed by `process_release`
    scopes its leftover-delivery gate to the release's OWN Milestone —
    never to the config's `active_milestone`, which must not gate a
    resume (Issue #139)."""
    issue = {
        "number": 664, "title": "Release v0.4.3", "body": "body",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
        "milestone": {"title": "v0.4.3"},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    resumed = claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    )
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]
    # The resumed release's gate scope is its own Milestone, not the
    # config value.
    assert runner.release_target_milestone(resumed, "v0.4.4") == "v0.4.3"


def test_pick_in_progress_issue_scans_in_flight_issues(monkeypatch, tmp_path):
    """A killed runner leaves `ai-ready`+`ai-in-progress` behind (Issue
    #18): the claim scan must recover such in-flight Issues, so the
    restart resume (run id / worktree / progress comment reuse) is
    reachable in the production flow — the ready scan alone excludes
    `ai-in-progress` and would strand the run forever."""
    issue = {
        "number": 18, "title": "task", "body": "body",
        "labels": [{"name": "ai-ready"}],
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(seam, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_uses_the_repo_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #527: a repository that replaced `ai-ready` with its own
    claim label keeps the restart-resume path — the in-flight scan must
    search the SAME queue entry the ready scan used, otherwise a killed
    (or model-wait-recovered) run is stranded forever."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
        dispatch_label="repo-ready",
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:repo-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels,milestone", "--limit", "1",
    ]]


def test_pick_in_progress_issue_returns_none_when_idle(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]",
    )
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None


def test_pick_in_progress_issue_skips_when_another_runner_is_live(
    monkeypatch, tmp_path,
):
    """A slot held by ANOTHER process proves a live runner is working
    (Issue #39 slot semantics): the `ai-in-progress` label is in
    flight, not orphaned, so no second Pi may be started for it. This
    runner's own slot (its own PID) does not block the scan."""
    gh_calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: gh_calls.append(command) or "[]",
    )
    monkeypatch.setattr(seam, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, 4242)])
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert gh_calls == [], "no gh traffic while another runner is live"
    # Own PID: the scan still runs (this runner holds its own slot).
    monkeypatch.setattr(seam, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, os.getpid())])
    assert claim.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert len(gh_calls) == 1


def test_pick_next_delivery_recovers_in_flight_issue_before_ready(
    monkeypatch, tmp_path,
):
    """Claim order (Issue #18): resumable PRs first, then in-flight
    restarts (killed runner, `ai-in-progress` left behind), then ready
    Issues — a dead run is resumed, never skipped by the ready scan."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    ready = {"number": 3, "title": "ready", "body": ""}
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: (
            in_flight if repo == "r1" else None
        ),
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: ready)
    assert claim.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r1", in_flight, None)


def test_pick_next_delivery_keeps_resumable_delivery_first(
    monkeypatch, tmp_path,
):
    """An `ai-fix-needed` PR resume always beats an in-flight restart:
    the PR already exists and must not be re-implemented."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    resumable = {"number": 5, "title": "fix needed", "body": ""}
    scene = config_domain.RunnerConfig(run_id="a1b2c3d4")
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: (
            (resumable, scene) if repo == "r2" else None
        ),
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: in_flight,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: in_flight)
    assert claim.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r2", resumable, scene)


def test_pick_next_delivery_falls_through_to_ready_when_no_in_flight(
    monkeypatch, tmp_path,
):
    ready = {"number": 3, "title": "ready", "body": ""}
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: ready)
    assert claim.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) == ("r1", ready, None)


def test_pick_next_delivery_returns_none_when_all_scans_empty(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        claim, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(
        claim, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: None)
    assert claim.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
        hooks=resume_deps(),
    ) is None


def test_pick_next_issue_returns_first_ready_source(monkeypatch):
    issue = {"number": 1, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None, **_kwargs):
        calls.append(repo)
        return issue if repo == "xqliu/orbi-backlog" else None

    monkeypatch.setattr(claim, "pick_issue", pick)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) == (
        "xqliu/orbi-backlog", issue,
    )
    assert calls == ["xqliu/orbi-backlog"]


def test_pick_next_issue_falls_through_to_second_source(monkeypatch):
    issue = {"number": 2, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None, **_kwargs):
        calls.append(repo)
        return issue if repo == "xqliu/orbi" else None

    monkeypatch.setattr(claim, "pick_issue", pick)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) == (
        "xqliu/orbi", issue,
    )
    assert calls == ["xqliu/orbi-backlog", "xqliu/orbi"]


def test_pick_next_issue_returns_none_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(claim, "pick_issue", lambda repo, active_milestone=None, **_kwargs: None)
    assert claim.pick_next_issue(["xqliu/orbi-backlog", "xqliu/orbi"]) is None


def test_edit_issue_builds_add_and_remove_command(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/orbi-backlog", add="ai-in-progress", remove="ai-ready")
    assert calls == [[
        "gh", "issue", "edit", "3", "--repo", "xqliu/orbi-backlog",
        "--add-label", "ai-in-progress", "--remove-label", "ai-ready",
    ]]


def test_edit_issue_allows_no_label_change(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/orbi-backlog")
    assert calls == [["gh", "issue", "edit", "3", "--repo", "xqliu/orbi-backlog"]]


def test_new_run_id_is_unique_short_hex():
    first = runner.new_run_id()
    second = runner.new_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert first != second


def test_validate_run_id_accepts_eight_hex_chars():
    assert runner.validate_run_id("e07383c2") == "e07383c2"


def test_validate_run_id_rejects_wrong_length_or_chars():
    for bad in ("run1", "", "a1b2c3d", "a1b2c3d4e", "A1B2C3D4", "g1b2c3d4"):
        with pytest.raises(ValueError, match="invalid run id"):
            runner.validate_run_id(bad)


def test_validate_run_id_rejects_non_string():
    with pytest.raises(ValueError, match="invalid run id"):
        runner.validate_run_id(None)


def test_run_marker_is_stable_machine_readable_comment():
    assert runner.run_marker("e07383c2") == "<!-- orbi:run=e07383c2 -->"


def test_run_marker_is_shared_with_progress_formatter():
    assert runner.run_marker is progress.run_marker


def test_set_run_id_binds_the_attempt_and_current_run_id_reads_it(monkeypatch):
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    assert runner.current_run_id() is None
    runner.set_run_id("e07383c2")
    assert runner.current_run_id() == "e07383c2"


def test_set_run_id_fails_fast_on_invalid_run_id(monkeypatch):
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    with pytest.raises(ValueError, match="invalid run id"):
        runner.set_run_id("run1")
    assert runner.current_run_id() is None


def test_run_id_filter_prefixes_messages_when_run_is_bound(caplog):
    runner.set_run_id("e07383c2")
    try:
        with caplog.at_level("INFO"):
            runner.LOGGER.info("hello %s", "world")
        assert caplog.messages[-1] == "[e07383c2] hello world"
    finally:
        journal._CURRENT_RUN_ID = None


def test_run_id_filter_leaves_messages_untouched_without_bound_run(
    monkeypatch, caplog,
):
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    with caplog.at_level("INFO"):
        runner.LOGGER.info("hello")
    assert caplog.messages[-1] == "hello"


def test_freeze_base_fetches_remote_and_returns_exact_sha(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return "abc123def456"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert calls == [(
        ["git", "fetch", "origin", "main"], {
            "cwd": tmp_path,
            "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
        },
    ), (
        ["git", "rev-parse", "origin/main"], {"cwd": tmp_path},
    )]


def _probe_lock_free(repo_dir: Path) -> bool:
    """True when a non-blocking probe acquires the base-sync lock
    (i.e. the lock is FREE); False while it is held."""
    lock_path = gitops.base_sync_lock_path(repo_dir)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


def test_freeze_base_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the freeze fetch updates the shared remote-tracking
    # ref, so it must run under the SAME base-sync lock (a concurrent
    # probe must not acquire it while the fetch is in flight) and the
    # lock must be free again afterwards.
    held = []

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            held.append(not _probe_lock_free(tmp_path))
        return "abc123def456"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


def test_freeze_base_fails_fast_when_remote_base_is_missing(monkeypatch, tmp_path):
    error = subprocess.CalledProcessError(
        128, ["git", "rev-parse", "origin/main"],
        stderr="fatal: ambiguous argument",
    )
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(subprocess.CalledProcessError):
        runner.freeze_base(tmp_path, "main")


def test_create_worktree_reuses_existing_path_for_a_resumed_run(
    monkeypatch, tmp_path,
):
    """Only a resumed run (same run id after a process restart) reaches
    an existing worktree path; it is the scene the run continues in
    (Issue #18), so it is reused, never recreated."""
    existing = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    existing.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command),
    )
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "abc123def456",
    ) == existing
    assert calls == [], "a resumed worktree must never be recreated"


def test_create_worktree_adds_branch_from_frozen_base_sha(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "",
    )
    assert runner.create_worktree(tmp_path, "owner/repo", 3, "run1", "abc123def456") == path
    assert calls == [
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "-b", "orbi/owner-repo-issue-3", str(path), "abc123def456"],
         {"cwd": tmp_path}),
    ]


def test_create_worktree_reuses_orphan_local_branch_never_exits_255(
    monkeypatch, tmp_path,
):
    """Issue #662 (the #655 incident): a SIGKILLed run can leave the stable
    branch behind with no worktree and no remote counterpart (a pure orphan).
    `worktree add -b` cannot re-create it (the exit-255 that burned the Issue
    into terminal ai-blocked), so the orphan branch is reused by identity."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return "orbi/owner-repo-issue-3"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
    ) == path
    assert calls == [
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", str(path), "orbi/owner-repo-issue-3"],
         {"cwd": tmp_path}),
    ]


# Real-git scaffolding shared with the release domain suite.
# tests/test_bootstrap_release.py imports both helpers from here
# instead of keeping a copy (one owner, Issue #1645).
def make_local_remote_pair(tmp_path):
    """A real local 'origin' bare remote + a working clone with one commit."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(work)],
        check=True, capture_output=True,
    )
    for key, value in (
        ("user.email", "t@t"), ("user.name", "t"),
        ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(work), "config", key, value],
            check=True, capture_output=True,
        )
    (work / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "f.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "one"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "main"],
        check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return work, head


def _git_output(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_create_worktree_reuses_orphan_local_branch_real_git(tmp_path):
    """Real-git acceptance (Issue #662, the #655 scene): the local orphan
    branch is checked out into the new worktree where the bare `worktree add
    -b` exited 255 (`fatal: a branch named ... already exists`)."""
    work, head = make_local_remote_pair(tmp_path)
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(repo_dir)],
        check=True, capture_output=True,
    )
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", key, value],
            check=True, capture_output=True,
        )
    # The killed claim left the branch at the base commit; it was never
    # pushed, so origin has no counterpart (the #655 orphan).
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch",
         "orbi/owner-repo-issue-3", head],
        check=True, capture_output=True,
    )
    remote = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-remote", "--heads", "origin",
         "refs/heads/orbi/owner-repo-issue-3"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert remote == "", "the orphan branch must not exist on origin"
    # The bare `git worktree add -b` of the incident fails (exit 255).
    incident = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b",
         "orbi/owner-repo-issue-3", str(tmp_path / "incident"), head],
        capture_output=True, text=True,
    )
    assert incident.returncode != 0, incident
    assert "already exists" in incident.stderr
    # The orphan-aware path reuses the branch instead.
    path = runner.create_worktree(repo_dir, "owner/repo", 3, "run1", head)
    current = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == "orbi/owner-repo-issue-3"


def test_create_worktree_reuses_existing_remote_branch(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base", existing_branch=True,
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/orbi/owner-repo-issue-3:refs/remotes/origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", "-b", "orbi/owner-repo-issue-3", str(path), "origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
    ]


def test_create_worktree_existing_local_branch_is_reused_never_re_created(
    monkeypatch, tmp_path,
):
    """An equal local delivery branch remains the checked-out identity."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return "orbi/owner-repo-issue-3"
        if command == ["git", "rev-parse", "orbi/owner-repo-issue-3"]:
            return "same-sha"
        if command == ["git", "rev-parse", "origin/orbi/owner-repo-issue-3"]:
            return "same-sha"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base", existing_branch=True,
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/orbi/owner-repo-issue-3:refs/remotes/origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "rev-parse", "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "rev-parse", "origin/orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", str(path), "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
    ]


def test_create_worktree_branch_override_checks_out_the_external_head(
    monkeypatch, tmp_path,
):
    """Issue #608: an external takeover checks out the contributor's own
    head branch — the identity the takeover PR is frozen on."""
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
        existing_branch=True, branch="fix/outer",
    ) == path
    assert calls == [
        (["git", "fetch", "origin", "+refs/heads/fix/outer:refs/remotes/origin/fix/outer"], {"cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS}),
        (["git", "branch", "--list", "fix/outer"], {"cwd": tmp_path}),
        (["git", "worktree", "add", "--force", "-b", "fix/outer", str(path), "origin/fix/outer"], {"cwd": tmp_path}),
    ]


def test_create_worktree_fork_takeover_fetches_pull_head_ref(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0:3] == ["git", "branch", "--list"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "base",
        existing_branch=True, branch="fix/outer", pr_number=592,
    ) == path
    fetch = calls[0]
    assert fetch[0] == [
        "git", "fetch", "origin",
        "+pull/592/head:refs/remotes/origin/fix/outer",
    ]
    assert len(fetch[0]) == 4
    assert fetch[1] == {
        "cwd": tmp_path, "timeout": journal.GIT_NETWORK_TIMEOUT_SECONDS,
    }
    assert calls[-1][0][-1] == "origin/fix/outer"


def test_create_worktree_fork_takeover_repeats_with_existing_target(tmp_path):
    """Real-git acceptance: a fork PR refspec can overwrite its existing
    remote-tracking destination on a repeated takeover."""
    work, first_head = make_local_remote_pair(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin",
         "HEAD:refs/pull/592/head"],
        check=True, capture_output=True, timeout=30,
    )
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(repo_dir)],
        check=True, capture_output=True, timeout=30,
    )
    path = runner.create_worktree(
        repo_dir, "owner/repo", 3, "first", first_head,
        existing_branch=True, branch="fix/outer", pr_number=592,
    )
    assert path.is_dir()
    subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(path)],
        check=True, capture_output=True, timeout=30,
    )

    (work / "f.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "f.txt"],
        check=True, capture_output=True, timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "second"],
        check=True, capture_output=True, timeout=30,
    )
    second_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "--force", "origin",
         "HEAD:refs/pull/592/head"],
        check=True, capture_output=True, timeout=30,
    )
    repeated = runner.create_worktree(
        repo_dir, "owner/repo", 3, "second", first_head,
        existing_branch=True, branch="fix/outer", pr_number=592,
    )
    assert repeated.is_dir()
    fetched = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse",
         "refs/remotes/origin/fix/outer"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    assert fetched == second_head


def test_create_worktree_takeover_with_existing_branch_never_exits_255(tmp_path):
    """Real-git acceptance (Issue #608 scenario a): the stable branch
    already exists on origin — the takeover path succeeds where the bare
    `worktree add -b <branch>` of the #600 incident exited 255."""
    work, head = make_local_remote_pair(tmp_path)
    repo_dir = tmp_path / "runner"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(repo_dir)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "t@t"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "t"],
        check=True, capture_output=True,
    )
    # The stable branch already exists on origin (left by the previous
    # run) — pushed from the working clone of the shared fixture.
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin",
         "main:refs/heads/orbi/owner-repo-issue-3"],
        check=True, capture_output=True,
    )
    # The previous run also left the LOCAL branch in the deployment
    # checkout (the incident scene: `-b` cannot re-create it).
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch",
         "orbi/owner-repo-issue-3", head],
        check=True, capture_output=True,
    )
    # The bare `git worktree add -b` of the incident scene fails (the
    # runner journal recorded it as exit 255): `-b` never re-creates an
    # existing branch.
    incident = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "add", "-b",
         "orbi/owner-repo-issue-3", str(tmp_path / "incident"), head],
        capture_output=True, text=True,
    )
    assert incident.returncode != 0, incident
    assert "already exists" in incident.stderr
    # The takeover path reuses the existing branch and succeeds.
    path = runner.create_worktree(
        repo_dir, "owner/repo", 3, "run1", head, existing_branch=True,
    )
    current = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == "orbi/owner-repo-issue-3"


def test_create_release_worktree_resets_leftover_worktree_to_current_commit(
    monkeypatch, tmp_path,
):
    old_worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-oldrun"
    branch = "orbi/owner-repo-issue-3"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return f"worktree {old_worktree}\nHEAD old\nbranch refs/heads/{branch}\n"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == old_worktree
    assert calls[-1] == (
        ["git", "reset", "--hard", "new-release-commit"],
        {"cwd": old_worktree},
    )
    assert not any(command[:3] == ["git", "worktree", "add"]
                   for command, _ in calls)


def test_create_release_worktree_handles_leftover_local_branch(
    monkeypatch, tmp_path,
):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-newrun"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return ""
        if command == ["git", "branch", "--list", "orbi/owner-repo-issue-3"]:
            return "  orbi/owner-repo-issue-3"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == path
    assert calls[-2:] == [
        (["git", "worktree", "add", "--force", str(path),
          "orbi/owner-repo-issue-3"], {"cwd": tmp_path}),
        (["git", "reset", "--hard", "new-release-commit"],
         {"cwd": path}),
    ]


def test_create_release_worktree_creates_from_release_commit_when_branch_is_new(
    monkeypatch, tmp_path,
):
    calls = []
    expected = tmp_path / "created"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["git", "worktree", "list", "--porcelain"]:
            return ""
        if command == ["git", "branch", "--list", "orbi/owner-repo-issue-3"]:
            return ""
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *args: expected)
    assert runner.create_release_worktree(
        tmp_path, "owner/repo", 3, "newrun", "new-release-commit",
    ) == expected
    assert calls[-1] == (
        ["git", "reset", "--hard", "new-release-commit"],
        {"cwd": expected},
    )


def test_latest_run_id_returns_none_without_task_worktrees(tmp_path):
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) is None
    # Unrelated directories are not task worktrees.
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "other").mkdir()
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) is None


def test_latest_run_id_returns_the_run_id_of_the_newest_worktree(tmp_path):
    slug = "owner-repo"
    first = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-0d111111"
    second = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-2e222222"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    # The newest by mtime wins, even when it was created first by name.
    os.utime(first, (200, 200))
    os.utime(second, (100, 100))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "0d111111"
    os.utime(second, (300, 300))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "2e222222"
    # A worktree of another issue (or another source repo) never counts.
    other_issue = tmp_path / ".worktrees" / f"orbi-{slug}-issue-4-ffff0000"
    other_repo = tmp_path / ".worktrees" / f"orbi-other-other-issue-3-ffff1111"
    other_issue.mkdir()
    other_repo.mkdir()
    os.utime(other_issue, (400, 400))
    os.utime(other_repo, (400, 400))
    assert runner.latest_run_id(tmp_path, "owner/repo", 3) == "2e222222"


# --- Issue #219: continue the interrupted run, never a fresh redo ---------


def test_run_state_path_lives_in_the_gitignored_orbi_dir(tmp_path):
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    assert run_state.run_state_path(worktree) == (
        worktree / ".orbi" / "run-state.json"
    )


def test_write_run_state_writes_the_run_identity(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = json.loads(run_state.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"
    assert state["branch"] == "orbi/owner-repo-issue-3"
    assert state["worktree"] == str(worktree)
    assert isinstance(state["created_at"], str) and state["created_at"]


def test_write_run_state_is_idempotent_for_a_resumed_run(tmp_path):
    """A resumed run refreshes the SAME state file (same run id) — the
    file is the same-run marker, never a per-session artifact."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = json.loads(run_state.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"


def test_read_run_state_returns_none_without_the_file(tmp_path):
    assert run_state.read_run_state(tmp_path) is None


def test_read_run_state_round_trips_the_written_state(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"


def test_read_run_state_fails_fast_on_unreadable_or_malformed_state(tmp_path):
    """A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run (Issue #219)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    path = run_state.run_state_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)
    path.write_text(json.dumps({"run_id": "a1b2c3d4"}), encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        run_state.read_run_state(worktree)


def test_write_run_state_preserves_the_push_history(tmp_path):
    """The push-history fields (Issue #833) survive a resume's state
    refresh — the same delivery line keeps its recorded heads."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    assert review_merge.read_pushed_head(worktree) == "h1"
    assert review_merge.read_pushed_base(worktree) == "b1"


def test_record_pushed_base_is_set_once_per_run(tmp_path):
    """A re-claim of the same run re-derives a moved HEAD, which is
    not the push line's origin — the first recorded base wins."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_state.write_run_state(
        runner.RunContext(
            run_id="a1b2c3d4", issue=3, branch="orbi/owner-repo-issue-3",
            worktree=worktree, source_repo="owner/repo",
        ),
    )
    run_state.record_pushed_base(worktree, "b1")
    run_state.record_pushed_base(worktree, "b2")
    assert review_merge.read_pushed_base(worktree) == "b1"


def test_record_pushed_head_degrades_without_the_state_file(
        tmp_path, caplog):
    """Recording is bypass-safe (Issue #73): a recreated worktree lost
    `.orbi/` — the record logs `pushed_head_unrecorded` and continues;
    the merge record degrades to `unknown`, the delivery does not
    fail."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    caplog.set_level("WARNING")
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    assert review_merge.read_pushed_head(worktree) is None
    assert review_merge.read_pushed_base(worktree) is None
    assert "pushed_head_unrecorded" in caplog.text


def test_record_pushed_head_degrades_on_a_corrupt_state_file(
        tmp_path, caplog):
    """The same bypass safety for a corrupt record: the resume readers
    fail fast on it before any record point, and the recorder itself
    only logs and degrades the merge record to `unknown`."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    path = run_state.run_state_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    caplog.set_level("WARNING")
    run_state.record_pushed_head(worktree, "h1")
    run_state.record_pushed_base(worktree, "b1")
    assert review_merge.read_pushed_head(worktree) is None
    assert "pushed_head_unrecorded" in caplog.text


def make_session_jsonl(worktree: Path, session_id: str = "sess-1") -> Path:
    """A minimal previous session file (Issue #219 resume context)."""
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": session_id}) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-02T04:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "bash",
                             "arguments": {"command": "pytest tests/"}}],
            },
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-02T04:01:00Z",
            "message": {"role": "toolResult", "toolName": "bash",
                        "isError": False},
        }) + "\n",
        encoding="utf-8",
    )
    return path


def test_changed_files_lists_uncommitted_changes(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        # The third line has a blank XY column: it is not a change.
        return " M src/a.py\n?? src/b.py\n   src/c.py\n"

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert pi_session.changed_files(tmp_path) == ["src/a.py", "src/b.py"]
    assert calls == [(
        ["git", "status", "--porcelain"], {"cwd": tmp_path},
    )]


def test_resume_context_is_none_for_a_fresh_worktree(tmp_path, monkeypatch):
    """No uncommitted changes and no previous session: the agent starts
    from the Issue alone (the exact pre-#219 prompt)."""
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(pi_session, "activity_snapshot", lambda *a, **k: None)
    assert pi_session.resume_context(tmp_path) is None


def test_resume_context_carries_changes_and_session_progress(
    tmp_path, monkeypatch,
):
    """The new session starts from the existing work: the instruction to
    continue, the previous session's progress and the changed files —
    never a fresh redo (Issue #219)."""
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: " M src/a.py\n?? src/b.py\n",
    )
    monkeypatch.setattr(
        pi_session, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-1", "session_file": "x.jsonl",
            "events": 7, "phase": "test", "last_activity": "2026-09-02T04:01:00Z",
            "action": "bash pytest tests/", "result": "ok",
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = pi_session.resume_context(tmp_path)
    assert "Continue" in context or "continue" in context
    assert "redo" in context.lower() or "scratch" in context.lower()
    assert "sess-1" in context
    assert "7" in context
    assert "test" in context
    assert "bash pytest tests/" in context
    assert "src/a.py" in context
    assert "src/b.py" in context
    assert "2" in context


def test_resume_context_without_changes_carries_the_session_progress(
    tmp_path, monkeypatch,
):
    """A clean worktree with a previous session (the agent committed
    mid-run before the interruption) still continues the same run."""
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(
        pi_session, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-2", "session_file": "x.jsonl",
            "events": 3, "phase": "commit", "last_activity": None,
            "action": "git commit -m x", "result": None,
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = pi_session.resume_context(tmp_path)
    assert context is not None
    assert "sess-2" in context
    assert "src/a.py" not in context


def test_resume_run_id_returns_none_without_candidates(tmp_path):
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "other").mkdir()
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None


def test_resume_run_id_matches_by_issue_and_repo_name_across_a_rename(
    tmp_path,
):
    """The repo was renamed (slug `xqliu-orbi` -> `orbi-build-orbi`):
    the old worktree is found by issue number + repo NAME (the stable
    identity) and the SAME run continues — no second run/branch/
    worktree (Issue #219)."""
    old = tmp_path / ".worktrees" / "orbi-xqliu-orbi-issue-42-aaaa1111"
    old.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="aaaa1111", issue=42, branch="orbi/xqliu-orbi-issue-42", worktree=old, source_repo="xqliu/orbi"))
    assert runner.resume_run_id(
        tmp_path, "orbi-build/orbi", 42,
    ) == "aaaa1111"
    # A different repo name never matches, even with the same issue.
    assert runner.resume_run_id(
        tmp_path, "orbi-build/other", 42,
    ) is None


def test_resume_run_id_returns_newest_matching_worktree(tmp_path):
    slug = "owner-repo"
    first = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-0d111111"
    second = tmp_path / ".worktrees" / f"orbi-{slug}-issue-3-2e222222"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="0d111111", issue=3, branch="b1", worktree=first, source_repo="owner/repo"))
    run_state.write_run_state(RunContext(run_id="2e222222", issue=3, branch="b2", worktree=second, source_repo="owner/repo"))
    os.utime(first, (300, 300))
    os.utime(second, (100, 100))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "0d111111"
    os.utime(second, (400, 400))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "2e222222"


def test_resume_run_id_excludes_other_issues_and_repos(tmp_path):
    slug = "owner-repo"
    other_issue = tmp_path / ".worktrees" / f"orbi-{slug}-issue-4-ffff0000"
    other_repo = tmp_path / ".worktrees" / f"orbi-other-other-issue-3-ffff1111"
    other_issue.mkdir(parents=True)
    other_repo.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="ffff0000", issue=4, branch="b1", worktree=other_issue, source_repo="owner/repo"))
    run_state.write_run_state(RunContext(run_id="ffff1111", issue=3, branch="b2", worktree=other_repo, source_repo="other/other"))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) is None


def test_resume_run_id_fails_fast_on_missing_state_file(tmp_path):
    """A worktree that claims the issue number but has NO run state file
    cannot be verified as the same run: fail fast with the exact reason,
    never a silent fresh redo (Issue #219)."""
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    worktree.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="run state"):
        runner.resume_run_id(tmp_path, "owner/repo", 3)


def test_resume_run_id_fails_fast_on_corrupt_state_file(tmp_path):
    worktree = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    worktree.mkdir(parents=True)
    state = run_state.run_state_path(worktree)
    state.parent.mkdir(parents=True)
    state.write_text("corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="run state"):
        runner.resume_run_id(tmp_path, "owner/repo", 3)


def test_resume_run_id_skips_unrelated_worktrees_without_a_state_file(
    tmp_path,
):
    """A worktree of ANOTHER issue without a state file (a legacy
    completed run) is not the resume scene of this issue — it is
    skipped, not a fail-fast (only a worktree claiming THIS issue
    number must be verifiable)."""
    other = tmp_path / ".worktrees" / "orbi-owner-repo-issue-4-ffff0000"
    other.mkdir(parents=True)
    mine = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-aaaa1111"
    mine.mkdir(parents=True)
    run_state.write_run_state(RunContext(run_id="aaaa1111", issue=3, branch="b1", worktree=mine, source_repo="owner/repo"))
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "aaaa1111"


def test_has_in_progress_label_checks_the_issue_label(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps({"labels": [{"name": "ai-in-progress"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.has_in_progress_label(4, "owner/repo") is True
    assert calls == [[
        "gh", "issue", "view", "4", "--repo", "owner/repo",
        "--json", "labels",
    ]]


def test_has_in_progress_label_is_false_without_the_label(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps(
            {"labels": [{"name": "ai-fix-needed"}]},
        ),
    )
    assert runner.has_in_progress_label(4, "owner/repo") is False


def test_has_in_progress_label_fails_fast_on_malformed_output(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    # Issue #785: the shared `issue_view` validates the object shape, so
    # the message wording is the view's ("must be"), not the caller's.
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.has_in_progress_label(4, "owner/repo")


def test_has_in_progress_label_fails_fast_on_malformed_labels(monkeypatch):
    """view 返回 JSON 对象但 labels 字段不是数组（Issue #658）：解析
    失败必须 fail-fast，绝不落入默认 False（那会让竞态守卫失明）。"""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"labels": "ai-in-progress"}),
    )
    with pytest.raises(ValueError, match="labels must be a JSON array"):
        runner.has_in_progress_label(4, "owner/repo")


def test_has_in_progress_label_reads_directly_not_via_the_search_index(
    monkeypatch,
):
    """Issue #658：认领前复查必须直读（gh issue view，强一致）——不能走
    与扫描同一套最终一致的搜索索引：另一实例几秒前刚打上的
    ai-in-progress，索引可能尚未收录，复查必须看到它才能让路。"""
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    assert runner.has_in_progress_label(4, "owner/repo") is True
    # The fixed path reads the Issue directly — the search index (which
    # has not ingested the other instance's claim yet) is never queried.
    assert calls == [[
        "gh", "issue", "view", "4", "--repo", "owner/repo",
        "--json", "labels",
    ]]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run_command(["gh", "issue", "list", "--search",
                          "label:ai-in-progress"])


def _claim_race_deps(monkeypatch, tmp_path, *, freeze_side_effect=None,
                     stable_branch_seq=None):
    """Minimal process_issue deps for the #658 claim-race tests.

    Everything after the claim decision is irrelevant (the guard must
    fire before any of it); run_pi returning normally keeps the
    unguarded path moving so its downstream label writes become
    observable — that is exactly what the red assertions catch.
    """
    if stable_branch_seq is not None:
        seq = list(stable_branch_seq)
        # Exactly two probes consume the sequence: the scan-time probe
        # and the pre-claim guard (Issue #658).
        monkeypatch.setattr(seam, "stable_branch_exists",
            lambda repo_dir, branch: seq.pop(0),
        )
    if freeze_side_effect is not None:
        def fake_freeze_base(repo_dir, base_branch):
            freeze_side_effect()
            return "0123456789abcdef0123456789abcdef01234567"
        monkeypatch.setattr(seam, "freeze_base", fake_freeze_base)
    else:
        monkeypatch.setattr(seam, "freeze_base",
            lambda repo_dir, base_branch:
                "0123456789abcdef0123456789abcdef01234567",
        )
    monkeypatch.setattr(seam, "new_run_id", lambda: "feedface",
    )
    # create_worktree/run_pi stay REAL: on the fixed path the guard
    # yields before either is reached, and a regression that skips the
    # guard then fails loudly on the real calls (tmp_path is no git
    # repo) — no dead fake arms.


def make_claim_race_gh(monkeypatch, state):
    """The gh fake for the #658 claim-race world.

    The direct read (`gh issue view`) answers the LIVE label truth from
    `state["in_progress"]`; the search index (`gh issue list`) answers
    from the same state — a lagging index is modeled by flipping the
    state only AFTER the pickup scan, exactly when the other instance's
    claim lands. One shared guard line, driven by its own test below.
    """
    def fake_run_command(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            labels = ([{"name": "ai-in-progress"}]
                      if state["in_progress"] else [])
            return json.dumps({"labels": labels})
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps(
                [{"number": 18}] if state["in_progress"] else [],
            )
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([])
        if command[:2] == ["git", "ls-remote"]:
            # The scan-time stable-branch probe (before the race lands).
            return ""
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                return json.dumps([])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return fake_run_command


def test_claim_race_gh_answers_every_expected_command(monkeypatch):
    """The shared #658 fake is a contract, not a sink: every branch it
    answers is one the claim flow issues — driven here (in_progress=True
    arm; the False arm is driven by the two yield tests) so no arm is
    dead in the fixed world, and anything else fails loud."""
    fake = make_claim_race_gh(monkeypatch, {"in_progress": True})
    assert json.loads(fake([
        "gh", "issue", "view", "18", "--repo", "o/r", "--json", "labels",
    ])) == {"labels": [{"name": "ai-in-progress"}]}
    assert json.loads(fake([
        "gh", "issue", "list", "--search", "label:ai-in-progress",
    ])) == [{"number": 18}]
    assert json.loads(fake(["gh", "pr", "list", "--state", "open"])) == []
    assert fake([
        "git", "ls-remote", "--heads", "origin", "refs/heads/x",
    ]) == ""
    assert json.loads(fake([
        "gh", "api", "repos/o/r/issues/18/comments",
    ])) == []
    assert fake([
        "gh", "api", "repos/o/r/issues/18/comments",
        "--method", "POST", "--field", "body=x",
    ]) == ""
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "release", "view"])


def test_process_issue_yields_when_the_label_lands_mid_preparation(
    monkeypatch, tmp_path,
):
    """Issue #658：扫描与认领写之间隔着 freeze_base 的秒级网络往返；
    另一实例在该窗口内认领（ai-in-progress 出现）时，本实例必须让路
    ——不打任何标签、不发失败评论、绝不能把胜者打成 ai-blocked。"""
    state = {"in_progress": False}
    make_claim_race_gh(monkeypatch, state)
    _claim_race_deps(
        monkeypatch, tmp_path,
        freeze_side_effect=lambda: state.__setitem__("in_progress", True),
    )
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def test_process_issue_yields_when_the_stable_branch_lands_mid_preparation(
    monkeypatch, tmp_path,
):
    """Issue #658 的撞分支面：扫描时 stable branch 尚不存在，freeze_base
    的窗口内被另一实例创建（它先认领）——认领写之前必须复查到并让路，
    而不是径直 create_worktree 撞分支后走 ai-blocked 失败路径。"""
    make_claim_race_gh(monkeypatch, {"in_progress": False})
    # First probe (scan-time): absent. Second probe (the pre-claim
    # guard): the other instance created it while we were preparing.
    _claim_race_deps(
        monkeypatch, tmp_path, stable_branch_seq=[False, True],
    )
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def _release_race_deps(monkeypatch, in_progress: bool, live_holders: list):
    """Issue #708 fakes: the direct read (`gh issue view`) answers the
    LIVE label truth; `slot_occupancy` answers the runner-liveness
    truth (`None`/own pid = alone, another pid = a live co-runner)."""
    def fake_run_command(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            labels = ([{"name": "ai-in-progress"}]
                      if in_progress else [])
            return json.dumps({"labels": labels})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(
        seam, "slot_occupancy", lambda *a, **k: list(live_holders),
    )
    return fake_run_command


def _release_issue() -> dict:
    return {"number": 30, "title": "Release 1.2.3", "body": "## Release",
            "labels": [{"name": "ai-release"}, {"name": "ai-ready"}]}


def _release_dispatch_config(tmp_path) -> dict:
    return config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)


def _spy_process_release(monkeypatch) -> list:
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return "done"

    import orbi.release as release_module
    monkeypatch.setattr(release_module, "process_release", spy)
    return calls


def test_release_dispatch_yields_when_in_progress_and_another_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #708：ready 扫描的滞后快照可把已被认领的发布票递给第二个
    活实例。开发路径在 #658 拿到了直读让路；发布分发同样必须让路——
    在途且另一活 runner 持槽 = 该发布正在被做，本 tick 退出，绝不与
    状态机并发（成功发布不会被败者改写成 ai-blocked）。"""
    _release_race_deps(
        monkeypatch, in_progress=True,
        live_holders=[(tmp_path / "slot-0", 424242)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("claim-yielded", None)
    assert calls == [], "the release state machine must not start"


def test_release_dispatch_resumes_orphan_when_no_other_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #708 的另一半：在途但无其他活 runner = 死 runner 留下的
    孤儿发布——照旧进 process_release 复用 run_id 续跑（#98 的重启
    resume 语义不因让路守卫而丢失）。"""
    _release_race_deps(
        monkeypatch, in_progress=True,
        live_holders=[(tmp_path / "slot-0", None)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("release", "done")
    assert len(calls) == 1


def test_release_dispatch_fresh_ticket_unchanged_by_the_guard(
    monkeypatch, tmp_path,
):
    """直读没有 ai-in-progress（新票）时，守卫零作用——照常分发；
    活 runner 存在与否只对在途票有意义。"""
    _release_race_deps(
        monkeypatch, in_progress=False,
        live_holders=[(tmp_path / "slot-0", 424242)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("release", "done")
    assert len(calls) == 1


def test_release_race_gh_rejects_unexpected_commands(monkeypatch):
    """The #708 fake is a contract, not a sink: the strict-raise arm is
    driven here so it cannot silently rot into a permissive fake that
    answers commands the dispatch no longer issues (the #658 fake keeps
    the same discipline via its own contract test)."""
    fake = _release_race_deps(monkeypatch, in_progress=True, live_holders=[])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "release", "view"])


def test_another_live_runner_reads_slot_occupancy(monkeypatch, tmp_path):
    """_another_live_runner 是 #39 存活规则的独立助手：别的 pid 持槽
    =True，None/自己 =False（与 pick_in_progress_issue 同一语义，
    runner.py:2412 的循环抽出来共用）。"""
    slot_dir = tmp_path / "slots"
    slot_dir.mkdir()
    seen = {}

    def foreign(directory, limit):
        seen["args"] = (directory, limit)
        return [(slot_dir / "slot-0", 424242)]

    monkeypatch.setattr(seam, "slot_occupancy", foreign)
    assert runner._another_live_runner(slot_dir, 2) is True
    assert seen["args"] == (slot_dir, 2)
    monkeypatch.setattr(
        seam, "slot_occupancy",
        lambda d, m: [(slot_dir / "slot-0", None),
                      (slot_dir / "slot-1", os.getpid())],
    )
    assert runner._another_live_runner(slot_dir, 2) is False


def test_process_issue_resumes_existing_run_and_same_progress_comment(
    monkeypatch, tmp_path,
):
    """Restart resume (Issue #18): the runner died with the task worktree
    and the `ai-in-progress` label behind. The re-claim reuses the
    newest worktree's run id, so the same hidden-marker progress comment
    is found by its marker and PATCHed — never a second one."""
    calls = []
    existing_comment = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nstale state from the dead run"
        ),
    }
    gh_calls, posted = make_fake_gh(
        monkeypatch, comments=[existing_comment], in_progress=True,
    )
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # The existing progress comment of the dead run.
                return json.dumps([existing_comment])
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            if command[-1] == "state":
                # The pre-PR closeout reads the source Issue state
                # (Issue #746): open in this scene.
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # The Issue still carries `ai-in-progress` (the runner died).
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(("edit", args, kwargs)),
    )
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    # The fresh id would be different: the resume must override it with
    # the newest worktree's run id.
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    assert runner.process_issue(
        issue, config, "xqliu/orbi-backlog",
    ) == runner.IssueResult("pr", "https://github.com/orbi-build/orbi/pull/4")
    # The reused run id drives the branch and the scene comments.
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/orbi-backlog",
                                       "add": "ai-in-progress"})
    scene_comments = [
        call for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    for call in scene_comments:
        assert "<!-- orbi:run=a1b2c3d4 -->" in call[-1]
        assert "ffffeeee" not in call[-1]
    # No new progress comment: the restarted run PATCHed the existing
    # one (id 77) found by its run marker.
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in gh_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi-backlog/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the existing progress comment was not updated"
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    # Issue #1733: the PR is open and waiting for checks and review.
    assert "waiting for checks and review" in last_body
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in last_body


def test_process_issue_second_tick_behind_the_index_resumes_not_reclaims(
    monkeypatch, tmp_path, caplog,
):
    """Issue #658 单实例面（xqliu 09-11 生产现场，orbi#702 并入 #658）：
    同一 timer 的两次 tick 竞争同一张票——第一次 tick 早已完成认领
    （`ai-in-progress` 落位在本 tick 之前，worktree 的 run 在飞），而搜索
    索引滞后，把这张票连同陈旧的 `ai-ready` labels 快照一起递给本 tick。
    认领复查的 `gh issue view` 直读必须看到标签：本 tick 走 resume 复用
    在飞 run 的 id，绝不 fresh claim（不消费新 run_id、不建第二个分支
    把在途交付撞成 push rejected → ai-blocked）——修复不依赖
    `max_concurrency` 的取值，单实例双 tick 与双实例同根因。"""
    existing_comment = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nfirst tick's run is in flight"
        ),
    }
    gh_calls, posted = make_fake_gh(
        monkeypatch, comments=[existing_comment], in_progress=True,
    )
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # The in-flight run's progress comment.
                return json.dumps([existing_comment])
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            # The strongly-consistent direct read: the label the first
            # tick wrote IS live truth, no matter how stale the index
            # that delivered the Issue was. The pre-PR closeout reads
            # the state too (Issue #746): open in this scene.
            if command[-1] == "state":
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #4\n\nPlan",
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    # If a regression makes this tick a FRESH claim, the fresh id leaks
    # into the scene comments and the assertions below catch it.
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    # The stale scan snapshot: still showing ai-ready even though the
    # first tick already claimed the Issue.
    issue = {"number": 4, "title": "Fix", "body": "Body",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        result = runner.process_issue(
            issue, config, "xqliu/orbi-backlog",
        )
    assert result == runner.IssueResult(
        "pr", "https://github.com/orbi-build/orbi/pull/4",
    )
    # The direct read routed this tick into the resume: the in-flight
    # run's id owns every scene comment — a fresh claim never starts.
    assert "fresh_claim_route" not in caplog.text
    assert "resuming_run" in caplog.text
    # And the claim recheck never asked the lagging index for the label
    # (the pre-#658 list search): the direct read is the only source.
    assert not any(
        command[:3] == ["gh", "issue", "list"] for command in gh_calls
    )
    scene_bodies = [
        call[-1] for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    assert scene_bodies, "no scene comment was published"
    for body in scene_bodies:
        assert "ffffeeee" not in body
    # And the claim-time label write stayed the shared idempotent patch
    # (run=a1b2c3d4 marker), never a second run's footprint.


def test_process_issue_binds_run_id_before_the_resume_scan(
    monkeypatch, tmp_path, caplog,
):
    """Run correlation (Issue #41): EVERY journal line of the attempt
    carries the `[run_id]` prefix — including the claim-time lines of a
    restart resume (the `ai-in-progress` label scan and `resuming_run`),
    which run before the resume decision (review round 3, PR #42)."""
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    head = "0123456789abcdef0123456789abcdef01234567"
    branch = "orbi/xqliu-orbi-backlog-issue-4"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123",
    )
    # The fresh id differs from the reused one: both are valid run ids
    # of this attempt (generated, then replaced by the resumed run).
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    # No unprefixed line: the claim-time gh scan and resuming_run are
    # part of the attempt's timeline.
    for message in caplog.messages:
        assert message.startswith("[ffffeeee]") \
            or message.startswith("[a1b2c3d4]"), message
    # The resume decision itself is logged under the REUSED run id.
    resuming = [m for m in caplog.messages if "resuming_run" in m]
    assert len(resuming) == 1
    assert resuming[0].startswith("[a1b2c3d4]")


def test_process_issue_starts_fresh_run_when_the_label_is_gone(
    monkeypatch, tmp_path,
):
    """A completed run keeps its worktree as evidence but loses the
    `ai-in-progress` label: re-claiming the Issue must start a fresh
    run, even when old worktrees exist (Issue #18)."""
    gh_calls, posted = make_fake_gh(monkeypatch)  # in_progress=False
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # No `ai-in-progress` label: the previous run finished.
            return "[]"
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": "orbi/xqliu-orbi-backlog-issue-4",
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=ffffeeee -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-orbi-backlog-issue-4"
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    scene_calls = []
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: scene_calls.append(1) or None,
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    # The fresh run id is used and the old worktree's run id is never
    # consulted (the label is the gate).
    assert scene_calls == []
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- run_id=ffffeeee" in progress_posts[0]


def test_process_issue_keeps_fresh_run_when_no_worktree_survived(
    monkeypatch, tmp_path, caplog,
):
    """The label survived a kill but the worktree is gone (e.g. cleaned
    up): there is nothing to resume, so the run starts fresh — the
    `ai-in-progress` label alone never resurrects a run id."""
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": "orbi/xqliu-orbi-backlog-issue-4",
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=ffffeeee -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-orbi-backlog-issue-4"
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    # The label is on, but no task worktree survived the kill.
    monkeypatch.setattr(runner, "worktree_resume_scene", lambda *a, **k: None)
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    # No resume happened: the fresh run id drives the delivery.
    assert "resuming_run" not in caplog.text
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- run_id=ffffeeee" in progress_posts[0]


def _resume_wiring_setup(monkeypatch, tmp_path, *, in_progress: bool,
                          latest_run_id_result="a1b2c3d4",
                          git_status: str = ""):
    """Shared fake-gh/git wiring for the Issue #219 process_issue tests.

    Returns `(gh_calls, posted, worktree, comment_bodies)`: `posted`
    carries the progress-comment bodies (`gh api` POST), `comment_bodies`
    the plain `gh issue comment` bodies (scene + failure comments).
    `git_status` is the `git status --porcelain` answer (the
    worktree's uncommitted changes).
    """
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=in_progress)
    # The branch carries the run id the attempt actually uses: the
    # resumed one (label on + a worktree to resume) or the fresh one.
    run_id = ("a1b2c3d4"
              if in_progress and latest_run_id_result else "ffffeeee")
    branch = f"orbi/xqliu-orbi-backlog-issue-4"
    comment_bodies = []

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [
                {"name": "ai-in-progress"}] if in_progress
                else [{"name": "ai-ready"}],
            })
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps(
                [{"number": 4}] if in_progress else [],
            )
        if command[:3] == ["gh", "issue", "comment"]:
            comment_bodies.append(command[-1])
            return ""
        if command[:3] == ["git", "status", "--porcelain"]:
            return git_status
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: (
            (latest_run_id_result, worktree)
            if latest_run_id_result else None
        ),
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: worktree,
    )
    return gh_calls, posted, worktree, comment_bodies


def test_process_issue_writes_run_state_and_resume_context(
    monkeypatch, tmp_path, caplog,
):
    """Issue #219: the interrupted run continues on the EXISTING work —
    the run state file is written (the same-run marker), the resume
    context (uncommitted changes + previous session progress) reaches
    the new session, and the `resume_continue` line is logged."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=True,
        git_status="?? src/a.py\n",
    )
    # The interrupted work: uncommitted changes + a previous session.
    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    make_session_jsonl(worktree, "sess-1")
    resume_contexts = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/orbi-build/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    # The run state file marks the worktree as the same run.
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 4
    assert state["repo"] == "xqliu/orbi-backlog"
    # The new session starts from the existing work.
    assert len(resume_contexts) == 1
    assert resume_contexts[0] is not None
    assert "src/a.py" in resume_contexts[0]
    assert "sess-1" in resume_contexts[0]
    # The structured resume line is logged (auditable, Issue #219).
    lines = [
        m for m in caplog.messages if "resume_continue" in m
    ]
    assert len(lines) == 1
    assert f"worktree={worktree}" in lines[0]
    assert "changed_files=1" in lines[0]
    assert "reused_runs=1" in lines[0]
    assert "previous_session=sess-1" in lines[0]


def test_process_issue_fresh_run_has_no_resume_context(
    monkeypatch, tmp_path, caplog,
):
    """A fresh claim (no `ai-in-progress`) gets a run state file too
    (so a later interruption can resume), but NO resume context and NO
    `resume_continue` line — the prompt is the exact pre-#219 shape."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=False,
    )
    resume_contexts = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/orbi-build/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "ffffeeee"
    assert resume_contexts == [None]
    assert "resume_continue" not in caplog.text


def test_process_issue_fails_fast_when_the_run_state_is_missing(
    monkeypatch, tmp_path, caplog,
):
    """Issue #219: the worktree of this issue exists but its run state
    file is gone — the same run cannot be verified. The attempt fails
    fast through the terminal failure path (`ai-blocked` + the reason
    comment): no fresh run, no silent redo."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=True,
    )

    def failing_resume_scene(repo_dir, source_repo, number):
        raise RuntimeError(
            f"worktree {worktree} has no run state file "
            "(.orbi/run-state.json): the same run cannot be "
            "verified (Issue #219)"
        )

    monkeypatch.setattr(runner, "worktree_resume_scene", failing_resume_scene)
    run_pi_calls = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: run_pi_calls.append(1) or "done",
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="run state"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    # No fresh run was started.
    assert run_pi_calls == []
    # The terminal state is `ai-blocked` (the claim label removed).
    assert edits[-1] == ((4,), {
        "repo": "xqliu/orbi-backlog", "add": "ai-blocked",
        "remove": "ai-in-progress",
    })
    # The failure comment carries the reason and the run marker.
    failed = [
        body for body in comment_bodies
        if body.startswith("<!-- orbi:run=")
        and "Orbi failed" in body
    ]
    assert len(failed) == 1
    assert "cannot continue the interrupted run" in failed[0]
    assert "run state" in failed[0]
    assert "resume_continue_failed" in caplog.text


def test_worktree_path_lives_inside_repo_worktrees_and_includes_run_id():
    repo_dir = Path("/srv/orbi/orbi")
    path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    assert path == repo_dir / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    assert Path(tempfile.gettempdir()) not in path.parents


def test_worktree_path_keeps_source_repo_in_name_to_avoid_same_number_collision():
    repo_dir = Path("/srv/orbi/orbi")
    pilot = runner.worktree_path(repo_dir, "xqliu/orbi", 14, "run1")
    ceo = runner.worktree_path(repo_dir, "xqliu/orbi-backlog", 14, "run1")
    assert pilot == repo_dir / ".worktrees" / "orbi-xqliu-orbi-issue-14-run1"
    assert ceo == repo_dir / ".worktrees" / "orbi-xqliu-orbi-backlog-issue-14-run1"
    assert pilot != ceo


def test_worktree_path_and_task_branch_differ_per_run_for_same_issue():
    repo_dir = Path("/srv/orbi/orbi")
    first_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    retry_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run2")
    assert first_path != retry_path
    assert runner.task_branch("owner/repo", 3, "run1") == "orbi/owner-repo-issue-3"
    assert runner.task_branch("owner/repo", 3, "run2") == runner.task_branch("owner/repo", 3, "run1")


def test_task_branch_includes_source_repo_to_avoid_same_number_collision():
    assert runner.task_branch("owner/pilot", 1, "run1") == "orbi/owner-pilot-issue-1"
    assert runner.task_branch("owner/pilot", 1, "run1") != runner.task_branch("owner/ceo", 1, "run1")


def test_claim_route_decision_table():
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=False, open_pr=False) == "implement"
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=True, open_pr=False) == "implement"
    assert runner.claim_route({runner.READY_LABEL}, branch_exists=True, open_pr=True) == "review"
    assert runner.claim_route({runner.PR_OPENED_LABEL}, branch_exists=True, open_pr=True) == "review"
    assert runner.claim_route({runner.FIX_NEEDED_LABEL}, branch_exists=True, open_pr=True) == "review"


def test_open_pr_for_branch_rejects_malformed_and_ambiguous_results(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: "{}")
    with pytest.raises(RuntimeError, match="must return an array"):
        runner.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-3")

    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: "[{}, {}]",
    )
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        runner.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-3")


def test_comment_issue_runs_gh_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command))
    runner.comment_issue(3, repo="xqliu/orbi-backlog", body="done")
    assert calls == [[
        "gh", "issue", "comment", "3", "--repo", "xqliu/orbi-backlog",
        "--body", "done\n\n<!-- runner="
        + progress.runner_fingerprint() + " -->",
    ]]


def test_issue_context_uses_owner_repo_number_form():
    assert runner.issue_context("xqliu/orbi", 40) == (
        "xqliu/orbi#40"
    )


def test_run_pi_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the implementer prompt carries the absolute path of
    # the shared base-sync lock so Pi's base freshness fetch can run
    # under it (flock <lock> git fetch origin <base>).
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert rendered_prompt(command) == (
        "SYSTEM " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_logs_provider_config_loaded_with_selection(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: run_pi logs `provider_config_loaded` with the
    configured provider/model identifiers (the same non-sensitive
    values as the redacted command line) before Pi is spawned."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", pi_provider="local-qwen", pi_model="qwen3.8:27b")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "issue=owner/repo#4" in line
    assert "role=implement" in line
    assert "provider=local-qwen" in line
    assert "model=qwen3.8:27b" in line
    assert "elapsed=" in line


def test_run_pi_logs_provider_config_loaded_unconfigured(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: without a configured provider/model the line still
    fires (Pi keeps its own agent dir) with `-` placeholders."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "provider=-" in lines[0]
    assert "model=-" in lines[0]
    assert "role=implement" in lines[0]


def test_run_review_logs_provider_config_loaded_with_review_role(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: the review session logs the same line with
    role=review (one run_id, the roles are steps of the same run)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "ok")
    with caplog.at_level("INFO"):
        pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b").run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b"), 1)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=review" in lines[0]
    assert "issue=owner/repo#4" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_run_review_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the review prompt carries the SAME lock path (the
    # review session's base merge fetch must not race the shared ref).
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text(
        "REVIEW {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "ok")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


# --- Issue #745: {{ISSUE_COMMENTS}} — the trusted-comment timeline -----------

def test_trusted_issue_comments_block_filters_untrusted_and_keeps_order(
    monkeypatch,
):
    """Issue #745: only trusted authors (the #45 authorAssociation set
    or the runner's own App) enter the task context — a public repo
    lets anyone comment, and an unfiltered injection would be a prompt
    injection surface. The timeline order is preserved (oldest first)."""
    # A NONE-association comment carrying a login reaches the
    # authenticated-login fallback; pin it so the test never depends on
    # the host's real gh login state (CI runs unauthenticated).
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "first decision"},
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "second decision"},
    ]
    block = github.trusted_issue_comments_block(comments, limit=20)
    assert "public drive-by" not in block
    assert "mallory" not in block
    first = block.index("- alice (OWNER) at 2026-09-12T01:00:00Z:")
    second = block.index("- carol (MEMBER) at 2026-09-12T03:00:00Z:")
    assert block.index("first decision") < second < block.index(
        "second decision",
    )
    # Below the limit: no omission note, the timeline speaks for itself.
    assert "omitted" not in block


def test_trusted_issue_comments_block_truncates_to_the_newest_with_a_visible_note():
    """Issue #745: over the limit the OLDEST trusted comments are
    dropped (the newest carry the latest decision) and the omission is
    stated in the block — the agent must know it did not see the full
    history, never a silent truncation."""
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "oldest decision"},
        {"author": {"login": "bob"}, "authorAssociation": "MAINTAINER",
         "createdAt": "2026-09-12T02:00:00Z", "body": "middle decision"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "newest decision"},
    ]
    block = github.trusted_issue_comments_block(comments, limit=2)
    assert block.startswith(
        "(1 older trusted comment omitted; showing the 2 most recent)",
    )
    assert "newest decision" in block
    assert "middle decision" in block
    assert "oldest decision" not in block


def test_trusted_issue_comments_block_states_when_nothing_is_trusted(
    monkeypatch,
):
    """Issue #745: zero trusted comments produce an explicit marker, not
    an empty string — the agent can tell an empty timeline apart from a
    missing section."""
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    block = github.trusted_issue_comments_block([
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
    ], limit=20)
    assert block == "(no trusted comments)"
    block = github.trusted_issue_comments_block([], limit=20)
    assert block == "(no trusted comments)"


def test_run_pi_injects_trusted_issue_comments_into_the_prompt(
    monkeypatch, tmp_path,
):
    """Issue #745: a template carrying {{ISSUE_COMMENTS}} receives the
    Issue's trusted-comment timeline — fetched from GitHub, maintainer
    comments only, capped by the configured `issue_comments_limit`
    with a visible omission note."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_COMMENTS}}", encoding="utf-8")
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "oldest decision"},
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "middle decision"},
        {"author": {"login": "dave"}, "authorAssociation": "MAINTAINER",
         "createdAt": "2026-09-12T04:00:00Z", "body": "newest decision"},
    ]
    fetches = []

    def fake_issue_comments(number, *, repo):
        fetches.append((number, repo))
        return comments

    monkeypatch.setattr(seam, "issue_comments", fake_issue_comments)
    # mallory's NONE-association comment reaches the authenticated-login
    # fallback; pin it so the test never depends on the host's real gh
    # login state (CI runs unauthenticated).
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", issue_comments_limit=2)
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    assert fetches == [(4, "owner/repo")]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "(1 older trusted comment omitted; showing the 2 most recent)"
        in system_prompt
    )
    assert "newest decision" in system_prompt
    assert "middle decision" in system_prompt
    assert "oldest decision" not in system_prompt
    assert "public drive-by" not in system_prompt
    # The context argument keeps its exact pre-#745 shape — the comments
    # reach the agent through the template placeholder, nowhere else.
    assert command[-1] == (
        "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a template without {{ISSUE_COMMENTS}} keeps the exact
    pre-#745 behavior — no comments GitHub read, no new failure mode
    (backward compatibility for custom templates)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == "SYSTEM b" + LANGUAGE_SUFFIX


def test_run_review_refreshes_issue_body_each_round(monkeypatch, tmp_path):
    """Issue #1273: each review round receives the Issue body as it reads
    now, so deleting an acceptance criterion takes effect on the next round."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    bodies = iter(["old criterion", "new criterion {{BASE_SYNC_LOCK}}"])
    issue_reads = []
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: issue_reads.append((number, fields, repo))
        or {"body": next(bodies)},
    )
    prompts = []
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        # The prompt is read through the FILE the argv names (Issue
        # #1634) at call time: each round overwrites the same prompt
        # file in the shared session dir, so the read has to happen
        # while the round is running.
        lambda command, **kwargs: prompts.append(rendered_prompt(command)) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    pi_session.run_review(ctx, pr, config, 1)
    pi_session.run_review(ctx, pr, config, 2)
    assert issue_reads == [(4, "body", "owner/repo"), (4, "body", "owner/repo")]
    assert prompts == [
        "BODY=old criterion" + LANGUAGE_SUFFIX,
        "BODY=new criterion {{BASE_SYNC_LOCK}}" + LANGUAGE_SUFFIX,
    ]
    assert "old criterion" not in prompts[1]


def test_run_review_rejects_a_non_string_issue_body(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: {"body": 42},
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    with pytest.raises(ValueError, match="issue body must be a string"):
        pi_session.run_review(ctx, pr, config, 1)


def test_run_review_combines_current_pr_feedback_with_issue_comments(
    monkeypatch, tmp_path,
):
    """The current PR's normalized feedback shares one bounded timeline."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda number, *, repo: [
        {"author": {"login": "alice"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "old issue decision"},
    ])
    pr_fetches = []
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "conversation")) or [])
    monkeypatch.setattr(seam, "pr_reviews", lambda number, *, repo:
                        pr_fetches.append((number, repo, "reviews")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "submitted_at": "2026-09-12T02:00:00Z",
         "state": "CHANGES_REQUESTED", "body": "review body"},
    ])
    monkeypatch.setattr(seam, "pr_review_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "inline")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "created_at": "2026-09-12T03:00:00Z", "path": "src/x.py",
         "line": 7, "body": "inline fix"},
    ])
    monkeypatch.setattr(
        seam, "_authenticated_github_login", lambda: "ci-runner[bot]"
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(), issue_comments_limit=2,
    )
    pi_session.run_review(
        RunContext(run_id=config.run_id, issue=4, branch="branch",
                   worktree=tmp_path, source_repo="owner/repo"),
        {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1,
    )
    assert pr_fetches == [
        (9, "owner/repo", "conversation"),
        (9, "owner/repo", "reviews"),
        (9, "owner/repo", "inline"),
    ]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "1 older trusted comment omitted; showing the 2 most recent"
        in system_prompt
    )
    assert "old issue decision" not in system_prompt
    assert "review body" in system_prompt
    assert "[CHANGES_REQUESTED]" in system_prompt
    assert "inline fix" in system_prompt
    assert "src/x.py:7" in system_prompt


def test_run_review_pr_feedback_failure_logs_and_keeps_issue_comments(
    monkeypatch, tmp_path, caplog,
):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda *args, **kwargs: [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "issue decision"},
    ])
    monkeypatch.setattr(seam, "pr_comments", lambda *args, **kwargs: [])

    def fail_reviews(*args, **kwargs):
        raise RuntimeError("reviews unavailable")

    monkeypatch.setattr(seam, "pr_reviews", fail_reviews)
    calls = []
    monkeypatch.setattr(seam, "stream_pi",
                        lambda command, **kwargs: calls.append(command) or "ok")
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    with caplog.at_level(logging.ERROR):
        pi_session.run_review(
            RunContext(run_id=config.run_id, issue=4, branch="branch",
                       worktree=tmp_path, source_repo="owner/repo"),
            {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config, 1,
        )
    system_prompt = rendered_prompt(calls[0])
    assert "issue decision" in system_prompt
    assert "pr_review_feedback_read_failed repo=owner/repo pr=9" in caplog.text


def test_run_review_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a review template without {{ISSUE_COMMENTS}} keeps
    the exact pre-#745 behavior (no comments GitHub read)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{BASE_SYNC_LOCK}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_injects_base_branch_sha_and_run_id_into_prompt(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{SOURCE_REPO}} {{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{ISSUE_BODY}} "
        "{{WORKSPACE_ROOT}} {{CONTEXT_FILES}} {{SKILLS}} {{BASE_BRANCH}} "
        "{{BASE_SHA}} {{RUN_ID}} {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=("context.md",), skills=("skill.md",), base_branch="main", base_sha="abc123def456", run_id="run1")
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    command, kwargs = calls[0]
    assert command[:5] == ["pi", "--no-extensions", "--skill", "skill.md", "--print"]
    system_prompt = rendered_prompt(command)
    assert "owner/repo" in system_prompt
    assert " 4 " in system_prompt
    assert "Fix title" in system_prompt
    assert "Fix body" in system_prompt
    assert "context.md" in system_prompt
    assert "skill.md" in system_prompt
    assert system_prompt.endswith(LANGUAGE_SUFFIX)
    assert (
        "main abc123def456 run1 "
        + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
    ) in system_prompt
    assert command[9] == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] is None
    assert kwargs["ctx"].run_id == "run1"
    assert kwargs["ctx"].issue == 4
    assert kwargs["ctx"].source_repo == "owner/repo"
    assert kwargs["ctx"].branch == "orbi/owner-repo-issue-4"
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_task_branch_to_stream_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(kwargs) or "done")
    issue = {"number": 5, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, timeout=7)
    assert calls[0]["ctx"].branch == "orbi/owner-repo-issue-5"
    assert calls[0]["timeout"] == 7


def test_run_pi_redacts_prompt_and_issue_from_command_log(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    pi_session.run_pi({"number": 5, "title": "secret", "body": "token"}, RunContext(run_id=config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1").run_id, issue={"number": 5, "title": "secret", "body": "token"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1"))
    command, kwargs = calls[0]
    system_prompt = rendered_prompt(command)
    assert "PRIVATE SYSTEM" in system_prompt
    assert "token" in system_prompt
    assert kwargs["log_command"] == [
        "pi", "--no-extensions", "--print", "--session-dir",
        str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_run_pi_keeps_the_fresh_context_without_a_resume_context(
    monkeypatch, tmp_path,
):
    """No resume context: the context argument is byte-identical to the
    pre-#219 shape (a fresh claim is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert command[-1] == (
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_appends_the_resume_context_to_the_context_argument(
    monkeypatch, tmp_path,
):
    """Issue #219: the continued run's new session starts from the
    existing work — the resume context is appended to the context
    argument (the prompt template itself is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    resume = (
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.\nPrevious session progress: session=sess-1 "
        "events=7 phase=test last_action=bash pytest last_result=ok\n"
        "Uncommitted changed files (2):\n- src/a.py\n- src/b.py"
    )
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, resume_context=resume)
    command = calls[0]
    context = command[-1]
    assert context.startswith(
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: " + str(tmp_path),
    )
    assert context.endswith(resume)


def test_verify_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "other-branch")
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.verify_pr(RunContext(run_id="e07383c2", issue=4, branch="orbi/issue-4", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


FAKE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
# Issue #1581: the commit-boundary tree comparison needs a delivered head
# tree distinct from the frozen base tree on the happy path.
FAKE_HEAD_TREE_SHA = "1111111111111111111111111111111111111111"
FAKE_BASE_TREE_SHA = "2222222222222222222222222222222222222222"
FAKE_RUN_ID = "e07383c2"


FAKE_PR_URL = "https://github.com/orbi-build/orbi/pull/4"
FAKE_PR_REPO = "orbi-build/orbi"


def fake_verify_pr_payload(**overrides) -> str:
    """One open PR in the production `gh pr list` shape."""
    payload = {
        "url": FAKE_PR_URL,
        "baseRefName": "main",
        "headRefName": f"orbi/issue-4-{FAKE_RUN_ID}",
        "headRefOid": FAKE_HEAD_SHA,
        "headRepository": {"name": "orbi"},
        "headRepositoryOwner": {"login": "orbi-build"},
        "body": (
            f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
            "Fixes #4\n\nPlan"
        ),
    }
    payload.update(overrides)
    return json.dumps([payload])


def fake_verify_run(command, **kwargs):
    """Complete fake for verify_pr: git commands answered, gh returns a PR."""
    if command[:3] == ["git", "branch", "--show-current"]:
        return f"orbi/issue-4-{FAKE_RUN_ID}"
    if command[:3] == ["git", "fetch", "origin"]:
        return ""
    if command[:3] == ["git", "merge-base", "--is-ancestor"]:
        return ""
    if command[:3] == ["git", "rev-parse", "HEAD"]:
        return FAKE_HEAD_SHA
    if command[0] == "gh" and command[1] == "issue":
        # Issue #1300: a body without the current marker makes the
        # check read the Issue's TRUSTED comment history on the deliver
        # path too. The empty history keeps these rejections about the
        # body's marker — the read happened and knew no run of the line
        # — instead of about a call the fake refused to answer.
        return json.dumps({"comments": []})
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
    raise AssertionError(f"unexpected command: {command}")


def test_verify_pr_rejects_delivery_behind_latest_remote_base(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command, stderr="not an ancestor")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "base_branch=main" in caplog.text


def test_fake_verify_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_verify_run(["gh", "release", "list"])


def test_verify_pr_rejects_missing_pr(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[]",
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_non_array(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "{}",
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_returns_url_when_delivery_contains_latest_base(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == "https://github.com/orbi-build/orbi/pull/4"
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls


def test_verify_pr_requires_the_repo_dir_lock_location(
    monkeypatch, tmp_path,
):
    # Issue #171: the verify fetch updates the shared remote-tracking
    # ref, so the lock location (the deployment checkout's shared state
    # dir) must be explicit — there is no bypass path.
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    with pytest.raises(TypeError):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main")


def test_verify_pr_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the verify fetch runs under the SAME base-sync lock
    # (a concurrent probe must not acquire it while the fetch is in
    # flight) and the lock must be free again afterwards.
    held = []

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            held.append(not _probe_lock_free(tmp_path))
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


def test_verify_pr_rejects_pr_without_url(monkeypatch, tmp_path):
    # Issue #291: the shared base check runs first, so the fixture must
    # carry a valid base to actually reach the URL check.
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA,
        json.dumps([{"baseRefName": "main"}]),
    ])
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="open PR has no URL"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_pr_based_on_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(baseRefName="develop")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR base is develop, expected main",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def test_verify_pr_rejects_diverged_remote_pr_head(monkeypatch, tmp_path,
                                                   caplog):
    """Issue #50: a remote PR head that is NOT an ancestor of the local
    HEAD (the branch diverged) is still a failure: a plain push would
    be rejected and a force push is forbidden, so the resume must not
    continue on this branch."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            if command[3] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef":
                raise subprocess.CalledProcessError(
                    1, command, stderr="not an ancestor",
                )
            return ""
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRefOid="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError,
        match="PR head deadbeef.* is not local HEAD 01234567.*diverged",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_head_diverged" in caplog.text


def test_verify_pr_passes_through_when_local_head_ahead_of_pr_head(
        monkeypatch, tmp_path, caplog,
):
    """Issue #50 (the #158 `d13b0c56` scene): the local HEAD is AHEAD of
    the remote PR head (a commit made by a killed session that was
    never pushed). The verification logs the exact heads and PASSES
    THROUGH — the unpushed commit is preserved and the next review
    session pushes the task branch on the same PR (it never fails here,
    never force pushes and never creates a replacement PR)."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # The PR head IS an ancestor of the local HEAD (local is
            # ahead) — the #158 scene.
            return ""
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRefOid="ed72915ed72915ed72915ed72915ed72915ed7291",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        url = runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert url == FAKE_PR_URL
    assert "local_head_ahead_of_pr_head" in caplog.text
    assert "ed72915ed72915ed72915ed72915ed72915ed7291" in caplog.text
    assert FAKE_HEAD_SHA in caplog.text


def test_verify_pr_rejects_pr_body_without_run_marker(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body="no run marker here")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_run_marker_missing" in caplog.text


def test_verify_pr_rejects_pr_body_missing_field(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body=None)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


# ------------------------------------------- Fixes #N (Issue #53)


def test_verify_pr_accepts_pr_body_with_fixes_keyword(monkeypatch, tmp_path):
    """The contract keyword `Fixes #<issue>` in the body is accepted, so
    GitHub closes the source Issue natively when the PR merges."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\n\nFixes #4",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


@pytest.mark.parametrize("reference", ["Fixes #4", "Fixes 4"])
def test_verify_pr_accepts_hash_and_hashless_fixes_reference(
    monkeypatch, tmp_path, reference,
):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\\n\\n{reference}",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_rejects_pr_body_without_fixes_keyword(
    monkeypatch, tmp_path, caplog,
):
    """A body without `Fixes #<issue>` would leave the source Issue open
    after the merge: the delivery is rejected."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=f"<!-- orbi:run={FAKE_RUN_ID} -->\n\nPlan",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text
    assert "issue=4" in caplog.text


def test_verify_pr_rejects_pr_body_with_wrong_issue_number(
    monkeypatch, tmp_path, caplog,
):
    """`Fixes #9` does not close Issue #4: the keyword must point at the
    source Issue of this delivery."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=(
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #9"
                ),
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text


def test_verify_pr_rejects_pr_body_with_longer_issue_number(
    monkeypatch, tmp_path, caplog,
):
    """`Fixes #41` closes Issue 41, not Issue 4: the keyword must match
    the source Issue number exactly, not as a digit prefix (review F1)."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                body=(
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #41"
                ),
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)
    assert "pr_fixes_missing" in caplog.text


def test_verify_pr_external_mode_skips_marker_and_fixes_checks(
    monkeypatch, tmp_path,
):
    """Issue #608: an external takeover PR carries neither the run marker
    nor a `Fixes #<issue>` keyword (the contributor wrote the body) —
    `external_pr=True` verifies the same delivery WITHOUT those two body
    checks; the Runner closes the triage Issue itself after the merge."""
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return "fix/outer"
        if command[:3] == ["gh", "pr", "view"]:
            payload = json.loads(fake_verify_pr_payload(
                headRefName="fix/outer",
                body="please review my fix, thanks",
                isCrossRepository=True,
                state="OPEN",
                headRepository={"name": "orbi-fork"},
                headRepositoryOwner={"login": "contributor"},
            ))[0]
            return json.dumps(payload)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(
        RunContext(
            run_id=FAKE_RUN_ID, issue=4, branch="fix/outer",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO,
        expected_url=FAKE_PR_URL, external_pr=True,
    ) == FAKE_PR_URL


def test_verify_pr_normal_mode_still_requires_marker_and_fixes(
    monkeypatch, tmp_path,
):
    """The external-mode skip is opt-in: a Runner-owned PR without the
    body contract still fails the verification."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body="please review my fix")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="run marker"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path)


def _fake_takeover_view(monkeypatch, payload):
    """Fake the `gh pr view` of `external_takeover_pr` with `payload`."""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps(payload),
    )


def test_external_takeover_pr_fails_fast_on_a_malformed_view(
    monkeypatch, tmp_path,
):
    """A `gh pr view` response that is not an object is a corrupted
    scene: the claim fails fast (the same contract as the open-PR
    query), it never silently proceeds."""
    _fake_takeover_view(monkeypatch, ["not", "an", "object"])
    with pytest.raises(RuntimeError, match="did not return an object"):
        runner.external_takeover_pr(
            tmp_path, "<!-- orbi:external-pr:592 -->",
            "xqliu/orbi", "main",
        )


def test_external_takeover_pr_resolves_the_marked_pr(monkeypatch, tmp_path):
    """Issue #608: the triage marker routes the claim to the external PR —
    open and against the configured base, the takeover proceeds."""
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN",
        "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "main",
        "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    pr = runner.external_takeover_pr(
        tmp_path,
        "body text\n<!-- orbi:external-pr:592 -->\n",
        "xqliu/orbi", "main",
    )
    assert pr is not None
    assert pr["headRefName"] == "fix/outer"
    assert pr["url"] == "https://github.com/xqliu/orbi/pull/592"


def test_external_takeover_pr_ignores_a_closed_pr(monkeypatch, tmp_path):
    """The external PR is merged/closed already: no takeover — the claim
    proceeds as a fresh internal delivery."""
    _fake_takeover_view(monkeypatch, {
        "state": "CLOSED", "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "main", "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    assert runner.external_takeover_pr(
        tmp_path, "<!-- orbi:external-pr:592 -->",
        "xqliu/orbi", "main",
    ) is None


def test_external_takeover_pr_ignores_a_pr_against_another_base(
    monkeypatch, tmp_path,
):
    """The delivery loop only merges the configured protected base: a
    PR against another branch is not a takeover."""
    _fake_takeover_view(monkeypatch, {
        "state": "OPEN", "url": "https://github.com/xqliu/orbi/pull/592",
        "baseRefName": "develop", "headRefName": "fix/outer",
        "headRefOid": "e592a11",
    })
    assert runner.external_takeover_pr(
        tmp_path, "<!-- orbi:external-pr:592 -->",
        "xqliu/orbi", "main",
    ) is None


def test_external_takeover_pr_returns_none_without_a_marker(monkeypatch,
                                                            tmp_path):
    """A body without the triage marker never reaches `gh pr view`."""
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no marker: gh must not be called"),
        ),
    )
    assert runner.external_takeover_pr(
        tmp_path, "an ordinary issue body", "xqliu/orbi", "main",
    ) is None
    assert runner.external_takeover_pr(
        tmp_path, None, "xqliu/orbi", "main",
    ) is None


def test_opened_pr_comment_round_trips_the_external_flag():
    """The takeover scene comment carries `external: true` and the parser
    restores it; a Runner-owned scene parses as non-external."""
    body = runner.opened_pr_comment_body(
        "a1b2c3d4",
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 "
        "priority=normal",
        "https://github.com/xqliu/orbi/pull/592", external=True, language_code="en",
    )
    scene = run_state.parse_pr_comment(body)
    assert scene["pr_url"] == "https://github.com/xqliu/orbi/pull/592"
    assert scene["run_id"] == "a1b2c3d4"
    assert bool(scene["external"]) is True

    own = run_state.parse_pr_comment(runner.opened_pr_comment_body(
        "a1b2c3d4",
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 "
        "priority=normal",
        "https://github.com/xqliu/orbi/pull/40", language_code="en",
    ))
    assert bool(own["external"]) is False


def test_verify_pr_queries_base_head_and_accepts_matching_pr(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == "https://github.com/orbi-build/orbi/pull/4"
    assert ["git", "rev-parse", "HEAD"] in calls
    # Issue #291: verify_pr issues the ONE shared PR query contract.
    assert [
        "gh", "pr", "list", "--state", "open", "--head",
        f"orbi/issue-4-{FAKE_RUN_ID}",
        "--json", (
            "number,url,baseRefName,baseRefOid,"
            "headRefName,headRefOid,headRepository,headRepositoryOwner,"
            "isCrossRepository,body"
        ),
        "--limit", "100",
    ] in calls


# ------------------------------------------- repo + expected URL (F1, F3)


def test_verify_pr_accepts_pr_in_expected_repo_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO, expected_url=FAKE_PR_URL) == FAKE_PR_URL


def test_verify_pr_rejects_pr_head_in_another_repo(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={"name": "other"},
                headRepositoryOwner={"login": "attacker"},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="PR head repo is attacker/other, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)
    assert "pr_repo_mismatch" in caplog.text


def test_verify_pr_rejects_pr_head_repo_missing_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository=None, headRepositoryOwner=None,
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)


def test_verify_pr_rejects_pr_head_repo_empty_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={}, headRepositoryOwner={},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "orbi-build/orbi",
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo=FAKE_PR_REPO)


def test_verify_pr_skips_repo_check_when_pr_repo_not_given(monkeypatch,
                                                            tmp_path):
    """The fresh-claim path (process_issue) does not pass pr_repo: a PR
    payload without head repo fields still passes."""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefOid": FAKE_HEAD_SHA,
                "body": (
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_rejects_url_different_from_expected(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                url="https://github.com/orbi-build/orbi/pull/99",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=(
            "PR URL https://github.com/orbi-build/orbi/pull/99 is "
            "not the recovered original PR "
            "https://github.com/orbi-build/orbi/pull/4"
        ),
    ):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, expected_url=FAKE_PR_URL)
    assert "pr_url_mismatch" in caplog.text


def test_verify_pr_skips_url_check_when_expected_url_not_given(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", fake_verify_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path) == FAKE_PR_URL


def test_verify_pr_skips_latest_base_check_when_not_required(
    monkeypatch, tmp_path,
):
    """The resume pre-validation runs before the base merge, when being
    behind the latest base is the expected state: no fetch, no ancestry
    check."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=f"orbi/issue-4-{FAKE_RUN_ID}", worktree=tmp_path, source_repo="owner/repo"), "main", repo_dir=tmp_path, require_latest_base=False) == FAKE_PR_URL
    assert not any(c[:3] == ["git", "fetch", "origin"] for c in calls)
    assert not any(
        c[:3] == ["git", "merge-base", "--is-ancestor"] for c in calls
    )


def test_process_issue_success_records_base_and_run_in_comment(monkeypatch, tmp_path):
    calls = []
    gh_calls, posted = make_fake_gh(monkeypatch)
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            if command[-1] == "state":
                # The pre-PR closeout reads the source Issue state
                # (Issue #746): open in this scene.
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:2] == ["gh", "issue"]:
            # Scene comments (started Pi / opened PR) go through
            # comment_issue -> run_command; record them like the others.
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        # git fetch / git merge-base: no output needed.
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    assert runner.process_issue(issue, config, "xqliu/orbi-backlog") == runner.IssueResult("pr", "https://github.com/orbi-build/orbi/pull/4")
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/orbi-backlog", "add": "ai-in-progress"})
    # The run state is published automatically: exactly one progress
    # comment (hidden run marker); started and PR-opened scenes are
    # separate resume announcements.
    progress_posts = [
        body for body in posted
        if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in progress_posts[0]
    # The PR URL is only known after verify_pr: the initial POST shows
    # `- PR: -`, the final delivery PATCH carries the URL.
    assert "- PR: -" in progress_posts[0]
    # The scene comments: the started Pi scene is upserted through the
    # progress publisher (Issue #1369), the opened PR scene still goes
    # through `comment_issue`.
    started_bodies = [
        body for body in posted if "Orbi started Pi:" in body
    ]
    assert len(started_bodies) == 1
    start_body = started_bodies[0]
    assert "Orbi started Pi:" in start_body
    assert "- base_branch: main" in start_body
    assert "- base_sha: abc123def456" in start_body
    assert "run_id=a1b2c3d4" in start_body
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in start_body
    # Issue #1715: the public started comment never names the worktree
    # path on the runner host.
    assert "- worktree" not in start_body
    assert str(tmp_path) not in start_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in start_body
    opened_bodies = [
        call[-1] for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
        and "Orbi opened PR:" in call[-1]
    ]
    assert len(opened_bodies) == 1
    opened_body = opened_bodies[0]
    assert "Orbi opened PR: https://github.com/orbi-build/orbi/pull/4" in opened_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in opened_body
    # The final delivery summary PATCHed the same progress comment.
    patches = [
        command for command in gh_calls
        if command[:2] == ["gh", "api"] and "PATCH" in command
    ]
    assert patches
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    # Issue #1733: the PR-opened state, waiting for checks and review.
    assert "waiting for checks and review" in last_body


def _gh_api(command, posted):
    """Answer the progress publisher's `gh api` traffic (shared fake)."""
    if "--method" not in command:
        return json.dumps([])
    method = command[command.index("--method") + 1]
    if method == "POST":
        body = command[command.index("--field") + 1]
        posted.append(body[len("body="):])
        return json.dumps({"id": 77, "body": body[len("body="):]})
    return ""


def test_process_issue_success_logs_run_end_with_commit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4")
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "run=a1b2c3d4" in ends[0]
    assert "issue=xqliu/orbi-backlog#4" in ends[0]
    assert "result=pr_opened" in ends[0]
    assert "pr=https://github.com/orbi-build/orbi/pull/4" in ends[0]
    assert "commit=0123456789abcdef0123456789abcdef01234567" in ends[0]


def test_process_issue_failure_marks_blocked_and_ends_cleanly(monkeypatch, tmp_path):
    """Issue #239: a delivery failure is terminal — `process_issue` marks
    the Issue `ai-blocked`, posts the `Orbi failed` comment, and
    RETURNS `None` instead of re-raising. The service must not crash on
    an already-handled failure; the tick ends cleanly and `main` skips
    the delivery wait."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert calls[1][2] == {"repo": "xqliu/orbi-backlog", "add": "ai-blocked", "remove": "ai-in-progress"}
    assert calls[2][0] == "comment"
    failure_body = calls[2][2]["body"]
    # The failure is also published as the blocked milestone (Issue
    # #18). The worktree was never created, so no progress comment
    # exists to PATCH: the milestone alone carries the notification.
    assert any("Orbi: blocked" in body for body in posted)
    blocked = [
        body for body in posted if "Orbi: blocked" in body
    ][0]
    assert "git failed" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    assert "git failed" in failure_body
    assert "- base_branch: main" in failure_body
    assert "- base_sha: abc123def456" in failure_body
    assert "run_id=a1b2c3d4" in failure_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure_body


def test_process_issue_delivery_no_commit_marks_blocked_without_crashing(
    monkeypatch, tmp_path,
):
    """Issue #239 regression: the commit-boundary failure of `deliver_pr`
    (the agent delivered no commit — HEAD is still the frozen base) flows
    through the NORMAL terminal failure path: the Issue is marked
    `ai-blocked` (removing `ai-in-progress`), the `Orbi failed`
    comment carries the no-commit reason and the run marker, and
    `process_issue` RETURNS `None` instead of re-raising — the failure is
    already terminal, so the tick must end cleanly and the service must
    not crash on the unhandled `RuntimeError` (the #239 scene). The
    Runner never auto-commits or expands the agent's commit boundary."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: worktree,
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    no_commit = RuntimeError(
        "the agent delivered no commit on the task branch (HEAD "
        "abc123def456 is still the frozen base abc123def456)"
    )

    def dead_deliver_pr(*args, **kwargs):
        raise no_commit

    monkeypatch.setattr(runner, "deliver_pr", dead_deliver_pr)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it.
    assert runner.process_issue(
        {"number": 239, "title": "No commit", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-blocked",
         "remove": "ai-in-progress"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    failure = [
        body for body in comment_bodies
        if "Orbi: blocked" in body
    ]
    assert len(failure) == 1
    assert "delivered no commit" in failure[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure[0]
    # The blocked milestone notification carries the same scene.
    blocked = [body for body in posted if "Orbi: blocked" in body]
    assert blocked
    assert "delivered no commit" in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]


def test_process_issue_empty_commit_marks_blocked_without_crashing(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1581: the agent delivered a commit that changes no file
    (git commit --allow-empty). The REAL commit boundary detects the tree
    equal to the frozen base, logs the delivery_no_file_change event, and
    the Issue is marked ai-blocked through the normal terminal failure
    path: no branch is pushed, no PR is opened, and the blocked comment
    carries the last commit subject. The pre-#1581 HEAD == base test
    alone would have pushed a zero-file branch and merged it."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config",
                    "user.email", "t@example.com"],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config",
                    "user.name", "test"],
                   check=True, capture_output=True, env=env)
    (repo / "index.html").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "index.html"],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"],
                   check=True, capture_output=True, env=env)
    base_sha = _git_output(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add",
                    "origin", str(remote)],
                   check=True, capture_output=True, env=env)
    worktree = repo / ".worktrees" / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b",
         "orbi/xqliu-orbi-issue-239", str(worktree), "HEAD"],
        check=True, capture_output=True, env=env,
    )
    subject = (
        "Blocked: home page (index.html) is committed empty — "
        "no page to measure"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--allow-empty",
         "-m", subject],
        check=True, capture_output=True, env=env,
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: base_sha,
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: worktree,
    )
    # The subprocess seam is patched through the fan-out target so the
    # ratcheted module-patch counts do not rise (Issue #789).
    monkeypatch.setattr(seam, "run_pi",
        lambda *args, **kwargs: "done",
    )
    monkeypatch.setattr(
        seam, "activity_snapshot", lambda session_dir: None,
    )
    real_run_command = journal.run_command
    comments = []
    posted = []

    def fake_run(command, **kwargs):
        # The commit boundary runs against the REAL git worktree; every
        # gh call is answered here, so nothing reaches the network.
        if command[0:1] == ["git"]:
            return real_run_command(command, **kwargs)
        if command[0:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[0:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        # The delivered branch stops before any further GitHub traffic:
        # the only remaining call is the blocked comment itself.
        comments.append(command[-1])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 239, "title": "Empty commit", "body": ""},
            config_domain.RunnerConfig(
                repo_dir=repo, prompt=tmp_path / "prompt.md",
                base_branch="main",
            ),
            "xqliu/orbi",
        )
    assert result.kind == "failed"
    assert {"repo": "xqliu/orbi", "add": "ai-blocked",
            "remove": "ai-in-progress"} in edits
    blocked = [body for body in comments if "Orbi: blocked" in body]
    assert len(blocked) == 1
    assert "no file change" in blocked[0]
    assert subject in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]
    assert "delivery_no_file_change" in caplog.text
    # The stop is terminal BEFORE the push: the bare remote never saw the
    # task branch, and no PR was created.
    remote_refs = subprocess.run(
        ["git", "-C", str(remote), "for-each-ref",
         "--format=%(refname)", "refs/heads/"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert remote_refs == ""


def test_process_issue_model_wait_dead_failure_stays_in_progress(
    monkeypatch, tmp_path,
):
    """Issue #227 acceptance: the hung-model-request failure of the
    implementer session (the model service process is alive but the
    request never completes, the session JSONL froze in model_wait,
    stream_pi killed Pi and raised the classified `ModelWaitDeadError`)
    is RECOVERABLE: the Issue keeps `ai-in-progress` (never `ai-blocked`),
    the `Orbi model_wait recovered` comment carries the
    hung-model-request reason and the run marker, and `process_issue`
    returns `None` so the tick ends cleanly and the slot is released by
    `main`'s `finally`. The next tick's in-flight restart scan resumes
    the SAME run (same run id, branch, worktree, progress comment). No
    terminal label, no fallback."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is recoverable: `process_issue` returns `None` (no PR)
    # and does NOT re-raise — the service must not crash on it (Issue
    # #239), and the Issue must NOT be marked `ai-blocked` (Issue #227).
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    # The Issue keeps `ai-in-progress`: the ONLY label edit is the claim
    # at the start — no `ai-blocked`, no removal of `ai-in-progress`
    # (the edit calls are the dict entries; the `gh issue comment`
    # traffic is tuple entries).
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    # Issue #645: the recovery scene comment is published through the
    # `ProgressPublisher` (`gh api` POST), so it lands in `posted`.
    recovered = [
        body for body in posted
        if "Orbi model_wait recovered:" in body
    ]
    assert len(recovered) == 1
    # The hung-model-request reason and the run marker stay in the
    # Issue.
    assert "the model request is hung" in recovered[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in recovered[0]
    # The resumable scene is explicit: the Issue stays ai-in-progress
    # and the next tick resumes the same run.
    assert "stays ai-in-progress" in recovered[0]
    # No terminal failure comment was posted.
    assert not any(
        "Orbi failed:" in body for body in comment_bodies
    )


def _health_runs(repo_dir):
    from orbi import runner_health
    return runner_health.load_health_state(
        runner_health.health_state_path(repo_dir),
    )["runs"]


def test_process_issue_model_wait_failure_records_health_attempt(
    monkeypatch, tmp_path,
):
    """Issue #266/#246: a RECOVERABLE failure (the #227 model_wait
    scene — the run stays ai-in-progress and the next tick retries the
    same dead end) must reach health.json exactly like the terminal
    failure branch, or the repeated-failure health check is blind to
    the most common repeat-failure scene."""
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    runs = _health_runs(tmp_path)
    assert len(runs) == 1, f"the recoverable failure is recorded: {runs}"
    entry = runs[0]
    assert entry["repo"] == "xqliu/orbi"
    assert entry["issue"] == 218
    assert entry["run_id"] == "a1b2c3d4"
    assert entry["outcome"] == "failed"
    assert entry["fingerprint"]


def test_process_issue_three_recoverable_failures_raise_health_finding(
    monkeypatch, tmp_path,
):
    """The #246 scene through the #227 path: the same Issue fails three
    consecutive runs with the same classified recoverable failure — the
    repeated-failure health check must see the streak (before the fix
    none of these runs ever reached health.json)."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    for _ in range(3):
        assert runner.process_issue(
            {"number": 218, "title": "Model wait dead", "body": ""},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi",
        ).kind == "failed"
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    findings = runner_health.repeated_failure_findings(state)
    assert [
        (f["repo"], f["issue"], f["count"]) for f in findings
    ] == [("xqliu/orbi", 218, 3)]


def test_process_issue_success_records_health_streak_break(
    monkeypatch, tmp_path,
):
    """A delivered PR writes the non-failed outcome the streak-break
    contract (runner_health.repeated_failure_findings, whose tests pin
    outcome="pr_opened") has always expected from production: a
    failure -> success -> failure sequence must NOT read as a streak."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4",
    )
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    result = runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    assert result.kind == "pr"
    runs = _health_runs(tmp_path)
    assert [entry["outcome"] for entry in runs] == ["pr_opened"], runs
    # The delivered outcome breaks a pre-existing failure streak of the
    # same Issue: a later failure does not inherit the old failures.
    runner_health.record_run_attempt(
        runner_health.health_state_path(tmp_path),
        repo="xqliu/orbi-backlog", issue=4, run_id="a1b2c3d4",
        outcome="failed", fingerprint="whatever",
    )
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    assert runner_health.repeated_failure_findings(state) == []


def test_process_issue_recoverable_health_record_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    """The recoverable branch's health record is a pure bypass: a
    state-write failure logs and never changes the recoverable outcome
    (the Issue stays `failed`-recoverable, never `ai-blocked`)."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_run_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 218, "title": "Model wait dead", "body": ""},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi",
        )
    assert result.kind == "failed"
    assert "health_failure_record_failed" in caplog.text


def test_process_issue_success_health_record_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    """The delivery path's health record is a pure bypass: a
    state-write failure logs and the PR result is returned unchanged."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4",
    )
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_run_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    assert result.kind == "pr"
    assert "health_success_record_failed" in caplog.text


def test_process_issue_model_wait_dead_comment_failure_stays_in_progress(
    monkeypatch, tmp_path,
):
    """Issue #227: the recovery scene comment is the delivery record,
    but the resume does not parse it (the run state file, the worktree
    and the `ai-in-progress` label carry the resume —
    `worktree_resume_scene`): a failure of the comment must only log.
    Falling through to the generic failure handler would mark the
    Issue `ai-blocked` — exactly the unrecoverable state Issue #227
    forbids for the model_wait recovery."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            # Issue #645: the recovery scene comment is published through
            # the `ProgressPublisher` (`gh api` POST) — inject the
            # failure there.
            if ("--method" in command and "POST" in command
                    and "model_wait recovered" in command[-1]):
                raise RuntimeError(
                    "gh api comment POST failed: API rate limit exceeded",
                )
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    # The Issue keeps `ai-in-progress`: the ONLY label edit is the claim
    # at the start — the failed recovery comment must NOT fall through
    # to the generic handler's terminal `ai-blocked` (Issue #227).
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
    ]
    # No terminal failure comment was posted.
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    assert not any(
        "Orbi failed:" in body for body in comment_bodies
    )


def test_process_issue_idle_recovery_failure_marks_blocked(
    monkeypatch, tmp_path,
):
    """Issue #94 acceptance: the idle-recovery escalation of the
    implementer session (three idle cycles without any new activity,
    stream_pi killed Pi and raised) flows through the EXISTING
    `ai-blocked`/recoverable failure path exactly like the Issue #75
    upstream-dead failure: the Issue is marked `ai-blocked` (removing
    `ai-in-progress`), the `Orbi failed` comment carries the
    idle-recovery reason and the run marker, and `process_issue` returns
    `None` so the tick ends cleanly and the slot is released by `main`
    's `finally` (Issue #239: the handled failure never re-raises to
    crash the service). No special handling, no fallback."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    idle_recovery = RuntimeError(
        "Pi session stayed idle for 15m after idle recovery (TERM/KILL "
        "of pre-idle descendants); Pi was killed (Issue #94)"
    )

    def dead_run_pi(*args, **kwargs):
        raise idle_recovery

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue(
        {"number": 94, "title": "Idle recovery", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-blocked",
         "remove": "ai-in-progress"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    failure = [
        body for body in comment_bodies
        if "Orbi: blocked" in body
    ]
    assert len(failure) == 1
    # The idle-recovery reason and the run marker stay in the Issue.
    assert "idle recovery" in failure[0]
    assert "(Issue #94)" in failure[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure[0]
    # The blocked milestone notification carries the same scene.
    blocked = [body for body in posted if "Orbi: blocked" in body]
    assert blocked
    assert "idle recovery" in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]


def test_process_issue_ends_cleanly_when_reporting_fails(monkeypatch, tmp_path, caplog):
    """Issue #239: when the failure reporting itself fails (the `Orbi
    failed` comment POST dies), `process_issue` still RETURNS
    `None` instead of re-raising the original failure — the service must
    not crash. The `ai-blocked` edit already landed (it precedes the
    comment POST), so the Issue is terminal; the missing failure comment
    is logged (`failure reporting failed`) and the blocked scene is
    degraded, not fatal."""
    edit_calls = []

    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edit_calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "issue"] and "comment" in command:
            # The `Orbi failed` comment POST is the only
            # non-bypass traffic of this scenario: it fails, the
            # failure report is broken, but `process_issue` still ends
            # cleanly (returns `None`) instead of crashing the service
            # (Issue #239). (Issue #79: the progress traffic — the
            # blocked milestone POST — is bypass, so a failure there
            # no longer breaks the failure report; see
            # test_progress_wiring.
            # test_process_issue_failure_path_progress_failure_keeps_
            # blocked_transition.)
            raise RuntimeError("github report failed")
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not issue-comment/list traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    with caplog.at_level("ERROR"):
        # The failure is terminal: `process_issue` returns `None` (no PR)
        # and does NOT re-raise — the service must not crash on it
        # (Issue #239).
        assert runner.process_issue({"number": 13, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert "failure reporting failed" in caplog.text
    # No progress comment was posted (the failure report died on the
    # failure-comment POST before the bypass steps).
    assert posted == []
    # The failure happened BEFORE the opened-PR transition (the
    # worktree creation failed), so the terminal state removes the
    # claim label, not the opened-PR label (Issue #79: the scene
    # comment is the first failure after the transition; its test in
    # test_progress_wiring pins the other branch).
    assert edit_calls == [
        {"repo": "xqliu/orbi-backlog", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi-backlog", "add": "ai-blocked",
         "remove": "ai-in-progress"},
    ]


def test_parse_milestone_version_accepts_only_strict_semver():
    assert runner._parse_version_title(None) is None
    assert runner._parse_version_title("v0.3.1") == (0, 3, 1)
    assert runner._parse_version_title("v01.3.1") is None
    assert runner._parse_version_title("1.3.1") is None
    assert runner._parse_version_title("v1.2.3.4") is None


def _advance_active_milestone_on_idle(*args, **kwargs):
    kwargs.setdefault("parse_version_title", runner._parse_version_title)
    return milestone.advance_active_milestone_on_idle(*args, **kwargs)


def test_rewrite_active_milestone_line_preserves_all_other_bytes(tmp_path):
    config = tmp_path / "orbi.toml"
    original = '# keep \r\nsource_repos = ["owner/repo"]\r\nactive_milestone = \'v0.3.0\'\r\nother = "x"\r\n'
    config.write_bytes(original.encode())
    milestone.rewrite_active_milestone_line(config, "v0.3.1")
    assert config.read_bytes() == original.replace(
        "active_milestone = 'v0.3.0'", 'active_milestone = "v0.3.1"',
    ).encode()


def test_rewrite_active_milestone_line_fails_when_missing(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="active_milestone line not found"):
        milestone.rewrite_active_milestone_line(config, "v0.3.1")


def test_advance_active_milestone_pending_creates_one_p0_ready_issue(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    milestones = [
        {"title": "v0.3.0", "state": "closed"},
        {"title": "v0.3.1", "state": "open", "open_issues": 2},
        {"title": "v0.4.0", "state": "open", "open_issues": 5},
    ]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return json.dumps([milestones])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config, auto_next_milestone=False,
        ) == ("closed", None)
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'
    create = [command for command in calls if command[:3] == ["gh", "issue", "create"]]
    assert len(create) == 1
    assert "--label" not in create[0]
    assert "`v0.3.1`: 2 open issues" in create[0][create[0].index("--body") + 1]
    # Issue #1290: the human instruction names the in-ticket command a
    # hosted tenant can actually use (no host CLI, no config edit).
    assert "/milestone" in create[0][create[0].index("--body") + 1]
    assert "active_milestone_advance_pending old=v0.3.0" in caplog.text
    assert "auto_next_milestone=false" in caplog.text


def test_advance_active_milestone_manual_notification_is_not_pickupable(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or (
            "[]" if command[:3] == ["gh", "issue", "list"] else json.dumps([[
                {"title": "v0.3.0", "state": "closed"},
                {"title": "v0.4.0", "state": "open"},
            ]])
        ),
    )

    _advance_active_milestone_on_idle(
        "owner/repo", "v0.3.0", config, auto_next_milestone=False,
    )

    create = next(command for command in calls if command[:3] == ["gh", "issue", "create"])
    assert "--label" not in create


def test_advance_active_milestone_closes_old_manual_notification_after_manual_move(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([
                {"number": "bad", "body": "orbi-milestone-advance old=v0.2.0"},
                {"number": 492, "body": "not a milestone notice"},
                {
                    "number": 493,
                    "body": "orbi-milestone-advance old=v0.3.0 candidates=v0.4.0",
                },
            ])
        return json.dumps([[{"title": "v0.4.0", "state": "open"}]])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.4.0", config, auto_next_milestone=False,
        language_code="zh",
    ) == ("open", None)
    close = next(command for command in calls if command[:3] == ["gh", "issue", "close"])
    assert close == [
        "gh", "issue", "close", "493", "--repo", "owner/repo",
        "--comment", "已收敛：当前配置 active_milestone = `v0.4.0`。",
    ]
    # Issue #1704: the same receipt in the repository's English language.
    calls.clear()
    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.4.0", config, auto_next_milestone=False,
        language_code="en",
    ) == ("open", None)
    close = next(command for command in calls if "--comment" in command)
    assert close[close.index("--comment") + 1] == (
        "Settled: the configured active_milestone is `v0.4.0`."
    )


def test_stale_notice_comment_table():
    """Issue #856: the pure close/keep decision for one decision notice."""
    active = "v0.4.0"
    release_body = "orbi-milestone-advance old=v0.4.0 candidates=v0.4.0"
    advance_body = "orbi-milestone-advance old=v0.4.0 candidates=v0.5.0"
    stale_body = "orbi-milestone-advance old=v0.3.0 candidates=v0.4.0"
    close = "已收敛：当前配置 active_milestone = `v0.4.0`。"
    confirmed = (
        "已收敛：Milestone `v0.4.0` 的 release ticket"
        " 已存在，不再等待发布确认。"
    )
    wait_over = "已收敛：Milestone `v0.4.0` 不再等待发布确认。"

    def zh(body, **kwargs):
        return milestone._stale_notice_comment(
            body, active, language_code="zh", **kwargs,
        )

    assert zh(release_body, keep_release_notice=True) is None
    assert zh(
        release_body, keep_release_notice=False, release_ticket_exists=True,
    ) == confirmed
    # The wait also ends without a release ticket (the Milestone got new
    # work, was closed, or the opt-in was turned off): the receipt must
    # not claim a ticket that does not exist.
    assert zh(release_body, keep_release_notice=False) == wait_over
    assert zh(
        release_body, keep_release_notice=False, release_ticket_exists=False,
    ) == wait_over
    assert zh(advance_body, keep_release_notice=False) is None
    assert zh(stale_body, keep_release_notice=True) == close
    # A body without a candidates= group keeps the pre-#856 behaviour: it is
    # closed like any advance notice once its old= no longer matches.
    assert zh(
        "orbi-milestone-advance old=v0.3.0", keep_release_notice=True,
    ) == close
    assert zh(None, keep_release_notice=True) is None
    assert zh("no fingerprint here", keep_release_notice=True) is None


def test_advance_active_milestone_close_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")

    def fail_close(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return json.dumps([[{"title": "v0.4.0", "state": "open"}]])
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "run_command", fail_close)
    with caplog.at_level("ERROR"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.4.0", config,
        ) == ("open", None)
    assert "stale_milestone_issue_close_failed repo=owner/repo active=v0.4.0" in caplog.text


def test_advance_active_milestone_closure_is_idempotent_on_repeated_tick(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or (
            "[]" if command[:3] == ["gh", "issue", "list"] else json.dumps([[
                {"title": "v0.4.0", "state": "open"},
            ]])
        ),
    )

    _advance_active_milestone_on_idle("owner/repo", "v0.4.0", config)
    _advance_active_milestone_on_idle("owner/repo", "v0.4.0", config)

    assert not any(command[:3] == ["gh", "issue", "close"] for command in calls)


def test_advance_active_milestone_pending_issue_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")

    def fail_pending(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        if command[:3] == ["gh", "issue", "create"]:
            raise RuntimeError("GitHub unavailable")
        return json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.3.1", "state": "open", "open_issues": 2},
        ]])

    monkeypatch.setattr(seam, "run_command", fail_pending)
    with caplog.at_level("ERROR"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config, auto_next_milestone=False,
        ) == ("closed", None)
    assert "pending_milestone_issue_failed repo=owner/repo old=v0.3.0" in caplog.text
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'


def test_pending_milestone_issue_serializes_concurrent_search_and_create(
    monkeypatch, tmp_path,
):
    candidates = [{"title": "v0.3.1", "open_issues": 2}]
    open_issues = []
    created = []
    first_search_entered = threading.Event()
    second_call_started = threading.Event()

    def fake_list(repo, *, state, **kwargs):
        snapshot = [{"number": number} for number in open_issues]
        if state == "all" and not first_search_entered.is_set():
            first_search_entered.set()
            assert second_call_started.wait(timeout=1)
            # Give an unlocked second call time to take the same empty
            # snapshot before this first search returns.
            time.sleep(0.05)
        return snapshot

    def fake_run(command, **kwargs):
        assert (command[0], command[1], command[2]) == ("gh", "issue", "create")
        number = 100 + len(created)
        created.append(number)
        open_issues.append(number)
        return f"https://github.com/owner/repo/issues/{number}"

    def call_pending():
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )

    monkeypatch.setattr(seam, "list_issues", fake_list)
    monkeypatch.setattr(seam, "run_command", fake_run)
    first = threading.Thread(target=call_pending, daemon=True)
    second = threading.Thread(
        target=lambda: (second_call_started.set(), call_pending()), daemon=True,
    )
    first.start()
    assert first_search_entered.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert created == [100]
    assert open_issues == [100]


def test_pending_milestone_issue_deduplicates_after_search_lag(
    monkeypatch, tmp_path, caplog,
):
    candidates = [{"title": "v0.3.1", "open_issues": 2}]
    created = []
    open_issues = []
    closed = []

    def fake_list(repo, *, state, **kwargs):
        if state == "all":
            # Model GitHub's search lag: neither initial check sees a
            # just-created Issue, while the post-create check sees both.
            return []
        return [{"number": number} for number in open_issues]

    def fake_run(command, **kwargs):
        if (command[0], command[1], command[2]) == ("gh", "issue", "create"):
            number = 100 + len(created)
            created.append(number)
            open_issues.append(number)
            return f"https://github.com/owner/repo/issues/{number}"
        assert (command[0], command[1], command[2]) == ("gh", "issue", "close")
        closed.append(command)
        open_issues.remove(int(command[3]))
        return ""

    monkeypatch.setattr(seam, "list_issues", fake_list)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO):
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )

    assert created == [100, 101]
    assert open_issues == [100]
    assert len(closed) == 1
    assert closed[0][-1] == "duplicate of #100"
    assert caplog.text.count("pending_milestone_issue_deduplicated") == 1


def test_advance_active_milestone_pending_is_idempotent(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    responses = [
        json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.3.1", "state": "open", "open_issues": 2},
        ]]),
        '[{"number": 436}]',
        # The confirmation ticket's comments are read for `/milestone`
        # commands; none here, so nothing is applied.
        json.dumps({"comments": []}),
    ]
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command) or responses.pop(0),
    )
    _advance_active_milestone_on_idle(
        "owner/repo", "v0.3.0", config, auto_next_milestone=False,
    )
    assert not any(command[:3] == ["gh", "issue", "create"] for command in calls)


def test_advance_active_milestone_sorts_double_digit_versions_numerically(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.9.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.9.0", "state": "closed"},
            {"title": "v0.10.0", "state": "open"},
            {"title": "v0.11.0", "state": "open"},
        ]]),
    )

    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.9.0", config,
    ) == ("closed", "v0.10.0")


def test_advance_active_milestone_selects_smallest_higher_open(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    milestones = [
        {"title": "v0.3.0", "state": "closed", "number": 1},
        {"title": "v0.4.0", "state": "open", "open_issues": 3},
        {"title": "v0.3.1", "state": "open", "open_issues": 8},
        {"title": "not-a-version", "state": "open", "open_issues": 1},
    ]
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([milestones]),
    )
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config,
        ) == ("closed", "v0.3.1")
    assert 'active_milestone = "v0.3.1"\n' == config.read_text()
    assert "active_milestone_advanced old=v0.3.0 new=v0.3.1 closed=v0.3.0" in caplog.text


def test_advance_active_milestone_open_reconciles_notifications_without_write(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or json.dumps([[
            {"title": "v0.3.0", "state": "open"},
        ]]),
    )
    assert _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config) == ("open", None)
    assert len(calls) == 2
    assert calls[1][:3] == ["gh", "issue", "list"]
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'


def test_advance_active_milestone_closed_without_candidate_is_unscoped(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391: a closed active Milestone with no higher open candidate
    means no scope. The advance reports the closed-unscoped outcome and logs
    one line, and the configured value is left untouched (the maintainer may
    still create the next Milestone)."""
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.2.0", "state": "open", "open_issues": 1},
            {"title": "future", "state": "open", "open_issues": 2},
        ]]),
    )
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config,
        ) == (milestone.MILESTONE_CLOSED_UNSCOPED, None)
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'
    assert (
        "active_milestone_closed_unscoped repo=owner/repo closed=v0.3.0"
        in caplog.text
    )


def test_advance_active_milestone_missing_lists_open_milestones(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.1", "state": "open", "open_issues": 8},
        ]]),
    )
    with pytest.raises(RuntimeError, match=r"active_milestone_missing current=v0.3.0; open milestones: v0.3.1\(8\)"):
        _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config)


def test_advance_active_milestone_rejects_duplicate_title(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.0", "state": "closed", "number": 1},
            {"title": "v0.3.0", "state": "closed", "number": 2},
        ]]),
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config)


def test_arm_release_ticket_adds_ready_to_matching_open_issue(
    monkeypatch, caplog,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 385}])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        milestone.arm_release_ticket("owner/repo", "v0.4.0")

    assert calls == [
        [
            "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
            "--search",
            'label:ai-release -label:ai-ready milestone:"v0.4.0"',
            "--json", "number", "--limit", "200",
        ],
        [
            "gh", "issue", "edit", "385", "--repo", "owner/repo",
            "--add-label", "ai-ready",
        ],
    ]
    assert "release_ticket_armed issue=#385 milestone=v0.4.0" in caplog.text


def test_arm_release_ticket_does_nothing_when_no_ticket_matches(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )

    milestone.arm_release_ticket("owner/repo", "v0.4.0")

    assert len(calls) == 1


def test_arm_release_ticket_rejects_malformed_issue_number(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: '[{"number": "385"}]',
    )

    with pytest.raises(RuntimeError, match="invalid issue number"):
        milestone.arm_release_ticket("owner/repo", "v0.4.0")


def test_arm_release_ticket_uses_custom_dispatch_label(monkeypatch, caplog):
    """A custom-dispatch-label repo must arm its release ticket with that
    label — search exclusion and added label alike.

    The hardcoded ``ai-ready`` armed the ticket with a label the release
    fallback scan never searches for (it searches the repo's dispatch
    label), so the ticket silently stranded; the next idle tick's
    ``-label:ai-ready`` exclusion then hid the polluted ticket from every
    future arm attempt.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 385}])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        milestone.arm_release_ticket(
            "owner/repo", "v0.4.0", dispatch_label="dev-queue",
        )

    assert calls == [
        [
            "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
            "--search",
            'label:ai-release -label:dev-queue milestone:"v0.4.0"',
            "--json", "number", "--limit", "200",
        ],
        [
            "gh", "issue", "edit", "385", "--repo", "owner/repo",
            "--add-label", "dev-queue",
        ],
    ]
    assert "release_ticket_armed issue=#385 milestone=v0.4.0" in caplog.text


def test_main_idle_release_arm_uses_repo_dispatch_label(
    monkeypatch, tmp_path,
):
    """The idle-path arm arms the release ticket with the repo policy's
    dispatch_label — the same label the fallback scan requires before a
    release ticket can ever be claimed. Armed with the host default
    instead, the ticket is invisible to every scan and the next idle
    tick's arm exclusion hides it for good (silent stranding)."""
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.add_issue(385, title="Release v0.4.0",
                   labels=("ai-release",), milestone=1)
    fake.set_repo_config('dispatch_label = "dev-queue"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )

    assert runner.main(["--config", str(config)]) == 0
    # Public surface: the ticket carries the repo's dispatch label, so
    # the release fallback scan can see and claim it.
    assert fake.issues[385]["labels"] == ["ai-release", "dev-queue"]


def test_main_idle_advance_uses_the_repos_fused_base_branch(
    monkeypatch, tmp_path,
):
    """Issue #1290: the idle path hands the milestone command the repo's
    FUSED base branch, never the raw host value.

    `resolve_source_base_branch` is the documented single source of a
    repository's effective base branch ("Every consumer (dev path AND the
    release state machine) reads `config.base_branch` and must never
    re-derive it from the raw entry"): the raw host value skips both the
    `[[repositories]]` entry fallback and the repository policy. The
    command writes that value into the release ticket it opens, and the
    release state machine freezes the DECLARED branch — so a raw value
    releases the wrong branch."""
    _write_prompts(tmp_path)
    checkout = tmp_path / "checkouts" / "pilot"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config('base_branch = "release/1.x"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(
        claim, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        'active_milestone = "v0.4.0"\n'
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = "checkouts/pilot"\n'
        'github = "owner/repo"\n'
        'base_branch = "develop"\n',
        encoding="utf-8",
    )

    assert runner.main(["--config", str(config)]) == 0

    # The policy layer overrides the entry, and neither of them is the
    # raw host value the idle path used to pass through.
    assert seen["base_branch"] == "release/1.x"


def test_main_idle_release_arm_failure_is_bypassed(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("permission denied")),
    )
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)

    with caplog.at_level("ERROR"):
        assert runner.main(["--config", str(config)]) == 0

    assert "release_ticket_arm_failed" in caplog.text
    assert "permission denied" in caplog.text


def test_main_idle_advance_failure_is_bypassed(monkeypatch, tmp_path, caplog):
    """Issue #614: the idle milestone advance is a pure bypass — a missing
    or ambiguous `active_milestone` (or any `gh` failure) must not change
    the idle outcome; the tick still returns 0 and only journals."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "active_milestone_missing current=v0.4.0; "
                "open milestones: v0.5.0(2)"
            )
        ),
    )

    with caplog.at_level("ERROR"):
        assert runner.main(["--config", str(config)]) == 0

    assert "active_milestone_advance_failed" in caplog.text
    assert "active_milestone_missing" in caplog.text


def _write_prompts(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")


def test_idle_diagnostic_logs_ready_outside_active_milestone(
    monkeypatch, caplog,
):
    issues = [
        {"number": 834, "milestone": None},
        {"number": 828, "milestone": {"title": "v0.5.37"}},
        {"number": 827, "milestone": {"title": "v0.5.38"}},
    ]
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: issues)
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is True
    assert "ready_outside_milestone" in caplog.text
    assert "repo=owner/repo" in caplog.text
    assert "active_milestone=v0.5.38" in caplog.text
    assert "count=2" in caplog.text
    assert "no_ready_issue" not in caplog.text


def test_idle_diagnostic_keeps_no_ready_issue_when_queue_is_empty(
    monkeypatch, caplog,
):
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: [])
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone" not in caplog.text


def test_idle_diagnostic_query_failure_falls_back_to_no_ready_issue(
    monkeypatch, caplog,
):
    def fail_query(*args, **kwargs):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "list_issues", fail_query)
    with caplog.at_level(logging.ERROR):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "GitHub unavailable" in caplog.text


def test_main_uses_outside_milestone_outcome_instead_of_no_ready(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(runner.__dict__, "log_ready_outside_milestone", lambda *args, **kwargs: True)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "no_ready_issue" not in caplog.text


def test_main_query_failure_keeps_idle_exit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("GitHub unavailable")
    ))
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "no_ready_issue" in caplog.text


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    assert runner.main(["--config", str(config)]) == 0


def test_main_passes_configured_active_milestone_to_the_claim_scan(
    monkeypatch, tmp_path,
):
    """Issue #139: the configured `active_milestone` reaches the claim
    scan (the fresh-claim scope), and an unconfigured one passes None
    (the compat behavior — no milestone filter)."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] == "v0.2.0"


def test_main_advances_milestone_only_after_no_ready_issue(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.3.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls == [
        (("owner/repo", "v0.3.0", config.resolve(), tmp_path.resolve()),
         {
             "auto_next_milestone": True,
             "release_confirmation": False,
             "parse_version_title": runner._parse_version_title,
             "policy": None,
             "policy_path": ".github/orbi.toml",
             "base_branch": "main",
             "dispatch_label": runner.READY_LABEL,
             "version_file": None,
             "language_code": "en",
         }),
    ]


def test_main_passes_disabled_auto_next_milestone_to_idle_advance(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        'active_milestone = "v0.3.0"\n'
        'auto_next_milestone = false\n',
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen == {
        "auto_next_milestone": False,
        "release_confirmation": False,
        "parse_version_title": runner._parse_version_title,
        "policy": None,
        "policy_path": ".github/orbi.toml",
        "base_branch": "main",
        "dispatch_label": runner.READY_LABEL,
        "version_file": None,
        "language_code": "en",
    }


def test_main_idle_uses_the_single_milestone_reconcile_entry_point(
    monkeypatch, tmp_path,
):
    """Issue #856: the idle path makes ONE Milestone bookkeeping call, so a
    new step never becomes a second, separately-failing call."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket",
        lambda *args, **kwargs: pytest.fail("the runner must not arm directly"),
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ("owner/repo", "v0.4.0")
    assert calls[0][1]["release_confirmation"] is False
    assert not hasattr(runner, "arm_release_ticket")
    # The acceptance is structural: the idle path reaches Milestone
    # bookkeeping through reconcile alone, never by calling the arm or
    # the advance directly (Issue #856).
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "arm_release_ticket" not in source
    assert "advance_active_milestone_on_idle" not in source


def test_main_idle_passes_release_confirmation_from_the_repo_policy(
    monkeypatch, tmp_path,
):
    """Issue #856: the opted-in repository policy reaches the reconcile call.

    Issue #1342: the same per-key precedence applies to
    `auto_next_milestone`, so the repository value reaches the idle path.
    """
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config(
        'dispatch_label = "dev-queue"\nrelease_confirmation = true\n'
        "auto_next_milestone = false\n"
    )
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs) or ("open", None),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["release_confirmation"] is True
    assert seen["dispatch_label"] == "dev-queue"
    # Issue #1342: `auto_next_milestone` is a repository policy key too --
    # the repo value (false) must reach the idle path even though the host
    # default is true, so a managed tenant can stop the auto-advance.
    assert seen["auto_next_milestone"] is False


def test_main_passes_none_active_milestone_when_unconfigured(
    monkeypatch, tmp_path,
):
    """Issue #139 compat: without the config field the claim scan
    receives None and keeps the pre-#139 scans."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] is None


def _stub_idle_milestone_path(monkeypatch, outcome):
    """Stub the idle Milestone path down to one resolved outcome."""
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "sync_active_milestone_variable",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: outcome,
    )
    monkeypatch.setitem(
        runner.__dict__, "log_ready_outside_milestone",
        lambda *args, **kwargs: False,
    )


def test_main_retries_the_claim_unscoped_when_active_milestone_closed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 user journey: the configured Milestone is closed with no
    newer open one, so the idle tick's fresh ready scan runs again WITHOUT
    the Milestone filter and the open `ai-ready` Issue is claimed.

    Counter-test: remove the unscoped retry and the second scan never runs,
    `claimed` stays empty and this assertion fails."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    issue = {"number": 2, "title": "after the first release", "body": "b"}
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    def fake_ready_scan(repo, active_milestone=None, **_kwargs):
        scopes.append(active_milestone)
        return issue

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(claim.__dict__, "pick_issue", fake_ready_scan)
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    # The scoped scan found nothing, the unscoped retry claimed the Issue.
    assert scopes == ["v0.1.0", None]
    assert claimed == [2]


def test_main_closed_unscoped_scan_that_finds_nothing_stays_idle(
    monkeypatch, tmp_path,
):
    """Issue #1391: the unscoped re-scan is not a claim by itself. When the
    closed-Milestone repo has no ready Issue either, the tick re-scans with
    no Milestone filter (still the repo's dispatch label) and ends idle
    instead of starting a delivery."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    rescans: list[tuple] = []
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: rescans.append(args) or None,
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert rescans == [("owner/repo", None)]


def test_main_closed_active_milestone_unscopes_the_repo_policy_milestone(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 through the real claim path: when the REPOSITORY policy
    declares `active_milestone` (`.github/orbi.toml`, the managed-sandbox
    default — a managed sandbox renders no host line), a closed one with no
    newer open Milestone must leave the unscoped re-scan unscoped too. The
    host fallback is only one of the two places the value can come from,
    and the policy value wins the scoped scan either way.

    Counter-test: drop the unscoped re-scan and the retry never runs, so
    the fake's milestone-scoped scan finds nothing and `claimed` stays
    empty."""
    _write_prompts(tmp_path)
    # The fake does not model `gh issue list --milestone` (the preflight's
    # closed-milestone report); the claim path under test stays the real one.
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.1.0", state="closed")
    fake.add_issue(2, title="after the first release", labels=("ai-ready",))
    fake.set_repo_config('active_milestone = "v0.1.0"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert (
        "active_milestone_closed_unscoped repo=owner/repo closed=v0.1.0"
        in caplog.text
    )
    assert claimed == [2]


@pytest.mark.parametrize("outcome", [("open", None), ("closed", "v0.2.0")])
def test_main_keeps_the_scope_without_the_unscoped_outcome(
    monkeypatch, tmp_path, outcome,
):
    """Acceptance: while the active Milestone is open (or the engine can
    advance to a newer open one), the tick never drops the Milestone filter
    and an Issue outside it is not claimed."""
    _stub_idle_milestone_path(monkeypatch, outcome)
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: pytest.fail("the scope must be kept"),
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert scopes == ["v0.1.0"]


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    steps = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "xqliu/orbi", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or runner.IssueResult("pr", "https://github.com/x/y/pull/12"))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue
    # Issue #788: the PR is open and the scene is written — the tick ends
    # WITHOUT running the review step in the same tick. The review is the
    # next tick's RESUME_REVIEW step, so the slot is released here and is
    # never held waiting for CI or mergeability.
    assert steps == []


def test_main_uses_process_result_kind_without_rechecking_task_type(
    monkeypatch, tmp_path,
):
    issue = {"number": 281, "title": "task", "body": "body",
             "labels": [{"name": "ai-ready"}]}
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args: runner.IssueResult("pr", "https://x/y/pull/281"),
    )
    monkeypatch.setattr(
        runner, "is_content_only",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        runner, "is_ops",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        claim, "is_release",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    steps = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append(args),
    )

    assert runner.main(["--config", str(config)]) == 0
    # Issue #788: a fresh claim ends its tick when the PR opens; the
    # review step runs on a later tick through the resume scan.
    assert steps == []


def test_main_ends_tick_when_process_issue_delivers_nothing(
    monkeypatch, tmp_path,
):
    """Issue #239: when `process_issue` returns `None` (a terminal
    delivery failure it already handled — the Issue is `ai-blocked` and
    the failure comment is posted), `main` ends the tick cleanly: it
    returns 0 and NEVER calls `delivery_step` (there is no PR to
    wait for). The service must not crash on the handled failure."""
    issue = {"number": 239, "title": "task", "body": "body"}
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: runner.IssueResult("failed", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_ticket_only_finishes_without_entering_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Ticket-only work has no PR, so the normal review/merge wait is invalid."""
    issue = {
        "number": 12, "title": "Launch copy", "body": "Write copy",
        "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}],
    }
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("ticket-only", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ticket-only work must not enter PR delivery wait")
        ),
    )

    assert runner.main(["--config", str(config)]) == 0


def test_main_release_success_ends_tick_without_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Issue #269: a release delivery returns a Release URL, not a PR URL.

    `process_release` already closed the delivery (tag pushed, GitHub
    Release published, Issue `ai-merged` and closed), so `main` must end
    the tick with exit code 0 and NEVER enter the PR delivery wait —
    `_pr_number` on `.../releases/tag/v0.3.0` raises `ValueError` and
    crashed the Runner (run_id=37216af5)."""
    issue = {
        "number": 254, "title": "Release v0.3.0", "body": "Release v0.3.0",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
    }
    release_url = "https://github.com/orbi-build/orbi/releases/tag/v0.3.0"
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("release", release_url))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append(args) or (
            _ for _ in ()
        ).throw(AssertionError(
            "release delivery must not enter PR delivery wait"
        )),
    )

    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_routes_fix_needed_resume_to_delivery_wait(
    monkeypatch, tmp_path,
):
    """A resumed `ai-fix-needed` delivery goes straight to the delivery
    wait (Issue #82: the review session fixes findings in the same
    session, so there is no cold-start fixer to run). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = {
        "run_id": "a1b2c3d4",
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": "https://github.com/owner/repo/pull/12",
    }
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        runner, "verify_resumed_pr",
        lambda *args, **kwargs: scene["pr_url"],
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene["pr_url"], issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_routes_awaiting_review_resume_to_delivery_wait(
    monkeypatch, tmp_path,
):
    """A resumed `ai-pr-opened` delivery (stranded by a dead runner or
    the Issue #70 progress 404) goes straight to the delivery wait:
    the independent review of the same PR runs (Issue #45 round-5
    contract: a clean PR is never sent to a fixer). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = {
        "run_id": "a1b2c3d4",
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": "https://github.com/owner/repo/pull/12",
    }
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        runner, "verify_resumed_pr",
        lambda *args, **kwargs: scene["pr_url"],
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer for a clean PR: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene["pr_url"], issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_rejects_repeated_source_repo_before_execution(
    monkeypatch, tmp_path,
):
    _write_prompts(tmp_path)
    seen = []
    config = tmp_path / "orbi.toml"
    config.write_text(
        "source_repos = [\"xqliu/orbi\", \"xqliu/orbi-backlog\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda *args, **kwargs: seen.append(args) or None,
    )
    assert runner.main(["--config", str(config)]) == 1
    assert seen == []


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.main(["--config", str(config)])


def test_process_issue_failure_without_session_still_carries_scene(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pi died")),
    )
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    # No session file yet: the scene still carries the full debug entry
    # (worktree, branch) with '-' session fields.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body
    assert "session log: `<unavailable>`" in failure_body


def test_tail_text_strips_raw_and_caret_sgr_sequences(tmp_path):
    log = tmp_path / "coloured.log"
    log.write_bytes(
        b"\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m10 passed\x1b[39m\n"
        b"^[[32m\xe2\x9c\x93^[[39m tests/cta-ref.test.js\n"
    )

    result = failure_report._tail_text(log)

    assert "Test Files  10 passed" in result
    assert "tests/cta-ref.test.js" in result
    assert "\x1b[" not in result
    assert "^[" not in result


def test_tail_text_preserves_plain_text_byte_for_byte(tmp_path):
    log = tmp_path / "plain.log"
    content = b"plain output  \ntrailing whitespace\t"
    log.write_bytes(content)

    assert failure_report._tail_text(log) == content.decode()


def test_tail_text_preserves_non_sgr_lookalikes(tmp_path):
    log = tmp_path / "lookalikes.log"
    content = "^[[1] [32m literal \x1b without bracket"
    log.write_text(content, encoding="utf-8")

    assert failure_report._tail_text(log) == content


def test_failure_summary_removes_command_and_stderr_duplication():
    summary = failure._failure_summary(
        "Review failed: Command ['pi', '--session', '/home/runner/session'] "
        "returned non-zero exit status 1; stderr=provider exploded", language_code="en",
    )

    assert "Command [" not in summary
    assert "stderr=" not in summary
    assert "/home/" not in summary
    assert summary.count("provider exploded") == 1
    assert "delivery command failed" in summary
    assert failure._failure_summary(
        "provider exploded stderr=provider exploded", language_code="en"
    ) == "provider exploded"


def test_failure_summary_defaults_when_reason_has_no_readable_content():
    assert failure._failure_summary(" stderr=", language_code="en") == "the delivery command failed"


def test_failure_summary_uses_concrete_final_stderr_line_and_redacts_it():
    summary = failure._failure_summary(
        "the independent review failed: Command ['pi', '--session-dir', "
        "'/home/runner/session'] returned non-zero exit status 1. "
        "stderr=mise installed 0 tools\n"
        "\n"
        "provider log: /home/runner/private/session.jsonl\n"
        "Codex error: The usage limit has been reached", language_code="en",
    )

    assert summary == (
        "the independent review failed: the delivery command failed: "
        "Codex error: The usage limit has been reached"
    )
    assert "/home/" not in summary


def test_failure_scene_is_structured_and_redacts_local_paths():
    rendered = failure_report._failure_scene(
        {"session_id": "s1", "session_file": "/home/a/.pi-session/s.jsonl",
         "phase": "review", "last_activity": "2026-09-20T18:00:00Z",
         "action": "run tests", "result": "failed"},
        run_id="abcdef12", issue="orbi-build/orbi#1221", role="review",
        branch="orbi/task", worktree="/home/a/worktree", pr_url="https://github.com/o/r/pull/1", language_code="en",
    )

    assert "- run: `abcdef12`" in rendered
    assert "- worktree: `local runner worktree`" in rendered
    assert "- session log: `local session log`" in rendered
    assert "/home/" not in rendered
    assert "|" not in rendered


def test_failure_comment_layout_keeps_raw_evidence_collapsed():
    body = failure._failure_comment_body(
        outcome="fix needed", action="Repair the delivery.",
        reason="the provider rejected the request",
        diagnosis="provider rejected the request",
        scene="- run: `abcdef12`\n- phase: `review`",
        evidence="\n\nstderr_tail:\n```\nprovider rejected the request\n```",
        pr_url="https://github.com/o/r/pull/1", issue="o/r#1221",
        run_id="abcdef12", language_code="en",
    )

    visible, collapsed = body.split("<details>", 1)
    assert "Orbi: fix needed — the engine will retry" in visible
    assert "provider rejected the request" in visible
    assert "stderr_tail:" not in visible
    assert "https://github.com/o/r/pull/1" in visible
    assert "run_id=abcdef12" in visible
    assert "stderr_tail:" in collapsed
    assert "```" in collapsed
    assert body.endswith("</details>")

    without_diagnosis = failure._failure_comment_body(
        outcome="fix needed", action="", reason="provider failed",
        diagnosis="", scene="- run: `abcdef12`", evidence="",
        pr_url=None, issue="o/r#1221", run_id="abcdef12", language_code="en",
    )
    assert "failure detail:" not in without_diagnosis
    assert without_diagnosis.endswith("</details>")


def test_blocked_failure_suffix_stays_inside_closed_diagnostics(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        runner.UnrecoverableDeliveryError("cannot recover"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id="abcdef12", pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_REVIEW,
        classify=True, blocked_suffix="the preserved PR needs a decision",
        current_labels=set(), language_code="en",
    )

    assert posted[0].count("<details>") == 1
    assert posted[0].count("</details>") == 1
    assert "the preserved PR needs a decision\n</details>" in posted[0]


def test_no_run_id_failure_still_preserves_action_and_evidence(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        subprocess.CalledProcessError(2, ["pi"], stderr="provider failed"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id=None, pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_IMPLEMENT,
        action="Repair provider access.", reason="delivery failed",
        diagnosis="provider failed", classify=False, evidence=True,
        current_labels=set(), language_code="en",
    )

    assert "**Action:** Repair provider access." in posted[0]
    assert "stderr_tail:" in posted[0]
    assert "provider failed" in posted[0]
    # Issue #1684: the blocked diagnostic ends with the community help
    # line, after the closed evidence block.
    assert posted[0].endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_failure_evidence_handles_binary_streams_and_unavailable_files(tmp_path):
    error = subprocess.CalledProcessError(
        2, ["pi"], output=b"binary stdout", stderr=b"binary stderr",
    )

    evidence = failure_report._failure_evidence(tmp_path / "missing", error, language_code="en")

    assert "exit_code=2" in evidence
    assert "stderr_tail:\n```" in evidence
    assert "stdout_tail:\n```" in evidence
    assert "binary stderr" in evidence
    assert "binary stdout" in evidence
    # Issue #775: raw segments are fenced; a missing file stays a
    # placeholder inside its fence instead of a raw dump.
    assert "test_log_tail:\n```\n<unavailable>\n```" in evidence
    assert "session_last_events (last 20 records; full log: <unavailable>):" \
        in evidence
    assert failure_report._tail_text(tmp_path / "missing.log") == "<unavailable>"


def test_failure_evidence_includes_streams_session_and_test_tail(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    (worktree / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:00:00Z",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "review the PR"},
            ]},
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:01:00Z",
            "message": {"role": "toolResult", "toolName": "Bash",
                        "isError": False},
        }) + "\n",
        encoding="utf-8",
    )
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        "test output\n1 failed, 2 passed\n", encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"], output="stdout tail", stderr="")

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    assert "exit_code=1" in evidence
    assert "stdout tail" in evidence
    assert "1 failed, 2 passed" in evidence
    # Issue #775: the session tail is a structural summary, not raw JSONL.
    assert "2026-09-12T10:00:00Z message role=user content=text" in evidence
    assert "2026-09-12T10:01:00Z message role=toolResult tool=Bash" in evidence
    assert "full log: local session log" in evidence


def test_failure_evidence_keeps_coverage_table_literal_inside_fence(tmp_path):
    """Issue #775: the vitest coverage table must render as monospace
    literal text on GitHub, never parsed as a markdown table."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    raw_record = json.dumps({
        "type": "message", "timestamp": "t1",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "**bold** | not | a | table"},
        ]},
    })
    (worktree / ".pi-session" / "s.jsonl").write_text(raw_record + "\n")
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        " src      | stmts |\n"
        "----------|-------\n"
        " billing  | 96%   |\n",
        encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"])

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    # Every raw segment opens its fence directly under the label.
    for label in ("stderr_tail:", "stdout_tail:", "test_log_tail:"):
        assert f"\n{label}\n```" in evidence
    # The table separator line survives verbatim inside the test-log
    # fence (label -> fence -> table row).
    assert "test_log_tail:\n```\n src      | stmts |\n----------|-------\n" \
        in evidence
    # The session record text never enters the comment: the summary
    # carries the shape, the fence carries the log path.
    assert "bold" not in evidence
    assert raw_record not in evidence


def test_failure_evidence_dedupes_repeated_session_text_block(tmp_path):
    """Issue #775: pi logs the final assistant text twice (once inside
    the full message, once standalone); the summary keeps it once."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    final_text = "审查完成。结论：verdict pass"
    duplicated = [
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking"}, {"type": "text", "text": final_text},
         ]}},
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": final_text},
         ]}},
    ]
    (worktree / ".pi-session" / "s.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in duplicated),
        encoding="utf-8",
    )

    evidence = failure_report._failure_evidence(
        worktree, subprocess.CalledProcessError(1, ["pi"]), language_code="en",
    )

    assert evidence.count("role=assistant") == 1
    assert final_text not in evidence


def test_session_summary_skips_malformed_records_and_respects_limit(tmp_path):
    """Defensive edges: malformed lines, messages and blocks are skipped,
    the toolCall kind carries its name, the limit keeps the LAST records,
    and an all-unparsable log degrades to the placeholder."""
    good = {"type": "message", "timestamp": "t", "message": {
        "role": "assistant",
        "content": [{"type": "toolCall", "name": "Bash"}, "junk", 3],
    }}
    path = tmp_path / "s.jsonl"
    path.write_text(
        "not json\n"
        "[1, 2]\n"
        + json.dumps({"type": "x", "message": "not-a-dict"}) + "\n"
        + json.dumps(good) + "\n",
        encoding="utf-8",
    )

    assert failure_report._session_summary(path, limit=1) == (
        "t message role=assistant content=toolCall:Bash"
    )

    many = tmp_path / "many.jsonl"
    many.write_text("".join(
        json.dumps({"type": f"r{i}", "timestamp": f"t{i}"}) + "\n"
        for i in range(5)
    ), encoding="utf-8")
    assert failure_report._session_summary(many, limit=2) == "t3 r3\nt4 r4"

    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n[1]\n", encoding="utf-8")
    assert failure_report._session_summary(bad) == "<unparsable>"

    # An unreadable/missing file is the placeholder (also the race path
    # where the session vanishes between the glob and this read).
    assert failure_report._session_summary(tmp_path / "missing.jsonl") == "<unavailable>"


def test_failure_evidence_truncates_oversized_segment_with_note(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    evidence = failure_report._failure_evidence(
        worktree,
        subprocess.CalledProcessError(1, ["pi"], stderr="x" * 9000), language_code="en",
    )

    assert "[truncated, 5000 chars omitted]" in evidence


def test_report_delivery_failure_caps_comment_and_names_session_log(
    monkeypatch, tmp_path,
):
    """Issue #775 + #1221: an oversized raw cause becomes a bounded
    visible summary while its bounded diagnostic evidence remains reachable."""
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *args, **kwargs: set())
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    error = subprocess.CalledProcessError(1, ["pi"])
    worktree = tmp_path / "wt"
    session_file = worktree / ".pi-session" / "s.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n", encoding="utf-8")

    outcome = failure_report.report_delivery_failure(
        error, issue={"number": 775, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=worktree, branch="b", role=runner.ROLE_IMPLEMENT,
        cause="x" * 25000,
        classify=False, evidence=True, language_code="en",
    )

    assert outcome == "blocked"
    body = posted[0]
    # Issue #1322: the hidden machine-readable block leads the readable
    # hierarchy (the run marker is prepended by `report_delivery_failure`
    # when a run_id is bound; this call has none).
    assert body.startswith("<!-- orbi:failure:v1 ")
    assert "\n\nOrbi: blocked — waiting on a human decision" in body
    assert "**Reason:** " + "x" * 500 in body
    assert len(body) < failure_report.FAILURE_COMMENT_MAX_CHARS
    assert "full log: local session log" in body
    # Issue #1684: the blocked diagnostic ends with the community help
    # line; the body stays under the cap, so it is not truncated away.
    assert body.endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_process_issue_failure_comment_includes_session_scene(monkeypatch, tmp_path):
    calls = []
    (tmp_path / "wt" / ".pi-session").mkdir(parents=True)
    (tmp_path / "wt" / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-08-25T02:30:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "assistant event"},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "wt" / ".orbi").mkdir()
    (tmp_path / "wt" / ".orbi" / "test.log").write_text(
        "coverage output\n1 failed\n", encoding="utf-8",
    )
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="boom"),
        ),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-9",
        "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
        "phase": "test",
        "last_activity": "2026-08-25T02:30:00Z",
        "action": "bash pytest tests/",
        "result": "ok",
    })
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    assert "Orbi: blocked" in failure_body
    assert "session log: `local session log`" in failure_body
    assert "phase: `test`" in failure_body
    assert "last activity: `2026-08-25T02:30:00Z`" in failure_body
    assert "action: `bash pytest tests/`" in failure_body
    assert "result: `ok`" in failure_body
    assert "exit_code=1" in failure_body
    assert "stderr=boom" not in failure_body
    assert "stderr_tail:\n```\nboom\n```" in failure_body
    # Issue #775: the session tail appears as its structured summary
    # line inside a fence, not as raw session text.
    assert "2026-08-25T02:30:00Z message role=assistant content=text" \
        in failure_body
    assert "assistant event" not in failure_body
    assert "1 failed" in failure_body
    # The reader-facing scene uses stable labels for local paths.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body


def test_process_issue_isolates_scene_lookup_failure(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The main-path resume-context read (Issue #219) succeeds; the
    # failure-scene lookup is the one that dies on the disk error.
    snapshot_calls = []

    def flaky_snapshot(session_dir):
        snapshot_calls.append(1)
        if len(snapshot_calls) == 1:
            return None
        raise OSError("disk error")

    monkeypatch.setattr(failure_report, "activity_snapshot", flaky_snapshot)
    # The main-path scene read now lives in `pi_session.resume_context`
    # (Issue #1262): patch the same flaky snapshot there too so the
    # first (resume-context) call succeeds and the runner's
    # failure-scene lookup is the one that dies.
    monkeypatch.setattr(pi_session, "activity_snapshot", flaky_snapshot)
    with caplog.at_level("ERROR"):
        # Issue #239: the failure is terminal — `process_issue` returns
        # `None` instead of re-raising; the scene-isolation assertions
        # below are unchanged.
        assert runner.process_issue({"number": 9, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "git failed" in failure_body


def test_log_format_has_no_python_timestamp():
    # journald already provides time, host and process (Issue #40): the
    # Python logger must not print a second timestamp.
    formatter = logging.Formatter(runner.log_format())
    record = logging.LogRecord(
        "orbi.bootstrap", logging.INFO, "file", 1, "message", None, None,
    )
    assert formatter.format(record) == "INFO message"


# --- live GitHub progress while Pi runs (Issue #18, review Major fix) ------


def test_stream_pi_invokes_progress_callback_while_child_is_running(
    tmp_path,
):
    """The review Major fix: the progress callback (which PATCHes the
    GitHub comment) must fire WHILE the Pi child is still running — on
    activity changes and on heartbeats — not only after it exits."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="final answer",
        sleep=1.2,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The callback fired many times during the run (polls at 0.1 s while
    # the child sleeps 1.2 s after the last session record)...
    assert len(seen) >= 4
    # ...and it saw the activity change (starting -> bash) live, before
    # the child exited: the tool call is visible in an early snapshot,
    # not only in a final state.
    # Issue #176: the live snapshots show the startup sub-phases
    # (request_pending: the session exists, the first response has not
    # arrived yet), not the generic `starting`.
    # Issue #741: the FIRST frame is load-dependent — on a busy machine
    # the first poll can predate the session file, so that frame is
    # `session_pending` (equally legal, pinned by the dedicated
    # regression test). This test pins the ORDER: request_pending must
    # appear, and before the tool phase.
    phases = [entry["phase"] for entry in seen]
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")
    assert phases.index("bash") < len(phases) - 1
    tool_entries = [entry for entry in seen if entry["phase"] == "bash"]
    assert any(
        entry["action"] == "bash pytest tests/"
        for entry in tool_entries
    )


def test_stream_pi_progress_first_frame_may_be_session_pending(tmp_path):
    """Issue #741 regression: on a busy machine the first progress frame
    fires BEFORE Pi creates its session file, so the startup sub-phase
    of that frame is `session_pending` — as legal as `request_pending`
    (Issue #176). The session record below is delayed by 1.0 s, so the
    forced scene is the exact #741 flake: the first frames predate the
    session file."""
    (_, session), (_, user), (_, assistant) = fake_session_records()
    command = make_fake_pi(
        tmp_path,
        session_records=[(1.0, session), (0.1, user), (0.1, assistant)],
        stdout="final answer",
        sleep=0.6,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The forced #741 scene: the first frame predates the session file,
    # so it is the `session_pending` startup sub-phase — never a reason
    # to fail (Issue #741).
    phases = [entry["phase"] for entry in seen]
    assert phases[0] == "session_pending"
    # The order contract (Issue #176): the startup sub-phases stay
    # visible and ordered — request_pending appears, before the tool
    # phase — regardless of which frame the sampling started in.
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")


def test_stream_pi_live_progress_patches_github_before_child_exits(
    tmp_path,
):
    """End-to-end shape of the fix: a publisher-style callback PATCHes
    the run-marker comment while the child is still running, so a mobile
    user never sees a static starting comment for the whole run."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="done",
        sleep=1.2,
    )
    patches = []

    class FakePublisher:
        language_code = "en"
        comment_id = 77

        def patch(self, body):
            patches.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="owner/repo"), publisher, title="Live progress", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=throttle)
    assert result == "done"
    # At least one PATCH happened while the child was still running
    # (all of them did: the callback only exists inside the poll loop).
    assert len(patches) >= 2
    # The live comment carries the run marker and the live phase.
    assert all(
        body.startswith("<!-- orbi:run=deadbeef -->")
        for body in patches
    )
    assert any("- phase: bash" in body for body in patches)
    # Issue #1657: the live body carries the versioned summary block and
    # the safe activity; the raw bash command reaches neither.
    assert all(body.count("<!-- orbi:progress:v1 ") == 1 for body in patches)
    assert any("- activity: command" in body for body in patches)
    assert not any("pytest tests/" in body for body in patches)


def test_stream_pi_progress_callback_error_never_interrupts_run(
    tmp_path, caplog,
):
    """Observability is best-effort: a failing progress callback (e.g. a
    transient gh failure) is logged and never interrupts the delivery."""
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )

    def boom(activity):
        raise RuntimeError("gh api failed")

    with caplog.at_level("ERROR"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=boom)
    assert result == "final answer"
    assert "progress_publish_failed" in caplog.text
    assert "run=deadbeef" in caplog.text
    assert "role=implement" in caplog.text


def test_live_progress_throttle_patches_on_change_and_cadence():
    """The throttle passes an update through on a visible activity change
    and at most every PI_HEARTBEAT_SECONDS (30 s) otherwise."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=1, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Throttle task", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "starting", "action": None, "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
    }
    throttle(first)
    assert len(calls) == 1  # the first update always passes
    # Unchanged activity within the cadence: suppressed.
    throttle(first)
    assert len(calls) == 1
    # A visible change: passed immediately.
    changed = dict(
        first, phase="test", action="bash pytest tests/", kind="command",
    )
    throttle(changed)
    assert len(calls) == 2
    # Unchanged again within the cadence: suppressed...
    throttle(changed)
    assert len(calls) == 2
    # ...until the 30 s cadence elapses: passed as a heartbeat update.
    throttle._last_patch -= progress.PI_HEARTBEAT_SECONDS + 1
    throttle(changed)
    assert len(calls) == 3
    # The rendered body carries the live state (Issue #1657: the readable
    # activity and the block share the safe value; the raw command never
    # reaches either).
    assert "- phase: test" in calls[-1]
    assert "- activity: command" in calls[-1]
    assert "pytest tests/" not in calls[-1]
    assert calls[-1].count("<!-- orbi:progress:v1 ") == 1


def test_live_progress_throttle_patches_on_recovery_change():
    """Issue #94: the recovery state is part of the visible progress —
    entering idle recovery (None -> `term`) PATCHes the live comment
    immediately even though phase/action/result did not change, and
    the rendered body carries the `recovery` line."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=94, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Idle recovery", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "test", "action": "bash pytest tests/", "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
        "recovery": None,
    }
    throttle(first)
    assert len(calls) == 1
    assert "- recovery" not in calls[0]  # no recovery line when idle-recovery is off
    # Entering recovery: visible change -> immediate PATCH with the line.
    throttle(dict(first, recovery="term"))
    assert len(calls) == 2
    assert "- recovery: term" in calls[1]
    # Escalating to KILL: another visible change -> another PATCH.
    throttle(dict(first, recovery="kill"))
    assert len(calls) == 3
    assert "- recovery: kill" in calls[2]
    # Back to None after the resume: visible change -> PATCH without the line.
    throttle(first)
    assert len(calls) == 4
    assert "- recovery" not in calls[3]


def test_run_pi_passes_progress_callback_to_stream_pi(monkeypatch, tmp_path):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()), progress=callback)
    assert seen["progress"] is callback
    assert seen["ctx"].run_id == "a1b2c3d4"
    assert seen["ctx"].branch == "b"


def test_run_review_passes_progress_callback_to_stream_pi(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1, progress=callback)
    assert seen["progress"] is callback
    assert seen["role"] == "review"


# --- Issue #228: the configured model_wait threshold reaches stream_pi ------

def test_run_pi_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the implement session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5))
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_review_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the review session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5), 1)
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_pi_keeps_module_default_without_config_key(
    monkeypatch, tmp_path,
):
    """Issue #228: a caller config without the key (the pre-#228 shape)
    keeps the module constant — the real `load_config` always provides
    the key, this only pins the fallback."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()))
    assert seen["watch"].model_wait_dead_seconds == (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


# --- role-specific --skill lists (Issue #83) ---------------------------------


def _skill_config(tmp_path, *names):
    """Build a config whose skills point at <name>/SKILL.md files."""
    skills = []
    for name in names:
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        skills.append(skill_dir / "SKILL.md")
    return config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=skills, base_branch="main", base_sha="abc123def456", run_id="a1b2c3d4")


def _command_skills(command):
    """Return the --skill values of an assembled pi command."""
    return [
        value for index, item in enumerate(command)
        if item == "--skill" and (value := command[index + 1])
    ]


def test_run_pi_keeps_tdd_dev_and_code_review_drops_review_fix_loop(
    monkeypatch, tmp_path,
):
    """Issue #83: the implementer/fixer keeps the delivery skills
    (tdd-dev, code-review) but not review-fix-loop — the Runner runs
    the independent review/fix loop itself after the PR is open."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    skills = _command_skills(calls[0])
    assert any("tdd-dev" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)


def test_run_review_keeps_only_code_review(monkeypatch, tmp_path):
    """Issue #83: the read-only review session must not load the
    delivery-oriented skills (tdd-dev would steer it into the
    implement/test/PR flow, review-fix-loop would open another
    fix/review round); code-review stays."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert not any("tdd-dev" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)


def test_run_pi_and_run_review_skill_lists_differ(monkeypatch, tmp_path):
    """Issue #83 acceptance: the two assembled command lines carry
    different --skill lists."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    implement_skills = _command_skills(calls[0])
    review_skills = _command_skills(calls[1])
    assert implement_skills != review_skills


def test_skill_name_of_skill_md_inside_skill_directory(tmp_path):
    path = tmp_path / "skills" / "tdd-dev" / "SKILL.md"
    path.parent.mkdir(parents=True)
    assert pi_command._skill_name(path) == "tdd-dev"


def test_skill_name_of_bare_markdown_entry(tmp_path):
    path = tmp_path / "my-skill.md"
    assert pi_command._skill_name(path) == "my-skill"


def test_run_review_keeps_non_delivery_skill_names(monkeypatch, tmp_path):
    """Only tdd-dev/review-fix-loop are excluded from the review; any
    other configured skill (including code-review) passes through.
    """
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(tmp_path, "code-review", "platform-qa")
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert any("code-review" in skill for skill in skills)
    assert any("platform-qa" in skill for skill in skills)


# --- max_concurrency config (Issue #39) --------------------------------------


def test_load_config_defaults_max_concurrency_to_one(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = config_domain.load_config(config_path)
    assert config.max_concurrency == 1


def test_load_config_reads_explicit_max_concurrency(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 2\n',
        encoding="utf-8",
    )
    config = config_domain.load_config(config_path)
    assert config.max_concurrency == 2


def test_load_config_derives_slot_dir_from_repo_dir(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = config_domain.load_config(config_path)
    assert config.slot_dir == (tmp_path / "repo").resolve() / ".orbi" / "slots"


@pytest.mark.parametrize(
    "value",
    [3, 5],
)
def test_load_config_accepts_capacity_up_to_max_runner_instances(
    tmp_path, value,
):
    """Issue #827: every capacity up to MAX_RUNNER_INSTANCES (5) is a
    legal config — 3 (the incident config) and the cap itself load."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["owner/repo"]\nmax_concurrency = {value}\n',
        encoding="utf-8",
    )
    assert config_domain.load_config(config_path).max_concurrency == value


def test_load_config_rejects_capacity_beyond_max_with_the_real_cap(tmp_path):
    """Issue #827: beyond the declaration cap the load fails fast naming
    the REAL cap and reason — never the old hardcoded "Runner timer
    instance" wording. Issue #829: the error also carries the CURRENT
    value (the incident's blast radius: the operator must see what the
    config wrote, not just the legal range)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 6\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        config_domain.load_config(config_path)
    message = str(excinfo.value)
    assert "no greater than 5 (MAX_RUNNER_INSTANCES)" in message
    assert "got 6" in message


@pytest.mark.parametrize(
    "value, got",
    [
        ("0", "0"),
        ("-1", "-1"),
        ("1.5", "1.5"),
        ('"1"', "'1'"),
        ("true", "True"),
        ("false", "False"),
        ("6", "6"),
    ],
)
def test_load_config_rejects_invalid_max_concurrency(tmp_path, value, got):
    """Issue #829: every rejected value — out of range or of the wrong
    type — is echoed back verbatim in the error (`got <value>`)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["owner/repo"]\nmax_concurrency = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        config_domain.load_config(config_path)
    message = str(excinfo.value)
    assert "max_concurrency must be a positive integer" in message
    assert f"got {got}" in message


# --- main() slot acquisition (Issue #39) --------------------------------------


def test_main_capacity_full_does_not_pick_issue_or_call_pi(
    monkeypatch, tmp_path, caplog,
):
    """A full slot stops the runner before any claim or Pi invocation."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 1\n',
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    # The single slot is already HELD (lock) by this live process: a
    # file on disk alone is not a held slot (flock is the token).
    slot_dir = tmp_path / ".orbi" / "slots"
    held = pilot_slots.acquire_slot(slot_dir, 1, os.getpid())
    assert held is not None

    def fail_if_called(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        raise AssertionError("pick_next_delivery must not run when capacity is full")

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    monkeypatch.setattr(runner, "process_issue", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="must not run when capacity is full"):
        fail_if_called(["owner/repo"], Path("slots"), 1)
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "capacity_full" in caplog.text
    assert "max_concurrency=1" in caplog.text
    assert "slot_dir=" in caplog.text
    # The pre-existing holder keeps its slot (no claim, no label change,
    # no Pi).
    assert (
        pilot_slots.slot_occupancy(slot_dir, 1) == [(1, os.getpid())]
    )
    held.release()


def test_main_holds_slot_while_processing_issue(monkeypatch, tmp_path):
    """The slot is acquired before the pick and held for the whole task.

    Issue #1319: the claim lock is held for the SAME pick and is free
    again while the Pi session runs — the hold spans the scan -> identity
    write, never a Pi session.
    """
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}
    slot_dir = tmp_path / ".orbi" / "slots"

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["occupancy"] = pilot_slots.slot_occupancy(
            tmp_path / ".orbi" / "slots", 1,
        )
        seen["claim_window"] = pilot_slots.is_claim_lock_held(slot_dir)
        return ("owner/repo", issue, None)

    def fake_process(*args, **kwargs):
        # The first Pi session starts only after `mark_slot_delivery`.
        seen["pi_session"] = pilot_slots.is_claim_lock_held(slot_dir)
        return runner.IssueResult("pr", "https://x/y/pull/12")

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setattr(runner, "process_issue", fake_process)
    monkeypatch.setattr(runner, "delivery_step", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["occupancy"] == [(1, os.getpid())]
    assert seen["claim_window"] is True
    assert seen["pi_session"] is False


def test_main_reacquires_slot_after_previous_release(monkeypatch, tmp_path):
    """After the holder releases (process exit), the next run takes the slot."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )

    assert runner.main(["--config", str(config)]) == 0
    slot_dir = tmp_path / ".orbi" / "slots"
    # The slot file exists and was released when main() returned ...
    assert (slot_dir / "slot-1").exists()
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    # ... so the next run takes the slot again.
    assert runner.main(["--config", str(config)]) == 0
    assert (slot_dir / "slot-1").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_main_delegates_all_preflight_to_one_helper(
    monkeypatch, tmp_path,
):
    """Issue #297: the pre-slot checks live behind one `_preflight`
    helper, so a test reaches the normal claim flow by stubbing THAT
    one function — not four unrelated modules. The helper receives the
    validated config and `main` still takes the slot afterwards."""
    seen: dict = {}

    def fake_preflight(config):
        seen["config"] = config

    monkeypatch.setattr(runner, "_preflight", fake_preflight)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["config"].source_repos == ("owner/repo",)
    # The slot was taken only after the (stubbed) preflight ran.
    assert (tmp_path / ".orbi" / "slots" / "slot-1").exists()


# --- deployment consistency preflight (Issue #103) --------------------------


def _drift_world(tmp_path, drift: bool) -> tuple[Path, Path]:
    """A deployment checkout plus an installed unit dir.

    With `drift` the installed timer carries one extra line; without
    it both units match the repo templates.
    """
    import shutil

    repo = tmp_path / "repo"
    (repo / "systemd").mkdir(parents=True)
    for name, body in (
        ("orbi@.service", "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n"),
        ("orbi@.timer", "[Timer]\nOnCalendar=*-*-* *:00/5\n"),
    ):
        (repo / "systemd" / name).write_text(body, encoding="utf-8")
    installed = tmp_path / "units"
    installed.mkdir()
    for name in ("orbi@.service", "orbi@.timer"):
        target = installed / name
        shutil.copyfile(repo / "systemd" / name, target)
        if drift and name == "orbi@.timer":
            with target.open("a", encoding="utf-8") as handle:
                handle.write("# drift\n")
    return repo, installed


def _fake_preflight_run(monkeypatch, installed: Path) -> list:
    """A recording run_command for the preflight self-heal (Issue #142).

    The real `install_units` would run `systemctl --user` against the
    real machine; the wiring tests record the commands instead and
    keep the install's file copy real (that is what is under test).
    `git rev-parse HEAD` returns a fixed commit; every other command
    succeeds with empty output.
    """
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    return calls


@_unit_drift_systemd
def test_main_unit_drift_blocks_claim_before_slot(monkeypatch, tmp_path,
                                                  caplog):
    """Issue #142: a drift the self-heal CANNOT resolve (the re-verify
    still sees it after the idempotent install) fails the start BEFORE
    any slot or claim: the structured `unit_drift` line (repo path,
    installed path, hashes, fix command) is logged, the tick raises
    (non-zero exit), no slot is taken and nothing is claimed — while
    a currently RUNNING task is never interrupted (only the next
    start is blocked)."""
    from orbi import scheduler, systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    # The real preflight (overrides the conftest default no-op).
    monkeypatch.setattr(
        runner, "check_unit_drift", scheduler.check_unit_drift,
    )
    # The install's copy is real; the external steps are recorded.
    calls = _fake_preflight_run(monkeypatch, installed)
    # The installed timer is re-tampered right after every copy: the
    # re-verify still sees the drift (an unresolvable scene).
    real_write_bytes = Path.write_bytes

    def re_tamper(self, data):
        real_write_bytes(self, data)
        if self.name == "orbi@.timer" and str(self).startswith(
            str(installed),
        ):
            real_write_bytes(self, data + b"# drift\n")

    monkeypatch.setattr(Path, "write_bytes", re_tamper)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("pick_next_delivery must not run on unit drift")

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on unit drift",
    ):
        fail_if_called()
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.UnitDriftError, match="unit_drift"):
            runner.main(["--config", str(config)])
    # The self-heal is enable=False (Issue #1364): the read-only
    # FragmentPath probe plus exactly one daemon-reload that re-reads
    # the rewritten templates, and NO enable/disable — a timer the
    # operator disabled stays disabled.
    assert [command[2] for command in calls
            if command[:2] == ["systemctl", "--user"]] == [
        "show", "daemon-reload",
    ]
    # The structured line carries what the Issue requires.
    assert "unit_drift unit=orbi@.timer" in caplog.text
    assert f"repo={repo / 'systemd' / 'orbi@.timer'}" in caplog.text
    assert f"installed={installed / 'orbi@.timer'}" in caplog.text
    assert "repo_sha256=" in caplog.text
    assert "installed_sha256=" in caplog.text
    assert 'fix="orbi install-units"' in caplog.text
    # No slot was taken and nothing was claimed.
    assert not (repo / ".orbi" / "slots").exists()


@_unit_drift_systemd
def test_main_unit_drift_auto_syncs_and_proceeds_to_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: the normal scene — a template change merged to main
    (the ExecStartPre-synced checkout carries the new templates, the
    installed units are still the old ones). The preflight self-heals
    with the SAME idempotent install (copy, daemon-reload — never
    start/stop/restart the service) and ``enable=False`` (Issue #1364:
    no timer the operator disabled is re-enabled), the re-verify is
    clean, the structured `auto_synced` line is logged and the tick
    proceeds to the normal claim flow (slot taken, queue scanned).
    No more per-tick drift loop until a human intervenes."""
    from orbi import scheduler, systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", scheduler.check_unit_drift,
    )
    calls = _fake_preflight_run(monkeypatch, installed)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    # The self-heal only re-reads the rewritten units (enable=False):
    # the read-only FragmentPath probe plus exactly one daemon-reload,
    # no enable/disable, no service command.
    assert [command[2] for command in calls
            if command[:2] == ["systemctl", "--user"]] == [
        "show", "daemon-reload",
    ]
    # The repo template won: the installed unit matches it again.
    status = scheduler.unit_status(repo, installed)
    assert all(entry["drifted"] is False for entry in status)
    assert "unit_drift result=auto_synced unit=orbi@.timer" in caplog.text
    assert "commit=0123456789abcdef0123456789abcdef01234567" in caplog.text
    # The tick proceeded: the slot was taken AFTER the preflight passed.
    assert (repo / ".orbi" / "slots" / "slot-1").exists()


@_unit_drift_systemd
def test_main_unit_drift_auto_sync_failure_blocks_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: a failing self-heal (e.g. daemon-reload fails) fails
    fast BEFORE any slot or claim — the install error propagates,
    nothing is claimed, and the scene stays in the journal."""
    from orbi import scheduler, systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", scheduler.check_unit_drift,
    )

    def failing_run(command, **kwargs):
        # The read-only loaded-unit probe comes first (Issue #1459);
        # the daemon-reload of the enable=False install is what must
        # fail fast here.
        if command[2] == "show":
            return ""
        assert command == ["systemctl", "--user", "daemon-reload"]
        raise subprocess.CalledProcessError(1, command, stderr="nope")

    monkeypatch.setattr(seam, "run_command", failing_run)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("pick_next_delivery must not run on unit drift")

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on unit drift",
    ):
        fail_if_called()
    with pytest.raises(subprocess.CalledProcessError):
        runner.main(["--config", str(config)])
    assert not (repo / ".orbi" / "slots").exists()


@_unit_drift_systemd
def test_main_unit_drift_clean_proceeds_to_claim(monkeypatch, tmp_path,
                                                 caplog):
    """Issue #103: matching units log `unit_drift result=clean` and the tick
    proceeds to the normal claim flow (slot taken, queue scanned)."""
    from orbi import scheduler, systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=False)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", scheduler.check_unit_drift,
    )
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "unit_drift result=clean" in caplog.text
    # The slot was taken AFTER the preflight passed.
    assert (repo / ".orbi" / "slots" / "slot-1").exists()


def test_main_preflight_receives_the_configured_repo_dir(
    monkeypatch, tmp_path,
):
    """Issue #103: the preflight checks the configured repo_dir (the
    ExecStartPre-synced deployment checkout), never a guess."""
    seen: list[Path] = []

    def fake_check(repo_dir, *args, **kwargs):
        seen.append(Path(repo_dir))

    monkeypatch.setattr(runner, "check_unit_drift", fake_check)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen == [tmp_path]


# --- git transport preflight (Issue #114) ------------------------------------


def test_main_transport_check_blocks_claim_before_slot(
    monkeypatch, tmp_path, caplog,
):
    """Issue #114: an unusable git transport fails the start BEFORE any
    slot or claim: the structured transport reason is raised (non-zero
    exit), no slot is taken and nothing is claimed — no HTTPS fallback,
    no silent skip."""
    from orbi import git_transport

    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "pick_next_delivery must not run on transport failure"
        )

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on transport failure",
    ):
        fail_if_called()
    monkeypatch.setattr(
        runner, "check_transport",
        lambda *a, **k: (_ for _ in ()).throw(
            git_transport.TransportError(
                "ssh_unreachable: git ls-remote "
                "git@github.com:owner/repo.git failed: "
                "Permission denied (publickey)"
            ),
        ),
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(
            git_transport.TransportError, match="ssh_unreachable",
        ):
            runner.main(["--config", str(config)])
    # The structured reason is logged with the run-free preflight scene.
    assert "transport_check_failed" in caplog.text
    assert "ssh_unreachable" in caplog.text
    # No slot was taken and nothing was claimed.
    assert not (tmp_path / ".orbi" / "slots").exists()


def test_main_transport_check_clean_proceeds_to_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #114: a passing transport check logs `transport result=clean` and
    the tick proceeds to the normal claim flow (slot taken, queue
    scanned)."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    monkeypatch.setattr(
        runner, "check_transport",
        lambda *a, **k: {
            "remote": "origin", "protocol": "ssh",
            "url": "git@github.com:owner/repo.git",
            "expected": "git@github.com:owner/repo.git",
            "migrated": False, "ssh_reachable": True,
        },
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "transport result=clean" in caplog.text
    # The slot was taken AFTER the preflight passed.
    assert (tmp_path / ".orbi" / "slots" / "slot-1").exists()


def test_main_transport_preflight_receives_the_configured_args(
    monkeypatch, tmp_path,
):
    """Issue #114: the preflight checks the configured repo_dir and
    source repos with the real run_command, and never migrates (the
    migration is the human-run setup entry's job). Issue #580: the
    configured git_transport mode is passed through (default ssh)."""
    seen: dict = {}

    def fake_check(repo_dir, source_repos, **kwargs):
        seen["repo_dir"] = Path(repo_dir)
        seen["source_repos"] = list(source_repos)
        seen["migrate"] = kwargs.get("migrate")
        seen["mode"] = kwargs.get("mode")
        seen["run_command"] = kwargs.get("run_command")
        return {}

    monkeypatch.setattr(runner, "check_transport", fake_check)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["repo_dir"] == tmp_path
    assert seen["source_repos"] == ["owner/repo"]
    assert seen["migrate"] is False
    assert seen["mode"] == "ssh"
    assert seen["run_command"] is runner.run_command


def test_main_transport_preflight_honors_the_https_config(
    monkeypatch, tmp_path, caplog,
):
    """Issue #580: `git_transport = "https"` reaches the preflight —
    the start gate checks the HTTPS transport, not SSH."""
    seen: dict = {}

    def fake_check(repo_dir, source_repos, **kwargs):
        seen["mode"] = kwargs.get("mode")
        return {
            "remote": "origin", "protocol": "https",
            "url": "https://github.com/owner/repo.git",
            "expected": "https://github.com/owner/repo.git",
            "migrated": False, "ssh_reachable": None,
            "transport_reachable": True,
        }

    monkeypatch.setattr(runner, "check_transport", fake_check)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\ngit_transport = "https"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert seen["mode"] == "https"
    # The clean line reports the ACTIVE transport's reachability.
    assert "protocol=https" in caplog.text
    assert "transport_reachable=True" in caplog.text


def test_main_cli_install_refresh_runs_before_slot_and_claim(
    monkeypatch, tmp_path,
):
    """Issue #158: the editable CLI install refresh runs in the Runner
    tick entry BEFORE any slot or claim, with the configured repo_dir
    and the real run_command — so a stale editable finder (a merged
    packaging change) is refreshed before the tick can claim work,
    and the NEXT CLI process can import the new runtime modules."""
    seen: dict = {}

    def fake_refresh(repo_dir, *, run_command, lock_timeout_seconds=300.0):
        seen["repo_dir"] = Path(repo_dir)
        seen["run_command"] = run_command
        seen["slot_existed_at_refresh"] = (
            Path(repo_dir) / ".orbi" / "slots"
        ).exists()
        return "unchanged"

    monkeypatch.setattr(seam, "refresh_cli_install", fake_refresh)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["repo_dir"] == tmp_path
    assert seen["run_command"] is runner.run_command
    # BEFORE the slot: at the refresh moment no slot exists yet (the
    # slot is taken only after every preflight passed).
    assert seen["slot_existed_at_refresh"] is False
    # ...and the tick proceeded to the normal claim flow (slot taken
    # after the refresh).
    assert (tmp_path / ".orbi" / "slots" / "slot-1").exists()


def test_main_cli_refresh_uses_deploy_home_not_repo_dir(
    monkeypatch, tmp_path,
):
    """Issue #330: with deploy_home set the CLI self-update acts on the
    deployment home — the delivery checkout (repo_dir) is never treated
    as the orbi editable-install source."""
    seen: dict = {}

    def fake_refresh(repo_dir, *, run_command, lock_timeout_seconds=300.0):
        seen["repo_dir"] = Path(repo_dir)
        return "unchanged"

    monkeypatch.setattr(seam, "refresh_cli_install", fake_refresh)
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "check_transport",
        lambda *a, **k: {
            "remote": "origin", "protocol": "ssh",
            "url": "git@github.com:owner/repo.git",
            "expected": "git@github.com:owner/repo.git",
            "migrated": False, "ssh_reachable": True,
        },
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts").mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (home / "prompts" / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "."\n'
        'deploy_home = "home"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["repo_dir"] == home


def test_main_unit_drift_check_uses_deploy_home(
    monkeypatch, tmp_path,
):
    """Issue #330: the pre-start unit drift check and its self-heal act on
    the deployment home's templates, never the delivery checkout."""
    seen: list[Path] = []

    def fake_check(repo_dir, *args, **kwargs):
        seen.append(Path(repo_dir))

    monkeypatch.setattr(runner, "check_unit_drift", fake_check)
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged",
    )
    monkeypatch.setattr(
        runner, "check_transport",
        lambda *a, **k: {
            "remote": "origin", "protocol": "ssh",
            "url": "git@github.com:owner/repo.git",
            "expected": "git@github.com:owner/repo.git",
            "migrated": False, "ssh_reachable": True,
        },
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "prompts").mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (home / "prompts" / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "."\n'
        'deploy_home = "home"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen == [home]


def test_main_cli_install_failure_fails_fast_before_slot_and_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #158: a failing editable install fails the start BEFORE
    any slot or claim (non-zero exit, the structured
    `cli_install_failed` line in the journal), nothing is claimed, no
    slot is taken — no fallback, no skipped tick."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "pick_next_delivery must not run on cli install failure"
        )

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    with pytest.raises(
        AssertionError, match="must not run on cli install failure",
    ):
        fail_if_called()
    monkeypatch.setattr(seam, "refresh_cli_install",
        lambda *a, **k: (_ for _ in ()).throw(
            runner.CliInstallError(
                "editable CLI install failed for /repo: uv exploded "
                "(fix: uv tool install --force --reinstall --editable "
                "--python /usr/bin/python3 /repo)"
            ),
        ),
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(
            runner.CliInstallError, match="CLI install failed",
        ):
            runner.main(["--config", str(config)])
    # No slot was taken and nothing was claimed.
    assert not (tmp_path / ".orbi" / "slots").exists()


# --- delivery lifecycle: slot held until merge or terminal failure ----------

PR_URL = "https://github.com/owner/repo/pull/46"


def fake_pr_view(monkeypatch, state: str) -> tuple[list, object]:
    """Answer `gh pr view <n> --repo owner/repo with delivery fields`.

    Returns the command log and the fake itself (so a test can prove
    the fake rejects unexpected commands).
    """
    seen: list = []

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"] and command[1] == "pr" \
                and command[2] == "view":
            seen.append(command)
            assert command[3] == "46"
            assert command[4:] == [
                "--repo", "owner/repo", "--json",
                "state,statusCheckRollup",
            ]
            return json.dumps({"state": state, "statusCheckRollup": []})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    return seen, fake_run


def test_fake_pr_view_rejects_unexpected_commands(monkeypatch):
    seen, fake_run = fake_pr_view(monkeypatch, "OPEN")
    assert seen == []
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_pr_delivery_status_rejects_non_array_check_rollup(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({"state": "OPEN", "statusCheckRollup": {}}),
    )
    with pytest.raises(ValueError, match="statusCheckRollup must be a JSON array"):
        runner.pr_delivery_status(PR_URL, "owner/repo")


def test_pr_delivery_status_ignores_malformed_check_entry(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({
            "state": "OPEN", "statusCheckRollup": [None],
        }),
    )
    assert runner.pr_delivery_status(PR_URL, "owner/repo") == ("OPEN", [])


def test_terminal_failure_parts_render_action_before_reason_and_diagnosis():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Approve the PR manually.",
        reason="Branch protection requires one human approval.",
        diagnosis="rounds=3; mergeable=CONFLICTING",
        detail="ignored", next_step="", pr_url=None, number=39,
        source_repo="owner/repo", language_code="en",
    )
    assert body.index("Approve the PR manually.") < body.index(
        "Branch protection requires one human approval."
    )
    assert body.index("Branch protection requires one human approval.") < body.index(
        "<details><summary>Raw error</summary>"
    )
    assert "rounds=3; mergeable=CONFLICTING" in body


def test_terminal_failure_parts_omit_empty_action_and_show_disposition():
    body = progress._finish_outcome_body(
        outcome="blocked", action="", reason="No human action is required.",
        diagnosis="engine retry state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What you need to do:" not in body
    assert "waiting on a human decision" in body
    assert "engine retry state" in body


def test_terminal_failure_parts_render_missing_reason_explicitly():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Inspect the PR.", reason="",
        diagnosis="raw state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What happened: No reason was provided." in body


def test_finish_progress_body_uses_fixed_sections_for_blocked_error(caplog):
    body = progress._finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None,
        pr_url="https://github.com/owner/repo/pull/46", review_round=0,
        priority="normal", detail="Command '['gh']' failed: raw stderr",
        next_step="", outcome="blocked", source_repo="owner/repo", language_code="en",
    )

    assert "What happened: Orbi could not complete the delivery" in body
    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next:" in body
    assert "https://github.com/owner/repo/pull/46" in body
    assert "<details><summary>Raw error</summary>" in body
    assert "Command '['gh']' failed: raw stderr" in body
    assert "next step: " not in body
    assert "progress_finish_missing_next_step" in caplog.text
    assert "call_point=_finish_progress_body" in caplog.text


def test_finish_progress_body_keeps_next_step_in_user_section():
    body = progress._finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="relabel the Issue ai-ready", outcome="blocked",
        source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: relabel the Issue ai-ready" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body


def test_finish_progress_body_defaults_orbi_action_when_fix_step_missing(caplog):
    body = progress._finish_progress_body(
        number=39, title="Fix task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="", outcome="fix needed", source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body
    assert "progress_finish_missing_next_step" in caplog.text


def test_finish_progress_blocked_is_a_noop_without_run_id(monkeypatch):
    """Without a bound run id there is no tracked comment to update
    (the failure comment simply carries no marker)."""
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )
    runner._finish_progress(
        39, None, "owner/repo", None, None, "https://x/pull/46",
        "failure", "next step", title="Blocked task", outcome="blocked", language_code="en",
    )
    assert calls == []


def test_finish_progress_blocked_creates_the_comment_when_missing(
    monkeypatch,
):
    """A run that never reached a progress comment (the runner died
    before `ensure`) still ends with the blocked scene: the comment is
    created with it."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        # Only the progress comment POST reaches the api here.
        assert command[command.index("--method") + 1] == "POST"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in body
    assert "the failure" in body
    assert "the next step" in body
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    # Issue #100: the blocked scene shows the number AND the title.
    assert "- issue: #39 Blocked task" in body


def test_finish_progress_carries_the_actual_role_and_round(
    monkeypatch,
):
    """The terminal scene must show the role and the review/fix round
    the run was actually in, not the hardcoded `fix`/`0` (review round
    2, PR #42)."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        assert command[command.index("--method") + 1] == "POST"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", role=runner.ROLE_REVIEW, review_round=2, language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 2" not in body


def test_finish_progress_defaults_to_review_round_zero(monkeypatch):
    """Without explicit role/round the default is the only post-PR role
    (Issue #82: the review session fixes findings in the same session,
    so a blocked delivery is always a review failure)."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        outcome="blocked", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 0" not in body


def test_finish_progress_renders_the_fix_needed_scene(monkeypatch):
    """Issue #293: one function serves both terminal scenes — the
    fix-needed scene (Issue #50) renders the same tracked comment with
    the fix-needed outcome and its next step."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._finish_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure",
        "the next tick resumes the same run, branch, worktree and PR "
        "automatically (the Issue stays ai-fix-needed)",
        title="Fix task", outcome="fix needed", language_code="en",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "Orbi fix needed" in body
    assert "What happened: Orbi found a problem" in body
    assert "What you need to do: Nothing" in body
    assert ("What Orbi will do next: the next tick resumes the same run, "
            "branch, worktree and PR automatically (the Issue stays "
            "ai-fix-needed)") in body
    assert "<details><summary>Raw error</summary>\nthe failure\n</details>" in body
    # The role default is the only post-PR role (Issue #82).
    assert "- role: review" in body
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "- issue: #39 Fix task" in body


def test_delivery_step_returns_when_pr_merged(monkeypatch, caplog):
    seen, _ = fake_pr_view(monkeypatch, "MERGED")
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    assert len(seen) == 1
    assert "delivery_merged" in caplog.text
    assert f"pr={PR_URL}" in caplog.text


def test_delivery_step_runs_one_review_per_tick(
        monkeypatch, tmp_path,
):
    """Issue #788: one tick, ONE review round, ONE PR state read. The old
    wait loop re-polled and re-reviewed in-process until merge; the step
    returns after the round and the next tick's resume scan continues."""
    calls = {"pr": 0, "labels": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            calls["pr"] += 1
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            calls["labels"] += 1
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # Awaiting review triggers the independent review (Issue #34); the
    # mock reports findings so the next tick runs the next round.
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # Exactly ONE PR state read and ONE review round — the step returns
    # afterwards (no loop, no second poll, no sleep).
    assert calls == {"pr": 1, "labels": 1}
    assert len(reviews) == 1
    # The review ran on the derived worktree/branch of the same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_delivery_step_defers_when_ci_pending(
        monkeypatch, tmp_path, caplog,
):
    """Issue #788: pending CI on the PR head defers the whole delivery to
    the next tick — one journal line, NO review session, NO label read,
    NO label change, no sleep. "pending" is a state, not a wait."""
    calls = {"pr": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            calls["pr"] += 1
            return json.dumps({
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "tests", "status": "IN_PROGRESS",
                     "conclusion": None},
                ],
            })
        # The defer path performs exactly ONE PR read: no Issue read
        # (comments or labels), no mutation — the fake rejects the rest.
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # One PR read, one journal line, then return: no review, no label
    # read, no mutation.
    assert calls == {"pr": 1}
    assert reviews == []
    assert edits == []
    assert "delivery_ci_pending" in caplog.text


def test_delivery_step_auto_merges_on_clean_review(
        monkeypatch, caplog, tmp_path,
):
    """A clean independent verdict merges the PR itself (Issue #34): the
    wait returns as soon as the review reports the merge, without any
    further polling."""
    states = ["OPEN"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            pr_calls["n"] += 1
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: True,
    )
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # One OPEN poll, one review, then the merge is terminal: no second
    # PR state poll.
    assert pr_calls["n"] == 1
    assert "delivery_auto_merged" in caplog.text


def test_delivery_step_passes_p0_priority_to_the_review(
        monkeypatch, caplog, tmp_path,
):
    """Issue #101: a P0 delivery in an opened-PR state (the resumable
    scan fetches `labels`) keeps its priority end to end: the awaiting
    log line carries `priority=p0` and the independent review receives
    it, so the review/merge progress comment shows `p0` too."""
    states = ["OPEN"]
    review_calls = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [
                {"name": "ai-awaiting-merge"}, {"name": "p0"},
            ]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(seam, "run_command", fake_run)

    def fake_review(*args, **kwargs):
        review_calls.append(kwargs)
        return True

    monkeypatch.setattr(review_merge, "review_and_merge_if_clean", fake_review)
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {
        "number": 39, "title": "p0 task", "body": "",
        "labels": [{"name": "ai-awaiting-merge"}, {"name": "p0"}],
    }
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # The review receives the priority, the recovered scene (the round
    # budget reads the scene, Issue #788) and the PR head's check rollup
    # (Issue #1613: the tests field comes from CI, never a local log).
    assert review_calls == [{
        "title": "p0 task", "priority": "p0",
        "scene": {
            "run_id": "a1b2c3d4", "base_branch": "main",
            "base_sha": "abc123def456", "pr_url": PR_URL,
            "external": "", "review_round": 0, "scene_at": None,
        },
        "checks": [],
        "merge_only": True,
    }]
    # The awaiting log line carries the explicit priority field.
    awaiting = [m for m in caplog.messages if "delivery_awaiting" in m]
    assert any("priority=p0" in m for m in awaiting)


def test_delivery_step_marks_blocked_when_review_fails(
        monkeypatch, caplog, tmp_path,
):
    """A review that cannot run (unrecoverable scene, missing worktree,
    malformed verdict, exhausted rounds) is a real failure: the Issue is
    marked ai-blocked with a failure comment, and the wait returns so
    the slot is released (the Issue is never stranded awaiting review).
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # No trusted `Orbi opened PR:` comment: the scene
            # cannot be recovered. The trusted review-round comments
            # still count for the blocked scene's round field (review
            # round 2, PR #42).
            return json.dumps({"comments": [
                {"body": "public comment", "authorAssociation": "NONE"},
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        return json.dumps({"labels": [{"name": "ai-pr-opened"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("the independent review of" in body for body in posted_bodies),
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "the independent review of" in blocked
    assert "What Orbi will do next:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (the failure happened
    # during the independent review) and the completed review rounds
    # (review round 2, PR #42) — not the hardcoded fix/0.
    assert "- role: review" in blocked
    assert "- review/fix round: 2" not in blocked
    # Issue #1724: a review blocked on the missing trusted scene names
    # ONE working resume path — restore the scene comment, then the
    # shared unblock step — never the "or relabel the Issue" alternative
    # that does not work on its own.
    assert "must restore the trusted 'Orbi opened PR' scene comment" in body
    assert "remove `ai-blocked`, then add `ai-fix-needed`" in body
    assert "or relabel" not in body


def test_delivery_step_marks_blocked_when_review_fails_while_fix_needed(
        monkeypatch, caplog, tmp_path,
):
    """A review failure while the Issue is `ai-fix-needed` (awaiting the
    next review session) must leave the terminal state `ai-blocked`
    ALONE: this PR routes both opened-PR states into the same review, so
    the leftover `ai-fix-needed` label is removed too (Issue #82).
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # No trusted `Orbi opened PR:` comment: the scene
            # cannot be recovered, so the review fails fast.
            return json.dumps({"comments": [
                {"body": "public comment", "authorAssociation": "NONE"},
            ]})
        # The delivery is in the `ai-fix-needed` state (awaiting the
        # next review session).
        return json.dumps({"labels": [{"name": "ai-fix-needed"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # The Issue is marked ai-blocked; the blocked patch clears every
    # delivery-state label that is present — here only `ai-fix-needed`
    # (the delivery was awaiting the next review session) — so the
    # terminal state is ai-blocked alone (never ai-blocked + ai-fix-needed).
    # One deterministic patch, not two hardcoded removes.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-fix-needed",
    }
    assert len(edits) == 1
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone AND the
    # tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("the independent review of" in body for body in posted_bodies),
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "the independent review of" in blocked
    assert "- role: review" in blocked


def test_delivery_step_blocks_when_scene_base_differs_from_config(
        monkeypatch, caplog, tmp_path,
):
    """Issue #91: the scene freezes the base the PR was opened against.
    When the configured base differs (config changed, or the comment is
    stale), the resume must fail fast BEFORE any git/Pi mutation: no
    review is started, nothing is merged, and the Issue is marked
    ai-blocked with BOTH base values named — never silently switch to
    the configured base for a PR frozen on another one.
    """
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    states = ["OPEN", "MERGED"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            pr_calls["n"] += 1
            return json.dumps({"state": states[pr_calls["n"] - 1]})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # The trusted scene freezes base_branch=develop.
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=develop "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        return json.dumps({"labels": [{"name": "ai-pr-opened"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the base mismatch is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    # The configured base is main; the scene froze develop.
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started and nothing was merged.
    assert reviews == []
    # The mismatch is terminal on the FIRST poll: the wait returns
    # instead of re-checking the PR state.
    assert pr_calls["n"] == 1
    # The Issue is marked ai-blocked (removing ai-pr-opened) ...
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    # ... with a failure comment that names BOTH base values and carries
    # the run marker.
    body = comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "base_branch=develop" in body
    assert "base_branch=main" in body
    assert "delivery_review_failed" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any("- result:" in body for body in posted_bodies)
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "base_branch=develop" in blocked
    assert "base_branch=main" in blocked
    assert "What Orbi will do next:" in blocked


def test_delivery_step_worktree_missing_stays_fix_needed(
        monkeypatch, caplog, tmp_path,
):
    """Issue #90 + #50: the resume derives the worktree from the repo,
    Issue and run id (never from a comment). When that directory does
    not exist the delivery must fail fast BEFORE any git/Pi mutation:
    no review is started, no command is spawned against the missing
    scene — but the failure is RECOVERABLE (the branch still exists on
    the remote and the worktree can be recreated on the next resume):
    the Issue is marked ai-fix-needed (never ai-blocked) with the PR
    and branch preserved (the pre-#82 `resume_delivery` fail-fast,
    restored)."""
    api_calls: list = []
    pr_comments: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        # The fake rejects anything that is not a pr/issue view, the
        # PR failure comment or the progress API: a missing worktree
        # must not spawn git/gh.
        if command[:2] == ["gh", "pr"]:
            if command[2] == "comment":
                pr_comments.append(command[-1])
                return ""
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                # The run's live progress comment exists.
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            # The trusted scene derives run_id=a1b2c3d4; the derived
            # worktree does not exist under tmp_path.
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
    # The fake rejects anything that is not a pr/issue view or the
    # progress API.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "fetch"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the missing worktree is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started and nothing was merged.
    assert reviews == []
    # The Issue is marked ai-fix-needed (removing ai-pr-opened) —
    # never ai-blocked (Issue #50) ...
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
        "remove": "ai-pr-opened",
    }
    # ... and it is the only label edit.
    assert len(edits) == 1
    # The failure comment names the missing worktree and carries the
    # run marker.
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body
    # The full scene carries the ACTUAL branch (derived before the
    # worktree check, Issue #50) — never a `branch=None` placeholder.
    assert "branch=orbi/owner-repo-issue-39" in body
    assert "branch=None" not in body
    assert "delivery_review_failed" in caplog.text
    # The failure comment is written to the Issue AND the PR
    # (Issue #50); the PR copy is the same formatted comment and
    # additionally carries the hidden runner fingerprint (Issue #526).
    assert pr_comments[0].startswith("<!-- orbi:run=a1b2c3d4 -->")
    assert pr_comments[0].endswith("<!-- runner=8a12fb1c -->")
    # The recoverable failure posts the fix-needed milestone (Issue
    # #18) AND the tracked progress comment becomes the fix-needed
    # scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted_bodies)
    # The milestone names the failing scene through the same bounded
    # summary as the failure comment — the missing worktree is still
    # named (a local runner path is redacted where the platform naming
    # allows it) — and it carries the machine-readable record too
    # (Issue #1322: the milestone is a failure comment).
    assert any(
        "worktree missing:" in body and
        "<!-- orbi:failure:v1 " in body
        for body in posted_bodies
    )
    assert not any(
        "Orbi: blocked" in body for body in posted_bodies
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the fix-needed scene in place.
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    fix_needed = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi fix needed" in fix_needed
    # Issue #1721: the "What happened" sentence keeps the failure, but the
    # worktree path on the runner's disk is redacted; the branch (the
    # public name) still says which worktree is missing. pytest's tmp_path
    # sits under a covered prefix on Ubuntu CI (/tmp) but not on hosted
    # macOS (/private/var — the gap Issue #1721 records as out of scope),
    # so the expectation is derived with the same helper instead of
    # hard-coding the redacted form.
    missing_worktree = gitops.worktree_path(tmp_path, "owner/repo", 39, "a1b2c3d4")
    assert progress._redact_local_paths(
        f"worktree missing: {missing_worktree}",
    ) in fix_needed
    assert "branch: orbi/owner-repo-issue-39" in fix_needed
    assert "What Orbi will do next:" in fix_needed


def test_delivery_step_worktree_missing_while_fix_needed_keeps_label(
        monkeypatch, tmp_path,
):
    """Issue #90 + #82 + #50: a missing resume worktree while the
    Issue is `ai-fix-needed` (awaiting the next review session) keeps
    the `ai-fix-needed` label (the opened-PR state label is removed,
    the fix-needed label is the one the next tick scans for) and no
    review is started."""
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            if command[2] == "comment":
                return ""
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        # The delivery is in the `ai-fix-needed` state (awaiting the
        # next review session).
        return json.dumps({"labels": [{"name": "ai-fix-needed"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    # No review was started.
    assert reviews == []
    # The Issue already carries only ai-fix-needed, so no absent label is
    # removed; the state remains in the automatic fix loop.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
    }
    assert len(edits) == 1
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body


def test_delivery_step_runs_review_when_fix_needed(
    monkeypatch, caplog, tmp_path,
):
    """A fix-needed delivery runs the SAME independent review as an
    awaiting-review delivery (Issue #82: the review session fixes
    findings in the same session — no cold-start fixer). Issue #788: the
    step runs the round ONCE and returns; the next tick resumes."""

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "labels":
                return json.dumps({
                    "labels": [{"name": "ai-fix-needed"}],
                })
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # The independent review runs for the fix-needed state too (Issue
    # #82) and reports findings; the next tick runs the next round.
    reviews = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": "stale body"}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    config = config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")
    runner.delivery_step(PR_URL, issue, config, "owner/repo")
    # ONE review round per tick (Issue #788): the step returns after it.
    assert len(reviews) == 1
    # No fixer: the review ran on the derived worktree/branch of the
    # same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39


def test_issue_labels_returns_names_and_fails_fast(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "issue", "view"]
        assert command[-2:] == ["--json", "labels"]
        # Corrupted entries (non-dict, missing name) are skipped; only
        # well-formed names are returned.
        return json.dumps({"labels": [
            {"name": "ai-ready"}, "garbage",
            {"name": "ai-fix-needed"}, {"other": 1},
        ]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.issue_labels(39, "owner/repo") == [
        "ai-ready", "ai-fix-needed",
    ]

    def bad_run(command, **kwargs):
        return json.dumps({"labels": "nope"})

    monkeypatch.setattr(seam, "run_command", bad_run)
    with pytest.raises(ValueError, match="issue labels must be a JSON array"):
        runner.issue_labels(39, "owner/repo")

    def bad_run2(command, **kwargs):
        return json.dumps(["ai-ready"])

    monkeypatch.setattr(seam, "run_command", bad_run2)
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_labels(39, "owner/repo")


def test_delivery_step_marks_blocked_when_pr_closed_unmerged(
    monkeypatch, caplog,
):
    api_calls: list = []
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"]:
            # The blocked scene derives the role from the delivery label
            # and the round from the trusted review-round comments
            # (review round 2, PR #42): no more hardcoded fix/0.
            if command[-1] == "labels":
                return json.dumps({"labels": [{"name": "ai-fix-needed"}]})
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        api_calls.append(command)
        if "--method" not in command:
            # The run's live progress comment exists.
            return json.dumps([existing])
        method = command[command.index("--method") + 1]
        if method == "POST":
            body = command[command.index("--field") + 1]
            return json.dumps({"id": 78, "body": body[len("body="):],
                               "url": "https://x/78"})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    # The Issue is marked ai-blocked; the blocked patch clears every
    # delivery-state label that is present — here only `ai-fix-needed`
    # (the delivery was awaiting the next review session) — so the
    # terminal state is ai-blocked alone. One deterministic patch,
    # not two hardcoded removes.
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-fix-needed",
    }
    assert len(edits) == 1
    # ... with a failure comment carrying the run marker and run_id and,
    # from the ONE blocked exit, the community help line last (Issue #1730).
    body = comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
    help_line = language.text("en", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1
    assert f"pr={PR_URL}" in caplog.text
    assert "delivery_closed_unmerged" in caplog.text
    # The terminal failure also posts the blocked milestone (Issue #18)
    # AND the tracked progress comment becomes the blocked scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("closed without a merge" in body for body in posted_bodies),
    )
    # No second progress comment: the tracked one (id 77) is PATCHed
    # into the blocked scene in place.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "closed without a merge" in blocked
    assert "What Orbi will do next:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (Issue #82: both
    # opened-PR states are review states, so always `review`) and the
    # completed review rounds (review round 2, PR #42).
    assert "- role: review" in blocked
    assert "- review/fix round: 2" not in blocked


def test_delivery_step_pr_closed_comment_follows_the_language(monkeypatch):
    """Issue #1730: the PR-closed blocked comment ends with the community
    help line in the run's bound language — the zh scene goes through the
    same blocked exit as the en one."""
    def fake_run(command, **kwargs):
        # Only the two reads this scene makes; a third command fails fast
        # with a KeyError instead of being answered by a catch-all arm.
        answers = {
            ("gh", "pr", "view"): {"state": "CLOSED"},
            ("gh", "issue", "view"): {"labels": [{"name": "ai-fix-needed"}]},
        }
        return json.dumps(answers[tuple(command[:3])])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "_CURRENT_RUN_ID", None)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, **kwargs: comments.append(kwargs))
    monkeypatch.setattr(seam, "apply_label_patch",
        lambda number, **kwargs: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(message_language="zh"),
        "owner/repo",
    )
    body = comments[0]["body"]
    help_line = language.text("zh", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1
    assert language.text(
        "zh", "runner.pr_closed_comment", pr_url=PR_URL,
    ) in body


def test_delivery_step_review_failure_without_bound_run_id(
        monkeypatch, tmp_path,
):
    """When no run id is bound the review-failure comment simply carries
    no marker (the Issue is still marked ai-blocked)."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": []})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    )
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "the independent review of" in body
    assert "orbi:run=" not in body


def test_delivery_step_repairs_in_progress_label_and_logs_ci(
        monkeypatch, caplog, tmp_path,
):
    """An open PR proves implementation reached delivery: repair a lost
    label transition and expose CI state before entering review."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "tests", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                    {"name": "lint", "status": "COMPLETED",
                     "conclusion": "SKIPPED"},
                ],
            })
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [{
                    "body": (
                        "<!-- orbi:run=a1b2c3d4 -->\n"
                        "Orbi opened PR: "
                        f"{PR_URL} (base_branch=main "
                        "base_sha=abc123def456 run_id=a1b2c3d4)"
                    ),
                    "authorAssociation": "OWNER",
                }]})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    monkeypatch.setattr(review_merge, "review_and_merge_if_clean", lambda *a, **k: True)
    (tmp_path / ".worktrees" /
     "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert any("--add-label" in call and "ai-pr-opened" in call
               and "--remove-label" in call and "ai-in-progress" in call
               for call in calls)
    assert "tests=COMPLETED/SUCCESS" in caplog.text
    assert "lint=COMPLETED/SKIPPED" in caplog.text


def test_delivery_step_blocks_when_in_progress_label_repair_fails(
        monkeypatch, caplog,
):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        return json.dumps({"labels": [{"name": "ai-in-progress"}]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    patches = []
    def fake_patch(number, **kwargs):
        patches.append(kwargs)
        if kwargs["event"] == runner.EVENT_PR_OPENED:
            raise RuntimeError("label API unavailable")
    monkeypatch.setattr(seam, "apply_label_patch", fake_patch)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append(kwargs))
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(), "owner/repo",
    )
    assert patches[-1]["event"] == EVENT_BLOCKED
    assert "delivery_label_repair_failed" in caplog.text
    # The blocked comment ends with the community help line (Issue #1730).
    help_line = language.text("en", "help.community")
    assert comments[0]["body"].endswith(f"\n\n{help_line}")
    assert comments[0]["body"].count(help_line) == 1


def test_delivery_step_keeps_holding_when_no_delivery_label(
        monkeypatch, caplog,
):
    """An OPEN PR whose Issue carries no delivery state label (neither
    awaiting review nor fix-needed) is simply re-checked: the slot stays
    held and no review or fix is started."""
    states = ["OPEN", "MERGED"]
    pr_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            pr_calls["n"] += 1
            return json.dumps({"state": states[pr_calls["n"] - 1]})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "edit"]:
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    issue = {"number": 39, "title": "task", "body": ""}
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, issue, config_domain.RunnerConfig(), "owner/repo")
    # An open PR with no resumable delivery label is unrecoverable: it is
    # blocked immediately instead of retaining the slot indefinitely.
    assert pr_calls["n"] == 1
    assert "delivery_label_inconsistent" in caplog.text


def test_delivery_step_label_repair_failure_comment_follows_the_language(
        monkeypatch, caplog,
):
    """Issue #1660: the terminal "labels could not be repaired" comment is
    a Runner-owned failure comment, so its prose follows the bound
    language while the label, the PR URL and the machine fields stay
    verbatim."""
    def fake_run(command, **kwargs):
        # Dispatch on the joined argv: the same fake answers both reads
        # and the writes, so no command-shape assertion is added.
        shape = " ".join(str(part) for part in command)
        if shape.startswith("gh pr view "):
            return json.dumps({"state": "OPEN", "statusCheckRollup": []})
        if shape.startswith("gh issue view "):
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append(kwargs),
    )
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=Path("/r"), base_branch="main",
            message_language="zh",
        ),
        "owner/repo",
    )
    assert "delivery_label_inconsistent" in caplog.text
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "Orbi 失败：PR" in body
    assert "ai-blocked" in body
    assert PR_URL in body
    # The blocked comment ends with the language's help line (#1730).
    help_line = language.text("zh", "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1


def test_delivery_step_logs_awaiting_without_bound_run_id(monkeypatch, caplog):
    """When no run id is bound the wait still works: the failure comment
    simply carries no marker."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"] and command[-1] == "labels":
            # No leftover fix-needed label: only ai-pr-opened is removed.
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, {"number": 39, "title": "t", "body": ""},
        config_domain.RunnerConfig(), "owner/repo",
    )
    assert "Orbi failed:" in comments[0][1]["body"]
    assert "orbi:run=" not in comments[0][1]["body"]


# ---------------------------------------------------- Issue #289
# `_run_review_round` is the per-round delivery flow extracted from
# `delivery_step` (one round = label read/repair + resumable gate
# + one independent review + failure classification). These tests pin
# the three-value contract the polling skeleton dispatches on:
# True = merged this round, False = findings (next round), None = a
# terminal state was already handled (slot released).


def _review_round_env(
    monkeypatch, tmp_path, *, scene_base="main", label="ai-pr-opened",
    with_worktree=True, review_result=False, scene_round=0,
    external="", record=None, review_raises=None,
):
    """Shared fake GitHub/git scene for one direct `_run_review_round`
    call. Returns (edits, comments, reviews, publishes) capture lists;
    `edits` carries the `edit_issue` label patches, `publishes` the
    `_safe_publish` progress calls."""
    def fake_run(command, **kwargs):
        if command[-1] == "comments":
            # The trusted scene comment.
            return json.dumps({"comments": [{
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n"
                    + ("<!-- orbi:scene:v1 " + json.dumps({
                        "base_branch": scene_base,
                        "base_sha": "abc123def456",
                        "external": external, "pr_url": PR_URL,
                        "review_round": scene_round,
                        "run_id": "a1b2c3d4", "schema": 1,
                    }) + " -->\n" if scene_round else "")
                    + "Orbi opened PR: "
                    f"{PR_URL} (base_branch={scene_base} "
                    "base_sha=abc123def456 run_id=a1b2c3d4)"
                ),
                "authorAssociation": "OWNER",
            }]})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": label}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything it does not implement.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    if with_worktree:
        worktree = (tmp_path / ".worktrees"
                    / "orbi-owner-repo-issue-39-a1b2c3d4")
        worktree.mkdir(parents=True)
        if record is not None:
            (worktree / ".orbi").mkdir(parents=True, exist_ok=True)
            (worktree / ".orbi" / "run-state.json").write_text(
                json.dumps({
                    "run_id": "a1b2c3d4", "issue": 39, "repo": "owner/repo",
                    "branch": "orbi/owner-repo-issue-39",
                    "worktree": str(worktree),
                    "reviewed_head": record[0],
                    "reviewed_issue_body_sha256": record[1],
                }),
                encoding="utf-8",
            )
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    reviews: list = []

    def fake_review(*args, **kwargs):
        reviews.append((args, kwargs))
        if review_raises is not None:
            raise review_raises
        return review_result

    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean", fake_review,
    )
    publishes: list = []
    monkeypatch.setattr(seam, "_safe_publish",
        lambda **kwargs: publishes.append(kwargs),
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    return edits, comments, reviews, publishes


def test_run_review_round_returns_true_when_review_merges(
        monkeypatch, tmp_path,
):
    """Issue #289: True = the round merged the PR; the caller releases
    the slot. No label edit and no failure comment on this path."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, review_result=True,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is True
    # One review on the frozen scene of the same run.
    assert len(reviews) == 1
    worktree, branch, base_branch, review_config, repo, number = \
        reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39"
    assert base_branch == "main"
    assert review_config.run_id == "a1b2c3d4"
    assert review_config.base_sha == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39
    assert edits == []
    assert comments == []


def test_run_review_round_returns_false_on_findings(
        monkeypatch, tmp_path,
):
    """Issue #289: False = findings remain (the ai-fix-needed
    transition happened inside the review itself); the caller yields
    the cadence and polls the next round."""
    edits, comments, reviews, _ = _review_round_env(monkeypatch, tmp_path)
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is False
    assert len(reviews) == 1
    assert edits == []
    assert comments == []


def test_run_review_round_passes_prior_comments_after_first_round(
        monkeypatch, tmp_path,
):
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_round=1,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    ) is False
    assert "previous_comments" in reviews[0][1]
    assert reviews[0][1]["previous_comments"]


def test_run_review_round_passes_a_recorded_pass(monkeypatch, tmp_path):
    """Issue #1626: a recorded pass with the same rendered Issue body is
    passed to the round, which merges without a session."""
    prompt = tmp_path / "prompt_review.md"
    prompt.write_text("Review.\n{{ISSUE_BODY}}\n", encoding="utf-8")
    body = "the issue body"
    body_sha = pi_session.issue_body_sha256(body)
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, record=("h1", body_sha),
    )
    issue = {"number": 39, "title": "task", "body": body}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", prompt_review=prompt,
        ),
        "owner/repo",
    )
    assert outcome is False
    assert reviews[0][1]["review_record"] == ("h1", body_sha)
    assert reviews[0][1]["issue_body_sha256"] == body_sha


def test_run_review_round_skips_the_record_for_an_external_takeover(
        monkeypatch, tmp_path):
    """Issue #1626: an external takeover is never merge-only, so the
    record is not passed."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_round=1, external="true",
        record=("h1", "b" * 64),
    )
    issue = {"number": 39, "title": "task", "body": "body"}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is False
    assert "review_record" not in reviews[0][1]


def test_run_review_round_marks_awaiting_merge_as_merge_only(
        monkeypatch, tmp_path):
    """Issue #1216/#1626: the ai-awaiting-merge retry stays merge_only."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, label="ai-awaiting-merge",
    )
    issue = {"number": 39, "title": "task", "body": "body"}
    assert review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo",
    ) is False
    assert reviews[0][1]["merge_only"] is True


def test_run_review_round_returns_none_when_scene_base_differs(
        monkeypatch, tmp_path,
):
    """Issue #289: None = terminal state handled. An unrecoverable
    precondition (the scene froze another base, Issue #91) marks the
    Issue ai-blocked with the failure comment; the review never runs
    and the caller releases the slot."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_base="develop",
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-blocked",
        "remove": "ai-pr-opened",
    }
    assert "Orbi failed:" in comments[0][1]["body"]
    assert "base_branch=develop" in comments[0][1]["body"]
    assert "base_branch=main" in comments[0][1]["body"]


def test_run_review_round_returns_none_when_worktree_missing(
        monkeypatch, tmp_path,
):
    """Issue #289: None also covers the RECOVERABLE failure (the
    missing worktree, Issue #90): the round writes the ai-fix-needed
    full scene (the next tick resumes) and the caller releases the
    slot — the failure never keeps re-running inside one process."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, with_worktree=False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
        "remove": "ai-pr-opened",
    }
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "branch: `orbi/owner-repo-issue-39`" in body


def test_run_review_round_failure_comment_follows_the_config_language(
        monkeypatch, tmp_path,
):
    """Issue #1687 acceptance 1: a delivery that fails in review posts its
    blocked comment in the bound message language. The review path used to
    call the failure reporter without a language, so a zh host still got an
    English blocked comment on its most common failure."""
    edits, comments, reviews, _ = _review_round_env(
        monkeypatch, tmp_path, scene_base="develop",
    )
    issue = {"number": 39, "title": "task", "body": ""}
    outcome = review_merge._run_review_round(
        PR_URL, issue,
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language="zh",
        ),
        "owner/repo",
    )
    assert outcome is None
    assert reviews == []
    assert edits[0][1]["add"] == "ai-blocked"
    body = comments[0][1]["body"]
    assert "Orbi：已阻塞 —— 等待人工决定" in body
    assert "**原因：**" in body
    assert "Orbi failed:" not in body


# The blocked-review comment body in both languages (Issue #1701): the
# Action line must say how to resume (remove `ai-blocked` BEFORE adding
# `ai-fix-needed` — the claim scans skip `ai-blocked` and the budget reset
# needs it removed first), and the Reason line is one full catalog sentence.
BLOCKED_REVIEW_TEXT = {
    "en": {
        "action": "**Action:**",
        "reason": "**Reason:**",
        "rounds_action": "fresh review-round budget",
        "rounds_reason": "used every one of its review rounds",
        "human_reason": "requires a human decision",
        "generic_action": "changing labels alone does not change it",
        "step": "remove `ai-blocked`, then add `ai-fix-needed`",
        "close": "close the PR and this Issue",
        "budget": "budget",
    },
    "zh": {
        "action": "**需要您做的操作：**",
        "reason": "**原因：**",
        "rounds_action": "评审预算",
        "rounds_reason": "评审轮次",
        "human_reason": "需要人工决定",
        "generic_action": "只改标签不会改变它",
        "step": "移除 `ai-blocked`，再添加 `ai-fix-needed`",
        "close": "关闭该 PR 和这个 Issue",
        "budget": "预算",
    },
}


def _blocked_line(body: str, label: str) -> str:
    """The one comment line starting with a rendered field label."""
    return next(line for line in body.splitlines() if line.startswith(label))


def _blocked_review_body(monkeypatch, tmp_path, code, **env):
    """The Issue comment one blocking review failure posts in that language."""
    edits, comments, _, _ = _review_round_env(monkeypatch, tmp_path, **env)
    outcome = review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language=code,
        ),
        "owner/repo",
    )
    assert outcome is None
    assert edits[0][1]["add"] == "ai-blocked"
    return comments[0][1]["body"]


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_rounds_exhausted_says_how_to_resume(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 1: the exhausted round budget names the PR,
    says the review used all its rounds without a mergeable result (no raw
    exception text), and tells the human to remove `ai-blocked` before
    adding `ai-fix-needed`, which buys a fresh review-round budget."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.ReviewRoundsExhausted("raw budget line"),
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    assert action.index("`ai-blocked`") < action.index("`ai-fix-needed`")
    assert text["rounds_action"] in action
    assert text["rounds_reason"] in reason
    assert PR_URL in reason
    assert "raw budget line" not in reason


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_human_decision_keeps_the_reviewer_fix(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 2: with a reviewer fix the action is the
    reviewer's own text plus the resume step and no budget clause — the
    budget resets only after all rounds are used."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.HumanDecisionRequired(
            "review requires human decision: note: same failure twice; "
            "fix: choose the authoritative address source",
            action="Choose the authoritative address source.",
        ),
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    # The reviewer's own text is kept verbatim; only its trailing
    # punctuation is dropped so the step sentence does not double it.
    assert "Choose the authoritative address source" in action
    assert "source.。" not in action
    assert text["step"] in action
    assert text["budget"] not in action
    assert text["human_reason"] in reason
    assert PR_URL in reason
    assert "same failure twice" not in reason


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_human_decision_without_fix_uses_generic_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 2: the reviewer gave no fix — the handler
    supplies the generic catalog action instead of an English literal."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code,
        review_raises=failure_report.HumanDecisionRequired(
            "review requires human decision: note: decide the base",
            action="",
        ),
    )
    action = _blocked_line(body, text["action"])
    assert text["generic_action"] in action
    assert text["step"] in action
    assert text["close"] in action
    assert "Decide how this PR should proceed." not in body


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_base_mismatch_gets_the_generic_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 3: a base mismatch has no case-specific
    remedy (the base check reads the scene comment, not the PR) — it gets
    the generic action in the language while the reason still names both
    base values."""
    text = BLOCKED_REVIEW_TEXT[code]
    body = _blocked_review_body(
        monkeypatch, tmp_path, code, scene_base="develop",
    )
    action = _blocked_line(body, text["action"])
    reason = _blocked_line(body, text["reason"])
    assert text["generic_action"] in action
    assert text["close"] in action
    assert "base_branch=develop" in reason
    assert "base_branch=main" in reason
    assert language.text(code, "failure.headline_blocked") in body


@pytest.mark.parametrize("code", ["en", "zh"])
def test_run_review_round_recoverable_failure_keeps_no_action(
        monkeypatch, tmp_path, code,
):
    """Issue #1701 acceptance 4: a recoverable review failure stays
    ai-fix-needed with an empty action — the next tick resumes it."""
    text = BLOCKED_REVIEW_TEXT[code]
    edits, comments, _, _ = _review_round_env(
        monkeypatch, tmp_path, with_worktree=False,
    )
    outcome = review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language=code,
        ),
        "owner/repo",
    )
    assert outcome is None
    assert edits[0][1]["add"] == "ai-fix-needed"
    body = comments[0][1]["body"]
    assert text["action"] not in body
    assert text["reason"] in body
    assert "worktree missing" in body


def test_main_releases_slot_after_opening_the_pr(monkeypatch, tmp_path):
    """Issue #788: a fresh claim ends its tick when the PR is open — the
    slot is released instead of being held through review -> merge. The
    review is the next tick's RESUME_REVIEW step, which takes the slot
    again for exactly the Pi session's duration. A concurrent runner can
    therefore take the slot the moment this tick ends."""
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *a, **k: runner.IssueResult("pr", PR_URL))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a fresh claim must not run the review step"),
        ),
    )

    assert runner.main(["--config", str(config)]) == 0
    # The tick ended after opening the PR: the slot is free again.
    slot_dir = tmp_path / ".orbi" / "slots"
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    assert pilot_slots.acquire_slot(slot_dir, 1, os.getpid()) is not None


# --- configurable Pi provider/model/thinking (Issue #119) -------------------


def _model_config(tmp_path, **extra):
    """Build a minimal config for run_pi/run_review with model keys."""
    config = config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="a1b2c3d4")
    return dataclasses.replace(config, **extra)


def _command_model_args(command):
    """Return the (flag, value) pairs for the pi model options."""
    flags = ("--provider", "--model", "--thinking")
    return [
        (item, command[index + 1])
        for index, item in enumerate(command)
        if item in flags
    ]


def test_load_config_defaults_pi_model_keys_to_none(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = config_domain.load_config(config_path)
    assert config.pi_provider is None
    assert config.pi_model is None
    assert config.pi_thinking is None
    assert config.review_pi_provider is None
    assert config.review_pi_model is None
    assert config.review_pi_thinking is None


def test_load_config_reads_pi_model_keys(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'pi_provider = "openai"\n'
        'pi_model = "gpt-5.6-sol"\n'
        'pi_thinking = "medium"\n'
        'review_pi_provider = "deepseek"\n'
        'review_pi_model = "deepseek-flash"\n'
        'review_pi_thinking = "high"\n',
        encoding="utf-8",
    )
    config = config_domain.load_config(config_path)
    assert config.pi_provider == "openai"
    assert config.pi_model == "gpt-5.6-sol"
    assert config.pi_thinking == "medium"
    assert config.review_pi_provider == "deepseek"
    assert config.review_pi_model == "deepseek-flash"
    assert config.review_pi_thinking == "high"


@pytest.mark.parametrize(
    "key, value",
    [
        ("pi_provider", '""'),
        ("pi_provider", "123"),
        ("pi_provider", "true"),
        ("pi_model", '""'),
        ("pi_model", "123"),
        ("pi_model", "true"),
        ("pi_thinking", '""'),
        ("pi_thinking", "123"),
        ("pi_thinking", "true"),
    ],
)
def test_load_config_rejects_invalid_pi_model_keys(tmp_path, key, value):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["owner/repo"]\n{key} = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"{key} must be a non-empty string"):
        config_domain.load_config(config_path)


def test_run_pi_passes_configured_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(
        tmp_path, pi_provider="openai", pi_model="gpt-5.6-sol",
        pi_thinking="medium",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command, kwargs = calls[0]
    assert _command_model_args(command) == [
        ("--provider", "openai"),
        ("--model", "gpt-5.6-sol"),
        ("--thinking", "medium"),
    ]
    # The journal-visible redacted command carries the same non-sensitive
    # values, so the run scene proves what was actually launched.
    assert _command_model_args(kwargs["log_command"]) == [
        ("--provider", "openai"),
        ("--model", "gpt-5.6-sol"),
        ("--thinking", "medium"),
    ]
    # The prompt and issue context stay redacted in the log command.
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_partial_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(tmp_path, pi_provider="openai")
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command, kwargs = calls[0]
    assert _command_model_args(command) == [("--provider", "openai")]
    assert "--model" not in command
    assert "--thinking" not in command
    assert _command_model_args(kwargs["log_command"]) == [("--provider", "openai")]


def test_run_pi_omits_model_args_when_not_configured(monkeypatch, tmp_path):
    """Default compatibility: without the keys the command is exactly the
    pre-#119 shape (Pi keeps its own defaults)."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(tmp_path)
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command, kwargs = calls[0]
    assert "--provider" not in command
    assert "--model" not in command
    assert "--thinking" not in command
    assert command[:4] == ["pi", "--no-extensions", "--print", "--session-dir"]
    assert kwargs["log_command"] == [
        "pi", "--no-extensions", "--print", "--session-dir", str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_run_review_passes_configured_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "ok",
    )
    config = _model_config(
        tmp_path, pi_provider="openai", pi_model="gpt-5.6-sol",
        pi_thinking="medium",
    )
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    command, kwargs = calls[0]
    assert _command_model_args(command) == [
        ("--provider", "openai"),
        ("--model", "gpt-5.6-sol"),
        ("--thinking", "medium"),
    ]
    assert _command_model_args(kwargs["log_command"]) == [
        ("--provider", "openai"),
        ("--model", "gpt-5.6-sol"),
        ("--thinking", "medium"),
    ]
    assert kwargs["log_command"][-2:] == [
        "<redacted>", "<review-context-redacted>",
    ]


def test_run_review_uses_independent_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "ok",
    )
    config = _model_config(
        tmp_path, pi_provider="cheap", pi_model="fast",
        pi_thinking="low", review_pi_provider="strong",
        review_pi_model="careful", review_pi_thinking="high",
    )
    pi_session.run_review(
        RunContext(run_id=config.run_id, issue=4, branch="branch",
                   worktree=tmp_path, source_repo="owner/repo"),
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1,
    )
    command, kwargs = calls[0]
    expected = [("--provider", "strong"), ("--model", "careful"),
                ("--thinking", "high")]
    assert _command_model_args(command) == expected
    assert _command_model_args(kwargs["log_command"]) == expected


def test_run_review_omits_model_args_when_not_configured(monkeypatch, tmp_path):
    """Default compatibility for the review session as well."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "ok",
    )
    config = _model_config(tmp_path)
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    command, kwargs = calls[0]
    assert "--provider" not in command
    assert "--model" not in command
    assert "--thinking" not in command
    assert kwargs["log_command"] == [
        "pi", "--no-extensions", "--print", "--session-dir", str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<review-context-redacted>",
    ]


# --- configurable Pi provider file (Issue #157) ----------------------------


GROQ_PROVIDERS = {
    "providers": {
        "groq": {
            "baseUrl": "https://api.groq.com/openai/v1",
            "api": "openai-completions",
            "apiKey": "$GROQ_API_KEY",
            "models": [
                {
                    "id": "qwen/qwen3.8-27b",
                    "contextWindow": 131072,
                    "maxTokens": 16384,
                }
            ],
        }
    }
}


def _providers_config(tmp_path, providers, **toml_keys):
    """Write a provider file + TOML, return the config path."""
    providers_path = tmp_path / "pi-providers.json"
    providers_path.write_text(
        json.dumps(providers), encoding="utf-8",
    )
    keys = {"pi_providers": '"pi-providers.json"', **toml_keys}
    toml = 'source_repos = ["owner/repo"]\n' + "\n".join(
        f"{key} = {value}" for key, value in keys.items()
    ) + "\n"
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(toml, encoding="utf-8")
    return config_path


def test_load_config_rejects_unknown_review_provider(tmp_path):
    providers = {"providers": {"base": {
        "baseUrl": "https://example.test", "api": "openai-completions",
        "models": [{"id": "base-model"}],
    }}}
    config_path = _providers_config(
        tmp_path, providers,
        pi_provider='"base"', pi_model='"base-model"',
        review_pi_provider='"missing"', review_pi_model='"base-model"',
    )
    with pytest.raises(ValueError, match="review provider selection invalid"):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_absent_defaults_to_none(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = config_domain.load_config(config_path)
    assert config.pi_providers is None
    assert config.pi_providers_data is None


@pytest.mark.parametrize("value", ['""', "123", "true"])
def test_load_config_pi_providers_rejects_non_string(tmp_path, value):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f'pi_providers = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pi_providers must be a non-empty string"):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_file_missing(tmp_path):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    (tmp_path / "pi-providers.json").unlink()
    with pytest.raises(FileNotFoundError):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_invalid_json(tmp_path):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    (tmp_path / "pi-providers.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        config_domain.load_config(config_path)


@pytest.mark.parametrize(
    "data",
    [
        {"nope": {}},
        {"providers": []},
        {"providers": "x"},
    ],
)
def test_load_config_pi_providers_rejects_missing_providers_object(
    tmp_path, data,
):
    config_path = _providers_config(tmp_path, data)
    with pytest.raises(
        ValueError, match="must have a 'providers' object",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_non_object_entry(tmp_path):
    config_path = _providers_config(
        tmp_path, {"providers": {"groq": "nope"}},
    )
    with pytest.raises(
        ValueError, match="provider 'groq' must be an object",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_missing_base_url(tmp_path):
    providers = {"providers": {"groq": {
        "api": "openai-completions",
        "apiKey": "local",
        "models": [{"id": "m1"}],
    }}}
    config_path = _providers_config(tmp_path, providers)
    with pytest.raises(
        ValueError, match="provider 'groq' is missing baseUrl",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_missing_api(tmp_path):
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "apiKey": "local",
        "models": [{"id": "m1"}],
    }}}
    config_path = _providers_config(tmp_path, providers)
    with pytest.raises(
        ValueError, match="provider 'groq' is missing api",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_accepts_model_level_api(tmp_path):
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "apiKey": "local",
        "models": [
            {"id": "m1", "api": "openai-completions"},
        ],
    }}}
    config_path = _providers_config(tmp_path, providers)
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == providers


def test_load_config_pi_providers_rejects_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"other"',
    )
    with pytest.raises(
        ValueError, match="pi_provider 'other' is not defined",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_unknown_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"missing/model"',
    )
    with pytest.raises(
        ValueError,
        match="pi_model 'missing/model' is not defined for provider 'groq'",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_missing_api_key_env(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"qwen/qwen3.8-27b"',
    )
    with pytest.raises(
        ValueError,
        match="API key for provider 'groq' references environment variable "
              "GROQ_API_KEY is not set",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_empty_api_key_env(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"qwen/qwen3.8-27b"',
    )
    with pytest.raises(
        ValueError,
        match="API key for provider 'groq' references environment variable "
              "GROQ_API_KEY is set but empty",
    ):
        config_domain.load_config(config_path)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("unset", "is not set"), ("empty", "is set but empty")],
)
def test_load_config_doctor_mode_keeps_provider_key_finding(
    tmp_path, monkeypatch, environment, expected,
):
    if environment == "unset":
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GROQ_API_KEY", "")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"qwen/qwen3.8-27b"',
    )
    config = config_domain.load_config(
        config_path, check_provider_api_keys=False,
    )
    finding = config.pi_provider_key_finding
    assert finding["variable"] == "GROQ_API_KEY"
    assert finding["state"] == expected
    assert "GROQ_API_KEY" in finding["error"]


def test_load_config_pi_providers_accepts_groq_example(tmp_path, monkeypatch):
    """The Issue's Groq example parses verbatim with the key present."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"qwen/qwen3.8-27b"',
        pi_thinking='"medium"',
    )
    config = config_domain.load_config(config_path)
    assert config.pi_provider == "groq"
    assert config.pi_model == "qwen/qwen3.8-27b"
    assert config.pi_providers_data == GROQ_PROVIDERS
    assert config.pi_providers.name == "pi-providers.json"


def test_load_config_pi_providers_unselected_provider_missing_key_ok(
    tmp_path, monkeypatch,
):
    """Only the selected provider's key must resolve: an unselected
    provider with a missing key stays unavailable in Pi, it never
    breaks the start."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    providers = {
        "providers": {
            "groq": GROQ_PROVIDERS["providers"]["groq"],
            "local": {
                "baseUrl": "http://127.0.0.1:18082/v1",
                "api": "openai-completions",
                "apiKey": "local",
                "models": [{"id": "Qwen3.8-27B"}],
            },
        }
    }
    config_path = _providers_config(
        tmp_path, providers,
        pi_provider='"local"', pi_model='"Qwen3.8-27B"',
    )
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == providers


# --- provider dir materialization + Pi env (Issue #157) --------------------


def _user_agent_dir(home: Path, models=None, settings=True, auth=True) -> Path:
    """Create a fake ~/.pi/agent under `home`."""
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    if models is not None:
        (agent / "models.json").write_text(
            json.dumps(models), encoding="utf-8",
        )
    if settings:
        (agent / "settings.json").write_text("{}", encoding="utf-8")
    if auth:
        (agent / "auth.json").write_text("{}", encoding="utf-8")
    return agent


def test_prepare_pi_agent_dir_none_when_not_configured(tmp_path):
    config = _model_config(tmp_path)
    assert config.pi_providers_data is None
    assert pi_session.prepare_pi_agent_dir(tmp_path, config) is None
    assert not (tmp_path / ".orbi" / "pi-agent").exists()


def test_prepare_pi_agent_dir_materializes_merged_models_json(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    user_agent = _user_agent_dir(
        home,
        models={"providers": {"userprov": {
            "baseUrl": "http://user:1/v1",
            "api": "openai-completions",
            "apiKey": "u",
            "models": [{"id": "um"}],
        }}},
        auth=False,
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    assert agent_dir == tmp_path / ".orbi" / "pi-agent"
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    # The user's providers stay available; the repo file adds its own.
    assert set(merged["providers"]) == {"userprov", "groq"}
    assert merged["providers"]["groq"]["baseUrl"] == (
        "https://api.groq.com/openai/v1"
    )
    # Issue #172: settings.json is a REAL per-run file consistent with
    # the merged catalog (a symlink to the global file would keep its
    # defaults/enabledModels pointing at models absent from this run's
    # catalog). The user's `{}` base stays `{}`. auth.json is absent in
    # the user dir, so no symlink.
    settings = agent_dir / "settings.json"
    assert not settings.is_symlink()
    assert json.loads(settings.read_text(encoding="utf-8")) == {}
    assert not (agent_dir / "auth.json").exists()


def test_prepare_pi_agent_dir_repo_providers_win_on_collision(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(
        home,
        models={"providers": {"groq": {
            "baseUrl": "http://stale:1/v1",
            "api": "openai-completions",
            "apiKey": "u",
            "models": [{"id": "old"}],
        }}},
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    # The repo file is the source of truth for the providers it names.
    assert merged["providers"]["groq"]["baseUrl"] == (
        "https://api.groq.com/openai/v1"
    )
    assert [m["id"] for m in merged["providers"]["groq"]["models"]] == (
        ["qwen/qwen3.8-27b"]
    )


def test_prepare_pi_agent_dir_keeps_api_key_env_reference_verbatim(
    tmp_path, monkeypatch,
):
    """The per-run catalog keeps the literal `$VAR` reference (Issue #1554).

    Pi 1.0 interpolates `apiKey` from the environment it inherits at
    request time, so the per-run `models.json` must never carry the
    plaintext key resolved into a file inside the worktree. Config load
    still validates that the selected provider's reference resolves.
    """
    home = tmp_path / "home"
    _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORBI_TEST_KEY", "secret-value")
    providers = {
        "providers": {
            "testprov": {
                "baseUrl": "http://127.0.0.1:18084/v1",
                "api": "openai-completions",
                "apiKey": "$ORBI_TEST_KEY",
                "models": [{"id": "test-model"}],
            },
        }
    }
    config_path = _providers_config(
        tmp_path, providers,
        pi_provider='"testprov"', pi_model='"test-model"',
    )
    config = config_domain.load_config(config_path)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    raw = (agent_dir / "models.json").read_text(encoding="utf-8")
    merged = json.loads(raw)
    assert merged["providers"]["testprov"]["apiKey"] == "$ORBI_TEST_KEY"
    assert "secret-value" not in raw


def test_prepare_pi_agent_dir_keeps_braced_and_embedded_references(
    tmp_path, monkeypatch,
):
    """`${VAR}` and references embedded in larger literals stay verbatim
    too (Pi resolves its documented interpolation syntax at request
    time, `docs/models.md`)."""
    home = tmp_path / "home"
    _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TEST_PREFIX", "alpha")
    monkeypatch.setenv("TEST_SUFFIX", "omega")
    providers = {
        "providers": {
            "local": {
                "baseUrl": "http://127.0.0.1:18082/v1",
                "api": "openai-completions",
                "apiKey": "${TEST_PREFIX}_mid_$TEST_SUFFIX",
                "models": [{"id": "Qwen3.8-27B"}],
            },
        }
    }
    config = _model_config(
        tmp_path, pi_providers_data=providers,
        pi_provider="local", pi_model="Qwen3.8-27B",
    )
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["local"]["apiKey"] == (
        "${TEST_PREFIX}_mid_$TEST_SUFFIX"
    )


def test_prepare_pi_agent_dir_keeps_every_reference_verbatim(
    tmp_path, monkeypatch,
):
    """Every provider's reference is copied verbatim, selected or not:
    a variable missing from the environment is resolved (or rejected)
    by Pi/config load, never materialized here."""
    home = tmp_path / "home"
    _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "sk-real-key-value")
    providers = {
        "providers": {
            "groq": GROQ_PROVIDERS["providers"]["groq"],
            "other": {
                "baseUrl": "http://127.0.0.1:18083/v1",
                "api": "openai-completions",
                "apiKey": "$MISSING_PROVIDER_KEY",
                "models": [{"id": "m"}],
            },
        }
    }
    config = _model_config(
        tmp_path, pi_providers_data=providers,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["groq"]["apiKey"] == "$GROQ_API_KEY"
    assert merged["providers"]["other"]["apiKey"] == "$MISSING_PROVIDER_KEY"


def test_prepare_pi_agent_dir_leaves_non_string_api_keys_untouched(
    tmp_path, monkeypatch,
):
    """Entries without a string `apiKey` (absent, or a malformed
    non-object entry from the user's own models.json) pass through
    unchanged — the verbatim copy never invents or crashes on them."""
    home = tmp_path / "home"
    _user_agent_dir(
        home,
        models={"providers": {
            "nokey": {"baseUrl": "http://u:1/v1", "api": "x"},
            "junk": "not-an-object",
        }},
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(
        tmp_path, pi_providers_data=GROQ_PROVIDERS,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert "apiKey" not in merged["providers"]["nokey"]
    assert merged["providers"]["junk"] == "not-an-object"


def test_prepare_pi_agent_dir_keeps_zai_scene_reference_from_load_config(
    tmp_path, monkeypatch,
):
    """The z.ai-shaped provider file (`apiKey: "$ZAI_API_KEY"`) loaded
    through `load_config` must reach the per-run catalog verbatim, so
    the plaintext key is never written into the worktree (Issue #1554)."""
    home = tmp_path / "home"
    _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-real-key")
    providers = {
        "providers": {
            "z-ai": {
                "baseUrl": "https://api.z.ai/api/paas/v4",
                "api": "openai-completions",
                "apiKey": "$ZAI_API_KEY",
                "models": [{"id": "glm-5.3-flash"}],
            },
        }
    }
    config_path = _providers_config(
        tmp_path, providers,
        pi_provider='"z-ai"', pi_model='"glm-5.3-flash"',
    )
    config = config_domain.load_config(config_path)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["z-ai"]["apiKey"] == "$ZAI_API_KEY"
    assert merged["providers"]["z-ai"]["baseUrl"] == (
        "https://api.z.ai/api/paas/v4"
    )


def test_prepare_pi_agent_dir_auth_symlink_settings_real_file(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    user_agent = _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    # auth.json stays a symlink (stored auth for the merged catalog's
    # providers is still valid); settings.json is a real per-run file
    # (Issue #172), never a link to the user's global settings.
    auth_link = agent_dir / "auth.json"
    assert auth_link.is_symlink()
    assert auth_link.resolve() == user_agent / "auth.json"
    settings = agent_dir / "settings.json"
    assert not settings.is_symlink()
    assert settings.is_file()


# --- per-run settings consistency (Issue #172) ----------------------------


def _user_settings(home: Path, settings: dict) -> Path:
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8",
    )
    return agent


def test_prepare_pi_agent_dir_settings_points_at_selected_provider_model(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    user_agent = _user_agent_dir(
        home,
        models={"providers": {"local-qwen": {
            "baseUrl": "http://127.0.0.1:18082/v1",
            "api": "openai-completions",
            "apiKey": "local",
            "models": [{"id": "qwen3.8:27b"}],
        }}},
        settings=False,
    )
    _user_settings(home, {
        "defaultProvider": "openai",
        "defaultModel": "gpt-5.6-sol",
        "enabledModels": [
            "openai/gpt-5.6-sol",
            "openrouter/stealth/ox-alpha",
        ],
        "httpIdleTimeoutMs": 0,
        "theme": "light/dark",
        "packages": ["npm:pi-mcp-adapter"],
    })
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(
        tmp_path, pi_providers_data=GROQ_PROVIDERS,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # The runtime defaults and enabled models point at the SELECTED
    # provider/model (which exists in the merged catalog) — never at a
    # model the per-run catalog cannot resolve.
    assert settings["defaultProvider"] == "groq"
    assert settings["defaultModel"] == "qwen/qwen3.8-27b"
    assert settings["enabledModels"] == ["groq/qwen/qwen3.8-27b"]
    # The user's disabled (0) HTTP idle timeout is dropped: Pi falls
    # back to its built-in default so a first response that never
    # arrives fails with a concrete timeout instead of hanging.
    assert "httpIdleTimeoutMs" not in settings
    # The user's other settings are preserved.
    assert settings["theme"] == "light/dark"
    assert settings["packages"] == ["npm:pi-mcp-adapter"]
    # The global file is never modified.
    global_settings = json.loads(
        (user_agent / "settings.json").read_text(encoding="utf-8"),
    )
    assert global_settings["defaultProvider"] == "openai"
    assert global_settings["httpIdleTimeoutMs"] == 0


def test_prepare_pi_agent_dir_settings_filters_enabled_models_to_catalog(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(
        home,
        models={"providers": {"userprov": {
            "baseUrl": "http://user:1/v1",
            "api": "openai-completions",
            "apiKey": "u",
            "models": [{"id": "um"}],
        }}},
        settings=False,
    )
    _user_settings(home, {
        "enabledModels": [
            "userprov/um",
            "openai/gpt-5.6-sol",
            "bogus/none",
        ],
    })
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # Without a selected provider/model only the patterns that resolve
    # in the merged catalog (user providers + repo file) survive.
    assert settings["enabledModels"] == ["userprov/um"]


def test_prepare_pi_agent_dir_settings_bare_model_id_resolves_when_unambiguous(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(
        home,
        models={"providers": {
            "provA": {
                "baseUrl": "http://a:1/v1",
                "api": "openai-completions",
                "apiKey": "a",
                "models": [{"id": "shared"}, {"id": "only-a"}],
            },
            "provB": {
                "baseUrl": "http://b:1/v1",
                "api": "openai-completions",
                "apiKey": "b",
                "models": [{"id": "shared"}],
            },
        }},
        settings=False,
    )
    _user_settings(home, {
        "enabledModels": ["only-a", "shared", ""],
    })
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # Pi's exact match: an unambiguous bare id resolves, an ambiguous
    # one (same id on two providers) does not, an empty pattern never
    # does.
    assert settings["enabledModels"] == ["only-a"]


def test_prepare_pi_agent_dir_settings_ignores_malformed_catalog_entries(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(
        home,
        models={"providers": {
            "nomodels": {"baseUrl": "http://n:1/v1", "api": "x"},
            "badmodel": {
                "baseUrl": "http://b:1/v1",
                "api": "x",
                "models": [{"name": "no id"}],
            },
            "good": {
                "baseUrl": "http://g:1/v1",
                "api": "x",
                "models": [{"id": "gm"}],
            },
        }},
        settings=False,
    )
    _user_settings(home, {"enabledModels": ["good/gm", "nomodels/x"]})
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # Malformed user catalog entries are skipped, never a crash: the
    # resolvable pattern survives.
    assert settings["enabledModels"] == ["good/gm"]


def test_prepare_pi_agent_dir_settings_user_file_not_object_fails_fast(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    (home / ".pi" / "agent" / "settings.json").write_text(
        "[1, 2]", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    with pytest.raises(ValueError, match="must be a JSON object"):
        pi_session.prepare_pi_agent_dir(tmp_path, config)


def test_prepare_pi_agent_dir_settings_drops_unresolvable_enabled_models(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    _user_settings(home, {"enabledModels": ["openai/gpt-5.6-sol"]})
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # Nothing resolves: the key is dropped entirely (Pi falls back to
    # the full catalog) — an empty list would scope the run to zero
    # models.
    assert "enabledModels" not in settings


def test_prepare_pi_agent_dir_settings_keeps_nonzero_http_idle_timeout(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    _user_settings(home, {"httpIdleTimeoutMs": 60000})
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    # A real (non-disabled) timeout is the user's explicit choice and
    # stays.
    assert settings["httpIdleTimeoutMs"] == 60000


def test_prepare_pi_agent_dir_settings_user_file_missing(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(
        tmp_path, pi_providers_data=GROQ_PROVIDERS,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    assert settings == {
        "defaultProvider": "groq",
        "defaultModel": "qwen/qwen3.8-27b",
        "enabledModels": ["groq/qwen/qwen3.8-27b"],
    }


def test_prepare_pi_agent_dir_settings_user_file_invalid_fails_fast(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    (home / ".pi" / "agent" / "settings.json").write_text(
        "{nope", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    with pytest.raises(ValueError, match="not valid JSON"):
        pi_session.prepare_pi_agent_dir(tmp_path, config)


def test_prepare_pi_agent_dir_selected_model_not_in_catalog_fails_fast(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(
        tmp_path, pi_providers_data=GROQ_PROVIDERS,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    # The selected provider/model is validated against the provider
    # file at config load (load_config); prepare_pi_agent_dir receives
    # the same validated config, so the per-run settings can always
    # point at a model that exists in the merged catalog.
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    model_ids = [
        f"{pid}/{m['id']}"
        for pid, entry in merged["providers"].items()
        for m in entry.get("models", [])
    ]
    assert settings["enabledModels"][0] in model_ids


def test_prepare_pi_agent_dir_user_dir_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert set(merged["providers"]) == {"groq"}
    # Issue #172: the per-run settings.json is always written (an empty
    # base when the user has none) — the run never inherits the global
    # file's defaults through a symlink.
    settings = json.loads(
        (agent_dir / "settings.json").read_text(encoding="utf-8"),
    )
    assert settings == {}
    assert not (agent_dir / "auth.json").exists()


def test_prepare_pi_agent_dir_user_models_json_invalid_fails_fast(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    user_agent = _user_agent_dir(home, models=None)
    (user_agent / "models.json").write_text("{nope", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    with pytest.raises(ValueError, match="not valid JSON"):
        pi_session.prepare_pi_agent_dir(tmp_path, config)


def test_stream_pi_sets_pi_env_on_the_process(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('PI_CODING_AGENT_DIR', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, pi_env={"PI_CODING_AGENT_DIR": "/agent-dir"})
    assert result == "/agent-dir"


def test_stream_pi_without_pi_env_keeps_inherited_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "inherited"


def test_stream_pi_strips_the_telegram_token_from_the_child_env(
    tmp_path, monkeypatch,
):
    """Issue #1673: the bot token stays in the Runner, never in Pi.

    Notifications are a bypass the child never needs, so the token is
    removed from the child environment while everything else stays
    inherited (the pre-#157 shape)."""
    monkeypatch.setenv("ORBI_TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', '') + '|' "
        "+ os.environ.get('ORBI_TELEGRAM_BOT_TOKEN', 'ABSENT'))\n"
    )
    result = pi_session.stream_pi(
        [sys.executable, "-c", script],
        ctx=RunContext(run_id="deadbeef", issue=24, branch="b",
                       worktree=tmp_path, source_repo="xqliu/orbi"),
        watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
    )
    assert result == "inherited|ABSENT"


def test_run_pi_materializes_provider_dir_and_env(monkeypatch, tmp_path):
    """The implementer session gets the materialized dir via env."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "done",
    )
    config = _model_config(
        tmp_path, pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    kwargs = calls[0]
    agent_dir = tmp_path / ".orbi" / "pi-agent"
    assert kwargs["pi_env"] == {"PI_CODING_AGENT_DIR": str(agent_dir)}
    assert (agent_dir / "models.json").is_file()
    # The redacted command is unchanged: no baseUrl, no apiKey, no dir.
    assert "https://api.groq.com/openai/v1" not in " ".join(
        kwargs["log_command"]
    )
    assert "PI_CODING_AGENT_DIR" not in " ".join(kwargs["log_command"])


def test_run_pi_without_providers_keeps_pre_157_env(monkeypatch, tmp_path):
    """No provider file: no materialization, no pi_env (pre-#157)."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "done",
    )
    config = _model_config(tmp_path)
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    kwargs = calls[0]
    assert "pi_env" not in kwargs
    assert not (tmp_path / ".orbi" / "pi-agent").exists()


def test_run_review_materializes_provider_dir_and_env(monkeypatch, tmp_path):
    """The review session uses the SAME provider config (Issue #157)."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "ok",
    )
    config = _model_config(
        tmp_path, pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    kwargs = calls[0]
    agent_dir = tmp_path / ".orbi" / "pi-agent"
    assert kwargs["pi_env"] == {"PI_CODING_AGENT_DIR": str(agent_dir)}
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["groq"]["baseUrl"] == (
        "https://api.groq.com/openai/v1"
    )


def test_load_config_pi_providers_rejects_top_level_list(tmp_path):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    (tmp_path / "pi-providers.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(
        ValueError, match="must have a 'providers' object",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_accepts_entry_without_models(tmp_path):
    """An override-only entry (no models, e.g. routing a built-in
    provider through a proxy) is valid — Pi's own schema allows it."""
    providers = {"providers": {"anthropic": {
        "baseUrl": "https://proxy.example.com/v1",
    }}}
    config_path = _providers_config(tmp_path, providers)
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == providers


def test_load_config_pi_providers_rejects_empty_models_list(tmp_path):
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "api": "openai-completions",
        "models": [],
    }}}
    config_path = _providers_config(tmp_path, providers)
    with pytest.raises(
        ValueError, match="models must be a non-empty list",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_rejects_model_without_id(tmp_path):
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "api": "openai-completions",
        "models": [{"contextWindow": 131072}],
    }}}
    config_path = _providers_config(tmp_path, providers)
    with pytest.raises(
        ValueError, match="has a model without an id",
    ):
        config_domain.load_config(config_path)


def test_load_config_pi_providers_provider_without_model_ok(
    tmp_path, monkeypatch,
):
    """pi_provider set, pi_model unset: the model check is skipped
    (Pi resolves its own default model for the provider)."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
    )
    config = config_domain.load_config(config_path)
    assert config.pi_model is None


def test_load_config_pi_providers_provider_without_api_key_ok(
    tmp_path, monkeypatch,
):
    """A selected provider without apiKey (auth via auth.json /
    --api-key) is valid — nothing to resolve."""
    providers = {"providers": {"local": {
        "baseUrl": "http://127.0.0.1:18082/v1",
        "api": "openai-completions",
        "models": [{"id": "Qwen3.8-27B"}],
    }}}
    config_path = _providers_config(
        tmp_path, providers, pi_provider='"local"',
        pi_model='"Qwen3.8-27B"',
    )
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == providers


def test_load_config_pi_providers_braced_env_reference(tmp_path, monkeypatch):
    """`${VAR}` is the braced env-var form of Pi's value syntax."""
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "api": "openai-completions",
        "apiKey": "${GROQ_API_KEY}",
        "models": [{"id": "qwen/qwen3.8-27b"}],
    }}}
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config_path = _providers_config(
        tmp_path, providers, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"',
    )
    with pytest.raises(
        ValueError, match="environment variable GROQ_API_KEY is not set",
    ):
        config_domain.load_config(config_path)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == providers


def test_load_config_api_key_from_deploy_env_file_ok(tmp_path, monkeypatch):
    """Issue #348: the key lives in `<deploy_home>/.orbi/env` (step 4 of
    getting-started) but is NOT exported in the shell — `load_config`
    must read the env file and validate successfully."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    env_file = tmp_path / ".orbi" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GROQ_API_KEY=file-key\n", encoding="utf-8")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"',
    )
    config = config_domain.load_config(config_path)
    assert config.pi_providers_data == GROQ_PROVIDERS
    # The variable must be visible to the process so Pi can interpolate
    # the verbatim `$GROQ_API_KEY` reference in the per-run catalog.
    assert os.environ.get("GROQ_API_KEY") == "file-key"


def test_load_config_api_key_shell_export_wins_over_env_file(
    tmp_path, monkeypatch,
):
    """A shell-exported value stays the documented override for manual
    ticks: the env file never replaces an existing variable."""
    monkeypatch.setenv("GROQ_API_KEY", "shell-key")
    env_file = tmp_path / ".orbi" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GROQ_API_KEY=file-key\n", encoding="utf-8")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"',
    )
    config_domain.load_config(config_path)
    assert os.environ.get("GROQ_API_KEY") == "shell-key"


def test_load_config_api_key_missing_everywhere_names_both_remedies(
    tmp_path, monkeypatch,
):
    """Key absent from the shell AND from `.orbi/env`: the failure must
    point at both fixes, not only name the missing variable."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"',
    )
    with pytest.raises(
        ValueError,
        match=r"environment variable GROQ_API_KEY is not set.*"
        r"Export GROQ_API_KEY.*\.orbi/env",
    ):
        config_domain.load_config(config_path)


def test_load_config_env_file_absent_missing_key_still_fails(
    tmp_path, monkeypatch,
):
    """No `.orbi/env` at all: the pre-#348 behavior is unchanged."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"',
    )
    with pytest.raises(
        ValueError, match="environment variable GROQ_API_KEY is not set",
    ):
        config_domain.load_config(config_path)


def test_load_config_env_file_follows_explicit_deploy_home(
    tmp_path, monkeypatch,
):
    """The env file is read from `<deploy_home>/.orbi/env`, not the
    delivery checkout (repo_dir) when deploy_home is explicit."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    home = tmp_path / "orbi-home"
    home.mkdir()
    env_file = home / ".orbi" / "env"
    env_file.parent.mkdir()
    env_file.write_text("GROQ_API_KEY=home-key\n", encoding="utf-8")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
        pi_model='"qwen/qwen3.8-27b"', deploy_home=f'"{home}"',
    )
    config_domain.load_config(config_path)
    assert os.environ.get("GROQ_API_KEY") == "home-key"


def test_load_deploy_env_file_parses_comments_quotes_and_export(
    tmp_path, monkeypatch,
):
    """Plain systemd EnvironmentFile syntax: blank lines and `#` comments
    are skipped; matching quotes are stripped; `export ` prefix is
    accepted."""
    env_file = tmp_path / ".orbi" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "# a comment\n"
        "\n"
        'QUOTED="double"\n'
        "SINGLE='single'\n"
        "export EXPORTED=yes\n"
        "PLAIN=raw\n",
        encoding="utf-8",
    )
    config_domain._load_deploy_env_file(tmp_path)
    assert os.environ.get("QUOTED") == "double"
    assert os.environ.get("SINGLE") == "single"
    assert os.environ.get("EXPORTED") == "yes"
    assert os.environ.get("PLAIN") == "raw"


def test_load_deploy_env_file_malformed_line_fails_fast(tmp_path):
    """A line without `=` is a misconfiguration — fail fast, no fallback."""
    env_file = tmp_path / ".orbi" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("NOT_AN_ASSIGNMENT\n", encoding="utf-8")
    with pytest.raises(ValueError, match="NOT_AN_ASSIGNMENT"):
        config_domain._load_deploy_env_file(tmp_path)


def test_prepare_pi_agent_dir_user_models_json_without_providers_key(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    user_agent = _user_agent_dir(home, models=None)
    (user_agent / "models.json").write_text(
        '{"nope": 1}', encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert set(merged["providers"]) == {"groq"}


# --- e2e: the provider endpoint reaches the Pi process (Issue #157) -------


FAKE_PI_PROVIDER = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
provider = args[args.index("--provider") + 1]
model = args[args.index("--model") + 1]
agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
assert agent_dir, "PI_CODING_AGENT_DIR is not set"
catalog = json.load(open(os.path.join(agent_dir, "models.json")))
entry = catalog["providers"][provider]
assert model in [m["id"] for m in entry["models"]], (
    f"model {model} not in materialized catalog"
)
# Prove the provider endpoint from the repo file reached Pi: print the
# baseUrl of the selected provider (the runner never puts it on argv).
sys.stdout.write(entry["baseUrl"])
"""


def test_e2e_provider_endpoint_reaches_pi_process(monkeypatch, tmp_path):
    """run_pi -> materialized dir -> PI_CODING_AGENT_DIR -> Pi sees the
    repo-file provider endpoint (baseUrl) for the selected model."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "pi"
    fake.write_text(FAKE_PI_PROVIDER, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    )
    config = _model_config(
        tmp_path, pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    result = pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    assert result == "https://api.groq.com/openai/v1"


def test_prepare_pi_agent_dir_is_idempotent_on_resume(
    tmp_path, monkeypatch,
):
    """A resumed run in the same worktree replaces stale files."""
    home = tmp_path / "home"
    user_agent = _user_agent_dir(home, auth=False)
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)
    # Simulate stale leftovers from an earlier attempt: a broken auth
    # symlink and a settings symlink (the pre-#172 shape).
    stale = agent_dir / "auth.json"
    stale.symlink_to(tmp_path / "gone.json")
    assert not stale.exists()  # broken
    settings = agent_dir / "settings.json"
    settings.unlink()
    settings.symlink_to(user_agent / "settings.json")
    pi_session.prepare_pi_agent_dir(tmp_path, config)
    assert not (agent_dir / "auth.json").exists()
    # The stale settings symlink is replaced by the real per-run file.
    assert not settings.is_symlink()
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


# ---------------------------------------------------------------------------
# Issue #48: log the active Issue context when the Runner is stopped
# (SIGTERM): `run_stopping` before the stop, `run_stopped` after the Pi
# child exits, `result=idle` when no Issue was claimed yet.
# ---------------------------------------------------------------------------

def _write_session_file(worktree: Path, session_id: str = "sess-1") -> None:
    """A session JSONL with a tool call so the snapshot shows phase=bash."""
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records = [
        {"type": "session", "id": session_id,
         "timestamp": fresh_timestamp(), "cwd": str(worktree)},
        {"type": "message", "id": "a1", "timestamp": fresh_timestamp(),
         "message": {"role": "assistant", "content": [
             {"type": "toolCall", "id": "t1", "name": "bash",
              "arguments": {"command": "pytest tests/"}}]}},
    ]
    with open(session_dir / f"{session_id}.jsonl", "w",
              encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_stop_handler_idle_logs_run_stopped_idle_and_exits(
    monkeypatch, caplog,
):
    # No run in flight (before the claim): the stop logs result=idle
    # WITHOUT any invented issue fields and exits 143.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = [line for line in caplog.text.splitlines()
             if " run_stopped " in line]
    assert len(lines) == 1
    assert "result=idle" in lines[0]
    assert "issue=" not in lines[0]
    # No run was in flight: no run_stopping line either.
    assert "run_stopping" not in caplog.text


def test_stop_handler_active_run_logs_stopping_then_stopped_and_exits(
    monkeypatch, caplog, tmp_path,
):
    # Run in flight: run_stopping carries the full scene BEFORE the
    # stop, the live Pi child is TERMed and waited for, then
    # run_stopped result=interrupted, then the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree=str(worktree), source_repo="owner/repo"), "Log the stop scene")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    runner.set_active_pi(child)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = caplog.text.splitlines()
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # run_stopping comes BEFORE run_stopped (the child exit is in
    # between).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # No session file yet: the snapshot fields are '-' (never invented).
    assert "phase=-" in line
    assert "session=-" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # The live Pi child was shut down: no orphan survives the stop.
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_stop_handler_active_run_uses_activity_snapshot_for_scene(
    monkeypatch, caplog, tmp_path,
):
    # With a session file the run_stopping line carries the phase and
    # session of the existing activity snapshot (reused, not re-parsed
    # ad hoc).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    line = [line for line in caplog.text.splitlines()
            if " run_stopping " in line][0]
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert 128 + died["signum"] == 143


def test_stop_handler_active_run_without_live_child_still_stops(
    monkeypatch, caplog, tmp_path,
):
    # Run in flight but no live Pi child (between sessions): both lines
    # are still logged and the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    lines = caplog.text.splitlines()
    assert any(" run_stopping " in line for line in lines)
    assert any(
        line.endswith("run_stopped issue=48 result=interrupted")
        for line in lines
    )
    assert 128 + died["signum"] == 143


def test_stop_handler_snapshot_failure_still_logs_and_exits(
    monkeypatch, caplog,
):
    # A failing activity snapshot never swallows the stop: the scene
    # falls back to `-` for phase/session and the stop still exits.
    monkeypatch.setattr(journal, "_ACTIVE_RUN",
        {"issue": 48, "title": "t", "branch": "b", "worktree": "/w",
         "pi": None},
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    def boom(_dir):
        raise RuntimeError("snapshot exploded")
    monkeypatch.setattr(runner, "activity_snapshot", boom)
    with caplog.at_level("INFO"):
        runner._stop_delivery(signal.SIGTERM)
    assert died["signum"] == signal.SIGTERM
    stopping = [
        line for line in caplog.text.splitlines()
        if " run_stopping " in line
    ]
    assert len(stopping) == 1
    assert "phase=-" in stopping[0]
    assert "session=-" in stopping[0]
    assert "snapshot exploded" in caplog.text


def test_stop_handler_reinstalls_default_disposition_and_raises_signal(
    monkeypatch, caplog,
):
    # After the stop work the handler restores SIGTERM's default
    # disposition and re-raises the signal at itself: the process dies
    # from the ORIGINAL signal (systemd sees a signal-caused stop, exit
    # 143) and a handler crash can never swallow the stop.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    monkeypatch.setattr(runner.os, "_exit", lambda code: None)
    raised = {}
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: raised.setdefault("args", (pid, sig)),
    )
    runner._die_from_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert raised["args"] == (os.getpid(), signal.SIGTERM)


def test_set_active_run_binds_scene_and_pi_tracking(monkeypatch):
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=7, branch="b", worktree="/w", source_repo="owner/repo"), "t")
    assert journal._ACTIVE_RUN == {
        "issue": 7, "title": "t", "branch": "b", "worktree": "/w",
        "pi": None,
    }
    child = object()
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN["pi"] is child
    runner.set_active_pi(None)
    assert journal._ACTIVE_RUN["pi"] is None
    runner.clear_active_run()
    assert journal._ACTIVE_RUN is None
    # set_active_pi without a bound run is a no-op (never crashes).
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN is None


def test_main_installs_the_stop_handler(monkeypatch, tmp_path):
    # main() installs the SIGTERM handler right after logging is set up,
    # so every phase of the tick (pre-claim, claim, implement, delivery
    # wait) stops with the scene.
    installed = {}
    monkeypatch.setattr(
        signal, "signal",
        lambda sig, handler: installed.setdefault(sig, handler),
    )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'workspace_root = ".."\nprompt = "prompt.md"\n'
        'prompt_review = "prompt_review.md"\n',
        encoding="utf-8",
    )
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        claim, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "acquire_slot", lambda *args: Mock(release=lambda: None))
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged")
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
    assert runner.main(["--config", str(config_path)]) == 0
    assert installed[signal.SIGTERM] is runner._handle_stop


# Issue #48 acceptance: a REAL subprocess SIGTERM test — the Runner
# process is a real child of the test, stopped with a real SIGTERM, and
# the journal (captured from its stderr) proves the ordering and fields.
STOP_DRIVER = """
import logging
import signal
import subprocess
import sys
import time

sys.path.insert(0, {repo!r} + "/src")
import orbi.runner as runner
import orbi.milestone as milestone
from orbi.delivery_scene import RunContext

logging.basicConfig(level=logging.INFO, format=runner.log_format())
# Exactly what main() does (Issue #48): install the stop handler first.
signal.signal(signal.SIGTERM, runner._handle_stop)
runner.set_run_id("a1b2c3d4")
runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree={worktree!r}, source_repo="owner/repo"), "Log the stop scene")
# The real Pi-like child: a real long-running process (what stream_pi
# tracks via set_active_pi). It lingers briefly on SIGTERM before
# exiting (a slow shutdown), so the stop handler's child.wait() has a
# real wait to do.
child = subprocess.Popen(
    [sys.executable, "-c",
     "import os, signal, time\\n"
     "signal.signal(signal.SIGTERM, lambda s, f: "
     "(time.sleep(0.5), os._exit(0)))\\n"
     "time.sleep(60)\\n"],
)
runner.set_active_pi(child)
print("ready child=" + str(child.pid), flush=True)
# Sit in the poll loop (like stream_pi) until stopped.
time.sleep(120)
"""


def test_shutdown_child_kills_a_child_that_ignores_sigterm(tmp_path):
    """Root cause (Issue #48): the stop handler must not block past the
    grace when the Pi child ignores SIGTERM (e.g. a model/network call).
    It TERMs, waits at most `grace`, then KILLs and reaps the child so
    the Runner exits with the original signal before systemd's own
    stop deadline — never leaving `Result=timeout`/failed."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time, sys\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "print(\"ready\", flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    # Wait until the child has INSTALLED SIG_IGN: else a
    # terminate() before the handler is set would exit the child
    # on SIGTERM and never exercise the grace/kill path.
    assert child.stdout.readline().strip() == "ready"
    # The child ignores SIGTERM: a bare terminate()+wait() would
    # block forever. `_shutdown_child` with a short grace must
    # escalate to SIGKILL and reap it within that grace.
    runner._shutdown_child(child, grace=0.5)
    assert child.poll() is not None
    # Reaped with a kill signal, not a clean SIGTERM exit.
    assert child.returncode is not None and child.returncode < 0


def test_shutdown_child_noop_for_exited_or_none_child():
    """A None or already-exited child is a no-op (no hang, no signal)."""
    runner._shutdown_child(None)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    runner._shutdown_child(child)
    assert child.poll() is not None


def test_real_subprocess_sigterm_logs_stop_scene_and_shuts_down_pi(
    tmp_path,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    driver = tmp_path / "driver.py"
    driver.write_text(
        STOP_DRIVER.format(
            repo=str(Path(__file__).resolve().parent.parent),
            worktree=str(worktree),
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = proc.stdout.readline()
    assert ready.startswith("ready child=")
    child_pid = int(ready.strip().split("=")[1])
    # The Pi child is alive before the stop.
    time.sleep(0.2)
    assert _pid_alive(child_pid)
    # The real SIGTERM that systemd would send on stop.
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    # The process exits with 128+SIGTERM = 143 (the value systemd
    # records for a signal-caused stop).
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # Ordering: run_stopping BEFORE run_stopped (the Pi child exit is
    # in between), and both BEFORE the process termination (they are in
    # the captured stderr of the already-exited process).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    # The [run_id] prefix (the existing run correlation, reused); the
    # journal format is `LEVEL message`.
    assert stopping[0].startswith("INFO [a1b2c3d4] run_stopping ")
    assert stopped[0].startswith("INFO [a1b2c3d4] run_stopped ")
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # phase/session from the existing activity snapshot of the
    # worktree's .pi-session.
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # No orphan Pi: the stop handler waited for the child to exit
    # BEFORE the process exited (driver exit implies the child was
    # reaped), so the child must be gone now.
    assert not _pid_alive(child_pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user: alive.
        return True
    return True


def test_pid_alive_reports_zombie_and_foreign_pid(monkeypatch):
    # A killed-but-unreaped child is a zombie: os.kill(pid, 0) succeeds
    # on it, so it still counts as alive (the stop handler's
    # child.poll() check is what distinguishes a zombie).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    child.kill()
    time.sleep(0.2)
    assert _pid_alive(child.pid) is True
    child.wait(timeout=5)
    # A pid that no longer exists (reaped).
    assert _pid_alive(child.pid) is False
    # A foreign pid (owned by another user) reports alive via
    # PermissionError — exercise the branch by stubbing os.kill.
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError()),
    )
    assert _pid_alive(1) is True


def test_real_subprocess_sigterm_before_claim_logs_idle(tmp_path):
    # Stopped before claiming an Issue: run_stopped result=idle, no
    # invented Issue fields.
    driver = tmp_path / "driver_idle.py"
    driver.write_text(
        "import logging, signal, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        "import orbi.runner as runner\n"
        "logging.basicConfig(level=logging.INFO, format=runner.log_format())\n"
        "signal.signal(signal.SIGTERM, runner._handle_stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopped) == 1
    assert stopped[0] == "INFO run_stopped result=idle"
    assert "issue=" not in stderr
    assert "run_stopping" not in stderr


# --- Issue #98: ticket kinds — content-only, ops, release dispatch --------


def test_process_issue_routes_release_to_process_release(monkeypatch):
    issue = {"number": 99, "title": "Release v0.3.0", "body": "",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    calls = []
    # Issue #708: the release dispatch now direct-reads the live label
    # state before dispatching (`gh issue view`) — the fresh ticket here
    # answers with no labels, and no slot config means no yield path.
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"labels": []}),
    )
    monkeypatch.setattr(release, "process_release",
                        lambda i, c, r: calls.append("release") or "rel-url")
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("run_pi must not run for a release task")))
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc")
    result = runner.process_issue(issue, config_domain.RunnerConfig(base_branch="main"), "o/r")
    assert result == runner.IssueResult("release", "rel-url")
    assert calls == ["release"]


def test_is_content_only_requires_the_explicit_label():
    """Issue #537: the content（纯内容）path dispatches on
    `ai-content-only`; the old `ai-ops-only` name now means ops."""
    assert runner.is_content_only(
        {"labels": [{"name": "ai-content-only"}]},
    ) is True
    assert runner.is_content_only(
        {"labels": [{"name": "ai-ops-only"}]},
    ) is False
    assert runner.is_content_only({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_content_only({"labels": "ai-content-only"}) is False


def test_is_ops_requires_the_explicit_label():
    """Issue #537: `ai-ops-only` is the full-execution ops marker — it
    must NOT dispatch the content agent anymore."""
    assert runner.is_ops({"labels": [{"name": "ai-ops-only"}]}) is True
    assert runner.is_ops({"labels": [{"name": "ai-content-only"}]}) is False
    assert runner.is_ops({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_ops({"labels": "ai-ops-only"}) is False


def test_run_ticket_agent_uses_a_temporary_session_without_git(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        # The prompt FILE lives in the session's TemporaryDirectory
        # (Issue #1634), so it is read while the session is running.
        lambda command, **kwargs: calls.append(
            (command, kwargs, rendered_prompt(command))) or "copy",
    )
    result = pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider=None, pi_model=None, pi_thinking=None),
        "o/r",
    )
    assert result == "copy"
    command, kwargs, prompt = calls[0]
    assert command[0] == "pi"
    # `pi --no-tools` is the enforcement boundary: a prompt instruction
    # alone cannot ensure the Agent never invokes git/gh or writes files.
    assert "--no-tools" in command
    assert "git/gh tools" in prompt
    assert kwargs["cwd"] != tmp_path
    assert kwargs["cwd"].name.startswith("orbi-ticket-")
    assert kwargs["ctx"].branch == "-"
    assert kwargs["role"] == runner.ROLE_TICKET
    assert str(tmp_path) not in command[command.index("--session-dir") + 1]
    assert Path(command[command.index("--session-dir") + 1]).parent == kwargs["cwd"]


def test_run_ticket_agent_materializes_provider_dir_and_env(
    monkeypatch, tmp_path,
):
    """Issue #1379: the ONE no-tools helper keeps reaching the configured
    provider — the materialization the clarify gate used to own still
    puts the per-run agent dir on `PI_CODING_AGENT_DIR`.

    The judgment session runs in a repo that configures a provider file
    (the ticket-only content session does too), and the selected model
    rides on `--provider/--model`: without the materialized catalog Pi
    could never resolve it, and the gate would silently fail open.
    """
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    seen: dict = {}

    def fake_stream(command, **kwargs):
        agent_dir = Path(kwargs["pi_env"]["PI_CODING_AGENT_DIR"])
        seen["kwargs"] = kwargs
        seen["models_written"] = (agent_dir / "models.json").is_file()
        return "copy"

    # The subprocess seam fans out to every binding (Issue #785), and
    # the patch ratchet forbids a new per-module `pi_session` patch
    # (Issue #789): the corpus may only shrink.
    monkeypatch.setattr(seam, "stream_pi", fake_stream)
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, run_id="a1b2c3d4", skills=(),
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    assert pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config, "o/r",
    ) == "copy"
    kwargs = seen["kwargs"]
    agent_dir = kwargs["cwd"] / ".orbi" / "pi-agent"
    # The per-run catalog reaches Pi through the env only...
    assert kwargs["pi_env"] == {"PI_CODING_AGENT_DIR": str(agent_dir)}
    assert seen["models_written"] is True
    # ...never through the journaled command.
    log = " ".join(kwargs["log_command"])
    assert str(agent_dir) not in log
    assert "https://api.groq.com/openai/v1" not in log
    # Transient OS state: the materialized dir goes with the session.
    assert not agent_dir.exists()


def test_run_ticket_agent_logs_provider_config_loaded(monkeypatch, tmp_path, caplog):
    """Issue #176: the ticket-only session logs the provider config
    line too (role=ticket) — every Pi session has the same startup
    sequence."""
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "copy")
    with caplog.at_level("INFO"):
        pi_session.run_ticket_agent(
            {"number": 99, "title": "Launch thread", "body": "Write copy"},
            config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b", pi_thinking=None),
            "o/r",
        )
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=ticket" in lines[0]
    assert "issue=o/r#99" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_progress_state_passes_startup_sub_phase_to_comment(tmp_path):
    """Issue #176: the progress comment's `phase` carries the watcher's
    startup sub-phase (session_pending / request_pending) instead of
    the generic `starting` while the first response is outstanding."""
    for sub_phase in ("session_pending", "request_pending"):
        state = runner._progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": sub_phase, "last_activity": None,
                      "action": None, "session_id": None}, language_code="en")
        assert state["phase"] == sub_phase
    # The tool-based phase passes through unchanged once the first
    # response arrived.
    state = runner._progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": "test", "last_activity": None,
                  "action": "bash pytest tests/", "session_id": "s1"}, language_code="en")
    assert state["phase"] == "test"


def test_process_ticket_only_posts_agent_output_without_git_delivery(monkeypatch):
    """A labeled content task is delivered in its Issue, never through Git."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}]}
    edits = []
    comments = []
    commands = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    monkeypatch.setattr(seam, "run_command",
                        lambda command, **kwargs: commands.append(command) or "")

    result = runner.process_issue(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")

    assert result == runner.IssueResult("ticket-only", None)
    assert edits == [
        (99, {"repo": "o/r", "add": "ai-in-progress"}),
        (99, {"repo": "o/r", "remove": "ai-in-progress"}),
    ]
    assert comments and "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50" in comments[0][1]["body"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comments[0][1]["body"]
    assert "run_id=a1b2c3d4" in comments[0][1]["body"]
    assert commands == [["gh", "issue", "close", "99", "--repo", "o/r"]]


def test_process_ticket_only_rejects_empty_agent_content(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(pi_session, "run_ticket_agent", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="returned no content"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")
    assert edits[-1][1]["add"] == "ai-blocked"


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_process_ticket_only_failure_marks_blocked_without_git_delivery(
        monkeypatch, language_code):
    """A failed ticket-only run blocks with the community help line from the
    ONE blocked exit, in the run's bound language (Issue #1730)."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    comments = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(
            issue, config_domain.RunnerConfig(
                repo_dir=Path("/repo"), message_language=language_code,
            ), "o/r",
        )
    assert edits[-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                              "remove": "ai-in-progress"})
    body = comments[-1][1]["body"]
    assert language.text(
        language_code, "runner.ticket_only_failed",
        detail="Pi failed",
        run_info="run_id=a1b2c3d4 priority=normal task_type=ticket-only",
        run_id="a1b2c3d4",
    ) in body
    assert "run_id=a1b2c3d4" in body
    help_line = language.text(language_code, "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1


def test_process_ticket_only_keeps_original_error_when_failure_reporting_fails(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("comment failed")))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")


def test_process_issue_keeps_pr_ready_label_when_scene_comment_5xx_exhausts(
    monkeypatch, tmp_path, caplog,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "stable_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
                        lambda *args, **kwargs: edits.append((args, kwargs)))
    monkeypatch.setattr(seam, "set_active_run", lambda *args, **k: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    def comment(number, *, repo, body):
        if "Orbi opened PR:" in body:
            raise subprocess.CalledProcessError(
                1, ["gh", "issue", "comment"],
                stderr="GraphQL: Something went wrong while executing your query",
            )
    monkeypatch.setattr(seam, "comment_issue", comment)
    monkeypatch.setattr(seam, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "run_pi", lambda *a, **k: "done")
    monkeypatch.setattr(seam, "deliver_pr", lambda *a, **k: "https://github.com/o/r/pull/1")
    def run(command, **kwargs):
        if command[0:3] == ["gh", "pr", "list"]:
            return "[]"
        return "abc123"
    monkeypatch.setattr(seam, "run_command", run)
    monkeypatch.setattr(seam, "delivery_step", Mock())
    monkeypatch.setattr(seam, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(seam, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(seam, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(seam, "_finish_progress", Mock())
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    with caplog.at_level("WARNING"):
        result = runner.process_issue(
            issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
        )
    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/1")
    assert any(kwargs.get("add") == runner.PR_OPENED_LABEL for _, kwargs in edits)
    assert "progress_comment_failed" in caplog.text


def test_process_issue_keeps_normal_flow_without_release_label(
    monkeypatch, tmp_path,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    # The worktree exists: `create_worktree` always returns a real
    # directory (the run state file is written into it, Issue #219).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(pi_session, "run_pi",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(runner, "deliver_pr",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    monkeypatch.setattr(runner, "delivery_step", Mock())
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "LOGGER", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(runner, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(runner, "_finish_progress", Mock())
    runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
    )


def _ops_issue_mocks(monkeypatch, tmp_path, *, head_sha: str, dirty: str):
    """Shared mock set for the `process_issue` ops-dispatch tests.

    `head_sha` answers `git rev-parse HEAD` (the frozen base means the
    session delivered no commit); `dirty` answers `git status
    --porcelain`. Every `gh`/`git`/pi collaborator is captured.
    """
    issue = {
        "number": 99, "title": "Merge the beta PR and verify deploy",
        "body": "Merge PR #7 into main, then verify the deployment.",
        "labels": [{"name": "ai-ready"}, {"name": "ai-ops-only"}],
    }
    commands: list[list[str]] = []
    configs: list[dict] = []
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda issue_arg, ctx, config, **k: configs.append(config),
    )
    monkeypatch.setattr(runner.runner_health, "record_run_attempt", Mock())
    monkeypatch.setattr(runner.runner_health, "record_pickup", Mock())

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head_sha
        if command[:2] == ["git", "status"]:
            return dirty
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    return issue, commands, worktree, configs


def test_process_issue_ops_without_commit_closes_with_evidence(
    monkeypatch, tmp_path,
):
    """Issue #537: a pure-ops session (real shell/gh work, evidence
    comments, no commit) is COMPLETE — the Runner closes the Issue,
    removes the claim label, and never enters the PR ceremony."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    ops_prompt = prompts / "prompt_ops.md"
    ops_prompt.write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pure ops delivery must not open a PR")
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)
    # The full-execution session ran with the OPS playbook, not the dev
    # one — and with the same worktree machinery (no --no-tools content
    # agent).
    assert len(configs) == 1
    assert configs[0].prompt == ops_prompt
    # Terminal transition mirrors the content path: close + claim label
    # removed. No PR, no push.
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    # The claim added the label at pickup; the delivery removes it.
    runner.edit_issue.assert_any_call(
        99, repo="o/r", remove=runner.IN_PROGRESS_LABEL,
    )


def test_process_issue_ops_health_record_failure_is_a_bypass(
    monkeypatch, tmp_path,
):
    """Issue #537/#266: the health state write is pure bypass — a state
    failure never turns a delivered pure-ops run into a failed one."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner.runner_health, "record_run_attempt",
        Mock(side_effect=OSError("state file unwritable")),
    )
    monkeypatch.setattr(runner, "deliver_pr", Mock())
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)


def test_process_issue_ops_with_commit_takes_the_pr_ceremony(
    monkeypatch, tmp_path,
):
    """Issue #537: an ops session that commits code (e.g. a
    wrangler.toml change) is delivered through the SAME deterministic
    closeout as a dev ticket — push + PR, `ai-pr-opened`."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="def456", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    deliver_calls: list[tuple] = []
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: deliver_calls.append(a) or (
            "https://github.com/o/r/pull/7"
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/7")
    assert len(deliver_calls) == 1
    assert any(c[:2] == ["git", "rev-parse"] for c in commands)


def test_process_issue_ops_with_uncommitted_leftovers_fails_fast(
    monkeypatch, tmp_path,
):
    """Issue #537: no commit but uncommitted leftovers is neither a pure
    ops delivery nor a code delivery — the deterministic closeout's
    fail-fast (never commit uncommitted changes) applies."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty=" M a.txt\n",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")

    def failing_deliver_pr(*a, **k):
        raise runner.RecoverablePiFailure(
            "the agent left uncommitted changes in the worktree"
        )

    cleanup = Mock()
    original_cleanup = runner.cleanup_task_worktree
    runner.cleanup_task_worktree = cleanup
    monkeypatch.setattr(runner, "deliver_pr", failing_deliver_pr)

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    # Recoverable delivery failures preserve the scene for the next
    # same-run implement session instead of entering ai-blocked.
    assert result == runner.IssueResult("failed", None)
    cleanup.assert_not_called()
    runner.cleanup_task_worktree = original_cleanup
    runner.edit_issue.assert_called_once_with(
        99, repo="o/r", add=runner.IN_PROGRESS_LABEL,
    )


def test_bot_git_identity_overrides_git_config(tmp_path):
    """The startup identity owns a commit even when `-c user.*` is given.

    Git's `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables take precedence over
    `user.*` config, including `-c user.*`, so a Pi session can no longer
    credit its commits to a stranger's account (Issue #1416).
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    runner.set_bot_git_identity(env)
    work = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=x",
         "-c", "user.email=x@example.com", "commit", "--allow-empty",
         "-m", "t"],
        check=True, capture_output=True, env=env,
    )
    identity = subprocess.run(
        ["git", "-C", str(work), "log", "-1",
         "--format=%an <%ae>|%cn <%ce>"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert identity == (
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>|"
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>"
    )


def test_runner_main_pins_bot_git_identity(monkeypatch, tmp_path):
    """A run pins Orbi's identity before any git call or Pi session."""
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    missing = tmp_path / "missing.toml"
    assert runner.main(["--config", str(missing)]) == 1
    assert os.environ["GIT_AUTHOR_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert os.environ["GIT_COMMITTER_NAME"] == "orbi-build[bot]"


def test_parse_paginated_issue_array_rejects_malformed_items():
    with pytest.raises(ValueError, match="non-object item"):
        runner.parse_paginated_issue_array('[[{"number": 1}, "bad"], []]')
    with pytest.raises(ValueError, match="array of arrays"):
        runner.parse_paginated_issue_array("[{}]")


def test_epic_issue_with_blockers_rejects_invalid_details(monkeypatch):
    with pytest.raises(ValueError, match="number is missing"):
        runner.epic_issue_with_blockers("o/r", {"number": "20"})
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(ValueError, match="details are not an object"):
        runner.epic_issue_with_blockers("o/r", {"number": 20})


def test_reconcile_open_epics_closes_and_deduplicates_audit(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return json.dumps([epic]) if command[:3] == ["gh", "issue", "list"] else ""
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "_verify_epic_complete", lambda repo, item: ["Issue #21 closed"])
    comments = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue", lambda number, *, repo, body: comments.append({"body": body}))
    result = claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert result == ["Epic #20 closed after verification (Issue #21 closed)"]
    assert len(comments) == 1 and "run_id=abc12345" in comments[0]["body"]
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands
    # The same audit is idempotent on the next tick.
    commands.clear()
    claim.reconcile_open_epics("o/r", "def67890", "en")
    assert len(comments) == 1
    assert "epic_closed issue=20 repo=o/r" in caplog.text


def test_reconcile_open_epics_posts_the_audit_in_the_bound_language(monkeypatch):
    epic = {"number": 20}

    def fake_run(command, **kwargs):
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    comments = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "_verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    assert len(comments) == 1
    assert "Epic 对账：已完成" in comments[0]["body"]
    assert "Issue #21 closed" in comments[0]["body"]
    assert "run_id=abc12345" in comments[0]["body"]


def test_reconcile_open_epics_keeps_one_audit_across_languages(monkeypatch):
    """A close that failed after today's English audit landed posts no
    second comment when the next sweep renders Chinese; two Chinese
    sweeps after a failed close post one comment in total."""
    epic = {"number": 20}
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    english = ("Epic reconciliation: complete; children: Issue #21 closed; "
               "no open native blockers/dependencies.")
    comments = [{"body": f"<!-- orbi:run=old -->\n{english}\nrun_id=old"}]
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "_verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    claim.reconcile_open_epics("o/r", "def67890", "zh")
    assert len(comments) == 1
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands


def test_reconcile_open_epics_keeps_incomplete_without_comment(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([epic]))
    monkeypatch.setattr(seam, "_verify_epic_complete", lambda repo, item: (_ for _ in ()).throw(ValueError("open blockers: #3")))
    comments = []
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: comments.append(True))
    claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert not comments
    assert "epic_kept_open issue=20 repo=o/r reason=\"open blockers: #3\"" in caplog.text


def test_milestone_reconcile_classified_failures_are_throttled_across_ticks(
    tmp_path, caplog,
):
    caplog.set_level("INFO")
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="Bad credentials (HTTP 401)",
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=1000)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)

    # A later systemd tick is a new Python process, so the filesystem marker —
    # not process memory — must prevent both another request and another log.
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r", now=1100)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    messages = [record.message for record in caplog.records]
    assert sum(message.endswith(
        'milestone_reconcile_auth_failed repo=o/r status=401 '
        'operation=list_milestones stderr="Bad credentials (HTTP 401)"'
    ) for message in messages) == 1
    assert "milestone_reconcile_failed" not in caplog.text


def test_milestone_reconcile_success_clears_suppression(tmp_path, caplog):
    error = github.MilestoneReconcileError(
        status=404, operation="list_milestones", stderr="Not Found (HTTP 404)",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert sum(record.message.endswith(
        'milestone_reconcile_not_found repo=o/r status=404 '
        'operation=list_milestones stderr="Not Found (HTTP 404)"'
    ) for record in caplog.records) == 2


def test_milestone_reconcile_failure_is_retried_after_cooldown(tmp_path):
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="HTTP 401",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    os.utime(marker, (1000, 1000))
    assert not milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS - 1,
    )
    assert milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS,
    )


def test_milestone_reconcile_state_io_failures_stay_fail_open(
    tmp_path, monkeypatch, caplog,
):
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    marker.parent.mkdir(parents=True)
    marker.touch()
    original_stat = Path.stat
    monkeypatch.setattr(
        Path, "stat",
        lambda self: (_ for _ in ()).throw(OSError("stat failed"))
        if self == marker else original_stat(self),
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    monkeypatch.setattr(Path, "stat", original_stat)

    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(OSError("unlink failed"))
        if self == marker else original_unlink(self),
    )
    os.utime(marker, (0, 0))
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=4000)
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert "milestone_reconcile_suppression_clear_failed repo=o/r" in caplog.text
    monkeypatch.setattr(Path, "unlink", original_unlink)

    marker.unlink()
    monkeypatch.setattr(
        os, "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )
    milestone.record_milestone_reconcile_failure(
        tmp_path, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    assert "milestone_reconcile_auth_failed repo=o/r status=401" in caplog.text


def test_verify_epic_complete_uses_native_sub_issue_shapes(monkeypatch):
    base = {"number": 20, "blockedBy": {"nodes": []}}
    monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: {"number": "bad", "blockedBy": {"nodes": []}})
    with pytest.raises(ValueError, match="Epic number is missing"):
        github._verify_epic_complete("o/r", base)
    cases = [
        ({"number": "bad"}, "invalid number"),
        ({"number": 21, "repository": {}, "state": "closed"}, "repository state is malformed"),
        ({"number": 21, "repository": {"full_name": "other/r"}, "state": "closed"}, "cross-repository"),
        ({"number": 21, "repository": {"full_name": "o/r"}}, "state is malformed"),
    ]
    for child, message in cases:
        monkeypatch.setattr(seam, "epic_issue_with_blockers", lambda repo, issue: base)
        monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([[child]]))
        with pytest.raises(ValueError, match=message):
            github._verify_epic_complete("o/r", base)
    pr = {"number": 21, "repository": {"full_name": "o/r"},
          "state": "closed", "pull_request": {}}
    def pr_run(command, **kwargs):
        if "sub_issues?per_page=100" in command[2]:
            return json.dumps([[pr]])
        if command[-1] == "repos/o/r/issues/21":
            return json.dumps({"pull_request": {}, "state": "closed"})
        if command[-1] == "repos/o/r/pulls/21":
            return json.dumps({"merged": False})
        raise AssertionError(command)
    monkeypatch.setattr(seam, "run_command", pr_run)
    with pytest.raises(ValueError, match="not merged"):
        github._verify_epic_complete("o/r", base)
    with pytest.raises(AssertionError):
        pr_run(["x", "y", "z"])


def test_reconcile_orphan_prs_reports_an_open_pr_of_a_closed_issue(monkeypatch, caplog):
    """Issue #746 (direction B): a delivery PR whose source Issue a human
    closed is reported ONCE on the PR — every delivery-state scan reads
    `state=open` Issues only, so nothing else would ever look at that PR
    again. The merge/close decision stays with the human: the Runner
    never auto-merges and never auto-closes across a human's close."""
    caplog.set_level("INFO")
    pr_comments: list[dict] = []
    posted = []

    def fake_run(command, **kwargs):
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: pr_comments)
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda number, *, repo, body: posted.append((number, repo, body)),
    )
    result = claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    assert result == ["PR #9 reported: source Issue #4 is closed"]
    assert len(posted) == 1
    number, repo, body = posted[0]
    assert (number, repo) == (9, "o/r")
    assert "<!-- orbi:orphan-pr -->" in body
    assert "<!-- orbi:run=abc12345 -->" in body
    assert "#4" in body
    assert "orphan_pr_reported pr=9 issue=4 repo=o/r" in caplog.text
    # Idempotent: the marker on the PR means the next tick reports nothing.
    pr_comments.append({"body": posted[0][2]})
    assert claim.reconcile_orphan_prs("o/r", "def67890", hooks=resume_deps(), language_code="en") == []
    assert len(posted) == 1
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_orphan_prs_rejects_malformed_pr_list_and_state(monkeypatch):
    """A non-array `pr list` answer and a malformed Issue state fail fast
    inside the sweep — the per-tick try/except turns that into the
    `orphan_pr_reconcile_failed` bypass log, never a broken delivery."""
    def pr_list_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps({"comments": []})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", pr_list_run)
    with pytest.raises(ValueError, match="pr list must return a JSON array"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        pr_list_run(["unexpected"])

    def weird_state_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command[-1] == "state":
            return json.dumps({"state": "WEIRD"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", weird_state_run)
    with pytest.raises(ValueError, match="state must be OPEN or CLOSED"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        weird_state_run(["unexpected"])


def test_pr_comments_reads_and_validates(monkeypatch):
    """`pr_comments` is the PR-side twin of `issue_comments`: same JSON
    shape contract, same fail-fast on a malformed answer."""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"comments": [{"body": "x"}]}),
    )
    assert github.pr_comments(9, repo="o/r") == [{"body": "x"}]
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: json.dumps([{"comments": []}]),
    )
    with pytest.raises(ValueError, match="pr view must be a JSON object"):
        github.pr_comments(9, repo="o/r")
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps({"comments": "bad"}),
    )
    with pytest.raises(ValueError, match="pr comments must be a JSON array"):
        github.pr_comments(9, repo="o/r")


def test_reconcile_orphan_prs_skips_open_issues_and_non_delivery_branches(monkeypatch):
    """An open source Issue is the normal delivery flow (no report); a
    head branch outside the stable delivery naming (a human or external
    PR) is never the Runner's business — no issue state is even read."""
    read_issues = []

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                "garbage",
                {"number": 9, "headRefName": "orbi/o-r-issue-4"},
                {"number": 10, "headRefName": "feature/human-branch"},
                {"number": 11, "headRefName": None},
            ])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            read_issues.append(4)
            return json.dumps({"state": "OPEN"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "comment_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no comment")))
    assert claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en") == []
    assert read_issues == [4]
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_orphan_prs_failure_is_fail_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a throttled milestone sweep must be skipped")
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "abc12345")
    slot_dir = tmp_path / "slots"
    milestone.record_milestone_reconcile_failure(
        slot_dir.parent, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    result = claim.pick_next_delivery(["o/r"], slot_dir, 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "orphan_pr_reconcile_failed repo=o/r" in caplog.text


@pytest.mark.parametrize("policy_text,expected,unexpected", [
    ('message_language = "en"\n', "Orbi orphan PR:", "Orbi 孤儿 PR"),
    (None, "Orbi 孤儿 PR：", "Orbi orphan PR:"),
])
def test_claim_time_comment_follows_the_repository_language(
        monkeypatch, tmp_path, policy_text, expected, unexpected,
):
    """Issue #1687 acceptance 2: the claim-time comments use the
    repository's merged language, not the host's. A zh host whose
    repository sets `message_language = "en"` posts English comments;
    with no repository file it keeps the host's Chinese."""
    import base64

    def fake_run(command, **kwargs):
        if command == ["gh", "api", "repos/o/r/contents/.github/orbi.toml"]:
            if policy_text is None:
                return json.dumps({})
            return json.dumps({
                "sha": "a" * 40,
                "content": base64.b64encode(
                    policy_text.encode("utf-8"),
                ).decode("ascii"),
            })
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r",
                       "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(f"unexpected command: {command}")

    posted: list[str] = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: [])
    monkeypatch.setattr(
        seam, "comment_pr",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setitem(
        claim.__dict__, "reconcile_open_epics", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.milestone_bookkeeping.__dict__, "milestone_reconcile_due",
        lambda *a, **k: False,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_resumable_delivery", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_in_progress_issue", lambda *a, **k: None,
    )
    monkeypatch.setitem(claim.__dict__, "pick_issue", lambda *a, **k: None)
    monkeypatch.setitem(journal.__dict__, "_CURRENT_RUN_ID", "abc12345")
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], tmp_path / "slots", 1, config=config, hooks=resume_deps(),
    ) is None
    assert len(posted) == 1
    assert expected in posted[0]
    assert unexpected not in posted[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "view", "9"])


def test_reconcile_open_epics_failure_is_fail_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            github.MilestoneReconcileError(
                status=404, operation="list_milestones", stderr="HTTP 404",
            )
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "abc12345")
    result = claim.pick_next_delivery(["o/r"], tmp_path / "slots", 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "epic_reconcile_failed repo=o/r" in caplog.text
    assert "milestone_reconcile_not_found repo=o/r status=404" in caplog.text


def test_reconcile_open_epics_runs_on_a_fresh_tick(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        claim, "reconcile_open_epics",
        lambda repo, run_id, language_code: calls.append(
            (repo, run_id, language_code)),
    )
    monkeypatch.setattr(milestone, "reconcile_release_milestones", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_issue", lambda *args, **kwargs: None)
    assert claim.pick_next_delivery(["o/r"], Path("/tmp/slots"), 1, hooks=resume_deps()) is None
    assert calls == [("o/r", "a1b2c3d4", "en")]
    # A repository's zh message language reaches the Epic sweep (Issue #1705).
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], Path("/tmp/slots"), 1, config=config, hooks=resume_deps(),
    ) is None
    assert calls[-1] == ("o/r", "a1b2c3d4", "zh")


def test_verify_epic_complete_rejects_malformed_native_blockers(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *args, **kwargs: json.dumps({
        "number": 20, "body": "## Children\n- #21",
    }))
    with pytest.raises(ValueError, match="native blocker/dependency state is unavailable"):
        github._verify_epic_complete("o/r", {"number": 20})


def test_verify_epic_complete_rejects_malformed_native_blocker_node():
    with pytest.raises(ValueError, match="native blocker/dependency state is malformed"):
        github._verify_epic_complete("o/r", {
            "number": 20, "body": "## Children\n- #21",
            "blockedBy": {"nodes": [{"state": "OPEN"}]},
        })


def test_epic_child_evidence_validates_issue_and_pr_shapes(monkeypatch):
    responses = {
        "repos/o/r/issues/1": json.dumps({"state": "closed"}),
        "repos/o/r/issues/2": json.dumps({"pull_request": {}, "state": "open"}),
        "repos/o/r/pulls/2": json.dumps({"merged": True}),
    }
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: responses[command[-1]])
    assert github._epic_child_evidence("o/r", "issue", 1) == "Issue #1 closed"
    assert github._epic_child_evidence("o/r", "pr", 2) == "PR #2 merged"
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(ValueError, match="not an object"):
        github._epic_child_evidence("o/r", "issue", 3)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}))
    with pytest.raises(ValueError, match="declared as Issue"):
        github._epic_child_evidence("o/r", "issue", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"state": "closed"}))
    with pytest.raises(ValueError, match="declared as PR"):
        github._epic_child_evidence("o/r", "pr", 2)
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps({"pull_request": {}}) if "issues" in command[-1] else json.dumps({"merged": False}))
    with pytest.raises(ValueError, match="not merged"):
        github._epic_child_evidence("o/r", "pr", 2)


# --- Issue #186: deliver_pr — the Runner owns the deterministic closeout ----

DELIVER_BRANCH = f"orbi/issue-4-{FAKE_RUN_ID}"


def fake_deliver_run(command, **kwargs):
    """Complete fake for deliver_pr: git commands answered, gh returns
    the PR of the task branch (the production `gh pr list` shape)."""
    if command[:3] == ["git", "branch", "--show-current"]:
        return DELIVER_BRANCH
    if command[:3] == ["git", "status", "--porcelain"]:
        return ""
    if command[:3] == ["git", "rev-parse", "HEAD"]:
        return FAKE_HEAD_SHA
    # Issue #1581: the commit-boundary tree comparison. The delivered
    # head tree differs from the frozen base tree, so the happy path has
    # a real file change (a distinct constant keeps the two apart).
    if command[0:3] == ["git", "rev-parse", "HEAD^{tree}"]:
        return FAKE_HEAD_TREE_SHA
    if (command[0:2] == ["git", "rev-parse"]
            and command[2].endswith("^{tree}")):
        return FAKE_BASE_TREE_SHA
    if command[:3] == ["git", "fetch", "origin"]:
        return ""
    if command[:3] == ["git", "merge-base", "--is-ancestor"]:
        return ""
    if command[:3] == ["git", "merge", "origin/main"]:
        return ""
    if command[:3] == ["git", "merge", "--abort"]:
        return ""
    if command[:3] == ["git", "push", "origin"]:
        return ""
    # Issue #898: the pushed head is read from the remote, never from a
    # local remote-tracking ref the fetch refspec may not cover (a
    # `--single-branch` clone).
    answer = remote_head_answer(command, FAKE_HEAD_SHA)
    if answer is not None:
        return answer
    if command[:3] == ["gh", "issue", "view"] and command[-1] == "state":
        # Issue #746: the pre-PR closeout reads the source Issue state;
        # the default scene is an open Issue.
        return json.dumps({"state": "OPEN"})
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
    raise AssertionError(f"unexpected command: {command}")


def test_fake_deliver_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_deliver_run(["gh", "release", "list"])


def _seed_deliver_run_state(worktree, **extra):
    """The claim-time run state file every real delivery carries before
    `deliver_pr` runs (the engine push history records into it, #833)."""
    state = {
        "run_id": FAKE_RUN_ID,
        "issue": 4,
        "repo": "o/r",
        "branch": DELIVER_BRANCH,
        "worktree": str(worktree),
    }
    state.update(extra)
    (worktree / ".orbi").mkdir(parents=True, exist_ok=True)
    (worktree / ".orbi" / "run-state.json").write_text(
        json.dumps(state) + "\n", encoding="utf-8",
    )


def test_deliver_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command[:3] == ["git", "branch", "--show-current"], command
        return "other-branch"

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", FAKE_HEAD_SHA, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_uncommitted_changes(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M leaked.py\n?? junk.txt"
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(runner.RecoverablePiFailure, match="uncommitted changes"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_delivery_without_a_commit(monkeypatch, tmp_path):
    # HEAD is still the frozen base: the agent delivered no commit.
    base_sha = "9" * 40

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return base_sha
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="no commit"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", base_sha, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_an_empty_commit_as_no_file_change(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1581: HEAD advanced past the frozen base, but the delivered
    tree equals the base tree (an empty commit). The commit boundary logs
    delivery_no_file_change and raises before any fetch, push or PR — the
    same terminal path as delivery_no_commit, so the run ends ai-blocked
    with the last commit subject in the reason."""
    base_sha = "9" * 40
    subject = (
        "Blocked: home page (index.html) is committed empty — "
        "no page to measure"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["git", "rev-parse", "HEAD^{tree}"]:
            return FAKE_BASE_TREE_SHA
        if command[0:3] == ["git", "rev-parse", f"{base_sha}^{{tree}}"]:
            return FAKE_BASE_TREE_SHA
        if command[0:4] == ["git", "log", "-1", "--format=%s"]:
            return subject
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="no file change") as failure:
            runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", base_sha, issue_title="t", repo_dir=tmp_path, language_code="en")
    # The reason names both SHAs and quotes the last commit subject.
    assert FAKE_HEAD_SHA in str(failure.value)
    assert base_sha in str(failure.value)
    assert subject in str(failure.value)
    # The event carries the branch/head/base scene.
    assert "delivery_no_file_change" in caplog.text
    assert f"head={FAKE_HEAD_SHA}" in caplog.text
    assert f"base={base_sha}" in caplog.text
    # Nothing was pushed and no PR was touched: the stop is terminal.
    assert not any(
        command[0:2] == ["git", "push"] for command in calls
    )
    assert not any(command[0:2] == ["gh", "pr"] for command in calls)
    assert ["git", "fetch", "origin", "main"] not in calls


def test_deliver_pr_completes_the_closeout(monkeypatch, tmp_path):
    """The happy path: clean commit boundary, base not advanced, plain
    push, the PR of the branch is verified, the URL is returned."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    # The closeout order: commit boundary, locked base fetch, ancestry,
    # plain push, remote-head verification, PR list.
    assert ["git", "status", "--porcelain"] in calls
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls
    assert ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"] in calls
    # Issue #898: the pushed head is resolved with ls-remote, never with
    # `git rev-parse origin/<branch>` — that local ref does not exist in
    # a checkout whose fetch refspec does not cover the pushed branch.
    assert ["git", "ls-remote", "--heads", "origin",
            f"refs/heads/{DELIVER_BRANCH}"] in calls
    # The pushed head is the delivery's first recorded engine-pushed
    # head (Issue #833): the merge record subtracts exactly these.
    assert review_merge.read_pushed_head(tmp_path) == FAKE_HEAD_SHA
    assert any(
        command[:2] == ["gh", "pr"] and command[2] == "list"
        for command in calls
    )
    # The PR already exists (the fake answers pr list with it): the
    # Runner never creates a second PR.
    assert not any(
        command[:3] == ["gh", "pr", "create"] for command in calls
    )
    # No merge: the base did not advance.
    assert not any(
        command[:3] == ["git", "merge", "origin/main"] for command in calls
    )


def test_deliver_pr_rolls_back_a_conflicting_base_absorb(
    monkeypatch, tmp_path, caplog,
):
    """Base advanced and the plain merge conflicts: the Runner aborts
    the merge (the worktree returns to the agent's exact commit
    boundary), logs `base_merge_conflict`, and continues — the PR opens
    on the agent's head and the existing review loop absorbs the base
    in-session (the state machine is unchanged)."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["git", "merge", "origin/main"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="CONFLICT (content): Merge conflict",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    assert ["git", "merge", "origin/main"] in calls
    assert ["git", "merge", "--abort"] in calls
    # The abort happens before the push: the pushed head is the
    # agent's exact commit boundary.
    assert calls.index(["git", "merge", "--abort"]) < calls.index(
        ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"],
    )
    assert "base_merge_conflict" in caplog.text


def test_deliver_pr_absorbs_an_advanced_base(monkeypatch, tmp_path, caplog):
    """Base advanced and the plain merge succeeds: the absorbed base is
    pushed with the delivery (`base_absorbed`), so the PR head contains
    the latest remote base."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") == FAKE_PR_URL
    assert calls.index(["git", "merge", "origin/main"]) < calls.index(
        ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"],
    )
    assert "base_absorbed" in caplog.text


@pytest.mark.parametrize(
    "attribution_footer, attribution_link, expected_footer, foreign_pr",
    [
        (True, "https://github.com/orbi-build/orbi",
         "Built by [Orbi](https://github.com/orbi-build/orbi) from Issue #4",
         False),
        (True, "https://example.com/x",
         "Built by [Orbi](https://example.com/x) from Issue #4", False),
        # Issue #1590: a link carrying its ?ref= query stays byte-identical.
        (True, "https://orbi.build/?ref=prfoot-owner",
         "Built by [Orbi](https://orbi.build/?ref=prfoot-owner)"
         " from Issue #4", False),
        (False, "https://github.com/orbi-build/orbi", None, False),
        (False, "https://github.com/orbi-build/orbi", None, True),
    ],
)
def test_deliver_pr_creates_the_pr_when_absent(
    monkeypatch, tmp_path, attribution_footer, attribution_link,
    expected_footer, foreign_pr,
):
    """No open PR of the branch: the Runner creates it with the run
    marker and `Fixes #<issue>` in the body (the PR body contract is
    the Runner's obligation now, Issue #186)."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    created = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            # Stateful: empty until the Runner creates the PR, then the
            # created PR of the task branch (like the real GitHub state).
            # A fork PR with the same head name is unrelated and must not
            # suppress creation of this Runner-owned PR.
            prs = ([{
                "number": 99,
                "url": "https://github.com/fork/repo/pull/99",
                "isCrossRepository": True,
            }] if foreign_pr else [])
            if created:
                prs.append(created[0])
            return json.dumps(prs)
        if command[:2] == ["gh", "pr"] and command[2] == "create":
            created.append({
                "url": FAKE_PR_URL,
                "baseRefName": "main",
                "headRefName": DELIVER_BRANCH,
                "headRefOid": FAKE_HEAD_SHA,
                "body": command[command.index("--body") + 1],
            })
            return FAKE_PR_URL
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="Closeout title", repo_dir=tmp_path, attribution_footer=attribution_footer, attribution_link=attribution_link, language_code="en") == FAKE_PR_URL
    create = [
        command for command in calls
        if command[:3] == ["gh", "pr", "create"]
    ]
    assert len(create) == 1
    command = create[0]
    assert command[command.index("--base") + 1] == "main"
    assert command[command.index("--head") + 1] == DELIVER_BRANCH
    assert command[command.index("--title") + 1] == "Closeout title"
    body = command[command.index("--body") + 1]
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert "Fixes #4" in body
    if expected_footer is None:
        assert "Built by Orbi" not in body
    else:
        assert expected_footer in body
    if "?ref=" not in attribution_link:
        assert "orbi.build" not in body
        assert "?ref=" not in body


def test_deliver_pr_fails_fast_when_pr_create_fails(monkeypatch, tmp_path):
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            return "[]"
        if command[:2] == ["gh", "pr"] and command[2] == "create":
            raise subprocess.CalledProcessError(
                1, command, stderr="no matching pull request",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_rejects_remote_head_mismatch_after_push(
    monkeypatch, tmp_path,
):
    def fake_run(command, **kwargs):
        # Issue #898: the remote head is read with ls-remote.
        answer = remote_head_answer(command, "f" * 40)
        if answer is not None:
            return answer
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="remote head"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_deliver_pr_verifies_the_pr_with_the_latest_base_check_skipped(
    monkeypatch, tmp_path,
):
    """deliver_pr just fetched and merged the base itself, so the
    verify step must not fetch it again (require_latest_base=False):
    no second `git fetch` after the push, and a delivery that is behind
    the base (the conflict-rollback scene) still passes verification."""
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["git", "merge", "origin/main"]:
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    fetches = [
        index for index, command in enumerate(calls)
        if command[:3] == ["git", "fetch", "origin"]
    ]
    push = calls.index(["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"])
    # Exactly one fetch (the deliver fetch); nothing after the push.
    assert len(fetches) == 1
    assert fetches[0] < push


def test_deliver_pr_reports_a_closed_issue_and_skips_the_pr(
    monkeypatch, tmp_path, caplog,
):
    """Issue #746 (direction A): the Issue was closed while the delivery
    was in flight — the pushed branch keeps the work, the closed Issue
    gets one explanatory comment, and NO PR is created (nothing dangles
    behind a closed Issue)."""
    caplog.set_level("INFO")
    calls = []
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "view"] and command[-1] == "state":
            return json.dumps({"state": "CLOSED"})
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    comments = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append((number, repo, body)),
    )
    monkeypatch.setattr(
        runner, "verify_pr",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("verify_pr must not run for a closed Issue")),
    )
    assert runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en") is None
    # The Issue state is read AFTER the push: the work stays on origin.
    assert calls.index(
        ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"],
    ) > calls.index(["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"])
    assert not any(command[:2] == ["gh", "pr"] for command in calls)
    assert len(comments) == 1
    number, repo, body = comments[0]
    assert (number, repo) == (4, "o/r")
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert DELIVER_BRANCH in body
    assert "delivery_issue_closed branch=" in caplog.text


def test_process_issue_closed_issue_ends_without_pr_ceremony(
    monkeypatch, tmp_path, caplog,
):
    """Issue #746: `deliver_pr` returns None for an Issue closed during
    delivery — no `ai-pr-opened` label patch, no `Orbi opened PR:` scene
    comment; the run ends `issue_closed` and the result kind ends the
    tick before the delivery wait."""
    monkeypatch.setattr(seam, "freeze_base",
                        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: None)
    edits: list[tuple] = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    comments = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append(body),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #266: the health record is a pure bypass — a failed record
    # never changes the issue-closed outcome (the except branch runs).
    def dead_health(*args, **kwargs):
        raise RuntimeError("health state unwritable")

    monkeypatch.setattr(runner.runner_health, "record_run_attempt", dead_health)
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    assert result == ("issue-closed", None)
    assert "health_success_record_failed" in caplog.text
    assert not any(
        "ai-pr-opened" in str(args) + str(kwargs) for args, kwargs in edits
    ), "no ai-pr-opened label patch for a closed Issue"
    assert not any("Orbi opened PR" in body for body in comments)
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "result=issue_closed" in ends[0]
    assert "pr=-" in ends[0]


# --- Issue #256: Runner-owned runtime state isolation ----------------------


def _make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real git repo plus one linked task worktree (the exact shape
    `create_worktree` produces: `.git` is a pointer file)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "base"],
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "task",
         str(wt), "HEAD"],
        check=True,
    )
    return repo, wt


def test_exclude_path_resolves_the_common_gitdir(tmp_path):
    repo, wt = _make_linked_worktree(tmp_path)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    assert exclude.name == "exclude"
    assert exclude.parts[-2] == "info"
    # Git applies the exclude of the COMMON gitdir to every worktree of
    # the repo (the worktree-specific gitdir carries none of its own):
    # the exclude is the shared repo's `.git/info/exclude`, never inside
    # the worktree checkout.
    assert exclude == repo / ".git" / "info" / "exclude"
    assert not exclude.is_relative_to(wt)


def test_apply_runner_runtime_excludes_writes_all_patterns(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    pi_session.apply_runner_runtime_excludes(wt)
    lines = pi_session.runner_runtime_exclude_path(wt).read_text(
        encoding="utf-8",
    ).splitlines()
    for pattern in pi_session.RUNNER_RUNTIME_EXCLUDES:
        assert pattern in lines


def test_apply_runner_runtime_excludes_is_idempotent(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    pi_session.apply_runner_runtime_excludes(wt)
    first = pi_session.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    pi_session.apply_runner_runtime_excludes(wt)
    second = pi_session.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    assert first == second
    lines = second.splitlines()
    assert len(lines) == len(set(lines)), "patterns must not be duplicated"


def test_apply_runner_runtime_excludes_preserves_user_content(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# my custom exclude\nsecrets-local/\n", encoding="utf-8")
    pi_session.apply_runner_runtime_excludes(wt)
    text = exclude.read_text(encoding="utf-8")
    assert "# my custom exclude" in text
    assert "secrets-local/" in text
    assert ".orbi/" in text
    assert ".pi-session/" in text


def test_apply_runner_runtime_excludes_without_git_is_a_noop(tmp_path):
    wt = tmp_path / "not-a-worktree"
    wt.mkdir()
    pi_session.apply_runner_runtime_excludes(wt)  # must not raise
    assert not (wt / ".git").exists()
    assert not list(wt.iterdir())


def test_run_pi_applies_runner_runtime_excludes_before_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    applied: list[Path] = []
    monkeypatch.setattr(
        pi_session, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(
        {"number": 5, "title": "t", "body": "b"},
        runner.RunContext(
            run_id=config.run_id, issue=5, branch="orbi/owner-repo-issue-5",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        config,
    )
    assert applied == [tmp_path]
    # Issue #302: the run artifact dir exists before the session — the
    # contract commands write `.orbi/plan.md` / `.orbi/test.log` into it.
    assert (tmp_path / ".orbi").is_dir()


def test_run_review_applies_runner_runtime_excludes_before_pi(
    monkeypatch, tmp_path,
):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW", encoding="utf-8")
    applied: list[Path] = []
    monkeypatch.setattr(
        pi_session, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "ok")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    assert applied == [tmp_path]
    # Issue #302: the review session reads/writes the same .orbi/ run
    # artifacts — the run dir exists here too.
    assert (tmp_path / ".orbi").is_dir()


def test_deliver_pr_repairs_runner_runtime_leftovers(monkeypatch, tmp_path,
                                                     caplog):
    """The #246 scene: the task renamed the tracked .gitignore away from
    `.orbi/`, but the parent Runner still created it. The delivery
    applies the local exclude, repairs, logs
    `runner_runtime_exclude_repaired`, and continues — no
    `delivery_uncommitted_changes`."""
    status_calls = {"n": 0}
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            # First check: the Runner's own state looks untracked. After
            # the exclude repair the re-check is clean.
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    assert url == FAKE_PR_URL
    assert "runner_runtime_exclude_repaired" in caplog.text
    assert "delivery_uncommitted_changes" not in caplog.text
    assert status_calls["n"] == 2


def test_deliver_pr_repairs_the_renamed_state_dir_too(monkeypatch, tmp_path,
                                                      caplog):
    """Migration window: the renamed state dir `.orbi/` is Runner-owned
    as well and must be repaired, not failed."""
    status_calls = {"n": 0}
    _seed_deliver_run_state(tmp_path)

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")
    assert url == FAKE_PR_URL
    assert "runner_runtime_exclude_repaired" in caplog.text


def test_deliver_pr_still_fails_on_agent_leftovers_alongside_runner_state(
    monkeypatch, tmp_path,
):
    """Runner state plus a REAL agent leftover: the repair must not mask
    the agent's uncommitted code — `delivery_uncommitted_changes` stands."""

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M src/app.py\n?? .orbi/\n?? foo.py\n"
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        runner.deliver_pr(RunContext(run_id=FAKE_RUN_ID, issue=4, branch=DELIVER_BRANCH, worktree=tmp_path, source_repo="o/r"), "main", "9" * 40, issue_title="t", repo_dir=tmp_path, language_code="en")


def test_cleanup_task_worktree_removes_the_scene_and_prunes(tmp_path):
    repo, wt = _make_linked_worktree(tmp_path)
    (wt / ".orbi").mkdir(parents=True)
    (wt / ".pi-session").mkdir(parents=True)
    runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=wt, source_repo="owner/repo"), repo)
    assert not wt.exists()
    listing = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(wt) not in listing


def test_cleanup_task_worktree_logs_failure_and_keeps_the_scene(
    monkeypatch, tmp_path, caplog,
):
    wt = tmp_path / "wt"
    wt.mkdir()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner.shutil, "rmtree", boom)
    with caplog.at_level(logging.ERROR, logger="orbi.bootstrap"):
        runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=wt, source_repo="owner/repo"), tmp_path)
    assert "worktree_cleanup_failed" in caplog.text
    assert wt.exists(), "a failed cleanup must never claim the scene is gone"


def test_exclude_path_rejects_a_pointer_without_gitdir(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").parent.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("# not a real pointer\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no gitdir entry"):
        pi_session.runner_runtime_exclude_path(wt)


def test_runner_runtime_only_accepts_quoted_runner_paths():
    # Porcelain quotes paths with special characters; the runner paths
    # are plain ASCII, so the unquoted match is exact.
    assert pi_session._is_runner_runtime_only('?? ".orbi/"\n')
    assert pi_session._is_runner_runtime_only('?? ".pi-session/"\n')
    # Issue #302: the orbi contract artifact set (pi-loop state, per-run
    # plan/test/verify artifacts) is runner-owned too — a porcelain entry
    # carrying only them must keep the repair path open, never the fail
    # fast.
    assert pi_session._is_runner_runtime_only('?? ".pi/"\n')
    assert pi_session._is_runner_runtime_only("?? plan.md\n")
    assert pi_session._is_runner_runtime_only("?? test.log\n")
    assert pi_session._is_runner_runtime_only("?? verify.md\n")
    # An agent-owned leftover is NEVER runner-owned, mixed or alone.
    assert pi_session._is_runner_runtime_only("?? notes.md\n") is False
    assert pi_session._is_runner_runtime_only("?? unexpected.py\n") is False
    assert pi_session._is_runner_runtime_only("?? plan.md\n?? notes.md\n") is False


def test_exclude_path_for_a_plain_checkout_uses_its_own_git_dir(tmp_path):
    # A worktree whose `.git` is a DIRECTORY (a plain checkout, not a
    # linked worktree): the exclude is its own `.git/info/exclude`.
    wt = tmp_path / "checkout"
    (wt / ".git").mkdir(parents=True)
    exclude = pi_session.runner_runtime_exclude_path(wt)
    assert exclude == wt / ".git" / "info" / "exclude"


def test_runner_runtime_only_rejects_an_empty_status():
    # An empty status is not "runner-only" — the repair must not fire on
    # a clean worktree.
    assert pi_session._is_runner_runtime_only("") is False
    assert pi_session._is_runner_runtime_only("\n") is False


def test_cleanup_task_worktree_prunes_without_a_scene(tmp_path):
    # The worktree directory is already gone (a partial failure): the
    # cleanup still prunes the git metadata and logs success.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    missing = tmp_path / "already-gone"
    runner.cleanup_task_worktree(RunContext(run_id="abc12345", issue=4, branch="orbi/issue", worktree=missing, source_repo="owner/repo"), repo)
    assert not missing.exists()


def test_apply_runner_runtime_excludes_creates_a_missing_exclude_file(
    tmp_path,
):
    # The common gitdir exists but has no info/exclude yet: the helper
    # creates the file with exactly the runner-owned patterns.
    repo, wt = _make_linked_worktree(tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    # `git init` ships a default exclude file (comments only); remove it
    # so this drives the file-creation branch.
    exclude.unlink()
    pi_session.apply_runner_runtime_excludes(wt)
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines == list(pi_session.RUNNER_RUNTIME_EXCLUDES)


def test_runtime_excludes_exempt_the_orbi_contract_artifacts(tmp_path):
    """Issue #302 acceptance: the four historical dirty-gate scenes are
    exempted by the RUNTIME local exclude alone — with NO tracked
    .gitignore at all (the #246 renamed/broken .gitignore scene), every
    orbi-owned artifact is invisible to `git status --porcelain` while
    real agent leftovers still fail the gate. No new .gitignore entry
    is needed for any of them."""
    repo, wt = _make_linked_worktree(tmp_path)
    # The tracked .gitignore is gone entirely: the exemption must not
    # depend on a file a task can legally rename or break.
    assert not (wt / ".gitignore").exists()
    pi_session.apply_runner_runtime_excludes(wt)
    # Scene #215: pi-loop state written at session shutdown.
    (wt / ".pi").mkdir()
    (wt / ".pi" / "loops.json").write_text("{}\n", encoding="utf-8")
    # Scene #256: runner runtime state (the brand dir and the pi
    # session dir).
    (wt / ".orbi").mkdir()
    (wt / ".orbi" / "run-state.json").write_text("{}\n", encoding="utf-8")
    (wt / ".pi-session").mkdir()
    (wt / ".pi-session" / "s.jsonl").write_text("{}\n", encoding="utf-8")
    # The per-run contract artifacts (the pre-#302 prompt wrote them at
    # the worktree root) and the coverage gate output in the run dir.
    for name in ("plan.md", "test.log", "verify.md"):
        (wt / name).write_text("artifact\n", encoding="utf-8")
    (wt / ".orbi" / "coverage.json").write_text(
        '{"totals": {}}\n', encoding="utf-8",
    )
    (wt / ".orbi" / "test.log").write_text("156 passed\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout == ""
    # The gate is NOT weakened: real agent leftovers are still reported.
    (wt / "unexpected.py").write_text("x = 1\n", encoding="utf-8")
    (wt / "notes.md").write_text("notes\n", encoding="utf-8")
    lines = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "?? unexpected.py" in lines
    assert "?? notes.md" in lines


def test_runtime_excludes_never_hide_a_modified_tracked_file(tmp_path):
    # Issue #302 reverse verification: the runtime exclude only ever
    # hides UNTRACKED orbi artifacts — a modified tracked source file is
    # never exempted (excludes cannot hide tracked changes), so the
    # delivery gate still fails fast on it.
    repo, wt = _make_linked_worktree(tmp_path)
    tracked = wt / "src.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "src.py"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "src"], check=True,
    )
    pi_session.apply_runner_runtime_excludes(wt)
    tracked.write_text("x = 2\n", encoding="utf-8")
    (wt / "plan.md").write_text("artifact\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout == " M src.py\n"


def test_load_config_allows_missing_provider_file_for_setup_diagnostics(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'pi_providers = "missing.json"\n', encoding="utf-8",
    )
    config = config_domain.load_config(config_path, allow_missing_pi_providers=True)
    assert config.pi_providers_data is None
    assert config.pi_provider_key_finding["state"] == "file missing"


def test_load_config_does_not_treat_provider_directory_as_missing(tmp_path):
    config_path = tmp_path / "orbi.toml"
    (tmp_path / "providers").mkdir()
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'pi_providers = "providers"\n', encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        config_domain.load_config(config_path, allow_missing_pi_providers=True)


def _run_failed_log_sites(source: str) -> list[int]:
    """Scan one Python source; return the line numbers of the
    `run_failed` emission sites — the Issue #791 single-emission form
    `event("run_failed ...")` and the legacy `LOGGER.error("run_failed
    ...")` form alike."""
    sites: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and (first.value == "run_failed"
                     or first.value.startswith("run_failed "))):
            continue
        func = node.func
        is_event = isinstance(func, ast.Name) and func.id == "event"
        is_error = isinstance(func, ast.Attribute) and func.attr == "error"
        if is_event or is_error:
            sites.append(node.lineno)
    return sites


def test_run_failed_pin_detects_every_error_site():
    """The detector really detects: two `run_failed` ERROR sites produce
    two line numbers and a non-ERROR sibling is ignored."""
    source = (
        "event(\n"
        "    'run_failed',\n"
        "    format_run_scene(activity),\n"
        "    level=logging.ERROR, reason='a',\n"
        ")\n"
        "LOGGER.info('run_failed (historic)', s)\n"
        "LOGGER.error('run_failed %s reason=b', s)\n"
    )
    assert _run_failed_log_sites(source) == [1, 7]


def test_run_failed_scene_logged_in_one_place():
    """Issue #292: the `run_failed` skeleton (the six-argument
    `format_run_scene` + reason + raise) lives in exactly ONE place — the
    local `_fail_run` helper in `stream_pi`; each terminal branch computes
    its reason and exception and calls it once. A second literal
    `LOGGER.error("run_failed ...` site is the duplicated debt this pin
    rejects (a new log field would have to be edited per branch again)."""
    sites = _run_failed_log_sites(
        Path(pi_process.__file__).read_text(encoding="utf-8")
    )
    assert len(sites) == 1, (
        "the run_failed scene must be logged in exactly one place (the "
        f"_fail_run helper, Issue #292); sites at lines: {sites}"
    )


def test_pick_resumable_routes_marker_ticket_instead_of_blocking(
    monkeypatch, tmp_path,
):
    """Issue #726 gap 1：triage workflow 给被外部 PR 关联的票贴了
    ai-pr-opened，resumable 扫描捡到它却找不到 runner 场景评论——旧码
    直接烧成 ai-blocked，而接管入口对这些票永不可达。带 external 标记
    且 PR 仍 OPEN 的票必须转投：EVENT_REQUEUE 回 ready 队列，下一个
    fresh claim 的接管探针接手评审。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    patches = []
    monkeypatch.setattr(seam, "apply_label_patch",
        lambda number, *, repo, event, current_labels:
            patches.append(event),
    )
    comments_posted = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments_posted.append(body),
    )

    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix the thing",
             "labels": [{"name": "ai-pr-opened"}]}

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "OPEN"})
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert patches == [runner.EVENT_REQUEUE]
    assert any("external contribution PR #55" in body
               for body in comments_posted)
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_pick_resumable_closes_marker_ticket_when_pr_already_merged(
    monkeypatch, tmp_path,
):
    """Issue #726 gap 1 的合并分支：标记票的外部 PR 已被人工合并——
    修复已交付，triage 票按接管合并的同款簿记关票，而不是 block、
    也不是重做。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    closes = []
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix the thing",
             "labels": [{"name": "ai-pr-opened"}]}

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        if command[:3] == ["gh", "pr", "view"]:
            return closes.append(command) or json.dumps({"state": "MERGED"})
        if command[:3] == ["gh", "issue", "close"]:
            return closes.append(command) or ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert any(c[:3] == ["gh", "issue", "close"] for c in closes)
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_delivery_step_never_closes_triage_issue_after_review(
    monkeypatch, tmp_path,
):
    """Issue #842 (D2)：外部接管的评审不再自动合并——clean verdict 在
    `review_and_merge_if_clean` 内部停在分诊态（ai-blocked，评审结论
    留在 Issue 上），`delivery_step` 因此永不因评审结束去关 triage 票
    （旧的 auto-merge 关票分支已随自动合并一起移除）；关票只发生在
    轮询发现人工已合并（MERGED）的分支。"""
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": [
                    {
                        "body": (
                            "<!-- orbi:run=a1b2c3d4 -->\n"
                            "Orbi opened PR: "
                            f"{PR_URL} (base_branch=main "
                            "base_sha=abc123def456 run_id=a1b2c3d4)"
                        ),
                        "authorAssociation": "OWNER",
                    },
                ]})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        if command[:3] == ["gh", "issue", "close"]:
            closes.append(command)
            return ""
        if command[:3] == ["gh", "issue", "edit"]:
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        if command[:2] == ["gh", "api"]:
            return ""
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    closes: list = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    # False is the D2 triage stop's return (the round ends without a
    # merge — the ticket is now the maintainer's).
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: False,
    )
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo", external_takeover=True,
    )
    assert closes == [], (
        "the triage Issue must NOT be closed by the review round — "
        "the engine never merges an external PR (Issue #842 D2)"
    )
    # The answered arms return their canned values; anything unmodeled
    # fails loud.
    assert fake_run(["gh", "issue", "edit", "1", "--repo", "o/r"]) == ""
    assert fake_run(["gh", "api", "x"]) == ""
    assert fake_run(
        ["git", "branch", "--show-current"],
    ) == "orbi/owner-repo-issue-39"
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_route_external_pr_probe_failure_falls_through_to_block(
    monkeypatch, tmp_path,
):
    """Issue #726/#786：接管探针自身失败（gh 异常）时绝不瞎猜路由——
    落回 missing-scene 分支自己的 block 路径，让失败被看见。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue])
        if command[:3] == ["gh", "issue", "list"]
        else (_ for _ in ()).throw(RuntimeError("gh down")),
    )
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix",
             "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((100,), {"repo": "owner/repo",
                               "add": "ai-blocked",
                               "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]


def test_route_external_pr_unknown_state_falls_through_to_block(
    monkeypatch, tmp_path,
):
    """Issue #726：PR 状态是意外值（非 OPEN/MERGED/CLOSED）时同样落回
    block 路径——路由器只承诺三种已知世界。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([issue])
        if command[:3] == ["gh", "issue", "list"] else (
        json.dumps({"state": "DRAFT"})
        if command[:3] == ["gh", "pr", "view"] else (_ for _ in ()).throw(
            AssertionError(f"unexpected command: {command}"))),
    )
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 100, "title": "triage", "state": "OPEN",
             "body": "<!-- orbi:external-pr:55 -->\nfix",
             "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((100,), {"repo": "owner/repo",
                               "add": "ai-blocked",
                               "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]


def test_route_external_pr_ignores_tickets_without_marker(
    monkeypatch, tmp_path,
):
    """Issue #726 的边界：body 没有外部标记的普通无现场票照走 block
    路径——路由器零介入（连 pr view 都不发生）。"""
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([issue])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    issue = {"number": 41, "title": "dev", "state": "OPEN",
             "body": "plain ticket", "labels": [{"name": "ai-pr-opened"}]}
    result = claim.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
        hooks=resume_deps(), language_code="en",
    )
    assert result is None
    assert edits == [((41,), {"repo": "owner/repo",
                              "add": "ai-blocked",
                              "remove": "ai-fix-needed"})]
    assert "no trusted 'Orbi opened PR'" in posted[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_delivery_step_closes_triage_issue_on_merged_poll(
    monkeypatch, tmp_path,
):
    """Issue #726 的既有行为钉子：轮询直接发现 PR 已 MERGED（重启后的
    首询）且是外部接管时，triage 票被关闭——:8023 的旁路调用在旧码里
    从未有测试覆盖。"""
    states = ["MERGED"]

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": states[0]})
        if command[:3] == ["gh", "issue", "comment"]:
            return ""
        if command[:3] == ["gh", "issue", "close"]:
            closes.append(command)
            return ""
        raise AssertionError(f"unexpected command: {command}")

    closes: list = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    issue = {"number": 39, "title": "task", "body": ""}
    runner.delivery_step(
        PR_URL, issue, config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main"),
        "owner/repo", external_takeover=True,
    )
    assert len(closes) == 1
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])


def test_close_external_triage_issue_swallows_close_failure(
    monkeypatch, caplog, tmp_path,
):
    """关票是已合并事实的簿记旁路：失败只记日志，永不改写合并事实、
    永不炸 tick（#608 语义，#726 抽取后必须保持）。"""
    monkeypatch.setattr(seam, "comment_issue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh down")),
    )
    with caplog.at_level("ERROR"):
        runner._close_external_triage_issue(
            39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
            "a1b2c3d4", language_code="en",
        )
    assert "external_takeover_close_failed" in caplog.text


def test_external_triage_comments_follow_the_config_language(monkeypatch):
    """Issue #1687 item 3: the closed/merged external-PR comments were
    English-only literals; they render in the bound message language now."""
    comments: list[str] = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append(body))
    monkeypatch.setattr(
        seam, "comment_pr",
        lambda number, *, repo, body: comments.append(body),
    )
    monkeypatch.setitem(runner.__dict__, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setitem(runner.__dict__, "issue_labels", lambda *a, **k: [])
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "")

    runner._requeue_closed_external_takeover(
        39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
        "a1b2c3d4", language_code="zh",
    )
    assert "Orbi：外部 PR" in comments[0]
    assert "未合并就被关闭" in comments[0]
    assert "run_id=a1b2c3d4" in comments[0]

    runner._close_external_triage_issue(
        39, "owner/repo", PR_URL, "<!-- orbi:external-pr:55 -->",
        "a1b2c3d4", language_code="zh",
    )
    assert "Orbi 已合并外部 PR" in comments[-1]
def test_process_issue_yields_when_the_label_lands_in_the_scan_window(
    monkeypatch, tmp_path,
):
    """Issue #724：认领竞态窗口的前半段——扫描快照还没有 ai-in-progress、
    首次直读却已经看到 = 另一个实例就在本 tick 刚认领。此时必须让路，
    绝不能复用活 run 的 worktree/run_id 启动第二个 Pi（或降级开双交付）。
    真孤儿（标签在快照和直读里都有）照旧走 resume，不受此守卫影响。"""
    make_claim_race_gh(monkeypatch, {"in_progress": True})
    _claim_race_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [
        (tmp_path / "slot-0", 424242)])
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("must not start a second Pi over a live run")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def test_process_issue_scan_window_orphan_resumes_when_no_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #724 的另一半（#18/#668 契约的当代钉子）：同样的滞后快照
    形态，但没有任何其他活 runner 持槽 = 死 runner 留下的孤儿——必须照
    旧 resume（复用 run_id 进 run_pi），绝不能误让路把重启恢复拖慢
    一拍。"""
    make_claim_race_gh(monkeypatch, {"in_progress": True})
    _claim_race_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [
        (tmp_path / "slot-0", None), (tmp_path / "slot-1", os.getpid())])
    monkeypatch.setattr(seam, "apply_label_patch", Mock())
    monkeypatch.setattr(seam, "create_worktree", Mock(
        side_effect=AssertionError("orphan must resume")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)
    result = runner.process_issue(issue, config, "owner/repo")
    # 交付失败的收尾细节由既有 resume 测试族钉住；这里只钉"没误让路"。
    assert result != runner.IssueResult("claim-yielded", None)


def test_process_issue_claim_yield_guard_honors_custom_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #724：让路守卫的标签谓词必须用仓库的 dispatch_label（#527），
    而不是写死的 ai-ready——自定义标签仓库的票永远不带 ai-ready，守卫
    在那里整段失效，#658 竞态原样复活。"""
    state = {"in_progress": False}
    make_claim_race_gh(monkeypatch, state)
    _claim_race_deps(
        monkeypatch, tmp_path,
        freeze_side_effect=lambda: state.__setitem__("in_progress", True),
    )
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("must not claim behind a live claimant")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "repo-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", dispatch_label="repo-ready")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def test_main_idle_passes_the_fused_message_language_to_milestone_bookkeeping(
    monkeypatch, tmp_path,
):
    """Issue #1704: the idle confirmation path renders in the repository's
    fused message language (the repo policy wins over the host value), the
    same object that carries the fused base branch."""
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config('message_language = "zh"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs) or ("open", None),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n'
        'message_language = "en"\n',
        encoding="utf-8",
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["language_code"] == "zh"
