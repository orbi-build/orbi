"""Issue #1730: every blocked comment ends with the community help line.

Ten scenes put an Issue into `ai-blocked` and each renders its own
reason text, unblock step, hidden markers and extra fields. The pointer to
where a person can ask for help is NOT one of them: the ONE blocked exit
(`orbi.blocked_comment.post_blocked_comment`) applies the label patch and
appends the run language's `help.community` line to the posted body.

Each scene test below stubs the exit's two writes and asserts the posted
body: the scene's own text first, then one blank line and the help line,
exactly once. The other six scenes (claim scan, release, external PR, label
repair, PR closed, ticket-only) are driven in their own test modules, where
their environments already live: test_resume_pr.py,
test_bootstrap_runner.py and test_fix_needed_loop.py.
"""
import pytest

import orbi.blocked_comment as blocked_comment
import orbi.failure_report as failure_report
from orbi import language
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker
from orbi.progress import run_marker
from seam import seam

RUN_ID = "a1b2c3d4"
REPO = "owner/repo"
LANGUAGES = ["en", "zh"]


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
    """The exit itself: label patch, then the body verbatim plus a blank
    line and the help line in the run's language — added once."""
    calls = capture_blocked_writes(monkeypatch)
    blocked_comment.post_blocked_comment(
        42, repo=REPO, body="Orbi failed: the reason",
        current_labels={"ai-fix-needed"}, language_code=language_code,
    )
    body = posted_body(calls)
    assert calls[0][2]["current_labels"] == {"ai-fix-needed"}
    assert body == f"Orbi failed: the reason\n\n{help_line(language_code)}"


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
    assert body == f"{expected}\n\n{help_line(language_code)}"


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
    """The classified blocked diagnostic (failure._failure_comment_body):
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
    assert language.text(language_code, "failure.headline_blocked") in body
    assert_one_help_line(body, language_code)
