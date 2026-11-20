"""The claim tick's reads and sweeps behind the Issue #2305 tests.

The scenario is one opened-PR delivery that passed review and only waits
for its checks. Everything AROUND the claim's own decision — the per-tick
sweeps, the comment-trust read and the PR read — is bound here so the test
module stays about the behaviour, and the patch ratchet's internal-patch
count stays flat (tests/fakes/ is the adapter contract, not coupling).
"""
from __future__ import annotations

from orbi import claim, review_wait
from seam import seam


def bind_claim_tick(monkeypatch) -> None:
    """Bind one idle tick: no sweep, no trust read, no GitHub write.

    The sweeps are the same module globals `claim.pick_next_delivery`
    reads (the #856/#186/#1918/#1825/#2221 bypasses); replacing them keeps
    the test's fake `gh` from seeing a command it does not model. The
    permission read is the comment-trust rule's (#1806): these scenarios
    name a maintainer.
    """
    monkeypatch.setattr(
        seam, "repository_permission", lambda login, *, repo: "admin",
    )
    monkeypatch.setitem(claim.__dict__, "reconcile_open_epics", lambda *a, **k: [])
    monkeypatch.setitem(
        claim.milestone_bookkeeping.__dict__, "milestone_reconcile_due",
        lambda *a, **k: False,
    )
    monkeypatch.setitem(claim.__dict__, "reconcile_orphan_prs", lambda *a, **k: [])
    monkeypatch.setitem(claim.__dict__, "check_permission", _Silent("recover"))
    monkeypatch.setitem(claim.__dict__, "base_ci_fix", _Silent("recover"))
    monkeypatch.setitem(claim.__dict__, "stopped_scans", _Silent("run"))
    monkeypatch.setitem(
        claim.__dict__, "pick_awaiting_merge_delivery", lambda *a, **k: None,
    )
    monkeypatch.setitem(claim.__dict__, "pick_in_progress_issue", lambda *a, **k: None)


class _Silent:
    """A module-like stand-in whose one entry point does nothing."""

    def __init__(self, name: str):
        setattr(self, name, lambda *a, **k: None)


def bind_gh(monkeypatch, fake) -> None:
    """Serve one fake `gh` behind the subprocess seam.

    The boundary patch lives here so the scenarios below add no patch site
    of their own (Issue #1856: the frozen baseline equals the real corpus
    exactly, and tests/fakes/ is the adapter contract).
    """
    monkeypatch.setattr(seam, "run_command", fake)


def bind_pr_read_failure(monkeypatch) -> None:
    """The ONE delivery read fails: the candidate must still be returned."""

    def boom(number, fields, *, repo=None, cwd=None, timeout=None):
        raise RuntimeError("gh down")

    monkeypatch.setattr(review_wait, "pr_view", boom)


def bind_pr_read(monkeypatch, payload: dict) -> list:
    """Bind the ONE delivery read and return the reads it received."""
    reads: list = []

    def view(number, fields, *, repo=None, cwd=None, timeout=None):
        reads.append((number, fields, repo))
        return dict(payload)

    monkeypatch.setattr(review_wait, "pr_view", view)
    return reads
