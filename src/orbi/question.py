#!/usr/bin/env python3
r"""One structured question per stop, and the one answer the engine gives itself.

A stop that needs a decision — a review deadlock, an external PR that passed
review — carries ONE machine-readable question in a hidden
'<!-- orbi:question:v1 ... -->' block beside the unchanged
'orbi:failure:v1' record (Issue #2220). The shape is a superset of the
drafting question in 'docs/public-contracts.mdx' ('question', optional
'header' and 2-4 'options'), so every question 'orbi draft' returns today
stays valid, plus what a stop needs:

- 'id' — unique per Issue, so a later stop never reuses an earlier id.
  The engine assigns it (`next_id`) from the questions already stored on
  the Issue: the reviewer's verdict question arrives without one, so the
  model cannot pick an id another stop already wrote (Issue #2227);
- 'kind' — 'choice', 'confirm' or 'text'; absent reads as 'choice' when
  'options' is present and 'text' otherwise, exactly like drafting;
- 'summary' — one plain sentence with the real cause, which is also the
  visible Reason line;
- per option a 'label', a 'description', the 'action' a reader performs and,
  on at most one option, 'recommended: true' with a one-line 'reason'.

The action set is closed: 'amend_requeue' (append the description as an
acceptance criterion, then re-queue), 'link' (with a 'url'; a person acts on
GitHub), 'close_external_pr' and 'close_all'.

A question the engine may answer itself is a review deadlock whose
'decision_class' is not 'authority' and whose recommended option performs
'amend_requeue'. That answer happens ONCE, in place, at the moment the stop
report is written ('report_once', the same place and the same rule as the one
automatic transient re-queue of Issue #2202): append the criterion, post ONE
'requeued' comment carrying the question and an 'orbi:answer:v1' record, and
relabel the Issue for delivery instead of 'ai-blocked'. That comment IS the
counter — an Issue gets at most one automatic resume over its life, the #2202
re-queue included (the 'base_ci_red' and 'checks_permission_missing'
self-recoveries write no 'requeued' comment and are not counted). No counter
file, no scan of 'ai-blocked', no new state store.

The answer record ('orbi:answer:v1') is one shape shared with the ticket
that reads a person's answer: 'action' is 'accept', 'decline' or 'cancel',
'value' is the chosen option's label, free text or a boolean (a 'confirm'
answer), and 'reason' is optional.

Every writer escapes '>' as the JSON escape '\u003e' inside the block: the
block is one line, and no free text may close the HTML comment before its own
'-->'.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from orbi import acceptance_section, language, notify, requeue, user_summary
from orbi.comment_kind import (
    COMMENT_KIND_REQUEUED, comment_kind_marker, parse_versioned_block,
    # `answer.py` reads this pattern through this module.
    versioned_block_pattern,
)
from orbi.delivery_labels import EVENT_FIX_NEEDED, EVENT_REQUEUE, READY_LABEL
from orbi.github import (
    apply_label_patch, comment_issue, issue_comments, issue_labels,
)
from orbi.journal import LOGGER, event
from orbi.progress import run_marker

QUESTION_MARKER = "orbi:question:v1"
ANSWER_MARKER = "orbi:answer:v1"
SCHEMA = 1

# The one closed set of decision classes a reviewer chooses from: who is
# allowed to answer. 'authority' is exactly three cases (the reviewer prompt
# states them); existing tests, gates and checks are NOT maintainer decisions.
DECISION_PICK_BEST = "pick_best"
DECISION_MISSING_INFO = "missing_info"
DECISION_AUTHORITY = "authority"
DECISION_CLASSES = (
    DECISION_PICK_BEST, DECISION_MISSING_INFO, DECISION_AUTHORITY,
)

KIND_CHOICE = "choice"
KIND_CONFIRM = "confirm"
KIND_TEXT = "text"
KINDS = (KIND_CHOICE, KIND_CONFIRM, KIND_TEXT)

ACTION_AMEND_REQUEUE = "amend_requeue"
ACTION_LINK = "link"
ACTION_CLOSE_EXTERNAL_PR = "close_external_pr"
ACTION_CLOSE_ALL = "close_all"
ACTIONS = (
    ACTION_AMEND_REQUEUE, ACTION_LINK, ACTION_CLOSE_EXTERNAL_PR,
    ACTION_CLOSE_ALL,
)

# The drafting mark the shape reads back as the field (Issue #2198).
RECOMMENDED_MARK = "(Recommended)"

ANSWER_ACCEPT = "accept"
ANSWER_DECLINE = "decline"
ANSWER_CANCEL = "cancel"
ANSWER_ACTIONS = (ANSWER_ACCEPT, ANSWER_DECLINE, ANSWER_CANCEL)

QUESTION_FIELDS = ("schema", "decision_class", "question")
# One answer record, shared with the ticket that reads a person's answer:
# 'reason' is the one optional field, the rest is what every record carries.
ANSWER_FIELDS = ("q", "action", "value", "by", "reason")
ANSWER_REQUIRED = ("q", "action", "value", "by")


class QuestionError(ValueError):
    """One unreadable question (the shape refused it)."""


@dataclass(frozen=True)
class Option:
    """One choice: a short label, what it means, and what taking it does."""

    label: str
    description: str
    action: str | None = None
    url: str | None = None
    recommended: bool = False
    reason: str | None = None

    def as_json(self) -> dict:
        data: dict = {"label": self.label, "description": self.description}
        if self.action is not None:
            data["action"] = self.action
        if self.url is not None:
            data["url"] = self.url
        if self.recommended:
            data["recommended"] = True
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class Question:
    """One question: its identity, its kind, its cause and its options."""

    id: str | None
    kind: str
    summary: str | None
    text: str
    options: tuple[Option, ...] = ()
    action: str | None = None
    criterion: str | None = None
    header: str | None = None

    def as_json(self) -> dict:
        data: dict = {}
        if self.id is not None:
            data["id"] = self.id
        data["kind"] = self.kind
        if self.summary is not None:
            data["summary"] = self.summary
        data["question"] = self.text
        if self.header is not None:
            data["header"] = self.header
        if self.action is not None:
            data["action"] = self.action
        if self.criterion is not None:
            data["criterion"] = self.criterion
        if self.options:
            data["options"] = [option.as_json() for option in self.options]
        return data


@dataclass(frozen=True)
class StopQuestion:
    """One stop's question: who may answer it, and the question itself."""

    decision_class: str
    question: Question


@dataclass(frozen=True)
class Answer:
    """One recorded answer: which question, who, and what was taken.

    'value' is the chosen option's label, free text, or a boolean (a
    'confirm' answer); 'reason' is the one optional field.
    """

    question: str
    action: str
    value: str | bool
    by: str
    reason: str | None = None


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _non_empty(value, field)


def _action(value: object, field: str) -> str:
    action = _non_empty(value, field)
    if action not in ACTIONS:
        raise QuestionError(f"{field} must be one of {list(ACTIONS)}")
    return action


def _option(entry: object, position: int, *, strict: bool) -> Option:
    if not isinstance(entry, dict):
        raise QuestionError(f"option {position} is not an object")
    label = _non_empty(entry.get("label"), f"option {position} label")
    description = _non_empty(
        entry.get("description"), f"option {position} description")
    action = entry.get("action")
    if action is None:
        if strict:
            raise QuestionError(f"option {position} has no action")
    else:
        action = _action(action, f"option {position} action")
    url = _optional_text(entry.get("url"), f"option {position} url")
    if action == ACTION_LINK and url is None:
        raise QuestionError(f"option {position} action link needs a url")
    recommended = entry.get("recommended")
    if recommended is not None and not isinstance(recommended, bool):
        raise QuestionError(f"option {position} recommended must be a boolean")
    reason = _optional_text(entry.get("reason"), f"option {position} reason")
    # The drafting mark reads as the field (Issue #2198): one shape for
    # drafting and stops.
    if recommended is None:
        recommended = label.endswith(RECOMMENDED_MARK)
    if recommended and reason is None and strict:
        raise QuestionError(f"option {position} recommendation needs a reason")
    return Option(
        label=label, description=description, action=action, url=url,
        recommended=recommended, reason=reason,
    )


def parse_question(payload: object, *, strict: bool = False) -> Question:
    """Validate one question — the drafting shape and the stop shape.

    Absent 'kind' reads as 'choice' when 'options' is present and 'text'
    otherwise, exactly how a drafting question reads today; a 'choice' needs
    2-4 options and at most one of them recommended. 'strict' is the stop's
    own rule: every option names its 'action', a recommendation carries its
    'reason', and a 'confirm' carries the 'action' "yes" performs (and, for
    'amend_requeue', the acceptance 'criterion').
    """
    if not isinstance(payload, dict):
        raise QuestionError("the question is not an object")
    text = _non_empty(payload.get("question"), "question")
    header = _optional_text(payload.get("header"), "header")
    raw_options = payload.get("options")
    if raw_options is None:
        raw_options = ()
    if not isinstance(raw_options, (list, tuple)):
        raise QuestionError("options must be a list")
    kind = payload.get("kind")
    if kind is None:
        kind = KIND_CHOICE if raw_options else KIND_TEXT
    if kind not in KINDS:
        raise QuestionError(f"kind must be one of {list(KINDS)}")
    options = tuple(
        _option(entry, position, strict=strict)
        for position, entry in enumerate(raw_options, start=1)
    )
    if kind == KIND_CHOICE and not 2 <= len(options) <= 4:
        raise QuestionError("a choice needs 2 to 4 options")
    if kind != KIND_CHOICE and options:
        raise QuestionError(f"a {kind} question carries no options")
    if sum(option.recommended for option in options) > 1:
        raise QuestionError("at most one option may be recommended")
    action = payload.get("action")
    criterion = _optional_text(payload.get("criterion"), "criterion")
    if kind == KIND_CONFIRM:
        if action is None:
            raise QuestionError(
                "a confirm question needs the action yes performs")
        action = _action(action, "action")
        if action == ACTION_LINK:
            raise QuestionError("a confirm question cannot link")
        if action == ACTION_AMEND_REQUEUE and criterion is None:
            raise QuestionError(
                "a confirm question needs the criterion to append")
    elif action is not None or criterion is not None:
        raise QuestionError(f"a {kind} question carries no action or criterion")
    return Question(
        id=_optional_text(payload.get("id"), "id"), kind=kind,
        summary=_optional_text(payload.get("summary"), "summary"), text=text,
        options=options, action=action, criterion=criterion, header=header,
    )


def parse_verdict_question(verdict: object) -> StopQuestion | None:
    """The question a REVIEW_VERDICT carries, or None.

    A question is optional and its readability never decides the verdict: a
    malformed one — the stop's own 'id' and 'summary' included — is logged
    and dropped, so the delivery still stops and the ticket simply carries
    no block. A missing or unknown 'decision_class' reads as 'authority' —
    the conservative class the engine never answers itself — and is logged
    too.
    """
    payload = verdict.get("question") if isinstance(verdict, dict) else None
    if payload is None:
        return None
    decision_class = verdict.get("decision_class")
    if decision_class not in DECISION_CLASSES:
        LOGGER.warning(
            "stop question has no usable decision_class (%r): reading it as "
            "authority, so the engine never answers it itself",
            decision_class,
        )
        decision_class = DECISION_AUTHORITY
    try:
        return parse_stop(payload, decision_class, require_id=False)
    except QuestionError as exc:
        LOGGER.warning("unreadable stop question: %s", exc)
        return None


def parse_stop(payload: object, decision_class: object, *,
               require_id: bool = True) -> StopQuestion:
    """A stored stop block: the strict question plus its decision class.

    A block stored on the Issue always carries the id the engine wrote
    (`require_id`); a reviewer's verdict question does not — the engine
    allocates it (`review_stop`), so the model never chooses an id.
    """
    if decision_class not in DECISION_CLASSES:
        raise QuestionError(
            f"decision_class must be one of {list(DECISION_CLASSES)}")
    question = parse_question(payload, strict=True)
    if require_id and question.id is None:
        raise QuestionError("a stop question needs an id")
    if question.summary is None:
        raise QuestionError("a stop question needs a summary")
    return StopQuestion(decision_class, question)


def recommended_option(question: Question) -> Option | None:
    """The option marked recommended, or None (at most one by the shape)."""
    return next(
        (option for option in question.options if option.recommended), None,
    )


def takeable_option(stop: StopQuestion | None) -> Option | None:
    """The option the engine may execute itself, or None.

    A review deadlock is the person's only when the class is 'authority';
    within the other two classes the engine takes the recommended option it
    can actually execute in place ('amend_requeue'). 'link', 'close_*' and a
    'confirm' (no recommendation to read) stay with the person.
    """
    if stop is None or stop.decision_class == DECISION_AUTHORITY:
        return None
    if stop.question.kind != KIND_CHOICE:
        return None
    option = recommended_option(stop.question)
    if option is None or option.action != ACTION_AMEND_REQUEUE:
        return None
    return option


def _render_block(marker: str, payload: dict) -> str:
    """One hidden block on one line, with '>' escaped inside the JSON.

    '>' can only sit inside a JSON string, so escaping every one keeps free
    text from closing the HTML comment: the marker regex stops at the block's
    own '-->' and nowhere else.
    """
    text = json.dumps(payload, ensure_ascii=False)
    return f"<!-- {marker} {text.replace('>', chr(92) + 'u003e')} -->"


def render_block(stop: StopQuestion) -> str:
    """The stop's one 'orbi:question:v1' block."""
    return _render_block(QUESTION_MARKER, {
        "schema": SCHEMA,
        "decision_class": stop.decision_class,
        "question": stop.question.as_json(),
    })


def parse_question_block(body: object) -> StopQuestion | None:
    """The stop question a comment body carries, or None."""
    data = parse_versioned_block(
        body, QUESTION_MARKER, QUESTION_FIELDS, QuestionError,
    )
    if data is None:
        return None
    if data["schema"] != SCHEMA:
        raise QuestionError(f"unsupported question schema: {data['schema']!r}")
    return parse_stop(data["question"], data["decision_class"])


def _answer_value(value: object) -> str | bool:
    """An answer's 'value': an option label, free text, or a boolean."""
    return value if isinstance(value, bool) else _non_empty(value, "value")


def render_answer(question_id: str, *, value: str | bool,
                  action: str = ANSWER_ACCEPT, by: str = "orbi",
                  reason: str | None = None) -> str:
    """One answer record: who took which option of which question."""
    if action not in ANSWER_ACTIONS:
        raise ValueError(f"unknown answer action: {action!r}")
    data: dict = {
        "q": _non_empty(question_id, "q"), "action": action,
        "value": _answer_value(value), "by": _non_empty(by, "by"),
    }
    if reason is not None:
        data["reason"] = _non_empty(reason, "reason")
    return _render_block(ANSWER_MARKER, data)


def _answer_record(data: dict) -> Answer:
    """One validated answer object — the one field set every reader shares."""
    if data["action"] not in ANSWER_ACTIONS:
        raise QuestionError(f"unknown answer action: {data['action']!r}")
    return Answer(
        question=_non_empty(data["q"], "q"), action=data["action"],
        value=_answer_value(data["value"]),
        by=_non_empty(data["by"], "by"),
        reason=_optional_text(data.get("reason"), "reason"),
    )


def parse_answer(body: object) -> Answer | None:
    """The answer record a comment body carries, or None."""
    data = parse_versioned_block(
        body, ANSWER_MARKER, ANSWER_FIELDS, QuestionError,
        required=ANSWER_REQUIRED,
    )
    if data is None:
        return None
    return _answer_record(data)


def parse_answer_payload(payload: object) -> Answer:
    """One answer record from its JSON payload (Issue #2221).

    The ticket-command scan reads a record command's occurrence as the
    block's payload text, so this is the same shape and the same rules as
    `parse_answer` — one answer parser, two entry points — and a
    corrupted record raises `QuestionError` instead of being ignored.
    """
    if not isinstance(payload, str) or not payload.strip():
        raise QuestionError("the answer record is empty")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QuestionError(
            f"the answer record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QuestionError("the answer record must be a JSON object")
    unknown = sorted(set(data) - set(ANSWER_FIELDS))
    if unknown:
        raise QuestionError(f"the answer record has unknown fields: {unknown}")
    missing = sorted(name for name in ANSWER_REQUIRED if name not in data)
    if missing:
        raise QuestionError(f"the answer record is missing fields: {missing}")
    return _answer_record(data)


def latest_stop(
    comments: Sequence[dict] | None,
) -> tuple[dict, StopQuestion] | None:
    """The newest comment carrying a stop question, and that question.

    The ticket's OPEN question is the newest one the engine wrote, so an
    answer is read against it (Issue #2221). A body whose block is present
    but unreadable raises `QuestionError`: the engine wrote it, so a
    corrupted one is reported, never silently skipped.
    """
    for comment in reversed(list(comments or ())):
        if not isinstance(comment, dict):
            continue
        stop = parse_question_block(comment.get("body"))
        if stop is not None:
            return comment, stop
    return None


# The one id series: a stored id outside it is never reissued by the
# allocator, so reading it cannot collide with what the allocator writes.
_SERIES_ID = re.compile(r"^q(\d+)$")


def _stored_ids(comments: Sequence[dict] | None) -> list[str]:
    """The question ids already stored on the Issue, read tolerantly.

    An unreadable block carries no id this allocator could reissue, so it is
    skipped: allocating the next id must never fail the stop that needs it.
    """
    ids: list[str] = []
    for comment in comments or ():
        body = comment.get("body") if isinstance(comment, dict) else None
        try:
            stop = parse_question_block(body)
        except QuestionError:
            continue
        if stop is not None and stop.question.id is not None:
            ids.append(stop.question.id)
    return ids


def next_id(comments: Sequence[dict] | None) -> str:
    """The id of the next question on an Issue: one past the highest one.

    The highest, never the number of stop comments: the stops do not write
    ids the same way (the reviewer used to choose one, the external-PR stop
    counts), so counting re-issued an id already taken — one stored `q2`
    and no `q1` gave `q2` again (Issue #2227). Every stop now reads the
    stored ids and continues their one series.
    """
    numbers = [
        int(match.group(1))
        for match in map(_SERIES_ID.match, _stored_ids(comments))
        if match is not None
    ]
    return f"q{max(numbers, default=0) + 1}"


def render_visible(stop: StopQuestion, language_code: str) -> str:
    """What the person reads: the question, every option and the way back."""
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    question = stop.question
    lines = [message("question.visible_title", question=question.text)]
    for option in question.options:
        line = message(
            "question.option_line", label=option.label,
            description=option.description,
        )
        if option.recommended:
            line += message(
                "question.recommended", reason=option.reason or "",
            )
        lines.append(line)
    if question.kind == KIND_CONFIRM:
        lines.append(message(
            "question.confirm_yes", criterion=question.criterion or "",
        ))
        lines.append(message("question.confirm_no"))
    lines.append(message("question.answer_hint"))
    return "\n".join(lines)


def render_stop(stop: StopQuestion, language_code: str) -> str:
    """The visible question AND its hidden block, as one comment segment."""
    return f"{render_visible(stop, language_code)}\n\n{render_block(stop)}"


def stop_reason(stop: StopQuestion | None, default: str) -> str:
    """The visible Reason of a stop: the question's summary when it has one."""
    return (stop.question.summary or default) if stop is not None else default


def review_stop(verdict: dict, *, comments: Sequence[dict] | None = None,
               ) -> tuple[str, str, StopQuestion | None]:
    """The (message, action, question) of a blocked_on_human_decision verdict.

    The message keeps the #1701 shape the exception text has always had (the
    finding's note and fix), the action is the fix direction the stop report
    renders, and the question is the reviewer's own — None when it wrote none
    or wrote one the shape cannot read. Its id is the engine's own, one past
    the highest id already on the Issue: the model writes no id, so a stop
    that echoes an earlier one can never reuse it (Issue #2227).
    """
    findings = verdict["findings"]
    decisions = "; ".join(
        f"note: {finding.get('note', '')}; fix: {finding.get('fix', '')}"
        for finding in findings
    )
    actions = "; ".join(
        str(finding.get("fix", "")).strip()
        for finding in findings
        if str(finding.get("fix", "")).strip()
    )
    stop = parse_verdict_question(verdict)
    if stop is not None:
        stop = StopQuestion(
            stop.decision_class,
            replace(stop.question, id=next_id(comments)),
        )
    return ("review requires human decision: " + decisions, actions, stop)


def _fixed_option(language_code: str, key: str, action: str, *,
                  url: str | None = None) -> Option:
    """One engine-fixed option: the text follows the language, the action not."""
    return Option(
        label=language.text(language_code, f"{key}_label"),
        description=language.text(language_code, f"{key}_description"),
        action=action, url=url,
    )


def external_pr_question(pr_url: str, *, question_id: str,
                         language_code: str) -> StopQuestion:
    """The maintainer's question for a contribution that passed review.

    The engine fixes this one, whatever the reviewer wrote: accepting code
    from outside contributors is exactly the 'authority' class, and the
    three options are the three things a maintainer can do with a PR that
    Orbi itself will not merge. No option is recommended.
    """
    return StopQuestion(DECISION_AUTHORITY, Question(
        id=question_id, kind=KIND_CHOICE,
        summary=language.text(language_code, "question.external_summary"),
        text=language.text(language_code, "question.external_text"),
        options=(
            _fixed_option(language_code, "question.external_link",
                          ACTION_LINK, url=pr_url),
            _fixed_option(language_code, "question.external_close_pr",
                          ACTION_CLOSE_EXTERNAL_PR),
            _fixed_option(language_code, "question.external_close_all",
                          ACTION_CLOSE_ALL),
        ),
    ))


def question_history(number: int, source_repo: str) -> Sequence[dict]:
    """The Issue's comments, for allocating the next question id, or ().

    Unique ids are per Issue, so the allocator reads the Issue's history. A
    failed read must never fail the stop itself: it logs and allocates from
    an empty history, exactly as the failure report keeps a stop terminal
    when the same read fails.
    """
    try:
        return issue_comments(number, repo=source_repo)
    except Exception:
        LOGGER.exception("issue=%s question history read failed", number)
        event("question_history_read_failed", issue=number)
        return ()


def external_pr_stop(*, number: int, source_repo: str, pr_url: str,
                     language_code: str,
                     comments: Sequence[dict] | None = None) -> str:
    """The external-PR stop's question segment (visible options + block)."""
    if comments is None:
        comments = question_history(number, source_repo)
    stop = external_pr_question(
        pr_url, question_id=next_id(comments), language_code=language_code,
    )
    return render_stop(stop, language_code)


def comment_body(stop: StopQuestion, option: Option, *, issue: dict,
                 run_id: str, language_code: str,
                 status_url: str | None) -> str:
    """The ONE automatic-choice comment: what, why, the question, the answer."""
    body = language.text(
        language_code, "question.auto_resume", label=option.label,
        reason=option.reason or "",
    )
    body = user_summary.append_summary(
        body, status_url=status_url, issue=int(issue["number"]),
        title=issue.get("title"), language_code=language_code,
    )
    return "\n".join((
        run_marker(run_id),
        comment_kind_marker(COMMENT_KIND_REQUEUED),
        body,
        "",
        render_block(stop),
        render_answer(stop.question.id or "q1", value=option.label),
    ))


def amended_body(body: object, criterion: str,
                 language_code: str) -> str | None:
    """'body' with 'criterion' as an acceptance criterion, or None if there.

    The criterion joins an acceptance section Orbi already appended, and
    creates one (with its hidden marker) when the ticket has none, so the
    ticket keeps ONE acceptance block however the amendment arrives.
    """
    existing = body if isinstance(body, str) else ""
    if criterion in existing:
        return None
    marker = acceptance_section.appended_marker()
    if marker in existing:
        return existing.replace(marker, f"- {criterion}\n\n{marker}", 1)
    return acceptance_section.appended_body(
        existing,
        acceptance_section.render_acceptance((criterion,), language_code),
    )


def report_once(stop: StopQuestion | None, *, issue: dict, source_repo: str,
                run_id: str, language_code: str, status_url: str | None,
                pr_url: str | None, current_labels,
                ready_label: str = READY_LABEL) -> str | None:
    """Answer the stop once, or return None to keep the terminal report.

    The counter is the 'requeued' comment every automatic resume writes
    ('orbi.requeue.marker_present', Issue #2202 included), so an Issue gets
    ONE over its life and no new state store appears. The history read is the
    capability check: when it fails the counter cannot be proven unused, so
    the stop stays terminal rather than risk a second automatic resume — a
    stop a person resolves is recoverable, a loop is not.

    The order is the ticket's: the criterion lands in the body the next
    session reads FIRST, the counter comment second, the queue label last.
    """
    option = takeable_option(stop)
    if option is None:
        return None
    number = int(issue["number"])
    try:
        history = issue_comments(number, repo=source_repo)
    except Exception:
        LOGGER.exception("issue=%s question history read failed", number)
        event("question_history_read_failed", issue=number, run_id=run_id)
        return None
    if requeue.marker_present(history, repo=source_repo):
        return None
    amended = amended_body(
        issue.get("body"), option.description, language_code,
    )
    if amended is not None:
        acceptance_section.write_body(number, repo=source_repo, body=amended)
        issue["body"] = amended
    comment_issue(number, repo=source_repo, body=comment_body(
        stop, option, issue=issue, run_id=run_id, language_code=language_code,
        status_url=status_url,
    ))
    labels = (
        current_labels if current_labels is not None
        else issue_labels(number, source_repo)
    )
    apply_label_patch(
        number, repo=source_repo,
        event=EVENT_FIX_NEEDED if pr_url else EVENT_REQUEUE,
        current_labels=labels, ready_label=ready_label,
    )
    notify.send_if_enabled(language.text(
        language_code, "question.telegram", decision=option.label,
        reason=option.reason or "",
        url=f"https://github.com/{source_repo}/issues/{number}",
    ))
    event(
        "question_auto_resumed", issue=number, run_id=run_id,
        question=stop.question.id, label=option.label,
    )
    return requeue.OUTCOME_REQUEUED
