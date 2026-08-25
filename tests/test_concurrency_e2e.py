"""E2E concurrency tests (Issue #39).

Real runner processes (``bootstrap_runner.py``) run against a local bare
origin plus a stateful fake ``gh`` executable on PATH, while a fake ``pi``
executable records every invocation. These prove the acceptance criteria:

- with ``max_concurrency = 1`` a second concurrent runner logs
  ``capacity_full``, claims no Issue, changes no label and never calls Pi;
- the slot is held for the whole delivery lifecycle (implement → review →
  fix → merge): after the first runner opens the PR it KEEPS the slot
  (polling the PR state), so a second concurrent runner still sees
  ``capacity_full`` and no second Issue is claimed;
- when the Issue moves to ``ai-fix-needed`` the next runner resumes the
  SAME PR (same run id, same slot) instead of claiming a new Issue;
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

def match_issue(labels, search):
    required = [t[6:] for t in search.split() if t.startswith("label:")]
    excluded = [t[7:] for t in search.split() if t.startswith("-label:")]
    return all(r in labels for r in required) and not any(
        e in labels for e in excluded
    )

if args[:2] == ["issue", "list"]:
    search = args[args.index("--search") + 1]
    out = [
        {"number": int(num), "title": issue["title"],
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
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_id = branch.rsplit("-", 1)[1]
    print(json.dumps([{
        "url": "https://github.com/owner/repo/pull/99",
        "baseRefName": "main",
        "headRefName": branch,
        "headRefOid": head,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
        "body": f"<!-- muyan-pilot:run={run_id} -->\\n\\nPlan for {branch}",
    }]))
elif args[:2] == ["pr", "view"]:
    print(json.dumps({"state": state.get("pr_state", "OPEN")}))
else:
    raise SystemExit(f"unexpected gh command: {args}")
"""

# Fake ``pi``: records one line per invocation, then stays busy for a while
# so concurrent runners overlap while it runs.
FAKE_PI = """#!/usr/bin/env python3
import os, sys, time
log = os.environ.get("MUYAN_FAKE_PI_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"pi {os.getpid()}\\n")
time.sleep(1.0)
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
    """Local bare origin plus a clone with one commit on main."""
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


def start_runner(
    config_path: Path, bin_dir: Path,
    state_path: Path, pi_log: Path,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["MUYAN_FAKE_GH_STATE"] = str(state_path)
    env["MUYAN_FAKE_PI_LOG"] = str(pi_log)
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


def set_labels(state_path: Path, number: str, labels: list[str]) -> None:
    state = read_state(state_path)
    state["issues"][number]["labels"] = list(labels)
    atomic_write_json(state_path, state)


def set_pr_state(state_path: Path, pr_state: str) -> None:
    state = read_state(state_path)
    state["pr_state"] = pr_state
    atomic_write_json(state_path, state)


def test_capacity_one_slot_held_through_review_fix_merge(
    clone, tmp_path,
):
    """The slot is held for the whole delivery lifecycle (Issue #39):
    after the PR opens the first runner keeps it (polling the PR state),
    the fix-needed resume reuses the same PR and the same slot, and only
    the merge releases the slot for the next Issue."""
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
    wait_for(
        lambda: len(pi_invocations(pi_log)) == 1,
        what="first runner to call Pi",
    )

    # The PR is open but NOT merged: the slot is still held. A second
    # concurrent runner must be denied and must not claim Issue 8.
    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    assert "capacity_full" in err
    snap = read_state(state)
    # Issue 7 is awaiting review (ai-in-progress was consumed by the
    # PR-opened transition); Issue 8 is untouched.
    assert snap["issues"]["7"]["labels"] == ["ai-ready", "ai-pr-opened"]
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]
    assert len(pi_invocations(pi_log)) == 1

    # The review finds a problem: the Issue moves to ai-fix-needed. The
    # first runner is still holding the slot (the PR is still open), so
    # the fix is done by the SAME holder: it resumes the SAME PR (same
    # run id, same branch) instead of claiming a new Issue.
    set_labels(state, "7", ["ai-ready", "ai-fix-needed"])
    wait_for(
        lambda: (
            "ai-fix-needed" not in read_state(state)["issues"]["7"]["labels"]
            and "ai-pr-opened" in read_state(state)["issues"]["7"]["labels"]
            and any("Muyan Pilot fixed PR:" in c["body"]
                    for c in read_state(state)["comments"])
        ),
        timeout=120,
        what="first runner to resume and fix the same PR",
    )
    # The fix consumed ai-fix-needed and returned to awaiting review ...
    snap = read_state(state)
    assert "ai-fix-needed" not in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    # ... with exactly one Pi invocation per phase of the SAME run:
    # implement, then fix — never a new claim of another Issue.
    assert len(pi_invocations(pi_log)) == 2
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert [c["issue"] for c in started] == ["7"]

    # The human merges the PR: the first runner sees it, releases the
    # slot and exits.
    set_pr_state(state, "MERGED")
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert "delivery_merged" in err
    assert slots_held(clone) == [(1, None)], (
        "slot must be released after the merge"
    )

    # The next runner takes the released slot and claims the NEXT issue.
    # Issue 8's PR is a fresh delivery: back to OPEN.
    set_pr_state(state, "OPEN")
    third = start_runner(config, bin_dir, state, pi_log)
    wait_for(
        lambda: "ai-pr-opened" in read_state(state)["issues"]["8"]["labels"],
        what="third runner to open the PR for issue 8",
    )
    # Issue 8 is the only ready Issue left; the merged delivery (Issue 7)
    # is ai-pr-opened, so it is not re-claimed.
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["8"]["labels"]
    # Two different Issues were processed; none was claimed twice.
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]
    # Issue 8's delivery is now awaiting review and still holds the slot.
    assert slots_held(clone)[0][1] is not None, (
        "issue 8's delivery still holds the slot"
    )
    # Merge it: the third runner exits and releases the slot.
    set_pr_state(state, "MERGED")
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "capacity_full" not in err
    assert "delivery_merged" in err
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

    # Merge both PRs: both runners exit and release their slots.
    set_pr_state(state, "MERGED")
    for runner in (first, second):
        out, err = runner.communicate(timeout=120)
        assert runner.returncode == 0, err
    assert slots_held(clone, 2) == [(1, None), (2, None)]
    # Exactly one Pi per Issue: two invocations, no duplicate claim.
    assert len(pi_invocations(pi_log)) == 2
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
    # (not capacity_full) and, with no ready Issue left, exits cleanly
    # via no_ready_issue.
    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    assert "capacity_full" not in err
    assert "no_ready_issue" in err
    assert slots_held(clone) == [(1, None)], "slot must be released on exit"
