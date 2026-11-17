#!/usr/bin/env python3
"""The documents `orbi draft` reads and writes, and the answers it takes itself.

One drafting round takes one stdin document and writes one round document:
a reply plus the questions it still needs answered, the drafted Issues, or
a reply alone when the person explicitly asked for no code change
(Issues #2041, #2201). `DraftError` is the machine triple a caller reads on
failure. The shapes, their parsing and the errors live here, and
`orbi.draft` keeps the sessions, the fidelity pass and the creation.

A question is read by the ONE question parser `orbi.question` — the same
shape a stop question has (Issue #2228) — so a question means the same thing
wherever it appears. A question may carry options and mark one as the
reasonable default, either with the `(Recommended)` label suffix or with the
option's `recommended` field, and such a question is not the person's to
answer: the engine takes that option and the drafting continues in the same
round (Issues #2198, #2228). Only a question with no recommended option — the
request is empty or contradicts itself — is returned to the person.

Split out of `orbi.draft`, whose recorded size may only shrink
(`tools/size_ratchet.py`, Issue #2167): the documents and the rule about
questions live together here, and the engine stays one readable unit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from orbi.question import (
    RECOMMENDED_MARK,
    Option,
    Question,
    QuestionError,
    parse_question,
    recommended_option,
)

# A small fixed bound: a session that keeps asking a question it could answer
# itself must not spin (the termination guard the interactive loop keeps too,
# Issue #95).
TAKEN_ROUNDS = 3

# What a taken round sends in place of the person's answer: every option the
# engine took, where the draft records it and what the reply must say.
# English like the engine's other session instruction (`REWRITE_INSTRUCTION`);
# the Issue titles, bodies and reply stay the person's language.
TAKEN_INSTRUCTION = (
    "The person is not asked: the engine takes the recommended option of "
    "every question above.\n{decisions}\n"
    "Write the draft now with these decisions taken. Record each one under "
    "the Assumptions section of the Issue it affects, saying what was chosen "
    "and why, and name each one in your reply."
)


class DraftError(RuntimeError):
    """One failed step of `orbi draft`, with the machine triple a caller
    reads: a stable `code`, the `step` that failed and the concrete
    `detail` (docs/public-contracts.mdx)."""

    def __init__(self, code: str, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.code = code
        self.step = step
        self.detail = detail


@dataclass(frozen=True)
class DraftIssue:
    """One drafted Issue: its title, its body and its place in the order."""

    title: str
    body: str
    after: int | None = None

    def as_json(self) -> dict:
        data: dict = {"title": self.title, "body": self.body}
        if self.after is not None:
            data["after"] = self.after
        return data


@dataclass(frozen=True)
class DraftAnswer:
    """One drafting-session answer: questions, a draft, or a reply alone
    (Issue #2201), never two of them."""

    reply: str
    questions: tuple[Question, ...] = ()
    issues: tuple[DraftIssue, ...] = ()


def question_document(question: Question) -> dict:
    """One question as the drafting document writes it (Issues #2136, #2147):
    the question itself, an optional header naming the decision and the
    optional 2–4 options to pick from.

    The shape is the drafting document's, not the stop block's: a drafting
    round adds no `kind`, `id` or `summary`, so every question `orbi draft`
    returned before the shared parser produces the same document
    (Issue #2228).
    """
    data: dict = {"question": question.text}
    if question.header is not None:
        data["header"] = question.header
    if question.options:
        data["options"] = [
            {"label": option.label, "description": option.description}
            for option in question.options
        ]
    return data


@dataclass(frozen=True)
class DraftRound:
    """One round as the caller reads it (the stdout contract)."""

    reply: str
    questions: tuple[Question, ...] = ()
    issues: tuple[DraftIssue, ...] = ()
    check: str | None = None
    warning: str | None = None

    def as_json(self) -> dict:
        data: dict = {
            "reply": self.reply,
            "questions": [
                question_document(question) for question in self.questions
            ],
            "issues": [issue.as_json() for issue in self.issues],
        }
        if self.check is not None:
            data["check"] = self.check
        if self.warning is not None:
            data["warning"] = self.warning
        return data


def render_round(round_: DraftRound) -> str:
    """One round as the terminal text a caller prints: every Issue, the
    check it still fails and the fidelity warning."""
    lines: list[str] = []
    for position, issue in enumerate(round_.issues, start=1):
        lines.append(f"{position}. {issue.title}")
        lines.append(issue.body)
        if issue.after is not None:
            lines.append(f"   after: {issue.after}")
        lines.append("")
    if round_.check is not None and round_.check != "passed":
        lines.append(f"check: {round_.check}")
    if round_.warning:
        lines.append(f"warning: {round_.warning}")
    return "\n".join(lines).rstrip()


# --- parsing -------------------------------------------------------------

def invalid_input(detail: str) -> DraftError:
    """One unreadable stdin document (the `invalid_input` code)."""
    return DraftError("invalid_input", "read stdin", detail)


def _unreadable(detail: str) -> DraftError:
    """One answer the engine cannot read as a question (Issue #2136)."""
    return DraftError("no_answer", "read the draft", detail)


def read_payload(text: str) -> dict:
    """Parse one stdin document; a non-object or broken JSON fails fast."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise invalid_input(f"stdin is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise invalid_input("stdin must be a JSON object")
    return data


def parse_issues(value: object) -> tuple[DraftIssue, ...]:
    """Validate one draft's Issues: titles, bodies and the `after` order.

    An `after` may only name an EARLIER Issue (1-based): a draft is a
    delivery order, so a forward or self reference is not a draft we can
    create.
    """
    if not isinstance(value, list) or not value:
        raise DraftError(
            "invalid_draft", "read the draft",
            "the draft carries no Issues",
        )
    issues: list[DraftIssue] = []
    for position, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} is not an object",
            )
        title = entry.get("title")
        body = entry.get("body")
        if not isinstance(title, str) or not title.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has no title",
            )
        if "\n" in title.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has a multi-line title",
            )
        if not isinstance(body, str) or not body.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has no body",
            )
        after = entry.get("after")
        if after is not None:
            if isinstance(after, bool) or not isinstance(after, int) \
                    or not 1 <= after < position:
                raise DraftError(
                    "invalid_draft", "read the draft",
                    f"Issue {position}: after must name an EARLIER Issue "
                    f"(1..{position - 1})",
                )
        issues.append(
            DraftIssue(title=title.strip(), body=body.strip(), after=after)
        )
    return tuple(issues)


def parse_questions(value: object) -> tuple[Question, ...]:
    """Validate a round's questions with the ONE question parser.

    A bare string is a question with no header and no options
    (Issue #2147); every other rule — 2 to 4 options, each a label and a
    sentence, at most one recommended — is `orbi.question`'s, the same
    shape a stop question is read with (Issue #2228). Where a recommended
    option sits is the caller's business, never the parser's.
    """
    if not isinstance(value, list) or not value:
        raise _unreadable("the answer carries no questions")
    questions: list[Question] = []
    for position, entry in enumerate(value, start=1):
        if isinstance(entry, str):
            entry = {"question": entry}
        if not isinstance(entry, dict):
            raise _unreadable(f"question {position} is not an object")
        try:
            questions.append(parse_question(entry))
        except QuestionError as exc:
            raise _unreadable(f"question {position}: {exc}") from None
    return tuple(questions)


def parse_answer(value: object) -> DraftAnswer:
    """Parse the extension's answer file: a questions, a draft or a reply.

    A reply-only answer (Issue #2201) is the answer to a request that must
    not change any code: it carries no question and no Issue, so the round
    has nothing to revise or to judge.
    """
    if not isinstance(value, dict):
        raise DraftError(
            "no_answer", "read the draft",
            "the drafting session wrote no answer",
        )
    kind = value.get("kind")
    reply = value.get("reply")
    if not isinstance(reply, str):
        raise DraftError(
            "no_answer", "read the draft", "the answer carries no reply",
        )
    if kind == "questions":
        return DraftAnswer(
            reply=reply,
            questions=parse_questions(value.get("questions")),
        )
    if kind == "draft":
        return DraftAnswer(reply=reply, issues=parse_issues(value.get("issues")))
    if kind == "reply":
        return DraftAnswer(reply=reply)
    raise DraftError(
        "no_answer", "read the draft", f"unknown answer kind: {kind!r}",
    )


def taken_options(
    questions: tuple[Question, ...],
) -> tuple[tuple[Option, ...], tuple[Question, ...]]:
    """Split the questions of one answer (Issue #2198).

    Every question carrying a recommended option is answered by the engine
    with that option; only the rest are the person's to answer. Which option
    is recommended is the ONE rule the stops read too
    (`orbi.question.recommended_option`), so a drafting option marked
    `"recommended": true` counts exactly like a label ending in
    `(Recommended)` (Issue #2228).
    """
    taken: list[Option] = []
    waiting: list[Question] = []
    for question in questions:
        option = recommended_option(question)
        if option is None:
            waiting.append(question)
        else:
            taken.append(option)
    return tuple(taken), tuple(waiting)


def unmarked(option: Option) -> str:
    """The option's own words: its label without the recommended mark.

    The mark is removed only when the label really carries it: an option
    recommended by its `recommended` field keeps its whole label
    (Issue #2228).
    """
    return option.label.removesuffix(RECOMMENDED_MARK).strip() or option.label


def decision_lines(taken: tuple[Option, ...]) -> list[str]:
    """One line per option taken, in the option's own words."""
    return [f"- {unmarked(option)}: {option.description}" for option in taken]


def taken_message(taken: tuple[Option, ...]) -> str:
    """The message a taken round sends instead of the person's answer."""
    return TAKEN_INSTRUCTION.format(decisions="\n".join(decision_lines(taken)))


def named_in_reply(reply: str, taken: tuple[Option, ...]) -> str:
    """The round's reply, naming every option the engine took (Issue #2198).

    The session is asked to name them; the engine lists them anyway, so the
    person always reads which decisions were taken for them, in the words of
    the options they were offered.
    """
    if not taken:
        return reply
    return "\n".join([reply.strip(), *decision_lines(taken)]).strip()
