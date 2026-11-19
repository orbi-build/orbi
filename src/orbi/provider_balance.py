#!/usr/bin/env python3
"""A model-provider balance refusal: its own failure reason (Issue #2278).

A provider that refuses a request because the account has no balance
answers HTTP 402 with `{"type": "insufficient_balance_error"}`, and Pi
exits on it. The stop is neither a code failure nor throttling, so the
provider's raw payload must never be the reason a person reads:
`pi_process` replaces that stderr with THIS module's one plain sentence,
and everything downstream reads the sentence rather than the payload —
the shared classifier (`failure.classify`), the failure report's
reader-facing cause and repair, and `orbi draft`'s own error code.
`orbi suggest` needs no change of its own: it logs the session failure's
text, which is already the sentence.

The stop is recognized by the provider's OWN words, not by the exit
status: a POSIX exit status is 8 bits, so the provider's 402 reaches the
runner as 146 (`pi_exit_146`) and the payload is the only exact
evidence left.

Nothing else about the stop changes (Issue #2278): no backoff retry is
added, no reason string in the journal is renamed and the exit keeps the
recoverable/terminal split it had before this module existed.

The stop's record reuses the closed `credential_missing` /
`check_credentials` category — the provider ACCOUNT is what a human
repairs — with `retry_safe` false: a plain re-queue meets the same empty
account, so a relabel only helps after the repair.

This lives in its own module because a module recorded by the size
ratchet may only shrink and `failure`/`pi_process` are at their ceilings
(`tools/size_ratchet.py`, the same reason `terminal_stops` exists).
"""
from __future__ import annotations

import re

from orbi import language

# The record the stop maps to: the provider refused the ACCOUNT, and a
# human repairs it (top up or switch the key) before retrying.
REASON_CODE = "credential_missing"

# `orbi draft`'s own code for the same stop (its codes are its own closed
# set in docs/public-contracts.mdx, not the failure record's).
DRAFT_CODE = "provider_insufficient_balance"

# The ONE sentence that stands for the provider's raw payload: the failure
# detail (never the JSON), the draft error's detail and the suggestion
# log's text all read it.
DETAIL = (
    "the model provider account has no balance: top up the account or "
    "switch to a key with balance (HTTP 402)"
)

# The provider's own refusal words, plus the status in the two shapes the
# payloads carry it (`http 402`, `"status": 402`, and the `402: {json}`
# prefix). A bare `402` only counts in that error shape, so an unrelated
# line (a test named `test_402_handling`) never becomes this stop.
_STOP_MARKERS = (
    "insufficient_balance", "insufficient balance", "payment required",
)
_PROVIDER_STATUS_RE = re.compile(
    r"(?:http|status)[\"']?\s*[:=]?\s*402\b"  # http 402, "status": 402
    r"|(?:^|[\s\"'(])402\s*:",  # 402: {provider payload}
    re.IGNORECASE,
)


def is_stop(detail: str) -> bool:
    """Whether one failure detail is the provider's balance refusal.

    Either the plain sentence this module substituted for the payload, or
    the provider's own refusal markers on a path that never passed through
    `stop_detail` (any command whose failure carries the payload). The
    markers are the provider's, so the classifier never guesses: they name
    the balance explicitly or carry the payment-required status.
    """
    lowered = detail.lower()
    return (
        DETAIL in detail
        or any(marker in lowered for marker in _STOP_MARKERS)
        or bool(_PROVIDER_STATUS_RE.search(detail))
    )


def stop_detail(stderr: str) -> str:
    """The detail a Pi exit carries: the plain sentence for a balance stop.

    Every other exit keeps its own stderr untouched — only the provider's
    balance refusal has a payload that says nothing a reader can act on.
    """
    return DETAIL if is_stop(stderr) else stderr


def cause(language_code: str) -> str:
    """The reader-facing cause: the account has no balance."""
    return language.text(language_code, "failure.provider_balance_cause")


def action(language_code: str) -> str:
    """The reader-facing repair: top up or switch the key, then relabel."""
    return language.text(language_code, "failure.provider_balance_action")
