"""Tests for the automatic GitHub progress comment wiring (Issue #18).

`process_issue` must create exactly one progress comment per run (hidden
run marker), PATCH it on progress changes and at most every 30 seconds,
post short milestone comments for the key events, and end with either the
final delivery summary or the blocked scene — in the same comment.
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner
import progress


def make_fake_gh(monkeypatch, comments=None, in_progress=None):
    """Answer every gh call; return (calls, posted_bodies)."""
    calls = []
    posted = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:2] != ["gh", "api"]:
            if command[:3] == ["gh", "issue", "list"]:
                return json.dumps(in_progress or [])
            return ""
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

    monkeypatch.setattr(runner, "run_command", fake_run_command)
    return calls, posted


def make_config(tmp_path):
    return {
        "repo_dir": tmp_path,
        "prompt": tmp_path / "prompt.md",
        "base_branch": "main",
    }


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


def make_issue():
    return {"number": 18, "title": "Publish progress", "body": "body"}


def patch_process_deps(monkeypatch, tmp_path, *, run_pi_side_effect=None):
    monkeypatch.setattr(runner, "edit_issue", Mock())
    monkeypatch.setattr(runner, "freeze_base",
                        lambda repo_dir, base_branch: "abc123def456")
    monkeypatch.setattr(runner, "new_run_id", lambda: "abc123")

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
            "last": "bash pytest tests/",
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


def test_process_issue_creates_progress_comment_with_marker(monkeypatch, tmp_path):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=abc123 -->")
    ]
    assert len(progress_posts) == 1, (
        f"expected exactly one progress comment, got: {posted}"
    )
    body = progress_posts[0]
    assert "- issue: #18" in body
    assert "- role: implement" in body
    assert "- branch: muyan-pilot/xqliu-muyan-pilot-issue-18-abc123" in body
    assert "- PR: -" in body


def test_process_issue_posts_started_and_pr_opened_milestones(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if body.startswith(progress.MILESTONE_PREFIX)
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
        and command[2] == "repos/xqliu/muyan-pilot/issues/18/comments/77"
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
    assert "<!-- muyan-pilot:run=abc123 -->" in last_body


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
        if body.startswith(progress.MILESTONE_PREFIX)
    ]
    assert any("Muyan Pilot: blocked" in body for body in milestones)
    final_patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/18/comments/77"
        and "PATCH" in command
    ]
    assert final_patches, "no PATCH of the progress comment"
    last_body = final_patches[-1][
        final_patches[-1].index("--field") + 1
    ][len("body="):]
    assert "Muyan Pilot blocked" in last_body
    assert "pi exploded" in last_body
    assert "<!-- muyan-pilot:run=abc123 -->" in last_body


def test_process_issue_resumes_existing_progress_comment_after_restart(
    monkeypatch, tmp_path,
):
    existing = {
        "id": 77,
        "body": "<!-- muyan-pilot:run=abc123 -->stale body",
    }
    calls, posted = make_fake_gh(monkeypatch, comments=[existing])
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    # No new progress comment is posted: the restarted run reuses id 77.
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=abc123 -->")
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/18/comments/77"
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
        if body.startswith(progress.MILESTONE_PREFIX)
    ]
    assert "Muyan Pilot: plan ready" in milestones


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
        if body.startswith(progress.MILESTONE_PREFIX)
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
        if body.startswith(progress.MILESTONE_PREFIX)
    ]
    assert any("Muyan Pilot: tests failed" in body for body in milestones)


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


def test_delivery_head_advanced_fails_fast_when_git_fails(
    monkeypatch, tmp_path,
):
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


def test_process_issue_posts_fix_pushed_milestone_when_branch_advanced(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "delivery_head_advanced", lambda *args: True)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if body.startswith(progress.MILESTONE_PREFIX)
    ]
    assert any("Muyan Pilot: fix pushed" in body for body in milestones)


def test_process_issue_skips_fix_pushed_milestone_without_new_commits(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "delivery_head_advanced", lambda *args: False)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    milestones = [
        body for body in posted
        if body.startswith(progress.MILESTONE_PREFIX)
    ]
    assert not any("fix pushed" in body for body in milestones)


def test_latest_run_id_returns_newest_worktree_run_id(tmp_path):
    old = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-18-run1"
    new = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-18-run2"
    other = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-ceo-issue-18-run9"
    other_issue = tmp_path / ".worktrees" / "muyan-pilot-xqliu-muyan-pilot-issue-17-run3"
    for path in (old, new, other, other_issue):
        path.mkdir(parents=True)
    old_time = old.stat().st_mtime
    os.utime(old, (old_time - 100, old_time - 100))
    assert runner.latest_run_id(
        tmp_path, "xqliu/muyan-pilot", 18,
    ) == "run2"


def test_latest_run_id_returns_none_without_worktree(tmp_path):
    assert runner.latest_run_id(tmp_path, "xqliu/muyan-pilot", 18) is None


def test_process_issue_resumes_existing_run_and_same_progress_comment(
    monkeypatch, tmp_path,
):
    """A killed runner leaves the worktree behind; the restarted claim
    must reuse the run id and keep updating the same progress comment."""
    existing = {
        "id": 77,
        "body": "<!-- muyan-pilot:run=oldrun1 -->stale blocked scene",
    }
    calls, posted = make_fake_gh(
        monkeypatch, comments=[existing],
        in_progress=[{"number": 18, "title": "t", "url": "u"}],
    )
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "new_run_id", lambda: "freshrun")
    (tmp_path / ".worktrees"
     / "muyan-pilot-xqliu-muyan-pilot-issue-18-oldrun1").mkdir(parents=True)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    # The resumed run reuses the old run id: no second progress comment.
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=")
    ]
    assert progress_posts == [], (
        f"restart must not create a second progress comment: {posted}"
    )
    patches = [
        command for command in calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/xqliu/muyan-pilot/issues/18/comments/77"
        and "PATCH" in command
    ]
    assert patches, "existing progress comment was not updated"
    # The resumed run's milestones carry the resumed run id.
    started = [b for b in posted if "Muyan Pilot: started" in b]
    assert started and "run_id=oldrun1" in started[0]


def test_process_issue_starts_fresh_run_when_label_without_worktree(
    monkeypatch, tmp_path,
):
    # The ai-in-progress label survives but the worktree was cleaned up:
    # nothing to resume, so a fresh run id is used.
    calls, posted = make_fake_gh(
        monkeypatch,
        in_progress=[{"number": 18, "title": "t", "url": "u"}],
    )
    patch_process_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "new_run_id", lambda: "freshrun")
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=freshrun -->")
    ]
    assert len(progress_posts) == 1


def test_process_issue_starts_fresh_run_without_leftover_worktree(
    monkeypatch, tmp_path,
):
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    runner.process_issue(make_issue(), make_config(tmp_path),
                         "xqliu/muyan-pilot")
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=abc123 -->")
    ]
    assert len(progress_posts) == 1


def test_process_issue_passes_role_to_pi_and_progress_comment(
    monkeypatch, tmp_path,
):
    """reviewer/fixer sessions (Issue #34) stay observable: the role is
    carried into the stream_pi context and the progress comment."""
    calls, posted = make_fake_gh(monkeypatch)
    patch_process_deps(monkeypatch, tmp_path)
    seen = {}

    def fake_run_pi(issue, worktree, config, source_repo, **kwargs):
        seen.update(kwargs)
        return "done"

    monkeypatch.setattr(runner, "run_pi", fake_run_pi)
    runner.process_issue(
        make_issue(), make_config(tmp_path), "xqliu/muyan-pilot",
        role="review",
    )
    assert seen["role"] == "review"
    progress_posts = [
        body for body in posted
        if body.startswith("<!-- muyan-pilot:run=abc123 -->")
    ]
    assert progress_posts
    assert "- role: review" in progress_posts[0]
