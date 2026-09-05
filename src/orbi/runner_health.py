"""Runner self-health check (Issue #266).

The two 2026-09-04 incidents (#246: three identical delivery failures on one
Issue, #262: the service crash loop) were both found by humans reading the
journal in real time. This module gives the Runner a lightweight, active
self-check that runs at every tick start (a pure bypass — a check failure
never fails the delivery, Issue #79 semantics):

- crash loop: the service unit crashed >= CRASH_THRESHOLD times within the
  last CRASH_WINDOW_MINUTES (counted from the systemd journal — each crash
  loop iteration, including the unit self-heal death loop, exits the service
  non-zero and lands a `Main process exited` / `Failed with result` line);
- repeated same-fingerprint run failures: the same Issue failed >=
  REPEAT_FAILURE_THRESHOLD consecutive runs with a highly similar failure
  fingerprint (exception class + first line of the message, volatile tokens
  stripped; exact match required — normal multi-round review/fix cycles fail
  differently and never match);
- stale pickup: the last successful ticket pickup is older than
  STALE_PICKUP_SECONDS while at least one `ai-ready` Issue exists (system
  stuck). An empty ready queue is idle, not a failure — no alarm.

Actions are tiered: a structured `health_degraded` journal line always; a
comment on the affected Issue for repeated failures (deduped via the state
file — never one comment per tick); one deduplicated bug+ai-ready Issue for
crash loop / stale pickup (the #106 body-marker mechanism).

State lives in ONE lightweight JSON file in the existing state dir
(`repo_dir/.orbi/health.json`) — no daemon, no database, no new dependency.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from orbi.delivery_labels import READY_LABEL
from orbi.systemd_deploy import SERVICE_INSTANCES

LOGGER = logging.getLogger("orbi.health")

STATE_FILENAME = "health.json"
CRASH_WINDOW_MINUTES = 60
CRASH_THRESHOLD = 3
REPEAT_FAILURE_THRESHOLD = 3
STALE_PICKUP_SECONDS = 24 * 60 * 60
RECENT_RUNS_KEEP = 50
GH_TIMEOUT_SECONDS = 60
HEALTH_MARKER_PREFIX = "orbi-health-fingerprint:"

# The real systemd crash lines (verified against the live journal):
#   systemd[1015]: orbi@1.service: Main process exited, code=exited, ...
#   systemd[1015]: orbi@1.service: Failed with result 'exit-code'.
# The `.service:` prefix requirement keeps the count conservative: the
# Runner's own journal lines can echo the words "Main process exited" when
# logging a command, and those must never count as a crash.
CRASH_EXIT_RE = re.compile(r"\.service: (Main process exited, code=|Failed with result)")

# Volatile tokens stripped before fingerprinting: 8-40 hex runs (run ids,
# SHAs) and ISO-ish timestamps. Only errors whose normalized text is IDENTICAL
# share a fingerprint — the conservative "same pit" rule of Issue #266.
VOLATILE_TOKEN_RE = re.compile(
    r"\b[0-9a-f]{8,40}\b|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",
)


def health_state_path(repo_dir: Path) -> Path:
    """Return the health state file of one configured repo."""
    return repo_dir / ".orbi" / STATE_FILENAME


def fresh_state() -> dict:
    return {"runs": [], "last_pickup_ts": None, "alerted": []}


def load_health_state(path: Path) -> dict:
    """Load the health state; a missing or corrupt file is a fresh state.

    The health check is a bypass (Issue #79): corrupt observability state
    must never fail the delivery — it is logged and replaced with fresh
    state on the next save.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # No state file yet: the normal first-tick case, not an error.
        return fresh_state()
    except (OSError, ValueError):
        LOGGER.warning("health_state_unreadable path=%s", path)
        return fresh_state()
    if not isinstance(data, dict):
        LOGGER.warning("health_state_unreadable path=%s", path)
        return fresh_state()
    state = fresh_state()
    runs = data.get("runs")
    if isinstance(runs, list):
        state["runs"] = runs
    pickup = data.get("last_pickup_ts")
    if isinstance(pickup, (int, float)) and not isinstance(pickup, bool):
        state["last_pickup_ts"] = pickup
    alerted = data.get("alerted")
    if isinstance(alerted, list) and all(
        isinstance(item, str) for item in alerted
    ):
        state["alerted"] = alerted
    return state


def save_health_state(path: Path, state: dict) -> None:
    """Write the health state atomically (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def failure_fingerprint(exc: BaseException) -> str:
    """Return the conservative failure fingerprint of one run failure.

    Exception class + first non-empty line of the message, with volatile
    tokens (hex run ids/SHAs, timestamps) replaced. Two failures share a
    fingerprint only when their normalized text is identical.
    """
    detail = str(exc)
    first_line = next(
        (line for line in detail.splitlines() if line.strip()), "",
    )
    normalized = VOLATILE_TOKEN_RE.sub("<x>", first_line)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    scene = f"{type(exc).__name__}|{normalized}"
    return hashlib.sha256(scene.encode("utf-8")).hexdigest()[:16]


def record_run_attempt(
    state_path: Path, *, repo: str, issue: int, run_id: str,
    outcome: str, fingerprint: str,
) -> None:
    """Append one run attempt to the bounded health history."""
    state = load_health_state(state_path)
    state["runs"].append({
        "repo": repo, "issue": issue, "run_id": run_id,
        "outcome": outcome, "fingerprint": fingerprint,
        "ts": time.time(),
    })
    state["runs"] = state["runs"][-RECENT_RUNS_KEEP:]
    save_health_state(state_path, state)


def record_pickup(repo_dir: Path) -> None:
    """Record a successful ticket pickup (resets the stale-pickup clock)."""
    path = health_state_path(repo_dir)
    state = load_health_state(path)
    state["last_pickup_ts"] = time.time()
    save_health_state(path, state)


def count_crashes(run_command, since_minutes: int = CRASH_WINDOW_MINUTES) -> int:
    """Count service crashes in the window from the systemd journal.

    One bounded `journalctl` query per service instance (the verified shape
    from `orbi doctor`); counts only the real systemd exit lines (see
    CRASH_EXIT_RE).
    """
    total = 0
    for unit in SERVICE_INSTANCES:
        output = run_command([
            "timeout", "30", "journalctl", "--user", "-u", unit,
            "--since", f"-{since_minutes}min", "--no-pager", "-q",
        ])
        total += sum(
            1 for line in output.splitlines() if CRASH_EXIT_RE.search(line)
        )
    return total


def repeated_failure_findings(state: dict) -> list[dict]:
    """Find consecutive same-fingerprint failure streaks per (repo, issue).

    Walks the history newest-first per Issue: a failed run starts or extends
    a streak only when its fingerprint matches; a different fingerprint
    restarts the streak; any non-failure outcome breaks it. A streak of >=
    REPEAT_FAILURE_THRESHOLD is one finding.
    """
    findings: list[dict] = []
    issues = sorted({
        (entry["repo"], entry["issue"])
        for entry in state["runs"]
        if isinstance(entry, dict)
    })
    for repo, issue in issues:
        fingerprint: str | None = None
        count = 0
        run_ids: list[str] = []
        for entry in reversed(state["runs"]):
            if entry.get("repo") != repo or entry.get("issue") != issue:
                continue
            if entry.get("outcome") == "failed":
                if entry.get("fingerprint") == fingerprint:
                    count += 1
                    run_ids.append(entry["run_id"])
                else:
                    fingerprint = entry.get("fingerprint")
                    count = 1
                    run_ids = [entry["run_id"]]
            else:
                break
        if count >= REPEAT_FAILURE_THRESHOLD:
            findings.append({
                "repo": repo, "issue": issue,
                "fingerprint": fingerprint, "count": count,
                "run_ids": run_ids,
            })
    return findings


def stale_pickup_finding(state: dict) -> bool:
    """True when the last successful pickup is older than the threshold."""
    pickup = state.get("last_pickup_ts")
    if not isinstance(pickup, (int, float)) or isinstance(pickup, bool):
        return False
    return time.time() - pickup > STALE_PICKUP_SECONDS


def health_marker(check: str) -> str:
    """Return the stable dedup marker for one health check type."""
    digest = hashlib.sha256(check.encode("utf-8")).hexdigest()[:16]
    return f"{HEALTH_MARKER_PREFIX}{digest}"


def create_health_issue(
    repo: str, check: str, detail: str, *, run_command,
) -> str | None:
    """Create (or find) one deduplicated bug Issue for a health alarm.

    Reuses the #106 mechanism: the stable marker is written into the body
    and `gh issue list --search 'in:body ...'` finds an existing Issue, so a
    recurring alarm never creates a second Issue.
    """
    marker = health_marker(check)
    raw = run_command([
        "timeout", str(GH_TIMEOUT_SECONDS), "gh", "issue", "list",
        "--repo", repo, "--state", "all",
        "--search", f'in:body "{marker}"', "--json", "number,url",
        "--limit", "1",
    ])
    try:
        existing = json.loads(raw) if raw.strip() else []
    except ValueError:
        existing = []
    if isinstance(existing, list) and existing:
        return existing[0].get("url")
    body = "\n".join([
        "## Runner 健康巡检告警",
        "",
        f"- check: `{check}`",
        f"- {marker}",
        "",
        "## Evidence",
        "",
        "```text",
        detail,
        "```",
        "",
        "该 Issue 由正常 `ai-ready` → PR → review → merge 流程处理。",
    ])
    run_command([
        "timeout", str(GH_TIMEOUT_SECONDS), "gh", "issue", "create",
        "--repo", repo, "--title", f"Runner 健康巡检告警: {check}",
        "--body", body, "--label", "bug", "--label", "ai-ready",
    ])
    return None


def repeat_failure_comment(finding: dict) -> str:
    """Build the Issue comment for one repeated-failure finding.

    Carries the latest failing run's marker (`<!-- orbi:run=<run_id> -->`
    plus the visible `run_id=` field) per the run-correlation contract.
    """
    run_id = finding["run_ids"][0]
    return "\n".join([
        f"<!-- orbi:run={run_id} -->",
        (
            f"Orbi health check: issue #{finding['issue']} failed "
            f"{finding['count']} consecutive runs with the same failure "
            f"fingerprint `{finding['fingerprint']}` (run_id={run_id})."
        ),
        (
            "This looks like a repeating dead end, not normal review/fix "
            "rounds — escalating for attention."
        ),
        "",
        f"run_id={run_id}",
    ])


def run_health_check(config: dict, *, run_command) -> list[str]:
    """Run the tick-start self-health check. Returns the fired check names.

    Pure bypass: callers wrap this in try/except — a check failure logs and
    never fails the delivery. The state file is saved even when a check
    raises (the alerted-dedup set must survive partial runs).
    """
    alerts: list[str] = []
    state_path = health_state_path(config["repo_dir"])
    state = load_health_state(state_path)
    try:
        # 1. Crash loop (#262 scene: repeated service exits, including the
        #    unit self-heal death loop — each iteration exits non-zero).
        crashes = count_crashes(run_command)
        if crashes >= CRASH_THRESHOLD:
            LOGGER.info(
                "health_degraded check=crash_loop crashes=%s "
                "window_minutes=%s", crashes, CRASH_WINDOW_MINUTES,
            )
            create_health_issue(
                config["source_repos"][0], "crash_loop",
                (
                    f"service crashed {crashes} times in the last "
                    f"{CRASH_WINDOW_MINUTES} minutes"
                ),
                run_command=run_command,
            )
            alerts.append("crash_loop")
        # 2. Repeated same-fingerprint run failures (#246 scene).
        for finding in repeated_failure_findings(state):
            key = (
                f"{finding['repo']}#{finding['issue']}:"
                f"{finding['fingerprint']}"
            )
            if key in state["alerted"]:
                continue
            state["alerted"].append(key)
            LOGGER.info(
                "health_degraded check=repeated_failure issue=%s count=%s "
                "fingerprint=%s",
                finding["repo"], finding["issue"],
                finding["fingerprint"],
            )
            run_command([
                "timeout", str(GH_TIMEOUT_SECONDS), "gh", "issue",
                "comment", str(finding["issue"]), "--repo", finding["repo"],
                "--body", repeat_failure_comment(finding),
            ])
            alerts.append(f"repeated_failure:{finding['repo']}#{finding['issue']}")
        # 3. Stale pickup: system stuck vs queue idle.
        if stale_pickup_finding(state):
            ready_raw = run_command([
                "timeout", str(GH_TIMEOUT_SECONDS), "gh", "issue", "list",
                "--repo", config["source_repos"][0], "--state", "open",
                "--label", READY_LABEL, "--json", "number", "--limit", "1",
            ])
            try:
                ready = json.loads(ready_raw) if ready_raw.strip() else []
            except ValueError:
                ready = []
            if not (isinstance(ready, list) and ready):
                LOGGER.info(
                    "health_check_queue_empty since_pickup_seconds=%s",
                    int(time.time() - state["last_pickup_ts"]),
                )
            else:
                LOGGER.info(
                    "health_degraded check=stale_pickup ready_issues=%s "
                    "since_pickup_seconds=%s",
                    len(ready),
                    int(time.time() - state["last_pickup_ts"]),
                )
                create_health_issue(
                    config["source_repos"][0], "stale_pickup",
                    (
                        "no ticket pickup for "
                        f"{int(time.time() - state['last_pickup_ts'])} "
                        "seconds while the ai-ready queue is non-empty"
                    ),
                    run_command=run_command,
                )
                alerts.append("stale_pickup")
    finally:
        save_health_state(state_path, state)
    return alerts
