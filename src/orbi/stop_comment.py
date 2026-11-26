"""The ONE exit for a comment that blocks an Issue (Issues #1730, #2245, #2287).

Ten scenes put an Issue into `ai-blocked` and each own their reason text,
unblock step, hidden markers and extra fields; this exit alone adds what they
share — the community help line, the failure kind and the terminal failure
record — so a later blocked scene carries them automatically.

Issue #2287 added the stop's ONE Telegram alert to that shared work: it is sent
AFTER the comment lands, because it quotes the comment's own reason and next
action and links it. The label patch therefore no longer sends the blocked
alert itself (`notify.patch_applied` lands before the comment exists, when
neither the reason nor the permalink is knowable).

The pair lives here and `orbi.blocked_comment` re-exports
`post_blocked_comment` for the ten scenes (the `failure_evidence` split of
Issue #2202): the size ratchet records that module at its ceiling, so the
implementation moved and the call sites stayed.
"""
from __future__ import annotations

from orbi import language, notify
from orbi.blocked_record import with_stop_record
from orbi.delivery_labels import EVENT_BLOCKED
from orbi import delivery_labels
from orbi.github import apply_label_patch, comment_issue, issue_title


def post_blocked_comment(number: int, *, repo: str, body: str,
                         language_code: str,
                         ready_label: str | None = None) -> None:
    """Block `number`: apply the `ai-blocked` label patch, then comment.

    The order is part of the contract: the label transition lands first, the
    comment second. The body is posted with the scene's own text plus the
    shared failure kind/record and the `help.community` line in the run's
    language — added here and only here, so each appears exactly once.

    The alert follows the comment (Issue #2287) and quotes the body this call
    posted, so a credential the comment redacts is never echoed to Telegram
    (`notify_text` applies the same redaction). A failed comment write
    therefore sends nothing: there is no stop comment to name.
    """
    ready_label = delivery_labels.queue_label(ready_label)
    apply_label_patch(
        number, repo=repo, event=EVENT_BLOCKED,
        ready_label=ready_label,
    )
    posted = (
        f"{with_stop_record(body)}\n\n"
        f"{language.text(language_code, 'help.community')}"
    )
    url = comment_issue(number, repo=repo, body=posted)
    notify.stop_alert(
        number, repo=repo, label=delivery_labels.BLOCKED_LABEL, body=posted,
        comment_url=url, comment_language=language_code,
        read_title=lambda: issue_title(number, repo=repo),
    )
