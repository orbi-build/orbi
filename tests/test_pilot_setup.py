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

VALID_DEFS = [
    {"name": "ai-ready", "color": "1d76db", "description": "dispatched"},
    {"name": "ai-in-progress", "color": "fbca04", "description": "work"},
    {"name": "ai-pr-opened", "color": "0e8a16", "description": "pr"},
    {"name": "ai-fix-needed", "color": "fbca04", "description": "fix"},
    {"name": "ai-merged", "color": "0e8a16", "description": "merged"},
    {"name": "ai-blocked", "color": "d73a4a", "description": "blocked"},
    {"name": "p0", "color": "fbca04", "description": "urgent"},
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
    (systemd / "muyan-pilot.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n",
        encoding="utf-8",
    )
    (systemd / "muyan-pilot.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/15\n", encoding="utf-8",
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
                return state.get("active_state", "active")
            return "loaded"
        if head[:3] == ["systemctl", "--user", "is-enabled"]:
            return state.get("is_enabled", "enabled")
        if head[:3] == ["systemctl", "--user", "list-timers"]:
            return state.get(
                "list_timers",
                "NEXT\n"
                "Thu 2026-08-27 10:00:00 +08        1min "
                "Thu 2026-08-27 09:45:00 +08    15min ago "
                "muyan-pilot.timer                   muyan-pilot.service\n",
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
        raise AssertionError(f"unexpected command: {command}")

    return fake_run, calls


def test_fake_run_factory_rejects_an_unexpected_command():
    fake_run, _ = fake_run_factory({})
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["definitely", "not", "a", "known", "command"])


# --- labels.toml: the single source of truth -------------------------------


def test_load_label_defs_parses_all_seven_platform_labels(tmp_path):
    path = write_labels_toml(tmp_path, VALID_DEFS)
    defs = pilot_setup.load_label_defs(path)
    assert [entry["name"] for entry in defs] == [
        "ai-ready", "ai-in-progress", "ai-pr-opened",
        "ai-fix-needed", "ai-merged", "ai-blocked", "p0",
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
        {"name": "ai-epic", "color": "bfdadc", "description": "epic"},
    ]
    path = write_labels_toml(tmp_path, defs)
    with pytest.raises(pilot_setup.SetupError, match="ai-epic"):
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


def test_committed_labels_toml_covers_the_seven_platform_labels():
    """The committed labels.toml (repo root) must define exactly the 7
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
    assert result["aligned"] == 7
    assert result["total"] == 7
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
    assert result["aligned"] == 7
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
    assert result["aligned"] == 7
    assert result["total"] == 7
    assert len([c for c in calls if c[:3] == ["gh", "label", "create"]]) == 7


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
    assert result["service"]["installed_path"] == installed / "muyan-pilot.service"
    assert result["service"]["sha256"] == systemd_deploy.sha256_hex(
        installed / "muyan-pilot.service",
    )
    assert result["timer"]["enabled"] is True
    assert result["timer"]["active"] is True
    assert result["timer"]["next"] == "Thu 2026-08-27 10:00:00 +08"
    # The install itself is the systemd_deploy idempotent install.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert [
        "systemctl", "--user", "enable", "--now", "muyan-pilot.timer",
    ] in calls


def test_install_units_step_reports_a_disabled_inactive_timer(tmp_path):
    state = {"is_enabled": "disabled", "active_state": "inactive"}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    assert result["timer"]["enabled"] is False
    assert result["timer"]["active"] is False


def test_install_units_step_reports_a_missing_next_trigger(tmp_path):
    state = {"list_timers": "NEXT\n"}
    fake_run, calls = fake_run_factory(state)
    repo = make_repo(tmp_path)
    result = pilot_setup.install_units_step(
        repo, tmp_path / "units", run_command=fake_run,
    )
    assert result["timer"]["next"] == "-"


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
        "muyan-pilot.timer                   muyan-pilot.service\n"
    )
    assert pilot_setup.timer_next_trigger(raw) == (
        "Thu 2026-08-27 10:00:00 +08"
    )


def test_timer_next_trigger_returns_dash_when_the_timer_is_absent():
    assert pilot_setup.timer_next_trigger("NEXT\n") == "-"


def test_timer_next_trigger_ignores_other_timers():
    raw = (
        "NEXT                                   LEFT LAST\n"
        "Thu 2026-08-27 10:00:00 +08        1min 46s "
        "Thu 2026-08-27 09:18:18 +08    28min ago "
        "other.timer                         other.service\n"
    )
    assert pilot_setup.timer_next_trigger(raw) == "-"


# --- checkout check (read-only) ----------------------------------------------


def test_check_checkout_reports_remote_branch_clean_and_fresh(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    result = pilot_setup.check_checkout(
        repo, "main", run_command=lambda command, **kwargs: (
            "0123456789abcdef0123456789abcdef01234567"
            if command[:3] == ["git", "rev-parse", "origin/main"]
            else "0123456789abcdef0123456789abcdef01234567"
            if command[:3] == ["git", "rev-parse", "HEAD"]
            else "origin"
            if command[:2] == ["git", "remote"]
            else "main"
            if command[:3] == ["git", "branch", "--show-current"]
            else ""
        ),
    )
    assert result == {
        "remote": "origin",
        "branch": "main",
        "clean": True,
        "base_fresh": True,
    }


def test_check_checkout_fails_fast_on_a_dirty_checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "remote"]:
            return "origin"
        if command[:2] == ["git", "status"]:
            return " M bootstrap_runner.py"
        return ""

    with pytest.raises(pilot_setup.SetupError, match="not clean"):
        pilot_setup.check_checkout(
            repo, "main", run_command=fake_run,
        )


def test_check_checkout_reports_a_stale_base(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "remote"]:
            return "origin"
        if command[:3] == ["git", "rev-parse", "origin/main"]:
            return "1111111111111111111111111111111111111111"
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "2222222222222222222222222222222222222222"
        return ""

    result = pilot_setup.check_checkout(
        repo, "main", run_command=fake_run,
    )
    assert result["base_fresh"] is False


def test_check_checkout_fails_fast_without_an_origin_remote(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="remote"):
        pilot_setup.check_checkout(
            repo, "main",
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
            repo, "main",
            run_command=lambda command, **kwargs: (
                (_ for _ in ()).throw(
                    subprocess.CalledProcessError(1, command, stderr="boom")
                )
            ),
        )


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
            "labels": {"aligned": 7, "total": 7},
        },
    ]
    assert result["service"]["installed"] is True
    assert result["timer"]["enabled"] is True
    assert result["timer"]["active"] is True
    assert result["checkout"]["clean"] is True
    assert result["checkout"]["base_fresh"] is True
    assert result["optional_proxy"]["optional"] is True


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
        "version": 1,
        "base_branch": "main",
        "repos": [
            {
                "repo": "xqliu/muyan-pilot",
                "permission": "ADMIN",
                "default_branch": "main",
                "labels": {"aligned": 7, "total": 7},
            },
        ],
        "service": {
            "installed": True,
            "installed_path": "/home/u/.config/systemd/user/muyan-pilot.service",
            "sha256": "ab" * 32,
        },
        "timer": {
            "enabled": True,
            "active": True,
            "next": "Thu 2026-08-27 10:00:00 +08",
        },
        "checkout": {
            "remote": "origin",
            "branch": "main",
            "clean": True,
            "base_fresh": True,
        },
        "optional_proxy": {
            "optional": True,
            "proxy": "healthy",
            "url": "http://127.0.0.1:18082/health",
        },
    }


def test_format_setup_renders_stable_key_value_lines():
    lines = pilot_setup.format_setup(sample_result())
    assert lines[0] == "setup=ok version=1 base_branch=main"
    assert (
        "repo=xqliu/muyan-pilot permission=ADMIN "
        "default_branch=main labels=7/7" in lines
    )
    assert (
        "service=installed path=/home/u/.config/systemd/user/"
        "muyan-pilot.service sha256=" + "ab" * 32 in lines
    )
    # The NEXT field carries spaces, so it is quoted (quote_value).
    assert (
        'timer=enabled active=true next="Thu 2026-08-27 10:00:00 +08"'
    ) in lines
    assert (
        "checkout=remote=origin branch=main clean=true base_fresh=true"
    ) in lines
    assert (
        "model_endpoint=optional optional_proxy=healthy "
        "url=http://127.0.0.1:18082/health"
    ) in lines


def test_format_setup_quotes_values_with_spaces():
    result = sample_result()
    result["timer"]["next"] = "Thu 2026 08-27 10:00:00"
    lines = pilot_setup.format_setup(result)
    assert (
        'timer=enabled active=true next="Thu 2026 08-27 10:00:00"'
    ) in lines


def test_format_setup_renders_multiple_repos():
    result = sample_result()
    result["repos"].append(
        {
            "repo": "xqliu/muyan-ceo",
            "permission": "WRITE",
            "default_branch": "main",
            "labels": {"aligned": 6, "total": 7},
        },
    )
    lines = pilot_setup.format_setup(result)
    repo_lines = [line for line in lines if line.startswith("repo=")]
    assert len(repo_lines) == 2
    assert repo_lines[1].startswith("repo=xqliu/muyan-ceo ")
    assert "labels=6/7" in repo_lines[1]


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
    raw = "X muyan-pilot.timer muyan-pilot.service\n"
    assert pilot_setup.timer_next_trigger(raw) == "X"


def test_check_checkout_fails_fast_when_origin_is_missing(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    with pytest.raises(pilot_setup.SetupError, match="origin remote"):
        pilot_setup.check_checkout(
            repo, "main",
            run_command=lambda command, **kwargs: "upstream",
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
