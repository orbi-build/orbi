"""Thin-ticket clarification gate (Issue #1088, trimmed in #1379).

The gate is a model judgment, so the unit tests stub the seam
(`seam.run_ticket_agent`, `seam.judge_issue_body`, `seam.comment_issue`,
`seam.edit_issue`) and the end-to-end tests run the REAL runner against a
real local git clone with a fake `pi` and a stateful fake `gh`: a thin
ticket stops with one comment and a label swap (and no worktree, branch,
session or PR), a ticket the gate accepts is delivered normally, and a
failed judgment is a bypass (model error).
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import git

from orbi import clarify, config as config_domain
from orbi import delivery_labels

import orbi.runner as runner
from seam import seam

from tests.test_run_id_e2e import (
    ISSUE_NUMBER,
    PR_URL,
    REPO,
    config_for,
    install_fake_gh,
    install_fake_pi,
    worktree_for,
)

ISSUE = {"number": 7, "title": "Thin ticket", "body": "Make it better."}
RUN_ID = "a1b2c3d4"

# A thin `ai-ready` ticket as the claim scan sees it. `author` is the
# `gh issue list --json author` shape: the gate reads `author.login`
# from the same payload the claim already fetched (Issue #1336).
THIN_ISSUE = {
    "number": ISSUE_NUMBER,
    "title": "Thin ticket",
    "body": "Make the thing better somehow.",
    "labels": [{"name": delivery_labels.READY_LABEL}],
    "author": {"login": "alice"},
}

# The clarify session is the `pi --no-tools` call; the delivery session is
# the ordinary one. The two-faced fake answers both: a passing verdict for
# the gate, a real commit for the implementer.
FAKE_PI_PASSING_GATE = """#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
if "--no-tools" in args:
    sys.stdout.write('{"missing": []}')
    sys.exit(0)
prompt = args[args.index("--system-prompt") + 1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
with open(os.path.join(cwd, ".orbi", "plan.md"), "w",
          encoding="utf-8") as handle:
    handle.write(f"<!-- orbi:run={run_id} -->\\n# Plan\\n\\nrun_id={run_id}\\n")
with open(os.path.join(cwd, "impl.py"), "w", encoding="utf-8") as handle:
    handle.write("# impl\\n\\nagent delivery\\n")
for command in (
    ["git", "add", "."],
    ["git", "commit", "-m", f"delivery for run {run_id}"],
):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
sys.stdout.write("done")
"""


# The same fake, but the gate's verdict is the thin-ticket failure. Built
# per missing set so each check is exercised by a real run.
def fake_pi_thin(missing: list[str]) -> str:
    verdict = json.dumps({"missing": missing})
    return (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({verdict!r})\n"
    )


# The gate's model call fails (non-zero exit); the delivery session is the
# ordinary one. Issue #1088: the failure is a bypass, so the delivery runs.
FAKE_PI_MODEL_ERROR = """#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
if "--no-tools" in args:
    sys.stderr.write("model unreachable")
    sys.exit(1)
prompt = args[args.index("--system-prompt") + 1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
with open(os.path.join(cwd, ".orbi", "plan.md"), "w",
          encoding="utf-8") as handle:
    handle.write(f"<!-- orbi:run={run_id} -->\\n# Plan\\n\\nrun_id={run_id}\\n")
with open(os.path.join(cwd, "impl.py"), "w", encoding="utf-8") as handle:
    handle.write("# impl\\n")
for command in (
    ["git", "add", "."],
    ["git", "commit", "-m", f"delivery for run {run_id}"],
):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
sys.stdout.write("done")
"""


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """Local bare origin plus a clone with one commit on main."""
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
    git(clone, "push", "origin", "main")
    return clone


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    """Each test starts without a bound run id."""
    monkeypatch.setattr(seam, "_CURRENT_RUN_ID", None)


def gate_config(clone: Path, tmp_path: Path, **overrides):
    return dataclasses.replace(
        config_for(clone, tmp_path), clarify_thin_tickets=True, **overrides,
    )


# --- parse_verdict ---------------------------------------------------------

def test_parse_verdict_reads_the_missing_list():
    """One field carries the verdict: empty passes, non-empty fails."""
    assert clarify.parse_verdict('{"missing": []}') == clarify.ClarifyVerdict(
        passed=True, missing=(),
    )
    assert clarify.parse_verdict(
        '{"missing": ["single_outcome", "observable_result"]}'
    ) == clarify.ClarifyVerdict(
        passed=False, missing=("single_outcome", "observable_result"),
    )
    # The `satisfied` field is no longer read (Issue #1379): a stale one
    # must not change the verdict the `missing` list gives.
    assert clarify.parse_verdict(
        '{"satisfied": false, "missing": []}'
    ) == clarify.ClarifyVerdict(passed=True, missing=())
    assert clarify.parse_verdict(
        'My verdict follows: {"missing": []} — done.'
    ) == clarify.ClarifyVerdict(passed=True, missing=())


@pytest.mark.parametrize("output", [
    "I could not decide.",
    '{"missing": true}',
    '{"missing": [7]}',
    '{"missing": ["whatever"]}',
    '{"missing": []',
    '{"missing": [] "x": 1}',
])
def test_parse_verdict_rejects_unusable_output(output):
    assert clarify.parse_verdict(output) is None


# --- judge_issue_body ------------------------------------------------------

def test_judge_issue_body_asks_the_shared_helper_with_a_timeout(monkeypatch):
    calls = []

    def fake_agent(issue, config, source_repo, **kwargs):
        calls.append((issue, config, source_repo, kwargs))
        return '{"missing": []}'

    monkeypatch.setattr(seam, "run_ticket_agent", fake_agent)
    config = config_domain.RunnerConfig()
    verdict = clarify.judge_issue_body(ISSUE, config, REPO, RUN_ID)
    assert verdict == clarify.ClarifyVerdict(passed=True, missing=())
    issue, got_config, repo, kwargs = calls[0]
    assert (issue, got_config, repo) == (ISSUE, config, REPO)
    assert kwargs["run_id"] == RUN_ID
    assert kwargs["timeout"] == clarify.CLARIFY_TIMEOUT_SECONDS
    assert kwargs["system_prompt"] == clarify.CLARIFY_SYSTEM_PROMPT
    assert ISSUE["body"] in kwargs["context"]


def test_judge_issue_body_fails_open_when_the_agent_raises(
    monkeypatch, caplog,
):
    def boom(*args, **kwargs):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(seam, "run_ticket_agent", boom)
    caplog.set_level("INFO")
    assert clarify.judge_issue_body(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is None
    assert "clarify_check_skipped" in caplog.text
    assert "reason=RuntimeError" in caplog.text


def test_judge_issue_body_fails_open_on_a_timeout(monkeypatch, caplog):
    def timeout(*args, **kwargs):
        raise TimeoutError("judgment timed out")

    monkeypatch.setattr(seam, "run_ticket_agent", timeout)
    caplog.set_level("INFO")
    assert clarify.judge_issue_body(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is None
    assert "clarify_check_skipped" in caplog.text
    assert "reason=TimeoutError" in caplog.text


def test_judge_issue_body_fails_open_on_an_unparsable_answer(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        seam, "run_ticket_agent", lambda *a, **k: "not a verdict",
    )
    caplog.set_level("INFO")
    assert clarify.judge_issue_body(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is None
    assert "clarify_check_skipped" in caplog.text
    assert "reason=no_verdict" in caplog.text


# --- enforce / mention -----------------------------------------------------

def _record_writes(monkeypatch):
    comments: list = []
    edits: list = []

    def fake_comment(number, *, repo, body):
        comments.append((number, repo, body))

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(seam, "comment_issue", fake_comment)
    monkeypatch.setattr(seam, "edit_issue", fake_edit)
    return comments, edits


def test_enforce_removes_the_repositorys_dispatch_label(monkeypatch, caplog):
    """Issue #1088/#527: the stop removes the label the claim scan reads.

    A custom-label repository's tickets never carry `ai-ready`; removing
    a hardcoded `ai-ready` would leave the ticket claimable and the gate
    would judge (and comment) again on every tick instead of stopping.
    """
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result", "single_outcome"),
    )
    monkeypatch.setattr(seam, "judge_issue_body", lambda *a, **k: verdict)
    comments, edits = _record_writes(monkeypatch)
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(dispatch_label="ai-queue"),
        REPO, RUN_ID,
    ) is False
    assert len(comments) == 1
    body = comments[0][2]
    assert body.startswith(f"<!-- orbi:run={RUN_ID} -->")
    assert clarify.MISSING["observable_result"] in body
    assert clarify.MISSING["single_outcome"] in body
    assert "`ai-queue`" in body
    assert "`ai-ready`" not in body
    assert f"run_id={RUN_ID}" in body
    assert edits == [(
        ISSUE["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, "ai-queue",
    )]
    assert "clarify_needs_detail" in caplog.text
    assert "clarify_check_skipped" not in caplog.text


def test_author_mention_mentions_any_login():
    """Issue #1379: any login is mentioned — no ghost/[bot] special cases."""
    assert clarify._author_mention(
        {"author": {"login": "alice"}}
    ) == "@alice"
    assert clarify._author_mention(
        {"author": {"login": "renovate[bot]"}}
    ) == "@renovate[bot]"
    assert clarify._author_mention(
        {"author": {"login": "ghost"}}
    ) == "@ghost"
    # No usable login means no mention; a malformed author never fails.
    assert clarify._author_mention({}) == ""
    assert clarify._author_mention({"author": None}) == ""
    assert clarify._author_mention({"author": {"login": "  "}}) == ""


@pytest.mark.parametrize("failing_write", ["comment_issue", "edit_issue"])
def test_enforce_fails_open_when_a_write_fails(
    monkeypatch, caplog, failing_write,
):
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result",),
    )
    monkeypatch.setattr(seam, "judge_issue_body", lambda *a, **k: verdict)
    comments, edits = _record_writes(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("gh write failed")

    monkeypatch.setattr(seam, failing_write, boom)
    caplog.set_level("INFO")

    assert clarify.enforce(ISSUE, config_domain.RunnerConfig(), REPO,
                           RUN_ID) is True
    assert "clarify_check_skipped" in caplog.text
    assert "reason=RuntimeError" in caplog.text
    if failing_write == "comment_issue":
        assert edits == []
    else:
        assert len(comments) == 1


# --- real-runner end to end ------------------------------------------------

@pytest.mark.parametrize("missing", [
    ["observable_result"],
    ["acceptance_condition"],
    ["single_outcome"],
    ["observable_result", "acceptance_condition", "single_outcome"],
])
def test_thin_ticket_stops_before_any_worktree_or_session(
    clone, tmp_path, monkeypatch, caplog, missing,
):
    """One real run per failing check: comment, label swap, nothing else."""
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, fake_pi_thin(missing))
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)
    caplog.set_level("INFO")

    result = runner.process_issue(
        THIN_ISSUE, gate_config(clone, tmp_path), REPO,
    )

    assert result == runner.IssueResult("needs-detail", None)
    # ONE comment, naming the missing pieces and the repair action.
    assert len(comments) == 1
    body = comments[0]
    # Issue #1336: the claim payload's author is mentioned so GitHub
    # notifies the person on the stopped ticket.
    assert "@alice" in body.splitlines()[0]
    assert f"<!-- orbi:run={RUN_ID} -->" in body
    assert f"run_id={RUN_ID}" in body
    for piece in missing:
        assert clarify.MISSING[piece] in body
    assert delivery_labels.NEEDS_DETAIL_LABEL in body
    # The labels are swapped: ai-ready off, ai-needs-detail on.
    assert labels[ISSUE_NUMBER] == [delivery_labels.NEEDS_DETAIL_LABEL]
    assert "clarify_needs_detail" in caplog.text
    # No worktree, branch, Pi session or PR for a stopped ticket.
    assert not worktree_for(clone, RUN_ID).exists()
    assert not (clone / ".worktrees").exists()
    assert not (clone / ".pi-session").exists()
    assert git(clone, "branch", "--list", "orbi/*").strip() == ""
    assert not list(clone.glob("**/impl.py"))


def test_ticket_the_gate_accepts_is_delivered(clone, tmp_path, monkeypatch):
    """The gate is a gate, not a wall: a passing verdict delivers.

    The ticket here already carries `ai-needs-detail` from an earlier
    thin-ticket stop (the author edited the body and re-added
    `ai-ready`); the claim must clear the stale stop label (Issue #1379).
    """
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_PASSING_GATE)
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [
        delivery_labels.READY_LABEL, delivery_labels.NEEDS_DETAIL_LABEL,
    ]}
    install_fake_gh(monkeypatch, comments, labels)
    repaired_issue = dict(THIN_ISSUE, labels=[
        {"name": delivery_labels.READY_LABEL},
        {"name": delivery_labels.NEEDS_DETAIL_LABEL},
    ])

    result = runner.process_issue(
        repaired_issue, gate_config(clone, tmp_path), REPO,
    )

    assert result.url == PR_URL
    assert worktree_for(clone, RUN_ID).is_dir()
    assert delivery_labels.PR_OPENED_LABEL in labels[ISSUE_NUMBER]
    # The stale gate label is gone once the ticket is claimed.
    assert delivery_labels.NEEDS_DETAIL_LABEL not in labels[ISSUE_NUMBER]
    assert all("not ready to deliver" not in body for body in comments)
    assert len(comments) == 4
    assert re.search(r"<!-- orbi:run=[0-9a-f]{8} -->", comments[0])


def test_model_error_lets_the_ticket_through(
    clone, tmp_path, monkeypatch, caplog,
):
    """A failed judgment is a bypass: the delivery proceeds (Issue #1088)."""
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_MODEL_ERROR)
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)
    caplog.set_level("INFO")

    result = runner.process_issue(
        THIN_ISSUE, gate_config(clone, tmp_path), REPO,
    )

    assert result.url == PR_URL
    assert delivery_labels.NEEDS_DETAIL_LABEL not in labels[ISSUE_NUMBER]
    assert "clarify_check_skipped" in caplog.text
