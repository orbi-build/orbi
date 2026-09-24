"""The launchd scheduler implementation (Issue #849).

macOS support without a macOS runner: the launchd branch is exercised
through the injected scheduler (plist rendering, the XML-normalized
drift comparison and the launchctl command contract), with the
subprocess seam faked by ``FakeLaunchd``. The launchctl invocations
assert the real subcommand contract from the modern
launchctl(1)/launchd.plist(5) man pages: ``bootstrap``/``bootout`` on
the ``gui/<uid>`` domain, persistent ``enable``/``disable``, ``print``
for state.
"""
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from orbi import launchd_deploy, scheduler
from tests.fakes.launchd import FakeLaunchd

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_repo(tmp_path: Path) -> Path:
    """A deployment checkout carrying the launchd template."""
    repo = tmp_path / "repo"
    (repo / "launchd").mkdir(parents=True)
    (repo / "launchd" / launchd_deploy.TEMPLATE_NAME).write_bytes(
        (REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME).read_bytes(),
    )
    return repo


def test_label_naming_covers_default_and_named_deployments():
    assert launchd_deploy.label_base(None) == "org.orbi.runner"
    assert launchd_deploy.label_base("website") == "org.orbi.website.runner"
    assert launchd_deploy.label_for(None, 2) == "org.orbi.runner.2"
    assert launchd_deploy.label_for("website", 1) == (
        "org.orbi.website.runner.1"
    )


def test_unit_pairs_generate_one_plist_per_instance():
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.template_units() == (launchd_deploy.TEMPLATE_NAME,)
    pairs = sched.unit_pairs(None, 3)
    assert pairs == [
        (launchd_deploy.TEMPLATE_NAME, "org.orbi.runner.1.plist"),
        (launchd_deploy.TEMPLATE_NAME, "org.orbi.runner.2.plist"),
        (launchd_deploy.TEMPLATE_NAME, "org.orbi.runner.3.plist"),
    ]
    # On launchd ONE label IS the service and the timer.
    assert sched.timer_instances(None, 2) == (
        "org.orbi.runner.1", "org.orbi.runner.2",
    )
    assert sched.service_instances(None, 2) == (
        "org.orbi.runner.1", "org.orbi.runner.2",
    )


def test_template_carries_the_launchd_contract():
    raw = (
        REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME
    ).read_bytes()
    plist = plistlib.loads(raw)
    assert plist["Label"] == "{{ORBI_LABEL}}"
    # StartInterval mirrors the systemd timer (OnCalendar=*:00/5).
    assert plist["StartInterval"] == 300
    # The env file (provider keys) is sourced OUTSIDE the plist — the
    # EnvironmentFile=- contract — with `set -a` so the bare KEY=value
    # assignments reach exec's environment (Issue #867), guarded by
    # `[ -r` so a missing file cannot abort the shell (the optional `-`
    # semantics), then the installed CLI execs.
    program = plist["ProgramArguments"]
    assert program[:2] == ["/bin/sh", "-c"]
    assert "[ -r '{{ORBI_REPO_DIR}}/.orbi/env' ]" in program[2]
    assert "set -a; . '{{ORBI_REPO_DIR}}/.orbi/env'; set +a" in program[2]
    assert "exec '{{ORBI_USER_HOME}}/.local/bin/orbi'" in program[2]
    # Issue #871: the wrapper mirrors the Linux ExecStartPre step 2 —
    # `orbi sync-engine-source` runs before the exec (chained with `&&`,
    # so a failed sync — exit 1 — stops the chain and the Runner never
    # starts: the ExecStartPre failure semantics, fail closed). It sits
    # AFTER the env sourcing because the sync's git transport may need
    # the env-file credentials. No flock/timeout wrapper: macOS ships
    # neither /usr/bin/flock nor timeout (the plain Issue-pinned shape).
    assert (
        "'{{ORBI_USER_HOME}}/.local/bin/orbi' sync-engine-source && exec"
        in program[2]
    )
    assert program[2].index("set +a") < program[2].index(
        "sync-engine-source",
    )
    assert plist["EnvironmentVariables"]["ORBI_CONFIG"] == (
        "{{ORBI_REPO_DIR}}/orbi.toml"
    )
    # The PATH leads with the Apple Silicon Homebrew prefix (Issue #869):
    # gh, uv and the global npm pi all resolve there on arm64 macs; the
    # Intel prefix /usr/local/bin stays in the list behind it.
    path = plist["EnvironmentVariables"]["PATH"]
    assert path.startswith("/opt/homebrew/bin:")
    assert "{{ORBI_USER_HOME}}/.npm-global/bin" in path
    assert "{{ORBI_USER_HOME}}/.local/bin" in path
    assert "/usr/local/bin" in path
    # Runner output lands in the gitignored state dir, per instance.
    assert plist["StandardOutPath"] == (
        "{{ORBI_REPO_DIR}}/.orbi/{{ORBI_LABEL}}.log"
    )
    assert plist["StandardErrorPath"] == plist["StandardOutPath"]


def test_render_substitutes_every_placeholder_and_parses(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    template = (
        REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME
    ).read_text(encoding="utf-8")
    rendered = sched.render_unit(template, tmp_path, None, instance=2)
    assert "{{" not in rendered
    plist = plistlib.loads(rendered.encode("utf-8"))
    assert plist["Label"] == "org.orbi.runner.2"
    assert plist["WorkingDirectory"] == str(tmp_path)
    assert plist["EnvironmentVariables"]["ORBI_CONFIG"] == (
        f"{tmp_path}/orbi.toml"
    )
    # The rendered log path matches the module's log_path helper —
    # the crash scan and the doctor tail read exactly where launchd writes.
    assert plist["StandardOutPath"] == str(
        launchd_deploy.log_path(tmp_path, "org.orbi.runner.2")
    )


def test_rendered_wrapper_exports_env_and_tolerates_a_missing_env_file(
    tmp_path,
):
    """The rendered ProgramArguments[2] behaves like EnvironmentFile=-
    under a real POSIX sh: the env-file keys reach exec's environment,
    and a missing env file still lets the exec happen (Issue #867).
    The old form sourced bare KEY=value without `set -a` (shell-only
    variables, invisible to exec) and aborted on a missing file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sched = launchd_deploy.LaunchdScheduler()
    rendered = sched.render_unit(
        (REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME).read_text(
            encoding="utf-8",
        ),
        repo, None, instance=1,
    )
    wrapper = plistlib.loads(rendered.encode("utf-8"))["ProgramArguments"][2]
    # The RENDERED wrapper carries the #871 sync step (the acceptance
    # target; the template-level assertion above does not see a
    # renderer regression). Asserted BEFORE the stub below: a missing
    # step would make that re.sub silently match nothing.
    assert "sync-engine-source" in wrapper
    # The acceptance target swaps the exec'd CLI for /usr/bin/env so the
    # test reads the environment the wrapper actually hands over, and
    # stubs the #871 sync step with `true` (this host HAS the real
    # `<home>/.local/bin/orbi` — running it here would sync the REAL
    # deploy home). The `&&` chain itself stays real.
    wrapper = re.sub(
        r"'[^']*/\.local/bin/orbi' sync-engine-source",
        "true sync-engine-source", wrapper,
    )
    wrapper = re.sub(
        r"exec '[^']*/\.local/bin/orbi'", "exec /usr/bin/env", wrapper,
    )
    assert "/usr/bin/env" in wrapper

    # Success path: the key written as bare KEY=value reaches the exec.
    (repo / ".orbi").mkdir()
    (repo / ".orbi" / "env").write_text(
        "ORBI_TICK_KEY=issue-867-ok\n", encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/sh", "-c", wrapper], capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ORBI_TICK_KEY=issue-867-ok" in result.stdout

    # Failure path: no env file — exec still runs, no shell abort, no
    # error noise (the old `2>/dev/null` form died before the exec).
    (repo / ".orbi" / "env").unlink()
    result = subprocess.run(
        ["/bin/sh", "-c", wrapper], capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr
    assert "ORBI_TICK_KEY" not in result.stdout
    assert "PATH=" in result.stdout


def test_rendered_wrapper_fail_closed_when_the_sync_fails(tmp_path):
    """The #871 failure path under a real POSIX sh: a failed
    `sync-engine-source` (the CLI exits 1 on EngineSourceError) stops
    the `&&` chain, the exec never runs and the tick does not start —
    the macOS mirror of a failed Linux ExecStartPre. The sync is
    stubbed with `false` (same exit shape, no deploy-home access); the
    failure reason itself lands in StandardErrorPath in production."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sched = launchd_deploy.LaunchdScheduler()
    wrapper = plistlib.loads(sched.render_unit(
        (REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME).read_text(
            encoding="utf-8",
        ),
        repo, None, instance=1,
    ).encode("utf-8"))["ProgramArguments"][2]
    wrapper = re.sub(
        r"'[^']*/\.local/bin/orbi' sync-engine-source",
        "false sync-engine-source", wrapper,
    )
    wrapper = re.sub(
        r"exec '[^']*/\.local/bin/orbi'", "exec /usr/bin/env", wrapper,
    )
    result = subprocess.run(
        ["/bin/sh", "-c", wrapper], capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode != 0
    # The exec never happened: no environment dump reached stdout.
    assert "PATH=" not in result.stdout


def test_content_sha_is_whitespace_and_key_order_insensitive():
    sched = launchd_deploy.LaunchdScheduler()
    a = plistlib.dumps({"Label": "l", "StartInterval": 300}).decode()
    b = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n  <dict>\n"
        "    <key>StartInterval</key>\n    <integer>300</integer>\n"
        "    <key>Label</key>\n    <string>l</string>\n"
        "  </dict>\n</plist>\n"
    )
    # Same plist, different key order and whitespace: NO drift.
    assert sched.content_sha(a) == sched.content_sha(b)
    assert sched.content_sha(a) != sched.content_sha(
        plistlib.dumps({"Label": "l", "StartInterval": 600}).decode(),
    )


def test_installed_sha_falls_back_to_raw_bytes_for_broken_xml(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    broken = tmp_path / "broken.plist"
    broken.write_text("<plist><dict>{{ nope", encoding="utf-8")
    sha = sched.installed_sha(broken)
    # A broken plist can never equal a canonical rendered form: drift.
    assert sha != sched.content_sha(plistlib.dumps({"Label": "x"}).decode())


def test_installed_sha_is_none_without_the_file(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.installed_sha(tmp_path / "absent.plist") is None


def test_unit_config_reads_the_env_dict(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    unit = tmp_path / "org.orbi.runner.1.plist"
    unit.write_bytes(plistlib.dumps({
        "Label": "org.orbi.runner.1",
        "EnvironmentVariables": {"ORBI_CONFIG": str(tmp_path / "orbi.toml")},
    }))
    assert sched.unit_config(unit) == (tmp_path / "orbi.toml").resolve()
    plain = tmp_path / "plain.plist"
    plain.write_bytes(plistlib.dumps({"Label": "x"}))
    assert sched.unit_config(plain) is None


def test_probe_args_print_the_gui_domain(monkeypatch):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.probe_args() == ["launchctl", "print", "gui/501"]


def test_unit_enabled_reads_the_persistent_disabled_db():
    fake = FakeLaunchd()
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.unit_enabled(fake, "org.orbi.runner.1") is True
    fake.disabled.add("org.orbi.runner.1")
    assert sched.unit_enabled(fake, "org.orbi.runner.1") is False


def test_unit_state_maps_print_output():
    fake = FakeLaunchd()
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.unit_state(fake, "org.orbi.runner.1") == "inactive"
    fake.load("org.orbi.runner.1", state="not running")
    assert sched.unit_state(fake, "org.orbi.runner.1") == "inactive"
    fake.load("org.orbi.runner.1", state="running")
    assert sched.unit_state(fake, "org.orbi.runner.1") == "active"


def test_activate_bootstraps_fresh_instances_and_disables_surplus(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    fake = FakeLaunchd()
    for _, name in sched.unit_pairs(None, 2):
        (tmp_path / name).write_bytes(plistlib.dumps({"Label": "x"}))
    sched.activate_instances(fake, tmp_path, None, max_concurrency=2)
    # Instances 1..2 enabled (persistent) and bootstrapped;
    # 3..5 disabled, never bootstrapped, never booted out.
    for index in (1, 2):
        assert (
            f"gui/501/org.orbi.runner.{index}"
        ) not in fake.disabled
        assert [
            "launchctl", "bootstrap", "gui/501",
            str(tmp_path / f"org.orbi.runner.{index}.plist"),
        ] in fake.commands
        assert f"org.orbi.runner.{index}" in fake.state
    for index in (3, 4, 5):
        assert f"org.orbi.runner.{index}" in fake.disabled
        assert f"org.orbi.runner.{index}" not in fake.state
    assert not any(
        len(command) > 1 and command[1] == "bootout"
        for command in fake.commands
    )


def test_activate_reloads_an_idle_instance_without_touching_a_running_one(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    for _, name in sched.unit_pairs(None, 1):
        (tmp_path / name).write_bytes(plistlib.dumps({"Label": "x"}))

    # Loaded but idle: the rewritten plist is reloaded (bootout +
    # bootstrap) — the daemon-reload equivalent, safe when idle.
    fake = FakeLaunchd()
    fake.load("org.orbi.runner.1", state="not running")
    sched.activate_instances(fake, tmp_path, None, max_concurrency=1)
    assert [
        "launchctl", "bootout", "gui/501/org.orbi.runner.1",
    ] in fake.commands
    assert [
        "launchctl", "bootstrap", "gui/501",
        str(tmp_path / "org.orbi.runner.1.plist"),
    ] in fake.commands

    # Now running: bootout would KILL a live Runner — never issued.
    fake = FakeLaunchd()
    fake.load("org.orbi.runner.1", state="running")
    sched.activate_instances(fake, tmp_path, None, max_concurrency=1)
    assert not any(
        len(command) > 1 and command[1] == "bootout"
        for command in fake.commands
    )
    assert not any(
        len(command) > 1 and command[1] == "bootstrap"
        for command in fake.commands
    )
    assert fake.state["org.orbi.runner.1"] == "running"


def test_running_instance_with_a_changed_plist_defers_the_reload_to_idle(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1347 acceptance (1): a running instance whose plist
    changed on upgrade is NOT booted out during its tick; the drift
    report marks it as pending reload so the next tick reloads it."""
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    repo = make_repo(tmp_path)
    installed = tmp_path / "LaunchAgents"
    sched = launchd_deploy.LaunchdScheduler()
    fake = FakeLaunchd(commit="beefbeef")

    scheduler.install_units(
        repo, installed, max_concurrency=1, run_command=fake, sched=sched,
    )
    assert sched.reload_pending(installed, "org.orbi.runner.1.plist") is False

    # The instance starts executing its tick, then the upgrade rewrites
    # the rendered plist (a real content change).
    fake.state["org.orbi.runner.1"] = "running"
    template = repo / "launchd" / launchd_deploy.TEMPLATE_NAME
    template.write_text(
        template.read_text(encoding="utf-8").replace(
            "<integer>300</integer>", "<integer>600</integer>",
        ),
        encoding="utf-8",
    )
    fake.commands.clear()
    report = scheduler.sync_drifted_units(
        repo, installed, max_concurrency=1, run_command=fake, sched=sched,
    )

    # The tick in flight is untouched: bootout would kill it.
    assert not any(
        len(command) > 1 and command[1] in ("bootout", "bootstrap")
        for command in fake.commands
    )
    assert fake.state["org.orbi.runner.1"] == "running"
    assert report  # the sync ran (the file drift was repaired)
    status = scheduler.unit_status(
        repo, installed, max_concurrency=1, sched=sched,
    )
    assert status[0]["drifted"] is False
    assert status[0]["reload_pending"] is True

    # The drift check sees the deferred reload (and says why).
    caplog.clear()
    with caplog.at_level("INFO"):
        with pytest.raises(
            scheduler.UnitDriftError, match="deferred to the next idle tick",
        ) as excinfo:
            scheduler.check_unit_drift(
                repo, installed, max_concurrency=1, sched=sched,
            )
    assert "reload_pending=true" in str(excinfo.value)
    assert "unit_drift result=reload_pending" in caplog.text

    # Issue #1347 acceptance (2): the next cycle, with the instance
    # idle, bootouts + bootstraps it and the drift is clean afterwards.
    fake.state["org.orbi.runner.1"] = "not running"
    fake.commands.clear()
    scheduler.sync_drifted_units(
        repo, installed, max_concurrency=1, run_command=fake, sched=sched,
    )
    assert [
        "launchctl", "bootout", "gui/501/org.orbi.runner.1",
    ] in fake.commands
    assert [
        "launchctl", "bootstrap", "gui/501",
        str(installed / "org.orbi.runner.1.plist"),
    ] in fake.commands
    status = scheduler.unit_status(
        repo, installed, max_concurrency=1, sched=sched,
    )
    assert status[0]["drifted"] is False
    assert status[0]["reload_pending"] is False


def test_pending_reload_is_applied_on_the_next_idle_cycle(
    monkeypatch, tmp_path,
):
    """Issue #1347 acceptance (2): on the next cycle, with instance 1
    idle, it is booted out and bootstrapped and the drift is clean."""
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    installed = tmp_path / "LaunchAgents"
    installed.mkdir()
    name = "org.orbi.runner.1.plist"
    (installed / name).write_bytes(plistlib.dumps({"Label": "x"}))
    # A marker the previous cycle left for the running tick.
    sched.reload_marker(installed, name).write_text(
        "pending\n", encoding="utf-8",
    )
    assert sched.reload_pending(installed, name) is True

    fake = FakeLaunchd()
    fake.load("org.orbi.runner.1", state="not running")
    sched.activate_instances(
        fake, installed, None, max_concurrency=1,
        changed=frozenset({name}),
    )

    assert [
        "launchctl", "bootout", "gui/501/org.orbi.runner.1",
    ] in fake.commands
    assert [
        "launchctl", "bootstrap", "gui/501", str(installed / name),
    ] in fake.commands
    assert sched.reload_pending(installed, name) is False


def test_activate_leaves_the_marker_for_a_running_unchanged_instance(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    installed = tmp_path
    name = "org.orbi.runner.1.plist"
    (installed / name).write_bytes(plistlib.dumps({"Label": "x"}))
    marker = sched.reload_marker(installed, name)
    marker.write_text("pending\n", encoding="utf-8")
    fake = FakeLaunchd()
    fake.load("org.orbi.runner.1", state="running")
    # Running and NOT in `changed`: the marker stays (no churn).
    sched.activate_instances(fake, installed, None, max_concurrency=1)
    assert marker.is_file()
    # A fresh bootstrap (not loaded) clears any stale marker.
    fresh = FakeLaunchd()
    sched.activate_instances(fresh, installed, None, max_concurrency=1)
    assert not marker.is_file()


def test_downscale_clears_a_pending_marker_for_a_disabled_instance(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    installed = tmp_path
    name = "org.orbi.runner.3.plist"
    (installed / name).write_bytes(plistlib.dumps({"Label": "x"}))
    marker = sched.reload_marker(installed, name)
    marker.write_text("pending\n", encoding="utf-8")
    fake = FakeLaunchd()
    sched.activate_instances(fake, installed, None, max_concurrency=2)
    assert "org.orbi.runner.3" in fake.disabled
    assert not marker.is_file()


def test_downscale_disables_the_surplus_and_bootouts_only_the_idle(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    fake = FakeLaunchd()
    # Downscale 5 -> 2 while instance 3 sits loaded and idle;
    # 1, 2 and 4, 5 are not loaded.
    fake.load("org.orbi.runner.3", state="not running")
    sched.activate_instances(fake, tmp_path, None, max_concurrency=2)
    assert "org.orbi.runner.3" in fake.disabled
    assert [
        "launchctl", "bootout", "gui/501/org.orbi.runner.3",
    ] in fake.commands
    assert "org.orbi.runner.3" not in fake.state
    for index in (4, 5):
        assert f"org.orbi.runner.{index}" in fake.disabled
        assert [
            "launchctl", "bootout", f"gui/501/org.orbi.runner.{index}",
        ] not in fake.commands


def test_restart_hint_targets_the_first_instance(monkeypatch):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.restart_hint() == (
        "launchctl kickstart -k gui/501/org.orbi.runner.1"
    )


def test_journal_lines_tail_the_rendered_log_files(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    for index in (1, 2):
        log = launchd_deploy.log_path(tmp_path, f"org.orbi.runner.{index}")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "\n".join(f"line{i}" for i in range(50)) + "\n",
            encoding="utf-8",
        )
    lines = sched.journal_lines(
        None, tmp_path, max_concurrency=2, lines=5,
    )
    assert lines == ["line45", "line46", "line47", "line48", "line49"] * 2


def test_install_and_drift_flow_end_to_end_on_the_launchd_impl(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(launchd_deploy.os, "getuid", lambda: 501)
    repo = make_repo(tmp_path)
    installed = tmp_path / "LaunchAgents"
    sched = launchd_deploy.LaunchdScheduler()
    fake = FakeLaunchd(commit="beefbeef")

    result = scheduler.install_units(
        repo, installed, max_concurrency=2,
        run_command=fake, sched=sched,
    )
    assert sorted(result["units"]) == [
        "org.orbi.runner.1.plist", "org.orbi.runner.2.plist",
    ]
    assert result["commit"] == "beefbeef"
    assert fake.state == {
        "org.orbi.runner.1": "not running",
        "org.orbi.runner.2": "not running",
    }
    scheduler.check_unit_drift(
        repo, installed, max_concurrency=2, sched=sched,
    )
    # KEY-ORDER-ONLY tampering must NOT fabricate drift (the XML
    # comparison is semantic).
    unit = installed / "org.orbi.runner.1.plist"
    reordered = plistlib.loads(unit.read_bytes())
    reordered = dict(reversed(list(reordered.items())))
    unit.write_bytes(plistlib.dumps(reordered))
    status = scheduler.unit_status(
        repo, installed, max_concurrency=2, sched=sched,
    )
    assert not status[0]["drifted"]
    # A VALUE change is drift, with the structured line naming the plist.
    reordered["StartCalendarInterval"] = [{"Minute": 4}]
    unit.write_bytes(plistlib.dumps(reordered))
    with pytest.raises(scheduler.UnitDriftError) as excinfo:
        scheduler.check_unit_drift(
            repo, installed, max_concurrency=2, sched=sched,
        )
    assert "unit_drift unit=org.orbi.runner.1.plist" in str(excinfo.value)


def test_unmanaged_entries_list_foreign_orbi_plists(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    installed = tmp_path
    (installed / "org.orbi.runner.1.plist").write_bytes(
        plistlib.dumps({"Label": "org.orbi.runner.1"}),
    )
    (installed / "org.orbi.rogue.plist").write_bytes(
        plistlib.dumps({
            "Label": "org.orbi.rogue",
            "EnvironmentVariables": {
                "ORBI_CONFIG": "/somewhere/orbi.toml",
            },
        }),
    )
    (installed / "com.other.plist").write_bytes(
        plistlib.dumps({"Label": "com.other"}),
    )
    entries = sched.unmanaged_entries(installed)
    assert [entry["unit"] for entry in entries] == ["org.orbi.rogue.plist"]
    assert entries[0]["config"] == Path("/somewhere/orbi.toml")


def test_genuine_launchctl_failure_propagates():
    # A launchctl failure that is NOT the documented not-loaded answer
    # must re-raise (fail fast), never map to a state.
    fake = FakeLaunchd()
    fake.commands = []  # silence recording

    def broken(command, **kwargs):
        fake.commands.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    sched = launchd_deploy.LaunchdScheduler()
    with pytest.raises(subprocess.CalledProcessError):
        sched.unit_state(broken, "org.orbi.runner.1")


def test_installed_unit_dir_follows_the_override_chain(monkeypatch, tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    # The explicit argument wins.
    assert sched.installed_unit_dir(str(tmp_path)) == tmp_path
    # Then $ORBI_UNIT_DIR (the test/e2e seam).
    monkeypatch.setenv("ORBI_UNIT_DIR", str(tmp_path / "env"))
    assert sched.installed_unit_dir() == tmp_path / "env"
    # The macOS default is the user's LaunchAgents directory.
    monkeypatch.delenv("ORBI_UNIT_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert sched.installed_unit_dir() == (
        tmp_path / "home" / "Library" / "LaunchAgents"
    )


def test_content_sha_falls_back_to_raw_bytes_for_unparseable_render():
    sched = launchd_deploy.LaunchdScheduler()
    import hashlib

    junk = "<not a plist"
    assert sched.content_sha(junk) == hashlib.sha256(
        junk.encode("utf-8")
    ).hexdigest()


def test_unit_config_ignores_a_non_string_orbi_config(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    unit = tmp_path / "weird.plist"
    unit.write_bytes(plistlib.dumps({
        "EnvironmentVariables": {"ORBI_CONFIG": 12345},
    }))
    assert sched.unit_config(unit) is None


def test_unit_state_without_a_state_line_is_inactive():
    sched = launchd_deploy.LaunchdScheduler()

    def no_state_line(command, **kwargs):
        return "\tpid = 7\n"

    assert sched.unit_state(no_state_line, "org.orbi.runner.1") == "inactive"


def test_instances_status_reports_enabled_active_and_an_honest_next():
    fake = FakeLaunchd()
    sched = launchd_deploy.LaunchdScheduler()
    fake.load("org.orbi.runner.1", state="running")
    fake.load("org.orbi.runner.2", state="not running")
    fake.disabled.add("org.orbi.runner.2")
    status = sched.instances_status(fake, None, max_concurrency=2)
    assert status == {
        "org.orbi.runner.1": {
            "enabled": True, "active": True, "next": "-", "schedule": "*-*-* *:00/5",
        },
        "org.orbi.runner.2": {
            "enabled": False, "active": False, "next": "-", "schedule": "*-*-* *:02/5",
        },
    }


def test_launchd_instances_are_staggered_on_the_wall_clock(tmp_path):
    """Issue #1320: at N > 1 every instance moves off `StartInterval`
    (which counts from the job's LOAD time, so instances bootstrapped
    together fire in the same second) onto the wall-clock
    `StartCalendarInterval` grid, shifted by the deterministic offset.

    `StartCalendarInterval` carries whole minutes only — launchd.plist(5)
    documents Minute/Hour/Day/Weekday/Month and launchd's parser reads
    those five, silently ignoring anything else (a `Second` key would
    make instance 2 fire at the same second as instance 1, only on
    minute 2). The 150s offset therefore lands as 2 minutes; what the
    two schedulers must agree on is the wall-clock grid, not a
    sub-minute value launchd cannot express.
    """
    sched = launchd_deploy.LaunchdScheduler()
    template = (
        REPO_ROOT / "launchd" / launchd_deploy.TEMPLATE_NAME
    ).read_text(encoding="utf-8")

    plists = {
        instance: plistlib.loads(sched.render_unit(
            template, tmp_path, None, instance=instance, max_concurrency=2,
        ).encode("utf-8"))
        for instance in (1, 2)
    }

    for instance, plist in plists.items():
        assert "StartInterval" not in plist, instance
        calendar = plist["StartCalendarInterval"]
        # Documented keys only: a `Second` entry is not a
        # calendar-interval key and is ignored by launchd.
        assert all(set(entry) == {"Minute"} for entry in calendar), calendar
    # Instance 1 anchors the 5-minute grid (offset 0, the systemd
    # OnCalendar=*-*-* *:00/5 tick), instance 2 sits two minutes into it.
    assert [e["Minute"] for e in plists[1]["StartCalendarInterval"]] == list(range(0, 60, 5))
    assert [e["Minute"] for e in plists[2]["StartCalendarInterval"]] == list(range(2, 60, 5))

    # A single instance has nothing to stagger: the template's
    # StartInterval is left untouched.
    alone = plistlib.loads(sched.render_unit(
        template, tmp_path, None, instance=1, max_concurrency=1,
    ).encode("utf-8"))
    assert alone["StartInterval"] == 300
    assert "StartCalendarInterval" not in alone

    # The schedule the report shows is the one the plist deploys.
    assert sched.schedule_text(1, 2) == "*-*-* *:00/5"
    assert sched.schedule_text(2, 2) == "*-*-* *:02/5"
    assert launchd_deploy.calendar_minute_offset(2, 3) == 1
    assert launchd_deploy.calendar_minute_offset(3, 3) == 3

    # Issue #1344: the report reads the installed schedule (the plist's
    # own whole-minute value), and launchd has no non-template installed
    # file for the drift check to compare.
    assert sched.installed_schedule(tmp_path, None, 2, 2) == "*-*-* *:02/5"
    assert sched.extra_drift(tmp_path, None, 2) == []


def test_journal_lines_skip_instances_without_a_log_file(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    only = launchd_deploy.log_path(tmp_path, "org.orbi.runner.2")
    only.parent.mkdir(parents=True)
    only.write_text("a\nb\n", encoding="utf-8")
    lines = sched.journal_lines(None, tmp_path, max_concurrency=2)
    assert lines == ["a", "b"]


def test_unmanaged_entries_is_empty_without_the_directory(tmp_path):
    sched = launchd_deploy.LaunchdScheduler()
    assert sched.unmanaged_entries(tmp_path / "absent") == []


def test_fake_launchd_mirrors_the_real_launchctl_contract():
    fake = FakeLaunchd()
    # Unknown subcommands fail loudly, never silently succeed.
    with pytest.raises(subprocess.CalledProcessError):
        fake(["launchctl", "bogus"])
    # Commands the fake does not model answer empty (e.g. git plumbing
    # other than rev-parse).
    assert fake(["git", "status"]) == ""
    # Bootstrap of an already-loaded label fails (the EIO contract).
    fake.load("org.orbi.runner.1")
    with pytest.raises(subprocess.CalledProcessError):
        fake([
            "launchctl", "bootstrap", "gui/501",
            "/Lib/LaunchAgents/org.orbi.runner.1.plist",
        ])
    # Bootout of a not-loaded label fails with the not-found contract.
    with pytest.raises(subprocess.CalledProcessError):
        fake(["launchctl", "bootout", "gui/501/org.orbi.runner.9"])
    # kickstart is accepted and recorded (the restart entry).
    assert fake(["launchctl", "kickstart", "gui/501/org.orbi.runner.1"]) == ""
    assert [
        "launchctl", "kickstart", "gui/501/org.orbi.runner.1",
    ] in fake.commands
