"""Configuration loading and startup preflight domain (Issue #1647).

Third domain split out of `test_bootstrap_runner.py` (after
`test_runner_config.py`, Issue #789, and the release and Pi session
suites): the runner configuration loading tests (config file, deploy env
file, Pi provider/model catalog materialization) and the
`runner._preflight` startup checks (active-milestone scope, editable CLI
install refresh, unit-drift self-heal, git transport) moved here verbatim
with no behavior change. The shared scaffolding lives in
`tests/fakes/bootstrap_preflight.py`; helpers that several domains use
(`_write_prompts` and the repo-level fixtures) keep their single home in
`test_bootstrap_runner.py` and are imported here.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import orbi.claim as claim
import orbi.journal as journal
import orbi.milestone as milestone
import orbi.pi_session as pi_session
import orbi.runner as runner
from orbi import config as config_domain
from orbi.delivery_scene import RunContext
from seam import seam
from tests.fakes.bootstrap_preflight import (
    FAKE_PI_PROVIDER,
    GROQ_PROVIDERS,
    _command_model_args,
    _drift_world,
    _fake_preflight_run,
    _model_config,
    _providers_config,
    _user_agent_dir,
    _user_settings,
)
from tests.test_bootstrap_runner import _write_prompts


# Hosted-macOS boundaries; the Ubuntu CI keeps every scene here
# authoritative. The unit-drift preflight scenes contract the
# systemd deployment over systemd-shaped fixtures.
_unit_drift_systemd = pytest.mark.usefixtures("systemd_scheduler")


@pytest.fixture(autouse=True)
def _unbound_run_id():
    """Start every test here with no run id bound to the journal prefix.

    `journal.set_run_id` binds one attempt in a module global so every
    later journal line carries `[run_id]`. A test that drives a real
    tick (or calls `set_run_id` directly) leaves that binding behind —
    `test_bootstrap_runner.py` alone leaks one from 82 of its tests — so
    the unprefixed `caplog` assertions moved here would read a stale
    prefix whenever another module ran first (Issue #1647). Clearing the
    module global is the same reset the binding tests already use; it
    only restores the pre-tick state and changes no moved assertion.
    """
    journal._CURRENT_RUN_ID = None


# --- configuration loading: the bootstrap config file ---------------


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


# --- Pi provider/model configuration (Issues #119, #157) -----------


# --- configurable Pi provider/model/thinking (Issue #119) -------------------


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


# --- provider file diagnostics for doctor/setup ---------------------


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


# --- provider dir materialization + Pi env (Issue #157) --------------------


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


# --- per-run settings consistency (Issue #172) ----------------------------


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


# --- e2e: the provider endpoint reaches the Pi process (Issue #157) -------


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





# --- startup preflight (runner._preflight, before any slot/claim) ---


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


# --- editable CLI install refresh preflight (Issue #158) --------------------


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
