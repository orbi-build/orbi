"""Unit tests for the auto review/fix/merge orchestration (Issues #34, #82).

The Runner (not Pi) closes the delivery loop: after the implementer opens a PR
it freezes the exact PR base/head SHA, runs one independent review session
that fixes Blocker/Major findings IN THE SAME SESSION (Issue #82: no
cold-start fixer, no third review), re-freezes the head after a clean
verdict (the reviewer may have pushed a fix), re-checks the merge gate
against the latest origin/main, and merges via `gh pr merge
--match-head-commit`. Pi never pushes main.
"""
import fcntl
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import orbi.runner as runner
from orbi import cli_install
from tests.test_progress_wiring import make_fake_gh


# ---------------------------------------------------------------------------
# parse_review_verdict
# ---------------------------------------------------------------------------

def test_parse_review_verdict_pass():
    text = "some review prose\nREVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 2,
        "findings": [],
    }) + "\ntrailing"
    verdict = runner.parse_review_verdict(text)
    assert verdict["verdict"] == "pass"
    assert verdict["blockers"] == 0
    assert verdict["majors"] == 0
    assert verdict["minors"] == 2


def test_parse_review_verdict_findings():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 1, "majors": 2, "minors": 0,
        "findings": [{"level": "Blocker", "location": "a.py:1", "note": "x"}],
    })
    verdict = runner.parse_review_verdict(text)
    assert verdict["verdict"] == "findings"
    assert verdict["blockers"] == 1
    assert verdict["majors"] == 2
    assert verdict["findings"][0]["location"] == "a.py:1"


def test_parse_review_verdict_uses_last_marker_line():
    first = json.dumps({"verdict": "findings", "blockers": 3, "majors": 0,
                        "minors": 0, "findings": []})
    last = json.dumps({"verdict": "pass", "blockers": 0, "majors": 0,
                       "minors": 0, "findings": []})
    text = f"REVIEW_VERDICT {first}\nmore prose\nREVIEW_VERDICT {last}"
    assert runner.parse_review_verdict(text)["verdict"] == "pass"


def test_parse_review_verdict_missing_marker_raises():
    with pytest.raises(ValueError, match="no REVIEW_VERDICT"):
        runner.parse_review_verdict("review prose without a verdict")


def test_parse_review_verdict_malformed_json_raises():
    with pytest.raises(ValueError, match="malformed REVIEW_VERDICT"):
        runner.parse_review_verdict("REVIEW_VERDICT {not json")


def test_parse_review_verdict_rejects_unknown_verdict():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "maybe", "blockers": 0, "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="verdict must be"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_negative_counts():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": -1, "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="non-negative"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_non_integer_counts():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": "many", "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="non-negative"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_boolean_counts():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": True, "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="non-negative"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_non_list_findings():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 0,
        "findings": "none",
    })
    with pytest.raises(ValueError, match="findings must be a list"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_non_dict_json():
    with pytest.raises(ValueError, match="malformed REVIEW_VERDICT"):
        runner.parse_review_verdict("REVIEW_VERDICT [1, 2, 3]")


def test_parse_review_verdict_rejects_pass_with_blocker_counts():
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="pass verdict cannot have"):
        runner.parse_review_verdict(text)


def test_parse_review_verdict_rejects_findings_without_counts():
    # A findings verdict with 0/0 would otherwise merge unreviewed work
    # (review_has_findings only looked at counts). Fail fast instead.
    text = "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 0, "majors": 0, "minors": 0,
        "findings": [],
    })
    with pytest.raises(ValueError, match="findings verdict requires"):
        runner.parse_review_verdict(text)


def test_review_has_findings_helper():
    assert runner.review_has_findings({
        "verdict": "findings", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [],
    }) is True
    assert runner.review_has_findings({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 3,
        "findings": [],
    }) is False


# ---------------------------------------------------------------------------
# freeze_pr
# ---------------------------------------------------------------------------

def _pr_json(number=4, base="main", base_oid="b1", head="h1"):
    return json.dumps([{
        "number": number,
        "url": f"https://github.com/owner/repo/pull/{number}",
        "baseRefName": base,
        "baseRefOid": base_oid,
        "headRefName": "orbi/owner-repo-issue-4-run1",
        "headRefOid": head,
    }])


def test_freeze_pr_returns_frozen_base_and_head(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _pr_json()

    monkeypatch.setattr(runner, "run_command", fake_run)
    pr = runner.freeze_pr(
        tmp_path, "orbi/owner-repo-issue-4-run1", "main",
    )
    assert pr["number"] == 4
    assert pr["base_ref"] == "main"
    assert pr["base_oid"] == "b1"
    assert pr["head_oid"] == "h1"
    assert pr["url"].endswith("/pull/4")
    assert calls == [[
        "gh", "pr", "list", "--state", "open", "--head",
        "orbi/owner-repo-issue-4-run1",
        "--json", "number,url,baseRefName,baseRefOid,headRefName,headRefOid",
        "--limit", "2",
    ]]


def test_freeze_pr_rejects_wrong_base(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command", lambda command, **kwargs: _pr_json(base="develop"),
    )
    with pytest.raises(RuntimeError, match="PR base is develop, expected main"):
        runner.freeze_pr(tmp_path, "orbi/owner-repo-issue-4-run1", "main")


def test_freeze_pr_rejects_no_open_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.freeze_pr(tmp_path, "orbi/owner-repo-issue-4-run1", "main")


def test_freeze_pr_rejects_multiple_open_prs(monkeypatch, tmp_path):
    two = json.dumps([
        {"number": 4, "url": "u4", "baseRefName": "main",
         "baseRefOid": "b1", "headRefName": "h", "headRefOid": "h1"},
        {"number": 5, "url": "u5", "baseRefName": "main",
         "baseRefOid": "b1", "headRefName": "h", "headRefOid": "h2"},
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: two)
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.freeze_pr(tmp_path, "orbi/owner-repo-issue-4-run1", "main")


# ---------------------------------------------------------------------------
# run_review command construction (streamed, role=review)
# ---------------------------------------------------------------------------

def _review_config(tmp_path, prompt_name="prompt_review.md"):
    prompt = tmp_path / prompt_name
    prompt.write_text("REVIEW PROMPT {{PR_NUMBER}} {{BASE_SHA}} {{HEAD_SHA}}",
                      encoding="utf-8")
    return {
        "prompt_review": prompt,
        "repo_dir": tmp_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [tmp_path / "code-review.md"],
        "base_branch": "main",
        "base_sha": "b1",
        "run_id": "run1",
    }


def test_run_review_launches_independent_readonly_pi_session(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    pr = {"number": 4, "url": "u", "base_ref": "main", "base_oid": "b1",
          "head_ref": "h", "head_oid": "h1"}
    out = runner.run_review(tmp_path, pr, _review_config(tmp_path),
                            "owner/repo", 4, "orbi/owner-repo-issue-4-run1", 1)
    assert out == "done"
    command, kwargs = calls[0]
    # Review skill, shared flat session dir (so the same live activity
    # pipeline can follow the new JSONL), redacted log.
    assert command[0] == "pi"
    assert "--skill" in command
    assert str(tmp_path / "code-review.md") in command
    session_dir = command[command.index("--session-dir") + 1]
    assert session_dir == str(tmp_path / ".pi-session")
    # Reviewer system prompt carries the frozen PR number and base/head SHA.
    system_prompt = command[command.index("--system-prompt") + 1]
    assert " 4 " in system_prompt
    assert "b1" in system_prompt
    assert "h1" in system_prompt
    # The review streams through the same pipeline as implement/fix with
    # its own role (Issue #41: one run, many roles).
    assert kwargs["cwd"] == tmp_path
    assert kwargs["role"] == runner.ROLE_REVIEW
    assert kwargs["run_id"] == "run1"
    assert kwargs["issue"] == 4
    assert kwargs["branch"] == "orbi/owner-repo-issue-4-run1"
    assert kwargs["source_repo"] == "owner/repo"
    assert kwargs["log_command"][-2:] == [
        "<redacted>", "<review-context-redacted>",
    ]


# ---------------------------------------------------------------------------
# merge gate
# ---------------------------------------------------------------------------

def _merge_gate_fake(pr_state="MERGEABLE", head_oid="h1"):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and "view" in command:
            return json.dumps({
                "number": 4, "url": "u", "state": "OPEN",
                "mergeable": pr_state, "headRefOid": head_oid,
                "mergedAt": None, "mergeCommit": None,
            })
        return ""
    return fake_run


def test_merge_gate_merges_reviewed_head_with_match_head_commit(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _merge_gate_fake()(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    pr = runner.merge_gate(tmp_path, {"number": 4, "url": "u",
                                      "base_ref": "main", "base_oid": "b1",
                                      "head_ref": "h", "head_oid": "h1"},
                           "main", repo_dir=tmp_path)
    assert pr["merged"] is True
    # The merge must use --match-head-commit with the reviewed head SHA.
    merge_cmd = [c for c in calls if c[:2] == ["gh", "pr"] and "merge" in c][0]
    assert "--match-head-commit" in merge_cmd
    assert merge_cmd[merge_cmd.index("--match-head-commit") + 1] == "h1"
    assert "--merge" in merge_cmd


def test_merge_gate_requires_the_repo_dir_lock_location(
    monkeypatch, tmp_path,
):
    # Issue #171: the gate fetch updates the shared remote-tracking
    # ref, so the lock location (the deployment checkout's shared state
    # dir) must be explicit — there is no bypass path.
    monkeypatch.setattr(runner, "run_command", _merge_gate_fake())
    with pytest.raises(TypeError):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u",
                                     "base_ref": "main", "base_oid": "b1",
                                     "head_ref": "h", "head_oid": "h1"},
                         "main")


def test_merge_gate_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the gate's base freshness fetch runs under the SAME
    # base-sync lock (a concurrent probe must not acquire it while the
    # fetch is in flight).
    spy, held = _lock_held_during_fetch(
        tmp_path, inner=lambda command, **kwargs: "",
    )

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            return spy(command, **kwargs)
        return _merge_gate_fake()(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner.merge_gate(tmp_path, {"number": 4, "url": "u",
                                 "base_ref": "main", "base_oid": "b1",
                                 "head_ref": "h", "head_oid": "h1"},
                      "main", repo_dir=tmp_path)
    assert held == [True]
    _lock_free(tmp_path)


def test_merge_gate_rejects_head_behind_latest_base(monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command, stderr="not ancestor")
        return ""
    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="behind latest remote base",
    ):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                                     "base_oid": "b1", "head_ref": "h",
                                     "head_oid": "h1"}, "main",
                          repo_dir=tmp_path)
    assert "base_branch=main" in caplog.text


def test_merge_gate_rejects_non_mergeable_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", _merge_gate_fake(pr_state="DIRTY"))
    with pytest.raises(RuntimeError, match="not mergeable"):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                                     "base_oid": "b1", "head_ref": "h",
                                     "head_oid": "h1"}, "main",
                          repo_dir=tmp_path)


def test_merge_gate_rejects_head_that_moved_since_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command", _merge_gate_fake(head_oid="moved"),
    )
    with pytest.raises(RuntimeError, match="head moved since review"):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                                     "base_oid": "b1", "head_ref": "h",
                                     "head_oid": "h1"}, "main",
                          repo_dir=tmp_path)


# ---------------------------------------------------------------------------
# confirm_merged
# ---------------------------------------------------------------------------

def test_confirm_merged_accepts_merged_pr_on_origin_main(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "pr"] and "view" in command:
            return json.dumps({
                "number": 4, "url": "u", "state": "MERGED",
                "mergedAt": "2026-08-25T00:00:00Z",
                "mergeCommit": {"oid": "m1"},
            })
        return ""
    monkeypatch.setattr(runner, "run_command", fake_run)
    result = runner.confirm_merged(
        tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                   "base_oid": "b1", "head_ref": "h", "head_oid": "h1"}, "main",
        repo_dir=tmp_path,
    )
    assert result["state"] == "MERGED"
    assert result["merge_commit"] == "m1"


def test_confirm_merged_requires_the_repo_dir_lock_location(
    monkeypatch, tmp_path,
):
    # Issue #171: the confirm fetch updates the shared remote-tracking
    # ref, so the lock location must be explicit — no bypass path.
    monkeypatch.setattr(runner, "run_command", _merge_gate_fake())
    with pytest.raises(TypeError):
        runner.confirm_merged(
            tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                       "base_oid": "b1", "head_ref": "h", "head_oid": "h1"},
            "main",
        )


def test_confirm_merged_fetches_under_the_base_sync_lock(
    monkeypatch, tmp_path,
):
    # Issue #171: the confirm fetch runs under the SAME base-sync lock
    # (a concurrent probe must not acquire it while the fetch is in
    # flight).
    spy, held = _lock_held_during_fetch(
        tmp_path, inner=lambda command, **kwargs: "",
    )

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            return spy(command, **kwargs)
        if command[:2] == ["gh", "pr"] and "view" in command:
            return json.dumps({
                "number": 4, "url": "u", "state": "MERGED",
                "mergedAt": "2026-08-25T00:00:00Z",
                "mergeCommit": {"oid": "m1"},
            })
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    runner.confirm_merged(
        tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                   "base_oid": "b1", "head_ref": "h", "head_oid": "h1"},
        "main", repo_dir=tmp_path,
    )
    assert held == [True]
    _lock_free(tmp_path)


def test_confirm_merged_rejects_unmerged_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps({
            "number": 4, "url": "u", "state": "OPEN",
            "mergedAt": None, "mergeCommit": None,
        }),
    )
    with pytest.raises(RuntimeError, match="not merged"):
        runner.confirm_merged(
            tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                       "base_oid": "b1", "head_ref": "h", "head_oid": "h1"},
            "main", repo_dir=tmp_path,
        )


def test_confirm_merged_rejects_merge_commit_missing_from_origin_main(
        monkeypatch, tmp_path, caplog):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command, stderr="not ancestor")
        if command[:2] == ["gh", "pr"] and "view" in command:
            return json.dumps({
                "number": 4, "url": "u", "state": "MERGED",
                "mergedAt": "2026-08-25T00:00:00Z",
                "mergeCommit": {"oid": "m1"},
            })
        return ""
    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="not on origin/main",
    ):
        runner.confirm_merged(
            tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                       "base_oid": "b1", "head_ref": "h", "head_oid": "h1"},
            "main", repo_dir=tmp_path,
        )
    assert "merge_commit=m1" in caplog.text


def test_confirm_merged_rejects_merged_pr_without_commit_oid(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: json.dumps({
            "number": 4, "url": "u", "state": "MERGED",
            "mergedAt": "2026-08-25T00:00:00Z", "mergeCommit": None,
        }),
    )
    with pytest.raises(RuntimeError, match="no merge commit oid"):
        runner.confirm_merged(
            tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                       "base_oid": "b1", "head_ref": "h", "head_oid": "h1"},
            "main", repo_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# comment_pr
# ---------------------------------------------------------------------------

def test_comment_pr_runs_gh_pr_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: calls.append(command),
    )
    runner.comment_pr(4, repo="owner/repo", body="round 1 findings")
    assert calls == [[
        "gh", "pr", "comment", "4", "--repo", "owner/repo",
        "--body", "round 1 findings",
    ]]


# ---------------------------------------------------------------------------
# review_rounds_so_far
# ---------------------------------------------------------------------------

def _round_comment(round_no, pr_number=4, association="OWNER"):
    return {
        "body": (
            "<!-- orbi:run=run1 -->\n"
            f"Orbi review round {round_no} for PR #{pr_number}: "
            "1 blocker(s), 0 major(s). Findings: []"
        ),
        "authorAssociation": association,
    }


def test_review_rounds_so_far_counts_recorded_rounds():
    comments = [
        {"body": "Orbi opened PR: https://x",
         "authorAssociation": "OWNER"},
        _round_comment(1),
        {"body": "Orbi fixed PR: https://x",
         "authorAssociation": "OWNER"},
        _round_comment(2),
        {"body": None, "authorAssociation": "OWNER"},
    ]
    assert runner.review_rounds_so_far(comments) == 2


def test_review_rounds_so_far_ignores_comments_without_round_line():
    assert runner.review_rounds_so_far([
        {"body": "Orbi started Pi: ...", "authorAssociation": "OWNER"},
        {"body": "some public comment", "authorAssociation": "OWNER"},
    ]) == 0


def test_review_rounds_so_far_ignores_untrusted_comments():
    # Public comments must not exhaust the 5-round budget (same trust
    # filter as resume_scene). Five NONE round comments still count as 0.
    untrusted = [_round_comment(i, association="NONE") for i in range(1, 6)]
    assert runner.review_rounds_so_far(untrusted) == 0
    trusted = [_round_comment(i, association="OWNER") for i in range(1, 6)]
    assert runner.review_rounds_so_far(trusted) == 5
    mixed = untrusted + [_round_comment(1, association="MEMBER")]
    assert runner.review_rounds_so_far(mixed) == 1


# ---------------------------------------------------------------------------
# sync_base_checkout (F1: the deployment checkout systemd executes)
# ---------------------------------------------------------------------------

def _clone_origin(origin: Path, name: str) -> Path:
    path = origin.parent / name
    path.mkdir()
    runner.run_command(["git", "clone", str(origin), "."], cwd=path)
    runner.run_command(["git", "config", "user.email", "pilot@test.local"],
                       cwd=path)
    runner.run_command(["git", "config", "user.name", "Pilot"], cwd=path)
    return path


def test_sync_base_checkout_fast_forwards_and_verifies(tmp_path):
    # A bare origin plus the deployment checkout (repo_dir) that systemd
    # executes from, plus an independent merge actor that advances the
    # remote base first.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."], cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    # The independent merge actor lands a merge on origin/main while the
    # deployment checkout is still on the old base.
    runner.run_command(["git", "commit", "--allow-empty", "-m", "merged"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    old_head = runner.run_command(["git", "rev-parse", "HEAD"], cwd=checkout)

    runner.sync_base_checkout(checkout, "main")

    new_head = runner.run_command(["git", "rev-parse", "HEAD"], cwd=checkout)
    remote = runner.run_command(
        ["git", "rev-parse", "origin/main"], cwd=checkout,
    )
    assert old_head != new_head
    assert new_head == remote


def test_sync_base_checkout_is_a_noop_when_already_at_remote(
        monkeypatch, tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."], cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")

    calls = []
    real = runner.run_command

    def spy(command, **kwargs):
        calls.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", spy)
    runner.sync_base_checkout(checkout, "main")
    # Only fetch + rev-parse; no merge is issued when already current.
    assert not any(c[:2] == ["git", "merge"] for c in calls)


def test_sync_base_checkout_fails_fast_when_not_fast_forwardable(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."], cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    # Local drift: the checkout has its own commit, the remote advanced
    # independently -> --ff-only cannot apply.
    runner.run_command(["git", "commit", "--allow-empty", "-m", "drift"],
                       cwd=checkout)
    runner.run_command(["git", "commit", "--allow-empty", "-m", "ahead"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)

    with pytest.raises(
        RuntimeError, match="cannot fast-forward",
    ):
        runner.sync_base_checkout(checkout, "main")


def test_sync_base_checkout_fails_fast_when_synced_head_mismatches(
        monkeypatch, tmp_path):
    heads = iter(["a" * 40, "b" * 40])

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "rev-parse"] and command[2] == "HEAD":
            return next(heads)
        if command[:2] == ["git", "rev-parse"]:
            return "c" * 40
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="after the sync"):
        runner.sync_base_checkout(tmp_path, "main")


def test_sync_base_checkout_lock_path_is_the_shared_state_dir_file(
    tmp_path,
):
    # Issue #149: the SAME lock file the ExecStartPre flock in the
    # service template uses (the shared state dir, never a per-process
    # temp file).
    assert cli_install.base_sync_lock_path(tmp_path) == (
        tmp_path / ".orbi" / "base-sync.lock"
    )


def test_sync_base_checkout_fails_fast_while_the_lock_is_held(
    tmp_path,
):
    # Issue #149: two instances may start in the same tick; the
    # Python-side sync must not run git while the ExecStartPre flock
    # (or another Runner's sync) holds the lock — it fails fast with a
    # useful error instead of racing the main worktree.
    lock_path = cli_install.base_sync_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            RuntimeError, match="base-sync.lock",
        ):
            runner.sync_base_checkout(
                tmp_path, "main", lock_timeout_seconds=0.5,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_sync_base_checkout_releases_the_lock_after_sync(
    monkeypatch, tmp_path,
):
    # Issue #149: the lock is short-lived — released when the sync
    # finishes (success or failure), so the next tick / instance can
    # proceed; no daemon holds it.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."],
                       cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "merged"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)

    runner.sync_base_checkout(checkout, "main")

    # After the sync the lock is free: a non-blocking probe acquires
    # and releases it immediately.
    lock_path = cli_install.base_sync_lock_path(checkout)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


def test_sync_base_checkout_releases_the_lock_on_failure(
    monkeypatch, tmp_path,
):
    # Issue #149: a failed sync (not fast-forwardable) must still
    # release the lock — the kernel releases it on process exit, but
    # the Runner stays alive and the next tick must not inherit a
    # stuck lock.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."],
                       cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "drift"],
                       cwd=checkout)
    runner.run_command(["git", "commit", "--allow-empty", "-m", "ahead"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)

    with pytest.raises(RuntimeError, match="cannot fast-forward"):
        runner.sync_base_checkout(checkout, "main")

    lock_path = cli_install.base_sync_lock_path(checkout)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


# ---------------------------------------------------------------------------
# fetch_base_ref (Issue #171: every shared-ref fetch under one lock)
# ---------------------------------------------------------------------------

def _lock_held_during_fetch(checkout: Path, inner=None) -> list[bool]:
    """Spy on run_command: while the fetch runs, probe whether the
    base-sync lock is held (a non-blocking acquire must fail).

    ``inner`` is what the fetch resolves to after the probe: the real
    ``run_command`` by default (real-git tests), or a no-op fake for
    the non-git tmp_path tests.
    """
    held: list[bool] = []
    if inner is None:
        inner = runner.run_command

    def spy(command, **kwargs):
        if command[:3] == ["git", "fetch", "origin"]:
            held.append(_probe_held(checkout))
        return inner(command, **kwargs)

    return spy, held


def _probe_held(checkout: Path) -> bool:
    """True while the base-sync lock is held: a non-blocking probe
    acquire fails; False (and the probe releases) when it is free."""
    lock_path = runner.base_sync_lock_path(checkout)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)


def _lock_free(checkout: Path) -> None:
    """A non-blocking probe acquires and releases the lock immediately."""
    assert _probe_held(checkout) is False


def test_fetch_base_ref_updates_the_remote_tracking_ref(monkeypatch, tmp_path):
    # Issue #171: the shared fetch helper updates origin/<base> ...
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."],
                       cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "ahead"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    remote_head = runner.run_command(
        ["git", "rev-parse", "HEAD"], cwd=actor,
    )

    runner.fetch_base_ref(checkout, "main")

    assert runner.run_command(
        ["git", "rev-parse", "origin/main"], cwd=checkout,
    ) == remote_head


def test_fetch_base_ref_holds_the_base_sync_lock_while_fetching(
    monkeypatch, tmp_path,
):
    # Issue #171: the fetch runs UNDER the same base-sync lock the
    # ExecStartPre flock and sync_base_checkout use — a concurrent
    # probe must not acquire it while the fetch is in flight.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."],
                       cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")

    spy, held = _lock_held_during_fetch(checkout)
    monkeypatch.setattr(runner, "run_command", spy)
    runner.fetch_base_ref(checkout, "main")

    assert held == [True]
    _lock_free(checkout)


def test_fetch_base_ref_fetches_in_the_given_worktree(
    monkeypatch, tmp_path,
):
    # Issue #171: the Runner verifies from the TASK WORKTREE (a
    # different worktree sharing the same common dir); the fetch runs
    # there (updating the shared refs/remotes/origin/<base>) while the
    # lock is still the deployment checkout's shared-state-dir lock.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    runner.run_command(["git", "init", "--bare", "-b", "main", "."],
                       cwd=origin)
    actor = _clone_origin(origin, "actor")
    runner.run_command(["git", "commit", "--allow-empty", "-m", "base"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    checkout = _clone_origin(origin, "checkout")
    worktree = tmp_path / "worktree"
    runner.run_command(
        ["git", "worktree", "add", str(worktree), "--detach",
         "origin/main"],
        cwd=checkout,
    )
    runner.run_command(["git", "commit", "--allow-empty", "-m", "ahead"],
                       cwd=actor)
    runner.run_command(["git", "push", "origin", "HEAD:main"], cwd=actor)
    remote_head = runner.run_command(
        ["git", "rev-parse", "HEAD"], cwd=actor,
    )

    spy, held = _lock_held_during_fetch(checkout)
    monkeypatch.setattr(runner, "run_command", spy)
    runner.fetch_base_ref(checkout, "main", cwd=worktree)

    assert held == [True]
    # The fetch ran in the worktree: ITS view of the shared ref moved.
    assert runner.run_command(
        ["git", "rev-parse", "origin/main"], cwd=worktree,
    ) == remote_head
    _lock_free(checkout)


def test_fetch_base_ref_fails_fast_while_the_lock_is_held(tmp_path):
    # Issue #171: a lock timeout is a fail-fast error with the scene
    # (lock path, timeout) — never a silent skip or a lock bypass.
    lock_path = runner.base_sync_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            RuntimeError, match="base-sync.lock",
        ):
            runner.fetch_base_ref(
                tmp_path, "main", lock_timeout_seconds=0.5,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_fetch_base_ref_propagates_fetch_errors_unchanged(
    monkeypatch, tmp_path,
):
    # Issue #171: a real fetch/network/ref error must propagate
    # fail-fast (the CalledProcessError with its stderr), never be
    # swallowed or retried silently.
    error = subprocess.CalledProcessError(
        128, ["git", "fetch", "origin", "main"],
        stderr="fatal: cannot lock ref 'refs/remotes/origin/main'",
    )

    def fake_run(command, **kwargs):
        raise error

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.fetch_base_ref(tmp_path, "main")


def test_fetch_base_ref_releases_the_lock_on_fetch_failure(
    monkeypatch, tmp_path,
):
    # Issue #171: a failed fetch must still release the lock — the
    # next fetch (any Runner, any Pi session) must not inherit a stuck
    # lock.
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            128, command, stderr="fatal: unable to access",
        )

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        runner.fetch_base_ref(tmp_path, "main")

    _lock_free(tmp_path)


# ---------------------------------------------------------------------------
# review_and_merge_if_clean (the wait-loop review step)
# ---------------------------------------------------------------------------

def _pass_verdict_text():
    return "REVIEW_VERDICT " + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0, "minors": 0,
        "findings": [],
    })


def _findings_verdict_text():
    return "REVIEW_VERDICT " + json.dumps({
        "verdict": "findings", "blockers": 1, "majors": 0, "minors": 0,
        "findings": [{"level": "Blocker", "location": "a.py:1", "note": "x"}],
    })


def _pr():
    return {"number": 4, "url": "u", "base_ref": "main", "base_oid": "b1",
            "head_ref": "h", "head_oid": "h1"}


def _review_merge_config(tmp_path):
    return {
        "repo_dir": tmp_path,
        "base_branch": "main",
        "base_sha": "b1",
        "run_id": "a1b2c3d4",
    }


def test_review_and_merge_clean_verdict_merges_and_labels_merged(
        monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        runner, "issue_comments", lambda *a, **k: [],
    )
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate", lambda *a, **k: {**_pr(), "merged": True},
    )
    monkeypatch.setattr(
        runner, "confirm_merged",
        lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(runner, "sync_base_checkout",
                        lambda *a, **k: calls.append("sync"))
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *a, **k: calls.append(("edit", k)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("comment", k.get("body"))),
    )
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is True
    assert "sync" in calls
    assert ("edit", {"repo": "owner/repo", "add": "ai-merged",
                     "remove": "ai-pr-opened"}) in calls
    comment = [c for c in calls if c[0] == "comment"][0][1]
    assert "Orbi merged PR: u" in comment
    assert "merge_commit=m1" in comment
    assert "review_rounds=1" in comment
    assert "<!-- orbi:run=a1b2c3d4 -->" in comment
    # Delivery labels land before the local checkout sync: a sync
    # failure must not rewrite a landed merge as ai-blocked.
    assert calls.index(("edit", {"repo": "owner/repo", "add": "ai-merged",
                                 "remove": "ai-pr-opened"})) < calls.index("sync")


def test_review_and_merge_refreezes_head_after_in_session_fix(
        monkeypatch, tmp_path, caplog):
    """Issue #82: the review session fixes findings in the same session
    and pushes the task branch, so the head the verdict covers is NEWER
    than the frozen head. The PR must be RE-FROZEN before the merge
    gate: the gate (and the `--match-head-commit` merge) run against
    the fixed head, not the frozen one."""
    calls = []
    frozen = _pr()
    fixed = {**_pr(), "head_oid": "h2"}
    heads = iter([frozen, fixed])

    def fake_freeze(*a, **k):
        return next(heads)

    monkeypatch.setattr(runner, "freeze_pr", fake_freeze)
    monkeypatch.setattr(
        runner, "issue_comments", lambda *a, **k: [],
    )
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())

    def fake_gate(worktree, pr, base_branch, *, repo_dir):
        calls.append(("gate", pr["head_oid"], repo_dir))
        return {**pr, "merged": True}

    monkeypatch.setattr(runner, "merge_gate", fake_gate)
    monkeypatch.setattr(
        runner, "confirm_merged",
        lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(runner, "sync_base_checkout", lambda *a, **k: None)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    caplog.set_level("INFO")
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is True
    # The merge gate ran against the RE-FROZEN (fixed) head... with the
    # deployment checkout as the base-sync lock location (Issue #171).
    assert calls == [("gate", "h2", tmp_path)]
    # ...and the head advance is logged for the journal.
    assert "review_head_advanced" in caplog.text
    assert "frozen=h1" in caplog.text
    assert "reviewed=h2" in caplog.text


def test_review_and_merge_clean_verdict_without_head_advance_keeps_frozen_head(
        monkeypatch, tmp_path, caplog):
    """Issue #82: when the review session did not push (clean PR,
    nothing to fix), the re-freeze returns the same head and the merge
    gate runs against it unchanged (no head-advance log)."""
    calls = []
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(
        runner, "issue_comments", lambda *a, **k: [],
    )
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())

    def fake_gate(worktree, pr, base_branch, *, repo_dir):
        calls.append(("gate", pr["head_oid"], repo_dir))
        return {**pr, "merged": True}

    monkeypatch.setattr(runner, "merge_gate", fake_gate)
    monkeypatch.setattr(
        runner, "confirm_merged",
        lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(runner, "sync_base_checkout", lambda *a, **k: None)
    monkeypatch.setattr(runner, "edit_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    caplog.set_level("INFO")
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is True
    # ...with the deployment checkout as the lock location (Issue #171).
    assert calls == [("gate", "h1", tmp_path)]
    assert "review_head_advanced" not in caplog.text


def test_review_and_merge_keeps_merged_when_checkout_sync_fails(
        monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate", lambda *a, **k: {**_pr(), "merged": True},
    )
    monkeypatch.setattr(
        runner, "confirm_merged",
        lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )

    def boom(*a, **k):
        raise RuntimeError("deployment checkout cannot fast-forward")

    monkeypatch.setattr(runner, "sync_base_checkout", boom)
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *a, **k: calls.append(("edit", k)),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("comment", k.get("body"))),
    )
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is True
    assert ("edit", {"repo": "owner/repo", "add": "ai-merged",
                     "remove": "ai-pr-opened"}) in calls
    assert any(
        c[0] == "comment" and "Orbi merged PR:" in (c[1] or "")
        for c in calls
    )
    assert not any(
        isinstance(c, tuple) and c[0] == "edit" and c[1].get("add") == "ai-blocked"
        for c in calls
    )


def test_review_and_merge_findings_labels_fix_needed_and_comments(
        monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(
        runner, "run_review", lambda *a, **k: _findings_verdict_text(),
    )
    monkeypatch.setattr(runner, "merge_gate", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("no merge")))
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("issue", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "comment_pr", lambda *a, **k: calls.append(("pr", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *a, **k: calls.append(("edit", k)),
    )
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is False
    # The findings are recorded on Issue and PR with the run marker...
    assert calls[0][0] == "issue"
    assert "a.py:1" in calls[0][1]
    assert "<!-- orbi:run=a1b2c3d4 -->" in calls[0][1]
    assert "Orbi review round 1 for PR #4" in calls[0][1]
    assert calls[1][0] == "pr"
    assert "a.py:1" in calls[1][1]
    # ...and the Issue moves to the explicit fix state (the #45 fix loop
    # repairs the same PR; a clean PR is never sent to the Fixer).
    assert calls[2] == ("edit", {"repo": "owner/repo", "add": "ai-fix-needed",
                                 "remove": "ai-pr-opened"})


def test_review_and_merge_behind_base_labels_fix_needed(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(
                "PR #4 head h1 is behind latest remote base origin/main; "
                "absorb the latest base, rerun tests and review, then retry"
            ),
        ),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("issue", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "comment_pr", lambda *a, **k: calls.append(("pr", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *a, **k: calls.append(("edit", k)),
    )
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is False
    # A behind head is never merged: the fixer absorbs the latest base.
    assert "behind the latest base" in calls[0][1]
    assert calls[2] == ("edit", {"repo": "owner/repo", "add": "ai-fix-needed",
                                 "remove": "ai-pr-opened"})


def test_review_and_merge_conflict_labels_fix_needed(monkeypatch, tmp_path):
    """A CONFLICTING/DIRTY PR is a fixer job, never a terminal block.

    Issue #34: behind or conflict -> absorb latest main, resolve, retest,
    re-review. ai-blocked is only for unrecoverable failures.
    """
    calls = []
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("PR #4 is not mergeable (mergeable=CONFLICTING)"),
        ),
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("issue", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "comment_pr", lambda *a, **k: calls.append(("pr", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "edit_issue",
        lambda *a, **k: calls.append(("edit", k)),
    )
    make_fake_gh(monkeypatch)
    merged = runner.review_and_merge_if_clean(
        tmp_path, "branch", "main", _review_merge_config(tmp_path),
        "owner/repo", 4, title="Review task",
        priority="normal",
    )
    assert merged is False
    assert "merge conflict" in calls[0][1] or "not mergeable" in calls[0][1]
    assert calls[2] == ("edit", {"repo": "owner/repo", "add": "ai-fix-needed",
                                 "remove": "ai-pr-opened"})


def test_review_and_merge_reraises_non_fixable_gate_error(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(
                "PR #4 head moved since review "
                "(reviewed=h1 remote=moved); re-review before merging"
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="head moved since review"):
        make_fake_gh(monkeypatch)
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", _review_merge_config(tmp_path),
            "owner/repo", 4, title="Review task",
            priority="normal",
        )


def test_review_and_merge_missing_verdict_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(
        runner, "run_review", lambda *a, **k: "review without a verdict",
    )
    with pytest.raises(ValueError, match="no REVIEW_VERDICT"):
        make_fake_gh(monkeypatch)
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", _review_merge_config(tmp_path),
            "owner/repo", 4, title="Review task",
            priority="normal",
        )


def test_review_and_merge_exhausted_rounds_raises(monkeypatch, tmp_path, caplog):
    comments = [
        {"body": f"Orbi review round {i} for PR #4: 1 blocker(s), "
                 "0 major(s). Findings: []",
         "authorAssociation": "OWNER"}
        for i in range(1, runner.MAX_REVIEW_ROUNDS + 1)
    ]
    monkeypatch.setattr(runner, "issue_comments", lambda *a, **k: comments)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError,
        match=f"exhausted after {runner.MAX_REVIEW_ROUNDS} rounds",
    ):
        make_fake_gh(monkeypatch)
        runner.review_and_merge_if_clean(
            tmp_path, "branch", "main", _review_merge_config(tmp_path),
            "owner/repo", 4, title="Review task",
            priority="normal",
        )
    assert "review_rounds_exhausted" in caplog.text
