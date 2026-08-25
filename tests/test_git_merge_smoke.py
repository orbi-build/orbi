"""Real git smoke tests for the auto review/fix/merge gate (Issue #34).

These tests execute actual git commands inside a temporary local repository
(no network, no remote push of a protected branch, no real GitHub merge) and
prove the merge-gate acceptance criteria:

- a PR head that contains the latest ``origin/<base>`` is merged with
  ``--match-head-commit`` and the merge commit lands on ``origin/<base>``;
- a PR head that is behind the latest ``origin/<base>`` is rejected before any
  merge, so a stale baseline is never merged.

``gh`` is faked: ``gh pr view`` answers the PR state and ``gh pr merge``
performs a real local merge (``git merge``) so the merge commit is a real git
object on ``origin/<base>`` that ``confirm_merged`` can verify.
"""
import json
import subprocess
from pathlib import Path

import pytest

import bootstrap_runner as runner


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """Local bare origin plus a clone with two commits on main."""
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
    (clone / "a.txt").write_text("a", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "first")
    (clone / "b.txt").write_text("b", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "second")
    git(clone, "push", "origin", "main")
    return clone


def commit_file(clone: Path, name: str, message: str) -> str:
    (clone / name).write_text(message, encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", message)
    return git(clone, "rev-parse", "HEAD")


def make_pr(head_oid: str, *, mergeable="MERGEABLE", state="OPEN",
            merged_at=None, merge_commit=None) -> str:
    return json.dumps({
        "number": 4, "url": "https://github.com/owner/repo/pull/4",
        "state": state, "mergeable": mergeable, "headRefOid": head_oid,
        "mergedAt": merged_at,
        "mergeCommit": ({"oid": merge_commit} if merge_commit else None),
    })


def install_fake_gh(monkeypatch, clone: Path, pr_json: str) -> list:
    """Run real git; answer `gh pr view` with pr_json and perform a real
    local merge for `gh pr merge` so the merge commit is a genuine git
    object on origin/<base>."""
    real_run = runner.run_command
    commands: list = []

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"]:
            commands.append(command)
            if command[:2] == ["gh", "pr"] and "view" in command:
                return pr_json
            # else: `gh pr merge` — perform a real local merge so the merge
            # commit is a genuine git object on origin/<base>.
            git(clone, "checkout", "main")
            git(clone, "merge", "--no-ff", command[command.index(
                "--match-head-commit") + 1])
            git(clone, "push", "origin", "main")
            git(clone, "checkout", "-")
            return ""
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    return commands


def test_merge_gate_merges_head_containing_latest_base(clone, monkeypatch):
    git(clone, "checkout", "-b", "muyan-pilot/owner-repo-issue-4-run1")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main"),
          "head_ref": "muyan-pilot/owner-repo-issue-4-run1",
          "head_oid": head_oid}
    commands = install_fake_gh(monkeypatch, clone, make_pr(head_oid))
    merged = runner.merge_gate(clone, pr, "main")
    assert merged["merged"] is True
    # The merge used --match-head-commit with the reviewed head SHA.
    merge_cmd = [c for c in commands if c[:2] == ["gh", "pr"]
                 and "merge" in c][0]
    assert merge_cmd[merge_cmd.index("--match-head-commit") + 1] == head_oid
    assert "--merge" in merge_cmd
    # The fake `gh pr merge` performed a real merge; origin/main now has a
    # merge commit that contains the delivery head.
    merge_commit = git(clone, "rev-parse", "origin/main")
    git(clone, "merge-base", "--is-ancestor", head_oid, "origin/main")
    # confirm_merged verifies the merge commit is now on origin/main.
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: (
            make_pr(head_oid, state="MERGED", merged_at="now",
                    merge_commit=merge_commit)
            if command[:2] == ["gh", "pr"] and "view" in command
            else ""
        ),
    )
    confirmed = runner.confirm_merged(clone, merged, "main")
    assert confirmed["state"] == "MERGED"
    assert confirmed["merge_commit"] == merge_commit


def test_merge_gate_rejects_head_behind_latest_base(clone, monkeypatch, caplog):
    # Delivery branch is based on the first commit only.
    git(clone, "checkout", "-b", "muyan-pilot/owner-repo-issue-4-run1",
        git(clone, "rev-parse", "origin/main~1"))
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    # Remote main advances after the delivery was created.
    git(clone, "checkout", "main")
    commit_file(clone, "advance.txt", "main advanced")
    git(clone, "push", "origin", "main")
    git(clone, "checkout", "muyan-pilot/owner-repo-issue-4-run1")

    real_run = runner.run_command
    commands: list = []

    def fake_run(command, **kwargs):
        # Record every command; the gate must fail at the real git
        # merge-base check before any `gh` command is issued.
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main~1"),
          "head_ref": "muyan-pilot/owner-repo-issue-4-run1",
          "head_oid": head_oid}
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.merge_gate(clone, pr, "main")
    # No merge was attempted: a stale baseline is never merged.
    assert not [c for c in commands if c[:2] == ["gh", "pr"]
               and "merge" in c]
    assert "merge_gate_behind_base" in caplog.text


def test_git_helper_fails_fast_on_nonzero_exit(clone):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(clone, "rev-parse", "no-such-ref")
