"""Orbi's own Git identity, set once for every process the Runner starts.

The delivery commits, the Runner's base merges, and the release commit and
tag are all local Git objects Orbi writes. They must be credited to Orbi's
GitHub App account, not to whatever identity the host config happens to
carry and not to an account a Pi session picks with `git -c user.*`.

`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/
`GIT_COMMITTER_EMAIL` take precedence over `user.*` config, including
`git -c user.*`, so applying this identity to the Runner's environment at
startup pins it for the whole tick and for every child process (Issue
#1416). The address resolves to the `orbi-build[bot]` App account
(`gh api /users/orbi-build%5Bbot%5D` returns id 327503608).
"""
from __future__ import annotations

from collections.abc import MutableMapping

BOT_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "orbi-build[bot]",
    "GIT_AUTHOR_EMAIL": "327503608+orbi-build[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "orbi-build[bot]",
    "GIT_COMMITTER_EMAIL": "327503608+orbi-build[bot]@users.noreply.github.com",
}


def set_bot_git_identity(env: MutableMapping[str, str]) -> None:
    """Set Orbi's bot identity in `env`, overwriting any inherited value."""
    env.update(BOT_GIT_IDENTITY)
