"""v0.1.0 release reconciliation record (Issue #97).

The release state reconciliation is a one-time GitHub-state task: the
durable, auditable artifact in the repository is the reconciliation
record ``docs/release-v0.1.0.md``. These tests pin that record against
the REAL git history and the REAL CLI:

- the tag/commit relationship the record claims (``v0.1.0`` →
  ``335fa274``, corrected ``v0.1.1`` → ``f82969e9``, PR #92 merge
  commit ``f6c065e8``) is verified with actual ``git`` commands on this
  repository — a record that guessed the relationship would fail;
- every #80 checklist item has a conclusive verdict in the record
  (MERGED with PR evidence, verified incomplete, or excluded);
- the ``muyan_pilot.py session`` CLI contract the record cites is
  asserted against one real call (``session --help``), not against a
  guessed shape.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORD = REPO_ROOT / "docs" / "release-v0.1.0.mdx"

# The real release objects (verified against origin with
# `git ls-remote --tags origin` and `gh pr view 92` — see the record).
V010_TAG_OBJECT = "912631d390b0b1f7039afac52125ecf7e1d92589"
V010_COMMIT = "335fa274e4b451a025bc2426b2a9fe7297ce8bb2"
V011_COMMIT = "f82969e974b17803759493bad7e339bdcd6f463f"
PR92_MERGE_COMMIT = "f6c065e8ec7ec53d7e9fb99e6475c1bc16c2dabf"

# Every #80 core checklist item must carry a conclusive verdict.
CORE_ISSUES = (
    75, 77, 78, 79, 82, 52, 53, 71, 73, 74, 49, 76, 81, 83,
)


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def record_text() -> str:
    assert RECORD.is_file(), (
        f"missing release reconciliation record: {RECORD}"
    )
    return RECORD.read_text(encoding="utf-8")


def test_record_pins_real_tag_commit_relationship(record_text: str):
    """Issue #97: the record must name the real tag object and commit
    SHAs — and those SHAs must form the claimed relationship in the
    actual git history (the record cannot assert a guessed
    relationship green)."""
    for sha in (V010_TAG_OBJECT, V010_COMMIT, V011_COMMIT, PR92_MERGE_COMMIT):
        assert sha in record_text, f"record must pin SHA {sha}"
    # The pinned objects must be real objects of this repository: the
    # tag object is a tag, the commit SHAs resolve to commits.
    assert git("cat-file", "-t", V010_TAG_OBJECT) == "tag", (
        "the pinned v0.1.0 tag object is not a tag object here"
    )
    for sha in (V010_COMMIT, V011_COMMIT, PR92_MERGE_COMMIT):
        assert git("rev-parse", f"{sha}^{{commit}}") == sha, (
            f"the pinned SHA {sha} does not resolve to a commit"
        )
    # The real history says: v0.1.0 target is an ancestor of the
    # corrected head, and the corrected head is the PR #92 branch head
    # (an ancestor of the merge commit). The record's claim must match.
    assert is_ancestor(V010_COMMIT, V011_COMMIT), (
        "record claim contradicted by history: v0.1.0 target is not an "
        "ancestor of the corrected head"
    )
    assert is_ancestor(V011_COMMIT, PR92_MERGE_COMMIT), (
        "record claim contradicted by history: corrected head is not "
        "inside the PR #92 merge commit"
    )
    assert not is_ancestor(PR92_MERGE_COMMIT, V010_COMMIT), (
        "record claim contradicted by history: the merge commit cannot "
        "be inside the v0.1.0 target"
    )


def test_record_names_review_fixes_missing_from_v010(record_text: str):
    """Issue #97: the corrected release notes must state which fixes
    the new tag adds over v0.1.0 (the review-round-1 commit)."""
    assert "f82969e" in record_text
    lowered = record_text.lower()
    assert "review" in lowered, (
        "record must explain that the missing commit is the PR #92 "
        "review-round-1 fix"
    )


def test_record_has_conclusive_verdict_for_every_core_issue(
    record_text: str,
):
    """Issue #97: every #80 checklist item gets a MERGED / verified
    incomplete / excluded verdict — no silent checklist gaps."""
    for number in CORE_ISSUES:
        pattern = re.compile(
            rf"#{number}\b[^\n]*\b(MERGED|verified incomplete|excluded)",
            re.IGNORECASE,
        )
        assert pattern.search(record_text), (
            f"issue #{number} has no conclusive verdict in the record"
        )


def test_record_verdict_73_closed_by_pr92(record_text: str):
    """Issue #97: #73 (verify against docs) was implemented by PR #92
    and must be recorded as MERGED/closed with that evidence."""
    line = next(
        line for line in record_text.splitlines() if re.search(r"#73\b", line)
    )
    assert "92" in line, "#73 verdict must cite PR #92 as the evidence"


def test_record_verdict_93_verified_incomplete(record_text: str):
    """Issue #97: #93 (Epic mechanism) has no implementation evidence —
    the record must keep it OPEN (verified incomplete), never close it
    to tick the checklist."""
    line = next(
        line for line in record_text.splitlines() if re.search(r"#93\b", line)
    )
    assert re.search(r"verified incomplete|stays? open", line, re.IGNORECASE), (
        "#93 must be recorded as verified incomplete / stays OPEN"
    )


def test_git_helper_fails_fast_on_nonzero_exit():
    """The helper must fail fast (no silent swallow) when git exits
    non-zero — the same contract as the other git smoke helpers."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git("rev-parse", "no-such-ref")


def test_session_cli_contract_matches_one_real_call():
    """Issue #74/#97: the record cites the `session` CLI — assert the
    real contract with one real call (verified against the actual
    `python3 -m muyan_pilot.cli session --help` output, Issue #168
    src layout), not a guessed shape."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "muyan_pilot.cli", "session", "--help"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"session --help failed rc={result.returncode} "
        f"stderr={result.stderr.strip()}"
    )
    assert "--follow" in result.stdout
    assert "--pretty" in result.stdout
    assert "session" in result.stdout
