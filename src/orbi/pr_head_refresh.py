#!/usr/bin/env python3
"""Make GitHub re-read the delivery branch when its PR never followed a push.

A delivery's merge waits for GitHub's own `mergeable` answer. GitHub derives
that answer from the PR's HEAD ref, which it re-reads when the branch moves —
except when it does not: on 2026-11-25/26 the runner pushed a base merge to
the task branch (`480d341b`) and `GET /pulls/3995` kept reporting the
pre-push head (`06ab6cba`) with `mergeable = null` for 8.5 hours. Every tick
read the same four states, found the branch behind the base, and deferred the
merge — no stop, no comment, no label change. A plain push to the branch
later made GitHub synchronize at once.

The repair is GitHub's own documented endpoint, `PUT
/repos/{owner}/{repo}/pulls/{pull_number}/update-branch` (docs.github.com/
en/rest/pulls/pulls): it "updates the pull request branch with the latest
upstream changes by merging HEAD from the base branch into the pull request
branch", so GitHub itself writes the branch and re-reads it. Its
`expected_head_sha` is the MOST RECENT COMMIT ON THE PULL REQUEST'S BRANCH —
the branch tip read from `git ls-remote`, never the stale head the PR view
reports (that value is exactly what answered 422 "expected head sha didn't
match current head ref" during the incident).

The attempt is bounded: one observation per tick is recorded in the run
state, and past `PR_HEAD_REFRESH_TICKS` observations the run stops with a
carried `human_decision_required` record and a message naming the cause and
the repair, because only GitHub can move a PR head — polling cannot. A
request GitHub rejects (422: the branch is not behind the base, or the merge
conflicts) is not a tick failure: it spends the tick's attempt, and the bound
decides the stop. Neither is an unreadable branch tip or an absent run state
(the delivery then waits exactly as it did before, and no uncounted refresh
loop starts).

This module imports no `runner` (CONSTITUTION Article 3.3) and calls no
`subprocess` directly (Article 3.4): every command goes through the one seam
in `orbi.journal`.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from orbi import failure, gitops, run_state
from orbi.failure_report import UnrecoverableDeliveryError
from orbi.journal import event, run_command

#: Issue #2440: how many ticks a delivery spends asking GitHub to re-read its
#: branch before the run stops and asks for a person. GitHub answers the
#: request immediately (202 Accepted) and one timer interval (five minutes)
#: is enough for the PR view to follow, so three attempts cover the outage
#: this guards against without holding a delivery for long.
PR_HEAD_REFRESH_TICKS = 3

#: The run-state field carrying the bounded counter: the stale PR head the
#: count belongs to and the observations so far. The file is per delivery
#: line (the same worktree across ticks), so the bound survives a tick.
_REFRESH_RECORD = "pr_head_refresh"


class PrHeadNotRefreshed(UnrecoverableDeliveryError):
    """GitHub never re-read the delivery branch after the runner's push.

    Only GitHub can move a pull request's head, so a PR that keeps reporting
    an old commit cannot be repaired by another review round: the run stops
    and the failure report asks a person for the one repair that works (a
    new commit on the branch, or the PR's "Update branch" button). The
    record is carried at the source (Issue #1756), so the comment's block
    names the decision instead of a classification of this message.
    """

    failure_record = failure.Failure(
        "human_decision_required",
        *failure.DISPOSITIONS["human_decision_required"],
    )

    def __init__(self, message: str, *, pr: int, pr_head: str,
                 branch_tip: str) -> None:
        super().__init__(message)
        self.pr = pr
        self.pr_head = pr_head
        self.branch_tip = branch_tip


def stop_message(*, pr_number: int, pr_head: str, branch: str,
                 branch_tip: str, ticks: int) -> str:
    """The reader-facing cause and repair of the bound being spent.

    The Reason line of the blocked comment is this sentence: it names what
    happened (GitHub never refreshed the PR after the push), the two heads
    that disagree and the one repair that has worked (a push GitHub sees).
    The resume step itself stays in the catalog action, never here.
    """
    return (
        f"GitHub did not refresh PR #{pr_number} after the runner pushed "
        f"the delivery branch: the PR still records head {pr_head} while "
        f"origin/{branch} is at {branch_tip}, after {ticks} attempts to make "
        "GitHub re-read the branch, so mergeability stays unknown and the "
        "merge cannot proceed. Recovery: push a new commit to "
        f"{branch} (or use the PR's \"Update branch\" button) so GitHub "
        "re-reads the branch; the delivery then continues from the "
        "refreshed head."
    )


def refresh_lagging_head(worktree: Path, *, repo: str, pr_number: int,
                         branch: str, pr_head: str) -> None:
    """Ask GitHub to re-read `branch` when the PR still reports an old head.

    Called by the merge gate at its `mergeable == "UNKNOWN"` deferral, the
    one place the incident's loop lived. A PR head equal to the branch tip
    (or a branch tip that cannot be read) is the wait this module leaves
    exactly as it was. Otherwise the observation is counted, GitHub is asked
    to update the branch, and past `PR_HEAD_REFRESH_TICKS` observations the
    run stops with `PrHeadNotRefreshed`.
    """
    if not isinstance(pr_head, str) or not pr_head:
        return
    tip = _branch_tip(branch, worktree=worktree)
    if tip is None or tip == pr_head:
        _clear(worktree)
        return
    ticks = _next_ticks(worktree, pr_head)
    if ticks is None:
        # No counter, no bound: an uncounted refresh loop is the polling
        # this Issue bans, so the tick waits exactly as it did before.
        event(
            "pr_head_refresh_unrecorded", level=logging.WARNING,
            pr=pr_number, pr_head=pr_head, branch_tip=tip,
        )
        return
    event(
        "pr_head_lagging", pr=pr_number, branch=branch, pr_head=pr_head,
        branch_tip=tip, ticks=ticks,
    )
    if ticks > PR_HEAD_REFRESH_TICKS:
        raise PrHeadNotRefreshed(
            stop_message(
                pr_number=pr_number, pr_head=pr_head, branch=branch,
                branch_tip=tip, ticks=ticks,
            ),
            pr=pr_number, pr_head=pr_head, branch_tip=tip,
        )
    _request_refresh(repo, pr_number, tip, worktree=worktree)


def _branch_tip(branch: str, *, worktree: Path) -> str | None:
    """The remote branch head, or None when the probe itself failed.

    A network or authentication failure on the read is not a verdict about
    the PR: the delivery waits exactly as it did before this module existed.
    """
    try:
        return gitops.remote_branch_head(branch, cwd=worktree)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        event(
            "pr_head_refresh_unreadable", level=logging.WARNING,
            branch=branch,
        )
        return None


def _request_refresh(repo: str, pr_number: int, tip: str, *,
                     worktree: Path) -> None:
    """`PUT /repos/{repo}/pulls/{pr_number}/update-branch` with the branch tip.

    GitHub's answer may be 422 (the branch is not behind the base, or the
    merge conflicts) or a transient server error: either way the attempt is
    spent and journaled, and the bound — not one API answer — decides the
    stop, so the tick keeps its normal deferral.
    """
    try:
        run_command(
            ["gh", "api", "-X", "PUT",
             f"repos/{repo}/pulls/{pr_number}/update-branch",
             "-f", f"expected_head_sha={tip}"],
            cwd=worktree, failure_log_level=logging.WARNING,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        event(
            "pr_head_refresh_failed", level=logging.WARNING, pr=pr_number,
            branch_tip=tip, returncode=getattr(exc, "returncode", None),
            stderr=str(getattr(exc, "stderr", None) or "").strip(),
        )
        return
    event("pr_head_refresh_requested", pr=pr_number, branch_tip=tip)


def _next_ticks(worktree: Path, pr_head: str) -> int | None:
    """Count this observation, or None when it cannot be recorded.

    The record belongs to ONE stale head: a different PR head is a different
    scene, so its budget starts at one — a resolved outage never eats a
    later one's attempts.
    """
    state = _read_state(worktree)
    if state is None:
        return None
    record = state.get(_REFRESH_RECORD)
    ticks = 0
    if isinstance(record, dict) and record.get("head") == pr_head:
        previous = record.get("ticks")
        if isinstance(previous, int) and not isinstance(previous, bool):
            ticks = previous
    ticks += 1
    state[_REFRESH_RECORD] = {"head": pr_head, "ticks": ticks}
    run_state.write_run_state_file(worktree, state)
    return ticks


def _clear(worktree: Path) -> None:
    """Drop the counter: the scene it counted resolved itself."""
    state = _read_state(worktree)
    if state is None or _REFRESH_RECORD not in state:
        return
    state.pop(_REFRESH_RECORD, None)
    run_state.write_run_state_file(worktree, state)


def _read_state(worktree: Path) -> dict | None:
    """The run state of this delivery, or None when there is none to write.

    A recreated worktree loses `.orbi/`, and a corrupt file already fails the
    resume readers before the gate runs; neither may start a refresh loop,
    so both mean "no counter" here.
    """
    try:
        return run_state.read_run_state(worktree)
    except ValueError:
        return None
