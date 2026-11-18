"""The ONE exit for a comment that blocks an Issue (Issue #1730, #2245).

Ten scenes put an Issue into `ai-blocked` and each own their reason text,
unblock step, hidden markers and extra fields; this exit alone adds what
they share — the community help line, the failure kind and the terminal
failure record — so a later blocked scene carries them automatically.
"""
from __future__ import annotations

from orbi import language
from orbi.blocked_record import with_stop_record
from orbi.delivery_labels import EVENT_BLOCKED, READY_LABEL
from orbi.github import apply_label_patch, comment_issue


def post_blocked_comment(number: int, *, repo: str, body: str,
                         current_labels, language_code: str,
                         ready_label: str = READY_LABEL) -> None:
    """Block `number`: apply the `ai-blocked` label patch, then comment.

    The order is part of the contract: the label transition lands first, the
    comment second. The body is posted with the scene's own text plus the
    shared failure kind/record and the `help.community` line in the run's
    language — added here and only here, so each appears exactly once.
    """
    apply_label_patch(
        number, repo=repo, event=EVENT_BLOCKED,
        current_labels=current_labels, ready_label=ready_label,
    )
    comment_issue(
        number, repo=repo,
        body=(
            f"{with_stop_record(body)}\n\n"
            f"{language.text(language_code, 'help.community')}"
        ),
    )
