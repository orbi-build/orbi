"""Orbi release execution: the deterministic release state machine.

`process_release` is the one entry point the Runner calls for an
`ai-release` Issue; it drives the ticket through the steps that never
enter the Pi delivery path — declaration resolution, the frozen base, the
pre-release gates, scope verification, the version commit, the CI-wait
gate on that commit, the tag, the GitHub Release, the `ai-merged`
closeout and the Milestone close — each step idempotent so a restart
resumes the same run from the top.

Extracted verbatim from the 1520-line `orbi.release` (Issue #1859): the
declaration/scope/gate domain stays there and the tag, publish, closeout
and Milestone steps live in `orbi.release_closeout`. This module imports
no `orbi.runner` — the runner dispatches here — and nothing imports back
into it.
"""
from __future__ import annotations

import functools
import logging
import re
import time
from typing import TYPE_CHECKING

from orbi import language, user_summary
from orbi.cli_source import refresh_cli_install
from orbi.comment_trust import trusted_comments
from orbi.delivery_labels import (
    EVENT_CLAIM,
    EVENT_RELEASE_WAITING,
    IN_PROGRESS_LABEL,
    READY_LABEL,
)
from orbi.delivery_scene import RunContext
from orbi.github import (
    apply_label_patch,
    comment_issue,
    has_in_progress_label,
    issue_comments,
    issue_priority,
)
from orbi.gitops import (
    create_release_worktree,
    freeze_base,
    latest_run_id,
    task_branch,
    worktree_path,
)
from orbi.journal import (
    event,
    new_run_id,
    run_command,
    set_active_run,
    set_run_id,
)
from orbi.progress import (
    ProgressPublisher,
    progress_body,
    progress_body_with_outcome,
    progress_state,
    run_marker,
    safe_publish,
)
from orbi.release import (
    ROLE_RELEASE,
    ReleaseDeliveriesWaiting,
    check_release_gates,
    derive_release_scope_from_milestone,
    release_target_milestone,
    resolve_release_declaration,
    verify_release_scope,
    verify_release_version_file,
)
from orbi.release_closeout import (
    ensure_release_tag_pushed,
    publish_release,
    release_failure_closeout,
    release_success_closeout,
    release_tag_commit,
    tag_commit_is_ancestor_of_base,
)
from orbi.release_declaration import parse_release_declaration
from orbi.release_git import ReleaseVersionAlreadyLanded, prepare_release_version
from orbi.release_notes import (
    build_release_changelog,
    release_changelog_scope,
    release_range_prs,
)

if TYPE_CHECKING:
    # Annotation-only: this module imports `orbi.runner` never — the
    # runner dispatches here — while `process_release` annotates the
    # frozen host config of #790.
    from orbi.config import RunnerConfig


def process_release(issue: dict, config: RunnerConfig,
                   source_repo: str) -> str:
    """Run the deterministic release state machine for one release Issue.

     A release task NEVER enters the normal `run_pi`
    development path: the Runner executes the state machine itself,
    step by step, each step idempotent so a restart resumes the same
    run (same run id, same worktree) from the top:

    1. Parse the optional `## Release` overrides from the Issue body.
       Without that section, the Issue Milestone supplies version and scope,
       while repository configuration supplies the base branch.
    2. Freeze the configured base — the release commit is exactly
       `origin/<base_branch>` (fetched under the base-sync lock).
       A declared version, when present, must match the Milestone title.
    2b. Prove the declared (or defaulted) `version_file` exists at the
       root of the frozen release tree (`verify_release_version_file`) — before any gate wait, so a declaration/repo
       mismatch fails fast at claim time with the supported files that
       do exist, never as a late FileNotFoundError after the gates.
    3. Enforce the pre-release gates (`check_release_gates`).
    4. When `scope_from_milestone` is declared, derive the scope from
       the Milestone (`derive_release_scope_from_milestone`): closed
       Issues + merged PRs; open items are surfaced as evidence, never
       released. Then verify the scope item by item
       (`verify_release_scope`).
    Before step 5, prepare the release version in the clean worktree using
       the declared `version_file` (`pyproject.toml` by default,
       `package.json`, or `none`), commit and push when metadata changes.
       The subsequent steps run against that commit.
    5. Test acceptance is the #268 CI-wait gate on the release commit:
       after the version bump the gates re-run against that exact
       commit, and a red or timed-out CI takes the terminal failure
       path: a spent wait budget is a human decision
       (`ReleaseGateWaitTimeout`), never a transient requeue
       (Issue #1760). No local test execution.
    6. Tag: the remote tag must not exist or must point EXACTLY at
       the release commit (a mismatch fails — an existing tag is
       never moved; one exception: a tag pointing at an ancestor of
       the frozen base resumes the release by rewinding the release
       commit to the tag); otherwise create an annotated tag at the
       release commit and push it with a plain push (never
       `--force`).
    7. Publish the GitHub Release (idempotent) with the full
       verification evidence.
    8. Apply `ai-merged` and close the release Issue (terminal delivery
       transition).
    9. Close the Milestone whose title is EXACTLY the released
        version, then write the success comment (release
        URL, tag, commit, evidence, Milestone evidence). Exact title
        match only; close it only when it has 0 open Issues;
        already-closed is idempotent. A missing Milestone or remaining
        open Issues fails fast. Any failure is `ai-blocked` ALONE, with
        a concrete failure comment, and the handled failure returns
        cleanly so the tick does not crash.
    """
    number = int(issue["number"])
    title = issue["title"]
    run_id = new_run_id()
    set_run_id(run_id)
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config.repo_dir, source_repo, number,
        )
        if existing_run_id is not None:
            run_id = existing_run_id
            set_run_id(run_id)
            event(
                "release_resuming_run", issue=number, run_id=run_id,
            )
    priority = issue_priority(issue)
    started = time.monotonic()
    # Bound before the try: the failure comment needs it even when the
    # declaration parse fails on the very first step.
    run_info = f"run_id={run_id} priority={priority}"
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
        language_code=language.of(config), status_url=config.status_url,
    )
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config.repo_dir, source_repo, number, run_id,
    )
    publish = functools.partial(
        safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_RELEASE,
    )

    ctx = RunContext(
        run_id=run_id, issue=number, branch=branch, worktree=worktree,
        source_repo=source_repo,
    )

    def progress() -> dict:
        return progress_state(
            ctx, title=title, role=ROLE_RELEASE, started=started,
            pr_url=None, review_round=0, priority=priority,
            activity={}, language_code=publisher.language_code,
        )

    def on_delivery_wait(detail: str) -> None:
        state = progress()
        state["phase"] = f"waiting deliveries: {detail}"
        publish(
            action=lambda: publisher.patch(progress_body_with_outcome(state)),
        )

    def on_ci_wait(detail: str) -> None:
        # The CI wait is reflected in the live progress
        # comment (pure bypass); the gate itself emits the
        # `release_waiting_ci` journal line.
        state = progress()
        state["phase"] = f"waiting CI: {detail}"
        publish(
            action=lambda: publisher.patch(progress_body_with_outcome(state)),
        )

    release_commit: str | None = None
    declaration: dict | None = None
    open_milestone_evidence: list[str] = []
    ready_label = config.dispatch_label or READY_LABEL
    try:
        declaration = parse_release_declaration(issue["body"])
        # Fused value: entry fallback + policy override (see
        # resolve_release_declaration — never re-read the raw entry).
        base_branch = declaration.get("base_branch") or config.base_branch
        # The started milestone below and the failure comment
        # read THIS value — base_branch known, base_sha not yet (the
        # post-gate reassignment further down adds base_sha). The journal
        # refactor deleted the assignment and both comments lost the only
        # field naming the frozen branch.
        run_info = f"base_branch={base_branch} run_id={run_id} priority={priority}"
        event("release_task", run_info, issue=number)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM,
            current_labels={label.get("name") for label in issue.get(
                "labels", []) if isinstance(label, dict)
                and isinstance(label.get("name"), str)},
        )
        set_active_run(ctx, title)
        publish(
            action=lambda: publisher.ensure(progress_body(progress())),
        )
        publish(
            action=lambda: publisher.milestone(
                language.text(language.of(config), "release.started",
                    run_info=run_info),
            ),
        )
        release_commit = freeze_base(config.repo_dir, base_branch)
        declaration = resolve_release_declaration(
            issue, declaration, config, source_repo, config.repo_dir,
            release_commit,
        )
        base_branch = declaration["base_branch"]
        run_info = f"base_branch={base_branch} base_sha={release_commit} run_id={run_id} priority={priority}"
        # The declared (or defaulted) version_file is proven
        # to exist in the frozen release tree BEFORE any gate wait — a
        # mismatch used to surface only at the version-write step, after
        # the gates and the scope verification, and burned the ticket
        # ai-blocked with a bare FileNotFoundError.
        verify_release_version_file(
            config.repo_dir, release_commit,
            declaration["version_file"],
        )
        wait_started: float | None = None
        # Waiting is persisted in the auditable Issue comment, so a later
        # tick can enforce one bounded waiting window without local state.
        for comment in trusted_comments(
                issue_comments(number, repo=source_repo), repo=source_repo,
        ):
            # Only the runner's trusted, structured waiting comments may
            # carry the persisted timer.  A public comment must not be able
            # to inject an old timestamp and turn a recoverable wait into an
            # immediate terminal block (the same trust boundary as resume
            # scenes).
            body = comment.get("body", "")
            # Language-independent anchor (Issue #1660): the persisted
            # timer field, never the translated prose.
            if "wait_started: " not in body:
                continue
            match = re.search(r"wait_started: ([0-9]+(?:\.[0-9]+)?)", body)
            if match:
                started_at = float(match.group(1))
                wait_started = (
                    started_at if wait_started is None
                    else min(wait_started, started_at)
                )
        release_waited_seconds = (
            max(0.0, time.time() - wait_started)
            if wait_started is not None else 0.0
        )
        run_info = (
            f"base_branch={base_branch} base_sha={release_commit} "
            f"run_id={run_id} priority={priority}"
        )
        # The leftover-delivery gate is scoped to the same
        # Milestone as the #663 completeness gate — the release Issue's own
        # GitHub Milestone, `active_milestone` fallback.
        target_milestone = release_target_milestone(
            issue, config.active_milestone,
        )
        gate_evidence, repo_has_ci = check_release_gates(
            source_repo, release_commit, number,
            milestone=target_milestone,
            # The gate waits out pending CI checks on the
            # release commit; the real load_config always provides the
            # key, the module constant stays the fallback for hand-built
            # configs.
            ci_wait_seconds=config.release_ci_wait_seconds,
            delivery_wait_seconds=config.release_deliveries_wait_seconds,
            delivery_waited_seconds=release_waited_seconds,
            on_wait=on_ci_wait,
            on_delivery_wait=on_delivery_wait,
        )
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "release.gates_passed",
                    evidence="; ".join(gate_evidence),
                ),
            ),
        )
        if declaration.get("scope_from_milestone") is not None:
            # The scope is derived from the Milestone, then
            # verified item by item exactly like a hand-listed scope.
            # The release Issue itself is exempt from the open-item
            # evidence (Issue #818): it is necessarily still open
            # here and closes only after the notes are published.
            derived_scope, open_milestone_evidence = (
                derive_release_scope_from_milestone(
                    source_repo, declaration["scope_from_milestone"],
                    release_issue=number,
                )
            )
            # An empty derived scope is allowed — a milestone with no
            # deliveries must not block the release (Issue #1384).
            declaration["scope"] = derived_scope
            if open_milestone_evidence:
                event(
                    "release_milestone_open_items", level=logging.WARNING,
                    issue=number, milestone=declaration["scope_from_milestone"],
                    open_items="; ".join(open_milestone_evidence),
                )
        scope_evidence = verify_release_scope(
            source_repo, declaration["scope"], config.repo_dir,
            release_commit,
        )
        if open_milestone_evidence:
            # Open items are NOT released; they are surfaced in the
            # auditable evidence instead of being silently swallowed.
            scope_evidence = scope_evidence + [
                f"NOT released (still open in milestone "
                f"{declaration['scope_from_milestone']}): {item}"
                for item in open_milestone_evidence
            ]
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "release.scope_verified",
                    evidence="; ".join(scope_evidence),
                ),
            ),
        )
        worktree = create_release_worktree(
            config.repo_dir, source_repo, number, run_id, release_commit,
        )
        # Version metadata is part of the release commit, not a post-release
        # fix: tests and the tag must identify the exact same commit.
        if declaration["version_file"] == "pyproject.toml":
            # Keep the default invocation compatible with existing callers;
            # the omitted field is the unchanged Python release path.
            release_commit = prepare_release_version(
                worktree, declaration["version"], base_branch,
                repo_dir=config.repo_dir,
            )
        else:
            release_commit = prepare_release_version(
                worktree, declaration["version"], base_branch,
                declaration["version_file"],
                repo_dir=config.repo_dir,
            )
        # Version preparation creates the commit that will be tagged. Re-run
        # the commit-specific gates so the recorded CI result and final
        # no-open-PR check cover that exact release commit, not the frozen
        # pre-version source commit. The just-pushed commit may hit the
        # CheckRun registration lag, so gate 1's observation (checks on the
        # frozen base = this repository runs CI) forbids the empty-list
        # pass here.
        gate_evidence, _ = check_release_gates(
            source_repo, release_commit, number,
            milestone=target_milestone,
            repo_has_ci=repo_has_ci,
            ci_wait_seconds=config.release_ci_wait_seconds,
            delivery_wait_seconds=config.release_deliveries_wait_seconds,
            delivery_waited_seconds=release_waited_seconds,
            on_wait=on_ci_wait,
            on_delivery_wait=on_delivery_wait,
        )
        # The release version changed the packaging inputs after the tick's
        # preflight refresh. Refresh Orbi from its deployment checkout, not
        # the published source worktree: a foreign release (for example a
        # Node-only repo) is not required to carry Orbi's pyproject.toml.
        # The fallback keeps direct callers with legacy hand-built configs
        # compatible; load_config always supplies deploy_home.
        deployment_home = (
            config.deploy_home
            if config.deploy_home is not None
            else config.repo_dir
        )
        refresh_cli_install(
            deployment_home, lock_repo_dir=deployment_home,
            run_command=run_command,
        )
        # There is NO local test execution — the CI-wait gate
        # re-run above already decided the test acceptance on this exact
        # release commit; a red or timed-out CI took the recoverable
        # failure path there.
        test_evidence = (
            f"release tests gated by GitHub Actions CI on the release "
            f"commit {release_commit} (the #268 CI-wait gate; no local "
            "test execution)"
        )
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "release.tests_passed",
                    evidence=test_evidence,
                ),
            ),
        )
        tag = declaration["version"]
        existing_tag_commit = release_tag_commit(config.repo_dir, tag)
        if existing_tag_commit is not None:
            if existing_tag_commit == release_commit:
                event(
                    "release_tag_exists", issue=number, tag=tag,
                    commit=existing_tag_commit,
                )
            elif tag_commit_is_ancestor_of_base(
                    existing_tag_commit, release_commit,
                    config.repo_dir):
                # A previous attempt pushed the tag and the frozen base
                # advanced past it afterwards. The tag commit is the
                # canonical release commit — recover it so the release
                # resumes instead of deadlocking on the tag check.
                event(
                    "release_base_advanced_past_tag", issue=number, tag=tag,
                    tag_commit=existing_tag_commit, base_commit=release_commit,
                )
                release_commit = existing_tag_commit
            else:
                raise RuntimeError(
                    f"release tag {tag} already exists on the remote "
                    f"and points at {existing_tag_commit}, not the "
                    f"release commit {release_commit} — an existing "
                    "tag is never moved or overwritten"
                )
        # Release notes come from the tagged commit range, never the
        # Milestone Issue list (Issue #1492). Compute them only now that
        # the release commit is final — after the tag-conflict handling
        # above — so a resume for the same version reproduces the same
        # notes byte for byte.
        range_prs = release_range_prs(
            config.repo_dir, source_repo, release_commit, base_branch,
        )
        changelog = build_release_changelog(
            source_repo,
            release_changelog_scope(
                source_repo, range_prs, release_issue=number,
            ),
            range_prs,
        )
        # Publish the tag with a plain push (never `--force`); the annotated
        # object is created locally by `ensure_release_tag_pushed`.
        if existing_tag_commit is None:
            ensure_release_tag_pushed(
                config.repo_dir, tag, release_commit,
            )
            event(
                "release_tag_pushed", issue=number, tag=tag,
                commit=release_commit,
            )
        release_url = publish_release(
            repo=source_repo, tag=tag, version=tag,
            release_commit=release_commit, changelog=changelog,
            scope_evidence=scope_evidence, gate_evidence=gate_evidence,
            test_evidence=test_evidence, run_id=run_id, issue_number=number,
            attribution_footer=config.attribution_footer,
            attribution_link=config.attribution_link,
        )
        publish(
            action=lambda: publisher.milestone(
                language.text(
                    language.of(config), "release.released",
                    release_url=release_url,
                ),
            ),
        )
        release_success_closeout(
            repo=source_repo, number=number, run_id=run_id,
            run_info=run_info, tag=tag, release_commit=release_commit,
            release_url=release_url, scope_evidence=scope_evidence,
            gate_evidence=gate_evidence, test_evidence=test_evidence,
            ready_label=ready_label, language_code=language.of(config),
            status_url=config.status_url, title=title,
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        event(
            "run_end", issue=number, result="release_success", tag=tag,
            url=release_url,
            elapsed=f"{time.monotonic() - started:.1f}s",
        )
        return release_url
    except ReleaseVersionAlreadyLanded as landed:
        # The version commit is already on the base branch: a concurrent
        # release instance won the push race (Issue #1289). This run yields
        # — the ticket returns to the ready queue and the next tick resumes
        # the release from the landed commit (tag/release) instead of
        # rewriting work that is already done as `ai-blocked`.
        event(
            "claim_yield", issue=number,
            reason="release_version_already_landed",
            base_branch=landed.base_branch,
            remote_commit=landed.remote_commit,
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_RELEASE_WAITING,
            current_labels={IN_PROGRESS_LABEL}, ready_label=ready_label,
        )
        comment_issue(
            number, repo=source_repo,
            body=user_summary.notice(
                run_marker(run_id), "release.yielded",
                language_code=language.of(config),
                status_url=config.status_url, issue=number, title=title,
                tag=landed.tag, base_branch=landed.base_branch,
                remote_commit=landed.remote_commit, run_id=run_id,
            ),
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        return ""
    except ReleaseDeliveriesWaiting as waiting:
        # This is a clean, recoverable tick. Return the release
        # ticket to the ready queue before releasing the caller's slot.
        apply_label_patch(
            number, repo=source_repo, event=EVENT_RELEASE_WAITING,
            current_labels={IN_PROGRESS_LABEL}, ready_label=ready_label,
        )
        detail = ", ".join(f"#{item}" for item in waiting.issue_numbers)
        comment_issue(
            number, repo=source_repo,
            body=(
                run_marker(run_id) + "\n"
                + language.text(
                    language.of(config), "release.waiting",
                    detail=detail,
                ) + "\n"
                # The readable summary precedes the persisted wait fields
                # (Issue #1826).
                + user_summary.summary_text(
                    status_url=config.status_url, issue=number, title=title,
                    language_code=language.of(config),
                ) + "\n"
                f"open_deliveries: {detail}\n"
                f"waited: {int(waiting.waited)}s / "
                f"{int(waiting.limit)}s\n"
                f"wait_started: {time.time()}\n"
                f"run_id={run_id}"
            ),
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        event(
            "release_waiting_deliveries_returned", issue=number,
            open=waiting.issue_numbers,
        )
        return ""
    except Exception as exc:
        release_failure_closeout(
            repo=source_repo, number=number, run_id=run_id,
            run_info=run_info, error=exc, ready_label=ready_label,
            language_code=language.of(config),
            status_url=config.status_url, title=title,
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        return ""
