#!/usr/bin/env python3
"""The model catalog one Pi session loads, and what its model declares (#157, #2364).

Pi reads the providers of the agent directory it runs with
(`PI_CODING_APP_DIR`'s `models.json`). Orbi merges the user's own agent-dir
catalog with the configured `pi_providers` file (the file wins on an id
collision) and writes the result as the per-run `models.json`
(`orbi.pi_session.prepare_pi_agent_dir`). This module owns that merge, so the
per-run file and the question "does the run's model read images?" can never
disagree.

An image reaches the model only when the catalog entry of the SELECTED model
declares `"input": ["text", "image"]` (Pi's own schema: the default is
`["text"]`, verified against Pi 1.0.0 `docs/models-configuration.md`). With
the default the engine lists an attached image as not viewable instead of
sending a request the model cannot read (Issue #2364).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orbi.config import RunnerConfig

# Pi's own agent directory: the catalog Pi loads when no provider file is
# configured for the run.
USER_AGENT_DIR = Path("~", ".pi", "agent")


def user_models_path() -> Path:
    """The user's own `models.json`, the file Pi itself reads."""
    return Path(os.path.expanduser(USER_AGENT_DIR / "models.json"))


def _user_providers() -> dict:
    """The user's own providers; an unusable file fails fast."""
    path = user_models_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"user agent dir models.json {path} is not valid JSON: {exc}"
        ) from None
    providers = data.get("providers") if isinstance(data, dict) else None
    return providers if isinstance(providers, dict) else {}


def merged_providers(config: RunnerConfig) -> dict:
    """The providers one run's Pi child loads.

    The user agent dir's providers first, then the configured file's: the
    file adds or overrides, exactly what the per-run `models.json` carries.
    """
    merged = dict(_user_providers())
    data = config.pi_providers_data
    if data is not None:
        merged.update(data["providers"])
    return merged


def selected(config: RunnerConfig, *, review: bool = False) -> tuple[str | None, str | None]:
    """The provider and model the run selects, review override included."""
    if review:
        return (
            config.review_pi_provider or config.pi_provider,
            config.review_pi_model or config.pi_model,
        )
    return config.pi_provider, config.pi_model


def _declares_image(declared: object) -> bool:
    return isinstance(declared, list) and "image" in declared


def declares_image_input(config: RunnerConfig, *, review: bool = False) -> bool:
    """Whether the selected model declares image input (Issue #2364).

    A selection the catalog cannot resolve declares nothing: the engine then
    treats images as not viewable rather than assuming the model reads them.
    """
    provider, model = selected(config, review=review)
    if not provider or not model:
        return False
    entry = merged_providers(config).get(provider)
    if not isinstance(entry, dict):
        return False
    overrides = entry.get("modelOverrides")
    declared = None
    if isinstance(overrides, dict) and isinstance(overrides.get(model), dict):
        declared = overrides[model].get("input")
    if declared is not None:
        return _declares_image(declared)
    models = entry.get("models")
    for candidate in models if isinstance(models, list) else ():
        if isinstance(candidate, dict) and candidate.get("id") == model:
            return _declares_image(candidate.get("input"))
    return False
