#!/usr/bin/env python3
"""The thin-ticket clarification gate (Issue #1088).

A confident delivery against a vague `ai-ready` ticket is worse than no
delivery: it burns a run, opens a PR nobody can accept and buries the
real question. Before the claim starts any work, one no-tools Pi session
judges the ticket body against the three pieces every deliverable needs —
an **observable result**, an **acceptance condition** and a **single
outcome** — and a failing verdict stops the ticket at the claim:

- ONE Issue comment names every missing piece and the repair action;
- `ai-ready` is removed and `ai-needs-detail` is added, so the ticket
  waits for a human exactly like `ai-blocked` does;
- no worktree, branch, Pi session dir or PR is created for the attempt.

The gate is OFF by default (`clarify_thin_tickets = false`) and may be
turned on per repository through `.github/orbi.toml`. It fails OPEN on
every error path: a model error, timeout or malformed answer, or a failed
comment/label write, logs `clarify_check_skipped` and lets the delivery
proceed — a broken bypass must never silently lose a ticket.

The module is the pure judgment layer: the model call is the
`pi_session.run_clarify_agent` leaf and the GitHub writes are the
`orbi.github` leaves, both resolved at call time so a test's seam patch
intercepts them.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orbi.delivery_labels import NEEDS_DETAIL_LABEL, READY_LABEL
from orbi.github import comment_issue, edit_issue
from orbi.journal import event
from orbi.pi_session import run_clarify_agent
from orbi.progress import run_marker

if TYPE_CHECKING:
    from orbi.config import RunnerConfig


# The three semantic checks, in the identifier -> human-terms shape the
# comment renders: the model answers with the identifiers, the person
# reads the sentence.
MISSING: dict[str, str] = {
    "observable_result": (
        "No observable result: the ticket never says what a user sees or "
        "does differently once the change ships."
    ),
    "acceptance_condition": (
        "No acceptance condition: there is no concrete way to tell a "
        "finished delivery from an unfinished one."
    ),
    "single_outcome": (
        "More than one outcome: the ticket asks for several unrelated "
        "things instead of one fix or one change."
    ),
}

CLARIFY_SYSTEM_PROMPT = (
    "You are Orbi's ticket gate. Before an unattended delivery starts, "
    "you decide whether this Issue states enough to deliver it. Judge "
    "only the Issue text.\n\n"
    "Three checks:\n"
    "1. observable_result — the ticket states the observable result: "
    "what a user sees or does differently once the change ships.\n"
    "2. acceptance_condition — the ticket states an acceptance "
    "condition: a concrete way to tell a finished delivery from an "
    "unfinished one.\n"
    "3. single_outcome — the ticket is one runtime outcome: one fix or "
    "one change, not several unrelated things.\n\n"
    "Answer with ONE JSON object on one line and nothing else:\n"
    '{"satisfied": true|false, "missing": []}\n\n'
    "`satisfied` is true only when all three checks pass. `missing` "
    'lists the failed checks using exactly these identifiers: '
    '"observable_result", "acceptance_condition", "single_outcome"; it '
    "is empty when satisfied is true. Do not explain and do not add "
    "other keys."
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class ClarifyVerdict:
    """One judgment of a ticket body."""

    passed: bool
    missing: tuple[str, ...]


def build_context(issue: dict) -> str:
    """Return the judgment payload: the raw ticket text."""
    return (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"{issue.get('body') or ''}"
    )


def parse_verdict(output: str) -> ClarifyVerdict | None:
    """Parse one model answer; `None` means "no usable verdict".

    The answer is a JSON object with a boolean `satisfied` and a `missing`
    list of known check identifiers. Anything else — prose without an
    object, broken JSON, a wrong type, an unknown identifier, or a
    `satisfied` value that contradicts `missing` — is `None`, and the
    caller fails open.
    """
    match = _JSON_OBJECT_RE.search(output)
    if match is None:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    satisfied = data.get("satisfied")
    missing = data.get("missing")
    if not isinstance(satisfied, bool) or not isinstance(missing, list):
        return None
    if any(
        not isinstance(item, str) or item not in MISSING
        for item in missing
    ):
        return None
    if satisfied != (not missing):
        return None
    return ClarifyVerdict(passed=satisfied, missing=tuple(missing))


def render_comment(issue: dict, verdict: ClarifyVerdict, run_id: str) -> str:
    """Render the ONE comment that names the missing pieces."""
    pieces = "\n".join(f"- {MISSING[item]}" for item in verdict.missing)
    return (
        f"{run_marker(run_id)}\n"
        "**Orbi: this ticket is not ready to deliver.**\n\n"
        "The pre-flight check says the Issue does not state pieces a "
        "delivery needs:\n\n"
        f"{pieces}\n\n"
        f"Edit the Issue to close each gap, then add the `{READY_LABEL}` "
        "label again. Orbi re-checks the ticket when it claims it and "
        "starts the delivery once the pieces are there. Until then the "
        f"ticket carries `{NEEDS_DETAIL_LABEL}` and waits for you.\n\n"
        f"run_id={run_id}\n"
    )


def judge_issue_body(issue: dict, config: RunnerConfig, source_repo: str,
                     run_id: str) -> ClarifyVerdict | None:
    """Judge one ticket body; `None` when the judgment is unavailable.

    A model error or an unusable answer is recorded as
    `clarify_check_skipped` and returns `None` — the gate fails open and
    the delivery proceeds.
    """
    try:
        output = run_clarify_agent(
            issue, config, source_repo, run_id,
            system_prompt=CLARIFY_SYSTEM_PROMPT,
            context=build_context(issue),
        )
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return None
    verdict = parse_verdict(output)
    if verdict is None:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason="no_verdict",
        )
    return verdict


def enforce(issue: dict, config: RunnerConfig, source_repo: str,
            run_id: str) -> bool:
    """Run the gate for one fresh claim.

    `True` lets the delivery proceed; `False` stops it (the ticket waits
    for a human). A failed comment or label write is a bypass: it is
    logged as `clarify_check_skipped` and the delivery proceeds.
    """
    verdict = judge_issue_body(issue, config, source_repo, run_id)
    if verdict is None or verdict.passed:
        return True
    try:
        comment_issue(
            issue["number"], repo=source_repo,
            body=render_comment(issue, verdict, run_id),
        )
        edit_issue(
            issue["number"], repo=source_repo,
            add=NEEDS_DETAIL_LABEL, remove=READY_LABEL,
        )
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return True
    event(
        "clarify_needs_detail", issue=issue["number"],
        missing=",".join(verdict.missing),
    )
    return False
