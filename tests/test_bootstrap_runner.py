from orbi import config as config_domain
from orbi import language

# The session-boundary message-language instruction every implementer/
# review prompt carries (Issue #1660). Prompt-content assertions compare
# the rendered template PLUS this instruction, so a custom template
# cannot silently lose it.
LANGUAGE_SUFFIX = language.model_instruction("en")

import ast
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.resume_verification as resume_verification
import orbi.run_state as run_state
import orbi.pi_session as pi_session
import orbi.milestone as milestone
import orbi.release_run as release_run
import orbi.github as github
from orbi import failure, pi_command, pi_stream_log, progress
from tests.fakes.bootstrap_pi import (
    fake_session_records, fresh_timestamp, make_fake_pi,
)
from tests.fakes.bootstrap_preflight import GROQ_PROVIDERS, _user_agent_dir
from tests.fakes.github import FakeGh
from tests.fakes.gitops import remote_head_answer
from tests.test_progress_wiring import make_fake_gh
import orbi.journal as journal
from seam import resume_deps, seam
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from conftest import rendered_prompt

from tests.fakes.bootstrap_delivery import (
    _git_output,
    make_session_jsonl,
    PR_URL,
)


def test_render_prompt_replaces_context_values():
    rendered = pi_session.render_prompt(
        "{{SOURCE_REPO}} #{{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{CONTEXT_FILES}}",
        {
            "SOURCE_REPO": "owner/repo",
            "ISSUE_NUMBER": "3",
            "ISSUE_TITLE": "Fix",
            "CONTEXT_FILES": "context.md",
        },
    )
    assert rendered == "owner/repo #3 Fix context.md"


def test_prompt_requires_verifying_external_behavior_against_docs():
    """Issue #73: the implementer prompt must make it a hard rule to
    verify external behavior (APIs, CLIs, config, HTTP paths) against
    official docs or `--help` before writing it — no assembling from
    memory — and test assertions must assert the real contract, not
    the implementation's guessed shape. It must also state that
    bypass failures (progress, notifications) only log and never
    decide the delivery outcome."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "--help" in template
    assert "docs" in template.lower()
    # The bypass rule is explicit.
    assert "bypass" in template.lower()


def test_review_prompt_flags_unverified_external_behavior():
    """Issue #73: the review prompt must check that external behavior
    (paths, parameters, status codes) was verified against docs or a
    real call, and that a bypass failure cannot break the main
    delivery — the #57 guessed-PATCH-route class of bug must be
    caught by review, not shipped."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt_review.md"
    ).read_text(encoding="utf-8")
    assert "verify" in template.lower()
    assert "bypass" in template.lower()


def test_prompt_does_not_require_review_fix_loop_before_pr():
    """Issue #78: the implementer only does plan -> implement -> test
    -> push PR. The independent review happens AFTER the PR is open
    (the Runner runs it); the prompt must not demand a complete
    review-fix loop before opening the PR — the old wording made the
    local Pi self-review for hours (#18/#34)."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "complete review-fix loop" not in template
    # The review is explicitly positioned AFTER the PR is open, and the
    # implementer never reviews/fixes/merges.
    assert "you do not review, fix, or merge" in template


def test_prompt_template_requires_fixes_keyword_for_the_source_issue():
    """Issue #53: the Pi prompt must carry `Fixes #<issue>` (the PR body
    contract the Runner fulfils when it opens the PR, Issue #186) so
    GitHub closes the source Issue natively on merge; the requirement
    renders with the real issue number."""
    template = (
        Path(__file__).resolve().parent.parent / "prompts" / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "Fixes #{{ISSUE_NUMBER}}" in template
    rendered = pi_session.render_prompt(template, {
        "SOURCE_REPO": "xqliu/orbi",
        "SOURCE_REPOS": "xqliu/orbi",
        "ISSUE_NUMBER": "53",
        "ISSUE_TITLE": "t",
        "ISSUE_BODY": "b",
        # Issue #745: the shipped template injects the trusted-comment
        # timeline; the render must fill it to prove no placeholder is
        # left unresolved.
        "ISSUE_COMMENTS": "(no trusted comments)",
        "WORKSPACE_ROOT": "/tmp",
        "CONTEXT_FILES": "",
        "SKILLS": "",
        "BASE_BRANCH": "main",
        "BASE_SHA": "abc123",
        "RUN_ID": "a2241189",
        "BASE_SYNC_LOCK": "/checkout/.orbi/base-sync.lock",
    })
    assert "Fixes #53" in rendered
    # Issue #186: the implementer prompt no longer carries the base-sync
    # lock path — the base fetch is the Runner's operation.
    assert "/checkout/.orbi/base-sync.lock" not in rendered
    assert "{{" not in rendered


def test_prompt_templates_carry_the_issue_comments_placeholder():
    """Issue #745: the shipped implementer and review templates inject
    the trusted-comment timeline by default — decisions written in
    Issue comments reach the agent without a human copying them into
    the body."""
    prompts = Path(__file__).resolve().parent.parent / "prompts"
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt.md"
    ).read_text(encoding="utf-8")
    assert "{{ISSUE_COMMENTS}}" in (
        prompts / "prompt_review.md"
    ).read_text(encoding="utf-8")


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


def test_new_run_id_is_unique_short_hex():
    first = runner.new_run_id()
    second = runner.new_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert first != second


def test_validate_run_id_accepts_eight_hex_chars():
    assert runner.validate_run_id("e07383c2") == "e07383c2"


def test_validate_run_id_rejects_wrong_length_or_chars():
    for bad in ("run1", "", "a1b2c3d", "a1b2c3d4e", "A1B2C3D4", "g1b2c3d4"):
        with pytest.raises(ValueError, match="invalid run id"):
            runner.validate_run_id(bad)


def test_validate_run_id_rejects_non_string():
    with pytest.raises(ValueError, match="invalid run id"):
        runner.validate_run_id(None)


def test_run_marker_is_stable_machine_readable_comment():
    assert runner.run_marker("e07383c2") == "<!-- orbi:run=e07383c2 -->"


def test_run_marker_is_shared_with_progress_formatter():
    assert runner.run_marker is progress.run_marker


def test_set_run_id_binds_the_attempt_and_current_run_id_reads_it(bind_run_id):
    bind_run_id(None)
    assert runner.current_run_id() is None
    runner.set_run_id("e07383c2")
    assert runner.current_run_id() == "e07383c2"


def test_set_run_id_fails_fast_on_invalid_run_id(bind_run_id):
    bind_run_id(None)
    with pytest.raises(ValueError, match="invalid run id"):
        runner.set_run_id("run1")
    assert runner.current_run_id() is None


def test_run_id_filter_prefixes_messages_when_run_is_bound(caplog):
    # The conftest `bind_run_id` fixture unbinds it again at teardown
    # (Issue #1865): the binding needs no per-test try/finally.
    runner.set_run_id("e07383c2")
    with caplog.at_level("INFO"):
        runner.LOGGER.info("hello %s", "world")
    assert caplog.messages[-1] == "[e07383c2] hello world"


def test_run_id_filter_leaves_messages_untouched_without_bound_run(
    caplog, bind_run_id,
):
    bind_run_id(None)
    with caplog.at_level("INFO"):
        runner.LOGGER.info("hello")
    assert caplog.messages[-1] == "hello"


def _claim_race_deps(monkeypatch, tmp_path, *, freeze_side_effect=None,
                     stable_branch_seq=None):
    """Minimal process_issue deps for the #658 claim-race tests.

    Everything after the claim decision is irrelevant (the guard must
    fire before any of it); run_pi returning normally keeps the
    unguarded path moving so its downstream label writes become
    observable — that is exactly what the red assertions catch.
    """
    if stable_branch_seq is not None:
        seq = list(stable_branch_seq)
        # Exactly two probes consume the sequence: the scan-time probe
        # and the pre-claim guard (Issue #658).
        monkeypatch.setattr(seam, "stable_branch_exists",
            lambda repo_dir, branch: seq.pop(0),
        )
    if freeze_side_effect is not None:
        def fake_freeze_base(repo_dir, base_branch):
            freeze_side_effect()
            return "0123456789abcdef0123456789abcdef01234567"
        monkeypatch.setattr(seam, "freeze_base", fake_freeze_base)
    else:
        monkeypatch.setattr(seam, "freeze_base",
            lambda repo_dir, base_branch:
                "0123456789abcdef0123456789abcdef01234567",
        )
    monkeypatch.setattr(seam, "new_run_id", lambda: "feedface",
    )
    # create_worktree/run_pi stay REAL: on the fixed path the guard
    # yields before either is reached, and a regression that skips the
    # guard then fails loudly on the real calls (tmp_path is no git
    # repo) — no dead fake arms.


def make_claim_race_gh(monkeypatch, state):
    """The gh fake for the #658 claim-race world.

    The direct read (`gh issue view`) answers the LIVE label truth from
    `state["in_progress"]`; the search index (`gh issue list`) answers
    from the same state — a lagging index is modeled by flipping the
    state only AFTER the pickup scan, exactly when the other instance's
    claim lands. One shared guard line, driven by its own test below.
    """
    def fake_run_command(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            labels = ([{"name": "ai-in-progress"}]
                      if state["in_progress"] else [])
            return json.dumps({"labels": labels})
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps(
                [{"number": 18}] if state["in_progress"] else [],
            )
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([])
        if command[:2] == ["git", "ls-remote"]:
            # The scan-time stable-branch probe (before the race lands).
            return ""
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                return json.dumps([])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return fake_run_command


def test_claim_race_gh_answers_every_expected_command(monkeypatch):
    """The shared #658 fake is a contract, not a sink: every branch it
    answers is one the claim flow issues — driven here (in_progress=True
    arm; the False arm is driven by the two yield tests) so no arm is
    dead in the fixed world, and anything else fails loud."""
    fake = make_claim_race_gh(monkeypatch, {"in_progress": True})
    assert json.loads(fake([
        "gh", "issue", "view", "18", "--repo", "o/r", "--json", "labels",
    ])) == {"labels": [{"name": "ai-in-progress"}]}
    assert json.loads(fake([
        "gh", "issue", "list", "--search", "label:ai-in-progress",
    ])) == [{"number": 18}]
    assert json.loads(fake(["gh", "pr", "list", "--state", "open"])) == []
    assert fake([
        "git", "ls-remote", "--heads", "origin", "refs/heads/x",
    ]) == ""
    assert json.loads(fake([
        "gh", "api", "repos/o/r/issues/18/comments",
    ])) == []
    assert fake([
        "gh", "api", "repos/o/r/issues/18/comments",
        "--method", "POST", "--field", "body=x",
    ]) == ""
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "release", "view"])


def test_process_issue_yields_when_the_label_lands_mid_preparation(
    monkeypatch, tmp_path,
):
    """Issue #658：扫描与认领写之间隔着 freeze_base 的秒级网络往返；
    另一实例在该窗口内认领（ai-in-progress 出现）时，本实例必须让路
    ——不打任何标签、不发失败评论、绝不能把胜者打成 ai-blocked。"""
    state = {"in_progress": False}
    make_claim_race_gh(monkeypatch, state)
    _claim_race_deps(
        monkeypatch, tmp_path,
        freeze_side_effect=lambda: state.__setitem__("in_progress", True),
    )
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def test_process_issue_yields_when_the_stable_branch_lands_mid_preparation(
    monkeypatch, tmp_path,
):
    """Issue #658 的撞分支面：扫描时 stable branch 尚不存在，freeze_base
    的窗口内被另一实例创建（它先认领）——认领写之前必须复查到并让路，
    而不是径直 create_worktree 撞分支后走 ai-blocked 失败路径。"""
    make_claim_race_gh(monkeypatch, {"in_progress": False})
    # First probe (scan-time): absent. Second probe (the pre-claim
    # guard): the other instance created it while we were preparing.
    _claim_race_deps(
        monkeypatch, tmp_path, stable_branch_seq=[False, True],
    )
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def _release_race_deps(monkeypatch, in_progress: bool, live_holders: list):
    """Issue #708 fakes: the direct read (`gh issue view`) answers the
    LIVE label truth; `slot_occupancy` answers the runner-liveness
    truth (`None`/own pid = alone, another pid = a live co-runner)."""
    def fake_run_command(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            labels = ([{"name": "ai-in-progress"}]
                      if in_progress else [])
            return json.dumps({"labels": labels})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(
        seam, "slot_occupancy", lambda *a, **k: list(live_holders),
    )
    return fake_run_command


def _release_issue() -> dict:
    return {"number": 30, "title": "Release 1.2.3", "body": "## Release",
            "labels": [{"name": "ai-release"}, {"name": "ai-ready"}]}


def _release_dispatch_config(tmp_path) -> dict:
    return config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)


def _spy_process_release(monkeypatch) -> list:
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return "done"

    import orbi.release_run as release_module
    monkeypatch.setattr(release_module, "process_release", spy)
    return calls


def test_release_dispatch_yields_when_in_progress_and_another_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #708：ready 扫描的滞后快照可把已被认领的发布票递给第二个
    活实例。开发路径在 #658 拿到了直读让路；发布分发同样必须让路——
    在途且另一活 runner 持槽 = 该发布正在被做，本 tick 退出，绝不与
    状态机并发（成功发布不会被败者改写成 ai-blocked）。"""
    _release_race_deps(
        monkeypatch, in_progress=True,
        live_holders=[(tmp_path / "slot-0", 424242)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("claim-yielded", None)
    assert calls == [], "the release state machine must not start"


def test_release_dispatch_resumes_orphan_when_no_other_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #708 的另一半：在途但无其他活 runner = 死 runner 留下的
    孤儿发布——照旧进 process_release 复用 run_id 续跑（#98 的重启
    resume 语义不因让路守卫而丢失）。"""
    _release_race_deps(
        monkeypatch, in_progress=True,
        live_holders=[(tmp_path / "slot-0", None)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("release", "done")
    assert len(calls) == 1


def test_release_dispatch_fresh_ticket_unchanged_by_the_guard(
    monkeypatch, tmp_path,
):
    """直读没有 ai-in-progress（新票）时，守卫零作用——照常分发；
    活 runner 存在与否只对在途票有意义。"""
    _release_race_deps(
        monkeypatch, in_progress=False,
        live_holders=[(tmp_path / "slot-0", 424242)],
    )
    calls = _spy_process_release(monkeypatch)
    result = runner.process_issue(
        _release_issue(), _release_dispatch_config(tmp_path), "owner/repo",
    )
    assert result == runner.IssueResult("release", "done")
    assert len(calls) == 1


def test_release_race_gh_rejects_unexpected_commands(monkeypatch):
    """The #708 fake is a contract, not a sink: the strict-raise arm is
    driven here so it cannot silently rot into a permissive fake that
    answers commands the dispatch no longer issues (the #658 fake keeps
    the same discipline via its own contract test)."""
    fake = _release_race_deps(monkeypatch, in_progress=True, live_holders=[])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "release", "view"])


def test_another_live_runner_reads_slot_occupancy(monkeypatch, tmp_path):
    """_another_live_runner 是 #39 存活规则的独立助手：别的 pid 持槽
    =True，None/自己 =False（与 pick_in_progress_issue 同一语义，
    runner.py:2412 的循环抽出来共用）。"""
    slot_dir = tmp_path / "slots"
    slot_dir.mkdir()
    seen = {}

    def foreign(directory, limit):
        seen["args"] = (directory, limit)
        return [(slot_dir / "slot-0", 424242)]

    monkeypatch.setattr(seam, "slot_occupancy", foreign)
    assert runner._another_live_runner(slot_dir, 2) is True
    assert seen["args"] == (slot_dir, 2)
    monkeypatch.setattr(
        seam, "slot_occupancy",
        lambda d, m: [(slot_dir / "slot-0", None),
                      (slot_dir / "slot-1", os.getpid())],
    )
    assert runner._another_live_runner(slot_dir, 2) is False


def test_process_issue_resumes_existing_run_and_same_progress_comment(
    monkeypatch, tmp_path,
):
    """Restart resume (Issue #18): the runner died with the task worktree
    and the `ai-in-progress` label behind. The re-claim reuses the
    newest worktree's run id, so the same hidden-marker progress comment
    is found by its marker and PATCHed — never a second one."""
    calls = []
    existing_comment = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nstale state from the dead run"
        ),
    }
    gh_calls, posted = make_fake_gh(
        monkeypatch, comments=[existing_comment], in_progress=True,
    )
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # The existing progress comment of the dead run.
                return json.dumps([existing_comment])
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            if command[-1] == "state":
                # The pre-PR closeout reads the source Issue state
                # (Issue #746): open in this scene.
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(("edit", args, kwargs)),
    )
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    # The fresh id would be different: the resume must override it with
    # the newest worktree's run id.
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    assert runner.process_issue(
        issue, config, "xqliu/orbi-backlog",
    ) == runner.IssueResult("pr", "https://github.com/orbi-build/orbi/pull/4")
    # The reused run id drives the branch and the scene comments.
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/orbi-backlog",
                                       "add": "ai-in-progress"})
    scene_comments = [
        call for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    for call in scene_comments:
        assert "<!-- orbi:run=a1b2c3d4 -->" in call[-1]
        assert "ffffeeee" not in call[-1]
    # No new progress comment: the restarted run PATCHed the existing
    # one (id 77) found by its run marker.
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in gh_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi-backlog/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the existing progress comment was not updated"
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    # Issue #1733: the PR is open and waiting for checks and review.
    assert "waiting for checks and review" in last_body
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in last_body


def test_process_issue_second_tick_behind_the_index_resumes_not_reclaims(
    monkeypatch, tmp_path, caplog,
):
    """Issue #658 单实例面（xqliu 09-11 生产现场，orbi#702 并入 #658）：
    同一 timer 的两次 tick 竞争同一张票——第一次 tick 早已完成认领
    （`ai-in-progress` 落位在本 tick 之前，worktree 的 run 在飞），而搜索
    索引滞后，把这张票连同陈旧的 `ai-ready` labels 快照一起递给本 tick。
    认领复查的 `gh issue view` 直读必须看到标签：本 tick 走 resume 复用
    在飞 run 的 id，绝不 fresh claim（不消费新 run_id、不建第二个分支
    把在途交付撞成 push rejected → ai-blocked）——修复不依赖
    `max_concurrency` 的取值，单实例双 tick 与双实例同根因。"""
    existing_comment = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nfirst tick's run is in flight"
        ),
    }
    gh_calls, posted = make_fake_gh(
        monkeypatch, comments=[existing_comment], in_progress=True,
    )
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # The in-flight run's progress comment.
                return json.dumps([existing_comment])
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            # The strongly-consistent direct read: the label the first
            # tick wrote IS live truth, no matter how stale the index
            # that delivered the Issue was. The pre-PR closeout reads
            # the state too (Issue #746): open in this scene.
            if command[-1] == "state":
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": "<!-- orbi:run=a1b2c3d4 -->\n\nFixes #4\n\nPlan",
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456",
    )
    # If a regression makes this tick a FRESH claim, the fresh id leaks
    # into the scene comments and the assertions below catch it.
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    # The stale scan snapshot: still showing ai-ready even though the
    # first tick already claimed the Issue.
    issue = {"number": 4, "title": "Fix", "body": "Body",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        result = runner.process_issue(
            issue, config, "xqliu/orbi-backlog",
        )
    assert result == runner.IssueResult(
        "pr", "https://github.com/orbi-build/orbi/pull/4",
    )
    # The direct read routed this tick into the resume: the in-flight
    # run's id owns every scene comment — a fresh claim never starts.
    assert "fresh_claim_route" not in caplog.text
    assert "resuming_run" in caplog.text
    # And the claim recheck never asked the lagging index for the label
    # (the pre-#658 list search): the direct read is the only source.
    assert not any(
        command[:3] == ["gh", "issue", "list"] for command in gh_calls
    )
    scene_bodies = [
        call[-1] for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    assert scene_bodies, "no scene comment was published"
    for body in scene_bodies:
        assert "ffffeeee" not in body
    # And the claim-time label write stayed the shared idempotent patch
    # (run=a1b2c3d4 marker), never a second run's footprint.


def test_process_issue_binds_run_id_before_the_resume_scan(
    monkeypatch, tmp_path, caplog,
):
    """Run correlation (Issue #41): EVERY journal line of the attempt
    carries the `[run_id]` prefix — including the claim-time lines of a
    restart resume (the `ai-in-progress` label scan and `resuming_run`),
    which run before the resume decision (review round 3, PR #42)."""
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    head = "0123456789abcdef0123456789abcdef01234567"
    branch = "orbi/xqliu-orbi-backlog-issue-4"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123",
    )
    # The fresh id differs from the reused one: both are valid run ids
    # of this attempt (generated, then replaced by the resumed run).
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    monkeypatch.setattr(
        runner, "worktree_resume_scene", lambda repo_dir, source_repo, number:
        ("a1b2c3d4", tmp_path / "wt"),
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    # No unprefixed line: the claim-time gh scan and resuming_run are
    # part of the attempt's timeline.
    for message in caplog.messages:
        assert message.startswith("[ffffeeee]") \
            or message.startswith("[a1b2c3d4]"), message
    # The resume decision itself is logged under the REUSED run id.
    resuming = [m for m in caplog.messages if "resuming_run" in m]
    assert len(resuming) == 1
    assert resuming[0].startswith("[a1b2c3d4]")


def test_process_issue_starts_fresh_run_when_the_label_is_gone(
    monkeypatch, tmp_path,
):
    """A completed run keeps its worktree as evidence but loses the
    `ai-in-progress` label: re-claiming the Issue must start a fresh
    run, even when old worktrees exist (Issue #18)."""
    gh_calls, posted = make_fake_gh(monkeypatch)  # in_progress=False
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-orbi-backlog-issue-4"
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    scene_calls = []
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: scene_calls.append(1) or None,
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    # The fresh run id is used and the old worktree's run id is never
    # consulted (the label is the gate).
    assert scene_calls == []
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- run_id=ffffeeee" in progress_posts[0]


def test_process_issue_keeps_fresh_run_when_no_worktree_survived(
    monkeypatch, tmp_path, caplog,
):
    """The label survived a kill but the worktree is gone (e.g. cleaned
    up): there is nothing to resume, so the run starts fresh — the
    `ai-in-progress` label alone never resurrects a run id."""
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=True)
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-in-progress"}]})
        if command[:2] == ["gh", "issue"]:
            return ""
        if command[:3] == ["git", "branch", "--show-current"]:
            return "orbi/xqliu-orbi-backlog-issue-4"
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    # The label is on, but no task worktree survived the kill.
    monkeypatch.setattr(runner, "worktree_resume_scene", lambda *a, **k: None)
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    # No resume happened: the fresh run id drives the delivery.
    assert "resuming_run" not in caplog.text
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- run_id=ffffeeee" in progress_posts[0]


def _resume_wiring_setup(monkeypatch, tmp_path, *, in_progress: bool,
                          latest_run_id_result="a1b2c3d4",
                          git_status: str = ""):
    """Shared fake-gh/git wiring for the Issue #219 process_issue tests.

    Returns `(gh_calls, posted, worktree, comment_bodies)`: `posted`
    carries the progress-comment bodies (`gh api` POST), `comment_bodies`
    the plain `gh issue comment` bodies (scene + failure comments).
    `git_status` is the `git status --porcelain` answer (the
    worktree's uncommitted changes).
    """
    gh_calls, posted = make_fake_gh(monkeypatch, in_progress=in_progress)
    # The branch carries the run id the attempt actually uses: the
    # resumed one (label on + a worktree to resume) or the fresh one.
    run_id = ("a1b2c3d4"
              if in_progress and latest_run_id_result else "ffffeeee")
    branch = f"orbi/xqliu-orbi-backlog-issue-4"
    comment_bodies = []

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [
                {"name": "ai-in-progress"}] if in_progress
                else [{"name": "ai-ready"}],
            })
        if command[:3] == ["gh", "issue", "comment"]:
            comment_bodies.append(command[-1])
            return ""
        if command[:3] == ["git", "status", "--porcelain"]:
            return git_status
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "ffffeeee")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner, "worktree_resume_scene",
        lambda repo_dir, source_repo, number: (
            (latest_run_id_result, worktree)
            if latest_run_id_result else None
        ),
    )
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: worktree,
    )
    return gh_calls, posted, worktree, comment_bodies


def test_process_issue_writes_run_state_and_resume_context(
    monkeypatch, tmp_path, caplog,
):
    """Issue #219: the interrupted run continues on the EXISTING work —
    the run state file is written (the same-run marker), the resume
    context (uncommitted changes + previous session progress) reaches
    the new session, and the `resume_continue` line is logged."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=True,
        git_status="?? src/a.py\n",
    )
    # The interrupted work: uncommitted changes + a previous session.
    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    make_session_jsonl(worktree, "sess-1")
    resume_contexts = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/orbi-build/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    # The run state file marks the worktree as the same run.
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "a1b2c3d4"
    assert state["issue"] == 4
    assert state["repo"] == "xqliu/orbi-backlog"
    # The new session starts from the existing work.
    assert len(resume_contexts) == 1
    assert resume_contexts[0] is not None
    assert "src/a.py" in resume_contexts[0]
    assert "sess-1" in resume_contexts[0]
    # The structured resume line is logged (auditable, Issue #219).
    lines = [
        m for m in caplog.messages if "resume_continue" in m
    ]
    assert len(lines) == 1
    assert f"worktree={worktree}" in lines[0]
    assert "changed_files=1" in lines[0]
    assert "reused_runs=1" in lines[0]
    assert "previous_session=sess-1" in lines[0]


def test_process_issue_fresh_run_has_no_resume_context(
    monkeypatch, tmp_path, caplog,
):
    """A fresh claim (no `ai-in-progress`) gets a run state file too
    (so a later interruption can resume), but NO resume context and NO
    `resume_continue` line — the prompt is the exact pre-#219 shape."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=False,
    )
    resume_contexts = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: resume_contexts.append(
            kwargs.get("resume_context"),
        ) or "done",
    )
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs:
        "https://github.com/orbi-build/orbi/pull/4",
    )
    caplog.set_level("INFO")
    runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    state = run_state.read_run_state(worktree)
    assert state["run_id"] == "ffffeeee"
    assert resume_contexts == [None]
    assert "resume_continue" not in caplog.text


@pytest.mark.parametrize(
    ("dispatch_label", "queue_label"),
    [(None, "ai-ready"), ("ship", "ship")],
    ids=["default_queue_label", "custom_dispatch_label"],
)
def test_process_issue_fails_fast_when_the_run_state_is_missing(
    monkeypatch, tmp_path, caplog, dispatch_label, queue_label,
):
    """Issue #219: the worktree of this issue exists but its run state
    file is gone — the same run cannot be verified. The attempt fails
    fast through the terminal failure path (`ai-blocked` + the reason
    comment): no fresh run, no silent redo.

    Issue #1775: the terminal state is `ai-blocked` ALONE — the queue
    label the claim scanned (the repository's `dispatch_label`, default
    `ai-ready`) is cleared with it."""
    gh_calls, posted, worktree, comment_bodies = _resume_wiring_setup(
        monkeypatch, tmp_path, in_progress=True,
    )

    def failing_resume_scene(repo_dir, source_repo, number):
        raise RuntimeError(
            f"worktree {worktree} has no run state file "
            "(.orbi/run-state.json): the same run cannot be "
            "verified (Issue #219)"
        )

    monkeypatch.setattr(runner, "worktree_resume_scene", failing_resume_scene)
    run_pi_calls = []
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: run_pi_calls.append(1) or "done",
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="run state"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(
                repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
                base_branch="main", dispatch_label=dispatch_label,
            ),
            "xqliu/orbi-backlog",
        )
    # No fresh run was started.
    assert run_pi_calls == []
    # The terminal state is `ai-blocked` ALONE (the claim label removed).
    assert edits[-2] == ((4,), {
        "repo": "xqliu/orbi-backlog", "add": "ai-blocked",
        "remove": "ai-in-progress",
    })
    # The queue label the claim kept is cleared last (Issue #1775).
    assert edits[-1] == ((4,), {
        "repo": "xqliu/orbi-backlog", "remove": queue_label,
    })
    # The failure comment carries the reason and the run marker.
    failed = [
        body for body in comment_bodies
        if body.startswith("<!-- orbi:run=")
        and "Orbi failed" in body
    ]
    assert len(failed) == 1
    assert "cannot continue the interrupted run" in failed[0]
    assert "run state" in failed[0]
    assert "resume_continue_failed" in caplog.text


def test_issue_context_uses_owner_repo_number_form():
    assert runner.issue_context("xqliu/orbi", 40) == (
        "xqliu/orbi#40"
    )


def test_run_pi_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the implementer prompt carries the absolute path of
    # the shared base-sync lock so Pi's base freshness fetch can run
    # under it (flock <lock> git fetch origin <base>).
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert rendered_prompt(command) == (
        "SYSTEM " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_logs_provider_config_loaded_with_selection(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: run_pi logs `provider_config_loaded` with the
    configured provider/model identifiers (the same non-sensitive
    values as the redacted command line) before Pi is spawned."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", pi_provider="local-qwen", pi_model="qwen3.8:27b")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    line = lines[0]
    assert "issue=owner/repo#4" in line
    assert "role=implement" in line
    assert "provider=local-qwen" in line
    assert "model=qwen3.8:27b" in line
    assert "elapsed=" in line


def test_run_pi_logs_provider_config_loaded_unconfigured(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: without a configured provider/model the line still
    fires (Pi keeps its own agent dir) with `-` placeholders."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "done")
    issue = {"number": 4, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    with caplog.at_level("INFO"):
        pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "provider=-" in lines[0]
    assert "model=-" in lines[0]
    assert "role=implement" in lines[0]


def test_run_review_logs_provider_config_loaded_with_review_role(
    monkeypatch, tmp_path, caplog,
):
    """Issue #176: the review session logs the same line with
    role=review (one run_id, the roles are steps of the same run)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW", encoding="utf-8")
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "ok")
    with caplog.at_level("INFO"):
        pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b").run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b"), 1)
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=review" in lines[0]
    assert "issue=owner/repo#4" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_run_review_renders_base_sync_lock_into_prompt(monkeypatch, tmp_path):
    # Issue #171: the review prompt carries the SAME lock path (the
    # review session's base merge fetch must not race the shared ref).
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text(
        "REVIEW {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(command) or "ok")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


# --- Issue #745: {{ISSUE_COMMENTS}} — the trusted-comment timeline -----------


def test_run_pi_injects_trusted_issue_comments_into_the_prompt(
    monkeypatch, tmp_path,
):
    """Issue #745: a template carrying {{ISSUE_COMMENTS}} receives the
    Issue's trusted-comment timeline — fetched from GitHub, maintainer
    comments only, capped by the configured `issue_comments_limit`
    with a visible omission note."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_COMMENTS}}", encoding="utf-8")
    comments = [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "oldest decision"},
        {"author": {"login": "mallory"}, "authorAssociation": "NONE",
         "createdAt": "2026-09-12T02:00:00Z", "body": "public drive-by"},
        {"author": {"login": "carol"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T03:00:00Z", "body": "middle decision"},
        {"author": {"login": "dave"}, "authorAssociation": "MAINTAINER",
         "createdAt": "2026-09-12T04:00:00Z", "body": "newest decision"},
    ]
    fetches = []

    def fake_issue_comments(number, *, repo):
        fetches.append((number, repo))
        return comments

    monkeypatch.setattr(seam, "issue_comments", fake_issue_comments)
    # Issue #1806: the writers are the repository's maintainers (alice,
    # carol, dave), mallory is not — the comment's association decides
    # nothing. The read is answered here so the test never depends on the
    # host's real gh login state (CI runs unauthenticated).
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: "none" if login == "mallory" else "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]"
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1", issue_comments_limit=2)
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    assert fetches == [(4, "owner/repo")]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "(1 older trusted comment omitted; showing the 2 most recent)"
        in system_prompt
    )
    assert "newest decision" in system_prompt
    assert "middle decision" in system_prompt
    assert "oldest decision" not in system_prompt
    assert "public drive-by" not in system_prompt
    # The context argument keeps its exact pre-#745 shape — the comments
    # reach the agent through the template placeholder, nowhere else.
    assert command[-1] == (
        "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a template without {{ISSUE_COMMENTS}} keeps the exact
    pre-#745 behavior — no comments GitHub read, no new failure mode
    (backward compatibility for custom templates)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == "SYSTEM b" + LANGUAGE_SUFFIX


def test_run_review_refreshes_issue_body_each_round(monkeypatch, tmp_path):
    """Issue #1273: each review round receives the Issue body as it reads
    now, so deleting an acceptance criterion takes effect on the next round."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    bodies = iter(["old criterion", "new criterion {{BASE_SYNC_LOCK}}"])
    issue_reads = []
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: issue_reads.append((number, fields, repo))
        or {"body": next(bodies)},
    )
    prompts = []
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        # The prompt is read through the FILE the argv names (Issue
        # #1634) at call time: each round overwrites the same prompt
        # file in the shared session dir, so the read has to happen
        # while the round is running.
        lambda command, **kwargs: prompts.append(rendered_prompt(command)) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    pi_session.run_review(ctx, pr, config, 1)
    pi_session.run_review(ctx, pr, config, 2)
    assert issue_reads == [(4, "body", "owner/repo"), (4, "body", "owner/repo")]
    assert prompts == [
        "BODY=old criterion" + LANGUAGE_SUFFIX,
        "BODY=new criterion {{BASE_SYNC_LOCK}}" + LANGUAGE_SUFFIX,
    ]
    assert "old criterion" not in prompts[1]


def test_run_review_rejects_a_non_string_issue_body(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("BODY={{ISSUE_BODY}}", encoding="utf-8")
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda number, fields, *, repo: {"body": 42},
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    ctx = RunContext(run_id=config.run_id, issue=4, branch="branch",
                     worktree=tmp_path, source_repo="owner/repo")
    pr = {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
          "head_oid": "h1", "head_ref": "h"}
    with pytest.raises(ValueError, match="issue body must be a string"):
        pi_session.run_review(ctx, pr, config, 1)


def test_run_review_combines_current_pr_feedback_with_issue_comments(
    monkeypatch, tmp_path,
):
    """The current PR's normalized feedback shares one bounded timeline."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda number, *, repo: [
        {"author": {"login": "alice"}, "authorAssociation": "MEMBER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "old issue decision"},
    ])
    pr_fetches = []
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "conversation")) or [])
    monkeypatch.setattr(seam, "pr_reviews", lambda number, *, repo:
                        pr_fetches.append((number, repo, "reviews")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "submitted_at": "2026-09-12T02:00:00Z",
         "state": "CHANGES_REQUESTED", "body": "review body"},
    ])
    monkeypatch.setattr(seam, "pr_review_comments", lambda number, *, repo:
                        pr_fetches.append((number, repo, "inline")) or [
        {"user": {"login": "bob"}, "author_association": "OWNER",
         "created_at": "2026-09-12T03:00:00Z", "path": "src/x.py",
         "line": 7, "body": "inline fix"},
    ])
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]"
    )
    # Issue #1806: the human comments of these timelines are writers;
    # the permission read is answered here so no test reaches GitHub.
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(), issue_comments_limit=2,
    )
    pi_session.run_review(
        RunContext(run_id=config.run_id, issue=4, branch="branch",
                   worktree=tmp_path, source_repo="owner/repo"),
        {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1,
    )
    assert pr_fetches == [
        (9, "owner/repo", "conversation"),
        (9, "owner/repo", "reviews"),
        (9, "owner/repo", "inline"),
    ]
    command = calls[0]
    system_prompt = rendered_prompt(command)
    assert (
        "1 older trusted comment omitted; showing the 2 most recent"
        in system_prompt
    )
    assert "old issue decision" not in system_prompt
    assert "review body" in system_prompt
    assert "[CHANGES_REQUESTED]" in system_prompt
    assert "inline fix" in system_prompt
    assert "src/x.py:7" in system_prompt


def test_run_review_pr_feedback_failure_logs_and_keeps_issue_comments(
    monkeypatch, tmp_path, caplog,
):
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{ISSUE_COMMENTS}}", encoding="utf-8")
    monkeypatch.setattr(seam, "issue_comments", lambda *args, **kwargs: [
        {"author": {"login": "alice"}, "authorAssociation": "OWNER",
         "createdAt": "2026-09-12T01:00:00Z", "body": "issue decision"},
    ])
    monkeypatch.setattr(seam, "pr_comments", lambda *args, **kwargs: [])
    # Issue #1806: the trust read is answered here so the timeline never
    # asks GitHub (and never needs the host's identity).
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "ci-runner[bot]",
    )

    def fail_reviews(*args, **kwargs):
        raise RuntimeError("reviews unavailable")

    monkeypatch.setattr(seam, "pr_reviews", fail_reviews)
    calls = []
    monkeypatch.setattr(seam, "stream_pi",
                        lambda command, **kwargs: calls.append(command) or "ok")
    config = config_domain.RunnerConfig(
        prompt_review=prompt_path, repo_dir=tmp_path / "checkout",
        source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4",
        skills=(),
    )
    with caplog.at_level(logging.ERROR):
        pi_session.run_review(
            RunContext(run_id=config.run_id, issue=4, branch="branch",
                       worktree=tmp_path, source_repo="owner/repo"),
            {"number": 9, "url": "https://x/pull/9", "base_oid": "b1",
             "head_oid": "h1", "head_ref": "h"}, config, 1,
        )
    system_prompt = rendered_prompt(calls[0])
    assert "issue decision" in system_prompt
    assert "pr_review_feedback_read_failed repo=owner/repo pr=9" in caplog.text


def test_run_review_skips_the_comment_fetch_without_the_placeholder(
    monkeypatch, tmp_path,
):
    """Issue #745: a review template without {{ISSUE_COMMENTS}} keeps
    the exact pre-#745 behavior (no comments GitHub read)."""
    prompt_path = tmp_path / "prompt_review.md"
    prompt_path.write_text("REVIEW {{BASE_SYNC_LOCK}}", encoding="utf-8")
    fetches = []
    monkeypatch.setattr(seam, "issue_comments",
        lambda *args, **kwargs: fetches.append((args, kwargs)),
    )
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1)
    assert fetches == []
    command = calls[0]
    assert rendered_prompt(command) == (
        "REVIEW " + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
        + LANGUAGE_SUFFIX
    )


def test_run_pi_injects_base_branch_sha_and_run_id_into_prompt(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "SYSTEM {{SOURCE_REPO}} {{ISSUE_NUMBER}} {{ISSUE_TITLE}} {{ISSUE_BODY}} "
        "{{WORKSPACE_ROOT}} {{CONTEXT_FILES}} {{SKILLS}} {{BASE_BRANCH}} "
        "{{BASE_SHA}} {{RUN_ID}} {{BASE_SYNC_LOCK}}",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    issue = {"number": 4, "title": "Fix title", "body": "Fix body"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path / "checkout", source_repos=("owner/repo",), workspace_root=tmp_path, context_files=("context.md",), skills=("skill.md",), base_branch="main", base_sha="abc123def456", run_id="run1")
    assert pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config) == "done"
    command, kwargs = calls[0]
    assert command[:5] == ["pi", "--no-extensions", "--skill", "skill.md", "--print"]
    system_prompt = rendered_prompt(command)
    assert "owner/repo" in system_prompt
    assert " 4 " in system_prompt
    assert "Fix title" in system_prompt
    assert "Fix body" in system_prompt
    assert "context.md" in system_prompt
    assert "skill.md" in system_prompt
    assert system_prompt.endswith(LANGUAGE_SUFFIX)
    assert (
        "main abc123def456 run1 "
        + str(tmp_path / "checkout" / ".orbi" / "base-sync.lock")
    ) in system_prompt
    assert command[9] == "Issue #4: Fix title\n\nIssue body:\nFix body\n\nWorktree: " + str(tmp_path) + "\nComplete the delivery process in the system prompt."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] is None
    assert kwargs["ctx"].run_id == "run1"
    assert kwargs["ctx"].issue == 4
    assert kwargs["ctx"].source_repo == "owner/repo"
    assert kwargs["ctx"].branch == "orbi/owner-repo-issue-4"
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_pi_passes_task_branch_to_stream_pi(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append(kwargs) or "done")
    issue = {"number": 5, "title": "t", "body": "b"}
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi(issue, RunContext(run_id=config.run_id, issue=issue["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, timeout=7)
    assert calls[0]["ctx"].branch == "orbi/owner-repo-issue-5"
    assert calls[0]["timeout"] == 7


def test_run_pi_redacts_prompt_and_issue_from_command_log(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("PRIVATE SYSTEM {{ISSUE_BODY}}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: calls.append((command, kwargs)) or "done")
    pi_session.run_pi({"number": 5, "title": "secret", "body": "token"}, RunContext(run_id=config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1").run_id, issue={"number": 5, "title": "secret", "body": "token"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1"))
    command, kwargs = calls[0]
    system_prompt = rendered_prompt(command)
    assert "PRIVATE SYSTEM" in system_prompt
    assert "token" in system_prompt
    assert kwargs["log_command"] == [
        "pi", "--no-extensions", "--print", "--session-dir",
        str(tmp_path / ".pi-session"),
        "--system-prompt", "<redacted>", "<issue-context-redacted>",
    ]


def test_run_pi_keeps_the_fresh_context_without_a_resume_context(
    monkeypatch, tmp_path,
):
    """No resume context: the context argument is byte-identical to the
    pre-#219 shape (a fresh claim is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config)
    command = calls[0]
    assert command[-1] == (
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: "
        + str(tmp_path) + "\n"
        "Complete the delivery process in the system prompt."
    )


def test_run_pi_appends_the_resume_context_to_the_context_argument(
    monkeypatch, tmp_path,
):
    """Issue #219: the continued run's new session starts from the
    existing work — the resume context is appended to the context
    argument (the prompt template itself is untouched)."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id="run1")
    resume = (
        "Resume context (Issue #219): this worktree already carries "
        "work from an earlier session of the SAME run. Continue that "
        "work — do not start from scratch, do not discard or rewrite "
        "the existing changes, and do not create a new plan from "
        "nothing.\nPrevious session progress: session=sess-1 "
        "events=7 phase=test last_action=bash pytest last_result=ok\n"
        "Uncommitted changed files (2):\n- src/a.py\n- src/b.py"
    )
    pi_session.run_pi({"number": 5, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 5, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-5", worktree=tmp_path, source_repo="owner/repo"), config, resume_context=resume)
    command = calls[0]
    context = command[-1]
    assert context.startswith(
        "Issue #5: t\n\nIssue body:\nb\n\nWorktree: " + str(tmp_path),
    )
    assert context.endswith(resume)


def test_process_issue_success_records_base_and_run_in_comment(monkeypatch, tmp_path):
    calls = []
    gh_calls, posted = make_fake_gh(monkeypatch)
    branch = "orbi/xqliu-orbi-backlog-issue-4"
    head = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(command, **kwargs):
        gh_calls.append(command)
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            if command[-1] == "state":
                # The pre-PR closeout reads the source Issue state
                # (Issue #746): open in this scene.
                return json.dumps({"state": "OPEN"})
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:2] == ["gh", "issue"]:
            # Scene comments (started Pi / opened PR) go through
            # comment_issue -> run_command; record them like the others.
            return ""
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/orbi-build/orbi/pull/4",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": head,
                "headRepository": {"name": "orbi"},
                "headRepositoryOwner": {"login": "orbi-build"},
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n\n"
                    "Fixes #4\n\nPlan"
                ),
            }])
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head
        # Issue #898: the pushed head is read from the remote itself.
        answer = remote_head_answer(command, head)
        if answer is not None:
            return answer
        # git fetch / git merge-base: no output needed.
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    issue = {"number": 4, "title": "Fix", "body": "Body"}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main")
    assert runner.process_issue(issue, config, "xqliu/orbi-backlog") == runner.IssueResult("pr", "https://github.com/orbi-build/orbi/pull/4")
    assert calls[0] == ("edit", (4,), {"repo": "xqliu/orbi-backlog", "add": "ai-in-progress"})
    # The run state is published automatically: exactly one progress
    # comment (hidden run marker); started and PR-opened scenes are
    # separate resume announcements.
    progress_posts = [
        body for body in posted
        if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in progress_posts[0]
    # The PR URL is only known after verify_pr: the initial POST shows
    # `- PR: -`, the final delivery PATCH carries the URL.
    assert "- PR: -" in progress_posts[0]
    # The scene comments: the started Pi scene is upserted through the
    # progress publisher (Issue #1369), the opened PR scene still goes
    # through `comment_issue`.
    started_bodies = [
        body for body in posted if "Orbi started Pi:" in body
    ]
    assert len(started_bodies) == 1
    start_body = started_bodies[0]
    assert "Orbi started Pi:" in start_body
    assert "- base_branch: main" in start_body
    assert "- base_sha: abc123def456" in start_body
    assert "run_id=a1b2c3d4" in start_body
    assert "- branch: orbi/xqliu-orbi-backlog-issue-4" in start_body
    # Issue #1715: the public started comment never names the worktree
    # path on the runner host.
    assert "- worktree" not in start_body
    assert str(tmp_path) not in start_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in start_body
    opened_bodies = [
        call[-1] for call in gh_calls
        if call[:2] == ["gh", "issue"] and "comment" in call
        and "Orbi opened PR:" in call[-1]
    ]
    assert len(opened_bodies) == 1
    opened_body = opened_bodies[0]
    assert "Orbi opened PR: https://github.com/orbi-build/orbi/pull/4" in opened_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in opened_body
    # The final delivery summary PATCHed the same progress comment.
    patches = [
        command for command in gh_calls
        if command[:2] == ["gh", "api"] and "PATCH" in command
    ]
    assert patches
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    # Issue #1733: the PR-opened state, waiting for checks and review.
    assert "waiting for checks and review" in last_body


def _gh_api(command, posted):
    """Answer the progress publisher's `gh api` traffic (shared fake)."""
    if "--method" not in command:
        return json.dumps([])
    method = command[command.index("--method") + 1]
    if method == "POST":
        body = command[command.index("--field") + 1]
        posted.append(body[len("body="):])
        return json.dumps({"id": 77, "body": body[len("body="):]})
    return ""


def test_process_issue_success_logs_run_end_with_commit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4")
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "run=a1b2c3d4" in ends[0]
    assert "issue=xqliu/orbi-backlog#4" in ends[0]
    assert "result=pr_opened" in ends[0]
    assert "pr=https://github.com/orbi-build/orbi/pull/4" in ends[0]
    assert "commit=0123456789abcdef0123456789abcdef01234567" in ends[0]


def test_process_issue_failure_marks_blocked_and_ends_cleanly(monkeypatch, tmp_path):
    """Issue #239: a delivery failure is terminal — `process_issue` marks
    the Issue `ai-blocked`, posts the `Orbi failed` comment, and
    RETURNS `None` instead of re-raising. The service must not crash on
    an already-handled failure; the tick ends cleanly and `main` skips
    the delivery wait."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert calls[1][2] == {"repo": "xqliu/orbi-backlog", "add": "ai-blocked", "remove": "ai-in-progress"}
    # The terminal state is ai-blocked ALONE: the queue label the claim
    # kept is cleared too.
    assert calls[2][2] == {"repo": "xqliu/orbi-backlog", "remove": "ai-ready"}
    assert calls[3][0] == "comment"
    failure_body = calls[3][2]["body"]
    # The failure is also published as the blocked milestone (Issue
    # #18). The worktree was never created, so no progress comment
    # exists to PATCH: the milestone alone carries the notification.
    assert any("Orbi: blocked" in body for body in posted)
    blocked = [
        body for body in posted if "Orbi: blocked" in body
    ][0]
    assert "git failed" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    assert "git failed" in failure_body
    assert "- base_branch: main" in failure_body
    assert "- base_sha: abc123def456" in failure_body
    assert "run_id=a1b2c3d4" in failure_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure_body


def test_process_issue_terminal_failure_clears_the_repositorys_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #1775: a repository whose dispatch_label is ship must end a
    terminal failure ai-blocked ALONE — the blocked transition clears the
    same queue label the claim scanned, not only the host default."""
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
        Mock(side_effect=RuntimeError("git failed")))
    monkeypatch.setitem(
        failure_report.__dict__, "activity_snapshot",
        lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[0] == "gh" and command[1] == "api":
            return _gh_api(command, posted)
        if (command[0] == "gh" and command[1] == "issue"
                and command[2] == "view"):
            return json.dumps({"labels": [{"name": "ship"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    result = runner.process_issue(
        {"number": 8, "title": "Fail", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=tmp_path, prompt=tmp_path / "prompt.md",
            base_branch="main", dispatch_label="ship",
        ),
        "xqliu/orbi-backlog",
    )
    assert result.kind == "failed"
    # The claim keeps ship and adds the in-flight label; the terminal
    # blocked patch clears both and leaves ai-blocked alone.
    assert edits == [
        {"repo": "xqliu/orbi-backlog", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi-backlog", "add": "ai-blocked",
         "remove": "ai-in-progress"},
        {"repo": "xqliu/orbi-backlog", "remove": "ship"},
    ]


@pytest.mark.parametrize(
    ("repo_config", "queue_label", "other_label"),
    [
        # Issue #1775 acceptance 1: the repository's own queue label.
        ('dispatch_label = "ship"\n', "ship", "ai-ready"),
        # Issue #1775 acceptance 2: the default stays exactly as today.
        ("", "ai-ready", "ship"),
    ],
    ids=["custom_dispatch_label", "default_queue_label"],
)
def test_claim_then_blocked_ends_ai_blocked_alone(
    monkeypatch, tmp_path, repo_config, queue_label, other_label,
):
    """Issue #1775, end to end on the public surface: the Issue the claim
    scan selects is the one carrying the repository's RESOLVED queue label
    (`dispatch_label`, default `ai-ready`), and the blocked terminal clears
    that SAME label — the Issue ends `ai-blocked` alone, no queue-label
    residue on any surface that reads it.

    The distractor Issue carries the OTHER label and is NEWER, so a scan
    that read the wrong label would pick it and a blocked transition that
    cleared the wrong label would leave the residue behind."""
    fake = FakeGh("owner/repo")
    if repo_config:
        fake.set_repo_config(repo_config)
    # Oldest first: the queue-label Issue, then the newer distractor.
    fake.add_issue(8, title="task", body="", labels=(queue_label,))
    fake.add_issue(9, title="other", body="", labels=(other_label,))
    fake.add_pr(41, head="orbi/owner-repo-issue-8", state="CLOSED")
    monkeypatch.setattr(seam, "run_command", fake)
    runner.set_run_id("a1b2c3d4")
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main",
    )
    # The tick's policy resolution, exactly as `main` does it.
    policy = runner.load_repo_policy(config, "owner/repo")
    fused = runner.resolve_source_base_branch(config, "owner/repo", policy)
    assert (fused.dispatch_label or "ai-ready") == queue_label

    # 1. The claim scan looks for the repository's queue label.
    issue = claim.pick_ready_issue("owner/repo", None, config=fused)
    assert issue["number"] == 8
    # 2. The claim transition keeps it: the delivery is in flight.
    github.apply_label_patch(
        8, repo="owner/repo", event=runner.EVENT_CLAIM,
        current_labels=github.issue_labels(8, repo="owner/repo"),
    )
    assert fake.issues[8]["labels"] == [queue_label, "ai-in-progress"]
    # 3. The delivery's PR closed without a merge: the blocked terminal.
    runner.delivery_step(
        fake.prs[41]["url"], issue, fused, "owner/repo",
    )
    assert fake.issues[8]["labels"] == ["ai-blocked"]
    # The distractor Issue is untouched.
    assert fake.issues[9]["labels"] == [other_label]


def test_process_issue_delivery_no_commit_marks_blocked_without_crashing(
    monkeypatch, tmp_path,
):
    """Issue #239 regression: the commit-boundary failure of `deliver_pr`
    (the agent delivered no commit — HEAD is still the frozen base) flows
    through the NORMAL terminal failure path: the Issue is marked
    `ai-blocked` (removing `ai-in-progress`), the `Orbi failed`
    comment carries the no-commit reason and the run marker, and
    `process_issue` RETURNS `None` instead of re-raising — the failure is
    already terminal, so the tick must end cleanly and the service must
    not crash on the unhandled `RuntimeError` (the #239 scene). The
    Runner never auto-commits or expands the agent's commit boundary."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: worktree,
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    no_commit = RuntimeError(
        "the agent delivered no commit on the task branch (HEAD "
        "abc123def456 is still the frozen base abc123def456)"
    )

    def dead_deliver_pr(*args, **kwargs):
        raise no_commit

    monkeypatch.setattr(runner, "deliver_pr", dead_deliver_pr)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it.
    assert runner.process_issue(
        {"number": 239, "title": "No commit", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-blocked",
         "remove": "ai-in-progress"},
        {"repo": "xqliu/orbi", "remove": "ai-ready"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    failure = [
        body for body in comment_bodies
        if "Orbi: blocked" in body
    ]
    assert len(failure) == 1
    assert "delivered no commit" in failure[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure[0]
    # The blocked milestone notification carries the same scene.
    blocked = [body for body in posted if "Orbi: blocked" in body]
    assert blocked
    assert "delivered no commit" in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]


def test_process_issue_empty_commit_marks_blocked_without_crashing(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1581: the agent delivered a commit that changes no file
    (git commit --allow-empty). The REAL commit boundary detects the tree
    equal to the frozen base, logs the delivery_no_file_change event, and
    the Issue is marked ai-blocked through the normal terminal failure
    path: no branch is pushed, no PR is opened, and the blocked comment
    carries the last commit subject. The pre-#1581 HEAD == base test
    alone would have pushed a zero-file branch and merged it."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config",
                    "user.email", "t@example.com"],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config",
                    "user.name", "test"],
                   check=True, capture_output=True, env=env)
    (repo / "index.html").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "index.html"],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"],
                   check=True, capture_output=True, env=env)
    base_sha = _git_output(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add",
                    "origin", str(remote)],
                   check=True, capture_output=True, env=env)
    worktree = repo / ".worktrees" / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b",
         "orbi/xqliu-orbi-issue-239", str(worktree), "HEAD"],
        check=True, capture_output=True, env=env,
    )
    subject = (
        "Blocked: home page (index.html) is committed empty — "
        "no page to measure"
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--allow-empty",
         "-m", subject],
        check=True, capture_output=True, env=env,
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base",
        lambda repo_dir, base_branch: base_sha,
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
        lambda *args, **kwargs: worktree,
    )
    # The subprocess seam is patched through the fan-out target so the
    # ratcheted module-patch counts do not rise (Issue #789).
    monkeypatch.setattr(seam, "run_pi",
        lambda *args, **kwargs: "done",
    )
    monkeypatch.setattr(
        seam, "activity_snapshot", lambda session_dir: None,
    )
    real_run_command = journal.run_command
    comments = []
    posted = []

    def fake_run(command, **kwargs):
        # The commit boundary runs against the REAL git worktree; every
        # gh call is answered here, so nothing reaches the network.
        if command[0:1] == ["git"]:
            return real_run_command(command, **kwargs)
        if command[0:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[0:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        # The delivered branch stops before any further GitHub traffic:
        # the only remaining call is the blocked comment itself.
        comments.append(command[-1])
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 239, "title": "Empty commit", "body": ""},
            config_domain.RunnerConfig(
                repo_dir=repo, prompt=tmp_path / "prompt.md",
                base_branch="main",
            ),
            "xqliu/orbi",
        )
    assert result.kind == "failed"
    assert {"repo": "xqliu/orbi", "add": "ai-blocked",
            "remove": "ai-in-progress"} in edits
    blocked = [body for body in comments if "Orbi: blocked" in body]
    assert len(blocked) == 1
    assert "no file change" in blocked[0]
    assert subject in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]
    assert "delivery_no_file_change" in caplog.text
    # The stop is terminal BEFORE the push: the bare remote never saw the
    # task branch, and no PR was created.
    remote_refs = subprocess.run(
        ["git", "-C", str(remote), "for-each-ref",
         "--format=%(refname)", "refs/heads/"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert remote_refs == ""


def test_process_issue_model_wait_dead_failure_stays_in_progress(
    monkeypatch, tmp_path,
):
    """Issue #227 acceptance: the hung-model-request failure of the
    implementer session (the model service process is alive but the
    request never completes, the session JSONL froze in model_wait,
    stream_pi killed Pi and raised the classified `ModelWaitDeadError`)
    is RECOVERABLE: the Issue keeps `ai-in-progress` (never `ai-blocked`),
    the `Orbi model_wait recovered` comment carries the
    hung-model-request reason and the run marker, and `process_issue`
    returns `None` so the tick ends cleanly and the slot is released by
    `main`'s `finally`. The next tick's in-flight restart scan resumes
    the SAME run (same run id, branch, worktree, progress comment). No
    terminal label, no fallback."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is recoverable: `process_issue` returns `None` (no PR)
    # and does NOT re-raise — the service must not crash on it (Issue
    # #239), and the Issue must NOT be marked `ai-blocked` (Issue #227).
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    # The Issue keeps `ai-in-progress`: the ONLY label edit is the claim
    # at the start — no `ai-blocked`, no removal of `ai-in-progress`
    # (the edit calls are the dict entries; the `gh issue comment`
    # traffic is tuple entries).
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    # Issue #645: the recovery scene comment is published through the
    # `ProgressPublisher` (`gh api` POST), so it lands in `posted`.
    recovered = [
        body for body in posted
        if "Orbi model_wait recovered:" in body
    ]
    assert len(recovered) == 1
    # The hung-model-request reason and the run marker stay in the
    # Issue.
    assert "the model request is hung" in recovered[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in recovered[0]
    # The resumable scene is explicit: the Issue stays ai-in-progress
    # and the next tick resumes the same run.
    assert "stays ai-in-progress" in recovered[0]
    # No terminal failure comment was posted.
    assert not any(
        "Orbi failed:" in body for body in comment_bodies
    )


def _health_runs(repo_dir):
    from orbi import runner_health
    return runner_health.load_health_state(
        runner_health.health_state_path(repo_dir),
    )["runs"]


def test_process_issue_model_wait_failure_records_health_attempt(
    monkeypatch, tmp_path,
):
    """Issue #266/#246: a RECOVERABLE failure (the #227 model_wait
    scene — the run stays ai-in-progress and the next tick retries the
    same dead end) must reach health.json exactly like the terminal
    failure branch, or the repeated-failure health check is blind to
    the most common repeat-failure scene."""
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    runs = _health_runs(tmp_path)
    assert len(runs) == 1, f"the recoverable failure is recorded: {runs}"
    entry = runs[0]
    assert entry["repo"] == "xqliu/orbi"
    assert entry["issue"] == 218
    assert entry["run_id"] == "a1b2c3d4"
    assert entry["outcome"] == "failed"
    assert entry["fingerprint"]


def test_process_issue_three_recoverable_failures_raise_health_finding(
    monkeypatch, tmp_path,
):
    """The #246 scene through the #227 path: the same Issue fails three
    consecutive runs with the same classified recoverable failure — the
    repeated-failure health check must see the streak (before the fix
    none of these runs ever reached health.json)."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    for _ in range(3):
        assert runner.process_issue(
            {"number": 218, "title": "Model wait dead", "body": ""},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi",
        ).kind == "failed"
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    findings = runner_health.repeated_failure_findings(state)
    assert [
        (f["repo"], f["issue"], f["count"]) for f in findings
    ] == [("xqliu/orbi", 218, 3)]


def test_process_issue_success_records_health_streak_break(
    monkeypatch, tmp_path,
):
    """A delivered PR writes the non-failed outcome the streak-break
    contract (runner_health.repeated_failure_findings, whose tests pin
    outcome="pr_opened") has always expected from production: a
    failure -> success -> failure sequence must NOT read as a streak."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4",
    )
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    result = runner.process_issue(
        {"number": 4, "title": "Fix", "body": "Body"},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi-backlog",
    )
    assert result.kind == "pr"
    runs = _health_runs(tmp_path)
    assert [entry["outcome"] for entry in runs] == ["pr_opened"], runs
    # The delivered outcome breaks a pre-existing failure streak of the
    # same Issue: a later failure does not inherit the old failures.
    runner_health.record_run_attempt(
        runner_health.health_state_path(tmp_path),
        repo="xqliu/orbi-backlog", issue=4, run_id="a1b2c3d4",
        outcome="failed", fingerprint="whatever",
    )
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    assert runner_health.repeated_failure_findings(state) == []


def test_process_issue_recoverable_health_record_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    """The recoverable branch's health record is a pure bypass: a
    state-write failure logs and never changes the recoverable outcome
    (the Issue stays `failed`-recoverable, never `ai-blocked`)."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_run_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 218, "title": "Model wait dead", "body": ""},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi",
        )
    assert result.kind == "failed"
    assert "health_failure_record_failed" in caplog.text


def test_process_issue_success_health_record_failure_is_bypassed(
    monkeypatch, tmp_path, caplog,
):
    """The delivery path's health record is a pure bypass: a
    state-write failure logs and the PR result is returned unchanged."""
    from orbi import runner_health
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs: "https://github.com/orbi-build/orbi/pull/4",
    )
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_run_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    assert result.kind == "pr"
    assert "health_success_record_failed" in caplog.text


def test_process_issue_model_wait_dead_comment_failure_stays_in_progress(
    monkeypatch, tmp_path,
):
    """Issue #227: the recovery scene comment is the delivery record,
    but the resume does not parse it (the run state file, the worktree
    and the `ai-in-progress` label carry the resume —
    `worktree_resume_scene`): a failure of the comment must only log.
    Falling through to the generic failure handler would mark the
    Issue `ai-blocked` — exactly the unrecoverable state Issue #227
    forbids for the model_wait recovery."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    model_wait_dead = runner.ModelWaitDeadError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    )

    def dead_run_pi(*args, **kwargs):
        raise model_wait_dead

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            # Issue #645: the recovery scene comment is published through
            # the `ProgressPublisher` (`gh api` POST) — inject the
            # failure there.
            if ("--method" in command and "POST" in command
                    and "model_wait recovered" in command[-1]):
                raise RuntimeError(
                    "gh api comment POST failed: API rate limit exceeded",
                )
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    assert runner.process_issue(
        {"number": 218, "title": "Model wait dead", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    # The Issue keeps `ai-in-progress`: the ONLY label edit is the claim
    # at the start — the failed recovery comment must NOT fall through
    # to the generic handler's terminal `ai-blocked` (Issue #227).
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
    ]
    # No terminal failure comment was posted.
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    assert not any(
        "Orbi failed:" in body for body in comment_bodies
    )


def test_process_issue_idle_recovery_failure_marks_blocked(
    monkeypatch, tmp_path,
):
    """Issue #94 acceptance: the idle-recovery escalation of the
    implementer session (three idle cycles without any new activity,
    stream_pi killed Pi and raised) flows through the EXISTING
    `ai-blocked`/recoverable failure path exactly like the Issue #75
    upstream-dead failure: the Issue is marked `ai-blocked` (removing
    `ai-in-progress`), the `Orbi failed` comment carries the
    idle-recovery reason and the run marker, and `process_issue` returns
    `None` so the tick ends cleanly and the slot is released by `main`
    's `finally` (Issue #239: the handled failure never re-raises to
    crash the service). No special handling, no fallback."""
    calls = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(return_value=tmp_path),
    )
    idle_recovery = RuntimeError(
        "Pi session stayed idle for 15m after idle recovery (TERM/KILL "
        "of pre-idle descendants); Pi was killed (Issue #94)"
    )

    def dead_run_pi(*args, **kwargs):
        raise idle_recovery

    monkeypatch.setattr(pi_session, "run_pi", dead_run_pi)
    monkeypatch.setattr(
        failure_report, "activity_snapshot", lambda session_dir: None,
    )
    posted = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The failure is terminal: `process_issue` returns `None` (no PR) and
    # does NOT re-raise — the service must not crash on it (Issue #239).
    assert runner.process_issue(
        {"number": 94, "title": "Idle recovery", "body": ""},
        config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
        "xqliu/orbi",
    ).kind == "failed"
    edits = [entry for entry in calls if isinstance(entry, dict)]
    assert edits == [
        {"repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-blocked",
         "remove": "ai-in-progress"},
        {"repo": "xqliu/orbi", "remove": "ai-ready"},
    ]
    comment_bodies = [
        entry[2]["body"] for entry in calls
        if isinstance(entry, tuple) and entry[0] == "comment"
    ]
    failure = [
        body for body in comment_bodies
        if "Orbi: blocked" in body
    ]
    assert len(failure) == 1
    # The idle-recovery reason and the run marker stay in the Issue.
    assert "idle recovery" in failure[0]
    assert "(Issue #94)" in failure[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure[0]
    # The blocked milestone notification carries the same scene.
    blocked = [body for body in posted if "Orbi: blocked" in body]
    assert blocked
    assert "idle recovery" in blocked[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked[0]


def test_process_issue_ends_cleanly_when_reporting_fails(monkeypatch, tmp_path, caplog):
    """Issue #239: when the failure reporting itself fails (the `Orbi
    failed` comment POST dies), `process_issue` still RETURNS
    `None` instead of re-raising the original failure — the service must
    not crash. The `ai-blocked` edit already landed (it precedes the
    comment POST), so the Issue is terminal; the missing failure comment
    is logged (`failure reporting failed`) and the blocked scene is
    degraded, not fatal."""
    edit_calls = []

    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edit_calls.append(kwargs),
    )
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", Mock(side_effect=RuntimeError("git failed")))
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "issue"] and "comment" in command:
            # The `Orbi failed` comment POST is the only
            # non-bypass traffic of this scenario: it fails, the
            # failure report is broken, but `process_issue` still ends
            # cleanly (returns `None`) instead of crashing the service
            # (Issue #239). (Issue #79: the progress traffic — the
            # blocked milestone POST — is bypass, so a failure there
            # no longer breaks the failure report; see
            # test_progress_wiring.
            # test_process_issue_failure_path_progress_failure_keeps_
            # blocked_transition.)
            raise RuntimeError("github report failed")
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["gh", "issue", "list"]:
            # Restart-resume scan (Issue #18): fresh claim, no label.
            return "[]"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not issue-comment/list traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    with caplog.at_level("ERROR"):
        # The failure is terminal: `process_issue` returns `None` (no PR)
        # and does NOT re-raise — the service must not crash on it
        # (Issue #239).
        assert runner.process_issue({"number": 13, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert "failure reporting failed" in caplog.text
    # No progress comment was posted (the failure report died on the
    # failure-comment POST before the bypass steps).
    assert posted == []
    # The failure happened BEFORE the opened-PR transition (the
    # worktree creation failed), so the terminal state removes the
    # claim label, not the opened-PR label (Issue #79: the scene
    # comment is the first failure after the transition; its test in
    # test_progress_wiring pins the other branch).
    assert edit_calls == [
        {"repo": "xqliu/orbi-backlog", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi-backlog", "add": "ai-blocked",
         "remove": "ai-in-progress"},
        {"repo": "xqliu/orbi-backlog", "remove": "ai-ready"},
    ]


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
    # Issue #1290: the human instruction names the in-ticket command a
    # hosted tenant can actually use (no host CLI, no config edit).
    assert "/milestone" in create[0][create[0].index("--body") + 1]
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


def _write_prompts(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("prompt.md", "prompt_review.md"):
        (prompts / name).write_text("prompt", encoding="utf-8")


def test_idle_diagnostic_logs_ready_outside_active_milestone(
    monkeypatch, caplog,
):
    issues = [
        {"number": 834, "milestone": None},
        {"number": 828, "milestone": {"title": "v0.5.37"}},
        {"number": 827, "milestone": {"title": "v0.5.38"}},
    ]
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: issues)
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is True
    assert "ready_outside_milestone" in caplog.text
    assert "repo=owner/repo" in caplog.text
    assert "active_milestone=v0.5.38" in caplog.text
    assert "count=2" in caplog.text
    assert "no_ready_issue" not in caplog.text


def test_idle_diagnostic_keeps_no_ready_issue_when_queue_is_empty(
    monkeypatch, caplog,
):
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: [])
    with caplog.at_level(logging.INFO):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone" not in caplog.text


def test_idle_diagnostic_query_failure_falls_back_to_no_ready_issue(
    monkeypatch, caplog,
):
    def fail_query(*args, **kwargs):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(seam, "list_issues", fail_query)
    with caplog.at_level(logging.ERROR):
        assert runner.log_ready_outside_milestone(
            ["owner/repo"], "v0.5.38",
        ) is False
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "GitHub unavailable" in caplog.text


def test_main_uses_outside_milestone_outcome_instead_of_no_ready(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(runner.__dict__, "log_ready_outside_milestone", lambda *args, **kwargs: True)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "no_ready_issue" not in caplog.text


def test_main_query_failure_keeps_idle_exit(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    monkeypatch.setattr(seam, "list_issues", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("GitHub unavailable")
    ))
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.5.38"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert "ready_outside_milestone_check_failed" in caplog.text
    assert "no_ready_issue" in caplog.text


def test_main_returns_zero_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    assert runner.main(["--config", str(config)]) == 0


def test_main_passes_configured_active_milestone_to_the_claim_scan(
    monkeypatch, tmp_path,
):
    """Issue #139: the configured `active_milestone` reaches the claim
    scan (the fresh-claim scope), and an unconfigured one passes None
    (the compat behavior — no milestone filter)."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.2.0"\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "advance_active_milestone_on_idle", lambda *args, **kwargs: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] == "v0.2.0"


def test_main_advances_milestone_only_after_no_ready_issue(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.3.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls == [
        (("owner/repo", "v0.3.0", config.resolve(), tmp_path.resolve()),
         {
             "auto_next_milestone": True,
             "release_confirmation": False,
             "parse_version_title": runner._parse_version_title,
             "policy": None,
             "policy_path": ".github/orbi.toml",
             "base_branch": "main",
             "dispatch_label": runner.READY_LABEL,
             "version_file": None,
             "language_code": "en",
         }),
    ]


def test_main_passes_disabled_auto_next_milestone_to_idle_advance(
    monkeypatch, tmp_path,
):
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n'
        'active_milestone = "v0.3.0"\n'
        'auto_next_milestone = false\n',
        encoding="utf-8",
    )
    seen = {}
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(milestone.__dict__, "arm_release_ticket", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "advance_active_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen == {
        "auto_next_milestone": False,
        "release_confirmation": False,
        "parse_version_title": runner._parse_version_title,
        "policy": None,
        "policy_path": ".github/orbi.toml",
        "base_branch": "main",
        "dispatch_label": runner.READY_LABEL,
        "version_file": None,
        "language_code": "en",
    }


def test_main_idle_uses_the_single_milestone_reconcile_entry_point(
    monkeypatch, tmp_path,
):
    """Issue #856: the idle path makes ONE Milestone bookkeeping call, so a
    new step never becomes a second, separately-failing call."""
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        milestone.__dict__, "arm_release_ticket",
        lambda *args, **kwargs: pytest.fail("the runner must not arm directly"),
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ("owner/repo", "v0.4.0")
    assert calls[0][1]["release_confirmation"] is False
    assert not hasattr(runner, "arm_release_ticket")
    # The acceptance is structural: the idle path reaches Milestone
    # bookkeeping through reconcile alone, never by calling the arm or
    # the advance directly (Issue #856).
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "arm_release_ticket" not in source
    assert "advance_active_milestone_on_idle" not in source


def test_main_idle_passes_release_confirmation_from_the_repo_policy(
    monkeypatch, tmp_path,
):
    """Issue #856: the opted-in repository policy reaches the reconcile call.

    Issue #1342: the same per-key precedence applies to
    `auto_next_milestone`, so the repository value reaches the idle path.
    """
    monkeypatch.setitem(milestone.__dict__, "validate_active_milestone", lambda *args, **k: None)
    _write_prompts(tmp_path)
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.4.0")
    fake.set_repo_config(
        'dispatch_label = "dev-queue"\nrelease_confirmation = true\n'
        "auto_next_milestone = false\n"
    )
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(claim, "pick_next_delivery", lambda *args, **kwargs: None)
    seen = {}
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: seen.update(kwargs) or ("open", None),
    )
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.4.0"\n',
        encoding="utf-8",
    )
    assert runner.main(["--config", str(config)]) == 0
    assert seen["release_confirmation"] is True
    assert seen["dispatch_label"] == "dev-queue"
    # Issue #1342: `auto_next_milestone` is a repository policy key too --
    # the repo value (false) must reach the idle path even though the host
    # default is true, so a managed tenant can stop the auto-advance.
    assert seen["auto_next_milestone"] is False


def test_main_passes_none_active_milestone_when_unconfigured(
    monkeypatch, tmp_path,
):
    """Issue #139 compat: without the config field the claim scan
    receives None and keeps the pre-#139 scans."""
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    seen = {}

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["milestone"] = active_milestone
        return None

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["milestone"] is None


def _stub_idle_milestone_path(monkeypatch, outcome):
    """Stub the idle Milestone path down to one resolved outcome."""
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "sync_active_milestone_variable",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        milestone.__dict__, "reconcile_milestone_on_idle",
        lambda *args, **kwargs: outcome,
    )
    monkeypatch.setitem(
        runner.__dict__, "log_ready_outside_milestone",
        lambda *args, **kwargs: False,
    )


def test_main_retries_the_claim_unscoped_when_active_milestone_closed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 user journey: the configured Milestone is closed with no
    newer open one, so the idle tick's fresh ready scan runs again WITHOUT
    the Milestone filter and the open `ai-ready` Issue is claimed.

    Counter-test: remove the unscoped retry and the second scan never runs,
    `claimed` stays empty and this assertion fails."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    issue = {"number": 2, "title": "after the first release", "body": "b"}
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    def fake_ready_scan(repo, active_milestone=None, **_kwargs):
        scopes.append(active_milestone)
        return issue

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(claim.__dict__, "pick_issue", fake_ready_scan)
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    # The scoped scan found nothing, the unscoped retry claimed the Issue.
    assert scopes == ["v0.1.0", None]
    assert claimed == [2]


def test_main_closed_unscoped_scan_that_finds_nothing_stays_idle(
    monkeypatch, tmp_path,
):
    """Issue #1391: the unscoped re-scan is not a claim by itself. When the
    closed-Milestone repo has no ready Issue either, the tick re-scans with
    no Milestone filter (still the repo's dispatch label) and ends idle
    instead of starting a delivery."""
    _stub_idle_milestone_path(
        monkeypatch, (milestone.MILESTONE_CLOSED_UNSCOPED, None),
    )
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    rescans: list[tuple] = []
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: rescans.append(args) or None,
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert rescans == [("owner/repo", None)]


def test_main_closed_active_milestone_unscopes_the_repo_policy_milestone(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1391 through the real claim path: when the REPOSITORY policy
    declares `active_milestone` (`.github/orbi.toml`, the managed-sandbox
    default — a managed sandbox renders no host line), a closed one with no
    newer open Milestone must leave the unscoped re-scan unscoped too. The
    host fallback is only one of the two places the value can come from,
    and the policy value wins the scoped scan either way.

    Counter-test: drop the unscoped re-scan and the retry never runs, so
    the fake's milestone-scoped scan finds nothing and `claimed` stays
    empty."""
    _write_prompts(tmp_path)
    # The fake does not model `gh issue list --milestone` (the preflight's
    # closed-milestone report); the claim path under test stays the real one.
    monkeypatch.setitem(
        milestone.__dict__, "validate_active_milestone", lambda *args, **k: None,
    )
    fake = FakeGh("owner/repo")
    fake.add_milestone(1, title="v0.1.0", state="closed")
    fake.add_issue(2, title="after the first release", labels=("ai-ready",))
    fake.set_repo_config('active_milestone = "v0.1.0"\n')
    monkeypatch.setattr(seam, "run_command", fake)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\n', encoding="utf-8",
    )
    claimed: list[int] = []
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda selected, *args, **kwargs: claimed.append(selected["number"])
        or runner.IssueResult(
            "pr", "https://github.com/owner/repo/pull/2",
        ),
    )
    with caplog.at_level(logging.INFO):
        assert runner.main(["--config", str(config)]) == 0
    assert (
        "active_milestone_closed_unscoped repo=owner/repo closed=v0.1.0"
        in caplog.text
    )
    assert claimed == [2]


@pytest.mark.parametrize("outcome", [("open", None), ("closed", "v0.2.0")])
def test_main_keeps_the_scope_without_the_unscoped_outcome(
    monkeypatch, tmp_path, outcome,
):
    """Acceptance: while the active Milestone is open (or the engine can
    advance to a newer open one), the tick never drops the Milestone filter
    and an Issue outside it is not claimed."""
    _stub_idle_milestone_path(monkeypatch, outcome)
    monkeypatch.setattr(seam, "load_repo_policy", lambda *args, **kwargs: None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nactive_milestone = "v0.1.0"\n',
        encoding="utf-8",
    )
    scopes: list[str | None] = []

    def fake_pick(
        repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs,
    ):
        scopes.append(active_milestone)
        return None

    monkeypatch.setitem(claim.__dict__, "pick_next_delivery", fake_pick)
    monkeypatch.setitem(
        claim.__dict__, "pick_issue",
        lambda *args, **kwargs: pytest.fail("the scope must be kept"),
    )
    monkeypatch.setitem(
        runner.__dict__, "process_issue",
        lambda *args, **kwargs: pytest.fail("nothing is claimable"),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert scopes == ["v0.1.0"]


def test_main_processes_one_issue(monkeypatch, tmp_path):
    issue = {"number": 12, "title": "task", "body": "body"}
    calls = []
    steps = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "xqliu/orbi", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: calls.append((args, kwargs)) or runner.IssueResult("pr", "https://github.com/x/y/pull/12"))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    assert calls[0][0][0] == issue
    # Issue #788: the PR is open and the scene is written — the tick ends
    # WITHOUT running the review step in the same tick. The review is the
    # next tick's RESUME_REVIEW step, so the slot is released here and is
    # never held waiting for CI or mergeability.
    assert steps == []


def test_main_uses_process_result_kind_without_rechecking_task_type(
    monkeypatch, tmp_path,
):
    issue = {"number": 281, "title": "task", "body": "body",
             "labels": [{"name": "ai-ready"}]}
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args: runner.IssueResult("pr", "https://x/y/pull/281"),
    )
    monkeypatch.setattr(
        runner, "is_content_only",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        runner, "is_ops",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    monkeypatch.setattr(
        claim, "is_release",
        lambda issue: (_ for _ in ()).throw(
            AssertionError("main must not recheck task type")
        ),
    )
    steps = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: steps.append(args),
    )

    assert runner.main(["--config", str(config)]) == 0
    # Issue #788: a fresh claim ends its tick when the PR opens; the
    # review step runs on a later tick through the resume scan.
    assert steps == []


def test_main_ends_tick_when_process_issue_delivers_nothing(
    monkeypatch, tmp_path,
):
    """Issue #239: when `process_issue` returns `None` (a terminal
    delivery failure it already handled — the Issue is `ai-blocked` and
    the failure comment is posted), `main` ends the tick cleanly: it
    returns 0 and NEVER calls `delivery_step` (there is no PR to
    wait for). The service must not crash on the handled failure."""
    issue = {"number": 239, "title": "task", "body": "body"}
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\nprompt = \"prompt.md\"\n", encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args, **kwargs: runner.IssueResult("failed", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_ticket_only_finishes_without_entering_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Ticket-only work has no PR, so the normal review/merge wait is invalid."""
    issue = {
        "number": 12, "title": "Launch copy", "body": "Write copy",
        "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}],
    }
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("ticket-only", None))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ticket-only work must not enter PR delivery wait")
        ),
    )

    assert runner.main(["--config", str(config)]) == 0


def test_main_release_success_ends_tick_without_pr_delivery_wait(
    monkeypatch, tmp_path,
):
    """Issue #269: a release delivery returns a Release URL, not a PR URL.

    `process_release` already closed the delivery (tag pushed, GitHub
    Release published, Issue `ai-merged` and closed), so `main` must end
    the tick with exit code 0 and NEVER enter the PR delivery wait —
    `pr_number` on `.../releases/tag/v0.3.0` raises `ValueError` and
    crashed the Runner (run_id=37216af5)."""
    issue = {
        "number": 254, "title": "Release v0.3.0", "body": "Release v0.3.0",
        "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
    }
    release_url = "https://github.com/orbi-build/orbi/releases/tag/v0.3.0"
    waits = []
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *args: runner.IssueResult("release", release_url))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append(args) or (
            _ for _ in ()
        ).throw(AssertionError(
            "release delivery must not enter PR delivery wait"
        )),
    )

    assert runner.main(["--config", str(config)]) == 0
    # No PR was delivered: the delivery wait must not run.
    assert waits == []


def test_main_routes_fix_needed_resume_to_delivery_wait(
    monkeypatch, tmp_path, bind_run_id,
):
    """A resumed `ai-fix-needed` delivery goes straight to the delivery
    wait (Issue #82: the review session fixes findings in the same
    session, so there is no cold-start fixer to run). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    bind_run_id(None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = {
        "run_id": "a1b2c3d4",
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": "https://github.com/owner/repo/pull/12",
    }
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        resume_verification, "verify_resumed_pr",
        lambda *args, **kwargs: scene["pr_url"],
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene["pr_url"], issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_routes_awaiting_review_resume_to_delivery_wait(
    monkeypatch, tmp_path, bind_run_id,
):
    """A resumed `ai-pr-opened` delivery (stranded by a dead runner or
    the Issue #70 progress 404) goes straight to the delivery wait:
    the independent review of the same PR runs (Issue #45 round-5
    contract: a clean PR is never sent to a fixer). The run id from
    the scene is bound before the wait so the resumed review's journal
    lines and comments carry it (Issue #41). Issue #89: the wait
    receives the URL the resume verification returned, never the raw
    comment string."""
    bind_run_id(None)
    _write_prompts(tmp_path)
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    issue = {"number": 12, "title": "task", "body": "body"}
    scene = {
        "run_id": "a1b2c3d4",
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": "https://github.com/owner/repo/pull/12",
    }
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene,
        ),
    )
    # The resume pre-validation (Issue #89) is stubbed: the dispatch
    # test proves the wait receives its verified URL.
    monkeypatch.setattr(
        resume_verification, "verify_resumed_pr",
        lambda *args, **kwargs: scene["pr_url"],
    )
    waits = []
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *args, **kwargs: waits.append((args, kwargs)),
    )
    assert runner.main(["--config", str(config)]) == 0
    # No fixer for a clean PR: the wait runs on the verified PR URL.
    assert waits[0][0][:2] == (scene["pr_url"], issue)
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == "a1b2c3d4"


def test_main_rejects_repeated_source_repo_before_execution(
    monkeypatch, tmp_path,
):
    _write_prompts(tmp_path)
    seen = []
    config = tmp_path / "orbi.toml"
    config.write_text(
        "source_repos = [\"xqliu/orbi\", \"xqliu/orbi-backlog\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda *args, **kwargs: seen.append(args) or None,
    )
    assert runner.main(["--config", str(config)]) == 1
    assert seen == []


def test_main_requires_prompt_file(monkeypatch, tmp_path):
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runner.main(["--config", str(config)])


def test_process_issue_failure_without_session_still_carries_scene(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pi died")),
    )
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: None)
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    # No session file yet: the scene still carries the full debug entry
    # (worktree, branch) with '-' session fields.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body
    assert "session log: `<unavailable>`" in failure_body


def test_tail_text_strips_raw_and_caret_sgr_sequences(tmp_path):
    log = tmp_path / "coloured.log"
    log.write_bytes(
        b"\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m10 passed\x1b[39m\n"
        b"^[[32m\xe2\x9c\x93^[[39m tests/cta-ref.test.js\n"
    )

    result = failure_report._tail_text(log)

    assert "Test Files  10 passed" in result
    assert "tests/cta-ref.test.js" in result
    assert "\x1b[" not in result
    assert "^[" not in result


def test_tail_text_preserves_plain_text_byte_for_byte(tmp_path):
    log = tmp_path / "plain.log"
    content = b"plain output  \ntrailing whitespace\t"
    log.write_bytes(content)

    assert failure_report._tail_text(log) == content.decode()


def test_tail_text_preserves_non_sgr_lookalikes(tmp_path):
    log = tmp_path / "lookalikes.log"
    content = "^[[1] [32m literal \x1b without bracket"
    log.write_text(content, encoding="utf-8")

    assert failure_report._tail_text(log) == content


def test_failure_summary_removes_command_and_stderr_duplication():
    summary = failure.failure_summary(
        "Review failed: Command ['pi', '--session', '/home/runner/session'] "
        "returned non-zero exit status 1; stderr=provider exploded", language_code="en",
    )

    assert "Command [" not in summary
    assert "stderr=" not in summary
    assert "/home/" not in summary
    assert summary.count("provider exploded") == 1
    assert "delivery command failed" in summary
    assert failure.failure_summary(
        "provider exploded stderr=provider exploded", language_code="en"
    ) == "provider exploded"


def test_failure_summary_defaults_when_reason_has_no_readable_content():
    assert failure.failure_summary(" stderr=", language_code="en") == "the delivery command failed"


def test_failure_summary_uses_concrete_final_stderr_line_and_redacts_it():
    summary = failure.failure_summary(
        "the independent review failed: Command ['pi', '--session-dir', "
        "'/home/runner/session'] returned non-zero exit status 1. "
        "stderr=mise installed 0 tools\n"
        "\n"
        "provider log: /home/runner/private/session.jsonl\n"
        "Codex error: The usage limit has been reached", language_code="en",
    )

    assert summary == (
        "the independent review failed: the delivery command failed: "
        "Codex error: The usage limit has been reached"
    )
    assert "/home/" not in summary


def test_failure_scene_is_structured_and_redacts_local_paths():
    rendered = failure_report._failure_scene(
        {"session_id": "s1", "session_file": "/home/a/.pi-session/s.jsonl",
         "phase": "review", "last_activity": "2026-09-20T18:00:00Z",
         "action": "run tests", "result": "failed"},
        run_id="abcdef12", issue="orbi-build/orbi#1221", role="review",
        branch="orbi/task", worktree="/home/a/worktree", pr_url="https://github.com/o/r/pull/1", language_code="en",
    )

    assert "- run: `abcdef12`" in rendered
    assert "- worktree: `local runner worktree`" in rendered
    assert "- session log: `local session log`" in rendered
    assert "/home/" not in rendered
    assert "|" not in rendered


def test_failure_comment_layout_keeps_raw_evidence_collapsed():
    body = failure.failure_comment_body(
        outcome="fix needed", action="Repair the delivery.",
        reason="the provider rejected the request",
        diagnosis="provider rejected the request",
        scene="- run: `abcdef12`\n- phase: `review`",
        evidence="\n\nstderr_tail:\n```\nprovider rejected the request\n```",
        pr_url="https://github.com/o/r/pull/1", issue="o/r#1221",
        run_id="abcdef12", language_code="en",
    )

    visible, collapsed = body.split("<details>", 1)
    assert "Orbi: fix needed — the engine will retry" in visible
    assert "provider rejected the request" in visible
    assert "stderr_tail:" not in visible
    assert "https://github.com/o/r/pull/1" in visible
    assert "run_id=abcdef12" in visible
    assert "stderr_tail:" in collapsed
    assert "```" in collapsed
    assert body.endswith("</details>")

    without_diagnosis = failure.failure_comment_body(
        outcome="fix needed", action="", reason="provider failed",
        diagnosis="", scene="- run: `abcdef12`", evidence="",
        pr_url=None, issue="o/r#1221", run_id="abcdef12", language_code="en",
    )
    assert "failure detail:" not in without_diagnosis
    assert without_diagnosis.endswith("</details>")


def test_blocked_failure_suffix_stays_inside_closed_diagnostics(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        runner.UnrecoverableDeliveryError("cannot recover"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id="abcdef12", pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_REVIEW,
        classify=True, blocked_suffix="the preserved PR needs a decision",
        current_labels=set(), language_code="en",
    )

    assert posted[0].count("<details>") == 1
    assert posted[0].count("</details>") == 1
    assert "the preserved PR needs a decision\n</details>" in posted[0]


def test_no_run_id_failure_still_preserves_action_and_evidence(
    monkeypatch, tmp_path,
):
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: posted.append(body))

    failure_report.report_delivery_failure(
        subprocess.CalledProcessError(2, ["pi"], stderr="provider failed"),
        issue={"number": 1, "title": "t", "labels": []},
        source_repo="owner/repo", run_id=None, pr_url=None,
        worktree=tmp_path, branch="orbi/task", role=runner.ROLE_IMPLEMENT,
        action="Repair provider access.", reason="delivery failed",
        diagnosis="provider failed", classify=False, evidence=True,
        current_labels=set(), language_code="en",
    )

    assert "**Action:** Repair provider access." in posted[0]
    assert "stderr_tail:" in posted[0]
    assert "provider failed" in posted[0]
    # Issue #1684: the blocked diagnostic ends with the community help
    # line, after the closed evidence block.
    assert posted[0].endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_failure_evidence_handles_binary_streams_and_unavailable_files(tmp_path):
    error = subprocess.CalledProcessError(
        2, ["pi"], output=b"binary stdout", stderr=b"binary stderr",
    )

    evidence = failure_report._failure_evidence(tmp_path / "missing", error, language_code="en")

    assert "exit_code=2" in evidence
    assert "stderr_tail:\n```" in evidence
    assert "stdout_tail:\n```" in evidence
    assert "binary stderr" in evidence
    assert "binary stdout" in evidence
    # Issue #775: raw segments are fenced; a missing file stays a
    # placeholder inside its fence instead of a raw dump.
    assert "test_log_tail:\n```\n<unavailable>\n```" in evidence
    assert "session_last_events (last 20 records; full log: <unavailable>):" \
        in evidence
    assert failure_report._tail_text(tmp_path / "missing.log") == "<unavailable>"


def test_failure_evidence_includes_streams_session_and_test_tail(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    (worktree / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:00:00Z",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "review the PR"},
            ]},
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-12T10:01:00Z",
            "message": {"role": "toolResult", "toolName": "Bash",
                        "isError": False},
        }) + "\n",
        encoding="utf-8",
    )
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        "test output\n1 failed, 2 passed\n", encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"], output="stdout tail", stderr="")

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    assert "exit_code=1" in evidence
    assert "stdout tail" in evidence
    assert "1 failed, 2 passed" in evidence
    # Issue #775: the session tail is a structural summary, not raw JSONL.
    assert "2026-09-12T10:00:00Z message role=user content=text" in evidence
    assert "2026-09-12T10:01:00Z message role=toolResult tool=Bash" in evidence
    assert "full log: local session log" in evidence


def test_failure_evidence_keeps_coverage_table_literal_inside_fence(tmp_path):
    """Issue #775: the vitest coverage table must render as monospace
    literal text on GitHub, never parsed as a markdown table."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    raw_record = json.dumps({
        "type": "message", "timestamp": "t1",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "**bold** | not | a | table"},
        ]},
    })
    (worktree / ".pi-session" / "s.jsonl").write_text(raw_record + "\n")
    (worktree / ".orbi").mkdir()
    (worktree / ".orbi" / "test.log").write_text(
        " src      | stmts |\n"
        "----------|-------\n"
        " billing  | 96%   |\n",
        encoding="utf-8",
    )
    error = subprocess.CalledProcessError(1, ["pi"])

    evidence = failure_report._failure_evidence(worktree, error, language_code="en")

    # Every raw segment opens its fence directly under the label.
    for label in ("stderr_tail:", "stdout_tail:", "test_log_tail:"):
        assert f"\n{label}\n```" in evidence
    # The table separator line survives verbatim inside the test-log
    # fence (label -> fence -> table row).
    assert "test_log_tail:\n```\n src      | stmts |\n----------|-------\n" \
        in evidence
    # The session record text never enters the comment: the summary
    # carries the shape, the fence carries the log path.
    assert "bold" not in evidence
    assert raw_record not in evidence


def test_failure_evidence_dedupes_repeated_session_text_block(tmp_path):
    """Issue #775: pi logs the final assistant text twice (once inside
    the full message, once standalone); the summary keeps it once."""
    worktree = tmp_path / "wt"
    (worktree / ".pi-session").mkdir(parents=True)
    final_text = "审查完成。结论：verdict pass"
    duplicated = [
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking"}, {"type": "text", "text": final_text},
         ]}},
        {"type": "message", "timestamp": "2026-09-12T10:44:12.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": final_text},
         ]}},
    ]
    (worktree / ".pi-session" / "s.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in duplicated),
        encoding="utf-8",
    )

    evidence = failure_report._failure_evidence(
        worktree, subprocess.CalledProcessError(1, ["pi"]), language_code="en",
    )

    assert evidence.count("role=assistant") == 1
    assert final_text not in evidence


def test_session_summary_skips_malformed_records_and_respects_limit(tmp_path):
    """Defensive edges: malformed lines, messages and blocks are skipped,
    the toolCall kind carries its name, the limit keeps the LAST records,
    and an all-unparsable log degrades to the placeholder."""
    good = {"type": "message", "timestamp": "t", "message": {
        "role": "assistant",
        "content": [{"type": "toolCall", "name": "Bash"}, "junk", 3],
    }}
    path = tmp_path / "s.jsonl"
    path.write_text(
        "not json\n"
        "[1, 2]\n"
        + json.dumps({"type": "x", "message": "not-a-dict"}) + "\n"
        + json.dumps(good) + "\n",
        encoding="utf-8",
    )

    assert failure_report._session_summary(path, limit=1) == (
        "t message role=assistant content=toolCall:Bash"
    )

    many = tmp_path / "many.jsonl"
    many.write_text("".join(
        json.dumps({"type": f"r{i}", "timestamp": f"t{i}"}) + "\n"
        for i in range(5)
    ), encoding="utf-8")
    assert failure_report._session_summary(many, limit=2) == "t3 r3\nt4 r4"

    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n[1]\n", encoding="utf-8")
    assert failure_report._session_summary(bad) == "<unparsable>"

    # An unreadable/missing file is the placeholder (also the race path
    # where the session vanishes between the glob and this read).
    assert failure_report._session_summary(tmp_path / "missing.jsonl") == "<unavailable>"


def test_failure_evidence_truncates_oversized_segment_with_note(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    evidence = failure_report._failure_evidence(
        worktree,
        subprocess.CalledProcessError(1, ["pi"], stderr="x" * 9000), language_code="en",
    )

    assert "[truncated, 5000 chars omitted]" in evidence


def test_report_delivery_failure_caps_comment_and_names_session_log(
    monkeypatch, tmp_path,
):
    """Issue #775 + #1221: an oversized raw cause becomes a bounded
    visible summary while its bounded diagnostic evidence remains reachable."""
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *args, **kwargs: set())
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    error = subprocess.CalledProcessError(1, ["pi"])
    worktree = tmp_path / "wt"
    session_file = worktree / ".pi-session" / "s.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n", encoding="utf-8")

    outcome = failure_report.report_delivery_failure(
        error, issue={"number": 775, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=worktree, branch="b", role=runner.ROLE_IMPLEMENT,
        cause="x" * 25000,
        classify=False, evidence=True, language_code="en",
    )

    assert outcome == "blocked"
    body = posted[0]
    # Issue #1322: the hidden machine-readable block leads the readable
    # hierarchy (the run marker is prepended by `report_delivery_failure`
    # when a run_id is bound; this call has none).
    assert body.startswith("<!-- orbi:failure:v1 ")
    assert "\n\nOrbi: blocked — waiting on a human decision" in body
    assert "**Reason:** " + "x" * 500 in body
    assert len(body) < failure_report.FAILURE_COMMENT_MAX_CHARS
    assert "full log: local session log" in body
    # Issue #1684: the blocked diagnostic ends with the community help
    # line; the body stays under the cap, so it is not truncated away.
    assert body.endswith(
        "Need help? Join the Orbi community: "
        "https://t.me/+kJRTTpFGBRU0M2Y9"
    )


def test_process_issue_failure_comment_includes_session_scene(monkeypatch, tmp_path):
    calls = []
    (tmp_path / "wt" / ".pi-session").mkdir(parents=True)
    (tmp_path / "wt" / ".pi-session" / "session.jsonl").write_text(
        json.dumps({
            "type": "message", "timestamp": "2026-08-25T02:30:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "assistant event"},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "wt" / ".orbi").mkdir()
    (tmp_path / "wt" / ".orbi" / "test.log").write_text(
        "coverage output\n1 failed\n", encoding="utf-8",
    )
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="boom"),
        ),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-9",
        "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
        "phase": "test",
        "last_activity": "2026-08-25T02:30:00Z",
        "action": "bash pytest tests/",
        "result": "ok",
    })
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the scene assertions below are unchanged.
    assert runner.process_issue({"number": 8, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    failure_body = calls[-1][2]["body"]
    assert "Orbi: blocked" in failure_body
    assert "session log: `local session log`" in failure_body
    assert "phase: `test`" in failure_body
    assert "last activity: `2026-08-25T02:30:00Z`" in failure_body
    assert "action: `bash pytest tests/`" in failure_body
    assert "result: `ok`" in failure_body
    assert "exit_code=1" in failure_body
    assert "stderr=boom" not in failure_body
    assert "stderr_tail:\n```\nboom\n```" in failure_body
    # Issue #775: the session tail appears as its structured summary
    # line inside a fence, not as raw session text.
    assert "2026-08-25T02:30:00Z message role=assistant content=text" \
        in failure_body
    assert "assistant event" not in failure_body
    assert "1 failed" in failure_body
    # The reader-facing scene uses stable labels for local paths.
    assert "worktree: `local runner worktree`" in failure_body
    assert "branch: `orbi/xqliu-orbi-backlog-issue-8`" in failure_body


def test_process_issue_isolates_scene_lookup_failure(monkeypatch, tmp_path, caplog):
    calls = []
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    monkeypatch.setattr(seam, "freeze_base", lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if command[:3] == ["git", "worktree", "prune"]:
            # Issue #256 terminal cleanup — not a delivery comment.
            return ""
        calls.append(("comment", (), {"body": command[-1]}))
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The main-path resume-context read (Issue #219) succeeds; the
    # failure-scene lookup is the one that dies on the disk error.
    snapshot_calls = []

    def flaky_snapshot(session_dir):
        snapshot_calls.append(1)
        if len(snapshot_calls) == 1:
            return None
        raise OSError("disk error")

    monkeypatch.setattr(failure_report, "activity_snapshot", flaky_snapshot)
    # The main-path scene read now lives in `pi_session.resume_context`
    # (Issue #1262): patch the same flaky snapshot there too so the
    # first (resume-context) call succeeds and the runner's
    # failure-scene lookup is the one that dies.
    monkeypatch.setattr(pi_session, "activity_snapshot", flaky_snapshot)
    with caplog.at_level("ERROR"):
        # Issue #239: the failure is terminal — `process_issue` returns
        # `None` instead of re-raising; the scene-isolation assertions
        # below are unchanged.
        assert runner.process_issue({"number": 9, "title": "Fail", "body": ""}, config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"), "xqliu/orbi-backlog").kind == "failed"
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "git failed" in failure_body


def test_log_format_has_no_python_timestamp():
    # journald already provides time, host and process (Issue #40): the
    # Python logger must not print a second timestamp.
    formatter = logging.Formatter(runner.log_format())
    record = logging.LogRecord(
        "orbi.bootstrap", logging.INFO, "file", 1, "message", None, None,
    )
    assert formatter.format(record) == "INFO message"


# --- live GitHub progress while Pi runs (Issue #18, review Major fix) ------


def test_stream_pi_invokes_progress_callback_while_child_is_running(
    tmp_path,
):
    """The review Major fix: the progress callback (which PATCHes the
    GitHub comment) must fire WHILE the Pi child is still running — on
    activity changes and on heartbeats — not only after it exits."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="final answer",
        sleep=1.2,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The callback fired many times during the run (polls at 0.1 s while
    # the child sleeps 1.2 s after the last session record)...
    assert len(seen) >= 4
    # ...and it saw the activity change (starting -> bash) live, before
    # the child exited: the tool call is visible in an early snapshot,
    # not only in a final state.
    # Issue #176: the live snapshots show the startup sub-phases
    # (request_pending: the session exists, the first response has not
    # arrived yet), not the generic `starting`.
    # Issue #741: the FIRST frame is load-dependent — on a busy machine
    # the first poll can predate the session file, so that frame is
    # `session_pending` (equally legal, pinned by the dedicated
    # regression test). This test pins the ORDER: request_pending must
    # appear, and before the tool phase.
    phases = [entry["phase"] for entry in seen]
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")
    assert phases.index("bash") < len(phases) - 1
    tool_entries = [entry for entry in seen if entry["phase"] == "bash"]
    assert any(
        entry["action"] == "bash pytest tests/"
        for entry in tool_entries
    )


def test_stream_pi_progress_first_frame_may_be_session_pending(tmp_path):
    """Issue #741 regression: on a busy machine the first progress frame
    fires BEFORE Pi creates its session file, so the startup sub-phase
    of that frame is `session_pending` — as legal as `request_pending`
    (Issue #176). The session record below is delayed by 1.0 s, so the
    forced scene is the exact #741 flake: the first frames predate the
    session file."""
    (_, session), (_, user), (_, assistant) = fake_session_records()
    command = make_fake_pi(
        tmp_path,
        session_records=[(1.0, session), (0.1, user), (0.1, assistant)],
        stdout="final answer",
        sleep=0.6,
    )
    seen = []

    def progress(activity):
        seen.append({
            "phase": activity["phase"],
            "action": activity["action"],
            "result": activity["result"],
        })

    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.05), cwd=tmp_path, progress=progress)
    assert result == "final answer"
    # The forced #741 scene: the first frame predates the session file,
    # so it is the `session_pending` startup sub-phase — never a reason
    # to fail (Issue #741).
    phases = [entry["phase"] for entry in seen]
    assert phases[0] == "session_pending"
    # The order contract (Issue #176): the startup sub-phases stay
    # visible and ordered — request_pending appears, before the tool
    # phase — regardless of which frame the sampling started in.
    assert "request_pending" in phases
    assert "bash" in phases
    assert phases.index("request_pending") < phases.index("bash")


def test_stream_pi_live_progress_patches_github_before_child_exits(
    tmp_path,
):
    """End-to-end shape of the fix: a publisher-style callback PATCHes
    the run-marker comment while the child is still running, so a mobile
    user never sees a static starting comment for the whole run."""
    command = make_fake_pi(
        tmp_path,
        session_records=fake_session_records(),
        stdout="done",
        sleep=1.2,
    )
    patches = []

    class FakePublisher:
        language_code = "en"
        comment_id = 77

        def patch(self, body):
            patches.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="owner/repo"), publisher, title="Live progress", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=throttle)
    assert result == "done"
    # At least one PATCH happened while the child was still running
    # (all of them did: the callback only exists inside the poll loop).
    assert len(patches) >= 2
    # The live comment carries the run marker and the live phase.
    assert all(
        body.startswith("<!-- orbi:run=deadbeef -->")
        for body in patches
    )
    assert any("- phase: bash" in body for body in patches)
    # Issue #1657: the live body carries the versioned summary block and
    # the safe activity; the raw bash command reaches neither.
    assert all(body.count("<!-- orbi:progress:v1 ") == 1 for body in patches)
    assert any("- activity: command" in body for body in patches)
    assert not any("pytest tests/" in body for body in patches)


def test_stream_pi_progress_callback_error_never_interrupts_run(
    tmp_path, caplog,
):
    """Observability is best-effort: a failing progress callback (e.g. a
    transient gh failure) is logged and never interrupts the delivery."""
    command = make_fake_pi(
        tmp_path, session_records=fake_session_records(),
        stdout="final answer",
    )

    def boom(activity):
        raise RuntimeError("gh api failed")

    with caplog.at_level("ERROR"):
        result = pi_session.stream_pi(command, ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, progress=boom)
    assert result == "final answer"
    assert "progress_publish_failed" in caplog.text
    assert "run=deadbeef" in caplog.text
    assert "role=implement" in caplog.text


def test_live_progress_throttle_patches_on_change_and_cadence():
    """The throttle passes an update through on a visible activity change
    and at most every PI_HEARTBEAT_SECONDS (30 s) otherwise."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=1, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Throttle task", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "starting", "action": None, "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
    }
    throttle(first)
    assert len(calls) == 1  # the first update always passes
    # Unchanged activity within the cadence: suppressed.
    throttle(first)
    assert len(calls) == 1
    # A visible change: passed immediately.
    changed = dict(
        first, phase="test", action="bash pytest tests/", kind="command",
    )
    throttle(changed)
    assert len(calls) == 2
    # Unchanged again within the cadence: suppressed...
    throttle(changed)
    assert len(calls) == 2
    # ...until the 30 s cadence elapses: passed as a heartbeat update.
    throttle._last_patch -= progress.PI_HEARTBEAT_SECONDS + 1
    throttle(changed)
    assert len(calls) == 3
    # The rendered body carries the live state (Issue #1657: the readable
    # activity and the block share the safe value; the raw command never
    # reaches either).
    assert "- phase: test" in calls[-1]
    assert "- activity: command" in calls[-1]
    assert "pytest tests/" not in calls[-1]
    assert calls[-1].count("<!-- orbi:progress:v1 ") == 1


def test_live_progress_throttle_patches_on_recovery_change():
    """Issue #94: the recovery state is part of the visible progress —
    entering idle recovery (None -> `term`) PATCHes the live comment
    immediately even though phase/action/result did not change, and
    the rendered body carries the `recovery` line."""
    calls = []

    class FakePublisher:
        language_code = "en"
        comment_id = 1

        def patch(self, body):
            calls.append(body)

    publisher = FakePublisher()
    throttle = progress.LiveProgressThrottle(RunContext(run_id="deadbeef", issue=94, branch="b", worktree=Path("/w"), source_repo="owner/repo"), publisher, title="Idle recovery", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal")
    first = {
        "phase": "test", "action": "bash pytest tests/", "result": None,
        "model_wait": False, "session_id": None,
        "last_activity": None, "stale_seconds": 0.0,
        "session_file": None, "events": 0, "changed": False,
        "recovery": None,
    }
    throttle(first)
    assert len(calls) == 1
    assert "- recovery" not in calls[0]  # no recovery line when idle-recovery is off
    # Entering recovery: visible change -> immediate PATCH with the line.
    throttle(dict(first, recovery="term"))
    assert len(calls) == 2
    assert "- recovery: term" in calls[1]
    # Escalating to KILL: another visible change -> another PATCH.
    throttle(dict(first, recovery="kill"))
    assert len(calls) == 3
    assert "- recovery: kill" in calls[2]
    # Back to None after the resume: visible change -> PATCH without the line.
    throttle(first)
    assert len(calls) == 4
    assert "- recovery" not in calls[3]


def test_run_pi_passes_progress_callback_to_stream_pi(monkeypatch, tmp_path):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()), progress=callback)
    assert seen["progress"] is callback
    assert seen["ctx"].run_id == "a1b2c3d4"
    assert seen["ctx"].branch == "b"


def test_run_review_passes_progress_callback_to_stream_pi(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    callback = lambda activity: None  # noqa: E731
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=()), 1, progress=callback)
    assert seen["progress"] is callback
    assert seen["role"] == "review"


# --- Issue #228: the configured model_wait threshold reaches stream_pi ------

def test_run_pi_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the implement session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=(), model_wait_dead_seconds=1234.5))
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_review_passes_configured_model_wait_dead_seconds(
    monkeypatch, tmp_path,
):
    """Issue #228 wiring: the review session uses the CONFIGURED
    threshold, not the module constant."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    pi_session.run_review(RunContext(run_id=config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5).run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config_domain.RunnerConfig(prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), base_branch="main", run_id="a1b2c3d4", skills=(), model_wait_dead_seconds=1234.5), 1)
    assert seen["watch"].model_wait_dead_seconds == 1234.5
    assert seen["watch"].model_wait_dead_seconds != (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


def test_run_pi_keeps_module_default_without_config_key(
    monkeypatch, tmp_path,
):
    """Issue #228: a caller config without the key (the pre-#228 shape)
    keeps the module constant — the real `load_config` always provides
    the key, this only pins the fallback."""
    seen = {}

    def fake_stream(command, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(pi_session, "stream_pi", fake_stream)
    monkeypatch.setattr(pi_session, "render_prompt", lambda template, values: "sp")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    pi_session.run_pi({"number": 4, "title": "Fix", "body": "b"}, RunContext(run_id=config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()).run_id, issue={"number": 4, "title": "Fix", "body": "b"}["number"], branch="b", worktree=tmp_path, source_repo="owner/repo"), config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, base_branch="main", base_sha="abc123", run_id="a1b2c3d4", skills=(), context_files=()))
    assert seen["watch"].model_wait_dead_seconds == (
        runner.PI_MODEL_WAIT_DEAD_SECONDS)


# --- role-specific --skill lists (Issue #83) ---------------------------------


def _skill_config(tmp_path, *names):
    """Build a config whose skills point at <name>/SKILL.md files."""
    skills = []
    for name in names:
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        skills.append(skill_dir / "SKILL.md")
    return config_domain.RunnerConfig(prompt=tmp_path / "prompt.md", prompt_review=tmp_path / "prompt_review.md", repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=skills, base_branch="main", base_sha="abc123def456", run_id="a1b2c3d4")


def _command_skills(command):
    """Return the --skill values of an assembled pi command."""
    return [
        value for index, item in enumerate(command)
        if item == "--skill" and (value := command[index + 1])
    ]


def test_run_pi_keeps_tdd_dev_and_code_review_drops_review_fix_loop(
    monkeypatch, tmp_path,
):
    """Issue #83: the implementer/fixer keeps the delivery skills
    (tdd-dev, code-review) but not review-fix-loop — the Runner runs
    the independent review/fix loop itself after the PR is open."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    skills = _command_skills(calls[0])
    assert any("tdd-dev" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)


def test_run_review_keeps_only_code_review(monkeypatch, tmp_path):
    """Issue #83: the read-only review session must not load the
    delivery-oriented skills (tdd-dev would steer it into the
    implement/test/PR flow, review-fix-loop would open another
    fix/review round); code-review stays."""
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert not any("tdd-dev" in skill for skill in skills)
    assert not any("review-fix-loop" in skill for skill in skills)
    assert any("code-review" in skill for skill in skills)


def test_run_pi_and_run_review_skill_lists_differ(monkeypatch, tmp_path):
    """Issue #83 acceptance: the two assembled command lines carry
    different --skill lists."""
    (tmp_path / "prompt.md").write_text("SYSTEM", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "done",
    )
    config = _skill_config(
        tmp_path, "tdd-dev", "code-review", "review-fix-loop",
    )
    pi_session.run_pi({"number": 4, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 4, "title": "t", "body": "b"}["number"], branch="orbi/owner-repo-issue-4", worktree=tmp_path, source_repo="owner/repo"), config)
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    implement_skills = _command_skills(calls[0])
    review_skills = _command_skills(calls[1])
    assert implement_skills != review_skills


def test_skill_name_of_skill_md_inside_skill_directory(tmp_path):
    path = tmp_path / "skills" / "tdd-dev" / "SKILL.md"
    path.parent.mkdir(parents=True)
    assert pi_command._skill_name(path) == "tdd-dev"


def test_skill_name_of_bare_markdown_entry(tmp_path):
    path = tmp_path / "my-skill.md"
    assert pi_command._skill_name(path) == "my-skill"


def test_run_review_keeps_non_delivery_skill_names(monkeypatch, tmp_path):
    """Only tdd-dev/review-fix-loop are excluded from the review; any
    other configured skill (including code-review) passes through.
    """
    (tmp_path / "prompt_review.md").write_text("REVIEW", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append(command) or "ok",
    )
    config = _skill_config(tmp_path, "code-review", "platform-qa")
    pi_session.run_review(RunContext(run_id=config.run_id, issue=4, branch="branch", worktree=tmp_path, source_repo="owner/repo"), {"number": 4, "url": "https://x/pull/4", "base_oid": "b1",
         "head_oid": "h1", "head_ref": "h"}, config, 1)
    skills = _command_skills(calls[0])
    assert any("code-review" in skill for skill in skills)
    assert any("platform-qa" in skill for skill in skills)


# --- main() slot acquisition (Issue #39) --------------------------------------


def test_main_capacity_full_does_not_pick_issue_or_call_pi(
    monkeypatch, tmp_path, caplog,
):
    """A full slot stops the runner before any claim or Pi invocation."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text(
        'source_repos = ["owner/repo"]\nmax_concurrency = 1\n',
        encoding="utf-8",
    )
    _write_prompts(tmp_path)
    # The single slot is already HELD (lock) by this live process: a
    # file on disk alone is not a held slot (flock is the token).
    slot_dir = tmp_path / ".orbi" / "slots"
    held = pilot_slots.acquire_slot(slot_dir, 1, os.getpid())
    assert held is not None

    def fail_if_called(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        raise AssertionError("pick_next_delivery must not run when capacity is full")

    monkeypatch.setattr(claim, "pick_next_delivery", fail_if_called)
    monkeypatch.setattr(runner, "process_issue", fail_if_called)
    # The guard itself must fail loudly if it is ever reached.
    with pytest.raises(AssertionError, match="must not run when capacity is full"):
        fail_if_called(["owner/repo"], Path("slots"), 1)
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(config)]) == 0
    assert "capacity_full" in caplog.text
    assert "max_concurrency=1" in caplog.text
    assert "slot_dir=" in caplog.text
    # The pre-existing holder keeps its slot (no claim, no label change,
    # no Pi).
    assert (
        pilot_slots.slot_occupancy(slot_dir, 1) == [(1, os.getpid())]
    )
    held.release()


def test_main_holds_slot_while_processing_issue(monkeypatch, tmp_path):
    """The slot is acquired before the pick and held for the whole task.

    Issue #1319: the claim lock is held for the SAME pick and is free
    again while the Pi session runs — the hold spans the scan -> identity
    write, never a Pi session.
    """
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    seen = {}
    slot_dir = tmp_path / ".orbi" / "slots"

    def fake_pick(repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs):
        seen["occupancy"] = pilot_slots.slot_occupancy(
            tmp_path / ".orbi" / "slots", 1,
        )
        seen["claim_window"] = pilot_slots.is_claim_lock_held(slot_dir)
        return ("owner/repo", issue, None)

    def fake_process(*args, **kwargs):
        # The first Pi session starts only after `mark_slot_delivery`.
        seen["pi_session"] = pilot_slots.is_claim_lock_held(slot_dir)
        return runner.IssueResult("pr", "https://x/y/pull/12")

    monkeypatch.setattr(claim, "pick_next_delivery", fake_pick)
    monkeypatch.setattr(runner, "process_issue", fake_process)
    monkeypatch.setattr(runner, "delivery_step", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert seen["occupancy"] == [(1, os.getpid())]
    assert seen["claim_window"] is True
    assert seen["pi_session"] is False


def test_main_reacquires_slot_after_previous_release(monkeypatch, tmp_path):
    """After the holder releases (process exit), the next run takes the slot."""
    from orbi import pilot_slots

    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: None,
    )

    assert runner.main(["--config", str(config)]) == 0
    slot_dir = tmp_path / ".orbi" / "slots"
    # The slot file exists and was released when main() returned ...
    assert (slot_dir / "slot-1").exists()
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    # ... so the next run takes the slot again.
    assert runner.main(["--config", str(config)]) == 0
    assert (slot_dir / "slot-1").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_terminal_failure_parts_render_action_before_reason_and_diagnosis():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Approve the PR manually.",
        reason="Branch protection requires one human approval.",
        diagnosis="rounds=3; mergeable=CONFLICTING",
        detail="ignored", next_step="", pr_url=None, number=39,
        source_repo="owner/repo", language_code="en",
    )
    assert body.index("Approve the PR manually.") < body.index(
        "Branch protection requires one human approval."
    )
    assert body.index("Branch protection requires one human approval.") < body.index(
        "<details><summary>Raw error</summary>"
    )
    assert "rounds=3; mergeable=CONFLICTING" in body


def test_terminal_failure_parts_omit_empty_action_and_show_disposition():
    body = progress._finish_outcome_body(
        outcome="blocked", action="", reason="No human action is required.",
        diagnosis="engine retry state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What you need to do:" not in body
    assert "waiting on a human decision" in body
    assert "engine retry state" in body


def test_terminal_failure_parts_render_missing_reason_explicitly():
    body = progress._finish_outcome_body(
        outcome="blocked", action="Inspect the PR.", reason="",
        diagnosis="raw state", detail="ignored", next_step="",
        pr_url=None, number=39, source_repo="owner/repo", language_code="en",
    )
    assert "What happened: No reason was provided." in body


def test_finish_progress_body_uses_fixed_sections_for_blocked_error(caplog):
    body = progress.finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None,
        pr_url="https://github.com/owner/repo/pull/46", review_round=0,
        priority="normal", detail="Command '['gh']' failed: raw stderr",
        next_step="", outcome="blocked", source_repo="owner/repo", language_code="en",
    )

    assert "What happened: Orbi could not complete the delivery" in body
    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next:" in body
    assert "https://github.com/owner/repo/pull/46" in body
    assert "<details><summary>Raw error</summary>" in body
    assert "Command '['gh']' failed: raw stderr" in body
    assert "next step: " not in body
    assert "progress_finish_missing_next_step" in caplog.text
    assert "call_point=_finish_progress_body" in caplog.text


def test_finish_progress_body_keeps_next_step_in_user_section():
    body = progress.finish_progress_body(
        number=39, title="Blocked task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="relabel the Issue ai-ready", outcome="blocked",
        source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: relabel the Issue ai-ready" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body


def test_finish_progress_body_defaults_orbi_action_when_fix_step_missing(caplog):
    body = progress.finish_progress_body(
        number=39, title="Fix task", run_id="a1b2c3d4",
        role=runner.ROLE_REVIEW, branch=None, worktree=None, pr_url=None,
        review_round=0, priority="normal", detail="failure detail",
        next_step="", outcome="fix needed", source_repo="owner/repo", language_code="en",
    )

    assert "What you need to do: Nothing" in body
    assert "What Orbi will do next: Orbi will wait for a human" in body
    assert "progress_finish_missing_next_step" in caplog.text


def test_main_releases_slot_after_opening_the_pr(monkeypatch, tmp_path):
    """Issue #788: a fresh claim ends its tick when the PR is open — the
    slot is released instead of being held through review -> merge. The
    review is the next tick's RESUME_REVIEW step, which takes the slot
    again for exactly the Pi session's duration. A concurrent runner can
    therefore take the slot the moment this tick ends."""
    from orbi import pilot_slots

    issue = {"number": 12, "title": "task", "body": "body"}
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    _write_prompts(tmp_path)
    monkeypatch.setattr(
        claim, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(runner, "process_issue", lambda *a, **k: runner.IssueResult("pr", PR_URL))
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a fresh claim must not run the review step"),
        ),
    )

    assert runner.main(["--config", str(config)]) == 0
    # The tick ended after opening the PR: the slot is free again.
    slot_dir = tmp_path / ".orbi" / "slots"
    assert pilot_slots.slot_occupancy(slot_dir, 1) == [(1, None)]
    assert pilot_slots.acquire_slot(slot_dir, 1, os.getpid()) is not None


# --- Pi child process environment (Issues #157, #1673) ---


def test_stream_pi_sets_pi_env_on_the_process(tmp_path):
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('PI_CODING_AGENT_DIR', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path, pi_env={"PI_CODING_AGENT_DIR": "/agent-dir"})
    assert result == "/agent-dir"


def test_stream_pi_without_pi_env_keeps_inherited_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', ''))\n"
    )
    result = pi_session.stream_pi([sys.executable, "-c", script], ctx=RunContext(run_id="deadbeef", issue=24, branch="b", worktree=tmp_path, source_repo="xqliu/orbi"), watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path)
    assert result == "inherited"


def test_stream_pi_strips_the_telegram_token_from_the_child_env(
    tmp_path, monkeypatch,
):
    """Issue #1673: the bot token stays in the Runner, never in Pi.

    Notifications are a bypass the child never needs, so the token is
    removed from the child environment while everything else stays
    inherited (the pre-#157 shape)."""
    monkeypatch.setenv("ORBI_TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("ORBI_TEST_ENV_MARKER", "inherited")
    session_dir = tmp_path / ".pi-session"
    session_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('ORBI_TEST_ENV_MARKER', '') + '|' "
        "+ os.environ.get('ORBI_TELEGRAM_BOT_TOKEN', 'ABSENT'))\n"
    )
    result = pi_session.stream_pi(
        [sys.executable, "-c", script],
        ctx=RunContext(run_id="deadbeef", issue=24, branch="b",
                       worktree=tmp_path, source_repo="xqliu/orbi"),
        watch=PiWatchOptions(poll_interval=0.1), cwd=tmp_path,
    )
    assert result == "inherited|ABSENT"


# ---------------------------------------------------------------------------
# Issue #48: log the active Issue context when the Runner is stopped
# (SIGTERM): `run_stopping` before the stop, `run_stopped` after the Pi
# child exits, `result=idle` when no Issue was claimed yet.
# ---------------------------------------------------------------------------

def _write_session_file(worktree: Path, session_id: str = "sess-1") -> None:
    """A session JSONL with a tool call so the snapshot shows phase=bash."""
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(exist_ok=True)
    records = [
        {"type": "session", "id": session_id,
         "timestamp": fresh_timestamp(), "cwd": str(worktree)},
        {"type": "message", "id": "a1", "timestamp": fresh_timestamp(),
         "message": {"role": "assistant", "content": [
             {"type": "toolCall", "id": "t1", "name": "bash",
              "arguments": {"command": "pytest tests/"}}]}},
    ]
    with open(session_dir / f"{session_id}.jsonl", "w",
              encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_stop_handler_idle_logs_run_stopped_idle_and_exits(
    monkeypatch, caplog, bind_run_id,
):
    # No run in flight (before the claim): the stop logs result=idle
    # WITHOUT any invented issue fields and exits 143.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id(None)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = [line for line in caplog.text.splitlines()
             if " run_stopped " in line]
    assert len(lines) == 1
    assert "result=idle" in lines[0]
    assert "issue=" not in lines[0]
    # No run was in flight: no run_stopping line either.
    assert "run_stopping" not in caplog.text


def test_stop_handler_active_run_logs_stopping_then_stopped_and_exits(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # Run in flight: run_stopping carries the full scene BEFORE the
    # stop, the live Pi child is TERMed and waited for, then
    # run_stopped result=interrupted, then the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree=str(worktree), source_repo="owner/repo"), "Log the stop scene")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    runner.set_active_pi(child)
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    assert 128 + died["signum"] == 143
    lines = caplog.text.splitlines()
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # run_stopping comes BEFORE run_stopped (the child exit is in
    # between).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # No session file yet: the snapshot fields are '-' (never invented).
    assert "phase=-" in line
    assert "session=-" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # The live Pi child was shut down: no orphan survives the stop.
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_stop_handler_active_run_uses_activity_snapshot_for_scene(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # With a session file the run_stopping line carries the phase and
    # session of the existing activity snapshot (reused, not re-parsed
    # ad hoc).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    line = [line for line in caplog.text.splitlines()
            if " run_stopping " in line][0]
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert 128 + died["signum"] == 143


def test_stop_handler_active_run_without_live_child_still_stops(
    monkeypatch, caplog, tmp_path, bind_run_id,
):
    # Run in flight but no live Pi child (between sessions): both lines
    # are still logged and the process exits 143.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id("a1b2c3d4")
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="b", worktree=str(worktree), source_repo="owner/repo"), "t")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    with caplog.at_level("INFO"):
        runner._handle_stop(signal.SIGTERM, None)
    lines = caplog.text.splitlines()
    assert any(" run_stopping " in line for line in lines)
    assert any(
        line.endswith("run_stopped issue=48 result=interrupted")
        for line in lines
    )
    assert 128 + died["signum"] == 143


def test_stop_handler_snapshot_failure_still_logs_and_exits(
    monkeypatch, caplog, bind_run_id,
):
    # A failing activity snapshot never swallows the stop: the scene
    # falls back to `-` for phase/session and the stop still exits.
    monkeypatch.setattr(journal, "_ACTIVE_RUN",
        {"issue": 48, "title": "t", "branch": "b", "worktree": "/w",
         "pi": None},
    )
    bind_run_id("a1b2c3d4")
    died = {}
    monkeypatch.setattr(
        runner, "_die_from_signal",
        lambda signum: died.setdefault("signum", signum),
    )
    def boom(_dir):
        raise RuntimeError("snapshot exploded")
    monkeypatch.setattr(runner, "activity_snapshot", boom)
    with caplog.at_level("INFO"):
        runner._stop_delivery(signal.SIGTERM)
    assert died["signum"] == signal.SIGTERM
    stopping = [
        line for line in caplog.text.splitlines()
        if " run_stopping " in line
    ]
    assert len(stopping) == 1
    assert "phase=-" in stopping[0]
    assert "session=-" in stopping[0]
    assert "snapshot exploded" in caplog.text


def test_stop_handler_reinstalls_default_disposition_and_raises_signal(
    monkeypatch, caplog, bind_run_id,
):
    # After the stop work the handler restores SIGTERM's default
    # disposition and re-raises the signal at itself: the process dies
    # from the ORIGINAL signal (systemd sees a signal-caused stop, exit
    # 143) and a handler crash can never swallow the stop.
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    bind_run_id(None)
    monkeypatch.setattr(runner.os, "_exit", lambda code: None)
    raised = {}
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: raised.setdefault("args", (pid, sig)),
    )
    runner._die_from_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert raised["args"] == (os.getpid(), signal.SIGTERM)


def test_set_active_run_binds_scene_and_pi_tracking(monkeypatch):
    monkeypatch.setattr(journal, "_ACTIVE_RUN", None)
    runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=7, branch="b", worktree="/w", source_repo="owner/repo"), "t")
    assert journal._ACTIVE_RUN == {
        "issue": 7, "title": "t", "branch": "b", "worktree": "/w",
        "pi": None,
    }
    child = object()
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN["pi"] is child
    runner.set_active_pi(None)
    assert journal._ACTIVE_RUN["pi"] is None
    runner.clear_active_run()
    assert journal._ACTIVE_RUN is None
    # set_active_pi without a bound run is a no-op (never crashes).
    runner.set_active_pi(child)
    assert journal._ACTIVE_RUN is None


def test_main_installs_the_stop_handler(monkeypatch, tmp_path):
    # main() installs the SIGTERM handler right after logging is set up,
    # so every phase of the tick (pre-claim, claim, implement, delivery
    # wait) stops with the scene.
    installed = {}
    monkeypatch.setattr(
        signal, "signal",
        lambda sig, handler: installed.setdefault(sig, handler),
    )
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nrepo_dir = "repo"\n'
        'workspace_root = ".."\nprompt = "prompt.md"\n'
        'prompt_review = "prompt_review.md"\n',
        encoding="utf-8",
    )
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("p", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        claim, "pick_next_delivery", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "acquire_slot", lambda *args: Mock(release=lambda: None))
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: "unchanged")
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
    assert runner.main(["--config", str(config_path)]) == 0
    assert installed[signal.SIGTERM] is runner._handle_stop


# Issue #48 acceptance: a REAL subprocess SIGTERM test — the Runner
# process is a real child of the test, stopped with a real SIGTERM, and
# the journal (captured from its stderr) proves the ordering and fields.
STOP_DRIVER = """
import logging
import signal
import subprocess
import sys
import time

sys.path.insert(0, {repo!r} + "/src")
import orbi.runner as runner
import orbi.milestone as milestone
from orbi.delivery_scene import RunContext

logging.basicConfig(level=logging.INFO, format=runner.log_format())
# Exactly what main() does (Issue #48): install the stop handler first.
signal.signal(signal.SIGTERM, runner._handle_stop)
runner.set_run_id("a1b2c3d4")
runner.set_active_run(RunContext(run_id="a1b2c3d4", issue=48, branch="orbi/owner-repo-issue-48", worktree={worktree!r}, source_repo="owner/repo"), "Log the stop scene")
# The real Pi-like child: a real long-running process (what stream_pi
# tracks via set_active_pi). It lingers briefly on SIGTERM before
# exiting (a slow shutdown), so the stop handler's child.wait() has a
# real wait to do.
child = subprocess.Popen(
    [sys.executable, "-c",
     "import os, signal, time\\n"
     "signal.signal(signal.SIGTERM, lambda s, f: "
     "(time.sleep(0.5), os._exit(0)))\\n"
     "time.sleep(60)\\n"],
)
runner.set_active_pi(child)
print("ready child=" + str(child.pid), flush=True)
# Sit in the poll loop (like stream_pi) until stopped.
time.sleep(120)
"""


def test_shutdown_child_kills_a_child_that_ignores_sigterm(tmp_path):
    """Root cause (Issue #48): the stop handler must not block past the
    grace when the Pi child ignores SIGTERM (e.g. a model/network call).
    It TERMs, waits at most `grace`, then KILLs and reaps the child so
    the Runner exits with the original signal before systemd's own
    stop deadline — never leaving `Result=timeout`/failed."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time, sys\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "print(\"ready\", flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    # Wait until the child has INSTALLED SIG_IGN: else a
    # terminate() before the handler is set would exit the child
    # on SIGTERM and never exercise the grace/kill path.
    assert child.stdout.readline().strip() == "ready"
    # The child ignores SIGTERM: a bare terminate()+wait() would
    # block forever. `_shutdown_child` with a short grace must
    # escalate to SIGKILL and reap it within that grace.
    runner._shutdown_child(child, grace=0.5)
    assert child.poll() is not None
    # Reaped with a kill signal, not a clean SIGTERM exit.
    assert child.returncode is not None and child.returncode < 0


def test_shutdown_child_noop_for_exited_or_none_child():
    """A None or already-exited child is a no-op (no hang, no signal)."""
    runner._shutdown_child(None)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    runner._shutdown_child(child)
    assert child.poll() is not None


def test_real_subprocess_sigterm_logs_stop_scene_and_shuts_down_pi(
    tmp_path,
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_session_file(worktree, session_id="sess-48")
    driver = tmp_path / "driver.py"
    driver.write_text(
        STOP_DRIVER.format(
            repo=str(Path(__file__).resolve().parent.parent),
            worktree=str(worktree),
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = proc.stdout.readline()
    assert ready.startswith("ready child=")
    child_pid = int(ready.strip().split("=")[1])
    # The Pi child is alive before the stop.
    time.sleep(0.2)
    assert _pid_alive(child_pid)
    # The real SIGTERM that systemd would send on stop.
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    # The process exits with 128+SIGTERM = 143 (the value systemd
    # records for a signal-caused stop).
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopping = [line for line in lines if " run_stopping " in line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopping) == 1
    assert len(stopped) == 1
    # Ordering: run_stopping BEFORE run_stopped (the Pi child exit is
    # in between), and both BEFORE the process termination (they are in
    # the captured stderr of the already-exited process).
    assert lines.index(stopping[0]) < lines.index(stopped[0])
    # The [run_id] prefix (the existing run correlation, reused); the
    # journal format is `LEVEL message`.
    assert stopping[0].startswith("INFO [a1b2c3d4] run_stopping ")
    assert stopped[0].startswith("INFO [a1b2c3d4] run_stopped ")
    line = stopping[0]
    assert "issue=48" in line
    assert 'title="Log the stop scene"' in line
    assert "signal=SIGTERM" in line
    # phase/session from the existing activity snapshot of the
    # worktree's .pi-session.
    assert "phase=bash" in line
    assert "session=sess-48" in line
    assert "branch=orbi/owner-repo-issue-48" in line
    assert f"worktree={worktree}" in line
    assert stopped[0].endswith("run_stopped issue=48 result=interrupted")
    # No orphan Pi: the stop handler waited for the child to exit
    # BEFORE the process exited (driver exit implies the child was
    # reaped), so the child must be gone now.
    assert not _pid_alive(child_pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user: alive.
        return True
    return True


def test_pid_alive_reports_zombie_and_foreign_pid(monkeypatch):
    # A killed-but-unreaped child is a zombie: os.kill(pid, 0) succeeds
    # on it, so it still counts as alive (the stop handler's
    # child.poll() check is what distinguishes a zombie).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    child.kill()
    time.sleep(0.2)
    assert _pid_alive(child.pid) is True
    child.wait(timeout=5)
    # A pid that no longer exists (reaped).
    assert _pid_alive(child.pid) is False
    # A foreign pid (owned by another user) reports alive via
    # PermissionError — exercise the branch by stubbing os.kill.
    monkeypatch.setattr(
        runner.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError()),
    )
    assert _pid_alive(1) is True


def test_real_subprocess_sigterm_before_claim_logs_idle(tmp_path):
    # Stopped before claiming an Issue: run_stopped result=idle, no
    # invented Issue fields.
    driver = tmp_path / "driver_idle.py"
    driver.write_text(
        "import logging, signal, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r})\n"
        "import orbi.runner as runner\n"
        "logging.basicConfig(level=logging.INFO, format=runner.log_format())\n"
        "signal.signal(signal.SIGTERM, runner._handle_stop)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGTERM)
    stderr = proc.stderr.read()
    assert proc.wait(timeout=10) == 143
    lines = [line for line in stderr.splitlines() if line]
    stopped = [line for line in lines if " run_stopped " in line]
    assert len(stopped) == 1
    assert stopped[0] == "INFO run_stopped result=idle"
    assert "issue=" not in stderr
    assert "run_stopping" not in stderr


# --- Issue #98: ticket kinds — content-only, ops, release dispatch --------


def test_process_issue_routes_release_to_process_release(monkeypatch):
    issue = {"number": 99, "title": "Release v0.3.0", "body": "",
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    calls = []
    # Issue #708: the release dispatch now direct-reads the live label
    # state before dispatching (`gh issue view`) — the fresh ticket here
    # answers with no labels, and no slot config means no yield path.
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps({"labels": []}),
    )
    monkeypatch.setattr(release_run, "process_release",
                        lambda i, c, r: calls.append("release") or "rel-url")
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("run_pi must not run for a release task")))
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc")
    result = runner.process_issue(issue, config_domain.RunnerConfig(base_branch="main"), "o/r")
    assert result == runner.IssueResult("release", "rel-url")
    assert calls == ["release"]


def test_is_content_only_requires_the_explicit_label():
    """Issue #537: the content（纯内容）path dispatches on
    `ai-content-only`; the old `ai-ops-only` name now means ops."""
    assert runner.is_content_only(
        {"labels": [{"name": "ai-content-only"}]},
    ) is True
    assert runner.is_content_only(
        {"labels": [{"name": "ai-ops-only"}]},
    ) is False
    assert runner.is_content_only({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_content_only({"labels": "ai-content-only"}) is False


def test_is_ops_requires_the_explicit_label():
    """Issue #537: `ai-ops-only` is the full-execution ops marker — it
    must NOT dispatch the content agent anymore."""
    assert runner.is_ops({"labels": [{"name": "ai-ops-only"}]}) is True
    assert runner.is_ops({"labels": [{"name": "ai-content-only"}]}) is False
    assert runner.is_ops({"labels": [{"name": "marketing"}]}) is False
    assert runner.is_ops({"labels": "ai-ops-only"}) is False


def test_run_ticket_agent_uses_a_temporary_session_without_git(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        # The prompt FILE lives in the session's TemporaryDirectory
        # (Issue #1634), so it is read while the session is running.
        lambda command, **kwargs: calls.append(
            (command, kwargs, rendered_prompt(command))) or "copy",
    )
    result = pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider=None, pi_model=None, pi_thinking=None),
        "o/r",
    )
    assert result == "copy"
    command, kwargs, prompt = calls[0]
    assert command[0] == "pi"
    # `pi --no-tools` is the enforcement boundary: a prompt instruction
    # alone cannot ensure the Agent never invokes git/gh or writes files.
    assert "--no-tools" in command
    assert "git/gh tools" in prompt
    assert kwargs["cwd"] != tmp_path
    assert kwargs["cwd"].name.startswith("orbi-ticket-")
    assert kwargs["ctx"].branch == "-"
    assert kwargs["role"] == runner.ROLE_TICKET
    assert str(tmp_path) not in command[command.index("--session-dir") + 1]
    assert Path(command[command.index("--session-dir") + 1]).parent == kwargs["cwd"]


def test_run_ticket_agent_materializes_provider_dir_and_env(
    monkeypatch, tmp_path,
):
    """Issue #1379: the ONE no-tools helper keeps reaching the configured
    provider — the materialization the clarify gate used to own still
    puts the per-run agent dir on `PI_CODING_AGENT_DIR`.

    The judgment session runs in a repo that configures a provider file
    (the ticket-only content session does too), and the selected model
    rides on `--provider/--model`: without the materialized catalog Pi
    could never resolve it, and the gate would silently fail open.
    """
    home = tmp_path / "home"
    _user_agent_dir(home, auth=False, settings=False)
    monkeypatch.setenv("HOME", str(home))
    seen: dict = {}

    def fake_stream(command, **kwargs):
        agent_dir = Path(kwargs["pi_env"]["PI_CODING_AGENT_DIR"])
        seen["kwargs"] = kwargs
        seen["models_written"] = (agent_dir / "models.json").is_file()
        return "copy"

    # The subprocess seam fans out to every binding (Issue #785), and
    # the patch ratchet forbids a new per-module `pi_session` patch
    # (Issue #789): the corpus may only shrink.
    monkeypatch.setattr(seam, "stream_pi", fake_stream)
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, run_id="a1b2c3d4", skills=(),
        pi_provider="groq", pi_model="qwen/qwen3.8-27b",
        pi_providers_data=GROQ_PROVIDERS,
    )
    assert pi_session.run_ticket_agent(
        {"number": 99, "title": "Launch thread", "body": "Write copy"},
        config, "o/r",
    ) == "copy"
    kwargs = seen["kwargs"]
    agent_dir = kwargs["cwd"] / ".orbi" / "pi-agent"
    # The per-run catalog reaches Pi through the env only...
    assert kwargs["pi_env"] == {"PI_CODING_AGENT_DIR": str(agent_dir)}
    assert seen["models_written"] is True
    # ...never through the journaled command.
    log = " ".join(kwargs["log_command"])
    assert str(agent_dir) not in log
    assert "https://api.groq.com/openai/v1" not in log
    # Transient OS state: the materialized dir goes with the session.
    assert not agent_dir.exists()


def test_run_ticket_agent_logs_provider_config_loaded(monkeypatch, tmp_path, caplog):
    """Issue #176: the ticket-only session logs the provider config
    line too (role=ticket) — every Pi session has the same startup
    sequence."""
    monkeypatch.setattr(pi_session, "stream_pi", lambda command, **kwargs: "copy")
    with caplog.at_level("INFO"):
        pi_session.run_ticket_agent(
            {"number": 99, "title": "Launch thread", "body": "Write copy"},
            config_domain.RunnerConfig(repo_dir=tmp_path, run_id="a1b2c3d4", skills=(), pi_provider="local-qwen", pi_model="qwen3.8:27b", pi_thinking=None),
            "o/r",
        )
    lines = [line for line in caplog.text.splitlines()
             if " provider_config_loaded " in line]
    assert len(lines) == 1
    assert "role=ticket" in lines[0]
    assert "issue=o/r#99" in lines[0]
    assert "provider=local-qwen" in lines[0]
    assert "model=qwen3.8:27b" in lines[0]


def test_progress_state_passes_startup_sub_phase_to_comment(tmp_path):
    """Issue #176: the progress comment's `phase` carries the watcher's
    startup sub-phase (session_pending / request_pending) instead of
    the generic `starting` while the first response is outstanding."""
    for sub_phase in ("session_pending", "request_pending"):
        state = runner.progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": sub_phase, "last_activity": None,
                      "action": None, "session_id": None}, language_code="en")
        assert state["phase"] == sub_phase
    # The tool-based phase passes through unchanged once the first
    # response arrived.
    state = runner.progress_state(RunContext(run_id="a1b2c3d4", issue=176, branch="b", worktree=tmp_path, source_repo="owner/repo"), title="t", role="implement", started=time.monotonic(), pr_url=None, review_round=0, priority="normal", activity={"phase": "test", "last_activity": None,
                  "action": "bash pytest tests/", "session_id": "s1"}, language_code="en")
    assert state["phase"] == "test"


# Issue #1826: the operator's one optional project status page.
TASK_STATUS_URL = "https://tasks.example.org/project-status?repo=example%2Fwidget"


def test_process_ticket_only_posts_agent_output_without_git_delivery(monkeypatch):
    """A labeled content task is delivered in its Issue, never through Git."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-ready"}, {"name": "ai-content-only"}]}
    edits = []
    comments = []
    commands = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    monkeypatch.setattr(seam, "run_command",
                        lambda command, **kwargs: commands.append(command) or "")

    result = runner.process_issue(
        issue,
        config_domain.RunnerConfig(
            repo_dir=Path("/repo"), status_url=TASK_STATUS_URL,
        ),
        "o/r",
    )

    assert result == runner.IssueResult("ticket-only", None)
    assert edits == [
        (99, {"repo": "o/r", "add": "ai-in-progress"}),
        (99, {"repo": "o/r", "remove": "ai-in-progress"}),
    ]
    assert comments and "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03\u7684\u5e16\u5b50" in comments[0][1]["body"]
    # Issue #1826: the delivery record carries the readable summary with
    # the operator's fixed status link, before the agent's output.
    delivered = comments[0][1]["body"]
    assert "**#99 Launch thread**" in delivered
    assert (
        f"**Project status:** [Open project status]({TASK_STATUS_URL})"
        in delivered
    )
    assert delivered.index("**Project status:**") < delivered.index(
        "\u53ef\u4ee5\u76f4\u63a5\u53d1\u5e03"
    )
    assert "<!-- orbi:run=a1b2c3d4 -->" in comments[0][1]["body"]
    assert "run_id=a1b2c3d4" in comments[0][1]["body"]
    assert commands == [["gh", "issue", "close", "99", "--repo", "o/r"]]


def test_process_ticket_only_rejects_empty_agent_content(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(pi_session, "run_ticket_agent", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="returned no content"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")
    assert edits[-2][1]["add"] == "ai-blocked"
    # ai-blocked ALONE: the queue label is cleared too (Issue #1775).
    assert edits[-1] == (99, {"repo": "o/r", "remove": "ai-ready"})


@pytest.mark.parametrize("language_code", ["en", "zh"])
@pytest.mark.parametrize(
    ("dispatch_label", "queue_label"),
    [(None, "ai-ready"), ("ship", "ship")],
    ids=["default_queue_label", "custom_dispatch_label"],
)
def test_process_ticket_only_failure_marks_blocked_without_git_delivery(
        monkeypatch, language_code, dispatch_label, queue_label):
    """A failed ticket-only run blocks with the community help line from the
    ONE blocked exit, in the run's bound language (Issue #1730).

    Issue #1775: the blocked terminal is `ai-blocked` ALONE — it clears
    the queue label the claim scanned, the repository's own
    `dispatch_label` when it declares one."""
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    edits = []
    comments = []
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue",
                        lambda number, **kwargs: edits.append((number, kwargs)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, **kwargs: comments.append((number, kwargs)))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(
            issue, config_domain.RunnerConfig(
                repo_dir=Path("/repo"), message_language=language_code,
                dispatch_label=dispatch_label, status_url=TASK_STATUS_URL,
            ), "o/r",
        )
    assert edits[-2] == (99, {"repo": "o/r", "add": "ai-blocked",
                               "remove": "ai-in-progress"})
    # The terminal state is ai-blocked ALONE: the queue label the claim
    # kept is cleared too (Issue #1775).
    assert edits[-1] == (99, {"repo": "o/r", "remove": queue_label})
    body = comments[-1][1]["body"]
    assert language.text(
        language_code, "runner.ticket_only_failed",
        detail="Pi failed",
        run_info="run_id=a1b2c3d4 priority=normal task_type=ticket-only",
        run_id="a1b2c3d4",
    ) in body
    assert "run_id=a1b2c3d4" in body
    # Issue #1826: the blocked task message carries the readable summary
    # (identity + the operator's fixed link) above the one help line.
    assert "**#99 Launch thread**" in body
    link = language.text(
        language_code, "summary.project_status", url=TASK_STATUS_URL,
    )
    assert link in body
    help_line = language.text(language_code, "help.community")
    assert body.endswith(f"\n\n{help_line}")
    assert body.count(help_line) == 1


def test_process_ticket_only_keeps_original_error_when_failure_reporting_fails(monkeypatch):
    issue = {"number": 99, "title": "Launch thread", "body": "Write copy",
             "labels": [{"name": "ai-content-only"}]}
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(seam, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("comment failed")))
    monkeypatch.setattr(pi_session, "run_ticket_agent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Pi failed")))
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "safe_publish", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="Pi failed"):
        runner.process_ticket_only(issue, config_domain.RunnerConfig(repo_dir=Path("/repo")), "o/r")


def test_process_issue_keeps_pr_ready_label_when_scene_comment_5xx_exhausts(
    monkeypatch, tmp_path, caplog,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "stable_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
                        lambda *args, **kwargs: edits.append((args, kwargs)))
    monkeypatch.setattr(seam, "set_active_run", lambda *args, **k: None)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    def comment(number, *, repo, body):
        if "Orbi opened PR:" in body:
            raise subprocess.CalledProcessError(
                1, ["gh", "issue", "comment"],
                stderr="GraphQL: Something went wrong while executing your query",
            )
    monkeypatch.setattr(seam, "comment_issue", comment)
    monkeypatch.setattr(seam, "ProgressPublisher", Mock())
    monkeypatch.setattr(seam, "run_pi", lambda *a, **k: "done")
    monkeypatch.setattr(seam, "deliver_pr", lambda *a, **k: "https://github.com/o/r/pull/1")
    def run(command, **kwargs):
        if command[0:3] == ["gh", "pr", "list"]:
            return "[]"
        return "abc123"
    monkeypatch.setattr(seam, "run_command", run)
    monkeypatch.setattr(seam, "delivery_step", Mock())
    monkeypatch.setattr(seam, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(seam, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(seam, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(seam, "_finish_progress", Mock())
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    with caplog.at_level("WARNING"):
        result = runner.process_issue(
            issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
        )
    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/1")
    assert any(kwargs.get("add") == runner.PR_OPENED_LABEL for _, kwargs in edits)
    assert "progress_comment_failed" in caplog.text


def test_process_issue_keeps_normal_flow_without_release_label(
    monkeypatch, tmp_path,
):
    issue = {"number": 99, "title": "Normal", "body": "",
             "labels": [{"name": "ai-ready"}]}
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    # The worktree exists: `create_worktree` always returns a real
    # directory (the run state file is written into it, Issue #219).
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **kwargs: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(pi_session, "run_pi",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(runner, "deliver_pr",
                        lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(seam, "run_command", lambda c, **k: "")
    monkeypatch.setattr(runner, "delivery_step", Mock())
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "LOGGER", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")
    monkeypatch.setattr(seam, "issue_context", lambda r, n: "#n")
    monkeypatch.setattr(runner, "format_run_scene", lambda *a, **k: "scene")
    monkeypatch.setattr(runner, "_finish_progress", Mock())
    runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path), "o/r",
    )


def _ops_issue_mocks(monkeypatch, tmp_path, *, head_sha: str, dirty: str):
    """Shared mock set for the `process_issue` ops-dispatch tests.

    `head_sha` answers `git rev-parse HEAD` (the frozen base means the
    session delivered no commit); `dirty` answers `git status
    --porcelain`. Every `gh`/`git`/pi collaborator is captured.
    """
    issue = {
        "number": 99, "title": "Merge the beta PR and verify deploy",
        "body": "Merge PR #7 into main, then verify the deployment.",
        "labels": [{"name": "ai-ready"}, {"name": "ai-ops-only"}],
    }
    commands: list[list[str]] = []
    configs: list[dict] = []
    monkeypatch.setattr(claim, "is_release", lambda i: False)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id", lambda rid: None)
    monkeypatch.setattr(seam, "has_in_progress_label", lambda n, r: False)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue", Mock())
    monkeypatch.setattr(seam, "set_active_run", Mock())
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(seam, "create_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr(seam, "comment_issue", Mock())
    monkeypatch.setattr(runner, "ProgressPublisher", Mock())
    monkeypatch.setattr(failure_report, "activity_snapshot", lambda p: None)
    monkeypatch.setattr(seam, "safe_publish", lambda **k: None)
    monkeypatch.setattr(
        pi_session, "run_pi",
        lambda issue_arg, ctx, config, **k: configs.append(config),
    )
    monkeypatch.setattr(runner.runner_health, "record_run_attempt", Mock())
    monkeypatch.setattr(runner.runner_health, "record_pickup", Mock())

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["git", "rev-parse"]:
            # Issue #1581: the tree probes get their own ref back, so
            # the delivered head tree differs from the frozen base tree.
            return command[2] if command[2].endswith("^{tree}") else head_sha
        if command[:2] == ["git", "status"]:
            return dirty
        return ""

    monkeypatch.setattr(seam, "run_command", fake_run)
    return issue, commands, worktree, configs


def test_process_issue_ops_without_commit_closes_with_evidence(
    monkeypatch, tmp_path,
):
    """Issue #537: a pure-ops session (real shell/gh work, evidence
    comments, no commit) is COMPLETE — the Runner closes the Issue,
    removes the claim label, and never enters the PR ceremony."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    ops_prompt = prompts / "prompt_ops.md"
    ops_prompt.write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pure ops delivery must not open a PR")
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)
    # The full-execution session ran with the OPS playbook, not the dev
    # one — and with the same worktree machinery (no --no-tools content
    # agent).
    assert len(configs) == 1
    assert configs[0].prompt == ops_prompt
    # Terminal transition mirrors the content path: close + claim label
    # removed. No PR, no push.
    assert ["gh", "issue", "close", "99", "--repo", "o/r"] in commands
    # The claim added the label at pickup; the delivery removes it.
    runner.edit_issue.assert_any_call(
        99, repo="o/r", remove=runner.IN_PROGRESS_LABEL,
    )


def test_process_issue_ops_health_record_failure_is_a_bypass(
    monkeypatch, tmp_path,
):
    """Issue #537/#266: the health state write is pure bypass — a state
    failure never turns a delivered pure-ops run into a failed one."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    monkeypatch.setattr(
        runner.runner_health, "record_run_attempt",
        Mock(side_effect=OSError("state file unwritable")),
    )
    monkeypatch.setattr(runner, "deliver_pr", Mock())
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("ops", None)


def test_process_issue_ops_with_commit_takes_the_pr_ceremony(
    monkeypatch, tmp_path,
):
    """Issue #537: an ops session that commits code (e.g. a
    wrangler.toml change) is delivered through the SAME deterministic
    closeout as a dev ticket — push + PR, `ai-pr-opened`."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="def456", dirty="",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")
    deliver_calls: list[tuple] = []
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *a, **k: deliver_calls.append(a) or (
            "https://github.com/o/r/pull/7"
        ),
    )
    monkeypatch.setattr(runner, "format_end_scene", lambda **k: "end")

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    assert result == runner.IssueResult("pr", "https://github.com/o/r/pull/7")
    assert len(deliver_calls) == 1
    assert any(c[:2] == ["git", "rev-parse"] for c in commands)


def test_process_issue_ops_with_uncommitted_leftovers_fails_fast(
    monkeypatch, tmp_path,
):
    """Issue #537: no commit but uncommitted leftovers is neither a pure
    ops delivery nor a code delivery — the deterministic closeout's
    fail-fast (never commit uncommitted changes) applies."""
    issue, commands, worktree, configs = _ops_issue_mocks(
        monkeypatch, tmp_path, head_sha="abc123", dirty=" M a.txt\n",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("dev", encoding="utf-8")
    (prompts / "prompt_ops.md").write_text("ops playbook", encoding="utf-8")

    def failing_deliver_pr(*a, **k):
        raise runner.RecoverablePiFailure(
            "the agent left uncommitted changes in the worktree"
        )

    cleanup = Mock()
    original_cleanup = runner.cleanup_task_worktree
    runner.cleanup_task_worktree = cleanup
    monkeypatch.setattr(runner, "deliver_pr", failing_deliver_pr)

    result = runner.process_issue(
        issue, config_domain.RunnerConfig(base_branch="main", repo_dir=tmp_path, prompt=prompts / "prompt.md"),
        "o/r",
    )

    # Recoverable delivery failures preserve the scene for the next
    # same-run implement session instead of entering ai-blocked.
    assert result == runner.IssueResult("failed", None)
    cleanup.assert_not_called()
    runner.cleanup_task_worktree = original_cleanup
    runner.edit_issue.assert_called_once_with(
        99, repo="o/r", add=runner.IN_PROGRESS_LABEL,
    )


def test_bot_git_identity_overrides_git_config(tmp_path):
    """The startup identity owns a commit even when `-c user.*` is given.

    Git's `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables take precedence over
    `user.*` config, including `-c user.*`, so a Pi session can no longer
    credit its commits to a stranger's account (Issue #1416).
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    runner.set_bot_git_identity(env)
    work = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=x",
         "-c", "user.email=x@example.com", "commit", "--allow-empty",
         "-m", "t"],
        check=True, capture_output=True, env=env,
    )
    identity = subprocess.run(
        ["git", "-C", str(work), "log", "-1",
         "--format=%an <%ae>|%cn <%ce>"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert identity == (
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>|"
        "orbi-build[bot] <327503608+orbi-build[bot]@users.noreply.github.com>"
    )


def test_runner_main_pins_bot_git_identity(monkeypatch, tmp_path):
    """A run pins Orbi's identity before any git call or Pi session."""
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    missing = tmp_path / "missing.toml"
    assert runner.main(["--config", str(missing)]) == 1
    assert os.environ["GIT_AUTHOR_EMAIL"] == (
        "327503608+orbi-build[bot]@users.noreply.github.com"
    )
    assert os.environ["GIT_COMMITTER_NAME"] == "orbi-build[bot]"


def test_reconcile_open_epics_closes_and_deduplicates_audit(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return json.dumps([epic]) if command[:3] == ["gh", "issue", "list"] else ""
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete", lambda repo, item: ["Issue #21 closed"])
    comments = []
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue", lambda number, *, repo, body: comments.append({"body": body}))
    result = claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert result == ["Epic #20 closed after verification (Issue #21 closed)"]
    assert len(comments) == 1 and "run_id=abc12345" in comments[0]["body"]
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands
    # The same audit is idempotent on the next tick.
    commands.clear()
    claim.reconcile_open_epics("o/r", "def67890", "en")
    assert len(comments) == 1
    assert "epic_closed issue=20 repo=o/r" in caplog.text


def test_reconcile_open_epics_posts_the_audit_in_the_bound_language(monkeypatch):
    epic = {"number": 20}

    def fake_run(command, **kwargs):
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    comments = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    assert len(comments) == 1
    assert "Epic 对账：已完成" in comments[0]["body"]
    assert "Issue #21 closed" in comments[0]["body"]
    assert "run_id=abc12345" in comments[0]["body"]


def test_reconcile_open_epics_keeps_one_audit_across_languages(monkeypatch):
    """A close that failed after today's English audit landed posts no
    second comment when the next sweep renders Chinese; two Chinese
    sweeps after a failed close post one comment in total."""
    epic = {"number": 20}
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["issue", "list"]:
            return json.dumps([epic])
        return ""

    english = ("Epic reconciliation: complete; children: Issue #21 closed; "
               "no open native blockers/dependencies.")
    comments = [{"body": f"<!-- orbi:run=old -->\n{english}\nrun_id=old"}]
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "verify_epic_complete",
                        lambda repo, item: ["Issue #21 closed"])
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: comments)
    monkeypatch.setattr(seam, "comment_issue",
                        lambda number, *, repo, body: comments.append({"body": body}))
    claim.reconcile_open_epics("o/r", "abc12345", "zh")
    claim.reconcile_open_epics("o/r", "def67890", "zh")
    assert len(comments) == 1
    assert ["gh", "issue", "close", "20", "--repo", "o/r"] in commands


def test_reconcile_open_epics_keeps_incomplete_without_comment(monkeypatch, caplog):
    caplog.set_level("INFO")
    epic = {"number": 20}
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: json.dumps([epic]))
    monkeypatch.setattr(seam, "verify_epic_complete", lambda repo, item: (_ for _ in ()).throw(ValueError("open blockers: #3")))
    comments = []
    monkeypatch.setattr(seam, "comment_issue", lambda *args, **kwargs: comments.append(True))
    claim.reconcile_open_epics("o/r", "abc12345", "en")
    assert not comments
    assert "epic_kept_open issue=20 repo=o/r reason=\"open blockers: #3\"" in caplog.text


def test_milestone_reconcile_classified_failures_are_throttled_across_ticks(
    tmp_path, caplog,
):
    caplog.set_level("INFO")
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="Bad credentials (HTTP 401)",
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=1000)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)

    # A later systemd tick is a new Python process, so the filesystem marker —
    # not process memory — must prevent both another request and another log.
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r", now=1100)
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    messages = [record.message for record in caplog.records]
    assert sum(message.endswith(
        'milestone_reconcile_auth_failed repo=o/r status=401 '
        'operation=list_milestones stderr="Bad credentials (HTTP 401)"'
    ) for message in messages) == 1
    assert "milestone_reconcile_failed" not in caplog.text


def test_milestone_reconcile_success_clears_suppression(tmp_path, caplog):
    error = github.MilestoneReconcileError(
        status=404, operation="list_milestones", stderr="Not Found (HTTP 404)",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert not milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    assert sum(record.message.endswith(
        'milestone_reconcile_not_found repo=o/r status=404 '
        'operation=list_milestones stderr="Not Found (HTTP 404)"'
    ) for record in caplog.records) == 2


def test_milestone_reconcile_failure_is_retried_after_cooldown(tmp_path):
    error = github.MilestoneReconcileError(
        status=401, operation="list_milestones", stderr="HTTP 401",
    )
    milestone.record_milestone_reconcile_failure(tmp_path, "o/r", error)
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    os.utime(marker, (1000, 1000))
    assert not milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS - 1,
    )
    assert milestone.milestone_reconcile_due(
        tmp_path, "o/r", now=1000 + milestone.MILESTONE_RECONCILE_RETRY_SECONDS,
    )


def test_milestone_reconcile_state_io_failures_stay_fail_open(
    tmp_path, monkeypatch, caplog,
):
    marker = milestone._milestone_failure_marker(tmp_path, "o/r", 401)
    marker.parent.mkdir(parents=True)
    marker.touch()
    original_stat = Path.stat
    monkeypatch.setattr(
        Path, "stat",
        lambda self: (_ for _ in ()).throw(OSError("stat failed"))
        if self == marker else original_stat(self),
    )
    assert milestone.milestone_reconcile_due(tmp_path, "o/r")
    monkeypatch.setattr(Path, "stat", original_stat)

    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(OSError("unlink failed"))
        if self == marker else original_unlink(self),
    )
    os.utime(marker, (0, 0))
    assert milestone.milestone_reconcile_due(tmp_path, "o/r", now=4000)
    milestone.clear_milestone_reconcile_failure(tmp_path, "o/r")
    assert "milestone_reconcile_suppression_clear_failed repo=o/r" in caplog.text
    monkeypatch.setattr(Path, "unlink", original_unlink)

    marker.unlink()
    monkeypatch.setattr(
        os, "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )
    milestone.record_milestone_reconcile_failure(
        tmp_path, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    assert "milestone_reconcile_auth_failed repo=o/r status=401" in caplog.text


def test_reconcile_orphan_prs_reports_an_open_pr_of_a_closed_issue(monkeypatch, caplog):
    """Issue #746 (direction B): a delivery PR whose source Issue a human
    closed is reported ONCE on the PR — every delivery-state scan reads
    `state=open` Issues only, so nothing else would ever look at that PR
    again. The merge/close decision stays with the human: the Runner
    never auto-merges and never auto-closes across a human's close.

    Issue #1788 acceptance 2: `orbi/x-y-issue-3` is ANOTHER repository's
    delivery name, so the sweep never reads Issue #3 for it and never
    reports it — the fake raises on that read, so the old regex fails here
    even though this repository's #3 is closed."""
    caplog.set_level("INFO")
    pr_comments: list[dict] = []
    posted = []

    def fake_run(command, **kwargs):
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([
                {"number": 9, "headRefName": "orbi/o-r-issue-4"},
                {"number": 30, "headRefName": "orbi/x-y-issue-3"},
            ])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: pr_comments)
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda number, *, repo, body: posted.append((number, repo, body)),
    )
    result = claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    assert result == ["PR #9 reported: source Issue #4 is closed"]
    assert len(posted) == 1
    number, repo, body = posted[0]
    assert (number, repo) == (9, "o/r")
    assert "<!-- orbi:orphan-pr -->" in body
    assert "<!-- orbi:run=abc12345 -->" in body
    assert "#4" in body
    assert "orphan_pr_reported pr=9 issue=4 repo=o/r" in caplog.text
    # Issue #1826: the same optional fixed project status link rides on
    # this task message; omitting it (above call) stays GitHub-only.
    pr_comments.clear()
    claim.reconcile_orphan_prs(
        "o/r", "abc12345", hooks=resume_deps(), language_code="en",
        status_url="https://tasks.example.org/status?repo=o%2Fr",
    )
    assert len(posted) == 2
    assert (
        "**Project status:** [Open project status]"
        "(https://tasks.example.org/status?repo=o%2Fr)"
    ) in posted[1][2]
    posted.pop()
    pr_comments.clear()
    assert "pr=30" not in caplog.text
    # Idempotent: the marker on the PR means the next tick reports nothing.
    pr_comments.append({"body": posted[0][2]})
    assert claim.reconcile_orphan_prs("o/r", "def67890", hooks=resume_deps(), language_code="en") == []
    assert len(posted) == 1
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_reconcile_orphan_prs_rejects_malformed_pr_list_and_state(monkeypatch):
    """A non-array `pr list` answer and a malformed Issue state fail fast
    inside the sweep — the per-tick try/except turns that into the
    `orphan_pr_reconcile_failed` bypass log, never a broken delivery."""
    def pr_list_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps({"comments": []})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", pr_list_run)
    with pytest.raises(ValueError, match="pr list must return a JSON array"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        pr_list_run(["unexpected"])

    def weird_state_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command[-1] == "state":
            return json.dumps({"state": "WEIRD"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", weird_state_run)
    with pytest.raises(ValueError, match="state must be OPEN or CLOSED"):
        claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en")
    with pytest.raises(AssertionError):
        weird_state_run(["unexpected"])


def test_reconcile_orphan_prs_skips_open_issues_and_non_delivery_branches(monkeypatch):
    """An open source Issue is the normal delivery flow (no report); a
    head branch outside THIS repository's delivery naming (a human or
    external PR, another repository's `orbi/x-y-issue-3`) is never the
    Runner's business — no issue state is even read."""
    read_issues = []

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                "garbage",
                {"number": 9, "headRefName": "orbi/o-r-issue-4"},
                {"number": 10, "headRefName": "feature/human-branch"},
                {"number": 11, "headRefName": None},
                {"number": 12, "headRefName": "orbi/manual"},
            ])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r", "--json", "state"]:
            read_issues.append(4)
            return json.dumps({"state": "OPEN"})
        raise AssertionError(command)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(failure_report, "comment_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no comment")))
    assert claim.reconcile_orphan_prs("o/r", "abc12345", hooks=resume_deps(), language_code="en") == []
    assert read_issues == [4]
    with pytest.raises(AssertionError):
        fake_run(["unexpected"])


def test_the_orphan_branch_regex_is_gone():
    """Issue #1788 acceptance 3: the sweep no longer parses the Issue
    number with its own pattern — it reads the one delivery naming."""
    source = Path(claim.__file__).read_text(encoding="utf-8")
    assert "_ORPHAN_PR_BRANCH_RE" not in source
    assert "orbi/.+-issue-" not in source


def test_reconcile_orphan_prs_failure_is_fail_open(
    tmp_path, monkeypatch, caplog, bind_run_id,
):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a throttled milestone sweep must be skipped")
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    bind_run_id("abc12345")
    slot_dir = tmp_path / "slots"
    milestone.record_milestone_reconcile_failure(
        slot_dir.parent, "o/r", github.MilestoneReconcileError(
            status=401, operation="list_milestones", stderr="HTTP 401",
        ),
    )
    result = claim.pick_next_delivery(["o/r"], slot_dir, 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "orphan_pr_reconcile_failed repo=o/r" in caplog.text


@pytest.mark.parametrize("policy_text,expected,unexpected", [
    ('message_language = "en"\n', "Orbi orphan PR:", "Orbi 孤儿 PR"),
    (None, "Orbi 孤儿 PR：", "Orbi orphan PR:"),
])
def test_claim_time_comment_follows_the_repository_language(
        monkeypatch, tmp_path, policy_text, expected, unexpected, bind_run_id,
):
    """Issue #1687 acceptance 2: the claim-time comments use the
    repository's merged language, not the host's. A zh host whose
    repository sets `message_language = "en"` posts English comments;
    with no repository file it keeps the host's Chinese."""
    import base64

    def fake_run(command, **kwargs):
        if command == ["gh", "api", "repos/o/r/contents/.github/orbi.toml"]:
            if policy_text is None:
                return json.dumps({})
            return json.dumps({
                "sha": "a" * 40,
                "content": base64.b64encode(
                    policy_text.encode("utf-8"),
                ).decode("ascii"),
            })
        if command == ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
                       "--json", "number,headRefName", "--limit", "200"]:
            return json.dumps([{"number": 9, "headRefName": "orbi/o-r-issue-4"}])
        if command == ["gh", "issue", "view", "4", "--repo", "o/r",
                       "--json", "state"]:
            return json.dumps({"state": "CLOSED"})
        raise AssertionError(f"unexpected command: {command}")

    posted: list[str] = []
    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "pr_comments", lambda number, *, repo: [])
    monkeypatch.setattr(
        seam, "comment_pr",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setitem(
        claim.__dict__, "reconcile_open_epics", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.milestone_bookkeeping.__dict__, "milestone_reconcile_due",
        lambda *a, **k: False,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_resumable_delivery", lambda *a, **k: None,
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_in_progress_issue", lambda *a, **k: None,
    )
    monkeypatch.setitem(claim.__dict__, "pick_issue", lambda *a, **k: None)
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery", lambda *a, **k: None,
    )
    bind_run_id("abc12345")
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], tmp_path / "slots", 1, config=config, hooks=resume_deps(),
    ) is None
    assert len(posted) == 1
    assert expected in posted[0]
    assert unexpected not in posted[0]
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "view", "9"])


def test_reconcile_open_epics_failure_is_fail_open(
    tmp_path, monkeypatch, caplog, bind_run_id,
):
    monkeypatch.setattr(claim, "reconcile_open_epics", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(
        milestone, "reconcile_release_milestones",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            github.MilestoneReconcileError(
                status=404, operation="list_milestones", stderr="HTTP 404",
            )
        ),
    )
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        claim, "pick_issue", lambda *args, **kwargs: {"number": 1},
    )
    bind_run_id("abc12345")
    result = claim.pick_next_delivery(["o/r"], tmp_path / "slots", 1, hooks=resume_deps())
    assert result == ("o/r", {"number": 1}, None)
    assert "epic_reconcile_failed repo=o/r" in caplog.text
    assert "milestone_reconcile_not_found repo=o/r status=404" in caplog.text


def test_reconcile_open_epics_runs_on_a_fresh_tick(
    monkeypatch, tmp_path, bind_run_id,
):
    calls = []
    bind_run_id(None)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        claim, "reconcile_open_epics",
        lambda repo, run_id, language_code: calls.append(
            (repo, run_id, language_code)),
    )
    monkeypatch.setattr(milestone, "reconcile_release_milestones", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "reconcile_orphan_prs", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_resumable_delivery", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_in_progress_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(claim, "pick_issue", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery", lambda *a, **k: None,
    )
    assert claim.pick_next_delivery(["o/r"], Path("/tmp/slots"), 1, hooks=resume_deps()) is None
    assert calls == [("o/r", "a1b2c3d4", "en")]
    # A repository's zh message language reaches the Epic sweep (Issue #1705).
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, source_repos=("o/r",), message_language="zh",
    )
    assert claim.pick_next_delivery(
        ["o/r"], Path("/tmp/slots"), 1, config=config, hooks=resume_deps(),
    ) is None
    assert calls[-1] == ("o/r", "a1b2c3d4", "zh")


def test_process_issue_closed_issue_ends_without_pr_ceremony(
    monkeypatch, tmp_path, caplog,
):
    """Issue #746: `deliver_pr` returns None for an Issue closed during
    delivery — no `ai-pr-opened` label patch, no `Orbi opened PR:` scene
    comment; the run ends `issue_closed` and the result kind ends the
    tick before the delivery wait."""
    monkeypatch.setattr(seam, "freeze_base",
                        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(pi_session, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(runner, "deliver_pr", lambda *args, **kwargs: None)
    edits: list[tuple] = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    comments = []
    monkeypatch.setattr(seam, "comment_issue",
        lambda number, *, repo, body: comments.append(body),
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(seam, "run_command", fake_run)
    # Issue #266: the health record is a pure bypass — a failed record
    # never changes the issue-closed outcome (the except branch runs).
    def dead_health(*args, **kwargs):
        raise RuntimeError("health state unwritable")

    monkeypatch.setattr(runner.runner_health, "record_run_attempt", dead_health)
    with caplog.at_level("INFO"):
        result = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main"),
            "xqliu/orbi-backlog",
        )
    assert result == ("issue-closed", None)
    assert "health_success_record_failed" in caplog.text
    assert not any(
        "ai-pr-opened" in str(args) + str(kwargs) for args, kwargs in edits
    ), "no ai-pr-opened label patch for a closed Issue"
    assert not any("Orbi opened PR" in body for body in comments)
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "result=issue_closed" in ends[0]
    assert "pr=-" in ends[0]


def _run_failed_log_sites(source: str) -> list[int]:
    """Scan one Python source; return the line numbers of the
    `run_failed` emission sites — the Issue #791 single-emission form
    `event("run_failed ...")` and the legacy `LOGGER.error("run_failed
    ...")` form alike."""
    sites: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and (first.value == "run_failed"
                     or first.value.startswith("run_failed "))):
            continue
        func = node.func
        is_event = isinstance(func, ast.Name) and func.id == "event"
        is_error = isinstance(func, ast.Attribute) and func.attr == "error"
        if is_event or is_error:
            sites.append(node.lineno)
    return sites


def test_run_failed_pin_detects_every_error_site():
    """The detector really detects: two `run_failed` ERROR sites produce
    two line numbers and a non-ERROR sibling is ignored."""
    source = (
        "event(\n"
        "    'run_failed',\n"
        "    format_run_scene(activity),\n"
        "    level=logging.ERROR, reason='a',\n"
        ")\n"
        "LOGGER.info('run_failed (historic)', s)\n"
        "LOGGER.error('run_failed %s reason=b', s)\n"
    )
    assert _run_failed_log_sites(source) == [1, 7]


def test_run_failed_scene_logged_in_one_place():
    """Issue #292: the `run_failed` skeleton (the six-argument
    `format_run_scene` + reason) lives in exactly ONE place —
    `pi_stream_log._log_run_failed` (extracted from `pi_process`, Issue
    #1860); each terminal branch computes its reason and exception and
    calls it once. A second literal `LOGGER.error("run_failed ...` site is
    the duplicated debt this pin rejects (a new log field would have to be
    edited per branch again)."""
    sites = _run_failed_log_sites(
        Path(pi_stream_log.__file__).read_text(encoding="utf-8")
    )
    assert len(sites) == 1, (
        "the run_failed scene must be logged in exactly one place (the "
        f"_fail_run helper, Issue #292); sites at lines: {sites}"
    )


def test_process_issue_yields_when_the_label_lands_in_the_scan_window(
    monkeypatch, tmp_path,
):
    """Issue #724：认领竞态窗口的前半段——扫描快照还没有 ai-in-progress、
    首次直读却已经看到 = 另一个实例就在本 tick 刚认领。此时必须让路，
    绝不能复用活 run 的 worktree/run_id 启动第二个 Pi（或降级开双交付）。
    真孤儿（标签在快照和直读里都有）照旧走 resume，不受此守卫影响。"""
    make_claim_race_gh(monkeypatch, {"in_progress": True})
    _claim_race_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [
        (tmp_path / "slot-0", 424242)])
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("must not start a second Pi over a live run")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


def test_process_issue_scan_window_orphan_resumes_when_no_runner_live(
    monkeypatch, tmp_path,
):
    """Issue #724 的另一半（#18/#668 契约的当代钉子）：同样的滞后快照
    形态，但没有任何其他活 runner 持槽 = 死 runner 留下的孤儿——必须照
    旧 resume（复用 run_id 进 run_pi），绝不能误让路把重启恢复拖慢
    一拍。"""
    make_claim_race_gh(monkeypatch, {"in_progress": True})
    _claim_race_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(seam, "slot_occupancy", lambda *a, **k: [
        (tmp_path / "slot-0", None), (tmp_path / "slot-1", os.getpid())])
    monkeypatch.setattr(seam, "apply_label_patch", Mock())
    monkeypatch.setattr(seam, "create_worktree", Mock(
        side_effect=AssertionError("orphan must resume")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "ai-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", slot_dir=tmp_path / "slots", max_concurrency=2)
    result = runner.process_issue(issue, config, "owner/repo")
    # 交付失败的收尾细节由既有 resume 测试族钉住；这里只钉"没误让路"。
    assert result != runner.IssueResult("claim-yielded", None)


def test_process_issue_claim_yield_guard_honors_custom_dispatch_label(
    monkeypatch, tmp_path,
):
    """Issue #724：让路守卫的标签谓词必须用仓库的 dispatch_label（#527），
    而不是写死的 ai-ready——自定义标签仓库的票永远不带 ai-ready，守卫
    在那里整段失效，#658 竞态原样复活。"""
    state = {"in_progress": False}
    make_claim_race_gh(monkeypatch, state)
    _claim_race_deps(
        monkeypatch, tmp_path,
        freeze_side_effect=lambda: state.__setitem__("in_progress", True),
    )
    monkeypatch.setattr(pi_session, "run_pi", Mock(
        side_effect=AssertionError("must not claim behind a live claimant")))
    issue = {"number": 18, "title": "t", "body": "b",
             "labels": [{"name": "repo-ready"}]}
    config = config_domain.RunnerConfig(repo_dir=tmp_path, prompt=tmp_path / "prompt.md", base_branch="main", dispatch_label="repo-ready")
    result = runner.process_issue(issue, config, "owner/repo")
    assert result == runner.IssueResult("claim-yielded", None)


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
