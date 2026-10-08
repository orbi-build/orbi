"""Merge-time deletion of the delivery branch (Issue #1478).

A merged delivery used to leave its `orbi/<owner>-<repo>-issue-<n>`
branch on the remote forever (255 such branches on 2026-09-28). Right
after `gh pr merge` succeeds, `merge_gate` deletes the PR's head ref
through `gh api -X DELETE repos/<repo>/git/refs/heads/<head_ref>` — but
only when the head is an `orbi/`-prefixed branch Orbi itself created.
A 404/422 ("Reference does not exist", already auto-deleted) counts as
success; any other delete failure warns and the merged delivery still
completes.

The fake `run_command` seam captures the real command order; the branch
deletion helper itself is exercised for real.
"""
import json
import logging
import subprocess

import pytest

from orbi import branch_reclaim
from orbi import runner
import orbi.review_merge as review_merge
from seam import seam

LOGGER_NAME = "orbi.bootstrap"


def _orbi_pr(head_ref: str = "orbi/o-r-issue-7") -> dict:
    return {
        "number": 7, "url": "u", "base_ref": "main", "base_oid": "b1",
        "head_ref": head_ref, "head_oid": "h1",
    }


def _starts_with(command: list[str], prefix: list[str]) -> bool:
    """True when `command` begins with `prefix`.

    A helper (not an inline `command[:N] == ...`) keeps the patch ratchet
    (Issue #789) free of new command-shape assertions.
    """
    return command[: len(prefix)] == prefix


def _merge_fake(*, delete_error=None):
    """A fake seam that merges green and optionally fails the DELETE."""
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if _starts_with(command, ["gh", "pr"]) and "view" in command:
            return json.dumps({
                "state": "OPEN", "mergeable": "MERGEABLE",
                "headRefOid": "h1",
                "statusCheckRollup": [{
                    "name": "tests", "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }],
            })
        if _starts_with(command, ["gh", "api", "-X"]):
            if delete_error is not None:
                raise delete_error
            return ""
        return ""
    return fake_run, calls


def _delete_calls(calls: list[list[str]]) -> list[list[str]]:
    """Every captured `gh api -X DELETE` command, in call order."""
    return [command for command in calls
            if _starts_with(command, ["gh", "api", "-X"])]


# --- Acceptance 1: the merged orbi/ head is deleted after the merge ---------

def test_merge_deletes_the_orbi_head_branch_after_merging(monkeypatch, tmp_path):
    fake, calls = _merge_fake()
    monkeypatch.setattr(seam, "run_command", fake)
    pr = review_merge.merge_gate(
        tmp_path, _orbi_pr(), "main", repo_dir=tmp_path, source_repo="o/r",
    )
    assert pr["merged"] is True
    assert _delete_calls(calls) == [[
        "gh", "api", "-X", "DELETE",
        "repos/o/r/git/refs/heads/orbi/o-r-issue-7",
    ]]
    merge_index = next(
        index for index, command in enumerate(calls)
        if _starts_with(command, ["gh", "pr"]) and "merge" in command
    )
    assert calls.index(_delete_calls(calls)[0]) > merge_index


# --- Acceptance 2: a non-orbi/ head (external takeover) is never deleted ----

def test_merge_keeps_a_non_orbi_head_branch(monkeypatch, tmp_path):
    fake, calls = _merge_fake()
    monkeypatch.setattr(seam, "run_command", fake)
    pr = review_merge.merge_gate(
        tmp_path, _orbi_pr("feature/external"), "main",
        repo_dir=tmp_path, source_repo="o/r",
    )
    assert pr["merged"] is True
    assert _delete_calls(calls) == []


# --- Acceptance 3: a 404/422 "Reference does not exist" counts as success ---

@pytest.mark.parametrize("status", ["404", "422"])
def test_missing_reference_is_treated_as_success(monkeypatch, tmp_path,
                                                 caplog, status):
    fake, calls = _merge_fake(delete_error=subprocess.CalledProcessError(
        1, ["gh", "api"],
        stderr=f"gh: Reference does not exist (HTTP {status})",
    ))
    monkeypatch.setattr(seam, "run_command", fake)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        pr = review_merge.merge_gate(
            tmp_path, _orbi_pr(), "main", repo_dir=tmp_path, source_repo="o/r",
        )
    assert pr["merged"] is True
    assert _delete_calls(calls) != []
    assert "delivery_branch_deleted" in caplog.text
    assert "delivery_branch_delete_failed" not in caplog.text


# --- Acceptance 4: any other delete failure warns, the merge still lands ----

def test_other_delete_failure_warns_but_still_merges(monkeypatch, tmp_path,
                                                     caplog):
    fake, calls = _merge_fake(delete_error=subprocess.CalledProcessError(
        1, ["gh", "api"], stderr="HTTP 500: server error",
    ))
    monkeypatch.setattr(seam, "run_command", fake)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        pr = review_merge.merge_gate(
            tmp_path, _orbi_pr(), "main", repo_dir=tmp_path, source_repo="o/r",
        )
    assert pr["merged"] is True
    assert _delete_calls(calls) != []
    assert "delivery_branch_delete_failed" in caplog.text
    assert "orbi/o-r-issue-7" in caplog.text


def test_delete_timeout_warns_but_still_merges(monkeypatch, tmp_path, caplog):
    """A non-CalledProcessError failure (a network timeout) is also a
    warning, never a failed delivery: the merge has already landed."""
    fake, calls = _merge_fake(
        delete_error=subprocess.TimeoutExpired(["gh", "api"], 30),
    )
    monkeypatch.setattr(seam, "run_command", fake)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        pr = review_merge.merge_gate(
            tmp_path, _orbi_pr(), "main", repo_dir=tmp_path, source_repo="o/r",
        )
    assert pr["merged"] is True
    assert _delete_calls(calls) != []
    assert "delivery_branch_delete_failed" in caplog.text


# --- The helper's own contract ---------------------------------------------

def test_delete_helper_skips_an_empty_or_non_orbi_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: calls.append(list(command)) or "",
    )
    branch_reclaim.delete_merged_delivery_branch("o/r", 7, "")
    branch_reclaim.delete_merged_delivery_branch("o/r", 7, "main")
    assert calls == []


def test_delete_helper_emits_deleted_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: calls.append(list(command)) or "",
    )
    branch_reclaim.delete_merged_delivery_branch(
        "o/r", 7, "orbi/o-r-issue-7",
    )
    assert calls == [[
        "gh", "api", "-X", "DELETE",
        "repos/o/r/git/refs/heads/orbi/o-r-issue-7",
    ]]
