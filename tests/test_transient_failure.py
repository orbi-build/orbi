"""Issue #2443: ONE definition of a transient GitHub or network failure.

The question "is this a transient GitHub or network error?" was answered in
three places with three lists that disagreed: the `gh` retry
(`gh_response.transient_command`), the network-`git` retry (`journal`) and
the failure classification (`failure.classify`). A short DNS failure
(`error connecting to api.github.com`, #2255) and a connection timeout
(`dial tcp <ip>:443: i/o timeout`, #2427) were therefore retried nowhere and
recorded `unclassified / fix_ticket` — the Issue went `ai-blocked` where a
retry (and then the #2202 re-queue) was the right answer.

These are behaviour tests: the fake command runner only, no network, no
sleeps.
"""
import subprocess

import pytest

import orbi.runner as runner
from orbi import failure, gh_response, github, journal, transient_failure

# The whole definition, tagged so the parametrized agreement test covers
# every wording — including one added later, without editing this test.
DEFINITION = (
    *transient_failure.TRANSIENT_FAILURE_WORDINGS,
    *transient_failure.TRANSIENT_TIMEOUT_WORDINGS,
)

# The real transport failures GitHub reported (Issues #2255, #2427): the
# definition must recognise each of them.
REPORTED_WORDINGS = (
    "error connecting to api.github.com",
    "dial tcp 140.82.121.6:443: i/o timeout",
    "read tcp 10.0.0.4:51234->140.82.112.5:443: connection reset by peer",
    "dial tcp 140.82.121.6:443: connect: connection refused",
    "temporary failure in name resolution",
    "network is unreachable",
    "net/http: TLS handshake timeout",
    "HTTP 502: Bad Gateway",
    "HTTP 429: Too Many Requests",
    "GraphQL: Something went wrong while executing your query",
)


def failed_gh(detail: str, *, command=None) -> gh_response.GitHubRequestFailed:
    """The failure `run_gh_read_command` raises for a transport failure.

    A status-less gh failure carries no response: only the command's output
    is left, which is exactly the shape the retry decides on.
    """
    return gh_response.failed_request(subprocess.CalledProcessError(
        1, command or ["gh", "issue", "view", "1", "--repo", "o/r"],
        stderr=detail,
    ))


@pytest.mark.parametrize("detail", REPORTED_WORDINGS)
def test_the_definition_covers_every_reported_wording(detail):
    """What must be true after #2443: the definition includes
    `error connecting to <host>` and the connection and timeout wordings."""
    assert transient_failure.is_transient(detail, timeout_source=True) is True


@pytest.mark.parametrize("detail", [
    # Acceptance 1: the #2255 DNS failure.
    "error connecting to api.github.com\ncheck your internet connection",
    # Acceptance 2: the #2427 connection timeout and a reset.
    "dial tcp 140.82.121.6:443: i/o timeout",
    "read tcp 10.0.0.4:51234->140.82.112.5:443: connection reset by peer",
])
def test_a_gh_read_that_fails_once_transiently_returns_the_success(
    monkeypatch, caplog, detail,
):
    """Acceptance 1 + 2: a `gh` read that fails once and then succeeds
    hands the caller the successful result — the retry ran, no failure
    record was written."""
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command, stderr=detail)
        return '{"number": 1}'

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with caplog.at_level("WARNING", logger="orbi.bootstrap"):
        assert runner.run_gh_read_command(
            ["gh", "issue", "view", "1", "--repo", "o/r",
             "--json", "number"],
            command_runner=fake_run,
        ) == '{"number": 1}'

    assert len(calls) == 2
    assert sleeps == [1]
    assert caplog.text.count("gh_read_retry") == 1
    # No failure record: the wrapper never reported a failed command.
    assert "command_failed" not in caplog.text


def test_a_gh_read_that_keeps_failing_transiently_is_recorded_github_transient(
    monkeypatch,
):
    """Acceptance 3 (the layer below the report): the retries run out and
    the failure the caller receives is classified `github_transient` /
    `requeue` — the record the #2202 re-queue acts on."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr="error connecting to api.github.com",
        )

    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        runner.run_gh_read_command(
            ["gh", "issue", "list", "--repo", "o/r"], command_runner=fake_run,
        )

    assert len(calls) == github.GH_READ_MAX_ATTEMPTS
    record = failure.classify(
        runner.failure_detail(caught.value), error=caught.value,
    )
    assert record.reason_code == "github_transient"
    assert record.action_code == "requeue"
    assert record.retry_safe is True


@pytest.mark.parametrize("sample", [sample for sample, _ in DEFINITION])
def test_the_gh_retry_and_the_classification_agree_on_every_wording(sample):
    """Acceptance 4: for each wording in the one definition the `gh` retry
    decision, the network-`git` retry decision and the failure
    classification give the same answer. Iterating the definition itself
    covers a wording added later without editing this test."""
    request = failed_gh(sample)
    assert gh_response.transient_command(request) is True

    record = failure.classify(runner.failure_detail(request), error=request)
    assert record.reason_code == "github_transient"
    assert record.action_code == "requeue"

    git_command = ["git", "fetch", "origin", "main"]
    git_error = subprocess.CalledProcessError(
        128, git_command, stderr=f"fatal: unable to access: {sample}",
    )
    assert journal._is_retryable_git_network_failure(
        git_command, git_error,
    ) is True


@pytest.mark.parametrize("status, detail, reason_code", [
    # Acceptance 5: a response status always wins; these keep the reason
    # code they had before #2443 (Issue #1760).
    (403, "gh: Resource not accessible (HTTP 403)", "credential_missing"),
    (404, "gh: Not Found (HTTP 404)", "unclassified"),
    (401, "gh: Bad credentials (HTTP 401)", "credential_missing"),
])
def test_a_four_x_response_is_never_transient(status, detail, reason_code):
    output = (
        f"HTTP/2.0 {status} Error\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "X-Ratelimit-Remaining: 4999\r\n"
        "\r\n"
        '{"message": "failed"}'
    )
    request = gh_response.failed_request(subprocess.CalledProcessError(
        1, ["gh", "api", "repos/o/r/issues/9"], output=output, stderr=detail,
    ))
    assert request.status == status
    assert gh_response.transient_command(request) is False
    assert failure.classify(
        runner.failure_detail(request), error=request,
    ).reason_code == reason_code


def test_a_rejected_credential_is_never_transient():
    """gh's documented auth exit code carries no status; it is a repair, not
    a retry (Issue #1760)."""
    request = gh_response.failed_request(subprocess.CalledProcessError(
        4, ["gh", "issue", "list", "--repo", "o/r"], stderr="no token",
    ))
    assert request.authentication_required is True
    assert gh_response.transient_command(request) is False
    assert failure.classify(
        runner.failure_detail(request), error=request,
    ).reason_code == "credential_missing"


def test_a_timeout_word_without_a_command_source_is_not_transient():
    """A detail that merely SAYS "timed out" proves no transport timeout:
    the command (or its timeout type) is the source that makes the wording
    readable (Issues #1760/#1768, kept by the shared definition)."""
    assert transient_failure.is_transient(
        "the release gate note says timed out", timeout_source=False,
    ) is False
    assert failure.classify(
        "the release gate note says timed out",
    ).reason_code != "github_transient"
