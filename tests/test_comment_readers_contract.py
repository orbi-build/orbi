"""Reader contracts for the display-only comment wrappers."""
import re
from pathlib import Path

import pytest

from orbi import progress, scene
from orbi.delivery_scene import RunContext
from orbi.review_merge import merged_pr_comment_body
from orbi.runner import (
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


def _posted_merged(**overrides) -> str:
    """The merged record exactly as it is posted (through the
    formatter that `comment_issue` applies before the `gh` call)."""
    kwargs = {
        "run_id": RUN_ID, "pr_url": PR_URL, "merge_commit": "deadbeef",
        "review_rounds": 1, "external_commits": 0, "commits": 1,
        "base_branch": "main", "review": "pass, no findings",
    }
    kwargs.update(overrides)
    return progress.format_status_comment(merged_pr_comment_body(**kwargs))


def _legacy_merged_one_liner() -> str:
    """The pre-#1431 one-line merged body kept for old callers."""
    return (
        f"{progress.run_marker(RUN_ID)}\n"
        f"Orbi merged PR: {PR_URL} (merge_commit=deadbeef "
        f"review_rounds=1 external_commits=0 commits=1 "
        f"base_branch=main run_id={RUN_ID})\n"
        "review: pass, no findings\n\n"
        "<details><summary>Run details</summary>\n\n"
        "- merge_commit: deadbeef\n- base_branch: main\n\n</details>"
    )


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
    # The new merged body is not a wrapper of a one-liner anymore: its
    # fold rows are the machine contract, so the legacy reader contract is
    # pinned against the pre-#1431 one-line merged input directly.
    legacy = (
        _legacy_merged_one_liner() if kind == "merged PR"
        else _unwrap_details(rendered)
    )

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


def test_posted_merged_comment_is_the_exact_final_shape():
    posted = _posted_merged()
    expected = (
        f"{progress.run_marker(RUN_ID)}\n"
        f"Orbi merged PR: {PR_URL}\n"
        "- review: pass, no findings\n"
        "- merged into: main\n\n"
        "<details><summary>Run details</summary>\n\n"
        "- merge_commit=deadbeef\n"
        "- review_rounds=1\n"
        "- commits=1\n"
        "- external_commits=0\n"
        f"- run_id={RUN_ID}\n\n"
        "</details>\n\n"
    )
    assert re.fullmatch(re.escape(expected) + r"<!-- runner=[^>]+ -->", posted)

    visible, _, _ = posted.partition("<details>")
    for token in ("merge_commit", "review_rounds", "external_commits",
                  "commits", "run_id", "detail:"):
        assert token not in visible
    assert "- review: pass, no findings" in visible
    assert "- merged into: main" in visible

    for row in ("- merge_commit=", "- review_rounds=", "- commits=",
                "- external_commits=", f"- run_id={RUN_ID}"):
        assert len(re.findall(rf"(?m)^{re.escape(row)}", posted)) == 1
    assert posted.count("<details><summary>Run details</summary>") == 1


def test_posted_merged_comment_keeps_the_cloud_reader_contract():
    # The regexes orbi-cloud's mergeCommentFields applies to the posted
    # body (ported, not imported).
    posted = _posted_merged()
    assert re.search(
        r"Orbi merged PR:\s+https://[^\s/]+/[^\s/]+/[^\s/]+/pull/(\d+)",
        posted,
    ).group(1) == "42"
    assert re.search(r"(?:^|\s)run_id=([^\s]+)", posted).group(1) == RUN_ID
    assert re.search(
        r"(?:^|\s)review_rounds(?:=|:\s*)(\d+)", posted,
    ).group(1) == "1"
    assert re.search(
        r"(?:^|\s)external_commits=(-?\d+)(?=\s|$|\))", posted,
    ).group(1) == "0"


def test_format_status_comment_is_idempotent_on_the_posted_merged_body():
    posted = _posted_merged()
    assert progress.format_status_comment(posted) == posted


def test_posted_merged_comment_review_rounds_suffix():
    assert "- review: pass, no findings\n" in _posted_merged(review_rounds=1)
    assert (
        "- review: pass, no findings (2 review rounds)\n"
        in _posted_merged(review_rounds=2)
    )


def test_legacy_merged_one_liner_keeps_its_expansion():
    # Pre-#1431 callers keep the old layout; only the new body passes
    # through unchanged.
    rendered = progress.format_status_comment(_legacy_merged_one_liner())
    assert "- merge_commit: deadbeef" in rendered
    assert f"- run_id={RUN_ID}" in rendered
    assert "- detail: Orbi merged PR:" in rendered


def test_five_happy_path_comments_hide_debug_rows_and_progress_review_noise():
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

    assert "- review:" not in comments["progress"]
    # The merged record is checked on the posted form (Issue #1431), the
    # body `comment_issue` actually hands to `gh`.
    merged_posted = progress.format_status_comment(comments["merged PR"])
    merged_visible, _, merged_details = merged_posted.partition(
        "<details><summary>Run details</summary>",
    )
    assert "- review: pass, no findings" in merged_visible
    assert "- merged into: main" in merged_visible
    assert "- merge_commit=deadbeef" in merged_details
    # The result users care about remains visible; only the merge hash row moves.
    milestone_visible, milestone_details = comments["merged milestone"].split(
        "<details><summary>Run details</summary>", 1,
    )
    assert f"- result: {PR_URL}" in milestone_visible
    assert "- merge_commit: deadbeef" in milestone_details
