"""The in-ticket command layer (Issue #1294).

A ticket the engine opened can carry a command in one of its comments: the
first was ``/milestone <version>`` (Issue #1290), which a hosted tenant can
send without host access or the CLI. This module is the generic layer that
extraction produced: regex construction, fenced/quoted handling, permission
enforcement, last-occurrence-wins and the receipts.

Each command registers a `CommandSpec` **explicitly** — no decorators
discovering things, no entry points, no dynamic import — the way
`JOURNAL_EVENTS` registers the journal events. `register_command` validates
the registration, so a duplicate name, an overlapping verb or an unknown
permission fails fast at import time instead of silently overwriting.

The shared layer owns the matching and the permission check:

- a command author supplies ``verbs``, never a regex, so the anchored
  pattern (which keeps ``> /milestone vX`` and fenced examples from firing)
  cannot be forgotten;
- a command declares a ``permission`` level, and the dispatcher enforces it
  before the handler ever sees the caller;
- ``process_commands`` is the single dispatch entry for one ticket — the
  only path to a handler — and it takes no caller state, so widening the
  scanning scope later is adding a call site, not changing an interface.

The decision functions (`parse_commands` / `select_command`) are
deterministic over the gathered comments and the command's ``validate``
callback; the GitHub I/O of the command's own business stays with the
registrant's ``apply`` callback.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from orbi.github import (
    TRUSTED_COMMENT_ASSOCIATIONS, _authenticated_github_login,
    _strip_bot_suffix, issue_comments,
)
from orbi.journal import event, run_command


def authenticated_login() -> str:
    """Public accessor for the login represented by the active credential."""
    return _authenticated_github_login()


def comment_author_login(comment: object) -> str | None:
    """The login of a comment's author, or None when it is unreadable."""
    if not isinstance(comment, dict):
        return None
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) and login else None


def same_github_identity(login: object, other: object) -> bool:
    """True when two logins name the same account (``[bot]`` normalized)."""
    if not isinstance(login, str) or not isinstance(other, str):
        return False
    return _strip_bot_suffix(login) == _strip_bot_suffix(other)


# The permission levels a CommandSpec may declare, level -> the comment
# author associations the dispatcher accepts. Declarative: the handler
# never checks a caller itself, so the help text and the gate cannot drift.
PERMISSION_LEVELS: dict[str, frozenset[str]] = {
    "write": TRUSTED_COMMENT_ASSOCIATIONS,
}


@dataclass(frozen=True)
class CommandSpec:
    """One in-ticket command: its verbs, its permission and its callbacks.

    ``validate(argument, **context)`` is the command's own business rule: it
    returns a readable rejection reason, or ``None`` when the argument is
    accepted. ``apply(argument, **context)`` performs the command and raises
    `TicketCommandError` naming the failed step. ``usage`` /
    ``description`` are the help reference the docs binding test pins.
    """

    name: str
    verbs: tuple[str, ...]
    permission: str
    validate: Callable[..., str | None]
    apply: Callable[..., None]
    usage: str
    description: str
    argument_label: str = "argument"


class TicketCommandRegistryError(RuntimeError):
    """A command registration contradicts the registry contract."""


class TicketCommandError(RuntimeError):
    """One command step failed; the receipt names the step and reason."""

    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


# The command registry: name -> CommandSpec, written out explicitly (the
# JOURNAL_EVENTS convention). A registrant adds its spec with
# register_command; the duplicate-name / overlapping-verb / unknown-
# permission checks make the registration itself the validation, so a typo
# fails at import time instead of at the first ticket that carries it.
TICKET_COMMANDS: dict[str, CommandSpec] = {}


def register_command(spec: CommandSpec) -> None:
    """Register one command; fail fast on a contract violation.

    Raises `TicketCommandRegistryError` for an empty name/verb set, a
    duplicate name, a verb already owned by another command, or a
    permission level not declared in `PERMISSION_LEVELS`.
    """
    if not spec.name or not spec.verbs:
        raise TicketCommandRegistryError(
            f"a ticket command needs a name and at least one verb: {spec.name!r}"
        )
    if spec.name in TICKET_COMMANDS:
        raise TicketCommandRegistryError(
            f"duplicate ticket command name: {spec.name!r}"
        )
    for verb in spec.verbs:
        for existing in TICKET_COMMANDS.values():
            if verb in existing.verbs:
                raise TicketCommandRegistryError(
                    f"ticket command verb {verb!r} on {spec.name!r} is "
                    f"already registered by {existing.name!r}"
                )
    if spec.permission not in PERMISSION_LEVELS:
        raise TicketCommandRegistryError(
            f"unknown permission level {spec.permission!r} for "
            f"ticket command {spec.name!r}"
        )
    TICKET_COMMANDS[spec.name] = spec


def strip_fenced_code_blocks(text: str) -> str:
    """Blank fenced code blocks so a documented command is not a command.

    The regex cannot see Markdown, so a command line inside a ``` or ~~~
    fence (the very way the command is explained) would otherwise trigger a
    real action. Fences are dropped line by line; the line count is
    preserved.
    """
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = None
        for candidate in ("```", "~~~"):
            if line.lstrip().startswith(candidate):
                marker = candidate
                break
        if fence is not None:
            if marker == fence:
                fence = None
            kept.append("")
            continue
        if marker is not None:
            fence = marker
            kept.append("")
            continue
        kept.append(line)
    return "\n".join(kept)


def _command_pattern(spec: CommandSpec) -> re.Pattern[str]:
    """The anchored matcher the shared layer compiles from a spec's verbs.

    A command author supplies verbs, never a pattern, so the anchor and the
    whitespace shape are identical for every command.
    """
    verbs = "|".join(re.escape(verb) for verb in spec.verbs)
    return re.compile(rf"(?mi)^/(?:{verbs})\b[ \t]*([^\r\n]*)$")


def parse_commands(text: object, spec: CommandSpec) -> list[str | None]:
    """Every command ``spec`` issues in a comment body, in line order.

    A command line whose argument is not exactly one field yields `None`: it
    is readable as a command but malformed, and the caller owes its author
    one readable receipt instead of silence.
    """
    if not isinstance(text, str) or not text:
        return []
    parsed: list[str | None] = []
    for argument in _command_pattern(spec).findall(strip_fenced_code_blocks(text)):
        fields = argument.split()
        parsed.append(fields[0] if len(fields) == 1 else None)
    return parsed


def _permission_reason(comment: dict, spec: CommandSpec) -> str:
    return (
        f"the command author has no {spec.permission} permission on this "
        f"repository (authorAssociation={comment.get('authorAssociation')!r})"
    )


def _malformed_reason(spec: CommandSpec) -> str:
    return (
        f"the command line is malformed: write exactly one "
        f"{spec.argument_label} on its own line, `{spec.usage}`"
    )


def select_command(
    comments: list[dict], spec: CommandSpec, *, runner_login: str,
    **context: object,
) -> tuple[tuple[dict, str] | None, list[tuple[dict, str | None, str]]]:
    """Resolve the ticket's occurrences of ``spec`` to one executable command.

    Returns `(target, rejections)`. `target` is the LAST occurrence when it
    is authorized, well-formed and accepted by the command's ``validate``
    callback — the last word wins, so an operator can correct a typo by
    commenting again. Every rejected occurrence is returned with a readable
    reason so the caller can leave one receipt each. The runner's own
    comments are skipped silently: the bot must never trigger itself off its
    own receipt text.
    """
    occurrences: list[tuple[dict, str | None]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        for argument in parse_commands(comment.get("body"), spec):
            occurrences.append((comment, argument))
    rejections: list[tuple[dict, str | None, str]] = []
    target: tuple[dict, str] | None = None
    for comment, argument in occurrences:
        login = comment_author_login(comment)
        if login is not None and same_github_identity(login, runner_login):
            continue
        if comment.get("authorAssociation") not in PERMISSION_LEVELS[spec.permission]:
            target = None
            rejections.append((
                comment, argument, _permission_reason(comment, spec),
            ))
            continue
        if argument is None:
            target = None
            rejections.append((comment, argument, _malformed_reason(spec)))
            continue
        reason = spec.validate(argument, **context)
        if reason is not None:
            target = None
            rejections.append((comment, argument, reason))
            continue
        target = (comment, argument)
    return target, rejections


def _command_comment_key(comment: dict) -> str:
    for field in ("id", "url"):
        value = comment.get(field)
        if value:
            return str(value)
    login = comment_author_login(comment) or "unknown"
    return f"{login}@{comment.get('createdAt') or 'unknown'}"


def _command_comment_ref(comment: dict) -> str:
    url = comment.get("url")
    if isinstance(url, str) and url:
        return url
    login = comment_author_login(comment) or "unknown"
    return f"comment by @{login} at {comment.get('createdAt') or 'unknown time'}"


def _command_receipt_marker(spec: CommandSpec) -> str:
    """The hidden marker prefix derived from the command's name."""
    return f"orbi-{spec.name}-command"


def _post_command_receipt(
    repo: str, issue_number: int, existing_comments: list[dict], comment: dict,
    *, spec: CommandSpec, marker: str, summary: str, argument: str | None,
    reason: str, step: str | None = None,
) -> None:
    """Post one reason comment, at most once per occurrence (idempotent)."""
    hidden = f"<!-- {marker} comment={_command_comment_key(comment)} -->"
    if any(
        isinstance(existing, dict) and isinstance(existing.get("body"), str)
        and hidden in existing["body"]
        for existing in existing_comments
    ):
        return
    lines = [summary, "", f"- reason: {reason}"]
    lines.append(
        f"- command line: `/{spec.name}`" if argument is None
        else f"- {spec.argument_label}: `{argument}`"
    )
    if step is not None:
        lines.append(f"- failed step: `{step}`")
    lines.extend([f"- command comment: {_command_comment_ref(comment)}", "", hidden])
    run_command([
        "gh", "issue", "comment", str(issue_number), "--repo", repo,
        "--body", "\n".join(lines),
    ], timeout=30)


def process_commands(
    repo: str, issue_number: int, commands: Iterable[CommandSpec],
    **context: object,
) -> None:
    """Evaluate a pending ticket's comments and run the winning command.

    The single dispatch entry: each spec in ``commands`` is parsed from the
    same comments, its rejections get one receipt each, and its winning
    occurrence reaches ``apply`` only through here.
    """
    comments = issue_comments(issue_number, repo=repo)
    runner_login = authenticated_login()
    for spec in commands:
        target, rejections = select_command(
            comments, spec, runner_login=runner_login, **context,
        )
        for comment, argument, reason in rejections:
            _post_command_receipt(
                repo, issue_number, comments, comment, spec=spec,
                marker=f"{_command_receipt_marker(spec)}-rejected",
                summary=f"**`/{spec.name}` command not applied**",
                argument=argument, reason=reason,
            )
        if target is None:
            continue
        comment, argument = target
        try:
            spec.apply(argument, repo=repo, issue_number=issue_number, **context)
        except TicketCommandError as exc:
            _post_command_receipt(
                repo, issue_number, comments, comment, spec=spec,
                marker=(
                    f"{_command_receipt_marker(spec)}-failed step={exc.step}"
                ),
                summary=f"**`/{spec.name} {argument}` command failed**",
                argument=argument, reason=exc.reason, step=exc.step,
            )
            event(
                f"{spec.name}_command_failed", level=logging.ERROR, repo=repo,
                **{spec.name: argument}, step=exc.step, reason=exc.reason,
            )
            continue
        event(
            f"{spec.name}_command_applied", repo=repo,
            **{spec.name: argument}, issue=f"#{issue_number}",
        )
