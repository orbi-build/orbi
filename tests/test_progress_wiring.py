"""Tests for the automatic GitHub progress comment wiring (Issue #18).

`process_issue`, `resume_delivery` and `review_and_merge_if_clean` must
keep exactly one live progress comment per run (hidden run marker),
PATCH it live while any Pi session runs and at most every 30 seconds,
post short milestone comments for the key events, and end with either
the final delivery summary or the blocked scene — in the same comment.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner
import progress


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

    def fake_create_worktree(*args):
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
    monkeypatch.setattr(runner, "verify_pr",
                        lambda *args, **kwargs:
                        "https://github.com/xqliu/muyan-pilot/pull/40")


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
        "run_id": "a1b2c3d4", "issue": 18, "role": "implement",
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
                         "xqliu/muyan-pilot")
    progress_posts = [
        body for body in posted
        if "**Muyan Pilot progress**" in body
    ]
    assert len(progress_posts) == 1, (
        f"expected exactly one progress comment, got: {posted}"
    )
    body = progress_posts[0]
    assert "- issue: #18" in body
    assert "- role: implement" in body
    assert "- branch: muyan-pilot/xqliu-muyan-pilot-issue-18-a1b2c3d4" in body
    assert "- PR: -" in body


def test_process_issue_passes_issue_number_to_verify_pr(
    monkeypatch, tmp_path,
):
    """The fresh path verifies the `Fixes #<issue>` keyword against the
    source Issue number (Issue #53)."""
    make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    verify_calls = []

    def fake_verify_pr(*args, **kwargs):
        verify_calls.append((args, kwargs))
        return "https://github.com/xqliu/muyan-pilot/pull/40"

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    assert len(verify_calls) == 1
    args, kwargs = verify_calls[0]
    assert kwargs.get("issue") == 18, (
        f"verify_pr must verify the Fixes keyword against the source "
        f"Issue number, got args={args} kwargs={kwargs}"
    )


def test_process_issue_posts_started_and_pr_opened_milestones(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: started" in body for body in milestones)
    assert any(
        "Muyan Pilot: PR opened" in body
        and "https://github.com/xqliu/muyan-pilot/pull/40" in body
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
                         "xqliu/muyan-pilot")
    # The final PATCH of comment 77 must carry the delivery summary.
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "--method" in command
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot delivered" in last_body
    assert "https://github.com/xqliu/muyan-pilot/pull/40" in last_body
    assert "156 passed" in last_body
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in last_body


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
    with pytest.raises(subprocess.CalledProcessError):
        runner.process_issue(make_issue(), make_config(tmp_path),
                             "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: blocked" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot blocked" in last_body
    assert "pi exploded" in last_body
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in last_body


def test_process_issue_resumes_existing_progress_comment_after_restart(
    monkeypatch, tmp_path,
):
    existing = {
        "id": 77,
        "body": (
            "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
            "**Muyan Pilot progress**\n\nstale implementer state"
        ),
    }
    calls, posted = make_fake_gh(monkeypatch, comments=[existing])
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    # No new progress comment is posted: the restarted run reuses id 77.
    progress_posts = [
        body for body in posted
        if "**Muyan Pilot progress**" in body
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "existing progress comment was not updated"
    # And the final summary still lands in the same comment.
    last_body = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Muyan Pilot delivered" in last_body


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
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any(
        "Muyan Pilot: plan ready" in body for body in milestones
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
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: tests passed" in body for body in milestones)


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
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: tests failed" in body for body in milestones)


def test_process_issue_never_posts_fix_pushed_on_a_fresh_claim(
    monkeypatch, tmp_path,
):
    # The implementer always commits the delivery on top of the frozen
    # base, so the head always advanced: a fresh claim must not turn
    # that into a `fix pushed` milestone (the PR opened milestone
    # announces the delivery; `fix pushed` is the fixer's milestone in
    # resume_delivery).
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "delivery_head_advanced", lambda *args: True)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
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
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "--method" in command
        and "PATCH" in command
    )


def _delivery_record_of(command) -> bool:
    """The three delivery-record calls after the PR is verified and
    labeled: the opened-PR scene comment, the `PR opened` milestone and
    the delivered finish() PATCH (all of them are progress publishing;
    none of them may fail the delivery, Issue #60)."""
    if command[:2] == ["gh", "issue"] and "comment" in command:
        return "Muyan Pilot opened PR:" in command[-1]
    if command[:2] == ["gh", "api"]:
        if "--method" in command and "POST" in command:
            return "Muyan Pilot: PR opened" in command[-1]
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
                                  "xqliu/muyan-pilot")

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    # The state transition happened (the Issue awaits review)...
    assert edits == [{
        "repo": "xqliu/muyan-pilot", "add": "ai-in-progress"},
        {"repo": "xqliu/muyan-pilot", "add": "ai-pr-opened",
         "remove": "ai-in-progress"},
    ]
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The failure is logged like the in-stream callback...
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "issue=xqliu/muyan-pilot#18" in line
        and "role=implement" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # ...and the delivery summary was attempted (the 404'd PATCH).
    delivered_patches = [c for c in calls if _progress_patch_of(c)]
    assert delivered_patches, "the delivered finish was not attempted"
    last_body = delivered_patches[-1][
        delivered_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot delivered" in last_body


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
            and "Muyan Pilot: PR opened" in command[-1]
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/muyan-pilot")

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The delivered finish still ran (independent step)...
    delivered_patches = [c for c in calls if _progress_patch_of(c)]
    assert delivered_patches, "the delivered finish was not attempted"
    last_body = delivered_patches[-1][
        delivered_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot delivered" in last_body
    # ...and the failed milestone was not posted.
    assert not any("Muyan Pilot: PR opened" in body for body in posted)


def test_process_issue_scene_comment_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """The opened-PR scene comment POST failing after the label
    transition is the same contract: logged, not fatal; the milestone
    and the delivered finish still run (independent publishing steps)."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "issue"]
            and "comment" in command
            and "Muyan Pilot opened PR:" in command[-1]
        ),
    )
    patch_process_deps(monkeypatch, tmp_path)
    edits = []
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    caplog.set_level("ERROR")

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/muyan-pilot")

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The milestone and the delivered finish still ran.
    assert any("Muyan Pilot: PR opened" in body for body in posted)
    assert any(_progress_patch_of(command) for command in calls)
    last_patch = [c for c in calls if _progress_patch_of(c)][-1]
    last_body = last_patch[last_patch.index("--field") + 1][len("body="):]
    assert "Muyan Pilot delivered" in last_body


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
                         "xqliu/muyan-pilot")

    ends = [line for line in caplog.text.splitlines()
            if " run_end " in line]
    assert len(ends) == 1
    assert "result=pr_opened" in ends[0]
    assert "pr=https://github.com/xqliu/muyan-pilot/pull/40" in ends[0]


def test_process_issue_publishing_failure_is_never_blocked_scene(
    monkeypatch, tmp_path,
):
    """The failure report (blocked milestone, blocked progress scene,
    `Muyan Pilot failed` comment) must NOT run for a progress failure
    after the PR open: the delivery succeeded."""
    calls, posted = make_failing_gh(monkeypatch, _delivery_record_of)
    patch_process_deps(monkeypatch, tmp_path)

    pr_url = runner.process_issue(make_issue(), make_config(tmp_path),
                                  "xqliu/muyan-pilot")

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    # No blocked milestone...
    assert not any("Muyan Pilot: blocked" in body for body in posted)
    # ...no `Muyan Pilot failed` comment...
    comment_bodies = [
        command[-1] for command in calls
        if command[:2] == ["gh", "issue"] and "comment" in command
    ]
    assert not any("Muyan Pilot failed" in body
                   for body in comment_bodies)
    # ...and no blocked progress scene.
    assert not any(
        "Muyan Pilot blocked" in body
        for command in calls if _progress_patch_of(command)
        for body in [command[command.index("--field") + 1][len("body="):]]
    )


# --- resume_delivery wiring (the fixer stays observable) ----------------------


def scene_for_resume():
    return {
        "run_id": "a1b2c3d4",
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": "https://github.com/xqliu/muyan-pilot/pull/40",
    }


def patch_resume_deps(monkeypatch, tmp_path, *, run_pi_side_effect=None):
    worktree = runner.worktree_path(
        tmp_path, "xqliu/muyan-pilot", 18, "a1b2c3d4",
    )
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner, "verify_pr",
                        lambda *args, **kwargs:
                        "https://github.com/xqliu/muyan-pilot/pull/40")
    monkeypatch.setattr(runner, "merge_latest_base", lambda *args: True)
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    if run_pi_side_effect is not None:
        monkeypatch.setattr(runner, "run_pi",
                            Mock(side_effect=run_pi_side_effect))
    else:
        monkeypatch.setattr(runner, "run_pi", Mock(return_value="done"))


def test_resume_delivery_reuses_existing_progress_comment(monkeypatch, tmp_path):
    """The fixer keeps updating the SAME progress comment created by the
    implementer: ensure finds it by run marker + progress header (no
    second progress comment)."""
    existing = {
        "id": 77,
        "body": (
            "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
            "**Muyan Pilot progress**\n\nstale implementer state"
        ),
    }
    calls, posted = make_fake_gh(monkeypatch, comments=[existing])
    patch_resume_deps(monkeypatch, tmp_path)
    runner.resume_delivery(
        make_issue(), scene_for_resume(), make_config(tmp_path),
        "xqliu/muyan-pilot",
    )
    progress_posts = [
        body for body in posted
        if "**Muyan Pilot progress**" in body
    ]
    assert progress_posts == [], (
        f"resume must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "existing progress comment was not updated"
    # The fixer state is visible in the live comment (role=fix, the PR).
    first_body = patches[0][patches[0].index("--field") + 1][len("body="):]
    assert "- role: fix" in first_body
    assert "- PR: https://github.com/xqliu/muyan-pilot/pull/40" in first_body


def test_resume_delivery_posts_fix_pushed_milestone_and_finishes(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_resume_deps(monkeypatch, tmp_path)
    runner.resume_delivery(
        make_issue(), scene_for_resume(), make_config(tmp_path),
        "xqliu/muyan-pilot",
    )
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: fix pushed" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot fix pushed" in last_body
    assert "awaiting review again" in last_body


def test_resume_delivery_failure_updates_progress_comment_with_blocked_scene(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_resume_deps(
        monkeypatch, tmp_path,
        run_pi_side_effect=subprocess.CalledProcessError(
            3, ["pi"], stderr="fixer exploded",
        ),
    )
    with pytest.raises(subprocess.CalledProcessError):
        runner.resume_delivery(
            make_issue(), scene_for_resume(), make_config(tmp_path),
            "xqliu/muyan-pilot",
        )
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: blocked" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot blocked" in last_body
    assert "fixer exploded" in last_body


# --- Issue #60: the fixer path keeps the same contract after the fix is
# --- verified (no ai-blocked, no re-raise; progress_publish_failed only) ----


def patch_resume_deps_live(monkeypatch, tmp_path):
    """Like `patch_resume_deps`, but `comment_issue` and `edit_issue` are
    NOT mocked: the delivery-record calls (fixed-PR scene comment,
    `fix pushed` milestone, delivered finish) must reach the fake gh so
    they can fail there (Issue #60). `edit_issue` is recorded."""
    worktree = runner.worktree_path(
        tmp_path, "xqliu/muyan-pilot", 18, "a1b2c3d4",
    )
    worktree.mkdir(parents=True, exist_ok=True)
    edits = []
    monkeypatch.setattr(runner, "verify_pr",
                        lambda *args, **kwargs:
                        "https://github.com/xqliu/muyan-pilot/pull/40")
    monkeypatch.setattr(runner, "merge_latest_base", lambda *args: True)
    monkeypatch.setattr(runner, "edit_issue", lambda number, **kwargs:
                        edits.append(kwargs))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    monkeypatch.setattr(runner, "run_pi", Mock(return_value="done"))
    return edits


def _fix_pushed_patch_of(command) -> bool:
    return (
        command[:2] == ["gh", "api"]
        # GitHub update route (Issue #58): no issue number.
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "--method" in command
        and "PATCH" in command
    )


def test_resume_delivery_fix_pushed_finish_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """The fix is verified and the Issue is back in `ai-pr-opened`
    (leaving `ai-fix-needed`) before the `fix pushed` finish() PATCH.
    That PATCH failing must not fail the delivery: no re-raise, no
    `ai-blocked`, the error is logged (`progress_publish_failed`) and
    the verified PR URL is returned so the run continues into the
    review/merge wait loop."""
    calls, posted = make_failing_gh(monkeypatch, _fix_pushed_patch_of)
    edits = patch_resume_deps_live(monkeypatch, tmp_path)
    caplog.set_level("ERROR")

    pr_url = runner.resume_delivery(
        make_issue(), scene_for_resume(), make_config(tmp_path),
        "xqliu/muyan-pilot",
    )

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    # The state transition happened (the Issue awaits review again)...
    assert edits == [{
        "repo": "xqliu/muyan-pilot", "add": "ai-pr-opened",
        "remove": "ai-fix-needed",
    }]
    # ...and the delivery was NOT marked blocked.
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    # The failure is logged like the in-stream callback...
    assert any(
        "progress_publish_failed" in line
        and "run=a1b2c3d4" in line
        and "issue=xqliu/muyan-pilot#18" in line
        and "role=fix" in line
        for line in caplog.text.splitlines()
    ), caplog.text
    # ...and the fix summary was attempted (the 404'd PATCH).
    fix_patches = [c for c in calls if _fix_pushed_patch_of(c)]
    assert fix_patches, "the fix pushed finish was not attempted"
    last_body = fix_patches[-1][
        fix_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot fix pushed" in last_body


def test_resume_delivery_scene_comment_failure_does_not_fail_delivery(
    monkeypatch, tmp_path, caplog,
):
    """The fixed-PR scene comment POST failing after the state transition
    is the same contract: logged, not fatal; the `fix pushed` milestone
    and the delivered finish still run (independent publishing steps)."""
    calls, posted = make_failing_gh(
        monkeypatch,
        lambda command: (
            command[:2] == ["gh", "issue"]
            and "comment" in command
            and "Muyan Pilot fixed PR:" in command[-1]
        ),
    )
    edits = patch_resume_deps_live(monkeypatch, tmp_path)
    caplog.set_level("ERROR")

    pr_url = runner.resume_delivery(
        make_issue(), scene_for_resume(), make_config(tmp_path),
        "xqliu/muyan-pilot",
    )

    assert pr_url == "https://github.com/xqliu/muyan-pilot/pull/40"
    assert not any(kwargs.get("add") == "ai-blocked" for kwargs in edits)
    assert any("progress_publish_failed" in line
               for line in caplog.text.splitlines()), caplog.text
    # The milestone and the delivered finish still ran.
    assert any("Muyan Pilot: fix pushed" in body for body in posted)
    fix_patches = [c for c in calls if _fix_pushed_patch_of(c)]
    assert fix_patches, "the fix pushed finish was not attempted"
    last_body = fix_patches[-1][
        fix_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot fix pushed" in last_body


# --- review_and_merge_if_clean wiring (the reviewer stays observable) ---------


def test_review_and_merge_posts_review_findings_milestone(monkeypatch, tmp_path):
    calls, posted = make_fake_gh(monkeypatch)
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: {
        "number": 4, "url": "https://github.com/xqliu/muyan-pilot/pull/40",
        "base_ref": "main", "base_oid": "b1",
        "head_ref": "h", "head_oid": "h1",
    })
    monkeypatch.setattr(runner, "run_review", lambda *a, **k:
                        "REVIEW_VERDICT " + json.dumps({
                            "verdict": "findings", "blockers": 1,
                            "majors": 0, "minors": 0,
                            "findings": [{"level": "Blocker",
                                          "location": "a.py:1",
                                          "note": "x"}],
                        }))
    monkeypatch.setattr(runner, "merge_gate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no merge")))
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "comment_issue", Mock())
    monkeypatch.setattr(runner, "comment_pr", Mock())
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main",
        {"repo_dir": tmp_path, "base_branch": "main", "base_sha": "b1",
         "run_id": "a1b2c3d4"},
        "xqliu/muyan-pilot", 18,
    )
    assert merged is False
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: review findings" in body for body in milestones)
    assert any("round 1" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot review findings" in last_body
    assert "- review/fix round: 1" in last_body


def test_review_and_merge_posts_merged_milestone_and_final_summary(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: {
        "number": 4, "url": "https://github.com/xqliu/muyan-pilot/pull/40",
        "base_ref": "main", "base_oid": "b1",
        "head_ref": "h", "head_oid": "h1",
    })
    monkeypatch.setattr(runner, "run_review", lambda *a, **k:
                        "REVIEW_VERDICT " + json.dumps({
                            "verdict": "pass", "blockers": 0, "majors": 0,
                            "minors": 0, "findings": [],
                        }))
    monkeypatch.setattr(runner, "merge_gate", lambda *a, **k: {
        "number": 4, "url": "https://github.com/xqliu/muyan-pilot/pull/40",
        "base_ref": "main", "base_oid": "b1",
        "head_ref": "h", "head_oid": "h1", "merged": True,
    })
    monkeypatch.setattr(runner, "confirm_merged",
                        lambda *a, **k: {
                            "state": "MERGED", "merge_commit": "m1",
                        })
    monkeypatch.setattr(runner, "sync_base_checkout", Mock())
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "comment_issue", Mock())
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main",
        {"repo_dir": tmp_path, "base_branch": "main", "base_sha": "b1",
         "run_id": "a1b2c3d4"},
        "xqliu/muyan-pilot", 18,
    )
    assert merged is True
    milestones = [
        body for body in posted
        if any(line.startswith(progress.MILESTONE_PREFIX)
               for line in body.splitlines())
    ]
    assert any("Muyan Pilot: merged" in body for body in milestones)
    assert any("merge_commit=m1" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/comments/77"
        and "PATCH" in command
    ]
    assert final_patches
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot delivered" in last_body
    assert "merge_commit=m1" in last_body


# --- wait_for_delivery terminal failures stay observable ----------------------


def test_wait_for_delivery_closed_unmerged_posts_blocked_milestone(
    monkeypatch,
):
    pr_url = "https://github.com/owner/repo/pull/46"
    api_calls = []
    existing = {
        "id": 77,
        "body": (
            "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
            "**Muyan Pilot progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "CLOSED"})
        if command[:2] == ["gh", "issue"]:
            # The blocked scene derives the role from the delivery label
            # and the round from the trusted review-round comments
            # (review round 2, PR #42).
            if command[-1] == "labels":
                return json.dumps({"labels": [{"name": "ai-fix-needed"}]})
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- muyan-pilot:run=a1b2c3d4 -->\n"
                        "Muyan Pilot review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- muyan-pilot:run=a1b2c3d4 -->\n"
                        "Muyan Pilot review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        api_calls.append(command)
        if "--method" not in command:
            # The run's live progress comment exists.
            return json.dumps([existing])
        method = command[command.index("--method") + 1]
        if method == "POST":
            body = command[command.index("--field") + 1]
            return json.dumps({"id": 78, "body": body[len("body="):],
                               "url": "https://x/78"})
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
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
    assert any("Muyan Pilot: blocked" in body for body in posted_bodies)
    assert any(
        ("closed without a merge" in body for body in posted_bodies),
    )
    # ...and the tracked progress comment becomes the blocked scene
    # (Issue #18): the same terminal body the other failure paths write.
    assert not any(
        "**Muyan Pilot progress**" in body for body in posted_bodies
    )
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    blocked = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Muyan Pilot blocked" in blocked
    assert "closed without a merge" in blocked
    assert "next step:" in blocked
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in blocked
    # The blocked scene carries the actual role (delivery label was
    # ai-fix-needed) and the completed review rounds (review round 2,
    # PR #42) — not the hardcoded fix/0.
    assert "- role: fix" in blocked
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
    existing = {
        "id": 77,
        "body": (
            "<!-- muyan-pilot:run=a1b2c3d4 -->\n\n"
            "**Muyan Pilot progress**\n\nawaiting review"
        ),
    }

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                return json.dumps([existing])
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[:2] == ["gh", "issue"]:
            # The review scan: the Issue is awaiting review, and the
            # comment history carries no trusted scene, so the review
            # fails fast. The trusted review-round comments still count
            # for the blocked scene's round field (review round 2,
            # PR #42).
            if command[-1] == "labels":
                return json.dumps({"labels": [
                    {"name": "ai-pr-opened"},
                ]})
            return json.dumps({"comments": [
                {
                    "body": (
                        "<!-- muyan-pilot:run=a1b2c3d4 -->\n"
                        "Muyan Pilot review round 1 for PR #46: clean"
                    ),
                    "authorAssociation": "OWNER",
                },
                {
                    "body": (
                        "<!-- muyan-pilot:run=a1b2c3d4 -->\n"
                        "Muyan Pilot review round 2 for PR #46: findings"
                    ),
                    "authorAssociation": "OWNER",
                },
            ]})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    # The fake rejects anything that is not pr/api/issue traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
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
    assert any("Muyan Pilot: blocked" in body for body in posted_bodies)
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
    assert "Muyan Pilot blocked" in last_body
    assert "independent review" in last_body
    assert "next step:" in last_body
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in last_body
    # The blocked scene carries the actual role (the failure happened
    # during the independent review) and the completed review rounds
    # (review round 2, PR #42) — not the hardcoded fix/0.
    assert "- role: review" in last_body
    assert "- review/fix round: 2" in last_body
    # No second progress comment was created.
    assert not any(
        "**Muyan Pilot progress**" in body for body in posted_bodies
    )
