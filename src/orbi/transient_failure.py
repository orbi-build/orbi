"""The ONE definition of "a transient GitHub or network failure".

Three decisions used to answer this question with three lists that
disagreed (Issue #2443): the `gh` retry
(`gh_response.transient_command`), the network-`git` retry
(`journal._is_retryable_git_network_failure`) and the failure
classification (`failure.classify`). The GitHub server sentences were
shared, the transport failures were not, so `error connecting to
api.github.com` (#2255) and `dial tcp <ip>:443: i/o timeout` (#2427) were
retried nowhere and recorded `unclassified / fix_ticket` — an `ai-blocked`
Issue where a retry, and then the existing #2202 re-queue, was the answer.

The definition is data, so every consumer reads the same set and a test
iterates over the set itself:

- `TRANSIENT_FAILURE_WORDINGS`: GitHub's own server sentences and the
  network transport failures — transient wherever they appear.
- `TRANSIENT_TIMEOUT_WORDINGS`: the timeout wordings. A timeout is
  transient only when a failing command is the source of the detail
  (`is_transient(..., timeout_source=True)`): a sentence that merely
  mentions the word proves no transport timeout (#1760, #1768).

An HTTP response status always wins over this text (`gh_response`): a 403,
a 404 and a rejected credential are never transient.

Each entry is `(sample, pattern)`: the sample is a real wording, the
pattern the expression that recognises it, case-insensitively.
"""
from __future__ import annotations

import re

TRANSIENT_FAILURE_WORDINGS: tuple[tuple[str, str], ...] = (
    # GitHub's own server sentences: a `gh` subcommand other than
    # `gh api` prints no response status line, so its failure keeps them.
    ("HTTP 500 Internal Server Error", r"http 5\d\d"),
    ("HTTP 429: Too Many Requests", r"http 429"),
    ("API rate limit exceeded", r"rate limit"),
    ("GraphQL: Something went wrong while executing your query",
     r"something went wrong while executing your query"),
    ("Internal Server Error", r"internal server error"),
    # The request never reached a response: DNS, transport and routing.
    ("error connecting to api.github.com", r"error connecting to"),
    ("read tcp 10.0.0.4:51234->140.82.112.5:443: connection reset by peer",
     r"connection reset"),
    ("dial tcp 140.82.121.6:443: connect: connection refused",
     r"connection refused"),
    ("temporary failure in name resolution",
     r"temporary failure in name resolution"),
    ("network is unreachable", r"network (?:is )?unreachable"),
)

TRANSIENT_TIMEOUT_WORDINGS: tuple[tuple[str, str], ...] = (
    ("dial tcp 140.82.121.6:443: i/o timeout", r"timed out|timeout"),
)


def _compile(wordings: tuple[tuple[str, str], ...]) -> re.Pattern[str]:
    """Compile the patterns of one part of the definition into one regex."""
    return re.compile(
        "|".join(f"(?:{pattern})" for _sample, pattern in wordings),
        re.IGNORECASE,
    )


_TRANSIENT_FAILURE_RE = _compile(TRANSIENT_FAILURE_WORDINGS)
_TRANSIENT_TIMEOUT_RE = _compile(TRANSIENT_TIMEOUT_WORDINGS)


def is_transient(detail: str, *, timeout_source: bool) -> bool:
    """Whether `detail` carries the definition's transient wording.

    `timeout_source` is whether the failing command itself produced the
    detail (`failure._timeout_is_text_only`): only then does a timeout
    wording mean a transport timeout, the rule #1760/#1768 established for
    a detail with no status and no type.
    """
    if _TRANSIENT_FAILURE_RE.search(detail):
        return True
    return timeout_source and _TRANSIENT_TIMEOUT_RE.search(detail) is not None
