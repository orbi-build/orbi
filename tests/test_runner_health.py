"""Behavioral tests for the Runner self-health check (Issue #266).

The two 2026-09-04 incidents (#246: three identical delivery failures on one
Issue, #262: the service crash loop) were both found by humans reading the
journal. These tests pin the active detection contract:

- three consecutive failed runs on one Issue with the SAME conservative
  failure fingerprint -> a structured `health_degraded` journal line and ONE
  comment on the Issue carrying the latest failing run's marker (never one
  comment per tick);
- the service unit crashing >= 3 times in the last 60 minutes -> a structured
  `health_degraded` line and ONE deduplicated bug+ai-ready Issue (the #106
  body-marker mechanism);
- a stale pickup (last successful claim > 24 h ago) with a NON-EMPTY ai-ready
  queue -> an alarm; an EMPTY queue is idle, not a failure (no alarm);
- normal multi-round review/fix cycles (different fingerprints, a successful
  run breaking the streak) never produce a finding.
"""
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from orbi import runner_health

# Captured at import time (before any monkeypatch): the conftest default
# stubs `run_health_check` for the dispatch tests, and this module
# exercises the REAL implementation (the fixture below restores it).
_REAL_RUN_HEALTH_CHECK = runner_health.run_health_check


@pytest.fixture(autouse=True)
def _use_real_health_check(monkeypatch):
    monkeypatch.setattr(
        runner_health, "run_health_check", _REAL_RUN_HEALTH_CHECK,
    )


class FakeRunCommand:
    """Records every call and answers from a substring route table.

    Routes are checked in insertion order (first match wins); an
    Exception-valued answer is raised. An unmatched command returns ""."""

    def __init__(self, routes=None):
        self.calls: list[list[str]] = []
        self.routes = routes or {}

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        joined = " ".join(command)
        for needle, answer in self.routes.items():
            if needle in joined:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return ""

    def commands(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if needle in " ".join(c)]


REPO = "owner/repo"
ORBI_REPO = "orbi-build/orbi"


def make_config(tmp_path: Path, *, health_alert_repo=None) -> dict:
    return {
        "repo_dir": tmp_path,
        "source_repos": [REPO],
        "deploy_home": tmp_path,
        "health_alert_repo": health_alert_repo,
    }


def origin_route(url: str = f"git@github.com:{ORBI_REPO}.git") -> dict:
    """A FakeRunCommand route answering the deploy-home origin query."""
    return {"remote get-url origin": url}


def write_state(tmp_path: Path, state: dict) -> Path:
    path = runner_health.health_state_path(tmp_path)
    runner_health.save_health_state(path, state)
    return path


def run_entry(repo: str, issue: int, run_id: str, fingerprint: str,
              outcome: str = "failed", ts: float | None = None) -> dict:
    return {
        "repo": repo, "issue": issue, "run_id": run_id,
        "outcome": outcome, "fingerprint": fingerprint,
        "ts": time.time() if ts is None else ts,
    }


# ---------------------------------------------------------------------------
# Failure fingerprint (conservative: only highly similar errors match)
# ---------------------------------------------------------------------------

def test_failure_fingerprint_is_stable_hex():
    fp = runner_health.failure_fingerprint(
        RuntimeError("delivery_no_commit: nothing to commit"),
    )
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_failure_fingerprint_same_for_same_error_text():
    exc_a = RuntimeError("delivery_no_commit: ?? .muyan-pilot/ run_id=8ecac198")
    exc_b = RuntimeError("delivery_no_commit: ?? .muyan-pilot/ run_id=abcd1234")
    # The volatile 8-hex run ids are stripped: same error, same fingerprint.
    assert runner_health.failure_fingerprint(exc_a) == \
        runner_health.failure_fingerprint(exc_b)


def test_failure_fingerprint_differs_for_different_errors():
    exc_a = RuntimeError("delivery_no_commit: ?? .muyan-pilot/")
    exc_b = RuntimeError("verify_pr_failed: PR base mismatch")
    assert runner_health.failure_fingerprint(exc_a) != \
        runner_health.failure_fingerprint(exc_b)


def test_failure_fingerprint_differs_for_different_exception_classes():
    exc_a = RuntimeError("the same message")
    exc_b = ValueError("the same message")
    assert runner_health.failure_fingerprint(exc_a) != \
        runner_health.failure_fingerprint(exc_b)


def test_failure_fingerprint_strips_timestamps_and_long_shas():
    exc_a = RuntimeError(
        "boom at 2026-09-04 11:41:02 commit "
        "bfb10fb2acd00c77c8c360cdd6d3e6d9771025db",
    )
    exc_b = RuntimeError(
        "boom at 2026-09-05 09:00:00 commit "
        "0000000000000000000000000000000000000000",
    )
    assert runner_health.failure_fingerprint(exc_a) == \
        runner_health.failure_fingerprint(exc_b)


def test_failure_fingerprint_uses_first_line_only():
    exc_a = RuntimeError("first line\nsecond line differs A")
    exc_b = RuntimeError("first line\nsecond line differs B")
    assert runner_health.failure_fingerprint(exc_a) == \
        runner_health.failure_fingerprint(exc_b)


# ---------------------------------------------------------------------------
# State file (the lightweight file in the existing state dir)
# ---------------------------------------------------------------------------

def test_health_state_path_lives_in_state_dir(tmp_path):
    assert runner_health.health_state_path(tmp_path) == \
        tmp_path / ".orbi" / "health.json"


def test_state_roundtrip(tmp_path):
    path = write_state(tmp_path, {
        "runs": [run_entry(REPO, 41, "r1", "fp1")],
        "last_pickup_ts": 1234.5,
        "alerted": ["owner/repo#41:fp1"],
    })
    state = runner_health.load_health_state(path)
    assert state["runs"][0]["run_id"] == "r1"
    assert state["last_pickup_ts"] == 1234.5
    assert state["alerted"] == ["owner/repo#41:fp1"]


def test_load_missing_state_returns_fresh(tmp_path):
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    assert state == {"runs": [], "last_pickup_ts": None, "alerted": []}


def test_load_corrupt_state_returns_fresh(tmp_path):
    path = runner_health.health_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    state = runner_health.load_health_state(path)
    assert state == {"runs": [], "last_pickup_ts": None, "alerted": []}


def test_load_non_dict_state_returns_fresh(tmp_path):
    path = runner_health.health_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    state = runner_health.load_health_state(path)
    assert state == {"runs": [], "last_pickup_ts": None, "alerted": []}


def test_load_state_with_invalid_fields_falls_back_to_fresh(tmp_path):
    path = write_state(tmp_path, {
        "runs": "not-a-list",
        "last_pickup_ts": True,
        "alerted": ["ok", 42],
    })
    state = runner_health.load_health_state(path)
    assert state == {"runs": [], "last_pickup_ts": None, "alerted": []}


def test_record_run_attempt_bounds_history(tmp_path):
    path = write_state(tmp_path, {"runs": [], "last_pickup_ts": None,
                                  "alerted": []})
    for i in range(60):
        runner_health.record_run_attempt(
            path, repo=REPO, issue=41, run_id=f"r{i:02d}",
            outcome="failed", fingerprint="fp",
        )
    state = runner_health.load_health_state(path)
    assert len(state["runs"]) == runner_health.RECENT_RUNS_KEEP
    assert state["runs"][-1]["run_id"] == "r59"


# ---------------------------------------------------------------------------
# Repeated same-fingerprint failure detection
# ---------------------------------------------------------------------------

def test_three_consecutive_same_fingerprint_failures_are_a_finding():
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp1"),
            run_entry(REPO, 41, "r3", "fp1"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    findings = runner_health.repeated_failure_findings(state)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["repo"] == REPO
    assert finding["issue"] == 41
    assert finding["fingerprint"] == "fp1"
    assert finding["count"] == 3
    # Newest failing run first (its marker goes on the alert comment).
    assert finding["run_ids"] == ["r3", "r2", "r1"]


def test_two_failures_are_below_threshold():
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp1"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    assert runner_health.repeated_failure_findings(state) == []


def test_success_breaks_the_streak():
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp1"),
            run_entry(REPO, 41, "r3", "fp1"),
            # The delivery eventually succeeded: the old streak is history.
            run_entry(REPO, 41, "r4", "fp1", outcome="pr_opened"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    assert runner_health.repeated_failure_findings(state) == []


def test_different_fingerprints_are_not_the_same_pit():
    # Normal multi-round review/fix: every round fails differently.
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp2"),
            run_entry(REPO, 41, "r3", "fp3"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    assert runner_health.repeated_failure_findings(state) == []


def test_other_issues_do_not_break_the_streak():
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 42, "x1", "fp9"),
            run_entry(REPO, 41, "r2", "fp1"),
            run_entry(REPO, 41, "r3", "fp1"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    findings = runner_health.repeated_failure_findings(state)
    assert len(findings) == 1
    assert findings[0]["issue"] == 41
    assert findings[0]["count"] == 3


def test_a_new_streak_after_an_old_one_is_counted_from_the_newest():
    state = {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp1"),
            run_entry(REPO, 41, "r3", "fp1"),
            run_entry(REPO, 41, "r4", "fp1", outcome="pr_opened"),
            run_entry(REPO, 41, "r5", "fp2"),
            run_entry(REPO, 41, "r6", "fp2"),
            run_entry(REPO, 41, "r7", "fp2"),
        ],
        "last_pickup_ts": None, "alerted": [],
    }
    findings = runner_health.repeated_failure_findings(state)
    assert len(findings) == 1
    assert findings[0]["fingerprint"] == "fp2"
    assert findings[0]["run_ids"] == ["r7", "r6", "r5"]


# ---------------------------------------------------------------------------
# Crash counting from the systemd journal
# ---------------------------------------------------------------------------

CRASH_LINE = (
    "Sep 04 11:41:02 host systemd[1015]: orbi@1.service: Main process "
    "exited, code=exited, status=1/FAILURE"
)
FAILED_RESULT_LINE = (
    "Sep 04 11:45:00 host systemd[1015]: orbi@2.service: Failed with "
    "result 'exit-code'."
)


def test_count_crashes_counts_real_systemd_exit_lines():
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service":
            f"{CRASH_LINE}\n{CRASH_LINE}\n",
        "journalctl --user -u orbi@2.service":
            f"{FAILED_RESULT_LINE}\n",
    })
    assert runner_health.count_crashes(fake) == 3


def test_count_crashes_ignores_non_crash_lines():
    noise = (
        "Sep 04 11:40:00 host orbi[123]: INFO [abc12345] heartbeat "
        "issue=owner/repo#41 role=implement phase=test\n"
        "Sep 04 11:40:01 host orbi[123]: INFO [abc12345] command=gh issue "
        'list --search "Main process exited"\n'
    )
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": noise,
        "journalctl --user -u orbi@2.service": "",
    })
    assert runner_health.count_crashes(fake) == 0


def test_count_crashes_uses_a_bounded_window():
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
    })
    runner_health.count_crashes(fake)
    for call in fake.calls:
        assert call[0] == "timeout"
        assert "--since" in call
        assert "-60min" in call


# ---------------------------------------------------------------------------
# Stale pickup (queue idle vs system stuck)
# ---------------------------------------------------------------------------

def test_stale_pickup_finding_requires_an_old_pickup():
    fresh = {"runs": [], "last_pickup_ts": time.time(), "alerted": []}
    assert runner_health.stale_pickup_finding(fresh) is False
    old = {"runs": [], "last_pickup_ts": time.time() - 48 * 3600,
           "alerted": []}
    assert runner_health.stale_pickup_finding(old) is True
    never = {"runs": [], "last_pickup_ts": None, "alerted": []}
    assert runner_health.stale_pickup_finding(never) is False


# ---------------------------------------------------------------------------
# run_health_check: the tick-start orchestrator
# ---------------------------------------------------------------------------

def test_healthy_tick_produces_no_alerts_and_no_github_traffic(tmp_path):
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == []
    assert fake.commands("gh issue") == []


def test_repeated_failure_alerts_once_with_the_latest_run_marker(tmp_path):
    write_state(tmp_path, {
        "runs": [
            run_entry(REPO, 41, "r1", "fp1"),
            run_entry(REPO, 41, "r2", "fp1"),
            run_entry(REPO, 41, "r3", "fp1"),
        ],
        "last_pickup_ts": time.time(), "alerted": [],
    })
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == [f"repeated_failure:{REPO}#41"]
    comments = fake.commands("gh issue comment")
    assert len(comments) == 1
    comment = comments[0]
    assert comment[2:5] == ["gh", "issue", "comment"]
    assert "41" in comment
    assert "--repo" in comment and REPO in comment
    body = comment[comment.index("--body") + 1]
    assert "<!-- orbi:run=r3 -->" in body
    assert "run_id=r3" in body
    assert "fp1" in body

    # The next tick must NOT comment again (deduped via the state file).
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == []
    assert len(fake.commands("gh issue comment")) == 1


def test_crash_loop_creates_one_deduplicated_bug_issue(tmp_path):
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    journal = f"{CRASH_LINE}\n{CRASH_LINE}\n{CRASH_LINE}\n"
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": journal,
        "journalctl --user -u orbi@2.service": "",
        **origin_route(),
        # No health Issue exists yet.
        'in:body "orbi-health-fingerprint:': "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == ["crash_loop"]
    creates = fake.commands("gh issue create")
    assert len(creates) == 1
    create = creates[0]
    assert "--label" in create and "bug" in create and "ai-ready" in create
    # Issue #345: the alert routes to the orbi repo (deploy-home origin),
    # never the delivery repo.
    assert create[create.index("--repo") + 1] == ORBI_REPO
    body = create[create.index("--body") + 1]
    marker = runner_health.health_marker("crash_loop")
    assert marker in body
    assert "3" in body  # the crash count is evidence

    # The next tick finds the open Issue: no second create.
    fake.routes['in:body "orbi-health-fingerprint:'] = json.dumps(
        [{"number": 99, "url": "https://github.com/owner/repo/issues/99"}],
    )
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == ["crash_loop"]
    assert len(fake.commands("gh issue create")) == 1


def test_stale_pickup_with_ready_issues_alarms(tmp_path):
    write_state(tmp_path, {"runs": [],
                           "last_pickup_ts": time.time() - 48 * 3600,
                           "alerted": []})
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
        **origin_route(),
        "--label ai-ready": json.dumps([{"number": 7}]),
        'in:body "orbi-health-fingerprint=': "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == ["stale_pickup"]
    creates = fake.commands("gh issue create")
    assert len(creates) == 1
    assert creates[0][creates[0].index("--repo") + 1] == ORBI_REPO


def test_stale_pickup_with_empty_queue_is_idle_not_a_failure(tmp_path):
    write_state(tmp_path, {"runs": [],
                           "last_pickup_ts": time.time() - 48 * 3600,
                           "alerted": []})
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
        "--label ai-ready": "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == []
    assert fake.commands("gh issue create") == []


def test_recorded_pickup_clears_staleness(tmp_path):
    write_state(tmp_path, {"runs": [],
                           "last_pickup_ts": time.time() - 48 * 3600,
                           "alerted": []})
    runner_health.record_pickup(tmp_path)
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    assert runner_health.stale_pickup_finding(state) is False


def test_run_health_check_saves_state_even_when_a_check_fails(tmp_path):
    write_state(tmp_path, {"runs": [], "last_pickup_ts": None,
                           "alerted": []})
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": RuntimeError("boom"),
    })
    with pytest.raises(RuntimeError, match="boom"):
        runner_health.run_health_check(make_config(tmp_path),
                                       run_command=fake)
    # The state file still exists (fresh, untouched).
    state = runner_health.load_health_state(
        runner_health.health_state_path(tmp_path),
    )
    assert state["runs"] == []


def test_create_health_issue_bad_search_json_still_creates(tmp_path):
    fake = FakeRunCommand({
        'in:body "orbi-health-fingerprint:': "{bad json",
    })
    runner_health.create_health_issue(
        REPO, "crash_loop", "detail", run_command=fake,
    )
    assert len(fake.commands("gh issue create")) == 1


def test_stale_pickup_bad_queue_json_is_idle_not_a_failure(tmp_path):
    write_state(tmp_path, {"runs": [],
                           "last_pickup_ts": time.time() - 48 * 3600,
                           "alerted": []})
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": "",
        "journalctl --user -u orbi@2.service": "",
        "--label ai-ready": "{bad json",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == []
    assert fake.commands("gh issue create") == []


def test_health_marker_is_stable_per_check():
    assert runner_health.health_marker("crash_loop") == \
        runner_health.health_marker("crash_loop")
    assert runner_health.health_marker("crash_loop") != \
        runner_health.health_marker("stale_pickup")


# ---------------------------------------------------------------------------
# Issue #345: routing (orbi repo) and config-vs-bug classification
# ---------------------------------------------------------------------------

CONFIG_REASON_LINE = (
    "ERROR [abc12345] Runner failed to start: API key for provider "
    "'opencode' references missing environment variable OPENCODE_API_KEY"
)
BUG_REASON_LINE = (
    "ERROR [abc12345] delivery_no_commit: nothing to commit in worktree"
)


def test_classify_crash_config_caused():
    lines = [
        CRASH_LINE,
        CONFIG_REASON_LINE,
        CRASH_LINE,
    ]
    kind, reason = runner_health.classify_crash(lines)
    assert kind == "config"
    assert reason == CONFIG_REASON_LINE


def test_classify_crash_bug_caused():
    lines = [
        CRASH_LINE,
        BUG_REASON_LINE,
        CRASH_LINE,
    ]
    kind, reason = runner_health.classify_crash(lines)
    assert kind == "bug"
    assert reason == BUG_REASON_LINE


def test_classify_crash_unknown_is_bug_with_empty_reason():
    lines = [CRASH_LINE, CRASH_LINE]
    assert runner_health.classify_crash(lines) == ("bug", "")


def test_classify_crash_prefers_most_recent_config_marker():
    # A config marker beats an earlier bug marker (newest cause wins).
    lines = [BUG_REASON_LINE, CONFIG_REASON_LINE]
    kind, reason = runner_health.classify_crash(lines)
    assert kind == "config"
    assert reason == CONFIG_REASON_LINE


def test_crash_reason_fingerprint_is_stable_and_empty_for_blank():
    fp = runner_health.crash_reason_fingerprint(CONFIG_REASON_LINE)
    assert fp == runner_health.crash_reason_fingerprint(CONFIG_REASON_LINE)
    assert len(fp) == 16
    assert runner_health.crash_reason_fingerprint("") == ""


def test_orbi_repo_from_origin_url_ssh_and_https():
    assert runner_health.orbi_repo_from_origin_url(
        f"git@github.com:{ORBI_REPO}.git",
    ) == ORBI_REPO
    assert runner_health.orbi_repo_from_origin_url(
        f"https://github.com/{ORBI_REPO}.git",
    ) == ORBI_REPO
    assert runner_health.orbi_repo_from_origin_url(
        f"https://github.com/{ORBI_REPO}",
    ) == ORBI_REPO


def test_orbi_repo_from_origin_url_rejects_garbage():
    assert runner_health.orbi_repo_from_origin_url("") is None
    assert runner_health.orbi_repo_from_origin_url("not a url") is None


def test_orbi_repo_from_deploy_home_reads_the_origin(tmp_path):
    fake = FakeRunCommand(origin_route())
    assert runner_health.orbi_repo_from_deploy_home(
        tmp_path, fake,
    ) == ORBI_REPO


def test_orbi_repo_from_deploy_home_query_failure_is_none(tmp_path):
    fake = FakeRunCommand({"remote get-url origin": RuntimeError("no git")})
    assert runner_health.orbi_repo_from_deploy_home(tmp_path, fake) is None


def test_config_caused_crash_loop_is_non_dispatchable(tmp_path):
    """Issue #345: a config-caused crash loop must NOT create an ai-ready
    task — it is a `bug`-only alert in the orbi repo naming the cause."""
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    journal = (
        f"{CRASH_LINE}\n{CONFIG_REASON_LINE}\n{CRASH_LINE}\n{CRASH_LINE}\n"
    )
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": journal,
        "journalctl --user -u orbi@2.service": "",
        **origin_route(),
        'in:body "orbi-health-fingerprint:': "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == ["crash_loop"]
    creates = fake.commands("gh issue create")
    assert len(creates) == 1
    create = creates[0]
    # Routes to the orbi repo, NOT the delivery repo.
    assert create[create.index("--repo") + 1] == ORBI_REPO
    # Non-dispatchable: `bug` only, NO `ai-ready`.
    assert "bug" in create
    assert "ai-ready" not in create
    body = create[create.index("--body") + 1]
    assert "OPENCODE_API_KEY" in body  # the config cause is named
    assert "ai-ready 流程" not in body


def test_bug_caused_crash_loop_is_dispatchable_with_reason(tmp_path):
    """Issue #345: a Runner-bug crash loop produces a bug+ai-ready issue in
    the orbi repo carrying the journal root cause."""
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    journal = (
        f"{CRASH_LINE}\n{BUG_REASON_LINE}\n{CRASH_LINE}\n{CRASH_LINE}\n"
    )
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": journal,
        "journalctl --user -u orbi@2.service": "",
        **origin_route(),
        'in:body "orbi-health-fingerprint:': "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path), run_command=fake,
    )
    assert alerts == ["crash_loop"]
    create = fake.commands("gh issue create")[0]
    assert create[create.index("--repo") + 1] == ORBI_REPO
    assert "bug" in create and "ai-ready" in create
    body = create[create.index("--body") + 1]
    assert BUG_REASON_LINE in body  # the journal root cause is in the body
    assert "crash reason fingerprint:" in body


def test_health_alert_repo_override_wins(tmp_path):
    """Issue #345: the configured override routes the alert and skips the
    deploy-home origin lookup (fork/private deployments)."""
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    journal = f"{CRASH_LINE}\n{CRASH_LINE}\n{CRASH_LINE}\n"
    override = "fork-owner/orbi-fork"
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": journal,
        "journalctl --user -u orbi@2.service": "",
        'in:body "orbi-health-fingerprint:': "[]",
    })
    alerts = runner_health.run_health_check(
        make_config(tmp_path, health_alert_repo=override), run_command=fake,
    )
    assert alerts == ["crash_loop"]
    create = fake.commands("gh issue create")[0]
    assert create[create.index("--repo") + 1] == override
    # No origin lookup was needed.
    assert fake.commands("remote get-url origin") == []


def test_undeterminable_alert_repo_skips_the_issue(tmp_path, caplog):
    """Issue #345: when the orbi repo cannot be determined the alert is
    skipped (bypass) — never filed in the delivery repo, never guessed."""
    write_state(tmp_path, {"runs": [], "last_pickup_ts": time.time(),
                           "alerted": []})
    journal = f"{CRASH_LINE}\n{CRASH_LINE}\n{CRASH_LINE}\n"
    fake = FakeRunCommand({
        "journalctl --user -u orbi@1.service": journal,
        "journalctl --user -u orbi@2.service": "",
        "remote get-url origin": "",
    })
    with caplog.at_level("INFO"):
        alerts = runner_health.run_health_check(
            make_config(tmp_path), run_command=fake,
        )
    assert alerts == []
    assert fake.commands("gh issue create") == []
    assert "health_alert_repo_undetermined" in caplog.text


# ---------------------------------------------------------------------------
# process_issue recording hooks: pure bypass (never fail the delivery)
# ---------------------------------------------------------------------------

def test_process_issue_pickup_record_failure_is_bypass(
    monkeypatch, tmp_path, caplog,
):
    """Issue #266: a health-state write failure at claim time must never
    fail the delivery (bypass) — the claim and the run go on."""
    import orbi.runner as runner
    from tests.test_bootstrap_runner import _gh_api
    from tests.test_progress_wiring import make_fake_gh

    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "create_worktree", lambda *args, **kwargs: tmp_path / "wt",
    )
    monkeypatch.setattr(runner, "run_pi", lambda *args, **kwargs: "done")
    monkeypatch.setattr(
        runner, "deliver_pr",
        lambda *args, **kwargs: "https://github.com/muyantech/orbi/pull/4",
    )
    monkeypatch.setattr(
        runner, "comment_issue", lambda *args, **kwargs: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_pickup",
        lambda repo_dir: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        pr_url = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
             "base_branch": "main"},
            "xqliu/muyan-ceo",
        )
    assert pr_url == "https://github.com/muyantech/orbi/pull/4"
    assert "health_pickup_record_failed" in caplog.text


def test_process_issue_failure_record_failure_is_bypass(
    monkeypatch, tmp_path, caplog,
):
    """Issue #266: a health-state write failure in the terminal failure
    path must never change the delivery outcome (bypass) — the Issue is
    still marked ai-blocked and the tick ends cleanly."""
    import orbi.runner as runner
    from tests.test_bootstrap_runner import _gh_api
    from tests.test_progress_wiring import make_fake_gh

    monkeypatch.setattr(runner, "edit_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "freeze_base", lambda repo_dir, base_branch: "abc123def456",
    )
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(
        runner, "create_worktree",
        Mock(side_effect=RuntimeError("git failed")),
    )
    monkeypatch.setattr(
        runner, "activity_snapshot", lambda session_dir: None,
    )
    gh_calls, posted = make_fake_gh(monkeypatch)

    def fake_run(command, **kwargs):
        if command[:2] == ["gh", "api"]:
            return _gh_api(command, posted)
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        return ""

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(
        runner_health, "record_run_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state dir read-only"),
        ),
    )
    with caplog.at_level("INFO"):
        pr_url = runner.process_issue(
            {"number": 4, "title": "Fix", "body": "Body"},
            {"repo_dir": tmp_path, "prompt": tmp_path / "prompt.md",
             "base_branch": "main"},
            "xqliu/muyan-ceo",
        )
    assert pr_url is None
    assert "health_failure_record_failed" in caplog.text


# ---------------------------------------------------------------------------
# main() wiring: the check runs every tick and is a pure bypass
# ---------------------------------------------------------------------------

def idle_tick_fake_run() -> FakeRunCommand:
    """A run_command fake for a full `main()` tick with an empty queue."""
    return FakeRunCommand({"gh issue list": "[]"})


def test_main_runs_the_health_check_every_tick(monkeypatch, tmp_path):
    import orbi.runner as runner

    _write_config(tmp_path)
    calls: list[dict] = []

    def recording_health_check(config, *, run_command):
        calls.append(config)
        return []

    monkeypatch.setattr(
        runner_health, "run_health_check", recording_health_check,
    )
    monkeypatch.setattr(runner, "run_command", idle_tick_fake_run())
    assert runner.main(["--config", str(tmp_path / "orbi.toml")]) == 0
    assert len(calls) == 1
    assert calls[0]["repo_dir"] == tmp_path / "orbi"


def test_main_health_check_failure_never_fails_the_tick(
    monkeypatch, tmp_path, caplog,
):
    import orbi.runner as runner

    _write_config(tmp_path)

    def exploding_health_check(config, *, run_command):
        raise RuntimeError("journalctl missing")

    monkeypatch.setattr(
        runner_health, "run_health_check", exploding_health_check,
    )
    monkeypatch.setattr(runner, "run_command", idle_tick_fake_run())
    with caplog.at_level("INFO"):
        assert runner.main(["--config", str(tmp_path / "orbi.toml")]) == 0
    assert "health_check_failed" in caplog.text
    assert "no_ready_issue" in caplog.text


def _write_config(tmp_path: Path) -> None:
    repo_dir = tmp_path / "orbi"
    repo_dir.mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / name).write_text("prompt", encoding="utf-8")
    (tmp_path / "orbi.toml").write_text(
        'source_repos = ["owner/repo"]\n'
        f'repo_dir = "{repo_dir}"\n',
        encoding="utf-8",
    )
