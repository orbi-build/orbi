#!/usr/bin/env python3
"""The documents `orbi draft` reads and writes, and the answers it takes itself.

One drafting round takes one stdin document and writes one round document:
a reply plus the questions it still needs answered, or the drafted Issues
(Issue #2041). `DraftError` is the machine triple a caller reads on failure.
The shapes, their parsing and the errors live here, and `orbi.draft` keeps
the sessions, the fidelity pass and the creation.

A question may carry options and mark one as the reasonable default with the
`(Recommended)` label suffix, and such a question is not the person's to
answer: the engine takes that option and the drafting continues in the same
round (Issue #2198). Only a question with no recommended option — the request
is empty or contradicts itself — is returned to the person.

Split out of `orbi.draft`, whose recorded size may only shrink
(`tools/size_ratchet.py`, Issue #2167): the documents and the rule about
questions live together here, and the engine stays one readable unit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# A recommended option's label carries this suffix (the Claude Agent SDK
# convention); where it sits is the caller's business, never the engine's.
RECOMMENDED_MARK = "(Recommended)"

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


@dataclass(frozen=True)
class DraftOption:
    """One choice a question offers: a short label and what it means."""

    label: str
    description: str

    def as_json(self) -> dict:
        return {"label": self.label, "description": self.description}


@dataclass(frozen=True)
class DraftQuestion:
    """One question (Issues #2136, #2147): the question itself, an optional
    header naming the decision and optional 2–4 options to pick from."""

    question: str
    header: str | None = None
    options: tuple[DraftOption, ...] = ()

    def as_json(self) -> dict:
        data: dict = {"question": self.question}
        if self.header is not None:
            data["header"] = self.header
        if self.options:
            data["options"] = [option.as_json() for option in self.options]
        return data


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
    """One drafting-session answer: questions, or a draft, never both."""

    reply: str
    questions: tuple[DraftQuestion, ...] = ()
    issues: tuple[DraftIssue, ...] = ()


@dataclass(frozen=True)
class DraftRound:
    """One round as the caller reads it (the stdout contract)."""

    reply: str
    questions: tuple[DraftQuestion, ...] = ()
    issues: tuple[DraftIssue, ...] = ()
    check: str | None = None
    warning: str | None = None

    def as_json(self) -> dict:
        data: dict = {
            "reply": self.reply,
            "questions": [question.as_json() for question in self.questions],
            "issues": [issue.as_json() for issue in self.issues],
        }
        if self.check is not None:
            data["check"] = self.check
        if self.warning is not None:
            data["warning"] = self.warning
        return data


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


def parse_options(value: object, position: int) -> tuple[DraftOption, ...]:
    """Validate one question's options WHEN it has any: 2 to 4, each a label
    and a sentence. A question without options is valid, and where a
    recommended option sits is the caller's business.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not 2 <= len(value) <= 4:
        raise _unreadable(f"question {position} needs 2 to 4 options")
    options: list[DraftOption] = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise _unreadable(
                f"question {position}, option {index} is not an object")
        label = entry.get("label")
        description = entry.get("description")
        if not isinstance(label, str) or not label.strip():
            raise _unreadable(
                f"question {position}, option {index} has no label")
        if not isinstance(description, str) or not description.strip():
            raise _unreadable(
                f"question {position}, option {index} has no description")
        options.append(
            DraftOption(label=label.strip(), description=description.strip())
        )
    return tuple(options)


def parse_questions(value: object) -> tuple[DraftQuestion, ...]:
    """Validate a round's questions: the shape the engine defines itself. A
    bare string is a question with no header and no options (Issue #2147).
    """
    if not isinstance(value, list) or not value:
        raise _unreadable("the answer carries no questions")
    questions: list[DraftQuestion] = []
    for position, entry in enumerate(value, start=1):
        if isinstance(entry, str):
            entry = {"question": entry}
        if not isinstance(entry, dict):
            raise _unreadable(f"question {position} is not an object")
        question, header = entry.get("question"), entry.get("header")
        if not isinstance(question, str) or not question.strip():
            raise _unreadable(f"question {position} has no question")
        if header is not None and (
            not isinstance(header, str) or not header.strip()
        ):
            raise _unreadable(f"question {position} has an unreadable header")
        questions.append(DraftQuestion(
            question=question.strip(),
            header=header.strip() if header is not None else None,
            options=parse_options(entry.get("options"), position),
        ))
    return tuple(questions)


def parse_answer(value: object) -> DraftAnswer:
    """Parse the extension's answer file: a questions or a draft answer."""
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
    raise DraftError(
        "no_answer", "read the draft", f"unknown answer kind: {kind!r}",
    )


def recommended(question: DraftQuestion) -> DraftOption | None:
    """The option whose label carries the recommended mark, or None."""
    return next(
        (option for option in question.options
         if option.label.endswith(RECOMMENDED_MARK)),
        None,
    )


def taken_options(
    questions: tuple[DraftQuestion, ...],
) -> tuple[tuple[DraftOption, ...], tuple[DraftQuestion, ...]]:
    """Split the questions of one answer (Issue #2198).

    Every question carrying a recommended option is answered by the engine
    with that option; only the rest are the person's to answer.
    """
    taken: list[DraftOption] = []
    waiting: list[DraftQuestion] = []
    for question in questions:
        option = recommended(question)
        if option is None:
            waiting.append(question)
        else:
            taken.append(option)
    return tuple(taken), tuple(waiting)


def unmarked(option: DraftOption) -> str:
    """The option's label without the recommended mark."""
    return option.label[:-len(RECOMMENDED_MARK)].strip() or option.label


def decision_lines(taken: tuple[DraftOption, ...]) -> list[str]:
    """One line per option taken, in the option's own words."""
    return [f"- {unmarked(option)}: {option.description}" for option in taken]


def taken_message(taken: tuple[DraftOption, ...]) -> str:
    """The message a taken round sends instead of the person's answer."""
    return TAKEN_INSTRUCTION.format(decisions="\n".join(decision_lines(taken)))


def named_in_reply(reply: str, taken: tuple[DraftOption, ...]) -> str:
    """The round's reply, naming every option the engine took (Issue #2198).

    The session is asked to name them; the engine lists them anyway, so the
    person always reads which decisions were taken for them, in the words of
    the options they were offered.
    """
    if not taken:
        return reply
    return "\n".join([reply.strip(), *decision_lines(taken)]).strip()
