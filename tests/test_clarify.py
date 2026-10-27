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
from orbi import delivery_labels, failure, failure_report, language, progress

import orbi.runner as runner
from seam import seam

from tests.fakes.clarify import (
    FakeGate, fix_run_id, record_label_notifications, stop_comment_entry,
    stub_runner_login, stub_stop_history,
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

def _stop_comment(issue: dict, run_id: str = "deadbeef",
                 mention: str = "") -> str:
    """The stop comment a previous claim left on the Issue.

    `render_comment()` puts the optional `@author` mention line in front
    of the run marker (Issue #1336); the rest of the formal prefix is the
    run marker immediately followed by the stop marker.
    """
    prefix = f"{mention}\n" if mention else ""
    return (
        f"{prefix}"
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


@pytest.mark.parametrize("author", [None, {"login": "alice"}])
def test_the_gate_recognizes_the_stop_comment_it_wrote(monkeypatch, author):
    """The read accepts the writer's real prefix (Issue #1908).

    Round trip through `render_comment` and the renderer `comment_issue`
    posts through (`progress.format_status_comment`), never a hand-built
    fixture: the formal prefix the gate writes and the prefix it reads are
    one contract, so a change to the writer cannot silently disable the
    dedup (the repeated model call, comment and notification of #1904).
    `author=None` is the run without the optional mention line.
    """
    issue = dict(ISSUE, author=author)
    written = progress.format_status_comment(clarify.render_comment(
        issue,
        clarify.ClarifyVerdict(passed=False, missing=("single_outcome",)),
        RUN_ID, language_code="en",
    ))
    gate = FakeGate(monkeypatch, history=[stop_comment_entry(written)])

    assert clarify.enforce(
        issue, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert (gate.judged, gate.comments, gate.notified) == ([], [], [])


@pytest.mark.parametrize("mention", ["", "@alice"])
def test_enforce_does_not_rejudge_an_unchanged_stopped_ticket(
    monkeypatch, caplog, mention,
):
    """Acceptance: no model call, no comment, no notification.

    Both stop prefixes `render_comment()` emits are recognized: with the
    `@author` mention line and without it (Issue #1908).
    """
    gate = FakeGate(
        monkeypatch,
        history=[stop_comment_entry(_stop_comment(ISSUE, mention=mention))],
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


@pytest.mark.parametrize(
    ("field", "value"), [("body", "Make it better somehow."),
                         ("title", "A different thin ticket")],
)
def test_enforce_judges_again_after_the_text_changes(monkeypatch, field, value):
    """Acceptance: an edited title or body is judged again (#1904, #1908)."""
    stopped = dict(ISSUE, **{field: value})
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


# --- Issue #1908: only the formal stop prefix is stop state ---------------

def _failure_comment_quoting(quoted: str) -> str:
    """The runner's own failure comment with `quoted` in its raw output.

    The REAL reporter path (the Issue #1908 reproduction): a failed test
    prints the marker, `_failure_evidence` copies the bounded stdout into
    a code fence, and `failure_comment_body` renders the comment the
    runner posts — whose prefix is the run marker plus the `failure` kind,
    never a stop marker.
    """
    error = subprocess.CalledProcessError(1, ["pytest"], output=quoted)
    evidence = failure_report._failure_evidence(
        None, error, language_code="en",
    )
    return (
        "<!-- orbi:run=aabbccdd -->\n"
        "<!-- orbi:comment=failure -->\n"
        + failure.failure_comment_body(
            outcome="blocked", action="Repair the test", reason="Test failed",
            diagnosis="Test failed", scene="- run: `aabbccdd`",
            evidence=evidence, pr_url=None, issue="owner/project#7",
            run_id="aabbccdd", language_code="en", issue_number=7,
            issue_title=ISSUE["title"],
        )
    )


def _comment_quoting(variant: str) -> str:
    """One runner comment that mentions the stop marker without stopping."""
    marker = clarify.stop_marker(ISSUE)
    if variant == "failure_stdout":
        body = _failure_comment_quoting(marker)
    elif variant == "prose":
        body = f"<!-- orbi:run=deadbeef -->\nThe log printed {marker} once.\n"
    else:
        assert variant == "code_fence"
        body = f"<!-- orbi:run=deadbeef -->\n\n```\n{marker}\n```\n"
    # The fixture reproduces the defect: the marker IS in the body.
    assert marker in body
    return body


@pytest.mark.parametrize("variant", ["failure_stdout", "prose", "code_fence"])
def test_enforce_ignores_a_stop_marker_quoted_in_a_runner_comment(
    monkeypatch, caplog, variant,
):
    """Acceptance #1: quoted marker text is not stop state (Issue #1908).

    docs/public-contracts.mdx: official metadata is the contiguous prefix
    that begins with the run marker, so marker-shaped text quoted in prose,
    a raw log or a code fence is not metadata. A failure comment the runner
    posted earlier must not stop a ticket that was never stopped.
    """
    gate = FakeGate(
        monkeypatch, history=[stop_comment_entry(_comment_quoting(variant))],
        verdict=clarify.ClarifyVerdict(passed=True, missing=()),
    )
    caplog.set_level("INFO")

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is True
    assert len(gate.judged) == 1
    assert (gate.comments, gate.edits, gate.notified) == ([], [], [])
    assert "clarify_already_stopped" not in caplog.text


@pytest.mark.parametrize("variant", ["failure_stdout", "prose", "code_fence"])
def test_enforce_still_stops_when_a_quoted_marker_is_newer(monkeypatch,
                                                          variant):
    """Acceptance #3: a newer quote never overrides an older genuine stop."""
    gate = FakeGate(
        monkeypatch,
        history=[
            stop_comment_entry(_stop_comment(ISSUE)),
            # The runner's OWN newer comment that merely quotes it.
            stop_comment_entry(_comment_quoting(variant)),
        ],
    )

    assert clarify.enforce(
        ISSUE, config_domain.RunnerConfig(), REPO, RUN_ID,
    ) is False
    assert gate.judged == []
    assert gate.comments == []
    assert gate.edits == [(
        ISSUE["number"], REPO,
        delivery_labels.NEEDS_DETAIL_LABEL, delivery_labels.READY_LABEL,
    )]


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


SPLIT_ANSWER = {
    "missing": ["single_outcome"],
    "suggestion": {
        "kind": "split",
        "items": [
            {
                "result": "Users can tag a note",
                "acceptance": (
                    "after adding a tag, filtering by it finds the note; "
                    "the tag survives a reload"
                ),
            },
            {
                "result": "Users can export a note as Markdown",
                "acceptance": (
                    "Export downloads a .md file with title, body and tags"
                ),
                "after": 1,
            },
        ],
    },
}

CRITERIA_ANSWER = {
    "missing": ["acceptance_condition"],
    "suggestion": {
        "kind": "criteria",
        "items": [
            "Adding a tag makes the note findable by filtering",
            "The tag survives a reload",
        ],
    },
}


def fake_pi_answer(answer: dict) -> str:
    """The gate session's answer: a full verdict carrying a proposal."""
    return (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({json.dumps(answer)!r})\n"
    )


@pytest.mark.parametrize("language_code", ["en", "zh"])
@pytest.mark.parametrize("answer, kind", [
    (SPLIT_ANSWER, "split"),
    (CRITERIA_ANSWER, "criteria"),
])
def test_the_real_runner_posts_the_proposal_in_the_stop_comment(
    clone, tmp_path, monkeypatch, language_code, answer, kind,
):
    """The user path: the claim-time gate stops the ticket and the comment
    the author reads carries the rendered section, in their language."""
    fix_run_id(monkeypatch, RUN_ID)
    install_fake_pi(monkeypatch, tmp_path, fake_pi_answer(answer))
    comments: list[str] = []
    labels = {ISSUE_NUMBER: [delivery_labels.READY_LABEL]}
    install_fake_gh(monkeypatch, comments, labels)

    result = runner.process_issue(
        THIN_ISSUE,
        gate_config(clone, tmp_path, message_language=language_code),
        REPO,
    )

    assert result == runner.IssueResult("needs-detail", None)
    assert len(comments) == 1
    body = comments[0]
    # The rendered fixture IS the engine output a person may depend on.
    assert (FIXTURES / f"{kind}.{language_code}.md").read_text() in body
    assert language.text(
        language_code, "clarify.suggestion.edit",
    ) in body
    assert labels[ISSUE_NUMBER] == [delivery_labels.NEEDS_DETAIL_LABEL]
    assert not worktree_for(clone, RUN_ID).exists()


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


# --- Issue #1926: the proposal the stop comment may carry ------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "suggestion"

# The two proposals the engine renders. They are the fixtures every other
# consumer tests against, so the same values drive the renderer and the
# parser here.
SPLIT_PROPOSAL = clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=(
    clarify.SuggestionItem(
        text="Users can tag a note",
        acceptance=(
            "after adding a tag, filtering by it finds the note; the tag "
            "survives a reload"
        ),
    ),
    clarify.SuggestionItem(
        text="Users can export a note as Markdown",
        acceptance="Export downloads a .md file with title, body and tags",
        after=1,
    ),
))

CRITERIA_PROPOSAL = clarify.Suggestion(
    kind=clarify.SUGGESTION_CRITERIA,
    items=(
        clarify.SuggestionItem(
            text="Adding a tag makes the note findable by filtering",
        ),
        clarify.SuggestionItem(text="The tag survives a reload"),
    ),
)

# kind -> (proposal, the one missing piece it answers)
PROPOSALS = {
    clarify.SUGGESTION_SPLIT: (SPLIT_PROPOSAL, "single_outcome"),
    clarify.SUGGESTION_CRITERIA: (
        CRITERIA_PROPOSAL, "acceptance_condition",
    ),
}

LANGUAGES = ("en", "zh")


def _failing(missing, suggestion=None):
    return clarify.ClarifyVerdict(
        passed=False, missing=missing, suggestion=suggestion,
    )


def _section(lines, kind=clarify.SUGGESTION_SPLIT):
    """One proposal section around the given item lines."""
    return "\n".join([
        f"<!-- orbi:suggestion:v1 kind={kind} -->",
        *lines,
        "<!-- /orbi/suggestion -->",
    ])


@pytest.mark.parametrize("language_code", LANGUAGES)
@pytest.mark.parametrize("kind", sorted(PROPOSALS))
def test_the_rendered_proposal_matches_its_fixture(kind, language_code):
    """Acceptance 1: both kinds render in en and zh, markers included."""
    proposal, missing = PROPOSALS[kind]
    block = clarify.verdict_suggestion(
        _failing((missing,), proposal), language_code,
    )
    fixture = FIXTURES / f"{kind}.{language_code}.md"
    assert block + "\n" == fixture.read_text(encoding="utf-8")
    assert block.startswith(f"<!-- orbi:suggestion:v1 kind={kind} -->\n")
    assert block.rstrip().endswith(
        language.text(language_code, "clarify.suggestion.edit")
    )


@pytest.mark.parametrize("language_code", LANGUAGES)
@pytest.mark.parametrize("kind", sorted(PROPOSALS))
def test_the_stop_comment_carries_the_proposal_and_the_edit_sentence(
    kind, language_code,
):
    """Acceptance 1: the section lands in the stop comment, in the ticket
    language, before the repair instruction."""
    proposal, missing = PROPOSALS[kind]
    verdict = _failing((missing,), proposal)
    body = clarify.render_comment(
        ISSUE, verdict, RUN_ID, language_code=language_code,
    )
    block = clarify.verdict_suggestion(verdict, language_code)
    repair = language.text(
        language_code, "clarify.repair", ready_label="ai-ready",
    )
    assert block in body
    assert body.index(block) < body.index(repair)
    assert body.index(clarify.missing_text(missing, language_code)) < (
        body.index(block)
    )
    assert language.text(
        language_code, "clarify.suggestion.edit",
    ) in body


@pytest.mark.parametrize("suggestion, missing", [
    # The model declined, or could not propose: nothing is rendered.
    (None, ("single_outcome",)),
    (None, ("acceptance_condition",)),
    # Kind mismatch: each proposal answers one missing piece only.
    (SPLIT_PROPOSAL, ("acceptance_condition",)),
    (CRITERIA_PROPOSAL, ("single_outcome",)),
    # A second missing piece: the proposal would cover only part of it.
    (SPLIT_PROPOSAL, ("single_outcome", "observable_result")),
    (CRITERIA_PROPOSAL, ("acceptance_condition", "single_outcome")),
    (SPLIT_PROPOSAL, ("observable_result",)),
    # Unusable proposals.
    (
        clarify.Suggestion(
            kind=clarify.SUGGESTION_SPLIT,
            items=SPLIT_PROPOSAL.items[:1],
        ),
        ("single_outcome",),
    ),
    (
        clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(text="Users can tag a note", acceptance=""),
            clarify.SuggestionItem(
                text="Users can export a note", acceptance="a", after=1,
            ),
        )),
        ("single_outcome",),
    ),
    (
        clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(text="", acceptance="a"),
            clarify.SuggestionItem(
                text="Users can export a note", acceptance="b",
            ),
        )),
        ("single_outcome",),
    ),
    (
        clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(
                text="Users can tag a note", acceptance="a",
            ),
            clarify.SuggestionItem(
                text="Users can export a note", acceptance="b", after=2,
            ),
        )),
        ("single_outcome",),
    ),
    (
        clarify.Suggestion(kind=clarify.SUGGESTION_CRITERIA, items=()),
        ("acceptance_condition",),
    ),
])
def test_an_unusable_proposal_keeps_the_stop_comment_byte_for_byte(
    suggestion, missing,
):
    """Acceptance 2: no section, no edit sentence, identical bytes."""
    with_proposal = clarify.render_comment(
        ISSUE, _failing(missing, suggestion), RUN_ID, language_code="en",
    )
    without = clarify.render_comment(
        ISSUE, _failing(missing), RUN_ID, language_code="en",
    )
    assert with_proposal == without
    assert "orbi:suggestion" not in with_proposal
    assert "Editing this comment" not in with_proposal


def test_the_gate_prompt_guards_the_proposal():
    """The model may propose only from the ticket text, only for the two
    documented cases, and must decline when a fact or a decision is
    missing (Issue #1926)."""
    prompt = clarify.CLARIFY_SYSTEM_PROMPT
    for marker in (
        "ONLY single_outcome failed",
        "ONLY acceptance_condition failed",
        "adding and dropping nothing",
        "no count cap",
        "never invent a goal",
        "omit the key",
        "decision only a person can make",
    ):
        assert marker in prompt, marker


@pytest.mark.parametrize("answer", [
    None,
    "not a proposal",
    {},
    {"kind": "split"},
    {"kind": "plan", "items": ["Users can tag a note"]},
    {"kind": "split", "items": []},
    {"kind": "criteria", "items": []},
    {"kind": "criteria", "items": ["  "]},
    {"kind": "criteria", "items": [["nested"]]},
    # A single item is not a split of anything.
    {"kind": "split", "items": [{"result": "A", "acceptance": "a"}]},
    # A split item is an object, and it needs both lines.
    {"kind": "split", "items": ["A plain string", "Another one"]},
    {"kind": "split", "items": [{"result": "A"}, {"result": "B"}]},
    {"kind": "split", "items": [
        {"result": "A", "acceptance": "a"},
        {"result": "B", "acceptance": "b", "after": True},
    ]},
    # A multi-line value would break the one-line grammar.
    {"kind": "criteria", "items": ["first\nsecond"]},
    {"kind": "split", "items": [
        {"result": "A", "acceptance": "a"},
        {"result": "B", "acceptance": "b", "after": "1"},
    ]},
    # The dependency must name an earlier item.
    {"kind": "split", "items": [
        {"result": "A", "acceptance": "a"},
        {"result": "B", "acceptance": "b", "after": 2},
    ]},
])
def test_suggestion_answer_rejects_an_unusable_proposal(answer):
    assert clarify.suggestion_answer(answer) is None


def test_parse_verdict_reads_the_optional_proposal():
    verdict = clarify.parse_verdict(json.dumps({
        "missing": ["single_outcome"],
        "suggestion": {
            "kind": "split",
            "items": [
                {
                    "result": "Users can tag a note",
                    "acceptance": (
                        "after adding a tag, filtering by it finds the note; "
                        "the tag survives a reload"
                    ),
                },
                {
                    "result": "Users can export a note as Markdown",
                    "acceptance": (
                        "Export downloads a .md file with title, body and tags"
                    ),
                    "after": 1,
                },
            ],
        },
    }))
    assert verdict == clarify.ClarifyVerdict(
        passed=False, missing=("single_outcome",), suggestion=SPLIT_PROPOSAL,
    )


def test_parse_verdict_reads_a_criteria_proposal():
    verdict = clarify.parse_verdict(json.dumps({
        "missing": ["acceptance_condition"],
        "suggestion": {
            "kind": "criteria",
            "items": [
                "Adding a tag makes the note findable by filtering",
                "The tag survives a reload",
            ],
        },
    }))
    assert verdict == clarify.ClarifyVerdict(
        passed=False, missing=("acceptance_condition",),
        suggestion=CRITERIA_PROPOSAL,
    )


def test_a_broken_proposal_never_discards_the_verdict():
    """The verdict is what stops the ticket: an unusable proposal beside it
    only means today's comment."""
    assert clarify.parse_verdict(
        '{"missing": ["single_outcome"], "suggestion": {"kind": "split"}}'
    ) == clarify.ClarifyVerdict(passed=False, missing=("single_outcome",))
    assert clarify.parse_verdict(
        '{"missing": [], "suggestion": "nonsense"}'
    ) == clarify.ClarifyVerdict(passed=True, missing=())


# --- the parser: rendered, edited and invalid sections --------------------

@pytest.mark.parametrize("language_code", LANGUAGES)
@pytest.mark.parametrize("kind", sorted(PROPOSALS))
def test_the_parser_reads_the_rendered_fixture(kind, language_code):
    """Acceptance 3: the parser accepts what the engine renders, both
    languages and both kinds (the fixture keeps its edit sentence)."""
    proposal, _missing = PROPOSALS[kind]
    fixture = (FIXTURES / f"{kind}.{language_code}.md").read_text(
        encoding="utf-8",
    )
    assert clarify.parse_suggestion(fixture) == proposal


def test_the_parser_accepts_an_edited_section():
    """Acceptance 3: an edit — a slice removed, renamed, reordered — still
    parses, because the grammar reads numbered lines and earlier-only
    dependencies, never the rendered text."""
    removed = _section([
        "1. Users can tag a note",
        "   Acceptance: filtering by the tag finds the note",
        "2. Users can export a note as Markdown",
        "   Acceptance: Export downloads a .md file",
    ])
    assert clarify.parse_suggestion(removed) == clarify.Suggestion(
        kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(
                text="Users can tag a note",
                acceptance="filtering by the tag finds the note",
            ),
            clarify.SuggestionItem(
                text="Users can export a note as Markdown",
                acceptance="Export downloads a .md file",
            ),
        ),
    )

    renamed = _section([
        "1. Users can label a note",
        "   Acceptance: filtering by the label finds the note",
        "2. Export (after 1)",
        "   Acceptance: a .md file downloads",
    ])
    assert clarify.parse_suggestion(renamed) == clarify.Suggestion(
        kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(
                text="Users can label a note",
                acceptance="filtering by the label finds the note",
            ),
            clarify.SuggestionItem(
                text="Export", acceptance="a .md file downloads", after=1,
            ),
        ),
    )

    # Reordered lines with the dependency re-pointed at an earlier slice.
    reordered = _section([
        "1. Users can tag a note",
        "   Acceptance: filtering by the tag finds the note",
        "2. Export downloads a .md file",
        "   Acceptance: the file carries title, body and tags",
        "3. Filter a note by its tag (after 1)",
        "   Acceptance: the note is listed",
    ])
    assert clarify.parse_suggestion(reordered) == clarify.Suggestion(
        kind=clarify.SUGGESTION_SPLIT, items=(
            clarify.SuggestionItem(
                text="Users can tag a note",
                acceptance="filtering by the tag finds the note",
            ),
            clarify.SuggestionItem(
                text="Export downloads a .md file",
                acceptance="the file carries title, body and tags",
            ),
            clarify.SuggestionItem(
                text="Filter a note by its tag",
                acceptance="the note is listed",
                after=1,
            ),
        ),
    )


def _dependency_text(proposal):
    """The result text of the slice the LAST slice depends on.

    The dependency itself is what matters, never the number written next
    to it: a fix is free to renumber the items (Issue #1935). A slice
    without a dependency is a failed expectation here, not a case to map.
    """
    return proposal.items[proposal.items[-1].after - 1].text


@pytest.mark.parametrize("language_code", LANGUAGES)
@pytest.mark.parametrize(
    "numbers", [(5, 9), (9, 5)], ids=["gaps", "reordered"],
)
def test_a_renumbered_proposal_keeps_each_dependency_on_its_item(
    language_code, numbers,
):
    """Acceptance 1 (Issue #1935): the contract allows removing and
    reordering items with unique numbers, so neither gaps nor a descending
    order may cost the dependency its target. The check is the target's
    text after parsing, rendering and parsing again — the rendered numbers
    are the renderer's (1..N), not the numbers the person typed."""
    def line(key, **fields):
        return language.text(language_code, key, **fields)

    first, second = numbers
    body = _section([
        line("clarify.suggestion.item", number=first,
             result="Users can tag a note"),
        line(
            "clarify.suggestion.acceptance", text="the tag survives a reload",
        ),
        " ".join((
            line("clarify.suggestion.item", number=second,
                 result="Users can export tags"),
            line("clarify.suggestion.after", number=first),
        )),
        line("clarify.suggestion.acceptance", text="the export carries tags"),
    ])
    expected = clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=(
        clarify.SuggestionItem(
            text="Users can tag a note",
            acceptance="the tag survives a reload",
        ),
        clarify.SuggestionItem(
            text="Users can export tags",
            acceptance="the export carries tags",
            after=1,
        ),
    ))

    proposal = clarify.parse_suggestion(body)
    assert proposal == expected
    assert _dependency_text(proposal) == "Users can tag a note"

    reparsed = clarify.parse_suggestion(
        clarify.render_suggestion(proposal, language_code),
    )
    assert reparsed == expected
    assert _dependency_text(reparsed) == "Users can tag a note"


@pytest.mark.parametrize("name, body, expected_line", [
    (
        "a dependency on an item written later across a gap",
        _section([
            "5. Users can tag a note (after 9)",
            "   Acceptance: filtering by the tag finds the note",
            "9. Users can export a note",
            "   Acceptance: a .md file downloads",
        ]),
        2,
    ),
    (
        "duplicate numbers across a gap",
        _section([
            "5. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "5. Users can export a note",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
    (
        "a dependency on a missing item across a gap",
        _section([
            "5. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "9. Users can export a note (after 7)",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
], ids=["later target", "duplicate numbers", "missing target"])
def test_a_numbering_gap_never_weakens_the_rejection(
    name, body, expected_line,
):
    """Acceptance 2 (Issue #1935): resolving the written number must not
    make a gap or a descending order accept a duplicate, a missing target
    or a later one; the first invalid source line is reported."""
    assert clarify.parse_suggestion(body) == expected_line, name


# The invalid examples the contract doc documents, with the line number the
# parser reports for each (Issue #1926 acceptance 3).
INVALID_SECTION_EXAMPLES = (
    (
        "missing end marker",
        "\n".join([
            "<!-- orbi:suggestion:v1 kind=split -->",
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
        ]),
        1,
    ),
    (
        "empty section",
        "\n".join([
            "<!-- orbi:suggestion:v1 kind=criteria -->",
            "<!-- /orbi/suggestion -->",
        ]),
        2,
    ),
    (
        "item without acceptance",
        _section([
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "2. Users can export a note",
        ]),
        4,
    ),
    (
        "duplicate item numbers",
        _section([
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "1. Users can export a note",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
    (
        "after itself",
        _section([
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "2. Users can export a note (after 2)",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
    (
        "after a later item",
        _section([
            "1. Users can tag a note (after 2)",
            "   Acceptance: filtering by the tag finds the note",
            "2. Users can export a note",
            "   Acceptance: a .md file downloads",
        ]),
        2,
    ),
    (
        "after a missing item",
        _section([
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "2. Users can export a note (after 7)",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
    (
        "item line without a number",
        _section([
            "1. Users can tag a note",
            "   Acceptance: filtering by the tag finds the note",
            "Users can export a note",
            "   Acceptance: a .md file downloads",
        ]),
        4,
    ),
    (
        "acceptance line without its prefix",
        _section([
            "1. Users can tag a note",
            "   Receipt: filtering by the tag finds the note",
        ]),
        2,
    ),
    (
        "unknown kind",
        _section(["- Users can tag a note"], kind="plan"),
        1,
    ),
    (
        "criteria item is not a condition",
        _section(["1. Users can tag a note"], kind="criteria"),
        2,
    ),
    (
        "second start marker",
        "\n".join([
            "<!-- orbi:suggestion:v1 kind=criteria -->",
            "- The tag survives a reload",
            "<!-- orbi:suggestion:v1 kind=criteria -->",
            "<!-- /orbi/suggestion -->",
        ]),
        3,
    ),
)


@pytest.mark.parametrize(
    "name, body, expected_line",
    INVALID_SECTION_EXAMPLES,
    ids=[example[0] for example in INVALID_SECTION_EXAMPLES],
)
def test_the_parser_reports_the_first_invalid_line(name, body, expected_line):
    """Acceptance 3: every invalid example in the contract doc returns its
    line number."""
    assert clarify.parse_suggestion(body) == expected_line, name


def test_blank_lines_inside_a_section_are_ignored():
    body = _section([
        "",
        "1. Users can tag a note",
        "",
        "   Acceptance: after adding a tag, filtering by it finds the note; "
        "the tag survives a reload",
        "",
        "2. Users can export a note as Markdown (after 1)",
        "",
        "   Acceptance: Export downloads a .md file with title, body and tags",
        "",
    ])
    assert clarify.parse_suggestion(body) == SPLIT_PROPOSAL


def test_a_malformed_start_marker_is_invalid_at_its_own_line():
    assert clarify.parse_suggestion("<!-- orbi:suggestion:v1 -->") == 1
    assert clarify.parse_suggestion(
        "<!-- orbi:suggestion:v1 kind=split\n1. A"
    ) == 1


def test_invalid_line_numbers_count_the_whole_comment_body():
    """The convention the contract documents: 1-based over the body the
    parser receives, markers and prose included."""
    body = "\n".join([
        f"<!-- orbi:run={RUN_ID} -->",
        clarify.stop_marker(ISSUE),
        "**Orbi: this ticket is not ready to deliver.**",
        "",
        "<!-- orbi:suggestion:v1 kind=split -->",
        "1. Users can tag a note",
        "   Acceptance: filtering by the tag finds the note",
        "2. Users can export a note",
        "<!-- /orbi/suggestion -->",
    ])
    assert clarify.parse_suggestion(body) == 8


def test_a_body_without_a_start_marker_carries_no_proposal():
    """A missing start marker is not an error: the comment simply has no
    proposal (the same answer as a stop from before this contract)."""
    assert clarify.parse_suggestion("just prose") is None
    assert clarify.parse_suggestion("") is None
    assert clarify.parse_suggestion(None) is None
    assert clarify.parse_suggestion(
        "<!-- /orbi/suggestion -->"
    ) is None


# --- the authoritative stop comment ---------------------------------------

def _stop_body(extra="", run_id="deadbeef"):
    return "\n".join([
        f"<!-- orbi:run={run_id} -->",
        clarify.stop_marker(ISSUE),
        extra,
    ])


def test_the_newest_trusted_stop_is_authoritative_even_without_proposal(
    monkeypatch,
):
    """Acceptance: the newest trusted stop wins, and an older stop's
    proposal is never reused."""
    older = stop_comment_entry(
        _stop_body(clarify.render_suggestion(SPLIT_PROPOSAL, "en")),
    )
    newer = stop_comment_entry(_stop_body("no proposal here"))
    stub_stop_history(monkeypatch, [older, newer])
    stub_runner_login(monkeypatch)
    assert clarify.authoritative_stop(ISSUE, REPO) == newer
    assert clarify.stop_suggestion(ISSUE, REPO) is None


def test_the_authoritative_stop_carries_the_proposal(monkeypatch):
    older = stop_comment_entry(_stop_body("no proposal here"))
    newer = stop_comment_entry(
        _stop_body(clarify.render_suggestion(SPLIT_PROPOSAL, "en")),
    )
    stub_stop_history(monkeypatch, [older, newer])
    stub_runner_login(monkeypatch)
    assert clarify.stop_suggestion(ISSUE, REPO) == SPLIT_PROPOSAL


def test_an_untrusted_newer_stop_is_never_authoritative(monkeypatch, caplog):
    """The same trust rule as the stop marker: a marker on somebody else's
    comment is not stop state."""
    trusted = stop_comment_entry(
        _stop_body(clarify.render_suggestion(CRITERIA_PROPOSAL, "zh")),
    )
    forged = stop_comment_entry(
        _stop_body(clarify.render_suggestion(SPLIT_PROPOSAL, "en")),
        login="mallory",
    )
    stub_stop_history(monkeypatch, [trusted, forged])
    stub_runner_login(monkeypatch)
    caplog.set_level("INFO")
    assert clarify.stop_suggestion(ISSUE, REPO) == CRITERIA_PROPOSAL
    assert "clarify_stop_untrusted" in caplog.text


def test_a_ticket_without_a_stop_has_no_authoritative_comment(monkeypatch):
    stub_stop_history(monkeypatch, [])
    stub_runner_login(monkeypatch)
    assert clarify.authoritative_stop(ISSUE, REPO) is None
    assert clarify.stop_suggestion(ISSUE, REPO) is None


def test_an_unreadable_history_has_no_authoritative_comment(
    monkeypatch, caplog,
):
    FakeGate(monkeypatch, history_error=RuntimeError("gh down"))
    caplog.set_level("INFO")
    assert clarify.authoritative_stop(ISSUE, REPO) is None
    assert "clarify_check_skipped" in caplog.text
