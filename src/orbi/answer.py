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

The stages never disagree about WHICH answer the ticket carries: the
stopped-ticket scan keeps the LAST one ("the last word wins", so a person
can change their mind by answering again), and `resolve_answers` applies
that same rule for the merge gate and the steering poll -- answering A and
then B carries out B at every stage, and A is superseded (Issue #2233).

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
from orbi.progress import run_marker
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

def answer_occurrences(body: object) -> list[str | None]:
    """The record command's occurrences in one comment body.

    Each occurrence is the answer BLOCK, which `validate` reads with
    `question.parse_answer`: the block's shape is validated in exactly one
    place (`comment_kind.parse_versioned_block`) and a corrupted record gets
    one readable receipt instead of silence. The block is one line, so an
    occurrence never splits a journal line. Two blocks in one comment are
    one unreadable occurrence: an answer comment carries ONE answer, the
    same rule the marker parser applies. The engine's own answer is engine
    OUTPUT, which the scan skips by its run marker before reading a record
    (`ticket_command.carries_run_marker`).
    """
    if not isinstance(body, str):
        return []
    blocks = [
        match.group(0) for match in question.versioned_block_pattern(
            question.ANSWER_MARKER,
        ).finditer(body)
    ]
    return [None] if len(blocks) > 1 else blocks


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
    # an occurrence came from, so the context maps each answer block to its
    # position on the ticket (the list is oldest first).
    positions: dict[str, int] = {}
    for position, comment in enumerate(ticket):
        for block in answer_occurrences(comment.get("body")):
            if isinstance(block, str):
                positions.setdefault(block, position)
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
    """Read one occurrence -- the answer block -- and decide it.

    The ONE read every caller uses (the layer's `validate`/`apply`, the
    merge gate and the steering poll), over the ONE answer parser
    (`question.parse_answer`).
    """
    if answer_problem is not None:
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable_question",
            detail=str(answer_problem),
        ))
    if stop is None:
        raise NotExecuted(
            language.text(language_code, "answer.reason_no_question"))
    try:
        answer = question.parse_answer(argument)
    except QuestionError as exc:
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable", detail=str(exc),
        )) from exc
    if answer is None:
        # An occurrence is the block `answer_occurrences` found, so this is
        # the caller handing `validate` text that carries no record at all.
        raise NotExecuted(language.text(
            language_code, "answer.reason_unreadable",
            detail="the comment carries no `orbi:answer:v1` block",
        ))
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
    _success_receipt(
        command_comment, issue_number=issue_number, repo=repo, effect=effect,
    )


def _receipt_marker(comment: dict) -> str:
    """The hidden marker of this answer's one receipt (its idempotency key)."""
    return (
        f"<!-- {command_receipt_marker(ANSWER_COMMAND)} "
        f"comment={command_comment_key(comment)} -->"
    )


def _success_receipt(
    comment: dict, *, issue_number: int, repo: str, effect: str,
    run_id: str | None = None,
) -> None:
    """Post one answer's ONE success receipt: what was carried out.

    The marker IS the counter the next read finds, so every reader that
    carries an answer out posts it -- including the steering poll, whose
    write would otherwise look uncarried to the merge gate and be carried
    out a second time by the stopped-ticket scan (Issue #2221). A carrier
    that runs inside a session prefixes the run marker, because engine
    output never steers the session it runs in.
    """
    receipt = _receipt_marker(comment)
    marker = f"{run_marker(run_id)}\n" if run_id else ""
    comment_issue(
        issue_number, repo=repo, body=f"{marker}{effect}\n\n{receipt}",
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
# The one answer that counts: the last word on the ticket
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Word:
    """One answer resolved against the ticket's newest stop question.

    `decision` is what the answer asks for at the stage, or `None` with the
    readable `refusal`. Only the LAST word counts (`resolve_answers`),
    exactly as the stopped-ticket scan reads the ticket: "the last word
    wins, so an operator can correct a typo by commenting again"
    (`ticket_command._selection`).
    """

    comment: dict
    decision: Decision | None = None
    refusal: str | None = None


def resolve_answers(
    occurrences: list[tuple[dict, str | None]],
    stop: question.StopQuestion,
    stage: str, *,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> list[Word]:
    """Resolve the gathered answers against the question, in ticket order.

    The LAST item is the answer that counts: the stopped-ticket scan's rule,
    so the merge gate's read and the steering poll's write never disagree
    with the scan about which answer a ticket carries. A last word the stage
    refuses counts as a refusal -- the person's latest word is the decision,
    never a fallback to the answer it replaced.

    `occurrences` are the `(comment, block)` pairs the caller gathered by
    its own rules (the gate's trust, the poll's freshness); a ticket whose
    question cannot be read is handled by the caller, because then no answer
    can be resolved at all.
    """
    resolved: list[Word] = []
    for comment, block in occurrences:
        if block is None:
            resolved.append(Word(comment, refusal=language.text(
                language_code, "answer.reason_two_records")))
            continue
        try:
            decision = _decision(
                block, stop=stop, stage=stage,
                language_code=language_code,
            )
        except NotExecuted as exc:
            resolved.append(Word(comment, refusal=exc.reason))
            continue
        resolved.append(Word(comment, decision=decision))
    return resolved


# --------------------------------------------------------------------------
# The other two readers: the merge gate and the steering poll
# --------------------------------------------------------------------------

def _pending(
    comments: list[dict], *, repo: str, stage: str, language_code: str,
) -> Decision | None:
    """The answer that counts when the engine has not carried it out, or None.

    "Counts" is the scan's rule (`resolve_answers`): the LAST trusted answer
    to the newest stop question, so an answer a later one replaced never
    holds a merge. "Not carried out" is read from the ticket, never from
    memory: the engine's one receipt (success or rejection) is keyed on the
    answer comment, so a scanned answer is not pending. The other rules are
    the scan's too: a run-marked comment is engine output, an answer older
    than the newest stop is stale, and the author must be trusted.
    """
    try:
        located = question.latest_stop(comments)
    except QuestionError:
        return None
    if located is None:
        return None
    stop_comment, stop = located
    permissions: dict[str, str] = {}
    occurrences: list[tuple[dict, str | None]] = []
    for comment in comments:
        if not isinstance(comment, dict) or carries_run_marker(comment):
            continue
        if not _newer(comment, stop_comment, comments):
            continue
        blocks = answer_occurrences(comment.get("body"))
        if not blocks:
            continue
        if not comment_is_trusted(
            comment, repo=repo, permissions=permissions,
        ):
            continue
        occurrences.extend((comment, block) for block in blocks)
    resolved = resolve_answers(
        occurrences, stop, stage, language_code=language_code,
    )
    if not resolved:
        return None
    counting = resolved[-1]
    if counting.decision is None:
        return None
    if runner_receipt(
        comments, f"comment={command_comment_key(counting.comment)} -->",
    ):
        return None
    return counting.decision


def pending_change(
    number: int, *, repo: str, head: str | None = None,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> Decision | None:
    """The merge gate's read: the answer that counts, while uncarried.

    Called by `merge_exec.merge_gate` immediately before the merge, next to
    the `ai-blocked` label re-read (Issue #1504): a person who answered
    while CI was running must not have the older decision merged over, and
    the answer that holds the merge is the last word on the ticket -- the
    same one the scan and the steering poll carry out (Issue #2233).
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
    run_id: str | None = None,
) -> str | None:
    """Write the body change the answer that counts implies.

    The `ai-in-progress` reader: the answer arrives as a fresh trusted
    comment while the session runs, so the engine writes the amendment the
    restarted session reads and leaves the answer's ONE receipt, so the
    merge gate and the stopped-ticket scan read it as carried out. It counts
    the same last word the scan and the gate read (`resolve_answers`), so a
    person who answers A and then B gets B written and A stays off the body.
    A `cancel` or a close is not carried out here -- the stage says so in
    one receipt, and the answer stays on the ticket for the stop scan.
    """
    occurrences: list[tuple[dict, str | None]] = []
    for comment in fresh:
        if not isinstance(comment, dict) or carries_run_marker(comment):
            continue
        occurrences.extend(
            (comment, block)
            for block in answer_occurrences(comment.get("body"))
        )
    if not occurrences:
        return None
    try:
        stop = _located(comments, language_code)[1]
    except NotExecuted as exc:
        # A ticket whose question cannot be read resolves no answer: each
        # answer gets the one receipt saying so, and nothing changes.
        for comment, _block in occurrences:
            _reject(
                comment, exc.reason, repo=repo, issue=issue,
                comments=comments, language_code=language_code,
            )
        return None
    resolved = resolve_answers(
        occurrences, stop, STAGE_SESSION, language_code=language_code,
    )
    amended: str | None = None
    counting = len(resolved) - 1
    for index, word in enumerate(resolved):
        if word.decision is None:
            # One receipt per answer the engine does not carry out here
            # (an unreadable record, a closing answer, no open question):
            # the person who answered reads why, and nothing changes.
            _reject(
                word.comment, word.refusal or "", repo=repo, issue=issue,
                comments=comments, language_code=language_code,
            )
            continue
        if index != counting:
            # A superseded answer is never written: the scan and the merge
            # gate read the same last word, so the person's earlier answer
            # must not reach the body behind their back.
            continue
        written = question.amended_body(
            amended if amended is not None else body,
            word.decision.text or "", language_code,
        )
        # An amendment the body already carries is a no-op write, not
        # an uncarried answer: the receipt below is what tells the merge
        # gate and the stopped-ticket scan that it IS done.
        if written is not None:
            run_step(
                "append the answer to the Issue body",
                lambda engine_body=written: acceptance_section.write_body(
                    int(issue["number"]), repo=repo, body=engine_body,
                ),
            )
            amended = written
        _success_receipt(
            word.comment, issue_number=int(issue["number"]), repo=repo,
            effect=language.text(
                language_code, "answer.effect_amended",
                text=word.decision.text or "",
            ),
            run_id=run_id,
        )
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
