"""The drafting fakes (Issue #2041).

They live in tests/fakes/ because the patch ratchet excludes it: a test
that drives orbi draft patches two session leaves, one subprocess seam and
the pre-work judgment, and those patches are contract, not coupling.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from orbi import clarify, draft
from seam import seam

from tests.fakes.github import FakeGh


class GhRecorder:
    """The seam fake: gh is FakeGh, and every argv is recorded in order."""

    def __init__(self, gh: FakeGh) -> None:
        self.gh = gh
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return self.gh(list(command), **kwargs)

    def issues_commands(self) -> list[list[str]]:
        return [c for c in self.commands if c[:2] == ["gh", "issue"]]


def structured_question(question: str, *, header: str = "Choice") -> dict:
    """One question in the object shape the hosted page reads (Issue #2136)."""
    return {
        "question": question,
        "header": header,
        "options": [
            {"label": "Local store (Recommended)",
             "description": "Keeps everything in one file."},
            {"label": "Remote store",
             "description": "Reads and writes through a server."},
        ],
    }


class FakeSessions:
    """The drafting and fidelity sessions, answered from queues."""

    def __init__(self) -> None:
        self.rounds: list[object] = []
        self.calls: list[SimpleNamespace] = []
        self.fidelity: list[object] = []
        self.fidelity_calls: list[SimpleNamespace] = []

    def queue(self, *answers: object) -> None:
        self.rounds.extend(answers)

    def queue_draft(self, *issues: dict,
                    reply: str = "It will change X.") -> None:
        self.rounds.append(
            {"kind": "draft", "reply": reply, "issues": list(issues)},
        )

    def queue_questions(self, *questions: object) -> None:
        self.rounds.append({
            "kind": "questions",
            "reply": "Which store should it read?",
            "questions": [
                question if isinstance(question, dict)
                else structured_question(question)
                for question in questions
            ] or [structured_question("Q?")],
        })

    def session(self, config, repo, *, session_root, system_prompt, context,
                run_id, out_path, resume=False):
        self.calls.append(SimpleNamespace(
            session_root=Path(session_root), system_prompt=system_prompt,
            context=context, resume=resume, out_path=Path(out_path),
        ))
        (Path(session_root) / draft.DRAFT_SESSION_DIR).mkdir(
            parents=True, exist_ok=True,
        )
        # A session file, the way Pi leaves one: the next round sees it.
        (Path(session_root) / draft.DRAFT_SESSION_DIR / "s.jsonl").write_text(
            "{}\n", encoding="utf-8",
        )
        answer = self.rounds.pop(0)
        if isinstance(answer, tuple):
            payload, error = answer
            if payload is not None:
                Path(out_path).write_text(
                    json.dumps(payload), encoding="utf-8",
                )
            raise error
        Path(out_path).write_text(json.dumps(answer), encoding="utf-8")

    def fidelity_session(self, config, repo, *, session_root, system_prompt,
                         context, run_id):
        self.fidelity_calls.append(SimpleNamespace(
            session_root=Path(session_root), system_prompt=system_prompt,
            context=context,
        ))
        if self.fidelity:
            answer = self.fidelity.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return json.dumps({"issues": json.loads(context)["issues"]})


def _record_fidelity(sessions, *, session_root, system_prompt, context):
    """Record one fidelity call and return the payload the pass received."""
    sessions.fidelity_calls.append(SimpleNamespace(
        session_root=Path(session_root), system_prompt=system_prompt,
        context=context,
    ))
    return json.loads(context)


def fidelity_echoes_request(monkeypatch, sessions) -> None:
    """The fidelity pass answers with the request it was given.

    A test reads the request back out of the returned draft, so the
    assertion covers the words that actually reached the pass, not only
    the recorded context.
    """

    def echo(config, repo, *, session_root, system_prompt, context, run_id):
        payload = _record_fidelity(
            sessions, session_root=session_root,
            system_prompt=system_prompt, context=context,
        )
        return json.dumps({"issues": [
            {"title": "Echo", "body": payload["request"]},
        ]})

    monkeypatch.setattr(seam, "run_fidelity_session", echo)


def _mentioned(line: str, request: str) -> bool:
    """Whether the request names this body line, markdown aside."""
    text = line.strip().lstrip("-* ").strip().rstrip(".").lower()
    return bool(text) and text in request


def fidelity_removes_unmentioned_lines(monkeypatch, sessions) -> None:
    """The fidelity pass drops every body line the request does not name.

    That is the pass's own job — remove what the person did not ask for —
    reduced to the one deterministic rule a test can assert on: headings
    stay, and a body line stays only when the request mentions it
    verbatim.
    """

    def strip(config, repo, *, session_root, system_prompt, context, run_id):
        payload = _record_fidelity(
            sessions, session_root=session_root,
            system_prompt=system_prompt, context=context,
        )
        request = payload["request"].lower()
        issues = []
        for entry in payload["issues"]:
            kept = [
                line for line in entry["body"].splitlines()
                if line.lstrip().startswith("#")
                or not line.strip()
                or _mentioned(line, request)
            ]
            issues.append({**entry, "body": "\n".join(kept)})
        return json.dumps({"issues": issues})

    monkeypatch.setattr(seam, "run_fidelity_session", strip)


def install_gh(monkeypatch, gh: FakeGh) -> GhRecorder:
    """Route the one subprocess seam to the in-memory GitHub."""
    route = GhRecorder(gh)
    monkeypatch.setattr(seam, "run_command", route)
    return route


def install_sessions(monkeypatch) -> FakeSessions:
    """Replace the drafting session and the fidelity pass."""
    fake = FakeSessions()
    monkeypatch.setattr(seam, "run_draft_session", fake.session)
    monkeypatch.setattr(seam, "run_fidelity_session", fake.fidelity_session)
    return fake


def judge_sequence(monkeypatch, verdicts=()) -> list[dict]:
    """Answer the pre-work check from a (passed, missing) list.

    An empty list passes every Issue; the list is consumed in call order,
    so a test scripts "fails, then passes after the rewrite". The judged
    payloads are returned.
    """
    queue = list(verdicts)
    judged: list[dict] = []

    def judge(issue_payload, config, repo, run_id):
        judged.append(issue_payload)
        if not queue:
            return clarify.ClarifyVerdict(passed=True, missing=())
        passed, missing = queue.pop(0)
        return clarify.ClarifyVerdict(
            passed=passed, missing=tuple(missing),
        )

    monkeypatch.setattr(clarify, "judge_issue_body", judge)
    return judged


def judge_passing(monkeypatch) -> list[dict]:
    """Every drafted Issue passes; returns the judged payloads."""
    return judge_sequence(monkeypatch)


def judge_failing(monkeypatch, *missing: str) -> list[dict]:
    """Every drafted Issue fails the named checks."""
    judged: list[dict] = []

    def judge(issue_payload, config, repo, run_id):
        judged.append(issue_payload)
        return clarify.ClarifyVerdict(passed=False, missing=tuple(missing))

    monkeypatch.setattr(clarify, "judge_issue_body", judge)
    return judged


def judge_unavailable(monkeypatch) -> None:
    """The judgment is unavailable (a model error): the check fails open."""
    monkeypatch.setattr(
        clarify, "judge_issue_body", lambda *args, **kwargs: None,
    )


def stub_login(monkeypatch, login: str = "orbi-bot") -> None:
    """The account this runner authenticates as."""
    monkeypatch.setattr(seam, "authenticated_github_login", lambda: login)


def stub_login_error(monkeypatch) -> None:
    """The account read fails (a broken gh credential)."""
    def boom() -> str:
        raise RuntimeError("gh auth status failed")

    monkeypatch.setattr(seam, "authenticated_github_login", boom)


def stub_stream_pi(monkeypatch, output: str = "") -> list[dict]:
    """Replace the Pi process: record one session call, answer output."""
    calls: list[dict] = []

    def stream(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return output

    monkeypatch.setattr(seam, "stream_pi", stream)
    return calls


def stub_agent_dir(monkeypatch, path: Path) -> None:
    """A configured provider file: the per-run agent dir is materialized."""
    monkeypatch.setattr(
        seam, "prepare_pi_agent_dir", lambda *args, **kwargs: path,
    )


def stub_create_error(monkeypatch, error: Exception) -> None:
    """Creation fails with the given exception."""

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(draft, "create_issues", fail)
