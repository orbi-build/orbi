"""Issue #2423: a red, behind-only delivery PR absorbs the base before review.

A delivery PR whose head's checks are RED and whose branch is merely behind
the base (GitHub says MERGEABLE, no conflict) used to run a review session on
that stale head: the review cannot pass on red checks, the merge gate (the
other base absorb) never runs, and the review session may not merge the base
for a conflict-free head. The same red result on the same old head then
repeated and the delivery stopped with `blocked_on_human_decision`.

The review tick itself now absorbs the base first (in code, not by a prompt
instruction): a plain `git merge origin/<base>` and a plain push, no session,
no round counted, no failure comment. The next tick reviews the new head
normally.

These tests drive the real review round (`review_and_merge_if_clean`) on a
REAL local repository with a fake `gh` and a fake `pi` on PATH, so the
absorption, the push and the conflict rollback are genuine git behaviour.
"""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import git
from orbi import config as config_domain
from orbi import run_state
from orbi import scene as scene_mod
import orbi.review_merge as review_merge
from seam import seam
from tests.fakes.pi import install_fake_pi

REPO = "owner/repo"
BRANCH = "orbi/owner-repo-issue-4"
PR_URL = f"https://github.com/{REPO}/pull/4"
RUN_ID = "a1b2c3d4"
FAILED = [{"name": "status-page-speed", "status": "COMPLETED",
           "conclusion": "FAILURE"}]
PASSED = [{"name": "status-page-speed", "status": "COMPLETED",
           "conclusion": "SUCCESS"}]

REVIEW_PI = """#!/usr/bin/env python3
import os, subprocess
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                      capture_output=True, text=True).stdout.strip()
with open("__MARKER__", "w", encoding="utf-8") as handle:
    handle.write(head)
print('REVIEW_VERDICT ' + '{"verdict": "pass", "head": "' + head + '", '
      '"blockers": 0, "majors": 0, "minors": 0, "findings": []}')
"""


def review_pi(marker: Path) -> str:
    """The fake review session: record that it ran and return a pass."""
    return REVIEW_PI.replace("__MARKER__", str(marker))


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """A local bare origin plus a clone whose main has two commits."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    checkout = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)],
        capture_output=True, text=True, check=True,
    )
    git(checkout, "config", "user.email", "pilot@test.local")
    git(checkout, "config", "user.name", "Pilot")
    (checkout / "a.txt").write_text("a", encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "first")
    (checkout / "b.txt").write_text("b", encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "second")
    git(checkout, "push", "origin", "main")
    return checkout


def starts_with(command, prefix) -> bool:
    """True when a recorded command begins with `prefix` (no shape asserts)."""
    return command[: len(prefix)] == prefix


class FakeGh:
    """The runner's `gh` calls and git's network pushes, recorded.

    `rollup` is mutated by a test between ticks (the same list object the
    fake serves), `mergeable` models GitHub's mergeability, and every
    `gh pr merge` lands the given head on origin/main for real.
    """

    def __init__(self, clone: Path, *, rollup: list, mergeable="MERGEABLE"):
        self.clone = clone
        self.rollup = rollup
        self.mergeable = mergeable
        self.pr_state = "OPEN"
        self.merge_commit = None
        # How many upcoming `gh pr view` reads answer a non-object payload
        # (a broken GitHub answer, the Issue #2423 readability rule).
        self.bad_pr_view = 0
        self.issue_comments = []
        self.pr_comments = []
        self.label_edits = []
        self.progress = []
        self.pushes = []

    # --- the two boundaries ----------------------------------------------

    def run_command(self, command, **kwargs):
        if not starts_with(command, ["gh"]):
            # Every non-GitHub command in these scenes is real git.
            return _REAL_COMMAND(command, **kwargs)
        return self._gh(command, **kwargs)

    def run_git_network_command(self, command, **kwargs):
        runner = kwargs.pop("command_runner", None) or _REAL_COMMAND
        if starts_with(command, ["git", "push"]):
            self.pushes.append(list(command))
        return runner(command, **kwargs)

    # --- gh ---------------------------------------------------------------

    def _gh(self, command, **kwargs):
        if command[1] == "auth":
            return (
                "github.com\n"
                "  Logged in to github.com account orbi-bot (keyring)\n"
                "  - Active account: true\n"
            )
        if command[1] == "pr":
            return self._pr(command, **kwargs)
        if command[1] == "issue":
            return self._issue(command, **kwargs)
        if command[1] == "api":
            return self._api(command, **kwargs)
        raise AssertionError(f"unexpected gh command: {command}")

    def _head(self, cwd=None) -> str:
        return git(self.clone if cwd is None else cwd, "rev-parse", "HEAD")

    def _pr(self, command, *, cwd=None, **_kwargs):
        if command[2] == "list":
            return json.dumps([{
                "number": 4, "url": PR_URL,
                "baseRefName": "main",
                "baseRefOid": git(self.clone, "rev-parse", "origin/main"),
                "headRefName": BRANCH,
                "headRefOid": self._head(cwd),
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "owner"},
                "isCrossRepository": False,
                "body": f"<!-- orbi:run={RUN_ID} -->\n\nFixes #4",
            }])
        if command[2] == "view":
            fields = command[command.index("--json") + 1]
            if self.bad_pr_view:
                self.bad_pr_view -= 1
                return "[]"
            if "mergeCommit" in fields:
                return json.dumps({
                    "state": self.pr_state,
                    "mergedAt": ("2026-11-25T00:00:00Z"
                                 if self.merge_commit else None),
                    "mergeCommit": ({"oid": self.merge_commit}
                                    if self.merge_commit else None),
                })
            if "statusCheckRollup" in fields:
                return json.dumps({
                    "state": self.pr_state,
                    "mergeable": self.mergeable,
                    "headRefOid": self._head(cwd),
                    "statusCheckRollup": list(self.rollup),
                })
            return json.dumps({"state": self.pr_state})
        if command[2] == "merge":
            head = command[command.index("--match-head-commit") + 1]
            git(self.clone, "checkout", "main")
            git(self.clone, "merge", "--no-ff", "-m", "Merge PR (fake)", head)
            git(self.clone, "push", "origin", "main")
            git(self.clone, "checkout", "-")
            self.pr_state = "MERGED"
            self.merge_commit = git(self.clone, "rev-parse", "origin/main")
            return ""
        if command[2] == "comment":
            self.pr_comments.append(command[-1])
            return ""
        raise AssertionError(f"unexpected gh pr command: {command}")

    def _issue(self, command, **_kwargs):
        if command[2] == "view":
            fields = command[command.index("--json") + 1]
            if fields == "labels":
                return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
            if fields == "state":
                return json.dumps({"state": "OPEN"})
            if fields == "title":
                return json.dumps({"title": "Review task"})
            return json.dumps({"comments": []})
        if command[2] == "comment":
            self.issue_comments.append(command[-1])
            return ""
        if command[2] == "edit":
            self.label_edits.append(list(command))
            return ""
        raise AssertionError(f"unexpected gh issue command: {command}")

    def _api(self, command, **_kwargs):
        path = command[2]
        if path == f"repos/{REPO}":
            return json.dumps({"allow_merge_commit": True})
        if "/collaborators/" in path:
            return json.dumps({"permission": "admin"})
        if "check-runs" in path:
            return json.dumps([{"total_count": 0, "check_runs": []}])
        if "--method" in command:
            method = command[command.index("--method") + 1]
            if method == "DELETE":
                return ""
            if method == "PUT":
                return json.dumps([{"name": "ai-pr-opened"}])
            body = command[command.index("--field") + 1][len("body="):]
            if method == "POST":
                self.progress.append(body)
                return json.dumps({"id": 77, "body": body})
            self.progress.append(body)
            return ""
        return json.dumps([])


_REAL_COMMAND = seam.run_command


def install(monkeypatch, fake: FakeGh) -> None:
    """Wire the one gh boundary and the one git network boundary."""
    monkeypatch.setattr(seam, "run_command", fake.run_command)
    monkeypatch.setattr(
        seam, "run_git_network_command", fake.run_git_network_command,
    )


def prompt_files(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("implement", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("review", encoding="utf-8")


def start_delivery(clone: Path, *, file="delivery.txt",
                   content="delivery", behind=True) -> str:
    """Create, commit and push the delivery branch; return its head.

    `behind` starts the branch one commit behind origin/main (the scene the
    ticket is about); the caller has already added the base's own commit.
    """
    start = "origin/main~1" if behind else "origin/main"
    git(clone, "checkout", "-b", BRANCH, start)
    (clone / file).write_text(content, encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "delivery")
    head = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "origin", f"HEAD:{BRANCH}")
    return head


def run_tick(clone: Path, tmp_path: Path) -> bool:
    """One review tick of the delivery, on the real clone."""
    config = config_domain.RunnerConfig(
        repo_dir=clone, deploy_home=tmp_path / "deploy",
        base_branch="main", base_sha="b1", run_id=RUN_ID,
        prompt=tmp_path / "prompt.md",
        prompt_review=tmp_path / "prompt_review.md",
    )
    run_state.write_run_state(run_state.RunContext(
        run_id=RUN_ID, issue=4, branch=BRANCH, worktree=clone,
        source_repo=REPO,
    ))
    return review_merge.review_and_merge_if_clean(
        clone, BRANCH, "main", config, REPO, 4,
        title="Review task", priority="normal",
        scene=run_state.ResumeScene(
            parsed=scene_mod.Scene(
                run_id=RUN_ID, base_branch="main", base_sha="b1",
                pr_url=PR_URL,
            ),
            scene_at="2026-11-25T00:00:00Z",
        ),
    )


# --------------------------------------------------------------------------
# Acceptance 1: absorb the base first, review the new head next tick
# --------------------------------------------------------------------------


def test_red_behind_head_absorbs_the_base_without_a_review_session(
        clone, tmp_path, monkeypatch, caplog,
):
    prompt_files(tmp_path)
    git(clone, "commit", "--allow-empty", "-m", "base fix")
    git(clone, "push", "origin", "main")
    base_commit = git(clone, "rev-parse", "origin/main")
    start_delivery(clone)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=list(FAILED))
    install(monkeypatch, fake)
    caplog.set_level("INFO")

    assert run_tick(clone, tmp_path) is False

    # The base was merged in with a plain merge commit and pushed plainly.
    head = git(clone, "rev-parse", "HEAD")
    git(clone, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert git(clone, "rev-parse", "HEAD^2") == base_commit
    assert git(clone, "rev-parse", f"origin/{BRANCH}") == head
    assert fake.pushes == [["git", "push", "origin", f"HEAD:{BRANCH}"]]
    # The tick's own push joins the run's engine push history (#833).
    assert run_state.read_run_state(clone)["pushed_head"] == head
    # No review session, no round, no failure comment, no label change.
    assert not marker.exists()
    assert fake.issue_comments == []
    assert fake.label_edits == []
    assert "review_base_absorbed" in caplog.text

    # The next tick, on the absorbed head with green checks, reviews and
    # merges normally.
    fake.rollup[:] = PASSED
    assert run_tick(clone, tmp_path) is True
    assert marker.exists()
    assert fake.pr_state == "MERGED"


def test_behind_head_without_a_failing_check_keeps_the_review_path(
        clone, tmp_path, monkeypatch,
):
    """Only a FAILED check redirects the tick: a behind head whose checks
    are still running keeps the ordinary review path (the session, then the
    merge gate's CI-pending deferral)."""
    prompt_files(tmp_path)
    git(clone, "commit", "--allow-empty", "-m", "base fix")
    git(clone, "push", "origin", "main")
    start_delivery(clone)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=[{"name": "tests",
                                 "status": "IN_PROGRESS"}])
    install(monkeypatch, fake)

    assert run_tick(clone, tmp_path) is False

    assert marker.exists()
    assert fake.issue_comments == []
    assert fake.label_edits == []


def test_unreadable_pr_state_never_authorizes_an_absorb(
        clone, tmp_path, monkeypatch,
):
    """A PR read that answers a non-object payload authorizes no absorb
    (today's behaviour): the tick falls back to the normal review path
    instead of failing or merging the base blind."""
    prompt_files(tmp_path)
    git(clone, "commit", "--allow-empty", "-m", "base fix")
    git(clone, "push", "origin", "main")
    start_delivery(clone)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=list(FAILED))
    fake.bad_pr_view = 1
    install(monkeypatch, fake)

    assert run_tick(clone, tmp_path) is False

    assert marker.exists()
    assert fake.issue_comments == []


# --------------------------------------------------------------------------
# Acceptance 2: a head that already contains the base is unchanged
# --------------------------------------------------------------------------


def test_red_fresh_head_still_runs_the_review_session(
        clone, tmp_path, monkeypatch,
):
    prompt_files(tmp_path)
    start_delivery(clone, behind=False)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=list(FAILED))
    install(monkeypatch, fake)

    assert run_tick(clone, tmp_path) is False

    # Unchanged: the session runs on the red head and the merge gate's CI
    # block hands the delivery to the next review round.
    assert marker.exists()
    assert fake.pushes == []
    assert any("ai-fix-needed" in edit for edit in fake.label_edits)
    assert any("CI check 'status-page-speed'" in body
               for body in fake.issue_comments)


# --------------------------------------------------------------------------
# Acceptance 3: a CONFLICTING PR keeps the existing conflict path
# --------------------------------------------------------------------------


def test_conflicting_pr_keeps_the_existing_conflict_path(
        clone, tmp_path, monkeypatch,
):
    prompt_files(tmp_path)
    git(clone, "commit", "--allow-empty", "-m", "base fix")
    git(clone, "push", "origin", "main")
    original = start_delivery(clone)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=list(FAILED), mergeable="CONFLICTING")
    install(monkeypatch, fake)

    assert run_tick(clone, tmp_path) is False

    # Unchanged: the review session runs with the conflict arm armed and no
    # base absorb happens in the tick.
    assert marker.exists()
    assert fake.pushes == []
    assert git(clone, "rev-parse", "HEAD") == original


# --------------------------------------------------------------------------
# Acceptance 4: a local merge conflict rolls back and takes the failure path
# --------------------------------------------------------------------------


def test_local_absorb_conflict_rolls_back_and_marks_the_delivery(
        clone, tmp_path, monkeypatch, caplog,
):
    prompt_files(tmp_path)
    # Both sides change the same file: the plain merge conflicts locally.
    (clone / "shared.txt").write_text("base", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base change")
    git(clone, "push", "origin", "main")
    git(clone, "checkout", "-b", BRANCH, "origin/main~1")
    (clone / "shared.txt").write_text("delivery", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "delivery change")
    original = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "origin", f"HEAD:{BRANCH}")
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, review_pi(marker))
    fake = FakeGh(clone, rollup=list(FAILED))
    install(monkeypatch, fake)
    caplog.set_level("INFO")

    assert run_tick(clone, tmp_path) is False

    # Rolled back: the original head, a clean worktree, nothing pushed.
    assert git(clone, "rev-parse", "HEAD") == original
    assert git(clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert fake.pushes == []
    assert git(clone, "rev-parse", f"origin/{BRANCH}") == original
    # The existing conflict path: ai-fix-needed and the base-advance
    # failure comment on the Issue, no review session.
    assert not marker.exists()
    assert any("ai-fix-needed" in edit for edit in fake.label_edits)
    assert any("cannot absorb origin/main" in body
               for body in fake.issue_comments)
    assert "base_merge_conflict" in caplog.text
