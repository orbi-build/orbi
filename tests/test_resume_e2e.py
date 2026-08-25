"""E2E: continue fixing the same PR after the base advances (Issue #45).

Real git (local bare origin + clone) plus a fake ``pi`` executable that
behaves like the delivery agent in both modes:

- fresh mode (no ``Existing PR:`` in the context): writes the run-scoped
  plan artifact, commits and pushes — the first delivery that opens the PR;
- resume mode (``Existing PR:`` in the context): resolves the merge
  conflict left by the runner's plain ``git merge origin/<base>`` and
  pushes to the SAME branch, so the SAME PR updates.

The acceptance criteria proven here:

- main advances on top of an old delivery and edits the SAME file; the
  runner merges the latest main into the ORIGINAL branch, the fixer
  resolves the conflict, reruns the push, and the PR keeps the same
  number, head branch, run_id and worktree;
- exactly one PR exists at the end;
- a fixer that cannot resolve marks the Issue ``ai-blocked`` and
  preserves the PR, branch and worktree.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import bootstrap_runner as runner

REPO = "owner/repo"
ISSUE_NUMBER = 45
PR_URL = "https://github.com/owner/repo/pull/45"

FAKE_PI = """#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
prompt = args[args.index("--system-prompt") + 1]
context = args[-1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
if "Existing PR:" in context:
    # Resume mode: resolve the merge conflict on the SAME branch.
    plan = (
        f"<!-- muyan-pilot:run={run_id} -->\\n"
        f"# Plan\\n\\nmerged main\\nrun_id={run_id}\\n"
    )
    with open(os.path.join(cwd, "plan.md"), "w", encoding="utf-8") as handle:
        handle.write(plan)
    for command in (
        ["git", "add", "."],
        ["git", "commit", "--no-edit"],
        ["git", "push", "origin", "HEAD"],
    ):
        subprocess.run(command, cwd=cwd, check=True, capture_output=True)
else:
    # Fresh mode: first delivery.
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

FAKE_PI_FIXER_FAILING = """#!/usr/bin/env python3
import os, sys
os.makedirs(os.path.join(os.getcwd(), ".pi-session"), exist_ok=True)
sys.stderr.write("fixer could not resolve the conflict")
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
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "pi"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def install_fake_gh(monkeypatch, comments: list[str],
                    edits: list[list[str]] | None = None) -> None:
    """Answer the runner's ``gh`` calls; capture comments and label edits.

    ``gh pr list`` answers with the local HEAD as ``headRefOid`` and a PR
    body carrying the marker of the run id embedded in the task branch.
    ``gh issue view --json comments`` returns the captured comment history
    (the same data the real GitHub would serve), which is how the resume
    scene is recovered after a restart.
    """
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"]:
            if command[1] == "issue":
                if command[2] == "comment":
                    comments.append(command[-1])
                    return ""
                if command[2] == "edit":
                    if edits is not None:
                        edits.append(command)
                    return ""
                if command[2] == "view":
                    if command[-1] == "body":
                        return json.dumps({"body": "original task body"})
                    return json.dumps(
                        [{"body": body} for body in comments],
                    )
                if command[2] == "list":
                    if "label:ai-pr-opened" in " ".join(command):
                        return json.dumps([{
                            "number": ISSUE_NUMBER,
                            "title": "Continue fixing the same PR",
                            "state": "OPEN",
                            "url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
                        }])
                    return "[]"
                return ""
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
    return {
        "number": ISSUE_NUMBER,
        "title": "Continue fixing the same PR",
        "body": "body",
    }


def worktree_for(clone: Path, run_id: str) -> Path:
    return (
        clone / ".worktrees"
        / f"muyan-pilot-{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    )


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_fake_gh_answers_other_commands_and_rejects_unknown(monkeypatch):
    comments: list[str] = []
    edits: list[list[str]] = []
    install_fake_gh(monkeypatch, comments, edits)
    # A non-resumable issue list answers with an empty queue...
    assert runner.run_command([
        "gh", "issue", "list", "--repo", REPO, "--state", "open",
        "--search", "label:ai-ready", "--json", "number", "--limit", "1",
    ]) == "[]"
    # ...other issue subcommands fall through to the empty answer...
    assert runner.run_command(
        ["gh", "issue", "status", "--repo", REPO],
    ) == ""
    # ...and anything outside issue/pr is rejected.
    with pytest.raises(AssertionError, match="unexpected gh command"):
        runner.run_command(["gh", "release", "list"])


def test_e2e_base_advances_and_resume_fixes_the_same_pr(
    clone, tmp_path, monkeypatch, caplog,
):
    comments: list[str] = []
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    install_fake_gh(monkeypatch, comments)
    caplog.set_level("INFO")
    config = config_for(clone, tmp_path)

    # ---- First delivery: PR A is opened on the old base.
    pr_url = runner.process_issue(issue(), config, REPO)
    assert pr_url == PR_URL
    run_id = runner.current_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", run_id)
    branch = f"muyan-pilot/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    worktree = worktree_for(clone, run_id)
    assert worktree.is_dir()
    started = comments[0]
    opened = comments[1]
    assert "Muyan Pilot started Pi:" in started
    assert f"Muyan Pilot opened PR: {PR_URL}" in opened
    assert f"run_id={run_id}" in opened

    # ---- origin/main advances and edits the SAME file (plan.md).
    (clone / "plan.md").write_text(
        "main edited plan.md after the PR was opened\n", encoding="utf-8",
    )
    git(clone, "add", ".")
    git(clone, "commit", "-m", "main advances with a conflicting edit")
    git(clone, "push", "origin", "main")
    origin_main = git(clone, "rev-parse", "origin/main")

    # The delivery is now behind: the frozen base is not the latest main.
    assert origin_main not in git(worktree, "log", "--format=%H")

    # ---- Resume: the next tick recovers the scene from the Issue
    #      comments and fixes the SAME PR in the ORIGINAL worktree.
    resumed, scene = runner.pick_resumable_delivery(REPO)
    assert scene is not None
    assert scene["run_id"] == run_id
    assert scene["branch"] == branch
    assert scene["worktree"] == str(worktree)
    assert scene["pr_url"] == PR_URL
    assert scene["base_branch"] == "main"

    result = runner.resume_delivery(resumed, scene, config, REPO)
    assert result == PR_URL

    # The same run id is bound for the whole resumed attempt.
    assert runner.current_run_id() == run_id

    # The latest base was merged into the ORIGINAL branch: the merge
    # commit contains both the delivery work and the new main commit.
    head = git(worktree, "rev-parse", "HEAD")
    assert origin_main in git(worktree, "log", "--format=%H", head)
    assert git(worktree, "branch", "--show-current") == branch
    # The conflict was resolved by the fixer, not auto-resolved.
    assert "merged main" in (worktree / "plan.md").read_text(encoding="utf-8")

    # The SAME PR updated: exactly one open PR for the head branch, and
    # its head is the new local HEAD (pushed by the fixer).
    raw = runner.run_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "url,baseRefName,headRefOid", "--limit", "2",
    ], cwd=worktree)
    prs = json.loads(raw)
    assert len(prs) == 1
    assert prs[0]["url"] == PR_URL
    assert prs[0]["headRefOid"] == head

    # The fix progress comment reuses the same marker and run_id.
    fixed = comments[-1]
    assert f"Muyan Pilot fixed PR: {PR_URL}" in fixed
    assert f"<!-- muyan-pilot:run={run_id} -->" in fixed
    assert f"run_id={run_id}" in fixed
    assert "Muyan Pilot opened PR" not in fixed

    # Every journal line of the resumed attempt carries the same run id.
    for message in caplog.messages:
        assert message.startswith(f"[{run_id}]"), message

    # One PR, one branch, one worktree, one run id: nothing was recreated.
    assert len(comments) == 3
    assert git(worktree, "branch", "--show-current") == branch
    assert worktree.is_dir()


def test_e2e_fixer_failure_keeps_pr_and_marks_blocked(
    clone, tmp_path, monkeypatch, caplog,
):
    comments: list[str] = []
    edits: list[list[str]] = []
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI)
    install_fake_gh(monkeypatch, comments, edits)
    caplog.set_level("INFO")
    config = config_for(clone, tmp_path)

    pr_url = runner.process_issue(issue(), config, REPO)
    assert pr_url == PR_URL
    run_id = runner.current_run_id()
    branch = f"muyan-pilot/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    worktree = worktree_for(clone, run_id)

    # origin/main advances with a conflicting edit to the same file.
    (clone / "plan.md").write_text(
        "main edited plan.md after the PR was opened\n", encoding="utf-8",
    )
    git(clone, "add", ".")
    git(clone, "commit", "-m", "main advances with a conflicting edit")
    git(clone, "push", "origin", "main")

    resumed, scene = runner.pick_resumable_delivery(REPO)
    assert scene is not None

    # The fixer cannot resolve the conflict.
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_FIXER_FAILING)
    with pytest.raises(subprocess.CalledProcessError):
        runner.resume_delivery(resumed, scene, config, REPO)

    # The Issue is marked ai-blocked (and ai-pr-opened removed)...
    assert [
        "gh", "issue", "edit", str(ISSUE_NUMBER), "--repo", REPO,
        "--add-label", "ai-blocked", "--remove-label", "ai-pr-opened",
    ] in edits
    # ...the PR, branch and worktree are all preserved (never deleted,
    # never force-pushed).
    assert worktree.is_dir()
    assert git(worktree, "branch", "--show-current") == branch
    failure = comments[-1]
    assert "Muyan Pilot failed:" in failure
    assert "returned non-zero exit status 3" in failure
    assert f"<!-- muyan-pilot:run={run_id} -->" in failure
    assert f"run_id={run_id}" in failure
    assert "Muyan Pilot fixed PR" not in failure
