"""E2E run_id correlation tests (Issue #41).

Real git (local bare origin + clone) plus a fake ``pi`` executable that
behaves like the delivery agent: it reads the run id from the injected
system prompt, writes a run-scoped plan artifact, commits and pushes. A fake
``gh`` answers the runner's issue/PR calls and captures every comment.

Together these prove the acceptance criteria:

- one attempt (implement → review → fix → merge) carries ONE run_id through
  every journal line, Issue comment, branch/worktree name and artifact;
- a retry of the same Issue gets a new run_id while the old scene stays
  queryable by the old run_id;
- a failed attempt marks the Issue blocked with the same run_id marker.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import bootstrap_runner as runner

REPO = "owner/repo"
ISSUE_NUMBER = 41
PR_URL = "https://github.com/owner/repo/pull/41"

# The fake pi behaves like the delivery agent: it extracts the run id from
# the injected system prompt (exactly where the real runner puts it), writes
# the plan artifact with the stable marker, then commits and pushes.
FAKE_PI = """#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
prompt = args[args.index("--system-prompt") + 1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
plan = (
    f"<!-- muyan-pilot:run={run_id} -->\\n"
    f"# Plan\\n\\nrun_id={run_id}\\n"
)
with open(os.path.join(cwd, "plan.md"), "w", encoding="utf-8") as handle:
    handle.write(plan)
for command in (
    ["git", "add", "."],
    ["git", "commit", "-m", f"plan for run {run_id}"],
    ["git", "push", "origin", "HEAD"],
):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
"""

FAKE_PI_FAILING = """#!/usr/bin/env python3
import os, sys
os.makedirs(os.path.join(os.getcwd(), ".pi-session"), exist_ok=True)
sys.stderr.write("pi exploded")
sys.exit(3)
"""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """Local bare origin plus a clone with two commits on main."""
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
    (clone / "a.txt").write_text("a", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "first")
    (clone / "b.txt").write_text("b", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "second")
    git(clone, "push", "origin", "main")
    return clone


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    """Each test starts without a bound run id."""
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)


def install_fake_pi(monkeypatch, tmp_path: Path, script: str) -> None:
    """Put a fake ``pi`` executable first on PATH for the runner's Popen."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "pi"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def install_fake_gh(monkeypatch, comments: list[str]) -> None:
    """Answer the runner's ``gh`` calls; capture every Issue comment.

    ``gh pr list`` answers with the local HEAD as ``headRefOid`` and a PR
    body carrying the marker of the run id embedded in the task branch —
    the same value the real delivery agent is told to use.
    """
    comment_ids: list[int] = []
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"]:
            if command[1] == "issue":
                if command[2] == "comment":
                    comment_ids.append(len(comments) + 1)
                    comments.append(command[-1])
                return ""
            if command[1] == "api":
                # The progress publisher (Issue #18) keeps the single
                # per-run comment via gh api: list (GET), create (POST),
                # update (PATCH). Comments are tracked by id like the
                # real API, so a PATCH updates the exact comment;
                # `comments` stays the chronological body history.
                if "--method" in command:
                    method = command[command.index("--method") + 1]
                    body = command[command.index("--field") + 1][
                        len("body="):]
                    if method == "POST":
                        comment_id = len(comments) + 1
                        comment_ids.append(comment_id)
                        comments.append(body)
                        return json.dumps(
                            {"id": comment_id, "body": body},
                        )
                    if method == "PATCH":
                        comment_id = int(command[2].rsplit("/", 1)[1])
                        index = comment_ids.index(comment_id)
                        comments[index] = body
                        return ""
                return json.dumps([
                    {"id": comment_id, "body": body}
                    for comment_id, body in zip(comment_ids, comments)
                ])
            if command[1] == "pr":
                branch = command[command.index("--head") + 1]
                run_id = branch.rsplit("-", 1)[1]
                head = real_run(
                    ["git", "rev-parse", "HEAD"], cwd=kwargs["cwd"],
                )
                return json.dumps([{
                    "url": PR_URL,
                    "baseRefName": "main",
                    "headRefOid": head,
                    "body": (
                        f"<!-- muyan-pilot:run={run_id} -->\n\n"
                        f"Plan for {branch}"
                    ),
                }])
            raise AssertionError(f"unexpected gh command: {command}")
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)


def write_prompt(tmp_path: Path) -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "You are the delivery agent.\n"
        "- Delivery base branch: `{{BASE_BRANCH}}`\n"
        "- Delivery base SHA: `{{BASE_SHA}}`\n"
        "- Run id: `{{RUN_ID}}`\n",
        encoding="utf-8",
    )
    return prompt


def config_for(clone: Path, tmp_path: Path) -> dict:
    return {
        "repo_dir": clone,
        "prompt": write_prompt(tmp_path),
        "base_branch": "main",
        "source_repos": [REPO],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
    }


def issue() -> dict:
    return {"number": ISSUE_NUMBER, "title": "E2E", "body": "body"}


def worktree_for(clone: Path, run_id: str) -> Path:
    return (
        clone / ".worktrees"
        / f"muyan-pilot-{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    )


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_fake_gh_rejects_unexpected_command(monkeypatch, tmp_path):
    install_fake_gh(monkeypatch, [])
    with pytest.raises(AssertionError, match="unexpected gh command"):
        runner.run_command(["gh", "release", "list"])


def test_fake_gh_ignores_unknown_api_methods(monkeypatch):
    # The runner only POSTs and PATCHes comments via gh api; any other
    # method answers with the comment list (no crash).
    comments: list[str] = []
    install_fake_gh(monkeypatch, comments)
    raw = runner.run_command(
        ["gh", "api", "repos/owner/repo/issues/41/comments",
         "--method", "DELETE", "--field", "body=x"],
    )
    assert json.loads(raw) == []


def test_e2e_one_run_id_carries_every_event_of_the_attempt(
    clone, tmp_path, monkeypatch, caplog,
):
    comments: list[str] = []
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    install_fake_gh(monkeypatch, comments)
    caplog.set_level("INFO")

    pr_url = runner.process_issue(issue(), config_for(clone, tmp_path), REPO)

    assert pr_url == PR_URL
    run_id = runner.current_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", run_id)

    # 1. Every journal line of the attempt starts with the same [run_id].
    assert caplog.messages, "no journal lines captured"
    for message in caplog.messages:
        assert message.startswith(f"[{run_id}]"), message

    # 2. Every Issue comment carries the visible field and the hidden
    # marker: the live progress comment, the started / plan ready / PR
    # opened milestones and the two scene comments.
    assert len(comments) == 6
    for body in comments:
        assert f"run_id={run_id}" in body
        assert f"<!-- muyan-pilot:run={run_id} -->" in body

    # 3. Branch and worktree names carry the run_id.
    worktree = worktree_for(clone, run_id)
    assert worktree.is_dir()
    assert git(worktree, "branch", "--show-current") == (
        f"muyan-pilot/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    )

    # 4. Run artifacts live inside the run-scoped worktree and carry the
    #    marker; the Pi session dir is under the same run-scoped path.
    plan = (worktree / "plan.md").read_text(encoding="utf-8")
    assert f"<!-- muyan-pilot:run={run_id} -->" in plan
    assert (worktree / ".pi-session").is_dir()


def test_e2e_retry_of_same_issue_gets_new_run_id_and_keeps_old_scene(
    clone, tmp_path, monkeypatch, caplog,
):
    run_ids = iter(["a1b2c3d4", "b2c3d4e5"])
    monkeypatch.setattr(runner, "new_run_id", lambda: next(run_ids))
    comments: list[str] = []
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    install_fake_gh(monkeypatch, comments)
    caplog.set_level("INFO")
    config = config_for(clone, tmp_path)

    runner.process_issue(issue(), config, REPO)
    runner.process_issue(issue(), config, REPO)

    first = worktree_for(clone, "a1b2c3d4")
    second = worktree_for(clone, "b2c3d4e5")
    assert first.is_dir() and second.is_dir()

    # The old scene is preserved and still queryable by the old run id.
    assert "a1b2c3d4" in (first / "plan.md").read_text(encoding="utf-8")
    assert "b2c3d4e5" in (second / "plan.md").read_text(encoding="utf-8")

    # Comments are not confused: each attempt carries exactly its own
    # id (six comments per attempt: started Pi, progress, started
    # milestone, plan ready milestone, opened PR, PR opened milestone).
    assert len(comments) == 12
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in comments[0]
    assert "b2c3d4e5" not in comments[0]
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in comments[1]
    assert "<!-- muyan-pilot:run=b2c3d4e5 -->" in comments[6]
    assert "a1b2c3d4" not in comments[6]
    assert "<!-- muyan-pilot:run=b2c3d4e5 -->" in comments[7]

    # The journal timeline splits cleanly into the two runs.
    first_lines = [
        m for m in caplog.messages if m.startswith("[a1b2c3d4]")
    ]
    second_lines = [
        m for m in caplog.messages if m.startswith("[b2c3d4e5]")
    ]
    assert first_lines and second_lines
    assert len(first_lines) + len(second_lines) == len(caplog.messages)


def test_e2e_failed_attempt_marks_blocked_with_same_run_id(
    clone, tmp_path, monkeypatch, caplog,
):
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    comments: list[str] = []
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_FAILING)
    install_fake_gh(monkeypatch, comments)
    caplog.set_level("INFO")

    with pytest.raises(subprocess.CalledProcessError):
        runner.process_issue(issue(), config_for(clone, tmp_path), REPO)

    # The failure report carries the same run id as the start comment
    # (progress + started milestone + started Pi + failure + blocked
    # milestone).
    assert len(comments) == 5
    start = [c for c in comments if "Muyan Pilot started Pi:" in c][0]
    failure = [c for c in comments if "Muyan Pilot failed:" in c][0]
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in start
    assert "Muyan Pilot failed:" in failure
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in failure
    assert "run_id=a1b2c3d4" in failure
    blocked = [c for c in comments if "Muyan Pilot: blocked" in c]
    assert blocked
    assert "<!-- muyan-pilot:run=a1b2c3d4 -->" in blocked[0]

    # Every journal line of the failed attempt carries the same run id.
    for message in caplog.messages:
        assert message.startswith("[a1b2c3d4]"), message

    # The run-scoped worktree (session scene) is preserved for inspection.
    worktree = worktree_for(clone, "a1b2c3d4")
    assert worktree.is_dir()
    assert (worktree / ".pi-session").is_dir()
