"""Reader contracts for the display-only comment wrappers."""
import re
from pathlib import Path

import pytest

from orbi import progress, scene
from orbi.delivery_scene import RunContext
from orbi.runner import (
    merged_pr_comment_body,
    opened_pr_comment_body,
    started_pi_comment_body,
)

RUN_ID = "abc12345"
PR_URL = "https://github.com/owner/repo/pull/42"


def _unwrap_details(body: str) -> str:
    body = re.sub(
        r"\n\n<details><summary>Run details</summary>\n\n(.*?)\n\n</details>",
        r"\n\1",
        body,
        flags=re.S,
    )
    return re.sub(r"\n- review: [^\n]+", "", body)


def _milestone() -> str:
    posted = []
    publisher = progress.ProgressPublisher(
        1153, "owner/repo", RUN_ID, lambda *args, **kwargs: "",
    )
    publisher._post_comment = lambda body: posted.append(body) or 1
    publisher.milestone(
        f"merged: {PR_URL} (merge_commit=deadbeef review_rounds=1 "
        "external_commits=0 commits=1)",
    )
    return posted[0]


def _comments():
    context = RunContext(
        run_id=RUN_ID, issue=1153, branch="orbi/issue-1153",
        worktree=Path("/tmp/worktree"), source_repo="owner/repo",
    )
    state = {
        "run_id": RUN_ID, "issue": 1153, "issue_title": "Comment display",
        "role": "review", "priority": "normal", "phase": "done",
        "elapsed": "1s", "last_activity": "-", "last_action": "-",
        "tests": "10 passed", "review_round": 1,
        "review": "pass, no findings", "branch": context.branch,
        "pr": PR_URL, "session": "session-1",
    }
    return {
        "started": started_pi_comment_body(
            context,
            "base_branch=main base_sha=abc123 repo_config=cfg "
            "run_id=abc12345 priority=normal session=session-1",
        ),
        "progress": progress.progress_body(state),
        "opened": opened_pr_comment_body(
            RUN_ID, "base_branch=main base_sha=abc123 run_id=abc12345",
            PR_URL,
        ),
        "merged milestone": _milestone(),
        "merged PR": merged_pr_comment_body(
            RUN_ID, PR_URL, "deadbeef", 1, 0, 1, "main",
            "pass, no findings",
        ),
    }


@pytest.mark.parametrize("kind", _comments())
def test_old_and_wrapped_comments_preserve_every_reader_contract(kind):
    rendered = _comments()[kind]
    legacy = _unwrap_details(rendered)

    assert scene.parse(rendered) == scene.parse(legacy)
    for body in (legacy, rendered):
        found = progress.find_progress_comment([{"id": 1, "body": body}], RUN_ID)
        assert (found is not None) == (kind == "progress")
        assert re.search(r"(?:^|\s)run_id=([^\s]+)", body)

    rendered_status = progress.format_status_comment(rendered).splitlines()
    legacy_status = progress.format_status_comment(legacy).splitlines()
    assert rendered_status[:2] == legacy_status[:2]

    cloud_patterns = (
        r"Orbi merged PR:\s+https://[^\s/]+/[^\s/]+/[^\s/]+/pull/(\d+)",
        r"(?:^|\s)run_id=([^\s]+)",
        r"(?:^|\s)external_commits=(-?\d+)",
    )
    assert [re.search(pattern, rendered) is not None for pattern in cloud_patterns] == [
        re.search(pattern, legacy) is not None for pattern in cloud_patterns
    ]


def test_five_happy_path_comments_hide_debug_rows_and_show_review():
    comments = _comments()
    assert len(comments) == 5
    debug_rows = (
        "base_sha", "repo_config", "branch", "worktree", "session",
        "merge_commit",
    )
    for body in comments.values():
        visible, _, details = body.partition(
            "<details><summary>Run details</summary>",
        )
        for key in debug_rows:
            assert f"- {key}:" not in visible
            if f"- {key}:" in body:
                assert f"- {key}:" in details

    assert "- review: pass, no findings" in comments["progress"]
    assert "\nreview: pass, no findings\n" in comments["merged PR"]
    # The result users care about remains visible; only the merge hash row moves.
    milestone_visible, milestone_details = comments["merged milestone"].split(
        "<details><summary>Run details</summary>", 1,
    )
    assert f"- result: {PR_URL}" in milestone_visible
    assert "- merge_commit: deadbeef" in milestone_details
