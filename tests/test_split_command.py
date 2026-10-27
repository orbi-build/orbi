"""`/orbi split`: the proposed split becomes child tickets (Issue #1927).

Every scenario here drives the REAL tick entry (`claim.pick_next_delivery`)
over the in-memory gh fake: the stopped ticket is picked up by the
`ai-needs-detail` scan, the real command layer checks the caller's write
permission, and the real GitHub adapter opens, links and labels the
children. What is asserted is the observable result — the fake's Issues,
relations, labels and comments — never an internal call sequence.
"""
from __future__ import annotations

import subprocess

import pytest

import orbi.split_command as split_command
from orbi import claim, clarify, journal, language, ticket_command
from orbi.delivery_labels import (
    BUG_LABEL,
    EPIC_LABEL,
    NEEDS_DETAIL_LABEL,
    P0_LABEL,
    READY_LABEL,
)
from orbi.resume_scan import ResumeHooks
from seam import seam

from tests.fakes.github import FakeGh

REPO = "acme/app"
PARENT = 100
STOP_END = "<!-- /orbi/suggestion -->"
RUN_MARKER = "<!-- orbi:run=abcdef12 -->"
STOP_MARKER = "<!-- orbi:clarify-stop:" + "0" * 64 + " -->"


# --------------------------------------------------------------------------
# fixtures: a stopped ticket over the gh fake
# --------------------------------------------------------------------------

def item(text: str, acceptance: str,
         after: int | None = None) -> clarify.SuggestionItem:
    return clarify.SuggestionItem(text=text, acceptance=acceptance, after=after)


def proposal(*items: clarify.SuggestionItem) -> clarify.Suggestion:
    return clarify.Suggestion(kind=clarify.SUGGESTION_SPLIT, items=items)


TWO_ITEMS = proposal(
    item("Users can tag a note", "filtering by the tag finds the note"),
    item("Users can export a note", "Export downloads a .md file", after=1),
)


def stop_body(section: str, *,
              prose: str = "This ticket needs a split.") -> str:
    """The formal stop prefix plus a rendered (or hand-written) proposal."""
    return f"{RUN_MARKER}\n{STOP_MARKER}\n\n{prose}\n\n{section}"


def _gh(monkeypatch) -> FakeGh:
    """The in-memory GitHub boundary: the ONE seam these tests replace."""
    gh = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", gh)
    return gh


def stopped_ticket(
    gh: FakeGh, *, section: str | None = None,
    labels: tuple[str, ...] = (NEEDS_DETAIL_LABEL, BUG_LABEL, P0_LABEL),
    parent: int | None = None, milestone: int | None = None,
    prose: str = "This ticket needs a split.",
) -> None:
    """The ticket the clarification gate stopped, with its trust chain."""
    rendered = (
        clarify.render_suggestion(TWO_ITEMS, "en")
        if section is None else section
    )
    gh.add_issue(
        PARENT, title="Note app", body="## Outcome\nUsers keep notes.\n",
        labels=labels, parent=parent, milestone=milestone,
    )
    gh.comment(PARENT, stop_body(rendered, prose=prose), login="orbi-bot")


def reply(gh: FakeGh, *, login: str = "maintainer",
          command: str = "/orbi split", permission: str = "write",
          number: int = PARENT) -> None:
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


def comment_bodies(gh: FakeGh, number: int = PARENT) -> list[str]:
    return [comment["body"] for comment in gh.issues[number]["comments"]]


def success_receipts(gh: FakeGh) -> list[str]:
    """The command's own success receipts (never a rejected/failed one)."""
    marker = (
        f"<!-- {ticket_command.command_receipt_marker(split_command.SPLIT_COMMAND)}"
        " comment="
    )
    return [body for body in comment_bodies(gh) if marker in body]


def failures(gh: FakeGh) -> list[str]:
    return [
        body for body in comment_bodies(gh)
        if "orbi-split-command-failed" in body
    ]


def rejection(gh: FakeGh) -> str:
    receipts = [
        body for body in comment_bodies(gh)
        if "orbi-split-command-rejected" in body
    ]
    assert len(receipts) == 1, comment_bodies(gh)
    return receipts[0]


def children(gh: FakeGh) -> list[dict]:
    """The issues attached as sub-issues of the stopped ticket."""
    return [
        issue for _, issue in sorted(gh.issues.items())
        if issue["parent"] == PARENT
    ]


def titles(gh: FakeGh) -> list[str]:
    return [issue["title"] for issue in children(gh)]


def fail_on(gh: FakeGh, predicate,
            *, reason: str = "gh: HTTP 422: Validation Failed"):
    """One failing gh write, the way a real refusal arrives."""
    def call(command, **kwargs):
        if predicate(command):
            raise subprocess.CalledProcessError(1, command, stderr=reason)
        return gh(command, **kwargs)
    return call


# --------------------------------------------------------------------------
# the child body: render + parse
# --------------------------------------------------------------------------

def test_the_child_body_carries_the_item_acceptance_origin_and_marker():
    body = split_command.render_child_body(
        item("Users can tag a note", "filtering by the tag finds the note"),
        parent=PARENT, item_number=2,
        parent_body="## Outcome\nUsers keep notes.",
    )
    assert body == (
        "## Outcome\n"
        "Users can tag a note\n"
        "\n"
        "## Acceptance\n"
        "- filtering by the tag finds the note\n"
        "\n"
        "Split from #100\n"
        "\n"
        "<details><summary>Parent ticket text</summary>\n"
        "\n"
        "## Outcome\n"
        "Users keep notes.\n"
        "\n"
        "</details>\n"
        "\n"
        "<!-- orbi:split-child:100:2 -->"
    )
    assert split_command.parse_child_body(body) == split_command.SplitChild(
        parent=PARENT, item=2, outcome="Users can tag a note",
        acceptance=("filtering by the tag finds the note",),
    )


def test_the_child_body_survives_an_empty_parent_body():
    body = split_command.render_child_body(
        item("Users can tag a note", "the tag survives a reload"),
        parent=PARENT, item_number=1, parent_body="",
    )
    assert "(the parent ticket has no body)" in body


@pytest.mark.parametrize("body", [
    None,
    "",
    "## Outcome\nno marker at all",
    "<!-- orbi:split-child:100:1 -->\n\nno sections",
    "<!-- orbi:split-child:100:1 -->\n\n## Outcome\ntext only\n",
])
def test_a_body_without_the_child_shape_is_not_a_child(body):
    assert split_command.parse_child_body(body) is None


# --------------------------------------------------------------------------
# acceptance 1: a two-item proposal becomes two linked, queued children
# --------------------------------------------------------------------------

def test_a_two_item_proposal_creates_two_linked_queued_children(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    gh.add_milestone(3, title="v1", open_issues=5)
    stopped_ticket(gh, milestone=3)
    reply(gh)

    assert tick(monkeypatch, tmp_path, gh) is not None

    kids = children(gh)
    assert [kid["title"] for kid in kids] == [
        "Users can tag a note", "Users can export a note",
    ]
    first, second = kids
    # The marker names the parent and the item, so a re-run finds them.
    assert f"<!-- orbi:split-child:{PARENT}:1 -->" in first["body"]
    assert f"<!-- orbi:split-child:{PARENT}:2 -->" in second["body"]
    assert f"Split from #{PARENT}" in second["body"]
    assert "Users keep notes." in second["body"]   # the collapsed parent text
    # Native relations: both are sub-issues, one blockedBy edge.
    assert first["parent"] == second["parent"] == PARENT
    assert second["blocked_by"] == [
        {"number": first["number"], "state": "OPEN"},
    ]
    assert first["blocked_by"] == []
    # Both children are queued; the parent is the epic and left the scan.
    assert READY_LABEL in first["labels"]
    assert READY_LABEL in second["labels"]
    assert EPIC_LABEL in gh.issues[PARENT]["labels"]
    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]
    assert READY_LABEL not in gh.issues[PARENT]["labels"]
    # One receipt lists both children.
    receipts = success_receipts(gh)
    assert len(receipts) == 1
    assert f"- #{first['number']} Users can tag a note" in receipts[0]
    assert f"- #{second['number']} Users can export a note" in receipts[0]


def test_children_inherit_only_p0_bug_and_the_milestone(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    gh.add_milestone(3, title="v1", open_issues=5)
    stopped_ticket(gh, milestone=3, labels=(
        NEEDS_DETAIL_LABEL, P0_LABEL, BUG_LABEL, "ai-human-review",
        "ai-content-only",
    ))
    reply(gh)
    tick(monkeypatch, tmp_path, gh)

    for kid in children(gh):
        assert kid["milestone"] == 3
        assert set(kid["labels"]) == {P0_LABEL, BUG_LABEL, READY_LABEL}


def test_a_ticket_without_inherited_labels_creates_plain_children(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(NEEDS_DETAIL_LABEL,))
    reply(gh)
    tick(monkeypatch, tmp_path, gh)

    for kid in children(gh):
        assert kid["milestone"] is None
        assert set(kid["labels"]) == {READY_LABEL}


# --------------------------------------------------------------------------
# acceptance 2: the edited comment is what gets created
# --------------------------------------------------------------------------

def test_an_edited_proposal_is_what_gets_created(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    stop = gh.issues[PARENT]["comments"][0]
    edited = proposal(
        item("Users can label a note", "filtering by the label finds the note"),
        item("Users can archive a note", "the note leaves the list", after=1),
        item("Users can unarchive a note", "the note returns"),
    )
    # A person edits the stop comment before replying: GitHub keeps the
    # edited body, so the command reads the new lines.
    stop["body"] = stop_body(clarify.render_suggestion(edited, "en"))
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert titles(gh) == [
        "Users can label a note", "Users can archive a note",
        "Users can unarchive a note",
    ]


def test_an_item_removed_by_the_edit_is_not_created(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    stop = gh.issues[PARENT]["comments"][0]
    stop["body"] = stop_body(clarify.render_suggestion(proposal(
        item("Users can tag a note", "filtering by the tag finds the note"),
        item("Users can rename a note", "the new title shows"),
    ), "en"))
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert titles(gh) == ["Users can tag a note", "Users can rename a note"]


# --------------------------------------------------------------------------
# acceptance 3: a failing run continues on the next tick
# --------------------------------------------------------------------------

def test_a_failed_write_step_leaves_one_receipt_naming_it(
    monkeypatch, tmp_path,
):
    """A `gh` refusal is a STEP failure, never silence.

    The shared layer posts its one failed receipt for a
    `TicketCommandError` only, so a step that let the raw `gh` error
    escape left the person who replied with no receipt at all — no step,
    no GitHub sentence, no repair action. The ticket kept
    `ai-needs-detail`, so the same command finishes once GitHub answers.
    """
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: command[1] == "issue" and command[2] == "create",
    ))

    tick(monkeypatch, tmp_path, gh)

    reported = failures(gh)
    assert len(reported) == 1
    assert language.text(
        "en", "command.failed_step", step="create the child ticket",
    ) in reported[0]
    assert "gh: HTTP 422: Validation Failed" in reported[0]
    assert children(gh) == []
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]

    # The same refusal on the next tick is still ONE receipt for that
    # comment (the failed receipt deduplicates like every other one).
    tick(monkeypatch, tmp_path, gh)
    assert len(failures(gh)) == 1

    # GitHub answers again: the same command finishes exactly once.
    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert titles(gh) == ["Users can tag a note", "Users can export a note"]
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]


def test_a_failed_relation_step_names_it_and_continues(monkeypatch, tmp_path):
    """The native relations are steps too: a refused `--add-sub-issue`
    names itself, and the child it was meant for is still adopted (never
    opened twice) on the next tick."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: "--add-sub-issue" in command,
    ))

    tick(monkeypatch, tmp_path, gh)

    reported = failures(gh)
    assert len(reported) == 1
    assert language.text(
        "en", "command.failed_step",
        step="attach the child as a sub-issue",
    ) in reported[0]
    # The child was opened; only its relation is missing.
    opened = [
        issue for number, issue in gh.issues.items() if number != PARENT
    ]
    assert [issue["title"] for issue in opened] == ["Users can tag a note"]
    assert opened[0]["parent"] is None
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]

    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    # The unattached child is adopted, never opened a second time.
    assert sorted(gh.issues) == [PARENT, 101, 102]
    assert titles(gh) == ["Users can tag a note", "Users can export a note"]
    assert {kid["parent"] for kid in children(gh)} == {PARENT}
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]


@pytest.mark.parametrize("failure, expected", [
    (subprocess.CalledProcessError(1, ["gh"], stderr="gh: nope\n"), "gh: nope"),
    (subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: bytes"), "gh: bytes"),
    (RuntimeError("plain failure"), "plain failure"),
])
def test_the_failure_reason_names_githubs_own_sentence(failure, expected):
    """The receipt carries GitHub's sentence (stderr), not an exit status."""
    assert split_command._failure_reason(failure) == expected


def test_a_failed_second_child_continues_on_the_next_tick(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    # The first child is opened, then the second `gh issue create` fails.
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: (
            command[1] == "issue" and command[2] == "create"
            and "Users can export a note" in command
        ),
    ))

    tick(monkeypatch, tmp_path, gh)
    assert titles(gh) == ["Users can tag a note"]
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]
    assert success_receipts(gh) == []

    # The next tick runs the SAME command over what is now on GitHub.
    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert titles(gh) == ["Users can tag a note", "Users can export a note"]
    assert all(READY_LABEL in kid["labels"] for kid in children(gh))
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]


# --------------------------------------------------------------------------
# acceptance 4: adoption is strict
# --------------------------------------------------------------------------

def existing_child(
    gh: FakeGh, *, number: int, item_number: int = 1,
    title: str = "Users can tag a note",
    acceptance: str = "filtering by the tag finds the note",
    author: str = "orbi-bot", blocked_by: tuple[int, ...] = (),
    attach: bool = True,
) -> None:
    """One Issue an earlier (possibly forged or interrupted) run left."""
    body = split_command.render_child_body(
        item(title, acceptance), parent=PARENT, item_number=item_number,
        parent_body="## Outcome\nUsers keep notes.\n",
    )
    gh.add_issue(number, title=title, body=body, author=author,
                 labels=(READY_LABEL, BUG_LABEL))
    gh.issues[number]["parent"] = PARENT if attach else None
    for blocker in blocked_by:
        gh.add_blocker(number, blocker)


def test_a_forged_marker_is_not_adopted(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    existing_child(gh, number=55, author="someone-else", attach=False)
    before = {number: list(issue["labels"]) for number, issue in gh.issues.items()}
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    # The forged Issue is left exactly as it was; the real child is opened
    # beside it and both items are created.
    assert gh.issues[55]["labels"] == before[55]
    assert gh.issues[55]["parent"] is None
    assert titles(gh) == ["Users can tag a note", "Users can export a note"]


def test_a_matching_existing_child_is_adopted_and_queued(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    existing_child(gh, number=55)
    gh.issues[55]["labels"] = []   # an interrupted run never queued it
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    # The existing child is adopted (no duplicate); only item 2 is new.
    assert before | {101} == set(gh.issues)
    assert READY_LABEL in gh.issues[55]["labels"]
    assert gh.issues[55]["parent"] == PARENT
    assert sorted(kid["number"] for kid in children(gh)) == [55, 101]


@pytest.mark.parametrize("author_shape", [
    "app/{login}",   # what `gh issue view --json author` really reports
    "{login}[bot]",  # the REST shape of the same App credential
    "{login}",       # a comment payload, and a user-credential install
])
def test_the_runner_author_has_every_github_payload_shape_adopted(
    monkeypatch, tmp_path, author_shape,
):
    """The adoption check compares ACCOUNTS, not payload shapes.

    GitHub names one credential three ways: an Issue this runner opened
    answers `app/<login>` on its `author` field (a real call against an
    engine-created ticket), its comments keep `<login>`, and REST keeps
    the `[bot]` suffix. A re-run that matched only one shape would open a
    duplicate child on every retry (Issue #1927).
    """
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    existing_child(gh, number=55, author=author_shape.format(login=gh.login))
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    # No duplicate: the existing child is adopted and only item 2 is new.
    assert before | {101} == set(gh.issues)
    assert sorted(kid["number"] for kid in children(gh)) == [55, 101]
    assert len(success_receipts(gh)) == 1


def test_the_login_key_normalizes_every_payload_shape():
    assert split_command._login_key("app/orbi-build") == "orbi-build"
    assert split_command._login_key("orbi-build[bot]") == "orbi-build"
    assert split_command._login_key("orbi-build") == "orbi-build"


def _mismatch_tick(monkeypatch, tmp_path, gh, field) -> str:
    """Run one tick and return the single difference receipt."""
    before = set(gh.issues)
    reply(gh)
    tick(monkeypatch, tmp_path, gh)
    assert success_receipts(gh) == []
    assert set(gh.issues) == before          # nothing more is created
    reported = failures(gh)
    assert len(reported) == 1
    assert field in reported[0]
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]
    return reported[0]


@pytest.mark.parametrize("field, kind", [
    ("title", "title"),
    ("acceptance lines", "acceptance"),
    ("`after` dependency", "after"),
    ("split marker", "marker"),
    ("parent", "parent"),
])
def test_a_differing_existing_child_stops_with_a_receipt(
    monkeypatch, tmp_path, field, kind,
):
    gh = _gh(monkeypatch)
    section = None
    if kind == "title":
        existing_child(gh, number=55, title="Users can tag notes")
    elif kind == "acceptance":
        existing_child(gh, number=55, acceptance="the tag is listed")
    elif kind == "marker":
        # The body no longer carries a child's sections: only the marker
        # survived an edit, so the Issue cannot be adopted.
        existing_child(gh, number=55)
        gh.issues[55]["body"] = f"<!-- orbi:split-child:{PARENT}:1 -->\nedited"
    elif kind == "parent":
        # Another ticket already claims this child.
        gh.add_issue(PARENT + 1, title="another epic", labels=(EPIC_LABEL,))
        existing_child(gh, number=55)
        gh.issues[55]["parent"] = PARENT + 1
    else:
        # The edit drops item 1's dependency, but the child is still
        # blocked by the other child: the dependency no longer matches.
        section = clarify.render_suggestion(proposal(
            item("Users can tag a note", "filtering by the tag finds the note"),
            item("Users can export a note", "Export downloads a .md file"),
        ), "en")
        existing_child(gh, number=55)
        existing_child(
            gh, number=56, item_number=2,
            title="Users can export a note",
            acceptance="Export downloads a .md file", blocked_by=(55,),
        )
    stopped_ticket(gh, **({} if kind != "after" else {"section": section}))

    _mismatch_tick(monkeypatch, tmp_path, gh, field)


def test_a_wrong_after_edge_between_three_items_stops(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    three = proposal(
        item("Users can tag a note", "filtering by the tag finds the note"),
        item("Users can export a note", "Export downloads a .md file"),
        item("Users can sync a note",
             "the note reaches the other device", after=2),
    )
    stopped_ticket(gh, section=clarify.render_suggestion(three, "en"))
    existing_child(gh, number=55)
    existing_child(gh, number=56, item_number=2,
                   title="Users can export a note",
                   acceptance="Export downloads a .md file")
    existing_child(
        gh, number=57, item_number=3, title="Users can sync a note",
        acceptance="the note reaches the other device", blocked_by=(55,),
    )

    _mismatch_tick(monkeypatch, tmp_path, gh, "`after` dependency")


def test_a_malformed_blocked_by_payload_reads_as_no_edges():
    """The native relation is fail-open like every other GitHub relation:
    a payload this engine cannot read is "no known edge", never a crash."""
    assert split_command._blocked_numbers({"blockedBy": None}) == set()
    assert split_command._blocked_numbers({"blockedBy": {"nodes": "no"}}) == set()
    assert split_command._blocked_numbers({}) == set()
    assert split_command._blocked_numbers({"blockedBy": {"nodes": [
        "text", {"number": "7"}, {"number": True}, {"number": 3},
    ]}}) == {3}


def test_two_matching_children_stop_before_adopting_either(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    existing_child(gh, number=55)
    existing_child(gh, number=56)
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert set(gh.issues) == before
    assert success_receipts(gh) == []
    reported = failures(gh)
    assert len(reported) == 1
    assert "2 issues carry the marker" in reported[0]


# --------------------------------------------------------------------------
# acceptance 5: an interrupted run finishes exactly once
# --------------------------------------------------------------------------

def test_a_failed_receipt_is_posted_once_on_the_next_tick(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: (
            command[1] == "issue" and command[2] == "comment"
            and str(PARENT) in command
        ),
    ))

    tick(monkeypatch, tmp_path, gh)
    assert len(children(gh)) == 2
    assert success_receipts(gh) == []
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]

    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    # The children are adopted, the receipt is posted once, the label goes.
    assert len(children(gh)) == 2
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]
    assert EPIC_LABEL in gh.issues[PARENT]["labels"]


def test_a_failed_label_removal_finishes_next_tick_without_a_second_receipt(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: (
            command[1] == "issue" and command[2] == "edit"
            and str(PARENT) in command and NEEDS_DETAIL_LABEL in command
        ),
    ))

    tick(monkeypatch, tmp_path, gh)
    assert len(success_receipts(gh)) == 1
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]

    monkeypatch.setattr(seam, "run_command", gh)
    tick(monkeypatch, tmp_path, gh)

    assert NEEDS_DETAIL_LABEL not in gh.issues[PARENT]["labels"]
    assert len(success_receipts(gh)) == 1


# --------------------------------------------------------------------------
# acceptance 6: every rejection leaves a receipt and creates nothing
# --------------------------------------------------------------------------

def test_a_caller_without_write_permission_is_rejected(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    before = set(gh.issues)
    reply(gh, login="stranger", permission="read")

    tick(monkeypatch, tmp_path, gh)

    assert language.text(
        "en", "command.reason_no_permission", permission="write",
        observed="read",
    ) in rejection(gh)
    assert set(gh.issues) == before
    assert NEEDS_DETAIL_LABEL in gh.issues[PARENT]["labels"]


def test_an_invalid_proposal_line_is_rejected_with_its_number(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    # The second item has no acceptance line: the body's line 9.
    section = "\n".join([
        "<!-- orbi:suggestion:v1 kind=split -->",
        "1. Users can tag a note",
        "   Acceptance: filtering by the tag finds the note",
        "2. Users can export a note",
        STOP_END,
    ])
    stopped_ticket(gh, section=section)
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text("en", "split.reason_invalid_line", line=9) in (
        rejection(gh)
    )
    assert set(gh.issues) == before


def test_a_criteria_proposal_is_not_a_split(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    criteria = clarify.Suggestion(
        kind=clarify.SUGGESTION_CRITERIA,
        items=(clarify.SuggestionItem(text="The tag survives a reload"),),
    )
    stopped_ticket(gh, section=clarify.render_suggestion(criteria, "en"))
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text(
        "en", "split.reason_not_split", kind="criteria",
    ) in rejection(gh)
    assert set(gh.issues) == before


def test_a_stop_without_a_proposal_carries_no_split(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, section="", prose="Add the missing outcome.")
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text("en", "split.reason_no_suggestion") in rejection(gh)
    assert set(gh.issues) == before


def test_a_child_ticket_is_not_split_again(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    gh.add_issue(PARENT + 1, title="grandparent", labels=(EPIC_LABEL,))
    stopped_ticket(gh, parent=PARENT + 1)
    before = set(gh.issues)
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert language.text(
        "en", "split.reason_child", parent=PARENT + 1,
    ) in rejection(gh)
    assert set(gh.issues) == before


# --------------------------------------------------------------------------
# the scan and the tick wiring
# --------------------------------------------------------------------------

def test_only_ai_needs_detail_tickets_are_scanned(monkeypatch, tmp_path):
    gh = _gh(monkeypatch)
    stopped_ticket(gh, labels=(READY_LABEL,))
    reply(gh)

    tick(monkeypatch, tmp_path, gh)

    assert children(gh) == []
    assert rejection_or_none(gh) is None


def rejection_or_none(gh: FakeGh) -> str | None:
    receipts = [
        body for body in comment_bodies(gh)
        if "orbi-split-command-rejected" in body
    ]
    assert len(receipts) <= 1
    return receipts[0] if receipts else None


def test_a_failed_scan_never_stops_the_tick(monkeypatch, tmp_path, caplog):
    """The scan is a pure bypass: a broken GitHub read is logged and the
    ordinary delivery scans still run in the same tick."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    monkeypatch.setattr(seam, "run_command", fail_on(
        gh, lambda command: (
            command[1] == "issue" and command[2] == "list"
            and NEEDS_DETAIL_LABEL in command
        ),
    ))
    caplog.set_level("INFO")

    tick(monkeypatch, tmp_path, gh)

    assert "ticket_command_scan_failed" in caplog.text


# --------------------------------------------------------------------------
# the command layer's own contracts
# --------------------------------------------------------------------------

def test_apply_refuses_a_ticket_without_a_proposal(monkeypatch):
    _gh(monkeypatch)
    with pytest.raises(ticket_command.TicketCommandError):
        split_command.SPLIT_COMMAND.apply(
            "", repo=REPO, issue_number=PARENT, command_comment={"id": 1},
            comments=[], issue={"number": PARENT, "labels": []},
            suggestion=None, language_code="en",
        )


def test_apply_refuses_an_after_whose_item_has_no_child(monkeypatch):
    gh = _gh(monkeypatch)
    gh.add_issue(PARENT, title="Note app")
    broken = proposal(
        item("Users can tag a note", "filtering by the tag finds the note"),
        item("Users can export a note", "Export downloads a .md file", after=7),
    )
    with pytest.raises(ticket_command.TicketCommandError) as excinfo:
        split_command.SPLIT_COMMAND.apply(
            "", repo=REPO, issue_number=PARENT, command_comment={"id": 1},
            comments=[], issue={"number": PARENT, "labels": (), "body": ""},
            suggestion=broken, language_code="en",
        )
    assert excinfo.value.step == "add the blockedBy edge"


def test_a_create_without_a_url_is_a_named_step_failure(
    monkeypatch, tmp_path,
):
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    reply(gh)
    real = gh

    def call(command, **kwargs):
        if command[1] == "issue" and command[2] == "create":
            return "gh: created, but no url"
        return real(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", call)

    tick(monkeypatch, tmp_path, gh)

    reported = failures(gh)
    assert len(reported) == 1
    assert "create the child ticket" in reported[0]
    assert children(gh) == []


def test_the_context_reads_the_ticket_and_its_proposal(monkeypatch):
    """The comment scan hands neither over, so the command reads both from
    the ticket itself; a caller that already has them costs no second read."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    comments = gh.issues[PARENT]["comments"]
    context = split_command.SPLIT_COMMAND.context(
        repo=REPO, issue_number=PARENT, comments=comments,
    )
    assert context["issue"]["number"] == PARENT
    assert context["suggestion"] == TWO_ITEMS
    supplied = split_command.SPLIT_COMMAND.context(
        repo=REPO, issue_number=PARENT, comments=[],
        issue={"number": PARENT}, suggestion=TWO_ITEMS,
    )
    assert supplied == {"issue": {"number": PARENT}, "suggestion": TWO_ITEMS}


def test_the_final_step_is_a_no_op_when_the_scan_label_is_gone(monkeypatch):
    """A resumed caller whose ticket already lost the label only checks."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    receipt = (
        f"<!-- {ticket_command.command_receipt_marker(split_command.SPLIT_COMMAND)}"
        " comment=1 -->"
    )
    before = [list(issue["labels"]) for issue in gh.issues.values()]
    split_command.SPLIT_COMMAND.apply(
        "", repo=REPO, issue_number=PARENT, command_comment={"id": 1},
        comments=[{"body": receipt}], issue={"number": PARENT, "labels": []},
        suggestion=TWO_ITEMS, language_code="en",
    )
    assert [list(issue["labels"]) for issue in gh.issues.values()] == before
    assert success_receipts(gh) == []


def test_the_terminal_spelling_runs_the_same_split(monkeypatch):
    """`orbi split --repo … --issue N` (Issue #1930) is the same execution
    path: the host login is the one occurrence and the ticket itself
    supplies the proposal."""
    gh = _gh(monkeypatch)
    stopped_ticket(gh)
    gh.add_collaborator("orbi-bot", "admin")

    outcome = ticket_command.execute_command(
        REPO, PARENT, split_command.SPLIT_COMMAND,
        caller=ticket_command.cli_caller("orbi-bot"),
        dispatch_label=READY_LABEL, language_code="en",
    )

    assert outcome.applied is True
    assert titles(gh) == ["Users can tag a note", "Users can export a note"]
    assert len(success_receipts(gh)) == 1
    # The receipt names the terminal occurrence it answers, exactly like a
    # comment's receipt names the comment.
    assert "comment=cli@" in success_receipts(gh)[0]


def test_the_split_command_is_registered_with_the_shared_layer():
    spec = ticket_command.TICKET_COMMANDS["split"]
    assert spec is split_command.SPLIT_COMMAND
    assert (spec.name, spec.subcommand, spec.arity, spec.permission) == (
        "split", "split", 0, "write",
    )
    assert spec.usage == "/orbi split"
    assert spec.aliases == ()
    assert "split_command_applied" in journal.JOURNAL_EVENTS
    assert "split_command_failed" in journal.JOURNAL_EVENTS
