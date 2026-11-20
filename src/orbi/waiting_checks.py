"""Say on its Issue that a deferred delivery waits for checks (Issue #2304).

A delivery can pass review and still not merge in that tick: the merge
gate merges no head whose checks it has not read. Two deferrals are
exactly that wait — the review session pushed a head (whose CI has not
even registered yet) and the gate's own base absorb pushed one — and the
next tick's scan defers again while the PR's rollup is still running. On
the production single-slot runner the next tick can be an hour away while
a long implementation holds the slot, and until this module the person
watched an Issue stuck at the review step with nothing telling them what
Orbi was waiting for (2026-11-20: two green PRs, over an hour of silence,
a paying user asking what Orbi waited for).

The waiting line is ONE line of the run's EXISTING progress comment: no
new comment kind, no new label, no state store. It names the head commit
whose checks Orbi waits for and says the merge follows automatically once
they pass — never a time, never a next tick, because neither is true. A
later render of the progress body (the next round, a gate failure, the
blocked scene) drops the line, so checks that then fail end on the
existing failure text.

Publication is the usual pure bypass (Issue #79): a comment write here
never decides whether the delivery, the review or the merge proceeds.
"""
from __future__ import annotations

from orbi.checks import render_check
from orbi.github import pr_number, pr_view
from orbi.journal import event, run_command
from orbi.language import text as render_text
from orbi.pi_command import ROLE_REVIEW
from orbi.progress import ProgressPublisher, safe_publish

# The line's hidden identity: it rides the same physical line as the
# sentence, so a repeat publication replaces exactly one line instead of
# stacking a second one (the same marker trick as every other Orbi
# record, Issue #1657).
WAITING_MARKER = "<!-- orbi:waiting-checks -->"
# The progress body's details fold: the status rows end above it, so the
# waiting line sits at the end of what a reader sees collapsed.
FOLD_OPEN = "<details><summary>"
# A head SHA is shown as the 12-character prefix the rest of the engine
# uses for a commit it names in prose.
HEAD_DISPLAY = 12


def line(language_code: str, head: str) -> str:
    """The one visible line: which commit's checks Orbi waits for.

    It states the merge follows automatically once they pass and nothing
    else: no time estimate (a long implementation holds the single slot)
    and no next-tick promise (the ticket that adds a merge-only tick is
    separate, Issue #2304).
    """
    return f"{WAITING_MARKER} " + render_text(
        language_code, "progress.waiting_checks",
        head=head[:HEAD_DISPLAY],
    )


def with_line(body: str, waiting: str) -> str:
    """Return the progress body with the waiting line set below its rows.

    The line is an edit of the record as it stands: the run marker and the
    versioned block stay the body's prefix (docs/public-contracts.mdx), the
    fields, fold and runner marker are untouched, and a line this run
    published before is REPLACED rather than duplicated, so each tick can
    republish the same line without growing the comment.
    """
    rows = body.splitlines()
    previous = next(
        (index for index, row in enumerate(rows) if WAITING_MARKER in row),
        None,
    )
    if previous is not None:
        # Drop the line this run published before AND the blank row that
        # separated it from the status rows, so republishing reproduces
        # exactly one layout instead of leaving a growing gap.
        del rows[previous]
        if previous and rows[previous - 1] == "":
            del rows[previous - 1]
    fold = next(
        (index for index, row in enumerate(rows) if FOLD_OPEN in row),
        None,
    )
    if fold is None:
        return "\n".join([*rows, "", waiting])
    return "\n".join([*rows[:fold], waiting, "", *rows[fold:]])


def pr_head(pr_url: str, source_repo: str) -> str:
    """The PR's head commit — the one whose checks the wait is about.

    One documented read of `headRefOid` (the field the merge gate freezes);
    a response without it fails fast, so a guessed field can never render a
    waiting line that names nothing.
    """
    data = pr_view(pr_number(pr_url), "headRefOid", repo=source_repo)
    head = data.get("headRefOid")
    if not isinstance(head, str) or not head:
        raise ValueError(f"PR {pr_url} reports no head commit")
    return head


def publish(*, issue: int, source_repo: str, run_id: str,
            language_code: str, head: str,
            status_url: str | None = None) -> None:
    """Set the waiting line on the run's existing progress comment.

    The line updates the record the run already publishes; it never
    creates one (only `ensure` creates), so a run without a progress
    comment publishes nothing rather than inventing a second comment kind.
    """
    ProgressPublisher(
        issue, source_repo, run_id, run_command=run_command,
        language_code=language_code, status_url=status_url,
    ).patch_existing(lambda body: with_line(body, line(language_code, head)))


def defer_pending(pr_url: str, pending: list, issue: dict, repo: str,
                  run_id: str | None, language_code: str,
                  status_url: str | None = None) -> None:
    """Journal the pending rollup and say on the Issue what it waits for.

    The one deferral both CI-pending paths share: `runner.delivery_step`
    (the pre-review gate re-read the rollup and it is still running) and
    the resume scan (the delivery is skipped for this tick). The journal
    line is the existing `delivery_ci_pending`; the Issue line names the
    PR's head commit beyond it. A tick with no bound run id has no progress
    comment to update, so only the journal line is written.
    """
    number = int(issue["number"])
    event(
        "delivery_ci_pending", issue=number, pr=pr_url,
        pending="; ".join(render_check(entry) for entry in pending),
    )
    if not run_id:
        return
    safe_publish(
        run_id=run_id, issue=number, source_repo=repo, role=ROLE_REVIEW,
        action=lambda: publish(
            issue=number, source_repo=repo, run_id=run_id,
            language_code=language_code,
            head=pr_head(pr_url, repo), status_url=status_url,
        ),
    )


def defer_skipped(scene_value, pending: list, issue: dict, repo: str,
                  language_code: str, status_url: str | None = None) -> None:
    """The resume scan skipped this CI-pending delivery for this tick.

    The scan holds the recovered scene, so it names the PR and the run the
    line belongs to from there and the deferral itself is the one
    `defer_pending` performs.
    """
    defer_pending(
        scene_value.parsed.pr_url, pending, issue, repo,
        scene_value.parsed.run_id, language_code, status_url,
    )
