"""E2E concurrency tests (Issue #39).

Real runner processes (``orbi.runner``) run against a local bare
origin plus a stateful fake ``gh`` executable on PATH, while a fake ``pi``
executable records every invocation. These prove the acceptance criteria:

- with ``max_concurrency = 1`` a second concurrent runner logs
  ``capacity_full``, claims no Issue, changes no label and never calls Pi;
- the slot is held ONLY while a Pi runs (Issue #788): the implement tick
  ends when the PR opens and RELEASES the slot; the review runs as the
  next tick's RESUME_REVIEW step, which takes a slot again for exactly
  its Pi session; while that review holds the slot a concurrent runner
  is denied and while it is LIVE the resumable scan skips the delivery;
- the review session fixes findings IN THE SAME SESSION (Issue #82):
  one review Pi per delivery (no cold-start fixer, no third review),
  and the runner re-freezes the fixed head before the merge gate;
- a PR closed without a merge is a terminal failure: the Issue is marked
  ``ai-blocked`` and the slot is released;
- with ``max_concurrency = 2`` two runners hold two different slots and
  claim two different Issues while a third runner is rejected;
- the review of a FREE opened-PR delivery is not starved by an in-flight
  delivery (Issue #809): the holder names its (repo, issue) in the slot
  file, the concurrent tick skips exactly that delivery and reviews the
  free one instead of falling through to a fresh claim;
- a SIGKILLed runner releases its slot automatically (the kernel owns
  the flock lock), so an abnormal exit never deadlocks the machine.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import git

# The e2e runner SUBPROCESS executes the real pre-start checks against
# this systemd-shaped fixture world; on macOS the launchd branch
# (Issue #849) needs the launchd deployment surface it does not
# provide. Linux CI keeps the scenes authoritative.
_runner_e2e_linux_only = pytest.mark.skipif(
    sys.platform == "darwin",
    reason=(
        "the e2e runner subprocess runs the real pre-start checks over "
        "a systemd-shaped fixture world; the macOS launchd branch "
        "(Issue #849) needs the launchd deployment surface it does not "
        "provide"
    ),
)

from orbi import systemd_deploy
from orbi.delivery_scene import RunContext

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = "owner/repo"
# Issue #168 src layout: the Runner is the package module
# `orbi.runner`, started with `-m` and the checkout's `src/`
# on PYTHONPATH (the same seam the pytest `pythonpath` ini uses for
# the in-process tests).
RUNNER_MODULE = "orbi.runner"

# Stateful fake ``gh``: one JSON file (ORBI_FAKE_GH_STATE) holds the Issue
# labels, the comments (with author association) and the PR state. It
# answers exactly the commands the runner runs.
FAKE_GH = """#!/usr/bin/env python3
import fcntl, json, os, subprocess, sys, tempfile
from pathlib import Path

state_path = Path(os.environ["ORBI_FAKE_GH_STATE"])
args = sys.argv[1:]

# All state access (this fake, and the test process) serializes on this
# lock: no lost updates, no torn reads. The lock is released when this
# process exits, however it exits.
lock_fd = os.open(str(state_path.parent / "gh-state.lock"),
                  os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(lock_fd, fcntl.LOCK_EX)

state = json.loads(state_path.read_text(encoding="utf-8"))


def save():
    # Atomic replace: a concurrent reader must never observe a torn
    # write; the lock above serializes the read-modify-write.
    fd, tmp = tempfile.mkstemp(dir=str(state_path.parent), prefix="gh-state-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state))
    os.replace(tmp, state_path)


def git(*cmd):
    return subprocess.run(
        ["git", *cmd], capture_output=True, text=True, check=True,
    ).stdout.strip()


def match_issue(labels, search):
    # `label:a,b` is GitHub's OR within one qualifier (Issue #70 scans
    # `ai-fix-needed` OR `ai-pr-opened`); space-separated qualifiers
    # are AND.
    required = [
        t[6:].split(",")
        for t in search.split() if t.startswith("label:")
    ]
    excluded = [t[7:] for t in search.split() if t.startswith("-label:")]
    return all(any(r in labels for r in group) for group in required) \
        and not any(e in labels for e in excluded)


if args[:2] == ["issue", "list"]:
    search = args[args.index("--search") + 1]
    # Issue #809: the resumable scan asks for a page (max_concurrency+1)
    # so it can skip held deliveries and still reach a free one — the
    # fake must honor the limit, not answer one issue regardless.
    limit = int(args[args.index("--limit") + 1])
    out = [
        # `--state open` only ever returns open Issues, and the real
        # payload carries `state` (the resumable scan checks it).
        {"number": int(num), "state": "OPEN",
         "title": issue["title"],
         "body": issue.get("body", "")}
        for num, issue in sorted(
            state["issues"].items(), key=lambda kv: int(kv[0])
        )
        if match_issue(issue["labels"], search)
    ]
    print(json.dumps(out[:limit]))
elif args[:2] == ["issue", "edit"]:
    num = args[2]
    if "--add-label" in args:
        label = args[args.index("--add-label") + 1]
        if label not in state["issues"][num]["labels"]:
            state["issues"][num]["labels"].append(label)
    if "--remove-label" in args:
        label = args[args.index("--remove-label") + 1]
        if label in state["issues"][num]["labels"]:
            state["issues"][num]["labels"].remove(label)
    save()
elif args[:2] == ["issue", "comment"]:
    state["comments"].append(
        {"issue": args[2], "body": args[args.index("--body") + 1],
         "authorAssociation": "OWNER"}
    )
    save()
elif args[:2] == ["issue", "view"]:
    num = args[2]
    if args[-1] == "state":
        # The pre-PR closeout reads the source Issue state (Issue
        # #746): open in these scenes.
        print(json.dumps(
            {"state": state["issues"][num].get("state", "OPEN")}
        ))
    elif args[-1] == "body":
        print(json.dumps(
            {"body": state["issues"][num].get("body", "")}
        ))
    elif args[-1] == "labels":
        print(json.dumps({
            "labels": [
                {"name": label}
                for label in state["issues"][num]["labels"]
            ]
        }))
    else:
        comments = [
            {"body": c["body"], "authorAssociation": c.get(
                "authorAssociation", "OWNER"
            )}
            for c in state["comments"] if c["issue"] == num
        ]
        print(json.dumps({"comments": comments}))
elif args[:2] == ["pr", "list"]:
    branch = args[args.index("--head") + 1]
    head = git("rev-parse", "HEAD")
    # Stable delivery branch shape: orbi-owner-repo-issue-<n>.
    issue_num = branch.split("-issue-", 1)[1]
    # Standalone verification may run before the delivery path writes its
    # state file; normal delivery calls still provide their real run id.
    state_path = os.path.join(os.getcwd(), ".orbi", "run-state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            run_id = json.load(f)["run_id"]
    except FileNotFoundError:
        # The ref hammer supplies this fixed id to verify_pr.
        run_id = "01234567"
    print(json.dumps([{
        "number": 99,
        "url": "https://github.com/owner/repo/pull/99",
        "baseRefName": "main",
        "baseRefOid": git("rev-parse", "origin/main"),
        "headRefName": branch,
        "headRefOid": head,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
        "body": f"<!-- orbi:run={run_id} -->\\n\\nFixes #{issue_num}\\n\\nPlan for {branch}",
    }]))
elif args[:2] == ["pr", "comment"]:
    state.setdefault("pr_comments", []).append(
        {"pr": args[2], "body": args[args.index("--body") + 1]}
    )
    save()
elif args[:2] == ["pr", "view"]:
    # The merge gate and confirm_merged read the full PR state: the
    # head is the current HEAD of the delivery branch (the fake world
    # has no separate PR object), and the merge is recorded when the
    # test (or the runner via `pr merge`) marks it MERGED. The state
    # is PER BRANCH: concurrent deliveries (two runners, two issues)
    # each see their own PR state; the global `pr_state` remains the
    # default for branches without a recorded state.
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    pr_state = state.setdefault("pr_states", {}).get(
        branch, state.get("pr_state", "OPEN"),
    )
    print(json.dumps({
        "state": pr_state,
        "mergeable": "MERGEABLE",
        "headRefOid": git("rev-parse", "HEAD"),
        "mergedAt": "2026-08-25T00:00:00Z"
        if pr_state == "MERGED" else None,
        "mergeCommit": (
            {"oid": git("rev-parse", "HEAD")}
            if pr_state == "MERGED" else None
        ),
    }))
elif args[:2] == ["pr", "merge"]:
    # The fake GitHub merge: the real `gh pr merge` lands the head on
    # the base (a merge commit when the base advanced in the meantime),
    # so the fake merges the delivery head (the PR head is the
    # delivery branch's HEAD) into origin/main in a throwaway clone.
    # The runner then confirms via `pr view` and syncs the deployment
    # checkout.
    import tempfile
    head = git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "clone", git("remote", "get-url",
                                             "origin"), td],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "config",
                        "user.email", "pilot@test.local"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "config", "user.name", "Pilot"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "fetch", "origin"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "merge", "--no-ff",
                        "-m", "Merge PR (fake)", head],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", td, "push", "origin",
                        "HEAD:main"],
                       check=True, capture_output=True)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    state.setdefault("pr_states", {})[branch] = "MERGED"
    state["merged_head"] = head
    save()
elif args[:1] == ["api"]:
    if "check-runs" in args[1]:
        print(json.dumps([{
            "name": "tests", "status": "completed",
            "conclusion": "success",
        }]))
    else:
        # The progress publisher (Issue #18) keeps the single per-run
        # comment via gh api: list (GET) and create (POST) on
        # repos/<owner>/<repo>/issues/<n>/comments, update (PATCH) on
        # repos/<owner>/<repo>/issues/comments/<id> — the GitHub update
        # route carries no issue number (Issue #58).
        parts = args[1].split("/")
        num = parts[4] if parts[4].isdigit() else None
        if "--method" in args:
            method = args[args.index("--method") + 1]
            body = args[args.index("--field") + 1][len("body="):]
            if method == "POST":
                cid = max([c.get("id", 0) for c in state["comments"]] or [0]) + 1
                state["comments"].append(
                    {"issue": num, "body": body, "id": cid,
                     "authorAssociation": "OWNER"}
                )
                save()
                print(json.dumps({"id": cid, "body": body}))
            elif method == "PATCH":
                # Update route: repos/<owner>/<repo>/issues/comments/<id>
                cid = int(parts[5])
                for c in state["comments"]:
                    if c.get("id") == cid:
                        c["body"] = body
                save()
                print(json.dumps({"id": cid, "body": body}))
        else:
            print(json.dumps([
                {"id": c.get("id", i + 1), "body": c["body"],
                 "authorAssociation": c.get("authorAssociation", "OWNER")}
                for i, c in enumerate(state["comments"])
                if c["issue"] == num
            ]))
else:
    raise SystemExit(f"unexpected gh command: {args}")
"""

# Fake ``pi``: records one line per invocation, then stays busy for a while
# so concurrent runners overlap while it runs. The review role (system
# prompt "INDEPENDENT REVIEW") simulates the Issue #82 contract: the
# independent review session fixes findings IN THE SAME SESSION — the
# first review of a run finds a major issue, commits and pushes the fix
# to the task branch (the PR head advances), and ends with a clean
# verdict covering the fixed head (the runner re-freezes and merges it).
# A second review of the same run (a restart inside the wait loop) is
# simply clean.
FAKE_PI = """#!/usr/bin/env python3
import json, os, subprocess, sys, time
log = os.environ.get("ORBI_FAKE_PI_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"pi {os.getpid()}\\n")
# Optional test gate: hold BEFORE any work until the file appears, so a
# test can pin the "delivery still running" scene deterministically.
gate = os.environ.get("ORBI_FAKE_PI_GATE")
if gate:
    while not os.path.exists(gate):
        time.sleep(0.05)
time.sleep(1.0)
system_prompt = sys.argv[sys.argv.index("--system-prompt") + 1]
if "INDEPENDENT REVIEW" in system_prompt:
    run_id = system_prompt.split("run_id=")[1].split()[0]
    # Issue #591: the verdict names the head it covers (the Runner
    # checks it against the PR head before merging) — the fake reviewer
    # states the same fact via git rev-parse HEAD.
    def reviewed_head():
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    marker = os.path.join(os.getcwd(), f".orbi-review-{run_id}")
    first_review = not os.path.exists(marker)
    if first_review:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("reviewed")
        # Initial review: one major finding, narrated as PROSE (Issue
        # #774: the review reply carries exactly ONE machine-readable
        # verdict line — an earlier `REVIEW_VERDICT` line would make the
        # output two conflicting verdicts, which the parser refuses).
        print("Major finding at e2e: first review finding — fixing in "
              "this session.")
        # Fixed in this same session: commit the fix (the PR head
        # advances; the runner must re-freeze and merge the fixed head,
        # not the frozen one). The fix content is unique per
        # invocation: a later delivery's base already carries an
        # earlier delivery's fix file.
        with open(os.path.join(os.getcwd(), "review-fix.txt"), "a",
                  encoding="utf-8") as handle:
            handle.write(f"fixed in the review session (pid {os.getpid()} "
                         f"at {time.time()})\\n")
        subprocess.run(["git", "add", "review-fix.txt"], check=True)
        subprocess.run(
            ["git", "-c", "user.email=pilot@test.local",
             "-c", "user.name=Pilot", "commit",
             "-m", "fix: in-session review fix"],
            check=True,
        )
    # The live-delivery regression test uses a bounded test-only gate:
    # issue 7's reviewer waits until issue 8 has passed its initial PR
    # verification, so a fake merge cannot advance main in that narrow
    # interval and mask the resumable-scan contract.
    review_gate = os.environ.get("ORBI_FAKE_PI_REVIEW_GATE")
    if review_gate:
        with open(f"{review_gate}.waiting", "w", encoding="utf-8"):
            pass
        deadline = time.monotonic() + 30
        while not os.path.exists(review_gate):
            if time.monotonic() >= deadline:
                raise SystemExit("timed out waiting for test review gate")
            time.sleep(0.05)
    # A head behind the latest base is fixed in-session too (Issue
    # #82), on EVERY review round: the base may advance between rounds.
    # Absorb origin/main, resolve, retest, push. The fetch updates the
    # shared remote-tracking ref, so it runs under the base-sync lock
    # (Issue #171): the same `flock <lock> git fetch origin main` the
    # real review prompt instructs (the lock path comes from the
    # rendered prompt).
    lock = system_prompt.split("lock=")[1].split()[0]
    subprocess.run(["flock", lock, "git", "fetch", "origin", "main"],
                   check=True)
    behind = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
        capture_output=True,
    )
    if behind.returncode != 0:
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "-m",
             "merge main (review session)", "origin/main"],
            capture_output=True,
        )
        if merge.returncode != 0:
            with open(os.path.join(os.getcwd(), "review-fix.txt"), "a",
                      encoding="utf-8") as handle:
                handle.write(f"conflict resolved (pid {os.getpid()})\\n")
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(
                ["git", "-c", "user.email=pilot@test.local",
                 "-c", "user.name=Pilot", "commit", "--no-edit"],
                check=True,
            )
    subprocess.run(["git", "push", "origin", "HEAD"], check=True)
    # Final verdict after the in-session fix: clean, bound to the
    # pushed head (Issue #591).
    print('REVIEW_VERDICT ' + json.dumps({
        "verdict": "pass", "head": reviewed_head(),
        "blockers": 0, "majors": 0,
        "minors": 0, "findings": [],
    }))
else:
    # Implementer session (Issue #186): the agent commits the delivery;
    # the Runner pushes the task branch and opens the PR (the fake pi
    # must NOT push or create the PR). A killed runner can leave the
    # first pi orphaned in its startup sleep, so the commit retries over
    # a concurrent index.lock holder (the resumed run's own pi).
    with open(os.path.join(os.getcwd(), f"plan-{os.getpid()}.md"), "w",
              encoding="utf-8") as handle:
        handle.write(f"plan (pid {os.getpid()})\\n")
    for attempt in range(20):
        try:
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(
                ["git", "-c", "user.email=pilot@test.local",
                 "-c", "user.name=Pilot", "commit",
                 "-m", f"plan for the run (pid {os.getpid()})"],
                check=True,
            )
            break
        except subprocess.CalledProcessError:
            if attempt == 19:
                raise
            time.sleep(0.1)
"""


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """Local bare origin plus a clone with one commit on main.

    The clone's `origin` remote is the SSH URL of the task repo
    (Issue #114: the pre-start transport check requires the checkout's
    remote to be SSH for the configured source repo); a
    `url.<base>.insteadOf` rewrite (git-config(1)) keeps the git data
    plane local: fetch/ls-remote of the SSH URL resolve to the bare
    origin without any network.
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
    # The deployment checkout's transport (Issue #114): the origin
    # remote is the SSH URL of the task repo; the insteadOf rewrite
    # keeps the data plane local (no network in the e2e world).
    git(clone, "remote", "set-url", "origin",
        f"git@github.com:{REPO}.git")
    git(clone, "config", f"url.{origin}.insteadOf",
        f"git@github.com:{REPO}.git")
    # The deployment checkout carries the unit templates (Issue #103):
    # the pre-start drift check compares them against the installed
    # units, so a synthetic clone without them could never pass it.
    shutil.copytree(REPO_ROOT / "systemd", clone / "systemd")
    # The deployment checkout carries the packaging input (Issue #158):
    # the pre-start CLI install refresh fingerprints the checkout's
    # `pyproject.toml`.
    shutil.copyfile(REPO_ROOT / "pyproject.toml", clone / "pyproject.toml")
    (clone / "a.txt").write_text("a", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "first")
    git(clone, "push", "origin", "main")
    # The e2e world's tool env is UP TO DATE (Issue #158): the
    # last-install state is pre-recorded for the clone's packaging
    # input, so the pre-start refresh is a no-op and never runs a real
    # `uv tool install` against the synthetic clone (the refresh's own
    # behavior is covered by tests/test_cli_install.py and the wiring
    # tests in tests/test_bootstrap_runner.py).
    fingerprint = hashlib.sha256(
        (clone / "pyproject.toml").read_bytes(),
    ).hexdigest()
    state_dir = clone / ".orbi"
    state_dir.mkdir()
    (state_dir / "cli-install.json").write_text(
        json.dumps({"pyproject_sha256": fingerprint}), encoding="utf-8",
    )
    return clone


def install_fakes(tmp_path: Path) -> Path:
    """Put the fake gh and pi executables first on PATH; return the dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("gh", FAKE_GH), ("pi", FAKE_PI)):
        fake = bin_dir / name
        fake.write_text(script, encoding="utf-8")
        fake.chmod(0o755)
    return bin_dir


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically under the state lock: readers never observe
    a torn write and no concurrent update is lost."""
    import fcntl
    import tempfile

    lock_fd = os.open(str(path.parent / "gh-state.lock"),
                      os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="state-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
        os.replace(tmp, path)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def write_state(
    state_path: Path, issues: dict[str, list[str]],
    pr_state: str = "OPEN",
) -> None:
    state = {
        "issues": {
            str(number): {
                "title": f"issue {number}", "body": f"body {number}",
                "labels": list(labels),
            }
            for number, labels in issues.items()
        },
        "comments": [],
        "pr_state": pr_state,
    }
    atomic_write_json(state_path, state)


def read_state(state_path: Path) -> dict:
    """Read the state file; the writers replace it atomically (under the
    state lock), so a torn read cannot happen."""
    return json.loads(state_path.read_text(encoding="utf-8"))


def write_config(
    clone: Path, tmp_path: Path, max_concurrency: int,
) -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(
        "You are the delivery agent.\n"
        "- Delivery base branch: `{{BASE_BRANCH}}`\n"
        "- Delivery base SHA: `{{BASE_SHA}}`\n"
        "- Base sync lock: `{{BASE_SYNC_LOCK}}`\n"
        "- Run id: `{{RUN_ID}}`\n",
        encoding="utf-8",
    )
    # validate_config requires the review prompt to exist as well (the
    # Runner runs the independent review itself). The review prompt
    # carries the run_id so the fake pi can key its
    # first-review-findings state per run, and the base-sync lock path
    # (Issue #171) so the fake pi's base-absorb fetch runs under the
    # SAME lock the real review prompt instructs.
    (tmp_path / "prompt_review.md").write_text(
        "INDEPENDENT REVIEW\nrun_id={{RUN_ID}}\n"
        "lock={{BASE_SYNC_LOCK}}\n", encoding="utf-8",
    )
    config = tmp_path / f"orbi-{max_concurrency}.toml"
    review_prompt = tmp_path / "prompt_review.md"
    config.write_text(
        f'source_repos = ["{REPO}"]\n'
        f'repo_dir = "{clone}"\n'
        f'workspace_root = "{tmp_path}"\n'
        f'prompt = "{prompt}"\n'
        f'prompt_review = "{review_prompt}"\n'
        f'max_concurrency = {max_concurrency}\n'
        # Issue #525: these e2e runners run the REAL gate in a spawned
        # subprocess, and its import source is THIS worktree — a task
        # branch whose HEAD is by construction not origin/main. The
        # documented escape hatch downgrades the (correctly detected)
        # staleness to a warning; the gate still runs and probes.
        f'allow_stale_runner = true\n',
        encoding="utf-8",
    )
    return config


# Every started runner is tracked so a failing test never leaves a live
# runner (and its slot lock) behind.
_RUNNING: list[subprocess.Popen] = []


def install_deployed_units(unit_dir: Path, repo_dir: Path) -> None:
    """Simulate the deployed machine: the repo templates installed as
    the user units (the idempotent install the README documents). The
    templates are rendered exactly as `orbi install-units` does — the
    {{ORBI_REPO_DIR}} placeholder replaced with the checkout path — so
    the pre-start drift check (which compares against the rendered
    template) sees a clean deployment."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in ("orbi@.service", "orbi@.timer"):
        template = (REPO_ROOT / "systemd" / name).read_text(encoding="utf-8")
        rendered = systemd_deploy.render_unit_template(template, repo_dir)
        (unit_dir / name).write_bytes(rendered.encode("utf-8"))


def _drain(pipe, sink) -> None:
    """Drain one pipe into `sink` in the background: a 64 KB OS pipe
    buffer fills after a few hundred log lines and then BLOCKS the
    runner mid-write — a long-lived runner whose stderr nobody reads
    freezes (the 2026-09-11 capacity-test stall). The sink keeps the
    text available for the test's final assertions."""
    for line in iter(pipe.readline, ""):
        sink.write(line)


def start_runner(
    config_path: Path, bin_dir: Path,
    state_path: Path, pi_log: Path,
    unit_dir: Path | None = None,
    review_gate: Path | None = None,
    pi_gate: Path | None = None,
    drain_stderr: bool = False,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["ORBI_FAKE_GH_STATE"] = str(state_path)
    env["ORBI_FAKE_PI_LOG"] = str(pi_log)
    if review_gate is not None:
        env["ORBI_FAKE_PI_REVIEW_GATE"] = str(review_gate)
    if pi_gate is not None:
        env["ORBI_FAKE_PI_GATE"] = str(pi_gate)
    # The pre-start drift check (Issue #103) reads the installed units
    # from here: a clean deployment by default (the templates as
    # installed), or an explicit dir for the drift scenarios.
    if unit_dir is None:
        unit_dir = config_path.parent / "unit-dir"
        install_deployed_units(unit_dir, config_path.parent / "clone")
    env["ORBI_UNIT_DIR"] = str(unit_dir)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    process = subprocess.Popen(
        ["/usr/bin/python3", "-m", RUNNER_MODULE, "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        cwd=REPO_ROOT,
    )
    if drain_stderr:
        process.drained_stderr = io.StringIO()
        threading.Thread(
            target=_drain, args=(process.stderr, process.drained_stderr),
            daemon=True,
        ).start()
    _RUNNING.append(process)
    return process


@pytest.fixture(autouse=True)
def _cleanup_runners():
    yield
    for process in list(_RUNNING):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    _RUNNING.clear()


def wait_for(predicate, timeout: float = 60.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_for_pid_exit(pid: int, timeout: float = 30.0) -> None:
    """Wait until `pid` is gone (reaped) — models the systemd cgroup
    taking a killed runner's Pi child with it (a raw SIGKILL of the
    runner alone orphans the child, which would otherwise commit into
    the resumed run's worktree at an unpredictable moment)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for pid {pid} to exit")


def test_wait_for_raises_when_condition_never_holds():
    with pytest.raises(AssertionError, match="timed out waiting for the moon"):
        wait_for(lambda: False, timeout=0.1, what="the moon")


def test_wait_for_pid_exit_times_out_on_a_live_pid():
    # This test's own pid is alive: the wait must time out instead of
    # hanging (the same contract as `wait_for`).
    with pytest.raises(AssertionError, match="timed out waiting for pid"):
        wait_for_pid_exit(os.getpid(), timeout=0.1)


@_runner_e2e_linux_only
def test_cleanup_fixture_kills_a_leftover_runner(clone, tmp_path):
    """A test that leaves a live runner behind must not leak it (or its
    slot lock): the autouse fixture kills it after the test."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)
    runner_proc = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["7"]["labels"],
        what="runner to claim the issue",
    )
    # The runner is alive (waiting for the PR to merge): the fixture
    # must kill it when this test ends.
    assert runner_proc.poll() is None


def pi_invocations(pi_log: Path) -> list[str]:
    if not pi_log.exists():
        return []
    return pi_log.read_text(encoding="utf-8").splitlines()


def slot_files(clone: Path) -> list[Path]:
    slot_dir = clone / ".orbi" / "slots"
    return sorted(slot_dir.glob("slot-*")) if slot_dir.is_dir() else []


def slots_held(clone: Path, capacity: int = 1) -> list:
    """Return the lock-state occupancy: the lock, not the file, is held."""
    from orbi import pilot_slots

    return pilot_slots.slot_occupancy(
        clone / ".orbi" / "slots", capacity,
    )


def set_pr_state(state_path: Path, pr_state: str) -> None:
    state = read_state(state_path)
    state["pr_state"] = pr_state
    atomic_write_json(state_path, state)


@_runner_e2e_linux_only
def test_capacity_one_slot_serves_the_review_tick(
    clone, tmp_path,
):
    """Issue #788: the slot is held only while a Pi RUNS. The implement
    tick ends when the PR opens — the slot is released — and the NEXT
    tick (a second runner process here) takes the slot again, classifies
    the delivery RESUME_REVIEW and runs the independent review (fixing
    IN THE SESSION, Issue #82) and the merge. While that review Pi is on
    the slot, a third concurrent runner is denied (`capacity_full`) and
    claims nothing — the #39 single-slot invariant now guards exactly the
    Pi sessions."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    # Tick 1: implement issue 7, open the PR, release the slot, exit.
    first = start_runner(config, bin_dir, state, pi_log)
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]
    assert slots_held(clone) == [(1, None)], (
        "the implement tick releases the slot when the PR opens"
    )

    # Tick 2: the resumable scan classifies issue 7 RESUME_REVIEW; the
    # review gate holds the review Pi mid-run so the contention scene is
    # deterministic: the slot is held exactly while the review runs.
    review_gate = tmp_path / "review-gate"
    second = start_runner(
        config, bin_dir, state, pi_log, review_gate=review_gate,
    )
    review_waiting = review_gate.with_suffix(".waiting")
    wait_for(
        review_waiting.exists, timeout=30,
        what="the resumed review to hold the slot mid-run",
    )
    held = slots_held(clone)
    assert held[0][0] == 1 and held[0][1] is not None, (
        "the review tick holds the slot while the review Pi runs"
    )

    # A third concurrent runner is denied while the review holds the
    # slot: it claims nothing (Issue 8 stays ai-ready).
    third = start_runner(config, bin_dir, state, pi_log)
    out, err = third.communicate(timeout=60)
    assert third.returncode == 0, err
    assert "capacity_full" in err
    assert read_state(state)["issues"]["8"]["labels"] == ["ai-ready"]

    # Release the review: the finding is fixed IN THE SESSION (Issue
    # #82), the head is re-frozen and merged by the SAME holder.
    review_gate.write_text("go", encoding="utf-8")
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=180,
        what="the resumed review to fix in-session and merge",
    )
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "delivery_auto_merged" in err
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the merge"
    )
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" not in snap["issues"]["7"]["labels"]
    # One Pi invocation per phase of the SAME run: implement (tick 1),
    # review (tick 2, the review fixes in-session) — never a new claim
    # of another Issue, never a cold-start fixer.
    assert len(pi_invocations(pi_log)) == 2
    started = [
        c for c in snap["comments"] if "Orbi started Pi:" in c["body"]
    ]
    assert [c["issue"] for c in started] == ["7"]
    bodies = [c["body"] for c in snap["comments"]]
    assert any("Orbi merged PR:" in b for b in bodies)
    # The in-session fix landed on the delivery branch: origin/main
    # carries the review fix file, so the runner merged the RE-FROZEN
    # head (the fixed one), not the frozen head (Issue #82).
    assert "review-fix.txt" in git(
        clone, "ls-tree", "--name-only", "origin/main",
    )


@_runner_e2e_linux_only
def test_capacity_one_closed_unmerged_pr_releases_slot_and_blocks_issue(
    clone, tmp_path,
):
    """A PR closed without a merge is a terminal failure handled by the
    resume classification (Issue #788: the implement tick already ended
    when the PR opened): the next tick marks the Issue ai-blocked and
    releases the slot (no permanent hold, no in-tick waiting)."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    # Tick 1: open the PR for issue 7; the runner exits, slot released.
    first = start_runner(config, bin_dir, state, pi_log)
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"]
    assert slots_held(clone) == [(1, None)]

    # The PR closes WITHOUT a merge while nobody holds the slot.
    set_pr_state(state, "CLOSED")

    # Tick 2: the resume classification sees the closed PR: ai-blocked
    # with the failure comment, exit 0, slot free.
    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "delivery_closed_unmerged" in err
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the failure"
    )
    snap = read_state(state)
    assert "ai-blocked" in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" not in snap["issues"]["7"]["labels"]
    failure = [
        c for c in snap["comments"] if "Orbi failed:" in c["body"]
    ]
    assert len(failure) == 1
    assert "closed without a merge" in failure[0]["body"]

    # The next tick claims the NEXT Issue and opens its PR; the tick
    # after that resumes it and merges.
    set_pr_state(state, "OPEN")
    third = start_runner(config, bin_dir, state, pi_log)
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"]
    assert slots_held(clone) == [(1, None)]
    set_pr_state(state, "MERGED")
    fourth = start_runner(config, bin_dir, state, pi_log)
    out, err = fourth.communicate(timeout=120)
    assert fourth.returncode == 0, err
    assert "capacity_full" not in err
    assert "delivery_merged" in err
    assert slots_held(clone) == [(1, None)]


@_runner_e2e_linux_only
def test_capacity_two_allows_two_runners_and_rejects_third(clone, tmp_path):
    """Two slots: two different Issues in parallel, third runner rejected.
    Issue #788: after the PRs open, both implement ticks END and release
    their slots — the reviews then run as separate resume ticks, each
    taking a free slot for exactly its Pi session."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 2)
    # The pi gates hold each delivery before any fake-pi work until the
    # capacity scene below is fully asserted: without them the deliveries
    # race ahead (ai-pr-opened within ~1s) and the strict in-progress
    # assertions below become a timing lottery (2026-09-11 flake). Two
    # SEPARATE gates: the deliveries are then released IN ORDER, because
    # the fake review fixes append to one shared file — two concurrent
    # merges of that file conflict, while an ordered delivery lets the
    # second review absorb the first merged base (the real review prompt
    # does the same base absorb).
    pi_gate_7 = tmp_path / "pi-gate-7"
    pi_gate_8 = tmp_path / "pi-gate-8"

    first = start_runner(
        config, bin_dir, state, pi_log, pi_gate=pi_gate_7, drain_stderr=True,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to claim issue 7",
    )

    second = start_runner(
        config, bin_dir, state, pi_log, pi_gate=pi_gate_8, drain_stderr=True,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["8"]["labels"],
        what="second runner to claim issue 8",
    )

    # Both runners hold two DIFFERENT slots with two different holder PIDs.
    slots = slot_files(clone)
    assert [path.name for path in slots] == ["slot-1", "slot-2"]
    pids = {path.read_text(encoding="utf-8").strip() for path in slots}
    assert len(pids) == 2

    third = start_runner(config, bin_dir, state, pi_log)
    out, err = third.communicate(timeout=60)
    assert third.returncode == 0, err
    assert "capacity_full" in err
    # The third runner claimed nothing: both Issues keep their labels.
    snap = read_state(state)
    assert snap["issues"]["7"]["labels"] == ["ai-ready", "ai-in-progress"]
    assert snap["issues"]["8"]["labels"] == ["ai-ready", "ai-in-progress"]
    # Scene pinned deterministically: release the deliveries IN ORDER —
    # issue 7 fully opens its PR first, then issue 8 follows.
    pi_gate_7.write_text("go", encoding="utf-8")
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="issue 7 PR to open",
    )
    pi_gate_8.write_text("go", encoding="utf-8")
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        what="issue 8 PR to open",
    )
    # Issue #788: with both PRs open, both implement ticks are DONE —
    # the slots are free again (they were held only for the Pi runs).
    for runner in (first, second):
        runner.wait(timeout=120)
        assert runner.returncode == 0, (
            runner.drained_stderr.getvalue()
        )
    assert slots_held(clone, 2) == [(1, None), (2, None)]

    # The review of issue 7 runs as its own resume tick (a NEW runner
    # process taking the freed slot): the finding is fixed in-session
    # (Issues #34/#82) and the PR merges.
    resume_7 = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=180,
        what="the first resume tick to review and merge issue 7",
    )
    out, err = resume_7.communicate(timeout=120)
    assert resume_7.returncode == 0, err
    assert "delivery_auto_merged" in err

    # The review of issue 8 follows as the next resume tick: its review
    # session absorbs the freshly advanced base in-session (the fake
    # review does the same base absorb the real prompt instructs).
    resume_8 = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["8"]["labels"],
        timeout=180,
        what="the second resume tick to review and merge issue 8",
    )
    out, err = resume_8.communicate(timeout=120)
    assert resume_8.returncode == 0, err
    assert slots_held(clone, 2) == [(1, None), (2, None)]
    # One Pi per phase of each delivery (implement, review — the review
    # fixes in-session, Issue #82): exactly four invocations, none a
    # duplicate claim.
    assert len(pi_invocations(pi_log)) == 4
    started = [
        c for c in read_state(state)["comments"]
        if "Orbi started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-merged" in snap["issues"]["8"]["labels"]


def _run_ref_hammer(
    clone: Path, tmp_path: Path, monkeypatch,
    rounds: int = 5, workers: int = 4,
    base_sha: str | None = None,
) -> list[str]:
    """Hammer the runner's ref-writing paths on the shared checkout.

    Four worker threads run the runner's ref-writing paths (the same
    calls the Runners make) while an advancer thread keeps pushing
    commits to the BARE origin — the #105 scene (a delivery merge
    landing on origin/main) with a wide window: every fetch must copy
    the new objects while holding git's optimistic ref expectation.
    Returns the collected operation errors (empty when every operation
    succeeded). ``base_sha`` overrides the worktree base (a bad SHA
    makes ``create_worktree`` fail — the error-collection path).
    """
    import fcntl
    import os
    import threading

    import orbi.runner as runner

    # The fake `gh` on PATH answers the PR commands of the verify path
    # (the state file carries the default OPEN PR state, which is
    # MERGEABLE with the worktree's own HEAD — see the fake in this
    # module).
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    # `ai-pr-opened` makes the fake `gh pr list` report the PR (the fake
    # derives it from the label, exactly like the delivery path does).
    write_state(state, {"9": ["ai-pr-opened"]})
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    monkeypatch.setenv("ORBI_FAKE_GH_STATE", str(state))

    # A task worktree of the deployment checkout (shared refstore),
    # the same shape the runner creates for a delivery.
    worktree = clone / ".worktrees" / \
        "orbi-owner-repo-issue-9-01234567"
    git(clone, "worktree", "add", "-b",
        "orbi/owner-repo-issue-9", str(worktree), "HEAD")
    if base_sha is None:
        base_sha = git(clone, "rev-parse", "HEAD")

    # The advancer keeps pushing commits to the BARE origin while the
    # hammer runs.
    adv = tmp_path / "adv"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "origin.git"), str(adv)],
        capture_output=True, text=True, check=True,
    )
    git(adv, "config", "user.email", "pilot@test.local")
    git(adv, "config", "user.name", "Pilot")
    stop = threading.Event()

    def advance() -> None:
        i = 0
        while not stop.is_set():
            with open(adv / f"adv{i % 16}.txt", "a",
                      encoding="utf-8") as handle:
                handle.write(f"adv {i}\n")
            git(adv, "add", ".")
            git(adv, "commit", "-qm", f"adv {i}")
            git(adv, "push", "-q", "origin", "main")
            i += 1

    advancer = threading.Thread(target=advance, daemon=True)
    errors: list[str] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def hammer(worker: int) -> None:
        for round_no in range(rounds):
            # All threads enter the same round at the same time:
            # maximum overlap of the ref-writing git commands.
            barrier.wait(timeout=60)
            # A unique Issue number per (round, worker) — a unique
            # worktree path and branch, so `create_worktree` always
            # runs the real `git worktree add -b` (never the
            # path-exists short-circuit).
            number = 1000 + round_no * workers + worker
            try:
                runner.freeze_base(clone, "main")
                runner.create_worktree(
                    clone, "owner/repo", number, "01234567", base_sha,
                )
                runner.verify_pr(RunContext(run_id="01234567", issue=9, branch="orbi/owner-repo-issue-9", worktree=worktree, source_repo="owner/repo"), "main", repo_dir=clone, require_latest_base=False)
                runner.sync_base_checkout(clone, "main")
            except Exception as exc:
                with errors_lock:
                    errors.append(repr(exc))

    advancer.start()
    threads = [
        threading.Thread(target=hammer, args=(worker,))
        for worker in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    stop.set()
    advancer.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive(), "a hammer thread hung"
    # The lock is short-lived: after the hammer it is free again.
    lock_path = runner.base_sync_lock_path(clone)
    probe = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(probe, fcntl.LOCK_UN)
        os.close(probe)
    return errors


def test_concurrent_ref_writing_on_the_shared_checkout_never_fails(
    clone, tmp_path, monkeypatch,
):
    """Issue #105 regression: the task worktrees are git worktrees of the
    deployment checkout — they share its refstore and object database.
    With `max_concurrency = 2` two Runners issue ref-writing git commands
    (fetch, worktree add, merge) from the checkout AND from their
    worktrees at the same time; git's optimistic ref lock made the loser
    fail with `cannot lock ref 'refs/remotes/origin/main': is at X but
    expected Y` — in the #105 scene the PR was already MERGED, the
    confirm fetch raced, and the delivery was marked `ai-blocked`.

    The runner's ref-writing paths must serialize on the shared
    base-sync flock (the SAME lock the ExecStartPre preflight uses), so
    concurrent operations wait instead of racing: four threads hammer
    the runner's ref-writing paths exactly the way the Runners call
    them — `freeze_base` / `create_worktree` / `sync_base_checkout` on
    the checkout, `verify_pr` from a task worktree (the git cwd) with
    the checkout as `repo_dir` (the lock) — while an advancer thread
    keeps pushing commits to origin (the #105 scene: a delivery merge
    landing on origin/main while the other Runner is mid-fetch). Every
    operation must succeed: with the lock the fetches wait for each
    other instead of racing git's optimistic ref lock. Without the lock
    the same hammer fails with `cannot lock ref` (verified against the
    neutered lock).

    `merge_gate` and `confirm_merged` are not in the hammer: their base
    checks (the PR head must contain the latest remote base) cannot pass
    while the advancer advances origin, and their lock serialization is
    covered by the unit tests (test_merge_gate_serializes_on_the_shared
    _checkout_lock, test_confirm_merged_serializes_on_the_shared
    _checkout_lock) plus the real concurrent delivery in
    test_capacity_two_allows_two_runners_and_rejects_third."""
    errors = _run_ref_hammer(clone, tmp_path, monkeypatch)
    assert errors == [], (
        f"concurrent ref-writing on the shared checkout failed: {errors}"
    )


def test_ref_hammer_collects_a_failing_operation(clone, tmp_path, monkeypatch):
    """The hammer's error collection: a failing ref-writing operation
    (a bad worktree base SHA) is recorded, never swallowed — the
    fail-fast contract of the runner's git paths (AGENTS.md: log the
    command, return code, stdout and stderr, then raise; never swallow
    an error)."""
    errors = _run_ref_hammer(
        clone, tmp_path, monkeypatch, rounds=1, workers=2,
        base_sha="0" * 40,  # not a commit: `git worktree add` fails
    )
    assert errors, "the failing operation must be collected"
    assert any("worktree" in error for error in errors), errors


@_runner_e2e_linux_only
def test_killed_runner_slot_is_released_by_the_kernel(clone, tmp_path):
    """SIGKILL cannot run any cleanup; the kernel releases the flock
    lock, so the next runner takes the slot back — no permanent lock."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: len(pi_invocations(pi_log)) == 1,
        what="first runner to call Pi",
    )
    first.kill()  # SIGKILL: no cleanup can run
    first.wait(timeout=10)
    # The slot file survives the kill (it is the lock target) ...
    assert slot_files(clone), "killed process must leave its slot file"
    # ... but the lock is gone: the next runner takes the slot back
    # (not capacity_full). The dead run's failure path is simulated
    # (label cleanup: ai-in-progress removed, ai-blocked added), so no
    # scan — resumable, in-flight or ready — can pick the Issue up and
    # the runner exits cleanly via no_ready_issue. (The no-cleanup
    # scene, where the claim label is left behind, is the restart
    # resume covered by
    # test_killed_runner_is_resumed_by_the_next_claim_scan.)
    state_now = read_state(state)
    labels = state_now["issues"]["7"]["labels"]
    labels.remove("ai-in-progress")
    labels.append("ai-blocked")
    state_now["issues"]["7"]["labels"] = labels
    atomic_write_json(state, state_now)
    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "capacity_full" not in err
    assert "no_ready_issue" in err
    assert slots_held(clone) == [(1, None)], "slot must be released on exit"


@_runner_e2e_linux_only
def test_killed_runner_is_resumed_by_the_next_claim_scan(clone, tmp_path):
    """Issue #18 acceptance (review round 3, PR #42): a SIGKILLed runner
    leaves the task worktree AND the `ai-in-progress` claim label behind
    (the failure path never ran). The NEXT runner must recover the
    in-flight Issue through the real claim scan (a fresh main() tick —
    no direct process_issue call): it reuses the same run id, the same
    worktree and PATCHes the same progress comment (found by its hidden
    run marker) instead of creating a second run — never
    no_ready_issue."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: len(pi_invocations(pi_log)) == 1,
        what="first runner to call Pi",
    )
    first.kill()  # SIGKILL: no cleanup can run
    first.wait(timeout=10)
    # The dead runner's Pi child was orphaned by the SIGKILL; in the
    # systemd world the service cgroup takes it with the runner. Wait
    # for the orphan to finish (its commit, if any, lands BEFORE the
    # resumed run starts — never in the middle of it).
    orphan_pid = int(pi_invocations(pi_log)[-1].split()[1])
    wait_for_pid_exit(orphan_pid)

    # The kill left the claim label behind (the failure path never ran).
    snap = read_state(state)
    assert "ai-in-progress" in snap["issues"]["7"]["labels"]
    assert "ai-ready" in snap["issues"]["7"]["labels"]
    # The dead run's task worktree survives; its name carries the run id.
    worktrees = sorted(
        (clone / ".worktrees").glob("orbi-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    dead_worktree = worktrees[0]
    dead_run_id = dead_worktree.name.rsplit("-", 1)[-1]
    # The dead run's progress comment (hidden run marker) exists.
    progress_bodies = [
        c["body"] for c in snap["comments"]
        if "**Orbi progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1
    assert f"<!-- orbi:run={dead_run_id} -->" in progress_bodies[0]

    # The NEXT runner (a fresh main() tick) resumes the SAME run through
    # the claim scan and delivers: the PR opens ...
    second = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in
        read_state(state)["issues"]["7"]["labels"],
        timeout=120,
        what="second runner to resume the in-flight issue and open the PR",
    )
    snap = read_state(state)
    # ... on the SAME worktree (no second worktree for the Issue) ...
    worktrees = sorted(
        (clone / ".worktrees").glob("orbi-owner-repo-issue-7-*"),
    )
    assert [path.name for path in worktrees] == [dead_worktree.name]
    # ... re-running Pi in it (one more invocation, same run) ...
    assert len(pi_invocations(pi_log)) == 2
    # ... and keeping the SAME progress comment (one, never two) under
    # the original run marker.
    progress_bodies = [
        c["body"] for c in snap["comments"]
        if "**Orbi progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1, (
        f"restart must not create a second progress comment: "
        f"{snap['comments']}"
    )
    assert f"<!-- orbi:run={dead_run_id} -->" in progress_bodies[0]
    # The delivery finished: the in-progress label is gone.
    assert "ai-in-progress" not in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    # The resumed implement tick ends when the PR opens (Issue #788):
    # the labels are ai-pr-opened, the slot is released, and the review
    # is the NEXT tick.
    assert "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"]
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "no_ready_issue" not in err
    assert "capacity_full" not in err
    assert slots_held(clone) == [(1, None)], "slot must be released on exit"

    # The final tick: the resumable scan classifies issue 7
    # RESUME_REVIEW on the SAME run and delivers (review -> fix ->
    # auto-merge in the fake world).
    third = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=180,
        what="resumed run to review, fix and merge",
    )
    # The final delivery summary PATCHed the SAME progress comment (the
    # summary lands after the ai-pr-opened transition, so it is checked
    # here, once the delivery is terminal).
    snap = read_state(state)
    progress_bodies = [
        c["body"] for c in snap["comments"]
        if "**Orbi progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1
    assert f"<!-- orbi:run={dead_run_id} -->" in progress_bodies[0]
    assert "Orbi delivered" in progress_bodies[0]
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "no_ready_issue" not in err
    assert "capacity_full" not in err
    assert "delivery_auto_merged" in err
    assert slots_held(clone) == [(1, None)], "slot must be released on exit"


@_runner_e2e_linux_only
def test_stranded_pr_opened_delivery_is_resumed_to_review_and_merge(
    clone, tmp_path,
):
    """Issue #70 acceptance, now the NORMAL path (Issue #788): after the
    PR opens the implement tick ends — the opened-PR delivery sits
    UNOWNED (no live runner holds it) until the next tick's resumable
    scan finds it (`ai-pr-opened` is scanned), recovers the scene, runs
    the independent review on the SAME PR and auto-merges it — never
    re-claims the Issue, never starts a second run, never blocks it."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to open the PR",
    )
    # The trusted opened-PR scene comment is the recovery source of the
    # next tick.
    wait_for(
        lambda: any(
            "Orbi opened PR:" in c["body"]
            for c in read_state(state)["comments"]
        ),
        what="opened-PR scene comment to be posted",
    )
    # The implement tick ENDS right after the PR opens: the delivery is
    # stranded by design — `ai-pr-opened`, no owner, slot free.
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    assert "ai-fix-needed" not in snap["issues"]["7"]["labels"]
    assert slots_held(clone) == [(1, None)]
    worktrees = sorted(
        (clone / ".worktrees").glob("orbi-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    dead_run_id = worktrees[0].name.rsplit("-", 1)[-1]

    # The NEXT tick (a fresh main()): the resumable scan finds the
    # stranded `ai-pr-opened` delivery, recovers the scene, and runs the
    # independent review — no fixer for a clean PR, no fresh claim.
    second = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=180,
        what="second runner to resume the review and merge the same PR",
    )
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "capacity_full" not in err
    assert "no_ready_issue" not in err
    assert "delivery_auto_merged" in err

    snap = read_state(state)
    labels = snap["issues"]["7"]["labels"]
    # The stranded delivery ended in the terminal success state — it
    # was never blocked, never re-claimed.
    assert "ai-merged" in labels
    assert "ai-blocked" not in labels
    assert "ai-pr-opened" not in labels
    # Same run, same worktree: nothing was recreated.
    worktrees = sorted(
        (clone / ".worktrees").glob("orbi-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    assert worktrees[0].name.rsplit("-", 1)[-1] == dead_run_id
    # One Pi per phase of the SAME run: implement (first runner),
    # review (second runner — the review fixes in-session, Issue #82)
    # — never a second implement, never a cold-start fixer.
    assert len(pi_invocations(pi_log)) == 2
    started = [
        c for c in snap["comments"] if "Orbi started Pi:" in c["body"]
    ]
    assert [c["issue"] for c in started] == ["7"]
    # The review actually ran on the resumed delivery.
    bodies = [c["body"] for c in snap["comments"]]
    assert any("Orbi merged PR:" in b for b in bodies)
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the merge"
    )


@_runner_e2e_linux_only
def test_live_review_tick_is_not_resumed_by_second_runner(
    clone, tmp_path,
):
    """Issue #70 review round 1 (Major), on the new architecture
    (Issue #788): a slot held by another process proves a LIVE runner is
    processing the opened-PR delivery (Issue #39 slot semantics), so the
    resumable scan SKIPS it — a second runner never starts a second
    review Pi in the same worktree/branch/run. Between ticks the
    delivery is unowned (the scan resumes it); DURING the review tick it
    is live (the scan skips it and the second runner claims the next
    ready Issue instead).

    The test-only review gate holds issue 7's fake review until the
    assertions below are established."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 2)

    # Tick 1: implement issue 7 and open its PR; the tick ends and
    # releases the slot (the delivery is now unowned, by design).
    first = start_runner(config, bin_dir, state, pi_log)
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"]

    # Tick 2 (slot 1 free again): the resumable scan resumes issue 7.
    # The review gate holds the review Pi mid-run — the delivery is
    # LIVE: its runner holds slot 1.
    review_gate = tmp_path / "review-gate"
    second = start_runner(
        config, bin_dir, state, pi_log, review_gate=review_gate,
    )
    review_waiting = review_gate.with_suffix(".waiting")
    wait_for(
        review_waiting.exists, timeout=30,
        what="the resumed review of issue 7 to hold the slot mid-run",
    )
    held = slots_held(clone, 2)
    assert held[0][1] is not None and held[1][1] is None

    # Tick 3, concurrent with tick 2 on the FREE slot 2: the resumable
    # scan sees slot 1 held by a live runner and SKIPS issue 7; the
    # ready scan claims issue 8 instead and opens its PR.
    third = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        timeout=120,
        what="the third runner to claim issue 8 and open its PR",
    )
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "capacity_full" not in err
    snap = read_state(state)
    # Issue 7's live delivery is untouched: simply awaiting the review
    # verdict — never re-resumed, never blocked.
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-fix-needed" not in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # Issue 8 was independently claimed and opened by the third runner.
    assert "ai-pr-opened" in snap["issues"]["8"]["labels"]
    # Exactly one "started Pi" comment per Issue: issue 8's is the third
    # runner's implement — never a second review of issue 7.
    started = [
        c for c in snap["comments"] if "Orbi started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]

    # Release the review: the SAME holder fixes in-session and merges
    # (issue 8's PR was verified before the fake merge could advance
    # main — no freshness race masks the scan contract).
    review_gate.write_text("go", encoding="utf-8")
    # The resumed review is the process holding the gate.  Wait for that
    # process to finish rather than polling the label while it is still
    # racing through the post-review merge path; the process exit is the
    # deterministic completion signal for this fixture.
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "delivery_auto_merged" in err
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # Three Pi invocations total: implement + review (in-session fix) of
    # issue 7's run, implement of issue 8 — never a second review of
    # issue 7, never a duplicate claim.
    assert len(pi_invocations(pi_log)) == 3
    assert slots_held(clone, 2) == [(1, None), (2, None)]


@_runner_e2e_linux_only
def test_review_of_free_pr_is_not_starved_by_an_in_flight_delivery(
    clone, tmp_path,
):
    """Issue #809 acceptance (the reported starvation, end to end): a
    delivery in flight in another runner must not stop the review of a
    DIFFERENT opened-PR delivery. The pre-#809 guard made the resumable
    scan return None whenever ANY slot was held, so every concurrent
    tick fell through to fresh claims — PRs kept opening and none was
    ever reviewed (nine MERGEABLE PRs, the oldest 90 minutes).

    The real user path, with real processes and a real slot flock:

    1. two implement ticks open the PRs of issues 7 and 8 (gated, in
       order), then end and release the slots — both deliveries sit
       unowned in `ai-pr-opened`, awaiting their review tick;
    2. the first review tick resumes issue 7 and its review session is
       held mid-run by the test gate — the delivery is LIVE: the runner
       holds slot 1 and its slot file names `owner/repo#7`;
    3. a concurrent tick must review issue 8 NOW (the free delivery) —
       the held issue 7 is skipped, the scan does NOT fall through to a
       fresh claim (there is nothing left to claim) — and merge it
       while issue 7's review is still parked;
    4. releasing the gate lets the SAME holder finish issue 7's review
       (absorbing the base that issue 8's merge advanced in-session).

    The multi-repo shape of the real deployment (two source repos on
    one deploy_home, one slot dir) is the same skip at the identity
    level: the hold matches on (repo, issue), covered by the unit
    tests in test_resume_pr.py."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 2)

    # ---- Stage two opened-PR deliveries: both implement sessions are
    # gated so the ticks overlap, then released in order; each tick ends
    # when its PR opens and releases the slot (Issue #788).
    pi_gate_7 = tmp_path / "pi-gate-7"
    pi_gate_8 = tmp_path / "pi-gate-8"
    first = start_runner(
        config, bin_dir, state, pi_log, pi_gate=pi_gate_7, drain_stderr=True,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to claim issue 7",
    )
    second = start_runner(
        config, bin_dir, state, pi_log, pi_gate=pi_gate_8, drain_stderr=True,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["8"]["labels"],
        what="second runner to claim issue 8",
    )
    pi_gate_7.write_text("go", encoding="utf-8")
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="issue 7 PR to open",
    )
    pi_gate_8.write_text("go", encoding="utf-8")
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        what="issue 8 PR to open",
    )
    for process in (first, second):
        process.wait(timeout=120)
        assert process.returncode == 0, process.drained_stderr.getvalue()
    assert slots_held(clone, 2) == [(1, None), (2, None)], (
        "both implement ticks ended: both deliveries await review unowned"
    )

    # ---- The review of issue 7 runs and is held mid-run: its runner
    # holds slot 1 and the slot file names the delivery (Issue #809's
    # per-delivery hold marker).
    review_gate = tmp_path / "review-gate"
    reviewer_7 = start_runner(
        config, bin_dir, state, pi_log, review_gate=review_gate,
    )
    review_waiting = review_gate.with_suffix(".waiting")
    wait_for(
        review_waiting.exists, timeout=30,
        what="issue 7's review to hold the slot mid-run",
    )
    held = slots_held(clone, 2)
    assert held[0][1] is not None and held[1][1] is None
    slot_lines = (clone / ".orbi" / "slots" / "slot-1").read_text(
        encoding="utf-8",
    ).splitlines()
    assert slot_lines[1] == "owner/repo#7", (
        "the live holder's slot file names the in-flight delivery"
    )

    # ---- The concurrent tick reviews the FREE delivery (issue 8) and
    # merges it while issue 7's review is still parked. Under the
    # pre-#809 whole-slot-dir guard this tick ended in `no_ready_issue`
    # — the starvation this test pins.
    reviewer_8 = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["8"]["labels"],
        timeout=180,
        what="the concurrent tick to review and merge the FREE delivery",
    )
    out, err = reviewer_8.communicate(timeout=120)
    assert reviewer_8.returncode == 0, err
    assert "delivery_auto_merged" in err
    assert "no_ready_issue" not in err
    assert "capacity_full" not in err
    # Issue 7's live delivery was untouched: still awaiting its verdict,
    # never re-resumed, never blocked, never re-claimed.
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-merged" not in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # The held slot still names issue 7 while the parked review runs.
    assert (clone / ".orbi" / "slots" / "slot-1").read_text(
        encoding="utf-8",
    ).splitlines()[1] == "owner/repo#7"

    # ---- Release issue 7's review: the SAME holder absorbs the base
    # issue 8's merge advanced (in-session, Issue #82) and merges.
    review_gate.write_text("go", encoding="utf-8")
    # The review holder must absorb issue 8's merge before it can finish.
    # Under CI's coverage-instrumented, shared-host load this serialized
    # base-sync path can exceed the earlier three-minute test bound even
    # though the runner is making progress (the failure that prompted
    # Issue #980). Keep the bound finite, but allow the real user path
    # enough time to complete instead of reporting a false starvation.
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=300,
        what="issue 7's review to finish and merge after the gate",
    )
    out, err = reviewer_7.communicate(timeout=120)
    assert reviewer_7.returncode == 0, err
    assert "delivery_auto_merged" in err
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # Exactly one Pi per phase: implement + review of each delivery —
    # never a second review of the held delivery, never a fresh claim
    # displacing a review.
    assert len(pi_invocations(pi_log)) == 4
    started = [
        c for c in snap["comments"] if "Orbi started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]
    assert slots_held(clone, 2) == [(1, None), (2, None)], (
        "both slots released after both merges"
    )


@_runner_e2e_linux_only
def test_unit_drift_auto_syncs_and_claims_without_human_intervention(
    clone, tmp_path,
):
    """Issue #142: the normal scene — a template change merged to main
    (the installed units are still the old ones). The start self-heals
    with the SAME idempotent install (copy, daemon-reload, enable the
    timer — never start/stop/restart the service), the re-verify is
    clean, the structured `unit_drift auto_synced` line is logged and
    the tick claims normally — no per-tick drift loop until a human
    intervenes. A clean deployment still logs `unit_drift result=clean`."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    # A drifted deployment: the installed timer carries one extra line.
    unit_dir = tmp_path / "drifted-units"
    install_deployed_units(unit_dir, clone)
    with (unit_dir / "orbi@.timer").open(
        "a", encoding="utf-8",
    ) as handle:
        handle.write("# drift\n")

    # A no-op fake systemctl on PATH: the self-heal runs the SAME
    # idempotent install, whose external steps (daemon-reload, enable
    # the timer) must succeed — but the e2e world has no user systemd
    # bus (GitHub CI, Issue #56's clean environment), so the real
    # `systemctl --user` would fail and the test would depend on the
    # host machine. The file copy and the re-verify (what is under
    # test) stay real; the `auto_synced` line below only appears after
    # the install's systemctl steps succeeded.
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    runner_proc = start_runner(
        config, bin_dir, state, pi_log, unit_dir=unit_dir,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["7"]["labels"],
        what="runner to self-heal the drift and claim",
    )
    runner_proc.kill()
    out, err = runner_proc.communicate(timeout=30)
    # The self-heal is logged with the structured auto_synced line.
    assert "unit_drift result=auto_synced unit=orbi@.timer" in err
    assert "before_sha256=" in err
    assert "after_sha256=" in err
    assert "commit=" in err
    # The repo template won: the installed unit matches it again.
    assert (unit_dir / "orbi@.timer").read_bytes() == (
        clone / "systemd" / "orbi@.timer"
    ).read_bytes()
    assert "ai-in-progress" in read_state(state)["issues"]["7"]["labels"]

    # A clean deployment (the units now match the templates) passes
    # the preflight without any sync and claims normally.
    write_state(state, {"8": ["ai-ready"]})
    runner_proc = start_runner(
        config, bin_dir, state, pi_log, unit_dir=unit_dir,
    )
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["8"]["labels"],
        what="runner to claim on a clean deployment",
    )
    runner_proc.kill()
    out, err = runner_proc.communicate(timeout=30)
    assert "unit_drift result=clean" in err
    assert "unit_drift auto_synced" not in err
    assert "ai-in-progress" in read_state(state)["issues"]["8"]["labels"]


@_runner_e2e_linux_only
def test_unit_drift_unresolvable_blocks_the_start_without_claiming(
    clone, tmp_path,
):
    """Issue #142: a drift the self-heal CANNOT resolve (the installed
    unit is re-tampered right after the install's copy, so the
    re-verify still sees it) fails the start BEFORE any claim:
    non-zero exit, the structured `unit_drift` line in the log (repo
    path, installed path, hashes, fix command), no slot, no label
    change, no Pi."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    # A drifted deployment: the installed timer carries one extra line.
    unit_dir = tmp_path / "unresolvable-units"
    install_deployed_units(unit_dir, clone)
    with (unit_dir / "orbi@.timer").open(
        "a", encoding="utf-8",
    ) as handle:
        handle.write("# drift\n")

    # A fake systemctl on PATH: every daemon-reload re-tampers the
    # installed timer AFTER the install's copy, so the re-verify still
    # sees the drift (an unresolvable scene).
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = '--user' ] && [ \"$2\" = 'daemon-reload' ]; then\n"
        f"    printf '# drift\\n' >> {unit_dir / 'orbi@.timer'}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    runner_proc = start_runner(
        config, bin_dir, state, pi_log, unit_dir=unit_dir,
    )
    out, err = runner_proc.communicate(timeout=60)
    assert runner_proc.returncode != 0, err
    assert "unit_drift unit=orbi@.timer" in err
    assert f"repo={clone / 'systemd' / 'orbi@.timer'}" in err
    assert f"installed={unit_dir / 'orbi@.timer'}" in err
    assert "repo_sha256=" in err
    assert "installed_sha256=" in err
    assert 'fix="orbi install-units"' in err
    assert "unit_drift auto_synced" not in err
    # Nothing was claimed: no labels, no comments, no Pi, no slot.
    snap = read_state(state)
    assert snap["issues"]["7"]["labels"] == ["ai-ready"]
    assert snap["comments"] == []
    assert pi_invocations(pi_log) == []
    assert slot_files(clone) == []
