"""Comment rendering contracts for the display-only details wrapper."""
import re
from pathlib import Path

from orbi import progress, scene
from orbi.delivery_scene import RunContext
from orbi.runner import opened_pr_comment_body, started_pi_comment_body

RUN_ID = "abc12345"
PR_URL = "https://github.com/owner/repo/pull/42"


def _without_details(body: str) -> str:
    return re.sub(
        r"\n\n<details><summary>Run details</summary>\n\n.*?\n\n</details>",
        "",
        body,
        flags=re.S,
    )


def test_display_wrapper_preserves_scene_progress_classifier_and_cloud_fields():
    context = RunContext(
        run_id=RUN_ID, issue=1153, branch="orbi/issue-1153",
        worktree=Path("/tmp/worktree"), source_repo="owner/repo",
    )
    opened = opened_pr_comment_body(
        RUN_ID, "base_branch=main base_sha=abc123 run_id=abc12345",
        PR_URL,
    )
    opened_old = _without_details(opened)
    assert scene.parse(opened) == scene.parse(opened_old)

    state = {
        "run_id": RUN_ID, "issue": 1153, "issue_title": "Comment display",
        "role": "review", "priority": "normal", "phase": "done",
        "elapsed": "1s", "last_activity": "-", "last_action": "-",
        "tests": "10 passed", "review_round": 1,
        "review": "pass, no findings", "branch": context.branch,
        "pr": PR_URL, "session": "session-1",
    }
    rendered_progress = progress.progress_body(state)
    legacy_progress = _without_details(rendered_progress)
    comments = [
        started_pi_comment_body(
            context,
            "base_branch=main base_sha=abc123 repo_config=cfg "
            "run_id=abc12345 priority=normal session=session-1",
        ),
        rendered_progress,
        opened,
        progress.field_block(
            RUN_ID, "Orbi: merged",
            {"result": "merge_commit=deadbeef external_commits=0",
             "run_id": RUN_ID},
            detail_keys={"result"},
        ),
    ]
    assert len(comments) + 1 == 5  # merged PR is rendered below
    assert progress.find_progress_comment(
        [{"id": 1, "body": rendered_progress}], RUN_ID,
    )["id"] == progress.find_progress_comment(
        [{"id": 1, "body": legacy_progress}], RUN_ID,
    )["id"]
    assert rendered_progress.splitlines()[0] == legacy_progress.splitlines()[0]
    assert rendered_progress.splitlines()[2] == legacy_progress.splitlines()[2]

    merged = (
        f"<!-- orbi:run={RUN_ID} -->\n"
        f"Orbi merged PR: {PR_URL} (merge_commit=deadbeef "
        f"review_rounds=1 external_commits=0 commits=1 run_id={RUN_ID})\n"
        "review: pass, no findings\n\n"
        "<details><summary>Run details</summary>\n\n"
        "- merge_commit: deadbeef\n\n</details>"
    )
    merged_old = merged.split("\nreview: ", 1)[0]
    merged_match = re.search(
        r"Orbi merged PR:\s+https://[^\s/]+/[^\s/]+/[^\s/]+/pull/(\d+)",
        merged,
    )
    run_match = re.search(r"(?:^|\s)run_id=([^\s]+)", merged)
    external_match = re.search(r"(?:^|\s)external_commits=(-?\d+)", merged)
    assert (merged_match.group(1), run_match.group(1).rstrip(")"),
            external_match.group(1)) == ("42", RUN_ID, "0")
    assert merged.splitlines()[1] == merged_old.splitlines()[1]
    classified = progress.format_status_comment(merged).splitlines()
    assert classified[:2] == [
        f"<!-- orbi:run={RUN_ID} -->",
        f"Orbi merged PR: {PR_URL}",
    ]
