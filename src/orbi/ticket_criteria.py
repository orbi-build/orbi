#!/usr/bin/env python3
"""The three pieces every deliverable ticket needs (Issue #2041).

One piece of text, in one place: the pre-work check judges a ticket with it
(`clarify.CLARIFY_SYSTEM_PROMPT` embeds it byte for byte) and `orbi draft`
gives it to the drafting session. A drafter that wrote against a copy of
these rules produced Issues the check stopped — the copy is what this
module removes.

The identifiers are the JSON contract the judgment answers with (see
`clarify.MISSING`); the prose is the check's own wording, so changing the
bar means changing this text once.
"""
from __future__ import annotations

TICKET_CHECK_CRITERIA = (
    "Three checks:\n"
    "1. observable_result — the ticket states the observable result: "
    "what a user sees or does differently once the change ships. A "
    "concrete runtime failure — an error, a crash, a wrong returned "
    "value, a reproduced misbehavior named with its evidence — is such "
    "a result together with its unambiguous implied correction (\"it "
    "should not do this\"), even when no goal is written out "
    "explicitly.\n"
    "2. acceptance_condition — the ticket states an acceptance "
    "condition: a concrete way to tell a finished delivery from an "
    "unfinished one. Historical diagnostic evidence is not one: a run "
    "that already happened, a count from an earlier attempt or a "
    "symptom read from a log says what the author observed, not what "
    "the delivery must achieve. Report this check only when no "
    "condition is stated at all, and never invent a target, a cleanup "
    "policy or a delivery scope for it.\n"
    "3. single_outcome — the ticket is one runtime outcome: one fix or "
    "one change, not several unrelated things. One failing behavior, "
    "or several symptoms of the same behavior, is one outcome no "
    "matter how many bullets or sentences describe it; report this "
    "check only when the text bundles independent deliverables."
)
