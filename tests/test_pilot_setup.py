"""One-time setup tests (Issue #117).

`orbi.py setup` is the config-driven, idempotent, fail-fast
initialization entry: it verifies gh auth and repo permissions, aligns
the platform labels from the repo-managed `labels.toml` (the single
source of truth for name/color/description), installs the repo's
systemd user units, checks the local checkout read-only, and reports the
optional model proxy health as a warning only. Core failures raise
`SetupError` with a concrete reason before any later mutation; the
optional proxy never blocks the core setup.
"""
import json
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner
from orbi import pilot_setup
from orbi import systemd_deploy

@pytest.fixture(autouse=True)
def _default_command_lookup(monkeypatch):
    """Issue #140: the installed `orbi` CLI is a required
    command; the orchestration tests must not depend on whether the
    CLI happens to be installed on the test machine. The dedicated
    `check_commands` tests re-stub `shutil.which` themselves."""
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: f"/usr/bin/{name}",
    )


@pytest.fixture(autouse=True)
def _cli_source_world(monkeypatch, tmp_path):
    """Issue #152: the setup's CLI step checks the RUNNING process's
    `orbi` import source. The test world simulates a CLI
    running from its own checkout (an editable install of
    `make_repo`'s `tmp_path/repo`); a test that needs a drifted
    source re-points `world["module_file"]` before calling
    `run_setup`."""
    import types

    world = {"module_file": tmp_path / "repo" / "src" / "orbi" / "__init__.py"}
    stub = types.SimpleNamespace(
        module_file=lambda: world["module_file"],
        PACKAGE_DIR=Path("src") / "orbi",
        reinstall_args=lambda repo_dir: [
            "uv", "tool", "install", "--force", "--reinstall",
            "--editable", "--python", "/usr/bin/python3", str(repo_dir),
        ],
        reinstall_command=lambda repo_dir: (
            "uv tool install --force --reinstall --editable "
            f"--python /usr/bin/python3 {repo_dir}"
        ),
    )
    monkeypatch.setattr(pilot_setup, "cli_source", stub)
    return world


VALID_DEFS = [
    {"name": "ai-ready", "color": "1d76db", "description": "dispatched"},
    {"name": "ai-in-progress", "color": "fbca04", "description": "work"},
    {"name": "ai-pr-opened", "color": "0e8a16", "description": "pr"},
    {"name": "ai-fix-needed", "color": "fbca04", "description": "fix"},
    {"name": "ai-merged", "color": "0e8a16", "description": "merged"},
    {"name": "ai-blocked", "color": "d73a4a", "description": "blocked"},
    {"name": "p0", "color": "fbca04", "description": "urgent"},
    # Issue #93: the Epic marker is platform state (the claim scan
    # skips `ai-epic`), so it is part of the platform label set.
    {"name": "ai-epic", "color": "bfdadc", "description": "epic"},
    # Issue #98: the Release task marker is platform state (the ready
    # scan routes `ai-ready`+`ai-release` to the release state machine),
    # so it is part of the platform label set.
    {"name": "ai-release", "color": "5319e7", "description": "release"},
    # Issue #209: ticket-only content is a separately dispatched type.
    {"name": "ai-ticket-only", "color": "0e8a16", "description": "ticket"},
]


def write_labels_toml(tmp_path: Path, defs: list[dict]) -> Path:
    lines = []
    for entry in defs:
        lines.append("[[label]]")
        for key in ("name", "color", "description"):
            lines.append(f'{key} = "{entry[key]}"')
        lines.append("")
    path = tmp_path / "labels.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def make_repo(tmp_path: Path) -> Path:
    """A deployment checkout carrying unit templates and labels.toml."""
    repo = tmp_path / "repo"
    systemd = repo / "systemd"
    systemd.mkdir(parents=True)
    (systemd / "orbi@.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n",
        encoding="utf-8",
    )
    (systemd / "orbi@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    write_labels_toml(repo, VALID_DEFS)
    return repo


def make_config(tmp_path: Path, repo_dir: Path,
                source_repos: list[str] | None = None,
                max_concurrency: int = 1) -> Path:
    config = tmp_path / "orbi.toml"
    config.write_text(
        "source_repos = ["
        + ", ".join(f'"{r}"' for r in (source_repos or ["xqliu/orbi"]))
        + f']\nrepo_dir = "{repo_dir}"\nbase_branch = "main"\n'
        + f'max_concurrency = {max_concurrency}\n',
        encoding="utf-8",
    )
    return config


def fake_run_factory(state: dict):
    """A run_command double driven by `state` (a dict of call counters)."""
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        head = list(command)
        if head[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        if head[:2] == ["git", "config"]:
            return state.get(
                "origin_url", "git@github.com:xqliu/orbi.git",
            )
        if head[:2] == ["git", "ls-remote"]:
            if state.get("ssh_down"):
                raise subprocess.CalledProcessError(
                    128, command,
                    stderr="git@github.com: Permission denied (publickey).",
                )
            return "abc\tHEAD"
        if head[:2] == ["git", "remote"]:
            return "origin"
        if head[:3] == ["git", "branch", "--show-current"]:
            return "main"
        if head[:2] == ["git", "status"]:
            return ""
        if head[:3] == ["git", "fetch", "origin"]:
            return ""
        if head[:3] == ["git", "rev-parse", "origin/main"]:
            return "0123456789abcdef0123456789abcdef01234567"
        if head[:2] == ["gh", "auth"]:
            return ""
        if head[:3] == ["gh", "repo", "view"]:
            return (
                '{"nameWithOwner":"xqliu/orbi",'
                '"viewerPermission":"ADMIN",'
                '"defaultBranchRef":{"name":"main"}}'
            )
        if head[:3] == ["gh", "label", "list"]:
            return json.dumps(state.get("existing_labels", []))
        if head[:3] == ["gh", "label", "create"]:
            return ""
        if head[:3] == ["systemctl", "--user", "show"]:
            if "-p" in command and command[command.index("-p") + 1] == "ActiveState":
                unit = command[-1]
                return state.get(
                    f"active_state:{unit}", state.get("active_state", "active"),
                )
            return "loaded"
        if head[:3] == ["systemctl", "--user", "enable"]:
            state[f"is_enabled:{command[-1]}"] = "enabled"
            state[f"active_state:{command[-1]}"] = "active"
            return ""
        if head[:3] == ["systemctl", "--user", "disable"]:
            state[f"is_enabled:{command[-1]}"] = "disabled"
            state[f"active_state:{command[-1]}"] = "inactive"
            return ""
        if head[:3] == ["systemctl", "--user", "is-enabled"]:
            unit = command[-1]
            return state.get(
                f"is_enabled:{unit}", state.get("is_enabled", "enabled"),
            )
        if head[:3] == ["systemctl", "--user", "list-timers"]:
            return state.get(
                "list_timers",
                "NEXT\n"
                "Thu 2026-08-27 10:00:00 +08        1min "
                "Thu 2026-08-27 09:45:00 +08    15min ago "
                "orbi@1.timer                orbi@1.service\n"
                "Fri 2026-08-27 10:05:00 +08        6min "
                "Thu 2026-08-27 10:00:00 +08     5min ago "
                "orbi@2.timer                orbi@2.service\n",
            )
        if head[:1] == ["curl"]:
            if state.get("proxy_down"):
                raise subprocess.CalledProcessError(
                    7, command, stderr="Connection refused",
                )
            return '{"status":"ok"}'
        if head[:2] == ["systemctl", "--user"] and head[2] in (
            "daemon-reload", "enable",
        ):
            return ""
        # Issue #152: the editable tool install (uv tool install ...).
        if head[:2] == ["uv", "tool"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    return fake_run, calls


def test_fake_run_factory_rejects_an_unexpected_command():
    fake_run, _ = fake_run_factory({})
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["definitely", "not", "a", "known", "command"])


# --- labels.toml: the single source of truth -------------------------------


def test_load_label_defs_parses_all_ten_platform_labels(tmp_path):
    path = write_labels_toml(tmp_path, VALID_DEFS)
    defs = pilot_setup.load_label_defs(path)
    assert [entry["name"] for entry in defs] == [
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked", "p0", "ai-epic",
        "ai-release", "ai-ticket-only",
    ]
    assert defs[0] == {
        "name": "ai-ready", "color": "1d76db", "description": "dispatched",
    }


def test_load_label_defs_rejects_a_missing_platform_label(tmp_path):
    defs = [entry for entry in VALID_DEFS if entry["name"] != "p0"]
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="p0"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_an_unknown_label(tmp_path):
    defs = VALID_DEFS + [
        {"name": "ai-unknown", "color": "bfdadc", "description": "x"},
    ]
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="ai-unknown"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_duplicate_names(tmp_path):
    defs = VALID_DEFS + [dict(VALID_DEFS[0])]
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="duplicate"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_a_bad_color(tmp_path):
    defs = [dict(entry) for entry in VALID_DEFS]
    defs[0]["color"] = "red"
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="color"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_an_empty_description(tmp_path):
    defs = [dict(entry) for entry in VALID_DEFS]
    defs[0]["description"] = ""
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="description"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_a_missing_file(tmp_path):
    with pytest.raises(pilot_setup.SetupError, match="labels.toml"):
        pilot_setup.load_label_defs(tmp_path / "labels.toml")


def test_load_label_defs_rejects_malformed_toml(tmp_path):
    path = tmp_path / "labels.toml"
    path.write_text("label = 3\n", encoding="utf-8")
    with pytest.raises(pilot_setup.SetupError, match="labels.toml"):
        pilot_setup.load_label_defs(path)


def test_committed_labels_toml_covers_the_ten_platform_labels():
    """The committed labels.toml (repo root) must define exactly the 10
    platform labels with valid colors and non-empty descriptions."""
    path = Path(__file__).resolve().parent.parent / "labels.toml"
    defs = pilot_setup.load_label_defs(path)
    assert [entry["name"] for entry in defs] == list(
        pilot_setup.REQUIRED_LABELS,
    )


# --- prerequisites: commands and auth ---------------------------------------


def test_check_commands_reports_the_required_commands(monkeypatch):
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: f"/usr/bin/{name}",
    )
    result = pilot_setup.check_commands(
        run_command=lambda command, **kwargs: "loaded",
    )
    assert result == {
        "git": "/usr/bin/git",
        "gh": "/usr/bin/gh",
        "python3": "/usr/bin/python3",
        # Issue #156: setup calls `uv tool install` in the CLI editable
        # step (Issue #152), so uv is a checked prerequisite too.
        "uv": "/usr/bin/uv",
        # Issue #140: the installed CLI is a prerequisite too (the
        # systemd entry it documents must exist on the machine).
        "orbi": "/usr/bin/orbi",
        "systemctl": "user-bus-ok",
    }


def test_check_commands_fails_fast_on_a_missing_command(monkeypatch):
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: None if name == "gh" else f"/usr/bin/{name}",
    )
    with pytest.raises(pilot_setup.SetupError, match="gh"):
        pilot_setup.check_commands(
            run_command=lambda command, **kwargs: "loaded",
        )


def test_check_commands_fails_fast_when_uv_is_missing(monkeypatch):
    """Issue #156: a machine with the CLI but no uv used to fail only in
    the later CLI editable install step with an indirect error. The
    prerequisite check must fail fast at the commands step, name uv and
    carry actionable install guidance (the official uv installer, verified
    against https://docs.astral.sh/uv/getting-started/installation/)."""
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: None if name == "uv" else f"/usr/bin/{name}",
    )
    with pytest.raises(pilot_setup.SetupError) as excinfo:
        pilot_setup.check_commands(
            run_command=lambda command, **kwargs: "loaded",
        )
    reason = str(excinfo.value)
    assert "uv" in reason
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in reason
    assert "https://docs.astral.sh/uv/" in reason


def test_check_commands_fails_fast_without_a_user_bus(monkeypatch):
    monkeypatch.setattr(
        pilot_setup.shutil, "which",
        lambda name: f"/usr/bin/{name}",
    )
    with pytest.raises(
        pilot_setup.SetupError, match="user bus",
    ):
        pilot_setup.check_commands(
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(
                        1, command,
                        stderr="Failed to connect to user scope bus",
                    )
                )
            ),
        )


def test_check_auth_passes_when_gh_is_logged_in():
    pilot_setup.check_auth(run_command=lambda command, **kwargs: "")
    # no exception: logged in


def test_check_auth_fails_fast_when_gh_is_not_logged_in():
    with pytest.raises(pilot_setup.SetupError, match="gh auth"):
        pilot_setup.check_auth(
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(
                        1, command, stderr="not logged in",
                    )
                )
            ),
        )


# --- repo permission check ---------------------------------------------------


def test_check_repo_reports_permission_and_default_branch():
    result = pilot_setup.check_repo(
        "xqliu/orbi",
        run_command=lambda command, **kwargs: (
            json.dumps({
                "nameWithOwner": "xqliu/orbi",
                "viewerPermission": "WRITE",
                "defaultBranchRef": {"name": "main"},
            })
        ),
    )
    assert result == {
        "repo": "xqliu/orbi",
        "permission": "WRITE",
        "default_branch": "main",
    }


def test_check_repo_fails_fast_on_a_read_only_repo():
    with pytest.raises(pilot_setup.SetupError, match="permission"):
        pilot_setup.check_repo(
            "xqliu/orbi",
            run_command=lambda command, **kwargs: (
                json.dumps({
                    "nameWithOwner": "xqliu/orbi",
                    "viewerPermission": "READ",
                    "defaultBranchRef": {"name": "main"},
                })
            ),
        )


def test_check_repo_fails_fast_when_the_repo_is_missing():
    with pytest.raises(pilot_setup.SetupError, match="no-such"):
        pilot_setup.check_repo(
            "nobody/no-such",
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(
                        1, command,
                        stderr="Could not resolve to a Repository",
                    )
                )
            ),
        )


def test_check_repo_fails_fast_on_unparseable_output():
    with pytest.raises(pilot_setup.SetupError, match="parse"):
        pilot_setup.check_repo(
            "xqliu/orbi",
            run_command=lambda command, **kwargs: "not-json",
        )


# --- label alignment ---------------------------------------------------------


def test_align_labels_creates_missing_and_edits_drifted(tmp_path):
    state = {"existing_labels": [
        dict(entry) for entry in VALID_DEFS
    ] + [
        {"name": "bug", "color": "d73a4a",
         "description": "Something isn't working"},
    ]}
    # p0 is drifted (color and description), ai-ready already matches.
    state["existing_labels"] = [
        entry
        for entry in state["existing_labels"]
        if entry["name"] != "p0"
    ] + [
        {"name": "p0", "color": "000000", "description": "old"},
    ]
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.align_labels(
        "xqliu/orbi",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["repo"] == "xqliu/orbi"
    assert result["aligned"] == 10
    assert result["total"] == 10
    # Only the drifted p0 label is written; ai-ready already matches and
    # the business label `bug` is never touched.
    creates = [c for c in calls if c[:3] == ["gh", "label", "create"]]
    assert len(creates) == 1
    assert creates[0][:3] == ["gh", "label", "create"]
    assert creates[0][3] == "p0"
    assert "--force" in creates[0]
    assert "--color" in creates[0]
    assert "--description" in creates[0]
    assert "--repo" in creates[0]


def test_align_labels_is_idempotent_when_everything_matches(tmp_path):
    state = {"existing_labels": [dict(entry) for entry in VALID_DEFS]}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.align_labels(
        "xqliu/orbi",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["aligned"] == 10
    assert [c for c in calls if c[:3] == ["gh", "label", "create"]] == []


def test_align_labels_reports_partial_alignment(tmp_path):
    state = {"existing_labels": []}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.align_labels(
        "xqliu/orbi",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["aligned"] == 10
    assert result["total"] == 10
    assert len([c for c in calls if c[:3] == ["gh", "label", "create"]]) == 10


def test_align_labels_fails_fast_on_a_label_write_error(tmp_path):
    state = {"existing_labels": []}
    fake_run, calls = fake_run_factory(state)

    def failing(command, **kwargs):
        if command[:3] == ["gh", "label", "create"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="boom",
            )
        return fake_run(command, **kwargs)

    repo = make_repo(tmp_path)
    with pytest.raises(pilot_setup.SetupError, match="label"):
        pilot_setup.align_labels(
            "xqliu/orbi",
            pilot_setup.load_label_defs(repo / "labels.toml"),
            run_command=failing,
        )


# --- units -------------------------------------------------------------------


def test_install_units_step_reports_install_state(tmp_path):
    state = {}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    installed = tmp_path / "units"
    result = pilot_setup.install_units_step(
        repo, installed, max_concurrency=1, run_command=fake_run,
    )
    assert result["service"]["installed"] is True
    assert result["service"]["installed_path"] == str(
        installed / "orbi@.service",
    )
    assert result["service"]["sha256"] == systemd_deploy.sha256_hex(
        installed / "orbi@.service",
    )
    # Issue #189: setup follows the configured default capacity (one).
    instances = result["timer"]["instances"]
    assert sorted(instances) == sorted(systemd_deploy.TIMER_INSTANCES)
    assert instances["orbi@1.timer"]["enabled"] is True
    assert instances["orbi@1.timer"]["active"] is True
    assert instances["orbi@2.timer"]["enabled"] is False
    assert instances["orbi@2.timer"]["active"] is False
    assert instances["orbi@1.timer"]["next"] == (
        "Thu 2026-08-27 10:00:00 +08"
    )
    assert instances["orbi@2.timer"]["next"] == (
        "Fri 2026-08-27 10:05:00 +08"
    )
    # The install itself is the systemd_deploy idempotent install.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl", "--user", "enable", "--now", "orbi@1.timer",
    ] in calls
    assert [
        "systemctl", "--user", "disable", "--now", "orbi@2.timer",
    ] in calls


def test_install_units_step_reports_the_disabled_surplus_timer(tmp_path):
    state = {}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", max_concurrency=1, run_command=fake_run,
    )
    instances = result["timer"]["instances"]
    assert instances["orbi@1.timer"]["enabled"] is True
    assert instances["orbi@1.timer"]["active"] is True
    assert instances["orbi@2.timer"]["enabled"] is False
    assert instances["orbi@2.timer"]["active"] is False
    assert [
        "systemctl", "--user", "disable", "--now", "orbi@2.timer",
    ] in calls


def test_install_units_step_reports_a_missing_next_trigger(tmp_path):
    state = {"list_timers": "NEXT\n"}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    instances = result["timer"]["instances"]
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert instances[instance]["next"] == "-"


def test_install_units_step_fails_fast_on_an_install_error(tmp_path):
    repo = make_repo(tmp_path)

    def failing(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "enable"]:
            raise subprocess.CalledProcessError(1, command, stderr="no bus")
        return ""

    with pytest.raises(pilot_setup.SetupError, match="units"):
        pilot_setup.install_units_step(
            repo, tmp_path / "units", run_command=failing,
        )


def test_timer_next_trigger_parses_the_list_timers_row():
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "orbi@1.timer                orbi@1.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "orbi@1.timer",
    ) == "Thu 2026-08-27 10:00:00 +08"


def test_timer_next_trigger_returns_dash_when_the_timer_is_absent():
    assert pilot_setup.timer_next_trigger(
        "NEXT\n", "orbi@1.timer",
    ) == "-"


def test_timer_next_trigger_ignores_other_timers():
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "other.timer                         other.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "orbi@1.timer",
    ) == "-"


def test_timer_next_trigger_distinguishes_the_two_instances():
    # Issue #149: the two instances have independent next triggers.
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "orbi@1.timer                orbi@1.service\n"
        "Fri 2026-08-27 10:05:00 +08        6min 46s "
        "Thu 2026-08-27 10:00:00 +08     5min ago "
        "orbi@2.timer                orbi@2.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "orbi@1.timer",
    ) == "Thu 2026-08-27 10:00:00 +08"
    assert pilot_setup.timer_next_trigger(
        raw, "orbi@2.timer",
    ) == "Fri 2026-08-27 10:05:00 +08"


# --- checkout check (read-only) ----------------------------------------------


def test_check_checkout_reports_remote_branch_clean_and_fresh(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "rev-parse", "origin/main"]:
            return "0123456789abcdef0123456789abcdef01234567"
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        if command[:3] == ["git", "branch", "--show-current"]:
            return "main"
        return ""

    result = pilot_setup.check_checkout(
        repo, "main", ["xqliu/orbi"], run_command=fake_run,
    )
    assert result == {
        "remote": "origin",
        "branch": "main",
        "clean": True,
        "base_fresh": True,
        "remote_url": "git@github.com:xqliu/orbi.git",
        "remote_protocol": "ssh",
        "migrated": False,
        "ssh_reachable": True,
    }


def test_check_checkout_fails_fast_on_a_dirty_checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:2] == ["git", "status"]:
            return " M bootstrap_runner.py"
        return ""

    with pytest.raises(pilot_setup.SetupError, match="not clean"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"], run_command=fake_run,
        )


def test_check_checkout_reports_a_stale_base(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "rev-parse", "origin/main"]:
            return "1111111111111111111111111111111111111111"
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "2222222222222222222222222222222222222222"
        return ""

    result = pilot_setup.check_checkout(
        repo, "main", ["xqliu/orbi"], run_command=fake_run,
    )
    assert result["base_fresh"] is False


def test_check_checkout_fails_fast_without_an_origin_remote(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="remote"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"],
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(1, command, stderr="no remote")
                )
            ),
        )


def test_check_checkout_fails_fast_on_a_git_error(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="checkout"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"],
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(1, command, stderr="boom")
                )
            ),
        )


# --- git transport in the checkout check (Issue #114) -------------------------


def test_check_checkout_migrates_an_https_remote_to_ssh(tmp_path):
    """Issue #114: the setup entry is the human-authorized migration
    path: an existing HTTPS `origin` is rewritten to the SSH URL of
    the first configured source repo with the plain
    `git remote set-url origin <ssh-url>` (never a remote read from a
    comment or Issue)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = {"url": "https://github.com/xqliu/orbi.git"}

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return state["url"]
        if command[:3] == ["git", "remote", "set-url"]:
            state["url"] = command[4]
            return ""
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "branch", "--show-current"]:
            return "main"
        return ""

    result = pilot_setup.check_checkout(
        repo, "main", ["xqliu/orbi"], run_command=fake_run,
    )
    assert state["url"] == "git@github.com:xqliu/orbi.git"
    assert result["remote_url"] == "git@github.com:xqliu/orbi.git"
    assert result["remote_protocol"] == "ssh"
    assert result["migrated"] is True
    assert result["ssh_reachable"] is True


def test_check_checkout_fails_fast_when_ssh_is_unreachable(tmp_path):
    """Issue #114: a failed SSH probe fails the setup with the
    structured reason (the exact probe command and git stderr) — no
    HTTPS fallback, no silent skip."""
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if command[:2] == ["git", "ls-remote"]:
            raise subprocess.CalledProcessError(
                128, command,
                stderr="git@github.com: Permission denied (publickey).",
            )
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(
        pilot_setup.SetupError, match="ssh_unreachable",
    ) as exc:
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git ls-remote git@github.com:xqliu/orbi.git" in message
    assert "Permission denied (publickey)" in message
    # The fake is strict: an unexpected command fails loudly.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "worktree", "add"])


def test_check_checkout_fails_fast_on_a_generic_git_error(tmp_path):
    """A non-git failure of a checkout command (e.g. a spawn error)
    fails the setup with the checkout reason — never a guessed state."""
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/orbi.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "branch", "--show-current"]:
            raise OSError("spawn failed")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(pilot_setup.SetupError, match="checkout"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"], run_command=fake_run,
        )
    # The fake is strict: an unexpected command fails loudly.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "worktree", "add"])


def test_check_checkout_fails_fast_on_a_remote_repo_mismatch(tmp_path):
    """Issue #114: the checkout's remote must match the configured
    source repo — a clone of a different repo fails fast with both
    URLs (no migration of a mismatching remote)."""
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:other/repo.git"
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(pilot_setup.SetupError, match="mismatch"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"], run_command=fake_run,
        )
    # The fake is strict: an unexpected command fails loudly.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "worktree", "add"])


# --- optional model proxy (never blocks) --------------------------------------


def test_check_optional_proxy_reports_healthy():
    result = pilot_setup.check_optional_proxy(
        run_command=lambda command, **kwargs: '{"status":"ok"}',
    )
    assert result == {
        "optional": True,
        "proxy": "healthy",
        "url": pilot_setup.OPTIONAL_PROXY_URL,
    }


def test_check_optional_proxy_reports_unhealthy_without_raising():
    result = pilot_setup.check_optional_proxy(
        run_command=lambda command, **kwargs: (
            (_ for _ in ()).throw(
                subprocess.CalledProcessError(7, command, stderr="refused")
            )
        ),
    )
    assert result["proxy"] == "unhealthy"
    assert result["optional"] is True


def test_check_optional_proxy_reports_a_missing_curl_without_raising():
    result = pilot_setup.check_optional_proxy(
        run_command=lambda command, **kwargs: (
            (_ for _ in ()).throw(FileNotFoundError("curl"))
        ),
    )
    assert result["proxy"] == "unavailable"
    assert result["optional"] is True


# --- CLI editable install (Issue #152) ----------------------------------------


def test_install_cli_step_verifies_an_existing_editable_install(tmp_path):
    """The running CLI already imports `orbi` from the
    configured checkout (the editable install): setup verifies it
    WITHOUT any uv call (idempotent re-run, no per-setup reinstall)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    calls: list = []
    result = pilot_setup.install_cli_step(
        repo, repo / "src" / "orbi" / "__init__.py",
        run_command=lambda command, **kwargs: calls.append(command) or "",
    )
    assert calls == []
    assert result == {
        "action": "verified",
        "source": str((repo / "src" / "orbi" / "__init__.py").resolve()),
    }


def test_install_cli_step_installs_the_editable_tool_when_drifted(tmp_path):
    """A non-editable (site-packages) or stale source triggers the
    EXACT editable force reinstall from the configured checkout
    (the flags verified against the real `uv tool install --help`:
    `--force`, `--reinstall`, `--editable`, `--python`)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    calls: list = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return ""

    result = pilot_setup.install_cli_step(
        repo,
        "/home/u/.local/share/uv/tools/orbi/"
        "lib/python3.14/site-packages/orbi.py",
        run_command=fake_run,
    )
    assert calls == [[
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", "/usr/bin/python3", str(repo),
    ]]
    assert result == {
        "action": "installed",
        "source": str((repo / "src" / "orbi").resolve()),
    }


def test_install_cli_step_fails_fast_on_a_failed_reinstall(tmp_path):
    """A failing `uv tool install` fails the setup with the concrete
    reason (fail fast, no fallback, no half-initialized state)."""
    repo = tmp_path / "checkout"
    repo.mkdir()

    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="uv boom",
        )

    with pytest.raises(
        pilot_setup.SetupError, match="editable tool install",
    ):
        pilot_setup.install_cli_step(
            repo, "/elsewhere/orbi.py", run_command=failing,
        )


# --- run_setup orchestration ---------------------------------------------------


def make_run_state(tmp_path: Path):
    repo = make_repo(tmp_path)
    installed = tmp_path / "units"
    state = {
        "existing_labels": [dict(entry) for entry in VALID_DEFS],
    }
    return repo, installed, state


def test_run_setup_success_reports_all_steps(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed,
        run_command=fake_run,
    )
    assert result["setup"] == "ok"
    assert result["version"] == pilot_setup.SETUP_VERSION
    assert result["base_branch"] == "main"
    assert result["repos"] == [
        {
            "repo": "xqliu/orbi",
            "permission": "ADMIN",
            "default_branch": "main",
            "labels": {"aligned": 10, "total": 10},
        },
    ]
    assert result["service"]["installed"] is True
    # Issue #189: setup reports the configured timer set, not merely
    # the fixed template instance list.
    instances = result["timer"]["instances"]
    assert sorted(instances) == sorted(systemd_deploy.TIMER_INSTANCES)
    assert instances["orbi@1.timer"]["enabled"] is True
    assert instances["orbi@1.timer"]["active"] is True
    assert instances["orbi@2.timer"]["enabled"] is False
    assert instances["orbi@2.timer"]["active"] is False
    assert result["checkout"]["clean"] is True
    assert result["checkout"]["base_fresh"] is True
    assert result["optional_proxy"]["optional"] is True
    # Issue #152: the CLI step reports the editable install verified
    # (the test process imports orbi from the repo world).
    assert result["cli"]["action"] == "verified"
    assert result["cli"]["source"] == str(
        (repo / "src" / "orbi" / "__init__.py").resolve(),
    )
    # No uv call: an existing editable install is never reinstalled.
    assert [c for c in calls if c[:2] == ["uv", "tool"]] == []


def test_run_setup_capacity_two_enables_both_timers(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo, max_concurrency=2))
    result = pilot_setup.run_setup(config, installed, run_command=fake_run)
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert result["timer"]["instances"][instance]["enabled"] is True
        assert result["timer"]["instances"][instance]["active"] is True
        assert ["systemctl", "--user", "enable", "--now", instance] in calls
    assert not any(command[2] == "disable" for command in calls
                   if command[:2] == ["systemctl", "--user"])


def test_run_setup_repo_override_limits_the_target(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(
        tmp_path, repo,
        source_repos=["xqliu/orbi", "xqliu/muyan-ceo"],
    ))
    result = pilot_setup.run_setup(
        config, installed,
        repos=["xqliu/orbi"],
        run_command=fake_run,
    )
    assert [entry["repo"] for entry in result["repos"]] == [
        "xqliu/orbi",
    ]
    views = [
        c for c in calls if c[:3] == ["gh", "repo", "view"]
    ]
    assert len(views) == 1


def test_run_setup_rejects_a_repo_outside_the_config(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="--repo"):
        pilot_setup.run_setup(
            config, installed,
            repos=["other/repo"],
            run_command=lambda command, **kwargs: "",
        )


def test_run_setup_fails_fast_on_auth_before_any_mutation(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)

    def failing(command, **kwargs):
        if command[:2] == ["gh", "auth"]:
            raise subprocess.CalledProcessError(1, command, stderr="nope")
        return fake_run(command, **kwargs)

    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="gh auth"):
        pilot_setup.run_setup(
            config, installed, run_command=failing,
        )
    # No label mutation, no unit install happened before the failure.
    assert [c for c in calls if c[:3] == ["gh", "label", "create"]] == []
    assert [c for c in calls if c[:3] == ["systemctl", "--user", "daemon-reload"]] == []


def test_run_setup_fails_fast_on_a_label_error_before_units(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    state["existing_labels"] = []
    fake_run, calls = fake_run_factory(state)

    def failing(command, **kwargs):
        if command[:3] == ["gh", "label", "create"]:
            raise subprocess.CalledProcessError(1, command, stderr="boom")
        return fake_run(command, **kwargs)

    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="label"):
        pilot_setup.run_setup(
            config, installed, run_command=failing,
        )
    assert [c for c in calls if c[:3] == ["systemctl", "--user", "daemon-reload"]] == []


def test_run_setup_installs_the_editable_cli_when_drifted(tmp_path,
                                                        _cli_source_world):
    """Issue #152: a non-editable (site-packages) or stale CLI source
    is reinstalled with the editable force reinstall BEFORE any unit
    install — the #152 deadlock was that the old CLI could never run
    the new migration code, so the source fix must precede the unit
    work."""
    repo, installed, state = make_run_state(tmp_path)
    _cli_source_world["module_file"] = (
        "/home/u/.local/share/uv/tools/orbi/"
        "lib/python3.14/site-packages/orbi.py"
    )
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=fake_run,
    )
    assert result["setup"] == "ok"
    assert result["cli"]["action"] == "installed"
    assert result["cli"]["source"] == str(
        (repo / "src" / "orbi").resolve(),
    )
    uv_calls = [c for c in calls if c[:2] == ["uv", "tool"]]
    assert uv_calls == [[
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", "/usr/bin/python3", str(repo),
    ]]
    # The reinstall precedes the unit install (daemon-reload).
    uv_index = calls.index(uv_calls[0])
    reload_index = next(
        i for i, c in enumerate(calls)
        if c[:3] == ["systemctl", "--user", "daemon-reload"]
    )
    assert uv_index < reload_index


def test_run_setup_fails_fast_on_a_failed_cli_reinstall(
    tmp_path, _cli_source_world,
):
    """Issue #152: a failing editable reinstall fails the setup
    before any unit install (fail fast, no half-initialized state)."""
    repo, installed, state = make_run_state(tmp_path)
    _cli_source_world["module_file"] = "/elsewhere/orbi.py"
    fake_run, calls = fake_run_factory(state)

    def failing(command, **kwargs):
        if command[:2] == ["uv", "tool"]:
            raise subprocess.CalledProcessError(
                1, command, stderr="uv boom",
            )
        return fake_run(command, **kwargs)

    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="editable tool install"):
        pilot_setup.run_setup(
            config, installed, run_command=failing,
        )
    assert [c for c in calls if c[:3] == ["systemctl", "--user", "daemon-reload"]] == []


def test_run_setup_optional_proxy_failure_never_blocks(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    state["proxy_down"] = True
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=fake_run,
    )
    assert result["setup"] == "ok"
    assert result["optional_proxy"]["proxy"] == "unhealthy"


def test_run_setup_migrates_an_https_checkout_remote(tmp_path):
    """Issue #114: the full setup entry migrates the deployment
    checkout's HTTPS `origin` to SSH (the human-authorized path) and
    reports it in the checkout result."""
    repo, installed, state = make_run_state(tmp_path)
    state["origin_url"] = "https://github.com/xqliu/orbi.git"
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=fake_run,
    )
    assert result["checkout"]["remote_url"] == (
        "git@github.com:xqliu/orbi.git"
    )
    assert result["checkout"]["remote_protocol"] == "ssh"
    assert result["checkout"]["migrated"] is True
    assert result["checkout"]["ssh_reachable"] is True
    assert [
        "git", "remote", "set-url", "origin",
        "git@github.com:xqliu/orbi.git",
    ] in calls


def test_run_setup_fails_fast_when_ssh_is_unreachable(tmp_path):
    """Issue #114: a failed SSH probe fails the setup (structured
    reason) — no HTTPS fallback, no silent skip."""
    repo, installed, state = make_run_state(tmp_path)
    state["ssh_down"] = True
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="ssh_unreachable"):
        pilot_setup.run_setup(
            config, installed, run_command=fake_run,
        )
    # No migration happened (the remote was already SSH).
    assert [c for c in calls if c[:3] == ["git", "remote", "set-url"]] == []


def test_run_setup_missing_labels_file_fails_fast(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "labels.toml").unlink()
    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="labels.toml"):
        pilot_setup.run_setup(
            config, tmp_path / "units",
            run_command=lambda command, **kwargs: "",
        )


# --- output rendering -----------------------------------------------------------


def sample_result() -> dict:
    return {
        "setup": "ok",
        # Issue #152 bumped the setup output contract (the cli line).
        "version": 2,
        "base_branch": "main",
        "repos": [
            {
                "repo": "xqliu/orbi",
                "permission": "ADMIN",
                "default_branch": "main",
                "labels": {"aligned": 10, "total": 10},
            },
        ],
        "service": {
            "installed": True,
            "installed_path": (
                "/home/u/.config/systemd/user/orbi@.service"
            ),
            "sha256": "ab" * 32,
        },
        "timer": {
            "instances": {
                "orbi@1.timer": {
                    "enabled": True,
                    "active": True,
                    "next": "Thu 2026-08-27 10:00:00 +08",
                },
                "orbi@2.timer": {
                    "enabled": True,
                    "active": True,
                    "next": "Fri 2026-08-27 10:05:00 +08",
                },
            },
        },
        "checkout": {
            "remote": "origin",
            "branch": "main",
            "clean": True,
            "base_fresh": True,
            "remote_url": "git@github.com:xqliu/orbi.git",
            "remote_protocol": "ssh",
            "migrated": False,
            "ssh_reachable": True,
        },
        "optional_proxy": {
            "optional": True,
            "proxy": "healthy",
            "url": "http://127.0.0.1:18082/health",
        },
        # Issue #152: the CLI editable-install step result.
        "cli": {
            "action": "verified",
            "source": "/home/u/repo/src/orbi/__init__.py",
        },
    }


def test_format_setup_renders_stable_key_value_lines():
    lines = pilot_setup.format_setup(sample_result())
    assert lines[0] == "setup=ok version=2 base_branch=main"
    # Issue #152: the CLI line reports the editable install state and
    # the import source (the checkout package `src/orbi/`).
    assert lines[1] == (
        "cli=verified source=/home/u/repo/src/orbi/__init__.py"
    )
    assert (
        "repo=xqliu/orbi permission=ADMIN "
        "default_branch=main labels=10/10" in lines
    )
    assert (
        "service=installed path=/home/u/.config/systemd/user/"
        "orbi@.service sha256=" + "ab" * 32 in lines
    )
    # Issue #149: one line per timer instance; the NEXT field carries
    # spaces, so it is quoted (quote_value).
    assert (
        'timer=orbi@1.timer enabled active=true '
        'next="Thu 2026-08-27 10:00:00 +08"'
    ) in lines
    assert (
        'timer=orbi@2.timer enabled active=true '
        'next="Fri 2026-08-27 10:05:00 +08"'
    ) in lines
    assert (
        "checkout=remote=origin branch=main clean=true base_fresh=true "
        "remote_url=git@github.com:xqliu/orbi.git protocol=ssh "
        "migrated=false ssh_reachable=true"
    ) in lines
    assert (
        "model_endpoint=optional optional_proxy=healthy "
        "url=http://127.0.0.1:18082/health"
    ) in lines


def test_format_setup_quotes_values_with_spaces():
    result = sample_result()
    result["timer"]["instances"]["orbi@1.timer"]["next"] = (
        "Thu 2026 08-27 10:00:00"
    )
    lines = pilot_setup.format_setup(result)
    assert (
        'timer=orbi@1.timer enabled active=true '
        'next="Thu 2026 08-27 10:00:00"'
    ) in lines


def test_format_setup_renders_multiple_repos():
    result = sample_result()
    result["repos"].append(
        {
            "repo": "xqliu/muyan-ceo",
            "permission": "WRITE",
            "default_branch": "main",
            "labels": {"aligned": 6, "total": 10},
        },
    )
    lines = pilot_setup.format_setup(result)
    repo_lines = [line for line in lines if line.startswith("repo=")]
    assert len(repo_lines) == 2
    assert repo_lines[1].startswith("repo=xqliu/muyan-ceo ")
    assert "labels=6/10" in repo_lines[1]


def test_install_units_step_result_is_json_serializable(tmp_path):
    """Regression: install_units_step returns a Path for the installed
    unit; the --json contract requires the result document to stay
    JSON-serializable."""
    state = {}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    assert isinstance(result["service"]["installed_path"], str)
    json.loads(pilot_setup.to_json(result))


def test_json_output_round_trips_the_result():
    payload = pilot_setup.to_json(sample_result())
    assert json.loads(payload) == sample_result()


def test_setup_error_is_a_runtime_error():
    assert issubclass(pilot_setup.SetupError, RuntimeError)


# --- remaining defensive branches ---------------------------------------------


def test_load_label_defs_rejects_broken_toml_syntax(tmp_path):
    path = tmp_path / "labels.toml"
    path.write_text('label = ["\n', encoding="utf-8")
    with pytest.raises(pilot_setup.SetupError, match="malformed"):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_a_non_table_entry(tmp_path):
    path = tmp_path / "labels.toml"
    path.write_text("label = [1]\n", encoding="utf-8")
    with pytest.raises(
        pilot_setup.SetupError, match="must be a table",
    ):
        pilot_setup.load_label_defs(path)


def test_load_label_defs_rejects_an_entry_without_a_name(tmp_path):
    path = tmp_path / "labels.toml"
    path.write_text(
        "[[label]]\ncolor = \"1d76db\"\ndescription = \"x\"\n"
        + "\n".join(
            f'[[label]]\nname = "{e["name"]}"\ncolor = "{e["color"]}"\n'
            f'description = "{e["description"]}"'
            for e in VALID_DEFS[1:]
        ),
        encoding="utf-8",
    )
    with pytest.raises(pilot_setup.SetupError, match="no non-empty name"):
        pilot_setup.load_label_defs(path)


def test_check_repo_fails_fast_on_non_object_json():
    with pytest.raises(pilot_setup.SetupError, match="parse"):
        pilot_setup.check_repo(
            "xqliu/orbi",
            run_command=lambda command, **kwargs: "[1, 2]",
        )


def test_check_repo_fails_fast_without_name_with_owner():
    with pytest.raises(pilot_setup.SetupError, match="nameWithOwner"):
        pilot_setup.check_repo(
            "xqliu/orbi",
            run_command=lambda command, **kwargs: (
                json.dumps({
                    "viewerPermission": "WRITE",
                    "defaultBranchRef": {"name": "main"},
                })
            ),
        )


def test_check_repo_fails_fast_without_default_branch():
    with pytest.raises(pilot_setup.SetupError, match="defaultBranchRef"):
        pilot_setup.check_repo(
            "xqliu/orbi",
            run_command=lambda command, **kwargs: (
                json.dumps({
                    "nameWithOwner": "xqliu/orbi",
                    "viewerPermission": "WRITE",
                })
            ),
        )


def test_align_labels_fails_fast_on_unparseable_label_list():
    with pytest.raises(pilot_setup.SetupError, match="label list"):
        pilot_setup.align_labels(
            "xqliu/orbi",
            VALID_DEFS,
            run_command=lambda command, **kwargs: "not-json",
        )


def test_align_labels_fails_fast_on_non_array_label_list():
    with pytest.raises(pilot_setup.SetupError, match="label list"):
        pilot_setup.align_labels(
            "xqliu/orbi",
            VALID_DEFS,
            run_command=lambda command, **kwargs: '{"name": "bug"}',
        )


def test_timer_next_trigger_falls_back_to_the_first_column():
    # A timer row without the two-space column separator (defensive
    # fallback): the first column is reported as-is.
    raw = "X orbi@1.timer orbi@1.service\n"
    assert pilot_setup.timer_next_trigger(
        raw, "orbi@1.timer",
    ) == "X"


def test_check_checkout_fails_fast_when_origin_is_missing(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="origin remote"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/orbi"],
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(
                        128, command, stderr="No remote configured",
                    )
                )
            ),
        )


def test_run_setup_rejects_an_empty_repo_list(tmp_path):
    repo = make_repo(tmp_path)
    config = runner.load_config(make_config(tmp_path, repo))
    with pytest.raises(pilot_setup.SetupError, match="--repo"):
        pilot_setup.run_setup(
            config, tmp_path / "units",
            repos=[],
            run_command=lambda command, **kwargs: "",
        )


def test_run_setup_routes_home_files_to_deploy_home(tmp_path, monkeypatch):
    """Issue #330: with deploy_home set, labels.toml, the CLI editable
    install and the unit templates come from the deployment home; the
    delivery checkout (repo_dir) is only the transport-checked repo."""
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)
    home = tmp_path / "home"
    home.mkdir()
    config = runner.load_config(make_config(tmp_path, repo))
    config["deploy_home"] = home

    seen: dict = {}

    def spy_load(path):
        seen["labels"] = Path(path)
        return [dict(entry) for entry in VALID_DEFS]

    def spy_cli(repo_dir, module_file, *, run_command):
        seen["cli"] = Path(repo_dir)
        return {"action": "verified", "source": str(repo_dir)}

    def spy_units(repo_dir, installed_dir, *, max_concurrency, run_command):
        seen["units"] = Path(repo_dir)
        return {
            "service": {
                "installed": True,
                "installed_path": str(installed_dir / "orbi@.service"),
                "sha256": "deadbeef",
            },
            "timer": {"instances": {}},
        }

    def spy_checkout(repo_dir, base_branch, source_repos, *, run_command):
        seen["checkout"] = Path(repo_dir)
        return {
            "remote": "origin", "branch": "main", "clean": True,
            "base_fresh": True, "remote_url": "git@github.com:x/orbi.git",
            "remote_protocol": "ssh", "migrated": False,
            "ssh_reachable": True,
        }

    monkeypatch.setattr(pilot_setup, "load_label_defs", spy_load)
    monkeypatch.setattr(pilot_setup, "install_cli_step", spy_cli)
    monkeypatch.setattr(pilot_setup, "install_units_step", spy_units)
    monkeypatch.setattr(pilot_setup, "check_checkout", spy_checkout)

    result = pilot_setup.run_setup(config, installed, run_command=fake_run)
    assert result["setup"] == "ok"
    assert seen["labels"] == home / "labels.toml"
    assert seen["cli"] == home
    assert seen["units"] == home
    # The delivery checkout stays the transport-checked repo.
    assert seen["checkout"] == repo
