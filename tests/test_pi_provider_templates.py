"""Contract tests for the committed Issue #309 provider templates."""
import json
from pathlib import Path

import pytest

import orbi.runner as runner


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "pi-providers"
OPENROUTER_FREE_MODELS = {
    "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "dots-studio/dots-3-note-preview:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling-small:free",
    "poolside/laguna-s-2.1:free",
    "thinkingmachines/inkling:free",
    "poolside/laguna-xs-2.1:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "minimax/minimax-m3:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m2.7:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
}

EXPECTED = {
    "gemini": ("google", "gemini-3.8-flash", "GOOGLE_API_KEY"),
    "z-ai": ("z-ai", "glm-5.3-flash", "ZAI_API_KEY"),
    "openrouter": ("openrouter", "google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "deepseek": ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "xai": ("xai", "grok-4.20-0309", "XAI_API_KEY"),
    "local-qwen": ("local-qwen", "Qwen3.8-27B", None),
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


def test_openrouter_template_contains_the_verified_free_catalog():
    _path, data = load_template("openrouter")
    models = data["providers"]["openrouter"]["models"]
    assert {model["id"] for model in models} == OPENROUTER_FREE_MODELS
    assert all(model["id"].endswith(":free") for model in models)


def test_gemini_uses_model_default_thinking_mapping():
    _path, data = load_template("gemini")
    model = data["providers"]["google"]["models"][0]
    assert model["thinkingLevelMap"] == {"off": None}
