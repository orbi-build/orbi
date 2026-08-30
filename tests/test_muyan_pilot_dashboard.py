"""Grafana dashboard structure contract for Issue #162.

The dashboard JSON in `monitoring/grafana/dashboards/` must be importable
into the machine's Grafana (Prometheus datasource uid `eflztqehr89a8c`)
and must show the Issue's required views: the two Runner services, the
current Issue/phase/idle per slot, model_wait, idle recovery, run_failed
by reason, progress_publish_failed, run durations, and the reused llama
slot/context/TPS/token metrics.
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = (
    REPO_ROOT / "monitoring" / "grafana" / "dashboards" / "muyan-pilot.json"
)

# The machine's Prometheus datasource (verified against the running
# Grafana 13.1.2 data_source table).
PROMETHEUS_DS = {"type": "prometheus", "uid": "eflztqehr89a8c"}

# The exporter's metric family (monitoring/prometheus/muyan-pilot-exporter.py).
MUYAN_METRICS = (
    "muyan_pilot_service_active",
    "muyan_pilot_run_active",
    "muyan_pilot_run_seconds",
    "muyan_pilot_run_idle_seconds",
    "muyan_pilot_run_start_total",
    "muyan_pilot_run_end_total",
    "muyan_pilot_run_failed_total",
    "muyan_pilot_progress_publish_failed_total",
    "muyan_pilot_model_wait_total",
    "muyan_pilot_pi_idle_total",
    "muyan_pilot_pi_idle_term_total",
    "muyan_pilot_pi_idle_kill_total",
)

# Existing llama metrics the dashboard REUSES (never re-implemented).
LLAMA_METRICS = (
    "llama_slot_is_processing",
    "llama_slots_processing",
    "llama_slots_total",
    "llama_slot_n_ctx",
    "llamacpp:n_tokens_max",
    "llamacpp:prompt_tokens_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:prompt_tokens_cached_total",
)


def load_dashboard():
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def all_panels(dashboard):
    panels = []
    for panel in dashboard.get("panels", []):
        panels.append(panel)
        panels.extend(panel.get("panels", []))  # collapsed rows
    return panels


def all_exprs(dashboard):
    exprs = []
    for panel in all_panels(dashboard):
        for target in panel.get("targets", []) or []:
            if target.get("expr"):
                exprs.append(target["expr"])
    return exprs


def test_dashboard_file_is_valid_json_with_stable_uid():
    dashboard = load_dashboard()
    assert dashboard["uid"] == "muyan-pilot"
    assert dashboard["title"]
    assert dashboard["schemaVersion"] >= 39
    assert dashboard["panels"], "the dashboard must have panels"


def test_every_panel_and_target_uses_the_prometheus_datasource():
    dashboard = load_dashboard()
    panels = all_panels(dashboard)
    assert panels
    _check_datasources(panels)


def _check_datasources(panels):
    for panel in panels:
        if panel.get("type") == "row":
            continue
        assert panel.get("datasource") == PROMETHEUS_DS, panel.get("title")
        for target in panel.get("targets", []) or []:
            if target.get("expr"):
                assert target.get("datasource") == PROMETHEUS_DS, (
                    panel.get("title"), target.get("expr")
                )


def test_dashboard_covers_every_exporter_metric_family():
    exprs = all_exprs(load_dashboard())
    joined = "\n".join(exprs)
    for name in MUYAN_METRICS:
        assert re.search(rf"\b{re.escape(name)}\b", joined), (
            f"no panel queries {name}"
        )


def test_dashboard_reuses_existing_llama_metrics():
    exprs = all_exprs(load_dashboard())
    joined = "\n".join(exprs)
    for name in LLAMA_METRICS:
        assert re.search(rf"\b{re.escape(name)}\b", joined), (
            f"no panel queries {name}"
        )


def test_no_high_cardinality_labels_or_run_id_in_any_expr():
    """The Issue contract: no unbounded run_id label, no command/prompt
    text — the dashboard may only filter on the low-cardinality label
    set (instance, repo, issue, role, phase, state, reason, result)."""
    _check_labels(all_exprs(load_dashboard()))


def test_helpers_handle_targets_without_expr_and_flag_disallowed_labels():
    # A panel target without `expr` (e.g. a reduce transform) is skipped
    # by both helpers.
    dashboard = {
        "panels": [
            {
                "type": "timeseries",
                "title": "p",
                "datasource": PROMETHEUS_DS,
                "targets": [
                    {"refId": "A", "expr": "muyan_pilot_service_active",
                     "datasource": PROMETHEUS_DS},
                    {"refId": "B"},
                ],
            },
        ],
    }
    assert all_exprs(dashboard) == ["muyan_pilot_service_active"]

    # The datasource check tolerates a target without `expr` and accepts
    # the Prometheus datasource on panel and expr targets.
    _check_datasources(all_panels(dashboard))

    # The label allowlist check flags a disallowed label (run_id) in an
    # expr and accepts the allowed ones.
    bad = 'muyan_pilot_run_active{run_id="cf357f0e"}'
    with pytest.raises(AssertionError):
        _check_labels([bad])
    _check_labels(["muyan_pilot_run_active{instance=\"1\"}"])
    _check_labels(["muyan_pilot_service_active"])


def _check_labels(exprs):
    allowed = {
        "instance", "repo", "issue", "role", "phase", "state",
        "reason", "result", "slot",
    }
    label_re = re.compile(r"\{[^}]*?([a-zA-Z_][a-zA-Z0-9_]*)\s*=")
    for expr in exprs:
        for match in label_re.finditer(expr):
            assert match.group(1) in allowed, expr


def test_panels_have_sane_grid_positions_and_titles():
    dashboard = load_dashboard()
    seen_positions = set()
    for panel in all_panels(dashboard):
        assert panel.get("title"), "every panel needs a title"
        pos = panel.get("gridPos")
        assert pos and all(k in pos for k in ("x", "y", "w", "h")), (
            panel.get("title")
        )
        key = (pos["x"], pos["y"])
        assert key not in seen_positions, (
            f"overlapping gridPos at {key}: {panel.get('title')}"
        )
        seen_positions.add(key)
        assert pos["w"] <= 24 and pos["x"] + pos["w"] <= 24


def test_dashboard_json_is_reimportable():
    """Grafana import requires the full dashboard document: round-tripping
    through json must be lossless and the file must not carry import-only
    fields (like a top-level `__inputs`) that break re-import."""
    raw = DASHBOARD_PATH.read_text(encoding="utf-8")
    dashboard = json.loads(raw)
    assert "__inputs" not in dashboard
    assert json.loads(json.dumps(dashboard)) == dashboard
