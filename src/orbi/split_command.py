"""`/orbi split`: turn the proposed split into child tickets (Issue #1927).

The clarification gate stops a thin ticket and may attach a `kind=split`
proposal to its stop comment (Issue #1926). This module defines the command a
person with write permission replies to act on it: ``/orbi split`` turns every
item of the proposal — **as the comment reads at that moment**, so editing it
first changes what is created — into a child ticket, posts ONE receipt listing
the children and removes `ai-needs-detail` LAST, so a run interrupted at any
earlier step is finished by the next tick.

The command runs from the stopped ticket's own scan
(`ticket_command.process_labeled_commands`): the ticket carries
`ai-needs-detail`, so no claim scan looks at it, and that scan is the only
place a comment on it is read.

The steps themselves live in `orbi.split_apply` (Issue #2207): the gate applies
a usable proposal in the claim that judged it, so the ordering and the
idempotency of "find or open every child, attach it, add its `blockedBy` edge,
queue it, mark the parent `ai-epic`" must have exactly one implementation. What
stays here is the COMMAND layer: the proposal is read from the newest trusted
stop comment, only a `kind=split` section is accepted, the ticket must not itself
be a native child, and the success receipt uses the command layer's own marker
keyed on the command comment AND must be a comment this runner wrote
(`ticket_command.runner_receipt`, the one authorship rule every command reads,
Issue #1992): the marker is public text, so "has this command already
succeeded?" is a marker lookup over the raw bodies, never prose, and never a
copy someone else pasted (Issue #1938).

Every GitHub call a step makes is wrapped in `run_step`: the shared layer posts
its one failed receipt only for a `TicketCommandError` (the same rule
`/orbi fix-base` follows), so a raw `gh` refusal must never escape into the
tick's bypass log — the person who replied would see no receipt at all.
"""
from __future__ import annotations

from orbi import clarify, language
from orbi.delivery_labels import NEEDS_DETAIL_LABEL, READY_LABEL, label_names
from orbi.github import comment_issue, edit_issue, issue_view
# The child-ticket engine moved to its own module (Issue #2207) because this one
# sits at its recorded size ceiling (tools/size_ratchet.py); its names stay
# importable HERE, where `orbi.draft` and the tests read them from.
from orbi.split_apply import (
    PARENT_FIELDS,
    SplitChild,
    apply_proposal,
    blocked_numbers,
    child_marker,
    child_state,
    claimed_item,
    create_ticket,
    find_children,
    milestone_title,
    parent_number,
    parse_child_body,
    process_child,
    render_child_body,
    split_receipt,
    verify_existing,
)
from orbi.ticket_command import (
    CommandSpec,
    TicketCommandError,
    command_comment_key,
    command_receipt_marker,
    runner_receipt,
)


def _split_context(
    *, repo: str, issue_number: int, comments: list[dict],
    issue: dict | None = None, suggestion: object = None, **_context: object,
) -> dict[str, object]:
    """The command's own context: the ticket and its stop proposal.

    The comment scan hands neither over (it reads the comments, not the
    Issue's relations), so the command reads both from the ticket itself —
    the CLI entry does the same, and no caller passes a placeholder.
    """
    if not isinstance(issue, dict):
        issue = issue_view(issue_number, PARENT_FIELDS, repo=repo)
    if suggestion is None:
        suggestion = clarify.stop_suggestion(issue, repo, comments=comments)
    return {"issue": issue, "suggestion": suggestion}


def _split_validate(
    argument: str, *, issue: dict, suggestion: object,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> str | None:
    """The command's own rule: a valid split proposal on a parent ticket.

    Everything the steps need is checked HERE, before any write: the
    newest trusted stop comment must carry a proposal that parses, the
    proposal must be a split, and the ticket must not itself be a native
    child (re-splitting a child is refused).
    """
    if suggestion is None:
        return language.text(language_code, "split.reason_no_suggestion")
    if isinstance(suggestion, int):
        return language.text(
            language_code, "split.reason_invalid_line", line=suggestion,
        )
    kind = suggestion.kind if isinstance(suggestion, clarify.Suggestion) else ""
    if kind != clarify.SUGGESTION_SPLIT:
        return language.text(
            language_code, "split.reason_not_split", kind=kind,
        )
    parent = parent_number(issue)
    if parent is not None:
        return language.text(
            language_code, "split.reason_child", parent=parent,
        )
    return None


def _split_apply(
    argument: str, *, repo: str, issue_number: int, command_comment: dict,
    comments: list[dict], issue: dict, suggestion: object,
    dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> None:
    """Do the three command steps: apply, receipt, drop the scan label.

    The seven child-ticket steps ride `split_apply.apply_proposal`; what is left
    here is the receipt keyed on the comment a person replied with, and the scan
    label removed LAST.
    """
    if not isinstance(suggestion, clarify.Suggestion):
        raise TicketCommandError(
            "find or open the child",
            language.text(language_code, "split.reason_no_suggestion"),
        )
    receipt = (
        f"<!-- {command_receipt_marker(SPLIT_COMMAND)} "
        f"comment={command_comment_key(command_comment)} -->"
    )
    if not runner_receipt(comments, receipt):
        children = apply_proposal(
            suggestion.items, repo=repo, issue=issue,
            dispatch_label=dispatch_label, language_code=language_code,
        )
        comment_issue(
            issue_number, repo=repo,
            body=f"{split_receipt(children, language_code)}\n\n{receipt}",
        )
    # The scan label is the LAST step: while it is there the next tick sees
    # the command again and finishes whatever is still missing. A failure
    # here is not wrapped in a step receipt — the split itself succeeded,
    # and a "failed" receipt beside the success would read as a second run.
    if NEEDS_DETAIL_LABEL in label_names(issue):
        edit_issue(issue_number, repo=repo, remove=NEEDS_DETAIL_LABEL)


SPLIT_COMMAND = CommandSpec(
    name="split",
    subcommand="split",
    arity=0,
    permission="write",
    validate=_split_validate,
    apply=_split_apply,
    usage="/orbi split",
    description=(
        "turn the split proposal on this ticket's stop comment into child "
        "tickets"
    ),
    context=_split_context,
)
