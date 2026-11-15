"""The thin-ticket gate's seams as test fakes (Issue #1904).

The gate is a model judgment on top of two GitHub writes and one comment
read. A test that exercises the gate's decision stubs those seams here,
never with a new `monkeypatch.setattr` site in `tests/` — the patch
ratchet (Issue #789) excludes `tests/fakes/`, the adapter contract.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orbi import clarify, notify
from seam import seam

# The login the fake runner posts its comments with. The real `gh auth
# status` reports an App with its `[bot]` suffix while GraphQL drops it,
# so the fake exercises the same normalization the real read does.
RUNNER_LOGIN = "orbi-bot[bot]"


def stop_comment_entry(body: str, login: str = RUNNER_LOGIN) -> dict:
    """One Issue comment carrying `body`, authored by `login`.

    The shape `gh issue view --json comments` returns: the gate reads the
    stop marker only from the runner's own comments (Issue #1904).
    """
    return {"body": body, "author": {"login": login}}


class FakeGate:
    """One test's gate seams, with every write and judgment recorded.

    `history` is the Issue comment history the dedup read returns (a
    list of `stop_comment_entry` dicts); `history_error`
    makes that read raise instead, `login_error` makes the
    runner-identity read raise, and `runner_login` is the login a stop's
    author is compared with. `verdict` is the answer `judge_issue_body`
    returns (`None` models an unavailable judgment). `body_error` makes
    the body write raise and `comment_errors` makes that many comment
    writes fail first (Issue #2206's fallback). Comments, body writes,
    label edits, judgments and notifications land on the public
    attributes so a test asserts what the gate really did.
    """

    def __init__(self, monkeypatch, *, history=None, verdict=None,
                 history_error: Exception | None = None,
                 edit_error: Exception | None = None,
                 body_error: Exception | None = None,
                 comment_errors: int = 0,
                 runner_login: str = RUNNER_LOGIN,
                 login_error: Exception | None = None):
        self.history = [] if history is None else history
        self.history_error = history_error
        self.edit_error = edit_error
        self.body_error = body_error
        self.comment_errors = comment_errors
        self.runner_login = runner_login
        self.login_error = login_error
        self.verdict = verdict
        self.judged: list = []
        self.comments: list = []
        self.edits: list = []
        self.bodies: list = []
        self.notified: list = []
        monkeypatch.setattr(seam, "issue_comments", self._issue_comments)
        monkeypatch.setattr(seam, "judge_issue_body", self._judge)
        monkeypatch.setattr(seam, "comment_issue", self._comment)
        monkeypatch.setattr(seam, "edit_issue", self._edit)
        monkeypatch.setattr(seam, "write_body", self._write_body)
        monkeypatch.setattr(
            seam, "authenticated_github_login", self._login,
        )
        monkeypatch.setattr(notify, "label_added", self._notify)

    def _issue_comments(self, number, *, repo):
        if self.history_error is not None:
            raise self.history_error
        return self.history

    def _login(self) -> str:
        if self.login_error is not None:
            raise self.login_error
        return self.runner_login

    def _judge(self, *args, **kwargs):
        self.judged.append(args)
        return self.verdict

    def _comment(self, number, *, repo, body):
        if self.comment_errors > 0:
            # A transient GitHub write: the NEXT comment (the stop's) lands,
            # so the fallback path is what the test observes.
            self.comment_errors -= 1
            raise RuntimeError("gh write failed")
        self.comments.append((number, repo, body))

    def _edit(self, number, *, repo, add=None, remove=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((number, repo, add, remove))

    def _write_body(self, number, *, repo, body):
        if self.body_error is not None:
            raise self.body_error
        self.bodies.append((number, repo, body))

    def _notify(self, *args, **kwargs):
        self.notified.append(args)


def verdict_agent(monkeypatch, payload=None, *, raw=None, calls=None):
    """Stub the gate's model call as a fake Pi that writes its verdict file.

    Since Issue #1955 the gate asks for the verdict through the shipped
    `clarify_verdict` Pi tool: the engine names the output file in
    `ORBI_CLARIFY_OUT` and reads the JSON the tool wrote there. This fake
    mirrors that contract from the engine side — it takes the path out of
    the `env` the gate passes, writes `payload` (or `raw` verbatim) and
    returns empty stdout, which the gate no longer parses. Both None
    writes nothing: the missing-answer path.

    `calls` (a list, when given) receives (issue, config, source_repo,
    kwargs) per judgment, the same shape the old inline stubs recorded.
    """
    def agent(issue, config, source_repo, **kwargs):
        if calls is not None:
            calls.append((issue, config, source_repo, kwargs))
        path = Path(kwargs["env"][clarify.CLARIFY_VERDICT_ENV])
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        elif payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return ""

    monkeypatch.setattr(seam, "run_ticket_agent", agent)
    return calls


def stub_runner_login(monkeypatch, login: str = RUNNER_LOGIN) -> None:
    """Pin the login the runner's own comments are recognized by.

    The gate reads its stop marker only from comments authored by this
    account (Issue #1904). The patch lives here so it stays a boundary
    seam, not a new internal patch in a test module (Issue #789).
    """
    monkeypatch.setattr(seam, "authenticated_github_login", lambda: login)


def fix_run_id(monkeypatch, run_id: str) -> None:
    """Bind a real-runner attempt to one deterministic run id."""
    monkeypatch.setattr(seam, "new_run_id", lambda: run_id)


def stub_stop_history(monkeypatch, comments) -> None:
    """Answer the gate's stop-history read with `comments`."""
    monkeypatch.setattr(
        seam, "issue_comments",
        lambda number, *, repo: comments,
    )


def record_label_notifications(monkeypatch) -> list:
    """Record `notify.label_added` calls and return the list."""
    notified: list = []
    monkeypatch.setattr(
        notify, "label_added",
        lambda *args, **kwargs: notified.append(args),
    )
    return notified


# --- the gate over real GitHub state (Issue #2207) ------------------------

def stub_judgment(monkeypatch, verdict) -> None:
    """Answer the gate's judgment with `verdict`, no Pi session runs."""
    monkeypatch.setattr(
        seam, "judge_issue_body", lambda *args, **kwargs: verdict,
    )


def install_github(monkeypatch, gh) -> None:
    """Answer every GitHub call with `tests.fakes.github.FakeGh`.

    The gate's own apply reads and writes real Issue state — labels,
    native relations, comments — so a recording fake cannot prove it. The
    patch is the Article 3.4 subprocess seam, a boundary fake.
    """
    monkeypatch.setattr(seam, "run_command", gh)
    monkeypatch.setattr(notify, "label_added", lambda *args, **kwargs: None)


def fail_github(monkeypatch, gh, predicate, *,
                reason: str = "gh: HTTP 422: Validation Failed") -> None:
    """Let `gh` answer, except the commands `predicate` refuses.

    The refusal arrives the way a real one does: a non-zero process with
    the reason on stderr.
    """
    def call(command, **kwargs):
        if predicate(command):
            raise subprocess.CalledProcessError(1, command, stderr=reason)
        return gh(command, **kwargs)
    monkeypatch.setattr(seam, "run_command", call)


def interrupt_github(monkeypatch, gh, predicate) -> None:
    """Answer GitHub, but interrupt the process at the matching command.

    `KeyboardInterrupt` is what a killed tick looks like from inside the
    apply: the gate catches `Exception` only, so nothing is rolled back
    and whatever already reached GitHub stays there.
    """
    def call(command, **kwargs):
        if predicate(command):
            raise KeyboardInterrupt
        return gh(command, **kwargs)
    monkeypatch.setattr(seam, "run_command", call)
