"""Issue #2465: a PR still in review follows the repository to its new base.

A repository's `base_branch` moves while its delivery PR is in review. Since
Issue #2458 that scene stopped the delivery (`base_branch_changed`,
`ai-blocked`, `decide`); Issue #2465 reverses that: the PR follows the
repository — it is moved to the configured base and that base is merged into
its branch — and review and merge continue the way they already do whenever
the base moves.

The scenes here are real repositories (a local bare origin plus a clone, the
delivery worktree created by `git worktree add`) with GitHub answered by the
in-memory `FakeGh`: the move is git-heavy (fetch, merge, push), so git must
be real for the acceptance to mean anything. What the tests assert is what
the person sees:

- the PR's base (`gh prs[<n>]["baseRefName"]`), the Issue's labels and the
  comments the delivery writes;
- the new base is actually merged into the delivery branch (the merge commit
  carries it), so the delivery then merges into the new base;
- the move is idempotent: every later resume/round of the same delivery (the
  scene comment keeps the base it was opened with) keeps going and writes
  nothing about the switch;
- the failure paths: a conflicting new base is the SAME failure the
  behind-base path raises, and a base branch the repository does not have
  keeps `base_branch_missing`.
"""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import git

import orbi.base_absorb as base_absorb
import orbi.base_branch_change as base_branch_change
import orbi.pr_verification as pr_verification
import orbi.resume_verification as resume_verification
import orbi.review_merge as review_merge
import orbi.run_state as run_state
from orbi import config as config_domain
from orbi import scene as scene_mod
from orbi.gitops import BaseBranchMissingError

from tests.fakes.bootstrap_delivery import install_real_git_gh_seam
from tests.fakes.github import FakeGh


REPO = "owner/repo"
ISSUE_NUMBER = 9
PR_NUMBER = 9
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
RUN_ID = "a1b2c3d4"
BRANCH = f"orbi/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}"


def _scene(base_branch: str, base_sha: str) -> scene_mod.Scene:
    return scene_mod.Scene(
        run_id=RUN_ID, base_branch=base_branch, base_sha=base_sha,
        pr_url=PR_URL,
    )


def _adopt(worktree: Path, clone: Path, *, base_branch: str, base_sha: str,
           configured: str) -> bool:
    return base_branch_change.adopt_configured_base(
        worktree, branch=BRANCH, source_repo=REPO,
        frozen=_scene(base_branch, base_sha),
        configured_base_branch=configured, repo_dir=clone,
    )


def run_state_worktree(clone: Path) -> Path:
    """The worktree the resume derives for this scene's run id."""
    import orbi.runner as runner

    return runner.worktree_path(clone, REPO, ISSUE_NUMBER, RUN_ID)


@pytest.fixture()
def repo(tmp_path):
    """A local bare origin, a clone and the delivery worktree.

    Branches: `old` (the base the PR was opened against), `main` (the NEW
    base, which moved on independently of `old`) and the delivery branch
    `orbi/owner-repo-issue-9` cut from `old` with one commit that adds
    `delivery.txt`.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    (clone / "base.txt").write_text("the repository\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "the repository starts")
    # The base the PR was opened against.
    git(clone, "checkout", "-b", "old")
    (clone / "old.txt").write_text("the old base\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "the old base")
    # The delivery: cut from the old base, one commit of its own.
    git(clone, "checkout", "-b", BRANCH, "old")
    (clone / "delivery.txt").write_text("delivered\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "delivery")
    # The new base moves on independently of the old one.
    git(clone, "checkout", "main")
    (clone / "main.txt").write_text("the new base moved on\n",
                                    encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "the new base moved on")
    git(clone, "push", "origin", "main", "old", BRANCH)
    git(clone, "checkout", "main")
    # The worktree lives where the resume DERIVES it (repo_dir + repo +
    # Issue + run id), so the resume path finds it instead of recreating it.
    worktree = run_state_worktree(clone)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(clone, "worktree", "add", str(worktree), BRANCH)
    return {
        "clone": clone, "origin": origin, "worktree": worktree,
        "main": git(clone, "rev-parse", "main"),
        "old": git(clone, "rev-parse", "old"),
        "delivery": git(worktree, "rev-parse", "HEAD"),
    }


def _fake_gh(repo, *, base: str = "old") -> FakeGh:
    fake = FakeGh(REPO)
    fake.add_pr(
        PR_NUMBER, head=BRANCH, base=base, oid=repo["delivery"],
        url=PR_URL, body=f"<!-- orbi:run={RUN_ID} -->\n\nFixes #{ISSUE_NUMBER}",
    )
    fake.add_branch("main", head=repo["main"])
    fake.add_issue(
        ISSUE_NUMBER, title="ship", body="", labels=("ai-pr-opened",),
    )
    fake.add_collaborator("maintainer", "write")
    return fake


def test_the_pr_follows_the_new_base_and_that_base_is_merged_in(
    allow_external_commands, repo, monkeypatch, caplog,
):
    """Acceptance 1: the PR ends up targeting the new base, the new base is
    merged into the delivery branch (so the delivery then merges into it),
    and the push reaches the remote delivery branch."""
    caplog.set_level("INFO")
    fake = _fake_gh(repo, base="old")
    extra = install_real_git_gh_seam(monkeypatch, fake)

    moved = _adopt(
        repo["worktree"], repo["clone"], base_branch="old",
        base_sha=repo["old"], configured="main",
    )

    assert moved is True
    # What the person sees on the PR: its base is the new one.
    assert fake.prs[PR_NUMBER]["baseRefName"] == "main"
    # The new base IS in the delivery branch (the merge commit carries it).
    assert git(repo["worktree"], "merge-base", "--is-ancestor",
               repo["main"], "HEAD") == ""
    parents = git(repo["worktree"], "log", "-1", "--format=%P").split()
    assert len(parents) == 2
    assert repo["main"] in parents
    # The delivery branch itself was pushed, and the base branch did not
    # absorb the delivery: the merge happens when the delivery merges.
    assert git(repo["origin"], "rev-parse", f"refs/heads/{BRANCH}") == (
        git(repo["worktree"], "rev-parse", "HEAD")
    )
    assert git(repo["origin"], "rev-parse", "refs/heads/main") == repo["main"]
    # The move is the ONE GitHub write; the merge of the new base into the
    # delivery branch is journaled.
    assert any(
        command[:3] == ["gh", "pr", "edit"] and command[-1] == "main"
        for command in extra["commands"]
    )
    assert "base_absorbed base_branch=main" in caplog.text


def test_a_second_round_does_not_move_the_pr_again(
    allow_external_commands, repo, monkeypatch,
):
    """Acceptance 2: the scene comment keeps the base the PR was OPENED with,
    so every later round of the same delivery runs the adopt again — it must
    keep going and write nothing (GitHub's own PR state decides)."""
    fake = _fake_gh(repo, base="old")
    state = install_real_git_gh_seam(monkeypatch, fake)
    _adopt(repo["worktree"], repo["clone"], base_branch="old",
           base_sha=repo["old"], configured="main")
    head = git(repo["worktree"], "rev-parse", "HEAD")
    state["commands"].clear()

    moved = _adopt(repo["worktree"], repo["clone"], base_branch="old",
                   base_sha=repo["old"], configured="main")

    assert moved is False
    assert git(repo["worktree"], "rev-parse", "HEAD") == head
    assert fake.prs[PR_NUMBER]["baseRefName"] == "main"
    # One read decided it: no fetch, no edit, no merge or push.
    assert state["commands"] == [[
        "gh", "pr", "view", str(PR_NUMBER), "--repo", REPO,
        "--json", "state,baseRefName",
    ]]


def test_a_decided_pr_is_never_retargeted(
    allow_external_commands, repo, monkeypatch,
):
    """A PR that is no longer OPEN is not a PR still in review. A merged one
    refuses a base change at GitHub, so retargeting it turned the merged
    closeout into a repeating failure; a withdrawn one delivers nothing any
    more. Either way the move is skipped and the caller's closed routing
    keeps owning the PR."""
    fake = _fake_gh(repo, base="old")
    fake.prs[PR_NUMBER]["state"] = "MERGED"
    state = install_real_git_gh_seam(monkeypatch, fake)

    assert _adopt(repo["worktree"], repo["clone"], base_branch="old",
                  base_sha=repo["old"], configured="main") is False

    assert fake.prs[PR_NUMBER]["baseRefName"] == "old"
    assert git(repo["worktree"], "rev-parse", "HEAD") == repo["delivery"]
    # One read decided it: no fetch, no edit, no merge, no push.
    assert state["commands"] == [[
        "gh", "pr", "view", str(PR_NUMBER), "--repo", REPO,
        "--json", "state,baseRefName",
    ]]


def test_a_conflicting_new_base_is_the_behind_base_failure(
    allow_external_commands, repo, monkeypatch, caplog,
):
    """The failure path: merging the new base conflicts. That is not a stop
    and not a resolution: the merge is aborted, the head is untouched, and
    the SAME `BaseMergeConflict` the behind-base path raises travels to the
    caller (which reports it and keeps the Issue in the fix loop). The PR is
    already on the new base, so the next tick continues instead of stopping."""
    clone, worktree = repo["clone"], repo["worktree"]
    # A new base that edits the SAME file the delivery edits.
    git(clone, "checkout", "-b", "conflicting", "main")
    (clone / "delivery.txt").write_text("the base wrote this\n",
                                        encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "the base edits the delivered file")
    git(clone, "push", "origin", "conflicting")
    git(clone, "checkout", "main")
    fake = _fake_gh(repo, base="old")
    install_real_git_gh_seam(monkeypatch, fake)
    head_before = git(worktree, "rev-parse", "HEAD")
    caplog.set_level("INFO")

    with pytest.raises(base_absorb.BaseMergeConflict):
        _adopt(worktree, clone, base_branch="old", base_sha=repo["old"],
               configured="conflicting")

    # The head and the remote branch are exactly as they were…
    assert git(worktree, "rev-parse", "HEAD") == head_before
    assert git(repo["origin"], "rev-parse", f"refs/heads/{BRANCH}") == (
        repo["delivery"]
    )
    # …the conflict is the behind-base evidence…
    assert "base_merge_conflict" in caplog.text
    assert "conflicting" in caplog.text
    # …and the PR is already on the new base, so the next tick continues.
    assert fake.prs[PR_NUMBER]["baseRefName"] == "conflicting"
    assert _adopt(worktree, clone, base_branch="old", base_sha=repo["old"],
                  configured="conflicting") is False


def test_a_missing_new_base_keeps_its_own_failure(
    allow_external_commands, repo, monkeypatch,
):
    """Acceptance 4, unchanged: a configured base branch the repository does
    not have is `base_branch_missing` (a human's decision point), and the PR
    is left where it is."""
    fake = _fake_gh(repo, base="old")
    install_real_git_gh_seam(monkeypatch, fake)

    with pytest.raises(BaseBranchMissingError):
        _adopt(repo["worktree"], repo["clone"], base_branch="old",
               base_sha=repo["old"], configured="does-not-exist")

    assert fake.prs[PR_NUMBER]["baseRefName"] == "old"


def test_the_resume_verification_follows_the_base_instead_of_stopping(
    allow_external_commands, repo, monkeypatch,
):
    """Acceptance 1 on the resume path: the delivery resumes after the
    configured base changed. The resume moves the PR to the new base and
    merges it in, then verifies the PR against it — no `ai-blocked` label and
    no `base_branch_changed` failure comment is written."""
    clone, worktree = repo["clone"], repo["worktree"]
    fake = _fake_gh(repo, base="old")
    install_real_git_gh_seam(monkeypatch, fake)
    verified: list = []
    monkeypatch.setattr(
        pr_verification, "verify_pr",
        lambda ctx, base_branch, **kwargs: (
            verified.append(base_branch) or PR_URL
        ),
    )

    url = resume_verification.verify_resumed_pr(
        run_state.ResumeScene(
            parsed=_scene("old", repo["old"]), scene_at=None,
        ),
        {"number": ISSUE_NUMBER, "title": "ship", "body": ""},
        config_domain.RunnerConfig(repo_dir=clone, base_branch="main"),
        REPO,
    )

    assert url == PR_URL
    assert verified == ["main"]
    assert fake.prs[PR_NUMBER]["baseRefName"] == "main"
    assert git(worktree, "merge-base", "--is-ancestor", repo["main"], "HEAD"
               ) == ""
    # What the person sees on the Issue: no blocked transition, no failure
    # comment, and the delivery label it had is untouched.
    assert fake.issues[ISSUE_NUMBER]["labels"] == ["ai-pr-opened"]
    assert fake.issues[ISSUE_NUMBER]["comments"] == []


def test_later_resumes_of_the_same_delivery_keep_going(
    allow_external_commands, repo, monkeypatch,
):
    """Acceptance 2 on the resume path: the scene still names the OLD base on
    every later resume, and each of them keeps going — no stop, and no second
    switch (no PR edit, no merge) once the PR is on the new base."""
    clone = repo["clone"]
    fake = _fake_gh(repo, base="old")
    state = install_real_git_gh_seam(monkeypatch, fake)
    monkeypatch.setattr(
        pr_verification, "verify_pr", lambda ctx, base_branch, **kw: PR_URL,
    )
    resume = run_state.ResumeScene(
        parsed=_scene("old", repo["old"]), scene_at=None,
    )
    config = config_domain.RunnerConfig(repo_dir=clone, base_branch="main")
    issue = {"number": ISSUE_NUMBER, "title": "ship", "body": ""}

    assert resume_verification.verify_resumed_pr(
        resume, issue, config, REPO) == PR_URL
    state["commands"].clear()
    assert resume_verification.verify_resumed_pr(
        resume, issue, config, REPO) == PR_URL

    assert fake.prs[PR_NUMBER]["baseRefName"] == "main"
    assert fake.issues[ISSUE_NUMBER]["labels"] == ["ai-pr-opened"]
    assert fake.issues[ISSUE_NUMBER]["comments"] == []
    # Nothing moved the PR a second time and nothing merged the base again.
    second = state["commands"]
    assert not [command for command in second if command[:3] == [
        "gh", "pr", "edit"]]
    assert not [command for command in second if command[:3] == [
        "git", "merge", "origin/main"]]


def test_the_review_round_follows_the_base_and_reviews_on_it(
    allow_external_commands, repo, monkeypatch,
):
    """Acceptance 1 on the review path: the round finds the PR on the base
    the scene was opened with, moves it to the configured base, and reviews
    THAT base. The Issue keeps its delivery label and gets no
    `base_branch_changed` comment."""
    clone = repo["clone"]
    fake = _fake_gh(repo, base="old")
    fake.comment(
        ISSUE_NUMBER,
        f"<!-- orbi:run={RUN_ID} -->\n"
        + scene_mod.render(_scene("old", repo["old"]))
        + f"\nOrbi opened PR: {PR_URL}",
    )
    install_real_git_gh_seam(monkeypatch, fake)
    reviews: list = []
    monkeypatch.setattr(
        review_merge, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )

    outcome = review_merge._run_review_round(
        PR_URL, {"number": ISSUE_NUMBER, "title": "ship", "body": ""},
        config_domain.RunnerConfig(repo_dir=clone, base_branch="main"), REPO,
    )

    assert outcome is False
    # The round reviews the CONFIGURED base, and the PR is on it with that
    # base merged in.
    assert reviews[0][0][2] == "main"
    assert fake.prs[PR_NUMBER]["baseRefName"] == "main"
    assert git(repo["worktree"], "merge-base", "--is-ancestor", repo["main"],
               "HEAD") == ""
    # Nothing blocked, nothing to repair: the label set is untouched and no
    # failure comment reached the Issue.
    assert fake.issues[ISSUE_NUMBER]["labels"] == ["ai-pr-opened"]
    assert "Orbi failed" not in json.dumps(
        fake.issues[ISSUE_NUMBER]["comments"])


def test_a_merged_scene_pr_keeps_its_merge_closeout(
    allow_external_commands, repo, monkeypatch,
):
    """The user-visible regression the state check prevents: the PR was
    merged while the configured base moved on. The resume leaves that decided
    PR alone and records the merged closeout (`ai-merged`, Issue closed),
    instead of retrying a base move GitHub refuses on a merged PR — a retry
    that `ai-fix-needed` would repeat on every tick and never end."""
    fake = _fake_gh(repo, base="old")
    fake.prs[PR_NUMBER]["state"] = "MERGED"
    fake.prs[PR_NUMBER]["mergeCommit"] = {"oid": "f" * 40}
    install_real_git_gh_seam(monkeypatch, fake)

    # The REAL verification raises: the scene PR is merged, so it is not
    # among the open PRs of the delivery branch.
    with pytest.raises(pr_verification.ResumePrClosedError):
        resume_verification.verify_resumed_pr(
            run_state.ResumeScene(
                parsed=_scene("old", repo["old"]), scene_at=None,
            ),
            {"number": ISSUE_NUMBER, "title": "ship", "body": ""},
            config_domain.RunnerConfig(repo_dir=repo["clone"],
                                       base_branch="main"),
            REPO,
        )

    # The decided PR was not touched, and the merge is recorded.
    assert fake.prs[PR_NUMBER]["baseRefName"] == "old"
    assert git(repo["worktree"], "rev-parse", "HEAD") == repo["delivery"]
    assert fake.issues[ISSUE_NUMBER]["labels"] == ["ai-merged"]
    assert fake.issues[ISSUE_NUMBER]["state"] == "closed"
