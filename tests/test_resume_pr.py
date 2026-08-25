"""Resume the same PR after review findings or base conflicts (Issue #45).

Unit tests for the runner's resume path: an Issue labeled `ai-pr-opened`
carries a run-scoped scene in its `Muyan Pilot opened PR:` comment. The
next tick recovers run_id, branch, worktree and PR URL from that comment,
re-runs the base freshness merge on the ORIGINAL branch, hands the fix to
Pi in the ORIGINAL worktree, and re-verifies the SAME PR. Failures mark
the Issue `ai-blocked` and preserve the PR, branch and worktree.
"""
import json
import subprocess
from pathlib import Path

import pytest

import bootstrap_runner as runner


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


def test_issue_body_returns_the_issue_body(monkeypatch):
    monkeypatch.setattr(
        runner, "run_command",
        lambda *a, **k: json.dumps({"body": "task text"}),
    )
    assert runner.issue_body(9, repo="owner/repo") == "task text"


def test_issue_body_returns_empty_string_for_null_body(monkeypatch):
    monkeypatch.setattr(
        runner, "run_command",
        lambda *a, **k: json.dumps({"body": None}),
    )
    assert runner.issue_body(9, repo="owner/repo") == ""


def test_issue_body_rejects_non_object_payload(monkeypatch):
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: "[]")
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_body(9, repo="owner/repo")


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
                   body_payload: str | None = None,
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
                if command[-1] == "body":
                    return body_payload or json.dumps({"body": ""})
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


def test_pick_resumable_delivery_returns_newest_issue_with_scene(monkeypatch):
    calls = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["human note", opened_pr_comment()]),
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", counting)
    issue, scene = runner.pick_resumable_delivery("owner/repo")
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID
    assert scene["pr_url"] == FAKE_PR_URL
    # Newest-first list, then the full comment history of that Issue.
    assert calls[0] == [
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search", "label:ai-fix-needed -label:ai-blocked",
        "--json", "number,title,state,url", "--limit", "1",
    ]
    assert calls[1] == [
        "gh", "issue", "view", "9", "--repo", "owner/repo",
        "--json", "comments",
    ]


def test_pick_resumable_delivery_scans_only_fix_needed_issues(monkeypatch):
    """`ai-pr-opened` means awaiting review: only the explicit
    `ai-fix-needed` state (review finding or base conflict) is scanned
    for Fixer work, so a clean PR waiting for review is never sent to
    the Fixer (Issue #45 round-5 review, Major 1)."""
    calls = []

    def counting(command, **kwargs):
        calls.append(command)
        return "[]"

    monkeypatch.setattr(runner, "run_command", counting)
    assert runner.pick_resumable_delivery("owner/repo") is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search", "label:ai-fix-needed -label:ai-blocked",
        "--json", "number,title,state,url", "--limit", "1",
    ]]


def test_pick_resumable_delivery_returns_none_when_queue_empty(monkeypatch):
    monkeypatch.setattr(runner, "run_command", make_pick_fake("[]"))
    assert runner.pick_resumable_delivery("owner/repo") is None


def test_pick_resumable_delivery_blocks_issue_without_scene_comment(
    monkeypatch, caplog,
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
        runner.pick_resumable_delivery("owner/repo")
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Muyan Pilot failed:" in comments[0]
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_skips_closed_issue(monkeypatch):
    monkeypatch.setattr(
        runner, "run_command",
        make_pick_fake(issue_payload(state="CLOSED")),
    )
    assert runner.pick_resumable_delivery("owner/repo") is None


# ------------------------------------- malformed scene → ai-blocked (F2)

def test_pick_resumable_delivery_blocks_issue_when_scene_is_malformed(
    monkeypatch, caplog,
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
        runner.pick_resumable_delivery("owner/repo")
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
    monkeypatch, caplog,
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
        runner.pick_resumable_delivery("owner/repo")
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Muyan Pilot failed:" in comments[0]
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_scene_failure_carries_marker_when_present(
    monkeypatch,
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
        runner.pick_resumable_delivery("owner/repo")
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in comments[0]


def test_pick_resumable_delivery_scene_failure_skips_bodyless_comments(
    monkeypatch,
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
        runner.pick_resumable_delivery("owner/repo")
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in comments[0]


def test_pick_resumable_delivery_scene_failure_preserves_error_when_reporting_fails(
    monkeypatch, caplog,
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
        runner.pick_resumable_delivery("owner/repo")
    assert "failure reporting failed" in caplog.text
    # The fake's edit/comment branches are reachable without capture
    # lists too (edits=None / comments=None): they simply do not record.
    assert fake(["gh", "issue", "edit", "9", "--repo", "owner/repo"]) == ""
    assert fake([
        "gh", "issue", "comment", "9", "--repo", "owner/repo",
        "--body", "x",
    ]) == ""


def test_pick_next_delivery_stops_when_scene_is_malformed(monkeypatch):
    """A malformed scene re-raises: the tick stops and no fresh task
    starts ahead of the broken delivery (round-5 review, Major 2)."""
    def broken(repo):
        raise ValueError("no 'Muyan Pilot opened PR' comment")

    calls = []
    monkeypatch.setattr(runner, "pick_resumable_delivery", broken)
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo: calls.append(("ready", repo)) or {"number": 10},
    )
    with pytest.raises(ValueError, match="no 'Muyan Pilot opened PR' comment"):
        runner.pick_next_delivery(["owner/repo"])
    # The ready queue was never consulted: no fresh claim started.
    assert calls == []


def test_pick_next_delivery_prefers_resumable_delivery_over_ready(monkeypatch):
    resumable = {"number": 9, "title": "ship"}
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo: calls.append(("resume", repo)) or (resumable, {"run_id": FAKE_RUN_ID}),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(["owner/repo"])
    assert result == ("owner/repo", resumable, {"run_id": FAKE_RUN_ID})
    assert calls == [("resume", "owner/repo")]


def test_pick_next_delivery_falls_back_to_ready_when_no_resumable(monkeypatch):
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo: calls.append(("resume", repo)) or None,
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(["owner/repo"])
    assert result == ("owner/repo", ready, None)
    assert calls == [("resume", "owner/repo"), ("ready", "owner/repo")]


def test_pick_next_delivery_scans_sources_in_order(monkeypatch):
    resumable = {"number": 9, "title": "ship"}
    scene = {"run_id": FAKE_RUN_ID}
    ready = {"number": 10, "title": "new"}
    calls = []
    monkeypatch.setattr(
        runner, "pick_resumable_delivery",
        lambda repo: calls.append(("resume", repo)) or (
            (resumable, scene) if repo == "owner/second" else None
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(["owner/first", "owner/second"])
    assert result == ("owner/second", resumable, scene)
    assert calls == [
        ("resume", "owner/first"), ("resume", "owner/second"),
    ]


def test_pick_next_delivery_returns_none_when_nothing_to_do(monkeypatch):
    monkeypatch.setattr(runner, "pick_resumable_delivery", lambda repo: None)
    monkeypatch.setattr(runner, "pick_issue", lambda repo: None)
    assert runner.pick_next_delivery(["owner/repo"]) is None


# ---------------------------------------------------------------- merge


def test_merge_latest_base_returns_false_when_head_contains_base(
    monkeypatch, tmp_path,
):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.merge_latest_base(tmp_path, "main") is False
    assert calls == [
        ["git", "fetch", "origin", "main"],
        ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
    ]


def test_merge_latest_base_merges_when_head_is_behind(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command)
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.merge_latest_base(tmp_path, "main") is True
    assert calls[-1] == ["git", "merge", "origin/main"]


def test_merge_latest_base_leaves_conflicts_for_the_fixer(
    monkeypatch, tmp_path, caplog,
):
    """A conflicted merge is left staged; the runner never auto-resolves."""
    error = subprocess.CalledProcessError(
        1, ["git", "merge", "origin/main"],
        stderr="CONFLICT (content): Merge conflict in a.txt",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["git", "merge"]:
            raise error
        # `git rev-parse -q --verify MERGE_HEAD` succeeds: mid-merge.
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    with caplog.at_level("WARNING"):
        assert runner.merge_latest_base(tmp_path, "main") is True
    # The conflict is handed to the fixer; no `git merge --abort`, no
    # force push, and the merge error did not escape.
    assert not any(
        "abort" in " ".join(command) or "force" in " ".join(command)
        for command in calls
    )
    assert "base_merge_conflict" in caplog.text


def test_merge_latest_base_fails_fast_on_non_conflict_merge_error(
    monkeypatch, tmp_path,
):
    error = subprocess.CalledProcessError(
        128, ["git", "merge", "origin/main"],
        stderr="fatal: refusing to merge unrelated histories",
    )

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return ""
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["git", "merge"]:
            raise error
        # No MERGE_HEAD: this was not a conflict.
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(runner, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.merge_latest_base(tmp_path, "main")
    assert excinfo.value is error


def test_merge_in_progress_checks_merge_head(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if command[:3] == ["git", "rev-parse", "-q"]:
            return "abc123"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.merge_in_progress(tmp_path) is True
    with pytest.raises(AssertionError, match="unexpected command"):
        runner.run_command(["git", "status"])


def test_merge_in_progress_false_when_merge_head_missing(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.merge_in_progress(tmp_path) is False


# ---------------------------------------------------------------- run_pi


def test_run_pi_resume_context_names_the_existing_pr(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM {{RUN_ID}}", encoding="utf-8")
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
        "owner/repo", branch=FAKE_BRANCH, pr_url=FAKE_PR_URL,
    )
    command, kwargs = calls[0]
    context = command[-1]
    assert f"Existing PR: {FAKE_PR_URL}" in context
    assert "same PR number" in context
    assert "never close" in context
    assert "never create a new PR" in context
    assert "No force push" in context
    assert kwargs["branch"] == FAKE_BRANCH


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


# ---------------------------------------------------------- resume_delivery


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


def config_for_resume(repo_dir: Path) -> dict:
    return {
        "repo_dir": repo_dir,
        "base_branch": "main",
        "source_repos": ["owner/repo"],
    }


def fake_verify_pr_for_resume(calls: list):
    """`verify_pr` fake that records the call and returns the scene URL."""
    def fake_verify(worktree, branch, base_branch, run_id, **kwargs):
        calls.append(("verify", (worktree, branch, base_branch, run_id),
                      kwargs))
        return FAKE_PR_URL
    return fake_verify


def derived_worktree(tmp_path: Path) -> Path:
    """The worktree the runner derives for Issue 9 / FAKE_RUN_ID."""
    path = runner.worktree_path(tmp_path, "owner/repo", 9, FAKE_RUN_ID)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_resume_delivery_success_keeps_same_run_branch_and_pr(
    monkeypatch, tmp_path, caplog,
):
    calls = []
    worktree = derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "merge_latest_base",
                        lambda wt, base: calls.append(("merge", wt, base)) or True)
    monkeypatch.setattr(runner, "run_pi",
                        lambda *args, **kwargs: calls.append(("run_pi", args, kwargs)) or "ok")
    monkeypatch.setattr(
        runner, "verify_pr",
        fake_verify_pr_for_resume(calls),
    )
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *args, **kwargs: calls.append(("comment", args, kwargs)))
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *args, **kwargs: calls.append(("edit", args, kwargs)))
    caplog.set_level("INFO")

    result = runner.resume_delivery(
        {"number": 9, "title": "ship", "body": ""},
        scene_for(),
        config_for_resume(tmp_path),
        "owner/repo",
    )

    assert result == FAKE_PR_URL
    # The ORIGINAL run id is re-bound for the whole resumed attempt.
    assert runner.current_run_id() == FAKE_RUN_ID
    # Branch and worktree are DERIVED from the configured repo_dir,
    # source_repo, Issue number and run id — never read from the comment.
    derived_branch = runner.task_branch("owner/repo", 9, FAKE_RUN_ID)
    expected_worktree = runner.worktree_path(
        tmp_path, "owner/repo", 9, FAKE_RUN_ID,
    )
    assert derived_branch == FAKE_BRANCH
    assert expected_worktree == worktree
    # Order: pre-verify → merge → fixer → post-verify → comment.
    assert calls[1] == ("merge", expected_worktree, "main")
    run_pi_args = calls[2]
    assert run_pi_args[0] == "run_pi"
    assert run_pi_args[1][1] == expected_worktree
    assert run_pi_args[2]["branch"] == derived_branch
    assert run_pi_args[2]["pr_url"] == FAKE_PR_URL
    # The PR is verified twice: before any git/Pi mutation (F1, without
    # the latest-base check — being behind is the state the resume fixes)
    # and after the fixer pushed (F3, with the full check). Both times it
    # must be the recovered original PR, in the configured source repo.
    verify_calls = [call for call in calls if call[0] == "verify"]
    assert len(verify_calls) == 2
    for verify_call in verify_calls:
        assert verify_call[1] == (expected_worktree, derived_branch, "main",
                                  FAKE_RUN_ID)
    assert verify_calls[0][2] == {
        "pr_repo": "owner/repo", "expected_url": FAKE_PR_URL,
        "require_latest_base": False,
    }
    assert verify_calls[1][2] == {
        "pr_repo": "owner/repo", "expected_url": FAKE_PR_URL,
    }
    # The pre-mutation verify ran before the merge and the fixer...
    assert calls.index(verify_calls[0]) < calls.index(
        ("merge", expected_worktree, "main"))
    # ...and the fixer ran between the two verifies.
    assert calls.index(verify_calls[1]) > calls.index(run_pi_args)
    comment = calls[-2]
    assert comment[0] == "comment"
    body = comment[2]["body"]
    assert f"Muyan Pilot fixed PR: {FAKE_PR_URL}" in body
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in body
    assert f"run_id={FAKE_RUN_ID}" in body
    # The fix-needed state is consumed: the Issue returns to awaiting
    # review (`ai-pr-opened`), so the next tick does not re-run the
    # Fixer (round-5 review, Major 1).
    assert calls[-1] == ("edit", (9,), {
        "repo": "owner/repo", "add": "ai-pr-opened", "remove": "ai-fix-needed",
    })
    # Every journal line of the resumed attempt carries the same run id.
    for message in caplog.messages:
        assert message.startswith(f"[{FAKE_RUN_ID}]"), message


def test_resume_delivery_fails_fast_when_scene_base_differs_from_config(
    monkeypatch, tmp_path, caplog,
):
    """A scene frozen on another base branch is never merged: the
    configured base must match before any git/Pi mutation."""
    calls = []
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base",
                        lambda wt, base: calls.append(("merge", wt, base)) or True)
    monkeypatch.setattr(runner, "run_pi", lambda *a, **k: "ok")
    monkeypatch.setattr(runner, "verify_pr", lambda *a, **k: FAKE_PR_URL)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    caplog.set_level("ERROR")

    scene = scene_for()
    scene["base_branch"] = "develop"
    with pytest.raises(RuntimeError, match="base branch mismatch"):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene,
            config_for_resume(tmp_path),
            "owner/repo",
        )
    # No merge, no fixer, no verify: the mismatch failed fast before any
    # mutation, and the Issue is marked ai-blocked with the reason.
    assert not any(call[0] == "merge" for call in calls)
    assert calls[0] == ("edit", (9,), {
        "repo": "owner/repo", "add": "ai-blocked", "remove": "ai-fix-needed",
    })
    assert "base branch mismatch" in calls[1][2]["body"]
    assert "issue=9 resume failed" in caplog.text


def test_resume_delivery_fails_fast_when_worktree_missing(
    monkeypatch, tmp_path, caplog,
):
    """The derived worktree (from config + issue number + run id) must
    exist locally; a comment can no longer point at an arbitrary path."""
    calls = []
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base",
                        lambda wt, base: calls.append(("merge", wt, base)) or True)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    caplog.set_level("ERROR")

    with pytest.raises(RuntimeError, match="worktree missing"):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )

    # ai-blocked with the concrete reason; PR and scene preserved.
    assert calls[0] == ("edit", (9,), {
        "repo": "owner/repo", "add": "ai-blocked", "remove": "ai-fix-needed",
    })
    failure_body = calls[1][2]["body"]
    assert "Muyan Pilot failed:" in failure_body
    assert "worktree missing" in failure_body
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in failure_body
    assert f"run_id={FAKE_RUN_ID}" in failure_body
    assert "pr_url=" + FAKE_PR_URL in failure_body
    # The merge was never attempted and no fixer was started.
    assert not any(call[0] == "merge" for call in calls)
    assert not any(call[0] == "comment" and "fixed PR" in call[2]["body"]
                   for call in calls)
    assert "issue=9 resume failed" in caplog.text


def _resume_pr_validation_failure_test(monkeypatch, tmp_path, caplog,
                                        error: str):
    """Shared shape: the open-PR pre-validation (verify_pr) fails fast
    before any merge or fixer starts, and the Issue is marked
    ai-blocked with the concrete reason."""
    calls = []
    worktree = derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)

    def fake_verify(worktree_, branch, base_branch, run_id, **kwargs):
        calls.append(("verify", (worktree_, branch, base_branch, run_id),
                      kwargs))
        raise RuntimeError(error)

    monkeypatch.setattr(runner, "merge_latest_base",
                        lambda wt, base: calls.append(("merge", wt, base)) or True)
    monkeypatch.setattr(runner, "run_pi",
                        lambda *a, **k: calls.append(("run_pi", a, k)) or "ok")
    monkeypatch.setattr(runner, "verify_pr", fake_verify)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    caplog.set_level("ERROR")

    with pytest.raises(RuntimeError, match=error):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )
    # The PR pre-validation ran with the derived branch/worktree and the
    # configured source repo + recovered original PR URL (without the
    # latest-base check: the pre-validation runs before the base merge)...
    verify_calls = [call for call in calls if call[0] == "verify"]
    assert verify_calls == [
        ("verify", (worktree, FAKE_BRANCH, "main", FAKE_RUN_ID),
         {"pr_repo": "owner/repo", "expected_url": FAKE_PR_URL,
          "require_latest_base": False}),
    ]
    # ...and it failed before any git mutation or fixer start.
    assert not any(call[0] == "merge" for call in calls)
    assert not any(call[0] == "run_pi" for call in calls)
    # The Issue is marked ai-blocked with the concrete reason.
    assert calls[-2][0] == "edit"
    assert calls[-2][1] == (9,)
    assert calls[-2][2] == {
        "repo": "owner/repo", "add": "ai-blocked",
        "remove": "ai-fix-needed",
    }
    assert error in calls[-1][2]["body"]
    assert "issue=9 resume failed" in caplog.text
    # The worktree is preserved, never deleted.
    assert worktree.is_dir()


def test_resume_delivery_fails_fast_when_pr_is_in_another_repo(
    monkeypatch, tmp_path, caplog,
):
    _resume_pr_validation_failure_test(
        monkeypatch, tmp_path, caplog,
        error="PR head repo is other/repo, expected owner/repo",
    )


def test_resume_delivery_fails_fast_when_no_open_pr_exists(
    monkeypatch, tmp_path, caplog,
):
    _resume_pr_validation_failure_test(
        monkeypatch, tmp_path, caplog,
        error="expected exactly one open PR for the task branch",
    )


def test_resume_delivery_fails_fast_when_pr_url_differs_from_scene(
    monkeypatch, tmp_path, caplog,
):
    """The verified PR URL must exactly equal the recovered original PR
    URL (finding 3): a different PR for the same branch is rejected."""
    _resume_pr_validation_failure_test(
        monkeypatch, tmp_path, caplog,
        error=("PR URL https://github.com/owner/repo/pull/99 is not the "
               "recovered original PR "
               "https://github.com/owner/repo/pull/9"),
    )


def test_resume_delivery_success_returns_verified_pr_url(
    monkeypatch, tmp_path,
):
    """The returned URL is the one verify_pr verified (pre- and
    post-fix), not a blind copy of the scene URL (finding 3)."""
    calls = []
    derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base", lambda wt, base: True)
    monkeypatch.setattr(runner, "run_pi", lambda *a, **k: "ok")
    monkeypatch.setattr(runner, "verify_pr",
                        lambda *a, **k: calls.append("verified") or FAKE_PR_URL)
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append("edit"))

    result = runner.resume_delivery(
        {"number": 9, "title": "ship", "body": ""},
        scene_for(),
        config_for_resume(tmp_path),
        "owner/repo",
    )
    assert result == FAKE_PR_URL
    assert calls == ["verified", "verified", "edit"]


def test_resume_delivery_marks_blocked_and_reraises_when_fixer_fails(
    monkeypatch, tmp_path, caplog,
):
    calls = []
    worktree = derived_worktree(tmp_path)
    (worktree / ".pi-session").mkdir()
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base", lambda wt, base: True)
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["pi"], stderr="fixer exploded"),
        ),
    )
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    monkeypatch.setattr(runner, "activity_snapshot",
                        lambda session_dir: None)
    caplog.set_level("ERROR")

    with pytest.raises(subprocess.CalledProcessError):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )

    assert calls[0] == ("edit", (9,), {
        "repo": "owner/repo", "add": "ai-blocked", "remove": "ai-fix-needed",
    })
    failure_body = calls[1][2]["body"]
    assert "Muyan Pilot failed:" in failure_body
    assert "returned non-zero exit status 1" in failure_body
    assert f"<!-- muyan-pilot:run={FAKE_RUN_ID} -->" in failure_body
    # The scene (PR, branch, worktree) stays queryable for the next attempt.
    assert "pr_url=" + FAKE_PR_URL in failure_body
    assert f"branch={FAKE_BRANCH}" in failure_body
    assert f"worktree={worktree}" in failure_body
    # The worktree is the derived one (config + issue + run id).
    assert worktree == runner.worktree_path(
        tmp_path, "owner/repo", 9, FAKE_RUN_ID,
    )
    # No success comment was posted.
    assert not any(
        call[0] == "comment" and "fixed PR" in call[2]["body"]
        for call in calls
    )
    # The worktree is preserved, never deleted.
    assert worktree.is_dir()


def test_resume_delivery_preserves_original_error_when_reporting_fails(
    monkeypatch, tmp_path, caplog,
):
    edit_calls = []

    def edit(*args, **kwargs):
        edit_calls.append(kwargs)
        raise RuntimeError("github report failed")

    worktree = derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base", lambda wt, base: True)
    monkeypatch.setattr(runner, "verify_pr", lambda *a, **k: FAKE_PR_URL)
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    monkeypatch.setattr(runner, "edit_issue", edit)
    monkeypatch.setattr(runner, "comment_issue", lambda *a, **k: None)
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: None)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="git failed",
    ):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )
    assert "failure reporting failed" in caplog.text


def test_resume_delivery_failure_comment_includes_session_scene(
    monkeypatch, tmp_path,
):
    calls = []
    worktree = derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base", lambda wt, base: True)
    monkeypatch.setattr(runner, "verify_pr", lambda *a, **k: FAKE_PR_URL)
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    monkeypatch.setattr(runner, "activity_snapshot", lambda session_dir: {
        "session_id": "sess-45",
        "session_file": str(worktree / ".pi-session" / "s.jsonl"),
        "phase": "base",
        "last_activity": "2026-08-25T02:30:00Z",
        "last": "bash git merge origin/main",
    })
    with pytest.raises(RuntimeError, match="git failed"):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )
    failure_body = calls[-1][2]["body"]
    assert "Muyan Pilot failed: git failed" in failure_body
    assert "session=sess-45" in failure_body
    assert "phase=base" in failure_body
    assert "last=bash git merge origin/main" in failure_body


def test_resume_delivery_scene_lookup_failure_is_isolated(
    monkeypatch, tmp_path, caplog,
):
    calls = []
    worktree = derived_worktree(tmp_path)
    monkeypatch.setattr(runner, "set_run_id", lambda run_id: None)
    monkeypatch.setattr(runner, "merge_latest_base", lambda wt, base: True)
    monkeypatch.setattr(runner, "verify_pr", lambda *a, **k: FAKE_PR_URL)
    monkeypatch.setattr(
        runner, "run_pi",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    monkeypatch.setattr(runner, "edit_issue",
                        lambda *a, **k: calls.append(("edit", a, k)))
    monkeypatch.setattr(runner, "comment_issue",
                        lambda *a, **k: calls.append(("comment", a, k)))
    monkeypatch.setattr(
        runner, "activity_snapshot",
        lambda session_dir: (_ for _ in ()).throw(OSError("disk error")),
    )
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="git failed"):
        runner.resume_delivery(
            {"number": 9, "title": "ship", "body": ""},
            scene_for(),
            config_for_resume(tmp_path),
            "owner/repo",
        )
    assert "activity scene failed" in caplog.text
    failure_body = calls[-1][2]["body"]
    assert "Muyan Pilot failed: git failed" in failure_body
    assert "session=" not in failure_body


# -------------------------------------------------------------------- main


def test_pick_resumable_delivery_fetches_issue_body_for_the_fixer(monkeypatch):
    """The fixer needs the Issue body; the resumable issue carries it."""
    def fake_run(command, **kwargs):
        if command[1] == "issue":
            if command[2] == "list":
                return issue_payload()
            if command[2] == "view":
                if command[-1] == "comments":
                    return gh_comments_payload([opened_pr_comment()])
                return json.dumps({
                    "number": 9, "title": "ship",
                    "body": "the original task description",
                })
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "run_command", fake_run)
    issue, scene = runner.pick_resumable_delivery("owner/repo")
    assert issue["body"] == "the original task description"
    assert scene["run_id"] == FAKE_RUN_ID
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "list"])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "issue", "status", "--repo", "owner/repo"])


def test_main_resumes_resumable_delivery_before_claiming_new(monkeypatch, tmp_path):
    issue = {"number": 9, "title": "ship", "body": ""}
    scene = scene_for()
    resumed = []
    processed = []
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos: ("owner/repo", issue, scene),
    )
    monkeypatch.setattr(
        runner, "resume_delivery",
        lambda *args, **kwargs: resumed.append(args) or FAKE_PR_URL,
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or FAKE_PR_URL,
    )
    assert runner.main(["--config", str(config)]) == 0
    # The resumable delivery is resumed, not re-claimed as a new task.
    assert len(resumed) == 1
    assert resumed[0][0] is issue
    assert resumed[0][1] is scene
    assert resumed[0][3] == "owner/repo"
    assert processed == []


def test_main_still_claims_new_issue_when_no_resumable(monkeypatch, tmp_path):
    issue = {"number": 10, "title": "new", "body": ""}
    processed = []
    (tmp_path / "prompt.md").write_text("prompt", encoding="utf-8")
    config = tmp_path / "muyan-pilot.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos: ("owner/repo", issue, None),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or FAKE_PR_URL,
    )
    assert runner.main(["--config", str(config)]) == 0
    assert len(processed) == 1
    assert processed[0][0] is issue
    assert processed[0][2] == "owner/repo"
