"""The scans that read a STOPPED ticket's comments (Issues #1927, #2221).

A ticket outside every delivery label is read by no claim scan: the
clarification gate leaves a ticket `ai-needs-detail`, and the three
delivery stops leave it `ai-blocked`, `ai-pr-opened` or `ai-fix-needed`.
One tick runs these scans once per repository, as a bypass (a per-ticket
failure is logged by the caller and never fails the tick), and each label
names the commands read on it: the registered `/orbi` commands on the
clarification stop, and the record command `answer` on the delivery stops.

Why the answer scan is here rather than in each caller: the same ticket
carries the same answer comment through all three delivery stops, and the
LABEL it carries at that moment IS the stage the answer is read at
(`answer.decide`) — so the stage can never be a guess a caller made.
"""
from __future__ import annotations

from orbi import blocked_scan, ticket_command, ticket_command_registry
from orbi.answer import ANSWER_COMMAND
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    FIX_NEEDED_LABEL,
    NEEDS_DETAIL_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)

# The delivery stops whose comments carry an answer (Issue #2221).
ANSWER_LABELS = (BLOCKED_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL)


def run(
    repo: str, *, language_code: str, dispatch_label: str = READY_LABEL,
    limit: int = 200,
) -> None:
    """Run every stopped-ticket scan for one repository."""
    ticket_command.process_labeled_commands(
        repo, NEEDS_DETAIL_LABEL,
        commands=ticket_command_registry.ticket_commands(),
        dispatch_label=dispatch_label, language_code=language_code,
        limit=limit,
    )
    for label in ANSWER_LABELS:
        if label != BLOCKED_LABEL:
            ticket_command.process_labeled_commands(
                repo, label, commands=(ANSWER_COMMAND,),
                answer_stage=label, dispatch_label=dispatch_label,
                language_code=language_code, limit=limit,
            )
            continue
        # `ai-blocked` is the tick's ONE read (Issue #2218): the permission
        # and base-branch-red scans already read this label and every
        # blocked Issue's comments, so the answer scan walks that same state
        # instead of listing the label and reading the comments again.
        reads = blocked_scan.blocked_reads(repo)
        for issue in reads.blocked_issues():
            ticket_command.execute_command(
                repo, int(issue["number"]), ANSWER_COMMAND,
                comments=reads.comments(int(issue["number"])),
                answer_stage=label, dispatch_label=dispatch_label,
                language_code=language_code,
            )

