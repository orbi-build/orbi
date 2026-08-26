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

import bootstrap_runner as runner
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
    # GitHub's OR within one label qualifier.
    assert calls[0] == [
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search",
        "label:ai-fix-needed,ai-pr-opened "
        "-label:ai-blocked -label:ai-merged -label:ai-in-progress",
        "--json", "number,title,state,url", "--limit", "1",
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
    review started). Blocked, merged and in-flight Issues are excluded.
    A clean PR is still never sent to the Fixer: `main` routes an
    `ai-pr-opened` resume to the independent review (Issue #45
    round-5 contract, tested in test_bootstrap_runner)."""
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
        "-label:ai-blocked -label:ai-merged -label:ai-in-progress",
        "--json", "number,title,state,url", "--limit", "1",
    ]]


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
        lambda repo: calls.append(("ready", repo)) or {"number": 10},
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
        lambda repo: calls.append(("ready", repo)) or ready,
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
        lambda repo: calls.append(("ready", repo)) or ready,
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
        lambda repo: calls.append(("ready", repo)) or ready,
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
    monkeypatch.setattr(runner, "pick_issue", lambda repo: None)
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
    in the same session); it is never re-claimed as a new task."""
    issue = {"number": 9, "title": "ship", "body": ""}
    scene = scene_for()
    processed = []
    waits = []
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency: (
            "owner/repo", issue, scene
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or FAKE_PR_URL,
    )
    # The dispatch test must not run the real delivery-wait loop (it would
    # call `gh` against the real PR number of FAKE_PR_URL).
    monkeypatch.setattr(
        runner, "wait_for_delivery",
        lambda *a, **k: waits.append((a, k)) or None,
    )
    assert runner.main(["--config", str(config)]) == 0
    # The resumable delivery is resumed, not re-claimed as a new task.
    assert processed == []
    assert len(waits) == 1
    assert waits[0][0][:2] == (FAKE_PR_URL, issue)
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
        lambda repos, slot_dir, max_concurrency: (
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
