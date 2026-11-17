"""The `/orbi <subcommand>` line grammar of the in-ticket command layer.

Split out of `orbi.ticket_command` (Issue #2221) so the layer could take the
record command's occurrence (`CommandSpec.occurrence`) and stay inside its
recorded size ceiling (`tools/size_ratchet.py`). What lives here is exactly
the spelling side of a command — the anchored matcher the layer compiles from
a spec, the fenced-code filter, and the per-comment parse — never a decision:
`ticket_command` still owns which command wins and what it does.

A command author supplies `subcommand` (`/orbi <subcommand>`) plus optional
legacy `aliases` (`/milestone`), never a regex, so the anchor and the
whitespace shape are identical for every command: a quoted `> /orbi fix-base`,
a fenced example or the word `/milestones` is never a command.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: the spec type lives in the layer that imports this
    # module for its own parse, so a runtime import would be cyclic.
    from orbi.ticket_command import CommandSpec


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


def _command_pattern(spec: "CommandSpec") -> re.Pattern[str]:
    """The anchored matcher the shared layer compiles from a spec."""
    # The spelling must end at whitespace or the line end: `/orbi
    # milestone-extra` and `/milestones` are other words, not a command
    # with an argument (Issue #1922).
    forms = [rf"/orbi[ \t]+{re.escape(spec.subcommand)}(?=[ \t]|$)"]
    forms.extend(
        rf"/{re.escape(alias)}(?=[ \t]|$)" for alias in spec.aliases
    )
    return re.compile(rf"(?mi)^(?:{'|'.join(forms)})[ \t]*([^\r\n]*)$")


def parse_commands(text: object, spec: "CommandSpec") -> list[str | None]:
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
