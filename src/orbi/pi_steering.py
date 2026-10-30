#!/usr/bin/env python3
"""The steering restart's task-context hand-off (Issue #1997).

A trusted Issue correction discovered while a session is running restarts
the SAME Pi session with the corrected task context (Issue #1094 — a new
comment, or an edited body whose full new text is appended). The context
travels as a FILE: `orbi.pi_command.build_pi_command` ends every argv with
Pi's own file argument `@<session_dir>/task-context.md`, because Linux caps
ONE argv string at 131072 bytes (MAX_ARG_STRLEN) and a ticket body of about
50,000 CJK characters is ~150,000 UTF-8 bytes — the local-mode gate's
regression case, where Pi never started (`OSError: [Errno 7] Argument list
too long: 'pi'`) and the run was reported as an unclassifiable start
failure.

So the restart rewrites that FILE, never argv: a corrected context that
embeds the whole edited body again stays a file write instead of rebuilding
the oversized command line the file transport was introduced to escape.

A leaf module: it imports nothing from `orbi` and is called from the one
place that restarts a session, `orbi.pi_process.stream_pi` (which extracts
it to stay under its module size ceiling — Issue #1229).
"""
from __future__ import annotations

from pathlib import Path


def write_steered_context(command: list[str], context: str) -> None:
    """Replace the task context a Pi restart will receive (Issue #1997).

    `command` is the argv `build_pi_command` returned: its `@<path>`
    element names the context file, and this writes the corrected
    `context` there. Nothing else changes, so the restarted session runs
    the exact command shape the first one did — the context never becomes
    one argv string again.

    A command with no `@<path>` element cannot be steered this way: fail
    fast with the offending argv tail instead of silently appending the
    context to the command line.
    """
    for argument in command:
        if argument.startswith("@"):
            Path(argument[1:]).write_text(context, encoding="utf-8")
            return
    raise ValueError(
        "the Pi command carries no @<context file> argument, so a "
        f"steering restart cannot replace the task context: {command[-1:]!r}"
    )
