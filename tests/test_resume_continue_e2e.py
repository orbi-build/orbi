"""E2E: an interrupted run continues on the existing work (Issue #219).

Real git (local bare origin + clone) plus a fake ``pi`` executable:

- interrupted mode (first attempt): writes UNCOMMITTED work and a
  session JSONL, then dies with a non-zero exit — the runner is killed
  before the failure path could remove the ``ai-in-progress`` label;
- continue mode (the restart): the new session receives the resume
  context (the uncommitted changes + the previous session's progress)
  in its context argument and CONTINUES the existing work (it commits
  what is there and appends — it never rewrites from scratch).

The acceptance criteria proven here:

- after the interruption the restart continues on the SAME run id,
  branch and worktree — no second run/branch/worktree;
- the new session's context carries the resume context (changed files
  + previous session progress) — the agent continues the existing work
  instead of a fresh redo;
- the ``resume_continue`` journal line carries worktree, changed_files
  and reused_runs;
- after a repo rename (slug change) the old worktree is found by issue
  number + repo NAME and the same run continues;
- a worktree whose run state file is missing or corrupt fails fast
  (the Issue goes ``ai-blocked`` with the reason) — never a silent
  fresh redo.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bootstrap_runner as runner

REPO = "owner/repo"
RENAME_OLD = "xqliu/orbi"
RENAME_NEW = "orbi-build/orbi"
ISSUE_NUMBER = 219
PR_URL = "https://github.com/owner/repo/pull/219"

# First attempt: the interrupted run. It leaves UNCOMMITTED work and a
# previous session file behind, then dies (the runner is killed before
# the failure path runs).
FAKE_PI_INTERRUPTED = """#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timezone
cwd = os.getcwd()
session_dir = os.path.join(cwd, ".pi-session")
os.makedirs(session_dir, exist_ok=True)
with open(os.path.join(cwd, "work.txt"), "a", encoding="utf-8") as f:
    f.write("part-1\\n")
now = datetime.now(timezone.utc).isoformat()
with open(os.path.join(session_dir, "interrupted.jsonl"), "a",
          encoding="utf-8") as f:
    f.write(json.dumps({"type": "session", "id": "sess-interrupted"}) + "\\n")
    f.write(json.dumps({
        "type": "message", "timestamp": now,
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "name": "bash",
             "arguments": {"command": "echo part-1 >> work.txt"}}]},
    }) + "\\n")
    f.write(json.dumps({
        "type": "message", "timestamp": now,
        "message": {"role": "toolResult", "toolName": "bash",
                    "isError": False},
    }) + "\\n")
sys.stderr.write("killed mid-run (simulated interruption)")
sys.exit(3)
"""

# The restart: the new session receives the resume context in its
# context argument. It CONTINUES the existing work: it commits the
# uncommitted changes (never discards or rewrites them) and appends.
FAKE_PI_CONTINUE = """#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
context = args[-1]
cwd = os.getcwd()
with open(os.path.join(cwd, "resume-context.txt"), "w",
          encoding="utf-8") as handle:
    handle.write(context)
if "Resume context (Issue #219)" not in context:
    sys.stderr.write("resume context missing from the new session")
    sys.exit(9)
if "work.txt" not in context or "sess-interrupted" not in context:
    sys.stderr.write("resume context lost the existing work")
    sys.exit(9)
# Continue the existing work: commit what is there, then append.
with open(os.path.join(cwd, "work.txt"), "a", encoding="utf-8") as f:
    f.write("part-2\\n")
for command in (
    ["git", "add", "."],
    ["git", "commit", "-m", "continue the interrupted work"],
):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
"""

# The fresh-mode agent (used by the fail-fast test's EXPECTED fresh
# run — which must never happen): if it ever runs, the test fails.
FAKE_PI_FRESH = """#!/usr/bin/env python3
import sys
sys.stderr.write("a FRESH run started — the resume was lost")
sys.exit(7)
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
                    labels: dict[int, list[str]]):
    """Answer the runner's ``gh`` calls; track labels and comments.

    ``gh pr list`` / ``gh pr create`` are stateful (Issue #186): no PR
    exists until the Runner's ``pr create`` lands, and the created PR
    carries the local HEAD plus the exact body the Runner passed.
    Returns the fake handler (the defensive branches are driven
    directly by a dedicated test).
    """
    comment_ids: list[int] = []
    created_prs: list[dict] = []
    real_run = runner.run_command

    def fake_run(command, **kwargs):
        if command[:1] == ["gh"]:
            if command[1] == "issue":
                if command[2] == "comment":
                    comment_ids.append(len(comments) + 1)
                    comments.append(command[-1])
                    return ""
                if command[2] == "edit":
                    number = int(command[3])
                    current = labels.setdefault(number, [])
                    if "--add-label" in command:
                        label = command[command.index("--add-label") + 1]
                        if label not in current:
                            current.append(label)
                    if "--remove-label" in command:
                        label = command[command.index("--remove-label") + 1]
                        if label in current:
                            current.remove(label)
                    return ""
                if command[2] == "list":
                    if "label:ai-in-progress" in " ".join(command):
                        return json.dumps([
                            {"number": number}
                            for number in labels
                            if "ai-in-progress" in labels[number]
                        ])
                    return "[]"
            if command[1] == "api":
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
                if command[2] == "list":
                    head = command[command.index("--head") + 1]
                    return json.dumps(
                        [pr for pr in created_prs
                         if pr["headRefName"] == head]
                    )
                if command[2] == "create":
                    base = command[command.index("--base") + 1]
                    branch = command[command.index("--head") + 1]
                    title = command[command.index("--title") + 1]
                    body = command[command.index("--body") + 1]
                    head = real_run(
                        ["git", "rev-parse", "HEAD"], cwd=kwargs["cwd"],
                    )
                    created_prs.append({
                        "url": PR_URL,
                        "baseRefName": base,
                        "headRefName": branch,
                        "headRefOid": head,
                        "headRepository": {"name": "repo"},
                        "headRepositoryOwner": {"login": "owner"},
                        "body": body,
                        "title": title,
                    })
                    return PR_URL
                raise AssertionError(f"unexpected gh pr command: {command}")
            raise AssertionError(f"unexpected gh command: {command}")
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner, "run_command", fake_run)
    return fake_run


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


def config_for(clone: Path, tmp_path: Path, source_repo: str) -> dict:
    return {
        "repo_dir": clone,
        "prompt": write_prompt(tmp_path),
        "base_branch": "main",
        "source_repos": [source_repo],
        "workspace_root": tmp_path,
        "context_files": [],
        "skills": [],
    }


def issue() -> dict:
    return {
        "number": ISSUE_NUMBER, "title": "E2E", "body": "body",
        "labels": [],
    }


def worktree_for(clone: Path, source_repo: str, run_id: str) -> Path:
    return (
        clone / ".worktrees"
        / f"muyan-pilot-{source_repo.replace('/', '-')}"
        f"-issue-{ISSUE_NUMBER}-{run_id}"
    )


def run_first_attempt(monkeypatch, tmp_path: Path, clone: Path,
                      comments: list[str], labels: dict[int, list[str]],
                      source_repo: str) -> Path:
    """The interrupted first attempt: claim label left behind (the
    failure path never ran — the runner was killed), uncommitted work
    and a session file in the worktree.

    The restart gets the REAL `edit_issue` again (the kill simulation
    is over): the resumed attempt's label transitions reach GitHub.
    """
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_INTERRUPTED)
    install_fake_gh(monkeypatch, comments, labels)
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    real_edit_issue = runner.edit_issue

    def claiming_edit(number, **kwargs):
        # Only the claim edit reaches GitHub before the kill; the
        # failure path's label transition never happens.
        if kwargs.get("add") == "ai-in-progress" \
                and kwargs.get("remove") is None:
            real_edit_issue(number, **kwargs)

    monkeypatch.setattr(runner, "edit_issue", claiming_edit)
    with pytest.raises(subprocess.CalledProcessError):
        runner.process_issue(issue(), config_for(clone, tmp_path,
                                                 source_repo),
                             source_repo)
    # The runner is back: label transitions work again.
    monkeypatch.setattr(runner, "edit_issue", real_edit_issue)
    worktree = worktree_for(clone, source_repo, "a1b2c3d4")
    assert worktree.is_dir()
    assert "ai-in-progress" in labels[ISSUE_NUMBER]
    return worktree


def test_e2e_restart_continues_on_the_uncommitted_work(
    clone, tmp_path, monkeypatch, caplog,
):
    """AC1/AC3: the interrupted run's uncommitted changes + session
    progress become the new session's starting point — the same run,
    branch and worktree continue; the `resume_continue` line is
    logged."""
    comments: list[str] = []
    labels: dict[int, list[str]] = {ISSUE_NUMBER: ["ai-ready"]}
    worktree = run_first_attempt(
        monkeypatch, tmp_path, clone, comments, labels, REPO,
    )
    # The interruption left uncommitted work and a session file.
    assert (worktree / "work.txt").read_text() == "part-1\n"
    assert (worktree / ".pi-session" / "interrupted.jsonl").is_file()
    assert git(worktree, "status", "--porcelain") != ""

    # ---- Restart: the healthy runner resumes the same run.
    caplog.set_level("INFO")
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_CONTINUE)
    install_fake_gh(monkeypatch, comments, labels)
    monkeypatch.setattr(runner, "new_run_id", lambda: "b2c3d4e5")
    pr_url = runner.process_issue(
        issue(), config_for(clone, tmp_path, REPO), REPO,
    )

    # The SAME run continues: no second worktree, same branch.
    assert pr_url == PR_URL
    assert runner.current_run_id() == "a1b2c3d4"
    assert not worktree_for(clone, REPO, "b2c3d4e5").exists()
    assert git(
        worktree, "branch", "--show-current",
    ) == f"muyan-pilot/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-a1b2c3d4"

    # The new session RECEIVED the resume context (the existing work),
    # and the agent continued it: the committed work.txt carries BOTH
    # parts — part-1 (the interrupted work, never discarded) and
    # part-2 (the continuation).
    context = (worktree / "resume-context.txt").read_text()
    assert "Resume context (Issue #219)" in context
    assert "work.txt" in context
    assert "sess-interrupted" in context
    committed = git(worktree, "show", "HEAD:work.txt")
    assert committed == "part-1\npart-2"

    # The structured resume line (auditable, Issue #219).
    lines = [m for m in caplog.messages if "resume_continue" in m]
    assert len(lines) == 1
    assert f"worktree={worktree}" in lines[0]
    assert "changed_files=" in lines[0]
    assert "reused_runs=1" in lines[0]
    assert "previous_session=sess-interrupted" in lines[0]

    # The delivery finished: the in-progress label is gone.
    assert "ai-in-progress" not in labels[ISSUE_NUMBER]
    assert "ai-pr-opened" in labels[ISSUE_NUMBER]


def test_e2e_repo_rename_finds_the_old_worktree_by_issue_and_name(
    clone, tmp_path, monkeypatch, caplog,
):
    """AC2: the repo was renamed (slug `xqliu-orbi` ->
    `orbi-build-orbi`): the old worktree is found by issue number +
    repo NAME and the SAME run/branch/worktree continues — no second
    run/branch/worktree is created."""
    comments: list[str] = []
    labels: dict[int, list[str]] = {ISSUE_NUMBER: ["ai-ready"]}
    worktree = run_first_attempt(
        monkeypatch, tmp_path, clone, comments, labels, RENAME_OLD,
    )
    assert worktree.name == (
        f"muyan-pilot-{RENAME_OLD.replace('/', '-')}"
        f"-issue-{ISSUE_NUMBER}-a1b2c3d4"
    )

    # ---- Restart AFTER the rename: the configured source repo is the
    # NEW slug; the old worktree must still be found and continued.
    caplog.set_level("INFO")
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_CONTINUE)
    install_fake_gh(monkeypatch, comments, labels)
    monkeypatch.setattr(runner, "new_run_id", lambda: "b2c3d4e5")
    pr_url = runner.process_issue(
        issue(), config_for(clone, tmp_path, RENAME_NEW), RENAME_NEW,
    )

    # The SAME worktree (old slug) continues; no second worktree under
    # the new slug.
    assert pr_url == PR_URL
    assert runner.current_run_id() == "a1b2c3d4"
    assert worktree.is_dir()
    assert not worktree_for(clone, RENAME_NEW, "a1b2c3d4").exists()
    assert not worktree_for(clone, RENAME_NEW, "b2c3d4e5").exists()
    # The branch keeps its original (old-slug) name: the same branch
    # continues, never a second one.
    assert git(
        worktree, "branch", "--show-current",
    ) == f"muyan-pilot/{RENAME_OLD.replace('/', '-')}-issue-{ISSUE_NUMBER}-a1b2c3d4"
    # The continued work landed on the ORIGINAL branch.
    committed = git(worktree, "show", "HEAD:work.txt")
    assert committed == "part-1\npart-2"
    # The resume was logged.
    assert [m for m in caplog.messages if "resume_continue" in m]


def test_e2e_missing_run_state_fails_fast_without_a_fresh_run(
    clone, tmp_path, monkeypatch, caplog,
):
    """AC4: the interrupted worktree exists but its run state file is
    gone — the same run cannot be verified. The attempt fails fast
    (the Issue goes `ai-blocked` with the reason); a fresh run is
    never started."""
    comments: list[str] = []
    labels: dict[int, list[str]] = {ISSUE_NUMBER: ["ai-ready"]}
    worktree = run_first_attempt(
        monkeypatch, tmp_path, clone, comments, labels, REPO,
    )
    # The state file is lost (e.g. a partial cleanup).
    state = worktree / ".muyan-pilot" / "run-state.json"
    assert state.is_file()
    state.unlink()

    # ---- Restart: the fresh-mode fake pi must NEVER run.
    caplog.set_level("INFO")
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_FRESH)
    install_fake_gh(monkeypatch, comments, labels)
    monkeypatch.setattr(runner, "new_run_id", lambda: "b2c3d4e5")
    with pytest.raises(RuntimeError, match="run state"):
        runner.process_issue(
            issue(), config_for(clone, tmp_path, REPO), REPO,
        )
    # The terminal state is `ai-blocked` (the claim label removed).
    assert labels[ISSUE_NUMBER] == ["ai-ready", "ai-blocked"]
    # The failure comment carries the reason and the run marker.
    failed = [
        body for body in comments
        if "Muyan Pilot failed" in body
        and "cannot continue the interrupted run" in body
    ]
    assert len(failed) == 1
    assert "run state" in failed[0]
    assert "<!-- muyan-pilot:run=" in failed[0]
    # No fresh run was started: the existing work is untouched.
    assert (worktree / "work.txt").read_text() == "part-1\n"
    assert git(worktree, "status", "--porcelain") != ""
    assert "resume_continue_failed" in caplog.text
    assert "resume_continue " not in caplog.text


def test_e2e_corrupt_run_state_fails_fast_without_a_fresh_run(
    clone, tmp_path, monkeypatch, caplog,
):
    """AC4: a corrupt run state file is a delivery failure, never a
    guess — the same fail-fast path as the missing file."""
    comments: list[str] = []
    labels: dict[int, list[str]] = {ISSUE_NUMBER: ["ai-ready"]}
    worktree = run_first_attempt(
        monkeypatch, tmp_path, clone, comments, labels, REPO,
    )
    state = worktree / ".muyan-pilot" / "run-state.json"
    state.write_text("corrupt", encoding="utf-8")

    caplog.set_level("INFO")
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_FRESH)
    install_fake_gh(monkeypatch, comments, labels)
    monkeypatch.setattr(runner, "new_run_id", lambda: "b2c3d4e5")
    with pytest.raises(RuntimeError, match="run state"):
        runner.process_issue(
            issue(), config_for(clone, tmp_path, REPO), REPO,
        )
    assert labels[ISSUE_NUMBER] == ["ai-ready", "ai-blocked"]
    failed = [
        body for body in comments
        if "Muyan Pilot failed" in body
        and "cannot continue the interrupted run" in body
    ]
    assert len(failed) == 1
    assert "corrupt" in failed[0]
    # The existing work is untouched (no fresh redo).
    assert (worktree / "work.txt").read_text() == "part-1\n"


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    """A git failure is a test failure with the exact reason."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "refs/heads/does-not-exist")


def test_fake_gh_handler_answers_the_unreached_transitions(
    tmp_path, monkeypatch,
):
    """The fake gh's defensive branches — a remove-only label edit, a
    remove of a label that is not there, a non-resume issue list, a
    plain comment GET, an unexpected subcommand — are driven directly:
    the delivery flow itself never issues them."""
    comments: list[str] = []
    labels: dict[int, list[str]] = {
        ISSUE_NUMBER: ["ai-ready", "ai-in-progress"],
    }
    fake = install_fake_gh(monkeypatch, comments, labels)

    # A remove-only label edit (the `--add-label`-absent branch).
    fake(["gh", "issue", "edit", str(ISSUE_NUMBER), "--repo", REPO,
          "--remove-label", "ai-in-progress"])
    assert labels[ISSUE_NUMBER] == ["ai-ready"]
    # Removing a label that is not there is a no-op (the `label in
    # current`-false branch).
    fake(["gh", "issue", "edit", str(ISSUE_NUMBER), "--repo", REPO,
          "--remove-label", "ai-in-progress"])
    assert labels[ISSUE_NUMBER] == ["ai-ready"]
    # A non-resume issue list (no `label:ai-in-progress` in the
    # search) answers the empty array.
    assert fake(["gh", "issue", "list", "--repo", REPO,
                 "--state", "open", "--search", "label:ai-ready",
                 "--json", "number"]) == "[]"
    # A POSTed comment gets an id; a plain GET lists the tracked
    # comments.
    posted = fake(["gh", "api", "repos/owner/repo/issues/219/comments",
                   "--method", "POST", "--field", "body=scene"])
    assert json.loads(posted) == {"id": 1, "body": "scene"}
    get = fake(["gh", "api", "repos/owner/repo/issues/219/comments"])
    assert json.loads(get) == [
        {"id": 1, "body": "scene"},
    ]
    # An unexpected `gh pr` subcommand and an unexpected `gh`
    # subcommand fail fast (the fake is a contract, not a sink).
    with pytest.raises(AssertionError, match="unexpected gh pr"):
        fake(["gh", "pr", "view", "219"])
    with pytest.raises(AssertionError, match="unexpected gh command"):
        fake(["gh", "release", "list"])
    # A `gh issue` subcommand the flow never issues falls through the
    # comment/edit/list handlers and fails fast.
    with pytest.raises(AssertionError, match="unexpected gh command"):
        fake(["gh", "issue", "view", str(ISSUE_NUMBER)])
    # A `gh api` method that is neither POST nor PATCH falls through
    # to the plain GET answer.
    get = fake(["gh", "api", "repos/owner/repo/issues/219/comments",
                "--method", "DELETE", "--field", "body=x"])
    assert json.loads(get) == [
        {"id": 1, "body": "scene"},
    ]
