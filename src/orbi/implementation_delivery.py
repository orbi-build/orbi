"""The implementation dispatch: one function per scene plus one shared closeout.

Extracted unchanged from `runner._dispatch_implementation` (CONSTITUTION
Article 3.2, Issue #2168). That function handled four scenes in one
618-line body; the delivery pipeline is now split into one entry point per
scene (`_dispatch_fresh_claim`, `_dispatch_restart_in_flight`,
`_dispatch_external_takeover`, `_dispatch_ops`), the shared attempt steps
(`_start_implementation`, `_claim_and_open`, `_run_session`) and one shared
closeout (`_closeout_implementation`). Moving code only: no logic, message
or label change.

This module imports no `runner` (Article 3.3): the seams the runner owns —
`deliver_pr`, the commit-boundary probe, the worktree cleanup, the progress
publisher class, the end-scene formatter, the health module and the live
co-runner probe — arrive as `ImplementationHooks`, built by the caller at
dispatch time from its own module globals, so a `runner.<name>` patch still
intercepts the delivery.
"""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from orbi import config as config_domain
from orbi import (
    clarify,
    failure_report,
    language,
    merge_exec,
    milestone as milestone_bookkeeping,
    notify,
    ops_delivery,
    pi_session,
    run_state,
)
from orbi.checks import test_status
from orbi.delivery_comment import opened_pr_comment_body, started_pi_comment_body
from orbi.delivery_labels import (
    EVENT_CLAIM,
    EVENT_PR_OPENED,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
)
from orbi.delivery_scene import DeliveryFacts, RunContext
from orbi.failure import failure_detail, terminal_reason
from orbi.failure_report import _classify_failure
from orbi.gitops import (
    create_worktree,
    freeze_base,
    stable_branch_exists,
    task_branch,
    worktree_path,
)
from orbi.github import (
    apply_label_patch,
    comment_issue,
    has_in_progress_label,
    issue_priority,
)
from orbi.github_feedback import pr_delivery_rollup
from orbi.journal import (
    LOGGER,
    event,
    issue_context,
    run_command,
    set_active_run,
    set_run_id,
)
from orbi.pi_activity import activity_snapshot
from orbi.pi_process import (
    ROLE_IMPLEMENT,
    ModelWaitDeadError,
    RecoverablePiFailure,
)
from orbi.progress import (
    LiveProgressThrottle,
    ProgressPublisher,
    progress_body_with_outcome,
    progress_state,
    recoverable_scene_body,
    safe_publish,
)
from orbi.run_fields import RunFields


class IssueResult(NamedTuple):
    """The single outcome contract returned by `process_issue`.

    It lives beside the dispatch that produces three of its four kinds
    (Article 3.5: no existing module grows for the move); `runner`
    re-exports it so `runner.IssueResult` keeps working for callers.
    """

    kind: str
    url: str | None


@dataclass(frozen=True)
class ImplementationHooks:
    """The runner-owned seams the extracted dispatch calls back into.

    `orbi.runner` builds the bundle right before the dispatch (from its own
    module globals, so a `runner.<name>` patch is honoured); this module never
    imports runner (Article 3.3).
    """

    deliver_pr: Callable[..., str | None]
    agent_delivery_boundary: Callable[[Path], tuple[str, str]]
    cleanup_worktree: Callable[..., None]
    progress_publisher: Callable[..., ProgressPublisher]
    format_end_scene: Callable[..., str]
    runner_health: Any
    another_live_runner: Callable[[Path, int], bool]


@dataclass
class _Attempt:
    """The mutable state of one implementation delivery attempt.

    The four scenes share one pipeline: this bundle carries what the
    pipeline learns step by step, so every step is a small function instead
    of one 618-line body (Issue #2168) and the shared closeout reads the
    state the scene set up.
    """

    issue: dict
    source_repo: str
    facts: DeliveryFacts
    hooks: ImplementationHooks
    ops: bool
    run_id: str
    config: config_domain.RunnerConfig
    priority: str
    started: float
    base_sha: str | None = None
    branch: str | None = None
    worktree: Path | None = None
    ctx: RunContext | None = None
    run_info: RunFields | None = None
    publisher: ProgressPublisher | None = None
    publish: Callable[..., None] | None = None
    resume_ctx: str | None = None
    pr_opened: bool = False


def dispatch_implementation(issue: dict, source_repo: str,
                            facts: DeliveryFacts, *, ops: bool, run_id: str,
                            hooks: ImplementationHooks) -> IssueResult:
    """Dispatch one implementation delivery to its scene's entry point.

    FRESH_CLAIM (the normal dev delivery), RESTART_IN_FLIGHT (the restart
    resume), EXTERNAL_TAKEOVER (the contributor-PR takeover) and OPS (the
    full-execution ops session: the same claim, worktree and run machinery —
    no command whitelist exists anywhere — with the ops playbook instead of
    the dev one and an evidence-on-the-Issue closeout when the session
    delivers no commit). `run_id` is already the resumed id of an in-flight
    run; the classified scene chose this handler.
    """
    if facts.in_progress:
        # The claim-race window has two halves. The scan
        # snapshot lacking the label while THIS direct read sees it
        # means the claim landed between the scan and the read. Whether
        # that claimant is still ALIVE decides the semantics: with a
        # live co-runner holding a slot, resuming here would reuse the
        # live run's worktree/run_id under a second Pi (or fork a
        # duplicate delivery) — yield. With nobody else on the slots
        # (the #18 single-instance restart, the #668 second tick) the
        # run is an orphan and resumes below exactly as before. A
        # wrongly-yielded orphan is picked up next tick by the
        # slot-guarded in-flight scan — one cadence late, never
        # stranded.
        yielded, run_id = _resume_in_flight_run(
            issue, source_repo, facts, run_id=run_id, hooks=hooks,
        )
        if yielded is not None:
            return yielded
    if ops:
        return _dispatch_ops(
            issue, source_repo, facts, run_id=run_id, hooks=hooks,
        )
    if facts.external_takeover:
        return _dispatch_external_takeover(
            issue, source_repo, facts, run_id=run_id, hooks=hooks,
        )
    if facts.in_progress:
        return _dispatch_restart_in_flight(
            issue, source_repo, facts, run_id=run_id, hooks=hooks,
        )
    return _dispatch_fresh_claim(
        issue, source_repo, facts, run_id=run_id, hooks=hooks,
    )


def _resume_in_flight_run(issue: dict, source_repo: str,
                          facts: DeliveryFacts, *, run_id: str,
                          hooks: ImplementationHooks,
                          ) -> tuple[IssueResult | None, str]:
    """Resolve the resume of an in-flight run: yield, fail, or rebind its id."""
    number = int(issue["number"])
    config = facts.config
    if (IN_PROGRESS_LABEL not in facts.claim_labels
            and config.slot_dir is not None
            and config.max_concurrency is not None
            and hooks.another_live_runner(
                config.slot_dir, config.max_concurrency)):
        event(
            "claim_yield", issue=number,
            reason="label_landed_in_scan_window",
        )
        return IssueResult("claim-yielded", None), run_id
    if facts.resume_error is not None:
        # The worktree of this issue exists but its run
        # state is missing or corrupt: the same run cannot be
        # verified. Fail fast through the terminal failure path
        # (`ai-blocked` + the reason comment) — never a silent
        # fresh redo on top of unknown work.
        failure_report._report_resume_failure(
            number=number, source_repo=source_repo, run_id=run_id,
            error=facts.resume_error,
            language_code=language.of(config), ready_label=facts.dispatch_label,
            status_url=config.status_url)
        raise facts.resume_error
    if facts.resume_scene is not None:
        run_id = facts.resume_scene[0]
        # The attempt continues the dead run: re-bind the reused
        # id so every later line (including resuming_run) carries
        # it.
        set_run_id(run_id)
        event(
            "resuming_run", issue=number, run_id=run_id,
        )
    return None, run_id


def _dispatch_fresh_claim(issue: dict, source_repo: str,
                          facts: DeliveryFacts, *, run_id: str,
                          hooks: ImplementationHooks) -> IssueResult:
    """The normal dev delivery: a queue-label Issue with no open PR."""
    return _start_implementation(
        issue, source_repo, facts, ops=False, run_id=run_id, hooks=hooks,
    )


def _dispatch_restart_in_flight(issue: dict, source_repo: str,
                                facts: DeliveryFacts, *, run_id: str,
                                hooks: ImplementationHooks) -> IssueResult:
    """The restart resume: the orphaned in-flight run keeps its run and worktree."""
    return _start_implementation(
        issue, source_repo, facts, ops=False, run_id=run_id, hooks=hooks,
    )


def _dispatch_external_takeover(issue: dict, source_repo: str,
                                facts: DeliveryFacts, *, run_id: str,
                                hooks: ImplementationHooks) -> IssueResult:
    """The contributor-PR takeover: the PR's own head, no Pi session, its URL."""
    return _start_implementation(
        issue, source_repo, facts, ops=False, run_id=run_id, hooks=hooks,
    )


def _dispatch_ops(issue: dict, source_repo: str,
                  facts: DeliveryFacts, *, run_id: str,
                  hooks: ImplementationHooks) -> IssueResult:
    """The ops session: the ops playbook and the evidence-only closeout."""
    return _start_implementation(
        issue, source_repo, facts, ops=True, run_id=run_id, hooks=hooks,
    )


def _start_implementation(issue: dict, source_repo: str,
                          facts: DeliveryFacts, *, ops: bool, run_id: str,
                          hooks: ImplementationHooks) -> IssueResult:
    """Claim, run and close one implementation delivery (all four scenes)."""
    attempt = _Attempt(
        issue=issue, source_repo=source_repo, facts=facts, hooks=hooks,
        ops=ops, run_id=run_id, config=facts.config,
        priority=issue_priority(issue), started=time.monotonic(),
    )
    try:
        yielded = _claim_and_open(attempt)
        if yielded is not None:
            return yielded
        delivered = _run_session(attempt)
        if delivered is not None:
            return delivered
        return _closeout_implementation(attempt)
    except (ModelWaitDeadError, RecoverablePiFailure) as exc:
        return _handle_recoverable_failure(attempt, exc)
    except Exception as exc:
        return _handle_terminal_failure(attempt, exc)


def _claim_and_open(attempt: _Attempt) -> IssueResult | None:
    """Freeze the base, claim the Issue, open the worktree and bind the run."""
    # The FIRST step is inside the failure range (Issue #1749): a base
    # branch the remote lacks used to escape before the claim.
    attempt.base_sha = freeze_base(
        attempt.config.repo_dir, attempt.facts.base_branch,
    )
    attempt.branch = _delivery_branch(attempt)
    attempt.run_info = _run_fields(attempt)
    yielded = _claim_window_yield(attempt)
    if yielded is not None:
        return yielded
    _claim_issue(attempt)
    _open_worktree(attempt)
    attempt.resume_ctx = _resume_context(attempt)
    attempt.config = replace(
        attempt.config, base_sha=attempt.base_sha, run_id=attempt.run_id,
    )
    if attempt.ops:
        attempt.config = ops_delivery.ops_config(attempt.config)
    return None


def _delivery_branch(attempt: _Attempt) -> str:
    """The branch this delivery works on: the scene's own frozen identity."""
    number = int(attempt.issue["number"])
    facts = attempt.facts
    existing_worktree = facts.resume_scene[1] if facts.resume_scene else None
    branch = task_branch(attempt.source_repo, number, attempt.run_id)
    if existing_worktree is not None:
        # The resumed run keeps its ORIGINAL branch —
        # after a repo rename the re-derived name would carry the NEW
        # slug and no longer match the branch the worktree is on (a
        # second branch would be a second delivery). The worktree's
        # current branch IS the scene's branch.
        branch = run_command(
            ["git", "branch", "--show-current"],
            cwd=existing_worktree,
        )
    elif facts.external_takeover:
        # The external takeover delivers the contributor's own
        # branch — the identity the takeover PR is frozen on.
        branch = facts.takeover_pr["headRefName"]
    return branch


def _run_fields(attempt: _Attempt) -> RunFields:
    """The journal run fields of this attempt (the config sha included)."""
    facts = attempt.facts
    run_info = RunFields(
        run_id=attempt.run_id, priority=attempt.priority,
        base_branch=facts.base_branch, base_sha=attempt.base_sha,
        task_type="ops" if attempt.ops else None,
    )
    if facts.repo_policy is not None and facts.repo_policy.sha is not None:
        # The run comment carries the repository config sha
        # (the file blob at the default branch tip) so a policy change is
        # always visible on the run.
        run_info = replace(run_info, repo_config=facts.repo_policy.sha)
    LOGGER.info(
        "issue=%s %s", int(attempt.issue["number"]), run_info.text(),
    )
    return run_info


def _claim_window_yield(attempt: _Attempt) -> IssueResult | None:
    """Yield when another instance claimed the Issue inside the prepare window."""
    number = int(attempt.issue["number"])
    facts = attempt.facts
    config = attempt.config
    if (facts.in_progress or facts.dispatch_label not in facts.claim_labels
            or facts.takeover_pr is not None):
        return None
    # The pickup scan and the in-progress recheck above
    # both predate freeze_base (a seconds-long network round trip).
    # A label — or the stable branch — appearing inside that window
    # means another instance claimed this Issue while we were
    # preparing: yield. No label writes, no comments, nothing that
    # could interrupt the winner's in-flight delivery; the next
    # tick's scan picks work up again on its own. The
    # predicate keys on the repository's dispatch label (#527), not
    # the default constant — a custom-label repository's tickets
    # never carry `ai-ready`, and the guard must guard them too.
    if has_in_progress_label(number, attempt.source_repo):
        event("claim_yield", issue=number, reason="in_progress_label")
        return IssueResult("claim-yielded", None)
    if not facts.stable_branch_present and stable_branch_exists(
            config.repo_dir, facts.stable_branch,
    ):
        event("claim_yield", issue=number, reason="stable_branch_appeared")
        return IssueResult("claim-yielded", None)
    if config.clarify_thin_tickets and not clarify.enforce(
            attempt.issue, config, attempt.source_repo, attempt.run_id):
        return IssueResult("needs-detail", None)
    return None


def _claim_issue(attempt: _Attempt) -> None:
    """Apply the claim label and reset the stale-pickup clock (bypass)."""
    number = int(attempt.issue["number"])
    apply_label_patch(
        number, repo=attempt.source_repo, event=EVENT_CLAIM,
        current_labels=attempt.facts.claim_labels,
    )
    # The successful pickup resets the stale-pickup clock in
    # the health state file (bypass — a state-write failure never fails
    # the claim).
    try:
        attempt.hooks.runner_health.record_pickup(attempt.config.repo_dir)
    except Exception:
        LOGGER.exception("issue=%s health_pickup_record_failed", number)


def _open_worktree(attempt: _Attempt) -> None:
    """Bind the stop scene and publisher, then create and mark the worktree."""
    number = int(attempt.issue["number"])
    title = attempt.issue["title"]
    config = attempt.config
    facts = attempt.facts
    existing_worktree = facts.resume_scene[1] if facts.resume_scene else None
    takeover_pr = facts.takeover_pr
    external_takeover = facts.external_takeover
    # The Issue is in flight from the claim label on: bind the stop
    # scene so a SIGTERM during this tick logs the active
    # Issue context, not only systemd's generic "Stopped" line. The
    # branch and worktree path are the same derived values the
    # worktree creation below uses (bound before the worktree exists);
    # the run identity is bound once as a frozen RunContext and
    # the failure/finish paths unpack it from there.
    ctx = RunContext(
        run_id=attempt.run_id, issue=number, branch=attempt.branch,
        worktree=existing_worktree or worktree_path(
            config.repo_dir, attempt.source_repo, number, attempt.run_id,
        ),
        source_repo=attempt.source_repo,
    )
    set_active_run(ctx, title)
    publisher = attempt.hooks.progress_publisher(
        number, attempt.source_repo, attempt.run_id, run_command=run_command,
        language_code=language.of(config), status_url=config.status_url,
    )
    attempt.publisher = publisher
    attempt.publish = functools.partial(
        safe_publish, run_id=attempt.run_id, issue=number,
        source_repo=attempt.source_repo, role=ROLE_IMPLEMENT,
    )
    worktree = create_worktree(
        config.repo_dir, attempt.source_repo, number, attempt.run_id,
        attempt.base_sha,
        existing=existing_worktree,
        # A stable branch without an open PR is the interrupted push/
        # create gap: continue from that branch rather than trying to
        # create a second local branch with the same name.
        existing_branch=facts.stable_branch_present or takeover_pr is not None,
        # An external takeover checks out the
        # contributor's own head branch — the identity the takeover
        # PR is frozen on.
        branch=(
            takeover_pr["headRefName"] if external_takeover else None
        ),
        pr_number=(
            takeover_pr["number"] if external_takeover else None
        ),
    )
    attempt.worktree = worktree
    ctx = replace(ctx, worktree=worktree)
    attempt.ctx = ctx
    # The run state file is the same-run marker —
    # written for EVERY run (a fresh one included, so a later
    # interruption can be verified and resumed), refreshed for a
    # resumed one (same run id, never a second marker).
    run_state.write_run_state(ctx)
    if external_takeover:
        # The takeover's engine push line starts on the
        # contributor's own head (Issue #608): the merge record's
        # external_commits (Issue #833) must subtract only what the
        # engine pushes on top of this foreign head, never the head
        # itself. No session has run yet, so HEAD is exactly that
        # head; the record is set once per run.
        run_state.record_pushed_base(worktree, run_command(
            ["git", "rev-parse", "HEAD"], cwd=worktree,
        ))


def _resume_context(attempt: _Attempt) -> str | None:
    """The previous session's resume context and its resume_continue line."""
    number = int(attempt.issue["number"])
    worktree = attempt.worktree
    # The new session starts from the existing work —
    # the uncommitted changes and the previous session's progress —
    # instead of a fresh redo. A clean worktree without a previous
    # session is a fresh scene (None, the pre-#219 prompt).
    resume_ctx = pi_session.resume_context(worktree)
    if resume_ctx is not None:
        session_dir = worktree / ".pi-session"
        previous_sessions = (
            len([p for p in session_dir.glob("*.jsonl")
                if p.is_file()])
            if session_dir.is_dir() else 0
        )
        snapshot = activity_snapshot(session_dir)
        event(
            "resume_continue", issue=number, worktree=worktree,
            changed_files=len(pi_session.changed_files(worktree)),
            reused_runs=previous_sessions,
            previous_session=(
                snapshot.get("session_id") if snapshot else None
            ) or "-",
        )
    return resume_ctx


def _run_session(attempt: _Attempt) -> IssueResult | None:
    """Publish the start, run the Pi session and publish the plan milestone."""
    title = attempt.issue["title"]
    config = attempt.config
    facts = attempt.facts
    ctx = attempt.ctx
    publisher = attempt.publisher
    publish = attempt.publish
    # One `Orbi started Pi:` comment per run; a resume PATCHes it (#1369).
    publish(
        action=lambda: publisher.started(started_pi_comment_body(
            ctx, attempt.run_info, extra_fields=facts.repo_config_fields,
            language_code=language.of(config),
            status_url=config.status_url, issue_title=title,
        )),
    )
    # The whole ProgressPublisher path is a bypass — a
    # failure here (404, rate limit) is logged and never skips
    # `run_pi` or fails the delivery.
    publish(
        action=lambda: publisher.ensure(progress_body_with_outcome(
            progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT,
                started=attempt.started, pr_url=None, review_round=0,
                priority=attempt.priority, language_code=language.of(config),
                status_url=config.status_url,
            ),
        )),
    )
    if facts.takeover_pr is None:
        pi_session.run_pi(
            attempt.issue, ctx, config,
            resume_context=attempt.resume_ctx,
            progress=LiveProgressThrottle(
                ctx, publisher, title=title, role=ROLE_IMPLEMENT,
                started=attempt.started, pr_url=None, review_round=0,
                priority=attempt.priority,
            ),
        )
    publish(
        action=lambda: milestone_bookkeeping._publish_plan_milestone(
            publisher, attempt.worktree),
    )
    if attempt.ops:
        # An ops delivery without a commit is COMPLETE — the evidence
        # the session posted on the Issue is the deliverable; committed
        # code (or uncommitted leftovers) falls through to the
        # deterministic closeout below.
        head, dirty = attempt.hooks.agent_delivery_boundary(attempt.worktree)
        if ops_delivery.deliver_pure_ops(
            ctx=ctx, config=config, publisher=publisher, publish=publish,
            run_info=attempt.run_info, title=title, started=attempt.started,
            priority=attempt.priority, base_sha=attempt.base_sha,
            head=head, dirty=dirty,
        ):
            return IssueResult("ops", None)
    return None


def _closeout_implementation(attempt: _Attempt) -> IssueResult:
    """The shared closeout: PR creation, the label transition and the run end."""
    title = attempt.issue["title"]
    config = attempt.config
    facts = attempt.facts
    # The deterministic closeout (commit boundary, base
    # freshness + absorb, plain push, PR creation, PR verification)
    # is the Runner's job — the agent stopped at the committed
    # delivery.
    pr_url = (
        facts.takeover_pr["url"] if facts.takeover_pr is not None
        else attempt.hooks.deliver_pr(
            attempt.ctx, facts.base_branch, attempt.base_sha,
            issue_title=title, repo_dir=config.repo_dir,
            language_code=language.of(config),
            attribution_footer=config.attribution_footer,
            attribution_link=config.attribution_link,
        )
    )
    attempt.ctx = replace(attempt.ctx, pr=pr_url)
    commit = run_command(
        ["git", "rev-parse", "HEAD"], cwd=attempt.worktree,
    )
    if pr_url is None:
        return _closeout_issue_closed(attempt, commit)
    # The implementer always commits the delivery on top of the
    # frozen base, so the head always advanced.
    apply_label_patch(
        int(attempt.issue["number"]), repo=attempt.source_repo,
        event=EVENT_PR_OPENED, current_labels={IN_PROGRESS_LABEL},
    )
    # The label transition has landed and must remain the state
    # reported if the following notification write exhausts.
    attempt.pr_opened = True
    _publish_pr_opened(attempt, pr_url)
    _finish_pr_opened(attempt, pr_url, commit)
    # An external takeover reports its own kind — the
    # delivery wait then closes the triage Issue itself after the
    # merge (the external PR body carries no `Fixes #N` for this
    # Issue, so GitHub never closes it natively).
    return IssueResult(
        "external-pr" if facts.external_takeover else "pr", attempt.ctx.pr,
    )


def _closeout_issue_closed(attempt: _Attempt, commit: str) -> IssueResult:
    """The delivery completed with no PR because the Issue was closed."""
    title = attempt.issue["title"]
    config = attempt.config
    ctx = attempt.ctx
    publisher = attempt.publisher
    publish = attempt.publish
    # The Issue was closed during delivery — the
    # delivery is complete with no PR (deliver_pr already left
    # the explanatory comment). No label patch (labels are moot
    # on a closed Issue, and a later reopen resumes the run
    # naturally), no scene comment, nothing to wait for: the
    # tick ends cleanly.
    publish(
        action=lambda: publisher.finish(progress_body_with_outcome(
            progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT,
                started=attempt.started, pr_url=None, review_round=0,
                priority=attempt.priority, language_code=language.of(config),
                status_url=config.status_url,
            ),
            outcome=language.text(
                language.of(config), "delivery.outcome_issue_closed"),
        )),
    )
    event(
        "run_end",
        attempt.hooks.format_end_scene(
            run_id=attempt.run_id,
            issue=issue_context(attempt.source_repo, ctx.issue),
            role=ROLE_IMPLEMENT, result="issue_closed",
            elapsed=time.monotonic() - attempt.started,
            pr="-", commit=commit,
        ),
    )
    # The delivered outcome breaks any failure
    # streak of this Issue in the health history. Pure bypass.
    _record_success(attempt, "issue_closed")
    return IssueResult("issue-closed", None)


def _publish_pr_opened(attempt: _Attempt, pr_url: str) -> None:
    """Post the PR scene comment and the human-review checklist (bypasses)."""
    number = int(attempt.issue["number"])
    title = attempt.issue["title"]
    config = attempt.config
    try:
        comment_issue(
            number, repo=attempt.source_repo,
            body=opened_pr_comment_body(
                attempt.run_id, attempt.run_info, pr_url,
                external=attempt.facts.external_takeover,
                issue=number, issue_title=title,
                language_code=language.of(config),
                status_url=config.status_url,
            ),
        )
    except Exception as exc:
        # The PR is the delivery boundary: notification failure must
        # never turn an already-open PR into a blocked delivery. The
        # next tick can retry the notification from the PR scene.
        LOGGER.warning(
            "issue=%s post-PR notification failed pr=%s: %s",
            number, pr_url, exc,
        )
        event(
            "progress_comment_failed", level=logging.WARNING,
            issue=issue_context(attempt.source_repo, number), pr=pr_url,
            reason=str(exc),
        )
    if config.human_review_gate:
        # The human acceptance checklist — the readable
        # face of the gate — posts ONCE per delivery, at the moment
        # the delivery completes (the PR opens), with its one wait
        # message when column 2 needs a person. Bypass: a failed
        # checklist never fails the delivery; the gate itself is the
        # label check in the review rounds, and a checklist-less
        # delivery still holds there when column 2 is non-empty.
        try:
            body, needs_person = merge_exec.human_review_checklist(
                attempt.worktree, config,
                run_id=attempt.run_id, pr_url=pr_url,
                test_status=test_status(
                    pr_delivery_rollup(pr_url, attempt.source_repo)[1],
                ),
                issue=number, title=title,
            )
            if needs_person:
                notify.human_review_waiting(attempt.source_repo, number, title)
            comment_issue(number, repo=attempt.source_repo, body=body)
        except Exception as exc:
            LOGGER.warning(
                "issue=%s post-PR notification failed pr=%s: %s",
                number, pr_url, exc,
            )
            event(
                "progress_comment_failed", level=logging.WARNING,
                issue=issue_context(attempt.source_repo, number), pr=pr_url,
                reason=str(exc),
            )


def _finish_pr_opened(attempt: _Attempt, pr_url: str, commit: str) -> None:
    """Publish the PR-opened report, the run end line and the health record."""
    title = attempt.issue["title"]
    config = attempt.config
    ctx = attempt.ctx
    publisher = attempt.publisher
    publish = attempt.publish
    # Issue #1733: the tick ends here with the PR open, so the
    # last report names the state left behind — the merge gate's
    # own `checks` stage, published through the same `step=`
    # path — instead of the finished `implement` action.
    publish(
        action=lambda: publisher.finish(progress_body_with_outcome(
            progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT, started=attempt.started,
                pr_url=pr_url, review_round=0, priority=attempt.priority,
                language_code=language.of(config), step="checks",
                status_url=config.status_url,
            ), outcome=language.text(
                language.of(config), "delivery.outcome_pr_opened"))),
    )
    event(
        "run_end",
        attempt.hooks.format_end_scene(
            run_id=attempt.run_id,
            issue=issue_context(attempt.source_repo, ctx.issue),
            role=ROLE_IMPLEMENT, result="pr_opened",
            elapsed=time.monotonic() - attempt.started,
            pr=pr_url, commit=commit,
        ),
    )
    # The delivered outcome breaks any failure streak of
    # this Issue in the health history (the streak-break contract of
    # `repeated_failure_findings`). Pure bypass: a state-write
    # failure never changes the delivery outcome.
    _record_success(attempt, "pr_opened")


def _record_success(attempt: _Attempt, outcome: str) -> None:
    """Record a delivered outcome in the health history (pure bypass)."""
    number = int(attempt.issue["number"])
    try:
        attempt.hooks.runner_health.record_run_attempt(
            attempt.hooks.runner_health.health_state_path(
                attempt.config.repo_dir),
            repo=attempt.source_repo, issue=number, run_id=attempt.run_id,
            outcome=outcome, fingerprint="",
        )
    except Exception:
        LOGGER.exception("issue=%s health_success_record_failed", number)


def _record_failed_attempt(attempt: _Attempt, exc: Exception) -> None:
    """Record a failed run attempt for the self-health check (pure bypass)."""
    number = int(attempt.issue["number"])
    try:
        attempt.hooks.runner_health.record_run_attempt(
            attempt.hooks.runner_health.health_state_path(
                attempt.config.repo_dir),
            repo=attempt.source_repo, issue=number, run_id=attempt.run_id,
            outcome="failed",
            fingerprint=attempt.hooks.runner_health.failure_fingerprint(exc),
        )
    except Exception:
        LOGGER.exception("issue=%s health_failure_record_failed", number)


def _handle_recoverable_failure(attempt: _Attempt,
                               exc: Exception) -> IssueResult:
    """The recoverable Pi/model failure: keep the run resumable, never block."""
    number = int(attempt.issue["number"])
    title = attempt.issue["title"]
    config = attempt.config
    publisher = attempt.publisher
    publish = attempt.publish
    run_id = attempt.run_id
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
        detail=detail, run_info=attempt.run_info,
        language_code=language.of(config),
        status_url=config.status_url, issue=number, title=title,
    )
    # The recovery comment is the delivery record, but the resume
    # does not parse it (the run state file, the worktree and the
    # `ai-in-progress` label carry the resume —
    # `worktree_resume_scene`): a failure here must only log.
    # Falling through to the generic handler below would mark the
    # Issue `ai-blocked` — exactly the unrecoverable state Issue
    # #227 forbids for this recovery.
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
    _record_failed_attempt(attempt, exc)
    return IssueResult("failed", None)


def _handle_terminal_failure(attempt: _Attempt, exc: Exception) -> IssueResult:
    """The terminal failure: report the evidence, block the Issue, clean up."""
    number = int(attempt.issue["number"])
    LOGGER.exception("issue=%s failed", number)
    # Record the failed run attempt (conservative failure
    # fingerprint) so the next tick's self-health check can detect a
    # repeating dead end (the #246 scene). Pure bypass: a state-write
    # failure never changes the delivery outcome.
    _record_failed_attempt(attempt, exc)
    try:
        _report_terminal_failure(attempt, exc)
    except Exception:
        LOGGER.exception("issue=%s failure reporting failed", number)
    else:
        # The terminal evidence is recorded (journal +
        # `Orbi failed` comment) and the Issue is genuinely
        # `ai-blocked` — the scene is never needed again (a retry
        # gets a new run id and worktree), so clean it up. The
        # recoverable paths (ModelWaitDeadError, ai-fix-needed) and
        # the simulated-kill scene (blocked transition never landed)
        # never reach this branch — the worktree is kept for the
        # same-run resume.
        if attempt.worktree is not None:
            attempt.hooks.cleanup_worktree(
                attempt.ctx, attempt.config.repo_dir,
            )
    # The failure is terminal — the Issue is `ai-blocked`
    # and the `Orbi failed` comment is posted above. Returning
    # `None` ends the tick cleanly: `main` skips the delivery wait
    # (there is no PR) and the slot is released by its `finally`.
    # Re-raising here would escape `main` and crash the service on an
    # already-handled delivery failure (the #239 scene: the
    # `delivery_no_commit` RuntimeError killed the tick). When the
    # reporting itself failed the Issue keeps `ai-in-progress`, and
    # the next tick's restart-resume scan recovers it — no crash
    # needed for either outcome.
    return IssueResult("failed", None)


def _report_terminal_failure(attempt: _Attempt, exc: Exception) -> None:
    """The shared terminal reporter: `ai-blocked` with the evidence."""
    facts = attempt.facts
    config = attempt.config
    # The shared terminal reporter: every failure
    # reaching this handler is terminal by design (the
    # recoverable Pi failures have their own handler above), so
    # it never classifies the OUTCOME — `ai-blocked` with the plain
    # `Orbi failed` template. The current delivery-state label
    # is derived from the `pr_opened` flag (the only label
    # present at this point): `ai-pr-opened` when the PR
    # transition landed, otherwise `ai-in-progress` (the claim
    # label) — docs/workflow.mdx label lifecycle:
    # `ai-pr-opened` is removed on terminal failure. The terminal
    # action is honest about what is known (Issue #1651).
    terminal_record = _classify_failure(exc, outcome="blocked")
    failure_report.report_delivery_failure(
        exc, issue=attempt.issue, source_repo=attempt.source_repo,
        run_id=attempt.run_id, pr_url=None,
        worktree=attempt.worktree, branch=attempt.branch,
        role=ROLE_IMPLEMENT,
        language_code=language.of(config),
        status_url=config.status_url,
        ready_label=facts.dispatch_label,
        action=failure_report.terminal_action(
            terminal_record, language.of(config),
            base_branch=facts.base_branch,
        ),
        reason=terminal_reason(
            failure_detail(exc), terminal_record,
            base_branch=facts.base_branch,
            language_code=language.of(config),
        ),
        diagnosis=(
            f"{failure_detail(exc)}\n"
            f"- base_branch: {facts.base_branch}\n"
            f"- base_sha: {attempt.base_sha or '-'}\n"
            f"run_id={attempt.run_id}\n"
            f"- priority: {attempt.priority}"
        ),
        classify=False, evidence=True,
        # The blocked patch must clear the queue label, or the
        # Issue is claimed again every tick (Issue #1749). Before
        # the claim write landed (the publisher is created right
        # after it) the scanned labels are the honest source.
        current_labels=(
            {PR_OPENED_LABEL, facts.dispatch_label} if attempt.pr_opened
            else {IN_PROGRESS_LABEL, facts.dispatch_label}
            if attempt.publisher is not None else set(facts.claim_labels)
        ),
        review_round=0,
        finish=(
            attempt.worktree is not None
            and attempt.publisher.comment_id is not None
        ),
        publisher=attempt.publisher,
    )
