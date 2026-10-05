from orbi import config as config_domain
import pytest

from orbi import runner
import orbi.pi_session as pi_session
from orbi import pi_command
from orbi.delivery_scene import RunContext


def test_load_config_pi_extensions_normalizes_enabled_and_local_source(tmp_path):
    extension = tmp_path / "fixture.mjs"
    extension.write_text("export default () => {}", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        '[[pi_extensions]]\nsource = "npm:fixture@1.2.3"\n'
        'enabled = true\n[pi_extensions.env]\nFIXTURE_TOKEN = "secret"\n'
        '[[pi_extensions]]\nsource = "fixture.mjs"\nenabled = false\n',
        encoding="utf-8",
    )
    loaded = config_domain.load_config(config)
    assert loaded.pi_extensions == (
        {"source": "npm:fixture@1.2.3", "enabled": True,
         "env": {"FIXTURE_TOKEN": "secret"}},
        {"source": str(extension), "enabled": False, "env": {}},
    )


def test_load_config_pi_extensions_rejects_unlocked_or_duplicate_and_conflicting_env(tmp_path):
    cases = [
        ('[[pi_extensions]]\nsource = "npm:fixture"', "pin a version"),
        ('[[pi_extensions]]\nsource = "git:github.com/x/repo"', "pin a ref"),
        ('[[pi_extensions]]\nsource = "npm:fixture@1.0.0"\n[[pi_extensions]]\nsource = "npm:fixture@1.0.0"', "duplicate"),
        ('[[pi_extensions]]\nsource = "npm:a@1.0.0"\n[pi_extensions.env]\nX="1"\n[[pi_extensions]]\nsource = "npm:b@1.0.0"\n[pi_extensions.env]\nX="2"', "conflict"),
    ]
    for body, message in cases:
        path = tmp_path / f"{len(list(tmp_path.iterdir()))}.toml"
        path.write_text('source_repos=["owner/repo"]\n' + body, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            config_domain.load_config(path)


def test_load_config_pi_extensions_rejects_bad_shapes_and_accepts_git_ref(tmp_path):
    bad_values = [
        ("pi_extensions = {}", "array"),
        ("pi_extensions = [1]", "must be a table"),
        ('[[pi_extensions]]\nsource = "missing.mjs"', "does not exist"),
        ('[[pi_extensions]]\nsource = 3', "source"),
        ('[[pi_extensions]]\nsource = "npm:a@1.0.0"\nenabled = "yes"', "enabled"),
        ('[[pi_extensions]]\nsource = "npm:a@1.0.0"\nenv = []', "env"),
        ('[[pi_extensions]]\nsource = "npm:a@1.0.0"\n[pi_extensions.env]\n"BAD-NAME" = "x"', "invalid variable"),
        ('[[pi_extensions]]\nsource = "npm:a@1.0.0"\n[pi_extensions.env]\nX = 3', "must be a string"),
    ]
    for body, message in bad_values:
        path = tmp_path / f"bad-{len(list(tmp_path.iterdir()))}.toml"
        path.write_text('source_repos=["owner/repo"]\n' + body, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            config_domain.load_config(path)
    path = tmp_path / "git.toml"
    path.write_text(
        'source_repos=["owner/repo"]\n[[pi_extensions]]\n'
        'source="git:github.com/example/fixture@v1.2.3"\n',
        encoding="utf-8",
    )
    assert config_domain.load_config(path).pi_extensions[0]["source"].endswith("@v1.2.3")


def test_pi_extension_args_and_env_isolate_disabled_and_secrets():
    config = config_domain.RunnerConfig(pi_extensions=({"source": "npm:fixture@1.2.3", "enabled": True,
         "env": {"FIXTURE_TOKEN": "secret"}},
        {"source": "/tmp/disabled.mjs", "enabled": False,
         "env": {"DISABLED": "no"}},))
    args = pi_command._pi_extension_args(config)
    assert args == ["--no-extensions", "--extension", "npm:fixture@1.2.3"]
    assert pi_session._pi_extension_env(config) == {"FIXTURE_TOKEN": "secret"}
    assert "secret" not in " ".join(args)


def test_run_pi_review_and_ticket_share_extension_contract(monkeypatch, tmp_path):
    (tmp_path / "prompt.md").write_text("system", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("review", encoding="utf-8")
    disabled = tmp_path / "disabled.mjs"
    disabled.write_text("export default () => {}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kw: calls.append((command, kw)) or "ok")
    config = config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc", run_id="deadbeef", pi_extensions=({"source": "npm:fixture@1.2.3", "enabled": True, "env": {"FIXTURE_TOKEN": "secret"}}, {"source": str(disabled), "enabled": False, "env": {}}))
    extension_args = pi_command._pi_extension_args(config)
    assert extension_args == ["--no-extensions", "--extension", "npm:fixture@1.2.3"]
    pi_session.run_pi({"number": 1, "title": "t", "body": ""}, RunContext(run_id=config.run_id, issue={"number": 1, "title": "t", "body": ""}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config)
    pi_session.run_review(RunContext(run_id=config.run_id, issue=1, branch="b", worktree=tmp_path, source_repo="owner/repo"), {"number": 1, "url": "u", "base_oid": "b", "head_oid": "h", "head_ref": "r"}, config, 1)
    for command, kwargs in calls:
        assert command[1:4] == extension_args
        assert kwargs["pi_env"] == {"FIXTURE_TOKEN": "secret"}
        assert "secret" not in " ".join(kwargs["log_command"])
        assert kwargs["log_command"][1:4] == command[1:4]
    assert len(calls) == 2
    # The ticket role (#1558) keeps the --no-tools boundary and passes the
    # SAME isolated extension flags implement/review use.
    pi_session.run_ticket_agent(
        {"number": 1558, "title": "t", "body": ""}, config, "owner/repo",
    )
    command, kwargs = calls[2]
    assert command[:2 + len(extension_args)] == [
        "pi", "--no-tools", *extension_args,
    ]
    assert kwargs["log_command"][:1 + len(extension_args)] == [
        "pi", *extension_args,
    ]
    assert kwargs["pi_env"] == {"FIXTURE_TOKEN": "secret"}
    assert "secret" not in " ".join(kwargs["log_command"])
