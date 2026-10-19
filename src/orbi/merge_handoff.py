"""Classification and handling of merge failures that need a maintainer.

`MergeHandoffRequired` classifies a ready-but-policy-blocked review as a
successful, resumable handoff. `handle_merge_handoff` performs that
handoff idempotently: the same waiting state (PR head + blocker text) is
announced once, and every later retry only journals (Issue #1422).

The announced state is identified LANGUAGE-INDEPENDENTLY (Issue #1660):
the head is a `- head: `<oid>`` field and the blocker sits in one fenced
block, so a handoff rendered in another language is still recognized on
the next tick. A comment written before that change (the English
"is delivered and waiting for a maintainer action" phrase) stays
readable, so mixed old-English/new-Chinese history keeps working.
"""
from __future__ import annotations

import re

from orbi import github, journal, language
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL, EVENT_AWAITING_MERGE, READY_LABEL,
)


class MergeHandoffRequired(RuntimeError):
    """The reviewed PR is ready, but known repository policy needs a human."""

    def __init__(self, message: str, *, preflight: list[str] | None = None):
        super().__init__(message)
        self.preflight = list(preflight or [])


_POLICY_REJECTION = re.compile(
    r"the base branch policy prohibits the merge", re.IGNORECASE,
)
_FAILED_PREFLIGHT = re.compile(r"^merge_gate: FAILED\b")


def is_maintainer_actionable(stderr: str, preflight: list[str]) -> bool:
    """Return true when the merge rejection names a fixable policy blocker.

    PASS and UNKNOWN lines are deliberately excluded: the preflight must
    establish a concrete action a maintainer can take.
    """
    return bool(
        _POLICY_REJECTION.search(stderr)
        and any(_FAILED_PREFLIGHT.search(line) for line in preflight)
    )


# The handoff comment identifies the exact waiting state it announced.
# ``ai-awaiting-merge`` is re-checked by design — but only on a tick
# whose queue is otherwise empty (Issue #1745: the deferred scan never
# takes the tick away from reviewable, resumable or fresh work), so the
# same policy blocker still reaches the merge gate again and again. The
# head OID and the blocker text decide whether the wait is a new state or
# the same one already posted (Issue #1422), so an unchanged blocker
# posts nothing.
AWAITING_MERGE_HANDOFF_PHRASE = (
    "is delivered and waiting for a maintainer action"
)
_HANDOFF_HEAD_RE = re.compile(
    "^[-*] head: `([^`]+)`$", re.MULTILINE,
)
_HANDOFF_BLOCKER_RE = re.compile(
    "````\n(.*?)\n````", re.DOTALL,
)
# The pre-#1660 grammar, kept so a historical handoff comment still
# anchors the wait (mixed history).
_LEGACY_HEAD_RE = re.compile(r"at head `([^`]+)` is blocked")
_LEGACY_BLOCKER_RE = re.compile(
    r"blocked by this repository policy:\n````\n(.*?)\n````",
    re.DOTALL,
)


def handoff_body(
    marker: str, *, pr_number: int, head: str, preflight: list[str],
    language_code: str,
) -> str:
    """Render the one maintainer-action comment for a waiting PR.

    The `- head:` field and the fenced policy list are the comment's
    language-independent identity (Issue #1660); the prose around them
    follows the run's message language.
    """
    failed_lines = "\n".join(preflight)
    fence = "````"
    return (
        f"{marker}\n"
        + language.text(
            language_code, "handoff.headline", pr_number=pr_number,
        )
        + f"\n- pr: #{pr_number}\n- head: `{head}`\n"
        + language.text(language_code, "handoff.policy_heading") + "\n"
        + f"{fence}\n{failed_lines}\n{fence}\n\n"
        + language.text(language_code, "handoff.next_step")
    )


def awaiting_merge_handoff_announced(
    comments: list[dict], *, head: str, preflight: list[str],
) -> bool:
    """True when the latest Orbi handoff comment announced this wait.

    The most recent trusted handoff comment is the state anchor. The wait
    is already announced when that comment names the same ``head`` OID and
    the same blocker text (``preflight`` joined by newlines). A malformed
    handoff, no comment at all, or a mismatching head/blocker all read as
    "not announced" so the caller posts the new state once.
    """
    blocker = "\n".join(preflight)
    for comment in reversed(comments):
        if not github._comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        # The current grammar first, then the pre-#1660 one; a comment
        # matching neither is not a handoff (progress, milestone, ...).
        head_match = _HANDOFF_HEAD_RE.search(body)
        blocker_match = _HANDOFF_BLOCKER_RE.search(body)
        if head_match is None:
            head_match = _LEGACY_HEAD_RE.search(body)
            blocker_match = _LEGACY_BLOCKER_RE.search(body)
        if head_match is None:
            continue
        if blocker_match is None:
            return False
        return (
            head_match.group(1) == head
            and blocker_match.group(1) == blocker
        )
    return False


def handle_merge_handoff(
    *, number: int, repo: str, pr_number: int, head: str,
    preflight: list[str], marker: str,
    language_code: str, ready_label: str = READY_LABEL,
) -> bool:
    """Announce one maintainer-action handoff, idempotently.

    The first entry (the Issue does not yet carry `ai-awaiting-merge`) and
    a changed waiting state (a different head OID or blocker text) keep
    the full handoff: label patch, one comment, the
    `delivery_awaiting_human_merge` event. The same waiting state writes
    nothing to GitHub and only journals
    `delivery_awaiting_human_merge_unchanged`, so a policy-blocked retry
    never reposts the comment (Issue #1422). Always returns False: a
    handoff is a resumable, non-merged tick.
    """
    current_labels = github.issue_labels(number, repo)
    if (
        AWAITING_MERGE_LABEL in current_labels
        and awaiting_merge_handoff_announced(
            github.issue_comments(number, repo=repo),
            head=head, preflight=preflight,
        )
    ):
        journal.event(
            "delivery_awaiting_human_merge_unchanged", issue=number,
            pr=pr_number, head=head,
        )
        return False
    github.apply_label_patch(
        number, repo=repo, event=EVENT_AWAITING_MERGE,
        current_labels=current_labels, ready_label=ready_label,
    )
    github.comment_issue(
        number, repo=repo,
        body=handoff_body(
            marker, pr_number=pr_number, head=head, preflight=preflight,
            language_code=language_code,
        ),
    )
    journal.event(
        "delivery_awaiting_human_merge", issue=number,
        pr=pr_number, head=head,
    )
    return False
