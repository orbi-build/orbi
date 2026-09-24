"""The claim domain: turning one ready Issue into this tick's delivery.

Extracted from `orbi.runner` (Issue #1259, Article 3.2): the five claim scans
(P0, bug, plain, release fallback, external takeover), the classifiers and
reconcilers they share, and the resumable-PR / in-flight / fresh-claim
selection order. The module never imports `orbi.runner` (Article 3.3): the
delivery-side helpers arrive as `ResumeHooks`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from orbi import milestone as milestone_bookkeeping
from orbi import scene
from orbi.config import RunnerConfig
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL, BLOCKED_LABEL, EPIC_LABEL, EVENT_BLOCKED,
    FIX_NEEDED_LABEL, IN_PROGRESS_LABEL, MERGED_LABEL, P0_LABEL,
    PR_OPENED_LABEL, READY_LABEL, RELEASE_LABEL,
)
from orbi.delivery_scene import (
    EXTERNAL_PR_MARKER, DeliveryScene, body_markers, classify,
)
from orbi.github import (
    _epic_audit, _verify_epic_complete, apply_label_patch, close_issue,
    comment_issue, issue_comments, issue_priority, issue_view,
    latest_run_marker, list_issues, milestone_open_issue_count,
    open_blocker_numbers, open_pr_for_branch, pr_comments, run_gh_read_command,
)
from orbi.gitops import task_branch
from orbi.journal import LOGGER, current_run_id, event, new_run_id, set_run_id
from orbi.pilot_slots import slot_held_deliveries, slot_occupancy
from orbi.progress import run_marker
from orbi.release import release_target_milestone
from orbi.repo_config import RepoConfigError, RepoPolicy, load_repo_policy


class ResumeHooks(NamedTuple):
    """The delivery-side helpers `runner.main` passes in (Article 3.3).

    The physical scene handling of `pick_resumable_delivery` plus the PR
    comment writer `reconcile_orphan_prs` reports its orphan through.
    """
    resume_scene: Callable[[list[dict]], dict]
    route_external_pr_ticket: Callable[[dict, str], bool]
    block_scene_failure: Callable[[dict, ValueError, str, list[dict]], None]
    recover_missing_pr_scene: Callable[[dict, str, Path], dict | None]
    has_recoverable_pr_scene: Callable[[dict, str, Path], bool]
    comment_pr: Callable[..., None]


# Ready scans: P0 urgent Issues are claimed before
# bugs, bugs before new features — if the delivery loop is broken,
# claiming enhancements only piles up unreviewed PRs, and a production
# outage (P0) must not wait behind ordinary work. The P0 scan runs
# first, then the bug scan, then the plain ready scan — each with the
# exact same exclusions. No priority numbers, no separate queue, no
# new state machine: three `gh issue list` searches with the same
# blockedBy semantics. `p0` is a plain label, not a
# delivery state: it only orders the pickup.
READY_SCAN_EXCLUSIONS = (
    f"-label:{IN_PROGRESS_LABEL} -label:{PR_OPENED_LABEL} "
    f"-label:{FIX_NEEDED_LABEL} -label:{MERGED_LABEL} "
    f"-label:{BLOCKED_LABEL}"
)


def ready_searches(active_milestone: str | None = None,
                   dispatch_label: str = READY_LABEL) -> tuple[str, str, str]:
    """Return the three ready scans (p0, bug, plain) in pickup order.

    With a configured `active_milestone` every scan
    carries the `milestone:"<title>"` qualifier — the quoted form is
    the contract because milestone titles may contain spaces or
    special characters (verified against the live API). The scope is
    part of the QUERY, so an Issue of another Milestone (or of no
    Milestone) never enters the result set, and a `v0.2.0` Issue
    without `ai-ready` never does either: the `label:ai-ready`
    qualifier stays. The Milestone is a version scope, not a
    replacement for the `ai-ready` execution switch. P0 does NOT
    cross milestones: the active Milestone is
    the claim scope of EVERY fresh claim, and `p0` only orders the
    pickup inside it — one uniform rule, no special case. Without a
    configured Milestone the searches are byte-identical to the
    pre-#139 scans (compat).
    """
    scope = (
        f' milestone:"{active_milestone}"' if active_milestone else ""
    )
    # The claim label is a delivery-policy key; the lifecycle
    # labels (`ai-in-progress`/`ai-merged`/...) stay host constants.
    label = dispatch_label or READY_LABEL
    return (
        f"label:{label} label:{P0_LABEL}{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:{label} label:bug{scope} {READY_SCAN_EXCLUSIONS}",
        f"label:{label}{scope} {READY_SCAN_EXCLUSIONS}",
    )


def release_fallback_search(active_milestone: str | None = None,
                            dispatch_label: str = READY_LABEL) -> str:
    """Return the release fallback scan query.

    A Release task is a closing action and must never compete with an
    ordinary delivery for the slot: it is claimed only after the three
    ordinary ready scans (p0/bug/plain) found nothing claimable. The
    query keeps `label:ai-ready` (the human execution switch — a release
    Issue without it stays waiting, as before) plus `label:ai-release`
    and the same five delivery-state exclusions as the ordinary scans
    (`READY_SCAN_EXCLUSIONS`). With a configured `active_milestone` it
    carries the same `milestone:"<title>"` scope (the quoted form is the
    contract, same as `ready_searches`). `ai-epic` is excluded by the
    code-layer Epic guard in `_pick_from_scan`, not the query.
    """
    scope = (
        f' milestone:"{active_milestone}"' if active_milestone else ""
    )
    label = dispatch_label or READY_LABEL
    return (
        f"label:{label} label:{RELEASE_LABEL}{scope} "
        f"{READY_SCAN_EXCLUSIONS}"
    )


def external_takeover_search(dispatch_label: str = READY_LABEL) -> str:
    """Return the external-takeover scan query (Issue #842, decision D1).

    The FIFTH and last ready scan: it runs only after the three
    ordinary ready scans and the release fallback found nothing
    claimable — an external review is supplementary capacity and never
    competes with this version's deliveries for the slot. It is the
    repository's FIRST scan WITHOUT the `milestone:"<title>"`
    qualifier, and it takes no `active_milestone` argument at all: an
    external contribution is not part of any version's delivery scope,
    and scoping it into the active Milestone would let an external PR
    block the release gate forever. The candidate set is the ready
    queue whose body mentions the triage workflow's external-PR marker
    (a body-text term — GitHub's search tokenizes the marker loosely,
    verified against the live API, so the term is a candidate PREFILTER
    only); the exact `EXTERNAL_PR_RE` marker check is the code-layer
    guard in `_pick_from_scan` (`external_only=True`). `ai-epic` and
    `ai-release` stay excluded by the same code-layer guards as every
    other scan.
    """
    label = dispatch_label or READY_LABEL
    return (
        f'label:{label} "orbi:external-pr" in:body '
        f"{READY_SCAN_EXCLUSIONS}"
    )


def _issue_label_set(issue: dict) -> frozenset[str]:
    """The Issue's label names (the scans fetch `labels`).

    The fact shape the scene classification (`orbi.delivery_scene`)
    reads; a missing or malformed `labels` field yields the empty set —
    the classification then sees an unlabelled ticket, never a crash.
    """
    return frozenset(
        label.get("name") for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    )


def is_epic(issue: dict) -> bool:
    """Return True when one issue carries the `ai-epic` label.

    A pure function of the issue's `labels` (the scans fetch `labels`,
    so no extra gh call), the same style as `issue_priority`. A
    missing or malformed `labels` field fails open to "not an epic":
    the scan always requests `labels`, so a shape change only loses
    the Epic guard for one run — it must never deadlock the queue.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == EPIC_LABEL:
            return True
    return False


def is_release(issue: dict) -> bool:
    """Return True when one issue carries the `ai-release` label (#98).

    A pure function of the issue's `labels` (the scans fetch `labels`,
    so no extra gh call), the same style as `is_epic` and
    `issue_priority`. A missing or malformed `labels` field fails open
    to "not a release task": the scan always requests `labels`, so a
    shape change only loses the release routing for one run — it must
    never deadlock the queue.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == RELEASE_LABEL:
            return True
    return False


def reconcile_open_epics(repo: str, run_id: str) -> list[str]:
    """Sweep open Epics once per tick; ordinary pickup must not depend on it."""
    epics = list_issues(
        repo, state="open", search=f"label:{EPIC_LABEL}",
        json_fields="number,body,labels", limit=200,
    )
    evidence: list[str] = []
    for listed_epic in epics:
        number = listed_epic.get("number")
        try:
            child_evidence = _verify_epic_complete(repo, listed_epic)
        except (ValueError, json.JSONDecodeError) as exc:
            reason = str(exc)
            event("epic_kept_open", issue=number, repo=repo, reason=reason)
            evidence.append(f"Epic #{number} kept open: {reason}")
            continue
        audit = _epic_audit(child_evidence)
        comments = issue_comments(int(number), repo=repo)
        if not any(audit in str(comment.get("body", "")) for comment in comments):
            comment_issue(int(number), repo=repo,
                         body=f"<!-- orbi:run={run_id} -->\n{audit}\nrun_id={run_id}")
        close_issue(int(number), repo=repo)
        event("epic_closed", issue=number, repo=repo)
        evidence.append(f"Epic #{number} closed after verification ({'; '.join(child_evidence)})")
    return evidence


# The hidden marker of the orphan-PR report — the sweep's
# idempotency key (one report per PR, never one per tick).
ORPHAN_PR_MARK = "<!-- orbi:orphan-pr -->"

_ORPHAN_PR_BRANCH_RE = re.compile(r"orbi/.+-issue-(\d+)\Z")


def orphan_pr_branch_issue(head_ref: object) -> int | None:
    """The source Issue number of a stable delivery branch, or None.

    `task_branch` names every Runner delivery branch
    `orbi/<source_repo slug>-issue-<N>`; any other head (a human's or an
    external contributor's branch) is never the Runner's business.
    """
    if not isinstance(head_ref, str):
        return None
    match = _ORPHAN_PR_BRANCH_RE.fullmatch(head_ref)
    return int(match.group(1)) if match else None


def reconcile_orphan_prs(
    repo: str, run_id: str, *, hooks: ResumeHooks,
) -> list[str]:
    """Report open delivery PRs whose source Issue is closed.

    A human may close an Issue while its delivery is in flight, and the
    close can land before OR after the PR is opened — every resumable
    scan reads `state=open` Issues only, so without this sweep nothing
    would ever look at the resulting PR again. Tick-level and fail-open
    like the Epic/Milestone reconciliations: each orphan PR gets ONE
    idempotent report comment (the `ORPHAN_PR_MARK` marker); the
    merge/close decision stays with the human — the Runner never
    auto-merges and never auto-closes across a human's close decision.
    """
    raw = run_gh_read_command([
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName", "--limit", "200",
    ], timeout=30)
    prs = json.loads(raw)
    if not isinstance(prs, list):
        raise ValueError("pr list must return a JSON array")
    evidence: list[str] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        pr_number = pr.get("number")
        issue_number = orphan_pr_branch_issue(pr.get("headRefName"))
        if not isinstance(pr_number, int) or issue_number is None:
            continue
        details = issue_view(issue_number, "state", repo=repo, timeout=30)
        state = details.get("state") if isinstance(details, dict) else None
        if state == "OPEN":
            continue
        if state != "CLOSED":
            raise ValueError("issue view state must be OPEN or CLOSED")
        if any(
            isinstance(comment, dict)
            and ORPHAN_PR_MARK in str(comment.get("body", ""))
            for comment in pr_comments(pr_number, repo=repo)
        ):
            continue
        hooks.comment_pr(
            pr_number, repo=repo,
            body=(
                f"{ORPHAN_PR_MARK}\n{run_marker(run_id)}\n"
                f"Orbi orphan PR: the source Issue #{issue_number} is "
                f"closed while this PR is still open, so it will never "
                f"be merged automatically. Human decision: merge this "
                f"PR to keep the work, or close it.\n"
                f"run_id={run_id}"
            ),
        )
        event(
            "orphan_pr_reported", pr=pr_number, issue=issue_number,
            repo=repo,
        )
        evidence.append(
            f"PR #{pr_number} reported: source Issue #{issue_number} is closed"
        )
    return evidence


# The scenes the ready scans claim: the fresh-claim family of the
# delivery-scene classification. A release candidate is
# claimable only through the release fallback scan's `allow_release`
# gate above; the classification still names its scene. A candidate
# that classifies elsewhere is skipped — a terminal state the query's
# index lagged behind, or an in-flight/opened-PR state another scan
# owns — and a candidate carrying NO readable labels fails open (the
# is_epic/is_release convention): the query is the claim authority.
_FRESH_CLAIM_SCENES = frozenset({
    DeliveryScene.FRESH_CLAIM,
    DeliveryScene.RELEASE,
    DeliveryScene.CONTENT_ONLY,
    DeliveryScene.OPS,
})
_SCAN_CLAIMABLE_SCENES = _FRESH_CLAIM_SCENES | {
    DeliveryScene.RESTART_IN_FLIGHT,
}


def _pick_from_scan(
    issues: list[dict], repo: str, allow_release: bool = False,
    active_milestone: str | None = None,
    ready_label: str = READY_LABEL,
    external_only: bool = False, held: frozenset = frozenset(),
) -> dict | None:
    """Return the first claimable Issue of one scan result, else None.

    The per-Issue guards, shared by the three ordinary ready scans and
    the release fallback scan. An `ai-epic` Issue is never
    claimed — no label change, no worktree, no run, no slot — with the
    structured `epic_not_claimed` line (the check precedes the blockedBy
    check: "it is an Epic" is the more fundamental reason). An Issue
    with open native blockers is skipped with the structured
    `blocked_by` line. Otherwise the Issue is picked and the
    pickup log carries the explicit priority field: `p0`
    for urgent Issues, `normal` otherwise.

    `allow_release` controls the release skip: the three
    ordinary scans skip an `ai-release` Issue (`release_not_claimed`) so
    a release never competes with an ordinary delivery for the slot; the
    release fallback scan passes `allow_release=True` and claims it.

    `external_only` (the Issue #842 takeover scan) requires the exact
    `<!-- orbi:external-pr:N -->` body marker before any other guard:
    the scan is the one milestone-free query, so a candidate the loose
    body-text prefilter matched but that carries no marker is skipped
    with `claim_yield reason=no_external_marker` — claiming it there
    would let a plain milestone-less Issue bypass the active
    Milestone's claim scope. Every skip is a structured event; the scan
    never returns silently for a ticket a human might expect to be
    claimable.

    A release candidate additionally passes the Milestone completeness
    gate: while the Milestone still counts any other open
    Issue (`open_issues > 1`), the release is skipped with the
    structured `release_milestone_incomplete` line and the Issue stays
    `ai-ready` for the next tick — a recoverable wait, never
    `ai-blocked`. A Milestone query that cannot be evaluated skips the
    release too (`release_milestone_check_failed`): a bad release is
    irreversible, so the check fails safe and the next tick retries.

    The last guard is the delivery-scene classification itself (Issue
    #787): the ready scans claim only the fresh-claim family, decided
    by the same pure `classify` the dispatch layer runs, so a stale
    index handing over a ticket that no longer carries the queue label
    is skipped, never claimed into a scene it is no longer in.

    `held` names the deliveries a live co-runner works on: such an Issue
    is skipped (`claim_yield reason=held_by_live_runner`) and never
    claimed twice — the skip is the FIRST guard of this loop.
    """
    for issue in issues:
        if (repo, int(issue["number"])) in held:
            event("claim_yield", issue=issue.get("number"), reason="held_by_live_runner")
            continue
        if external_only and EXTERNAL_PR_MARKER not in body_markers(
                issue.get("body")):
            event("claim_yield", issue=issue.get("number"), reason="no_external_marker")
            continue
        if is_epic(issue):
            event("epic_not_claimed", issue=issue.get("number"), repo=repo)
            continue
        if not allow_release and is_release(issue):
            event("release_not_claimed", issue=issue.get("number"), repo=repo)
            continue
        if allow_release and is_release(issue):
            target_milestone = release_target_milestone(
                issue, active_milestone,
            )
            if target_milestone is not None:
                try:
                    open_issues = milestone_open_issue_count(
                        repo, target_milestone,
                    )
                except Exception as exc:
                    event(
                        "release_milestone_check_failed", level=logging.ERROR,
                        issue=issue.get("number"), repo=repo,
                        milestone=target_milestone, error=exc)
                    return None
                if open_issues > 1:
                    event("release_milestone_incomplete", issue=issue.get("number"),
                          repo=repo, milestone=target_milestone,
                          open_issues=open_issues)
                    continue
        blockers = open_blocker_numbers(issue)
        if blockers:
            event("blocked_by", issue=issue.get("number"), repo=repo,
                  blockers=",".join(str(number) for number in blockers))
            continue
        current_labels = _issue_label_set(issue)
        if current_labels:
            scene_value = classify(
                labels=current_labels, scene=None, pr_state=None,
                body_markers=body_markers(issue.get("body")),
                ready_label=ready_label,
            )
            if scene_value not in _SCAN_CLAIMABLE_SCENES:
                event(
                    "claim_yield", issue=issue.get("number"),
                    reason=f"scene_{scene_value.value}",
                )
                continue
        event("picked", issue=issue.get("number"), repo=repo,
              priority=issue_priority(issue))
        return issue
    return None


def pick_issue(repo: str, active_milestone: str | None = None,
               dispatch_label: str = READY_LABEL,
               held: frozenset = frozenset()) -> dict | None:
    # A merged delivery keeps `ai-ready` + `ai-merged` on the (still
    # open) Issue; `ai-merged` is the success terminal state, so it is
    # excluded from the ready scan like every other delivery state.
    # The scan fetches the ready queue (not just the first Issue) and
    # reads the native GitHub dependency per Issue: an
    # Issue with open blockers is skipped — no claim, no label change,
    # no worktree — and the next ready Issue is considered instead.
    # An `ai-epic` Issue is skipped too — no claim, no
    # label change, no worktree, no run, no slot — with the structured
    # `epic_not_claimed` line (the Epic check precedes the blockedBy
    # check). The P0 scan runs first, then the bug
    # scan; a P0 or bug with open blockers is skipped there and the
    # next scan still decides. The scan also fetches `labels` so the
    # picked issue's priority (and Epic-ness) is visible without an
    # extra gh call. With a configured `active_milestone`
    # all three scans are scoped to that Milestone in the query itself
    # (see `ready_searches`) — the Epic skip and the blockedBy skip
    # above are the unchanged second (code) layer, and a failed scan
    # still fails open (never a silent claim of the wrong version).
    # `held` carries the deliveries live co-runners work on RIGHT NOW:
    # the claim window is serialized by the caller, so a second runner
    # re-scanning in the same tick finds the first one's identity here
    # and yields instead of claiming the same Issue (#1319).
    # An `ai-release` Issue is skipped by the three ordinary
    # scans (`release_not_claimed`) and claimed only by the release
    # fallback scan that runs AFTER all three found nothing claimable —
    # a release is a closing action and must never take the slot ahead
    # of an ordinary delivery. The external-takeover scan (Issue #842,
    # decision D1) runs FIFTH and last, after the release fallback: the
    # one scan without the Milestone qualifier, restricted by the
    # code-layer marker guard to the triage tickets an external
    # contribution routes to — an external review is supplementary
    # capacity, never a competitor of this version's deliveries.
    for search in ready_searches(active_milestone, dispatch_label):
        try:
            issues = list_issues(
                repo, state="open", search=search,
                json_fields="number,title,body,labels,blockedBy", limit=200,
            )
        except Exception as exc:
            # Fail open: a failed blockedBy query must
            # never deadlock the queue. This tick claims nothing from
            # this repo and the next tick retries the query; the error
            # is logged, never raised, and no label is touched.
            event(
                "blocked_by_check_failed", level=logging.ERROR,
                repo=repo, error=exc,
            )
            return None
        picked = _pick_from_scan(
            issues, repo, ready_label=dispatch_label, held=held,
        )
        if picked is not None:
            return picked
    # Release fallback: only when no ordinary delivery
    # (p0/bug/plain) is claimable. The query keeps `label:ai-ready` +
    # `label:ai-release` and the same delivery-state exclusions; the Epic
    # and blockedBy guards still apply via `_pick_from_scan`. Issue
    # #663: the release candidate also passes the Milestone completeness
    # gate inside `_pick_from_scan`, so the query fetches `milestone`.
    # A failed fallback query fails open exactly like any other scan.
    try:
        issues = list_issues(
            repo, state="open",
            search=release_fallback_search(active_milestone, dispatch_label),
            json_fields="number,title,body,labels,blockedBy,milestone",
            limit=200,
        )
    except Exception as exc:
        event(
            "blocked_by_check_failed", level=logging.ERROR,
            repo=repo, error=exc,
        )
        return None
    picked = _pick_from_scan(
        issues, repo, allow_release=True, held=held,
        active_milestone=active_milestone,
        ready_label=dispatch_label,
    )
    if picked is not None:
        return picked
    # External takeover fallback (Issue #842, decision D1): the FIFTH
    # and last scan — after every ordinary delivery and the release
    # fallback. It is the one scan WITHOUT the `milestone:"<title>"`
    # qualifier (an external contribution belongs to no version's
    # delivery scope, and the release gate must stay immune to external
    # PRs), so the code layer keeps the scope: only a ticket whose body
    # carries the exact triage marker is claimable here
    # (`external_only`). A failed query fails open exactly like any
    # other scan.
    try:
        issues = list_issues(
            repo, state="open",
            search=external_takeover_search(dispatch_label),
            json_fields="number,title,body,labels,blockedBy",
            limit=200,
        )
    except Exception as exc:
        event(
            "blocked_by_check_failed", level=logging.ERROR,
            repo=repo, error=exc,
        )
        return None
    return _pick_from_scan(
        issues, repo, external_only=True, held=held,
        ready_label=dispatch_label,
    )


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
    dispatch_label: str = READY_LABEL,
) -> dict | None:
    """Return one in-flight Issue a killed runner left behind.

    `dispatch_label` is the repository's claim label: a
    repository policy may replace `ai-ready` with its own label, and the
    in-flight scan must find the SAME queue entry the ready scan used —
    otherwise a killed (or model-wait-recovered) run would be stranded
    forever in a repository with a custom dispatch label.

    A SIGKILLed runner leaves the task worktree and the `ai-in-progress`
    claim label behind (the failure path never ran); the Issue keeps
    `ai-ready` too (a claim never removes it). The ready scan excludes
    `ai-in-progress`, so without this scan the run is never resumed and
    the Issue is stuck forever — the "restart finds
    the same progress comment by run marker" would be unreachable in the
    production flow. `process_issue`'s resume block (newest worktree's
    run id) then reuses the run instead of starting a second one. Every
    other delivery state is excluded: those Issues are owned by the
    resumable-PR scan (`ai-fix-needed`) or are terminal. `ai-epic` is
    excluded too: a legacy Epic left behind with
    `ai-in-progress` (the #80 scene, before the Epic mechanism existed)
    must never be resumed into a run — an Epic is coordination, not an
    executable task.

    The scan runs only when no OTHER runner is live: a slot held by
    another process proves a live runner is working (on this or another
    Issue), so the `ai-in-progress` label is in flight, not orphaned —
    resuming it here would start a second Pi for a run that is alive.
    This runner's own slot is excluded: `main` took it before the claim
    scan and holds it for the whole delivery.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    label = dispatch_label or READY_LABEL
    # `labels`: a P0 a killed runner left behind
    # keeps its priority in the progress comment on resume.
    # `milestone`: a release run killed mid-release is
    # resumed with THIS dict, and `process_release` scopes its
    # leftover-delivery gate to the release's own Milestone — the
    # Issue is the authority (never `active_milestone`: a resume is
    # not gated by a Milestone change).
    issues = list_issues(
        repo, state="open",
        search=(
            f"label:{label} label:{IN_PROGRESS_LABEL} "
            f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
            f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL} "
            f"-label:{EPIC_LABEL}"
        ),
        json_fields="number,title,body,labels,milestone", limit=1,
    )
    candidate = issues[0] if issues else None
    if candidate is not None:
        # The same pure classification the dispatch runs.
        # This scan hands the candidate to `process_issue`, whose full
        # fact set decides the handler, so the scan skips only a
        # candidate that left EVERY claimable state (a relabel inside
        # the query's staleness window); a candidate with no readable
        # labels fails open (the is_epic/is_release convention).
        current_labels = _issue_label_set(candidate)
        if current_labels:
            scene_value = classify(
                labels=current_labels, scene=None, pr_state=None,
                body_markers=body_markers(candidate.get("body")),
            )
            if scene_value not in _SCAN_CLAIMABLE_SCENES:
                event(
                    "claim_yield", issue=int(candidate["number"]),
                    reason=f"scene_{scene_value.value}",
                )
                return None
    return candidate


def pick_next_issue(
    repos: list[str], active_milestone: str | None = None,
) -> tuple[str, dict] | None:
    """Scan sources in order; return the first ready issue and its source."""
    for repo in repos:
        issue = pick_issue(repo, active_milestone)
        if issue is not None:
            return repo, issue
    return None


def pick_resumable_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
    repo_dir: Path | None = None, *, hooks: ResumeHooks,
) -> tuple[dict, dict] | None:
    """Return the newest FREE opened-PR delivery and its resume scene.

    All opened-PR states are scanned: `ai-fix-needed`
    (awaiting the next review session after a finding or a base
    conflict — the review session fixes findings in the same
    session), `ai-pr-opened` (awaiting review), and
    `ai-awaiting-merge` (a known policy blocker whose maintainer action
    is retried without another review). The `ai-pr-opened`
    scan exists because the delivery that opened the PR can be gone: the
    runner can die inside the delivery wait loop, leaving a valid
    MERGEABLE PR with no owner. Without the scan such a delivery is
    picked up by no other scan (`pick_issue` excludes `ai-pr-opened`)
    and is stranded forever. `ai-blocked` Issues are excluded (they need
    a human decision first), as are merged Issues and closed Issues.
    `ai-in-progress` is NOT excluded: a runner killed
    during review leaves the backfilled in-flight label behind on the
    opened-PR delivery, and the same scan must pick it back up — the
    positive `label:ai-fix-needed,ai-pr-opened` qualifier already
    restricts the scan to opened-PR Issues (an implement-phase Issue
    has `ai-ready`+`ai-in-progress` but neither opened-PR label, so it
    never matches). A missing scene on `ai-fix-needed` with exactly one
    open PR on the stable internal branch is a fresh-claim takeover: the PR
    supplies the continuation anchor and the claim writes a new trusted
    scene. Other scenes that cannot be recovered are SINGLE-Issue failures:
    the Issue is marked `ai-blocked` with the concrete reason
    (`block_scene_failure`) and the scan moves on to the next candidate, so
    the tick continues with the in-flight and ready scans and exits 0 — one
    corrupted Issue must never make every tick crash while the whole queue
    waits.

    Only the deliveries a live co-runner CURRENTLY holds are skipped:
    every holder names its (repo, issue) in its slot file
    (`pilot_slots.mark_slot_delivery`), so this scan skips exactly the
    in-flight deliveries. That is the round-1 protection at
    the right granularity — a held delivery is never resumed here, so a
    second review Pi never starts in the same worktree/branch/run and a
    second `gh pr merge --match-head-commit` never hits the merged PR.
    The pre-#809 guard abandoned the WHOLE scan whenever any slot in the
    (often shared, multi-repo) slot dir was held: with any concurrency
    the review never ran while fresh claims kept opening PRs — the
    reported starvation (nine MERGEABLE PRs, the oldest 90 minutes,
    zero review ticks). A free delivery is now resumed even while other
    deliveries are in flight, and the review backlog drains at the
    concurrency rate instead of only growing. This runner's own slot is
    excluded: `main` took it before the claim scan and holds it for the
    whole delivery.

    The query page is `max_concurrency + 1` candidates: at most
    `max_concurrency` deliveries can be held by live co-runners, so a
    free candidate is always inside the page when one exists. The scan
    reviews the newest FREE candidate (held ones are skipped before any
    candidate read — an in-flight delivery is never touched).
    """
    held = slot_held_deliveries(slot_dir, max_concurrency)
    # `label:a,b` is GitHub's OR within one label qualifier
    # (verified live: repeating the qualifier matches only the
    # first label). `ai-in-progress` is intentionally NOT excluded:
    # a killed review runner leaves the backfilled
    # in-flight label behind, and the positive qualifier above
    # already keeps implement-phase Issues out.
    # `labels`: a resumed P0 delivery keeps its
    # priority in the progress comment through review/merge.
    # `body`: the scene classification reads the
    # delivery markers — without it the #726 external routing of a
    # marker ticket with no trusted scene comment is unreachable in
    # production (the probe read a body the query never fetched).
    issues = list_issues(
        repo, state="open",
        search=(
            f"label:{FIX_NEEDED_LABEL},{PR_OPENED_LABEL},"
            f"{AWAITING_MERGE_LABEL} "
            f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL}"
        ),
        json_fields="number,title,state,url,labels,body",
        limit=max_concurrency + 1,
    )
    for issue in issues:
        if issue.get("state") != "OPEN":
            # Left the opened-PR state between the query and this read
            # (a close/merge race): this candidate is gone, not the scan.
            continue
        if (repo, int(issue["number"])) in held:
            # In flight in another live runner: never a
            # second review Pi for it — and no candidate read either.
            continue
        comments = issue_comments(int(issue["number"]), repo=repo)
        try:
            found = hooks.resume_scene(comments)
        except scene.SceneError as exc:
            # A trusted scene comment exists but is corrupted:
            # probe the #726 external route first; otherwise this is the
            # ONLY trigger of `block_scene_failure` — a present-but-broken
            # scene is a writer bug or tampering and needs a human.
            if hooks.route_external_pr_ticket(issue, repo):
                continue
            hooks.block_scene_failure(issue, exc, repo, comments)
            continue
        except scene.SceneMissingError as exc:
            # No trusted comment carries a scene at all — a distinct branch
            # from corruption. The original #726 incident was
            # exactly this shape, so the external route is probed first.
            if hooks.route_external_pr_ticket(issue, repo):
                continue
            # Issue #1216: `awaiting_merge` can deliberately remove the
            # queue label before the delivery returns to `ai-fix-needed`.
            # An open PR on the stable internal branch is enough durable
            # identity to route this through the ordinary fresh-claim
            # takeover, which writes a new trusted scene before review.
            # Probe the exact branch (and require the helper's sole-PR
            # contract); no open PR falls through to the existing missing-
            # scene failure below.
            current_labels = _issue_label_set(issue)
            if (repo_dir is not None
                    and FIX_NEEDED_LABEL in current_labels
                    and open_pr_for_branch(
                        repo_dir,
                        task_branch(repo, int(issue["number"])),
                    ) is not None):
                found_scene = classify(
                    labels=current_labels, scene=None, pr_state="OPEN",
                    body_markers=body_markers(issue.get("body")),
                )
                if found_scene is DeliveryScene.FRESH_CLAIM:
                    return issue, None
                event(
                    "claim_yield", issue=int(issue["number"]),
                    reason=f"scene_{found_scene.value}",
                )
                continue
            # A runner may have created the PR and label, then lost the
            # scene comment write. Retry it from the durable local run
            # state; keep ai-pr-opened for another tick if the retry fails.
            recovered = (
                hooks.recover_missing_pr_scene(issue, repo, repo_dir)
                if repo_dir is not None else None
            )
            if recovered is not None:
                found = recovered
            elif repo_dir is not None and hooks.has_recoverable_pr_scene(
                    issue, repo, repo_dir):
                LOGGER.warning(
                    "issue=%s resume scene recovery pending: %s",
                    issue["number"], exc,
                )
                continue
            else:
                number = int(issue["number"])
                LOGGER.error("issue=%s resume scene is missing: %s", number, exc)
                marker = latest_run_marker(comments)
                try:
                    apply_label_patch(
                        number, repo=repo, event=EVENT_BLOCKED,
                        current_labels={FIX_NEEDED_LABEL},
                    )
                    comment_issue(
                        number, repo=repo,
                        body=(f"{marker}\n" if marker else "") + (
                            f"Orbi failed: {exc}; no trusted 'Orbi opened PR' "
                            "scene comment exists on this Issue, so the "
                            "opened-PR delivery cannot be resumed — this is an "
                            "external precondition the AI cannot safely judge "
                            "or fix, so it cannot be recovered automatically "
                            "(the Issue stays ai-blocked until a human "
                            "decides) — restore the trusted 'Orbi opened PR' "
                            "scene comment or relabel the Issue ai-fix-needed"
                        ),
                    )
                except Exception:
                    LOGGER.exception("issue=%s failure reporting failed", number)
                continue
        # The scan and the dispatch classify with the same pure
        # function. This scan owns the resumable route only: a candidate
        # that classifies elsewhere left the opened-PR state between the
        # query and this read (a relabel race) — this candidate is skipped
        # and the scan moves on. A candidate with no readable labels fails
        # open (the is_epic / is_release convention): the trusted scene is
        # the authority.
        current_labels = _issue_label_set(issue)
        if current_labels:
            found_scene = classify(
                labels=current_labels, scene=found, pr_state=None,
                body_markers=body_markers(issue.get("body")),
            )
            if found_scene is not DeliveryScene.RESUME_REVIEW:
                event(
                    "claim_yield", issue=int(issue["number"]),
                    reason=f"scene_{found_scene.value}",
                )
                continue
        return issue, found
    return None


def pick_next_delivery(
    repos: Sequence[str], slot_dir: Path, max_concurrency: int,
    active_milestone: str | None = None, config: RunnerConfig | None = None,
    *, hooks: ResumeHooks,
) -> tuple[str, dict, dict | None] | None:
    """Scan sources in order: resumable PRs, in-flight restarts, ready.

    Returns `(source_repo, issue, scene)` where `scene` is None for a
    fresh claim or a restart resume. Resuming an open PR keeps the
    single concurrency slot occupied by the same run (implement →
    review → fix → merge), so a second Pi is never started for a run
    that already has a PR. An in-flight Issue (a killed runner left
    `ai-in-progress` behind) is recovered before the ready
    scan: `process_issue`'s resume block reuses the newest worktree's
    run id, so the same progress comment is kept instead of a second
    run being started on an Issue that is already in flight.

    `active_milestone` scopes only the FRESH claim
    (`pick_issue`) — the resumable-PR and in-flight restart scans are
    resume states, and running an in-flight or opened-PR delivery to
    completion is never gated by a Milestone change.
    """
    # The sweep runs before a delivery is selected, so a fresh tick has no
    # task attempt yet.  Give its auditable comments one tick-scoped marker;
    # a selected delivery will replace this binding with its own attempt id.
    tick_run_id = current_run_id()
    if tick_run_id is None:
        tick_run_id = new_run_id()
        set_run_id(tick_run_id)
    for repo in repos:
        # Epic reconciliation is a per-tick bypass: a broken GitHub query or
        # mutation must never prevent the ordinary delivery scans.
        try:
            reconcile_open_epics(repo, tick_run_id)
        except Exception:
            LOGGER.exception("epic_reconcile_failed repo=%s", repo)
        # Milestone reconciliation intentionally follows the Epic sweep so a
        # just-closed final Epic can make its Milestone eligible this tick.
        # A classified 401/404 leaves a cross-process retry marker: systemd
        # starts a fresh Runner for every timer tick, so process-local
        # suppression would still issue and log the same failure every time.
        milestone_state_dir = slot_dir.parent
        if milestone_bookkeeping.milestone_reconcile_due(
            milestone_state_dir, repo,
        ):
            try:
                milestone_bookkeeping.reconcile_release_milestones(
                    repo, tick_run_id,
                )
                milestone_bookkeeping.clear_milestone_reconcile_failure(
                    milestone_state_dir, repo,
                )
            except milestone_bookkeeping.MilestoneReconcileError as exc:
                milestone_bookkeeping.record_milestone_reconcile_failure(
                    milestone_state_dir, repo, exc,
                )
            except Exception:
                LOGGER.exception("milestone_reconcile_failed repo=%s", repo)
        # Orphan-PR reconciliation follows the same bypass
        # pattern: a broken GitHub query must never prevent the ordinary
        # delivery scans.
        try:
            reconcile_orphan_prs(repo, tick_run_id, hooks=hooks)
        except Exception:
            LOGGER.exception("orphan_pr_reconcile_failed repo=%s", repo)
        if config is None:
            selected = pick_resumable_delivery(
                repo, slot_dir, max_concurrency, hooks=hooks,
            )
        else:
            selected = pick_resumable_delivery(
                repo, slot_dir, max_concurrency, config.repo_dir,
                hooks=hooks,
            )
        if selected is not None:
            issue, scene = selected
            return repo, issue, scene
    for repo in repos:
        _, dispatch_label = _repo_scan_keys(config, repo, active_milestone)
        issue = pick_in_progress_issue(
            repo, slot_dir, max_concurrency,
            dispatch_label=dispatch_label,
        )
        if issue is not None:
            return repo, issue, None
    for repo in repos:
        held = slot_held_deliveries(slot_dir, max_concurrency)
        issue = _pick_issue_with_repo_policy(repo, active_milestone, config, held)
        if issue is not None:
            return repo, issue, None
    return None


def _repo_scan_keys(
    config: RunnerConfig | None, repo: str, active_milestone: str | None,
) -> tuple[str | None, str]:
    """Resolve one source repo's scan keys from its policy.

    Returns `(active_milestone, dispatch_label)`. The read is fail-open and
    a malformed repository file is ignored here (host keys keep the scan
    alive): the claim blocks the Issue with the readable reason instead of
    silently claiming nothing.
    """
    milestone, dispatch_label, _policy = _repo_scan_context(
        config, repo, active_milestone,
    )
    return milestone, dispatch_label


def _repo_scan_context(
    config: RunnerConfig | None, repo: str, active_milestone: str | None,
) -> tuple[str | None, str, RepoPolicy | None]:
    """Resolve one source repo's scan keys AND the policy they came from.

    The idle path needs the policy itself to know whether `active_milestone`
    is landed in the repository file or in the host config, so both are
    returned from the same read instead of reading the file twice.
    """
    if config is None:
        return active_milestone, READY_LABEL, None
    try:
        policy = load_repo_policy(config, repo)
    except RepoConfigError as exc:
        event(
            "repo_config_invalid", level=logging.ERROR,
            repo=repo, reason=exc,
        )
        policy = None
    if policy is None:
        return active_milestone, READY_LABEL, None
    return (
        policy.active_milestone
        if policy.active_milestone is not None
        else active_milestone,
        policy.dispatch_label or READY_LABEL,
        policy,
    )


def _pick_issue_with_repo_policy(
    repo: str, active_milestone: str | None, config: RunnerConfig | None,
    held: frozenset = frozenset(),
) -> dict | None:
    """Fresh ready scan with the repo's scan keys.

    `dispatch_label` and `active_milestone` are resolved from the
    repository policy before the scan (see `_repo_scan_keys`), and
    `held` (the deliveries live co-runners work on) is passed through.
    """
    milestone, dispatch_label = _repo_scan_keys(
        config, repo, active_milestone,
    )
    if dispatch_label == READY_LABEL:
        return pick_issue(repo, milestone, held=held)
    return pick_issue(repo, milestone, dispatch_label=dispatch_label, held=held)
