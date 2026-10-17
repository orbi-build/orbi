"""The ONE exit for a comment that blocks an Issue (Issue #1730).

Ten scenes put an Issue into `ai-blocked` and each own their reason text,
unblock step, hidden markers and extra fields. What they must NOT each own is
the pointer to where a person can ask for help: it is appended here, once, so
a blocked scene added later through this exit carries the community help line
without anyone remembering to add it.

A comment that is not a blocked scene (a retrying diagnostic, progress, a
receipt) never goes through this exit and never gets the line.
"""
from __future__ import annotations

from orbi import language
from orbi.delivery_labels import EVENT_BLOCKED
from orbi.github import apply_label_patch, comment_issue


def post_blocked_comment(number: int, *, repo: str, body: str,
                         current_labels, language_code: str) -> None:
    """Block `number`: apply the `ai-blocked` label patch, then comment.

    The order is part of the contract: the label transition lands first, the
    comment second. The body is posted exactly as the scene rendered it,
    followed by a blank line and the `help.community` line in the run's
    language — added here and only here, so it appears exactly once.
    """
    apply_label_patch(
        number, repo=repo, event=EVENT_BLOCKED,
        current_labels=current_labels,
    )
    comment_issue(
        number, repo=repo,
        body=f"{body}\n\n{language.text(language_code, 'help.community')}",
    )
