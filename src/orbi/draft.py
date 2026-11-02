#!/usr/bin/env python3
"""`orbi draft`'s drafting engine (Issue #2041).

Drafting used to live only in the `write-ai-ready-issue` skill, next to
the pre-work check that judged the result: a one-sentence request drafted
into one Issue with ten acceptance items was stopped as "more than one
outcome" and only delivered after `/orbi split`. This module drafts
INSIDE the engine, against the check's own criteria: one round takes the
request, the conversation and the current draft, runs one read-only Pi
session through the shipped `draft_tools` extension, runs the fidelity
pass and judges the result. `orbi.draft_cli` owns the command boundary
(interactive mode, the stdin/stdout documents, creation).

Every drafted Issue is judged by the pre-work check itself —
`clarify.judge_issue_body`, the function the claim runs — before a person
sees it, with a placeholder number because the Issue does not exist yet.
A failing draft goes back to the drafter EXACTLY once; a draft that still
fails is returned with the failed checks in `check`. The created Issues
carry a hidden marker naming the draft, their position and a fingerprint
of the body as created, so the claim-time check skips a body this runner
drafted and nobody edited (`clarify.enforce`): Orbi does not bounce its
own drafts.

Creation reuses the split command's own order and helpers — create
without the dispatch label, add the `blockedBy` edge, then the label —
so a re-run only does what is still missing on GitHub.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orbi import clarify, draft_marker, journal, split_command
from orbi.amend_command import appended_body, write_body
from orbi.claim import _repo_scan_keys
from orbi.config import RunnerConfig
from orbi.delivery_labels import (
    EPIC_LABEL,
    NEEDS_DETAIL_LABEL,
    READY_LABEL,
    label_names,
)
from orbi.delivery_scene import RunContext
from orbi.github import edit_issue, issue_view, list_issues, list_milestones
from orbi.journal import event
from orbi.pi_command import (
    IMPLEMENT_EXCLUDED_SKILLS,
    ROLE_SUGGEST,
    ROLE_TICKET,
    build_pi_command,
)
from orbi.pi_process import PiWatchOptions, stream_pi
from orbi.pi_session import pi_extension_env, prepare_pi_agent_dir
from orbi.ticket_command import author_login_key, runner_login

# The two tools the shipped extension registers; the prompt names them and
# the tool allowlist below must carry them (one spelling, two places).
WRITE_DRAFT_TOOL = "write_draft"
ASK_USER_TOOL = "ask_user"
# The drafting session reads the checkout and writes ONLY through the two
# shipped tools: no shell, no edit, no commit (the same read-only boundary
# `orbi suggest` keeps).
#
# Pi's `--tools` REPLACES the whole tool selection and applies to
# extension tools too ("Applies to built-in, extension, and custom tools" —
# `pi --help`, docs/cli.md), so the two answer tools are named here as
# well. Without them the model has no tool to answer with: the session
# then replies in prose, no answer file is written and every round fails
# `no_answer` (verified against Pi 1.0.0 — the "read,grep,find,ls"-only
# form makes the model report it cannot call write_draft/ask_user).
DRAFT_TOOLS = ",".join((
    "read", "grep", "find", "ls", WRITE_DRAFT_TOOL, ASK_USER_TOOL,
))
DRAFT_EXTENSION = Path(__file__).resolve().parent.joinpath(
    "extensions", "draft_tools.ts",
)
# The extension writes its one answer here; the engine reads the file after
# the session, so a broken stdout never loses a draft the model did write.
DRAFT_OUT_ENV = "ORBI_DRAFT_OUT"
DRAFT_ANSWER_FILE = "answer.json"
# The session layout a caller may depend on (docs/public-contracts.mdx):
# the drafting session and the fidelity pass each keep their Pi session
# files under the caller's directory.
DRAFT_SESSION_DIR = ".pi-session"
FIDELITY_DIR = ".fidelity"
FIDELITY_REL = Path(
    "integrations/claude-plugin/skills/write-ai-ready-issue/fidelity-pass.md"
)
# A small fixed bound: a hung model request must not hold the command.
DRAFT_TIMEOUT_SECONDS = 900
FIDELITY_TIMEOUT_SECONDS = 180
# The caller's id for one draft, and the key of the marker each created
# Issue carries. Flat and short so it survives as one marker token.
DRAFT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SEARCH_LIMIT = 200
ISSUE_FIELDS = "number,title,body,labels,milestone,parent,author"
# The interactive answers: Enter or any yes word creates, any no word
# cancels, anything else is a typed edit that goes back to the drafter.
CONFIRM = frozenset({"", "y", "yes", "create"})
DECLINE = frozenset({"n", "no", "cancel", "q", "quit"})
# A termination guard for the interactive loop (Issue #95): a drafter that
# never converges must fail fast instead of spinning forever.
MAX_ROUNDS = 12

# The one rule that replaced the skill's "draft only the first slice": the
# check reports single_outcome for a request that bundles independent
# deliverables, so the drafter splits it BEFORE the check ever sees it.
SINGLE_OUTCOME_RULE = (
    "A request that fails the single_outcome check becomes SEVERAL Issues "
    "in one draft: one Issue per outcome, in delivery order, never one "
    "Issue whose acceptance list covers independent deliverables. A draft "
    "may therefore hold one Issue or many."
)

# The fixed drafting rules the hosted caller added itself move into the
# engine (Issue #2041). They are page-independent: the ones about a hosted
# web page stay with the caller.
DRAFTING_RULES = (
    "Rules for every draft:\n"
    "- Write in the person's language: the Issue titles, the Issue bodies, "
    "your reply and your questions.\n"
    "- Refer to files by their repository-relative path.\n"
    "- Do not write the commit SHA.\n"
    "- Open the reply with one plain-language sentence of what will "
    "change.\n"
    "- State the assumptions you made instead of asking about them.\n"
    "- Do not repeat the draft or the questions in the reply.\n"
    "- When you ask questions, do not claim the person chose the "
    "recommended option.\n"
    "- Never tell the person to create the Issue themselves, and never "
    "give a new-Issue link.\n"
    "- Each Issue body carries its own outcome and its own acceptance "
    "condition, so the pre-work check accepts it as it stands.\n"
    "- The after field is the 1-based number of an EARLIER Issue in this "
    "same draft that this one waits for; omit it when nothing comes "
    "first."
)

FIDELITY_PROTOCOL = (
    "Input: one JSON object {\"request\": <the person's words verbatim>, "
    "\"issues\": [{\"title\", \"body\", \"after\"}]}.\n"
    "Return ONLY a JSON object {\"issues\": [{\"title\", \"body\", "
    "\"after\"}]} holding the complete revised draft, never a diff and "
    "never a comment. Keep the same Issues the input has: this pass "
    "removes what the person did not ask for, it never drops an outcome."
)

REWRITE_INSTRUCTION = (
    "The pre-work check judged the current draft and these checks failed: "
    "{reasons}. Rewrite the WHOLE draft so every Issue passes them, keep "
    "exactly what the request asks for, and call write_draft again."
)


class DraftError(RuntimeError):
    """One failed step of `orbi draft`, with the machine triple a caller
    reads: a stable `code`, the `step` that failed and the concrete
    `detail` (docs/public-contracts.mdx)."""

    def __init__(self, code: str, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.code = code
        self.step = step
        self.detail = detail


@dataclass(frozen=True)
class DraftIssue:
    """One drafted Issue: its title, its body and its place in the order."""

    title: str
    body: str
    after: int | None = None

    def as_json(self) -> dict:
        data: dict = {"title": self.title, "body": self.body}
        if self.after is not None:
            data["after"] = self.after
        return data


@dataclass(frozen=True)
class DraftAnswer:
    """One drafting-session answer: questions, or a draft, never both."""

    reply: str
    questions: tuple[str, ...] = ()
    issues: tuple[DraftIssue, ...] = ()


@dataclass(frozen=True)
class DraftRound:
    """One round as the caller reads it (the stdout contract)."""

    reply: str
    questions: tuple[str, ...] = ()
    issues: tuple[DraftIssue, ...] = ()
    check: str | None = None
    warning: str | None = None

    def as_json(self) -> dict:
        data: dict = {
            "reply": self.reply,
            "questions": list(self.questions),
            "issues": [issue.as_json() for issue in self.issues],
        }
        if self.check is not None:
            data["check"] = self.check
        if self.warning is not None:
            data["warning"] = self.warning
        return data


# --- parsing -------------------------------------------------------------

def _invalid(detail: str) -> DraftError:
    return DraftError("invalid_input", "read stdin", detail)


def read_payload(text: str) -> dict:
    """Parse one stdin document; a non-object or broken JSON fails fast."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _invalid(f"stdin is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise _invalid("stdin must be a JSON object")
    return data


def parse_issues(value: object) -> tuple[DraftIssue, ...]:
    """Validate one draft's Issues: titles, bodies and the `after` order.

    An `after` may only name an EARLIER Issue (1-based): a draft is a
    delivery order, so a forward or self reference is not a draft we can
    create.
    """
    if not isinstance(value, list) or not value:
        raise DraftError(
            "invalid_draft", "read the draft",
            "the draft carries no Issues",
        )
    issues: list[DraftIssue] = []
    for position, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} is not an object",
            )
        title = entry.get("title")
        body = entry.get("body")
        if not isinstance(title, str) or not title.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has no title",
            )
        if "\n" in title.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has a multi-line title",
            )
        if not isinstance(body, str) or not body.strip():
            raise DraftError(
                "invalid_draft", "read the draft",
                f"Issue {position} has no body",
            )
        after = entry.get("after")
        if after is not None:
            if isinstance(after, bool) or not isinstance(after, int) \
                    or not 1 <= after < position:
                raise DraftError(
                    "invalid_draft", "read the draft",
                    f"Issue {position}: after must name an EARLIER Issue "
                    f"(1..{position - 1})",
                )
        issues.append(
            DraftIssue(title=title.strip(), body=body.strip(), after=after)
        )
    return tuple(issues)


def parse_answer(value: object) -> DraftAnswer:
    """Parse the extension's answer file: a questions or a draft answer."""
    if not isinstance(value, dict):
        raise DraftError(
            "no_answer", "read the draft",
            "the drafting session wrote no answer",
        )
    kind = value.get("kind")
    reply = value.get("reply")
    if not isinstance(reply, str):
        raise DraftError(
            "no_answer", "read the draft", "the answer carries no reply",
        )
    if kind == "questions":
        questions = value.get("questions")
        if not isinstance(questions, list):
            raise DraftError(
                "no_answer", "read the draft",
                "the answer carries no questions",
            )
        kept = tuple(
            str(question).strip() for question in questions
            if str(question).strip()
        )
        if not kept:
            raise DraftError(
                "no_answer", "read the draft",
                "the answer carries no questions",
            )
        return DraftAnswer(reply=reply, questions=kept)
    if kind == "draft":
        return DraftAnswer(reply=reply, issues=parse_issues(value.get("issues")))
    raise DraftError(
        "no_answer", "read the draft", f"unknown answer kind: {kind!r}",
    )


def _round_payload(payload: dict) -> dict:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _invalid("message must be a non-empty string")
    return payload


def render_context(payload: dict, *, repo_dir: Path) -> str:
    """The one context a round hands the session.

    The conversation and the current draft are REPLAYED here, not read back
    from Pi's session file: a round with no session file at all still works,
    and the same context shape is what a second round resumes with.
    """
    message = str(payload["message"]).strip()
    lines = ["Request:", message, ""]
    conversation = payload.get("conversation")
    if isinstance(conversation, list) and conversation:
        lines.append("Earlier turns of this conversation:")
        for turn in conversation:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "person")
            text = str(turn.get("text") or "").strip()
            if text:
                lines.append(f"- {role}: {text}")
        lines.append("")
    draft = payload.get("draft")
    if draft:
        lines.append("The current draft, as JSON:")
        lines.append(json.dumps(draft, ensure_ascii=False, indent=2))
        lines.append("")
    lines.append(
        f"Repository checkout: {repo_dir} (read-only; refer to files by "
        "their repository-relative path)."
    )
    return "\n".join(lines)


# --- the drafting session ------------------------------------------------

def draft_system_prompt() -> str:
    """The drafting prompt: the check's OWN criteria plus the draft rules."""
    return "\n\n".join((
        "You are Orbi's drafting session: read-only, no shell. Turn one "
        "request into GitHub Issues that Orbi's pre-work check accepts. "
        "Answer only by calling exactly one tool: " + WRITE_DRAFT_TOOL
        + " when you have a draft, " + ASK_USER_TOOL + " when you need "
        "the person to answer something first. Never both in one turn.",
        clarify.TICKET_CHECK_CRITERIA,
        SINGLE_OUTCOME_RULE,
        DRAFTING_RULES,
    ))


def run_draft_session(
    config: RunnerConfig, repo: str, *, session_root: Path,
    system_prompt: str, context: str, run_id: str, out_path: Path,
    resume: bool = False,
) -> None:
    """Run ONE drafting session; its answer lands in `out_path`.

    The same session directory across rounds lets Pi resume the session it
    already ran (`--continue`), and the read-only tool allowlist plus the
    shipped `draft_tools` extension are the whole boundary: the session
    cannot edit, commit or open anything.
    """
    session_root = Path(session_root)
    session_dir = session_root / DRAFT_SESSION_DIR
    session_dir.mkdir(parents=True, exist_ok=True)
    command, log_command = build_pi_command(
        config, ROLE_SUGGEST, IMPLEMENT_EXCLUDED_SKILLS, session_dir,
        system_prompt, context,
        context_placeholder="<draft-context-redacted>",
        tools=DRAFT_TOOLS, extensions=True,
        only_extension=str(DRAFT_EXTENSION),
        no_skills=True, no_context_files=True, skills=[],
        resume=resume,
    )
    agent_dir = prepare_pi_agent_dir(session_root, config, role=ROLE_SUGGEST)
    pi_env = pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    pi_env[DRAFT_OUT_ENV] = str(out_path)
    stream_pi(
        command, cwd=session_root,
        ctx=RunContext(
            run_id=run_id, issue=0, branch="", worktree=session_root,
            source_repo=repo,
        ),
        role=ROLE_SUGGEST,
        timeout=DRAFT_TIMEOUT_SECONDS,
        log_command=log_command,
        pi_env=pi_env,
        watch=PiWatchOptions(
            model_wait_dead_seconds=config.model_wait_dead_seconds,
            model_wait_probe_url=config.model_wait_probe_url,
            model_wait_probe_seconds=config.model_wait_probe_seconds,
        ),
    )


def run_fidelity_session(
    config: RunnerConfig, repo: str, *, session_root: Path,
    system_prompt: str, context: str, run_id: str,
) -> str:
    """Run the independent fidelity pass: no tools, no conversation.

    Its own Pi session under the caller's directory, so the public contract
    can name where the pass's files are; it answers in stdout, which is the
    revised draft.
    """
    session_root = Path(session_root)
    session_dir = session_root / DRAFT_SESSION_DIR
    session_dir.mkdir(parents=True, exist_ok=True)
    command, log_command = build_pi_command(
        config, ROLE_TICKET, IMPLEMENT_EXCLUDED_SKILLS, session_dir,
        system_prompt, context,
        context_placeholder="<fidelity-context-redacted>",
        tools=False, extensions=False, no_extensions=True,
        no_skills=True, no_context_files=True, skills=[],
    )
    agent_dir = prepare_pi_agent_dir(session_root, config, role=ROLE_TICKET)
    pi_env = pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    return stream_pi(
        command, cwd=session_root,
        ctx=RunContext(
            run_id=run_id, issue=0, branch="", worktree=session_root,
            source_repo=repo,
        ),
        role=ROLE_TICKET,
        timeout=FIDELITY_TIMEOUT_SECONDS,
        log_command=log_command,
        pi_env=pi_env or None,
        watch=PiWatchOptions(
            model_wait_dead_seconds=config.model_wait_dead_seconds,
            model_wait_probe_url=config.model_wait_probe_url,
            model_wait_probe_seconds=config.model_wait_probe_seconds,
        ),
    )


def _run_answer_session(
    config: RunnerConfig, repo: str, *, session_root: Path, context: str,
    out_path: Path, run_id: str, resume: bool,
) -> None:
    """Run the session and keep a draft that a timeout left behind.

    The answer file is removed first, so an answer found after a failure is
    the one THIS round wrote: a round that timed out after the model wrote
    its draft still returns that draft, and a round that wrote nothing
    fails.
    """
    out_path.unlink(missing_ok=True)
    try:
        run_draft_session(
            config, repo, session_root=session_root,
            system_prompt=draft_system_prompt(), context=context,
            run_id=run_id, out_path=out_path, resume=resume,
        )
    except Exception as exc:
        if not out_path.is_file():
            code = (
                "timeout"
                if isinstance(exc, subprocess.TimeoutExpired)
                else "session_failed"
            )
            raise DraftError(
                code, "run the drafting session",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        event(
            "draft_session_failed", level=logging.WARNING,
            repo=repo, reason=type(exc).__name__,
        )


def read_answer(path: Path) -> DraftAnswer:
    """Read the answer file; no file or broken JSON is a failed round."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DraftError(
            "no_answer", "read the draft",
            "the drafting session wrote no answer",
        ) from None
    return parse_answer(data)


# --- the pre-work check on the draft -------------------------------------

def check_draft(
    config: RunnerConfig, repo: str, *, run_id: str,
    issues: tuple[DraftIssue, ...],
) -> tuple[str, tuple[str, ...]]:
    """Run every drafted Issue through the check the claim runs.

    `clarify.judge_issue_body` is the SAME function, not a copy, and the
    Issue number is a placeholder (0) because the Issue does not exist yet
    — the judgment only reads the title and the body. An unavailable
    judgment (`None`, a model error) fails OPEN exactly like the gate, so
    a broken model call never turns a good draft into a failed one.
    """
    failed: list[str] = []
    for issue in issues:
        verdict = clarify.judge_issue_body(
            {"number": 0, "title": issue.title, "body": issue.body},
            config, repo, run_id,
        )
        if verdict is None or verdict.passed:
            continue
        for item in verdict.missing:
            if item not in failed:
                failed.append(item)
    if not failed:
        return "passed", ()
    return ",".join(failed), tuple(failed)


def _parse_revised(output: str) -> tuple[DraftIssue, ...] | None:
    """The titles and bodies of the fidelity pass answer, or None.

    The pass only revises titles and bodies (Issue #2085): the order is the
    drafter's, so its `after` fields are dropped here and never judged. A
    revision that moved `after` onto the wrong Issue is still a usable
    revision, and judging its copy would throw the whole pass away.
    """
    text = output.strip()
    if text.startswith("{") is False:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    entries = data.get("issues")
    if not isinstance(entries, list):
        return None
    try:
        return parse_issues([
            {**entry, "after": None} if isinstance(entry, dict) else entry
            for entry in entries
        ])
    except DraftError:
        return None


def fidelity_pass(
    config: RunnerConfig, repo: str, *, session_root: Path, run_id: str,
    request: str, issues: tuple[DraftIssue, ...],
) -> tuple[tuple[DraftIssue, ...], str | None]:
    """Run the independent fidelity pass; never fails the round.

    The pass revises titles and bodies only (Issue #2085): the returned
    Issues are the answer's wording at the drafter's positions, so the
    number of Issues and every `after` link stay exactly as the drafter
    wrote them. The pass is a bypass (Issue #79): a missing rules file, a
    failed or empty session, an unusable answer or a different Issue count
    keeps the draft the drafter wrote and the round reports it in the
    `warning` field.
    """
    rules_path = Path(config.deploy_home) / FIDELITY_REL
    try:
        rules = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        return issues, f"fidelity_pass_unavailable: {exc}"
    context = json.dumps(
        {"request": request, "issues": [i.as_json() for i in issues]},
        ensure_ascii=False,
    )
    try:
        output = run_fidelity_session(
            config, repo, session_root=session_root / FIDELITY_DIR,
            system_prompt=f"{rules}\n\n{FIDELITY_PROTOCOL}",
            context=context, run_id=run_id,
        )
    except Exception as exc:
        return issues, f"fidelity_pass_failed: {type(exc).__name__}"
    revised = _parse_revised(output)
    if revised is None or len(revised) != len(issues):
        return issues, "fidelity_pass_empty"
    return tuple(
        DraftIssue(title=entry.title, body=entry.body, after=drafted.after)
        for entry, drafted in zip(revised, issues)
    ), None


# --- one drafting round --------------------------------------------------

def _fidelity_request(payload: dict) -> str:
    """The person's own words: every earlier person turn, then the message.

    The fidelity pass removes what the person did not ask for, so it must
    hear everything the person asked for. On an answer round the latest
    message is only the answer, and an outcome stated in an earlier round
    would be stripped out of the draft (Issue #2050). A round with no
    earlier person turn sends exactly the message, as before. A turn
    without a role is the person's, the way `render_context` reads it.
    """
    message = str(payload["message"]).strip()
    conversation = payload.get("conversation")
    turns: list[str] = []
    for turn in conversation if isinstance(conversation, list) else ():
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "person") != "person":
            continue
        text = str(turn.get("text") or "").strip()
        if text:
            turns.append(text)
    if not turns:
        return message
    return "\n\n".join((
        "The person's words in this conversation, oldest first, the "
        "latest message last:",
        *turns,
        message,
    ))


def run_round(
    config: RunnerConfig, repo: str, *, session_dir: Path, payload: dict,
    run_id: str,
) -> DraftRound:
    """Run one drafting round: questions, or a checked draft.

    Order: session -> fidelity pass -> the pre-work check -> at most ONE
    rewrite with the check's own reasons -> the check again. There is no
    loop and no second rewrite.
    """
    payload = _round_payload(payload)
    session_root = Path(session_dir).resolve()
    session_root.mkdir(parents=True, exist_ok=True)
    out_path = session_root / DRAFT_ANSWER_FILE
    resume = any((session_root / DRAFT_SESSION_DIR).glob("*.jsonl"))
    repo_dir = Path(config.repo_dir)
    _run_answer_session(
        config, repo, session_root=session_root,
        context=render_context(payload, repo_dir=repo_dir),
        out_path=out_path, run_id=run_id, resume=resume,
    )
    answer = read_answer(out_path)
    if answer.questions:
        return DraftRound(reply=answer.reply, questions=answer.questions)
    issues, warning = fidelity_pass(
        config, repo, session_root=session_root, run_id=run_id,
        request=_fidelity_request(payload), issues=answer.issues,
    )
    issues, check, warning, reply = _apply_check(
        config, repo, session_root=session_root, payload=payload,
        run_id=run_id, issues=issues, warning=warning, reply=answer.reply,
    )
    return DraftRound(
        reply=reply, issues=issues, check=check, warning=warning,
    )


def _apply_check(
    config: RunnerConfig, repo: str, *, session_root: Path, payload: dict,
    run_id: str, issues: tuple[DraftIssue, ...], warning: str | None,
    reply: str,
) -> tuple[tuple[DraftIssue, ...], str, str | None, str]:
    """Judge the draft; on a failure rewrite it ONCE and judge again.

    Returns the issues, the check result (passed, or the failed criteria
    as a comma-separated list), the fidelity warning and the reply. The
    reply travels back from the rewrite too: it is the one sentence of
    what will change that the person reads above the draft, so it must
    describe the draft actually shown. There is no loop: a rewrite that
    still fails goes back to the person with the checks it failed, and the
    person may submit it.
    """
    check, failed = check_draft(config, repo, run_id=run_id, issues=issues)
    if check == "passed":
        return issues, check, warning, reply
    rewrite_payload = dict(payload)
    rewrite_payload["message"] = REWRITE_INSTRUCTION.format(
        reasons="\n".join(f"- {item}" for item in failed),
    )
    rewrite_payload["draft"] = {"issues": [i.as_json() for i in issues]}
    try:
        _run_answer_session(
            config, repo, session_root=session_root,
            context=render_context(rewrite_payload, repo_dir=Path(
                config.repo_dir,
            )),
            out_path=session_root / DRAFT_ANSWER_FILE, run_id=run_id,
            resume=True,
        )
        answer = read_answer(session_root / DRAFT_ANSWER_FILE)
    except DraftError as exc:
        return issues, check, warning or f"rewrite_failed: {exc.code}", reply
    if answer.questions or not answer.issues:
        return issues, check, warning or "rewrite_failed: no_draft", reply
    rewritten = answer.issues
    check, _failed = check_draft(
        config, repo, run_id=run_id, issues=rewritten,
    )
    return rewritten, check, warning, answer.reply


# --- creation ------------------------------------------------------------

def _drafted_body(issue: DraftIssue, header: str) -> str:
    return f"{header}\n\n{issue.body}" if header else issue.body


def _open_milestone(repo: str, milestone: str | None) -> str | None:
    """`milestone` when it is one of the OPEN milestones, else None."""
    if milestone is None:
        return None
    titles = {
        entry.get("title") for entry in list_milestones(repo)
        if entry.get("state") == "open"
    }
    return milestone if milestone in titles else None


def _existing_positions(repo: str, key: str) -> dict[int, int]:
    """The Issues THIS runner already created for one draft key.

    The marker is public text, so only an Issue this runner authored
    counts, exactly like the split-child adoption rule; the position comes
    from the marker, so a re-run finds its own Issues instead of guessing
    from a title.
    """
    runner = runner_login()
    found: dict[int, int] = {}
    for issue in list_issues(
        repo, state="all",
        search=f'"{draft_marker.DRAFT_MARKER}:{key}" in:body',
        json_fields="number,body,author", limit=SEARCH_LIMIT,
    ):
        if author_login_key(issue) != runner:
            continue
        mark = draft_marker.parse_draft_marker(issue.get("body"))
        if mark is None or mark.key != key:
            continue
        found.setdefault(mark.position, int(issue["number"]))
    return found


def _labels(issue: dict) -> list[str]:
    return label_names(issue)


def _queue_positions(
    repo: str, *, key: str, items: tuple[DraftIssue, ...], header: str,
    milestone: str | None, parent: int | None, dispatch_label: str,
) -> list[int]:
    """Create (or adopt) each Issue, then attach, edge and label it.

    The three steps per Issue are the split command's own helpers, in the
    same order: create WITHOUT the dispatch label, add the `blockedBy`
    edge, then the label — so an interrupted run is finished by the next
    one and a created Issue never sits in the claim queue without its
    dependency.
    """
    existing = _existing_positions(repo, key)
    numbers: dict[int, int] = {}
    created: list[int] = []
    for position, item in enumerate(items, start=1):
        number = existing.get(position)
        if number is None:
            number = split_command.create_ticket(
                repo, title=item.title,
                body=draft_marker.with_marker(
                    _drafted_body(item, header), key=key, position=position,
                ),
                milestone=milestone, step="create the drafted Issue",
            )
        state = split_command.child_state(repo, number)
        blocker = (
            numbers.get(item.after) if item.after is not None else None
        )
        split_command.process_child(
            repo, state, parent=parent, blocker=blocker,
            dispatch_label=dispatch_label,
        )
        numbers[position] = number
        created.append(number)
    return created


def create_issues(
    config: RunnerConfig, repo: str, payload: dict,
) -> list[int]:
    """Create the confirmed draft; return the Issue numbers in draft order.

    Three shapes, all idempotent through the draft marker:

    * no `issue`: every drafted Issue is a new ticket;
    * `issue` with ONE drafted Issue: the body is appended to that ticket
      once, then `ai-needs-detail` becomes the dispatch label — the
      `/orbi amend` outcome;
    * `issue` with SEVERAL drafted Issues: that ticket becomes the parent
      and the drafted Issues its children — the `/orbi split` outcome.
    """
    key = payload.get("key")
    if not isinstance(key, str) or not DRAFT_KEY_RE.match(key):
        raise _invalid("key must match [A-Za-z0-9_-]{1,64}")
    items = parse_issues(payload.get("issues"))
    header = payload.get("header")
    header = header.strip() if isinstance(header, str) else ""
    target = payload.get("issue")
    if target is not None and (
        isinstance(target, bool) or not isinstance(target, int) or target <= 0
    ):
        raise _invalid("issue must be an Issue number")
    dispatch_label, milestone = policy_keys(config, repo)
    if target is not None and len(items) == 1:
        return [
            _append_to_issue(
                repo, target=target, item=items[0], header=header, key=key,
                dispatch_label=dispatch_label,
            )
        ]
    if target is not None:
        parent = issue_view(target, ISSUE_FIELDS, repo=repo)
        children = _queue_positions(
            repo, key=key, items=items, header=header,
            milestone=split_command.milestone_title(parent), parent=target,
            dispatch_label=dispatch_label,
        )
        _close_the_parent(
            repo, target, parent_issue=parent,
            dispatch_label=dispatch_label,
        )
        return children
    return _queue_positions(
        repo, key=key, items=items, header=header,
        milestone=milestone, parent=None, dispatch_label=dispatch_label,
    )


def policy_keys(config: RunnerConfig, repo: str) -> tuple[str, str | None]:
    """The repository's dispatch label and open Milestone."""
    milestone, label = _repo_scan_keys(
        config, repo, config.active_milestone,
    )
    return (label or READY_LABEL), _open_milestone(repo, milestone)


def _append_to_issue(
    repo: str, *, target: int, item: DraftIssue, header: str, key: str,
    dispatch_label: str,
) -> int:
    """Append one drafted body to an existing ticket, once."""
    issue = issue_view(target, ISSUE_FIELDS, repo=repo)
    current = issue.get("body") or ""
    mark = draft_marker.parse_draft_marker(current)
    if mark is None or mark.key != key or mark.position != 1:
        section = draft_marker.with_marker(
            _drafted_body(item, header), key=key, position=1,
        )
        write_body(
            target, repo=repo, body=appended_body(current, section),
        )
    if dispatch_label not in _labels(issue):
        edit_issue(target, repo=repo, add=dispatch_label)
    if NEEDS_DETAIL_LABEL in _labels(issue):
        edit_issue(target, repo=repo, remove=NEEDS_DETAIL_LABEL)
    return target


def _close_the_parent(
    repo: str, parent: int, *, parent_issue: dict, dispatch_label: str,
) -> None:
    """Label the parent `ai-epic` and release it, the way split does."""
    labels = _labels(parent_issue)
    add = None if EPIC_LABEL in labels else EPIC_LABEL
    remove = dispatch_label if dispatch_label in labels else None
    if add is not None or remove is not None:
        edit_issue(parent, repo=repo, add=add, remove=remove)
    if NEEDS_DETAIL_LABEL in labels:
        edit_issue(parent, repo=repo, remove=NEEDS_DETAIL_LABEL)
