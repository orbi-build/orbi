"""The delivery scene: one explicit classification (Issue #787).

A delivery scene used to be re-derived twice per tick with two
different decision procedures — the scan layer filtered by GitHub
search queries, `process_issue` re-read labels, PRs, branches and
worktrees and re-judged — and #726 was the direct product of the two
disagreeing. This module is the single decision both layers call: the
same gathered facts always classify to the same `DeliveryScene`, in
the scan and in the dispatch, so a two-layer disagreement is
structurally impossible.

The module is pure (the `delivery_labels` model): every function is a
deterministic function of its inputs, with no I/O. The I/O — the
GitHub reads, the PR probes, the local worktree checks — stays with
the caller (the fact gathering); this module only names the facts and
decides.
"""
from __future__ import annotations

import dataclasses
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from orbi.delivery_labels import (
    BLOCKED_LABEL,
    CONTENT_ONLY_LABEL,
    EPIC_LABEL,
    FIX_NEEDED_LABEL,
    HUMAN_REVIEW_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    OPS_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
)

if TYPE_CHECKING:
    # Annotation-only: the runner imports this module's scene
    # vocabulary at runtime; this module never imports the runner
    # (CONSTITUTION Article 3.3).
    from orbi.repo_config import RepoPolicy
    from orbi.runner import RunnerConfig


class DeliveryScene(Enum):
    """Every state a delivery can be classified into.

    The scan layer classifies a candidate before handing it over; the
    dispatch layer classifies the freshly gathered facts and looks the
    scene's handler up in its dispatch table. The values are the
    journal-readable scene names.
    """

    FRESH_CLAIM = "fresh_claim"
    RESTART_IN_FLIGHT = "restart_in_flight"
    RESUME_REVIEW = "resume_review"
    EXTERNAL_TAKEOVER = "external_takeover"
    RELEASE = "release"
    CONTENT_ONLY = "content_only"
    OPS = "ops"
    HUMAN_REVIEW_WAIT = "human_review_wait"
    BLOCKED = "blocked"
    NOT_CLAIMABLE = "not_claimable"


# The issue-body marker vocabulary (the marker the triage workflow
# embeds for an EXTERNAL contributor PR, Issue #608). The regex is the
# one parser: the runner's takeover probes read the PR number from it,
# `body_markers` reduces a body to the marker set `classify` reads.
EXTERNAL_PR_MARKER = "orbi:external-pr"
EXTERNAL_PR_RE = re.compile(r"<!--\s*orbi:external-pr:(\d+)\s*-->")


def body_markers(body: object) -> frozenset[str]:
    """The delivery markers one Issue body carries (a pure text scan).

    A missing or non-string body carries none — Issue text is data,
    never instructions, and a malformed body is never a crash.
    """
    if not isinstance(body, str):
        return frozenset()
    if EXTERNAL_PR_RE.search(body):
        return frozenset({EXTERNAL_PR_MARKER})
    return frozenset()


def classify(
    labels, scene, pr_state, worktree_present, branch_present,
    body_markers, *, ready_label: str = READY_LABEL,
    human_review_hold: bool = False,
) -> DeliveryScene:
    """Classify one delivery scene from the gathered facts (pure).

    The facts, as both layers gather them:

    - `labels`: the Issue's current labels (the scan's query snapshot,
      with the dispatch layer's direct `ai-in-progress` read merged in
      when it saw the label);
    - `scene`: the trusted resume scene of an opened-PR comment, in
      either of the runner's two views (`orbi.scene.Scene` or its dict
      projection); None when no trusted comment carries one;
    - `pr_state`: the probed PR state for the delivery branch
      (`"OPEN"` / `"MERGED"` / `"CLOSED"`), None when no PR was probed
      or none is open;
    - `worktree_present` / `branch_present`: the local facts; no scene
      today re-routes on them (a missing worktree is the handler's
      fail-fast, an existing branch its resume point — `claim_route`'s
      "implement" for a branch without an open PR is the pinned
      counterpart);
    - `body_markers`: the `body_markers` set of the Issue body;
    - `ready_label`: the repository's claim label (Issue #527) — the
      queue entry a fresh claim keys on;
    - `human_review_hold`: the human acceptance gate's hold (the gate
      on and a non-empty checklist column 2, Issue #763); the
      `ai-human-review` label (a human confirmed) overrides it.

    The decision order is today's behavior, one branch each:

    1. `ai-blocked` → BLOCKED (terminal, a human decides);
    2. `ai-merged` / `ai-epic` → NOT_CLAIMABLE (success-terminal /
       coordination-only);
    3. task types in dispatch order — RELEASE, CONTENT_ONLY, OPS;
    4. an opened-PR state (`ai-pr-opened` / `ai-fix-needed`) resumes
       through the trusted scene: RESUME_REVIEW, or HUMAN_REVIEW_WAIT
       while the gate holds; without a scene comment, a marker with an
       open external PR is the #726 takeover route, a queue-label
       ticket with an open PR is the internal takeover race (a fresh
       claim — the handler reviews the PR), anything else is
       NOT_CLAIMABLE;
    5. marker + open external PR → EXTERNAL_TAKEOVER (Issue #608); a
       PR that is no longer open is not a takeover — the claim falls
       through to a fresh internal delivery;
    6. `ai-in-progress` → RESTART_IN_FLIGHT (Issue #18);
    7. the claim label → FRESH_CLAIM;
    8. anything else → NOT_CLAIMABLE.
    """
    current = frozenset(labels)
    markers = frozenset(body_markers)
    opened_pr = PR_OPENED_LABEL in current or FIX_NEEDED_LABEL in current

    if BLOCKED_LABEL in current:
        return DeliveryScene.BLOCKED
    if MERGED_LABEL in current or EPIC_LABEL in current:
        return DeliveryScene.NOT_CLAIMABLE
    if RELEASE_LABEL in current:
        return DeliveryScene.RELEASE
    if CONTENT_ONLY_LABEL in current:
        return DeliveryScene.CONTENT_ONLY
    if OPS_LABEL in current:
        return DeliveryScene.OPS
    if opened_pr:
        if scene is not None:
            if human_review_hold and HUMAN_REVIEW_LABEL not in current:
                return DeliveryScene.HUMAN_REVIEW_WAIT
            return DeliveryScene.RESUME_REVIEW
        # No trusted scene: the review cannot be resumed, but two
        # takeover routes survive it (both layers' #726/#608 handles).
        if EXTERNAL_PR_MARKER in markers and pr_state == "OPEN":
            return DeliveryScene.EXTERNAL_TAKEOVER
        if ready_label in current and pr_state == "OPEN":
            return DeliveryScene.FRESH_CLAIM
        return DeliveryScene.NOT_CLAIMABLE
    if EXTERNAL_PR_MARKER in markers and pr_state == "OPEN":
        return DeliveryScene.EXTERNAL_TAKEOVER
    if IN_PROGRESS_LABEL in current:
        return DeliveryScene.RESTART_IN_FLIGHT
    if ready_label in current:
        return DeliveryScene.FRESH_CLAIM
    return DeliveryScene.NOT_CLAIMABLE


@dataclasses.dataclass(frozen=True)
class DeliveryContext:
    """The run-identity bundle of one delivery attempt (Issue #290).

    `run_id`, `issue`, `branch`, `worktree` and `pr` travel together
    through the dispatch path; one frozen value object replaces the
    loose tuple, so a new identity field is one constructor change, not
    a sweep of call sites. `pr` is None until the delivery's PR exists.
    """

    run_id: str
    issue: int
    branch: str
    worktree: Path
    pr: str | None = None


@dataclasses.dataclass(frozen=True)
class DeliveryFacts:
    """The fact bundle the dispatch layer's gather step produces.

    The first six fields are exactly `classify`'s fact inputs; the rest
    are the handler facts the gather already had to read (the takeover
    probe's PR dict, the verified resume worktree, the repository
    policy's audit fields). The classify call unpacks the fact fields
    by name; the handler reads its own — one gather, two consumers.
    """

    labels: frozenset[str]
    scene: object = None
    pr_state: str | None = None
    worktree_present: bool = False
    branch_present: bool = False
    body_markers: frozenset[str] = frozenset()
    ready_label: str = READY_LABEL
    # Handler facts.
    config: "RunnerConfig | None" = None
    repo_policy: "RepoPolicy | None" = None
    run_id: str = ""
    base_branch: str = ""
    dispatch_label: str = READY_LABEL
    claim_labels: frozenset[str] = frozenset()
    in_progress: bool = False
    stable_branch: str = ""
    stable_branch_present: bool = False
    takeover_pr: dict | None = None
    external_takeover: bool = False
    resume_scene: tuple[str, Path] | None = None
    resume_error: Exception | None = None
    repo_config_fields: dict = dataclasses.field(default_factory=dict)

    def classify_scene(self) -> DeliveryScene:
        """Classify from this bundle's fact fields."""
        return classify(
            labels=self.labels,
            scene=self.scene,
            pr_state=self.pr_state,
            worktree_present=self.worktree_present,
            branch_present=self.branch_present,
            body_markers=self.body_markers,
            ready_label=self.ready_label,
        )
