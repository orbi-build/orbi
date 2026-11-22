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
import sys
from contextlib import contextmanager
from pathlib import Path

from orbi import (
    attachments, clarify, draft_marker, failure, gitops, journal,
    model_catalog, provider_balance, split_command,
)
from orbi.acceptance_section import appended_body, write_body
from orbi.claim import _repo_scan_keys
from orbi.config import RunnerConfig
from orbi.delivery_labels import has_delivery_state, label_names
from orbi import delivery_labels
from orbi.delivery_scene import RunContext
from orbi.draft_document import (
    TAKEN_ROUNDS,
    Creation,
    DraftAnswer,
    DraftError,
    DraftIssue,
    DraftRound,
    invalid_input,
    named_in_reply,
    parse_answer,
    parse_issues,
    read_payload,
    taken_message,
    taken_options,
)
# The prompt text and the three tool names live in their own module (Issue
# #2364): this module's recorded size may only shrink (size ratchet).
from orbi.draft_prompt import (
    ASK_USER_TOOL,
    FIDELITY_PROTOCOL,
    FIDELITY_REL,
    REPLY_TOOL,
    REWRITE_INSTRUCTION,
    WRITE_DRAFT_TOOL,
    draft_system_prompt,
)
from orbi.failure import failure_detail
from orbi.github import edit_issue, issue_view, list_issues, list_milestones
from orbi.journal import LOGGER, event, run_command
from orbi.pi_command import (
    IMPLEMENT_EXCLUDED_SKILLS,
    ROLE_SUGGEST,
    ROLE_TICKET,
    build_pi_command,
)
from orbi.pi_process import PiWatchOptions, stream_pi
from orbi.pi_session import pi_extension_env, prepare_pi_agent_dir
from orbi.question import Option
from orbi.ticket_command import author_login_key, runner_login

# The drafting session reads the checkout and writes ONLY through the
# shipped tools: no shell, no edit, no commit (the same read-only boundary
# `orbi suggest` keeps).
#
# Pi's `--tools` REPLACES the whole tool selection and applies to
# extension tools too ("Applies to built-in, extension, and custom tools" —
# `pi --help`, docs/cli.md), so the three answer tools are named here as
# well. Without them the model has no tool to answer with: the session
# then replies in prose, no answer file is written and every round fails
# `no_answer` (verified against Pi 1.0.0 — the "read,grep,find,ls"-only
# form makes the model report it cannot call write_draft/ask_user).
DRAFT_TOOLS = ",".join((
    "read", "grep", "find", "ls", WRITE_DRAFT_TOOL, ASK_USER_TOOL,
    REPLY_TOOL,
))
DRAFT_EXTENSION = Path(__file__).resolve().parent.joinpath(
    "extensions", "draft_tools.ts",
)
# The extension writes its one answer here; the engine reads the file after
# the session, so a broken stdout never loses a draft the model did write.
DRAFT_OUT_ENV = "ORBI_DRAFT_OUT"
# Issue #2303: the extension validates an answer with the ENGINE's own parser
# before recording it, so a cross-field rule (an 'after' naming no earlier
# Issue) comes back to the model as an error tool result and the model corrects
# it in the same session. The session is handed the command that runs it — this
# interpreter plus the module, as one JSON argv array so a path carrying
# whitespace needs no splitting — never a TypeScript copy of the rule.
DRAFT_VALIDATE_ENV = "ORBI_DRAFT_VALIDATE"
DRAFT_VALIDATE_MODULE = "orbi.draft_validate"
DRAFT_ANSWER_FILE = "answer.json"
# The tools' last rejection reason, kept NEXT TO the answer
# ('answer.json.rejected'): a session that ends without an answer fails with
# that reason instead of a bare timeout. The extension derives the same path
# from DRAFT_OUT_ENV.
DRAFT_REJECTION_SUFFIX = ".rejected"
# The session layout a caller may depend on (docs/public-contracts.mdx):
# the drafting session and the fidelity pass each keep their Pi session
# files under the caller's directory.
DRAFT_SESSION_DIR = ".pi-session"
FIDELITY_DIR = ".fidelity"
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


def _round_payload(payload: dict) -> dict:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise invalid_input("message must be a non-empty string")
    return payload


DRAFT_VIEW_DIRNAME = "draft-view"


def draft_view_path(repo_dir: Path, run_id: str) -> Path:
    """Where one draft's view of `origin/<base>` lives.

    Inside the checkout's gitignored `.orbi/` run dirs, so the deployment
    checkout's working tree is never moved (Issue #2276) and no delivery
    worktree ever sees a file it would call dirty.
    """
    return Path(repo_dir) / ".orbi" / DRAFT_VIEW_DIRNAME / run_id


def add_draft_view(repo_dir: Path, base_branch: str, run_id: str) -> Path:
    """Fetch `origin/<base>` under the base-sync lock, then check it out.

    The detached worktree of the fetched commit IS the drafting session's
    repository view: a delivery never advances the checkout, so its working
    tree can be arbitrarily old. `--detach` keeps the view off every branch,
    so a draft can neither move the checkout nor touch a delivery's
    worktree, and only the fetch writes shared state (the one remote-tracking
    ref the lock guards). `--force` re-creates over the `prunable` entry a
    killed draft left at the same path.
    """
    gitops.fetch_base_ref(repo_dir, base_branch)
    path = draft_view_path(repo_dir, run_id)
    run_command(
        ["git", "worktree", "add", "--detach", "--force", str(path),
         f"origin/{base_branch}"],
        cwd=repo_dir,
    )
    return path


def remove_draft_view(repo_dir: Path, path: Path) -> None:
    """Remove a draft's view; a view already gone is nothing to remove.

    No `git worktree prune` here: the next view is created with
    `--force`, which re-creates over the `prunable` registration a killed
    draft left behind, while a prune would touch the one `worktrees/`
    registry a delivery running at the same time is writing to.
    """
    if not Path(path).is_dir():
        return
    run_command(
        ["git", "worktree", "remove", "--force", str(path)], cwd=repo_dir,
    )


@contextmanager
def draft_repo_view(config: RunnerConfig, *, run_id: str):
    """The repository state ONE draft reads (Issue #2276).

    Yields a detached worktree of `origin/<base_branch>`, fetched at the
    moment the draft starts. The deployment checkout is never advanced —
    deliveries build their worktrees from the remote — so its working tree
    can predate the base by weeks, and a draft that reads it reports
    existing files as missing and cites names the base renamed.

    A failed fetch FAILS the draft (`draft_failed`): a failed draft is
    recoverable, a stale read is the defect this replaces, so there is no
    fallback to the checkout. The view is removed when the draft ends; the
    checkout and every delivery worktree are never touched.
    """
    repo_dir = Path(config.repo_dir)
    base_branch = config.base_branch
    try:
        view = add_draft_view(repo_dir, base_branch, run_id)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        raise DraftError(
            "draft_failed", f"read the repository at origin/{base_branch}",
            failure_detail(exc),
        ) from exc
    try:
        yield view
    finally:
        try:
            remove_draft_view(repo_dir, view)
        except subprocess.SubprocessError as exc:
            LOGGER.warning(
                "draft view %s could not be removed: %s", view, exc,
            )


def render_context(
    payload: dict, *, repo_dir: Path,
    attached: "attachments.Attached | None" = None,
) -> str:
    """The one context a round hands the session.

    The conversation and the current draft are REPLAYED here, not read back
    from Pi's session file: a round with no session file at all still works,
    and the same context shape is what a second round resumes with. The
    draft's attached files are named in one line (Issue #2364).
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
    naming = attached.naming() if attached is not None else ""
    if naming:
        lines.extend([naming, ""])
    lines.append(
        f"Repository checkout: {repo_dir} (read-only; refer to files by "
        "their repository-relative path)."
    )
    return "\n".join(lines)


# --- the drafting session ------------------------------------------------



def run_draft_session(
    config: RunnerConfig, repo: str, *, session_root: Path,
    system_prompt: str, context: str, run_id: str, out_path: Path,
    resume: bool = False, attached: "attachments.Attached | None" = None,
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
    command = attachments.with_arguments(
        command, attached.arguments() if attached else [],
    )
    agent_dir = prepare_pi_agent_dir(session_root, config, role=ROLE_SUGGEST)
    pi_env = pi_extension_env(config)
    if agent_dir is not None:
        pi_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    pi_env[DRAFT_OUT_ENV] = str(out_path)
    pi_env[DRAFT_VALIDATE_ENV] = json.dumps(
        [sys.executable, "-m", DRAFT_VALIDATE_MODULE]
    )
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
    attached: "attachments.Attached | None" = None,
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
    # The pass reads the draft's files as arguments: it has no tool to read
    # one, and it must not remove what came from an attachment.
    command = attachments.with_arguments(
        command, attached.arguments(text=True) if attached else [],
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
    attached: "attachments.Attached | None" = None,
) -> None:
    """Run the session and keep a draft that a timeout left behind.

    The answer file is removed first, so an answer found after a failure is
    the one THIS round wrote: a round that timed out after the model wrote
    its draft still returns that draft, and a round that wrote nothing
    fails. A session that ends without an answer after the tools refused one
    fails with that last rejection reason (Issue #2303) — whether Pi failed or
    ended the turn cleanly — so the person reads why the draft failed instead
    of a bare `timeout` or `no_answer`. A model-provider balance
    refusal is its own code with the plain sentence as its detail — never
    `session_failed` and never the Python exception text, which is where the
    provider's raw payload lives (Issue #2278).
    """
    out_path.unlink(missing_ok=True)
    # The reason belongs to ONE session: a previous round's rejection must
    # never explain this round's failure.
    rejection_path(out_path).unlink(missing_ok=True)
    try:
        run_draft_session(
            config, repo, session_root=session_root,
            system_prompt=draft_system_prompt(), context=context,
            run_id=run_id, out_path=out_path, resume=resume,
            attached=attached,
        )
    except Exception as exc:
        if not out_path.is_file():
            if provider_balance.is_stop(failure.failure_detail(exc)):
                raise DraftError(
                    provider_balance.DRAFT_CODE, "run the drafting session",
                    provider_balance.DETAIL,
                ) from exc
            raise_if_rejected(out_path)
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
        return
    if not out_path.is_file():
        # Pi ended the turn cleanly and left no answer: the model stopped
        # answering after a rejected one, which fails like the timeout path.
        raise_if_rejected(out_path)


def raise_if_rejected(out_path: Path) -> None:
    """Fail `invalid_draft` with the tool's last reason, when it refused one."""
    rejected = last_rejection(out_path)
    if rejected is not None:
        raise DraftError("invalid_draft", "run the drafting session", rejected)


def rejection_path(out_path: Path) -> Path:
    """Where the draft tools record their last rejection (Issue #2303).

    Derived from the answer file's own path, so the extension and the engine
    agree on one place without a second environment variable.
    """
    return Path(f"{out_path}{DRAFT_REJECTION_SUFFIX}")


def last_rejection(out_path: Path) -> str | None:
    """The reason the tools last refused an answer in THIS session, if any.

    The tools write it before answering the model with the same reason, so a
    session that ends there fails with the reason the person can act on.
    """
    try:
        reason = rejection_path(out_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return reason or None


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
    attached: "attachments.Attached | None" = None,
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
            context=context, run_id=run_id, attached=attached,
        )
    except Exception as exc:
        return issues, f"fidelity_pass_failed: {type(exc).__name__}"
    revised = _parse_revised(output)
    if revised is None or len(revised) != len(issues):
        return issues, "fidelity_pass_empty"
    return tuple(
        DraftIssue(
            title=entry.title, body=entry.body, after=drafted.after,
            attachments=drafted.attachments,
        )
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
    """Run one drafting round: questions, a reply alone, or a checked draft.

    Order: session -> the options the engine takes itself -> fidelity pass ->
    the pre-work check -> at most ONE rewrite with the check's own reasons ->
    the check again. There is no loop and no second rewrite. A reply-only
    answer (Issue #2201) asks for no code change, so neither the fidelity
    pass nor the check runs.
    """
    payload = _round_payload(payload)
    session_root = Path(session_dir).resolve()
    session_root.mkdir(parents=True, exist_ok=True)
    out_path = session_root / DRAFT_ANSWER_FILE
    try:
        attached = attachments.stage(
            payload.get("attachments"),
            session_root / DRAFT_SESSION_DIR / attachments.DIRECTORY,
            viewable=model_catalog.declares_image_input(config),
        )
    except attachments.AttachmentError as exc:
        raise DraftError(
            "invalid_input", "read the attachments", str(exc),
        ) from None
    with draft_repo_view(config, run_id=run_id) as repo_view:
        payload, answer, taken = _answered_round(
            config, repo, session_root=session_root, payload=payload,
            out_path=out_path, run_id=run_id, repo_view=repo_view,
            attached=attached,
        )
        if answer.questions:
            return DraftRound(
                reply=named_in_reply(answer.reply, taken),
                questions=answer.questions,
            )
        if not answer.issues:
            # Issue #2201: a reply-only answer asks for no code change, so
            # neither the fidelity pass nor the pre-work check runs.
            return DraftRound(reply=named_in_reply(answer.reply, taken))
        issues, warning = fidelity_pass(
            config, repo, session_root=session_root, run_id=run_id,
            request=_fidelity_request(payload), issues=answer.issues,
            attached=attached,
        )
        issues, check, warning, reply = _apply_check(
            config, repo, session_root=session_root, payload=payload,
            run_id=run_id, issues=issues, warning=warning, reply=answer.reply,
            repo_view=repo_view, attached=attached,
        )
        return DraftRound(
            reply=named_in_reply(reply, taken), issues=issues,
            check=check, warning=warning,
        )


def _answered_round(
    config: RunnerConfig, repo: str, *, session_root: Path, payload: dict,
    out_path: Path, run_id: str, repo_view: Path,
    attached: "attachments.Attached | None" = None,
) -> tuple[dict, DraftAnswer, tuple[Option, ...]]:
    """Run the session until it drafts or asks what only the person can answer.

    A question carrying a recommended option is the engine's own answer
    (Issue #2198): the option is taken and the round continues in the same
    session, with those decisions replayed the way a caller replays a
    person's answer. Returns the payload the draft was written from, the
    answer, and the options taken on the way.
    """
    message = str(payload["message"]).strip()
    turns = payload.get("conversation")
    turns = list(turns) if isinstance(turns, list) else []
    taken: list[Option] = []
    for _round in range(TAKEN_ROUNDS):
        round_payload = {**payload, "message": message, "conversation": turns}
        _run_answer_session(
            config, repo, session_root=session_root,
            context=render_context(
                round_payload, repo_dir=repo_view, attached=attached,
            ),
            out_path=out_path, run_id=run_id, attached=attached,
            resume=any((session_root / DRAFT_SESSION_DIR).glob("*.jsonl")),
        )
        answer = read_answer(out_path)
        if not answer.questions:
            return round_payload, answer, tuple(taken)
        chosen, waiting = taken_options(answer.questions)
        taken.extend(chosen)
        if waiting:
            return (
                round_payload,
                DraftAnswer(reply=answer.reply, questions=waiting),
                tuple(taken),
            )
        turns = [*turns, {"role": "person", "text": message},
                 {"role": "orbi", "text": answer.reply}]
        message = taken_message(tuple(taken))
    raise DraftError(
        "too_many_rounds", "run the drafting round",
        "the drafting session still asks a question it can answer itself "
        f"after {TAKEN_ROUNDS} rounds",
    )


def _apply_check(
    config: RunnerConfig, repo: str, *, session_root: Path, payload: dict,
    run_id: str, issues: tuple[DraftIssue, ...], warning: str | None,
    reply: str, repo_view: Path,
    attached: "attachments.Attached | None" = None,
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
            context=render_context(
                rewrite_payload, repo_dir=repo_view, attached=attached,
            ),
            out_path=session_root / DRAFT_ANSWER_FILE, run_id=run_id,
            resume=True, attached=attached,
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
    filed: "attachments.Filing | None" = None,
    copied: "attachments.Copied | None" = None,
) -> Creation:
    """Create (or adopt) each Issue, then attach, edge and label it.

    The three steps per Issue are the split command's own helpers, in the
    same order: create WITHOUT the dispatch label, add the `blockedBy`
    edge, then the label — so an interrupted run is finished by the next
    one and a created Issue never sits in the claim queue without its
    dependency. The Attachments section is written in the SAME create, below
    the draft marker (Issue #2364), restricted to the files the Issue uses:
    an upload of the draft's own files for a new ticket, or the links copied
    from the original Issue's body for the '/orbi split' outcome.
    """
    existing = _existing_positions(repo, key)
    numbers: dict[int, int] = {}
    created: list[int] = []
    unuploaded: list[str] = []
    for position, item in enumerate(items, start=1):
        source = attachments.source(
            item.attachments, position, filed=filed, copied=copied,
        )
        number = existing.get(position)
        if number is None:
            number = split_command.create_ticket(
                repo, title=item.title,
                body=draft_marker.with_marker(
                    _drafted_body(item, header), key=key, position=position,
                    section=attachments.section(source),
                ),
                milestone=milestone, step="create the drafted Issue",
                filed=source if isinstance(source, attachments.Filing) else None,
            )
            if isinstance(source, attachments.Filing):
                unuploaded.extend(source.unuploaded(
                    issue_view(number, "body", repo=repo).get("body"),
                ))
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
    return Creation(numbers=created, unuploaded=tuple(unuploaded))


def create_issues(
    config: RunnerConfig, repo: str, payload: dict,
) -> Creation:
    """Create the confirmed draft; the numbers in draft order.

    Three shapes, all idempotent through the draft marker:

    * no `issue`: every drafted Issue is a new ticket, carrying the draft's
      attachments in its Attachments section (Issue #2364);
    * `issue` with ONE drafted Issue: the body is appended to that ticket
      once, then `ai-needs-detail` becomes the dispatch label — the
      `/orbi amend` outcome, which carries no attachments;
    * `issue` with SEVERAL drafted Issues: that ticket becomes the parent
      and the drafted Issues its children — the `/orbi split` outcome,
      where each child copies the links of the attachments it uses from
      the parent's body instead of uploading files again.
    """
    key = payload.get("key")
    if not isinstance(key, str) or not DRAFT_KEY_RE.match(key):
        raise invalid_input("key must match [A-Za-z0-9_-]{1,64}")
    items = parse_issues(payload.get("issues"))
    header = payload.get("header")
    header = header.strip() if isinstance(header, str) else ""
    target = payload.get("issue")
    if target is not None and (
        isinstance(target, bool) or not isinstance(target, int) or target <= 0
    ):
        raise invalid_input("issue must be an Issue number")
    dispatch_label, milestone = policy_keys(config, repo)
    if target is not None and len(items) == 1:
        return Creation([_append_to_issue(
            repo, target=target, item=items[0], header=header, key=key,
            dispatch_label=dispatch_label,
        )])
    if target is not None:
        parent = issue_view(target, ISSUE_FIELDS, repo=repo)
        try:
            children = _queue_positions(
                repo, key=key, items=items, header=header,
                milestone=split_command.milestone_title(parent), parent=target,
                dispatch_label=dispatch_label,
                copied=attachments.copied(parent.get("body")),
            )
        except attachments.AttachmentError as exc:
            raise DraftError(
                "invalid_input", "read the attachments", str(exc),
            ) from None
        _close_the_parent(
            repo, target, parent_issue=parent,
            dispatch_label=dispatch_label,
        )
        return children
    try:
        with attachments.filing(payload.get("attachments")) as filed:
            return _queue_positions(
                repo, key=key, items=items, header=header,
                milestone=milestone, parent=None,
                dispatch_label=dispatch_label, filed=filed,
            )
    except attachments.AttachmentError as exc:
        raise DraftError(
            "invalid_input", "read the attachments", str(exc),
        ) from None


def policy_keys(config: RunnerConfig, repo: str) -> tuple[str, str | None]:
    """The repository's dispatch label and open Milestone."""
    milestone, label = _repo_scan_keys(
        config, repo, config.active_milestone,
    )
    return delivery_labels.queue_label(label), _open_milestone(repo, milestone)


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
    # The retry finishes an interrupted draft (body, label, detail stop)
    # and queues the ticket ONLY while nothing has started (Issue #2309):
    # the claim removes the dispatch label, so its absence no longer means
    # "not yet dispatched" — a stage label means the ticket is in flight.
    labels = _labels(issue)
    if dispatch_label not in labels and not has_delivery_state(labels):
        edit_issue(target, repo=repo, add=dispatch_label)
    if delivery_labels.NEEDS_DETAIL_LABEL in labels:
        edit_issue(target, repo=repo, remove=delivery_labels.NEEDS_DETAIL_LABEL)
    return target


def _close_the_parent(
    repo: str, parent: int, *, parent_issue: dict, dispatch_label: str,
) -> None:
    """Label the parent `ai-epic` and release it, the way split does."""
    labels = _labels(parent_issue)
    add = None if delivery_labels.EPIC_LABEL in labels else delivery_labels.EPIC_LABEL
    remove = dispatch_label if dispatch_label in labels else None
    if add is not None or remove is not None:
        edit_issue(parent, repo=repo, add=add, remove=remove)
    if delivery_labels.NEEDS_DETAIL_LABEL in labels:
        edit_issue(parent, repo=repo, remove=delivery_labels.NEEDS_DETAIL_LABEL)
