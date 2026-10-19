"""Real git smoke tests for the auto review/fix/merge gate (Issue #34).

These tests execute actual git commands inside a temporary local repository
(no network, no remote push of a protected branch, no real GitHub merge) and
prove the merge-gate acceptance criteria:

- a PR head that contains the latest ``origin/<base>`` is merged with
  ``--match-head-commit`` and the merge commit lands on ``origin/<base>``;
- a PR head that is behind the latest ``origin/<base>`` is absorbed when
  conflict-free and the pushed head is deferred (Issue #1626); the next
  gate pass merges it once its CI is green, while a real conflict remains
  recoverable.

``gh`` is faked: ``gh pr view`` answers the PR state and ``gh pr merge``
performs a real local merge (``git merge``) so the merge commit is a real git
object on ``origin/<base>`` that ``confirm_merged`` can verify.
"""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import git

import orbi.runner as runner
import orbi.review_merge as review_merge
import orbi.run_state as run_state
from seam import seam


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
            if command[0] == "gh" and command[1] == "pr" and "view" in command:
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

    monkeypatch.setattr(seam, "run_command", fake_run)
    return commands


def test_merge_gate_merges_head_containing_latest_base(clone, monkeypatch):
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main"),
          "head_ref": "orbi/owner-repo-issue-4",
          "head_oid": head_oid}
    commands = install_fake_gh(monkeypatch, clone, make_pr(head_oid))
    merged = review_merge.merge_gate(clone, pr, "main", repo_dir=clone, language_code="en")
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
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: (
            make_pr(head_oid, state="MERGED", merged_at="now",
                    merge_commit=merge_commit)
            if command[0] == "gh" and command[1] == "pr" and "view" in command
            else ""
        ),
    )
    confirmed = review_merge.confirm_merged(clone, merged, "main", repo_dir=clone)
    assert confirmed["state"] == "MERGED"
    assert confirmed["merge_commit"] == merge_commit


def test_merge_gate_absorbs_clean_behind_base_then_defers(
        clone, monkeypatch, caplog):
    """A clean stale PR is absorbed; the pushed head is deferred (Issue
    #1626) and merges on the next gate pass."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4",
        git(clone, "rev-parse", "origin/main~1"))
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    git(clone, "checkout", "main")
    commit_file(clone, "advance.txt", "main advanced")
    git(clone, "push", "origin", "main")
    git(clone, "checkout", "orbi/owner-repo-issue-4")
    absorbed_head = None
    views = 0
    real_run = runner.run_command
    commands: list = []

    def fake_run(command, **kwargs):
        nonlocal absorbed_head, views
        commands.append(command)
        if command[0] == "gh" and command[1] == "pr" and "view" in command:
            views += 1
            oid = head_oid if views == 1 else absorbed_head
            return make_pr(oid)
        if command[0] == "gh" and command[1] == "pr" and "merge" in command:
            git(clone, "checkout", "main")
            git(clone, "merge", "--no-ff", command[command.index(
                "--match-head-commit") + 1])
            git(clone, "push", "origin", "main")
            git(clone, "checkout", "-")
            return ""
        result = real_run(command, **kwargs)
        if command[:2] == ["git", "rev-parse"] and command[2] == "HEAD":
            absorbed_head = result
        return result

    monkeypatch.setattr(seam, "run_command", fake_run)
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main~1"),
          "head_ref": "orbi/owner-repo-issue-4", "head_oid": head_oid}
    with caplog.at_level("INFO"):
        with pytest.raises(review_merge.DeliveryDeferred):
            review_merge.merge_gate(clone, pr, "main", repo_dir=clone, language_code="en")
    # Issue #1626: the gate absorbed the base and pushed the merged head,
    # then deferred instead of reading the new head's CI. No merge command
    # ran in this tick.
    assert views == 2
    assert absorbed_head is not None
    assert "base_absorbed" in caplog.text
    assert not [c for c in commands if c[:2] == ["gh", "pr"]
                and "merge" in c]

    # The next gate pass (the branch now contains the base) merges the
    # absorbed head with --match-head-commit.
    merged = review_merge.merge_gate(
        clone, {**pr, "head_oid": absorbed_head}, "main", repo_dir=clone, language_code="en",
    )
    assert merged["merged"] is True
    merge_cmd = [c for c in commands if c[:2] == ["gh", "pr"]
                 and "merge" in c][0]
    assert merge_cmd[merge_cmd.index("--match-head-commit") + 1] == absorbed_head


def test_merge_gate_conflicted_absorb_stays_recoverable(clone, monkeypatch):
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4",
        git(clone, "rev-parse", "origin/main~1"))
    head_oid = commit_file(clone, "shared.txt", "delivery")
    git(clone, "checkout", "main")
    commit_file(clone, "shared.txt", "main")
    git(clone, "push", "origin", "main")
    git(clone, "checkout", "orbi/owner-repo-issue-4")
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[0] == "gh" and command[1] == "pr" and "view" in command:
            return make_pr(head_oid, mergeable="MERGEABLE")
        return real_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main~1"),
          "head_ref": "orbi/owner-repo-issue-4", "head_oid": head_oid}
    with pytest.raises(review_merge.RecoverableMergeGateError, match="cannot absorb"):
        review_merge.merge_gate(clone, pr, "main", repo_dir=clone, language_code="en")
    assert git(clone, "status", "--porcelain", "--untracked-files=no") == ""


def test_merge_gate_rejects_conflicting_pr_reports_mergeable(
        clone, monkeypatch, caplog):
    # The head contains the latest base, so the only failing gate state
    # is the conflicting mergeable answer (Issue #879).
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    install_fake_gh(monkeypatch, clone,
                    make_pr(head_oid, mergeable="DIRTY"))
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main"),
          "head_ref": "orbi/owner-repo-issue-4",
          "head_oid": head_oid}
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="mergeable=DIRTY",
    ):
        review_merge.merge_gate(clone, pr, "main", repo_dir=clone, language_code="en")
    assert "merge_gate_not_mergeable" in caplog.text


def test_deployment_checkout_fast_forwards_after_independent_merge(
        clone, tmp_path, monkeypatch):
    """F1: an independent merge actor advances origin/main first; the
    deployment checkout (the repo_dir systemd executes) is a separate
    clone that still sits on the old base, and `sync_base_checkout`
    fast-forwards it to the new origin/main, verified by SHA."""
    # The deployment checkout: a separate clone of the same origin, on
    # the old base (this is what systemd loads on the next tick).
    checkout = tmp_path / "deployment-checkout"
    subprocess.run(
        ["git", "clone", git(clone, "remote", "get-url", "origin"),
         str(checkout)],
        capture_output=True, text=True, check=True,
    )
    git(checkout, "config", "user.email", "deploy@test.local")
    git(checkout, "config", "user.name", "Deploy")
    old_head = git(checkout, "rev-parse", "HEAD")

    # Delivery branch in the worktree clone; the fake `gh pr merge`
    # (the independent merge actor) lands the merge on origin/main.
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    pr = {"number": 4, "url": "u", "base_ref": "main",
          "base_oid": git(clone, "rev-parse", "origin/main"),
          "head_ref": "orbi/owner-repo-issue-4",
          "head_oid": head_oid}
    install_fake_gh(monkeypatch, clone, make_pr(head_oid))
    merged = review_merge.merge_gate(clone, pr, "main", repo_dir=clone, language_code="en")
    assert merged["merged"] is True
    merge_commit = git(clone, "rev-parse", "origin/main")
    # The remote advanced; the deployment checkout has not yet.
    assert git(clone, "rev-parse", "origin/main") != old_head
    assert git(checkout, "rev-parse", "HEAD") == old_head
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[0] == "gh" and command[1] == "pr" and "view" in command:
            return make_pr(head_oid, state="MERGED", merged_at="now",
                           merge_commit=merge_commit)
        return real_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    confirmed = review_merge.confirm_merged(clone, merged, "main", repo_dir=clone)
    assert confirmed["merge_commit"] == merge_commit

    # The deployment checkout now fast-forwards to the merged base.
    review_merge.sync_base_checkout(checkout, "main")
    synced = git(checkout, "rev-parse", "HEAD")
    assert synced == merge_commit
    git(checkout, "merge-base", "--is-ancestor", head_oid, "HEAD")


def test_sync_base_checkout_fails_fast_on_drifted_checkout(
        clone, tmp_path):
    """A deployment checkout with local drift cannot fast-forward; the
    sync fails fast (the merge already landed on GitHub)."""
    checkout = tmp_path / "deployment-checkout"
    subprocess.run(
        ["git", "clone", git(clone, "remote", "get-url", "origin"),
         str(checkout)],
        capture_output=True, text=True, check=True,
    )
    git(checkout, "config", "user.email", "deploy@test.local")
    git(checkout, "config", "user.name", "Deploy")
    # Local drift on the checkout...
    (checkout / "drift.txt").write_text("drift", encoding="utf-8")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "drift")
    # ...and the remote advances independently.
    git(clone, "checkout", "main")
    commit_file(clone, "advance.txt", "main advanced")
    git(clone, "push", "origin", "main")

    with pytest.raises(RuntimeError, match="cannot fast-forward"):
        review_merge.sync_base_checkout(checkout, "main")


# ---------------------------------------------------------------------------
# Issue #1798: a merge that landed despite an error from the merge call
# ---------------------------------------------------------------------------

def install_failing_merge_gh(monkeypatch, clone: Path, head_oid: str, *,
                             landed: bool = True, pr_after=None) -> list:
    """Run real git; `gh pr merge` lands a real merge and THEN fails.

    This is the Issue #1798 scene (orbi-build/orbi-cloud PR #2652): GitHub
    creates the merge commit on the base branch and the merge call itself
    returns `502 Bad Gateway`. `landed=False` models the same error with
    nothing on the base branch. `pr_after` answers `gh pr view` once the
    merge call has failed; before it the reviewed head reads OPEN and
    mergeable.
    """
    real_run = runner.run_command
    calls: list = []
    failed = False

    def fake_run(command, **kwargs):
        nonlocal failed
        if command[0] == "gh" and command[1:3] == ["pr", "view"]:
            calls.append(command)
            if failed and pr_after is not None:
                return pr_after()
            return make_pr(head_oid)
        if command[0] == "gh" and command[1] == "pr" and "merge" in command:
            calls.append(command)
            if landed:
                git(clone, "checkout", "main")
                git(clone, "merge", "--no-ff", command[command.index(
                    "--match-head-commit") + 1])
                git(clone, "push", "origin", "main")
                git(clone, "checkout", "-")
            failed = True
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 502: Bad Gateway",
            )
        if command[0] == "gh":
            calls.append(command)
            return ""
        return real_run(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    return calls


def _delivery_pr(clone: Path, head_oid: str) -> dict:
    return {"number": 4, "url": "u", "base_ref": "main",
            "base_oid": git(clone, "rev-parse", "origin/main"),
            "head_ref": "orbi/owner-repo-issue-4", "head_oid": head_oid}


def test_merge_gate_accepts_a_merge_that_landed_despite_a_502(
        clone, monkeypatch, caplog):
    """Issue #1798 acceptance 1: `gh pr merge` fails 5xx and the PR then
    reads MERGED with its merge commit on origin/<base> — the delivery
    continues like a successful merge instead of a fix-needed round."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    calls = install_failing_merge_gh(
        monkeypatch, clone, head_oid,
        pr_after=lambda: make_pr(
            head_oid, state="MERGED", merged_at="2026-10-19T13:38:31Z",
            merge_commit=git(clone, "rev-parse", "origin/main"),
        ),
    )
    with caplog.at_level("WARNING"):
        merged = review_merge.merge_gate(
            clone, _delivery_pr(clone, head_oid), "main",
            repo_dir=clone, language_code="en",
        )
    assert merged["merged"] is True
    assert "merge_landed_after_error" in caplog.text
    # Still exactly one merge call: the error is confirmed, never retried.
    assert len([c for c in calls
                if c[0] == "gh" and c[1] == "pr" and "merge" in c]) == 1
    merge_commit = git(clone, "rev-parse", "origin/main")
    confirmed = review_merge.confirm_merged(clone, merged, "main",
                                            repo_dir=clone)
    assert confirmed == {"state": "MERGED", "merge_commit": merge_commit}


def test_merge_gate_accepts_a_landed_head_while_the_pr_reads_open(
        clone, monkeypatch, caplog):
    """Issue #1798 acceptance 2 (PR #2652's shape): GitHub created the merge
    commit on the base branch and never marked the PR merged, so re-reading
    the PR state cannot recognise the merge. The reviewed head being
    reachable from origin/<base> does, and the landed merge commit is the
    one the delivery records."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    calls = install_failing_merge_gh(monkeypatch, clone, head_oid)
    with caplog.at_level("INFO"):
        merged = review_merge.merge_gate(
            clone, _delivery_pr(clone, head_oid), "main",
            repo_dir=clone, language_code="en",
        )
    assert merged["merged"] is True
    assert len([c for c in calls
                if c[0] == "gh" and c[1] == "pr" and "merge" in c]) == 1
    merge_commit = git(clone, "rev-parse", "origin/main")
    # The commit that landed the reviewed head is a real merge commit whose
    # second parent is that head.
    assert git(clone, "rev-parse", f"{merge_commit}^2") == head_oid
    with caplog.at_level("INFO"):
        confirmed = review_merge.confirm_merged(clone, merged, "main",
                                                repo_dir=clone)
    # Issue #1801: the PR never reads MERGED, so GitHub never closes the
    # Issue through the closing keyword; the answer says OPEN and the
    # merged path closes it itself.
    assert confirmed == {"state": "OPEN", "merge_commit": merge_commit}
    assert "confirm_merged_landed_on_base" in caplog.text


def test_a_landed_merge_that_never_reads_merged_closes_the_issue(
        clone, monkeypatch, caplog):
    """Issue #1801 acceptance 1, against a real repository: `gh pr merge`
    fails with a 5xx, the reviewed head landed on `origin/main` and the PR
    still reads `OPEN`. The merged path's decision (`confirm_merged` did
    not answer MERGED) runs `gh issue close`, so the Issue ends closed
    like a normal merge instead of holding its milestone's release."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    calls = install_failing_merge_gh(monkeypatch, clone, head_oid)
    merged = review_merge.merge_gate(
        clone, _delivery_pr(clone, head_oid), "main",
        repo_dir=clone, language_code="en",
    )
    confirmed = review_merge.confirm_merged(clone, merged, "main",
                                            repo_dir=clone)
    assert confirmed["state"] != "MERGED"
    with caplog.at_level("INFO"):
        run_state.close_issue_after_landed_merge(
            4, repo="owner/repo", merge_commit=confirmed["merge_commit"],
        )
    assert ["gh", "issue", "close", "4",
            "--repo", "owner/repo"] in calls
    assert "issue_closed_after_landed_merge" in caplog.text
    assert f"merge_commit={confirmed['merge_commit']}" in caplog.text


def test_merge_gate_still_fails_when_the_merge_did_not_land(
        clone, monkeypatch):
    """Issue #1798 acceptance 3: the same 5xx with the reviewed head NOT on
    origin/<base> keeps today's outcome — the error surfaces."""
    git(clone, "checkout", "-b", "orbi/owner-repo-issue-4")
    head_oid = commit_file(clone, "delivery.txt", "delivery")
    pr = _delivery_pr(clone, head_oid)
    calls = install_failing_merge_gh(monkeypatch, clone, head_oid,
                                     landed=False)
    monkeypatch.setattr(
        seam, "merge_gate_preflight",
        lambda *args: ["merge_gate: PASS protection readable"],
    )
    with pytest.raises(subprocess.CalledProcessError) as raised:
        review_merge.merge_gate(
            clone, pr, "main", repo_dir=clone, language_code="en",
        )
    assert raised.value.returncode == 1
    assert "502" in (raised.value.stderr or "")
    assert len([c for c in calls
                if c[0] == "gh" and c[1] == "pr" and "merge" in c]) == 1
    # Nothing landed: the base branch is untouched.
    assert git(clone, "rev-parse", "origin/main") == pr["base_oid"]

