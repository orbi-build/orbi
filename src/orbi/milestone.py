"""Milestone bookkeeping and progress milestones."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from orbi.delivery_labels import READY_LABEL, RELEASE_LABEL
from orbi.gitops import acquire_base_sync_lock
from orbi.github import (
    close_milestone, list_issues, list_milestones, milestone_open_issues,
    parse_paginated_issue_array, run_gh_read_command, run_gh_write_command,
)
from orbi.journal import (
    LOGGER, MilestoneReconcileError, classify_milestone_error, event,
    run_command, single_line,
)
from orbi.milestone_command import (
    _land_active_milestone, process_milestone_commands,
    rewrite_active_milestone_line,
)
from orbi.progress import ProgressPublisher, read_test_result
from orbi.repo_config import REPO_CONFIG_PATH, RepoPolicy

MILESTONE_RECONCILE_RETRY_SECONDS = 60 * 60
_MILESTONE_FAILURE_DIR = "milestone-reconcile-failures"
_MILESTONE_FAILURE_STATUSES = (401, 404)

# The waiting states one active Milestone can be in on the idle path
# (Issue #856). `in_progress` and `nothing_to_do` need no human reply;
# the two `awaiting_*` states each own one decision notice the maintainer
# answers with `/milestone <version>`.
MILESTONE_IN_PROGRESS = "in_progress"
MILESTONE_AWAITING_RELEASE = "awaiting_release"
MILESTONE_AWAITING_NEXT = "awaiting_next"
MILESTONE_NOTHING_TO_DO = "nothing_to_do"

# The waiting state an existing decision notice was opened for. Only
# `next` matches the pre-#856 notice; `release` is the release-ticket
# confirmation.
_WAITING_NEXT = "next"
_WAITING_RELEASE = "release"


def classify_milestone_waiting(
    *,
    state: object,
    open_issues: int,
    release_ticket_exists: bool,
    candidates: list,
    auto_next_milestone: bool,
    release_confirmation: bool,
) -> str:
    """Classify the active Milestone's idle waiting state (Issue #856).

    Pure table over the already-resolved facts, so the Runner's idle path
    reads GitHub once and this function owns the decision:

    - an OPEN Milestone still carrying Issues is `in_progress`;
    - an OPEN Milestone with no open Issue and `release_confirmation` on
      but no `ai-release` Issue yet is `awaiting_release` — the maintainer
      is told to open the release ticket;
    - a CLOSED Milestone with a higher open candidate is `awaiting_next`
      when `auto_next_milestone` is false (the existing notice) and
      `nothing_to_do` when the engine advances by itself;
    - everything else is `nothing_to_do`.
    """
    if state == "open":
        if open_issues > 0:
            return MILESTONE_IN_PROGRESS
        if release_confirmation and not release_ticket_exists:
            return MILESTONE_AWAITING_RELEASE
        return MILESTONE_NOTHING_TO_DO
    if not candidates or auto_next_milestone:
        return MILESTONE_NOTHING_TO_DO
    return MILESTONE_AWAITING_NEXT


def _release_ticket_exists(repo: str, version: str) -> bool:
    """Whether an `ai-release` Issue already exists in ``version``.

    The same search `_ensure_command_release_ticket` uses to stay
    idempotent: `gh issue list` over one Milestone's release label.
    """
    issues = list_issues(
        repo, state="all",
        search=f'label:{RELEASE_LABEL} milestone:"{version}"',
        json_fields="number", limit=1, timeout=30,
    )
    return bool(issues)


def arm_release_ticket(
    repo: str, active_milestone: str,
    dispatch_label: str = READY_LABEL,
) -> None:
    """Arm one open release ticket for the current milestone on idle.

    The ticket is armed with the repo's dispatch label — the same label
    `release_fallback_search` requires before the ticket can be claimed —
    so callers must resolve it from the repository policy, not assume the
    host default. The search exclusion uses the same label: an armed
    ticket carries it, and a ticket polluted by a stale arm (labelled
    `ai-ready` under a custom-dispatch-label repo) no longer matches the
    exclusion, so the next idle tick re-arms and heals it.

    This is an idle-path bypass: callers deliberately catch failures so a
    GitHub label operation cannot change the outcome of the main tick.
    """
    search = (
        f"label:{RELEASE_LABEL} -label:{dispatch_label} "
        f'milestone:"{active_milestone}"'
    )
    issues = list_issues(
        repo, state="open", search=search,
        json_fields="number", limit=200, timeout=30,
    )
    if not issues:
        return
    number = issues[0].get("number")
    if not isinstance(number, int):
        raise RuntimeError(f"release ticket has invalid issue number: {number!r}")
    run_gh_write_command([
        "gh", "issue", "edit", str(number), "--repo", repo,
        "--add-label", dispatch_label,
    ], timeout=30, command_runner=run_command)
    event(
        "release_ticket_armed", issue=f"#{number}",
        milestone=active_milestone,
    )


def reconcile_milestone_on_idle(
    repo: str, active_milestone: str, config_path: Path,
    repo_dir: Path | None = None,
    *, auto_next_milestone: bool = True, release_confirmation: bool = False,
    parse_version_title: Callable[[object], tuple[int, int, int] | None],
    policy: RepoPolicy | None = None,
    policy_path: str = REPO_CONFIG_PATH,
    base_branch: str = "main",
    dispatch_label: str = READY_LABEL,
    version_file: str | None = None,
) -> tuple[str, str | None]:
    """One idle-path Milestone bookkeeping entry point (Issue #856).

    Arms any existing release ticket, then classifies the Milestone's
    waiting state and acts on it (`advance_active_milestone_on_idle`).
    The Runner makes ONE call inside ONE bypass `try/except`, so a new
    bookkeeping step never becomes a second, separately-failing call.
    """
    try:
        arm_release_ticket(
            repo, active_milestone, dispatch_label=dispatch_label,
        )
    except Exception:
        LOGGER.exception(
            "release_ticket_arm_failed repo=%s milestone=%s",
            repo, active_milestone,
        )
    return advance_active_milestone_on_idle(
        repo, active_milestone, config_path, repo_dir,
        auto_next_milestone=auto_next_milestone,
        release_confirmation=release_confirmation,
        parse_version_title=parse_version_title,
        policy=policy, policy_path=policy_path,
        base_branch=base_branch, dispatch_label=dispatch_label,
        version_file=version_file,
    )


def _milestone_failure_marker(state_dir: Path, repo: str, status: int) -> Path:
    repo_key = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    return state_dir / _MILESTONE_FAILURE_DIR / f"{repo_key}-{status}"


def milestone_reconcile_due(
    state_dir: Path, repo: str, *, now: float | None = None,
) -> bool:
    """Return whether a failed repo is due for its bounded retry.

    The Runner is a fresh process on every timer tick, so an in-memory guard
    cannot reduce cross-tick requests or journal noise.  A tiny marker under
    the existing ``.orbi`` state directory suppresses retries for one hour;
    success clears the marker and restores the normal every-tick sweep.
    """
    current = time.time() if now is None else now
    markers = [
        _milestone_failure_marker(state_dir, repo, status)
        for status in _MILESTONE_FAILURE_STATUSES
    ]
    modified: list[float] = []
    for marker in markers:
        try:
            modified.append(marker.stat().st_mtime)
        except FileNotFoundError:
            continue
        except OSError:
            return True
    if not modified:
        return True
    if current - max(modified) < MILESTONE_RECONCILE_RETRY_SECONDS:
        return False
    for marker in markers:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return True
    return True


def record_milestone_reconcile_failure(
    state_dir: Path, repo: str, error: MilestoneReconcileError,
) -> None:
    marker = _milestone_failure_marker(state_dir, repo, error.status)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    except OSError:
        # State persistence is a log-noise bypass: never let it decide the
        # delivery outcome.  Emit the classified failure so auth loss remains
        # observable even on a read-only state directory.
        pass
    else:
        os.close(descriptor)
    kind = {
        401: "milestone_reconcile_auth_failed",
        404: "milestone_reconcile_not_found",
    }[error.status]
    event(
        kind, level=logging.ERROR, repo=repo, status=error.status,
        operation=error.operation, stderr=single_line(error.stderr),
    )


def clear_milestone_reconcile_failure(state_dir: Path, repo: str) -> None:
    for status in _MILESTONE_FAILURE_STATUSES:
        marker = _milestone_failure_marker(state_dir, repo, status)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.exception(
                "milestone_reconcile_suppression_clear_failed repo=%s path=%s",
                repo, marker,
            )


def sync_active_milestone_variable(
    repo: str, milestone: str | None, *,
    run_command: Callable[..., str] | None = None,
) -> None:
    """Keep the triage workflow's read-only milestone variable current.

    This is deliberately a bypass: a GitHub variable outage must not stop
    the Runner's issue delivery tick.  The workflow consumes this value via
    ``vars.ORBI_ACTIVE_MILESTONE`` and the triage script resolves its title.
    """
    command_runner = run_command or globals()["run_command"]
    endpoint = f"repos/{repo}/actions/variables/ORBI_ACTIVE_MILESTONE"
    try:
        # The read's 404 is the designed absent branch (a repo without the
        # variable is the normal state), so its generic command_failed line
        # stays at DEBUG — same contract as the repo_config contents read.
        raw = command_runner(
            ["gh", "api", endpoint], timeout=30,
            failure_log_level=logging.DEBUG,
        )
        current = json.loads(raw)
        if not isinstance(current, dict):
            raise ValueError("variable response is not an object")
        if milestone is None:
            command_runner(
                ["gh", "api", "-X", "DELETE", endpoint], timeout=30,
            )
            event("active_milestone_variable_removed", repo=repo)
            return
        if current.get("value") == milestone:
            event("active_milestone_variable_unchanged", repo=repo)
            return
        command_runner(
            ["gh", "api", "-X", "PATCH", endpoint, "-f", f"name=ORBI_ACTIVE_MILESTONE",
             "-f", f"value={milestone}"], timeout=30,
        )
        event("active_milestone_variable_updated", repo=repo)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1 or "404" not in (exc.stderr or ""):
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
            return
        if milestone is None:
            event("active_milestone_variable_absent", repo=repo)
            return
        try:
            command_runner(
                ["gh", "api", "-X", "POST", "repos/{}/actions/variables".format(repo),
                 "-f", "name=ORBI_ACTIVE_MILESTONE", "-f", f"value={milestone}"],
                timeout=30,
            )
            event("active_milestone_variable_created", repo=repo)
        except Exception:
            LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)
    except Exception:
        LOGGER.exception("active_milestone_variable_sync_failed repo=%s", repo)

def validate_active_milestone(repo: str, active_milestone: str | None) -> None:
    """Validate the configured Milestone before a tick can claim work.

    A missing title is an unambiguous configuration error and fails fast.
    A closed title is reported as a fact only: maintainers may intentionally
    leave it configured during release wind-down or while preparing another
    line of work (Issue #1185 correction).
    """
    if active_milestone is None:
        return

    milestones = list_milestones(repo, timeout=30)
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and milestone.get("title") == active_milestone
    ]
    if not matches:
        open_titles = [
            str(milestone.get("title"))
            for milestone in milestones
            if isinstance(milestone, dict)
            and milestone.get("state") == "open"
        ]
        event(
            "active_milestone_missing", level=logging.ERROR,
            configured=active_milestone, repo=repo, state="absent",
            open_milestones=", ".join(open_titles) or "(none)",
            fix=("set active_milestone to an exact existing title, or remove "
                 "the field"),
        )
        raise RuntimeError(
            f"active_milestone_missing configured={active_milestone!r} "
            f"repo={repo} state=absent open_milestones="
            f"{', '.join(open_titles) or '(none)'}; "
            "fix=set active_milestone to an exact existing title or remove it"
        )

    milestone = matches[0]
    if milestone.get("state") != "closed":
        return

    ready_issues = list_issues(
        repo, state="open", label=READY_LABEL,
        milestone=active_milestone, json_fields="number", limit=1000,
        timeout=30,
    )
    event(
        "active_milestone_closed", level=logging.INFO,
        configured=active_milestone, repo=repo, state="closed",
        ai_ready_count=len(ready_issues),
    )

def reconcile_release_milestones(repo: str, run_id: str) -> list[str]:
    """Close published-release Milestones that now have no open Issues.

    This is a tick-level, fail-open sweep: release publication and the exact
    Milestone title are independent GitHub facts, so a late-closing Issue is
    reconciled on a later tick without requiring a new release run.
    """
    all_milestones = list_milestones(repo, timeout=30)
    milestones = [m for m in all_milestones if m.get("state") == "open"]
    for milestone in all_milestones:
        if not isinstance(milestone, dict) or milestone.get("state") not in {"open", "closed"}:
            event(
                "milestone_kept_open", number=milestone.get("number")
                if isinstance(milestone, dict) else None,
                repo=repo, reason="malformed",
            )
    try:
        releases_raw = run_gh_read_command([
            "gh", "api", f"repos/{repo}/releases?per_page=100",
            "--paginate", "--slurp",
        ], timeout=30, failure_log_level=logging.DEBUG)
    except subprocess.CalledProcessError as exc:
        classify_milestone_error(exc, "list_releases")
    releases = parse_paginated_issue_array(releases_raw)
    published_tags = {
        release.get("tag_name") for release in releases
        if isinstance(release.get("tag_name"), str)
        and release.get("draft") is False
    }
    evidence: list[str] = []
    for milestone in milestones:
        number = milestone.get("number")
        title = milestone.get("title")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(title, str):
            event("milestone_kept_open", number=number, repo=repo, reason="malformed")
            continue
        # Closed duplicates still make the title ambiguous; do not guess
        # which milestone a release belongs to.
        matches = [m for m in all_milestones if m.get("title") == title]
        if len(matches) != 1:
            reason = "ambiguous title" if len(matches) > 1 else "missing title"
            event("milestone_kept_open", number=number, repo=repo, reason=reason)
            continue
        if title not in published_tags:
            event("milestone_kept_open", number=number, repo=repo,
                  reason="no published release")
            continue
        try:
            open_issues = milestone_open_issues(repo, number)
        except subprocess.CalledProcessError as exc:
            classify_milestone_error(exc, "list_milestone_issues")
        if open_issues:
            event("milestone_kept_open", number=number, repo=repo, reason="open issues")
            continue
        try:
            close_milestone(repo, int(number))
        except subprocess.CalledProcessError as exc:
            classify_milestone_error(exc, "close_milestone")
        event("milestone_closed", number=number, repo=repo)
        evidence.append(f"Milestone #{number} ({title}) closed")
    return evidence

def _pending_milestone_issue(
    repo: str, old: str, candidates: list[dict], repo_dir: Path,
    *, waiting_state: str = _WAITING_NEXT,
) -> int | None:
    """Create one idempotent human-confirmation issue for a milestone wait.

    The fingerprint check and create share the deployment checkout's lock.
    GitHub search can lag a create, so the open-issue search is repeated and
    any duplicate is closed in favour of the lowest issue number. Returns the
    winning issue number so the caller can read its `/milestone` commands.

    ``waiting_state`` selects the wording: the pre-#856 ``next`` advance
    notice (`auto_next_milestone = false`) or ``release`` — the Issue #856
    notice that a finished Milestone still needs its release ticket. Both
    carry the same fingerprint shape, so the close sweep recognises each.
    """
    titles = [str(candidate["title"]) for candidate in candidates]
    fingerprint = f"orbi-milestone-advance old={old} candidates={','.join(titles)}"
    if waiting_state == _WAITING_RELEASE:
        title = f"Milestone {old} 已完成，等待确认发布"
        intro = [
            "## Milestone 已完成，等待人工确认发布",
            "",
            fingerprint,
            "",
            f"当前 milestone `{old}` 的 Issue 已全部关闭，但尚未创建 release ticket。",
            "本 Issue 不会自动创建 release 票，需要你确认后才会开票。",
            "",
            f"请在本 Issue 评论 `/milestone {old}` 创建 release 票"
            "（按 `.github/release-ticket-template.md` 渲染并附上 `"
            f"{RELEASE_LABEL}` 标签，三步各自幂等）。",
            "评论者需对该仓有 write 权限。",
        ]
    else:
        title = f"Milestone {old} 已完成，等待确认推进到 {titles[0]}"
        intro = [
            "## Milestone 自动推进待人工确认",
            "",
            fingerprint,
            "",
            f"当前 milestone `{old}` 已完成，等待确认推进到以下候选版本：",
            "",
        ]
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        existing = list_issues(
            repo, state="all", search=f'in:body "{fingerprint}"',
            json_fields="number", limit=1, timeout=30,
        )
        if existing:
            return _issue_number(existing[0])
        lines = list(intro)
        if waiting_state != _WAITING_RELEASE:
            lines.extend(
                f"- `{candidate['title']}`：{candidate.get('open_issues', 0)} open issues"
                for candidate in candidates
            )
            lines.extend([
                "",
                "请在本 Issue 评论 `/milestone <目标版本>`（对候选标题精确匹配）：",
                "命令会创建 milestone、按 `.github/release-ticket-template.md`"
                " 开一张 release 票，并落地 `active_milestone`；三步各自幂等。",
                "评论者需对该仓有 write 权限。也可恢复自动推进，然后关闭本 Issue。",
            ])
        run_command([
            "gh", "issue", "create", "--repo", repo,
            "--title", title,
            "--body", "\n".join(lines),
        ], timeout=30)
        open_issues = list_issues(
            repo, state="open", search=f'in:body "{fingerprint}"',
            json_fields="number", limit=200, timeout=30,
        )
        numbered = sorted(
            issue["number"] for issue in open_issues
            if isinstance(issue, dict) and isinstance(issue.get("number"), int)
        )
        if not numbered:
            return None
        winner = numbered[0]
        for duplicate in numbered[1:]:
            run_command([
                "gh", "issue", "close", str(duplicate), "--repo", repo,
                "--comment", f"duplicate of #{winner}",
            ], timeout=30)
        if len(numbered) > 1:
            event(
                "pending_milestone_issue_deduplicated",
                issue=f"#{winner}", duplicates=",".join(
                    f"#{number}" for number in numbered[1:]
                ),
            )
        return winner
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _issue_number(issue: object) -> int | None:
    if not isinstance(issue, dict):
        return None
    number = issue.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    return None

_NOTICE_FINGERPRINT_RE = re.compile(
    r"orbi-milestone-advance old=([^ \r\n]+)(?: candidates=([^ \r\n]+))?"
)


def _stale_notice_comment(
    body: object, active_milestone: str, *, keep_release_notice: bool,
    release_ticket_exists: bool = False,
) -> str | None:
    """The close comment for a decision notice, or ``None`` to keep it.

    A notice whose ``old=`` differs from the current active Milestone is
    always stale — both the pre-#856 advance notice and the Issue #856
    release notice. The release notice carries the active Milestone's own
    title as its only candidate (``old=`` == ``candidates=``), so it is
    kept while the release ticket is still missing and closed, with a
    receipt, once the wait is over (``keep_release_notice=False``).

    The receipt states WHY the wait ended, so it never claims a release
    ticket that does not exist: the ticket wording only when the caller
    resolved ``release_ticket_exists=True``, otherwise the neutral "no
    longer waiting" wording (the Milestone got new work, was closed, or
    the opt-in was turned off).

    A decoded ``candidates=`` is compared instead of the raw group so a
    body without one keeps the pre-#856 behaviour: ``str(None)`` is never
    a single-title candidate list, so it is closed like any advance notice.
    """
    match = _NOTICE_FINGERPRINT_RE.search(body) if isinstance(body, str) else None
    if match is None:
        return None
    old = match.group(1)
    candidates = str(match.group(2)).split(",")
    if old != active_milestone:
        return f"已收敛：当前配置 active_milestone = `{active_milestone}`。"
    if candidates != [old] or keep_release_notice:
        return None
    if release_ticket_exists:
        return (
            f"已收敛：Milestone `{active_milestone}` 的 release ticket"
            " 已存在，不再等待发布确认。"
        )
    return f"已收敛：Milestone `{active_milestone}` 不再等待发布确认。"


def _close_stale_milestone_issues(
    repo: str, active_milestone: str, *, keep_release_notice: bool = True,
    release_ticket_exists: bool = False,
) -> None:
    """Close decision notices that no longer match the config.

    A notice whose ``old=`` differs from the current active Milestone is
    always stale. The Issue #856 release notice shares the active
    Milestone's own title (``old=`` == ``candidates=``), so it is kept
    while the release ticket is still missing and closed — with its own
    receipt — once the wait is over (``keep_release_notice=False``).
    """
    issues = list_issues(
        repo, state="open", search='in:body "orbi-milestone-advance"',
        json_fields="number,body", limit=200, timeout=30,
    )
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        comment = _stale_notice_comment(
            issue.get("body"), active_milestone,
            keep_release_notice=keep_release_notice,
            release_ticket_exists=release_ticket_exists,
        )
        if comment is None:
            continue
        run_command([
            "gh", "issue", "close", str(issue["number"]), "--repo", repo,
            "--comment", comment,
        ], timeout=30)
        event(
            "stale_milestone_issue_closed", issue=f"#{issue['number']}",
            active=active_milestone,
        )


def _milestone_open_issue_count(milestone: dict) -> int:
    """GitHub's own open-Issue counter, coerced to an int (0 if absent)."""
    value = milestone.get("open_issues")
    return value if isinstance(value, int) else 0


def _close_stale_milestone_issues_safely(
    repo: str, active_milestone: str, *, keep_release_notice: bool,
    release_ticket_exists: bool = False,
) -> None:
    """Close obsolete decision notices as a pure idle-path bypass.

    Closing an obsolete confirmation is notification maintenance; it must
    not turn an otherwise successful idle tick into a delivery failure.
    """
    try:
        _close_stale_milestone_issues(
            repo, active_milestone, keep_release_notice=keep_release_notice,
            release_ticket_exists=release_ticket_exists,
        )
    except Exception:
        LOGGER.exception(
            "stale_milestone_issue_close_failed repo=%s active=%s",
            repo, active_milestone,
        )


def _ensure_pending_milestone_notice(
    repo: str, old: str, candidates: list[dict], repo_dir: Path | None,
    config_path: Path, waiting_state: str, candidate_titles: list[str],
    *, policy: RepoPolicy | None, policy_path: str, base_branch: str,
    dispatch_label: str, version_file: str | None,
) -> None:
    """Open one idempotent decision notice and apply any `/milestone` reply.

    Both steps are idle-path bypasses: a failed notice or command must not
    turn an otherwise successful no-ready tick into a delivery failure.
    """
    issue_number: int | None = None
    try:
        issue_number = _pending_milestone_issue(
            repo, old, candidates,
            repo_dir if repo_dir is not None else config_path.parent,
            waiting_state=waiting_state,
        )
    except Exception:
        LOGGER.exception(
            "pending_milestone_issue_failed repo=%s old=%s", repo, old,
        )
    if issue_number is None:
        return
    try:
        process_milestone_commands(
            repo, issue_number,
            candidate_titles=candidate_titles,
            config_path=config_path, policy=policy,
            policy_path=policy_path, base_branch=base_branch,
            dispatch_label=dispatch_label, version_file=version_file,
        )
    except Exception:
        LOGGER.exception(
            "milestone_command_processing_failed repo=%s old=%s", repo, old,
        )


def advance_active_milestone_on_idle(
    repo: str, active_milestone: str, config_path: Path,
    repo_dir: Path | None = None,
    *, auto_next_milestone: bool = True, release_confirmation: bool = False,
    parse_version_title: Callable[[object], tuple[int, int, int] | None],
    policy: RepoPolicy | None = None,
    policy_path: str = REPO_CONFIG_PATH,
    base_branch: str = "main",
    dispatch_label: str = READY_LABEL,
    version_file: str | None = None,
) -> tuple[str, str | None]:
    """Check and advance a configured milestone after no_ready_issue."""
    milestones = list_milestones(repo, timeout=30)
    matches = [
        milestone for milestone in milestones
        if isinstance(milestone, dict)
        and milestone.get("title") == active_milestone
    ]
    if not matches:
        open_list = ", ".join(
            f"{milestone.get('title')}({milestone.get('open_issues')})"
            for milestone in milestones
            if isinstance(milestone, dict) and milestone.get("state") == "open"
        ) or "(none)"
        raise RuntimeError(
            f"active_milestone_missing current={active_milestone}; "
            f"open milestones: {open_list}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"active_milestone {active_milestone}: ambiguous exact-title "
            f"match in {repo}, refusing to guess"
        )
    active = matches[0]
    state = str(active.get("state"))
    open_issues = _milestone_open_issue_count(active)
    if state == "open":
        # Issue #856: only the opted-in repository pays the extra release
        # ticket search, and only when the Milestone is actually finished.
        release_ticket_exists = False
        if open_issues == 0 and release_confirmation:
            release_ticket_exists = _release_ticket_exists(
                repo, active_milestone,
            )
        waiting = classify_milestone_waiting(
            state=state, open_issues=open_issues,
            release_ticket_exists=release_ticket_exists,
            candidates=[], auto_next_milestone=auto_next_milestone,
            release_confirmation=release_confirmation,
        )
        if waiting == MILESTONE_AWAITING_RELEASE:
            _close_stale_milestone_issues_safely(
                repo, active_milestone, keep_release_notice=True,
            )
            event(
                "active_milestone_release_pending", level=logging.WARNING,
                old=active_milestone, repo=repo,
            )
            _ensure_pending_milestone_notice(
                repo, active_milestone,
                [{"title": active_milestone, "open_issues": open_issues}],
                repo_dir, config_path, _WAITING_RELEASE, [active_milestone],
                policy=policy, policy_path=policy_path,
                base_branch=base_branch, dispatch_label=dispatch_label,
                version_file=version_file,
            )
            return state, None
        _close_stale_milestone_issues_safely(
            repo, active_milestone, keep_release_notice=False,
            release_ticket_exists=release_ticket_exists,
        )
        return state, None
    if release_confirmation:
        # Issue #856: the release notice's wait also ends when its Milestone
        # closes — the release shipped, or the decision went another way —
        # and the closed path never swept notices before #856. Key-gated so
        # an unopted repository keeps the pre-#856 closed path unchanged;
        # the pre-#856 advance notice (``candidates != [old]``) is kept
        # either way, exactly like the open path.
        _close_stale_milestone_issues_safely(
            repo, active_milestone, keep_release_notice=False,
        )
    current = parse_version_title(active_milestone)
    candidates = []
    for milestone in milestones:
        if not isinstance(milestone, dict) or milestone.get("state") != "open":
            continue
        version = parse_version_title(milestone.get("title"))
        if version is not None and current is not None and version > current:
            candidates.append((version, milestone.get("title")))
    if not candidates:
        event(
            "active_milestone_advance_none", current=active_milestone,
            closed=active_milestone, repo=repo,
        )
        return "closed", None
    candidates.sort()
    candidate_details = [
        milestone for _, title in candidates
        for milestone in milestones
        if isinstance(milestone, dict) and milestone.get("title") == title
    ]
    waiting = classify_milestone_waiting(
        state=state, open_issues=open_issues, release_ticket_exists=False,
        candidates=candidates, auto_next_milestone=auto_next_milestone,
        release_confirmation=release_confirmation,
    )
    if waiting == MILESTONE_AWAITING_NEXT:
        candidate_titles = [title for _, title in candidates]
        event(
            "active_milestone_advance_pending", level=logging.WARNING,
            old=active_milestone, candidates=",".join(candidate_titles),
            auto_next_milestone="false",
        )
        _ensure_pending_milestone_notice(
            repo, active_milestone, candidate_details, repo_dir, config_path,
            _WAITING_NEXT, candidate_titles,
            policy=policy, policy_path=policy_path,
            base_branch=base_branch, dispatch_label=dispatch_label,
            version_file=version_file,
        )
        return "closed", None
    new_value = candidates[0][1]
    # Issue #1304: land the value where it actually comes from.  The
    # repository policy overrides the host config, so a host write here
    # was discarded on the next read and the advance never took effect.
    _land_active_milestone(
        repo, new_value, policy=policy, policy_path=policy_path,
        config_path=config_path,
    )
    event(
        "active_milestone_advanced", old=active_milestone, new=new_value,
        closed=active_milestone, repo=repo,
    )
    return "closed", new_value

def _publish_plan_milestone(publisher: ProgressPublisher, worktree: Path) -> None:
    """Post the `plan ready` milestone once the worktree has the plan
    artifact."""
    if (worktree / ".orbi" / "plan.md").is_file():
        publisher.milestone("plan ready")

def _publish_test_milestone(
    publisher: ProgressPublisher,
    worktree: Path,
    test_result_failed: Callable[[str], bool],
) -> None:
    """Post `tests passed` / `tests failed` from the worktree's test.log."""
    result = read_test_result(worktree)
    if result is None:
        return
    if test_result_failed(result):
        publisher.milestone(f"tests failed: {result}")
    else:
        publisher.milestone(f"tests passed: {result}")
