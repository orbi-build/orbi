"""Issue #1749: a failure in the FIRST delivery step reaches the Issue.

Freezing the base branch runs before the claim and before the delivery's
failure range, so a base branch the remote does not have escaped every
catch: no label, no comment, no help line, and the next tick claimed the
same Issue again and failed the same way (`fatal: couldn't find remote ref`
only in the host journal).

The user journey driven here is the real one — the Runner's delivery
dispatch `runner.process_issue`, the same entry the tick calls, plus the
real ready pickup scan (`orbi.claim.pick_issue`) for the next tick:

* the missing-branch failure posts the blocked comment, names the
  CONFIGURED branch, says the repository has no such branch, ends with
  the community help line (en + zh), and leaves the Issue `ai-blocked`
  alone — so the next tick claims nothing;
* a different error of the same step (a network error after the existing
  transient retries) reports the same way instead of raising.

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

# The branch the #1753 wordings are asserted with: the reader sees the
# CONFIGURED branch verbatim, whatever real Git printed.
WORDING_BRANCH = "develop"

# The sentence each language uses for "this repository has no such
# branch" (Issue #1753): the reason names the branch and says the
# repository lacks it, never Git's raw message.
MISSING_REF_SUFFIX = {
    "en": "This repository has no branch",
    "zh": "这个仓库里没有分支",
}

# The exact #1753 wording: what is wrong, where to fix it (the web
# connection or the config file) and how to restart the Issue. Both
# sentences are asserted verbatim, so a rephrase is a failing test.
REASON_TEXT = {
    "en": (
        f"This repository has no branch `{WORDING_BRANCH}`, so Orbi "
        "doesn't know which branch to start from."
    ),
    "zh": (
        f"这个仓库里没有分支 `{WORDING_BRANCH}`，Orbi 不知道该从哪个"
        "分支开始改。"
    ),
}
ACTION_TEXT = {
    "en": (
        f"Either create `{WORDING_BRANCH}` in the repository, or switch "
        "Orbi to a branch that exists, wherever you set up this repository "
        "(the branch you picked when connecting it on the web, or "
        "base_branch in your config file). Then remove `ai-blocked` and "
        "add `ai-ready` again, and Orbi will start over."
    ),
    "zh": (
        f"两种改法选一种：在仓库里建好分支 `{WORDING_BRANCH}`；或者把 "
        "Orbi 用的基础分支换成仓库里已有的分支，在你当初设置这个仓库的"
        "地方改（网页上连接仓库时选的分支，或配置文件里的 base_branch）。"
        "改好后去掉 `ai-blocked` 标签、重新加上 `ai-ready`，Orbi 会"
        "重新开始。"
    ),
}


def _progress_api(_command: list[str]) -> str:
    """Accept the publisher's comment write (the run's milestone).

    The reply shape is the real `gh api` comment object; the milestone's
    own body is not what these tests assert (the Issue's failure comment
    is posted through `gh issue comment`, so the fake's Issue state holds
    it).
    """
    return json.dumps({"id": 1, "body": ""})


def fake_seam(gh: FakeGh, *, fetch_stderr: str):
    """Answer the git/gh argv the claim and the failure report issue.

    `git fetch origin <base>` raises the error the real remote would give:
    the base branch the checkout asks for is not there (or the network
    died). `git ls-remote` answers "no such branch" (the no-op probe).
    The external-takeover scan of `pick_issue` has no candidates.
    """
    attempts: list = []

    def run(command, **kwargs):
        if command[0] == "git":
            if command[1] == "fetch":
                attempts.append(command)
                raise subprocess.CalledProcessError(
                    128, command, stderr=fetch_stderr,
                )
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
    return run


MISSING_REF_STDERR = f"fatal: couldn't find remote ref {MISSING_BRANCH}"


def world(monkeypatch, tmp_path, *, language_code: str = "en",
          fetch_stderr: str = MISSING_REF_STDERR,
          base_branch: str = MISSING_BRANCH):
    gh = FakeGh(REPO)
    gh.add_issue(NUMBER, title="Base branch is gone", labels=("ai-ready",))
    command_runner = fake_seam(gh, fetch_stderr=fetch_stderr)
    monkeypatch.setattr(seam, "run_command", command_runner)
    monkeypatch.setattr(seam, "new_run_id", lambda: RUN_ID)
    # The transient-retry backoff would make the test wait for the real
    # 1s/2s budget; the retried-then-exhausted behavior stays real.
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    config = config_domain.RunnerConfig(
        repo_dir=tmp_path, base_branch=base_branch,
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
def test_the_blocked_comment_says_what_where_and_how_to_restart(
        monkeypatch, tmp_path, language_code):
    """Issue #1753 acceptance 1-3: the reader of the blocked comment learns
    what is wrong (the repository has no such branch), where to fix it (the
    place the repository was set up — the web connection or the config
    file) and how to restart the Issue, verbatim, with Git's raw error kept
    inside the collapsed run details."""
    fetch_stderr = f"fatal: couldn't find remote ref {WORDING_BRANCH}"
    gh, config, issue, _ = world(
        monkeypatch, tmp_path, language_code=language_code,
        fetch_stderr=fetch_stderr, base_branch=WORDING_BRANCH,
    )

    runner.process_issue(issue, config, REPO)

    body = assert_blocked_exit(gh, language_code)
    # The two sentences the reader acts on, word for word.
    assert REASON_TEXT[language_code] in body
    assert ACTION_TEXT[language_code] in body
    # Git's raw answer is evidence, not the explanation: it sits in the
    # collapsed run details, never before them.
    details_at = body.index("<details>")
    assert fetch_stderr not in body[:details_at]
    assert fetch_stderr in body[details_at:]
    if language_code == "zh":
        # A Chinese comment is fully Chinese where the reader reads: no
        # English term and no mention of a "Runner configuration" the web
        # tenant does not have.
        assert "base branch" not in body[:details_at]
        assert "Runner" not in body[:details_at]


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
        base_branch=MISSING_BRANCH,
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


def test_another_failure_of_the_first_step_is_reported_not_raised(
        monkeypatch, tmp_path):
    """Acceptance 3: a different error in the same step (a network failure
    that exhausts the existing transient retries) also produces the failure
    comment with the help line and a label — never an uncaught exception."""
    network = (
        "fatal: unable to access 'https://github.com/owner/repo.git/': "
        "Connection timed out"
    )
    gh, config, issue, command_runner = world(
        monkeypatch, tmp_path, fetch_stderr=network,
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
