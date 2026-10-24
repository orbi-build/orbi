"""Classify and render one list of CI checks (Issues #1473, #1889).

The claim scan's pending skip, the runner's pre-review CI gate, the merge
gate and the release gate all read the same CI data and must reach the
same conclusion. GitHub reports it in two shapes — a PR status check
rollup (`COMPLETED`/`SUCCESS`, a legacy status context carrying
`state`) and a commit's check runs (`completed`/`success`) — and
`classify_checks` is the ONE pending/failed/passed classifier for both,
so the gates cannot drift apart. These pure helpers are the shared,
cycle-free home (the scan cannot import `runner.py` back): `github.py`
stays the I/O leaf and no domain module owns a private copy.
"""
from __future__ import annotations

# A status a check reports once it has concluded (the rollup's CheckRun
# `COMPLETED`, a legacy status context's `SUCCESS`/`FAILURE`/`ERROR`);
# anything else is still running. GitHub recomputes mergeability and
# registers new CheckRuns asynchronously.
_FINAL_STATUSES = ("COMPLETED", "SUCCESS", "FAILURE", "ERROR")
# The conclusions that do not fail the build.
_PASSING_CONCLUSIONS = ("SUCCESS", "NEUTRAL", "SKIPPED")


def _render_check(entry: dict) -> str:
    """Render one classified check entry for humans, from the data.

    The rendered string is presentation only — never parsed back
    (Issue #906): callers that need the name/status read the entry.
    """
    if str(entry["status"]).upper() == "COMPLETED":
        return (f"check '{entry['name']}' is "
                f"{entry['status']}/{entry['conclusion']}")
    return f"check '{entry['name']}' is {entry['status'] or 'UNKNOWN'}"


def classify_checks(
    entries: list,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split CI checks into (pending, failed, passed) evidence.

    One classifier for both shapes GitHub reports (Issue #1889): a PR
    status check rollup and a commit's check runs. The status/conclusion
    comparison is case-insensitive, so the merge gate's uppercase
    `COMPLETED/SUCCESS` and the release gate's lowercase
    `completed/success` reach the same bucket and the two gates cannot
    drift apart. The entry keeps the fields exactly as reported
    (`name`, `status`, `conclusion`; Issue #906 — callers read them and
    `_render_check` derives the human wording from them), so each gate
    renders its own data verbatim.

    A check is pending while its status is anything but a final one
    (GitHub recomputes mergeability and registers new CheckRuns
    asynchronously); a completed check with a non-passing conclusion —
    or a legacy status-context FAILURE/ERROR — is failed; every other
    concluded check has passed. Pure: the claim scan's pending skip, the
    pre-review CI gate, the merge gate and the release gate classify the
    same check the same way.
    """
    pending: list[dict] = []
    failed: list[dict] = []
    passed: list[dict] = []
    for check in entries:
        status = str(check.get("status", check.get("state", "")) or "")
        entry = {
            "name": check.get("name", check.get("context", "check")),
            "status": status,
            "conclusion": check.get("conclusion"),
        }
        final_status = status.upper()
        conclusion = str(entry["conclusion"] or "").upper()
        if final_status not in _FINAL_STATUSES:
            pending.append(entry)
        elif final_status == "COMPLETED":
            if conclusion in _PASSING_CONCLUSIONS:
                passed.append(entry)
            else:
                failed.append(entry)
        elif final_status in ("FAILURE", "ERROR"):
            failed.append(entry)
        else:
            passed.append(entry)
    return pending, failed, passed


def test_status(rollup: list | None) -> str:
    """The PR head's test status, from the same status check rollup the
    gates classify (Issue #1613).

    `passed` when every check concluded successfully, `failed: <name,
    ...>` when one or more concluded otherwise, `pending` while any check
    has not concluded, and `-` when there is no PR or the head has no
    checks. A known failure is decisive and read before pending: the run
    cannot pass while a check is red.
    """
    if not rollup:
        return "-"
    pending, failed, _passed = classify_checks(rollup)
    if failed:
        return "failed: " + ", ".join(entry["name"] for entry in failed)
    if pending:
        return "pending"
    return "passed"
