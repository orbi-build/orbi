#!/usr/bin/env python3
"""The one validation entry the drafting tools run (Issue #2303).

The draft tools are TypeScript and validate their arguments against a TypeBox
schema, which cannot express a cross-field rule: `after` must name an EARLIER
Issue, a question carries at most one recommended option. Those rules live in
`orbi.draft_document.parse_answer`, and before this entry they ran only after
the whole session had ended — so one bad value discarded a finished draft with
no chance for the model to correct it (production: a one-Issue draft whose
Issue 1 carried `after: 1`).

The tools therefore run THIS module over the answer they are about to record,
with the answer document on stdin: exit 0 means the engine will accept it,
non-zero prints the very reason `parse_answer` gives, which the model reads as
an error tool result and corrects in the same session. The same function, not a
copy of its rules in TypeScript.

Internal, not the CLI: the engine passes `<its own interpreter> -m
orbi.draft_validate` to the session in `ORBI_DRAFT_VALIDATE`; the module is
deliberately not an `orbi` subcommand and carries no public contract of its
own.
"""
from __future__ import annotations

import json
import sys

from orbi.draft_document import DraftError, parse_answer


def main() -> int:
    """Validate one answer document: 0 valid, 1 rejected with its reason."""
    text = sys.stdin.read()
    try:
        data = json.loads(text)
    except ValueError as exc:
        print(f"stdin is not JSON: {exc}")
        return 1
    try:
        parse_answer(data)
    except DraftError as exc:
        print(exc.detail)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
