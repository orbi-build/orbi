"""Classify and render one PR status check rollup (Issue #1473).

The claim scan's pending skip, the runner's pre-review CI gate and the
merge gate all read the same `statusCheckRollup` contexts and must reach
the same conclusion. Since Issue #1534 the read is `orbi.pr_status`'s
`gh api graphql` query (never `gh pr view`'s App-locked
`checkSuite.workflowRun` field). These pure helpers are the shared,
cycle-free home (the scan cannot import `runner.py` back): `github.py`
stays the I/O leaf and no domain module owns a private copy.
"""
from __future__ import annotations


def _render_check(entry: dict) -> str:
    """Render one classified check entry for humans, from the data.

    The rendered string is presentation only — never parsed back
    (Issue #906): callers that need the name/status read the entry.
    """
    if entry["status"] == "COMPLETED":
        return (f"check '{entry['name']}' is "
                f"{entry['status']}/{entry['conclusion']}")
    return f"check '{entry['name']}' is {entry['status'] or 'UNKNOWN'}"


def _classify_rollup(rollup: list) -> tuple[list[dict], list[dict]]:
    """Split one PR status check rollup into (pending, failed) evidence.

    Returns STRUCTURED entries carrying `name`, `status` and `conclusion`
    (Issue #906) — callers read the fields; `_render_check` derives the
    human wording from them. A check is pending while its status is
    anything but a final one (GitHub recomputes mergeability and
    registers new CheckRuns asynchronously); a completed check with a
    non-passing conclusion — or a legacy status-context FAILURE/ERROR —
    is failed. Pure: the claim scan's pending skip, the pre-review CI gate
    and the merge gate classify the same rollup the same way.
    """
    pending: list[dict] = []
    failed: list[dict] = []
    for check in rollup:
        status = str(check.get("status", check.get("state", "")) or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        name = check.get("name", check.get("context", "check"))
        entry = {"name": name, "status": status, "conclusion": conclusion}
        if status not in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR"):
            pending.append(entry)
        elif status == "COMPLETED" and conclusion not in (
            "SUCCESS", "NEUTRAL", "SKIPPED",
        ):
            failed.append(entry)
        elif status in ("FAILURE", "ERROR"):
            failed.append(entry)
    return pending, failed
