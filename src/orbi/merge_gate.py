"""Merge-gate policy: the blocker lines that rule a merge (Issue #1361).

The pure half of the merge-gate preflight. `orbi.github` reads the
target branch, its classic protection and the branch rules through the
`gh` seam, and owns how a failed read is reported; this module turns
the payloads it read into the report the user sees — one line per
blocker, each naming the merge gate's own repair, or the single PASS
line. No I/O, no subprocess, no environment: one deterministic
function over already-read data (Article 3.3's pure-module model,
`delivery_labels.py` being the original).
"""
from __future__ import annotations


def classify_protection(
    repo: str, branch: str, *, classic: dict | None, rules: list[dict],
    classic_unreadable: bool,
) -> list[str]:
    """Return the merge-gate report lines for the protection that was
    read: the classic protection blockers, the branch-ruleset blockers,
    the classic 404 verdict, or the PASS line when nothing blocks."""
    blockers: list[str] = []
    if classic is not None:
        reviews = classic.get("required_pull_request_reviews")
        if reviews is None:
            reviews = {}
        if not isinstance(reviews, dict):
            return ["merge_gate: UNKNOWN review protection is not an object"]
        approvals = reviews.get("required_approving_review_count", 0)
        if isinstance(approvals, int) and approvals >= 1:
            blockers.append(
                f"merge_gate: FAILED classic protection requires "
                f"{approvals} approving review(s) the configured merge "
                "identity cannot supply (self-approval is forbidden); "
                "repair: add a different approving reviewer; after approval "
                "Orbi retries the merge"
            )
        admins = classic.get("enforce_admins") or {}
        if isinstance(admins, dict) and admins.get("enabled") is True:
            blockers.append(
                "merge_gate: FAILED classic protection enforces admins; "
                "repair: disable 'Do not allow bypassing the above settings' "
                "or configure a repository-governance merge path for Orbi; "
                "after the policy change Orbi retries the merge"
            )

    for rule in rules:
        parameters = rule.get("parameters")
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            return ["merge_gate: UNKNOWN ruleset parameters are not an object"]
        approvals = parameters.get("required_approving_review_count")
        if isinstance(approvals, int) and approvals >= 1:
            source = rule.get("ruleset_source") or "ruleset"
            ruleset_id = rule.get("ruleset_id")
            location = (
                f"https://github.com/{repo}/settings/rules/{ruleset_id}"
                if isinstance(ruleset_id, int)
                else f"https://github.com/{repo}/settings/rules"
            )
            blockers.append(
                f"merge_gate: FAILED {source} requires {approvals} "
                "approving review(s) the configured merge identity cannot "
                "supply (self-approval is forbidden); repair: add a different "
                "approving reviewer or change the ruleset at "
                f"{location}; after the action Orbi retries the merge"
            )

    if classic_unreadable:
        blockers.append(
            "merge_gate: UNKNOWN classic protection returned 404 for a "
            "protected branch; grant the token repository administration "
            "permission"
        )

    if blockers:
        return blockers
    return [f"merge_gate: PASS repo={repo} branch={branch} protection readable"]
