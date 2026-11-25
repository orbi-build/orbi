"""The merge-gate preflight reads: branch, classic protection, rules.

The I/O half of the merge-gate preflight. `orbi.github` keeps the public
name and hands in its own `gh` read seam (the retry and response-status
rule stays there); `orbi.merge_gate` owns the rule that turns these
payloads into blocker lines. This module was extracted from `orbi.github`
unchanged when the Issue #2394 Issue/PR/milestone call surface needed that
module's room under its size-ratchet ceiling.

GitHub uses the same classic-protection 404 for "not protected" and for
protection hidden from the token. The branch resource's documented
`protected` boolean distinguishes those cases before the detailed read; an
empty branch-rules response alone is not sufficient (Issue #1174).
"""
from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from urllib.parse import quote

from orbi import gh_response
from orbi.journal import event
from orbi.merge_gate import classify_protection


def _is_ruleset_plan_limit(exc: subprocess.CalledProcessError) -> bool:
    """Whether a failed branch-rules read hit GitHub's plan limitation.

    GitHub answers the branch-rules endpoint of a private repository on
    a plan without rulesets with a documented 403, and its message is
    the only thing that separates it from a permission 403 (Issue
    #1361): the status alone cannot.
    """
    return "upgrade to github pro" in " ".join(
        str(part or "") for part in (exc.stderr, exc.stdout)
    ).lower()


def merge_gate_preflight(
    repo: str, branch: str, *, read: Callable[..., str],
) -> list[str]:
    """Classify protection visible to the configured GitHub token."""
    def api(path: str, *, timeout: int = 30,
            failure_log_level: int = logging.ERROR) -> object:
        """Read one merge-gate endpoint through the read-only gh seam."""
        suffix = f"/{path}" if path else ""
        return json.loads(read(
            ["gh", "api", f"repos/{repo}{suffix}"], timeout=timeout,
            failure_log_level=failure_log_level,
        ))

    encoded_branch = quote(branch, safe="")
    try:
        branch_info = api(f"branches/{encoded_branch}")
    except (subprocess.CalledProcessError, json.JSONDecodeError,
            TypeError, ValueError):
        return ["merge_gate: UNKNOWN cannot read target branch"]
    if (not isinstance(branch_info, dict)
            or not isinstance(branch_info.get("protected"), bool)):
        return ["merge_gate: UNKNOWN branch protection state is not a boolean"]

    classic: dict | None = None
    classic_unreadable = False
    if branch_info["protected"]:
        try:
            value = api(f"branches/{encoded_branch}/protection")
            if not isinstance(value, dict):
                return ["merge_gate: UNKNOWN protection response is not an object"]
            classic = value
        except subprocess.CalledProcessError as exc:
            if not gh_response.not_found(exc):
                return ["merge_gate: UNKNOWN cannot read branch protection; "
                        "requires repository administration permission"]
            classic_unreadable = True
        except (json.JSONDecodeError, TypeError, ValueError):
            return ["merge_gate: UNKNOWN cannot parse branch protection response"]

    try:
        rules = api(
            f"rules/branches/{encoded_branch}", failure_log_level=logging.DEBUG,
        )
    except subprocess.CalledProcessError as exc:
        if _is_ruleset_plan_limit(exc):
            # The plan cannot carry rulesets, so none can block this
            # merge and the classic result above stands (Issue #1361).
            event("merge_gate_rulesets_unavailable", reason="plan")
            rules = []
        else:
            # The read logged at DEBUG because a plan limit is expected
            # here; every other failure keeps its raw ERROR line.
            event(
                "command_failed", level=logging.ERROR,
                returncode=exc.returncode,
                stdout=(exc.stdout or "").rstrip(),
                stderr=(exc.stderr or "").rstrip(),
            )
            if gh_response.not_found(exc):
                return ["merge_gate: UNKNOWN protection is unreadable; "
                        "grant the token repository administration permission"]
            return ["merge_gate: UNKNOWN cannot read rulesets; "
                    "requires repository administration permission"]
    except (json.JSONDecodeError, TypeError, ValueError):
        return ["merge_gate: UNKNOWN cannot parse ruleset response"]
    if not isinstance(rules, list) or not all(
            isinstance(rule, dict) for rule in rules):
        return ["merge_gate: UNKNOWN ruleset response is not an object array"]

    return classify_protection(
        repo, branch, classic=classic, rules=rules,
        classic_unreadable=classic_unreadable,
    )
