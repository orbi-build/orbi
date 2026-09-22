"""The `orbi.pi_session` module boundary and agent-dir contract (Issue #1262).

Launching a Pi session is one module: the Runner reaches the launchers
through `orbi.pi_session` and re-exports none of them, and the module
never imports `orbi.runner` back (Constitution Article 3.3). The
launchers keep their exact bodies (Article 3.2: an extraction never
changes behaviour), so these tests pin the boundary and the per-run
agent dir `prepare_pi_agent_dir` materializes.
"""
import ast
import json
import stat
from pathlib import Path

import orbi.pi_session as pi_session
import orbi.runner as runner
from orbi import config as config_domain

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

# The launchers moved out of `runner.py` by Issue #1262.
LAUNCHERS = (
    "run_pi",
    "prepare_pi_agent_dir",
    "run_review",
    "run_ticket_agent",
    "_pi_extension_env",
)


def _module_definitions(name: str) -> set[str]:
    path = PACKAGE_DIR / f"{name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imported_names(tree: ast.Module) -> list[str]:
    """Every imported module path plus `from orbi import X` member."""
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            if module == "orbi":
                imported.extend(alias.name for alias in node.names)
    return imported


def test_pi_session_owns_the_session_launchers():
    assert set(LAUNCHERS) <= _module_definitions("pi_session")


def test_runner_no_longer_defines_the_session_launchers():
    # Acceptance: `grep -n "def run_pi" src/orbi/runner.py` finds nothing.
    assert set(LAUNCHERS).isdisjoint(_module_definitions("runner"))


def test_runner_does_not_re_export_the_moved_names():
    """The four entry points call `pi_session.<name>`: a re-export would
    let a stale `runner.<name>` patch silently stop intercepting."""
    for name in LAUNCHERS:
        assert not hasattr(runner, name), (
            f"orbi.runner must not re-export {name}: the launcher is "
            "reached through `pi_session` (Issue #1262)"
        )


def test_pi_session_never_imports_runner():
    """Constitution Article 3.3: an extracted module never imports
    `runner` back — not at module scope and not inside a function."""
    path = PACKAGE_DIR / "pi_session.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        name for name in _imported_names(tree)
        if name == "runner" or name.startswith("runner.")
    ]
    assert offenders == []
    assert not hasattr(pi_session, "runner")


def test_pi_session_launchers_are_the_runner_call_sites_target():
    """The Runner's call site resolves the launcher on the module
    object — the real indirection a patch must follow."""
    assert runner.pi_session is pi_session
    assert runner.pi_session.run_pi is pi_session.run_pi


# --- the per-run agent dir (the move keeps its exact shape) ----------------


def test_prepare_pi_agent_dir_materializes_the_per_run_agent_dir(
    tmp_path, monkeypatch,
):
    """Acceptance: same contents and permissions after the move.

    `models.json` merges the user's providers with the configured file,
    `settings.json` is a per-run REAL file (never the user's global
    one), `auth.json` stays the symlink to the user's stored auth, the
    user's own agent dir is untouched and no per-run file is
    executable.
    """
    home = tmp_path / "home"
    user_agent = home / ".pi" / "agent"
    user_agent.mkdir(parents=True)
    user_models = {"providers": {"user-provider": {"apiKey": "sk-user"}}}
    user_settings = {"theme": "dark", "httpIdleTimeoutMs": 0}
    (user_agent / "models.json").write_text(
        json.dumps(user_models), encoding="utf-8",
    )
    (user_agent / "settings.json").write_text(
        json.dumps(user_settings), encoding="utf-8",
    )
    (user_agent / "auth.json").write_text(
        json.dumps({"stored": True}), encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
        base_branch="main",
        pi_providers_data={
            "providers": {"run-provider": {"apiKey": "sk-run"}},
        },
        pi_provider="run-provider", pi_model="model-1",
    )

    agent_dir = pi_session.prepare_pi_agent_dir(tmp_path, config)

    assert agent_dir == tmp_path / ".orbi" / "pi-agent"
    assert sorted(entry.name for entry in agent_dir.iterdir()) == [
        "auth.json", "models.json", "settings.json",
    ]
    assert json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8"),
    ) == {"providers": {
        "user-provider": {"apiKey": "sk-user"},
        "run-provider": {"apiKey": "sk-run"},
    }}
    settings_path = agent_dir / "settings.json"
    assert not settings_path.is_symlink()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "defaultProvider": "run-provider",
        "defaultModel": "model-1",
        "enabledModels": ["run-provider/model-1"],
    }
    auth_link = agent_dir / "auth.json"
    assert auth_link.is_symlink()
    assert auth_link.resolve() == (user_agent / "auth.json").resolve()
    # The user's own agent dir is never modified.
    assert json.loads(
        (user_agent / "settings.json").read_text(encoding="utf-8"),
    ) == user_settings
    for name in ("models.json", "settings.json"):
        mode = stat.S_IMODE((agent_dir / name).stat().st_mode)
        assert mode & 0o111 == 0, f"{name} must not be executable: {mode:o}"


def test_prepare_pi_agent_dir_is_none_without_a_provider_file(tmp_path):
    """The unconfigured path keeps the exact pre-#157 shape: no per-run
    agent dir, Pi uses its own."""
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
        base_branch="main",
    )
    assert pi_session.prepare_pi_agent_dir(tmp_path, config) is None
    assert not (tmp_path / ".orbi" / "pi-agent").exists()
