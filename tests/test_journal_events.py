"""Journal single emission point (Issue #791).

Every structured journal event line — `LEVEL [run_id] <kind> key=value
...` — is emitted through `journal.event()`, the ONE emission point, and
every event name is registered in the `JOURNAL_EVENTS` table. Three
consistency nets hold the registry, the docs and the exporter together:

- the docs event tables (`docs/operations.mdx` EN + `docs/zh/operations.mdx`
  ZH) carry exactly the registry set, in both directions;
- the exporter's `KNOWN_KINDS` (`monitoring/prometheus/orbi-exporter.py`)
  are a subset of the registry — the exporter is stdlib-only and cannot
  import the package, so this test IS the shared-registry binding;
- no module outside `journal.py` (the kernel itself) and `cli.py`
  (exempt by the Issue: non-journal output) builds a kind-shaped line
  through the logger directly.

`LOGGER.exception(...)` calls stay exempt from the AST net: they are
failure records whose value is the traceback, not structured events.
"""
import ast
import importlib.util
import logging
import re
from pathlib import Path

import pytest

from orbi import journal

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "orbi"
EXPORTER_PATH = REPO_ROOT / "monitoring" / "prometheus" / "orbi-exporter.py"

# A kind-shaped first token: a lowercase snake word with no `=` (a
# `key=value` first token, a `%s` placeholder or a prose Sentence never
# matches). The second net below additionally requires a field token
# somewhere on the line, so free-form prose is never flagged.
KIND_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Kinds with no literal at the call site (passed dynamically): the
# startup milestones of `_log_startup` and the model_wait/resumed
# ternary. Frozen here so a rename cannot silently unregister them.
DYNAMIC_KINDS = (
    "process_spawned",
    "provider_config_loaded",
    "session_created",
    "first_request_started",
    "first_response_received",
    "startup_failed",
    "model_wait",
    "resumed",
)


@pytest.fixture(autouse=True)
def _reset_run_binding(monkeypatch):
    """Isolate the module-level run binding between tests."""
    monkeypatch.setattr(journal, "_CURRENT_RUN_ID", None)


# ---------------------------------------------------------------------------
# journal.event: the single emission point
# ---------------------------------------------------------------------------


def test_event_emits_kind_and_fields_in_call_order(caplog):
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        journal.event(
            "run_end", run="ab12cd34", issue="o/r#1", result="merged",
        )
    record = caplog.records[0]
    assert record.message == "run_end run=ab12cd34 issue=o/r#1 result=merged"
    assert record.levelno == logging.INFO


def test_event_carries_the_bound_run_id_prefix(caplog):
    journal.set_run_id("ab12cd34")
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        journal.event("run_stopped", result="idle")
    assert caplog.records[0].message == "[ab12cd34] run_stopped result=idle"


def test_event_quotes_spaced_values_and_flattens_newlines(caplog):
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        journal.event(
            "pi_idle_term", cmdline="timeout 30\npytest -q", pid="7",
        )
    message = caplog.records[0].message
    assert message == r'pi_idle_term cmdline="timeout 30\npytest -q" pid=7'
    # The quoted value round-trips through the journal scene parser; the
    # line break stays the visible two-character `\n` escape (Issue #143:
    # display flattening only — one journal line per event).
    fields = _exporter_module().parse_scene(
        message.removeprefix("pi_idle_term "),
    )
    assert fields["cmdline"] == "timeout 30\\npytest -q"


def test_event_rejects_an_unregistered_kind():
    with pytest.raises(ValueError, match="unknown journal event"):
        journal.event("definitely_not_registered", issue="o/r#1")


def test_event_supports_scene_continuation_and_level(caplog):
    with caplog.at_level(logging.WARNING, logger="orbi.bootstrap"):
        journal.event(
            "run_failed", "run=ab12cd34 issue=o/r#1 role=implement",
            reason="pi_exit_1", level=logging.WARNING,
        )
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.message == (
        "run_failed run=ab12cd34 issue=o/r#1 role=implement reason=pi_exit_1"
    )


def test_every_registered_kind_is_a_lowercase_event_word():
    for kind in journal.JOURNAL_EVENTS:
        assert kind and kind == kind.lower(), kind
        assert KIND_TOKEN_RE.fullmatch(kind), kind


# ---------------------------------------------------------------------------
# The registry ↔ docs ↔ exporter consistency nets
# ---------------------------------------------------------------------------


def _docs_event_names(page_text: str, heading: str) -> set[str]:
    """Parse the `| Event | Meaning |` table under `heading`.

    The heading is followed by an intro paragraph; the table starts at
    the first pipe row after it and ends at the first non-pipe line.
    A missing heading is a hard failure (the registry contract died).
    """
    lines = page_text.splitlines()
    if heading not in lines:
        raise AssertionError(f"missing docs event heading: {heading!r}")
    index = lines.index(heading) + 1
    while index < len(lines) and not lines[index].startswith("|"):
        index += 1
    names: set[str] = set()
    for line in lines[index:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or not cells[0].startswith("`"):
            continue  # header (`| Event | ...`) or separator row
        names.add(cells[0].strip("`"))
    return names


def test_docs_event_table_parser_rejects_a_missing_heading():
    with pytest.raises(AssertionError, match="missing docs event heading"):
        _docs_event_names("## Logs\n\nno table here", "### Event reference")


def test_docs_event_table_parser_reads_a_table_to_the_end_of_the_page():
    # No trailing text after the table: the row loop exhausts normally.
    text = (
        "### Event reference\n"
        "intro\n"
        "| Event | Meaning |\n"
        "| --- | --- |\n"
        "| `run_end` | a delivery ended |\n"
    )
    assert _docs_event_names(text, "### Event reference") == {"run_end"}


def test_docs_event_table_parser_stops_at_the_first_non_row_line():
    text = (
        "### Event reference\n"
        "| Event | Meaning |\n"
        "| --- | --- |\n"
        "| `run_end` | a delivery ended |\n"
        "\n"
        "| `not_a_table_row_anymore` | skipped |\n"
    )
    assert _docs_event_names(text, "### Event reference") == {"run_end"}


def test_docs_event_table_en_carries_exactly_the_registry():
    page = REPO_ROOT / "docs" / "operations.mdx"
    table = _docs_event_names(
        page.read_text(encoding="utf-8"), "### Event reference",
    )
    assert table == set(journal.JOURNAL_EVENTS)


def test_docs_event_table_zh_carries_exactly_the_registry():
    page = REPO_ROOT / "docs" / "zh" / "operations.mdx"
    table = _docs_event_names(
        page.read_text(encoding="utf-8"), "### 事件参考",
    )
    assert table == set(journal.JOURNAL_EVENTS)


def _exporter_module():
    spec = importlib.util.spec_from_file_location(
        "orbi_exporter_under_test", EXPORTER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exporter_known_kinds_are_registered_events():
    exporter = _exporter_module()
    unregistered = exporter.KNOWN_KINDS - set(journal.JOURNAL_EVENTS)
    assert not unregistered, (
        f"exporter kinds missing from JOURNAL_EVENTS: {sorted(unregistered)}"
    )


def test_dynamic_kinds_are_registered():
    for kind in DYNAMIC_KINDS:
        assert kind in journal.JOURNAL_EVENTS, kind


# ---------------------------------------------------------------------------
# The AST nets: one emission point, no direct kind lines
# ---------------------------------------------------------------------------


def _kind_shaped(first: str, fmt: str) -> bool:
    """Whether a direct LOGGER format string builds a `kind ...` line.

    True when the first token is a registered event name, or when it is
    a lowercase snake word followed only by more kind/`key=value` tokens
    (an unregistered future event). Free-form prose and `key=value`-first
    lines (command=, stdout=) never match: prose words carry no `=`, and
    their first token is a key or a Sentence.
    """
    if first in journal.JOURNAL_EVENTS:
        return True
    tokens = fmt.split(" ")
    if not KIND_TOKEN_RE.fullmatch(first):
        return False
    rest = tokens[1:]
    return not rest or all("=" in token for token in rest)


def _direct_logger_kind_lines(
    source: str,
) -> list[tuple[int, str, str]]:
    """(lineno, first format token, full format) of direct LOGGER calls
    with a constant format string (implicit concatenation arrives folded
    into one Constant, so the full format is exact)."""
    tree = ast.parse(source)
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"info", "warning", "error", "log",
                                       "debug"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "LOGGER"):
            continue
        index = 1 if node.func.attr == "log" else 0
        if len(node.args) <= index:
            continue
        fmt = node.args[index]
        if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str):
            found.append(
                (node.lineno, fmt.value.split(" ")[0], fmt.value),
            )
    return found


def test_detector_flags_kind_lines_and_ignores_prose():
    """The detector really detects: a registered kind, an unregistered
    snake kind and a bare kind word are flagged; key=value-first lines,
    prose and exception calls are not."""
    source = (
        "LOGGER.info('run_end run=x')\n"          # registered kind
        "LOGGER.warning('new_event issue=%s')\n"  # unregistered kind
        "LOGGER.error('health_check_failed')\n"   # bare kind word
        "LOGGER.info('run_end run=%s', x)\n"      # via LOGGER.log index
        "LOGGER.info('command=%s cwd=%s', a, b)\n"  # key-first, no kind
        "LOGGER.info('stop scene activity snapshot failed')\n"  # prose
        "LOGGER.exception('run_end run=x')\n"     # exception: exempt
        "journal.event('run_end', run='x')\n"     # the emission point
        "other.info('run_end run=x')\n"           # not the LOGGER name
        "LOGGER.log(logging.INFO, 'new_event x=1')\n"  # log form
        "LOGGER.info()\n"                          # no format: skipped
        "LOGGER.info(fmt_variable)\n"              # non-constant format: outside the net
    )
    assert _direct_logger_kind_lines(source) == [
        (1, "run_end", "run_end run=x"),
        (2, "new_event", "new_event issue=%s"),
        (3, "health_check_failed", "health_check_failed"),
        (4, "run_end", "run_end run=%s"),
        (5, "command=%s", "command=%s cwd=%s"),
        (6, "stop", "stop scene activity snapshot failed"),
        (10, "new_event", "new_event x=1"),
    ]
    flagged = [
        lineno for lineno, first, fmt in _direct_logger_kind_lines(source)
        if _kind_shaped(first, fmt)
    ]
    # `command=%s` is key-first (not a kind) and `stop` heads a prose
    # sentence; the registered kind, the unregistered snake kind and the
    # bare kind word flag (the LOGGER.log form at line 10 too).
    assert flagged == [1, 2, 3, 4, 10]


def test_kind_shaped_needs_only_keyvalue_looking_tokens():
    assert _kind_shaped("brand_new", "brand_new a=1 b=2")
    assert not _kind_shaped("brand_new", "brand_new prose tail words")
    assert not _kind_shaped("Sentence case", "Sentence case x=1")
    assert "brand_new" not in journal.JOURNAL_EVENTS
    # The registered-name shortcut fires before the shape heuristic.
    assert _kind_shaped("run_end", "run_end run=x")
    assert _kind_shaped("merged", "merged")


def _kind_offenders(files: list[tuple[str, str]]) -> list[str]:
    """`name:line kind` for every direct kind-shaped LOGGER call."""
    offenders: list[str] = []
    for name, text in files:
        for lineno, first, fmt in _direct_logger_kind_lines(text):
            if _kind_shaped(first, fmt):
                offenders.append(f"{name}:{lineno} {first}")
    return offenders


def test_kind_offender_detector_reports_the_file_and_the_kind():
    offenders = _kind_offenders([
        ("clean.py", "LOGGER.info('stop scene activity snapshot failed')"),
        ("dirty.py", "LOGGER.info('run_end run=x')"),
    ])
    assert offenders == ["dirty.py:1 run_end"]


def test_no_module_outside_journal_and_cli_builds_kind_lines():
    exempt = {"journal.py", "cli.py"}
    files = [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(SRC.glob("*.py"))
        if path.name not in exempt
    ]
    offenders = _kind_offenders(files)
    assert not offenders, (
        "direct journal event lines outside journal.py/cli.py — emit "
        f"through journal.event() instead: {offenders}"
    )


def _unregistered_event_calls(source: str) -> list[tuple[int, str]]:
    """(lineno, kind) of journal.event calls with an unregistered kind."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "event"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "journal"):
            continue
        if not node.args:
            continue
        kind = node.args[0]
        if (isinstance(kind, ast.Constant)
                and isinstance(kind.value, str)
                and kind.value not in journal.JOURNAL_EVENTS):
            found.append((node.lineno, kind.value))
    return found


def test_event_call_detector_flags_unregistered_kinds_only():
    source = (
        "journal.event('run_end', run='x')\n"
        "journal.event(kind_variable)\n"
        "journal.event()\n"
        "journal.event('definitely_not_registered', x=1)\n"
        "other.event('also_unregistered')\n"
    )
    assert _unregistered_event_calls(source) == [(4, "definitely_not_registered")]


def _unregistered_event_offenders(
    files: list[tuple[str, str]],
) -> list[str]:
    """`name:line kind` for every journal.event literal kind outside the
    registry."""
    offenders: list[str] = []
    for name, text in files:
        for lineno, kind in _unregistered_event_calls(text):
            offenders.append(f"{name}:{lineno} {kind}")
    return offenders


def test_event_call_net_reports_unregistered_kinds_with_location():
    offenders = _unregistered_event_offenders([
        ("clean.py", "journal.event('run_end', run='x')"),
        ("dirty.py", "journal.event('typo_knd', x=1)"),
    ])
    assert offenders == ["dirty.py:1 typo_knd"]


def test_every_journal_event_call_uses_a_registered_kind():
    files = [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(SRC.glob("*.py"))
    ]
    offenders = _unregistered_event_offenders(files)
    assert not offenders, f"unregistered journal.event kinds: {offenders}"


# ---------------------------------------------------------------------------
# Exporter seam: the 10 metric kinds keep parsing through journal.event
# ---------------------------------------------------------------------------


# (kind, positional scene, event kwargs, the scene fields the exporter reads)
EXPORTER_EVENT_LINES = (
    ("run_start",
     "run=ab12cd34 issue=o/r#1 role=implement branch=b "
     "worktree=w session=s1 session_file=f1 phase=starting "
     "last_activity=- action=- result=-",
     {},
     {"issue": "o/r#1", "role": "implement", "phase": "starting"}),
    ("activity",
     None,
     dict(issue="o/r#1", role="implement", phase="test",
          action="run tests", result="-", state="running", idle="3s"),
     {"issue": "o/r#1", "phase": "test", "state": "running", "idle": "3s"}),
    ("heartbeat",
     None,
     dict(issue="o/r#1", role="implement", phase="test",
          state="running", elapsed="14m", idle="1s"),
     {"issue": "o/r#1", "phase": "test", "elapsed": "14m"}),
    ("model_wait",
     None,
     dict(issue="o/r#1", role="implement", phase="test",
          state="model_wait"),
     {"issue": "o/r#1", "state": "model_wait"}),
    ("pi_idle",
     None,
     dict(issue="o/r#1", role="implement", idle="6m", model_wait="false"),
     {"issue": "o/r#1"}),
    ("pi_idle_term",
     None,
     dict(issue="o/r#1", role="implement", pid="7",
          cmdline="timeout 30 pytest -q", result="sent"),
     {"issue": "o/r#1", "pid": "7", "result": "sent"}),
    ("pi_idle_kill",
     None,
     dict(issue="o/r#1", role="implement", pid="7",
          cmdline="timeout 30 pytest -q", result="sent"),
     {"issue": "o/r#1", "pid": "7", "result": "sent"}),
    ("run_failed",
     "run=ab12cd34 issue=o/r#1 role=implement branch=b "
     "worktree=w session=s1 session_file=f1 phase=test "
     "last_activity=- action=- result=-",
     dict(reason="pi_exit_1"),
     {"issue": "o/r#1", "reason": "pi_exit_1"}),
    ("run_end",
     "run=ab12cd34 issue=o/r#1 role=implement result=merged "
     "elapsed=14m pr=https://example/pr/1 commit=abc123",
     {},
     {"issue": "o/r#1", "role": "implement", "result": "merged",
      "elapsed": "14m"}),
    ("progress_publish_failed",
     None,
     dict(issue="o/r#1", reason="gh_api_down"),
     {"issue": "o/r#1"}),
)


@pytest.mark.parametrize(
    ("kind", "scene", "fields", "expected"), EXPORTER_EVENT_LINES,
)
def test_exporter_kind_keeps_parsing_through_event(
    kind, scene, fields, expected, caplog,
):
    """Each exporter kind emitted through journal.event keeps the exact
    `LEVEL [run_id] kind key=value ...` contract: the run id, the kind
    and every field the exporter reads survive the emission (Issue #791:
    exporter behavior unchanged)."""
    journal.set_run_id("ab12cd34")
    with caplog.at_level(logging.INFO, logger="orbi.bootstrap"):
        journal.event(kind, scene, **fields)
    record = caplog.records[0]
    message = record.message
    assert message.startswith(f"[ab12cd34] {kind} ")
    parsed = _exporter_module().parse_message(
        f"{logging.getLevelName(record.levelno)} {message}",
    )
    assert parsed is not None, message
    assert parsed["kind"] == kind
    assert parsed["run_id"] == "ab12cd34"
    for key, value in expected.items():
        assert parsed["scene"].get(key) == value, key
