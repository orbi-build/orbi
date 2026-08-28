"""One-time setup tests (Issue #117).

`muyan_pilot.py setup` is the config-driven, idempotent, fail-fast
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

import bootstrap_runner as runner
import pilot_setup
import systemd_deploy

@pytest.fixture(autouse=True)
def _default_command_lookup(monkeypatch):
    """Issue #140: the installed `muyan-pilot` CLI is a required
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
    `muyan_pilot` import source. The test world simulates a CLI
    running from its own checkout (an editable install of
    `make_repo`'s `tmp_path/repo`); a test that needs a drifted
    source re-points `world["module_file"]` before calling
    `run_setup`."""
    import types

    world = {"module_file": tmp_path / "repo" / "muyan_pilot.py"}
    stub = types.SimpleNamespace(
        module_file=lambda: world["module_file"],
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
    (systemd / "muyan-pilot@.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n",
        encoding="utf-8",
    )
    (systemd / "muyan-pilot@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    write_labels_toml(repo, VALID_DEFS)
    return repo


def make_config(tmp_path: Path, repo_dir: Path,
                source_repos: list[str] | None = None) -> Path:
    config = tmp_path / "muyan-pilot.toml"
    config.write_text(
        "source_repos = ["
        + ", ".join(f'"{r}"' for r in (source_repos or ["xqliu/muyan-pilot"]))
        + f']\nrepo_dir = "{repo_dir}"\nbase_branch = "main"\n',
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
                "origin_url", "git@github.com:xqliu/muyan-pilot.git",
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
                '{"nameWithOwner":"xqliu/muyan-pilot",'
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
                "muyan-pilot@1.timer                muyan-pilot@1.service\n"
                "Fri 2026-08-27 10:05:00 +08        6min "
                "Thu 2026-08-27 10:00:00 +08     5min ago "
                "muyan-pilot@2.timer                muyan-pilot@2.service\n",
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


def test_load_label_defs_parses_all_eight_platform_labels(tmp_path):
    path = write_labels_toml(tmp_path, VALID_DEFS)
    defs = pilot_setup.load_label_defs(path)
    assert [entry["name"] for entry in defs] == [
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked", "p0", "ai-epic",
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


def test_committed_labels_toml_covers_the_eight_platform_labels():
    """The committed labels.toml (repo root) must define exactly the 8
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
        "muyan-pilot": "/usr/bin/muyan-pilot",
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
        "xqliu/muyan-pilot",
        run_command=lambda command, **kwargs: (
            json.dumps({
                "nameWithOwner": "xqliu/muyan-pilot",
                "viewerPermission": "WRITE",
                "defaultBranchRef": {"name": "main"},
            })
        ),
    )
    assert result == {
        "repo": "xqliu/muyan-pilot",
        "permission": "WRITE",
        "default_branch": "main",
    }


def test_check_repo_fails_fast_on_a_read_only_repo():
    with pytest.raises(pilot_setup.SetupError, match="permission"):
        pilot_setup.check_repo(
            "xqliu/muyan-pilot",
            run_command=lambda command, **kwargs: (
                json.dumps({
                    "nameWithOwner": "xqliu/muyan-pilot",
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
            "xqliu/muyan-pilot",
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
        "xqliu/muyan-pilot",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["repo"] == "xqliu/muyan-pilot"
    assert result["aligned"] == 8
    assert result["total"] == 8
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
        "xqliu/muyan-pilot",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["aligned"] == 8
    assert [c for c in calls if c[:3] == ["gh", "label", "create"]] == []


def test_align_labels_reports_partial_alignment(tmp_path):
    state = {"existing_labels": []}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.align_labels(
        "xqliu/muyan-pilot",
        pilot_setup.load_label_defs(repo / "labels.toml"),
        run_command=fake_run,
    )
    assert result["aligned"] == 8
    assert result["total"] == 8
    assert len([c for c in calls if c[:3] == ["gh", "label", "create"]]) == 8


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
            "xqliu/muyan-pilot",
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
        repo, installed, run_command=fake_run,
    )
    assert result["service"]["installed"] is True
    assert result["service"]["installed_path"] == str(
        installed / "muyan-pilot@.service",
    )
    assert result["service"]["sha256"] == systemd_deploy.sha256_hex(
        installed / "muyan-pilot@.service",
    )
    # Issue #149: EACH timer instance is reported.
    instances = result["timer"]["instances"]
    assert sorted(instances) == sorted(systemd_deploy.TIMER_INSTANCES)
    assert instances["muyan-pilot@1.timer"]["enabled"] is True
    assert instances["muyan-pilot@1.timer"]["active"] is True
    assert instances["muyan-pilot@1.timer"]["next"] == (
        "Thu 2026-08-27 10:00:00 +08"
    )
    assert instances["muyan-pilot@2.timer"]["next"] == (
        "Fri 2026-08-27 10:05:00 +08"
    )
    # The install itself is the systemd_deploy idempotent install.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert [
            "systemctl", "--user", "enable", "--now", instance,
        ] in calls


def test_install_units_step_reports_a_disabled_inactive_timer(tmp_path):
    state = {"is_enabled": "disabled", "active_state": "inactive"}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    instances = result["timer"]["instances"]
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert instances[instance]["enabled"] is False
        assert instances[instance]["active"] is False


def test_install_units_step_reports_per_instance_state(tmp_path):
    # Issue #149: one instance can be enabled/active while the other
    # is not — the per-instance report must not collapse them.
    state = {
        "is_enabled:muyan-pilot@2.timer": "disabled",
        "active_state:muyan-pilot@2.timer": "inactive",
    }
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    instances = result["timer"]["instances"]
    assert instances["muyan-pilot@1.timer"]["enabled"] is True
    assert instances["muyan-pilot@1.timer"]["active"] is True
    assert instances["muyan-pilot@2.timer"]["enabled"] is False
    assert instances["muyan-pilot@2.timer"]["active"] is False


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
        "muyan-pilot@1.timer                muyan-pilot@1.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "muyan-pilot@1.timer",
    ) == "Thu 2026-08-27 10:00:00 +08"


def test_timer_next_trigger_returns_dash_when_the_timer_is_absent():
    assert pilot_setup.timer_next_trigger(
        "NEXT\n", "muyan-pilot@1.timer",
    ) == "-"


def test_timer_next_trigger_ignores_other_timers():
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "other.timer                         other.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "muyan-pilot@1.timer",
    ) == "-"


def test_timer_next_trigger_distinguishes_the_two_instances():
    # Issue #149: the two instances have independent next triggers.
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "muyan-pilot@1.timer                muyan-pilot@1.service\n"
        "Fri 2026-08-27 10:05:00 +08        6min 46s "
        "Thu 2026-08-27 10:00:00 +08     5min ago "
        "muyan-pilot@2.timer                muyan-pilot@2.service\n"
    )
    assert pilot_setup.timer_next_trigger(
        raw, "muyan-pilot@1.timer",
    ) == "Thu 2026-08-27 10:00:00 +08"
    assert pilot_setup.timer_next_trigger(
        raw, "muyan-pilot@2.timer",
    ) == "Fri 2026-08-27 10:05:00 +08"


# --- checkout check (read-only) ----------------------------------------------


def test_check_checkout_reports_remote_branch_clean_and_fresh(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
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
        repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert result == {
        "remote": "origin",
        "branch": "main",
        "clean": True,
        "base_fresh": True,
        "remote_url": "git@github.com:xqliu/muyan-pilot.git",
        "remote_protocol": "ssh",
        "migrated": False,
        "ssh_reachable": True,
    }


def test_check_checkout_fails_fast_on_a_dirty_checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:2] == ["git", "status"]:
            return " M bootstrap_runner.py"
        return ""

    with pytest.raises(pilot_setup.SetupError, match="not clean"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
        )


def test_check_checkout_reports_a_stale_base(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "config"]:
            return "git@github.com:xqliu/muyan-pilot.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "rev-parse", "origin/main"]:
            return "1111111111111111111111111111111111111111"
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "2222222222222222222222222222222222222222"
        return ""

    result = pilot_setup.check_checkout(
        repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert result["base_fresh"] is False


def test_check_checkout_fails_fast_without_an_origin_remote(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="remote"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/muyan-pilot"],
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
            repo, "main", ["xqliu/muyan-pilot"],
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
    state = {"url": "https://github.com/xqliu/muyan-pilot.git"}

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
        repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
    )
    assert state["url"] == "git@github.com:xqliu/muyan-pilot.git"
    assert result["remote_url"] == "git@github.com:xqliu/muyan-pilot.git"
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
            return "git@github.com:xqliu/muyan-pilot.git"
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
            repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
        )
    message = str(exc.value)
    assert "git ls-remote git@github.com:xqliu/muyan-pilot.git" in message
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
            return "git@github.com:xqliu/muyan-pilot.git"
        if command[:2] == ["git", "ls-remote"]:
            return "abc\tHEAD"
        if command[:3] == ["git", "branch", "--show-current"]:
            raise OSError("spawn failed")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(pilot_setup.SetupError, match="checkout"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
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
            repo, "main", ["xqliu/muyan-pilot"], run_command=fake_run,
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
    """The running CLI already imports `muyan_pilot` from the
    configured checkout (the editable install): setup verifies it
    WITHOUT any uv call (idempotent re-run, no per-setup reinstall)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    calls: list = []
    result = pilot_setup.install_cli_step(
        repo, repo / "muyan_pilot.py",
        run_command=lambda command, **kwargs: calls.append(command) or "",
    )
    assert calls == []
    assert result == {
        "action": "verified",
        "source": str((repo / "muyan_pilot.py").resolve()),
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
        "/home/u/.local/share/uv/tools/muyan-pilot/"
        "lib/python3.14/site-packages/muyan_pilot.py",
        run_command=fake_run,
    )
    assert calls == [[
        "uv", "tool", "install", "--force", "--reinstall", "--editable",
        "--python", "/usr/bin/python3", str(repo),
    ]]
    assert result == {
        "action": "installed",
        "source": str((repo / "muyan_pilot.py").resolve()),
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
            repo, "/elsewhere/muyan_pilot.py", run_command=failing,
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
            "repo": "xqliu/muyan-pilot",
            "permission": "ADMIN",
            "default_branch": "main",
            "labels": {"aligned": 8, "total": 8},
        },
    ]
    assert result["service"]["installed"] is True
    # Issue #149: the timer report is per instance.
    instances = result["timer"]["instances"]
    assert sorted(instances) == sorted(systemd_deploy.TIMER_INSTANCES)
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert instances[instance]["enabled"] is True
        assert instances[instance]["active"] is True
    assert result["checkout"]["clean"] is True
    assert result["checkout"]["base_fresh"] is True
    assert result["optional_proxy"]["optional"] is True
    # Issue #152: the CLI step reports the editable install verified
    # (the test process imports muyan_pilot from the repo world).
    assert result["cli"]["action"] == "verified"
    assert result["cli"]["source"] == str(
        (repo / "muyan_pilot.py").resolve(),
    )
    # No uv call: an existing editable install is never reinstalled.
    assert [c for c in calls if c[:2] == ["uv", "tool"]] == []


def test_run_setup_repo_override_limits_the_target(tmp_path):
    repo, installed, state = make_run_state(tmp_path)
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(
        tmp_path, repo,
        source_repos=["xqliu/muyan-pilot", "xqliu/muyan-ceo"],
    ))
    result = pilot_setup.run_setup(
        config, installed,
        repos=["xqliu/muyan-pilot"],
        run_command=fake_run,
    )
    assert [entry["repo"] for entry in result["repos"]] == [
        "xqliu/muyan-pilot",
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
        "/home/u/.local/share/uv/tools/muyan-pilot/"
        "lib/python3.14/site-packages/muyan_pilot.py"
    )
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=fake_run,
    )
    assert result["setup"] == "ok"
    assert result["cli"]["action"] == "installed"
    assert result["cli"]["source"] == str(
        (repo / "muyan_pilot.py").resolve(),
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
    _cli_source_world["module_file"] = "/elsewhere/muyan_pilot.py"
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
    state["origin_url"] = "https://github.com/xqliu/muyan-pilot.git"
    fake_run, calls = fake_run_factory(state)
    config = runner.load_config(make_config(tmp_path, repo))
    result = pilot_setup.run_setup(
        config, installed, run_command=fake_run,
    )
    assert result["checkout"]["remote_url"] == (
        "git@github.com:xqliu/muyan-pilot.git"
    )
    assert result["checkout"]["remote_protocol"] == "ssh"
    assert result["checkout"]["migrated"] is True
    assert result["checkout"]["ssh_reachable"] is True
    assert [
        "git", "remote", "set-url", "origin",
        "git@github.com:xqliu/muyan-pilot.git",
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
                "repo": "xqliu/muyan-pilot",
                "permission": "ADMIN",
                "default_branch": "main",
                "labels": {"aligned": 8, "total": 8},
            },
        ],
        "service": {
            "installed": True,
            "installed_path": (
                "/home/u/.config/systemd/user/muyan-pilot@.service"
            ),
            "sha256": "ab" * 32,
        },
        "timer": {
            "instances": {
                "muyan-pilot@1.timer": {
                    "enabled": True,
                    "active": True,
                    "next": "Thu 2026-08-27 10:00:00 +08",
                },
                "muyan-pilot@2.timer": {
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
            "remote_url": "git@github.com:xqliu/muyan-pilot.git",
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
            "source": "/home/u/repo/muyan_pilot.py",
        },
    }


def test_format_setup_renders_stable_key_value_lines():
    lines = pilot_setup.format_setup(sample_result())
    assert lines[0] == "setup=ok version=2 base_branch=main"
    # Issue #152: the CLI line reports the editable install state and
    # the import source (the checkout root `muyan_pilot.py`).
    assert lines[1] == (
        "cli=verified source=/home/u/repo/muyan_pilot.py"
    )
    assert (
        "repo=xqliu/muyan-pilot permission=ADMIN "
        "default_branch=main labels=8/8" in lines
    )
    assert (
        "service=installed path=/home/u/.config/systemd/user/"
        "muyan-pilot@.service sha256=" + "ab" * 32 in lines
    )
    # Issue #149: one line per timer instance; the NEXT field carries
    # spaces, so it is quoted (quote_value).
    assert (
        'timer=muyan-pilot@1.timer enabled active=true '
        'next="Thu 2026-08-27 10:00:00 +08"'
    ) in lines
    assert (
        'timer=muyan-pilot@2.timer enabled active=true '
        'next="Fri 2026-08-27 10:05:00 +08"'
    ) in lines
    assert (
        "checkout=remote=origin branch=main clean=true base_fresh=true "
        "remote_url=git@github.com:xqliu/muyan-pilot.git protocol=ssh "
        "migrated=false ssh_reachable=true"
    ) in lines
    assert (
        "model_endpoint=optional optional_proxy=healthy "
        "url=http://127.0.0.1:18082/health"
    ) in lines


def test_format_setup_quotes_values_with_spaces():
    result = sample_result()
    result["timer"]["instances"]["muyan-pilot@1.timer"]["next"] = (
        "Thu 2026 08-27 10:00:00"
    )
    lines = pilot_setup.format_setup(result)
    assert (
        'timer=muyan-pilot@1.timer enabled active=true '
        'next="Thu 2026 08-27 10:00:00"'
    ) in lines


def test_format_setup_renders_multiple_repos():
    result = sample_result()
    result["repos"].append(
        {
            "repo": "xqliu/muyan-ceo",
            "permission": "WRITE",
            "default_branch": "main",
            "labels": {"aligned": 6, "total": 8},
        },
    )
    lines = pilot_setup.format_setup(result)
    repo_lines = [line for line in lines if line.startswith("repo=")]
    assert len(repo_lines) == 2
    assert repo_lines[1].startswith("repo=xqliu/muyan-ceo ")
    assert "labels=6/8" in repo_lines[1]


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
            "xqliu/muyan-pilot",
            run_command=lambda command, **kwargs: "[1, 2]",
        )


def test_check_repo_fails_fast_without_name_with_owner():
    with pytest.raises(pilot_setup.SetupError, match="nameWithOwner"):
        pilot_setup.check_repo(
            "xqliu/muyan-pilot",
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
            "xqliu/muyan-pilot",
            run_command=lambda command, **kwargs: (
                json.dumps({
                    "nameWithOwner": "xqliu/muyan-pilot",
                    "viewerPermission": "WRITE",
                })
            ),
        )


def test_align_labels_fails_fast_on_unparseable_label_list():
    with pytest.raises(pilot_setup.SetupError, match="label list"):
        pilot_setup.align_labels(
            "xqliu/muyan-pilot",
            VALID_DEFS,
            run_command=lambda command, **kwargs: "not-json",
        )


def test_align_labels_fails_fast_on_non_array_label_list():
    with pytest.raises(pilot_setup.SetupError, match="label list"):
        pilot_setup.align_labels(
            "xqliu/muyan-pilot",
            VALID_DEFS,
            run_command=lambda command, **kwargs: '{"name": "bug"}',
        )


def test_timer_next_trigger_falls_back_to_the_first_column():
    # A timer row without the two-space column separator (defensive
    # fallback): the first column is reported as-is.
    raw = "X muyan-pilot@1.timer muyan-pilot@1.service\n"
    assert pilot_setup.timer_next_trigger(
        raw, "muyan-pilot@1.timer",
    ) == "X"


def test_check_checkout_fails_fast_when_origin_is_missing(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="origin remote"):
        pilot_setup.check_checkout(
            repo, "main", ["xqliu/muyan-pilot"],
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
