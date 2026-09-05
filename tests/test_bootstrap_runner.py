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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.runner as runner
from orbi import pi_activity
from tests.test_progress_wiring import make_fake_gh


def test_parse_issue_list_returns_first_issue():
    issue = {"number": 7, "title": "ship", "body": "do it"}
    assert runner.parse_issue_list(json.dumps([issue])) == issue


def test_parse_issue_list_returns_none_for_empty_list():
    assert runner.parse_issue_list("[]") is None


def test_parse_issue_list_rejects_non_list():
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.parse_issue_list("{}")


def test_load_config_resolves_relative_paths_and_values(tmp_path):
    config_path = tmp_path / "orbi.toml"
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
    config_path = tmp_path / "orbi.toml"
    config_path.write_text("prompt = \"prompt.md\"\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must be a non-empty list"):
        runner.load_config(config_path)


def test_load_config_rejects_empty_source_repo_name(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo", ""]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="source_repos must contain non-empty strings"):
        runner.load_config(config_path)


def test_load_config_defaults_base_branch_to_main(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["base_branch"] == "main"


def test_load_config_repair_issue_creation_is_opt_in(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path)["auto_repair_issues"] is False
    config_path.write_text(
        'source_repos = ["owner/repo"]\nauto_repair_issues = true\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path)["auto_repair_issues"] is True
    config_path.write_text(
        'source_repos = ["owner/repo"]\nauto_repair_issues = "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="auto_repair_issues must be a boolean"):
        runner.load_config(config_path)


def test_load_config_reads_explicit_base_branch(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = "develop"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["base_branch"] == "develop"


def test_load_config_rejects_empty_base_branch(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = ""\n', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="base_branch must be a non-empty string"):
        runner.load_config(config_path)


def test_load_config_defaults_active_milestone_to_none(tmp_path):
    """Issue #139: without an active_milestone the config keeps the
    current compat behavior (no milestone filter on the ready scans)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["active_milestone"] is None


def test_load_config_reads_explicit_active_milestone(tmp_path):
    """Issue #139: the active Milestone is an explicit claim scope —
    it is never guessed from the repo's Milestone list."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["active_milestone"] == "v0.2.0"


def test_load_config_rejects_empty_active_milestone(tmp_path):
    """Issue #139: an empty active_milestone is a misconfiguration —
    fail fast instead of silently disabling the scope."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="active_milestone must be a non-empty string",
    ):
        runner.load_config(config_path)


def test_load_config_rejects_non_string_active_milestone(tmp_path):
    """Issue #139: a non-string active_milestone is a misconfiguration —
    fail fast."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="active_milestone must be a non-empty string",
    ):
        runner.load_config(config_path)


# --- Issue #228: model_wait_dead_seconds is configurable ---------------------

def test_load_config_defaults_model_wait_dead_seconds_to_thirty_minutes(
    tmp_path,
):
    """Issue #228: omitted -> 1800 seconds (30 minutes): a slow local
    model (Qwen 27B, ~17 tokens/s, llama-server request timeout 1200 s)
    must not be killed merely because one complete assistant message
    takes more than 10 minutes."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["model_wait_dead_seconds"] == 1800.0


def test_load_config_reads_explicit_model_wait_dead_seconds_int(tmp_path):
    """Issue #228: an explicit integer override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmodel_wait_dead_seconds = 900\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["model_wait_dead_seconds"] == 900.0


def test_load_config_reads_explicit_model_wait_dead_seconds_float(tmp_path):
    """Issue #228: an explicit float override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmodel_wait_dead_seconds = 1234.5\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["model_wait_dead_seconds"] == 1234.5


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("true", "not a boolean"),
        ("false", "not a boolean"),
        ("0", "positive"),
        ("-5", "positive"),
        ("nan", "finite"),
        ("inf", "finite"),
        ("-inf", "finite"),
        ('"300"', "number"),
    ],
)
def test_load_config_rejects_invalid_model_wait_dead_seconds(
    tmp_path, value, reason,
):
    """Issue #228: booleans, zero, negative, NaN/infinity and
    non-numeric values are rejected at config load with the field name
    and the concrete reason."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"model_wait_dead_seconds = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "model_wait_dead_seconds" in str(excinfo.value)
    assert reason in str(excinfo.value)


# --- Issue #233: the /slots swallow probe is configurable --------------------

def test_load_config_defaults_swallow_probe_disabled(tmp_path):
    """Issue #233: omitted -> the probe is disabled (None) and the grace
    defaults to 60 s: the run is bounded by model_wait_dead_seconds only
    (the exact pre-#233 behavior)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["model_wait_probe_url"] is None
    assert config["model_wait_probe_seconds"] == 60.0


def test_load_config_reads_explicit_swallow_probe(tmp_path):
    """Issue #233: an explicit /slots URL and grace are accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'model_wait_probe_url = "http://127.0.0.1:18082/slots"\n'
        "model_wait_probe_seconds = 90\n",
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["model_wait_probe_url"] == "http://127.0.0.1:18082/slots"
    assert config["model_wait_probe_seconds"] == 90.0


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('""', "non-empty"),
        ("123", "non-empty"),
        ("true", "non-empty"),
        ('"ftp://x/slots"', "http:// or https://"),
        ('"file:///tmp"', "http:// or https://"),
        ('"x://y"', "http:// or https://"),
    ],
)
def test_load_config_rejects_invalid_swallow_probe_url(
    tmp_path, value, reason,
):
    """Issue #233: a present probe URL must be a non-empty http(s) URL;
    anything else fails fast at config load with the field name and the
    concrete reason."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"model_wait_probe_url = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "model_wait_probe_url" in str(excinfo.value)
    assert reason in str(excinfo.value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("true", "not a boolean"),
        ("false", "not a boolean"),
        ("0", "positive"),
        ("-5", "positive"),
        ("nan", "finite"),
        ("inf", "finite"),
        ("-inf", "finite"),
        ('"300"', "number"),
    ],
)
def test_load_config_rejects_invalid_swallow_probe_seconds(
    tmp_path, value, reason,
):
    """Issue #233: booleans, zero, negative, NaN/infinity and non-numeric
    values are rejected at config load with the field name and the
    concrete reason."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"model_wait_probe_seconds = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "model_wait_probe_seconds" in str(excinfo.value)
    assert reason in str(excinfo.value)


def test_load_config_parses_repositories_registry(tmp_path):
    """Issue #134: an explicit [[repositories]] section parses into a
    registry of name/path/github/base_branch, with each path resolved
    relative to the config file."""
    (tmp_path / "checkouts" / "pilot").mkdir(parents=True)
    (tmp_path / "checkouts" / "ceo").mkdir(parents=True)
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n'
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = "checkouts/pilot"\n'
        'github = "owner/pilot"\n'
        'base_branch = "main"\n'
        "[[repositories]]\n"
        'name = "ceo"\n'
        'path = "checkouts/ceo"\n'
        'github = "owner/ceo"\n'
        'base_branch = "develop"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["repositories"] == [
        {
            "name": "pilot",
            "path": (tmp_path / "checkouts" / "pilot").resolve(),
            "github": "owner/pilot",
            "base_branch": "main",
        },
        {
            "name": "ceo",
            "path": (tmp_path / "checkouts" / "ceo").resolve(),
            "github": "owner/ceo",
            "base_branch": "develop",
        },
    ]


def test_load_config_defaults_repositories_to_empty_list(tmp_path):
    """Issue #134: without a repositories section the config keeps the
    exact single-repo shape (empty registry, all existing keys intact)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["repositories"] == []
    assert config["source_repos"] == ["owner/pilot"]
    assert config["repo_dir"] == (tmp_path / "repo").resolve()
    assert config["base_branch"] == "main"


@pytest.mark.parametrize("field", ["name", "path", "github", "base_branch"])
def test_load_config_rejects_repository_missing_field(tmp_path, field):
    """Issue #134: a repository entry missing one required field is
    rejected, naming the field."""
    entry = {
        "name": "pilot",
        "path": "checkouts/pilot",
        "github": "owner/pilot",
        "base_branch": "main",
    }
    del entry[field]
    lines = ["source_repos = [\"owner/pilot\"]\n", "[[repositories]]\n"]
    lines += [f'{key} = "{value}"\n' for key, value in entry.items()]
    config_path = tmp_path / "orbi.toml"
    config_path.write_text("".join(lines), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        runner.load_config(config_path)


def test_load_config_rejects_repository_empty_field(tmp_path):
    """Issue #134: an empty required field counts as missing."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n'
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = ""\n'
        'github = "owner/pilot"\n'
        'base_branch = "main"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="path"):
        runner.load_config(config_path)


def test_load_config_rejects_repository_non_string_field(tmp_path):
    """Issue #134: a required field with a non-string type is rejected."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n'
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = "checkouts/pilot"\n'
        'github = "owner/pilot"\n'
        "base_branch = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="base_branch"):
        runner.load_config(config_path)


def test_load_config_rejects_repository_non_table_entry(tmp_path):
    """Issue #134: a repositories entry that is not a table is rejected."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n'
        'repositories = ["pilot"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"repositories\[0\]"):
        runner.load_config(config_path)


def test_load_config_rejects_repositories_not_a_list(tmp_path):
    """Issue #134: a repositories section that is not a list is rejected."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n'
        'repositories = "pilot"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repositories must be a list"):
        runner.load_config(config_path)


def test_load_config_rejects_duplicate_repository_name(tmp_path):
    """Issue #134: two entries with the same name are rejected, naming
    the duplicate."""
    entry = (
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = "checkouts/pilot"\n'
        'github = "owner/pilot"\n'
        'base_branch = "main"\n'
    )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\n' + entry + entry,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate.*pilot"):
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


def test_prompt_requires_verifying_external_behavior_against_docs():
    """Issue #73: the implementer prompt must make it a hard rule to
    verify external behavior (APIs, CLIs, config, HTTP paths) against
    official docs or `--help` before writing it — no assembling from
    memory — and test assertions must assert the real contract, not
    the implementation's guessed shape. It must also state that
    bypass failures (progress, notifications) only log and never
    decide the delivery outcome."""
    template = (
        Path(__file__).resolve().parent.parent / "prompt.md"
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
        Path(__file__).resolve().parent.parent / "prompt_review.md"
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
        Path(__file__).resolve().parent.parent / "prompt.md"
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
        Path(__file__).resolve().parent.parent / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "Fixes #{{ISSUE_NUMBER}}" in template
    rendered = runner.render_prompt(template, {
        "SOURCE_REPO": "xqliu/orbi",
        "SOURCE_REPOS": "xqliu/orbi",
        "ISSUE_NUMBER": "53",
        "ISSUE_TITLE": "t",
        "ISSUE_BODY": "b",
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


def test_validate_config_accepts_existing_files(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt_review = tmp_path / "prompt_review.md"
    skill = tmp_path / "skill.md"
    context = tmp_path / "context.md"
    for path in (prompt, prompt_review, skill, context):
        path.write_text("ok", encoding="utf-8")
    runner.validate_config({
        "repo_dir": tmp_path,
        "prompt": prompt,
        "prompt_review": prompt_review,
        "skills": [skill],
        "context_files": [context],
    })


def test_validate_config_requires_review_prompt(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("ok", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="prompt_review.md"):
        runner.validate_config({
            "repo_dir": tmp_path,
            "prompt": prompt,
            "prompt_review": tmp_path / "prompt_review.md",
            "skills": [],
            "context_files": [],
        })


def test_validate_config_fails_before_issue_claim_when_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.md"):
        runner.validate_config({
            "repo_dir": tmp_path,
            "prompt": tmp_path / "missing.md",
            "prompt_review": tmp_path / "prompt_review.md",
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


def _git_checkout(path: Path) -> Path:
    """A real Git checkout with one commit (verified against the real
    CLI; the commit lets `git worktree add` check a branch out)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("repo", encoding="utf-8")
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *identity, "add", "README.md"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", *identity, "commit", "-q", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _base_config(tmp_path: Path) -> dict:
    """The minimal valid single-repo config (no repositories key)."""
    prompt = tmp_path / "prompt.md"
    prompt_review = tmp_path / "prompt_review.md"
    for path in (prompt, prompt_review):
        path.write_text("ok", encoding="utf-8")
    return {
        "repo_dir": tmp_path,
        "prompt": prompt,
        "prompt_review": prompt_review,
        "skills": [],
        "context_files": [],
    }


def test_validate_config_accepts_repository_git_checkout(tmp_path):
    """Issue #134: a registered path that is a real Git checkout passes."""
    checkout = _git_checkout(tmp_path / "pilot")
    config = _base_config(tmp_path)
    config["repositories"] = [{
        "name": "pilot",
        "path": checkout,
        "github": "owner/pilot",
        "base_branch": "main",
    }]
    runner.validate_config(config)


def test_validate_config_accepts_repository_linked_worktree(tmp_path):
    """Issue #134: a linked worktree (a .git FILE, not a directory) is a
    Git checkout too."""
    main_repo = _git_checkout(tmp_path / "main")
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree)],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    assert (worktree / ".git").is_file()
    config = _base_config(tmp_path)
    config["repositories"] = [{
        "name": "wt",
        "path": worktree,
        "github": "owner/pilot",
        "base_branch": "main",
    }]
    runner.validate_config(config)


def test_validate_config_rejects_missing_repository_path(tmp_path):
    """Issue #134: a registered path that does not exist is rejected."""
    config = _base_config(tmp_path)
    config["repositories"] = [{
        "name": "pilot",
        "path": tmp_path / "no-such-checkout",
        "github": "owner/pilot",
        "base_branch": "main",
    }]
    with pytest.raises(FileNotFoundError, match="no-such-checkout"):
        runner.validate_config(config)


def test_validate_config_rejects_repository_path_not_a_git_checkout(
    tmp_path,
):
    """Issue #134: an existing path without a .git (file or directory)
    is not a Git checkout and is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    config = _base_config(tmp_path)
    config["repositories"] = [{
        "name": "plain",
        "path": plain,
        "github": "owner/plain",
        "base_branch": "main",
    }]
    with pytest.raises(ValueError, match="not a git checkout"):
        runner.validate_config(config)


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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/muyan-ceo") == issue
    # Issue #101: the P0 scan runs first, then the bug scan (Issue
    # #71); with nothing in either queue the plain ready scan decides.
    # All three keep the same exclusions.
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            "label:ai-ready label:p0 -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            "label:ai-ready label:bug -label:ai-in-progress "
            "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
            "-label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            "label:ai-ready -label:ai-in-progress -label:ai-pr-opened "
            "-label:ai-fix-needed -label:ai-merged -label:ai-blocked",
            "--json", "number,title,body,labels,blockedBy",
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == ready
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([blocked_a, blocked_b]),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert runner.pick_issue("xqliu/orbi") == issue


def test_pick_issue_claims_issue_with_empty_blocked_by(monkeypatch):
    issue = {
        "number": 54, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
    }
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert runner.pick_issue("xqliu/orbi") == issue


def test_pick_issue_stays_blocked_while_any_blocker_is_open(monkeypatch):
    blocked = {
        "number": 54, "title": "mixed", "body": "",
        "blockedBy": {"nodes": [
            {"number": 31, "state": "CLOSED", "title": "done"},
            {"number": 32, "state": "OPEN", "title": "pending"},
        ], "totalCount": 2},
    }
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([blocked]),
    )
    assert runner.pick_issue("xqliu/orbi") is None


def test_pick_issue_fails_open_when_blocked_by_field_missing(monkeypatch):
    # Older gh versions or a changed API shape omit the field: the
    # Issue must still be claimable (fail open, Issue #54).
    issue = {"number": 9, "title": "task", "body": "body"}
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([issue]),
    )
    assert runner.pick_issue("xqliu/orbi") == issue


def test_pick_issue_fails_open_when_blocked_by_query_fails(
    monkeypatch, caplog,
):
    """A failed blockedBy query must not deadlock the queue (Issue
    #54 fail open): the tick claims nothing this round and the error
    is logged, never raised — the next tick retries the query."""
    error = subprocess.CalledProcessError(
        1, ["gh"], output="boom", stderr="rate limited",
    )
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
    assert "blocked_by_check_failed" in caplog.text


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue(
        "xqliu/muyan-ceo", active_milestone="v0.2.0",
    ) == issue
    scope = ' milestone:"v0.2.0"'
    exclusions = (
        "-label:ai-in-progress -label:ai-pr-opened -label:ai-fix-needed "
        "-label:ai-merged -label:ai-blocked"
    )
    assert calls == [
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            f"label:ai-ready label:p0{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            f"label:ai-ready label:bug{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy",
            "--limit", "200",
        ],
        [
            "gh", "issue", "list", "--repo", "xqliu/muyan-ceo",
            "--state", "open", "--search",
            f"label:ai-ready{scope} {exclusions}",
            "--json", "number,title,body,labels,blockedBy",
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
    p0_search, bug_search, plain_search = runner.ready_searches("v0.2.0")
    for search in (p0_search, bug_search, plain_search):
        assert 'milestone:"v0.2.0"' in search
    assert "label:p0" in p0_search
    assert "label:p0" not in bug_search
    assert "label:p0" not in plain_search


def test_ready_searches_without_milestone_are_the_compat_scans():
    """Issue #139 compat: without a configured Milestone the three
    ready scans are byte-identical to the pre-#139 scans — a config
    without `active_milestone` behaves exactly like before."""
    p0_search, bug_search, plain_search = runner.ready_searches(None)
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
    assert runner.ready_searches() == runner.ready_searches(None)


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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([epic, blocked, ready]),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue(
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/orbi") == bug
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/orbi") == bug
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/orbi") == feature
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/orbi") == feature
    assert len(searches) == 3


def test_pick_issue_bug_scan_failure_fails_open(monkeypatch, caplog):
    """Issue #71: a failed bug scan fails open like the ready scan
    (Issue #54) — the tick claims nothing from this repo, the error is
    logged, never raised."""
    error = subprocess.CalledProcessError(1, ["gh"], output="boom")
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == p0
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_issue("xqliu/orbi") == p0
    assert searches[0] == (
        "label:ai-ready label:p0 -label:ai-in-progress "
        "-label:ai-pr-opened -label:ai-fix-needed -label:ai-merged "
        "-label:ai-blocked"
    )


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == bug
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(error),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == feature
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
    assert runner.is_epic({
        "number": 80,
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
    }) is True
    assert runner.is_epic({
        "number": 10,
        "labels": [{"name": "ai-ready"}, {"name": "bug"}],
    }) is False
    assert runner.is_epic({"number": 10}) is False
    assert runner.is_epic({"number": 10, "labels": "nope"}) is False
    assert runner.is_epic({
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([epic, ready]),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == ready
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
        # The Epics carry no p0/bug label: only the plain ready scan
        # sees them.
        if "label:p0" in search or "label:bug" in search:
            return json.dumps([])
        return json.dumps([epic_a, epic_b])

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([epic]),
    )
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") is None
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.pick_issue("xqliu/orbi") == feature
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels", "--limit", "1",
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

    monkeypatch.setattr(runner, "run_command", fake_run)
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert runner.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels", "--limit", "1",
    ]]


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    # No slot dir yet: every slot is free, so the scan runs.
    assert runner.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) == issue
    assert calls == [[
        "gh", "issue", "list", "--repo", "xqliu/orbi",
        "--state", "open", "--search",
        "label:ai-ready label:ai-in-progress -label:ai-pr-opened "
        "-label:ai-fix-needed -label:ai-merged -label:ai-blocked "
        "-label:ai-epic",
        "--json", "number,title,body,labels", "--limit", "1",
    ]]


def test_pick_in_progress_issue_returns_none_when_idle(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        runner, "run_command", lambda command, **kwargs: "[]",
    )
    assert runner.pick_in_progress_issue(
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
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: gh_calls.append(command) or "[]",
    )
    monkeypatch.setattr(runner, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, 4242)])
    assert runner.pick_in_progress_issue(
        "xqliu/orbi", tmp_path / "slots", 1,
    ) is None
    assert gh_calls == [], "no gh traffic while another runner is live"
    # Own PID: the scan still runs (this runner holds its own slot).
    monkeypatch.setattr(runner, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, os.getpid())])
    assert runner.pick_in_progress_issue(
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
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: (
            in_flight if repo == "r1" else None
        ),
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: ready)
    assert runner.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
    ) == ("r1", in_flight, None)


def test_pick_next_delivery_keeps_resumable_delivery_first(
    monkeypatch, tmp_path,
):
    """An `ai-fix-needed` PR resume always beats an in-flight restart:
    the PR already exists and must not be re-implemented."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    resumable = {"number": 5, "title": "fix needed", "body": ""}
    scene = {"run_id": "a1b2c3d4"}
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: (
            (resumable, scene) if repo == "r2" else None
        ),
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: in_flight,
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: in_flight)
    assert runner.pick_next_delivery(
        ["r1", "r2"], tmp_path / "slots", 1,
    ) == ("r2", resumable, scene)


def test_pick_next_delivery_falls_through_to_ready_when_no_in_flight(
    monkeypatch, tmp_path,
):
    ready = {"number": 3, "title": "ready", "body": ""}
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: ready)
    assert runner.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
    ) == ("r1", ready, None)


def test_pick_next_delivery_returns_none_when_all_scans_empty(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: None)
    assert runner.pick_next_delivery(
        ["r1"], tmp_path / "slots", 1,
    ) is None


def test_pick_next_issue_returns_first_ready_source(monkeypatch):
    issue = {"number": 1, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None):
        calls.append(repo)
        return issue if repo == "xqliu/muyan-ceo" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/orbi"]) == (
        "xqliu/muyan-ceo", issue,
    )
    assert calls == ["xqliu/muyan-ceo"]


def test_pick_next_issue_falls_through_to_second_source(monkeypatch):
    issue = {"number": 2, "title": "pilot", "body": ""}
    calls = []

    def pick(repo, active_milestone=None):
        calls.append(repo)
        return issue if repo == "xqliu/orbi" else None

    monkeypatch.setattr(runner, "pick_issue", pick)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/orbi"]) == (
        "xqliu/orbi", issue,
    )
    assert calls == ["xqliu/muyan-ceo", "xqliu/orbi"]


def test_pick_next_issue_returns_none_when_all_sources_empty(monkeypatch):
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: None)
    assert runner.pick_next_issue(["xqliu/muyan-ceo", "xqliu/orbi"]) is None


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
    assert runner.run_marker("e07383c2") == "<!-- orbi:run=e07383c2 -->"


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


def _probe_lock_free(repo_dir: Path) -> bool:
    """True when a non-blocking probe acquires the base-sync lock
    (i.e. the lock is FREE); False while it is held."""
    lock_path = runner.base_sync_lock_path(repo_dir)
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.freeze_base(tmp_path, "main") == "abc123def456"
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


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


def test_create_worktree_reuses_existing_path_for_a_resumed_run(
    monkeypatch, tmp_path,
):
    """Only a resumed run (same run id after a process restart) reaches
    an existing worktree path; it is the scene the run continues in
    (Issue #18), so it is reused, never recreated."""
    existing = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    existing.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: calls.append(command),
    )
    assert runner.create_worktree(
        tmp_path, "owner/repo", 3, "run1", "abc123def456",
    ) == existing
    assert calls == [], "a resumed worktree must never be recreated"


def test_create_worktree_adds_branch_from_frozen_base_sha(monkeypatch, tmp_path):
    path = tmp_path / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    calls = []
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: calls.append((command, kwargs)))
    assert runner.create_worktree(tmp_path, "owner/repo", 3, "run1", "abc123def456") == path
    assert calls == [(
        ["git", "worktree", "add", "-b", "orbi/owner-repo-issue-3-run1", str(path), "abc123def456"],
        {"cwd": tmp_path},
    )]


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
    assert runner.run_state_path(worktree) == (
        worktree / ".orbi" / "run-state.json"
    )


def test_write_run_state_writes_the_run_identity(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    runner.write_run_state(
        worktree, run_id="a1b2c3d4", issue=3, source_repo="owner/repo",
        branch="orbi/owner-repo-issue-3-a1b2c3d4",
    )
    state = json.loads(runner.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"
    assert state["branch"] == "orbi/owner-repo-issue-3-a1b2c3d4"
    assert state["worktree"] == str(worktree)
    assert isinstance(state["created_at"], str) and state["created_at"]


def test_write_run_state_is_idempotent_for_a_resumed_run(tmp_path):
    """A resumed run refreshes the SAME state file (same run id) — the
    file is the same-run marker, never a per-session artifact."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    runner.write_run_state(
        worktree, run_id="a1b2c3d4", issue=3, source_repo="owner/repo",
        branch="orbi/owner-repo-issue-3-a1b2c3d4",
    )
    runner.write_run_state(
        worktree, run_id="a1b2c3d4", issue=3, source_repo="owner/repo",
        branch="orbi/owner-repo-issue-3-a1b2c3d4",
    )
    state = json.loads(runner.run_state_path(worktree).read_text())
    assert state["run_id"] == "a1b2c3d4"


def test_read_run_state_returns_none_without_the_file(tmp_path):
    assert runner.read_run_state(tmp_path) is None


def test_read_run_state_round_trips_the_written_state(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    runner.write_run_state(
        worktree, run_id="a1b2c3d4", issue=3, source_repo="owner/repo",
        branch="orbi/owner-repo-issue-3-a1b2c3d4",
    )
    state = runner.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 3
    assert state["repo"] == "owner/repo"


def test_read_run_state_fails_fast_on_unreadable_or_malformed_state(tmp_path):
    """A corrupt state file is a delivery failure, never a guess: the
    resume must continue the SAME run (Issue #219)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    path = runner.run_state_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        runner.read_run_state(worktree)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        runner.read_run_state(worktree)
    path.write_text(json.dumps({"run_id": "a1b2c3d4"}), encoding="utf-8")
    with pytest.raises(ValueError, match="run state"):
        runner.read_run_state(worktree)


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.changed_files(tmp_path) == ["src/a.py", "src/b.py"]
    assert calls == [(
        ["git", "status", "--porcelain"], {"cwd": tmp_path},
    )]


def test_resume_context_is_none_for_a_fresh_worktree(tmp_path, monkeypatch):
    """No uncommitted changes and no previous session: the agent starts
    from the Issue alone (the exact pre-#219 prompt)."""
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(runner, "activity_snapshot", lambda *a, **k: None)
    assert runner.resume_context(tmp_path) is None


def test_resume_context_carries_changes_and_session_progress(
    tmp_path, monkeypatch,
):
    """The new session starts from the existing work: the instruction to
    continue, the previous session's progress and the changed files —
    never a fresh redo (Issue #219)."""
    monkeypatch.setattr(
        runner, "run_command",
        lambda *a, **k: " M src/a.py\n?? src/b.py\n",
    )
    monkeypatch.setattr(
        runner, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-1", "session_file": "x.jsonl",
            "events": 7, "phase": "test", "last_activity": "2026-09-02T04:01:00Z",
            "action": "bash pytest tests/", "result": "ok",
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = runner.resume_context(tmp_path)
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
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: "")
    monkeypatch.setattr(
        runner, "activity_snapshot",
        lambda *a, **k: {
            "session_id": "sess-2", "session_file": "x.jsonl",
            "events": 3, "phase": "commit", "last_activity": None,
            "action": "git commit -m x", "result": None,
            "model_wait": False, "changed": False, "stale_seconds": 1.0,
        },
    )
    context = runner.resume_context(tmp_path)
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
    runner.write_run_state(
        old, run_id="aaaa1111", issue=42, source_repo="xqliu/orbi",
        branch="orbi/xqliu-orbi-issue-42-aaaa1111",
    )
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
    runner.write_run_state(
        first, run_id="0d111111", issue=3, source_repo="owner/repo",
        branch="b1",
    )
    runner.write_run_state(
        second, run_id="2e222222", issue=3, source_repo="owner/repo",
        branch="b2",
    )
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
    runner.write_run_state(
        other_issue, run_id="ffff0000", issue=4, source_repo="owner/repo",
        branch="b1",
    )
    runner.write_run_state(
        other_repo, run_id="ffff1111", issue=3, source_repo="other/other",
        branch="b2",
    )
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
    state = runner.run_state_path(worktree)
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
    runner.write_run_state(
        mine, run_id="aaaa1111", issue=3, source_repo="owner/repo",
        branch="b1",
    )
    assert runner.resume_run_id(tmp_path, "owner/repo", 3) == "aaaa1111"


def test_has_in_progress_label_checks_the_issue_label(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return json.dumps([{"number": 4}])

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.has_in_progress_label(4, "owner/repo") is True
    assert calls == [[
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "all",
        "--search", "label:ai-in-progress",
        "--json", "number", "--limit", "50",
    ]]


def test_has_in_progress_label_is_false_without_the_label(monkeypatch):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps([{"number": 5}]),
    )
    assert runner.has_in_progress_label(4, "owner/repo") is False


def test_has_in_progress_label_fails_fast_on_malformed_output(monkeypatch):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "{}")
    with pytest.raises(ValueError, match="issue list must be a JSON array"):
        runner.has_in_progress_label(4, "owner/repo")


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
    branch = "orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # The existing progress comment of the dead run.
                return json.dumps([existing_comment])
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # The Issue still carries `ai-in-progress` (the runner died).
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "muyantech"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            return head
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append(("edit", args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    # The fresh id would be different: the resume must override it with
    # the newest worktree's run id.
    monkeypatch.setattr(runner, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(
        runner, "create_worktree",
        lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
              "base_branch": "main"}
    assert runner.process_issue(
        issue, config, "xqliu/muyan-ceo",
    ) == "https://github.com/muyantech/orbi/pull/4"
    # The reused run id drives the branch and the scene comments.
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/muyan-ceo",
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
        and command[2] == "repos/xqliu/muyan-ceo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the existing progress comment was not updated"
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi delivered" in last_body
    assert "- branch: orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4" in last_body


def test_process_issue_binds_run_id_before_the_resume_scan(
    monkeypatch, tmp_path, caplog,
):
    """Run correlation (Issue #41): EVERY journal line of the attempt
    carries the `[run_id]` prefix — including the claim-time lines of a
    restart resume (the `ai-in-progress` label scan and `resuming_run`),
    which run before the resume decision (review round 3, PR #42)."""
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    head = "0123456789abcdef0123456789abcdef01234567"
    branch = "orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "muyantech"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            return head
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123",
    )
    # The fresh id differs from the reused one: both are valid run ids
    # of this attempt (generated, then replaced by the resumed run).
    monkeypatch.setattr(runner, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
             "base_branch": "main"},
            "xqliu/muyan-ceo",
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
        if command[:3] == ["gh", "issue", "list"]:
            # No `ai-in-progress` label: the previous run finished.
            return "[]"
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": "orbi/xqliu-muyan-ceo-issue-4-ffffeeee",
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "muyantech"},
                "body": (
                    "<!-- orbi:run=ffffeeee -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-muyan-ceo-issue-4-ffffeeee"
        if command[:2] == ["git", "rev-parse"]:
            return head
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "ffffeeee")
    scene_calls = []
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: scene_calls.append(1) or None,
    )
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/muyan-ceo",
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
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 4}])
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": "orbi/xqliu-muyan-ceo-issue-4-ffffeeee",
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "muyantech"},
                "body": (
                    "<!-- orbi:run=ffffeeee -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-muyan-ceo-issue-4-ffffeeee"
        if command[:2] == ["git", "rev-parse"]:
            return head
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "ffffeeee")
    # The label is on, but no task worktree survived the kill.
    monkeypatch.setattr(runner, "worktree_resume_scene", lambda *a: None)
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
             "base_branch": "main"},
            "xqliu/muyan-ceo",
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
    branch = f"orbi/xqliu-muyan-ceo-issue-4-{run_id}"
    comment_bodies = []

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "ffffeeee")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: (
            (latest_run_id_result, worktree)
            if latest_run_id_result else None
        ),
    )
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: worktree,
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
        runner, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/muyantech/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/muyan-ceo",
    )
    # The run state file marks the worktree as the same run.
    state = runner.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 4
    assert state["repo"] == "xqliu/muyan-ceo"
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
        runner, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/muyantech/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/muyan-ceo",
    )
    state = runner.read_run_state(worktree)
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
        runner, "run_pi",
        lambda *args, **kwargs: run_pi_calls.append(1) or "done",
    )
    edits = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="run state"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
             "base_branch": "main"},
            "xqliu/muyan-ceo",
        )
    # No fresh run was started.
    assert run_pi_calls == []
    # The terminal state is `ai-blocked` (the claim label removed).
    assert edits[-1] == ((4,), {
        "repo": "xqliu/muyan-ceo", "add": "ai-blocked",
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
    repo_dir = Path("/srv/muyan/orbi")
    path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    assert path == repo_dir / ".worktrees" / "orbi-owner-repo-issue-3-run1"
    assert Path(tempfile.gettempdir()) not in path.parents


def test_worktree_path_keeps_source_repo_in_name_to_avoid_same_number_collision():
    repo_dir = Path("/srv/muyan/orbi")
    pilot = runner.worktree_path(repo_dir, "xqliu/orbi", 14, "run1")
    ceo = runner.worktree_path(repo_dir, "xqliu/muyan-ceo", 14, "run1")
    assert pilot == repo_dir / ".worktrees" / "orbi-xqliu-orbi-issue-14-run1"
    assert ceo == repo_dir / ".worktrees" / "orbi-xqliu-muyan-ceo-issue-14-run1"
    assert pilot != ceo


def test_worktree_path_and_task_branch_differ_per_run_for_same_issue():
    repo_dir = Path("/srv/muyan/orbi")
    first_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run1")
    retry_path = runner.worktree_path(repo_dir, "owner/repo", 3, "run2")
    assert first_path != retry_path
    assert runner.task_branch("owner/repo", 3, "run1") != runner.task_branch("owner/repo", 3, "run2")
    assert runner.task_branch("owner/repo", 3, "run1") == "orbi/owner-repo-issue-3-run1"


def test_task_branch_includes_source_repo_to_avoid_same_number_collision():
    assert runner.task_branch("owner/pilot", 1, "run1") == "orbi/owner-pilot-issue-1-run1"
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
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append(command) or "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path / "checkout",
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
        branch="orbi/owner-repo-issue-4-run1",
    )
    command = calls[0]
    assert command[command.index("--system-prompt") + 1] == "SYSTEM " + str(
        tmp_path / "checkout" / ".orbi" / "base-sync.lock",
    )


def test_run_pi_logs_provider_config_loaded_with_selection(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: run_pi logs `provider_config_loaded` with the
    configured provider/model identifiers (the same non-sensitive
    values as the redacted command line) before Pi is spawned."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path / "checkout",
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "run1",
        "pi_provider": "local-qwen",
        "pi_model": "qwen3.8:27b",
    }
    with caplog.at_level("INFO"):
        runner.run_pi(
            issue, tmp_path, config, "owner/repo",
            branch="orbi/owner-repo-issue-4-run1",
        )
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
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path / "checkout",
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "run1",
    }
    with caplog.at_level("INFO"):
        runner.run_pi(
            issue, tmp_path, config, "owner/repo", branch="b",
        )
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
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "ok")
    with caplog.at_level("INFO"):
        runner.run_review(
            tmp_path,
            {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"},
            {
                "prompt_review": prompt_path,
                "repo_dir": tmp_path / "checkout",
                "source_repos": ["owner/repo"],
                "base_branch": "main",
                "run_id": "a1b2c3d4",
                "skills": [],
                "pi_provider": "local-qwen",
                "pi_model": "qwen3.8:27b",
            },
            "owner/repo", 4, "branch", 1,
        )
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
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append(command) or "ok")
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        {
            "prompt_review": prompt_path,
            "repo_dir": tmp_path / "checkout",
            "source_repos": ["owner/repo"],
            "base_branch": "main",
            "run_id": "a1b2c3d4",
            "skills": [],
        },
        "owner/repo", 4, "branch", 1,
    )
    command = calls[0]
    assert command[command.index("--system-prompt") + 1] == "REVIEW " + str(
        tmp_path / "checkout" / ".orbi" / "base-sync.lock",
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
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path / "checkout",
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
        branch="orbi/owner-repo-issue-4-run1",
    ) == "done"
    command, kwargs = calls[0]
    assert command[:4] == ["pi", "--skill", "skill.md", "--print"]
    assert "owner/repo" in command[7]
    assert " 4 " in command[7]
    assert "Fix title" in command[7]
    assert "Fix body" in command[7]
    assert "context.md" in command[7]
    assert "skill.md" in command[7]
    assert command[7].endswith(
        "main abc123def456 run1 "
        + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock"),
    )
    assert command[8] == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] is None
    assert kwargs["run_id"] == "run1"
    assert kwargs["issue"] == 4
    assert kwargs["source_repo"] == "owner/repo"
    assert kwargs["branch"] == "orbi/owner-repo-issue-4-run1"
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_task_branch_to_stream_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append(kwargs) or "done")
    issue = {"number": 5, "title": "t", "body": "b"}
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path,
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
        timeout=7, branch="orbi/owner-repo-issue-5-run1",
    )
    assert calls[0]["branch"] == "orbi/owner-repo-issue-5-run1"
    assert calls[0]["timeout"] == 7


def test_run_pi_redacts_prompt_and_issue_from_command_log(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    runner.run_pi(
        {"number": 5, "title": "secret", "body": "token"}, tmp_path,
        {"prompt": prompt_path, "repo_dir": tmp_path, "source_repos": ["owner/repo"], "workspace_root": tmp_path, "context_files": [], "skills": [], "base_branch": "main", "base_sha": "abc123def456", "run_id": "run1"},
        "owner/repo", branch="orbi/owner-repo-issue-5-run1",
    )
    command, kwargs = calls[0]
    assert "PRIVATE SYSTEM" in command[5]
    assert "token" in command[5]
    assert kwargs["log_command"] == [
        "pi", "--print", "--session-dir",
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = {
        "prompt": prompt_path, "repo_dir": tmp_path,
        "source_repos": ["owner/repo"], "workspace_root": tmp_path,
        "context_files": [], "skills": [], "base_branch": "main",
        "base_sha": "abc123def456", "run_id": "run1",
    }
    runner.run_pi(
        {"number": 5, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-5-run1",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = {
        "prompt": prompt_path, "repo_dir": tmp_path,
        "source_repos": ["owner/repo"], "workspace_root": tmp_path,
        "context_files": [], "skills": [], "base_branch": "main",
        "base_sha": "abc123def456", "run_id": "run1",
    }
    resume = (
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.\nPrevious session progress: session=sess-1 "
        "events=7 phase=test last_action=bash pytest last_result=ok\n"
        "Uncommitted changed files (2):\n- src/a.py\n- src/b.py"
    )
    runner.run_pi(
        {"number": 5, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-5-run1",
        resume_context=resume,
    )
    command = calls[0]
    context = command[-1]
    assert context.startswith(
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: " + str(tmp_path),
    )
    assert context.endswith(resume)


def test_verify_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "other-branch")
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.verify_pr(
            tmp_path, "orbi/issue-4", "main", "e07383c2", issue=4, repo_dir=tmp_path,
        )


FAKE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
FAKE_RUN_ID = "e07383c2"


FAKE_PR_URL = "https://github.com/muyantech/orbi/pull/4"
FAKE_PR_REPO = "muyantech/orbi"


def fake_verify_pr_payload(**overrides) -> str:
    """One open PR in the production `gh pr list` shape."""
    payload = {
        "url": FAKE_PR_URL,
        "baseRefName": "main",
        "headRefName": f"orbi/issue-4-{FAKE_RUN_ID}",
        "headRefOid": FAKE_HEAD_SHA,
        "headRepository": {"name": "orbi"},
        "headRepositoryOwner": {"login": "muyantech"},
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
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
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
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
    assert "base_branch=main" in caplog.text


def test_fake_verify_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_verify_run(["gh", "release", "list"])


def test_verify_pr_rejects_missing_pr(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[]",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )


def test_verify_pr_rejects_non_array(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "{}",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )


def test_verify_pr_returns_url_when_delivery_contains_latest_base(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path,
    ) == "https://github.com/muyantech/orbi/pull/4"
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls


def test_verify_pr_requires_the_repo_dir_lock_location(
    monkeypatch, tmp_path,
):
    # Issue #171: the verify fetch updates the shared remote-tracking
    # ref, so the lock location (the deployment checkout's shared state
    # dir) must be explicit — there is no bypass path.
    monkeypatch.setattr(runner, "run_command", fake_verify_run)
    with pytest.raises(TypeError):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4,
        )


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
        FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
    )
    assert held == [True]
    assert _probe_lock_free(tmp_path) is True


def test_verify_pr_rejects_pr_without_url(monkeypatch, tmp_path):
    outputs = iter([
        f"orbi/issue-4-{FAKE_RUN_ID}", "", "", FAKE_HEAD_SHA, "[{}]",
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: next(outputs))
    with pytest.raises(RuntimeError, match="open PR has no URL"):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )


def test_verify_pr_rejects_pr_based_on_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(baseRefName="develop")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR base is develop, expected main",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError,
        match="PR head deadbeef.* is not local HEAD 01234567.*diverged",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        url = runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
    assert url == FAKE_PR_URL
    assert "local_head_ahead_of_pr_head" in caplog.text
    assert "ed72915ed72915ed72915ed72915ed72915ed7291" in caplog.text
    assert FAKE_HEAD_SHA in caplog.text


def test_verify_pr_rejects_pr_body_without_run_marker(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body="no run marker here")
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
    assert "pr_run_marker_missing" in caplog.text


def test_verify_pr_rejects_pr_body_missing_field(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(body=None)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="missing the stable run marker",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path,
    ) == FAKE_PR_URL


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=r"missing `Fixes #4`",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path,
        )
    assert "pr_fixes_missing" in caplog.text


def test_verify_pr_queries_base_head_and_accepts_matching_pr(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path,
    ) == "https://github.com/muyantech/orbi/pull/4"
    assert ["git", "rev-parse", "HEAD"] in calls
    assert [
        "gh", "pr", "list", "--state", "open", "--head",
        f"orbi/issue-4-{FAKE_RUN_ID}",
        "--json", (
            "url,baseRefName,headRefName,headRefOid,"
            "headRepository,headRepositoryOwner,body"
        ),
        "--limit", "2",
    ] in calls


# ------------------------------------------- repo + expected URL (F1, F3)


def test_verify_pr_accepts_pr_in_expected_repo_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", fake_verify_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path, pr_repo=FAKE_PR_REPO, expected_url=FAKE_PR_URL,
    ) == FAKE_PR_URL


def test_verify_pr_rejects_pr_head_in_another_repo(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={"name": "other"},
                headRepositoryOwner={"login": "attacker"},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="PR head repo is attacker/other, expected "
                            "muyantech/orbi",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path, pr_repo=FAKE_PR_REPO,
        )
    assert "pr_repo_mismatch" in caplog.text


def test_verify_pr_rejects_pr_head_repo_missing_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository=None, headRepositoryOwner=None,
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "muyantech/orbi",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path, pr_repo=FAKE_PR_REPO,
        )


def test_verify_pr_rejects_pr_head_repo_empty_fields(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                headRepository={}, headRepositoryOwner={},
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(
        RuntimeError, match="PR head repo is <missing>, expected "
                            "muyantech/orbi",
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path, pr_repo=FAKE_PR_REPO,
        )


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path,
    ) == FAKE_PR_URL


def test_verify_pr_rejects_url_different_from_expected(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return fake_verify_pr_payload(
                url="https://github.com/muyantech/orbi/pull/99",
            )
        return fake_verify_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match=(
            "PR URL https://github.com/muyantech/orbi/pull/99 is "
            "not the recovered original PR "
            "https://github.com/muyantech/orbi/pull/4"
        ),
    ):
        runner.verify_pr(
            tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main",
            FAKE_RUN_ID, issue=4, repo_dir=tmp_path, expected_url=FAKE_PR_URL,
        )
    assert "pr_url_mismatch" in caplog.text


def test_verify_pr_skips_url_check_when_expected_url_not_given(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runner, "run_command", fake_verify_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path,
    ) == FAKE_PR_URL


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.verify_pr(
        tmp_path, f"orbi/issue-4-{FAKE_RUN_ID}", "main", FAKE_RUN_ID,
        issue=4, repo_dir=tmp_path, require_latest_base=False,
    ) == FAKE_PR_URL
    assert not any(c[:3] == ["git", "fetch", "origin"] for c in calls)
    assert not any(
        c[:3] == ["git", "merge-base", "--is-ancestor"] for c in calls
    )


def test_process_issue_success_records_base_and_run_in_comment(monkeypatch, tmp_path):
    calls = []
    gh_calls, posted = make_fake_gh(monkeypatch)
    branch = "orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:2] == ["gh", "issue"]:
            # Scene comments (started Pi / opened PR) go through
            # comment_issue -> run_command; record them like the others.
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/muyantech/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "muyantech"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            return head
        # git fetch / git merge-base: no output needed.
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}
    assert runner.process_issue(issue, config, "xqliu/muyan-ceo") == "https://github.com/muyantech/orbi/pull/4"
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/muyan-ceo", "add": "ai-in-progress"})
    # The run state is published automatically: exactly one progress
    # comment (hidden run marker) plus the started / PR opened milestones.
    progress_posts = [
        body for body in posted
        if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- branch: orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4" in progress_posts[0]
    # The PR URL is only known after verify_pr: the initial POST shows
    # `- PR: -`, the final delivery PATCH carries the URL.
    assert "- PR: -" in progress_posts[0]
    assert any("Orbi: started" in body for body in posted)
    started = [body for body in posted if "Orbi: started" in body][0]
    assert "base_branch=main" in started
    assert "base_sha=abc123def456" in started
    assert "run_id=a1b2c3d4" in started
    assert "branch=orbi/xqliu-muyan-ceo-issue-4-a1b2c3d4" in started
    assert "worktree=" + str(tmp_path / "wt") in started
    pr_opened = [body for body in posted if "Orbi: PR opened" in body]
    assert pr_opened
    assert "https://github.com/muyantech/orbi/pull/4" in pr_opened[0]
    assert "run_id=a1b2c3d4" in pr_opened[0]
    # The scene comments (started Pi / opened PR) still carry the run scene.
    scene_comments = [
        call for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    assert len(scene_comments) == 2
    start_body = scene_comments[0][-1]
    assert "Orbi started Pi:" in start_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in start_body
    opened_body = scene_comments[1][-1]
    assert "Orbi opened PR: https://github.com/muyantech/orbi/pull/4" in opened_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in opened_body
    # The final delivery summary PATCHed the same progress comment.
    patches = [
        command for command in gh_calls
        if command[:2] == ["gh", "api"] and "PATCH" in command
    ]
    assert patches
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi delivered" in last_body


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
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: "https://github.com/muyantech/orbi/pull/4")
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(runner, "run_command", fake_run)
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
    assert "pr=https://github.com/muyantech/orbi/pull/4" in ends[0]
    assert "commit=0123456789abcdef0123456789abcdef01234567" in ends[0]


def test_process_issue_failure_marks_blocked_and_ends_cleanly(monkeypatch, tmp_path):
    """Issue #239: a delivery failure is terminal — `process_issue` marks
    the Issue `ai-blocked`, posts the `Orbi failed` comment, and
    RETURNS `None` instead of re-raising. The service must not crash on
    an already-handled failure; the tick ends cleanly and `main` skips
    the delivery wait."""
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo") is None
    assert calls[1][2] == {"repo": "xqliu/muyan-ceo", "add": "ai-blocked", "remove": "ai-in-progress"}
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
    assert "Orbi failed: git failed" in failure_body
    assert "base_branch=main" in failure_body
    assert "base_sha=abc123def456" in failure_body
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: worktree,
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    no_commit = RuntimeError(
        "the agent delivered no commit on the task branch (HEAD "
        "abc123def456 is still the frozen base abc123def456)"
    )

    def dead_deliver_pr(*args, **kwargs):
        raise no_commit

    monkeypatch.setattr(runner, "deliver_pr", dead_deliver_pr)
    monkeypatch.setattr(
        runner, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it.
    assert runner.process_issue(
        {"number": 239, "title": "No commit", "body": ""},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/orbi",
    ) is None
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
        if "Orbi failed:" in body
    ]
    assert len(failure) == 1
    assert "delivered no commit" in failure[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure[0]
    # The blocked milestone notification carries the same scene.
    blocked = [body for body in posted if "Orbi: blocked" in body]
    assert blocked
    assert "delivered no commit" in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]


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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(runner, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        runner, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The failure is recoverable: `process_issue` returns `None` (no PR)
    # and does NOT re-raise — the service must not crash on it (Issue
    # #239), and the Issue must NOT be marked `ai-blocked` (Issue #227).
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/orbi",
    ) is None
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
    recovered = [
        body for body in comment_bodies
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(runner, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        runner, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if (command[:3] == ["gh", "issue", "comment"]
                and "model_wait recovered" in command[-1]):
            raise RuntimeError(
                "gh issue comment failed: API rate limit exceeded",
            )
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/orbi",
    ) is None
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "create_worktree", Mock(return_value=tmp_path),
    )
    idle_recovery = RuntimeError(
        "Pi session stayed idle for 15m after idle recovery (TERM/KILL "
        "of pre-idle descendants); Pi was killed (Issue #94)"
    )

    def dead_run_pi(*args, **kwargs):
        raise idle_recovery

    monkeypatch.setattr(runner, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        runner, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue(
        {"number": 94, "title": "Idle recovery", "body": ""},
        {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
         "base_branch": "main"},
        "xqliu/orbi",
    ) is None
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
        if "Orbi failed:" in body
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

    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edit_calls.append(kwargs),
    )
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
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
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not issue-comment/list traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    with caplog.at_level("ERROR"):
        # The failure is terminal: `process_issue` returns `None` (no PR)
        # and does NOT re-raise — the service must not crash on it
        # (Issue #239).
        assert runner.process_issue({"number": 13, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo") is None
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
        {"repo": "xqliu/muyan-ceo", "add": "ai-in-progress"},
        {"repo": "xqliu/muyan-ceo", "add": "ai-blocked",
         "remove": "ai-in-progress"},
    ]


def _write_prompts(tmp_path):
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
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
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(runner, "pick_next_delivery", fake_pick)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] == "v0.2.0"


def test_main_passes_none_active_milestone_when_unconfigured(
    monkeypatch, tmp_path,
):
    """Issue #139 compat: without the config field the claim scan
    receives None and keeps the pre-#139 scans."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(runner, "pick_next_delivery", fake_pick)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] is None


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "xqliu/orbi", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or "https://github.com/x/y/pull/12")
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue
    # The slot is held through the delivery wait (implement -> review ->
    # fix -> merge), not released when the PR opens (Issue #39).
    assert waits[0][0][:2] == (
        "https://github.com/x/y/pull/12", issue,
    )


def test_main_ends_tick_when_process_issue_delivers_nothing(
    monkeypatch, tmp_path,
):
    """Issue #239: when `process_issue` returns `None` (a terminal
    delivery failure it already handled — the Issue is `ai-blocked` and
    the failure comment is posted), `main` ends the tick cleanly: it
    returns 0 and NEVER calls `wait_for_delivery` (there is no PR to
    wait for). The service must not crash on the handled failure."""
    issue = {"number": 239, "title": "task", "body": "body"}
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "wait_for_delivery",
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
        "labels": [{"name": "ai-ready"}, {"name": "ai-ticket-only"}],
    }
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: "ticket-only")
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *args: (_ for _ in ()).throw(
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
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: release_url)
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *args: waits.append(args) or (
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
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
        runner, "wait_for_delivery",
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
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
        runner, "wait_for_delivery",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer for a clean PR: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene["pr_url"], issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_accepts_repeated_source_repo(monkeypatch, tmp_path):
    _write_prompts(tmp_path)
    seen = []
    issue = {"number": 14, "title": "task"}
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"xqliu/orbi\", \"xqliu/muyan-ceo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            seen.append(repos) or (repos[0], issue, None)
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: "https://github.com/x/y/pull/14")
    monkeypatch.setattr(runner, "wait_for_delivery", lambda *a, **k: None)
    assert runner.main([
        "--config", str(config),
    ]) == 0
    assert seen == [["xqliu/orbi", "xqliu/muyan-ceo"]]


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
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
    monkeypatch.setattr(runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pi died")),
    )
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo") is None
    failure_body = calls[-1][2]["body"]
    # No session file yet: the scene still carries the full debug entry
    # (worktree, branch) with '-' session fields.
    assert f"worktree={tmp_path / 'wt'}" in failure_body
    assert "branch=orbi/xqliu-muyan-ceo-issue-8-a1b2c3d4" in failure_body
    assert "session=-" in failure_body
    assert "session_file=-" in failure_body


def test_process_issue_failure_comment_includes_session_scene(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="boom"),
        ),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-9",
        "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
        "phase": "test",
        "last_activity": "2026-08-25T02:30:00Z",
        "action": "bash pytest tests/",
        "result": "ok",
    })
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo") is None
    failure_body = calls[-1][2]["body"]
    assert "Orbi failed:" in failure_body
    assert "session=sess-9" in failure_body
    assert "phase=test" in failure_body
    assert "last_activity=2026-08-25T02:30:00Z" in failure_body
    assert 'action="bash pytest tests/"' in failure_body
    assert "result=ok" in failure_body
    # The full scene on the failure comment carries the debug entry.
    assert f"worktree={tmp_path / 'wt'}" in failure_body
    assert "branch=orbi/xqliu-muyan-ceo-issue-8-a1b2c3d4" in failure_body


def test_process_issue_isolates_scene_lookup_failure(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The main-path resume-context read (Issue #219) succeeds; the
    # failure-scene lookup is the one that dies on the disk error.
    snapshot_calls = []

    def flaky_snapshot(session_dir):
        snapshot_calls.append(1)
        if len(snapshot_calls) == 1:
            return None
        raise OSError("disk error")

    monkeypatch.setattr(runner, "activity_snapshot", flaky_snapshot)
    with caplog.at_level("ERROR"):
        # Issue #239: the failure is terminal — `process_issue` returns
        # `None` instead of re-raising; the scene-isolation assertions
        # below are unchanged.
        assert runner.process_issue({"number": 9, "title": "Fail", "body": ""}, {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md", "base_branch": "main"}, "xqliu/muyan-ceo") is None
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "Orbi failed: git failed" in failure_body
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


def fresh_timestamp(offset: float = 0.0) -> str:
    """A session timestamp at (real) now + offset.

    The watcher computes `stale_seconds` against the real clock, so the
    fake session records must carry fresh timestamps: a fixed 2026 date
    would look years stale and trip the 5-minute idle warning (Issue
    #18) in every stream_pi test.
    """
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset)
    ).isoformat()


def fake_session_records():
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.2, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]


def test_log_format_has_no_python_timestamp():
    # journald already provides time, host and process (Issue #40): the
    # Python logger must not print a second timestamp.
    formatter = logging.Formatter(runner.log_format())
    record = logging.LogRecord(
        "orbi.bootstrap", logging.INFO, "file", 1, "message", None, None,
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="orbi/xqliu-orbi-issue-24-run1",
        )
    assert result == "final answer"
    # Without an explicit log_command the raw command is never logged.
    assert "command=<redacted>" in caplog.text
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    start = starts[0]
    assert "run=run1" in start
    assert "issue=xqliu/orbi#24" in start
    assert "role=implement" in start
    assert "branch=orbi/xqliu-orbi-issue-24-run1" in start
    assert f"worktree={tmp_path}" in start
    # The session fields are part of the scene; before Pi writes its first
    # record they are '-' (the full entry reappears on run_failed).
    assert "session=-" in start
    assert "session_file=-" in start
    # Issue #176: the scene at start shows the startup sub-phase — no
    # session file yet, so `session_pending` (not the generic
    # `starting`).
    assert "phase=session_pending" in start
    # The user message (full prompt / Issue body) never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_run_start_never_follows_pre_existing_session_file(
    tmp_path, caplog,
):
    # A resumed run starts in a worktree where the previous invocation's
    # session JSONL already exists (Issue #45): the watcher never binds to
    # it, so run_start reports no session until the current invocation
    # creates its own JSONL.
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    starts = [line for line in caplog.text.splitlines()
              if " run_start " in line]
    assert len(starts) == 1
    # The pre-existing file belongs to the previous invocation.
    assert "session=-" in starts[0]
    assert "session_file=-" in starts[0]
    assert "sess-1" not in caplog.text


def test_stream_pi_logs_activity_and_heartbeat_lines(tmp_path, caplog):
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer", sleep=0.3,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    heartbeats = [line for line in lines if " heartbeat " in line]
    # Issue #176: the visible fields change twice (request_pending ->
    # test): the startup sub-phase line, then the tool-call line;
    # unchanged polls must not repeat them (Issue #40).
    assert len(activities) == 2
    assert "phase=request_pending" in activities[0]
    line = activities[1]
    # No redundant `run=` field on the high-frequency lines (Issue #57);
    # the `[run_id]` prefix is the run-id carrier (bound-run tests and
    # the e2e suite cover the prefix itself).
    assert "run=run1" not in line
    assert "issue=xqliu/orbi#24" in line
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
        assert "run=run1" not in line
        assert "role=implement" in line
        assert ("phase=request_pending" in line
                or "phase=test" in line)
        assert "elapsed=" in line
        assert "idle=" in line
        assert "branch=" not in line
        assert f"worktree={tmp_path}" not in line
    # The legacy verbose line is gone.
    assert "pi_activity" not in caplog.text
    assert "pi_idle" not in caplog.text


def test_stream_pi_high_frequency_lines_carry_run_id_exactly_once(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: the `[run_id]` prefix (Issue #41) is the single
    run-id carrier on the high-frequency lines; the redundant `run=`
    field is gone, so the 8-hex id appears exactly once per line and a
    single grep still reconstructs the whole timeline. The run is
    bound like in the real journal (`process_issue` binds it before
    starting Pi)."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        (1.2, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(3),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="a1b2c3d4", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    kinds = (
        "activity", "heartbeat", "model_wait", "resumed",
        "pi_idle", "pi_resumed",
    )
    high_freq = [
        message for message in caplog.messages
        if any(f" {kind} " in message for kind in kinds)
    ]
    # The record set produces every high-frequency kind except the idle
    # pair (activity + heartbeats, one model_wait, one resumed).
    assert any(" activity " in m for m in high_freq)
    assert any(" heartbeat " in m for m in high_freq)
    assert any(" model_wait " in m for m in high_freq)
    assert any(" resumed " in m for m in high_freq)
    for message in high_freq:
        assert "run=" not in message, message
        assert message.startswith("[a1b2c3d4]"), message
        assert message.count("a1b2c3d4") == 1, message


def test_stream_pi_idle_lines_carry_run_id_exactly_once(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: `pi_idle` / `pi_resumed` repeat the same rule as the
    other high-frequency lines: prefix only, no `run=` field."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    # a1 is 4s stale when it is polled, so the idle warning fires;
    # a2 arrives later with a fresh timestamp, so the resume does not
    # re-trigger the warning.
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(-5), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(-4),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "one"}]}}),
        (1.0, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(1.0),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "two"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.5,
            run_id="a1b2c3d4", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    idles = [m for m in caplog.messages if " pi_idle " in m]
    resumed = [m for m in caplog.messages if " pi_resumed " in m]
    assert len(idles) == 1
    assert len(resumed) == 1
    for message in idles + resumed:
        assert "run=" not in message, message
        assert message.startswith("[a1b2c3d4]"), message
        assert message.count("a1b2c3d4") == 1, message


def test_stream_pi_scene_lines_keep_run_field_for_parse_scene(
    tmp_path, monkeypatch, caplog,
):
    """Issue #57: the low-frequency scene lines (run_start / run_failed)
    keep `run=` so `pi_activity.parse_scene` still returns the run id
    from the lines that need to be parsed. The run is bound like in
    the real journal."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stderr="pi exploded", exit_code=3,
    )
    with caplog.at_level("INFO"), pytest.raises(
        subprocess.CalledProcessError,
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="a1b2c3d4", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    starts = [m for m in caplog.messages if " run_start " in m]
    failures = [m for m in caplog.messages if " run_failed " in m]
    assert len(starts) == 1 and len(failures) == 1
    for message in starts + failures:
        assert "run=a1b2c3d4" in message, message
        fields = pi_activity.parse_scene(message)
        assert fields["run"] == "a1b2c3d4"
        assert fields["issue"] == "xqliu/orbi#24"


def test_stream_pi_activity_keeps_action_after_tool_result(tmp_path, caplog):
    """A tool result updates result only; the action line is not repeated."""
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    activities = [line for line in lines if " activity " in line]
    # Issue #176: one activity line for the startup sub-phase, one for
    # the tool call, one for the result=ok update; the action (the real
    # command) is preserved on the tool lines.
    assert len(activities) == 3
    assert "phase=request_pending" in activities[0]
    assert all('action="bash pytest tests/"' in line
               for line in activities[1:])
    assert "result=-" in activities[1]
    assert "result=ok" in activities[2]
    assert "tool_result" not in caplog.text


def test_stream_pi_heartbeat_interval_is_stable(tmp_path, caplog):
    """A silent session emits one heartbeat per poll interval, no activity."""
    command = make_fake_pi(
        tmp_path, session_records=[], sleep=1.0,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    heartbeats = [line for line in lines if " heartbeat " in line]
    activities = [line for line in lines if " activity " in line]
    assert activities == []
    # ~1s of idleness at a 0.1s interval: several heartbeats, one per poll.
    # Issue #176: with no session file the sub-phase is session_pending.
    assert len(heartbeats) >= 4
    for line in heartbeats:
        assert "phase=session_pending" in line
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
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
    assert "issue=xqliu/orbi#24" in failure
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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    # Idle duration is visible on the heartbeat line itself.
    assert any("idle=" in line for line in heartbeats)


def test_stream_pi_heartbeats_when_no_session_file_appears(tmp_path, caplog):
    # Issue #176: with no session file the startup sub-phase is
    # `session_pending` (Pi is spawned but has not created its session
    # yet) — the generic `starting` is gone from the live lines.
    command = make_fake_pi(tmp_path, session_records=[], sleep=1.0)
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    heartbeats = [line for line in caplog.text.splitlines()
                  if " heartbeat " in line]
    assert len(heartbeats) >= 1
    assert all("phase=session_pending" in line for line in heartbeats)


# ------------------------------------ startup phases (Issue #176)

def startup_records():
    """A full healthy startup: session, the model Pi selected, the first
    request (user message) and the first response (assistant message)."""
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.05, {"type": "model_change", "id": "m1",
                "timestamp": fresh_timestamp(),
                "provider": "local-qwen", "modelId": "qwen3.8:27b"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.2, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]


def test_stream_pi_logs_process_spawned_after_popen(tmp_path, caplog):
    """`process_spawned` is logged exactly once, right after the Pi
    process is spawned, and carries the pid (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(), stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = [line for line in caplog.text.splitlines()
             if " process_spawned " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "issue=xqliu/orbi#24" in line
    assert "role=implement" in line
    # Before the session file exists the selection is unknown.
    assert "provider=-" in line
    assert "model=-" in line
    assert "elapsed=" in line
    # The pid is a real integer and the raw prompt never reaches the log.
    pid = int(line.split("pid=")[1].split()[0])
    assert pid > 0
    assert "SECRET ISSUE BODY" not in caplog.text


def test_stream_pi_logs_startup_milestones_in_order(tmp_path, caplog):
    """A healthy startup logs `session_created`, `first_request_started`
    and `first_response_received` exactly once each, in order, and the
    lines carry the provider/model Pi actually selected (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(), stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    created = [line for line in lines if " session_created " in line]
    requested = [line for line in lines if " first_request_started " in line]
    responded = [line for line in lines if " first_response_received " in line]
    assert len(created) == 1
    assert len(requested) == 1
    assert len(responded) == 1
    # The milestones appear in the order the session records arrive.
    assert (lines.index(created[0]) < lines.index(requested[0])
            < lines.index(responded[0]))
    # The real selection (from the session's model_change record) is
    # visible on the request/response lines.
    assert "provider=local-qwen" in requested[0]
    assert "model=qwen3.8:27b" in requested[0]
    assert "provider=local-qwen" in responded[0]
    assert "model=qwen3.8:27b" in responded[0]
    for line in (created[0], requested[0], responded[0]):
        assert "issue=xqliu/orbi#24" in line
        assert "role=implement" in line
        assert "elapsed=" in line


def test_stream_pi_activity_lines_show_startup_sub_phase(tmp_path, caplog):
    """The live activity/heartbeat lines show the startup sub-phase
    instead of the generic `starting` (Issue #176): `session_pending`
    until the session file exists, `request_pending` until the first
    response, then the tool-based phase. The records are delayed so
    each sub-phase is visible for at least one poll."""
    records = [
        (0.3, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.45, {"type": "model_change", "id": "m1",
                "timestamp": fresh_timestamp(),
                "provider": "local-qwen", "modelId": "qwen3.8:27b"}),
        (0.6, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
        (0.9, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    live = [line for line in lines
            if " activity " in line or " heartbeat " in line]
    assert any("phase=session_pending" in line for line in live)
    assert any("phase=request_pending" in line for line in live)
    # After the first response the tool-based phase applies as before.
    assert any("phase=test" in line for line in live)


def test_stream_pi_startup_failed_without_session_file(tmp_path, caplog):
    """Pi exits before creating its session file: `startup_failed`
    carries `reason=session_not_created` (and the existing `run_failed`
    line is unchanged, Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[], stdout="", stderr="boom", exit_code=3,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "reason=session_not_created" in line
    assert "session_created=false" in line
    assert "first_request=false" in line
    assert "issue=xqliu/orbi#24" in line
    assert "role=implement" in line
    assert "elapsed=" in line
    # The existing fail-fast scene line is unchanged.
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=pi_exit_3" in failed[0]


def test_stream_pi_startup_failed_without_first_request(tmp_path, caplog):
    """The session file exists but Pi never sent its first request:
    `reason=no_first_request` (Issue #176)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=no_first_request" in lines[0]
    assert "session_created=true" in lines[0]
    assert "first_request=false" in lines[0]


def test_stream_pi_startup_failed_early_exit_after_first_request(
        tmp_path, caplog,
):
    """The first request went out but Pi exited early without a first
    response (unclassifiable stderr): `reason=pi_exit_<N>` with the
    stuck-point fields (Issue #176)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "u1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "SECRET ISSUE BODY"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=pi_exit_1" in lines[0]
    assert "session_created=true" in lines[0]
    assert "first_request=true" in lines[0]
    # The prompt never reaches the journal.
    assert "SECRET ISSUE BODY" not in caplog.text


def test_startup_failed_reason_maps_hung_first_request_to_timeout():
    """A frozen model_wait killed before the first response is the
    first-response timeout (Issue #176): the request was in flight and
    the response never arrived."""
    activity = {
        "session_file": "/w/.pi-session/s.jsonl",
        "first_request": True,
        "first_response": False,
    }
    assert runner._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=True,
        model_wait_swallowed=False, idle_recovery_failed=False,
    ) == "first_response_timeout"
    assert runner._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=False,
        model_wait_swallowed=True, idle_recovery_failed=False,
    ) == "model_wait_swallowed"
    # The other failure classes keep their own reasons.
    assert runner._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=True, model_wait_dead=False,
        model_wait_swallowed=False, idle_recovery_failed=False,
    ) == "timeout"
    assert runner._startup_failed_reason(
        activity, returncode=0, stderr="",
        timed_out=False, model_wait_dead=False,
        model_wait_swallowed=False, idle_recovery_failed=True,
    ) == "idle_recovery_stale"


def test_stream_pi_startup_failed_auth_failure(tmp_path, caplog):
    """A provider authentication failure in Pi's stderr is classified as
    `reason=auth_failure` (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[], stderr="Error: 401 Unauthorized",
        exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=auth_failure" in lines[0]
    assert "session_created=false" in lines[0]


def test_stream_pi_startup_failed_network_timeout(tmp_path, caplog):
    """A network timeout in Pi's stderr is classified as
    `reason=network_timeout` (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=[],
        stderr='Error: connect ETIMEDOUT (request timed out)',
        exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    lines = [line for line in caplog.text.splitlines()
             if " startup_failed " in line]
    assert len(lines) == 1
    assert "reason=network_timeout" in lines[0]


def test_stream_pi_no_startup_failed_after_first_response(tmp_path, caplog):
    """A failure AFTER the first response is a mid-run failure, not a
    startup failure: no `startup_failed` line (Issue #176)."""
    command = make_fake_pi(
        tmp_path, session_records=startup_records(),
        stderr="boom", exit_code=1,
    )
    with caplog.at_level("INFO"):
        with pytest.raises(subprocess.CalledProcessError):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                run_id="run1", issue=24, source_repo="xqliu/orbi",
                branch="b",
            )
    assert not any(" startup_failed " in line
                   for line in caplog.text.splitlines())
    # The existing run_failed scene line still carries the reason.
    failed = [line for line in caplog.text.splitlines()
              if " run_failed " in line]
    assert len(failed) == 1
    assert "reason=pi_exit_1" in failed[0]


def test_stream_pi_model_wait_then_resumed_no_warning_spam(
    tmp_path, caplog,
):
    """Regression (Issue #40): a long model response after a tool result
    must be reported as `state=model_wait`, not as idle. Exactly one
    transition line when entering model_wait, one `resumed` line when the
    next assistant event arrives, and only configured-interval heartbeats
    while waiting — no WARNING/ERROR spam (a slow model is not a stalled
    agent)."""
    records = fake_session_records() + [
        (0.5, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        (1.2, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(3),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    # One transition into model_wait, one transition back on resume.
    waits = [line for line in lines if " model_wait " in line]
    resumed = [line for line in lines if " resumed " in line]
    assert len(waits) == 1
    assert len(resumed) == 1
    wait = waits[0]
    # The transition lines carry no redundant `run=` field (Issue #57);
    # the `[run_id]` prefix is the run-id carrier.
    assert "run=run1" not in wait
    assert "issue=xqliu/orbi#24" in wait
    assert "role=implement" in wait
    assert "phase=test" in wait
    assert "state=model_wait" in wait
    # The full scene never rides on the transition lines.
    assert "branch=" not in wait
    assert f"worktree={tmp_path}" not in wait
    resume = resumed[0]
    assert "run=run1" not in resume
    assert "state=resumed" in resume
    assert "phase=test" in resume
    # While waiting, the heartbeats carry the model_wait state and the
    # idle time (the wait duration is visible on the line itself)...
    wait_heartbeats = [
        line for line in lines
        if " heartbeat " in line and "state=model_wait" in line
    ]
    assert len(wait_heartbeats) >= 1
    for line in wait_heartbeats:
        assert "idle=" in line
        assert "elapsed=" in line
    # ...and nothing is escalated: no WARNING/ERROR while the model
    # responds (no warning spam, no stalled inference).
    assert "WARNING" not in caplog.text
    assert "ERROR" not in caplog.text


def test_stream_pi_no_model_wait_after_assistant_text(tmp_path, caplog):
    # model_wait is only entered when the last session event is a tool
    # result; an assistant text (e.g. the final answer) must not trigger
    # the transition.
    records = fake_session_records() + [
        (0.4, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.5,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    assert " model_wait " not in caplog.text
    assert " resumed " not in caplog.text


def test_stream_pi_idle_warn_default_is_five_minutes():
    # The Issue #18 contract: no model/session activity for 5 minutes
    # logs an idle warning.
    assert runner.PI_IDLE_WARN_SECONDS == 300.0


def test_stream_pi_logs_idle_warning_once_when_session_stalls(
    tmp_path, caplog,
):
    """Issue #18 acceptance: a stalled session (no model/session
    activity past the threshold, not waiting on the model) logs ONE
    `pi_idle` WARNING carrying `stale_seconds` — never a warning per
    heartbeat."""
    command = make_fake_pi(tmp_path, session_records=[], sleep=1.2)
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.5,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    assert len(idles) == 1, f"exactly one idle warning: {lines}"
    idle = idles[0]
    # No redundant `run=` field (Issue #57); the `[run_id]` prefix is
    # the run-id carrier.
    assert "run=run1" not in idle
    assert "issue=xqliu/orbi#24" in idle
    assert "role=implement" in idle
    # Issue #176: no session file was ever created, so the sub-phase is
    # session_pending (the stall is visible with its stuck point).
    assert "phase=session_pending" in idle
    assert "stale_seconds=" in idle
    # The warning is a WARNING (visible in journalctl without -p info).
    assert any(
        record.levelno == logging.WARNING
        and " pi_idle " in record.getMessage()
        for record in caplog.records
    )


def test_stream_pi_logs_pi_resumed_after_idle_warning(tmp_path, caplog):
    """The first new session event after an idle warning logs
    `pi_resumed` (Issue #18: 恢复后输出 resumed)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.8, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "back"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="ok",
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.4,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(idles) == 1
    assert len(resumed) == 1
    # resumed comes after the warning and carries no redundant `run=`
    # field (Issue #57).
    assert lines.index(resumed[0]) > lines.index(idles[0])
    assert "run=run1" not in resumed[0]
    assert "issue=xqliu/orbi#24" in resumed[0]
    assert "role=implement" in resumed[0]
    assert "phase=starting" in resumed[0]


def test_stream_pi_no_idle_warning_during_model_wait(tmp_path, caplog):
    """Issue #40 semantics: while the newest session event is a tool
    result the model is expected to reply next — a long silence is a
    slow model, never an idle warning (no warning spam)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, sleep=1.2,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.5,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    assert " pi_idle " not in caplog.text
    # The silence is visible as the model_wait state instead.
    waits = [line for line in caplog.text.splitlines()
             if " model_wait " in line]
    assert len(waits) == 1


def test_stream_pi_resumed_run_follows_new_session_file(tmp_path, caplog):
    """Regression (Issue #45 round-5 review, Major 3): a resumed Fixer
    starts in the original worktree where the previous implementer's
    session JSONL already exists. The activity journal must follow the
    NEW session file created by the current Pi process, not the old
    one (which would report the previous session as idle)."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    old = session_dir / "old-session.jsonl"
    with old.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "session", "id": "old-session",
            "timestamp": "2026-08-25T02:00:00Z", "cwd": "/w",
        }) + "\n")
    command = make_fake_pi(
        tmp_path,
        session_records=[
            (0.0, {"type": "session", "id": "new-session",
                   "timestamp": fresh_timestamp(), "cwd": "/w"}),
            (0.1, {"type": "message", "id": "a1",
                   "timestamp": fresh_timestamp(1),
                   "message": {"role": "assistant", "content": [
                       {"type": "toolCall", "id": "t1", "name": "bash",
                        "arguments": {"command": "pytest tests/"}}]}}),
        ],
        stdout="fixed",
    )
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run45", issue=45, source_repo="xqliu/orbi",
            branch="orbi/xqliu-orbi-issue-45-run45",
        )
    assert result == "fixed"
    # The journal follows the NEW session created by this invocation: its
    # tool call is reported (the old session has no messages, so binding to
    # it would show no phase/action at all)...
    assert "phase=test" in caplog.text
    assert 'action="bash pytest tests/"' in caplog.text
    # ...and never reports the pre-existing session as the live one.
    assert "old-session" not in caplog.text


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
            # A real Popen always carries the child pid (the
            # process_spawned line, Issue #176, logs it).
            self.pid = os.getpid()

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
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == "late stdout data"
    assert "stderr=late stderr data" in caplog.text


def test_stream_pi_hung_model_request_killed_when_upstream_gone(
    tmp_path, caplog,
):
    """Issue #218 (subsumes #75): the model is expected to reply next
    (model_wait) and the session JSONL is frozen past the dead
    threshold — the model request is hung (here the connection is even
    gone: the old #75 dead-upstream scene). The runner must kill Pi and
    fail fast (the normal failure path releases the slot): an infinite
    `model_wait` hang is not acceptable. This is not a business timeout:
    it only fires while the session file is frozen (stale seconds),
    never while events keep arriving."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ) as excinfo:
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            run_id="run1", issue=75, source_repo="xqliu/orbi",
            branch="b",
        )
    # The failure message names the hung model request and the stale
    # time.
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#75" in failures[0]
    assert f"worktree={tmp_path}" in failures[0]
    # The structured model_wait_dead line carries the evidence: no
    # live connection here.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=false" in dead[0]
    assert "reason=hung_model_request" in dead[0]
    # The kill happened fast (well before the child's 10 s sleep).
    assert "pi_idle" not in caplog.text


def test_stream_pi_model_wait_dead_default_is_thirty_minutes():
    # Issue #228: the default dead-request threshold is 30 minutes
    # (1800 s) — a slow local model (Qwen 27B, ~17 tokens/s,
    # llama-server request timeout 1200 s) must survive a 10-minute
    # complete-message silence under the default; a genuinely frozen
    # request is still bounded and releases the slot.
    assert runner.PI_MODEL_WAIT_DEAD_SECONDS == 1800.0


def _frozen_model_wait_records(stale_seconds: float):
    """Session records whose newest event (a toolResult) is
    `stale_seconds` in the PAST: the session JSONL is frozen in
    model_wait for that long without any real waiting (the watcher
    measures stale against the record timestamps, Issue #169)."""
    return [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(-stale_seconds), "cwd": "/w"}),
        (0.0, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(-stale_seconds),
               "message": {"role": "assistant", "content": [
                   {"type": "toolCall", "id": "t1", "name": "bash",
                    "arguments": {"command": "pytest tests/"}}]}}),
        (0.0, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(-stale_seconds),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]


def test_stream_pi_frozen_model_wait_just_before_default_survives(
    tmp_path, caplog,
):
    """Issue #228: a frozen model_wait of just under the 1800 s default
    (the #176/#175/#173/#168 scene: 10-minute complete messages on a
    slow local model) must NOT be killed under the default
    configuration — the Runner keeps waiting and the session finishes
    normally."""
    records = _frozen_model_wait_records(1799.0)
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=228, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text
    assert "model_wait_slow" not in caplog.text


def test_stream_pi_frozen_model_wait_at_default_kills_with_configured_threshold(
    tmp_path, caplog,
):
    """Issue #228: a frozen model_wait AT the 1800 s default still
    kills (the bound is inclusive) — and the structured line and the
    failure scene report the ACTUAL configured threshold (1800)."""
    records = _frozen_model_wait_records(1800.5)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=228, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "threshold=1800" in dead[0]
    assert "upstream_alive=" in dead[0]
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


def test_stream_pi_explicit_short_override_kills_before_default(
    tmp_path, caplog,
):
    """Issue #228: an explicit short override (a test setting) kills at
    the configured value, well before the 1800 s default — no real
    waiting, and the line reports the configured threshold."""
    records = _frozen_model_wait_records(1.5)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=1.0,
            run_id="run1", issue=228, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "threshold=1" in dead[0]


def test_stream_pi_frozen_model_wait_at_ten_minutes_survives_default(
    tmp_path, caplog,
):
    """Issue #228 regression: the #176/#175/#173/#168 scene — a frozen
    model_wait at 600 seconds (10 minutes, the pre-#228 default that
    marked those Issues ai-blocked) survives under the new 1800 s
    default: the Runner keeps waiting and does not signal Pi."""
    records = _frozen_model_wait_records(600.0)
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.3,
    )
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=228, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text


def test_stream_pi_slow_model_still_generating_is_not_killed(
    tmp_path, caplog,
):
    """Issue #75: the kill only fires while the session file is FROZEN
    (stale seconds past the threshold in model_wait). A model that
    keeps producing session events is not dead: every new record
    resets the stale time, so a long slow generation — even with a
    model_wait window that would have crossed the threshold had the
    file stayed frozen — survives without a kill."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
        # A new event arrives BEFORE the 0.4 s dead threshold: the
        # model is slow, not dead (the stale time resets).
        (0.3, {"type": "message", "id": "a2",
               "timestamp": fresh_timestamp(2),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "done"}]}}),
    ]
    command = make_fake_pi(
        tmp_path, session_records=records, stdout="final answer",
        sleep=0.8,
    )
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.4,
            run_id="run1", issue=75, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == "final answer"
    assert "model_wait_dead" not in caplog.text


def test_stream_pi_no_upstream_kill_before_model_wait(tmp_path, caplog):
    """Issue #75: the kill only applies while the model is expected to
    reply next (model_wait). A frozen session that is NOT waiting on
    the model (e.g. Pi itself stalled after an assistant message) is
    the existing `pi_idle` warning's territory, not a kill: the
    business task keeps running (no artificial timeout)."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "thinking out loud"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=1.2)
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            run_id="run1", issue=75, source_repo="xqliu/orbi",
            branch="b",
        )
    assert "model_wait_dead" not in caplog.text


# --- Issue #169: evidence-based stall detection ------------------------------


def make_upstream_listener(drop: bool = False):
    """A local TCP listener that mimics the upstream (llama/proxy).

    Returns `(port, stop)`: the listener accepts ONE connection and
    either holds it (a live upstream) or closes it immediately (the
    upstream died: the client socket goes CLOSE_WAIT). `stop()` closes
    the listener.
    """
    import socket as _socket
    import threading as _threading
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        if drop:
            conn.close()
        else:
            time.sleep(30)
        conn.close()  # idempotent: safe after the drop close

    thread = _threading.Thread(target=serve, daemon=True)
    thread.start()

    def stop():
        listener.close()

    return port, stop


def make_slow_model_pi(tmp_path, *, port: int, hold_seconds: float = 2.5,
                       drop_upstream: bool = False) -> list[str]:
    """Build a command that mimics a Pi with a HUNG model request
    (Issue #218): it writes the session toolCall + toolResult (the
    model is expected to reply next: model_wait), opens a TCP
    connection to the local upstream listener (the live model request)
    and holds it for `hold_seconds` without ever replying — the model
    service process is alive, the connection is alive, but the request
    never completes (the session JSONL stays frozen past the dead
    threshold). When `drop_upstream` the listener closes the connection
    immediately (the old #169 dead-upstream scene: the client socket
    leaves the live TCP states). After the hold it writes the assistant
    reply and exits — a runner that does not kill it gets to finish."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    script = (
        "import json, socket, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-slow', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'pytest tests/'}}]}})\n"
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': False,\n"
        "                   'content': [{'type': 'text', 'text': 'ok'}]}})\n"
        f"sock = socket.create_connection(('127.0.0.1', {port!r}))\n"
        f"time.sleep({hold_seconds!r})\n"
        "try:\n"
        "    sock.close()\n"
        "except OSError:\n"
        "    pass\n"
        "write({'type': 'message', 'id': 'a2', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'text', 'text': 'slow answer'}]}})\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def test_stream_pi_hung_model_request_killed_despite_live_upstream(
    tmp_path, caplog,
):
    """Issue #218 (the #183 scene; supersedes the #169/#158
    connection-alive exemption): the model_wait silence crosses the
    dead threshold while the model request is still CONNECTED to the
    upstream (a live TCP connection) — but the session JSONL is frozen
    and the model service process is alive without ever answering
    ("process alive ≠ responding"). A live connection is evidence for
    the journal, never a veto: the runner kills Pi and fails fast with
    the `model_wait_dead` reason — the slot is never held forever."""
    port, stop = make_upstream_listener(drop=False)
    try:
        command = make_slow_model_pi(
            tmp_path, port=port, hold_seconds=30.0,
        )
        with caplog.at_level("WARNING"), pytest.raises(
            RuntimeError, match="hung",
        ) as excinfo:
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                model_wait_dead_seconds=0.5,
                run_id="run1", issue=218, source_repo="xqliu/orbi",
                branch="b",
            )
    finally:
        stop()
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#218" in failures[0]
    # The structured model_wait_dead line carries the evidence: the
    # connection was still alive (upstream_alive=true) — process
    # alive ≠ responding.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=true" in dead[0]
    assert "reason=hung_model_request" in dead[0]
    # The kill is the failure path: no idle warning, no recovery.
    assert " pi_idle " not in caplog.text
    assert " pi_idle_term " not in caplog.text


def test_stream_pi_model_wait_dead_line_fields(tmp_path, caplog):
    """Issue #218 acceptance: the recovery logs ONE structured,
    grep-able `model_wait_dead` line with the full scene (like
    `unit_drift` / `transport_check_failed`): issue, idle seconds, the
    threshold, the action, the session, the run id, the upstream
    connection evidence and the reason. The line is parseable with
    `pi_activity.parse_scene`."""
    records = [
        (0.0, {"type": "session", "id": "sess-218",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            run_id="ab12cd34", issue=218, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    fields = pi_activity.parse_scene(dead[0])
    assert fields["issue"] == "xqliu/orbi#218"
    assert fields["role"] == "implement"
    assert fields["action"] == "kill_pi"
    assert fields["session"] == "sess-218"
    assert fields["run_id"] == "ab12cd34"
    assert fields["upstream_alive"] == "false"
    assert fields["reason"] == "hung_model_request"
    assert fields["threshold"] == "0"
    assert int(fields["idle_seconds"]) >= 0


def test_stream_pi_dropped_connection_model_wait_dead_upstream_false(
    tmp_path, caplog,
):
    """Issue #218: the upstream closed the connection (the client
    socket left the live TCP states — the old #169 dead-upstream
    scene) and the model_wait silence crossed the threshold: the same
    `model_wait_dead` path kills Pi and fails fast, with the
    connection evidence recorded as `upstream_alive=false`."""
    port, stop = make_upstream_listener(drop=True)
    try:
        command = make_slow_model_pi(
            tmp_path, port=port, hold_seconds=3.0, drop_upstream=True,
        )
        with caplog.at_level("WARNING"), pytest.raises(
            RuntimeError, match="hung",
        ):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                model_wait_dead_seconds=0.5,
                run_id="run1", issue=169, source_repo="xqliu/orbi",
                branch="b",
            )
    finally:
        stop()
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]
    assert "issue=xqliu/orbi#169" in failures[0]
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert "upstream_alive=false" in dead[0]
    # The kill is the failure path: no idle warning, no recovery.
    assert " pi_idle " not in caplog.text
    assert " pi_idle_term " not in caplog.text


# --- Issue #233: the /slots swallow probe kills fast -------------------------

def test_stream_pi_swallowed_model_request_killed_fast(tmp_path, caplog,
                                                        monkeypatch):
    """Issue #233 (the #231 scene): the model is expected to reply next
    (model_wait) and the /slots probe reports EVERY slot idle for the
    sustained grace — the request was accepted but never scheduled (the
    model process is alive, the connection ESTABLISHED, nothing
    generating). The runner kills Pi FAST (well before the
    model_wait_dead_seconds bound) and fails fast with the
    model_wait_swallowed reason."""
    monkeypatch.setattr(runner, "slots_idle", lambda url: True)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="swallowed",
    ) as excinfo:
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=10.0,
            model_wait_probe_url="http://127.0.0.1:18082/slots",
            model_wait_probe_seconds=0.5,
            run_id="run1", issue=233, source_repo="xqliu/orbi",
            branch="b",
        )
    assert "model_wait" in str(excinfo.value)
    lines = caplog.text.splitlines()
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_swallowed_idle_" in failures[0]
    assert "issue=xqliu/orbi#233" in failures[0]
    # The structured model_wait_swallowed line carries the evidence.
    swallowed = [line for line in lines if " model_wait_swallowed " in line]
    assert len(swallowed) == 1
    assert "reason=swallowed_model_request" in swallowed[0]
    assert "action=kill_pi" in swallowed[0]
    assert "probe_seconds=0" in swallowed[0]
    assert "upstream_alive=" in swallowed[0]
    # The fast path fired: the dead-request bound never did.
    assert " model_wait_dead " not in caplog.text


def test_stream_pi_swallow_line_fields(tmp_path, caplog, monkeypatch):
    """Issue #233 acceptance: the recovery logs ONE structured,
    grep-able `model_wait_swallowed` line with the full scene (like
    `model_wait_dead`): issue, idle seconds, the probe grace, the action,
    the session, the run id, the upstream connection evidence and the
    reason. The line is parseable with `pi_activity.parse_scene`."""
    monkeypatch.setattr(runner, "slots_idle", lambda url: True)
    records = [
        (0.0, {"type": "session", "id": "sess-233",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "r1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "toolResult", "toolCallId": "t1",
                           "toolName": "bash",
                           "content": [{"type": "text", "text": "ok"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=10.0,
            model_wait_probe_url="http://127.0.0.1:18082/slots",
            model_wait_probe_seconds=1.0,
            run_id="ab12cd34", issue=233, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    swallowed = [line for line in lines if " model_wait_swallowed " in line]
    assert len(swallowed) == 1
    fields = pi_activity.parse_scene(swallowed[0])
    assert fields["issue"] == "xqliu/orbi#233"
    assert fields["role"] == "implement"
    assert fields["action"] == "kill_pi"
    assert fields["session"] == "sess-233"
    assert fields["run_id"] == "ab12cd34"
    assert fields["reason"] == "swallowed_model_request"
    assert fields["probe_seconds"] == "1"
    assert int(fields["idle_seconds"]) >= 0


def test_stream_pi_swallow_not_fired_while_a_slot_is_processing(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233: the /slots probe reports a slot is processing
    (is_processing=true — the model is generating, a slow model, NOT a
    swallow): the swallow path never fires and the existing
    model_wait_dead_seconds bound still applies."""
    monkeypatch.setattr(runner, "slots_idle", lambda url: False)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            model_wait_probe_url="http://127.0.0.1:18082/slots",
            model_wait_probe_seconds=0.2,
            run_id="run1", issue=233, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    # The dead-request bound fired, not the swallow probe.
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


def test_stream_pi_swallow_probe_failure_is_inconclusive(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233 (bypass, Issue #79): the /slots probe fails (returns
    None — network/JSON error): the probe is inconclusive, never an
    error, and the existing model_wait_dead_seconds bound still applies
    (the delivery is not failed by the probe)."""
    monkeypatch.setattr(runner, "slots_idle", lambda url: None)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            model_wait_probe_url="http://127.0.0.1:18082/slots",
            model_wait_probe_seconds=0.2,
            run_id="run1", issue=233, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text


def test_stream_pi_unconfigured_probe_keeps_dead_bound(
    tmp_path, caplog, monkeypatch,
):
    """Issue #233: with no probe URL configured the swallow path is a
    no-op (the exact pre-#233 behavior) — the run is bounded by
    model_wait_dead_seconds only, and the probe is never called."""
    probe = Mock(return_value=True)
    monkeypatch.setattr(runner, "slots_idle", probe)
    records = _frozen_model_wait_records(0.0)
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("WARNING"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            run_id="run1", issue=233, source_repo="xqliu/orbi",
            branch="b",
        )
    # The probe was never called (no URL configured).
    probe.assert_not_called()
    lines = caplog.text.splitlines()
    dead = [line for line in lines if " model_wait_dead " in line]
    assert len(dead) == 1
    assert " model_wait_swallowed " not in caplog.text


def make_timeout_tool_pi(tmp_path, *, tool_seconds: float = 0.6,
                         react_on_tool_done: bool = True,
                         wrapper_honors_deadline: bool = True) -> list[str]:
    """Build a command that mimics a Pi running a LONG tool with an
    explicit `timeout` (Issue #169, the #105 regression): it writes the
    session toolCall, spawns `bash -c 'timeout <tool_seconds> sleep 300'`
    (the tool self-terminates at its deadline — no session event until
    then) and waits for it. When `wrapper_honors_deadline` is False the
    fake simulates a wrapper that FAILED to end its command (a fake
    `timeout` on PATH that ignores the duration: the sleep outlives the
    nominal deadline, the evidence flips back to stalled). When
    `react_on_tool_done` the tool's exit (deadline reached or killed)
    makes the fake Pi write the toolResult and exit; otherwise it stays
    silent (Pi itself is stuck)."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    react = (
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'tool finished'}]}})\n"
        if react_on_tool_done else "time.sleep(10)\n"
    )
    if wrapper_honors_deadline:
        tool_command = f"timeout {tool_seconds!r} sleep 300"
    else:
        # The nominal deadline is in the command line (what the runner
        # sees), but the wrapper never ends the command: a fake
        # `timeout` on PATH that ignores the duration and just runs the
        # command — the sleep runs on past the deadline.
        fake_bin = session_dir / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_timeout = fake_bin / "timeout"
        fake_timeout.write_text(
            "#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8",
        )
        fake_timeout.chmod(0o755)
        tool_command = (
            f"PATH={fake_bin!s}:/usr/bin:/bin "
            f"timeout {tool_seconds!r} sleep 30000"
        )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-timeout', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'timeout 0.6 sleep 300'}}]}})\n"
        f"child = subprocess.Popen(['bash', '-c', {tool_command!r}])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        f"{react}"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def test_stream_pi_timeout_tool_inside_deadline_not_killed(
    tmp_path, caplog,
):
    """Issue #169 acceptance 2 (the #105 regression): a pre-idle
    descendant running `timeout <seconds> ...` INSIDE its deadline is
    a legitimately running tool, not a hung one — the runner logs
    `pi_idle_wait` (the evidence: pid, cmdline, deadline) instead of
    TERMed it, and the run SUCCEEDS when the tool reaches its deadline
    on its own."""
    command = make_timeout_tool_pi(tmp_path, tool_seconds=0.6)
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=105, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == ""
    lines = caplog.text.splitlines()
    waits = [line for line in lines if " pi_idle_wait " in line]
    assert len(waits) == 1, f"exactly one wait decision: {lines}"
    wait = waits[0]
    assert "run=run1" in wait
    assert "issue=xqliu/orbi#105" in wait
    assert "pid=" in wait
    assert "cmdline=" in wait
    assert "deadline=" in wait
    # No signal was ever DELIVERED: the tool finished on its own
    # deadline (a later `no_target` record is fine — the descendants
    # were already gone when the next window re-evaluated).
    terms = [line for line in lines if " pi_idle_term " in line]
    assert not any("result=sent" in line for line in terms), lines
    assert " pi_idle_kill " not in caplog.text
    assert "run_failed" not in caplog.text
    # The session resumed when the tool's result arrived.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1


def make_timeout_tool_pi_controlled(tmp_path, *, tool_seconds: float,
                                    control: Path) -> list[str]:
    """A timeout tool whose (delayed) deadline fire is controlled by
    the TEST, not by scheduling (Issue #181): the command line carries
    the nominal `timeout <tool_seconds>` (what the runner reads), but
    a fake `timeout` on PATH first waits for `control` to appear — the
    test's "the wrapper's deadline handling finished" signal — and
    only then enforces the nominal duration. The tool's lifetime is
    therefore `control_appeared + tool_seconds`, fully determined by
    the test (no real-scheduling coincidence). The test that never
    creates `control` gets a broken wrapper: the deadline handling
    never finishes, the tool outlives the nominal deadline forever."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    import shutil as _shutil
    # The real coreutils `timeout` is exec'd by ABSOLUTE path — the
    # fake shadows it on PATH, so a bare `timeout` would re-enter the
    # fake and loop forever. Coreutils `timeout` is a hard system
    # dependency (the prompt contract requires it); a missing binary
    # fails the generated script with a clear shell error.
    real_timeout = _shutil.which("timeout")
    fake_bin = session_dir / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_timeout = fake_bin / "timeout"
    fake_timeout.write_text(
        "#!/bin/sh\n"
        "duration=\"$1\"; shift\n"
        f"while [ ! -e {str(control)!r} ]; do sleep 0.01; done\n"
        f'exec {real_timeout!r} "$duration" "$@"\n', encoding="utf-8",
    )
    fake_timeout.chmod(0o755)
    tool_command = (
        f"PATH={fake_bin!s}:/usr/bin:/bin "
        f"timeout {tool_seconds!r} sleep 300"
    )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-timeout', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        f"            'arguments': {{'command': 'timeout {tool_seconds!r} sleep 300'}}}}]}}}})\n"
        f"child = subprocess.Popen(['bash', '-c', {tool_command!r}])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'tool finished'}]}})\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


def test_stream_pi_timeout_tool_deadline_grace_window_not_killed(
    tmp_path, caplog,
):
    """Issue #181 regression (deterministic): the nominal `timeout`
    deadline is BEST-EFFORT — the wrapper's alarm fires at
    `start + duration`, but the signal delivery and the exit take a
    finite, under load unbounded, time. A single "past deadline and
    still alive" observation must NOT escalate: the runner records the
    target in the window its nominal deadline passes (the grace
    window, nothing is signaled) and re-evaluates in the next window.

    Deterministic margins (no real-scheduling coincidence, poll
    0.1 s, idle window 0.3 s): the nominal deadline (0.2 s) has
    passed at least 0.1 s before the first escalation window (the
    window opens at 0.3 s of stale, the first escalation poll is at
    t∈[0.35, 0.45)), so the grace window always sees a past-deadline
    target that is still alive (the wrapper's deadline handling never
    finishes: the control file is never created). The grace window
    signals NOTHING; the flip window (one full idle window later, the
    target still alive) TERMs it — the wrapper failed to end the
    command, the evidence is confirmed, the escalation runs to the
    bounded session kill. Without the grace window the runner TERMs
    the tool in the FIRST escalation window (the CI flake this test
    pins): the assertion below distinguishes the two — the first
    `pi_idle_term` line must be the flip window's, i.e. at least one
    full idle window after the window opened."""
    # The control file is never created: the wrapper's deadline
    # handling never finishes (the wrapper is broken), the tool
    # outlives the nominal deadline forever.
    control = tmp_path / "tool-deadline-fired"
    command = make_timeout_tool_pi_controlled(
        tmp_path, tool_seconds=0.2, control=control,
    )
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=105, source_repo="xqliu/orbi",
            branch="b", progress=progress,
        )
    # The tool was TERMed by the runner (the wrapper failed to end it:
    # still alive one full idle window after the deadline), the fake
    # Pi resumed when the tool died, and the run SUCCEEDED.
    assert result == ""
    # The `recovery` progress state is observable per poll: the grace
    # window reports `wait` (nothing was signaled) BEFORE the flip
    # window reports `term`. Without the grace window the first
    # escalation window would report `term` directly (the CI flake
    # this test pins).
    assert "wait" in seen, seen
    first_wait = seen.index("wait")
    assert "term" in seen, seen
    first_term = seen.index("term")
    assert first_wait < first_term, seen
    # No `term` before the first `wait` (the grace window never
    # signaled).
    assert not any(state == "term" for state in seen[:first_wait]), seen
    lines = caplog.text.splitlines()
    # The deadline had clearly passed when the window opened: no wait
    # decision at all.
    assert " pi_idle_wait " not in caplog.text
    # The flip window delivered the signal (the wrapper failed to end
    # the command: still alive one full idle window after the
    # deadline).
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) >= 1, lines
    assert "result=sent" in terms[0], lines
    # The session resumed when the tool's death reached the fake Pi.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) >= 1, lines


def test_stream_pi_timeout_tool_past_deadline_still_terminated(
    tmp_path, caplog,
):
    """Issue #169 constraint + Issue #181 grace: the wait is bounded —
    when the `timeout` deadline passed but the descendant is STILL
    alive one full idle window later (the wrapper failed to end the
    command), the evidence is confirmed: the TERM → KILL →
    session-kill escalation runs (the slot is never held forever, no
    silent ignoring of the timeout). The tool deadline (0.1 s) lands
    clearly BEFORE the idle window opens (0.3 s of stale), so the
    first escalation window already sees a past deadline: it is the
    grace window (no signal, Issue #181), and the NEXT window — the
    target still alive — delivers the TERM."""
    command = make_timeout_tool_pi(
        tmp_path, tool_seconds=0.1, react_on_tool_done=False,
        wrapper_honors_deadline=False,
    )
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=105, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    # The first escalation window already saw a past deadline (the
    # wrapper failed to end the command): no wait decision at all.
    assert " pi_idle_wait " not in caplog.text
    # The escalation ran: the grace window signaled nothing, and the
    # next window (the target still alive one full idle window after
    # the deadline) TERMed the pre-idle descendants (one line per
    # target, at least one delivered)...
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) >= 1
    assert any("result=sent" in line for line in terms)
    # ...and after the bounded cycles the session was killed.
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


def test_stream_pi_idle_recovery_state_wait_visible_in_progress_callback(
    tmp_path,
):
    """Issue #169: the live GitHub progress comment shows the wait
    state — the activity state passed to the progress callback carries
    `recovery=wait` while the runner waits for the tool's deadline."""
    command = make_timeout_tool_pi(tmp_path, tool_seconds=0.6)
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    runner.stream_pi(
        command, cwd=tmp_path, poll_interval=0.1,
        idle_warn_seconds=0.3,
        run_id="run1", issue=105, source_repo="xqliu/orbi",
        branch="b", progress=progress,
    )
    assert "wait" in seen
    assert "term" not in seen
    assert seen[-1] is None


def test_stream_pi_wait_state_cleared_when_waited_tool_exits(
    tmp_path, caplog,
):
    """Issue #169: the wait evidence FLIPS when the waited tool reaches
    its own deadline and exits while Pi stays silent (Pi itself is
    stuck): the next window finds no pre-idle descendants (`no_target`),
    the stale `wait` state is cleared (the progress callback must not
    keep reporting `recovery=wait` after the tool is gone), and the
    escalation continues to the bounded session kill."""
    command = make_timeout_tool_pi(
        tmp_path, tool_seconds=0.6, react_on_tool_done=False,
    )
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=105, source_repo="xqliu/orbi",
            branch="b", progress=progress,
        )
    lines = caplog.text.splitlines()
    waits = [line for line in lines if " pi_idle_wait " in line]
    assert len(waits) == 1, lines
    # The tool exited on its own deadline: the next window finds no
    # pre-idle descendants (`no_target`, nothing was signaled)...
    terms = [line for line in lines if " pi_idle_term " in line]
    assert any("result=no_target" in line for line in terms), lines
    assert not any("result=sent" in line for line in terms), lines
    # ...and the stale wait state is cleared: no `wait` is reported
    # after the no_target window (the escalation keeps running).
    no_target_index = next(
        i for i, line in enumerate(lines)
        if " pi_idle_term " in line and "result=no_target" in line
    )
    wait_after = [
        line for line in lines[no_target_index:]
        if " pi_idle_wait " in line
    ]
    assert not wait_after, lines
    # The poll right after the last `wait` is the no_target window:
    # the stale state is cleared (None), and the escalation continues
    # (`kill`) — no `wait` is ever reported after the tool is gone.
    last_wait = max(i for i, state in enumerate(seen) if state == "wait")
    assert seen[last_wait + 1] is None, seen
    assert all(state != "wait" for state in seen[last_wait + 1:]), seen
    assert "kill" in seen[last_wait + 1:], seen
    # The escalation still ends in the bounded session kill.
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


def test_pending_timeout_targets_unit(tmp_path, monkeypatch):
    """Issue #169: the clock-consistent pending computation — a target
    whose start time is unreadable (it exited between the discovery and
    the check) is skipped, a target without a clear timeout is not
    pending, a target inside its deadline is pending with the remaining
    time, and a target whose deadline already passed is not pending."""
    now_mono = time.monotonic()

    def fake_start(pid, *, hz):
        if pid == 1:
            return None  # gone between discovery and the check
        if pid == 2:
            return now_mono - 1.0  # started 1 s ago, timeout 5 s
        return now_mono - 9.0  # started 9 s ago, timeout 5 s (passed)

    monkeypatch.setattr(runner, "process_start_monotonic", fake_start)
    targets = [
        {"pid": 1, "cmdline": "timeout 5 pytest"},
        {"pid": 2, "cmdline": "timeout 5 pytest"},
        {"pid": 3, "cmdline": "timeout 5 pytest"},
        {"pid": 4, "cmdline": "pytest"},  # no clear timeout
    ]
    pending = runner._pending_timeout_targets(targets)
    assert [target["pid"] for target, _ in pending] == [2]
    # The deadline is the realtime now plus the remaining time.
    (target, deadline) = pending[0]
    assert target["pid"] == 2
    assert time.time() + 3.0 < deadline < time.time() + 5.0
    # An empty target list is a no-op.
    assert runner._pending_timeout_targets([]) == []


def make_hung_pi(tmp_path, *, child_sleep=10.0, ignore_sigterm=False,
                 react_on_child_death=True, exit_code=0,
                 model_wait=False):
    """Build a command that mimics a HUNG Pi (Issue #94): it writes the
    session toolCall, spawns a long-running child (the hung bash tool)
    and waits for it. When `react_on_child_death`, the child's death
    (the runner's TERM/KILL) makes the fake Pi write the toolResult
    error — the failure signal to the model — and exit with
    `exit_code` (the session continues on its own). Otherwise it stays
    silent (Pi itself is stuck). `model_wait` writes a toolResult
    first (the model is expected to reply next: the Issue #75
    territory, never the idle-recovery territory).

    The sleeps are short on purpose (Issue #95 termination guard):
    WITHOUT the recovery implementation the child exits naturally and
    the run ends, so the red phase fails on the missing recovery
    lines instead of hanging for the full child sleep."""
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    ignore = (
        "import signal as _sig\n"
        "_sig.signal(_sig.SIGTERM, _sig.SIG_IGN)\n"
        if ignore_sigterm else ""
    )
    tool_result = (
        "write({'type': 'message', 'id': 'r0', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': False,\n"
        "                   'content': [{'type': 'text', 'text': 'ok'}]}})\n"
        if model_wait else ""
    )
    react = (
        "write({'type': 'message', 'id': 'r1', 'timestamp': ts(),\n"
        "       'message': {'role': 'toolResult', 'toolCallId': 't1',\n"
        "                   'toolName': 'bash', 'isError': True,\n"
        "                   'content': [{'type': 'text',\n"
        "                                  'text': 'killed by runner'}]}})\n"
        if react_on_child_death else "time.sleep(10)\n"
    )
    script = (
        "import json, subprocess, sys, time\n"
        "from datetime import datetime, timezone\n"
        f"session = {str(session_dir / 'sess.jsonl')!r}\n"
        "def ts():\n"
        "    return datetime.now(timezone.utc).isoformat()\n"
        "def write(record):\n"
        "    with open(session, 'a') as handle:\n"
        "        handle.write(json.dumps(record) + '\\n')\n"
        "write({'type': 'session', 'id': 'sess-hung', 'timestamp': ts(),\n"
        "       'cwd': '/w'})\n"
        "write({'type': 'message', 'id': 'a1', 'timestamp': ts(),\n"
        "       'message': {'role': 'assistant', 'content': [\n"
        "           {'type': 'toolCall', 'id': 't1', 'name': 'bash',\n"
        "            'arguments': {'command': 'sleep 300'}}]}})\n"
        f"{tool_result}"
        f"{ignore}"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        f"     'import time; time.sleep({child_sleep!r})'])\n"
        "while child.poll() is None:\n"
        "    time.sleep(0.05)\n"
        f"{react}"
        f"sys.exit({exit_code!r})\n"
    )
    return [sys.executable, "-c", script]


def test_stream_pi_idle_recovery_terms_hung_descendant_and_resumes(
    tmp_path, caplog,
):
    """Issue #94 acceptance 1 (TERM 成功恢复): a stalled session
    (no model/session activity past `idle_warn_seconds`, not
    model_wait) with a hung descendant that existed before the idle
    window gets SIGTERM'd — the journal line carries run_id, pid,
    cmdline and result — the fake Pi gets the non-zero exit, writes
    the failure toolResult and the run RESUMES and SUCCEEDS (the
    failure signal reached the model)."""
    command = make_hung_pi(tmp_path)
    with caplog.at_level("INFO"):
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=94, source_repo="xqliu/orbi",
            branch="b",
        )
    assert result == ""
    lines = caplog.text.splitlines()
    idles = [line for line in lines if " pi_idle " in line]
    assert len(idles) == 1  # the existing warning, once
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) == 1, f"exactly one TERM step: {lines}"
    term = terms[0]
    assert "run=run1" in term
    assert "issue=xqliu/orbi#94" in term
    assert "role=implement" in term
    assert "pid=" in term
    assert "cmdline=" in term
    assert "result=sent" in term
    # The TERM came after the warning...
    assert lines.index(term) > lines.index(idles[0])
    # ...and the model got the failure signal: the fake Pi wrote the
    # error toolResult (visible as result=error) and resumed.
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1
    assert lines.index(resumed[0]) > lines.index(term)
    assert "result=error" in caplog.text
    # The run SUCCEEDED: no failure, no session kill.
    assert "run_failed" not in caplog.text
    # No KILL escalation: the TERM worked within one idle cycle.
    assert " pi_idle_kill " not in caplog.text


def test_stream_pi_idle_recovery_kills_descendant_that_ignores_term(
    tmp_path, caplog,
):
    """Issue #94 acceptance 2 (TERM 后仍存活升级 KILL): a descendant
    that ignores SIGTERM is still alive one idle cycle later — the
    runner SIGKILLs it (journal line with pid, cmdline, result), the
    fake Pi reacts to the death and the run resumes."""
    command = make_hung_pi(
        tmp_path, ignore_sigterm=True, react_on_child_death=True,
    )
    with caplog.at_level("INFO"):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=94, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    kills = [line for line in lines if " pi_idle_kill " in line]
    assert len(terms) == 1 and "result=sent" in terms[0]
    assert len(kills) == 1, f"exactly one KILL step: {lines}"
    kill = kills[0]
    assert "run=run1" in kill
    assert "pid=" in kill
    assert "cmdline=" in kill
    assert "result=sent" in kill  # the SIGKILL was delivered
    # The KILL came one idle cycle after the TERM...
    assert lines.index(kill) > lines.index(terms[0])
    # ...and the run still recovered (the fake Pi reacted to the death).
    resumed = [line for line in lines if " pi_resumed " in line]
    assert len(resumed) == 1
    assert "run_failed" not in caplog.text


def test_stream_pi_idle_recovery_kills_pi_session_after_three_idle_cycles(
    tmp_path, caplog,
):
    """Issue #94 acceptance 3 (连续 idle 升级终止会话): the hung tool
    died from the TERM but the session NEVER resumes (Pi itself is
    stuck) — after three idle cycles the runner kills the Pi session
    itself and fails fast: `run_failed` carries the full scene and
    `reason=idle_recovery_stale_...`, the error names the idle
    recovery, and the normal `ai-blocked` flow takes over (the slot is
    never held forever)."""
    command = make_hung_pi(tmp_path, react_on_child_death=False)
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=94, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    kills = [line for line in lines if " pi_idle_kill " in line]
    assert len(terms) == 1 and "result=sent" in terms[0]
    # The TERM worked (the child died) — the KILL step reports the
    # target as already dead instead of signaling again.
    assert len(kills) == 1
    assert "result=already_dead" in kills[0]
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    failure = failures[0]
    assert "reason=idle_recovery_stale_" in failure
    assert "run=run1" in failure
    assert "issue=xqliu/orbi#94" in failure
    assert f"worktree={tmp_path}" in failure


def test_stream_pi_idle_recovery_without_descendants_still_terminates(
    tmp_path, caplog,
):
    """Issue #94: a stalled session with NO pre-idle descendants (Pi
    itself is stuck, no hung tool) still escalates: the TERM step logs
    `result=no_target` (nothing was signaled — no pid), there is no
    KILL step, and after three idle cycles the session is killed and
    the run fails fast."""
    records = [
        (0.0, {"type": "session", "id": "sess-1",
               "timestamp": fresh_timestamp(), "cwd": "/w"}),
        (0.1, {"type": "message", "id": "a1",
               "timestamp": fresh_timestamp(1),
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": "stuck thinking"}]}}),
    ]
    command = make_fake_pi(tmp_path, session_records=records, sleep=10.0)
    with caplog.at_level("INFO"), pytest.raises(
        RuntimeError, match="idle recovery",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            idle_warn_seconds=0.3,
            run_id="run1", issue=94, source_repo="xqliu/orbi",
            branch="b",
        )
    lines = caplog.text.splitlines()
    terms = [line for line in lines if " pi_idle_term " in line]
    assert len(terms) == 1
    assert "result=no_target" in terms[0]
    assert " pid=" not in terms[0]
    assert " pi_idle_kill " not in caplog.text
    failures = [line for line in lines if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=idle_recovery_stale_" in failures[0]


def test_stream_pi_idle_recovery_never_signals_non_descendants(
    tmp_path, caplog,
):
    """Issue #94 constraint (非 pi 后代不被误杀): a long-running
    process that is NOT a descendant of the Pi process (parented by
    the test itself) is never signaled — the ppid chain is the only
    target criterion."""
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        command = make_hung_pi(tmp_path)
        with caplog.at_level("INFO"):
            runner.stream_pi(
                command, cwd=tmp_path, poll_interval=0.1,
                idle_warn_seconds=0.3,
                run_id="run1", issue=94, source_repo="xqliu/orbi",
                branch="b",
            )
        # The bystander is untouched: still running, and its pid never
        # appears on a recovery line.
        assert bystander.poll() is None
        for line in caplog.text.splitlines():
            if " pi_idle_term " in line or " pi_idle_kill " in line:
                assert f"pid={bystander.pid}" not in line
    finally:
        bystander.kill()
        bystander.wait()


def test_stream_pi_idle_recovery_never_fires_during_model_wait(
    tmp_path, caplog,
):
    """Issue #94: the recovery is the non-model_wait territory (same
    gate as the `pi_idle` warning). A frozen model_wait with a hung
    descendant is the Issue #218 hung-model-request case: the runner
    kills Pi via the `model_wait_dead` path and never TERMs the
    descendant."""
    command = make_hung_pi(tmp_path, model_wait=True)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="hung",
    ):
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            model_wait_dead_seconds=0.5,
            run_id="run1", issue=94, source_repo="xqliu/orbi",
            branch="b",
        )
    assert " pi_idle_term " not in caplog.text
    assert " pi_idle_kill " not in caplog.text
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=model_wait_dead_stale_" in failures[0]


def test_stream_pi_idle_recovery_state_visible_in_progress_callback(
    tmp_path,
):
    """Issue #94: the live GitHub progress comment is synced with the
    recovery — the activity state passed to the progress callback
    carries `recovery` (None -> `term` while the TERMed tool is being
    waited on -> back to None when the session resumes)."""
    command = make_hung_pi(tmp_path)
    seen = []

    def progress(activity):
        seen.append(activity.get("recovery"))

    runner.stream_pi(
        command, cwd=tmp_path, poll_interval=0.1,
        idle_warn_seconds=0.3,
        run_id="run1", issue=94, source_repo="xqliu/orbi",
        branch="b", progress=progress,
    )
    assert "term" in seen
    # Before the stall and after the resume the state is None again.
    assert seen[0] is None
    assert seen[-1] is None
    assert seen.index("term") > 0


def test_stream_pi_times_out_and_kills_process(tmp_path, caplog):
    command = make_fake_pi(tmp_path, session_records=[], sleep=10.0)
    with caplog.at_level("ERROR"), pytest.raises(
        subprocess.TimeoutExpired,
    ) as excinfo:
        runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1, timeout=0.5,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b",
        )
    assert excinfo.value.timeout == 0.5
    # The exception must not carry the raw command (prompt / Issue body).
    assert "sleep" not in str(excinfo.value)
    failures = [line for line in caplog.text.splitlines()
                if " run_failed " in line]
    assert len(failures) == 1
    assert "reason=timeout_0.5s" in failures[0]
    assert "issue=xqliu/orbi#24" in failures[0]


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

    result = runner.stream_pi(
        command, cwd=tmp_path, poll_interval=0.1,
        run_id="run1", issue=24, source_repo="xqliu/orbi",
        branch="b", progress=progress,
    )
    assert result == "final answer"
    # The callback fired many times during the run (polls at 0.1 s while
    # the child sleeps 1.2 s after the last session record)...
    assert len(seen) >= 4
    # ...and it saw the activity change (starting -> test) live, before
    # the child exited: the tool call is visible in an early snapshot,
    # not only in a final state.
    # Issue #176: the first live snapshot shows the startup sub-phase
    # (request_pending: the session exists, the first response has not
    # arrived yet), not the generic `starting`.
    phases = [entry["phase"] for entry in seen]
    assert phases[0] == "request_pending"
    assert "test" in phases
    assert phases.index("test") < len(phases) - 1
    test_entries = [entry for entry in seen if entry["phase"] == "test"]
    assert any(
        entry["action"] == "bash pytest tests/"
        for entry in test_entries
    )


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
        comment_id = 77

        def patch(self, body):
            patches.append(body)

    publisher = FakePublisher()
    throttle = runner.LiveProgressThrottle(
        publisher, issue=24, title="Live progress", run_id="run1",
        role="implement", branch="b", worktree=tmp_path,
        started=time.monotonic(), pr_url=None, review_round=0,
        priority="normal",
    )
    result = runner.stream_pi(
        command, cwd=tmp_path, poll_interval=0.1,
        run_id="run1", issue=24, source_repo="xqliu/orbi",
        branch="b", progress=throttle,
    )
    assert result == "done"
    # At least one PATCH happened while the child was still running
    # (all of them did: the callback only exists inside the poll loop).
    assert len(patches) >= 2
    # The live comment carries the run marker and the live phase.
    assert all(
        body.startswith("<!-- orbi:run=run1 -->")
        for body in patches
    )
    assert any("- phase: test" in body for body in patches)
    assert any("- last action: bash pytest tests/" in body for body in patches)


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
        result = runner.stream_pi(
            command, cwd=tmp_path, poll_interval=0.1,
            run_id="run1", issue=24, source_repo="xqliu/orbi",
            branch="b", progress=boom,
        )
    assert result == "final answer"
    assert "progress_publish_failed" in caplog.text
    assert "run=run1" in caplog.text
    assert "role=implement" in caplog.text


def test_live_progress_throttle_patches_on_change_and_cadence():
    """The throttle passes an update through on a visible activity change
    and at most every PI_HEARTBEAT_SECONDS (30 s) otherwise."""
    calls = []

    class FakePublisher:
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = runner.LiveProgressThrottle(
        publisher, issue=1, title="Throttle task", run_id="run1",
        role="implement", branch="b", worktree=Path("/w"),
        started=time.monotonic(), pr_url=None, review_round=0,
        priority="normal",
    )
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
    changed = dict(first, phase="test", action="bash pytest tests/")
    throttle(changed)
    assert len(calls) == 2
    # Unchanged again within the cadence: suppressed...
    throttle(changed)
    assert len(calls) == 2
    # ...until the 30 s cadence elapses: passed as a heartbeat update.
    throttle._last_patch -= runner.PI_HEARTBEAT_SECONDS + 1
    throttle(changed)
    assert len(calls) == 3
    # The rendered body carries the live state.
    assert "- phase: test" in calls[-1]
    assert "- last action: bash pytest tests/" in calls[-1]


def test_live_progress_throttle_patches_on_recovery_change():
    """Issue #94: the recovery state is part of the visible progress —
    entering idle recovery (None -> `term`) PATCHes the live comment
    immediately even though phase/action/result did not change, and
    the rendered body carries the `recovery` line."""
    calls = []

    class FakePublisher:
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = runner.LiveProgressThrottle(
        publisher, issue=94, title="Idle recovery", run_id="run1",
        role="implement", branch="b", worktree=Path("/w"),
        started=time.monotonic(), pr_url=None, review_round=0,
        priority="normal",
    )
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

    monkeypatch.setattr(runner, "stream_pi", fake_stream)
    monkeypatch.setattr(runner, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    runner.run_pi(
        {"number": 4, "title": "Fix", "body": "b"}, tmp_path,
        {
            "prompt": tmp_path / "prompt.md",
            "repo_dir": tmp_path,
            "source_repos": ["owner/repo"],
            "workspace_root": tmp_path,
            "base_branch": "main",
            "base_sha": "abc123",
            "run_id": "a1b2c3d4",
            "skills": [],
            "context_files": [],
        },
        "owner/repo", branch="b",
        progress=callback,
    )
    assert seen["progress"] is callback
    assert seen["run_id"] == "a1b2c3d4"
    assert seen["branch"] == "b"


def test_run_review_passes_progress_callback_to_stream_pi(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(runner, "stream_pi", fake_stream)
    monkeypatch.setattr(runner, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        {
            "prompt_review": tmp_path / "prompt_review.md",
            "repo_dir": tmp_path,
            "source_repos": ["owner/repo"],
            "base_branch": "main",
            "run_id": "a1b2c3d4",
            "skills": [],
        },
        "owner/repo", 4, "branch", 1, progress=callback,
    )
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

    monkeypatch.setattr(runner, "stream_pi", fake_stream)
    monkeypatch.setattr(runner, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    runner.run_pi(
        {"number": 4, "title": "Fix", "body": "b"}, tmp_path,
        {
            "prompt": tmp_path / "prompt.md",
            "repo_dir": tmp_path,
            "source_repos": ["owner/repo"],
            "workspace_root": tmp_path,
            "base_branch": "main",
            "base_sha": "abc123",
            "run_id": "a1b2c3d4",
            "skills": [],
            "context_files": [],
            "model_wait_dead_seconds": 1234.5,
        },
        "owner/repo", branch="b",
    )
    assert seen["model_wait_dead_seconds"] == 1234.5
    assert seen["model_wait_dead_seconds"] != runner.PI_MODEL_WAIT_DEAD_SECONDS


def test_run_review_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the review session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(runner, "stream_pi", fake_stream)
    monkeypatch.setattr(runner, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        {
            "prompt_review": tmp_path / "prompt_review.md",
            "repo_dir": tmp_path,
            "source_repos": ["owner/repo"],
            "base_branch": "main",
            "run_id": "a1b2c3d4",
            "skills": [],
            "model_wait_dead_seconds": 1234.5,
        },
        "owner/repo", 4, "branch", 1,
    )
    assert seen["model_wait_dead_seconds"] == 1234.5
    assert seen["model_wait_dead_seconds"] != runner.PI_MODEL_WAIT_DEAD_SECONDS


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

    monkeypatch.setattr(runner, "stream_pi", fake_stream)
    monkeypatch.setattr(runner, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    runner.run_pi(
        {"number": 4, "title": "Fix", "body": "b"}, tmp_path,
        {
            "prompt": tmp_path / "prompt.md",
            "repo_dir": tmp_path,
            "source_repos": ["owner/repo"],
            "workspace_root": tmp_path,
            "base_branch": "main",
            "base_sha": "abc123",
            "run_id": "a1b2c3d4",
            "skills": [],
            "context_files": [],
        },
        "owner/repo", branch="b",
    )
    assert seen["model_wait_dead_seconds"] == runner.PI_MODEL_WAIT_DEAD_SECONDS


# --- role-specific --skill lists (Issue #83) ---------------------------------


def _skill_config(tmp_path, *names):
    """Build a config whose skills point at <name>/SKILL.md files."""
    skills = []
    for name in names:
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        skills.append(skill_dir / "SKILL.md")
    return {
        "prompt": tmp_path / "prompt.md",
        "prompt_review": tmp_path / "prompt_review.md",
        "repo_dir": tmp_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": skills,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "a1b2c3d4",
    }


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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
    implement_skills = _command_skills(calls[0])
    review_skills = _command_skills(calls[1])
    assert implement_skills != review_skills


def test_skill_name_of_skill_md_inside_skill_directory(tmp_path):
    path = tmp_path / "skills" / "tdd-dev" / "SKILL.md"
    path.parent.mkdir(parents=True)
    assert runner._skill_name(path) == "tdd-dev"


def test_skill_name_of_bare_markdown_entry(tmp_path):
    path = tmp_path / "my-skill.md"
    assert runner._skill_name(path) == "my-skill"


def test_run_review_keeps_non_delivery_skill_names(monkeypatch, tmp_path):
    """Only tdd-dev/review-fix-loop are excluded from the review; any
    other configured skill (including code-review) passes through.
    """
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(tmp_path, "code-review", "platform-qa")
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
    skills = _command_skills(calls[0])
    assert any("code-review" in skill for skill in skills)
    assert any("platform-qa" in skill for skill in skills)


# --- max_concurrency config (Issue #39) --------------------------------------


def test_load_config_defaults_max_concurrency_to_one(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["max_concurrency"] == 1


def test_load_config_reads_explicit_max_concurrency(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 2\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["max_concurrency"] == 2


def test_load_config_derives_slot_dir_from_repo_dir(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["slot_dir"] == (tmp_path / "repo").resolve() / ".orbi" / "slots"


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "1.5", '"1"', "true", "false", "3"],
)
def test_load_config_rejects_invalid_max_concurrency(tmp_path, value):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["owner/repo"]\nmax_concurrency = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_concurrency must be a positive integer"):
        runner.load_config(config_path)


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

    def fail_if_called(repos, slot_dir, max_concurrency, active_milestone=None):
        raise AssertionError("pick_next_delivery must not run when capacity is full")

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
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
    """The slot is acquired before the pick and held for the whole task."""
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None):
        seen["occupancy"] = pilot_slots.slot_occupancy(
            tmp_path / ".orbi" / "slots", 1,
        )
        return ("owner/repo", issue, None)

    monkeypatch.setattr(runner, "pick_next_delivery", fake_pick)
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: "https://x/y/pull/12")
    monkeypatch.setattr(runner, "wait_for_delivery", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["occupancy"] == [(1, os.getpid())]


def test_main_reacquires_slot_after_previous_release(monkeypatch, tmp_path):
    """After the holder releases (process exit), the next run takes the slot."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
    )

    assert runner.main(["--config", str(config)]) == 0
    slot_dir = tmp_path / ".orbi" / "slots"
    # The slot file exists and was released when main() returned ...
    assert (slot_dir / "slot-1").exists()
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    # ... so the next run takes the slot again.
    assert runner.main(["--config", str(config)]) == 0
    assert (slot_dir / "slot-1").read_text(encoding="utf-8").strip() == str(os.getpid())


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

    monkeypatch.setattr(runner, "run_command", fake_run)
    return calls


def test_main_unit_drift_blocks_claim_before_slot(monkeypatch, tmp_path,
                                                  caplog):
    """Issue #142: a drift the self-heal CANNOT resolve (the re-verify
    still sees it after the idempotent install) fails the start BEFORE
    any slot or claim: the structured `unit_drift` line (repo path,
    installed path, hashes, fix command) is logged, the tick raises
    (non-zero exit), no slot is taken and nothing is claimed — while
    a currently RUNNING task is never interrupted (only the next
    start is blocked)."""
    from orbi import systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    # The real preflight (overrides the conftest default no-op).
    monkeypatch.setattr(
        runner, "check_unit_drift", systemd_deploy.check_unit_drift,
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

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on unit drift",
    ):
        fail_if_called()
    with caplog.at_level("ERROR"):
        with pytest.raises(runner.UnitDriftError, match="unit_drift"):
            runner.main(["--config", str(config)])
    # The self-heal follows default capacity one: it enables @1 and
    # stops only surplus @2.timer, never a service instance.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl", "--user", "enable", "--now", "orbi@1.timer",
    ] in calls
    assert [
        "systemctl", "--user", "disable", "--now", "orbi@2.timer",
    ] in calls
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable", "disable")
    # The structured line carries what the Issue requires.
    assert "unit_drift unit=orbi@.timer" in caplog.text
    assert f"repo={repo / 'systemd' / 'orbi@.timer'}" in caplog.text
    assert f"installed={installed / 'orbi@.timer'}" in caplog.text
    assert "repo_sha256=" in caplog.text
    assert "installed_sha256=" in caplog.text
    assert "fix=orbi install-units" in caplog.text
    # No slot was taken and nothing was claimed.
    assert not (repo / ".orbi" / "slots").exists()


def test_main_unit_drift_auto_syncs_and_proceeds_to_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: the normal scene — a template change merged to main
    (the ExecStartPre-synced checkout carries the new templates, the
    installed units are still the old ones). The preflight self-heals
    with the SAME idempotent install (copy, daemon-reload, enable the
    timer — never start/stop/restart the service), the re-verify is
    clean, the structured `auto_synced` line is logged and the tick
    proceeds to the normal claim flow (slot taken, queue scanned).
    No more per-tick drift loop until a human intervenes."""
    from orbi import systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", systemd_deploy.check_unit_drift,
    )
    calls = _fake_preflight_run(monkeypatch, installed)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    # The self-heal follows default capacity one and never a service.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl", "--user", "enable", "--now", "orbi@1.timer",
    ] in calls
    assert [
        "systemctl", "--user", "disable", "--now", "orbi@2.timer",
    ] in calls
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable", "disable")
    # The repo template won: the installed unit matches it again.
    status = systemd_deploy.unit_status(repo, installed)
    assert all(entry["drifted"] is False for entry in status)
    assert "unit_drift auto_synced unit=orbi@.timer" in caplog.text
    assert "commit=0123456789abcdef0123456789abcdef01234567" in caplog.text
    # The tick proceeded: the slot was taken AFTER the preflight passed.
    assert (repo / ".orbi" / "slots" / "slot-1").exists()


def test_main_unit_drift_auto_sync_failure_blocks_claim(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: a failing self-heal (e.g. daemon-reload fails) fails
    fast BEFORE any slot or claim — the install error propagates,
    nothing is claimed, and the scene stays in the journal."""
    from orbi import systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=True)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", systemd_deploy.check_unit_drift,
    )

    def failing_run(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "enable"]:
            raise subprocess.CalledProcessError(1, command, stderr="nope")
        return ""

    monkeypatch.setattr(runner, "run_command", failing_run)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("pick_next_delivery must not run on unit drift")

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(
        AssertionError, match="must not run on unit drift",
    ):
        fail_if_called()
    with pytest.raises(subprocess.CalledProcessError):
        runner.main(["--config", str(config)])
    assert not (repo / ".orbi" / "slots").exists()


def test_main_unit_drift_clean_proceeds_to_claim(monkeypatch, tmp_path,
                                                 caplog):
    """Issue #103: matching units log `unit_drift clean` and the tick
    proceeds to the normal claim flow (slot taken, queue scanned)."""
    from orbi import systemd_deploy

    repo, installed = _drift_world(tmp_path, drift=False)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    monkeypatch.setattr(
        runner, "check_unit_drift", systemd_deploy.check_unit_drift,
    )
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        f'source_repos = ["owner/repo"]\nrepo_dir = "{repo}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "unit_drift clean" in caplog.text
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
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
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

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
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
    """Issue #114: a passing transport check logs `transport clean` and
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
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
    )
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "transport clean" in caplog.text
    # The slot was taken AFTER the preflight passed.
    assert (tmp_path / ".orbi" / "slots" / "slot-1").exists()


def test_main_transport_preflight_receives_the_configured_args(
    monkeypatch, tmp_path,
):
    """Issue #114: the preflight checks the configured repo_dir and
    source repos with the real run_command, and never migrates (the
    migration is the human-run setup entry's job)."""
    seen: dict = {}

    def fake_check(repo_dir, source_repos, **kwargs):
        seen["repo_dir"] = Path(repo_dir)
        seen["source_repos"] = list(source_repos)
        seen["migrate"] = kwargs.get("migrate")
        seen["run_command"] = kwargs.get("run_command")
        return {}

    monkeypatch.setattr(runner, "check_transport", fake_check)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo", "owner/backlog"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["repo_dir"] == tmp_path
    assert seen["source_repos"] == ["owner/repo", "owner/backlog"]
    assert seen["migrate"] is False
    assert seen["run_command"] is runner.run_command


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

    monkeypatch.setattr(runner, "refresh_cli_install", fake_refresh)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: None,
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

    monkeypatch.setattr(runner, "pick_next_delivery", fail_if_called)
    with pytest.raises(
        AssertionError, match="must not run on cli install failure",
    ):
        fail_if_called()
    monkeypatch.setattr(
        runner, "refresh_cli_install",
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


def test_bootstrap_chain_loads_and_refreshes_when_cli_install_is_unmapped(
    tmp_path,
):
    """Issue #158 acceptance (the incident itself, one module later):
    the editable finder's module MAPPING is generated at INSTALL time
    from `pyproject.toml`, so right after this PR merges, the INSTALLED
    finder does not map `cli_install` yet — and the bootstrap chain
    (`orbi.cli` -> `orbi.runner`) must still LOAD in that
    tool env, with the refresh REACHABLE: the refresh implementation
    lives in `runner` itself (a separate new module would not be
    importable in the stale-finder env, and the very refresh that
    reinstalls the tool env could never run — the #158 incident, one
    module later). Load a fresh `orbi.runner` with
    `orbi.cli_install` blocked from the import system: it must
    import cleanly and its `refresh_cli_install` gate must run (the
    unchanged path needs no uv call and no other new module)."""
    class _BlockCliInstall:
        """Meta path finder that hides the `orbi.cli_install`
        module."""

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "orbi.cli_install":
                raise ModuleNotFoundError(
                    "No module named 'orbi.cli_install'",
                    name=fullname,
                )
            return None

    hook = _BlockCliInstall()
    # The hook hides exactly the module under test and nothing else.
    with pytest.raises(ModuleNotFoundError, match="cli_install"):
        hook.find_spec("orbi.cli_install")
    assert hook.find_spec("some_other_module") is None
    import orbi
    saved = sys.modules.pop("orbi.runner")
    sys.meta_path.insert(0, hook)
    try:
        import orbi.runner as fresh_runner
    finally:
        sys.meta_path.remove(hook)
        # Restore BOTH the sys.modules entry and the package attribute:
        # the fresh import binds `orbi.runner` to the new module
        # instance, and leaving it would shadow the original for every
        # later test in the session.
        sys.modules["orbi.runner"] = saved
        orbi.runner = saved
    module = fresh_runner
    # The fresh module loaded with `cli_install` unmapped: the chain
    # is importable in the stale-finder tool env, so the next systemd
    # start reaches `main()` and the refresh can repair the finder.
    assert module.main is not None
    # The refresh gate itself runs in the stale env (unchanged path:
    # no uv call, no other new module needed).
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "a"\n', encoding="utf-8",
    )
    module.write_install_state(
        tmp_path, module.packaging_fingerprint(tmp_path),
    )
    calls = []
    assert module.refresh_cli_install(
        tmp_path, run_command=lambda *a, **k: calls.append(1),
    ) == "unchanged"
    assert calls == []


# --- delivery lifecycle: slot held until merge or terminal failure ----------

PR_URL = "https://github.com/owner/repo/pull/46"


def fake_pr_view(monkeypatch, state: str) -> tuple[list, object]:
    """Answer `gh pr view <n> --json state`.

    Returns the command log and the fake itself (so a test can prove
    the fake rejects unexpected commands).
    """
    seen: list = []

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"] and command[1] == "pr" \
                and command[2] == "view":
            seen.append(command)
            assert command[3] == "46"
            assert command[4:] == ["--json", "state"]
            return json.dumps({"state": state})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    return seen, fake_run


def test_pr_state_returns_open_merged_or_closed(monkeypatch):
    for state in ("OPEN", "MERGED", "CLOSED"):
        fake_pr_view(monkeypatch, state)
        assert runner.pr_state(PR_URL) == state


def test_fake_pr_view_rejects_unexpected_commands(monkeypatch):
    seen, fake_run = fake_pr_view(monkeypatch, "OPEN")
    assert seen == []
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_pr_state_fails_fast_on_unexpected_state(monkeypatch):
    fake_pr_view(monkeypatch, "WEIRD")
    with pytest.raises(ValueError, match="unexpected PR state"):
        runner.pr_state(PR_URL)


def test_pr_state_fails_fast_on_non_object_json(monkeypatch):
    def fake_run(command, **kwargs):
        return json.dumps(["OPEN"])

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(ValueError, match="pr view must be a JSON object"):
        runner.pr_state(PR_URL)


def test_finish_blocked_progress_is_a_noop_without_run_id(monkeypatch):
    """Without a bound run id there is no tracked comment to update
    (the failure comment simply carries no marker)."""
    calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )
    runner._finish_blocked_progress(
        39, None, "owner/repo", None, None, "https://x/pull/46",
        "failure", "next step", title="Blocked task",
    )
    assert calls == []


def test_finish_blocked_progress_creates_the_comment_when_missing(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner._finish_blocked_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
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


def test_finish_blocked_progress_carries_the_actual_role_and_round(
    monkeypatch,
):
    """The blocked scene must show the role and the review/fix round the
    run was actually in, not the hardcoded `fix`/`0` (review round 2,
    PR #42)."""
    api_calls = []

    def fake_run(command, **kwargs):
        api_calls.append(command)
        if "--method" not in command:
            return "[]"
        assert command[command.index("--method") + 1] == "POST"
        body = command[command.index("--field") + 1]
        return json.dumps({"id": 78, "body": body[len("body="):],
                           "url": "https://x/78"})

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner._finish_blocked_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
        role=runner.ROLE_REVIEW, review_round=2,
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 2" in body


def test_finish_blocked_progress_defaults_to_review_round_zero(monkeypatch):
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner._finish_blocked_progress(
        39, "a1b2c3d4", "owner/repo", None, None, "https://x/pull/46",
        "the failure", "the next step", title="Blocked task",
    )
    posts = [
        command for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert len(posts) == 1
    body = posts[0][posts[0].index("--field") + 1][len("body="):]
    assert "- role: review" in body
    assert "- review/fix round: 0" in body


def test_wait_for_delivery_returns_when_pr_merged(monkeypatch, caplog):
    seen, _ = fake_pr_view(monkeypatch, "MERGED")
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(PR_URL, issue, {}, "owner/repo")
    assert len(seen) == 1
    assert "delivery_merged" in caplog.text
    assert f"pr={PR_URL}" in caplog.text


def test_wait_for_delivery_keeps_waiting_while_pr_open(
        monkeypatch, tmp_path,
):
    states = ["OPEN", "OPEN", "MERGED"]
    calls = {"pr": 0, "labels": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            calls["pr"] += 1
            return json.dumps({"state": states[calls["pr"] - 1]})
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
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    config = {"repo_dir": tmp_path, "base_branch": "main"}
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # Awaiting review triggers the independent review (Issue #34); the
    # mock reports findings so the wait keeps polling.
    reviews = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.wait_for_delivery(PR_URL, issue, config, "owner/repo")
    # Two OPEN polls (PR state + labels each) before the MERGED poll.
    assert calls == {"pr": 3, "labels": 2}
    # One review per OPEN+awaiting-review poll.
    assert len(reviews) == 2
    # The review ran on the derived worktree/branch of the same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39-a1b2c3d4"
    assert base_branch == "main"
    assert review_config["run_id"] == "a1b2c3d4"
    assert review_config["base_sha"] == "abc123def456"
    assert repo == "owner/repo"
    assert number == 39
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


def test_wait_for_delivery_auto_merges_on_clean_review(
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
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: True,
    )
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
        "owner/repo",
    )
    # One OPEN poll, one review, then the merge is terminal: no second
    # PR state poll.
    assert pr_calls["n"] == 1
    assert "delivery_auto_merged" in caplog.text


def test_wait_for_delivery_passes_p0_priority_to_the_review(
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
                {"name": "ai-pr-opened"}, {"name": "p0"},
            ]})
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(runner, "run_command", fake_run)

    def fake_review(*args, **kwargs):
        review_calls.append(kwargs)
        return True

    monkeypatch.setattr(runner, "review_and_merge_if_clean", fake_review)
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    issue = {
        "number": 39, "title": "p0 task", "body": "",
        "labels": [{"name": "ai-pr-opened"}, {"name": "p0"}],
    }
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
        "owner/repo",
    )
    assert review_calls == [{"title": "p0 task", "priority": "p0"}]
    # The awaiting log line carries the explicit priority field.
    awaiting = [m for m in caplog.messages if "delivery_awaiting" in m]
    assert any("priority=p0" in m for m in awaiting)


def test_wait_for_delivery_marks_blocked_when_review_fails(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
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
    assert "next step:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (the failure happened
    # during the independent review) and the completed review rounds
    # (review round 2, PR #42) — not the hardcoded fix/0.
    assert "- role: review" in blocked
    assert "- review/fix round: 2" in blocked


def test_wait_for_delivery_marks_blocked_when_review_fails_while_fix_needed(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
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


def test_wait_for_delivery_blocks_when_scene_base_differs_from_config(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the base mismatch is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    # The configured base is main; the scene froze develop.
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
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
    assert any(
        "base_branch=develop" in body and "base_branch=main" in body
        for body in posted_bodies
    )
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
    assert "next step:" in blocked


def test_wait_for_delivery_worktree_missing_stays_fix_needed(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view or the
    # progress API.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "fetch"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    # The independent review must never run: the missing worktree is
    # terminal before any git/Pi mutation.
    reviews: list = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
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
    assert "orbi-owner-repo-issue-39-a1b2c3d4" in body
    # The full scene carries the ACTUAL branch (derived before the
    # worktree check, Issue #50) — never a `branch=None` placeholder.
    assert "branch=orbi/owner-repo-issue-39-a1b2c3d4" in body
    assert "branch=None" not in body
    assert "delivery_review_failed" in caplog.text
    # The failure comment is written to the Issue AND the PR
    # (Issue #50).
    assert pr_comments == [body]
    # The recoverable failure posts the fix-needed milestone (Issue
    # #18) AND the tracked progress comment becomes the fix-needed
    # scene.
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted_bodies)
    assert any(
        "orbi-owner-repo-issue-39-a1b2c3d4" in body
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
    assert "orbi-owner-repo-issue-39-a1b2c3d4" in fix_needed
    assert "next step:" in fix_needed


def test_wait_for_delivery_worktree_missing_while_fix_needed_keeps_label(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    reviews: list = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
        "owner/repo",
    )
    # No review was started.
    assert reviews == []
    # The single transition: ai-pr-opened removed, ai-fix-needed added
    # (the label the Issue already carries is re-added idempotently —
    # the next tick's scan needs it; Issue #50: never ai-blocked).
    assert edits[0][1] == {
        "repo": "owner/repo",
        "add": "ai-fix-needed",
        "remove": "ai-pr-opened",
    }
    assert len(edits) == 1
    body = comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "orbi-owner-repo-issue-39-a1b2c3d4" in body


def test_wait_for_delivery_runs_review_when_fix_needed(
    monkeypatch, caplog, tmp_path,
):
    """While holding the slot, a fix-needed delivery runs the SAME
    independent review as an awaiting-review delivery (Issue #82: the
    review session fixes findings in the same session — no cold-start
    fixer): the review reports findings and the wait continues."""
    states = ["OPEN", "OPEN", "MERGED"]
    pr_view_calls = {"n": 0}
    labels_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            pr_view_calls["n"] += 1
            return json.dumps({
                "state": states[pr_view_calls["n"] - 1],
            })
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "labels":
                labels_calls["n"] += 1
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
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    # The derived worktree exists: a normal resume reaches the review
    # (Issue #90 fails fast only when the directory is missing).
    (tmp_path / ".worktrees"
     / "orbi-owner-repo-issue-39-a1b2c3d4").mkdir(parents=True)
    # The independent review runs for the fix-needed state too (Issue
    # #82) and reports findings, so the wait continues to the MERGED
    # poll.
    reviews = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    issue = {"number": 39, "title": "task", "body": "stale body"}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    config = {"repo_dir": tmp_path, "base_branch": "main"}
    runner.wait_for_delivery(PR_URL, issue, config, "owner/repo")
    # One review per OPEN+fix-needed poll (two polls before MERGED).
    assert len(reviews) == 2
    # No fixer: the review ran on the derived worktree/branch of the
    # same run.
    worktree, branch, base_branch, review_config, repo, number = reviews[0][0]
    assert branch == "orbi/owner-repo-issue-39-a1b2c3d4"
    assert base_branch == "main"
    assert review_config["run_id"] == "a1b2c3d4"
    assert review_config["base_sha"] == "abc123def456"
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.issue_labels(39, "owner/repo") == [
        "ai-ready", "ai-fix-needed",
    ]

    def bad_run(command, **kwargs):
        return json.dumps({"labels": "nope"})

    monkeypatch.setattr(runner, "run_command", bad_run)
    with pytest.raises(ValueError, match="issue labels must be a JSON array"):
        runner.issue_labels(39, "owner/repo")

    def bad_run2(command, **kwargs):
        return json.dumps(["ai-ready"])

    monkeypatch.setattr(runner, "run_command", bad_run2)
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_labels(39, "owner/repo")


def test_wait_for_delivery_marks_blocked_when_pr_closed_unmerged(
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("INFO")
    runner.wait_for_delivery(PR_URL, issue, {}, "owner/repo")
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
    # ... with a failure comment carrying the run marker and run_id.
    body = comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert f"<!-- orbi:run=a1b2c3d4 -->" in body
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
    assert "next step:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (Issue #82: both
    # opened-PR states are review states, so always `review`) and the
    # completed review rounds (review round 2, PR #42).
    assert "- role: review" in blocked
    assert "- review/fix round: 2" in blocked


def test_wait_for_delivery_review_failure_without_bound_run_id(
        monkeypatch, tmp_path,
):
    """When no run id is bound the review-failure comment simply carries
    no marker (the Issue is still marked ai-blocked)."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "issue"] and command[2] == "view":
            if command[-1] == "comments":
                return json.dumps({"comments": []})
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    edits: list = []
    comments: list = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    issue = {"number": 39, "title": "task", "body": ""}
    runner.wait_for_delivery(
        PR_URL, issue, {"repo_dir": tmp_path, "base_branch": "main"},
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


def test_wait_for_delivery_keeps_holding_when_no_delivery_label(
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
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    issue = {"number": 39, "title": "task", "body": ""}
    caplog.set_level("INFO")
    runner.wait_for_delivery(PR_URL, issue, {}, "owner/repo")
    # First poll: OPEN + no delivery label -> awaiting (no review);
    # second poll: MERGED -> terminal.
    assert pr_calls["n"] == 2
    assert "delivery_awaiting" in caplog.text


def test_wait_for_delivery_logs_awaiting_without_bound_run_id(monkeypatch, caplog):
    """When no run id is bound the wait still works: the failure comment
    simply carries no marker."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "view":
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"] and command[-1] == "labels":
            # No leftover fix-needed label: only ai-pr-opened is removed.
            return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not a pr/issue view.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    comments: list = []
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, {"number": 39, "title": "t", "body": ""}, {}, "owner/repo",
    )
    assert "Orbi failed:" in comments[0][1]["body"]
    assert "orbi:run=" not in comments[0][1]["body"]


def test_main_holds_slot_through_delivery_wait(monkeypatch, tmp_path):
    """The slot stays occupied while the delivery awaits review: a second
    concurrent runner must see capacity_full until the PR is merged."""
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *a, **k: PR_URL)

    started = threading.Event()
    release = threading.Event()

    def fake_wait(pr_url, iss, cfg, repo):
        started.set()
        assert release.wait(timeout=10), "wait must hold the slot"

    monkeypatch.setattr(runner, "wait_for_delivery", fake_wait)

    def run_main():
        return runner.main(["--config", str(config)])

    thread = threading.Thread(target=run_main)
    thread.start()
    assert started.wait(timeout=10)
    # While the delivery awaits review, the slot is still held: a second
    # take (another runner) is denied.
    slot_dir = tmp_path / ".orbi" / "slots"
    assert pilot_slots.acquire_slot(slot_dir, 1, os.getpid()) is None
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, os.getpid())]
    release.set()
    thread.join(timeout=10)
    # After the wait ends, the slot is released.
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]


# --- configurable Pi provider/model/thinking (Issue #119) -------------------


def _model_config(tmp_path, **extra):
    """Build a minimal config for run_pi/run_review with model keys."""
    config = {
        "prompt": tmp_path / "prompt.md",
        "prompt_review": tmp_path / "prompt_review.md",
        "repo_dir": tmp_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": "a1b2c3d4",
    }
    config.update(extra)
    return config


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
    config = runner.load_config(config_path)
    assert config["pi_provider"] is None
    assert config["pi_model"] is None
    assert config["pi_thinking"] is None


def test_load_config_reads_pi_model_keys(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'pi_provider = "openai"\n'
        'pi_model = "gpt-5.6-sol"\n'
        'pi_thinking = "medium"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config["pi_provider"] == "openai"
    assert config["pi_model"] == "gpt-5.6-sol"
    assert config["pi_thinking"] == "medium"


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
        runner.load_config(config_path)


def test_run_pi_passes_configured_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(
        tmp_path, pi_provider="openai", pi_model="gpt-5.6-sol",
        pi_thinking="medium",
    )
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(tmp_path, pi_provider="openai")
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = _model_config(tmp_path)
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
    command, kwargs = calls[0]
    assert "--provider" not in command
    assert "--model" not in command
    assert "--thinking" not in command
    assert command[:3] == ["pi", "--print", "--session-dir"]
    assert kwargs["log_command"] == [
        "pi", "--print", "--session-dir", str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_run_review_passes_configured_model_args(monkeypatch, tmp_path):
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "ok",
    )
    config = _model_config(
        tmp_path, pi_provider="openai", pi_model="gpt-5.6-sol",
        pi_thinking="medium",
    )
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
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


def test_run_review_omits_model_args_when_not_configured(monkeypatch, tmp_path):
    """Default compatibility for the review session as well."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "ok",
    )
    config = _model_config(tmp_path)
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
    command, kwargs = calls[0]
    assert "--provider" not in command
    assert "--model" not in command
    assert "--thinking" not in command
    assert kwargs["log_command"] == [
        "pi", "--print", "--session-dir", str(tmp_path / ".pi-session"),
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
    keys = {"pi_providers": '"pi-providers.json"'}
    keys.update(toml_keys)
    toml = 'source_repos = ["owner/repo"]\n' + "\n".join(
        f"{key} = {value}" for key, value in keys.items()
    ) + "\n"
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(toml, encoding="utf-8")
    return config_path


def test_load_config_pi_providers_absent_defaults_to_none(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config["pi_providers"] is None
    assert config["pi_providers_data"] is None


@pytest.mark.parametrize("value", ['""', "123", "true"])
def test_load_config_pi_providers_rejects_non_string(tmp_path, value):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f'pi_providers = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pi_providers must be a non-empty string"):
        runner.load_config(config_path)


def test_load_config_pi_providers_file_missing(tmp_path):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    (tmp_path / "pi-providers.json").unlink()
    with pytest.raises(FileNotFoundError):
        runner.load_config(config_path)


def test_load_config_pi_providers_invalid_json(tmp_path):
    config_path = _providers_config(tmp_path, GROQ_PROVIDERS)
    (tmp_path / "pi-providers.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        runner.load_config(config_path)


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
        runner.load_config(config_path)


def test_load_config_pi_providers_rejects_non_object_entry(tmp_path):
    config_path = _providers_config(
        tmp_path, {"providers": {"groq": "nope"}},
    )
    with pytest.raises(
        ValueError, match="provider 'groq' must be an object",
    ):
        runner.load_config(config_path)


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
        runner.load_config(config_path)


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
        runner.load_config(config_path)


def test_load_config_pi_providers_accepts_model_level_api(tmp_path):
    providers = {"providers": {"groq": {
        "baseUrl": "https://api.groq.com/openai/v1",
        "apiKey": "local",
        "models": [
            {"id": "m1", "api": "openai-completions"},
        ],
    }}}
    config_path = _providers_config(tmp_path, providers)
    config = runner.load_config(config_path)
    assert config["pi_providers_data"] == providers


def test_load_config_pi_providers_rejects_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"other"',
    )
    with pytest.raises(
        ValueError, match="pi_provider 'other' is not defined",
    ):
        runner.load_config(config_path)


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
        runner.load_config(config_path)


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
        match="API key for provider 'groq' references missing "
              "environment variable GROQ_API_KEY",
    ):
        runner.load_config(config_path)


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
        match="API key for provider 'groq' references missing "
              "environment variable GROQ_API_KEY",
    ):
        runner.load_config(config_path)


def test_load_config_pi_providers_accepts_groq_example(tmp_path, monkeypatch):
    """The Issue's Groq example parses verbatim with the key present."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS,
        pi_provider='"groq"', pi_model='"qwen/qwen3.8-27b"',
        pi_thinking='"medium"',
    )
    config = runner.load_config(config_path)
    assert config["pi_provider"] == "groq"
    assert config["pi_model"] == "qwen/qwen3.8-27b"
    assert config["pi_providers_data"] == GROQ_PROVIDERS
    assert config["pi_providers"].name == "pi-providers.json"


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
    config = runner.load_config(config_path)
    assert config["pi_providers_data"] == providers


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
    assert config.get("pi_providers_data") is None
    assert runner.prepare_pi_agent_dir(tmp_path, config) is None
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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


def test_prepare_pi_agent_dir_expands_api_key_env_reference(
    tmp_path, monkeypatch,
):
    """The per-run catalog carries the REAL key (Issue #303).

    `_load_pi_providers` validates that the selected provider's
    `$VAR` reference resolves; the materialized per-run `models.json`
    must close the validate/use gap and write the resolved value,
    otherwise the provider has no usable credential and the run
    cannot authenticate. The loaded config data keeps the literal
    reference (only the gitignored per-run copy materializes it).
    """
    home = tmp_path / "home"
    _user_agent_dir(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GROQ_API_KEY", "sk-real-key-value")
    config = _model_config(
        tmp_path, pi_providers_data=GROQ_PROVIDERS,
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
    )
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["groq"]["apiKey"] == "sk-real-key-value"
    # The loaded provider data (and the user's file behind it) keeps
    # the env-var reference: only the per-run copy materializes it.
    assert config["pi_providers_data"]["providers"]["groq"][
        "apiKey"
    ] == "$GROQ_API_KEY"


def test_prepare_pi_agent_dir_expands_braced_and_embedded_references(
    tmp_path, monkeypatch,
):
    """`${VAR}` and references embedded in larger literals expand too
    (Pi's documented interpolation syntax, `docs/models.md`)."""
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["local"]["apiKey"] == "alpha_mid_omega"


def test_prepare_pi_agent_dir_unresolved_reference_stays_verbatim(
    tmp_path, monkeypatch,
):
    """Only the SELECTED provider's key must resolve: an unselected
    provider whose variable is missing keeps the literal reference and
    stays unavailable in Pi (the pre-#303 behavior, never an error)."""
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["groq"]["apiKey"] == "sk-real-key-value"
    assert merged["providers"]["other"]["apiKey"] == "$MISSING_PROVIDER_KEY"


def test_prepare_pi_agent_dir_leaves_non_string_api_keys_untouched(
    tmp_path, monkeypatch,
):
    """Entries without a string `apiKey` (absent, or a malformed
    non-object entry from the user's own models.json) pass through
    unchanged — expansion never invents or crashes on them."""
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert "apiKey" not in merged["providers"]["nokey"]
    assert merged["providers"]["junk"] == "not-an-object"


def test_prepare_pi_agent_dir_expands_zai_scene_from_load_config(
    tmp_path, monkeypatch,
):
    """The Issue #303 scene end to end: a z.ai-shaped provider file
    (`apiKey: "$ZAI_API_KEY"`) loaded through `load_config` then
    materialized must carry the real key in the per-run catalog."""
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
    config = runner.load_config(config_path)
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    merged = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    )
    assert merged["providers"]["z-ai"]["apiKey"] == "sk-zai-real-key"
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
        runner.prepare_pi_agent_dir(tmp_path, config)


def test_prepare_pi_agent_dir_settings_drops_unresolvable_enabled_models(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _user_agent_dir(home, models=None, settings=False)
    _user_settings(home, {"enabledModels": ["openai/gpt-5.6-sol"]})
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
        runner.prepare_pi_agent_dir(tmp_path, config)


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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
        runner.prepare_pi_agent_dir(tmp_path, config)


def test_stream_pi_sets_pi_env_on_the_process(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('PI_CODING_AGENT_DIR', ''))\n"
    )
    result = runner.stream_pi(
        [sys.executable, "-c", script],
        cwd=tmp_path, poll_interval=0.1,
        run_id="run1", issue=24, source_repo="xqliu/orbi",
        branch="b", pi_env={"PI_CODING_AGENT_DIR": "/agent-dir"},
    )
    assert result == "/agent-dir"


def test_stream_pi_without_pi_env_keeps_inherited_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', ''))\n"
    )
    result = runner.stream_pi(
        [sys.executable, "-c", script],
        cwd=tmp_path, poll_interval=0.1,
        run_id="run1", issue=24, source_repo="xqliu/orbi",
        branch="b",
    )
    assert result == "inherited"


def test_run_pi_materializes_provider_dir_and_env(monkeypatch, tmp_path):
    """The implementer session gets the materialized dir via env."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "done",
    )
    config = _model_config(
        tmp_path, pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "done",
    )
    config = _model_config(tmp_path)
    runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
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
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(kwargs) or "ok",
    )
    config = _model_config(
        tmp_path, pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        config, "owner/repo", 4, "branch", 1,
    )
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
        runner.load_config(config_path)


def test_load_config_pi_providers_accepts_entry_without_models(tmp_path):
    """An override-only entry (no models, e.g. routing a built-in
    provider through a proxy) is valid — Pi's own schema allows it."""
    providers = {"providers": {"anthropic": {
        "baseUrl": "https://proxy.example.com/v1",
    }}}
    config_path = _providers_config(tmp_path, providers)
    config = runner.load_config(config_path)
    assert config["pi_providers_data"] == providers


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
        runner.load_config(config_path)


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
        runner.load_config(config_path)


def test_load_config_pi_providers_provider_without_model_ok(
    tmp_path, monkeypatch,
):
    """pi_provider set, pi_model unset: the model check is skipped
    (Pi resolves its own default model for the provider)."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config_path = _providers_config(
        tmp_path, GROQ_PROVIDERS, pi_provider='"groq"',
    )
    config = runner.load_config(config_path)
    assert config["pi_model"] is None


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
    config = runner.load_config(config_path)
    assert config["pi_providers_data"] == providers


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
        ValueError, match="missing environment variable GROQ_API_KEY",
    ):
        runner.load_config(config_path)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    config = runner.load_config(config_path)
    assert config["pi_providers_data"] == providers


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
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
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
    result = runner.run_pi(
        {"number": 4, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch="orbi/owner-repo-issue-4-a1b2c3d4",
    )
    assert result == "https://api.groq.com/openai/v1"


def test_prepare_pi_agent_dir_is_idempotent_on_resume(
    tmp_path, monkeypatch,
):
    """A resumed run in the same worktree replaces stale files."""
    home = tmp_path / "home"
    user_agent = _user_agent_dir(home, auth=False)
    monkeypatch.setenv("HOME", str(home))
    config = _model_config(tmp_path, pi_providers_data=GROQ_PROVIDERS)
    agent_dir = runner.prepare_pi_agent_dir(tmp_path, config)
    # Simulate stale leftovers from an earlier attempt: a broken auth
    # symlink and a settings symlink (the pre-#172 shape).
    stale = agent_dir / "auth.json"
    stale.symlink_to(tmp_path / "gone.json")
    assert not stale.exists()  # broken
    settings = agent_dir / "settings.json"
    settings.unlink()
    settings.symlink_to(user_agent / "settings.json")
    runner.prepare_pi_agent_dir(tmp_path, config)
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
    """A session JSONL with a tool call so the snapshot shows phase=test."""
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
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(
        48, "Log the stop scene", "orbi/owner-repo-issue-48-a1b2c3d4",
        str(worktree),
    )
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
    assert "branch=orbi/owner-repo-issue-48-a1b2c3d4" in line
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
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(48, "t", "b", str(worktree))
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    line = [line for line in caplog.text.splitlines()
            if " run_stopping " in line][0]
    assert "phase=test" in line
    assert "session=sess-48" in line
    assert 128 + died["signum"] == 143


def test_stop_handler_active_run_without_live_child_still_stops(
    monkeypatch, caplog, tmp_path,
):
    # Run in flight but no live Pi child (between sessions): both lines
    # are still logged and the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.set_active_run(48, "t", "b", str(worktree))
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
    monkeypatch.setattr(
        runner, "_ACTIVE_RUN",
        {"issue": 48, "title": "t", "branch": "b", "worktree": "/w",
         "pi": None},
    )
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
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
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
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
    monkeypatch.setattr(runner, "_ACTIVE_RUN", None)
    runner.set_active_run(7, "t", "b", "/w")
    assert runner._ACTIVE_RUN == {
        "issue": 7, "title": "t", "branch": "b", "worktree": "/w",
        "pi": None,
    }
    child = object()
    runner.set_active_pi(child)
    assert runner._ACTIVE_RUN["pi"] is child
    runner.set_active_pi(None)
    assert runner._ACTIVE_RUN["pi"] is None
    runner.clear_active_run()
    assert runner._ACTIVE_RUN is None
    # set_active_pi without a bound run is a no-op (never crashes).
    runner.set_active_pi(child)
    assert runner._ACTIVE_RUN is None


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
        runner, "pick_next_delivery", lambda *args: None,
    )
    monkeypatch.setattr(runner, "acquire_slot", lambda *args: Mock(release=lambda: None))
    monkeypatch.setattr(runner, "refresh_cli_install", lambda *a, **k: "unchanged")
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

logging.basicConfig(level=logging.INFO, format=runner.log_format())
# Exactly what main() does (Issue #48): install the stop handler first.
signal.signal(signal.SIGTERM, runner._handle_stop)
runner.set_run_id("a1b2c3d4")
runner.set_active_run(
    48, "Log the stop scene",
    "orbi/owner-repo-issue-48-a1b2c3d4", {worktree!r},
)
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
    assert "phase=test" in line
    assert "session=sess-48" in line
    assert "branch=orbi/owner-repo-issue-48-a1b2c3d4" in line
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


# --- Release task (Issue #98) ---------------------------------------------

RELEASE_DECLARATION_BODY = """Ship v0.3.0 to the remote.

## Release

- version: v0.3.0
- base_branch: main
- test_command: /usr/bin/python3 -m coverage run --branch -m pytest tests/ -q && /usr/bin/python3 -m coverage report --show-missing
- scope:
  - #123
  - #124

## Notes

- this text is outside the release section
"""


def test_parse_release_declaration_returns_all_fields():
    decl = runner.parse_release_declaration(RELEASE_DECLARATION_BODY)
    assert decl == {
        "version": "v0.3.0",
        "base_branch": "main",
        "test_command": (
            "/usr/bin/python3 -m coverage run --branch -m pytest tests/ -q "
            "&& /usr/bin/python3 -m coverage report --show-missing"
        ),
        "scope": [123, 124],
    }


def test_parse_release_declaration_requires_the_release_section():
    with pytest.raises(ValueError, match="## Release"):
        runner.parse_release_declaration("no section here\n")


def test_parse_release_declaration_requires_version():
    body = RELEASE_DECLARATION_BODY.replace("- version: v0.3.0\n", "")
    with pytest.raises(ValueError, match="version"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_requires_base_branch():
    body = RELEASE_DECLARATION_BODY.replace("- base_branch: main\n", "")
    with pytest.raises(ValueError, match="base_branch"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_requires_test_command():
    body = RELEASE_DECLARATION_BODY.replace("- test_command: ", "- test_command2: ")
    with pytest.raises(ValueError, match="test_command"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_requires_scope():
    lines = [line for line in RELEASE_DECLARATION_BODY.splitlines()
             if not line.strip().startswith("- #")]
    body = "\n".join(line for line in lines if line.strip() != "- scope:") + "\n"
    with pytest.raises(ValueError, match="scope"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_empty_scope():
    lines = [line for line in RELEASE_DECLARATION_BODY.splitlines()
             if not line.strip().startswith("- #")]
    with pytest.raises(ValueError, match="scope"):
        runner.parse_release_declaration("\n".join(lines) + "\n")


def test_parse_release_declaration_rejects_duplicate_field():
    body = RELEASE_DECLARATION_BODY.replace(
        "\n## Notes", "\n- version: v9.9.9\n\n## Notes",
    )
    with pytest.raises(ValueError, match="version"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_unknown_key():
    body = RELEASE_DECLARATION_BODY.replace(
        "\n## Notes", "\n- channel: stable\n\n## Notes",
    )
    with pytest.raises(ValueError, match="channel"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_version_with_space():
    body = RELEASE_DECLARATION_BODY.replace(
        "- version: v0.3.0", "- version: v0.3 .0",
    )
    with pytest.raises(ValueError, match="version"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_empty_value():
    body = RELEASE_DECLARATION_BODY.replace("- base_branch: main",
                                            "- base_branch:")
    with pytest.raises(ValueError, match="base_branch"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_malformed_scope_item():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - #abc")
    with pytest.raises(ValueError, match="scope item"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_scope_item_without_hash():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - 123")
    with pytest.raises(ValueError, match="scope item"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_zero_scope_item():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "  - #0")
    with pytest.raises(ValueError, match="scope item"):
        runner.parse_release_declaration(body)


def test_is_release_detects_the_label():
    issue = {"number": 99, "labels": [
        {"name": "ai-ready"}, {"name": "ai-release"},
    ]}
    assert runner.is_release(issue) is True


def test_is_release_false_without_label_or_malformed():
    assert runner.is_release({"number": 1, "labels": [{"name": "ai-ready"}]}) is False
    assert runner.is_release({"number": 1}) is False
    assert runner.is_release({"number": 1, "labels": "ai-release"}) is False


def test_process_issue_routes_release_to_process_release(monkeypatch):
    issue = {"number": 99, "title": "Release v0.3.0", "body": "",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    calls = []
    monkeypatch.setattr(runner, "process_release",
                        lambda i, c, r: calls.append("release") or "rel-url")
    monkeypatch.setattr(runner, "run_pi", Mock(
        side_effect=AssertionError("run_pi must not run for a release task")))
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "freeze_base", lambda r, b: "abc")
    result = runner.process_issue(issue, {"base_branch": "main"}, "o/r")
    assert result == "rel-url"
    assert calls == ["release"]


def test_is_ticket_only_requires_the_explicit_label():
    assert runner.is_ticket_only({"labels": [{"name": "ai-ticket-only"}]}) is True
    assert runner.is_ticket_only({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_ticket_only({"labels": "ai-ticket-only"}) is False


def test_run_ticket_agent_uses_a_temporary_session_without_git(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "copy",
    )
    result = runner.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        {"repo_dir": tmp_path, "run_id": "a1b2c3d4", "skills": [],
         "pi_provider": None, "pi_model": None, "pi_thinking": None},
        "o/r",
    )
    assert result == "copy"
    command, kwargs = calls[0]
    assert command[0] == "pi"
    # `pi --no-tools` is the enforcement boundary: a prompt instruction
    # alone cannot ensure the Agent never invokes git/gh or writes files.
    assert "--no-tools" in command
    assert "git/gh tools" in command[command.index("--system-prompt") + 1]
    assert kwargs["cwd"] != tmp_path
    assert kwargs["cwd"].name.startswith("orbi-ticket-")
    assert kwargs["branch"] == "-"
    assert kwargs["role"] == runner.ROLE_TICKET
    assert str(tmp_path) not in command[command.index("--session-dir") + 1]
    assert Path(command[command.index("--session-dir") + 1]).parent == kwargs["cwd"]


def test_run_ticket_agent_logs_provider_config_loaded(monkeypatch, tmp_path, caplog):
    """Issue #176: the ticket-only session logs the provider config
    line too (role=ticket) — every Pi session has the same startup
    sequence."""
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "copy")
    with caplog.at_level("INFO"):
        runner.run_ticket_agent(
            {"number": 99, "title": "Launch thread", "body": "Write copy"},
            {"repo_dir": tmp_path, "run_id": "a1b2c3d4", "skills": [],
             "pi_provider": "local-qwen", "pi_model": "qwen3.8:27b",
             "pi_thinking": None},
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
        state = runner._progress_state(
            issue=176, title="t", run_id="a1b2c3d4", role="implement",
            branch="b", worktree=tmp_path, started=time.monotonic(),
            pr_url=None, review_round=0, priority="normal",
            activity={"phase": sub_phase, "last_activity": None,
                      "action": None, "session_id": None},
        )
        assert state["phase"] == sub_phase
    # The tool-based phase passes through unchanged once the first
    # response arrived.
    state = runner._progress_state(
        issue=176, title="t", run_id="a1b2c3d4", role="implement",
        branch="b", worktree=tmp_path, started=time.monotonic(),
        pr_url=None, review_round=0, priority="normal",
        activity={"phase": "test", "last_activity": None,
                  "action": "bash pytest tests/", "session_id": "s1"},
    )
    assert state["phase"] == "test"


def test_process_ticket_only_posts_agent_output_without_git_delivery(monkeypatch):
    """A labeled content task is delivered in its Issue, never through Git."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ready"}, {"name": "ai-ticket-only"}]}
    edits = []
    comments = []
    commands = []
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(runner, "run_ticket_agent",
                        lambda *args, **kwargs: "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "_safe_publish", lambda **kwargs: None)
    monkeypatch.setattr(runner, "run_command",
                        lambda command, **kwargs: commands.append(command) or "")

    result = runner.process_issue(issue, {"repo_dir": Path("/repo")}, "o/r")

    assert result == "ticket-only"
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
             "labels": [{"name": "ai-ticket-only"}]}
    edits = []
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(runner, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "run_ticket_agent", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="returned no content"):
        runner.process_ticket_only(issue, {"repo_dir": Path("/repo")}, "o/r")
    assert edits[-1][1]["add"] == "ai-blocked"


def test_process_ticket_only_failure_marks_blocked_without_git_delivery(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ticket-only"}]}
    edits = []
    comments = []
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(runner, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(issue, {"repo_dir": Path("/repo")}, "o/r")
    assert edits[-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                              "remove": "ai-in-progress"})
    assert "No Git branch, commit, or PR was created." in comments[-1][1]["body"]
    assert "run_id=a1b2c3d4" in comments[-1][1]["body"]


def test_process_ticket_only_keeps_original_error_when_failure_reporting_fails(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ticket-only"}]}
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("comment failed")))
    monkeypatch.setattr(runner, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "_safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(issue, {"repo_dir": Path("/repo")}, "o/r")


def test_process_issue_keeps_normal_flow_without_release_label(
    monkeypatch, tmp_path,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(runner, "is_release", lambda i: False)
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id", lambda rid: None)
    monkeypatch.setattr(runner, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(runner, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "set_active_run", Mock())
    # The worktree exists: `create_worktree` always returns a real
    # directory (the run state file is written into it, Issue #219).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(runner, "create_worktree", lambda *a, **kwargs: worktree)
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "run_pi",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(runner, "deliver_pr",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(runner, "run_command", lambda c, **k: "")
    monkeypatch.setattr(runner, "wait_for_delivery", Mock())
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "LOGGER", Mock())
    monkeypatch.setattr(runner, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(runner, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(runner, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(runner, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(runner, "_finish_blocked_progress", Mock())
    runner.process_issue(
        issue, {"base_branch": "main", "repo_dir": tmp_path}, "o/r",
    )


def make_scope_gh(monkeypatch, *, pr_state_map=None, issue_state_map=None):
    """Answer `gh pr view` / `gh issue view` for scope verification.

    `pr_state_map`: number -> (state, merge_commit_oid) for PRs; a
    number absent from the map is NOT a PR (gh exits 1 with the real
    "Could not resolve to a PullRequest" error).
    `issue_state_map`: number -> state for Issues; a number absent from
    BOTH maps is neither.
    """
    pr_state_map = pr_state_map or {}
    issue_state_map = issue_state_map or {}
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            number = int(command[3])
            if number not in pr_state_map:
                raise subprocess.CalledProcessError(
                    1, command,
                    stderr=(
                        "GraphQL: Could not resolve to a PullRequest "
                        f"with the number of {number}. "
                        "(repository.pullRequest)"
                    ),
                )
            state, oid = pr_state_map[number]
            return json.dumps({
                "number": number, "state": state,
                "mergeCommit": {"oid": oid} if oid else None,
            })
        if command[:3] == ["gh", "issue", "view"]:
            number = int(command[3])
            if number not in issue_state_map:
                raise subprocess.CalledProcessError(
                    1, command,
                    stderr=(
                        "GraphQL: Could not resolve to an Issue with "
                        f"the number of {number}."
                    ),
                )
            return json.dumps({"number": number, "state": issue_state_map[number]})
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls


def test_verify_release_scope_evidence_for_pr_and_issue(monkeypatch):
    make_scope_gh(
        monkeypatch,
        pr_state_map={123: ("MERGED", "aaa111")},
        issue_state_map={124: "CLOSED"},
    )
    evidence = runner.verify_release_scope("o/r", [123, 124], Path("/repo"), "release123")
    assert evidence == [
        "PR #123 merged (mergeCommit=aaa111)",
        "Issue #124 closed",
    ]


def test_verify_release_scope_fails_on_unmerged_pr(monkeypatch):
    make_scope_gh(monkeypatch, pr_state_map={123: ("OPEN", None)})
    with pytest.raises(RuntimeError, match="PR #123 is not merged"):
        runner.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_rejects_merged_pr_outside_release_base(
        monkeypatch):
    """A PR merged on another branch is not evidence for this tag."""
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", "other123")})
    original = runner.run_command

    def not_an_ancestor(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", not_an_ancestor)
    with pytest.raises(RuntimeError, match="not contained in release commit"):
        runner.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_reraises_git_ancestry_check_failure(
        monkeypatch):
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", "aaa111")})
    original = runner.run_command

    def git_failure(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(128, command, stderr="bad object")
        return original(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", git_failure)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.verify_release_scope("o/r", [123], Path("/repo"), "release123")
    assert excinfo.value.stderr == "bad object"


def test_verify_release_scope_fails_on_unclosed_issue(monkeypatch):
    make_scope_gh(monkeypatch, issue_state_map={124: "OPEN"})
    with pytest.raises(RuntimeError, match="Issue #124 is not closed"):
        runner.verify_release_scope("o/r", [124], Path("/repo"), "release123")


def test_verify_release_scope_fails_on_merged_pr_without_merge_commit(
        monkeypatch):
    # A MERGED PR without merge-commit evidence is not evidence.
    make_scope_gh(monkeypatch, pr_state_map={123: ("MERGED", None)})
    with pytest.raises(RuntimeError, match="no merge commit evidence"):
        runner.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def test_verify_release_scope_fails_on_unknown_item(monkeypatch):
    make_scope_gh(monkeypatch)
    with pytest.raises(RuntimeError, match="neither a PR nor an Issue"):
        runner.verify_release_scope("o/r", [999], Path("/repo"), "release123")


def test_verify_release_scope_reraises_real_gh_failure(monkeypatch):
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 403: rate limited",
        )
    monkeypatch.setattr(runner, "run_command", fake_run_command)
    with pytest.raises(subprocess.CalledProcessError):
        runner.verify_release_scope("o/r", [123], Path("/repo"), "release123")


def make_gate_gh(monkeypatch, *, leftover_labels=None, check_runs=None,
                 open_prs=None):
    """Answer the gh calls of `check_release_gates`.

    `leftover_labels`: label -> [issue numbers] still open with it.
    `check_runs`: list of (name, status, conclusion) for the release
    commit (None -> the call fails, which must propagate).
    `open_prs`: list of (number, head_ref) open against the base.
    """
    leftover_labels = leftover_labels or {}
    open_prs = open_prs or []
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            label = command[command.index("--label") + 1]
            return json.dumps([{"number": n} for n in leftover_labels.get(label, [])])
        if command[:2] == ["gh", "api"]:
            if check_runs is None:
                raise subprocess.CalledProcessError(
                    1, command, stderr="HTTP 500: server error",
                )
            return json.dumps([
                {"name": name, "status": status, "conclusion": conclusion}
                for name, status, conclusion in check_runs
            ])
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                {"number": n, "headRefName": head} for n, head in open_prs
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls


def test_check_release_gates_pass_clean(monkeypatch):
    calls = make_gate_gh(
        monkeypatch,
        check_runs=[("tests", "completed", "success"),
                    ("lint", "completed", "skipped")],
    )
    evidence = runner.check_release_gates("o/r", "main", "abc123", 99)
    assert evidence == [
        "no open Issue carries ai-in-progress / ai-pr-opened / ai-fix-needed",
        "CI on the release commit: 2 check(s) all success/neutral/skipped",
        "no open PR targets main",
    ]
    # The release Issue itself is excluded from the leftover scan:
    # every leftover scan fetched the label's open Issues.
    labels = [c[c.index("--label") + 1] for c in calls
              if c[:3] == ["gh", "issue", "list"]]
    assert labels == ["ai-in-progress", "ai-pr-opened", "ai-fix-needed"]


def test_check_release_gates_excludes_the_release_issue_itself(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-in-progress": [99]},  # the release Issue
        check_runs=[],
    )
    evidence = runner.check_release_gates("o/r", "main", "abc123", 99)
    assert evidence[0].startswith("no open Issue carries")


def test_check_release_gates_fails_on_leftover_in_progress(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-in-progress": [7]},
        check_runs=[],
    )
    with pytest.raises(RuntimeError, match="Issue #7 still carries ai-in-progress"):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_check_release_gates_fails_on_leftover_fix_needed(monkeypatch):
    make_gate_gh(
        monkeypatch,
        leftover_labels={"ai-fix-needed": [8]},
        check_runs=[],
    )
    with pytest.raises(RuntimeError, match="Issue #8 still carries ai-fix-needed"):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_check_release_gates_fails_on_failing_ci(monkeypatch):
    make_gate_gh(
        monkeypatch,
        check_runs=[("tests", "completed", "failure")],
    )
    with pytest.raises(RuntimeError, match="check 'tests' is completed/failure"):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_check_release_gates_fails_on_pending_ci(monkeypatch):
    make_gate_gh(
        monkeypatch,
        check_runs=[("tests", "in_progress", None)],
    )
    with pytest.raises(RuntimeError, match="check 'tests' is in_progress/None"):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_check_release_gates_reraises_real_gh_failure(monkeypatch):
    make_gate_gh(monkeypatch, check_runs=None)
    with pytest.raises(subprocess.CalledProcessError):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_check_release_gates_fails_on_open_pr_against_base(monkeypatch):
    make_gate_gh(
        monkeypatch,
        check_runs=[],
        open_prs=[(55, "feature/x")],
    )
    with pytest.raises(RuntimeError,
                       match="PR #55 \\(head feature/x\\) is still open against main"):
        runner.check_release_gates("o/r", "main", "abc123", 99)


def test_run_release_tests_wraps_the_command_in_timeout_bash(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_command",
                        lambda c, **k: calls.append((c, k)) or "")
    runner.run_release_tests(Path("/wt"), "pytest -q", 120)
    (command, kwargs), = calls
    assert command == [
        "timeout", "120", "bash", "-c",
        "pytest -q && /usr/bin/python3 -m coverage report "
        "--show-missing && /usr/bin/python3 coverage_gate.py",
    ]
    assert kwargs == {"cwd": Path("/wt")}


def test_run_release_tests_fails_fast_on_nonzero(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="1 failed",
        )
    monkeypatch.setattr(runner, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_release_tests(Path("/wt"), "pytest -q", 120)


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


def test_release_tag_commit_returns_none_for_missing_remote_tag(tmp_path):
    work, _ = make_local_remote_pair(tmp_path)
    assert runner.release_tag_commit(work, "v9.9.9") is None


def test_release_tag_commit_returns_the_tagged_commit(tmp_path):
    work, head = make_local_remote_pair(tmp_path)
    subprocess.run(
        ["git", "-C", str(work), "tag", "-a", "v0.1.0", "-m", "rel", head],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "v0.1.0"],
        check=True, capture_output=True,
    )
    assert runner.release_tag_commit(work, "v0.1.0") == head


def test_release_tag_commit_reraises_real_fetch_failure(tmp_path, monkeypatch):
    work, _ = make_local_remote_pair(tmp_path)
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")
    monkeypatch.setattr(runner, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        runner.release_tag_commit(work, "v0.1.0")


def make_release_gh(monkeypatch, *, release_exists=False,
                    release_body="## Changelog"):
    """Answer `gh release view` / `gh release create` / `gh release edit`."""
    calls = []
    state = {"exists": release_exists, "body": release_body}

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "release", "view"]:
            if not state["exists"]:
                raise subprocess.CalledProcessError(
                    1, command, stderr="release not found",
                )
            return json.dumps({
                "tagName": command[3],
                "url": "https://github.com/o/r/releases/tag/" + command[3],
                "body": state["body"],
            })
        if command[:3] == ["gh", "release", "create"]:
            state["exists"] = True
            return ""
        if command[:3] == ["gh", "release", "edit"]:
            state["body"] = command[command.index("--notes") + 1]
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls


def test_build_release_changelog_groups_descriptions_links_and_orders(monkeypatch):
    source = {
        30: {
            "number": 30, "title": "Deploy the exporter as a persistent service",
            "body": "The service keeps metrics available after restart.",
            "url": "https://github.com/o/r/issues/30",
            "labels": [{"name": "bug"}],
            "closedByPullRequestsReferences": [{
                "number": 301, "url": "https://github.com/o/r/pull/301",
            }],
        },
        10: {
            "number": 10, "title": "Add Prometheus metrics dashboard",
            "body": "Users can inspect delivery metrics.",
            "url": "https://github.com/o/r/issues/10",
            "labels": [{"name": "enhancement"}],
            "closedByPullRequestsReferences": [],
        },
        20: {
            "number": 20, "title": "Explain the full setup workflow",
            "body": "Documentation covers first use.",
            "url": "https://github.com/o/r/issues/20",
            "labels": [{"name": "documentation"}],
            "closedByPullRequestsReferences": [],
        },
    }

    def fake_run_command(command, **kwargs):
        assert command[:3] == ["gh", "issue", "view"]
        return json.dumps(source[int(command[3])])

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    changelog = runner.build_release_changelog("o/r", [30, 10, 20])
    assert changelog == """## Changelog

### Deployment and operations

- Deploy the exporter as a persistent service ([Issue #30](https://github.com/o/r/issues/30); [PR #301](https://github.com/o/r/pull/301))

### Observability

- Add Prometheus metrics dashboard ([Issue #10](https://github.com/o/r/issues/10))

### Documentation

- Explain the full setup workflow ([Issue #20](https://github.com/o/r/issues/20))"""


def test_release_changelog_category_covers_reliability_bug_and_features():
    assert runner.release_changelog_category({
        "title": "Recover a stalled delivery", "labels": [],
    }) == "Reliability and recovery"
    assert runner.release_changelog_category({
        "title": "Fix an unrelated failure", "labels": [{"name": "bug"}],
    }) == "Bug fixes"
    assert runner.release_changelog_category({
        "title": "Add a provider setting", "labels": "malformed",
    }) == "Features"


def test_build_release_changelog_fails_on_empty_or_malformed_evidence(monkeypatch):
    bad_items = [
        {"number": 10, "title": "", "body": "", "url": "https://github.com/o/r/issues/10", "labels": [], "closedByPullRequestsReferences": []},
        {"number": 10, "title": "Useful title", "body": "detail", "url": "not-a-url", "labels": [], "closedByPullRequestsReferences": []},
        {"number": 10, "title": "Useful title", "body": "detail", "url": "https://github.com/o/r/issues/10", "labels": [], "closedByPullRequestsReferences": "malformed"},
        {"number": 10, "title": "Useful title", "body": "detail", "url": "https://github.com/o/r/issues/10", "labels": [], "closedByPullRequestsReferences": [{"number": "bad", "url": "https://github.com/o/r/pull/20"}]},
    ]
    for item in bad_items:
        monkeypatch.setattr(runner, "run_command", lambda *args, item=item, **kwargs: json.dumps(item))
        with pytest.raises(ValueError, match="release changelog"):
            runner.build_release_changelog("o/r", [10])
    monkeypatch.setattr(runner, "run_command", lambda *args, **kwargs: json.dumps({
        "number": 10, "title": "", "body": "\nConcrete summary\n",
        "url": "https://github.com/o/r/issues/10", "labels": [],
        "closedByPullRequestsReferences": [],
    }))
    assert "Concrete summary" in runner.build_release_changelog("o/r", [10])
    monkeypatch.setattr(runner, "run_command", lambda *args, **kwargs: json.dumps({
        "number": 10, "title": "Merge direct release work", "body": "",
        "url": "https://github.com/o/r/pull/10", "labels": [],
        "closedByPullRequestsReferences": [],
    }))
    assert "[PR #10](https://github.com/o/r/pull/10)" in (
        runner.build_release_changelog("o/r", [10])
    )


def test_publish_release_creates_when_missing_and_returns_url(monkeypatch):
    calls = make_release_gh(monkeypatch, release_exists=False)
    url = runner.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog\n\n### Features\n\n- A useful change ([Issue #123](https://github.com/o/r/issues/123))",
        scope_evidence=["PR #123 merged (mergeCommit=aaa111)"],
        gate_evidence=["no open Issue carries ai-in-progress"],
        test_evidence="tests passed (exit 0)",
        run_id="a1b2c3d4", issue_number=99,
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    creates = [c for c in calls if c[:3] == ["gh", "release", "create"]]
    assert len(creates) == 1
    create = creates[0]
    assert create[:3] == ["gh", "release", "create"]
    assert create[3] == "v0.3.0"
    assert "--verify-tag" in create
    notes = create[create.index("--notes") + 1]
    assert "v0.3.0" in notes
    assert "## Changelog" in notes
    assert "A useful change" in notes
    assert "abc123" in notes
    assert "PR #123 merged (mergeCommit=aaa111)" in notes
    assert "no open Issue carries ai-in-progress" in notes
    assert "tests passed (exit 0)" in notes
    assert "<!-- orbi:run=a1b2c3d4 -->" in notes
    assert "run_id=a1b2c3d4" in notes
    assert "Issue #99" in notes


def test_publish_release_reuses_the_existing_release(monkeypatch):
    calls = make_release_gh(monkeypatch, release_exists=True)
    url = runner.publish_release(
        repo="o/r", tag="v0.3.0", version="v0.3.0",
        release_commit="abc123", changelog="## Changelog",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99,
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert not [c for c in calls if c[:3] == ["gh", "release", "create"]]
    assert not [c for c in calls if c[:3] == ["gh", "release", "edit"]]


def test_publish_release_upgrades_existing_notes_without_a_changelog(monkeypatch):
    calls = make_release_gh(
        monkeypatch, release_exists=True, release_body="# v0.2.0\n\nIssue #10 closed",
    )
    runner.publish_release(
        repo="o/r", tag="v0.2.0", version="v0.2.0",
        release_commit="abc123", changelog="## Changelog\n\n### Features\n\n- Useful change",
        scope_evidence=[], gate_evidence=[], test_evidence="ok",
        run_id="a1b2c3d4", issue_number=99,
    )
    edits = [c for c in calls if c[:3] == ["gh", "release", "edit"]]
    assert len(edits) == 1
    assert edits[0][:6] == ["gh", "release", "edit", "v0.2.0", "--repo", "o/r"]
    assert "## Changelog" in edits[0][edits[0].index("--notes") + 1]


def test_publish_release_reraises_real_gh_failure(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 403: rate limited",
        )
    monkeypatch.setattr(runner, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        runner.publish_release(
            repo="o/r", tag="v0.3.0", version="v0.3.0",
            release_commit="abc123", changelog="## Changelog",
            scope_evidence=[], gate_evidence=[], test_evidence="ok",
            run_id="a1b2c3d4", issue_number=99,
        )


def make_release_process_env(monkeypatch, *, body=RELEASE_DECLARATION_BODY,
                             tag_commit=None, release_url="https://github.com/o/r/releases/tag/v0.3.0",
                             in_progress=False, existing_run_id=None):
    """Full fake environment for `process_release`.

    Returns a dict of captured state: edit_issue / comment_issue calls,
    run_command calls, and the monkeypatched pieces.
    """
    state = {
        "edits": [], "comments": [], "commands": [],
        "run_ids": [], "active_runs": [], "sync_docs_calls": [],
    }

    def fake_run_command(command, **kwargs):
        state["commands"].append((command, kwargs))
        if command[:3] == ["gh", "pr", "view"]:
            number = int(command[3])
            if number == 123:
                return json.dumps({
                    "number": 123, "state": "MERGED",
                    "mergeCommit": {"oid": "aaa111"},
                })
            raise subprocess.CalledProcessError(
                1, command,
                stderr=(
                    "GraphQL: Could not resolve to a PullRequest with "
                    f"the number of {number}. (repository.pullRequest)"
                ),
            )
        if command[:3] == ["gh", "issue", "view"]:
            number = int(command[3])
            fields = command[command.index("--json") + 1]
            if fields == "number,state" and number == 124:
                return json.dumps({"number": 124, "state": "CLOSED"})
            if fields.startswith("number,title,") and number in (123, 124):
                return json.dumps({
                    "number": number,
                    "title": f"Deliver release scope item {number}",
                    "body": "Concrete release behavior.",
                    "url": f"https://github.com/o/r/issues/{number}",
                    "labels": [],
                    "closedByPullRequestsReferences": [],
                })
            raise subprocess.CalledProcessError(
                1, command,
                stderr=(
                    "GraphQL: Could not resolve to an Issue with the "
                    f"number of {number}."
                ),
            )
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        if command == ["gh", "api",
                       "repos/o/r/milestones?state=all", "--paginate"]:
            return json.dumps([
                {"number": 1, "title": "v0.2.0", "state": "closed",
                 "open_issues": 0, "closed_issues": 28,
                 "url": "https://api.github.com/repos/o/r/milestones/1",
                 "html_url": "https://github.com/o/r/milestone/1"},
                {"number": 5, "title": "v0.3.0", "state": "open",
                 "open_issues": 0, "closed_issues": 2,
                 "url": "https://api.github.com/repos/o/r/milestones/5",
                 "html_url": "https://github.com/o/r/milestone/5"},
            ])
        if command == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps({"number": 5, "title": "v0.3.0",
                               "state": "closed", "open_issues": 0})
        if command[:2] == ["gh", "api"]:
            return json.dumps([
                {"name": "tests", "status": "completed",
                 "conclusion": "success"},
            ])
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:2] == ["git", "fetch"]:
            return ""
        if command[:2] == ["git", "tag"]:
            return ""
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return ""
        if command[:2] == ["git", "push"]:
            return ""
        if command[0] == "timeout":
            return ""
        if command[:3] == ["gh", "issue", "close"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "set_run_id",
                        lambda rid: state["run_ids"].append(rid))
    monkeypatch.setattr(runner, "has_in_progress_label",
                        lambda n, r: in_progress)
    monkeypatch.setattr(runner, "latest_run_id",
                        lambda r, s, n: existing_run_id)
    monkeypatch.setattr(runner, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(runner, "edit_issue",
                        lambda n, **k: state["edits"].append((n, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda n, **k: state["comments"].append((n, k)))
    monkeypatch.setattr(runner, "create_worktree",
                        lambda *a: Path("/wt"))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(runner, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(runner, "set_active_run",
                        lambda *a: state["active_runs"].append(a))
    monkeypatch.setattr(runner, "LOGGER", Mock())
    monkeypatch.setattr(runner, "release_tag_commit",
                        lambda r, t: tag_commit)
    monkeypatch.setattr(runner, "publish_release", lambda **k: release_url)
    # Issue #275: the docs sync step is covered by its own unit tests
    # (real git repos); here it is stubbed so the orchestration order is
    # what is asserted.
    def fake_sync_docs(**kwargs):
        state["sync_docs_calls"].append(kwargs)
        return "docs release notes for " + kwargs["tag"] + " synced to base"

    monkeypatch.setattr(runner, "sync_release_docs", fake_sync_docs)
    return state


def test_process_release_success_end_to_end(monkeypatch):
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    # Claim first, terminal ai-merged at the end (ai-in-progress removed).
    assert state["edits"][0] == (99, {"repo": "o/r",
                                      "add": "ai-in-progress"})
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-merged",
                                       "remove": "ai-in-progress"})
    # The tag was created at the release commit and pushed plainly.
    commands = [c for c, _ in state["commands"]]
    assert (["git", "tag", "-a", "v0.3.0", "-m", "Release v0.3.0",
             "abc123"], {"cwd": Path("/r")}) in state["commands"]
    assert (["git", "push", "origin", "refs/tags/v0.3.0"],
            {"cwd": Path("/r")}) in state["commands"]
    assert not any("--force" in c or "-f" == c for c in commands)
    # The release Issue is closed.
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    # Issue #214: the Milestone whose title is EXACTLY the released
    # version (v0.3.0 here) is closed via the official REST contract
    # AFTER the release Issue is closed — and no other Milestone is
    # touched.
    close_milestone = ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]
    assert close_milestone in commands
    assert commands.index(close_milestone) > commands.index(
        ["gh", "issue", "close", "99", "--repo", "o/r"])
    assert not [c for c in commands
                if c[:2] == ["gh", "api"] and "milestones/1" in c[2]]
    # The declared test command ran timeout-wrapped in the worktree.
    assert (["timeout", str(runner.RELEASE_TEST_TIMEOUT_SECONDS),
             "bash", "-c",
             "/usr/bin/python3 -m coverage run --branch -m pytest tests/ -q "
             "&& /usr/bin/python3 -m coverage report --show-missing && "
             "/usr/bin/python3 -m coverage report --show-missing && "
             "/usr/bin/python3 coverage_gate.py"],
            {"cwd": Path("/wt")}) in state["commands"]
    # Issue #275: the docs sync step runs once, after the GitHub Release
    # is published and before the Milestone is closed, with the release
    # identity.
    assert len(state["sync_docs_calls"]) == 1
    sync_call = state["sync_docs_calls"][0]
    assert sync_call == {
        "source_repo": "o/r", "repo_dir": Path("/r"),
        "worktree": Path("/wt"), "base_branch": "main", "tag": "v0.3.0",
        "release_commit": "abc123", "issue_number": 99,
    }
    # The success comment carries the run marker and the release URL.
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]
    assert "run_id=a1b2c3d4" in comment_kwargs["body"]
    assert "https://github.com/o/r/releases/tag/v0.3.0" in comment_kwargs["body"]
    assert "PR #123 merged (mergeCommit=aaa111)" in comment_kwargs["body"]
    assert "Issue #124 closed" in comment_kwargs["body"]
    assert "docs release notes for v0.3.0 synced to base" \
        in comment_kwargs["body"]
    assert state["run_ids"][0] == "a1b2c3d4"


def test_create_repair_issue_deduplicates_a_matching_failure_signature(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:5] == ["timeout", "30", "gh", "issue", "list"]:
            return json.dumps([{"number": 77, "url": "https://github.com/o/r/issues/77"}])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    url = runner.create_repair_issue(
        repo="o/r", source_issue=99, run_id="a1b2c3d4",
        release_commit="abc123", command="pytest -q", evidence="1 failed",
    )
    assert url == "https://github.com/o/r/issues/77"
    assert calls[0][:5] == ["timeout", "30", "gh", "issue", "list"]
    search = calls[0][calls[0].index("--search") + 1]
    assert "orbi-repair-signature=" in search
    assert "--label" not in calls[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_create_repair_issue_creates_a_reproducible_ready_bug(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:5] == ["timeout", "30", "gh", "issue", "list"]:
            return "[]"
        if command[:5] == ["timeout", "30", "gh", "issue", "create"]:
            return "https://github.com/o/r/issues/77"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    url = runner.create_repair_issue(
        repo="o/r", source_issue=99, run_id="a1b2c3d4",
        release_commit="abc123", command="pytest -q", evidence="1 failed",
    )
    assert url == "https://github.com/o/r/issues/77"
    create = calls[1]
    assert create[:5] == ["timeout", "30", "gh", "issue", "create"]
    assert create.count("--label") == 2
    assert "ai-ready" in create and "bug" in create
    body = create[create.index("--body") + 1]
    assert "source Issue: #99" in body
    assert "run_id=a1b2c3d4" in body
    assert "commit: `abc123`" in body
    assert "pytest -q" in body and "1 failed" in body
    assert "orbi-repair-signature=" in body
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_process_release_milestone_failure_fails_fast_and_blocks(monkeypatch):
    state = make_release_process_env(monkeypatch)
    real = runner.run_command

    def milestone_failing(command, **kwargs):
        # The v0.3.0 Milestone still has an open Issue: the close must
        # fail fast and the release must not be reported as successful.
        if command[:2] == ["gh", "api"] and "?state=all" in command[2]:
            return json.dumps([
                {"number": 5, "title": "v0.3.0", "state": "open",
                 "open_issues": 1, "closed_issues": 2,
                 "url": "https://api.github.com/repos/o/r/milestones/5",
                 "html_url": "https://github.com/o/r/milestone/5"},
            ])
        if command[:3] == ["gh", "issue", "list"] and "--milestone" in command:
            return json.dumps(
                [{"number": 101, "title": "leftover"}],
            )
        return real(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", milestone_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(RuntimeError, match="Milestone #5"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    commands = [c for c, _ in state["commands"]]
    # The release itself succeeded (tag pushed, Release published,
    # Issue closed with ai-merged)...
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    assert any(k.get("add") == "ai-merged" for _, k in state["edits"])
    # ...but the Milestone close failed: no PATCH was issued, the run
    # failed fast and the terminal state is ai-blocked with the
    # concrete reason on the Issue.
    assert not [c for c in commands if "PATCH" in c]
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "Orbi release failed (ai-blocked)" in comment_kwargs["body"]
    assert "Milestone #5" in comment_kwargs["body"]
    assert "#101" in comment_kwargs["body"]


def test_process_release_docs_sync_failure_fails_fast_and_blocks(monkeypatch):
    state = make_release_process_env(monkeypatch)

    def sync_failing(**kwargs):
        raise RuntimeError(
            "release v0.3.0: docs release notes commit push rejected "
            "(non-fast-forward)"
        )

    monkeypatch.setattr(runner, "sync_release_docs", sync_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(RuntimeError, match="non-fast-forward"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    # The GitHub Release was published (step 7 succeeded) but the docs
    # sync failed: the release is NOT reported successful — the terminal
    # state is ai-blocked with the concrete reason on the Issue.
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    assert not any(k.get("add") == "ai-merged" for _, k in state["edits"])
    assert not [c for c, _ in state["commands"]
                if c[:2] == ["gh", "api"] and "PATCH" in c]
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert "Orbi release failed (ai-blocked)" in comment_kwargs["body"]
    assert "non-fast-forward" in comment_kwargs["body"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]


def test_process_release_reuses_the_run_id_on_resume(monkeypatch):
    state = make_release_process_env(
        monkeypatch, in_progress=True, existing_run_id="deadbeef",
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"},
                        {"name": "ai-in-progress"}]}
    runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    # The fresh id is generated first (the normal-path rule), then the
    # resumed run id wins — the terminal comment carries the resumed id.
    assert state["run_ids"] == ["a1b2c3d4", "deadbeef"]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=deadbeef -->" in comment_kwargs["body"]


def test_process_release_fails_on_malformed_declaration(monkeypatch):
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0", "body": "no section",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(ValueError, match="## Release"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    # Terminal failure: ai-blocked ALONE, no ai-merged, no close.
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    assert not any(k.get("add") == "ai-merged" for _, k in state["edits"])
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:3] == ["gh", "issue", "close"]]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]
    assert "## Release" in comment_kwargs["body"]


def test_release_test_evidence_keeps_stdout_and_stderr():
    error = subprocess.CalledProcessError(
        1, ["timeout"], output="tests/test_x.py::test_y FAILED\n",
        stderr="coverage gate failed\n",
    )
    assert runner.release_test_evidence(error) == (
        "[stdout]\ntests/test_x.py::test_y FAILED\n\n"
        "[stderr]\ncoverage gate failed"
    )


def test_process_release_test_failure_creates_repair_and_keeps_release_blocked(monkeypatch):
    state = make_release_process_env(monkeypatch)

    def test_failure(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, ["timeout", "1800", "bash", "-c", "pytest -q"],
            stderr="tests/test_x.py::test_y FAILED",
        )

    repairs = []
    monkeypatch.setattr(runner, "run_release_tests", test_failure)
    monkeypatch.setattr(
        runner, "create_repair_issue",
        lambda **kwargs: repairs.append(kwargs) or "https://github.com/o/r/issues/77",
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(subprocess.CalledProcessError):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main",
                    "auto_repair_issues": True}, "o/r",
        )
    assert repairs == [{
        "repo": "o/r", "source_issue": 99, "run_id": "a1b2c3d4",
        "release_commit": "abc123",
        "command": RELEASE_DECLARATION_BODY.split("- test_command: ")[1].split("\n")[0],
        "evidence": "[stderr]\ntests/test_x.py::test_y FAILED",
    }]
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                        "remove": "ai-in-progress"})
    assert not any(k.get("add") == "ai-merged" for _, k in state["edits"])


def test_process_release_test_failure_stays_blocked_when_repair_opt_in_is_disabled(monkeypatch):
    state = make_release_process_env(monkeypatch)
    monkeypatch.setattr(
        runner, "run_release_tests",
        lambda *args: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["timeout"], stderr="1 failed"),
        ),
    )
    monkeypatch.setattr(
        runner, "create_repair_issue",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must remain opt-in")),
    )
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(subprocess.CalledProcessError):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                        "remove": "ai-in-progress"})


def test_process_release_repair_creation_failure_is_observable_and_preserves_test_failure(monkeypatch, caplog):
    make_release_process_env(monkeypatch)
    monkeypatch.setattr(runner, "LOGGER", logging.getLogger("repair-test"))
    original = subprocess.CalledProcessError(1, ["timeout"], stderr="1 failed")
    monkeypatch.setattr(runner, "run_release_tests",
                        lambda *args: (_ for _ in ()).throw(original))
    monkeypatch.setattr(runner, "create_repair_issue",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("GitHub unavailable")))
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main",
                    "auto_repair_issues": True}, "o/r",
        )
    assert excinfo.value is original
    assert "repair_issue_failed source_issue=99 run_id=a1b2c3d4" in caplog.text


def test_process_release_fails_on_tag_mismatch_without_moving_it(monkeypatch):
    state = make_release_process_env(monkeypatch, tag_commit="other123")
    # A genuine tag mismatch: the tag commit is NOT an ancestor of the base
    # (the merge-base check fails), so the release must fail fast instead of
    # recovering the tag commit (Issue #275).
    real_run = runner.run_command

    def merge_base_failing(command, **kwargs):
        # Only the tag check's merge-base call fails (the tag commit
        # "other123" is not an ancestor of the base); the scope-verification
        # merge-base call (ancestor "aaa111") still succeeds.
        if command[:3] == ["git", "merge-base", "--is-ancestor"] \
                and command[3] == "other123":
            raise subprocess.CalledProcessError(
                1, command, stderr="not an ancestor",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", merge_base_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(RuntimeError, match="never moved"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:2] == ["git", "tag"]]
    assert not [c for c in commands if c[:2] == ["git", "push"]]
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})
    (comment_number, comment_kwargs), = state["comments"]
    assert "other123" in comment_kwargs["body"]
    assert "abc123" in comment_kwargs["body"]


def test_process_release_reuses_a_matching_existing_tag(monkeypatch):
    state = make_release_process_env(monkeypatch, tag_commit="abc123")
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    commands = [c for c, _ in state["commands"]]
    assert not [c for c in commands if c[:2] == ["git", "tag"]]
    assert not [c for c in commands if c[:2] == ["git", "push"]]


def test_process_release_fails_on_scope_violation(monkeypatch):
    # Reuse the env, but make scope item #123 an OPEN PR.
    state = make_release_process_env(monkeypatch)
    real = runner.run_command
    def scope_failing(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"] and command[3] == "123":
            return json.dumps({
                "number": 123, "state": "OPEN", "mergeCommit": None,
            })
        return real(command, **kwargs)
    monkeypatch.setattr(runner, "run_command", scope_failing)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(RuntimeError, match="PR #123 is not merged"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                       "remove": "ai-in-progress"})


MILESTONE_LIST_COMMAND = [
    "gh", "api", "repos/o/r/milestones?state=all", "--paginate",
]


def _milestone(number, title, state, open_issues):
    return {
        "number": number, "title": title, "state": state,
        "open_issues": open_issues, "closed_issues": 0,
        "url": f"https://api.github.com/repos/o/r/milestones/{number}",
        "html_url": f"https://github.com/o/r/milestone/{number}",
    }


def test_close_release_milestone_closes_an_open_empty_milestone(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([
                _milestone(4, "v0.2.0", "closed", 0),
                _milestone(5, "v0.3.0", "open", 0),
                _milestone(6, "v0.4.0", "open", 3),
            ])
        if command == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps(_milestone(5, "v0.3.0", "closed", 0))
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    evidence = runner.close_release_milestone("o/r", "v0.3.0")
    # The close uses the official REST contract
    # (PATCH /repos/{owner}/{repo}/milestones/{number}, state=closed)
    # and the list query asks for ALL states (the idempotent case needs
    # the closed Milestones too).
    assert calls[-1] == ["gh", "api", "repos/o/r/milestones/5",
                        "--method", "PATCH", "-f", "state=closed"]
    assert calls[0] == MILESTONE_LIST_COMMAND
    assert "Milestone #5" in evidence
    assert "https://github.com/o/r/milestone/5" in evidence
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_is_idempotent_when_already_closed(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([
                _milestone(1, "v0.2.0", "closed", 0),
                _milestone(2, "v0.3.0", "closed", 0),
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    evidence = runner.close_release_milestone("o/r", "v0.3.0")
    # Already closed: no mutation at all (no PATCH, no reopen).
    assert calls == [MILESTONE_LIST_COMMAND]
    assert "already closed" in evidence
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_without_a_matching_title(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([
                _milestone(1, "v0.2.0", "closed", 0),
                _milestone(3, "v0.4.0", "open", 7),
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="no Milestone with the exact title"):
        runner.close_release_milestone("o/r", "v0.3.0")
    # No mutation, no fuzzy match against a different Milestone.
    assert calls == [MILESTONE_LIST_COMMAND]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_when_open_issues_remain(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([
                _milestone(5, "v0.3.0", "open", 2),
            ])
        if command == ["gh", "issue", "list", "--repo", "o/r",
                       "--milestone", "v0.3.0", "--state", "open",
                       "--json", "number,title", "--limit", "100"]:
            return json.dumps([
                {"number": 101, "title": "leftover one"},
                {"number": 102, "title": "leftover two"},
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="Milestone #5") as excinfo:
        runner.close_release_milestone("o/r", "v0.3.0")
    # The error carries the version, the Milestone number/url and the
    # open issue list — never a silent skip.
    assert "v0.3.0" in str(excinfo.value)
    assert "https://github.com/o/r/milestone/5" in str(excinfo.value)
    assert "#101" in str(excinfo.value)
    assert "#102" in str(excinfo.value)
    # No close was attempted.
    assert not [c for c in calls if "PATCH" in c]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_fails_fast_on_duplicate_titles(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps([
                _milestone(5, "v0.3.0", "open", 0),
                _milestone(7, "v0.3.0", "open", 0),
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="ambiguous") as excinfo:
        runner.close_release_milestone("o/r", "v0.3.0")
    # Both candidates are named; neither is closed.
    assert "#5" in str(excinfo.value)
    assert "#7" in str(excinfo.value)
    assert calls == [MILESTONE_LIST_COMMAND]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


# ---------------------------------------------------------------------------
# Release docs sync — state machine step 8 (Issue #275)
# ---------------------------------------------------------------------------

RELEASE_DOCS_BODY_V040 = (
    "# v0.4.0\n\n"
    "- tag: `v0.4.0`\n"
    "- release commit: `" + "c" * 40 + "`\n\n"
    "## Changelog\n\n"
    "- A useful change ([Issue #123](https://github.com/o/r/issues/123))"
)


def release_docs_fixture_config(latest_slug="release-v0.1.2") -> str:
    """The minimal Mintlify config the docs tests use: two languages,
    each with one release group listing `latest_slug` first."""
    return json.dumps({
        "$schema": "https://mintlify.com/docs.json",
        "name": "Orbi",
        "navigation": {
            "languages": [
                {
                    "language": "en",
                    "default": True,
                    "groups": [
                        {"group": "Getting Started",
                         "pages": ["index"]},
                        {"group": "Releases",
                         "pages": [latest_slug, "release-v0.1.1"]},
                    ],
                },
                {
                    "language": "zh",
                    "groups": [
                        {"group": "快速开始",
                         "pages": ["zh/index"]},
                        {"group": "发布",
                         "pages": [f"zh/{latest_slug}", "zh/release-v0.1.1"]},
                    ],
                },
            ],
        },
    }, indent=2, ensure_ascii=False) + "\n"


def make_release_docs_repo(tmp_path, latest_slug="release-v0.1.2"):
    """A real bare remote + clone carrying the release docs layout on
    `main`: docs.json (latest-first nav), the previous latest page with
    its `(latest)` marker (EN + ZH). Returns the clone path."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t"),
                       ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        subprocess.run(["git", "-C", str(work), "config", key, value],
                       check=True, capture_output=True)
    docs = work / "docs"
    zh = docs / "zh"
    zh.mkdir(parents=True)
    (docs / "docs.json").write_text(
        release_docs_fixture_config(latest_slug), encoding="utf-8",
    )
    (docs / f"{latest_slug}.mdx").write_text(
        f"# v0.1.2 release (latest)\n\nprevious latest content\n",
        encoding="utf-8",
    )
    (zh / f"{latest_slug}.mdx").write_text(
        "# v0.1.2 发布（最新）\n\n上一版内容\n", encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(work), "add", "docs"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "docs baseline"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin",
                    str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "main"],
                   check=True, capture_output=True)
    return work


def git_out(work: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(work), *args],
                           capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def test_git_out_helper_fails_fast_on_nonzero_exit(tmp_path):
    """The helper must fail fast (no silent swallow) when git exits
    non-zero — the same contract as the other git smoke helpers."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git_out(tmp_path, "rev-parse", "no-such-ref")


def fake_gh_release_view(monkeypatch, *, body: str, tag: str = "v0.4.0",
                         raise_not_found: bool = False):
    """Answer `gh release view` with canned JSON; everything else goes to
    the REAL run_command (real git)."""
    real = runner.run_command

    def mixed(command, **kwargs):
        if command[:3] == ["gh", "release", "view"]:
            if raise_not_found:
                raise subprocess.CalledProcessError(
                    1, command, stderr="release not found",
                )
            return json.dumps({
                "tagName": tag,
                "publishedAt": "2026-09-08T12:00:00Z",
                "url": f"https://github.com/o/r/releases/tag/{tag}",
                "body": body,
            })
        return real(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", mixed)
    return real


def test_release_docs_page_en_carries_meta_and_body_without_duplicate_heading():
    tag_object = "t" * 40
    release_commit = "c" * 40
    page = runner.release_docs_page(
        version="v0.4.0", tag_object=tag_object,
        release_commit=release_commit,
        published_at="2026-09-08T12:00:00Z",
        release_url="https://github.com/o/r/releases/tag/v0.4.0",
        issue_number=77, body=RELEASE_DOCS_BODY_V040, language="en",
    )
    assert page.startswith("# v0.4.0 release (latest)\n")
    assert "2026-09-08T12:00:00Z" in page
    assert "Issue #77" in page
    assert f"annotated tag `{tag_object}`" in page
    assert f"commit `{release_commit}`" in page
    assert "https://github.com/o/r/releases/tag/v0.4.0" in page
    assert "- A useful change" in page
    # The release body's own `# v0.4.0` heading is dropped: exactly one H1.
    assert page.count("# v0.4.0") == 1


def test_release_docs_page_zh_uses_the_chinese_title_and_table():
    page = runner.release_docs_page(
        version="v0.4.0", tag_object="t" * 40, release_commit="c" * 40,
        published_at="2026-09-08T12:00:00Z",
        release_url="https://github.com/o/r/releases/tag/v0.4.0",
        issue_number=77, body=RELEASE_DOCS_BODY_V040, language="zh",
    )
    assert page.startswith("# v0.4.0 发布（最新）\n")
    assert "注解 tag" in page
    assert "提交" in page
    assert "release task：Issue #77" in page
    assert "2026-09-08T12:00:00Z" in page
    assert "- A useful change" in page
    assert page.count("# v0.4.0") == 1


def test_release_docs_page_keeps_a_body_without_leading_heading():
    page = runner.release_docs_page(
        version="v0.4.0", tag_object="t" * 40, release_commit="c" * 40,
        published_at="2026-09-08T12:00:00Z",
        release_url="https://github.com/o/r/releases/tag/v0.4.0",
        issue_number=77, body="## Changelog\n\n- Legacy body", language="en",
    )
    assert "## Changelog" in page
    assert "- Legacy body" in page
    assert page.count("# v0.4.0") == 1


def test_update_release_navigation_inserts_the_new_version_first_in_both_groups():
    config_text = release_docs_fixture_config()
    new_text, changed = runner.update_release_navigation(
        config_text, "release-v0.4.0",
    )
    assert changed is True
    config = json.loads(new_text)
    languages = config["navigation"]["languages"]
    en_pages = languages[0]["groups"][1]["pages"]
    zh_pages = languages[1]["groups"][1]["pages"]
    assert en_pages == [
        "release-v0.4.0", "release-v0.1.2", "release-v0.1.1",
    ]
    assert zh_pages == [
        "zh/release-v0.4.0", "zh/release-v0.1.2", "zh/release-v0.1.1",
    ]


def test_update_release_navigation_is_idempotent_when_already_listed():
    config_text = release_docs_fixture_config("release-v0.4.0")
    new_text, changed = runner.update_release_navigation(
        config_text, "release-v0.4.0",
    )
    assert changed is False
    assert new_text == config_text


def test_move_latest_marker_strips_the_marker_from_both_previous_pages(tmp_path):
    work = tmp_path / "work"
    (work / "docs" / "zh").mkdir(parents=True)
    en = work / "docs" / "release-v0.1.2.mdx"
    zh = work / "docs" / "zh" / "release-v0.1.2.mdx"
    en.write_text("# v0.1.2 release (latest)\n\nrest\n", encoding="utf-8")
    zh.write_text("# v0.1.2 发布（最新）\n\n余下内容\n", encoding="utf-8")
    changed = runner.move_latest_marker(
        work, "release-v0.1.2", "release-v0.4.0", resume=False,
    )
    assert changed == ["docs/release-v0.1.2.mdx",
                      "docs/zh/release-v0.1.2.mdx"]
    assert en.read_text(encoding="utf-8") == "# v0.1.2 release\n\nrest\n"
    assert zh.read_text(encoding="utf-8") == "# v0.1.2 发布\n\n余下内容\n"


def test_move_latest_marker_fails_fast_when_the_marker_is_missing(tmp_path):
    work = tmp_path / "work"
    (work / "docs" / "zh").mkdir(parents=True)
    (work / "docs" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 release\n\nrest\n", encoding="utf-8",
    )
    (work / "docs" / "zh" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 发布\n\n余下内容\n", encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"does not carry the \(latest\)"):
        runner.move_latest_marker(
            work, "release-v0.1.2", "release-v0.4.0", resume=False,
        )


def test_move_latest_marker_accepts_an_already_moved_marker_on_resume(
        tmp_path):
    """Resume after a partial step: the old page already lost its marker
    and the new page already carries it — the move is a no-op, not an
    error (a permanent ai-blocked deadlock would be worse)."""
    work = tmp_path / "work"
    (work / "docs" / "zh").mkdir(parents=True)
    (work / "docs" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 release\n\nrest\n", encoding="utf-8",
    )
    (work / "docs" / "zh" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 发布\n\n余下内容\n", encoding="utf-8",
    )
    (work / "docs" / "release-v0.4.0.mdx").write_text(
        "# v0.4.0 release (latest)\n\nnew\n", encoding="utf-8",
    )
    (work / "docs" / "zh" / "release-v0.4.0.mdx").write_text(
        "# v0.4.0 发布（最新）\n\n新\n", encoding="utf-8",
    )
    assert runner.move_latest_marker(
        work, "release-v0.1.2", "release-v0.4.0", resume=True,
    ) == []


def test_move_latest_marker_fails_fast_when_the_previous_page_is_missing(
        tmp_path):
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="is missing"):
        runner.move_latest_marker(
            work, "release-v0.1.2", "release-v0.4.0", resume=False,
        )


def test_sync_release_docs_generates_pages_navigation_marker_and_commits(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    evidence = runner.sync_release_docs(
        source_repo="o/r", repo_dir=work, worktree=work,
        base_branch="main", tag="v0.4.0", release_commit=head,
        issue_number=77,
    )
    assert "v0.4.0" in evidence
    en = (work / "docs" / "release-v0.4.0.mdx").read_text(encoding="utf-8")
    zh = (work / "docs" / "zh" / "release-v0.4.0.mdx").read_text(encoding="utf-8")
    tag_object = git_out(work, "rev-parse", "refs/tags/v0.4.0")
    assert en.startswith("# v0.4.0 release (latest)\n")
    assert f"annotated tag `{tag_object}`" in en
    assert f"commit `{head}`" in en
    assert "Issue #77" in en
    assert "- A useful change" in en
    assert zh.startswith("# v0.4.0 发布（最新）\n")
    assert f"注解 tag `{tag_object}`" in zh
    # Navigation: the new version is first in BOTH languages.
    config = json.loads(
        (work / "docs" / "docs.json").read_text(encoding="utf-8"),
    )
    languages = config["navigation"]["languages"]
    assert languages[0]["groups"][1]["pages"][0] == "release-v0.4.0"
    assert languages[1]["groups"][1]["pages"][0] == "zh/release-v0.4.0"
    # The (latest) marker moved off the previous latest page.
    old_en = (work / "docs" / "release-v0.1.2.mdx").read_text(encoding="utf-8")
    old_zh = (work / "docs" / "zh" / "release-v0.1.2.mdx").read_text(encoding="utf-8")
    assert "(latest)" not in old_en
    assert "（最新）" not in old_zh
    assert old_en.startswith("# v0.1.2 release\n")
    # The change was committed to the base branch and pushed.
    remote_head = git_out(
        work, "ls-remote", "origin", "refs/heads/main",
    ).split()[0]
    assert remote_head == git_out(work, "rev-parse", "HEAD")
    assert remote_head != head
    message = git_out(work, "log", "-1", "--format=%s")
    assert "v0.4.0" in message and "#77" in message
    committed = git_out(work, "show", "--name-only", "--format=", "HEAD")
    assert committed.split() == [
        "docs/docs.json", "docs/release-v0.1.2.mdx",
        "docs/release-v0.4.0.mdx", "docs/zh/release-v0.1.2.mdx",
        "docs/zh/release-v0.4.0.mdx",
    ]


def test_sync_release_docs_is_idempotent_on_rerun(tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    first = runner.sync_release_docs(
        source_repo="o/r", repo_dir=work, worktree=work,
        base_branch="main", tag="v0.4.0", release_commit=head,
        issue_number=77,
    )
    after_first_head = git_out(work, "rev-parse", "HEAD")
    en_before = (work / "docs" / "release-v0.4.0.mdx").read_text(encoding="utf-8")
    second = runner.sync_release_docs(
        source_repo="o/r", repo_dir=work, worktree=work,
        base_branch="main", tag="v0.4.0", release_commit=head,
        issue_number=77,
    )
    assert "already in sync" in second
    assert first != second
    # No second commit, no overwrite: the remote head and the page
    # content are exactly what the first run produced.
    assert git_out(work, "rev-parse", "HEAD") == after_first_head
    assert (work / "docs" / "release-v0.4.0.mdx").read_text(encoding="utf-8") == en_before


def test_sync_release_docs_resumes_after_a_partial_step(tmp_path, monkeypatch):
    """Resume after a partial step: the pages are written and the marker
    moved, but the navigation was never updated (a crash mid-step). The
    re-run must finish the job instead of deadlocking on the moved
    marker."""
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    original_real = runner.run_command
    release_json = json.dumps({
        "tagName": "v0.4.0",
        "publishedAt": "2026-09-08T12:00:00Z",
        "url": "https://github.com/o/r/releases/tag/v0.4.0",
        "body": RELEASE_DOCS_BODY_V040,
    })

    def gh_view(command, **kwargs):
        if command[:3] == ["gh", "release", "view"]:
            return release_json
        return original_real(command, **kwargs)

    def crash_before_commit(command, **kwargs):
        if command[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="simulated crash",
            )
        return gh_view(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", crash_before_commit)
    with pytest.raises(subprocess.CalledProcessError):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )
    # The pages exist, the marker moved, but nothing was committed.
    assert (work / "docs" / "release-v0.4.0.mdx").is_file()
    assert "(latest)" not in (
        work / "docs" / "release-v0.1.2.mdx"
    ).read_text(encoding="utf-8")
    assert git_out(work, "rev-parse", "origin/main") == \
        git_out(work, "rev-parse", "HEAD")
    # The re-run finishes: marker move is a lenient no-op, the nav is
    # updated, one commit lands on main.
    monkeypatch.setattr(runner, "run_command", gh_view)
    evidence = runner.sync_release_docs(
        source_repo="o/r", repo_dir=work, worktree=work,
        base_branch="main", tag="v0.4.0", release_commit=head,
        issue_number=77,
    )
    assert "committed and pushed" in evidence
    config = json.loads(
        (work / "docs" / "docs.json").read_text(encoding="utf-8"),
    )
    assert config["navigation"]["languages"][0]["groups"][1]["pages"][0] \
        == "release-v0.4.0"


def test_sync_release_docs_fails_fast_when_the_existing_page_differs(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    (work / "docs" / "release-v0.4.0.mdx").write_text(
        "# v0.4.0 release (latest)\n\nhuman-edited content\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(work), "add",
                    "docs/release-v0.4.0.mdx"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "manual page"],
                   check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    with pytest.raises(RuntimeError, match="already exists with different"):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )
    # Nothing was committed over the human edit.
    assert git_out(work, "log", "-1", "--format=%s") == "manual page"


def test_sync_release_docs_fails_fast_when_the_previous_latest_lacks_the_marker(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    # Break the invariant: the previous latest page lost its marker.
    en = work / "docs" / "release-v0.1.2.mdx"
    en.write_text(en.read_text(encoding="utf-8").replace(" (latest)", ""),
                  encoding="utf-8")
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    with pytest.raises(RuntimeError, match=r"does not carry the \(latest\)"):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )


def test_sync_release_docs_fails_fast_on_an_empty_release_body(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body="   ")
    with pytest.raises(RuntimeError, match="body is empty"):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )
    assert not (work / "docs" / "release-v0.4.0.mdx").exists()


def test_sync_release_docs_propagates_a_missing_release(tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body="x", raise_not_found=True)
    with pytest.raises(subprocess.CalledProcessError):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )


def test_sync_release_docs_fails_fast_when_the_base_advanced(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    old_head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", old_head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    runner.sync_release_docs(
        source_repo="o/r", repo_dir=work, worktree=work,
        base_branch="main", tag="v0.4.0", release_commit=old_head,
        issue_number=77,
    )
    # A second release worktree frozen at the OLD commit: the push to
    # main must be rejected (non-fast-forward) — never force-pushed.
    stale = tmp_path / "stale"
    subprocess.run(["git", "-C", str(work), "worktree", "add",
                    "-b", "stale-branch", str(stale), old_head],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(stale), "tag", "-a", "v0.4.1",
                    "-m", "rel", old_head], check=True, capture_output=True)

    real_run = runner.run_command

    def view_v041(command, **kwargs):
        if command[:3] == ["gh", "release", "view"]:
            return json.dumps({
                "tagName": "v0.4.1",
                "publishedAt": "2026-09-09T12:00:00Z",
                "url": "https://github.com/o/r/releases/tag/v0.4.1",
                "body": "# v0.4.1\n\n- Another change",
            })
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", view_v041)
    with pytest.raises(subprocess.CalledProcessError):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=stale,
            base_branch="main", tag="v0.4.1", release_commit=old_head,
            issue_number=78,
        )
    # Remote main still points at the v0.4.0 docs commit.
    remote_head = git_out(
        work, "ls-remote", "origin", "refs/heads/main",
    ).split()[0]
    assert remote_head == git_out(work, "rev-parse", "HEAD")


def test_sync_release_docs_fails_fast_when_the_tag_is_missing_locally(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040,
                         tag="v0.9.9")
    with pytest.raises(subprocess.CalledProcessError):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.9.9", release_commit=head,
            issue_number=77,
        )


def test_release_docs_page_rejects_an_unknown_language():
    with pytest.raises(ValueError, match="not supported"):
        runner.release_docs_page(
            version="v0.4.0", tag_object="t" * 40,
            release_commit="c" * 40, published_at="2026-09-08T12:00:00Z",
            release_url="https://github.com/o/r/releases/tag/v0.4.0",
            issue_number=77, body=RELEASE_DOCS_BODY_V040, language="fr",
        )


def test_release_docs_page_drops_html_comment_lines_but_keeps_the_run_id():
    """The Mintlify MDX parser rejects `<!-- ... -->` comment lines
    (mint validate: 'Unexpected character `!`'), so the run-marker
    comments of the release body must be dropped — but the visible
    `run_id=` line stays (the correlation is kept)."""
    body = (
        "# v0.4.0\n\n"
        "- A useful change\n\n"
        "<!-- orbi:run=a1b2c3d4 -->\n"
        "run_id=a1b2c3d4"
    )
    page = runner.release_docs_page(
        version="v0.4.0", tag_object="t" * 40, release_commit="c" * 40,
        published_at="2026-09-08T12:00:00Z",
        release_url="https://github.com/o/r/releases/tag/v0.4.0",
        issue_number=77, body=body, language="en",
    )
    assert "<!--" not in page
    assert "run_id=a1b2c3d4" in page
    assert "- A useful change" in page


def test_release_docs_page_handles_a_body_without_any_lines():
    page = runner.release_docs_page(
        version="v0.4.0", tag_object="t" * 40, release_commit="c" * 40,
        published_at="2026-09-08T12:00:00Z",
        release_url="https://github.com/o/r/releases/tag/v0.4.0",
        issue_number=77, body="", language="en",
    )
    assert page.startswith("# v0.4.0 release (latest)\n")
    assert page.count("# v0.4.0") == 1


def test_current_latest_release_slug_returns_the_first_en_release_page():
    config_text = release_docs_fixture_config()
    assert runner.current_latest_release_slug(config_text) == \
        "release-v0.1.2"


def test_current_latest_release_slug_fails_fast_on_an_empty_release_group():
    config_text = release_docs_fixture_config()
    config = json.loads(config_text)
    config["navigation"]["languages"][0]["groups"][1]["pages"] = []
    with pytest.raises(RuntimeError, match="has no pages"):
        runner.current_latest_release_slug(
            json.dumps(config),
        )


def test_current_latest_release_slug_fails_fast_without_an_en_release_group():
    config_text = release_docs_fixture_config()
    config = json.loads(config_text)
    config["navigation"]["languages"] = [
        config["navigation"]["languages"][1],
    ]
    with pytest.raises(RuntimeError, match="no English Releases group"):
        runner.current_latest_release_slug(
            json.dumps(config, ensure_ascii=False),
        )


def test_current_latest_release_slug_fails_fast_when_en_lacks_a_release_group():
    """An en language that exists but carries no Releases group: the
    group loop runs and exhausts, then the lookup fails fast."""
    config_text = release_docs_fixture_config()
    config = json.loads(config_text)
    config["navigation"]["languages"][0]["groups"] = [
        {"group": "Getting Started", "pages": ["index"]},
    ]
    with pytest.raises(RuntimeError, match="no English Releases group"):
        runner.current_latest_release_slug(json.dumps(config))


def test_update_release_navigation_fails_fast_when_only_one_group_exists():
    config_text = release_docs_fixture_config()
    config = json.loads(config_text)
    config["navigation"]["languages"] = [
        config["navigation"]["languages"][0],
    ]
    with pytest.raises(RuntimeError, match="expected exactly two release"):
        runner.update_release_navigation(
            json.dumps(config), "release-v0.4.0",
        )


def test_move_latest_marker_fails_fast_on_resume_when_the_new_page_is_missing(
        tmp_path):
    """resume=True but the new page does not exist: the marker was lost,
    not moved — fail fast."""
    work = tmp_path / "work"
    (work / "docs" / "zh").mkdir(parents=True)
    (work / "docs" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 release\n\nrest\n", encoding="utf-8",
    )
    (work / "docs" / "zh" / "release-v0.1.2.mdx").write_text(
        "# v0.1.2 发布\n\n余下内容\n", encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"does not carry the \(latest\)"):
        runner.move_latest_marker(
            work, "release-v0.1.2", "release-v0.4.0", resume=True,
        )


def test_sync_release_docs_fails_fast_when_the_body_is_not_a_string(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    real = runner.run_command

    def bad_body(command, **kwargs):
        if command[:3] == ["gh", "release", "view"]:
            return json.dumps({
                "tagName": "v0.4.0",
                "publishedAt": "2026-09-08T12:00:00Z",
                "url": "https://github.com/o/r/releases/tag/v0.4.0",
                "body": 123,
            })
        return real(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", bad_body)
    with pytest.raises(RuntimeError, match="body is empty"):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )
    # The fall-through path answers real git commands (sync_release_docs
    # never reaches it in this test — the body check fails first).
    assert bad_body(["git", "rev-parse", "HEAD"], cwd=work)


def test_sync_release_docs_fails_fast_when_docs_json_is_missing(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    (work / "docs" / "docs.json").unlink()
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    with pytest.raises(RuntimeError, match="docs.json.*is missing"):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )


def test_sync_release_docs_fails_fast_on_a_real_git_diff_error(
        tmp_path, monkeypatch):
    work = make_release_docs_repo(tmp_path)
    head = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "tag", "-a", "v0.4.0",
                    "-m", "rel", head], check=True, capture_output=True)
    fake_gh_release_view(monkeypatch, body=RELEASE_DOCS_BODY_V040)
    real = runner.run_command

    def diff_failing(command, **kwargs):
        if command[:3] == ["git", "diff", "--cached"]:
            raise subprocess.CalledProcessError(
                2, command, stderr="fatal: not a git repository",
            )
        return real(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", diff_failing)
    with pytest.raises(subprocess.CalledProcessError):
        runner.sync_release_docs(
            source_repo="o/r", repo_dir=work, worktree=work,
            base_branch="main", tag="v0.4.0", release_commit=head,
            issue_number=77,
        )


def test_tag_commit_is_ancestor_of_base_true_when_ancestor(tmp_path):
    """Issue #275: the docs-sync step advances the base past the tag
    commit; the tag commit is an ancestor of the base (and not vice
    versa)."""
    work = make_release_docs_repo(tmp_path)
    tag_commit = git_out(work, "rev-parse", "HEAD")
    (work / "docs" / "extra.mdx").write_text("extra", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "docs/extra.mdx"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "docs"],
                   check=True, capture_output=True)
    docs_head = git_out(work, "rev-parse", "HEAD")
    assert runner.tag_commit_is_ancestor_of_base(
        tag_commit, docs_head, work) is True
    assert runner.tag_commit_is_ancestor_of_base(
        docs_head, tag_commit, work) is False


def test_tag_commit_is_ancestor_of_base_false_on_unrelated_commits(
        tmp_path):
    """Issue #275: a tag commit on an unrelated side branch is NOT an
    ancestor of the base — the genuine tag mismatch that must fail
    fast."""
    work = make_release_docs_repo(tmp_path)
    base = git_out(work, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "side"],
                   check=True, capture_output=True)
    (work / "side.txt").write_text("side", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "side.txt"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "side"],
                   check=True, capture_output=True)
    side_head = git_out(work, "rev-parse", "HEAD")
    assert runner.tag_commit_is_ancestor_of_base(
        side_head, base, work) is False


def test_process_release_resumes_after_docs_sync_advanced_the_base(
        monkeypatch):
    """Issue #275: the docs-sync step pushes the release notes to the base
    branch, advancing origin/<base> past the tag commit. On a resume the
    frozen base is the docs commit; the release must recover the tag commit
    as the canonical release commit instead of deadlocking on the tag
    check."""
    state = make_release_process_env(monkeypatch, tag_commit="abc123")
    # The frozen base is the docs commit (a descendant of the tag commit);
    # the env's fake_run_command answers "ancestor" for the merge-base
    # check.
    monkeypatch.setattr(runner, "freeze_base", lambda r, b: "docs456")
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    release_url = runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    assert release_url == "https://github.com/o/r/releases/tag/v0.3.0"
    # The tag commit was recovered as the canonical release commit.
    assert state["sync_docs_calls"][0]["release_commit"] == "abc123"
    # The release succeeded (ai-merged), not blocked.
    assert any(k.get("add") == "ai-merged" for _, k in state["edits"])


def test_close_release_milestone_rejects_a_non_array_milestone_list(monkeypatch):
    def fake_run(command, **kwargs):
        if command == MILESTONE_LIST_COMMAND:
            return json.dumps({"number": 5, "title": "v0.3.0"})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(ValueError, match="must be a JSON array"):
        runner.close_release_milestone("o/r", "v0.3.0")
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_close_release_milestone_reraises_a_real_gh_failure(monkeypatch):
    def fake_run(command, **kwargs):
        if command == MILESTONE_LIST_COMMAND:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: rate limit exceeded",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.close_release_milestone("o/r", "v0.3.0")
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["unexpected"])


def test_parse_release_declaration_rejects_non_string_body():
    with pytest.raises(ValueError, match="must be a string"):
        runner.parse_release_declaration(123)


def test_parse_release_declaration_rejects_empty_section():
    # `## Release` is the very last line: the section holds no fields.
    with pytest.raises(ValueError, match="version"):
        runner.parse_release_declaration("Ship it.\n\n## Release\n")


def test_parse_release_declaration_rejects_colonless_field_line():
    # A `- ` line without a colon BEFORE the scope list is a malformed
    # field line (after the scope list it is a malformed scope item).
    body = RELEASE_DECLARATION_BODY.replace(
        "## Release\n", "## Release\n\n- broken\n",
    )
    with pytest.raises(ValueError, match=r"field 'broken' is malformed"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_inline_scope_value():
    body = RELEASE_DECLARATION_BODY.replace("- scope:\n", "- scope: 123\n")
    with pytest.raises(ValueError, match="not an inline value"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_plain_line_in_scope():
    body = RELEASE_DECLARATION_BODY.replace("  - #123", "hello")
    with pytest.raises(ValueError, match=r"scope item 'hello' is malformed"):
        runner.parse_release_declaration(body)


def test_parse_release_declaration_rejects_plain_line_outside_scope():
    # A plain line while the scope list is NOT open is rejected as a
    # non-field line (the same line inside the scope list is a
    # malformed scope item).
    body = RELEASE_DECLARATION_BODY.replace(
        "## Release\n", "## Release\n\nhello\n",
    )
    with pytest.raises(ValueError, match=r"line 'hello' is not a"):
        runner.parse_release_declaration(body)


def test_verify_release_scope_reraises_real_issue_gh_failure(monkeypatch):
    def fail(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"]:
            raise subprocess.CalledProcessError(
                1, command,
                stderr="GraphQL: Could not resolve to a PullRequest with "
                       "the number of 7.",
            )
        if command[:3] == ["gh", "issue", "view"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 403: rate limited",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.verify_release_scope("o/r", [7], Path("/repo"), "release123")
    assert "HTTP 403: rate limited" in str(excinfo.value.stderr)
    # The fake rejects anything that is not pr/issue view traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fail(["gh", "release", "list"])


def test_process_release_keeps_the_fresh_id_when_no_run_id_is_recoverable(
        monkeypatch):
    # ai-in-progress is present but no run id can be recovered: the
    # fresh id is kept (the resume branch is skipped).
    state = make_release_process_env(monkeypatch, in_progress=True,
                                     existing_run_id=None)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"},
                        {"name": "ai-in-progress"}]}
    runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    assert state["run_ids"] == ["a1b2c3d4"]
    (comment_number, comment_kwargs), = state["comments"]
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment_kwargs["body"]


def test_process_release_publishes_the_release_role_progress_body(
        monkeypatch):
    # `_safe_publish` really invokes the action here, so the progress
    # closure renders the body: role=release, run marker, branch.
    state = make_release_process_env(monkeypatch)
    publishes = []

    def publish(**kwargs):
        publishes.append(kwargs)
        kwargs["action"]()

    monkeypatch.setattr(runner, "_safe_publish", publish)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = runner.process_release(
        issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    assert publishes, "no progress publish happened"
    assert all(kwargs["role"] == "release" for kwargs in publishes)
    assert all(kwargs["issue"] == 99 for kwargs in publishes)
    publisher = runner.ProgressPublisher.return_value
    assert publisher.ensure.call_count == 1
    body = publisher.ensure.call_args[0][0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in body
    assert "- role: release" in body
    assert "- priority: normal" in body
    assert "- branch: orbi/o-r-issue-99-a1b2c3d4" in body


def test_process_release_fails_on_scope_item_that_is_neither(monkeypatch):
    # Scope item #125 is neither a PR nor an Issue on the remote: the
    # item-by-item verification fails fast and the release is blocked.
    body = RELEASE_DECLARATION_BODY.replace("  - #124", "  - #125")
    state = make_release_process_env(monkeypatch, body=body)
    issue = {"number": 99, "title": "Release v0.3.0", "body": body,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    with pytest.raises(RuntimeError, match="neither a PR nor an Issue"):
        runner.process_release(
            issue, {"repo_dir": Path("/r"), "base_branch": "main"}, "o/r",
        )
    assert state["edits"][-1] == (99, {"repo": "o/r", "add": "ai-blocked",
                                        "remove": "ai-in-progress"})


def test_scope_gh_fake_rejects_unexpected_command(monkeypatch):
    make_scope_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "release", "list"])


def test_gate_gh_fake_rejects_unexpected_command(monkeypatch):
    make_gate_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "release", "list"])


def test_release_gh_fake_rejects_unexpected_command(monkeypatch):
    make_release_gh(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["git", "tag", "v0.3.0"])


def test_release_process_env_fake_rejects_unexpected_command(monkeypatch):
    make_release_process_env(monkeypatch)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "label", "list"])


def test_release_process_env_fake_answers_the_real_tag_fetch(tmp_path,
                                                             monkeypatch):
    # The env fake answers the `git fetch` of the REAL
    # `release_tag_commit` (the lock is taken for real on tmp_path).
    real_release_tag_commit = runner.release_tag_commit
    make_release_process_env(monkeypatch)
    monkeypatch.setattr(runner, "release_tag_commit",
                        real_release_tag_commit)
    env_fake = runner.run_command

    def fake(command, **kwargs):
        if command[:2] == ["git", "rev-parse"]:
            return "abc123"
        return env_fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake)
    assert runner.release_tag_commit(tmp_path, "v0.3.0") == "abc123"


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
    if command[:3] == ["git", "rev-parse", f"origin/{DELIVER_BRANCH}"]:
        return FAKE_HEAD_SHA
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
    raise AssertionError(f"unexpected command: {command}")


def test_fake_deliver_run_rejects_unexpected_command():
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_deliver_run(["gh", "release", "list"])


def test_deliver_pr_rejects_wrong_branch(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command[:3] == ["git", "branch", "--show-current"], command
        return "other-branch"

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="Pi changed branch"):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", FAKE_HEAD_SHA, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_deliver_pr_rejects_uncommitted_changes(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M leaked.py\n?? junk.txt"
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_deliver_pr_rejects_delivery_without_a_commit(monkeypatch, tmp_path):
    # HEAD is still the frozen base: the agent delivered no commit.
    base_sha = "9" * 40

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return base_sha
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="no commit"):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", base_sha, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_deliver_pr_completes_the_closeout(monkeypatch, tmp_path):
    """The happy path: clean commit boundary, base not advanced, plain
    push, the PR of the branch is verified, the URL is returned."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.deliver_pr(
        tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
        issue=4, issue_title="t", repo_dir=tmp_path,
    ) == FAKE_PR_URL
    # The closeout order: commit boundary, locked base fetch, ancestry,
    # plain push, remote-head verification, PR list.
    assert ["git", "status", "--porcelain"] in calls
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"] in calls
    assert ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"] in calls
    assert ["git", "rev-parse", f"origin/{DELIVER_BRANCH}"] in calls
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        ) == FAKE_PR_URL
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

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        ) == FAKE_PR_URL
    assert calls.index(["git", "merge", "origin/main"]) < calls.index(
        ["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"],
    )
    assert "base_absorbed" in caplog.text


def test_deliver_pr_creates_the_pr_when_absent(monkeypatch, tmp_path):
    """No open PR of the branch: the Runner creates it with the run
    marker and `Fixes #<issue>` in the body (the PR body contract is
    the Runner's obligation now, Issue #186)."""
    calls = []

    created = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            # Stateful: empty until the Runner creates the PR, then the
            # created PR of the task branch (like the real GitHub state).
            if not created:
                return "[]"
            return json.dumps([created[0]])
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.deliver_pr(
        tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
        issue=4, issue_title="Closeout title", repo_dir=tmp_path,
    ) == FAKE_PR_URL
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


def test_deliver_pr_fails_fast_when_pr_create_fails(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and command[2] == "list":
            return "[]"
        if command[:2] == ["gh", "pr"] and command[2] == "create":
            raise subprocess.CalledProcessError(
                1, command, stderr="no matching pull request",
            )
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_deliver_pr_rejects_remote_head_mismatch_after_push(
    monkeypatch, tmp_path,
):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", f"origin/{DELIVER_BRANCH}"]:
            return "f" * 40
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="remote head"):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_deliver_pr_verifies_the_pr_with_the_latest_base_check_skipped(
    monkeypatch, tmp_path,
):
    """deliver_pr just fetched and merged the base itself, so the
    verify step must not fetch it again (require_latest_base=False):
    no second `git fetch` after the push, and a delivery that is behind
    the base (the conflict-rollback scene) still passes verification."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # origin/main is NOT an ancestor of HEAD: base advanced.
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["git", "merge", "origin/main"]:
            raise subprocess.CalledProcessError(1, command)
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner.deliver_pr(
        tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
        issue=4, issue_title="t", repo_dir=tmp_path,
    )
    fetches = [
        index for index, command in enumerate(calls)
        if command[:3] == ["git", "fetch", "origin"]
    ]
    push = calls.index(["git", "push", "origin", f"HEAD:{DELIVER_BRANCH}"])
    # Exactly one fetch (the deliver fetch); nothing after the push.
    assert len(fetches) == 1
    assert fetches[0] < push


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
    exclude = runner.runner_runtime_exclude_path(wt)
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
    runner.apply_runner_runtime_excludes(wt)
    lines = runner.runner_runtime_exclude_path(wt).read_text(
        encoding="utf-8",
    ).splitlines()
    for pattern in runner.RUNNER_RUNTIME_EXCLUDES:
        assert pattern in lines


def test_apply_runner_runtime_excludes_is_idempotent(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    runner.apply_runner_runtime_excludes(wt)
    first = runner.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    runner.apply_runner_runtime_excludes(wt)
    second = runner.runner_runtime_exclude_path(wt).read_text(encoding="utf-8")
    assert first == second
    lines = second.splitlines()
    assert len(lines) == len(set(lines)), "patterns must not be duplicated"


def test_apply_runner_runtime_excludes_preserves_user_content(tmp_path):
    _, wt = _make_linked_worktree(tmp_path)
    exclude = runner.runner_runtime_exclude_path(wt)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# my custom exclude\nsecrets-local/\n", encoding="utf-8")
    runner.apply_runner_runtime_excludes(wt)
    text = exclude.read_text(encoding="utf-8")
    assert "# my custom exclude" in text
    assert "secrets-local/" in text
    assert ".orbi/" in text
    assert ".pi-session/" in text


def test_apply_runner_runtime_excludes_without_git_is_a_noop(tmp_path):
    wt = tmp_path / "not-a-worktree"
    wt.mkdir()
    runner.apply_runner_runtime_excludes(wt)  # must not raise
    assert not (wt / ".git").exists()
    assert not list(wt.iterdir())


def test_run_pi_applies_runner_runtime_excludes_before_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    applied: list[Path] = []
    monkeypatch.setattr(
        runner, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "done")
    config = {
        "prompt": prompt_path, "repo_dir": tmp_path,
        "source_repos": ["owner/repo"], "workspace_root": tmp_path,
        "context_files": [], "skills": [], "base_branch": "main",
        "base_sha": "abc123def456", "run_id": "run1",
    }
    runner.run_pi(
        {"number": 5, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo",
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
        runner, "apply_runner_runtime_excludes",
        lambda worktree: applied.append(worktree),
    )
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kwargs: "ok")
    runner.run_review(
        tmp_path,
        {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"},
        {
            "prompt_review": prompt_path, "repo_dir": tmp_path / "checkout",
            "source_repos": ["owner/repo"], "base_branch": "main",
            "run_id": "a1b2c3d4", "skills": [],
        },
        "owner/repo", 4, "branch", 1,
    )
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

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            # First check: the Runner's own state looks untracked. After
            # the exclude repair the re-check is clean.
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )
    assert url == FAKE_PR_URL
    assert "runner_runtime_exclude_repaired" in caplog.text
    assert "delivery_uncommitted_changes" not in caplog.text
    assert status_calls["n"] == 2


def test_deliver_pr_repairs_the_renamed_state_dir_too(monkeypatch, tmp_path,
                                                      caplog):
    """Migration window: the renamed state dir `.orbi/` is Runner-owned
    as well and must be repaired, not failed."""
    status_calls = {"n": 0}

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            return "?? .orbi/\n" if status_calls["n"] == 1 else ""
        return fake_deliver_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        url = runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        runner.deliver_pr(
            tmp_path, DELIVER_BRANCH, "main", "9" * 40, FAKE_RUN_ID,
            issue=4, issue_title="t", repo_dir=tmp_path,
        )


def test_cleanup_task_worktree_removes_the_scene_and_prunes(tmp_path):
    repo, wt = _make_linked_worktree(tmp_path)
    (wt / ".orbi").mkdir(parents=True)
    (wt / ".pi-session").mkdir(parents=True)
    runner.cleanup_task_worktree(wt, repo, run_id="abc12345", issue=4)
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
        runner.cleanup_task_worktree(wt, tmp_path, run_id="abc12345", issue=4)
    assert "worktree_cleanup_failed" in caplog.text
    assert wt.exists(), "a failed cleanup must never claim the scene is gone"


def test_exclude_path_rejects_a_pointer_without_gitdir(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").parent.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("# not a real pointer\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no gitdir entry"):
        runner.runner_runtime_exclude_path(wt)


def test_runner_runtime_only_accepts_quoted_runner_paths():
    # Porcelain quotes paths with special characters; the runner paths
    # are plain ASCII, so the unquoted match is exact.
    assert runner._is_runner_runtime_only('?? ".orbi/"\n')
    assert runner._is_runner_runtime_only('?? ".pi-session/"\n')
    # Issue #302: the orbi contract artifact set (pi-loop state, per-run
    # plan/test/verify artifacts) is runner-owned too — a porcelain entry
    # carrying only them must keep the repair path open, never the fail
    # fast.
    assert runner._is_runner_runtime_only('?? ".pi/"\n')
    assert runner._is_runner_runtime_only("?? plan.md\n")
    assert runner._is_runner_runtime_only("?? test.log\n")
    assert runner._is_runner_runtime_only("?? verify.md\n")
    # An agent-owned leftover is NEVER runner-owned, mixed or alone.
    assert runner._is_runner_runtime_only("?? notes.md\n") is False
    assert runner._is_runner_runtime_only("?? unexpected.py\n") is False
    assert runner._is_runner_runtime_only("?? plan.md\n?? notes.md\n") is False


def test_exclude_path_for_a_plain_checkout_uses_its_own_git_dir(tmp_path):
    # A worktree whose `.git` is a DIRECTORY (a plain checkout, not a
    # linked worktree): the exclude is its own `.git/info/exclude`.
    wt = tmp_path / "checkout"
    (wt / ".git").mkdir(parents=True)
    exclude = runner.runner_runtime_exclude_path(wt)
    assert exclude == wt / ".git" / "info" / "exclude"


def test_runner_runtime_only_rejects_an_empty_status():
    # An empty status is not "runner-only" — the repair must not fire on
    # a clean worktree.
    assert runner._is_runner_runtime_only("") is False
    assert runner._is_runner_runtime_only("\n") is False


def test_cleanup_task_worktree_prunes_without_a_scene(tmp_path):
    # The worktree directory is already gone (a partial failure): the
    # cleanup still prunes the git metadata and logs success.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    missing = tmp_path / "already-gone"
    runner.cleanup_task_worktree(missing, repo, run_id="abc12345", issue=4)
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
    runner.apply_runner_runtime_excludes(wt)
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines == list(runner.RUNNER_RUNTIME_EXCLUDES)


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
    runner.apply_runner_runtime_excludes(wt)
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
    runner.apply_runner_runtime_excludes(wt)
    tracked.write_text("x = 2\n", encoding="utf-8")
    (wt / "plan.md").write_text("artifact\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt,
        capture_output=True, text=True, check=True,
    ).stdout == " M src.py\n"
