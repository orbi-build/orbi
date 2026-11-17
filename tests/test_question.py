"""The stop question shape and its one hidden block (Issue #2220).

A stop that needs a decision carries ONE machine-readable question: a
superset of the drafting question (`{"question", "header"?, "options"?}`,
docs/public-contracts.mdx), so every question `orbi draft` returns today
stays valid, plus the fields a stop needs (an `id` unique per Issue, a
`kind`, a one-sentence `summary` and, per option, the `action` a person
performs). The block is a separate marker (`orbi:question:v1`) because the
strict `orbi:failure:v1` parser rejects unknown fields; `>` is written as
`\u003e` inside it so no free text can close the HTML comment before its
`-->`.
"""
import json
from pathlib import Path

import pytest

from orbi import question, review_verdict

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "question"
    / "review-deadlock-1243.json"
)
DRAFT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "draft"


def _payload(**overrides) -> dict:
    """The real reviewer answer, with a test's own field overrides."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return payload


def _stop(**overrides) -> question.StopQuestion:
    return question.parse_verdict_question(_payload(**overrides))


def _strict(payload: object) -> question.StopQuestion:
    """The stop shape, read with the conservative 'authority' class."""
    return question.parse_stop(payload, question.DECISION_AUTHORITY)


def _stored(question_id: str) -> dict:
    """One Issue comment carrying a stored stop question with this id."""
    payload = _payload()
    payload["question"]["id"] = question_id
    stop = question.parse_verdict_question(payload)
    return {"body": f"a stop\n\n{question.render_block(stop)}"}


def test_the_real_reviewer_answer_parses_into_a_valid_question():
    """Acceptance 1: the captured deepseek-flash answer for
    orbi-website#1243 is a valid stop question."""
    verdict = review_verdict.parse_review_verdict(
        "REVIEW_VERDICT " + json.dumps(_payload()),
    )
    stop = question.parse_verdict_question(verdict)
    assert stop is not None
    assert stop.decision_class == question.DECISION_PICK_BEST
    assert stop.question.id == "q1"
    assert stop.question.kind == question.KIND_CHOICE
    assert stop.question.summary.startswith("The SEO gate and this ticket")
    assert stop.question.text.startswith("Which requirement should give way")
    assert [option.label for option in stop.question.options] == [
        "Scope the SEO gate", "First-party card headlines",
    ]
    chosen = question.recommended_option(stop.question)
    assert chosen is not None
    assert chosen.action == question.ACTION_AMEND_REQUEUE
    assert chosen.description.startswith(
        "The homepage title-keyword gate measures only first-party",
    )
    assert chosen.reason.startswith("Keeps the ticket's verbatim-title")


def test_the_fixture_stays_inside_the_failure_comment_budget():
    """The block is one line of a comment: the real answer must not carry
    a newline into it (the marker regex is single-line by construction)."""
    block = question.render_block(_stop())
    assert block.count("\n") == 0
    assert block.startswith(f"<!-- {question.QUESTION_MARKER} {{")
    assert block.endswith(" -->")


def test_a_pick_best_question_with_a_recommendation_is_the_engines_to_take():
    """Acceptance 1: `pick_best` + `recommended` resumes at once."""
    option = question.takeable_option(_stop())
    assert option is not None
    assert option.label == "Scope the SEO gate"


@pytest.mark.parametrize("decision_class", ["authority"])
def test_an_authority_question_is_never_taken(decision_class):
    """Acceptance 1: the same fixture as `authority` stays a stop."""
    assert question.takeable_option(
        _stop(decision_class=decision_class)) is None


def test_a_question_without_a_recommendation_is_never_taken():
    """The reviewer leaves `recommended` out when the call is close."""
    payload = _payload()
    for option in payload["question"]["options"]:
        option.pop("recommended", None)
        option.pop("reason", None)
    assert question.takeable_option(
        question.parse_verdict_question(payload)) is None


def test_an_option_the_engine_cannot_execute_in_place_is_never_taken():
    """Only `amend_requeue` is done in place; `link`/`close_*` need a
    person acting on GitHub."""
    payload = _payload()
    payload["question"]["options"][0]["action"] = question.ACTION_CLOSE_ALL
    assert question.takeable_option(
        question.parse_verdict_question(payload)) is None


def test_every_draft_question_in_the_fixtures_validates_unchanged():
    """Acceptance 6: every question `orbi draft` returns today validates
    against the shape and keeps every field it carries."""
    fixtures = sorted(DRAFT_FIXTURES.glob("*.json"))
    assert fixtures
    seen = 0
    for path in fixtures:
        document = json.loads(path.read_text(encoding="utf-8"))
        for raw in document.get("questions", ()):
            seen += 1
            parsed = question.parse_question(raw).as_json()
            assert parsed["question"] == raw["question"], path.name
            assert parsed.get("header") == raw.get("header"), path.name
            assert [
                (option["label"], option["description"])
                for option in parsed.get("options", ())
            ] == [
                (option["label"], option["description"])
                for option in raw.get("options", ())
            ], path.name
    assert seen >= 3


def test_a_recommended_label_reads_as_recommended():
    """Acceptance 6: the drafting mark reads as the field."""
    parsed = question.parse_question({
        "question": "Where should it live?",
        "options": [
            {"label": "A local file (Recommended)",
             "description": "One file next to the project."},
            {"label": "A hosted store", "description": "Needs an account."},
        ],
    })
    assert [option.recommended for option in parsed.options] == [True, False]
    explicit = question.parse_question({
        "question": "Where should it live?",
        "options": [
            {"label": "A local file", "description": "One file.",
             "recommended": True, "reason": "No setup."},
            {"label": "A hosted store", "description": "Needs an account."},
        ],
    })
    assert question.recommended_option(explicit).label == "A local file"


def test_a_drafting_question_without_options_reads_as_text():
    assert question.parse_question(
        {"question": "What should it be called?"}).kind == question.KIND_TEXT
    assert question.parse_question({
        "question": "Which one?", "header": "Choice",
        "options": [{"label": "A", "description": "First."},
                    {"label": "B", "description": "Second."}],
    }).kind == question.KIND_CHOICE


def test_a_confirm_question_carries_the_action_and_its_criterion():
    parsed = _strict({
        "id": "q2", "kind": question.KIND_CONFIRM,
        "summary": "The gate needs a decision.",
        "question": "Scope the gate to first-party headings?",
        "action": question.ACTION_AMEND_REQUEUE,
        "criterion": "The homepage gate measures only first-party headings.",
    })
    assert parsed.question.action == question.ACTION_AMEND_REQUEUE
    assert parsed.question.criterion.startswith("The homepage gate")


def test_option_text_containing_the_comment_close_round_trips():
    """Acceptance 7: `-->` inside an option cannot close the block."""
    payload = _payload()
    payload["question"]["options"][0]["description"] = (
        "The card links to the PR --> and never renders the raw title."
    )
    stop = question.parse_verdict_question(payload)
    block = question.render_block(stop)
    # The free text's `>` is escaped, so the marker regex still stops at
    # the block's OWN close and nowhere else.
    assert block.count("-->") == 1
    assert block.endswith(" -->")
    assert "--> and never renders" not in block
    parsed = question.parse_question_block(block)
    assert parsed is not None
    assert parsed.question.options[0].description == (
        "The card links to the PR --> and never renders the raw title."
    )


def test_the_answer_record_round_trips():
    block = question.render_answer("q1", value="Scope the SEO gate")
    assert block.startswith(f"<!-- {question.ANSWER_MARKER} {{")
    answer = question.parse_answer(block)
    assert answer is not None
    assert (answer.question, answer.action, answer.value, answer.by) == (
        "q1", question.ANSWER_ACCEPT, "Scope the SEO gate", "orbi",
    )
    assert answer.reason is None


@pytest.mark.parametrize("answer,expected", [
    ({"q": "q1", "action": question.ANSWER_CANCEL, "value": "drop it",
      "by": "xqliu"},
     ("q1", question.ANSWER_CANCEL, "drop it", "xqliu", None)),
    ({"q": "q2", "action": question.ANSWER_ACCEPT, "value": True,
      "by": "xqliu"},
     ("q2", question.ANSWER_ACCEPT, True, "xqliu", None)),
    ({"q": "q3", "action": question.ANSWER_DECLINE, "value": "no",
      "by": "xqliu", "reason": "the gate stays as it is"},
     ("q3", question.ANSWER_DECLINE, "no", "xqliu",
      "the gate stays as it is")),
])
def test_the_shared_answer_record_reads_every_action_and_value(
        answer, expected):
    """Acceptance 10: one shape shared with the ticket that reads a
    person's answer — `cancel`, a boolean `value` and an optional
    `reason` all parse."""
    parsed = question.parse_answer(
        f"<!-- {question.ANSWER_MARKER} {json.dumps(answer)} -->")
    assert parsed is not None
    assert (parsed.question, parsed.action, parsed.value, parsed.by,
            parsed.reason) == expected


def test_the_engine_writes_a_reason_only_when_it_has_one():
    with_reason = question.parse_answer(question.render_answer(
        "q1", value=False, action=question.ANSWER_CANCEL, by="orbi",
        reason="the call is even",
    ))
    assert (with_reason.value, with_reason.action) == (
        False, question.ANSWER_CANCEL,
    )
    assert with_reason.reason == "the call is even"
    assert "reason" not in question.render_answer("q1", value="x")


@pytest.mark.parametrize("answer", [
    {"q": "q1", "action": "accept", "value": 5, "by": "orbi"},
    {"q": "q1", "action": "accept", "value": "", "by": "orbi"},
    {"q": "q1", "action": "accept", "value": "x", "by": "orbi",
     "reason": "  "},
    {"q": "q1", "action": "accept", "value": "x"},
])
def test_an_unreadable_answer_record_is_refused(answer):
    """The shared shape is closed: a value is a label, free text or a
    boolean, and `reason` is the one optional field."""
    with pytest.raises(question.QuestionError):
        question.parse_answer(
            f"<!-- {question.ANSWER_MARKER} {json.dumps(answer)} -->")


def test_the_question_block_is_read_back_from_a_stop_comment():
    block = question.render_block(_stop())
    body = f"<!-- orbi:run=abcdef12 -->\n\nOrbi failed: still blocked.\n\n{block}"
    parsed = question.parse_question_block(body)
    assert parsed is not None
    assert parsed.question.as_json() == _stop().question.as_json()
    assert parsed.decision_class == question.DECISION_PICK_BEST
    assert question.parse_question_block("no block here") is None


@pytest.mark.parametrize("payload,reason", [
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q"},
     "a choice with no options"),
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q",
      "options": [{"label": "only", "description": "d",
                   "action": "amend_requeue"}]},
     "one option"),
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q",
      "options": [{"label": "a", "description": "d", "action": "explode"},
                  {"label": "b", "description": "d",
                   "action": "amend_requeue"}]},
     "unknown action"),
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q",
      "options": [{"label": "a", "description": "d", "action": "link"},
                  {"label": "b", "description": "d",
                   "action": "amend_requeue"}]},
     "link without url"),
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q",
      "options": [
          {"label": "a", "description": "d", "action": "amend_requeue",
           "recommended": True, "reason": "r"},
          {"label": "b", "description": "d", "action": "amend_requeue",
           "recommended": True, "reason": "r"}]},
     "two recommendations"),
    ({"id": "q1", "kind": "telepathy", "summary": "s", "question": "q",
      "action": "amend_requeue", "criterion": "c"}, "unknown kind"),
])
def test_a_malformed_stop_question_is_refused(payload, reason):
    with pytest.raises(question.QuestionError):
        _strict(payload)


@pytest.mark.parametrize("payload", [
    {"question": "Which one?", "options": [{"label": "a", "description": "d",
                                            "action": "explode"},
                                           {"label": "b", "description": "d",
                                            "action": "amend_requeue"}]},
    {"id": "q1", "summary": "s", "question": "q",
     "options": [{"label": "a", "action": "amend_requeue"},
                 {"label": "b", "description": "d",
                  "action": "amend_requeue"}]},
])
def test_a_malformed_verdict_question_drops_the_block_and_logs(payload, caplog):
    """Acceptance 3: a malformed question does not fail the verdict — the
    stop is written without a block and the parse error is logged."""
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        stop = question.parse_verdict_question(
            {"verdict": "blocked_on_human_decision", "question": payload,
             "decision_class": "pick_best"},
        )
    assert stop is None
    assert "question" in caplog.text


def test_a_verdict_without_a_question_carries_no_stop_question():
    assert question.parse_verdict_question(
        {"verdict": "blocked_on_human_decision"}) is None


def test_the_block_parser_refuses_an_unknown_field():
    body = ('<!-- orbi:question:v1 {"schema":1,"decision_class":"pick_best",'
            '"question":{},"extra":1} -->')
    with pytest.raises(question.QuestionError):
        question.parse_question_block(body)


def test_the_external_pr_question_is_authority_with_the_three_fixed_options():
    """Acceptance 5: whatever the reviewer wrote, an external-PR stop asks
    the maintainer's question with exactly these three options."""
    stop = question.external_pr_question(
        "https://github.com/orbi-build/orbi/pull/7", question_id="q1",
        language_code="en",
    )
    assert stop.decision_class == question.DECISION_AUTHORITY
    assert [option.action for option in stop.question.options] == [
        question.ACTION_LINK, question.ACTION_CLOSE_EXTERNAL_PR,
        question.ACTION_CLOSE_ALL,
    ]
    assert stop.question.options[0].url == (
        "https://github.com/orbi-build/orbi/pull/7"
    )
    assert not any(option.recommended for option in stop.question.options)
    assert question.takeable_option(stop) is None


def test_the_visible_options_name_every_choice_and_the_recommendation():
    visible = question.render_visible(_stop(), "en")
    assert "Scope the SEO gate" in visible
    assert "First-party card headlines" in visible
    assert "Recommended" in visible
    assert _stop().question.text in visible


def test_next_id_is_one_past_the_highest_id_not_the_comment_count():
    """Acceptance 1: `id` is unique per Issue. A stored `q2` with no `q1`
    must not hand `q2` out again, so the highest id — not the number of
    stop comments — is what the next one follows."""
    assert question.next_id([_stored("q2")]) == "q3"
    assert question.next_id([_stored("q1"), _stored("q3")]) == "q4"
    assert question.next_id([]) == "q1"


def test_next_id_skips_an_unreadable_block_and_an_id_outside_the_series():
    """The allocator never fails the stop that needs the id: a block it
    cannot read carries no id it could reissue, and an id that is not a
    `q<n>` can never collide with the series it writes."""
    corrupt = {"body": "<!-- orbi:question:v1 {not json -->"}
    assert question.next_id([corrupt, _stored("x1")]) == "q1"


def test_a_review_stop_reusing_a_stored_id_gets_a_new_one():
    """Acceptance 2: the reviewer's echoed id is never posted twice."""
    payload = _payload()
    payload["question"]["id"] = "q2"
    stop = question.review_stop(payload, comments=[_stored("q2")])[2]
    assert stop.question.id == "q3"


def test_a_review_stop_needs_no_id_from_the_reviewer():
    """Acceptance 4: the prompt no longer asks the model for an id, so the
    engine assigns the next one itself."""
    payload = _payload()
    payload["question"].pop("id")
    stop = question.review_stop(payload, comments=())[2]
    assert stop.question.id == "q1"


def test_the_external_stop_and_a_review_deadlock_get_different_ids():
    """Acceptance 3: whichever stop writes second, the two ids differ."""
    external = question.external_pr_stop(
        number=42, source_repo="orbi-build/orbi",
        pr_url="https://github.com/orbi-build/orbi/pull/7",
        language_code="en", comments=(),
    )
    assert question.parse_question_block(external).question.id == "q1"
    review = question.review_stop(
        _payload(), comments=[{"body": external}])[2]
    assert review.question.id == "q2"


def test_a_stored_block_still_needs_the_id_the_engine_wrote():
    """Only a reviewer's verdict question may arrive without an id: the
    block on the Issue is the engine's own and carries it."""
    payload = _payload()["question"]
    payload.pop("id")
    with pytest.raises(question.QuestionError, match="needs an id"):
        question.parse_stop(payload, question.DECISION_AUTHORITY)


@pytest.mark.parametrize("payload", [
    "not an object",
    {"question": ""},
    {"question": "q", "header": "   "},
    {"question": "q", "options": "two"},
    {"question": "q", "options": ["a", "b"]},
    {"question": "q", "options": [{"description": "d"},
                                  {"label": "b", "description": "d"}]},
    {"question": "q", "options": [{"label": "a"},
                                  {"label": "b", "description": "d"}]},
    {"question": "q", "options": [
        {"label": "a", "description": "d", "recommended": "yes"},
        {"label": "b", "description": "d"}]},
    {"question": "q", "options": [
        {"label": "a", "description": "d", "url": 5},
        {"label": "b", "description": "d"}]},
    {"question": "q", "options": [
        {"label": "a", "description": "d", "reason": 5},
        {"label": "b", "description": "d"}]},
    {"question": "q", "kind": "text", "options": [
        {"label": "a", "description": "d"},
        {"label": "b", "description": "d"}]},
    {"question": "q", "options": [
        {"label": "a", "description": "d"},
        {"label": "b", "description": "d"}], "action": "amend_requeue"},
    {"question": "q", "options": [
        {"label": "a", "description": "d"},
        {"label": "b", "description": "d"}], "criterion": "c"},
    {"question": "q", "kind": "confirm", "action": "link",
     "criterion": "c"},
    {"question": "q", "kind": "confirm", "action": "amend_requeue"},
])
def test_an_unreadable_question_is_refused(payload):
    """The shape is closed: every field it names is validated."""
    with pytest.raises(question.QuestionError):
        question.parse_question(payload)


def test_a_confirm_question_round_trips_with_its_action_and_criterion():
    payload = {
        "id": "q2", "kind": question.KIND_CONFIRM,
        "summary": "The gate needs a decision.",
        "question": "Scope the gate to first-party headings?",
        "action": question.ACTION_AMEND_REQUEUE,
        "criterion": "The homepage gate measures first-party headings.",
    }
    parsed = question.parse_question(payload, strict=True)
    assert parsed.as_json() == payload
    assert parsed.as_json().get("criterion")


def test_a_text_question_round_trips_without_options():
    payload = {"question": "What should it be called?"}
    parsed = question.parse_question(payload)
    assert parsed.kind == question.KIND_TEXT
    assert parsed.text == payload["question"]
    assert parsed.as_json() == {
        "question": payload["question"], "kind": question.KIND_TEXT,
    }


def test_a_stored_block_needs_its_own_schema():
    body = ('<!-- orbi:question:v1 {"schema":2,"decision_class":"pick_best",'
            '"question":{}} -->')
    with pytest.raises(question.QuestionError):
        question.parse_question_block(body)


@pytest.mark.parametrize("decision_class", [None, "telepathy"])
def test_an_unusable_decision_class_reads_as_authority(decision_class, caplog):
    """A missing or unknown class never lets the engine answer itself."""
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        stop = question.parse_verdict_question(
            {"question": _payload()["question"],
             "decision_class": decision_class},
        )
    assert stop.decision_class == question.DECISION_AUTHORITY
    assert question.takeable_option(stop) is None


def test_an_unreadable_decision_class_is_logged(caplog):
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        question.parse_verdict_question(
            {"question": _payload()["question"], "decision_class": "telepathy"},
        )
    assert "decision_class" in caplog.text


def test_a_non_verdict_carries_no_question():
    assert question.parse_verdict_question(None) is None


@pytest.mark.parametrize("payload,decision_class", [
    ({"id": "q1", "kind": "choice", "summary": "s", "question": "q"},
     "authority"),
    ({"id": "q1", "summary": "s", "question": "q",
      "options": [{"label": "a", "description": "d",
                   "action": "amend_requeue"},
                  {"label": "b", "description": "d",
                   "action": "amend_requeue"}]}, "telepathy"),
])
def test_a_stored_block_without_its_stop_fields_is_refused(
        payload, decision_class):
    with pytest.raises(question.QuestionError):
        question.parse_stop(payload, decision_class)


def test_the_answer_record_refuses_an_unknown_action():
    with pytest.raises(ValueError):
        question.render_answer("q1", value="x", action="maybe")
    body = ('<!-- orbi:answer:v1 {"q":"q1","action":"maybe",'
            '"value":"x","by":"orbi"} -->')
    with pytest.raises(question.QuestionError):
        question.parse_answer(body)
    assert question.parse_answer("no record here") is None


def test_next_id_ignores_a_comment_without_a_body():
    assert question.next_id([{"body": None}, {"body": "no block"}]) == "q1"


def test_the_visible_copy_of_a_confirm_question_states_both_answers():
    stop = _strict({
        "id": "q3", "kind": question.KIND_CONFIRM,
        "summary": "The gate needs a decision.",
        "question": "Scope the gate to first-party headings?",
        "action": question.ACTION_AMEND_REQUEUE,
        "criterion": "The homepage gate measures first-party headings.",
    })
    visible = question.render_visible(stop, "en")
    assert "Scope the gate to first-party headings?" in visible
    assert "The homepage gate measures first-party headings." in visible
    assert question.render_block(stop) in question.render_stop(stop, "en")


def test_a_confirm_with_a_close_action_renders_without_a_criterion():
    stop = _strict({
        "id": "q4", "kind": question.KIND_CONFIRM,
        "summary": "Drop the work?",
        "question": "Close this Issue?",
        "action": question.ACTION_CLOSE_ALL,
    })
    assert "Close this Issue?" in question.render_visible(stop, "en")


def test_a_hand_built_recommendation_without_a_reason_still_renders():
    """The shape requires the reason; a hand-built option proves the
    renderer never prints a placeholder instead of it."""
    stop = question.StopQuestion(question.DECISION_AUTHORITY, question.Question(
        id=None, kind=question.KIND_CHOICE, summary=None, text="Which one?",
        options=(
            question.Option("a", "First.", question.ACTION_AMEND_REQUEUE,
                            recommended=True),
            question.Option("b", "Second.", question.ACTION_AMEND_REQUEUE),
        ),
    ))
    assert "Recommended" in question.render_visible(stop, "en")
    body = question.comment_body(
        stop, stop.question.options[0], issue={"number": 1},
        run_id="abcdef12", language_code="en", status_url=None,
    )
    assert question.parse_answer(body).question == "q1"


def test_the_review_stop_keeps_the_1701_message_and_action():
    payload = _payload()
    message, action, stop = question.review_stop(payload)
    assert message.startswith("review requires human decision: note: ")
    assert action.startswith("The person must choose")
    assert stop is not None

    message, action, stop = question.review_stop({"findings": []})
    assert message == "review requires human decision: "
    assert action == ""
    assert stop is None


def test_stop_reason_prefers_the_summary_and_falls_back():
    stop = _stop()
    assert question.stop_reason(stop, "fallback") == stop.question.summary
    summaryless = question.StopQuestion(
        question.DECISION_AUTHORITY,
        question.Question(id="q1", kind=question.KIND_TEXT, summary=None,
                          text="q"),
    )
    assert question.stop_reason(summaryless, "fallback") == "fallback"
    assert question.stop_reason(None, "fallback") == "fallback"


def test_amended_body_appends_once_and_joins_an_existing_section():
    criterion = "The homepage gate measures first-party headings."
    body = "## Outcome\n\nSomething.\n"
    amended = question.amended_body(body, criterion, "en")
    assert amended.count(criterion) == 1
    assert question.amended_body(amended, criterion, "en") is None

    marker = __import__(
        "orbi.acceptance_section", fromlist=["appended_marker"],
    ).appended_marker()
    sectioned = f"{body}\n\n## Acceptance\n\n- one\n\n{marker}\n"
    joined = question.amended_body(sectioned, criterion, "en")
    assert joined.count(marker) == 1
    assert f"- {criterion}" in joined
    assert question.amended_body(None, criterion, "en").startswith(
        "## Acceptance",
    )


@pytest.mark.parametrize("payload", [
    {"id": "q1", "summary": "s", "question": "q",
     "options": [{"label": "a", "description": "d"},
                 {"label": "b", "description": "d",
                  "action": "amend_requeue"}]},
    {"id": "q1", "summary": "s", "question": "q",
     "options": [{"label": "a", "description": "d",
                  "action": "amend_requeue", "recommended": True},
                 {"label": "b", "description": "d",
                  "action": "amend_requeue"}]},
    {"summary": "s", "question": "q"},
    {"id": "q1", "question": "q"},
])
def test_a_stop_question_missing_its_own_fields_is_refused(payload):
    """The strict rules: an action per option, a reason on a
    recommendation, and the id + summary a stop needs."""
    with pytest.raises(question.QuestionError):
        _strict(payload)


def test_a_confirm_question_must_name_what_yes_does():
    with pytest.raises(question.QuestionError):
        question.parse_question(
            {"question": "q", "kind": question.KIND_CONFIRM,
             "criterion": "c"}, strict=True,
        )


@pytest.mark.parametrize("payload", [
    {"question": "q"},
    {"id": "q1", "question": "q"},
])
def test_a_stored_block_missing_its_own_fields_is_refused(payload):
    with pytest.raises(question.QuestionError):
        question.parse_stop(payload, "pick_best")


def test_a_question_the_engine_cannot_execute_stays_with_the_person():
    """Only a choice the engine can act on in place is taken."""
    confirm = question.parse_stop({
        "id": "q5", "kind": question.KIND_CONFIRM,
        "summary": "Decide.",
        "question": "Append the criterion?",
        "action": question.ACTION_AMEND_REQUEUE,
        "criterion": "The gate measures first-party headings.",
    }, question.DECISION_PICK_BEST)
    assert question.takeable_option(confirm) is None
    text = _strict({
        "id": "q6", "summary": "Need a name.", "question": "What name?",
    })
    assert question.takeable_option(text) is None


def test_the_external_stop_reads_the_history_it_is_given():
    """A caller that already holds the comments passes them in."""
    segment = question.external_pr_stop(
        number=42, source_repo="orbi-build/orbi",
        pr_url="https://github.com/orbi-build/orbi/pull/7",
        language_code="en", comments=(),
    )
    assert question.parse_question_block(segment).question.id == "q1"
