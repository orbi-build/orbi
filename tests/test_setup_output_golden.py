"""The byte-pinned `orbi check` / `orbi setup` output contract (Issue #1861).

The config/label scaffolding and the machine/repo checks moved out of
`pilot_setup` into `setup_config` / `setup_checks`. The operator's view
must not change by a single byte: the same `check=<step> ok ...` report,
the same `setup=...` result document and the same structured failure /
repair text (`check_failed ...`, `setup_failed reason=...`).

These tests drive the REAL entry points (`cli.main`, `run_checks`,
`run_setup`, `format_setup`) and compare the exact strings, so a
behaviour drift in any message fails here first.
"""
import json
import subprocess
import sys

import pytest

from orbi import (
    cli,
    cli_commands,
    config as config_domain,
    pilot_setup,
    setup_config,
)
from tests.test_pilot_setup import (
    VALID_DEFS,
    fake_run_factory as setup_run_factory,
    make_config,
    make_repo,
    make_run_state,
)
from tests.test_prerequisite_check import (
    REPO,
    fake_run_factory as check_run_factory,
    make_world,
)

pytestmark = pytest.mark.usefixtures("systemd_scheduler")


@pytest.fixture(autouse=True)
def _commands_found(monkeypatch):
    """The report must not depend on the host's real PATH contents."""
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}",
    )


@pytest.fixture(autouse=True)
def _isolate_provider_key_env(monkeypatch):
    """The config load merges the deploy env file into os.environ
    (setdefault); keep that merge test-local."""
    monkeypatch.delenv("PROVIDER_API_KEY", raising=False)


# --- orbi check ---------------------------------------------------------------


def test_check_success_report_is_byte_identical(tmp_path, monkeypatch, capsys):
    """Every `check=<step> ok ...` line, in order, byte for byte."""
    make_world(tmp_path)
    config_path = tmp_path / "run" / "orbi.toml"
    run_command = check_run_factory({})
    lines = pilot_setup.run_checks(config_path, run_command=run_command)
    assert lines == [
        f"check=python ok version={sys.version_info.major}."
        f"{sys.version_info.minor}.{sys.version_info.micro}",
        "check=command ok name=git",
        "check=command ok name=gh",
        "check=command ok name=uv",
        "check=command ok name=orbi",
        "check=systemd_session ok",
        "check=gh_auth ok",
        "check=pi ok",
        f"check=config ok path={config_path.resolve()}",
        f"check=repo ok repo={REPO}",
        "check=transport ok protocol=ssh "
        f"url=git@github.com:{REPO}.git",
        "check=model_provider ok provider=openai model=your-model "
        "key=PROVIDER_API_KEY=set",
        "prerequisites=ok checks=12",
    ]

    monkeypatch.setattr(cli_commands, "run_command", check_run_factory({}))
    assert cli.main(["check", "--config", str(config_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "\n".join(lines) + "\n"
    assert captured.err == ""


CHECK_FAILURES = [
    (
        {"gh_logged_out": True},
        'check_failed check=gh_auth reason="gh auth status failed (log in '
        "with `gh auth login`): Command '['gh', 'auth', 'status']' "
        'returned non-zero exit status 4." fix="log in with `gh auth '
        'login`" docs=https://docs.github.com/en/authentication\n',
    ),
    (
        {
            "repo_view": json.dumps({
                "nameWithOwner": REPO,
                "viewerPermission": "READ",
                "defaultBranchRef": {"name": "main"},
            }),
        },
        'check_failed check=repo_access reason="insufficient permission for '
        f"{REPO}: viewerPermission='READ' (one of ['ADMIN', 'MAINTAIN', "
        '\'WRITE\'] required to manage labels)" fix="verify the repository '
        "name and your write permission on it (label management needs "
        'WRITE or more)" docs=https://cli.github.com/\n',
    ),
    (
        {"transport_down": True},
        'check_failed check=transport reason="ssh_unreachable: git '
        f"ls-remote git@github.com:{REPO}.git failed: Command '"
        f"['git', 'ls-remote', 'git@github.com:{REPO}.git']' returned "
        "non-zero exit status 128. stderr=git@github.com: Permission denied "
        "(publickey). \u2014 SSH is unavailable (check the SSH key / agent / "
        "network). No HTTPS fallback and no silent skip: fix SSH and retry."
        '" fix="repair the origin remote (the human-run migration entry is '
        '`orbi setup`)" docs=https://docs.github.com/en/authentication/'
        "connecting-to-github-with-ssh\n",
    ),
]


@pytest.mark.parametrize(("state", "expected"), CHECK_FAILURES)
def test_check_failure_lines_are_byte_identical(
    tmp_path, monkeypatch, capsys, state, expected,
):
    make_world(tmp_path)
    monkeypatch.setattr(
        cli_commands, "run_command", check_run_factory(state),
    )
    config_path = tmp_path / "run" / "orbi.toml"
    assert cli.main(["check", "--config", str(config_path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == expected
    assert captured.out == ""


def test_check_provider_failure_line_is_byte_identical(
    tmp_path, monkeypatch, capsys,
):
    """The unconfigured provider is a value-free `model_provider`
    finding whose repair text names the provider file and the env slot."""
    home = make_world(tmp_path)
    (home / ".orbi" / "env").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_commands, "run_command", check_run_factory({}))
    config_path = tmp_path / "run" / "orbi.toml"
    assert cli.main(["check", "--config", str(config_path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        'check_failed check=model_provider reason="model provider not '
        "configured: edit orbi.toml (pi_providers/pi_provider/pi_model), "
        f"fill {home / 'pi-providers.json'} and put the key in "
        f"{home / '.orbi' / 'env'} (PROVIDER_API_KEY)\" fix=\"configure the "
        'provider and the key (values are never printed)" '
        "docs=https://docs.orbi.build/getting-started"
        "#4-configure-the-model-provider\n"
    )


# --- orbi setup ---------------------------------------------------------------


def test_setup_result_lines_are_byte_identical(tmp_path):
    """The `orbi setup` result document, rendered line by line."""
    repo, installed, state = make_run_state(tmp_path)
    run_command, _ = setup_run_factory(state)
    config = config_domain.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=run_command,
    )
    assert pilot_setup.format_setup(result) == [
        "setup=ok version=4 base_branch=main",
        f"cli=installed source={repo / 'src' / 'orbi'}",
        "repo=xqliu/orbi permission=ADMIN default_branch=main labels=14/14",
        'service=installed path=' + str(installed / "orbi@.service")
        + " sha256=cb20e033651429a2c7fc54e6cce789ac72578b8f5605fa1709b8d3298a7b536c",
        'timer=orbi@1.timer enabled active=true next="Thu 2026-08-27 '
        '10:00:00 +08"',
        "checkout=remote=origin branch=main clean=true base_fresh=true "
        "remote_url=git@github.com:xqliu/orbi.git protocol=ssh migrated=false "
        "ssh_reachable=true transport_reachable=true "
        "worktrees_exclude_added=true",
        "model_provider=NOT CONFIGURED \u2014 edit orbi.toml "
        "(pi_providers/pi_provider/pi_model), fill "
        f"{repo / '.orbi' / 'pi-providers.json'} and put the key in "
        f"{repo / '.orbi' / 'env'} (PROVIDER_API_KEY)",
        "model_provider_guide=See https://docs.orbi.build/getting-started"
        "#4-configure-the-model-provider",
        "model_endpoint=optional optional_proxy=healthy "
        "url=http://127.0.0.1:18082/health",
    ]


def test_setup_failed_line_is_byte_identical(tmp_path, monkeypatch, capsys):
    """The CLI's one structured setup failure line."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        f'source_repos = ["{REPO}"]\n', encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_commands, "run_command", check_run_factory({}),
    )
    assert cli.main(["setup", "--config", str(config_path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "setup_failed reason=required path missing: "
        f"{tmp_path / 'prompts' / 'prompt.md'}\n"
    )


# --- the repair messages of the extracted steps ------------------------------


def _raised(call, exc_type):
    with pytest.raises(exc_type) as excinfo:
        call()
    return str(excinfo.value)


def test_config_and_label_repair_messages_are_byte_identical(tmp_path):
    """The config scaffolding's fail-fast text, finding by finding."""
    absent = tmp_path / "absent" / "orbi.toml"
    assert _raised(
        lambda: setup_config.ensure_config(absent),
        pilot_setup.SetupError,
    ) == (
        f"config creation failed for {absent}: [Errno 2] No such file or "
        f"directory: '{absent}'"
    )
    labels = tmp_path / "labels.toml"
    assert _raised(
        lambda: pilot_setup.load_label_defs(labels),
        pilot_setup.SetupError,
    ) == (
        f"label definitions missing: {labels} (the repo-managed single "
        "source of truth for the platform labels)"
    )
    malformed = tmp_path / "malformed.toml"
    malformed.write_text(
        '[[label]]\nname = "ai-ready"\ncolor = "zzz"\n'
        'description = "dispatched"\n',
        encoding="utf-8",
    )
    assert _raised(
        lambda: pilot_setup.load_label_defs(malformed),
        pilot_setup.SetupError,
    ) == (
        f"malformed labels.toml: {malformed}: label ai-ready has an "
        "invalid color (6-character hex required): 'zzz'"
    )


def test_label_alignment_repair_message_is_byte_identical(tmp_path):
    repo = make_repo(tmp_path)

    def failing(command, **kwargs):
        if list(command)[:3] == ["gh", "label", "list"]:
            return "[]"
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    assert _raised(
        lambda: pilot_setup.align_labels(
            "xqliu/orbi",
            pilot_setup.load_label_defs(repo / "labels.toml"),
            run_command=failing,
        ),
        pilot_setup.SetupError,
    ).startswith(
        "label alignment failed for xqliu/orbi: ai-ready "
        "(gh label create failed: Command "
        "'['gh', 'label', 'create', 'ai-ready', '--repo', 'xqliu/orbi', "
        "'--color', '1d76db', '--description', 'dispatched', '--force']' "
        "returned non-zero exit status 1.)",
    )


def test_machine_and_checkout_repair_messages_are_byte_identical(
    tmp_path, monkeypatch,
):
    """The machine/repo/transport check text (a missing command, a repo
    permission, a dirty checkout) stays word for word the repair the
    operator reads."""
    import shutil

    monkeypatch.setattr(
        shutil, "which",
        lambda name: None if name == "gh" else f"/usr/bin/{name}",
    )
    assert _raised(
        lambda: pilot_setup.check_commands(lambda command, **kwargs: ""),
        pilot_setup.SetupError,
    ) == (
        "required command missing: gh (not on PATH) \u2014 install the "
        "GitHub CLI (https://cli.github.com/) and log in with "
        "`gh auth login` (setup verifies the login, it never runs it "
        "for you)"
    )
    assert _raised(
        lambda: pilot_setup.check_repo(
            "xqliu/orbi",
            lambda command, **kwargs: json.dumps({
                "nameWithOwner": "xqliu/orbi",
                "viewerPermission": "READ",
                "defaultBranchRef": {"name": "main"},
            }),
        ),
        pilot_setup.SetupError,
    ) == (
        "insufficient permission for xqliu/orbi: viewerPermission='READ' "
        "(one of ['ADMIN', 'MAINTAIN', 'WRITE'] required to manage labels)"
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def dirty(command, **kwargs):
        head = list(command)
        if head[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if head[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if head[:2] == ["git", "status"]:
            return " M bootstrap_runner.py"
        return ""

    assert _raised(
        lambda: pilot_setup.check_checkout(
            checkout, "main", ["xqliu/orbi"], run_command=dirty,
        ),
        pilot_setup.SetupError,
    ) == (
        f"checkout is not clean: {checkout} (uncommitted changes: "
        "'M bootstrap_runner.py') \u2014 commit or stash them first: the "
        "timer's ExecStartPre fast-forward refuses a dirty worktree, so "
        "the Runner could never start"
    )


def test_the_platform_label_set_is_unchanged(tmp_path):
    """The label alignment side effect: the exact ordered platform set."""
    repo = make_repo(tmp_path)
    defs = pilot_setup.load_label_defs(repo / "labels.toml")
    assert [entry["name"] for entry in defs] == [
        entry["name"] for entry in VALID_DEFS
    ]
