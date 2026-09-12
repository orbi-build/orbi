"""Issue #738: bounded retries for read-only gh commands.

A transient credential/API blip (a keyring race under concurrent orbi
processes surfaces as ``HTTP 401: Bad credentials``; plus API rate
limits and GitHub-side 5xx) must not crash a whole tick that was only
reading. Retry lives in the read-only gh wrapper layer so a write path
can never reach it, and only gh's own transient error lines are
retried — a 404 or a parameter error is deterministic and re-raises on
the first attempt.
"""

import subprocess

import pytest

import orbi.runner as runner

TRANSIENT_STDERRS = [
    "HTTP 401: Bad credentials (https://api.github.com/graphql)",
    "HTTP 429: Too Many Requests (https://api.github.com/graphql)",
    "API rate limit exceeded for organization 'orbi-build'",
    "HTTP 502: Bad Gateway (https://api.github.com/graphql)",
    "HTTP 503: Service Unavailable (https://api.github.com/graphql)",
    "HTTP 504: Gateway Time-out (https://api.github.com/graphql)",
]


@pytest.mark.parametrize("stderr", TRANSIENT_STDERRS)
def test_gh_read_command_retries_transient_failures_then_succeeds(
    monkeypatch, caplog, stderr,
):
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, command, stderr=stderr)
        return "[]"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with caplog.at_level("INFO"):
        assert runner.run_gh_read_command(
            ["gh", "issue", "list", "--repo", "o/r",
             "--json", "number", "--limit", "1"],
            command_runner=fake_run,
        ) == "[]"

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert "gh_read_retry" in caplog.text
    assert "attempt=2" in caplog.text


def test_gh_read_command_returns_first_success_without_retry(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return "token"

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    assert runner.run_gh_read_command(
        ["gh", "auth", "token"], command_runner=fake_run,
    ) == "token"
    assert len(calls) == 1


def test_gh_read_command_exhausts_and_reraises_stderr_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command,
            stderr="HTTP 502: Bad Gateway (https://api.github.com/graphql)",
        )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.run_gh_read_command(
            ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
             "--json", "number", "--limit", "1"],
            command_runner=fake_run,
        )

    assert len(calls) == 3
    assert caught.value.stderr == (
        "HTTP 502: Bad Gateway (https://api.github.com/graphql)"
    )


def test_gh_read_command_does_not_retry_deterministic_failure(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command,
            stderr="HTTP 404: Not Found (https://api.github.com/repos/o/r)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_gh_read_command(
            ["gh", "issue", "view", "9", "--repo", "o/r"],
            command_runner=fake_run,
        )
    assert len(calls) == 1


def test_gh_read_command_does_not_retry_write_subcommands(monkeypatch):
    """A retried write can duplicate a side effect (a failed `gh pr merge`
    response may still have executed) — the wrapper must never retry one,
    even when the error looks transient."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command,
            stderr="HTTP 502: Bad Gateway (https://api.github.com/graphql)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    for command in (
        ["gh", "pr", "merge", "12", "--squash"],
        ["gh", "issue", "close", "9", "--repo", "o/r"],
        ["gh", "issue", "comment", "9", "--repo", "o/r", "--body", "x"],
        ["gh", "issue", "create", "--repo", "o/r", "--title", "t"],
        ["gh", "issue", "edit", "9", "--repo", "o/r"],
        ["gh", "pr", "comment", "12", "--body", "x"],
        ["gh", "pr", "create", "--head", "b"],
        ["gh", "release", "create", "v0.1.0"],
        ["gh", "release", "edit", "v0.1.0"],
        ["gh", "label", "create", "p0"],
    ):
        with pytest.raises(subprocess.CalledProcessError):
            runner.run_gh_read_command(command, command_runner=fake_run)
    assert len(calls) == 10


def test_gh_read_command_does_not_retry_gh_api_writes(monkeypatch):
    """`gh api` is a read only without a non-GET --method/-X override —
    the flag is gh's own read/write semantics, not a call-site list."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command,
            stderr="HTTP 429: Too Many Requests (https://api.github.com/graphql)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    for command in (
        ["gh", "api", "repos/o/r/issues/9/comments",
         "-f", "body=hi", "-X", "POST"],
        ["gh", "api", "repos/o/r/milestones/3",
         "--method", "PATCH", "-f", "state=closed"],
        ["gh", "api", "repos/o/r/labels", "--method=POST", "-f", "name=p0"],
        ["gh", "api", "-X", "DELETE", "repos/o/r/actions/variables/V"],
    ):
        with pytest.raises(subprocess.CalledProcessError):
            runner.run_gh_read_command(command, command_runner=fake_run)
    assert len(calls) == 4


def test_is_readonly_gh_command_classifies_gh_api_reads():
    assert runner._is_readonly_gh_command(["gh", "api", "repos/o/r"])
    assert runner._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--paginate", "--slurp"])
    assert runner._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method", "GET"])
    assert runner._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method=GET", "--jq", ".name"])
    assert runner._is_readonly_gh_command(["gh", "api", "-X", "GET", "repos/o/r"])
    # A trailing -X with no value is malformed; no method override was
    # parsed, so the command is still classified as a read (gh rejects it).
    assert runner._is_readonly_gh_command(["gh", "api", "repos/o/r", "-X"])
    assert not runner._is_readonly_gh_command(["gh", "api"])
    assert not runner._is_readonly_gh_command(["git", "push", "origin", "main"])
    assert not runner._is_readonly_gh_command(
        ["timeout", "30", "gh", "issue", "list"])


def test_is_readonly_gh_command_classifies_subcommands():
    for command in (
        ["gh", "issue", "list", "--repo", "o/r"],
        ["gh", "issue", "view", "9", "--repo", "o/r"],
        ["gh", "pr", "list", "--state", "open"],
        ["gh", "pr", "view", "12"],
        ["gh", "release", "view", "v0.1.0", "--repo", "o/r"],
        ["gh", "repo", "view", "o/r"],
        ["gh", "label", "list", "--repo", "o/r"],
        ["gh", "auth", "status"],
        ["gh", "auth", "token"],
    ):
        assert runner._is_readonly_gh_command(command), command
    for command in (
        ["gh", "issue", "close", "9"],
        ["gh", "issue"],
    ):
        assert not runner._is_readonly_gh_command(command), command


def test_list_issues_survives_transient_401_from_keyring_race(monkeypatch):
    """Issue #738 acceptance: the Issue's exact crash scene — the tick's
    `list_issues` read — recovers from a first-attempt keyring 401
    through the runner's own `run_command` seam."""
    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, command,
                stderr="HTTP 401: Bad credentials (https://api.github.com/graphql)",
            )
        return "[]"

    monkeypatch.setattr(runner, "run_command", fake_run)
    assert runner.list_issues(
        "orbi-build/orbi-cloud", state="open",
        search="label:ai-fix-needed,ai-pr-opened -label:ai-blocked",
        json_fields="number,title,state,url,labels", limit=1,
    ) == []
    assert len(calls) == 2
