"""The HTTP status of a failed gh request (Issue #1760).

The fixtures below are the real shape gh 2.100.0 prints for
`gh api --include`: the status line, the response headers (CRLF) and the
body on stdout, with gh's own one-line error on stderr and exit code 1 —
captured in this workspace against a 404 and, with a rejected token, a
401. They are trimmed to the headers the adapter reads; the status line,
the `X-Ratelimit-Remaining` header and the body are byte-real.
"""
import json
import subprocess

import pytest

from orbi import gh_response

# `gh api --include repos/<owner>/<repo>` with a token GitHub rejected.
UNAUTHORIZED_RESPONSE = (
    "HTTP/2.0 401 Unauthorized\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 4999\r\n"
    "\r\n"
    '{\n  "message": "Bad credentials",\n'
    '  "documentation_url": "https://docs.github.com/rest",\n'
    '  "status": "401"\n}'
)

# `gh api --include repos/<owner>/<repo>/nonexistent`.
NOT_FOUND_RESPONSE = (
    "HTTP/2.0 404 Not Found\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 4966\r\n"
    "\r\n"
    '{"message":"Not Found","documentation_url":"https://docs.github.com/rest",'
    '"status":"404"}'
)

# GitHub's primary rate limit: a 403 with no quota left.
RATE_LIMITED_RESPONSE = (
    "HTTP/2.0 403 Forbidden\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 0\r\n"
    "\r\n"
    '{"message":"API rate limit exceeded for installation","status":"403"}'
)

# A permission 403 still has quota left.
PERMISSION_RESPONSE = (
    "HTTP/2.0 403 Forbidden\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 4980\r\n"
    "\r\n"
    '{"message":"Resource not accessible by integration","status":"403"}'
)

# `gh api --paginate --slurp --include`: the JSON separator shares the line
# with the next page's status line ("[" before the first block, "," before
# every later one).
PAGINATED_SLURP_RESPONSE = (
    "[HTTP/2.0 200 OK\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 4747\r\n"
    "\r\n"
    '[{"number": 1}]\n'
    ",HTTP/2.0 200 OK\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "X-Ratelimit-Remaining: 4746\r\n"
    "\r\n"
    '[{"number": 2}]\n'
    "]"
)


def failed(stderr="gh: ...", output=None, returncode=1,
           command=("gh", "api", "repos/o/r", "--include")):
    return gh_response.failed_request(subprocess.CalledProcessError(
        returncode, list(command), output=output, stderr=stderr,
    ))


def test_include_headers_only_touches_gh_api_commands():
    assert gh_response.include_headers(["gh", "api", "repos/o/r"]) == [
        "gh", "api", "repos/o/r", "--include",
    ]
    # Already present: the flag is never doubled.
    assert gh_response.include_headers(
        ["gh", "api", "-i", "repos/o/r"]) == ["gh", "api", "-i", "repos/o/r"]
    assert gh_response.include_headers(["gh", "api", "repos/o/r", "--include"]) == [
        "gh", "api", "repos/o/r", "--include",
    ]
    # Only `gh api` prints a response status line; every other subcommand
    # keeps its argv (its auth failure is gh's exit code 4).
    for command in (
        ["gh", "issue", "list", "--repo", "o/r"],
        ["gh", "pr", "view", "1", "--json", "number"],
        ["gh", "auth", "status", "--hostname", "github.com"],
    ):
        assert gh_response.include_headers(command) == command


@pytest.mark.parametrize("response, status, remaining", [
    (UNAUTHORIZED_RESPONSE, 401, 4999),
    (NOT_FOUND_RESPONSE, 404, 4966),
    (RATE_LIMITED_RESPONSE, 403, 0),
    (PERMISSION_RESPONSE, 403, 4980),
])
def test_response_parts_reads_the_status_and_quota_header(
    response, status, remaining,
):
    got_status, got_remaining, body = gh_response.response_parts(response)
    assert got_status == status
    assert got_remaining == remaining
    assert "HTTP/" not in body and "Ratelimit" not in body
    assert json.loads(body)["status"] == str(status)


def test_response_parts_keeps_every_page_of_a_paginated_read():
    status, remaining, body = gh_response.response_parts(
        PAGINATED_SLURP_RESPONSE,
    )
    assert status == 200
    assert remaining == 4746
    assert json.loads(body) == [[{"number": 1}], [{"number": 2}]]


def test_response_parts_without_a_header_block_is_the_body():
    # A gh command run without --include (or a fake that answers plain
    # JSON) has no status line: the whole output stays the body.
    assert gh_response.response_parts('[{"number": 1}]') == (
        None, None, '[{"number": 1}]',
    )
    assert gh_response.response_parts("") == (None, None, "")


@pytest.mark.parametrize("output", [
    # A status-looking line with nothing after it.
    "HTTP/2.0 500 Error",
    # A status-looking line followed by a line that is not a header.
    "HTTP/2.0 403 Forbidden\nnot a header\n\nbody",
    "HTTP/2.0 403 Forbidden\nContent-Type: application/json\nnot a header\n\nbody",
    # A header block that never reaches its blank line.
    "HTTP/2.0 500 Error\nContent-Type: application/json",
])
def test_response_parts_needs_a_complete_header_block(output):
    """Only a complete block (status line, header lines, blank line) is
    response metadata; anything else stays part of the body."""
    assert gh_response.response_parts(output) == (None, None, output.strip())


def test_failed_request_carries_the_response_status():
    exc = failed(output=UNAUTHORIZED_RESPONSE, stderr="gh: Bad credentials (HTTP 401)")
    request = gh_response.failed_request(exc)
    assert isinstance(request, subprocess.CalledProcessError)
    assert isinstance(request, gh_response.GitHubRequestFailed)
    assert request.status == 401
    assert request.ratelimit_remaining == 4999
    assert request.returncode == 1
    assert request.cmd == exc.cmd
    assert request.stdout == UNAUTHORIZED_RESPONSE
    assert request.stderr == "gh: Bad credentials (HTTP 401)"
    # Already structured: returned unchanged.
    assert gh_response.failed_request(request) is request


def test_failed_request_without_a_response_has_no_status():
    request = failed(output="", stderr="error connecting to api.github.com")
    assert request.status is None
    assert request.ratelimit_remaining is None


def test_gh_auth_exit_code_means_authentication_is_required():
    """gh documents exit code 4 for "requires authentication"
    (`gh help exit-codes`); it carries no HTTP status, so the auth
    meaning travels on the exit code."""
    assert gh_response.GITHUB_AUTH_EXIT_CODE == 4
    request = failed(returncode=4, output="", stderr="no token")
    assert request.status is None
    assert request.authentication_required is True


@pytest.mark.parametrize("response, expected", [
    (UNAUTHORIZED_RESPONSE, False),
    (NOT_FOUND_RESPONSE, False),
    (PERMISSION_RESPONSE, False),
    (RATE_LIMITED_RESPONSE, True),
])
def test_transient_reads_the_status_not_the_text(response, expected):
    # The text says nothing at all about the status: the decision is the
    # response's.
    request = failed(output=response)
    assert gh_response.transient(request) is expected


@pytest.mark.parametrize("status, expected", [
    (429, True),
    (500, True),
    (502, True),
    (503, True),
    (401, False),
    (403, False),
    (404, False),
])
def test_transient_statuses(status, expected):
    request = gh_response.GitHubRequestFailed(
        1, ["gh", "api", "repos/o/r"], status=status,
        ratelimit_remaining=10,
    )
    assert gh_response.transient(request) is expected


def test_transient_requires_the_structured_error():
    # A plain CalledProcessError carries no response status, so it is
    # never retried on a guess about its text.
    assert gh_response.transient(subprocess.CalledProcessError(
        1, ["gh", "api", "repos/o/r"], stderr="HTTP 502: Bad Gateway",
    )) is False


def test_rate_limited_needs_both_the_403_and_the_empty_quota():
    assert gh_response.GitHubRequestFailed(
        1, ["gh", "api"], status=403, ratelimit_remaining=0,
    ).rate_limited is True
    assert gh_response.GitHubRequestFailed(
        1, ["gh", "api"], status=403, ratelimit_remaining=7,
    ).rate_limited is False
    assert gh_response.GitHubRequestFailed(
        1, ["gh", "api"], status=403, ratelimit_remaining=None,
    ).rate_limited is False
