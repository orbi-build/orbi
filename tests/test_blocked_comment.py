"""Issue #1730: every blocked comment ends with the community help line.

Eleven scenes put an Issue into `ai-blocked` and each renders its own
reason text, unblock step, hidden markers and extra fields. What they do
NOT each own is what every stop shares: the ONE blocked exit
(`orbi.blocked_comment.post_blocked_comment`) applies the label patch,
writes the `failure` kind and the terminal `orbi:failure:v1` record
(Issue #2245), and appends the run language's `help.community` line to the
posted body.

Each scene test below stubs the exit's two writes and asserts the posted
body: the scene's own text, the shared kind/record, then one blank line and
the help line, exactly once. The scenes whose environment lives in another
module (claim scan, release, external PR, label repair, PR closed,
ticket-only) are driven end to end there too — the external PR stop in
test_question_resume.py — while the table test at the bottom pins the
failure kind and record of EVERY stop body (Issue #2245 acceptance 2).
"""
import pytest

import orbi.blocked_comment as blocked_comment
import orbi.failure_report as failure_report
from orbi import (
    check_permission, failure, language, question, release_comments,
    user_summary,
)
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker
from orbi.progress import run_marker
from orbi.run_fields import RunFields
from seam import seam

RUN_ID = "a1b2c3d4"
REPO = "owner/repo"
LANGUAGES = ["en", "zh"]


def stop_record() -> str:
    """The terminal blocked record the exit writes when a scene rendered
    none (Issue #2245)."""
    return failure.render(failure.Failure(
        reason_code="human_decision_required", action_code="decide",
        retry_safe=False, outcome="blocked",
    ))


def help_line(language_code: str) -> str:
    return language.text(language_code, "help.community")


def capture_blocked_writes(monkeypatch) -> list:
    """Record the exit's label patch and comment, in call order.

    The seam fans both writes out to every module that binds them, the
    blocked exit included (tests/seam.py), so the capture never adds a
    module-level test patch (Issue #789's ratchet).
    """
    calls: list = []
    monkeypatch.setattr(
        seam, "apply_label_patch",
        lambda number, **kwargs: calls.append(("label", number, kwargs)),
    )
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: calls.append(("comment", number, body)),
    )
    return calls


def posted_body(calls: list) -> str:
    """The one body the exit posted, asserting the label landed first."""
    assert [call[0] for call in calls] == ["label", "comment"], (
        "the blocked exit writes the label first, the comment second"
    )
    assert calls[0][1] == 42
    assert calls[0][2]["event"] == blocked_comment.EVENT_BLOCKED
    return calls[1][2]


def assert_one_help_line(body: str, language_code: str) -> None:
    """The body ends with the language's help line as its last paragraph."""
    line = help_line(language_code)
    assert body.endswith(f"\n\n{line}"), body
    assert body.count(line) == 1, body


@pytest.mark.parametrize("language_code", LANGUAGES)
def test_the_blocked_exit_labels_then_posts_the_body_and_one_help_line(
        monkeypatch, language_code):
    """The exit itself: label patch, then the scene body with the
    terminal failure record plus a blank line and the help line in the
    run's language — added once."""
    calls = capture_blocked_writes(monkeypatch)
    blocked_comment.post_blocked_comment(
        42, repo=REPO, body="Orbi failed: the reason",
        current_labels={"ai-fix-needed"}, language_code=language_code,
    )
    body = posted_body(calls)
    assert calls[0][2]["current_labels"] == {"ai-fix-needed"}
    assert body == (
        f"{stop_record()}\nOrbi failed: the reason\n\n"
        f"{help_line(language_code)}"
    )


@pytest.mark.parametrize("language_code", LANGUAGES)
def test_scene_blocked_scene_ends_with_the_help_line(
        monkeypatch, language_code):
    """failure.scene_blocked (the malformed opened-PR scene)."""
    calls = capture_blocked_writes(monkeypatch)
    error = ValueError("bad scene")
    failure_report.block_scene_failure(
        {"number": 42}, error, REPO, [], language_code=language_code,
    )
    body = posted_body(calls)
    expected = language.text(language_code, "failure.scene_blocked", error=error)
    assert body == (
        f"{stop_record()}\n{expected}\n\n{help_line(language_code)}"
    )


@pytest.mark.parametrize("language_code", LANGUAGES)
def test_repo_config_blocked_ends_with_the_help_line(
        monkeypatch, language_code):
    """failure.repo_config_blocked (the invalid repository config)."""
    calls = capture_blocked_writes(monkeypatch)
    error = ValueError("bad key")
    failure_report.block_repo_config_failure(
        42, REPO, error, RUN_ID, {"ai-ready"}, language_code=language_code,
    )
    body = posted_body(calls)
    assert calls[0][2]["current_labels"] == {"ai-ready"}
    expected = (
        f"{run_marker(RUN_ID)}\n"
        f"{comment_kind_marker(COMMENT_KIND_FAILURE)}\n"
        f"{stop_record()}\n"
        + language.text(
            language_code, "failure.repo_config_blocked", error=error,
        )
    )
    assert body == f"{expected}\n\n{help_line(language_code)}"


@pytest.mark.parametrize("language_code", LANGUAGES)
def test_resume_blocked_ends_with_the_help_line_exactly_once(
        monkeypatch, language_code):
    """failure.resume_blocked: the scene that already appended the line
    itself now gets it from the exit — never twice."""
    calls = capture_blocked_writes(monkeypatch)
    error = RuntimeError("no run state file")
    failure_report._report_resume_failure(
        number=42, source_repo=REPO, run_id=RUN_ID, error=error,
        language_code=language_code,
    )
    body = posted_body(calls)
    # The claimed Issue still carries the queue label, so the blocked
    # terminal clears it with the in-flight label (Issue #1775).
    assert calls[0][2]["current_labels"] == {"ai-in-progress", "ai-ready"}
    expected = (
        f"{run_marker(RUN_ID)}\n"
        f"{comment_kind_marker(COMMENT_KIND_FAILURE)}\n"
        f"{stop_record()}\n"
        + language.text(
            language_code, "failure.resume_blocked",
            detail="no run state file", run_id=RUN_ID,
        )
    )
    assert body == f"{expected}\n\n{help_line(language_code)}"


class Publisher:
    """Records the milestone publish without GitHub."""

    def __init__(self) -> None:
        self.milestones: list[str] = []

    def milestone(self, body: str, **kwargs) -> None:
        self.milestones.append(body)


@pytest.mark.parametrize("language_code", LANGUAGES)
def test_delivery_failure_diagnostic_ends_with_the_help_line(
        monkeypatch, tmp_path, language_code):
    """The classified blocked diagnostic (failure.failure_comment_body):
    its renderer stopped appending the line; the exit adds it once."""
    calls = capture_blocked_writes(monkeypatch)
    outcome = failure_report.report_delivery_failure(
        failure_report.UnrecoverableDeliveryError(
            "cannot continue the interrupted run",
        ),
        issue={"number": 42, "title": "task", "labels": []},
        source_repo=REPO, run_id=RUN_ID, pr_url=None,
        worktree=tmp_path, branch="orbi/task", role="review",
        classify=True, finish=False, publisher=Publisher(),
        current_labels=set(), language_code=language_code,
    )
    assert outcome == "blocked"
    body = posted_body(calls)
    assert calls[0][2]["current_labels"] == set()
    assert language.text(
        language_code, "failure.headline_blocked",
        disposition=language.text(language_code, "failure.disposition_decide"),
    ) in body
    assert_one_help_line(body, language_code)


PR_URL = "https://github.com/owner/repo/pull/7"


def stop_scene_bodies(language_code: str) -> dict[str, str]:
    """The body each ONE of the eleven ai-blocked stop scenes hands over.

    Each body is composed the way its writer composes it (run marker first,
    then the scene's own text / kind / record), so the table below covers
    the whole set of stops that label an Issue `ai-blocked` — the four
    failure report scenes, the permission stop, the claim scan, the release,
    the external PR, the label repair, the closed PR and the ticket-only
    stop (Issue #2245).
    """
    marker = run_marker(RUN_ID)
    kind = comment_kind_marker(COMMENT_KIND_FAILURE)
    summary = {
        "status_url": None, "issue": 42, "title": "task",
        "language_code": language_code,
    }
    return {
        # failure_report: the malformed opened-PR scene.
        "scene_blocked": (
            f"{marker}\n{kind}\n"
            + language.text(
                language_code, "failure.scene_blocked",
                error=ValueError("bad scene"),
            )
        ),
        # failure_report: the invalid repository config.
        "repo_config_blocked": (
            f"{marker}\n{kind}\n"
            + language.text(
                language_code, "failure.repo_config_blocked",
                error=ValueError("bad key"),
            )
        ),
        # failure_report: the unverifiable resumed run.
        "resume_blocked": (
            f"{marker}\n{kind}\n"
            + language.text(
                language_code, "failure.resume_blocked",
                detail="no run state", run_id=RUN_ID,
            )
        ),
        # failure_report: the classified delivery failure (its own record).
        "delivery_failure": (
            f"{marker}\n{kind}\n"
            + failure.failure_comment_body(
                outcome="blocked", action="fix", reason="boom",
                diagnosis="diagnosis", scene="scene", evidence="",
                pr_url=None, issue="owner/repo#42", run_id=RUN_ID,
                language_code=language_code, issue_number=42,
                issue_title="task",
            )
        ),
        # check_permission: the App has not approved the check read.
        "checks_permission_missing": check_permission.comment_body(
            check_permission.CheckPermission(pr=7),
            url="https://github.com/settings/installations/9",
            run_id=RUN_ID, issue_number=42, issue_title="task",
            language_code=language_code,
        ),
        # resume_scan: the opened-PR scene comment is missing.
        "claim_scene_missing": user_summary.notice(
            marker, "claim.scene_missing", error="gone", **summary,
        ),
        # release_closeout: the release task failed.
        "release_failed": release_comments.release_failure_comment_body(
            RUN_ID, RunFields(RUN_ID, "normal"), ValueError("boom"),
            language_code=language_code, step="publish",
        ),
        # review_merge: the external PR that passed review.
        "external_pr": (
            f"{marker}\n"
            + language.text(
                language_code, "review.external_conclusion", run_id=RUN_ID,
            )
            + "\n\n"
            + question.external_pr_stop(
                number=42, source_repo=REPO, pr_url=PR_URL,
                language_code=language_code, comments=[],
            )
        ),
        # review_merge: the delivery labels could not be repaired.
        "label_repair_failed": user_summary.append_summary(
            marker + "\n" + language.text(
                language_code, "review.label_repair_failed",
                pr_url=PR_URL, reason="labels unreadable",
            ),
            **summary,
        ),
        # runner: the PR was closed without a merge.
        "pr_closed": user_summary.notice(
            marker, "runner.pr_closed_comment", pr_url=PR_URL, **summary,
        ),
        # runner: the ticket-only delivery failed.
        "ticket_only_failed": user_summary.notice(
            marker, "runner.ticket_only_failed", detail="boom",
            run_info="base_branch=main", run_id=RUN_ID, **summary,
        ),
    }


@pytest.mark.parametrize("scene", sorted(stop_scene_bodies("en")))
def test_every_ai_blocked_stop_writes_the_failure_kind_and_record(
        monkeypatch, scene):
    """Issue #2245 acceptance 2: every stop that labels an Issue
    `ai-blocked` writes ONE `orbi:failure:v1` record with outcome
    `blocked` and a closed-set reason, beside the failure kind — a reader
    that identifies a stop by its record (the Cloud status page) finds
    every one of them."""
    calls = capture_blocked_writes(monkeypatch)
    blocked_comment.post_blocked_comment(
        42, repo=REPO, body=stop_scene_bodies("en")[scene],
        current_labels=set(), language_code="en",
    )
    body = posted_body(calls)
    assert body.splitlines()[1] == comment_kind_marker(COMMENT_KIND_FAILURE)
    assert body.count("<!-- orbi:failure:v1 ") == 1
    record = failure.parse(body)
    assert record is not None
    assert record.outcome == "blocked"
    assert record.reason_code in failure.REASON_CODES
