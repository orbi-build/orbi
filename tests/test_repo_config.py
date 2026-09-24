"""Repository-level config-as-code tests (Issue #527).

Covers the strict schema (host-only / type / path rejections; an unknown key
is ignored with a warning, Issue #1329), the
per-key merge over the host fallback (D3), the `gh api` read pipeline (missing
file -> no-op, malformed file -> fail fast, read error -> fail open), the D4
change-visibility audit, and the runner wiring at the claim scan and claim.
"""
from orbi import __version__, config as config_domain
import base64
import dataclasses
import json
import logging
import subprocess
from pathlib import Path

import pytest

import orbi.repo_config as repo_config
import orbi.claim as claim
import orbi.runner as runner
import orbi.pi_session as pi_session
import orbi.milestone as milestone
from seam import resume_deps, seam
from orbi.delivery_scene import RunContext


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
        'context_files = ["AGENTS.md", "docs/testing.mdx"]\n'
        'dispatch_label = "ai-ready"\n'
        'steering_enabled = false\n'
        'steering_poll_seconds = 0.1\n'
        'steering_max_rounds = 7\n'
        'release_confirmation = true\n'
        'clarify_thin_tickets = true\n'
    )
    assert repo_config.parse_repo_config(text) == repo_config.RepoPolicy(
        base_branch="beta",
        active_milestone="v0.5.0",
        context_files=("AGENTS.md", "docs/testing.mdx"),
        dispatch_label="ai-ready",
        steering_enabled=False,
        steering_poll_seconds=0.1,
        steering_max_rounds=7,
        release_confirmation=True,
        clarify_thin_tickets=True,
    )


def test_clarify_thin_tickets_is_a_repository_policy_key():
    """Issue #1088: the thin-ticket clarification gate is an opt-in the
    repository declares, so it never becomes host-only state."""
    assert "clarify_thin_tickets" in repo_config.POLICY_KEYS
    assert "clarify_thin_tickets" not in repo_config.HOST_ONLY_KEYS
    policy = repo_config.parse_repo_config("clarify_thin_tickets = true\n")
    assert policy.clarify_thin_tickets is True
    assert repo_config.parse_repo_config("clarify_thin_tickets = false\n") == (
        repo_config.RepoPolicy(clarify_thin_tickets=False)
    )


@pytest.mark.parametrize("value", ['"true"', "1", '["yes"]'])
def test_parse_repo_config_rejects_a_non_boolean_clarify_thin_tickets(value):
    with pytest.raises(
        repo_config.RepoConfigError,
        match="clarify_thin_tickets must be a boolean",
    ):
        repo_config.parse_repo_config(f"clarify_thin_tickets = {value}\n")


def test_release_confirmation_is_a_repository_policy_key():
    """Issue #856: the finished-Milestone release confirmation is an
    opt-in the repository that owns the Milestone declares — never a
    host-only key."""
    assert "release_confirmation" in repo_config.POLICY_KEYS
    assert "release_confirmation" not in repo_config.HOST_ONLY_KEYS
    policy = repo_config.parse_repo_config("release_confirmation = true\n")
    assert policy.release_confirmation is True
    assert repo_config.parse_repo_config("release_confirmation = false\n") == (
        repo_config.RepoPolicy(release_confirmation=False)
    )


@pytest.mark.parametrize("value", ['"true"', "1", '["yes"]'])
def test_parse_repo_config_rejects_a_non_boolean_release_confirmation(value):
    with pytest.raises(
        repo_config.RepoConfigError,
        match="release_confirmation must be a boolean",
    ):
        repo_config.parse_repo_config(f"release_confirmation = {value}\n")


def test_parse_repo_config_tolerates_a_legacy_test_command():
    """Issue #805: `test_command` left the whitelist — the merge gate is
    the GitHub Actions check runs and the AI infers the test method
    itself. A repository file that still declares the legacy key must
    keep delivering: it is ignored, never rejected and never stored."""
    policy = repo_config.parse_repo_config(
        'base_branch = "beta"\ntest_command = "pytest -q"\n',
    )
    assert policy == repo_config.RepoPolicy(base_branch="beta")
    assert not hasattr(policy, "test_command")


def test_parse_repo_config_empty_document_is_a_no_op():
    """A file with no policy keys is valid and changes nothing (D3: no
    whole-file override)."""
    assert repo_config.parse_repo_config("# nothing here\n") == (
        repo_config.RepoPolicy()
    )


def test_parse_repo_config_rejects_bad_toml():
    with pytest.raises(repo_config.RepoConfigError, match="invalid TOML"):
        repo_config.parse_repo_config('base_branch = "unterminated')


def test_parse_repo_config_ignores_an_unknown_future_key(caplog):
    """Issue #1329: a key the running engine does not know is ignored.

    The policy file is read by whatever engine version the delivery host
    runs, often older than the code that wrote the file. Rejecting a
    newer key deadlocks delivery: the release that knows the key cannot
    ship, because shipping it goes through a claim the key blocks."""
    with caplog.at_level(logging.WARNING, logger="orbi.bootstrap"):
        policy = repo_config.parse_repo_config(
            'active_milestone = "v1.0.0"\nsome_future_key = true\n',
        )
    assert policy.active_milestone == "v1.0.0"
    assert policy.ignored_keys == ("some_future_key",)
    assert "repo_config_keys_ignored" in caplog.text
    assert "some_future_key" in caplog.text
    assert __version__ in caplog.text


def test_parse_repo_config_reports_every_ignored_key_sorted():
    policy = repo_config.parse_repo_config('zeta = 1\nalpha = "x"\n')
    assert policy.ignored_keys == ("alpha", "zeta")


def test_parse_repo_config_without_unknown_keys_reports_nothing_ignored():
    policy = repo_config.parse_repo_config('base_branch = "beta"\n')
    assert policy.ignored_keys == ()


def test_parse_repo_config_still_rejects_a_host_only_key_before_ignoring():
    """The #527 credential red line outranks the #1329 fail-open."""
    with pytest.raises(
        repo_config.RepoConfigError, match=r"host-only key\(s\).*pi_model",
    ):
        repo_config.parse_repo_config(
            'pi_model = "m"\nsome_future_key = true\n',
        )


def test_parse_repo_config_still_rejects_a_bad_value_of_a_known_key():
    """A typo'd value of a known key is not a future key."""
    with pytest.raises(
        repo_config.RepoConfigError,
        match="clarify_thin_tickets must be a boolean",
    ):
        repo_config.parse_repo_config('clarify_thin_tickets = "yes"\n')


def test_ignored_keys_reach_the_policy_audit_comment():
    fields = repo_config.repo_config_audit(
        "sha1", repo_config.RepoPolicy(ignored_keys=("some_future_key",)),
        previous_sha=None, previous_policy=None,
    )
    assert fields["repo_config_ignored"] == "some_future_key"


def test_ignored_keys_are_not_a_policy_diff():
    """`ignored_keys` is a read observation, not a policy key."""
    assert repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="beta"),
        repo_config.RepoPolicy(base_branch="beta", ignored_keys=("x",)),
    ) is None


@pytest.mark.parametrize(
    "key",
    ["source_repos", "pi_provider", "repo_dir", "prompt", "skills",
     "engine_source_track", "issue_comments_limit", "human_review_gate"],
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
        'dispatch_label = 7\n',
        "context_files = []\n",
        'context_files = "AGENTS.md"\n',
        'context_files = ["AGENTS.md", 3]\n',
        'context_files = ["/etc/passwd"]\n',
        'steering_enabled = "yes"\n',
        'steering_poll_seconds = "60"\n',
        'steering_max_rounds = 1.5\n',
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
    host = config_domain.RunnerConfig(
        base_branch="main",
        active_milestone="v0.1.0",
        context_files=(Path("/host/ctx.md"),),
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
    # Repository context files are additive under their own key.
    assert effective.repo_context_files == ()


def test_resolve_policy_overrides_steering_and_omitted_keys_fall_back():
    host = config_domain.RunnerConfig(
        steering_enabled=True, steering_poll_seconds=60.0,
        steering_max_rounds=3,
    )
    effective = repo_config.resolve_policy(
        host,
        repo_config.RepoPolicy(
            steering_enabled=False, steering_poll_seconds=1.0,
        ),
    )
    assert effective.steering_enabled is False
    assert effective.steering_poll_seconds == 1.0
    assert effective.steering_max_rounds == 3


def test_resolve_policy_overrides_all_declared_steering_keys():
    effective = repo_config.resolve_policy(
        config_domain.RunnerConfig(
            steering_enabled=True, steering_poll_seconds=60.0,
            steering_max_rounds=3,
        ),
        repo_config.RepoPolicy(
            steering_enabled=False, steering_poll_seconds=1.0,
            steering_max_rounds=7,
        ),
    )
    assert (effective.steering_enabled, effective.steering_poll_seconds,
            effective.steering_max_rounds) == (False, 1.0, 7)


def test_resolve_policy_overrides_clarify_thin_tickets():
    """Issue #1088: the repository flag reaches the effective config and
    an omitted key keeps the host default (off)."""
    host = config_domain.RunnerConfig(clarify_thin_tickets=False)
    assert repo_config.resolve_policy(
        host, repo_config.RepoPolicy(clarify_thin_tickets=True),
    ).clarify_thin_tickets is True
    assert repo_config.resolve_policy(
        host, repo_config.RepoPolicy(),
    ).clarify_thin_tickets is False


# --- D4 audit ---------------------------------------------------------------

def test_policy_diff_reports_changed_keys_only():
    assert repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="main"),
    ) is None
    diff = repo_config.policy_diff(
        repo_config.RepoPolicy(base_branch="main"),
        repo_config.RepoPolicy(base_branch="beta"),
    )
    assert diff == "base_branch=main->beta"


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
        return json.dumps({"sha": "x", "content": _b64('pi_model = "m"\n')})

    with pytest.raises(repo_config.RepoConfigError, match="host-only key"):
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
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *a, **k: None)
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones", lambda *a, **k: None,
    )
    config = config_domain.RunnerConfig()
    assert claim.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1, config=config,
        hooks=resume_deps(),
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
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *a, **k: None)
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones", lambda *a, **k: None,
    )
    assert claim.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
        config=config_domain.RunnerConfig(),
        hooks=resume_deps(),
    ) is None
    assert any(
        search.startswith("label:ai-ready label:ai-in-progress")
        for search in searches
    )


def test_repository_config_path_defaults_and_honors_the_entry():
    config = config_domain.RunnerConfig(repositories=(
        {"github": "owner/pilot", "config_path": ".orbi/policy.toml"},
    ))
    assert config_domain.repository_config_path(config, "owner/pilot") == ".orbi/policy.toml"
    assert config_domain.repository_config_path(config, "owner/other") == ".github/orbi.toml"
    assert config_domain.repository_config_path(
        config_domain.RunnerConfig(), "owner/other",
    ) == ".github/orbi.toml"


def test_repository_base_branch_falls_back_to_the_entry_then_host():
    config = config_domain.RunnerConfig(
        base_branch="main",
        repositories=({"github": "owner/pilot", "base_branch": "develop"},),
    )
    assert runner.repository_base_branch(config, "owner/pilot") == "develop"
    assert runner.repository_base_branch(config, "owner/other") == "main"


def test_apply_repo_policy_uses_the_entry_base_branch_as_fallback():
    config = config_domain.RunnerConfig(
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
    monkeypatch.setattr(seam, "_authenticated_github_login", lambda: "orbi")
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
    assert claim.pick_issue("owner/repo", dispatch_label="custom-ready") is None
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
    config = config_domain.RunnerConfig()
    assert claim._pick_issue_with_repo_policy("owner/repo", "v1.0.0", config) is None
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
    assert claim._pick_issue_with_repo_policy(
        "owner/repo", "v1.0.0", config_domain.RunnerConfig(),
    ) is None


def test_pick_issue_with_repo_policy_without_config_uses_host_keys(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return json.dumps([])

    monkeypatch.setattr(seam, "run_command", fake_run)
    claim._pick_issue_with_repo_policy("owner/repo", "v1.0.0", None)
    searched = "\n".join(" ".join(command) for command in commands)
    assert "label:ai-ready" in searched
    assert "gh api" not in searched


def test_started_pi_comment_body_renders_repo_config_fields():
    from orbi.progress import field_block

    body = runner.started_pi_comment_body(
        RunContext(
            run_id="a1b2c3d4", issue=18, branch="branch",
            worktree=Path("/tmp/wt"), source_repo="owner/repo",
        ),
        "base_branch=beta base_sha=abc run_id=a1b2c3d4 priority=normal "
        "repo_config=" + "a" * 40,
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
    monkeypatch.setattr(pi_session, "resume_context", lambda worktree: None)
    monkeypatch.setattr(pi_session, "apply_runner_runtime_excludes", lambda *a: None)
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda issue, ctx, config, **kwargs: "done",
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
        config_domain.RunnerConfig(
            repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
            base_branch="main",
        ),
        "owner/repo",
        repo_config.RepoPolicy(
            base_branch="beta", sha="b" * 40,
            ignored_keys=("some_future_key",),
        ),
    )
    assert result.kind == "pr"
    assert seen["base"] == "beta"
    start = starts[0]
    assert "- base_branch: beta" in start
    assert "- repo_config: " + "b" * 40 in start
    # Issue #1329: the claim proceeds with the known keys and the run
    # comment records the keys this engine ignored (the #527 host-only
    # key never reaches here — it blocks the claim before this point).
    assert "- repo_config_ignored: some_future_key" in start


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
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None,
        **_kwargs: ("owner/repo", issue, scene),
    )
    seen = {}

    def fake_verify(scene_, issue_, config_, source_repo):
        seen["verify_base"] = config_.base_branch
        return "https://github.com/owner/repo/pull/98"

    monkeypatch.setattr(runner, "verify_resumed_pr", fake_verify)
    monkeypatch.setattr(
        runner, "delivery_step",
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
        claim, "pick_next_delivery",
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


def test_resolve_policy_overrides_the_declared_milestone():
    effective = repo_config.resolve_policy(
        config_domain.RunnerConfig(active_milestone="v1"),
        repo_config.RepoPolicy(active_milestone="v2"),
    )
    assert effective.active_milestone == "v2"


def test_policy_diff_formats_lists_and_none():
    diff = repo_config.policy_diff(
        repo_config.RepoPolicy(context_files=("a.md",)),
        repo_config.RepoPolicy(context_files=("a.md", "b.md")),
    )
    assert diff == "context_files=a.md->a.md,b.md"


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
        config_domain.parse_repositories(
            [{
                "name": "p", "path": "repo", "github": "owner/p",
                "base_branch": "main", "config_path": 7,
            }],
            tmp_path,
        )


def test_parse_repositories_honors_a_custom_config_path(tmp_path):
    repos = config_domain.parse_repositories(
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


def test_run_pi_injects_repo_context_files(monkeypatch, tmp_path):
    """Issue #805: the implementer prompt no longer carries a declared
    test command — the agent infers the test method from the repository;
    only the context files stay injectable."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{{CONTEXT_FILES}}", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("guide", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    monkeypatch.setattr(pi_session, "prepare_pi_agent_dir", lambda *a, **k: None)
    config = config_domain.RunnerConfig(
        prompt=prompt, repo_dir=tmp_path,
        source_repos=("owner/repo",), workspace_root=tmp_path,
        context_files=(), skills=(), base_branch="main",
        base_sha="sha", run_id="run1",
        repo_context_files=("AGENTS.md",),
    )
    assert pi_session.run_pi(
        {"number": 4, "title": "T", "body": "b"},
        runner.RunContext(
            run_id=config.run_id, issue=4, branch="orbi/owner-repo-issue-4",
            worktree=tmp_path, source_repo="owner/repo",
        ),
        config,
    ) == "done"
    system_prompt = calls[0][calls[0].index("--system-prompt") + 1]
    assert str(tmp_path / "AGENTS.md") in system_prompt


def test_process_issue_accepts_a_pre_resolved_record(monkeypatch, tmp_path):
    """`main` resolves the policy once and passes the record in (Issue
    #527); `process_issue` must not re-read or re-block."""
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        seam, "load_repo_policy",
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
        config_domain.RunnerConfig(
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
    good = runner.load_repo_policy(config_domain.RunnerConfig(), "owner/good")
    assert good == repo_config.RepoPolicy(base_branch="beta", sha="g")
    with pytest.raises(repo_config.RepoConfigError):
        runner.load_repo_policy(config_domain.RunnerConfig(), "owner/bad")


# --- this repository's own policy file (Issue #731) -------------------------

def test_this_repositorys_own_orbi_toml_is_valid_and_legacy_free():
    """Orbi dogfoods its own config-as-code (Issue #731): the file this
    repository carries at `.github/orbi.toml` must pass the strict claim
    schema and no longer declare the removed `test_command` key (Issue
    #805) — a broken file here would block every claim of this
    repository with `repo_config_invalid`."""
    path = Path(__file__).resolve().parents[1] / ".github" / "orbi.toml"
    assert path.is_file(), (
        "the repository must carry .github/orbi.toml (Issue #731)"
    )
    text = path.read_text(encoding="utf-8")
    repo_config.parse_repo_config(text)
    assert "test_command" not in text


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


def test_resolve_policy_returns_a_runner_config_with_declared_overrides():
    host = config_domain.RunnerConfig(
        base_branch="main", active_milestone="v0.1.0",
        context_files=(Path("/host/ctx.md"),),
        dispatch_label="ai-ready",
    )
    effective = repo_config.resolve_policy(
        host, repo_config.RepoPolicy(
            base_branch="beta", dispatch_label="custom-ready",
        ),
    )
    assert isinstance(effective, config_domain.RunnerConfig)
    assert effective.base_branch == "beta"
    assert effective.dispatch_label == "custom-ready"
    # Omitted keys keep the host fallback.
    assert effective.active_milestone == "v0.1.0"
    assert effective.context_files == (Path("/host/ctx.md"),)
    assert effective.repo_context_files == ()


def test_resolve_policy_context_files_are_additive():
    effective = repo_config.resolve_policy(
        config_domain.RunnerConfig(context_files=(Path("/host.md"),)),
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
        repo_config.RepoPolicy(base_branch="beta"),
    )
    assert diff == "base_branch=main->beta"


def test_resolve_source_base_branch_fuses_entry_without_a_policy_file():
    """With NO repository policy file the entry fallback must STILL
    apply: before this fix the dev path read the host base_branch while
    the release path re-derived the entry value — two paths, two base
    branches for the same repository. A policy, when present, still
    overrides the entry."""
    config = config_domain.RunnerConfig(
        repo_dir=Path("/repo"), source_repos=("o/r",),
        deploy_home=Path("/repo"), max_concurrency=2,
        base_branch="main",
        repositories=[{"github": "o/r", "base_branch": "develop"}],
    )
    fused = runner.resolve_source_base_branch(config, "o/r", None)
    assert fused.base_branch == "develop"
    other = runner.resolve_source_base_branch(config, "other/repo", None)
    assert other.base_branch == "main"

    policy = repo_config.RepoPolicy(base_branch="release")
    overridden = runner.resolve_source_base_branch(config, "o/r", policy)
    assert overridden.base_branch == "release"
