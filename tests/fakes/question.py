"""The stop-question scene the resume tests run against (Issue #2220).

Two scopes are shared by the tests and belong here rather than in a test
module: one repository behind the adapter seam (with the permission reads
the comment-trust rule makes and a silent notifier), and the three
collaborators the external-takeover stop posts through. Patching them in
one place keeps the test module about the behaviour, and keeps the patch
ratchet's internal-patch count flat (tests/fakes/ is the adapter
contract, not coupling).
"""
from __future__ import annotations

from orbi import review_merge
from seam import seam
from tests.fakes.telegram import reset_notify_state


def bind_repository(monkeypatch, fake, repo_dir) -> None:
    """Serve one FakeGh, the permission reads and a notifier that is off."""
    monkeypatch.setattr(seam, "run_command", fake)
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    monkeypatch.setattr(
        seam, "authenticated_github_login", lambda: "orbi-bot",
    )
    reset_notify_state(monkeypatch, repo_dir)


def bind_external_stop(monkeypatch, *, rollup, posted, labels) -> None:
    """Bind what the external-takeover stop reads and posts through.

    The stop goes through the REAL blocked exit: only its two writes are
    replaced, so `posted` holds the comment GitHub would receive (the
    exit adds the failure kind/record and the community help line).
    """
    monkeypatch.setattr(
        review_merge, "pr_delivery_rollup", lambda *a, **k: rollup,
    )
    monkeypatch.setattr(seam, "apply_label_patch", lambda *a, **k: None)
    monkeypatch.setattr(
        seam, "comment_issue",
        lambda number, *, repo, body: posted.append(body),
    )
    monkeypatch.setattr(
        review_merge, "issue_labels", lambda *a, **k: labels,
    )


def bind_history_failure(monkeypatch, error: Exception | None = None) -> None:
    """Make the stop-question history read fail (the counter is unprovable)."""
    from orbi import question

    failure = error or RuntimeError("gh issue view failed")

    def raiser(*args, **kwargs):
        raise failure

    monkeypatch.setattr(question, "issue_comments", raiser)
