"""E2E concurrency tests (Issue #39).

Real runner processes (``bootstrap_runner.py``) run against a local bare
origin plus a stateful fake ``gh`` executable on PATH, while a fake ``pi``
executable records every invocation. These prove the acceptance criteria:

- with ``max_concurrency = 1`` a second concurrent runner logs
  ``capacity_full``, claims no Issue, changes no label and never calls Pi;
- after the first runner exits, the next runner takes the released slot and
  claims the NEXT ready Issue (never the same one twice);
- with ``max_concurrency = 2`` two runners hold two different slots and claim
  two different Issues while a third runner is rejected;
- a SIGKILLed runner leaves a stale slot that the next runner takes back,
  so an abnormal exit never deadlocks the machine.
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
# labels and the comments. It answers exactly the commands the runner runs.
FAKE_GH = """#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(os.environ["MUYAN_FAKE_GH_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

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
        {"issue": args[2], "body": args[args.index("--body") + 1]}
    )
    save()
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
        "headRefOid": head,
        "body": f"<!-- muyan-pilot:run={run_id} -->\\n\\nPlan for {branch}",
    }]))
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


def write_state(state_path: Path, issues: dict[str, list[str]]) -> None:
    state = {
        "issues": {
            str(number): {
                "title": f"issue {number}", "body": f"body {number}",
                "labels": list(labels),
            }
            for number, labels in issues.items()
        },
        "comments": [],
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")


def read_state(state_path: Path) -> dict:
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


def start_runner(
    config_path: Path, bin_dir: Path,
    state_path: Path, pi_log: Path,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["MUYAN_FAKE_GH_STATE"] = str(state_path)
    env["MUYAN_FAKE_PI_LOG"] = str(pi_log)
    return subprocess.Popen(
        ["/usr/bin/python3", str(RUNNER), "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        cwd=REPO_ROOT,
    )


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


def pi_invocations(pi_log: Path) -> list[str]:
    if not pi_log.exists():
        return []
    return pi_log.read_text(encoding="utf-8").splitlines()


def slot_files(clone: Path) -> list[Path]:
    slot_dir = clone / ".muyan-pilot" / "slots"
    return sorted(slot_dir.glob("slot-*")) if slot_dir.is_dir() else []


def test_capacity_one_rejects_second_concurrent_runner(clone, tmp_path):
    """First runner holds the slot; the second exits via capacity_full."""
    bin_dir = install_fakes(tmp_path)
    state = tmp_path / "gh-state.json"
    write_state(state, {"7": ["ai-ready"], "8": ["ai-ready"]})
    pi_log = tmp_path / "pi.log"
    config = write_config(clone, tmp_path, 1)

    first = start_runner(config, bin_dir, state, pi_log)
    wait_for(lambda: slot_files(clone), what="first runner to hold a slot")
    wait_for(
        lambda: len(pi_invocations(pi_log)) == 1,
        what="first runner to call Pi",
    )

    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    assert "capacity_full" in err
    assert "max_concurrency=1" in err
    # The second runner claimed nothing and never called Pi.
    snap = read_state(state)
    assert snap["issues"]["7"]["labels"] == ["ai-ready", "ai-in-progress"]
    assert snap["issues"]["8"]["labels"] == ["ai-ready"]
    assert len(pi_invocations(pi_log)) == 1

    # The first runner finishes and releases the slot on process exit.
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0, err
    assert not slot_files(clone), "slot must be released when the process exits"
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]

    # The next runner takes the released slot and claims the NEXT issue.
    third = start_runner(config, bin_dir, state, pi_log)
    out, err = third.communicate(timeout=120)
    assert third.returncode == 0, err
    assert "capacity_full" not in err
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["8"]["labels"]
    # Two different Issues were processed; none was claimed twice.
    assert len(pi_invocations(pi_log)) == 2
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]


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

    for runner in (first, second):
        out, err = runner.communicate(timeout=120)
        assert runner.returncode == 0, err
    assert not slot_files(clone)
    snap = read_state(state)
    assert "ai-pr-opened" in snap["issues"]["7"]["labels"]
    assert "ai-pr-opened" in snap["issues"]["8"]["labels"]
    # Exactly one Pi per Issue: two invocations, no duplicate claim.
    assert len(pi_invocations(pi_log)) == 2
    started = [
        c for c in snap["comments"] if "Muyan Pilot started Pi:" in c["body"]
    ]
    assert sorted(c["issue"] for c in started) == ["7", "8"]


def test_killed_runner_slot_is_reclaimed_by_next_runner(clone, tmp_path):
    """SIGKILL leaves a stale slot; the next runner takes it back."""
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
    first.kill()  # SIGKILL: no atexit, no signal handler
    first.wait(timeout=10)
    # The slot file survives the kill ...
    assert slot_files(clone), "killed process must leave its slot file"

    second = start_runner(config, bin_dir, state, pi_log)
    out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err
    # It re-took the stale slot (not capacity_full) ...
    assert "capacity_full" not in err
    # ... and, with no ready Issue left, exited cleanly via no_ready_issue.
    assert "no_ready_issue" in err
    assert not slot_files(clone), "stale slot must be released on exit"
