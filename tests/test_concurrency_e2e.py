"""E2E concurrency tests (Issue #39).

Real runner processes (``bootstrap_runner.py``) run against a local bare
origin plus a stateful fake ``gh`` executable on PATH, while a fake ``pi``
executable records every invocation. These prove the acceptance criteria:

- with ``max_concurrency = 1`` a second concurrent runner logs
  ``capacity_full``, claims no Issue, changes no label and never calls Pi;
- the slot is held for the whole delivery lifecycle (implement → review
  → merge): after the first runner opens the PR it KEEPS the slot
  (polling the PR state), so a second concurrent runner still sees
  ``capacity_full`` and no second Issue is claimed;
- the review session fixes findings IN THE SAME SESSION (Issue #82):
  one review Pi per delivery (no cold-start fixer, no third review),
  and the runner re-freezes the fixed head before the merge gate;
- after the PR is merged the first runner exits and releases the slot,
  and the next runner claims the NEXT ready Issue (never the same one
  twice);
- a PR closed without a merge is a terminal failure: the Issue is marked
  ``ai-blocked`` and the slot is released;
- with ``max_concurrency = 2`` two runners hold two different slots and
  claim two different Issues while a third runner is rejected;
- a SIGKILLed runner releases its slot automatically (the kernel owns
  the flock lock), so an abnormal exit never deadlocks the machine.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = "owner/repo"
RUNNER = REPO_ROOT / "bootstrap_runner.py"

# Stateful fake ``gh``: one JSON file (MUYAN_FAKE_GH_STATE) holds the Issue
# labels, the comments (with author association) and the PR state. It
# answers exactly the commands the runner runs.
FAKE_GH = """#!/usr/bin/env python3
import fcntl, json, os, subprocess, sys, tempfile
from pathlib import Path

state_path = Path(os.environ["MUYAN_FAKE_GH_STATE"])
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
    print(json.dumps(out[:1]))
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
    if args[-1] == "body":
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
    # Branch shape: muyan-pilot-owner-repo-issue-<n>-<run_id>.
    issue_num = branch.rsplit("-", 2)[1]
    run_id = branch.rsplit("-", 1)[1]
    print(json.dumps([{
        "number": 99,
        "url": "https://github.com/owner/repo/pull/99",
        "baseRefName": "main",
        "baseRefOid": git("rev-parse", "origin/main"),
        "headRefName": branch,
        "headRefOid": head,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
        "body": f"<!-- muyan-pilot:run={run_id} -->\\n\\nFixes #{issue_num}\\n\\nPlan for {branch}",
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
log = os.environ.get("MUYAN_FAKE_PI_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"pi {os.getpid()}\\n")
time.sleep(1.0)
system_prompt = sys.argv[sys.argv.index("--system-prompt") + 1]
if "INDEPENDENT REVIEW" in system_prompt:
    run_id = system_prompt.split("run_id=")[1].split()[0]
    marker = os.path.join(os.getcwd(), f".muyan-pilot-review-{run_id}")
    first_review = not os.path.exists(marker)
    if first_review:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("reviewed")
        # Initial review: one major finding...
        print('REVIEW_VERDICT ' + json.dumps({
            "verdict": "findings", "blockers": 0, "majors": 1,
            "minors": 0,
            "findings": [
                {"level": "Major", "location": "e2e",
                 "note": "first review finding"}
            ],
        }))
        # ...fixed in this same session: commit the fix (the PR head
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
    # A head behind the latest base is fixed in-session too (Issue
    # #82), on EVERY review round: the base may advance between rounds.
    # Absorb origin/main, resolve, retest, push.
    subprocess.run(["git", "fetch", "origin", "main"], check=True)
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
    # Final verdict after the in-session fix: clean.
    print('REVIEW_VERDICT ' + json.dumps({
        "verdict": "pass", "blockers": 0, "majors": 0,
        "minors": 0, "findings": [],
    }))
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
    (clone / "a.txt").write_text("a", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "first")
    git(clone, "push", "origin", "main")
    return clone


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


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
        "- Run id: `{{RUN_ID}}`\n",
        encoding="utf-8",
    )
    # validate_config requires the review prompt to exist as well (the
    # Runner runs the independent review itself). The review prompt
    # carries the run_id so the fake pi can key its
    # first-review-findings state per run.
    (tmp_path / "prompt_review.md").write_text(
        "INDEPENDENT REVIEW\nrun_id={{RUN_ID}}\n", encoding="utf-8",
    )
    config = tmp_path / f"muyan-pilot-{max_concurrency}.toml"
    config.write_text(
        f'source_repos = ["{REPO}"]\n'
        f'repo_dir = "{clone}"\n'
        f'workspace_root = "{tmp_path}"\n'
        f'prompt = "{prompt}"\n'
        f'max_concurrency = {max_concurrency}\n',
        encoding="utf-8",
    )
    return config


# Every started runner is tracked so a failing test never leaves a live
# runner (and its slot lock) behind.
_RUNNING: list[subprocess.Popen] = []


def install_deployed_units(unit_dir: Path) -> None:
    """Simulate the deployed machine: the repo templates installed as
    the user units (the idempotent install the README documents)."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in ("muyan-pilot.service", "muyan-pilot.timer"):
        shutil.copyfile(REPO_ROOT / "systemd" / name, unit_dir / name)


def start_runner(
    config_path: Path, bin_dir: Path,
    state_path: Path, pi_log: Path,
    unit_dir: Path | None = None,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["MUYAN_FAKE_GH_STATE"] = str(state_path)
    env["MUYAN_FAKE_PI_LOG"] = str(pi_log)
    # The pre-start drift check (Issue #103) reads the installed units
    # from here: a clean deployment by default (the templates as
    # installed), or an explicit dir for the drift scenarios.
    if unit_dir is None:
        unit_dir = config_path.parent / "unit-dir"
        install_deployed_units(unit_dir)
    env["MUYAN_PILOT_UNIT_DIR"] = str(unit_dir)
    process = subprocess.Popen(
        ["/usr/bin/python3", str(RUNNER), "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        cwd=REPO_ROOT,
    )
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


def test_wait_for_raises_when_condition_never_holds():
    with pytest.raises(AssertionError, match="timed out waiting for the moon"):
        wait_for(lambda: False, timeout=0.1, what="the moon")


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
    slot_dir = clone / ".muyan-pilot" / "slots"
    return sorted(slot_dir.glob("slot-*")) if slot_dir.is_dir() else []


def slots_held(clone: Path, capacity: int = 1) -> list:
    """Return the lock-state occupancy: the lock, not the file, is held."""
    import pilot_slots

    return pilot_slots.slot_occupancy(
        clone / ".muyan-pilot" / "slots", capacity,
    )


def set_pr_state(state_path: Path, pr_state: str) -> None:
    state = read_state(state_path)
    state["pr_state"] = pr_state
    atomic_write_json(state_path, state)


def test_capacity_one_slot_held_through_review_merge(
    clone, tmp_path,
):
    """The slot is held for the whole delivery lifecycle (Issue #39):
    after the PR opens the first runner keeps it, runs the independent
    review — which fixes the finding IN THE SAME SESSION (Issue #82:
    no cold-start fixer, no third review) — re-freezes the fixed head
    and auto-merges the PR itself (Issue #34), and only then releases
    the slot for the next Issue."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to open the PR",
    )

    # The PR is open but NOT merged: the slot is still held. A second
    # concurrent runner must be denied and must not claim Issue 8.
    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    assert "capacity_full" in err
    snap = read_state(state)
    # Issue 7 is in the review/fix state (ai-in-progress was consumed by
    # the PR-opened transition); Issue 8 is untouched.
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-in-progress" not in snap["issues"]["7"]["labels"]
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]

    # The first independent review finds a problem and fixes it IN THE
    # SAME SESSION (Issue #82): the fixed head is pushed, re-frozen and
    # merged by the SAME holder (still holding the slot) instead of
    # claiming a new Issue.
    wait_for(
        lambda: (
            "ai-merged" in read_state(state)["issues"]["7"]["labels"]
            and any("Muyan Pilot merged PR:" in c["body"]
                    for c in read_state(state)["comments"])
        ),
        timeout=180,
        what="first runner to review, fix in-session and merge",
    )
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" not in snap["issues"]["7"]["labels"]
    # One Pi invocation per phase of the SAME run: implement, review
    # (the review fixes in-session) — never a new claim of another
    # Issue, never a cold-start fixer.
    assert len(pi_invocations(pi_log)) == 2
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert [c["issue"] for c in started] == ["7"]
    bodies = [c["body"] for c in snap["comments"]]
    assert any("Muyan Pilot merged PR:" in b for b in bodies)
    # The in-session fix landed on the delivery branch: origin/main
    # carries the review fix file, so the runner merged the RE-FROZEN
    # head (the fixed one), not the frozen head (Issue #82).
    assert "review-fix.txt" in git(
        clone, "ls-tree", "--name-only", "origin/main",
    )

    # The auto-merge released the slot; the runner exited cleanly.
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "delivery_auto_merged" in err
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the merge"
    )

    # The next runner takes the released slot and claims the NEXT issue;
    # issue 8's PR is a fresh delivery (back to OPEN), and the full loop
    # runs again for it.
    set_pr_state(state, "OPEN")
    third = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["8"]["labels"],
        timeout=180,
        what="third runner to deliver issue 8",
    )
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["8"]["labels"]
    # Two different Issues were processed; none was claimed twice.
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "capacity_full" not in err
    assert "delivery_auto_merged" in err
    assert slots_held(clone) == [(1, None)]


def test_capacity_one_closed_unmerged_pr_releases_slot_and_blocks_issue(
    clone, tmp_path,
):
    """A PR closed without a merge is a terminal failure: the Issue is
    marked ai-blocked and the slot is released (no permanent hold)."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to open the PR",
    )
    # The PR is closed WITHOUT a merge.
    set_pr_state(state, "CLOSED")
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "delivery_closed_unmerged" in err
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the failure"
    )
    snap = read_state(state)
    assert "ai-blocked" in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" not in snap["issues"]["7"]["labels"]
    failure = [
        c for c in snap["comments"] if "Muyan Pilot failed:" in c["body"]
    ]
    assert len(failure) == 1
    assert "closed without a merge" in failure[0]["body"]

    # The next runner can proceed and claims the next Issue.
    # Issue 8's PR is a fresh delivery: back to OPEN.
    set_pr_state(state, "OPEN")
    second = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        what="second runner to open the PR for issue 8",
    )
    # Merge it: the second runner exits and releases the slot.
    set_pr_state(state, "MERGED")
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "capacity_full" not in err
    assert "delivery_merged" in err
    assert slots_held(clone) == [(1, None)]


def test_capacity_two_allows_two_runners_and_rejects_third(clone, tmp_path):
    """Two slots: two different Issues in parallel, third runner rejected."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 2)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-in-progress" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to claim issue 7",
    )

    second = start_runner(config, bin_dir, state, pi_log)
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

    # Both deliveries open their PRs and keep holding their slots.
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="issue 7 PR to open",
    )
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        what="issue 8 PR to open",
    )
    # While both PRs are open (unmerged), both slots are still held: a
    # third runner is still denied.
    third = start_runner(config, bin_dir, state, pi_log)
    out, err = third.communicate(timeout=60)
    assert third.returncode == 0, err
    assert "capacity_full" in err

    # Both runners auto-merge their own PRs (the review fixes in-session
    # and the runner merges the re-frozen head, Issues #34/#82) and
    # release their slots.
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"]
        and "ai-merged" in read_state(state)["issues"]["8"]["labels"],
        timeout=180,
        what="both runners to auto-merge their PRs",
    )
    for runner in (first, second):
        out, err = runner.communicate(timeout=120)
        assert runner.returncode == 0, err
        assert "delivery_auto_merged" in err
    assert slots_held(clone, 2) == [(1, None), (2, None)]
    # One Pi per phase of each run (implement, review — the review
    # fixes in-session, Issue #82): two invocations per Issue at a
    # minimum. When the first merge advances origin/main while the
    # second delivery's head is still behind, the second review round
    # absorbs the base in-session (one extra invocation per affected
    # delivery) — never a cold-start fixer, never a duplicate claim.
    assert 4 <= len(pi_invocations(pi_log)) <= 6
    started = [
        c for c in read_state(state)["comments"]
        if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]


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

    # The kill left the claim label behind (the failure path never ran).
    snap = read_state(state)
    assert "ai-in-progress" in snap["issues"]["7"]["labels"]
    assert "ai-ready" in snap["issues"]["7"]["labels"]
    # The dead run's task worktree survives; its name carries the run id.
    worktrees = sorted(
        (clone / ".worktrees").glob("muyan-pilot-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    dead_worktree = worktrees[0]
    dead_run_id = dead_worktree.name.rsplit("-", 1)[-1]
    # The dead run's progress comment (hidden run marker) exists.
    progress_bodies = [
        c["body"] for c in snap["comments"]
        if "**Muyan Pilot progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1
    assert f"<!-- muyan-pilot:run={dead_run_id} -->" in progress_bodies[0]

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
        (clone / ".worktrees").glob("muyan-pilot-owner-repo-issue-7-*"),
    )
    assert [path.name for path in worktrees] == [dead_worktree.name]
    # ... re-running Pi in it (one more invocation, same run) ...
    assert len(pi_invocations(pi_log)) == 2
    # ... and keeping the SAME progress comment (one, never two) under
    # the original run marker.
    progress_bodies = [
        c["body"] for c in snap["comments"]
        if "**Muyan Pilot progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1, (
        f"restart must not create a second progress comment: "
        f"{snap['comments']}"
    )
    assert f"<!-- muyan-pilot:run={dead_run_id} -->" in progress_bodies[0]
    # The delivery finished: the in-progress label is gone.
    assert "ai-in-progress" not in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    # The resumed run completes the full lifecycle (review -> fix ->
    # re-review -> auto-merge in the fake world) and exits cleanly.
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
        if "**Muyan Pilot progress**" in c["body"]
    ]
    assert len(progress_bodies) == 1
    assert f"<!-- muyan-pilot:run={dead_run_id} -->" in progress_bodies[0]
    assert "Muyan Pilot delivered" in progress_bodies[0]
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "no_ready_issue" not in err
    assert "capacity_full" not in err
    assert "delivery_auto_merged" in err
    assert slots_held(clone) == [(1, None)], "slot must be released on exit"


def test_stranded_pr_opened_delivery_is_resumed_to_review_and_merge(
    clone, tmp_path,
):
    """Issue #70 acceptance: the runner that opened a PR can die before
    the review starts (the progress 404 used to label the Issue
    ai-blocked and skip the review; a killed runner leaves no owner at
    all). The NEXT tick must resume the SAME delivery through the
    resumable scan (`ai-pr-opened` is scanned now), run the independent
    review on the SAME PR and auto-merge it — never re-claim the Issue,
    never start a second run, never block it."""
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
    # Wait until the trusted opened-PR scene comment exists (it is the
    # recovery source of the next tick), then kill the runner inside the
    # delivery wait: the PR stays open and unreviewed, the Issue stays
    # `ai-pr-opened` — a stranded delivery with no owner.
    wait_for(
        lambda: any(
            "Muyan Pilot opened PR:" in c["body"]
            for c in read_state(state)["comments"]
        ),
        what="opened-PR scene comment to be posted",
    )
    first.kill()  # SIGKILL: the delivery wait dies with the process
    first.wait(timeout=10)
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    assert "ai-fix-needed" not in snap["issues"]["7"]["labels"]
    worktrees = sorted(
        (clone / ".worktrees").glob("muyan-pilot-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    dead_run_id = worktrees[0].name.rsplit("-", 1)[-1]

    # The NEXT tick (a fresh main()): the resumable scan now finds the
    # stranded `ai-pr-opened` delivery, recovers the scene, and the
    # dispatch sends it straight to the delivery wait (independent
    # review) — no fixer for a clean PR, no fresh claim.
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
        (clone / ".worktrees").glob("muyan-pilot-owner-repo-issue-7-*"),
    )
    assert len(worktrees) == 1
    assert worktrees[0].name.rsplit("-", 1)[-1] == dead_run_id
    # One Pi per phase of the SAME run: implement (first runner),
    # review (second runner — the review fixes in-session, Issue #82)
    # — never a second implement, never a cold-start fixer.
    assert len(pi_invocations(pi_log)) == 2
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert [c["issue"] for c in started] == ["7"]
    # The review actually ran on the resumed delivery.
    bodies = [c["body"] for c in snap["comments"]]
    assert any("Muyan Pilot merged PR:" in b for b in bodies)
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the merge"
    )


def test_live_pr_opened_delivery_is_not_resumed_by_second_runner(
    clone, tmp_path,
):
    """Issue #70 review round 1 (Major): with `max_concurrency = 2` a
    second runner must NOT enter the delivery wait of a LIVE
    `ai-pr-opened` delivery — a slot held by another process proves a
    live runner is actively processing it (Issue #39 slot semantics).
    Resuming it would start a second review Pi in the same
    worktree/branch/run, and the second `gh pr merge
    --match-head-commit` on the already-merged PR would fail and mark
    the merged Issue `ai-blocked` (wrong terminal state). Instead the
    second runner skips the resumable scan and claims the NEXT ready
    Issue.

    The fake world carries one global PR state, so the second
    runner's own delivery (issue 8) is only asserted up to the claim:
    the guard under test is the resumable scan, not the second
    delivery's merge (covered by
    test_capacity_two_allows_two_runners_and_rejects_third)."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 2)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"],
        what="first runner to open the PR for issue 7",
    )
    # The first runner is LIVE in the delivery wait (holding slot 1,
    # PR open and unmerged).
    wait_for(
        lambda: any(
            "Muyan Pilot opened PR:" in c["body"]
            for c in read_state(state)["comments"]
        ),
        what="opened-PR scene comment to be posted",
    )

    # A second runner takes the free slot 2 while the first is live.
    # It must skip the resumable scan (another slot is held by a live
    # runner) and claim issue 8 instead of resuming issue 7's live
    # delivery.
    second = start_runner(config, bin_dir, state, pi_log)
    # The second runner's implement comment proves it claimed issue 8
    # (a fresh run) and not a resume of issue 7's live delivery (whose
    # run already has its started comment).
    wait_for(
        lambda: any(
            "Muyan Pilot started Pi:" in c["body"] and c["issue"] == "8"
            for c in read_state(state)["comments"]
        ),
        what="second runner to claim the next ready issue",
    )
    snap = read_state(state)
    # Issue 7's live delivery is untouched: still simply awaiting
    # review — never re-resumed (no ai-fix-needed from a second
    # review), never blocked.
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-fix-needed" not in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # Issue 8 is claimed by the second runner.
    assert "ai-in-progress" in snap["issues"]["8"]["labels"]
    # Exactly one "started Pi" comment per Issue: issue 8's is the
    # second runner's implement — never a second review of issue 7.
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]
    # The second runner never entered the delivery wait of issue 7's
    # live PR (the finding's repro: it logged `issue=7
    # delivery_awaiting` for the live runner's PR).
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "issue=7 delivery_awaiting" not in err
    # The first runner still owns issue 7's delivery and auto-merges
    # it itself — no second merge attempt from the second runner.
    wait_for(
        lambda: "ai-merged" in read_state(state)["issues"]["7"]["labels"],
        timeout=180,
        what="first runner to review and merge issue 7",
    )
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "delivery_auto_merged" in err
    snap = read_state(state)
    assert "ai-merged" in snap["issues"]["7"]["labels"]
    assert "ai-blocked" not in snap["issues"]["7"]["labels"]
    # The second runner's Pi work is bounded: its implement plus at
    # most its own review of issue 8 (the fake world's single global
    # PR state ends its wait once the first runner merges). Crucially,
    # none of it is a second review of issue 7's live delivery: the
    # first run always uses exactly two invocations (implement,
    # review — the review fixes in-session, Issue #82).
    assert 3 <= len(pi_invocations(pi_log)) <= 4
    assert slots_held(clone, 2) == [(1, None), (2, None)]


def test_unit_drift_auto_syncs_and_claims_without_human_intervention(
    clone, tmp_path,
):
    """Issue #142: the normal scene — a template change merged to main
    (the installed units are still the old ones). The start self-heals
    with the SAME idempotent install (copy, daemon-reload, enable the
    timer — never start/stop/restart the service), the re-verify is
    clean, the structured `unit_drift auto_synced` line is logged and
    the tick claims normally — no per-tick drift loop until a human
    intervenes. A clean deployment still logs `unit_drift clean`."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    # A drifted deployment: the installed timer carries one extra line.
    unit_dir = tmp_path / "drifted-units"
    install_deployed_units(unit_dir)
    with (unit_dir / "muyan-pilot.timer").open(
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
    assert "unit_drift auto_synced unit=muyan-pilot.timer" in err
    assert "before_sha256=" in err
    assert "after_sha256=" in err
    assert "commit=" in err
    # The repo template won: the installed unit matches it again.
    assert (unit_dir / "muyan-pilot.timer").read_bytes() == (
        clone / "systemd" / "muyan-pilot.timer"
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
    assert "unit_drift clean" in err
    assert "unit_drift auto_synced" not in err
    assert "ai-in-progress" in read_state(state)["issues"]["8"]["labels"]


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
    install_deployed_units(unit_dir)
    with (unit_dir / "muyan-pilot.timer").open(
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
        f"    printf '# drift\\n' >> {unit_dir / 'muyan-pilot.timer'}\n"
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
    assert "unit_drift unit=muyan-pilot.timer" in err
    assert f"repo={clone / 'systemd' / 'muyan-pilot.timer'}" in err
    assert f"installed={unit_dir / 'muyan-pilot.timer'}" in err
    assert "repo_sha256=" in err
    assert "installed_sha256=" in err
    assert "fix=muyan-pilot install-units" in err
    assert "unit_drift auto_synced" not in err
    # Nothing was claimed: no labels, no comments, no Pi, no slot.
    snap = read_state(state)
    assert snap["issues"]["7"]["labels"] == ["ai-ready"]
    assert snap["comments"] == []
    assert pi_invocations(pi_log) == []
    assert slot_files(clone) == []
