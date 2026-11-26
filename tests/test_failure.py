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

import orbi.failure_report as failure_report
import orbi.runner as runner
from orbi import failure, language, progress, provider_balance
from orbi.comment_kind import (
    COMMENT_KIND_REQUEUED, comment_kind, comment_kind_marker,
)
from orbi.pi_process import RateLimitExhaustedError
from seam import seam
from tests.fakes.github import FakeGh, install_progress_gh

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
        "provider_balance",
        "provider_quota",
        "base_branch_missing",
        "base_branch_changed",
        "base_ci_red",
        "checks_permission_missing",
        "release_failed",
        "no_commit_delivered",
        "no_file_change",
        "attachment_unavailable",
        "unclassified",
    }
    assert failure.ACTION_CODES == {
        "requeue",
        "fix_ticket",
        "decide",
        "check_credentials",
        "top_up_balance",
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


# The captured two-line rejection of Issue #1651: the decisive App/
# workflows refusal sits immediately ABOVE Git's generic final line.
WORKFLOW_REFUSAL_STDERR = (
    " ! [remote rejected] HEAD -> orbi/example-repo-issue-4 "
    "(refusing to allow a GitHub App to create or update workflow "
    "`.github/workflows/ci.yml` without `workflows` permission)\n"
    "error: failed to push some refs to "
    "'https://github.com/example/repo.git'"
)


def workflow_refusal_error() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ["git", "push", "origin", "HEAD:orbi/example-repo-issue-4"],
        stderr=WORKFLOW_REFUSAL_STDERR,
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
        "base_branch_missing",
        # Issue #1756: the first delivery step decides the reason at the
        # source and carries it on the exception.
        lambda: failure.BaseBranchMissingError("release/9"),
        "fix_ticket",
        False,
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


@pytest.mark.parametrize(
    "detail",
    [
        # Issue #1759: the delivery runs no tests of its own, so an "N
        # failed" count in a message is prose, never a test result. The
        # classifier must not relabel it as `tests_failed`.
        "push failed: 2 failed attempts",
        "2 failed attempts to reach the provider",
        "test failed on the second call",
        # Even a pytest-shaped line is not evidence: nothing runs pytest.
        "3 failed, 10 passed in 1.2s",
    ],
)
def test_a_failure_merely_mentioning_a_failed_count_is_not_tests_failed(detail):
    record = failure.classify(detail)
    assert record.reason_code != "tests_failed"
    assert record == failure.Failure(
        reason_code="unclassified", action_code="fix_ticket",
        retry_safe=False, outcome="blocked",
    )


def test_classify_failure_maps_generic_unrecoverable_to_human_decision():
    record = runner._classify_failure(
        runner.UnrecoverableDeliveryError("external precondition"), outcome="blocked",
    )
    assert record.reason_code == "human_decision_required"
    assert record.action_code == "decide"


def test_classify_maps_the_actions_workflow_refusal_to_credential_missing():
    """Issue #1651: the explicit GitHub App/workflows refusal reuses the
    existing `credential_missing` / `check_credentials` category (a
    repair is required before retry)."""
    record = failure.classify(runner.failure_detail(workflow_refusal_error()))
    assert record.reason_code == "credential_missing"
    assert record.action_code == "check_credentials"
    assert record.retry_safe is False


@pytest.mark.parametrize(
    "detail",
    [
        # A non-fast-forward rejection is a plain push conflict.
        " ! [rejected] HEAD -> b (non-fast-forward)\n"
        "error: failed to push some refs to 'https://github.com/e/r.git'",
        # A generic 403 without the App/workflows refusal sentence.
        "remote: HTTP 403",
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "The requested URL returned error: 403",
        # The bare word `workflow` (or a workflow path) is not the refusal.
        "error: failed to push some refs (workflow)",
        "diff --git a/.github/workflows/ci.yml",
        # A transient network failure.
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "Could not resolve host: github.com",
        # An unrecognized error.
        "totally unexpected",
    ],
)
def test_workflow_permission_refusal_needs_the_full_sentence(detail):
    assert failure.workflow_permission_refused(detail) is False
    assert failure.classify(detail).reason_code != "credential_missing"


def test_failure_summary_keeps_the_workflow_refusal_above_gits_final_line():
    """Issue #1651: the decisive refusal line must not be replaced by Git's
    generic `error: failed to push some refs` final line."""
    summary = failure.failure_summary(
        runner.failure_detail(workflow_refusal_error()), language_code="en"
    )
    assert "refusing to allow a GitHub App" in summary
    assert "without `workflows` permission" in summary
    assert "failed to push some refs" not in summary


def test_workflow_permission_action_without_a_pr_names_the_ai_ready_repair():
    action = failure.workflow_permission_action(None, language_code="en")
    assert "cannot modify Actions workflows" in action
    assert "Workflows read/write" in action
    assert "remove `ai-blocked`" in action
    assert "`ai-ready`" in action
    assert "contact the App operator" in action
    assert "No PR has been created." in action
    # No PR: the reader restarts the Issue, not a delivery PR, and no
    # automatic restart is promised.
    assert "ai-fix-needed" not in action
    assert "automatically" not in action


def test_workflow_permission_action_with_a_pr_continues_the_same_pr():
    url = "https://github.com/orbi-build/orbi/pull/9"
    action = failure.workflow_permission_action(url, language_code="en")
    assert "cannot modify Actions workflows" in action
    assert "remove `ai-blocked`" in action
    assert "`ai-fix-needed`" in action
    assert url in action
    assert "`ai-ready`" not in action
    assert "No PR has been created." not in action
    # The fix never asks the reader to reinstall the App or share a token.
    assert "reinstall" not in action
    assert "token" not in action


def test_terminal_action_unknown_failure_names_the_undetermined_cause():
    action = failure.terminal_action(failure.classify("totally unexpected"), language_code="en")
    assert "could not be determined" in action
    assert "re-run this Issue" in action
    # The unknown-error explanation never blames the Issue text.
    assert "Issue text" not in action
    assert "reword" not in action


def test_terminal_action_recognized_failure_keeps_the_generic_action():
    action = failure.terminal_action(
        failure.classify("http 500 internal server error"), language_code="en"
    )
    assert action == "Fix the failure described below and re-run this Issue."


# --------------------------------------------------------------------------
# Issue #2351: ONE function of the record decides the headline and the action
# --------------------------------------------------------------------------

# Every stop whose recorded action a plain re-queue cannot satisfy: the set
# the Issue calls the twelve `retry_safe=False` codes.
RETRY_UNSAFE = tuple(
    code for code, (_action, retry_safe) in failure.DISPOSITIONS.items()
    if not retry_safe
)


def record_of(reason_code: str) -> failure.Failure:
    return failure.Failure(reason_code, *failure.DISPOSITIONS[reason_code])


@pytest.mark.parametrize("reason_code", RETRY_UNSAFE)
def test_every_retry_unsafe_stop_words_its_own_action(reason_code):
    """Issue #2351 acceptance 4: none of the twelve `retry_safe=False`
    stops shows the generic "fix the failure and re-run" line any more, and
    only the action `decide` says "waiting on a human decision"."""
    record = record_of(reason_code)
    headline, action = failure.stop_wording(record, "en")
    assert action != language.text("en", "failure.terminal_generic")
    assert action.strip() != ""
    decision = language.text("en", "failure.disposition_decide")
    if record.action_code == "decide":
        assert decision in headline
    else:
        assert decision not in headline
    body = failure.failure_comment_body(
        outcome="blocked", action=action, reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="", pr_url=None,
        issue="orbi-build/orbi#2351", run_id="a1b2c3d4",
        failure_record=record, language_code="en",
    )
    assert language.text("en", "failure.terminal_generic") not in body
    assert headline in body
    assert failure.parse(body) == record


def test_the_fourteen_retry_unsafe_codes_are_covered():
    """The twelve codes of Issue #2351 plus `attachment_unavailable`
    (Issue #2364) and `base_branch_changed` (Issue #2458): the table above
    must not silently shrink to a subset."""
    assert len(RETRY_UNSAFE) == 14


def test_two_reasons_with_the_same_action_get_the_same_wording():
    """The wording reads the recorded action and retry_safe, not the reason
    code alone: two `fix_ticket` stops say the same thing."""
    ticket_change = failure.stop_wording(record_of("tests_failed"), "en")
    assert failure.stop_wording(record_of("release_failed"), "en") == ticket_change
    assert failure.stop_wording(record_of("base_ci_red"), "en") == ticket_change
    for code in ("review_budget_exhausted", "human_decision_required"):
        assert failure.stop_wording(record_of(code), "en")[0] == (
            failure.stop_wording(record_of("review_budget_exhausted"), "en")[0]
        )


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_a_no_file_change_stop_says_the_run_changed_no_file(language_code):
    """Issue #2358: the empty-tree stop names what happened (the run changed
    no file) and gives the TWO steps that fit it — close an already satisfied
    Issue, or add the missing detail and re-queue. Never the generic
    `fix_ticket` headline/action, which asks for a change nobody may need."""
    headline, action = failure.stop_wording(
        record_of("no_file_change"), language_code,
    )
    assert language.text(
        language_code, "failure.disposition_no_file_change") in headline
    assert language.text(
        language_code, "failure.disposition_ticket_change") not in headline
    assert action == language.text(
        language_code, "failure.terminal_no_file_change")
    assert language.text(
        language_code, "failure.terminal_ticket_change") not in action
    assert "`ai-blocked`" in action and "`ai-ready`" in action
    if language_code == "en":
        assert "changed no file" in headline
        assert "close the Issue" in action
        assert "already satisfied" in action
        assert "still missing" in action
        assert "the Issue body" in action


def test_a_no_file_change_stop_keeps_the_no_commit_wording_unchanged():
    """Issue #2358 acceptance 3: the no-commit stop and the other
    `fix_ticket` codes render exactly as before."""
    no_commit = failure.stop_wording(record_of("no_commit_delivered"), "en")
    assert no_commit == (
        language.text(
            "en", "failure.headline_blocked",
            disposition=language.text("en", "failure.disposition_no_commit"),
        ),
        language.text("en", "failure.terminal_ticket_change"),
    )
    assert failure.stop_wording(record_of("no_file_change"), "en") != no_commit


def test_a_no_commit_stop_says_the_run_produced_no_commit():
    """Issue #2351 headline: the no-commit stop names what happened and that
    the ticket needs a change — it never claims a human decision."""
    headline, action = failure.stop_wording(record_of("no_commit_delivered"), "en")
    assert "produced no commit" in headline
    assert "issue needs a change" in headline.lower()
    assert language.text("en", "failure.disposition_decide") not in headline
    assert "the Issue body" in action
    assert "`ai-blocked`" in action and "`ai-ready`" in action


@pytest.mark.parametrize(
    "reason_code, headline_fragment, action_fragment",
    [
        # Issue #1651: an undetermined cause never sends the reader to edit
        # the Issue body — its action says the cause is unknown.
        ("unclassified", "could not be determined", "could not be determined"),
        # Issue #1749: the missing base branch is a repository repair.
        ("base_branch_missing", "base branch", "create `develop`"),
    ],
)
def test_a_reason_with_its_own_repair_keeps_a_headline_that_agrees(
    reason_code, headline_fragment, action_fragment,
):
    """Issue #2351: the two reasons that own an action sentence keep the
    matching headline, so the headline never asks for a ticket change the
    action does not ask for."""
    headline, action = failure.stop_wording(
        record_of(reason_code), "en", base_branch="develop",
    )
    assert headline_fragment in headline
    assert action_fragment in action
    for other in ("disposition_ticket_change", "disposition_decide"):
        assert language.text("en", f"failure.{other}") not in headline


def test_a_credential_stop_asks_for_the_credential_repair():
    headline, action = failure.stop_wording(record_of("credential_missing"), "en")
    assert "credential" in headline
    assert "credential" in action
    assert "`ai-blocked`" in action and "`ai-ready`" in action


def test_a_balance_stop_names_the_existing_top_up_repair():
    """`provider_balance` already owns its reader repair (Issue #2278);
    the one wording function keeps it instead of a generic sentence."""
    from orbi import provider_balance

    _headline, action = failure.stop_wording(record_of("provider_balance"), "en")
    assert action == provider_balance.action("en")


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_the_wording_function_gives_every_stop_a_headline_and_an_action(
    language_code,
):
    for reason_code in failure.REASON_CODES:
        headline, action = failure.stop_wording(
            record_of(reason_code), language_code,
        )
        assert headline.strip() and action.strip(), reason_code


def test_classify_uses_the_carried_record_and_applies_the_outcome():
    """Issue #1756: a failure that decided its reason at the source is
    used as it is — the classifier never re-reads an error message."""
    record = runner._classify_failure(
        failure.BaseBranchMissingError("develop"), outcome="fix_needed",
    )
    assert record == failure.Failure(
        reason_code="base_branch_missing", action_code="fix_ticket",
        retry_safe=False, outcome="fix_needed",
    )


@pytest.mark.parametrize(
    "detail",
    [
        # Git localizes its message, so neither wording decides the
        # reason any more: the remote is asked instead (Issue #1756).
        "fatal: couldn't find remote ref release/9",
        "fatal: could not find remote ref release/9",
        "致命错误：无法找到远程引用 release/9",
    ],
)
def test_gits_missing_ref_text_never_decides_the_reason(detail):
    assert failure.classify(detail).reason_code != "base_branch_missing"


@pytest.mark.parametrize(
    "detail",
    [
        # A network failure of the same fetch keeps its own class.
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "Could not resolve host: github.com",
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "Connection timed out",
        # A non-fast-forward push and an unclassified error are unchanged.
        "! [rejected] HEAD -> b (non-fast-forward)",
        "totally unexpected",
    ],
)
def test_other_failures_are_never_base_branch_missing(detail):
    assert failure.classify(detail).reason_code != "base_branch_missing"


def test_terminal_reason_names_the_configured_base_branch():
    """The comment names the CONFIGURED branch and keeps Git's raw answer
    out of the explanation (Issue #1749, #1753): the evidence belongs to
    the run details, the reason to the reader. The record is the
    structured failure's own (Issue #1756) — nothing re-reads Git's
    (localized) error text."""
    error = failure.BaseBranchMissingError("other")
    record = runner._classify_failure(error, outcome="blocked")
    assert record.reason_code == "base_branch_missing"
    for code, expected in (
        ("en",
         "This repository has no branch `release/9`, so Orbi doesn't "
         "know which branch to start from."),
        ("zh",
         "这个仓库里没有分支 `release/9`，Orbi 不知道该从哪个分支开始改。"),
    ):
        reason = failure.terminal_reason(
            failure.failure_detail(error), record,
            base_branch="release/9", language_code=code,
        )
        assert reason == expected
        assert "couldn't find remote ref" not in reason


def test_terminal_reason_keeps_the_generic_sentence_for_other_failures():
    record = failure.classify("http 500 internal server error")
    reason = failure.terminal_reason(
        "http 500 internal server error", record,
        base_branch="main", language_code="en",
    )
    assert reason == (
        "The delivery stopped before it could be completed: "
        "http 500 internal server error"
    )


def test_classify_failure_reads_a_provider_quota_marker():
    record = runner._classify_failure(
        RuntimeError("provider returned RESOURCE_EXHAUSTED"), outcome="blocked",
    )
    assert record.reason_code == "provider_quota"
    assert record.action_code == "wait_quota"
    assert record.retry_safe is True


@pytest.mark.parametrize(
    "wording",
    [
        # DeepSeek's rejected-key wording (Issue #1465).
        '401: {"message":"Authentication Fails, Your api key: **** is '
        'invalid (request_id: A)"}',
        # OpenAI's rejected-key wording.
        "401 Incorrect API key provided: sk-abc",
        "Error: 401 Unauthorized",
        "HTTP 401: Bad credentials",
        "status 401",
    ],
)
def test_classify_maps_provider_401_wordings_to_credential_missing(wording):
    record = failure.classify(wording)
    assert record.reason_code == "credential_missing"
    assert record.action_code == "check_credentials"
    assert record.retry_safe is False


def test_classify_leaves_a_pytest_401_line_out_of_credential_missing():
    """A bare `401` in test output (e.g. `test_401_handling ... FAILED`)
    is not a provider credential rejection: only the provider-error shape
    (`401:` / `status 401` / `HTTP 401`) counts (Issue #1465)."""
    detail = (
        "Command '['pytest']' returned non-zero exit status 1 "
        "stderr=FAILED tests/test_auth.py::test_401_handling - "
        "AssertionError: expected 200"
    )
    assert failure.classify(detail).reason_code != "credential_missing"


def test_auth_wording_is_shared_with_the_pi_startup_classifier():
    """Issue #1465: `failure` and the Pi startup classifier (extracted to
    `pi_stream_log`, Issue #1860) match ONE auth wording list, so the two
    classifiers cannot drift."""
    from orbi import pi_stream_log

    assert pi_stream_log.PROVIDER_AUTH_WORDS is failure.PROVIDER_AUTH_WORDS
    assert pi_stream_log._classify_startup_exit(
        "Bad unauthorized: the api key was rejected", 1,
    ) == "auth_failure"


def test_classify_a_release_failure_falls_back_to_release_failed():
    """Issue #2222: a release failure the classifier does not otherwise
    recognize is `release_failed` (a ticket to fix), never `unclassified`."""
    record = failure.classify(
        "release tag v0.3.0 already exists on the remote and points at "
        "other123, not the release commit abc123", release=True,
    )
    assert record == failure.Failure(
        reason_code="release_failed", action_code="fix_ticket",
        retry_safe=False, outcome="blocked",
    )


def test_classify_keeps_a_recognized_cause_of_a_release_failure():
    """The release fallback never overrides a cause the classifier knows:
    a rejected credential keeps its own repair path, and a non-release
    failure keeps `unclassified`."""
    assert failure.classify(
        "HTTP 401: bad credentials", release=True,
    ).reason_code == "credential_missing"
    assert failure.classify("boom").reason_code == "unclassified"


def test_release_failure_comment_names_the_code_and_the_failed_step():
    """Issue #2222 acceptance 1: a failed release's stop comment carries
    `release_failed` / `fix_ticket`, and its visible reason says the release
    failed and at which step, in both languages."""
    from orbi import release_comments
    from orbi.run_fields import RunFields

    detail = RuntimeError(
        "release tag v0.3.0 already exists on the remote and points at "
        "other123",
    )
    run_info = RunFields(run_id="a1b2c3d4", priority="normal")
    body = release_comments.release_failure_comment_body(
        "a1b2c3d4", run_info, detail, "en", step="tag",
    )
    assert failure.parse(body) == failure.Failure(
        "release_failed", *failure.DISPOSITIONS["release_failed"],
    )
    assert "Orbi release failed (ai-blocked)" in body
    assert "**Reason:** The release failed at the tag step" in body
    chinese = release_comments.release_failure_comment_body(
        "a1b2c3d4", run_info, detail, "zh", step="tag",
    )
    assert "**原因：** release 在 tag 步骤失败" in chinese
    assert failure.parse(chinese).reason_code == "release_failed"


def test_release_failure_reason_lands_before_the_field_rows():
    """The reason is a visible body line. A comment without field rows still
    carries it, at the end, instead of dropping it."""
    from orbi import terminal_stops

    helper = terminal_stops.release_failure_reason_line
    assert helper(
        "publish", "boom", "Orbi release failed (ai-blocked)\n- run_id=x",
        language_code="en",
    ) == (
        "Orbi release failed (ai-blocked)\n\n"
        "**Reason:** The release failed at the publish step: boom\n"
        "- run_id=x"
    )
    assert helper("tag", "boom", "headline", language_code="en").endswith(
        "**Reason:** The release failed at the tag step: boom",
    )


def test_classify_failure_dispositions_cover_the_closed_reason_set():
    assert set(failure.DISPOSITIONS) == failure.REASON_CODES
    assert {
        action for action, _ in failure.DISPOSITIONS.values()
    } <= failure.ACTION_CODES


# --------------------------------------------------------------------------
# The comment: the block plus the readable hierarchy
# --------------------------------------------------------------------------

def test_failure_comment_body_renders_the_block_before_the_headline():
    body = failure.failure_comment_body(
        outcome="blocked", action="relabel ai-ready", reason="boom",
        diagnosis="boom", scene="- run: `a1b2c3d4`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="a1b2c3d4",
        failure_record=record_for(), language_code="en",
    )
    assert body.startswith(block_for())
    parsed = failure.parse(body)
    assert parsed == record_for()
    # Issue #2351: a `retry_safe` stop says the failure was temporary — it
    # never claims a human decision the record does not ask for.
    assert language.text(
        "en", "failure.headline_blocked",
        disposition=language.text("en", "failure.disposition_retry"),
    ) in body
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
    record = runner._classify_failure(factory(), outcome=outcome)
    body = failure.failure_comment_body(
        outcome=outcome,
        action="do the thing", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="", pr_url=None,
        issue="orbi-build/orbi#1322", run_id="-", failure_record=record, language_code="en",
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
    body = failure.failure_comment_body(
        outcome="fix_needed", action="", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="-",
        failure_record=record_for(
            reason_code="tests_failed", action_code="fix_ticket",
            retry_safe=False, outcome="fix_needed",
        ), language_code="en",
    )
    parsed = failure.parse(body)
    assert parsed is not None
    assert parsed.outcome == "fix_needed"
    assert parsed.reason_code == "tests_failed"
    assert parsed.action_code == "fix_ticket"


def test_failure_comment_body_defaults_to_unclassified_without_a_record():
    body = failure.failure_comment_body(
        outcome="blocked", action="", reason="boom", diagnosis="boom",
        scene="- run: `-`", evidence="",
        pr_url=None, issue="orbi-build/orbi#7", run_id="-", language_code="en",
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
    monkeypatch.setattr(
        seam, "comment_issue", lambda number, *, repo, body: posted.append(body),
    )
    # A recoverable failure with a PR is written to the PR as well; the
    # write is never this suite's business.
    monkeypatch.setattr(seam, "comment_pr", lambda *a, **k: None)
    # The report's progress publisher ("milestone" and "finish" scenes) is
    # not under test here; answer its gh traffic (Issue #2253).
    install_progress_gh(monkeypatch)
    return posted


# --------------------------------------------------------------------------
# Issue #2202: one automatic re-queue after a transient GitHub failure
# --------------------------------------------------------------------------

REQUEUE_ISSUE = 2202
REQUEUE_RUN_ID = "a1b2c3d4"
REQUEUE_REPO = "orbi-build/orbi"


def _requeue_github(monkeypatch, *seeded_comments, number=REQUEUE_ISSUE,
                    title="Re-queue once",
                    labels=("ai-ready", "ai-in-progress")):
    """The real gh adapter over the in-memory GitHub fake (one seam patch).

    `seeded_comments` are `(login, body)` pairs already on the Issue;
    `maintainer` can write to the repository, `stranger` cannot.
    """
    gh = FakeGh(REQUEUE_REPO)
    gh.add_issue(number, title=title, labels=labels)
    gh.add_collaborator("maintainer", "admin")
    for login, body in seeded_comments:
        gh.comment(number, body, login=login)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def _report_implement_failure(error=None, **overrides):
    """One implement-phase terminal report (`classify=False`)."""
    kwargs = dict(
        issue={
            "number": REQUEUE_ISSUE, "title": "Re-queue once",
            "labels": [{"name": "ai-ready"}, {"name": "ai-in-progress"}],
        },
        source_repo=REQUEUE_REPO, run_id=REQUEUE_RUN_ID, pr_url=None,
        worktree=None, branch="orbi/orbi-build-orbi-issue-2202",
        role=runner.ROLE_IMPLEMENT,
        reason="the delivery command failed: pull request create failed",
        diagnosis="pull request create failed",
        classify=False, evidence=False, language_code="en",
    )
    kwargs.update(overrides)
    return failure_report.report_delivery_failure(
        github_transient_error() if error is None else error, **kwargs,
    )


def _bot_comments(gh, number=REQUEUE_ISSUE) -> list[str]:
    """The bodies of the comments this runner wrote on the Issue."""
    return [
        comment["body"] for comment in gh.issues[number]["comments"]
        if comment["author"]["login"] == gh.login
    ]


def test_report_delivery_failure_classifies_a_transient_github_error(monkeypatch):
    """Acceptance 1 (Issue #2202): the FIRST implement-phase
    `github_transient` failure re-queues the Issue itself — the dispatch
    label comes back, ONE comment of the `requeued` kind says what
    happened, and neither a failure block nor `ai-blocked` appears."""
    gh = _requeue_github(monkeypatch)
    publisher = _RecordingPublisher()
    outcome = _report_implement_failure(publisher=publisher, finish=True)
    assert outcome == "requeued"
    labels = gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-ready" in labels
    assert "ai-in-progress" not in labels
    assert "ai-blocked" not in labels
    posted = _bot_comments(gh)
    assert len(posted) == 1
    body = posted[0]
    assert comment_kind(body) == COMMENT_KIND_REQUEUED
    assert body.startswith(f"{progress.run_marker(REQUEUE_RUN_ID)}\n")
    assert "temporary GitHub error" in body
    assert "orbi:failure:v1" not in body
    assert "<details>" not in body
    # ONE comment is the whole message: no milestone notification and no
    # second terminal progress scene, and nothing in it reads as blocked.
    assert publisher.milestones == []
    assert publisher.finish_bodies == []
    assert "blocked" not in body


def test_a_persistent_dns_failure_re_queues_instead_of_blocking(monkeypatch):
    """Issue #2443 acceptance 3 (#2255): a `gh` call that keeps failing with
    `error connecting to api.github.com` is recorded `github_transient` and
    takes the existing single automatic re-queue (#2202) — never
    `ai-blocked` with `unclassified / fix_ticket`."""
    from orbi import gh_response

    gh = _requeue_github(monkeypatch)
    error = gh_response.failed_request(subprocess.CalledProcessError(
        1, ["gh", "issue", "list", "--repo", REQUEUE_REPO],
        stderr="error connecting to api.github.com",
    ))
    publisher = _RecordingPublisher()
    outcome = _report_implement_failure(
        error=error, publisher=publisher, finish=True,
    )

    assert failure.classify(
        runner.failure_detail(error), error=error,
    ) == failure.Failure(
        reason_code="github_transient", action_code="requeue",
        retry_safe=True, outcome="blocked",
    )
    assert outcome == "requeued"
    labels = gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-ready" in labels
    assert "ai-blocked" not in labels
    posted = _bot_comments(gh)
    assert len(posted) == 1
    assert comment_kind(posted[0]) == COMMENT_KIND_REQUEUED
    assert "orbi:failure:v1" not in posted[0]
    assert publisher.milestones == []


def test_a_second_transient_failure_after_the_re_queue_is_terminal(monkeypatch):
    """Acceptance 2: a runner-authored re-queue marker on the Issue makes
    the same failure terminal exactly as before — `ai-blocked` with the
    failure block — so the automatic re-queue can never loop."""
    gh = _requeue_github(monkeypatch, ("maintainer", (
        f"{progress.run_marker('d1e2f3a4')}\n"
        f"{comment_kind_marker(COMMENT_KIND_REQUEUED)}\n"
        "Orbi re-queued this Issue automatically: the run hit a temporary "
        "GitHub error before any PR existed."
    )))
    publisher = _RecordingPublisher()
    outcome = _report_implement_failure(publisher=publisher, finish=True)
    assert outcome == "blocked"
    labels = gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-blocked" in labels
    assert "ai-ready" not in labels
    assert "ai-in-progress" not in labels
    posted = _bot_comments(gh)
    assert len(posted) == 1
    parsed = failure.parse(posted[0])
    assert parsed == failure.Failure(
        reason_code="github_transient", action_code="requeue",
        retry_safe=True, outcome="blocked",
    )
    assert "<details>" in posted[0]
    assert publisher.milestones != []


def test_a_re_queue_marker_from_an_untrusted_comment_is_ignored(monkeypatch):
    """Acceptance 3: the marker is trusted only from a comment whose
    author can write to the repository (or is this runner): a stranger's
    look-alike comment re-queues as the first occurrence."""
    gh = _requeue_github(monkeypatch, ("stranger", (
        f"{progress.run_marker('d1e2f3a4')}\n"
        f"{comment_kind_marker(COMMENT_KIND_REQUEUED)}\n"
        "Orbi re-queued this Issue automatically."
    )))
    outcome = _report_implement_failure()
    assert outcome == "requeued"
    assert "ai-ready" in gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-blocked" not in gh.issues[REQUEUE_ISSUE]["labels"]


def test_the_re_queue_patch_reads_the_issues_real_labels(monkeypatch):
    """The re-queue adds the dispatch label and clears the in-flight one,
    computed from the labels the Issue really carries — the caller's
    snapshot is never the source of a label write (Issue #2426)."""
    gh = _requeue_github(monkeypatch)
    gh.issues[REQUEUE_ISSUE]["labels"][:] = ["ai-in-progress", "bug"]
    outcome = _report_implement_failure()
    assert outcome == "requeued"
    # The whole set is rebuilt in the Issue's own label order and ends on
    # the queue label; the unrelated `bug` survives.
    assert gh.issues[REQUEUE_ISSUE]["labels"] == ["bug", "ai-ready"]


def test_a_failed_history_read_still_re_queues_once(monkeypatch):
    """The counter is read through the GitHub adapter; a failed read keeps
    the re-queue (it must never turn a transient failure into a blocked
    Issue by itself), and the comment it posts is the next read's marker."""
    gh = _requeue_github(monkeypatch)

    reads = []

    def unreadable(command, **kwargs):
        # Only the FIRST comment-history read fails: a plain gh failure is
        # not retried, so it raises at once, and the writes (with their own
        # idempotency read) still go through.
        if tuple(command[1:3]) == ("issue", "view"):
            reads.append(command)
            if len(reads) == 1:
                raise subprocess.CalledProcessError(
                    1, command, stderr="gh: could not read comments",
                )
        return gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", unreadable)
    outcome = _report_implement_failure()
    assert outcome == "requeued"
    assert "ai-ready" in gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-blocked" not in gh.issues[REQUEUE_ISSUE]["labels"]
    assert len(_bot_comments(gh)) == 1


def test_the_re_queue_comment_lands_before_the_queue_label(monkeypatch):
    """The comment IS the counter (Issue #2202), so it is written FIRST: a
    comment write that fails leaves the Issue in flight — the restart-resume
    scan owns it, and the marker this path retries is what makes the next
    identical failure terminal — never `ai-ready` with no marker, which the
    fresh claim scan would claim and the same transient failure would
    re-queue again, without end."""
    gh = _requeue_github(monkeypatch)

    commands: list[tuple[str, ...]] = []

    def comment_write_fails(command, **kwargs):
        commands.append(tuple(command[1:3]))
        # A validation failure is not retried, so the write raises at once.
        if tuple(command[1:3]) == ("issue", "comment"):
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Validation Failed (HTTP 422)",
            )
        return gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", comment_write_fails)
    with pytest.raises(subprocess.CalledProcessError):
        _report_implement_failure()
    # The counter was attempted; no label patch ran behind the lost marker.
    assert ("issue", "comment") in commands
    assert ("issue", "edit") not in commands
    labels = gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-in-progress" in labels
    assert _bot_comments(gh) == []


def test_a_failed_permission_read_still_re_queues_once(monkeypatch):
    """The trust rule's per-author permission read is part of the same GitHub
    the failure came from, so the SAME fail-open rule covers it: a failed read
    keeps the re-queue instead of dropping the whole failure report (no
    re-queue, no terminal comment, the Issue left in flight)."""
    gh = _requeue_github(monkeypatch, ("maintainer", "an ordinary comment"))
    gh.set_permission_failure("maintainer")
    outcome = _report_implement_failure()
    assert outcome == "requeued"
    assert "ai-ready" in gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-blocked" not in gh.issues[REQUEUE_ISSUE]["labels"]
    assert len(_bot_comments(gh)) == 1


def test_provider_quota_stays_terminal_without_a_re_queue(monkeypatch):
    """Acceptance 4 (#698): every other reason is unchanged — an exhausted
    provider quota stops terminal and never re-queues."""
    gh = _requeue_github(monkeypatch)
    publisher = _RecordingPublisher()
    outcome = _report_implement_failure(
        RateLimitExhaustedError("provider quota exhausted, 429"),
        publisher=publisher, finish=True,
    )
    assert outcome == "blocked"
    assert "ai-blocked" in gh.issues[REQUEUE_ISSUE]["labels"]
    assert "ai-ready" not in gh.issues[REQUEUE_ISSUE]["labels"]
    posted = _bot_comments(gh)
    assert len(posted) == 1
    parsed = failure.parse(posted[0])
    assert parsed is not None
    assert parsed.reason_code == "provider_quota"
    assert parsed.outcome == "blocked"
    assert COMMENT_KIND_REQUEUED not in posted[0]


def test_report_delivery_failure_keeps_full_bounded_stderr(monkeypatch):
    posted = _capture_comment(monkeypatch)
    stderr = (
        "pull request create failed: GraphQL: Something went wrong while "
        "executing your query on 2026-09-23T14:13:38Z. Please include "
        "D1F4ED5C in your report."
    )
    error = subprocess.CalledProcessError(1, ["gh", "pr", "create"], stderr=stderr)
    failure_report.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        reason=f"the delivery command failed: {runner.failure_detail(error)}",
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, language_code="en",
    )
    body = posted[0]
    assert stderr in body
    assert "stderr: pull" not in body


def test_report_delivery_failure_unknown_exception_is_unclassified(monkeypatch):
    posted = _capture_comment(monkeypatch)
    outcome = failure_report.report_delivery_failure(
        RuntimeError("totally unexpected"),
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason="The delivery stopped before it could be completed: totally unexpected",
        diagnosis="totally unexpected",
        classify=False, evidence=False, language_code="en",
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
    error = subprocess.CalledProcessError(1, ["pytest"], stderr="3 failed")
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None,
        pr_url="https://github.com/orbi-build/orbi/pull/9",
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason="the tests failed", diagnosis="3 failed",
        evidence=False, language_code="en",
    )
    assert outcome == "fix_needed"
    parsed = failure.parse(posted[0])
    assert parsed is not None
    # Issue #1759: pytest-shaped text no longer decides the reason; the
    # outcome still follows the exception type (recoverable -> fix_needed).
    assert parsed.reason_code == "unclassified"
    assert parsed.action_code == "fix_ticket"
    assert parsed.outcome == "fix_needed"
    assert parsed.retry_safe is False


def _milestone_publisher(run_id="c517c8c7"):
    """A real publisher whose milestone POST bodies are captured.

    Milestones go through the publisher's own `_post_comment`
    (`gh api ... --method POST`), so capturing that command is the real
    milestone rendering, not a stub. Reads (the comment history the
    implement-phase report checks before it decides on the automatic
    re-queue, Issue #2202) are the seam's business, not the publisher's.
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
        1322, "orbi-build/orbi", run_id, run_command=fake_run_command, language_code="en",
    )
    return publisher, milestones


def test_report_delivery_failure_milestone_is_machine_readable(monkeypatch):
    """Acceptance: the live `Orbi: blocked` milestone (the shape
    orbi-build/orbi#1088 shows) carries the same block as the detailed
    comment, the full bounded stderr, and no truncated `stderr: pull`
    line. The Issue already records one automatic re-queue, so the same
    transient failure is terminal (Issue #2202)."""
    posted = _capture_comment(monkeypatch)
    _requeue_github(monkeypatch, ("maintainer", (
        f"{progress.run_marker('d1e2f3a4')}\n"
        f"{comment_kind_marker(COMMENT_KIND_REQUEUED)}\n"
        "Orbi re-queued this Issue automatically."
    )), number=1322, title="t")
    publisher, milestones = _milestone_publisher()
    stderr = (
        "pull request create failed: GraphQL: Something went wrong while "
        "executing your query on 2026-09-23T14:13:38Z. Please include "
        "D1F4ED5C in your report."
    )
    error = subprocess.CalledProcessError(
        1, ["gh", "pr", "create"], stderr=stderr,
    )
    failure_report.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c517c8c7", pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        reason=f"The delivery stopped: {runner.failure_detail(error)}",
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, publisher=publisher, finish=False, language_code="en",
    )
    assert len(milestones) == 1
    milestone = milestones[0]
    assert "Orbi: blocked" in milestone
    # The stderr line is the bounded stderr, not its first word.
    assert stderr in milestone
    assert "- stderr: pull" not in milestone
    record = failure.Failure(
        reason_code="github_transient", action_code="requeue",
        retry_safe=True, outcome="blocked",
    )
    assert failure.parse(milestone) == record
    # The detailed comment carries its own copy of the same record.
    assert failure.parse(posted[0]) == record


def test_report_delivery_failure_fix_needed_milestone_is_machine_readable(
    monkeypatch,
):
    posted = _capture_comment(monkeypatch)
    publisher, milestones = _milestone_publisher()
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [])
    error = subprocess.CalledProcessError(1, ["pytest"], stderr="3 failed")
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 1322, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c517c8c7",
        pr_url="https://github.com/orbi-build/orbi/pull/9",
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason=(
            "The independent review of PR 9 failed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        evidence=False, publisher=publisher, finish=False, language_code="en",
    )
    assert outcome == "fix_needed"
    assert len(milestones) == 1
    milestone = milestones[0]
    assert "Orbi: fix needed" in milestone
    assert failure.parse(milestone) == failure.Failure(
        reason_code="unclassified", action_code="fix_ticket",
        retry_safe=False, outcome="fix_needed",
    )
    assert "- stderr: 3" not in milestone
    assert "3 failed" in milestone


# --------------------------------------------------------------------------
# The Actions-workflow refusal (Issue #1651): all three surfaces agree
# --------------------------------------------------------------------------

class _RecordingPublisher:
    """Records the milestone and the terminal progress body in memory."""

    def __init__(self) -> None:
        self.milestones: list[str] = []
        self.milestone_blocks: list[str | None] = []
        self.finish_bodies: list[str] = []
        self.comment_id = 1

    def milestone(self, body: str, *, block: str | None = None,
                  **kwargs) -> None:
        self.milestones.append(body)
        self.milestone_blocks.append(block)

    def finish(self, body: str) -> None:
        self.finish_bodies.append(body)


def test_report_delivery_failure_workflow_refusal_no_pr_surfaces(monkeypatch):
    """Acceptance #1651: the detailed comment, terminal progress scene and
    short milestone each expose the cause and the no-PR repair path; the
    original refusal stays in collapsed diagnostics; the block identifies a
    rejected credential."""
    posted = _capture_comment(monkeypatch)
    # The finish scene counts the Issue review rounds, so the GitHub
    # read boundary is replaced here: an unstubbed read calls the REAL
    # gh issue view inside the publish bypass — a live network call and
    # an authenticated-gh dependency CI does not have, which left the
    # terminal finish body unpublished there (the red CI scene).
    monkeypatch.setattr(seam, "issue_comments", lambda *a, **k: [])
    publisher = _RecordingPublisher()
    outcome = failure_report.report_delivery_failure(
        workflow_refusal_error(),
        issue={"number": 1651, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c517c8c7", pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason=(
            "The delivery stopped before it could be completed: "
            f"{runner.failure_detail(workflow_refusal_error())}"
        ),
        diagnosis=runner.failure_detail(workflow_refusal_error()),
        classify=False, evidence=False, publisher=publisher, finish=True, language_code="en",
    )
    assert outcome == "blocked"
    record = failure.Failure(
        reason_code="credential_missing", action_code="check_credentials",
        retry_safe=False, outcome="blocked",
    )
    body = posted[0]
    assert failure.parse(body) == record
    assert "**Action:** GitHub rejected the push" in body
    assert "cannot modify Actions workflows" in body
    assert "Workflows read/write" in body
    assert "remove `ai-blocked`" in body
    assert "`ai-ready`" in body
    assert "No PR has been created." in body
    # The refusal is not the visible prose but survives in the collapsed
    # diagnostics, and Git's generic final line did not replace it.
    assert "refusing to allow a GitHub App" in body
    assert body.index("refusing to allow a GitHub App") > body.index("<details>")
    assert body.count("<details>") == 1
    assert len(publisher.milestones) == 1
    milestone = publisher.milestones[0]
    assert "cannot modify Actions workflows" in milestone
    assert "`ai-ready`" in milestone
    assert failure.parse(publisher.milestone_blocks[0]) == record
    assert len(publisher.finish_bodies) == 1
    finish_body = publisher.finish_bodies[0]
    assert "What you need to do: GitHub rejected the push" in finish_body
    assert "cannot modify Actions workflows" in finish_body
    assert "`ai-ready`" in finish_body


def test_report_delivery_failure_workflow_refusal_with_pr_keeps_the_pr(
    monkeypatch,
):
    """Acceptance #1651: an existing delivery PR is preserved and the repair
    path removes `ai-blocked` then uses `ai-fix-needed` on the SAME PR."""
    posted = _capture_comment(monkeypatch)
    url = "https://github.com/orbi-build/orbi/pull/9"
    outcome = failure_report.report_delivery_failure(
        workflow_refusal_error(),
        issue={"number": 1651, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=url,
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason=(
            f"the independent review of PR {url} failed: "
            f"{runner.failure_detail(workflow_refusal_error())}"
        ),
        diagnosis=runner.failure_detail(workflow_refusal_error()),
        evidence=False, language_code="en",
    )
    assert outcome == "blocked"
    body = posted[0]
    assert failure.parse(body) == failure.Failure(
        reason_code="credential_missing", action_code="check_credentials",
        retry_safe=False, outcome="blocked",
    )
    assert url in body
    assert "remove `ai-blocked`" in body
    assert "`ai-fix-needed`" in body
    assert "`ai-ready`" not in body
    assert "No PR has been created." not in body


def test_a_429_stop_is_still_the_provider_quota_stop():
    """Issue #2278 criterion 3: the balance stop's own reason changes nothing
    about the 429 stop (still throttling/quota) or about the Codex usage-limit
    annotation — only the provider's missing-balance refusal moves."""
    assert failure.classify(
        "Error: 429 RESOURCE_EXHAUSTED. Please retry in 36.5s",
    ).reason_code == "provider_quota"
    assert failure.classify(
        "the delivery command failed stderr="
        "Codex error: The usage limit has been reached",
    ).reason_code == "provider_quota"
    # The balance sentence is neither a rate limit nor a transient error.
    assert failure.classify(provider_balance.DETAIL).reason_code == (
        provider_balance.REASON_CODE
    )


def test_a_raw_provider_402_payload_is_the_provider_balance_stop():
    """Issue #2283 acceptance 1: the provider's own refusal payload — the
    shape a path that never passed through `pi_process` still carries — is
    the `provider_balance` stop with its top-up action, not `unclassified`
    and not the credential repair."""
    raw = (
        '{"type":"insufficient_balance_error",'
        '"message":"Insufficient Balance"}'
    )
    record = failure.classify(
        f"Command '['pi']' returned non-zero exit status 146. stderr={raw}",
    )
    assert record == failure.Failure(
        reason_code="provider_balance", action_code="top_up_balance",
        retry_safe=False,
    )


def test_report_delivery_failure_names_the_provider_balance_stop(monkeypatch):
    """Issue #2278 acceptance: a provider 402 `insufficient_balance_error`
    stop is reported as its own reason. The blocked comment says in plain
    words that the account has no balance and what to do next (top up the
    account or switch the key, then relabel), the provider's raw JSON never
    reaches the Reason line, and the visible reason is not the #825 "dead
    loop" sentence. Its record is its own `provider_balance` /
    `top_up_balance` code (Issue #2283)."""
    posted = _capture_comment(monkeypatch)
    error = subprocess.CalledProcessError(
        402, ["pi", "--session", "/home/runner/session"],
        output="", stderr=provider_balance.DETAIL,
    )
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 2278, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="b688ac4a", pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason=(
            "The delivery stopped before it could be completed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, language_code="en",
    )

    assert outcome == "blocked"
    body = posted[0]
    assert failure.parse(body) == failure.Failure(
        reason_code="provider_balance", action_code="top_up_balance",
        retry_safe=False, outcome="blocked",
    )
    reason_line = next(
        line for line in body.splitlines()
        if line.startswith("**Reason:**")
    )
    assert "no balance" in reason_line
    assert "insufficient_balance_error" not in body
    assert "Insufficient Balance" not in body
    assert "dead loop" not in body
    # The next step is a human's: repair the provider account, then relabel.
    assert "**Action:** Top up" in body
    assert "`ai-ready`" in body
    assert "/home/" not in body


def test_a_genuinely_missing_credential_keeps_its_own_record(monkeypatch):
    """Issue #2283 acceptance 2: the provider's balance stop gets its own
    code, and the genuine credential repair is not swallowed by it — a
    missing/rejected key still writes `credential_missing` /
    `check_credentials`."""
    posted = _capture_comment(monkeypatch)
    error = subprocess.CalledProcessError(
        1, ["pi"], output="",
        stderr="Error: No API key found for provider deepseek",
    )
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 2283, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id="c4bc52b3", pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Verify the provider credential and re-run this Issue.",
        reason=(
            "The delivery stopped before it could be completed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, language_code="en",
    )

    assert outcome == "blocked"
    assert failure.parse(posted[0]) == failure.Failure(
        reason_code="credential_missing", action_code="check_credentials",
        retry_safe=False, outcome="blocked",
    )


def test_report_delivery_failure_balance_stop_keeps_the_review_fix_loop(
    monkeypatch,
):
    """Issue #2278: the balance stop is NOT made terminal here (that is
    parked): the review phase still routes it to the recoverable
    `ai-fix-needed` path, and its comment carries the plain cause and the
    account repair instead of the provider payload."""
    posted = _capture_comment(monkeypatch)
    url = "https://github.com/orbi-build/orbi/pull/9"
    error = subprocess.CalledProcessError(
        402, ["pi"], output="", stderr=provider_balance.DETAIL,
    )
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 2278, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=url,
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason=(
            f"the independent review of PR {url} failed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        evidence=False, language_code="en",
    )

    assert outcome == "fix_needed"
    body = posted[0]
    assert failure.parse(body) == failure.Failure(
        reason_code="provider_balance", action_code="top_up_balance",
        retry_safe=False, outcome="fix_needed",
    )
    reason_line = next(
        line for line in body.splitlines()
        if line.startswith("**Reason:**")
    )
    assert "no balance" in reason_line
    assert "insufficient_balance_error" not in body
    assert "**Action:** Top up" in body


@pytest.mark.parametrize(
    "stderr",
    [
        " ! [rejected] HEAD -> b (non-fast-forward)\n"
        "error: failed to push some refs to 'https://github.com/e/r.git'",
        "remote: HTTP 403",
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "The requested URL returned error: 403",
        "fatal: unable to access 'https://github.com/e/r.git/': "
        "Could not resolve host: github.com",
        "totally unexpected",
    ],
)
def test_report_delivery_failure_other_push_errors_keep_generic_action(
    monkeypatch, stderr,
):
    """Acceptance #1651: a non-fast-forward rejection, an unrelated 403, a
    transient network failure and an unrecognized error never receive the
    workflow-permission instructions."""
    posted = _capture_comment(monkeypatch)
    error = subprocess.CalledProcessError(
        1, ["git", "push", "origin", "HEAD:b"], stderr=stderr,
    )
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 1651, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action="Fix the failure described below and re-run this Issue.",
        reason=(
            "The delivery stopped before it could be completed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, language_code="en",
    )
    assert outcome == "blocked"
    body = posted[0]
    assert "**Action:** Fix the failure described below and re-run this " \
        "Issue." in body
    assert "cannot modify Actions workflows" not in body
    assert "Workflows read/write" not in body
    assert "ai-fix-needed" not in body


def test_report_delivery_failure_unknown_error_does_not_blame_the_issue(
    monkeypatch,
):
    """Acceptance #1651: the unrecognized-error explanation says the cause
    could not be determined and never asserts the Issue text is wrong."""
    posted = _capture_comment(monkeypatch)
    error = subprocess.CalledProcessError(
        1, ["git", "push", "origin", "HEAD:b"], stderr="totally unexpected",
    )
    record = failure.classify(runner.failure_detail(error))
    outcome = failure_report.report_delivery_failure(
        error,
        issue={"number": 1651, "title": "t", "labels": []},
        source_repo="orbi-build/orbi", run_id=None, pr_url=None,
        worktree=None, branch="b", role=runner.ROLE_IMPLEMENT,
        action=failure.terminal_action(record, language_code="en"),
        reason=(
            "The delivery stopped before it could be completed: "
            f"{runner.failure_detail(error)}"
        ),
        diagnosis=runner.failure_detail(error),
        classify=False, evidence=False, language_code="en",
    )
    assert outcome == "blocked"
    body = posted[0]
    assert failure.parse(body).reason_code == "unclassified"
    assert "could not be determined" in body
    assert "the Issue text" not in body
    assert "reword" not in body
    assert "Workflows read/write" not in body


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


def test_public_contracts_name_the_provider_balance_stop_apart():
    """Issue #2283 acceptance 3: a consumer that renders by `reason_code`
    reads the public contract — `provider_balance` is its own stop, told
    apart from the missing-credential repair, in both languages."""
    for relative in (
        "docs/public-contracts.mdx", "docs/zh/public-contracts.mdx",
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "orbi:failure:v1" in text, relative
        assert "provider_balance" in text, relative
        assert "top_up_balance" in text, relative
        assert "credential_missing" in text, relative


# --------------------------------------------------------------------------
# Issue #1760: the GitHub response status decides, not the error text
# --------------------------------------------------------------------------

def github_failed(status, *, remaining=None, stderr="gh: failed"):
    """A failed gh request carrying the response status it received."""
    from orbi import gh_response

    return gh_response.GitHubRequestFailed(
        1, ["gh", "api", "repos/o/r"], status=status,
        ratelimit_remaining=remaining, stderr=stderr,
    )


@pytest.mark.parametrize("status, remaining, action_code, retry_safe", [
    (401, None, "check_credentials", False),
    (429, 4999, "requeue", True),
    (502, 4999, "requeue", True),
    (403, 0, "requeue", True),          # the primary rate limit
    (403, 4999, "check_credentials", False),   # a permission 403
])
def test_classify_reads_the_github_response_status(
    status, remaining, action_code, retry_safe,
):
    """Acceptance #1: 401/429/502/403-with-no-quota/403-with-quota are told
    apart by the response status alone — the detail text below contains
    none of the words the old regexes looked for."""
    record = failure.classify(
        "Command returned non-zero exit status 1.",
        error=github_failed(status, remaining=remaining),
    )
    assert record.action_code == action_code
    assert record.retry_safe is retry_safe
    assert record.reason_code == (
        "credential_missing" if action_code == "check_credentials"
        else "github_transient"
    )


def test_classify_status_wins_over_the_error_text():
    """Acceptance #2: a permission 403 whose text happens to mention a rate
    limit or a timeout is still a permission failure; a 5xx whose text
    mentions a credential is still transient."""
    permission = failure.classify(
        "API rate limit exceeded; request timed out; bad credentials",
        error=github_failed(403, remaining=4100),
    )
    assert permission.reason_code == "credential_missing"
    transient = failure.classify(
        "credential forbidden unauthorized",
        error=github_failed(503),
    )
    assert transient.reason_code == "github_transient"


def test_classify_ignores_argv_words_of_a_non_github_failure():
    """Acceptance #2: the command's argv is part of the exception detail, so
    a branch named `forbidden` or `credential` must not relabel a Git
    failure as a credential problem."""
    exc = subprocess.CalledProcessError(
        128,
        ["git", "push", "origin",
         "orbi/forbidden-credential-repair", "forbidden"],
        stderr="fatal: the remote end hung up unexpectedly",
    )
    record = runner._classify_failure(exc, outcome="blocked")
    assert record.reason_code != "credential_missing"


def test_classify_reads_a_timeout_by_its_type():
    """Acceptance #4: a subprocess timeout is transient by type, whatever
    its message says."""
    timeout = subprocess.TimeoutExpired(
        ["gh", "api", "repos/o/r"], 30,
        output="", stderr="no marker in this text",
    )
    record = failure.classify("no marker in this text", error=timeout)
    assert record.reason_code == "github_transient"
    assert record.action_code == "requeue"
    assert record.retry_safe is True


def test_classify_does_not_read_the_word_timeout_without_a_source():
    """A detail that merely SAYS "timed out" is not a transport timeout: the
    type (or the text-only source that produced the detail) decides
    (Issue #1760)."""
    record = failure.classify("the release gate note says timed out")
    assert record.reason_code != "github_transient"


def test_classify_keeps_the_text_timeout_of_a_text_only_source():
    """A Pi session and a Git fetch/push report a timeout in their own
    words — no status, no timeout type — so their wording is the one
    signal there is (the same text-only carve-out the provider reasons
    keep)."""
    git = subprocess.CalledProcessError(
        128, ["git", "fetch", "origin", "main"],
        stderr="fatal: unable to access 'https://github.com/o/r.git/': "
               "Connection timed out",
    )
    assert failure.classify(
        runner.failure_detail(git), error=git,
    ).reason_code == "github_transient"
    pi = subprocess.CalledProcessError(
        1, ["pi"], stderr="request timed out after 30s",
    )
    assert failure.classify(
        runner.failure_detail(pi), error=pi,
    ).reason_code == "github_transient"


def test_classify_does_not_read_a_github_command_timeout_word():
    """A GitHub request's timeout never comes from the wording: a gh
    subcommand whose text says "timed out" is not transient by that word
    (Issue #1760)."""
    gh = subprocess.CalledProcessError(
        1, ["gh", "issue", "list", "--repo", "o/r"],
        stderr="request timed out",
    )
    assert failure.classify(
        runner.failure_detail(gh), error=gh,
    ).reason_code != "github_transient"


def test_classify_a_release_gate_wait_timeout_is_a_human_decision():
    """Acceptance #3: the release gate's wait budget running out is a
    terminal, human-decided stop — never the requeue a transient GitHub
    failure gets."""
    record = failure.classify(
        "release gate: waiting for CI on the release commit abc123 timed "
        "out after 7s (still pending: check 'tests')",
        error=failure.ReleaseGateWaitTimeout("wait timeout"),
    )
    assert record == failure.Failure(
        reason_code="human_decision_required", action_code="decide",
        retry_safe=False, outcome="blocked",
    )


def test_classify_a_wrapped_permission_403_keeps_its_repair():
    """A 403 with quota left that a caller had to re-word keeps the
    `check_credentials` repair on its TYPE: the release gate names the
    missing Checks:read scope in its own sentence, and the removed
    `credential` word no longer has to carry the meaning (Issue #1760)."""
    message = (
        "CI gate could not be evaluated: the credential lacks "
        "Checks:read permission (GitHub returned HTTP 403 while listing "
        "check runs)"
    )
    assert failure.classify(
        message, error=failure.GitHubPermissionDenied(message),
    ) == failure.Failure(
        reason_code="credential_missing", action_code="check_credentials",
        retry_safe=False, outcome="blocked",
    )


def test_release_failure_comment_classifies_a_gate_wait_timeout_as_blocked():
    """The release comment's block is the one a status reader parses: a
    gate wait timeout must not claim `github_transient` / `requeue`."""
    from orbi import release_comments
    from orbi.run_fields import RunFields

    body = release_comments.release_failure_comment_body(
        "a1b2c3d4", RunFields(run_id="a1b2c3d4", priority="normal"),
        failure.ReleaseGateWaitTimeout(
            "release gate: waiting for CI on the release commit abc123 "
            "timed out after 7s",
        ),
        "en", step="CI gate",
    )
    record = failure.parse(body)
    assert record is not None
    assert record.reason_code == "human_decision_required"
    assert record.action_code == "decide"


# --------------------------------------------------------------------------
# Issue #1768: the three #1762 review regressions keep their classification
# --------------------------------------------------------------------------

def test_pi_no_api_key_found_is_a_credential_problem():
    """The provider's own missing-key sentence is `credential_missing`: the
    marker `PROVIDER_AUTH_WORDS` still lists was dropped from the text
    classifier by #1762 and comes back here (Issue #1768).

    The marker cannot be forged by a command's argv: it contains a space,
    so no branch, label or check name can carry it (the #1760 concern)."""
    result = failure.classify(
        "Pi session failed: No API key found for provider deepseek")
    assert result.reason_code == "credential_missing"
    assert result.action_code == "check_credentials"
    assert result.retry_safe is False


def test_gh_tls_handshake_timeout_is_transient():
    """gh's own transport can fail before any response exists: a TLS
    handshake timeout exits 1 with no status and no `TimeoutExpired`
    (cli/cli#1761), so the wording is the only signal there is
    (Issue #1768). A response status, when present, still wins."""
    from orbi import gh_response

    error = gh_response.GitHubRequestFailed(
        1, ["gh", "api", "repos/o/r/issues"],
        stderr='Post "https://api.github.com/graphql": net/http: '
               "TLS handshake timeout",
        status=None,
    )
    result = failure.classify(
        "gh api repos/o/r/issues failed: net/http: TLS handshake timeout",
        error=error,
    )
    assert result.reason_code == "github_transient"
    assert result.action_code == "requeue"
    assert result.retry_safe is True
