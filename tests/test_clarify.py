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

from tests.fakes.clarify import (
    FakeGate, fix_run_id, record_label_notifications, stop_comment_entry,
    stub_stop_history,
)

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


# --- Issue #1831: the six ticket texts the judgment is replayed on ---------

@dataclasses.dataclass(frozen=True)
class ClarifyCase:
    """One real ticket text and the judgment Issue #1831 expects for it.

    The gate is a model judgment, so a stub cannot prove the semantics and
    the real-prompt replay stays with the maintainer (Issue #1831 assigns
    it there). `expectation` is the behavior that replay must show and
    `expected_missing` is the same answer as check identifiers — an
    expectation, never a measured one: no model runs in this test. The test
    below pins what the sandbox can prove — the author's text reaches the
    judgment untouched (no rewrite, no splitting) and the expected answer
    drives the documented stop/proceed path.
    """
    name: str
    title: str
    body: str
    expectation: str
    expected_missing: tuple[str, ...]


CLARIFY_CASES = (
    ClarifyCase(
        name="k8e_668_runtime_failures",
        title="warm pool churn and flaky regression rounds",
        body=(
            "- warm pool 抖动：约每 60s 一次 trim surplus warm pod "
            "(pool above target)——补一个再裁一个。来自上游 b68aef26，"
            "我的 PR 未触及。它是那些 PVC 绑定冲突"
            "（VolumeBinding: the object has been modified）的来源。\n"
            "- 连续多轮回归偶发失败：节点残留上一轮 session pod/PVC 时，"
            "下一轮会出 1~2 个 SandboxUnreachableException。清理残留后即恢复 "
            "44/44，L6 隔离跑 3/3 通过——属资源竞争，非功能缺陷。"
        ),
        # Observable runtime behavior is NOT absent here; acceptance or
        # scope clarification may still be requested.
        expectation="reports no missing observable_result",
        expected_missing=("acceptance_condition", "single_outcome"),
    ),
    ClarifyCase(
        name="runtime_failure_with_implied_correction",
        title="GET /health returns 500",
        body=(
            "GET /health returns 500; it should return 200, verified by a "
            "regression test."
        ),
        expectation="proceeds without requiring template headings",
        expected_missing=(),
    ),
    ClarifyCase(
        name="one_behavior_in_two_bullets",
        title="Login session lost after submit",
        body=(
            "- The login page reloads after submitting valid credentials.\n"
            "- Keep the user signed in after that submit; verify with a "
            "browser regression test."
        ),
        expectation="proceeds: both bullets specify one behavior",
        expected_missing=(),
    ),
    ClarifyCase(
        name="no_delivery_requirement",
        title="Thin ticket",
        body="Make it better.",
        expectation="requests the missing details",
        expected_missing=("observable_result", "acceptance_condition"),
    ),
    ClarifyCase(
        name="two_unrelated_deliveries",
        title="Password reset and CSV export",
        body=(
            "Add password reset and CSV export; verify both with regression "
            "tests."
        ),
        # Both deliverables name a user-visible result and "verify both
        # with regression tests" is already the acceptance condition, so
        # the bundled deliverables are the only missing piece.
        expectation="requests a single delivery outcome",
        expected_missing=("single_outcome",),
    ),
    ClarifyCase(
        name="historical_count_is_not_a_condition",
        title="Improve performance",
        body="The benchmark passed 44/44 yesterday; improve performance.",
        expectation="requests a completion condition, not the past count",
        expected_missing=("observable_result", "acceptance_condition"),
    ),
)

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
prompt_arg = args[args.index("--system-prompt") + 1]
# Issue #1634: --system-prompt carries a FILE path; Pi reads the
prompt = (open(prompt_arg, encoding="utf-8").read()
          if os.path.exists(prompt_arg) else prompt_arg)
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
prompt_arg = args[args.index("--system-prompt") + 1]
# Issue #1634: --system-prompt carries a FILE path; Pi reads the
prompt = (open(prompt_arg, encoding="utf-8").read()
          if os.path.exists(prompt_arg) else prompt_arg)
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
def _reset_run_id(bind_run_id):
    """Each test starts without a bound run id."""
    bind_run_id(None)


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
    stub_stop_history(monkeypatch, [])
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
    assert clarify.missing_text("observable_result", language_code="en") in body
    assert clarify.missing_text("single_outcome", language_code="en") in body
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
    stub_stop_history(monkeypatch, [])
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


# --- Issue #1904: one stop per unchanged ticket text ----------------------

def _stop_comment(issue: dict, run_id: str = "deadbeef") -> str:
    """The stop comment a previous claim left on the Issue."""
    return (
        f"<!-- orbi:run={run_id} -->\n"
        f"{clarify.stop_marker(issue)}\n"
        "Orbi: this ticket is not ready to deliver."
    )


def test_render_comment_carries_the_judged_text_digest():
    """The stop comment records WHICH text it judged (Issue #1904)."""
    body = clarify.render_comment(
        ISSUE,
        clarify.ClarifyVerdict(
            passed=False, missing=("single_outcome",),
        ),
        RUN_ID, language_code="en",
    )
    assert clarify.stop_marker(ISSUE) in body
    # The run marker stays first: the progress contract keys on it.
    assert body.index(f"<!-- orbi:run={RUN_ID} -->") < body.index(
        clarify.stop_marker(ISSUE)
    )


def test_enforce_does_not_rejudge_an_unchanged_stopped_ticket(
    monkeypatch, caplog,
):
    """Acceptance: no model call, no comment, no notification."""
    gate = FakeGate(
        monkeypatch, history=[stop_comment_entry(_stop_comment(ISSUE))],
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert gate.judged == []
    assert gate.comments == []
    assert gate.notified == []
    # The stop labels are re-applied so the ticket leaves the claim queue.
    assert gate.edits == [(
        ISSUE["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, delivery_labels.READY_LABEL,
    )]
    assert "clarify_already_stopped" in caplog.text
    assert "clarify_needs_detail" not in caplog.text


def test_enforce_judges_again_after_the_body_changes(monkeypatch):
    """Acceptance: an edited body is judged again and gets its new stop."""
    stopped = dict(ISSUE, body="Make it better somehow.")
    verdict = clarify.ClarifyVerdict(
        passed=False, missing=("observable_result",),
    )
    gate = FakeGate(
        monkeypatch, history=[stop_comment_entry(_stop_comment(stopped))],
        verdict=verdict,
    )

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert len(gate.judged) == 1
    assert len(gate.comments) == 1
    assert clarify.stop_marker(ISSUE) in gate.comments[0][2]
    assert gate.edits == [(
        ISSUE["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, delivery_labels.READY_LABEL,
    )]


def test_enforce_judges_a_ticket_never_stopped(monkeypatch):
    """Acceptance: a ticket without a stop comment behaves as today."""
    gate = FakeGate(
        monkeypatch,
        # Malformed history entries are data, never a crash.
        history=[None, {}, {"body": None}, {"body": "a human comment"}],
        verdict=clarify.ClarifyVerdict(passed=True, missing=()),
    )

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is True
    assert len(gate.judged) == 1
    assert (gate.comments, gate.edits) == ([], [])


def test_enforce_uses_the_newest_stop_comment(monkeypatch):
    """A later stop of an edited body is the one the next claim compares."""
    older = dict(ISSUE, body="first text")
    gate = FakeGate(
        monkeypatch,
        history=[
            stop_comment_entry(_stop_comment(older, "aaaaaaaa")),
            stop_comment_entry(_stop_comment(ISSUE, "bbbbbbbb")),
        ],
    )

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert gate.judged == []


def test_enforce_still_stops_when_reapplying_the_labels_fails(
    monkeypatch, caplog,
):
    """A failed label write never turns an unchanged stop into delivery."""
    gate = FakeGate(
        monkeypatch, history=[stop_comment_entry(_stop_comment(ISSUE))],
        edit_error=RuntimeError("gh write failed"),
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert gate.judged == []
    assert gate.comments == []
    assert "clarify_already_stopped" in caplog.text
    assert "reason=RuntimeError" in caplog.text


def test_enforce_fails_open_when_the_stop_history_is_unreadable(
    monkeypatch, caplog,
):
    """A broken history read keeps today's fail-open behavior."""
    gate = FakeGate(
        monkeypatch, history_error=RuntimeError("gh read failed"),
        verdict=clarify.ClarifyVerdict(passed=True, missing=()),
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is True
    assert len(gate.judged) == 1
    assert "clarify_check_skipped" in caplog.text
    assert "reason=RuntimeError" in caplog.text


def test_enforce_ignores_a_stop_marker_from_another_author(monkeypatch, caplog):
    """The hidden marker only counts on the runner's OWN comment.

    docs/public-contracts.mdx: a hidden marker "does not authenticate an
    author, so a consumer keeps its own trusted-author rules". The digest
    is computed from public ticket text, so any commenter could forge one;
    honouring it would let a stranger stop a ticket silently — no stop
    comment, no notification.
    """
    gate = FakeGate(
        monkeypatch,
        history=[
            stop_comment_entry(_stop_comment(ISSUE), login="alice"),
            # A marker on a comment that names no author is no better.
            {"body": _stop_comment(ISSUE)},
        ],
        verdict=clarify.ClarifyVerdict(
            passed=False, missing=("single_outcome",),
        ),
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    # Judged as today, and the real stop comment (with its own marker,
    # naming the missing piece) lands on the Issue.
    assert len(gate.judged) == 1
    assert len(gate.comments) == 1
    assert clarify.stop_marker(ISSUE) in gate.comments[0][2]
    assert "clarify_stop_untrusted" in caplog.text
    assert "clarify_needs_detail" in caplog.text


def test_enforce_fails_open_when_the_runner_identity_is_unreadable(
    monkeypatch, caplog,
):
    """An unreadable own-account read keeps today's judgment (fail open)."""
    gate = FakeGate(
        monkeypatch,
        history=[stop_comment_entry(_stop_comment(ISSUE))],
        login_error=ValueError("gh auth status failed"),
        verdict=clarify.ClarifyVerdict(passed=True, missing=()),
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is True
    assert len(gate.judged) == 1
    assert "clarify_check_skipped" in caplog.text
    assert "reason=ValueError" in caplog.text


# --- the Issue #1831 ticket texts -----------------------------------------

@pytest.mark.parametrize(
    "case", CLARIFY_CASES, ids=[case.name for case in CLARIFY_CASES],
)
def test_expected_ticket_outcome_takes_the_documented_path(case, monkeypatch):
    """Each Issue #1831 text keeps its expected outcome end to end.

    The judgment itself is the model's and Issue #1831 replays it with the
    maintainer; here the expected answer proves the gate carries the
    author's text into the judgment unchanged (no rewrite, no splitting)
    and that this answer takes the documented path: a passing one delivers
    without a write, a failing one stops with ONE comment naming exactly
    the pieces the judgment named.
    """
    issue = {"number": 1831, "title": case.title, "body": case.body}
    context = clarify.build_context(issue)
    assert context.startswith(f"Issue #{issue['number']}: {case.title}")
    assert case.body in context

    verdict = clarify.ClarifyVerdict(
        passed=not case.expected_missing, missing=case.expected_missing,
    )
    stub_stop_history(monkeypatch, [])
    monkeypatch.setattr(seam, "judge_issue_body", lambda *a, **k: verdict)
    comments, edits = _record_writes(monkeypatch)

    proceed = clarify.enforce(
        issue, config_domain.RunnerConfig(), REPO, RUN_ID,
    )

    assert proceed is (case.expected_missing == ())
    if not case.expected_missing:
        assert (comments, edits) == ([], [])
        return
    assert len(comments) == 1
    body = comments[0][2]
    for piece in case.expected_missing:
        assert clarify.missing_text(piece, language_code="en") in body
    assert edits == [(
        issue["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, delivery_labels.READY_LABEL,
    )]


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
        assert clarify.missing_text(piece, language_code="en") in body
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


# --- Issue #1904: the real runner does not repeat an unchanged stop -------

def _pi_that_must_not_run(marker: Path) -> str:
    """A fake Pi that records any invocation and fails the run."""
    return (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('invoked')\n"
        "sys.exit(1)\n"
    )


def test_unchanged_ticket_re_labelled_ready_is_not_judged_again(
    clone, tmp_path, monkeypatch, caplog,
):
    """Acceptance #1/#2: one stop per text, the queue keeps moving.

    The repository automation re-adds `ai-ready` without editing the
    body. The next claim must make no model call, post no comment and
    send no notification, and must re-apply the stop labels so the
    ticket leaves the `label:ai-ready` scan and never starves the other
    ready tickets.
    """
    fix_run_id(monkeypatch, RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, fake_pi_thin(["single_outcome"]))
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)
    notified = record_label_notifications(monkeypatch)
    config = gate_config(clone, tmp_path)
    caplog.set_level("INFO")

    assert runner.process_issue(THIN_ISSUE, config, REPO) == (
        runner.IssueResult("needs-detail", None)
    )
    assert len(comments) == 1
    assert clarify.stop_marker(THIN_ISSUE) in comments[0]
    assert len(notified) == 1

    # The repository automation re-adds the claim label, body untouched.
    labels[ISSUE_NUMBER].append(delivery_labels.READY_LABEL)
    calls = tmp_path / "pi-invoked"
    install_fake_pi(monkeypatch, tmp_path, _pi_that_must_not_run(calls))
    caplog.clear()

    assert runner.process_issue(THIN_ISSUE, config, REPO) == (
        runner.IssueResult("needs-detail", None)
    )
    # NO model call, NO second comment, NO second notification.
    assert not calls.exists()
    assert len(comments) == 1
    assert len(notified) == 1
    # The ticket is stopped again, so the ai-ready scan no longer sees it
    # and the other ready tickets stay claimable.
    assert labels[ISSUE_NUMBER] == [delivery_labels.NEEDS_DETAIL_LABEL]
    assert not delivery_labels.is_pickup_eligible(labels[ISSUE_NUMBER])
    assert "clarify_already_stopped" in caplog.text
    assert "clarify_needs_detail" not in caplog.text
    assert not worktree_for(clone, RUN_ID).exists()


def test_edited_failing_ticket_is_judged_again_and_gets_a_new_comment(
    clone, tmp_path, monkeypatch,
):
    """Acceptance #3 (failing path): an edit re-runs the judgment."""
    fix_run_id(monkeypatch, RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, fake_pi_thin(["single_outcome"]))
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)
    config = gate_config(clone, tmp_path)

    assert runner.process_issue(THIN_ISSUE, config, REPO) == (
        runner.IssueResult("needs-detail", None)
    )
    assert len(comments) == 1

    edited = dict(
        THIN_ISSUE,
        body=THIN_ISSUE["body"] + "\n\nAlso add CSV export.",
        labels=[
            {"name": delivery_labels.READY_LABEL},
            {"name": delivery_labels.NEEDS_DETAIL_LABEL},
        ],
    )
    labels[ISSUE_NUMBER].append(delivery_labels.READY_LABEL)

    assert runner.process_issue(edited, config, REPO) == (
        runner.IssueResult("needs-detail", None)
    )
    assert len(comments) == 2
    assert clarify.stop_marker(edited) in comments[1]
    assert comments[0] != comments[1]
    assert labels[ISSUE_NUMBER] == [delivery_labels.NEEDS_DETAIL_LABEL]
    assert not worktree_for(clone, RUN_ID).exists()


def test_edited_passing_ticket_is_judged_again_and_delivered(
    clone, tmp_path, monkeypatch,
):
    """Acceptance #3 (passing path): the repaired body is delivered."""
    fix_run_id(monkeypatch, RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, fake_pi_thin(["single_outcome"]))
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)
    config = gate_config(clone, tmp_path)

    assert runner.process_issue(THIN_ISSUE, config, REPO) == (
        runner.IssueResult("needs-detail", None)
    )
    assert len(comments) == 1

    edited = dict(
        THIN_ISSUE,
        body=(
            "GET /health returns 500; it should return 200, verified by a "
            "regression test."
        ),
        labels=[
            {"name": delivery_labels.READY_LABEL},
            {"name": delivery_labels.NEEDS_DETAIL_LABEL},
        ],
    )
    labels[ISSUE_NUMBER].append(delivery_labels.READY_LABEL)
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_PASSING_GATE)

    result = runner.process_issue(edited, config, REPO)

    assert result.url == PR_URL
    assert worktree_for(clone, RUN_ID).is_dir()
    assert delivery_labels.NEEDS_DETAIL_LABEL not in labels[ISSUE_NUMBER]
    assert len(comments) > 1
