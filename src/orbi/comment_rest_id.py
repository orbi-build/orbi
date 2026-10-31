"""The REST id of one Issue comment record (Issue #2004).

`gh issue view --json comments` reports the GraphQL node id — an
`"IC_kw..."` string no REST route accepts — next to the comment's
permalink, and the permalink's fragment carries the REST id that the
in-place edit's `PATCH /repos/{repo}/issues/comments/{id}` route takes. A
numeric `id` field (an `gh api` read) is that id already.

One rule, one owner: the base-CI red recovery (`base_ci_red`), the
check-permission recovery (`check_permission`) and the failure reporter
(`failure_report`) all read the id from here, so the pattern lives in this
module only. `github.issue_comment_rest_id` stays the neighbouring rule
that resolves a GraphQL node id to a REST id with an API read.
"""
from __future__ import annotations

import re

_COMMENT_URL_ID_RE = re.compile(r"#issuecomment-(\d+)$")


def rest_comment_id(comment: dict) -> int | None:
    """The REST id of one comment, from the id field or its permalink."""
    comment_id = comment.get("id")
    if isinstance(comment_id, int) and not isinstance(comment_id, bool):
        return comment_id
    url = comment.get("url")
    match = _COMMENT_URL_ID_RE.search(url) if isinstance(url, str) else None
    return int(match.group(1)) if match else None
