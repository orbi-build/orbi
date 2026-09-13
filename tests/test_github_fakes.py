"""Fake-based tests for the pickup scan and the GitHub adapter surface
(Issue #789).

These tests NEVER patch a runner-internal name — zero
``setattr(runner, ...)``. They arrange state in the
``FakeGh`` in-memory GitHub, patch only the ONE subprocess seam
(``seam.run_command``, Article 3.4), run the real public entry points
(the claim scans through ``runner.pick_issue``; the label/comment/PR/
check-run reads and writes through ``orbi.github``), and assert the
public surface: which Issue the scan picks, the labels an edit leaves,
the comment order, and the returned PR/check data.

The list order mirrors ``gh issue list``: newest first, so tests seed
issues oldest-first.
"""
import logging
import subprocess

import pytest

import orbi.github as github
import orbi.runner as runner
from orbi.delivery_labels import (
    EPIC_LABEL,
    IN_PROGRESS_LABEL,
    MERGED_LABEL,
    P0_LABEL,
    READY_LABEL,
    RELEASE_LABEL,
)
from orbi.progress import format_status_comment
from seam import seam

from tests.fakes.github import FakeGh


@pytest.fixture
def fake_gh(monkeypatch):
    """The FakeGh wired into the ONE subprocess seam — the only patch
    a fake-based test needs."""
    fake = FakeGh("owner/repo")
    monkeypatch.setattr(seam, "run_command", fake)
    return fake


# --- the claim scans (public entry: runner.pick_issue) ----------------------


def test_pickup_claims_the_p0_issue_first(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, P0_LABEL))
    picked = runner.pick_issue("owner/repo")
    assert picked["number"] == 2
    assert github.issue_priority(picked) == "p0"


def test_pickup_prefers_a_bug_over_plain_ready(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, "bug"))
    assert runner.pick_issue("owner/repo")["number"] == 2


def test_pickup_claims_the_p0_scan_before_the_bug_scan(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL, "bug"))
    fake_gh.add_issue(2, labels=(READY_LABEL, P0_LABEL))
    assert runner.pick_issue("owner/repo")["number"] == 2


def test_pickup_skips_a_blocked_issue_and_claims_the_next(fake_gh, caplog):
    # gh lists newest first: #2 seeds first so #1 heads the scan.
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    fake_gh.add_issue(3)
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_blocker(1, 3, state="OPEN")
    with caplog.at_level(logging.INFO):
        picked = runner.pick_issue("owner/repo")
    assert picked["number"] == 2
    # A skip never touches labels: the blocked Issue stays claimable.
    assert github.issue_labels(1, "owner/repo") == [READY_LABEL]
    assert "blocked_by issue=1" in caplog.text


def test_pickup_claims_an_issue_whose_blocker_is_closed(fake_gh):
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    fake_gh.add_issue(3, state="closed")
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_blocker(1, 3, state="CLOSED")
    assert runner.pick_issue("owner/repo")["number"] == 1


def test_pickup_skips_an_epic(fake_gh, caplog):
    fake_gh.add_issue(2, labels=(READY_LABEL,))
    fake_gh.add_issue(1, labels=(READY_LABEL, EPIC_LABEL))
    with caplog.at_level(logging.INFO):
        picked = runner.pick_issue("owner/repo")
    assert picked["number"] == 2
    assert "epic_not_claimed issue=1" in caplog.text


def test_pickup_excludes_terminal_and_in_flight_states(fake_gh):
    fake_gh.add_issue(3, labels=(READY_LABEL,))
    fake_gh.add_issue(1, labels=(READY_LABEL, MERGED_LABEL))
    fake_gh.add_issue(2, labels=(READY_LABEL, IN_PROGRESS_LABEL))
    assert runner.pick_issue("owner/repo")["number"] == 3


def test_pickup_scopes_the_scan_to_the_active_milestone(fake_gh):
    fake_gh.add_issue(7, title="v2 issue", labels=(READY_LABEL,),
                      milestone=2)
    fake_gh.add_issue(9, title="milestoneless", labels=(READY_LABEL,))
    fake_gh.add_issue(8, title="v1 issue", labels=(READY_LABEL,),
                      milestone=1)
    fake_gh.add_milestone(1, title="v1")
    fake_gh.add_milestone(2, title="v2")
    picked = runner.pick_issue("owner/repo", active_milestone="v1")
    assert picked["number"] == 8


def test_pickup_fails_open_when_the_scan_query_fails(
    fake_gh, monkeypatch, caplog,
):
    fake_gh.add_issue(1, labels=(READY_LABEL,))

    def down(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output="", stderr="gh: not found (fatal)"
        )

    monkeypatch.setattr(seam, "run_command", down)
    with caplog.at_level(logging.ERROR):
        assert runner.pick_issue("owner/repo") is None
    assert "blocked_by_check_failed" in caplog.text


# --- the release fallback scan (claimed only when nothing else is) ----------


def test_pickup_claims_the_release_from_the_fallback_scan(fake_gh, caplog):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)
    with caplog.at_level(logging.INFO):
        picked = runner.pick_issue("owner/repo")
    assert picked["number"] == 1
    # The ordinary scans logged the release skip before the fallback ran.
    assert "release_not_claimed issue=1" in caplog.text


def test_pickup_skips_the_release_while_the_milestone_has_open_work(
    fake_gh, caplog,
):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=2)
    fake_gh.add_issue(2, milestone=1)  # the other open Milestone Issue
    with caplog.at_level(logging.INFO):
        assert runner.pick_issue("owner/repo") is None
    assert "release_milestone_incomplete issue=1" in caplog.text


def test_pickup_release_with_a_failed_milestone_check_fails_safe(
    fake_gh, monkeypatch, caplog,
):
    # The milestone-counter read fails (an API outage scene): a bad
    # release must never ship on a failed check, so the scan returns
    # nothing this tick.
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)

    def api_outage(command, **kwargs):
        if command[1:2] == ["api"] and "milestones" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, output="", stderr="gh: API error (fatal)"
            )
        return fake_gh(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", api_outage)
    with caplog.at_level(logging.ERROR):
        assert runner.pick_issue("owner/repo") is None
    assert "release_milestone_check_failed issue=1" in caplog.text


def test_pickup_release_scoped_to_the_active_milestone(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL, RELEASE_LABEL), milestone=1)
    fake_gh.add_milestone(1, title="v1.0", open_issues=1)
    fake_gh.add_milestone(2, title="v2.0", open_issues=1)
    # The scan scoped to v2.0 must not see the v1.0 release Issue.
    assert runner.pick_issue("owner/repo", active_milestone="v2.0") is None
    picked = runner.pick_issue("owner/repo", active_milestone="v1.0")
    assert picked["number"] == 1


# --- the label lifecycle (public entries: apply_label_patch etc.) -----------


def test_claim_label_patch_is_idempotent_on_the_fake(fake_gh):
    fake_gh.add_issue(5, labels=(READY_LABEL,))
    current = github.issue_labels(5, "owner/repo")
    github.apply_label_patch(5, repo="owner/repo", event="claim",
                             current_labels=current)
    labels = github.issue_labels(5, "owner/repo")
    assert labels == [READY_LABEL, IN_PROGRESS_LABEL]
    assert github.has_in_progress_label(5, "owner/repo") is True
    # Idempotent: re-applying the same event changes nothing.
    github.apply_label_patch(5, repo="owner/repo", event="claim",
                             current_labels=labels)
    assert github.issue_labels(5, "owner/repo") == labels
    github.edit_issue(5, repo="owner/repo",
                      remove=IN_PROGRESS_LABEL)
    github.edit_issue(5, repo="owner/repo",
                      remove=IN_PROGRESS_LABEL)
    assert github.issue_labels(5, "owner/repo") == [READY_LABEL]


def test_comments_append_in_order_and_read_back_oldest_first(fake_gh):
    fake_gh.add_issue(5)
    github.comment_issue(5, repo="owner/repo", body="first runner note")
    fake_gh.comment(5, "human decision", login="xqliu",
                    association="MEMBER")
    github.comment_issue(5, repo="owner/repo", body="second runner note")
    comments = github.issue_comments(5, repo="owner/repo")
    bodies = [comment["body"] for comment in comments]
    assert bodies == [
        format_status_comment("first runner note"),
        "human decision",
        format_status_comment("second runner note"),
    ]
    assert [comment["createdAt"] for comment in comments] == sorted(
        comment["createdAt"] for comment in comments
    )


def test_trusted_block_keeps_maintainer_and_bot_comments(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.comment(5, "drive-by noise", login="stranger",
                    association="NONE")
    fake_gh.comment(5, "the decision", login="xqliu", association="MEMBER")
    github.comment_issue(5, repo="owner/repo", body="runner note")
    block = github.trusted_issue_comments_block(
        github.issue_comments(5, repo="owner/repo"), limit=20,
    )
    assert "xqliu" in block and "the decision" in block
    # The runner's own comment is trusted by its authenticated login
    # (resolved through `gh auth status` on the fake).
    assert "orbi-bot" in block and "runner note" in block
    assert "stranger" not in block and "drive-by noise" not in block


def test_trusted_block_drops_the_oldest_over_the_limit(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.comment(5, "old decision", login="xqliu",
                    association="MEMBER")
    fake_gh.comment(5, "new decision", login="xqliu",
                    association="MEMBER")
    block = github.trusted_issue_comments_block(
        github.issue_comments(5, repo="owner/repo"), limit=1,
    )
    assert "new decision" in block
    assert "old decision" not in block
    assert "1 older trusted comment omitted" in block


# --- the PR and CI surfaces ---------------------------------------------------


def test_open_pr_for_branch_and_delivery_status(fake_gh, tmp_path):
    branch = "orbi/owner-repo-issue-5"
    fake_gh.add_pr(
        11, head=branch,
        checks=({"name": "CI", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},),
    )
    pr = github.open_pr_for_branch(tmp_path, branch)
    assert pr["number"] == 11
    assert github.pr_state(pr["url"], "owner/repo") == "OPEN"
    state, summaries = github.pr_delivery_status(pr["url"], "owner/repo")
    assert (state, summaries) == ("OPEN", ["CI=COMPLETED/SUCCESS"])
    fake_gh.add_pr(12, head=branch)
    with pytest.raises(RuntimeError, match="multiple open PRs"):
        github.open_pr_for_branch(tmp_path, branch)


def test_open_pr_for_branch_returns_none_without_a_pr(fake_gh, tmp_path):
    assert github.open_pr_for_branch(tmp_path, "orbi/owner-repo-issue-5") \
        is None


def test_commit_check_runs_reads_the_ci_evidence(fake_gh):
    runs = [{"name": "CI", "conclusion": "SUCCESS"}]
    fake_gh.add_check_runs("abc123", runs)
    assert github.commit_check_runs("owner/repo", "abc123") == runs
    with pytest.raises(subprocess.CalledProcessError):
        github.commit_check_runs("owner/repo", "missing")


def test_milestone_counter_fails_on_an_unknown_title(fake_gh):
    # GitHub's own counter for a Milestone that does not exist yields
    # empty output: the adapter must raise (a failed check), never
    # return a silent 0 the release gate could ship on.
    fake_gh.add_milestone(1, title="v1.0")
    fake_gh.add_milestone(2, title="v2.0")
    with pytest.raises(RuntimeError, match="not found in owner/repo"):
        github.milestone_open_issue_count("owner/repo", "v9.9")


def test_close_issue_marks_the_terminal_state(fake_gh):
    fake_gh.add_issue(5, labels=(READY_LABEL,))
    github.close_issue(5, repo="owner/repo")
    assert github.issue_view(5, "state")["state"] == "CLOSED"
    assert github.issue_view(5, "url")["url"] == (
        "https://github.com/owner/repo/issues/5"
    )


def test_list_issues_label_and_state_filters(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    fake_gh.add_issue(2, labels=(READY_LABEL, "bug"), state="closed")
    fake_gh.add_issue(3, labels=("bug",))
    # gh lists newest first; #2 is closed and must never match open.
    assert github.list_issues(
        "owner/repo", state="open", label="bug",
        json_fields="number", limit=200,
    ) == [{"number": 3}]
    # A milestoneless issue renders the milestone field as null (the
    # shape the release scope derivation reads).
    assert github.list_issues(
        "owner/repo", state="open", search=f"label:bug",
        json_fields="number,milestone", limit=200,
    ) == [{"number": 3, "milestone": None}]


# --- the failure paths (fail fast, never a silent pass) ----------------------


def assert_fails_with(command_callable, stderr_fragment: str) -> None:
    """Assert the fake failed like a real CLI: CalledProcessError whose
    stderr carries the reason (never a silent empty success)."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        command_callable()
    assert stderr_fragment in excinfo.value.stderr


def test_fake_fails_fast_on_unsupported_commands(fake_gh):
    assert_fails_with(
        lambda: fake_gh(["bogus", "argv"]), "unsupported command: bogus"
    )
    assert_fails_with(lambda: fake_gh(["gh"]), "unsupported command: gh")
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/milestones", "bare-token"]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "api", "repos/owner/repo/unknown-endpoint"]),
        "unsupported command: gh api repos/owner/repo/unknown-endpoint",
    )
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/milestones", "--jq", "."]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "api", "repos/owner/repo/commits/abc123/check-runs",
             "--jq", ".wrong"]
        ),
        "unsupported command",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "release", "view", "v1"]),
        "unsupported command: gh release view v1",
    )
    assert_fails_with(
        lambda: fake_gh(["gh", "issue"]), "unsupported command: gh issue"
    )


def test_fake_fails_fast_on_unsupported_issue_verbs(fake_gh):
    fake_gh.add_issue(5)
    assert_fails_with(
        lambda: fake_gh(
            ["gh", "issue", "lock", "5", "--repo", "owner/repo"]
        ),
        "unsupported command: gh issue lock",
    )


def test_fake_fails_fast_on_repository_mismatch(fake_gh):
    fake_gh.add_issue(5)
    fake_gh.add_check_runs("abc123", [])
    assert_fails_with(
        lambda: github.comment_issue(5, repo="other/repo", body="x"),
        "repository mismatch",
    )
    assert_fails_with(
        lambda: github.commit_check_runs("other/repo", "abc123"),
        "repository mismatch",
    )


def test_fake_fails_fast_on_an_unknown_issue(fake_gh):
    assert_fails_with(
        lambda: github.issue_labels(999, "owner/repo"),
        "Could not resolve to an Issue",
    )
    assert_fails_with(
        lambda: github.pr_state(
            "https://github.com/owner/repo/pull/999", "owner/repo"
        ),
        "Could not resolve to a pull request",
    )


def test_fake_fails_fast_on_unsupported_field_requests(fake_gh):
    fake_gh.add_issue(5)
    assert_fails_with(
        lambda: github.issue_view(5, "author"),
        "unsupported issue field: 'author'",
    )
    fake_gh.add_pr(6, head="b")
    assert_fails_with(
        lambda: github.pr_view(6, "mergeable"),
        "unsupported pr field: 'mergeable'",
    )


def test_fake_fails_fast_on_unimplemented_list_flags(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    # `--milestone` is a real gh filter the fake does not implement:
    # silently ignoring it would match issues the real scan excludes.
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open", milestone="v1",
            json_fields="number", limit=200,
        ),
        "unsupported command: gh issue list --milestone",
    )


def test_fake_fails_fast_on_bad_search_qualifiers(fake_gh):
    fake_gh.add_issue(1, labels=(READY_LABEL,))
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open", search="label:ai-ready in:title",
            json_fields="number", limit=200,
        ),
        "unsupported search qualifier",
    )
    assert_fails_with(
        lambda: github.list_issues(
            "owner/repo", state="open",
            search='milestone:"a" milestone:"b"',
            json_fields="number", limit=200,
        ),
        "two milestone qualifiers",
    )
