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

from orbi import delivery_labels, delivery_scene
from orbi import runner, scene
from orbi.delivery_scene import (
    EXTERNAL_PR_MARKER,
    DeliveryScene,
    RunContext,
    body_markers,
    classify,
)
from tests.seam import seam as runner_seam
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
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
        ({AWAITING_MERGE_LABEL}, TRUSTED_SCENE, None, frozenset(),
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
        # Issue #1216: an open PR is enough to recover a fix round when
        # the trusted scene comment was lost.
        ({FIX_NEEDED_LABEL}, None, "OPEN", frozenset(),
         DeliveryScene.FRESH_CLAIM),
        ({FIX_NEEDED_LABEL}, None, "CLOSED", frozenset(),
         DeliveryScene.NOT_CLAIMABLE),
        (
            {FIX_NEEDED_LABEL, BLOCKED_LABEL}, None, "OPEN", frozenset(),
            DeliveryScene.BLOCKED,
        ),
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
        body_markers=frozenset(markers),
    ) is expected


def test_awaiting_merge_then_fix_needed_remains_claimable_with_open_pr():
    """Issue #1216: losing the queue label while awaiting a merge must
    not strand the subsequent fix-needed delivery when its PR is open."""
    labels = {READY_LABEL}
    for event in (
        delivery_labels.EVENT_AWAITING_MERGE,
        delivery_labels.EVENT_FIX_NEEDED,
    ):
        to_add, to_remove = delivery_labels.label_patch(event, labels)
        labels.update(to_add)
        labels.difference_update(to_remove)

    assert labels == {FIX_NEEDED_LABEL}
    assert classify(
        labels, None, "OPEN", body_markers=frozenset(),
    ) is DeliveryScene.FRESH_CLAIM


def _scene_less_fix_issue():
    return {
        "number": 1216, "title": "continue the open PR", "state": "OPEN",
        "url": "https://github.com/owner/repo/issues/1216", "body": "",
        "labels": [{"name": FIX_NEEDED_LABEL}],
    }


def test_pick_resumable_routes_scene_less_fix_with_open_pr_to_fresh_claim(
    monkeypatch, tmp_path,
):
    """The production scan gathers the PR fact before deciding #1216."""
    issue = _scene_less_fix_issue()
    monkeypatch.setitem(runner.__dict__, "slot_held_deliveries", lambda *_: set())
    monkeypatch.setitem(runner.__dict__, "list_issues", lambda *a, **k: [issue])
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: [])
    monkeypatch.setitem(runner.__dict__, "_route_external_pr_ticket", lambda *a: False)
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *a: {
        "number": 1224, "url": "https://github.com/owner/repo/pull/1224",
    })

    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    ) == (issue, None)


def test_pick_resumable_scene_less_fix_respects_blocked_precedence(
    monkeypatch, tmp_path,
):
    """A stale scan result can never reclaim an already blocked ticket."""
    issue = _scene_less_fix_issue()
    issue["labels"].append({"name": BLOCKED_LABEL})
    events = []
    monkeypatch.setitem(runner.__dict__, "slot_held_deliveries", lambda *_: set())
    monkeypatch.setitem(runner.__dict__, "list_issues", lambda *a, **k: [issue])
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: [])
    monkeypatch.setitem(runner.__dict__, "_route_external_pr_ticket", lambda *a: False)
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *a: {
        "number": 1224, "url": "https://github.com/owner/repo/pull/1224",
    })
    monkeypatch.setitem(
        runner.__dict__, "event",
        lambda name, **fields: events.append((name, fields)),
    )

    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    ) is None
    assert events == [(
        "claim_yield", {"issue": 1216, "reason": "scene_blocked"},
    )]


def test_pick_resumable_scene_less_fix_without_open_pr_stays_unclaimable(
    monkeypatch, tmp_path,
):
    """A closed/absent PR never enters the fresh-claim takeover."""
    issue = _scene_less_fix_issue()
    blocked = []
    monkeypatch.setitem(runner.__dict__, "slot_held_deliveries", lambda *_: set())
    monkeypatch.setitem(runner.__dict__, "list_issues", lambda *a, **k: [issue])
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: [])
    monkeypatch.setitem(runner.__dict__, "_route_external_pr_ticket", lambda *a: False)
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *a: None)
    monkeypatch.setitem(runner.__dict__, "_recover_missing_pr_scene", lambda *a: None)
    monkeypatch.setitem(runner.__dict__, "_has_recoverable_pr_scene", lambda *a: False)
    monkeypatch.setitem(
        runner.__dict__, "apply_label_patch",
        lambda number, **kwargs: blocked.append(number),
    )
    monkeypatch.setitem(runner.__dict__, "comment_issue", lambda *a, **k: None)

    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    ) is None
    assert blocked == [1216]


def test_pick_resumable_scene_less_fix_rejects_ambiguous_open_prs(
    monkeypatch, tmp_path,
):
    """The sole-open-PR contract fails fast instead of choosing a PR."""
    issue = _scene_less_fix_issue()
    monkeypatch.setitem(runner.__dict__, "slot_held_deliveries", lambda *_: set())
    monkeypatch.setitem(runner.__dict__, "list_issues", lambda *a, **k: [issue])
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *a, **k: [])
    monkeypatch.setitem(runner.__dict__, "_route_external_pr_ticket", lambda *a: False)
    monkeypatch.setitem(
        runner.__dict__, "open_pr_for_branch",
        lambda *a: (_ for _ in ()).throw(RuntimeError("multiple open PRs")),
    )

    with pytest.raises(RuntimeError, match="multiple open PRs"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1, tmp_path,
        )


def test_gather_claim_facts_probes_scene_less_fix_needed_takeover(
    monkeypatch, tmp_path,
):
    """The selected fresh claim reaches dispatch with the same OPEN fact."""
    issue = _scene_less_fix_issue()
    pr = {
        "number": 1224, "url": "https://github.com/owner/repo/pull/1224",
        "headRefName": "orbi/owner-repo-issue-1216",
    }
    monkeypatch.setitem(runner.__dict__, "has_in_progress_label", lambda *a: False)
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *a: pr)
    monkeypatch.setitem(runner.__dict__, "stable_branch_exists", lambda *a: True)

    facts = runner._gather_claim_facts(
        issue, runner.RunnerConfig(repo_dir=tmp_path), "owner/repo", None,
    )

    assert facts.takeover_pr == pr
    assert facts.pr_state == "OPEN"
    assert facts.classify_scene() is DeliveryScene.FRESH_CLAIM


def test_scene_less_fix_yields_when_pr_closes_between_scan_and_dispatch(
    monkeypatch, tmp_path,
):
    """The open-PR race cannot turn into a fresh implementation run."""
    events = []
    monkeypatch.setitem(
        runner.__dict__, "event",
        lambda name, **fields: events.append((name, fields)),
    )
    facts = delivery_scene.DeliveryFacts(
        labels=frozenset({FIX_NEEDED_LABEL}),
        config=runner.RunnerConfig(repo_dir=tmp_path),
        run_id="a1b2c3d4", base_branch="main",
        claim_labels=frozenset({FIX_NEEDED_LABEL}),
        stable_branch="orbi/owner-repo-issue-1216",
    )

    result = runner._dispatch_implementation(
        _scene_less_fix_issue(), "owner/repo", facts, ops=False,
    )

    assert result == runner.IssueResult("claim-yielded", None)
    assert events == [(
        "claim_yield",
        {"issue": 1216, "reason": "scene_less_fix_pr_not_open"},
    )]


def test_classify_custom_dispatch_label():
    """The repository's claim label (Issue #527) is the queue entry:
    a custom-label ticket is fresh-claimable only under its own label."""
    assert classify(
        {"my-queue"}, None, None,
        body_markers=frozenset(), ready_label="my-queue",
    ) is DeliveryScene.FRESH_CLAIM
    assert classify(
        {"my-queue", IN_PROGRESS_LABEL}, None, None,
        body_markers=frozenset(), ready_label="my-queue",
    ) is DeliveryScene.RESTART_IN_FLIGHT
    assert classify(
        {"my-queue"}, None, "OPEN",
        body_markers=frozenset(MARKER), ready_label="my-queue",
    ) is DeliveryScene.EXTERNAL_TAKEOVER


def test_classify_human_review_hold():
    """The human acceptance gate (Issue #763): a held opened-PR delivery
    waits while no human has confirmed (`ai-human-review` absent); the
    label's presence resumes the review, and no hold is a plain resume."""
    hold_kwargs = {"human_review_hold": True}
    assert classify(
        {PR_OPENED_LABEL}, TRUSTED_SCENE, None,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.HUMAN_REVIEW_WAIT
    assert classify(
        {FIX_NEEDED_LABEL}, TRUSTED_SCENE, None,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.HUMAN_REVIEW_WAIT
    assert classify(
        {PR_OPENED_LABEL, "ai-human-review"}, TRUSTED_SCENE,
        None,
        body_markers=frozenset(), **hold_kwargs,
    ) is DeliveryScene.RESUME_REVIEW
    assert classify(
        {PR_OPENED_LABEL}, TRUSTED_SCENE, None,
        body_markers=frozenset(),
    ) is DeliveryScene.RESUME_REVIEW



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
    context = RunContext(
        run_id="a1b2c3d4", issue=787,
        branch="orbi/o-r-issue-787",
        worktree="/wt/orbi-o-r-issue-787-a1b2c3d4",
        source_repo="o/r",
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
