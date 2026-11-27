"""Carry out a person's answer to a stop question at every stage (Issue #2221).

A stop that needs a decision carries one hidden question (Issue #2220).
These tests drive the REAL entry points over the in-memory gh fake:

- the stopped-ticket command scan (through the real tick,
  `claim.pick_next_delivery`, and through the scan function it calls,
  `stopped_scans.run`) for `ai-blocked`, `ai-pr-opened` and `ai-fix-needed`;
- the steering poll (`pi_session.run_pi`'s own `check_steering`) for
  `ai-in-progress`;
- the merge gate (`merge_exec.merge_gate`) and the review round's
  fix-round transition for a clean-reviewed PR waiting for CI.

What is asserted is the observable result — the Issue body, the labels, the PR
state, the one receipt — never an internal call sequence.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import pytest

import orbi.answer as answer
import orbi.answer as answer_module
import orbi.merge_exec as merge_exec
import orbi.pi_session as pi_session
import orbi.review_merge as review_merge
from orbi import (
    acceptance_section, claim, question, stopped_scans, ticket_command,
)
from orbi.config import RunnerConfig
from orbi.delivery_labels import (
    BLOCKED_LABEL, FIX_NEEDED_LABEL, PR_OPENED_LABEL, READY_LABEL,
)
from orbi.delivery_scene import RunContext
from orbi.resume_scan import ResumeHooks
from seam import seam

from tests.fakes.github import FakeGh
from tests.test_review_merge import (
    _pr, _review_merge_config, _scene, _seed_run_state,
)

REPO = "acme/app"
TICKET = 100
RUN_MARKER = "<!-- orbi:run=abcdef12 -->"
BODY = "## Outcome\nUsers keep notes.\n"
APPENDED_MARKER = f"<!-- {acceptance_section.ACCEPTANCE_MARKER} -->"
CRITERION = "A saved note keeps its tag after a reload"

# The stop question a review deadlock or an external contribution asks: a
# closed choice with the four actions, one of them naming the external PR.
CHOICE = {
    "id": "q1",
    "kind": "choice",
    "summary": "The reviewer and the engine disagree about the tag model.",
    "question": "How should this delivery go on?",
    "options": [
        {
            "label": "Add the assumption",
            "description": CRITERION,
            "action": "amend_requeue",
            "recommended": True,
            "reason": "the evidence in the ticket supports it",
        },
        {
            "label": "Merge it on GitHub",
            "description": "A maintainer reviews and merges the PR there.",
            "action": "link",
            "url": "https://github.com/acme/app/pull/7",
        },
        {
            "label": "Close the external PR",
            "description": "Close it and let Orbi build the change itself.",
            "action": "close_external_pr",
        },
        {
            "label": "Drop the work",
            "description": "Close this Issue and its PR.",
            "action": "close_all",
        },
    ],
}

# The same choice without a link option: the answer concerns this Issue's
# own delivery PR, found on its stable delivery branch.
INTERNAL_CHOICE = {
    "id": "q1",
    "kind": "choice",
    "summary": "The reviewer and the engine disagree about the tag model.",
    "question": "How should this delivery go on?",
    "options": [
        {
            "label": "Add the assumption",
            "description": CRITERION,
            "action": "amend_requeue",
        },
        {
            "label": "Drop the work",
            "description": "Close this Issue and its PR.",
            "action": "close_all",
        },
    ],
}

CONFIRM = {
    "id": "q1",
    "kind": "confirm",
    "summary": "One assumption decides the whole design.",
    "question": "Append this assumption and continue?",
    "action": "amend_requeue",
    "criterion": CRITERION,
}



# --------------------------------------------------------------------------
# fixtures: a stopped ticket and one answer comment over the gh fake
# --------------------------------------------------------------------------

def _gh(monkeypatch) -> FakeGh:
    """The in-memory GitHub boundary: the ONE seam these tests replace."""
    gh = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def stop(payload: dict, decision_class: str = "authority"):
    """One strict stop question, exactly as the engine writes it."""
    return question.parse_stop(payload, decision_class)


def internal_choice() -> dict:
    """The deadlock question an internal delivery's reviewer writes."""
    return dict(INTERNAL_CHOICE)


def stopped_ticket(
    gh: FakeGh, payload: dict = CHOICE, *, labels=(BLOCKED_LABEL,),
    body: str = BODY, number: int = TICKET,
) -> None:
    """The stopped ticket: the engine's stop comment carrying the question."""
    gh.add_issue(number, title="Note app", body=body, labels=labels)
    gh.comment(
        number,
        f"{RUN_MARKER}\n<!-- orbi:comment=failure -->\n\n"
        f"The delivery stopped.\n\n{question.render_stop(stop(payload), 'en')}",
        login="orbi-bot",
    )


def post(
    gh: FakeGh, *, value, action: str = "accept", q: str = "q1",
    reason: str | None = None, login: str = "maintainer",
    permission: str | None = "write", number: int = TICKET,
) -> None:
    """The answer one person posts, in the record shape of the contract."""
    gh.comment(
        number,
        f"{question.render_answer(q, value=value, action=action, by=login, reason=reason)}\n\n"
        "Carry this one out, please.\n",
        login=login,
    )
    if permission is not None:
        gh.add_collaborator(login, permission)


def _hooks() -> ResumeHooks:
    noop = lambda *args, **kwargs: None  # noqa: E731
    return ResumeHooks(
        resume_scene=noop,
        route_external_pr_ticket=lambda *args, **kwargs: False,
        block_scene_failure=noop,
        recover_missing_pr_scene=noop,
        has_recoverable_pr_scene=lambda *args, **kwargs: False,
        comment_pr=noop,
    )


def tick(monkeypatch, tmp_path, gh: FakeGh):
    """One real Runner tick over the fake GitHub (the stopped-ticket scans)."""
    return claim.pick_next_delivery(
        [REPO], tmp_path, 1, config=None, hooks=_hooks(),
    )


def bodies(gh: FakeGh, number: int = TICKET) -> list[str]:
    return [comment["body"] for comment in gh.issues[number]["comments"]]


def labels(gh: FakeGh, number: int = TICKET) -> list[str]:
    return list(gh.issues[number]["labels"])


def successes(gh: FakeGh, number: int = TICKET) -> list[str]:
    """The answer's own success receipts (never a rejected/failed one)."""
    return [
        body for body in bodies(gh, number)
        if "<!-- orbi-answer-command comment=" in body
    ]


def rejection(gh: FakeGh, number: int = TICKET) -> str:
    receipts = [
        body for body in bodies(gh, number)
        if "orbi-answer-command-rejected" in body
    ]
    assert len(receipts) == 1, bodies(gh, number)
    return receipts[0]



# --------------------------------------------------------------------------
# acceptance 1: ai-blocked carries out the chosen action, with one receipt
# --------------------------------------------------------------------------

def test_an_amend_requeue_answer_appends_the_criterion_and_queues(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    body = gh.issues[TICKET]["body"]
    assert body.startswith("## Outcome\nUsers keep notes.")
    assert f"- {CRITERION}" in body
    assert body.count(APPENDED_MARKER) == 1
    assert READY_LABEL in labels(gh)
    assert BLOCKED_LABEL not in labels(gh)
    assert len(successes(gh)) == 1
    assert CRITERION in successes(gh)[0]


def test_the_receipt_is_the_counter_and_a_second_tick_does_nothing(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(gh, value="Add the assumption")
    tick(monkeypatch, tmp_path, gh)
    body = gh.issues[TICKET]["body"]
    comments = bodies(gh)

    stopped_scans.run(REPO, language_code="en")

    assert gh.issues[TICKET]["body"] == body
    assert bodies(gh) == comments
    assert len(successes(gh)) == 1


def test_a_close_external_pr_answer_closes_the_named_pr_and_queues(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    gh.add_pr(7, head="contributor/feature")
    stopped_ticket(gh)
    post(gh, value="Close the external PR")

    tick(monkeypatch, tmp_path, gh)

    assert gh.prs[7]["state"] == "CLOSED"
    assert READY_LABEL in labels(gh)
    assert BLOCKED_LABEL not in labels(gh)
    assert len(successes(gh)) == 1
    assert "https://github.com/acme/app/pull/7" in successes(gh)[0]


def test_a_cancel_closes_the_issue_its_delivery_pr_and_the_branch(
    monkeypatch, tmp_path,
):
    """Cancel performs close_all: the Issue, its delivery PR and the branch."""
    gh = _gh(monkeypatch)
    gh.add_pr(9, head="orbi/acme-app-issue-100")
    stopped_ticket(gh, internal_choice())
    post(gh, action="cancel", value="cancel")

    tick(monkeypatch, tmp_path, gh)

    assert gh.prs[9]["state"] == "CLOSED"
    assert gh.issues[TICKET]["state"] == "closed"
    assert "orbi/acme-app-issue-100" in gh.deleted_branches
    assert len(successes(gh)) == 1


def test_a_close_all_answer_closes_the_pr_the_question_names(
    monkeypatch, tmp_path,
):
    """The external-contribution question names its PR: that one closes."""
    gh = _gh(monkeypatch)
    gh.add_pr(7, head="contributor/feature")
    stopped_ticket(gh)
    post(gh, value="Drop the work")

    tick(monkeypatch, tmp_path, gh)

    assert gh.prs[7]["state"] == "CLOSED"
    assert gh.issues[TICKET]["state"] == "closed"
    assert len(successes(gh)) == 1


def test_a_decline_appends_the_persons_own_words_and_requeues(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(
        gh, action="decline", value="decline",
        reason="Scope the tag to the note, not to the notebook",
    )

    tick(monkeypatch, tmp_path, gh)

    assert "Scope the tag to the note, not to the notebook" in (
        gh.issues[TICKET]["body"]
    )
    assert READY_LABEL in labels(gh) and BLOCKED_LABEL not in labels(gh)
    assert len(successes(gh)) == 1


def test_the_unblock_is_one_whole_set_label_write(monkeypatch, tmp_path):
    """Issue #2426: answering a blocked stop is ONE whole-set
    replacement of the delivery labels, so GitHub cannot apply the
    ai-blocked removal and fail the ai-ready add (the #3953/#669 shape
    that left the Issue with no delivery label at all). Labels that are
    not delivery states (bug, p0) survive either way."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(BLOCKED_LABEL, "bug", "p0"))
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert set(labels(gh)) == {READY_LABEL, "bug", "p0"}
    writes = [
        command for command in gh.commands
        if command[0:4] == ["gh", "api", "--method", "PUT"]
        and command[4].endswith(f"/issues/{TICKET}/labels")
    ]
    assert len(writes) == 1
    # A single-effect gh label edit would be the pre-#2426 shape.
    assert not [
        command for command in gh.commands
        if " ".join(command).startswith("gh issue edit")
        and any(part in ("--add-label", "--remove-label") for part in command)
    ]
    assert len(successes(gh)) == 1


def test_a_confirm_yes_appends_the_questions_criterion(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, CONFIRM)
    post(gh, value=True)

    tick(monkeypatch, tmp_path, gh)

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]
    assert READY_LABEL in labels(gh)
    assert len(successes(gh)) == 1


def test_a_value_matching_no_option_is_not_carried_out(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(gh, value="Do something else entirely")

    tick(monkeypatch, tmp_path, gh)

    assert "has the value" in rejection(gh)
    assert "Do something else entirely" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY
    assert labels(gh) == [BLOCKED_LABEL]
    assert successes(gh) == []


def test_a_cancel_between_rounds_is_not_carried_out(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(PR_OPENED_LABEL,))
    post(gh, action="cancel", value="cancel")

    stopped_scans.run(REPO, language_code="en")

    assert "ai-blocked" in rejection(gh)
    assert gh.issues[TICKET]["state"] == "open"
    assert labels(gh) == [PR_OPENED_LABEL]
    assert successes(gh) == []



# --------------------------------------------------------------------------
# acceptance 2: untrusted, stale and engine-written answers are not executed
# --------------------------------------------------------------------------

def test_an_untrusted_author_is_not_carried_out(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(gh, value="Add the assumption", login="drive-by", permission=None)

    tick(monkeypatch, tmp_path, gh)

    assert "not carried out" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY
    assert labels(gh) == [BLOCKED_LABEL]
    assert successes(gh) == []


def test_an_answer_older_than_the_newest_stop_is_not_carried_out(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    post(gh, value="Add the assumption")
    newer = dict(CHOICE, id="q2")
    gh.comment(
        TICKET,
        f"{RUN_MARKER}\n<!-- orbi:comment=failure -->\n\n"
        f"The delivery stopped again.\n\n{question.render_stop(stop(newer), 'en')}",
        login="orbi-bot",
    )

    tick(monkeypatch, tmp_path, gh)

    assert "older" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY
    assert labels(gh) == [BLOCKED_LABEL]
    assert successes(gh) == []


def _stale_answer_ticket(gh: FakeGh) -> None:
    """An applied-looking answer, then a stop on a newer question."""
    stopped_ticket(gh)
    post(gh, value="Add the assumption")
    newer = dict(CHOICE, id="q2")
    gh.comment(
        TICKET,
        f"{RUN_MARKER}\n<!-- orbi:comment=failure -->\n\n"
        f"The delivery stopped again.\n\n{question.render_stop(stop(newer), 'en')}",
        login="orbi-bot",
    )


def test_a_rejection_receipt_is_not_read_back_as_a_new_answer(
    monkeypatch, tmp_path,
):
    """Issue #2295: the receipt quotes the stale answer, so every tick read
    it as a new answer and posted an identical receipt, forever."""
    gh = _gh(monkeypatch)
    _stale_answer_ticket(gh)

    for _ in range(3):
        stopped_scans.run(REPO, language_code="en")

    assert "older" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY
    assert labels(gh) == [BLOCKED_LABEL]
    assert successes(gh) == []


def test_a_real_answer_after_a_rejection_receipt_is_still_applied(
    monkeypatch, tmp_path,
):
    """Issue #2295: skipping the receipt must not swallow the answer a
    person posts to the latest question afterwards."""
    gh = _gh(monkeypatch)
    _stale_answer_ticket(gh)
    for _ in range(3):
        stopped_scans.run(REPO, language_code="en")
    assert "older" in rejection(gh)

    post(gh, value="Add the assumption", q="q2")
    stopped_scans.run(REPO, language_code="en")

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]
    assert READY_LABEL in labels(gh)
    assert BLOCKED_LABEL not in labels(gh)
    assert len(successes(gh)) == 1
    # The one stale answer still has its one rejection receipt.
    assert len([
        body for body in bodies(gh) if "orbi-answer-command-rejected" in body
    ]) == 1


def test_the_engines_own_record_is_never_carried_out(monkeypatch, tmp_path):
    """Issue #2220's automatic answer is engine output: its comment carries
    the run marker AND answers as the engine, so it is not a person's
    decision and leaves no receipt."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    gh.comment(
        TICKET,
        f"{RUN_MARKER}\n<!-- orbi:comment=requeued -->\n\n"
        f"{question.render_answer('q1', value='Add the assumption')}\n",
        login="orbi-bot",
    )

    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["body"] == BODY
    assert labels(gh) == [BLOCKED_LABEL]
    assert successes(gh) == []
    assert "orbi-answer-command-rejected" not in "".join(bodies(gh))


# --------------------------------------------------------------------------
# acceptance 4: between rounds the body change lands before the next review
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", [PR_OPENED_LABEL, FIX_NEEDED_LABEL])
def test_a_between_rounds_answer_updates_the_body_without_moving_labels(
    monkeypatch, label,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(label,))
    post(gh, value="Add the assumption")

    stopped_scans.run(REPO, language_code="en")

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]
    assert labels(gh) == [label]
    assert len(successes(gh)) == 1



# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# acceptance 3: ai-in-progress writes the body change and restarts once
# --------------------------------------------------------------------------

def _config(tmp_path, **kwargs) -> RunnerConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system", encoding="utf-8")
    return RunnerConfig(
        prompt=prompt, repo_dir=tmp_path, source_repos=(REPO,),
        workspace_root=tmp_path, context_files=(), skills=(),
        base_branch="main", base_sha="abc123def456", run_id="abc12345",
        **kwargs,
    )


def test_a_steering_answer_updates_the_body_and_restarts_the_session_once(
    monkeypatch, tmp_path,
):
    """Acceptance 3: the poll writes the amendment the restarted session
    reads, and it causes exactly ONE restart. The engine records the body it
    wrote, so the next poll never reads it back as a second, person-made
    correction (a maintainer sharing the runner account is still read as a
    person: only the engine write itself is skipped)."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(), body=BODY)
    post(gh, value="Add the assumption")
    # GitHub exposes only whole-second stamps: the answer is created while
    # the session runs, so it must be newer than the session start.
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    gh.issues[TICKET]["comments"][-1]["createdAt"] = future
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda *a, **k: {
            "comments": gh.issues[TICKET]["comments"],
            "body": gh.issues[TICKET]["body"],
            "state": "OPEN",
        },
    )
    monkeypatch.setitem(
        pi_session.__dict__, "changed_files", lambda *a, **k: [],
    )
    monkeypatch.setitem(
        pi_session.__dict__, "activity_snapshot", lambda *a, **k: None,
    )
    captured = {}
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        lambda command, **kw: captured.update(kw) or "done",
    )
    issue = {"number": TICKET, "title": "Note app", "body": BODY}
    assert pi_session.run_pi(
        issue, RunContext("abc12345", TICKET, "branch", tmp_path, REPO),
        _config(tmp_path),
    ) == "done"
    check = captured["watch"].steering_check

    request = check()

    assert request is not None
    assert request.body_revision is True
    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]
    assert f"- {CRITERION}" in request.context
    # The answer's ONE receipt follows the body change, run-marked so the
    # poll never steers on its own output (Issue #2221): without it the
    # merge gate reads the answer as uncarried and a later scan carries it
    # out a second time.
    assert len(successes(gh)) == 1
    assert "<!-- orbi:run=abc12345 -->" in successes(gh)[0]
    assert CRITERION in successes(gh)[0]
    # Exactly one restart: the next poll sees no fresh correction and no
    # body edit, because the engine recorded the body it wrote and skips
    # its own marked output.
    assert check() is None
    assert gh.issues[TICKET]["body"].count(CRITERION) == 1


def test_a_run_marked_engine_comment_does_not_restart_the_session(
    monkeypatch, tmp_path,
):
    """The poll reads the person's correction, never the engine's own marked
    output: an answer's receipt is such a comment (Issue #2221), and it is
    created while the session runs, so without the rule it would restart the
    session a second time."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(), body=BODY)
    gh.comment(
        TICKET,
        "<!-- orbi:run=abc12345 -->\n\nThe answer was carried out.\n",
        login="orbi-bot",
    )
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    gh.issues[TICKET]["comments"][-1]["createdAt"] = future
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda *a, **k: {
            "comments": gh.issues[TICKET]["comments"],
            "body": gh.issues[TICKET]["body"],
            "state": "OPEN",
        },
    )
    monkeypatch.setitem(
        pi_session.__dict__, "changed_files", lambda *a, **k: [],
    )
    monkeypatch.setitem(
        pi_session.__dict__, "activity_snapshot", lambda *a, **k: None,
    )
    captured = {}
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        lambda command, **kw: captured.update(kw) or "done",
    )
    issue = {"number": TICKET, "title": "Note app", "body": BODY}
    assert pi_session.run_pi(
        issue, RunContext("abc12345", TICKET, "branch", tmp_path, REPO),
        _config(tmp_path),
    ) == "done"

    assert captured["watch"].steering_check() is None


# --------------------------------------------------------------------------
# acceptance 5: the merge gate reads the answer before it merges
# --------------------------------------------------------------------------

def _starts_with(command: list[str], prefix: list[str]) -> bool:
    """True when the command begins with prefix (keeps the ratchet clean)."""
    return command[: len(prefix)] == prefix


def _stop_body() -> str:
    return ("<!-- orbi:run=abcdef12 -->\n<!-- orbi:comment=failure -->\n\n"
            "The delivery stopped.\n\n"
            f"{question.render_stop(stop(CHOICE), 'en')}")


def _answer_record(*, value: str = "Add the assumption") -> str:
    return question.render_answer("q1", value=value, by="maintainer")


def _comments() -> list[dict]:
    return [
        {"id": "stop", "createdAt": "2026-11-17T00:00:01Z",
         "author": {"login": "orbi-bot"}, "body": _stop_body()},
        {"id": "answer", "createdAt": "2026-11-17T00:00:02Z",
         "author": {"login": "maintainer"}, "body": _answer_record()},
    ]


def _gate_fake(merged: list[list[str]], comments: list[dict]):
    """A gh seam: a green mergeable PR, one ticket, and its trust reads."""
    def fake_run(command, **kwargs):
        if _starts_with(command, ["gh", "pr"]) and "view" in command:
            return json.dumps({
                "state": "OPEN", "mergeable": "MERGEABLE",
                "headRefOid": "h1",
                "statusCheckRollup": [{
                    "name": "tests", "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }],
            })
        if _starts_with(command, ["gh", "pr"]) and "merge" in command:
            merged.append(list(command))
            return ""
        if _starts_with(command, ["gh", "issue", "view"]):
            if "comments" in command:
                return json.dumps({"comments": comments})
            return json.dumps({"labels": [{"name": PR_OPENED_LABEL}]})
        if (_starts_with(command, ["gh", "api"])
                and "collaborators" in " ".join(command)):
            return json.dumps({"permission": "admin"})
        if _starts_with(command, ["gh", "auth"]):
            return (
                "github.com\n"
                "  Logged in to github.com account orbi-bot (keyring)\n"
                "  - Active account: true\n"
            )
        return ""
    return fake_run


def test_the_merge_gate_refuses_to_merge_over_a_newer_answer(
    monkeypatch, tmp_path,
):
    """Acceptance 5: a person who answered while CI was running must not
    have the older decision merged over."""
    merged: list[list[str]] = []
    monkeypatch.setattr(
        seam, "run_command", _gate_fake(merged, _comments()),
    )

    with pytest.raises(answer.AnswerSupersedesVerdict) as raised:
        merge_exec.merge_gate(
            tmp_path, _pr(), "main", repo_dir=tmp_path,
            source_repo=REPO, issue_number=TICKET, run_id="abc12345",
            language_code="en",
        )

    assert "q1" in str(raised.value)
    assert merged == []


def test_the_merge_gate_merges_when_the_answer_is_already_carried_out(
    monkeypatch, tmp_path,
):
    """The rule is "not carried out yet", not "any answer on the ticket":
    a receipted answer changed the ticket before this review read it."""
    comments = _comments()
    comments.append({
        "id": "receipt", "createdAt": "2026-11-17T00:00:03Z",
        "author": {"login": "orbi-bot"},
        "body": answer._receipt_marker(comments[-1]),
    })
    merged: list[list[str]] = []
    monkeypatch.setattr(
        seam, "run_command", _gate_fake(merged, comments),
    )

    pr = merge_exec.merge_gate(
        tmp_path, _pr(), "main", repo_dir=tmp_path,
        source_repo=REPO, issue_number=TICKET, run_id="abc12345",
        language_code="en",
    )

    assert pr["merged"] is True
    assert len(merged) == 1


def test_the_merge_gate_merges_after_the_poll_carried_the_answer_out(
    monkeypatch, tmp_path,
):
    """Acceptance 3 and 5 together: an answer the steering poll carried out
    (body written, session restarted) is DONE. It must not hold a later
    merge, or a clean delivery would go to a fix round over a decision its
    body already carries."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")
    issue = {"number": TICKET, "title": "Note app", "body": BODY}
    fresh = [
        dict(comment, issue=TICKET)
        for comment in gh.issues[TICKET]["comments"]
    ]

    amended = answer.apply_steering(
        fresh, issue=issue, repo=REPO, comments=gh.issues[TICKET]["comments"],
        body=BODY, language_code="en", run_id="abc12345",
    )

    assert amended is not None and f"- {CRITERION}" in amended
    merged: list[list[str]] = []
    monkeypatch.setattr(
        seam, "run_command",
        _gate_fake(merged, gh.issues[TICKET]["comments"]),
    )
    pr = merge_exec.merge_gate(
        tmp_path, _pr(), "main", repo_dir=tmp_path, source_repo=REPO,
        issue_number=TICKET, run_id="abc12345", language_code="en",
    )

    assert pr["merged"] is True
    assert len(merged) == 1


def _gh_and_git(gh: FakeGh):
    """Every gh call goes to the stateful fake; git answers empty."""
    def run(command, **kwargs):
        if _starts_with(command, ["gh"]):
            return gh(command, **kwargs)
        return ""
    return run


def test_a_newer_answer_sends_the_waiting_merge_to_a_fix_round(
    monkeypatch, tmp_path,
):
    """Acceptance 5, second half: the clean-reviewed PR is NOT merged, and
    the Issue moves to ai-fix-needed so the next review reads the amended
    ticket. The real gate, review round and label patch run over the fake."""
    gh = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", _gh_and_git(gh))
    branch = "orbi/acme-app-issue-100"
    gh.add_pr(
        4, head=branch, base="main", oid="h1",
        checks=({"name": "tests", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},),
    )
    gh.prs[4]["mergeable"] = "MERGEABLE"
    stopped_ticket(gh, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")
    _seed_run_state(tmp_path)

    merged = review_merge.review_and_merge_if_clean(
        tmp_path, branch, "main", _review_merge_config(tmp_path), REPO,
        TICKET, title="Note app", priority="normal", scene=_scene(),
        merge_only=True,
    )

    assert merged is False
    assert gh.prs[4]["state"] == "OPEN"
    assert FIX_NEEDED_LABEL in labels(gh)
    assert PR_OPENED_LABEL not in labels(gh)
    assert any("q1" in body and "not merged" in body for body in bodies(gh))


# --------------------------------------------------------------------------
# one answer counts at every stage: the last word wins (Issue #2233)
# --------------------------------------------------------------------------

SECOND_LABEL = "Keep the tag on rename"
SECOND_CRITERION = "A tag survives renaming the note"

# The same question with TWO amend options: a person answers the first,
# changes their mind and answers the second.
TWO_WAYS = {
    "id": "q1",
    "kind": "choice",
    "summary": "The reviewer and the engine disagree about the tag model.",
    "question": "How should this delivery go on?",
    "options": [
        {
            "label": "Add the assumption",
            "description": CRITERION,
            "action": "amend_requeue",
            "recommended": True,
            "reason": "the evidence in the ticket supports it",
        },
        {
            "label": SECOND_LABEL,
            "description": SECOND_CRITERION,
            "action": "amend_requeue",
        },
    ],
}


def test_the_last_answer_wins_at_every_stage(monkeypatch, tmp_path):
    """Acceptance (Issue #2233): a ticket carrying answers A then B to the
    same stop question is carried out as B by the stopped-ticket scan, the
    merge gate's refusal text and the steering poll, and the Issue body
    carries B's words, never A's. Before this the scan read B, the gate
    named A and the poll wrote both."""
    # The stopped-ticket scan: one amendment, B's.
    scan = _gh(monkeypatch)
    stopped_ticket(scan, TWO_WAYS)
    post(scan, value="Add the assumption")
    post(scan, value=SECOND_LABEL)

    tick(monkeypatch, tmp_path, scan)

    assert f"- {SECOND_CRITERION}" in scan.issues[TICKET]["body"]
    assert CRITERION not in scan.issues[TICKET]["body"]
    assert len(successes(scan)) == 1
    assert SECOND_CRITERION in successes(scan)[0]

    # The steering poll of a running session: both answers arrive while it
    # runs, and the poll carries out B only, with ONE restart.
    poll = _gh(monkeypatch)
    stopped_ticket(poll, TWO_WAYS, labels=())
    post(poll, value="Add the assumption")
    post(poll, value=SECOND_LABEL)
    # GitHub exposes only whole-second stamps: the answers are created while
    # the session runs, so they are newer than the session start.
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10))
    for comment in poll.issues[TICKET]["comments"][-2:]:
        comment["createdAt"] = future
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view",
        lambda *a, **k: {
            "comments": poll.issues[TICKET]["comments"],
            "body": poll.issues[TICKET]["body"],
            "state": "OPEN",
        },
    )
    monkeypatch.setitem(
        pi_session.__dict__, "changed_files", lambda *a, **k: [],
    )
    monkeypatch.setitem(
        pi_session.__dict__, "activity_snapshot", lambda *a, **k: None,
    )
    captured = {}
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        lambda command, **kw: captured.update(kw) or "done",
    )
    issue = {"number": TICKET, "title": "Note app", "body": BODY}
    assert pi_session.run_pi(
        issue, RunContext("abc12345", TICKET, "branch", tmp_path, REPO),
        _config(tmp_path),
    ) == "done"
    check = captured["watch"].steering_check

    request = check()

    assert request is not None
    assert request.body_revision is True
    assert f"- {SECOND_CRITERION}" in poll.issues[TICKET]["body"]
    assert CRITERION not in poll.issues[TICKET]["body"]
    assert len(successes(poll)) == 1
    assert SECOND_CRITERION in successes(poll)[0]
    assert check() is None
    # The answer that counts is carried out, so nothing holds a merge and
    # the superseded A is not pending either.
    assert answer.pending_change(
        TICKET, repo=REPO, head="h1", language_code="en",
    ) is None

    # The merge gate's refusal names the answer that counts.
    gate = _gh(monkeypatch)
    stopped_ticket(gate, TWO_WAYS, labels=(PR_OPENED_LABEL,))
    post(gate, value="Add the assumption")
    post(gate, value=SECOND_LABEL)

    with pytest.raises(answer.AnswerSupersedesVerdict) as raised:
        answer.refuse_superseded_merge(
            TICKET, repo=REPO, head="h1", language_code="en",
        )

    assert SECOND_LABEL in str(raised.value)
    assert "Add the assumption" not in str(raised.value)


def test_a_refused_last_answer_supersedes_the_earlier_one(monkeypatch):
    """The last word decides even when the stage refuses it: a person who
    changes their mind to a cancel between rounds is not carried out, and
    the answer they replaced is not carried out behind their back -- the
    gate holds no merge over either, and one receipt says why."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, TWO_WAYS, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")
    post(gh, action="cancel", value="cancel")

    assert answer.pending_change(
        TICKET, repo=REPO, head="h1", language_code="en",
    ) is None

    comments = gh.issues[TICKET]["comments"]
    issue = {"number": TICKET, "title": "Note app", "body": BODY}
    fresh = [dict(comment, issue=TICKET) for comment in comments]

    assert answer.apply_steering(
        fresh, issue=issue, repo=REPO, comments=comments, body=BODY,
        language_code="en", run_id="abc12345",
    ) is None
    assert gh.issues[TICKET]["body"] == BODY
    assert successes(gh) == []
    assert "ai-blocked" in rejection(gh)


# --------------------------------------------------------------------------
# one answer per question: a changed answer replaces the earlier amendment
# (Issue #2241)
# --------------------------------------------------------------------------

# The second question the same ticket asks after it stops again: its own
# options and its own id, so its criterion is a different decision.
NEXT_QUESTION = {
    "id": "q2",
    "kind": "choice",
    "summary": "The note's tag outlived the note's name.",
    "question": "Should a tag survive a rename?",
    "options": [
        {
            "label": "Keep the tag on rename",
            "description": SECOND_CRITERION,
            "action": "amend_requeue",
        },
        {
            "label": "Drop the tag on rename",
            "description": "A renamed note loses its tag.",
            "action": "amend_requeue",
        },
    ],
}

THIRD_LABEL = "Drop the tag on rename"
THIRD_CRITERION = "A renamed note loses its tag."


def _stop_again(gh: FakeGh, payload: dict = NEXT_QUESTION) -> None:
    """The ticket stops a second time and asks a NEW question (new id)."""
    gh.comment(
        TICKET,
        f"{RUN_MARKER}\n<!-- orbi:comment=failure -->\n\n"
        f"The delivery stopped again.\n\n"
        f"{question.render_stop(stop(payload), 'en')}",
        login="orbi-bot",
    )


def test_a_changed_answer_replaces_the_earlier_amendment(monkeypatch):
    """Acceptance 1 (Issue #2241): A is amended by one scan, B arrives after
    that amendment was written, and the next scan REPLACES A's criterion --
    the body never carries two contradictory acceptance lines."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, TWO_WAYS, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")

    stopped_scans.run(REPO, language_code="en")

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]

    post(gh, value=SECOND_LABEL)

    stopped_scans.run(REPO, language_code="en")

    body = gh.issues[TICKET]["body"]
    assert f"- {SECOND_CRITERION}" in body
    assert CRITERION not in body
    assert body.count(APPENDED_MARKER) == 1
    assert len(successes(gh)) == 2


def test_a_changed_answer_at_ai_blocked_replaces_the_earlier_amendment(
    monkeypatch, tmp_path,
):
    """Acceptance 1 through the real tick: the first answer is carried out
    and re-queues the ticket, then the SAME question is answered again and
    the earlier criterion goes."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, TWO_WAYS, labels=(BLOCKED_LABEL,), body=BODY)
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]

    # The delivery stops again on the same question and the person changes
    # their answer.
    gh.issues[TICKET]["labels"] = [BLOCKED_LABEL]
    post(gh, value=SECOND_LABEL)

    tick(monkeypatch, tmp_path, gh)

    body = gh.issues[TICKET]["body"]
    assert f"- {SECOND_CRITERION}" in body
    assert CRITERION not in body
    assert len(successes(gh)) == 2


def test_an_answer_to_another_question_keeps_both_criteria(monkeypatch):
    """Acceptance 2: two different questions answered on the same Issue --
    both sentences stay, because each belongs to its own question."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, TWO_WAYS, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")

    stopped_scans.run(REPO, language_code="en")

    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]

    _stop_again(gh)
    post(gh, value=SECOND_LABEL, q="q2")

    stopped_scans.run(REPO, language_code="en")

    body = gh.issues[TICKET]["body"]
    assert f"- {CRITERION}" in body
    assert f"- {SECOND_CRITERION}" in body
    assert body.count(APPENDED_MARKER) == 1
    assert len(successes(gh)) == 2

    # A third answer to that second question replaces only ITS criterion.
    post(gh, value=THIRD_LABEL, q="q2")

    stopped_scans.run(REPO, language_code="en")

    body = gh.issues[TICKET]["body"]
    assert f"- {CRITERION}" in body
    assert f"- {THIRD_CRITERION}" in body
    assert SECOND_CRITERION not in body


def test_a_changed_answer_leaves_the_tickets_own_text_untouched(monkeypatch):
    """Acceptance 3: everything outside the engine's amendment section is
    unchanged byte for byte, before and after the answer changes."""
    own = (
        "## Outcome\nUsers keep notes.\n\n"
        "## Acceptance\n- The notes list loads in under a second."
    )
    gh = _gh(monkeypatch)
    stopped_ticket(gh, TWO_WAYS, labels=(PR_OPENED_LABEL,), body=own)
    post(gh, value="Add the assumption")

    stopped_scans.run(REPO, language_code="en")

    post(gh, value=SECOND_LABEL)

    stopped_scans.run(REPO, language_code="en")

    body = gh.issues[TICKET]["body"]
    assert body.startswith(own)
    # The engine's section is the ONE block after the ticket's own text and
    # it states B alone.
    section = body[len(own):]
    assert f"- {SECOND_CRITERION}" in section
    assert CRITERION not in section


# --------------------------------------------------------------------------
# the decision rules: what one answer asks for, and what is refused
# --------------------------------------------------------------------------

TEXT_QUESTION = {
    "id": "q1",
    "kind": "text",
    "summary": "One fact decides the design.",
    "question": "Which tag model should the note keep?",
}


def _answer(*, value="Add the assumption", action="accept", q="q1",
            by="maintainer", reason=None):
    return question.Answer(question=q, action=action, value=value,
                           by=by, reason=reason)


def _refused(payload: dict, answer, stage: Optional[str]) -> str:
    """The readable reason one answer is not carried out."""
    with pytest.raises(answer_module.NotExecuted) as raised:
        answer_module.decide(
            answer, stop(payload), stage, language_code="en",
        )
    return raised.value.reason


@pytest.mark.parametrize("stage", [None, "ai-ready", "ai-merged"])
def test_an_answer_is_read_only_at_a_delivery_stage(stage):
    assert "no stop question is open" in _refused(
        CHOICE, _answer(), stage,
    )


def test_an_answer_to_another_question_is_not_carried_out():
    reason = _refused(CHOICE, _answer(q="q2"), BLOCKED_LABEL)
    assert "q2" in reason and "q1" in reason


def test_a_confirm_question_takes_yes_and_refuses_no_or_a_word():
    assert _refused(CONFIRM, _answer(value=False), BLOCKED_LABEL)
    reason = _refused(CONFIRM, _answer(value="yes"), BLOCKED_LABEL)
    assert "true" in reason
    decision = answer_module.decide(
        _answer(value=True), stop(CONFIRM), BLOCKED_LABEL,
        language_code="en",
    )
    assert (decision.action, decision.text) == (
        question.ACTION_AMEND_REQUEUE, CRITERION,
    )


def test_a_text_question_appends_the_answer_itself():
    decision = answer_module.decide(
        _answer(value="The tag belongs to the note"), stop(TEXT_QUESTION),
        BLOCKED_LABEL, language_code="en",
    )
    assert (decision.action, decision.text) == (
        question.ACTION_AMEND_REQUEUE,
        "The tag belongs to the note",
    )


def test_a_decline_needs_the_persons_own_words():
    assert "declining needs" in _refused(
        CHOICE, _answer(action="decline", value=""), BLOCKED_LABEL,
    )
    decision = answer_module.decide(
        _answer(action="decline", value="decline", reason="Use the notebook"),
        stop(CHOICE), BLOCKED_LABEL, language_code="en",
    )
    assert decision.text == "Use the notebook"


def test_a_link_option_is_a_persons_own_action():
    reason = _refused(
        CHOICE, _answer(value="Merge it on GitHub"), BLOCKED_LABEL,
    )
    assert "pull/7" in reason


def test_a_boolean_value_matches_no_option():
    reason = _refused(CHOICE, _answer(value=True), BLOCKED_LABEL)
    assert "`true`" in reason


@pytest.mark.parametrize("label", [PR_OPENED_LABEL, FIX_NEEDED_LABEL])
def test_a_closing_option_is_carried_out_only_at_ai_blocked(label):
    assert "ai-blocked" in _refused(
        CHOICE, _answer(value="Drop the work"), label,
    )
    assert "ai-blocked" in _refused(
        CHOICE, _answer(action="cancel", value="cancel"), label,
    )


def test_the_timeline_comparison_falls_back_to_the_stamps():
    older = {"createdAt": "2026-01-01T00:00:00Z"}
    newer = {"createdAt": "2026-01-02T00:00:00Z"}
    assert answer_module._newer(newer, older, []) is True
    assert answer_module._newer(older, newer, []) is False


def test_an_answer_block_is_read_as_the_block_and_two_are_unreadable():
    """Issue #2235: the occurrence IS the block, so the one parser
    (`question.parse_answer` over `comment_kind.parse_versioned_block`)
    validates the record and no payload validator of its own exists."""
    record = question.render_answer("q1", value="x", by="maintainer")
    assert answer_module.answer_occurrences(f"text\n{record}") == [record]
    assert question.parse_answer(record) == question.Answer(
        question="q1", action="accept", value="x", by="maintainer",
    )
    assert answer_module.answer_occurrences(f"{record}\n{record}") == [None]
    assert answer_module.answer_occurrences("no record here") == []
    # A comment without a body is no record either (the ticket's own read
    # hands over `comment.get("body")`).
    assert answer_module.answer_occurrences(None) == []
    # The engine's own `by: "orbi"` record is read like any other block: it
    # is engine OUTPUT (its run marker) that keeps it from being carried out,
    # and that rule lives in the occurrence layer, not here.
    engine = question.render_answer("q1", value="x")
    assert answer_module.answer_occurrences(engine) == [engine]
    assert ticket_command.carries_run_marker(
        {"body": f"{RUN_MARKER}\n{engine}"},
    )
    # A record whose JSON is broken is a readable refusal, never a skip.
    broken = '<!-- orbi:answer:v1 {"by": } -->'
    assert answer_module.answer_occurrences(broken) == [broken]



# --------------------------------------------------------------------------
# the refusals a person reads, and the paths only a real ticket reaches
# --------------------------------------------------------------------------

EXTERNAL_ONLY = {
    "id": "q1",
    "kind": "choice",
    "summary": "This contribution needs a decision.",
    "question": "What should happen to this contribution?",
    "options": [
        {
            "label": "Close the external PR",
            "description": "Close it and let Orbi build the change itself.",
            "action": "close_external_pr",
        },
        {
            "label": "Add the assumption",
            "description": CRITERION,
            "action": "amend_requeue",
        },
    ],
}

BROKEN_QUESTION = (
    f"{RUN_MARKER}\n<!-- orbi:comment=failure -->\n\n"
    'The delivery stopped.\n\n<!-- orbi:question:v1 {"schema": 1, -->'
)


def test_a_ticket_without_a_stop_question_cannot_be_answered(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    gh.add_issue(TICKET, title="Note app", body=BODY, labels=(BLOCKED_LABEL,))
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert "no stop question to answer" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY


def test_a_comment_carrying_two_answer_records_is_reported(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    record = _answer_record()
    gh.comment(TICKET, f"{record}\n{record}", login="maintainer")
    gh.add_collaborator("maintainer", "write")

    tick(monkeypatch, tmp_path, gh)

    receipts = [
        body for body in bodies(gh) if "orbi-answer-command-rejected" in body
    ]
    assert len(receipts) == 1, bodies(gh)
    assert gh.issues[TICKET]["body"] == BODY


def test_an_unreadable_stop_question_is_reported(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    gh.add_issue(TICKET, title="Note app", body=BODY, labels=(BLOCKED_LABEL,))
    gh.comment(TICKET, BROKEN_QUESTION, login="orbi-bot")
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert "unreadable" in rejection(gh)


def test_an_unreadable_answer_record_is_reported(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    gh.comment(
        TICKET, '<!-- orbi:answer:v1 {"q": "q1", -->', login="maintainer",
    )
    gh.add_collaborator("maintainer", "write")

    tick(monkeypatch, tmp_path, gh)

    assert "unreadable" in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY


def test_a_malformed_block_does_not_hide_the_valid_answer(
    monkeypatch, tmp_path,
):
    """One unreadable record is one receipt; the ticket's other answer is
    still read in the same scan (the two blocks are separate occurrences)."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    gh.comment(
        TICKET, '<!-- orbi:answer:v1 {"q": "q1", -->', login="maintainer",
    )
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert "unreadable" in rejection(gh)
    assert f"- {CRITERION}" in gh.issues[TICKET]["body"]
    assert len(successes(gh)) == 1


def test_validate_without_the_timeline_context_still_decides():
    assert answer_module._answer_validate(
        _answer_record(), stop=stop(CHOICE), answer_stage=BLOCKED_LABEL,
        language_code="en",
    ) is None


def test_a_between_rounds_answer_is_carried_out_once(monkeypatch):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(PR_OPENED_LABEL,))
    post(gh, value="Add the assumption")

    stopped_scans.run(REPO, language_code="en")
    comments = bodies(gh)
    stopped_scans.run(REPO, language_code="en")

    assert bodies(gh) == comments
    assert gh.issues[TICKET]["body"].count(CRITERION) == 1
    assert len(successes(gh)) == 1


def test_a_close_all_answer_without_any_pull_request_still_closes(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, internal_choice())
    post(gh, value="Drop the work")

    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["state"] == "closed"
    assert len(successes(gh)) == 1


def test_a_close_all_answer_never_closes_a_cross_repository_pr(
    monkeypatch, tmp_path,
):
    """Issue #2234: a fork PR on the delivery branch name is not THIS PR.

    The delivery PR is found by the shared branch reader, which drops a
    cross-repository result, so `close_all` closes the Issue and no PR.
    """
    gh = _gh(monkeypatch)
    gh.add_pr(9, head="orbi/acme-app-issue-100", cross_repository=True)
    stopped_ticket(gh, internal_choice())
    post(gh, value="Drop the work")

    tick(monkeypatch, tmp_path, gh)

    assert gh.prs[9]["state"] == "OPEN"
    assert gh.deleted_branches == set()
    assert gh.issues[TICKET]["state"] == "closed"
    assert len(successes(gh)) == 1


def test_a_close_external_pr_answer_without_a_pull_request_fails_the_step(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, EXTERNAL_ONLY)
    post(gh, value="Close the external PR")

    tick(monkeypatch, tmp_path, gh)

    failures = [body for body in bodies(gh) if "orbi-answer-command-failed" in body]
    assert len(failures) == 1
    assert "names no pull request" in failures[0]
    assert gh.issues[TICKET]["state"] == "open"


def test_an_answer_whose_criterion_is_already_there_only_receipts(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(
        gh,
        body=f"{BODY}\n## Acceptance\n- {CRITERION}\n\n{APPENDED_MARKER}\n",
    )
    post(gh, value="Add the assumption")

    tick(monkeypatch, tmp_path, gh)

    assert len(successes(gh)) == 1
    assert gh.issues[TICKET]["body"].count(CRITERION) == 1
    assert READY_LABEL in labels(gh)


def test_two_open_prs_on_the_delivery_branch_fail_the_step(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    gh.add_pr(8, head="orbi/acme-app-issue-100")
    gh.add_pr(9, head="orbi/acme-app-issue-100")
    stopped_ticket(gh, internal_choice())
    post(gh, value="Drop the work")

    tick(monkeypatch, tmp_path, gh)

    failures = [body for body in bodies(gh) if "orbi-answer-command-failed" in body]
    assert len(failures) == 1
    assert "multiple open PRs" in failures[0]



def test_pending_change_skips_engine_output_stale_and_untrusted_answers(
    monkeypatch,
):
    """The gate's read uses the scan's rules: a run-marked comment is engine
    output, an answer older than the newest stop is stale, an untrusted
    author is not a person, and a refusal is not a decision."""
    gh = _gh(monkeypatch)
    gh.add_collaborator("maintainer", "write")
    comments = _comments()
    comments.insert(0, {
        "id": "old-stop", "createdAt": "2026-11-17T00:00:00Z",
        "author": {"login": "orbi-bot"},
        "body": f"{RUN_MARKER}\n{_stop_body()}",
    })
    assert answer_module._pending(
        comments, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is not None
    # The same answer with an engine run marker on it is engine output.
    marked = [dict(comment) for comment in comments]
    marked[-1]["body"] = f"{RUN_MARKER}\n{marked[-1]['body']}"
    assert answer_module._pending(
        marked, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is None
    # An untrusted author is not a person.
    outsider = [dict(comment) for comment in comments]
    outsider[-1]["author"] = {"login": "drive-by"}
    assert answer_module._pending(
        outsider, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is None
    # An answer older than the newest stop is stale.
    newer_stop = dict(comments[0], id="new-stop")
    stale = [comments[0], comments[-1], newer_stop]
    assert answer_module._pending(
        stale, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is None
    # A cancel outside ai-blocked is refused, not a decision to hold for.
    cancel = [comments[0], dict(
        comments[-1],
        body=question.render_answer("q1", value="cancel", action="cancel",
                                    by="maintainer"),
    )]
    assert answer_module._pending(
        cancel, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is None
    # A ticket whose stop question cannot be read carries no decision.
    broken = [dict(comments[0], body=BROKEN_QUESTION), comments[-1]]
    assert answer_module._pending(
        broken, repo=REPO, stage=PR_OPENED_LABEL, language_code="en",
    ) is None
    # The engine's own receipt quotes the answer it rejected: that quoted
    # block is not an uncarried answer (Issue #2295).
    receipt = {
        "id": "receipt", "createdAt": "2026-11-17T00:00:03Z",
        "author": {"login": "maintainer"},
        "body": (
            "**not applied**\n\n"
            f"- argument: `{_answer_record()}`\n\n"
            "<!-- orbi-answer-command-rejected comment=answer -->"
        ),
    }
    assert answer_module._pending(
        [comments[0], receipt], repo=REPO, stage=PR_OPENED_LABEL,
        language_code="en",
    ) is None


def test_the_gate_holds_when_the_ticket_cannot_be_read(monkeypatch, tmp_path):
    """An unreadable ticket is no evidence that the decision still holds."""
    def unreadable(command, **kwargs):
        # The ticket read itself fails: no identity read is ever reached.
        raise RuntimeError("gh: HTTP 502")

    monkeypatch.setattr(seam, "run_command", unreadable)

    with pytest.raises(answer.AnswerSupersedesVerdict) as raised:
        answer.refuse_superseded_merge(
            TICKET, repo=REPO, head="h1", language_code="en",
        )

    assert "not performed" in str(raised.value)


def test_steering_reads_answers_and_receipts_what_it_cannot_do(
    monkeypatch,
):
    """The poll's reader: engine output and a malformed block are skipped, a
    refusal leaves its receipt, and a criterion already in the body is not
    written twice."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=())
    gh.comment(
        TICKET, f"{RUN_MARKER}\n{_answer_record()}", login="orbi-bot",
    )
    gh.comment(
        TICKET, '<!-- orbi:answer:v1 {"q": "q1", -->', login="maintainer",
    )
    gh.comment(
        TICKET,
        question.render_answer("q1", value="cancel", action="cancel",
                               by="maintainer"),
        login="maintainer",
    )
    comments = gh.issues[TICKET]["comments"]
    fresh = [dict(comment, issue=TICKET) for comment in comments[1:]]
    issue = {"number": TICKET, "title": "Note app", "body": BODY}

    # Engine output, the malformed block and the cancel change nothing; the
    # cancel leaves its one receipt.
    assert answer.apply_steering(
        fresh, issue=issue, repo=REPO, comments=comments, body=BODY,
        language_code="en",
    ) is None
    assert gh.issues[TICKET]["body"] == BODY
    rejected = [body for body in bodies(gh) if "orbi-answer-command-rejected" in body]
    # One receipt per answer the engine does not carry out: the unreadable
    # record and the cancel (a closing answer is not carried out here).
    assert len(rejected) == 2
    assert any("unreadable" in body for body in rejected)
    assert any("ai-blocked" in body for body in rejected)

    # The engine's own receipt quotes the answer it rejected: the poll does
    # not read that quoted block as a new answer (Issue #2295).
    receipt = {
        "id": "receipt", "author": {"login": "orbi-bot"},
        "body": (
            "**not applied**\n\n"
            f"- argument: `{_answer_record()}`\n\n"
            "<!-- orbi-answer-command-rejected comment=cancel -->"
        ),
    }
    before = bodies(gh)
    assert answer.apply_steering(
        [receipt], issue=issue, repo=REPO, comments=comments + [receipt],
        body=BODY, language_code="en",
    ) is None
    assert bodies(gh) == before

    # The same acceptance criterion a second time is not written again.
    accepted = [{"id": "accepted", "author": {"login": "maintainer"},
                 "body": _answer_record()}]
    assert answer.apply_steering(
        accepted, issue=issue, repo=REPO, comments=comments + accepted,
        body=f"{BODY}\n## Acceptance\n- {CRITERION}\n\n{APPENDED_MARKER}\n",
        language_code="en",
    ) is None
    # A no-op amendment is still a carried-out answer, so it leaves its one
    # receipt: without it the merge gate would hold a clean merge over a
    # decision the body already carries.
    assert len(successes(gh)) == 1
    # A comment carrying two answer blocks, and a ticket whose question is
    # gone, are refusals with a receipt -- never a silent skip.
    doubled = [{"id": "double", "author": {"login": "maintainer"},
                "body": f"{_answer_record()}\n{_answer_record()}"}]
    assert answer.apply_steering(
        doubled, issue=issue, repo=REPO, comments=comments, body=BODY,
        language_code="en",
    ) is None
    orphan = [{"id": "orphan", "author": {"login": "maintainer"},
               "body": _answer_record()}]
    assert answer.apply_steering(
        orphan, issue=issue, repo=REPO, comments=comments[1:], body=BODY,
        language_code="en",
    ) is None
    broken = [{"id": "broken-stop", "author": {"login": "orbi-bot"},
               "body": BROKEN_QUESTION}]
    assert answer.apply_steering(
        orphan, issue=issue, repo=REPO, comments=broken, body=BODY,
        language_code="en",
    ) is None
    reasons = [
        body for body in bodies(gh) if "orbi-answer-command-rejected" in body
    ]
    assert any("ONE answer record" in body for body in reasons), reasons
    assert any("no stop question" in body for body in reasons), reasons
    assert any("unreadable" in body for body in reasons), reasons



# --------------------------------------------------------------------------
# the answer record parser (Issues #2221, #2235): ONE implementation
# --------------------------------------------------------------------------

def test_the_answer_record_has_one_validator():
    """Issue #2235: the hand-rolled payload validator is gone, so a record is
    validated by the strict marker parser alone (`parse_versioned_block`)."""
    assert not hasattr(question, "parse_answer_payload")


@pytest.mark.parametrize("payload, expected", [
    ("", "not valid JSON"),
    ("[]", "JSON object"),
    ('{"q": "q1", "action": "accept", "value": "x", "by": "m", "x": 1}',
     "unknown fields"),
    ('{"q": "q1", "action": "accept", "value": "x"}', "missing fields"),
    ('{"q": "q1", "action": "maybe", "value": "x", "by": "m"}',
     "unknown answer action"),
])
def test_an_unreadable_answer_block_is_refused_with_its_reason(
    payload, expected,
):
    """The refusal a person reads comes from the ONE parser, through the
    command's own `validate` seam."""
    reason = answer_module._answer_validate(
        f"<!-- orbi:answer:v1 {payload} -->", stop=stop(CHOICE),
        answer_stage=BLOCKED_LABEL, language_code="en",
    )
    assert reason is not None and expected in reason


def test_an_occurrence_without_a_record_is_refused_readably():
    reason = answer_module._answer_validate(
        "a comment that only mentions orbi:answer:v1 in prose",
        stop=stop(CHOICE), answer_stage=BLOCKED_LABEL, language_code="en",
    )
    assert reason is not None and "orbi:answer:v1" in reason


def test_latest_stop_reads_the_newest_question_and_skips_other_comments():
    comments = [
        {"body": "a plain comment"},
        {"body": f"{RUN_MARKER}\n{_stop_body()}"},
        "not a comment",
    ]
    located = question.latest_stop(comments)
    assert located is not None and located[1].question.id == "q1"
    assert question.latest_stop([{"body": "no question"}]) is None


def test_a_run_marker_is_read_from_a_comment_body_only():
    assert ticket_command.carries_run_marker(None) is False
    assert ticket_command.carries_run_marker("x") is False
    assert ticket_command.carries_run_marker({"body": None}) is False
    assert ticket_command.carries_run_marker(
        {"body": "not marked"},
    ) is False
    assert ticket_command.carries_run_marker(
        {"body": f"{RUN_MARKER}\nbody"},
    ) is True


def test_a_command_receipt_is_recognized_by_its_trailing_marker():
    """Issue #2295: the receipt is engine output, whatever it quotes."""
    marker = ticket_command.command_receipt_marker(answer.ANSWER_COMMAND)
    receipt = (
        "**`/orbi answer` command not applied**\n\n"
        "- argument: `<!-- orbi:answer:v1 {} -->`\n\n"
        "<!-- orbi-answer-command-rejected comment=123 -->"
    )
    assert ticket_command.carries_command_receipt(
        {"body": receipt}, marker,
    ) is True
    assert ticket_command.carries_command_receipt(
        {"body": "<!-- orbi-answer-command comment=123 -->"}, marker,
    ) is True
    assert ticket_command.carries_command_receipt(
        {"body": "<!-- orbi-answer-command-failed step=read comment=123 -->"},
        marker,
    ) is True
    # A person's longer comment quoting a receipt is still a person's.
    assert ticket_command.carries_command_receipt(
        {"body": f"See this:\n{receipt}\n\nwhy?"}, marker,
    ) is False
    # Another command's receipt is not this command's output.
    assert ticket_command.carries_command_receipt(
        {"body": "<!-- orbi-fix_base-command comment=123 -->"}, marker,
    ) is False
    # A marker-shaped last line with no closing delimiter is not a receipt.
    assert ticket_command.carries_command_receipt(
        {"body": "<!-- orbi-answer-command comment=123"}, marker,
    ) is False
    assert ticket_command.carries_command_receipt(None, marker) is False
    assert ticket_command.carries_command_receipt(
        {"body": None}, marker,
    ) is False

