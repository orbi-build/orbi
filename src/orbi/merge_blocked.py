"""The merge gate's `ai-blocked` refusal (Issue #1504, extracted #2221).

A maintainer's terminal label wins over a fully green gate. The gate
re-reads the Issue's labels at the last possible moment -- after the base,
head and CI checks all passed, immediately before the merge command -- so a
label added while the PR was in review is never merged over. The PR stays
open and the labels stay exactly as the human set them: no `ai-merged`, no
label repair.

It lives here rather than in `orbi.merge_exec` because that module and its
`merge_gate` function are at their recorded size ceilings
(`tools/size_ratchet.py`), and this refusal is the code path the gate's new
answer read (Issue #2221) sits beside. The exception TYPE stays the contract
`orbi.review_merge` catches (`merge_exec.MergeBlockedByIssueLabel` is the
same object).
"""
from __future__ import annotations

import logging

from orbi import language
from orbi import delivery_labels
from orbi.github import comment_issue
from orbi.journal import LOGGER, event
from orbi.progress import run_marker


class MergeBlockedByIssueLabel(Exception):
    """The gate refused to merge because the source Issue is `ai-blocked`.

    A maintainer's terminal label wins over a fully green gate: the skip
    comment is posted by `refuse_blocked_issue`, the PR stays open and the
    labels stay as the human set them (no `ai-merged`), so the caller only
    stops the round.
    """


def refuse_blocked_issue(
    issue_number: int, *, repo: str, pr_number: int, head: str,
    run_id: str | None, language_code: str,
) -> None:
    """Post the one skip comment, then raise `MergeBlockedByIssueLabel`."""
    marker = run_marker(run_id)
    event(
        "merge_gate_issue_blocked", level=logging.ERROR,
        issue=issue_number, pr=pr_number, head=head,
    )
    try:
        comment_issue(
            issue_number, repo=repo,
            body=f"{marker}\n" + language.text(
                language_code, "review.merge_skipped_blocked",
                pr_number=pr_number, label=delivery_labels.BLOCKED_LABEL,
                head=head, run_id=run_id,
            ),
        )
    except Exception:
        # The skip comment is a bypass: publishing it must never turn the
        # human's `ai-blocked` decision into a merge or a label change. The
        # gate still refuses the merge.
        LOGGER.exception(
            "merge_skip_comment_publish_failed issue=%s pr=%s run_id=%s",
            issue_number, pr_number, run_id,
        )
    raise MergeBlockedByIssueLabel(
        f"Issue #{issue_number} is labelled {delivery_labels.BLOCKED_LABEL}; the reviewed "
        f"head {head} of PR #{pr_number} was not merged"
    )
