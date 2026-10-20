"""Delivery and claim scaffolding (Issue #1648).

Moved verbatim out of `tests/test_bootstrap_runner.py` when the delivery
and claim tests moved to `tests/test_bootstrap_delivery.py`: the PR
verification/delivery fakes (`fake_verify_run`, `fake_deliver_run` and the
payloads they answer with), the review-round environment, the blocked
review text, and the real-git worktree/run-state fixtures. The scenes that
still need them - the new suite and `test_bootstrap_runner.py`
(`_git_output`, `make_session_jsonl`, `PR_URL`) - import them from here
instead of keeping a copy (one owner).
"""
from orbi import config as config_domain

import fcntl
import json
import os
import subprocess

import pytest

import orbi.failure_report as failure_report
import orbi.review_merge as review_merge
import orbi.gitops as gitops
from tests.fakes.gitops import remote_head_answer
import orbi.journal as journal
from seam import seam


RELEASE_663 = {
    "number": 254, "title": "release", "body": "",
    "labels": [{"name": "ai-ready"}, {"name": "ai-release"}],
    "blockedBy": {"nodes": [], "totalCount": 0},
    "milestone": {"title": "v0.4.2"},
}


def _release_scan_fake(api_result):
    """Return a `run_command` fake: empty ordinary scans, the release
    in the fallback scan, and `api_result` (a string or an exception)
    for the Milestone counter query."""

    def fake_run(command, **kwargs):
        if command[1] == "api":
            if isinstance(api_result, Exception):
                raise api_result
            return api_result
        search = command[command.index("--search") + 1]
        if "label:ai-release" in search:
            return json.dumps([RELEASE_663])
        return json.dumps([])

    return fake_run


def _probe_lock_free(repo_dir: Path) -> bool:
    """True when a non-blocking probe acquires the base-sync lock
    (i.e. the lock is FREE); False while it is held."""
    lock_path = gitops.base_sync_lock_path(repo_dir)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


# Real-git scaffolding shared with the release domain suite.
# tests/test_bootstrap_release.py imports both helpers from here
# instead of keeping a copy (one owner, Issue #1645).
def make_local_remote_pair(tmp_path):
    """A real local 'origin' bare remote + a working clone with one commit."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(work)],
        check=True, capture_output=True,
    )
    for key, value in (
        ("user.email", "t@t"), ("user.name", "t"),
        ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(work), "config", key, value],
            check=True, capture_output=True,
        )
    (work / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "f.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "one"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "main"],
        check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return work, head


def _git_output(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_session_jsonl(worktree: Path, session_id: str = "sess-1") -> Path:
    """A minimal previous session file (Issue #219 resume context)."""
    session_dir = worktree / ".pi-session"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "id": session_id}) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-02T04:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "bash",
                             "arguments": {"command": "pytest tests/"}}],
            },
        }) + "\n"
        + json.dumps({
            "type": "message", "timestamp": "2026-09-02T04:01:00Z",
            "message": {"role": "toolResult", "toolName": "bash",
                        "isError": False},
        }) + "\n",
        encoding="utf-8",
    )
    return path


FAKE_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"

# Issue #1581: the commit-boundary tree comparison needs a delivered head
# tree distinct from the frozen base tree on the happy path.
FAKE_HEAD_TREE_SHA = "1111111111111111111111111111111111111111"

FAKE_BASE_TREE_SHA = "2222222222222222222222222222222222222222"

FAKE_RUN_ID = "e07383c2"


FAKE_PR_URL = "https://github.com/orbi-build/orbi/pull/4"

FAKE_PR_REPO = "orbi-build/orbi"


def fake_verify_pr_payload(**overrides) -> str:
    """One open PR in the production `gh pr list` shape."""
    payload = {
        "url": FAKE_PR_URL,
        "baseRefName": "main",
        "headRefName": f"orbi/issue-4-{FAKE_RUN_ID}",
        "headRefOid": FAKE_HEAD_SHA,
        "headRepository": {"name": "orbi"},
        "headRepositoryOwner": {"login": "orbi-build"},
        "body": (
            f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n"
            "Fixes #4\n\nPlan"
        ),
    }
    payload.update(overrides)
    return json.dumps([payload])


def fake_verify_run(command, **kwargs):
    """Complete fake for verify_pr: git commands answered, gh returns a PR."""
    if command[:3] == ["git", "branch", "--show-current"]:
        return f"orbi/issue-4-{FAKE_RUN_ID}"
    if command[:3] == ["git", "fetch", "origin"]:
        return ""
    if command[:3] == ["git", "merge-base", "--is-ancestor"]:
        return ""
    if command[:3] == ["git", "rev-parse", "HEAD"]:
        return FAKE_HEAD_SHA
    if command[0] == "gh" and command[1] == "issue":
        # Issue #1300: a body without the current marker makes the
        # check read the Issue's TRUSTED comment history on the deliver
        # path too. The empty history keeps these rejections about the
        # body's marker — the read happened and knew no run of the line
        # — instead of about a call the fake refused to answer.
        return json.dumps({"comments": []})
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
    raise AssertionError(f"unexpected command: {command}")


def _fake_takeover_view(monkeypatch, payload):
    """Fake the `gh pr view` of `external_takeover_pr` with `payload`."""
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: json.dumps(payload),
    )


# --- delivery lifecycle: slot held until merge or terminal failure ----------

PR_URL = "https://github.com/owner/repo/pull/46"


def fake_pr_view(monkeypatch, state: str) -> tuple[list, object]:
    """Answer `gh pr view <n> --repo owner/repo with delivery fields`.

    Returns the command log and the fake itself (so a test can prove
    the fake rejects unexpected commands).
    """
    seen: list = []

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"] and command[1] == "pr" \
                and command[2] == "view":
            seen.append(command)
            assert command[3] == "46"
            assert command[4:] == [
                "--repo", "owner/repo", "--json",
                "state,statusCheckRollup",
            ]
            return json.dumps({"state": state, "statusCheckRollup": []})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    return seen, fake_run


# ---------------------------------------------------- Issue #289
# `_run_review_round` is the per-round delivery flow extracted from
# `delivery_step` (one round = label read/repair + resumable gate
# + one independent review + failure classification). These tests pin
# the three-value contract the polling skeleton dispatches on:
# True = merged this round, False = findings (next round), None = a
# terminal state was already handled (slot released).


def _review_round_env(
    monkeypatch, tmp_path, *, scene_base="main", label="ai-pr-opened",
    with_worktree=True, review_result=False, scene_round=0,
    external="", record=None, review_raises=None,
):
    """Shared fake GitHub/git scene for one direct `_run_review_round`
    call. Returns (edits, comments, reviews, publishes) capture lists;
    `edits` carries the `edit_issue` label patches, `publishes` the
    `_safe_publish` progress calls."""
    def fake_run(command, **kwargs):
        if command[-1] == "comments":
            # The trusted scene comment.
            return json.dumps({"comments": [{
                "body": (
                    "<!-- orbi:run=a1b2c3d4 -->\n"
                    + ("<!-- orbi:scene:v1 " + json.dumps({
                        "base_branch": scene_base,
                        "base_sha": "abc123def456",
                        "external": external, "pr_url": PR_URL,
                        "review_round": scene_round,
                        "run_id": "a1b2c3d4", "schema": 1,
                    }) + " -->\n" if scene_round else "")
                    + "Orbi opened PR: "
                    f"{PR_URL} (base_branch={scene_base} "
                    "base_sha=abc123def456 run_id=a1b2c3d4)"
                ),
                "authorAssociation": "OWNER",
                # Issue #1806: trust is the author's repository
                # permission, so the scene names a writer and the
                # permission endpoint answers below.
                "author": {"login": "maintainer"},
            }]})
        if command[-1] == "labels":
            return json.dumps({"labels": [{"name": label}]})
        if command == ["git", "branch", "--show-current"]:
            return "orbi/owner-repo-issue-39"
        raise AssertionError(f"unexpected command: {command}")

    # The fake rejects anything it does not implement.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    if with_worktree:
        worktree = (tmp_path / ".worktrees"
                    / "orbi-owner-repo-issue-39-a1b2c3d4")
        worktree.mkdir(parents=True)
        if record is not None:
            (worktree / ".orbi").mkdir(parents=True, exist_ok=True)
            (worktree / ".orbi" / "run-state.json").write_text(
                json.dumps({
                    "run_id": "a1b2c3d4", "issue": 39, "repo": "owner/repo",
                    "branch": "orbi/owner-repo-issue-39",
                    "worktree": str(worktree),
                    "reviewed_head": record[0],
                    "reviewed_issue_body_sha256": record[1],
                }),
                encoding="utf-8",
            )
    edits: list = []
    comments: list = []
    monkeypatch.setattr(seam, "edit_issue",
        lambda *args, **kwargs: edits.append((args, kwargs)),
    )
    monkeypatch.setattr(seam, "comment_issue",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    monkeypatch.setattr(
        failure_report, "comment_pr",
        lambda *args, **kwargs: comments.append((args, kwargs)),
    )
    reviews: list = []

    def fake_review(*args, **kwargs):
        reviews.append((args, kwargs))
        if review_raises is not None:
            raise review_raises
        return review_result

    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean", fake_review,
    )
    publishes: list = []
    monkeypatch.setattr(seam, "_safe_publish",
        lambda **kwargs: publishes.append(kwargs),
    )
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "a1b2c3d4")
    return edits, comments, reviews, publishes


# The blocked-review comment body in both languages (Issue #1701): the
# Action line must say how to resume (remove `ai-blocked` BEFORE adding
# `ai-fix-needed` — the claim scans skip `ai-blocked` and the budget reset
# needs it removed first), and the Reason line is one full catalog sentence.
BLOCKED_REVIEW_TEXT = {
    "en": {
        "action": "**Action:**",
        "reason": "**Reason:**",
        "rounds_action": "fresh review-round budget",
        "rounds_reason": "used every one of its review rounds",
        "human_reason": "requires a human decision",
        "generic_action": "changing labels alone does not change it",
        "step": "remove `ai-blocked`, then add `ai-fix-needed`",
        "close": "close the PR and this Issue",
        "budget": "budget",
    },
    "zh": {
        "action": "**需要您做的操作：**",
        "reason": "**原因：**",
        "rounds_action": "评审预算",
        "rounds_reason": "评审轮次",
        "human_reason": "需要人工决定",
        "generic_action": "只改标签不会改变它",
        "step": "移除 `ai-blocked`，再添加 `ai-fix-needed`",
        "close": "关闭该 PR 和这个 Issue",
        "budget": "预算",
    },
}


def _blocked_line(body: str, label: str) -> str:
    """The one comment line starting with a rendered field label."""
    return next(line for line in body.splitlines() if line.startswith(label))


def _blocked_review_body(monkeypatch, tmp_path, code, **env):
    """The Issue comment one blocking review failure posts in that language."""
    edits, comments, _, _ = _review_round_env(monkeypatch, tmp_path, **env)
    outcome = review_merge._run_review_round(
        PR_URL, {"number": 39, "title": "task", "body": ""},
        config_domain.RunnerConfig(
            repo_dir=tmp_path, base_branch="main", message_language=code,
        ),
        "owner/repo",
    )
    assert outcome is None
    assert edits[0][1]["add"] == "ai-blocked"
    return comments[0][1]["body"]


# --- Issue #186: deliver_pr — the Runner owns the deterministic closeout ----

DELIVER_BRANCH = f"orbi/issue-4-{FAKE_RUN_ID}"


def fake_deliver_run(command, **kwargs):
    """Complete fake for deliver_pr: git commands answered, gh returns
    the PR of the task branch (the production `gh pr list` shape)."""
    if command[:3] == ["git", "branch", "--show-current"]:
        return DELIVER_BRANCH
    if command[:3] == ["git", "status", "--porcelain"]:
        return ""
    if command[:3] == ["git", "rev-parse", "HEAD"]:
        return FAKE_HEAD_SHA
    # Issue #1581: the commit-boundary tree comparison. The delivered
    # head tree differs from the frozen base tree, so the happy path has
    # a real file change (a distinct constant keeps the two apart).
    if command[0:3] == ["git", "rev-parse", "HEAD^{tree}"]:
        return FAKE_HEAD_TREE_SHA
    if (command[0:2] == ["git", "rev-parse"]
            and command[2].endswith("^{tree}")):
        return FAKE_BASE_TREE_SHA
    if command[:3] == ["git", "fetch", "origin"]:
        return ""
    if command[:3] == ["git", "merge-base", "--is-ancestor"]:
        return ""
    if command[:3] == ["git", "merge", "origin/main"]:
        return ""
    if command[:3] == ["git", "merge", "--abort"]:
        return ""
    if command[:3] == ["git", "push", "origin"]:
        return ""
    # Issue #898: the pushed head is read from the remote, never from a
    # local remote-tracking ref the fetch refspec may not cover (a
    # `--single-branch` clone).
    answer = remote_head_answer(command, FAKE_HEAD_SHA)
    if answer is not None:
        return answer
    if command[:3] == ["gh", "issue", "view"] and command[-1] == "state":
        # Issue #746: the pre-PR closeout reads the source Issue state;
        # the default scene is an open Issue.
        return json.dumps({"state": "OPEN"})
    if command[:2] == ["gh", "pr"]:
        return fake_verify_pr_payload()
    raise AssertionError(f"unexpected command: {command}")


def _seed_deliver_run_state(worktree, **extra):
    """The claim-time run state file every real delivery carries before
    `deliver_pr` runs (the engine push history records into it, #833)."""
    state = {
        "run_id": FAKE_RUN_ID,
        "issue": 4,
        "repo": "o/r",
        "branch": DELIVER_BRANCH,
        "worktree": str(worktree),
    }
    state.update(extra)
    (worktree / ".orbi").mkdir(parents=True, exist_ok=True)
    (worktree / ".orbi" / "run-state.json").write_text(
        json.dumps(state) + "\n", encoding="utf-8",
    )


# --- Issue #256: Runner-owned runtime state isolation ----------------------


def _make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real git repo plus one linked task worktree (the exact shape
    `create_worktree` produces: `.git` is a pointer file)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "base"],
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "task",
         str(wt), "HEAD"],
        check=True,
    )
    return repo, wt
