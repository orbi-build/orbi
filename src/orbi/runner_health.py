"""Runner self-health check.

The two 2026-09-04 incidents (#246: three identical delivery failures on one
Issue, #262: the service crash loop) were both found by humans reading the
journal in real time. This module gives the Runner a lightweight, active
self-check that runs at every tick start (a pure bypass — a check failure
never fails the delivery):

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

Actions are tiered: a structured `health_degraded` journal line on every
degraded tick (the repeated-failure check emits it once per streak — its
dedup guard skips the line too); a
comment on the affected Issue for repeated failures (deduped via the state
file — never one comment per tick); one deduplicated bug Issue for crash
loops and stale pickups, `ai-ready` attached only for bug-kind crash loops
(the #106 body-marker mechanism).

State lives in ONE lightweight JSON file in the existing state dir
(`repo_dir/.orbi/health.json`) — no daemon, no database, no new dependency.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from orbi.delivery_labels import READY_LABEL
from orbi import scheduler
from orbi.journal import RunIdFilter, event
from orbi.progress import format_status_comment, run_marker

if TYPE_CHECKING:
    # Annotation-only: `orbi.runner` imports this module at runtime.
    from orbi.runner import RunnerConfig

LOGGER = logging.getLogger("orbi.health")

LOGGER.addFilter(RunIdFilter())

STATE_FILENAME = "health.json"
CRASH_WINDOW_MINUTES = 60
CRASH_THRESHOLD = 3
REPEAT_FAILURE_THRESHOLD = 3
STALE_PICKUP_SECONDS = 24 * 60 * 60
RECENT_RUNS_KEEP = 50
GH_TIMEOUT_SECONDS = 60
HEALTH_MARKER_PREFIX = "orbi-health-fingerprint:"

# Two real systemd crash line shapes (verified against the live journal):
#   systemd[1015]: orbi@1.service: Main process exited, code=exited, status=1/FAILURE
#   systemd[1015]: orbi@1.service: Failed with result 'exit-code'.
# A single crash usually emits BOTH — the exit line and the result line
# land on the same or an adjacent second — and an ExecStartPre failure
# (dirty checkout, broken CLI install, SSH transport down: the Runner
# never starts at all) emits ONLY the result line. Both shapes must count,
# and the pair must count ONCE (a raw line count would halve the crash
# threshold — that dedupe lives in count_crashes, keyed on unit+clock).
# Exclusions are by STATUS, never by code class:
# - status=0/SUCCESS is the healthy timer-tick exit (a timer-driven
#   Type=simple service exits cleanly every run — counting those would
#   fire the crash_loop alert on every healthy deployment);
# - status=15/TERM is a systemd/human stop (`orbi install-units` never
#   stops a running Runner, so a TERM is an external action);
# everything else counts, including code=killed status=9/KILL: the OOM
# killer and TimeoutStopSec land there, and an OOM crash loop must alert.
# The `.service:` prefix requirement keeps the count conservative: the
# Runner's own journal lines can echo the words "Main process exited" when
# logging a command, and those must never count as a crash.
CRASH_EXIT_RE = re.compile(
    r"\.service: Main process exited, code=(?:dumped|killed|exited), "
    r"status=(?!0/|15/TERM)\d+/"
)
CRASH_FAIL_RESULT_RE = re.compile(r"\.service: Failed with result")

# journalctl --user default ("short") line clock: "Sep 04 11:41:02 …" —
# monotonic within the bounded window EXCEPT at month boundaries (the
# naive (month*31+day) encoding jumps a day or three when a 30/28-day
# month ends), so a paired result line straddling e.g. Sep 30 -> Oct 1
# can be counted as a fresh crash; the direction is over-counting, never
# a missed crash. Within a month it tells the paired result line of the
# same crash (same or adjacent second) from a fresh crash.
_JOURNAL_CLOCK_RE = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})\s+(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\b"
)
_UNIT_ON_LINE_RE = re.compile(r"(?P<unit>[\w.-]+@\d+\.service): ")
_MONTH_INDEX = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
# One crash emits its exit line and its result line at most this far apart
# (same second in practice; +1s covers a second-boundary straddle).
CRASH_PAIR_WINDOW_SECONDS = 1


def _line_clock_seconds(line: str) -> int | None:
    """Monotonic-ish seconds for a journalctl short-format line (None when
    the clock cannot be parsed — the caller then counts conservatively)."""
    m = _JOURNAL_CLOCK_RE.match(line)
    if not m:
        return None
    return (
        (_MONTH_INDEX[m.group("mon")] * 31 + int(m.group("day"))) * 86400
        + int(m.group("h")) * 3600 + int(m.group("m")) * 60
        + int(m.group("s"))
    )

# Volatile tokens stripped before fingerprinting: 8-40 hex runs (run ids,
# SHAs), ISO-ish timestamps, and duration shapes ("30m", "6s", "1h5m",
# "200ms" — the format_duration output the model_wait / idle-recovery
# failures embed; the duration drifts by one poll interval for the same
# pit). Only errors whose normalized text is IDENTICAL share a
# fingerprint — the conservative "same pit" rule.
VOLATILE_TOKEN_RE = re.compile(
    r"\b[0-9a-f]{8,40}\b"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"|\b(?:\d+(?:ms|[smh]))+(?![0-9a-z])",
)

# Fail-fast validation errors a HUMAN must fix in the config:
# there is nothing for any agent to implement until a human edits the
# config. These are the exact messages the Runner raises at config load
# (`_pi_provider_api_key_finding`, `load_config`) and the milestone errors
# `advance_active_milestone_on_idle` raises for a misconfigured
# `active_milestone`; a crash loop whose journal carries one
# of them is a deployment-config problem, not an orbi bug.
CONFIG_CAUSE_RE = re.compile(
    r"API key for provider"
    r"|must be a non-empty"
    r"|must be a boolean"
    r"|must be a positive integer"
    r"|is not defined for provider"
    r"|active_milestone_missing"
    r"|ambiguous exact-title match",
)


def health_state_path(repo_dir: Path) -> Path:
    """Return the health state file of one configured repo."""
    return repo_dir / ".orbi" / STATE_FILENAME


def fresh_state() -> dict:
    return {"runs": [], "last_pickup_ts": None, "alerted": []}


def load_health_state(path: Path) -> dict:
    """Load the health state; a missing or corrupt file is a fresh state.

    The health check is a bypass: corrupt observability state
    must never fail the delivery — it is logged and replaced with fresh
    state on the next save.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # No state file yet: the normal first-tick case, not an error.
        return fresh_state()
    except (OSError, ValueError):
        event("health_state_unreadable", level=logging.WARNING, path=path)
        return fresh_state()
    if not isinstance(data, dict):
        event("health_state_unreadable", level=logging.WARNING, path=path)
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


HEALTH_LOCK_TIMEOUT_S = 5.0


def _health_lock_path(state_path: Path) -> Path:
    return state_path.parent / (state_path.name + ".lock")


def _acquire_health_lock(state_path: Path, *,
                         blocking: bool) -> int | None:
    """Take the cross-instance health-state lock; None when unavailable.

    Health.json is the one state file two runner instances
    both read and write with no other synchronization — their
    load..save spans interleave and the last writer rolls the other's
    updates back. This is a LEAF lock: while holding it the code never
    acquires another lock (the check runs before slot acquisition and
    gives up on a busy lock; the recorders run inside a delivery but
    only do plain file I/O), so no ordering hazard exists with the
    slot or base-sync flocks even though the recorders may take this
    lock while holding the slot. The returned fd owns the flock —
    closing it releases.

    `blocking=False` bounds the wait at HEALTH_LOCK_TIMEOUT_S and gives
    up for the tick: the check is a documented pure bypass, so a busy
    lock skips this tick's check with the same semantics as a check
    failure.
    """
    lock_path = _health_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    if blocking:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    deadline = time.monotonic() + HEALTH_LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def save_health_state(path: Path, state: dict) -> None:
    """Write the health state atomically (tmp file + rename).

    The tmp name is pid-scoped. The previous shared
    `health.tmp` meant two instances saving concurrently wrote the same
    tmp inode — interleaved truncate/write produced torn JSON that the
    read side then treated as fresh state (a silent reset of every
    alert/counter). Unique tmp names make each writer's rename
    all-or-nothing regardless of interleaving.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
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
    """Append one run attempt to the bounded health history.

    The load..save span is a cross-instance RMW — the
    blocking lock serializes it (the critical section is pure file
    work, milliseconds).
    """
    lock_fd = _acquire_health_lock(state_path, blocking=True)
    try:
        state = load_health_state(state_path)
        if outcome != "failed":
            # A non-failure outcome breaks the repeated-failure streak. The
            # issue's alert history must turn a fresh page with it: a later
            # NEW streak on the same issue alerts again instead of staying
            # silent forever behind the key recorded for the old streak.
            prefix = f"{repo}#{issue}:"
            state["alerted"] = [
                key for key in state["alerted"]
                if not key.startswith(prefix)
            ]
        state["runs"].append({
            "repo": repo, "issue": issue, "run_id": run_id,
            "outcome": outcome, "fingerprint": fingerprint,
            "ts": time.time(),
        })
        state["runs"] = state["runs"][-RECENT_RUNS_KEEP:]
        save_health_state(state_path, state)
    finally:
        os.close(lock_fd)


def record_pickup(repo_dir: Path) -> None:
    """Record a successful ticket pickup (resets the stale-pickup clock).

    Locked like :func:`record_run_attempt` — an unlocked RMW
    here is exactly the mechanism behind the false `stale_pickup` alarm
    (one instance's check saves back an old pickup timestamp over the
    other instance's fresh one).
    """
    path = health_state_path(repo_dir)
    lock_fd = _acquire_health_lock(path, blocking=True)
    try:
        state = load_health_state(path)
        state["last_pickup_ts"] = time.time()
        save_health_state(path, state)
    finally:
        os.close(lock_fd)


def crash_journal_lines(
    run_command, since_minutes: int = CRASH_WINDOW_MINUTES,
    unit_name: str | None = None, *, max_concurrency: int,
    repo_dir: Path | None = None,
) -> list[str]:
    """Return the Runner output lines for every service instance in the
    window.

    Delegates to the scheduler layer (Issue #849): systemd queries the
    journal per service instance of THIS deployment through the
    configured `max_concurrency` (Issue #827 — the instance set follows
    the capacity, not a hardcoded count; a `unit_name` deployment
    installs `orbi-<name>@N.service`, so the query must follow the same
    naming — `None` keeps the default `orbi@N.service`). On launchd the
    lines are the tails of the per-instance log files the agent plist
    renders. The lines are the raw scene the crash-loop alert needs.
    NOTE: the crash line patterns below are systemd journal shapes, so
    on macOS the crash-loop check stays inert by design.
    """
    sched = scheduler.detect()
    return sched.journal_lines(
        run_command, repo_dir, unit_name,
        max_concurrency=max_concurrency, since_minutes=since_minutes,
    )


def count_crashes(
    run_command, since_minutes: int = CRASH_WINDOW_MINUTES,
    unit_name: str | None = None, *, max_concurrency: int,
    repo_dir: Path | None = None,
) -> int:
    """Count crash EVENTS in the window from the systemd journal.

    Scans every service instance @1..max_concurrency of THIS deployment
    (Issue #827). Both crash shapes count (an abnormal main-process exit,
    and a "Failed with result" line — the only shape an ExecStartPre
    failure leaves). One crash usually emits both lines on the same or an
    adjacent second: per unit, a matched line within
    CRASH_PAIR_WINDOW_SECONDS of the previous matched line is the pair
    half of that crash, not a new event. A matched line whose clock or
    unit cannot be parsed counts as-is (conservative: never miss a real
    crash to dedupe).
    """
    last_matched: dict[str, int] = {}
    events = 0
    for line in crash_journal_lines(
        run_command, since_minutes, unit_name=unit_name,
        max_concurrency=max_concurrency, repo_dir=repo_dir,
    ):
        if not (CRASH_EXIT_RE.search(line) or CRASH_FAIL_RESULT_RE.search(line)):
            continue
        unit_m = _UNIT_ON_LINE_RE.search(line)
        clock = _line_clock_seconds(line)
        if unit_m is None or clock is None:
            events += 1
            continue
        unit = unit_m.group("unit")
        prev = last_matched.get(unit)
        last_matched[unit] = clock
        if prev is not None and clock - prev <= CRASH_PAIR_WINDOW_SECONDS:
            continue
        events += 1
    return events


def classify_crash(journal_lines: list[str]) -> tuple[str, str]:
    """Classify a crash loop as deployment-config or orbi-bug.

    Returns ``(kind, reason_line)``. ``kind`` is ``"config"`` when the
    journal carries a fail-fast validation error (CONFIG_CAUSE_RE) — a human
    must edit the config, no agent can fix it; otherwise ``"bug"`` (an
    orbi-internal defect orbi's own agent can fix). ``reason_line`` is the
    most recent journal line that names the cause (empty when unknown), so
    the receiving agent has the scene.
    """
    for line in reversed(journal_lines):
        if CONFIG_CAUSE_RE.search(line):
            return "config", line
    for line in reversed(journal_lines):
        if "ERROR" in line.upper() or "Traceback" in line:
            return "bug", line
    return "bug", ""


def crash_reason_fingerprint(reason_line: str) -> str:
    """Return the stable 16-hex fingerprint of one crash reason line.

    A per-crash identity for the alert body: the same fail-fast
    reason always fingerprints identically, so a recurring loop is
    recognizable at a glance. Empty reason -> empty fingerprint.
    """
    if not reason_line:
        return ""
    return hashlib.sha256(reason_line.encode("utf-8")).hexdigest()[:16]


def orbi_repo_from_origin_url(url: str) -> str | None:
    """Derive ``owner/repo`` from a git origin URL.

    Handles the two forms `git remote get-url origin` returns for the orbi
    checkout: SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo[.git]``). Anything else -> None
    (undeterminable; the caller logs and skips rather than guess).
    """
    url = url.strip()
    if not url:
        return None
    match = re.match(r"^[^@]+@[^:]+:(.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    match = re.match(r"^https?://[^/]+/(.+?)(?:\.git)?/?$", url)
    if match:
        return match.group(1)
    return None


def orbi_repo_from_deploy_home(deploy_home: Path, run_command) -> str | None:
    """Derive the orbi repo from the deploy home's git origin.

    The deploy home is the orbi source checkout; its origin is
    the orbi repo the Runner-self health alerts belong in.
    A failed query or unparseable URL returns None (the caller falls back to
    the configured override or skips — never guesses a repo).
    """
    try:
        output = run_command(
            ["git", "-C", str(deploy_home), "remote", "get-url", "origin"],
            timeout=30,
        )
    except Exception:
        return None
    return orbi_repo_from_origin_url(output)


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
    repo: str, check: str, detail: str, *, run_command, dispatchable=True,
) -> str | None:
    """Create (or find) one deduplicated bug Issue for a health alarm.

    Reuses the #106 mechanism: the stable marker is written into the body
    and `gh issue list --search 'in:body ...'` finds an existing Issue, so a
    recurring alarm never creates a second Issue.

    ``dispatchable``: True -> the normal `bug`+`ai-ready`
    dispatch (an orbi bug orbi's own agent can fix); False -> a
    non-dispatchable alert (`bug` only, NO `ai-ready`) for a deployment
    config problem only a human can fix — the Runner never picks it up.
    """
    marker = health_marker(check)
    raw = run_command(
        [
            "gh", "issue", "list", "--repo", repo, "--state", "all",
            "--search", f'in:body "{marker}"', "--json", "number,url",
            "--limit", "1",
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    try:
        existing = json.loads(raw) if raw.strip() else []
    except ValueError:
        existing = []
    if isinstance(existing, list) and existing:
        return existing[0].get("url")
    closing = (
        "该 Issue 由正常 `ai-ready` → PR → review → merge 流程处理。"
        if dispatchable else
        "该告警为部署配置问题，需人工修改配置后重启服务，不进入 `ai-ready` 流程。"
    )
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
        closing,
    ])
    command = [
        "gh", "issue", "create",
        "--repo", repo, "--title", f"Runner 健康巡检告警: {check}",
        "--body", body, "--label", "bug",
    ]
    if dispatchable:
        command += ["--label", "ai-ready"]
    run_command(command, timeout=GH_TIMEOUT_SECONDS)
    return None


def repeat_failure_comment(finding: dict) -> str:
    """Build the Issue comment for one repeated-failure finding.

    Carries the latest failing run's marker (`<!-- orbi:run=<run_id> -->`
    plus the visible `run_id=` field) per the run-correlation contract.
    """
    run_id = finding["run_ids"][0]
    return "\n".join([
        run_marker(run_id),
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


def run_health_check(config: RunnerConfig, *, run_command) -> list[str]:
    """Run the tick-start self-health check. Returns the fired check names.

    The state read-modify-write is serialized across
    instances with the leaf health lock, acquired non-blocking with a
    bounded wait — the check is a documented pure bypass, so a lock
    that stays busy skips this tick's check with the same semantics as
    a check failure (callers log it and never fail the delivery).

    Pure bypass: callers wrap this in try/except — a check failure logs and
    never fails the delivery. The state file is saved even when a check
    raises (the alerted-dedup set must survive partial runs).
    """
    state_path = health_state_path(config.repo_dir)
    lock_fd = _acquire_health_lock(state_path, blocking=False)
    if lock_fd is None:
        event("health_check_skipped", reason="health_lock_busy")
        return []
    try:
        return _run_health_check_locked(config, run_command=run_command)
    finally:
        os.close(lock_fd)


def _run_health_check_locked(config: RunnerConfig, *, run_command) -> list[str]:
    alerts: list[str] = []
    state_path = health_state_path(config.repo_dir)
    state = load_health_state(state_path)
    try:
        # Routing: Runner-self health alerts belong in the orbi
        # repo, never the delivery repo by default. The configured
        # `health_alert_repo` override wins (fork/private deployments);
        # otherwise the orbi repo is derived from the deploy home's git
        # origin. Undeterminable -> log and skip (bypass: never guess a
        # repo, never file the alert in the delivery repo).
        alert_repo = config.health_alert_repo
        if not alert_repo:
            alert_repo = orbi_repo_from_deploy_home(
                config.deploy_home, run_command,
            )
        # 1. Crash loop (#262 scene: repeated service exits, including the
        #    unit self-heal death loop — each iteration exits non-zero).
        # Watch THIS deployment's units (unit_name-aware).
        unit_name = config.unit_name
        crashes = count_crashes(
            run_command, unit_name=unit_name,
            max_concurrency=config.max_concurrency,
            repo_dir=config.deploy_home,
        )
        if crashes >= CRASH_THRESHOLD:
            journal_lines = crash_journal_lines(
                run_command, unit_name=unit_name,
                max_concurrency=config.max_concurrency,
                repo_dir=config.deploy_home,
            )
            kind, reason_line = classify_crash(journal_lines)
            event(
                "health_degraded", check="crash_loop", crashes=crashes,
                window_minutes=CRASH_WINDOW_MINUTES, kind=kind,
            )
            if not alert_repo:
                event(
                    "health_alert_repo_undetermined",
                    level=logging.WARNING, check="crash_loop",
                    crashes=crashes,
                )
            else:
                detail = (
                    f"service crashed {crashes} times in the last "
                    f"{CRASH_WINDOW_MINUTES} minutes"
                )
                if reason_line:
                    detail += (
                        f"\ncrash reason (journal): {reason_line}\n"
                        f"crash reason fingerprint: "
                        f"{crash_reason_fingerprint(reason_line)}"
                    )
                create_health_issue(
                    alert_repo, "crash_loop", detail,
                    run_command=run_command,
                    dispatchable=(kind == "bug"),
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
            event(
                "health_degraded", check="repeated_failure",
                issue=f"{finding['repo']}#{finding['issue']}",
                count=finding["count"],
                fingerprint=finding["fingerprint"],
            )
            run_command(
                [
                    "gh", "issue", "comment", str(finding["issue"]),
                    "--repo", finding["repo"],
                    "--body", format_status_comment(
                        repeat_failure_comment(finding),
                    ),
                ],
                timeout=GH_TIMEOUT_SECONDS,
            )
            alerts.append(f"repeated_failure:{finding['repo']}#{finding['issue']}")
        # 3. Stale pickup: system stuck vs queue idle.
        if stale_pickup_finding(state):
            ready_raw = run_command(
                [
                    "gh", "issue", "list",
                    "--repo", config.source_repos[0], "--state", "open",
                    "--label", READY_LABEL, "--json", "number",
                    "--limit", "1",
                ],
                timeout=GH_TIMEOUT_SECONDS,
            )
            try:
                ready = json.loads(ready_raw) if ready_raw.strip() else []
            except ValueError:
                ready = []
            if not (isinstance(ready, list) and ready):
                event(
                    "health_check_queue_empty",
                    since_pickup_seconds=int(
                        time.time() - state["last_pickup_ts"],
                    ),
                )
            else:
                event(
                    "health_degraded", check="stale_pickup",
                    ready_issues=len(ready),
                    since_pickup_seconds=int(
                        time.time() - state["last_pickup_ts"],
                    ),
                )
                if not alert_repo:
                    event(
                        "health_alert_repo_undetermined",
                        level=logging.WARNING, check="stale_pickup",
                    )
                else:
                    create_health_issue(
                        alert_repo, "stale_pickup",
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
