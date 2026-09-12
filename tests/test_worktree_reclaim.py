"""Bounded task-worktree reclamation at tick start (Issue #760).

The worktrees only ever grew: every delivered or failed run left its
`repo_dir/.worktrees/orbi-{slug}-issue-{N}-{run_id}` directory behind and
nothing ever removed it (275 worktrees / 18.7G across the three dogfood
deployments, 95% for Issues closed long ago). Issue #760 adds ONE bounded,
idempotent, never-fatal reclamation pass at the tick start, beside the
unit-drift check: a registered task worktree whose Issue is CLOSED and past
the retention window is `git worktree remove --force`d — never an open
Issue's worktree, never an in-flight run's scene, and a failure is a
warning, never a failed tick.

The tests drive REAL git repositories with REAL registered worktrees (the
`git worktree remove` path is the product path); only the GitHub read
(`list_issues`) and the clock are faked.
"""
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orbi import runner
import orbi.journal as journal
from orbi.delivery_labels import (
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
)
from seam import seam

LOGGER_NAME = "orbi.bootstrap"
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


def _git_repo(path: Path) -> Path:
    """A real Git checkout with one commit (worktree add needs a HEAD)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("repo", encoding="utf-8")
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(
        ["git", *identity, "add", "README.md"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", *identity, "commit", "-q", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _register(repo: Path, name: str, content: str = "x") -> Path:
    """Register one detached worktree with an orbi-style (or any) name."""
    worktrees = repo / ".worktrees"
    worktrees.mkdir(exist_ok=True)
    target = worktrees / name
    subprocess.run(
        ["git", "worktree", "add", "--detach", "-q", str(target)],
        cwd=repo, check=True, capture_output=True,
    )
    (target / "data.txt").write_text(content, encoding="utf-8")
    return target


def _registered(repo: Path) -> list[str]:
    """The repo's registered worktree paths, straight from git."""
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]


def _config(repo: Path) -> dict:
    """The minimal config `reclaim_released_worktrees` reads."""
    return {
        "repo_dir": repo,
        "source_repos": ["owner/repo"],
        "worktree_retain_hours": 72,
    }


def _closed(number: int, hours_ago: float, labels=()) -> dict:
    """One closed Issue in the exact `gh issue list --json` shape
    (verified live: closedAt is a UTC ISO-8601 `Z` timestamp)."""
    closed_at = (NOW - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    return {
        "number": number,
        "closedAt": closed_at,
        "labels": [{"name": name} for name in labels],
    }


def _stub_closed(monkeypatch, *issues: dict) -> None:
    """Stub the batched GitHub read; any repo answers with these Issues."""

    def fake_list_issues(repo, *, state, json_fields, limit, **kwargs):
        assert state == "closed"
        assert "closedAt" in json_fields and "labels" in json_fields
        return list(issues)

    monkeypatch.setattr(seam, "list_issues", fake_list_issues)


def test_reclaims_closed_issue_worktree_past_window(tmp_path, monkeypatch, caplog):
    """The core scene: a delivered run's worktree whose Issue closed 100h
    ago (window 72h) is removed; the journal carries the structured
    `worktree_reclaimed count=N freed=<bytes>` line."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-9-abcd1234")
    _stub_closed(monkeypatch, _closed(9, hours_ago=100))
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) not in _registered(repo)
    assert not worktree.exists()
    line = [
        record.getMessage() for record in caplog.records
        if "worktree_reclaimed" in record.getMessage()
    ]
    assert any("count=1" in text and "freed=" in text for text in line), line


def test_journal_line_reports_bytes_freed(tmp_path, monkeypatch, caplog):
    """`freed=` is the bytes of the removed worktree, not a placeholder."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-9-abcd1234")
    # A dangling symlink stats as OSError inside the size walk: the size
    # is journal metadata, never a gate — the removal still happens.
    (worktree / "dangling.lnk").symlink_to(worktree / "missing-target")
    _stub_closed(monkeypatch, _closed(9, hours_ago=100))
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        runner.reclaim_released_worktrees(_config(repo), now=NOW)
    freed = next(
        int(text.split("freed=")[1].split()[0])
        for record in caplog.records
        if "worktree_reclaimed" in (text := record.getMessage())
    )
    assert freed >= 10
    assert not worktree.exists()


def test_keeps_open_issue_worktree(tmp_path, monkeypatch):
    """Acceptance: an OPEN Issue's worktree is NEVER deleted — the Issue
    is absent from the closed map, so the scene stays for its run."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-10-abcd1234")
    _stub_closed(monkeypatch)  # no closed Issues at all
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


def test_keeps_worktree_within_retention_window(tmp_path, monkeypatch):
    """An Issue closed 1h ago stays far inside the 72h window: keep the
    scene for post-incident inspection (the Issue's explicit demand)."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-11-abcd1234")
    _stub_closed(monkeypatch, _closed(11, hours_ago=1))
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


def test_keeps_worktree_of_this_process_active_run(tmp_path, monkeypatch):
    """The Issue's run_id criterion: a worktree held by THIS process's
    bound run is never reclaimed, even when its Issue reads closed."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-12-abcd1234")
    _stub_closed(monkeypatch, _closed(12, hours_ago=100))
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", "abcd1234")
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


def test_keeps_worktree_bound_as_active_scene(tmp_path, monkeypatch):
    """The stop-scene binding (Issue #48) is a second active-run guard:
    the exact worktree path of the in-flight delivery is never reclaimed
    (an external takeover checks out a path whose name is not derived
    from the run id)."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-12-abcd1234")
    _stub_closed(monkeypatch, _closed(12, hours_ago=100))
    monkeypatch.setattr(journal, "_ACTIVE_RUN", {"worktree": str(worktree)},
    )
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


@pytest.mark.parametrize("label", [IN_PROGRESS_LABEL, PR_OPENED_LABEL,
                                   FIX_NEEDED_LABEL])
def test_keeps_issue_closed_mid_run_with_inflight_label(
    tmp_path, monkeypatch, label,
):
    """A human closed the Issue while a run was still in flight: the
    Issue stays closed but wears an in-flight label, and the run's scene
    must survive until the delivery path resolves it."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-13-abcd1234")
    _stub_closed(
        monkeypatch, _closed(13, hours_ago=100, labels=[label]),
    )
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


def test_skips_unrecognized_names_and_unconfigured_slugs(
    tmp_path, monkeypatch,
):
    """Only orbi's own naming for the CONFIGURED source repos is ever
    touched: a foreign worktree and an old-slug worktree (a repo renamed
    long ago, Issue #219) stay."""
    repo = _git_repo(tmp_path / "repo")
    foreign = _register(repo, "my-own-worktree")
    old_slug = _register(repo, "orbi-owner-oldrepo-issue-9-abcd1234")
    _stub_closed(monkeypatch, _closed(9, hours_ago=100))
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(foreign) in _registered(repo)
    assert str(old_slug) in _registered(repo)


def test_same_issue_number_in_two_source_repos_is_not_crossed(
    tmp_path, monkeypatch,
):
    """The closed-Issue read is matched per (source repo, number), never
    by number alone: two configured repos can carry the same Issue
    number, and one repo's closed #9 must never answer for the other
    repo's still-open #9 (an open Issue's scene is never deleted)."""
    repo = _git_repo(tmp_path / "repo")
    closed_one = _register(repo, "orbi-owner-one-issue-9-aaaaaaaa")
    open_two = _register(repo, "orbi-owner-two-issue-9-bbbbbbbb")

    def fake_list_issues(repo, *, state, json_fields, limit, **kwargs):
        assert state == "closed"
        if repo == "owner/one":
            return [_closed(9, hours_ago=100)]
        assert repo == "owner/two"
        return []

    monkeypatch.setattr(seam, "list_issues", fake_list_issues)
    config = _config(repo)
    config["source_repos"] = ["owner/one", "owner/two"]
    runner.reclaim_released_worktrees(config, now=NOW)
    assert str(closed_one) not in _registered(repo)
    assert str(open_two) in _registered(repo)


def test_read_failure_removes_nothing_and_warns(tmp_path, monkeypatch, caplog):
    """The GitHub read is the safe direction: when it fails, NOTHING is
    removed and the tick continues (宁可不删，不可误删)."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-9-abcd1234")

    def failing_list_issues(repo, **kwargs):
        raise RuntimeError("gh is down")

    monkeypatch.setattr(seam, "list_issues", failing_list_issues)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)
    assert any(
        "worktree_reclaim_failed" in record.getMessage()
        for record in caplog.records
    )


def test_single_removal_failure_warns_and_continues(
    tmp_path, monkeypatch, caplog,
):
    """One unremovable worktree (permissions, occupied) is a warning with
    the path; the remaining candidates are still reclaimed and the tick
    continues."""
    repo = _git_repo(tmp_path / "repo")
    stuck = _register(repo, "orbi-owner-repo-issue-20-11111111")
    other = _register(repo, "orbi-owner-repo-issue-21-22222222")
    _stub_closed(
        monkeypatch, _closed(20, hours_ago=100), _closed(21, hours_ago=90),
    )
    real_run_command = runner.run_command

    def run_command(command, **kwargs):
        if command[:3] == ["git", "worktree", "remove"] \
                and command[-1] == str(stuck):
            raise RuntimeError("boom")
        return real_run_command(command, **kwargs)

    monkeypatch.setattr(seam, "run_command", run_command)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(stuck) in _registered(repo)
    assert str(other) not in _registered(repo)
    failure = [
        record.getMessage() for record in caplog.records
        if "worktree_reclaim_failed" in record.getMessage()
    ]
    assert any(str(stuck) in text and "boom" in text for text in failure)
    reclaimed = [
        record.getMessage() for record in caplog.records
        if "worktree_reclaimed" in record.getMessage()
    ]
    assert any("count=1" in text for text in reclaimed)


def test_per_tick_cap_reclaims_oldest_closed_first(tmp_path, monkeypatch):
    """The pass is bounded: at most 25 removals per tick, oldest-closed
    first, so a 275-worktree backlog drains over ticks deterministically
    and one tick can never spend unbounded time on `rm -rf`."""
    repo = _git_repo(tmp_path / "repo")
    kept = {}
    for number in range(1, 28):
        name = f"orbi-owner-repo-issue-{number}-{number:08x}"
        kept[number] = _register(repo, name)
    _stub_closed(
        monkeypatch, *(
            # Issue 1 closed longest ago ... Issue 27 closed most recently.
            _closed(number, hours_ago=200 - number * 5)
            for number in range(1, 28)
        ),
    )
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    registered = _registered(repo)
    removed = [
        number for number in range(1, 28)
        if str(kept[number]) not in registered
    ]
    assert removed == list(range(1, 26))


@pytest.mark.parametrize(
    "closed_at",
    [None, "2026-06-01T00:00:00"],  # missing / naive (no timezone anchor)
    ids=["missing", "naive"],
)
def test_keeps_worktree_with_unusable_closedat(
    tmp_path, monkeypatch, caplog, closed_at,
):
    """A closed Issue without a parsable or timezone-anchored closedAt
    has no trustworthy window anchor: skip it (never guess)."""
    repo = _git_repo(tmp_path / "repo")
    worktree = _register(repo, "orbi-owner-repo-issue-14-abcd1234")
    _stub_closed(
        monkeypatch,
        {"number": 14, "closedAt": closed_at, "labels": []},
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert str(worktree) in _registered(repo)


def test_noop_without_worktrees_directory(tmp_path, monkeypatch):
    """A deployment without any worktrees makes no GitHub read at all."""
    repo = _git_repo(tmp_path / "repo")
    called = []
    monkeypatch.setattr(seam, "list_issues",
        lambda *a, **k: called.append(a) or [],
    )
    runner.reclaim_released_worktrees(_config(repo), now=NOW)
    assert called == []


# --- Issue #760: the retention window is configurable ------------------------

def test_load_config_defaults_worktree_retain_hours_to_72(tmp_path):
    """Omitted -> 72 hours: the Issue's conservative option — a closed
    Issue's scene stays inspectable for three days before reclamation."""
    config_path = tmp_path / "orbi.toml"
    config_path.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    assert runner.load_config(config_path)["worktree_retain_hours"] == 72.0


def test_load_config_reads_explicit_worktree_retain_hours(tmp_path):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\nworktree_retain_hours = 24\n',
        encoding="utf-8",
    )
    assert runner.load_config(config_path)["worktree_retain_hours"] == 24.0


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("true", "not a boolean"),
        ("0", "positive"),
        ("-1", "positive"),
        ("nan", "finite"),
        ("inf", "finite"),
        ('"72"', "number"),
    ],
)
def test_load_config_rejects_invalid_worktree_retain_hours(
    tmp_path, value, reason,
):
    config_path = tmp_path / "orbi.toml"
    config_path.write_text(
        'source_repos = ["owner/repo"]\n'
        f"worktree_retain_hours = {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        runner.load_config(config_path)
    assert "worktree_retain_hours" in str(excinfo.value)
    assert reason in str(excinfo.value)


# --- Issue #760: the preflight wiring (beside the unit-drift check) ----------

def _preflight_config(tmp_path) -> dict:
    """The keys `_preflight` itself reads (every gate it calls is stubbed
    by conftest or by the test)."""
    return {
        "source_repos": ["owner/repo"],
        "active_milestone": None,
        "repo_dir": tmp_path,
        "deploy_home": tmp_path,
        "git_transport": "ssh",
        "max_concurrency": 1,
    }


def test_preflight_runs_the_reclamation(tmp_path, monkeypatch):
    """The tick start calls the reclamation with the effective config,
    beside `check_unit_drift` (Issue #760: NOT a manual command)."""
    config = _preflight_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        runner, "reclaim_released_worktrees",
        lambda config: calls.append(config),
    )
    monkeypatch.setattr(runner, "sync_active_milestone_variable",
                        lambda *a, **k: None)
    runner._preflight(config)
    assert calls == [config]


def test_preflight_reclaim_failure_never_fails_the_start(
    tmp_path, monkeypatch, caplog,
):
    """The reclamation is a pure bypass (Issue #79 discipline): a failure
    logs `worktree_reclaim_failed` and the tick start continues."""
    config = _preflight_config(tmp_path)

    def failing_reclaim(config):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(runner, "reclaim_released_worktrees", failing_reclaim)
    monkeypatch.setattr(runner, "sync_active_milestone_variable",
                        lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        runner._preflight(config)
    assert any(
        "worktree_reclaim_failed" in record.getMessage()
        for record in caplog.records
    )
