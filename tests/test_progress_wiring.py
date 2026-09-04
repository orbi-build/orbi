"""Tests for the automatic GitHub progress comment wiring (Issue #18).

`process_issue` and `review_and_merge_if_clean` must keep exactly one
live progress comment per run (hidden run marker), PATCH it live while
any Pi session runs and at most every 30 seconds, post short milestone
comments for the key events, and end with either the final delivery
summary or the blocked scene — in the same comment.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, call as mock_call

import pytest

import orbi.runner as runner
from orbi import progress


def make_fake_gh(monkeypatch, comments=None, in_progress=False):
    """Answer every gh call; return (calls, posted_bodies).

    `in_progress` controls the restart-resume scan (`gh issue list
    --search label:ai-in-progress`): True when the Issue still carries
    the label (a run is, or was when the runner died, in flight),
    False for a fresh claim (Issue #18).
    """
    calls = []
    posted = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                # Plain GET of the comment list.
                return json.dumps(comments or [])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                posted.append(body[len("body="):])
                # Real `gh api` replies with the full comment object.
                return json.dumps({"id": 77, "body": body[len("body="):],
                                   "url": "https://x/77"})
            return ""
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([{"number": 18}] if in_progress else [])
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls, posted


def make_config(tmp_path):
    return {
        "repo_dir": tmp_path,
        "prompt": tmp_path / "prompt.md",
        "base_branch": "main",
    }


def make_issue():
    return {"number": 18, "title": "Publish progress", "body": "body"}


def patch_process_deps(monkeypatch, tmp_path, *, run_pi_side_effect=None):
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "freeze_base",
                        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")

    def fake_create_worktree(*args, **kwargs):
        path = tmp_path / "wt"
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(runner, "create_worktree", fake_create_worktree)
    monkeypatch.setattr(
        runner, "activity_snapshot",
        lambda session_dir: {
            "session_id": "sess-1",
            "session_file": str(tmp_path / "wt" / ".pi-session" / "s.jsonl"),
            "events": 3,
            "phase": "test",
            "last_activity": "2026-08-25T02:30:00Z",
            "action": "bash pytest tests/",
            "result": "ok",
            "model_wait": False,
            "changed": False,
            "stale_seconds": 1.0,
        },
    )
    if run_pi_side_effect is not None:
        monkeypatch.setattr(runner, "run_pi",
                            Mock(side_effect=run_pi_side_effect))
    else:
        monkeypatch.setattr(runner, "run_pi",
                            Mock(return_value="done"))
    # Issue #186: the fresh-claim closeout is `deliver_pr` (the Runner
    # pushes the task branch and opens the PR); the agent no longer does.
    monkeypatch.setattr(runner, "deliver_pr",
                        lambda *args, **kwargs:
                        "https://github.com/xqliu/orbi/pull/40")


# --- helpers ------------------------------------------------------------------


def test_read_test_result_returns_none_without_test_log(tmp_path):
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_returns_matching_summary_line(tmp_path):
    (tmp_path / "test.log").write_text(
        "collected 202 items\n156 passed in 4.43s\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "156 passed in 4.43s"


def test_read_test_result_falls_back_to_last_line(tmp_path):
    (tmp_path / "test.log").write_text("hello\nworld\n", encoding="utf-8")
    assert runner.read_test_result(tmp_path) == "world"


def test_read_test_result_returns_none_for_blank_log(tmp_path):
    (tmp_path / "test.log").write_text("   \n", encoding="utf-8")
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_skips_failures_section_header(tmp_path):
    """A pytest log with a FAILURES section must yield the real summary
    line, not the `=== FAILURES ===` section header (review round 2, PR
    #42): the old code returned the first `=` line, so a failed run
    posted `tests passed: === FAILURES ===`."""
    (tmp_path / "test.log").write_text(
        "============================= test session starts "
        "=============================\n"
        "platform linux -- Python 3.12.3, pytest-9.0.3\n"
        "collected 156 items\n"
        "\n"
        "tests/test_a.py .......................................... [ 26%]\n"
        "tests/test_b.py F........................................ [ 58%]\n"
        "\n"
        "=================================== FAILURES "
        "==================================\n"
        "____________________________ test_b_fails _____________________________\n"
        "\n"
        "    def test_b_fails():\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "\n"
        "=========================== short test summary info "
        "===========================\n"
        "FAILED tests/test_b.py::test_b_fails - assert 1 == 2\n"
        "1 failed, 155 passed in 4.43s\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "1 failed, 155 passed in 4.43s"


def test_read_test_result_prefers_last_summary_of_a_multi_run_log(tmp_path):
    """A log holding several runs (TDD red, then green) reports the most
    recent run: the last pytest summary line."""
    (tmp_path / "test.log").write_text(
        "1 failed, 155 passed in 4.43s\n"
        "--- second run ---\n"
        "156 passed in 4.43s\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "156 passed in 4.43s"


def test_read_test_result_falls_back_to_failed_line_without_summary(
    tmp_path,
):
    """A truncated log with a FAILURES section but no summary line must
    not report the section header: the first `FAILED` line is the
    failure evidence."""
    (tmp_path / "test.log").write_text(
        "=================================== FAILURES "
        "==================================\n"
        "FAILED tests/test_b.py::test_b_fails - assert 1 == 2\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == \
        "FAILED tests/test_b.py::test_b_fails - assert 1 == 2"


def test_read_test_result_unwraps_padded_summary_line(tmp_path):
    """Some runners wrap the final summary in `=` padding; the reported
    line is the bare summary, never the padding."""
    (tmp_path / "test.log").write_text(
        "========================= 1 failed, 155 passed in 4.43s "
        "=========================\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "1 failed, 155 passed in 4.43s"


def test_read_test_result_is_none_when_the_log_holds_only_headers(
    tmp_path,
):
    """A log holding nothing but section headers carries no result info:
    reporting the header itself is the bug this fix removes (review
    round 2, PR #42)."""
    (tmp_path / "test.log").write_text(
        "==================== FAILURES ====================\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_is_none_when_no_tests_ran(tmp_path):
    """A pytest run that collected no tests verified nothing: reporting
    it as a pass is a false mobile notification (review round 3, PR
    #42)."""
    (tmp_path / "test.log").write_text(
        "collected 0 items\nno tests ran in 0.01s\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_is_none_when_no_tests_collected(tmp_path):
    """The collect-only variant of the no-tests message is no result
    either."""
    (tmp_path / "test.log").write_text(
        "no tests collected in 0.00s\n", encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_is_none_for_deselected_only_summary(tmp_path):
    """A summary whose counts carry no outcome (`N deselected`) is no
    result either (review round 3, PR #42)."""
    (tmp_path / "test.log").write_text(
        "3 deselected in 0.02s\n", encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_is_none_for_skipped_only_summary(tmp_path):
    (tmp_path / "test.log").write_text(
        "2 skipped in 0.01s\n", encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) is None


def test_read_test_result_prefers_last_run_with_an_outcome(tmp_path):
    """A multi-run log whose LAST run collected no tests reports the
    last run that actually collected tests (review round 3, PR #42)."""
    (tmp_path / "test.log").write_text(
        "1 failed, 1 passed in 0.03s\n"
        "--- second run (empty selection) ---\n"
        "no tests ran in 0.01s\n",
        encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "1 failed, 1 passed in 0.03s"


def test_read_test_result_reports_error_summary(tmp_path):
    """A collection error IS an outcome (pytest exits non-zero): it is
    reported, so the milestone check can post `tests failed`."""
    (tmp_path / "test.log").write_text(
        "1 error in 0.01s\n", encoding="utf-8",
    )
    assert runner.read_test_result(tmp_path) == "1 error in 0.01s"


def test_delivery_head_advanced_detects_new_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: "1111111111111111111111111111111111111111",
    )
    assert runner.delivery_head_advanced(
        tmp_path, "2222222222222222222222222222222222222222",
    ) is True


def test_delivery_head_advanced_is_false_when_head_equals_base(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: "2222222222222222222222222222222222222222",
    )
    assert runner.delivery_head_advanced(
        tmp_path, "2222222222222222222222222222222222222222",
    ) is False


def test_delivery_head_advanced_fails_fast_when_git_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(128, command, stderr="not a repo"),
        ),
    )
    with pytest.raises(subprocess.CalledProcessError):
        runner.delivery_head_advanced(
            tmp_path, "2222222222222222222222222222222222222222",
        )


def test_failure_detail_keeps_subprocess_stderr_visible():
    exc = subprocess.CalledProcessError(1, ["pi"], stderr="boom")
    assert "boom" in runner._failure_detail(exc)


def test_failure_detail_without_stderr_uses_message():
    assert runner._failure_detail(RuntimeError("plain")) == "plain"


def test_progress_body_outcome_header_prepends_outcome():
    state = {
        "run_id": "a1b2c3d4", "issue": 18,
        "issue_title": "Publish progress", "role": "implement",
        "phase": "test", "elapsed": "1s", "last_activity": None,
        "last_action": None, "tests": None, "review_round": 0,
        "branch": "b", "pr": None, "session": None,
    }
    body = runner._progress_body(state, outcome="**done**")
    assert body.startswith("**done**\n\n")
    assert runner._progress_body(state) == progress.progress_body(state)


def test_publish_plan_milestone_posts_only_when_plan_exists(tmp_path):
    posted = []
    publisher = Mock()
    publisher.milestone = Mock(side_effect=lambda text: posted.append(text))
    runner._publish_plan_milestone(publisher, tmp_path)
    assert posted == []
    (tmp_path / "plan.md").write_text("# Plan\n", encoding="utf-8")
    runner._publish_plan_milestone(publisher, tmp_path)
    assert posted == ["plan ready"]


def test_publish_test_milestone_posts_passed_or_failed(tmp_path):
    posted = []
    publisher = Mock()
    publisher.milestone = Mock(side_effect=lambda text: posted.append(text))
    # No test.log: nothing is posted.
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == []
    (tmp_path / "test.log").write_text(
        "156 passed in 4.43s\n", encoding="utf-8",
    )
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == ["tests passed: 156 passed in 4.43s"]
    posted.clear()
    (tmp_path / "test.log").write_text(
        "1 failed, 155 passed in 4.43s\n", encoding="utf-8",
    )
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == ["tests failed: 1 failed, 155 passed in 4.43s"]


def test_publish_test_milestone_detects_failure_case_insensitively(
    tmp_path,
):
    """Uppercase failure markers (e.g. a `FAILURES` section line that a
    truncated log still yields) must post `tests failed`, never
    `tests passed` (review round 2, PR #42)."""
    posted = []
    publisher = Mock()
    publisher.milestone = Mock(side_effect=lambda text: posted.append(text))
    (tmp_path / "test.log").write_text(
        "FAILED tests/test_b.py::test_b_fails - assert 1 == 2\n",
        encoding="utf-8",
    )
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == [
        "tests failed: FAILED tests/test_b.py::test_b_fails - assert 1 == 2",
    ]


def test_publish_test_milestone_posts_nothing_when_no_tests_ran(
    tmp_path,
):
    """A run that collected no tests must not post a `tests passed`
    milestone: no result, no notification (review round 3, PR #42)."""
    posted = []
    publisher = Mock()
    publisher.milestone = Mock(side_effect=lambda text: posted.append(text))
    (tmp_path / "test.log").write_text(
        "collected 0 items\nno tests ran in 0.01s\n",
        encoding="utf-8",
    )
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == []
    posted.clear()
    (tmp_path / "test.log").write_text(
        "3 deselected in 0.02s\n", encoding="utf-8",
    )
    runner._publish_test_milestone(publisher, tmp_path)
    assert posted == []


# --- process_issue wiring -----------------------------------------------------


def test_process_issue_creates_progress_comment_with_marker(monkeypatch, tmp_path):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    progress_posts = [
        body for body in posted
        if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1, (
        f"expected exactly one progress comment, got: {posted}"
    )
    body = progress_posts[0]
    # Issue #100: the issue line shows the number AND the title.
    assert "- issue: #18 Publish progress" in body
    assert "- role: implement" in body
    assert "- branch: orbi/xqliu-orbi-issue-18-a1b2c3d4" in body
    assert "- PR: -" in body


def test_process_issue_p0_progress_comment_and_milestones_carry_priority(
    monkeypatch, tmp_path,
):
    """Issue #101: a P0 pickup (the scan's issue dict carries `labels`)
    shows `priority: p0` in the progress comment and `priority=p0` in
    the journal/scene text (run_info, started milestone, started-Pi
    scene comment) — the explicit field is visible end to end."""
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    edit = Mock()
    # AFTER patch_process_deps: it installs its own edit_issue mock.
    monkeypatch.setattr(runner, "edit_issue", edit)
    issue = make_issue()
    issue["labels"] = [{"name": "p0"}, {"name": "ai-ready"}]
    runner.process_issue(issue, make_config(tmp_path), "xqliu/orbi")
    progress_posts = [
        body for body in posted if "**Orbi progress**" in body
    ]
    assert len(progress_posts) == 1
    assert "- priority: p0" in progress_posts[0]
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    started = [b for b in milestones if "Orbi: started" in b]
    assert started, f"no started milestone: {milestones}"
    assert "priority=p0" in started[0]
    # The started-Pi scene comment (run_info) carries the priority too.
    scene_comments = [
        call for call in calls
        if call[:2] == ["gh", "issue"] and "comment" in call
    ]
    assert any("priority=p0" in call[-1] for call in scene_comments)


def test_process_issue_p0_failure_enters_ai_blocked_terminal_state(
    monkeypatch, tmp_path,
):
    """Issue #101: a failed P0 run enters the terminal state
    `ai-blocked` ALONE (the claim label is removed, the `ai-ready`
    residue is excluded by every ready scan) — no tick can re-claim it,
    so there is no infinite retry; the blocked scene keeps the
    priority visible and the concrete failure reason."""
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(
        monkeypatch, tmp_path,
        run_pi_side_effect=subprocess.CalledProcessError(
            3, ["pi"], stderr="pi exploded",
        ),
    )
    edit = Mock()
    # AFTER patch_process_deps: it installs its own edit_issue mock.
    monkeypatch.setattr(runner, "edit_issue", edit)
    issue = make_issue()
    issue["labels"] = [{"name": "p0"}, {"name": "ai-ready"}]
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the terminal-state assertions below are
    # unchanged.
    assert runner.process_issue(issue, make_config(tmp_path),
                                "xqliu/orbi") is None
    # Claim first, then the terminal state: `ai-blocked` added,
    # `ai-in-progress` removed.
    assert edit.call_args_list[0] == mock_call(
        18, repo="xqliu/orbi", add="ai-in-progress",
    )
    assert edit.call_args_list[1] == mock_call(
        18, repo="xqliu/orbi", add="ai-blocked",
        remove="ai-in-progress",
    )
    # The failure comment carries the run marker and the reason.
    failure_comments = [
        call for call in calls
        if call[:2] == ["gh", "issue"] and "comment" in call
        and "Orbi failed" in call[-1]
    ]
    assert failure_comments, f"no failure comment: {calls}"
    assert "<!-- orbi:run=a1b2c3d4 -->" in failure_comments[0][-1]
    assert "pi exploded" in failure_comments[0][-1]
    # The blocked progress scene keeps the priority visible.
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):] if final_patches else ""
    assert "Orbi blocked" in last_body
    assert "- priority: p0" in last_body
    assert "pi exploded" in last_body
    # Issue #100: the P0 blocked scene shows the number AND the title.
    assert "- issue: #18 Publish progress" in last_body


def test_process_issue_fails_fast_on_missing_issue_title(
    monkeypatch, tmp_path,
):
    """Issue #100: a scanned issue without a title violates the GitHub
    issue data contract: `process_issue` fails fast (KeyError) instead
    of fabricating a title or publishing a bare `#<number>` line."""
    make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    issue = {"number": 18, "body": "body"}  # no "title"
    with pytest.raises(KeyError):
        runner.process_issue(issue, make_config(tmp_path),
                             "xqliu/orbi")


def test_process_issue_passes_issue_number_to_deliver_pr(
    monkeypatch, tmp_path,
):
    """The fresh path verifies the `Fixes #<issue>` keyword against the
    source Issue number (Issue #53) — now inside the Runner's
    `deliver_pr` closeout (Issue #186), which forwards it to
    `verify_pr`."""
    make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    deliver_calls = []

    def fake_deliver_pr(*args, **kwargs):
        deliver_calls.append((args, kwargs))
        return "https://github.com/xqliu/orbi/pull/40"

    monkeypatch.setattr(runner, "deliver_pr", fake_deliver_pr)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    assert len(deliver_calls) == 1
    args, kwargs = deliver_calls[0]
    assert kwargs.get("issue") == 18, (
        f"deliver_pr must verify the Fixes keyword against the source "
        f"Issue number, got args={args} kwargs={kwargs}"
    )


def test_process_issue_posts_started_and_pr_opened_milestones(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: started" in body for body in milestones)
    assert any(
        "Orbi: PR opened" in body
        and "https://github.com/xqliu/orbi/pull/40" in body
        for body in milestones
    )


def test_process_issue_finishes_progress_comment_with_delivery_summary(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)

    def fake_run_pi(*args, **kwargs):
        (tmp_path / "wt" / "test.log").write_text(
            "156 passed in 4.43s\n", encoding="utf-8",
        )
        return "done"

    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    # The final PATCH of comment 77 must carry the delivery summary.
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "--method" in command
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi delivered" in last_body
    assert "https://github.com/xqliu/orbi/pull/40" in last_body
    assert "156 passed" in last_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in last_body


def test_process_issue_failure_updates_progress_comment_with_blocked_scene(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(
        monkeypatch, tmp_path,
        run_pi_side_effect=subprocess.CalledProcessError(
            3, ["pi"], stderr="pi exploded",
        ),
    )
    # Issue #239: the failure is terminal — `process_issue` returns `None`
    # instead of re-raising; the blocked-scene assertions below are
    # unchanged.
    assert runner.process_issue(make_issue(), make_config(tmp_path),
                                "xqliu/orbi") is None
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: blocked" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi blocked" in last_body
    assert "pi exploded" in last_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in last_body


def test_process_issue_resumes_existing_progress_comment_after_restart(
    monkeypatch, tmp_path,
):
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nstale implementer state"
        ),
    }
    calls, posted = make_fake_gh(monkeypatch, comments=[existing])
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    # No new progress comment is posted: the restarted run reuses id 77.
    progress_posts = [
        body for body in posted
        if "**Orbi progress**" in body
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "existing progress comment was not updated"
    # And the final summary still lands in the same comment.
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi delivered" in last_body


def test_process_issue_posts_plan_ready_milestone_when_plan_written(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    # The fake pi session writes plan.md into the worktree.
    def fake_run_pi(*args, **kwargs):
        (tmp_path / "wt" / "plan.md").write_text(
            "# Plan\n\n## Goal\n\nship it\n", encoding="utf-8",
        )
        return "done"

    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any(
        "Orbi: plan ready" in body for body in milestones
    )


def test_process_issue_posts_tests_passed_milestone_when_test_log_ok(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)

    def fake_run_pi(*args, **kwargs):
        (tmp_path / "wt" / "test.log").write_text(
            "156 passed in 4.43s\n", encoding="utf-8",
        )
        return "done"

    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: tests passed" in body for body in milestones)


def test_process_issue_posts_tests_failed_milestone_when_test_log_fails(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)

    def fake_run_pi(*args, **kwargs):
        (tmp_path / "wt" / "test.log").write_text(
            "1 failed, 155 passed in 4.43s\n", encoding="utf-8",
        )
        return "done"

    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: tests failed" in body for body in milestones)


def test_process_issue_never_posts_fix_pushed_on_a_fresh_claim(
    monkeypatch, tmp_path,
):
    # The implementer always commits the delivery on top of the frozen
    # base, so the head always advanced: a fresh claim must not turn
    # that into a `fix pushed` milestone (the PR opened milestone
    # announces the delivery; Issue #82 removed the fixer and its
    # `fix pushed` milestone — findings are fixed by the review
    # session, which records its own round comments).
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "delivery_head_advanced", lambda *args: True)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert milestones
    assert not any("fix pushed" in body for body in milestones)


# --- Issue #60: progress failures after the PR open must not fail the
# --- delivery (no ai-blocked, no re-raise; progress_publish_failed only) -----


def make_failing_gh(monkeypatch, is_failing, comments=None):
    """Like `make_fake_gh`, but calls matching `is_failing` raise the
    `gh: Not Found (HTTP 404)` error — the shape of the #57 delivered
    PATCH failure (Issue #60)."""
    calls = []
    posted = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if is_failing(command):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Not Found (HTTP 404)",
            )
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                return json.dumps(comments or [])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                posted.append(body[len("body="):])
                return json.dumps({"id": 77, "body": body[len("body="):],
                                   "url": "https://x/77"})
            return ""
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls, posted


def _progress_patch_of(command) -> bool:
    return (
        command[:2] == ["gh", "api"]
        # GitHub update route (Issue #58): no issue number.
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "--method" in command
        and "PATCH" in command
    )


def _delivery_record_of(command) -> bool:
    """The two `ProgressPublisher` delivery-record calls after the PR
    is verified and labeled: the `PR opened` milestone and the
    delivered finish() PATCH (both are progress publishing; neither may
    fail the delivery, Issue #60). The opened-PR scene comment is NOT
    part of this set: it is the resume contract (Issue #45/#89) and
    stays fail-fast (Issue #79)."""
    if command[:2] == ["gh", "api"]:
        if "--method" in command and "POST" in command:
            return "Orbi: PR opened" in command[-1]
        return _progress_patch_of(command)
    return False


def test_process_issue_delivered_patch_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """The #57 scene: the PR is verified and labeled `ai-pr-opened`,
    then the delivered finish() PATCH 404s. The delivery must NOT fail:
    no re-raise, no `ai-blocked`, the error is logged like the in-stream
    callback (`progress_publish_failed`) and the PR URL is returned so
    the run continues into the review/merge wait loop."""
    calls, posted = make_failing_gh(monkeypatch, _progress_patch_of)
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    # The state transition happened (the Issue awaits review)...
    assert edits == [{
        "repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-pr-opened",
         "remove": "ai-in-progress"},
    ]
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The failure is logged like the in-stream callback...
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "issue=xqliu/orbi#18" in line
        and "role=implement" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # ...and the delivery summary was attempted (the 404'd PATCH).
    delivered_patches = [c for c in calls if _progress_patch_of(c)]
    assert delivered_patches, "the delivered finish was not attempted"
    last_body = delivered_patches[-1][
        delivered_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi delivered" in last_body


def test_process_issue_pr_opened_milestone_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """The `PR opened` milestone POST failing after the label transition
    is the same contract: logged, not fatal; the scene comment and the
    delivered finish still run (independent publishing steps)."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "api"]
            and "--method" in command
            and "POST" in command
            and "Orbi: PR opened" in command[-1]
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The delivered finish still ran (independent step)...
    delivered_patches = [c for c in calls if _progress_patch_of(c)]
    assert delivered_patches, "the delivered finish was not attempted"
    last_body = delivered_patches[-1][
        delivered_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi delivered" in last_body
    # ...and the failed milestone was not posted.
    assert not any("Orbi: PR opened" in body for body in posted)


def test_process_issue_scene_comment_failure_fails_delivery(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: the `Orbi opened PR:` scene comment is NOT a
    bypass — the next tick's resume (Issue #45/#89) parses it to recover
    run_id, base and PR, so a failure there is a real delivery failure:
    the Issue is marked `ai-blocked` with a `Orbi failed`
    comment, and `process_issue` returns `None` so the tick ends cleanly
    (Issue #239: the handled failure never re-raises to crash the
    service). The scene comment stays fail-fast (the resume contract is
    unchanged); only the `ProgressPublisher` steps around it (milestone,
    delivered finish) are bypasses. The failure happens AFTER the
    opened-PR transition, so the terminal state is `ai-blocked` ALONE
    (the docs/workflow.mdx label lifecycle removes `ai-pr-opened` on
    terminal failure) — never `ai-pr-opened` + `ai-blocked`, which no
    scan would own."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "issue"]
            and "comment" in command
            # Only the scene comment POST fails: the `Orbi
            # failed` comment embeds the scene body in its error
            # detail, so it must not 404 in the fake.
            and "Orbi opened PR:" in command[-1]
            and "Orbi failed" not in command[-1]
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    # Issue #239: the failure is terminal — `process_issue` returns
    # `None` instead of re-raising; the terminal-state assertions below
    # are unchanged.
    assert runner.process_issue(make_issue(), make_config(tmp_path),
                                "xqliu/orbi") is None

    # The delivery failed: the opened-PR transition is undone and the
    # Issue is marked ai-blocked ALONE (the failure happened after the
    # `ai-pr-opened` label was added, so the terminal state removes it
    # instead of the already-removed claim label)...
    assert edits == [{
        "repo": "xqliu/orbi", "add": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-pr-opened",
         "remove": "ai-in-progress"},
        {"repo": "xqliu/orbi", "add": "ai-blocked",
         "remove": "ai-pr-opened"},
    ]
    # ...with a run-marked `Orbi failed` comment naming the
    # scene-comment failure...
    comment_bodies = [
        command[-1] for command in calls
        if command[:2] == ["gh", "issue"] and "comment" in command
    ]
    failed = [body for body in comment_bodies
              if "Orbi failed" in body]
    assert failed, comment_bodies
    assert "Orbi opened PR:" in failed[0]
    assert "<!-- orbi:run=a1b2c3d4 -->" in failed[0]
    # ...and the failure is NOT logged as a progress bypass (the scene
    # comment is delivery, not observability).
    assert not any("progress_publish_failed" in line
                   for line in caplog.text.splitlines()), caplog.text
    # The `PR opened` milestone was never posted: the scene comment is
    # the first delivery-record step and its failure interrupts the
    # flow before the bypass steps (a PR without a resumable scene must
    # not be announced as delivered).
    assert not any("Orbi: PR opened" in body for body in posted)
    # The terminal blocked scene landed in the progress comment (the
    # failure-path publishing, like the rest of the failure report).
    assert any("Orbi: blocked" in body for body in posted)
    last_patch = [c for c in calls if _progress_patch_of(c)][-1]
    last_body = last_patch[last_patch.index("--field") + 1][len("body="):]
    assert "Orbi blocked" in last_body
    assert "Orbi opened PR:" in last_body


def test_process_issue_publishing_failure_still_logs_run_end(
    monkeypatch, tmp_path, caplog,
):
    """The `run_end` line is the journal record of the successful
    delivery; it must be logged even when the delivery-record publishing
    failed."""
    make_failing_gh(monkeypatch, _progress_patch_of)
    patch_process_deps(monkeypatch, tmp_path)
    caplog.set_level("INFO")

    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/orbi")

    ends = [line for line in caplog.text.splitlines()
            if " run_end " in line]
    assert len(ends) == 1
    assert "result=pr_opened" in ends[0]
    assert "pr=https://github.com/xqliu/orbi/pull/40" in ends[0]


def test_process_issue_publishing_failure_is_never_blocked_scene(
    monkeypatch, tmp_path,
):
    """The failure report (blocked milestone, blocked progress scene,
    `Orbi failed` comment) must NOT run for a progress failure
    after the PR open: the delivery succeeded."""
    calls, posted = make_failing_gh(monkeypatch, _delivery_record_of)
    patch_process_deps(monkeypatch, tmp_path)

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    # No blocked milestone...
    assert not any("Orbi: blocked" in body for body in posted)
    # ...no `Orbi failed` comment...
    comment_bodies = [
        command[-1] for command in calls
        if command[:2] == ["gh", "issue"] and "comment" in command
    ]
    assert not any("Orbi failed" in body
                   for body in comment_bodies)
    # ...and no blocked progress scene.
    assert not any(
        "Orbi blocked" in body
        for command in calls if _progress_patch_of(command)
        for body in [command[command.index("--field") + 1][len("body="):]]
    )


# --- Issue #79: the whole ProgressPublisher path is a bypass --------------


def test_process_issue_ensure_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79 acceptance: the progress comment cannot even be
    created (the ensure GET/POST 404s) BEFORE `run_pi`. The delivery
    must not fail: Pi still runs, the PR is still opened, the Issue is
    never `ai-blocked`, and the error is logged as
    `progress_publish_failed` (the same bypass semantics as the
    in-stream callback)."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: command[:2] == ["gh", "api"],
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    # The delivery completed: the PR is open and awaits review...
    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    assert any(
        kwargs.get("add") == "ai-pr-opened"
        for kwargs in edits
    )
    # ...Pi ran (run_pi was not skipped because ensure failed)...
    assert runner.run_pi.called
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The failure is logged like the in-stream callback.
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "issue=xqliu/orbi#18" in line
        and "role=implement" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # No blocked scene was published.
    assert not any("Orbi: blocked" in body for body in posted)


def test_process_issue_started_milestone_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: the `started` milestone POST failing before `run_pi`
    is the same contract: logged, not fatal; Pi still runs and the PR
    is still opened."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "api"]
            and "--method" in command
            and "POST" in command
            and "Orbi: started" in command[-1]
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    assert runner.run_pi.called
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The ensure itself succeeded (only the milestone failed)...
    assert any("Orbi progress" in body for body in posted)
    # ...and the failed milestone was not posted.
    assert not any("Orbi: started" in body for body in posted)


def test_process_issue_plan_test_milestone_failures_do_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: the post-Pi `plan ready` / `tests passed` milestone
    POSTs failing is the same contract: logged, not fatal; the PR is
    still opened and the run continues into the review wait."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "api"]
            and "--method" in command
            and "POST" in command
            and ("Orbi: plan ready" in command[-1]
                 or "Orbi: tests passed" in command[-1])
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    # The worktree (created by the patched create_worktree) carries a
    # plan.md and a passing test.log so both milestones would post.
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (worktree / "test.log").write_text(
        "5 passed in 1.0s\n", encoding="utf-8",
    )

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/orbi")

    assert pr_url == "https://github.com/xqliu/orbi/pull/40"
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text
    # The failed milestones were not posted...
    assert not any("Orbi: plan ready" in body for body in posted)
    assert not any("Orbi: tests passed" in body for body in posted)
    # ...but the delivery record still completed.
    assert any("Orbi: PR opened" in body for body in posted)


def test_process_issue_failure_path_progress_failure_keeps_blocked_transition(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: on a REAL delivery failure (Pi exploded), the
    failure-path progress publishing (blocked milestone, blocked-scene
    finish) is bypass too: a 404 there must not abort the `ai-blocked`
    label transition or the `Orbi failed` comment — the progress
    failure only logs `progress_publish_failed` (a bypass failure never
    decides whether the delivery succeeded, Issue #79). Issue #239: the
    handled failure returns `None` from `process_issue` instead of
    re-raising."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "api"]
            and "--method" in command
            and (
                # The blocked milestone POST...
                ("POST" in command
                 and "Orbi: blocked" in command[-1])
                # ...and the blocked-scene finish PATCH.
                or "PATCH" in command
            )
        ),
    )
    patch_process_deps(
        monkeypatch, tmp_path,
        run_pi_side_effect=subprocess.CalledProcessError(
            3, ["pi"], stderr="pi exploded",
        ),
    )
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    # Issue #239: the failure is terminal — `process_issue` returns
    # `None` instead of re-raising; the bypass assertions below are
    # unchanged.
    assert runner.process_issue(make_issue(), make_config(tmp_path),
                                "xqliu/orbi") is None

    # The `ai-blocked` transition completed even though the progress
    # publishing 404'd...
    assert any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # ...the `Orbi failed` comment was posted...
    comment_bodies = [
        command[-1] for command in calls
        if command[:2] == ["gh", "issue"] and "comment" in command
    ]
    assert any("Orbi failed" in body for body in comment_bodies)
    # ...and the progress failures were logged as bypass, not re-raised
    # out of the failure report.
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "role=implement" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # The failure report did not die on the first progress failure:
    # the blocked-scene finish was still attempted after the blocked
    # milestone 404'd (independent bypass steps), so the blocked scene
    # PATCH was attempted...
    attempted_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and "--method" in command
        and "PATCH" in command
    ]
    assert attempted_patches, "the blocked-scene finish was not attempted"
    # ...both bypass steps failed (two logged failures)...
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text
    # ...and the blocked milestone was never posted.
    assert not any("Orbi: blocked" in body for body in posted)


# --- review_and_merge_if_clean wiring (the reviewer stays observable) ---------


def test_review_and_merge_posts_review_findings_milestone(monkeypatch, tmp_path):
    findings_verdict = "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [{"level": "Blocker", "location": "a.py:1",
                      "note": "x"}],
    })
    merged, edits, calls, posted = _run_review_and_merge(
        monkeypatch, tmp_path, verdict=findings_verdict,
    )
    assert merged is False
    assert any(kwargs.get("add") == "ai-fix-needed" for kwargs in edits)
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: review findings" in body for body in milestones)
    assert any("round 1" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi review findings" in last_body
    assert "- review/fix round: 1" in last_body


def test_review_and_merge_posts_merged_milestone_and_final_summary(
    monkeypatch, tmp_path,
):
    pass_verdict = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 0,
        "findings": [],
    })
    merged, edits, calls, posted = _run_review_and_merge(
        monkeypatch, tmp_path, verdict=pass_verdict,
    )
    assert merged is True
    assert any(kwargs.get("add") == "ai-merged" for kwargs in edits)
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Orbi: merged" in body for body in milestones)
    assert any("merge_commit=m1" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/orbi/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Orbi delivered" in last_body
    assert "merge_commit=m1" in last_body


# --- Issue #79: the whole ProgressPublisher path is a bypass in the
# --- review/merge step (a 404 never marks the Issue ai-blocked) ----------


def _progress_call_of(command) -> bool:
    """True for the `ProgressPublisher` gh traffic (issue comments API:
    the ensure GET/POST, the milestone POST, the finish PATCH)."""
    return command[:2] == ["gh", "api"]


def _run_review_and_merge(monkeypatch, tmp_path, *, verdict,
                          fail_progress=None):
    """Run `review_and_merge_if_clean` with a fake gh that optionally
    404s the progress calls; return (merged, edits, calls, posted).
    The fake serves ONLY the progress API (issue comments): every
    other command is rejected, so the test proves the review step's
    gh traffic is progress publishing plus the mocked delivery steps."""
    calls = []
    posted = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if fail_progress is not None and fail_progress(command):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Not Found (HTTP 404)",
            )
        if command[:2] == ["gh", "api"]:
            if "--method" not in command:
                return json.dumps([])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                posted.append(body[len("body="):])
                return json.dumps({"id": 77, "body": body[len("body="):],
                                   "url": "https://x/77"})
            return ""
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything that is not progress API traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run_command(["gh", "release", "list"])
    monkeypatch.setattr(runner, "run_command", fake_run_command)
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: {
        "number": 4, "url": "https://github.com/xqliu/orbi/pull/40",
        "base_ref": "main", "base_oid": "b1",
        "head_ref": "h", "head_oid": "h1",
    })
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: verdict)
    edits = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda number, **kwargs: edits.append(kwargs),
    )
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "comment_pr", Mock())
    if "pass" in verdict:
        monkeypatch.setattr(runner, "merge_gate", lambda *a, **k: {
            "number": 4, "url": "https://github.com/xqliu/orbi/pull/40",
            "base_ref": "main", "base_oid": "b1",
            "head_ref": "h", "head_oid": "h1", "merged": True,
        })
        monkeypatch.setattr(runner, "confirm_merged", lambda *a, **k: {
            "state": "MERGED", "merge_commit": "m1",
        })
        monkeypatch.setattr(runner, "sync_base_checkout", Mock())
    else:
        # Findings: the merge gate must never be reached (the Issue
        # moves to ai-fix-needed instead).
        monkeypatch.setattr(runner, "merge_gate",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("no merge")))
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main",
        {"repo_dir": tmp_path, "base_branch": "main", "base_sha": "b1",
         "run_id": "a1b2c3d4"},
        "xqliu/orbi", 18, title="Publish progress",
        priority="normal",
    )
    return merged, edits, calls, posted


def test_review_and_merge_ensure_failure_does_not_block_issue(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79 acceptance: the progress comment 404s in the review
    step (the ensure GET/POST before the review Pi). The review must
    still run and the delivery state must still move: findings land
    `ai-fix-needed`, a clean verdict merges and lands `ai-merged` —
    never `ai-blocked`, and the failure is logged as
    `progress_publish_failed` only."""
    findings_verdict = "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [{"level": "Blocker", "location": "a.py:1",
                      "note": "x"}],
    })
    caplog.set_level("ERROR")
    merged, edits, calls, posted = _run_review_and_merge(
        monkeypatch, tmp_path, verdict=findings_verdict,
        fail_progress=_progress_call_of,
    )
    assert merged is False
    # The findings state transition happened (the next review session
    # retries the same PR)...
    assert any(kwargs.get("add") == "ai-fix-needed" for kwargs in edits)
    # ...the review ran (run_review was called: the findings comment
    # was posted to the Issue)...
    assert runner.comment_issue.called
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The progress failure is logged as bypass.
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "issue=xqliu/orbi#18" in line
        and "role=review" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # No progress comment was posted (the fake 404s all progress
    # traffic), but the delivery-record steps still ran: the findings
    # milestone was attempted...
    assert posted == []
    attempted_milestones = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and "--method" in command
        and "POST" in command
        and "Orbi: review findings" in command[-1]
    ]
    assert attempted_milestones, "the findings milestone was not attempted"
    # ...and the findings finish was attempted too (it raises
    # `no progress comment to update` because the ensure 404'd, and
    # that is logged as bypass as well — no PATCH can run without a
    # tracked comment id).
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text


def test_review_and_merge_clean_verdict_merges_despite_progress_404(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: the same 404 on a clean verdict must not stop the
    merge: the PR is merged, the Issue is labeled `ai-merged`, and the
    progress failure is logged as `progress_publish_failed` only."""
    pass_verdict = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 0,
        "findings": [],
    })
    caplog.set_level("ERROR")
    merged, edits, calls, posted = _run_review_and_merge(
        monkeypatch, tmp_path, verdict=pass_verdict,
        fail_progress=_progress_call_of,
    )
    assert merged is True
    # The merge landed and the Issue is ai-merged...
    assert any(kwargs.get("add") == "ai-merged" for kwargs in edits)
    # ...the merged PR scene comment was posted (delivery record, not
    # progress)...
    assert runner.comment_issue.called
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The progress failures are logged as bypass (the merged milestone
    # and the delivered finish were both attempted and 404'd).
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text
    assert all(
        "role=review" in line
        for line in caplog.text.splitlines()
        if "progress_publish_failed" in line
    ), caplog.text
    assert posted == []


def test_review_and_merge_findings_publish_failure_does_not_block_issue(
    monkeypatch, tmp_path, caplog,
):
    """Issue #79: the findings-branch milestone/finish 404 AFTER the
    review verdict is the same contract: logged, not fatal; the
    `ai-fix-needed` transition still lands."""
    findings_verdict = "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [{"level": "Blocker", "location": "a.py:1",
                      "note": "x"}],
    })
    caplog.set_level("ERROR")
    merged, edits, calls, posted = _run_review_and_merge(
        monkeypatch, tmp_path, verdict=findings_verdict,
        fail_progress=lambda command: (
            command[:2] == ["gh", "api"]
            and "--method" in command
            and (
                ("POST" in command
                 and "Orbi: review findings" in command[-1])
                or "PATCH" in command
            )
        ),
    )
    assert merged is False
    assert any(kwargs.get("add") == "ai-fix-needed" for kwargs in edits)
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The ensure succeeded (only the findings milestone/finish 404'd)...
    assert any("Orbi progress" in body for body in posted)
    # ...the failed milestone was not posted...
    assert not any("Orbi: review findings" in body
                   for body in posted)
    # ...and the findings finish was still attempted (independent
    # bypass step).
    attempted_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and "--method" in command
        and "PATCH" in command
    ]
    assert attempted_patches, "the findings finish was not attempted"


# --- wait_for_delivery terminal failures stay observable ----------------------


def test_wait_for_delivery_closed_unmerged_posts_blocked_milestone(
    monkeypatch,
):
    pr_url = "https://github.com/owner/repo/pull/46"
    api_calls = []
    posted = []
    # The blocked scene derives the round from the trusted
    # review-round comments (review round 2, PR #42); Issue
    # #82: both opened-PR states are review states, so the
    # role is always `review` (the label lookup only serves
    # the leftover-label cleanup below).
    _wait_delivery_fake_gh(
        monkeypatch, pr_state="CLOSED",
        labels=[{"name": "ai-fix-needed"}],
        comments=_review_round_comments(),
        fail_progress=lambda command: False,
        api_calls=api_calls, posted=posted,
    )
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.wait_for_delivery(
        pr_url, {"number": 39, "title": "t", "body": ""}, {}, "owner/repo",
    )
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    # The blocked milestone carries the mobile notification...
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        ("closed without a merge" in body for body in posted_bodies),
    )
    # ...and the tracked progress comment becomes the blocked scene
    # (Issue #18): the same terminal body the other failure paths write.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in blocked
    assert "closed without a merge" in blocked
    assert "next step:" in blocked
    assert "<!-- orbi:run=a1b2c3d4 -->" in blocked
    # Issue #100: the wait-loop blocked scene shows the number AND
    # the title, consistent with the other scenes.
    assert "- issue: #39 t" in blocked
    # The blocked scene carries the actual role (Issue #82: both
    # opened-PR states are review states, so always `review`) and the
    # completed review rounds (review round 2, PR #42).
    assert "- role: review" in blocked
    assert "- review/fix round: 2" in blocked


def test_wait_for_delivery_review_failure_finishes_progress_comment_with_blocked_scene(
    monkeypatch,
):
    """A review that cannot run is a terminal failure: the Issue is
    marked `ai-blocked` AND the tracked progress comment becomes the
    blocked scene with the next-step reason (Issue #18) — not only the
    blocked milestone."""
    pr_url = "https://github.com/owner/repo/pull/46"
    api_calls = []
    posted = []
    # The review scan: the Issue is awaiting review, and the
    # comment history carries no trusted scene, so the review
    # fails fast. The trusted review-round comments still count
    # for the blocked scene's round field (review round 2,
    # PR #42).
    _wait_delivery_fake_gh(
        monkeypatch, pr_state="OPEN",
        labels=[{"name": "ai-pr-opened"}],
        comments=_review_round_comments(),
        fail_progress=lambda command: False,
        api_calls=api_calls, posted=posted,
    )
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    runner.wait_for_delivery(
        pr_url, {"number": 39, "title": "t", "body": ""},
        {"repo_dir": Path("/srv/repo")}, "owner/repo",
    )
    posted_bodies = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted_bodies)
    assert any(
        "independent review" in body and "failed" in body
        for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in last_body
    assert "independent review" in last_body
    assert "next step:" in last_body
    assert "<!-- orbi:run=a1b2c3d4 -->" in last_body
    # The blocked scene carries the actual role (the failure happened
    # during the independent review) and the completed review rounds
    # (review round 2, PR #42) — not the hardcoded fix/0.
    assert "- role: review" in last_body
    assert "- review/fix round: 2" in last_body
    # No second progress comment was created.
    assert not any(
        "**Orbi progress**" in body for body in posted_bodies
    )


# --- Issue #79: the blocked-scene progress publishing in the wait loop is
# --- bypass (a 404 never escapes the loop, the terminal bookkeeping
# --- completes and the slot is released) -------------------------------------


def _wait_delivery_fake_gh(monkeypatch, *, pr_state, labels, comments,
                           fail_progress, api_calls, posted):
    """Fake gh for `wait_for_delivery`: fixed PR state/labels/comments,
    and a progress API that raises when `fail_progress` matches.
    `api_calls` records every attempt (including the 404'd ones);
    `posted` records only the bodies that were actually posted."""
    existing = {
        "id": 77,
        "body": (
            "<!-- orbi:run=a1b2c3d4 -->\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": pr_state})
        if command[:2] == ["gh", "issue"]:
            if command[-1] == "labels":
                return json.dumps({"labels": labels})
            return json.dumps({"comments": comments})
        api_calls.append(command)
        if fail_progress(command):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Not Found (HTTP 404)",
            )
        if "--method" not in command:
            return json.dumps([existing])
        method = command[command.index("--method") + 1]
        if method == "POST":
            body = command[command.index("--field") + 1]
            posted.append(body[len("body="):])
            return json.dumps({"id": 78, "body": body[len("body="):],
                               "url": "https://x/78"})
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)


def _review_round_comments():
    return [
        {
            "body": (
                "<!-- orbi:run=a1b2c3d4 -->\n"
                "Orbi review round 1 for PR #46: clean"
            ),
            "authorAssociation": "OWNER",
        },
        {
            "body": (
                "<!-- orbi:run=a1b2c3d4 -->\n"
                "Orbi review round 2 for PR #46: findings"
            ),
            "authorAssociation": "OWNER",
        },
    ]


def _blocked_progress_failures(command) -> bool:
    """404 the blocked-scene progress traffic: the blocked milestone
    POST and the blocked-scene finish PATCH."""
    return (
        command[:2] == ["gh", "api"]
        and "--method" in command
        and (
            ("POST" in command
             and "Orbi: blocked" in command[-1])
            or "PATCH" in command
        )
    )


def test_wait_for_delivery_closed_unmerged_progress_failure_still_releases(
    monkeypatch, caplog,
):
    """Issue #79: the PR is closed without a merge and the blocked-scene
    progress publishing (milestone, blocked-scene finish) 404s. The
    terminal bookkeeping must still complete — the Issue is marked
    `ai-blocked` with the failure comment — and the loop RETURNS (the
    slot is released); the progress failure is logged as
    `progress_publish_failed` only, never re-raised."""
    api_calls = []
    posted = []
    _wait_delivery_fake_gh(
        monkeypatch, pr_state="CLOSED",
        labels=[{"name": "ai-fix-needed"}],
        comments=_review_round_comments(),
        fail_progress=_blocked_progress_failures, api_calls=api_calls,
        posted=posted,
    )
    edits = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda number, **kwargs: edits.append(kwargs),
    )
    comments = []
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda number, **kwargs: comments.append(kwargs),
    )
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("ERROR")

    # No exception: the loop completed the terminal failure and the
    # slot is released by the caller.
    runner.wait_for_delivery(
        "https://github.com/owner/repo/pull/46",
        {"number": 39, "title": "t", "body": ""}, {}, "owner/repo",
    )

    # The `ai-blocked` transition (plus the leftover-label cleanup)
    # completed even though the progress publishing 404'd...
    assert {
        "repo": "owner/repo", "add": "ai-blocked",
        "remove": "ai-pr-opened",
    } in edits
    assert {"repo": "owner/repo", "remove": "ai-fix-needed"} in edits
    # ...the failure comment was posted...
    assert any(
        "closed without a merge" in kwargs.get("body", "")
        for kwargs in comments
    )
    # ...and the progress failures were logged as bypass (the blocked
    # milestone and the blocked-scene finish were both attempted).
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text
    assert all(
        "role=review" in line
        for line in caplog.text.splitlines()
        if "progress_publish_failed" in line
    ), caplog.text
    # Nothing was posted (the fake 404s the blocked milestone POST)...
    assert not any("Orbi: blocked" in body for body in posted)
    # ...but the blocked milestone was attempted...
    attempted_milestones = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and "--method" in command
        and "POST" in command
        and "Orbi: blocked" in command[-1]
    ]
    assert attempted_milestones, "the blocked milestone was not attempted"
    # ...and the blocked-scene finish was still attempted (independent
    # bypass step).
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the blocked-scene finish was not attempted"


def test_wait_for_delivery_review_failure_progress_failure_still_releases(
    monkeypatch, caplog,
):
    """Issue #79: the independent review cannot run (no trusted scene)
    and the blocked-scene progress publishing 404s. The terminal
    bookkeeping must still complete — the Issue is marked `ai-blocked`
    ALONE with the failure comment — and the loop RETURNS (the slot is
    released); the progress failure is logged as
    `progress_publish_failed` only, never re-raised."""
    api_calls = []
    posted = []
    _wait_delivery_fake_gh(
        monkeypatch, pr_state="OPEN",
        labels=[{"name": "ai-pr-opened"}],
        comments=_review_round_comments(),
        fail_progress=_blocked_progress_failures, api_calls=api_calls,
        posted=posted,
    )
    edits = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda number, **kwargs: edits.append(kwargs),
    )
    comments = []
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda number, **kwargs: comments.append(kwargs),
    )
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "a1b2c3d4")
    caplog.set_level("ERROR")

    # No exception: the loop completed the terminal failure and the
    # slot is released by the caller.
    runner.wait_for_delivery(
        "https://github.com/owner/repo/pull/46",
        {"number": 39, "title": "t", "body": ""},
        {"repo_dir": Path("/srv/repo")}, "owner/repo",
    )

    # The `ai-blocked` transition completed even though the progress
    # publishing 404'd...
    assert {
        "repo": "owner/repo", "add": "ai-blocked",
        "remove": "ai-pr-opened",
    } in edits
    # ...the failure comment was posted...
    assert any(
        "independent review" in kwargs.get("body", "")
        and "failed" in kwargs.get("body", "")
        for kwargs in comments
    )
    # ...and the progress failures were logged as bypass (the blocked
    # milestone and the blocked-scene finish were both attempted).
    assert caplog.text.count("progress_publish_failed") >= 2, caplog.text
    assert all(
        "role=review" in line
        for line in caplog.text.splitlines()
        if "progress_publish_failed" in line
    ), caplog.text
    # The blocked-scene finish was still attempted (independent bypass
    # step).
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the blocked-scene finish was not attempted"
