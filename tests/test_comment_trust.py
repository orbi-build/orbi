"""Comment trust from the author's repository permission (Issue #1806).

The engine used to decide "is this a maintainer" from the comment's
``authorAssociation``, a field the reading token sees: for a private org
member the delivery's App token reads ``CONTRIBUTOR`` even when the person
is an org admin (orbi-build/orbi-cloud#2715), and ``MEMBER``/
``COLLABORATOR`` do not imply write access at all. The decision is now
the permission GitHub reports for the author
(``GET /repos/{repo}/collaborators/{login}/permission``, verified against
the real API), and this file pins both users of it: the in-ticket command
gate and the comment trust (task-context block, recovery scene, markers).

A failed read is a GitHub read failure — it propagates, it is never
turned into "no permission" and it never silently drops a comment.
"""
import json
import subprocess
from pathlib import Path

import pytest

import orbi.github as github
import orbi.milestone_command as milestone_command
import orbi.ticket_command as ticket_command
from orbi import comment_trust, gh_response
from orbi.delivery_labels import READY_LABEL
from tests.fakes.github import FakeGh
from tests.seam import seam

REPO = "owner/repo"


def _comment(body, *, login, association="CONTRIBUTOR", cid=1):
    return {
        "id": cid,
        "author": {"login": login},
        "authorAssociation": association,
        "body": body,
    }


@pytest.fixture
def fake_gh(monkeypatch):
    """The in-memory GitHub at the one subprocess seam."""
    fake = FakeGh(REPO)
    monkeypatch.setattr(seam, "run_command", fake)
    return fake


def _command_context():
    """The in-ticket dispatcher's context for the milestone command."""
    return {
        "candidate_titles": ["v0.5.41"],
        "config_path": Path("/nope"),
        "policy": None,
        "policy_path": ".github/orbi.toml",
        "base_branch": "main",
        "dispatch_label": READY_LABEL,
        "version_file": None,
    }


def _solve_permission_retry(monkeypatch):
    """A 5xx is retried; the retry backoff is not this test's subject."""
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)


# --- the read --------------------------------------------------------------


def test_repository_permission_reads_the_documented_endpoint(fake_gh):
    """The one read: `collaborators/{login}/permission` (verified against
    the real API on both repositories named in the ticket)."""
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.add_collaborator("reader", "read")
    assert github.repository_permission("xqliu", repo=REPO) == "admin"
    assert github.repository_permission("reader", repo=REPO) == "read"


def test_repository_permission_reads_a_404_as_no_access(fake_gh):
    """GitHub answers 404 for a login this token cannot see: no access."""
    assert github.repository_permission("ghost", repo=REPO) == "none"


def test_repository_permission_propagates_every_other_failed_read(
        monkeypatch):
    """A 5xx is a GitHub read failure, never "no permission"."""
    _solve_permission_retry(monkeypatch)

    def failed(status):
        def read(command, **kwargs):
            raise gh_response.GitHubRequestFailed(
                1, command,
                output=(
                    f"HTTP/2.0 {status} Error\r\n"
                    "Content-Type: application/json\r\n\r\n"
                    '{"message": "failed"}'
                ),
                stderr="gh: failed",
                status=status,
            )
        return read

    monkeypatch.setattr(seam, "run_gh_read_command", failed(500))
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        github.repository_permission("xqliu", repo=REPO)
    assert gh_response.status_of(excinfo.value) == 500


@pytest.mark.parametrize("payload", ['[]', '{"permission": 7}', "{}"])
def test_repository_permission_rejects_a_malformed_response(
        monkeypatch, payload,
):
    monkeypatch.setattr(
        seam, "run_gh_read_command", lambda command, **kwargs: payload,
    )
    with pytest.raises(ValueError, match="malformed"):
        github.repository_permission("xqliu", repo=REPO)


# --- acceptance 2: the trusted-comments block ------------------------------


def test_a_private_org_member_comment_enters_the_trusted_block(fake_gh):
    """Acceptance 2: the App token reads CONTRIBUTOR for `xqliu`, the
    permission endpoint reads admin — the comment is trusted."""
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.comment(5, "the decision", login="xqliu",
                    association="CONTRIBUTOR")
    fake_gh.comment(5, "drive-by noise", login="stranger",
                    association="NONE")
    block = comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo=REPO), limit=20, repo=REPO,
    )
    assert "the decision" in block and "xqliu" in block
    assert "drive-by noise" not in block


def test_a_member_without_write_access_is_not_trusted(fake_gh):
    """Acceptance 3: ``MEMBER`` is not write access."""
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("reader", "read")
    fake_gh.comment(5, "a reader's note", login="reader",
                    association="MEMBER")
    assert comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo=REPO), limit=20, repo=REPO,
    ) == "(no trusted comments)"


def test_a_collaborator_with_write_access_is_trusted(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("writer", "write")
    fake_gh.comment(5, "a writer's note", login="writer",
                    association="COLLABORATOR")
    block = comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo=REPO), limit=20, repo=REPO,
    )
    assert "a writer's note" in block


def test_an_unknown_login_shares_the_404_no_access_answer(fake_gh):
    """Acceptance 3, the 404 leg: no access, and the comment is skipped."""
    fake_gh.add_issue(5)
    fake_gh.comment(5, "a ghost's note", login="ghost", association="MEMBER")
    assert comment_trust.trusted_issue_comments_block(
        github.issue_comments(5, repo=REPO), limit=20, repo=REPO,
    ) == "(no trusted comments)"


# --- acceptance 1 and 3: the in-ticket command gate ------------------------


def test_a_private_org_members_command_is_applied(fake_gh, monkeypatch):
    """Acceptance 1: CONTRIBUTOR association, `admin` permission, the
    command runs and no rejection receipt is posted."""
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.comment(5, "/milestone v0.5.41", login="xqliu",
                    association="CONTRIBUTOR")
    applied = []
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda repo, version, **kwargs: applied.append((repo, version)),
    )
    ticket_command.process_commands(
        REPO, 5, commands=[milestone_command.MILESTONE_COMMAND],
        **_command_context(),
    )
    assert applied == [(REPO, "v0.5.41")]
    # No rejection receipt reached the ticket.
    assert [comment["body"] for comment in fake_gh.issues[5]["comments"]] == [
        "/milestone v0.5.41",
    ]


def test_a_members_command_is_rejected_with_the_no_permission_receipt(
        fake_gh, monkeypatch,
):
    """Acceptance 3: a ``MEMBER``/``COLLABORATOR`` author with only read
    access is rejected, with the receipt naming the permission it read."""
    fake_gh.add_issue(5)
    fake_gh.add_collaborator("reader", "read")
    fake_gh.comment(5, "/milestone v0.5.41", login="reader",
                    association="COLLABORATOR")
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: pytest.fail("nothing to apply"),
    )
    ticket_command.process_commands(
        REPO, 5, commands=[milestone_command.MILESTONE_COMMAND],
        **_command_context(),
    )
    posted = [
        comment["body"] for comment in fake_gh.issues[5]["comments"]
        if "orbi-milestone-command-rejected" in comment["body"]
    ]
    assert len(posted) == 1
    assert "no write permission" in posted[0]
    assert "repository permission='read'" in posted[0]
    assert "authorAssociation" not in posted[0]


def test_an_unknown_logins_command_is_rejected(fake_gh, monkeypatch):
    """Acceptance 3, the 404 leg: no access, so the command is rejected."""
    fake_gh.add_issue(5)
    fake_gh.comment(5, "/milestone v0.5.41", login="ghost",
                    association="MEMBER")
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: pytest.fail("nothing to apply"),
    )
    ticket_command.process_commands(
        REPO, 5, commands=[milestone_command.MILESTONE_COMMAND],
        **_command_context(),
    )
    posted = [
        comment["body"] for comment in fake_gh.issues[5]["comments"]
        if "orbi-milestone-command-rejected" in comment["body"]
    ]
    assert len(posted) == 1
    assert "repository permission='none'" in posted[0]


# --- acceptance 4: a failed read is a failure, not a decision --------------


def test_a_failed_read_posts_no_receipt_and_drops_no_comment(
        fake_gh, monkeypatch,
):
    """Acceptance 4: a 5xx on the permission endpoint fails the read —
    no "not applied" receipt, no receipt-less silence, the error
    surfaces."""
    _solve_permission_retry(monkeypatch)
    fake_gh.add_issue(5)
    fake_gh.comment(5, "/milestone v0.5.41", login="xqliu",
                    association="CONTRIBUTOR")
    fake_gh.set_permission_failure("xqliu")
    posted, applied = [], []
    monkeypatch.setattr(
        seam, "run_gh_write_command",
        lambda command, **kwargs: posted.append(command) or "",
    )
    monkeypatch.setattr(
        seam, "apply_milestone_command",
        lambda *args, **kwargs: applied.append(args),
    )
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        ticket_command.process_commands(
            REPO, 5, commands=[milestone_command.MILESTONE_COMMAND],
            **_command_context(),
        )
    assert gh_response.status_of(excinfo.value) == 500
    assert posted == []
    assert applied == []


def test_a_failed_read_never_renders_an_empty_trusted_block(
        fake_gh, monkeypatch,
):
    """Acceptance 4 on the block: "(no trusted comments)" would be a silent
    drop; the read failure propagates instead."""
    _solve_permission_retry(monkeypatch)
    fake_gh.add_issue(5)
    fake_gh.comment(5, "the decision", login="xqliu",
                    association="CONTRIBUTOR")
    fake_gh.set_permission_failure("xqliu")
    with pytest.raises(subprocess.CalledProcessError):
        comment_trust.trusted_issue_comments_block(
            github.issue_comments(5, repo=REPO), limit=20, repo=REPO,
        )


# --- one read per author, not per comment ---------------------------------


def test_a_scan_reads_each_author_once(fake_gh, monkeypatch):
    """A long Issue history costs one permission read per author."""
    fake_gh.add_collaborator("xqliu", "admin")
    fake_gh.add_collaborator("reader", "read")
    calls = []
    original = github.repository_permission

    def counting(login, *, repo):
        calls.append(login)
        return original(login, repo=repo)

    monkeypatch.setattr(seam, "repository_permission", counting)
    comments = [
        _comment("one", login="xqliu", cid=1),
        _comment("two", login="reader", association="MEMBER", cid=2),
        _comment("three", login="xqliu", cid=3),
        _comment("four", login="xqliu", cid=4),
        _comment("five", login="reader", association="MEMBER", cid=5),
    ]
    trusted = comment_trust.trusted_comments(comments, repo=REPO)
    assert [comment["id"] for comment in trusted] == [1, 3, 4]
    assert calls == ["xqliu", "reader"]
