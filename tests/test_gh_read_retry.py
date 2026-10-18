"""Issue #738 / #1760: bounded retries for gh commands.

A transient failure (GitHub rate limiting, 429, GitHub-side 5xx) must not
crash a whole tick that was only reading. Retry lives in the wrapper layer
so a write path can never reach the read retry loop, and it is decided by
the HTTP status the response carries (Issue #1760): `gh api --include`
puts the status line and the `x-ratelimit-remaining` header in the
error's stdout, so a 404 or a rejected credential is never mistaken for a
transient failure by a word in the error text. A rejected credential
(401) is a repair, not a retry.
"""

import subprocess

import pytest

import orbi.runner as runner
import orbi.github as github
from seam import seam


def failed_response(status, *, remaining=10, body='{"message": "failed"}'):
    """The stdout of a failed `gh api --include` (gh exits 1)."""
    return (
        f"HTTP/2.0 {status} Error\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"X-Ratelimit-Remaining: {remaining}\r\n"
        "\r\n"
        f"{body}"
    )


@pytest.mark.parametrize("status, remaining", [
    (429, 10),   # too many requests
    (500, 10),   # GitHub-side 5xx
    (502, 10),
    (503, 10),
    (504, 10),
    (403, 0),    # the primary rate limit: a 403 with no quota left
])
def test_gh_read_command_retries_transient_statuses_then_succeeds(
    monkeypatch, caplog, status, remaining,
):
    calls = []
    sleeps = []
    output = failed_response(status, remaining=remaining)

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                1, command, output=output, stderr=f"gh: failed (HTTP {status})",
            )
        return "[]"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with caplog.at_level("INFO"):
        assert runner.run_gh_read_command(
            ["gh", "api", "repos/o/r/issues?state=open"],
            command_runner=fake_run,
        ) == "[]"

    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert "gh_read_retry" in caplog.text
    assert "attempt=2" in caplog.text


def test_gh_read_command_does_not_retry_a_rejected_credential(monkeypatch):
    """A 401 is a credential problem, not a transient blip: retrying it
    cannot succeed (Issue #1760)."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=failed_response(401),
            stderr="gh: Bad credentials (HTTP 401)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(github.gh_response.GitHubRequestFailed) as caught:
        runner.run_gh_read_command(
            ["gh", "api", "repos/o/r"], command_runner=fake_run,
        )
    assert len(calls) == 1
    assert caught.value.status == 401
    assert caught.value.authentication_required is True


def test_gh_read_command_retries_a_status_less_server_failure(
    monkeypatch, caplog,
):
    """A gh subcommand other than `gh api` prints no status line (`gh
    issue list`), so GitHub's own server sentence is the only signal
    there is — the same fallback the write path keeps. Dropping it would
    turn a GitHub-side 5xx on a plain scan read back into the crashed
    tick the bounded retry exists to prevent (Issues #738 / #1760)."""
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                1, command, stderr="gh: Bad Gateway (HTTP 502)",
            )
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


def test_gh_read_command_does_not_retry_a_status_less_four_x(monkeypatch):
    """A rejected credential prints no server-failure sentence, so a
    status-less gh subcommand still fails on its first attempt
    (Issue #1760)."""
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="gh: Bad credentials (HTTP 401)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_gh_read_command(
            ["gh", "issue", "list", "--repo", "o/r",
             "--json", "number", "--limit", "1"],
            command_runner=fake_run,
        )


def test_gh_write_command_retries_server_error_then_succeeds(monkeypatch):
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1, command, stderr="HTTP 503: Service Unavailable"
            )
        return "ok"

    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    assert github.run_gh_write_command(
        ["gh", "issue", "comment", "9", "--body", "x"],
        command_runner=fake_run,
    ) == "ok"
    assert len(calls) == 2
    assert sleeps == [1]


def test_gh_write_command_reads_the_status_before_the_text(monkeypatch):
    """A response status decides the retry; the text never overrides it —
    a permission 403 whose message mentions a rate limit is not retried."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=failed_response(
                403, body='{"message": "API rate limit exceeded"}',
            ),
            stderr="gh: Resource not accessible (HTTP 403)",
        )

    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(github.gh_response.GitHubRequestFailed):
        github.run_gh_write_command(
            ["gh", "api", "repos/o/r/issues/9/comments",
             "--method", "POST", "-f", "body=x"],
            command_runner=fake_run,
        )
    assert len(calls) == 1


def test_gh_write_command_does_not_retry_permission_error(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 403: Resource not accessible"
        )

    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_write_command(["gh", "issue", "edit", "9"], command_runner=fake_run)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 404])
def test_gh_write_command_does_not_retry_four_x(monkeypatch, status):
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output=failed_response(status),
            stderr=f"gh: failed (HTTP {status})",
        )

    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_write_command(
            ["gh", "api", "repos/o/r/issues/9/comments",
             "--method", "POST", "-f", "body=x"],
            command_runner=fake_run,
        )


def test_gh_write_command_handles_zero_attempt_budget(monkeypatch):
    monkeypatch.setattr(github, "GH_WRITE_MAX_ATTEMPTS", 0)
    assert github.run_gh_write_command(["gh", "issue", "edit", "9"], command_runner=lambda *a, **k: pytest.fail("executed")) is None


def test_gh_write_command_forwards_cwd_and_timeout(monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        raise subprocess.CalledProcessError(1, command, stderr="HTTP 503")

    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_write_command(
            ["gh", "issue", "edit", "9"], cwd="/tmp", timeout=7,
            command_runner=fake_run,
        )
    assert seen[0][1] == {"cwd": "/tmp", "timeout": 7}


def test_gh_write_command_retries_when_idempotency_check_is_false(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="HTTP 503")

    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_write_command(
            ["gh", "issue", "comment", "9"],
            command_runner=fake_run, already_applied=lambda: False,
        )
    assert len(calls) == github.GH_WRITE_MAX_ATTEMPTS


def test_gh_write_command_retries_when_idempotency_check_fails(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="HTTP 503")

    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError):
        github.run_gh_write_command(
            ["gh", "issue", "comment", "9"], command_runner=fake_run,
            already_applied=lambda: (_ for _ in ()).throw(RuntimeError("read failed")),
        )
    assert len(calls) == github.GH_WRITE_MAX_ATTEMPTS


def test_comment_issue_stops_after_remote_comment_is_found(monkeypatch):
    calls = []
    attempts = {"n": 0}

    def fake_run(command, **kwargs):
        calls.append(command)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise subprocess.CalledProcessError(
                1, command, stderr="GraphQL: Something went wrong while executing your query",
            )
        return '{"comments": [{"body": "hello\\n\\n<!-- runner=x -->"}]}'

    monkeypatch.setattr(github, "format_status_comment", lambda body: body + "\n\n<!-- runner=x -->")
    monkeypatch.setattr(github, "run_command", fake_run)
    github.comment_issue(9, repo="o/r", body="hello")
    assert len(calls) == 2


def test_gh_write_command_idempotency_check_prevents_duplicate(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="GraphQL: Something went wrong while executing your query"
        )

    monkeypatch.setattr(github.time, "sleep", lambda _: pytest.fail("slept"))
    assert github.run_gh_write_command(
        ["gh", "issue", "comment", "9", "--body", "x"],
        command_runner=fake_run, already_applied=lambda: True,
    ) == ""
    assert len(calls) == 1


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


def test_gh_read_command_returns_an_empty_body_not_its_headers(monkeypatch):
    """A read whose response body is empty returns "" — never the status
    line and headers gh printed for --include (Issue #1760).

    The real scene: the milestone read's jq filter selects nothing, so gh
    prints the headers only; run_command strips the trailing blank line,
    and the adapter must still recognize the header block it asked for.
    """
    def fake_run(command, **kwargs):
        # Exactly what run_command returns for an empty body: the status
        # line and the headers, stripped, without the trailing blank line.
        return (
            "HTTP/2.0 200 OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "X-Ratelimit-Remaining: 3796\r\n"
            "\r\n"
        ).strip()

    assert runner.run_gh_read_command(
        ["gh", "api", "repos/o/r/milestones",
         "--jq", '.[] | select(.title=="v0.7.5") | .open_issues'],
        command_runner=fake_run,
    ) == ""


def test_gh_read_command_exhausts_and_reraises_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    output = failed_response(502)
    stderr = "gh: Bad Gateway (HTTP 502)"

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=output, stderr=stderr,
        )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.run_gh_read_command(
            ["gh", "api", "repos/o/r/pulls?state=open"],
            command_runner=fake_run,
        )

    assert len(calls) == 3
    assert caught.value.stderr == stderr
    assert caught.value.status == 502


def test_gh_read_command_does_not_retry_deterministic_failure(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=failed_response(404),
            stderr="gh: Not Found (HTTP 404)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(subprocess.CalledProcessError):
        runner.run_gh_read_command(
            ["gh", "api", "repos/o/r/issues/9"],
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
            1, command, output=failed_response(502),
            stderr="gh: Bad Gateway (HTTP 502)",
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
    """`gh api` is a read only without a write marker — a non-GET
    --method/-X override, or any `-f`/`-F` parameter, which per gh's
    own semantics switches the request to POST. The flags are gh's
    read/write semantics, not a call-site list."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=failed_response(429),
            stderr="gh: Too Many Requests (HTTP 429)",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))
    for command in (
        ["gh", "api", "repos/o/r/issues/9/comments",
         "-f", "body=hi", "-X", "POST"],
        ["gh", "api", "repos/o/r/milestones/3",
         "--method", "PATCH", "-f", "state=closed"],
        ["gh", "api", "repos/o/r/labels", "--method=POST", "-f", "name=p0"],
        ["gh", "api", "-X", "DELETE", "repos/o/r/actions/variables/V"],
        ["gh", "api", "repos/o/r/labels", "-f", "name=p0"],
    ):
        with pytest.raises(subprocess.CalledProcessError):
            runner.run_gh_read_command(command, command_runner=fake_run)
    assert len(calls) == 5


def test_is_readonly_gh_command_classifies_gh_api_reads():
    assert github._is_readonly_gh_command(["gh", "api", "repos/o/r"])
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--paginate", "--slurp"])
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method", "GET"])
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--method=GET", "--jq", ".name"])
    assert github._is_readonly_gh_command(["gh", "api", "-X", "GET", "repos/o/r"])
    # The adapter's own --include flag never turns a read into a write.
    assert github._is_readonly_gh_command(
        ["gh", "api", "repos/o/r", "--include"])
    # A trailing -X with no value is malformed; no method override was
    # parsed, so the command is still classified as a read (gh rejects it).
    assert github._is_readonly_gh_command(["gh", "api", "repos/o/r", "-X"])
    assert not github._is_readonly_gh_command(["gh", "api"])
    assert not github._is_readonly_gh_command(["git", "push", "origin", "main"])
    assert not github._is_readonly_gh_command(
        ["timeout", "30", "gh", "issue", "list"])


def test_is_readonly_gh_command_field_parameters_imply_post():
    """gh's documented semantics (gh api --help): the default method is
    GET normally and POST if any parameters were added — a `-f`/`-F`
    parameter without an explicit `--method GET` is a write and must
    never classify as a read."""
    for command in (
        ["gh", "api", "repos/o/r/labels", "-f", "name=p0"],
        ["gh", "api", "repos/o/r/issues/9/comments", "-F", "body=hi"],
        ["gh", "api", "repos/o/r/labels", "--raw-field", "name=p0"],
        ["gh", "api", "repos/o/r/labels", "--field=name=p0"],
        ["gh", "api", "graphql", "-fquery=query { viewer { login } }"],
    ):
        assert not github._is_readonly_gh_command(command), command
    # An explicit `--method GET` keeps the parameters on the query
    # string (gh's documented escape hatch) — a read again.
    for command in (
        ["gh", "api", "repos/o/r/issues", "--method", "GET",
         "-f", "state=open"],
        ["gh", "api", "repos/o/r/issues", "-X", "GET", "-f", "state=open"],
        ["gh", "api", "repos/o/r/issues", "--method=GET", "-fstate=open"],
    ):
        assert github._is_readonly_gh_command(command), command


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
        assert github._is_readonly_gh_command(command), command
    for command in (
        ["gh", "issue", "close", "9"],
        ["gh", "issue"],
    ):
        assert not github._is_readonly_gh_command(command), command


def test_list_issues_does_not_retry_a_rejected_credential(monkeypatch):
    """Issue #1760: the tick's `list_issues` read of a rejected credential
    is a repair, not a retry.

    `list_issues` runs `gh issue list`, which prints no response headers
    (`gh --include` is a `gh api` flag), so the real failure is exit 1
    with `HTTP 401: Bad credentials` on stderr — modelled here as gh
    prints it. The read fails on its first attempt; the classification
    then reads the remaining text marker, which is all that shape carries.
    """
    from orbi import failure

    calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda _: pytest.fail("slept"))

    def fake_run(command, **kwargs):
        calls.append(list(command))
        raise subprocess.CalledProcessError(
            1, command,
            stderr="HTTP 401: Bad credentials "
                   "(https://api.github.com/graphql)",
        )

    monkeypatch.setattr(seam, "run_command", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.list_issues(
            "orbi-build/orbi-cloud", state="open",
            search="label:ai-fix-needed,ai-pr-opened -label:ai-blocked",
            json_fields="number,title,state,url,labels", limit=1,
        )
    assert len(calls) == 1
    assert gh_response_status(caught.value) is None
    assert failure.classify(
        runner._failure_detail(caught.value), error=caught.value,
    ).reason_code == "credential_missing"


def gh_response_status(exc):
    """The response status the adapter attached, or None."""
    return getattr(exc, "status", None)
