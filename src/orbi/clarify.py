#!/usr/bin/env python3
"""The thin-ticket clarification gate (Issue #1088).

A confident delivery against a vague `ai-ready` ticket is worse than no
delivery: it burns a run, opens a PR nobody can accept and buries the
real question. Before the claim starts any work, one no-tools Pi session
judges the ticket body against the three pieces every deliverable needs —
an **observable result**, an **acceptance condition** and a **single
outcome** — and a failing verdict stops the ticket at the claim:

- ONE Issue comment names every missing piece and the repair action;
- the repository's claim label (default `ai-ready`) is removed and
  `ai-needs-detail` is added, so the ticket waits for a human exactly
  like `ai-blocked` does;
- no worktree, branch, Pi session dir or PR is created for the attempt.

The gate is OFF by default (`clarify_thin_tickets = false`) and may be
turned on per repository through `.github/orbi.toml`. It fails OPEN on
every error path: a model error, timeout or malformed answer, or a failed
comment/label write, logs `clarify_check_skipped` and lets the delivery
proceed — a broken bypass must never silently lose a ticket.

A stop is recorded ONCE per ticket text (Issue #1904): the stop comment
carries a hidden digest of the text the gate judged, and a re-readied
ticket whose text still hashes to the newest stop is not judged,
commented or notified again — it is simply stopped again (the claim label
is removed, so it leaves the queue). Editing the ticket changes the digest
and the next claim judges it again. Because that marker decides a claim,
it is read only from the formal prefix of THIS runner's own comments: the
digest is derived from public ticket text, so anyone could compute a
forged one, a hidden marker never authenticates its author, and
marker-shaped text quoted in prose, a raw log or a code fence is not
official metadata (docs/public-contracts.mdx).

The module is the pure judgment layer: the model call is the
`pi_session.run_ticket_agent` leaf and the GitHub writes are the
`orbi.github` leaves, both resolved at call time so a test's seam patch
intercepts them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orbi import language, notify, user_summary
from orbi.delivery_labels import NEEDS_DETAIL_LABEL, READY_LABEL
from orbi.github import (
    authenticated_github_login, comment_issue, edit_issue, issue_comments,
    strip_bot_suffix,
)
from orbi.journal import event
from orbi.pi_session import run_ticket_agent
from orbi.progress import RUN_MARKER_PATTERN, run_marker

if TYPE_CHECKING:
    from orbi.config import RunnerConfig


# The three semantic check identifiers the model answers with, mapped to
# the catalog key of the human sentence the comment renders. The JSON
# protocol is unchanged (#1660): the identifiers stay machine values, and
# only the RENDERER turns each one into a sentence in the run's message
# language (English and Chinese both live in the shared catalog).
MISSING: dict[str, str] = {
    "observable_result": "clarify.missing.observable_result",
    "acceptance_condition": "clarify.missing.acceptance_condition",
    "single_outcome": "clarify.missing.single_outcome",
}


def missing_text(item: str, language_code: str,
                 ) -> str:
    """The human sentence for one failed check, in one language."""
    return language.text(language_code, MISSING[item])

CLARIFY_SYSTEM_PROMPT = (
    "You are Orbi's ticket gate. Before an unattended delivery starts, "
    "you decide whether this Issue states enough to deliver it. Judge "
    "only the Issue text, and judge what it says rather than the shape "
    "it is written in: separate template headings are optional, so a "
    "missing heading is never a missing piece.\n\n"
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
    "check only when the text bundles independent deliverables.\n\n"
    "Answer with ONE JSON object on one line and nothing else:\n"
    '{"missing": []}\n\n'
    "`missing` lists the failed checks using exactly these identifiers: "
    '"observable_result", "acceptance_condition", "single_outcome"; it '
    "is empty when all three checks pass. Do not explain and do not add "
    "other keys."
)

# A small fixed bound on the judgment call: a hung model request must
# not hold the slot. A timeout raises out of the shared helper and the
# caller's fail-open path lets the delivery proceed (Issue #1379).
CLARIFY_TIMEOUT_SECONDS = 120

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# The stop comment carries the digest of the exact text the gate judged
# (Issue #1904). A re-readied ticket whose text still hashes to the newest
# stop's digest is not judged again: the comment and notification already
# on the Issue cover it. Hidden because it is machine state, never prose.
CLARIFY_STOP_MARKER = "orbi:clarify-stop"
# The marker counts only in the formal prefix `render_comment()` writes
# (Issue #1908): the optional `@author` mention line, then the comment's
# run marker and then the stop marker. Marker-shaped text quoted deeper in
# a comment — prose, a raw log or a code fence such as a failure comment's
# `stdout_tail` — is not official metadata (docs/public-contracts.mdx),
# and honoring it would stop a ticket the gate never rejected.
_CLARIFY_STOP_RE = re.compile(
    r"\A(?:@[^\s\n]+\n)?"
    + RUN_MARKER_PATTERN.pattern
    + r"\n<!-- " + re.escape(CLARIFY_STOP_MARKER)
    + r":(?P<digest>[0-9a-f]{64}) -->"
)


def _author_mention(issue: dict) -> str:
    """Return the `@login` mention for the Issue author, or "".

    The login comes from the claim payload's `author.login` — the same
    `gh issue` JSON the claim scan already fetched, so the gate adds no
    API call. Any non-empty login is mentioned, bot or not (#1379); a
    missing/empty/non-string field means no mention: a malformed author
    must never fail the gate, it only renders the comment as before.
    """
    author = issue.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str):
        return ""
    login = login.strip()
    if not login:
        return ""
    return f"@{login}"


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


def judged_digest(issue: dict) -> str:
    """The digest of the exact text one judgment reads (Issue #1904)."""
    return hashlib.sha256(build_context(issue).encode("utf-8")).hexdigest()


def stop_marker(issue: dict) -> str:
    """The hidden marker a stop comment carries for its judged text."""
    return f"<!-- {CLARIFY_STOP_MARKER}:{judged_digest(issue)} -->"


def _runner_login(issue: dict) -> str | None:
    """The login this runner posts with, or None when it cannot be read.

    The stop marker steers a claim, so it is only accepted from this
    runner's own comments: the digest is computed from public ticket text,
    so any commenter could forge one, and a hidden marker never
    authenticates its author (docs/public-contracts.mdx). The identity
    read fails OPEN — an unreadable login means the gate judges the
    ticket exactly as before Issue #1904.
    """
    try:
        return strip_bot_suffix(authenticated_github_login())
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return None


def last_judged_digest(issue: dict, source_repo: str) -> str | None:
    """Return the digest of the newest stop by THIS runner, or None.

    Only the formal prefix counts (Issue #1908): the optional author
    mention line, the run marker and then the stop marker. Marker text
    quoted anywhere else — including a failure comment's fenced stdout —
    is not stop state, so a ticket that was never stopped keeps its claim.

    A marker on somebody else's comment is ignored (and journaled): it is
    a forged or copied marker, never a stop this engine recorded. The
    read fails open: an unreadable comment history means the gate judges
    the ticket exactly as it did before Issue #1904, so a broken read
    never loses a delivery.
    """
    try:
        comments = issue_comments(issue["number"], repo=source_repo)
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return None
    own: str | None = None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = _CLARIFY_STOP_RE.match(body)
        if match is None:
            continue
        if own is None:
            own = _runner_login(issue)
            if own is None:
                return None
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(login, str) or strip_bot_suffix(login) != own:
            event(
                "clarify_stop_untrusted", level=logging.WARNING,
                issue=issue["number"],
            )
            continue
        return match.group("digest")
    return None


def already_stopped(issue: dict, source_repo: str) -> bool:
    """True when the newest stop judged exactly this ticket text.

    Compares against `build_context`, the text the model reads: a title
    or body change is a new judgment, an unchanged ticket is not.
    """
    previous = last_judged_digest(issue, source_repo)
    return previous is not None and previous == judged_digest(issue)


def parse_verdict(output: str) -> ClarifyVerdict | None:
    """Parse one model answer; `None` means "no usable verdict".

    The answer is a JSON object with a `missing` list of known check
    identifiers; an empty list passes and a non-empty one fails. Anything
    else — prose without an object, broken JSON, a wrong type, or an
    unknown identifier — is `None`, and the caller fails open. The
    `satisfied` field is gone (#1379): one field carries the whole
    verdict.
    """
    match = _JSON_OBJECT_RE.search(output)
    if match is None:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    missing = data.get("missing")
    if not isinstance(missing, list):
        return None
    if any(
        not isinstance(item, str) or item not in MISSING
        for item in missing
    ):
        return None
    return ClarifyVerdict(passed=not missing, missing=tuple(missing))


def render_comment(issue: dict, verdict: ClarifyVerdict, run_id: str, *,
                   ready_label: str = READY_LABEL,
                   language_code: str,
                   status_url: str | None = None) -> str:
    """Render the ONE comment that names the missing pieces.

    The first line mentions the Issue author (`@<login>`, #1336) so the
    stop reaches a person through GitHub's mention notification; without
    a usable login the mention is omitted and the comment renders as
    before. `ready_label` is the repository's claim label (default
    `ai-ready`, #527): the repair instruction names the label the claim
    scan really reads, never a hardcoded default a custom-label
    repository does not use. The comment ends with the community help
    line (Issue #1684): this stop asks the author to step in.
    """
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    pieces = "\n".join(
        f"- {missing_text(item, language_code)}" for item in verdict.missing
    )
    # The task identity and the optional fixed project status link follow
    # the state headline, before the missing-piece diagnostics (#1826).
    summary = user_summary.summary_text(
        status_url=status_url, issue=issue.get("number"),
        title=issue.get("title"), language_code=language_code,
    )
    mention = _author_mention(issue)
    prefix = f"{mention}\n" if mention else ""
    return (
        f"{prefix}"
        f"{run_marker(run_id)}\n"
        f"{stop_marker(issue)}\n"
        f"{message('clarify.heading')}\n"
        f"{summary}"
        f"\n{message('clarify.intro')}\n\n"
        f"{pieces}\n\n"
        f"{message('clarify.repair', ready_label=ready_label)}\n\n"
        "```\n"
        f"{message('clarify.repair_outcome')}\n"
        f"{message('clarify.repair_acceptance')}\n"
        "```\n\n"
        f"{message('clarify.followup', needs_detail_label=NEEDS_DETAIL_LABEL)}"
        f"\n\nrun_id={run_id}"
        f"\n\n{message('help.community')}\n"
    )


def judge_issue_body(issue: dict, config: RunnerConfig, source_repo: str,
                     run_id: str) -> ClarifyVerdict | None:
    """Judge one ticket body; `None` when the judgment is unavailable.

    A model error or an unusable answer is recorded as
    `clarify_check_skipped` and returns `None` — the gate fails open and
    the delivery proceeds.
    """
    try:
        output = run_ticket_agent(
            issue, config, source_repo,
            system_prompt=CLARIFY_SYSTEM_PROMPT,
            context=build_context(issue),
            run_id=run_id,
            timeout=CLARIFY_TIMEOUT_SECONDS,
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
    logged as `clarify_check_skipped` and the delivery proceeds. The one
    exception is the unchanged, already-stopped ticket (Issue #1904): its
    stop was already written and notified, so a failed label re-write is
    logged as `clarify_already_stopped` and the ticket is STILL stopped —
    delivering a text the gate already rejected would be the worse
    outcome, and the ticket keeps the claim label for the next tick to
    stop again.
    """
    # The stop removes the label the claim scan actually reads: the
    # repository's dispatch label (default `ai-ready`, #527). Removing a
    # hardcoded `ai-ready` in a custom-label repository would leave the
    # ticket claimable, and the next tick would judge it again — the gate
    # would repeat instead of stopping.
    ready_label = config.dispatch_label or READY_LABEL
    if already_stopped(issue, source_repo):
        # Issue #1904: the automation re-added the claim label without
        # touching the text. The stop already named the missing pieces
        # and notified the author, so this claim must not judge, comment
        # or notify again. Re-applying the stop labels keeps the ticket
        # out of the claim queue, so it never starves other ready
        # tickets. A failed label write is logged and still not
        # delivered: delivering an unchanged, already-rejected ticket
        # would be the worse outcome.
        try:
            edit_issue(
                issue["number"], repo=source_repo,
                add=NEEDS_DETAIL_LABEL, remove=ready_label,
            )
        except Exception as exc:
            event(
                "clarify_already_stopped", level=logging.WARNING,
                issue=issue["number"], reason=type(exc).__name__,
            )
        else:
            event("clarify_already_stopped", issue=issue["number"])
        return False
    verdict = judge_issue_body(issue, config, source_repo, run_id)
    if verdict is None or verdict.passed:
        return True
    try:
        comment_issue(
            issue["number"], repo=source_repo,
            body=render_comment(
                issue, verdict, run_id, ready_label=ready_label,
                language_code=language.of(config),
                status_url=config.status_url,
            ),
        )
        edit_issue(
            issue["number"], repo=source_repo,
            add=NEEDS_DETAIL_LABEL, remove=ready_label,
        )
    except Exception as exc:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return True
    # Tell the person outside GitHub (Issue #1673): OUTSIDE the try (a
    # notify problem must never turn the failed-write branch into "deliver
    # anyway") and every stop sends — the old `ai-needs-detail` can still
    # be on a ticket someone just re-readied.
    notify.label_added(
        source_repo, issue["number"], NEEDS_DETAIL_LABEL,
        issue.get("title"),
    )
    event(
        "clarify_needs_detail", issue=issue["number"],
        missing=",".join(verdict.missing),
    )
    return False
