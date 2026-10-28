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
from string import Formatter
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
    "Answer by calling the clarify_verdict tool exactly once: it is "
    "the only tool in this session, and the run ends with that call. Do "
    "not answer in prose.\n\n"
    "The tool's missing argument lists the failed checks using exactly "
    'these identifiers: "observable_result", '
    '"acceptance_condition", "single_outcome"; it is empty when all '
    "three checks pass.\n\n"
    "Optional proposal. Add the tool's suggestion argument ONLY in "
    "these two cases, and only when the proposal follows from the "
    "ticket text alone: every result and every acceptance line must be "
    "stated there, adding and dropping nothing. When the text lacks a "
    "fact a proposal needs, or writing one needs a decision only a "
    "person can make, omit the argument and answer with missing "
    "alone.\n"
    '- ONLY single_outcome failed: suggestion {"kind": "split", '
    '"items": [{"result": "...", "acceptance": "...", "after": 1}]}. '
    "One item per new ticket: the fewest vertical slices that cover "
    "every goal of the text; after names the number of an earlier item "
    "this slice depends on and is omitted otherwise; there is no count "
    "cap.\n"
    '- ONLY acceptance_condition failed: suggestion {"kind": '
    '"criteria", "items": ["..."]}. One string per acceptance condition '
    "the ticket needs; never invent a goal.\n"
    "Every value stays on one line. Do not explain, and do not pass "
    "kind or items outside suggestion: the tool schema rejects them."
)

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
    write one, and rendered only when it answers the one missing piece.
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


# --- the editable proposal the stop comment may carry (Issue #1926) -------

SUGGESTION_SPLIT = "split"
SUGGESTION_CRITERIA = "criteria"
SUGGESTION_KINDS = (SUGGESTION_SPLIT, SUGGESTION_CRITERIA)

# The fixed markers around the proposal (docs/public-contracts.mdx). A
# person edits the plain lines between them; the markers are machine
# metadata and are never translated.
SUGGESTION_MARKER = "orbi:suggestion:v1"
SUGGESTION_START = f"<!-- {SUGGESTION_MARKER} kind="
SUGGESTION_END = "<!-- /orbi/suggestion -->"
SUGGESTION_START_PREFIX = f"<!-- {SUGGESTION_MARKER}"

# The one missing piece each proposal kind answers: a split plan proposes
# the split a "single_outcome" failure asks for, a criteria draft the
# acceptance an "acceptance_condition" failure asks for. Every other
# verdict renders no proposal.
SUGGESTION_FOR_MISSING = {
    "single_outcome": SUGGESTION_SPLIT,
    "acceptance_condition": SUGGESTION_CRITERIA,
}

# The one command sentence each proposal kind adds below its edit sentence
# (Issue #1927 for a split, Issue #1929 for criteria): the reader never
# copies the lines by hand, and the command reads the SAME section.
SUGGESTION_REPLY = {
    SUGGESTION_SPLIT: "clarify.suggestion.split_reply",
    SUGGESTION_CRITERIA: "clarify.suggestion.criteria_reply",
}


@dataclass(frozen=True)
class SuggestionItem:
    """One proposed line.

    A "split" item is one new ticket: "text" is its result, "acceptance"
    its acceptance line and "after" the 1-based position of the earlier
    item it depends on — a position, never the number a person wrote in an
    edited section, so a renumbered or reordered proposal keeps every
    dependency on its own item (Issue #1935). A "criteria" item is one
    condition and carries "text" only.
    """

    text: str
    acceptance: str = ""
    after: int | None = None


@dataclass(frozen=True)
class Suggestion:
    """The proposal the gate may attach to a failing verdict."""

    kind: str
    items: tuple[SuggestionItem, ...]


def _single_line(value: object) -> str | None:
    """A usable proposal line: non-empty, one line, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text.splitlines()) != 1:
        return None
    return text


def _usable(suggestion: Suggestion) -> bool:
    """Can this proposal be rendered, and is it a proposal at all?

    A split needs at least two slices (splitting into one contradicts the
    "single_outcome" diagnosis), a non-empty result and acceptance per
    slice, and an "after" that names an earlier slice; a criteria draft
    needs at least one condition. An unusable proposal renders nothing, so
    the stop comment stays exactly the pre-#1926 one.
    """
    if suggestion.kind not in SUGGESTION_KINDS or not suggestion.items:
        return False
    if any(not item.text for item in suggestion.items):
        return False
    if suggestion.kind == SUGGESTION_CRITERIA:
        return True
    if len(suggestion.items) < 2:
        return False
    for number, item in enumerate(suggestion.items, start=1):
        if not item.acceptance:
            return False
        if item.after is not None and not 1 <= item.after < number:
            return False
    return True


def suggestion_answer(value: object) -> Suggestion | None:
    """Parse the model's optional proposal; None when it is unusable.

    The answer is {"kind": "split"|"criteria", "items": [...]}: a split
    item is {"result", "acceptance", "after"?}, a criteria item one plain
    string. Anything else -- a wrong type, an unknown kind, an empty or
    single-slice split, a multi-line value, an "after" that does not name
    an earlier item -- is unusable and yields None: the verdict still stops
    the ticket with today's comment.
    """
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    raw_items = value.get("items")
    if kind not in SUGGESTION_KINDS or not isinstance(raw_items, list):
        return None
    items: list[SuggestionItem] = []
    for entry in raw_items:
        if kind == SUGGESTION_CRITERIA:
            text = _single_line(entry)
            if text is None:
                return None
            items.append(SuggestionItem(text=text))
            continue
        if not isinstance(entry, dict):
            return None
        text = _single_line(entry.get("result"))
        acceptance = _single_line(entry.get("acceptance"))
        after = entry.get("after")
        if text is None or acceptance is None:
            return None
        if after is not None and (
            isinstance(after, bool) or not isinstance(after, int)
        ):
            return None
        items.append(
            SuggestionItem(text=text, acceptance=acceptance, after=after)
        )
    suggestion = Suggestion(kind=kind, items=tuple(items))
    return suggestion if _usable(suggestion) else None


def render_suggestion(suggestion: Suggestion, language_code: str) -> str:
    """Render one proposal as the editable, marker-delimited section.

    The markers are fixed; the numbered slice, the "(after N)" phrase, the
    "Acceptance:" prefix and the edit sentence come from the message
    catalog, so a Chinese ticket gets the Chinese line grammar and the
    parser, which derives its patterns from the same templates, reads both.
    """
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    lines = [f"{SUGGESTION_START}{suggestion.kind} -->"]
    for number, item in enumerate(suggestion.items, start=1):
        if suggestion.kind == SUGGESTION_CRITERIA:
            lines.append(
                message("clarify.suggestion.criteria_item", text=item.text)
            )
            continue
        line = message(
            "clarify.suggestion.item", number=number, result=item.text,
        )
        if item.after is not None:
            line = f"{line} " + message(
                "clarify.suggestion.after", number=item.after,
            )
        lines.append(line)
        lines.append(
            message("clarify.suggestion.acceptance", text=item.acceptance)
        )
    lines.extend((SUGGESTION_END, "", message("clarify.suggestion.edit")))
    # A proposal is also an offer: the command that acts on these very
    # lines is named here (a split creates the tickets, criteria land in
    # the ticket body), so the reader never copies them by hand.
    lines.append(message(SUGGESTION_REPLY[suggestion.kind]))
    return "\n".join(lines)


def verdict_suggestion(verdict: ClarifyVerdict, language_code: str) -> str:
    """The proposal section for one failing verdict, or "".

    Rendered only when exactly one piece is missing, it is the piece this
    proposal kind answers, and the proposal is usable. Every other stop
    keeps the pre-#1926 comment byte for byte: no section, no edit
    sentence.
    """
    suggestion = verdict.suggestion
    if suggestion is None or len(verdict.missing) != 1:
        return ""
    if suggestion.kind != SUGGESTION_FOR_MISSING.get(verdict.missing[0]):
        return ""
    if not _usable(suggestion):
        return ""
    return render_suggestion(suggestion, language_code)


def _template_pattern(template: str,
                      fields: dict[str, tuple[str, str]]) -> str:
    """Compile one catalog template into a regex with named groups.

    "fields" maps a template placeholder to (group name, sub-pattern); the
    literal text around the placeholders is escaped. The parser's grammar is
    therefore generated from the very catalog templates the renderer
    formats, so the two cannot drift apart (Issue #1926).
    """
    pattern: list[str] = []
    for literal, name, _spec, _conversion in Formatter().parse(template):
        pattern.append(re.escape(literal))
        if name is not None:
            group, sub = fields[name]
            pattern.append(f"(?P<{group}>{sub})")
    return "".join(pattern)


def _acceptance_line(line: str) -> str | None:
    """The condition an acceptance line carries, in any catalog language."""
    for code in language.LANGUAGES:
        match = re.fullmatch(
            _template_pattern(
                language.text(code, "clarify.suggestion.acceptance"),
                {"text": ("text", r".+")},
            ),
            line,
        )
        if match is not None:
            return match.group("text").strip()
    return None


def _criteria_line(line: str) -> str | None:
    """The condition a criteria item line carries, in any language."""
    for code in language.LANGUAGES:
        match = re.fullmatch(
            _template_pattern(
                language.text(code, "clarify.suggestion.criteria_item"),
                {"text": ("text", r".+")},
            ),
            line,
        )
        if match is not None:
            return match.group("text").strip()
    return None


def _split_item_line(line: str) -> tuple[int, str, int | None] | None:
    """One split item line: (number, result, after) in any language.

    The dependency form is tried in EVERY language before the plain form:
    a plain item pattern would otherwise swallow the "(after N)" phrase of
    the other catalog language into its result text.
    """
    patterns = []
    for code in language.LANGUAGES:
        patterns.append((
            _template_pattern(
                language.text(code, "clarify.suggestion.item"),
                {
                    "number": ("number", r"[0-9]+"),
                    "result": ("result", r".+"),
                },
            ),
            _template_pattern(
                language.text(code, "clarify.suggestion.after"),
                {"number": ("after", r"[0-9]+")},
            ),
        ))
    for item, after in patterns:
        match = re.fullmatch(f"{item} {after}", line)
        if match is not None:
            return (
                int(match.group("number")),
                match.group("result").strip(),
                int(match.group("after")),
            )
    for item, _after in patterns:
        match = re.fullmatch(item, line)
        if match is not None:
            return (
                int(match.group("number")),
                match.group("result").strip(),
                None,
            )
    return None


def parse_suggestion(body: object) -> Suggestion | int | None:
    """The proposal one comment body carries (Issue #1926).

    Returns the Suggestion when the section is valid, the 1-based line
    number of the FIRST invalid line when it is not, and None when the body
    carries no start marker at all: no proposal is never an error. Line
    numbers count every line of the body from 1, markers included
    (docs/public-contracts.mdx).
    """
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    start = next(
        (index for index, line in enumerate(lines)
         if line.strip().startswith(SUGGESTION_START_PREFIX)),
        None,
    )
    if start is None:
        return None
    header = lines[start].strip()
    if not header.startswith(SUGGESTION_START) or not header.endswith(" -->"):
        return start + 1
    kind = header[len(SUGGESTION_START):-len(" -->")]
    if kind not in SUGGESTION_KINDS:
        return start + 1
    end: int | None = None
    for index in range(start + 1, len(lines)):
        line = lines[index].strip()
        if line == SUGGESTION_END:
            end = index
            break
        if line.startswith(SUGGESTION_START_PREFIX):
            return index + 1
    if end is None:
        return start + 1
    items: list[SuggestionItem] = []
    # The number written in the section -> the item's 1-based position in
    # "items". A person may leave gaps (5, 9) or reorder the lines, so the
    # written number is resolved here and never leaves the parser: the
    # stored dependency is the target item's position, exactly what the
    # model's answer carries and what the renderer numbers (Issue #1935).
    positions: dict[int, int] = {}
    index = start + 1
    while index < end:
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        line_number = index + 1
        if kind == SUGGESTION_CRITERIA:
            text = _criteria_line(line)
            if text is None:
                return line_number
            items.append(SuggestionItem(text=text))
            index += 1
            continue
        parsed = _split_item_line(line)
        if parsed is None:
            return line_number
        number, text, after = parsed
        acceptance_index = index + 1
        while (
            acceptance_index < end and not lines[acceptance_index].strip()
        ):
            acceptance_index += 1
        acceptance = None
        if acceptance_index < end:
            # The acceptance line keeps its indentation: it is part of the
            # catalog's grammar (three spaces, then the acceptance prefix).
            acceptance = _acceptance_line(lines[acceptance_index].rstrip())
        if acceptance is None:
            return line_number
        if number in positions or (
            after is not None and after not in positions
        ):
            return line_number
        items.append(SuggestionItem(
            text=text,
            acceptance=acceptance,
            after=None if after is None else positions[after],
        ))
        positions[number] = len(items)
        index = acceptance_index + 1
    if not items:
        return end + 1
    return Suggestion(kind=kind, items=tuple(items))


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
    # The editable proposal, only when this verdict's one missing piece is
    # the piece the proposal answers (Issue #1926). Without one the string
    # is empty, so the comment stays byte for byte as before.
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
                system_prompt=CLARIFY_SYSTEM_PROMPT,
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
