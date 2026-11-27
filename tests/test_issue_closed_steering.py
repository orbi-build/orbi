"""Issue #2468: closing the Issue during a delivery stops the session.

The closed state is read by the SAME poll that already reads the Issue
during an active session (`check_steering`): a CLOSED Issue ends the
session where it is instead of running to the end. The delivery's closeout
then pushes what the session already committed, posts the existing
closed-mid-delivery comment once, opens no PR and ends the run with
`result=issue_closed`.
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

import orbi.pi_process as pi_process
import orbi.pi_session as pi_session
import orbi.runner as runner
from conftest import git
from orbi import config as config_domain
from orbi import journal
from orbi.delivery_scene import RunContext
from orbi.pi_process import PiWatchOptions
from seam import seam
from tests.fakes.bootstrap_pi import (
    fake_session_records,
    make_committing_fake_pi,
    make_fake_pi,
)

SOURCE_REPO = "owner/repo"
ISSUE = 7
RUN_ID = "abc12345"
BRANCH = f"orbi/owner-repo-issue-{ISSUE}"

# The real seam, captured before any fan-out patch replaces it.
REAL_RUN_COMMAND = journal.run_command


def _config(tmp_path: Path, **kwargs) -> config_domain.RunnerConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("system", encoding="utf-8")
    return config_domain.RunnerConfig(
        prompt=prompt, repo_dir=tmp_path, source_repos=(SOURCE_REPO,),
        workspace_root=tmp_path, context_files=(), skills=(),
        base_branch="main", run_id=RUN_ID, **kwargs,
    )


def _steering_check(monkeypatch, tmp_path: Path, *snapshots):
    """The run_pi steering check answering the given poll payloads."""
    polls = iter(snapshots)
    monkeypatch.setitem(
        pi_session.__dict__, "issue_view", lambda *a, **k: next(polls),
    )
    monkeypatch.setitem(pi_session.__dict__, "changed_files", lambda *a, **k: [])
    monkeypatch.setitem(
        pi_session.__dict__, "activity_snapshot", lambda *a, **k: None,
    )
    captured: dict = {}
    monkeypatch.setitem(
        pi_session.__dict__, "stream_pi",
        lambda command, **kw: captured.update(kw) or "done",
    )
    issue = {"number": ISSUE, "title": "title", "body": "body"}
    ctx = RunContext(RUN_ID, ISSUE, BRANCH, tmp_path, SOURCE_REPO)
    assert pi_session.run_pi(issue, ctx, _config(tmp_path)) == "done"
    return captured["watch"].steering_check


def _payload(state: str, comments=None) -> dict:
    return {"comments": comments or [], "body": "body", "state": state}


def test_the_steering_poll_keeps_an_open_issue_running(monkeypatch, tmp_path):
    """The poll's own payload now carries the state; OPEN steers nothing."""
    check = _steering_check(monkeypatch, tmp_path, _payload("OPEN"))
    assert check() is None


def test_the_steering_poll_stops_the_session_when_the_issue_is_closed(
    monkeypatch, tmp_path,
):
    check = _steering_check(
        monkeypatch, tmp_path, _payload("OPEN"), _payload("CLOSED"),
    )
    assert check() is None
    with pytest.raises(pi_process.DeliveryIssueClosed):
        check()


def test_a_closed_issue_terminates_the_running_session(tmp_path):
    """The stop ends the live process instead of the session running on."""
    def check():
        raise pi_process.DeliveryIssueClosed("issue closed")

    started = time.monotonic()
    with pytest.raises(pi_process.DeliveryIssueClosed):
        pi_session.stream_pi(
            make_fake_pi(tmp_path, session_records=fake_session_records()),
            ctx=RunContext(RUN_ID, ISSUE, BRANCH, tmp_path, SOURCE_REPO),
            cwd=tmp_path, timeout=30,
            watch=PiWatchOptions(
                poll_interval=0.01, steering_poll_seconds=0.001,
                steering_check=check,
            ),
        )
    # The fake Pi ran its own end (a fast, clean exit) only after the stop
    # request: the session never waited for it to finish by itself.
    assert time.monotonic() - started < 10


class FakeGitHub:
    """The gh boundary of one delivery scene: reads answer, writes record.

    The steering poll's payloads come from `states` (the last one repeats);
    the closeout's own `--json state` read answers `final_state`.
    """

    def __init__(self, *, states=("OPEN", "CLOSED"), final_state="CLOSED"):
        self.calls: list[list[str]] = []
        self.comments: list[str] = []
        self.poll_states: list[str] = []
        self.states = list(states)
        self.final_state = final_state

    def run_command(self, command, **kwargs):
        if command[0] != "gh":
            return REAL_RUN_COMMAND(command, **kwargs)
        self.calls.append(list(command))
        words = [word for word in command[1:] if word != "--include"]
        if words[0] == "api":
            return self._api(words)
        if words[1] == "comment":
            self.comments.append(words[words.index("--body") + 1])
            return "https://github.com/owner/repo/issues/7#issuecomment-1"
        return self._issue_view(words)

    def _api(self, words: list[str]) -> str:
        if "--method" not in words:
            return "[]"
        index = words.index("--method") + 1
        if words[index] != "POST":
            return ""
        body = words[words.index("--field") + 1]
        self.comments.append(body[len("body="):])
        return json.dumps({"id": 77, "body": body[len("body="):]})

    def _issue_view(self, words: list[str]) -> str:
        """Answer the Issue reads this scene makes: labels, state, comments.

        The poll (`comments,body,state`) is the one read whose state changes
        between calls — the Issue reads OPEN at claim and CLOSED later.
        """
        fields = words[words.index("--json") + 1]
        if fields == "labels":
            return json.dumps({"labels": [{"name": "ai-ready"}]})
        if fields == "state":
            return json.dumps({"state": self.final_state})
        if fields != "comments,body,state":
            return json.dumps({"comments": []})
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        self.poll_states.append(state)
        return json.dumps({"comments": [], "body": "Body", "state": state})


@pytest.fixture
def delivery_repo(tmp_path):
    """A local bare origin plus a clone of main with one base commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base")
    git(clone, "push", "origin", "main")
    return origin, clone


def _closed_comments(comments: list[str]) -> list[str]:
    return [
        body for body in comments
        if "the Issue was closed while the delivery" in body
    ]


def _pull_request_calls(gh: FakeGitHub) -> list[list[str]]:
    """Every gh call whose subcommand is `pr`: none may be made."""
    return [
        command for command in gh.calls
        if len(command) > 1 and command[1] == "pr"
    ]


def test_a_closed_issue_on_a_later_poll_ends_the_delivery(
    monkeypatch, tmp_path, caplog, delivery_repo,
):
    """The acceptance scene: OPEN at claim, CLOSED on a later poll.

    The session is terminated after that poll; the comment the closeout
    already posts for a closed Issue is posted ONCE, no PR is created, the
    work the session committed is on the pushed delivery branch, and the run
    ends with `result=issue_closed`.
    """
    caplog.set_level("INFO")
    origin, clone = delivery_repo
    gh = FakeGitHub()
    monkeypatch.setattr(seam, "run_command", gh.run_command)
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    finished = tmp_path / "pi-finished"
    fake_pi = make_committing_fake_pi(tmp_path / "fake-pi", finished=finished)

    started = time.monotonic()
    result = runner.process_issue(
        {"number": ISSUE, "title": "Deliver", "body": "Body"},
        # Generous poll spacing: the fake commits right after it starts, so
        # the first (OPEN) poll must come after that commit.
        _config(
            clone, pi_executable=str(fake_pi), steering_poll_seconds=0.5,
        ),
        SOURCE_REPO,
    )
    elapsed = time.monotonic() - started

    assert result == ("issue-closed", None)
    # The Issue read OPEN on the first poll and CLOSED on a later one: the
    # session was stopped by the closed state, not by its own end of work.
    assert gh.poll_states == ["OPEN", "CLOSED"]
    # The session process was terminated: it never reached its own end.
    assert not finished.exists()
    assert elapsed < 30
    # The work the session committed is on the PUSHED delivery branch.
    assert git(origin, "show", f"{BRANCH}:delivered.txt") == "delivered"
    pushed_head = git(origin, "rev-parse", f"refs/heads/{BRANCH}")
    assert pushed_head != git(origin, "rev-parse", "refs/heads/main")
    # No PR was created or touched.
    assert _pull_request_calls(gh) == []
    # Exactly the ONE closed-mid-delivery comment, with the run marker.
    closed = _closed_comments(gh.comments)
    assert len(closed) == 1
    assert f"<!-- orbi:run={RUN_ID} -->" in closed[0]
    assert BRANCH in closed[0]
    assert "no PR was opened" in closed[0]
    # The run ends with the existing outcome.
    assert "delivery_issue_closed branch=" in caplog.text
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "result=issue_closed" in ends[0]
    assert "pr=-" in ends[0]


def test_a_poll_payload_without_a_state_is_a_bypassed_poll_failure(
    monkeypatch, tmp_path, caplog,
):
    """A payload the poll cannot judge never stops the session.

    The state is part of the same read; an unreadable one is the existing
    `steering_poll_failed` bypass (the session keeps running, and the
    pre-PR check still ends a closed Issue).
    """
    caplog.set_level("INFO")
    check = _steering_check(monkeypatch, tmp_path, {"comments": [], "body": "b"})
    assert check() is None
    assert "steering_poll_failed" in caplog.text


def _branch_exists(origin: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=origin, capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def test_a_stop_before_the_delivery_commit_pushes_nothing(
    monkeypatch, tmp_path, caplog, delivery_repo,
):
    """The interrupted worktree is not a delivery to verify.

    The session is stopped before it commits: the uncommitted work is never
    committed by the Runner, nothing is pushed, and the run still ends with
    the closed-mid-delivery comment and `result=issue_closed` — the slot is
    free for the next ticket instead of a recoverable resume of a closed
    Issue.
    """
    caplog.set_level("INFO")
    origin, clone = delivery_repo
    gh = FakeGitHub()
    monkeypatch.setattr(seam, "run_command", gh.run_command)
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    finished = tmp_path / "pi-finished"
    fake_pi = make_committing_fake_pi(
        tmp_path / "fake-pi", finished=finished, commit=False,
    )

    result = runner.process_issue(
        {"number": ISSUE, "title": "Deliver", "body": "Body"},
        _config(
            clone, pi_executable=str(fake_pi), steering_poll_seconds=0.15,
        ),
        SOURCE_REPO,
    )

    assert result == ("issue-closed", None)
    assert gh.poll_states == ["OPEN", "CLOSED"]
    assert not finished.exists()
    # Nothing was committed, so nothing is pushed.
    assert not _branch_exists(origin, BRANCH)
    assert _pull_request_calls(gh) == []
    assert len(_closed_comments(gh.comments)) == 1
    assert "delivery_uncommitted_changes" not in caplog.text
    ends = [line for line in caplog.text.splitlines() if " run_end " in line]
    assert len(ends) == 1
    assert "result=issue_closed" in ends[0]


def test_a_reopened_issue_resumes_instead_of_opening_an_empty_pr(
    monkeypatch, tmp_path, caplog, delivery_repo,
):
    """A stop whose Issue reads OPEN again has no delivery to PR.

    The poll saw CLOSED, the closeout's own read sees OPEN and nothing was
    committed: the run is recoverable (the next tick resumes the same run)
    instead of opening an empty PR or leaking the reason as a crash.
    """
    caplog.set_level("INFO")
    origin, clone = delivery_repo
    gh = FakeGitHub(final_state="OPEN")
    monkeypatch.setattr(seam, "run_command", gh.run_command)
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    finished = tmp_path / "pi-finished"
    fake_pi = make_committing_fake_pi(
        tmp_path / "fake-pi", finished=finished, commit=False,
    )

    result = runner.process_issue(
        {"number": ISSUE, "title": "Deliver", "body": "Body"},
        _config(
            clone, pi_executable=str(fake_pi), steering_poll_seconds=0.15,
        ),
        SOURCE_REPO,
    )

    assert result == ("failed", None)
    assert gh.poll_states == ["OPEN", "CLOSED"]
    assert not finished.exists()
    assert not _branch_exists(origin, BRANCH)
    assert _pull_request_calls(gh) == []
    assert _closed_comments(gh.comments) == []
    assert "the next tick resumes the same run" in caplog.text
