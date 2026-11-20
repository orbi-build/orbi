"""The waiting-for-checks line of a deferred delivery (Issue #2304).

A delivery that passed review is not merged in the tick whose head it
pushed: the merge gate merges no head whose checks it has not read, and a
single-slot runner can be busy for an hour before the next tick reads
them. The person watching the Issue must see what Orbi waits for: one
line on the run's EXISTING progress comment, naming the head commit and
saying the merge follows automatically once the checks pass — no time, no
next tick, no new comment kind, no new label.
"""
import pytest

from orbi import language, progress, waiting_checks
from tests.fakes.progress import patched_comment_bodies
from tests.fakes.waiting_checks import (
    bind_gh, bind_pending_delivery, bind_unreachable_gh,
)

RUN_ID = "abc12345"
PR_URL = "https://github.com/owner/repo/pull/9"
HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"

# One progress body exactly as progress.progress_body renders it: the
# run marker, the versioned block, the status rows, the details fold and
# the runner marker.
PROGRESS_BODY = "\n".join([
    f"<!-- orbi:run={RUN_ID} -->",
    '<!-- orbi:progress:v1 {"step":"checks","reported_at":'
    '"2026-11-20T06:12:00Z","activity":null} -->',
    "",
    "**Orbi progress**",
    "",
    "**#9 ship the login form**",
    "",
    "- role: review",
    "- step: checks",
    "- reported at: 2026-11-20T06:12:00Z",
    "- last activity: -",
    "- activity: -",
    "- tests: pending",
    f"- PR: {PR_URL}",
    "",
    "<details><summary>Run details</summary>",
    "",
    f"- run_id={RUN_ID}",
    "- priority: normal",
    "",
    "</details>",
    "",
    "<!-- runner=8a12fb1c -->",
])


def _comments(*bodies: str) -> list[dict]:
    return [
        {"id": 100 + index, "body": body}
        for index, body in enumerate(bodies)
    ]


def _published_bodies(calls: list[list[str]]) -> list[str]:
    return patched_comment_bodies(calls)


# ---------------------------------------------------------------- the line


def test_the_line_names_the_head_commit():
    """Which commit's checks: the line carries the head SHA."""
    line = waiting_checks.line("en", HEAD)
    assert HEAD[:12] in line


def test_the_line_says_orbi_merges_it_once_the_checks_pass():
    """The promise the person needs: the merge follows by itself."""
    line = waiting_checks.line("en", HEAD)
    assert "checks" in line
    assert "merges this PR automatically once they pass" in line


def test_the_line_promises_no_time_and_no_next_tick():
    """A single-slot runner can be an hour from the next tick, so the
    line carries no time and no next-tick promise (Issue #2304)."""
    line = waiting_checks.line("en", HEAD)
    assert "next tick" not in line.lower()
    assert "within" not in line.lower()
    assert "minute" not in line.lower()
    assert "hour" not in line.lower()


def test_the_line_is_chinese_too():
    line = waiting_checks.line("zh", HEAD)
    assert "正在等待提交 1a2b3c4d5e6f" in line
    assert "Orbi 会自动合并" in line
    assert "next tick" not in line.lower()


def test_the_line_is_one_visible_line_with_a_hidden_marker():
    """The marker is the line's identity: it rides the same physical line
    as the sentence, so a later publication replaces exactly one line."""
    line = waiting_checks.line("en", HEAD)
    assert waiting_checks.WAITING_MARKER in line
    assert "\n" not in line


# ------------------------------------------------------------ the body edit


def test_with_line_sets_the_line_above_the_details_fold():
    body = waiting_checks.with_line(
        PROGRESS_BODY, waiting_checks.line("en", HEAD))
    rows = body.splitlines()
    waiting_at = next(
        index for index, row in enumerate(rows)
        if waiting_checks.WAITING_MARKER in row
    )
    fold_at = next(
        index for index, row in enumerate(rows) if row.startswith("<details>")
    )
    assert waiting_at < fold_at
    assert rows[waiting_at + 1] == ""
    assert rows[fold_at - 1] == ""
    assert HEAD[:12] in rows[waiting_at]


def test_with_line_keeps_the_documented_prefix_and_the_rest_of_the_record():
    body = waiting_checks.with_line(
        PROGRESS_BODY, waiting_checks.line("en", HEAD))
    assert body.startswith(f"<!-- orbi:run={RUN_ID} -->\n<!-- orbi:progress:v1 ")
    assert body.endswith("<!-- runner=8a12fb1c -->")
    # Every original row survives: the line is an addition, not a rewrite
    # (a re-render would drop the fold and reset step/role).
    for row in PROGRESS_BODY.splitlines():
        assert row in body.splitlines()


def test_with_line_replaces_its_own_line_instead_of_stacking():
    """A repeated publication (one per tick while the wait lasts) keeps
    exactly one line, and the line always names the newest head."""
    once = waiting_checks.with_line(
        PROGRESS_BODY, waiting_checks.line("en", HEAD))
    twice = waiting_checks.with_line(
        once, waiting_checks.line("en", "f" * 40))
    assert twice.count(waiting_checks.WAITING_MARKER) == 1
    assert "f" * 12 in twice
    assert HEAD[:12] not in twice
    # ...and the body is byte-identical to the first publication.
    assert twice == waiting_checks.with_line(
        PROGRESS_BODY, waiting_checks.line("en", "f" * 40))


def test_with_line_moves_its_own_line_from_the_top_of_a_body():
    """A body whose only row above the fold IS the old waiting line keeps
    exactly one line after the republish."""
    body = waiting_checks.with_line(
        waiting_checks.line("en", HEAD) + "\n\n- step: checks",
        waiting_checks.line("en", "f" * 40),
    )
    assert body.count(waiting_checks.WAITING_MARKER) == 1
    assert "f" * 12 in body
    assert HEAD[:12] not in body


def test_with_line_appends_to_a_body_without_the_details_fold():
    body = waiting_checks.with_line(
        f"<!-- orbi:run={RUN_ID} -->\n\n- step: checks",
        waiting_checks.line("en", HEAD),
    )
    assert body.endswith("\n\n" + waiting_checks.line("en", HEAD))


# -------------------------------------------------------------- publishing


def test_publish_patches_the_runs_existing_progress_comment(monkeypatch):
    calls = bind_gh(
        monkeypatch, comments=_comments("a human note", PROGRESS_BODY))
    waiting_checks.publish(
        issue=9, source_repo="owner/repo", run_id=RUN_ID,
        language_code="en", head=HEAD,
    )
    bodies = _published_bodies(calls)
    assert len(bodies) == 1
    assert HEAD[:12] in bodies[0]
    assert bodies[0].startswith(f"<!-- orbi:run={RUN_ID} -->")
    # The PATCH targets the located comment, never the issue-scoped route.
    patch = next(call for call in calls if "--method" in call)
    assert patch[2] == "repos/owner/repo/issues/comments/101"


def test_publish_never_creates_a_comment(monkeypatch):
    """No progress comment, no publication: the line is an update of the
    existing record, never a new comment kind."""
    calls = bind_gh(monkeypatch, comments=_comments(
        f"<!-- orbi:run={RUN_ID} -->\nOrbi started Pi: #9 ship",
    ))
    waiting_checks.publish(
        issue=9, source_repo="owner/repo", run_id=RUN_ID,
        language_code="en", head=HEAD,
    )
    assert [call for call in calls if "--method" in call] == []


def test_publish_fails_fast_when_the_patch_fails(monkeypatch):
    bind_gh(monkeypatch, comments=_comments(PROGRESS_BODY), patch_fails=True)
    with pytest.raises(RuntimeError, match="PATCH failed"):
        waiting_checks.publish(
            issue=9, source_repo="owner/repo", run_id=RUN_ID,
            language_code="en", head=HEAD,
        )


# ------------------------------------------------------------ head reading


def test_pr_head_reads_the_documented_head_field(monkeypatch):
    commands = bind_gh(monkeypatch, head=HEAD)
    assert waiting_checks.pr_head(PR_URL, "owner/repo") == HEAD
    assert commands == [[
        "gh", "pr", "view", "9", "--repo", "owner/repo",
        "--json", "headRefOid",
    ]]


def test_pr_head_fails_fast_without_a_head(monkeypatch):
    bind_gh(monkeypatch)
    with pytest.raises(ValueError, match="no head commit"):
        waiting_checks.pr_head(PR_URL, "owner/repo")


# --------------------------------------------- the delivery_ci_pending sites


PENDING_ROLLUP = [
    {"name": "tests", "status": "IN_PROGRESS", "conclusion": None},
]


def test_defer_pending_journals_the_state_and_says_which_commit_it_waits_for(
    monkeypatch, caplog,
):
    """delivery_ci_pending: the scan skips the delivery, so this line is
    the only thing the person sees for as long as the wait lasts."""
    calls = bind_gh(
        monkeypatch, comments=_comments(PROGRESS_BODY), head=HEAD)
    caplog.set_level("INFO")
    waiting_checks.defer_pending(
        PR_URL, PENDING_ROLLUP, {"number": 9, "title": "ship"},
        "owner/repo", RUN_ID, "en", None,
    )
    assert "delivery_ci_pending" in caplog.text
    bodies = _published_bodies(calls)
    assert len(bodies) == 1
    assert HEAD[:12] in bodies[0]
    assert "automatically once they pass" in bodies[0]


def test_defer_pending_without_a_run_id_only_journals(monkeypatch, caplog):
    """A tick with no bound run id has no progress comment to update."""
    calls = bind_gh(monkeypatch, comments=_comments(PROGRESS_BODY))
    caplog.set_level("INFO")
    waiting_checks.defer_pending(
        PR_URL, PENDING_ROLLUP, {"number": 9, "title": "ship"},
        "owner/repo", None, "en", None,
    )
    assert "delivery_ci_pending" in caplog.text
    assert calls == []


def test_defer_pending_is_a_bypass_when_the_read_fails(monkeypatch, caplog):
    """Issue #79: observability never decides the delivery. A failing read
    is logged and the deferral stands."""
    bind_unreachable_gh(monkeypatch)
    caplog.set_level("INFO")
    waiting_checks.defer_pending(
        PR_URL, PENDING_ROLLUP, {"number": 9, "title": "ship"},
        "owner/repo", RUN_ID, "en", None,
    )
    assert "delivery_ci_pending" in caplog.text
    assert "progress_publish_failed" in caplog.text


def test_the_gh_fakes_reject_an_unexpected_command(monkeypatch):
    """The fakes prove the deferral performs nothing but its reads and the
    one PATCH: any other command fails loudly instead of passing for a
    read the delivery never makes."""
    bind_gh(monkeypatch, comments=_comments(PROGRESS_BODY))
    with pytest.raises(AssertionError, match="unexpected command"):
        waiting_checks.run_command(["gh", "issue", "view", "9"])
    bind_pending_delivery(monkeypatch, run_id=RUN_ID)
    with pytest.raises(AssertionError, match="unexpected command"):
        waiting_checks.run_command(["gh", "issue", "view", "9"])


def test_the_catalog_carries_both_languages():
    english, chinese = language.MESSAGES["progress.waiting_checks"]
    assert "{head}" in english and "{head}" in chinese


def test_the_failure_scene_replaces_the_line_with_the_failure_text(tmp_path):
    """When the checks then fail, the waiting line is gone: a failed
    delivery ends on the existing failure scene, and no full progress
    render ever carries the line (it lives only in the edit)."""
    body = progress.finish_progress_body(
        number=9, title="ship the login form", run_id=RUN_ID,
        role="review", branch="b", worktree=tmp_path, pr_url=PR_URL,
        review_round=1, priority="normal",
        detail="delivery gate: CI check 'tests' failed on PR #9",
        next_step="the next review session fixes the red check",
        outcome="blocked", source_repo="owner/repo", language_code="en",
    )
    assert waiting_checks.WAITING_MARKER not in body
    assert "CI check 'tests' failed on PR #9" in body
