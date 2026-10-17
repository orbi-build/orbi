"""The terminal outcome prose of the progress comment (Issue #1660).

The user-facing paragraphs a run publishes when it finishes: the
headline, the user action, the cause and the raw diagnosis. It is a
LEAF module (it imports only the message catalog and the journal), so
`progress.py` can delegate its wording to it without growing an import
cycle; the state/body rendering stays in `progress.py`.
"""
from __future__ import annotations

import logging

from orbi import language
from orbi.journal import event


def finish_outcome_body(*, outcome: str, detail: str,
                        next_step: str, pr_url: str | None,
                        number: int, source_repo: str,
                        action: str | None = None,
                        reason: str | None = None,
                        diagnosis: str | None = None,
                        language_code: str,
                        ) -> str:
    """Render a terminal outcome with action, reason, and diagnosis."""
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    if not next_step:
        event(
            "progress_finish_missing_next_step", level=logging.WARNING,
            call_point="_finish_progress_body", issue=number,
            repo=source_repo, outcome=outcome,
        )
    legacy_action = action is None
    blocked = outcome == "blocked"
    outcome_word = message(
        "progress.outcome_blocked" if blocked
        else "progress.outcome_fix_needed",
    )
    disposition = message(
        "progress.disposition_blocked" if blocked
        else "progress.disposition_retry",
    )
    if reason is None:
        reason = message(
            "progress.default_reason_blocked" if blocked
            else "progress.default_reason_retry",
        )
    if diagnosis is None:
        diagnosis = detail
    paragraphs = [message(
        "progress.outcome_headline", outcome=outcome_word,
        disposition=disposition,
    )]
    if legacy_action:
        user_action = next_step if blocked and next_step else message(
            "progress.nothing",
        )
        paragraphs.append(message(
            "progress.what_to_do", action=user_action,
        ))
    elif action:
        paragraphs.append(message("progress.what_to_do", action=action))
    if reason:
        paragraphs.append(message("progress.what_happened", reason=reason))
    else:
        paragraphs.append(message("progress.what_happened_missing"))
    if legacy_action:
        if blocked and pr_url:
            engine_action = message(
                "progress.engine_wait_pr", pr_url=pr_url,
            )
        elif blocked:
            engine_action = message("progress.engine_wait_human")
        else:
            engine_action = next_step or message("progress.engine_wait_human")
        paragraphs.append(message(
            "progress.what_next", action=engine_action,
        ))
    elif blocked:
        # A carried action never hides what the engine does next: a
        # blocked delivery still waits for a human (Issue #1701 keeps the
        # blocked review comment's engine line unchanged).
        paragraphs.append(message(
            "progress.what_next", action=message(
                "progress.engine_wait_pr", pr_url=pr_url,
            ) if pr_url else message("progress.engine_wait_human"),
        ))
    elif not action and next_step:
        paragraphs.append(message("progress.what_next", action=next_step))
    paragraphs.append(message("progress.raw_error", diagnosis=diagnosis))
    return "\n\n".join(paragraphs)
