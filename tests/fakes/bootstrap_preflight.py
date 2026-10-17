"""Shared scaffolding for the configuration and preflight domain suite (Issue #1647).

Moved verbatim out of `tests/test_bootstrap_runner.py` when the
configuration loading and startup preflight tests moved to
`tests/test_bootstrap_preflight.py`: the unit-drift deployment fixture, the
recording preflight command, the config builders and the provider catalog
fixtures those tests share. Single owner: the domain suite and the remaining
`test_bootstrap_runner.py` scenes (the ticket-kind provider materialization)
import them from here instead of keeping a copy of each.
"""
import dataclasses
import json
from pathlib import Path

from orbi import config as config_domain
from seam import seam


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

    monkeypatch.setattr(seam, "run_command", fake_run)
    return calls


def _model_config(tmp_path, **extra):
    """Build a minimal config for run_pi/run_review with model keys."""
    config = config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="a1b2c3d4")
    return dataclasses.replace(config, **extra)


def _command_model_args(command):
    """Return the (flag, value) pairs for the pi model options."""
    flags = ("--provider", "--model", "--thinking")
    return [
        (item, command[index + 1])
        for index, item in enumerate(command)
        if item in flags
    ]


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
    keys = {"pi_providers": '"pi-providers.json"', **toml_keys}
    toml = 'source_repos = ["owner/repo"]\n' + "\n".join(
        f"{key} = {value}" for key, value in keys.items()
    ) + "\n"
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(toml, encoding="utf-8")
    return config_path


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


def _user_settings(home: Path, settings: dict) -> Path:
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8",
    )
    return agent


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
