"""Shared scaffolding for the release domain suite (Issue #1645).

Moved verbatim out of `tests/test_bootstrap_runner.py` when the release
tests moved to `tests/test_bootstrap_release.py`: the GitHub fakes, the
real-git release fixtures and the release-declaration bodies those tests
share. Single owner: the suite imports them from here instead of keeping a
copy of each.
"""
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import orbi.release as release
import orbi.runner as runner
from orbi import gh_response
from seam import seam


RELEASE_DECLARATION_BODY = """Ship v0.3.0 to the remote.

## Release

- version: v0.3.0
- base_branch: main
- scope:
  - #123
  - #124

## Notes

- this text is outside the release section
"""


# Issue #805: the removed `test_command` field is no longer part of the
# declaration contract — a body that still declares it fails as an
# unknown field (every live ticket that carried it is long closed).
LEGACY_TEST_COMMAND_DECLARATION_BODY = RELEASE_DECLARATION_BODY.replace(
    "- base_branch: main\n",
    "- base_branch: main\n- test_command: scripts/test\n",
)


_SCOPE_ITEMS_DROPPED = [
    line for line in RELEASE_DECLARATION_BODY.splitlines()
    if not line.strip().startswith("- #")
]


_EMPTY_SCOPE_BODY = "\n".join(_SCOPE_ITEMS_DROPPED) + "\n"


_NEITHER_SCOPE_BODY = (
    "\n".join(
        line for line in _SCOPE_ITEMS_DROPPED if line.strip() != "- scope:"
    ) + "\n"
)


RELEASE_MILESTONE_DECLARATION_BODY = """Ship v0.3.0 to the remote.

## Release

- version: v0.3.0
- base_branch: main
- scope_from_milestone: v0.3.0

## Notes

- this text is outside the release section
"""


def make_scope_gh(monkeypatch, *, pr_state_map=None, issue_state_map=None,
                  issue_reason_map=None):
    """Answer `gh pr view` / `gh issue view` for scope verification.

    `pr_state_map`: number -> (state, merge_commit_oid) for PRs; a
    number absent from the map is NOT a PR (gh exits 1 with the real
    "Could not resolve to a PullRequest" error).
    `issue_state_map`: number -> state for Issues; a number absent from
    BOTH maps is neither.
    `issue_reason_map`: number -> stateReason for Issues (absent = the
    null/legacy closure reason).
    """
    pr_state_map = pr_state_map or {}
    issue_state_map = issue_state_map or {}
    issue_reason_map = issue_reason_map or {}
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            number = int(command[3])
            if number not in pr_state_map:
                raise subprocess.CalledProcessError(
                    1, command,
                    stderr=(
                        "GraphQL: Could not resolve to a PullRequest "
                        f"with the number of {number}. "
                        "(repository.pullRequest)"
                    ),
                )
            state, oid = pr_state_map[number]
            return json.dumps({
                "number": number, "state": state,
                "mergeCommit": {"oid": oid} if oid else None,
            })
        if command[:3] == ["gh", "issue", "view"]:
            number = int(command[3])
            if number not in issue_state_map:
                raise subprocess.CalledProcessError(
                    1, command,
                    stderr=(
                        "GraphQL: Could not resolve to an Issue with "
                        f"the number of {number}."
                    ),
                )
            return json.dumps({
                "number": number, "state": issue_state_map[number],
                "stateReason": issue_reason_map.get(number),
            })
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return calls


def make_milestone_gh(monkeypatch, *, milestones=None, items_by_milestone=None):
    """Answer the gh API calls used for milestone issue scope derivation.

    `milestones`: the `repos/o/r/milestones?state=all&per_page=100` payload.
    `items_by_milestone`: milestone number -> `issues_closed` /
    `issues_open` lists. Pull requests are intentionally not a milestone
    scope source: the REST `/pulls` list endpoint ignores `milestone`.
    """
    milestones = milestones or []
    items_by_milestone = items_by_milestone or {}
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        path = command[2] if len(command) > 2 else ""
        if path == "repos/o/r/milestones?state=all&per_page=100":
            return json.dumps([milestones])
        if "/pulls?" in path:
            raise AssertionError(
                "milestone scope must not query /pulls: milestone is ignored"
            )
        match = re.fullmatch(
            r"repos/o/r/issues\?state=(\w+)&milestone=(\d+)&per_page=100",
            path,
        )
        if match:
            state, number = match.groups()
            bucket = items_by_milestone.get(int(number), {})
            return json.dumps([bucket.get(f"issues_{state}", [])])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return calls


def make_gate_gh(monkeypatch, *, leftover_labels=None, check_runs=None,
                 leftover_milestones=None, check_runs_failure=None):
    """Answer the gh calls of `check_release_gates`.

    `leftover_labels`: label -> [issue numbers] still open with it.
    `check_runs`: list of (name, status, conclusion) for the release
    commit (None -> the call fails, which must propagate). No open-PR
    query is answered: since Issue #608 the gate never makes one.
    `check_runs_failure`: a `(status, ratelimit_remaining)` pair the
    check-runs read fails with, carrying the response gh reported — a
    rate-limited 403 is `(403, 0)` (Issue #1768).
    `leftover_milestones`: issue number -> Milestone title (or None).
    When a leftover scan carries the documented `--milestone <title>`
    flag (Issue #671) only the Issues with that Milestone are answered —
    the real filter is server-side.
    """
    leftover_labels = leftover_labels or {}
    leftover_milestones = leftover_milestones or {}
    calls = []

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "issue", "list"]:
            label = command[command.index("--label") + 1]
            numbers = leftover_labels.get(label, [])
            if "--milestone" in command:
                milestone = command[command.index("--milestone") + 1]
                numbers = [
                    n for n in numbers
                    if leftover_milestones.get(n) == milestone
                ]
            return json.dumps([{"number": n} for n in numbers])
        if command[:2] == ["gh", "api"]:
            if check_runs_failure is not None:
                status, remaining = check_runs_failure
                raise gh_response.GitHubRequestFailed(
                    1, command, status=status,
                    ratelimit_remaining=remaining,
                    stderr=f"gh: failed with HTTP {status}",
                )
            if check_runs is None:
                raise subprocess.CalledProcessError(
                    1, command, stderr="HTTP 500: server error",
                )
            return json.dumps([
                {"name": name, "status": status, "conclusion": conclusion}
                for name, status, conclusion in check_runs
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    # the staying helpers the moved gates call (`list_issues`) resolve
    # the primitive in the runner globals.
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return calls


def make_registration_lag_gh(monkeypatch, api_responses):
    """Answer the gh calls of `check_release_gates` for the #657 world.

    `api_responses`: popped once per `gh api` poll — each entry is the
    (name, status, conclusion) list for that poll (`[]` = no check run
    registered yet). The leftover-label scans always answer empty. The
    returned fake raises on any unexpected command (one shared guard
    line, driven by its own test below).
    """
    def fake_run_command(command, **kwargs):
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps([])
        if command[:2] == ["gh", "api"]:
            return json.dumps([
                {"name": name, "status": status, "conclusion": conclusion}
                for name, status, conclusion in api_responses.pop(0)
            ])
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return fake_run_command


def make_release_race_clones(tmp_path):
    """A bare origin plus two clones of the same v0.2.0 base (Issue #1289).

    Models two independent Runner instances releasing the same version from
    the same frozen base: each clone owns a real `origin` remote.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        check=True, capture_output=True,
    )
    for key, value in (("user.email", "t@t"), ("user.name", "t"),
                       ("commit.gpgsign", "false"),
                       ("tag.gpgsign", "false")):
        subprocess.run(
            ["git", "-C", str(seed), "config", key, value],
            check=True, capture_output=True,
        )
    (seed / "pyproject.toml").write_text(
        '[project]\nname = "orbi"\nversion = "0.2.0"\n', encoding="utf-8",
    )
    (seed / "package.json").write_text(
        '{\n  "name": "cloud",\n  "version": "0.2.0"\n}\n',
        encoding="utf-8",
    )
    package = seed / "src" / "orbi"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '__version__ = "0.2.0"\n', encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(seed), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "base"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True, capture_output=True,
    )
    winner = tmp_path / "winner"
    loser = tmp_path / "loser"
    for clone in (winner, loser):
        subprocess.run(
            ["git", "clone", str(remote), str(clone)],
            check=True, capture_output=True,
        )
        for key, value in (("user.email", "t@t"), ("user.name", "t"),
                           ("commit.gpgsign", "false"),
                           ("tag.gpgsign", "false")):
            subprocess.run(
                ["git", "-C", str(clone), "config", key, value],
                check=True, capture_output=True,
            )
    return winner, loser


def make_release_gh(monkeypatch, *, release_exists=False,
                    release_body="## Changelog"):
    """Answer `gh release view` / `gh release create` / `gh release edit`."""
    calls = []
    state = {"exists": release_exists, "body": release_body}

    def fake_run_command(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "release", "view"]:
            if not state["exists"]:
                raise subprocess.CalledProcessError(
                    1, command, stderr="release not found",
                )
            return json.dumps({
                "tagName": command[3],
                "url": "https://github.com/o/r/releases/tag/" + command[3],
                "body": state["body"],
            })
        if command[:3] == ["gh", "release", "create"]:
            state["exists"] = True
            return ""
        if command[:3] == ["gh", "release", "edit"]:
            state["body"] = command[command.index("--notes") + 1]
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    return calls


def make_release_range_repo(tmp_path):
    """A real `main` repository for the tagged-range tests (Issue #1492)."""
    work = tmp_path / "range"
    subprocess.run(["git", "init", "-b", "main", str(work)],
                   check=True, capture_output=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t"),
                       ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        subprocess.run(["git", "-C", str(work), "config", key, value],
                       check=True, capture_output=True)
    return work


def commit_file(work, name, message, content=None):
    (work / name).write_text(
        message if content is None else content, encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(work), "add", name],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", message],
                   check=True, capture_output=True)
    return git_out(work, "rev-parse", "HEAD")


def mock_release_range_gh(monkeypatch, by_commit, calls=None):
    """Answer `GET repos/o/r/commits/{sha}/pulls` from a sha -> list map;
    every other command (git included) runs for real."""
    real = runner.run_command

    def fake_run_command(command, **kwargs):
        if not (command[0] == "gh" and command[1] == "api" and
                command[2].startswith("repos/o/r/commits/") and \
                command[2].endswith("/pulls")):
            return real(command, **kwargs)
        sha = command[2].split("/")[-2]
        if calls is not None:
            calls.append(sha)
        return json.dumps(by_commit.get(sha, []))
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)


def make_release_process_env(monkeypatch, *, body=RELEASE_DECLARATION_BODY,
                             tag_commit=None, release_url="https://github.com/o/r/releases/tag/v0.3.0",
                             in_progress=False, existing_run_id=None,
                             check_run_pages=None, check_runs_status=None,
                             milestone_items=None,
                             leftover_labels=None, leftover_milestones=None,
                             release_tree_files=("pyproject.toml", "package.json"),
                             prepare_release_version_error=None,
                             range_prs=None, range_commit_sink=None):
    """Full fake environment for `process_release`.

    Returns a dict of captured state: edit_issue / comment_issue calls,
    run_command calls, and the monkeypatched pieces.
    `check_run_pages` (Issue #268) optionally sequences the check-runs
    endpoint: a list of [(name, status, conclusion)] pages answered in
    order, the last page repeating when exhausted.
    `check_runs_status` (Issue #1760) makes the check-runs endpoint fail
    with that HTTP status, the way `gh api --include` reports a refused
    read (the release gate's missing Checks:read permission).
    `milestone_items` (Issue #253) answers the milestone scope-derivation
    queries: milestone number -> dict with keys `issues_closed` /
    `issues_open` / `pulls_closed` / `pulls_open`.
    `leftover_milestones` (Issue #671) maps a leftover Issue number to its
    Milestone title (None = no Milestone) so the `gh issue list --milestone`
    filter is modelled server-side.
    """
    leftover_labels = leftover_labels or {}
    leftover_milestones = leftover_milestones or {}
    state = {
        "edits": [], "comments": [], "commands": [],
        "run_ids": [], "active_runs": [],
        "published": [],
    }

    def fake_run_command(command, **kwargs):
        state["commands"].append((command, kwargs))
        # Issue #1492: the release notes come from the tagged commit range,
        # computed by real git against a real checkout in production. This
        # fake env has no repository, so the range is modelled here from
        # `resolved_range_prs`; `range_commit_sink` records the release
        # commit the range was computed against.
        if command[0] == "git" and command[1] == "describe":
            raise subprocess.CalledProcessError(1, command)
        if command[0] == "git" and command[1] == "rev-list":
            if range_commit_sink is not None:
                range_commit_sink.append(command[-1])
            return "range1"
        if command[0] == "gh" and command[1] == "api" and \
                command[2].startswith("repos/o/r/commits/") and \
                command[2].endswith("/pulls"):
            return json.dumps([
                {"number": n, "merged_at": "2026-01-01T00:00:00Z",
                 "base": {"ref": "main"}}
                for n in sorted(resolved_range_prs)
            ])
        if command[:3] == ["gh", "pr", "view"]:
            number = int(command[3])
            if number == 123:
                return json.dumps({
                    "number": 123, "state": "MERGED",
                    "mergeCommit": {"oid": "aaa111"},
                })
            raise subprocess.CalledProcessError(
                1, command,
                stderr=(
                    "GraphQL: Could not resolve to a PullRequest with "
                    f"the number of {number}. (repository.pullRequest)"
                ),
            )
        if command[:3] == ["gh", "issue", "view"]:
            fields = command[command.index("--json") + 1]
            if fields == "comments":
                return json.dumps({"comments": []})
            number = int(command[3])
            if fields == "number,state,stateReason" and number == 124:
                return json.dumps({
                    "number": 124, "state": "CLOSED",
                    "stateReason": "COMPLETED",
                })
        if command[:3] == ["gh", "api", "graphql"]:
            number = int(next(
                a for a in command if a.startswith("number=")
            ).split("=", 1)[1])
            return json.dumps({"data": {"repository": {"issue": {
                "__typename": "Issue", "number": number,
                "title": f"Deliver release scope item {number}",
                "body": "Concrete release behavior.",
                "url": f"https://github.com/o/r/issues/{number}",
                "stateReason": "COMPLETED",
                "labels": {"nodes": []},
                "closedByPullRequestsReferences": {"nodes": []},
            }}}})
        if command[:3] == ["gh", "issue", "list"]:
            label = command[command.index("--label") + 1]
            numbers = leftover_labels.get(label, [])
            if "--milestone" in command:
                milestone = command[command.index("--milestone") + 1]
                numbers = [
                    n for n in numbers
                    if leftover_milestones.get(n) == milestone
                ]
            return json.dumps([{"number": n} for n in numbers])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones?state=all&per_page=100",
                       "--paginate", "--slurp"]:
            return json.dumps([[
                {"number": 1, "title": "v0.2.0", "state": "closed",
                 "open_issues": 0, "closed_issues": 28,
                 "url": "https://api.github.com/repos/o/r/milestones/1",
                 "html_url": "https://github.com/o/r/milestone/1"},
                {"number": 5, "title": "v0.3.0", "state": "open",
                 "open_issues": 0, "closed_issues": 2,
                 "url": "https://api.github.com/repos/o/r/milestones/5",
                 "html_url": "https://github.com/o/r/milestone/5"},
            ]])
        if without_include(command) == ["gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100",
                       "--paginate", "--slurp"]:
            return json.dumps([[]])
        if without_include(command) == ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]:
            return json.dumps({"number": 5, "title": "v0.3.0",
                               "state": "closed", "open_issues": 0})
        if command[:2] == ["gh", "api"]:
            if milestone_items is not None:
                match = re.fullmatch(
                    r"repos/o/r/(issues|pulls)\?state=(\w+)&milestone=(\d+)"
                    r"(?:&per_page=100)?",
                    command[2],
                )
                if match:
                    kind, item_state, number = match.groups()
                    bucket = milestone_items.get(int(number), {})
                    return json.dumps([bucket.get(f"{kind}_{item_state}", [])])
            if check_runs_status is not None and \
                    command[2].endswith("/check-runs"):
                raise gh_response.GitHubRequestFailed(
                    1, command, status=check_runs_status,
                    ratelimit_remaining=4980,
                    stderr=("gh: Resource not accessible by integration "
                            f"(HTTP {check_runs_status})"),
                )
            if check_run_pages:
                page = check_run_pages.pop(0)
                if not check_run_pages:
                    check_run_pages.append(page)
                return json.dumps([
                    {"name": n, "status": s, "conclusion": c}
                    for n, s, c in page
                ])
            return json.dumps([
                {"name": "tests", "status": "completed",
                 "conclusion": "success"},
            ])
        if command[:3] == ["git", "ls-tree", "--name-only"]:
            # Issue #740: the frozen release tree the version_file check
            # probes — root entries, one per line.
            return "".join(name + "\n" for name in release_tree_files)
        if command[:2] == ["git", "tag"]:
            return ""
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return ""
        if command[:2] == ["git", "push"]:
            return ""
        if command[:2] == ["git", "rev-parse"]:
            # ensure_release_tag_pushed 的本地残留探测：env 假环境没有
            # 本地 tag——按"不存在"失败（走创建分支）。
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["gh", "issue", "close"]:
            return ""
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "run_command", fake_run_command)
    monkeypatch.setattr(seam, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(seam, "set_run_id",
                        lambda rid: state["run_ids"].append(rid))
    monkeypatch.setattr(seam, "has_in_progress_label",
                        lambda n, r: in_progress)
    monkeypatch.setattr(seam, "latest_run_id",
                        lambda r, s, n: existing_run_id)
    monkeypatch.setattr(seam, "freeze_base", lambda r, b: "abc123")
    monkeypatch.setattr(seam, "edit_issue",
                        lambda n, **k: state["edits"].append((n, k)))
    monkeypatch.setattr(seam, "comment_issue",
                        lambda n, **k: state["comments"].append((n, k)))
    monkeypatch.setattr(seam, "create_worktree",
                        lambda *a: Path("/wt"))
    monkeypatch.setattr(seam, "create_release_worktree",
                        lambda *a: Path("/wt"))
    publisher_class = Mock()
    # Issue #811: the milestone-capturing tests read the publisher mock
    # (`ProgressPublisher.return_value`) through the returned state.
    state["publisher"] = publisher_class.return_value
    monkeypatch.setattr(release, "ProgressPublisher", publisher_class)
    monkeypatch.setattr(seam, "_safe_publish", lambda **k: None)
    monkeypatch.setattr(seam, "set_active_run",
                        lambda *a: state["active_runs"].append(a))
    monkeypatch.setattr(release, "LOGGER", Mock())
    monkeypatch.setattr(release, "release_tag_commit",
                        lambda r, t: tag_commit)
    resolved_range_prs = {123} if range_prs is None else set(range_prs)

    def fake_prepare_release_version(worktree, tag, base_branch, **kwargs):
        if prepare_release_version_error is not None:
            raise prepare_release_version_error
        return "abc123"

    monkeypatch.setattr(release, "prepare_release_version",
                        fake_prepare_release_version)
    monkeypatch.setattr(seam, "refresh_cli_install",
        lambda worktree, **kwargs: "installed",
    )
    def fake_publish_release(**kwargs):
        state["published"].append(kwargs)
        return release_url

    monkeypatch.setattr(release, "publish_release", fake_publish_release)
    return state


def without_include(command: list[str]) -> list[str]:
    """The argv minus the adapter's trailing `--include` (Issue #1760).

    The adapter appends `--include` to a `gh api` command to read the
    response status; a fake that answers the call does not care about the
    header request, so it compares the caller's own argv.
    """
    return command[:-1] if command[-1:] == ["--include"] else command


# The adapter's gh api reads carry `--include` so the response status is
# readable (Issue #1760); the fake answers the documented header shape.
MILESTONE_LIST_COMMAND = [
    "gh", "api", "repos/o/r/milestones?state=all&per_page=100",
    "--paginate", "--slurp", "--include",
]


MILESTONE_ISSUES_COMMAND = [
    "gh", "api", "repos/o/r/issues?milestone=5&state=open&per_page=100",
    "--paginate", "--slurp", "--include",
]


def _milestone(number, title, state, open_issues):
    return {
        "number": number, "title": title, "state": state,
        "open_issues": open_issues, "closed_issues": 0,
        "url": f"https://api.github.com/repos/o/r/milestones/{number}",
        "html_url": f"https://github.com/o/r/milestone/{number}",
    }


def git_out(work: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(work), *args],
                           capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"git {args} failed rc={result.returncode} "
            f"stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()
