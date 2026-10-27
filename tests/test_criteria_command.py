"""`/orbi add-criteria`: the proposed acceptance criteria start the ticket
(Issue #1929).

Every scenario here drives the REAL tick entry (`claim.pick_next_delivery`)
over the in-memory gh fake: the stopped ticket is picked up by the
`ai-needs-detail` scan (the same one `/orbi split` uses), the real command
layer checks the caller's write permission, and the real GitHub adapter edits
the body and the labels. What is asserted is the observable result — the
fake's Issue body, labels and comments — never an internal call sequence.
"""
from __future__ import annotations

import subprocess

import pytest

import orbi.criteria_command as criteria_command
from orbi import claim, clarify, journal, language, ticket_command
from orbi.delivery_labels import NEEDS_DETAIL_LABEL, READY_LABEL
from orbi.resume_scan import ResumeHooks
from seam import seam

from tests.fakes.github import FakeGh

REPO = "acme/app"
TICKET = 100
RUN_MARKER = "<!-- orbi:run=abcdef12 -->"
STOP_MARKER = "<!-- orbi:clarify-stop:" + "0" * 64 + " -->"
BODY = "## Outcome\nUsers keep notes.\n"
APPENDED_MARKER = f"<!-- {criteria_command.ACCEPTANCE_MARKER} -->"


# --------------------------------------------------------------------------
# fixtures: a stopped ticket over the gh fake
# --------------------------------------------------------------------------

def criteria(*texts: str) -> clarify.Suggestion:
    return clarify.Suggestion(
        kind=clarify.SUGGESTION_CRITERIA,
        items=tuple(clarify.SuggestionItem(text=text) for text in texts),
    )


TWO_CRITERIA = criteria(
    "Adding a tag makes the note findable by filtering",
    "The tag survives a reload",
)

SPLIT = clarify.Suggestion(
    kind=clarify.SUGGESTION_SPLIT,
    items=(
        clarify.SuggestionItem(
            text="Users can tag a note",
            acceptance="filtering by the tag finds the note",
        ),
        clarify.SuggestionItem(
            text="Users can export a note",
            acceptance="Export downloads a .md file",
            after=1,
        ),
    ),
)


def stop_body(section: str) -> str:
    """The formal stop prefix plus the rendered (or hand-written) proposal."""
    return (
        f"{RUN_MARKER}\n{STOP_MARKER}\n\n"
        "This ticket needs acceptance criteria.\n\n" + section
    )


def _gh(monkeypatch) -> FakeGh:
    """The in-memory GitHub boundary: the ONE seam these tests replace."""
    gh = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def stopped_ticket(
    gh: FakeGh, *, section: str | None = None,
    labels: tuple[str, ...] = (NEEDS_DETAIL_LABEL,), body: str = BODY,
) -> None:
    """The ticket the clarification gate stopped, with its trust chain."""
    rendered = (
        section if section is not None
        else clarify.render_suggestion(TWO_CRITERIA, "en")
    )
    gh.add_issue(TICKET, title="Note app", body=body, labels=labels)
    gh.comment(TICKET, stop_body(rendered), login="orbi-bot")


def reply(gh: FakeGh, *, login: str = "maintainer",
          command: str = "/orbi add-criteria", permission: str = "write",
          number: int = TICKET) -> None:
    gh.comment(number, command, login=login)
    gh.add_collaborator(login, permission)


def _hooks() -> ResumeHooks:
    noop = lambda *args, **kwargs: None
    return ResumeHooks(
        resume_scene=noop,
        route_external_pr_ticket=lambda *args, **kwargs: False,
        block_scene_failure=noop,
        recover_missing_pr_scene=noop,
        has_recoverable_pr_scene=lambda *args, **kwargs: False,
        comment_pr=noop,
    )


def tick(monkeypatch, tmp_path, gh: FakeGh):
    """One real Runner tick over the fake GitHub."""
    return claim.pick_next_delivery([REPO], tmp_path, 1, hooks=_hooks())


def comment_bodies(gh: FakeGh, number: int = TICKET) -> list[str]:
    return [comment["body"] for comment in gh.issues[number]["comments"]]


def labels(gh: FakeGh, number: int = TICKET) -> list[str]:
    return gh.issues[number]["labels"]


def receipt_marker(outcome: str) -> str:
    marker = ticket_command.command_receipt_marker(
        criteria_command.ADD_CRITERIA_COMMAND,
    )
    return f"<!-- {marker}{outcome} comment="


def success_receipts(gh: FakeGh) -> list[str]:
    """The command's own success receipts (never a rejected/failed one)."""
    return [
        body for body in comment_bodies(gh) if receipt_marker("") in body
    ]


def receipt_for(key: str) -> str:
    """The hidden marker THIS command's success receipt carries."""
    marker = ticket_command.command_receipt_marker(
        criteria_command.ADD_CRITERIA_COMMAND,
    )
    return f"<!-- {marker} comment={key} -->"


def command_key(gh: FakeGh) -> str:
    """The key of the ticket's `/orbi add-criteria` comment."""
    return ticket_command.command_comment_key(
        gh.issues[TICKET]["comments"][-1]
    )


def receipt_comments(gh: FakeGh, key: str) -> list[dict]:
    return [
        comment for comment in gh.issues[TICKET]["comments"]
        if receipt_for(key) in comment["body"]
    ]


def failures(gh: FakeGh) -> list[str]:
    return [
        body for body in comment_bodies(gh)
        if "orbi-add_criteria-command-failed" in body
    ]


def rejection(gh: FakeGh) -> str:
    receipts = [
        body for body in comment_bodies(gh)
        if "orbi-add_criteria-command-rejected" in body
    ]
    assert len(receipts) == 1, comment_bodies(gh)
    return receipts[0]


def fail_on(gh: FakeGh, predicate,
            *, reason: str = "gh: HTTP 422: Validation Failed"):
    """One failing gh write, the way a real refusal arrives."""
    def call(command, **kwargs):
        if predicate(command):
            raise subprocess.CalledProcessError(1, command, stderr=reason)
        return gh(command, **kwargs)
    return call


def label_add(command: list[str]) -> bool:
    return (
        command[1] == "issue" and command[2] == "edit"
        and READY_LABEL in command
    )


def label_remove(command: list[str]) -> bool:
    return (
        command[1] == "issue" and command[2] == "edit"
        and NEEDS_DETAIL_LABEL in command
    )


# --------------------------------------------------------------------------
# the appended section: render and append
# --------------------------------------------------------------------------

def test_the_appended_section_carries_the_heading_lines_and_marker():
    assert criteria_command.render_acceptance(TWO_CRITERIA.items) == (
        "## Acceptance\n"
        "- Adding a tag makes the note findable by filtering\n"
        "- The tag survives a reload\n"
        "\n"
        "<!-- orbi:acceptance:v1 -->"
    )


@pytest.mark.parametrize("body, expected", [
    ("## Outcome\nUsers keep notes.\n",
     "## Outcome\nUsers keep notes.\n\n## Acceptance"),
    ("## Outcome\nUsers keep notes.", "## Outcome\nUsers keep notes.\n\n##"),
    ("", "## Acceptance"),
    (None, "## Acceptance"),
])
def test_the_section_is_appended_after_the_existing_body(body, expected):
    section = criteria_command.render_acceptance(TWO_CRITERIA.items)
    assert criteria_command.appended_body(body, section).startswith(expected)


# --------------------------------------------------------------------------
# acceptance 1: the body gains the section once, labels, one receipt
# --------------------------------------------------------------------------

def test_the_criteria_are_appended_once_and_the_ticket_is_queued(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)

    assert tick(monkeypatch, tmp_path, gh) is not None

    body = gh.issues[TICKET]["body"]
    assert body.startswith("## Outcome\nUsers keep notes.")
    assert body.count(APPENDED_MARKER) == 1
    assert body.count("## Acceptance") == 1
    assert "- Adding a tag makes the note findable by filtering" in body
    assert "- The tag survives a reload" in body
    assert READY_LABEL in labels(gh)
    assert NEEDS_DETAIL_LABEL not in labels(gh)
    receipts = success_receipts(gh)
    assert len(receipts) == 1
    assert "Adding a tag makes the note findable by filtering" in receipts[0]
    assert "The tag survives a reload" in receipts[0]


def test_the_queued_ticket_is_claimed_by_the_same_tick(monkeypatch, tmp_path):
    """`... and start`: the criteria land before the claim scans run, so the
    ticket leaves the stop scan and is picked as a ready delivery."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)

    selected = tick(monkeypatch, tmp_path, gh)

    assert selected is not None
    repo, issue, _scene = selected
    assert (repo, issue["number"]) == (REPO, TICKET)


# --------------------------------------------------------------------------
# acceptance 2: the edited comment is what gets appended
# --------------------------------------------------------------------------

def test_an_edited_proposal_is_what_gets_appended(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    edited = criteria(
        "Filtering by a tag lists the tagged note",
        "Untagging the note removes it from the filtered list",
        "The tag survives a reload",
    )
    # A person edits the stop comment before replying: GitHub keeps the
    # edited body, so the command reads the new lines.
    gh.issues[TICKET]["comments"][0]["body"] = stop_body(
        clarify.render_suggestion(edited, "en")
    )
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    body = gh.issues[TICKET]["body"]
    assert "- Filtering by a tag lists the tagged note" in body
    assert "- Untagging the note removes it from the filtered list" in body
    assert "- Adding a tag makes the note findable by filtering" not in body


# --------------------------------------------------------------------------
# acceptance 3: a failed step continues on the next tick
# --------------------------------------------------------------------------

def test_a_failed_label_step_continues_on_the_next_tick(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(gh, label_add))

    tick(monkeypatch, tmp_path, gh)

    reported = failures(gh)
    assert len(reported) == 1
    assert language.text(
        "en", "command.failed_step", step="add the dispatch label",
    ) in reported[0]
    assert "gh: HTTP 422: Validation Failed" in reported[0]
    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1
    assert READY_LABEL not in labels(gh)
    assert NEEDS_DETAIL_LABEL in labels(gh)
    assert success_receipts(gh) == []

    # GitHub answers again: the SAME command finishes the remaining steps
    # over what is already on the ticket.
    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1
    assert READY_LABEL in labels(gh)
    assert NEEDS_DETAIL_LABEL not in labels(gh)
    assert len(success_receipts(gh)) == 1


# --------------------------------------------------------------------------
# acceptance 4: a failed receipt is posted once on the next tick
# --------------------------------------------------------------------------

def test_a_failed_receipt_write_is_finished_on_the_next_tick(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: command[1:3] == ["issue", "comment"],
    ))

    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1
    assert READY_LABEL in labels(gh)
    assert success_receipts(gh) == []
    # The scan label stays, so the next tick sees the command again.
    assert NEEDS_DETAIL_LABEL in labels(gh)

    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL not in labels(gh)


def test_a_failed_label_removal_finishes_next_tick_without_a_second_receipt(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(gh, label_remove))

    tick(monkeypatch, tmp_path, gh)

    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL in labels(gh)

    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert NEEDS_DETAIL_LABEL not in labels(gh)
    assert len(success_receipts(gh)) == 1
    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1


# --------------------------------------------------------------------------
# acceptance 5: the rejections write nothing
# --------------------------------------------------------------------------

def test_a_caller_without_write_permission_is_rejected(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh, login="stranger", permission="read")

    tick(monkeypatch, tmp_path, gh)

    assert language.text(
        "en", "command.reason_no_permission", permission="write",
        observed="read",
    ) in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY
    assert NEEDS_DETAIL_LABEL in labels(gh)


def test_an_invalid_proposal_line_is_rejected_with_its_number(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    # A criteria item must be a "- <condition>" line: the body's line 8.
    section = "\n".join([
        "<!-- orbi:suggestion:v1 kind=criteria -->",
        "- The tag survives a reload",
        "1. Users can tag a note",
        "<!-- /orbi/suggestion -->",
        "",
        "Editing this comment changes the proposal.",
    ])
    stopped_ticket(gh, section=section)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text("en", "criteria.reason_invalid_line", line=8) in (
        rejection(gh)
    )
    assert gh.issues[TICKET]["body"] == BODY
    assert NEEDS_DETAIL_LABEL in labels(gh)


def test_an_empty_section_has_no_criteria_to_add(monkeypatch, tmp_path):
    """`at least one line`: an empty section is the end marker's line."""
    gh = _gh(monkeypatch)
    section = "\n".join([
        "<!-- orbi:suggestion:v1 kind=criteria -->",
        "<!-- /orbi/suggestion -->",
    ])
    stopped_ticket(gh, section=section)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text("en", "criteria.reason_invalid_line", line=7) in (
        rejection(gh)
    )
    assert gh.issues[TICKET]["body"] == BODY


def test_a_split_proposal_is_not_acceptance_criteria(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, section=clarify.render_suggestion(SPLIT, "en"))
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text(
        "en", "criteria.reason_not_criteria", kind="split",
    ) in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY


def test_a_stop_without_a_proposal_is_rejected(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, section="")
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text("en", "criteria.reason_no_suggestion") in rejection(gh)
    assert gh.issues[TICKET]["body"] == BODY


# --------------------------------------------------------------------------
# a copied completion receipt is never a completion (Issue #1944)
# --------------------------------------------------------------------------

def test_a_copied_success_receipt_is_not_a_completion(monkeypatch, tmp_path):
    """The marker is public text: a read-only commenter can copy it.

    Reading the marker without its author made the authorized command look
    done — no append, no dispatch label, the ticket lost
    `ai-needs-detail` and left the stopped scan unqueued, and the only
    "receipt" was the stranger's copy (Issue #1944).
    """
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    key = command_key(gh)
    gh.comment(
        TICKET, receipt_for(key), login="stranger", association="NONE",
    )
    gh.add_collaborator("stranger", "read")

    assert tick(monkeypatch, tmp_path, gh) is not None

    # The authorized command runs: the criteria land, the ticket is queued
    # and the genuine receipt is the runner's own, beside the copied one.
    body = gh.issues[TICKET]["body"]
    assert body.count(APPENDED_MARKER) == 1
    assert "- Adding a tag makes the note findable by filtering" in body
    assert "- The tag survives a reload" in body
    assert READY_LABEL in labels(gh)
    assert NEEDS_DETAIL_LABEL not in labels(gh)
    receipts = receipt_comments(gh, key)
    assert [comment["author"]["login"] for comment in receipts] == [
        "stranger", gh.login,
    ]
    assert "Adding a tag makes the note findable by filtering" in (
        receipts[1]["body"]
    )

    # A second tick has nothing left to do: the label is gone and the
    # runner's receipt is now the completion record.
    tick(monkeypatch, tmp_path, gh)
    assert gh.issues[TICKET]["body"].count(APPENDED_MARKER) == 1
    assert len(receipt_comments(gh, key)) == 2


@pytest.mark.parametrize("author", [
    {"login": "stranger"},   # a different account
    {},                      # an unreadable author payload
    None,
])
def test_an_untrusted_receipt_author_does_not_authenticate(
    monkeypatch, author,
):
    """Only the authenticated account's own comment is a completion."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    comment = {"body": receipt_for("1")}
    if author is not None:
        comment["author"] = author

    criteria_command.ADD_CRITERIA_COMMAND.apply(
        "", repo=REPO, issue_number=TICKET, command_comment={"id": "1"},
        comments=[comment],
        issue={
            "number": TICKET, "body": BODY,
            "labels": [{"name": NEEDS_DETAIL_LABEL}],
        },
        suggestion=TWO_CRITERIA, language_code="en",
    )

    # The command executed instead of trusting the copied record.
    assert gh.issues[TICKET]["body"].count("## Acceptance") == 1
    assert NEEDS_DETAIL_LABEL not in labels(gh)
    assert len(success_receipts(gh)) == 1


@pytest.mark.parametrize("author_shape", [
    "{login}",
    "{login}[bot]",     # REST keeps the App suffix
    "app/{login}",      # the issue-author payload shape
])
def test_a_runner_receipt_in_every_payload_shape_authenticates(
    monkeypatch, author_shape,
):
    """The three shapes name one account (Issue #1927): the runner's own
    receipt on an interrupted-final-label retry finishes the label only."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(READY_LABEL, NEEDS_DETAIL_LABEL),
                   body=f"{BODY}\n{APPENDED_MARKER}")
    comments = [{
        "body": receipt_for("1"),
        "author": {"login": author_shape.format(login=gh.login)},
    }]
    before = comment_bodies(gh)

    criteria_command.ADD_CRITERIA_COMMAND.apply(
        "", repo=REPO, issue_number=TICKET, command_comment={"id": "1"},
        comments=comments,
        issue={
            "number": TICKET, "body": f"{BODY}\n{APPENDED_MARKER}",
            "labels": [{"name": READY_LABEL}, {"name": NEEDS_DETAIL_LABEL}],
        },
        suggestion=TWO_CRITERIA, language_code="en",
    )

    assert comment_bodies(gh) == before
    assert NEEDS_DETAIL_LABEL not in labels(gh)


# --------------------------------------------------------------------------
# the scan and the registration
# --------------------------------------------------------------------------

def test_only_ai_needs_detail_tickets_are_scanned(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(READY_LABEL,))
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert gh.issues[TICKET]["body"] == BODY
    assert success_receipts(gh) == []
    assert [
        body for body in comment_bodies(gh)
        if "orbi-add_criteria-command-rejected" in body
    ] == []


def test_the_command_is_registered_with_the_shared_layer():
    spec = ticket_command.TICKET_COMMANDS["add_criteria"]
    assert spec is criteria_command.ADD_CRITERIA_COMMAND
    assert (spec.name, spec.subcommand, spec.arity, spec.permission) == (
        "add_criteria", "add-criteria", 0, "write",
    )
    assert spec.usage == "/orbi add-criteria"
    assert spec.aliases == ()
    assert "add_criteria_command_applied" in journal.JOURNAL_EVENTS
    assert "add_criteria_command_failed" in journal.JOURNAL_EVENTS


def test_a_ticket_that_already_carries_the_success_receipt_only_checks(
    monkeypatch,
):
    """A resumed caller whose ticket already carries the success receipt and
    lost the scan label changes nothing."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    marker = ticket_command.command_receipt_marker(
        criteria_command.ADD_CRITERIA_COMMAND,
    )
    before = comment_bodies(gh)
    criteria_command.ADD_CRITERIA_COMMAND.apply(
        "", repo=REPO, issue_number=TICKET, command_comment={"id": 1},
        comments=[{
            "body": f"<!-- {marker} comment=1 -->",
            "author": {"login": gh.login},
        }],
        issue={"number": TICKET, "body": BODY, "labels": []},
        suggestion=TWO_CRITERIA, language_code="en",
    )
    assert gh.issues[TICKET]["body"] == BODY
    assert comment_bodies(gh) == before
    assert success_receipts(gh) == []


def test_apply_refuses_a_ticket_without_a_proposal(monkeypatch):
    """The dispatcher validates first; the handler still names the step when
    a caller hands it nothing to append (the CLI entry's own guard)."""
    _gh(monkeypatch)
    with pytest.raises(ticket_command.TicketCommandError) as excinfo:
        criteria_command.ADD_CRITERIA_COMMAND.apply(
            "", repo=REPO, issue_number=TICKET, command_comment={"id": 1},
            comments=[], issue={"number": TICKET, "labels": []},
            suggestion=None, language_code="en",
        )
    assert excinfo.value.step == "append the criteria"


def test_the_terminal_spelling_runs_the_same_append(monkeypatch):
    """`orbi add-criteria --repo … --issue N` (Issue #1930) is the same
    execution path: the host login is the one occurrence and the ticket
    itself supplies the proposal."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    gh.add_collaborator("orbi-bot", "admin")

    outcome = ticket_command.execute_command(
        REPO, TICKET, criteria_command.ADD_CRITERIA_COMMAND,
        caller=ticket_command.cli_caller("orbi-bot"),
        dispatch_label=READY_LABEL, language_code="en",
    )

    assert outcome.applied is True
    assert gh.issues[TICKET]["body"].count("## Acceptance") == 1
    assert NEEDS_DETAIL_LABEL not in labels(gh)
    receipts = success_receipts(gh)
    assert len(receipts) == 1
    # The receipt names the terminal occurrence it answers, exactly like a
    # comment's receipt names the comment.
    assert "comment=cli@" in receipts[0]


def test_the_context_reads_the_ticket_and_its_proposal(monkeypatch):
    """The comment scan hands neither over, so the command reads both from
    the ticket itself; a caller that already has them costs no second read."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    comments = gh.issues[TICKET]["comments"]
    context = criteria_command.ADD_CRITERIA_COMMAND.context(
        repo=REPO, issue_number=TICKET, comments=comments,
    )
    assert context["issue"]["number"] == TICKET
    assert context["suggestion"] == TWO_CRITERIA
    supplied = criteria_command.ADD_CRITERIA_COMMAND.context(
        repo=REPO, issue_number=TICKET, comments=[],
        issue={"number": TICKET}, suggestion=TWO_CRITERIA,
    )
    assert supplied == {
        "issue": {"number": TICKET}, "suggestion": TWO_CRITERIA,
    }
