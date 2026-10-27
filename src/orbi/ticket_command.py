"""The in-ticket command layer (Issue #1294).

A ticket the engine opened can carry a command in one of its comments: the
first was ``/milestone <version>`` (Issue #1290; canonical
``/orbi milestone <version>`` since #1922), which a hosted tenant can send
without host access or the CLI. This module is the generic layer that
extraction produced: regex construction, fenced/quoted handling, permission
enforcement, last-occurrence-wins and the receipts.

Every command is written ``/orbi <subcommand> [argument]`` (Issue #1922):
the shared layer owns the prefix, so a registered command inherits it. The
machine id (``name``) stays separate from what a user types
(``subcommand``) because it is baked into the receipt markers and the
journal event names of receipts already posted.

Each command registers a `CommandSpec` **explicitly** — no decorators
discovering things, no entry points, no dynamic import — the way
`JOURNAL_EVENTS` registers the journal events. `register_command` validates
the registration, so a duplicate name, a duplicate subcommand or alias, or
an unknown permission fails fast at import time instead of silently
overwriting, and it registers the command's journal events: a new command
is one spec plus one registration.

The shared layer owns the matching and the permission check:

- a command author supplies a ``subcommand`` (plus optional legacy
  ``aliases``), never a regex, so the anchored pattern (which keeps
  ``> /orbi fix-base`` and fenced examples from firing) cannot be
  forgotten;
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

from orbi import language
from orbi.github import WRITE_PERMISSIONS, issue_comments, repository_permission
from orbi.journal import JOURNAL_EVENTS, event, run_command


# Every Runner-authored comment carries one of these hidden markers: the run
# markers `<!-- orbi:run=… -->`, the failure blocks `<!-- orbi:failure:v1 … -->`
# and the command receipts `<!-- orbi-<command>-command-rejected|failed … -->`
# (which carry no `orbi:run=`). The marker — never the author login —
# identifies Runner output, so a self-hosted install where the Runner and the
# maintainer share one `gh` account can still send a `/milestone` command
# (Issue #1460).
RUNNER_COMMENT_MARKER = "<!-- orbi"


def comment_author_login(comment: object) -> str | None:
    """The login of a comment's author, or None when it is unreadable."""
    if not isinstance(comment, dict):
        return None
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) and login else None


def is_runner_comment(comment: object) -> bool:
    """True when a comment carries a hidden ``<!-- orbi`` Runner marker."""
    if not isinstance(comment, dict):
        return False
    body = comment.get("body")
    return isinstance(body, str) and RUNNER_COMMENT_MARKER in body


# The permission levels a CommandSpec may declare, level -> the repository
# permissions the dispatcher accepts (Issue #1806: the permission GitHub
# reports for the author, never the comment's `authorAssociation`).
# Declarative: the handler never checks a caller itself, so the help text
# and the gate cannot drift.
PERMISSION_LEVELS: dict[str, frozenset[str]] = {
    "write": WRITE_PERMISSIONS,
}


@dataclass(frozen=True)
class CommandSpec:
    """One in-ticket command: its spelling, its permission and its callbacks.

    ``name`` is the stable MACHINE id: the registry key, the receipt marker
    (``orbi-<name>-command``) and the journal event names
    (``<name>_command_applied|failed``). ``subcommand`` is the canonical
    word a user types after ``/orbi`` (Issue #1922); ``aliases`` are legacy
    top-level spellings still accepted. ``arity`` is the number of
    arguments the command takes (0 or 1).

    ``validate(argument, **context)`` is the command's own business rule: it
    returns a readable rejection reason, or ``None`` when the argument is
    accepted. ``apply(argument, **context)`` performs the command and raises
    `TicketCommandError` naming the failed step. ``usage`` /
    ``description`` are the help reference the docs binding test pins.
    """

    name: str
    subcommand: str
    arity: int
    permission: str
    validate: Callable[..., str | None]
    apply: Callable[..., None]
    usage: str
    description: str
    # The argument's human label is a message-catalog key (Issue #1704),
    # so the receipt renders `version` / `版本` in the ticket's language.
    argument_label: str = "command.argument_label.argument"
    # Top-level spellings accepted before the `/orbi` prefix existed
    # (Issue #1922): ``/milestone`` keeps working, receipts show the
    # canonical form.
    aliases: tuple[str, ...] = ()

    @property
    def canonical(self) -> str:
        """The canonical spelling every receipt and help text shows."""
        return f"/orbi {self.subcommand}"

    def command_text(self, argument: str | None = None) -> str:
        """The canonical command line, with the argument when it has one."""
        if argument and self.arity:
            return f"{self.canonical} {argument}"
        return self.canonical


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
# register_command; the duplicate-name / duplicate-spelling / unknown-
# permission checks make the registration itself the validation, so a typo
# fails at import time instead of at the first ticket that carries it.
TICKET_COMMANDS: dict[str, CommandSpec] = {}


def register_command(spec: CommandSpec) -> None:
    """Register one command; fail fast on a contract violation.

    Raises `TicketCommandRegistryError` for an empty name/subcommand, an
    arity other than 0 or 1, a duplicate name, a subcommand or legacy alias
    already owned by another command, or a permission level not declared in
    `PERMISSION_LEVELS`. Registration also registers the command's journal
    events, so a new command needs nothing else (Issue #1922).
    """
    if not spec.name or not spec.subcommand:
        raise TicketCommandRegistryError(
            f"a ticket command needs a name and a subcommand: {spec.name!r}"
        )
    if spec.arity not in (0, 1):
        raise TicketCommandRegistryError(
            f"ticket command {spec.name!r} declares arity {spec.arity!r}; "
            "only 0 or 1 arguments are supported"
        )
    if spec.name in TICKET_COMMANDS:
        raise TicketCommandRegistryError(
            f"duplicate ticket command name: {spec.name!r}"
        )
    for existing in TICKET_COMMANDS.values():
        if spec.subcommand == existing.subcommand:
            raise TicketCommandRegistryError(
                f"ticket subcommand {spec.subcommand!r} on {spec.name!r} is "
                f"already registered by {existing.name!r}"
            )
        duplicate_aliases = set(spec.aliases) & set(existing.aliases)
        if duplicate_aliases:
            raise TicketCommandRegistryError(
                f"ticket command alias {sorted(duplicate_aliases)[0]!r} on "
                f"{spec.name!r} is already registered by {existing.name!r}"
            )
    if spec.permission not in PERMISSION_LEVELS:
        raise TicketCommandRegistryError(
            f"unknown permission level {spec.permission!r} for "
            f"ticket command {spec.name!r}"
        )
    TICKET_COMMANDS[spec.name] = spec
    # The command's journal events are the registry's own business: a
    # registrant that had to add them to JOURNAL_EVENTS by hand could ship
    # a command whose first run raises inside ``event()`` (Issue #1922).
    JOURNAL_EVENTS[f"{spec.name}_command_applied"] = (
        f"an in-ticket `{spec.canonical}` command ran"
    )
    JOURNAL_EVENTS[f"{spec.name}_command_failed"] = (
        f"an in-ticket `{spec.canonical}` command failed at one step"
    )


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
    """The anchored matcher the shared layer compiles from a spec.

    The canonical form is ``/orbi <subcommand>``; every declared legacy
    alias keeps its old top-level spelling (Issue #1922). A command author
    supplies spellings, never a pattern, so the anchor and the whitespace
    shape are identical for every command.
    """
    # The spelling must end at whitespace or the line end: `/orbi
    # milestone-extra` and `/milestones` are other words, not a command
    # with an argument (Issue #1922).
    forms = [rf"/orbi[ \t]+{re.escape(spec.subcommand)}(?=[ \t]|$)"]
    forms.extend(
        rf"/{re.escape(alias)}(?=[ \t]|$)" for alias in spec.aliases
    )
    return re.compile(rf"(?mi)^(?:{'|'.join(forms)})[ \t]*([^\r\n]*)$")


def parse_commands(text: object, spec: CommandSpec) -> list[str | None]:
    """Every command ``spec`` issues in a comment body, in line order.

    A command line whose argument count is not the spec's ``arity`` yields
    `None`: it is readable as a command but malformed, and the caller owes
    its author one readable receipt instead of silence. An arity-0 command
    yields the empty argument for a well-formed line.
    """
    if not isinstance(text, str) or not text:
        return []
    parsed: list[str | None] = []
    for argument in _command_pattern(spec).findall(strip_fenced_code_blocks(text)):
        fields = argument.split()
        if len(fields) != spec.arity:
            parsed.append(None)
        else:
            parsed.append(fields[0] if fields else "")
    return parsed


def _language_code(context: dict[str, object]) -> str:
    """The message language carried through the dispatcher's `**context`."""
    value = context.get("language_code")
    return value if isinstance(value, str) else language.DEFAULT_LANGUAGE


def _argument_label(spec: CommandSpec, language_code: str) -> str:
    """The argument's human label in one language (a catalog entry)."""
    return language.text(language_code, spec.argument_label)


def _author_permission(
    comment: dict, *, repo: str, cache: dict[str, str],
) -> str:
    """The repository permission of the comment's author (Issue #1806).

    The one GitHub read `select_command` makes: ``cache`` keeps it to one
    read per author for the whole ticket. An unreadable author has no
    permission ("none"), while a failed read is a GitHub read failure and
    propagates — never a "no permission" receipt.
    """
    login = comment_author_login(comment)
    if login is None:
        return "none"
    if login not in cache:
        cache[login] = repository_permission(login, repo=repo)
    return cache[login]


def _permission_reason(
    permission: str, spec: CommandSpec, language_code: str,
) -> str:
    return language.text(
        language_code, "command.reason_no_permission",
        permission=spec.permission, observed=permission,
    )


def _malformed_reason(spec: CommandSpec, language_code: str) -> str:
    # An arity-0 command has no argument slot, so the "exactly one
    # {label}" wording would contradict the usage line it prints beside
    # (Issue #1922: `/orbi fix-base now`).
    if spec.arity == 0:
        return language.text(
            language_code, "command.reason_malformed_no_argument",
            usage=spec.usage,
        )
    return language.text(
        language_code, "command.reason_malformed",
        label=_argument_label(spec, language_code), usage=spec.usage,
    )


def select_command(
    comments: list[dict], spec: CommandSpec, *, repo: str, **context: object,
) -> tuple[tuple[dict, str] | None, list[tuple[dict, str | None, str]]]:
    """Resolve the ticket's occurrences of ``spec`` to one executable command.

    Returns `(target, rejections)`. `target` is the LAST occurrence when
    it is authorized — its author's REPOSITORY PERMISSION allows the
    command's level (Issue #1806; the comment's ``authorAssociation`` is
    never read) — well-formed and accepted by the command's ``validate``
    callback: the last word wins, so an operator can correct a typo by
    commenting again. Every rejected occurrence is returned with a
    readable reason so the caller can leave one receipt each. A comment
    carrying a hidden ``<!-- orbi`` Runner marker is skipped silently:
    the bot must never trigger itself off its own output, and a
    self-hosted maintainer whose own account is the Runner is evaluated
    like anyone else (Issue #1460).
    """
    language_code = _language_code(context)
    occurrences: list[tuple[dict, str | None]] = []
    for comment in comments:
        if not isinstance(comment, dict) or is_runner_comment(comment):
            continue
        for argument in parse_commands(comment.get("body"), spec):
            occurrences.append((comment, argument))
    rejections: list[tuple[dict, str | None, str]] = []
    target: tuple[dict, str] | None = None
    permissions: dict[str, str] = {}
    for comment, argument in occurrences:
        permission = _author_permission(comment, repo=repo, cache=permissions)
        if permission not in PERMISSION_LEVELS[spec.permission]:
            target = None
            rejections.append((
                comment, argument,
                _permission_reason(permission, spec, language_code),
            ))
            continue
        if argument is None:
            target = None
            rejections.append((
                comment, argument, _malformed_reason(spec, language_code),
            ))
            continue
        reason = spec.validate(argument, **context)
        if reason is not None:
            target = None
            rejections.append((comment, argument, reason))
            continue
        target = (comment, argument)
    return target, rejections


def command_comment_key(comment: dict) -> str:
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


def command_receipt_marker(spec: CommandSpec) -> str:
    """The hidden marker prefix derived from the command's name.

    Public because a command's own success reply is its idempotency
    record: carrying this marker lets the next scan recognize "this
    comment already ran" (Issue #1825's `/orbi fix-base`).
    """
    return f"orbi-{spec.name}-command"


def _post_command_receipt(
    repo: str, issue_number: int, existing_comments: list[dict], comment: dict,
    *, spec: CommandSpec, marker: str, summary: str, argument: str | None,
    reason: str, step: str | None = None,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> None:
    """Post one reason comment, at most once per occurrence (idempotent)."""
    hidden = f"<!-- {marker} comment={command_comment_key(comment)} -->"
    if any(
        isinstance(existing, dict) and isinstance(existing.get("body"), str)
        and hidden in existing["body"]
        for existing in existing_comments
    ):
        return
    lines = [
        summary, "",
        language.text(language_code, "command.reason", reason=reason),
    ]
    # The command line, never the machine id: a receipt shows the canonical
    # ``/orbi <subcommand>`` form (Issue #1922). An arity-0 command has no
    # argument to name, so its parsed empty argument shows the line too.
    lines.append(
        language.text(
            language_code, "command.command_line", command=spec.canonical,
        )
        if not argument else language.text(
            language_code, "command.argument",
            label=_argument_label(spec, language_code), argument=argument,
        )
    )
    if step is not None:
        lines.append(language.text(language_code, "command.failed_step", step=step))
    lines.extend([
        language.text(
            language_code, "command.comment",
            ref=_command_comment_ref(comment),
        ),
        "",
        hidden,
    ])
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
    language_code = _language_code(context)
    comments = issue_comments(issue_number, repo=repo)
    for spec in commands:
        target, rejections = select_command(
            comments, spec, repo=repo, **context,
        )
        for comment, argument, reason in rejections:
            _post_command_receipt(
                repo, issue_number, comments, comment, spec=spec,
                marker=f"{command_receipt_marker(spec)}-rejected",
                summary=language.text(
                    language_code, "command.not_applied",
                    command=spec.canonical,
                ),
                argument=argument, reason=reason, language_code=language_code,
            )
        if target is None:
            continue
        comment, argument = target
        try:
            # The winning occurrence travels with the command: a handler
            # that must run at most once per comment keys its own receipt
            # on it (Issue #1825).
            spec.apply(
                argument, repo=repo, issue_number=issue_number,
                command_comment=comment, comments=comments, **context,
            )
        except TicketCommandError as exc:
            _post_command_receipt(
                repo, issue_number, comments, comment, spec=spec,
                marker=(
                    f"{command_receipt_marker(spec)}-failed step={exc.step}"
                ),
                summary=language.text(
                    language_code, "command.failed",
                    command=spec.command_text(argument),
                ),
                argument=argument, reason=exc.reason, step=exc.step,
                language_code=language_code,
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
