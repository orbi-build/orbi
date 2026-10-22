"""The readable summary of a task notification and its status link (#1826).

Every Orbi task comment opens with the same readable section: the
localized state the caller already renders as its headline, the task's
identity when the caller already knows it, the required action when there
is one, and — only when the host configured it — one clearly labeled link
to the operator's existing project status page.

This module is the ONE place that validates and renders that value. The
configuration loader calls :func:`normalize_status_url` (the only
validation; an optional invalid value produces one safe warning and is
ignored, so it can never block a delivery), and every renderer calls
:func:`summary_lines` / :func:`summary_text`, so no caller formats a link
or copies a URL check.

`status_url` is a complete, fixed URL: not a template, with no
placeholder and no task identity of its own. The operator owns the
destination and its access behavior; Orbi never requests it. The URL is
published verbatim, including a dashboard's repository-filter query.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from orbi import language

LOGGER = logging.getLogger("orbi.bootstrap")

STATUS_URL_KEY = "status_url"

# A URL published inside a Markdown link must not break the link or the
# comment around it: whitespace and the Markdown/HTML delimiters are
# rejected (a legitimate dashboard URL needs no parentheses or quotes).
_UNSAFE_URL_CHARACTERS = frozenset(" \t\r\n<>\"'`()[]{}|\\^")


def normalize_status_url(raw: object, *, source: str) -> str | None:
    """Validate the optional host ``status_url``; never fails the load.

    Absent (``None``) is the normal case: it returns ``None`` quietly, so
    the notifications stay GitHub-only. A present value must be a complete
    ``https`` URL without userinfo and without characters that would break
    the rendered link. Anything else is reported as ONE warning naming the
    source, the key and the reason — never the raw value, which may carry
    a credential — and returns ``None`` so the rest of the configuration
    and the delivery continue normally.
    """
    if raw is None:
        return None
    reason = _rejection_reason(raw)
    if reason is not None:
        # Key-first (never kind-first): this is a configuration warning,
        # not a journal event, and every structured journal line must be
        # emitted through journal.event().
        LOGGER.warning(
            "key=%s ignored (source=%s reason=%s)",
            STATUS_URL_KEY, source, reason,
        )
        return None
    return raw


def _rejection_reason(raw: object) -> str | None:
    """The safe reason code of one unusable value, or None when valid."""
    if not isinstance(raw, str) or not raw:
        return "not_a_non_empty_string"
    if any(character in _UNSAFE_URL_CHARACTERS for character in raw):
        return "unsafe_characters"
    try:
        parts = urlsplit(raw)
        # A non-numeric or out-of-range port is only validated on access.
        _ = parts.port
    except ValueError:
        return "malformed_url"
    if parts.scheme != "https":
        return "scheme_not_https"
    if not parts.netloc:
        return "missing_host"
    if "@" in parts.netloc:
        return "userinfo_not_allowed"
    return None


def project_status_line(status_url: str | None,
                        language_code: str) -> str | None:
    """The one project-status link line, or None when unconfigured."""
    if not status_url:
        return None
    return language.text(language_code, "summary.project_status", url=status_url)


def summary_lines(*, status_url: str | None = None,
                  issue: object = None, title: object = None,
                  action: str | None = None,
                  language_code: str) -> list[str]:
    """The summary lines a task notification shows before its diagnostics.

    The caller's own localized headline IS the short readable state; these
    lines add what the caller already knows on top of it: the task
    identity (when a title is available), the required action (when there
    is one) and the optional fixed project-status link. Missing pieces
    render nothing at all — no placeholder, no guessed value.
    """
    lines: list[str] = []
    if isinstance(title, str) and title.strip():
        number = (
            f"#{issue} "
            if isinstance(issue, int) and not isinstance(issue, bool)
            else ""
        )
        lines.append(f"**{number}{' '.join(title.split())}**")
    if action:
        lines.append(language.text(
            language_code, "summary.action", action=action,
        ))
    link = project_status_line(status_url, language_code)
    if link is not None:
        lines.append(link)
    return lines


def append_summary(body: str, **fields: object) -> str:
    """Append the summary lines to a one-sentence task message.

    The existing headline sentence stays the readable state; the summary
    follows it, so the identity, the pre-rendered keyword values and the
    project status link come before any diagnostics.
    """
    lines = summary_lines(**fields)  # type: ignore[arg-type]
    return body + "\n\n" + "\n".join(lines) if lines else body


def notice(marker: str, key: str, *, language_code: str,
           status_url: str | None = None, issue: int | None = None,
           title: str | None = None, **fields: object) -> str:
    """One short task message: its own sentence plus the summary.

    The callers whose notification is a single localized sentence (a
    ticket-only result, a blocked scene, a release wait) keep their exact
    marker prefix and wording; the readable summary — the task identity
    when the caller already knows it and the operator's optional fixed
    project status link — follows directly below it, so the state comes
    first and the routine diagnostics after it.
    """
    sentence = language.text(language_code, key, **fields)
    return append_summary(
        f"{marker}\n{sentence}" if marker else sentence,
        status_url=status_url, issue=issue, title=title,
        language_code=language_code,
    )


def summary_text(**fields: object) -> str:
    """:func:`summary_lines` as one template segment ('' when empty).

    A non-empty segment ends with one newline, so a catalog template can
    put it on its own line(s) right below the state sentence without the
    empty case leaving a stray blank line.
    """
    lines = summary_lines(**fields)  # type: ignore[arg-type]
    return "\n".join(lines) + "\n" if lines else ""
