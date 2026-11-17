"""Carry out a person's answer to a stop question (Issue #2221).

A stop that needs a decision carries ONE hidden question
(`orbi.question`, Issue #2220). This module is the other half: a person
answers it with one trusted comment

    <!-- orbi:answer:v1 {"q":"q1","action":"accept","value":"<label>","by":"<login>"} -->

and the engine carries the answer out at every stage of the delivery:

- `ai-blocked`: the stopped-ticket command scan reads it, performs the
  action and posts ONE receipt;
- `ai-in-progress`: the steering poll (`pi_session.check_steering`) writes
  the body change the answer implies; the existing restart machinery then
  restarts the session once with the amended ticket;
- `ai-pr-opened` / `ai-fix-needed` (between rounds): the same scan writes
  the body change, so the next review session reads the amended ticket;
- after a clean review, while the merge waits for CI: the merge gate
  itself reads it (`pending_change`) and stops the merge when the engine
  has not carried the answer out yet, so the delivery goes to a fix round
  instead of merging over the new decision.

The answer is a RECORD command of the ticket-command layer
(`CommandSpec.occurrence`, `orbi.ticket_command`): its occurrence is the
hidden block, not a `/orbi` line, so it has no typed spelling and no CLI
entry, and the layer's one receipt writer and idempotency rule are the ones
it uses (`_post_command_receipt`, `runner_receipt`). Engine output is told
apart by its RUN MARKER prefix (`ticket_command.carries_run_marker`), never
by the `<!-- orbi` marker the record itself carries -- otherwise the answer
would be skipped exactly like engine output and never read at all.

What the engine does with one answer, and nothing else:

- `accept` with a chosen option: the option's `action` --
  `amend_requeue` appends the option's `description` (a `confirm`
  question's `criterion`, a `text` question's text) as an acceptance
  criterion through the `/orbi amend` body logic and re-queues the
  delivery; `close_external_pr` closes the external pull request and
  re-queues the triage Issue for internal delivery; `close_all` closes the
  Issue and its pull request and deletes the delivery branch; `link` is a
  person's own action on GitHub, so the engine carries out nothing and says
  so;
- `decline` appends the person's own words to the Issue body and
  re-queues (`amend_requeue`);
- `cancel` closes the Issue and its pull request -- at `ai-blocked` only;
  at any other stage it is not carried out and the receipt says why.

One answer is executed once: the success receipt carries the layer's
marker keyed on the answer comment, and a later scan reads that receipt
instead of running again. A rejection (an untrusted author, an answer
older than the newest stop, an answer for another question, a value that
matches no option, a `cancel` outside `ai-blocked`) leaves the layer's one
rejection receipt and no action. The engine's own record (`"by": "orbi"`,
the auto-answer of Issue #2220) is never executed and needs no receipt: it
IS engine output, and its comment carries the run marker.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from orbi import acceptance_section, language, question
from orbi.branch_reclaim import delete_merged_delivery_branch
from orbi.comment_trust import comment_is_trusted
from orbi.delivery_labels import (
    BLOCKED_LABEL,
    EVENT_FIX_NEEDED,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
)
from orbi.github import (
    apply_label_patch,
    close_issue,
    comment_issue,
    edit_issue,
    issue_comments,
    issue_labels,
    issue_view,
    pr_number,
)
from orbi.gitops import is_delivery_branch, task_branch
from orbi.journal import event, run_command
from orbi.question import QuestionError
from orbi.ticket_command import (
    CommandSpec,
    TicketCommandError,
    _post_command_receipt,
    carries_run_marker,
    command_comment_key,
    command_receipt_marker,
    failure_reason,
    run_step,
    runner_receipt,
)

# The stage is the label the ticket carries when the answer is read: the
# scan label of the stopped-ticket scans, or `ai-in-progress` for the
# steering poll. Nothing else is a stage an answer is read at.
STAGE_BLOCKED = BLOCKED_LABEL
STAGE_PR_OPENED = PR_OPENED_LABEL
STAGE_FIX_NEEDED = FIX_NEEDED_LABEL
STAGE_SESSION = IN_PROGRESS_LABEL
STAGES = (STAGE_BLOCKED, STAGE_PR_OPENED, STAGE_FIX_NEEDED, STAGE_SESSION)
# The answerer the engine itself writes (Issue #2220's automatic answer):
# never executed, and never a reason to stop a merge.
ENGINE_ANSWERER = "orbi"

# The Issue fields the answer's one read needs.
ISSUE_FIELDS = "number,body,labels"


class NotExecuted(ValueError):
    """The answer is readable but the engine carries nothing out.

    `reason` is the readable sentence the one receipt carries: a value
    that matches no option, a `cancel` outside `ai-blocked`, an option a
    person takes on GitHub themselves. It is not a failure -- nothing was
    attempted -- so it never becomes a `TicketCommandError`.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AnswerSupersedesVerdict(RuntimeError):
    """A trusted answer the engine has not carried out blocks the merge.

    Raised by the merge gate itself: a decision that changed after the
    clean review must not be merged over, so the delivery goes to a fix
    round and the next review reads the amended ticket.
    """


@dataclass(frozen=True)
class Decision:
    """What one answer asks the engine to do, already validated.

    `action` is the QUESTION action to carry out
    (`question.ACTION_AMEND_REQUEUE`, `ACTION_CLOSE_EXTERNAL_PR`,
    `ACTION_CLOSE_ALL`); `text` is the acceptance criterion of an
    `amend_requeue` -- the option's description, a confirm question's
    criterion, a text answer, or the person's own words on a decline.
    """

    answer: question.Answer
    stop: question.StopQuestion
    action: str
    text: str | None = None


# --------------------------------------------------------------------------
# The occurrence: the answer block of one comment
# --------------------------------------------------------------------------

def _payloads(body: object) -> list[str]:
    """Every `orbi:answer:v1` payload in one comment body, in order."""
    if not isinstance(body, str) or question.ANSWER_MARKER not in body:
        return []
    return question.versioned_block_pattern(
        question.ANSWER_MARKER,
    ).findall(body)


def _engine_record(payload: str) -> bool:
    """True when the record is the engine's own answer (never executed)."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("by") == ENGINE_ANSWERER


def answer_occurrences(body: object) -> list[str | None]:
    """The record command's occurrences in one comment body.

    Each occurrence is the answer block's raw JSON payload, which
    `validate` parses (`question.parse_answer_payload`) so a corrupted
    record gets one readable receipt instead of silence. Two blocks in one
    comment are one unreadable occurrence: an answer comment carries ONE
    answer, the same rule `question.parse_versioned_block` applies. The
    engine's own `by: "orbi"` record yields no occurrence at all.
    """
    payloads = [
        payload for payload in _payloads(body) if not _engine_record(payload)
    ]
    if not payloads:
        return []
    if len(payloads) > 1:
        return [None]
    return [payloads[0].strip() or None]


# --------------------------------------------------------------------------
# The decision: which option, which stage, what the engine does
# --------------------------------------------------------------------------

def _key(comment: dict) -> str:
    """One comment's identity on the ticket's timeline."""
    return command_comment_key(comment)


def _newer(comment: dict, boundary: dict, comments: list[dict]) -> bool:
    """True when `comment` comes after `boundary` on the ticket's timeline.

    The list `issue_comments` returns is oldest first, so the order of the
    list IS the order of the ticket. The `createdAt` stamps are the
    fallback when one of the two is not in the list.
    """
    order = [_key(item) for item in comments if isinstance(item, dict)]
    later, earlier = _key(comment), _key(boundary)
    if later in order and earlier in order:
        return order.index(later) > order.index(earlier)
    return str(comment.get("createdAt") or "") > str(
        boundary.get("createdAt") or "")


def _value_text(value: str | bool) -> str:
    """An answer value as one readable word (booleans included)."""
    if value is True:
        return "true"
    return "false" if value is False else str(value)


def decide(
    answer: question.Answer, stop: question.StopQuestion, stage: str | None,
    *, language_code: str = language.DEFAULT_LANGUAGE,
) -> Decision:
    """What one answer asks for at one stage, or `NotExecuted`.

    The shape rules are the question's (`orbi.question`); what this adds is
    the closed set of things the ENGINE may carry out: an accepted choice's
    action, a decline's own words, a cancel (at `ai-blocked` only). An
    answer whose `value` matches no option of the question is refused
    here, before any write.
    """
    if stage not in STAGES:
        raise NotExecuted(language.text(
            language_code, "answer.reason_no_stage", stage=stage or "-",
        ))
    asked = stop.question
    if answer.question != asked.id:
        raise NotExecuted(language.text(
            language_code, "answer.reason_other_question",
            answered=answer.question, asked=asked.id,
        ))
    if answer.action == question.ANSWER_CANCEL:
        if stage != STAGE_BLOCKED:
            raise NotExecuted(language.text(
                language_code, "answer.reason_cancel_stage", stage=stage,
            ))
        return Decision(answer, stop, question.ACTION_CLOSE_ALL)
    if answer.action == question.ANSWER_DECLINE:
        return Decision(
            answer, stop, question.ACTION_AMEND_REQUEUE,
            _own_words(answer, language_code),
        )
    if asked.kind == question.KIND_CHOICE:
        chosen = str(answer.value).strip()
        option = next(
            (item for item in asked.options if item.label.strip() == chosen),
            None,
        )
        if option is None:
            raise NotExecuted(language.text(
                language_code, "answer.reason_no_option",
                question=asked.id, value=_value_text(answer.value),
            ))
        return _from_option(answer, stop, option, stage, language_code)
    if asked.kind == question.KIND_CONFIRM:
        if answer.value is False:
            raise NotExecuted(
                language.text(language_code, "answer.reason_no"))
        if answer.value is not True:
            raise NotExecuted(language.text(
                language_code, "answer.reason_confirm_value",
                value=_value_text(answer.value),
            ))
        return _carried_out(
            answer, stop, asked.action or question.ACTION_AMEND_REQUEUE,
            asked.criterion, stage, language_code,
        )
    # A text question asks for the assumption itself: the answer's text is
    # appended to the Issue body, exactly like a decline's own words.
    return _carried_out(
        answer, stop, question.ACTION_AMEND_REQUEUE,
        _own_words(answer, language_code), stage, language_code,
    )


def _own_words(answer: question.Answer, language_code: str) -> str:
    """The person's own words: the `reason`, else a text `value`."""
    words = answer.reason or (
        answer.value if isinstance(answer.value, str) else ""
    ).strip()
    if not words:
        raise NotExecuted(
            language.text(language_code, "answer.reason_no_text"))
    return words


def _from_option(
    answer: question.Answer, stop: question.StopQuestion,
    option: question.Option, stage: str, language_code: str,
) -> Decision:
    """The decision one chosen option stands for."""
    if option.action == question.ACTION_LINK:
        raise NotExecuted(language.text(
            language_code, "answer.reason_link", label=option.label,
            url=option.url or "-",
        ))
    return _carried_out(
        answer, stop, option.action or question.ACTION_AMEND_REQUEUE,
        option.description, stage, language_code,
    )


def _carried_out(
    answer: question.Answer, stop: question.StopQuestion, action: str,
    text: str | None, stage: str, language_code: str,
) -> Decision:
    """Refuse a close outside `ai-blocked`; everything else is taken."""
    if action in (
        question.ACTION_CLOSE_EXTERNAL_PR, question.ACTION_CLOSE_ALL,
    ) and stage != STAGE_BLOCKED:
        raise NotExecuted(language.text(
            language_code, "answer.reason_close_stage", stage=stage,
        ))
    return Decision(answer, stop, action, text)


# --------------------------------------------------------------------------
# The command: the ticket-command layer's record command
# --------------------------------------------------------------------------

def _located(
    comments: list[dict], language_code: str,
) -> tuple[dict, question.StopQuestion]:
    """The newest stop comment and its question, or `NotExecuted`."""
    try:
        located = question.latest_stop(comments)
    except QuestionError as exc:
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable_question", detail=str(exc),
        )) from exc
    if located is None:
        raise NotExecuted(
            language.text(language_code, "answer.reason_no_question"))
    return located


def _answer_context(
    *, comments: list[dict] | None = None,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> dict[str, object]:
    """The command's own context: the ticket's newest stop question.

    The comment scan hands the comments to the dispatcher, not to
    `validate`, so the question is read HERE — from the ticket itself, the
    same way `/orbi amend` reads its proposal — and both callbacks below
    receive it. An unreadable stop block is reported, never skipped: the
    engine wrote it, so a corrupted one must not read as "no question".
    """
    ticket = [item for item in comments or [] if isinstance(item, dict)]
    try:
        located = question.latest_stop(ticket)
    except QuestionError as exc:
        return {"answer_problem": str(exc)}
    if located is None:
        return {}
    stop_comment, stop = located
    # The timeline the stale rule needs: `validate` never sees the comment
    # an occurrence came from, so the context maps each answer payload to
    # its position on the ticket (the list is oldest first).
    positions: dict[str, int] = {}
    for position, comment in enumerate(ticket):
        for payload in answer_occurrences(comment.get("body")):
            if isinstance(payload, str):
                positions.setdefault(payload, position)
    return {
        "stop": stop, "stop_comment": stop_comment,
        "answer_positions": positions,
        "stop_position": ticket.index(stop_comment),
    }


def _decision(
    argument: str, *, stop: question.StopQuestion | None,
    answer_problem: object = None, stage: object = None,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> Decision:
    """Parse one occurrence and decide it -- the one read both callers use."""
    if answer_problem is not None:
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable_question",
            detail=str(answer_problem),
        ))
    if stop is None:
        raise NotExecuted(
            language.text(language_code, "answer.reason_no_question"))
    try:
        answer = question.parse_answer_payload(argument)
    except QuestionError as exc:
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable", detail=str(exc),
        )) from exc
    return decide(
        answer, stop, stage if isinstance(stage, str) else None,
        language_code=language_code,
    )


def _answer_validate(
    argument: str, *, stop: object = None, answer_problem: object = None,
    answer_stage: object = None, answer_positions: object = None,
    stop_position: object = None,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> str | None:
    """The layer's `validate`: the readable reason to carry nothing out."""
    if isinstance(answer_positions, dict) and isinstance(stop_position, int):
        position = answer_positions.get(argument)
        if isinstance(position, int) and position < stop_position:
            return language.text(language_code, "answer.reason_stale")
    try:
        _decision(
            argument, stop=stop, answer_problem=answer_problem,
            stage=answer_stage, language_code=language_code,
        )
    except NotExecuted as exc:
        return exc.reason
    return None


def _answer_apply(
    argument: str, *, repo: str, issue_number: int, command_comment: dict,
    comments: list[dict] | None = None, stop: object = None,
    answer_problem: object = None,
    answer_stage: object = None, dispatch_label: str = READY_LABEL,
    language_code: str = language.DEFAULT_LANGUAGE, **_context: object,
) -> None:
    """Carry the answer out and post its ONE receipt (idempotent)."""
    comments = list(comments or [])
    receipt = _receipt_marker(command_comment)
    if runner_receipt(comments, receipt):
        # The success receipt IS the counter: the next scan reads it and
        # does not carry the same answer out twice.
        return
    # `validate` accepted this very occurrence against this very context,
    # so the decision is re-derived (never re-judged against a new read):
    # the stale rule, the option and the stage all come from that context.
    decision = _decision(
        argument, stop=stop, answer_problem=answer_problem,
        stage=answer_stage, language_code=language_code,
    )
    issue = run_step(
        "read the ticket",
        lambda: issue_view(issue_number, ISSUE_FIELDS, repo=repo),
    )
    effect = _carry_out(
        decision, stage=answer_stage, issue=issue, repo=repo,
        issue_number=issue_number, dispatch_label=dispatch_label,
        language_code=language_code,
    )
    comment_issue(issue_number, repo=repo, body=f"{effect}\n\n{receipt}")


def _receipt_marker(comment: dict) -> str:
    """The hidden marker of this answer's one receipt (its idempotency key)."""
    return (
        f"<!-- {command_receipt_marker(ANSWER_COMMAND)} "
        f"comment={command_comment_key(comment)} -->"
    )


def _carry_out(
    decision: Decision, *, stage: object, issue: dict, repo: str,
    issue_number: int, dispatch_label: str, language_code: str,
) -> str:
    """Perform the decision's action and render the receipt body.

    Every GitHub write rides `run_step`, so a refusal becomes the layer's
    failed receipt naming the step instead of escaping into the tick's
    bypass log where the person who answered would see nothing.
    """
    action, text = decision.action, decision.text or ""
    if action == question.ACTION_CLOSE_ALL:
        pull = _target_pr(decision.stop, repo=repo, number=issue_number)
        if pull is not None:
            _close_pr(pull, repo=repo)
            branch = str(pull.get("headRefName") or "")
            if branch and is_delivery_branch(repo, branch):
                delete_merged_delivery_branch(repo, int(pull["number"]), branch)
        run_step("close the Issue",
                 lambda: close_issue(issue_number, repo=repo))
        return language.text(
            language_code, "answer.effect_closed_all",
            url=str((pull or {}).get("url") or "-"),
        )
    if action == question.ACTION_CLOSE_EXTERNAL_PR:
        pull = _target_pr(decision.stop, repo=repo, number=issue_number)
        if pull is None:
            raise TicketCommandError(
                "close the pull request",
                language.text(language_code, "answer.reason_no_pr"),
            )
        _close_pr(pull, repo=repo)
        _requeue(issue_number, repo=repo, dispatch_label=dispatch_label)
        return language.text(
            language_code, "answer.effect_closed_pr",
            url=str(pull.get("url") or "-"), label=dispatch_label,
        )
    return _amend(
        issue, text, stage=stage, repo=repo, issue_number=issue_number,
        dispatch_label=dispatch_label, language_code=language_code,
    )


def _amend(
    issue: dict, text: str, *, stage: object, repo: str,
    issue_number: int, dispatch_label: str, language_code: str,
) -> str:
    """Append the criterion through the `/orbi amend` logic, then re-queue."""
    amended = question.amended_body(issue.get("body"), text, language_code)
    if amended is not None:
        run_step(
            "append the answer to the Issue body",
            lambda: acceptance_section.write_body(
                issue_number, repo=repo, body=amended,
            ),
        )
    lines = [language.text(
        language_code, "answer.effect_amended", text=text,
    )]
    if stage == STAGE_BLOCKED:
        # A blocked Issue is not moved by the label event alone (a terminal
        # label wins over `fix_needed`), so the two labels are swapped
        # directly: with an open PR the review/fix state resumes it, and a
        # fresh delivery is claimed otherwise.
        pull = _delivery_pr(repo, issue_number)
        target = FIX_NEEDED_LABEL if pull is not None else dispatch_label
        run_step(
            "re-queue the ticket",
            lambda: edit_issue(
                issue_number, repo=repo, add=target, remove=BLOCKED_LABEL,
            ),
        )
        lines.append(language.text(
            language_code, "answer.effect_requeued", label=target,
        ))
    return "\n".join(f"- {line}" for line in lines)


def _requeue(number: int, *, repo: str, dispatch_label: str) -> None:
    """Put a blocked triage Issue back in the ready queue."""
    run_step(
        "re-queue the ticket",
        lambda: edit_issue(
            number, repo=repo, add=dispatch_label, remove=BLOCKED_LABEL,
        ),
    )


# --------------------------------------------------------------------------
# The pull requests an answer closes
# --------------------------------------------------------------------------

def _target_pr(
    stop: question.StopQuestion, *, repo: str, number: int,
) -> dict | None:
    """The pull request an answer closes: the question's, else the delivery's.

    A question may name its PR (the external-contribution stop always does,
    through its `link` option); otherwise the answer concerns this Issue's
    own delivery branch.
    """
    named = next(
        (
            option.url for option in stop.question.options
            if option.action == question.ACTION_LINK and option.url
        ),
        None,
    )
    if named is not None:
        return {"number": pr_number(named), "url": named}
    return _delivery_pr(repo, number)


def _delivery_pr(repo: str, number: int) -> dict | None:
    """The open pull request on this Issue's stable delivery branch, or None."""
    branch = task_branch(repo, number)

    def read() -> dict | None:
        raw = run_command([
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--head", branch, "--json", "number,url,headRefName",
            "--limit", "10",
        ], timeout=30)
        pull = json.loads(raw) if raw.strip() else []
        if len(pull) > 1:
            # The delivery identity is ONE PR: a second one on the same
            # branch is a state this engine never creates, so it is
            # reported (as the step's failure) instead of picking one.
            raise ValueError(f"multiple open PRs on {branch}")
        return pull[0] if pull else None

    return run_step("find the delivery pull request", read)


def _close_pr(pull: dict, *, repo: str) -> None:
    """Close one pull request (`gh pr close`)."""
    run_step(
        "close the pull request",
        lambda: run_command(
            ["gh", "pr", "close", str(pull["number"]), "--repo", repo],
            timeout=60,
        ),
    )
    event("answer_pr_closed", pr=pull["number"], repo=repo)


# --------------------------------------------------------------------------
# The other two readers: the merge gate and the steering poll
# --------------------------------------------------------------------------

def _pending(
    comments: list[dict], *, repo: str, stage: str, language_code: str,
) -> Decision | None:
    """The oldest answer the engine has not carried out yet, or None.

    "Not carried out" is read from the ticket, never from memory: the
    engine's one receipt (success or rejection) is keyed on the answer
    comment, so a scanned answer is not pending. The other rules are the
    scan's: a run-marked comment is engine output, an answer older than the
    newest stop is stale, and the author must be trusted.
    """
    try:
        located = question.latest_stop(comments)
    except QuestionError:
        located = None
    if located is None:
        return None
    stop_comment, stop = located
    permissions: dict[str, str] = {}
    for comment in comments:
        if not isinstance(comment, dict) or carries_run_marker(comment):
            continue
        for payload in answer_occurrences(comment.get("body")):
            if payload is None or not _newer(comment, stop_comment, comments):
                continue
            if not comment_is_trusted(
                comment, repo=repo, permissions=permissions,
            ):
                continue
            if runner_receipt(
                comments, f"comment={command_comment_key(comment)} -->",
            ):
                continue
            try:
                return decide(
                    question.parse_answer_payload(payload), stop, stage,
                    language_code=language_code,
                )
            except (QuestionError, NotExecuted):
                continue
    return None


def pending_change(
    number: int, *, repo: str, head: str | None = None,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> Decision | None:
    """The merge gate's read: an uncarried answer to the current question.

    Called by `merge_exec.merge_gate` immediately before the merge, next to
    the `ai-blocked` label re-read (Issue #1504): a person who answered
    while CI was running must not have the older decision merged over.
    """
    decision = _pending(
        issue_comments(number, repo=repo), repo=repo,
        stage=STAGE_PR_OPENED, language_code=language_code,
    )
    if decision is not None:
        event(
            "merge_gate_decision_superseded", level=logging.ERROR,
            issue=number, head=head or "-",
            question=decision.stop.question.id,
        )
    return decision


def refuse_superseded_merge(
    number: int, *, repo: str, head: str | None = None,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> None:
    """Raise `AnswerSupersedesVerdict` when a newer answer waits.

    The one call the merge gate makes: the decision, the journal line and
    the exception body live here, so the gate keeps one line and no copy of
    the rule. An unreadable ticket is not evidence that the decision still
    holds: the gate holds too (the safe direction) and the next review
    round reads the ticket again.
    """
    try:
        decision = pending_change(
            number, repo=repo, head=head, language_code=language_code,
        )
    except Exception as exc:
        raise AnswerSupersedesVerdict(
            f"Issue #{number} answer history could not be read "
            f"({failure_reason(exc)}); the merge is not performed"
        ) from exc
    if decision is None:
        return
    raise AnswerSupersedesVerdict(language.text(
        language_code, "answer.superseded_merge",
        question=decision.stop.question.id,
        value=_value_text(decision.answer.value),
    ))


def stop_for_fix_round(
    number: int, *, repo: str, pr_number: int, reason: str, marker: str,
    language_code: str,
) -> None:
    """Send the delivery to a fix round: the Issue comment, then the label.

    The next tick's review session reads the amended ticket, which is why
    the label (not a second comment) is the last step.
    """
    comment_issue(
        number, repo=repo,
        body=f"{marker}\n" + language.text(
            language_code, "answer.fix_round", pr_number=pr_number,
            reason=reason,
        ),
    )
    apply_label_patch(
        number, repo=repo, event=EVENT_FIX_NEEDED,
        current_labels=issue_labels(number, repo),
    )


def apply_steering(
    fresh: list[dict], *, issue: dict, repo: str, comments: list[dict],
    body: object, language_code: str = language.DEFAULT_LANGUAGE,
) -> str | None:
    """Write the body change a steering answer implies; return the new body.

    The `ai-in-progress` reader: the answer arrives as a fresh trusted
    comment while the session runs, so the engine writes the amendment the
    restarted session reads and nothing else. A `cancel` or a close is not
    carried out here -- the stage says so in one receipt, and the answer
    stays on the ticket for the stop scan.
    """
    stop: question.StopQuestion | None = None
    amended: str | None = None
    for comment in fresh:
        if not isinstance(comment, dict) or carries_run_marker(comment):
            continue
        for payload in answer_occurrences(comment.get("body")):
            if payload is None:
                _reject(
                    comment,
                    language.text(language_code, "answer.reason_two_records"),
                    repo=repo, issue=issue, comments=comments,
                    language_code=language_code,
                )
                continue
            try:
                if stop is None:
                    stop = _located(comments, language_code)[1]
                decision = _decision(
                    payload, stop=stop, stage=STAGE_SESSION,
                    language_code=language_code,
                )
            except NotExecuted as exc:
                # One receipt per answer the engine does not carry out here
                # (an unreadable record, a closing answer, no open question):
                # the person who answered reads why, and nothing changes.
                _reject(
                    comment, exc.reason, repo=repo, issue=issue,
                    comments=comments, language_code=language_code,
                )
                continue
            written = question.amended_body(
                amended if amended is not None else body,
                decision.text or "", language_code,
            )
            if written is None:
                continue
            run_step(
                "append the answer to the Issue body",
                lambda engine_body=written: acceptance_section.write_body(
                    int(issue["number"]), repo=repo, body=engine_body,
                ),
            )
            amended = written
    return amended


def _reject(
    comment: dict, reason: str, *, repo: str, issue: dict,
    comments: list[dict], language_code: str,
) -> None:
    """One rejection receipt for one answer the engine does not carry out."""
    _post_command_receipt(
        repo, int(issue["number"]), comments, comment, spec=ANSWER_COMMAND,
        marker=f"{command_receipt_marker(ANSWER_COMMAND)}-rejected",
        summary=language.text(
            language_code, "command.not_applied",
            command=ANSWER_COMMAND.canonical,
        ),
        argument=None, reason=reason, language_code=language_code,
    )


ANSWER_COMMAND = CommandSpec(
    name="answer",
    subcommand="answer",
    arity=0,
    permission="write",
    validate=_answer_validate,
    apply=_answer_apply,
    usage="<!-- orbi:answer:v1 {json} -->",
    description="carry out the answer to this ticket's stop question",
    occurrence=answer_occurrences,
    context=_answer_context,
)
