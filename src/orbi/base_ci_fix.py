"""What the engine DOES about a base-branch-red scene (Issues #1825, #2203).

Split out of `base_ci_red` (Issue #2203) because that module sits at its
size-ratchet ceiling; the scene there owns the record, the comment and the
eligibility rule. This module owns the actions:

* every path that OPENS the fix Issue — the engine's own run when the
  scene is recorded (`ensure_fix_issues`, once per scene) and the
  in-ticket `/orbi fix-base` command (`FIX_BASE_COMMAND`). Both call
  `open_fix_issues`, the ONE lookup/create loop, so a command run and an
  engine run can never open two Issues for one check and each reply says
  which Issue it reused;
* the pause comment that names them (`scene_comment`), so the reader never
  has to reply a command the engine already ran;
* the bounded recovery scan (`recover`): run a pending command, read the
  base head once, and take `ai-blocked` to `ai-fix-needed` when every
  recorded check is green.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from orbi import blocked_scan, failure, language
from orbi.base_ci_red import (
    PASSING_CONCLUSIONS, REMINDER_AFTER_SECONDS,
    STATE_MERGED, STATE_RESUMING, BaseCIRed, FailingCheck, comment_body,
    edit_state, failing_checks, open_triage_issue, paused_scene, remind,
    scene_receipt, triage_title,
)
from orbi.delivery_labels import (
    BLOCKED_LABEL, FIX_NEEDED_LABEL, P0_LABEL, READY_LABEL,
)
from orbi.github import (
    branch_head_sha, comment_issue, commit_check_runs, edit_issue,
    issue_comments, pr_view,
)
from orbi.journal import LOGGER, event, run_command
from orbi.pi_activity import parse_iso_utc
from orbi.progress import fenced
from orbi.ticket_command import (
    CommandSpec, TicketCommandError, command_comment_key,
    command_receipt_marker, execute_command, failure_reason,
    runner_receipt,
)


def _triage_body(check: FailingCheck, record: BaseCIRed,
                 language_code: str) -> str:
    parts = [
        language.text(language_code, "base_ci_red.triage_check",
                      name=check.name, base_branch=record.base_branch),
        language.text(language_code, "base_ci_red.triage_reason",
                      base_branch=record.base_branch,
                      base_sha=record.base_sha),
    ]
    if check.url:
        parts.append(language.text(language_code, "base_ci_red.check_run",
                                   name=check.name, url=check.url))
    if check.errors:
        parts.append(
            f"{language.text(language_code, 'base_ci_red.triage_errors')}\n"
            f"{fenced('\n'.join(check.errors))}"
        )
    return "\n\n".join(parts)


def _create_triage_issue(repo: str, check: FailingCheck, record: BaseCIRed,
                         *, milestone: str | None, dispatch_label: str,
                         language_code: str) -> int:
    command = [
        "gh", "issue", "create", "--repo", repo,
        "--title", triage_title(check.name, record.base_branch),
        "--body", _triage_body(check, record, language_code),
        "--label", dispatch_label,
        "--label", P0_LABEL,
    ]
    if milestone:
        command += ["--milestone", milestone]
    url = run_command(command, timeout=30).strip()
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise TicketCommandError(
            "create issue", f"gh returned no issue URL: {url!r}",
        ) from None


def open_fix_issues(
    repo: str, issue_number: int, record: BaseCIRed, *,
    milestone: str | None = None, dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> list[str]:
    """Open (or reuse) ONE open fix Issue per failing base check.

    The ONE creation path: `/orbi fix-base` posts the returned lines as
    its receipt comment, and the scene record (Issue #2203) renders the
    same lines into the pause comment — same lookup, same create, same
    journal line, so a command run and an engine run can never open two
    Issues for one check.

    Raises `TicketCommandError` when a lookup fails: opening a second
    Issue beside one the read could not see is never the smaller risk. An
    empty evidence read returns the one "no evidence" sentence — no Issue
    is invented for a check GitHub no longer reports.
    """
    checks = failing_checks(repo, list(record.failing_checks), record.base_sha)
    if not checks:
        return [language.text(
            language_code, "base_ci_red.triage_no_evidence",
            base_branch=record.base_branch,
        )]
    lines: list[str] = []
    for check in checks:
        try:
            existing = open_triage_issue(
                repo, check.name, record.base_branch)
        except Exception as exc:
            # Never open a second Issue beside one this read could not
            # see: the caller fails its own step and reports the reason.
            raise TicketCommandError(
                "look up the triage Issue", str(exc)) from exc
        if existing is not None and isinstance(existing.get("number"), int):
            number = existing["number"]
            key = "base_ci_red.triage_reused"
        else:
            number = _create_triage_issue(
                repo, check, record, milestone=milestone,
                dispatch_label=dispatch_label, language_code=language_code,
            )
            key = "base_ci_red.triage_created"
            event(
                "base_ci_red_triage", repo=repo, issue=f"#{issue_number}",
                created=f"#{number}", check=check.name,
                base_branch=record.base_branch,
            )
        lines.append(language.text(
            language_code, key, number=number, check=check.name,
            base_branch=record.base_branch,
        ))
    return lines


def ensure_fix_issues(
    repo: str, issue_number: int, record: BaseCIRed, *,
    milestone: str | None = None, dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> list[str] | None:
    """The engine-initiated run: the fix Issue(s), ONCE per scene.

    Returns the reader-facing lines the pause comment renders, or
    `None` when this scene already ran — the pause comment it already
    wrote carries `scene_receipt` and only a runner-authored receipt
    counts (`runner_receipt`, the same rule the command's receipt uses),
    so a later tick that still sees the same pause opens nothing, comments
    nothing and repeats no receipt.

    A failed read or create is a BYPASS: the pause is already decided and
    the person still has to read it, so the failure is logged and the
    comment renders the pre-#2203 repair text (`/orbi fix-base`) instead
    of turning a recorded scene into an unreported one.
    """
    try:
        receipt = scene_receipt(record)
        if runner_receipt(issue_comments(issue_number, repo=repo), receipt):
            return None
        return open_fix_issues(
            repo, issue_number, record, milestone=milestone,
            dispatch_label=dispatch_label, language_code=language_code,
        )
    except Exception as exc:
        LOGGER.exception(
            "issue=%s base_ci_red scene fix Issues failed", issue_number,
        )
        event(
            "base_ci_red_fix_issues_failed", level=logging.ERROR,
            issue=issue_number, repo=repo, reason=failure_reason(exc),
        )
        return None


# --------------------------------------------------------------------------
# The base head, the green decision and the recovery scan
# --------------------------------------------------------------------------

def _base_head(repo: str, pr_number: int,
               base_branch: str) -> tuple[str, str]:
    """The paused PR's state and the base branch's LIVING head.

    The branch's current head, never the PR's recorded `baseRefOid`: that
    field is the base snapshot the PR was compared against, so the fix a
    maintainer pushes to the base AFTER the pause would never be observed
    and the delivery would never resume.
    """
    state = pr_view(pr_number, "state", repo=repo)
    return str(state.get("state") or ""), branch_head_sha(repo, base_branch)


def _newest_runs(runs: list[dict]) -> dict[str, dict]:
    """One current run per check name.

    A RE-RUN of a check adds a second run under the same name, and the
    check-runs endpoint lists a commit's runs newest first (descending
    `started_at`/`id`, verified against the live endpoint), so the FIRST
    occurrence is the current verdict. Reading the last one would keep an
    old failure alive after the maintainer re-ran the check to green and
    the delivery would never resume.
    """
    by_name: dict[str, dict] = {}
    for run in runs:
        by_name.setdefault(str(run.get("name")), run)
    return by_name


def _base_is_green(repo: str, record: BaseCIRed,
                   base_head: str) -> bool:
    """True when every check in the record concluded green on the base.

    A pending check is not green: the scan waits, it never guesses.
    """
    by_name = _newest_runs(commit_check_runs(repo, base_head))
    for name in record.failing_checks:
        run = by_name.get(name)
        if run is None:
            return False
        if run.get("status") != "completed":
            return False
        if run.get("conclusion") not in PASSING_CONCLUSIONS:
            return False
    return True


def _aged_out(comment: dict, now: datetime) -> bool:
    created = comment.get("createdAt")
    stamp = parse_iso_utc(created) if isinstance(created, str) else None
    if stamp is None:
        return False
    return (now - datetime.fromtimestamp(stamp, tz=timezone.utc)
            ).total_seconds() >= REMINDER_AFTER_SECONDS


def recover(repo: str, *, language_code: str,
            milestone: str | None = None,
            dispatch_label: str = READY_LABEL,
            now: datetime | None = None) -> list[int]:
    """Advance every paused base-branch-red delivery once.

    One bounded scan per tick, before the resume scan: an `ai-blocked`
    Issue is a terminal state no claim scan picks up, so this is the only
    path back. The state it walks is the tick's ONE read
    (`orbi.blocked_scan`, Issue #2218). The base branch is the BLOCK's (the
    configured one the delivery reported) — never a hard-coded `main`.
    For each Issue whose LATEST TRUSTED failure is the `base_ci_red` one
    and whose record in that SAME comment is `waiting` (an older record a
    newer trusted failure superseded — including a newer failure that
    carries no record or no base-CI block — and a copied record from an
    author without write permission, are never eligible — Issue #1833):

    1. run the in-ticket `/orbi fix-base` command (its own permission
       gate and receipts);
    2. read the CURRENT base head once and the record's checks against it:
       all green -> `ai-blocked` becomes `ai-fix-needed` (the existing
       resume flow absorbs the base and merges) and the comment says so;
       a merged PR -> the comment says merged;
    3. still red/pending past `REMINDER_AFTER_DAYS` -> one reminder and
       `stale` (polling stops; a human relabel still recovers the Issue).

    Returns the Issue numbers that were resumed.
    """
    stamp = now or datetime.now(timezone.utc)
    reads = blocked_scan.blocked_reads(repo)
    resumed: list[int] = []
    for issue in reads.blocked_issues():
        number = issue["number"]
        comments = reads.comments(number)
        scene = paused_scene(comments, repo=repo)
        if scene is None:
            continue
        record, comment = scene
        execute_command(
            repo, number, FIX_BASE_COMMAND, comments=comments,
            language_code=language_code, record=record,
            milestone=milestone, dispatch_label=dispatch_label,
        )
        try:
            pr_state, base_head = _base_head(
                repo, record.pr, record.base_branch)
        except Exception:
            LOGGER.exception(
                "base_ci_red_pr_read_failed issue=%s pr=%s repo=%s",
                number, record.pr, repo,
            )
            continue
        if pr_state == "MERGED":
            edit_state(repo, number, comment, STATE_MERGED,
                        language_code=language_code)
            event(
                "base_ci_red_merged", issue=number, pr=record.pr,
                base_branch=record.base_branch,
            )
            continue
        if _base_is_green(repo, record, base_head):
            edit_issue(number, repo=repo, add=FIX_NEEDED_LABEL,
                       remove=BLOCKED_LABEL)
            edit_state(repo, number, comment, STATE_RESUMING,
                        language_code=language_code)
            event(
                "base_ci_red_recovered", issue=number, pr=record.pr,
                base_branch=record.base_branch, base_head=base_head,
            )
            reads.retire(number)
            resumed.append(number)
            continue
        if _aged_out(comment, stamp):
            remind(repo, number, record, comment,
                    language_code=language_code)
    return resumed


# --------------------------------------------------------------------------
# The pause comment: naming the fix Issues the engine already opened
# --------------------------------------------------------------------------

def scene_comment(
    repo: str, issue: dict, record: BaseCIRed, checks: list[FailingCheck],
    *, failure_record: failure.Failure, language_code: str,
    status_url: str | None = None, milestone: str | None = None,
    dispatch_label: str = READY_LABEL,
) -> str:
    """The base-branch-red comment, with the fix Issues Orbi opened itself.

    Recording the scene and opening its fix Issues are ONE step (Issue
    #2203): the comment names what `ensure_fix_issues` opened or reused,
    and that run is once per scene — its receipt rides in this same
    comment. A failed run is a bypass (it is logged inside
    `ensure_fix_issues`), so the pause the delivery already decided still
    reaches the reader, with the command's own repair offered instead.
    """
    number = int(issue["number"])
    return comment_body(
        record, checks, failure_record=failure_record,
        language_code=language_code, issue_number=number,
        issue_title=issue.get("title"), status_url=status_url,
        fix_issues=ensure_fix_issues(
            repo, number, record, milestone=milestone,
            dispatch_label=dispatch_label, language_code=language_code,
        ),
    )


def _fix_base_context(
    *, repo: str, comments: list[dict], record: BaseCIRed | None = None,
    **_context: object,
) -> dict[str, object]:
    """The command's own context: the record this ticket pauses on.

    The comment scan located the record while walking the ticket and hands
    it over; a terminal caller (`orbi fix-base`, Issue #1930) has no such
    scan, so the command reads the SAME record from the ticket itself —
    the CLI never passes a placeholder, and a caller that already has it
    costs no second read.
    """
    if record is not None:
        return {}
    scene = paused_scene(comments, repo=repo)
    return {"record": scene[0] if scene is not None else None}


def _fix_base_validate(
    argument: str, *, language_code: str = language.DEFAULT_LANGUAGE,
    record: BaseCIRed | None = None, **_context: object,
) -> str | None:
    """The command's own business rule: a paused scene to fix.

    The shared dispatcher evaluates EVERY registered command on every
    ticket that runs commands (`process_commands` runs each one through
    `execute_command`, Issue #1294), so "is there a block to fix at all" is
    this command's own precondition: without one the reader gets this
    receipt instead of a handler running against state the ticket never
    carried. The command takes no argument (Issue #1922): the `/orbi`
    prefix and `fix-base` are matched by the shared layer, not by this
    rule.
    """
    if not isinstance(record, BaseCIRed):
        return language.text(
            language_code, "command.reason_fix_base_no_block",
        )
    return None


def _fix_base_apply(
    argument: str, *, repo: str, issue_number: int, command_comment: dict,
    comments: list[dict], record: BaseCIRed | None = None,
    milestone: str | None = None, dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE,
    **_context: object,
) -> None:
    """Open (or reuse) ONE open fix Issue per failing base check.

    Idempotent per COMMENT: the reply carries the command layer's own
    receipt marker, so a second tick that still sees the same command
    comment does nothing (the block stays `waiting` until the base turns
    green, so the comment is visible for several ticks). Only a receipt
    THIS runner wrote counts (`runner_receipt`): the marker is public
    text, so a copied one must never read as "the fix Issue was opened"
    (Issue #1992).

    The lookup/create loop is `open_fix_issues`, the SAME code the scene
    record runs by itself (Issue #2203): a fix Issue the engine opened is
    therefore reused here, and the reply says so.
    """
    if not isinstance(record, BaseCIRed):
        # The layer's contract: a handler that cannot run raises its own
        # step error (never a bare TypeError) so the dispatcher posts the
        # shared failure receipt.
        raise TicketCommandError(
            "locate base-ci-red block",
            language.text(language_code, "command.reason_fix_base_no_block"),
        )
    receipt = (
        f"<!-- {command_receipt_marker(FIX_BASE_COMMAND)} "
        f"comment={command_comment_key(command_comment)} -->"
    )
    if runner_receipt(comments, receipt):
        return
    lines = open_fix_issues(
        repo, issue_number, record, milestone=milestone,
        dispatch_label=dispatch_label, language_code=language_code,
    )
    comment_issue(
        issue_number, repo=repo,
        body="\n".join(lines) + "\n\n" + receipt,
    )


FIX_BASE_COMMAND = CommandSpec(
    name="fix_base",
    subcommand="fix-base",
    arity=0,
    permission="write",
    validate=_fix_base_validate,
    apply=_fix_base_apply,
    usage="/orbi fix-base",
    description=(
        "open a fix Issue for the base checks that pause this delivery"
    ),
    context=_fix_base_context,
)
