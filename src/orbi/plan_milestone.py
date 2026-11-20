"""The run's `plan ready` milestone, posted at most once per run (Issue #2293).

A resumed round of the same run re-reaches the plan milestone with the
same `.orbi/plan.md` in its worktree, and the run already announced it:
kit#128 (run `f75b7c1d`) collected 294 identical plan-ready comments from
one run. The comment therefore carries a hidden
`<!-- orbi:milestone=plan-ready -->` marker on the line right under the
run marker, and the writer scans the Issue's comments for that anchored
prefix before posting — the same once-per-run read the started scene
uses, so a restarted runner instance finds the earlier comment too.

Only THIS milestone is once per run: every failure milestone reports a
different outcome, so those keep posting per outcome.
"""
from __future__ import annotations

from pathlib import Path

from orbi import language
from orbi.progress import ProgressPublisher, run_marker

# The hidden marker that makes the plan-ready milestone once per run.
PLAN_READY_MARKER = "<!-- orbi:milestone=plan-ready -->"


def marker_present(comments: list[dict], *, run_id: str) -> bool:
    """True when one of the comments is this run's plan-ready milestone.

    The marker is read only from the official prefix — the run marker on
    the first line and the milestone marker on the next — so a
    marker-shaped string quoted deeper in a body never counts.
    """
    prefix = f"{run_marker(run_id)}\n{PLAN_READY_MARKER}"
    return any(
        isinstance(comment, dict)
        and isinstance(comment.get("body"), str)
        and comment["body"].startswith(prefix)
        for comment in comments
    )


def publish(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the plan-ready milestone once the worktree holds the plan.

    A missing plan artifact means the session never wrote one (a takeover
    or a pure-ops delivery): nothing to announce.
    """
    if not (worktree / ".orbi" / "plan.md").is_file():
        return
    if marker_present(publisher.list_comments(), run_id=publisher.run_id):
        return
    publisher.milestone(
        language.text(publisher.language_code, "milestone.plan_ready"),
        block=PLAN_READY_MARKER,
    )
