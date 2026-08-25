import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner


def test_parse_issue_list_returns_first_issue():
    issue = {"number": 7, "title": "ship", "body": "do it"}
    assert runner.parse_issue_list(json.dumps([issue])) == issue


def test_parse_issue_list_returns_none_for_empty_list():
    assert runner.parse_issue_list("[]") is None


def test_parse_issue_list_rejects_non_list():
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.parse_issue_list("{}")


def test_load_config_resolves_relative_paths_and_values(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text(
        """source_repos = [\"owner/pilot\", \"owner/backlog\"]\nrepo_dir = \"repo\"\nworkspace_root = \"..\"\nprompt = \"prompt.md\"\nskills = [\"skill.md\"]\ncontext_files = [\"context.md\"]\n""",
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["source_repos"] == ["owner/pilot", "owner/backlog"]
    assert config["repo_dir"] == (tmp_path / "repo").resolve()
    assert config["workspace_root"] == tmp_path.parent.resolve()
    assert config["prompt"] == (tmp_path / "prompt.md").resolve()
    assert config["skills"] == [(tmp_path / "skill.md").resolve()]
    assert config["context_files"] == [(tmp_path / "context.md").resolve()]


def test_load_config_requires_source_repos(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text("prompt = \"prompt.md\"\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must be a non-empty list"):
        runner.load_config(config_path)


def test_load_config_rejects_empty_source_repo_name(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text('source_repos = ["owner/repo", ""]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must contain non-empty strings"):
        runner.load_config(config_path)


def test_load_config_defaults_base_branch_to_main(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["base_branch"] == "main"


def test_load_config_reads_explicit_base_branch(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = "develop"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["base_branch"] == "develop"


def test_load_config_rejects_empty_base_branch(tmp_path):
    config_path = tmp_path / "muyan-pilot.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = ""\n', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="base_branch must be a non-empty string"):
        runner.load_config(config_path)


def test_render_prompt_replaces_context_values():
    rendered = runner.render_prompt(
        "{{SOURCE_REPO}} #{{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{CONTEXT_FILES}}",
        {
            "SOURCE_REPO": "owner/repo",
            "ISSUE_NUMBER": "3",
            "ISSUE_TITLE": "Fix",
            "CONTEXT_FILES": "context.md",
        },
    )
    assert rendered == "owner/repo #3 Fix context.md"


def test_validate_config_accepts_existing_files(tmp_path):
    prompt = tmp_path / "prompt.md"
    skill = tmp_path / "skill.md"
    context = tmp_path / "context.md"
    for path in (prompt, skill, context):
        path.write_text("ok", encoding="utf-8")
    runner.validate_config({
        "repo_dir": tmp_path,
        "prompt": prompt,
        "skills": [skill],
        "context_files": [context],
    })


def test_validate_config_fails_before_issue_claim_when_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.md"):
        runner.validate_config({
            "repo_dir": tmp_path,
            "prompt": tmp_path / "missing.md",
            "skills": [],
            "context_files": [],
        })


def test_validate_config_rejects_missing_repo_dir(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing-repo"):
        runner.validate_config({
            "repo_dir": tmp_path / "missing-repo",
            "prompt": tmp_path / "prompt.md",
            "skills": [],
            "context_files": [],
        })


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


def test_pick_issue_uses_github_queue(monkeypatch):
    issue = {"number": 9, "title": "task", "body": "body"}
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([issue])

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/muyan-ceo") == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
        "--state", "open", "--search",
        "label:ai-ready -label:ai-in-progress -label:ai-pr-opened -label:ai-blocked",
        "--json", "number,title,body", "--limit", "1",
    ]]


def test_pick_next_issue_returns_first_ready_source(monkeypatch):
    issue = {"number": 1, "title": "pilot", "body": ""}
    calls = []

    def pick(repo):
        calls.append(repo)
        return issue if repo == "xqliu/muyan-ceo" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) == (
        "xqliu/muyan-ceo", issue,
    )
    assert calls == ["xqliu/muyan-ceo"]


def test_pick_next_issue_falls_through_to_second_source(monkeypatch):
    issue = {"number": 2, "title": "pilot", "body": ""}
    calls = []

    def pick(repo):
        calls.append(repo)
        return issue if repo == "xqliu/muyan-pilot" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) == (
        "xqliu/muyan-pilot", issue,
    )
    assert calls == ["xqliu/muyan-ceo", "xqliu/muyan-pilot"]


def test_pick_next_issue_returns_none_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(runner, "pick_issue", lambda repo: None)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/muyan-pilot"]) is None


def test_edit_issue_builds_add_and_remove_command(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/muyan-ceo", add="ai-in-progress", remove="ai-ready")
    assert calls == [[
        "gh", "issue", "edit", "3", "--repo", "xqliu/muyan-ceo",
        "--add-label", "ai-in-progress", "--remove-label", "ai-ready",
    ]]


def test_edit_issue_allows_no_label_change(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.edit_issue(3, repo="xqliu/muyan-ceo")
    assert calls == [["gh", "issue", "edit", "3", "--repo", "xqliu/muyan-ceo"]]


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
    assert runner.run_marker("e07383c2") == "<!-- muyan-pilot:run=e07383c2 -->"


def test_run_marker_rejects_missing_or_invalid_run_id():
    for bad in ("", "run1", None):
        with pytest.raises(ValueError, match="invalid run id"):
            runner.run_marker(bad)


def test_set_run_id_binds_the_attempt_and_current_run_id_reads_it(monkeypatch):
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
    assert runner.current_run_id() is None
    runner.set_run_id("e07383c2")
    assert runner.current_run_id() == "e07383c2"


def test_set_run_id_fails_fast_on_invalid_run_id(monkeypatch):
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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
        runner._CURRENT_RUN_ID = None


def test_run_id_filter_leaves_messages_untouched_without_bound_run(
    monkeypatch, caplog,
):
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
    with caplog.at_level("INFO"):
        runner.LOGGER.info("hello")
    assert caplog.messages[-1] == "hello"


def test_freeze_base_fetches_remote_and_returns_exact_sha(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return "abc123def456"

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert calls == [(
        ["git", "fetch", "origin", "main"], {"cwd": tmp_path},
    ), (
        ["git", "rev-parse", "origin/main"], {"cwd": tmp_path},
    )]


def test_freeze_base_fails_fast_when_remote_base_is_missing(monkeypatch, tmp_path):
    error = subprocess.CalledProcessError(
        128, ["git", "rev-parse", "origin/main"],
        stderr="fatal: ambiguous argument",
    )
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(subprocess.CalledProcessError):
        runner.freeze_base(tmp_path, "main")


def test_create_worktree_rejects_existing_path(tmp_path):
    existing = tmp_path / ".worktrees" / "muyan-pilot-owner-repo-issue-3-run1"
    existing.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="worktree path already exists"):
        runner.create_worktree(tmp_path, "owner/repo", 3, "run1", "abc123def456")


def test_create_worktree_adds_branch_from_frozen_base_sha(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "muyan-pilot-owner-repo-issue-3-run1"
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append((command, kwargs)))
    assert runner.create_worktree(tmp_path, "owner/repo", 3, "run1", "abc123def456") == path
    assert calls == [(
        ["git", "worktree", "add", "-b", "muyan-pilot/owner-repo-issue-3-run1", str(path), "abc123def456"],
        {"cwd": tmp_path},
    )]


def test_worktree_path_lives_inside_repo_worktrees_and_includes_run_id():
    repo_dir = Path("/srv/muyan/muyan-pilot")
    path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    assert path == repo_dir / ".worktrees" / "muyan-pilot-owner-repo-issue-3-run1"
    assert Path(tempfile.gettempdir()) not in path.parents


def test_worktree_path_keeps_source_repo_in_name_to_avoid_same_number_collision():
    repo_dir = Path("/srv/muyan/muyan-pilot")
    pilot = runner.worktree_path(repo_dir, "xqliu/muyan-pilot", 14, "run1")
    ceo = runner.worktree_path(repo_dir, "xqliu/muyan-ceo", 14, "run1")
    assert pilot == repo_dir / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-14-run1"
    assert ceo == repo_dir / ".worktrees" / "muyan-pilot-xqliu-muyan-ceo-issue-14-run1"
    assert pilot != ceo


def test_worktree_path_and_task_branch_differ_per_run_for_same_issue():
    repo_dir = Path("/srv/muyan/muyan-pilot")
    first_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    retry_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run2")
    assert first_path != retry_path
    assert runner.task_branch("owner/repo", 3, "run1") != runner.task_branch("owner/repo", 3, "run2")
    assert runner.task_branch("owner/repo", 3, "run1") == "muyan-pilot/owner-repo-issue-3-run1"


def test_task_branch_includes_source_repo_to_avoid_same_number_collision():
    assert runner.task_branch("owner/pilot", 1, "run1") == "muyan-pilot/owner-pilot-issue-1-run1"
    assert runner.task_branch("owner/pilot", 1, "run1") != runner.task_branch("owner/ceo", 1, "run1")


def test_comment_issue_runs_gh_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append(command))
    runner.comment_issue(3, repo="xqliu/muyan-ceo", body="done")
    assert calls == [[
        "gh", "issue", "comment", "3", "--repo", "xqliu/muyan-ceo",
        "--body", "done",
    ]]


def test_issue_context_uses_owner_repo_number_form():
    assert runner.issue_context("xqliu/muyan-pilot", 40) == (
        "xqliu/muyan-pilot#40"
    )


def test_run_pi_injects_base_branch_sha_and_run_id_into_prompt(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{SOURCE_REPO}} {{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{ISSUE_BODY}} "
        "{{WORKSPACE_ROOT}} {{CONTEXT_FILES}} {{SKILLS}} {{BASE_BRANCH}} "
        "{{BASE_SHA}} {{RUN_ID}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = {
        "prompt": prompt_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": ["context.md"],
        "skills": ["skill.md"],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "run1",
    }
    assert runner.run_pi(
        issue, tmp_path, config, "owner/repo",
        branch="muyan-pilot/owner-repo-issue-4-run1",
    ) == "done"
    command, kwargs = calls[0]
    assert command[:4] == ["pi", "--skill", "skill.md", "--print"]
    assert "owner/repo" in command[7]
    assert " 4 " in command[7]
    assert "Fix title" in command[7]
    assert "Fix body" in command[7]
    assert "context.md" in command[7]
    assert "skill.md" in command[7]
    assert command[7].endswith("main abc123def456 run1")
    assert command[8] == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] is None
    assert kwargs["run_id"] == "run1"
    assert kwargs["issue"] == 4
    assert kwargs["source_repo"] == "owner/repo"
    assert kwargs["branch"] == "muyan-pilot/owner-repo-issue-4-run1"
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_task_branch_to_stream_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append(kwargs) or "done")
    issue = {"number": 5, "title": "t", "body": "b"}
    config = {
        "prompt": prompt_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "run1",
    }
    runner.run_pi(
        issue, tmp_path, config, "owner/repo",
        timeout=7, branch="muyan-pilot/owner-repo-issue-5-run1",
    )
    assert calls[0]["branch"] == "muyan-pilot/owner-repo-issue-5-run1"
    assert calls[0]["timeout"] == 7


def test_run_pi_redacts_prompt_and_issue_from_command_log(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    runner.run_pi(
        {"number": 5, "title": "secret", "body": "token"}, tmp_path,
        {"prompt": prompt_path, "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": [], "base_branch": "main", "base_sha": "abc123def456", "run_id": "run1"},
        "owner/repo", branch="muyan-pilot/owner-repo-issue-5-run1",
    )
    command, kwargs = calls[0]
    assert "PRIVATE SYSTEM" in command[5]
    assert "token" in command[5]
    assert kwargs["log_command"] == [
        "pi", "--print", "--session-dir",
        str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_verify_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "other-branch")
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.verify_pr(tmp_path, "muyan-pilot/issue-4", "main", "e07383c2")


FAKE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
FAKE_RUN_ID = "e07383c2"


def fake_verify_run(command, **kwargs):
    """Complete fake for verify_pr: git commands answered, gh returns a PR."""
    if command[:3] == ["git", "branch", "--show-current"]:
        return f"muyan-pilot/issue-4-{FAKE_RUN_ID}"
    if command[:3] == ["git", "fetch", "origin"]:
        return ""
    if command[:3] == ["git", "merge-base", "--is-ancestor"]:
        return ""
    if command[:3] == ["git", "rev-parse", "HEAD"]:
        return FAKE_HEAD_SHA
    if command[:2] == ["gh", "pr"]:
        return json.dumps([{
            "url": "https://github.com/muyantech/muyan-pilot/pull/4",
            "baseRefName": "main",
            "headRefOid": FAKE_HEAD_SHA,
            "body": f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->\n\nPlan",
        }])
    raise AssertionError(f"unexpected command: {command}")


def test_verify_pr_rejects_delivery_behind_latest_remote_base(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command, stderr="not an ancestor")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)
    assert "base_branch=main" in caplog.text


def test_fake_verify_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_verify_run(["gh", "release", "list"])


def test_verify_pr_rejects_missing_pr(monkeypatch, tmp_path):
    outputs = iter([
        f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[]",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_rejects_non_array(monkeypatch, tmp_path):
    outputs = iter([
        f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "{}",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_returns_url_when_delivery_contains_latest_base(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
    ) == "https://github.com/muyantech/muyan-pilot/pull/4"
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls


def test_verify_pr_rejects_pr_without_url(monkeypatch, tmp_path):
    outputs = iter([
        f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[{}]",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="open PR has no URL"):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_rejects_pr_based_on_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/muyan-pilot/pull/4",
                "baseRefName": "develop",
                "headRefOid": FAKE_HEAD_SHA,
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR base is develop, expected main",
    ):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_rejects_stale_remote_pr_head(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/muyan-pilot/pull/4",
                "baseRefName": "main",
                "headRefOid": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head deadbeef.* is not local HEAD 01234567",
    ):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_rejects_pr_body_without_run_marker(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/muyan-pilot/pull/4",
                "baseRefName": "main",
                "headRefOid": FAKE_HEAD_SHA,
                "body": "no run marker here",
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)
    assert "pr_run_marker_missing" in caplog.text


def test_verify_pr_rejects_pr_body_missing_field(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/muyan-pilot/pull/4",
                "baseRefName": "main",
                "headRefOid": FAKE_HEAD_SHA,
            }])
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID)


def test_verify_pr_queries_base_head_and_accepts_matching_pr(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"muyan-pilot/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
    ) == "https://github.com/muyantech/muyan-pilot/pull/4"
    assert ["git", "rev-parse", "HEAD"] in calls
    assert [
        "gh", "pr", "list", "--state", "open", "--head",
        f"muyan-pilot/issue-4-{FAKE_RUN_ID}",
        "--json", "url,baseRefName,headRefOid,body", "--limit", "2",
    ] in calls


def test_process_issue_success_records_base_and_run_in_comment(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "verify_pr", lambda *args, **kwargs: "https://github.com/muyantech/muyan-pilot/pull/4")
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "0123456789abcdef0123456789abcdef01234567")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}
    assert runner.process_issue(issue, config, "xqliu/muyan-ceo") == "https://github.com/muyantech/muyan-pilot/pull/4"
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/muyan-ceo", "add": "ai-in-progress"})
    start = calls[1]
    assert start[0] == "comment"
    assert "Muyan Pilot started Pi:" in start[2]["body"]
    assert "base_branch=main" in start[2]["body"]
    assert "base_sha=abc123def456" in start[2]["body"]
    assert "run_id=a1b2c3d4" in start[2]["body"]
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in start[2]["body"]
    assert "branch=muyan-pilot/xqliu-muyan-ceo-issue-4-a1b2c3d4" in start[2]["body"]
    assert "worktree=" + str(tmp_path / "wt") in start[2]["body"]
    assert calls[2][0] == "edit"
    assert calls[2][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-pr-opened", "remove": "ai-in-progress"}
    comment = calls[3]
    assert comment[0] == "comment"
    body = comment[2]["body"]
    assert "Muyan Pilot opened PR: https://github.com/muyantech/muyan-pilot/pull/4" in body
    assert "base_branch=main" in body
    assert "base_sha=abc123def456" in body
    assert "run_id=a1b2c3d4" in body
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in body


def test_process_issue_success_logs_run_end_with_commit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "verify_pr", lambda *args, **kwargs: "https://github.com/muyantech/muyan-pilot/pull/4")
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: "0123456789abcdef0123456789abcdef01234567",
    )
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"},
            "xqliu/muyan-ceo",
        )
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "run=a1b2c3d4" in ends[0]
    assert "issue=xqliu/muyan-ceo#4" in ends[0]
    assert "result=pr_opened" in ends[0]
    assert "pr=https://github.com/muyantech/muyan-pilot/pull/4" in ends[0]
    assert "commit=0123456789abcdef0123456789abcdef01234567" in ends[0]


def test_process_issue_failure_marks_blocked_and_reraises(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    with pytest.raises(RuntimeError, match="git failed"):
        runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo")
    assert calls[1][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-blocked", "remove": "ai-in-progress"}
    assert calls[2][0] == "comment"
    failure_body = calls[2][2]["body"]
    assert "Muyan Pilot failed: git failed" in failure_body
    assert "base_branch=main" in failure_body
    assert "base_sha=abc123def456" in failure_body
    assert "run_id=a1b2c3d4" in failure_body
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in failure_body


def test_process_issue_preserves_original_failure_when_reporting_fails(monkeypatch, tmp_path, caplog):
    edit_calls = []

    def edit(*args, **kwargs):
        edit_calls.append(kwargs)
        if len(edit_calls) == 2:
            raise RuntimeError("github report failed")

    monkeypatch.setattr(runner, "edit_issue", edit)
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="git failed"):
        runner.process_issue({"number": 13, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo")
    assert "failure reporting failed" in caplog.text


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: None)
    config = tmp_path / "muyan-pilot.toml"
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    assert runner.main(["--config", str(config)]) == 0


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: ("xqliu/muyan-pilot", issue))
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or "https://github.com/x/y/pull/12")
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue


def test_main_accepts_repeated_source_repo(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt", encoding="utf-8")
    seen = []
    issue = {"number": 14, "title": "task"}
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"xqliu/muyan-pilot\", \"xqliu/muyan-ceo\"]\n", encoding="utf-8")
    monkeypatch.setattr(runner, "pick_next_issue", lambda repos: seen.append(repos) or (repos[0], issue))
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: "https://github.com/x/y/pull/14")
    assert runner.main([
        "--config", str(config),
    ]) == 0
    assert seen == [["xqliu/muyan-pilot", "xqliu/muyan-ceo"]]


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.main(["--config", str(config)])


def test_process_issue_failure_without_session_still_carries_scene(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pi died")),
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    with pytest.raises(RuntimeError, match="pi died"):
        runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo")
    failure_body = calls[-1][2]["body"]
    # No session file yet: the scene still carries the full debug entry
    # (worktree, branch) with '-' session fields.
    assert f"worktree={tmp_path / 'wt'}" in failure_body
    assert "branch=muyan-pilot/xqliu-muyan-ceo-issue-8-a1b2c3d4" in failure_body
    assert "session=-" in failure_body
    assert "session_file=-" in failure_body


def test_process_issue_failure_comment_includes_session_scene(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="boom"),
        ),
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-9",
        "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
        "phase": "test",
        "last_activity": "2026-08-25T02:30:00Z",
        "action": "bash pytest tests/",
        "result": "ok",
    })
    with pytest.raises(subprocess.CalledProcessError):
        runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo")
    failure_body = calls[-1][2]["body"]
    assert "Muyan Pilot failed:" in failure_body
    assert "session=sess-9" in failure_body
    assert "phase=test" in failure_body
    assert "last_activity=2026-08-25T02:30:00Z" in failure_body
    assert 'action="bash pytest tests/"' in failure_body
    assert "result=ok" in failure_body
    # The full scene on the failure comment carries the debug entry.
    assert f"worktree={tmp_path / 'wt'}" in failure_body
    assert "branch=muyan-pilot/xqliu-muyan-ceo-issue-8-a1b2c3d4" in failure_body


def test_process_issue_isolates_scene_lookup_failure(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(
        runner, "activity_snapshot",
        lambda session_dir: (_ for _ in ()).throw(OSError("disk error")),
    )
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="git failed"):
        runner.process_issue({"number": 9, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo")
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "Muyan Pilot failed: git failed" in failure_body
    assert "session=" not in failure_body


def make_fake_pi(tmp_path: Path, *, session_records: list[tuple[float, dict]],
                 stdout: str = "", stderr: str = "", exit_code: int = 0,
                 sleep: float = 0.0) -> list[str]:
    """Build a command that mimics pi: appends session records over time."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records_literal = repr(session_records)
    script = (
        "import json, sys, time\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        f"records = {records_literal}\n"
        "for delay, record in records:\n"
        "    time.sleep(delay)\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        f"time.sleep({sleep!r})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code!r})\n"
    )
    return [sys.executable, "-c", script]


def fake_session_records():
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": "2026-08-25T02:00:00Z", "cwd": "/w"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": "2026-08-25T02:00:00Z",
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.2, {"type": "message", "id": "a1",
               "timestamp": "2026-08-25T02:00:01Z",
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]


def test_log_format_has_no_python_timestamp():
    # journald already provides time, host and process (Issue #40): the
    # Python logger must not print a second timestamp.
    formatter = logging.Formatter(runner.log_format())
    record = logging.LogRecord(
        "muyan_pilot.bootstrap", logging.INFO, "file", 1, "message", None, None,
    )
    assert formatter.format(record) == "INFO message"


def test_stream_pi_logs_run_start_once_with_full_scene(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="muyan-pilot/xqliu-muyan-pilot-issue-24-run1",
        )
    assert result == "final answer"
    # Without an explicit log_command the raw command is never logged.
    assert "command=<redacted>" in caplog.text
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    start = starts[0]
    assert "run=run1" in start
    assert "issue=xqliu/muyan-pilot#24" in start
    assert "role=implement" in start
    assert "branch=muyan-pilot/xqliu-muyan-pilot-issue-24-run1" in start
    assert f"worktree={tmp_path}" in start
    # The session fields are part of the scene; before Pi writes its first
    # record they are '-' (the full entry reappears on run_failed).
    assert "session=-" in start
    assert "session_file=-" in start
    assert "phase=starting" in start
    # The user message (full prompt / Issue body) never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_run_start_carries_existing_session_file(tmp_path, caplog):
    # When the session file already exists, run_start carries its path.
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    (session_dir / "sess.jsonl").write_text(
        json.dumps({"type": "session", "id": "sess-1"}) + "\n",
        encoding="utf-8",
    )
    command = make_fake_pi(tmp_path, session_records=[], stdout="ok")
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    assert "session=sess-1" in starts[0]
    assert f"session_file={session_dir / 'sess.jsonl'}" in starts[0]


def test_stream_pi_logs_activity_and_heartbeat_lines(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer", sleep=0.3,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    heartbeats = [line for line in lines if " heartbeat " in line]
    # The visible fields change once (starting -> test): exactly one
    # activity line; unchanged polls must not repeat it (Issue #40).
    assert len(activities) == 1
    line = activities[0]
    assert "run=run1" in line
    assert "issue=xqliu/muyan-pilot#24" in line
    assert "role=implement" in line
    assert "phase=test" in line
    assert 'action="bash pytest tests/"' in line
    assert "result=-" in line  # no tool result in this fake session
    assert "idle=" in line
    # No full scene on activity lines (Issue #40).
    assert "branch=" not in line
    assert f"worktree={tmp_path}" not in line
    assert "session_file=" not in line
    assert "source_repo=" not in line
    # The idle tail produced heartbeats at the poll interval.
    assert len(heartbeats) >= 1
    for line in heartbeats:
        assert "run=run1" in line
        assert "role=implement" in line
        assert "phase=starting" in line or "phase=test" in line
        assert "elapsed=" in line
        assert "idle=" in line
        assert "branch=" not in line
        assert f"worktree={tmp_path}" not in line
    # The legacy verbose line is gone.
    assert "pi_activity" not in caplog.text
    assert "pi_idle" not in caplog.text


def test_stream_pi_activity_keeps_action_after_tool_result(tmp_path, caplog):
    """A tool result updates result only; the action line is not repeated."""
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": "2026-08-25T02:00:02Z",
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    # One activity line for the tool call, one for the result=ok update;
    # the action (the real command) is preserved on both.
    assert len(activities) == 2
    assert all('action="bash pytest tests/"' in line for line in activities)
    assert "result=-" in activities[0]
    assert "result=ok" in activities[1]
    assert "tool_result" not in caplog.text


def test_stream_pi_heartbeat_interval_is_stable(tmp_path, caplog):
    """A silent session emits one heartbeat per poll interval, no activity."""
    command = make_fake_pi(
        tmp_path, session_records=[], sleep=1.0,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    lines = caplog.text.splitlines()
    heartbeats = [line for line in lines if " heartbeat " in line]
    activities = [line for line in lines if " activity " in line]
    assert activities == []
    # ~1s of idleness at a 0.1s interval: several heartbeats, one per poll.
    assert len(heartbeats) >= 4
    for line in heartbeats:
        assert "phase=starting" in line
        assert "session=-" not in line  # session fields are not repeated


def test_stream_pi_success_logs_no_run_end(tmp_path, caplog):
    # run_end is logged by process_issue once the PR and commit are known;
    # stream_pi must not emit it (it cannot know them).
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    assert " run_end " not in caplog.text


def test_stream_pi_logs_command_redacted_and_stderr(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=[],
        stdout="ok", stderr="warning line",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b", log_command=["pi", "--print", "<redacted>"],
        )
    assert "command=pi --print <redacted>" in caplog.text
    assert "stderr=warning line" in caplog.text


def test_stream_pi_logs_run_failed_with_full_scene_and_reraises(
    tmp_path, caplog,
):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stderr="pi exploded", exit_code=3,
    )
    with caplog.at_level("ERROR"), pytest.raises(
        subprocess.CalledProcessError,
    ) as excinfo:
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    assert excinfo.value.returncode == 3
    assert "pi exploded" in (excinfo.value.stderr or "")
    # The exception must not carry the raw command (prompt / Issue body).
    assert "SECRET ISSUE BODY" not in str(excinfo.value)
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    failure = failures[0]
    assert "run=run1" in failure
    assert "issue=xqliu/muyan-pilot#24" in failure
    assert "role=implement" in failure
    assert "phase=test" in failure
    assert "reason=pi_exit_3" in failure
    # The full scene is the debug entry again: worktree and session file.
    assert f"worktree={tmp_path}" in failure
    assert f"session_file={tmp_path / '.pi-session' / 'sess.jsonl'}" in failure
    assert "session=sess-1" in failure
    # The session JSONL stays in the worktree as the local record.
    session_files = list((tmp_path / ".pi-session").glob("*.jsonl"))
    assert len(session_files) == 1
    assert len(session_files[0].read_text(encoding="utf-8").splitlines()) == 3


def test_stream_pi_heartbeats_when_session_is_idle(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        sleep=1.0,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    # Idle duration is visible on the heartbeat line itself.
    assert any("idle=" in line for line in heartbeats)


def test_stream_pi_heartbeats_when_no_session_file_appears(tmp_path, caplog):
    command = make_fake_pi(tmp_path, session_records=[], sleep=1.0)
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    assert all("phase=starting" in line for line in heartbeats)


def test_stream_pi_drains_pipe_data_written_after_exit(
    monkeypatch, tmp_path, caplog,
):
    """Data left in the pipes after the process exits is drained and kept."""

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self._out_r, self._out_w = os.pipe()
            self._err_r, self._err_w = os.pipe()
            self.stdout = os.fdopen(self._out_r, "rb")
            self.stderr = os.fdopen(self._err_r, "rb")
            self._out_writer = os.fdopen(self._out_w, "wb")
            self._err_writer = os.fdopen(self._err_w, "wb")
            self.returncode = 0

        def poll(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)

        def writer():
            time.sleep(0.3)
            process._out_writer.write(b"late stdout data")
            process._err_writer.write(b"late stderr data")
            process._out_writer.close()
            process._err_writer.close()

        threading.Thread(target=writer, daemon=True).start()
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            ["fake"], cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    assert result == "late stdout data"
    assert "stderr=late stderr data" in caplog.text


def test_stream_pi_times_out_and_kills_process(tmp_path, caplog):
    command = make_fake_pi(tmp_path, session_records=[], sleep=10.0)
    with caplog.at_level("ERROR"), pytest.raises(
        subprocess.TimeoutExpired,
    ) as excinfo:
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1, timeout=0.5,
            run_id="run1", issue=24, source_repo="xqliu/muyan-pilot",
            branch="b",
        )
    assert excinfo.value.timeout == 0.5
    # The exception must not carry the raw command (prompt / Issue body).
    assert "sleep" not in str(excinfo.value)
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=timeout_0.5s" in failures[0]
    assert "issue=xqliu/muyan-pilot#24" in failures[0]
