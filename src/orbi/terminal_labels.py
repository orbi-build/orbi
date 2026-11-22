"""The ordered writes of a terminal delivery label transition (Issue #2357).

A person reading an Issue timeline must see the transition END on the new
state: every old state label is removed BEFORE the terminal label is added,
so "unlabeled ai-pr-opened" is never the last event after "labeled
ai-merged". GitHub does not guarantee the order inside one
`gh issue edit --add-label/--remove-label` call, which is why the terminal
patch cannot keep the shared one-call application.

The labels actually on the Issue are read here: terminal callers pass
synthesized current-label sets (the release close-out's queue entry is
usually not on the Issue anymore), and a label that is not on the Issue must
never be written back.

A failed write puts back the labels this sequence already removed and then
re-raises, leaving each recovery scan the same state a failed single write
leaves today (#2292, #1895). If putting the labels back fails too, the Issue
is left without a state label; that double failure is accepted.
"""
from __future__ import annotations

from collections.abc import Callable

from orbi.journal import LOGGER


def apply_terminal_patch(number: int, *, repo: str, to_add: list[str],
                         to_remove: list[str],
                         edit_issue: Callable[..., None],
                         read_labels: Callable[..., list[str]]) -> None:
    """Remove first, add last; restore the removed labels on any failure.

    The gh writes and the label read arrive as callables from
    `orbi.github` (the adapter's one seam), so this module imports no
    runtime dependency back into it.
    """
    present = set(read_labels(number, repo=repo))
    removed: list[str] = []
    try:
        for label in to_remove:
            if label not in present:
                continue
            edit_issue(number, repo=repo, remove=label)
            removed.append(label)
        if to_add:
            edit_issue(number, repo=repo, add=to_add[0])
    except Exception:
        for label in removed:
            try:
                edit_issue(number, repo=repo, add=label)
            except Exception:
                LOGGER.exception(
                    "issue=%s label_restore_failed label=%s", number, label,
                )
                break
        raise
