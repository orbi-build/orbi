"""Issue #2442: a red PR whose base is red pauses as base_ci_red first.

base_ci_red (Issue #1825) already owned the pause — one fix Issue per base
branch, the orbi:base-ci-red:v1 record, the automatic recovery — but only the
merge gate reached it, and a review cannot pass on red checks. With a red base
the reviewer found the base's own failure, may not fix unrelated code, and the
delivery ended human_decision_required: no record, no fix Issue, and every
paused PR needed a person to relabel it.

The review tick now answers the question BEFORE its session, with the SAME
detection the merge gate uses, on the base commit the head actually contains:
a shared red check pauses the delivery as base_ci_red (fix Issue opened or
reused, record in state waiting, automatic resume), a base whose matching
check is still running defers to the next tick, a base that is green on every
failed check reviews as today, and a failed base-check read is reported as the
read failure it is.

These tests drive the real review tick (_run_review_round ->
review_and_merge_if_clean) on a REAL local repository with a fake gh, a fake
pi on PATH and no model session: the tick's own git reads, the base absorb,
the label writes, the comments and the fix Issue are genuine.
"""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import git
from orbi import config as config_domain
from orbi import base_ci_fix, base_ci_red, run_state, scene as scene_mod
import orbi.review_base_ci_red as review_base_ci_red
import orbi.review_merge as review_merge
from seam import seam
from tests.fakes.pi import install_fake_pi

REPO = "owner/repo"
BRANCH = "orbi/owner-repo-issue-4"
PR_URL = f"https://github.com/{REPO}/pull/4"
RUN_ID = "a1b2c3d4"
ISSUE = 4
BASE_SHA_FROZEN = "abc123def456"
CHECK = "status-page-speed"
OTHER_CHECK = "public-api-contract"


def check_run(name=CHECK, *, status="completed", conclusion="failure",
              run_id=91):
    """One check run as GitHub's check-runs API returns it."""
    return {
        "id": run_id, "name": name, "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "output": {"text": f"{name} failed"},
    }


def rollup(name=CHECK, *, conclusion="FAILURE"):
    """The PR's statusCheckRollup entry for one check."""
    return [{"name": name, "status": "COMPLETED", "conclusion": conclusion}]


REVIEW_PASS = """#!/usr/bin/env python3
import os, subprocess
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                      capture_output=True, text=True).stdout.strip()
open("__MARKER__", "w", encoding="utf-8").write(head)
print('REVIEW_VERDICT ' + '{"verdict": "pass", "head": "' + head + '", '
      '"blockers": 0, "majors": 0, "minors": 0, "findings": []}')
"""

REVIEW_BLOCKED = """#!/usr/bin/env python3
import os, subprocess
cwd = os.getcwd()
os.makedirs(os.path.join(cwd, ".pi-session"), exist_ok=True)
head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                      capture_output=True, text=True).stdout.strip()
open("__MARKER__", "w", encoding="utf-8").write(head)
print('REVIEW_VERDICT ' + '{"verdict": "blocked_on_human_decision", '
      '"head": "' + head + '", "blockers": 0, "majors": 1, "minors": 0, '
      '"findings": [{"level": "Major", "note": "the base is red", '
      '"fix": "use /orbi fix-base"}]}')
"""


def session_script(template: str, marker: Path) -> str:
    """The fake review session: prove it ran, then answer its verdict."""
    return template.replace("__MARKER__", str(marker))


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """A local bare origin plus the delivery worktree cloned from it."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    worktree = worktree_dir(tmp_path)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(origin), str(worktree)],
        capture_output=True, text=True, check=True,
    )
    git(worktree, "config", "user.email", "pilot@test.local")
    git(worktree, "config", "user.name", "Pilot")
    (worktree / "a.txt").write_text("a", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "first")
    git(worktree, "push", "origin", "main")
    return worktree


def worktree_dir(tmp_path: Path) -> Path:
    """The path the tick derives from repo_dir, repo, Issue and run id."""
    return (
        tmp_path / ".worktrees" / f"orbi-owner-repo-issue-{ISSUE}-{RUN_ID}"
    )


def repo_dir_for(worktree: Path) -> Path:
    """The configured repo_dir the worktree path is derived from."""
    return worktree.parent.parent


def base_commit(worktree: Path) -> str:
    return git(worktree, "rev-parse", "origin/main")


def push_base_commit(worktree: Path, *, message="base fix") -> str:
    """Add one commit on main (the base advance) and push it."""
    git(worktree, "checkout", "main")
    git(worktree, "commit", "--allow-empty", "-m", message)
    git(worktree, "push", "origin", "main")
    head = git(worktree, "rev-parse", "HEAD")
    git(worktree, "checkout", "-")
    return head


def start_delivery(worktree: Path, *, behind: bool) -> str:
    """Create, commit and push the delivery branch; return its head."""
    start = "origin/main~1" if behind else "origin/main"
    git(worktree, "checkout", "-b", BRANCH, start)
    (worktree / "delivery.txt").write_text("delivery", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "delivery")
    head = git(worktree, "rev-parse", "HEAD")
    git(worktree, "push", "origin", f"HEAD:{BRANCH}")
    return head


def scene_comment() -> dict:
    """The trusted 'Orbi opened PR' comment the resume scene is read from."""
    return {
        "body": (
            f"<!-- orbi:run={RUN_ID} -->\n"
            + scene_mod.render(scene_mod.Scene(
                run_id=RUN_ID, base_branch="main",
                base_sha=BASE_SHA_FROZEN, pr_url=PR_URL,
            ))
            + f"\nOrbi opened PR: {PR_URL}"
        ),
        "authorAssociation": "OWNER",
        "author": {"login": "maintainer"},
    }


class FakeGh:
    """The runner's gh calls for one tick, recorded and answered.

    Dispatch is by verb; an unexpected command raises, so a scene the test
    did not model fails loudly instead of answering something plausible.
    Every gh argv goes through the ONE subprocess seam; every command that is
    not gh is real git (seam.run_command is captured before patching).
    """

    def __init__(self, worktree: Path, *, checks=None, base_runs=None,
                 mergeable="MERGEABLE", issue_labels=None):
        self.worktree = worktree
        self.rollup = list(checks or [])
        self.base_runs = dict(base_runs or {})
        self.mergeable = mergeable
        self.issues: dict[int, dict] = {
            ISSUE: {
                "number": ISSUE, "title": "Review task", "state": "OPEN",
                "labels": list(issue_labels or ["ai-pr-opened"]),
                "milestone": None,
                "comments": [scene_comment()],
                "author": {"login": "issue-author"},
            },
        }
        self.label_history: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []
        self.pr_comments: list[str] = []
        self.progress: dict[int, list[dict]] = {}
        self.commands: list[list[str]] = []
        # The in-place comment edits (the recovery scan rewrites a paused
        # comment's state instead of posting a second one).
        self.comment_edits: list[str] = []
        # The commits whose check-runs read fails server-side (Issue #2442,
        # acceptance 6: a failed read is reported as a read failure).
        self.read_failures: set[str] = set()
        self._next_id = 5000

    # --- the seam ---------------------------------------------------------

    def run_command(self, command, **kwargs):
        self.commands.append(list(command))
        if command[0] != "gh":
            return _REAL_COMMAND(command, **kwargs)
        return {
            "pr": self._pr, "issue": self._issue, "api": self._api,
        }[command[1]](command, **kwargs)

    def run_git_network_command(self, command, **kwargs):
        runner = kwargs.pop("command_runner", None) or _REAL_COMMAND
        return runner(command, **kwargs)

    # --- gh verbs ---------------------------------------------------------

    def _pr(self, command, **kwargs):
        return {
            "list": self._pr_list, "view": self._pr_view,
            "comment": self._pr_comment,
        }[command[2]](command, **kwargs)

    def _pr_list(self, _command, **_kwargs):
        return json.dumps([{
            "number": ISSUE, "url": PR_URL,
            "baseRefName": "main",
            "baseRefOid": base_commit(self.worktree),
            "headRefName": BRANCH,
            "headRefOid": git(self.worktree, "rev-parse", "HEAD"),
            "headRepository": {"name": "repo"},
            "headRepositoryOwner": {"login": "owner"},
            "isCrossRepository": False,
            "body": f"<!-- orbi:run={RUN_ID} -->\n\nFixes #{ISSUE}",
        }])

    def _pr_view(self, _command, **_kwargs):
        return json.dumps({
            "state": "OPEN",
            "mergeable": self.mergeable,
            "headRefOid": git(self.worktree, "rev-parse", "HEAD"),
            "statusCheckRollup": list(self.rollup),
        })

    def _pr_comment(self, command, **_kwargs):
        self.pr_comments.append(command[-1])
        return ""

    def _issue(self, command, **kwargs):
        return {
            "view": self._issue_view, "comment": self._issue_comment,
            "list": self._issue_list, "create": self._issue_create,
        }[command[2]](command, **kwargs)

    def _issue_view(self, command, **_kwargs):
        number = int(command[3])
        fields = command[command.index("--json") + 1].split(",")
        assert set(fields) <= {"labels", "comments"}, fields
        issue = self.issues[number]
        rendered = {"labels": [{"name": n} for n in issue["labels"]]}
        if "comments" in fields:
            rendered["comments"] = list(issue["comments"])
        return json.dumps(rendered)

    def _issue_comment(self, command, **_kwargs):
        number = int(command[3])
        body = command[command.index("--body") + 1]
        self._append_comment(number, body, login="orbi-bot")
        self.comments.append((number, body))
        return f"https://github.com/{REPO}/issues/{number}#issuecomment-1"

    def _issue_list(self, command, **_kwargs):
        state = command[command.index("--state") + 1]
        limit = int(command[command.index("--limit") + 1])
        search = (
            command[command.index("--search") + 1]
            if "--search" in command else ""
        )
        prefix = search.split('"')[1] if '"' in search else ""
        matches = [
            {
                "number": number,
                "url": f"https://github.com/{REPO}/issues/{number}",
                "title": issue["title"],
            }
            for number, issue in sorted(self.issues.items())
            if issue["state"].lower() == state.lower()
            and issue["title"].startswith(prefix)
        ]
        return json.dumps(matches[:limit])

    def _issue_create(self, command, **_kwargs):
        number = max(self.issues) + 1
        labels = [
            command[index + 1] for index, part in enumerate(command)
            if part == "--label"
        ]
        self.issues[number] = {
            "number": number,
            "title": command[command.index("--title") + 1],
            "body": command[command.index("--body") + 1],
            "state": "OPEN", "labels": labels, "milestone": None,
            "comments": [], "author": {"login": "orbi-bot"},
        }
        return f"https://github.com/{REPO}/issues/{number}"

    def _append_comment(self, number: int, body: str, *, login: str) -> None:
        self._next_id += 1
        self.issues[number]["comments"].append({
            "id": self._next_id,
            "url": f"https://github.com/{REPO}/issues/{number}"
                   f"#issuecomment-{self._next_id}",
            "author": {"login": login},
            "authorAssociation": "NONE",
            "createdAt": "2026-11-26T00:00:00Z",
            "body": body,
        })

    # --- gh api -----------------------------------------------------------

    def _api(self, command, **_kwargs):
        args = [part for part in command[2:] if part != "--include"]
        method = ""
        if "--method" in args:
            # gh accepts the flag on either side of the path.
            index = args.index("--method")
            method = args[index + 1]
            args = args[:index] + args[index + 2:]
        path, flags = args[0], args[1:]
        if path.endswith("/check-runs"):
            return self._check_runs(path)
        if "/collaborators/" in path:
            return json.dumps(
                {"permission": "write", "role_name": "write"},
            )
        if path.startswith(f"repos/{REPO}/branches/"):
            # The base branch's LIVING head (the recovery scan's read).
            return json.dumps({
                "protected": False,
                "commit": {"sha": base_commit(self.worktree)},
            })
        if path.endswith("/labels") and method == "PUT":
            labels = [
                part.split("=", 1)[1] for part in flags
                if part.startswith("labels[]=")
            ]
            self.label_history.append(labels)
            self.issues[int(path.split("/")[-2])]["labels"] = labels
            return json.dumps([{"name": name} for name in labels])
        if path.endswith("/comments") and not method:
            return json.dumps(self.progress.get(int(path.split("/")[-2]), []))
        if path.endswith("/comments") and method == "POST":
            number = int(path.split("/")[-2])
            body = next(
                part.split("=", 1)[1] for part in flags
                if part.startswith("body=")
            )
            self._next_id += 1
            entry = {"id": self._next_id, "body": body}
            self.progress.setdefault(number, []).append(entry)
            return json.dumps(entry)
        if "/issues/comments/" in path and method == "PATCH":
            body = next(
                part.split("=", 1)[1] for part in flags
                if part.startswith("body=")
            )
            self.comment_edits.append(body)
            return json.dumps({"id": int(path.rsplit("/", 1)[1])})
        raise AssertionError(f"unexpected gh api call: {command}")

    def _check_runs(self, path: str) -> str:
        sha = path.split("/commits/")[1].split("/")[0]
        if sha in self.read_failures:
            raise subprocess.CalledProcessError(
                1, ["gh"], output="",
                stderr="gh: Server Error (HTTP 503)",
            )
        runs = self.base_runs.get(sha, [])
        return json.dumps([{"total_count": len(runs), "check_runs": runs}])


_REAL_COMMAND = seam.run_command


def install(monkeypatch, fake: FakeGh) -> None:
    """Wire the one gh boundary and the one git network boundary."""
    monkeypatch.setattr(seam, "run_command", fake.run_command)
    monkeypatch.setattr(
        seam, "run_git_network_command", fake.run_git_network_command,
    )


def prompt_files(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("implement", encoding="utf-8")
    (tmp_path / "prompt_review.md").write_text("review", encoding="utf-8")


def tick(clone: Path, tmp_path: Path, fake: FakeGh, monkeypatch, *,
         session=REVIEW_PASS) -> tuple[bool | None, Path]:
    """Run ONE real review tick of the delivery; return (outcome, marker).

    The marker file exists only when a review session actually ran — the
    ticket's central claim is that a base-caused failure starts none.
    """
    prompt_files(tmp_path)
    marker = tmp_path / "session-started"
    install_fake_pi(monkeypatch, tmp_path, session_script(session, marker))
    install(monkeypatch, fake)
    config = config_domain.RunnerConfig(
        repo_dir=repo_dir_for(clone),
        deploy_home=repo_dir_for(clone),
        base_branch="main", base_sha=BASE_SHA_FROZEN, run_id=RUN_ID,
        prompt=tmp_path / "prompt.md",
        prompt_review=tmp_path / "prompt_review.md",
    )
    run_state.write_run_state(run_state.RunContext(
        run_id=RUN_ID, issue=ISSUE, branch=BRANCH, worktree=clone,
        source_repo=REPO,
    ))
    outcome = review_merge._run_review_round(
        PR_URL, {"number": ISSUE, "title": "Review task", "labels": []},
        config, REPO,
    )
    return outcome, marker


def record_bodies(fake: FakeGh, number: int = ISSUE) -> list[str]:
    """Every comment body of one Issue carrying the base_ci_red block."""
    return [
        comment["body"] for comment in fake.issues[number]["comments"]
        if "orbi:base-ci-red:v1" in comment["body"]
    ]


def fix_issues(fake: FakeGh) -> list[dict]:
    """The base-branch fix Issues the tick opened."""
    return [
        issue for issue in fake.issues.values()
        if issue["title"] == base_ci_red.triage_title("main")
    ]

# --------------------------------------------------------------------------
# Acceptance 1: a head up to date with a red base pauses before the session
# --------------------------------------------------------------------------


def test_a_base_caused_failure_pauses_as_base_ci_red_without_a_session(
        clone, tmp_path, monkeypatch, bind_run_id, caplog,
):
    """Acceptance 1: the head contains the base, check X concluded red on
    both. The tick starts no review session, records reason_code base_ci_red
    with an orbi:base-ci-red:v1 block in state waiting, and opens the fix
    Issue — instead of reviewing into human_decision_required."""
    bind_run_id(RUN_ID)
    caplog.set_level("INFO")
    start_delivery(clone, behind=False)
    fake = FakeGh(clone, checks=rollup(), base_runs={
        base_commit(clone): [check_run()],
    })

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    # No review session, no round: the pause is decided before it.
    assert not marker.exists()
    # The Issue is paused (ai-blocked alone) and carries the record.
    assert fake.issues[ISSUE]["labels"] == ["ai-blocked"]
    assert ["ai-blocked"] in fake.label_history
    bodies = record_bodies(fake)
    assert len(bodies) == 1, bodies
    body = bodies[0]
    record = base_ci_red.parse(body)
    assert record.state == base_ci_red.STATE_WAITING
    assert record.base_branch == "main"
    assert record.base_sha == base_commit(clone)
    assert record.pr == ISSUE
    assert record.failing_checks == (CHECK,)
    assert '"reason_code":"base_ci_red"' in body
    # No review ran in this tick, so the comment never claims one did.
    assert "is not the cause" in body
    assert "review passed" not in body
    # ONE fix Issue for the base branch, labelled ai-ready + p0.
    fixes = fix_issues(fake)
    assert len(fixes) == 1, fixes
    assert set(fixes[0]["labels"]) == {"ai-ready", "p0"}
    assert f"created #{fixes[0]['number']}" in body
    # The delivery PR got no failure comment: the pause lives on the Issue.
    assert fake.pr_comments == []
    assert "review_base_checks_pending" not in caplog.text


def test_an_open_fix_issue_is_reused_instead_of_a_second_one(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 1, second half: an already OPEN fix Issue for the base
    branch is reused, so a second paused delivery never opens a second one."""
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    fake = FakeGh(clone, checks=rollup(), base_runs={
        base_commit(clone): [check_run()],
    })
    fake.issues[70] = {
        "number": 70, "title": base_ci_red.triage_title("main"),
        "body": "existing", "state": "OPEN", "labels": ["ai-ready", "p0"],
        "milestone": None, "comments": [],
        "author": {"login": "orbi-bot"},
    }

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    assert not marker.exists()
    assert fix_issues(fake) == [fake.issues[70]]
    assert f"reused #{70}" in record_bodies(fake)[0]


# --------------------------------------------------------------------------
# Acceptance 3: the base commit compared is the one the head CONTAINS
# --------------------------------------------------------------------------


def test_the_frozen_base_oid_is_not_the_one_compared(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 3: the frozen base_sha is GREEN on X while the current base
    tip the head contains is RED on X. The tick must still record
    base_ci_red — a base that turned red after the freeze is recognised."""
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    tip = base_commit(clone)
    fake = FakeGh(clone, checks=rollup(), base_runs={
        BASE_SHA_FROZEN: [check_run(conclusion="success")],
        tip: [check_run()],
    })

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    assert not marker.exists()
    record = base_ci_red.parse(record_bodies(fake)[0])
    assert record.base_sha == tip
    assert record.base_sha != BASE_SHA_FROZEN


def test_a_base_absorbed_earlier_is_the_commit_that_is_compared(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 3, the absorb shape: the frozen base_sha is green, the
    absorbed base commit the head carries is red on X, so the tick pauses
    instead of reviewing the head."""
    bind_run_id(RUN_ID)
    absorbed = push_base_commit(clone)
    start_delivery(clone, behind=False)
    assert git(clone, "merge-base", "--is-ancestor", absorbed, "HEAD") == ""
    fake = FakeGh(clone, checks=rollup(), base_runs={
        BASE_SHA_FROZEN: [check_run(conclusion="success")],
        absorbed: [check_run()],
    })

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    assert not marker.exists()
    assert base_ci_red.parse(record_bodies(fake)[0]).base_sha == absorbed


# --------------------------------------------------------------------------
# Acceptance 4: a failure of the PR's OWN keeps the review path
# --------------------------------------------------------------------------


def test_a_check_that_is_green_on_the_base_still_reviews(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 4: check Y failed on the PR and is green on the base. The
    session runs as today and its blocked_on_human_decision verdict is still
    recorded as human_decision_required — no fix Issue, no base-ci block."""
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    fake = FakeGh(clone, checks=rollup(OTHER_CHECK), base_runs={
        base_commit(clone): [check_run(OTHER_CHECK, conclusion="success")],
    })

    outcome, marker = tick(
        clone, tmp_path, fake, monkeypatch, session=REVIEW_BLOCKED,
    )

    assert outcome is None
    assert marker.exists()
    assert fake.issues[ISSUE]["labels"] == ["ai-blocked"]
    assert fix_issues(fake) == []
    assert record_bodies(fake) == []
    body = fake.comments[-1][1]
    assert '"reason_code":"human_decision_required"' in body


# --------------------------------------------------------------------------
# Acceptance 5: a base check that has not concluded is not green
# --------------------------------------------------------------------------


def test_a_running_base_check_defers_without_a_record_or_a_session(
        clone, tmp_path, monkeypatch, bind_run_id, caplog,
):
    """Acceptance 5: the base's matching check is still running. The tick
    neither records base_ci_red nor starts a session: it defers and the next
    tick decides."""
    bind_run_id(RUN_ID)
    caplog.set_level("INFO")
    start_delivery(clone, behind=False)
    fake = FakeGh(clone, checks=rollup(), base_runs={
        base_commit(clone): [check_run(status="in_progress",
                                      conclusion=None)],
    })

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is False
    assert not marker.exists()
    assert "review_base_checks_pending" in caplog.text
    assert record_bodies(fake) == []
    assert fix_issues(fake) == []
    assert fake.label_history == []
    assert len(fake.comments) == 0

    # The next tick, once the base check concluded red, pauses the delivery.
    fake.base_runs[base_commit(clone)] = [check_run()]
    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)
    assert outcome is None
    assert not marker.exists()
    assert len(record_bodies(fake)) == 1
    assert len(fix_issues(fake)) == 1


# --------------------------------------------------------------------------
# Acceptance 6: a failed base read is reported as the read failure it is
# --------------------------------------------------------------------------


def test_a_failed_base_check_read_is_reported_and_never_pauses(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 6: the base's check-runs read fails. The tick reports that
    failure (ai-fix-needed, the read error in the comment) and writes no
    base_ci_red record."""
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    tip = base_commit(clone)
    fake = FakeGh(clone, checks=rollup())
    fake.read_failures.add(tip)

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    assert not marker.exists()
    assert fake.issues[ISSUE]["labels"] == ["ai-fix-needed"]
    assert record_bodies(fake) == []
    assert fix_issues(fake) == []
    body = fake.comments[-1][1]
    assert "Server Error (HTTP 503)" in body


# --------------------------------------------------------------------------
# Acceptance 2: one red base, two paused deliveries, one fix Issue
# --------------------------------------------------------------------------


def test_two_deliveries_paused_on_one_red_base_resume_together(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """Acceptance 2: two Issues hit the same red base check. Both are
    recorded base_ci_red against the same single fix Issue, and when X turns
    green on the base the next tick resumes both — no label edit by a person.
    """
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    tip = base_commit(clone)
    fake = FakeGh(clone, checks=rollup(), base_runs={tip: [check_run()]})
    second = 5
    fake.issues[second] = {
        "number": second, "title": "Second review task", "state": "OPEN",
        "labels": ["ai-pr-opened"], "milestone": None,
        "comments": [{
            "body": (
                f"<!-- orbi:run={RUN_ID} -->\n"
                + scene_mod.render(scene_mod.Scene(
                    run_id=RUN_ID, base_branch="main",
                    base_sha=BASE_SHA_FROZEN,
                    pr_url=f"https://github.com/{REPO}/pull/{second}",
                ))
                + f"\nOrbi opened PR: https://github.com/{REPO}/pull/{second}"
            ),
            "authorAssociation": "OWNER",
            "author": {"login": "maintainer"},
        }],
        "author": {"login": "issue-author"},
    }
    # The second delivery: its own worktree, branch and PR fake.
    second_worktree = tmp_path / ".worktrees" / (
        f"orbi-owner-repo-issue-{second}-{RUN_ID}"
    )
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(second_worktree)],
        capture_output=True, text=True, check=True,
    )
    git(second_worktree, "config", "user.email", "pilot@test.local")
    git(second_worktree, "config", "user.name", "Pilot")
    second_branch = f"orbi/owner-repo-issue-{second}"
    git(second_worktree, "checkout", "-b", second_branch, "origin/main")
    (second_worktree / "second.txt").write_text("second", encoding="utf-8")
    git(second_worktree, "add", ".")
    git(second_worktree, "commit", "-m", "second delivery")
    git(second_worktree, "push", "origin", f"HEAD:{second_branch}")

    def run_second() -> None:
        pr_url = f"https://github.com/{REPO}/pull/{second}"
        fake.worktree = second_worktree
        fake.label_history.clear()
        config = config_domain.RunnerConfig(
            repo_dir=repo_dir_for(second_worktree),
            deploy_home=repo_dir_for(second_worktree),
            base_branch="main", base_sha=BASE_SHA_FROZEN, run_id=RUN_ID,
            prompt=tmp_path / "prompt.md",
            prompt_review=tmp_path / "prompt_review.md",
        )
        run_state.write_run_state(run_state.RunContext(
            run_id=RUN_ID, issue=second, branch=second_branch,
            worktree=second_worktree, source_repo=REPO,
        ))
        review_merge._run_review_round(
            pr_url, {"number": second, "title": "Second review task",
                     "labels": []},
            config, REPO,
        )

    prompt_files(tmp_path)
    install(monkeypatch, fake)
    install_fake_pi(monkeypatch, tmp_path, session_script(
        REVIEW_PASS, tmp_path / "session-started",
    ))
    tick(clone, tmp_path, fake, monkeypatch)
    run_second()

    assert fake.issues[ISSUE]["labels"] == ["ai-blocked"]
    assert fake.issues[second]["labels"] == ["ai-blocked"]
    assert len(record_bodies(fake, ISSUE)) == 1
    assert len(record_bodies(fake, second)) == 1
    fixes = fix_issues(fake)
    assert len(fixes) == 1, fixes
    assert f"created #{fixes[0]['number']}" in record_bodies(fake, ISSUE)[0]
    assert f"reused #{fixes[0]['number']}" in record_bodies(fake, second)[0]

    # The base turns green: the next tick resumes BOTH deliveries itself.
    fake.base_runs[tip] = [check_run(conclusion="success")]
    resumed = base_ci_fix.recover(REPO, language_code="en")

    assert sorted(resumed) == [ISSUE, second]
    assert fake.issues[ISSUE]["labels"] == ["ai-fix-needed"]
    assert fake.issues[second]["labels"] == ["ai-fix-needed"]
    assert len(fix_issues(fake)) == 1
    # Both paused comments now say so, in place, with no new block.
    assert [
        base_ci_red.parse(body).state for body in fake.comment_edits
    ] == [base_ci_red.STATE_RESUMING] * 2


# --------------------------------------------------------------------------
# The decision itself: one read, three answers (pending / red / green)
# --------------------------------------------------------------------------


def answer_check_runs(monkeypatch, runs, *, commit="c0ffee"):
    """Answer the ONE base check-runs read the decision makes."""
    calls = []

    def fake(command, **kwargs):
        assert command[0] == "gh" and command[1] == "api", command
        assert command[2] == f"repos/{REPO}/commits/{commit}/check-runs"
        calls.append(list(command))
        return json.dumps([{"total_count": len(runs), "check_runs": runs}])

    monkeypatch.setattr(seam, "run_command", fake)
    return calls


def test_pending_checks_reports_only_unconcluded_base_runs(monkeypatch):
    """A running base run is not green; a name the base never ran is not
    pending — the base carries no such check, so it cannot explain it."""
    answer_check_runs(monkeypatch, [
        check_run(status="in_progress", conclusion=None),
        check_run(OTHER_CHECK, conclusion="success", run_id=92),
    ])
    assert base_ci_red.pending_checks(
        REPO, [CHECK, OTHER_CHECK, "never-ran"], "c0ffee",
    ) == [CHECK]


def test_the_decision_needs_a_failing_check_and_the_base_commit():
    """Without both facts the tick reviews as today (nothing is comparable,
    so it reads no base checks at all)."""
    pr = {"number": ISSUE}
    assert review_base_ci_red.stop_before_session(
        pr, "main", source_repo=REPO, failed=(), base_tip="c0ffee",
    ) is False
    assert review_base_ci_red.stop_before_session(
        pr, "main", source_repo=REPO, failed=(CHECK,), base_tip=None,
    ) is False


def test_a_check_the_base_never_ran_keeps_the_review_path(monkeypatch):
    """The base has no run for the failed name at all: it cannot explain
    the failure, so nothing is recorded and no session is skipped."""
    answer_check_runs(
        monkeypatch, [check_run(OTHER_CHECK, conclusion="success")],
    )
    assert review_base_ci_red.stop_before_session(
        {"number": ISSUE}, "main", source_repo=REPO,
        failed=(CHECK,), base_tip="c0ffee",
    ) is False


def test_the_fake_gh_fails_loudly_on_an_unmodelled_call():
    """The fake answers only the scene it models: an unexpected gh call is
    a loud failure, never a plausible answer about the wrong thing."""
    fake = FakeGh(Path("/nonexistent"))
    with pytest.raises(AssertionError, match="unexpected gh api call"):
        fake.run_command(
            ["gh", "api", f"repos/{REPO}/actions/runners"], cwd=Path("."),
        )


def test_a_mixed_red_pr_pauses_on_the_base_cause_first(
        clone, tmp_path, monkeypatch, bind_run_id,
):
    """A PR fails X (also red on the base) and Y (green on the base). The
    tick pauses it on the base cause FIRST, naming only X; once the base is
    green the next tick reviews, and the reviewer's own verdict decides the
    remaining failure."""
    bind_run_id(RUN_ID)
    start_delivery(clone, behind=False)
    tip = base_commit(clone)
    fake = FakeGh(
        clone, checks=rollup(CHECK) + rollup(OTHER_CHECK),
        base_runs={
            tip: [
                check_run(CHECK),
                check_run(OTHER_CHECK, conclusion="success", run_id=92),
            ],
        },
    )

    outcome, marker = tick(clone, tmp_path, fake, monkeypatch)

    assert outcome is None
    assert not marker.exists()
    record = base_ci_red.parse(record_bodies(fake)[0])
    assert record.failing_checks == (CHECK,)
    assert record.state == base_ci_red.STATE_WAITING

    # The base turns green on both checks: the recovery resumes the delivery
    # and the next tick reviews the failure the PR itself still has.
    fake.base_runs[tip] = [
        check_run(CHECK, conclusion="success"),
        check_run(OTHER_CHECK, conclusion="success", run_id=92),
    ]
    assert base_ci_fix.recover(REPO, language_code="en") == [ISSUE]
    outcome, marker = tick(
        clone, tmp_path, fake, monkeypatch, session=REVIEW_BLOCKED,
    )
    assert outcome is None
    assert marker.exists()
    assert '"reason_code":"human_decision_required"' in fake.comments[-1][1]
    assert len(fix_issues(fake)) == 1

