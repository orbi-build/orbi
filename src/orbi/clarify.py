#!/usr/bin/env python3
"""The thin-ticket clarification gate (Issue #1088).

A confident delivery against a vague `ai-ready` ticket is worse than no
delivery: it burns a run, opens a PR nobody can accept and buries the
real question. Before the claim starts any work, one Pi session with no
built-in tools — the shipped `clarify_verdict` tool is its only answer
path (Issue #1955) — judges the ticket body against the three pieces
every deliverable needs — an **observable result**, an **acceptance
condition** and a **single outcome** — and a failing verdict stops the
ticket at the claim:

- ONE Issue comment names every missing piece and the repair action;
- the repository's claim label (default `ai-ready`) is removed and
  `ai-needs-detail` is added, so the ticket waits for a human exactly
  like `ai-blocked` does;
- no worktree, branch, Pi session dir or PR is created for the attempt.

A failing verdict that carries a **usable split proposal** is not stopped
(Issue #2207): the proposal follows from the ticket text alone and the
author already authorized the work by adding the claim label, so the gate
applies it in the same claim — exactly what `/orbi split` does. The child
tickets are created, linked and queued, this ticket becomes `ai-epic` and
loses its claim label, ONE comment lists the children, and the claim
delivers nothing. `orbi.split_apply` owns those steps, so an apply that is
interrupted here is finished by the next claim (no duplicate child) and an
apply that hits a GitHub write failure falls back to the stop above — whose
comment still carries the proposal, so `/orbi split` can finish it. A
half-split ticket is therefore never delivered.

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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import draft_marker, language, notify, split_apply, user_summary
# The proposal domain moved to its own leaf module (Issue #2207) because this
# one sits at its recorded size ceiling; the names stay importable HERE, the
# module every caller and test already reads them from.
from orbi.clarify_proposal import (
    SUGGESTION_CRITERIA,
    SUGGESTION_END,
    SUGGESTION_KINDS,
    SUGGESTION_MARKER,
    SUGGESTION_REPLY,
    SUGGESTION_SPLIT,
    SUGGESTION_START,
    SUGGESTION_START_PREFIX,
    Suggestion,
    SuggestionItem,
    _usable,
    parse_suggestion,
    render_suggestion,
    suggestion_answer,
)
from orbi.ticket_criteria import TICKET_CHECK_CRITERIA
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


# The gate's answer is a schema-checked Pi tool call, not free text
# (Issue #1955). The extension ships INSIDE the package
# (`pyproject` package-data), so both the editable install and the wheel
# resolve the same file; the engine names the file the tool writes in
# `CLARIFY_VERDICT_ENV` and reads it after the session.
CLARIFY_EXTENSION = Path(__file__).resolve().parent.joinpath(
    "extensions", "clarify_verdict.ts",
)
CLARIFY_VERDICT_ENV = "ORBI_CLARIFY_OUT"

CLARIFY_SYSTEM_PROMPT = (
    "You are Orbi's ticket gate. Before an unattended delivery starts, "
    "you decide whether this Issue states enough to deliver it. Judge "
    "only the Issue text, and judge what it says rather than the shape "
    "it is written in: separate template headings are optional, so a "
    "missing heading is never a missing piece.\n\n"
    + TICKET_CHECK_CRITERIA
    + "\n\n"
    "Answer by calling the clarify_verdict tool exactly once: it is "
    "the only tool in this session, and the run ends with that call. Do "
    "not answer in prose.\n\n"
    "The tool's missing argument lists the failed checks using exactly "
    'these identifiers: "observable_result", '
    '"acceptance_condition", "single_outcome"; it is empty when all '
    "three checks pass.\n\n"
    "Optional proposal. Add the tool's suggestion argument ONLY in "
    "these two cases, and only when the proposal follows from the "
    "ticket text alone, adding no goal or scope the text does not "
    "state. When the text lacks a fact a proposal needs, or writing one "
    "needs a decision only a person can make, omit the argument and "
    "answer with missing alone.\n"
    '- single_outcome failed, with or without "acceptance_condition" '
    'also failing: suggestion {"kind": "split", '
    '"items": [{"result": "...", "acceptance": "...", "after": 1}]}. '
    "One item per new ticket, each result and acceptance stated in the "
    "text: the fewest vertical slices that cover every goal of the "
    "text; after names the number of an earlier item "
    "this slice depends on and is omitted otherwise; there is no count "
    "cap.\n"
    '- ONLY acceptance_condition failed: suggestion {"kind": '
    '"criteria", "items": ["..."]}. One string per acceptance condition: '
    "a concrete check that tells a finished delivery of the goals the "
    "text states from an unfinished one; never invent a goal.\n"
    "Every value stays on one line. Do not explain, and do not pass "
    "kind or items outside suggestion: the tool schema rejects them."
)

# The proposal's VALUES are the model's own prose, so their language cannot
# be left to the model (Issue #1971): on beta a zh repository's criteria
# draft came out English inside a Chinese stop comment, and asking the model
# to detect the ticket's language changed the verdict itself. The configured
# message language -- the engine's own setting, never a detection -- is
# named in one catalog sentence.
CLARIFY_OUTPUT_LANGUAGE_KEY = "clarify.prompt.output_language"


def render_system_prompt(language_code: str) -> str:
    """The language-independent protocol plus the proposal's output
    language, so the draft follows `message_language`, never a guess."""
    return "\n\n".join((
        CLARIFY_SYSTEM_PROMPT,
        language.text(language_code, CLARIFY_OUTPUT_LANGUAGE_KEY),
    ))


# A small fixed bound on the judgment call: a hung model request must
# not hold the slot. A timeout raises out of the shared helper and the
# caller's fail-open path lets the delivery proceed (Issue #1379).
CLARIFY_TIMEOUT_SECONDS = 120

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
    """One judgment of a ticket body.

    "suggestion" is the optional proposal the model attached to a failing
    verdict (Issue #1926): present only when the ticket text was enough to
    write one, and rendered only when it answers the failed checks — a split
    answers `single_outcome` with or without a missing acceptance condition
    (Issue #1978), a criteria draft an acceptance condition alone.
    "suggestion_dropped" says WHY a proposal the answer carried was
    discarded (Issue #1958) — "misplaced" when the keys sat at the top
    level instead of under "suggestion", "unusable" when the value failed
    validation — so the gate can journal the drop instead of losing it
    silently. It stays None when the answer carried no proposal and on a
    passing verdict (which renders no proposal at all).
    """

    passed: bool
    missing: tuple[str, ...]
    suggestion: Suggestion | None = None
    suggestion_dropped: str | None = None


def verdict_suggestion(verdict: ClarifyVerdict, language_code: str) -> str:
    """The proposal section for one failing verdict, or "".

    A split is rendered whenever `single_outcome` is among the failed checks,
    with or without a missing acceptance condition (Issue #1978): every split
    item carries its own acceptance line, so one split plan answers both
    failures, and a ticket that should be split gets the split plan. The
    criteria draft keeps its single-piece rule — it answers an
    `acceptance_condition` failure alone (Issue #1926). An unusable proposal
    renders nothing, so every other stop keeps the pre-#1926 comment byte for
    byte: no section, no edit sentence.
    """
    suggestion = verdict.suggestion
    if suggestion is None or not _usable(suggestion):
        return ""
    if suggestion.kind == SUGGESTION_SPLIT:
        if "single_outcome" not in verdict.missing:
            return ""
    elif verdict.missing != ("acceptance_condition",):
        return ""
    return render_suggestion(suggestion, language_code)


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


def authoritative_stop(
    issue: dict, source_repo: str, *, comments: list[dict] | None = None,
) -> dict | None:
    """The newest stop comment THIS runner wrote, or None.

    The one locator of the authoritative stop (Issue #1926), so the digest
    check and every reader of the proposal select the same comment.

    Only the formal prefix counts (Issue #1908): the optional author
    mention line, the run marker and then the stop marker. Marker text
    quoted anywhere else — including a failure comment's fenced stdout —
    is not stop state, so a ticket that was never stopped keeps its claim.

    A marker on somebody else's comment is ignored (and journaled): it is
    a forged or copied marker, never a stop this engine recorded. The
    read fails open: an unreadable comment history means the gate judges
    the ticket exactly as it did before Issue #1904, so a broken read
    never loses a delivery.

    Selection is comment creation order (oldest first), never the edit
    time: a stop keeps its position when a person edits it. The newest
    trusted stop wins even when it carries no proposal, and an older
    stop's proposal is never reused.

    ``comments`` lets a caller that already read the ticket's comments
    (the command dispatcher) reuse that read instead of paying a second
    one; the selection itself is unchanged.
    """
    try:
        if comments is None:
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
        if _CLARIFY_STOP_RE.match(body) is None:
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
        return comment
    return None


def last_judged_digest(issue: dict, source_repo: str) -> str | None:
    """Return the digest of the newest stop by THIS runner, or None."""
    comment = authoritative_stop(issue, source_repo)
    if comment is None:
        return None
    match = _CLARIFY_STOP_RE.match(comment["body"])
    return match.group("digest") if match is not None else None


def stop_suggestion(
    issue: dict, source_repo: str, *, comments: list[dict] | None = None,
) -> Suggestion | int | None:
    """The proposal on the authoritative stop comment, or None.

    Same result convention as parse_suggestion: the Suggestion, the
    1-based line number of its first invalid line, or None when the newest
    trusted stop carries no section. `/orbi split` (#1927) consumes this
    contract and passes the comments it already read.
    """
    comment = authoritative_stop(issue, source_repo, comments=comments)
    if comment is None:
        return None
    return parse_suggestion(comment.get("body"))


def already_stopped(issue: dict, source_repo: str) -> bool:
    """True when the newest stop judged exactly this ticket text.

    Compares against `build_context`, the text the model reads: a title
    or body change is a new judgment, an unchanged ticket is not.
    """
    previous = last_judged_digest(issue, source_repo)
    return previous is not None and previous == judged_digest(issue)


def parse_verdict(data: object) -> ClarifyVerdict | None:
    """Read one decoded tool answer; `None` means "no usable verdict".

    The answer is the JSON object the shipped `clarify_verdict` tool
    wrote (Issue #1955): Pi validated its arguments against the tool
    schema before `execute`, so this reader only re-checks the contract
    the engine itself depends on — a `missing` list of known identifiers,
    where an empty list passes and a non-empty one fails. Anything else —
    a payload that is not an object, a wrong type, an unknown identifier
    — is `None`, and the caller fails open exactly as it did when the
    same answer arrived as prose. The `satisfied` field is gone (#1379):
    one field carries the whole verdict. A proposal the answer carries but
    the engine cannot render is NOT a parse failure: the verdict keeps its
    `missing` list and records why the proposal went with
    `suggestion_dropped` (Issue #1958).
    """
    if not isinstance(data, dict):
        return None
    missing = data.get("missing")
    if not isinstance(missing, list):
        return None
    if any(
        not isinstance(item, str) or item not in MISSING
        for item in missing
    ):
        return None
    suggestion = suggestion_answer(data.get("suggestion"))
    return ClarifyVerdict(
        passed=not missing, missing=tuple(missing), suggestion=suggestion,
        suggestion_dropped=_suggestion_dropped(data, missing, suggestion),
    )


def _suggestion_dropped(
    data: dict, missing: list, suggestion: Suggestion | None,
) -> str | None:
    """Why this failing answer's proposal was dropped, or None (Issue #1958).

    A failing answer that carries a proposal the engine cannot render used
    to lose it without a trace: the stop comment posted exactly as if the
    model had proposed nothing, so a broken proposal path looked healthy
    (#1955). The reason names the drift a person can repair —
    "misplaced" when "kind"/"items" sit at the top level instead of under
    "suggestion", "unusable" when the "suggestion" value fails validation.
    No proposal at all, and a passing verdict (nothing to render), drop
    nothing.
    """
    if not missing or suggestion is not None:
        return None
    if "suggestion" in data:
        return "unusable"
    if "kind" in data or "items" in data:
        return "misplaced"
    return None


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

    A verdict that renders a proposal gives ONE instruction (Issue #1962):
    the proposal, its edit sentence and the command sentence that acts on
    it. The generic repair block is rendered only when there is no
    proposal, so the reader never has to choose between two instructions.
    """
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    pieces = "\n".join(
        f"- {missing_text(item, language_code)}" for item in verdict.missing
    )
    # The editable proposal, only when it answers this verdict's failed
    # checks (Issue #1926, #1978). Without one the string is empty, so the
    # comment stays byte for byte as before.
    proposal = verdict_suggestion(verdict, language_code)
    # A proposal IS the instruction (Issue #1962): the comment then carries
    # the proposal, its edit sentence and its command sentence, and never
    # the generic repair block — the reader would otherwise see two
    # competing instructions. Without a proposal the comment keeps the one
    # repair instruction it always gave.
    if proposal:
        instruction = f"{proposal}\n\n"
    else:
        instruction = (
            f"{message('clarify.repair', ready_label=ready_label)}\n\n"
            "```\n"
            f"{message('clarify.repair_outcome')}\n"
            f"{message('clarify.repair_acceptance')}\n"
            "```\n\n"
            f"{message('clarify.followup', needs_detail_label=NEEDS_DETAIL_LABEL)}"
            "\n\n"
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
        f"{instruction}"
        f"run_id={run_id}"
        f"\n\n{message('help.community')}\n"
    )


def verdict_file_answer(path: Path) -> ClarifyVerdict | None:
    """The verdict written by the tool at `path`, or `None`.

    The gate's answer is a file, not stdout (Issue #1955): a session that
    never called the tool leaves no file, and a broken or schema-breaking
    write leaves unreadable JSON. Both are the same unusable answer as
    before — no verdict — so the caller fails open.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_verdict(data)


def judge_issue_body(issue: dict, config: RunnerConfig, source_repo: str,
                     run_id: str) -> ClarifyVerdict | None:
    """Judge one ticket body; `None` when the judgment is unavailable.

    The session loads ONLY the shipped `clarify_verdict` extension with no
    built-in tools, and the engine names the file its `execute` writes in
    `CLARIFY_VERDICT_ENV`; the file is read after the session and lives
    outside it, so the temp dir the session runs in can be discarded. A
    model error or an unusable answer — no file, broken JSON, an unknown
    check id — is recorded as `clarify_check_skipped` and returns `None`:
    the gate fails open and the delivery proceeds.
    """
    with tempfile.TemporaryDirectory(prefix="orbi-clarify-") as directory:
        verdict_path = Path(directory) / "verdict.json"
        try:
            run_ticket_agent(
                issue, config, source_repo,
                system_prompt=render_system_prompt(language.of(config)),
                context=build_context(issue),
                run_id=run_id,
                timeout=CLARIFY_TIMEOUT_SECONDS,
                extension=str(CLARIFY_EXTENSION),
                env={CLARIFY_VERDICT_ENV: str(verdict_path)},
            )
        except Exception as exc:
            event(
                "clarify_check_skipped", level=logging.WARNING,
                issue=issue["number"], reason=type(exc).__name__,
            )
            return None
        verdict = verdict_file_answer(verdict_path)
    if verdict is None:
        event(
            "clarify_check_skipped", level=logging.WARNING,
            issue=issue["number"], reason="no_verdict",
        )
    elif verdict.suggestion_dropped is not None:
        # Issue #1958: the stop comment posts without the proposal, so the
        # only trace of a broken proposal path is this line.
        event(
            "clarify_suggestion_dropped", level=logging.WARNING,
            issue=issue["number"], reason=verdict.suggestion_dropped,
        )
    return verdict


def usable_split(verdict: ClarifyVerdict) -> Suggestion | None:
    """The split proposal this failing verdict applies itself, or None.

    The rule is the one the stop comment renders by (#1978): a split answers
    `single_outcome`, so a proposal attached to a verdict that did not fail
    it is rendered by nothing and applied by nothing. Usability is already
    decided where the answer is parsed — `suggestion_answer` keeps only a
    proposal it can render — so there is no second rule here.
    """
    suggestion = verdict.suggestion
    if suggestion is None or suggestion.kind != SUGGESTION_SPLIT:
        return None
    return suggestion if "single_outcome" in verdict.missing else None


def apply_split(issue: dict, config: RunnerConfig, proposal: Suggestion,
                run_id: str, *, ready_label: str, source_repo: str) -> bool:
    """Apply the proposal in this claim: children queued, parent `ai-epic`.

    The person already authorized the work by adding the claim label, and the
    proposal is the engine's own reading of the ticket (the same principle as
    #2133), so the gate finishes the split instead of asking somebody to reply
    `/orbi split`. The steps ride `split_apply.apply_proposal`, the ONE
    implementation `/orbi split` uses, so an apply interrupted here is
    finished by the other and no child is ever duplicated.

    `True` means the parent carries `ai-epic` without its claim label and the
    children are on GitHub: this claim delivers nothing. A GitHub write failure
    returns `False`, and the caller falls back to today's stop — its comment
    still carries the proposal and the ticket carries `ai-needs-detail`, so
    `/orbi split` finishes the same child tickets from what is already there.
    An interruption (`KeyboardInterrupt`, `SystemExit`) is NOT caught
    (`Exception` only): the claim label is still on the ticket, so the next
    tick judges it again and adopts the children this attempt created.
    """
    language_code = language.of(config)
    try:
        parent = split_apply.issue_view(
            issue["number"], split_apply.PARENT_FIELDS, repo=source_repo,
        )
        children = split_apply.apply_proposal(
            proposal.items, repo=source_repo, issue=parent,
            dispatch_label=ready_label, language_code=language_code,
        )
        comment_issue(
            issue["number"], repo=source_repo,
            body=(
                f"{run_marker(run_id)}\n"
                f"{split_apply.split_receipt(children, language_code)}"
                f"\n\nrun_id={run_id}"
            ),
        )
    except Exception as exc:
        event(
            "clarify_split_failed", level=logging.WARNING,
            issue=issue["number"], reason=type(exc).__name__,
        )
        return False
    event(
        "clarify_split_applied", issue=issue["number"],
        children=len(children),
    )
    return True


def enforce(issue: dict, config: RunnerConfig, source_repo: str,
            run_id: str) -> bool:
    """Run the gate for one fresh claim.

    `True` lets the delivery proceed; `False` stops it (the ticket waits
    for a human). A failing verdict whose proposal the gate can apply itself
    is applied instead of stopped (Issue #2207): a usable `split` creates the
    child tickets, queues them, marks this ticket `ai-epic` and posts ONE
    comment listing them, so this claim delivers nothing and no human command
    is needed.

    A failed comment or label write is a bypass: it is logged as
    `clarify_check_skipped` and the delivery proceeds. Two exceptions: the
    unchanged, already-stopped ticket (Issue #1904), whose failed label
    re-write is logged as `clarify_already_stopped` and which is STILL
    stopped — delivering a text the gate already rejected would be the worse
    outcome, and the ticket keeps the claim label for the next tick to stop
    again; and a ticket whose split apply already began, which is never
    delivered as one ticket even when the fallback stop cannot be written.
    """
    # The stop removes the label the claim scan actually reads: the
    # repository's dispatch label (default `ai-ready`, #527). Removing a
    # hardcoded `ai-ready` in a custom-label repository would leave the
    # ticket claimable, and the next tick would judge it again — the gate
    # would repeat instead of stopping.
    ready_label = config.dispatch_label or READY_LABEL
    if draft_marker.own_draft_unchanged(issue):
        # Issue #2041: `orbi draft` already judged this exact body with
        # THIS function before the person ever saw it, and nobody has
        # edited it since (the marker's fingerprint still matches). Orbi
        # does not bounce its own drafts: the check would repeat a
        # judgment it already made, and a draft that failed it after the
        # rewrite is the person's to submit — not the gate's to stop.
        event("clarify_draft_skipped", issue=issue["number"])
        return True
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
    proposal = usable_split(verdict)
    split = proposal is not None and apply_split(
        issue, config, proposal, run_id,
        ready_label=ready_label, source_repo=source_repo,
    )
    if split:
        return False
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
        # A split attempt already created children (#2207): never deliver it.
        return proposal is None
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
