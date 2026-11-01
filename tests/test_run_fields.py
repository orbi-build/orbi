"""The run's fields are values all the way to the renderers (Issue #2055).

The run's identity used to travel as one space-separated `k=v k=v` string
that `progress.run_info_fields` split back on spaces and `=`, so a value
could never carry a space and the resume record (the `orbi:scene:v1`
block) was read out of a display string. `RunFields` carries the six
fields as values; the text form exists only where a sentence or a journal
line shows it, and its field order is a rendering contract of the
existing comments.
"""
from pathlib import Path

import pytest

from orbi import delivery_comment, progress
from orbi.delivery_scene import RunContext
from orbi.run_fields import RunFields


def test_text_renders_the_existing_field_order():
    """The four shapes the runner and the release build, byte-identical."""
    assert RunFields(
        run_id="a1b2c3d4", priority="normal", task_type="ticket-only",
    ).text() == "run_id=a1b2c3d4 priority=normal task_type=ticket-only"
    assert RunFields(
        run_id="a1b2c3d4", priority="normal",
        base_branch="main", base_sha="abc1234",
    ).text() == (
        "base_branch=main base_sha=abc1234 run_id=a1b2c3d4 priority=normal"
    )
    assert RunFields(
        run_id="a1b2c3d4", priority="normal", base_branch="main",
        base_sha="abc1234", task_type="ops", repo_config="deadbeef",
    ).text() == (
        "base_branch=main base_sha=abc1234 run_id=a1b2c3d4 priority=normal "
        "task_type=ops repo_config=deadbeef"
    )
    # A release starts before its base sha is frozen.
    assert RunFields(
        run_id="a1b2c3d4", priority="p0", base_branch="main",
    ).text() == "base_branch=main run_id=a1b2c3d4 priority=p0"


def test_a_value_is_never_split_back_into_fields():
    """One value with a space stays one value: nothing parses the text."""
    run = RunFields(
        run_id="a1b2c3d4", priority="normal", base_branch="main",
        repo_config="cfg with space",
    )
    assert run.as_fields()["repo_config"] == "cfg with space"
    assert run.text().endswith("repo_config=cfg with space")


def test_the_renderers_take_the_fields_as_values():
    ctx = RunContext(
        run_id="a1b2c3d4", issue=99, branch="orbi/o-r-issue-99",
        worktree=Path("/w"), source_repo="o/r",
    )
    run = RunFields(
        run_id="a1b2c3d4", priority="normal", base_branch="main",
        base_sha="abc1234",
    )
    started = delivery_comment.started_pi_comment_body(
        ctx, run, language_code="en",
    )
    assert "- base_branch: main" in started
    assert "- base_sha: abc1234" in started
    opened = delivery_comment.opened_pr_comment_body(
        "a1b2c3d4", run, "https://github.com/o/r/pull/1", language_code="en",
    )
    assert "- base_branch: main" in opened
    # The split-back helper is gone: nothing parses the text form again.
    assert not hasattr(progress, "run_info_fields")


def test_the_opened_pr_scene_fails_fast_without_the_frozen_base():
    """The scene is the resume record: a run without its base never
    renders one."""
    with pytest.raises(ValueError, match="base_branch and base_sha"):
        delivery_comment.opened_pr_comment_body(
            "a1b2c3d4", RunFields(run_id="a1b2c3d4", priority="normal"),
            "https://github.com/o/r/pull/1", language_code="en",
        )
