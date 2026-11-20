"""The fresh-claim race between two Orbi executors (Issue #2298).

One Issue can be served by two Orbi deployments under two GitHub ACCOUNTS
(an Orbi Cloud sandbox and a self-hosted engine are the observed pair), and
both used to claim it: each read the ai-in-progress label before the other
side had written it (self-hosted claimed at 04:31:45, Cloud at 04:31:52, and
Cloud saw no label). The loser then found foreign commits on the branch and
marked the winner's live delivery ai-blocked.

The race is now decided from the one record both sides write before running:
their own "Orbi started Pi:" comment. Both read the same list, so both reach
the same answer, and the later start yields. These tests pin the scan over
simulated comment lists and a simulated local account (the gh credential),
the two GitHub comment shapes the engine reads, and the step that logs the
yield and retires only this executor's own worktree.
"""
import pytest

import orbi.claim_race as claim_race
import orbi.github as github
from tests.fakes.github import FakeGh
from tests.seam import seam


REPO = "owner/repo"


def _started_body(run_id: str) -> str:
    return (
        f"<!-- orbi:run={run_id} -->\n"
        "<!-- orbi:comment=started -->\n"
        "**Orbi started Pi:** run details"
    )


def _kind_body(run_id: str, kind: str) -> str:
    return f"<!-- orbi:run={run_id} -->\n<!-- orbi:comment={kind} -->\ndetail"


@pytest.fixture
def github_state(monkeypatch):
    """One repository, both executor accounts, the local one in its login.

    The fake answers the real gh argv: the comment list
    (gh issue view --json comments), the per-author permission read and
    gh auth status (the local account). The local executor is swapped by
    assigning its login, so the same two accounts read the same list from
    opposite sides.
    """
    fake = FakeGh(REPO, login="selfhosted")
    monkeypatch.setattr(seam, "run_command", fake)
    fake.add_issue(5)
    fake.add_collaborator("selfhosted", "admin")
    fake.add_collaborator("cloud", "admin")
    return fake


def _start(fake, login: str, run_id: str) -> dict:
    fake.comment(5, _started_body(run_id), login=login)
    return fake.issues[5]["comments"][-1]


def _end(fake, login: str, run_id: str, kind: str = "merged") -> None:
    fake.comment(5, _kind_body(run_id, kind), login=login)


def _comments(fake) -> list[dict]:
    return github.issue_comments(5, repo=REPO)


def _scan(fake, *, rest: bool = False) -> dict | None:
    comments = _comments(fake)
    if rest:
        # The progress publisher's read is the REST list (complete
        # pagination), whose author field is user; the scan accepts both.
        comments = [
            {"user": comment["author"], "body": comment["body"]}
            for comment in comments
        ]
    return claim_race.other_executor_live_started(comments, repo=REPO)


# --- two executors, one Issue ----------------------------------------------


def test_two_executors_starting_the_same_issue_yield_the_later_one(
    github_state,
):
    """The acceptance scene: both started, the LATER one yields.

    Both sides read the same list and reach opposite answers - the first
    live start keeps running, the later one stops its round."""
    cloud = _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state)["id"] == cloud["id"]
    assert _scan(github_state)["author"]["login"] == "cloud"
    github_state.login = "cloud"
    assert _scan(github_state) is None


def test_the_publishers_rest_comment_shape_reads_the_same_way(github_state):
    """The delivery reads the REST list (user.login): the answer does not
    depend on which of GitHub's two comment shapes reaches the scan."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state, rest=True)["user"]["login"] == "cloud"
    github_state.login = "cloud"
    assert _scan(github_state, rest=True) is None


def test_a_bot_suffixed_own_login_is_not_another_executor(github_state):
    """GraphQL drops the App's [bot] suffix and REST keeps it: both name the
    local account, so neither is another executor."""
    github_state.comment(5, _started_body("aaaa1111"),
                         login="selfhosted[bot]")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None
    assert _scan(github_state, rest=True) is None


# --- the list order is the timeline ---------------------------------------


def test_same_second_starts_resolve_by_comment_list_order(github_state):
    """createdAt is second-resolution and identical here, so only the list
    order decides: the later-list start is the one that yields."""
    first = _start(github_state, "cloud", "aaaa1111")
    second = _start(github_state, "selfhosted", "bbbb2222")
    for comment in (first, second):
        comment["createdAt"] = "2026-11-20T04:31:52Z"
    github_state.login = "selfhosted"
    assert _scan(github_state)["id"] == first["id"]
    github_state.login = "cloud"
    assert _scan(github_state) is None


def test_created_at_never_overrides_the_list_order(github_state):
    """A timestamp that contradicts the list position changes nothing."""
    first = _start(github_state, "selfhosted", "aaaa1111")
    second = _start(github_state, "cloud", "bbbb2222")
    first["createdAt"] = "2026-11-20T04:31:52Z"
    second["createdAt"] = "2026-11-20T04:31:45Z"
    github_state.login = "selfhosted"
    assert _scan(github_state) is None
    github_state.login = "cloud"
    assert _scan(github_state)["author"]["login"] == "selfhosted"


# --- what is NOT another executor -----------------------------------------


def test_a_same_account_restart_is_not_another_executor(github_state):
    """One account starting twice (a restart, a new run id) is one
    executor: its newest started comment is its run, never a rival."""
    _start(github_state, "selfhosted", "aaaa1111")
    restart = _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None
    github_state.login = "cloud"
    assert _scan(github_state)["id"] == restart["id"]


def test_a_human_restored_scene_and_orbi_commands_are_not_runs(github_state):
    """A maintainer's restore note and an /orbi command steer the delivery;
    neither is a run of another executor."""
    github_state.comment(5, "restored the labels after the incident",
                         login="cloud")
    github_state.comment(5, "/orbi retry", login="cloud")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


def test_a_quoted_or_fenced_started_marker_is_not_a_run(github_state):
    """An external contributor quoting the marker in prose is not official
    metadata (the kind is read from the anchored prefix only), so a
    takeover never looks like a second run."""
    github_state.comment(
        5, "> <!-- orbi:comment=started -->\n"
           "the CI failure on this PR quoted the engine's own comment",
        login="contributor",
    )
    github_state.comment(
        5, "an example:\n\n" + _started_body("cccc3333"),
        login="contributor",
    )
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


def test_a_started_comment_from_an_untrusted_author_is_not_a_run(github_state):
    """A drive-by account cannot stall deliveries by copying the marker: the
    scan keeps the trusted-author rule every comment reader uses."""
    github_state.comment(5, _started_body("dddd4444"), login="stranger")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


# --- liveness, paired by account ------------------------------------------


def test_a_yielded_started_comment_never_blocks_a_later_claim(github_state):
    """Once the winner's run ends, the yielded start is not a live run
    either: a later claim by EITHER side proceeds."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    _end(github_state, "cloud", "aaaa1111")
    for account in ("cloud", "selfhosted"):
        github_state.login = account
        assert _scan(github_state) is None


def test_another_accounts_failure_does_not_end_a_live_run(github_state):
    """paseo-orbi#62: Cloud's ai-blocked failure comment landed while the
    self-hosted run was still reviewing. A terminal comment ends only its
    OWN account's run - failure comments do not always carry the run
    marker, so the account is the only reliable pair."""
    _start(github_state, "selfhosted", "3ebea5ab")
    _end(github_state, "cloud", "8f8f8f8f", kind="failure")
    github_state.login = "cloud"
    assert _scan(github_state)["author"]["login"] == "selfhosted"
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


def test_the_own_terminal_comment_ends_the_own_run(github_state):
    """The same pairing in the other direction: our own merged comment ends
    OUR run, and the other account's start stays live."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    _end(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    assert _scan(github_state)["author"]["login"] == "cloud"


def test_a_requeued_comment_also_ends_its_accounts_run(github_state):
    """A re-queued comment is a terminal record of that account's run."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    _end(github_state, "cloud", "aaaa1111", kind="requeued")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


# --- our own started comment ----------------------------------------------


def test_a_missing_own_started_comment_yields_to_a_live_other_executor(
    github_state,
):
    """The re-read can lag our own post (read-after-write). With no own
    comment in the list the order cannot be established, so the other
    executor's live start stands and this side yields."""
    _start(github_state, "cloud", "aaaa1111")
    github_state.login = "selfhosted"
    assert _scan(github_state)["author"]["login"] == "cloud"


def test_a_missing_own_started_comment_without_another_executor_runs(
    github_state,
):
    """A failed started publish is a bypass failure: a solo delivery is
    never stopped by it - there is no live executor to yield to."""
    github_state.comment(5, "a maintainer's note", login="cloud")
    github_state.login = "selfhosted"
    assert _scan(github_state) is None


# --- the delivery step ----------------------------------------------------


def test_yield_to_another_executor_logs_the_reason_and_retires(
    github_state, caplog,
):
    """The step the fresh claim runs: it logs claim_yield with the reason,
    retires only this executor's own worktree and returns True."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "selfhosted"
    retired = []
    with caplog.at_level("INFO"):
        yielded = claim_race.yield_to_another_executor(
            issue=5, source_repo=REPO, comments=_comments(github_state),
            retire=lambda: retired.append("retired"),
        )
    assert yielded is True
    assert retired == ["retired"]
    assert "claim_yield" in caplog.text
    assert "reason=other_executor_started" in caplog.text


def test_yield_to_another_executor_runs_with_no_other_executor(
    github_state, caplog,
):
    """The winner's side: nothing to yield to, so nothing is retired and no
    claim_yield line is written."""
    _start(github_state, "cloud", "aaaa1111")
    _start(github_state, "selfhosted", "bbbb2222")
    github_state.login = "cloud"
    retired = []
    with caplog.at_level("INFO"):
        yielded = claim_race.yield_to_another_executor(
            issue=5, source_repo=REPO, comments=_comments(github_state),
            retire=lambda: retired.append("retired"),
        )
    assert yielded is False
    assert retired == []
    assert "claim_yield" not in caplog.text
