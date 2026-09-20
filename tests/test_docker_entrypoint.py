"""First-start provider configuration for the Docker entrypoint."""
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "3rd/docker/docker-entrypoint.sh"
SETUP_SCRIPT = REPO_ROOT / "3rd/docker/orbi-container-setup.sh"


def stub_path(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "git": "#!/bin/sh\nif [ \"$1\" = -C ] && [ \"$3\" = rev-parse ] && [ \"$4\" = HEAD ]; then exit \"${GIT_REV_PARSE_STATUS:-0}\"; fi\nexit 0\n",
        "gh": "#!/bin/sh\nexit 0\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "useradd": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "id": "#!/bin/sh\nexit 1\n",
        "runuser": "#!/bin/sh\nshift 3\nexec \"$@\"\n",
        "install": "#!/bin/sh\npath=\"${@: -1}\"\nmkdir -p \"$path\"\n",
        "systemd": "#!/bin/sh\nprintf systemd-started > \"$SYSTEMD_MARKER\"\n",
        "tail": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
    for name in ("cat", "chmod", "env", "grep", "ls", "mkdir", "printf"):
        target = shutil.which(name)
        assert target
        (bin_dir / name).symlink_to(target)
    return bin_dir, tmp_path / "systemd-started"


def prepare_root(tmp_path: Path) -> tuple[Path, Path]:
    deploy = tmp_path / "orbi"
    work = tmp_path / "work"
    (deploy / ".git" / "info").mkdir(parents=True, exist_ok=True)
    work.mkdir(exist_ok=True)
    (work / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (work / ".git" / "info" / "exclude").touch()
    return deploy, work


def run_entrypoint(tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess:
    deploy, work = prepare_root(tmp_path)
    bin_dir, marker = stub_path(tmp_path)
    env = {
        "PATH": str(bin_dir),
        "GH_TOKEN": "token",
        "ORBI_SOURCE_REPO": "owner/repo",
        "ORBI_DEPLOY_HOME": str(deploy),
        "ORBI_WORKSPACE": str(work),
        "ORBI_SYSTEMD_BIN": str(bin_dir / "systemd"),
        "ORBI_UV_BIN": str(bin_dir / "uv"),
        "ORBI_SETUP_LOG": str(tmp_path / "setup.log"),
        "SYSTEMD_MARKER": str(marker),
        **extra_env,
    }
    return subprocess.run(
        ["/bin/bash", str(ENTRYPOINT)],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_provider_environment_generates_json_and_toml(tmp_path):
    result = run_entrypoint(
        tmp_path,
        ORBI_PI_PROVIDER="deepseek",
        ORBI_PI_MODEL="deepseek-chat",
        ORBI_PI_BASE_URL="https://api.deepseek.com",
        ORBI_PI_API_KEY="secret-key",
        ORBI_PI_CONTEXT_WINDOW="64000",
        ORBI_PI_MAX_TOKENS="8192",
    )
    assert result.returncode == 0, result.stderr
    deploy = tmp_path / "orbi"
    data = json.loads((deploy / ".orbi/pi-providers.json").read_text())
    provider = data["providers"]["deepseek"]
    assert provider["baseUrl"] == "https://api.deepseek.com"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$PI_API_KEY"
    assert "PI_API_KEY=secret-key" in (deploy / ".orbi/env").read_text()
    assert provider["models"] == [{
        "id": "deepseek-chat", "name": "deepseek-chat",
        "contextWindow": 64000, "maxTokens": 8192,
    }]
    toml = (deploy / "orbi.toml").read_text()
    assert 'pi_providers = ".orbi/pi-providers.json"' in toml
    assert 'pi_provider = "deepseek"' in toml
    assert 'pi_model = "deepseek-chat"' in toml


def test_partial_provider_environment_fails_with_missing_names(tmp_path):
    result = run_entrypoint(
        tmp_path,
        ORBI_PI_PROVIDER="deepseek",
        ORBI_PI_MODEL="deepseek-chat",
    )
    assert result.returncode != 0
    assert "ORBI_PI_BASE_URL" in result.stderr
    assert "ORBI_PI_API_KEY" in result.stderr
    assert "systemd-started" not in result.stdout


def test_missing_provider_environment_prints_hint_and_writes_no_file(tmp_path):
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "model delivery is not configured" in result.stdout
    assert not (tmp_path / "orbi/.orbi/pi-providers.json").exists()
    assert "pi_provider" not in (tmp_path / "orbi/orbi.toml").read_text()


def test_work_checkout_without_head_fails_with_mount_guidance(tmp_path):
    result = run_entrypoint(tmp_path, GIT_REV_PARSE_STATUS="128")
    assert result.returncode != 0
    assert str(tmp_path / "work") in result.stderr
    assert "owner/repo" in result.stderr
    assert "must be a git checkout" in result.stderr
    assert "docker run -v <path>:/work" in result.stderr


def run_setup_script(tmp_path: Path, setup_status: int = 0) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "setup-bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(
        "#!/bin/sh\nif [ \"$1\" = start ]; then exit 0; fi\nexit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "install").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "runuser").write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *' orbi setup '*)\n"
        "    printf 'setup=ok\\n'\n"
        "    printf 'setup_failed reason=test\\n' >&2\n"
        "    exit \"$FAKE_SETUP_STATUS\"\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "id").write_text(
        "#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8"
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    setup_log = tmp_path / "setup.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ORBI_SETUP_LOG": str(setup_log),
        "FAKE_SETUP_STATUS": str(setup_status),
    }
    return subprocess.run(
        ["/bin/bash", str(SETUP_SCRIPT)],
        env=env, capture_output=True, text=True, timeout=30,
    )


def test_entrypoint_starts_stdout_follower_before_systemd():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    follower = '( exec tail -n +1 -F "$SETUP_LOG" ) &'
    assert follower in text
    assert text.index(follower) < text.index('exec "$SYSTEMD_BIN"')


def test_setup_result_is_replayed_to_journal_and_docker_streams(tmp_path):
    result = run_setup_script(tmp_path)
    setup_log = tmp_path / "setup.log"
    assert result.returncode == 0
    assert "setup=ok" in result.stdout
    assert "setup=ok" in setup_log.read_text()
    assert "setup_failed reason=test" in result.stderr
    assert "setup_failed reason=test" in setup_log.read_text()
    setup_log.unlink(missing_ok=True)


def test_failed_setup_status_and_error_are_visible_to_docker_logs(tmp_path):
    result = run_setup_script(tmp_path, setup_status=1)
    setup_log = tmp_path / "setup.log"
    assert result.returncode == 1
    assert "setup_failed reason=test" in setup_log.read_text()
    setup_log.unlink(missing_ok=True)


def test_setup_appends_to_the_fixed_log_path():
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert 'SETUP_LOG="${ORBI_SETUP_LOG:-/run/orbi-setup.log}"' in text
    assert '>> "$SETUP_LOG"' in text
    assert "/proc/1/fd/" not in text
    assert "ORBI_CONTAINER_STDOUT" not in text
    assert "ORBI_CONTAINER_STDERR" not in text


def test_existing_config_and_provider_are_not_overwritten(tmp_path):
    deploy, _ = prepare_root(tmp_path)
    deploy.joinpath("orbi.toml").write_text("existing config\n")
    deploy.joinpath(".orbi").mkdir(exist_ok=True)
    deploy.joinpath(".orbi/pi-providers.json").write_text('{"existing": true}\n')
    before = {
        path: path.read_bytes()
        for path in (deploy / "orbi.toml", deploy / ".orbi/pi-providers.json")
    }
    result = run_entrypoint(
        tmp_path,
        ORBI_PI_PROVIDER="deepseek",
        ORBI_PI_MODEL="deepseek-chat",
        ORBI_PI_BASE_URL="https://api.deepseek.com",
        ORBI_PI_API_KEY="secret-key",
    )
    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in before} == before
