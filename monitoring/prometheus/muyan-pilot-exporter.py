#!/usr/bin/env python3
"""Muyan Pilot Prometheus exporter (Issue #162).

Read-only bridge from the structured user systemd journal of the
`muyan-pilot@*` Runner services to Prometheus:

    systemd journal (user units)
          |  journalctl --user -u 'muyan-pilot@*' -o json
          v
    this exporter  --GET /metrics-->  Prometheus  -->  Grafana

The exporter is INDEPENDENT of the Runner: it only reads the journal and
`systemctl --user is-active`; the Runner does not know it exists, so a
failure of the exporter, Prometheus or Grafana can never affect Issue
claiming, Pi execution, review, merge or fail-fast. It adds no database,
queue or state store — the journal is the only source.

Journal line contract (verified against the live journal; the same
`key=value` scenes `pi_activity.parse_scene` parses):

    LEVEL [run_id] <kind> key=value ...

with kinds `run_start`, `activity`, `heartbeat`, `model_wait`, `pi_idle`,
`pi_idle_term`, `pi_idle_kill`, `run_failed`, `run_end` and
`progress_publish_failed`. Other line kinds (`command=`, `stdout=`, ...)
are skipped.

Metric labels stay low-cardinality by contract: only `slot`, `repo`,
`issue`, `role`, `phase`, `state`, `reason` and `result` are ever used —
never `run_id`, never branch/worktree/command/prompt text. The per-Runner
dimension is labeled `slot` (the Issue's own term for a Runner slot, the
same convention as the machine's llama slot exporter), NOT `instance`:
`instance` is a Prometheus scraper label, so a scraped `instance` label
is renamed to `exported_instance` and every `sum by (instance)` /
legend `{{instance}}` in the Dashboard would silently group by the scrape
target `127.0.0.1:9106` instead of the Runner slot (verified against the
running Prometheus, which stores the llama exporter's `slot` label
unchanged).

Stdlib only; no third-party runtime dependency. Fail fast: a journalctl
failure is raised with the command, return code and stderr (no fallback,
no swallowed error); the HTTP handler answers 500 with the error so the
scrape failure is visible in Prometheus.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# `LEVEL [run_id] kind scene...` — the run id is 8 lowercase hex chars
# (bootstrap_runner.new_run_id), the kind is a bare word.
LINE_RE = re.compile(
    r"^(INFO|WARNING|ERROR) \[([0-9a-f]{8})\] ([A-Za-z_]+)(?: (.*))?$"
)

# Journal kinds the exporter understands (Issue #162). Anything else on
# the line (command=, stdout=, stderr=, ...) is not an event.
KNOWN_KINDS = frozenset({
    "run_start", "activity", "heartbeat", "model_wait", "pi_idle",
    "pi_idle_term", "pi_idle_kill", "run_failed", "run_end",
    "progress_publish_failed",
})

# `1h43m`, `42m`, `0.4s` — the format_duration contract of pi_activity
# (hours always come with minutes; seconds only below one minute and may
# be fractional). `1h` alone is NOT a journal duration.
DURATION_RE = re.compile(
    r"^(?:(\d+)h(\d+)m|(\d+)m|(\d+(?:\.\d+)?)s)$"
)

DEFAULT_PORT = 9106
DEFAULT_BIND = "127.0.0.1"
DEFAULT_UNITS = "muyan-pilot@*"
DEFAULT_INSTANCES = ("1", "2")
DEFAULT_CACHE_TTL = 5.0


class JournalError(RuntimeError):
    """A journal read failed; the message carries the full scene."""


def parse_scene(text: str) -> dict[str, str | None]:
    """Parse a `key=value` scene into a dict (standalone copy of the
    `pi_activity.parse_scene` contract: values may be double-quoted when
    they contain spaces, embedded quotes are `\\"`, `key=` without a
    value parses to None, bare words are skipped)."""
    fields: dict[str, str | None] = {}
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] == " ":
            index += 1
        if index >= length:
            break
        key_start = index
        while index < length and text[index] != "=" and text[index] != " ":
            index += 1
        key = text[key_start:index]
        if not key or index >= length or text[index] != "=":
            continue  # bare word: skip it
        index += 1  # skip '='
        if index < length and text[index] == '"':
            index += 1
            start = index
            while index < length:
                if (text[index] == "\\"
                        and index + 1 < length
                        and text[index + 1] == '"'):
                    index += 2  # escaped quote: part of the value
                    continue
                if text[index] == '"':
                    break
                index += 1
            raw = text[start:index]
            if index < length:  # closing quote found
                index += 1
            else:  # unterminated quote: keep the rest of the line
                index = length
            fields[key] = raw.replace('\\"', '"')
        else:
            start = index
            while index < length and text[index] != " ":
                index += 1
            fields[key] = text[start:index] or None
    return fields


def parse_message(message: str) -> dict | None:
    """Split one journal MESSAGE into run id, kind and scene.

    Returns None for lines that are not one of the known event kinds
    (the journal also carries `command=`, `stdout=`, ... lines).
    """
    match = LINE_RE.match(message)
    if match is None:
        return None
    _level, run_id, kind, scene_text = match.groups()
    if kind not in KNOWN_KINDS:
        return None
    return {
        "run_id": run_id,
        "kind": kind,
        "scene": parse_scene(scene_text or ""),
    }


def parse_duration(value: str | None) -> float | None:
    """Parse a journal duration (`1h43m`, `42m`, `0.4s`) into seconds."""
    if not isinstance(value, str) or not value:
        return None
    match = DURATION_RE.match(value)
    if match is None:
        return None
    hours, minutes, bare_minutes, seconds = match.groups()
    if hours is not None:
        return float(int(hours) * 3600 + int(minutes) * 60)
    if bare_minutes is not None:
        return float(int(bare_minutes) * 60)
    return float(seconds)


def issue_repo(issue: str | None) -> str:
    """`owner/repo#number` -> `owner/repo` (the low-cardinality repo)."""
    if not issue:
        return "unknown"
    return issue.split("#", 1)[0] or "unknown"


def fetch_journal(units: str, run=subprocess.run) -> list[dict]:
    """Read the user journal of the Runner units as structured entries.

    Fail fast: a non-zero journalctl exit raises JournalError with the
    command, return code and stderr — never a fallback, never swallowed.
    """
    command = ["journalctl", "--user", "-u", units, "-o", "json"]
    result = run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise JournalError(
            f"journalctl failed rc={result.returncode} "
            f"command={' '.join(command)} stderr={result.stderr.strip()}"
        )
    entries: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a non-JSON line is not a journal entry
        if not isinstance(record, dict):
            continue
        message = record.get("MESSAGE")
        if not isinstance(message, str):
            continue
        timestamp = record.get("__REALTIME_TIMESTAMP")
        try:
            ts = int(timestamp) / 1_000_000
        except (TypeError, ValueError):
            ts = 0.0
        unit = record.get("_SYSTEMD_USER_UNIT")
        instance = None
        if isinstance(unit, str) and "@" in unit:
            instance = unit.rsplit("@", 1)[1].removesuffix(".service")
        entries.append({"ts": ts, "instance": instance, "message": message})
    return entries


def _label_value(value: str | None, fallback: str = "-") -> str:
    """A scene field as a label value: missing/empty -> fallback."""
    if value is None or value == "":
        return fallback
    return value


def _labels(pairs: list[tuple[str, str]]) -> str:
    return (
        "{" + ",".join(f'{k}="{v}"' for k, v in pairs) + "}"
        if pairs
        else ""
    )


def build_metrics(entries: list[dict], now: float,
                  service_active: dict[str, int]) -> str:
    """Aggregate journal entries into the Prometheus text exposition.

    `entries` are chronological (the journal order); `service_active`
    maps instance -> 1/0 for the configured service instances. The
    emitted per-Runner label is `slot` (see the module docstring: the
    `instance` label would be renamed by the Prometheus scraper).
    """
    live: dict[str, dict] = {}      # instance -> live run state
    seen_combos: set[tuple] = set()  # (slot, repo, issue, role, phase, state)
    idle_gauges: dict[tuple, float] = {}
    seconds_gauges: dict[tuple, float] = {}
    counters: dict[tuple, float] = {}

    def bump(labels: tuple, amount: float = 1.0) -> None:
        counters[labels] = counters.get(labels, 0.0) + amount

    for entry in entries:
        instance = entry["instance"] or "unknown"
        parsed = parse_message(entry["message"])
        if parsed is None:
            continue
        kind = parsed["kind"]
        scene = parsed["scene"]
        issue = _label_value(scene.get("issue"))
        role = _label_value(scene.get("role"))
        repo = issue_repo(issue)
        ts = entry["ts"]

        if kind == "run_start":
            phase = _label_value(scene.get("phase"))
            combo = (instance, repo, issue, role, phase, "-")
            seen_combos.add(combo)
            live[instance] = {
                "combo": combo,
                "phase": phase,
                "state": "-",
                "issue": issue,
                "role": role,
            }
            bump(("muyan_pilot_run_start_total",
                  ("slot", instance), ("issue", issue),
                  ("role", role)))
        elif kind in ("activity", "heartbeat"):
            run = live.get(instance)
            if run is None:
                continue  # an activity line before its run_start
            phase = _label_value(scene.get("phase"))
            state = _label_value(scene.get("state"))
            run["combo"] = (instance, repo, issue, role, phase, state)
            run["phase"] = phase
            run["state"] = state
            seen_combos.add(run["combo"])
            idle = parse_duration(scene.get("idle"))
            if idle is not None:
                idle_gauges[(instance, issue)] = idle
            elapsed = parse_duration(scene.get("elapsed"))
            if elapsed is not None:
                seconds_gauges[(instance, issue, role)] = elapsed
        elif kind == "model_wait":
            bump(("muyan_pilot_model_wait_total",
                  ("slot", instance), ("issue", issue)))
        elif kind == "pi_idle":
            bump(("muyan_pilot_pi_idle_total",
                  ("slot", instance), ("issue", issue)))
        elif kind == "pi_idle_term":
            bump(("muyan_pilot_pi_idle_term_total",
                  ("slot", instance), ("issue", issue)))
        elif kind == "pi_idle_kill":
            bump(("muyan_pilot_pi_idle_kill_total",
                  ("slot", instance), ("issue", issue)))
        elif kind == "run_failed":
            reason = _label_value(scene.get("reason"))
            bump(("muyan_pilot_run_failed_total",
                  ("slot", instance), ("issue", issue),
                  ("reason", reason)))
            live.pop(instance, None)
        elif kind == "run_end":
            result = _label_value(scene.get("result"))
            bump(("muyan_pilot_run_end_total",
                  ("slot", instance), ("issue", issue),
                  ("role", role), ("result", result)))
            elapsed = parse_duration(scene.get("elapsed"))
            if elapsed is not None:
                seconds_gauges[(instance, issue, role)] = elapsed
            live.pop(instance, None)
        elif kind == "progress_publish_failed":
            bump(("muyan_pilot_progress_publish_failed_total",
                  ("slot", instance), ("issue", issue)))
        else:
            # Unreachable: parse_message only returns KNOWN_KINDS and the
            # chain above covers every one of them. Fail fast if a kind
            # is ever added to KNOWN_KINDS without a branch here.
            raise AssertionError(f"unhandled journal kind: {kind}")

    lines: list[str] = []

    def gauge(name: str, value: float, pairs: list[tuple[str, str]]) -> None:
        lines.append(f"{name}{_labels(pairs)} {value}")

    def counter(name: str) -> None:
        for labels in sorted(counters):
            if labels[0] != name:
                continue
            gauge(name, counters[labels], list(labels[1:]))

    for instance in sorted(service_active):
        gauge("muyan_pilot_service_active", float(service_active[instance]),
              [("slot", instance)])

    for combo in sorted(seen_combos):
        instance, repo, issue, role, phase, state = combo
        is_live = (
            instance in live
            and live[instance]["combo"] == combo
        )
        gauge(
            "muyan_pilot_run_active", 1.0 if is_live else 0.0,
            [("slot", instance), ("repo", repo), ("issue", issue),
             ("role", role), ("phase", phase), ("state", state)],
        )

    for (instance, issue) in sorted(idle_gauges):
        run = live.get(instance)
        if run is None or run["issue"] != issue:
            continue  # the idle gauge is a live-state gauge
        gauge("muyan_pilot_run_idle_seconds", idle_gauges[(instance, issue)],
              [("slot", instance), ("issue", issue)])

    for (instance, issue, role) in sorted(seconds_gauges):
        gauge("muyan_pilot_run_seconds",
              seconds_gauges[(instance, issue, role)],
              [("slot", instance), ("issue", issue), ("role", role)])

    for name in (
        "muyan_pilot_run_start_total",
        "muyan_pilot_run_end_total",
        "muyan_pilot_run_failed_total",
        "muyan_pilot_progress_publish_failed_total",
        "muyan_pilot_model_wait_total",
        "muyan_pilot_pi_idle_total",
        "muyan_pilot_pi_idle_term_total",
        "muyan_pilot_pi_idle_kill_total",
    ):
        counter(name)

    return "\n".join(lines) + ("\n" if lines else "")


class Exporter:
    """One scrape cycle: journal (TTL-cached) + service states -> text."""

    def __init__(self, units: str, cache_ttl: float,
                 instances: tuple[str, ...],
                 fetch_journal, service_active,
                 clock=time.monotonic) -> None:
        self.units = units
        self.cache_ttl = cache_ttl
        self.instances = instances
        self._fetch_journal = fetch_journal
        self._service_active = service_active
        self._clock = clock
        self._cache: list[dict] | None = None
        self._cache_at = 0.0

    def _entries(self) -> list[dict]:
        now = self._clock()
        if (self._cache is None
                or now - self._cache_at >= self.cache_ttl):
            self._cache = self._fetch_journal(self.units)
            self._cache_at = now
        return self._cache

    def metrics(self) -> str:
        entries = self._entries()  # a JournalError is NOT cached
        states = {
            instance: (
                1 if self._service_active(
                    f"muyan-pilot@{instance}.service"
                ) == 0 else 0
            )
            for instance in self.instances
        }
        return build_metrics(entries, now=self._clock(),
                             service_active=states)


def default_service_active(unit: str, run=subprocess.run) -> int:
    """`systemctl --user is-active <unit>` exit code (0 = active)."""
    return run(
        ["systemctl", "--user", "is-active", unit],
        capture_output=True, text=True, timeout=15,
    ).returncode


def make_handler(exporter_instance: Exporter):
    """Build the request handler class bound to one Exporter."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server contract)
            if self.path == "/metrics":
                try:
                    body = exporter_instance.metrics().encode("utf-8")
                except Exception as exc:  # fail fast, visibly
                    body = (
                        f"# exporter error (fail fast, no fallback)\n"
                        f"exporter_error {exc!r}\n"
                    ).encode("utf-8")
                    self.send_response(500)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            # Keep the exporter quiet on stdout; scrape errors are
            # visible in the 500 body and in Prometheus itself.
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Muyan Pilot Prometheus exporter (Issue #162)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--units", default=DEFAULT_UNITS,
                        help="journalctl -u unit pattern")
    parser.add_argument("--instances", default=",".join(DEFAULT_INSTANCES),
                        help="comma-separated service instances")
    parser.add_argument("--cache-ttl", type=float, default=DEFAULT_CACHE_TTL,
                        help="journal re-read interval in seconds")
    args = parser.parse_args(argv)
    instances = tuple(
        part for part in args.instances.split(",") if part
    )
    exporter_instance = Exporter(
        units=args.units,
        cache_ttl=args.cache_ttl,
        instances=instances,
        fetch_journal=fetch_journal,
        service_active=default_service_active,
    )
    try:
        server = HTTPServer((args.bind, args.port),
                            make_handler(exporter_instance))
    except (OSError, ValueError, OverflowError) as exc:
        print(
            f"exporter_failed reason={exc} bind={args.bind} "
            f"port={args.port}",
            file=sys.stderr,
        )
        return 1
    print(
        f"muyan-pilot-exporter listening on {args.bind}:{args.port} "
        f"units={args.units} instances={','.join(instances)} "
        f"cache_ttl={args.cache_ttl}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
