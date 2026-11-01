"""Claim and resume scene: run ids, claim races, resume wiring (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: run-id binding/validation, the
claim and release-dispatch races (label, stable branch, slot occupancy), the
resume scan that reuses an existing run and its progress comment, the resume
context written into run state, and the claim-yield guard over a scan window.

`_gh_api` (the shared fake for the progress publisher's `gh api` traffic)
is owned by `tests/test_bootstrap_runner_outcome.py` and imported here.
"""

from orbi import config as config_domain
import json
import logging
import os
import re
from unittest.mock import Mock

import pytest
import orbi.runner as runner

import orbi.run_state as run_state
import orbi.pi_session as pi_session
from orbi import progress
from tests.fakes.gitops import remote_head_answer
from tests.test_progress_wiring import make_fake_gh

from seam import seam
from tests.fakes.bootstrap_delivery import make_session_jsonl
from tests.test_bootstrap_runner_outcome import _gh_api


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
