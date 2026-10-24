"""The thin-ticket gate's seams as test fakes (Issue #1904).

The gate is a model judgment on top of two GitHub writes and one comment
read. A test that exercises the gate's decision stubs those seams here,
never with a new `monkeypatch.setattr` site in `tests/` — the patch
ratchet (Issue #789) excludes `tests/fakes/`, the adapter contract.
"""
from __future__ import annotations

from orbi import notify
from seam import seam


class FakeGate:
    """One test's gate seams, with every write and judgment recorded.

    `history` is the Issue comment history the dedup read returns (a
    list of `{"body": ...}` dicts); `history_error` makes that read
    raise instead. `verdict` is the answer `judge_issue_body` returns
    (`None` models an unavailable judgment). Comments, label edits,
    judgments and notifications land on the public attributes so a test
    asserts what the gate really did.
    """

    def __init__(self, monkeypatch, *, history=None, verdict=None,
                 history_error: Exception | None = None,
                 edit_error: Exception | None = None):
        self.history = [] if history is None else history
        self.history_error = history_error
        self.edit_error = edit_error
        self.verdict = verdict
        self.judged: list = []
        self.comments: list = []
        self.edits: list = []
        self.notified: list = []
        monkeypatch.setattr(seam, "issue_comments", self._issue_comments)
        monkeypatch.setattr(seam, "judge_issue_body", self._judge)
        monkeypatch.setattr(seam, "comment_issue", self._comment)
        monkeypatch.setattr(seam, "edit_issue", self._edit)
        monkeypatch.setattr(notify, "label_added", self._notify)

    def _issue_comments(self, number, *, repo):
        if self.history_error is not None:
            raise self.history_error
        return self.history

    def _judge(self, *args, **kwargs):
        self.judged.append(args)
        return self.verdict

    def _comment(self, number, *, repo, body):
        self.comments.append((number, repo, body))

    def _edit(self, number, *, repo, add=None, remove=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((number, repo, add, remove))

    def _notify(self, *args, **kwargs):
        self.notified.append(args)


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
