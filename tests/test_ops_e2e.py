"""E2E ops-delivery tests (Issue #537).

Real git (local bare origin + clone) plus REAL subprocess executables:
a fake ``pi`` that behaves like the ops agent (it executes real shell
commands and posts the evidence comment through a stateful fake ``gh``
on PATH) and the same fake ``gh`` answering the runner. This proves the
acceptance criteria at the real user path:

- an `ai-ready`+`ai-ops-only` Issue is claimed into a FULL execution
  session (a worktree like a dev ticket — there is no content-agent
  `--no-tools` dispatch and no command whitelist anywhere): the session
  really executes the authorized merge, really verifies the resulting
  state, and posts the per-step evidence comment carrying the run
  marker (the orbi-website#70 replay shape: merge PR + verify +
  evidence);
- a pure-ops delivery (no commit, clean worktree) is complete: the
  Runner closes the Issue, removes `ai-in-progress`, opens NO PR and
  skips the delivery wait (`IssueResult("ops", None)`);
- an ops session that commits code takes the SAME deterministic PR
  closeout as a dev ticket (the Runner pushes and opens the PR with the
  run marker and `Fixes #N`).

The production replay of orbi-website#70 itself needs the deployed
runner and its real credentials; this file proves the runner-side
behavior end to end in a hermetic world.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import orbi.runner as runner

REPO = "owner/repo"
ISSUE_NUMBER = 99
PR_URL = "https://github.com/owner/repo/pull/99"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Stateful fake ``gh`` executable (real subprocess — the fake pi's own
# gh calls go through it too). One JSON state file holds the issues
# (labels, state), the captured comments, the progress API comments and
# the PR objects. `pr merge` really merges the PR's head branch into
# main on the bare origin through a throwaway clone, so the merge is an
# observable git-state change, never a recorded flag only.
FAKE_GH = r"""#!/usr/bin/env python3
import fcntl, json, os, subprocess, sys, tempfile
from pathlib import Path

state_path = Path(os.environ["ORBI_FAKE_GH_STATE"])
args = sys.argv[1:]
lock_fd = os.open(str(state_path.parent / "gh-state.lock"),
                  os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
state = json.loads(state_path.read_text(encoding="utf-8"))


def save():
    fd, tmp = tempfile.mkstemp(dir=str(state_path.parent),
                               prefix="gh-state-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state))
    os.replace(tmp, state_path)


def git(*cmd):
    return subprocess.run(["git", *cmd], capture_output=True, text=True,
                          check=True).stdout.strip()


if args[:2] == ["issue", "view"]:
    # Issue #658: the pre-claim recheck reads the LIVE label truth
    # directly (`gh issue view`) instead of the search index.
    issue = state["issues"][args[2]]
    if args[-1] == "state":
        # The pre-PR closeout reads the source Issue state (Issue
        # #746): open in these scenes.
        print(json.dumps({"state": issue.get("state", "OPEN")}))
    else:
        print(json.dumps(
            {"labels": [{"name": label} for label in issue["labels"]]},
        ))
elif args[:2] == ["issue", "list"]:
    search = args[args.index("--search") + 1]
    tokens = search.split()
    required = [t[6:].split(",") for t in tokens if t.startswith("label:")]
    excluded = [t[7:] for t in tokens if t.startswith("-label:")]
    out = [
        {"number": int(num), "state": issue.get("state", "OPEN"),
         "title": issue["title"], "body": issue.get("body", "")}
        for num, issue in sorted(state["issues"].items(),
                                 key=lambda kv: int(kv[0]))
        if issue.get("state", "OPEN") == "OPEN"
        and all(any(r in issue["labels"] for r in group)
                for group in required)
        and not any(e in issue["labels"] for e in excluded)
    ]
    print(json.dumps(out))
elif args[:2] == ["issue", "edit"]:
    issue = state["issues"][args[2]]
    if "--add-label" in args:
        label = args[args.index("--add-label") + 1]
        if label not in issue["labels"]:
            issue["labels"].append(label)
    if "--remove-label" in args:
        label = args[args.index("--remove-label") + 1]
        if label in issue["labels"]:
            issue["labels"].remove(label)
    save()
elif args[:2] == ["issue", "close"]:
    state["issues"][args[2]]["state"] = "CLOSED"
    save()
elif args[:2] == ["issue", "comment"]:
    state.setdefault("comments", []).append(
        {"issue": args[2], "body": args[args.index("--body") + 1]})
    save()
elif args[:1] == ["api"]:
    if "--method" in args:
        method = args[args.index("--method") + 1]
        body = args[args.index("--field") + 1][len("body="):]
        if method == "POST":
            state.setdefault("api_ids", [])
            comment_id = len(state["api_ids"]) + 1
            state["api_ids"].append(comment_id)
            state.setdefault("api_comments", []).append(body)
            save()
            print(json.dumps({"id": comment_id, "body": body}))
        elif method == "PATCH":
            comment_id = int(args[1].rsplit("/", 1)[1])
            state["api_comments"][
                state["api_ids"].index(comment_id)] = body
            save()
        else:
            print(json.dumps([]))
    else:
        ids = state.get("api_ids", [])
        print(json.dumps([
            {"id": cid, "body": body}
            for cid, body in zip(ids, state.get("api_comments", []))
        ]))
elif args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    print(json.dumps([
        pr for pr in state.get("prs", {}).values()
        if pr["headRefName"] == head and pr.get("state", "OPEN") == "OPEN"
    ]))
elif args[:2] == ["pr", "create"]:
    branch = args[args.index("--head") + 1]
    issue_num = branch.split("-issue-", 1)[1]
    state.setdefault("prs", {})[branch] = {
        "url": f"https://github.com/owner/repo/pull/{issue_num}",
        "baseRefName": args[args.index("--base") + 1],
        "headRefName": branch,
        "headRefOid": git("rev-parse", "HEAD"),
        "state": "OPEN",
        "body": args[args.index("--body") + 1],
        "title": args[args.index("--title") + 1],
    }
    save()
    print(f"https://github.com/owner/repo/pull/{issue_num}")
elif args[:2] == ["pr", "merge"]:
    pr = state["prs"][args[2]]
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "clone", git("remote", "get-url", "origin"),
                        td], check=True, capture_output=True)
        for key, value in (("user.email", "pilot@test.local"),
                           ("user.name", "Pilot")):
            subprocess.run(["git", "-C", td, "config", key, value],
                           check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "fetch", "origin",
                        pr["headRefName"]],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "merge", "--no-ff", "-m",
                        "Merge PR #7 (ops)", "FETCH_HEAD"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "push", "origin", "HEAD:main"],
                       check=True, capture_output=True)
    pr["state"] = "MERGED"
    save()
else:
    raise AssertionError(f"unexpected gh command: {args}")
"""

# The fake ops pi (the orbi-website#70 replay shape): it receives the
# REAL rendered ops playbook as its system prompt, executes the
# authorized merge through a real `gh` call, verifies the resulting
# state with a real shell command, and posts the per-step evidence
# comment with the run marker. It does NOT commit (pure ops delivery).
FAKE_PI_OPS = r"""#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
prompt = args[args.index("--system-prompt") + 1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
with open(os.environ["ORBI_PI_PROMPT_DUMP"], "w", encoding="utf-8") as h:
    h.write(prompt)
merge = subprocess.run(
    ["gh", "pr", "merge", "7", "--repo", "owner/repo", "--merge"],
    capture_output=True, text=True,
)
verify = subprocess.run(
    ["sh", "-c", "git fetch origin && git log --oneline -1 origin/main"],
    cwd=cwd, capture_output=True, text=True,
)
evidence = (
    f"<!-- orbi:run={run_id} -->\n"
    f"# Ops evidence\n\nrun_id={run_id}\n\n"
    "## merge PR #7\n\n"
    "command: `gh pr merge 7 --repo owner/repo --merge`\n"
    f"exit={merge.returncode}\n\n"
    "## verify deployment state\n\n"
    "command: `git fetch origin && git log --oneline -1 origin/main`\n"
    "output:\n\n```\n"
    f"{verify.stdout.strip()}\n"
    "```\n"
)
comment = subprocess.run(
    ["gh", "issue", "comment", "99", "--repo", "owner/repo",
     "--body", evidence],
    capture_output=True, text=True,
)
sys.stdout.write(
    f"merge={merge.returncode} verify={verify.returncode} "
    f"comment={comment.returncode}"
)
"""

# The ops pi that ALSO delivers code (e.g. a wrangler.toml change): it
# commits on the task branch and stops — the Runner owns the push/PR.
FAKE_PI_OPS_COMMIT = r"""#!/usr/bin/env python3
import os, re, subprocess, sys
args = sys.argv[1:]
prompt = args[args.index("--system-prompt") + 1]
run_id = re.search(r"Run id: `([0-9a-f]{8})`", prompt).group(1)
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
with open(os.environ["ORBI_PI_PROMPT_DUMP"], "w", encoding="utf-8") as h:
    h.write(prompt)
with open(os.path.join(cwd, "wrangler.toml"), "w",
          encoding="utf-8") as handle:
    handle.write(f'name = "site"  # run {run_id}\n')
for command in (
    ["git", "add", "."],
    ["git", "commit", "-m", f"ops config change for run {run_id}"],
):
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)
sys.stdout.write("committed")
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
    """Local bare origin plus a clone with `main` and a `beta` branch
    one commit ahead (the PR #7 the ops ticket authorizes merging)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        capture_output=True, text=True, check=True,
    )
    git(repo, "config", "user.email", "pilot@test.local")
    git(repo, "config", "user.name", "Pilot")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "-b", "beta")
    (repo / "feature.txt").write_text("beta work", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "beta feature")
    git(repo, "push", "origin", "beta")
    git(repo, "checkout", "main")
    return repo


@pytest.fixture()
def gh_world(tmp_path: Path, monkeypatch) -> Path:
    """Install the stateful fake ``gh`` executable on PATH with a fresh
    state file; seed Issue #99 (`ai-ready`+`ai-ops-only`) and PR #7
    (beta → main). Returns the state file path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE_GH, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "issues": {
            str(ISSUE_NUMBER): {
                "title": "Merge PR #7 into main and verify the deployment",
                "body": (
                    "Authorized ops actions: merge PR #7 (beta → main) "
                    "and verify the deployment state. Nothing else."
                ),
                "labels": ["ai-ready", "ai-ops-only"],
                "state": "OPEN",
            },
        },
        "comments": [],
        "prs": {
            "7": {
                "url": "https://github.com/owner/repo/pull/7",
                "baseRefName": "main",
                "headRefName": "beta",
                "state": "OPEN",
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv("ORBI_FAKE_GH_STATE", str(state_path))
    return state_path


def install_fake_pi(monkeypatch, tmp_path: Path, script: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "pi"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(
        "ORBI_PI_PROMPT_DUMP", str(tmp_path / "pi-system-prompt.txt"),
    )


@pytest.fixture(autouse=True)
def _reset_run_id(monkeypatch):
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)


def ops_issue() -> dict:
    return {
        "number": ISSUE_NUMBER,
        "title": "Merge PR #7 into main and verify the deployment",
        "body": (
            "Authorized ops actions: merge PR #7 (beta → main) and "
            "verify the deployment state. Nothing else."
        ),
        "labels": [
            {"name": "ai-ready"}, {"name": "ai-ops-only"},
        ],
    }


def config_for(clone: Path) -> dict:
    # The REAL repo prompts: the ops session must receive the real
    # rendered ops playbook (`prompt_ops.md`, the sibling of the
    # configured dev prompt), not a test stub.
    return {
        "repo_dir": clone,
        # The REAL repo prompts: the ops session must receive the real
        # rendered ops playbook (`prompt_ops.md`, the sibling of the
        # configured dev prompt), not a test stub.
        "prompt": REPO_ROOT / "prompts" / "prompt.md",
        "base_branch": "main",
        "source_repos": [REPO],
        "workspace_root": clone.parent,
        "context_files": [],
        "skills": [],
    }


def worktree_for(clone: Path, run_id: str) -> Path:
    return (
        clone / ".worktrees"
        / f"orbi-{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}-{run_id}"
    )


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_e2e_pure_ops_ticket_executes_merges_and_delivers_evidence(
    clone, gh_world, tmp_path, monkeypatch, caplog,
):
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_OPS)
    caplog.set_level("INFO")

    result = runner.process_issue(ops_issue(), config_for(clone), REPO)

    run_id = runner.current_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}", run_id)
    # A pure-ops delivery is complete: no PR, no delivery wait.
    assert result == runner.IssueResult("ops", None)

    state = json.loads(gh_world.read_text(encoding="utf-8"))
    # 1. The session REALLY executed the authorized merge: the fake gh
    #    recorded it and the bare origin's main really advanced onto the
    #    beta work (an observable git-state change, not a flag).
    assert state["prs"]["7"]["state"] == "MERGED"
    subprocess.run(
        ["git", "-C", str(clone), "fetch", "origin"],
        capture_output=True, text=True, check=True,
    )
    origin_main = git(clone, "log", "--oneline", "origin/main")
    assert "Merge PR #7 (ops)" in origin_main
    assert "beta feature" in origin_main

    # 2. The evidence comment landed on the ticket: run marker, the
    #    executed commands and their REAL output (the merge commit
    #    subject the verify step observed). The runner's own scene
    #    comments carry the marker too — filter to the agent's report.
    evidence = [
        c for c in state["comments"]
        if "# Ops evidence" in c["body"]
    ]
    assert len(evidence) == 1
    assert f"<!-- orbi:run={run_id} -->" in evidence[0]["body"]
    assert "gh pr merge 7 --repo owner/repo --merge" in evidence[0]["body"]
    assert "exit=0" in evidence[0]["body"]
    assert "Merge PR #7 (ops)" in evidence[0]["body"]
    assert f"run_id={run_id}" in evidence[0]["body"]

    # 3. The runner closed the ticket and removed the claim label.
    issue = state["issues"][str(ISSUE_NUMBER)]
    assert issue["state"] == "CLOSED"
    assert "ai-in-progress" not in issue["labels"]

    # 4. No delivery PR was created and the delivery branch was never
    #    pushed: a pure-ops ticket takes no PR ceremony.
    assert list(state["prs"]) == ["7"]
    branches = git(clone, "ls-remote", "--heads", "origin")
    assert f"orbi/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}" \
        not in branches

    # 5. The session received the REAL ops playbook (the ops agent's
    #    system prompt is rendered from prompts/prompt_ops.md).
    prompt = (tmp_path / "pi-system-prompt.txt").read_text(
        encoding="utf-8",
    )
    assert "Orbi Ops Agent" in prompt
    assert "The ticket is the authorization" in prompt

    # 6. The run-scoped worktree stays as the run's evidence.
    assert worktree_for(clone, run_id).is_dir()


def test_e2e_ops_ticket_with_code_change_takes_the_pr_ceremony(
    clone, gh_world, tmp_path, monkeypatch,
):
    install_fake_pi(monkeypatch, tmp_path, FAKE_PI_OPS_COMMIT)

    result = runner.process_issue(ops_issue(), config_for(clone), REPO)

    run_id = runner.current_run_id()
    # The committed ops code is delivered exactly like a dev ticket:
    # the Runner pushed and opened the PR.
    assert result == runner.IssueResult("pr", PR_URL)

    state = json.loads(gh_world.read_text(encoding="utf-8"))
    branch = f"orbi/{REPO.replace('/', '-')}-issue-{ISSUE_NUMBER}"
    # The task branch really reached the remote.
    assert branch in git(clone, "ls-remote", "--heads", "origin")
    # Exactly the delivery PR exists, created by the RUNNER with the
    # run marker and the `Fixes #N` close keyword.
    delivery_prs = [
        pr for pr in state["prs"].values() if pr["headRefName"] == branch
    ]
    assert len(delivery_prs) == 1
    assert f"<!-- orbi:run={run_id} -->" in delivery_prs[0]["body"]
    assert f"Fixes #{ISSUE_NUMBER}" in delivery_prs[0]["body"]
    assert delivery_prs[0]["baseRefName"] == "main"
    assert delivery_prs[0]["headRefOid"] == git(
        clone, "rev-parse", f"origin/{branch}",
    )
