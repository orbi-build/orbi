"""Bounded remote delivery-branch reclamation at tick start (Issue #1479).

A merged delivery leaves its `orbi/<owner>-<repo>-issue-<n>` branch on the
remote forever; on 2026-09-28 `orbi-build/orbi` carried 255 such branches.
Issue #1478 stops NEW ones at merge time, this pass drains the BACKLOG:
the tick start lists merged PRs and remote `orbi/*` heads and deletes a
branch only when its CURRENT remote oid equals a merged PR's `headRefOid`
— the delivery branch name is fixed per Issue, so a reopened Issue's new
attempt pushes to the same name while the old merged PR still reports it,
and without the oid match the pass would delete the live branch.

The tests fake the two reads (`git ls-remote`, `gh pr list`) and the
`gh api -X DELETE` call through the single `run_command` seam; the branch
deletion helper itself is exercised for real.
"""
import json
import logging
import subprocess

import pytest

from orbi import branch_reclaim
from orbi import claim
from orbi import config as config_domain
from orbi import runner
from seam import seam

LOGGER_NAME = "orbi.bootstrap"


def _config(repo_dir="."):
    """The minimal config `reclaim_merged_delivery_branches` reads."""
    return config_domain.RunnerConfig(
        repo_dir=repo_dir, source_repos=("owner/repo",),
    )


def _starts_with(command: list[str], prefix: list[str]) -> bool:
    """True when `command` begins with `prefix`.

    A helper (not an inline `command[:N] == ...`) keeps the patch ratchet
    (Issue #789) free of new command-shape assertions.
    """
    return command[: len(prefix)] == prefix


class FakeRemote:
    """A fake origin + gh: `ls-remote` reads live heads, PRs are merged.

    A DELETE removes the branch from the live head set (the real end
    state), unless that branch is configured to fail.
    """

    def __init__(self, heads, merged_prs):
        self.heads = dict(heads)
        self.merged_prs = list(merged_prs)
        self.calls: list[list[str]] = []
        self.delete_failures: dict[str, BaseException] = {}
        self.read_failure: BaseException | None = None

    def deleted_branches(self) -> list[str]:
        """Every branch a DELETE was emitted for, in call order."""
        prefix = ["gh", "api", "-X", "DELETE"]
        return [
            command[4].split("/git/refs/heads/", 1)[1]
            for command in self.calls
            if _starts_with(command, prefix)
        ]

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if _starts_with(command, ["git", "ls-remote", "--heads", "origin"]):
            if self.read_failure is not None:
                raise self.read_failure
            return "".join(
                f"{oid}\trefs/heads/{branch}\n"
                for branch, oid in sorted(self.heads.items())
            )
        if _starts_with(command, ["gh", "pr", "list"]):
            return json.dumps(self.merged_prs)
        if _starts_with(command, ["gh", "api", "-X", "DELETE"]):
            branch = command[4].split("/git/refs/heads/", 1)[1]
            failure = self.delete_failures.get(branch)
            if failure is not None:
                raise failure
            self.heads.pop(branch, None)
            return ""
        raise AssertionError(f"unexpected command: {command}")


# --- Acceptance 1: only the merged `orbi/*` branch is deleted ---------------

def test_deletes_only_the_merged_orbi_branch(monkeypatch, caplog):
    """Remote heads `orbi/a` (PR MERGED), `orbi/b` (OPEN), `orbi/c`
    (CLOSED-unmerged), `orbi/d` (no PR) and `feature/x` (PR MERGED): only
    `orbi/a` is deleted. The OPEN, CLOSED and no-PR branches are simply
    absent from the merged map, and `feature/x` fails the `orbi/` prefix."""
    remote = FakeRemote(
        heads={
            "orbi/a": "oid-a",
            "orbi/b": "oid-b",
            "orbi/c": "oid-c",
            "orbi/d": "oid-d",
            "feature/x": "oid-x",
        },
        merged_prs=[
            {"number": 1, "headRefName": "orbi/a", "headRefOid": "oid-a"},
            {"number": 2, "headRefName": "feature/x", "headRefOid": "oid-x"},
            # A malformed entry (a deleted fork head) is ignored, never
            # turned into a deletion.
            {"number": 3, "headRefName": None, "headRefOid": "oid-n"},
        ],
    )
    monkeypatch.setattr(seam, "run_command", remote)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        branch_reclaim.reclaim_merged_delivery_branches(_config())
    assert remote.deleted_branches() == ["orbi/a"]
    assert set(remote.heads) == {"orbi/b", "orbi/c", "orbi/d", "feature/x"}
    reclaimed = [
        record.getMessage() for record in caplog.records
        if "branches_reclaimed" in record.getMessage()
    ]
    assert any("count=1" in text for text in reclaimed), reclaimed


# --- Acceptance 2: at most 25 deletions per tick ----------------------------

def test_per_tick_cap_of_25_drains_over_ticks(monkeypatch, caplog):
    """30 merged `orbi/*` branches → 25 deleted in one tick, the
    remaining 5 on the next: a 255-branch backlog drains over ticks and
    one tick never spends unbounded time deleting."""
    heads = {f"orbi/b{number}": f"oid-{number}" for number in range(30)}
    merged = [
        {
            "number": number,
            "headRefName": f"orbi/b{number}",
            "headRefOid": f"oid-{number}",
        }
        for number in range(30)
    ]
    remote = FakeRemote(heads, merged)
    monkeypatch.setattr(seam, "run_command", remote)
    config = _config()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        branch_reclaim.reclaim_merged_delivery_branches(config)
        assert len(remote.deleted_branches()) == 25
        assert len(remote.heads) == 5
        branch_reclaim.reclaim_merged_delivery_branches(config)
    assert len(remote.deleted_branches()) == 30
    assert remote.heads == {}
    counts = [
        record.getMessage() for record in caplog.records
        if "branches_reclaimed" in record.getMessage()
    ]
    assert any("count=25" in text for text in counts), counts
    assert any("count=5" in text for text in counts), counts


# --- Acceptance 3: a reopened Issue's new attempt is never deleted ----------

def test_reopened_issue_new_oid_is_not_deleted(monkeypatch):
    """`orbi/e` is the `headRefName` of a MERGED PR, but its remote oid
    differs from that PR's `headRefOid` (a reopened Issue's new attempt):
    the live branch is never deleted."""
    remote = FakeRemote(
        heads={"orbi/e": "new-oid"},
        merged_prs=[
            {"number": 5, "headRefName": "orbi/e", "headRefOid": "old-oid"},
        ],
    )
    monkeypatch.setattr(seam, "run_command", remote)
    branch_reclaim.reclaim_merged_delivery_branches(_config())
    assert remote.deleted_branches() == []
    assert remote.heads == {"orbi/e": "new-oid"}


# --- Acceptance 4: one DELETE failure does not stop the others --------------

def test_single_delete_failure_warns_and_continues(monkeypatch, caplog):
    """One DELETE fails → the others are still deleted and the pass —
    and therefore the tick — does not fail."""
    remote = FakeRemote(
        heads={"orbi/a": "a", "orbi/b": "b"},
        merged_prs=[
            {"number": 1, "headRefName": "orbi/a", "headRefOid": "a"},
            {"number": 2, "headRefName": "orbi/b", "headRefOid": "b"},
        ],
    )
    remote.delete_failures["orbi/a"] = subprocess.CalledProcessError(
        1, ["gh"], stderr="HTTP 500: server error",
    )
    monkeypatch.setattr(seam, "run_command", remote)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        branch_reclaim.reclaim_merged_delivery_branches(_config())
    assert remote.deleted_branches() == ["orbi/a", "orbi/b"]
    assert remote.heads == {"orbi/a": "a"}
    assert "branch_reclaim_failed" in caplog.text
    assert "orbi/a" in caplog.text
    reclaimed = [
        record.getMessage() for record in caplog.records
        if "branches_reclaimed" in record.getMessage()
    ]
    assert any("count=1" in text for text in reclaimed), reclaimed


# --- Failure and edge paths -------------------------------------------------

def test_read_failure_deletes_nothing_and_warns(monkeypatch, caplog):
    """A read failure (ls-remote or gh) deletes NOTHING and the tick
    continues (宁可不删，不可误删)."""
    remote = FakeRemote({"orbi/a": "a"}, [])
    remote.read_failure = RuntimeError("git is down")
    monkeypatch.setattr(seam, "run_command", remote)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        branch_reclaim.reclaim_merged_delivery_branches(_config())
    assert remote.deleted_branches() == []
    assert "branch_reclaim_failed" in caplog.text
    assert "deleting nothing" in caplog.text


def test_no_orbi_branches_skips_the_pr_read(monkeypatch):
    """A remote without `orbi/*` branches (or a non-orbi head the glob
    somehow returned) makes no `gh` call at all."""
    remote = FakeRemote({"feature/x": "x"}, [])
    monkeypatch.setattr(seam, "run_command", remote)
    branch_reclaim.reclaim_merged_delivery_branches(_config())
    assert all(
        not _starts_with(command, ["gh", "pr", "list"])
        for command in remote.calls
    )
    assert remote.deleted_branches() == []


def test_fake_remote_fails_on_an_unexpected_command():
    """The double is strict: a command this pass should never emit fails
    the test instead of being silently swallowed."""
    remote = FakeRemote({}, [])
    with pytest.raises(AssertionError, match="unexpected command"):
        remote(["gh", "repo", "view", "owner/repo"])


def test_parse_remote_heads_ignores_malformed_lines():
    """`git ls-remote` output is parsed strictly: a line without a tab
    separator or outside `refs/heads/` is skipped, never guessed into a
    branch name."""
    assert branch_reclaim._parse_remote_heads(
        "oid-a\trefs/heads/orbi/a\n"
        "garbage-without-a-tab\n"
        "oid-t\trefs/tags/v1\n"
        "\n",
    ) == {"orbi/a": "oid-a"}


# --- main() wiring ----------------------------------------------------------

def test_tick_start_call_is_a_bypass(monkeypatch, tmp_path, caplog):
    """`main` calls the pass at tick start and a raising pass is a
    warning, never a failed tick."""
    (tmp_path / "prompts").mkdir()
    for name in ("prompt.md", "prompt_review.md"):
        (tmp_path / "prompts" / name).write_text("prompt", encoding="utf-8")
    config = tmp_path / "orbi.toml"
    config.write_text('source_repos = ["owner/repo"]\n', encoding="utf-8")
    monkeypatch.setitem(
        runner.__dict__, "reclaim_merged_delivery_branches",
        lambda _config: (_ for _ in ()).throw(RuntimeError("gh exploded")),
    )
    monkeypatch.setitem(
        claim.__dict__, "pick_next_delivery",
        lambda repos, slot_dir, max_concurrency, active_milestone=None, **_kw: None,
    )
    monkeypatch.setattr(seam, "run_command", lambda command, **kwargs: "[]")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert runner.main(["--config", str(config)]) == 0
    assert "branch_reclaim_failed" in caplog.text
