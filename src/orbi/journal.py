"""Runner journal kernel: logging, run binding, and the subprocess seam.

Every journal line of a task attempt starts with `[run_id]`;
this module owns the logger that enforces it, the run-id binding the
filter reads, and the in-flight delivery scene the stop handler reports.
It also owns the ONE subprocess seam of Article 3.4: ``run_command`` and
its bounded git network retry.

This module imports nothing from the `orbi` package — it is the lowest
leaf, so `github` / `gitops` / `progress` / the extracted modules can all
share one seam and one logger without importing `runner`.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING


class MilestoneReconcileError(RuntimeError):
    def __init__(self, *, status: int, operation: str, stderr: str) -> None:
        self.status, self.operation, self.stderr = status, operation, stderr
        super().__init__(f"{operation} failed with HTTP {status}: {stderr}")


def classify_milestone_error(exc, operation: str) -> None:
    status = milestone_error_status(str(exc.stderr or ""), str(exc.stdout or ""))
    if status is not None:
        raise MilestoneReconcileError(
            status=status, operation=operation,
            stderr=str(exc.stderr or exc.stdout or ""),
        ) from exc
    raise exc


def milestone_error_status(stderr: str, stdout: str = "") -> int | None:
    detail = " ".join((stderr, stdout))
    if re.search(r"(?:http|status) ?401|bad credentials", detail, re.I):
        return 401
    if re.search(r"(?:http|status) ?404|not found", detail, re.I):
        return 404
    return None


if TYPE_CHECKING:
    # Annotation-only: this module stays the runtime leaf (no `orbi`
    # imports at runtime); the run-identity bundle travels as a value.
    from orbi.delivery_scene import RunContext

LOGGER = logging.getLogger("orbi.bootstrap")

# Credential shapes are deliberately anchored at provider/header prefixes.
# Keep this table here as the single redaction choke point shared by journal,
# activity summaries, and GitHub comments.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+"), "Bearer <redacted>"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b(sk-(?:proj-|ant-api03-|or-v1-))[A-Za-z0-9_-]{16,}"),
     r"\1<redacted>"),
    (re.compile(r"\b(sk-[A-Za-z0-9]+[-_])[A-Za-z0-9_-]{16,}"),
     r"\1<redacted>"),
    (re.compile(r"\b(sk-)[A-Za-z0-9]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b(gsk_)[A-Za-z0-9]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b(AIza)[A-Za-z0-9_-]{35}"), r"\1<redacted>"),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b(xox[abprs]-)[A-Za-z0-9-]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b(AKIA)[A-Z0-9]{16}"), r"\1<redacted>"),
    # Preserve the optional auth scheme while replacing the header value.
    (re.compile(r"(?i)(Authorization:\s*)(Bearer\s+)?\S+"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)(x-api-key:\s*)\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)(?<!x-)(api-key:\s*)\S+"),
     r"\1<redacted>"),
)


def redact_secrets(text: str) -> str:
    """Redact supported credentials while retaining their readable prefix."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFormatter(logging.Formatter):
    """Format records, including exception tracebacks, without credentials."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_logging() -> None:
    """Configure the standard journal format with source redaction."""
    logging.basicConfig(level=logging.INFO, format=log_format())
    formatter = SecretRedactingFormatter(log_format())
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


# Run correlation: one task attempt generates one run_id and
# every journal line of the attempt starts with `[run_id]`, so a single
# grep reconstructs the whole timeline. The filter rewrites the message in
# place, so every handler (journal, caplog) sees the same prefixed text.
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
_CURRENT_RUN_ID: str | None = None


def validate_run_id(run_id: object) -> str:
    """Fail fast unless ``run_id`` identifies exactly one task attempt."""
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


class RunIdFilter(logging.Filter):
    """Prefix every log message with the current `[run_id]`, if bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _CURRENT_RUN_ID is not None:
            record.msg = f"[{_CURRENT_RUN_ID}] {record.msg}"
        return True


LOGGER.addFilter(RunIdFilter())


def set_run_id(run_id: str) -> None:
    """Bind one task attempt: every later journal line carries `[run_id]`."""
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = validate_run_id(run_id)


def current_run_id() -> str | None:
    """Return the run id bound to this tick, or None before the claim."""
    return _CURRENT_RUN_ID


def new_run_id() -> str:
    """Return a unique short run identifier for one task attempt."""
    return uuid.uuid4().hex[:8]


def issue_context(source_repo: str, number: int) -> str:
    """Issue reference used on every journal line: `owner/repo#number`."""
    return f"{source_repo}#{number}"


def log_format() -> str:
    """Journal log format without a Python timestamp.

    systemd journal already provides time, host and process on every
    line; printing `%(asctime)s` again only duplicates information.
    """
    return "%(levelname)s %(message)s"


def single_line(value: str) -> str:
    """Flatten a log value to one journal line.

    A command argument may carry line breaks (the multi-line progress
    comment body behind `gh api ... --field body=...`); emitted verbatim,
    they split one `command=` log into several systemd journal lines with
    the same timestamp and PID. Escape each line break to the visible
    two-character sequence `\\n` so the field content stays readable on
    one line. This only changes the log display — the real command is
    never modified.
    """
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def quote_value(value: str) -> str:
    """Double-quote a key=value field value when it needs quoting.

    Values containing spaces or double quotes are quoted; embedded double
    quotes are escaped as ``\\"`` so the field stays parseable as a single
    ``key=value`` token.
    """
    if " " in value or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


# The journal event registry: every structured event name the
# Runner journal can carry, `kind` -> one-line meaning. `event()` refuses
# unregistered names (fail fast on a typo), the docs event tables
# (operations.mdx EN/ZH) must carry exactly this set, and the exporter's
# KNOWN_KINDS are a subset of it — the tests in test_journal_events.py are
# the binding in all three directions.
JOURNAL_EVENTS: dict[str, str] = {
    # Run lifecycle.
    "run_start": "a Pi run started (the full invariant scene)",
    "run_end": "a delivery ended (result, PR, commit)",
    "run_failed": "a Pi run failed (the full scene plus the reason)",
    "run_stopping": "SIGTERM arrived while a delivery was in flight",
    "run_stopped": "the Runner stopped (idle, or the interrupted scene)",
    "picked": "an Issue was claimed for this run",
    "fresh_claim_route": "which ready-claim scan produced the pickup",
    "foreign_pr_ignored": "a cross-repository PR was ignored in a branch lookup",
    "claim_yield": "the claim was yielded to a concurrent claimant",
    "clarify_needs_detail": "the thin-ticket gate stopped the claim: the missing pieces are on the Issue",
    "clarify_check_skipped": "the thin-ticket gate could not decide or could not write (fail open)",
    "blocked_by": "an Issue was skipped: open blockedBy blockers",
    "blocked_by_check_failed": "the blockedBy query failed (fail open)",
    "capacity_full": "every slot is taken; no claim this tick",
    "resuming_run": "an interrupted delivery is resumed under the same run",
    "resume_continue": "a resumed run continues the interrupted work",
    "resume_continue_failed": "the resume scene could not be verified (fail fast)",
    # Live Pi activity and startup milestones.
    "activity": "live Pi activity snapshot (high frequency)",
    "heartbeat": "periodic live Pi progress line",
    "model_wait": "a model request is in flight (no complete session event)",
    "resumed": "the model replied after a model_wait",
    "process_spawned": "Pi was spawned (startup milestone)",
    "provider_config_loaded": "the provider config was materialized (startup milestone)",
    "session_created": "Pi created its session file (startup milestone)",
    "first_request_started": "the first model request went out (startup milestone)",
    "first_response_received": "the first model response arrived (startup milestone)",
    "startup_failed": "the run failed before the first response (classified reason)",
    "pi_resumed": "a resumed session re-bound to its run",
    "pi_drain_abandoned": "output drain stopped early: the pipe write end outlives Pi",
    # Idle recovery and hung model requests.
    "pi_idle": "session idle beyond the warning window (model not expected to reply)",
    "pi_idle_wait": "idle escalation paused: a bounded timeout wrapper is inside its deadline",
    "pi_idle_term": "SIGTERM sent to a hung idle descendant",
    "pi_idle_kill": "SIGKILL sent to a descendant that survived SIGTERM",
    "model_wait_dead": "frozen model_wait crossed the dead threshold; Pi session killed",
    "model_wait_swallowed": "/slots probe confirmed a swallowed model request; Pi session killed",
    "delivery_steered": "an active delivery was restarted with a trusted Issue correction",
    "steering_limit_reached": "active delivery steering reached its configured round limit",
    "steering_poll_failed": "steering polling failed (pure bypass)",
    "steering_limit_notice_failed": "the post-limit correction notice could not be published (pure bypass)",
    # Provider rate limiting and quota stops.
    "pi_retry_429": "a provider 429 triggers an in-run backoff retry",
    "pi_429_attempts_reset": "the persisted 429 attempt counter was reset",
    "pi_429_attempts_write_failed": "persisting the 429 attempt counter failed",
    "pi_429_attempts_clear_failed": "clearing the 429 attempt counter failed",
    "provider_quota": "Pi exited on a provider usage-limit stop (the journal errorMessage)",
    # Bypass publishing.
    "progress_publish_failed": "the progress comment could not be published (pure bypass)",
    "progress_comment_failed": "a post-delivery notification comment could not be published (pure bypass)",
    "progress_finish_missing_next_step": "a terminal progress scene had no next step",
    # Pre-start checks.
    "config_invalid": "the runner config failed validation (no start)",
    "transport": "the pre-start git transport check passed",
    "transport_check_failed": "the pre-start git transport check failed (no start)",
    "active_milestone_missing": "the configured active Milestone does not exist (no start)",
    "active_milestone_closed": "the configured active Milestone is closed (informational)",
    "ready_outside_milestone": "open ready Issues exist outside the active Milestone",
    "ready_outside_milestone_check_failed": "the outside-Milestone ready queue diagnostic failed (idle fallback)",
    "runner_source": "the runner source freshness gate passed",
    "runner_source_stale": "the runner source freshness gate failed (stale or unverifiable)",
    "engine_source_synced": "the deployment checkout synced to the engine source channel",
    "deploy_home_dirty": "tracked local edits block the engine source sync (fail closed)",
    "engine_source_unresolved": "the engine source channel cannot be resolved (fail closed)",
    "engine_source_not_fast_forwardable": "the engine branch diverged locally (fail closed)",
    "engine_source_unverified": "the checkout head could not be verified (fail closed)",
    "base_sync_lock_timeout": "the shared base-sync lock timed out",
    "check_failed": "`orbi check` stopped at the first missing prerequisite",
    "cli_source_drift": "the installed CLI imports from an unexpected source",
    "unit_drift": "installed units vs repo templates: clean, self-healed, or drifting",
    "unit_conflict": "an installed unit file conflicts with the deployment",
    "units_installed": "the systemd user units were installed or refreshed",
    "legacy_units_migrated": "pre-#149 non-templated units were removed",
    # Self-health check.
    "health_check_skipped": "the periodic health check skipped this tick",
    "health_check_queue_empty": "the health check found a non-empty-but-stale queue claim",
    "health_check_failed": "the periodic health check crashed (bypass; never fails the delivery)",
    "health_degraded": "a service instance shows incident-pattern degradation",
    "health_alert_repo_undetermined": "a health alert could not be attributed to a source repo",
    "health_state_unreadable": "the persisted health state could not be read",
    "health_pickup_record_failed": "recording the health pickup scene failed (bypass)",
    "health_success_record_failed": "recording the delivery success in health state failed (bypass)",
    "health_failure_record_failed": "recording the delivery failure in health state failed (bypass)",
    # Delivery closeout.
    "delivery_takeover": "an existing PR delivery is taken over for review",
    "delivery_awaiting": "the delivery waits for its PR to become mergeable",
    "delivery_awaiting_human_merge": "the reviewed PR awaits a maintainer merge",
    "delivery_ci": "the PR CI status observed",
    "delivery_auto_merged": "the PR was auto-merged (content-only or trivial)",
    "delivery_merged": "the PR merged; the slot is released",
    "delivery_closed_unmerged": "the PR closed without merging; the Issue goes ai-blocked",
    "delivery_label_inconsistent": "the delivery labels contradict the PR state",
    "delivery_label_repaired": "an inconsistent delivery label was repaired",
    "delivery_label_repair_failed": "the delivery label repair failed",
    "delivery_behind_base": "the task branch is behind the base branch",
    "delivery_no_commit": "the task worktree carries no commit to deliver",
    "delivery_uncommitted_changes": "the task worktree carries uncommitted changes",
    "delivery_issue_closed": "the source Issue was closed mid-delivery",
    "delivery_ci_evidence_publish_failed": "the CI evidence comment could not be published (bypass)",
    "delivery_ci_triage_lookup_failed": "the CI-failure triage Issue lookup failed",
    "delivery_review_failed": "the review session failed",
    # Dead-loop guard (Issue #825).
    "failure_comment_deduplicated": "an identical failure repeat bumped the existing comment's counter; no second comment",
    "failure_comment_update_failed": "the existing failure comment could not be resolved or updated; the tick continues",
    "failure_comment_id_unavailable": "a repeated failure comment had no recoverable REST id; a new comment was posted",
    "failure_streak_escalated": "the same failure reached the consecutive limit; the Issue goes ai-blocked",
    "failure_history_read_failed": "the failure-history read for the dead-loop guard failed (fail-open)",
    # External contributor PRs.
    "external_takeover": "an external PR is taken over for review",
    "external_takeover_skipped": "the external PR is not takeover-eligible",
    "external_takeover_closed": "the external PR closed without merging; the Issue requeues",
    "external_takeover_close_failed": "closing the takeover triage Issue failed",
    "external_takeover_triage": "the external review passed; the ticket stops at ai-blocked for the maintainer (Issue #842 D2, no auto-merge)",
    "external_pr_already_merged": "the triaged external PR is already merged",
    "external_pr_routed_takeover": "the triage Issue routes to the external PR takeover",
    "external_pr_state_probe_failed": "the external PR state probe failed",
    # PR verification (fresh delivery and resume).
    "pr_created": "the delivery PR was opened",
    "pr_base_mismatch": "the PR base branch does not match the configured base",
    "pr_head_diverged": "the PR head is not the reviewed head",
    "pr_repo_mismatch": "the PR belongs to a different repository",
    "pr_fixes_missing": "the PR body is missing the Fixes #N keyword",
    "pr_run_marker_missing": "the PR body is missing the orbi run marker",
    "pr_url_mismatch": "the stored PR URL does not match the live PR",
    "remote_head_mismatch": "the remote task branch head moved under us",
    "local_head_ahead_of_pr_head": "local commits are not pushed to the PR head",
    "resume_pr_closed": "the resumed delivery's PR is closed",
    "resume_pr_handled": "the resumed delivery's PR scene was processed",
    "resume_pr_missing": "no PR found for the resumed delivery",
    "resume_pr_multiple_open": "multiple open PRs claim the same Issue",
    "resume_pr_verification_failed": "the resumed delivery failed PR verification",
    "resume_scene_pr_state_lookup_failed": "the resume scene's PR state lookup failed",
    # Review loop.
    "review": "an independent review session finished (verdict)",
    "review_verdict_head_unknown": "the review verdict names an unknown Git object",
    "delivery_ci_pending": "the PR head's CI is still pending; the delivery defers to the next tick",
    "review_head_advanced": "the review session pushed a fixed head",
    "pushed_head_recorded": "a round-start head adoption recorded an engine-pushed head (Issue #833)",
    "pushed_head_unrecorded": "the engine push history could not be recorded; the merge record degrades to unknown (Issue #833)",
    "review_budget_recovered": "a stale review round counter was recovered",
    "review_rounds_exhausted": "the review round budget is exhausted (fail fast)",
    "base_advance_rounds_exhausted": "the base-advance retry budget is exhausted (fail fast)",
    "review_rounds_exhausted_expected_terminal": "the exhausted budget terminal was already recorded",
    "review_human_decision_required": "a review reached an intentional human decision point, not a Runner defect",
    "review_findings_unfixed": "review findings survive; the Issue goes ai-fix-needed",
    "review_absorb_abandoned": "a behind-base round emitted pass without absorbing or reporting the abandoned absorb (Issue #877)",
    "review_merge_deferred": "the merge gate deferred the merge to the next tick",
    "review_recovery_ci_status": "CI status for review recovery observed",
    "review_recovery_ci_status_failed": "the CI status read for review recovery failed",
    # Merge gate.
    "merge_gate_ci_pending": "the merge gate found pending CI; the merge defers to the next tick",
    "merge_gate_mergeable_unknown": "GitHub mergeability is still UNKNOWN; the merge defers to the next tick",
    "merge_gate_behind_base": "the merge gate found the PR behind the base",
    "merge_gate_head_moved": "the PR head moved since the review",
    "merge_gate_not_mergeable": "the PR is not mergeable (conflict or failing checks)",
    "merged": "the PR merge landed on the reviewed head",
    "confirm_merged_missing_on_base": "the merged commit is missing from the base branch",
    "confirm_merged_not_merged": "the PR reports merged but GitHub contradicts the state",
    # Human review gate.
    "human_review_waiting": "the delivery waits for the human review label",
    "human_review_checklist_failed": "publishing the human review checklist failed (bypass)",
    "human_review_diff_read_failed": "the human review diff could not be read",
    # Base and worktree upkeep.
    "base_absorbed": "an advanced base branch was merged into the task branch",
    "base_merge_conflict": "the base merge conflicted; aborted without resolution",
    "base_checkout_synced": "the repo_dir base checkout fast-forwarded to the remote base",
    "base_checkout_sync_skipped": "the base checkout sync was skipped (already current)",
    "base_checkout_sync_failed": "the base checkout sync failed",
    "base_checkout_not_fast_forwardable": "the base checkout diverged from the remote base",
    "worktree_cleaned": "a task worktree was cleaned up",
    "worktree_cleanup_failed": "a task worktree cleanup failed",
    "worktree_recreated": "a resumed delivery's missing worktree was rebuilt from the remote branch",
    "worktree_reclaimed": "released task worktrees were reclaimed in bulk",
    "worktree_reclaim_failed": "a worktree reclamation step failed (bypass; the pass continues)",
    "worktrees_exclude_added": "a worktrees path was added to the local git exclude",
    "orbi_exclude_added": "an .orbi runtime path was added to the local git exclude",
    "runner_runtime_exclude_skipped": "the runtime exclude repair was skipped (already correct)",
    "runner_runtime_exclude_repaired": "the runtime exclude was repaired",
    # Repository config.
    "repo_config_invalid": "the repository config failed validation",
    "repo_config_keys_ignored": "the repository config carried key(s) this engine version does not know",
    "repo_config_read_failed": "the repository config could not be read",
    "repo_config_previous_read_failed": "the previous repository config snapshot could not be read",
    "repo_config_previous_lookup_failed": "the previous repository config snapshot lookup failed",
    "repo_config_failure_report_failed": "reporting the repository config failure failed",
    # Epics and milestones.
    "epic_not_claimed": "an ai-epic Issue was skipped by the claim scan",
    "epic_kept_open": "the Epic stays open: a completion condition is unmet",
    "epic_closed": "every completion condition is met; the Epic Issue closed",
    "epic_reconcile_failed": "the Epic reconciliation sweep crashed (bypass)",
    "milestone_kept_open": "the release Milestone stays open (unmet condition)",
    "milestone_closed": "the release Milestone closed",
    "milestone_reconcile_failed": "the Milestone reconciliation sweep crashed (bypass)",
    "milestone_reconcile_auth_failed": "milestone reconciliation could not authenticate to GitHub",
    "milestone_reconcile_not_found": "milestone reconciliation target was not found on GitHub",
    "orphan_pr_reported": "an orphan PR (source Issue closed) was reported",
    "orphan_pr_reconcile_failed": "the orphan-PR reconciliation sweep crashed (bypass)",
    "stale_milestone_issue_closed": "a stale milestone-tracking Issue was closed",
    "stale_milestone_issue_close_failed": "closing a stale milestone-tracking Issue failed",
    "pending_milestone_issue_failed": "the pending-milestone Issue state could not be applied",
    "pending_milestone_issue_deduplicated": "duplicate pending-milestone Issues were closed",
    "active_milestone_advanced": "the active Milestone advanced to the next pending one",
    "active_milestone_closed_unscoped": (
        "the active Milestone is closed with no newer open one, so the tick "
        "claims without a Milestone scope"
    ),
    "active_milestone_advance_pending": "a pending Milestone candidate is waiting",
    "active_milestone_release_pending": (
        "a finished Milestone still needs its release ticket confirmed"
    ),
    "active_milestone_advance_failed": "advancing the active Milestone failed",
    "active_milestone_variable_removed": "the active_milestone repo variable was removed",
    "active_milestone_variable_unchanged": "the active_milestone repo variable matches the config",
    "active_milestone_variable_updated": "the active_milestone repo variable was updated",
    "active_milestone_variable_absent": "the active_milestone repo variable is absent",
    "active_milestone_variable_created": "the active_milestone repo variable was created",
    "active_milestone_variable_sync_failed": "the active_milestone repo variable sync failed",
    "milestone_command_applied": "an in-ticket `/milestone` command ran all three steps",
    "milestone_command_failed": "an in-ticket `/milestone` command failed at one step",
    # Releases.
    "release_not_claimed": "the release state machine did not claim the release Issue",
    "release_task": "the release state machine advanced (step report)",
    "release_resuming_run": "the release continues an interrupted run",
    "release_tag_exists": "the release tag already exists",
    "release_tag_pushed": "the release tag was pushed",
    "release_base_advanced_past_tag": "the base advanced past the release tag",
    "release_waiting_deliveries": "the release waits for open delivery PRs",
    "release_waiting_deliveries_returned": "waiting deliveries returned within the budget",
    "release_waiting_ci": "the release waits for CI on the tagged commit",
    "release_milestone_open_items": "the release Milestone still has open items",
    "release_milestone_check_failed": "the release Milestone check failed",
    "release_milestone_incomplete": "the release Milestone is incomplete for the release",
    "release_ticket_armed": "the release Issue entered the release state machine",
    "release_ticket_arm_failed": "arming the release Issue failed",
    "release_failed": "the release state machine failed (ai-blocked)",
    "release_publish_closeout_failed": "the release publish closeout failed",
    "release_milestone_evidence_failed": "the release Milestone evidence publish failed",
    "release_success_comment_failed": "the release success comment failed (bypass)",
    "release_changelog_issue_excluded": "an Issue was excluded from the release changelog",
    "release_changelog_contributor_skipped": "a changelog contributor entry was skipped",
    "release_changelog_pr_link_dropped": "a changelog PR link was dropped (unlinked PR)",
    # Subprocess seam (journal.py itself).
    "command_failed": "an external command exited non-zero (fail fast)",
    "command_timeout": "an external command timed out (fail fast)",
    "command_spawn_failed": "an external command could not be spawned",
    "git_network_retry": "a transient git fetch/push failure is retried with backoff",
    "gh_read_retry": "a transient `gh` read failure is retried with backoff",
    "gh_write_retry": "a transient `gh` write failure is retried with backoff",
}


def event(kind: str, scene: str | None = None, /, *,
          level: int = logging.INFO, **fields: object) -> None:
    """Emit one structured journal event — THE single emission point.

    Every `kind key=value...` journal line is born here:
    the kind must be registered in `JOURNAL_EVENTS` (a typo fails fast,
    never misleads the exporter or the docs), `scene` is the optional
    pre-formatted `key=value ...` continuation (the
    `pi_activity.format_run_scene` / `format_end_scene` contract), and
    every field value is flattened to one journal line (`single_line`)
    and quoted when it needs it (`quote_value`) in exactly this one
    place. The shared logger's `RunIdFilter` adds the `[run_id]` prefix.
    """
    if kind not in JOURNAL_EVENTS:
        raise ValueError(f"unknown journal event kind: {kind!r}")
    parts = [kind]
    if scene is not None:
        parts.append(scene)
    for key, value in fields.items():
        parts.append(f"{key}={quote_value(single_line(str(value)))}")
    LOGGER.log(level, " ".join(parts))


def run_command(command: list[str], *, cwd: Path | None = None,
                timeout: int | None = None,
                log_command: list[str] | None = None,
                log_stdout: bool = False,
                failure_log_level: int = logging.ERROR,
                check: bool = True,
                env: dict[str, str] | None = None) -> str | subprocess.CompletedProcess[str]:
    """Run one external command; log context and fail fast by default.

    ``check=False`` is for callers that need to inspect an expected status
    result (for example, a Git object-existence probe). The completed
    process is returned in that mode; normal callers retain the historical
    stripped stdout return value and fail-fast behavior.
    """
    LOGGER.info(
        "command=%s cwd=%s",
        single_line(" ".join(log_command or command)), cwd or Path.cwd(),
    )
    try:
        run_kwargs = {
            "cwd": cwd,
            "capture_output": True,
            "text": True,
            "check": check,
            "timeout": timeout,
        }
        if env is not None:
            run_kwargs["env"] = env
        result = subprocess.run(command, **run_kwargs)
    except subprocess.CalledProcessError as exc:
        event(
            "command_failed", level=failure_log_level,
            returncode=exc.returncode,
            stdout=(exc.stdout or "").rstrip(),
            stderr=(exc.stderr or "").rstrip(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        event(
            "command_timeout", level=failure_log_level, timeout=timeout,
            stdout=(exc.stdout or "").rstrip(),
            stderr=(exc.stderr or "").rstrip(),
        )
        raise
    except OSError as exc:
        event("command_spawn_failed", level=failure_log_level, error=exc)
        raise
    if result.stderr:
        LOGGER.info("stderr=%s", result.stderr.rstrip())
    if log_stdout and result.stdout:
        LOGGER.info("stdout=%s", result.stdout.rstrip())
    if not check:
        return result
    return result.stdout.strip()


GIT_NETWORK_MAX_ATTEMPTS = 3
GIT_NETWORK_TIMEOUT_SECONDS = 30
GIT_NETWORK_BACKOFF_SECONDS = 1
GIT_TRANSIENT_ERROR_MARKERS = (
    "connection timed out",
    "operation timed out",
    "connection reset",
    "connection refused",
    "temporary failure in name resolution",
    "network is unreachable",
    "network unreachable",
    # A transient GitHub SSH auth failure (a missing key reports the same
    # text, but it keeps failing after the bounded retries and is re-raised).
    "could not read from remote repository",
    "permission denied (publickey)",
)


def _is_retryable_git_network_failure(
    command: list[str], exc: subprocess.CalledProcessError,
) -> bool:
    """Return whether a Git fetch/push failed with a known transient error."""
    if len(command) < 2 or command[:1] != ["git"]:
        return False
    if command[1] not in {"fetch", "push"}:
        return False
    stderr = (exc.stderr or "").lower()
    return any(marker in stderr for marker in GIT_TRANSIENT_ERROR_MARKERS)


def _is_git_network_command(command: list[str]) -> bool:
    return (
        len(command) >= 2
        and command[:1] == ["git"]
        and command[1] in {"fetch", "push"}
    )


def run_git_network_command(
    command: list[str], *, cwd: Path | str | None = None,
    command_runner: Callable[..., str] | None = None,
) -> str:
    """Run an Orbi-controlled Git fetch/push with bounded network retries.

    Only explicitly recognized transient network messages are retried. The
    last ``CalledProcessError`` is re-raised unchanged so its stderr remains
    available to the existing failure handling.
    """
    execute = command_runner or run_command
    attempt = 0
    while True:
        attempt += 1
        try:
            return execute(
                command, cwd=cwd, timeout=GIT_NETWORK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            retryable = _is_git_network_command(command)
            detail = f"command timed out after {GIT_NETWORK_TIMEOUT_SECONDS}s"
            if attempt >= GIT_NETWORK_MAX_ATTEMPTS or not retryable:
                raise
        except subprocess.CalledProcessError as exc:
            retryable = _is_retryable_git_network_failure(command, exc)
            detail = (exc.stderr or "").strip()
            if attempt >= GIT_NETWORK_MAX_ATTEMPTS or not retryable:
                raise
        delay = GIT_NETWORK_BACKOFF_SECONDS * (2 ** (attempt - 1))
        event(
            "git_network_retry", level=logging.WARNING,
            command=single_line(" ".join(command)), attempt=attempt + 1,
            max_attempts=GIT_NETWORK_MAX_ATTEMPTS, delay_seconds=delay,
            stderr=single_line(detail),
        )
        time.sleep(delay)


# Stop scene: when systemd (or any caller) stops the Runner
# with SIGTERM, the journal must show which Issue context was active
# BEFORE systemd's generic "Stopped" line, and the live Pi child must be
# shut down (no orphan Pi). The context is bound while a delivery is in
# flight (after the claim, or after a resumed scene is bound) and cleared
# when the delivery ends. It carries no new id: the run id is the
# existing `_CURRENT_RUN_ID`, and the phase/session come from the
# existing activity snapshot of the worktree's `.pi-session`.
_ACTIVE_RUN: dict | None = None


def set_active_run(ctx: RunContext, title: str) -> None:
    """Bind the in-flight delivery scene for the stop handler."""
    global _ACTIVE_RUN
    _ACTIVE_RUN = {
        "issue": int(ctx.issue),
        "title": title,
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "pi": None,
    }


def set_active_pi(process: subprocess.Popen | None) -> None:
    """Track the live Pi child of the in-flight delivery.

    `stream_pi` calls it after the child is spawned (and again with None
    after the child is reaped), so the stop handler signals exactly the
    child that is alive — never an already-exited process. Without a
    bound run (unit tests call `stream_pi` directly) it is a no-op.
    """
    if _ACTIVE_RUN is not None:
        _ACTIVE_RUN["pi"] = process


def clear_active_run() -> None:
    """No delivery in flight anymore."""
    global _ACTIVE_RUN
    _ACTIVE_RUN = None


def active_run() -> dict | None:
    """The in-flight delivery scene, or None (the stop handler reads it)."""
    return _ACTIVE_RUN
