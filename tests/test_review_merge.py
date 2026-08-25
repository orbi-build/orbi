"""Unit tests for the auto review/fix/merge orchestration (Issue #34).

The Runner (not Pi) closes the delivery loop: after the implementer opens a PR
it freezes the exact PR base/head SHA, runs an independent review session,
loops a fixer session while Blocker/Major findings exist, re-checks the merge
gate against the latest origin/main, and merges via `gh pr merge
--match-head-commit`. Pi never pushes main.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import bootstrap_runner as runner


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
# run_review / run_fix command construction
# ---------------------------------------------------------------------------

def _review_config(tmp_path, prompt_name="prompt_review.md"):
    prompt = tmp_path / prompt_name
    prompt.write_text("REVIEW PROMPT {{PR_NUMBER}} {{BASE_SHA}} {{HEAD_SHA}}",
                      encoding="utf-8")
    return {
        "prompt_review": prompt,
        "prompt_fix": tmp_path / "prompt_fix.md",
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [tmp_path / "code-review.md"],
        "base_branch": "main",
        "base_sha": "b1",
        "run_id": "run1",
    }


def test_run_review_launches_independent_readonly_pi_session(monkeypatch, tmp_path):
    (tmp_path / "prompt_fix.md").write_text("fix", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    pr = {"number": 4, "url": "u", "base_ref": "main", "base_oid": "b1",
          "head_ref": "h", "head_oid": "h1"}
    out = runner.run_review(tmp_path, pr, _review_config(tmp_path),
                            "owner/repo", 1)
    assert out == "done"
    command, kwargs = calls[0]
    # Independent session dir, review skill, redacted log.
    assert command[0] == "pi"
    assert "--skill" in command
    assert str(tmp_path / "code-review.md") in command
    session_dir = command[command.index("--session-dir") + 1]
    assert "review" in session_dir
    assert "round-1" in session_dir
    # Reviewer system prompt carries the frozen PR number and base/head SHA.
    system_prompt = command[command.index("--system-prompt") + 1]
    assert " 4 " in system_prompt
    assert "b1" in system_prompt
    assert "h1" in system_prompt
    assert kwargs["cwd"] == tmp_path
    assert kwargs["log_stdout"] is True
    assert kwargs["log_command"][-2:] == ["<redacted>", "<issue-context-redacted>"]


def test_run_fix_passes_findings_to_fixer(monkeypatch, tmp_path):
    (tmp_path / "prompt_review.md").write_text("rev", encoding="utf-8")
    (tmp_path / "prompt_fix.md").write_text("FIX PROMPT {{PR_NUMBER}}",
                                             encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    pr = {"number": 4, "url": "u", "base_ref": "main", "base_oid": "b1",
          "head_ref": "h", "head_oid": "h1"}
    findings = [{"level": "Blocker", "location": "a.py:1", "note": "x"}]
    runner.run_fix(tmp_path, pr, _review_config(tmp_path), "owner/repo",
                   findings, 1)
    command, kwargs = calls[0]
    session_dir = command[command.index("--session-dir") + 1]
    assert "fix" in session_dir
    assert "round-1" in session_dir
    # The findings are injected into the fixer context so it can act on them.
    assert "a.py:1" in command[8]
    assert "Blocker" in command[8]
    assert kwargs["cwd"] == tmp_path


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
# review_fix_merge loop
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


def test_review_fix_merge_passes_first_round_and_merges(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(runner, "merge_gate", lambda *a, **k: {**_pr(), "merged": True})
    monkeypatch.setattr(
        runner, "confirm_merged", lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: calls.append("issue"))
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: calls.append("pr"))
    monkeypatch.setattr(runner, "run_fix", lambda *a, **k: calls.append("fix"))
    result = runner.review_fix_merge(tmp_path, "branch", "main", {}, "owner/repo", 4)
    assert result["rounds"] == 1
    assert result["merge_commit"] == "m1"
    assert result["verdict"]["verdict"] == "pass"
    assert calls == []  # no findings, no fix, no comments


def test_review_fix_merge_fixes_findings_then_passes(monkeypatch, tmp_path):
    calls = []
    verdicts = iter([_findings_verdict_text(), _pass_verdict_text()])
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: next(verdicts))
    monkeypatch.setattr(runner, "merge_gate", lambda *a, **k: {**_pr(), "merged": True})
    monkeypatch.setattr(
        runner, "confirm_merged", lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(
        runner, "comment_issue",
        lambda *a, **k: calls.append(("issue", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "comment_pr", lambda *a, **k: calls.append(("pr", k.get("body"))),
    )
    monkeypatch.setattr(
        runner, "run_fix",
        lambda *a, **k: calls.append(("fix", a[4])),  # findings passed to fixer
    )
    result = runner.review_fix_merge(tmp_path, "branch", "main", {}, "owner/repo", 4)
    assert result["rounds"] == 2
    assert result["merge_commit"] == "m1"
    # Round 1: findings commented to issue and PR, fixer invoked with findings.
    assert calls[0][0] == "issue" and "a.py:1" in calls[0][1]
    assert calls[1][0] == "pr" and "a.py:1" in calls[1][1]
    assert calls[2][0] == "fix" and calls[2][1][0]["location"] == "a.py:1"


def test_review_fix_merge_absorbs_behind_base_then_merges(monkeypatch, tmp_path):
    fix_calls = []
    gate_calls = []

    def fake_merge_gate(worktree, pr, base_branch):
        gate_calls.append(pr["head_oid"])
        if len(gate_calls) == 1:
            raise RuntimeError(
                "PR #4 head h1 is behind latest remote base origin/main; "
                "absorb the latest base, rerun tests and review, then retry"
            )
        return {**_pr(), "merged": True}

    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(runner, "merge_gate", fake_merge_gate)
    monkeypatch.setattr(
        runner, "confirm_merged", lambda *a, **k: {"state": "MERGED", "merge_commit": "m1"},
    )
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    monkeypatch.setattr(
        runner, "run_fix",
        lambda *a, **k: fix_calls.append(a[4]),
    )
    result = runner.review_fix_merge(tmp_path, "branch", "main", {}, "owner/repo", 4)
    # Round 1 was behind (fixer invoked to absorb the base); round 2 merged.
    assert result["rounds"] == 2
    assert result["merge_commit"] == "m1"
    assert len(fix_calls) == 1
    assert fix_calls[0][0]["location"] == "base"
    assert gate_calls == ["h1", "h1"]


def test_review_fix_merge_reraises_non_behind_gate_error(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _pass_verdict_text())
    monkeypatch.setattr(
        runner, "merge_gate",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("PR #4 is not mergeable (mergeable=DIRTY)"),
        ),
    )
    with pytest.raises(RuntimeError, match="not mergeable"):
        runner.review_fix_merge(tmp_path, "branch", "main", {}, "owner/repo", 4)


def test_review_fix_merge_exhausts_rounds_and_fails(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(runner, "freeze_pr", lambda *a, **k: _pr())
    monkeypatch.setattr(runner, "run_review", lambda *a, **k: _findings_verdict_text())
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "comment_pr", lambda *a, **k: None)
    monkeypatch.setattr(runner, "run_fix", lambda *a, **k: None)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="exhausted after 2 rounds",
    ):
        runner.review_fix_merge(tmp_path, "branch", "main", {}, "owner/repo", 4,
                                max_rounds=2)
    assert "review_fix_merge_exhausted" in caplog.text
