"""Issue #2440: GitHub never refreshed the delivery PR after a push.

The incident (orbi-cloud#3953, run c9839bce): the runner pushed the delivery
branch, GitHub's PR view kept the pre-push head, mergeable stayed UNKNOWN and
every tick deferred the merge for 8.5 hours — no stop, no comment, no label
change. The merge gate now asks GitHub to update the PR branch itself, counts
the observation in the run state, and — past the bound — stops the run with a
comment that names the cause and the repair.
"""
import json
import logging
import subprocess

import pytest

import orbi.failure as failure
import orbi.failure_report as failure_report
import orbi.merge_exec as merge_exec
import orbi.pr_head_refresh as pr_head_refresh
import orbi.run_state as run_state
import orbi.runner as runner
from orbi.progress import run_marker
from tests.fakes.github import FakeGh, install_run_command
from tests.fakes.pr_head_refresh import (
    gate_scene, refresh_requests, refresh_scene,
)

REPO = "owner/repo"
BRANCH = "orbi/owner-repo-issue-7"
# The stale PR head GitHub kept reporting, and the branch tip it never read.
STALE_HEAD = "06ab6cba"
BRANCH_TIP = "480d341b"
RUN_ID = "c9839bce"
PR_URL = f"https://github.com/{REPO}/pull/7"


@pytest.fixture(autouse=True)
def _reset_run_binding(bind_run_id):
    bind_run_id(None)


def _seed_run_state(worktree, **extra):
    """The claim-time run state file every live delivery carries."""
    state = {
        "run_id": RUN_ID, "issue": 7, "repo": REPO,
        "branch": BRANCH, "worktree": str(worktree),
    }
    state.update(extra)
    path = worktree / ".orbi" / "run-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    return path


def _journal(caplog):
    return [record.getMessage() for record in caplog.records]


def _refresh(worktree, *, pr_head=STALE_HEAD):
    return pr_head_refresh.refresh_lagging_head(
        worktree, repo=REPO, pr_number=7, branch=BRANCH, pr_head=pr_head,
    )


def test_a_current_pr_head_is_waited_on_without_a_refresh_request(
        monkeypatch, tmp_path):
    """Acceptance 3: the PR head IS the branch tip — a briefly UNKNOWN
    mergeability is waited on exactly as today: no request, no counter."""
    _fake, calls = refresh_scene(monkeypatch, tip=STALE_HEAD)
    _seed_run_state(tmp_path)
    _refresh(tmp_path, pr_head=STALE_HEAD)
    assert refresh_requests(calls) == []
    assert "pr_head_refresh" not in run_state.read_run_state(tmp_path)


def test_a_lagging_pr_head_asks_github_to_update_the_branch(
        monkeypatch, tmp_path, caplog):
    """Acceptance 1: the PR head is an old commit while the branch tip is
    newer — the runner makes GitHub re-read the branch itself."""
    fake, calls = refresh_scene(monkeypatch, tip=BRANCH_TIP)
    _seed_run_state(tmp_path)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        _refresh(tmp_path)
    # The documented endpoint takes the MOST RECENT COMMIT ON THE PR BRANCH:
    # the stale PR view head is exactly what answers 422.
    assert refresh_requests(calls) == [[
        "gh", "api", "-X", "PUT", f"repos/{REPO}/pulls/7/update-branch",
        "-f", f"expected_head_sha={BRANCH_TIP}",
    ]]
    # Issue #95: the network mutation is bounded, never an open wait.
    assert fake.kwargs_seen[-1].get("timeout") == (
        pr_head_refresh.REFRESH_REQUEST_TIMEOUT_SECONDS
    )
    state = run_state.read_run_state(tmp_path)
    assert state["pr_head_refresh"] == {"head": STALE_HEAD, "ticks": 1}
    lines = _journal(caplog)
    assert any(
        line.startswith("pr_head_lagging ")
        and f"pr_head={STALE_HEAD}" in line
        and f"branch_tip={BRANCH_TIP}" in line
        for line in lines
    ), lines
    assert any(
        line.startswith("pr_head_refresh_requested ") for line in lines
    ), lines


def test_the_run_stops_after_the_bounded_number_of_refresh_ticks(
        monkeypatch, tmp_path, caplog):
    """Acceptance 2: the PR never refreshes — the run ends with the stated
    cause and repair instead of polling forever."""
    _fake, calls = refresh_scene(monkeypatch, tip=BRANCH_TIP)
    _seed_run_state(tmp_path)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        for _tick in range(pr_head_refresh.PR_HEAD_REFRESH_TICKS):
            _refresh(tmp_path)
        with pytest.raises(pr_head_refresh.PrHeadNotRefreshed) as stop:
            _refresh(tmp_path)
    # Bounded: the requests stop at the bound, the stop is the last attempt.
    assert len(refresh_requests(calls)) == (
        pr_head_refresh.PR_HEAD_REFRESH_TICKS
    )
    message = str(stop.value)
    assert "GitHub did not refresh PR #7" in message
    assert STALE_HEAD in message and BRANCH_TIP in message
    assert "Update branch" in message
    record = stop.value.failure_record
    assert isinstance(record, failure.Failure)
    assert (record.reason_code, record.action_code, record.retry_safe) == (
        "human_decision_required", "decide", False,
    )
    assert any(
        line.startswith("pr_head_lagging ")
        and f"ticks={pr_head_refresh.PR_HEAD_REFRESH_TICKS + 1}" in line
        for line in _journal(caplog)
    )


def test_a_failed_refresh_request_still_counts_the_tick(monkeypatch, tmp_path):
    """GitHub answers 422 (the branch is not behind the base, or a conflict):
    the attempt is spent, journaled, and the tick still waits — the bound,
    not one API answer, decides when the run stops."""
    error = subprocess.CalledProcessError(
        422, ["gh", "api"], output="",
        stderr="expected head sha didn't match current head ref",
    )
    _fake, calls = refresh_scene(
        monkeypatch, tip=BRANCH_TIP, refresh_error=error,
    )
    _seed_run_state(tmp_path)
    _refresh(tmp_path)
    assert len(refresh_requests(calls)) == 1
    assert run_state.read_run_state(tmp_path)["pr_head_refresh"]["ticks"] == 1


def test_an_unreadable_branch_tip_waits_as_today(monkeypatch, tmp_path):
    """A network/auth failure on the branch-tip probe is not a verdict: the
    merge is deferred exactly as before, and no write is spent."""
    _fake, calls = refresh_scene(monkeypatch, tip=None)
    _seed_run_state(tmp_path)
    _refresh(tmp_path)
    assert refresh_requests(calls) == []
    assert "pr_head_refresh" not in run_state.read_run_state(tmp_path)


def test_an_unreadable_branch_tip_keeps_the_bound_it_cannot_reach(
        monkeypatch, tmp_path):
    """A probe failure is not a verdict about the PR, so it never resets the
    counted scene: with each flaky read clearing the count, a stale PR would
    poll forever and acceptance 2's bound would never arrive."""
    _fake, calls = refresh_scene(monkeypatch, tip=None)
    _seed_run_state(
        tmp_path, pr_head_refresh={"head": STALE_HEAD, "ticks": 2},
    )
    _refresh(tmp_path)
    assert refresh_requests(calls) == []
    assert run_state.read_run_state(tmp_path)["pr_head_refresh"] == {
        "head": STALE_HEAD, "ticks": 2,
    }


def test_a_missing_run_state_never_starts_an_unbounded_refresh_loop(
        monkeypatch, tmp_path, caplog):
    """Without the record the tick cannot be counted, and an uncounted loop
    is exactly the polling this Issue bans: wait as today, journal it."""
    _fake, calls = refresh_scene(monkeypatch, tip=BRANCH_TIP)
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        _refresh(tmp_path)
    assert refresh_requests(calls) == []
    assert any(
        line.startswith("pr_head_refresh_unrecorded ")
        for line in _journal(caplog)
    ), _journal(caplog)


def test_a_refreshed_pr_head_clears_the_counter(monkeypatch, tmp_path):
    """The scene resolved (the PR head follows the branch again): a later
    stale scene starts its own bounded budget."""
    refresh_scene(monkeypatch, tip=BRANCH_TIP)
    _seed_run_state(tmp_path)
    _refresh(tmp_path)
    assert run_state.read_run_state(tmp_path)["pr_head_refresh"]["ticks"] == 1
    refresh_scene(monkeypatch, tip=STALE_HEAD)
    _refresh(tmp_path)
    assert "pr_head_refresh" not in run_state.read_run_state(tmp_path)


def test_the_blocked_comment_names_the_cause_and_the_repair(monkeypatch):
    """Acceptance 2, reader side: the stop is ONE ai-blocked comment whose
    Reason names the cause and the repair."""
    gh = FakeGh(REPO)
    gh.add_issue(7, title="task")
    install_run_command(monkeypatch, gh)
    error = pr_head_refresh.PrHeadNotRefreshed(
        pr_head_refresh.stop_message(
            pr_number=7, pr_head=STALE_HEAD, branch=BRANCH,
            branch_tip=BRANCH_TIP, ticks=pr_head_refresh.PR_HEAD_REFRESH_TICKS,
        ),
        pr=7, pr_head=STALE_HEAD, branch_tip=BRANCH_TIP,
    )
    outcome = failure_report.report_delivery_failure(
        error, issue={"number": 7, "title": "task", "labels": []},
        source_repo=REPO, run_id=RUN_ID, pr_url=PR_URL, worktree=None,
        branch=BRANCH, role=runner.ROLE_REVIEW, language_code="en",
        finish=False,
    )
    assert outcome == "blocked"
    assert "ai-blocked" in gh.issues[7]["labels"]
    posted = [
        comment["body"] for comment in gh.issues[7]["comments"]
        if comment["author"]["login"] == gh.login
    ]
    body = posted[-1]
    assert body.startswith(f"{run_marker(RUN_ID)}\n")
    assert "GitHub did not refresh PR #7" in body
    assert "Update branch" in body
    block = failure.parse(body)
    assert block is not None
    assert block.reason_code == "human_decision_required"


# ---------------------------------------------------------------------------
# The merge gate seam: the tick that used to poll forever
# ---------------------------------------------------------------------------


def _pr_state(mergeable="UNKNOWN", *, head=STALE_HEAD, rollup=()):
    return {"state": "OPEN", "mergeable": mergeable, "headRefOid": head,
            "statusCheckRollup": list(rollup)}


def _one_gate_pass_states():
    """One UNKNOWN pass = the first read plus every bounded re-read."""
    return [
        _pr_state()
        for _ in range(1 + len(merge_exec.MERGEABLE_UNKNOWN_RETRY_DELAYS))
    ]


def _gate(tmp_path, *, head=STALE_HEAD):
    return merge_exec.merge_gate(
        tmp_path,
        {"number": 7, "url": "u", "base_ref": "main", "base_oid": "b1",
         "head_ref": BRANCH, "head_oid": head},
        "main", repo_dir=tmp_path, source_repo=REPO, language_code="en",
    )


def test_the_gate_asks_github_to_refresh_a_lagging_pr_head(
        monkeypatch, tmp_path):
    """Acceptance 1: the PR head is an old commit, the branch tip is newer
    and mergeability stays UNKNOWN — the gate makes GitHub re-read the
    branch instead of deferring the same read forever."""
    _seed_run_state(tmp_path)
    _fake, calls = gate_scene(
        monkeypatch, _one_gate_pass_states(), tip=BRANCH_TIP,
    )
    with pytest.raises(merge_exec.DeliveryDeferred):
        _gate(tmp_path)
    assert refresh_requests(calls) == [[
        "gh", "api", "-X", "PUT", f"repos/{REPO}/pulls/7/update-branch",
        "-f", f"expected_head_sha={BRANCH_TIP}",
    ]]


def test_the_gate_continues_once_github_reports_the_new_head(
        monkeypatch, tmp_path):
    """Acceptance 1, second half: after the refresh the PR reports the
    branch tip, so the next gate pass follows its normal path (here: it
    waits for the new head's checks) and asks for no further refresh."""
    _seed_run_state(tmp_path)
    _fake, calls = gate_scene(
        monkeypatch, _one_gate_pass_states(), tip=BRANCH_TIP,
    )
    with pytest.raises(merge_exec.DeliveryDeferred):
        _gate(tmp_path)
    assert len(refresh_requests(calls)) == 1
    _refreshed, later = gate_scene(monkeypatch, [_pr_state(
        head=BRANCH_TIP,
        rollup=[{
            "name": "tests", "status": "IN_PROGRESS", "conclusion": None,
        }],
    )], tip=BRANCH_TIP)
    with pytest.raises(merge_exec.DeliveryDeferred,
                       match="CI is still running"):
        _gate(tmp_path, head=BRANCH_TIP)
    assert refresh_requests(later) == []


def test_a_current_pr_head_with_unknown_mergeability_is_waited_on_as_today(
        monkeypatch, tmp_path):
    """Acceptance 3: the PR head IS the branch tip — GitHub is still
    computing mergeability, so the gate defers exactly as before."""
    _fake, calls = gate_scene(
        monkeypatch, _one_gate_pass_states(), tip=STALE_HEAD,
    )
    with pytest.raises(merge_exec.DeliveryDeferred,
                       match="mergeable state is UNKNOWN"):
        _gate(tmp_path)
    assert refresh_requests(calls) == []


def test_the_gate_stops_after_the_bounded_refresh_ticks(
        monkeypatch, tmp_path):
    """Acceptance 2: the PR still does not refresh after the bound — the
    gate raises the stop the review round reports as ai-blocked, and makes
    no further request."""
    _seed_run_state(tmp_path, pr_head_refresh={
        "head": STALE_HEAD, "ticks": pr_head_refresh.PR_HEAD_REFRESH_TICKS,
    })
    _fake, calls = gate_scene(
        monkeypatch, _one_gate_pass_states(), tip=BRANCH_TIP,
    )
    with pytest.raises(pr_head_refresh.PrHeadNotRefreshed,
                       match="GitHub did not refresh PR #7"):
        _gate(tmp_path)
    assert refresh_requests(calls) == []


def test_no_pr_head_where_a_head_belongs_is_left_alone(monkeypatch, tmp_path):
    """The gate may hand in a read without a head (an unreadable answer):
    nothing is counted, requested or cleared."""
    _fake, calls = refresh_scene(monkeypatch, tip=BRANCH_TIP)
    _seed_run_state(tmp_path)
    _refresh(tmp_path, pr_head="")
    _refresh(tmp_path, pr_head=None)
    assert calls == []
    assert "pr_head_refresh" not in run_state.read_run_state(tmp_path)


def test_a_counter_for_another_scene_starts_a_fresh_budget(
        monkeypatch, tmp_path):
    """The count belongs to ONE stale head; a corrupt count authorizes
    nothing, so the new scene's budget restarts at one."""
    refresh_scene(monkeypatch, tip=BRANCH_TIP)
    _seed_run_state(
        tmp_path, pr_head_refresh={"head": "another-head", "ticks": 3},
    )
    _refresh(tmp_path)
    assert run_state.read_run_state(tmp_path)["pr_head_refresh"] == {
        "head": STALE_HEAD, "ticks": 1,
    }
    _seed_run_state(
        tmp_path, pr_head_refresh={"head": STALE_HEAD, "ticks": "3"},
    )
    _refresh(tmp_path)
    assert run_state.read_run_state(tmp_path)["pr_head_refresh"] == {
        "head": STALE_HEAD, "ticks": 1,
    }


def test_a_resolved_scene_without_a_run_state_writes_nothing(
        monkeypatch, tmp_path):
    """A recreated worktree has no state file: clearing is a no-op, never an
    error."""
    _fake, calls = refresh_scene(monkeypatch, tip=STALE_HEAD)
    _refresh(tmp_path, pr_head=STALE_HEAD)
    assert calls != []
    assert not (tmp_path / ".orbi" / "run-state.json").exists()


def test_a_corrupt_run_state_never_starts_a_refresh_loop(
        monkeypatch, tmp_path):
    """A state file the resume readers already refuse cannot count a tick,
    so the merge waits as before rather than refreshing uncounted."""
    _fake, calls = refresh_scene(monkeypatch, tip=BRANCH_TIP)
    path = tmp_path / ".orbi" / "run-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _refresh(tmp_path)
    assert refresh_requests(calls) == []

