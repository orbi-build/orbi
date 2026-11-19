"""Drafting reads `origin/<base>` at draft start (Issue #2276).

A delivery always builds a fresh worktree from `origin/<base>`, so nothing
moves the deployment checkout: its working tree can be arbitrarily old, and a
draft that reads it reports modules that exist on the base as missing and
cites names the base already renamed. These are the real-git acceptance tests
of the fix: the drafting session's repository view is a freshly fetched
`origin/<base_branch>`, materialized for the length of one draft round and
removed when the round ends, while the checkout and any delivery worktree stay
exactly where they were.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import git

import orbi.draft as draft
import orbi.draft_cli as draft_cli
import orbi.gitops as gitops
from orbi.config import RunnerConfig
from orbi.journal import run_command as real_run_command

from tests.fakes import draft as draft_fakes

REPO = "acme/app"
BASE = "main"

OUTCOME = "## Outcome\n\nA user can tag a note."
ACCEPTANCE = "## Acceptance\n\n- Adding a tag makes the note findable."


def issue(title: str = "Tag a note") -> dict:
    return {"title": title, "body": f"{OUTCOME}\n\n{ACCEPTANCE}"}


def configure(repo: Path) -> None:
    git(repo, "config", "user.email", "pilot@test.local")
    git(repo, "config", "user.name", "Pilot")


@pytest.fixture
def scene(tmp_path: Path) -> SimpleNamespace:
    """A checkout behind `origin/main`, which gained a file and a rename.

    The checkout is cloned at the FIRST commit and never moved afterwards —
    exactly the deployment checkout of the Issue: deliveries build their
    worktrees from `origin/main`, so the checkout itself stays behind.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", BASE)
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    configure(seed)
    (seed / "src").mkdir()
    (seed / "src" / "old_name.py").write_text(
        "SYMBOL = 'stale'\n", encoding="utf-8",
    )
    (seed / "notes.md").write_text("notes\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "first")
    git(seed, "push", "origin", BASE)

    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(origin), str(checkout))
    stale = git(checkout, "rev-parse", "HEAD")
    # A deployment checkout carries the run dir in its local exclude
    # (`orbi setup` pins it), so the draft never shows up in its status.
    gitops.pin_git_exclude(checkout, ".orbi/", run_command=real_run_command)

    # Only the remote base moves: a file is added and a name is changed.
    git(seed, "mv", "src/old_name.py", "src/new_name.py")
    (seed / "src" / "new_name.py").write_text(
        "SYMBOL = 'fresh'\n", encoding="utf-8",
    )
    (seed / "src" / "added.py").write_text("ADDED = True\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "second")
    git(seed, "push", "origin", BASE)
    return SimpleNamespace(
        origin=origin, seed=seed, checkout=checkout, stale=stale,
        fresh=git(seed, "rev-parse", "HEAD"),
    )


def make_config(tmp_path: Path, checkout: Path, *, base: str = BASE,
                source_repos: tuple[str, ...] = (REPO,)) -> RunnerConfig:
    deploy = tmp_path / "deploy"
    rules = deploy / draft.FIDELITY_REL
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("Fidelity: remove what the person did not ask for.",
                     encoding="utf-8")
    return RunnerConfig(
        repo_dir=checkout, deploy_home=deploy, workspace_root=tmp_path,
        source_repos=source_repos, base_branch=base,
    )


def run_round(config: RunnerConfig, session_dir: Path, payload: dict | None = None):
    out, err = io.StringIO(), io.StringIO()
    code = draft_cli.run_draft(
        config, REPO, session_dir=session_dir,
        stdin=io.StringIO(json.dumps(payload or {"message": "notes need tags"})),
        stdout=out, stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


# --- the acceptance: the view is the fetched base, not the checkout ------

def test_the_draft_session_reads_the_fetched_base_not_the_stale_checkout(
    scene, tmp_path, monkeypatch,
):
    sessions = draft_fakes.install_sessions(monkeypatch)
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(issue())
    config = make_config(tmp_path, scene.checkout)

    code, out, err = run_round(config, tmp_path / "session")

    assert code == 0, err
    assert json.loads(out)["check"] == "passed"
    # The checkout really is behind the base the draft had to read.
    assert git(scene.checkout, "rev-parse", "HEAD") == scene.stale
    view = sessions.views[0]
    assert view.files == ["notes.md", "src/added.py", "src/new_name.py"]
    # ... and the session was handed that path, not the checkout.
    assert draft_fakes.repo_view(sessions.calls[0].context) == view.path
    assert view.path != scene.checkout
    # The view lives in the checkout's ignored run dir and is gone again.
    assert view.path.parent == scene.checkout / ".orbi" / "draft-view"
    assert not view.path.exists()
    assert git(scene.checkout, "status", "--porcelain") == ""
    # The checkout itself was never moved: nothing fetched into its HEAD.
    assert git(scene.checkout, "rev-parse", "HEAD") == scene.stale
    assert not (scene.checkout / "src" / "added.py").exists()
    assert (scene.checkout / "src" / "old_name.py").is_file()


# --- a delivery running at the same time --------------------------------

def test_drafting_does_not_disturb_a_delivery_running_at_the_same_time(
    scene, tmp_path, monkeypatch,
):
    delivery = gitops.create_worktree(
        scene.checkout, REPO, 12, "deadbeef", scene.stale,
    )
    (delivery / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    head = git(delivery, "rev-parse", "HEAD")
    sessions = draft_fakes.install_sessions(monkeypatch)
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(issue())
    config = make_config(tmp_path, scene.checkout)

    code, _out, err = run_round(config, tmp_path / "session")

    assert code == 0, err
    # The delivery keeps its own frozen worktree and its uncommitted work.
    assert delivery.is_dir()
    assert git(delivery, "rev-parse", "HEAD") == head
    assert (delivery / "wip.txt").read_text(encoding="utf-8") == (
        "work in progress\n"
    )
    assert git(delivery, "status", "--porcelain") == "?? wip.txt"
    assert not (delivery / "src" / "added.py").exists()
    # The draft read the fresh base all the same.
    assert sessions.views[0].path != delivery
    assert "src/added.py" in sessions.views[0].files
    assert "src/old_name.py" not in sessions.views[0].files


# --- the failure path ---------------------------------------------------

def test_a_failed_base_fetch_fails_the_draft_loudly(scene, tmp_path, monkeypatch):
    """No silent fallback to the stale working tree (the defect's premise)."""
    sessions = draft_fakes.install_sessions(monkeypatch)
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(issue())
    config = make_config(tmp_path, scene.checkout, base="release-9")

    code, out, err = run_round(config, tmp_path / "session")

    assert code == 1
    assert out == ""
    failure = json.loads(err)
    assert failure["code"] == "draft_failed"
    assert failure["step"] == "read the repository at origin/release-9"
    assert "release-9" in failure["detail"]
    # The session never ran against the stale checkout.
    assert sessions.calls == []
    assert list((scene.checkout / ".orbi" / "draft-view").glob("*")) == []


def test_a_killed_drafts_leftover_view_never_blocks_the_next_one(scene):
    """A draft killed after its view was added leaves a `prunable` entry.

    It is re-created over, not pruned: pruning runs in the shared
    `worktrees/` registry a delivery running at the same time writes to.
    """
    path = draft.draft_view_path(scene.checkout, "a1b2c3d4")
    draft.add_draft_view(scene.checkout, BASE, "a1b2c3d4")
    shutil.rmtree(path)
    assert "prunable" in git(scene.checkout, "worktree", "list", "--porcelain")

    again = draft.add_draft_view(scene.checkout, BASE, "a1b2c3d4")

    assert again == path
    assert (path / "src" / "added.py").is_file()
    assert "prunable" not in git(
        scene.checkout, "worktree", "list", "--porcelain",
    )
    draft.remove_draft_view(scene.checkout, path)
    assert not path.exists()
    # A view already gone is nothing to remove — not an error.
    draft.remove_draft_view(scene.checkout, path)


def test_a_view_that_cannot_be_removed_only_warns(
    scene, tmp_path, monkeypatch, caplog,
):
    """Cleanup is a bypass: a failed removal never fails a finished draft."""
    sessions = draft_fakes.install_sessions(monkeypatch)
    draft_fakes.judge_passing(monkeypatch)
    sessions.queue_draft(issue())
    draft_fakes.fail_view_cleanup(monkeypatch)
    config = make_config(tmp_path, scene.checkout)

    with caplog.at_level(logging.WARNING):
        code, out, err = run_round(config, tmp_path / "session")

    assert code == 0, err
    assert json.loads(out)["check"] == "passed"
    assert "could not be removed" in caplog.text
