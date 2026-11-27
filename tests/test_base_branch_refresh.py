"""Issue #2462: the claim reads `base_branch` from the config at CLAIM time.

`runner.main` loads `orbi.toml` once per process, then picks the next Issue —
which can take minutes on a busy queue. An operator changes `base_branch` in
that window without restarting the runner, so the tick re-reads the
base-branch inputs right after the claim: the Issue claimed after the change
must be delivered on the NEW base — its delivery branch cut from it and its PR
opened against it.

The scene is the real user path end to end: a real bare origin plus a clone
carrying both candidate bases, the real `runner.main` tick, the shared
`FakeGh` in-memory GitHub, and the fake `pi` executable the e2e suites use.
The config file is rewritten DURING the claim scan — after the tick started and
before the Issue is claimed — which is exactly the production window.

The failure path is the other half of the contract: a config that no longer
loads at claim time ends the tick with the structured `config_invalid` line
and exit 1 BEFORE anything is claimed on GitHub (no `ai-in-progress`, no
worktree, no PR), instead of delivering on a base nobody asked for.
"""
import logging
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner
from orbi.delivery_labels import IN_PROGRESS_LABEL, READY_LABEL
from tests.fakes.bootstrap_delivery import install_claim_time_config_seam
from tests.fakes.github import FakeGh, command_flags
from tests.fakes.pi import install_fake_pi

from conftest import git


REPO = "owner/repo"
ISSUE_NUMBER = 9
BRANCH = f"orbi/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}"

# The fake agent executable the runner really starts: it delivers one
# committed source file in its worktree, the exact commit boundary the
# Runner's closeout requires (Issue #186).
FAKE_PI = """#!/usr/bin/env python3
import os, subprocess
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".orbi"), exist_ok=True)
with open(os.path.join(cwd, "delivery.txt"), "w", encoding="utf-8") as handle:
    handle.write("delivered")
for command in (["git", "add", "."], ["git", "commit", "-m", "delivery"]):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
"""


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """A local bare origin plus a clone carrying bases `A` and `B`.

    `B` sits one commit past `A`, so the delivery branch's FIRST parent
    names which base the worktree was cut from.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    git(clone, "config", "user.email", "pilot@test.local")
    git(clone, "config", "user.name", "Pilot")
    (clone / "base.txt").write_text("base A\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base A")
    git(clone, "branch", "A")
    (clone / "new-base.txt").write_text("base B\n", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "base B")
    git(clone, "branch", "B")
    git(clone, "push", "origin", "A", "B")
    return clone


def _write_config(tmp_path: Path, base_branch: str) -> Path:
    """Write the deployment config the tick reads at start and at claim."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "prompt.md").write_text(
        "Deliver the Issue.\n", encoding="utf-8",
    )
    (prompts / "prompt_review.md").write_text("review", encoding="utf-8")
    path = tmp_path / "orbi.toml"
    path.write_text(
        f'source_repos = ["{REPO}"]\n'
        f'base_branch = "{base_branch}"\n'
        'repo_dir = "clone"\n'
        "max_concurrency = 1\n",
        encoding="utf-8",
    )
    return path


def _make_issue(fake: FakeGh) -> None:
    fake.add_issue(
        ISSUE_NUMBER, title="ship the change", body="body",
        labels=(READY_LABEL,),
    )


def test_tick_delivers_the_issue_claimed_after_the_base_branch_changes(
    allow_external_commands, clone, tmp_path, monkeypatch,
):
    """Issue #2462 acceptance: config `A` at tick start, rewritten to `B`
    while the tick is claiming — the delivery branch is cut from `B` and the
    PR is opened against `B`."""
    base_a = git(clone, "rev-parse", "A")
    base_b = git(clone, "rev-parse", "B")
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    fake = FakeGh(REPO)
    _make_issue(fake)
    config = _write_config(tmp_path, "A")
    state = install_claim_time_config_seam(
        monkeypatch, fake, lambda: _write_config(tmp_path, "B"),
    )

    assert runner.main(["--config", str(config)]) == 0

    # The tick started on A and the rewrite landed inside the claim window.
    assert state["rewritten"] is True
    assert base_a != base_b
    # The delivery branch is cut from B: its one delivery commit sits
    # directly on origin/B (on origin/A it would answer base_a).
    assert git(clone, "rev-parse", f"{BRANCH}^") == base_b
    # The PR is opened against B and IS the delivery's verified PR.
    assert len(state["creates"]) == 1
    assert command_flags(state["creates"][0])["--base"] == ["B"]
    assert fake.prs[1]["baseRefName"] == "B"
    assert fake.prs[1]["headRefName"] == BRANCH
    # The Issue was claimed and delivered, not left in progress.
    assert IN_PROGRESS_LABEL not in fake.issues[ISSUE_NUMBER]["labels"]


def test_tick_stops_before_delivering_when_the_claim_time_config_is_invalid(
    allow_external_commands, clone, tmp_path, monkeypatch, caplog,
):
    """Failure path (Issue #2462): a config that no longer loads at claim time
    ends the tick with the structured `config_invalid` line and exit 1 before
    anything is claimed on GitHub — no label, no worktree, no PR."""
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    fake = FakeGh(REPO)
    _make_issue(fake)
    config = _write_config(tmp_path, "A")

    def break_the_config() -> None:
        config.write_text("base_branch = [unclosed\n", encoding="utf-8")

    state = install_claim_time_config_seam(
        monkeypatch, fake, break_the_config,
    )
    with caplog.at_level(logging.ERROR):
        assert runner.main(["--config", str(config)]) == 1
    assert state["rewritten"] is True
    assert "config_invalid" in caplog.text
    assert "Invalid value (at line 1, column 16)" in caplog.text
    assert state["creates"] == []
    assert IN_PROGRESS_LABEL not in fake.issues[ISSUE_NUMBER]["labels"]
    assert not (clone / ".worktrees").exists()
