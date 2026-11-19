"""orbi suggest reads `origin/<base>` at run start (Issue #2277).

A delivery always builds its own worktree from `origin/<base>`, so nothing
advances the deployment checkout: its working tree can be arbitrarily old. A
suggestion computed from it proposes work the base branch already contains and
describes files the base branch renamed or removed. These are the real-git
acceptance tests: both sessions of one run read a freshly fetched
`origin/<base_branch>`, the recorded `base_sha` is that remote commit, and the
checkout itself is never moved.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import git, task_context

import orbi.gitops as gitops
import orbi.suggest as suggest
from orbi.config import RunnerConfig
from orbi.journal import run_command as real_run_command
from tests.fakes import suggest as suggest_fakes
from tests.fakes.github import FakeGh

REPO = "acme/app"
BASE = "main"
UNDERSTANDING_TEXT = "A tiny repository used by the suggest tests."


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
    # (`orbi setup` pins it), so the view never shows up in its status.
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


def make_config(tmp_path: Path, checkout: Path) -> RunnerConfig:
    deploy = tmp_path / "deploy"
    skill = deploy / suggest.SUGGEST_SKILL_REL
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("skill\n", encoding="utf-8")
    return RunnerConfig(
        repo_dir=checkout, deploy_home=deploy, workspace_root=tmp_path,
        source_repos=(REPO,), base_branch=BASE,
    )


VIEW_RE = re.compile(
    r"Read the repository (?:itself under|at)( [^ ]+?)(?: by | and )",
)


class Sessions:
    """Both Pi sessions, recording the repository view each one saw."""

    def __init__(self) -> None:
        self.views: list[SimpleNamespace] = []

    def __call__(self, command, **kwargs):
        prompt = task_context(command)
        match = VIEW_RE.search(prompt)
        assert match is not None, prompt
        view = Path(match.group(1).strip())
        self.views.append(SimpleNamespace(
            path=view,
            files=sorted(
                path.relative_to(view).as_posix()
                for path in view.rglob("*")
                if path.is_file()
                and ".git" not in path.relative_to(view).parts
            ),
        ))
        tools = command[command.index("--tools") + 1]
        if tools == suggest.UNDERSTANDING_TOOLS:
            return UNDERSTANDING_TEXT
        return json.dumps({"suggestions": []})


def run_suggest(config: RunnerConfig) -> tuple[int, str]:
    out = io.StringIO()
    code = suggest.run_suggest(
        config, REPO, json_output=True,
        dispatch_issue=lambda *args, **kwargs: "",
        stdout=out,
    )
    return code, out.getvalue()


# --- the acceptance: sessions read the fetched base, base_sha is that base ---

def test_sessions_read_the_fetched_base_not_the_stale_checkout(
    scene, tmp_path, monkeypatch,
):
    suggest_fakes.install_gh(monkeypatch, FakeGh(REPO))
    sessions = Sessions()
    suggest_fakes.stub_stream_pi(monkeypatch, sessions)
    config = make_config(tmp_path, scene.checkout)

    code, out = run_suggest(config)

    assert code == 0
    document = json.loads(out)
    # The checkout really is behind the base the run had to read.
    assert git(scene.checkout, "rev-parse", "HEAD") == scene.stale
    assert len(sessions.views) == 2
    for view in sessions.views:
        assert view.path != scene.checkout
        assert "src/added.py" in view.files
        assert "src/new_name.py" in view.files
        assert "src/old_name.py" not in view.files
        # The view lives in the checkout's ignored run dir and is gone again.
        assert view.path.parent == scene.checkout / ".orbi" / "draft-view"
    assert list((scene.checkout / ".orbi" / "draft-view").glob("*")) == []
    assert document["base_sha"] == scene.fresh


def test_suggestions_leave_the_deployment_checkout_alone(
    scene, tmp_path, monkeypatch,
):
    suggest_fakes.install_gh(monkeypatch, FakeGh(REPO))
    suggest_fakes.stub_stream_pi(monkeypatch, Sessions())
    branch = git(scene.checkout, "rev-parse", "--abbrev-ref", "HEAD")
    status = git(scene.checkout, "status", "--porcelain")
    config = make_config(tmp_path, scene.checkout)

    code, _out = run_suggest(config)

    assert code == 0
    assert git(scene.checkout, "rev-parse", "HEAD") == scene.stale
    assert git(scene.checkout, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git(scene.checkout, "status", "--porcelain") == status == ""
    assert not (scene.checkout / "src" / "added.py").exists()
    assert (scene.checkout / "src" / "old_name.py").is_file()
