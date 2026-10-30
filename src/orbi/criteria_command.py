"""`/orbi amend`: put the proposed acceptance criteria on the ticket.

The clarification gate stops a thin ticket and may attach a `kind=criteria`
proposal to its stop comment (Issue #1926). This module registers the one
command that acts on it: a person with write permission replies
`/orbi amend` and the proposed conditions — **as the comment reads at
that moment**, so editing it first changes what lands — are appended to the
ticket body under an acceptance heading that follows the repository's
`message_language` (`## Acceptance` in English, `## 验收` in Chinese,
Issue #1971). The ticket is then queued with the dispatch label, so the next
claim scan (this one, in the same tick) takes it like any ready ticket.

The command runs from the stopped ticket's own scan
(`ticket_command.process_labeled_commands`): the ticket carries
`ai-needs-detail`, so no claim scan looks at it, and that scan is the only
place a comment on it is read.

It is not a second state machine. The steps ride GitHub's own primitives —
`gh issue edit --body` for the body, the existing delivery labels for the
queue — and what this module owns is the ORDER and the idempotency of the
four steps:

1. append the section, with its hidden marker, unless the marker is there;
2. add the dispatch label to a ticket that does not carry it;
3. post ONE receipt listing the conditions;
4. remove `ai-needs-detail` LAST.

Because the scan label goes last, a run interrupted at any earlier step is
still scanned on the next tick and the same command continues from what is
already on the ticket; a ticket that already carries this command's success
receipt only gets step 4. The success receipt uses the command layer's own
marker keyed on the command comment, so "has this command already
succeeded?" is a marker lookup over the raw bodies, never prose — and only
a receipt THIS runner wrote counts (`runner_receipt`): the marker is public
text, so a copied one must never stand in for the work (Issue #1992).

The two steps that change what the next tick reads — the append and the
dispatch label — are wrapped in the shared `ticket_command.run_step`, so
their refusal becomes the failed receipt and journal line naming that step
(the shared layer posts its one failed receipt only for a
`TicketCommandError`). The success receipt and the final label removal are
deliberately NOT wrapped: each of them is retried by the next tick over what
is already on the ticket, and a "failed" receipt beside the success would
read as a second run.
"""
from __future__ import annotations

from orbi import clarify, language
from orbi.delivery_labels import NEEDS_DETAIL_LABEL, READY_LABEL, label_names
from orbi.github import (
    comment_issue, edit_issue, issue_view, run_gh_write_command,
)
from orbi.ticket_command import (
    CommandSpec,
    TicketCommandError,
    command_comment_key,
    command_receipt_marker,
    register_command,
    run_step,
    runner_receipt,
)

# The heading the conditions land under follows the message language
# (Issue #1971), and the hidden marker that makes the append idempotent is
# the same in every language: the second run of the command finds the marker
# in the body and adds nothing (docs/public-contracts.mdx).
ACCEPTANCE_HEADING_KEY = "criteria.acceptance_heading"
ACCEPTANCE_MARKER = "orbi:acceptance:v1"
# What this command reads from the ticket: the body it appends to, the labels
# it moves and the number it writes to.
ISSUE_FIELDS = "number,body,labels"


def appended_marker() -> str:
    """The hidden line the appended section ends with."""
    return f"<!-- {ACCEPTANCE_MARKER} -->"


def acceptance_heading(language_code: str) -> str:
    """The section heading in one message language."""
    return language.text(language_code, ACCEPTANCE_HEADING_KEY)


def render_acceptance(
    items: tuple[clarify.SuggestionItem, ...], language_code: str,
) -> str:
    """The section appended to the ticket body: the conditions, verbatim.

    The heading and the list grammar are the ones a filed Issue uses, so the
    result reads like an acceptance section a person wrote, and the hidden
    marker after it records that Orbi appended it.
    """
    lines = [acceptance_heading(language_code)]
    lines.extend(f"- {item.text}" for item in items)
    lines.extend(["", appended_marker()])
    return "\n".join(lines)


def appended_body(body: object, section: str) -> str:
    """`body` with `section` appended after one blank line.

    An empty (or unreadable) body receives the section alone: the command
    never writes a leading blank block.
    """
    existing = body if isinstance(body, str) else ""
    if not existing.strip():
        return section
    kept = existing.rstrip("\n")
    return f"{kept}\n\n{section}"


def write_body(number: int, *, repo: str, body: str) -> None:
    """Replace one Issue body: the documented `gh issue edit --body`."""
    run_gh_write_command([
        "gh", "issue", "edit", str(number), "--repo", repo, "--body", body,
    ])


def _receipt(items: tuple[clarify.SuggestionItem, ...], *,
             dispatch_label: str, language_code: str) -> str:
    """The ONE success receipt: the conditions, in proposal order."""
    lines = [
        language.text(language_code, "criteria.receipt_title"),
        "",
        *[
            language.text(
                language_code, "criteria.receipt_item", text=item.text,
            )
            for item in items
        ],
        "",
        language.text(
            language_code, "criteria.receipt_done", label=dispatch_label,
            heading=acceptance_heading(language_code),
        ),
    ]
    return "\n".join(lines)


def _criteria_context(
    *, repo: str, issue_number: int, comments: list[dict],
    issue: dict | None = None, suggestion: object = None, **_context: object,
) -> dict[str, object]:
    """The command's own context: the ticket and its stop proposal.

    The comment scan hands neither over (it reads the comments, not the
    Issue), so the command reads both from the ticket itself — the CLI entry
    does the same, and no caller passes a placeholder.
    """
    if not isinstance(issue, dict):
        issue = issue_view(issue_number, ISSUE_FIELDS, repo=repo)
    if suggestion is None:
        suggestion = clarify.stop_suggestion(issue, repo, comments=comments)
    return {"issue": issue, "suggestion": suggestion}


def _criteria_validate(
    argument: str, *, suggestion: object,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> str | None:
    """The command's own rule: a valid criteria proposal on the ticket.

    Everything the steps need is checked HERE, before any write: the newest
    trusted stop comment must carry a proposal that parses, the proposal must
    be acceptance criteria, and the parser's "at least one line" rule is part
    of parsing (an empty section is an invalid line, never a silent no-op).
    """
    if suggestion is None:
        return language.text(language_code, "criteria.reason_no_suggestion")
    if isinstance(suggestion, int):
        return language.text(
            language_code, "criteria.reason_invalid_line", line=suggestion,
        )
    kind = suggestion.kind if isinstance(suggestion, clarify.Suggestion) else ""
    if kind != clarify.SUGGESTION_CRITERIA:
        return language.text(
            language_code, "criteria.reason_not_criteria", kind=kind,
        )
    return None


def _criteria_apply(
    argument: str, *, repo: str, issue_number: int, command_comment: dict,
    comments: list[dict], issue: dict, suggestion: object,
    dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> None:
    """Do the four steps, in the order the ticket depends on."""
    if not isinstance(suggestion, clarify.Suggestion):
        raise TicketCommandError(
            "append the criteria",
            language.text(language_code, "criteria.reason_no_suggestion"),
        )
    receipt = (
        f"<!-- {command_receipt_marker(ADD_CRITERIA_COMMAND)} "
        f"comment={command_comment_key(command_comment)} -->"
    )
    if not runner_receipt(comments, receipt):
        current = issue.get("body") or ""
        if appended_marker() not in current:
            section = render_acceptance(suggestion.items, language_code)
            run_step(
                "append the criteria",
                lambda: write_body(
                    issue_number, repo=repo,
                    body=appended_body(current, section),
                ),
            )
        if dispatch_label not in label_names(issue):
            run_step(
                "add the dispatch label",
                lambda: edit_issue(
                    issue_number, repo=repo, add=dispatch_label,
                ),
            )
        # The receipt write is NOT wrapped: it IS the success reply, and a
        # refusal here is retried by the next tick over the posted body and
        # label instead of claiming a step it cannot name.
        receipt_body = _receipt(
            suggestion.items, dispatch_label=dispatch_label,
            language_code=language_code,
        )
        comment_issue(
            issue_number, repo=repo,
            body=f"{receipt_body}\n\n{receipt}",
        )
    # The scan label is the LAST step: while it is there the next tick sees
    # the command again and finishes whatever is still missing. A failure
    # here is not wrapped in a step receipt — the append itself succeeded,
    # and a "failed" receipt beside the success would read as a second run.
    if NEEDS_DETAIL_LABEL in label_names(issue):
        edit_issue(issue_number, repo=repo, remove=NEEDS_DETAIL_LABEL)


ADD_CRITERIA_COMMAND = CommandSpec(
    name="add_criteria",
    subcommand="amend",
    arity=0,
    permission="write",
    validate=_criteria_validate,
    apply=_criteria_apply,
    usage="/orbi amend",
    description=(
        "append the proposed acceptance criteria to this ticket and queue it"
    ),
    context=_criteria_context,
)

register_command(ADD_CRITERIA_COMMAND)
