#!/usr/bin/env python3
"""The interactive mode of orbi draft (Issues #2041, #2364).

Questions, the draft, typed edits and the confirmation in ONE process, with
nothing left behind: the drafting session lives in a temp directory that is
removed when the command ends, created or not. It lives here rather than in
orbi.draft_cli — which owns the stdin/stdout documents of the other two
modes and the exit codes — because the mode is self-contained and that
module's recorded size may only shrink (tools/size_ratchet.py, Issue #2167).
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Sequence

from orbi import attachments, draft
from orbi.config import RunnerConfig
from orbi.draft_document import render_round


def ask(stdin, stdout, prompt: str) -> str | None:
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


def declined(stdout) -> int:
    """The person's own decision not to create anything."""
    print("Nothing was created.", file=stdout)
    return 0


def interactive(
    config: RunnerConfig, repo: str, *, request: str | None, stdin, stdout,
    stderr, run_id: str, attach: Sequence[str] = (),
) -> int:
    """Questions, draft, typed edits and the confirmation in one process.

    Nothing is left behind: the drafting session lives in a temp directory
    that is removed when the command ends, created or not.
    """
    message = (request or "").strip()
    if not message:
        asked = ask(stdin, stdout, "What should Orbi build? ")
        if asked is None or not asked.strip():
            raise draft.DraftError(
                "no_request", "read the request", "no request was given",
            )
        message = asked.strip()
    conversation: list[dict] = []
    current: dict | None = None
    entries = attachments.from_paths(attach)
    attached = attachments.parse(entries)
    session_root = Path(tempfile.mkdtemp(prefix="orbi-draft-"))
    try:
        for _round in range(draft.MAX_ROUNDS):
            payload = {
                "message": message,
                "conversation": list(conversation),
                "draft": current,
                "attachments": entries,
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
                        f"Q{position} — {question.text}", file=stdout,
                    )
                    for option in question.options:
                        print(
                            f"  - {option.label}: {option.description}",
                            file=stdout,
                        )
                answer = ask(stdin, stdout, "Your answers: ")
                if answer is None:
                    return declined(stdout)
                conversation.append({"role": "person", "text": message})
                conversation.append({"role": "orbi", "text": round_.reply})
                # A bare Enter accepts no default answer (Issue #2133): an
                # empty line answers nothing, so nothing is drafted from it.
                if not answer.strip():
                    return declined(stdout)
                message = answer.strip()
                continue
            if not round_.issues:
                # Issue #2201: the round answered with a reply alone (a
                # request not to change any code). The reply is the whole
                # answer, so there is nothing to show or to create.
                return 0
            print(render_round(round_, attached=attached), file=stdout)
            current = {"issues": [i.as_json() for i in round_.issues]}
            answer = ask(
                stdin, stdout,
                "Press Enter to create these Issues, type an edit, or 'n' "
                "to cancel: ",
            )
            if answer is None:
                return declined(stdout)
            typed = answer.strip()
            if typed.lower() in draft.DECLINE:
                return declined(stdout)
            if typed.lower() in draft.CONFIRM:
                created = draft.create_issues(config, repo, {
                    "key": f"draft-{run_id}",
                    "issues": [issue.as_json() for issue in round_.issues],
                    "attachments": entries,
                })
                for number in created.numbers:
                    print(f"#{number}", file=stdout)
                if created.unuploaded:
                    print(
                        attachments.upload_notice(created.unuploaded),
                        file=stderr,
                    )
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


