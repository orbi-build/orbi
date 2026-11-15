#!/usr/bin/env python3
"""`orbi draft`'s round document model (Issue #2201).

The shapes one drafting answer and one drafting round carry — the Issues and
their order, a question and its options, the answer the extension writes and
the round the caller reads — as pure data with no I/O, so the engine that
builds them and the CLI that prints them share one definition.

They moved out of `orbi.draft` (Issue #2201) because the size ratchet lets a
module only shrink: this is the one part of the engine that is data rather
than behaviour. `orbi.draft` imports them, so `draft.DraftIssue` and its
siblings still resolve for every existing caller.
"""
from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class DraftAnswer:
    """One drafting-session answer: questions, a draft, or a reply alone
    (Issue #2201)."""

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
