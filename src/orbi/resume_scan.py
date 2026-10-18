"""Which opened-PR delivery one tick resumes (Issues #45, #82, #1745).

Extracted from `orbi.claim` (Constitution Article 3.5 / Issue #1229: a
module that crosses 1,000 lines is split by domain before the next feature
lands in it). `claim` keeps the ready, in-flight and release scans plus the
tick ordering; the opened-PR CANDIDATE scan lives here, one implementation
shared by the two label sets it runs for: the reviewable states
(`ai-pr-opened` / `ai-fix-needed`, scanned first) and the maintainer wait
(`ai-awaiting-merge`, scanned only after every other scan found nothing).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from orbi import language, scene
from orbi.blocked_comment import post_blocked_comment
from orbi.checks import _classify_rollup, _render_check
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL, BLOCKED_LABEL, FIX_NEEDED_LABEL, MERGED_LABEL,
    PR_OPENED_LABEL,
)
from orbi.delivery_scene import (
    DeliveryScene, _issue_label_set, body_markers, classify,
)
from orbi.github import (
    issue_comments, latest_run_marker, list_issues, open_pr_for_branch,
    pr_delivery_rollup,
)
from orbi.gitops import task_branch
from orbi.journal import LOGGER, event
from orbi.pilot_slots import slot_held_deliveries


class ResumeHooks(NamedTuple):
    """The delivery-side helpers `runner.main` passes in (Article 3.3).

    The physical scene handling of `pick_resumable_delivery` plus the PR
    comment writer `reconcile_orphan_prs` reports its orphan through.
    Every hook that writes a comment takes the claim's already-resolved
    per-repository `language_code` (Issue #1687): the claim knows the
    merged repository config before the first comment is written.
    """
    resume_scene: Callable[[list[dict]], dict]
    route_external_pr_ticket: Callable[..., bool]
    block_scene_failure: Callable[..., None]
    recover_missing_pr_scene: Callable[..., dict | None]
    has_recoverable_pr_scene: Callable[[dict, str, Path], bool]
    comment_pr: Callable[..., None]


def pick_resumable_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
    repo_dir: Path | None = None, *, hooks: ResumeHooks,
    language_code: str,
) -> tuple[dict, dict] | None:
    """Return the oldest FREE reviewable opened-PR delivery and its scene.

    Two opened-PR states are scanned here: `ai-fix-needed`
    (awaiting the next review session after a finding or a base
    conflict — the review session fixes findings in the same
    session) and `ai-pr-opened` (awaiting review). `ai-awaiting-merge`
    is NOT scanned here (Issue #1745): that delivery waits for a
    maintainer, so re-checking it on every tick let one
    maintainer-blocked PR starve the review, restart and ready scans.
    `pick_next_delivery` runs the dedicated
    `pick_awaiting_merge_delivery` scan only after all its scans found
    nothing for the tick. The `ai-pr-opened`
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

    The candidate rules (held deliveries, the page size, oldest first,
    CI-pending skip) are the shared opened-PR ones documented on
    `_pick_opened_pr_delivery`.
    """
    return _pick_opened_pr_delivery(
        repo, slot_dir, max_concurrency, repo_dir,
        search_labels=(FIX_NEEDED_LABEL, PR_OPENED_LABEL),
        hooks=hooks, language_code=language_code,
    )


def pick_awaiting_merge_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
    repo_dir: Path | None = None, *, hooks: ResumeHooks,
    language_code: str,
) -> tuple[dict, dict] | None:
    """Return the oldest FREE `ai-awaiting-merge` delivery and its scene.

    Issue #1745: an `ai-awaiting-merge` delivery waits for a maintainer
    (the review passed, a known repository policy blocks the merge), so
    retrying it on every tick used to take the slot away from work the
    runner CAN move — a newer `ai-pr-opened`/`ai-fix-needed` review, a
    killed `ai-in-progress` restart, or a fresh `ai-ready` claim.
    `pick_next_delivery` therefore runs this scan only AFTER all three
    of those scans found nothing for the tick. `delivery_step` then
    reruns the merge gate on the same head (merge-only, and the #1422
    handoff comment is de-duplicated, so an unchanged blocker posts
    nothing).

    The cost is explicit and documented: after the maintainer acts the
    merge happens on the first tick whose queue is otherwise empty, not
    on the very next tick.

    The candidate rules are the shared opened-PR ones (see
    `_pick_opened_pr_delivery`): held deliveries are skipped and the
    page is `max_concurrency + 1` candidates, oldest Issue first
    (`sort:created-asc`, Issue #1734).
    """
    return _pick_opened_pr_delivery(
        repo, slot_dir, max_concurrency, repo_dir,
        search_labels=(AWAITING_MERGE_LABEL,),
        hooks=hooks, language_code=language_code,
    )


def _pick_opened_pr_delivery(
    repo: str, slot_dir: Path, max_concurrency: int,
    repo_dir: Path | None, *, search_labels: Sequence[str],
    hooks: ResumeHooks, language_code: str,
) -> tuple[dict, dict] | None:
    """Scan ONE opened-PR label set for the oldest FREE resumable delivery.

    `search_labels` is the label group the query matches (GitHub's OR
    within one `label:` qualifier): the reviewable states for
    `pick_resumable_delivery`, the single `ai-awaiting-merge` state for
    `pick_awaiting_merge_delivery`. Everything else is shared, so the
    two scans can never drift apart in candidate handling:

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

    The query page is `max_concurrency + 1` candidates, ordered OLDEST
    Issue first (`sort:created-asc`, Issue #1734). At most
    `max_concurrency` deliveries can be held by live co-runners, so a
    free candidate is inside the page when one exists; one whose OPEN PR
    still waits for CI (Issue #1473) is skipped too. The scan reviews
    the oldest FREE candidate (held ones are skipped before any
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
            f"label:{','.join(search_labels)} "
            f"-label:{BLOCKED_LABEL} -label:{MERGED_LABEL} sort:created-asc"
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
            if hooks.route_external_pr_ticket(issue, repo, language_code=language_code):
                continue
            hooks.block_scene_failure(issue, exc, repo, comments, language_code=language_code)
            continue
        except scene.SceneMissingError as exc:
            # No trusted comment carries a scene at all — a distinct branch
            # from corruption. The original #726 incident was
            # exactly this shape, so the external route is probed first.
            if hooks.route_external_pr_ticket(issue, repo, language_code=language_code):
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
                hooks.recover_missing_pr_scene(issue, repo, repo_dir, language_code=language_code)
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
                    post_blocked_comment(
                        number, repo=repo,
                        body=(f"{marker}\n" if marker else "")
                        + language.text(
                            language_code, "claim.scene_missing", error=exc,
                        ),
                        current_labels={FIX_NEEDED_LABEL},
                        language_code=language_code,
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
        # Issue #1473: skip a CI-pending OPEN PR; MERGED/CLOSED and a
        # failed rollup read still return the candidate.
        try:
            pr_state, rollup = pr_delivery_rollup(found["pr_url"], repo)
        except Exception:
            return issue, found
        pending = _classify_rollup(rollup)[0] if pr_state == "OPEN" else []
        if pending:
            event("delivery_ci_pending", issue=int(issue["number"]),
                  pr=found["pr_url"], pending="; ".join(map(_render_check, pending)))
            continue
        return issue, found
    return None
