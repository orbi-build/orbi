"""The editable proposal a clarification stop comment may carry (Issue #1926).

The clarification gate's stop comment offers what it would change instead of
only naming what is missing: a `kind=split` plan (one item per new ticket) or a
`kind=criteria` draft (one line per acceptance condition). A person edits the
plain lines between two fixed markers, and a command (`/orbi split`,
`/orbi amend`) — or the gate itself, when the proposal is usable and the claim
can finish the job in place (Issue #2207) — acts on the section as it reads at
that moment.

This module is the pure proposal domain: the fixed markers, the two dataclasses,
the model's optional answer parser, the renderer, and the section parser whose
grammar is generated from the same catalog templates the renderer formats, so
the two cannot drift apart (Issue #1926). It performs no I/O — no model call, no
GitHub write — so both the gate (`orbi.clarify`) and the split engine
(`orbi.split_apply`) read a proposal without importing each other.

Issue #2207 moved it out of `clarify` untouched: that module sits at its
recorded size ceiling (`tools/size_ratchet.py`) and the gate's new automatic
apply needs room.

The markers are machine metadata and are never translated; everything a person
reads comes from the message catalog (EN/ZH).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from string import Formatter

from orbi import language


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
