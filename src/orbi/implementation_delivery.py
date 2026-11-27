"""The implementation dispatch: one function per scene plus one shared closeout.

Extracted unchanged from `runner._dispatch_implementation` (CONSTITUTION
Article 3.2, Issue #2168). That function handled four scenes in one
618-line body; the delivery pipeline is now split into one entry point per
scene (`_dispatch_fresh_claim`, `_dispatch_restart_in_flight`,
`_dispatch_external_takeover`, `_dispatch_ops`), the shared attempt steps
(`_start_implementation`, `_claim_and_open`, `_run_session`) and one shared
closeout (`_closeout_implementation`). Moving code only: no logic, message
or label change.

The recoverable Pi/model failure handler lives in
`orbi.recoverable_failure` (Issue #2292; the module-size ratchet keeps
this module at its ceiling), reached through the same dispatch.

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

from orbi import claim_race
from orbi import config as config_domain
from orbi import (
    failure_report,
    issue_images,
    language,
    merge_exec,
    notify,
    ops_delivery,
    pi_session,
    plan_milestone,
    recoverable_failure,
    run_state,
    user_summary,
)
from orbi.blocked_comment import post_blocked_comment
from orbi.checks import test_status
from orbi.delivery_comment import opened_pr_comment_body, started_pi_comment_body
from orbi.delivery_labels import EVENT_CLAIM, EVENT_PR_OPENED
from orbi import delivery_labels
from orbi.delivery_scene import DeliveryFacts, RunContext
from orbi.failure import failure_detail, terminal_reason
from orbi.failure_report import _classify_failure
from orbi.gitops import (
    create_worktree,
    freeze_base,
    task_branch,
    worktree_path,
)
from orbi.github import (
    apply_label_patch,
    comment_issue,
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
from orbi.pi_activity import activity_snapshot, sanitize
from orbi.pi_command import ROLE_TICKET
from orbi.pi_process import (
    ROLE_IMPLEMENT,
    DeliveryIssueClosed,
    ModelWaitDeadError,
    RecoverablePiFailure,
)
from orbi.progress import (
    LiveProgressThrottle,
    ProgressPublisher,
    progress_body_with_outcome,
    progress_state,
    run_marker,
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
    # The steering poll found the Issue closed and stopped the session
    # (Issue #2468): the closeout then pushes what it committed and reports
    # the existing `issue_closed` stop, with no PR.
    issue_closed_stop: bool = False


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
    if (delivery_labels.IN_PROGRESS_LABEL not in facts.claim_labels
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
        fresh_claim=True,
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
                          hooks: ImplementationHooks,
                          fresh_claim: bool = False) -> IssueResult:
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
        delivered = _run_session(attempt, fresh_claim=fresh_claim)
        if delivered is not None:
            return delivered
        return _closeout_implementation(attempt)
    except (ModelWaitDeadError, RecoverablePiFailure) as exc:
        recoverable_failure.handle(
            attempt, exc, record_failed=_record_failed_attempt,
        )
        return IssueResult("failed", None)
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
    kind = claim_race.claim_window_yield(
        attempt.issue, attempt.facts, attempt.config,
        source_repo=attempt.source_repo, run_id=attempt.run_id,
    )
    if kind is not None:
        return IssueResult(kind, None)
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


def _claim_issue(attempt: _Attempt) -> None:
    """Apply the claim label and reset the stale-pickup clock (bypass)."""
    number = int(attempt.issue["number"])
    apply_label_patch(
        number, repo=attempt.source_repo, event=EVENT_CLAIM,
        ready_label=attempt.facts.dispatch_label,
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


def _yield_to_other_executor(attempt: _Attempt) -> bool:
    """Yield when another executor already started this Issue (Issue #2298).

    Read after OUR own started comment exists, so the list carries both
    sides' starts: the first live one wins (claim_race). This is the same
    GitHub read the started publish makes, so a failure here is a bypass
    failure (Issue #79): log it and run, never decide the delivery.
    """
    number = int(attempt.issue["number"])
    try:
        return claim_race.yield_to_another_executor(
            issue=number, source_repo=attempt.source_repo,
            comments=attempt.publisher.list_comments(),
            retire=functools.partial(
                attempt.hooks.cleanup_worktree, attempt.ctx,
                attempt.config.repo_dir),
        )
    except Exception as exc:
        LOGGER.exception("issue=%s claim_yield_check_failed: %s", number, exc)
        return False


def _run_session(attempt: _Attempt, *,
                 fresh_claim: bool = False) -> IssueResult | None:
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
    # The cross-deployment claim race is decided only by a FRESH claim
    # (Issue #2298): a restart resumes this deployment's own run and a
    # takeover follows its own PR. The loser stops here, before Pi.
    if fresh_claim and _yield_to_other_executor(attempt):
        return IssueResult("claim-yielded", None)
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
        try:
            pi_session.run_pi(
                attempt.issue, ctx, config,
                resume_context=attempt.resume_ctx,
                progress=LiveProgressThrottle(
                    ctx, publisher, title=title, role=ROLE_IMPLEMENT,
                    started=attempt.started, pr_url=None, review_round=0,
                    priority=attempt.priority,
                ),
            )
        except DeliveryIssueClosed:
            # The steering poll found the Issue closed and terminated the
            # session where it was (Issue #2468). The plan milestone and the
            # ops evidence path are moot: the closeout pushes the committed
            # work and reports the existing `issue_closed` stop.
            attempt.issue_closed_stop = True
            return None
    publish(
        action=lambda: plan_milestone.publish(
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
            issue_closed_stop=attempt.issue_closed_stop,
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
    # Issue #2369: this event's patch is computed from the callee's own
    # fresh label read and written as ONE whole-set replacement, so the
    # pre-upgrade claim's dispatch residue it must remove is seen there.
    apply_label_patch(
        int(attempt.issue["number"]), repo=attempt.source_repo,
        event=EVENT_PR_OPENED,
        ready_label=facts.dispatch_label,
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
        # The terminal evidence is recorded and the Issue is genuinely
        # `ai-blocked`; the recoverable paths (model_wait,
        # `ai-fix-needed`) and the simulated-kill scene never reach this
        # branch — their worktree is kept for the same-run resume. The
        # host-only `keep_failed_worktrees` (Issue #2175) decides whether
        # this terminal scene is retained; it never changes the outcome.
        if attempt.worktree is not None:
            attempt.hooks.cleanup_worktree(
                attempt.ctx, attempt.config.repo_dir,
                keep=attempt.config.keep_failed_worktrees,
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
        milestone=config.active_milestone,
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
        review_round=0,
        finish=(
            attempt.worktree is not None
            and attempt.publisher.comment_id is not None
        ),
        publisher=attempt.publisher,
    )


def report_ticket_only_failure(
    issue: dict, config: config_domain.RunnerConfig, source_repo: str,
    run_id: str, run_info: RunFields, publisher, publish,
    exc: BaseException,
) -> None:
    """Report the ticket-only run's failure; an unreadable image stops as the
    SAME `attachment_unavailable` rule every other path follows (Issue
    #2390), while every other error keeps the ticket-only comment."""
    if isinstance(exc, issue_images.Unreadable):
        _report_attachment_stop(issue, config, source_repo, run_id, exc)
        return
    number = int(issue["number"])
    detail = failure_detail(exc)
    ready_label = delivery_labels.queue_label(config.dispatch_label)
    try:
        post_blocked_comment(
            number, repo=source_repo,
            body=user_summary.notice(
                run_marker(run_id), "runner.ticket_only_failed",
                language_code=language.of(config), issue=number,
                status_url=config.status_url, title=issue.get("title"),
                detail=detail, run_info=run_info.text(), run_id=run_id,
            ),
            language_code=language.of(config), ready_label=ready_label)
        publish(
            action=lambda: publisher.milestone(language.text(
                language.of(config),
                "runner.ticket_only_blocked_milestone",
                detail=sanitize(detail), run_info=run_info.text(),
            )),
        )
    except Exception:
        LOGGER.exception("issue=%s ticket-only failure reporting failed", number)


def _report_attachment_stop(issue: dict, config: config_domain.RunnerConfig,
                            source_repo: str, run_id: str,
                            exc: BaseException) -> None:
    """The ticket-only run's unreadable-attachment stop (Issue #2390).

    ONE failure comment names the file and its repair, and `ai-blocked` is
    alone — the same rule the implementer and the review round follow.
    """
    language_code = language.of(config)
    ready_label = delivery_labels.queue_label(config.dispatch_label)
    failure_report.report_delivery_failure(
        exc, issue=issue, source_repo=source_repo, run_id=run_id,
        pr_url=None, worktree=None, branch="-", role=ROLE_TICKET,
        language_code=language_code, status_url=config.status_url,
        milestone=config.active_milestone, ready_label=ready_label,
        action=failure_report.terminal_action(
            failure_report._classify_failure(exc, outcome="blocked"),
            language_code,
        ),
        reason=failure_detail(exc),
        classify=False, evidence=False, finish=False,
    )
