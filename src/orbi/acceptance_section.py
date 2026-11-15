#!/usr/bin/env python3
"""Writing proposed acceptance conditions onto a ticket (Issue #1929).

The acceptance conditions a `kind=criteria` proposal carries land in the
ticket body under one section: an acceptance heading that follows the
repository's `message_language` (`## Acceptance` in English, `## 验收` in
Chinese, Issue #1971), one `- <condition>` line per condition and the hidden
`<!-- orbi:acceptance:v1 -->` marker after them. The marker is what makes
the append idempotent — a second run finds it and appends nothing, so the
body gains the section exactly once, however often the work is interrupted.

Two callers share this one contract:

- `/orbi amend` (Issue #1929) appends the section to a ticket the gate
  stopped, then queues it;
- the clarification gate itself (Issue #2206) appends it at the claim when
  its failing verdict carries a usable criteria proposal, posts the ONE
  comment listing what was added, and lets the same claim deliver.

The module also owns the write (`write_body`: the documented
`gh issue edit --body`) and the comment the gate adds after it, so both
writers produce the same text and a reviewer reads one implementation.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from orbi import language, user_summary
from orbi.github import comment_issue, run_gh_write_command
from orbi.journal import event
from orbi.progress import run_marker

# The heading the conditions land under follows the message language
# (Issue #1971), and the hidden marker that makes the append idempotent is
# the same in every language: the second run finds the marker in the body and
# adds nothing (docs/public-contracts.mdx).
ACCEPTANCE_HEADING_KEY = "criteria.acceptance_heading"
ACCEPTANCE_MARKER = "orbi:acceptance:v1"


def appended_marker() -> str:
    """The hidden line the appended section ends with."""
    return f"<!-- {ACCEPTANCE_MARKER} -->"


def acceptance_heading(language_code: str) -> str:
    """The section heading in one message language."""
    return language.text(language_code, ACCEPTANCE_HEADING_KEY)


def render_acceptance(
    conditions: Sequence[str], language_code: str,
) -> str:
    """The section appended to the ticket body: the conditions, verbatim.

    The heading and the list grammar are the ones a filed Issue uses, so the
    result reads like an acceptance section a person wrote, and the hidden
    marker after it records that Orbi appended it.
    """
    lines = [acceptance_heading(language_code)]
    lines.extend(f"- {condition}" for condition in conditions)
    lines.extend(["", appended_marker()])
    return "\n".join(lines)


def appended_body(body: object, section: str) -> str:
    """`body` with `section` appended after one blank line.

    An empty (or unreadable) body receives the section alone: the command
    never writes a leading blank block.
    """
    existing = body if isinstance(body, str) else ""
    if not existing.strip():
        return section
    kept = existing.rstrip("\n")
    return f"{kept}\n\n{section}"


def write_body(number: int, *, repo: str, body: str) -> None:
    """Replace one Issue body: the documented `gh issue edit --body`."""
    run_gh_write_command([
        "gh", "issue", "edit", str(number), "--repo", repo, "--body", body,
    ])


def render_applied_comment(
    issue: dict, conditions: Sequence[str], run_id: str, *,
    language_code: str, status_url: str | None = None,
) -> str:
    """The ONE comment listing the conditions the gate appended.

    The person reads what was added and that the conditions now live in the
    ticket, so editing the ticket changes them (Issue #2206). The title and
    the item lines are the ones `/orbi amend` posts: both paths describe the
    same append, so they read the same.
    """
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    summary = user_summary.summary_text(
        status_url=status_url, issue=issue.get("number"),
        title=issue.get("title"), language_code=language_code,
    )
    return "\n".join((
        run_marker(run_id),
        message("criteria.receipt_title"),
        f"{summary}",
        *[
            message("criteria.receipt_item", text=condition)
            for condition in conditions
        ],
        "",
        message(
            "clarify.criteria_appended",
            heading=acceptance_heading(language_code),
        ),
        "",
        f"run_id={run_id}",
    ))


def apply(
    issue: dict, conditions: Sequence[str], run_id: str, *,
    source_repo: str, language_code: str, status_url: str | None = None,
) -> bool:
    """Append `conditions` to the ticket and report them; True when done.

    `False` means the gate adds nothing and the caller falls back to the
    stop: the ticket already carries the appended section (the marker makes
    a second apply a no-op, so an already-amended ticket is never amended
    twice), or a write failed — nothing, or only the body, is on the ticket,
    the stop follows, and `/orbi amend` finishes the append.
    """
    body = issue.get("body")
    body = body if isinstance(body, str) else ""
    if appended_marker() in body:
        return False
    appended = appended_body(body, render_acceptance(conditions, language_code))
    try:
        write_body(issue["number"], repo=source_repo, body=appended)
    except Exception as exc:
        event(
            "clarify_criteria_apply_failed", level=logging.WARNING,
            issue=issue["number"], step="append", reason=type(exc).__name__,
        )
        return False
    # The write landed, so the ticket this claim carries (the delivery
    # session is rendered from it) carries the conditions too.
    issue["body"] = appended
    try:
        comment_issue(
            issue["number"], repo=source_repo,
            body=render_applied_comment(
                issue, conditions, run_id, language_code=language_code,
                status_url=status_url,
            ),
        )
    except Exception as exc:
        event(
            "clarify_criteria_apply_failed", level=logging.WARNING,
            issue=issue["number"], step="comment", reason=type(exc).__name__,
        )
        return False
    event(
        "clarify_criteria_appended", issue=issue["number"],
        count=len(conditions),
    )
    return True
