#!/usr/bin/env python3
"""The images one Issue hands a delivery or review session (Issue #2364).

A session's images come from exactly two places: the Issue's Attachments
section — the markdown image lines a creation wrote AFTER the draft marker,
whatever their host — and every native GitHub image
(github.com/user-attachments/assets/...) in the body or in a comment whose
author can write (orbi.comment_trust). An arbitrary link written in a
comment is never fetched.

A native image is read the one documented way for an installation token:
GitHub's Markdown rendering turns it into a signed address, and that address
is fetched with NO credential of ours (measured 2026-11-22: the same
repository's native image downloads through the rendered address, while the
bare asset needs a person's token). The signed address carries the
credential, so it is never logged and never written into a comment.

Only a model that declares image input gets this work: an image cannot
reach any other model, so nothing is read for it (orbi.model_catalog).

Unreadable means stop: an attachment is often the request itself, so
skipping it would write the wrong code. A failed image download or render
raises `Unreadable` naming the files, the caller stops before the session
starts, and the exception CARRIES its failure record: the existing
failure-stop path reports it once as `attachment_unavailable`, blocks the
Issue and resumes nothing, so an unreadable file cannot be retried every tick
(Issue #2364). A failed COMMENT read stays a bypass (Issue #79): the body's
own images still arrive.
"""
from __future__ import annotations

import logging
import re
import urllib.request
from pathlib import Path

from orbi import attachments, draft_marker, failure, model_catalog
from orbi.comment_trust import comment_is_trusted
from orbi.github import issue_comments, run_gh_write_command
from orbi.journal import event

# The stop's own reason code, one member of `orbi.failure`'s closed set: a
# person repairs it by uploading the file again or removing it, and no
# automatic retry can.
REASON_CODE = "attachment_unavailable"


class Unreadable(RuntimeError):
    """One or more of an Issue's images could not be read.

    The names are what the failure comment shows; a URL never rides along,
    because a signed address carries its credential in the query. The failure
    record rides the exception, so the STOP is decided at its source: the
    failure report reads the record, never the sentence, and the Issue blocks
    with one comment instead of re-running the same unreadable image.
    """

    def __init__(self, names: "tuple[str, ...]") -> None:
        self.names = names
        plural = "s" if len(names) > 1 else ""
        super().__init__(
            f"the attached image{plural} {', '.join(names)} could not be read"
        )
        action_code, retry_safe = failure.DISPOSITIONS[REASON_CODE]
        self.failure_record = failure.Failure(
            reason_code=REASON_CODE, action_code=action_code,
            retry_safe=retry_safe,
        )


# The repository-relative directory the images are staged in, inside the
# worktree's excluded run dir.
IMAGES_DIRNAME = "attachments"
DOWNLOAD_TIMEOUT_SECONDS = 60
NATIVE_PREFIX = "https://github.com/user-attachments/assets/"
_MARKER_LINE_RE = re.compile(
    r"<!-- " + re.escape(draft_marker.DRAFT_MARKER) + r":"
)
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^)\s]+)\)")
_SRC_RE = re.compile(r'src="(?P<url>[^"]+)"')
_MAGIC = (
    (b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"),
    (b"GIF8", ".gif"), (b"RIFF", ".webp"),
)


def for_session(
    issue: dict, *, config, repo: str, worktree: Path, review: bool = False,
) -> attachments.Attached:
    """The images one delivery or review session receives.

    Raises `Unreadable` when any image cannot be read: the caller stops
    before the session starts, so the request is never half-seen, and the
    failure-stop path reports it once as `attachment_unavailable` (the
    record rides the exception). A failed comment read is still a bypass,
    logged and skipped, and leaves the body's own images in place.
    """
    viewable = model_catalog.declares_image_input(config, review=review)
    if not viewable:
        # A session whose model declares no image input cannot see one: the
        # file is neither read nor named, and an unreadable image can never
        # stop a session that could not use it (Issue #2364).
        return attachments.Attached()
    number = issue.get("number")
    body = issue.get("body")
    comments = _comments(number, repo=repo)
    files: list[attachments.StagedFile] = []
    unreadable: list[str] = []
    destination = Path(worktree) / ".orbi" / IMAGES_DIRNAME
    for position, (url, alt) in enumerate(
        targets(body, comments, repo=repo), start=1,
    ):
        name = alt or _display(url).rsplit("/", 1)[-1] or "an image"
        try:
            data = _image_bytes(url, repo=repo)
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / f"{position}{_suffix(data)}"
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001 - reported by its name only
            event(
                "issue_images_unreadable", level=logging.WARNING,
                issue=number, url=_display(url), reason=type(exc).__name__,
            )
            unreadable.append(name)
            continue
        files.append(attachments.StagedFile(
            name=path.name, source=name, image=True, path=path, address=url,
        ))
    if unreadable:
        raise Unreadable(tuple(unreadable))
    return attachments.Attached(files=tuple(files), viewable=True)


def with_context(context: str, images: attachments.Attached) -> str:
    """One session context with the image naming line appended."""
    naming = images.naming()
    return f"{context}\n{naming}" if naming else context


def targets(
    body: object, comments: object, *, repo: str,
) -> list[tuple[str, str]]:
    """The (address, alt) pairs one Issue offers a session, deduplicated.

    The Attachments section's images come first, then the body's own native
    images, then those of every TRUSTED comment: untrusted text is never
    fetched, and an image repeated in two places is read once.
    """
    text = body if isinstance(body, str) else ""
    found = _images(_section(text))
    found.extend(_native(text))
    permissions: dict[str, str] = {}
    for comment in comments if isinstance(comments, list) else ():
        if not comment_is_trusted(comment, repo=repo, permissions=permissions):
            continue
        found.extend(_native(str(comment.get("body") or "")))
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for url, alt in found:
        if url in seen:
            continue
        seen.add(url)
        pairs.append((url, alt))
    return pairs


def _section(body: str) -> str:
    """What one created body carries after its LAST draft marker line."""
    lines = body.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if _MARKER_LINE_RE.search(lines[index]):
            return "\n".join(lines[index + 1:])
    return ""


def _images(text: str) -> list[tuple[str, str]]:
    """Every markdown image of one text, with its alt text."""
    return [
        (_unescape(match.group("url")), match.group("alt").strip())
        for match in _IMAGE_RE.finditer(text)
    ]


def _native(text: str) -> list[tuple[str, str]]:
    """The native GitHub images of one text, in order."""
    return [pair for pair in _images(text) if pair[0].startswith(NATIVE_PREFIX)]


def _unescape(url: str) -> str:
    return url.replace("&amp;", "&")


def _comments(number: object, *, repo: str) -> list[dict]:
    """The Issue's comments; a failed read leaves the body's images only."""
    try:
        return issue_comments(int(number), repo=repo)
    except Exception as exc:  # noqa: BLE001 - a bypass, logged and skipped
        event(
            "issue_images_unreadable", level=logging.WARNING,
            issue=number, reason=type(exc).__name__,
        )
        return []


def _image_bytes(url: str, *, repo: str) -> bytes:
    """One image's bytes; a native one through its rendered address."""
    address = _signed(url, repo=repo) if url.startswith(NATIVE_PREFIX) else url
    with urllib.request.urlopen(
        address, timeout=DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        return response.read()


def _signed(url: str, *, repo: str) -> str:
    """The address GitHub's Markdown rendering signs for one native image.

    POST /markdown with the installation token renders the image into an
    address that downloads with no token; a rendering that carries no image
    falls back to the native url, which still downloads when public.
    """
    html = run_gh_write_command([
        "gh", "api", "--method", "POST", "/markdown",
        "-f", f"text=![image]({url})",
        "-f", f"context={repo}",
    ])
    match = _SRC_RE.search(html)
    return _unescape(match.group("url")) if match else url


def _suffix(data: bytes) -> str:
    """One downloaded image's suffix, from its own magic bytes.

    Anything else is not an image the model could read, so it counts as
    unreadable rather than being staged under a guessed suffix.
    """
    for magic, suffix in _MAGIC:
        if data.startswith(magic):
            return suffix
    raise ValueError("not an image")


def _display(url: str) -> str:
    """One address as the journal may keep it: never its query string.

    A signed address carries its credential in the query (jwt=...), so a
    logged address stops at the path.
    """
    return url.split("?", 1)[0]
