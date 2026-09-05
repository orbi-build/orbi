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

import pytest

import orbi.runner as runner

PR_URL = "https://github.com/owner/repo/pull/46"
RUN_ID = "a1b2c3d4"
MARKER = f"<!-- orbi:run={RUN_ID} -->"
WORKTREE = "/srv/repo/.worktrees/orbi-owner-repo-issue-39-a1b2c3d4"
BRANCH = "orbi/owner-repo-issue-39-a1b2c3d4"


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)


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
    """Shared `run_command` fake for the `wait_for_delivery` failure
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    return api_calls


def _issue():
    return {"number": 39, "title": "task", "body": ""}


def _config(tmp_path):
    return {"repo_dir": tmp_path, "base_branch": "main"}


# ---------------------------------------------------------------- classification


def test_is_unrecoverable_failure_true_only_for_explicit_error():
    assert runner.is_unrecoverable_failure(
        runner.UnrecoverableDeliveryError("human decision needed"),
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


# ------------------------------------------------- wait_for_delivery: recoverable


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
def test_wait_for_delivery_recoverable_review_failure_stays_fix_needed(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    # The wait returns (the slot is released by the caller); the next
    # timer picks the ai-fix-needed Issue up on the same run/PR.
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")

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
    assert "stderr=<empty>" in body
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
    assert "next step:" in finished
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


def test_wait_for_delivery_recoverable_failure_while_fix_needed_keeps_label(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_1: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    # The single transition: ai-pr-opened removed, ai-fix-needed added
    # (the label the Issue already carries is re-added idempotently —
    # the next tick's scan needs it, and a leftover fix-needed label
    # must never be removed on a recoverable failure).
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed",
                 "remove": "ai-pr-opened"}),
    ]


def _write_session(worktree_dir, session_id="sess-1"):
    """Write one minimal session JSONL (the snapshot's session id)."""
    session_dir = worktree_dir / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sess.jsonl").write_text(
        json.dumps({"type": "session", "id": session_id}) + "\n",
        encoding="utf-8",
    )


def test_wait_for_delivery_recoverable_failure_with_session_file_includes_session_scene(
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
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    body = issue_comments[0][1]["body"]
    assert "session=sess-1" in body
    assert f"session_file={worktree / '.pi-session' / 'sess.jsonl'}" in body


def test_wait_for_delivery_recoverable_failure_scene_snapshot_failure_is_logged(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    def failing_snapshot(*args, **kwargs):
        raise OSError("session file unreadable")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(runner, "activity_snapshot", failing_snapshot)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")
    assert "activity scene failed" in caplog.text
    body = issue_comments[0][1]["body"]
    assert "Orbi needs a fix:" in body
    assert "session=-" in body
    assert edits == [
        ((39,), {"repo": "owner/repo", "add": "ai-fix-needed",
                 "remove": "ai-pr-opened"}),
    ]


# ------------------------------------------- wait_for_delivery: unrecoverable


def test_wait_for_delivery_recoverable_failure_without_bound_run_id(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)

    def failing_review(*args, **kwargs):
        raise RuntimeError("pi_exit_3: the review Pi failed")

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    # The autouse fixture resets the run id to None; do not re-bind it.
    assert runner.current_run_id() is None
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")
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


def test_wait_for_delivery_unrecoverable_failure_marks_blocked_with_reason(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    reason = (
        "the review/fix loop is bounded (5 rounds) and exhausted "
        "without a clean verdict; the remaining findings need a human "
        "decision, so the AI cannot safely continue this PR"
    )

    def failing_review(*args, **kwargs):
        raise runner.UnrecoverableDeliveryError(reason)

    monkeypatch.setattr(runner, "review_and_merge_if_clean", failing_review)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.wait_for_delivery(PR_URL, _issue(), _config(tmp_path), "owner/repo")

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


def test_wait_for_delivery_base_branch_mismatch_marks_blocked_with_reason(
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
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: issue_comments.append((args, kwargs)),
    )
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    reviews = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", RUN_ID)
    caplog.set_level("INFO")
    runner.wait_for_delivery(
        PR_URL, _issue(),
        {"repo_dir": tmp_path, "base_branch": "main"}, "owner/repo",
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    assert "orbi/owner-repo-issue-9-a1b2c3d4" in body
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
    branch = f"orbi/owner-repo-issue-9-{FAKE_RUN_ID}"
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

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    url = runner.verify_resumed_pr(
        make_resume_scene(), make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    assert url == "https://github.com/owner/repo/pull/9"
    # Issue #178: the resume backfills the in-flight label before the
    # review continues — exactly one idempotent edit, nothing else.
    assert edits == [
        ["gh", "issue", "edit", "9", "--repo", "owner/repo",
         "--add-label", "ai-in-progress"],
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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


def test_finish_fix_needed_progress_without_run_id_is_noop(monkeypatch):
    """Issue #50: the fix-needed progress scene is bound to the run id
    (the hidden marker); without one it is a no-op (no gh traffic)."""
    api_calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda *args, **kwargs: api_calls.append(args) or "",
    )
    assert runner._finish_fix_needed_progress(
        39, None, "owner/repo", None, None, PR_URL, "the failure",
        "task",
    ) is None
    assert api_calls == []


# ------------------------------------------------------------- block_scene_failure


def test_block_scene_failure_states_why_not_auto_recoverable(
        monkeypatch, caplog,
):
    """Issue #50: a scene that cannot be recovered (no trusted
    `Orbi opened PR:` comment) is an external precondition the
    AI cannot fix by itself (the runner cannot derive run_id/branch/
    worktree/PR without it and cannot start a review session): the
    Issue is marked ai-blocked and the comment states the EXPLICIT
    reason why automatic recovery is impossible plus the human
    next step."""
    issue = {"number": 39, "title": "task", "body": ""}
    comments = [
        {"body": "public comment", "authorAssociation": "NONE"},
    ]
    edits = []
    posted = []
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *args, **kwargs: posted.append(kwargs["body"]),
    )
    with pytest.raises(ValueError, match="no trusted"):
        runner.block_scene_failure(
            issue, ValueError("no trusted opened PR scene comment"),
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


def test_review_rounds_exhausted_raises_unrecoverable(monkeypatch, tmp_path):
    """Issue #50: the bounded review/fix loop (5 rounds) exhausted
    without a clean verdict is a human decision, not a recoverable
    failure: `review_and_merge_if_clean` raises
    UnrecoverableDeliveryError (the caller marks the Issue ai-blocked
    with the reason)."""
    from tests.test_resume_pr import FAKE_RUN_ID

    monkeypatch.setattr(
        runner, "issue_comments",
        lambda number, repo: [
            {
                "body": (
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
                    f"Orbi review round {i} for PR #46: findings"
                ),
                "authorAssociation": "OWNER",
            }
            for i in range(1, 6)
        ],
    )
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: {
        "number": 46, "url": PR_URL, "base_ref": "main",
        "base_oid": "abc", "head_ref": "b", "head_oid": "def",
    })
    config = {
        "run_id": FAKE_RUN_ID, "base_branch": "main",
        "repo_dir": tmp_path,
    }
    with pytest.raises(
        runner.UnrecoverableDeliveryError, match="exhausted",
    ):
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", config, "owner/repo", 39,
            title="task", priority="normal",
        )


# -------------------------------------------------------------------- prompt


def test_prompt_review_covers_unpushed_local_commit():
    """Issue #50 (the #158 `d13b0c56` scene): the review prompt must
    tell the reviewer to push an unpushed local commit (local HEAD
    ahead of the frozen PR head) on the same task branch before
    emitting the verdict — never discard it, never create a
    replacement PR."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent
            / "prompt_review.md").read_text(encoding="utf-8").lower()
    assert "local head" in text
    assert "ahead of the frozen" in text
    assert "push" in text
