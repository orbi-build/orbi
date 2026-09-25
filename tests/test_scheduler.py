"""The scheduler layer (Issue #849).

``orbi.scheduler`` is the platform-neutral front door for every
scheduling operation (install, activate/deactivate, state queries,
drift check and sync). The platform implementations (``systemd_deploy``
for Linux, ``launchd_deploy`` for macOS) provide the hooks; the
orchestration here must be provably implementation-agnostic, so the
drift/install flow is exercised against an injected FAKE scheduler —
the same seam that lets the launchd branch be unit-tested on Linux CI.
"""
import hashlib
from pathlib import Path

import pytest

from orbi import launchd_deploy, scheduler, systemd_deploy


def make_repo(tmp_path: Path, template_dir: str = "fake-templates") -> Path:
    repo = tmp_path / "repo"
    (repo / template_dir).mkdir(parents=True)
    (repo / template_dir / "fake@.service").write_text(
        "[Service]\nExecStart={{R}}/bin/orbi\n", encoding="utf-8",
    )
    (repo / template_dir / "fake@.timer").write_text(
        "[Timer]\nOnCalendar=*\n", encoding="utf-8",
    )
    return repo


class FakeScheduler:
    """The injectable fake: the hooks the generic orchestration calls,
    observable and deterministic."""

    name = "fake"
    display = "fake"
    template_dir = "fake-templates"

    def __init__(self, tmp_path: Path):
        self.pairs = [
            ("fake@.service", "fake@.service"),
            ("fake@.timer", "fake@.timer"),
        ]
        self.calls: list[tuple] = []
        self.installed_root = tmp_path / "installed"
        self.config_override: Path | None = None
        self.damage_on_activate = False

    def unit_pairs(self, unit_name, count):
        return list(self.pairs)

    def timer_instances(self, unit_name=None, count=1):
        return tuple(f"fake@{i}.timer" for i in range(1, count + 1))

    def render_unit(self, template_text, repo_dir, unit_name=None, instance=1):
        return (
            template_text.replace("{{R}}", str(Path(repo_dir).resolve()))
            + f"#rendered-{instance}\n"
        )

    def content_sha(self, rendered: str) -> str:
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def installed_sha(self, path: Path) -> str | None:
        path = Path(path)
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def unit_config(self, path: Path) -> Path | None:
        return self.config_override

    def activate_instances(self, run_command, installed_dir,
                           unit_name=None, *, max_concurrency=1):
        self.calls.append(("activate", max_concurrency))
        if self.damage_on_activate:
            for _, name in self.pairs:
                path = Path(installed_dir) / name
                path.write_text(
                    path.read_text(encoding="utf-8") + "# tampered\n",
                    encoding="utf-8",
                )

    def pre_install(self, run_command, installed_dir, unit_name=None):
        self.calls.append(("pre_install",))


def recording_run_command(command, **kwargs):
    """The seam stub: the generic orchestration only runs `git
    rev-parse HEAD` itself (the hooks receive this same callable),
    and it reports a fixed commit."""
    return "cafecafe"


def test_dispatch_maps_linux_to_systemd():
    sched = scheduler.detect("Linux")
    assert isinstance(sched, systemd_deploy.SystemdScheduler)
    assert sched.name == "systemd"
    assert sched.template_dir == "systemd"


def test_dispatch_maps_darwin_to_launchd():
    sched = scheduler.detect("Darwin")
    assert isinstance(sched, launchd_deploy.LaunchdScheduler)
    assert sched.name == "launchd"
    assert sched.template_dir == "launchd"


def test_dispatch_defaults_to_the_running_platform():
    # The default must be the REAL running platform, not a hardcoded
    # one: the suite runs on Linux CI and on the hosted macOS runner
    # alike, and each must see its own scheduler.
    from platform import system as platform_system

    expected = {"Darwin": "launchd"}.get(platform_system(), "systemd")
    assert scheduler.detect().name == expected


def test_dispatch_rejects_unsupported_platforms_with_the_issue_link():
    with pytest.raises(
        scheduler.UnsupportedPlatformError, match="FreeBSD",
    ) as excinfo:
        scheduler.detect("FreeBSD")
    message = str(excinfo.value)
    assert "Linux" in message and "macOS" in message
    assert scheduler.ISSUE_URL in message


def test_install_units_writes_rendered_units_through_the_hooks(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    result = scheduler.install_units(
        repo, fake.installed_root, max_concurrency=2,
        run_command=recording_run_command, sched=fake,
    )
    # Both managed units exist, RENDERED per instance (pair position
    # drives the instance index).
    for index, (_, name) in enumerate(fake.pairs, start=1):
        installed = Path(fake.installed_root) / name
        text = installed.read_text(encoding="utf-8")
        assert "{{R}}" not in text
        assert f"#rendered-{index}\n" in text
        assert result["units"][name]["sha256"] == (
            fake.content_sha(text)
        )
    assert result["commit"] == "cafecafe"
    assert ("pre_install",) in fake.calls
    assert ("activate", 2) in fake.calls


def test_install_units_rejects_capacity_outside_the_declared_range(tmp_path):
    fake = FakeScheduler(tmp_path)
    for bad in (0, scheduler.MAX_RUNNER_INSTANCES + 1):
        with pytest.raises(ValueError, match=rf"got {bad!r}"):
            scheduler.install_units(
                tmp_path, fake.installed_root, max_concurrency=bad,
                run_command=recording_run_command, sched=fake,
            )


def test_install_units_fails_fast_on_a_missing_template(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "fake-templates" / "fake@.timer").unlink()
    fake = FakeScheduler(tmp_path)
    with pytest.raises(FileNotFoundError, match="fake@.timer"):
        scheduler.install_units(
            repo, fake.installed_root, max_concurrency=1,
            run_command=recording_run_command, sched=fake,
        )


def test_install_units_rejects_a_foreign_deployment(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    fake.config_override = tmp_path / "other" / "orbi.toml"
    with pytest.raises(scheduler.UnitConflictError, match="ORBI_CONFIG"):
        scheduler.install_units(
            repo, fake.installed_root, max_concurrency=1,
            run_command=recording_run_command, sched=fake,
        )


def test_unit_status_reports_clean_drifted_and_missing(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    scheduler.install_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    # Tamper with ONE installed unit and delete the other.
    service = Path(fake.installed_root) / "fake@.service"
    service.write_text(
        service.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8",
    )
    (Path(fake.installed_root) / "fake@.timer").unlink()
    status = scheduler.unit_status(
        repo, fake.installed_root, sched=fake,
    )
    by_name = {entry["unit"]: entry for entry in status}
    assert by_name["fake@.service"]["drifted"] is True
    assert by_name["fake@.service"]["missing"] is False
    assert by_name["fake@.timer"]["drifted"] is True
    assert by_name["fake@.timer"]["missing"] is True
    assert by_name["fake@.timer"]["installed_sha256"] is None


def test_unit_status_reports_a_missing_template_as_drift(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "fake-templates" / "fake@.timer").unlink()
    fake = FakeScheduler(tmp_path)
    status = scheduler.unit_status(repo, fake.installed_root, sched=fake)
    timer = next(
        entry for entry in status if entry["unit"] == "fake@.timer"
    )
    assert timer["repo_sha256"] is None
    assert timer["drifted"] is True


def test_drift_lines_carry_paths_hashes_and_the_fix_command(tmp_path):
    from orbi.progress import quote_value

    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    installed = tmp_path / "installed dir with spaces"
    fake.installed_root = installed
    status = scheduler.unit_status(repo, installed, sched=fake)
    lines = scheduler.drift_lines(status)
    assert len(lines) == 2
    for line, (_, name) in zip(lines, fake.pairs):
        assert line.startswith(f"unit_drift unit={name} ")
        assert (
            f"repo={quote_value(str(repo / 'fake-templates' / name))}"
            in line
        )
        assert f"installed={quote_value(str(installed / name))}" in line
        assert "repo_sha256=" in line and "installed_sha256=" in line
        assert "fix=orbi install-units" in line


def test_check_unit_drift_is_clean_after_a_fresh_install(
    tmp_path, caplog,
):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    scheduler.install_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    scheduler.check_unit_drift(repo, fake.installed_root, sched=fake)


def test_check_unit_drift_raises_with_the_structured_line(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    scheduler.install_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    timer = Path(fake.installed_root) / "fake@.timer"
    timer.write_text(
        timer.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8",
    )
    with pytest.raises(scheduler.UnitDriftError) as excinfo:
        scheduler.check_unit_drift(repo, fake.installed_root, sched=fake)
    message = str(excinfo.value)
    assert "unit_drift unit=fake@.timer" in message
    assert "fix=orbi install-units" in message


def test_sync_drifted_units_repairs_and_reports(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    scheduler.install_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    timer = Path(fake.installed_root) / "fake@.timer"
    before_sha = fake.installed_sha(timer)
    timer.write_text(
        timer.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8",
    )
    report = scheduler.sync_drifted_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    # The report covers EVERY managed unit with its before/after
    # identity; only the tampered unit actually changed.
    assert [entry["unit"] for entry in report] == [
        "fake@.service", "fake@.timer",
    ]
    timer_entry = report[1]
    # before = the drifted bytes the sync started from;
    # after = repaired back to the clean install identity.
    assert timer_entry["before_sha256"] != before_sha
    assert timer_entry["after_sha256"] == before_sha
    assert timer_entry["after_sha256"] == fake.installed_sha(timer)
    assert timer_entry["commit"] == "cafecafe"
    status = scheduler.unit_status(repo, fake.installed_root, sched=fake)
    assert not any(entry["drifted"] for entry in status)


def test_sync_drifted_units_is_a_no_op_when_clean(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    scheduler.install_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    fake.calls.clear()
    report = scheduler.sync_drifted_units(
        repo, fake.installed_root, max_concurrency=1,
        run_command=recording_run_command, sched=fake,
    )
    assert report == []
    assert fake.calls == []  # nothing was reinstalled


def test_sync_drifted_units_still_drifted_after_sync_fails_fast(tmp_path):
    repo = make_repo(tmp_path)
    fake = FakeScheduler(tmp_path)
    fake.damage_on_activate = True
    with pytest.raises(scheduler.UnitDriftError, match="still drift"):
        scheduler.sync_drifted_units(
            repo, fake.installed_root, max_concurrency=1,
            run_command=recording_run_command, sched=fake,
        )


def test_systemd_impl_keeps_the_user_unit_dir_contract(monkeypatch,
                                                      tmp_path):
    monkeypatch.delenv("ORBI_UNIT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sched = systemd_deploy.SystemdScheduler()
    assert sched.installed_unit_dir() == (
        tmp_path / "home" / ".config" / "systemd" / "user"
    )


def test_systemd_impl_exposes_its_template_units():
    assert systemd_deploy.SystemdScheduler().template_units() == (
        "orbi@.service", "orbi@.timer",
    )
