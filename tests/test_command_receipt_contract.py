
"""The command receipt markers are a public contract (Issue #1926).

A receipt identifies the command comment it answers through the existing
comment key and its outcome through the marker prefix; the translated prose
beside it is display only. These tests drive the real dispatcher and the
real GitHub adapter over the in-memory gh fake, so the contract is proved
by engine output instead of a hand-written fixture. No runtime change is
made by the ticket: the test pins what already ships.
"""
from __future__ import annotations

import contextlib
import re

import pytest

from orbi import journal, ticket_command
from orbi.github import issue_comments
from seam import seam

from tests.fakes.github import FakeGh
from tests.test_base_ci_red import (
    BASE_SHA,
    REPO as BASE_REPO,
    _blocks_issue,
    _command_fixture,
    _run_command,
    base_run,
)

REPO = "owner/repo"
ISSUE_NUMBER = 436
MARKER_RE = re.compile(r"<!-- orbi-probe-command[^>]*-->")


@contextlib.contextmanager
def _registered(spec):
    """Register the probe in the REAL registry and undo it afterwards.

    Registration is what wires the journal events a receipt-emitting path
    needs, so the test uses it instead of patching the registry.
    """
    ticket_command.register_command(spec)
    try:
        yield spec
    finally:
        ticket_command.TICKET_COMMANDS.pop(spec.name, None)
        for suffix in ("applied", "failed"):
            journal.JOURNAL_EVENTS.pop(f"{spec.name}_command_{suffix}", None)


def _probe_spec() -> ticket_command.CommandSpec:
    """A fictional command whose failure step the receipt must name."""

    def _validate(argument, **_context):
        return None

    def _apply(argument, *, command_comment, **_context):
        # The one winning occurrence is the failing one: its receipt has to
        # name the step, so the handler always raises here.
        raise ticket_command.TicketCommandError("probe step", "boom")

    return ticket_command.CommandSpec(
        name="probe",
        subcommand="probe",
        arity=1,
        permission="write",
        validate=_validate,
        apply=_apply,
        usage="/orbi probe <target>",
        description="receipt probe for the contract test",
    )


def _probe_run(monkeypatch, language_code: str) -> dict:
    """Dispatch one rejection and one failure; return what they left.

    The single subprocess seam this file patches: the in-memory gh fake is
    the GitHub boundary (Issue #789), so the real dispatcher and the real
    adapter run.
    """
    gh = FakeGh(REPO)
    gh.add_issue(ISSUE_NUMBER, title="t")
    gh.comment(ISSUE_NUMBER, "/orbi probe alpha", login="stranger")
    gh.comment(ISSUE_NUMBER, "/orbi probe fail", login="maintainer")
    gh.add_collaborator("stranger", "read")
    gh.add_collaborator("maintainer", "write")
    monkeypatch.setattr(seam, "run_command", gh)
    spec = _probe_spec()
    with _registered(spec):
        ticket_command.process_commands(
            REPO, ISSUE_NUMBER, commands=[spec],
            language_code=language_code,
        )
    comments = issue_comments(ISSUE_NUMBER, repo=REPO)
    return {
        "receipts": [
            comment["body"] for comment in comments
            if "orbi-probe-command" in comment["body"]
        ],
        "keys": [
            ticket_command.command_comment_key(comment)
            for comment in comments
            if comment["body"].startswith("/orbi probe")
        ],
    }


def test_a_rejection_and_a_failure_receipt_name_their_command_comment(
    monkeypatch,
):
    """The receipt carries comment=<key> of the comment it answers, and the
    outcome lives in the marker prefix (rejected) or the step field."""
    state = _probe_run(monkeypatch, "en")
    rejected, failed = state["receipts"]
    rejected_key, failed_key = state["keys"]
    assert (
        f"<!-- orbi-probe-command-rejected comment={rejected_key} -->"
        in rejected
    )
    assert (
        f"<!-- orbi-probe-command-failed step=probe step "
        f"comment={failed_key} -->"
    ) in failed
    # The prose never decides the outcome: the same engine output is read
    # back from the marker alone.
    assert MARKER_RE.findall(rejected) == [
        f"<!-- orbi-probe-command-rejected comment={rejected_key} -->",
    ]
    assert MARKER_RE.findall(failed) == [
        f"<!-- orbi-probe-command-failed step=probe step "
        f"comment={failed_key} -->",
    ]


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_the_receipt_marker_is_language_independent(
    monkeypatch, language_code,
):
    """The prose follows the ticket language; the marker does not, so a
    consumer never parses a translated sentence."""
    state = _probe_run(monkeypatch, language_code)
    markers = [MARKER_RE.findall(body) for body in state["receipts"]]
    assert all(marker for marker in markers)
    english = _probe_run(monkeypatch, "en")
    english_markers = [MARKER_RE.findall(body) for body in english["receipts"]]
    assert markers == english_markers
    if language_code == "zh":
        assert state["receipts"] != english["receipts"]


def test_the_fix_base_success_receipt_names_its_command_comment(monkeypatch):
    """The existing success receipt uses the same key, so a consumer finds
    the command comment behind every outcome the same way."""
    gh = _command_fixture(monkeypatch)
    _blocks_issue(gh)
    gh.add_check_runs(BASE_SHA, [base_run()])
    _run_command(gh)
    comments = issue_comments(1825, repo=BASE_REPO)
    command = next(
        comment for comment in comments if "/orbi fix-base" in comment["body"]
    )
    receipt = next(
        comment for comment in comments
        if "orbi-fix_base-command" in comment["body"]
    )
    assert (
        "<!-- orbi-fix_base-command "
        f"comment={ticket_command.command_comment_key(command)} -->"
    ) in receipt["body"]
    assert ticket_command.command_receipt_marker(
        ticket_command.TICKET_COMMANDS["fix_base"]
    ) == "orbi-fix_base-command"
