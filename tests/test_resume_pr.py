"""Resume the same PR from its opened-PR state (Issue #45, #82).

Unit tests for the runner's resume path: an Issue in an opened-PR state
(`ai-pr-opened` or `ai-fix-needed`) carries a run-scoped scene in its
`Muyan Pilot opened PR:` comment. The next tick recovers run_id, branch,
worktree and PR URL from that comment and resumes the delivery on the
ORIGINAL branch, worktree and PR. Issue #82 removed the cold-start
fixer: both states resume into the SAME independent review session,
which fixes findings in the same session. Failures mark the Issue
`ai-blocked` and preserve the PR, branch and worktree.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import muyan_pilot.runner as runner
from tests.test_progress_wiring import make_fake_gh


FAKE_RUN_ID = "a1b2c3d4"
FAKE_BRANCH = f"muyan-pilot/owner-repo-issue-9-{FAKE_RUN_ID}"
FAKE_WORKTREE = "/srv/repo/.worktrees/muyan-pilot-owner-repo-issue-9-a1b2c3d4"
FAKE_PR_URL = "https://github.com/owner/repo/pull/9"


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    """Each test starts without a bound run id."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)


def opened_pr_comment(run_id=FAKE_RUN_ID,
                      base_branch="main", base_sha="abc123def456",
                      pr_url=FAKE_PR_URL) -> str:
    # The runner is the only writer of this comment. It carries the run
    # scene (run_id, base, PR URL); branch and worktree are derived by
    # the runner from its own config and the run id, never parsed from a
    # comment (a public comment must not be able to name a local path).
    return (
        f"<!-- muyan-pilot:run={run_id} -->\n"
        f"Muyan Pilot opened PR: {pr_url} "
        f"(base_branch={base_branch} base_sha={base_sha} run_id={run_id})"
    )


def scene_for() -> dict:
    # The scene recovered from the trusted `Muyan Pilot opened PR:`
    # comment. Branch and worktree are NOT part of it: the runner
    # derives them from its own config, the Issue number and the run id.
    return {
        "run_id": FAKE_RUN_ID,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": FAKE_PR_URL,
    }


# ---------------------------------------------------------------- parse


def test_parse_pr_comment_returns_scene_for_opened_pr_comment():
    scene = runner.parse_pr_comment(opened_pr_comment())
    assert scene == {
        "run_id": FAKE_RUN_ID,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": FAKE_PR_URL,
    }


def test_parse_pr_comment_ignores_legacy_branch_and_worktree_fields():
    # Comments written before the scene was trimmed still parse; the
    # extra fields are simply not part of the recovered scene.
    body = opened_pr_comment().replace(
        ")", f" branch={FAKE_BRANCH} worktree={FAKE_WORKTREE})", 1,
    )
    scene = runner.parse_pr_comment(body)
    assert scene["run_id"] == FAKE_RUN_ID
    assert "branch" not in scene
    assert "worktree" not in scene


def test_parse_pr_comment_returns_none_for_started_comment():
    body = (
        f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->\n"
        f"Muyan Pilot started Pi: base_branch=main base_sha=abc123def456 "
        f"run_id={FAKE_RUN_ID} branch={FAKE_BRANCH} worktree={FAKE_WORKTREE}"
    )
    assert runner.parse_pr_comment(body) is None


def test_parse_pr_comment_returns_none_for_failed_comment():
    body = (
        f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->\n"
        f"Muyan Pilot failed: boom (base_branch=main base_sha=abc123def456 "
        f"run_id={FAKE_RUN_ID})"
    )
    assert runner.parse_pr_comment(body) is None


def test_parse_pr_comment_returns_none_for_empty_body():
    assert runner.parse_pr_comment("") is None


def test_parse_pr_comment_fails_fast_when_a_field_is_missing():
    needles = {
        "base_branch": "(base_branch=",
        "base_sha": " base_sha=",
        "run_id": " run_id=",
        "pr_url": FAKE_PR_URL,
    }
    for field, needle in needles.items():
        body = opened_pr_comment().replace(needle, "", 1)
        with pytest.raises(ValueError, match=f"missing {field}"):
            runner.parse_pr_comment(body)


def test_parse_pr_comment_fails_fast_on_invalid_run_id():
    body = opened_pr_comment(run_id="run1")
    with pytest.raises(ValueError, match="invalid run id"):
        runner.parse_pr_comment(body)


def test_parse_pr_comment_fails_fast_on_empty_field_value():
    body = opened_pr_comment(pr_url="")
    with pytest.raises(ValueError, match="missing pr_url"):
        runner.parse_pr_comment(body)


def comment(body: str, association: str | None = "OWNER") -> dict:
    if association is None:
        return {"body": body}
    return {"body": body, "authorAssociation": association}


def test_resume_scene_returns_latest_trusted_opened_pr_scene():
    comments = [
        comment(opened_pr_comment(base_sha="oldsha123456")),
        comment("unrelated human comment"),
        comment(opened_pr_comment(base_sha="newsha123456")),
    ]
    scene = runner.resume_scene(comments)
    assert scene["base_sha"] == "newsha123456"
    assert scene["run_id"] == FAKE_RUN_ID


def test_resume_scene_fails_fast_when_no_opened_pr_comment_exists():
    comments = [comment("no PR here")]
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.resume_scene(comments)


def test_resume_scene_skips_non_dict_comments():
    # The non-dict entry is hit first when scanning newest-first.
    comments = [
        comment(opened_pr_comment()),
        "not a dict",
    ]
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


# ------------------------------------------------- trusted comments (F1)


def test_resume_scene_ignores_public_comment_with_scene():
    """A public comment (authorAssociation=NONE) must never steer the
    runner into an arbitrary local worktree/branch/PR, even when it is
    the only and newest scene comment."""
    comments = [
        comment(opened_pr_comment(), association="NONE"),
    ]
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.resume_scene(comments)


def test_resume_scene_ignores_public_comment_even_when_newest():
    """The latest comment is public; the older trusted comment wins."""
    comments = [
        comment(opened_pr_comment(base_sha="trusted123456")),
        comment(opened_pr_comment(base_sha="attacker12345"),
                association="NONE"),
    ]
    scene = runner.resume_scene(comments)
    assert scene["base_sha"] == "trusted123456"


def test_resume_scene_ignores_comment_without_association():
    # A missing association is never trusted: only a positive trusted
    # value (OWNER/MAINTAINER/MEMBER/COLLABORATOR) passes.
    comments = [comment(opened_pr_comment(), association=None)]
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.resume_scene(comments)


@pytest.mark.parametrize("association", [
    "OWNER", "MAINTAINER", "MEMBER", "COLLABORATOR",
])
def test_resume_scene_accepts_every_trusted_association(association):
    comments = [comment(opened_pr_comment(), association=association)]
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


def test_resume_scene_skips_non_dict_trusted_comment():
    # A non-dict entry cannot carry an association, so it is skipped.
    comments = ["not a dict", comment(opened_pr_comment())]
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


def test_issue_comments_returns_comments_from_production_shape(monkeypatch):
    """Real `gh issue view --json comments` returns a top-level object
    with a `.comments` array (verified against the production CLI)."""
    payload = json.dumps({
        "comments": [
            {"body": "first", "authorAssociation": "NONE"},
            {"body": "second", "authorAssociation": "OWNER"},
        ],
    })
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: payload)
    comments = runner.issue_comments(9, repo="owner/repo")
    assert comments == [
        {"body": "first", "authorAssociation": "NONE"},
        {"body": "second", "authorAssociation": "OWNER"},
    ]


def test_issue_comments_rejects_top_level_array_payload(monkeypatch):
    monkeypatch.setattr(
        runner, "run_command",
        lambda *a, **k: json.dumps([{"body": "first"}]),
    )
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_comments(9, repo="owner/repo")


def test_issue_comments_rejects_payload_without_comments_array(monkeypatch):
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: "{}")
    with pytest.raises(ValueError, match="issue comments must be a JSON array"):
        runner.issue_comments(9, repo="owner/repo")



def test_parse_pr_comment_ignores_field_part_without_key():
    body = opened_pr_comment().replace(")", " =keyless)", 1)
    scene = runner.parse_pr_comment(body)
    assert scene["run_id"] == FAKE_RUN_ID


# ---------------------------------------------------------------- pick


def gh_comments_payload(comments: list[str],
                        association: str = "OWNER") -> str:
    # The production shape of `gh issue view --json comments`: a
    # top-level object with a `.comments` array; each comment carries
    # the author association of the viewer.
    return json.dumps({
        "comments": [
            {"body": body, "authorAssociation": association}
            for body in comments
        ],
    })


def issue_payload(state: str = "OPEN") -> str:
    return json.dumps([
        {"number": 9, "title": "ship", "state": state,
         "url": "https://github.com/owner/repo/issues/9"},
    ])


def make_pick_fake(list_payload: str, view_payload: str | None = None,
                   edits: list[list[str]] | None = None,
                   comments: list[str] | None = None):
    """Fake `gh` for the resumable scan; guard rejects anything else.

    `edits`/`comments` (when given) capture the label edits and comments
    posted by the scene-failure blocked transition.
    """
    def fake_run(command, **kwargs):
        if command[1] == "issue":
            if command[2] == "list":
                return list_payload
            if command[2] == "view":
                return view_payload
            if command[2] == "edit":
                if edits is not None:
                    edits.append(command)
                return ""
            if command[2] == "comment":
                if comments is not None:
                    comments.append(command[-1])
                return ""
        raise AssertionError(f"unexpected command: {command}")

    return fake_run


def test_pick_fake_rejects_unexpected_command(monkeypatch):
    fake = make_pick_fake("[]")
    monkeypatch.setattr(runner, "run_command", fake)
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["gh", "release", "list"])
    # An `issue` subcommand that is neither list/view/edit/comment is
    # rejected too.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake(["gh", "issue", "create", "--repo", "owner/repo"])


def test_pick_resumable_delivery_returns_newest_issue_with_scene(
    monkeypatch, tmp_path,
):
    calls = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["human note", opened_pr_comment()]),
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", counting)
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID
    assert scene["pr_url"] == FAKE_PR_URL
    # Newest-first list, then the full comment history of that Issue.
    # Both opened-PR states are scanned (Issue #70): `label:a,b` is
    # GitHub's OR within one label qualifier. `ai-in-progress` is NOT
    # excluded (Issue #178): a runner killed during review leaves the
    # backfilled in-flight label behind, and the same scan must pick
    # the delivery back up — the positive `label:ai-fix-needed,
    # ai-pr-opened` qualifier already restricts the scan to opened-PR
    # Issues (an implement-phase Issue has `ai-ready`+`ai-in-progress`
    # but neither opened-PR label, so it never matches).
    assert calls[0] == [
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search",
        "label:ai-fix-needed,ai-pr-opened "
        "-label:ai-blocked -label:ai-merged",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        "--json", "number,title,state,url,labels", "--limit", "1",
    ]
    assert calls[1] == [
        "gh", "issue", "view", "9", "--repo", "owner/repo",
        "--json", "comments",
    ]


def test_pick_resumable_delivery_scans_fix_needed_and_awaiting_review(
    monkeypatch, tmp_path,
):
    """Both opened-PR states are scanned (Issue #70): `ai-fix-needed`
    (a review finding or base conflict — Fixer work) and
    `ai-pr-opened` (awaiting review — a stranded delivery whose runner
    died, or the progress 404 that used to block the Issue before the
    review started). Blocked and merged Issues are excluded; a
    delivery that carries the backfilled `ai-in-progress` label is NOT
    excluded (Issue #178: a runner killed during review leaves it
    behind). A clean PR is still never sent to the Fixer: `main`
    routes an `ai-pr-opened` resume to the independent review (Issue
    #45 round-5 contract, tested in test_bootstrap_runner)."""
    calls = []

    def counting(command, **kwargs):
        calls.append(command)
        return "[]"

    monkeypatch.setattr(runner, "run_command", counting)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search",
        "label:ai-fix-needed,ai-pr-opened "
        "-label:ai-blocked -label:ai-merged",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        "--json", "number,title,state,url,labels", "--limit", "1",
    ]]


def test_pick_resumable_delivery_resumes_delivery_that_carries_in_progress_label(
    monkeypatch, tmp_path,
):
    """Issue #178: a runner killed DURING review leaves the backfilled
    `ai-in-progress` label behind on the opened-PR delivery (e.g.
    `ai-pr-opened` + `ai-in-progress`). The resumable scan must still
    find it and return the scene — otherwise the delivery is stranded
    (the in-progress scan excludes `ai-pr-opened`/`ai-fix-needed`, the
    ready scan excludes every delivery state)."""
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["human note", opened_pr_comment()]),
    )
    monkeypatch.setattr(runner, "run_command", fake)
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID
    assert scene["pr_url"] == FAKE_PR_URL


def test_pick_resumable_delivery_returns_none_when_queue_empty(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runner, "run_command", make_pick_fake("[]"))
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None


def test_pick_resumable_delivery_skips_when_another_runner_is_live(
    monkeypatch, tmp_path,
):
    """A slot held by ANOTHER process proves a live runner is working
    (Issue #39 slot semantics, Issue #70 review round 1): the
    `ai-pr-opened`/`ai-fix-needed` delivery is in flight, not stranded,
    so no second resume may start a second review Pi in the same
    worktree/branch/run. This runner's own slot (its own PID) does not
    block the scan."""
    gh_calls = []
    monkeypatch.setattr(
        runner, "run_command",
        lambda command, **kwargs: gh_calls.append(command) or "[]",
    )
    monkeypatch.setattr(runner, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, 4242)])
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert gh_calls == [], "no gh traffic while another runner is live"
    # Own PID: the scan still runs (this runner holds its own slot).
    monkeypatch.setattr(runner, "slot_occupancy",
                        lambda slot_dir, capacity: [(1, os.getpid())])
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert len(gh_calls) == 1


def test_pick_resumable_delivery_blocks_issue_without_scene_comment(
    monkeypatch, caplog, tmp_path,
):
    """An `ai-fix-needed` Issue whose comment history carries no trusted
    opened-PR comment at all cannot be resumed: blocked, not skipped
    (round-5 review, Major 2)."""
    edits: list[list[str]] = []
    comments: list[str] = []
    monkeypatch.setattr(
        runner, "run_command",
        make_pick_fake(
            issue_payload(),
            gh_comments_payload(["only a human comment here"]),
            edits=edits,
            comments=comments,
        ),
    )
    caplog.set_level("ERROR")
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Muyan Pilot failed:" in comments[0]
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_skips_closed_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "run_command",
        make_pick_fake(issue_payload(state="CLOSED")),
    )
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None


# ------------------------------------- malformed scene → ai-blocked (F2)

def test_pick_resumable_delivery_blocks_issue_when_scene_is_malformed(
    monkeypatch, caplog, tmp_path,
):
    """A trusted opened-PR comment with a missing/invalid scene field is
    an unresolvable recovery state: the Issue is marked `ai-blocked` with
    the concrete reason and the tick stops — it is never silently
    skipped while a fresh task starts ahead of it (round-5 review,
    Major 2)."""
    calls = []
    edits: list[list[str]] = []
    comments: list[str] = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["Muyan Pilot opened PR: "
                             "https://github.com/owner/repo/pull/9 "
                             "(base_branch=main base_sha=abc123def456)"
                             ]),
        edits=edits,
        comments=comments,
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", counting)
    caplog.set_level("ERROR")
    with pytest.raises(ValueError, match="missing run_id"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    # The blocked transition: add ai-blocked, remove ai-fix-needed...
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    # ...and a failure comment with the concrete reason...
    body = comments[0]
    assert "Muyan Pilot failed:" in body
    assert "missing run_id" in body
    # ...but no run marker: no valid run id exists, so none is guessed.
    assert "muyan-pilot:run=" not in body
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_blocks_issue_when_no_trusted_scene(
    monkeypatch, caplog, tmp_path,
):
    """An `ai-fix-needed` Issue whose comment history carries no trusted
    opened-PR comment cannot be resumed: blocked, not skipped."""
    calls = []
    edits: list[list[str]] = []
    comments: list[str] = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["only a human comment here"]),
        edits=edits,
        comments=comments,
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", counting)
    caplog.set_level("ERROR")
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Muyan Pilot failed:" in comments[0]
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_scene_failure_carries_marker_when_present(
    monkeypatch, tmp_path,
):
    """When the malformed comment still carries a valid run marker, the
    failure comment reuses it — the same run id, never a new one."""
    calls = []
    comments: list[str] = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload([
            f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->\n"
            "Muyan Pilot opened PR: "
            "https://github.com/owner/repo/pull/9 "
            "(base_branch=main base_sha=abc123def456)"
        ]),
        comments=comments,
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", counting)
    with pytest.raises(ValueError, match="missing run_id"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in comments[0]


def test_pick_resumable_delivery_scene_failure_skips_bodyless_comments(
    monkeypatch, tmp_path,
):
    """Trusted comments without a string body are skipped while looking
    for the run marker (never crash the recovery scan)."""
    comments: list[str] = []
    fake = make_pick_fake(
        issue_payload(),
        json.dumps({"comments": [
            {
                "body": (
                    f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->\n"
                    "Muyan Pilot opened PR: "
                    "https://github.com/owner/repo/pull/9 "
                    "(base_branch=main base_sha=abc123def456)"
                ),
                "authorAssociation": "OWNER",
            },
            {"authorAssociation": "OWNER"},
            {"body": None, "authorAssociation": "OWNER"},
        ]}),
        comments=comments,
    )
    monkeypatch.setattr(runner, "run_command", fake)
    with pytest.raises(ValueError, match="missing run_id"):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in comments[0]


def test_pick_resumable_delivery_scene_failure_preserves_error_when_reporting_fails(
    monkeypatch, caplog, tmp_path,
):
    """When the blocked transition itself cannot be reported, the
    original scene error is still re-raised (the tick still stops)."""

    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["only a human comment here"]),
    )

    def fake_run(command, **kwargs):
        if command[1] == "issue" and command[2] == "edit":
            raise RuntimeError("github edit failed")
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("ERROR"), pytest.raises(
        ValueError, match="no 'Muyan Pilot opened PR' comment",
    ):
        runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        )
    assert "failure reporting failed" in caplog.text
    # The fake's edit/comment branches are reachable without capture
    # lists too (edits=None / comments=None): they simply do not record.
    assert fake(["gh", "issue", "edit", "9", "--repo", "owner/repo"]) == ""
    assert fake([
        "gh", "issue", "comment", "9", "--repo", "owner/repo",
        "--body", "x",
    ]) == ""


def test_pick_next_delivery_stops_when_scene_is_malformed(
    monkeypatch, tmp_path,
):
    """A malformed scene re-raises: the tick stops and no fresh task
    starts ahead of the broken delivery (round-5 review, Major 2)."""
    def broken(repo, slot_dir, max_concurrency):
        raise ValueError("no 'Muyan Pilot opened PR' comment")

    calls = []
    monkeypatch.setattr(runner, "pick_resumable_delivery", broken)
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None: calls.append(("ready", repo)) or {"number": 10},
    )
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.pick_next_delivery(
            ["owner/repo"], tmp_path / "slots", 1,
        )
    # The ready queue was never consulted: no fresh claim started.
    assert calls == []


def test_pick_next_delivery_prefers_resumable_delivery_over_ready(
    monkeypatch, tmp_path,
):
    resumable = {"number": 9, "title": "ship"}
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: (
            calls.append(("resume", repo))
            or (resumable, {"run_id": FAKE_RUN_ID})
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    )
    assert result == ("owner/repo", resumable, {"run_id": FAKE_RUN_ID})
    assert calls == [("resume", "owner/repo")]


def test_pick_next_delivery_falls_back_to_ready_when_no_resumable(
    monkeypatch, tmp_path,
):
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: (
            calls.append(("resume", repo)) or None
        ),
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: (
            calls.append(("in_progress", repo)) or None
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    )
    assert result == ("owner/repo", ready, None)
    assert calls == [
        ("resume", "owner/repo"),
        ("in_progress", "owner/repo"),
        ("ready", "owner/repo"),
    ]


def test_pick_next_delivery_scans_sources_in_order(monkeypatch, tmp_path):
    resumable = {"number": 9, "title": "ship"}
    scene = {"run_id": FAKE_RUN_ID}
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: (
            calls.append(("resume", repo))
            or ((resumable, scene) if repo == "owner/second" else None)
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(
        ["owner/first", "owner/second"], tmp_path / "slots", 1,
    )
    assert result == ("owner/second", resumable, scene)
    assert calls == [
        ("resume", "owner/first"), ("resume", "owner/second"),
    ]


def test_pick_next_delivery_returns_none_when_nothing_to_do(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(
        runner, "pick_in_progress_issue",
        lambda repo, slot_dir, max_concurrency: None,
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None: None)
    assert runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    ) is None




# ---------------------------------------------------------------- run_pi




def test_run_pi_fresh_context_has_no_existing_pr(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        runner, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = {
        "prompt": prompt_path,
        "repo_dir": tmp_path,
        "source_repos": ["owner/repo"],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
        "base_branch": "main",
        "base_sha": "abc123def456",
        "run_id": FAKE_RUN_ID,
    }
    runner.run_pi(
        {"number": 9, "title": "t", "body": "b"}, tmp_path, config,
        "owner/repo", branch=FAKE_BRANCH,
    )
    context = calls[0][0][-1]
    assert "Existing PR:" not in context




# -------------------------------------------------------------------- main




def test_main_resumes_resumable_delivery_before_claiming_new(monkeypatch, tmp_path):
    """A resumable opened-PR delivery goes straight to the delivery wait
    (Issue #82: no cold-start fixer — the review session fixes findings
    in the same session); it is never re-claimed as a new task.
    Issue #89: the wait receives the URL that verify_pr VERIFIED, never
    the raw comment string (a comment must not steer the runner into
    the wrong PR, Issue #45)."""
    issue = {"number": 9, "title": "ship", "body": ""}
    scene = scene_for()
    verified_url = "https://github.com/owner/repo/pull/98"
    processed = []
    waits = []
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, scene
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or FAKE_PR_URL,
    )
    # The resume pre-validation (Issue #89) is stubbed: it returns a
    # verified URL that differs from the scene's comment string, so the
    # test proves the wait never sees the comment string.
    monkeypatch.setattr(
        runner, "verify_resumed_pr",
        lambda *a, **k: verified_url,
    )
    # The dispatch test must not run the real delivery-wait loop (it would
    # call `gh` against the real PR number of the verified URL).
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *a, **k: waits.append((a, k)) or None,
    )
    assert runner.main(["--config", str(config)]) == 0
    # The resumable delivery is resumed, not re-claimed as a new task.
    assert processed == []
    assert len(waits) == 1
    assert waits[0][0][:2] == (verified_url, issue)
    # The comment string itself never reached the wait.
    assert waits[0][0][0] != scene["pr_url"]
    # The resumed review runs under the scene's run id.
    assert runner.current_run_id() == FAKE_RUN_ID


def test_main_still_claims_new_issue_when_no_resumable(monkeypatch, tmp_path):
    issue = {"number": 10, "title": "new", "body": ""}
    processed = []
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or FAKE_PR_URL,
    )
    # The dispatch test must not run the real delivery-wait loop (it would
    # call `gh` against the real PR number of FAKE_PR_URL).
    monkeypatch.setattr(runner, "wait_for_delivery", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert len(processed) == 1
    assert processed[0][0] is issue
    assert processed[0][2] == "owner/repo"


# --------------------------- resume PR verification (Issue #89)


def make_resume_config(tmp_path) -> dict:
    return {
        "repo_dir": tmp_path,
        "base_branch": "main",
    }


def make_resume_scene(pr_url: str = FAKE_PR_URL) -> dict:
    return {
        "run_id": FAKE_RUN_ID,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": pr_url,
    }


def make_resume_issue() -> dict:
    return {"number": 9, "title": "ship", "body": ""}


def expected_resume_worktree(tmp_path) -> Path:
    return runner.worktree_path(
        tmp_path, "owner/repo", 9, FAKE_RUN_ID,
    )


def test_verify_resumed_pr_verifies_scene_pr_and_returns_verified_url(
    monkeypatch, tmp_path,
):
    """Issue #89: the resume verifies the open PR BEFORE any git/Pi
    mutation: exactly one open PR of the DERIVED branch (derived from
    the configured repo_dir, source repo, Issue number and run id —
    never read from the comment), in the configured source repo, on the
    configured base, carrying the run marker and the `Fixes` keyword,
    with the EXACT URL of the recovered scene. The latest-base check is
    skipped (`require_latest_base=False`): being behind the base is the
    expected state the review session absorbs in-session (Issue #82),
    so the base merge never returns to the runner. The returned URL is
    the one verify_pr verified, never the comment string."""
    calls = []
    verified_url = "https://github.com/owner/repo/pull/9"

    def fake_verify_pr(worktree, branch, base_branch, run_id, *, issue,
                       repo_dir=None, pr_repo=None, expected_url=None,
                       require_latest_base=True):
        calls.append({
            "worktree": worktree, "branch": branch,
            "base_branch": base_branch, "run_id": run_id, "issue": issue,
            "repo_dir": repo_dir,
            "pr_repo": pr_repo, "expected_url": expected_url,
            "require_latest_base": require_latest_base,
        })
        return verified_url

    edits = []

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "edit_issue", fake_edit)
    # The derived worktree exists (a real delivery always has one).
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    url = runner.verify_resumed_pr(
        make_resume_scene(), make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    # The verified URL is returned, not the comment string.
    assert url == verified_url
    assert len(calls) == 1
    # Issue #178: the in-flight label is backfilled before the resumed
    # delivery continues (idempotent, the state label untouched).
    assert edits == [(9, "owner/repo", "ai-in-progress", None)]
    call = calls[0]
    # Branch and worktree are DERIVED from config + Issue + run id.
    assert call["worktree"] == expected_resume_worktree(tmp_path)
    assert call["branch"] == FAKE_BRANCH
    # The configured base is verified (the scene base equals it here).
    assert call["base_branch"] == "main"
    assert call["run_id"] == FAKE_RUN_ID
    assert call["issue"] == 9
    # The resume pre-validation contract (Issue #45, pre-#82
    # resume_delivery): head repo, exact URL, no latest-base check.
    assert call["pr_repo"] == "owner/repo"
    assert call["expected_url"] == FAKE_PR_URL
    assert call["require_latest_base"] is False
    # Issue #171: the verify fetch's lock location is the configured
    # deployment checkout (the shared state dir), never the worktree.
    assert call["repo_dir"] == tmp_path



def make_resume_failure_fake(monkeypatch, *, progress_comments=None,
                             labels=None):
    """Shared `run_command` fake of the resume verification failure
    tests (Issue #89).

    Answers exactly the failure-scene reporting: the progress API
    (GET comment list / POST create / PATCH update), the Issue label
    read (the leftover `ai-fix-needed` check), the comment-history
    read (the completed review rounds — no round comments exist yet)
    and the PR failure comment (Issue #50: the failure comment is
    written to the Issue AND the PR). Anything else is rejected.
    `progress_comments` is the GET payload (an existing tracked
    progress comment makes the blocked/fix-needed scene PATCH it in
    place; empty makes it POST a new one) and `labels` the label
    read. Captures the gh api calls and the label edits / failure
    comments; monkeypatches `edit_issue` / `comment_issue`
    accordingly. Returns `(captured, fake_run)` where `captured` is
    `{"api": [...], "edits": [...], "comments": [...],
    "pr_comments": [...]}`.
    """
    if progress_comments is None:
        progress_comments = []
    if labels is None:
        labels = ["ai-pr-opened"]
    captured = {"api": [], "edits": [], "comments": [], "pr_comments": []}

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            captured["api"].append(command)
            if "--method" not in command:
                return json.dumps(progress_comments)
            method = command[command.index("--method") + 1]
            if method == "POST":
                body = command[command.index("--field") + 1]
                return json.dumps({"id": 78, "body": body[len("body="):],
                                   "url": "https://x/78"})
            return ""
        if command[:3] == ["gh", "pr", "comment"]:
            # Issue #50: the failure comment is written to the PR too.
            captured["pr_comments"].append(command[command.index("--body") + 1])
            return ""
        if command[-1] == "labels":
            return json.dumps({
                "labels": [{"name": name} for name in labels],
            })
        if command[-1] == "comments":
            return json.dumps({"comments": []})
        raise AssertionError(f"unexpected command: {command}")

    def fake_edit(*args, **kwargs):
        captured["edits"].append((args, kwargs))

    def fake_comment(*args, **kwargs):
        captured["comments"].append((args, kwargs))

    monkeypatch.setattr(runner, "edit_issue", fake_edit)
    monkeypatch.setattr(runner, "comment_issue", fake_comment)
    monkeypatch.setattr(runner, "run_command", fake_run)
    return captured, fake_run


def test_resume_failure_fake_answers_blocked_scene_and_rejects_other(
    monkeypatch,
):
    """Every branch of the shared fake is exercised (the repo's
    fake-coverage convention): the GET/POST/PATCH answers, the
    labels/comments reads and the rejection of anything else."""
    existing = {
        "id": 77,
        "body": (
            f"{run_marker_body()}\n\n"
            "**Muyan Pilot progress**\n\nawaiting review"
        ),
    }
    captured, fake_run = make_resume_failure_fake(
        monkeypatch, progress_comments=[existing],
        labels=["ai-pr-opened", "ai-fix-needed"],
    )
    # GET: the tracked progress comment exists (the blocked scene
    # PATCHes it in place instead of POSTing a second comment).
    assert json.loads(fake_run([
        "gh", "api", "repos/owner/repo/issues/9/comments",
    ])) == [existing]
    # POST: a new comment (milestone, blocked scene without a tracked
    # comment) answers with the full comment object.
    posted = json.loads(fake_run([
        "gh", "api", "repos/owner/repo/issues/9/comments",
        "--method", "POST", "--field", "body=x",
    ]))
    assert posted == {"id": 78, "body": "x", "url": "https://x/78"}
    # PATCH: the update route answers empty.
    assert fake_run([
        "gh", "api", "repos/owner/repo/issues/comments/77",
        "--method", "PATCH", "--field", "body=x",
    ]) == ""
    # The label read carries the served labels ...
    labels = json.loads(fake_run([
        "gh", "issue", "view", "9", "--repo", "owner/repo",
        "--json", "labels",
    ]))
    assert [item["name"] for item in labels["labels"]] == [
        "ai-pr-opened", "ai-fix-needed",
    ]
    # ... and the comment-history read has no review rounds yet.
    comments = json.loads(fake_run([
        "gh", "issue", "view", "9", "--repo", "owner/repo",
        "--json", "comments",
    ]))
    assert comments == {"comments": []}
    # Anything else is rejected (no git, no other gh route).
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["git", "status"])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    # The captures hold the api calls (the edits/comments captures are
    # filled by the monkeypatched writers, not by the fake itself).
    assert len(captured["api"]) == 3
    assert captured["edits"] == []
    assert captured["comments"] == []


def test_verify_resumed_pr_backfills_in_progress_label_before_continuing(
    monkeypatch, tmp_path,
):
    """Issue #178: the resumed delivery is in flight from the verified
    PR on — the Runner holds the slot and continues the review/merge
    work — so the Issue must carry `ai-in-progress` BEFORE the work
    continues. The backfill is an idempotent label projection repair:
    the run, worktree and PR are the ones verify_pr verified — nothing
    is recreated, and the opened-PR state label is untouched (the
    transitions into `ai-pr-opened`/`ai-fix-needed`/`ai-merged`/
    `ai-blocked` keep their existing add/remove pairs)."""
    calls = []
    edits = []

    def fake_verify_pr(worktree, branch, base_branch, run_id, *, issue,
                       repo_dir=None, pr_repo=None, expected_url=None,
                       require_latest_base=True):
        calls.append(1)
        return FAKE_PR_URL

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "edit_issue", fake_edit)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    url = runner.verify_resumed_pr(
        make_resume_scene(), make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    assert url == FAKE_PR_URL
    assert len(calls) == 1
    # The in-flight label is backfilled after the PR is verified, with
    # the state label (ai-pr-opened / ai-fix-needed) untouched.
    assert edits == [(9, "owner/repo", "ai-in-progress", None)]


def test_verify_resumed_pr_repeated_resume_backfill_is_idempotent(
    monkeypatch, tmp_path,
):
    """Issue #178: a repeated resume (two ticks, same scene) re-adds
    the SAME single label edit each time and repairs the label
    projection only — no new run id, no new worktree, no new PR. The
    run, branch and worktree of the scene are the ones that continue."""
    edits = []
    verify_calls = []

    def fake_verify_pr(worktree, branch, base_branch, run_id, *, issue,
                       repo_dir=None, pr_repo=None, expected_url=None,
                       require_latest_base=True):
        verify_calls.append((worktree, branch, run_id))
        return FAKE_PR_URL

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "edit_issue", fake_edit)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    # Two consecutive ticks resume the same scene.
    for _ in range(2):
        url = runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
        assert url == FAKE_PR_URL
    # Exactly one idempotent backfill edit per tick, nothing else.
    assert edits == [
        (9, "owner/repo", "ai-in-progress", None),
        (9, "owner/repo", "ai-in-progress", None),
    ]
    # Nothing was recreated: both ticks verified the SAME derived
    # worktree, branch and run id of the scene — no new run, no new
    # worktree, no new PR.
    assert verify_calls == [
        (expected_resume_worktree(tmp_path), FAKE_BRANCH, FAKE_RUN_ID),
        (expected_resume_worktree(tmp_path), FAKE_BRANCH, FAKE_RUN_ID),
    ]


def test_verify_resumed_pr_backfill_label_api_failure_fails_fast(
    monkeypatch, tmp_path, caplog,
):
    """Issue #178: a label API failure during the resume backfill is a
    RECOVERABLE resume failure (never `ai-blocked`): the Issue is
    marked `ai-fix-needed`, the run-marked failure comment is posted to
    the Issue AND the PR, the error is re-raised so the tick stops, and
    the journal carries the concrete failure (fail fast, never
    swallowed). No review Pi is started and nothing is merged."""
    captured, _ = make_resume_failure_fake(monkeypatch)

    def failing_edit(number, *, repo, add=None, remove=None):
        if add == "ai-in-progress":
            raise subprocess.CalledProcessError(
                1, ["gh", "issue", "edit", str(number), "--repo", repo,
                    "--add-label", add],
                output="gh: HTTP 429: rate limited",
                stderr="gh: HTTP 429: rate limited",
            )
        captured["edits"].append(
            ((number,), {"repo": repo, "add": add, "remove": remove}),
        )

    monkeypatch.setattr(runner, "verify_pr", lambda *a, **kw: FAKE_PR_URL)
    monkeypatch.setattr(runner, "edit_issue", failing_edit)
    reviews: list = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The label API failure itself is re-raised (tick stops) ...
    assert "ai-in-progress" in str(excinfo.value)
    assert "rate limited" in (excinfo.value.stderr or "")
    # No review Pi was started and nothing was merged.
    assert reviews == []
    # The recoverable transition happened (ai-pr-opened removed) — the
    # failed backfill edit itself is not a recorded edit, it raised.
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    # The run-marked failure comment names the label API failure ...
    assert len(captured["comments"]) == 1
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot needs a fix:" in body
    assert run_marker_body() in body
    assert "rate limited" in body
    # ... written to the Issue AND the PR (Issue #50) ...
    assert len(captured["pr_comments"]) == 1
    assert captured["pr_comments"][0] == body
    # ... and the fix-needed milestone.
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Muyan Pilot: fix needed" in body for body in posted)
    assert any("rate limited" in body for body in posted)
    assert "resume_pr_verification_failed" in caplog.text


def test_verify_resumed_pr_pr_url_mismatch_stays_fix_needed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #89 + #50: the comment URL does not match the open PR of
    the task branch -> fail fast, but the failure is RECOVERABLE: the
    Issue is marked `ai-fix-needed` (never `ai-blocked`) with a
    run-marked failure comment that names the reason, and the error is
    re-raised so the tick stops — no review Pi is started, the wrong
    PR is never merged; the next tick resumes the same run, branch,
    worktree and PR. (The pre-#82 `pr_url_mismatch` resume test,
    restored.)"""
    existing = {
        "id": 77,
        "body": (
            f"{run_marker_body()}\n\n"
            "**Muyan Pilot progress**\n\nawaiting review"
        ),
    }
    captured, _ = make_resume_failure_fake(
        monkeypatch, progress_comments=[existing],
    )

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError(
            "PR URL https://github.com/owner/repo/pull/87 is not the "
            "recovered original PR "
            "https://github.com/owner/repo/pull/99; the resume must "
            "keep the same PR number"
        )

    reviews: list = []
    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="not the recovered original PR"):
        runner.verify_resumed_pr(
            make_resume_scene(pr_url="https://github.com/owner/repo/pull/99"),
            make_resume_issue(), make_resume_config(tmp_path), "owner/repo",
        )
    # No review Pi was started and nothing was merged.
    assert reviews == []
    # The Issue is marked ai-fix-needed (ai-pr-opened removed) — never
    # ai-blocked (Issue #50: the next tick resumes the same PR) ...
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    # ... with a run-marked failure comment that names the reason ...
    assert len(captured["comments"]) == 1
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot needs a fix:" in body
    assert run_marker_body() in body
    assert "not the recovered original PR" in body
    # ... written to the Issue AND the PR (Issue #50) ...
    assert len(captured["pr_comments"]) == 1
    assert captured["pr_comments"][0] == body
    # ... and the fix-needed milestone.
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Muyan Pilot: fix needed" in body for body in posted)
    assert any("not the recovered original PR" in body for body in posted)
    # The tracked progress comment becomes the fix-needed scene in
    # place.
    patches = [
        command for command in captured["api"]
        if command[:2] == ["gh", "api"]
        and command[2] == "repos/owner/repo/issues/comments/77"
        and "PATCH" in command
    ]
    assert patches, "the tracked progress comment was not updated"
    fix_needed = patches[-1][patches[-1].index("--field") + 1][len("body="):]
    assert "Muyan Pilot fix needed" in fix_needed
    assert "next step:" in fix_needed
    assert "resume_pr_verification_failed" in caplog.text


def test_verify_resumed_pr_pr_repo_mismatch_stays_fix_needed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #89 + #50: the PR head repo is not the configured source
    repo -> the same fail-fast, but RECOVERABLE: `ai-fix-needed` (the
    next tick resumes the same PR), never `ai-blocked` (the pre-#82
    `pr_repo_mismatch` resume test, restored)."""
    captured, _ = make_resume_failure_fake(monkeypatch)

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError(
            "PR head repo is fork/repo, expected owner/repo; the resume "
            "must keep the PR of the configured source repo"
        )

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="PR head repo is fork/repo"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot needs a fix:" in body
    assert run_marker_body() in body
    assert "fork/repo" in body
    assert len(captured["pr_comments"]) == 1
    assert "resume_pr_verification_failed" in caplog.text


def test_verify_resumed_pr_recoverable_failure_keeps_fix_needed_label(
    monkeypatch, tmp_path,
):
    """Issue #50: a RECOVERABLE resume failure while the Issue awaits
    the next review session (`ai-fix-needed`) keeps the `ai-fix-needed`
    label (the opened-PR state label is removed, the fix-needed label
    is the one the next tick scans for — Issue #82 routes both
    opened-PR states into the same resume)."""
    captured, _ = make_resume_failure_fake(
        monkeypatch, labels=["ai-fix-needed"],
    )

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError("expected exactly one open PR for the task branch")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The single transition: ai-pr-opened removed, ai-fix-needed added
    # (the label the Issue already carries is re-added idempotently —
    # the next tick's scan needs it).
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]


def test_verify_resumed_pr_fails_fast_when_scene_base_differs(
    monkeypatch, tmp_path, caplog,
):
    """Issue #91 + #50 (pre-verify): the scene freezes the base the PR
    was opened against; when the configured base differs, the resume
    fails fast BEFORE any git/gh command (no verify_pr, no fetch) and
    the Issue is marked ai-blocked with both base values named — a
    base-branch change is a human decision (an explicit
    UnrecoverableDeliveryError), never ai-fix-needed (auto-retrying
    would keep failing on the same mismatch)."""
    commands: list = []
    captured, fake_run = make_resume_failure_fake(monkeypatch)

    def counting(command, **kwargs):
        commands.append(command)
        return fake_run(command, **kwargs)

    def fake_verify_pr(*args, **kwargs):
        # Must never run: the base mismatch is terminal before it.
        raise AssertionError("verify_pr must not run on a base mismatch")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "run_command", counting)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    scene = make_resume_scene()
    scene["base_branch"] = "develop"
    with pytest.raises(
        runner.UnrecoverableDeliveryError, match="differs from configured",
    ):
        runner.verify_resumed_pr(
            scene, make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # No git command ran before the terminal transition (the only gh
    # traffic is the blocked-scene reporting: the progress API, the
    # label read, the comment-history read of the review rounds and
    # the PR failure comment of Issue #50).
    assert all(command[0] != "git" for command in commands)
    assert all(
        command[:2] == ["gh", "api"]
        or command[:3] == ["gh", "pr", "comment"]
        or command[-1] in ("comments", "labels")
        for command in commands
    )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-blocked",
                "remove": "ai-pr-opened"}),
    ]
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot failed:" in body
    assert run_marker_body() in body
    assert "base_branch=develop" in body
    assert "base_branch=main" in body
    # Issue #50: the blocked comment states why automatic recovery is
    # impossible.
    assert "cannot be recovered automatically" in body
    assert "resume_pr_verification_failed" in caplog.text
    # The fake proves the contract when called directly: verify_pr
    # must never run on a base mismatch.
    with pytest.raises(
        AssertionError, match="must not run on a base mismatch",
    ):
        fake_verify_pr()


def test_verify_resumed_pr_worktree_missing_stays_fix_needed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #90 + #50 (pre-verify): the worktree is derived from the
    configured repo_dir, source repo, Issue number and run id (never
    read from a comment); a missing directory is a RECOVERABLE failure
    (the branch still exists on the remote and the worktree can be
    recreated on the next resume): fail fast BEFORE any git/gh command
    and mark the Issue ai-fix-needed (never ai-blocked) with the PR
    and branch preserved."""
    commands: list = []
    captured, fake_run = make_resume_failure_fake(monkeypatch)

    def counting(command, **kwargs):
        commands.append(command)
        return fake_run(command, **kwargs)

    def fake_verify_pr(*args, **kwargs):
        raise AssertionError("verify_pr must not run on a missing worktree")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(runner, "run_command", counting)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    # The derived worktree does not exist under tmp_path.
    assert not expected_resume_worktree(tmp_path).is_dir()
    with pytest.raises(RuntimeError, match="worktree missing"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # No git command ran against the missing worktree (the only gh
    # traffic is the fix-needed-scene reporting).
    assert all(command[0] != "git" for command in commands)
    assert all(
        command[:2] == ["gh", "api"]
        or command[:3] == ["gh", "pr", "comment"]
        or command[-1] in ("comments", "labels")
        for command in commands
    )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot needs a fix:" in body
    assert run_marker_body() in body
    assert str(expected_resume_worktree(tmp_path)) in body
    # The PR and branch are preserved in the failure comment (the
    # pre-#82 resume_delivery fail-fast scene) ... the failure comment
    # is written to the Issue AND the PR (Issue #50) ...
    assert FAKE_PR_URL in body
    assert FAKE_BRANCH in body
    assert len(captured["pr_comments"]) == 1
    # ... and the fix-needed milestone (not the blocked one).
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Muyan Pilot: fix needed" in body for body in posted)
    assert not any("Muyan Pilot: blocked" in body for body in posted)
    assert "resume_pr_verification_failed" in caplog.text
    # The fake proves the contract when called directly: verify_pr
    # must never run on a missing worktree.
    with pytest.raises(
        AssertionError, match="must not run on a missing worktree",
    ):
        fake_verify_pr()


def test_verify_resumed_pr_reraises_when_failure_reporting_fails(
    monkeypatch, caplog, tmp_path,
):
    """When the blocked transition itself cannot be reported, the
    original verification error is still re-raised (the tick still
    stops)."""

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError("the original verification failure")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    make_resume_failure_fake(monkeypatch)

    def broken_edit(*args, **kwargs):
        raise RuntimeError("github edit failed")

    monkeypatch.setattr(runner, "edit_issue", broken_edit)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="the original verification failure",
    ):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert "failure reporting failed" in caplog.text


def test_verify_resumed_pr_without_bound_run_id_still_fix_needed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #50: a RECOVERABLE resume verification failure with no
    bound run id (the caller must bind the scene's run id first; this
    is the defensive branch) still marks the Issue ai-fix-needed: the
    failure comment simply carries no run marker, and the milestone /
    progress scene are skipped (no run id to bind them to)."""
    captured, _ = make_resume_failure_fake(monkeypatch)

    def fake_verify_pr(*args, **kwargs):
        raise RuntimeError("the verification failure")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    # The autouse fixture resets the run id to None; do not re-bind it.
    assert runner.current_run_id() is None
    caplog.set_level("INFO")
    with pytest.raises(RuntimeError, match="the verification failure"):
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed",
                "remove": "ai-pr-opened"}),
    ]
    body = captured["comments"][0][1]["body"]
    assert "Muyan Pilot needs a fix:" in body
    # No run id: no marker, no milestone, no fix-needed progress scene
    # — the only gh traffic is the label read of the leftover-label
    # check and the PR failure comment (no progress API at all).
    assert run_marker_body() not in body
    assert captured["api"] == []
    assert len(captured["pr_comments"]) == 1
    assert "resume_pr_verification_failed" in caplog.text


def run_marker_body() -> str:
    return f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->"
