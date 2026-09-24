"""Thin-ticket clarification gate (Issue #1088).

The gate is a model judgment, so the unit tests stub the seam
(`seam.run_clarify_agent`, `seam.judge_issue_body`, `seam.comment_issue`,
`seam.edit_issue`, `seam.prepare_pi_agent_dir`, `seam.stream_pi`) and the
end-to-end tests run the REAL runner against a real local git clone with a
fake `pi` and a stateful fake `gh`: one per failing check proves a thin
ticket stops with one comment and a label swap (and no worktree, branch,
session or PR), one proves a ticket the gate accepts is delivered normally,
and one proves a failed judgment is a bypass.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import git

from orbi import clarify, config as config_domain, pi_session
from orbi import delivery_labels

import orbi.runner as runner
from seam import seam

from tests.test_run_id_e2e import (
    FAKE_PI,
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

# A thin `ai-ready` ticket as the claim scan sees it.
THIN_ISSUE = {
    "number": ISSUE_NUMBER,
    "title": "Thin ticket",
    "body": "Make the thing better somehow.",
    "labels": [{"name": delivery_labels.READY_LABEL}],
}

# The clarify session is the `pi --no-tools` call; the delivery session is
# the ordinary one. The two-faced fake answers both: a passing verdict for
# the gate, a real commit for the implementer.
FAKE_PI_PASSING_GATE = """#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
if "--no-tools" in args:
    sys.stdout.write('{"satisfied": true, "missing": []}')
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
# per missing set so each of the three checks is exercised by a real run.
def fake_pi_thin(missing: list[str]) -> str:
    verdict = json.dumps({"satisfied": False, "missing": missing})
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

def test_parse_verdict_reads_a_passing_answer():
    assert clarify.parse_verdict('{"satisfied": true, "missing": []}') == (
        clarify.ClarifyVerdict(passed=True, missing=())
    )


def test_parse_verdict_reads_the_missing_pieces():
    assert clarify.parse_verdict(
        '{"satisfied": false, "missing": ["single_outcome", "observable_result"]}'
    ) == clarify.ClarifyVerdict(
        passed=False, missing=("single_outcome", "observable_result"),
    )


def test_parse_verdict_reads_a_verdict_embedded_in_prose():
    assert clarify.parse_verdict(
        'My verdict follows: {"satisfied": true, "missing": []} — done.'
    ) == clarify.ClarifyVerdict(passed=True, missing=())


def test_parse_verdict_rejects_output_without_a_json_object():
    assert clarify.parse_verdict("I could not decide.") is None


def test_parse_verdict_rejects_broken_json():
    assert clarify.parse_verdict('{"satisfied": true,') is None


def test_parse_verdict_rejects_malformed_json_inside_an_object():
    # Braces but not valid JSON: the parse itself fails (the fail-open path).
    assert clarify.parse_verdict('{"satisfied": true "missing": []}') is None


@pytest.mark.parametrize("output", [
    '{"satisfied": "yes", "missing": []}',
    '{"satisfied": true, "missing": "none"}',
    '{"satisfied": true}',
    '{"missing": []}',
])
def test_parse_verdict_rejects_wrong_value_types(output):
    assert clarify.parse_verdict(output) is None


@pytest.mark.parametrize("output", [
    '{"satisfied": false, "missing": ["whatever"]}',
    '{"satisfied": false, "missing": [7]}',
])
def test_parse_verdict_rejects_an_unknown_missing_piece(output):
    assert clarify.parse_verdict(output) is None


@pytest.mark.parametrize("output", [
    '{"satisfied": true, "missing": ["observable_result"]}',
    '{"satisfied": false, "missing": []}',
])
def test_parse_verdict_rejects_an_inconsistent_answer(output):
    assert clarify.parse_verdict(output) is None


# --- render_comment --------------------------------------------------------

def test_render_comment_names_every_missing_piece_and_the_repair():
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result", "acceptance_condition"),
    )
    body = clarify.render_comment(ISSUE, verdict, RUN_ID)
    assert body.startswith(f"<!-- orbi:run={RUN_ID} -->")
    assert clarify.MISSING["observable_result"] in body
    assert clarify.MISSING["acceptance_condition"] in body
    assert clarify.MISSING["single_outcome"] not in body
    assert delivery_labels.READY_LABEL in body
    assert f"run_id={RUN_ID}" in body
    # Issue #1088: the comment shows the shape to fill in.
    assert "Outcome:" in body
    assert "Acceptance:" in body


@pytest.mark.parametrize("missing_piece", [
    "observable_result", "acceptance_condition", "single_outcome",
])
def test_render_comment_covers_each_of_the_three_checks(missing_piece):
    """Issue #1088: every check has a human-readable gap sentence."""
    verdict = clarify.ClarifyVerdict(passed=False, missing=(missing_piece,))
    body = clarify.render_comment(ISSUE, verdict, RUN_ID)
    assert clarify.MISSING[missing_piece] in body
    for other, text in clarify.MISSING.items():
        if other != missing_piece:
            assert text not in body


# --- judge_issue_body ------------------------------------------------------

def test_judge_issue_body_asks_the_model_and_parses_the_answer(monkeypatch):
    calls = []

    def fake_agent(issue, config, source_repo, run_id, **kwargs):
        calls.append((issue, config, source_repo, run_id, kwargs))
        return '{"satisfied": false, "missing": ["observable_result"]}'

    monkeypatch.setattr(seam, "run_clarify_agent", fake_agent)
    config = config_domain.RunnerConfig()
    verdict = clarify.judge_issue_body(ISSUE, config, REPO, RUN_ID)
    assert verdict == clarify.ClarifyVerdict(
        passed=False, missing=("observable_result",),
    )
    issue, got_config, repo, run_id, kwargs = calls[0]
    assert (issue, got_config, repo, run_id) == (ISSUE, config, REPO, RUN_ID)
    assert kwargs["system_prompt"] == clarify.CLARIFY_SYSTEM_PROMPT
    assert ISSUE["body"] in kwargs["context"]


def test_judge_issue_body_fails_open_when_the_agent_raises(
    monkeypatch, caplog,
):
    def boom(*args, **kwargs):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(seam, "run_clarify_agent", boom)
    caplog.set_level("INFO")
    assert clarify.judge_issue_body(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is None
    assert "clarify_check_skipped" in caplog.text
    assert "reason=RuntimeError" in caplog.text


def test_judge_issue_body_fails_open_on_a_malformed_verdict(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        seam, "run_clarify_agent", lambda *a, **k: "not a verdict",
    )
    caplog.set_level("INFO")
    assert clarify.judge_issue_body(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is None
    assert "clarify_check_skipped" in caplog.text
    assert "reason=no_verdict" in caplog.text


# --- enforce ---------------------------------------------------------------

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


def test_enforce_posts_one_comment_and_swaps_the_labels(monkeypatch, caplog):
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result", "single_outcome"),
    )
    monkeypatch.setattr(seam, "judge_issue_body", lambda *a, **k: verdict)
    comments, edits = _record_writes(monkeypatch)
    caplog.set_level("INFO")

    assert clarify.enforce(ISSUE, config_domain.RunnerConfig(), REPO,
                           RUN_ID) is False
    assert len(comments) == 1
    number, repo, body = comments[0]
    assert (number, repo) == (ISSUE["number"], REPO)
    assert body.startswith(f"<!-- orbi:run={RUN_ID} -->")
    assert f"run_id={RUN_ID}" in body
    assert edits == [(
        ISSUE["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, delivery_labels.READY_LABEL,
    )]
    assert "clarify_needs_detail" in caplog.text
    assert "clarify_check_skipped" not in caplog.text


def test_enforce_lets_a_satisfied_verdict_through(monkeypatch):
    monkeypatch.setattr(
        seam, "judge_issue_body",
        lambda *a, **k: clarify.ClarifyVerdict(passed=True, missing=()),
    )
    comments, edits = _record_writes(monkeypatch)
    assert clarify.enforce(ISSUE, config_domain.RunnerConfig(), REPO,
                           RUN_ID) is True
    assert comments == [] and edits == []


def test_enforce_lets_a_ticket_through_when_the_judge_cannot_decide(
    monkeypatch,
):
    monkeypatch.setattr(seam, "judge_issue_body", lambda *a, **k: None)
    comments, edits = _record_writes(monkeypatch)
    assert clarify.enforce(ISSUE, config_domain.RunnerConfig(), REPO,
                           RUN_ID) is True
    assert comments == [] and edits == []


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


# --- pi_session.run_clarify_agent ------------------------------------------

def test_run_clarify_agent_materializes_the_provider_dir(monkeypatch, tmp_path):
    agent_dir = tmp_path / "agent"
    monkeypatch.setattr(
        seam, "prepare_pi_agent_dir",
        lambda worktree, config, role=None: agent_dir,
    )
    calls: list = []

    def fake_stream(command, **kwargs):
        calls.append((command, kwargs))
        return '{"satisfied": true, "missing": []}'

    monkeypatch.setattr(seam, "stream_pi", fake_stream)
    output = pi_session.run_clarify_agent(
        ISSUE, config_domain.RunnerConfig(pi_providers_data={"providers": {}}),
        REPO, RUN_ID, system_prompt="PROMPT", context="CONTEXT",
    )
    assert output == '{"satisfied": true, "missing": []}'
    command, kwargs = calls[0]
    assert "--no-tools" in command
    assert "PROMPT" in command and "CONTEXT" in command
    assert kwargs["pi_env"]["PI_CODING_AGENT_DIR"] == str(agent_dir)
    assert kwargs["role"] == pi_session.ROLE_TICKET
    assert kwargs["ctx"].run_id == RUN_ID
    session_dir = Path(command[command.index("--session-dir") + 1])
    assert session_dir.name == ".pi-session"
    assert kwargs["cwd"] == session_dir.parent
    # Transient OS state: the session dir is gone after the call, so a
    # stopped ticket leaves no session behind under the repository.
    assert not session_dir.parent.exists()


def test_run_clarify_agent_keeps_pis_own_agent_dir_without_a_provider_file(
    monkeypatch,
):
    monkeypatch.setattr(
        seam, "prepare_pi_agent_dir", lambda *a, **k: None,
    )
    calls: list = []
    monkeypatch.setattr(
        seam, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "{}",
    )
    pi_session.run_clarify_agent(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
        system_prompt="PROMPT", context="CONTEXT",
    )
    assert calls[0][1].get("pi_env") is None


def test_needs_detail_is_a_scheduling_marker_not_a_delivery_state():
    assert delivery_labels.NEEDS_DETAIL_LABEL == "ai-needs-detail"
    assert (
        delivery_labels.NEEDS_DETAIL_LABEL
        not in delivery_labels.LIFECYCLE_STATES
    )


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


def test_ticket_the_gate_accepts_is_delivered(clone, tmp_path, monkeypatch):
    """The gate is a gate, not a wall: a passing verdict delivers."""
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_PASSING_GATE)
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)

    result = runner.process_issue(
        THIN_ISSUE, gate_config(clone, tmp_path), REPO,
    )

    assert result.url == PR_URL
    assert worktree_for(clone, RUN_ID).is_dir()
    assert delivery_labels.PR_OPENED_LABEL in labels[ISSUE_NUMBER]
    assert delivery_labels.NEEDS_DETAIL_LABEL not in labels[ISSUE_NUMBER]
    assert all("not ready to deliver" not in body for body in comments)
    assert len(comments) == 4
    assert re.search(r"<!-- orbi:run=[0-9a-f]{8} -->", comments[0])
