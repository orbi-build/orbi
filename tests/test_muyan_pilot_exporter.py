"""Exporter contract for Issue #162 (Prometheus exporter + Grafana
Dashboard).

The exporter is a standalone, read-only reader of the structured user
systemd journal of the `muyan-pilot@*` Runner services. These tests pin
the journal line contract (verified against the live journal and
`pi_activity.parse_scene`), the low-cardinality label allowlist (no
`run_id`, no command/prompt text), the fail-fast behavior on journal
errors, and the HTTP surface (`/metrics`, `/health`, 404).
"""
import importlib.util
import io
import json
import threading
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTER_PATH = REPO_ROOT / "monitoring" / "prometheus" / "muyan-pilot-exporter.py"
EXPORTER_UNIT_PATH = REPO_ROOT / "systemd" / "muyan-pilot-exporter.service"


def load_exporter():
    spec = importlib.util.spec_from_file_location(
        "muyan_pilot_exporter", EXPORTER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load_exporter()


def entry(ts, instance, message):
    return {"ts": ts, "instance": instance, "message": message}


def metric(lines, name, labels=None, value=None):
    """Find one metric line; return its value (or assert it)."""
    wanted = f"{name}{{{labels}}}" if labels else f"{name} "
    found = [ln for ln in lines if ln.startswith(wanted)]
    if value is None:
        assert len(found) == 1, (
            f"expected exactly one {name}{{{labels}}}, got {found}"
        )
        return float(found[0].split(" ", 2)[-1])
    assert any(ln == f"{wanted} {value}" for ln in found), (
        f"expected {wanted} {value} in {lines}"
    )


def lines_of(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


# --- systemd deployment: persistent exporter contract ---------------------


def test_exporter_unit_runs_the_deployed_exporter_and_restarts():
    """The versioned user unit keeps the exporter available after boot/exit."""
    unit = EXPORTER_UNIT_PATH.read_text(encoding="utf-8")
    assert "[Service]" in unit
    assert "WorkingDirectory=%h/Documents/muyan/muyan-pilot" in unit
    assert (
        "ExecStart=/usr/bin/python3 "
        "%h/Documents/muyan/muyan-pilot/monitoring/prometheus/"
        "muyan-pilot-exporter.py"
    ) in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    assert "[Install]" in unit
    assert "WantedBy=default.target" in unit


# --- parse_message: the journal line contract -----------------------------


def test_parse_message_run_start_extracts_run_id_kind_and_scene():
    parsed = exporter.parse_message(
        "INFO [cf357f0e] run_start run=cf357f0e issue=xqliu/orbi#162 "
        "role=implement branch=b worktree=/w session=- session_file=- "
        "phase=starting last_activity=- action=- result=-"
    )
    assert parsed is not None
    assert parsed["run_id"] == "cf357f0e"
    assert parsed["kind"] == "run_start"
    assert parsed["scene"]["issue"] == "xqliu/orbi#162"
    assert parsed["scene"]["role"] == "implement"
    assert parsed["scene"]["phase"] == "starting"


def test_parse_message_quoted_action_with_spaces_and_escaped_quotes():
    parsed = exporter.parse_message(
        'INFO [cf357f0e] activity issue=xqliu/orbi#162 role=implement '
        'phase=bash action="bash cd /w \\"quoted\\" && pytest" result=ok '
        "state=model_wait idle=12s"
    )
    assert parsed["kind"] == "activity"
    assert parsed["scene"]["action"] == 'bash cd /w "quoted" && pytest'
    assert parsed["scene"]["state"] == "model_wait"


def test_parse_message_skips_unknown_line_kinds():
    # `command=` / `stdout=` journal lines are whole-line scenes, not
    # `LEVEL [run_id] kind scene` lines: the line regex does not match.
    assert exporter.parse_message(
        "INFO [cf357f0e] command=gh api repos/xqliu/orbi/issues/162"
    ) is None
    assert exporter.parse_message(
        "INFO [cf357f0e] stdout=some output"
    ) is None
    # A future journal kind that DOES match the line shape is tolerated:
    # it is skipped, not an error.
    assert exporter.parse_message(
        "INFO [cf357f0e] some_future_kind issue=x#1"
    ) is None


def test_parse_message_rejects_bad_run_id_and_bad_level():
    assert exporter.parse_message("INFO [xyz] run_start run=1") is None
    assert exporter.parse_message("DEBUG [cf357f0e] run_start run=1") is None
    assert exporter.parse_message("garbage line without structure") is None


def test_parse_scene_key_without_value_parses_to_none():
    scene = exporter.parse_scene("phase= action=-")
    assert scene == {"phase": None, "action": "-"}


def test_parse_scene_skips_bare_words_and_trailing_spaces():
    scene = exporter.parse_scene("phase=read bareword   ")
    assert scene == {"phase": "read"}


def test_parse_scene_unterminated_quote_keeps_rest_of_line():
    scene = exporter.parse_scene('action="unterminated rest')
    assert scene == {"action": "unterminated rest"}


# --- parse_duration: the journal duration contract ------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0s", 0.0),
        ("0.4s", 0.4),
        ("5s", 5.0),
        ("42m", 42 * 60.0),
        ("1h43m", 6180.0),
        ("1h0m", 3600.0),
    ],
)
def test_parse_duration_journal_formats(raw, expected):
    assert exporter.parse_duration(raw) == pytest.approx(expected)


def test_parse_duration_rejects_non_journal_formats():
    # The journal only ever carries format_duration output (pi_activity):
    # `0s`, `X.Ys`, `Ns`, `Nm`, `NhMm` — nothing else.
    for raw in ("-", "1h", "m", "", None, "5m30s", "1h5m30s", "1h43"):
        assert exporter.parse_duration(raw) is None, raw


# --- issue label helpers ---------------------------------------------------


def test_issue_repo_splits_owner_repo_from_issue_ref():
    assert exporter.issue_repo("xqliu/orbi#162") == "xqliu/orbi"
    assert exporter.issue_repo("xqliu/muyan-pilot#18") == "xqliu/muyan-pilot"


def test_issue_repo_falls_back_to_whole_ref_when_no_hash():
    assert exporter.issue_repo("unknown") == "unknown"
    assert exporter.issue_repo("#162") == "unknown"
    assert exporter.issue_repo(None) == "unknown"
    assert exporter.issue_repo("") == "unknown"


# --- fetch_journal: fail fast, no fallback --------------------------------


def test_fetch_journal_parses_json_entries_with_instance_and_ts():
    payload = "\n".join([
        json.dumps({
            "__REALTIME_TIMESTAMP": "1787958782202724",
            "_SYSTEMD_USER_UNIT": "muyan-pilot@1.service",
            "MESSAGE": "INFO [cf357f0e] heartbeat issue=xqliu/orbi#162 "
                       "role=implement phase=read state=- elapsed=1m idle=1m",
        }),
        json.dumps({
            "__REALTIME_TIMESTAMP": "1787958785633784",
            "_SYSTEMD_USER_UNIT": "muyan-pilot@2.service",
            "MESSAGE": "INFO [cf357f0e] run_start run=cf357f0e "
                       "issue=xqliu/orbi#162 role=implement phase=starting",
        }),
    ]) + "\n"

    class FakeCompleted:
        returncode = 0
        stdout = payload
        stderr = ""

    entries = exporter.fetch_journal(
        "muyan-pilot@*",
        run=subprocess_run_factory(FakeCompleted()),
    )
    assert [e["instance"] for e in entries] == ["1", "2"]
    assert entries[0]["ts"] == 1787958782202724 / 1_000_000
    assert entries[0]["message"].startswith("INFO [cf357f0e] heartbeat")


def test_fetch_journal_skips_blank_lines_and_non_dict_records():
    payload = "\n" + json.dumps([1, 2, 3]) + "\n" + json.dumps({
        "__REALTIME_TIMESTAMP": "1000",
        "MESSAGE": "INFO [cf357f0e] heartbeat issue=x#1 role=implement",
    }) + "\n" + json.dumps({
        "__REALTIME_TIMESTAMP": "2000",
        "_SYSTEMD_USER_UNIT": "muyan-pilot@1.service",
    }) + "\n"

    class FakeCompleted:
        returncode = 0
        stdout = payload
        stderr = ""

    entries = exporter.fetch_journal(
        "muyan-pilot@*",
        run=subprocess_run_factory(FakeCompleted()),
    )
    assert len(entries) == 1


def test_fetch_journal_invalid_timestamp_falls_back_to_zero():
    payload = json.dumps({
        "__REALTIME_TIMESTAMP": "not-a-number",
        "_SYSTEMD_USER_UNIT": "muyan-pilot@1.service",
        "MESSAGE": "INFO [cf357f0e] heartbeat issue=x#1 role=implement",
    }) + "\n"

    class FakeCompleted:
        returncode = 0
        stdout = payload
        stderr = ""

    entries = exporter.fetch_journal(
        "muyan-pilot@*",
        run=subprocess_run_factory(FakeCompleted()),
    )
    assert entries[0]["ts"] == 0.0
    assert entries[0]["instance"] == "1"


def test_fetch_journal_skips_non_json_lines_and_missing_unit():
    payload = "not-json\n" + json.dumps({
        "__REALTIME_TIMESTAMP": "1000",
        "MESSAGE": "INFO [cf357f0e] heartbeat issue=x#1 role=implement",
    }) + "\n"

    class FakeCompleted:
        returncode = 0
        stdout = payload
        stderr = ""

    entries = exporter.fetch_journal(
        "muyan-pilot@*",
        run=subprocess_run_factory(FakeCompleted()),
    )
    assert len(entries) == 1
    assert entries[0]["instance"] is None


def test_fetch_journal_fails_fast_on_nonzero_exit():
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "No journal files were opened"

    with pytest.raises(exporter.JournalError) as excinfo:
        exporter.fetch_journal(
            "muyan-pilot@*",
            run=subprocess_run_factory(FakeCompleted()),
        )
    assert "No journal files were opened" in str(excinfo.value)
    assert "journalctl" in str(excinfo.value)


def subprocess_run_factory(completed):
    def run(cmd, **kwargs):
        return completed
    return run


# --- build_metrics: aggregation over journal entries ----------------------


def test_build_metrics_live_run_gauges_and_counters():
    entries = [
        entry(100, "1",
              "INFO [aaaa1111] run_start run=aaaa1111 issue=xqliu/orbi#162 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
        entry(160, "1",
              "INFO [aaaa1111] activity issue=xqliu/orbi#162 role=implement "
              "phase=read action=\"read /x\" result=ok state=model_wait "
              "idle=5s"),
        entry(175, "1",
              "INFO [aaaa1111] model_wait issue=xqliu/orbi#162 "
              "role=implement phase=read state=model_wait"),
        entry(200, "1",
              "INFO [aaaa1111] heartbeat issue=xqliu/orbi#162 "
              "role=implement phase=read state=model_wait elapsed=1m "
              "idle=20s"),
    ]
    text = exporter.build_metrics(entries, now=300.0, service_active={"1": 1})
    lines = lines_of(text)

    metric(lines, "muyan_pilot_service_active", 'slot="1"', 1.0)
    metric(lines, "muyan_pilot_run_active",
           'slot="1",repo="xqliu/orbi",issue="xqliu/orbi#162",'
           'role="implement",phase="read",state="model_wait"', 1.0)
    metric(lines, "muyan_pilot_run_idle_seconds",
           'slot="1",issue="xqliu/orbi#162"', 20.0)
    metric(lines, "muyan_pilot_run_seconds",
           'slot="1",issue="xqliu/orbi#162",role="implement"', 60.0)
    metric(lines, "muyan_pilot_run_start_total",
           'slot="1",issue="xqliu/orbi#162",role="implement"', 1.0)
    metric(lines, "muyan_pilot_model_wait_total",
           'slot="1",issue="xqliu/orbi#162"', 1.0)


def test_build_metrics_finished_run_keeps_end_scene_and_zeroes_active():
    entries = [
        entry(100, "1",
              "INFO [bbbb2222] run_start run=bbbb2222 issue=xqliu/orbi#50 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
        entry(200, "1",
              "INFO [bbbb2222] heartbeat issue=xqliu/orbi#50 "
              "role=implement phase=test state=- elapsed=10m idle=1s"),
        entry(300, "1",
              "INFO [bbbb2222] run_end run=bbbb2222 issue=xqliu/orbi#50 "
              "role=implement result=pr_opened elapsed=3h30m "
              "pr=https://github.com/xqliu/orbi/pull/187 commit=abc123"),
        # A later live run on the same instance: the finished combo must
        # still be present with run_active=0 (Prometheus needs the series).
        entry(400, "1",
              "INFO [cccc3333] run_start run=cccc3333 issue=xqliu/orbi#181 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
    ]
    text = exporter.build_metrics(entries, now=400.0, service_active={"1": 0})
    lines = lines_of(text)

    metric(lines, "muyan_pilot_service_active", 'slot="1"', 0.0)
    metric(lines, "muyan_pilot_run_active",
           'slot="1",repo="xqliu/orbi",issue="xqliu/orbi#181",'
           'role="implement",phase="starting",state="-"', 1.0)
    metric(lines, "muyan_pilot_run_active",
           'slot="1",repo="xqliu/orbi",issue="xqliu/orbi#50",'
           'role="implement",phase="test",state="-"', 0.0)
    metric(lines, "muyan_pilot_run_seconds",
           'slot="1",issue="xqliu/orbi#50",role="implement"', 12600.0)
    metric(lines, "muyan_pilot_run_end_total",
           'slot="1",issue="xqliu/orbi#50",role="implement",'
           'result="pr_opened"', 1.0)
    # The finished run's idle gauge is not re-emitted (no live state).
    assert not any(
        ln.startswith('muyan_pilot_run_idle_seconds{slot="1",'
                      'issue="xqliu/orbi#50"}')
        for ln in lines
    )


def test_build_metrics_failure_and_recovery_counters_by_reason():
    entries = [
        entry(100, "2",
              "INFO [dddd4444] run_start run=dddd4444 issue=xqliu/orbi#181 "
              "role=review branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
        entry(200, "2",
              "WARNING [dddd4444] pi_idle issue=xqliu/orbi#181 "
              "role=review phase=bash stale_seconds=5m"),
        entry(300, "2",
              "INFO [dddd4444] pi_idle_term run=dddd4444 issue=xqliu/orbi#181 "
              "role=review pid=4242 cmdline=\"timeout 240 pytest\" "
              "result=sent"),
        entry(400, "2",
              "INFO [dddd4444] pi_idle_kill run=dddd4444 issue=xqliu/orbi#181 "
              "role=review pid=4242 cmdline=\"timeout 240 pytest\" "
              "result=already_dead"),
        entry(500, "2",
              "ERROR [dddd4444] run_failed run=dddd4444 issue=xqliu/orbi#181 "
              "role=review branch=b worktree=/w session=- session_file=- "
              "phase=bash last_activity=- action=- result=- "
              "reason=idle_recovery_stale_15m"),
        entry(600, "2",
              "INFO [dddd4444] progress_publish_failed run=dddd4444 "
              "issue=xqliu/orbi#181 role=review"),
    ]
    text = exporter.build_metrics(entries, now=700.0, service_active={"2": 1})
    lines = lines_of(text)

    metric(lines, "muyan_pilot_pi_idle_total",
           'slot="2",issue="xqliu/orbi#181"', 1.0)
    metric(lines, "muyan_pilot_pi_idle_term_total",
           'slot="2",issue="xqliu/orbi#181"', 1.0)
    metric(lines, "muyan_pilot_pi_idle_kill_total",
           'slot="2",issue="xqliu/orbi#181"', 1.0)
    metric(lines, "muyan_pilot_run_failed_total",
           'slot="2",issue="xqliu/orbi#181",'
           'reason="idle_recovery_stale_15m"', 1.0)
    metric(lines, "muyan_pilot_progress_publish_failed_total",
           'slot="2",issue="xqliu/orbi#181"', 1.0)
    # The failed run is no longer active.
    assert not any(
        ln.endswith('state="model_wait"} 1')
        and 'issue="xqliu/orbi#181"' in ln
        for ln in lines
        if ln.startswith("muyan_pilot_run_active")
    )


def test_build_metrics_labels_stay_within_allowlist():
    """No run_id, no command/prompt text may ever reach a label."""
    entries = [
        entry(100, "1",
              "INFO [eeee5555] run_start run=eeee5555 issue=xqliu/orbi#162 "
              "role=implement branch=muyan-pilot/xqliu-orbi-issue-162-eeee5555 "
              "worktree=/home/x/w session=- session_file=- phase=starting "
              "last_activity=- action=- result=-"),
        entry(200, "1",
              "INFO [eeee5555] activity issue=xqliu/orbi#162 role=implement "
              "phase=bash action=\"bash cat > /tmp/x.py <<'EOF' secret "
              "prompt text EOF\" result=ok state=- idle=2s"),
    ]
    text = exporter.build_metrics(entries, now=300.0, service_active={"1": 1})
    for line in lines_of(text):
        body = line.split("{", 1)[1] if "{" in line else ""
        assert "eeee5555" not in body, line
        assert "branch=" not in body, line
        assert "worktree=" not in body, line
        assert "prompt text" not in body, line
        for label in body.strip("}").split(","):
            name = label.split("=", 1)[0].strip()
            # The allowlist deliberately excludes `instance` and `job`:
            # the Prometheus scraper reserves both, so an emitted
            # `instance` label would arrive as `exported_instance` and
            # break every per-Runner grouping in the Dashboard (Issue
            # #162, verified against the running Prometheus).
            assert name in {
                "slot", "repo", "issue", "role", "phase", "state",
                "reason", "result",
            }, line


def test_build_metrics_unparseable_messages_are_skipped():
    entries = [
        entry(100, "1", "command=gh api repos/xqliu/orbi/issues/162"),
        entry(200, "1", "stdout=some output"),
    ]
    text = exporter.build_metrics(entries, now=300.0, service_active={"1": 1})
    lines = lines_of(text)
    assert metric(lines, "muyan_pilot_service_active", 'slot="1"') == 1.0
    assert not any(
        ln.startswith("muyan_pilot_run_") for ln in lines
    )


def test_build_metrics_activity_before_run_start_is_ignored():
    entries = [
        entry(100, "1",
              "INFO [aaaa1111] activity issue=xqliu/orbi#162 "
              "role=implement phase=test action=\"read /x\" result=ok "
              "state=- idle=2s"),
    ]
    text = exporter.build_metrics(entries, now=200.0, service_active={"1": 1})
    lines = lines_of(text)
    assert not any(
        ln.startswith("muyan_pilot_run_") for ln in lines
    )


def test_build_metrics_heartbeat_without_idle_or_elapsed_updates_combo_only():
    entries = [
        entry(100, "1",
              "INFO [aaaa1111] run_start run=aaaa1111 issue=xqliu/orbi#162 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
        entry(200, "1",
              "INFO [aaaa1111] heartbeat issue=xqliu/orbi#162 "
              "role=implement phase=read state=model_wait"),
    ]
    text = exporter.build_metrics(entries, now=300.0, service_active={"1": 1})
    lines = lines_of(text)
    metric(lines, "muyan_pilot_run_active",
           'slot="1",repo="xqliu/orbi",issue="xqliu/orbi#162",'
           'role="implement",phase="read",state="model_wait"', 1.0)
    assert not any(ln.startswith("muyan_pilot_run_idle_seconds")
                   for ln in lines)
    assert not any(ln.startswith("muyan_pilot_run_seconds") for ln in lines)


def test_build_metrics_run_end_without_elapsed_still_counts():
    entries = [
        entry(100, "1",
              "INFO [aaaa1111] run_start run=aaaa1111 issue=xqliu/orbi#162 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
        entry(200, "1",
              "INFO [aaaa1111] run_end run=aaaa1111 issue=xqliu/orbi#162 "
              "role=implement result=failed pr=- commit=-"),
    ]
    text = exporter.build_metrics(entries, now=300.0, service_active={"1": 1})
    lines = lines_of(text)
    metric(lines, "muyan_pilot_run_end_total",
           'slot="1",issue="xqliu/orbi#162",role="implement",'
           'result="failed"', 1.0)
    assert not any(ln.startswith("muyan_pilot_run_seconds") for ln in lines)


def test_label_value_falls_back_for_missing_and_empty():
    assert exporter._label_value(None) == "-"
    assert exporter._label_value("") == "-"
    assert exporter._label_value("model_wait") == "model_wait"
    assert exporter._label_value(None, "none") == "none"


def test_build_metrics_fails_fast_on_kind_outside_chain(monkeypatch):
    # The chain covers every KNOWN_KINDS member; a kind that slips past
    # parse_message (e.g. a future kind added to KNOWN_KINDS without a
    # branch) must fail fast, not be silently dropped.
    monkeypatch.setattr(
        exporter, "parse_message",
        lambda message: {"run_id": "aaaa1111", "kind": "future_kind",
                         "scene": {}},
    )
    with pytest.raises(AssertionError, match="future_kind"):
        exporter.build_metrics(
            [entry(100, "1", "INFO [aaaa1111] future_kind issue=x#1")],
            now=200.0, service_active={"1": 1},
        )


def test_build_metrics_empty_journal_emits_only_service_gauges():
    text = exporter.build_metrics([], now=10.0, service_active={"1": 1, "2": 0})
    lines = lines_of(text)
    assert metric(lines, "muyan_pilot_service_active", 'slot="1"') == 1.0
    assert metric(lines, "muyan_pilot_service_active", 'slot="2"') == 0.0
    assert not any(ln.startswith("muyan_pilot_run_") for ln in lines)


def test_build_metrics_unknown_scene_fields_are_ignored():
    entries = [
        entry(100, "1",
              "INFO [ffff6666] run_start run=ffff6666 issue=xqliu/orbi#9 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=- "
              "unexpected_field=whatever"),
    ]
    text = exporter.build_metrics(entries, now=200.0, service_active={"1": 1})
    lines = lines_of(text)
    metric(lines, "muyan_pilot_run_active",
           'slot="1",repo="xqliu/orbi",issue="xqliu/orbi#9",'
           'role="implement",phase="starting",state="-"', 1.0)


def test_build_metrics_missing_instance_falls_back_to_unknown():
    entries = [
        entry(100, None,
              "INFO [aaaa1111] run_start run=aaaa1111 issue=xqliu/orbi#162 "
              "role=implement branch=b worktree=/w session=- session_file=- "
              "phase=starting last_activity=- action=- result=-"),
    ]
    text = exporter.build_metrics(entries, now=200.0, service_active={})
    lines = lines_of(text)
    metric(lines, "muyan_pilot_run_start_total",
           'slot="unknown",issue="xqliu/orbi#162",role="implement"', 1.0)


# --- Exporter: TTL cache + service state ----------------------------------


def test_exporter_caches_journal_until_ttl_expires():
    calls = []

    def fake_journal(units, run=None):
        calls.append(units)
        return [entry(100, "1",
                      "INFO [aaaa1111] run_start run=aaaa1111 "
                      "issue=xqliu/orbi#162 role=implement branch=b "
                      "worktree=/w session=- session_file=- phase=starting "
                      "last_activity=- action=- result=-")]

    def fake_systemctl(unit):
        return 0  # systemctl exit code: 0 = active

    exp = exporter.Exporter(
        units="muyan-pilot@*", cache_ttl=10.0, instances=("1",),
        fetch_journal=fake_journal, service_active=fake_systemctl,
        clock=lambda: 1.0,
    )
    first = exp.metrics()
    second = exp.metrics()
    assert first == second
    assert len(calls) == 1  # second scrape served from the cache
    assert 'muyan_pilot_service_active{slot="1"} 1' in first


def test_exporter_refreshes_after_ttl_and_after_journal_error():
    state = {"n": 0}

    def fake_journal(units, run=None):
        state["n"] += 1
        if state["n"] == 1:
            raise exporter.JournalError("boom")
        return []

    exp = exporter.Exporter(
        units="muyan-pilot@*", cache_ttl=10.0, instances=("1",),
        fetch_journal=fake_journal, service_active=lambda u: 0,
        clock=lambda: 1.0,
    )
    with pytest.raises(exporter.JournalError):
        exp.metrics()  # the error is NOT cached
    text = exp.metrics()  # the next scrape retries immediately
    assert "muyan_pilot_service_active" in text
    assert state["n"] == 2


def test_exporter_service_active_maps_unit_instances():
    seen = []

    def fake_systemctl(unit):
        seen.append(unit)
        return 0 if unit == "muyan-pilot@1.service" else 3

    exp = exporter.Exporter(
        units="muyan-pilot@*", cache_ttl=0.0,
        instances=("1", "2"),
        fetch_journal=lambda units, run=None: [],
        service_active=fake_systemctl,
        clock=lambda: 1.0,
    )
    text = exp.metrics()
    assert seen == ["muyan-pilot@1.service", "muyan-pilot@2.service"]
    lines = lines_of(text)
    metric(lines, "muyan_pilot_service_active", 'slot="1"', 1.0)
    metric(lines, "muyan_pilot_service_active", 'slot="2"', 0.0)


def test_exporter_default_systemctl_uses_systemctl_user_is_active():
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return type("C", (), {"returncode": 0, "stdout": "active\n",
                              "stderr": ""})()

    result = exporter.default_service_active(
        "muyan-pilot@1.service", run=fake_run,
    )
    assert result == 0  # systemctl exit code: 0 = active
    assert seen["cmd"] == [
        "systemctl", "--user", "is-active", "muyan-pilot@1.service",
    ]


def test_default_service_active_nonzero_exit_means_inactive():
    def fake_run(cmd, **kwargs):
        return type("C", (), {"returncode": 3, "stdout": "inactive\n",
                              "stderr": ""})()

    assert exporter.default_service_active(
        "muyan-pilot@2.service", run=fake_run,
    ) == 3


# --- HTTP surface ----------------------------------------------------------


def serve(exporter_module, exporter_instance):
    handler = exporter_module.make_handler(exporter_instance)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def http_get(server, path):
    import urllib.request
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_http_metrics_health_and_404():
    exp = exporter.Exporter(
        units="muyan-pilot@*", cache_ttl=0.0, instances=("1",),
        fetch_journal=lambda units, run=None: [],
        service_active=lambda u: 0,
        clock=lambda: 1.0,
    )
    server = serve(exporter, exp)
    try:
        status, body = http_get(server, "/metrics")
        assert status == 200
        assert "muyan_pilot_service_active" in body
        status, body = http_get(server, "/health")
        assert status == 200
        assert body == "ok"
        status, _ = http_get(server, "/nope")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_http_metrics_survives_journal_failure_with_error_body():
    def broken_journal(units, run=None):
        raise exporter.JournalError("No journal files were opened")

    exp = exporter.Exporter(
        units="muyan-pilot@*", cache_ttl=0.0, instances=("1",),
        fetch_journal=broken_journal,
        service_active=lambda u: 0,
        clock=lambda: 1.0,
    )
    server = serve(exporter, exp)
    try:
        status, body = http_get(server, "/metrics")
        assert status == 500
        assert "No journal files were opened" in body
    finally:
        server.shutdown()
        server.server_close()


# --- main: argument parsing and fail fast ---------------------------------


def test_main_fails_fast_on_bad_port(capsys):
    rc = exporter.main(["--port", "99999", "--bind", "127.0.0.1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "exporter_failed" in err


def test_main_rejects_unknown_argument():
    with pytest.raises(SystemExit) as excinfo:
        exporter.main(["--nonsense"])
    assert excinfo.value.code == 2


def test_main_serves_until_interrupted(monkeypatch, capsys):
    def fake_serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(HTTPServer, "serve_forever", fake_serve_forever)
    rc = exporter.main(["--port", "0", "--bind", "127.0.0.1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "muyan-pilot-exporter listening" in out
    assert "units=muyan-pilot@*" in out
