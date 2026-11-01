"""The ops-only branch of the implementation dispatch (Issue #2006).

An `ai-ops-only` ticket runs the SAME claim, worktree, session and run
machinery as a dev ticket (a Pi session with the real tools — there is no
command whitelist anywhere) and differs in exactly two places, both here:

- the session runs the ops playbook (`prompt_ops.md`, the sibling of the
  configured dev prompt) instead of the dev one;
- a pure-ops delivery — the real shell/API work and the evidence the session
  posted on the Issue, no commit — is COMPLETE without PR ceremony: the
  Issue is closed and the claim label removed.

Committed ops code, and uncommitted leftovers, are NOT an ops delivery: they
fall through to the deterministic closeout the dev path uses (committed ops
code takes the normal PR ceremony, leftovers fail fast there because the
Runner never commits uncommitted changes).

Extracted unchanged from `runner._dispatch_implementation` (CONSTITUTION
Article 3.2). This module imports no `runner` (Article 3.3): the commit
boundary probe and the progress-publish bypass are the caller's and arrive
as arguments.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from orbi import config as config_domain
from orbi import language, runner_health
from orbi.delivery_labels import IN_PROGRESS_LABEL
from orbi.delivery_scene import RunContext
from orbi.github import edit_issue
from orbi.journal import LOGGER, event, issue_context, run_command
from orbi.pi_activity import format_end_scene
from orbi.pi_process import ROLE_IMPLEMENT
from orbi.progress import (
    ProgressPublisher,
    progress_body_with_outcome,
    progress_state,
)


def ops_config(
    config: config_domain.RunnerConfig,
) -> config_domain.RunnerConfig:
    """Return the config of an ops session: the ops playbook.

    `prompt_ops.md` is the sibling of the configured dev prompt, so a custom
    prompt deployment carries the ops playbook next to it. A missing file
    fails the run fast through the delivery failure path with the exact path
    in the comment.
    """
    return replace(
        config, prompt=config.prompt.with_name("prompt_ops.md"),
    )


def deliver_pure_ops(
    *, ctx: RunContext,
    config: config_domain.RunnerConfig,
    publisher: ProgressPublisher,
    publish: Callable[..., None],
    run_info: str,
    title: str,
    started: float,
    priority: str,
    base_sha: str,
    head: str,
    dirty: str,
) -> bool:
    """Close the Issue when the ops session delivered evidence and no commit.

    `head`/`dirty` are the agent's commit boundary, probed by the caller
    with the same `_agent_delivery_boundary` the dev closeout uses.

    Return True when this delivery closed the Issue (no PR): the caller's
    `IssueResult("ops", None)`. Return False when the session committed code
    or left uncommitted changes, which take the deterministic dev closeout
    instead.
    """
    if head != base_sha or dirty:
        return False
    run_command([
        "gh", "issue", "close", str(ctx.issue),
        "--repo", ctx.source_repo,
    ])
    edit_issue(
        ctx.issue, repo=ctx.source_repo, remove=IN_PROGRESS_LABEL,
    )
    # Pure ops actions take no PR ceremony; the terminal transition mirrors
    # the content path — the claim label is removed, the Issue is closed, the
    # worktree stays as the run's evidence.
    publish(
        action=lambda: publisher.milestone(language.text(
            language.of(config),
            "runner.ops_milestone", run_info=run_info,
        )),
    )
    publish(
        action=lambda: publisher.finish(progress_body_with_outcome(
            progress_state(
                ctx, title=title, role=ROLE_IMPLEMENT,
                started=started, pr_url=None, review_round=0,
                priority=priority, language_code=language.of(config),
                status_url=config.status_url,
            ),
            outcome=language.text(
                language.of(config),
                "runner.ops_outcome",
            ),
        )),
    )
    event(
        "run_end",
        format_end_scene(
            run_id=ctx.run_id,
            issue=issue_context(ctx.source_repo, ctx.issue),
            role=ROLE_IMPLEMENT, result="ops_delivered",
            elapsed=time.monotonic() - started,
            pr="-", commit=head,
        ),
    )
    # The delivered outcome breaks any failure streak of this Issue in the
    # health history. Pure bypass: a state-write failure never changes the
    # delivery outcome.
    try:
        runner_health.record_run_attempt(
            runner_health.health_state_path(config.repo_dir),
            repo=ctx.source_repo, issue=ctx.issue, run_id=ctx.run_id,
            outcome="ops_delivered", fingerprint="",
        )
    except Exception:
        LOGGER.exception(
            "issue=%s health_success_record_failed", ctx.issue,
        )
    return True
