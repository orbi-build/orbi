"""The gh layer the waiting-for-checks tests run against (Issue #2304).

The waiting line is an EDIT of the run's existing progress comment, so its
tests need three things from the boundary at once: the comment list, the
PATCH that writes the body, and the PR head read that names the commit.
The patch ratchet (Issue #789) keeps a test file from adding new
`monkeypatch.setattr` sites, and tests/fakes/ is the sanctioned home for
exactly that coupling.
"""
from __future__ import annotations

import json

from orbi import github
from seam import seam
from tests.fakes.progress import progress_comment_body


def _comments_payload(comments) -> str:
    return json.dumps(list(comments))


def bind_gh(monkeypatch, *, comments=(), head: str | None = None,
            patch_fails: bool = False) -> list[list[str]]:
    """Answer the comment routes and the head read; return every command.

    The list read answers `comments`, the PATCH writes the body (or raises
    when `patch_fails`), and the head read answers `headRefOid` — a view
    without one when `head` is None, which is what a broken PR read looks
    like.
    """
    calls: list[list[str]] = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[1] != "api":
            raise AssertionError(f"unexpected command: {command}")
        if "--method" not in command:
            return _comments_payload(comments)
        if patch_fails:
            raise RuntimeError("gh api PATCH failed")
        return ""

    def fake_read(command, **kwargs):
        calls.append(command)
        return json.dumps({} if head is None else {"headRefOid": head})

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(github, "run_gh_read_command", fake_read)
    return calls


def bind_unreachable_gh(monkeypatch) -> None:
    """Make every gh call fail — the bypass evidence of Issue #79."""
    def _raise(command, **kwargs):
        raise RuntimeError("gh down")

    monkeypatch.setattr(seam, "run_command", _raise)
    monkeypatch.setattr(github, "run_gh_read_command", _raise)


# The head commit the CI-pending fakes report (Issue #2304).
PENDING_HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def bind_pending_delivery(monkeypatch, *, run_id: str,
                          head: str = PENDING_HEAD) -> list[list[str]]:
    """Answer ONE CI-pending delivery step; return the gh api commands.

    The PR read reports an OPEN PR whose check is still running, the head
    read the commit that check belongs to, and the comment routes the
    run's existing progress comment. Any other command is a failure, so
    the fake also proves the deferral performs nothing but those reads and
    the one PATCH.
    """
    api_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[1] == "pr" and command[2] == "view":
            if command[-1] == "headRefOid":
                return json.dumps({"headRefOid": head})
            return json.dumps({
                "state": "OPEN",
                "statusCheckRollup": [
                    {"name": "tests", "status": "IN_PROGRESS",
                     "conclusion": None},
                ],
            })
        if command[1] == "api":
            api_calls.append(command)
            if "--method" not in command:
                return json.dumps([{
                    "id": 7, "body": progress_comment_body(run_id),
                }])
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run)
    return api_calls
