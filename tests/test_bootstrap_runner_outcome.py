"""Delivery outcome scene: success, blocked and health records (Issue #2008).

Scene split out of the former `tests/test_bootstrap_runner.py` (now gone),
moved verbatim with no behavior change: what `process_issue` records on a
delivered run (base/run comment, `run_end`), what a failure marks blocked
(missing commit, empty commit, terminal failure, dispatch label), and the
runner health records/streak breaks plus the bypass when reporting a health
record itself fails.

`_gh_api` (the shared fake for the progress publisher's `gh api` traffic)
lives here; other suites import it from this module.
"""

from orbi import config as config_domain
import json

import os
import subprocess
from unittest.mock import Mock

import pytest

import orbi.claim as claim
import orbi.failure_report as failure_report
import orbi.runner as runner
import orbi.pi_session as pi_session
import orbi.github as github
from orbi import failure
from tests.fakes.github import FakeGh
from tests.fakes.gitops import remote_head_answer
from tests.test_progress_wiring import make_fake_gh
import orbi.journal as journal
from seam import seam
from tests.fakes.bootstrap_delivery import _git_output


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
        # Worktree creation fails first, so the restart-resume scan
        # (Issue #18) never runs: the label read and the failing comment
        # POST are the only traffic this fake accepts.
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    # The fake rejects anything that is not issue traffic.
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
