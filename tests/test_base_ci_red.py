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
from pathlib import Path

import pytest

import orbi.base_ci_red as base_ci_red
import orbi.runner as runner
from orbi import failure, failure_report, ticket_command
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker
from orbi.progress import field_block, run_marker
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


ARROW_CHECK = "build --> deploy"
NESTED_MARKER_CHECK = "lint <!-- see the note"


def test_a_check_name_that_closes_an_html_comment_round_trips():
    """The block is an HTML comment: '-->' in a field must not end it.

    A check name is GitHub's own text (a workflow name may carry a shell
    pipeline) and the base branch is a user string, so either one can
    hold the comment terminator. Unescaped, the next tick's parse reads a
    truncated payload, raises `BaseCIRedError ... Unterminated string`
    and `paused_scene` abandons the recovery (Issue #1998). The payload
    escapes '<'/'>' exactly the way the progress block does, and the
    reader decodes the same value back.
    """
    escaped = record(
        failing_checks=(ARROW_CHECK, NESTED_MARKER_CHECK),
        base_branch="release-->7",
    )
    block = base_ci_red.render(escaped)
    assert block.count("-->") == 1
    assert block.count("<!--") == 1
    assert base_ci_red.parse(block) == escaped


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


def test_the_comment_carries_the_task_identity_and_the_status_link():
    """Issue #1826: every task comment names the task and the operator's
    project status page — this blocked one included, right below the
    ready line (the first thing the reader sees)."""
    body = base_ci_red.comment_body(
        record(), [base_ci_red.FailingCheck(CHECK, url=RUN_URL)],
        failure_record=failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked"),
        language_code="en", issue_number=1825, issue_title="be nicer",
        status_url="https://status.example.com/board",
    )
    lines = body.splitlines()
    # The ready line the reader sees first, the identity right below it.
    assert lines[2] == "✅ **PR #123 is ready:** review passed, tests pass."
    assert lines[4] == "**#1825 be nicer**"
    assert any("https://status.example.com/board" in line
               for line in lines[:8])


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


def _blocks_issue(gh, number=1825, *, state="waiting", checks=(CHECK,),
                  login="orbi-bot"):
    """The paused scene's failure comment, written by the runner account.

    The runner's own account is trusted without a repository permission
    read (Issue #1806), which is exactly how the real base-CI comment is
    written; the recovery gate reads trust (Issue #1833).
    """
    gh.comment(number, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "⛔ paused",
        base_ci_red.render(record(state=state, failing_checks=checks)),
    ]), login=login, association="NONE")
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


def test_a_success_receipt_from_before_the_prefix_still_dedupes(
        monkeypatch):
    """Acceptance (Issue #1922): the machine id did not change with the
    `/orbi` prefix, so the `orbi-fix_base-command` marker of a success
    reply posted by the OLD build still stops the next scan — the comment
    is not executed twice."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    command_id = gh.issues[1825]["comments"][0]["id"]
    # The pre-#1922 success reply: same marker shape, written before the
    # prefix existed.
    gh.comment(
        1825,
        "created #950 — `backend — build, vet, test` on `release/7`\n\n"
        f"<!-- orbi-fix_base-command comment={command_id} -->",
        login="orbi-bot",
    )
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    before = len(gh.issues[1825]["comments"])
    _run_command(gh)
    # No triage Issue opened and no second reply posted for that comment.
    assert [number for number in gh.issues if number != 1825] == []
    assert len(gh.issues[1825]["comments"]) == before


def test_a_copied_success_receipt_does_not_count(monkeypatch):
    """Acceptance 3 (Issue #1992): the receipt marker is public text, so a
    read-only commenter can copy it. The author-blind check read that copy
    as "this command already ran" — no triage Issue was opened and the
    person who replied got nothing."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    command_id = gh.issues[1825]["comments"][0]["id"]
    gh.comment(
        1825,
        f"<!-- orbi-fix_base-command comment={command_id} -->",
        login="stranger", association="NONE",
    )
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    before = len(gh.issues[1825]["comments"])

    _run_command(gh)

    # The command runs: one triage Issue, and the runner's own receipt
    # beside the copied one.
    assert [number for number in gh.issues if number != 1825] != []
    receipts = [
        comment for comment in gh.issues[1825]["comments"][before:]
        if str(command_id) in comment["body"]
    ]
    assert [comment["author"]["login"] for comment in receipts] == [
        gh.login,
    ]


def test_a_runner_authored_success_receipt_still_dedupes(monkeypatch):
    """Acceptance 3 (Issue #1992): the runner's own receipt is still the
    completion record — no second triage Issue and no second reply."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    command_id = gh.issues[1825]["comments"][0]["id"]
    gh.comment(
        1825,
        f"created #950\n\n<!-- orbi-fix_base-command comment={command_id} -->",
        login="orbi-bot",
    )
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    before = len(gh.issues[1825]["comments"])

    _run_command(gh)

    assert [number for number in gh.issues if number != 1825] == []
    assert len(gh.issues[1825]["comments"]) == before


def test_fix_base_fails_the_step_instead_of_opening_a_duplicate(
        monkeypatch):
    """A lookup that cannot be read must never read as "no triage Issue":
    the command would open a SECOND `ai-ready`+`p0` fix Issue beside
    the one it could not see. It fails its own step instead, and the
    dispatcher posts the one idempotent failure receipt."""
    gh = _command_fixture(monkeypatch)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])

    def boom(*_args, **_kwargs):
        raise RuntimeError("gh down")

    monkeypatch.setattr(seam, "list_issues", boom)
    ticket_command.process_commands(
        REPO, 1825, [base_ci_red.FIX_BASE_COMMAND], language_code="en",
        record=record(), milestone=None, dispatch_label="ai-ready",
    )
    assert [number for number in gh.issues if number != 1825] == []
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any("look up the triage Issue" in body for body in bodies), bodies


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


def test_fix_base_on_a_ticket_without_a_block_is_one_receipt(monkeypatch):
    """The shared dispatcher evaluates EVERY registered command on every
    ticket that dispatches commands — the milestone notice does
    (`TICKET_COMMANDS.values()`, no base-ci-red block). A
    `/orbi fix-base` comment there owes its author one readable
    rejection, never an uncaught error that would also drop the ticket's
    other command (Issue #1825)."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.issues[1825]["labels"] = []
    gh.comment(1825, "/orbi fix-base", login="maintainer")
    ticket_command.process_commands(
        REPO, 1825, commands=ticket_command.TICKET_COMMANDS.values(),
        candidate_titles=["v0.7.15"], config_path=Path("orbi.toml"),
        policy=None, policy_path="policy.toml", base_branch=BASE_BRANCH,
        dispatch_label="ai-ready", version_file=None, language_code="en",
    )
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any(
        "no `orbi:base-ci-red:v1` block" in body for body in bodies
    ), bodies


def test_fix_base_apply_without_a_block_fails_as_a_command_step(monkeypatch):
    """The layer's contract: a handler that cannot run raises its own
    step error, so the dispatcher posts the shared failure receipt."""
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t")
    monkeypatch.setattr(seam, "run_command", gh)
    with pytest.raises(ticket_command.TicketCommandError):
        base_ci_red.FIX_BASE_COMMAND.apply(
            "fix-base", repo=REPO, issue_number=1825,
            command_comment={"id": 1}, comments=[], language_code="en",
        )
    assert gh.issues[1825]["comments"] == []


# --------------------------------------------------------------------------
# The automatic recovery scan
# --------------------------------------------------------------------------

def _scan_gh(monkeypatch, *, base_runs, pr_state="OPEN", comments=None,
             labels=("ai-blocked",), base_head=BASE_SHA, checks=(CHECK,)):
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=labels)
    _blocks_issue(gh, number=1825, checks=checks,
                  **({} if comments is None else {"state": comments}))
    gh.add_pr(PR_NUMBER, head="orbi/x", base=BASE_BRANCH, state=pr_state)
    # The PR's recorded base snapshot: deliberately NOT the base branch's
    # head (GitHub never moves it when the base branch advances).
    gh.prs[PR_NUMBER]["baseRefOid"] = BASE_SHA
    gh.add_branch(BASE_BRANCH, head=base_head)
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


def test_the_scan_recovers_a_check_name_that_closes_the_comment(
        monkeypatch):
    """Recovery is driven by the record read back from the block.

    A check name carrying `-->` must therefore still parse: the base
    check turns green and the paused delivery resumes, instead of the
    scan abandoning the scene as corrupted (Issue #1998).
    """
    gh = _scan_gh(
        monkeypatch, checks=(ARROW_CHECK,),
        base_runs=[{"id": 1, "name": ARROW_CHECK, "status": "completed",
                    "conclusion": "success", "html_url": "", "output": {}}],
    )
    assert base_ci_red.paused_scene(
        gh.issues[1825]["comments"], repo=REPO) is not None
    assert base_ci_red.recover(REPO, language_code="en") == [1825]
    assert "ai-fix-needed" in gh.issues[1825]["labels"]
    assert "ai-blocked" not in gh.issues[1825]["labels"]
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "resuming"


def test_a_re_run_check_reads_its_newest_run(monkeypatch):
    """A re-run adds a second run under the same name, newest first: the
    current verdict is the FIRST one. Reading the older failure would keep
    the delivery paused after the maintainer re-ran the check to green."""
    gh = _scan_gh(monkeypatch, base_runs=[
        {"id": 2, "name": CHECK, "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
        base_run(),
    ])
    assert base_ci_red.recover(REPO, language_code="en") == [1825]
    assert "ai-fix-needed" in gh.issues[1825]["labels"]


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
        REPO, 1, {"body": base_ci_red.render(record())},
        "merged", language_code="en",
    ) is False
    assert base_ci_red._edit_state(
        REPO, 1, {"id": 7}, "merged", language_code="en",
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


def test_an_argument_fix_base_cannot_take_is_rejected_with_the_usage(
        monkeypatch):
    """Issue #1922: `fix-base` has arity 0, so a trailing word is a
    malformed command — one readable receipt naming the canonical usage."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi fix-base now", login="maintainer")
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert any(
        "command not applied" in body and "`/orbi fix-base`" in body
        and "takes no argument" in body
        for body in bodies
    ), bodies


def test_an_unknown_orbi_subcommand_gets_no_receipt(monkeypatch):
    """Acceptance (Issue #1922): `/orbi <unknown>` is an unrelated comment
    — no action, no receipt."""
    gh = _command_fixture(monkeypatch)
    gh.issues[1825]["comments"].clear()
    gh.comment(1825, "/orbi retry", login="maintainer")
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    assert len(gh.issues) == 1
    bodies = [comment["body"] for comment in gh.issues[1825]["comments"]]
    assert not any("command not applied" in body for body in bodies), bodies
    assert not any("command failed" in body for body in bodies), bodies


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


def test_a_branch_without_a_head_leaves_the_issue_alone(monkeypatch):
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    gh.branches[BASE_BRANCH]["head"] = None
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]


def test_the_scan_reads_the_live_base_head_not_the_prs_base_snapshot(
        monkeypatch):
    """The PR's `baseRefOid` is the base snapshot the PR was compared
    against: GitHub never moves it when the base branch advances. A fix
    that landed on the base AFTER the pause must still be seen, so the
    scan reads the branch's CURRENT head (Issue #1825)."""
    gh = _scan_gh(monkeypatch, base_runs=[base_run()])
    gh.add_check_runs("live-base-head", [
        {"id": 2, "name": CHECK, "status": "completed",
         "conclusion": "success", "html_url": "", "output": {}},
    ])
    gh.branches[BASE_BRANCH]["head"] = "live-base-head"
    assert base_ci_red.recover(REPO, language_code="en") == [1825]
    assert "ai-fix-needed" in gh.issues[1825]["labels"]
    assert base_ci_red.parse(_block_comment(gh)["body"]).state == "resuming"


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
# Only the LATEST TRUSTED failure authorizes a resume (Issue #1833)
# --------------------------------------------------------------------------

GREEN_RUN = {"id": 1, "name": CHECK, "status": "completed",
             "conclusion": "success", "html_url": "", "output": {}}


def _trust_scene(monkeypatch, *, base_runs=(GREEN_RUN,)):
    """The paused Issue with a green base and the two authors GitHub knows."""
    gh = FakeGh(REPO)
    gh.add_issue(1825, title="t", labels=("ai-blocked",))
    gh.add_pr(PR_NUMBER, head="orbi/x", base=BASE_BRANCH)
    gh.prs[PR_NUMBER]["baseRefOid"] = BASE_SHA
    gh.add_branch(BASE_BRANCH, head=BASE_SHA)
    gh.add_check_runs(BASE_SHA, list(base_runs))
    gh.add_collaborator("visitor", "read")
    gh.add_collaborator("maintainer", "write")
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def _fresh(gh, number=1825):
    """Stamp the newest comment NOW: the 7-day reminder never fires."""
    gh.issues[number]["comments"][-1]["createdAt"] = datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_ci_comment(gh, *, login, checks=(CHECK,),
                     state=base_ci_red.STATE_WAITING,
                     run_id="096197d2"):
    """One base-CI failure comment as `login` writes it."""
    gh.comment(1825, "\n".join([
        run_marker(run_id),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "⛔ paused",
        base_ci_red.render(record(state=state, failing_checks=checks)),
    ]), login=login, association="NONE")
    _fresh(gh)


def _other_failure_comment(gh, *, login, run_id="096197d2"):
    """A newer failure comment carrying NO base-CI block."""
    gh.comment(1825, "\n".join([
        run_marker(run_id),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="human_decision_required", action_code="decide",
            retry_safe=False, outcome="blocked",
        )),
    ]), login=login, association="NONE")
    _fresh(gh)


def _snapshot(gh, number=1825):
    """The Issue's comments as the reader sees them (id + body)."""
    return [(comment["id"], comment["body"])
            for comment in gh.issues[number]["comments"]]


def test_a_read_only_copied_record_never_authorizes_a_resume(monkeypatch):
    """Issue #1833: `comment_trust` rejects the copied record, so the scan
    does too — no label change, no comment edit, no new comment."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="visitor")
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_read_only_copy_with_no_checks_never_authorizes_a_resume(
        monkeypatch):
    """An empty check list is vacuously green; a copied record must not
    turn that into a resume either, with no check result at all."""
    gh = _trust_scene(monkeypatch, base_runs=())
    _base_ci_comment(gh, login="visitor", checks=())
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_newer_trusted_failure_supersedes_the_older_base_ci_record(
        monkeypatch):
    """The latest trusted failure is `human_decision_required` and carries
    no base-CI block: the older waiting record is no longer eligible."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="orbi-bot")
    _other_failure_comment(gh, login="orbi-bot")
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_current_trusted_record_resumes_and_a_later_copy_does_not_stop_it(
        monkeypatch):
    """A valid trusted recovery is unaffected by a newer untrusted copy:
    the delivery resumes on the run id its own record carries."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="orbi-bot", run_id="abc12345")
    _base_ci_comment(gh, login="visitor", run_id="abc12345")
    assert base_ci_red.recover(REPO, language_code="en") == [1825]
    labels = gh.issues[1825]["labels"]
    assert "ai-fix-needed" in labels and "ai-blocked" not in labels
    trusted_body, copied_body = [
        comment["body"] for comment in gh.issues[1825]["comments"]
    ]
    assert base_ci_red.parse(trusted_body).state == base_ci_red.STATE_RESUMING
    assert run_marker("abc12345") in trusted_body
    # The untrusted copy stays exactly as its author wrote it.
    assert base_ci_red.parse(copied_body).state == base_ci_red.STATE_WAITING


def test_a_kindless_milestone_repeating_the_block_is_no_failure(
        monkeypatch):
    """The runner's short milestone comment is posted AFTER the failure
    comment and repeats the same `orbi:failure:v1` block (Issue #1322).
    It carries no kind marker, so it is not a failure: the base-CI comment
    stays the latest trusted failure and a green base still recovers it."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="orbi-bot")
    milestone = field_block(
        "096197d2", "Orbi paused: base is red", {"run_id": "096197d2"},
        language_code="en",
    )
    marker, _, rest = milestone.partition("\n")
    gh.comment(1825, "\n".join([
        marker,
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        rest,
    ]), login="orbi-bot", association="NONE")
    _fresh(gh)
    assert base_ci_red.recover(REPO, language_code="en") == [1825]
    assert "ai-fix-needed" in gh.issues[1825]["labels"]


def test_a_corrupted_failure_block_supersedes_the_older_record(monkeypatch):
    """A corrupted `orbi:failure:v1` block is never read as "no reason"
    (`failure.parse`): it is the latest trusted failure, it states no
    `base_ci_red` reason, and it therefore ends the older record's
    eligibility instead of hiding behind it."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="orbi-bot")
    gh.comment(1825, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        "<!-- orbi:failure:v1 not-json -->",
    ]), login="orbi-bot", association="NONE")
    _fresh(gh)
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_kinded_failure_without_a_record_supersedes_the_older_record(
        monkeypatch):
    """The real scene-blocked / resume-blocked comment: `failure` kind, no
    `orbi:failure:v1` block. It states no `base_ci_red` reason, so it is
    the latest trusted failure that ends the older record's eligibility —
    the scan never falls back past it to an older base-CI block."""
    gh = _trust_scene(monkeypatch)
    _base_ci_comment(gh, login="orbi-bot")
    gh.comment(1825, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        "⛔ restore the scene comment, then relabel `ai-ready`",
    ]), login="orbi-bot", association="NONE")
    _fresh(gh)
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_corrupted_base_ci_block_in_the_selected_comment_is_skipped(
        monkeypatch):
    """A present-but-invalid block is never read as "no block", and it is
    never acted on either: the Issue stays `ai-blocked` untouched."""
    gh = _trust_scene(monkeypatch)
    gh.comment(1825, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
        "<!-- orbi:base-ci-red:v1 broken -->",
    ]), login="orbi-bot", association="NONE")
    _fresh(gh)
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


def test_a_base_ci_red_failure_without_a_record_never_recovers(monkeypatch):
    """The reason `base_ci_red` alone authorizes nothing: the record must
    be in that same comment (Issue #1833)."""
    gh = _trust_scene(monkeypatch)
    gh.comment(1825, "\n".join([
        run_marker("096197d2"),
        comment_kind_marker(COMMENT_KIND_FAILURE),
        failure.render(failure.Failure(
            reason_code="base_ci_red", action_code="fix_ticket",
            retry_safe=False, outcome="blocked",
        )),
    ]), login="orbi-bot", association="NONE")
    _fresh(gh)
    before = _snapshot(gh)
    assert base_ci_red.recover(REPO, language_code="en") == []
    assert gh.issues[1825]["labels"] == ["ai-blocked"]
    assert _snapshot(gh) == before


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

