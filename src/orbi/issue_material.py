#!/usr/bin/env python3
"""The ONE Issue material every model session receives (Issue #2390).

Before #2390 the Issue reached models through four text builders and two
image paths that disagreed: the clarify gate judged the text alone, the
ticket-only run had no images, the review prompt had no title, and the
steering restart never refreshed images. A fix landed in one copy and the
others missed it. AGENTS.md "One operation, one entry point" applies.

`material` is that one entry point. Every place that hands an Issue to a
model — the thin-ticket gate, the ticket-only content agent, implementation
and ops (including resume), a steering restart, and every review/fix round —
receives the SAME material:

- the title and the body (HTML/text attachments are already inline blocks
  in the body);
- the trusted comments under ONE trust rule and ONE count limit
  (`orbi.comment_trust.trusted_issue_comments`), so a comment whose text is
  dropped cannot still contribute an image;
- the Issue's images (Attachments section, native images in the body and in
  trusted comments) when the session's model declares image input
  (`orbi.issue_images.stage`).

An unreadable image raises `orbi.issue_images.Unreadable` here, BEFORE any
session starts, so every path follows the existing `attachment_unavailable`
stop instead of judging or delivering half the Issue.

`orbi suggest` uses the same text renderer (`text`) without comments or
images: it reads up to 200 open Issues, so material is deliberately the
session-only shape.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orbi import attachments, issue_images, model_catalog
from orbi.comment_trust import (
    trusted_comments,
    trusted_issue_comments_block,
)
from orbi.github import pr_comments, pr_review_comments, pr_reviews
from orbi.github_feedback import normalize_pr_feedback
from orbi.journal import LOGGER


def text(issue: dict) -> str:
    """The ONE text renderer of an Issue: its title, then its body.

    Used by every session's material and by `orbi suggest` (which reads only
    the text: no comments, no images). The clarify stop digest is built from
    the same function, so the text the gate judges and the text it records
    can never disagree.
    """
    return (
        f"Issue #{issue['number']}: {issue['title']}\n\n"
        f"{issue.get('body') or ''}"
    )


@dataclass(frozen=True)
class Material:
    """One Issue as a session receives it, built by `material` only."""

    number: int
    title: str
    body: str
    comments: tuple[dict, ...]
    comment_text: str
    images: attachments.Attached

    def text(self) -> str:
        """The shared title-and-body text of this material."""
        return text(
            {"number": self.number, "title": self.title, "body": self.body},
        )

    def decorate(self, context: str, *, comments: bool = True) -> str:
        """One session context with the shared blocks appended.

        `comments` is False on a path whose prompt template already carries
        `{{ISSUE_COMMENTS}}` (the implementer, the reviewer): the text must
        not be duplicated. The image naming line is always appended, so a
        restarted session that re-stages images names them too.
        """
        parts = [context.rstrip()]
        if comments and self.comment_text:
            parts.append(self.comment_text)
        naming = self.images.naming()
        if naming:
            parts.append(naming)
        return "\n\n".join(part for part in parts if part)


def material(
    issue: dict, config, *, repo: str, directory: Path,
    review: bool = False, comments: Sequence[dict] | None = None,
    extra_comments: Sequence[dict] = (), with_comments: bool = True,
) -> Material:
    """Build the ONE Issue material a session receives (Issue #2390).

    `comments` lets a steering restart reuse the snapshot it already read
    instead of asking GitHub again. `extra_comments` is the reviewer-only PR
    feedback added on top of the shared Issue timeline. `with_comments` is
    False when the session's prompt carries `{{ISSUE_COMMENTS}}` itself; the
    images still read the trusted comments, but the context omits the block.

    A failed comment read stays a bypass (`issue_images.comments`), while an
    unreadable image raises `issue_images.Unreadable` before the session.
    """
    number = int(issue["number"])
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    viewable = model_catalog.declares_image_input(config, review=review)
    fetched = (
        list(comments) if comments is not None
        else issue_images.comments(number, repo=repo)
        if with_comments or viewable else []
    )
    limit = config.issue_comments_limit
    issue_kept = _limited(fetched, limit, repo=repo)
    if extra_comments:
        kept = _limited(
            sorted(
                [*fetched, *extra_comments],
                key=lambda item: str(item.get("createdAt") or ""),
            ),
            limit, repo=repo,
        )
        block = trusted_issue_comments_block(
            sorted(
                [*fetched, *extra_comments],
                key=lambda item: str(item.get("createdAt") or ""),
            ),
            limit, repo=repo,
        )
    else:
        kept = issue_kept
        block = trusted_issue_comments_block(fetched, limit, repo=repo)
    images = issue_images.stage(
        issue, trusted_comments=issue_kept, viewable=viewable,
        directory=directory, repo=repo,
    )
    return Material(
        number=number, title=title, body=body, comments=tuple(kept),
        comment_text=block if with_comments else "", images=images,
    )


def _limited(comments: list[dict], limit: int, *, repo: str) -> list[dict]:
    """The trusted comments one session keeps, under the ONE count limit."""
    return trusted_comments(comments, repo=repo)[-limit:]


def review_feedback(pr: dict, *, repo: str) -> list[dict]:
    """The current PR's feedback, normalized for the shared timeline.

    Review-only context added on top of the Issue material: formal reviews,
    conversation and inline comments. A failed read is logged and leaves the
    Issue's own timeline in place (the pre-#2390 review behavior).
    """
    try:
        feedback = pr_comments(pr["number"], repo=repo)
        feedback += pr_reviews(pr["number"], repo=repo)
        feedback += pr_review_comments(pr["number"], repo=repo)
        return normalize_pr_feedback(feedback)
    except Exception:
        LOGGER.exception(
            "pr_review_feedback_read_failed repo=%s pr=%s",
            repo, pr["number"],
        )
        return []
