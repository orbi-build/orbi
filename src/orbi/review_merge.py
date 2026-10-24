"""Review and merge: the delivery's independent review loop and merge gate.

This module owns the domain that closed the delivery in `runner.py`: the
independent review round (`_run_review_round`, `review_and_merge_if_clean`)
and the round comment (`review_round_comment_body`,
`_review_findings_markdown`), all moved here unchanged (Article 3.2: an
extraction changes no behaviour).

The merge execution and closeout — the gate, its three exception types, the
merge-method selection, the merge record metrics, the deployment-checkout
sync and the human-review checklist — is `orbi.merge_exec` (Issue #1857).
This module calls it and catches the exception types from it; `runner`
reaches `human_review_checklist` through `merge_exec` directly.

The machine-readable verdict protocol is not part of this orchestration
(Issue #1644): `orbi.review_verdict` owns the parser, its validation and
`review_has_findings`, and this module imports the two names the review
round calls. Binding a clean verdict to the exact PR head stays in the
merge gate, not in the parser.

It imports nothing from `orbi.runner` (Article 3.3). The review loop pulls
in the helpers it cannot travel without — the PR freeze, the
base-freshness assessment, the run-state push record the merge metrics
read, the live progress throttle and the scene resume — as its own
definitions or as imports from the leaf module that owns them.
"""
from __future__ import annotations

import functools
import logging
import re
import subprocess
import time
from dataclasses import replace
from enum import Enum
from pathlib import Path

from orbi import config as config_domain
from orbi import failure_report, merge_exec, pi_session, run_state, scene
from orbi.blocked_comment import post_blocked_comment
from orbi.checks import test_status
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    EVENT_FIX_NEEDED,
    EVENT_HUMAN_REVIEW_WAITING,
    EVENT_MERGED,
    EVENT_PR_OPENED,
    HUMAN_REVIEW_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    is_resumable,
    needs_human_intervention,
)
from orbi.delivery_scene import RunContext
from orbi.failure import failure_detail
from orbi.failure_report import (
    GateCIFailure,
    HumanDecisionRequired,
    ReviewRoundsExhausted,
    UnrecoverableDeliveryError,
)
from orbi.github import (
    RESUME_PR_STATE_TIMEOUT_SECONDS,
    apply_label_patch,
    comment_issue,
    commit_check_runs,
    issue_comments,
    issue_labels,
    issue_priority,
    pr_view,
)
from orbi.github_feedback import check_summaries, pr_delivery_rollup
from orbi import base_ci_red, language, user_summary
from orbi.delivery_comment import merged_pr_comment_body  # noqa: F401
from orbi.gitops import (
    is_ancestor,
    task_branch,
    worktree_path,
)
from orbi.journal import (
    LOGGER,
    current_run_id,
    event,
    run_command,
    validate_run_id,
)
from orbi.merge_exec import (
    DeliveryDeferred,
    MergeBlockedByIssueLabel,
    RecoverableMergeGateError,
)
from orbi.merge_handoff import (
    MergeHandoffRequired,
    handle_merge_handoff,
)
from orbi.pi_command import ROLE_REVIEW
from orbi.comment_kind import (
    COMMENT_KIND_FAILURE,
    COMMENT_KIND_MERGED,
    COMMENT_KIND_REVIEW_ROUND,
    comment_kind_marker,
)
from orbi.progress import (
    LiveProgressThrottle,
    ProgressPublisher,
    progress_body_with_outcome,
    progress_state,
    run_marker,
    safe_publish,
    step_publisher,
)
from orbi.review_rounds import (
    MAX_REVIEW_ROUNDS,
    effective_review_round,
    review_round_comment_body,
)
from orbi.review_verdict import parse_review_verdict, review_has_findings
# The bounded size of the review/fix loop (review_rounds.MAX_REVIEW_ROUNDS,
# the same value the reported round caps at — Issue #1779).
MAX_BASE_ADVANCE_ROUNDS = 5


def _read_pushed(worktree: Path, key: str) -> str | None:
    """One recorded push-history field, or None when unusable.

    The merge record reads these AFTER the merge landed: a missing or
    corrupt record must degrade the metric to `unknown`, never fail a
    landed delivery and never fabricate a count.
    """
    try:
        state = run_state.read_run_state(worktree)
    except ValueError:
        return None
    if not state:
        return None
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def read_pushed_head(worktree: Path) -> str | None:
    """The recorded last engine-pushed head, or None when unusable."""
    return _read_pushed(worktree, "pushed_head")


def read_pushed_base(worktree: Path) -> str | None:
    """The recorded engine push-line base, or None when absent.

    None means the engine's first push created the branch, so the
    merge record's engine interval starts at the merge's base parent.
    """
    return _read_pushed(worktree, "pushed_base")


def freeze_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    *,
    external_pr_url: str | None = None,
    source_repo: str | None = None,
) -> dict:
    """Freeze the exact base/head SHA of the one open PR for a task branch."""
    pr = run_state._single_open_pr(
        worktree, branch, base_branch, scene="freeze_pr",
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    return {
        "number": pr["number"],
        "url": pr["url"],
        "base_ref": pr.get("baseRefName"),
        "base_oid": pr["baseRefOid"],
        "head_ref": pr["headRefName"],
        "head_oid": pr["headRefOid"],
    }


def log_recovery_ci_status(pr: dict, repo: str) -> None:
    """Record the recovered PR's current check status without check output.

    This is observability only: a GitHub status lookup must never decide
    whether the recovered review runs.
    """
    try:
        checks = commit_check_runs(repo, pr["head_oid"])
        summary = [
            f"{check.get('name', '?')}={check.get('status', '?')}/"
            f"{check.get('conclusion', '?')}"
            for check in checks if isinstance(check, dict)
        ]
    except Exception as exc:
        event(
            "review_recovery_ci_status_failed", level=logging.WARNING,
            pr=pr.get("number", "?"), error=str(exc),
        )
        return
    event(
        "review_recovery_ci_status", pr=pr["number"],
        checks=",".join(summary) or "none",
    )


def _round_scene_block(resumed: dict, pr_url: str, round: int,
                       *, base_advance_round: int | None = None,
                       verdict_head_unknown_round: int | None = None) -> str:
    """The updated scene block a completed round carries to the next resume.

    The round comment is the budget's write path: embedding
    the scene with `review_round=round` makes THAT comment the latest
    scene, so the next tick's `resume_scene` reads the advanced count —
    GitHub stays the only state store, no second record exists.
    """
    return scene.render(scene.Scene(
        run_id=validate_run_id(resumed["run_id"]),
        base_branch=resumed["base_branch"],
        base_sha=resumed["base_sha"],
        pr_url=pr_url,
        external=resumed.get("external", ""),
        review_round=round,
        base_advance_round=(
            resumed.get("base_advance_round", 0)
            if base_advance_round is None else base_advance_round
        ),
        verdict_head_unknown_round=(
            resumed.get("verdict_head_unknown_round", 0)
            if verdict_head_unknown_round is None
            else verdict_head_unknown_round
        ),
    ))


def review_and_merge_if_clean(worktree: Path, branch: str, base_branch: str,
                              config: config_domain.RunnerConfig, source_repo: str,
                              number: int, title: str, priority: str,
                              *, scene: dict,
                              previous_comments: list[dict] | None = None,
                              merge_only: bool = False,
                              checks: list | None = None,
                              review_record: tuple[str, str] | None = None,
                              issue_body_sha256: str | None = None) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    Issue #1626: the review record (reviewed_head plus the hash of the
    Issue body the session rendered) authorizes exactly one later
    merge-only tick. Every exit drops it except a deferral (pending CI,
    UNKNOWN mergeability, or a head pushed in this tick): findings, a
    malformed verdict, an exception, a gate failure, the ai-blocked skip
    and the completed merge all clear it, so a stale pass can never
    authorize a later merge.
    """
    keep_review_record: list[bool] = []
    try:
        return _review_and_merge_round(
            worktree, branch, base_branch, config, source_repo, number,
            title, priority, scene=scene,
            previous_comments=previous_comments, merge_only=merge_only,
            checks=checks, review_record=review_record,
            issue_body_sha256=issue_body_sha256,
            keep_review_record=keep_review_record,
        )
    finally:
        if not keep_review_record:
            run_state.clear_reviewed(worktree)


def _review_and_merge_round(worktree: Path, branch: str, base_branch: str,
                            config: config_domain.RunnerConfig, source_repo: str,
                            number: int, title: str, priority: str,
                            *, scene: dict,
                            previous_comments: list[dict] | None = None,
                            merge_only: bool = False,
                            checks: list | None = None,
                            review_record: tuple[str, str] | None = None,
                            issue_body_sha256: str | None = None,
                            keep_review_record: list[bool]) -> bool:
    """Run one independent review round; merge when the verdict is clean.

    `title` is the issue's GitHub title: the review
    progress scenes (ensure, findings, merged) show `#<number> <title>`
    like every other scene; it is required, never fabricated.

    `scene` is the delivery's recovered resume scene (run_id, base,
    PR URL, `review_round`, `scene_at`): the round budget reads
    `review_rounds.effective_review_round` of it — the scene's count with
    a maintainer recovery after the scene reset (Issue #483) — so the
    budget, the round in progress and every failure report show one
    number (Issue #1779). The scene is the waiting primitive's state
    anchor, and every round comment carries the updated scene block, so
    the next resume continues the count.

    The delivery step calls this while the PR is open and the Issue
    awaits review (`ai-pr-opened`) or awaits the next review session
    (`ai-fix-needed`). It freezes the PR, runs the independent review
    (streamed, role=review), and then:

    - clean verdict -> the reviewer may have fixed findings IN THE SAME
      SESSION and pushed the task branch, so the PR is
      RE-FROZEN before the merge gate: the gate (latest-base ancestor,
      one-shot CI read, mergeable, head match,
      `gh pr merge --match-head-commit`) then runs against the head the
      verdict actually covers; confirm the merge landed on
      origin/<base>, sync the deployment checkout, label the Issue
      `ai-merged`; returns True. An EXTERNAL takeover (the scene marks
      `external`) NEVER merges: its clean verdict ends at the triage
      stop below — the review conclusion stays on the Issue and the
      ticket is labeled `ai-blocked` for the maintainer's acceptance
      decision (Issue #842, decision D2); returns False;
    - an intermediate gate state (CI still pending on the reviewed head,
      or mergeability still UNKNOWN) -> one journal line and return
      without a comment or a label change: "pending" is a
      state, not a failure — the next tick re-reads it, the round budget
      does not advance; returns False;
    - `blocked_on_human_decision` -> raise `HumanDecisionRequired` with
      the finding note and fix direction; the caller marks the Issue
      `ai-blocked` immediately, without recording another review round;
    - Blocker/Major findings the reviewer could not fix in-session ->
      comment them to Issue and PR (the comment carries the updated
      scene) and label the Issue `ai-fix-needed`; the next tick resumes
      the same PR with the next round — no cold-start fixer, no third
      review; returns False;
    - a gate failure because the head is behind the latest base, has a
      merge conflict, or its CI is red -> label the Issue
      `ai-fix-needed` with the finding (the next review session absorbs
      the latest base in-session or repairs the red CI); returns False;
    - missing/malformed verdict -> raise; the caller keeps the Issue in
      the automatic fix loop (`ai-fix-needed`). A mismatched head is
      recoverable when it names another real commit, and an unknown
      object gets two retries per review run before becoming terminal
      (`ai-blocked`);
    - an exhausted round budget -> raise `UnrecoverableDeliveryError`
      (the bounded loop is a human decision, not a
      recoverable failure); the caller marks the Issue `ai-blocked`
      with the explicit reason.
    """
    marker = run_marker(config.run_id)
    # The queue entry the claim scanned: the repository's resolved
    # `dispatch_label` (default `ai-ready`), never a second constant
    # (Issue #1775).
    ready_label = config.dispatch_label or READY_LABEL
    # The round budget lives in the scene: `review_round` counts the
    # COMPLETED rounds recorded by the round comments' scene blocks, with
    # a maintainer's blocked -> ai-fix-needed recovery reset applied — ONE
    # decision owned by `review_rounds` (Issue #1779): the budget, the
    # round in progress and failure reports share it (Issue #1783).
    scene_round = int(scene["review_round"])
    rounds = effective_review_round(scene, number=number, repo=source_repo)
    recovered = rounds != scene_round
    base_advance_rounds = (
        0 if recovered else int(scene.get("base_advance_round", 0))
    )
    if rounds >= MAX_REVIEW_ROUNDS and not merge_only:
        event(
            "review_rounds_exhausted", level=logging.ERROR,
            issue=number, rounds=rounds,
            terminal="expected_human_decision",
        )
        # The loop is bounded by MAX_REVIEW_ROUNDS on purpose
        # — after 5 rounds without a clean verdict the remaining findings
        # need a human decision, so the AI cannot safely continue this PR.
        raise ReviewRoundsExhausted(
            f"review/fix loop exhausted after {MAX_REVIEW_ROUNDS} rounds "
            "without a clean verdict; the bounded loop is a human "
            "decision, so the AI cannot safely continue this PR"
        )
    round = rounds if merge_only else rounds + 1
    scene["review_round_running"] = round  # the failure report's source
    external_pr_url = scene["pr_url"] if scene.get("external") else None
    pr = freeze_pr(
        worktree, branch, base_branch,
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    # Issue #1626: a record whose reviewed head and rendered-Issue-body hash
    # both still match authorizes THIS tick to merge without a session.
    merge_only_from_record = (
        review_record is not None
        and not merge_only
        and not scene.get("external")
        and issue_body_sha256 == review_record[1]
        and pr["head_oid"] == review_record[0]
    )
    # Issue #877: the absorb contract is armed only when GitHub reports the
    # PR CONFLICTING at round start (one mergeable read, Issue #1626). A
    # head that is merely behind is absorbed by the Runner's gate, not by
    # the session; UNKNOWN does not arm, and an unreadable read leaves the
    # round unarmed (the gate's own fresh-fetch ancestor check still guards
    # the merge).
    absorb_required = False
    if not merge_only and not merge_only_from_record:
        try:
            absorb_required = pr_view(
                pr["number"], "mergeable", cwd=worktree,
            ).get("mergeable") == "CONFLICTING"
        except (subprocess.CalledProcessError, ValueError, TypeError):
            absorb_required = False
    # A fix push whose round ended before the re-freeze record (a
    # findings verdict, or a malformed verdict head) still left an
    # engine-pushed head on the remote (Issue #833). When the frozen
    # remote head is exactly this worktree's checked-out head, only
    # this worktree's own sessions push it — adopt it into the push
    # history. The adoption is internal-only: an external takeover's
    # worktree legitimately starts on the contributor's foreign head,
    # whose base the claim recorded instead; an external push leaves
    # the local head behind and is never adopted. The recorded-head
    # check runs first: the common already-recorded round never pays a
    # git call.
    if (not scene.get("external")
            and read_pushed_head(worktree) != pr["head_oid"]
            and pr["head_oid"] == run_command(
                ["git", "rev-parse", "HEAD"], cwd=worktree)):
        run_state.record_pushed_head(worktree, pr["head_oid"])
        event(
            "pushed_head_recorded", pr=pr["number"],
            head=pr["head_oid"], round=round,
        )
    if recovered:
        # Only the explicit recovery reset reaches this branch. Check the
        # latest PR CI before spending the newly granted review budget.
        log_recovery_ci_status(pr, source_repo)
    publisher = ProgressPublisher(
        number, source_repo, config.run_id, run_command=run_command,
        language_code=language.of(config), status_url=config.status_url,
    )
    publish = functools.partial(
        safe_publish, run_id=config.run_id, issue=number,
        source_repo=source_repo, role=ROLE_REVIEW,
    )
    started = time.monotonic()
    # The whole review/merge phase PATCHes the run's live progress
    # comment, so the publisher above carries the bound message language
    # too (#1660/#1687): the header and field labels follow it exactly
    # like the implementation phase's.
    ctx = RunContext(
        run_id=config.run_id, issue=number, branch=branch,
        worktree=worktree, source_repo=source_repo,
    )
    # The step's own updates (this ensure, the merge gate's `checks`
    # stage, the findings/merged finishes) render through one call shape
    # (Issue #1657). Publishing is a bypass — a 404 here must not stop the
    # review (the delivery is already open and awaiting review; the
    # journal is the record, the progress comment is observability).
    round_update = step_publisher(
        publisher, ctx, role=ROLE_REVIEW, title=title,
        started=started, priority=priority, checks=checks,
    )
    publish(action=lambda: round_update(pr["url"], round))
    if merge_only or merge_only_from_record:
        verdict = {
            "verdict": "pass", "head": pr["head_oid"],
            "findings": [], "blockers": 0, "majors": 0,
        }
        review_summary = (
            language.text(
                language.of(config), "review.summary_human_action",
            ) if merge_only else language.text(
                language.of(config), "review.summary_pass_recorded",
                head=pr["head_oid"],
            )
        )
        reviewed_body_sha256 = None
    else:
        # Issue #1626: a record must never outlive the session that
        # replaces it, even if the process dies mid-session.
        run_state.clear_reviewed(worktree)
        result = pi_session.run_review(
            ctx, pr, config, round,
            progress=LiveProgressThrottle(
                ctx, publisher, title=title, role=ROLE_REVIEW,
                started=started, pr_url=pr["url"], review_round=round,
                priority=priority, checks=checks,
            ),
        )
        # Drop any record the session wrote: only the Runner's own record
        # (written below on a pass) may authorize a later merge.
        run_state.clear_reviewed(worktree)
        output = result.output
        reviewed_body_sha256 = result.issue_body_sha256
        verdict = parse_review_verdict(output)
        fixed_findings = len(verdict["findings"])
        review_summary = (
            language.text(language.of(config), "review.summary_pass_clean")
            if fixed_findings == 0
            else language.text(
                language.of(config), "review.summary_pass_fixed",
                findings=fixed_findings,
            )
        )
        event(
            "review", pr=pr["number"], round=round,
            verdict=verdict["verdict"], blockers=verdict["blockers"],
            majors=verdict["majors"],
        )
    if verdict["verdict"] == "blocked_on_human_decision":
        decisions = "; ".join(
            f"note: {finding.get('note', '')}; "
            f"fix: {finding.get('fix', '')}"
            for finding in verdict["findings"]
        )
        actions = "; ".join(
            str(finding.get("fix", "")).strip()
            for finding in verdict["findings"]
            if str(finding.get("fix", "")).strip()
        )
        # No fix: the handler supplies the catalog action (Issue #1701).
        raise HumanDecisionRequired(
            "review requires human decision: " + decisions,
            action=actions,
        )
    if review_has_findings(verdict):
        # The reviewer could not make the PR mergeable in this session
        # (findings are fixed in the same session; reaching
        # this branch means the fix was not verifiable or not this
        # session's to decide). The Issue moves to the explicit
        # fix-needed state: the next review session retries the same PR
        # (no cold-start fixer), and the round budget bounds the loop.
        event(
            "review_findings_unfixed", pr=pr["number"], round=round,
        )
        body = review_round_comment_body(
            marker, round, pr["number"], verdict["blockers"],
            verdict["majors"], verdict["findings"],
            _round_scene_block(
                scene, pr["url"], round,
                verdict_head_unknown_round=0,
            ),
            repo=source_repo, previous_comments=previous_comments,
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        )
        comment_issue(number, repo=source_repo, body=body)
        failure_report.comment_pr(pr["number"], repo=source_repo, body=body)
        # The findings publishing is bypass — a 404 here
        # must not stop the `ai-fix-needed` transition below (the next
        # review session retries the same PR either way).
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "review.findings_milestone",
                    round=round, blockers=verdict["blockers"],
                    majors=verdict["majors"], pr_number=pr["number"],
                )
            ),
        )
        publish(action=lambda: round_update(
            pr["url"], round, outcome=language.text(
                language.of(config), "review.findings_outcome",
                round=round, blockers=verdict["blockers"],
                majors=verdict["majors"],
            ),
        ))
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=issue_labels(number, source_repo),
        )
        return False
    # The reviewer fixes findings in the same session and
    # pushes the task branch, so the head the verdict covers may be
    # NEWER than the frozen head. Re-freeze before the merge gate: the
    # gate then checks the latest-base ancestor, mergeability and the
    # exact reviewed head against the current remote head, and merges
    # only that head via --match-head-commit.
    refrozen = freeze_pr(
        worktree, branch, base_branch,
        external_pr_url=external_pr_url, source_repo=source_repo,
    )
    if refrozen["head_oid"] != pr["head_oid"]:
        event(
            "review_head_advanced", pr=pr["number"], round=round,
            frozen=pr["head_oid"], reviewed=refrozen["head_oid"],
        )
    # The clean verdict is bound to the head it covers. The
    # gate below merges exactly `refrozen["head_oid"]`, so a verdict
    # naming any other head (forged by injected text, replayed from an
    # older round, or stale after a fix the reviewer forgot to state)
    # never merges: it is a malformed verdict. The object probe below
    # distinguishes a recoverable branch race from a terminal unknown head.
    if verdict["head"] != refrozen["head_oid"]:
        # A real alternate commit means the branch moved during review and
        # remains recoverable. An object that is not a commit cannot be a
        # race: it is a malformed/model-invented verdict and retrying the
        # same review would only reproduce the dead loop (Issue #988).
        object_probe = run_command(
            ["git", "cat-file", "-e", f"{verdict['head']}^{{commit}}"],
            cwd=worktree, check=False, timeout=10,
        )
        if object_probe.returncode != 0:
            unknown_round = (
                int(scene.get("verdict_head_unknown_round", 0)) + 1
            )
            scene["verdict_head_unknown_round"] = unknown_round
            event(
                "review_verdict_head_unknown", level=logging.WARNING,
                pr=pr["number"], verdict_head=verdict["head"],
                round=unknown_round,
            )
            if unknown_round >= 3:
                raise UnrecoverableDeliveryError(
                    f"review verdict points to unknown object {verdict['head']} "
                    f"on attempt {unknown_round}; the verdict head is not a "
                    "commit in the delivery repository; human intervention "
                    "is required"
                )
            raise ValueError(
                f"review verdict points to unknown object {verdict['head']} "
                f"(unknown-head attempt {unknown_round}/3); retrying review"
            )
        raise ValueError(
            f"review verdict head {verdict['head']} does not match the "
            f"PR head {refrozen['head_oid']}; the merge gate only merges "
            "the head the verdict covers"
        )
    if refrozen["head_oid"] != pr["head_oid"]:
        # The advanced head is verdict-covered (checked above), so it
        # is this round's own review/fix output — an engine-pushed
        # head (Issue #833): the merge record's external_commits must
        # not count the session's own fixes as external commits.
        run_state.record_pushed_head(worktree, refrozen["head_oid"])
    if (not scene.get("external")
            and not merge_only and not merge_only_from_record
            and refrozen["head_oid"] != pr["head_oid"]):
        # Issue #1626: a head pushed in this tick is never merged in this
        # tick — its CI has not registered yet. Record the pass and defer;
        # the next tick with green CI on that head merges without a session.
        run_state.record_reviewed(
            worktree, refrozen["head_oid"], reviewed_body_sha256,
        )
        keep_review_record.append(True)
        event(
            "review_merge_deferred", pr=refrozen["number"], round=round,
            reason=(
                f"the review session pushed head {refrozen['head_oid']}; "
                "its CI is not read in this tick"
            ),
        )
        return False
    if scene.get("external"):
        # D2 (Issue #842): an external contribution's clean verdict is
        # the engine's FINAL output — the PR is NEVER auto-merged and
        # no Milestone is ever written. The review conclusion stays on
        # the Issue and the ticket stops at `ai-blocked` (the human
        # decision point): code quality is the engine's judgment, but
        # accepting the contribution and picking its version is the
        # maintainer's.
        state, rollup = pr_delivery_rollup(pr["url"], source_repo)
        checks = ", ".join(check_summaries(rollup)) or "none"
        bound = language.of(config)
        body = (
            f"{marker}\n"
            + language.text(
                bound, "review.external_pr_round", round=round,
                pr_number=pr["number"], verdict=verdict["verdict"],
                blockers=verdict["blockers"], majors=verdict["majors"],
                minors=verdict["minors"], state=state, checks=checks,
            ) + "\n"
            + user_summary.summary_text(
                status_url=config.status_url, issue=number, title=title,
                language_code=bound,
            ) + "\n"
            + language.text(bound, "review.review_report_open")
            + "````\n" + output.rstrip("\n") + "\n````\n\n</details>\n\n"
            + language.text(
                bound, "review.external_conclusion", run_id=config.run_id,
            )
        )
        post_blocked_comment(
            number, repo=source_repo, body=body,
            current_labels=issue_labels(number, source_repo),
            language_code=bound, ready_label=ready_label,
        )
        event(
            "external_takeover_triage", pr=pr["number"], round=round,
            verdict=verdict["verdict"],
        )
        return False
    def handle_gate_failure(message: str, *, ci_failure: bool,
                            absorb_abandoned: bool = False) -> None:
        # Issue #877: the machine-checked absorb violation rides the same
        # counted round comment, so the next session and the human both see
        # that the round neither merged the base nor reported the
        # abandonment — the gate now names the silent abandon instead of
        # showing only the final behind/conflict state.
        violation = ""
        if absorb_abandoned:
            violation = (
                language.text(
                    language.of(config), "review.absorb_violation",
                    base_branch=base_branch,
                )
            )
        next_base_advance_round = base_advance_rounds + (0 if ci_failure else 1)
        base_advance_exhausted = (
            not ci_failure and next_base_advance_round > MAX_BASE_ADVANCE_ROUNDS
        )
        if ci_failure:
            gate_messages = [
                language.text(
                    language.of(config), "review.gate_ci_blocked",
                    message=message, run_id=config.run_id,
                ),
                language.text(
                    language.of(config), "review.gate_next_step",
                    base_branch=base_branch,
                ),
            ]
        else:
            gate_messages = [
                language.text(
                    language.of(config), "review.gate_blocked",
                    message=message, run_id=config.run_id,
                ),
                (language.text(
                    language.of(config), "review.gate_exhausted",
                ) if base_advance_exhausted else language.text(
                    language.of(config), "review.gate_next_step",
                    base_branch=base_branch,
                )),
            ]
        if violation:
            gate_messages.append(violation.strip())
        body = review_round_comment_body(
            marker, round, pr["number"], 0, 0, [],
            _round_scene_block(
                scene, pr["url"],
                round if ci_failure else int(scene["review_round"]),
                base_advance_round=(
                    scene.get("base_advance_round", 0)
                    if ci_failure else min(
                        next_base_advance_round, MAX_BASE_ADVANCE_ROUNDS,
                    )
                ),
                verdict_head_unknown_round=0,
            ),
            messages=gate_messages, repo=source_repo,
            heading=(None if ci_failure else language.text(
                language.of(config), "review.base_advance_heading",
                round=next_base_advance_round, pr_number=pr["number"],
            )),
            # A base-advance retry reuses the SAME round scene: it is a
            # review failure, not a completed round, so it must not
            # consume a round record (Issue #1659).
            kind=(COMMENT_KIND_REVIEW_ROUND if ci_failure
                  else COMMENT_KIND_FAILURE),
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        )
        # CI evidence is best-effort observability.  A GitHub comment
        # outage must not prevent the required ai-fix-needed transition.
        try:
            comment_issue(number, repo=source_repo, body=body)
            failure_report.comment_pr(pr["number"], repo=source_repo, body=body)
        except Exception:
            LOGGER.exception(
                "delivery_ci_evidence_publish_failed pr=%s run_id=%s",
                pr["number"], config.run_id,
            )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=issue_labels(number, source_repo),
        )
        if base_advance_exhausted:
            event(
                "base_advance_rounds_exhausted", level=logging.ERROR,
                issue=number, rounds=next_base_advance_round,
                terminal="expected_human_decision",
            )
            raise UnrecoverableDeliveryError(
                "base-advance retry loop exhausted after "
                f"{MAX_BASE_ADVANCE_ROUNDS} base advances; the bounded loop "
                "is a human decision, so the AI cannot safely continue this PR"
            )

    # Issue #1626: record the pass before the gate, so the gate's own base
    # absorb can point the record at the pushed head and defer.
    if not merge_only and not merge_only_from_record:
        run_state.record_reviewed(
            worktree, refrozen["head_oid"], reviewed_body_sha256,
        )
    # The Runner has entered the merge gate's CI-check action on the
    # reviewed head: the one live comment reports that stage before the
    # gate waits for CI and merges (Issue #1657). Best effort like every
    # progress update - a publication failure cannot affect the merge.
    publish(action=lambda: round_update(
        pr["url"], round, step="checks",
    ))
    try:
        merged = merge_exec.merge_gate(
            worktree,
            {**refrozen, "_source_repo": source_repo},
            base_branch, repo_dir=config.repo_dir,
            issue_number=number, run_id=config.run_id,
            language_code=language.of(config),
        )
    except MergeHandoffRequired as exc:
        # A known policy blocker is a successful, resumable handoff.
        return handle_merge_handoff(
            number=number, repo=source_repo, pr_number=refrozen["number"],
            head=refrozen["head_oid"], preflight=exc.preflight, marker=marker,
            language_code=language.of(config), ready_label=ready_label,
            status_url=config.status_url, title=title,
        )
    except DeliveryDeferred as exc:
        # A pending check or an UNKNOWN mergeability on the
        # reviewed head is an intermediate state, not a failure — the
        # next tick re-reads it. No comment, no label change, no round
        # consumed: the scene's counter only advances on round comments.
        event(
            "review_merge_deferred", pr=refrozen["number"], round=round,
            reason=str(exc),
        )
        keep_review_record.append(True)
        return False
    except MergeBlockedByIssueLabel as exc:
        # Issue #1504: a maintainer labelled the source Issue `ai-blocked`
        # while the PR was in review. The gate already posted the skip
        # comment; the labels stay as the human set them and the PR stays
        # open (no `ai-merged`, no `ai-fix-needed`). The Issue is terminal
        # for the engine, so no next tick re-claims it.
        event(
            "review_merge_skipped_issue_blocked",
            pr=refrozen["number"], round=round, reason=str(exc),
        )
        return False
    except RecoverableMergeGateError as exc:
        # Issue #877 machine check: an armed round whose verdict was `pass`
        # and whose head STILL lacks the latest base (re-read against the
        # gate's fresh fetch) neither absorbed nor reported findings — log
        # the structured fact and name the violation in the round comment.
        absorb_abandoned = False
        if absorb_required:
            try:
                absorb_abandoned = not is_ancestor(
                    f"origin/{base_branch}", refrozen["head_oid"], cwd=worktree,
                )
            except subprocess.CalledProcessError:
                absorb_abandoned = False
        if absorb_abandoned:
            event(
                "review_absorb_abandoned", level=logging.ERROR,
                pr=pr["number"], round=round, head=refrozen["head_oid"],
                base_branch=base_branch,
            )
        handle_gate_failure(str(exc), ci_failure=False,
                            absorb_abandoned=absorb_abandoned)
        return False
    except GateCIFailure as exc:
        # Issue #906: the recoverable CI failure is identified by TYPE,
        # never by matching text inside the message — rewording a gate
        # message cannot change which recovery path runs. `GateCIFailure`
        # is a sibling of `PreExistingCIFailure` and
        # `RecoverableMergeGateError`, so an already-red main and a
        # behind-base/conflict head propagate untouched and fail fast.
        handle_gate_failure(str(exc), ci_failure=True)
        return False
    # Issue #1802: use the gate's confirmation; a success reads it here.
    confirmed = merged.get("confirmation") or merge_exec.confirm_merged(
        worktree, merged, base_branch, repo_dir=config.repo_dir,
    )
    # The merged-as-is fact (Issue #833): every count failure degrades
    # to `unknown` inside merge_commit_metrics — a landed merge is
    # never re-failed by its own record.
    external_commits, pr_commits = merge_exec.merge_commit_metrics(
        worktree, confirmed["merge_commit"], read_pushed_head(worktree),
        read_pushed_base(worktree),
        merge_method=merged.get("merge_method", "--merge"),
    )
    # The merged publishing is bypass — the GitHub merge already landed; a
    # 404 here must not stop `ai-merged` and the merged record below.
    publish(
        action=lambda: publisher.milestone(
            f"merged: {merged['url']} "
            f"(merge_commit={confirmed['merge_commit']} "
            f"review_rounds={round} "
            f"external_commits={external_commits} "
            f"commits={pr_commits})"
        ),
    )
    publish(action=lambda: round_update(
        merged["url"], round, step="merge", review=review_summary,
        outcome=language.text(
            language.of(config), "review.delivered_outcome",
            pr_url=merged["url"],
            merge_commit=confirmed["merge_commit"], round=round,
        ),
    ))
    # The GitHub merge already landed. Read the labels after it (a resumed
    # fix round may carry `ai-in-progress` and `ai-fix-needed`), record
    # `ai-merged` — clearing every state label present — before touching the
    # systemd checkout: a checkout that cannot fast-forward is runner ops.
    apply_label_patch(
        number, repo=source_repo, event=EVENT_MERGED,
        current_labels=issue_labels(number, source_repo),
        ready_label=ready_label,
    )
    # Issue #1825: the merged outcome also edits the paused delivery's ONE
    # base-branch-red comment in place (to `merged`).
    base_ci_red.post_merged(
        number, source_repo, merged_pr_comment_body(
            config.run_id, merged["url"], confirmed["merge_commit"], round,
            external_commits, pr_commits, base_branch, review_summary,
            language_code=language.of(config), status_url=config.status_url,
            issue=number, title=title,
        ),
        language_code=language.of(config))

    if confirmed["state"] != "MERGED":  # Issue #1801: nothing else closes it
        run_state.close_issue_after_landed_merge(
            number, repo=source_repo, merge_commit=confirmed["merge_commit"])
    try:
        # A config built by config_domain.load_config always carries both paths (the
        # deploy home defaults to the repo dir). A split layout does not
        # use the delivery checkout for the next tick; same-checkout
        # configs retain the existing engine-channel guard below.
        if (
            # ``Path(".")`` is the placeholder on hand-built partial
            # configs; only config_domain.load_config's resolved path represents an
            # explicitly configured split deployment.
            config.deploy_home != Path(".")
            and config.repo_dir != config.deploy_home
        ):
            # The delivery checkout is not the engine source in a split
            # deployment layout. The next tick loads code from deploy_home,
            # so syncing this delivery checkout has no runtime effect.
            event(
                "base_checkout_sync_skipped", repo_dir=config.repo_dir,
                base_branch=base_branch,
                reason="repo_dir_is_not_deploy_home",
            )
        elif (
            config.engine_source_track is not None
            and config.deploy_home is not None
            and config.repo_dir == config.deploy_home
            and config.engine_source_track != "main"
        ):
            # The delivery checkout IS the engine source in
            # the dogfood layout, and the engine channel is locked (or
            # tracks a non-main branch) — fast-forwarding it to
            # origin/<base_branch> would break the lock. The next tick's
            # ExecStartPre engine sync owns this checkout instead.
            event(
                "base_checkout_sync_skipped", repo_dir=config.repo_dir,
                engine_source_track=config.engine_source_track,
                base_branch=base_branch,
            )
        else:
            merge_exec.sync_base_checkout(config.repo_dir, base_branch)
    except RuntimeError:
        LOGGER.exception(
            "base_checkout_sync_failed after merge pr=%s repo_dir=%s; "
            "the delivery already landed on origin/%s",
            merged["url"], config.repo_dir, base_branch,
        )
    return True


def _run_review_round(
    pr_url: str, issue: dict, config: config_domain.RunnerConfig, source_repo: str,
    *, checks: list | None = None,
) -> bool | None:
    """Run ONE review round of an open-PR delivery.

    The delivery step's round body: read the delivery labels ONCE per
    round, repair a lost `ai-in-progress` transition, gate on the
    resumable opened-PR states, then recover the trusted scene,
    validate the frozen base, derive the worktree/branch, run the
    independent review and classify any failure.

    Returns True when this round merged the PR (terminal success);
    False when the delivery stays open for a LATER TICK (`ai-fix-needed`
    after findings or a failed gate, or a deferred merge — the label
    transition happens inside the review itself, a defer writes none);
    and None when a terminal state was already handled and the caller
    must release the slot and return: an unrecoverable precondition
    (`ai-blocked`), a recoverable failure's full `ai-fix-needed` scene
    (the next tick resumes the same run, branch, worktree and PR), a
    failed `ai-in-progress` label repair, or an open PR without a
    resumable delivery label (both `ai-blocked`).
    """
    number = int(issue["number"])
    title = issue["title"]
    run_id = current_run_id()
    marker = run_marker(run_id) if run_id else ""
    priority = issue_priority(issue)
    ready_label = config.dispatch_label or READY_LABEL

    def block_label_inconsistency(labels: list[str], reason: str) -> None:
        event(
            "delivery_label_inconsistent", level=logging.ERROR,
            issue=number, pr=pr_url, reason=reason,
        )
        body = language.text(
            language.of(config), "review.label_repair_failed",
            pr_url=pr_url, reason=reason,
        )
        if marker:
            body = f"{marker}\n{body}"
        # The blocked scene carries the readable summary before the
        # implementation diagnostics (Issue #1826).
        body = user_summary.append_summary(
            body, status_url=config.status_url, issue=number, title=title,
            language_code=language.of(config),
        )
        post_blocked_comment(
            number, repo=source_repo, body=body,
            current_labels=labels, language_code=language.of(config),
            ready_label=ready_label,
        )

    labels = issue_labels(number, source_repo)
    if IN_PROGRESS_LABEL in labels:
        try:
            apply_label_patch(
                number, repo=source_repo, event=EVENT_PR_OPENED,
                current_labels=labels,
            )
        except Exception as exc:
            LOGGER.exception(
                "issue=%s delivery_label_repair_failed pr=%s",
                number, pr_url,
            )
            block_label_inconsistency(labels, str(exc))
            return None
        labels = [label for label in labels if label != IN_PROGRESS_LABEL]
        labels.append(PR_OPENED_LABEL)
        event(
            "delivery_label_repaired", issue=number, pr=pr_url,
            **{"from": IN_PROGRESS_LABEL, "to": PR_OPENED_LABEL},
        )
    if not (is_resumable(labels)
            and not needs_human_intervention(labels)
            and MERGED_LABEL not in labels):
        block_label_inconsistency(
            labels,
            "open PR has no resumable delivery label",
        )
        return None
    # The human acceptance gate — checked BEFORE the scene,
    # the worktree or any review session. One label read decides; the
    # column-2 re-derivation is local evidence reads only. With the
    # gate on and no `ai-human-review` label, a non-empty column 2
    # holds the delivery: the waiting primitive returns the ticket to
    # `ai-ready` (the opened-PR anchor stays, so the resume scan keeps
    # finding it), the round ends here, the caller releases the slot
    # and every next tick costs one label read — never a review
    # session, never an `ai-fix-needed` round (the review-round budget
    # is not consumed by waiting). An empty column 2 needs no human and
    # falls through to the normal review; a missing worktree falls
    # through too so the existing recovery semantics stay intact.
    if config.human_review_gate and HUMAN_REVIEW_LABEL not in labels:
        gate_worktree = worktree_path(
            config.repo_dir, source_repo, number, run_id,
        )
        if gate_worktree.is_dir() and merge_exec._delivery_checklist(
            gate_worktree, config, test_status=test_status(checks),
        )["column2"]:
            # The waiting guard asks for the repository RESOLVED claim
            # label, never the host default (Issue #1775): in a repository
            # whose dispatch_label is a custom queue label the ticket
            # already carries it, so a second tick must not rewrite the
            # waiting patch.
            if ready_label not in labels:
                apply_label_patch(
                    number, repo=source_repo,
                    event=EVENT_HUMAN_REVIEW_WAITING,
                    current_labels=labels, ready_label=ready_label,
                )
            event(
                "human_review_waiting", issue=number, pr=pr_url,
            )
            return None
    # The PR is in an opened-PR review state: run the
    # independent review of the frozen PR on the same run
    # `ai-pr-opened` awaits review; `ai-fix-needed`
    # awaits the next review session after a finding or a base
    # conflict (the review session fixes findings in
    # the same session, so both states run the same review). A
    # clean verdict re-freezes the head, merges and returns
    # True (terminal); unfixed findings or a behind/conflict
    # gate label the Issue `ai-fix-needed` and the next
    # iteration re-runs the same independent review. A review
    # that cannot run is classified: a RECOVERABLE
    # failure (Pi execution failure, model wait, runner
    # exception, missing/malformed verdict, missing worktree,
    # unpushed local commit) keeps the Issue in the automatic
    # fix loop — `ai-fix-needed` with the full scene (run_id,
    # PR, branch, worktree, session, phase, last activity,
    # concrete error) on Issue AND PR, and the next timer
    # resumes the same run, branch, worktree and PR. Only an
    # explicit `UnrecoverableDeliveryError` (an external
    # precondition the AI cannot safely judge or fix: an
    # unrecoverable scene, a base-branch config change,
    # exhausted rounds) is terminal: the Issue is marked
    # `ai-blocked` ALONE (the opened-PR state label,
    # `ai-pr-opened` or `ai-fix-needed`, is removed) with the
    # explicit reason why automatic recovery is impossible.
    worktree = None
    branch = None
    scene = None
    try:
        try:
            comments = issue_comments(number, repo=source_repo)
            scene = run_state.resume_scene(comments, repo=source_repo)
        except ValueError as scene_exc:
            # Without the trusted scene the runner
            # cannot derive run_id, branch, worktree or PR and
            # cannot start a review session — an external
            # precondition the AI cannot fix by itself (the
            # same terminal state as the scan-time
            # `block_scene_failure`), so the handler below
            # marks the Issue ai-blocked with the explicit
            # reason.
            # The reason names the condition and the ONE missing
            # precondition; the resume step itself lives only in the
            # catalog (Issue #1724: a second copy here once offered the
            # reader a non-working alternative).
            raise UnrecoverableDeliveryError(
                f"the resume scene is unrecoverable "
                f"({scene_exc}); the runner cannot derive "
                "run_id, branch, worktree or PR without the "
                "trusted 'Orbi opened PR' comment, so "
                "it cannot start a review session; a human "
                "must restore the trusted 'Orbi opened PR' "
                "scene comment"
            ) from scene_exc
        # The scene freezes the base the PR
        # was opened against. The config may have moved on (or
        # the comment is stale): reviewing or merging a PR
        # frozen on another base against the configured one
        # would run the freeze/merge gate on the wrong base,
        # so fail fast before any git/Pi mutation instead of
        # silently switching bases. A base-branch change is a
        # human decision: the runner must not
        # auto-retry a PR frozen on another base, so the
        # handler below marks the Issue ai-blocked with the
        # explicit reason and both base values named.
        if scene["base_branch"] != config.base_branch:
            raise UnrecoverableDeliveryError(
                f"resume scene base_branch={scene['base_branch']} "
                f"differs from configured base_branch="
                f"{config.base_branch}; the PR is frozen on a "
                "different base and must not be reviewed or "
                "merged against the configured one — a base "
                "change is a human decision, so auto-retrying "
                "would keep failing on the same mismatch"
            )
        worktree = worktree_path(
            config.repo_dir, source_repo, number,
            scene["run_id"],
        )
        # The worktree is derived from the
        # configured repo_dir, source repo, Issue number and run id
        # (never read from a comment). A missing directory is a
        # RECOVERABLE failure: the branch still exists on the
        # remote and the worktree can be recreated (git worktree
        # add) on the next resume, so the handler below keeps the
        # Issue in the automatic fix loop (ai-fix-needed) with the
        # PR and branch preserved.
        if not worktree.is_dir():
            # The failure comment must carry the full scene
            # including the branch: the stable
            # derivation is the best available guess when the
            # worktree is gone.
            branch = task_branch(
                source_repo, number, scene["run_id"],
            )
            raise RuntimeError(f"worktree missing: {worktree}")
        # The delivery branch is a local git fact of the derived
        # worktree — the stable naming for the Runner's own
        # deliveries, the contributor's head branch for an
        # external takeover. Deriving it from the
        # worktree keeps the whole review/merge loop
        # branch-identity agnostic while the worktree path itself
        # stays comment-independent.
        branch = run_command(
            ["git", "branch", "--show-current"], cwd=worktree,
        ) or task_branch(source_repo, number, scene["run_id"])
        review_config = replace(
            config,
            base_sha=scene["base_sha"],
            run_id=scene["run_id"],
        )
        review_kwargs = {}
        if scene["review_round"] > 0:
            review_kwargs["previous_comments"] = comments
        if AWAITING_MERGE_LABEL in labels:
            review_kwargs["merge_only"] = True
        elif not scene.get("external"):
            # Issue #1626: a recorded pass on this head and this Issue
            # body lets this round merge without starting a session.
            review_record = run_state.read_reviewed(worktree)
            if review_record is not None:
                review_kwargs["review_record"] = review_record
                review_kwargs["issue_body_sha256"] = (
                    pi_session.rendered_issue_body_sha256(
                        config.prompt_review, issue.get("body") or "",
                    )
                )
        merged = review_and_merge_if_clean(
            worktree, branch, config.base_branch,
            review_config, source_repo, number,
            title=title, priority=priority, scene=scene,
            checks=checks,
            **review_kwargs,
        )
    except Exception as exc:
        detail = failure_detail(exc)
        if isinstance(exc, (ReviewRoundsExhausted, HumanDecisionRequired)):
            # These are intentional human decision points, not Runner
            # bugs. Keep structured evidence without a traceback.
            event(
                "review_human_decision_required"
                if isinstance(exc, HumanDecisionRequired)
                else "review_rounds_exhausted_expected_terminal",
                level=logging.ERROR, issue=number, pr=pr_url,
                reason=detail,
            )
        else:
            # Real delivery failures retain traceback evidence for
            # health monitoring and diagnosis.
            LOGGER.exception(
                "issue=%s delivery_review_failed pr=%s", number, pr_url,
            )
        # The blocked comment says why and how to resume (Issue #1701).
        language_code = language.of(config)
        text = functools.partial(language.text, language_code, pr_url=pr_url)
        action = ""
        if isinstance(exc, ReviewRoundsExhausted):
            reason, action = (text("review.blocked_rounds_reason"),
                              text("review.blocked_rounds_action"))
        elif isinstance(exc, HumanDecisionRequired):
            reason = text("review.blocked_human_reason")
            if exc.action:
                fix = exc.action.strip().rstrip(".。;")
                action = text("review.blocked_human_action", fix=fix)
        else:
            reason = text("review.blocked_failed_reason", detail=detail)
        if not action and failure_report.is_unrecoverable_failure(exc):
            action = text("review.blocked_generic_action")
        # The shared classified reporter: recoverable ->
        # `ai-fix-needed` with the full scene on Issue AND PR,
        # unrecoverable -> `ai-blocked` ALONE. No wrapper: a reporting
        # failure here fails the tick fast (the slot is released by
        # `main`'s `finally`), exactly like any other Runner bug.
        failure_report.report_delivery_failure(
            exc, issue=issue, source_repo=source_repo,
            run_id=run_id, pr_url=pr_url,
            worktree=worktree, branch=branch, role=ROLE_REVIEW,
            language_code=language_code, ready_label=ready_label,
            status_url=config.status_url,
            action=action,
            reason=reason,
            diagnosis=detail, evidence=True,
            review_round=(scene or {}).get("review_round_running"),
            review_scene_block=(
                _round_scene_block(scene, pr_url, int(scene["review_round"]))
                if isinstance(scene, dict)
                and scene.get("verdict_head_unknown_round", 0) else None
            ),
        )
        return None
    if merged:
        event(
            "delivery_auto_merged", issue=number, pr=pr_url,
        )
        return True
    return False
