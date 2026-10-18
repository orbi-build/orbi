"""Issue #1749: a failure in the FIRST delivery step reaches the Issue.

Freezing the base branch runs before the claim and before the delivery's
failure range, so a base branch the remote does not have escaped every
catch: no label, no comment, no help line, and the next tick claimed the
same Issue again and failed the same way (Git's error only in the host
journal).

The user journey driven here is the real one — the Runner's delivery
dispatch `runner.process_issue`, the same entry the tick calls, plus the
real ready pickup scan (`orbi.claim.pick_issue`) for the next tick:

* the missing-branch failure posts the blocked comment, names the
  CONFIGURED branch, says it does not exist in the repository, ends with
  the community help line (en + zh), and leaves the Issue `ai-blocked`
  alone — so the next tick claims nothing;
* the reason is decided by the remote's own answer (Issue #1756): the
  failed fetch is confirmed with `git ls-remote --heads`, so a localized
  Git message (a zh_CN host) still yields `base_branch_missing`, while a
  listed branch or a failing probe keeps the existing classification.

The git/gh seam is the fake `run_command` below (never a module-level
patch), and `freeze_base` itself runs for real: the tests observe the
actual fetch failure, not a stub of it.
"""
import json
import subprocess

import pytest

import orbi.claim as claim
import orbi.journal as journal
import orbi.runner as runner
from orbi import config as config_domain
from orbi import failure, language
from tests.fakes.github import FakeGh
from seam import seam

REPO = "owner/repo"
NUMBER = 1749
MISSING_BRANCH = "release/9"
RUN_ID = "feedface"

# The two wordings real Git prints when the fetched ref does not exist.
MISSING_REF_SUFFIX = {
    "en": "does not exist in the repository",
    "zh": "不存在",
}


def _progress_api(_command: list[str]) -> str:
    """Accept the publisher's comment write (the run's milestone).

    The reply shape is the real `gh api` comment object; the milestone's
    own body is not what these tests assert (the Issue's failure comment
    is posted through `gh issue comment`, so the fake's Issue state holds
    it).
    """
    return json.dumps({"id": 1, "body": ""})


def fake_seam(gh: FakeGh, *, fetch_stderr: str, ls_remote=""):
    """Answer the git/gh argv the claim and the failure report issue.

    `git fetch origin <base>` raises the error the real remote would give:
    the base branch the checkout asks for is not there (or the network
    died). `git ls-remote --heads` is the branch probe (Issue #1756):
    `ls_remote` is the remote's answer — the empty default (no such
    branch), a listed ref line, or the exception a failed probe raises.
    The external-takeover scan of `pick_issue` has no candidates.
    """
    attempts: list = []
    probes: list = []

    def run(command, **kwargs):
        if command[0] == "git":
            if command[1] == "fetch":
                attempts.append(command)
                raise subprocess.CalledProcessError(
                    128, command, stderr=fetch_stderr,
                )
            # Only the BASE-branch probe gets the configured answer;
            # the delivery branch genuinely does not exist yet.
            if command[-1] == f"refs/heads/{MISSING_BRANCH}":
                probes.append(command)
                if isinstance(ls_remote, BaseException):
                    raise ls_remote
                return ls_remote
            return ""
        if command[0] == "gh" and command[1] == "issue" \
                and command[2] == "list" and "--search" in command:
            search = command[command.index("--search") + 1]
            if "in:body" in search:
                return json.dumps([])
        if command[0] == "gh" and command[1] == "api":
            # The progress publisher's own comment traffic (the run's
            # milestone): no tracked comment exists yet, and the write is
            # accepted. The Issue's failure comment below goes through
            # `gh issue comment`, so the fake's Issue state holds it.
            return _progress_api(command)
        return gh(command)

    run.attempts = attempts
    run.probes = probes
    return run


MISSING_REF_STDERR = f"fatal: couldn't find remote ref {MISSING_BRANCH}"
# The zh_CN host's wording of the same Git failure: no English text the
# removed regex could have matched (Issue #1756).
LOCALIZED_MISSING_REF_STDERR = f"致命错误：无法找到远程引用 {MISSING_BRANCH}"
# The remote's answer when the branch DOES exist (a network failure, not
# a missing branch).
LISTED_BRANCH = f"{'a' * 40}\trefs/heads/{MISSING_BRANCH}\n"


def world(monkeypatch, tmp_path, *, language_code: str = "en",
          fetch_stderr: str = MISSING_REF_STDERR, ls_remote=""):
    gh = FakeGh(REPO)
    gh.add_issue(NUMBER, title="Base branch is gone", labels=("ai-ready",))
    command_runner = fake_seam(
        gh, fetch_stderr=fetch_stderr, ls_remote=ls_remote,
    )
    monkeypatch.setattr(seam, "run_command", command_runner)
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    # The transient-retry backoff would make the test wait for the real
    # 1s/2s budget; the retried-then-exhausted behavior stays real.
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, base_branch=MISSING_BRANCH,
        message_language=language_code,
    )
    issue = {
        "number": NUMBER, "title": "Base branch is gone", "body": "",
        "labels": [{"name": "ai-ready"}],
    }
    return gh, config, issue, command_runner


def blocked_comments(gh: FakeGh) -> list[str]:
    """The Issue's failure comments, identified by their kind marker."""
    return [
        comment["body"]
        for comment in gh.issues[NUMBER]["comments"]
        if "<!-- orbi:comment=failure -->" in comment["body"]
    ]


def assert_blocked_exit(gh: FakeGh, language_code: str) -> str:
    """The Issue is `ai-blocked` alone and one blocked comment was posted."""
    assert gh.issues[NUMBER]["labels"] == ["ai-blocked"]
    bodies = blocked_comments(gh)
    assert len(bodies) == 1, bodies
    body = bodies[0]
    # The community help line is the body's last paragraph; only the
    # runner fingerprint footer the comment renderer appends follows it.
    head, _, _ = body.partition("<!-- runner=")
    help_line = language.text(language_code, "help.community")
    assert head.rstrip().endswith(help_line), body
    assert body.count(help_line) == 1
    assert failure.parse(body).outcome == "blocked"
    return body


@pytest.mark.parametrize("language_code", ["en", "zh"])
def test_missing_base_branch_blocks_the_issue_and_names_the_branch(
        monkeypatch, tmp_path, language_code):
    """Acceptance 1: the configured branch the remote lacks is reported on
    the Issue — branch name, "does not exist in the repository", the
    community help line, and the `ai-blocked` terminal label."""
    gh, config, issue, command_runner = world(
        monkeypatch, tmp_path, language_code=language_code,
    )

    result = runner.process_issue(issue, config, REPO)

    assert result == runner.IssueResult("failed", None)
    # Git's missing-ref answer is not a transient network error: the fetch
    # is attempted exactly once, never retried.
    assert command_runner.attempts == [
        ["git", "fetch", "origin", MISSING_BRANCH],
    ]
    body = assert_blocked_exit(gh, language_code)
    # The comment names the CONFIGURED branch and states the fact.
    assert MISSING_BRANCH in body
    assert MISSING_REF_SUFFIX[language_code] in body
    # The machine-readable reason is the new failure class, and the
    # reader gets the one repair action.
    assert failure.parse(body).reason_code == "base_branch_missing"
    assert failure.parse(body).action_code == "fix_ticket"
    assert language.text(
        language_code, "failure.terminal_base_branch_missing",
    ) in body
    # The failure is recorded once, with the run marker of this attempt.
    assert f"<!-- orbi:run={RUN_ID} -->" in body


def test_the_blocked_issue_is_not_claimed_on_the_next_tick(
        monkeypatch, tmp_path):
    """Acceptance 2: the ready scan the next tick runs finds nothing —
    before the failure the same Issue WAS claimable (the control), after
    it it carries `ai-blocked` and stays untouched."""
    gh, config, issue, _ = world(monkeypatch, tmp_path)
    # Control: the untouched fixture is claimable, so the scan below is
    # really answering and not merely failing open.
    assert claim.pick_issue(REPO)["number"] == NUMBER

    runner.process_issue(issue, config, REPO)
    assert gh.issues[NUMBER]["labels"] == ["ai-blocked"]

    assert claim.pick_issue(REPO) is None


def test_a_localized_fetch_error_is_still_base_branch_missing(
        monkeypatch, tmp_path):
    """Acceptance 1: a zh_CN host's Git prints its own wording (the removed
    regex matched nothing there). The remote's empty answer to the branch
    probe still produces `reason_code: base_branch_missing`."""
    gh, config, issue, command_runner = world(
        monkeypatch, tmp_path, fetch_stderr=LOCALIZED_MISSING_REF_STDERR,
    )

    result = runner.process_issue(issue, config, REPO)

    assert result == runner.IssueResult("failed", None)
    body = assert_blocked_exit(gh, "en")
    record = failure.parse(body)
    assert record.reason_code == "base_branch_missing"
    assert record.action_code == "fix_ticket"
    assert MISSING_BRANCH in body
    assert language.text(
        "en", "failure.terminal_base_branch_missing",
    ) in body
    # The remote was asked directly, for the CONFIGURED branch.
    assert [
        "git", "ls-remote", "--heads", "origin",
        f"refs/heads/{MISSING_BRANCH}",
    ] in command_runner.probes


def test_a_network_failure_with_the_branch_still_listed_is_not_missing(
        monkeypatch, tmp_path):
    """Acceptance 2: the fetch failed, but the remote still lists the
    branch — the existing transient classification applies."""
    network = (
        "fatal: unable to access 'https://github.com/owner/repo.git/': "
        "Connection timed out"
    )
    gh, config, issue, command_runner = world(
        monkeypatch, tmp_path, fetch_stderr=network, ls_remote=LISTED_BRANCH,
    )

    result = runner.process_issue(issue, config, REPO)

    assert result == runner.IssueResult("failed", None)
    # The existing transient budget really ran and was exhausted.
    assert len(command_runner.attempts) == journal.GIT_NETWORK_MAX_ATTEMPTS
    body = assert_blocked_exit(gh, "en")
    # The existing classification is unchanged: a transient network error
    # stays transient, and its evidence reaches the comment.
    assert failure.parse(body).reason_code == "github_transient"
    assert "Connection timed out" in body
    assert MISSING_REF_SUFFIX["en"] not in body


def test_a_failing_probe_keeps_the_existing_classification(
        monkeypatch, tmp_path):
    """Acceptance 3: `ls-remote` itself fails, so the remote gave no
    answer — never `base_branch_missing`; the original failure stands."""
    network = (
        "fatal: unable to access 'https://github.com/owner/repo.git/': "
        "Connection timed out"
    )
    probe_failure = subprocess.CalledProcessError(
        128, ["git", "ls-remote", "--heads", "origin", MISSING_BRANCH],
        stderr=network,
    )
    gh, config, issue, command_runner = world(
        monkeypatch, tmp_path, fetch_stderr=network,
        ls_remote=probe_failure,
    )

    result = runner.process_issue(issue, config, REPO)

    assert result == runner.IssueResult("failed", None)
    body = assert_blocked_exit(gh, "en")
    record = failure.parse(body)
    assert record.reason_code == "github_transient"
    assert record.reason_code != "base_branch_missing"
    # The probe really ran and really failed.
    assert [
        "git", "ls-remote", "--heads", "origin",
        f"refs/heads/{MISSING_BRANCH}",
    ] in command_runner.probes
