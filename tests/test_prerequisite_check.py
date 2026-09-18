"""`orbi check` — the read-only prerequisite gate (Issue #163).

One lightweight executable entry verifies every deployment precondition
from the Issue: Python version, the required commands + the systemd
the scheduler session, `gh auth`, the `pi` CLI, the config file (existence, parse,
validation), per-source-repo access + permission, the git transport,
and the model provider — WITHOUT printing any secret value. The gate is
read-only (no labels, no units, no git mutation, no config creation)
and fails fast on the FIRST missing prerequisite with a structured
`check_failed check=... reason=... fix=... docs=<official-link>` line;
it never raises a traceback at the user.

The checks reuse the setup primitives (REQUIRED_COMMANDS,
COMMAND_INSTALL_HINTS, check_auth, check_repo, check_transport,
model_provider_status) so the gate and `orbi setup` can never disagree
about what a prerequisite is.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from orbi import cli
from orbi import pilot_setup

# The systemd-shape deployment/setup contract, pinned to the systemd
# impl on every host (the conftest fixture documents the seam).
pytestmark = pytest.mark.usefixtures("systemd_scheduler")

REPO_ROOT = Path(__file__).resolve().parent.parent

REPO = "octocat/hello-world"
ORIGIN_URL = f"git@github.com:{REPO}.git"
REPO_VIEW_OK = (
    '{"nameWithOwner":"' + REPO + '",'
    '"viewerPermission":"ADMIN",'
    '"defaultBranchRef":{"name":"main"}}'
)


def make_world(tmp_path: Path) -> Path:
    """A minimal valid deployment layout; returns the deploy home.

    The config lives in a sibling `run` dir, `deploy_home` is explicit
    (so the prompt defaults resolve inside the home like the external
    single-repo mode does) and carries the provider file, the provider
    key env file and the prompt files `validate_config` requires.
    """
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "prompt.md").write_text("prompt\n", encoding="utf-8")
    (home / "prompts" / "prompt_review.md").write_text(
        "review\n", encoding="utf-8")
    (home / "pi-providers.json").write_text(
        json.dumps(pilot_setup.PROVIDER_STARTER), encoding="utf-8")
    (home / ".orbi").mkdir()
    (home / ".orbi" / "env").write_text(
        "PROVIDER_API_KEY=check-test-key\n", encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "orbi.toml").write_text(
        f'source_repos = ["{REPO}"]\n'
        f'deploy_home = "{home}"\n'
        'pi_provider = "openai"\n'
        'pi_model = "your-model"\n'
        f'pi_providers = "{home / "pi-providers.json"}"\n',
        encoding="utf-8",
    )
    return home


def fake_run_factory(state: dict):
    """A run_command double answering the check probes from `state`."""

    def fake_run(command, **kwargs):
        head = list(command)
        if head[:3] == ["systemctl", "--user", "show"]:
            if not state.get("bus_down"):
                return "loaded"
            raise subprocess.CalledProcessError(
                1, command, stderr="Failed to connect to bus",
            )
        if head[:2] == ["gh", "auth"]:
            if state.get("gh_logged_out"):
                raise subprocess.CalledProcessError(
                    4, command, stderr="not logged in",
                )
            return ""
        if head[:3] == ["gh", "repo", "view"]:
            if state.get("repo_missing"):
                raise subprocess.CalledProcessError(
                    1, command, stderr="HTTP 404: Not Found",
                )
            return state.get("repo_view", REPO_VIEW_OK)
        if head[:2] == ["git", "config"]:
            if state.get("no_origin"):
                raise subprocess.CalledProcessError(
                    1, command, stderr="error: key 'remote.origin.url' not found",
                )
            return state.get("origin_url", ORIGIN_URL)
        if head[:2] == ["git", "ls-remote"]:
            if state.get("transport_down"):
                raise subprocess.CalledProcessError(
                    128, command,
                    stderr="git@github.com: Permission denied (publickey).",
                )
            return "abc\tHEAD"
        if head[:2] == ["pi", "--version"]:
            if state.get("pi_broken"):
                raise subprocess.CalledProcessError(
                    1, command,
                    stderr="SyntaxError: Unexpected token 'export'",
                )
            return "0.85.1"
        raise AssertionError(f"unexpected command in check world: {command}")

    return fake_run


@pytest.fixture(autouse=True)
def _commands_found(monkeypatch):
    """The check must not depend on the host's real PATH contents."""
    monkeypatch.setattr(
        pilot_setup.shutil, "which", lambda name: f"/usr/bin/{name}",
    )


@pytest.fixture(autouse=True)
def _isolate_provider_key_env(monkeypatch):
    """`load_config` merges the deploy env file into os.environ
    (setdefault); keep that merge test-local so no key value leaks into
    other tests of the session."""
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)


# --- the individual check steps -----------------------------------------------


def test_unsupported_platform_is_a_platform_finding_with_the_issue_link(
    monkeypatch,
):
    """Issue #849: a machine that is neither Linux nor macOS is a
    `platform` finding carrying the honest limitation message and the
    issue link — never a traceback from the scheduler dispatch."""
    from orbi import scheduler

    def refuse(system=None):
        raise scheduler.UnsupportedPlatformError(
            "orbi has no scheduler support for platform 'FreeBSD': it "
            "runs on Linux (systemd) and macOS (launchd); see "
            + scheduler.ISSUE_URL
        )

    monkeypatch.setattr(scheduler, "detect", refuse)
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            Path("/absent/orbi.toml"), run_command=lambda c, **k: "",
        )
    failure = excinfo.value
    assert failure.check == "platform"
    assert "FreeBSD" in failure.reason
    assert scheduler.ISSUE_URL in failure.docs


def test_python_version_ok_on_the_supported_runtime():
    pilot_setup.check_python_version()


def test_python_version_fails_fast_with_official_link(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.check_python_version()
    failure = excinfo.value
    assert failure.check == "python"
    assert "3.13" in failure.reason and "3.14" in failure.reason
    assert failure.docs == "https://www.python.org/downloads/"


# --- the pi probe (Issue #1079) -------------------------------------------------


def test_pi_check_executes_the_version_probe_and_names_the_node_floor():
    """Pi on the PATH is not enough (Issue #1079): npm does not enforce
    Pi's `engines` floor, so an install on a distro-default Node reports
    success and then every `pi` invocation crashes with a bundle-level
    SyntaxError. The check must execute `pi --version` and its fix must
    name the Node >= 22.19 floor."""
    calls: list[list[str]] = []

    def broken(command, **kwargs):
        calls.append(list(command))
        raise subprocess.CalledProcessError(
            1, command,
            stderr="/usr/lib/node_modules/@earendil-works/pi/dist/"
                   "bundle.js:1\nSyntaxError: Unexpected token 'export'",
        )

    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.check_pi_command(broken)
    assert calls == [["pi", "--version"]]
    failure = excinfo.value
    assert failure.check == "pi"
    assert "22.19" in failure.fix


def test_pi_check_ok_when_the_probe_succeeds():
    assert pilot_setup.check_pi_command(
        lambda command, **kwargs: "0.85.1"
    ) is None


def test_gate_reports_a_broken_pi_with_the_node_floor_in_the_fix_line():
    """The failure path: pi present but not executable → the structured
    `check_failed check=pi` line carries the Node floor in `fix=`."""
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_machine_checks(
            run_command=fake_run_factory({"pi_broken": True}),
        )
    failure = excinfo.value
    assert failure.check == "pi"
    assert "22.19" in failure.fix
    line = pilot_setup.format_check_failure(failure)
    assert line.startswith("check_failed check=pi ")
    assert "22.19" in line


def test_machine_collection_reports_all_independent_failures(monkeypatch):
    """The missing-config path must not stop at the first machine defect."""
    from orbi import scheduler

    monkeypatch.setattr(
        pilot_setup, "check_python_version",
        lambda: (_ for _ in ()).throw(pilot_setup.CheckError(
            "python", "old", "upgrade", "https://docs.python.org",
        )),
    )
    monkeypatch.setattr(
        scheduler, "detect",
        lambda: (_ for _ in ()).throw(
            scheduler.UnsupportedPlatformError("unsupported"),
        ),
    )
    monkeypatch.setattr(pilot_setup.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        pilot_setup, "check_auth",
        lambda _run: (_ for _ in ()).throw(pilot_setup.SetupError("logged out")),
    )

    _, failures = pilot_setup.run_machine_checks(
        run_command=lambda *_args, **_kwargs: "", collect_failures=True,
    )

    assert {failure.check for failure in failures} == {
        "python", "platform", "commands", "gh_auth", "pi",
    }


def test_machine_collection_reports_scheduler_session_failure(monkeypatch):
    from types import SimpleNamespace
    from orbi import scheduler

    sched = SimpleNamespace(
        name="systemd", display="systemd",
        probe_args=lambda _unit: ["systemctl"],
    )
    monkeypatch.setattr(scheduler, "detect", lambda: sched)
    monkeypatch.setattr(pilot_setup.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(pilot_setup, "check_auth", lambda _run: None)

    # Only the scheduler probe fails; the pi version probe (Issue #1079)
    # is a separate check and succeeds here.
    def run(command, **_kwargs):
        if list(command) == ["pi", "--version"]:
            return "0.85.1"
        raise RuntimeError("user bus unavailable")

    _, failures = pilot_setup.run_machine_checks(
        run_command=run,
        collect_failures=True,
    )

    assert [failure.check for failure in failures] == ["systemd_session"]


def test_missing_command_carries_the_install_hint_and_link(monkeypatch):
    monkeypatch.setattr(
        pilot_setup.shutil, "which", lambda name: None if name == "gh" else "/usr/bin/x",
    )
    home = Path("/")
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(home / "orbi.toml", run_command=lambda c, **k: "")
    failure = excinfo.value
    assert failure.check == "commands"
    assert "required command missing: gh" in failure.reason
    assert pilot_setup.COMMAND_INSTALL_HINTS["gh"] in failure.fix
    assert failure.docs == "https://cli.github.com/"


def test_orbi_install_hint_uses_the_selected_interpreter():
    """Issue #861: the actionable `orbi` install hint names the SAME
    interpreter the CLI editable step actually passes (cli_source's
    compatible selection: system python3 when it satisfies the floor,
    uv-provisioned 3.14 otherwise) — never a hardcoded
    /usr/bin/python3 that fails on Ubuntu 24.04."""
    from orbi import cli_source

    hint = pilot_setup.COMMAND_INSTALL_HINTS["orbi"]
    assert f"--python {cli_source.PYTHON_INTERPRETER}" in hint
    assert "/usr/bin/python3" not in hint


def test_user_bus_failure_names_the_session_and_the_systemd_docs(tmp_path):
    state: dict = {"bus_down": True}
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(f'source_repos = ["{REPO}"]\n', encoding="utf-8")
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "systemd_session"
    assert "systemd user session" in failure.reason
    assert failure.docs == (
        "https://www.freedesktop.org/software/systemd/man/systemctl.html"
    )


def test_gh_auth_failure_points_at_gh_auth_login(tmp_path):
    state: dict = {"gh_logged_out": True}
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(f'source_repos = ["{REPO}"]\n', encoding="utf-8")
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "gh_auth"
    assert "gh auth login" in failure.fix
    assert failure.docs == "https://docs.github.com/en/authentication"


def test_missing_pi_cli_fails_with_the_pi_docs_link(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pilot_setup.shutil, "which", lambda name: None if name == "pi" else "/usr/bin/x",
    )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(f'source_repos = ["{REPO}"]\n', encoding="utf-8")
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory({}),
        )
    failure = excinfo.value
    assert failure.check == "pi"
    assert failure.docs == "https://github.com/earendil-works/pi"


def test_missing_config_is_a_structured_finding_not_a_traceback(tmp_path):
    missing = tmp_path / "orbi.toml"
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(missing, run_command=fake_run_factory({}))
    failure = excinfo.value
    assert failure.check == "config"
    assert str(missing) in failure.reason
    assert "orbi setup" in failure.fix
    assert failure.docs == "https://docs.orbi.build/getting-started"


def test_invalid_config_toml_is_a_config_finding(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text("not toml at all [", encoding="utf-8")
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory({}),
        )
    failure = excinfo.value
    assert failure.check == "config"
    assert "invalid orbi.toml" in failure.reason


def test_config_pointing_at_a_missing_deploy_home_fails(tmp_path):
    make_world(tmp_path)
    config_path = tmp_path / "run" / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["{REPO}"]\n'
        f'deploy_home = "{tmp_path / "gone"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory({}),
        )
    failure = excinfo.value
    assert failure.check == "config"
    assert "required path missing" in failure.reason


def test_repo_access_failure_names_the_repo(tmp_path):
    make_world(tmp_path)
    state: dict = {"repo_missing": True}
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            tmp_path / "run" / "orbi.toml", run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "repo_access"
    assert REPO in failure.reason
    assert failure.docs == "https://cli.github.com/"


def test_insufficient_repo_permission_fails(tmp_path):
    make_world(tmp_path)
    state: dict = {
        "repo_view": (
            '{"nameWithOwner":"' + REPO + '",'
            '"viewerPermission":"READ",'
            '"defaultBranchRef":{"name":"main"}}'
        ),
    }
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            tmp_path / "run" / "orbi.toml", run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "repo_access"
    assert "insufficient permission" in failure.reason


def test_transport_mismatch_fails_with_the_ssh_docs_link(tmp_path):
    make_world(tmp_path)
    state: dict = {
        "origin_url": f"https://github.com/{REPO}.git",
    }
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            tmp_path / "run" / "orbi.toml", run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "transport"
    assert failure.docs == (
        "https://docs.github.com/en/authentication/connecting-to-github-with-ssh"
    )


def test_missing_origin_remote_fails_as_a_transport_finding(tmp_path):
    make_world(tmp_path)
    state: dict = {"no_origin": True}
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            tmp_path / "run" / "orbi.toml", run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "transport"
    assert "no origin remote" in failure.reason


def test_check_world_rejects_unexpected_commands():
    """The factory double fails loudly on a command the check flow
    should never run, instead of silently answering it."""
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run_factory({})(["some", "unexpected", "command"])


def test_unreachable_transport_fails(tmp_path):
    make_world(tmp_path)
    state: dict = {"transport_down": True}
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            tmp_path / "run" / "orbi.toml", run_command=fake_run_factory(state),
        )
    failure = excinfo.value
    assert failure.check == "transport"
    assert "ls-remote" in failure.reason


def test_unconfigured_provider_fails_without_leaking_secrets(tmp_path):
    home = make_world(tmp_path)
    # The env file carries a secret; the provider selection is absent
    # from the config, so the provider check must fail WITHOUT ever
    # printing a secret value.
    (home / ".orbi" / "env").write_text(
        "CHECK_TEST_SECRET=supersecret-value\n", encoding="utf-8",
    )
    config_path = tmp_path / "run" / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["{REPO}"]\n'
        f'deploy_home = "{home}"\n',
        encoding="utf-8",
    )
    with pytest.raises(pilot_setup.CheckError) as excinfo:
        pilot_setup.run_checks(
            config_path, run_command=fake_run_factory({}),
        )
    failure = excinfo.value
    assert failure.check == "model_provider"
    assert "supersecret-value" not in failure.reason
    assert "supersecret-value" not in failure.fix
    assert failure.docs.endswith("#configure-the-model-provider")


# --- the success path ----------------------------------------------------------


def test_run_checks_success_reports_every_step(tmp_path):
    make_world(tmp_path)
    lines = pilot_setup.run_checks(
        tmp_path / "run" / "orbi.toml", run_command=fake_run_factory({}),
    )
    assert f"check=repo ok repo={REPO}" in lines
    assert "check=model_provider ok provider=openai" in lines[-2]
    assert "key=PROVIDER_API_KEY=set" in lines[-2]
    assert lines[-1] == "prerequisites=ok checks=12"
    joined = "\n".join(lines)
    assert "check=python ok" in joined
    assert "check=command ok name=git" in joined
    assert "check=systemd_session ok" in joined
    assert "check=gh_auth ok" in joined
    assert "check=pi ok" in joined
    assert "check=config ok" in joined
    assert "check=transport ok" in joined
    assert "supersecret" not in joined


# --- the failure rendering -----------------------------------------------------


def test_format_check_failure_is_one_parseable_line():
    failure = pilot_setup.CheckError(
        "config", "config file not found: /x/orbi.toml",
        "run `orbi setup`", "https://docs.orbi.build/getting-started",
    )
    line = pilot_setup.format_check_failure(failure)
    assert line == (
        "check_failed check=config "
        'reason="config file not found: /x/orbi.toml" '
        'fix="run `orbi setup`" '
        "docs=https://docs.orbi.build/getting-started"
    )


# --- the CLI entry -------------------------------------------------------------


def test_cli_check_success_prints_the_report_and_exits_zero(
    tmp_path, monkeypatch, capsys,
):
    make_world(tmp_path)
    monkeypatch.setattr(
        cli, "run_command", fake_run_factory({}),
    )
    assert cli.main([
        "check", "--config", str(tmp_path / "run" / "orbi.toml"),
    ]) == 0
    out = capsys.readouterr().out
    assert "prerequisites=ok" in out


def test_cli_check_failure_prints_one_structured_line_and_exits_one(
    tmp_path, monkeypatch, capsys,
):
    state: dict = {"gh_logged_out": True}
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(f'source_repos = ["{REPO}"]\n', encoding="utf-8")
    monkeypatch.setattr(
        cli, "run_command", fake_run_factory(state),
    )
    assert cli.main(["check", "--config", str(config_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.startswith("check_failed check=gh_auth ")
    assert "reason=" in err and "fix=" in err and "docs=" in err
    assert "Traceback" not in err


def test_cli_check_missing_config_reports_prerequisites_before_config(
    tmp_path, monkeypatch, capsys,
):
    """A first run reports independent machine failures and the config hint."""
    config_path = tmp_path / "orbi.toml"
    monkeypatch.setattr(cli, "run_command", fake_run_factory({}))
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: None if name in {"gh", "pi"} else "/usr/bin/x",
    )
    assert cli.main(["check", "--config", str(config_path)]) == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0].startswith("check_failed check=commands ")
    assert any(line.startswith("check_failed check=pi ") for line in lines)
    assert lines[-1].startswith("config_not_found path=")
    assert "reason=no Orbi config at this path" in err
    assert "fix=run from the deployment directory" in err
    assert "Traceback" not in err


def test_cli_setup_missing_deployment_path_fails_structured(
    tmp_path, monkeypatch, capsys,
):
    """Issue #163: the PyPI first run — `orbi setup` in a fresh dir with
    the config just created from the shipped example — fails validation
    on a missing deployment path with the structured setup_failed line,
    never a bare FileNotFoundError traceback."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(f'source_repos = ["{REPO}"]\n', encoding="utf-8")
    monkeypatch.setattr(
        cli, "run_command", fake_run_factory({}),
    )
    assert cli.main([
        "setup", "--config", str(config_path),
    ]) == 1
    captured = capsys.readouterr()
    err = captured.err
    assert re.fullmatch(r"setup_failed reason=required path missing: .+", err.strip())
    assert "Traceback" not in err
