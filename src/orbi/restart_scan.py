"""The in-flight restart scan and the scenes a claim scan may claim.

Extracted from `orbi.claim` (Constitution Article 3.5: a module is split by
domain before a new feature lands in it) for Issue #2256, which gave the scan
its own question and its own answer.

That question is the one the other claim scans never ask: does THIS
deployment hold the killed run's task worktree? The `ai-in-progress` label
lives on GitHub and is therefore shared by every deployment that serves the
repository, from any machine, while the slots that prove a live runner and
the worktree with its run state file are local to one. That pair is the only
proof a run was started here — `worktree_resume_scene` is its one lookup, and
`runner.process_issue` resumes in the scene it returns.

The scene sets below are the second shared piece: `SCAN_CLAIMABLE_SCENES` is
what a scan of the claim may hand to the dispatch (`claim._pick_from_scan`
for the ready scans, this restart scan for the in-flight ones).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from orbi import run_state
from orbi.delivery_labels import (
    BLOCKED_LABEL, EPIC_LABEL, FIX_NEEDED_LABEL, IN_PROGRESS_LABEL,
    MERGED_LABEL, PR_OPENED_LABEL, label_name_set,
)
from orbi.delivery_scene import DeliveryScene, body_markers, classify
from orbi.github import list_issues
from orbi.journal import event
from orbi.pilot_slots import slot_occupancy

# Issue #2256: one search result page (the search API's maximum). The scan
# skips every candidate this deployment holds no worktree for, so a
# single-result read would let such a skip hide this deployment's own orphan
# further down the list.
IN_FLIGHT_SCAN_LIMIT = 100

# The scenes the ready scans claim: the fresh-claim family of the
# delivery-scene classification. A release candidate is claimable only
# through the release fallback scan's `allow_release` gate; the
# classification still names its scene. A candidate that classifies
# elsewhere is skipped — a terminal state the query's index lagged behind,
# or an in-flight/opened-PR state another scan owns — and a candidate
# carrying NO readable labels fails open (the is_epic/is_release
# convention): the query is the claim authority.
FRESH_CLAIM_SCENES = frozenset({
    DeliveryScene.FRESH_CLAIM,
    DeliveryScene.RELEASE,
    DeliveryScene.CONTENT_ONLY,
    DeliveryScene.OPS,
})
# ... plus the restart scan's own: a killed run keeps `ai-ready`, so the
# query matches it and only the classification can tell it apart.
SCAN_CLAIMABLE_SCENES = FRESH_CLAIM_SCENES | {
    DeliveryScene.RESTART_IN_FLIGHT,
}


def worktree_resume_scene(
    repo_dir: Path, source_repo: str, number: int,
) -> tuple[str, Path] | None:
    """Return this deployment's resume scene `(run_id, worktree)`, or None.

    The worktrees are matched by the RUN STATE FILE, not
    by the directory name alone: the directory name carries the
    source-repo slug, which changes when the repo is renamed, while
    the state file carries the issue number and the repo NAME (the
    part after the slash — stable across a rename). The newest
    matching worktree by its OWN `created_at` stamp wins — never the
    directory mtime (Issue #2175): a retained terminal scene sits beside
    the new run and its detach touched its directory last. The scene's
    worktree path may carry the OLD slug (a rename): it is the scene the
    run continues in, never a reason for a second worktree.

    A worktree that claims THIS issue number but has a MISSING or
    CORRUPT run state file cannot be verified as the same run: it
    fails fast with the exact reason — never a silent fresh redo.
    A worktree of another issue without a state file
    (a legacy completed run) is unrelated and skipped.
    """
    name = source_repo.rsplit("/", 1)[-1]
    pattern = re.compile(
        r"^orbi-.+-issue-" + str(number) + r"-[0-9a-f]{8}$",
    )
    candidates: list[tuple[Path, dict]] = []
    worktrees = repo_dir / ".worktrees"
    if worktrees.is_dir():
        for path in worktrees.iterdir():
            if not path.is_dir() or not pattern.match(path.name):
                continue
            try:
                state = run_state.read_run_state(path)
            except ValueError as exc:
                raise RuntimeError(
                    f"worktree {path} has a corrupt run state file "
                    f"({exc}): the same run cannot be verified "
                    "(Issue #219)"
                ) from exc
            if state is None:
                raise RuntimeError(
                    f"worktree {path} has no run state file "
                    f"({run_state.run_state_path(path)}): the same run cannot "
                    "be verified (Issue #219)"
                )
            if str(state["repo"]).rsplit("/", 1)[-1] != name:
                continue
            candidates.append((path, state))
    if not candidates:
        return None
    # Newest by the scene's own start stamp; the directory name breaks a
    # same-second tie, so the choice is deterministic on every tick.
    newest, state = max(
        candidates,
        key=lambda candidate: (
            str(candidate[1].get("created_at", "")), candidate[0].name,
        ),
    )
    return str(state["run_id"]), newest


def _this_deployments_run(
    repo_dir: Path | None, repo: str, number: int,
) -> bool:
    """Whether THIS deployment holds the run's worktree + run state file.

    A local worktree that names the Issue but carries no verifiable run
    state (a kill between the worktree creation and the state file) is
    still this deployment's run beyond doubt, so it answers True: the
    dispatch reports that scene through the terminal failure path
    (Issue #219) instead of the scan skipping a run it started.
    """
    if repo_dir is None:
        return False
    try:
        return worktree_resume_scene(repo_dir, repo, number) is not None
    except RuntimeError:
        return True


def pick_in_progress_issue(
    repo: str, slot_dir: Path, max_concurrency: int,
    repo_dir: Path | None = None,
) -> dict | None:
    """Return one in-flight Issue THIS deployment started and must resume.

    A killed runner leaves the task worktree, the run state file and the
    `ai-in-progress` label behind (the failure path never ran). The ready
    scan excludes `ai-in-progress`, so without this scan such a run is
    never resumed and the Issue is stuck forever; `process_issue`'s resume
    block then reuses the run instead of starting a second one. The query
    keys on `ai-in-progress` alone (Issue #2309): the claim REMOVES the
    dispatch label, so the queue entry can never name an interrupted run,
    and a custom `dispatch_label` changes nothing here.

    Only a run THIS deployment started is resumed (Issue #2256). The label
    is shared on GitHub by every deployment serving the repository, while
    the slots that prove a live runner are local, so another machine's live
    delivery is invisible here — its missing worktree is not.
    `worktree_resume_scene` is that proof: a candidate without a local
    worktree and run state file belongs to another deployment, so it is
    skipped (journaled, no label change, no comment) while the scan keeps
    reading, and the skip never hides this deployment's own orphan further
    down the list. Without a `repo_dir` nothing is verifiable, so nothing
    is resumed. A local worktree that names the Issue but carries no
    verifiable run state (a kill between the worktree creation and the
    state file) is this deployment's run beyond doubt, so it is returned
    for the dispatch's terminal report (Issue #219) instead of being
    skipped.

    The scan runs only when no OTHER runner is live: a slot held by another
    process proves a live runner is working (on this or another Issue), so
    the label is in flight, not orphaned. This runner's own slot is
    excluded: `main` took it before the claim scan.
    """
    mine = os.getpid()
    for _, holder in slot_occupancy(slot_dir, max_concurrency):
        if holder is not None and holder != mine:
            return None
    # `labels`: a P0 a killed runner left behind keeps its priority in the
    # progress comment on resume. `milestone`: a release run killed
    # mid-release is resumed with THIS dict, and `process_release` scopes its
    # leftover-delivery gate to the release's own Milestone — the Issue is
    # the authority (never `active_milestone`: a resume is not gated by a
    # Milestone change).
    issues = list_issues(
        repo, state="open",
        search=(
            f"label:{IN_PROGRESS_LABEL} "
            f"-label:{PR_OPENED_LABEL} -label:{FIX_NEEDED_LABEL} "
            f"-label:{MERGED_LABEL} -label:{BLOCKED_LABEL} "
            f"-label:{EPIC_LABEL}"
        ),
        json_fields="number,title,body,labels,milestone",
        limit=IN_FLIGHT_SCAN_LIMIT,
    )
    for candidate in issues:
        number = int(candidate["number"])
        # The same pure classification the dispatch runs: this scan hands
        # the candidate to `process_issue`, so it skips only a candidate
        # that left EVERY claimable state (a relabel inside the query's
        # staleness window); no readable labels fails open (the
        # is_epic/is_release convention).
        current_labels = label_name_set(candidate)
        if current_labels:
            scene_value = classify(
                labels=current_labels, scene=None, pr_state=None,
                body_markers=body_markers(candidate.get("body")),
            )
            if scene_value not in SCAN_CLAIMABLE_SCENES:
                event(
                    "claim_yield", issue=number,
                    reason=f"scene_{scene_value.value}",
                )
                continue
        # Issue #2256: the label alone does not prove the run is ours. No
        # local worktree + run state file means no scene to resume —
        # another deployment's live delivery, or one whose deployment is
        # gone (a human relabels that one).
        if not _this_deployments_run(repo_dir, repo, number):
            event(
                "restart_scan_skipped", issue=number,
                reason="no_local_run_state",
            )
            continue
        return candidate
    return None
