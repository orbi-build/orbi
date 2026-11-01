"""Milestone scene: active-milestone bookkeeping and the release arm (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: syncing the `active_milestone`
config variable, rewriting the config line atomically (symlinks, killed
mid-write), selecting/advancing the next milestone, the pending P0
notification Issue, arming the matching release ticket, and the message
language the milestone bookkeeping comment is posted in.

`_write_prompts` (the shared prompt-file scaffold) is owned by
`tests/test_bootstrap_runner_main.py` and imported here.
"""

from orbi import config as config_domain
import json
import logging
import os
import subprocess

import threading
import time
from pathlib import Path
import pytest

import orbi.claim as claim

import orbi.runner as runner
import orbi.milestone as milestone
from tests.fakes.github import FakeGh
from seam import seam
from tests.test_bootstrap_runner_main import _write_prompts


def test_sync_active_milestone_variable_skips_matching_value():
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v1"}'

    milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert len(calls) == 1
    # The read may 404 by design (no variable yet): its generic
    # command_failed line must stay at DEBUG (Issue #1085).
    assert calls[0][1] == {"timeout": 30, "failure_log_level": logging.DEBUG}


def test_sync_active_milestone_variable_patches_different_value():
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v0"}' if len(calls) == 1 else "{}"

    milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert calls[1][:5] == ["gh", "api", "-X", "PATCH", "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE"]
    assert "value=v1" in calls[1]


def test_sync_active_milestone_variable_creates_missing_value(caplog, monkeypatch):
    """Read 404 + a configured milestone: the real run_command path issues
    the POST and the generic command_failed line from the 404 read stays
    below INFO (Issue #1085)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, cmd, output="",
                stderr="gh: Not Found (HTTP 404)",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG):
        milestone.sync_active_milestone_variable("owner/repo", "v1")
    assert calls[1][:4] == ["gh", "api", "-X", "POST"]
    assert "value=v1" in calls[1]
    failed = [
        record for record in caplog.records
        if "command_failed" in record.getMessage()
    ]
    assert failed and all(record.levelno == logging.DEBUG for record in failed)
    assert "active_milestone_variable_created repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_create_failure_is_bypassed(caplog):
    calls = 0

    def command(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 404")
        raise RuntimeError("create denied")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable(
            "owner/repo", "v1", run_command=command,
        )

    assert calls == 2
    assert "active_milestone_variable_sync_failed repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_removes_stale_value():
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return '{"name":"ORBI_ACTIVE_MILESTONE","value":"v0"}' if len(calls) == 1 else "{}"

    milestone.sync_active_milestone_variable("owner/repo", None, run_command=command)
    assert calls[1] == [
        "gh", "api", "-X", "DELETE",
        "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE",
    ]


def test_sync_active_milestone_variable_absent_on_404(caplog, monkeypatch):
    """Removing an already-absent variable is a scene, not a failure: the
    read goes through the REAL run_command, its 404's generic
    command_failed line stays at DEBUG, and the structured `absent` line
    owns the outcome (Issue #1085)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        raise subprocess.CalledProcessError(
            1, cmd,
            output=('{"message":"Not Found",'
                    '"documentation_url":"https://docs.github.com/rest/'
                    'actions/variables#get-a-repository-variable",'
                    '"status":"404"}'),
            stderr="gh: Not Found (HTTP 404)",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG):
        milestone.sync_active_milestone_variable("owner/repo", None)
    assert calls == [
        ["gh", "api", "repos/owner/repo/actions/variables/ORBI_ACTIVE_MILESTONE"],
    ]
    failed = [
        record for record in caplog.records
        if "command_failed" in record.getMessage()
    ]
    assert failed and all(record.levelno == logging.DEBUG for record in failed)
    assert "active_milestone_variable_absent repo=owner/repo" in caplog.text


def test_sync_active_milestone_variable_read_403_logs_sync_failed_at_error(caplog):
    def command(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable(
            "owner/repo", None, run_command=command,
        )
    failed = [
        record for record in caplog.records
        if "active_milestone_variable_sync_failed" in record.getMessage()
    ]
    assert failed
    assert all(record.levelno == logging.ERROR for record in failed)


def test_sync_active_milestone_variable_failure_is_bypass(caplog):
    def command(args, **kwargs):
        raise RuntimeError("offline")

    with caplog.at_level("ERROR"):
        milestone.sync_active_milestone_variable("owner/repo", "v1", run_command=command)
    assert "active_milestone_variable_sync_failed" in caplog.text


def test_main_epic_only_queue_ends_idle_without_process_issue(
    monkeypatch, tmp_path, caplog,
):
    """Issue #93: when the ready queue contains only `ai-epic`
    Issues, the tick ends idle (`no_ready_issue`): no claim, no
    worktree, no `run_pi` — an Epic never enters the delivery
    pipeline and never occupies a slot through a delivery."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    epic = {
        "number": 80, "title": "v0.1 release checklist", "body": "",
        "labels": [{"name": "ai-epic"}, {"name": "ai-ready"}],
        "blockedBy": {"nodes": [], "totalCount": 0},
    }

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            search = command[command.index("--search") + 1]
            # The resumable-PR and in-flight restart scans are idle;
            # the ready scans see the Epic.
            if search.startswith("label:ai-fix-needed") or search.startswith(
                "label:ai-ready label:ai-in-progress",
            ):
                return "[]"
            return json.dumps([epic])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not issue-list traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])

    def fail_process(*args, **kwargs):
        raise AssertionError("process_issue must not run for an Epic")

    monkeypatch.setattr(runner, "process_issue", fail_process)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="must not run for an Epic"):
        fail_process()
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "no_ready_issue" in caplog.text
    assert "epic_not_claimed" in caplog.text
    assert "issue=80" in caplog.text


def test_parse_milestone_version_accepts_only_strict_semver():
    assert runner._parse_version_title(None) is None
    assert runner._parse_version_title("v0.3.1") == (0, 3, 1)
    assert runner._parse_version_title("v01.3.1") is None
    assert runner._parse_version_title("1.3.1") is None
    assert runner._parse_version_title("v1.2.3.4") is None


def _advance_active_milestone_on_idle(*args, **kwargs):
    kwargs.setdefault("parse_version_title", runner._parse_version_title)
    return milestone.advance_active_milestone_on_idle(*args, **kwargs)


def test_rewrite_active_milestone_line_preserves_all_other_bytes(tmp_path):
    config = tmp_path / "orbi.toml"
    original = '# keep \r\nsource_repos = ["owner/repo"]\r\nactive_milestone = \'v0.3.0\'\r\nother = "x"\r\n'
    config.write_bytes(original.encode())
    milestone.rewrite_active_milestone_line(config, "v0.3.1")
    assert config.read_bytes() == original.replace(
        "active_milestone = 'v0.3.0'", 'active_milestone = "v0.3.1"',
    ).encode()


@pytest.mark.parametrize("via_symlink", [False, True])
def test_rewrite_active_milestone_line_keeps_the_old_config_when_killed(
        tmp_path, monkeypatch, via_symlink):
    """Issue #1851: a kill between truncate and write left a truncated
    orbi.toml, and every later tick failed config load. The rewrite is
    tmp+rename now, so a failure at the rename keeps the previous file
    byte-identical and loadable.

    Issue #1872: the same holds when the config is a symlink alias — the
    tmp file and the rename operate beside the RESOLVED target, so the
    failure keeps the real config's bytes AND leaves the link a link."""
    real = tmp_path / "deployment"
    real.mkdir()
    config = real / "orbi.toml"
    original = (
        '# keep\nsource_repos = ["owner/repo"]\n'
        'active_milestone = "v0.3.0"\nother = "x"\n'
    )
    config.write_text(original, encoding="utf-8")
    written = config
    alias = None
    if via_symlink:
        alias_dir = tmp_path / "slot"
        alias_dir.mkdir()
        alias = alias_dir / "alias.toml"
        alias.symlink_to(os.path.join("..", "deployment", "orbi.toml"))
        written = alias
    renames: list[tuple[str, str]] = []

    def killed(src, dst):
        renames.append((str(src), str(dst)))
        raise OSError("killed before the rename")

    # The stdlib rename boundary itself, never a src-module symbol.
    monkeypatch.setattr(os, "replace", killed)
    with pytest.raises(OSError, match="killed before the rename"):
        milestone.rewrite_active_milestone_line(written, "v0.3.1")
    # Both rename ends sit beside the resolved target, never beside the
    # alias: that is what keeps a symlink a symlink.
    assert len(renames) == 1
    source, destination = renames[0]
    assert Path(source).parent == config.resolve().parent
    assert Path(destination) == config.resolve()
    assert config.read_text(encoding="utf-8") == original
    loaded = config_domain.load_config(config)
    assert loaded.active_milestone == "v0.3.0"
    if alias is not None:
        assert alias.is_symlink()
        assert config_domain.load_config(alias).active_milestone == "v0.3.0"


def test_rewrite_active_milestone_line_keeps_a_symlink_alias_intact(tmp_path):
    """Issue #1872: the config may be a symlink alias (a relative stored
    target pointing into another directory). The atomic write must land in
    the RESOLVED target, never replace the link with a regular file."""
    real = tmp_path / "deployment"
    real.mkdir()
    alias_dir = tmp_path / "slot"
    alias_dir.mkdir()
    config = real / "orbi.toml"
    original = (
        '# keep this comment\r\nsource_repos = ["owner/repo"]\r\n'
        "active_milestone = 'v0.3.0'\r\nother = \"x\"\r\n"
    )
    config.write_bytes(original.encode())
    alias = alias_dir / "alias.toml"
    stored_target = os.path.join("..", "deployment", "orbi.toml")
    alias.symlink_to(stored_target)

    milestone.rewrite_active_milestone_line(alias, "v0.3.1")

    # The alias is still the same relative symlink ...
    assert alias.is_symlink()
    assert os.readlink(alias) == stored_target
    expected = original.replace(
        "active_milestone = 'v0.3.0'", 'active_milestone = "v0.3.1"',
    )
    # ... the original file holds the new bytes, every other byte intact ...
    assert config.read_bytes() == expected.encode()
    assert alias.read_bytes() == expected.encode()
    # ... and no tmp file is left behind on either path.
    assert sorted(path.name for path in real.iterdir()) == ["orbi.toml"]
    assert sorted(path.name for path in alias_dir.iterdir()) == ["alias.toml"]
    assert config_domain.load_config(alias).active_milestone == "v0.3.1"
    assert config_domain.load_config(config).active_milestone == "v0.3.1"


def test_rewrite_active_milestone_line_fails_when_missing(tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="active_milestone line not found"):
        milestone.rewrite_active_milestone_line(config, "v0.3.1")


def test_advance_active_milestone_pending_creates_one_p0_ready_issue(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    milestones = [
        {"title": "v0.3.0", "state": "closed"},
        {"title": "v0.3.1", "state": "open", "open_issues": 2},
        {"title": "v0.4.0", "state": "open", "open_issues": 5},
    ]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return json.dumps([milestones])

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config, auto_next_milestone=False,
        ) == ("closed", None)
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'
    create = [command for command in calls if command[:3] == ["gh", "issue", "create"]]
    assert len(create) == 1
    assert "--label" not in create[0]
    assert "`v0.3.1`: 2 open issues" in create[0][create[0].index("--body") + 1]
    # Issue #1290/#1922: the human instruction names the in-ticket command
    # a hosted tenant can actually use (no host CLI, no config edit), in
    # its canonical `/orbi` form.
    assert "/orbi milestone" in create[0][create[0].index("--body") + 1]
    assert "active_milestone_advance_pending old=v0.3.0" in caplog.text
    assert "auto_next_milestone=false" in caplog.text


def test_advance_active_milestone_manual_notification_is_not_pickupable(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or (
            "[]" if command[:3] == ["gh", "issue", "list"] else json.dumps([[
                {"title": "v0.3.0", "state": "closed"},
                {"title": "v0.4.0", "state": "open"},
            ]])
        ),
    )

    _advance_active_milestone_on_idle(
        "owner/repo", "v0.3.0", config, auto_next_milestone=False,
    )

    create = next(command for command in calls if command[:3] == ["gh", "issue", "create"])
    assert "--label" not in create


def test_advance_active_milestone_closes_old_manual_notification_after_manual_move(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([
                {"number": "bad", "body": "orbi-milestone-advance old=v0.2.0"},
                {"number": 492, "body": "not a milestone notice"},
                {
                    "number": 493,
                    "body": "orbi-milestone-advance old=v0.3.0 candidates=v0.4.0",
                },
            ])
        return json.dumps([[{"title": "v0.4.0", "state": "open"}]])

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.4.0", config, auto_next_milestone=False,
        language_code="zh",
    ) == ("open", None)
    close = next(command for command in calls if command[:3] == ["gh", "issue", "close"])
    assert close == [
        "gh", "issue", "close", "493", "--repo", "owner/repo",
        "--comment", "已收敛：当前配置 active_milestone = `v0.4.0`。",
    ]
    # Issue #1704: the same receipt in the repository's English language.
    calls.clear()
    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.4.0", config, auto_next_milestone=False,
        language_code="en",
    ) == ("open", None)
    close = next(command for command in calls if "--comment" in command)
    assert close[close.index("--comment") + 1] == (
        "Settled: the configured active_milestone is `v0.4.0`."
    )


def test_stale_notice_comment_table():
    """Issue #856: the pure close/keep decision for one decision notice."""
    active = "v0.4.0"
    release_body = "orbi-milestone-advance old=v0.4.0 candidates=v0.4.0"
    advance_body = "orbi-milestone-advance old=v0.4.0 candidates=v0.5.0"
    stale_body = "orbi-milestone-advance old=v0.3.0 candidates=v0.4.0"
    close = "已收敛：当前配置 active_milestone = `v0.4.0`。"
    confirmed = (
        "已收敛：Milestone `v0.4.0` 的 release ticket"
        " 已存在，不再等待发布确认。"
    )
    wait_over = "已收敛：Milestone `v0.4.0` 不再等待发布确认。"

    def zh(body, **kwargs):
        return milestone._stale_notice_comment(
            body, active, language_code="zh", **kwargs,
        )

    assert zh(release_body, keep_release_notice=True) is None
    assert zh(
        release_body, keep_release_notice=False, release_ticket_exists=True,
    ) == confirmed
    # The wait also ends without a release ticket (the Milestone got new
    # work, was closed, or the opt-in was turned off): the receipt must
    # not claim a ticket that does not exist.
    assert zh(release_body, keep_release_notice=False) == wait_over
    assert zh(
        release_body, keep_release_notice=False, release_ticket_exists=False,
    ) == wait_over
    assert zh(advance_body, keep_release_notice=False) is None
    assert zh(stale_body, keep_release_notice=True) == close
    # A body without a candidates= group keeps the pre-#856 behaviour: it is
    # closed like any advance notice once its old= no longer matches.
    assert zh(
        "orbi-milestone-advance old=v0.3.0", keep_release_notice=True,
    ) == close
    assert zh(None, keep_release_notice=True) is None
    assert zh("no fingerprint here", keep_release_notice=True) is None


def test_advance_active_milestone_close_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")

    def fail_close(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return json.dumps([[{"title": "v0.4.0", "state": "open"}]])
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "run_command", fail_close)
    with caplog.at_level("ERROR"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.4.0", config,
        ) == ("open", None)
    assert "stale_milestone_issue_close_failed repo=owner/repo active=v0.4.0" in caplog.text


def test_advance_active_milestone_closure_is_idempotent_on_repeated_tick(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.4.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or (
            "[]" if command[:3] == ["gh", "issue", "list"] else json.dumps([[
                {"title": "v0.4.0", "state": "open"},
            ]])
        ),
    )

    _advance_active_milestone_on_idle("owner/repo", "v0.4.0", config)
    _advance_active_milestone_on_idle("owner/repo", "v0.4.0", config)

    assert not any(command[:3] == ["gh", "issue", "close"] for command in calls)


def test_advance_active_milestone_pending_issue_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")

    def fail_pending(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        if command[:3] == ["gh", "issue", "create"]:
            raise RuntimeError("GitHub unavailable")
        return json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.3.1", "state": "open", "open_issues": 2},
        ]])

    monkeypatch.setattr(seam, "run_command", fail_pending)
    with caplog.at_level("ERROR"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config, auto_next_milestone=False,
        ) == ("closed", None)
    assert "pending_milestone_issue_failed repo=owner/repo old=v0.3.0" in caplog.text
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'


def test_pending_milestone_issue_serializes_concurrent_search_and_create(
    monkeypatch, tmp_path,
):
    candidates = [{"title": "v0.3.1", "open_issues": 2}]
    open_issues = []
    created = []
    first_search_entered = threading.Event()
    second_call_started = threading.Event()

    def fake_list(repo, *, state, **kwargs):
        snapshot = [{"number": number} for number in open_issues]
        if state == "all" and not first_search_entered.is_set():
            first_search_entered.set()
            assert second_call_started.wait(timeout=1)
            # Give an unlocked second call time to take the same empty
            # snapshot before this first search returns.
            time.sleep(0.05)
        return snapshot

    def fake_run(command, **kwargs):
        assert (command[0], command[1], command[2]) == ("gh", "issue", "create")
        number = 100 + len(created)
        created.append(number)
        open_issues.append(number)
        return f"https://github.com/owner/repo/issues/{number}"

    def call_pending():
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )

    monkeypatch.setattr(seam, "list_issues", fake_list)
    monkeypatch.setattr(seam, "run_command", fake_run)
    first = threading.Thread(target=call_pending, daemon=True)
    second = threading.Thread(
        target=lambda: (second_call_started.set(), call_pending()), daemon=True,
    )
    first.start()
    assert first_search_entered.wait(timeout=1)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert created == [100]
    assert open_issues == [100]


def test_pending_milestone_issue_deduplicates_after_search_lag(
    monkeypatch, tmp_path, caplog,
):
    candidates = [{"title": "v0.3.1", "open_issues": 2}]
    created = []
    open_issues = []
    closed = []

    def fake_list(repo, *, state, **kwargs):
        if state == "all":
            # Model GitHub's search lag: neither initial check sees a
            # just-created Issue, while the post-create check sees both.
            return []
        return [{"number": number} for number in open_issues]

    def fake_run(command, **kwargs):
        if (command[0], command[1], command[2]) == ("gh", "issue", "create"):
            number = 100 + len(created)
            created.append(number)
            open_issues.append(number)
            return f"https://github.com/owner/repo/issues/{number}"
        assert (command[0], command[1], command[2]) == ("gh", "issue", "close")
        closed.append(command)
        open_issues.remove(int(command[3]))
        return ""

    monkeypatch.setattr(seam, "list_issues", fake_list)
    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level(logging.INFO):
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )
        milestone._pending_milestone_issue(
            "owner/repo", "v0.3.0", candidates, tmp_path,
        )

    assert created == [100, 101]
    assert open_issues == [100]
    assert len(closed) == 1
    assert closed[0][-1] == "duplicate of #100"
    assert caplog.text.count("pending_milestone_issue_deduplicated") == 1


def test_advance_active_milestone_pending_is_idempotent(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    responses = [
        json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.3.1", "state": "open", "open_issues": 2},
        ]]),
        '[{"number": 436}]',
        # The confirmation ticket's comments are read for `/milestone`
        # commands; none here, so nothing is applied.
        json.dumps({"comments": []}),
    ]
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: calls.append(command) or responses.pop(0),
    )
    _advance_active_milestone_on_idle(
        "owner/repo", "v0.3.0", config, auto_next_milestone=False,
    )
    assert not any(command[:3] == ["gh", "issue", "create"] for command in calls)


def test_advance_active_milestone_sorts_double_digit_versions_numerically(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.9.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.9.0", "state": "closed"},
            {"title": "v0.10.0", "state": "open"},
            {"title": "v0.11.0", "state": "open"},
        ]]),
    )

    assert _advance_active_milestone_on_idle(
        "owner/repo", "v0.9.0", config,
    ) == ("closed", "v0.10.0")


def test_advance_active_milestone_selects_smallest_higher_open(
    monkeypatch, tmp_path, caplog,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    milestones = [
        {"title": "v0.3.0", "state": "closed", "number": 1},
        {"title": "v0.4.0", "state": "open", "open_issues": 3},
        {"title": "v0.3.1", "state": "open", "open_issues": 8},
        {"title": "not-a-version", "state": "open", "open_issues": 1},
    ]
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([milestones]),
    )
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config,
        ) == ("closed", "v0.3.1")
    assert 'active_milestone = "v0.3.1"\n' == config.read_text()
    assert "active_milestone_advanced old=v0.3.0 new=v0.3.1 closed=v0.3.0" in caplog.text


def test_advance_active_milestone_open_reconciles_notifications_without_write(
    monkeypatch, tmp_path,
):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or json.dumps([[
            {"title": "v0.3.0", "state": "open"},
        ]]),
    )
    assert _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config) == ("open", None)
    assert len(calls) == 2
    assert calls[1][:3] == ["gh", "issue", "list"]
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'


def test_advance_active_milestone_closed_without_candidate_is_unscoped(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391: a closed active Milestone with no higher open candidate
    means no scope. The advance reports the closed-unscoped outcome and logs
    one line, and the configured value is left untouched (the maintainer may
    still create the next Milestone)."""
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.0", "state": "closed"},
            {"title": "v0.2.0", "state": "open", "open_issues": 1},
            {"title": "future", "state": "open", "open_issues": 2},
        ]]),
    )
    with caplog.at_level("INFO"):
        assert _advance_active_milestone_on_idle(
            "owner/repo", "v0.3.0", config,
        ) == (milestone.MILESTONE_CLOSED_UNSCOPED, None)
    assert config.read_text() == 'active_milestone = "v0.3.0"\n'
    assert (
        "active_milestone_closed_unscoped repo=owner/repo closed=v0.3.0"
        in caplog.text
    )


def test_advance_active_milestone_missing_lists_open_milestones(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.1", "state": "open", "open_issues": 8},
        ]]),
    )
    with pytest.raises(RuntimeError, match=r"active_milestone_missing current=v0.3.0; open milestones: v0.3.1\(8\)"):
        _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config)


def test_advance_active_milestone_rejects_duplicate_title(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text('active_milestone = "v0.3.0"\n', encoding="utf-8")
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([[
            {"title": "v0.3.0", "state": "closed", "number": 1},
            {"title": "v0.3.0", "state": "closed", "number": 2},
        ]]),
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        _advance_active_milestone_on_idle("owner/repo", "v0.3.0", config)


def test_arm_release_ticket_adds_ready_to_matching_open_issue(
    monkeypatch, caplog,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 385}])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        milestone.arm_release_ticket("owner/repo", "v0.4.0")

    assert calls == [
        [
            "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
            "--search",
            'label:ai-release -label:ai-ready milestone:"v0.4.0"',
            "--json", "number", "--limit", "200",
        ],
        [
            "gh", "issue", "edit", "385", "--repo", "owner/repo",
            "--add-label", "ai-ready",
        ],
    ]
    assert "release_ticket_armed issue=#385 milestone=v0.4.0" in caplog.text


def test_arm_release_ticket_does_nothing_when_no_ticket_matches(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: calls.append(command) or "[]",
    )

    milestone.arm_release_ticket("owner/repo", "v0.4.0")

    assert len(calls) == 1


def test_arm_release_ticket_rejects_malformed_issue_number(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: '[{"number": "385"}]',
    )

    with pytest.raises(RuntimeError, match="invalid issue number"):
        milestone.arm_release_ticket("owner/repo", "v0.4.0")


def test_arm_release_ticket_uses_custom_dispatch_label(monkeypatch, caplog):
    """A custom-dispatch-label repo must arm its release ticket with that
    label — search exclusion and added label alike.

    The hardcoded ``ai-ready`` armed the ticket with a label the release
    fallback scan never searches for (it searches the repo's dispatch
    label), so the ticket silently stranded; the next idle tick's
    ``-label:ai-ready`` exclusion then hid the polluted ticket from every
    future arm attempt.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 385}])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        milestone.arm_release_ticket(
            "owner/repo", "v0.4.0", dispatch_label="dev-queue",
        )

    assert calls == [
        [
            "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
            "--search",
            'label:ai-release -label:dev-queue milestone:"v0.4.0"',
            "--json", "number", "--limit", "200",
        ],
        [
            "gh", "issue", "edit", "385", "--repo", "owner/repo",
            "--add-label", "dev-queue",
        ],
    ]
    assert "release_ticket_armed issue=#385 milestone=v0.4.0" in caplog.text


def test_main_idle_release_arm_uses_repo_dispatch_label(
    monkeypatch, tmp_path,
):
    """The idle-path arm arms the release ticket with the repo policy's
    dispatch_label — the same label the fallback scan requires before a
    release ticket can ever be claimed. Armed with the host default
    instead, the ticket is invisible to every scan and the next idle
    tick's arm exclusion hides it for good (silent stranding)."""
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.add_issue(385, title="Release v0.4.0",
                   labels=("ai-release",), milestone=1)
    fake.set_repo_config('dispatch_label = "dev-queue"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )

    assert runner.main(["--config", str(config)]) == 0
    # Public surface: the ticket carries the repo's dispatch label, so
    # the release fallback scan can see and claim it.
    assert fake.issues[385]["labels"] == ["ai-release", "dev-queue"]


def test_main_idle_advance_uses_the_repos_fused_base_branch(
    monkeypatch, tmp_path,
):
    """Issue #1290: the idle path hands the milestone command the repo's
    FUSED base branch, never the raw host value.

    `resolve_source_base_branch` is the documented single source of a
    repository's effective base branch ("Every consumer (dev path AND the
    release state machine) reads `config.base_branch` and must never
    re-derive it from the raw entry"): the raw host value skips both the
    `[[repositories]]` entry fallback and the repository policy. The
    command writes that value into the release ticket it opens, and the
    release state machine freezes the DECLARED branch — so a raw value
    releases the wrong branch."""
    _write_prompts(tmp_path)
    checkout = tmp_path / "checkouts" / "pilot"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config('base_branch = "release/1.x"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(
        claim, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        'active_milestone = "v0.4.0"\n'
        "[[repositories]]\n"
        'name = "pilot"\n'
        'path = "checkouts/pilot"\n'
        'github = "owner/repo"\n'
        'base_branch = "develop"\n',
        encoding="utf-8",
    )

    assert runner.main(["--config", str(config)]) == 0

    # The policy layer overrides the entry, and neither of them is the
    # raw host value the idle path used to pass through.
    assert seen["base_branch"] == "release/1.x"


def test_main_idle_release_arm_failure_is_bypassed(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("permission denied")),
    )
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)

    with caplog.at_level("ERROR"):
        assert runner.main(["--config", str(config)]) == 0

    assert "release_ticket_arm_failed" in caplog.text
    assert "permission denied" in caplog.text


def test_main_idle_advance_failure_is_bypassed(monkeypatch, tmp_path, caplog):
    """Issue #614: the idle milestone advance is a pure bypass — a missing
    or ambiguous `active_milestone` (or any `gh` failure) must not change
    the idle outcome; the tick still returns 0 and only journals."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "active_milestone_missing current=v0.4.0; "
                "open milestones: v0.5.0(2)"
            )
        ),
    )

    with caplog.at_level("ERROR"):
        assert runner.main(["--config", str(config)]) == 0

    assert "active_milestone_advance_failed" in caplog.text
    assert "active_milestone_missing" in caplog.text


def test_main_idle_passes_the_fused_message_language_to_milestone_bookkeeping(
    monkeypatch, tmp_path,
):
    """Issue #1704: the idle confirmation path renders in the repository's
    fused message language (the repo policy wins over the host value), the
    same object that carries the fused base branch."""
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config('message_language = "zh"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs) or ("open", None),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n'
        'message_language = "en"\n',
        encoding="utf-8",
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["language_code"] == "zh"
