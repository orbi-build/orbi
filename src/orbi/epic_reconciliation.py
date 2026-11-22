"""One Epic's children and how each of them ended (Issue #2356).

The verification used to reduce every closed child to "Issue #N closed", so
an Epic whose children a person closed without any delivery received the
same "complete" comment and the same completed close reason as a fully
delivered one. The verification now returns one :class:`EpicChild` per
child carrying its outcome — delivered by Orbi (a merged PR child, or an
Issue carrying `ai-merged`), closed while it carried `ai-blocked`, or
closed by a person with GitHub's own `state_reason` — and this module owns
the two decisions built on it: the reconciliation sentence (catalog text,
both languages) and the Epic's close reason.

Both sweeps render through these functions — the per-tick
`claim.reconcile_open_epics` and the release
`release_closeout.reconcile_release_epics` — so the same children can never
produce two different comments or close reasons. The module imports the
`gh` read primitives from :mod:`orbi.github`, which imports neither this
module nor a sweep owner, so there is no cycle.
"""
from __future__ import annotations

import json
from typing import NamedTuple

from orbi import delivery_labels, language
from orbi.delivery_labels import label_name_set
from orbi.github import (
    epic_issue_with_blockers,
    open_blocker_numbers,
    parse_paginated_issue_array,
    run_gh_read_command,
)

# How one Epic child ended.
CHILD_DELIVERED = "delivered"
CHILD_BLOCKED = "blocked"
CHILD_COMPLETED = "completed"
CHILD_NOT_PLANNED = "not_planned"

# `gh issue close --reason`'s documented values, verified with
# `gh issue close --help`: {completed|not planned|duplicate}.
CLOSE_NOT_PLANNED = "not planned"


class EpicChild(NamedTuple):
    """One verified Epic child: what it is and how it ended."""

    kind: str  # "issue" or "pr"
    number: int
    outcome: str  # one of the CHILD_* constants


def verify_epic_complete(repo: str, listed_epic: dict) -> list[EpicChild]:
    """Verify native sub-issues and blockers; otherwise fail closed.

    Live API shape (measured 2026-09-09 against #305):
    ``GET /repos/{owner}/{repo}/issues/{number}/sub_issues`` returns a JSON
    array (``[]`` for no children), paginates with the standard ``--paginate``
    contract, and Issue items include ``repository.full_name`` and no
    ``pull_request`` key.  The endpoint is paginated with ``per_page=100``;
    a cross-repository item is identified by its ``repository.full_name`` and
    is rejected.  A PR-shaped item is identified by a non-null
    ``pull_request`` field and is checked through the PR endpoint.
    """
    epic = epic_issue_with_blockers(repo, listed_epic)
    number = epic.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ValueError("Epic number is missing or invalid")
    blockers = epic.get("blockedBy")
    # Live API check: `gh issue view --json blockedBy` returns
    # {"blockedBy":{"nodes":[],"totalCount":0}} for zero dependencies.
    # Missing/malformed blockedBy is not equivalent to that empty set.
    if not isinstance(blockers, dict) or not isinstance(blockers.get("nodes"), list):
        raise ValueError("native blocker/dependency state is unavailable")
    for node in blockers["nodes"]:
        if (not isinstance(node, dict)
                or not isinstance(node.get("number"), int)
                or isinstance(node.get("number"), bool)
                or ("state" in node and node["state"] not in {"OPEN", "CLOSED"})):
            raise ValueError("native blocker/dependency state is malformed")
    open_blockers = open_blocker_numbers(epic)
    if open_blockers:
        raise ValueError("open blockers: " + ", ".join(f"#{n}" for n in open_blockers))
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/issues/{number}/sub_issues?per_page=100",
        "--paginate", "--slurp",
    ])
    children = parse_paginated_issue_array(raw)
    if not children:
        raise ValueError("Epic child scope is missing or empty")
    verified: list[EpicChild] = []
    for child in children:
        child_number = child.get("number")
        if not isinstance(child_number, int) or isinstance(child_number, bool):
            raise ValueError("Epic child scope contains an invalid number")
        child_repo = child.get("repository")
        if not isinstance(child_repo, dict) or not isinstance(child_repo.get("full_name"), str):
            raise ValueError(f"child #{child_number} repository state is malformed")
        if child_repo["full_name"].lower() != repo.lower():
            raise ValueError(f"cross-repository child #{child_number}")
        if child.get("state") not in {"open", "closed"}:
            raise ValueError(f"child #{child_number} state is malformed")
        kind = "pr" if child.get("pull_request") is not None else "issue"
        verified.append(_epic_child(repo, kind, child_number))
    return verified


def _epic_child(repo: str, kind: str, number: int) -> EpicChild:
    """Verify one child against GitHub's live Issue/PR state."""
    raw = run_gh_read_command(["gh", "api", f"repos/{repo}/issues/{number}"])
    item = json.loads(raw)
    if not isinstance(item, dict):
        raise ValueError(f"child #{number} response is not an object")
    is_pr = "pull_request" in item
    if kind == "issue" and is_pr:
        raise ValueError(f"child #{number} declared as Issue but is a PR")
    if kind == "pr" and not is_pr:
        raise ValueError(f"child #{number} declared as PR but is an Issue")
    if is_pr:
        pr = json.loads(run_gh_read_command(["gh", "api", f"repos/{repo}/pulls/{number}"]))
        if not isinstance(pr, dict) or pr.get("merged") is not True:
            raise ValueError(f"child PR #{number} is not merged")
        return EpicChild("pr", number, CHILD_DELIVERED)
    if item.get("state") != "closed":
        raise ValueError(f"child Issue #{number} is not closed")
    return EpicChild("issue", number, _closed_issue_outcome(item))


def _closed_issue_outcome(item: dict) -> str:
    """How one closed child Issue ended.

    `ai-merged` means Orbi delivered it; `ai-blocked` means Orbi stopped on
    it and a person closed it anyway. Every other closed Issue was closed by
    a person, and GitHub's own `state_reason` says whether they recorded it
    as completed or not planned — a closed Issue without the field is
    GitHub's default, completed.
    """
    labels = label_name_set(item)
    if delivery_labels.MERGED_LABEL in labels:
        return CHILD_DELIVERED
    if delivery_labels.BLOCKED_LABEL in labels:
        return CHILD_BLOCKED
    return (CHILD_NOT_PLANNED if item.get("state_reason") == "not_planned"
            else CHILD_COMPLETED)


def epic_audit(children: list[EpicChild], version: str | None,
               language_code: str) -> str:
    """The ONE Epic reconciliation sentence, in the bound language.

    Today's "complete" sentence is kept byte for byte when every child was
    delivered by Orbi — its child fragments are the same shared evidence the
    pre-#2356 verification built ("PR #N merged", "Issue #N closed"), so
    only the sentence around them moves. Every other Epic states that all
    children are closed but not all of them were delivered, followed by one
    fragment per child naming how it ended.
    """
    prefix = "" if not version else language.text(
        language_code, "epic.reconciliation_version", version=version,
    )
    if all(child.outcome == CHILD_DELIVERED for child in children):
        return language.text(
            language_code, "epic.reconciliation", prefix=prefix,
            children=", ".join(_delivered_fragment(child) for child in children),
        )
    return language.text(
        language_code, "epic.reconciliation_closed", prefix=prefix,
        children=", ".join(
            _child_wording(child, language_code) for child in children
        ),
    )


def _delivered_fragment(child: EpicChild) -> str:
    """The delivered-child fragment of the "complete" sentence."""
    return (f"PR #{child.number} merged" if child.kind == "pr"
            else f"Issue #{child.number} closed")


# One catalog key per (child kind, outcome): how a child is named in the
# sentence that does not claim completion (Issue #2356).
_CHILD_WORDING = {
    ("pr", CHILD_DELIVERED): "epic.child_delivered_pr",
    ("issue", CHILD_DELIVERED): "epic.child_delivered_issue",
    ("issue", CHILD_BLOCKED): "epic.child_blocked",
    ("issue", CHILD_COMPLETED): "epic.child_completed",
    ("issue", CHILD_NOT_PLANNED): "epic.child_not_planned",
}


def _child_wording(child: EpicChild, language_code: str) -> str:
    """One child named with how it ended, in the bound language."""
    return language.text(
        language_code, _CHILD_WORDING[(child.kind, child.outcome)],
        number=child.number,
    )


def epic_close_reason(children: list[EpicChild]) -> str | None:
    """The `gh issue close --reason` value this Epic must be closed with.

    "not planned" only when Orbi delivered none of the children and none of
    them was closed as completed — the scene a person reads as "Orbi
    delivered nothing here". Every other case keeps today's completed close
    (`None` leaves GitHub's default, completed).
    """
    if any(child.outcome in {CHILD_DELIVERED, CHILD_COMPLETED}
           for child in children):
        return None
    return CLOSE_NOT_PLANNED


def epic_audit_posted(comments: list[dict], children: list[EpicChild],
                      version: str | None = None) -> bool:
    """True when a comment already carries the audit in EITHER language.

    The audit sentence is its own duplicate check: both sweeps close the
    Epic right after commenting, so a comment without a close means the
    close failed and the retry may render in another language. Matching
    every rendering keeps a posted English audit from being duplicated by a
    Chinese one (Issue #1705), in both the complete and the #2356 sentence.
    """
    renderings = [
        epic_audit(children, version, language_code)
        for language_code in language.LANGUAGES
    ]
    return any(
        rendering in str(comment.get("body", ""))
        for comment in comments
        for rendering in renderings
    )


def epic_closed_evidence(number: int, children: list[EpicChild]) -> str:
    """The ONE evidence line both sweeps record for a closed Epic."""
    fragments = "; ".join(
        f"#{child.number} {child.outcome}" for child in children
    )
    return f"Epic #{number} closed after verification ({fragments})"
