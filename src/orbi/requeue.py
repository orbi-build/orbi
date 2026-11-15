"""The ONE automatic re-queue of a transient GitHub failure (Issue #2202).

`failure.DISPOSITIONS` rates `github_transient` as `("requeue", True)`: a plain
re-queue can succeed WITHOUT a human repair. In the opened-PR phase the engine
already resumes by itself; only the implement-phase terminal report (no PR
exists yet) used to end in `ai-blocked` and wait for somebody to relabel the
Issue. When the system already has what it needs to continue, it must not wait
for a person to do a mechanical step.

This module makes that decision, once per delivery line:

- the FIRST `github_transient` terminal failure of an Issue whose trusted
  comments carry no re-queue marker applies the `EVENT_REQUEUE` label patch
  (the resolved dispatch label back, every delivery-state label removed) and
  posts ONE comment of its own kind (`<!-- orbi:comment=requeued -->`) saying
  the run hit a temporary GitHub error and was re-queued automatically. That
  comment IS the run's terminal message: there is no failure block, no
  `ai-blocked`, no milestone notification and no second terminal scene, so
  nothing shows a state a person must act on;
- THAT COMMENT IS THE COUNTER. A runner-written re-queue marker already on the
  Issue makes the same failure terminal exactly as before (`ai-blocked` with
  the failure block), so an unreachable GitHub can never loop. No per-issue
  counter, no backoff, no new state store;
- the marker is trusted only from a comment whose author can write to the
  repository or is this runner's own account — the authorship rule the failure
  report already applies (`orbi.comment_trust`).

`provider_quota` and every other reason never reach this module: a provider
quota stop stays terminal on purpose (Issue #698).
"""
from __future__ import annotations

from orbi import failure, language, user_summary
from orbi.comment_kind import (
    COMMENT_KIND_REQUEUED, comment_kind, comment_kind_marker,
)
from orbi.comment_trust import trusted_comments
from orbi.delivery_labels import EVENT_REQUEUE, READY_LABEL
from orbi.github import (
    apply_label_patch, comment_issue, issue_comments, issue_labels,
)
from orbi.journal import LOGGER, event
from orbi.progress import run_marker

# The outcome string of a re-queued delivery: distinct from `blocked` and
# `fix_needed`, so no reader mistakes it for a stop that needs a person.
OUTCOME_REQUEUED = "requeued"


def marker_present(comments: list[dict], *, repo: str) -> bool:
    """True when a trusted comment already records one automatic re-queue.

    A pure scan over the already-fetched comment list: the kind marker is
    read from the official metadata prefix (`orbi.comment_kind`), so a
    marker-shaped string quoted in prose never counts, and the author must
    pass the same trust filter the failure report uses.
    """
    return any(
        comment_kind(comment.get("body")) == COMMENT_KIND_REQUEUED
        for comment in trusted_comments(comments, repo=repo)
    )


def comment_body(*, run_id: str, reason: str, language_code: str,
                 status_url: str | None, issue: int, title: str) -> str:
    """The one re-queue comment: run marker, kind marker, readable sentence."""
    return (
        f"{run_marker(run_id)}\n"
        f"{comment_kind_marker(COMMENT_KIND_REQUEUED)}\n"
        + user_summary.append_summary(
            language.text(
                language_code, "failure.requeued",
                reason=failure.failure_summary(reason, language_code),
            ),
            status_url=status_url, issue=issue, title=title,
            language_code=language_code,
        )
    )


def report_once(record: failure.Failure, *, issue: dict, source_repo: str,
                run_id: str, reason: str, current_labels,
                language_code: str, status_url: str | None,
                ready_label: str = READY_LABEL) -> str | None:
    """Re-queue the Issue once, or return None to keep the terminal report.

    Only a `github_transient` record can be re-queued, and only while no
    trusted comment records an earlier re-queue. A failed history read keeps
    the re-queue (the guard must never turn a transient failure into a
    blocked Issue by itself): the read is the same GitHub the failure came
    from, and the comment this path posts is the marker the next read looks
    for.
    """
    if record.reason_code != "github_transient":
        return None
    number = int(issue["number"])
    try:
        history = issue_comments(number, repo=source_repo)
    except Exception:
        # The same comment-history read the failure report performs, and
        # the same fail-open rule: a read failure never turns a transient
        # failure into a blocked Issue by itself.
        LOGGER.exception("issue=%s re-queue history read failed", number)
        event("failure_history_read_failed", issue=number, run_id=run_id)
    else:
        if marker_present(history, repo=source_repo):
            return None
    labels = (
        current_labels if current_labels is not None
        else issue_labels(number, source_repo)
    )
    apply_label_patch(
        number, repo=source_repo, event=EVENT_REQUEUE,
        current_labels=labels, ready_label=ready_label,
    )
    comment_issue(number, repo=source_repo, body=comment_body(
        run_id=run_id, reason=reason, language_code=language_code,
        status_url=status_url, issue=number, title=issue["title"],
    ))
    return OUTCOME_REQUEUED
