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
    current_review_round,
    review_round_comment_body,
    review_rounds_so_far,
    same_findings_round,
)
from tests.test_progress import make_stateful_publisher

from seam import seam

@pytest.fixture(autouse=True)
def _repository_permissions(monkeypatch):
    """Issue #1806: the trusted comments here are written by a repository
    maintainer, so the permission read answers `admin`; a non-writer
    names an outsider, and the runner's own account is pinned to a bot no
    comment names."""
    monkeypatch.setattr(
        seam, "repository_permission",
        lambda login, *, repo: (
            "none" if login in {"drive-by", "stranger", "outsider"}
            else "admin"
        ),
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "orbi-bot[bot]",
    )


RUN_ID = "abc12345"
MARKER = "<!-- orbi:run=abc12345 -->"
KIND_STARTED = "<!-- orbi:comment=started -->"
KIND_FAILURE = "<!-- orbi:comment=failure -->"
KIND_ROUND = "<!-- orbi:comment=review-round -->"
KIND_MERGED = "<!-- orbi:comment=merged -->"
TRUSTED = {"author": {"login": "maintainer"}}
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


def test_the_kind_prefix_uses_the_journal_run_id_definition():
    """Issue #2003: the id format has one owner, `journal.validate_run_id`.

    The anchored prefix interpolates `journal.RUN_ID_PATTERN` instead of
    spelling the id out again, and it reads exactly the marker
    `progress.run_marker` renders.
    """
    from orbi import journal

    assert comment_kind._KIND_PREFIX_RE.pattern == (
        r"\A<!-- orbi:run=" + journal.RUN_ID_PATTERN.pattern
        + r" -->\r?\n<!-- orbi:comment=([a-z-]+) -->"
    )
    assert comment_kind.comment_kind(
        f"{progress.run_marker(RUN_ID)}\n{KIND_STARTED}\nbody"
    ) == "started"
    # An id the writer refuses is not read as official metadata either.
    assert comment_kind.comment_kind(
        f"<!-- orbi:run=abc1234 -->\n{KIND_STARTED}\nbody"
    ) is None


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
        comments, run_id=RUN_ID, pr_number=42, repo="owner/repo",
    ) == 1
    assert review_rounds_so_far(
        comments, run_id=RUN_ID, pr_number=99, repo="owner/repo",
    ) == 1


def _opened_pr_body(pr_number=42, round_no=0):
    return (
        f"{MARKER}\n"
        f"Orbi opened PR: https://github.com/owner/repo/pull/{pr_number}\n"
        + _round_scene(round_no, pr_number)
    )


def test_current_review_round_reads_the_current_pr_scene_not_the_history():
    """Issue #1772: five completed rounds on an earlier PR (#100) followed
    by a new PR (#200) at round 0. The reported round is the NEW PR's
    scene round — the value the review loop's budget reads — never a
    recount of the Issue history (which still counts the old records)."""
    old_rounds = [
        {**TRUSTED, "body": _round_body(i, pr_number=100)}
        for i in (1, 2, 3, 4, 5)
    ]
    new_pr = {**TRUSTED, "body": _opened_pr_body(200, 0)}
    comments = old_rounds + [new_pr]
    assert review_rounds_so_far(comments, repo="owner/repo",) == 5
    assert current_review_round(comments, number=42, repo="owner/repo") == 1


def test_current_review_round_is_the_round_the_review_is_on():
    """Acceptance 2 (Issue #1772): one PR with two completed rounds — the
    review is on round 3, exactly the number the review loop uses and
    its own round comments carry (Issue #1779 made the report and the
    review use one number)."""
    comments = [
        {**TRUSTED, "body": _opened_pr_body(42, 0)},
        {**TRUSTED, "body": _round_body(1)},
        {**TRUSTED, "body": _round_body(2)},
    ]
    assert current_review_round(comments, number=42, repo="owner/repo") == 3


def test_current_review_round_without_a_readable_scene_is_zero():
    """No trusted scene is no review round: the reporting path reports 0
    instead of failing or guessing."""
    assert current_review_round([], number=42, repo="owner/repo") == 0
    # An untrusted public comment can never be the round's source.
    assert current_review_round([
        {"authorAssociation": "NONE", "body": _opened_pr_body(42, 5)},
    ], number=42, repo="owner/repo") == 0
    # A corrupted trusted scene is still no readable round.
    assert current_review_round([{
        **TRUSTED,
        "body": f"{MARKER}\n<!-- orbi:scene:v1 {{broken -->",
    }], number=42, repo="owner/repo") == 0


def _five_round_history(*, last_created="2026-01-01T00:00:00Z"):
    """One PR whose trusted history completed all five review rounds.

    The LAST trusted scene-carrying comment is round 5 (created at
    `last_created`): the resume scene a report and the review loop both
    read, so the recovery boundary can be placed before or after it.
    """
    history = [
        {**TRUSTED, "body": _opened_pr_body(42, 0),
         "createdAt": "2025-12-31T00:00:00Z"},
    ]
    history += [
        {**TRUSTED, "body": _round_body(round_no),
         "createdAt": f"2025-12-31T0{round_no}:00:00Z"}
        for round_no in (1, 2, 3, 4)
    ]
    history.append({
        **TRUSTED, "body": _round_body(5), "createdAt": last_created,
    })
    return history


def test_current_review_round_applies_the_human_recovery_reset(monkeypatch):
    """Issue #1779 acceptance 1: a recovery AFTER the exhausted scene
    restarts the budget, so the report shows round 1 — the round the
    review is on — never the exhausted 5."""
    monkeypatch.setattr(
        seam, "human_review_recovery_at",
        lambda *a, **k: "2026-01-02T00:00:00Z",
    )
    assert current_review_round(
        _five_round_history(), number=42, repo="owner/repo",
    ) == 1


def test_current_review_round_keeps_five_without_a_recovery(monkeypatch):
    """Issue #1779 acceptance 2: no recovery — the budget stays exhausted
    and the report shows 5, exactly as today."""
    monkeypatch.setattr(
        seam, "human_review_recovery_at", lambda *a, **k: None,
    )
    assert current_review_round(
        _five_round_history(), number=42, repo="owner/repo",
    ) == 5


def test_current_review_round_ignores_a_recovery_before_the_scene(
        monkeypatch,
):
    """A recovery BEFORE the scene is already carried by the scene's own
    count: it never rewinds the rounds completed after it."""
    monkeypatch.setattr(
        seam, "human_review_recovery_at",
        lambda *a, **k: "2025-12-30T00:00:00Z",
    )
    assert current_review_round(
        _five_round_history(), number=42, repo="owner/repo",
    ) == 5


def test_current_review_round_survives_a_failed_recovery_read(
        monkeypatch, caplog,
):
    """A failed recovery read never fails the report: it is reported as
    `review_recovery_read_failed` and the scene round stands."""
    def boom(*args, **kwargs):
        raise RuntimeError("gh timeline read failed")

    monkeypatch.setattr(seam, "human_review_recovery_at", boom)
    caplog.set_level("WARNING")
    assert current_review_round(
        _five_round_history(), number=42, repo="owner/repo",
    ) == 5
    assert "review_recovery_read_failed" in caplog.text


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
    assert review_rounds_so_far(comments, run_id=RUN_ID, pr_number=42, repo="owner/repo",) == 3


def test_untrusted_history_cannot_change_the_review_budget():
    comments = [
        {"authorAssociation": "NONE", "body": _round_body(i)}
        for i in (1, 2, 3)
    ]
    assert review_rounds_so_far(comments, run_id=RUN_ID, pr_number=42, repo="owner/repo",) == 0


def test_repeated_findings_identity_uses_kind_and_scene():
    finding = {"level": "Major", "location": "comments",
               "note": "repeat", "fix": "repair"}
    first = review_round_comment_body(
        MARKER, 1, 42, 0, 1, [finding], _round_scene(1), language_code="en", repo="owner/repo",
    )
    second = review_round_comment_body(
        MARKER, 2, 42, 0, 1, [finding], _round_scene(2),
        previous_comments=[{**TRUSTED, "body": first}], language_code="en", repo="owner/repo",
    )
    assert "Findings are the same as round 1." in second
    # A gate failure reusing the same findings/scene is not the predecessor.
    gate_failure = review_round_comment_body(
        MARKER, 1, 42, 0, 1, [finding], _round_scene(1), kind="failure", language_code="en", repo="owner/repo",
    )
    third = review_round_comment_body(
        MARKER, 2, 42, 0, 1, [finding], _round_scene(2),
        previous_comments=[{**TRUSTED, "body": gate_failure}], language_code="en", repo="owner/repo",
    )
    assert "Findings are the same" not in third


def test_same_findings_round_reads_the_canonical_identity_and_fallbacks():
    rendered = "- **Level:** Major"
    legacy = f"{MARKER}\nOrbi review round 1 for PR #42:\n{rendered}"
    base = {"pr_number": 42, "previous_round": 1, "rendered": rendered}
    # A non-string body is never a match.
    assert same_findings_round(
        {**TRUSTED, "body": None}, marker=MARKER, **base, repo="owner/repo",
    ) is False
    # The identical findings must appear in the predecessor.
    assert same_findings_round(
        {**TRUSTED, "body": f"{MARKER}\nOrbi review round 1 for PR #42:\nother"},
        marker=MARKER, **base, repo="owner/repo",
    ) is False
    # A historical English comment with no kind keeps the fallback.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker=MARKER, **base, repo="owner/repo",
    ) is True
    # A body that matches but whose author cannot write is not the
    # predecessor (Issue #1806: identity alone never trusts a comment).
    assert same_findings_round(
        {"author": {"login": "drive-by"}, "body": legacy},
        marker=MARKER, **base, repo="owner/repo",
    ) is False
    assert same_findings_round(
        {**TRUSTED, "body": legacy.replace("round 1", "round 2")},
        marker=MARKER, **base, repo="owner/repo",
    ) is False
    # An empty marker (no run id) still reads the fallback.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker="", **base, repo="owner/repo",
    ) is True
    # A different run marker never matches.
    assert same_findings_round(
        {**TRUSTED, "body": legacy}, marker="<!-- orbi:run=deadbeef -->", **base, repo="owner/repo",
    ) is False
    # A canonical kind whose scene is malformed does not match.
    malformed = (
        f"{MARKER}\n{KIND_ROUND}\nOrbi review round 1 for PR #42:\n"
        f"{rendered}\n<!-- orbi:scene:v1 {{broken -->"
    )
    assert same_findings_round({**TRUSTED, "body": malformed}, marker=MARKER, **base, repo="owner/repo",) is False
    # A canonical kind with no scene does not match.
    no_scene = f"{MARKER}\n{KIND_ROUND}\nOrbi review round 1 for PR #42:\n{rendered}"
    assert same_findings_round({**TRUSTED, "body": no_scene}, marker=MARKER, **base, repo="owner/repo",) is False
    # A canonical scene for another round does not match.
    other_round = no_scene + "\n" + _round_scene(2)
    assert same_findings_round({**TRUSTED, "body": other_round}, marker=MARKER, **base, repo="owner/repo",) is False
    # A canonical scene for another PR does not match.
    other_pr = no_scene + "\n" + _round_scene(1, pr_number=99)
    assert same_findings_round({**TRUSTED, "body": other_pr}, marker=MARKER, **base, repo="owner/repo",) is False
    # An unparseable PR URL in the scene does not match.
    bad_pr = scene.render(scene.Scene(
        run_id=RUN_ID, base_branch="main", base_sha="abc",
        pr_url="https://github.com/owner/repo/pull/notanumber",
        review_round=1,
    ))
    assert same_findings_round(
        {**TRUSTED, "body": no_scene + "\n" + bad_pr}, marker=MARKER, **base, repo="owner/repo",
    ) is False


# --- the merged receipt -----------------------------------------------------


def test_merged_receipt_is_kinded_and_exposes_one_pr_url():
    body = merged_pr_comment_body(
        RUN_ID, PR_URL, "deadbeef", 2, 0, 1, "main", "通过", language_code="en",
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
