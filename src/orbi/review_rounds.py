"""Review-round accounting and the resumable round comment (Issue #1659).

Two readers decide which comments are review rounds, without depending on
a translatable English heading:

* a NEW round record is canonical — the comment carries the
  `<!-- orbi:comment=review-round -->` kind and a valid trusted scene,
  whose `review_round`/`pr_url` supply the round and PR identity;
* a historical comment with no kind marker keeps the existing
  `Orbi review round <N> for PR #<P>:` heading fallback.

A comment that DOES carry a kind marker is never reinterpreted through
its prose: a `failure` comment that happens to contain a scene consumes
no round. `review_round_comment_body` writes the canonical kind, so the
writer and the readers share one definition.

Imports pull in `github` (`_comment_is_trusted`, `_pr_number`) and
`progress` (`run_marker`); nothing here imports `runner` back
(Article 3.3). The module is small and pure apart from those helpers.
"""
from __future__ import annotations

import re

from orbi import language, scene
from orbi.comment_kind import (
    COMMENT_KIND_REVIEW_ROUND,
    comment_kind,
    comment_kind_marker,
)
from orbi.github import _comment_is_trusted, _pr_number
from orbi.progress import run_marker


def _shorten_review_shas(text: str) -> str:
    """Keep full object IDs in the scene, but readable IDs in prose."""
    return re.sub(
        r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
        lambda match: match.group(1)[:10], text, flags=re.IGNORECASE,
    )


def _review_findings_markdown(findings: list[dict],
                              language_code: str = language.DEFAULT_LANGUAGE,
                              ) -> str:
    """Render the human part of a review verdict, never as JSON.

    Only the four human labels follow the message language (Issue
    #1660); the model-written values stay verbatim.
    """
    labels = {
        name: language.text(language_code, f"review.finding_{name}")
        for name in ("level", "location", "note", "fix")
    }
    items = []
    for finding in findings:
        items.append(
            f"- **{labels['level']}** " + str(finding.get("level", ""))
            + f"\n  **{labels['location']}** "
            + str(finding.get("location", ""))
            + f"\n  **{labels['note']}** " + str(finding.get("note", ""))
            + f"\n  **{labels['fix']}** " + str(finding.get("fix", ""))
        )
    return "\n".join(items)


def _scene_matches_pr(parsed: scene.Scene, pr_number: int) -> bool:
    """True when a parsed scene names the expected PR number."""
    try:
        return _pr_number(parsed.pr_url) == pr_number
    except (TypeError, ValueError):
        return False


def same_findings_round(
    comment: dict, *, marker: str, pr_number: int, previous_round: int,
    rendered: str,
) -> bool:
    """True when one trusted comment is this round's identical predecessor.

    Identity is canonical: the `review-round` kind plus the trusted
    scene's round and PR (Issue #1659). A historical English comment
    without a kind marker keeps the existing heading fallback; a comment
    that DOES carry a kind marker is identified by that kind alone and
    never falls back to its prose. A present-but-invalid scene does not
    match.
    """
    if not _comment_is_trusted(comment):
        return False
    body = comment.get("body")
    if not isinstance(body, str):
        return False
    if marker and not body.startswith(marker):
        return False
    if rendered not in body:
        return False
    kind = comment_kind(body)
    if kind is None:
        return (
            f"Orbi review round {previous_round} for PR #{pr_number}:"
            in body
        )
    if kind != COMMENT_KIND_REVIEW_ROUND:
        return False
    try:
        parsed = scene.parse(body)
    except scene.SceneError:
        return False
    if parsed is None or parsed.review_round != previous_round:
        return False
    return _scene_matches_pr(parsed, pr_number)


def review_round_comment_body(
    marker: str, round: int, pr_number: int, blockers: int, majors: int,
    findings: list[dict], scene_block: str, *,
    previous_comments: list[dict] | None = None,
    messages: list[str] | None = None,
    heading: str | None = None,
    kind: str = COMMENT_KIND_REVIEW_ROUND,
    language_code: str = language.DEFAULT_LANGUAGE,
) -> str:
    """Build a readable, resumable review-round comment.

    The comment carries the canonical kind right after the run marker:
    `review-round` for a COMPLETED review round, `failure` for a gate
    failure that reuses the previous scene (Issue #1659). The scene block
    is appended untouched because it is the machine-readable recovery
    protocol. The readable review-round line stays for humans and for
    the historical English fallback.
    """
    rendered = _review_findings_markdown(findings, language_code)
    same_as = None
    if rendered and previous_comments:
        for comment in previous_comments:
            if same_findings_round(
                comment, marker=marker, pr_number=pr_number,
                previous_round=round - 1, rendered=rendered,
            ):
                same_as = round - 1
                break
    lines = [
        f"{marker}",
        comment_kind_marker(kind),
        heading or language.text(
            language_code, "review.round_heading", round=round,
            pr_number=pr_number, blockers=blockers, majors=majors,
        ),
    ]
    if messages:
        lines.extend(["", "\n\n".join(
            _shorten_review_shas(message) for message in messages
        )])
    if rendered:
        findings_heading = language.text(
            language_code, "review.findings_heading",
        )
        lines.extend(["", findings_heading, ""])
        if same_as is not None:
            lines.append(language.text(
                language_code, "review.same_findings", round=same_as,
            ))
            lines.append("")
        lines.append(_shorten_review_shas(rendered))
    return "\n".join(lines) + "\n" + scene_block


def review_rounds_so_far(
    comments: list[dict], *, after: str | None = None,
    run_id: str | None = None, pr_number: int | None = None,
) -> int:
    """Count trusted review rounds for the selected delivery identity.

    A NEW round record is canonical (Issue #1659): the comment carries
    the `review-round` kind and a valid trusted scene, whose round and PR
    supply the identity. A historical English comment without a kind
    marker keeps the existing `Orbi review round ...` heading fallback.
    A comment that DOES carry a kind marker is never reinterpreted
    through its prose: a failure comment that happens to contain a scene
    consumes no round. With `run_id` supplied, only comments carrying
    that attempt's stable marker count. This is essential when an Issue
    is picked up again after a closed PR: historical rounds remain
    visible evidence, but cannot consume the new PR's budget. `after`
    retains the existing human-recovery boundary for resumes of the
    same PR. Calls without an identity filter remain available for
    legacy evidence rendering.
    """
    rounds = 0
    marker = run_marker(run_id) if run_id is not None else None
    for comment in comments:
        if not _comment_is_trusted(comment):
            continue
        if after is not None and comment.get("createdAt", "") <= after:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if marker is not None and not body.startswith(marker):
            continue
        kind = comment_kind(body)
        if kind is not None:
            if kind != COMMENT_KIND_REVIEW_ROUND:
                continue
            try:
                parsed = scene.parse(body)
            except scene.SceneError:
                continue
            if parsed is None:
                continue
            if run_id is not None and parsed.run_id != run_id:
                continue
            if pr_number is not None and not _scene_matches_pr(
                    parsed, pr_number):
                continue
            rounds += 1
            continue
        # Historical English fallback (no canonical metadata).
        if pr_number is None:
            matches = any(line.startswith("Orbi review round ")
                          for line in body.splitlines())
        else:
            matches = any(
                line.startswith(f"Orbi review round ") and
                f" for PR #{pr_number}:" in line
                for line in body.splitlines()
            )
        if matches:
            rounds += 1
    return rounds
