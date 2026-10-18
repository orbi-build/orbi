"""The response a failed `gh` request received, read from gh's own output.

A GitHub failure is classified from the HTTP status the response carries,
never from the error text: the detail of a `CalledProcessError` also holds
the command's argv, so a branch, label or check named `credential` or
`forbidden` could be read as a credential problem (Issue #1760).

`gh api --include` (documented: "Include HTTP response status line and
headers in the output") prints the status line and the response headers in
front of the body — for a failed request too, verified against gh 2.100.0:
a 403 prints `HTTP/2.0 403 Forbidden`, its headers and the error body on
stdout, writes one line to stderr and exits 1. `X-Ratelimit-Remaining` is
one of those headers, and a 403 with `0` remaining is GitHub's rate limit,
while a 403 with quota left is a permission failure. The status is
therefore read from the response itself.

The other structural source is gh's documented exit code: `gh help
exit-codes` says "If a command requires authentication, the exit code will
be 4", which covers the gh subcommands that print no response headers.

JSON output escapes newlines inside string values, so a line that starts
with a status line can only be gh's own header block — a body that merely
QUOTES an HTTP example keeps it inside one JSON string.
"""
from __future__ import annotations

import re
import subprocess

# gh's documented exit code for "requires authentication".
GITHUB_AUTH_EXIT_CODE = 4
INCLUDE_FLAG = "--include"

# gh subcommands other than `gh api` print no response status line, so
# their server-side failures keep GitHub's own sentences. Read only when
# the request carried NO status at all: a response status always wins.
_GH_SERVER_FAILURE_RE = re.compile(
    r"http 5\d\d|http 429|rate limit|"
    r"something went wrong while executing your query|internal server error",
    re.IGNORECASE,
)
_STATUS_LINE_RE = re.compile(r"HTTP/\d+(?:\.\d+)? (\d{3})\b")
# RFC 9110 header field name followed by ":". Deliberately strict: a block
# only counts when every line up to the blank line is a header, so body
# text cannot be mistaken for response metadata.
_HEADER_LINE_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+:")
_RATELIMIT_RE = re.compile(r"^x-ratelimit-remaining:\s*(\d+)\s*$", re.IGNORECASE)


class GitHubRequestFailed(subprocess.CalledProcessError):
    """A failed gh request carrying the HTTP response it received.

    `status` is the status the response itself reported, or None when the
    request produced no response (a connection failure). A
    `CalledProcessError` subclass, so every existing
    `except subprocess.CalledProcessError` handler keeps its behaviour.
    """

    def __init__(self, returncode: int, cmd: list[str],
                 output: str | None = None, stderr: str | None = None, *,
                 status: int | None = None,
                 ratelimit_remaining: int | None = None) -> None:
        super().__init__(returncode, cmd, output=output, stderr=stderr)
        self.status = status
        self.ratelimit_remaining = ratelimit_remaining

    @property
    def rate_limited(self) -> bool:
        """GitHub's rate limit is a 403 with no quota left."""
        return self.status == 403 and self.ratelimit_remaining == 0

    @property
    def authentication_required(self) -> bool:
        """A rejected credential: an HTTP 401, or gh's auth exit code."""
        return self.status == 401 or self.returncode == GITHUB_AUTH_EXIT_CODE


def include_headers(command: list[str]) -> list[str]:
    """Return `command` asking `gh api` to print the response status line.

    Only `gh api` prints response headers; every other gh subcommand keeps
    its argv (its authentication failure is gh's own exit code).
    """
    if command[:2] != ["gh", "api"]:
        return command
    if INCLUDE_FLAG in command or "-i" in command:
        return command
    return [*command, INCLUDE_FLAG]


def _header_block_end(lines: list[str], start: int) -> int | None:
    """Index of the first line after a header block, or None if not one."""
    index = start + 1
    if index >= len(lines) or not (
        _HEADER_LINE_RE.match(lines[index]) or lines[index][:1] in " \t"
    ):
        return None
    while index < len(lines):
        line = lines[index]
        if not line:
            return index + 1
        if not (_HEADER_LINE_RE.match(line) or line[:1] in " \t"):
            return None
        index += 1
    return None


def response_parts(output: str | None) -> tuple[int | None, int | None, str]:
    """Split gh's output into (status, x-ratelimit-remaining, body).

    Every header block is removed from the body, including the ones gh
    prints between the pages of a `--paginate` read (the JSON separator
    shares the line with the next page's status line). The LAST block wins:
    a failed paginated request ends on the response that failed.
    """
    lines = str(output or "").splitlines()
    statuses: list[int] = []
    remaining: int | None = None
    body: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        prefix = ""
        candidate = line
        if candidate[:1] in "[,":
            prefix, candidate = candidate[:1], candidate[1:]
        match = _STATUS_LINE_RE.match(candidate)
        end = _header_block_end(lines, index) if match else None
        if match is not None and end is not None:
            statuses.append(int(match.group(1)))
            for header in lines[index + 1:end]:
                header_match = _RATELIMIT_RE.match(header)
                if header_match:
                    remaining = int(header_match.group(1))
            body.append(prefix)
            index = end
            continue
        body.append(line)
        index += 1
    return (
        statuses[-1] if statuses else None,
        remaining,
        "\n".join(body).strip(),
    )


def failed_request(
    exc: subprocess.CalledProcessError,
) -> GitHubRequestFailed:
    """Return `exc` carrying the status of the response it received.

    An already structured error is returned unchanged, so the wrapper can
    re-raise without losing the status.
    """
    if isinstance(exc, GitHubRequestFailed):
        return exc
    status, remaining, _ = response_parts(exc.stdout)
    return GitHubRequestFailed(
        exc.returncode, exc.cmd,
        output=exc.output, stderr=exc.stderr,
        # A 2xx status next to a non-zero exit is an incomplete read, not a
        # failed request: it carries no failure status.
        status=status if status is not None and status >= 400 else None,
        ratelimit_remaining=remaining,
    )


def transient_write(failure: GitHubRequestFailed) -> bool:
    """Whether a failed gh write is worth retrying.

    The response status decides whenever gh printed one — a permission
    failure is never retried. A `gh` subcommand that prints no response
    headers keeps GitHub's own server sentences: there is no status to
    read, and the caller's idempotency check guards the retry.
    """
    if failure.status is not None or failure.authentication_required:
        return transient(failure)
    detail = " ".join(
        str(part or "") for part in (failure.stderr, failure.stdout))
    return _GH_SERVER_FAILURE_RE.search(detail) is not None


def status_of(exc: BaseException | None) -> int | None:
    """The HTTP status a failed gh request carried, or None."""
    return exc.status if isinstance(exc, GitHubRequestFailed) else None


def authentication_required(exc: BaseException) -> bool:
    """Whether the failed request was refused for its credential."""
    return isinstance(exc, GitHubRequestFailed) and exc.authentication_required


def transient(exc: BaseException) -> bool:
    """Whether the failed request is worth retrying.

    Transient: a rate limit (403 with no quota left), 429 and every 5xx.
    A rejected credential (401 / gh's auth exit code) and every other
    client failure are not — retrying them cannot succeed.
    """
    if not isinstance(exc, GitHubRequestFailed):
        return False
    if exc.rate_limited:
        return True
    status = exc.status
    return status is not None and (status == 429 or 500 <= status <= 599)
