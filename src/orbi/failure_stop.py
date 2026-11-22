"""The ONE reader-facing wording of a stop: its headline and its action.

Issue #2351: every `ai-blocked` stop used to share one headline ("waiting on
a human decision") while the record beside it could carry the action
`fix_ticket` — the headline contradicted the action the reader was told to
take. `stop_wording` decides BOTH strings from ONE record, reading its
`action_code` and `retry_safe` (plus the reason codes that already own a better
sentence than their action family can give: an undetermined cause, a missing
base branch and the provider balance refusal). The failure comment and the
terminal progress comment both read it, so the two cannot drift apart.

It lives in its own module because `orbi.failure` is at its size-ratchet
ceiling and may only shrink (`tools/size_ratchet.py`, Article 3.5) — the same
reason `terminal_stops` and `provider_balance` exist. `orbi.failure` re-exports
`terminal_action` and `stop_wording`, so their existing callers are unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from orbi import language, provider_balance

if TYPE_CHECKING:  # the record's home imports this module's names back
    from orbi.failure import Failure

# reason_code -> (action key, disposition key): the reasons that own a
# better sentence than their action family can give: the undetermined cause
# (which must not blame the Issue text, Issue #1651), the branch a human
# must create or pick (Issue #1749) and the empty-tree stop (whose Issue may
# already be satisfied, Issue #2358). ONE table feeds BOTH strings, so a
# headline can never tell the reader to change the Issue while the action
# beside it names a repository or evidence repair. They are read before the
# action family, never instead of it.
_REASON_WORDING = {
    "unclassified": (
        "failure.terminal_unclassified", "failure.disposition_unknown_cause",
    ),
    "base_branch_missing": (
        "failure.terminal_base_branch_missing",
        "failure.disposition_base_branch",
    ),
    "no_file_change": (
        "failure.terminal_no_file_change",
        "failure.disposition_no_file_change",
    ),
}

# action_code -> the headline disposition that action really asks for
# (`retry_safe` false). The two account actions share one sentence: the
# reader repairs a credential/account either way, and the visible Reason
# already names which.
_ACTION_DISPOSITIONS = {
    "decide": "failure.disposition_decide",
    "fix_ticket": "failure.disposition_ticket_change",
    "check_credentials": "failure.disposition_repair",
    provider_balance.ACTION_CODE: "failure.disposition_repair",
}

# action_code -> the concrete reader step of that action.
_ACTION_STEPS = {
    "decide": "failure.terminal_ticket_change",
    "fix_ticket": "failure.terminal_ticket_change",
    "check_credentials": "failure.terminal_check_credentials",
}


def _disposition_key(record: "Failure") -> str:
    """The catalog key of the stop's headline disposition.

    A `retry_safe` stop needs no repair (a plain re-queue can succeed), so it
    never claims a decision; the no-commit stop names what happened instead,
    because "the ticket needs a change" is not what its reader would guess;
    and a reason in `_REASON_WORDING` above keeps the disposition paired with
    its action sentence (the empty-tree stop says its Issue may already be
    satisfied), so the headline never asks for a ticket edit the action does
    not ask for (Issue #2351, #2358).
    """
    if record.retry_safe:
        return "failure.disposition_retry"
    if record.reason_code == "no_commit_delivered":
        return "failure.disposition_no_commit"
    reason_wording = _REASON_WORDING.get(record.reason_code)
    if reason_wording is not None:
        return reason_wording[1]
    return _ACTION_DISPOSITIONS.get(
        record.action_code, "failure.disposition_ticket_change",
    )


def terminal_action(record: "Failure", language_code: str, *,
                    base_branch: str | None = None) -> str:
    """The reader's concrete next step for one recorded action.

    A `retry_safe` stop keeps the generic re-run sentence (nothing needs a
    repair); every other stop names its own step, so the action text and the
    headline always describe the same action.
    """
    reason_wording = _REASON_WORDING.get(record.reason_code)
    if reason_wording is not None:
        action_key = reason_wording[0]
        if action_key == "failure.terminal_base_branch_missing":
            return language.text(
                language_code, action_key, base_branch=base_branch or "-",
            )
        return language.text(language_code, action_key)
    if record.action_code == provider_balance.ACTION_CODE:
        return provider_balance.action(language_code)
    if record.retry_safe:
        return language.text(language_code, "failure.terminal_generic")
    return language.text(
        language_code,
        _ACTION_STEPS.get(record.action_code, "failure.terminal_ticket_change"),
    )


def stop_wording(record: "Failure", language_code: str, *,
                 base_branch: str | None = None) -> tuple[str, str]:
    """Return `(headline, action)` — the ONE wording of one stop.

    The failure comment and the terminal progress comment both render this
    pair, so a headline can never promise a decision the recorded action does
    not ask for (Issue #2351).
    """
    return (
        language.text(
            language_code, "failure.headline_blocked",
            disposition=language.text(language_code, _disposition_key(record)),
        ),
        terminal_action(record, language_code, base_branch=base_branch),
    )
