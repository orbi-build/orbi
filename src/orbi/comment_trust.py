"""Which comments may steer a delivery (Issue #1806).

A comment is trusted when its AUTHOR CAN WRITE to the repository, decided
from the repository permission GitHub reports for them
(`github.repository_permission`) — never from the comment's
``authorAssociation``. That field is the reading token's view of the
author's organization membership: for a private org member the App token
sees ``CONTRIBUTOR`` even when the person is an org admin with admin
access (orbi-build/orbi-cloud#2715), and ``MEMBER``/``COLLABORATOR``
do not imply write access either (an org member or collaborator can be
read-only).

The second channel is the runner's own account (the identity the `gh`
credential resolves to), so a self-hosted install whose Runner and
maintainer share one account still trusts its own comments.

Every function here is a pure scan over an already-fetched comment list;
the only GitHub read is the permission read inside
`comment_is_trusted`, and `trusted_comments` keeps a scan to one read
per author. A failed permission read is a GitHub read failure and
propagates — it is never turned into "no permission" or a silently
dropped comment.
"""
from __future__ import annotations

from orbi.github import (
    WRITE_PERMISSIONS,
    authenticated_github_login,
    strip_bot_suffix,
    repository_permission,
)
from orbi.progress import RUN_MARKER_PATTERN, run_marker


def comment_is_trusted(
    comment: object, *, repo: str,
    permissions: dict[str, str] | None = None,
) -> bool:
    """True when the comment's author can write to `repo`, or is this runner.

    ``permissions`` is an optional per-scan memo (login -> repository
    permission): a scan passes the same dict to every check so each author
    costs one read, not one read per comment.
    """
    if not isinstance(comment, dict):
        return False
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str):
        return False
    if permissions is not None and login in permissions:
        permission = permissions[login]
    else:
        permission = repository_permission(login, repo=repo)
        if permissions is not None:
            permissions[login] = permission
    if permission in WRITE_PERMISSIONS:
        return True
    # A copied run marker is not sufficient: the author must be the account
    # represented by the currently authenticated installation token. The
    # optional `[bot]` suffix is normalized on both sides because GraphQL
    # drops it and REST keeps it.
    return strip_bot_suffix(login) == strip_bot_suffix(
        authenticated_github_login()
    )


def trusted_comments(comments: list[dict], *, repo: str) -> list[dict]:
    """The comments whose author can write to `repo`, or is this runner.

    The scan form of `comment_is_trusted` — order preserved, one
    permission read per author (a long Issue history is one read per
    person, not one per comment).
    """
    permissions: dict[str, str] = {}
    return [
        comment for comment in comments
        if comment_is_trusted(comment, repo=repo, permissions=permissions)
    ]


def trusted_issue_comments_block(
    comments: list[dict], limit: int, *, repo: str,
) -> str:
    """Render the {{ISSUE_COMMENTS}} prompt block.

    Only trusted authors enter the task context — a public repo lets
    anyone comment, and an unfiltered injection would be a
    prompt-injection surface (Issue #45), so the trust boundary is the
    author's repository write permission (Issue #1806). The input order is
    preserved (oldest first, the natural timeline read). Over `limit` the
    OLDEST trusted comments are dropped — the newest carry the latest
    decision — and the omission is stated inside the block: the agent must
    know it did not see the full history, never a silent truncation. Zero
    trusted comments produce an explicit marker, not an empty string.
    """
    trusted = trusted_comments(comments, repo=repo)
    kept = trusted[-limit:]
    omitted = len(trusted) - len(kept)
    if not kept:
        return "(no trusted comments)"
    lines = []
    if omitted:
        noun = "comment" if omitted == 1 else "comments"
        lines.append(
            f"({omitted} older trusted {noun} omitted; showing the "
            f"{len(kept)} most recent)"
        )
    for comment in kept:
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        state = comment.get("state")
        anchor = ""
        if "path" in comment:
            line = comment.get("line") or comment.get("original_line") or "-"
            anchor = f" at {comment.get('path') or '-'}:{line}"
        state_text = f" [{state}]" if state else ""
        lines.append(
            f"- {login or 'unknown'}{anchor}{state_text} at "
            f"{comment.get('createdAt') or '-'}:\n\n"
            f"{str(comment.get('body') or '').rstrip()}"
        )
    return "\n\n".join(lines)


def latest_run_marker(comments: list[dict], *, repo: str) -> str:
    """The latest trusted comment's rendered run marker, or "".

    Recovery reports name the run they failed for when any trusted
    comment of the Issue still carries the marker; an empty string when
    none does. A pure scan over the already-fetched comment list.
    """
    for comment in reversed(trusted_comments(comments, repo=repo)):
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = RUN_MARKER_PATTERN.search(body)
        if match:
            return run_marker(match.group(1))
    return ""


def line_run_markers(comments: list[dict], *, repo: str) -> frozenset[str]:
    """Every rendered run marker in the Issue's trusted comments.

    Issue #825: a delivery line may resume under a NEW run id, so the
    line's identity is the SET of run ids its trusted comments carry —
    the creating run's marker stays accepted after the rebind. The same
    trust filter as the resume scene (`trusted_comments`): a copied
    marker in a public comment widens nothing. A pure scan over the
    already-fetched comment list.
    """
    markers: set[str] = set()
    for comment in trusted_comments(comments, repo=repo):
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        markers.update(
            run_marker(match.group(1))
            for match in RUN_MARKER_PATTERN.finditer(body)
        )
    return frozenset(markers)
