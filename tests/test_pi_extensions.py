import pytest

from orbi import runner


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
    loaded = runner.load_config(config)
    assert loaded["pi_extensions"] == [
        {"source": "npm:fixture@1.2.3", "enabled": True,
         "env": {"FIXTURE_TOKEN": "secret"}},
        {"source": str(extension), "enabled": False, "env": {}},
    ]


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
            runner.load_config(path)


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
            runner.load_config(path)
    path = tmp_path / "git.toml"
    path.write_text(
        'source_repos=["owner/repo"]\n[[pi_extensions]]\n'
        'source="git:github.com/example/fixture@v1.2.3"\n',
        encoding="utf-8",
    )
    assert runner.load_config(path)["pi_extensions"][0]["source"].endswith("@v1.2.3")


def test_pi_extension_args_and_env_isolate_disabled_and_secrets():
    config = {"pi_extensions": [
        {"source": "npm:fixture@1.2.3", "enabled": True,
         "env": {"FIXTURE_TOKEN": "secret"}},
        {"source": "/tmp/disabled.mjs", "enabled": False,
         "env": {"DISABLED": "no"}},
    ]}
    args = runner._pi_extension_args(config)
    assert args == ["--no-extensions", "--extension", "npm:fixture@1.2.3"]
    assert runner._pi_extension_env(config) == {"FIXTURE_TOKEN": "secret"}
    assert "secret" not in " ".join(args)


def test_run_pi_and_review_share_extension_contract(monkeypatch, tmp_path):
    (tmp_path / "prompt.md").write_text("system", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("review", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "stream_pi", lambda command, **kw: calls.append((command, kw)) or "ok")
    config = {
        "prompt": tmp_path / "prompt.md", "prompt_review": tmp_path / "prompt_review.md",
        "repo_dir": tmp_path, "source_repos": ["owner/repo"], "workspace_root": tmp_path,
        "context_files": [], "skills": [], "base_branch": "main", "base_sha": "abc",
        "run_id": "deadbeef", "pi_extensions": [{"source": "npm:fixture@1.2.3", "enabled": True, "env": {"FIXTURE_TOKEN": "secret"}}],
    }
    runner.run_pi({"number": 1, "title": "t", "body": ""}, tmp_path, config, "owner/repo", branch="b")
    runner.run_review(tmp_path, {"number": 1, "url": "u", "base_oid": "b", "head_oid": "h", "head_ref": "r"}, config, "owner/repo", 1, "b", 1)
    for command, kwargs in calls:
        assert command[1:4] == ["--no-extensions", "--extension", "npm:fixture@1.2.3"]
        assert kwargs["pi_env"] == {"FIXTURE_TOKEN": "secret"}
        assert "secret" not in " ".join(kwargs["log_command"])
        assert kwargs["log_command"][1:4] == command[1:4]
