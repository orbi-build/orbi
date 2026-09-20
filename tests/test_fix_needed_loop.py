"""Issue #50: keep AI-recoverable failures in the automatic fix loop.

Recoverable failures of an existing run/PR (Pi execution failure, model
wait, runner exception, missing/malformed verdict, missing worktree,
unpushed local commit, ...) must NOT leave the automatic queue: the
Issue is labeled `ai-fix-needed` (not `ai-blocked`) with a failure
comment carrying the full scene (run_id, PR, branch, worktree, session,
phase, last activity, concrete error), and the next timer resumes the
same run, branch, worktree and PR. `ai-blocked` is reserved for
external preconditions the AI cannot safely judge or fix; every blocked
comment states the explicit reason why automatic recovery is impossible.
"""
import json
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner
import orbi.runner_health as runner_health
from orbi import progress, scene
from seam import seam
import orbi.journal as journal

PR_URL = "https://github.com/owner/repo/pull/46"
RUN_ID = "a1b2c3d4"
MARKER = f"<!-- orbi:run={RUN_ID} -->"
WORKTREE = "/srv/repo/.worktrees/orbi-owner-repo-issue-39-a1b2c3d4"
BRANCH = "orbi/owner-repo-issue-39"


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)


def _scene_comments():
    """The trusted opened-PR scene comment (the recovery source)."""
    return [
        {
            "body": (
                f"{MARKER}\n"
                f"Orbi opened PR: {PR_URL} (base_branch=main "
                f"base_sha=abc123def456 run_id={RUN_ID})"
            ),
            "authorAssociation": "OWNER",
        },
    ]


def make_wait_failure_fake(monkeypatch, *, labels=("ai-pr-opened",),
                           progress_comments=None, scene=None):
    """Shared `run_command` fake for the `delivery_step` failure
    tests: one OPEN PR poll, the progress API (GET/POST/PATCH), the
    label read, the comment-history read (scene + review rounds).
    `review_and_merge_if_clean` is monkeypatched separately by the
    caller. Returns the captured api calls."""
    if progress_comments is None:
        progress_comments = [
            {
                "id": 77,
                "body": f"{MARKER}\n\n**Orbi progress**\n\nawaiting",
            },
        ]
    if scene is None:
        scene = _scene_comments()
    api_calls = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"]:
            return json.dumps({"state": "OPEN"})
        if command[:3] == ["git", "branch", "--show-current"]:
            # Issue #608: the delivery branch is read from the derived
            # worktree (stable naming here; the contributor's head for an
            # external takeover).
            return BRANCH
        if command[:2] == ["gh", "api"]:
            api_calls.append(command)
            if "--method" not in command:
                return json.dumps(progress_comments)
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[-1] == "comments":
            return json.dumps({"comments": scene})
        return json.dumps({"labels": [{"name": name} for name in labels]})

    monkeypatch.setattr(seam, "run_command", fake_run)
    return api_calls


def _issue():
    return {"number": 39, "title": "task", "body": ""}


def _config(tmp_path):
    return runner.RunnerConfig(repo_dir=tmp_path, base_branch="main")


# ---------------------------------------------------------------- classification


def test_is_unrecoverable_failure_true_only_for_explicit_error():
    assert runner.is_unrecoverable_failure(
        runner.UnrecoverableDeliveryError("human decision needed"),
    )


def test_is_unrecoverable_failure_true_for_rate_limit_exhaustion():
    # Issue #698: the exhausted 429 backoff budget is an external
    # provider-quota precondition the AI cannot fix. The recoverable
    # classification would resume the open-PR review with the persisted
    # counter already at the limit — one 429 exit per tick, forever:
    # exactly the unbounded loop the issue bans.
    assert runner.is_unrecoverable_failure(
        runner.RateLimitExhaustedError(
            "provider rate limit retries exhausted",
        ),
    )


@pytest.mark.parametrize("exc", [
    RuntimeError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    ),
    RuntimeError(
        "Pi session stayed idle for 15m after idle recovery "
        "(TERM/KILL of pre-idle descendants); Pi was killed (Issue #94)"
    ),
    subprocess.CalledProcessError(1, ["pi", "--print"]),
    subprocess.TimeoutExpired(["pi", "--print"], 600),
    ValueError("no REVIEW_VERDICT line in review output"),
    RuntimeError("worktree missing: /srv/repo/.worktrees/x"),
    RuntimeError(
        "PR head 18c78a2 is not local HEAD 18c78a2b; the verified "
        "commit was not pushed, push the reviewed commit and retry"
    ),
])
def test_is_unrecoverable_failure_false_for_recoverable_failures(exc):
    # Issue #50: every failure the AI can still diagnose, fix and
    # verify on the same run/PR stays in the automatic fix loop.
    assert not runner.is_unrecoverable_failure(exc)


# --------------------------------------------- snapshot placeholder (Issue #288)


def test_snapshot_or_placeholder_returns_the_watcher_state(tmp_path):
    """Issue #288: a readable session dir yields the real snapshot —
    the same state the failure comment's scene has always shown."""
    _write_session(tmp_path)
    snapshot = runner._snapshot_or_placeholder(
        tmp_path / ".pi-session", number=39,
    )
    assert snapshot["session_id"] == "sess-1"
    assert snapshot["session_file"] == str(
        tmp_path / ".pi-session" / "sess.jsonl",
    )


def test_snapshot_or_placeholder_returns_placeholder_without_session(
        tmp_path,
):
    """Issue #288: no session file yet (the Pi never started or the dir
    is gone) yields the placeholder scene, fresh per call — the shared
    constant is never handed out for mutation."""
    first = runner._snapshot_or_placeholder(
        tmp_path / ".pi-session", number=39,
    )
    assert first == {
        "session_id": None, "session_file": None,
        "phase": "starting", "last_activity": None,
        "action": None, "result": None,
    }
    first["phase"] = "mutated"
    assert runner._snapshot_or_placeholder(
        tmp_path / ".pi-session", number=39,
    )["phase"] == "starting"


def test_snapshot_or_placeholder_logs_a_failed_read(
        monkeypatch, caplog, tmp_path,
):
    """Issue #288: a failing snapshot read is best-effort observability —
    it is logged and degrades to the placeholder, never a second
    failure of the reporting path."""

    def failing_snapshot(*args, **kwargs):
        raise OSError("session file unreadable")

    monkeypatch.setattr(runner, "activity_snapshot", failing_snapshot)
    caplog.set_level("INFO")
    snapshot = runner._snapshot_or_placeholder(
        tmp_path / ".pi-session", number=39,
    )
    assert snapshot["session_id"] is None
    assert snapshot["phase"] == "starting"
    assert "activity scene failed" in caplog.text


# ------------------------------------------------- delivery_step: recoverable


@pytest.mark.parametrize("exc", [
    # Pi execution failure (hung model request / pi exit).
    RuntimeError(
        "Pi is stuck in model_wait with a frozen session for 10m: "
        "the model request is hung (the model service process is alive "
        "but the request never completes); Pi was killed (Issue #218)"
    ),
    subprocess.CalledProcessError(1, ["pi", "--print"]),
    # Missing/malformed verdict.
    ValueError("no REVIEW_VERDICT line in review output"),
    # Missing worktree.
    RuntimeError(f"worktree missing: {WORKTREE}"),
])
def test_delivery_step_recoverable_review_failure_stays_fix_needed(
        monkeypatch, caplog, tmp_path, exc,
):
    """Issue #50: a recoverable review failure (Pi execution failure,
    model wait, runner exception, malformed verdict, missing worktree)
    keeps the Issue in the automatic fix loop: `ai-fix-needed` (NOT
    `ai-blocked`), a failure comment with the full scene on Issue AND
    PR, the progress comment finished with the fix-needed scene, and
    the wait returns so the next timer resumes the same run, branch,
    worktree and PR."""
    # The worktree exists (created at claim time) so the review runs.
    (tmp_path / ".worktrees"
     / f"orbi-owner-repo-issue-39-{RUN_ID}").mkdir(parents=True)
    api_calls = make_wait_failure_fake(monkeypatch)
    edits = []
    issue_comments = []
    pr_comments = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_pr",
        lambda *args, **kwargs: pr_comments.append((args, kwargs)),
    )
    reviews = []

    def failing_review(*args, **kwargs):
        reviews.append((args, kwargs))
        raise exc

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    # The wait returns (the slot is released by the caller); the next
    # timer picks the ai-fix-needed Issue up on the same run/PR.
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")

    # One review attempt, then the fix-needed transition.
    assert len(reviews) == 1
    # The Issue is marked ai-fix-needed (removing ai-pr-opened) — and
    # never ai-blocked.
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed",
                 "remove": "ai-pr-opened"}),
    ]
    # The failure comment carries the run marker, the PR, the concrete
    # error and the full scene (run_id, branch, worktree, session,
    # phase, last activity).
    assert len(issue_comments) == 1
    body = issue_comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert MARKER in body
    assert PR_URL in body
    assert str(exc).split(" (Issue")[0] in body
    assert f"branch={BRANCH}" in body
    expected_worktree = (
        tmp_path / ".worktrees"
        / f"orbi-owner-repo-issue-39-{RUN_ID}"
    )
    assert f"worktree={expected_worktree}" in body
    assert "session=" in body
    assert "phase=" in body
    assert "last_activity=" in body
    # Issue #775: raw evidence segments are fenced; the empty stderr is
    # the placeholder inside its fence.
    assert "stderr_tail:\n```\n<empty>\n```" in body
    if isinstance(exc, subprocess.CalledProcessError):
        assert "exit_code=1" in body
    # The SAME failure comment is written to the PR (Issue #50: the
    # failure comment must be written to Issue/PR).
    assert len(pr_comments) == 1
    assert pr_comments[0][1]["body"] == body
    # The tracked progress comment is finished with the fix-needed
    # scene (not the blocked scene) ...
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    finished = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi fix needed" in finished
    assert "What Orbi will do next:" in finished
    assert "ai-blocked" not in finished
    # ... and the fix-needed milestone is posted (mobile notification).
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted)
    # The failure is logged with the run-scene marker.
    assert "delivery_review_failed" in caplog.text


def test_delivery_step_recoverable_failure_while_fix_needed_keeps_label(
        monkeypatch, tmp_path,
):
    """Issue #50 + #82: a recoverable failure while the Issue is ALREADY
    `ai-fix-needed` (awaiting the next review session) keeps the
    `ai-fix-needed` label (the opened-PR state label is removed, the
    fix-needed label is the one the next tick scans for)."""
    (tmp_path / ".worktrees"
     / f"orbi-owner-repo-issue-39-{RUN_ID}").mkdir(parents=True)
    make_wait_failure_fake(
        monkeypatch, labels=("ai-fix-needed",), progress_comments=[],
    )
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_1: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    # The current label set contains only ai-fix-needed, so the
    # idempotent transition adds it without inventing a remove for the
    # absent ai-pr-opened label.
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed"}),
    ]


def _write_session(worktree_dir, session_id="sess-1"):
    """Write one minimal session JSONL (the snapshot's session id)."""
    session_dir = worktree_dir / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sess.jsonl").write_text(
        json.dumps({"type": "session", "id": session_id}) + "\n",
        encoding="utf-8",
    )


def test_delivery_step_recoverable_failure_with_session_file_includes_session_scene(
        monkeypatch, tmp_path,
):
    """Issue #50: when the worktree carries a session file, the
    failure comment's scene shows the ACTUAL session (not the '-'
    placeholder): the full debug entry a human needs to continue."""
    worktree = (
        tmp_path / ".worktrees"
        / f"orbi-owner-repo-issue-39-{RUN_ID}"
    )
    worktree.mkdir(parents=True)
    _write_session(worktree)
    make_wait_failure_fake(monkeypatch)
    issue_comments = []
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    body = issue_comments[0][1]["body"]
    assert "session=sess-1" in body
    assert f"session_file={worktree / '.pi-session' / 'sess.jsonl'}" in body


def test_delivery_step_recoverable_failure_scene_snapshot_failure_is_logged(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50: a failing session-file read is best-effort
    observability: it is logged, the failure comment still carries the
    scene (with the '-' session placeholder), and the wait still
    completes the fix-needed transition."""
    worktree = (
        tmp_path / ".worktrees"
        / f"orbi-owner-repo-issue-39-{RUN_ID}"
    )
    worktree.mkdir(parents=True)
    make_wait_failure_fake(monkeypatch)
    issue_comments = []
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    def failing_snapshot(*args, **kwargs):
        raise OSError("session file unreadable")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(runner, "activity_snapshot", failing_snapshot)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    assert "activity scene failed" in caplog.text
    body = issue_comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "session=-" in body
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed",
                 "remove": "ai-pr-opened"}),
    ]


# ------------------------------------------- delivery_step: unrecoverable


def test_delivery_step_recoverable_failure_without_bound_run_id(
        monkeypatch, tmp_path,
):
    """Issue #50: a RECOVERABLE review failure with no bound run id
    (the defensive branch — the production caller always binds the
    scene's run id first) still keeps the Issue in the automatic fix
    loop: the failure comment simply carries no run marker, the scene
    shows `run=-`, and the milestone / progress scene are skipped (no
    run id to bind them to)."""
    (tmp_path / ".worktrees"
     / f"orbi-owner-repo-issue-39-{RUN_ID}").mkdir(parents=True)
    api_calls = make_wait_failure_fake(monkeypatch)
    issue_comments = []
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    # The autouse fixture resets the run id to None; do not re-bind it.
    assert runner.current_run_id() is None
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed",
                 "remove": "ai-pr-opened"}),
    ]
    body = issue_comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "<!-- orbi:run=" not in body
    assert "run=-" in body
    # No run id: no milestone, no progress scene at all.
    assert api_calls == []


def test_delivery_step_unrecoverable_failure_marks_blocked_with_reason(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50: an explicit UnrecoverableDeliveryError (an external
    precondition the AI cannot safely judge or fix) is the ONLY
    opened-PR failure that leaves the automatic loop: `ai-blocked`
    ALONE, and the failure comment states the explicit reason why
    automatic recovery is impossible."""
    (tmp_path / ".worktrees"
     / f"orbi-owner-repo-issue-39-{RUN_ID}").mkdir(parents=True)
    api_calls = make_wait_failure_fake(monkeypatch)
    edits = []
    issue_comments = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    reason = (
        "the review/fix loop is bounded (5 rounds) and exhausted "
        "without a clean verdict; the remaining findings need a human "
        "decision, so the AI cannot safely continue this PR"
    )

    def failing_review(*args, **kwargs):
        raise runner.ReviewRoundsExhausted(reason)

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")

    # The terminal state is ai-blocked ALONE ...
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-blocked",
                 "remove": "ai-pr-opened"}),
    ]
    # ... with a failure comment that carries the run marker, the PR
    # and the EXPLICIT reason why automatic recovery is impossible.
    body = issue_comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert MARKER in body
    assert PR_URL in body
    assert reason in body
    assert "cannot be recovered automatically" in body
    # The blocked scene (not the fix-needed scene) finishes the
    # progress comment.
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in api_calls
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted)
    assert not any("Orbi: fix needed" in body for body in posted)
    patches = [
        command for command in api_calls
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    finished = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Orbi blocked" in finished
    assert reason in finished
    # A bounded stop is an expected terminal state, not a Runner crash.
    assert "review_rounds_exhausted" in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
    assert "delivery_review_failed" not in caplog.text


def test_delivery_step_real_unrecoverable_failure_keeps_traceback(
        monkeypatch, caplog, tmp_path,
):
    """Unexpected unrecoverable failures remain visible as failures."""
    (tmp_path / ".worktrees"
     / f"orbi-owner-repo-issue-39-{RUN_ID}").mkdir(parents=True)
    make_wait_failure_fake(monkeypatch)
    monkeypatch.setattr(seam, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(seam, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise runner.UnrecoverableDeliveryError("credential revoked")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("ERROR")
    runner.delivery_step(PR_URL, _issue(), _config(tmp_path), "owner/repo")

    assert "delivery_review_failed" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


def test_delivery_step_base_branch_mismatch_marks_blocked_with_reason(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50 + #91: a resume scene frozen on another base branch
    than the configured one is an external precondition (a config
    change): the runner must not silently switch bases, so the Issue
    is marked ai-blocked with the explicit reason — never
    ai-fix-needed (auto-retrying would keep failing on the same
    mismatch)."""
    api_calls = make_wait_failure_fake(
        monkeypatch,
        scene=[
            {
                "body": (
                    f"{MARKER}\n"
                    f"Orbi opened PR: {PR_URL} (base_branch=develop "
                    f"base_sha=abc123def456 run_id={RUN_ID})"
                ),
                "authorAssociation": "OWNER",
            },
        ],
    )
    edits = []
    issue_comments = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    reviews = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.delivery_step(
        PR_URL, _issue(),
        runner.RunnerConfig(repo_dir=tmp_path, base_branch="main"), "owner/repo",
    )
    # No review was started (the mismatch is terminal before it).
    assert reviews == []
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-blocked",
                 "remove": "ai-pr-opened"}),
    ]
    body = issue_comments[0][1]["body"]
    assert "Orbi failed:" in body
    assert "base_branch=develop" in body
    assert "base_branch=main" in body
    # The blocked comment states why automatic recovery is impossible.
    assert "cannot be recovered automatically" in body
    assert "delivery_review_failed" in caplog.text


# --------------------------------------------------------- verify_resumed_pr


def test_verify_resumed_pr_diverged_pr_head_stays_fix_needed(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50: a resume verification failure from `verify_pr` (here
    the diverged-head failure — the plain #158 unpushed-commit scene
    passes through `verify_pr` and is continued by the next review
    session, which pushes the task branch on the same PR) is a
    RECOVERABLE failure: the branch, worktree and PR scene are
    preserved and the Issue is marked `ai-fix-needed` (never
    `ai-blocked`), so the next tick re-derives the same run, branch,
    worktree and PR."""
    from tests.test_resume_pr import (
        FAKE_PR_URL, FAKE_RUN_ID, make_resume_config, make_resume_issue,
        make_resume_scene, make_resume_failure_fake,
        expected_resume_worktree,
    )

    captured, _ = make_resume_failure_fake(monkeypatch)

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError(
            "PR head ed72915 is not local HEAD 18c78a2 and is not an "
            "ancestor of it (the branch diverged); a plain push would "
            "be rejected and a force push is forbidden, so the resume "
            "must not continue on this branch"
        )

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(
        RuntimeError, match="the branch diverged",
    ):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The Issue is marked ai-fix-needed (removing ai-pr-opened) — and
    # never ai-blocked: the branch, worktree and PR are preserved.
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    # The failure comment carries the run marker, the PR, the branch,
    # the worktree and the concrete error ...
    assert len(captured["comments"]) == 1
    body = captured["comments"][0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert FAKE_PR_URL in body
    assert "orbi/owner-repo-issue-9" in body
    assert str(expected_resume_worktree(tmp_path)) in body
    assert "the branch diverged" in body
    # ... and the fix-needed milestone (not the blocked one).
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted)
    assert not any("Orbi: blocked" in body for body in posted)
    assert "resume_pr_verification_failed" in caplog.text


def test_verify_resumed_pr_local_ahead_of_pr_head_continues_to_review(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50 (the #158 `d13b0c56` scene): the local HEAD is ahead
    of the remote PR head (an unpushed commit from a killed session).
    The resume verification must NOT fail: the real `verify_pr` logs
    the exact heads and returns the verified PR URL, so the delivery
    wait starts the next review session — which pushes the task branch
    on the same PR before its verdict (prompt_review.md). Only the
    idempotent `ai-in-progress` backfill (Issue #178), no ai-blocked,
    no replacement PR."""
    from tests.test_resume_pr import (
        FAKE_RUN_ID, make_resume_config, make_resume_issue,
        make_resume_scene, expected_resume_worktree,
    )

    worktree = expected_resume_worktree(tmp_path)
    worktree.mkdir(parents=True)
    branch = f"orbi/owner-repo-issue-9"
    local_head = "18c78a2" * 5 + "18c78a2"
    pr_head = "ed72915" * 5 + "ed72915"

    edits = []

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "edit"]:
            edits.append(command)
            return ""
        if command[:3] == ["git", "branch", "--show-current"]:
            return branch
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            # The PR head IS an ancestor of the local HEAD (local is
            # ahead) — the #158 scene.
            return ""
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return local_head
        if command[:2] == ["gh", "pr"]:
            return json.dumps([{
                "url": "https://github.com/owner/repo/pull/9",
                "baseRefName": "main",
                "headRefName": branch,
                "headRefOid": pr_head,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "owner"},
                "body": (
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
                    "Fixes #9\n\nPlan"
                ),
            }])
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything else (the repo's fake-coverage
    # convention): the resume verification must not run any other
    # command (no fetch — `require_latest_base=False` — no push).
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    issue = make_resume_issue()
    issue["labels"] = [{"name": "ai-fix-needed"}]
    url = runner.verify_resumed_pr(
        make_resume_scene(), issue, make_resume_config(tmp_path), "owner/repo",
    )
    assert url == "https://github.com/owner/repo/pull/9"
    # Issue #178: the resume backfills the in-flight label and clears
    # the stale fix-needed label before the review continues.
    assert edits == [
        ["gh", "issue", "edit", "9", "--repo", "owner/repo",
         "--add-label", "ai-in-progress", "--remove-label",
         "ai-fix-needed"],
    ]
    # The journal carries the commit/push phase: the exact local head
    # and remote PR head.
    assert "local_head_ahead_of_pr_head" in caplog.text
    assert f"pr_head={pr_head}" in caplog.text
    assert f"local_head={local_head}" in caplog.text


def test_verify_resumed_pr_unrecoverable_failure_marks_blocked_with_reason(
        monkeypatch, tmp_path,
):
    """Issue #50: an explicit UnrecoverableDeliveryError from the resume
    verification (an external precondition the AI cannot safely judge
    or fix) is terminal: `ai-blocked` ALONE with the explicit reason
    why automatic recovery is impossible."""
    from tests.test_resume_pr import (
        make_resume_config, make_resume_issue, make_resume_scene,
        make_resume_failure_fake, expected_resume_worktree, FAKE_RUN_ID,
    )

    captured, _ = make_resume_failure_fake(monkeypatch)

    def fake_verify_pr(*args, **kwargs):
        raise runner.UnrecoverableDeliveryError(
            "the PR head repo is a fork the runner is not authorized "
            "to merge; a human must re-target the PR"
        )

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with pytest.raises(
        runner.UnrecoverableDeliveryError, match="not authorized",
    ):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-blocked",
                "remove": "ai-pr-opened"}),
    ]
    body = captured["comments"][0][1]["body"]
    assert "Orbi failed:" in body
    assert "not authorized" in body
    assert "cannot be recovered automatically" in body
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: blocked" in body for body in posted)
    assert not any("Orbi: fix needed" in body for body in posted)


def test_verify_resumed_pr_recoverable_failure_with_session_file_includes_session_scene(
        monkeypatch, tmp_path,
):
    """Issue #50: when the worktree carries a session file, the resume
    failure comment's scene shows the ACTUAL session (not the '-'
    placeholder)."""
    from tests.test_resume_pr import (
        make_resume_config, make_resume_issue, make_resume_scene,
        make_resume_failure_fake, expected_resume_worktree, FAKE_RUN_ID,
    )

    captured, _ = make_resume_failure_fake(monkeypatch)
    worktree = expected_resume_worktree(tmp_path)
    worktree.mkdir(parents=True)
    _write_session(worktree)

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError("the verified commit was not pushed")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with pytest.raises(RuntimeError, match="not pushed"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    body = captured["comments"][0][1]["body"]
    assert "session=sess-1" in body
    assert f"session_file={worktree / '.pi-session' / 'sess.jsonl'}" in body


def test_verify_resumed_pr_recoverable_failure_scene_snapshot_failure_is_logged(
        monkeypatch, caplog, tmp_path,
):
    """Issue #50: a failing session-file read during the resume
    failure reporting is best-effort observability: it is logged, the
    failure comment still carries the scene (with the '-' session
    placeholder), and the original error is still re-raised."""
    from tests.test_resume_pr import (
        make_resume_config, make_resume_issue, make_resume_scene,
        make_resume_failure_fake, expected_resume_worktree, FAKE_RUN_ID,
    )

    captured, _ = make_resume_failure_fake(monkeypatch)
    expected_resume_worktree(tmp_path).mkdir(parents=True)

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError("the verified commit was not pushed")

    def failing_snapshot(*args, **kwargs):
        raise OSError("session file unreadable")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "activity_snapshot", failing_snapshot)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="not pushed"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert "activity scene failed" in caplog.text
    body = captured["comments"][0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "session=-" in body


def test_verify_resumed_pr_unrecoverable_failure_removes_leftover_fix_needed_label(
        monkeypatch, tmp_path,
):
    """Issue #50 + #82: an UNRECOVERABLE resume failure while the
    Issue is in `ai-fix-needed` (awaiting the next review session)
    leaves the terminal state `ai-blocked` ALONE — the leftover
    `ai-fix-needed` label is removed too."""
    from tests.test_resume_pr import (
        make_resume_config, make_resume_issue, make_resume_scene,
        make_resume_failure_fake, expected_resume_worktree, FAKE_RUN_ID,
    )

    captured, _ = make_resume_failure_fake(
        monkeypatch, labels=["ai-fix-needed"],
    )

    def fake_verify_pr(*args, **kwargs):
        raise runner.UnrecoverableDeliveryError("human decision needed")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with pytest.raises(
        runner.UnrecoverableDeliveryError, match="human decision",
    ):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The blocked patch clears every delivery-state label that is
    # present — here only `ai-fix-needed` (the delivery was awaiting the
    # next review session) — in one deterministic patch.
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-blocked",
                "remove": "ai-fix-needed"}),
    ]


def test_finish_progress_fix_needed_without_run_id_is_noop(monkeypatch):
    """Issue #50: the fix-needed progress scene is bound to the run id
    (the hidden marker); without one it is a no-op (no gh traffic)."""
    api_calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda *args, **kwargs: api_calls.append(args) or "",
    )
    assert runner._finish_progress(
        39, None, "owner/repo", None, None, PR_URL, "the failure",
        "the next tick resumes the same run", title="task",
        outcome="fix needed",
    ) is None
    assert api_calls == []


# ------------------------------------------------------------- block_scene_failure


def test_block_scene_failure_states_why_not_auto_recoverable(
        monkeypatch, caplog,
):
    """Issue #50 + #672 + #786: a CORRUPTED scene (a trusted
    `Orbi opened PR:` comment exists but cannot be parsed) is an
    external precondition the AI cannot fix by itself (the runner
    cannot derive run_id/branch/worktree/PR without it and cannot
    start a review session): the Issue is marked ai-blocked, the
    comment states the EXPLICIT reason why automatic recovery is
    impossible plus the human next step, and the function returns so
    the tick continues. `block_scene_failure` is the ONLY trigger of
    this path — a missing scene goes through its own branch."""
    issue = {"number": 39, "title": "task", "body": ""}
    comments = [
        {"body": "public comment", "authorAssociation": "NONE"},
    ]
    edits = []
    posted = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    runner.block_scene_failure(
        issue, scene.SceneError("opened PR comment is missing run_id"),
        "owner/repo", comments,
    )
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-blocked",
                 "remove": "ai-fix-needed"}),
    ]
    assert len(posted) == 1
    body = posted[0]
    assert "Orbi failed:" in body
    assert "cannot be recovered automatically" in body
    # The reason names what is missing (the trusted scene) and what a
    # human must do (restore the scene or relabel).
    assert "Orbi opened PR" in body
    assert "ai-fix-needed" in body
    assert "resume_scene_failed" in caplog.text or \
        "resume scene is malformed" in caplog.text


# ------------------------------------------------------------------ review rounds


def test_review_rounds_after_human_recovery_ignores_old_run():
    """Issue #483: a human recovery starts a new review budget."""
    comments = [
        {"body": "Orbi review round 1 for PR #46: findings",
         "authorAssociation": "OWNER", "createdAt": "2026-01-01T00:00:00Z"},
        {"body": "Orbi review round 5 for PR #46: findings",
         "authorAssociation": "OWNER", "createdAt": "2026-01-02T00:00:00Z"},
    ]
    assert runner.review_rounds_so_far(
        comments, after="2026-01-01T12:00:00Z",
    ) == 1


def test_human_recovery_requires_blocked_removal_then_fix_needed(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps([
            {"event": "labeled", "label": {"name": "ai-blocked"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"event": "unlabeled", "label": {"name": "ai-blocked"},
             "created_at": "2026-01-02T00:00:00Z"},
            {"event": "labeled", "label": {"name": "ai-fix-needed"},
             "created_at": "2026-01-02T01:00:00Z"},
        ]),
    )
    assert runner.human_review_recovery_at(39, "owner/repo") == \
        "2026-01-02T01:00:00Z"


def test_human_review_recovery_ignores_unrelated_label_history(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: '{"event":"labeled"}\n',
    )
    assert runner.human_review_recovery_at(39, "owner/repo") is None


def test_human_review_recovery_requires_latest_blocked_removal(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: json.dumps([
            {"event": "unlabeled", "label": {"name": "ai-blocked"},
             "created_at": "2026-01-01T00:00:00Z"},
            {"event": "labeled", "label": {"name": "ai-blocked"},
             "created_at": "2026-01-02T00:00:00Z"},
            {"event": "labeled", "label": {"name": "ai-fix-needed"},
             "created_at": "2026-01-02T01:00:00Z"},
        ]),
    )
    assert runner.human_review_recovery_at(39, "owner/repo") is None


def test_human_review_recovery_skips_non_object_events(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "1\n",
    )
    assert runner.human_review_recovery_at(39, "owner/repo") is None


def test_log_recovery_ci_status_logs_malformed_response_and_continues(
        monkeypatch, caplog,
):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: json.dumps({}),
    )
    caplog.set_level("WARNING")
    runner.log_recovery_ci_status(
        {"number": 46, "head_oid": "head-sha"}, "owner/repo",
    )
    assert "review_recovery_ci_status_failed pr=46" in caplog.text


def test_log_recovery_ci_status_logs_only_check_summary(monkeypatch, caplog):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps([]),
    )
    caplog.set_level("INFO")
    runner.log_recovery_ci_status(
        {"number": 46, "head_oid": "head-sha"}, "owner/repo",
    )
    assert "review_recovery_ci_status pr=46 checks=none" in caplog.text


def test_exhausted_review_enters_new_budget_after_human_recovery(
    monkeypatch, tmp_path,
):
    """Issue #483 on the scene budget (Issue #788): the human recovery
    transition AFTER the recovered scene starts a fresh budget — the
    round runs (the freeze proves it)."""
    from tests.test_resume_pr import FAKE_RUN_ID
    monkeypatch.setattr(runner, "human_review_recovery_at",
                        lambda *a: "2026-02-01T00:00:00Z")
    frozen = {"number": 46, "url": PR_URL, "base_ref": "main",
              "base_oid": "abc", "head_ref": "b", "head_oid": "def"}
    freezes = []
    monkeypatch.setattr(runner, "freeze_pr",
                        lambda *a, **k: freezes.append(1) or frozen)
    # The frozen head is the delivery's recorded engine push (the
    # normal in-flight state, Issue #833): the round-start adoption
    # short-circuits before any git call on the bare worktree.
    runner.write_run_state(runner.RunContext(
        run_id=FAKE_RUN_ID, issue=39, branch="branch",
        worktree=tmp_path, source_repo="owner/repo",
    ))
    runner.record_pushed_head(tmp_path, "def")
    monkeypatch.setattr(runner, "log_recovery_ci_status", lambda *a, **k: None)
    monkeypatch.setattr(seam, "_safe_publish", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "run_review",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("review started")),
    )
    config = runner.RunnerConfig(run_id=FAKE_RUN_ID, base_branch="main", repo_dir=tmp_path)
    with pytest.raises(RuntimeError, match="review started"):
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", config, "owner/repo", 39,
            title="task", priority="normal",
            # Five completed rounds in the scene, but the recovery
            # (2026-02-01) postdates the scene comment (2026-01-01).
            scene={
                "run_id": FAKE_RUN_ID, "base_branch": "main",
                "base_sha": "abc", "pr_url": PR_URL, "external": "",
                "review_round": 5, "scene_at": "2026-01-01T00:00:00Z",
            },
        )
    assert freezes == [1]


def test_review_rounds_exhausted_raises_unrecoverable(monkeypatch, tmp_path):
    """Issue #50: the bounded review/fix loop (5 rounds) exhausted
    without a clean verdict is a human decision, not a recoverable
    failure: `review_and_merge_if_clean` raises
    UnrecoverableDeliveryError (the caller marks the Issue ai-blocked
    with the reason). Issue #788: the budget is the scene's round
    counter — exhausted BEFORE any freeze or review."""
    monkeypatch.setattr(runner, "human_review_recovery_at", lambda *a: None)
    from tests.test_resume_pr import FAKE_RUN_ID

    freezes = []
    monkeypatch.setattr(runner, "freeze_pr",
                        lambda *a, **k: freezes.append(1) or {
                            "number": 46, "url": PR_URL, "base_ref": "main",
                            "base_oid": "abc", "head_ref": "b",
                            "head_oid": "def",
                        })
    config = runner.RunnerConfig(run_id=FAKE_RUN_ID, base_branch="main", repo_dir=tmp_path)
    with pytest.raises(
        runner.UnrecoverableDeliveryError, match="exhausted",
    ):
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", config, "owner/repo", 39,
            title="task", priority="normal",
            scene={
                "run_id": FAKE_RUN_ID, "base_branch": "main",
                "base_sha": "abc", "pr_url": PR_URL, "external": "",
                "review_round": 5, "scene_at": "2026-01-01T00:00:00Z",
            },
        )
    assert freezes == []


# --------------------------------- Issue #842 (D2): external triage stop ----

from tests.test_resume_pr import FAKE_RUN_ID


def _external_review_env(monkeypatch, tmp_path, *, external: bool):
    """Shared scene for the D2 direct `review_and_merge_if_clean` tests:
    a frozen PR, a clean verdict for that head, and capture lists for
    the Issue/PR comments and label patches. The merge gate and the
    merge confirmation FAIL the test when reached — the caller decides
    by scene whether they may run."""
    frozen = {"number": 46, "url": PR_URL, "base_ref": "main",
              "base_oid": "abc", "head_ref": "contributor-patch",
              "head_oid": "def"}
    monkeypatch.setattr(seam, "freeze_pr", lambda *a, **k: frozen)
    monkeypatch.setattr(
        runner, "run_review",
        lambda *a, **k: (
            "Review of the contributor diff: the change is minimal and "
            "tested.\nREVIEW_VERDICT "
            '{"verdict":"pass","head":"def","blockers":0,"majors":0,'
            '"minors":1,"findings":[]}'
        ),
    )
    monkeypatch.setattr(seam, "merge_gate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("merge gate must not run")))
    monkeypatch.setattr(seam, "confirm_merged",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("confirm_merged must not run")))
    monkeypatch.setattr(seam, "human_review_recovery_at",
                        lambda *a: None)
    monkeypatch.setattr(seam, "pr_delivery_rollup",
                        lambda *a, **k: (
                            "OPEN",
                            [{"name": "tests", "status": "COMPLETED",
                              "conclusion": "SUCCESS"}],
                        ))
    monkeypatch.setattr(seam, "_safe_publish", lambda *a, **k: None)
    # The frozen head is the delivery's recorded engine push (the normal
    # in-flight state, Issue #833): the round-start adoption
    # short-circuits before any git call on the bare worktree.
    runner.write_run_state(runner.RunContext(
        run_id=FAKE_RUN_ID, issue=39, branch="contributor-patch",
        worktree=tmp_path, source_repo="owner/repo",
    ))
    runner.record_pushed_head(tmp_path, "def")
    comments: list = []
    monkeypatch.setattr(seam, "comment_issue",
                        lambda *args, **kwargs: comments.append(kwargs))
    monkeypatch.setattr(seam, "comment_pr",
                        lambda *args, **kwargs: comments.append(kwargs))
    patches: list = []
    monkeypatch.setattr(seam, "issue_labels",
                        lambda *a, **k: ["ai-pr-opened"])
    monkeypatch.setattr(seam, "apply_label_patch",
                        lambda number, **kwargs: patches.append(kwargs))
    config = runner.RunnerConfig(
        run_id=FAKE_RUN_ID, base_branch="main", repo_dir=tmp_path,
    )
    scene = {
        "run_id": FAKE_RUN_ID, "base_branch": "main",
        "base_sha": "abc", "pr_url": PR_URL,
        "external": "true" if external else "",
        "review_round": 0, "scene_at": "2026-01-01T00:00:00Z",
    }
    return comments, patches


def test_external_takeover_clean_verdict_stops_at_triage(
    monkeypatch, tmp_path, caplog,
):
    """Issue #842 (D2): an external contribution's clean verdict is the
    engine's FINAL output — the merge gate never runs, the review
    conclusion stays on the Issue, and the ticket stops at `ai-blocked`
    (the human decision point): a maintainer decides whether to accept
    the PR and which version ships it. The engine never writes a
    Milestone."""
    comments, patches = _external_review_env(monkeypatch, tmp_path,
                                             external=True)
    caplog.set_level("INFO")
    merged = runner.review_and_merge_if_clean(
        tmp_path, "contributor-patch", "main",
        runner.RunnerConfig(run_id=FAKE_RUN_ID, base_branch="main",
                            repo_dir=tmp_path),
        "owner/repo", 39, title="task", priority="normal",
        scene={
            "run_id": FAKE_RUN_ID, "base_branch": "main",
            "base_sha": "abc", "pr_url": PR_URL, "external": "true",
            "review_round": 0, "scene_at": "2026-01-01T00:00:00Z",
        },
    )
    assert merged is False
    # The review conclusion is on the ISSUE (the maintainer's decision
    # surface), never silently merged away.
    assert len(comments) == 1
    body = comments[0]["body"]
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in body
    assert "external PR review" in body
    assert "verdict=pass" in body
    assert "tests=COMPLETED/SUCCESS" in body
    assert "Review of the contributor diff" in body
    assert "ai-blocked" in body
    assert comments[0]["repo"] == "owner/repo"
    # The terminal triage state: ai-blocked ALONE (the opened-PR label
    # is cleared with the same blocked patch as every other terminal
    # path).
    assert patches[-1]["event"] == runner.EVENT_BLOCKED
    assert "external_takeover_triage" in caplog.text


def test_internal_clean_verdict_still_merges(monkeypatch, tmp_path):
    """Issue #842 regression guard: the D2 triage stop keys on the
    external scene ONLY — an internal delivery's clean verdict reaches
    the merge gate exactly as before."""
    comments, patches = _external_review_env(monkeypatch, tmp_path,
                                             external=False)
    # Replace the failing gate stubs with the real merge path fakes.
    monkeypatch.setattr(seam, "merge_gate",
                        lambda *a, **k: {"url": PR_URL})
    monkeypatch.setattr(seam, "confirm_merged",
                        lambda *a, **k: {"merge_commit": "m1"})
    monkeypatch.setattr(seam, "merge_commit_metrics",
                        lambda *a, **k: (0, 1))
    monkeypatch.setattr(seam, "sync_base_checkout", lambda *a, **k: None)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main",
        runner.RunnerConfig(run_id=FAKE_RUN_ID, base_branch="main",
                            repo_dir=tmp_path),
        "owner/repo", 39, title="task", priority="normal",
        scene={
            "run_id": FAKE_RUN_ID, "base_branch": "main",
            "base_sha": "abc", "pr_url": PR_URL, "external": "",
            "review_round": 0, "scene_at": "2026-01-01T00:00:00Z",
        },
    )
    assert merged is True
    assert patches[-1]["event"] == runner.EVENT_MERGED


# -------------------------------------------------------------------- prompt


def test_prompt_review_covers_unpushed_local_commit():
    """Issue #50 (the #158 `d13b0c56` scene): the review prompt must
    tell the reviewer to push an unpushed local commit (local HEAD
    ahead of the frozen PR head) on the same task branch before
    emitting the verdict — never discard it, never create a
    replacement PR."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent
            / "prompts" / "prompt_review.md").read_text(encoding="utf-8").lower()
    assert "local head" in text
    assert "ahead of the frozen" in text
    assert "push" in text


# --------------------------------- dead-loop cap + comment dedup (Issue #825)

def _failure_exc():
    return RuntimeError("the resume verification of PR x failed: boom")


def _failure_history(count, fp, *, run_id=RUN_ID, marker=None):
    """`count` identical trusted failure comments (run marker + hidden
    fingerprint marker) — the repeated-dead-end history. Each carries
    the comment id the real `gh issue view --json comments` returns."""
    marker = marker or f"<!-- orbi:run={run_id} -->"
    return [
        {
            "id": 900 + i,
            "body": (
                f"{marker}\n<!-- orbi:fail={fp} -->\n"
                "Orbi needs a fix: the resume verification failed: boom"
            ),
            "authorAssociation": "OWNER",
        }
        for i in range(count)
    ]


def make_report_fake(monkeypatch, *, history=None, labels=("ai-fix-needed",),
                     failing_history_read=False, failing_comment_update=False):
    """Shared fake for the direct `report_delivery_failure` tests
    (Issue #825): a MUTABLE comment store seeded with `history` backs
    the comment-history read; posted failure comments join the store
    (with an id, like the real API), `update_issue_comment` mutates the
    store in place; the label read answers `labels`; comments, in-place
    updates and label edits are captured."""
    captured = {"comments": [], "pr_comments": [], "edits": [],
                "updates": []}
    store = [dict(comment) for comment in (history or [])]
    next_id = [1000]

    def fake_run(command, **kwargs):
        # Single-element dispatch — no argv-shape asserts (Issue #789).
        if (command[0] == "gh" and command[1] == "api"
                and "--paginate" in command):
            return json.dumps([
                {"id": comment.get("rest_id", comment.get("id")),
                 "node_id": comment.get("id")}
                for comment in store
            ])
        if command[0] == "gh" and command[1] == "api":
            # The progress publisher (pure bypass): GET answers no
            # tracked comment, POST/PATCH answer success shapes.
            if "--method" not in command:
                return json.dumps([])
            return json.dumps({"id": 91, "body": "x", "url": "u"})
        if command[-1] == "comments":
            if failing_history_read:
                raise RuntimeError("history read unavailable")
            return json.dumps({"comments": store})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": name}
                                          for name in labels]})
        raise AssertionError(command)

    def post_issue_comment(number, *, repo, body):
        captured["comments"].append(body)
        store.append({"id": next_id[0], "body": body,
                      "authorAssociation": "OWNER"})
        next_id[0] += 1

    def update_stored_comment(comment_id, *, repo, body):
        if failing_comment_update:
            raise RuntimeError("comment update unavailable")
        for comment in store:
            if comment.get("id") == comment_id or comment.get("rest_id") == comment_id:
                comment["body"] = body
                break
        else:
            raise AssertionError(f"no comment {comment_id} to update")
        captured["updates"].append((comment_id, body))

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(seam, "comment_issue", post_issue_comment)
    monkeypatch.setattr(seam, "update_issue_comment",
                        update_stored_comment)
    monkeypatch.setattr(seam, "comment_pr",
        lambda number, *, repo, body:
            captured["pr_comments"].append(body))
    monkeypatch.setattr(seam, "edit_issue",
        lambda number, *, repo, add=None, remove=None:
            captured["edits"].append((add, remove)))
    return captured


def _report(exc, monkeypatch_unused=None, **kwargs):
    """One classified recoverable report of the resume-verify failure."""
    runner.report_delivery_failure(
        exc, issue={"number": 39, "title": "task", "body": ""},
        source_repo="owner/repo", run_id=RUN_ID, pr_url=PR_URL,
        worktree=Path("/nonexistent"), branch=BRANCH,
        role=runner.ROLE_REVIEW,
        cause=f"the resume verification of PR {PR_URL} failed: {exc}",
        **kwargs,
    )


def test_report_failure_comment_carries_unknown_head_scene(
        monkeypatch, tmp_path):
    captured = make_report_fake(monkeypatch)
    scene_block = scene.render(scene.Scene(
        run_id=RUN_ID, base_branch="main", base_sha="base", pr_url=PR_URL,
        verdict_head_unknown_round=2,
    ))
    _report(ValueError("unknown head"),
            review_scene_block=scene_block)
    body = captured["comments"][0]
    assert scene.parse(body).verdict_head_unknown_round == 2
    assert body.index("<details><summary>Diagnosis</summary>") < body.index(
        scene_block
    ) < body.index("</details>")


def test_report_failure_comment_carries_the_fingerprint_marker(
        monkeypatch, tmp_path):
    """Issue #825 (defect 3): every recoverable failure comment carries
    the hidden `orbi:fail` fingerprint marker — the key the dedup and
    the dead-loop streak scan match on."""
    captured = make_report_fake(monkeypatch)
    exc = _failure_exc()
    _report(exc)
    fp = runner_health.failure_fingerprint(exc)
    assert captured["comments"], "the first failure is reported"
    assert captured["comments"][0].startswith(
        f"{MARKER}\n<!-- orbi:fail={fp} -->\n"
    )
    # Recoverable: the PR thread carries the same failure once.
    assert len(captured["pr_comments"]) == 1


def test_report_failure_repeats_bump_the_comment_counter_in_place(
        monkeypatch, tmp_path, caplog):
    """Issue #825 (defect 3): a repeated identical failure of the same
    run (same fingerprint) bumps the existing comment's hidden repeat
    counter in place — no second Issue comment, no second PR comment.
    82 identical comments became one, updated in place."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    captured = make_report_fake(
        monkeypatch, history=_failure_history(1, fp),
    )
    caplog.set_level("INFO")
    retry_scene = "review retry=2"
    _report(exc, review_scene_block=retry_scene)
    assert captured["comments"] == []
    assert captured["pr_comments"] == []
    # The new retry scene is persisted on the existing comment, rather
    # than being discarded by the dedup path.
    assert retry_scene in captured["updates"][0][1]
    # The counter rides on the EXISTING comment: `:2` after the second
    # failure, same comment id.
    update_id, update_body = captured["updates"][0]
    assert update_id == 900
    assert f"<!-- orbi:fail={fp}:2 -->" in update_body
    assert "failure_comment_deduplicated" in caplog.text


def test_report_failure_three_identical_ticks_escalate_the_dead_loop(
        monkeypatch, tmp_path, caplog):
    """Issue #825, the REAL three-tick user path (the acceptance red
    proof): tick 1 posts the failure comment, tick 2 bumps its counter
    in place, tick 3 reads the summed count and escalates to
    `ai-blocked`. The dead loop is capped at 3 ticks with 2 comments
    total — the #360 scene burned 82 ticks and 389 comments."""
    captured = make_report_fake(monkeypatch)
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    caplog.set_level("INFO")
    _report(exc)
    _report(exc)
    _report(exc)
    # Two comments total: the first report + the terminal escalation.
    assert len(captured["comments"]) == 2
    assert "3 consecutive times" in captured["comments"][1]
    assert "dead loop" in captured["comments"][1]
    assert "Orbi failed:" in captured["comments"][1]
    # Recoverable ticks patch ai-fix-needed; the third escalates.
    assert captured["edits"] == [
        ("ai-fix-needed", None),
        ("ai-fix-needed", None),
        ("ai-blocked", "ai-fix-needed"),
    ]
    # The PR thread only ever saw the first recoverable copy; terminal
    # comments are Issue-only.
    assert len(captured["pr_comments"]) == 1
    # Exactly one in-place counter bump (tick 2); the store keeps the
    # bumped body (tick 3's streak scan read it).
    assert len(captured["updates"]) == 1
    assert f"<!-- orbi:fail={fp}:2 -->" in captured["updates"][0][1]
    assert "failure_streak_escalated" in caplog.text
    assert "failure_comment_deduplicated" in caplog.text


def test_report_failure_third_identical_failure_blocks_the_dead_loop(
        monkeypatch, tmp_path, caplog):
    """Issue #825 (defect 2): the same failure of the same run
    recurring to the limit is a dead loop, not a transient error — the
    third consecutive occurrence escalates to `ai-blocked` ALONE with
    the explicit count and the unchanged-precondition verdict."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    captured = make_report_fake(
        monkeypatch, history=_failure_history(2, fp),
    )
    caplog.set_level("INFO")
    _report(exc)
    assert captured["edits"] == [("ai-blocked", "ai-fix-needed")]
    assert len(captured["comments"]) == 1
    body = captured["comments"][0]
    assert "3 consecutive times" in body
    assert "dead loop" in body
    assert "Orbi failed:" in body
    # Terminal: Issue only — the PR thread gets no comment.
    assert captured["pr_comments"] == []
    assert "failure_streak_escalated" in caplog.text


def test_report_failure_scene_block_breaks_the_streak(
        monkeypatch, tmp_path):
    """Issue #825 (defect 2): a scene block (an opened PR, a completed
    review round) means the delivery line ADVANCED — it ends the
    identical-failure streak, so a following identical failure stays in
    the recoverable fix loop (the round budget is the bound there),
    never escalated on stale history. The dedup is independent of the
    streak: the comment exists once, repeats update it in place."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    history = [
        {
            "id": 800,
            "body": (
                f"{MARKER}\nOrbi review round 1 for PR #46\n"
                '<!-- orbi:scene:v1 {"base_branch": "main", "schema": 1, '
                '"run_id": "%s", "review_round": 1} -->' % RUN_ID
            ),
            "authorAssociation": "OWNER",
        },
        *_failure_history(1, fp),
    ]
    captured = make_report_fake(monkeypatch, history=history)
    _report(exc)
    # The streak reset: still recoverable (no escalation) ...
    assert captured["edits"] == [("ai-fix-needed", None)]
    # ... and the dedup holds: the identical failure was already
    # reported, the counter bumps in place instead of a new comment.
    assert captured["comments"] == []
    assert captured["pr_comments"] == []
    assert len(captured["updates"]) == 1
    assert captured["updates"][0][0] == 900


def test_report_failure_different_fingerprint_breaks_the_streak(
        monkeypatch, tmp_path):
    """Issue #825 (defect 2): a different failure of the same run is a
    NEW dead-end candidate — it neither continues the old streak nor is
    deduped against it."""
    exc = _failure_exc()
    other = {
        "body": (
            f"{MARKER}\n<!-- orbi:fail={'0' * 16} -->\n"
            "Orbi needs a fix: something else"
        ),
        "authorAssociation": "OWNER",
    }
    captured = make_report_fake(monkeypatch, history=[other])
    _report(exc)
    assert len(captured["comments"]) == 1
    assert "Orbi needs a fix:" in captured["comments"][0]


def test_report_failure_history_read_failure_degrades_fail_open(
        monkeypatch, tmp_path, caplog):
    """Issue #825: a failed history read must never break the failure
    report itself — the report degrades to the plain recoverable path
    (one comment, fix-needed) and the read failure is journaled."""
    captured = make_report_fake(
        monkeypatch, failing_history_read=True,
    )
    caplog.set_level("INFO")
    _report(_failure_exc())
    assert len(captured["comments"]) == 1
    assert "Orbi needs a fix:" in captured["comments"][0]
    assert "failure_history_read_failed" in caplog.text


def test_human_decision_failure_is_terminal_with_decision_details(
        monkeypatch, tmp_path):
    captured = make_report_fake(monkeypatch, labels=("ai-pr-opened",))
    decision = (
        "review requires human decision: note: same failure repeated; "
        "fix: choose the authoritative address source"
    )
    action = "Choose the authoritative address source."
    outcome = runner.report_delivery_failure(
        runner.HumanDecisionRequired(decision, action=action),
        issue={"number": 39, "title": "task", "body": ""},
        source_repo="owner/repo", run_id=RUN_ID, pr_url=PR_URL,
        worktree=Path("/nonexistent"), branch=BRANCH,
        role=runner.ROLE_REVIEW, action=action,
        reason="The review requires a human decision.", diagnosis=decision,
    )
    assert outcome == "blocked"
    assert captured["edits"] == [("ai-blocked", "ai-pr-opened")]
    body = captured["comments"][0]
    assert body.index(action) < body.index("The review requires")
    assert body.index("The review requires") < body.index(
        "<details><summary>Diagnosis</summary>"
    )
    assert decision in body
    assert captured["pr_comments"] == []


def test_report_failure_without_run_id_keeps_the_plain_path(
        monkeypatch, tmp_path):
    """Issue #825: without a bound run id there is nothing to
    correlate — no fingerprint marker, no dedup, no streak; the report
    still goes out once."""
    captured = make_report_fake(monkeypatch)
    runner.report_delivery_failure(
        _failure_exc(), issue={"number": 39, "title": "task", "body": ""},
        source_repo="owner/repo", run_id=None, pr_url=PR_URL,
        worktree=Path("/nonexistent"), branch=BRANCH,
        role=runner.ROLE_REVIEW,
        cause="the resume verification of PR x failed: boom",
    )
    assert len(captured["comments"]) == 1
    assert "orbi:fail=" not in captured["comments"][0]


def test_streak_and_dedup_scans_skip_non_failure_noise(
        monkeypatch, tmp_path):
    """Issue #825: the pure scans read TRUSTED comments only — a public
    comment (even one carrying a copied fail marker) neither extends
    nor breaks the streak and never satisfies the dedup; a non-string
    body is skipped alike. Publisher milestones in between change no
    premise, so the streak survives them."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    failure_body = (
        f"{MARKER}\n<!-- orbi:fail={fp} -->\n"
        "Orbi needs a fix: the resume verification failed: boom"
    )
    history = [
        {"body": failure_body, "authorAssociation": "OWNER"},
        # A public comment with a copied marker: skipped, not a break.
        {"body": failure_body, "authorAssociation": "NONE"},
        # A non-string body: skipped alike.
        {"body": None, "authorAssociation": "OWNER"},
        # A publisher milestone: no premise change, the streak survives.
        {"body": f"{MARKER}\n**Orbi progress**\n\nfix needed",
         "authorAssociation": "OWNER"},
    ]
    assert runner._failure_streak(history, RUN_ID, fp) == 1
    assert runner._reported_failure_comment(
        history, RUN_ID, fp) is history[0]
    # The public copy alone never satisfies the dedup.
    public_only = [
        {"body": failure_body, "authorAssociation": "NONE"},
    ]
    assert runner._reported_failure_comment(
        public_only, RUN_ID, fp,
    ) is None
    assert runner._failure_streak(public_only, RUN_ID, fp) == 0
    # The hidden `:count` suffix IS the occurrence count: one comment
    # stamped `:2` (two deduped failures) counts 2, and the bump
    # helper raises it in place.
    counted = {
        "body": (
            f"{MARKER}\n<!-- orbi:fail={fp}:2 -->\n"
            "Orbi needs a fix: the resume verification failed: boom"
        ),
        "authorAssociation": "OWNER",
    }
    assert runner._failure_streak([counted], RUN_ID, fp) == 2
    bumped = progress.bump_failure_repeat(counted["body"], fp)
    assert f"<!-- orbi:fail={fp}:3 -->" in bumped
    assert runner._failure_streak([{"body": bumped,
                                    "authorAssociation": "OWNER"}],
                                 RUN_ID, fp) == 3


def test_report_failure_repeat_resolves_graphql_comment_id(
        monkeypatch, tmp_path):
    """Issue #1217: GraphQL comment node ids are resolved to the REST id
    before the repeat counter is patched, and the report completes."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    history = [{
        "id": "IC_kwDOUC1jsc8AAAABVrm_6w",
        "rest_id": 900,
        "body": _failure_history(1, fp)[0]["body"],
        "authorAssociation": "OWNER",
    }]
    captured = make_report_fake(monkeypatch, history=history)
    _report(exc)
    assert captured["comments"] == []
    assert captured["updates"][0][0] == 900
    assert f"<!-- orbi:fail={fp}:2 -->" in captured["updates"][0][1]


def test_issue_comment_rest_id_rejects_non_numeric_rest_id(monkeypatch):
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: json.dumps([[{
            "node_id": "node-1", "id": "not-an-integer",
        }]]),
    )
    with pytest.raises(ValueError, match="REST comment id not found"):
        runner.issue_comment_rest_id(
            39, repo="owner/repo", node_id="node-1",
        )
    with pytest.raises(ValueError, match="REST comment id not found"):
        runner.issue_comment_rest_id(
            39, repo="owner/repo", node_id="node-2",
        )


def test_report_failure_unsupported_comment_id_does_not_escape(
        monkeypatch, tmp_path, caplog):
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    captured = make_report_fake(monkeypatch, history=[{
        "id": None,
        "body": _failure_history(1, fp)[0]["body"],
        "authorAssociation": "OWNER",
    }])
    caplog.set_level("INFO")
    _report(exc)
    assert captured["comments"] == []
    assert "failure_comment_update_failed" in caplog.text


def test_report_failure_comment_update_error_does_not_escape(
        monkeypatch, tmp_path, caplog):
    """Issue #1217: an unavailable comment PATCH is logged and does not
    crash the Runner tick."""
    exc = _failure_exc()
    fp = runner_health.failure_fingerprint(exc)
    history = _failure_history(1, fp)
    make_report_fake(
        monkeypatch, history=history, failing_comment_update=True,
    )
    caplog.set_level("INFO")
    assert _report(exc) is None
    assert "failure_comment_update_failed" in caplog.text


def test_report_fake_rejects_unexpected_commands(monkeypatch):
    """Every branch of the report fake is exercised (the repo's
    fake-coverage convention): an unrecognized command fails fast, and
    an in-place update of an unknown comment id fails fast."""
    make_report_fake(monkeypatch)
    with pytest.raises(AssertionError):
        seam.run_command(["git", "status"])
    with pytest.raises(AssertionError):
        seam.update_issue_comment(999999, repo="owner/repo", body="b")
