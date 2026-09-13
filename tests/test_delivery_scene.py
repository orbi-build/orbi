"""The delivery scene classification (Issue #787).

`classify` is the single pure decision the scan layer and the dispatch
layer share: the same facts must classify to the same scene in both
layers, so a "#726-style two-layer disagreement" is structurally
impossible. The tests below enumerate the fact combinations and assert
the classified scene — no `gh` stub, no I/O: the module is pure.
"""
import dataclasses
import json

import pytest

from orbi import delivery_scene
from orbi import runner, scene
from orbi.delivery_scene import (
    EXTERNAL_PR_MARKER,
    DeliveryContext,
    DeliveryScene,
    body_markers,
    classify,
)
from tests.seam import seam as runner_seam
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    CONTENT_ONLY_LABEL,
    EPIC_LABEL,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    OPS_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
)
from orbi.scene import Scene

MARKER = {EXTERNAL_PR_MARKER}
TRUSTED_SCENE = {
    "run_id": "a1b2c3d4",
    "base_branch": "main",
    "base_sha": "0ab8d1f8",
    "pr_url": "https://github.com/o/r/pull/9",
    "external": "",
}
TRUSTED_SCENE_RECORD = Scene(
    run_id="a1b2c3d4",
    base_branch="main",
    base_sha="0ab8d1f8",
    pr_url="https://github.com/o/r/pull/9",
)


@pytest.mark.parametrize(
    (
        "labels", "scene", "pr_state", "markers", "expected",
    ),
    [
        # --- Terminal and coordination states are never claimable. ---
        ({BLOCKED_LABEL}, None, None, frozenset(), DeliveryScene.BLOCKED),
        ({MERGED_LABEL}, None, None, frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        ({READY_LABEL, MERGED_LABEL}, None, None, frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        ({EPIC_LABEL}, None, None, frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        ({READY_LABEL, EPIC_LABEL}, None, None, frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        # --- Task types, today's dispatch order: release > content > ops.
        (
            {READY_LABEL, RELEASE_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.RELEASE,
        ),
        (
            {READY_LABEL, CONTENT_ONLY_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.CONTENT_ONLY,
        ),
        (
            {READY_LABEL, OPS_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.OPS,
        ),
        ({OPS_LABEL, IN_PROGRESS_LABEL}, None, None, frozenset(),
         DeliveryScene.OPS),
        (
            {RELEASE_LABEL, CONTENT_ONLY_LABEL, OPS_LABEL}, None, None,
            frozenset(), DeliveryScene.RELEASE,
        ),
        # A terminal state outranks every task type.
        (
            {READY_LABEL, RELEASE_LABEL, BLOCKED_LABEL}, None, None,
            frozenset(), DeliveryScene.BLOCKED,
        ),
        # --- Opened-PR states resume through the trusted scene. ---
        ({PR_OPENED_LABEL}, TRUSTED_SCENE, None, frozenset(),
         DeliveryScene.RESUME_REVIEW),
        ({FIX_NEEDED_LABEL}, TRUSTED_SCENE, None, frozenset(),
         DeliveryScene.RESUME_REVIEW),
        # Issue #178: a killed review runner leaves the in-flight label
        # behind; the opened-PR state still resumes the review.
        (
            {PR_OPENED_LABEL, IN_PROGRESS_LABEL}, TRUSTED_SCENE, None,
            frozenset(), DeliveryScene.RESUME_REVIEW,
        ),
        # The opened-PR transition keeps `ai-ready`; the trusted scene
        # still decides (the resumable scan's world).
        (
            {READY_LABEL, PR_OPENED_LABEL}, TRUSTED_SCENE, None,
            frozenset(), DeliveryScene.RESUME_REVIEW,
        ),
        # The scene record is accepted in either of the runner's two
        # views: the `Scene` dataclass or its dict projection.
        (
            {PR_OPENED_LABEL}, TRUSTED_SCENE_RECORD, None, frozenset(),
            DeliveryScene.RESUME_REVIEW,
        ),
        # Without the trusted scene the review cannot be resumed.
        ({PR_OPENED_LABEL}, None, None, frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        ({FIX_NEEDED_LABEL}, None, "OPEN", frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        # Issue #726: a marker ticket with an open external PR routes to
        # the takeover even without a scene comment.
        (
            {PR_OPENED_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.EXTERNAL_TAKEOVER,
        ),
        (
            {FIX_NEEDED_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.EXTERNAL_TAKEOVER,
        ),
        # Issue #608 internal counterpart: a fresh-claim ticket whose
        # stable branch grew an open PR mid-preparation stays a fresh
        # claim — the handler takes the PR over for review.
        (
            {READY_LABEL, PR_OPENED_LABEL}, None, "OPEN", frozenset(),
            DeliveryScene.FRESH_CLAIM,
        ),
        # --- External takeover (Issue #608): marker + open PR. ---
        (
            {READY_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.EXTERNAL_TAKEOVER,
        ),
        # A PR that is no longer open is not a takeover: the claim
        # proceeds as a fresh internal delivery.
        (
            {READY_LABEL}, None, "MERGED", MARKER,
            DeliveryScene.FRESH_CLAIM,
        ),
        (
            {READY_LABEL}, None, "CLOSED", MARKER,
            DeliveryScene.FRESH_CLAIM,
        ),
        # The marker without an open PR probe never routes the takeover.
        ({READY_LABEL}, None, None, MARKER, DeliveryScene.FRESH_CLAIM),
        # An in-flight external takeover (the run died between the
        # worktree creation and the opened-PR transition) resumes the
        # takeover delivery.
        (
            {READY_LABEL, IN_PROGRESS_LABEL}, None, "OPEN", MARKER,
            DeliveryScene.EXTERNAL_TAKEOVER,
        ),
        # --- In-flight restart (Issue #18). ---
        ({IN_PROGRESS_LABEL}, None, None, frozenset(),
         DeliveryScene.RESTART_IN_FLIGHT),
        (
            {READY_LABEL, IN_PROGRESS_LABEL}, None, None, frozenset(),
            DeliveryScene.RESTART_IN_FLIGHT,
        ),
        # --- Fresh claim. ---
        ({READY_LABEL}, None, None, frozenset(),
         DeliveryScene.FRESH_CLAIM),
        # A repository policy may replace the queue label (Issue #527).
        ("my-queue", None, None, frozenset(), DeliveryScene.NOT_CLAIMABLE),
    ],
)
def test_classify_decision_table(labels, scene, pr_state, markers, expected):
    assert classify(
        labels, scene, pr_state,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(markers),
    ) is expected


def test_classify_custom_dispatch_label():
    """The repository's claim label (Issue #527) is the queue entry:
    a custom-label ticket is fresh-claimable only under its own label."""
    assert classify(
        {"my-queue"}, None, None,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(), ready_label="my-queue",
    ) is DeliveryScene.FRESH_CLAIM
    assert classify(
        {"my-queue", IN_PROGRESS_LABEL}, None, None,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(), ready_label="my-queue",
    ) is DeliveryScene.RESTART_IN_FLIGHT
    assert classify(
        {"my-queue"}, None, "OPEN",
        worktree_present=False, branch_present=False,
        body_markers=frozenset(MARKER), ready_label="my-queue",
    ) is DeliveryScene.EXTERNAL_TAKEOVER


def test_classify_human_review_hold():
    """The human acceptance gate (Issue #763): a held opened-PR delivery
    waits while no human has confirmed (`ai-human-review` absent); the
    label's presence resumes the review, and no hold is a plain resume."""
    hold_kwargs = {"human_review_hold": True}
    assert classify(
        {PR_OPENED_LABEL}, TRUSTED_SCENE, None,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.HUMAN_REVIEW_WAIT
    assert classify(
        {FIX_NEEDED_LABEL}, TRUSTED_SCENE, None,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.HUMAN_REVIEW_WAIT
    assert classify(
        {PR_OPENED_LABEL, "ai-human-review"}, TRUSTED_SCENE,
        None, worktree_present=False, branch_present=False,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.RESUME_REVIEW
    assert classify(
        {PR_OPENED_LABEL}, TRUSTED_SCENE, None,
        worktree_present=False, branch_present=False,
        body_markers=frozenset(),
    ) is DeliveryScene.RESUME_REVIEW


@pytest.mark.parametrize(
    "labels,scene,pr_state,markers",
    [
        ({READY_LABEL}, None, None, frozenset()),
        ({READY_LABEL}, None, "OPEN", frozenset()),
        ({READY_LABEL}, None, "OPEN", MARKER),
        ({READY_LABEL, IN_PROGRESS_LABEL}, None, None, frozenset()),
        ({PR_OPENED_LABEL}, TRUSTED_SCENE, None, frozenset()),
        ({READY_LABEL, OPS_LABEL}, None, None, frozenset()),
        ({READY_LABEL, RELEASE_LABEL}, None, None, frozenset()),
    ],
)
def test_classify_ignores_local_presence(labels, scene, pr_state, markers):
    """No scene today keys on the local worktree/branch presence: a
    missing worktree is the handler's fail-fast, an existing branch the
    handler's resume point — both facts ride along, neither re-routes
    the classification (claim_route's "implement" for a branch without
    an open PR is the pinned counterpart)."""
    expected = classify(
        labels, scene, pr_state, worktree_present=False,
        branch_present=False, body_markers=frozenset(markers),
    )
    for worktree_present in (False, True):
        for branch_present in (False, True):
            assert classify(
                labels, scene, pr_state,
                worktree_present=worktree_present,
                branch_present=branch_present,
                body_markers=frozenset(markers),
            ) is expected


def test_body_markers():
    """The marker vocabulary is the triage workflow's hidden external-PR
    marker (Issue #608); a missing or non-string body carries none."""
    assert body_markers("<!-- orbi:external-pr:55 -->\nfix it") == MARKER
    assert body_markers("plain body") == frozenset()
    assert body_markers(None) == frozenset()
    assert body_markers(42) == frozenset()


def test_external_pr_regex_is_the_marker_vocabulary_owner():
    """The regex the runner's takeover probes parse the PR number with
    lives beside the marker vocabulary — one concept, one implementation
    (CONSTITUTION Article 4.2)."""
    match = delivery_scene.EXTERNAL_PR_RE.search(
        "<!-- orbi:external-pr:55 -->",
    )
    assert match.group(1) == "55"


def test_delivery_context_is_a_frozen_bundle():
    """The run-identity bundle (Issue #290): one frozen value object
    instead of the loose four-tuple threaded through the dispatch path."""
    context = DeliveryContext(
        run_id="a1b2c3d4", issue=787,
        branch="orbi/o-r-issue-787",
        worktree="/wt/orbi-o-r-issue-787-a1b2c3d4",
        pr="https://github.com/o/r/pull/9",
    )
    assert context.run_id == "a1b2c3d4"
    assert context.issue == 787
    assert context.pr == "https://github.com/o/r/pull/9"
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.issue = 788


def _issue_list_fake(monkeypatch, issues: list[dict]):
    """Answer the scan's one `gh issue list` from a fixed payload.

    Patches the ONE subprocess seam (never a runner module global);
    the dispatch keys on the subcommand name, not the argv shape."""
    def fake_run(command, **kwargs):
        if "list" in command:
            return json.dumps(issues)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner_seam, "run_command", fake_run)


def test_issue_list_fake_fails_loud_on_unexpected_commands(monkeypatch):
    """The seam fake is a contract, not a sink: an argv outside the
    scan's one read fails the test, never a silent empty success."""
    _issue_list_fake(monkeypatch, [])
    with pytest.raises(AssertionError, match="unexpected command"):
        runner_seam.run_command(["gh", "pr", "view", "9"])


def _trusted_scene_comment() -> dict:
    body = scene.render(scene.Scene(
        run_id="a1b2c3d4", base_branch="main", base_sha="abc123def456",
        pr_url="https://github.com/owner/repo/pull/9",
    ))
    return {"body": body, "authorAssociation": "OWNER"}


def test_pick_resumable_delivery_skips_candidate_relabelled_blocked(
    monkeypatch, tmp_path,
):
    """The scan and the dispatch classify with the same pure function
    (Issue #787): a candidate relabelled `ai-blocked` inside the query's
    staleness window classifies BLOCKED and this scan claims nothing —
    a terminal state is never resumed."""
    monkeypatch.setattr(runner_seam, "slot_occupancy",
                        lambda *a, **k: [])
    monkeypatch.setattr(
        runner_seam, "issue_comments",
        lambda number, repo: [_trusted_scene_comment()],
    )
    _issue_list_fake(monkeypatch, [{
        "number": 9, "title": "ship", "state": "OPEN",
        "url": "https://github.com/owner/repo/issues/9",
        "body": "b", "labels": [{"name": "ai-blocked"}],
    }])
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None


def test_pick_resumable_delivery_resumes_a_scene_candidate(
    monkeypatch, tmp_path,
):
    """The same classification keeps the route honest in the positive
    direction: an opened-PR candidate with a trusted scene classifies
    RESUME_REVIEW and is returned with its scene."""
    monkeypatch.setattr(runner_seam, "slot_occupancy",
                        lambda *a, **k: [])
    monkeypatch.setattr(
        runner_seam, "issue_comments",
        lambda number, repo: [_trusted_scene_comment()],
    )
    _issue_list_fake(monkeypatch, [{
        "number": 9, "title": "ship", "state": "OPEN",
        "url": "https://github.com/owner/repo/issues/9",
        "body": "b", "labels": [{"name": "ai-pr-opened"}],
    }])
    found = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert found is not None
    issue, resumed = found
    assert issue["number"] == 9
    assert resumed["run_id"] == "a1b2c3d4"


def test_pick_in_progress_issue_skips_candidate_relabelled_blocked(
    monkeypatch, tmp_path,
):
    """The in-flight scan hands its candidate to the dispatch, so it
    skips only a candidate that left every claimable state — a relabel
    to `ai-blocked` inside the query's staleness window claims
    nothing."""
    monkeypatch.setattr(runner_seam, "slot_occupancy",
                        lambda *a, **k: [])
    _issue_list_fake(monkeypatch, [{
        "number": 18, "title": "task", "body": "b",
        "labels": [{"name": "ai-blocked"}],
    }])
    assert runner.pick_in_progress_issue(
        "owner/repo", tmp_path / "slots", 1,
    ) is None


def test_pick_issue_skips_candidate_that_left_the_queue(monkeypatch):
    """A stale index hands an opened-PR-labelled ticket to the ready
    scan: the classification names it NOT_CLAIMABLE here (the resumable
    scan owns that state) and the ready scan claims nothing."""
    _issue_list_fake(monkeypatch, [{
        "number": 55, "title": "free task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
        "labels": [{"name": "ai-pr-opened"}],
    }])
    assert runner.pick_issue("owner/repo") is None


def test_pick_issue_claims_a_fresh_candidate(monkeypatch):
    """The positive direction of the same guard: a queue-label ticket
    classifies FRESH_CLAIM and is picked."""
    _issue_list_fake(monkeypatch, [{
        "number": 56, "title": "task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
        "labels": [{"name": "ai-ready"}],
    }])
    assert runner.pick_issue("owner/repo") == {
        "number": 56, "title": "task", "body": "",
        "blockedBy": {"nodes": [], "totalCount": 0},
        "labels": [{"name": "ai-ready"}],
    }
