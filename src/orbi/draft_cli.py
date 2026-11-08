#!/usr/bin/env python3
"""`orbi draft`'s command boundary (Issue #2041).

The drafting engine lives in `orbi.draft`; this module owns the three
modes a caller drives — interactive, one drafting round (`--session-dir`)
and creation (`--create`) — plus the stdout/stderr documents they read and
write and the exit codes. Split out of `orbi.draft` so the engine stays one
readable unit.

Failure output is the contract a program caller depends on: one JSON object
`{"code", "step", "detail"}` on stderr and exit code 1
(docs/public-contracts.mdx).
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from orbi import draft, journal
from orbi.config import RunnerConfig
from orbi.journal import event
from orbi.ticket_command import TicketCommandError

# --- the command ---------------------------------------------------------

def _ask(stdin, stdout, prompt: str) -> str | None:
    """One terminal line; None at EOF, so a scripted or closed stdin never
    loops and never creates anything by itself."""
    stdout.write(prompt)
    stdout.flush()
    line = stdin.readline()
    if line == "":
        return None
    is_tty = getattr(stdin, "isatty", None)
    if not (callable(is_tty) and is_tty()):
        # A terminal echoes the typed line itself; a scripted stdin does
        # not, so the next line of output would otherwise run into the
        # prompt.
        stdout.write("\n")
    return line.rstrip("\n")


def _render_draft(round_: draft.DraftRound) -> str:
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


def _declined(stdout) -> int:
    print("Nothing was created.", file=stdout)
    return 0


def _interactive(
    config: RunnerConfig, repo: str, *, request: str | None, stdin, stdout,
    run_id: str,
) -> int:
    """Questions, draft, typed edits and the confirmation in one process.

    Nothing is left behind: the drafting session lives in a temp directory
    that is removed when the command ends, created or not.
    """
    message = (request or "").strip()
    if not message:
        asked = _ask(stdin, stdout, "What should Orbi build? ")
        if asked is None or not asked.strip():
            raise draft.DraftError(
                "no_request", "read the request", "no request was given",
            )
        message = asked.strip()
    conversation: list[dict] = []
    current: dict | None = None
    session_root = Path(tempfile.mkdtemp(prefix="orbi-draft-"))
    try:
        for _round in range(draft.MAX_ROUNDS):
            payload = {
                "message": message,
                "conversation": list(conversation),
                "draft": current,
            }
            round_ = draft.run_round(
                config, repo, session_dir=session_root, payload=payload,
                run_id=run_id,
            )
            if round_.reply.strip():
                print(round_.reply, file=stdout)
            if round_.questions:
                for position, question in enumerate(
                    round_.questions, start=1,
                ):
                    print(
                        f"Q{position} — {question.question}", file=stdout,
                    )
                    for option in question.options:
                        print(
                            f"  - {option.label}: {option.description}",
                            file=stdout,
                        )
                answer = _ask(stdin, stdout, "Your answers: ")
                if answer is None:
                    return _declined(stdout)
                conversation.append({"role": "person", "text": message})
                conversation.append({"role": "orbi", "text": round_.reply})
                # A bare Enter accepts no default answer (Issue #2133): an
                # empty line answers nothing, so nothing is drafted from it.
                if not answer.strip():
                    return _declined(stdout)
                message = answer.strip()
                continue
            print(_render_draft(round_), file=stdout)
            current = {"issues": [i.as_json() for i in round_.issues]}
            answer = _ask(
                stdin, stdout,
                "Press Enter to create these Issues, type an edit, or 'n' "
                "to cancel: ",
            )
            if answer is None:
                return _declined(stdout)
            typed = answer.strip()
            if typed.lower() in draft.DECLINE:
                return _declined(stdout)
            if typed.lower() in draft.CONFIRM:
                created = draft.create_issues(config, repo, {
                    "key": f"draft-{run_id}",
                    "issues": [issue.as_json() for issue in round_.issues],
                })
                for number in created:
                    print(f"#{number}", file=stdout)
                return 0
            conversation.append({"role": "person", "text": message})
            conversation.append({"role": "orbi", "text": round_.reply})
            message = typed
        raise draft.DraftError(
            "too_many_rounds", "run the drafting round",
            f"the draft still needs changes after {draft.MAX_ROUNDS} rounds",
        )
    finally:
        shutil.rmtree(session_root, ignore_errors=True)


def _fail(exc: draft.DraftError, *, non_interactive: bool, stderr) -> int:
    """Report one failed step: the JSON triple for a caller, prose for a
    person, and a non-zero exit either way."""
    event(
        "draft_failed", level=logging.ERROR, code=exc.code, step=exc.step,
        detail=exc.detail,
    )
    if non_interactive:
        print(json.dumps(
            {"code": exc.code, "step": exc.step, "detail": exc.detail},
            ensure_ascii=False,
        ), file=stderr)
    else:
        print(
            f"orbi draft failed ({exc.code}, step={exc.step}): {exc.detail}",
            file=stderr,
        )
    return 1


def _read_stdin(stdin) -> str:
    data = stdin.read()
    if not data.strip():
        raise draft.DraftError("invalid_input", "read stdin", "stdin is empty")
    return data


def run_draft(
    config: RunnerConfig, repo: str, *, request: str | None = None,
    session_dir: Path | None = None, create: bool = False,
    stdin=None, stdout=None, stderr=None,
) -> int:
    """Run one orbi draft invocation; return the process exit code."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    run_id = journal.new_run_id()
    journal.set_run_id(run_id)
    non_interactive = create or session_dir is not None
    try:
        if create:
            created = draft.create_issues(
                config, repo, draft.read_payload(_read_stdin(stdin)),
            )
            print(json.dumps({"created": created}), file=stdout)
            return 0
        if session_dir is not None:
            round_ = draft.run_round(
                config, repo, session_dir=Path(session_dir),
                payload=draft.read_payload(_read_stdin(stdin)), run_id=run_id,
            )
            print(
                json.dumps(round_.as_json(), ensure_ascii=False),
                file=stdout,
            )
            return 0
        return _interactive(
            config, repo, request=request, stdin=stdin, stdout=stdout,
            run_id=run_id,
        )
    except draft.DraftError as exc:
        return _fail(exc, non_interactive=non_interactive, stderr=stderr)
    except TicketCommandError as exc:
        return _fail(
            draft.DraftError("create_failed", exc.step, exc.reason),
            non_interactive=non_interactive, stderr=stderr,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return _fail(
            draft.DraftError(
                "draft_failed", "run the command",
                f"{type(exc).__name__}: {exc}",
            ),
            non_interactive=non_interactive, stderr=stderr,
        )


def register_cli(subparsers, common) -> None:
    """Register orbi draft and its three modes (Issue #2041)."""
    parser = subparsers.add_parser(
        "draft", parents=[common],
        help="turn one request into deliverable Issues (interactive; "
             "--session-dir runs one drafting round, --create creates the "
             "confirmed draft)",
    )
    parser.add_argument(
        "request", nargs="?", default=None,
        help="the request, in one sentence (interactive mode asks when "
             "it is omitted)",
    )
    parser.add_argument(
        "--repo", default=None,
        help="source repo (default: first configured source)",
    )
    parser.add_argument(
        "--session-dir", type=Path, default=None,
        help="run ONE drafting round: stdin "
             '{"message","conversation","draft"}, stdout '
             '{"reply","questions","issues"}',
    )
    parser.add_argument(
        "--create", action="store_true",
        help="create the confirmed draft from stdin "
             '{"key","issues","header","issue"} and print the created '
             "numbers",
    )
