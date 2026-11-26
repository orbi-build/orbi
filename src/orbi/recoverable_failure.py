"""The recoverable Pi/model failure: resume it, or stop a dead loop (#2292).

Split out of `implementation_delivery` and `progress` so both stay under
the module-size ratchet (Article 3.5). The handler keeps the claim, the
worktree and the run-state file so the next tick's in-flight scan resumes
the SAME run with the same id, branch, worktree and progress comment.

ONE exception (Issue #2292): the same (run_id, failure fingerprint)
recurring to `FAILURE_STREAK_LIMIT` consecutive rounds is a dead loop, not
a transient error — the reported kit#128 scene resumed every 5 minutes for
24 hours. That stop reuses the blocked reporting path
(`report_delivery_failure` with `classify=True`), so the label transition,
the blocker comment and the hidden `orbi:failure:v1` record keep coming
from one place, and afterwards the failed worktree is retained under
`keep_failed_worktrees` so the agent's uncommitted changes are not lost
(the runner never commits them).

The module imports no `runner` (Article 3.3) and no
`implementation_delivery` (the caller passes the attempt and its health
recorder).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orbi import failure_report, language, user_summary
from orbi import delivery_labels
from orbi.failure import Failure, failure_detail, redact_local_paths
from orbi.failure_report import _classify_failure
from orbi.github import issue_comments
from orbi.journal import LOGGER, event
from orbi.pi_process import ROLE_IMPLEMENT, ModelWaitDeadError
from orbi.progress import (
    failure_marker,
    fenced,
    progress_body_with_outcome,
    progress_state,
    recovered_marker,
    run_marker,
)
from orbi.run_fields import RunFields


def recoverable_scene_body(
    *, run_id: str, recoverable_name: str, record: Failure,
    fingerprint: str, detail: str, run_info: RunFields,
    language_code: str, status_url: str | None = None,
    issue: int | None = None, title: str | None = None,
) -> str:
    """Render the classified recoverable-failure comment (Issue #1465).

    A recoverable failure retries on the next tick under the same run
    id; the headline names the CLASSIFIED reason — the same
    `failure.classify` code the blocked path uses — plus the retry, and
    the raw provider detail moves into a collapsed `<details>`. The
    comment carries the hidden `<!-- orbi:recovered=<reason_code> -->`
    reason marker, the `<!-- orbi:fail=<fingerprint>[:count] -->` marker
    the dead-loop guard counts (Issue #2292) and deliberately no
    `orbi:failure:v1` block. A single occurrence stays `ai-in-progress`
    (#1455/#227); the same fingerprint reaching `FAILURE_STREAK_LIMIT` is
    the dead loop the blocked path stops. `failure_scene` folds the same
    `(run_id, fingerprint, reason_code)` into one comment and gives a
    different failure its own. `run_info` is the run's fields; the
    sentence below is the ONE place that renders their space-separated
    text (Issue #2055).
    """
    return language.text(
        language_code, "progress.recoverable_scene",
        run_marker=run_marker(run_id),
        recovered_marker=recovered_marker(record.reason_code),
        failure_marker=failure_marker(fingerprint),
        recoverable_name=recoverable_name,
        reason_code=record.reason_code,
        run_info=run_info.text(),
        action_code=record.action_code,
        retry_safe=str(record.retry_safe).lower(),
        detail=fenced(redact_local_paths(detail)),
        diagnosis=language.text(language_code, "failure.diagnosis_summary"),
        summary=user_summary.summary_text(
            status_url=status_url, issue=issue, title=title,
            language_code=language_code,
        ),
    )


def _failure_streak_for(attempt: Any, fingerprint: str) -> int:
    """The run's consecutive failures with this fingerprint (Issue #2292).

    The recovery comments carry the `orbi:fail=<fingerprint>` marker, so
    the #825 streak scan reads them exactly like a classified failure
    comment. The history read fails open: a failed read degrades to
    "no streak yet" and never breaks the delivery report itself.
    """
    number = int(attempt.issue["number"])
    try:
        history = issue_comments(number, repo=attempt.source_repo)
    except Exception:
        LOGGER.exception("issue=%s failure history read failed", number)
        event(
            "failure_history_read_failed", issue=number,
            run_id=attempt.run_id,
        )
        return 0
    return failure_report._failure_streak(
        history, attempt.run_id, fingerprint, repo=attempt.source_repo,
    )


def _stop_repeated_failure(
    attempt: Any, exc: Exception, *, fingerprint: str, count: int,
    record_failed: Callable[[Any, Exception], None],
) -> None:
    """Stop resuming a dead loop: block the Issue, retain the agent's work.

    The same (run, failure fingerprint) reaching `FAILURE_STREAK_LIMIT`
    consecutive recoverable failures means the next tick would only repeat
    the same failure. The stop goes through the blocked reporting path with
    `classify=True`, so the label transition, the blocker comment and the
    hidden `orbi:failure:v1` record keep coming from one place. The comment
    names the reason and the human decision; after it lands, the failed
    worktree is retained under the existing `keep_failed_worktrees` rule so
    the agent's uncommitted changes are not lost (the runner never commits
    them).
    """
    number = int(attempt.issue["number"])
    lang = language.of(attempt.config)
    retained = attempt.config.keep_failed_worktrees
    LOGGER.error(
        "issue=%s recoverable failure repeated %s times (fingerprint %s); "
        "stopping the run", number, count, fingerprint,
    )
    event(
        "failure_streak_escalated", level=logging.ERROR, issue=number,
        run_id=attempt.run_id, streak=count, fingerprint=fingerprint,
    )
    record_failed(attempt, exc)
    # The required action carries the decision AND what happened to the
    # agent's work: the rendered Reason is a bounded summary, so a long
    # dirty-file list must never push the retention statement out of it.
    action = f"{language.text(
        lang, 'delivery.recoverable_streak_action',
        ready_label=attempt.facts.dispatch_label,
    )}{language.text(
        lang,
        'delivery.recoverable_streak_retained' if retained
        else 'delivery.recoverable_streak_removed',
    )}"
    try:
        failure_report.report_delivery_failure(
            exc, issue=attempt.issue, source_repo=attempt.source_repo,
            run_id=attempt.run_id, pr_url=None,
            worktree=attempt.worktree, branch=attempt.branch,
            role=ROLE_IMPLEMENT, language_code=lang,
            status_url=attempt.config.status_url,
            milestone=attempt.config.active_milestone,
            ready_label=attempt.facts.dispatch_label,
            action=action,
            reason=failure_detail(exc),
            diagnosis=(
                f"{failure_detail(exc)}\n"
                f"- base_branch: {attempt.facts.base_branch}\n"
                f"- base_sha: {attempt.base_sha or '-'}\n"
                f"run_id={attempt.run_id}\n"
                f"- priority: {attempt.priority}"
            ),
            classify=True, evidence=True,
            review_round=0,
            finish=True,
            publisher=attempt.publisher,
        )
    except Exception:
        # The Issue then keeps `ai-in-progress` and the next tick resumes
        # it (the same-run recovery), so the worktree is NOT cleaned up.
        LOGGER.exception("issue=%s failure reporting failed", number)
    else:
        # The stop landed: retain the scene (default on) so the agent's
        # uncommitted candidate changes stay inspectable and never reach a
        # commit.
        attempt.hooks.cleanup_worktree(
            attempt.ctx, attempt.config.repo_dir, keep=retained,
        )


def handle(
    attempt: Any, exc: Exception, *,
    record_failed: Callable[[Any, Exception], None],
) -> None:
    """Handle one recoverable Pi/model failure (never a crash).

    Keeps the run resumable and stays `ai-in-progress` — except when the
    SAME failure fingerprint recurs to `FAILURE_STREAK_LIMIT` consecutive
    rounds (Issue #2292), which stops the dead loop `ai-blocked`.
    """
    number = int(attempt.issue["number"])
    title = attempt.issue["title"]
    config = attempt.config
    publisher = attempt.publisher
    publish = attempt.publish
    run_id = attempt.run_id
    fingerprint = attempt.hooks.runner_health.failure_fingerprint(exc)
    streak = _failure_streak_for(attempt, fingerprint)
    if streak + 1 >= failure_report.FAILURE_STREAK_LIMIT:
        _stop_repeated_failure(
            attempt, exc, fingerprint=fingerprint, count=streak + 1,
            record_failed=record_failed,
        )
        return
    # Classified Pi/model infrastructure failures are
    # recoverable. Keep the claim, worktree and run-state file so the
    # next in-flight scan resumes this same run.
    recoverable_name = (
        language.text(
            language.of(config), "progress.recoverable_model_wait")
        if isinstance(exc, ModelWaitDeadError)
        else language.text(
            language.of(config), "progress.recoverable_pi_failure")
    )
    # The hung-model-request recovery is a CLASSIFIED,
    # AI-recoverable failure — NOT the terminal `ai-blocked`. The
    # worktree keeps the interrupted work and the run state file is
    # intact, so the Issue keeps `ai-in-progress`: the next tick's
    # in-flight restart scan (`pick_in_progress_issue`) resumes the
    # SAME run (same run id, branch, worktree, progress comment).
    # The recovery stays fail-fast (Pi was killed, the tick ends
    # cleanly below, the slot is released by `main`'s `finally`);
    # only the label outcome changes — never `ai-blocked`.
    LOGGER.exception("issue=%s %s", number, recoverable_name)
    # The recoverable path classifies its cause exactly like the
    # terminal one (Issue #1465), but the classification never
    # decides whether the run blocks: the recovery stays
    # `ai-in-progress` (#1455/#227).
    record = _classify_failure(exc, outcome="blocked")
    detail = failure_detail(exc)
    body = recoverable_scene_body(
        run_id=run_id, recoverable_name=recoverable_name, record=record,
        fingerprint=fingerprint, detail=detail, run_info=attempt.run_info,
        language_code=language.of(config),
        status_url=config.status_url, issue=number, title=title,
    )
    # The recovery comment is the delivery record, but the resume
    # does not parse it (the run state file, the worktree and the
    # `ai-in-progress` label carry the resume —
    # `worktree_resume_scene`): a failure here must only log.
    # An identical repeated failure of this run updates
    # the existing scene comment in place (the progress patch path)
    # instead of appending a duplicate on every retry tick.
    try:
        publisher.failure_scene(body)
    except Exception:
        LOGGER.exception(
            "issue=%s model_wait_recovered_comment_failed", number,
        )
    publish(
        action=lambda: publisher.finish(progress_body_with_outcome(
            progress_state(
                attempt.ctx, title=title, role=ROLE_IMPLEMENT,
                started=attempt.started, pr_url=None, review_round=0,
                priority=attempt.priority, language_code=language.of(config),
                status_url=config.status_url,
            ), outcome=(
                language.text(
                    language.of(config), "delivery.recoverable_outcome",
                    recoverable_name=recoverable_name,
                    reason_code=record.reason_code,
                    action_code=record.action_code,
                )
            ))),
    )
    # The recoverable failure reaches the health history
    # exactly like the terminal one — the resume loop retries the
    # same dead end, which IS the repeating-dead-end scene the
    # self-health check exists to catch (#246). Without this record
    # the #227 recovery path is invisible to it. Pure bypass: a
    # state-write failure never changes the delivery outcome.
    record_failed(attempt, exc)
