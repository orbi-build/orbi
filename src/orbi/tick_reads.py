"""The identical GitHub reads one tick makes once (Issue #2218).

One idle tick used to buy the same answer repeatedly: every claim loop
resolved one repository's `.github/orbi.toml` for itself — eight reads of
one file in a measured tick, 17% of that deployment's hourly `gh` calls —
and the scans over the terminal `ai-blocked` state (the missing
check-read permission scan, the base-branch-red scan and the
stopped-ticket answer scan) each listed the label and read every blocked
Issue's comments. On one token running several deployments that is the
hourly API budget spent on an answer the tick already had, and the tick
then fails with `API rate limit already exceeded`.

`begin` is the tick boundary: inside it `once` answers a repeated read
from the first one, and `orbi.blocked_scan.blocked_reads` is the ONE read
of a repository's `ai-blocked` state every scan shares. Outside a tick
every call reads for itself, so a caller that legitimately changes what
it reads between two reads (the milestone command writing the policy
file, a test modelling two deployments) is never served a stale answer.

The trust read — the collaborator permission behind a comment — is NOT
here: it stays one read per comments read, exactly as it was. A cached
permission would be a security question, not a quota one.
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

# The tick's reads, or `None` outside a tick (every caller reads).
_SCOPE: dict[tuple, object] | None = None


def begin() -> None:
    """Start a tick: the reads below answer once for the whole tick."""
    global _SCOPE
    _SCOPE = {}


def end() -> None:
    """Leave the tick: the next caller reads for itself."""
    global _SCOPE
    _SCOPE = None


def once(key: tuple, read: Callable[[], T]) -> T:
    """`read()`, or the value it already produced for `key` this tick.

    A failure is the tick's answer too: a malformed or unreadable policy
    file costs one read and every caller gets the same exception (and the
    same `repo_config_read_failed` line), exactly as one read would.
    """
    if _SCOPE is None:
        return read()
    if key not in _SCOPE:
        try:
            _SCOPE[key] = read()
        except Exception as error:
            _SCOPE[key] = error
    value = _SCOPE[key]
    if isinstance(value, Exception):
        raise value
    return value
