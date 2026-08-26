"""Unit tests for the auto review/fix/merge orchestration (Issues #34, #82).

The Runner (not Pi) closes the delivery loop: after the implementer opens a PR
it freezes the exact PR base/head SHA, runs one independent review session
that fixes Blocker/Major findings IN THE SAME SESSION (Issue #82: no
cold-start fixer, no third review), re-freezes the head after a clean
verdict (the reviewer may have pushed a fix), re-checks the merge gate
against the latest origin/main, and merges via `gh pr merge
--match-head-commit`. Pi never pushes main.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner
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
        "headRefName": "muyan-pilot/owner-repo-issue-4-run1",
        "headRefOid": head,
    }])


def test_freeze_pr_returns_frozen_base_and_head(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _pr_json()

    monkeypatch.setattr(runner, "run_command", fake_run)
    pr = runner.freeze_pr(
        tmp_path, "muyan-pilot/owner-repo-issue-4-run1", "main",
    )
    assert pr["number"] == 4
    assert pr["base_ref"] == "main"
    assert pr["base_oid"] == "b1"
    assert pr["head_oid"] == "h1"
    assert pr["url"].endswith("/pull/4")
    assert calls == [[
        "gh", "pr", "list", "--state", "open", "--head",
        "muyan-pilot/owner-repo-issue-4-run1",
        "--json", "number,url,baseRefName,baseRefOid,headRefName,headRefOid",
        "--limit", "2",
    ]]


def test_freeze_pr_rejects_wrong_base(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command", lambda command, **kwargs: _pr_json(base="develop"),
    )
    with pytest.raises(RuntimeError, match="PR base is develop, expected main"):
        runner.freeze_pr(tmp_path, "muyan-pilot/owner-repo-issue-4-run1", "main")


def test_freeze_pr_rejects_no_open_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: "[]")
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.freeze_pr(tmp_path, "muyan-pilot/owner-repo-issue-4-run1", "main")


def test_freeze_pr_rejects_multiple_open_prs(monkeypatch, tmp_path):
    two = json.dumps([
        {"number": 4, "url": "u4", "baseRefName": "main",
         "baseRefOid": "b1", "headRefName": "h", "headRefOid": "h1"},
        {"number": 5, "url": "u5", "baseRefName": "main",
         "baseRefOid": "b1", "headRefName": "h", "headRefOid": "h2"},
    ])
    monkeypatch.setattr(runner, "run_command", lambda command, **kwargs: two)
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.freeze_pr(tmp_path, "muyan-pilot/owner-repo-issue-4-run1", "main")


# ---------------------------------------------------------------------------
# run_review command construction (streamed, role=review)
# ---------------------------------------------------------------------------

def _review_config(tmp_path, prompt_name="prompt_review.md"):
    prompt = tmp_path / prompt_name
    prompt.write_text("REVIEW PROMPT {{PR_NUMBER}} {{BASE_SHA}} {{HEAD_SHA}}",
                      encoding="utf-8")
    return {
        "prompt_review": prompt,
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
                            "owner/repo", 4, "muyan-pilot/owner-repo-issue-4-run1", 1)
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
    assert kwargs["branch"] == "muyan-pilot/owner-repo-issue-4-run1"
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
                           "main")
    assert pr["merged"] is True
    # The merge must use --match-head-commit with the reviewed head SHA.
    merge_cmd = [c for c in calls if c[:2] == ["gh", "pr"] and "merge" in c][0]
    assert "--match-head-commit" in merge_cmd
    assert merge_cmd[merge_cmd.index("--match-head-commit") + 1] == "h1"
    assert "--merge" in merge_cmd


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
                                     "head_oid": "h1"}, "main")
    assert "base_branch=main" in caplog.text


def test_merge_gate_rejects_non_mergeable_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_command", _merge_gate_fake(pr_state="DIRTY"))
    with pytest.raises(RuntimeError, match="not mergeable"):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                                     "base_oid": "b1", "head_ref": "h",
                                     "head_oid": "h1"}, "main")


def test_merge_gate_rejects_head_that_moved_since_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command", _merge_gate_fake(head_oid="moved"),
    )
    with pytest.raises(RuntimeError, match="head moved since review"):
        runner.merge_gate(tmp_path, {"number": 4, "url": "u", "base_ref": "main",
                                     "base_oid": "b1", "head_ref": "h",
                                     "head_oid": "h1"}, "main")


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
    )
    assert result["state"] == "MERGED"
    assert result["merge_commit"] == "m1"


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
            "main",
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
            "main",
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
            "main",
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
    runner.comment_pr(4, body="round 1 findings")
    assert calls == [["gh", "pr", "comment", "4", "--body", "round 1 findings"]]


# ---------------------------------------------------------------------------
# review_rounds_so_far
# ---------------------------------------------------------------------------

def _round_comment(round_no, pr_number=4, association="OWNER"):
    return {
        "body": (
            "<!-- muyan-pilot:run=run1 -->\n"
            f"Muyan Pilot review round {round_no} for PR #{pr_number}: "
            "1 blocker(s), 0 major(s). Findings: []"
        ),
        "authorAssociation": association,
    }


def test_review_rounds_so_far_counts_recorded_rounds():
    comments = [
        {"body": "Muyan Pilot opened PR: https://x",
         "authorAssociation": "OWNER"},
        _round_comment(1),
        {"body": "Muyan Pilot fixed PR: https://x",
         "authorAssociation": "OWNER"},
        _round_comment(2),
        {"body": None, "authorAssociation": "OWNER"},
    ]
    assert runner.review_rounds_so_far(comments) == 2


def test_review_rounds_so_far_ignores_comments_without_round_line():
    assert runner.review_rounds_so_far([
        {"body": "Muyan Pilot started Pi: ...", "authorAssociation": "OWNER"},
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
        "owner/repo", 4,
    )
    assert merged is True
    assert "sync" in calls
    assert ("edit", {"repo": "owner/repo", "add": "ai-merged",
                     "remove": "ai-pr-opened"}) in calls
    comment = [c for c in calls if c[0] == "comment"][0][1]
    assert "Muyan Pilot merged PR: u" in comment
    assert "merge_commit=m1" in comment
    assert "review_rounds=1" in comment
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in comment
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

    def fake_gate(worktree, pr, base_branch):
        calls.append(("gate", pr["head_oid"]))
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
        "owner/repo", 4,
    )
    assert merged is True
    # The merge gate ran against the RE-FROZEN (fixed) head...
    assert calls == [("gate", "h2")]
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

    def fake_gate(worktree, pr, base_branch):
        calls.append(("gate", pr["head_oid"]))
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
        "owner/repo", 4,
    )
    assert merged is True
    assert calls == [("gate", "h1")]
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
        "owner/repo", 4,
    )
    assert merged is True
    assert ("edit", {"repo": "owner/repo", "add": "ai-merged",
                     "remove": "ai-pr-opened"}) in calls
    assert any(
        c[0] == "comment" and "Muyan Pilot merged PR:" in (c[1] or "")
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
        "owner/repo", 4,
    )
    assert merged is False
    # The findings are recorded on Issue and PR with the run marker...
    assert calls[0][0] == "issue"
    assert "a.py:1" in calls[0][1]
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in calls[0][1]
    assert "Muyan Pilot review round 1 for PR #4" in calls[0][1]
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
        "owner/repo", 4,
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
        "owner/repo", 4,
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
            "owner/repo", 4,
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
            "owner/repo", 4,
        )


def test_review_and_merge_exhausted_rounds_raises(monkeypatch, tmp_path, caplog):
    comments = [
        {"body": f"Muyan Pilot review round {i} for PR #4: 1 blocker(s), "
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
            "owner/repo", 4,
        )
    assert "review_rounds_exhausted" in caplog.text
