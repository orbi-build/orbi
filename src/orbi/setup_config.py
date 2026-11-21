"""The config, label and model-provider scaffolding of `orbi setup`.

Extracted from :mod:`orbi.pilot_setup` (Issue #1861) so the setup entry
keeps the orchestration and the output contract while this module owns
the files and the GitHub labels a deployment is made of:

- the config file creation from the shipped example (never overwriting);
- the repo-managed `labels.toml` parsing and validation;
- the declarative label alignment (`gh label` create/update, nothing
  deleted);
- the non-secret model-provider starter and the value-free provider
  status used by `orbi setup`, `orbi check` and `orbi doctor`.

The fail-fast :class:`SetupError` lives here: it is this module's error
for a broken config or label declaration and the shared error of the
whole setup domain, raised by :mod:`orbi.setup_checks` and re-used by
:mod:`orbi.pilot_setup`. The import direction is one-way —
pilot_setup -> setup_checks -> setup_config — and this module never
imports `runner` (Constitution Article 3.3).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.resources
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import language
from orbi import labels as label_data

if TYPE_CHECKING:  # annotation-only: the callers import it at runtime
    from orbi.config import RunnerConfig


class SetupError(RuntimeError):
    """A core setup prerequisite or step failed (fail fast)."""


PROVIDER_FILE_NAME = "pi-providers.json"
PROVIDER_ENV_NAME = "PROVIDER_API_KEY"
PROVIDER_STARTER = {
    "_comment": "Edit this OpenAI-compatible provider and select it in orbi.toml.",
    "providers": {
        "openai": {
            "baseUrl": "https://api.openai.com/v1",
            "api": "openai-completions",
            "apiKey": "$PROVIDER_API_KEY",
            "models": [{
                "id": "your-model",
                "name": "Your model",
                "contextWindow": 131072,
                "maxTokens": 16384,
            }],
        },
    },
}
PROVIDER_GUIDE = (
    "See https://docs.orbi.build/getting-started"
    "#4-configure-the-model-provider"
)


# The engine's single source of truth for the platform labels: the file
# shipped in this package (Issue #2347). A deployment copy is never read —
# there is one definition file, and setup aligns the configured repos with
# it exactly like it aligns them with the unit templates.
LABELS_FILE = label_data.LABELS_FILE
COLOR_PATTERN = label_data.COLOR_PATTERN


def packaged_example_bytes() -> bytes:
    """The example config SHIPPED IN THE PACKAGE.

    A PyPI install carries no checkout, so the example travels inside
    the wheel (`src/orbi/example_config.toml`, declared as package
    data). Raises OSError when the install is broken (the file is
    missing from the environment).
    """
    return (
        importlib.resources.files("orbi")
        .joinpath("example_config.toml")
        .read_bytes()
    )


def ensure_config(path: Path) -> Path:
    """Create a local config when absent; never overwrite.

    The ADJACENT `.orbi.example.toml` wins (a deployment home may keep
    its own example); without one the example shipped in the package is
    used (a NEW config seeds `message_language` from the locale, #1660).
    Both unavailable is a broken install: fail fast, no partial config.
    """
    path = Path(path)
    if path.exists():
        return path
    example = path.parent / ".orbi.example.toml"
    if example.is_file():
        payload = example.read_bytes()
    else:
        try:
            payload = packaged_example_bytes()
        except OSError as exc:
            raise SetupError(
                f"example config unavailable for {path}: no adjacent "
                f"{example} and the packaged example failed: {exc}"
            ) from exc
    try:
        path.write_bytes(language.seed_config(payload))
    except OSError as exc:
        raise SetupError(f"config creation failed for {path}: {exc}") from exc
    return path


def load_label_defs(path: Path) -> list[dict]:
    """Parse and validate the label definitions `orbi setup` aligns.

    Delegates to `orbi.labels`: the one validator of the platform label
    data (every role exactly once, exactly one queue role, non-empty
    name/color/description). Any deviation is a fail-fast SetupError: a
    typo'd or missing role would silently skip a delivery state.
    """
    try:
        return label_data.load(Path(path)).label_defs()
    except label_data.LabelsError as exc:
        raise SetupError(f"label definitions invalid: {exc}") from exc


def align_labels(repo: str, defs: list[dict], run_command) -> dict:
    """Declaratively align the repo's platform labels with `defs`.

    Reads the current labels (``gh label list --json name,color,
    description``) and, for every definition, creates the label when
    missing or updates color/description when drifted (``gh label
    create NAME --color C --description D --force`` — verified against
    the CLI help: with ``--force`` the command updates an existing
    label instead of failing). Existing labels whose name, color and
    description already match are left untouched, and labels outside
    the platform set (business labels) are never read-modified or
    deleted. Returns ``{"repo", "aligned", "total"}``.
    """
    raw = run_command([
        "gh", "label", "list", "--repo", repo,
        "--json", "name,color,description", "--limit", "100",
    ])
    try:
        existing = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"cannot parse gh label list output for {repo}: {raw!r}"
        ) from exc
    if not isinstance(existing, list):
        raise SetupError(
            f"cannot parse gh label list output for {repo}: {raw!r}"
        )
    by_name = {
        entry.get("name"): entry
        for entry in existing
        if isinstance(entry, dict)
    }
    pending = []
    for entry in defs:
        current = by_name.get(entry["name"])
        matches = (
            isinstance(current, dict)
            and (current.get("color") or "").lower() == entry["color"].lower()
            and current.get("description") == entry["description"]
        )
        if not matches:
            pending.append(entry)
    def create(entry):
        try:
            run_command([
                "gh", "label", "create", entry["name"], "--repo", repo,
                "--color", entry["color"],
                "--description", entry["description"], "--force"])
        except Exception as exc:
            raise SetupError(
                f"label alignment failed for {repo}: {entry['name']} "
                f"(gh label create failed: {exc})"
            ) from exc
    # Overlap the writes, at most 6 in flight (Issue #1599); `map` yields and fails in definition order.
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(create, pending))
    return {"repo": repo, "aligned": len(defs), "total": len(defs)}


def scaffold_model_config(config: RunnerConfig) -> dict:
    """Create the non-secret provider starter and key slot idempotently."""
    deploy_home = Path(config.deploy_home)
    state_dir = deploy_home / ".orbi"
    state_dir.mkdir(parents=True, exist_ok=True)
    provider_path = config.pi_providers or state_dir / PROVIDER_FILE_NAME
    provider_path = Path(provider_path)
    provider_created = False
    if not provider_path.exists():
        provider_path.write_text(
            json.dumps(PROVIDER_STARTER, indent=2) + "\n", encoding="utf-8",
        )
        provider_created = True
    env_path = state_dir / "env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_created = PROVIDER_ENV_NAME not in {
        line.split("=", 1)[0].removeprefix("export ").strip()
        for line in env_text.splitlines() if "=" in line
    }
    if env_created:
        with env_path.open("a", encoding="utf-8") as handle:
            if env_text and not env_text.endswith("\n"):
                handle.write("\n")
            handle.write(f"# API key for the starter provider\n{PROVIDER_ENV_NAME}=\n")
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    return {
        "provider_file": str(provider_path),
        "provider_created": provider_created,
        "env_file": str(env_path),
        "env_variable": PROVIDER_ENV_NAME,
        "env_created": env_created,
    }


def model_provider_status(config: RunnerConfig) -> dict:
    """Return a safe, value-free provider status for setup and doctor."""
    provider = config.pi_provider
    model = config.pi_model
    path = config.pi_providers
    if path is None:
        path = Path(config.deploy_home) / ".orbi" / PROVIDER_FILE_NAME
    finding = config.pi_provider_key_finding
    env_file = Path(config.deploy_home) / ".orbi" / "env"
    if not provider or not model or not config.pi_providers_data:
        return {
            "state": "NOT CONFIGURED", "provider_file": str(path),
            "env_file": str(env_file), "env_variable": PROVIDER_ENV_NAME,
        }
    if finding:
        return {
            "state": "NOT CONFIGURED", "provider": provider, "model": model,
            "key": f"{finding['variable']}={finding['state']}",
            "provider_file": str(path), "env_file": str(env_file),
            "env_variable": finding["variable"],
        }
    entry = config.pi_providers_data["providers"][provider]
    key = entry.get("apiKey") if isinstance(entry, dict) else None
    variable = (re.fullmatch(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)\}?", key or ""))
    key_name = variable.group(1) if variable else "literal"
    return {
        "state": "ok", "provider": provider, "model": model,
        "key": f"{key_name}=set", "provider_file": str(path),
        "env_file": str(env_file), "env_variable": key_name,
    }
