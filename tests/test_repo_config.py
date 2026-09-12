"""Repository-level config-as-code tests (Issue #527).

Covers the strict schema (host-only / unknown / type / path rejections), the
per-key merge over the host fallback (D3), the `gh api` read pipeline (missing
file -> no-op, malformed file -> fail fast, read error -> fail open), the D4
change-visibility audit, and the runner wiring at the claim scan and claim.
"""
import base64
import dataclasses
import json
import logging
import subprocess
from pathlib import Path

import pytest

import orbi.repo_config as repo_config
import orbi.runner as runner
from seam import seam


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _record(policy_text: str, *, sha: str = "a" * 40, path: str = ".github/orbi.toml"):
    raw = json.dumps({
        "name": Path(path).name,
        "path": path,
        "sha": sha,
        "size": len(policy_text.encode("utf-8")),
        "content": _b64(policy_text),
        "encoding": "base64",
    })
    return lambda command, **kwargs: raw


# --- strict schema ----------------------------------------------------------

def test_parse_repo_config_accepts_every_whitelisted_key():
    text = (
        'base_branch = "beta"\n'
        'active_milestone = "v0.5.0"\n'
        'test_command = "pytest -q"\n'
        'context_files = ["AGENTS.md", "docs/testing.mdx"]\n'
        'dispatch_label = "ai-ready"\n'
    )
    assert repo_config.parse_repo_config(text) == repo_config.RepoPolicy(
        base_branch="beta",
        active_milestone="v0.5.0",
        test_command="pytest -q",
        context_files=("AGENTS.md", "docs/testing.mdx"),
        dispatch_label="ai-ready",
    )


def test_parse_repo_config_empty_document_is_a_no_op():
    """A file with no policy keys is valid and changes nothing (D3: no
    whole-file override)."""
    assert repo_config.parse_repo_config("# nothing here\n") == (
        repo_config.RepoPolicy()
    )


def test_parse_repo_config_rejects_bad_toml():
    with pytest.raises(repo_config.RepoConfigError, match="invalid TOML"):
        repo_config.parse_repo_config('base_branch = "unterminated')


def test_parse_repo_config_rejects_unknown_key():
    with pytest.raises(
        repo_config.RepoConfigError, match=r"unknown key\(s\): mystery",
    ):
        repo_config.parse_repo_config('mystery = "x"\n')


@pytest.mark.parametrize(
    "key",
    ["source_repos", "pi_provider", "repo_dir", "prompt", "skills",
     "engine_source_track", "issue_comments_limit"],
)
def test_parse_repo_config_rejects_host_only_keys(key):
    with pytest.raises(
        repo_config.RepoConfigError, match=r"host-only key\(s\).*" + key,
    ):
        repo_config.parse_repo_config(f'{key} = "x"\n')


def test_parse_repo_config_lists_every_offending_host_only_key():
    with pytest.raises(repo_config.RepoConfigError) as excinfo:
        repo_config.parse_repo_config(
            'source_repos = ["o/r"]\npi_model = "m"\n',
        )
    message = str(excinfo.value)
    assert "host-only" in message
    assert "source_repos" in message
    assert "pi_model" in message


@pytest.mark.parametrize(
    "text",
    [
        'base_branch = 1\n',
        "base_branch = []\n",
        'active_milestone = ""\n',
        "test_command = true\n",
        'dispatch_label = 7\n',
        "context_files = []\n",
        'context_files = "AGENTS.md"\n',
        'context_files = ["AGENTS.md", 3]\n',
        'context_files = ["/etc/passwd"]\n',
        'context_files = ["../outside.md"]\n',
    ],
)
def test_parse_repo_config_type_errors_fail_fast(text):
    with pytest.raises(repo_config.RepoConfigError):
        repo_config.parse_repo_config(text)


@pytest.mark.parametrize(
    "label", ["ai-in-progress", "ai-pr-opened", "ai-merged", "ai-blocked"],
)
def test_parse_repo_config_rejects_a_lifecycle_dispatch_label(label):
    """Issue #527 D2: the delivery lifecycle labels stay host constants —
    a claim label that IS one of them would make the ready scan
    self-contradictory (`label:ai-merged ... -label:ai-merged`) and
    silently stop claiming."""
    with pytest.raises(repo_config.RepoConfigError, match="lifecycle"):
        repo_config.parse_repo_config(f'dispatch_label = "{label}"\n')


def test_parse_repo_config_accepts_ai_ready_as_the_dispatch_label():
    assert repo_config.parse_repo_config(
        'dispatch_label = "ai-ready"\n',
    ) == repo_config.RepoPolicy(
        dispatch_label="ai-ready",
    )


def test_context_files_rejects_absolute_posix_path():
    with pytest.raises(repo_config.RepoConfigError, match="repository-relative"):
        repo_config.parse_repo_config('context_files = ["/tmp/x"]\n')


# --- D3 per-key resolution --------------------------------------------------

def test_resolve_policy_overrides_only_the_declared_keys():
    host = runner.RunnerConfig(
        base_branch="main",
        active_milestone="v0.1.0",
        context_files=(Path("/host/ctx.md"),),
        test_command="host-cmd",
        dispatch_label="ai-ready",
    )
    effective = repo_config.resolve_policy(
        host, repo_config.RepoPolicy(
            base_branch="beta", dispatch_label="custom-ready",
        ),
    )
    assert effective.base_branch == "beta"
    assert effective.dispatch_label == "custom-ready"
    # Omitted keys keep the host fallback.
    assert effective.active_milestone == "v0.1.0"
    assert effective.context_files == (Path("/host/ctx.md"),)
    assert effective.test_command == "host-cmd"
    # Repository context files are additive under their own key.
    assert effective.repo_context_files == ()


def test_resolve_policy_context_files_are_additive():
    effective = repo_config.resolve_policy(
        {"context_files": [Path("/host.md")]},
        {"context_files": ["AGENTS.md"]},
    )
    assert effective["context_files"] == [Path("/host.md")]
    assert effective["repo_context_files"] == ["AGENTS.md"]


# --- D4 audit ---------------------------------------------------------------

def test_policy_diff_reports_changed_keys_only():
    assert repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="main"),
    ) is None
    diff = repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="beta", test_command="pytest"),
    )
    assert diff == "base_branch=main->beta test_command=(none)->pytest"


def test_repo_config_audit_marks_a_changed_sha_with_the_diff():
    fields = repo_config.repo_config_audit(
        "newsha", repo_config.RepoPolicy(base_branch="beta"),
        previous_sha="oldsha",
        previous_policy=repo_config.RepoPolicy(base_branch="main"),
    )
    assert fields["repo_config_changed"] == "oldsha..newsha"
    assert fields["repo_config_diff"] == "base_branch=main->beta"


def test_repo_config_audit_is_empty_on_first_run_and_unchanged_sha():
    assert repo_config.repo_config_audit(
        "sha", repo_config.RepoPolicy(),
        previous_sha=None, previous_policy=None,
    ) == {}
    assert repo_config.repo_config_audit(
        "sha", repo_config.RepoPolicy(),
        previous_sha="sha", previous_policy=repo_config.RepoPolicy(),
    ) == {}


def test_repo_config_audit_changed_without_previous_content():
    fields = repo_config.repo_config_audit(
        "new", repo_config.RepoPolicy(),
        previous_sha="old", previous_policy=None,
    )
    assert fields["repo_config_changed"] == "old..new"
    assert "repo_config_diff" not in fields


# --- read pipeline ----------------------------------------------------------

def test_read_repo_config_returns_sha_and_policy():
    policy = repo_config.read_repo_config(
        "owner/repo",
        run_command=_record('base_branch = "beta"\n', sha="cafe" * 10),
    )
    assert policy == repo_config.RepoPolicy(
        base_branch="beta", sha="cafe" * 10,
    )


def test_read_repo_config_missing_file_is_none():
    def not_found(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command,
            output='{"message":"Not Found","status":"404"}',
            stderr="gh: Not Found (HTTP 404)",
        )

    assert repo_config.read_repo_config(
        "owner/repo", run_command=not_found,
    ) is None


def test_read_repo_config_read_failure_fails_open(caplog):
    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    assert repo_config.read_repo_config(
        "owner/repo", run_command=broken,
    ) is None
    assert "repo_config_read_failed" in caplog.text


def test_read_repo_config_missing_file_logs_no_journal_error(
    caplog, monkeypatch,
):
    """Issue #730: the designed 404 no-op is a `None` return, not a failure —
    the real `run_command` (as wired by `load_repo_policy`) must not emit a
    journal-visible (>= INFO) `command_failed` line for it."""
    def not_found(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command,
            output='{"message":"Not Found","status":"404"}',
            stderr="gh: Not Found (HTTP 404)",
        )

    monkeypatch.setattr(runner.subprocess, "run", not_found)
    with caplog.at_level(logging.DEBUG, logger="orbi.bootstrap"):
        assert repo_config.read_repo_config(
            "owner/repo", run_command=runner.run_command,
        ) is None
    journal_visible = [
        record for record in caplog.records
        if record.levelno >= logging.INFO
        and "command_failed" in record.getMessage()
    ]
    assert journal_visible == []


def test_read_repo_config_read_failure_logs_error_with_detail(caplog):
    """Issue #730: a real read failure (network/permission/rate limit) keeps
    an ERROR line carrying the command output, not just the bare exit code."""
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command,
            stderr="gh: API rate limit exceeded for installation 1234",
        )

    with caplog.at_level(logging.DEBUG, logger="orbi.bootstrap"):
        assert repo_config.read_repo_config(
            "owner/repo", run_command=denied,
        ) is None
    error_lines = [
        record.getMessage() for record in caplog.records
        if record.levelno >= logging.ERROR
        and "repo_config_read_failed" in record.getMessage()
    ]
    assert any("rate limit exceeded" in line for line in error_lines)


def test_read_repo_config_non_file_response_is_none():
    assert repo_config.read_repo_config(
        "owner/repo", run_command=lambda command, **kwargs: json.dumps([]),
    ) is None


def test_read_repo_config_undecodable_content_fails_fast():
    raw = json.dumps({"sha": "x", "content": "!!!not-base64!!!", "size": 4})
    with pytest.raises(repo_config.RepoConfigError, match="undecodable"):
        repo_config.read_repo_config(
            "owner/repo", run_command=lambda command, **kwargs: raw,
        )


def test_read_repo_config_size_cap_fails_fast():
    raw = json.dumps({
        "sha": "x",
        "size": repo_config.MAX_REPO_CONFIG_BYTES + 1,
        "content": _b64("x"),
    })
    with pytest.raises(repo_config.RepoConfigError, match="too large"):
        repo_config.read_repo_config(
            "owner/repo", run_command=lambda command, **kwargs: raw,
        )


def test_read_repo_config_decoded_size_cap_fails_fast():
    big = "x" * (repo_config.MAX_REPO_CONFIG_BYTES + 10)
    raw = json.dumps({"sha": "x", "content": _b64(big)})
    with pytest.raises(repo_config.RepoConfigError, match="too large"):
        repo_config.read_repo_config(
            "owner/repo", run_command=lambda command, **kwargs: raw,
        )


def test_read_repo_config_invalid_policy_fails_fast():
    def bad(command, **kwargs):
        return json.dumps({"sha": "x", "content": _b64("nope = 1\n")})

    with pytest.raises(repo_config.RepoConfigError, match="unknown key"):
        repo_config.read_repo_config("owner/repo", run_command=bad)


def test_read_repo_config_uses_the_configured_custom_path():
    seen = []

    def command(args, **kwargs):
        seen.append(args)
        return json.dumps({"sha": "x", "content": _b64("base_branch = 'b'\n")})

    repo_config.read_repo_config(
        "owner/repo", path=".orbi/policy.toml", run_command=command,
    )
    assert seen == [["gh", "api", "repos/owner/repo/contents/.orbi/policy.toml"]]


def test_read_repo_config_at_reads_a_previous_blob():
    raw = json.dumps({"content": _b64('base_branch = "old"\n')})
    policy = repo_config.read_repo_config_at(
        "owner/repo", "abc123", run_command=lambda command, **kwargs: raw,
    )
    assert policy == repo_config.RepoPolicy(base_branch="old", sha="abc123")


def test_read_repo_config_at_failure_is_none():
    def broken(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    assert repo_config.read_repo_config_at(
        "owner/repo", "abc123", run_command=broken,
    ) is None


def test_read_repo_config_at_non_blob_is_none():
    assert repo_config.read_repo_config_at(
        "owner/repo", "abc", run_command=lambda command, **kwargs: "[]",
    ) is None


# --- context file validation ------------------------------------------------

def test_validate_context_file_returns_the_resolved_path(tmp_path):
    (tmp_path / "AGENTS.md").write_text("ok", encoding="utf-8")
    assert repo_config.validate_context_file(tmp_path, "AGENTS.md") == (
        tmp_path / "AGENTS.md"
    )


def test_validate_context_file_missing_fails_fast(tmp_path):
    with pytest.raises(repo_config.RepoConfigError, match="does not exist"):
        repo_config.validate_context_file(tmp_path, "AGENTS.md")


def test_validate_context_file_too_large_fails_fast(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * (repo_config.MAX_REPO_CONTEXT_BYTES + 1), encoding="utf-8")
    with pytest.raises(repo_config.RepoConfigError, match="too large"):
        repo_config.validate_context_file(tmp_path, "big.md")


# --- runner wiring ----------------------------------------------------------

def test_pick_next_delivery_in_flight_scan_uses_the_repo_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #527: the in-flight restart scan resolves the SAME repository
    policy as the ready scan. A repository whose `.github/orbi.toml`
    replaces `ai-ready` must still recover a killed run (the Issue #18
    acceptance "a dead run is resumed, never skipped") instead of
    stranding it forever in `ai-in-progress`."""
    in_flight = {"number": 2, "title": "in flight", "body": ""}
    searches = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _record('dispatch_label = "repo-ready"\n')(command)
        searches.append(command[command.index("--search") + 1])
        return json.dumps([in_flight] if "in-progress" in searches[-1] else [])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner, "reconcile_open_epics", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "reconcile_release_milestones", lambda *a, **k: None,
    )
    config = runner.RunnerConfig()
    assert runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1, config=config,
    ) == ("owner/repo", in_flight, None)
    # The resumable-PR scan ran first, then the in-flight scan with the
    # repository's own claim label; the ready scan was never reached.
    assert len(searches) == 2
    assert "label:repo-ready label:ai-in-progress" in searches[-1]
    assert "label:ai-ready" not in searches[-1]


def test_pick_next_delivery_in_flight_scan_falls_back_on_a_malformed_file(
    monkeypatch, tmp_path,
):
    """A malformed repository file must not break the scans (the read is
    fail-open; `process_issue` blocks the claim with the reason): the
    in-flight scan keeps the host claim label."""
    searches = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return json.dumps({"sha": "x", "content": _b64("nope = 1\n")})
        searches.append(command[command.index("--search") + 1])
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(runner, "reconcile_open_epics", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "reconcile_release_milestones", lambda *a, **k: None,
    )
    assert runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
        config=runner.RunnerConfig(),
    ) is None
    assert any(
        search.startswith("label:ai-ready label:ai-in-progress")
        for search in searches
    )


def test_repository_config_path_defaults_and_honors_the_entry():
    config = runner.RunnerConfig(repositories=(
        {"github": "owner/pilot", "config_path": ".orbi/policy.toml"},
    ))
    assert runner.repository_config_path(config, "owner/pilot") == ".orbi/policy.toml"
    assert runner.repository_config_path(config, "owner/other") == ".github/orbi.toml"
    assert runner.repository_config_path(
        runner.RunnerConfig(), "owner/other",
    ) == ".github/orbi.toml"


def test_repository_base_branch_falls_back_to_the_entry_then_host():
    config = runner.RunnerConfig(
        base_branch="main",
        repositories=({"github": "owner/pilot", "base_branch": "develop"},),
    )
    assert runner.repository_base_branch(config, "owner/pilot") == "develop"
    assert runner.repository_base_branch(config, "owner/other") == "main"


def test_apply_repo_policy_uses_the_entry_base_branch_as_fallback():
    config = runner.RunnerConfig(
        base_branch="main",
        repositories=({"github": "owner/pilot", "base_branch": "develop"},),
    )
    effective = runner.apply_repo_policy(
        config, "owner/pilot", repo_config.RepoPolicy(sha="s"),
    )
    assert effective.base_branch == "develop"


def test_previous_repo_config_sha_reads_the_newest_trusted_comment(monkeypatch):
    # Ordered oldest -> newest (reversed by the function): an untrusted
    # comment, a non-string body, a trusted comment without the field and
    # finally the trusted match. The match is the newest, so it wins after
    # the earlier candidates are skipped.
    comments = [
        {"authorAssociation": "MEMBER", "body": "- repo_config: " + "c" * 40},
        {"authorAssociation": "MEMBER", "body": "no repo config field"},
        {"authorAssociation": "NONE", "body": "- repo_config: " + "b" * 40},
        {"authorAssociation": "MEMBER", "body": None},
    ]
    monkeypatch.setattr(runner, "_authenticated_github_login", lambda: "orbi")
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments,
    )
    assert runner.previous_repo_config_sha(1, "owner/repo") == "c" * 40


def test_previous_repo_config_sha_is_best_effort(monkeypatch, caplog):
    def broken(number, repo):
        raise RuntimeError("api down")

    monkeypatch.setattr(seam, "issue_comments", broken)
    assert runner.previous_repo_config_sha(1, "owner/repo") is None
    assert "previous_lookup_failed" in caplog.text


def test_pick_issue_uses_a_custom_dispatch_label(monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.pick_issue("owner/repo", dispatch_label="custom-ready") is None
    searched = " ".join(" ".join(command) for command in seen)
    assert "label:custom-ready" in searched
    assert "label:ai-ready" not in searched


def test_pick_issue_with_repo_policy_uses_repo_scan_keys(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["gh", "api"]:
            return _record(
                'dispatch_label = "repo-ready"\n'
                'active_milestone = "v9.9.9"\n',
            )(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    config = runner.RunnerConfig()
    assert runner._pick_issue_with_repo_policy("owner/repo", "v1.0.0", config) is None
    searched = "\n".join(" ".join(command) for command in commands)
    assert "label:repo-ready" in searched
    assert 'milestone:"v9.9.9"' in searched


def test_pick_issue_with_repo_policy_ignores_a_malformed_file(monkeypatch):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return json.dumps({"sha": "x", "content": _b64("nope = 1\n")})
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The scan keeps the host keys and stays alive; process_issue blocks.
    assert runner._pick_issue_with_repo_policy(
        "owner/repo", "v1.0.0", runner.RunnerConfig(),
    ) is None


def test_pick_issue_with_repo_policy_without_config_uses_host_keys(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    runner._pick_issue_with_repo_policy("owner/repo", "v1.0.0", None)
    searched = "\n".join(" ".join(command) for command in commands)
    assert "label:ai-ready" in searched
    assert "gh api" not in searched


def test_started_pi_comment_body_renders_repo_config_fields():
    from orbi.progress import field_block

    body = runner.started_pi_comment_body(
        "a1b2c3d4",
        "base_branch=beta base_sha=abc run_id=a1b2c3d4 priority=normal "
        "repo_config=" + "a" * 40,
        "branch", Path("/tmp/wt"),
        extra_fields={
            "repo_config_changed": "old..new",
            "repo_config_diff": "base_branch=main->beta",
        },
    )
    assert "- repo_config: " + "a" * 40 in body
    assert "- repo_config_changed: old..new" in body
    assert "- repo_config_diff: base_branch=main->beta" in body


def test_process_issue_applies_repo_base_branch_and_records_sha(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _record('base_branch = "beta"\n', sha="b" * 40)(command)
        return "[]"

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "has_in_progress_label", lambda number, repo: False,
    )
    monkeypatch.setattr(seam, "open_pr_for_branch", lambda *a: None)
    monkeypatch.setattr(runner, "external_takeover_pr", lambda *a: None)
    monkeypatch.setattr(seam, "stable_branch_exists", lambda *a: False)
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: seen.setdefault("base", base_branch) or "sha",
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **k: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "write_run_state", lambda *a, **k: None)
    monkeypatch.setattr(runner, "resume_context", lambda worktree: None)
    monkeypatch.setattr(runner, "apply_runner_runtime_excludes", lambda *a: None)
    monkeypatch.setattr(
        runner, "run_pi",
        lambda issue, worktree, config, repo, **kwargs: seen.setdefault(
            "test_command", config.test_command,
        ),
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: "https://github.com/owner/repo/pull/1",
    )
    starts = []

    def fake_comment(number, repo, body):
        starts.append(body)

    monkeypatch.setattr(seam, "comment_issue", fake_comment)
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "set_active_run", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "ProgressPublisher",
        lambda *a, **k: type("P", (), {
            "ensure": lambda self, body: None,
            "patch": lambda self, body: None,
            "milestone": lambda self, body: None,
            "finish": lambda self, body: None,
            "comment_id": None,
        })(),
    )
    monkeypatch.setattr(seam, "_safe_publish", lambda **kwargs: kwargs["action"](),
    )
    monkeypatch.setattr(runner, "runner_health", type("H", (), {
        "record_pickup": staticmethod(lambda *a: None),
        "record_run_attempt": staticmethod(lambda *a, **k: None),
        "health_state_path": staticmethod(lambda *a: Path("/tmp/x")),
        "failure_fingerprint": staticmethod(lambda *a: ""),
    }))
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [],
    )
    result = runner.process_issue(
        {"number": 4, "title": "T", "body": "", "labels": []},
        runner.RunnerConfig(
            repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
            base_branch="main",
        ),
        "owner/repo",
        repo_config.RepoPolicy(base_branch="beta", sha="b" * 40),
    )
    assert result.kind == "pr"
    assert seen["base"] == "beta"
    start = starts[0]
    assert "- base_branch: beta" in start
    assert "- repo_config: " + "b" * 40 in start


def _write_main_config(tmp_path):
    (tmp_path / "prompts").mkdir()
    for name in ("prompts/prompt.md", "prompts/prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    return config


def test_main_applies_repo_base_branch_before_resume_verification(
    monkeypatch, tmp_path,
):
    """The effective repository base branch must drive the resume
    verification AND the delivery wait (Issue #527): otherwise a PR frozen
    on the repository base would be rejected as a base mismatch."""
    config = _write_main_config(tmp_path)
    issue = {"number": 9, "title": "ship", "body": "", "labels": []}
    scene = {
        "run_id": "a1b2c3d4", "base_branch": "beta", "base_sha": "s",
        "pr_url": "https://github.com/owner/repo/pull/98", "external": "",
    }
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None,
        **_kwargs: ("owner/repo", issue, scene),
    )
    seen = {}

    def fake_verify(scene_, issue_, config_, source_repo):
        seen["verify_base"] = config_.base_branch
        return "https://github.com/owner/repo/pull/98"

    monkeypatch.setattr(runner, "verify_resumed_pr", fake_verify)
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *a, **k: seen.setdefault("wait_base", a[2].base_branch),
    )

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "api", "repos/owner/repo/contents/.github/orbi.toml"]:
            return _record('base_branch = "beta"\n', sha="b" * 40)(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.main(["--config", str(config)]) == 0
    assert seen == {"verify_base": "beta", "wait_base": "beta"}


def test_main_blocks_a_claim_when_the_repo_config_is_invalid(
    monkeypatch, tmp_path,
):
    config = _write_main_config(tmp_path)
    issue = {"number": 9, "title": "ship", "body": "", "labels": []}
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None,
        **_kwargs: ("owner/repo", issue, None),
    )
    blocked = []
    monkeypatch.setattr(
        runner, "block_repo_config_failure",
        lambda *a, **k: blocked.append((a, k)),
    )

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "api", "repos/owner/repo/contents/.github/orbi.toml"]:
            return json.dumps({
                "sha": "x", "content": _b64("pi_model = 'evil'\n"),
            })
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.main(["--config", str(config)]) == 0
    assert len(blocked) == 1
    assert "pi_model" in str(blocked[0][0][2])


def test_resolve_policy_overrides_milestone_and_test_command():
    effective = repo_config.resolve_policy(
        runner.RunnerConfig(active_milestone="v1", test_command="host"),
        repo_config.RepoPolicy(active_milestone="v2", test_command="repo"),
    )
    assert effective.active_milestone == "v2"
    assert effective.test_command == "repo"


def test_policy_diff_formats_lists_and_none():
    diff = repo_config.policy_diff(
        repo_config.RepoPolicy(context_files=("a.md",)),
        repo_config.RepoPolicy(
            context_files=("a.md", "b.md"), test_command="pytest",
        ),
    )
    assert diff == (
        "context_files=a.md->a.md,b.md test_command=(none)->pytest"
    )


def test_read_repo_config_missing_size_field_is_ok():
    raw = json.dumps({"sha": "x", "content": _b64('base_branch = "b"\n')})
    policy = repo_config.read_repo_config(
        "owner/repo", run_command=lambda command, **kwargs: raw,
    )
    assert policy.base_branch == "b"


def test_read_repo_config_directory_listing_is_none():
    raw = json.dumps([{"name": "orbi.toml", "type": "file"}])
    assert repo_config.read_repo_config(
        "owner/repo", run_command=lambda command, **kwargs: raw,
    ) is None


def test_read_repo_config_at_invalid_toml_is_none():
    raw = json.dumps({"content": _b64('base_branch = "unterminated')})
    assert repo_config.read_repo_config_at(
        "owner/repo", "abc", run_command=lambda command, **kwargs: raw,
    ) is None


def test_parse_repositories_rejects_non_string_config_path(tmp_path):
    with pytest.raises(ValueError, match=r"config_path must be a non-empty"):
        runner.parse_repositories(
            [{
                "name": "p", "path": "repo", "github": "owner/p",
                "base_branch": "main", "config_path": 7,
            }],
            tmp_path,
        )


def test_parse_repositories_honors_a_custom_config_path(tmp_path):
    repos = runner.parse_repositories(
        [{
            "name": "p", "path": "repo", "github": "owner/p",
            "base_branch": "main", "config_path": ".orbi/policy.toml",
        }],
        tmp_path,
    )
    assert repos[0]["config_path"] == ".orbi/policy.toml"


def test_block_repo_config_failure_is_best_effort(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("github down")

    monkeypatch.setattr(seam, "apply_label_patch", broken)
    runner.block_repo_config_failure(
        1, "owner/repo", ValueError("bad"), "a1b2c3d4",
    )
    assert "repo_config_failure_report_failed" in caplog.text


def test_run_pi_injects_repo_test_command_and_context_files(
    monkeypatch, tmp_path,
):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{{TEST_COMMAND}}|{{CONTEXT_FILES}}", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("guide", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    monkeypatch.setattr(runner, "prepare_pi_agent_dir", lambda *a, **k: None)
    config = runner.RunnerConfig(
        prompt=prompt, repo_dir=tmp_path,
        source_repos=("owner/repo",), workspace_root=tmp_path,
        context_files=(), skills=(), base_branch="main",
        base_sha="sha", run_id="run1", test_command="pytest -q",
        repo_context_files=("AGENTS.md",),
    )
    assert runner.run_pi(
        {"number": 4, "title": "T", "body": "b"}, tmp_path, config, "owner/repo",
    ) == "done"
    system_prompt = calls[0][calls[0].index("--system-prompt") + 1]
    assert "pytest -q|" in system_prompt
    assert str(tmp_path / "AGENTS.md") in system_prompt


def test_process_issue_accepts_a_pre_resolved_record(monkeypatch, tmp_path):
    """`main` resolves the policy once and passes the record in (Issue
    #527); `process_issue` must not re-read or re-block."""
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "load_repo_policy",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("process_issue must not re-read the policy"),
        ),
    )
    monkeypatch.setattr(seam, "has_in_progress_label", lambda number, repo: False,
    )
    monkeypatch.setattr(seam, "open_pr_for_branch", lambda *a: None)
    monkeypatch.setattr(runner, "external_takeover_pr", lambda *a: None)
    monkeypatch.setattr(seam, "stable_branch_exists", lambda *a: False)
    seen = {}
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: seen.setdefault("base", base_branch),
    )
    monkeypatch.setattr(seam, "create_worktree",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(runner, "runner_health", type("H", (), {
        "record_pickup": staticmethod(lambda *a: None),
        "record_run_attempt": staticmethod(lambda *a, **k: None),
        "health_state_path": staticmethod(lambda *a: Path("/tmp/x")),
        "failure_fingerprint": staticmethod(lambda *a: ""),
    }))
    result = runner.process_issue(
        {"number": 4, "title": "T", "body": "", "labels": []},
        runner.RunnerConfig(
            repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
            base_branch="main",
        ),
        "owner/repo",
        repo_config.RepoPolicy(base_branch="beta", sha="s"),
    )
    assert result.kind == "failed"
    assert seen["base"] == "beta"


def test_repo_policies_are_isolated_per_repository(monkeypatch):
    """A malformed file blocks only its own repository (Issue #527):
    a sibling repo with a valid file reads and applies normally."""
    def fake_run(command, **kwargs):
        endpoint = command[2]
        if "owner/good" in endpoint:
            return json.dumps({
                "sha": "g", "content": _b64('base_branch = "beta"\n'),
            })
        return json.dumps({
            "sha": "b", "content": _b64("source_repos = 1\n"),
        })

    monkeypatch.setattr(seam, "run_command", fake_run)
    good = runner.load_repo_policy(runner.RunnerConfig(), "owner/good")
    assert good == repo_config.RepoPolicy(base_branch="beta", sha="g")
    with pytest.raises(repo_config.RepoConfigError):
        runner.load_repo_policy(runner.RunnerConfig(), "owner/bad")


# --- this repository's own policy file (Issue #731) -------------------------

def test_this_repositorys_own_orbi_toml_is_valid_and_declares_test_command():
    """Orbi dogfoods its own config-as-code (Issue #731): the file this
    repository carries at `.github/orbi.toml` must pass the strict claim
    schema and declare the test command the implementer prompt injects —
    a broken file here would block every claim of this repository with
    `repo_config_invalid`."""
    path = Path(__file__).resolve().parents[1] / ".github" / "orbi.toml"
    assert path.is_file(), (
        "the repository must carry .github/orbi.toml (Issue #731)"
    )
    policy = repo_config.parse_repo_config(
        path.read_text(encoding="utf-8"),
    )
    assert isinstance(policy.test_command, str) and policy.test_command.strip()


def test_this_repositorys_own_orbi_toml_is_not_gitignored():
    """The policy file must be deliverable (Issue #731): the tracked
    .gitignore ignores the local ROOT-level `orbi.toml` (host-style
    engine config), and the bare pattern must not reach into `.github/`
    — an ignored `.github/orbi.toml` could never reach the default
    branch, so every claim would keep falling back to the host config."""
    root = Path(__file__).resolve().parents[1]

    def ignored(relative: str) -> bool:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", relative],
            cwd=root, capture_output=True,
        )
        return proc.returncode == 0

    assert not ignored(".github/orbi.toml")
    assert ignored("orbi.toml")


# --- typed config contract (Issue #790) --------------------------------------

def test_parse_repo_config_returns_frozen_repo_policy():
    policy = repo_config.parse_repo_config(
        'base_branch = "beta"\ncontext_files = ["AGENTS.md"]\n'
    )
    assert isinstance(policy, repo_config.RepoPolicy)
    assert policy.base_branch == "beta"
    assert policy.context_files == ("AGENTS.md",)
    assert policy.active_milestone is None  # omitted keys stay None
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.base_branch = "main"


def test_parse_repo_config_empty_document_is_an_empty_policy():
    assert repo_config.parse_repo_config("# nothing here\n") == (
        repo_config.RepoPolicy()
    )


def test_read_repo_config_returns_the_policy_with_bound_sha():
    policy = repo_config.read_repo_config(
        "owner/repo",
        run_command=_record('base_branch = "beta"\n', sha="cafe" * 10),
    )
    assert policy == repo_config.RepoPolicy(
        base_branch="beta", sha="cafe" * 10,
    )


def test_resolve_policy_returns_a_runner_config_with_declared_overrides():
    host = runner.RunnerConfig(
        base_branch="main", active_milestone="v0.1.0",
        context_files=(Path("/host/ctx.md"),),
        test_command="host-cmd", dispatch_label="ai-ready",
    )
    effective = repo_config.resolve_policy(
        host, repo_config.RepoPolicy(
            base_branch="beta", dispatch_label="custom-ready",
        ),
    )
    assert isinstance(effective, runner.RunnerConfig)
    assert effective.base_branch == "beta"
    assert effective.dispatch_label == "custom-ready"
    # Omitted keys keep the host fallback.
    assert effective.active_milestone == "v0.1.0"
    assert effective.context_files == (Path("/host/ctx.md"),)
    assert effective.test_command == "host-cmd"
    assert effective.repo_context_files == ()


def test_resolve_policy_context_files_are_additive():
    effective = repo_config.resolve_policy(
        runner.RunnerConfig(context_files=(Path("/host.md"),)),
        repo_config.RepoPolicy(context_files=("AGENTS.md",)),
    )
    assert effective.context_files == (Path("/host.md"),)
    assert effective.repo_context_files == ("AGENTS.md",)


def test_policy_diff_compares_policy_fields_and_skips_the_sha():
    assert repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="main", sha="a" * 40),
    ) is None
    diff = repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="beta", test_command="pytest"),
    )
    assert diff == "base_branch=main->beta test_command=(none)->pytest"


def test_repo_config_audit_typed_marks_a_changed_sha_with_the_diff():
    fields = repo_config.repo_config_audit(
        "newsha", repo_config.RepoPolicy(base_branch="beta"),
        previous_sha="oldsha",
        previous_policy=repo_config.RepoPolicy(base_branch="main"),
    )
    assert fields["repo_config_changed"] == "oldsha..newsha"
    assert fields["repo_config_diff"] == "base_branch=main->beta"
