"""The delivery failure report: classification, label, body, comment.

Every failure the Runner reports goes through `report_delivery_failure`
here: classify the exception (`_classify_failure`,
`is_unrecoverable_failure`), transition the label (`ai-blocked` alone for
an external precondition, `ai-fix-needed` for everything recoverable),
assemble the body (fixed phrase + run scene + bounded evidence), comment
the Issue and, on the recoverable branch, the PR, then publish the
milestone and the terminal progress scene as a pure bypass.

A verbatim extraction out of `orbi.runner` (Issue #1260, Constitution
Article 3.2: an extraction never changes behaviour), so it imports
nothing from `orbi.runner` back (Article 3.3) and the runner reaches it
as `failure_report.<name>`. `comment_pr` and `review_rounds_so_far` moved
here too: the frozen `orbi.github` size ceiling (github.py at 1146 lines
against 1162) forbids the +58 lines the preferred home would need.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
from pathlib import Path

from orbi import failure, runner_health, scene
from orbi.delivery_labels import (
    EVENT_BLOCKED, EVENT_FIX_NEEDED, FIX_NEEDED_LABEL, IN_PROGRESS_LABEL,
)
from orbi.failure import (
    _failure_comment_body, _failure_detail, _failure_summary,
    _redact_local_paths,
)
from orbi.github import (
    _comment_is_trusted, _pr_number, apply_label_patch, comment_issue,
    commit_check_runs, issue_comments, issue_labels, issue_priority,
    latest_run_marker, list_issues, pr_comments, run_gh_write_command,
    update_issue_comment,
)
from orbi.journal import LOGGER, event, issue_context, run_command
from orbi.pi_activity import activity_snapshot
from orbi.pi_process import RateLimitExhaustedError
from orbi.progress import (
    FAILURE_MARKER_PATTERN, RUN_MARKER_PATTERN, ProgressPublisher, _fenced,
    _finish_progress_body, _safe_publish, bump_failure_repeat, failure_marker,
    failure_repeat_count, format_status_comment, run_marker,
)


class UnrecoverableDeliveryError(RuntimeError):
    """A delivery failure that is an EXTERNAL precondition the AI cannot
    safely judge or fix.

    `ai-blocked` is not the result of "one run failed": it is the
    terminal state the Runner reaches only after reading the full task
    context (code, logs, existing artifacts) and deciding that it cannot
    safely continue. The message carries the explicit reason why
    automatic recovery is impossible (missing external resource/permission/
    credential, a human decision, or a bounded loop the AI may not exceed);
    the failure comment renders it so a human sees exactly what to do.
    Every other delivery failure is recoverable and stays in the automatic
    fix loop (`ai-fix-needed`, the next timer resumes the same run, branch,
    worktree and PR).
    """


class PreExistingCIFailure(UnrecoverableDeliveryError):
    """A failed delivery check is already failing on the PR's base.

    Retrying review/fix rounds cannot change a failure that the PR did not
    introduce, so this external precondition fails the delivery quickly.
    """


class GateCIFailure(RuntimeError):
    """A failed CI check on the reviewed PR head itself.

    Unlike `PreExistingCIFailure` this is recoverable: the review/fix
    loop repairs the head until the check passes or the round budget
    exhausts. Routing to that recoverable path is by THIS type, never by
    matching text inside the message — rewording the message must not
    change control flow (the rule at `RecoverableMergeGateError`).
    """


class ReviewRoundsExhausted(UnrecoverableDeliveryError):
    """Expected terminal stop after the bounded review/fix budget.

    This remains an exception internally so the existing delivery cleanup
    path performs the terminal label/comment transition, but it is not a
    Runner failure and must not be logged with a traceback.
    """


class HumanDecisionRequired(UnrecoverableDeliveryError):
    """The reviewer found a decision that only a maintainer can make."""

    def __init__(self, message: str, *, action: str) -> None:
        super().__init__(message)
        self.action = action


def is_unrecoverable_failure(exc: BaseException) -> bool:
    """Classify one delivery failure.

    True for an explicit `UnrecoverableDeliveryError` (an external
    precondition the AI cannot safely judge or fix) and for the #698
    provider-quota exhaustion (`RateLimitExhaustedError`): the backoff
    budget of the delivery attempt is spent on an external condition,
    and a recoverable classification would resume the open-PR review
    with the persisted counter already at the limit — one 429 exit per
    tick, forever, the unbounded loop the issue bans. Every other
    failure — Pi execution failure (pi exit, upstream dead, idle
    recovery), timeout, runner exception, missing/malformed verdict,
    missing worktree, unpushed local commit, gate failure — is
    recoverable: the Issue goes to `ai-fix-needed` and the next timer
    resumes the same run, branch, worktree and PR. A single failure
    must never permanently stop an Issue.
    """
    return isinstance(
        exc, (UnrecoverableDeliveryError, RateLimitExhaustedError),
    )


# The delivery exception hierarchy is OURS; the closed code set and its
# markers live in `orbi.failure` (Issue #1229 keeps this module small).
def _classify_failure(exc: BaseException, *, outcome: str) -> failure.Failure:
    """Map one delivery exception to the machine-readable failure record."""
    return failure.classify(
        _failure_detail(exc),
        provider_quota=isinstance(exc, RateLimitExhaustedError),
        review_budget=isinstance(exc, ReviewRoundsExhausted),
        human_decision=isinstance(exc, HumanDecisionRequired),
        unrecoverable=isinstance(exc, UnrecoverableDeliveryError),
        outcome=outcome,
    )


def comment_pr(number: int, *, repo: str, body: str) -> None:
    """Comment on a PR in the configured source repository.

    The same `format_status_comment` rendering as `comment_issue`: the
    PR-side copy of a round / finding / blocked comment carries the run
    marker and the runner fingerprint like its Issue twin.
    """
    rendered = format_status_comment(body)
    run_gh_write_command(
        ["gh", "pr", "comment", str(number), "--repo", repo,
         "--body", rendered],
        command_runner=run_command,
        already_applied=lambda: any(
            comment.get("body") == rendered
            for comment in pr_comments(number, repo=repo)
        ),
    )


def review_rounds_so_far(
    comments: list[dict], *, after: str | None = None,
    run_id: str | None = None, pr_number: int | None = None,
) -> int:
    """Count trusted review rounds for the selected delivery identity.

    With ``run_id`` supplied, only comments carrying that attempt's stable
    marker count. This is essential when an Issue is picked up again after a
    closed PR: historical rounds remain visible evidence, but cannot consume
    the new PR's budget. ``after`` retains the existing human-recovery
    boundary for resumes of the same PR. Calls without an identity filter
    remain available for legacy evidence rendering.
    """
    rounds = 0
    marker = run_marker(run_id) if run_id is not None else None
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        if after is not None and comment.get("createdAt", "") <= after:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if marker is not None and marker not in body:
            continue
        if pr_number is None:
            matches = any(line.startswith("Orbi review round ")
                          for line in body.splitlines())
        else:
            matches = any(
                line.startswith(f"Orbi review round ") and
                f" for PR #{pr_number}:" in line
                for line in body.splitlines()
            )
        if matches:
            rounds += 1
    return rounds


_SGR_RE = re.compile(r"(?:\x1b\[[0-9;]*m|\^\[\[[0-9;]*m)")


def _tail_text(path: Path, *, lines: int = 20, chars: int = 4000) -> str:
    """Read a bounded, de-coloured tail for failure evidence."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # UTF-8 characters are at most four bytes.  This is enough to
            # retain the requested character tail without reading a huge
            # test log or session file into memory.
            window = min(size, chars * 4 + 1)
            handle.seek(size - window)
            content = handle.read(window).decode(
                "utf-8", errors="replace",
            )
    except (OSError, UnicodeError):
        return "<unavailable>"
    tail = "\n".join(content.splitlines()[-lines:])
    tail = _SGR_RE.sub("", tail)
    return tail[-chars:] if len(tail) > chars else tail


# The number of session records a failure comment summarizes.
SESSION_SUMMARY_LIMIT = 20


def _latest_session_file(worktree: Path | None) -> Path | None:
    """The most recent pi session log in the worktree, or None."""
    if worktree is None:
        return None
    session_files = sorted(
        (p for p in (worktree / ".pi-session").glob("*.jsonl")
         if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    return session_files[-1] if session_files else None


def _session_summary(session_file: Path,
                     *, limit: int = SESSION_SUMMARY_LIMIT) -> str:
    """Structured summary of the last session records.

    One line per parsed record — timestamp, type, role, tool name and
    content-block kinds; the record text itself never enters the comment
    (the full log path is named next to the segment). A record whose
    text blocks repeat the previous record's is skipped: pi logs the
    final assistant text twice (once inside the full message, once
    standalone), which used to duplicate whole paragraphs in the
    comment. Unparsable tail lines are skipped; an unreadable file is
    the `<unavailable>` placeholder.
    """
    try:
        with session_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 65536)
            handle.seek(size - window)
            content = handle.read(window).decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return "<unavailable>"
    summary: list[str] = []
    previous_text: str | None = None
    for line in reversed(content.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
        blocks = blocks if isinstance(blocks, list) else []
        text = "".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text and text == previous_text:
            continue
        previous_text = text or None
        parts = [str(record.get("type", "?"))]
        if message.get("role"):
            parts.append(f"role={message['role']}")
        if message.get("toolName"):
            parts.append(f"tool={message['toolName']}")
        kinds = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "?"))
            if kind == "toolCall" and block.get("name"):
                kind = f"toolCall:{block['name']}"
            kinds.append(kind)
        if kinds:
            parts.append(f"content={','.join(kinds)}")
        summary.append(f"{record.get('timestamp', '-')} {' '.join(parts)}")
        if len(summary) >= limit:
            break
    if not summary:
        return "<unparsable>"
    return "\n".join(reversed(summary))


def _failure_scene(snapshot: dict, *, run_id: str, issue: str, role: str,
                   branch: str, worktree: str, pr_url: str | None = None) -> str:
    """Render run facts as wrapping Markdown, never as a host-path dump."""
    fields = [
        ("run", run_id), ("issue", issue), ("role", role),
        ("branch", branch),
        ("worktree", "local runner worktree"),
        ("session", snapshot.get("session_id") or "-"),
        ("session log", "local session log"),
        ("phase", snapshot.get("phase") or "-"),
        ("last activity", snapshot.get("last_activity") or "-"),
        ("action", snapshot.get("action") or "-"),
        ("result", snapshot.get("result") or "-"),
    ]
    if snapshot.get("session_file"):
        fields[6] = ("session log", "local session log")
    else:
        fields[6] = ("session log", "<unavailable>")
    rendered_fields = [
        f"- {key}: `{_redact_local_paths(str(value))}`"
        for key, value in fields
    ]
    if pr_url:
        rendered_fields.insert(1, f"- PR: [{pr_url}]({pr_url})")
    rendered = "\n".join(rendered_fields)
    return rendered + (
        "\n- legacy correlation: "
        f"`run={run_id} branch={branch} session="
        f"{snapshot.get('session_id') or '-'} phase={snapshot.get('phase') or '-'} "
        f"last_activity={snapshot.get('last_activity') or '-'}`"
    )


def _failure_evidence(worktree: Path | None, exc: BaseException) -> str:
    """Render the bounded evidence that survives terminal worktree cleanup.

    Pi subprocess streams come from ``CalledProcessError``. The session and
    test log are read before cleanup and only bounded, fenced segments are
    copied into the failure comment: raw output stays literal on GitHub,
    the session tail is a structural summary whose duplicates are removed,
    and the full session log is named for the deep-dive. The
    failure reason itself stays the readable head of the comment.
    """
    stderr = getattr(exc, "stderr", None)
    stdout = getattr(exc, "output", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    stderr = str(stderr) if stderr else "<empty>"
    stdout = str(stdout) if stdout else "<empty>"
    return_code = getattr(exc, "returncode", None)
    session_file = _latest_session_file(worktree)
    session = (
        _session_summary(session_file) if session_file else "<unavailable>"
    )
    session_label = "local session log" if session_file else "<unavailable>"
    test_log = "<unavailable>"
    if worktree is not None:
        test_path = worktree / ".orbi" / "test.log"
        if test_path.is_file():
            test_log = _tail_text(test_path)
    return (
        "\n\nFailure evidence (captured before cleanup):\n"
        f"exit_code={return_code if return_code is not None else '<unknown>'}\n"
        f"\nstderr_tail:\n{_fenced(_redact_local_paths(stderr))}\n"
        f"\nstdout_tail:\n{_fenced(_redact_local_paths(stdout))}\n"
        "\nsession_last_events "
        f"(last {SESSION_SUMMARY_LIMIT} records; full log: "
        f"{session_label}):\n{_fenced(_redact_local_paths(session))}\n"
        f"\ntest_log_tail:\n{_fenced(_redact_local_paths(test_log))}"
    )


# The failure-scene snapshot placeholder — the fields a
# failure comment shows when no session file exists yet (the Pi never
# started or the session dir is gone). One constant for every reporter.
_SNAPSHOT_PLACEHOLDER: dict = {
    "session_id": None, "session_file": None,
    "phase": "starting",
    "last_activity": None, "action": None,
    "result": None,
}


def _snapshot_or_placeholder(session_dir: Path, *, number: int) -> dict:
    """Best-effort activity snapshot for a failure scene.

    The watcher state when a session file exists, the placeholder scene
    when none does yet, and the placeholder again — with the read
    failure logged — when the snapshot itself fails: best-effort
    observability, never a second failure.
    """
    try:
        snapshot = activity_snapshot(session_dir)
    except Exception:
        LOGGER.exception("issue=%s activity scene failed", number)
        return dict(_SNAPSHOT_PLACEHOLDER)
    return snapshot if snapshot is not None else dict(_SNAPSHOT_PLACEHOLDER)


# The fixed phrases of the two failure templates: the
# classified blocked branch names WHY automatic recovery is impossible,
# the recoverable branch names the automatic next step.
_BLOCKED_PRECONDITION_PHRASE = (
    "; this is an external precondition the AI cannot safely judge or "
    "fix, so it cannot be recovered automatically (the Issue stays "
    "ai-blocked until a human decides)"
)


# The whole failure-comment body is capped well below the
# GitHub comment limit; the cut is noted with the full session log path.
FAILURE_COMMENT_MAX_CHARS = 20000


# The dead-loop limit (Issue #825): the same (run_id, failure
# fingerprint) recurring to this many CONSECUTIVE failures escalates the
# recoverable classification to `ai-blocked` — the premises never
# changed, so the next tick would only repeat the same failure (the
# orbi-cloud#360 scene: 82 identical failures, one slot pinned all day).
FAILURE_STREAK_LIMIT = 3


def _is_line_failure_comment(body: str, run_id: str,
                             fingerprint: str) -> bool:
    """True when one comment is the failure record of this exact
    (run_id, fingerprint) pair — the hidden `orbi:fail` marker plus the
    run marker (Issue #825)."""
    fail = FAILURE_MARKER_PATTERN.search(body)
    return (
        fail is not None
        and fail.group(1) == fingerprint
        and run_id in set(RUN_MARKER_PATTERN.findall(body))
    )


def _reported_failure_comment(comments: list, run_id: str,
                              fingerprint: str) -> dict | None:
    """The trusted comment already reporting this exact
    (run_id, fingerprint) failure, or None — the #825 dedup key. A pure
    scan over the already-fetched comment list."""
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if isinstance(body, str) and _is_line_failure_comment(
                body, run_id, fingerprint):
            return comment
    return None


def _failure_streak(comments: list, run_id: str,
                    fingerprint: str) -> int:
    """The number of CONSECUTIVE identical failures at the tail of the
    trusted comment history (Issue #825). A pure scan over the
    already-fetched comment list.

    A matching failure comment adds its repeat count — the dedup keeps
    ONE comment per (run_id, fingerprint) and bumps its counter in
    place, so the counter IS the occurrence count. A DIFFERENT failure
    ends the streak; a scene block (an opened PR, a completed review
    round) ends it too — the delivery line advanced, the premises
    changed. Publisher milestones and human chatter in between are
    skipped: they change no premise."""
    streak = 0
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if _is_line_failure_comment(body, run_id, fingerprint):
            streak += failure_repeat_count(body)
            continue
        if FAILURE_MARKER_PATTERN.search(body) or scene.carries_scene_block(
                body):
            break
    return streak


def block_scene_failure(issue: dict, error: ValueError, repo: str,
                        comments: list[dict]) -> None:
    """Mark an opened-PR Issue `ai-blocked` when its scene is malformed.

    The blocked transition is scoped to this one Issue: the
    caller continues the tick, so a malformed scene never crashes the
    whole runner. The failure comment carries the run marker recovered
    from a trusted comment when it is present — the same run id, never a
    new or guessed one. The PR, branch and worktree stay intact. A
    failure of the reporting itself is logged, never raised: the Issue
    then stays in its opened-PR state and the next tick retries the
    block.
    """
    number = int(issue["number"])
    LOGGER.error(
        "issue=%s resume scene is malformed: %s", number, error,
    )
    marker = latest_run_marker(comments)
    try:
        apply_label_patch(
            number, repo=repo, event=EVENT_BLOCKED,
            current_labels={FIX_NEEDED_LABEL},
        )
        # A scene that cannot be recovered is an external
        # precondition the AI cannot fix by itself (the runner cannot
        # derive run_id, branch, worktree or PR without the trusted
        # scene comment, so it cannot start a review session): the
        # comment states the EXPLICIT reason why automatic recovery is
        # impossible plus the human next step (restore the scene or
        # relabel).
        comment_issue(
            number, repo=repo,
            body=(
                f"{marker}\n" if marker else ""
            ) + (
                f"Orbi failed: {error}; this is an external "
                "precondition the AI cannot safely judge or fix, so "
                "it cannot be recovered automatically (the Issue "
                "stays ai-blocked until a human decides) — restore "
                "the trusted 'Orbi opened PR' scene comment or "
                "relabel the Issue ai-fix-needed to resume this same PR"
            ),
        )
    except Exception:
        LOGGER.exception("issue=%s failure reporting failed", number)


def block_repo_config_failure(number: int, source_repo: str,
                              error: ValueError, run_id: str,
                              current_labels=()) -> None:
    """Mark an Issue `ai-blocked` when its repo config is invalid (#527).

    The strict repository schema is a fail-fast precondition: a file that
    exists on the default branch but carries an unknown/host-only key, a
    wrong type or invalid TOML blocks the claim with the concrete reason
    and the offending key names. The failure is scoped to this repository
    only (a sibling pool's valid config is unaffected). A failure of the
    reporting itself is logged, never raised, so the tick still ends
    cleanly (`main` releases the slot in its `finally`).
    """
    try:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=set(current_labels),
        )
        comment_issue(
            number, repo=source_repo,
            body=(
                f"{run_marker(run_id)}\n"
                f"Orbi failed: repository config is invalid: {error}; "
                "this is a fail-fast repository precondition the AI does "
                "not fix "
                "itself — correct the file on the default branch "
                "(remove the host-only key or fix the value) and "
                "relabel the Issue ai-ready for a new run"
            ),
        )
    except Exception:
        LOGGER.exception("issue=%s repo_config_failure_report_failed", number)


def _main_ci_triage_url(repo: str, check_name: str) -> str | None:
    """Find the existing auto-created main CI issue, when present."""
    try:
        issues = list_issues(
            repo, state="all",
            search=f"CI failure: {check_name} on branch main",
            json_fields="number,url,title", limit=20,
        )
    except Exception:
        LOGGER.exception("delivery_ci_triage_lookup_failed repo=%s check=%s",
                         repo, check_name)
        return None
    prefix = f"CI failure: {check_name} on branch main"
    for issue in issues:
        if isinstance(issue, dict) and str(issue.get("title", "")).startswith(prefix):
            url = issue.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _raise_if_preexisting_ci_failure(
        repo: str, failed_names: list[str], base_commit: str | None) -> None:
    """Raise a fast, explicit error when base has the same failed check."""
    if not base_commit:
        return
    base_checks = commit_check_runs(repo, base_commit)
    base_failures = {
        check.get("name") for check in base_checks
        if check.get("status") == "completed"
        and check.get("conclusion") not in ("success", "neutral", "skipped")
    }
    for name in failed_names:
        if name not in base_failures:
            continue
        triage_url = _main_ci_triage_url(repo, name)
        suffix = f"; triage: {triage_url}" if triage_url else ""
        raise PreExistingCIFailure(
            f"delivery gate: main is already red on check '{name}' — "
            f"fix main first{suffix}"
        )


def _report_resume_failure(*, number: int, source_repo: str, run_id: str,
                           error: Exception) -> None:
    """Report a failed resume decision through the terminal path.

    The worktree of this issue exists but its run state cannot be
    verified: continuing on a guessed identity risks a
    silent fresh redo on top of unknown work, and a fresh run would
    lose the existing work. The Issue goes `ai-blocked` ALONE (the
    claim label removed) with the exact reason in the failure comment
    — a human decides (restore or remove the worktree, then re-label).
    """
    apply_label_patch(
        number, repo=source_repo, event=EVENT_BLOCKED,
        current_labels={IN_PROGRESS_LABEL},
    )
    comment_issue(
        number, repo=source_repo,
        body=(
            f"{run_marker(run_id)}\n"
            f"Orbi failed: cannot continue the interrupted run: "
            f"{_failure_detail(error)} (run_id={run_id})"
        ),
    )


def report_delivery_failure(
    exc: BaseException, *, issue: dict, source_repo: str,
    run_id: str | None, pr_url: str | None, worktree: Path | None,
    branch: str | None, role: str, cause: str | None = None,
    action: str = "", reason: str | None = None,
    diagnosis: str | None = None, evidence: bool = False,
    classify: bool = True, current_labels: set[str] | None = None,
    blocked_suffix: str = "", review_round: int | None = None,
    review_scene_block: str | None = None,
    finish: bool = True, publisher: ProgressPublisher | None = None,
) -> str:
    """Report one delivery failure through the classified-failure flow.

    The single implementation of the flow previously hand-copied in
    `verify_resumed_pr`, `_run_review_round` and `process_issue`:
    classify the exception, transition the label (`ai-blocked` ALONE
    for an explicit `UnrecoverableDeliveryError`, `ai-fix-needed` for
    every recoverable failure), assemble the failure body (fixed
    phrase + `cause` + run scene + evidence), prefix the run marker,
    comment the Issue (the PR too on the recoverable branch — the
    terminal blocked state is Issue-only), then publish the milestone
    and the terminal progress scene as a pure bypass. The carried
    failure is named `action`, `reason`, and `diagnosis`; the milestone
    uses the reason so its mobile notification remains useful. Returns
    the outcome, `"blocked"` or `"fix needed"`.

    `classify=False` forces the terminal branch (the implement-phase
    handler: every failure reaching it is terminal by design — the
    recoverable Pi failures have their own handler); the body is then
    the plain `Orbi failed:` template and the run scene is
    space-joined to the FIRST body line, where `format_status_comment`
    lifts its fields into the structured-alert field block.
    `current_labels` pins the label-patch source (the implement-phase
    handler derives the current delivery label locally); None reads
    the labels from GitHub. `blocked_suffix` extends the classified
    blocked body (the resume verification's preserved-PR note).
    `review_round` pins the terminal scene's round (the implement
    phase has none); None computes `review_rounds_so_far` lazily
    inside the finish publish (a history-read failure there is bypass,
    never a second failure). `finish=False` skips the progress scene
    (the tracked progress comment does not exist and the milestone
    alone carries the notification). `publisher` is the run's LIVE
    progress publisher when the caller holds one: its `finish` then
    PATCHes the tracked comment directly instead of relocating it by
    the run marker. `evidence` appends the bounded
    `_failure_evidence` block after the scene.

    Label and ordinary comment failures retain their existing caller
    semantics. A malformed deduplicated comment URL posts a fresh failure
    comment and emits `failure_comment_id_unavailable`; an update failure
    is logged as `failure_comment_update_failed`. Neither escapes this
    function, so one malformed comment cannot stop the tick.

    The #825 dead-loop guard (classified recoverable failures only):
    the same (run_id, failure fingerprint) recurring to
    `FAILURE_STREAK_LIMIT` consecutive failures escalates to the
    terminal `ai-blocked` outcome with the count and the
    unchanged-precondition verdict in the comment, and an identical
    failure that was already reported bumps the hidden repeat counter
    of its existing comment in place (the streak scan reads that
    counter as the occurrence count) instead of posting a second
    comment; the journal records the repeat. The history read fails
    open — a read failure degrades to the plain classified report,
    never a second failure of the reporting path.
    """
    number = int(issue["number"])
    title = issue["title"]
    priority = issue_priority(issue)
    # Keep `cause` as a compatibility input for callers outside the three
    # production paths; new callers provide the three named parts.
    if reason is None:
        reason = cause or _failure_detail(exc)
    if diagnosis is None:
        diagnosis = cause or _failure_detail(exc)
    blocked = not classify or is_unrecoverable_failure(exc)
    # The #825 dead-loop guard, recoverable failures only (blocked is
    # already terminal; `classify=False` is the implement handler's
    # terminal template): the same (run_id, failure fingerprint)
    # recurring to `FAILURE_STREAK_LIMIT` consecutive failures is a
    # dead loop, not a transient error — the escalation turns it
    # `ai-blocked`, the human decision point. The history read fails
    # open: the guard must never break the failure report itself.
    fingerprint = runner_health.failure_fingerprint(exc)
    reported_failure: dict | None = None
    if classify and not blocked and run_id:
        try:
            history = issue_comments(number, repo=source_repo)
        except Exception:
            LOGGER.exception("issue=%s failure history read failed", number)
            event(
                "failure_history_read_failed", issue=number,
                run_id=run_id,
            )
        else:
            reported_failure = _reported_failure_comment(
                history, run_id, fingerprint,
            )
            streak = _failure_streak(history, run_id, fingerprint)
            if streak + 1 >= FAILURE_STREAK_LIMIT:
                blocked = True
                reason = (
                    f"{reason}; the same failure has now occurred "
                    f"{streak + 1} consecutive times for run_id={run_id} "
                    f"(fingerprint {fingerprint}) with unchanged "
                    "preconditions — a dead loop, not a transient error"
                )
                event(
                    "failure_streak_escalated", level=logging.ERROR,
                    issue=number, run_id=run_id, streak=streak + 1,
                    fingerprint=fingerprint,
                )

    # The machine-readable record travels with every failure comment:
    # a status reader parses the block, a human reads the hierarchy.
    # `failure_record` is computed once here so the block and the
    # reader-facing Action / Reason cannot disagree.
    failure_record = _classify_failure(
        exc, outcome="blocked" if blocked else "fix_needed",
    )

    def scene_line() -> str | None:
        """The run scene for the body, or None when omitted.

        The classified reporters degrade a failed snapshot read to the
        '-' placeholder (the debug entry still carries worktree and
        branch); the implement-phase first-line scene is
        omitted entirely on a failed read — the structured-alert field
        block then shows only the failure's own fields (the pinned
        #256 isolation).
        """
        if classify:
            snapshot = (
                _snapshot_or_placeholder(worktree / ".pi-session", number=number)
                if worktree is not None else dict(_SNAPSHOT_PLACEHOLDER)
            )
        else:
            if worktree is None:
                return None
            try:
                snapshot = activity_snapshot(worktree / ".pi-session")
            except Exception:
                LOGGER.exception("issue=%s activity scene failed", number)
                return None
            if snapshot is None:
                snapshot = dict(_SNAPSHOT_PLACEHOLDER)
        return _failure_scene(
            snapshot, run_id=run_id or "-",
            issue=issue_context(source_repo, number), role=role,
            branch=branch or "-", worktree=str(worktree), pr_url=pr_url,
        )

    labels = (
        current_labels if current_labels is not None
        else issue_labels(number, source_repo)
    )
    evidence_detail = _failure_evidence(worktree, exc) if evidence else ""
    if blocked:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels=labels,
        )
        scene = scene_line() or "- run: `-`"
        body = _failure_comment_body(
            outcome="blocked", action=action, reason=reason,
            diagnosis=diagnosis, scene=scene, evidence=evidence_detail,
            pr_url=pr_url, issue=issue_context(source_repo, number),
            run_id=run_id or "-", failure_record=failure_record,
        )
        if classify:
            body = body.replace(
                "</details>",
                f"\n{_BLOCKED_PRECONDITION_PHRASE.lstrip('; ')}\n</details>",
                1,
            )
        if blocked_suffix:
            body = body.replace(
                "</details>",
                f"\n{blocked_suffix.lstrip('; ')}\n</details>", 1,
            )
        outcome = "blocked"
    else:
        apply_label_patch(
            number, repo=source_repo, event=EVENT_FIX_NEEDED,
            current_labels=labels,
        )
        scene = scene_line() or "- run: `-`"
        if review_scene_block is not None:
            scene += f"\n{_redact_local_paths(review_scene_block)}"
        body = _failure_comment_body(
            outcome="fix needed", action=action, reason=reason,
            diagnosis=diagnosis, scene=scene, evidence=evidence_detail,
            pr_url=pr_url, issue=issue_context(source_repo, number),
            run_id=run_id or "-", failure_record=failure_record,
        )
        outcome = "fix needed"
    if len(body) > FAILURE_COMMENT_MAX_CHARS:
        # The comment body is capped; the cut preserves the
        # cause-first head and names the full session log for the part
        # that was dropped.
        session_file = _latest_session_file(worktree)
        body = (
            body[:FAILURE_COMMENT_MAX_CHARS]
            + f"\n\n_[comment truncated at {FAILURE_COMMENT_MAX_CHARS} "
            f"chars; full session log: "
            f"{'local session log' if session_file else '<unavailable>'}]_"
        )
    if run_id:
        # The hidden fingerprint marker rides under the run marker on
        # the recoverable path only: a blocked Issue is terminal, so
        # its comment never joins a future streak scan (a human's
        # blocked -> ai-fix-needed recovery starts a fresh streak).
        fail_marker_line = "" if blocked else (
            f"{failure_marker(fingerprint)}\n"
        )
        body = f"{run_marker(run_id)}\n{fail_marker_line}{body}"
    post_new_comment = True
    if run_id and reported_failure is not None and not blocked:
        # The #825 dedup: `gh issue view --json comments` returns a GraphQL
        # node id, but the PATCH route requires the REST id. The comment URL
        # already carries that id, so do not make a second API request.
        comment_id = reported_failure.get("id")
        if isinstance(comment_id, int) and not isinstance(comment_id, bool):
            rest_comment_id = comment_id
        else:
            url = reported_failure.get("url")
            match = (
                re.search(r"#issuecomment-(\d+)$", url)
                if isinstance(url, str) else None
            )
            rest_comment_id = int(match.group(1)) if match is not None else None
        if rest_comment_id is None:
            # A malformed or missing URL is data loss in the dedup metadata,
            # not a delivery failure. Preserve the pre-#825 behavior so the
            # current failure is still recorded and the tick continues.
            unavailable_reason = (
                "comment URL has no #issuecomment-<digits> suffix"
            )
            LOGGER.warning(
                "issue=%s failure comment id unavailable: %s",
                number, unavailable_reason,
            )
            event(
                "failure_comment_id_unavailable", level=logging.ERROR,
                issue=number, run_id=run_id, reason=unavailable_reason,
            )
        else:
            # A resolved existing comment owns this occurrence even if its
            # PATCH fails. Preserve the existing update-failure behavior:
            # log the failure without attempting a second ordinary comment.
            post_new_comment = False
            try:
                update_issue_comment(
                    rest_comment_id, repo=source_repo,
                    # Apply the increment to the newly assembled body, not the
                    # stale first report. This preserves scene/retry fields
                    # added by this attempt while retaining the dedup counter.
                    body=bump_failure_repeat(body, fingerprint),
                )
            except Exception:
                LOGGER.exception(
                    "issue=%s failure comment update failed", number,
                )
                event(
                    "failure_comment_update_failed", level=logging.ERROR,
                    issue=number, run_id=run_id,
                )
            else:
                event(
                    "failure_comment_deduplicated", issue=number,
                    run_id=run_id, fingerprint=fingerprint,
                )
    if post_new_comment:
        comment_issue(number, repo=source_repo, body=body)
        if pr_url and not blocked:
            # The recoverable scene is written to the PR too:
            # the next review session and any human watcher see it where
            # the delivery lives. A blocked Issue is terminal — Issue only.
            comment_pr(_pr_number(pr_url), repo=source_repo, body=body)
    if run_id:
        # One publisher for the milestone and the terminal scene: the
        # run's LIVE publisher when the caller holds one (its `finish`
        # PATCHes the tracked comment directly, comment id already
        # known), a fresh one otherwise (`finish` then locates-or-
        # creates the tracked comment by the run marker).
        target = (
            publisher if publisher is not None
            else ProgressPublisher(
                number, source_repo, run_id, run_command=run_command,
            )
        )
        publish = functools.partial(
            _safe_publish, run_id=run_id, issue=number,
            source_repo=source_repo, role=role,
        )
        if outcome == "blocked":
            publish(action=lambda: target.milestone(
                f"blocked: {_failure_summary(reason)}",
                block=failure.render(failure_record),
            ))
            if classify:
                finish_failure = reason
                next_step = (
                    "fix the precondition above (see the reason) and "
                    "relabel the Issue ai-fix-needed to resume this "
                    "same PR"
                )
            else:
                finish_failure = reason
                next_step = (
                    "fix the failure above and re-run this Issue (a "
                    "new run id is created automatically)"
                )
            finish_outcome = "blocked"
        else:
            publish(action=lambda: target.milestone(
                f"fix needed: {_failure_summary(reason)}",
                block=failure.render(failure_record),
            ))
            finish_failure = reason
            next_step = (
                "the next tick resumes the same run, branch, worktree "
                "and PR automatically (the Issue stays ai-fix-needed)"
            )
            finish_outcome = "fix needed"
        if finish:
            publish(action=lambda: target.finish(_finish_progress_body(
                number=number, title=title, run_id=run_id, role=role,
                branch=branch, worktree=worktree, pr_url=pr_url,
                review_round=(
                    review_round if review_round is not None
                    else review_rounds_so_far(
                        issue_comments(number, repo=source_repo),
                    )
                ),
                priority=priority, detail=finish_failure,
                next_step=next_step, outcome=finish_outcome,
                source_repo=source_repo, action=action,
                reason=reason, diagnosis=diagnosis,
            )))
    return outcome
