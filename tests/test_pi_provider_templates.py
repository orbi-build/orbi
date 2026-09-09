"""Contract tests for the committed Issue #309 provider templates."""
import json
from pathlib import Path

import pytest

import orbi.runner as runner


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "pi-providers"
EXPECTED = {
    "gemini": ("google", "gemini-3.8-flash", "GOOGLE_API_KEY"),
    "z-ai": ("z-ai", "glm-5.3-flash", "ZAI_API_KEY"),
    "openrouter": ("openrouter", "google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "deepseek": ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "xai": ("xai", "grok-4.20-0309-reasoning", "XAI_API_KEY"),
    "groq": ("groq", "groq/compound", "GROQ_API_KEY"),
    "local-qwen": ("local-qwen", "Qwen3.8-27B", None),
    "cloudflare-workers-ai": (
        "cloudflare-workers-ai", "@cf/meta/llama-3.1-8b-instruct-fp8", "CLOUDFLARE_API_TOKEN",
    ),
    "github-models": ("github-models", "openai/gpt-4.1-mini", "GH_MODELS_TOKEN"),
}


def load_template(name):
    path = TEMPLATE_DIR / f"{name}.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", EXPECTED)
def test_template_is_a_complete_runner_validated_provider(name, monkeypatch, tmp_path):
    path, data = load_template(name)
    provider_id, model_id, variable = EXPECTED[name]
    assert set(data) == {"providers"}
    assert set(data["providers"]) == {provider_id}
    if variable:
        monkeypatch.setenv(variable, "test-only-key")
    runner._load_pi_providers(
        path, provider_id, model_id, tmp_path / "env",
    )


@pytest.mark.parametrize("name", EXPECTED)
def test_template_selected_model_is_explicit(name):
    _path, data = load_template(name)
    provider_id, model_id, _variable = EXPECTED[name]
    models = data["providers"][provider_id]["models"]
    assert model_id in {model["id"] for model in models}


def test_xai_does_not_retain_the_obsolete_bare_model_id():
    _path, data = load_template("xai")
    model = data["providers"]["xai"]["models"][0]
    assert model["id"] == "grok-4.20-0309-reasoning"
    assert model["contextWindow"] == 1_000_000


def test_gemini_uses_model_default_thinking_mapping():
    _path, data = load_template("gemini")
    model = data["providers"]["google"]["models"][0]
    assert model["thinkingLevelMap"] == {"off": None}
