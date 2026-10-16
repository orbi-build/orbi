"""Canonical comment identity and review-round accounting (Issue #1659).

A delivery comment carries display-independent metadata: the hidden run
marker plus a hidden kind marker on the line right after it. These tests
drive the real readers and writers through the `gh api` subprocess fake,
so a translated heading can never change which comment is resumed, which
rounds count, or which PR a merged receipt names.
"""
import re

import pytest

from orbi import comment_kind, progress, scene
from orbi.review_merge import merged_pr_comment_body
from orbi.review_rounds import (
    review_round_comment_body,
    review_rounds_so_far,
    same_findings_round,
)
from tests.test_progress import make_stateful_publisher

RUN_ID = "abc12345"
MARKER = "<!-- orbi:run=abc12345 -->"
KIND_STARTED = "<!-- orbi:comment=started -->"
KIND_FAILURE = "<!-- orbi:comment=failure -->"
KIND_ROUND = "<!-- orbi:comment=review-round -->"
KIND_MERGED = "<!-- orbi:comment=merged -->"
TRUSTED = {"authorAssociation": "MEMBER"}
PR_URL = "https://github.com/owner/repo/pull/42"


def _round_scene(round_no, pr_number=42, run_id=RUN_ID):
    return scene.render(scene.Scene(
        run_id=run_id, base_branch="main", base_sha="abc123",
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        review_round=round_no,
    ))


def _round_body(round_no, *, kind=KIND_ROUND, pr_number=42, run_id=RUN_ID):
    return (
        f"{MARKER}\n{kind}\n"
        f"Orbi 评审第 {round_no} 轮 for PR #{pr_number}:\n\n"
        "### Findings\n\n- **Level:** Major\n"
        + _round_scene(round_no, pr_number, run_id)
    )


def _started_body():
    return (
        f"{MARKER}\n{KIND_STARTED}\n"
        "Orbi 已启动 Pi：run_id=abc12345 priority=normal\n"
        "- base_sha: abc123"
    )


def _progress_body(heading="**Orbi 进度**"):
    return (
        f"{MARKER}\n"
        '<!-- orbi:progress:v1 {"step":"review","reported_at":'
        '"2026-10-16T00:00:00Z","activity":null} -->\n\n'
        f"{heading}\n\n- role: review"
    )


# --- the kind reader --------------------------------------------------------


def test_comment_kind_reads_only_the_anchored_official_prefix():
    assert comment_kind.comment_kind(
        f"{MARKER}\n{KIND_ROUND}\ntext"
    ) == "review-round"
    # A marker-shaped string after the first line is not official.
    assert comment_kind.comment_kind(f"{MARKER}\ntext\n{KIND_ROUND}") is None
    assert comment_kind.comment_kind(
        "a public log\n\n```\n" + _started_body() + "\n```\n"
    ) is None
    assert comment_kind.comment_kind(None) is None
    # An unsupported kind is returned verbatim, never reinterpreted.
    assert comment_kind.comment_kind(
        f"{MARKER}\n<!-- orbi:comment=other -->"
    ) == "other"


def test_progress_and_opened_comments_carry_no_kind():
    assert comment_kind.comment_kind(_progress_body()) is None


def test_comment_kind_marker_rejects_an_unknown_kind():
    assert comment_kind.comment_kind_marker("started") == KIND_STARTED
    assert comment_kind.comment_kind_marker("merged") == KIND_MERGED
    with pytest.raises(ValueError, match="unknown comment kind"):
        comment_kind.comment_kind_marker("other")


# --- discovery --------------------------------------------------------------


def test_translated_headings_are_found_by_canonical_identity():
    comments = [
        {"id": 1, "body": "unrelated"},
        {"id": 2, "body": _started_body()},
        {"id": 3, "body": _progress_body()},
        {"id": 4, "body": _progress_body(heading="**Translated progress**")},
    ]
    assert progress.find_started_comment(comments, RUN_ID)["id"] == 2
    # The canonical progress prefix wins even with a translated heading.
    assert progress.find_progress_comment(comments, RUN_ID)["id"] == 3


def test_a_quoted_marker_never_selects_a_comment():
    quoted_started = (
        "a public log\n\n```\n" + _started_body() + "\n```\n"
    )
    assert progress.find_started_comment(
        [{"id": 1, "body": quoted_started}], RUN_ID
    ) is None
    quoted_progress = "discussion\n\n```\n" + _progress_body() + "\n```\n"
    assert progress.find_progress_comment(
        [{"id": 1, "body": quoted_progress}], RUN_ID
    ) is None


def test_historical_english_comments_still_resume():
    started = f"{MARKER}\nOrbi started Pi: run_id={RUN_ID} priority=normal"
    progress_comment = f"{MARKER}\n\n**Orbi progress**\n\n- role: review"
    assert progress.find_started_comment(
        [{"id": 5, "body": started}], RUN_ID
    )["id"] == 5
    assert progress.find_progress_comment(
        [{"id": 6, "body": progress_comment}], RUN_ID
    )["id"] == 6


def test_an_unsupported_kinded_comment_is_not_reinterpreted_through_prose():
    body = (
        f"{MARKER}\n<!-- orbi:comment=other -->\n"
        f"Orbi started Pi: run_id={RUN_ID} priority=normal"
    )
    assert progress.find_started_comment([{"id": 1, "body": body}], RUN_ID) is None


# --- the gh subprocess boundary --------------------------------------------


def test_translated_run_resumes_the_same_comment_ids_by_patch():
    publisher, comments, posts, patches = make_stateful_publisher(
        comments=[
            {"id": 700, "body": _started_body()},
            {"id": 701, "body": _progress_body()},
        ],
    )
    assert publisher.ensure("fresh progress body") == 701
    assert publisher.started("fresh started body") == 700
    assert posts == [], "a resumed run must not POST duplicates"
    assert patches == [
        (701, "fresh progress body"), (700, "fresh started body"),
    ]
    assert len(comments) == 2


# --- review accounting ------------------------------------------------------


def test_review_rounds_use_the_run_and_pr_identity():
    old_attempt = {**TRUSTED, "body": _round_body(5, pr_number=47, run_id="deadbeef")}
    other_pr = {**TRUSTED, "body": _round_body(1, pr_number=99)}
    current = {**TRUSTED, "body": _round_body(1)}
    comments = [old_attempt, other_pr, current]
    assert review_rounds_so_far(
        comments, run_id=RUN_ID, pr_number=42,
    ) == 1
    assert review_rounds_so_far(
        comments, run_id=RUN_ID, pr_number=99,
    ) == 1


def test_only_a_completed_review_round_kind_with_a_valid_scene_counts():
    comments = [
        {**TRUSTED, "body": _round_body(1)},
        {**TRUSTED, "body": _round_body(2)},
        # A review failure that reuses the previous scene is not a round.
        {**TRUSTED, "body": _round_body(1, kind=KIND_FAILURE)},
        # An opened-PR comment containing a scene is not a round.
        {**TRUSTED, "body": f"{MARKER}\nOrbi opened PR: {PR_URL}\n"
                            + _round_scene(1)},
        # A canonical kind with malformed structured data does not fall
        # back to the English prose.
        {**TRUSTED, "body": f"{MARKER}\n{KIND_ROUND}\n"
                            "Orbi review round 9 for PR #42:\n"
                            "<!-- orbi:scene:v1 {broken -->"},
        # A canonical kind with no scene carries no round record and does
        # not fall back to the prose.
        {**TRUSTED, "body": f"{MARKER}\n{KIND_ROUND}\n"
                            "Orbi review round 8 for PR #42:"},
        # A historical English comment with no kind still counts.
        {**TRUSTED, "body": f"{MARKER}\nOrbi review round 7 for PR #42: legacy"},
    ]
    assert review_rounds_so_far(comments, run_id=RUN_ID, pr_number=42) == 3


def test_untrusted_history_cannot_change_the_review_budget():
    comments = [
        {"authorAssociation": "NONE", "body": _round_body(i)}
        for i in (1, 2, 3)
    ]
    assert review_rounds_so_far(comments, run_id=RUN_ID, pr_number=42) == 0


def test_repeated_findings_identity_uses_kind_and_scene():
    finding = {"level": "Major", "location": "comments",
               "note": "repeat", "fix": "repair"}
    first = review_round_comment_body(
        MARKER, 1, 42, 0, 1, [finding], _round_scene(1),
    )
    second = review_round_comment_body(
        MARKER, 2, 42, 0, 1, [finding], _round_scene(2),
        previous_comments=[{**TRUSTED, "body": first}],
    )
    assert "Findings are the same as round 1." in second
    # A gate failure reusing the same findings/scene is not the predecessor.
    gate_failure = review_round_comment_body(
        MARKER, 1, 42, 0, 1, [finding], _round_scene(1), kind="failure",
    )
    third = review_round_comment_body(
        MARKER, 2, 42, 0, 1, [finding], _round_scene(2),
        previous_comments=[{**TRUSTED, "body": gate_failure}],
    )
    assert "Findings are the same" not in third


def test_same_findings_round_reads_the_canonical_identity_and_fallbacks():
    rendered = "- **Level:** Major"
    legacy = f"{MARKER}\nOrbi review round 1 for PR #42:\n{rendered}"
    base = {"pr_number": 42, "previous_round": 1, "rendered": rendered}
    # A non-string body is never a match.
    assert same_findings_round(
        {**TRUSTED, "body": None}, marker=MARKER, **base,
    ) is False
    # The identical findings must appear in the predecessor.
    assert same_findings_round(
        {**TRUSTED, "body": f"{MARKER}\nOrbi review round 1 for PR #42:\nother"},
        marker=MARKER, **base,
    ) is False
    # A historical English comment with no kind keeps the fallback.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker=MARKER, **base,
    ) is True
    assert same_findings_round(
        {**TRUSTED, "body": legacy.replace("round 1", "round 2")},
        marker=MARKER, **base,
    ) is False
    # An empty marker (no run id) still reads the fallback.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker="", **base,
    ) is True
    # A different run marker never matches.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker="<!-- orbi:run=deadbeef -->", **base,
    ) is False
    # A canonical kind whose scene is malformed does not match.
    malformed = (
        f"{MARKER}\n{KIND_ROUND}\nOrbi review round 1 for PR #42:\n"
        f"{rendered}\n<!-- orbi:scene:v1 {{broken -->"
    )
    assert same_findings_round({**TRUSTED, "body": malformed}, marker=MARKER, **base) is False
    # A canonical kind with no scene does not match.
    no_scene = f"{MARKER}\n{KIND_ROUND}\nOrbi review round 1 for PR #42:\n{rendered}"
    assert same_findings_round({**TRUSTED, "body": no_scene}, marker=MARKER, **base) is False
    # A canonical scene for another round does not match.
    other_round = no_scene + "\n" + _round_scene(2)
    assert same_findings_round({**TRUSTED, "body": other_round}, marker=MARKER, **base) is False
    # A canonical scene for another PR does not match.
    other_pr = no_scene + "\n" + _round_scene(1, pr_number=99)
    assert same_findings_round({**TRUSTED, "body": other_pr}, marker=MARKER, **base) is False
    # An unparseable PR URL in the scene does not match.
    bad_pr = scene.render(scene.Scene(
        run_id=RUN_ID, base_branch="main", base_sha="abc",
        pr_url="https://github.com/owner/repo/pull/notanumber",
        review_round=1,
    ))
    assert same_findings_round(
        {**TRUSTED, "body": no_scene + "\n" + bad_pr}, marker=MARKER, **base,
    ) is False


# --- the merged receipt -----------------------------------------------------


def test_merged_receipt_is_kinded_and_exposes_one_pr_url():
    body = merged_pr_comment_body(
        RUN_ID, PR_URL, "deadbeef", 2, 0, 1, "main", "通过",
    )
    assert body.splitlines()[1] == KIND_MERGED
    assert re.search(r"pull/42", body)
    assert body.count(f"- pr_url={PR_URL}") == 1
    for row in ("merge_commit=deadbeef", "review_rounds=2", "commits=1",
                "external_commits=0", f"run_id={RUN_ID}"):
        assert len(re.findall(rf"(?m)^- {re.escape(row)}$", body)) == 1
    assert body.count("<details><summary>Run details</summary>") == 1


def test_short_milestones_do_not_claim_a_failure_or_merged_kind():
    publisher, comments, posts, patches = make_stateful_publisher()
    publisher.milestone("blocked: boom")
    publisher.milestone(
        f"merged: {PR_URL} (merge_commit=m review_rounds=1 "
        "external_commits=0 commits=1)"
    )
    assert posts, "the milestones must be posted"
    assert all(
        comment_kind.comment_kind(post["body"]) is None for post in posts
    )
