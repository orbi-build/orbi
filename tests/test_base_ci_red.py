"""The `base_ci_red` scene (Issue #1825).

A delivery PR that is ready (review passed, tests pass) but whose failing
check is ALSO failing on the base commit used to end in `ai-blocked` with
`human_decision_required`, one line of prose, empty evidence and no way
back without a human relabel. This module tests the four user-facing
outcomes:

- the failure record (`reason_code=base_ci_red`, `action_code=fix_ticket`,
  `retry_safe=false`) and its hidden `orbi:base-ci-red:v1` block;
- the ONE readable comment (PR ready, why it paused, every failing base
  check with its run link and first error lines, next steps);
- `/orbi fix-base` (write permission, one execution per comment, reuse of
  an OPEN triage Issue, `ai-ready`+`p0`+Milestone on a new one);
- the automatic recovery (base green -> `ai-fix-needed`, 7-day reminder ->
  `stale`, merge -> the same comment says merged).
"""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

import orbi.base_ci_red as base_ci_red
import orbi.runner as runner
from orbi import failure, failure_report, ticket_command
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker
from orbi.progress import run_marker
from tests.fakes.github import FakeGh
from seam import seam

REPO = "acme/app"
BASE_BRANCH = "release/7"
BASE_SHA = "abc1234def"
PR_NUMBER = 123
CHECK = "backend — build, vet, test"

RUN_URL = (
    "https://github.com/acme/app/actions/runs/4567/job/8910"
)
ERROR_LINE = (
    "./internal/svc/handler.go:42:9: *Server does not implement "
    "Service (missing method Foo)"
)


def record(**overrides) -> base_ci_red.BaseCIRed:
    values = dict(
        state=base_ci_red.STATE_WAITING,
        base_branch=BASE_BRANCH,
        base_sha=BASE_SHA,
        pr=PR_NUMBER,
        failing_checks=(CHECK,),
    )
    values.update(overrides)
    return base_ci_red.BaseCIRed(**values)


def base_run(name=CHECK, *, url=RUN_URL, errors=(ERROR_LINE,)):
    """One failed base check run as the check-runs API returns it."""
    output: dict = {}
    if errors is not None:
        output = {"summary": "1 failing check", "text": "\n".join(errors)}
    return {
        "id": 8910, "name": name, "status": "completed",
        "conclusion": "failure", "html_url": url,
        "output": output,
    }


# --------------------------------------------------------------------------
# The record: render + parse (the hidden block is a contract)
# --------------------------------------------------------------------------

def test_the_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        record().state = base_ci_red.STATE_MERGED


def test_the_state_set_is_closed():
    assert base_ci_red.STATES == {
        "waiting", "resuming", "merged", "stale",
    }


def test_render_is_one_hidden_block_and_matches_the_documented_example():
    block = base_ci_red.render(record())
    assert block == (
        '<!-- orbi:base-ci-red:v1 {"schema":1,"state":"waiting",'
        '"base_branch":"release/7","base_sha":"abc1234def","pr":123,'
        '"failing_checks":["backend — build, vet, test"]} -->'
    )


def test_parse_round_trips_a_rendered_block():
    assert base_ci_red.parse(base_ci_red.render(record())) == record()
    merged = record(state=base_ci_red.STATE_MERGED,
                    failing_checks=(CHECK, "lint"))
    assert base_ci_red.parse(base_ci_red.render(merged)) == merged


def test_parse_returns_none_without_a_block():
    assert base_ci_red.parse("") is None
    assert base_ci_red.parse("just prose") is None
    assert base_ci_red.parse(None) is None


def test_parse_fails_fast_on_a_corrupted_block():
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.parse("<!-- orbi:base-ci-red:v1 not-json -->")
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.parse(
            '<!-- orbi:base-ci-red:v1 '
            '{"schema":1,"state":"waiting","base_branch":"main",'
            '"base_sha":"a","pr":1} -->'
        )
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.parse(base_ci_red.render(record(state="paused")))


def test_render_rejects_an_unknown_state():
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.render(record(state="paused"))


def test_with_state_keeps_every_other_field():
    merged = base_ci_red.with_state(record(), base_ci_red.STATE_MERGED)
    assert merged.state == "merged"
    assert merged.failing_checks == record().failing_checks
    assert merged.pr == PR_NUMBER


# --------------------------------------------------------------------------
# The evidence: every failing base check, its run link and error lines
# --------------------------------------------------------------------------

def test_failing_checks_reports_every_named_failure(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, [
        base_run(),
        base_run("lint", url="https://github.com/acme/app/actions/runs/1"),
        {"id": 1, "name": "docs", "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
    ])
    monkeypatch.setattr(seam, "run_command", gh)
    checks = base_ci_red.failing_checks(
        REPO, [CHECK, "lint", "missing"], BASE_SHA,
    )
    assert [check.name for check in checks] == [CHECK, "lint"]
    assert checks[0].url == RUN_URL
    assert checks[0].errors == (ERROR_LINE,)


def test_failing_checks_omits_missing_output_and_redacts_host_paths(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, [
        base_run("no-output", errors=None),
        base_run("host-path",
                 errors=("/home/someone/repo/x_test.go:3: boom",)),
    ])
    monkeypatch.setattr(seam, "run_command", gh)
    checks = base_ci_red.failing_checks(
        REPO, ["no-output", "host-path"], BASE_SHA,
    )
    assert checks[0].errors == ()
    assert checks[1].errors == ("local runner path boom",)


def test_failing_checks_bounds_the_error_lines(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, [
        base_run(errors=tuple(f"line {index}" for index in range(40))),
    ])
    monkeypatch.setattr(seam, "run_command", gh)
    checks = base_ci_red.failing_checks(REPO, [CHECK], BASE_SHA)
    assert len(checks[0].errors) == base_ci_red.MAX_ERROR_LINES


# --------------------------------------------------------------------------
# The raise site: the record is decided at the source
# --------------------------------------------------------------------------

def _raise_gh(monkeypatch, *, base_runs, open_issues=()):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, list(base_runs))
    for number, title in enumerate(open_issues, start=900):
        gh.add_issue(number, title=title)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def test_the_raise_carries_the_record_and_every_failing_check(monkeypatch):
    _raise_gh(monkeypatch, base_runs=[
        base_run(), base_run("lint",
                             url="https://github.com/acme/app/actions/runs/1"),
    ])
    with pytest.raises(failure_report.PreExistingCIFailure) as caught:
        failure_report._raise_if_preexisting_ci_failure(
            REPO, [CHECK, "lint"], BASE_SHA,
            base_branch=BASE_BRANCH, pr_number=PR_NUMBER,
        )
    error = caught.value
    assert error.base_ci_red == record(failing_checks=(CHECK, "lint"))
    assert [check.name for check in error.checks] == [CHECK, "lint"]
    assert error.failure_record == failure.Failure(
        reason_code="base_ci_red", action_code="fix_ticket",
        retry_safe=False, outcome="blocked",
    )
    # The configured base branch travels in the fail-fast message: no
    # hard-coded `main`.
    assert BASE_BRANCH in str(error)
    assert "main" not in str(error)


def test_no_raise_when_the_base_check_concluded_successfully(monkeypatch):
    _raise_gh(monkeypatch, base_runs=[
        {"id": 1, "name": CHECK, "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
    ])
    failure_report._raise_if_preexisting_ci_failure(
        REPO, [CHECK], BASE_SHA,
        base_branch=BASE_BRANCH, pr_number=PR_NUMBER,
    )


def test_no_raise_without_a_base_commit(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "unused")
    failure_report._raise_if_preexisting_ci_failure(
        REPO, [CHECK], None, base_branch=BASE_BRANCH, pr_number=PR_NUMBER,
    )


def test_the_raise_names_an_existing_open_triage_issue(monkeypatch):
    _raise_gh(monkeypatch, base_runs=[base_run()], open_issues=(
        f"{base_ci_red.triage_title(CHECK, BASE_BRANCH)} (push)",
    ))
    with pytest.raises(failure_report.PreExistingCIFailure) as caught:
        failure_report._raise_if_preexisting_ci_failure(
            REPO, [CHECK], BASE_SHA,
            base_branch=BASE_BRANCH, pr_number=PR_NUMBER,
        )
    assert "/issues/900" in str(caught.value)


def test_the_triage_lookup_never_reuses_a_closed_issue(monkeypatch):
    gh = FakeGh(REPO)
    title = base_ci_red.triage_title(CHECK, BASE_BRANCH)
    gh.add_issue(900, title=title, state="closed")
    monkeypatch.setattr(seam, "run_command", gh)
    assert base_ci_red.triage_url(REPO, CHECK, BASE_BRANCH) is None
    assert base_ci_red.open_triage_issue(REPO, CHECK, BASE_BRANCH) is None


def test_the_triage_lookup_is_best_effort(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "{}")
    assert base_ci_red.triage_url(REPO, CHECK, BASE_BRANCH) is None
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "not json")
    assert base_ci_red.triage_url(REPO, CHECK, BASE_BRANCH) is None


# --------------------------------------------------------------------------
# The comment: what the user reads
# --------------------------------------------------------------------------

def _reported_body(monkeypatch, error, *, language_code="en",
                   pr_url="https://github.com/acme/app/pull/123"):
    posted: list[str] = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *a, **k: {"ai-pr-opened"})
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    outcome = failure_report.report_delivery_failure(
        error, issue={"number": 1825, "title": "t", "labels": []},
        source_repo=REPO, run_id="096197d2", pr_url=pr_url,
        worktree=None, branch="b", role=runner.ROLE_REVIEW,
        reason="whatever the caller said", diagnosis="whatever",
        evidence=True, language_code=language_code,
    )
    assert outcome == "blocked"
    return posted[0]


def _preexisting_error(monkeypatch, *, base_runs=None, checks=None):
    _raise_gh(monkeypatch, base_runs=base_runs or [base_run()])
    try:
        failure_report._raise_if_preexisting_ci_failure(
            REPO, checks or [CHECK], BASE_SHA,
            base_branch=BASE_BRANCH, pr_number=PR_NUMBER,
        )
    except failure_report.PreExistingCIFailure as exc:
        return exc
    raise AssertionError("expected PreExistingCIFailure")


def test_the_comment_reads_ready_then_paused_then_evidence_then_steps(
        monkeypatch):
    error = _preexisting_error(monkeypatch)
    body = _reported_body(monkeypatch, error)
    lines = body.splitlines()
    assert lines[0] == run_marker("096197d2")
    assert lines[1] == comment_kind_marker(COMMENT_KIND_FAILURE)
    assert failure.parse(body) == failure.Failure(
        reason_code="base_ci_red", action_code="fix_ticket",
        retry_safe=False, outcome="blocked",
    )
    # The failure block must be the FIRST line of the readable body: the
    # resume path reads it there.
    assert lines[2].startswith("<!-- orbi:failure:v1 ")
    assert "PR #123 is ready" in body
    assert f"base branch `{BASE_BRANCH}`" in body
    assert f"**`{CHECK}`**" in body
    assert RUN_URL in body
    assert ERROR_LINE in body
    assert "Next steps" in body
    assert "/orbi fix-base" in body
    # The base_ci_red block is parseable and matches the record.
    assert base_ci_red.parse(body) == record()
    # No empty evidence noise: this is a non-subprocess failure.
    assert "<empty>" not in body
    assert "exit_code=" not in body
    assert "stderr_tail" not in body


def test_the_comment_lists_every_failing_check(monkeypatch):
    error = _preexisting_error(
        monkeypatch,
        base_runs=[base_run(), base_run(
            "lint", url="https://github.com/acme/app/actions/runs/1",
            errors=("lint error",),
        )],
        checks=[CHECK, "lint"],
    )
    body = _reported_body(monkeypatch, error)
    assert f"**`{CHECK}`**" in body
    assert "**`lint`**" in body
    assert "https://github.com/acme/app/actions/runs/1" in body
    assert "lint error" in body


def test_the_comment_speaks_the_run_message_language(monkeypatch):
    error = _preexisting_error(monkeypatch)
    body = _reported_body(monkeypatch, error, language_code="zh")
    assert "已就绪" in body
    assert "下一步" in body
    # Machine fields stay verbatim in both languages.
    assert base_ci_red.parse(body) == record()
    assert CHECK in body


def test_a_plain_preexisting_failure_still_reports_the_generic_block(
        monkeypatch):
    """A raise site that carries no evidence (a caller-built exception)
    keeps the pre-#1825 classified comment — never a half-filled one."""
    posted = []
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(seam, "issue_labels", lambda *a, **k: set())
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    error = failure_report.PreExistingCIFailure("main is already red")
    failure_report.report_delivery_failure(
        error, issue={"number": 1825, "title": "t", "labels": []},
        source_repo=REPO, run_id=None, pr_url=None, worktree=None,
        branch="b", role=runner.ROLE_IMPLEMENT, language_code="en",
    )
    assert failure.parse(posted[0]).reason_code == "base_ci_red"
    assert base_ci_red.parse(posted[0]) is None


# --------------------------------------------------------------------------
# /orbi fix-base
# --------------------------------------------------------------------------

def _command_fixture(monkeypatch, *, permission="write"):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-blocked",))
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    gh.add_collaborator("maintainer", permission)
    gh.add_collaborator("stranger", "read")
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def _run_command(gh, issue_number=1825, *, milestone=None):
    base_ci_red.recover(
        REPO, language_code="en", milestone=milestone,
    )


def _blocks_issue(gh, number=1825, *, state="waiting", checks=(CHECK,)):
    gh.comment(number, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "⛔ paused",
        base_ci_red.render(record(state=state, failing_checks=checks)),
    ]))
    # The block is FRESH: the fake's clock is a fixed past date, and the
    # 7-day reminder must not fire on a comment that was just written.
    gh.issues[number]["comments"][-1]["createdAt"] = datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fix_base_opens_an_ai_ready_p0_issue_in_the_active_milestone(
        monkeypatch):
    gh = _command_fixture(monkeypatch)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    gh.add_check_runs("head", [])
    gh.add_milestone(7, title="v0.7.15")
    gh.issues[1825]["milestone"] = 7
    _run_command(gh, milestone="v0.7.15")
    titles = {
        issue["title"]: issue
        for number, issue in gh.issues.items() if number != 1825
    }
    created = titles[base_ci_red.triage_title(CHECK, BASE_BRANCH)]
    assert set(created["labels"]) == {"ai-ready", "p0"}
    assert created["milestone"] == 7
    assert RUN_URL in created["body"]
    assert ERROR_LINE in created["body"]
    replies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("created #" in body for body in replies), replies


def test_fix_base_reuses_an_open_triage_issue_and_reports_reused(monkeypatch):
    gh = _command_fixture(monkeypatch)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    gh.add_issue(
        950, title=base_ci_red.triage_title(CHECK, BASE_BRANCH) + " (push)",
        labels=("ai-ready",),
    )
    _run_command(gh)
    assert 950 in gh.issues and len(gh.issues) == 2
    replies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("reused #950" in body for body in replies), replies


@pytest.mark.parametrize("permission", ["none", "read"])
def test_fix_base_ignores_callers_without_write_permission(
        monkeypatch, permission):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-blocked",))
    gh.comment(1825, "/orbi fix-base", login="stranger")
    gh.add_collaborator("stranger", permission)
    monkeypatch.setattr(seam, "run_command", gh)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("permission" in body for body in bodies), bodies


@pytest.mark.parametrize("permission", ["write", "admin"])
def test_fix_base_runs_once_per_comment(monkeypatch, permission):
    gh = _command_fixture(monkeypatch, permission=permission)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    created = [number for number in gh.issues if number != 1825]
    assert len(created) == 1
    _run_command(gh)
    replies = [
        comment["body"] for comment in gh.issues[1825]["comments"]
        if "created #" in comment["body"]
    ]
    assert len(replies) == 1


def test_fix_base_never_triggers_on_the_runners_own_comment(monkeypatch):
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    # The runner's own output carries a hidden marker: the bot must never
    # trigger itself.
    gh.comment(1825, "<!-- orbi:run=096197d2 -->\n/orbi fix-base",
               login="maintainer")
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    assert len(gh.issues) == 1


# --------------------------------------------------------------------------
# The automatic recovery scan
# --------------------------------------------------------------------------

def _scan_gh(monkeypatch, *, base_runs, pr_state="OPEN", comments=None,
             labels=("ai-blocked",)):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=labels)
    _blocks_issue(gh, number=1825, **({} if comments is None
                                      else {"state": comments}))
    gh.add_pr(PR_NUMBER, head="orbi/x", base=BASE_BRANCH, state=pr_state)
    gh.prs[PR_NUMBER]["baseRefOid"] = BASE_SHA
    gh.add_check_runs(BASE_SHA, list(base_runs))
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def _block_comment(gh, number=1825):
    """The comment carrying the base_ci_red block (the scan edits THAT one)."""
    return [
        comment for comment in gh.issues[number]["comments"]
        if "orbi:base-ci-red:v1" in comment["body"]
    ][-1]


def test_the_scan_recovers_a_green_base_without_a_human(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[
        {"id": 1, "name": CHECK, "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
    ])
    recovered = base_ci_red.recover(REPO, language_code="en")
    assert recovered == [1825]
    labels = gh.issues[1825]["labels"]
    assert "ai-fix-needed" in labels
    assert "ai-blocked" not in labels
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "resuming"


def test_the_scan_leaves_a_still_red_base_alone(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "waiting"


def test_the_scan_treats_a_pending_base_check_as_not_green(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[
        {"id": 1, "name": CHECK, "status": "in_progress",
         "conclusion": None, "html_url": "", "output": {}},
    ])
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]


def test_the_scan_only_picks_a_waiting_block(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[
        {"id": 1, "name": CHECK, "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
    ], comments="stale")
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]


def _age_comment(gh, days):
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    gh.issues[1825]["comments"][-1]["createdAt"] = (
        stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def test_seven_days_red_posts_one_reminder_and_stops_polling(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    _age_comment(gh, 8)
    before = len(gh.issues[1825]["comments"])
    assert base_ci_red.recover(REPO, language_code="en") == []
    comments = gh.issues[1825]["comments"]
    assert len(comments) == before + 1
    assert "still failing" in comments[-1]["body"]
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "stale"
    # ... and the scan skips it from now on.
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert len(gh.issues[1825]["comments"]) == before + 1


def test_a_merged_pr_updates_the_same_comment_to_merged(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()], pr_state="MERGED")
    assert base_ci_red.recover(REPO, language_code="en") == []
    block = _block_comment(gh)["body"]
    assert base_ci_red.parse(block).state == "merged"
    assert "Merged" in block


def test_the_merge_path_notes_the_merged_state(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    assert base_ci_red.note_merged(REPO, 1825, language_code="en") is True
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "merged"
    # Idempotent: a second call changes nothing and reports no change.
    assert base_ci_red.note_merged(REPO, 1825, language_code="en") is False


def test_rewrite_state_appends_the_state_once_and_keeps_the_block():
    body = "\n".join([
        run_marker("096197d2"),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "⛔ paused",
        base_ci_red.render(record()),
    ])
    resuming = base_ci_red.rewrite_state(body, "resuming", language_code="en")
    assert base_ci_red.parse(resuming).state == "resuming"
    assert resuming.count("green again") == 1
    merged = base_ci_red.rewrite_state(resuming, "merged", language_code="en")
    assert base_ci_red.parse(merged).state == "merged"
    assert merged.count("Merged") == 1
    assert "green again" in merged
    assert merged.count("orbi:base-ci-red:v1") == 1


# --------------------------------------------------------------------------
# The docs contract
# --------------------------------------------------------------------------

def test_the_reason_code_is_documented_in_both_languages():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in ("docs/workflow.mdx", "docs/zh/workflow.mdx"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "base_ci_red" in text, relative
        assert "/orbi fix-base" in text, relative


# --------------------------------------------------------------------------
# The corrupted/misshapen inputs: fail fast, never a half-read record
# --------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"state": "paused"},
    {"schema": 2},
    {"pr": "123"},
    {"pr": True},
    {"base_branch": ""},
    {"base_sha": 42},
    {"failing_checks": [CHECK]},
    {"failing_checks": ("",)},
])
def test_render_rejects_a_misshapen_record(overrides):
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.render(record(**overrides))


@pytest.mark.parametrize("payload", [
    "not-json",
    "[]",
    '{"schema":1,"state":"waiting","base_branch":"main","base_sha":"a",'
    '"pr":1,"failing_checks":[],"extra":true}',
    '{"schema":1,"state":"waiting","base_branch":"main","pr":1}',
    '{"schema":1,"state":"waiting","base_branch":"main","base_sha":"a",'
    '"pr":1,"failing_checks":"a"}',
])
def test_parse_fails_fast_on_every_malformed_shape(payload):
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.parse(f"<!-- orbi:base-ci-red:v1 {payload} -->")


def test_parse_rejects_two_blocks_in_one_body():
    body = base_ci_red.render(record()) + base_ci_red.render(record())
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.parse(body)


def test_rewrite_state_refuses_a_body_without_a_block():
    with pytest.raises(base_ci_red.BaseCIRedError):
        base_ci_red.rewrite_state("no block here", "merged", language_code="en")


def test_a_summary_only_check_still_carries_its_error_line(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, [{
        "name": CHECK, "status": "completed", "conclusion": "failure",
        "html_url": None, "output": {"summary": "the build broke"},
    }])
    monkeypatch.setattr(seam, "run_command", gh)
    checks = base_ci_red.failing_checks(REPO, [CHECK], BASE_SHA)
    assert checks[0].errors == ("the build broke",)
    # GitHub gave no run link: the comment omits the segment.
    assert checks[0].url is None


def test_a_missing_run_link_and_errors_are_omitted_from_both_bodies(monkeypatch):
    bare = base_ci_red.FailingCheck(CHECK)
    body = base_ci_red.comment_body(
        record(), [bare],
        failure_record=failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked"),
        language_code="en",
    )
    assert CHECK in body
    assert "failing run" not in body
    assert "first error lines" not in body
    triage = base_ci_red._triage_body(bare, record(), "en")
    assert "failing run" not in triage
    assert "First error lines" not in triage


def test_a_pending_or_duplicate_base_run_is_not_a_failure(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_check_runs(BASE_SHA, [
        {"name": CHECK, "status": "in_progress", "conclusion": None,
         "html_url": "", "output": {}},
        base_run(),
    ])
    monkeypatch.setattr(seam, "run_command", gh)
    checks = base_ci_red.failing_checks(REPO, [CHECK], BASE_SHA)
    assert [check.name for check in checks] == [CHECK]


def test_the_rest_id_falls_back_to_the_permalink():
    assert base_ci_red._rest_comment_id({"id": 41}) == 41
    assert base_ci_red._rest_comment_id(
        {"url": "https://github.com/a/b/issues/1#issuecomment-42"}) == 42
    assert base_ci_red._rest_comment_id({"url": "https://github.com/a/b"}) is None


def test_a_comment_without_a_rest_id_is_reported_and_skipped(monkeypatch):
    posted = []
    monkeypatch.setattr(seam, "update_issue_comment",
                        lambda *a, **k: posted.append(k))
    assert base_ci_red._edit_state(
        REPO, 1, record(), {"body": base_ci_red.render(record())},
        "merged", language_code="en",
    ) is False
    assert base_ci_red._edit_state(
        REPO, 1, record(), {"id": 7}, "merged", language_code="en",
    ) is False
    assert posted == []


def test_a_corrupted_block_in_an_older_comment_is_skipped(monkeypatch):
    """One unreadable block never hides the newest valid one, and a
    ticket with only a corrupted block is simply not picked up."""
    comments = [
        {"body": "<!-- orbi:base-ci-red:v1 broken -->", "url": "u1"},
        {"body": base_ci_red.render(record()), "url": "u2"},
    ]
    assert base_ci_red._located(comments) == (record(), comments[1])
    assert base_ci_red._located([comments[0]]) is None


def test_the_triage_url_omits_a_record_without_a_url(monkeypatch):
    monkeypatch.setattr(seam, "list_issues", lambda *a, **k: [])
    assert base_ci_red.triage_url(REPO, CHECK, BASE_BRANCH) is None
    monkeypatch.setattr(
        seam, "run_command",
        lambda *a, **k: json.dumps([{"number": 5, "title": "x"}]),
    )


def test_a_garbage_issue_create_answer_fails_the_command(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "not-a-url")
    with pytest.raises(ticket_command.TicketCommandError):
        base_ci_red._create_triage_issue(
            REPO, base_ci_red.FailingCheck(CHECK), record(),
            milestone=None, dispatch_label="ai-ready", language_code="en",
        )


def test_an_unknown_argument_is_rejected_with_the_usage(monkeypatch):
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi retry", login="maintainer")
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("/orbi fix-base" in body for body in bodies), bodies


def test_fix_base_without_check_evidence_replies_instead_of_inventing_one(
        monkeypatch):
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    _blocks_issue(gh)
    # The block names a check the base commit no longer reports.
    gh.add_check_runs(BASE_SHA, [])
    _run_command(gh)
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("No failing check evidence" in body for body in bodies), bodies


def test_a_pr_without_a_base_head_leaves_the_issue_alone(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    gh.prs[PR_NUMBER]["baseRefOid"] = ""
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]


def test_an_issue_without_the_block_is_left_alone(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-blocked",))
    gh.comment(1825, "just a human comment")
    monkeypatch.setattr(seam, "run_command", gh)
    assert base_ci_red.recover(REPO, language_code="en") == []


def test_a_comment_without_a_timestamp_never_turns_stale(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    _block_comment = [
        comment for comment in gh.issues[1825]["comments"]
        if "orbi:base-ci-red:v1" in comment["body"]
    ][-1]
    _block_comment["createdAt"] = "not a timestamp"
    before = len(gh.issues[1825]["comments"])
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert len(gh.issues[1825]["comments"]) == before


def test_post_merged_swallows_a_failed_status_edit(monkeypatch):
    """The merge already landed: a failed comment edit is a bypass log."""
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-merged",))
    monkeypatch.setattr(seam, "run_command", gh)

    def boom(*_args, **_kwargs):
        raise RuntimeError("gh down")

    monkeypatch.setattr(seam, "issue_comments", boom)
    base_ci_red.post_merged(1825, REPO, "merged body", language_code="en")
    assert "merged body" in gh.issues[1825]["comments"][-1]["body"]


def test_note_merged_reports_no_change_without_a_rest_id(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-merged",))
    gh.comment(1825, base_ci_red.render(record()))
    gh.issues[1825]["comments"][-1].pop("id")
    gh.issues[1825]["comments"][-1].pop("url")
    monkeypatch.setattr(seam, "run_command", gh)
    assert base_ci_red.note_merged(REPO, 1825, language_code="en") is False



def test_level_triage_lookup_skips_a_non_matching_search_hit(monkeypatch):
    monkeypatch.setattr(
        seam, "list_issues",
        lambda *a, **k: [
            {"number": 1, "title": "some other issue", "url": "u1"},
            {"number": 2, "title": base_ci_red.triage_title(CHECK, BASE_BRANCH),
             "url": "u2"},
        ],
    )
    assert base_ci_red.open_triage_issue(REPO, CHECK, BASE_BRANCH) == {
        "number": 2, "title": base_ci_red.triage_title(CHECK, BASE_BRANCH),
        "url": "u2",
    }


def test_rewriting_the_same_state_twice_keeps_one_line():
    body = base_ci_red.render(record())
    merged = base_ci_red.rewrite_state(body, "merged", language_code="en")
    again = base_ci_red.rewrite_state(merged, "merged", language_code="en")
    assert again.count("Merged:") == 1


def test_a_check_that_vanished_from_the_base_is_not_green(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[])
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]



def test_a_check_run_without_an_output_object_has_no_error_lines():
    assert base_ci_red._error_lines({"name": CHECK}) == ()


def test_note_merged_without_a_block_reports_no_change(monkeypatch):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-merged",))
    gh.comment(1825, "no block here")
    monkeypatch.setattr(seam, "run_command", gh)
    assert base_ci_red.note_merged(REPO, 1825, language_code="en") is False



# --------------------------------------------------------------------------
# The tick wiring: the scan runs BEFORE the resume scan
# --------------------------------------------------------------------------

def test_the_claim_tick_runs_the_base_ci_scan_before_the_resume_scan(
        monkeypatch, tmp_path):
    """Issue #1825 acceptance: the recovered `ai-fix-needed` Issue is
    picked by the SAME tick's resume scan — the ordering is the feature."""
    from orbi import claim

    order: list[tuple[str, dict]] = []
    # The seam fans the fakes out to every module that binds the name, so
    # the test patches no package module directly (the patch ratchet).
    for name in (
        "reconcile_open_epics", "reconcile_orphan_prs",
        "pick_resumable_delivery", "pick_in_progress_issue",
        "pick_ready_issue", "pick_awaiting_merge_delivery",
    ):
        monkeypatch.setattr(seam, name, lambda *a, **k: None)
    monkeypatch.setattr(seam, "milestone_reconcile_due",
                        lambda *a, **k: False)
    monkeypatch.setattr(
        seam, "recover", lambda *a, **k: order.append(("recover", k)),
    )
    monkeypatch.setattr(
        seam, "pick_resumable_delivery",
        lambda *a, **k: order.append(("resume", k)) or None,
    )
    assert claim.pick_next_delivery(
        ["acme/app"], tmp_path, 1, hooks=None,
    ) is None
    assert [name for name, _ in order] == ["recover", "resume"]
    assert order[0][1]["dispatch_label"] == "ai-ready"
    assert order[0][1]["language_code"] == "en"



def test_the_merged_comment_drops_the_next_steps(monkeypatch):
    """The repairs the waiting comment offered are no longer next steps
    once the delivery landed — the merged comment says so and stops
    offering them."""
    error = _preexisting_error(monkeypatch)
    body = _reported_body(monkeypatch, error)
    assert "Next steps" in body
    merged = base_ci_red.rewrite_state(body, "merged", language_code="en")
    assert "Next steps" not in merged
    assert "/orbi fix-base" not in merged
    assert "Merged:" in merged
    assert base_ci_red.parse(merged).state == "merged"

