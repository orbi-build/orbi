#!/usr/bin/env python3
"""`orbi draft`'s command boundary (Issue #2041).

The drafting engine lives in `orbi.draft`; this module owns the command a
caller drives — one drafting round (`--session-dir`), creation
(`--create`) and the interactive entry it dispatches to
(`orbi.draft_interactive`) — plus the stdout/stderr documents they read and
write and the exit codes. Split out of `orbi.draft` so the engine stays one
readable unit.

Failure output is the contract a program caller depends on: one JSON object
`{"code", "step", "detail"}` on stderr and exit code 1
(docs/public-contracts.mdx).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from orbi import attachments, draft, journal
from orbi.config import RunnerConfig
from orbi.draft_interactive import interactive
from orbi.journal import event
from orbi.ticket_command import TicketCommandError

# --- the command ---------------------------------------------------------

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
    attach: Sequence[str] = (), stdin=None, stdout=None, stderr=None,
) -> int:
    """Run one orbi draft invocation; return the process exit code.

    `attach` is the repeatable `--attach <path>` list (Issue #2364).
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    run_id = journal.new_run_id()
    journal.set_run_id(run_id)
    non_interactive = create or session_dir is not None
    try:
        payload = (
            attachments.with_paths(draft.read_payload(_read_stdin(stdin)), attach)
            if create or session_dir is not None else {}
        )
        if create:
            created = draft.create_issues(config, repo, payload)
            print(json.dumps({"created": created.numbers}), file=stdout)
            if created.unuploaded:
                print(
                    attachments.upload_notice(created.unuploaded),
                    file=stderr,
                )
            return 0
        if session_dir is not None:
            round_ = draft.run_round(
                config, repo, session_dir=Path(session_dir), payload=payload,
                run_id=run_id,
            )
            print(
                json.dumps(round_.as_json(), ensure_ascii=False),
                file=stdout,
            )
            return 0
        return interactive(
            config, repo, request=request, stdin=stdin, stdout=stdout,
            stderr=stderr, run_id=run_id, attach=attach,
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
        "--attach", action="append", default=[], metavar="PATH",
        help="attach an image, HTML or text file to the draft (repeatable)",
    )
    parser.add_argument(
        "--create", action="store_true",
        help="create the confirmed draft from stdin "
             '{"key","issues","header","issue"} and print the created '
             "numbers",
    )
