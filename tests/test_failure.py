"""The failure record: versioned machine-readable reason (Issue #1322).

`orbi.failure` is a pure module (the `scene` model): no I/O, no `gh`,
deterministic render and parse. Every `Orbi: blocked` / `Orbi: fix
needed` failure comment carries one hidden `<!-- orbi:failure:v1
{json} -->` block so a status reader (Orbi Cloud) can classify the stop
without reading prose.

`parse` distinguishes the two shapes the reader must tell apart:
`None` — the body carries no failure block (an old comment, a comment
of another kind); `FailureError` — a block is present but corrupted.

The readable hierarchy stays beside the block: `**Action:**`,
`**Reason:**` and the bounded raw evidence inside ONE `<details>`.
"""
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner
from orbi import failure, progress
from orbi.pi_process import RateLimitExhaustedError
from seam import seam

REPO_ROOT = Path(__file__).resolve().parents[1]


def record_for(**overrides) -> failure.Failure:
    values = dict(
        reason_code="github_transient",
        action_code="requeue",
        retry_safe=True,
        outcome="blocked",
    )
    values.update(overrides)
    return failure.Failure(**values)


def block_for(**overrides) -> str:
    return failure.render(record_for(**overrides))


# --------------------------------------------------------------------------
# The pure module: render + parse
# --------------------------------------------------------------------------

def test_failure_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        record_for().reason_code = "unclassified"


def test_schema_version_is_one():
    assert failure.SCHEMA_VERSION == 1
    assert record_for().schema == 1


def test_reason_and_action_codes_are_closed_sets_with_expected_members():
    assert failure.REASON_CODES == {
        "github_transient",
        "tests_failed",
        "review_budget_exhausted",
        "human_decision_required",
        "credential_missing",
        "provider_quota",
        "unclassified",
    }
    assert failure.ACTION_CODES == {
        "requeue",
        "fix_ticket",
        "decide",
        "check_credentials",
        "wait_quota",
    }


def test_render_produces_the_single_hidden_v1_block():
    block = block_for()
    assert block.startswith("<!-- orbi:failure:v1 {")
    assert block.endswith(" -->")
    assert block.count("<!--") == 1


def test_render_matches_the_documented_example_byte_for_byte():
    """`docs/workflow.mdx` (EN + ZH) quotes this exact block: the doc
    and the real output are one contract, not two guesses."""
    assert block_for() == (
        '<!-- orbi:failure:v1 {"schema":1,"outcome":"blocked",'
        '"reason_code":"github_transient","action_code":"requeue",'
        '"retry_safe":true} -->'
    )


def test_render_is_deterministic():
    assert block_for() == block_for()


def test_parse_round_trips_a_rendered_record():
    assert failure.parse(block_for()) == record_for()
    assert failure.parse(
        block_for(
            reason_code="tests_failed", action_code="fix_ticket",
            retry_safe=False, outcome="fix_needed",
        )
    ) == record_for(
        reason_code="tests_failed", action_code="fix_ticket",
        retry_safe=False, outcome="fix_needed",
    )


def test_parse_reads_the_block_from_a_full_production_comment():
    body = (
        "<!-- orbi:run=a1b2c3d4 -->\n"
        "<!-- orbi:fail=0123456789abcdef -->\n"
        f"{block_for()}\n\n"
        "Orbi: blocked — waiting on a human decision\n\n"
        "**Action:** relabel the Issue `ai-ready` to retry\n"
        "**Reason:** GraphQL: Something went wrong while executing your query\n"
    )
    assert failure.parse(body) == record_for()


def test_parse_returns_none_when_the_body_carries_no_block():
    for body in (
        "",
        "Orbi opened PR: https://github.com/o/r/pull/1",
        "<!-- orbi:run=a1b2c3d4 -->\n<!-- orbi:fail=0123456789abcdef -->\n"
        "Orbi: blocked — waiting on a human decision",
        None,
        12345,
    ):
        assert failure.parse(body) is None


def test_parse_ignores_the_distinct_failure_fingerprint_marker():
    """`orbi:fail=<fingerprint>` (Issue #825) is not the v1 block."""
    assert failure.parse("<!-- orbi:fail=0123456789abcdef -->") is None


def test_parse_fails_fast_on_a_block_with_invalid_json():
    with pytest.raises(failure.FailureError, match="not valid JSON"):
        failure.parse("<!-- orbi:failure:v1 {nope -->")


def test_parse_fails_fast_on_a_non_object_json_payload():
    with pytest.raises(failure.FailureError, match="JSON object"):
        failure.parse('<!-- orbi:failure:v1 ["blocked"] -->')


def test_parse_fails_fast_on_a_block_missing_a_required_field():
    with pytest.raises(failure.FailureError, match="missing fields"):
        failure.parse('<!-- orbi:failure:v1 {"schema": 1} -->')


def test_parse_fails_fast_on_a_block_with_an_unknown_field():
    payload = (
        '{"schema": 1, "outcome": "blocked", "reason_code": "unclassified", '
        '"action_code": "fix_ticket", "retry_safe": false, "extra": 1}'
    )
    with pytest.raises(failure.FailureError, match="unknown fields"):
        failure.parse(f"<!-- orbi:failure:v1 {payload} -->")


def test_parse_fails_fast_on_an_unknown_reason_code():
    with pytest.raises(failure.FailureError, match="unknown reason_code"):
        failure.parse(block_for(reason_code="made_up"))


def test_parse_fails_fast_on_an_unknown_action_code():
    with pytest.raises(failure.FailureError, match="unknown action_code"):
        failure.parse(block_for(action_code="made_up"))


def test_parse_fails_fast_on_an_unknown_outcome():
    with pytest.raises(failure.FailureError, match="unknown failure outcome"):
        failure.parse(block_for(outcome="delivered"))


def test_parse_fails_fast_on_a_wrong_schema_version():
    with pytest.raises(failure.FailureError, match="schema"):
        failure.parse(
            '<!-- orbi:failure:v1 {"schema": 2, "outcome": "blocked", '
            '"reason_code": "unclassified", "action_code": "fix_ticket", '
            '"retry_safe": false} -->'
        )


def test_parse_fails_fast_on_a_non_integer_schema():
    with pytest.raises(failure.FailureError, match="schema"):
        failure.parse(
            '<!-- orbi:failure:v1 {"schema": "1", "outcome": "blocked", '
            '"reason_code": "unclassified", "action_code": "fix_ticket", '
            '"retry_safe": false} -->'
        )


def test_parse_fails_fast_on_a_non_boolean_retry_safe():
    with pytest.raises(failure.FailureError, match="retry_safe"):
        failure.parse(
            '<!-- orbi:failure:v1 {"schema": 1, "outcome": "blocked", '
            '"reason_code": "unclassified", "action_code": "fix_ticket", '
            '"retry_safe": "false"} -->'
        )


def test_parse_fails_fast_on_multiple_blocks_in_one_body():
    with pytest.raises(failure.FailureError, match="multiple"):
        failure.parse(f"{block_for()}\n{block_for()}")


def test_render_fails_fast_on_a_code_outside_the_closed_set():
    with pytest.raises(failure.FailureError, match="unknown reason_code"):
        failure.render(record_for(reason_code="made_up"))
    with pytest.raises(failure.FailureError, match="unknown action_code"):
        failure.render(record_for(action_code="made_up"))
    with pytest.raises(failure.FailureError, match="unknown failure outcome"):
        failure.render(record_for(outcome="made_up"))
    with pytest.raises(failure.FailureError, match="schema"):
        failure.render(record_for(schema=2))
    with pytest.raises(failure.FailureError, match="retry_safe"):
        failure.render(record_for(retry_safe="yes"))


def test_parse_keeps_parsing_a_pre_feature_comment_without_the_block():
    """The resume path reads old comments; a missing block is not an error."""
    body = (
        "<!-- orbi:run=a1b2c3d4 -->\n"
        "Orbi opened PR: https://github.com/o/r/pull/1\n"
        "- branch: `orbi/o-r-issue-1`\n"
        "- run_id=a1b2c3d4\n"
        "\n<!-- runner=19472631 -->"
    )
    assert failure.parse(body) is None


# --------------------------------------------------------------------------
# The classifier: every reason_code, its action_code and retry_safe
# --------------------------------------------------------------------------

def github_transient_error() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ["gh", "pr", "create"],
        stderr=(
            "pull request create failed: GraphQL: Something went wrong "
            "while executing your query on 2026-09-23T14:13:38Z."
        ),
    )


CLASSIFY_CASES = (
    (
        "github_transient",
        github_transient_error,
        "requeue",
        True,
        "blocked",
    ),
    (
        "tests_failed",
        lambda: subprocess.CalledProcessError(
            1, ["pytest"], stderr="3 failed, 10 passed in 1.2s",
        ),
        "fix_ticket",
        False,
        "fix_needed",
    ),
    (
        "review_budget_exhausted",
        lambda: runner.ReviewRoundsExhausted("round budget spent"),
        "decide",
        False,
        "blocked",
    ),
    (
        "human_decision_required",
        lambda: runner.HumanDecisionRequired(
            "a maintainer must choose", action="choose",
        ),
        "decide",
        False,
        "blocked",
    ),
    (
        "credential_missing",
        lambda: subprocess.CalledProcessError(
            1, ["gh"], stderr="gh: Bad credentials (HTTP 401)",
        ),
        "check_credentials",
        False,
        "blocked",
    ),
    (
        "provider_quota",
        lambda: RateLimitExhaustedError("provider quota exhausted"),
        "wait_quota",
        True,
        "blocked",
    ),
    (
        "unclassified",
        lambda: RuntimeError("something truly unexpected"),
        "fix_ticket",
        False,
        "blocked",
    ),
)


@pytest.mark.parametrize(
    "reason_code, factory, action_code, retry_safe, outcome",
    CLASSIFY_CASES,
)
def test_classify_failure_maps_each_reason_code(
    reason_code, factory, action_code, retry_safe, outcome,
):
    record = runner._classify_failure(
        factory(), outcome="fix_needed" if outcome == "fix_needed" else "blocked",
    )
    assert record == failure.Failure(
        reason_code=reason_code,
        action_code=action_code,
        retry_safe=retry_safe,
        outcome="fix_needed" if outcome == "fix_needed" else "blocked",
    )


def test_classify_failure_returns_unclassified_for_an_arbitrary_exception():
    record = runner._classify_failure(
        ValueError("no marker anywhere"), outcome="blocked",
    )
    assert record.reason_code == "unclassified"
    assert record.action_code == "fix_ticket"
    assert record.retry_safe is False


def test_classify_failure_maps_generic_unrecoverable_to_human_decision():
    record = runner._classify_failure(
        runner.UnrecoverableDeliveryError("external precondition"), outcome="blocked",
    )
    assert record.reason_code == "human_decision_required"
    assert record.action_code == "decide"


def test_classify_failure_reads_a_provider_quota_marker():
    record = runner._classify_failure(
        RuntimeError("provider returned RESOURCE_EXHAUSTED"), outcome="blocked",
    )
    assert record.reason_code == "provider_quota"
    assert record.action_code == "wait_quota"
    assert record.retry_safe is True


def test_classify_failure_dispositions_cover_the_closed_reason_set():
    assert set(failure.DISPOSITIONS) == failure.REASON_CODES
    assert {
        action for action, _ in failure.DISPOSITIONS.values()
    } <= failure.ACTION_CODES


# --------------------------------------------------------------------------
# The comment: the block plus the readable hierarchy
# --------------------------------------------------------------------------

def test_failure_comment_body_renders_the_block_before_the_headline():
    body = runner._failure_comment_body(
        outcome="blocked", action="relabel ai-ready", reason="boom",
        diagnosis="boom", scene="- run: `a1b2c3d4`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="a1b2c3d4",
        failure_record=record_for(),
    )
    assert body.startswith(block_for())
    parsed = failure.parse(body)
    assert parsed == record_for()
    assert "Orbi: blocked — waiting on a human decision" in body
    assert "**Action:** relabel ai-ready" in body
    assert "**Reason:** boom" in body


@pytest.mark.parametrize(
    "reason_code, factory, action_code, retry_safe, outcome",
    CLASSIFY_CASES,
)
def test_every_reason_code_renders_a_parsable_comment(
    reason_code, factory, action_code, retry_safe, outcome,
):
    """Acceptance: one comment per reason_code whose block parses back to
    that code, its action_code and retry_safe."""
    record = runner._classify_failure(
        factory(), outcome="fix_needed" if outcome == "fix_needed" else "blocked",
    )
    body = runner._failure_comment_body(
        outcome="fix needed" if outcome == "fix_needed" else "blocked",
        action="do the thing", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="", pr_url=None,
        issue="orbi-build/orbi#1322", run_id="-", failure_record=record,
    )
    parsed = failure.parse(body)
    assert parsed == failure.Failure(
        reason_code=reason_code,
        action_code=action_code,
        retry_safe=retry_safe,
        outcome="fix_needed" if outcome == "fix_needed" else "blocked",
    )
    assert body.startswith(failure.render(parsed))
    assert "**Action:** do the thing" in body
    assert "**Reason:** boom" in body


def test_failure_comment_body_renders_a_fix_needed_block():
    body = runner._failure_comment_body(
        outcome="fix needed", action="", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="-",
        failure_record=record_for(
            reason_code="tests_failed", action_code="fix_ticket",
            retry_safe=False, outcome="fix_needed",
        ),
    )
    parsed = failure.parse(body)
    assert parsed is not None
    assert parsed.outcome == "fix_needed"
    assert parsed.reason_code == "tests_failed"
    assert parsed.action_code == "fix_ticket"


def test_failure_comment_body_defaults_to_unclassified_without_a_record():
    body = runner._failure_comment_body(
        outcome="blocked", action="", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="-",
    )
    parsed = failure.parse(body)
    assert parsed is not None
    assert parsed.reason_code == "unclassified"
    assert parsed.action_code == "fix_ticket"
    assert parsed.retry_safe is False
    assert "**Reason:** boom" in body


# --------------------------------------------------------------------------
# End to end through report_delivery_failure (the single failure path)
# --------------------------------------------------------------------------

def _capture_comment(monkeypatch):
    posted: list[str] = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *a, **k: set())
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [])
    monkeypatch.setattr(
        seam, "comment_issue", lambda number, *, repo, body: posted.append(body),
    )
    return posted


def test_report_delivery_failure_classifies_a_transient_github_error(monkeypatch):
    posted = _capture_comment(monkeypatch)
    outcome = runner.report_delivery_failure(
        github_transient_error(),
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        reason="the delivery command failed: pull request create failed",
        diagnosis="pull request create failed",
        classify=False, evidence=False,
    )
    assert outcome == "blocked"
    parsed = failure.parse(posted[0])
    assert parsed is not None
    assert parsed.reason_code == "github_transient"
    assert parsed.action_code == "requeue"
    assert parsed.retry_safe is True
    assert parsed.outcome == "blocked"


def test_report_delivery_failure_keeps_full_bounded_stderr(monkeypatch):
    posted = _capture_comment(monkeypatch)
    stderr = (
        "pull request create failed: GraphQL: Something went wrong while "
        "executing your query on 2026-09-23T14:13:38Z. Please include "
        "D1F4ED5C in your report."
    )
    error = subprocess.CalledProcessError(1, ["gh", "pr", "create"], stderr=stderr)
    runner.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        reason=f"the delivery command failed: {runner._failure_detail(error)}",
        diagnosis=runner._failure_detail(error),
        classify=False, evidence=False,
    )
    body = posted[0]
    assert stderr in body
    assert "stderr: pull" not in body


def test_report_delivery_failure_unknown_exception_is_unclassified(monkeypatch):
    posted = _capture_comment(monkeypatch)
    outcome = runner.report_delivery_failure(
        RuntimeError("totally unexpected"),
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason="The delivery stopped before it could be completed: totally unexpected",
        diagnosis="totally unexpected",
        classify=False, evidence=False,
    )
    assert outcome == "blocked"
    body = posted[0]
    parsed = failure.parse(body)
    assert parsed is not None
    assert parsed.reason_code == "unclassified"
    assert parsed.action_code == "fix_ticket"
    assert parsed.outcome == "blocked"
    assert "**Action:** Fix the failure described below and re-run this Issue." in body
    assert "**Reason:** The delivery stopped" in body
    assert body.count("<details>") == 1
    assert body.count("</details>") == 1


def test_report_delivery_failure_recoverable_is_fix_needed(monkeypatch):
    posted = _capture_comment(monkeypatch)
    monkeypatch.setattr(seam, "comment_pr", lambda *a, **k: None)
    error = subprocess.CalledProcessError(1, ["pytest"], stderr="3 failed")
    outcome = runner.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None,
        pr_url="https://github.com/orbi-build/orbi/pull/9",
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason="the tests failed", diagnosis="3 failed",
        evidence=False,
    )
    assert outcome == "fix needed"
    parsed = failure.parse(posted[0])
    assert parsed is not None
    assert parsed.reason_code == "tests_failed"
    assert parsed.action_code == "fix_ticket"
    assert parsed.outcome == "fix_needed"
    assert parsed.retry_safe is False


def _milestone_publisher(run_id="c517c8c7"):
    """A real publisher whose milestone POST bodies are captured.

    Milestones go through the publisher's own `_post_comment`
    (`gh api ... --method POST`), so capturing that command is the real
    milestone rendering, not a stub.
    """
    milestones: list[str] = []

    def fake_run_command(command, **kwargs):
        # Only the milestone POST happens: the reporter is called with
        # `finish=False`, so the tracked comment is never located or
        # PATCHed.
        assert "--method" in command and "POST" in command, command
        milestones.append(command[-1].removeprefix("body="))
        return json.dumps({"id": 42})

    publisher = progress.ProgressPublisher(
        1322, "orbi-build/orbi", run_id, run_command=fake_run_command,
    )
    return publisher, milestones


def test_report_delivery_failure_milestone_is_machine_readable(monkeypatch):
    """Acceptance: the live `Orbi: requeued` milestone of a transient
    failure (Issue #1351; orbi-build/orbi#1088 shows the pre-retry shape)
    carries the same block as the detailed comment, the full bounded
    stderr, and no truncated `stderr: pull` line."""
    posted = _capture_comment(monkeypatch)
    publisher, milestones = _milestone_publisher()
    stderr = (
        "pull request create failed: GraphQL: Something went wrong while "
        "executing your query on 2026-09-23T14:13:38Z. Please include "
        "D1F4ED5C in your report."
    )
    error = subprocess.CalledProcessError(
        1, ["gh", "pr", "create"], stderr=stderr,
    )
    outcome = runner.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c517c8c7", pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        reason=f"The delivery stopped: {runner._failure_detail(error)}",
        diagnosis=runner._failure_detail(error),
        classify=False, evidence=False, publisher=publisher, finish=False,
    )
    assert outcome == "requeued"
    assert len(milestones) == 1
    milestone = milestones[0]
    assert "Orbi: requeued" in milestone
    # The stderr line is the bounded stderr, not its first word.
    assert stderr in milestone
    assert "- stderr: pull" not in milestone
    record = failure.Failure(
        reason_code="github_transient", action_code="requeue",
        retry_safe=True, outcome="blocked",
    )
    assert failure.parse(milestone) == record
    # The detailed comment carries its own copy of the same record and
    # the visible one-shot retry line.
    assert failure.parse(posted[0]) == record
    assert failure.AUTO_RETRY_LINE in posted[0]


def test_report_delivery_failure_fix_needed_milestone_is_machine_readable(
    monkeypatch,
):
    posted = _capture_comment(monkeypatch)
    publisher, milestones = _milestone_publisher()
    monkeypatch.setattr(seam, "comment_pr", lambda *a, **k: None)
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [])
    error = subprocess.CalledProcessError(1, ["pytest"], stderr="3 failed")
    outcome = runner.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c517c8c7",
        pr_url="https://github.com/orbi-build/orbi/pull/9",
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason=(
            "The independent review of PR 9 failed: "
            f"{runner._failure_detail(error)}"
        ),
        diagnosis=runner._failure_detail(error),
        evidence=False, publisher=publisher, finish=False,
    )
    assert outcome == "fix needed"
    assert len(milestones) == 1
    milestone = milestones[0]
    assert "Orbi: fix needed" in milestone
    assert failure.parse(milestone) == failure.Failure(
        reason_code="tests_failed", action_code="fix_ticket",
        retry_safe=False, outcome="fix_needed",
    )
    assert "- stderr: 3" not in milestone
    assert "3 failed" in milestone


# --------------------------------------------------------------------------
# The one-shot transient retry (Issue #1351)
# --------------------------------------------------------------------------

def _retry_fakes(monkeypatch, *, history=None,
                 labels=("ai-in-progress",)):
    """A mutable comment store behind the REAL label patch.

    `apply_label_patch` runs for real and its `edit_issue` calls are
    captured, so a test asserts the actual label transition rather than a
    stub. A posted failure comment joins the store exactly like the GitHub
    API — that shared store is what a second runner instance reads.
    """
    store = [dict(comment) for comment in (history or [])]
    captured = {"comments": [], "edits": [], "milestones": []}

    def post_issue_comment(number, *, repo, body):
        captured["comments"].append(body)
        store.append({
            "id": 5000 + len(store), "body": body,
            "authorAssociation": "OWNER",
            "url": (
                f"https://github.com/{repo}/issues/{number}"
                f"#issuecomment-{5000 + len(store)}"
            ),
        })

    monkeypatch.setattr(seam, "comment_issue", post_issue_comment)
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: list(store))
    monkeypatch.setattr(seam, "issue_labels", lambda *a, **k: set(labels))
    monkeypatch.setattr(
        seam, "edit_issue",
        lambda number, *, repo, add=None, remove=None:
            captured["edits"].append((add, remove)),
    )

    def fake_run_command(command, **kwargs):
        # The progress milestone is a bypass; capture its body and answer
        # the `gh api` response shape the publisher requires.
        captured["milestones"].append(command[-1].removeprefix("body="))
        return json.dumps({"id": 42})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return captured, store


def _report_failure(exc, *, run_id="a1b2c3d4", reason="the delivery failed",
                    diagnosis="the delivery failed", **kwargs):
    return runner.report_delivery_failure(
        exc, issue={"number": 1351, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=run_id, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason=reason, diagnosis=diagnosis,
        classify=False, evidence=False, finish=False, **kwargs,
    )


def test_first_github_transient_failure_requeues_the_issue_once(monkeypatch):
    """Acceptance: the first transient failure becomes `ai-ready` without
    `ai-blocked` and the comment carries the one-shot retry line."""
    captured, store = _retry_fakes(monkeypatch)

    outcome = _report_failure(
        github_transient_error(),
        reason="the delivery command failed: pull request create failed",
        diagnosis="pull request create failed",
    )

    assert outcome == "requeued"
    assert captured["edits"] == [("ai-ready", "ai-in-progress")]
    body = captured["comments"][0]
    assert failure.AUTO_RETRY_LINE in body
    parsed = failure.parse(body)
    assert parsed == record_for()
    # The retry comment is itself the record the next occurrence reads.
    assert any(failure.AUTO_RETRY_LINE in c["body"] for c in store)


def test_second_github_transient_failure_stays_blocked(monkeypatch):
    """Acceptance: once the retry is used, the next transient failure
    stops at `ai-blocked` and says so."""
    first = (
        "<!-- orbi:run=deadbeef -->\n"
        f"{failure.AUTO_RETRY_LINE}\n{failure.render(record_for())}\n\n"
        "Orbi: blocked — waiting on a human decision\n"
    )
    captured, _ = _retry_fakes(
        monkeypatch,
        history=[{"body": first, "authorAssociation": "OWNER"}],
    )

    outcome = _report_failure(github_transient_error(), run_id="feedface")

    assert outcome == "blocked"
    assert captured["edits"] == [("ai-blocked", "ai-in-progress")]
    body = captured["comments"][0]
    assert failure.AUTO_RETRY_SPENT_LINE in body
    assert failure.AUTO_RETRY_LINE not in body


def test_terminal_tests_failed_never_requeues(monkeypatch):
    """Acceptance: a non-retry-safe record still stops at `ai-blocked`."""
    captured, _ = _retry_fakes(monkeypatch)
    error = subprocess.CalledProcessError(1, ["pytest"], stderr="3 failed")

    outcome = _report_failure(
        error, reason="the delivery tests failed", diagnosis="3 failed",
    )

    assert outcome == "blocked"
    assert captured["edits"] == [("ai-blocked", "ai-in-progress")]
    body = captured["comments"][0]
    assert failure.AUTO_RETRY_LINE not in body
    assert failure.AUTO_RETRY_SPENT_LINE not in body


def test_terminal_unclassified_never_requeues(monkeypatch):
    """Acceptance: an unclassified record still stops at `ai-blocked`."""
    captured, _ = _retry_fakes(monkeypatch)

    outcome = _report_failure(RuntimeError("totally unexpected"))

    assert outcome == "blocked"
    assert captured["edits"] == [("ai-blocked", "ai-in-progress")]
    assert failure.AUTO_RETRY_LINE not in captured["comments"][0]


def test_two_instances_requeue_a_transient_failure_at_most_once(monkeypatch):
    """Acceptance: the retry budget lives in the Issue comments, so a
    second runner instance that re-reads the same failure does not
    re-queue it again."""
    captured, store = _retry_fakes(monkeypatch)

    _report_failure(github_transient_error(), run_id="a1b2c3d4")
    _report_failure(github_transient_error(), run_id="a1b2c3d4")

    retry_comments = [
        comment for comment in store
        if failure.AUTO_RETRY_LINE in comment["body"]
    ]
    requeues = [edit for edit in captured["edits"] if edit[0] == "ai-ready"]
    assert len(retry_comments) == 1
    assert len(requeues) == 1


def test_transient_failure_scan_skips_corrupted_and_untrusted_comments():
    """A malformed or untrusted failure record neither consumes nor
    grants the one-shot retry budget — the scan ignores it."""
    transient = failure.render(record_for())
    untrusted = {"body": transient, "authorAssociation": "NONE"}
    corrupted = {
        "body": f"{transient}\n{transient}\n",
        "authorAssociation": "OWNER",
    }
    plain = {"body": "just chatter", "authorAssociation": "OWNER"}
    not_transient = {
        "body": failure.render(record_for(
            reason_code="tests_failed", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "authorAssociation": "OWNER",
    }
    assert runner._transient_failure_seen(
        [untrusted, corrupted, plain, not_transient],
    ) is False
    assert runner._transient_failure_seen(
        [untrusted, corrupted, plain, not_transient,
         {"body": transient, "authorAssociation": "OWNER"}],
    ) is True


# --------------------------------------------------------------------------
# The public contract in the docs
# --------------------------------------------------------------------------

def test_docs_document_the_failure_block_and_the_closed_code_sets():
    example = failure.render(record_for())
    for relative in ("docs/workflow.mdx", "docs/zh/workflow.mdx"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "orbi:failure:v1" in text, relative
        # The quoted example is the real render output, not prose.
        assert example in text, relative
        for code in failure.REASON_CODES:
            assert code in text, f"{relative} missing reason_code {code}"
        for code in failure.ACTION_CODES:
            assert code in text, f"{relative} missing action_code {code}"
