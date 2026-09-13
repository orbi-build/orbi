"""Runner config domain: `load_config` / `validate_config` (Issue #789).

The first domain split out of `test_bootstrap_runner.py` (moved
verbatim, no behavior change): the tests arrange config files and call
the public `load_config` / `validate_config` entry points directly —
the config surface is pure file-driven behavior. Follow-up domains
move to their own modules under sub-issues.
"""
import dataclasses
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner


def test_load_config_resolves_relative_paths_and_values(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        """source_repos = [\"owner/repo\"]\nrepo_dir = \"repo\"\nworkspace_root = \"..\"\nprompt = \"prompt.md\"\nskills = [\"skill.md\"]\ncontext_files = [\"context.md\"]\n""",
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.source_repos == ("owner/repo",)
    assert config.repo_dir == (tmp_path / "repo").resolve()
    assert config.workspace_root == tmp_path.parent.resolve()
    assert config.prompt == (tmp_path / "prompt.md").resolve()
    assert config.skills == ((tmp_path / "skill.md").resolve(),)
    assert config.context_files == ((tmp_path / "context.md").resolve(),)


def test_validate_execution_source_repos_rejects_multiple_checkouts():
    with pytest.raises(
        ValueError,
        match="multiple source_repos are not supported with one checkout",
    ):
        runner.validate_execution_source_repos(
            ["owner/first", "owner/second"],
        )


def test_example_config_passes_execution_source_repos_validation():
    """Issue #697: the committed example must be a config the Runner
    accepts — exactly one source repository until multi-repo workspaces
    are available (Issue #133). Issue #163: the example lives INSIDE
    the package (`src/orbi/example_config.toml`) so a PyPI install can
    create a config without a checkout-adjacent file."""
    example = (
        Path(__file__).resolve().parent.parent
        / "src" / "orbi" / "example_config.toml"
    )
    config = runner.load_config(example)
    assert len(config.source_repos) == 1
    runner.validate_execution_source_repos(config.source_repos)


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


def test_load_config_returns_the_frozen_runner_config(tmp_path):
    """Issue #790: `load_config` is the single constructor of the frozen
    `RunnerConfig` — every module consumes typed attribute access, and a
    frozen instance refuses field assignment."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert isinstance(config, runner.RunnerConfig)
    assert config.source_repos == ("owner/repo",)
    assert config.repo_dir == (tmp_path / "repo").resolve()
    assert config.base_branch == "main"
    assert config.git_transport == "ssh"
    assert config.max_concurrency == 1
    assert config.auto_next_milestone is True
    assert config.allow_stale_runner is False
    assert config.human_review_gate is False
    assert config.model_wait_dead_seconds == 1800.0
    assert config.issue_comments_limit == 20
    assert config.repositories == ()
    assert config.repo_context_files == ()
    assert config.run_id == ""
    assert config.base_sha == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.base_branch = "beta"


def test_load_config_defaults_base_branch_to_main(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.base_branch == "main"


def test_load_config_defaults_git_transport_to_ssh(tmp_path):
    """Issue #580: the delivery transport key is optional — absent keeps
    the exact pre-#580 SSH contract."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path).git_transport == "ssh"


def test_load_config_accepts_the_https_git_transport(tmp_path):
    """Issue #580: `git_transport = "https"` selects the token-only
    sandbox path (origin stays HTTPS, gh credential helper)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\ngit_transport = "https"\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).git_transport == "https"


def test_load_config_rejects_an_unknown_git_transport(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\ngit_transport = "gopher"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="git_transport must be"):
        runner.load_config(config_path)


def test_load_config_rejects_a_non_string_git_transport(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\ngit_transport = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="git_transport must be"):
        runner.load_config(config_path)


def test_load_config_defaults_engine_source_track_to_main(tmp_path):
    """Issue #535: absent engine_source_track keeps the exact pre-#535
    dogfood behavior — the deploy home tracks origin/main."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path).engine_source_track == "main"


@pytest.mark.parametrize(
    "track",
    ["main", "release", "branch:release-candidate", "tag:v0.4.2",
     "sha:" + "a" * 40],
)
def test_load_config_accepts_the_engine_source_track_forms(tmp_path, track):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["owner/repo"]\nengine_source_track = "{track}"\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).engine_source_track == track


def test_load_config_normalizes_stable_to_release(tmp_path):
    """Issue #756: `stable` loads as the plain `release` track — the
    stored config carries the canonical value only."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nengine_source_track = "stable"\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).engine_source_track == "release"


def test_load_config_rejects_an_invalid_engine_source_track(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nengine_source_track = "latest"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="engine_source_track"):
        runner.load_config(config_path)


def test_load_config_defaults_prompts_to_prompts_directory(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.prompt == (tmp_path / "prompts" / "prompt.md").resolve()
    assert config.prompt_review == (
        tmp_path / "prompts" / "prompt_review.md"
    ).resolve()


def test_load_config_maps_missing_explicit_legacy_prompt_to_new_asset(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nprompt = "prompt.md"\n'
        'prompt_review = "prompt_review.md"\n',
        encoding="utf-8",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (prompts / "prompt_review.md").write_text("review", encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.prompt == (prompts / "prompt.md").resolve()
    assert config.prompt_review == (prompts / "prompt_review.md").resolve()


def test_load_config_health_alert_repo_default_and_override(tmp_path):
    # Issue #345: absent -> None (derived from the deploy-home origin);
    # present -> the verbatim `owner/repo` override.
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path).health_alert_repo is None
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        'health_alert_repo = "fork-owner/orbi-fork"\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).health_alert_repo == \
        "fork-owner/orbi-fork"


def test_load_config_rejects_empty_health_alert_repo(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nhealth_alert_repo = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="health_alert_repo must be a non-empty string",
    ):
        runner.load_config(config_path)


def test_load_config_defaults_deploy_home_to_repo_dir(tmp_path):
    """Issue #330: without deploy_home the deployment home IS the delivery
    checkout — the orbi-bootstrap layout keeps its exact behavior."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.deploy_home == (tmp_path / "repo").resolve()


def test_load_config_reads_explicit_deploy_home(tmp_path):
    """Issue #330: an explicit deploy_home resolves like every other
    config path (relative to the config file dir)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'deploy_home = "home"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.repo_dir == (tmp_path / "repo").resolve()
    assert config.deploy_home == (tmp_path / "home").resolve()


def test_load_config_rejects_an_empty_deploy_home(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\ndeploy_home = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="deploy_home must be a non-empty string",
    ):
        runner.load_config(config_path)


def test_load_config_rejects_a_non_string_deploy_home(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\ndeploy_home = 7\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="deploy_home must be a non-empty string",
    ):
        runner.load_config(config_path)


def test_load_config_prompt_defaults_resolve_from_deploy_home(tmp_path):
    """Issue #330: with deploy_home set and no explicit prompt, the prompt
    defaults live in the deployment home — never in the delivery checkout."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'deploy_home = "home"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.prompt == (
        tmp_path / "home" / "prompts" / "prompt.md"
    ).resolve()
    assert config.prompt_review == (
        tmp_path / "home" / "prompts" / "prompt_review.md"
    ).resolve()


def test_load_config_explicit_prompt_resolves_from_config_dir(tmp_path):
    """Issue #330: an explicit prompt path keeps resolving against the
    config file dir even when deploy_home is set."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'deploy_home = "home"\nprompt = "my-prompt.md"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.prompt == (tmp_path / "my-prompt.md").resolve()


def test_validate_config_requires_the_deploy_home_dir(tmp_path):
    """Issue #330: a missing deployment home fails the start fast, like a
    missing delivery checkout."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'deploy_home = "missing-home"\n',
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    config = runner.load_config(config_path)
    with pytest.raises(FileNotFoundError):
        runner.validate_config(config)


def test_load_config_has_no_auto_repair_issues_field(tmp_path):
    """Issue #569: the repair-issue config field dispatched a failed
    local release test command; with the local test execution removed
    the field and its consumer are gone."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert getattr(runner.load_config(config_path), "auto_repair_issues",
                   "absent") == "absent"


def test_load_config_reads_explicit_base_branch(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = "develop"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.base_branch == "develop"


def test_load_config_rejects_empty_base_branch(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nbase_branch = ""\n', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="base_branch must be a non-empty string"):
        runner.load_config(config_path)


def test_load_config_defaults_auto_next_milestone_to_true(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path).auto_next_milestone is True


def test_load_config_reads_and_validates_auto_next_milestone(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nauto_next_milestone = false\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).auto_next_milestone is False
    config_path.write_text(
        'source_repos = ["owner/repo"]\nauto_next_milestone = "no"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="auto_next_milestone must be a boolean"):
        runner.load_config(config_path)


def test_load_config_defaults_active_milestone_to_none(tmp_path):
    """Issue #139: without an active_milestone the config keeps the
    current compat behavior (no milestone filter on the ready scans)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.active_milestone is None


def test_load_config_reads_explicit_active_milestone(tmp_path):
    """Issue #139: the active Milestone is an explicit claim scope —
    it is never guessed from the repo's Milestone list."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.active_milestone == "v0.2.0"


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
    model (27B Q4 GGUF, llama-server request timeout 1200 s — ~57
    tokens/s at 12K context on an RX 7900 XTX with all layers on GPU,
    well under 20 tokens/s on partial GPU offload or CPU-only) must
    not be killed merely because one complete assistant message takes
    more than 10 minutes."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.model_wait_dead_seconds == 1800.0


def test_load_config_reads_explicit_model_wait_dead_seconds_int(tmp_path):
    """Issue #228: an explicit integer override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmodel_wait_dead_seconds = 900\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.model_wait_dead_seconds == 900.0


def test_load_config_reads_explicit_model_wait_dead_seconds_float(tmp_path):
    """Issue #228: an explicit float override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmodel_wait_dead_seconds = 1234.5\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.model_wait_dead_seconds == 1234.5


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


# --- Issue #745: the trusted-comment injection cap is configurable -----------

def test_load_config_defaults_issue_comments_limit_to_twenty(tmp_path):
    """Issue #745: omitted -> 20 — the newest 20 trusted comments cover
    the recent decision history without letting a long-discussed Issue
    dominate the context window."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.issue_comments_limit == 20


def test_load_config_reads_explicit_issue_comments_limit(tmp_path):
    """Issue #745: an explicit integer override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nissue_comments_limit = 5\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.issue_comments_limit == 5


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("true", "boolean"),
        ("false", "boolean"),
        ("0", "positive"),
        ("-3", "positive"),
        ("2.5", "integer"),
        ('"20"', "integer"),
    ],
)
def test_load_config_rejects_invalid_issue_comments_limit(
    tmp_path, value, reason,
):
    """Issue #745: booleans, zero, negative, fractional and non-numeric
    values are rejected at config load with the field name and the
    concrete reason."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"issue_comments_limit = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "issue_comments_limit" in str(excinfo.value)
    assert reason in str(excinfo.value)


# --- Issue #233: the /slots swallow probe is configurable --------------------

def test_load_config_defaults_swallow_probe_disabled(tmp_path):
    """Issue #233: omitted -> the probe is disabled (None) and the grace
    defaults to 60 s: the run is bounded by model_wait_dead_seconds only
    (the exact pre-#233 behavior)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.model_wait_probe_url is None
    assert config.model_wait_probe_seconds == 60.0


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
    assert config.model_wait_probe_url == "http://127.0.0.1:18082/slots"
    assert config.model_wait_probe_seconds == 90.0


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


# --- Issue #381: release delivery wait is configurable ------------------------

def test_load_config_defaults_release_deliveries_wait_seconds(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.release_deliveries_wait_seconds == 1800.0


def test_load_config_reads_explicit_release_deliveries_wait_seconds(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrelease_deliveries_wait_seconds = 42\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).release_deliveries_wait_seconds == 42.0


@pytest.mark.parametrize("value", ["true", "0", "-1", '"42"'])
def test_load_config_rejects_invalid_release_deliveries_wait_seconds(tmp_path, value):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"release_deliveries_wait_seconds = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="release_deliveries_wait_seconds"):
        runner.load_config(config_path)


# --- Issue #268: release_ci_wait_seconds is configurable ---------------------

def test_load_config_defaults_release_ci_wait_seconds(tmp_path):
    """Issue #268: omitted -> RELEASE_CI_WAIT_SECONDS (1800 s): the
    release gate waits for pending CI checks on the release commit
    instead of failing on the intermediate state."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    config = runner.load_config(config_path)
    assert config.release_ci_wait_seconds == 1800.0


def test_load_config_reads_explicit_release_ci_wait_seconds(tmp_path):
    """Issue #268: an explicit override is accepted as-is."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrelease_ci_wait_seconds = 900\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.release_ci_wait_seconds == 900.0


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
def test_load_config_rejects_invalid_release_ci_wait_seconds(
    tmp_path, value, reason,
):
    """Issue #268: booleans, zero, negative, NaN/infinity and non-numeric
    values are rejected at config load with the field name and the
    concrete reason."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"release_ci_wait_seconds = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "release_ci_wait_seconds" in str(excinfo.value)
    assert reason in str(excinfo.value)


def test_load_config_defaults_mergeable_wait_seconds(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path).mergeable_wait_seconds == 120.0


def test_load_config_reads_mergeable_wait_seconds(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nmergeable_wait_seconds = 15\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path).mergeable_wait_seconds == 15.0


@pytest.mark.parametrize("value", ["true", "0", "-1", "nan", '"15"'])
def test_load_config_rejects_invalid_mergeable_wait_seconds(tmp_path, value):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"mergeable_wait_seconds = {value}\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mergeable_wait_seconds"):
        runner.load_config(config_path)


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
    assert config.repositories == (
        {
            "name": "pilot",
            "path": (tmp_path / "checkouts" / "pilot").resolve(),
            "github": "owner/pilot",
            "base_branch": "main",
            # Issue #527: the optional repository config path defaults to
            # the single `.github/orbi.toml` location.
            "config_path": ".github/orbi.toml",
        },
        {
            "name": "ceo",
            "path": (tmp_path / "checkouts" / "ceo").resolve(),
            "github": "owner/ceo",
            "base_branch": "develop",
            "config_path": ".github/orbi.toml",
        },
    )


def test_load_config_defaults_repositories_to_empty_list(tmp_path):
    """Issue #134: without a repositories section the config keeps the
    exact single-repo shape (empty registry, all existing keys intact)."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/pilot"]\nrepo_dir = "repo"\n',
        encoding="utf-8",
    )
    config = runner.load_config(config_path)
    assert config.repositories == ()
    assert config.source_repos == ("owner/pilot",)
    assert config.repo_dir == (tmp_path / "repo").resolve()
    assert config.base_branch == "main"


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


def test_validate_config_accepts_existing_files(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt_review = tmp_path / "prompt_review.md"
    skill = tmp_path / "skill.md"
    context = tmp_path / "context.md"
    for path in (prompt, prompt_review, skill, context):
        path.write_text("ok", encoding="utf-8")
    runner.validate_config(runner.RunnerConfig(repo_dir=tmp_path, deploy_home=tmp_path, prompt=prompt, prompt_review=prompt_review, skills=(skill,), context_files=(context,)))


def test_validate_config_requires_review_prompt(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("ok", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="prompt_review.md"):
        runner.validate_config(runner.RunnerConfig(repo_dir=tmp_path, deploy_home=tmp_path, prompt=prompt, prompt_review=tmp_path / "prompt_review.md", skills=(), context_files=()))


def test_validate_config_fails_before_issue_claim_when_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.md"):
        runner.validate_config(runner.RunnerConfig(repo_dir=tmp_path, deploy_home=tmp_path, prompt=tmp_path / "missing.md", prompt_review=tmp_path / "prompt_review.md", skills=(), context_files=()))


def test_validate_config_rejects_missing_repo_dir(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing-repo"):
        runner.validate_config(runner.RunnerConfig(repo_dir=tmp_path / "missing-repo", deploy_home=tmp_path, prompt=tmp_path / "prompt.md", skills=(), context_files=()))


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


def _base_config(tmp_path: Path) -> runner.RunnerConfig:
    """The minimal valid single-repo config (no repositories key)."""
    prompt = tmp_path / "prompt.md"
    prompt_review = tmp_path / "prompt_review.md"
    for path in (prompt, prompt_review):
        path.write_text("ok", encoding="utf-8")
    return runner.RunnerConfig(
        repo_dir=tmp_path,
        # Issue #330: the bootstrap deployment — home == delivery checkout.
        deploy_home=tmp_path,
        prompt=prompt,
        prompt_review=prompt_review,
        skills=(),
        context_files=(),
    )


def test_validate_config_accepts_repository_git_checkout(tmp_path):
    """Issue #134: a registered path that is a real Git checkout passes."""
    checkout = _git_checkout(tmp_path / "pilot")
    config = _base_config(tmp_path)
    config = dataclasses.replace(config, repositories=[{
        "name": "pilot",
        "path": checkout,
        "github": "owner/pilot",
        "base_branch": "main",
    }])
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
    config = dataclasses.replace(config, repositories=[{
        "name": "wt",
        "path": worktree,
        "github": "owner/pilot",
        "base_branch": "main",
    }])
    runner.validate_config(config)


def test_validate_config_rejects_missing_repository_path(tmp_path):
    """Issue #134: a registered path that does not exist is rejected."""
    config = _base_config(tmp_path)
    config = dataclasses.replace(config, repositories=[{
        "name": "pilot",
        "path": tmp_path / "no-such-checkout",
        "github": "owner/pilot",
        "base_branch": "main",
    }])
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
    config = dataclasses.replace(config, repositories=[{
        "name": "plain",
        "path": plain,
        "github": "owner/plain",
        "base_branch": "main",
    }])
    with pytest.raises(ValueError, match="not a git checkout"):
        runner.validate_config(config)
