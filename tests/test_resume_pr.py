"""Resume the same PR from its opened-PR state (Issue #45, #82).

Unit tests for the runner's resume path: an Issue in an opened-PR state
(`ai-pr-opened` or `ai-fix-needed`) carries a run-scoped scene in its
`Orbi opened PR:` comment. The next tick recovers run_id, branch,
worktree and PR URL from that comment and resumes the delivery on the
ORIGINAL branch, worktree and PR. Issue #82 removed the cold-start
fixer: both states resume into the SAME independent review session,
which fixes findings in the same session. Failures mark the Issue
`ai-blocked` and preserve the PR, branch and worktree.
"""
from orbi import config as config_domain
import json
import os
import subprocess
from pathlib import Path

import pytest

import orbi.gitops as gitops
import orbi.runner as runner
import orbi.pi_session as pi_session
import orbi.milestone as milestone
from orbi import progress
from orbi import scene as scene_mod
from tests.test_progress_wiring import make_fake_gh
from tests.fakes.github import FakeGh
from tests.fakes.gitops import FakeGit
from seam import seam
import orbi.journal as journal
import orbi.github as github
from orbi.delivery_scene import RunContext


FAKE_RUN_ID = "a1b2c3d4"
FAKE_BRANCH = f"orbi/owner-repo-issue-9"
FAKE_WORKTREE = "/srv/repo/.worktrees/orbi-owner-repo-issue-9-a1b2c3d4"
FAKE_PR_URL = "https://github.com/owner/repo/pull/9"


def test_missing_pr_scene_recovery_republishes_from_run_state(monkeypatch):
    issue = {"number": 9}
    worktree = Path("/tmp/delivery")
    state = {"branch": "orbi/owner-repo-issue-9"}
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (FAKE_RUN_ID, worktree),
    )
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: state)
    monkeypatch.setitem(
        runner.__dict__, "open_pr_for_branch",
        lambda *_args: {"baseRefName": "main", "baseRefOid": "a" * 40,
                        "url": FAKE_PR_URL},
    )
    posted = []
    monkeypatch.setitem(
        runner.__dict__, "comment_issue", lambda number, **kwargs: posted.append(kwargs["body"]),
    )
    found = runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo"))
    assert found["run_id"] == FAKE_RUN_ID
    assert found["pr_url"] == FAKE_PR_URL
    assert posted and "Orbi opened PR:" in posted[0]


def test_missing_pr_scene_retry_stays_recoverable_on_write_failure(monkeypatch):
    issue = {"number": 9}
    worktree = Path("/tmp/delivery")
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (FAKE_RUN_ID, worktree),
    )
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: {"branch": "branch"})
    monkeypatch.setitem(
        runner.__dict__, "open_pr_for_branch",
        lambda *_args: {"baseRefName": "main", "baseRefOid": "a" * 40,
                        "url": FAKE_PR_URL},
    )
    monkeypatch.setitem(runner.__dict__, "comment_issue", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None
    assert runner._has_recoverable_pr_scene(issue, "owner/repo", Path("/tmp/repo"))


def test_missing_pr_scene_helper_handles_unavailable_state_pr_and_bad_pr(monkeypatch):
    issue = {"number": 9}
    worktree = Path("/tmp/delivery")
    monkeypatch.setitem(runner.__dict__, "worktree_resume_scene", lambda *_args: (FAKE_RUN_ID, worktree))
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: None)
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: {"branch": "branch"})
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *_args: None)
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *_args: {"url": FAKE_PR_URL})
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None


def test_missing_pr_scene_helper_handles_no_resume_and_unparseable_scene(monkeypatch):
    issue = {"number": 9}
    monkeypatch.setitem(runner.__dict__, "worktree_resume_scene", lambda *_args: None)
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None
    monkeypatch.setitem(runner.__dict__, "worktree_resume_scene", lambda *_args: (FAKE_RUN_ID, Path("/tmp/delivery")))
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: {"branch": "branch"})
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *_args: {
        "baseRefName": "main", "baseRefOid": "a" * 40, "url": FAKE_PR_URL,
    })
    monkeypatch.setitem(runner.__dict__, "comment_issue", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(runner.__dict__, "parse_pr_comment", lambda _body: None)
    assert runner._recover_missing_pr_scene(issue, "owner/repo", Path("/tmp/repo")) is None


def test_has_recoverable_pr_scene_handles_probe_failure(monkeypatch):
    worktree = Path("/tmp/delivery")
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (FAKE_RUN_ID, worktree),
    )
    monkeypatch.setitem(
        runner.__dict__, "read_run_state",
        lambda _path: {"branch": "branch"},
    )
    monkeypatch.setitem(
        runner.__dict__, "open_pr_for_branch",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert runner._has_recoverable_pr_scene(
        {"number": 9}, "owner/repo", Path("/tmp/repo"),
    )


def test_has_recoverable_pr_scene_handles_outer_probe_failure(monkeypatch):
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert not runner._has_recoverable_pr_scene(
        {"number": 9}, "owner/repo", Path("/tmp/repo"),
    )


def test_has_recoverable_pr_scene_handles_missing_state(monkeypatch):
    worktree = Path("/tmp/delivery")
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (FAKE_RUN_ID, worktree),
    )
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: None)
    assert not runner._has_recoverable_pr_scene(
        {"number": 9}, "owner/repo", Path("/tmp/repo"),
    )


def test_has_recoverable_pr_scene_rejects_missing_pr(monkeypatch):
    worktree = Path("/tmp/delivery")
    monkeypatch.setitem(
        runner.__dict__, "worktree_resume_scene",
        lambda *_args: (FAKE_RUN_ID, worktree),
    )
    monkeypatch.setitem(
        runner.__dict__, "read_run_state",
        lambda _path: {"branch": "branch"},
    )
    monkeypatch.setitem(
        runner.__dict__, "open_pr_for_branch", lambda *_args: None,
    )
    assert not runner._has_recoverable_pr_scene(
        {"number": 9}, "owner/repo", Path("/tmp/repo"),
    )


def test_missing_pr_scene_recovery_clears_scene_timestamp(monkeypatch):
    monkeypatch.setitem(runner.__dict__, "worktree_resume_scene", lambda *_args: (FAKE_RUN_ID, Path("/tmp/delivery")))
    monkeypatch.setitem(runner.__dict__, "read_run_state", lambda _path: {"branch": "branch"})
    monkeypatch.setitem(runner.__dict__, "open_pr_for_branch", lambda *_args: {
        "baseRefName": "main", "baseRefOid": "a" * 40, "url": FAKE_PR_URL,
    })
    monkeypatch.setitem(runner.__dict__, "comment_issue", lambda *_args, **_kwargs: None)
    recovered = {"run_id": FAKE_RUN_ID, "scene_at": "old"}
    monkeypatch.setitem(runner.__dict__, "parse_pr_comment", lambda _body: recovered)
    assert runner._recover_missing_pr_scene(
        {"number": 9}, "owner/repo", Path("/tmp/repo"),
    ) == {"run_id": FAKE_RUN_ID, "scene_at": None}


def test_pick_missing_pr_scene_recovers_or_defers_or_blocks(monkeypatch, tmp_path):
    issue = {"number": 9, "title": "delivery", "state": "OPEN",
             "labels": [{"name": "ai-pr-opened"}], "body": ""}
    monkeypatch.setitem(runner.__dict__, "slot_held_deliveries", lambda *_args: set())
    monkeypatch.setitem(runner.__dict__, "list_issues", lambda *args, **kwargs: [issue])
    monkeypatch.setitem(runner.__dict__, "issue_comments", lambda *args, **kwargs: [])
    monkeypatch.setitem(runner.__dict__, "_route_external_pr_ticket", lambda *_args: False)
    monkeypatch.setitem(runner.__dict__, "apply_label_patch", lambda *args, **kwargs: None)
    monkeypatch.setitem(runner.__dict__, "comment_issue", lambda *args, **kwargs: None)
    monkeypatch.setitem(runner.__dict__, "_recover_missing_pr_scene", lambda *args: {"run_id": FAKE_RUN_ID})
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    )[1]["run_id"] == FAKE_RUN_ID
    monkeypatch.setitem(runner.__dict__, "_recover_missing_pr_scene", lambda *args: None)
    monkeypatch.setitem(runner.__dict__, "_has_recoverable_pr_scene", lambda *args: True)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    ) is None
    monkeypatch.setitem(runner.__dict__, "_has_recoverable_pr_scene", lambda *args: False)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1, tmp_path,
    ) is None


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    """Each test starts without a bound run id."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)


def opened_pr_comment(run_id=FAKE_RUN_ID,
                      base_branch="main", base_sha="abc123def456",
                      pr_url=FAKE_PR_URL) -> str:
    # The runner is the only writer of this comment. It carries the run
    # scene (run_id, base, PR URL); branch and worktree are derived by
    # the runner from its own config and the run id, never parsed from a
    # comment (a public comment must not be able to name a local path).
    return (
        f"<!-- orbi:run={run_id} -->\n"
        f"Orbi opened PR: {pr_url} "
        f"(base_branch={base_branch} base_sha={base_sha} run_id={run_id})"
    )


def scene_for() -> dict:
    # The scene recovered from the trusted `Orbi opened PR:`
    # comment. Branch and worktree are NOT part of it: the runner
    # derives them from its own config, the Issue number and the run id.
    # `external` is the Issue #608 external-takeover marker — empty for
    # a Runner-owned PR, `true` for a contributor-PR takeover.
    # `review_round` travels with the scene (Issue #788): the round
    # budget's counter, 0 when the PR opens. (`resume_scene` additionally
    # stamps `scene_at` — the scene comment's `createdAt` — which this
    # parse-level projection does not carry.)
    return {
        "run_id": FAKE_RUN_ID,
        "base_branch": "main",
        "base_sha": "abc123def456",
        "pr_url": FAKE_PR_URL,
        "external": "",
        "review_round": 0,
    }


# ---------------------------------------------------------------- parse


def test_parse_pr_comment_returns_scene_for_multiline_opened_pr_comment():
    body = runner.opened_pr_comment_body(
        FAKE_RUN_ID, "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4",
        FAKE_PR_URL,
    )
    scene = runner.parse_pr_comment(body)
    assert scene == scene_for()
    # Issue #786: the machine-readable record is the hidden
    # `orbi:scene:v1` block right under the run marker; the field lines
    # stay for humans only.
    scene_block = scene_mod.render(scene_mod.Scene(
        run_id=FAKE_RUN_ID,
        base_branch="main",
        base_sha="abc123def456",
        pr_url=FAKE_PR_URL,
    ))
    assert body.splitlines() == [
        f"<!-- orbi:run={FAKE_RUN_ID} -->",
        scene_block,
        f"Orbi opened PR: {FAKE_PR_URL}",
        "- base_branch: main",
        f"- run_id={FAKE_RUN_ID}",
        "",
        "<details><summary>Run details</summary>",
        "",
        "- base_sha: abc123def456",
        "",
        "</details>",
        "",
        f"<!-- runner={progress.runner_fingerprint()} -->",
    ]


def test_parse_pr_comment_preserves_unknown_head_counter():
    body = runner.opened_pr_comment_body(
        FAKE_RUN_ID, "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4",
        FAKE_PR_URL,
    )
    record = scene_mod.Scene(
        run_id=FAKE_RUN_ID, base_branch="main",
        base_sha="abc123def456", pr_url=FAKE_PR_URL,
        verdict_head_unknown_round=2,
    )
    parsed = runner.parse_pr_comment(
        body.split(scene_mod.render(scene_mod.Scene(
            run_id=FAKE_RUN_ID, base_branch="main",
            base_sha="abc123def456", pr_url=FAKE_PR_URL,
        )), 1)[0] + scene_mod.render(record)
    )
    assert parsed["verdict_head_unknown_round"] == 2


def test_parse_pr_comment_returns_scene_for_legacy_opened_pr_comment():
    scene = runner.parse_pr_comment(opened_pr_comment())
    assert scene == scene_for()


def test_parse_pr_comment_ignores_unrelated_new_format_line():
    body = runner.opened_pr_comment_body(
        FAKE_RUN_ID, "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4",
        FAKE_PR_URL,
    ) + "\nnot a field"
    assert runner.parse_pr_comment(body) == scene_for()


def test_started_pi_comment_uses_multiline_field_block():
    body = runner.started_pi_comment_body(
        RunContext(
            run_id=FAKE_RUN_ID, issue=18, branch=FAKE_BRANCH,
            worktree=Path(FAKE_WORKTREE), source_repo="owner/repo",
        ),
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4 priority=normal",
    )
    assert body == (
        f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
        f"Orbi started Pi: run_id={FAKE_RUN_ID} priority=normal\n"
        "- base_branch: main\n"
        f"- run_id={FAKE_RUN_ID}\n"
        "- priority: normal\n"
        "\n"
        "<details><summary>Run details</summary>\n"
        "\n"
        "- base_sha: abc123def456\n"
        f"- branch: {FAKE_BRANCH}\n"
        f"- worktree: {FAKE_WORKTREE}\n"
        "\n"
        "</details>\n\n"
        f"<!-- runner={progress.runner_fingerprint()} -->"
    )


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
        f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
        f"Orbi started Pi: base_branch=main base_sha=abc123def456 "
        f"run_id={FAKE_RUN_ID} branch={FAKE_BRANCH} worktree={FAKE_WORKTREE}"
    )
    assert runner.parse_pr_comment(body) is None


def test_parse_pr_comment_returns_none_for_failed_comment():
    body = (
        f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
        f"Orbi failed: boom (base_branch=main base_sha=abc123def456 "
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
    with pytest.raises(ValueError, match="no 'Orbi opened PR' comment"):
        runner.resume_scene(comments)


def test_resume_scene_skips_non_dict_comments():
    # The non-dict entry is hit first when scanning newest-first.
    comments = [
        comment(opened_pr_comment()),
        "not a dict",
    ]
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


def test_authenticated_github_login_uses_active_gh_account(monkeypatch):
    calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command: calls.append(command) or (
            "github.com\n"
            "  ✓ Logged in to github.com account orbi-dev-test[bot] (keyring)\n"
            "  - Active account: true\n"
        ),
    )
    assert github._authenticated_github_login() == "orbi-dev-test[bot]"
    assert calls == [["gh", "auth", "status", "--hostname", "github.com"]]


def test_authenticated_github_login_selects_active_account(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda command: (
            "github.com\n"
            "  ✓ Logged in to github.com account old-bot[bot] (keyring)\n"
            "  - Active account: false\n"
            "  ✓ Logged in to github.com account orbi-dev-test[bot] (keyring)\n"
            "  - Active account: true\n"
        ),
    )
    assert github._authenticated_github_login() == "orbi-dev-test[bot]"


def test_authenticated_github_login_reports_identity_resolution_failure(monkeypatch):
    def fail(command):
        raise RuntimeError("gh api installation: 404 Not Found")

    monkeypatch.setattr(seam, "run_command", fail)
    with pytest.raises(ValueError, match="identity resolution.*gh auth status"):
        github._authenticated_github_login()


def test_authenticated_github_login_rejects_missing_active_account(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda command: "Active account: true\n",
    )
    with pytest.raises(ValueError, match="identity resolution"):
        github._authenticated_github_login()


def test_authenticated_github_login_resolves_despite_a_failed_inactive_account(
        monkeypatch):
    # Issue #1074: `gh auth status` exits 1 whenever ANY configured
    # account fails, even when the active account is valid — the
    # measured production report (stderr, run 27d71eef) must resolve
    # to the active account, not block the delivery.
    measured_report = (
        "github.com\n"
        "  ✓ Logged in to github.com account orbi-build[bot] (…/hosts.yml)\n"
        "  - Active account: true\n"
        "  - Git operations protocol: https\n"
        "  - Token: ghs_***\n"
        "  X Failed to log in to github.com account orbi-dev-test[bot]"
        " (…/hosts.yml)\n"
        "  - Active account: false\n"
    )

    def exit_one(command):
        raise subprocess.CalledProcessError(
            1, command, output="", stderr=measured_report)

    monkeypatch.setattr(seam, "run_command", exit_one)
    assert github._authenticated_github_login() == "orbi-build[bot]"


def test_authenticated_github_login_exit_one_without_an_active_account_fails(
        monkeypatch):
    # The real exit-1 no-account shape (isolated empty GH_CONFIG_DIR):
    # the report carries no `Active account: true` line, so the
    # existing error stays.
    def exit_one(command):
        raise subprocess.CalledProcessError(
            1, command, output="",
            stderr="You are not logged into any GitHub hosts. "
                   "To log in, run: gh auth login\n")

    monkeypatch.setattr(seam, "run_command", exit_one)
    with pytest.raises(ValueError, match="did not report an active account"):
        github._authenticated_github_login()


def test_resume_scene_accepts_the_authenticated_runner_app_bot(monkeypatch):
    comments = [{
        "body": opened_pr_comment(),
        "authorAssociation": "NONE",
        "author": {"login": "orbi-dev-test[bot]"},
    }]

    def gh_status(command):
        assert command == [
            "gh", "auth", "status", "--hostname", "github.com",
        ]
        return (
            "github.com\n"
            "  ✓ Logged in to github.com account orbi-dev-test[bot] (keyring)\n"
            "  - Active account: true\n"
        )

    monkeypatch.setattr(seam, "run_command", gh_status)
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


def test_resume_scene_rejects_another_app_bot_even_with_the_marker(monkeypatch):
    comments = [{
        "body": opened_pr_comment(),
        "authorAssociation": "NONE",
        "author": {"login": "unrelated-app[bot]"},
    }]
    monkeypatch.setattr(
        seam, "_authenticated_github_login",
        lambda: "orbi-dev-test[bot]",
    )
    with pytest.raises(ValueError, match="no 'Orbi opened PR' comment"):
        runner.resume_scene(comments)


# Issue #655: `gh issue view --json comments` (GraphQL) returns
# `author.login` without the `[bot]` suffix while REST keeps it. Both shapes
# describe the same App credential and must be trusted; a foreign App, with
# or without the suffix, must not.
@pytest.mark.parametrize("login,expected", [
    # The real GraphQL shape of this App bot (the reported failure).
    ("orbi-dev-test", True),
    # The REST shape of this App bot.
    ("orbi-dev-test[bot]", True),
    # Another App, suffix-less GraphQL shape.
    ("other-app", False),
    # Another App, REST shape.
    ("other-app[bot]", False),
    # A shared prefix must not match: the comparison is exact after
    # normalization.
    ("orbi-dev-test-2", False),
])
def test_comment_is_trusted_normalizes_optional_bot_suffix(
    monkeypatch, login, expected,
):
    monkeypatch.setattr(
        seam, "_authenticated_github_login",
        lambda: "orbi-dev-test[bot]",
    )
    comment = {
        "body": opened_pr_comment(),
        "authorAssociation": "NONE",
        "author": {"login": login},
    }
    assert runner._comment_is_trusted(comment) is expected


def test_resume_scene_accepts_graphql_shaped_runner_app_bot(monkeypatch):
    """End-to-end resume with the exact GraphQL author shape that failed."""
    comments = [{
        "body": opened_pr_comment(),
        "authorAssociation": "NONE",
        "author": {"login": "orbi-dev-test"},
    }]
    monkeypatch.setattr(
        seam, "_authenticated_github_login",
        lambda: "orbi-dev-test[bot]",
    )
    scene = runner.resume_scene(comments)
    assert scene["run_id"] == FAKE_RUN_ID


@pytest.mark.parametrize("author", [
    {"login": None},
    {"login": 7},
    {},
    "not-a-dict",
])
def test_comment_is_trusted_rejects_missing_or_non_string_login(
    monkeypatch, author,
):
    monkeypatch.setattr(
        seam, "_authenticated_github_login",
        lambda: "orbi-dev-test[bot]",
    )
    comment = {
        "body": opened_pr_comment(),
        "authorAssociation": "NONE",
        "author": author,
    }
    assert runner._comment_is_trusted(comment) is False


# ------------------------------------------------- trusted comments (F1)


def test_resume_scene_ignores_public_comment_with_scene():
    """A public comment (authorAssociation=NONE) must never steer the
    runner into an arbitrary local worktree/branch/PR, even when it is
    the only and newest scene comment."""
    comments = [
        comment(opened_pr_comment(), association="NONE"),
    ]
    with pytest.raises(ValueError, match="no 'Orbi opened PR' comment"):
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
    with pytest.raises(ValueError, match="no 'Orbi opened PR' comment"):
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
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: payload)
    comments = runner.issue_comments(9, repo="owner/repo")
    assert comments == [
        {"body": "first", "authorAssociation": "NONE"},
        {"body": "second", "authorAssociation": "OWNER"},
    ]


def test_issue_comments_rejects_top_level_array_payload(monkeypatch):
    monkeypatch.setattr(seam, "run_command",
        lambda *a, **k: json.dumps([{"body": "first"}]),
    )
    with pytest.raises(ValueError, match="issue view must be a JSON object"):
        runner.issue_comments(9, repo="owner/repo")


def test_issue_comments_rejects_payload_without_comments_array(monkeypatch):
    monkeypatch.setattr(seam, "run_command", lambda *a, **k: "{}")
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


def issue_payload(state: str = "OPEN",
                  labels: list[str] | None = None) -> str:
    if labels is None:
        labels = ["ai-fix-needed"]
    return json.dumps([
        {"number": 9, "title": "ship", "state": state,
         "url": "https://github.com/owner/repo/issues/9",
         "labels": [{"name": name} for name in labels]},
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
    monkeypatch.setattr(seam, "run_command", fake)
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

    monkeypatch.setattr(seam, "run_command", counting)
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
        "label:ai-fix-needed,ai-pr-opened,ai-awaiting-merge "
        "-label:ai-blocked -label:ai-merged",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        # `body` (Issue #787): the scene classification reads the
        # delivery markers, so the #726 external routing of a marker
        # ticket with no trusted scene comment is reachable.
        # Issue #809: the page is max_concurrency + 1 candidates — at
        # most max_concurrency deliveries can be held by live co-runners,
        # so a free candidate is always inside the page when one exists.
        "--json", "number,title,state,url,labels,body", "--limit", "2",
    ]
    assert calls[1] == [
        "gh", "issue", "view", "9", "--repo", "owner/repo",
        "--json", "comments",
    ]


def test_pick_resumable_delivery_scans_all_open_pr_states(
    monkeypatch, tmp_path,
):
    """Every resumable opened-PR state is scanned: `ai-fix-needed`
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

    monkeypatch.setattr(seam, "run_command", counting)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert calls == [[
        "gh", "issue", "list", "--repo", "owner/repo", "--state", "open",
        "--search",
        "label:ai-fix-needed,ai-pr-opened,ai-awaiting-merge "
        "-label:ai-blocked -label:ai-merged",
        # `labels` (Issue #101): a resumed P0 delivery keeps its
        # priority in the progress comment through review/merge.
        # `body` (Issue #787): the scene classification reads the
        # delivery markers, so the #726 external routing of a marker
        # ticket with no trusted scene comment is reachable.
        # Issue #809: the page is max_concurrency + 1 candidates — at
        # most max_concurrency deliveries can be held by live co-runners,
        # so a free candidate is always inside the page when one exists.
        "--json", "number,title,state,url,labels,body", "--limit", "2",
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
    monkeypatch.setattr(seam, "run_command", fake)
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID
    assert scene["pr_url"] == FAKE_PR_URL


def test_pick_resumable_delivery_returns_none_when_queue_empty(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(seam, "run_command", make_pick_fake("[]"))
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None


def test_pick_resumable_delivery_skips_only_the_held_delivery(
    monkeypatch, tmp_path,
):
    """Issue #809, acceptance 2 (the semantic that must NOT regress): a
    delivery a live other runner HOLDS — its slot file names the
    (repo, issue) — is never resumed here. That is the #70 round-1
    protection at the right granularity: no second review Pi in the
    same worktree/branch/run, no second `gh pr merge
    --match-head-commit` on the already-merged PR. The pre-#809 guard
    abandoned the WHOLE scan whenever any slot was held; now only the
    held candidate is skipped and the scan still reads the queue — the
    held candidate's comments are never even fetched (an in-flight
    delivery is not touched)."""
    gh_calls = []
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: gh_calls.append(command) or "[]",
    )
    monkeypatch.setattr(runner, "slot_held_deliveries",
                        lambda slot_dir, capacity: {("owner/repo", 9)})
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert len(gh_calls) == 1, "only the queue query, no candidate reads"
    assert gh_calls[0][:3] == ["gh", "issue", "list"]


def test_pick_resumable_delivery_reviews_free_pr_while_other_in_flight(
    monkeypatch, tmp_path,
):
    """Issue #809, acceptance 1: a delivery A in flight (another live
    runner's slot names it) must not stop the review of a DIFFERENT
    opened-PR delivery B. The pre-#809 guard returned None for the
    whole scan and the tick fell through to a fresh claim — the
    reported starvation (nine MERGEABLE PRs, the oldest 90 minutes,
    zero review ticks while new PRs kept opening)."""
    monkeypatch.setattr(
        runner, "slot_held_deliveries",
        lambda slot_dir, capacity: {("owner/repo", 7)},
    )
    monkeypatch.setattr(seam, "run_command", make_pick_fake(
        issue_payload(),
        gh_comments_payload(["human note", opened_pr_comment()]),
    ))
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID
    assert scene["pr_url"] == FAKE_PR_URL


def test_pick_resumable_delivery_skips_held_and_reviews_next_free(
    monkeypatch, tmp_path,
):
    """Issue #809, acceptance 4: the scan skips every HELD candidate and
    reviews the newest FREE one, so the PR backlog drains even while
    several deliveries are in flight — the drain rate scales with the
    concurrency instead of dropping to zero."""
    issues = [
        {"number": 9, "title": "held", "state": "OPEN",
         "url": "https://github.com/owner/repo/issues/9",
         "labels": [{"name": "ai-pr-opened"}]},
        {"number": 10, "title": "free", "state": "OPEN",
         "url": "https://github.com/owner/repo/issues/10",
         "labels": [{"name": "ai-pr-opened"}]},
    ]
    views = []

    def fake_run(command, **kwargs):
        if command[1] == "issue":
            if command[2] == "list":
                return json.dumps(issues)
            if command[2] == "view":
                views.append(command[3])
                return gh_comments_payload(
                    [opened_pr_comment(run_id="b2c3d4e5")],
                )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(runner, "slot_held_deliveries",
                        lambda slot_dir, capacity: {("owner/repo", 9)})
    monkeypatch.setattr(seam, "run_command", fake_run)
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
    )
    assert issue["number"] == 10
    assert scene["run_id"] == "b2c3d4e5"
    # Only the FREE candidate's comments were read; the held one was
    # skipped before any candidate read.
    assert views == ["10"]
    # The fake is a contract, not a sink: its strict arm is driven here.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "release", "view"])
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "issue", "edit", "9"])


def test_pick_resumable_delivery_ignores_holds_of_other_repos(
    monkeypatch, tmp_path,
):
    """Issue #809, acceptance 3 (the real deployment): two source repos
    share ONE slot dir (the same deploy_home derives it), so the held
    set carries deliveries of BOTH repos. A hold of repo A never blocks
    repo B's review — the skip matches on (repo, issue), never on the
    shared slot dir."""
    monkeypatch.setattr(
        runner, "slot_held_deliveries",
        lambda slot_dir, capacity: {("orbi-build/orbi-cloud", 367)},
    )
    monkeypatch.setattr(seam, "run_command", make_pick_fake(
        issue_payload(),
        gh_comments_payload(["human note", opened_pr_comment()]),
    ))
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
    )
    assert issue["number"] == 9
    assert scene["run_id"] == FAKE_RUN_ID


def test_pick_resumable_delivery_blocks_issue_without_scene_comment(
    monkeypatch, caplog, tmp_path,
):
    """An `ai-fix-needed` Issue whose comment history carries no trusted
    opened-PR comment at all cannot be resumed: it is blocked through
    the DISTINCT missing-scene branch (Issue #786 — never
    `block_scene_failure`, which only fires on a corrupted scene) and
    the scan returns None so the tick continues (Issue #672)."""
    edits: list[list[str]] = []
    comments: list[str] = []
    monkeypatch.setattr(seam, "run_command",
        make_pick_fake(
            issue_payload(),
            gh_comments_payload(["only a human comment here"]),
            edits=edits,
            comments=comments,
        ),
    )
    caplog.set_level("ERROR")
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Orbi failed:" in comments[0]
    assert "no trusted 'Orbi opened PR'" in comments[0]
    assert "issue=9 resume scene is missing" in caplog.text


def test_pick_resumable_delivery_blocks_pr_opened_issue_without_scene(
    monkeypatch, caplog, tmp_path,
):
    """Issue #672 (the incident scene): an `ai-pr-opened` Issue with no
    trusted scene comment is a single-Issue failure. The scan adds
    `ai-blocked` and returns None so the tick keeps going — it never
    crashes the tick. The erroneous `ai-pr-opened` label is deliberately
    left for a human: cleaning it is a separate state-inference problem
    (Issue #672 scope)."""
    edits: list[list[str]] = []
    comments: list[str] = []
    monkeypatch.setattr(seam, "run_command",
        make_pick_fake(
            issue_payload(labels=["ai-pr-opened"]),
            gh_comments_payload(["only a human comment here"]),
            edits=edits,
            comments=comments,
        ),
    )
    caplog.set_level("ERROR")
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    # The erroneous ai-pr-opened label is NOT auto-cleaned (Issue #672).
    assert "ai-pr-opened" not in edits[0]
    assert "Orbi failed:" in comments[0]
    assert "issue=9 resume scene is missing" in caplog.text


def test_pick_resumable_delivery_skips_closed_issue(monkeypatch, tmp_path):
    monkeypatch.setattr(seam, "run_command",
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
    the concrete reason and the scan returns None so the tick continues
    (Issue #672)."""
    calls = []
    edits: list[list[str]] = []
    comments: list[str] = []
    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["Orbi opened PR: "
                             "https://github.com/owner/repo/pull/9 "
                             "(base_branch=main base_sha=abc123def456)"
                             ]),
        edits=edits,
        comments=comments,
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", counting)
    caplog.set_level("ERROR")
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    # The blocked transition: add ai-blocked, remove ai-fix-needed...
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    # ...and a failure comment with the concrete reason...
    body = comments[0]
    assert "Orbi failed:" in body
    assert "missing run_id" in body
    # ...but no run marker: no valid run id exists, so none is guessed.
    assert "orbi:run=" not in body
    assert "issue=9 resume scene is malformed" in caplog.text


def test_pick_resumable_delivery_routes_corrupted_marker_and_next_candidate(
    monkeypatch, tmp_path,
):
    """A corrupted trusted scene on a marker-bearing ticket is routed
    through the #726 external takeover (never `ai-blocked`), and the
    scan CONTINUES to the next candidate: with the #809 page the route
    of one candidate no longer ends the scan — the next free opened-PR
    delivery is still reviewed this tick."""
    issues = [
        {"number": 9, "title": "marker", "state": "OPEN",
         "url": "https://github.com/owner/repo/issues/9",
         "body": "<!-- orbi:external-pr:55 -->\nfix the thing",
         "labels": [{"name": "ai-pr-opened"}]},
        {"number": 10, "title": "free", "state": "OPEN",
         "url": "https://github.com/owner/repo/issues/10",
         "body": "b",
         "labels": [{"name": "ai-pr-opened"}]},
    ]
    edits: list[list[str]] = []
    comments: list[str] = []

    def fake_run(command, **kwargs):
        if command[1] == "issue":
            if command[2] == "list":
                return json.dumps(issues)
            if command[2] == "view":
                number = command[3]
                if command[-1] == "comments":
                    body = (
                        # Trusted but corrupted: no run_id in the scene.
                        "Orbi opened PR: "
                        "https://github.com/owner/repo/pull/9 "
                        "(base_branch=main base_sha=abc123def456)"
                        if number == "9" else opened_pr_comment(
                            run_id="c3d4e5f6",
                        )
                    )
                    return gh_comments_payload([body])
                if command[-1] == "labels":
                    return json.dumps({"labels": [{"name": "ai-pr-opened"}]})
            if command[2] == "edit":
                edits.append(command)
                return ""
            if command[2] == "comment":
                comments.append(command[-1])
                return ""
        if command[1] == "pr" and command[2] == "view":
            return json.dumps({"state": "OPEN"})
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    found = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 2,
    )
    assert found is not None
    issue, scene = found
    assert issue["number"] == 10
    assert scene["run_id"] == "c3d4e5f6"
    # The corrupted marker ticket was routed to the takeover queue, not
    # blocked.
    assert any(
        "edit" in command and "9" in command and "ai-ready" in command
        for command in edits
    ), edits
    assert not any("ai-blocked" in command for command in edits)
    assert any(
        "external contribution PR #55" in body for body in comments
    )
    # The fake is a contract, not a sink: its strict arm is driven here.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "issue", "view", "9", "--json", "state"])


def test_pick_resumable_delivery_resumes_from_the_v1_scene_block(
    monkeypatch, tmp_path,
):
    """Issue #786: a production opened-PR comment carries the hidden
    `orbi:scene:v1` block, and the scan resumes the delivery from the
    block — the legacy text fallback stays for one transition version
    so older comments keep resuming."""
    body = runner.opened_pr_comment_body(
        FAKE_RUN_ID,
        "base_branch=main base_sha=abc123def456 run_id=a1b2c3d4",
        FAKE_PR_URL,
    )
    monkeypatch.setattr(seam, "run_command", make_pick_fake(
        issue_payload(), gh_comments_payload([body]),
    ))
    issue, scene = runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    )
    assert issue["number"] == 9
    # The scan's scene carries the `scene_at` stamp (the fake comment
    # payload has no createdAt, so it is None here).
    assert scene == {**scene_for(), "scene_at": None}


def test_pick_resumable_delivery_blocks_on_a_corrupted_v1_block(
    monkeypatch, tmp_path,
):
    """Issue #786: a corrupted `orbi:scene:v1` block is 损坏 — the
    corrupted branch fires `block_scene_failure` (its only trigger)
    and never falls back to the legacy text beside the block."""
    body = (
        f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
        "<!-- orbi:scene:v1 {broken -->\n"
        f"Orbi opened PR: {FAKE_PR_URL} "
        "(base_branch=main base_sha=abc123def456 run_id=a1b2c3d4)"
    )
    blocked = []
    monkeypatch.setattr(
        runner, "block_scene_failure",
        lambda issue, error, repo, comments: blocked.append(
            issue["number"],
        ),
    )
    monkeypatch.setattr(seam, "run_command", make_pick_fake(
        issue_payload(), gh_comments_payload([body]),
    ))
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert blocked == [9]


def test_pick_resumable_delivery_blocks_issue_when_no_trusted_scene(
    monkeypatch, caplog, tmp_path,
):
    """An `ai-fix-needed` Issue whose comment history carries no trusted
    opened-PR comment cannot be resumed: blocked, not skipped — through
    the missing-scene branch (Issue #786), never `block_scene_failure`."""
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

    monkeypatch.setattr(seam, "run_command", counting)
    caplog.set_level("ERROR")
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert "Orbi failed:" in comments[0]
    assert "issue=9 resume scene is missing" in caplog.text


def test_pick_resumable_delivery_missing_scene_names_the_run_marker(
    monkeypatch, tmp_path,
):
    """The missing-scene block names the run when some OTHER trusted
    comment (e.g. the progress comment) still carries its marker — the
    same run id, never a new one."""
    comments: list[str] = []
    monkeypatch.setattr(seam, "run_command", make_pick_fake(
        issue_payload(),
        gh_comments_payload([
            f"<!-- orbi:run={FAKE_RUN_ID} -->\n\n**Orbi progress**\n"
            "- phase: review",
        ]),
        edits=[],
        comments=comments,
    ))
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in comments[0]
    assert "no trusted 'Orbi opened PR'" in comments[0]


def test_pick_resumable_delivery_missing_scene_reporting_failure_is_bypass(
    monkeypatch, caplog, tmp_path,
):
    """A failure of the missing-scene REPORTING itself is logged, never
    raised (the same bypass contract as the corrupted path): the scan
    returns None so the tick continues."""
    monkeypatch.setattr(seam, "issue_comments", lambda number, repo: [])
    monkeypatch.setattr(seam, "run_command",
                        make_pick_fake(issue_payload()))

    def failing_edit(*args, **kwargs):
        raise RuntimeError("gh down")

    monkeypatch.setattr(seam, "edit_issue", failing_edit)
    caplog.set_level("ERROR")
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert "failure reporting failed" in caplog.text


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
            f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
            "Orbi opened PR: "
            "https://github.com/owner/repo/pull/9 "
            "(base_branch=main base_sha=abc123def456)"
        ]),
        comments=comments,
    )

    def counting(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", counting)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in comments[0]


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
                    f"<!-- orbi:run={FAKE_RUN_ID} -->\n"
                    "Orbi opened PR: "
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
    monkeypatch.setattr(seam, "run_command", fake)
    assert runner.pick_resumable_delivery(
        "owner/repo", tmp_path / "slots", 1,
    ) is None
    assert f"<!-- orbi:run={FAKE_RUN_ID} -->" in comments[0]


def test_pick_resumable_delivery_scene_failure_logs_reporting_failure(
    monkeypatch, caplog, tmp_path,
):
    """When the blocked transition itself cannot be reported (Issue
    #672), the failure is logged and the scan still returns None: the
    Issue stays in its opened-PR state so the next tick retries, and the
    runner never stalls on it."""

    fake = make_pick_fake(
        issue_payload(),
        gh_comments_payload(["only a human comment here"]),
    )

    def fake_run(command, **kwargs):
        if command[1] == "issue" and command[2] == "edit":
            raise RuntimeError("github edit failed")
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with caplog.at_level("ERROR"):
        assert runner.pick_resumable_delivery(
            "owner/repo", tmp_path / "slots", 1,
        ) is None
    assert "failure reporting failed" in caplog.text
    # The fake's edit/comment branches are reachable without capture
    # lists too (edits=None / comments=None): they simply do not record.
    assert fake(["gh", "issue", "edit", "9", "--repo", "owner/repo"]) == ""
    assert fake([
        "gh", "issue", "comment", "9", "--repo", "owner/repo",
        "--body", "x",
    ]) == ""


def test_pick_next_delivery_continues_after_scene_failure(
    monkeypatch, tmp_path,
):
    """Issue #672: a corrupted resumable Issue is blocked and the scan
    falls through to the ready queue in the SAME tick (never stops the
    tick ahead of a valid delivery)."""
    edits: list[list[str]] = []
    comments: list[str] = []
    ready = {"number": 10, "title": "new"}
    calls = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return "[]"
        if command[:3] == ["gh", "issue", "list"]:
            search = command[command.index("--search") + 1]
            if "label:ai-fix-needed,ai-pr-opened" in search:
                return issue_payload(labels=["ai-pr-opened"])
            return "[]"
        if command[:3] == ["gh", "issue", "view"]:
            return gh_comments_payload(["only a human comment here"])
        if command[:3] == ["gh", "issue", "edit"]:
            edits.append(command)
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            comments.append(command[-1])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None, **_kwargs: (
            calls.append(("ready", repo)) or ready
        ),
    )
    result = runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    )
    # The ready queue was consulted in the same tick: the corrupted
    # Issue was scoped to a single-ticket block, not a tick stop.
    assert result == ("owner/repo", ready, None)
    assert calls == [("ready", "owner/repo")]
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert len(comments) == 1
    assert "Orbi failed:" in comments[0]
    # The fake rejects anything but its own traffic (same guard pattern
    # as `make_pick_fake`).
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


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
            or (resumable, config_domain.RunnerConfig(run_id=FAKE_RUN_ID))
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None, **_kwargs: calls.append(("ready", repo)) or ready,
    )
    result = runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    )
    assert result == ("owner/repo", resumable, config_domain.RunnerConfig(run_id=FAKE_RUN_ID))
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
        lambda repo, slot_dir, max_concurrency, **_kwargs: (
            calls.append(("in_progress", repo)) or None
        ),
    )
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None, **_kwargs: calls.append(("ready", repo)) or ready,
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
    scene = config_domain.RunnerConfig(run_id=FAKE_RUN_ID)
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
        lambda repo, active_milestone=None, **_kwargs: calls.append(("ready", repo)) or ready,
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
        lambda repo, slot_dir, max_concurrency, **_kwargs: None,
    )
    monkeypatch.setattr(runner, "pick_issue", lambda repo, active_milestone=None, **_kwargs: None)
    assert runner.pick_next_delivery(
        ["owner/repo"], tmp_path / "slots", 1,
    ) is None




# ---------------------------------------------------------------- run_pi




def test_run_pi_fresh_context_has_no_existing_pr(monkeypatch, tmp_path):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("SYSTEM", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        pi_session, "stream_pi",
        lambda command, **kwargs: calls.append((command, kwargs)) or "done",
    )
    config = config_domain.RunnerConfig(prompt=prompt_path, repo_dir=tmp_path, source_repos=("owner/repo",), workspace_root=tmp_path, context_files=(), skills=(), base_branch="main", base_sha="abc123def456", run_id=FAKE_RUN_ID)
    pi_session.run_pi({"number": 9, "title": "t", "body": "b"}, RunContext(run_id=config.run_id, issue={"number": 9, "title": "t", "body": "b"}["number"], branch=FAKE_BRANCH, worktree=tmp_path, source_repo="owner/repo"), config)
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
    (tmp_path / "prompts").mkdir()
    for name in ("prompts/prompt.md", "prompts/prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, scene
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or runner.IssueResult("pr", FAKE_PR_URL),
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
        runner, "delivery_step",
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
    (tmp_path / "prompts").mkdir()
    for name in ("prompts/prompt.md", "prompts/prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text("source_repos = [\"owner/repo\"]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kwargs: (
            "owner/repo", issue, None
        ),
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args) or runner.IssueResult("pr", FAKE_PR_URL),
    )
    # The dispatch test must not run the real delivery-wait loop (it would
    # call `gh` against the real PR number of FAKE_PR_URL).
    monkeypatch.setattr(runner, "delivery_step", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    assert len(processed) == 1
    assert processed[0][0] is issue
    assert processed[0][2] == "owner/repo"


def test_main_continues_to_ready_delivery_after_scene_failure(
    monkeypatch, tmp_path,
):
    """Issue #672 acceptance (real dispatch): one corrupted resumable
    Issue (`ai-pr-opened`, no trusted scene) is marked `ai-blocked` with
    a reason, the SAME tick delivers the next ready Issue, and `main()`
    returns 0 — the runner no longer dies with status=1 on it."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (prompts / "prompt_review.md").write_text("review", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    ready = {"number": 10, "title": "new", "body": ""}
    edits: list[list[str]] = []
    comments: list[str] = []
    processed = []

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return "[]"
        if command[:3] == ["gh", "issue", "list"]:
            search = command[command.index("--search") + 1]
            if "label:ai-fix-needed,ai-pr-opened" in search:
                return issue_payload(labels=["ai-pr-opened"])
            return "[]"
        if command[:3] == ["gh", "issue", "view"]:
            return gh_comments_payload(["only a human comment here"])
        if command[:3] == ["gh", "issue", "edit"]:
            edits.append(command)
            return ""
        if command[:3] == ["gh", "issue", "comment"]:
            comments.append(command[-1])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(
        milestone, "sync_active_milestone_variable", lambda *a, **k: None,
    )
    # The held slot is a real file: `main` names the selected delivery in
    # it right after selection (Issue #809).
    slot_file = tmp_path / "slot-1"
    mark_fd = os.open(slot_file, os.O_RDWR | os.O_CREAT, 0o644)
    monkeypatch.setattr(runner, "acquire_slot", lambda *a, **k: type(
        "Slot", (), {
            "fd": mark_fd,
            "release": lambda self: os.close(mark_fd),
        },
    )())
    monkeypatch.setattr(
        runner, "pick_issue",
        lambda repo, active_milestone=None, **_kwargs: ready,
    )
    monkeypatch.setattr(
        runner, "process_issue",
        lambda *args, **kwargs: processed.append(args)
        or runner.IssueResult("pr", FAKE_PR_URL),
    )
    monkeypatch.setattr(runner, "delivery_step", lambda *a, **k: None)
    assert runner.main(["--config", str(config)]) == 0
    # The corrupted Issue was scoped to a single-ticket block...
    assert edits == [[
        "gh", "issue", "edit", "9", "--repo", "owner/repo",
        "--add-label", "ai-blocked", "--remove-label", "ai-fix-needed",
    ]]
    assert len(comments) == 1
    assert "Orbi failed:" in comments[0]
    # ...and the same tick delivered the next ready Issue.
    assert processed[0][0] is ready
    assert processed[0][2] == "owner/repo"
    # The selected delivery (the fresh claim of issue 10) was named in
    # the held slot file for the other runners' resume scans (Issue
    # #809).
    assert slot_file.read_text(encoding="utf-8").splitlines()[1] == (
        "owner/repo#10"
    )
    # The fake rejects anything but its own traffic.
    with pytest.raises(AssertionError, match="unexpected command"):
        fake_run(["gh", "pr", "list"])


# --------------------------- resume PR verification (Issue #89)


@pytest.mark.parametrize(
    "verification_error",
    [
        runner.ReportedMissingFixesError(
            "PR body is missing `Fixes #9`; the keyword must point at the "
            "source Issue so GitHub closes it natively"
        ),
        runner.ResumeVerificationError("reported terminal resume failure"),
    ],
)
def test_main_ends_cleanly_after_reported_resume_failure(
    monkeypatch, tmp_path, verification_error,
):
    """Issue #1219: a reported per-ticket failure does not kill the tick."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (prompts / "prompt_review.md").write_text("review", encoding="utf-8")
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    issue = {"number": 9, "title": "ship", "body": ""}
    released = []

    monkeypatch.setattr(milestone, "sync_active_milestone_variable", lambda *a, **k: None)
    monkeypatch.setattr(seam, "refresh_cli_install", lambda *a, **k: None)
    monkeypatch.setattr(runner, "check_unit_drift", lambda *a, **k: None)
    monkeypatch.setattr(runner, "check_transport", lambda *a, **k: {})
    monkeypatch.setattr(runner.runner_health, "run_health_check", lambda *a, **k: None)
    # The held slot is a real file: `main` names the resumed delivery in
    # it right after selection, BEFORE any verification work (Issue
    # #809) — asserted below even though the scene turns out stale.
    slot_file = tmp_path / "slot-1"
    mark_fd = os.open(slot_file, os.O_RDWR | os.O_CREAT, 0o644)

    def release_slot(self):
        os.close(mark_fd)
        released.append(True)

    monkeypatch.setattr(runner, "acquire_slot", lambda *a, **k: type(
        "Slot", (), {"fd": mark_fd, "release": release_slot},
    )())
    monkeypatch.setattr(
        runner, "pick_next_delivery",
        lambda *a, **k: ("owner/repo", issue, make_resume_scene()),
    )
    monkeypatch.setattr(
        runner, "verify_resumed_pr",
        lambda *a, **k: (_ for _ in ()).throw(verification_error),
    )
    monkeypatch.setattr(
        runner, "delivery_step",
        lambda *a, **k: pytest.fail("stale resume must not enter delivery wait"),
    )
    assert runner.main(["--config", str(config_path)]) == 0
    assert released == [True]
    # The resumed delivery was named in the held slot file (Issue #809).
    assert slot_file.read_text(encoding="utf-8").splitlines()[1] == (
        "owner/repo#9"
    )


def make_resume_config(tmp_path) -> dict:
    return config_domain.RunnerConfig(repo_dir=tmp_path, base_branch="main")


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


@pytest.mark.parametrize(
    ("open_prs", "scene_state", "expected", "error_type"),
    [
        ([], "CLOSED", "scene_pr_state=CLOSED", runner.UnrecoverableDeliveryError),
        ([], "MERGED", "scene_pr_state=MERGED", runner.UnrecoverableDeliveryError),
        ([{"url": "https://github.com/owner/repo/pull/10"},
         {"url": "https://github.com/owner/repo/pull/11"}],
         "OPEN", "open_pr_count=2", runner.ResumeVerificationError),
    ],
)
def test_verify_pr_resume_rejects_stale_or_ambiguous_scene_with_evidence(
    monkeypatch, tmp_path, open_prs, scene_state, expected, error_type,
):
    """Issue #494: resume classifies closed and ambiguous PR scenes."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "branch", "--show-current"]:
            return FAKE_BRANCH
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "head"
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps(open_prs)
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": scene_state,
                               "mergedAt": "2024-01-01" if scene_state == "MERGED" else None})
        raise AssertionError(command)

    with pytest.raises(AssertionError):
        fake_run(["unexpected"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(error_type) as excinfo:
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo="owner/repo", expected_url=FAKE_PR_URL, require_latest_base=False)
    message = str(excinfo.value)
    assert expected in message
    assert "open_prs=" in message
    assert FAKE_PR_URL in message
    assert any(command[:3] == ["gh", "pr", "view"] for command in commands)


def test_verify_pr_resume_rejects_pr_based_on_wrong_branch_with_evidence(
    monkeypatch, tmp_path,
):
    """Issue #291: the shared base check covers the non-resume path via
    _single_open_pr; the resume keeps its typed failure with the run
    evidence."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return FAKE_BRANCH
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "head"
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([{
                "url": FAKE_PR_URL,
                "baseRefName": "develop",
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "owner"},
            }])
        raise AssertionError(command)

    with pytest.raises(AssertionError):
        fake_run(["unexpected"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        runner.ResumeVerificationError,
        match="PR base is develop, expected main",
    ) as excinfo:
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo="owner/repo", expected_url=FAKE_PR_URL, require_latest_base=False)
    message = str(excinfo.value)
    assert "resume PR validation:" in message
    assert "open_pr_count=1" in message
    assert FAKE_PR_URL in message


def test_verify_pr_non_resume_rejects_multiple_open_prs(
    monkeypatch, tmp_path,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return FAKE_BRANCH
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "head"
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"url": FAKE_PR_URL}, {"url": FAKE_PR_URL}])
        raise AssertionError(command)

    with pytest.raises(AssertionError):
        fake_run(["unexpected"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, require_latest_base=False)


def test_verify_pr_resume_keeps_unknown_state_for_non_object_scene_lookup(
    monkeypatch, tmp_path,
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return FAKE_BRANCH
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "head"
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "pr", "view"]:
            return "[]"
        raise AssertionError(command)

    with pytest.raises(AssertionError):
        fake_run(["unexpected"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(runner.ResumeVerificationError, match="scene_pr_state=unknown"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo="owner/repo", expected_url=FAKE_PR_URL, require_latest_base=False)


def test_verify_pr_resume_keeps_failure_evidence_when_scene_lookup_fails(
    monkeypatch, tmp_path, caplog,
):
    """A failed scene lookup is logged without replacing PR evidence."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def fake_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return FAKE_BRANCH
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "head"
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "pr", "view"]:
            raise RuntimeError("lookup unavailable")
        raise AssertionError(command)

    with pytest.raises(AssertionError):
        fake_run(["unexpected"])
    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(runner.ResumeVerificationError, match="scene_pr_state=unknown"):
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo="owner/repo", expected_url=FAKE_PR_URL, require_latest_base=False)
    assert "resume_scene_pr_state_lookup_failed" in caplog.text


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

    def fake_verify_pr(ctx, base_branch, *, repo_dir=None, pr_repo=None,
                       expected_url=None, require_latest_base=True,
                       external_pr=False):
        calls.append({
            "worktree": ctx.worktree, "branch": ctx.branch,
            "base_branch": base_branch, "run_id": ctx.run_id,
            "issue": ctx.issue,
            "repo_dir": repo_dir,
            "pr_repo": pr_repo, "expected_url": expected_url,
            "require_latest_base": require_latest_base,
        })
        return verified_url

    edits = []

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(seam, "edit_issue", fake_edit)
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
    # The PR-side failure comment carries the hidden runner marker
    # (Issue #526); pin the fingerprint for a deterministic assertion.
    monkeypatch.setattr(progress, "runner_fingerprint", lambda: "8a12fb1c")
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

    monkeypatch.setattr(seam, "edit_issue", fake_edit)
    monkeypatch.setattr(seam, "comment_issue", fake_comment)
    monkeypatch.setattr(seam, "run_command", fake_run)
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
            "**Orbi progress**\n\nawaiting review"
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

    def fake_verify_pr(ctx, base_branch, *, repo_dir=None, pr_repo=None,
                       expected_url=None, require_latest_base=True,
                       external_pr=False):
        calls.append(1)
        return FAKE_PR_URL

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(seam, "edit_issue", fake_edit)
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

    def fake_verify_pr(ctx, base_branch, *, repo_dir=None, pr_repo=None,
                       expected_url=None, require_latest_base=True,
                       external_pr=False):
        verify_calls.append((ctx.worktree, ctx.branch, ctx.run_id))
        return FAKE_PR_URL

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(seam, "edit_issue", fake_edit)
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


def test_verify_resumed_pr_external_scene_reads_branch_from_worktree(
    monkeypatch, tmp_path,
):
    """Issue #608: an external takeover scene resumes the contributor's
    own PR — the delivery branch is read from the derived takeover
    worktree, and verify_pr runs in external mode (no run-marker /
    Fixes body checks) with every other check intact."""
    verify_calls = []
    edits = []

    def fake_verify_pr(ctx, base_branch, *, repo_dir=None, pr_repo=None,
                       expected_url=None, require_latest_base=True,
                       external_pr=False):
        verify_calls.append(
            (ctx.worktree, ctx.branch, expected_url, require_latest_base,
             external_pr),
        )
        return "https://github.com/xqliu/orbi/pull/592"

    def fake_edit(number, *, repo, add=None, remove=None):
        edits.append((number, repo, add, remove))

    # verify_pr is mocked, so the only command the flow issues is the
    # worktree branch read.
    monkeypatch.setattr(seam, "run_command",
        lambda command, **kwargs: "fix/outer",
    )
    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(seam, "edit_issue", fake_edit)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    scene = make_resume_scene("https://github.com/xqliu/orbi/pull/592")
    scene["external"] = "true"
    url = runner.verify_resumed_pr(
        scene, make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    assert url == "https://github.com/xqliu/orbi/pull/592"
    worktree, branch, expected_url, require_latest_base, external_pr = (
        verify_calls[0]
    )
    # The branch is the contributor's head branch (read from the derived
    # worktree), the scene PR URL is the exact anchor, and the body
    # checks are off for the takeover PR.
    assert branch == "fix/outer"
    assert expected_url == "https://github.com/xqliu/orbi/pull/592"
    assert require_latest_base is False
    assert external_pr is True
    # The in-flight backfill still applies (the resumed delivery is in
    # flight from the verified PR on).
    assert edits == [(9, "owner/repo", "ai-in-progress", None)]


def test_verify_resumed_pr_recreates_missing_worktree_from_remote_branch(
    monkeypatch, tmp_path,
):
    """Issue #807: the worktree is a local cache of the remote delivery
    state (the branch and the PR live on GitHub), so a missing
    directory is recreated from the remote branch and the resume
    continues — the recovery the #90/#50 comment promised but the code
    never performed (the beta incident: every tick re-emitted the
    identical `worktree missing` failure and the delivery never
    advanced). The REAL verify_pr runs on the rebuilt worktree."""
    fake_git = FakeGit(tmp_path, base_branch="main")
    fake_gh = FakeGh("owner/repo")
    fake_gh.add_issue(9, title="ship")
    local_head = fake_git.commit([fake_git.base_sha])
    remote_head = fake_git.commit([local_head])
    # Reclamation removed the worktree but deliberately left the local
    # delivery branch behind. The remote advanced while that cache was gone.
    fake_git.branch(FAKE_BRANCH, local_head)
    fake_git.origin[FAKE_BRANCH] = remote_head
    fake_gh.add_pr(
        9, head=FAKE_BRANCH, base="main", oid=remote_head, url=FAKE_PR_URL,
        body=f"{run_marker_body()}\n\nFixes #9\n\nPlan",
    )

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return fake_git(command, **kwargs)
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    url = runner.verify_resumed_pr(
        make_resume_scene(), make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    assert url == FAKE_PR_URL
    # The worktree was REBUILT from the remote branch: the derived path
    # exists again, carries the branch and sits at the origin head.
    worktree = expected_resume_worktree(tmp_path)
    assert worktree.is_dir()
    assert fake_git.worktrees[str(worktree)]["branch"] == FAKE_BRANCH
    assert fake_git.worktrees[str(worktree)]["head"] == remote_head
    assert fake_git.local[FAKE_BRANCH] == remote_head
    # The REAL verify_pr compared this rebuilt HEAD to the PR head, so the
    # successful return proves the resumed user path no longer diverges.
    assert "ai-in-progress" in fake_gh.issues[9]["labels"]
    assert "ai-pr-opened" not in fake_gh.issues[9]["labels"]


def test_verify_resumed_pr_preserves_diverged_local_branch_with_evidence(
    monkeypatch, tmp_path,
):
    """A reclaimed diverged branch fails before checkout and identifies
    the local cache precisely, so a maintainer can preserve or resolve it."""
    fake_git = FakeGit(tmp_path, base_branch="main")
    fake_gh = FakeGh("owner/repo")
    fake_gh.add_issue(9, title="ship", labels=("ai-pr-opened",))
    local_head = fake_git.commit([fake_git.base_sha])
    remote_head = fake_git.commit([fake_git.base_sha])
    fake_git.branch(FAKE_BRANCH, local_head)
    fake_git.origin[FAKE_BRANCH] = remote_head
    fake_gh.add_pr(
        9, head=FAKE_BRANCH, base="main", oid=remote_head, url=FAKE_PR_URL,
        body=f"{run_marker_body()}\n\nFixes #9\n\nPlan",
    )

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return fake_git(command, **kwargs)
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    monkeypatch.setattr(gitops.socket, "gethostname", lambda: "runner-box")
    with pytest.raises(RuntimeError) as excinfo:
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )

    message = str(excinfo.value)
    assert FAKE_BRANCH in message
    assert f"local={local_head}" in message
    assert f"remote={remote_head}" in message
    assert f"checkout {tmp_path}" in message
    assert "host runner-box" in message
    assert fake_git.local[FAKE_BRANCH] == local_head
    assert not expected_resume_worktree(tmp_path).exists()


def test_verify_resumed_pr_branch_gone_from_origin_is_terminal(
    monkeypatch, tmp_path,
):
    """Issue #807: when the delivery branch is gone from the remote
    there is nothing to recreate the worktree from — an external
    precondition (the remote delivery state itself is destroyed): the
    Issue is ai-blocked ALONE with the explicit reason, never the fix
    loop that used to re-emit the identical failure every tick."""
    fake_git = FakeGit(tmp_path, base_branch="main")
    fake_gh = FakeGh("owner/repo")
    fake_gh.add_issue(9, title="ship", labels=("ai-pr-opened",))
    # No delivery branch on origin and no worktree: nothing to resume.

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return fake_git(command, **kwargs)
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(
        runner.UnrecoverableDeliveryError,
        match="no longer exists on origin",
    ) as excinfo:
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert type(excinfo.value) is runner.ResumeBranchGoneError
    labels = fake_gh.issues[9]["labels"]
    assert "ai-blocked" in labels
    assert "ai-pr-opened" not in labels
    body = fake_gh.issues[9]["comments"][-1]["body"]
    assert "Orbi failed:" in body
    assert f"delivery branch {FAKE_BRANCH} no longer exists" in body
    assert "worktree: `local runner worktree`" in body
    # The blocked comment states why automatic recovery is impossible.
    assert "cannot be recovered automatically" in body
    # Nothing is left to preserve: the preserved-objects suffix of the
    # other blocked scenes would contradict the reason.
    assert "are preserved" not in body


def test_verify_resumed_pr_external_scene_recreates_worktree_from_scene_pr(
    monkeypatch, tmp_path,
):
    """Issue #807 + #608: an external takeover resumes the contributor's
    own head branch — a fact the gone worktree can no longer carry, so
    the scene PR is the remaining authority (headRefName) and the
    worktree is rebuilt from that branch on origin."""
    fake_git = FakeGit(tmp_path, base_branch="main")
    fake_gh = FakeGh("owner/repo")
    fake_gh.add_issue(9, title="ship")
    external_head = fake_git.commit([fake_git.base_sha])
    fake_git.pull_heads["592"] = external_head
    external_url = "https://github.com/xqliu/orbi/pull/592"
    fake_gh.add_pr(
        592, head="fix/outer", base="main", oid=external_head,
        url=external_url,
    )

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return fake_git(command, **kwargs)
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    scene = make_resume_scene(external_url)
    scene["external"] = "true"
    url = runner.verify_resumed_pr(
        scene, make_resume_issue(),
        make_resume_config(tmp_path), "owner/repo",
    )
    assert url == external_url
    # The rebuilt worktree carries the contributor's head branch (read
    # from the scene PR) at the fetched origin head.
    worktree = expected_resume_worktree(tmp_path)
    assert worktree.is_dir()
    assert fake_git.worktrees[str(worktree)]["branch"] == "fix/outer"
    assert fake_git.worktrees[str(worktree)]["head"] == external_head


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
    monkeypatch.setattr(seam, "edit_issue", failing_edit)
    reviews: list = []
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    assert "Orbi needs a fix:" in body
    assert run_marker_body() in body
    assert "rate limited" in body
    # ... written to the Issue AND the PR (Issue #50); the PR copy is
    # the same formatted comment and additionally carries the hidden
    # runner fingerprint (Issue #526) ...
    assert len(captured["pr_comments"]) == 1
    assert captured["pr_comments"][0].startswith(run_marker_body())
    assert captured["pr_comments"][0].endswith("<!-- runner=8a12fb1c -->")
    # ... and the fix-needed milestone.
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted)
    assert any("rate limited" in body for body in posted)
    assert "resume_pr_verification_failed" in caplog.text


def test_verify_resumed_pr_missing_fixes_stays_fix_needed(
    monkeypatch, tmp_path, caplog,
):
    """Issue #1219 + #50: a staged PR body without `Fixes #N` is
    recoverably reported with an actionable repair. The Issue remains
    `ai-fix-needed`, the error is re-raised for the tick boundary, and
    the runner process can continue with other deliveries."""
    existing = {
        "id": 77,
        "body": (
            f"{run_marker_body()}\n\n"
            "**Orbi progress**\n\nawaiting review"
        ),
    }
    captured, _ = make_resume_failure_fake(
        monkeypatch, progress_comments=[existing],
    )

    def fake_verify_pr(*args, **kwargs):
        raise runner.MissingFixesError(
            "PR body is missing `Fixes #9`; the keyword must point at the "
            "source Issue so GitHub closes it natively when the PR merges"
        )

    reviews: list = []
    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    monkeypatch.setattr(
        runner, "review_and_merge_if_clean",
        lambda *args, **kwargs: reviews.append((args, kwargs)) or False,
    )
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(
        runner.ReportedMissingFixesError,
        match="missing `Fixes #9`",
    ):
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
    assert "Orbi needs a fix:" in body
    assert run_marker_body() in body
    assert "missing `Fixes #9`" in body
    assert "Update PR https://github.com/owner/repo/pull/99" in body
    assert "split the remaining phases into child Issues" in body
    # ... written to the Issue AND the PR (Issue #50); the PR copy is
    # the same formatted comment and additionally carries the hidden
    # runner fingerprint (Issue #526) ...
    assert len(captured["pr_comments"]) == 1
    assert captured["pr_comments"][0].startswith(run_marker_body())
    assert captured["pr_comments"][0].endswith("<!-- runner=8a12fb1c -->")
    # ... and the fix-needed milestone.
    posted = [
        command[command.index("--field") + 1][len("body="):]
        for command in captured["api"]
        if "--method" in command and "POST" in command
    ]
    assert any("Orbi: fix needed" in body for body in posted)
    assert any("missing `Fixes #9`" in body for body in posted)
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
    assert "Orbi fix needed" in fix_needed
    assert "What you need to do: Update PR" in fix_needed
    assert "resume_pr_verification_failed" in caplog.text


def _closed_scene_gh(monkeypatch):
    """The FakeGh wired into the ONE subprocess seam beside the two
    local git reads the resume verification makes (the branch and HEAD
    of the derived worktree). The git dispatch indexes single argv
    elements — no argv-shape asserts (Issue #789)."""
    fake = FakeGh("owner/repo")
    git_reads = {"branch": FAKE_BRANCH, "rev-parse": "head"}

    def run(command, **kwargs):
        if command[0] == "git":
            return git_reads[command[1]]
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run)
    return fake


def _closed_scene(external: bool, pr_url: str = FAKE_PR_URL) -> dict:
    scene = make_resume_scene(pr_url=pr_url)
    if external:
        scene["external"] = "true"
    return scene


def test_verify_pr_zero_open_prs_raises_typed_closed_scene_error(
    monkeypatch, tmp_path,
):
    """Issue #788: the zero-open-PR scene carries the scene PR's GitHub
    state on a TYPED error (`ResumePrClosedError`, still an
    `UnrecoverableDeliveryError`), so the resume handler can route the
    already-decided fact (delivered / withdrawn) instead of blocking
    every closed or merged scene alike."""
    fake = _closed_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-pr-opened",))
    fake.add_pr(9, head=FAKE_BRANCH, state="MERGED",
                merged_at="2026-09-13T00:00:00Z")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(runner.ResumePrClosedError) as excinfo:
        runner.verify_pr(RunContext(run_id=FAKE_RUN_ID, issue=9, branch=FAKE_BRANCH, worktree=worktree, source_repo="owner/repo"), "main", repo_dir=tmp_path, pr_repo="owner/repo", expected_url=FAKE_PR_URL, require_latest_base=False)
    assert excinfo.value.scene_pr_state == "MERGED"
    assert isinstance(excinfo.value, runner.UnrecoverableDeliveryError)
    assert "scene_pr_state=MERGED" in str(excinfo.value)


def test_verify_resumed_pr_scene_pr_merged_between_ticks_does_not_block(
    monkeypatch, tmp_path, caplog,
):
    """Issue #788: the resumed path IS the path, so a scene PR merged
    between ticks is a NORMAL state, not a crash leftover. The `Fixes
    #N` keyword closed the Issue natively when the merge landed — the
    runner journals the fact and stops; blocking a DELIVERED Issue as
    `ai-blocked` (a human must decide) writes noise on a decided fact
    and is gone."""
    fake = _closed_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-pr-opened",))
    fake.add_pr(9, head=FAKE_BRANCH, state="MERGED",
                merged_at="2026-09-13T00:00:00Z")
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(runner.ResumePrClosedError):
        runner.verify_resumed_pr(
            _closed_scene(external=False), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # No label change, no comment: the native Fixes #N close owns the
    # delivered Issue; only the journal records the observation.
    assert fake.issues[9]["labels"] == ["ai-pr-opened"]
    assert fake.issues[9]["comments"] == []
    assert fake.prs[9]["comments"] == []
    assert "delivery_merged" in caplog.text


def test_verify_resumed_pr_external_pr_closed_between_ticks_requeues(
    monkeypatch, tmp_path,
):
    """Issue #608 + #788: the takeover delivery's PR was closed without
    a merge between ticks (the contributor withdrew it). The requeue —
    `ai-ready` for an internal redo, the supersession explained on the
    closed PR thread — was the old wait loop's in-process branch; the
    resume path is now the ONLY path, so the verify seam must keep it
    reachable instead of blocking the triage Issue."""
    fake = _closed_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-pr-opened",))
    fake.add_pr(592, head=FAKE_BRANCH, state="CLOSED")
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    external_url = "https://github.com/owner/repo/pull/592"
    with pytest.raises(runner.ResumePrClosedError):
        runner.verify_resumed_pr(
            _closed_scene(external=True, pr_url=external_url),
            make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The requeue: the triage Issue returns to the ready queue.
    assert fake.issues[9]["labels"] == ["ai-ready"]
    # The supersession story on the triage Issue ...
    assert len(fake.issues[9]["comments"]) == 1
    body = fake.issues[9]["comments"][0]["body"]
    assert "closed without" in body and "internal" in body
    assert run_marker_body() in body
    # ... and on the closed PR thread the contributor watches.
    assert len(fake.prs[592]["comments"]) == 1
    assert "closed without" in fake.prs[592]["comments"][0]["body"]


def test_verify_resumed_pr_external_pr_merged_between_ticks_closes_triage(
    monkeypatch, tmp_path,
):
    """Issue #608/#726 + #788: the takeover delivery's PR was MERGED
    between ticks (a maintainer clicked merge). The contribution
    delivered the fix — the triage Issue closes as delivered, exactly
    like the merged-poll branch; blocking it as `ai-blocked` would deny
    a landed contribution."""
    fake = _closed_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-pr-opened",))
    fake.add_pr(592, head=FAKE_BRANCH, state="MERGED",
                merged_at="2026-09-13T00:00:00Z")
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    external_url = "https://github.com/owner/repo/pull/592"
    with pytest.raises(runner.ResumePrClosedError):
        runner.verify_resumed_pr(
            _closed_scene(external=True, pr_url=external_url),
            make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    # The triage Issue closes as delivered — never requeued, never
    # blocked.
    assert fake.issues[9]["labels"] == ["ai-pr-opened"]
    assert fake.issues[9]["state"] == "closed"
    assert len(fake.issues[9]["comments"]) == 1
    assert "closing" in fake.issues[9]["comments"][0]["body"]
    assert "triage Issue" in fake.issues[9]["comments"][0]["body"]


def test_verify_resumed_pr_scene_pr_closed_between_ticks_still_blocks(
    monkeypatch, tmp_path, caplog,
):
    """Issue #494 + #788: an INTERNAL delivery whose PR was closed
    without a merge between ticks keeps its terminal contract —
    `ai-blocked` ALONE with the explicit reason (a human decides
    whether to reopen or start a fresh delivery)."""
    captured, reporting_fake = make_resume_failure_fake(monkeypatch)
    git_reads = {"branch": FAKE_BRANCH, "rev-parse": "head"}
    pr_reads = {
        "list": "[]",
        "view": json.dumps({"state": "CLOSED", "mergedAt": None}),
    }

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return git_reads[command[1]]
        if command[1] == "pr":
            return pr_reads[command[2]]
        return reporting_fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", fake_run)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    caplog.set_level("INFO")
    with pytest.raises(runner.UnrecoverableDeliveryError):
        runner.verify_resumed_pr(
            _closed_scene(external=False), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-blocked",
                "remove": "ai-pr-opened"}),
    ]
    assert len(captured["comments"]) == 1
    assert "Orbi failed:" in captured["comments"][0][1]["body"]
    assert "scene_pr_state=CLOSED" in captured["comments"][0][1]["body"]
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
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    assert "Orbi needs a fix:" in body
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
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    issue = make_resume_issue()
    issue["labels"] = [{"name": "ai-fix-needed"}]
    with pytest.raises(RuntimeError, match="exactly one open PR"):
        runner.verify_resumed_pr(
            make_resume_scene(), issue, make_resume_config(tmp_path), "owner/repo",
        )
    # The current label set already carries ai-fix-needed, so the
    # transition does not invent a remove for absent ai-pr-opened.
    assert captured["edits"] == [
        ((9,), {"repo": "owner/repo", "add": "ai-fix-needed"}),
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
    monkeypatch.setattr(seam, "run_command", counting)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
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
    assert "Orbi failed:" in body
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


def test_verify_resumed_pr_reraises_when_failure_reporting_fails(
    monkeypatch, caplog, tmp_path,
):
    """When the blocked transition itself cannot be reported, the
    original verification error is still re-raised (the tick still
    stops)."""

    def fake_verify_pr(*args, **kwargs):
        raise runner.MissingFixesError("the original verification failure")

    monkeypatch.setattr(runner, "verify_pr", fake_verify_pr)
    make_resume_failure_fake(monkeypatch)

    def broken_edit(*args, **kwargs):
        raise RuntimeError("github edit failed")

    monkeypatch.setattr(seam, "edit_issue", broken_edit)
    expected_resume_worktree(tmp_path).mkdir(parents=True)
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", FAKE_RUN_ID)
    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="the original verification failure",
    ) as excinfo:
        runner.verify_resumed_pr(
            make_resume_scene(), make_resume_issue(),
            make_resume_config(tmp_path), "owner/repo",
        )
    assert type(excinfo.value) is runner.MissingFixesError
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
    assert "Orbi needs a fix:" in body
    # No run id: no marker, no milestone, no fix-needed progress scene
    # — the only gh traffic is the label read of the leftover-label
    # check and the PR failure comment (no progress API at all).
    assert run_marker_body() not in body
    assert captured["api"] == []
    assert len(captured["pr_comments"]) == 1
    assert "resume_pr_verification_failed" in caplog.text


def run_marker_body() -> str:
    return f"<!-- orbi:run={FAKE_RUN_ID} -->"


# ------------------------------------------------- delivery-line markers (#825)

CREATING_RUN_ID = "0e4b1923"
RESUME_RUN_ID = "be3c1434"
FOREIGN_RUN_ID = "deadbeef"


def _resume_line_gh(monkeypatch, *, issue_comments, pr_body):
    """FakeGh wired for the resume-mode `verify_pr`: one open PR whose
    body is `pr_body`, the Issue comment history `issue_comments`, and
    the three local git reads the verification makes (branch, HEAD and
    the ancestor probe). Returns `(fake, run)` — the FakeGh state and
    the dispatch (the fake-coverage convention: the rejecting branch is
    exercisable)."""
    fake = FakeGh("owner/repo")
    fake.add_issue(9, labels=("ai-fix-needed",))
    for body in issue_comments:
        fake.comment(9, body)
    fake.add_pr(9, head=FAKE_BRANCH, body=pr_body, url=FAKE_PR_URL)

    def run(command, **kwargs):
        if command[0] == "git":
            if command[1] == "branch":
                return FAKE_BRANCH
            if command[1] == "rev-parse":
                return "head"
            if command[1] == "merge-base":
                return ""
            raise AssertionError(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run)
    return fake, run


def test_verify_pr_resume_accepts_a_marker_of_the_delivery_line(
        monkeypatch, tmp_path):
    """Issue #825 (defect 1, the root cause): the PR body is written
    ONCE, by the run that created the PR. A resume binds a NEW run id
    (one GitHub 502 is enough), so requiring the body to carry the
    CURRENT attempt's marker made every later tick of the line fail
    with `resume_pr_verification_failed` forever — the orbi-cloud#360
    scene (82 identical failures). The resume path accepts ANY run
    marker of the delivery line: the marker set is read from the
    Issue's TRUSTED comment history when the current marker misses."""
    _resume_line_gh(
        monkeypatch,
        issue_comments=[
            (
                f"<!-- orbi:run={CREATING_RUN_ID} -->\n"
                f"Orbi opened PR: {FAKE_PR_URL}"
            ),
        ],
        pr_body=(
            f"<!-- orbi:run={CREATING_RUN_ID} -->\n\n"
            "Fixes #9\n\nPlan"
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    url = runner.verify_pr(
        RunContext(run_id=RESUME_RUN_ID, issue=9, branch=FAKE_BRANCH,
               worktree=worktree, source_repo="owner/repo"),
        "main", repo_dir=tmp_path, pr_repo="owner/repo",
        expected_url=FAKE_PR_URL, require_latest_base=False,
    )
    assert url == FAKE_PR_URL


def test_verify_resumed_pr_resume_under_a_new_run_id_continues(
        monkeypatch, tmp_path):
    """Issue #825: the full user path — a delivery line whose scene
    carries the NEW run id (the re-delivery wrote a new opened-PR scene
    for the SAME PR) resumes successfully: the PR body carries the
    creating run's marker, the trusted comments carry both, and the
    review continues instead of dead-looping."""
    fake = FakeGh("owner/repo")
    fake.add_issue(9, labels=("ai-fix-needed",))
    fake.comment(
        9,
        f"<!-- orbi:run={CREATING_RUN_ID} -->\n"
        f"Orbi opened PR: {FAKE_PR_URL} (base_branch=main "
        f"base_sha=abc123def456 run_id={CREATING_RUN_ID})",
    )
    fake.comment(
        9,
        f"<!-- orbi:run={RESUME_RUN_ID} -->\n"
        f"Orbi opened PR: {FAKE_PR_URL} (base_branch=main "
        f"base_sha=abc123def456 run_id={RESUME_RUN_ID})",
    )
    fake.add_pr(
        9, head=FAKE_BRANCH,
        body=(
            f"<!-- orbi:run={CREATING_RUN_ID} -->\n\n"
            "Fixes #9\n\nPlan"
        ),
        url=FAKE_PR_URL,
    )

    def run(command, **kwargs):
        if command[0] == "git":
            if command[1] == "branch":
                return FAKE_BRANCH
            if command[1] == "rev-parse":
                return "head"
            if command[1] == "merge-base":
                return ""
            raise AssertionError(command)
        return fake(command, **kwargs)

    # The rejecting branch of the dispatch is reachable (the
    # fake-coverage convention).
    with pytest.raises(AssertionError):
        run(["git", "status"])
    monkeypatch.setattr(seam, "run_command", run)
    edits = []
    monkeypatch.setattr(seam, "edit_issue",
                        lambda *args, **kwargs: edits.append((args, kwargs)))
    # The worktree is derived from the SCENE's (new) run id.
    runner.worktree_path(
        tmp_path, "owner/repo", 9, RESUME_RUN_ID,
    ).mkdir(parents=True)
    scene = make_resume_scene()
    scene["run_id"] = RESUME_RUN_ID
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", RESUME_RUN_ID)
    url = runner.verify_resumed_pr(
        scene, make_resume_issue(), make_resume_config(tmp_path),
        "owner/repo",
    )
    assert url == FAKE_PR_URL
    # The in-flight backfill still lands after the verification.
    assert edits == [
        ((9,), {"repo": "owner/repo", "add": "ai-in-progress"}),
    ]


def test_verify_pr_resume_still_rejects_a_foreign_line_marker(
        monkeypatch, tmp_path):
    """Issue #825 security regression (#45/#89): a PR body carrying
    ONLY another delivery line's marker stays rejected — acceptance of
    the creating run's marker never widens to markers the Issue's
    trusted history does not know."""
    _resume_line_gh(
        monkeypatch,
        issue_comments=[
            (
                f"<!-- orbi:run={CREATING_RUN_ID} -->\n"
                f"Orbi opened PR: {FAKE_PR_URL}"
            ),
        ],
        pr_body=(
            f"<!-- orbi:run={FOREIGN_RUN_ID} -->\n\n"
            "Fixes #9\n\nPlan"
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(RuntimeError, match="stable run marker"):
        runner.verify_pr(
            RunContext(run_id=RESUME_RUN_ID, issue=9, branch=FAKE_BRANCH,
                   worktree=worktree, source_repo="owner/repo"),
            "main", repo_dir=tmp_path, pr_repo="owner/repo",
            expected_url=FAKE_PR_URL, require_latest_base=False,
        )


def test_verify_pr_resume_ignores_public_comment_markers(
        monkeypatch, tmp_path):
    """Issue #825 security regression (#45/#89): the line marker set is
    built from TRUSTED comments only — a public comment claiming a run
    marker (with a PR body prepared to match it) must not steer the
    resume into an unverified PR."""
    fake, run = _resume_line_gh(
        monkeypatch,
        issue_comments=[
            (
                f"<!-- orbi:run={CREATING_RUN_ID} -->\n"
                f"Orbi opened PR: {FAKE_PR_URL}"
            ),
        ],
        pr_body=(
            f"<!-- orbi:run={FOREIGN_RUN_ID} -->\n\n"
            "Fixes #9\n\nPlan"
        ),
    )
    # The rejecting branch of the dispatch is reachable (the
    # fake-coverage convention).
    with pytest.raises(AssertionError):
        run(["git", "status"])
    fake.issues[9]["comments"].append({
        "author": {"login": "stranger"},
        "authorAssociation": "NONE",
        "createdAt": "2026-09-13T00:00:00Z",
        "body": f"<!-- orbi:run={FOREIGN_RUN_ID} --> resume here please",
    })
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(RuntimeError, match="stable run marker"):
        runner.verify_pr(
            RunContext(run_id=RESUME_RUN_ID, issue=9, branch=FAKE_BRANCH,
                   worktree=worktree, source_repo="owner/repo"),
            "main", repo_dir=tmp_path, pr_repo="owner/repo",
            expected_url=FAKE_PR_URL, require_latest_base=False,
        )


CREATING_RUN_ID = "675f38a0"
RECLAIM_RUN_ID = "f70987dd"


def _deliver_scene_gh(monkeypatch):
    """FakeGh at the ONE subprocess seam, beside the two local git reads
    the deliver-path verification makes. The git dispatch indexes single
    argv elements — no argv-shape asserts (Issue #789)."""
    fake = FakeGh("owner/repo")
    git_reads = {"branch": FAKE_BRANCH, "rev-parse": "head"}

    def run(command, **kwargs):
        if command[0] == "git":
            return git_reads[command[1]]
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run)
    return fake


def _deliver_verify(worktree, tmp_path):
    """The REAL deliver-path call (runner.py `deliver_pr`'s closeout).

    `deliver_pr` passes neither `pr_repo` nor `expected_url`, so the
    marker source cannot depend on `pr_repo` — the repo the delivery
    already knows is its own source repo. A test that hands `pr_repo`
    in verifies a call shape production never makes (Issue #1300).
    """
    return runner.verify_pr(
        RunContext(run_id=RECLAIM_RUN_ID, issue=9, branch=FAKE_BRANCH,
                   worktree=worktree, source_repo="owner/repo"),
        "main", repo_dir=tmp_path, require_latest_base=False,
    )


def test_deliver_path_accepts_the_creating_runs_marker_on_an_existing_pr(
    monkeypatch, tmp_path,
):
    """Issue #1300: a re-claim must not reject the PR it already owns.

    The deliver path creates a PR only when none is open, but verifies
    UNCONDITIONALLY. When an earlier run of the SAME Issue already opened the
    PR, its body carries the CREATING run's marker while the current run has a
    new id, so the strict current-attempt check rejected a healthy delivery and
    the Issue fell to `ai-blocked`. Production #914: the PR body carried
    `orbi:run=675f38a0` while the re-claim ran as `f70987dd`.

    The delivery line's identity is the SET of run ids the Issue's trusted
    comments carry (Issue #825) — the same source the resume path already
    consults.
    """
    fake = _deliver_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-ready",))
    fake.add_pr(9, head=FAKE_BRANCH, oid="head", body=(
        f"{runner.run_marker(CREATING_RUN_ID)}\n\nFixes #9\n"
    ))
    fake.comment(9, f"Orbi opened PR: {runner.run_marker(CREATING_RUN_ID)}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert _deliver_verify(worktree, tmp_path) == FAKE_PR_URL


def test_deliver_path_still_rejects_a_marker_no_trusted_comment_knows(
    monkeypatch, tmp_path,
):
    """Issue #1300: widening to the delivery line must not accept ANY PR.

    A body whose marker belongs to no run of this Issue's trusted history is
    still a foreign PR — otherwise the check would degrade into "any
    marker-shaped string passes".
    """
    fake = _deliver_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-ready",))
    fake.add_pr(9, head=FAKE_BRANCH, oid="head", body=(
        f"{runner.run_marker('deadbeef')}\n\nFixes #9\n"
    ))
    fake.comment(9, f"Orbi opened PR: {runner.run_marker(CREATING_RUN_ID)}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(RuntimeError, match="missing the stable run marker"):
        _deliver_verify(worktree, tmp_path)


def test_deliver_path_rejects_a_marker_only_an_untrusted_comment_carries(
    monkeypatch, tmp_path,
):
    """Issue #1300: the #45/#89 posture holds — a copied marker in a public
    comment widens nothing, on the deliver path too."""
    fake = _deliver_scene_gh(monkeypatch)
    fake.add_issue(9, labels=("ai-ready",))
    fake.add_pr(9, head=FAKE_BRANCH, oid="head", body=(
        f"{runner.run_marker('deadbeef')}\n\nFixes #9\n"
    ))
    fake.comment(9, f"Orbi opened PR: {runner.run_marker('deadbeef')}",
                 login="drive-by", association="NONE")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(RuntimeError, match="missing the stable run marker"):
        _deliver_verify(worktree, tmp_path)


def test_deliver_closeout_continues_on_the_creating_runs_existing_pr(
    monkeypatch, tmp_path,
):
    """Issue #1300, the real user path: the `deliver_pr` closeout finds
    the open PR an EARLIER run of this Issue created, skips creation and
    returns the verified URL — the delivery continues to review instead
    of stopping at `ai-blocked` with
    "PR body is missing the stable run marker ...".

    The closeout is driven for real (one FakeGh at the subprocess seam
    plus the local git reads it makes); only `verify_pr`'s own call
    shape is what production uses — no `pr_repo`, no `expected_url`.
    """
    fake = FakeGh("owner/repo")
    fake.add_issue(9, labels=("ai-ready",), state="open")
    fake.add_pr(9, head=FAKE_BRANCH, oid="head", body=(
        f"{runner.run_marker(CREATING_RUN_ID)}\n\nFixes #9\n"
    ))
    fake.comment(9, f"Orbi opened PR: {runner.run_marker(CREATING_RUN_ID)}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_reads = {
        "status": "", "rev-parse": "head", "fetch": "",
        "merge-base": "", "push": "", "branch": FAKE_BRANCH,
        # Issue #898: the pushed head is resolved with ls-remote.
        "ls-remote": f"head\trefs/heads/{FAKE_BRANCH}\n",
    }

    def run(command, **kwargs):
        if command[0] == "git":
            return git_reads[command[1]]
        return fake(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run)
    ctx = RunContext(run_id=RECLAIM_RUN_ID, issue=9, branch=FAKE_BRANCH,
                     worktree=worktree, source_repo="owner/repo")
    assert runner.deliver_pr(
        ctx, "main", "c0000", issue_title="t", repo_dir=tmp_path,
    ) == FAKE_PR_URL
