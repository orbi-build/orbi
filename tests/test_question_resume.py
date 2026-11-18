"""A review deadlock with a recommended option resolves itself once (Issue #2220).

The stop report is where the engine decides: a `blocked_on_human_decision`
verdict that carries one valid question, a `decision_class` other than
`authority` and a `recommended` option the engine can execute in place
(`amend_requeue`) is answered by the engine at that moment — the option's
acceptance criterion is appended to the Issue body, the Issue is relabelled
for delivery and ONE `requeued` comment carries the question plus an
`orbi:answer:v1` record. Every other stop stays `ai-blocked` with the
question attached. Real `gh` argv is served by `tests.fakes.github`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbi import (
    acceptance_section, failure, failure_report, notify, question, requeue,
    review_merge, runner,
)
from orbi.comment_kind import COMMENT_KIND_FAILURE, comment_kind_marker
from orbi.delivery_labels import BLOCKED_LABEL, FIX_NEEDED_LABEL, PR_OPENED_LABEL
from tests.fakes.github import FakeGh
from tests.fakes.question import (
    bind_external_stop, bind_history_failure, bind_repository,
)
from tests.fakes.telegram import TelegramEndpoint, bind_notify, point_at


FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "question"
    / "review-deadlock-1243.json"
)
REPO = "orbi-build/orbi"
RUN_ID = "abcdef12"
PR_URL = "https://github.com/orbi-build/orbi/pull/7"
ISSUE_NUMBER = 42
TITLE = "Proof list vs SEO gate"
BODY = (
    "## Outcome\n\nThe homepage proof list shows five merges.\n\n"
    "## Acceptance\n\n1. It lists five proof cards.\n"
)
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
CHAT_ID = "-1001234567890"


@dataclass
class _HostConfig:
    """The host config fields a notifier reads, bound like a tick does."""

    telegram_chat_id: str | None = CHAT_ID
    message_language: str = "en"
    repo_dir: Path = Path(".")
    source_repos: tuple[str, ...] = ()


def _payload(**overrides) -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return payload


def _stop(**overrides) -> question.StopQuestion:
    return question.parse_verdict_question(_payload(**overrides))


@pytest.fixture
def gh(monkeypatch, tmp_path) -> FakeGh:
    """One repository's GitHub state behind the adapter seam."""
    fake = FakeGh(REPO)
    fake.add_issue(ISSUE_NUMBER, title=TITLE, body=BODY,
                   labels=(PR_OPENED_LABEL,))
    bind_repository(monkeypatch, fake, tmp_path)
    return fake


def _report(fake: FakeGh, stop, *, action: str = "Decide which one.",
            run_id: str | None = RUN_ID) -> str:
    """Run the real stop report against the fake repository."""
    return failure_report.report_delivery_failure(
        failure_report.HumanDecisionRequired(
            "review requires human decision: same failure repeated in 2 "
            "consecutive rounds",
            action=action, question=stop,
        ),
        issue=dict(_issue(fake)),
        source_repo=REPO, run_id=run_id, pr_url=PR_URL, worktree=None,
        branch=None, role=runner.ROLE_REVIEW, action=action,
        reason="review requires human decision", diagnosis="same failure",
        language_code="en", status_url=None, finish=False,
    )


def _issue(fake: FakeGh) -> dict:
    return fake.issues[ISSUE_NUMBER]


def _labels(fake: FakeGh) -> set[str]:
    return set(_issue(fake)["labels"])


def _bodies(fake: FakeGh, kind: str | None = None) -> list[str]:
    """The Issue's comments, optionally only those of one kind marker."""
    bodies = [comment["body"] for comment in _issue(fake)["comments"]]
    if kind is None:
        return bodies
    return [body for body in bodies if f"<!-- orbi:comment={kind} -->" in body]


def test_a_recommended_pick_best_question_resumes_the_delivery_once(gh):
    """Acceptance 2: the criterion is appended, the Issue is relabelled and
    ONE comment carries the question, the answer record and no stuck state."""
    chosen = question.recommended_option(_stop().question)
    outcome = _report(gh, _stop())

    assert outcome == requeue.OUTCOME_REQUEUED
    assert _labels(gh) == {FIX_NEEDED_LABEL}
    # The criterion landed in the ticket body the next session reads.
    assert acceptance_section.appended_marker() in _issue(gh)["body"]
    assert chosen.description in _issue(gh)["body"]
    assert acceptance_section.acceptance_heading("en") in _issue(gh)["body"]
    # Exactly ONE new comment: the `requeued` kind, the question, the answer.
    requeued = _bodies(gh, "requeued")
    assert len(requeued) == 1
    body = requeued[0]
    assert f"<!-- orbi:run={RUN_ID} -->" in body
    assert question.render_block(_stop()) in body
    answer = question.parse_answer(body)
    assert answer is not None
    assert (answer.question, answer.value, answer.action, answer.by) == (
        "q1", chosen.label, question.ANSWER_ACCEPT, "orbi",
    )
    # What the person reads: the decision, the reason, and the way back.
    assert chosen.label in body
    assert chosen.reason in body
    assert "Not right?" in body
    # No failure block: nothing shows a state a person must act on.
    assert "orbi:failure:v1" not in body


def test_a_resume_with_an_open_pr_requeues_the_review_not_the_implement(gh):
    """The criterion is appended and the SAME PR's review resumes."""
    _report(gh, _stop())
    assert PR_OPENED_LABEL not in _labels(gh)
    assert FIX_NEEDED_LABEL in _labels(gh)


def test_the_automatic_resume_reports_its_choice_over_telegram(
        monkeypatch, gh):
    """Acceptance 2 and 4: one message names the decision and the Issue."""
    with TelegramEndpoint() as endpoint:
        point_at(monkeypatch, endpoint.base_url)
        bind_notify(monkeypatch, _HostConfig(), TOKEN)
        _report(gh, _stop())
        texts = endpoint.texts()
    assert len(texts) == 1
    assert "Scope the SEO gate" in texts[0]
    assert "https://github.com/orbi-build/orbi/issues/42" in texts[0]


def test_an_authority_question_stays_a_stop(gh):
    """Acceptance 1: the same fixture as `authority` stays `ai-blocked`."""
    outcome = _report(gh, _stop(decision_class=question.DECISION_AUTHORITY))

    assert outcome == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}
    body = _bodies(gh, "failure")[0]
    assert question.render_block(
        _stop(decision_class=question.DECISION_AUTHORITY)) in body
    assert _issue(gh)["body"] == BODY


def test_the_visible_reason_is_the_questions_summary(gh):
    """Acceptance 1: the stop's one-sentence Reason is `summary`."""
    summary = _stop().question.summary
    _report(gh, _stop(decision_class=question.DECISION_AUTHORITY))
    assert summary in _bodies(gh, "failure")[0]


def test_a_question_without_a_recommendation_stays_a_stop(gh):
    """Acceptance 3: no recommended option, no automatic answer."""
    payload = _payload()
    for option in payload["question"]["options"]:
        option.pop("recommended", None)
        option.pop("reason", None)
    outcome = _report(gh, question.parse_verdict_question(payload))
    assert outcome == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}
    assert question.parse_question_block(_bodies(gh, "failure")[0]) is not None


def test_a_malformed_question_stops_without_a_block(gh, caplog):
    """Acceptance 3: the malformed question loses the block, not the stop."""
    payload = _payload()
    payload["question"]["options"] = payload["question"]["options"][:1]
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        stop = question.parse_verdict_question(payload)
    assert stop is None
    outcome = _report(gh, stop)
    assert outcome == "blocked"
    assert "question" in caplog.text
    body = _bodies(gh, "failure")[0]
    assert question.QUESTION_MARKER not in body
    assert _labels(gh) == {BLOCKED_LABEL}


def test_a_used_automatic_resume_keeps_the_next_deadlock_terminal(gh):
    """Acceptance 4: the `requeued` comment IS the one-resume counter."""
    gh.comment(ISSUE_NUMBER, (
        f"<!-- orbi:run=0badf00d -->\n"
        f"<!-- orbi:comment=requeued -->\n"
        "Orbi chose an option on its own and re-queued this delivery."
    ), login="orbi-bot")

    outcome = _report(gh, _stop())

    assert outcome == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}
    assert question.parse_question_block(_bodies(gh, "failure")[0]) is not None
    assert _issue(gh)["body"] == BODY


def test_an_issue_already_resumed_by_the_transient_requeue_stays_terminal(gh):
    """Acceptance 4: the #2202 re-queue counts as the Issue's one resume."""
    gh.comment(ISSUE_NUMBER, requeue.comment_body(
        run_id="0badf00d", reason="a transient GitHub error",
        language_code="en", status_url=None, issue=ISSUE_NUMBER, title=TITLE,
    ), login="orbi-bot")

    assert _report(gh, _stop()) == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}


def test_the_failure_block_of_a_stop_is_unchanged(gh):
    """Acceptance 8: the question rides BESIDE the failure record."""
    expected = (
        '<!-- orbi:failure:v1 {"schema":1,"outcome":"blocked",'
        '"reason_code":"human_decision_required","action_code":"decide",'
        '"retry_safe":false} -->'
    )
    _report(gh, _stop(decision_class=question.DECISION_AUTHORITY))
    assert expected in _bodies(gh, "failure")[0]
    _report(gh, None)
    with_question = [
        body for body in _bodies(gh, "failure")
        if question.QUESTION_MARKER in body
    ]
    assert len(with_question) == 1
    assert expected in _bodies(gh, "failure")[-1]


def test_the_external_pr_stop_asks_the_maintainers_three_questions(gh, monkeypatch):
    """Acceptance 5: the engine's own authority question, whatever the
    reviewer wrote."""
    work = _ExternalWork(reviewer_question=_payload()["question"])
    posted: list[str] = []
    bind_external_stop(
        monkeypatch, rollup=("SUCCESS", []), posted=posted,
        labels={PR_OPENED_LABEL},
    )

    assert review_merge._round_external_phase(work) is True

    body = posted[0]
    # Issue #2245 acceptance 1: the posted stop carries the blocked failure
    # record beside the unchanged question, so a reader that identifies a
    # stop by its failure record finds it.
    assert body.splitlines()[1] == comment_kind_marker(COMMENT_KIND_FAILURE)
    record = failure.parse(body)
    assert record is not None
    assert record.outcome == "blocked"
    assert record.reason_code in failure.REASON_CODES
    stop = question.parse_question_block(body)
    assert stop is not None
    assert stop.decision_class == question.DECISION_AUTHORITY
    assert [option.action for option in stop.question.options] == [
        question.ACTION_LINK,
        question.ACTION_CLOSE_EXTERNAL_PR,
        question.ACTION_CLOSE_ALL,
    ]
    assert stop.question.options[0].url == PR_URL
    assert not any(option.recommended for option in stop.question.options)
    # The reviewer's own pick_best question never reaches the person.
    assert "Scope the SEO gate" not in body


@dataclass
class _Parsed:
    external: bool = True
    pr_url: str = PR_URL
    review_round: int = 1


@dataclass
class _Scene:
    parsed: _Parsed


@dataclass
class _Config:
    run_id: str = RUN_ID
    status_url: str | None = None
    message_language: str = "en"


@dataclass
class _ExternalWork:
    """The `_Round` fields the external-takeover stop reads."""

    reviewer_question: dict = None
    scene: _Scene = None
    config: _Config = None
    source_repo: str = REPO
    number: int = ISSUE_NUMBER
    title: str = TITLE
    round: int = 1
    marker: str = f"<!-- orbi:run={RUN_ID} -->"
    ready_label: str = "ai-ready"
    verdict: dict = None
    session_output: str = "review report"
    pr: dict = None

    def __post_init__(self):
        self.scene = self.scene or _Scene(_Parsed())
        self.config = self.config or _Config()
        self.pr = self.pr or {"number": 7, "url": PR_URL}
        self.verdict = self.verdict or {
            "verdict": "pass", "head": "a" * 40, "blockers": 0,
            "majors": 0, "minors": 0, "findings": [],
            "question": self.reviewer_question, "decision_class": "pick_best",
        }


def test_a_deadlock_without_an_open_pr_requeues_the_ticket_ready(gh):
    """The label follows the delivery: no PR, so the implement phase runs."""
    outcome = question.report_once(
        _stop(), issue={"number": ISSUE_NUMBER, "title": TITLE, "body": BODY},
        source_repo=REPO, run_id=RUN_ID, language_code="en", status_url=None,
        pr_url=None, current_labels=None,
    )
    assert outcome == requeue.OUTCOME_REQUEUED
    assert _labels(gh) == {"ai-ready"}


def test_a_criterion_already_in_the_body_is_not_appended_twice(gh):
    chosen = question.recommended_option(_stop().question)
    amended = f"{BODY}\n\n## Acceptance\n\n- {chosen.description}\n"
    gh.issues[ISSUE_NUMBER]["body"] = amended
    assert _report(gh, _stop()) == requeue.OUTCOME_REQUEUED
    assert _issue(gh)["body"] == amended


def test_an_unreadable_history_keeps_the_stop_terminal(
        monkeypatch, gh, caplog):
    """The counter cannot be proven unused, so the stop stays terminal."""
    bind_history_failure(monkeypatch)
    with caplog.at_level("ERROR", logger="orbi.bootstrap"):
        outcome = _report(gh, _stop())
    assert outcome == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}
    assert "question history read failed" in caplog.text
    assert _issue(gh)["body"] == BODY


def test_a_review_stop_continues_the_issues_one_id_series(gh):
    """Acceptance 1+2 end to end: a stored `q2` and a reviewer that echoes
    it leave the posted block — and the answer record the engine writes
    beside it — on `q3`, so an answer names exactly one question."""
    stored = _payload()
    stored["question"]["id"] = "q2"
    gh.comment(ISSUE_NUMBER, question.render_block(
        question.parse_verdict_question(stored)))
    echoed = _payload()
    echoed["question"]["id"] = "q2"
    stop = question.review_stop(
        echoed, comments=question.question_history(ISSUE_NUMBER, REPO))[2]
    assert stop.question.id == "q3"

    assert _report(gh, stop) == requeue.OUTCOME_REQUEUED

    body = _bodies(gh, "requeued")[0]
    assert question.parse_question_block(body).question.id == "q3"
    assert question.parse_answer(body).question == "q3"


def test_a_stop_with_a_question_and_no_run_id_writes_no_resume(gh):
    """Without a run there is no comment marker, no counter and no resume."""
    outcome = _report(gh, _stop(), run_id=None)
    assert outcome == "blocked"
    assert _labels(gh) == {BLOCKED_LABEL}
    # The kind marker rides the run marker, so the comment is unkinded here.
    assert _bodies(gh, "failure") == []
    assert question.parse_question_block(_bodies(gh)[0]) is not None


def test_the_external_stop_numbers_its_question_after_the_stops_there(gh):
    gh.comment(ISSUE_NUMBER, question.render_block(_stop()))
    segment = question.external_pr_stop(
        number=ISSUE_NUMBER, source_repo=REPO, pr_url=PR_URL,
        language_code="en",
    )
    assert question.parse_question_block(segment).question.id == "q2"


def test_an_unreadable_history_still_asks_the_external_question(
        monkeypatch, gh, caplog):
    bind_history_failure(monkeypatch)
    with caplog.at_level("ERROR", logger="orbi.bootstrap"):
        segment = question.external_pr_stop(
            number=ISSUE_NUMBER, source_repo=REPO, pr_url=PR_URL,
            language_code="en",
        )
    assert question.parse_question_block(segment).question.id == "q1"
    assert "question history read failed" in caplog.text
