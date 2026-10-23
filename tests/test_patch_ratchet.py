"""Behavioral tests for the patch ratchet gate (Issues #789, #1856).

The gate hard-caps ``internal_total`` — adapter-method and domain-function
patches — and ``command_shape_asserts``. It reports ``boundary_total``
(the subprocess/process/time/filesystem seam) and ``per_target`` without
failing them: a boundary fake is legitimate, and moving a patch between
modules is neutral when the internal total does not rise.

These tests drive the tool exactly the way CI runs it — in-process
``main()`` calls with the real exit code, plus one test over the REAL
repository corpus proving the committed baseline still matches.

The fixture literals are built by string concatenation so THIS file's
source never contains the ratcheted patterns — the gate's own tests must
not inflate the counts they freeze.
"""
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

# Direct patches: target + name assembled so the source stays clean.
RUNNER_BOUNDARY = 'monkeypatch.setattr(' + 'runner, "run_command", fake)'
MODULE_INTERNAL = 'monkeypatch.setattr(' + 'journal, "x", fake)'
MODULE_WRAPPED = 'monkeypatch.setattr(' + '\n        journal, "x", fake)'
SEAM_BOUNDARY = 'monkeypatch.setattr(' + 'seam, "run_command", fake)'
SEAM_ADAPTER = 'monkeypatch.setattr(' + 'seam, "edit_issue", fake)'
SEAM_RANDOM = 'monkeypatch.setattr(' + 'seam, "new_run_id", fake)'
PROC_BOUNDARY = 'monkeypatch.setattr(' + 'pi_recovery, "PROC", fake)'
PATH_BOUNDARY = 'monkeypatch.setattr(' + 'Path, "write_text", fake)'
MODULE_QUALIFIED = 'monkeypatch.setattr(' + 'orbi.scheduler, "x", fake)'
RUNNER_QUALIFIED = 'monkeypatch.setattr(' + 'runner.github, "x", fake)'
MODULE_STRING = 'monkeypatch.setattr("orbi.' + 'release.x", fake)'
RUNNER_STRING = 'monkeypatch.setattr("orbi.' + 'runner.x", fake)'
FALSE_MODULE = 'monkeypatch.setattr(' + 'my_journal_helper, "x", fake)'
WRAPPED_RUNNER = 'monkeypatch.setattr(' + '\n        runner, "run_command", fake)'
SHAPE = 'assert command[:' + '3] == ["gh", "issue", "view"]'
STDLIB_WHICH = 'monkeypatch.setattr(' + 'shutil, "which", fake)'
STDLIB_FLOCK = 'monkeypatch.setattr(' + 'fcntl, "flock", fake)'
STDLIB_GETHOSTNAME = 'monkeypatch.setattr(' + 'socket, "gethostname", fake)'
STDLIB_ARGV = 'monkeypatch.setattr(' + 'sys, "argv", fake)'
QUALIFIED_STDLIB = 'monkeypatch.setattr(' + 'pilot_setup.shutil, "which", fake)'
QUALIFIED_STDLIB_TIME = 'monkeypatch.setattr(' + 'github.time, "sleep", fake)'
QUALIFIED_SUBMODULE = (
    'monkeypatch.setattr(' + 'pi_recovery.urllib.request, "urlopen", fake)'
)
CLASS_IMPORT = 'from http.server import HTTPServer'
CLASS_TARGET = 'monkeypatch.setattr(' + 'HTTPServer, "serve_forever", fake)'
ALIAS_IMPORT = 'import shutil as sh'
ALIAS_TARGET = 'monkeypatch.setattr(' + 'sh, "which", fake)'
OWNED_INTERNAL_IMPORT = 'from orbi import journal'
RELATIVE_IMPORT = 'from . import sibling'
DOMAIN_PATCH = 'monkeypatch.setattr(' + 'claim, "pick_next_delivery", fake)'


def load_ratchet():
    spec = importlib.util.spec_from_file_location(
        "patch_ratchet", TOOLS_DIR / "patch_ratchet.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_corpus(tmp_path: Path, files: dict[str, str]) -> Path:
    tests = tmp_path / "tests"
    for name, lines in files.items():
        path = tests / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # A valid body even with no patch lines: the scanner parses the
        # file's imports for target ownership (Issue #1868).
        path.write_text(
            "\n".join(
                ["def test_x():", "    pass",
                 *[f"    {line}" for line in lines]]
            ),
            encoding="utf-8",
        )
    return tests


def baseline_for(module, tests: Path, **overrides):
    baseline = module.counts(tests)
    baseline.update(overrides)
    path = tests.parent / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return path


def test_counts_split_boundary_from_internal(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [
            RUNNER_BOUNDARY, MODULE_INTERNAL, SEAM_BOUNDARY, SEAM_ADAPTER,
            SEAM_RANDOM, PROC_BOUNDARY, PATH_BOUNDARY, MODULE_STRING,
            RUNNER_STRING, FALSE_MODULE, SHAPE,
        ],
    })
    counts = module.counts(tests)
    # internal: journal.x, seam.edit_issue, release.x, runner.x,
    # my_journal_helper.x
    assert counts["internal_total"] == 5
    # boundary: runner.run_command, seam.run_command, seam.new_run_id,
    # pi_recovery.PROC, Path.write_text
    assert counts["boundary_total"] == 5
    assert counts["monkeypatch_setattr_runner"] == 1
    assert counts["monkeypatch_setattr_runner_string"] == 1
    assert counts["command_shape_asserts"] == 1
    assert counts["per_target"]["seam"] == 3
    assert counts["per_target"]["journal"] == 1


def test_counts_ignore_tests_fakes(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [MODULE_INTERNAL, SHAPE],
        "fakes/github.py": [MODULE_INTERNAL, SHAPE],
        "test_git_fakes.py": [RUNNER_BOUNDARY, SHAPE, SHAPE],
    })
    counts = module.counts(tests)
    assert counts["internal_total"] == 1
    assert counts["boundary_total"] == 1
    assert counts["command_shape_asserts"] == 1


def test_internal_growth_fails(tmp_path, capsys):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [MODULE_INTERNAL]})
    baseline = baseline_for(module, tests, internal_total=0)
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 1
    assert "internal_total" in capsys.readouterr().out


def test_command_shape_growth_fails(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [SHAPE]})
    baseline = baseline_for(module, tests, command_shape_asserts=0)
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 1


def test_boundary_growth_is_reported_but_passes(tmp_path, capsys):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [SEAM_BOUNDARY, PROC_BOUNDARY]})
    baseline = baseline_for(module, tests, boundary_total=0)
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 0
    output = capsys.readouterr().out
    assert "boundary=2" in output
    assert "internal=0" in output


def test_stdlib_module_targets_are_boundary(tmp_path):
    """A stdlib module target is the real external seam, never internal
    coupling (Issue #1868)."""
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_stdlib.py": [
            STDLIB_WHICH, STDLIB_FLOCK, STDLIB_GETHOSTNAME, STDLIB_ARGV,
        ],
    })
    counts = module.counts(tests)
    assert counts["boundary_total"] == 4
    assert counts["internal_total"] == 0


def test_qualified_stdlib_targets_are_boundary(tmp_path):
    """A qualified target owns its last segment: the stdlib module in the
    chain decides, at any depth (Issue #1868)."""
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_qualified.py": [
            QUALIFIED_STDLIB, QUALIFIED_STDLIB_TIME, QUALIFIED_SUBMODULE,
        ],
    })
    counts = module.counts(tests)
    assert counts["boundary_total"] == 3
    assert counts["internal_total"] == 0
    assert counts["per_target"]["shutil"] == 1
    assert counts["per_target"]["request"] == 1


def test_imported_class_and_alias_targets_are_boundary(tmp_path):
    """The test file's own imports own a class or alias target: the
    HTTP server class and an aliased stdlib module are boundaries, an
    orbi import stays internal (Issue #1868)."""
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_owned.py": [
            CLASS_IMPORT, CLASS_TARGET, ALIAS_IMPORT, ALIAS_TARGET,
            OWNED_INTERNAL_IMPORT, MODULE_INTERNAL, RELATIVE_IMPORT,
        ],
    })
    counts = module.counts(tests)
    assert counts["boundary_total"] == 2
    assert counts["internal_total"] == 1
    assert counts["per_target"]["HTTPServer"] == 1
    assert counts["per_target"]["sh"] == 1


def test_stdlib_stub_keeps_the_internal_count_and_lifts_boundary(tmp_path):
    """The Issue #1868 acceptance: against a baseline captured BEFORE the
    addition, one stdlib stub leaves internal unchanged, raises boundary
    and passes the gate."""
    module = load_ratchet()
    before = make_corpus(tmp_path / "before", {"test_one.py": [MODULE_INTERNAL]})
    baseline = module.counts(before)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    after = make_corpus(
        tmp_path / "after", {"test_one.py": [MODULE_INTERNAL, STDLIB_WHICH]},
    )
    current = module.counts(after)
    assert current["internal_total"] == baseline["internal_total"] == 1
    assert current["boundary_total"] == baseline["boundary_total"] + 1
    assert module.main(["patch_ratchet.py", str(after)], baseline_file=path) == 0


def test_new_adapter_or_domain_patch_still_exceeds_the_ceiling(tmp_path, capsys):
    """A stdlib boundary target must not weaken the hard cap: a new
    adapter method or domain function patch still fails (Issue #1868)."""
    module = load_ratchet()
    before = make_corpus(tmp_path / "before", {"test_one.py": [STDLIB_WHICH]})
    baseline = module.counts(before)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    assert module.main(["patch_ratchet.py", str(before)], baseline_file=path) == 0
    after = make_corpus(
        tmp_path / "after",
        {"test_one.py": [STDLIB_WHICH, SEAM_ADAPTER, DOMAIN_PATCH]},
    )
    assert module.main(["patch_ratchet.py", str(after)], baseline_file=path) == 1
    assert "internal_total 2 > 0" in capsys.readouterr().out


def test_per_target_rise_is_reported_but_passes(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [MODULE_INTERNAL]})
    baseline = baseline_for(module, tests, per_target={})
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 0


REPORT_PREFIX = "patch ratchet per-target (reported, not gated):"


def report_entries(output: str) -> dict[str, int]:
    """Parse the printed per-target report into an ordered name->count map."""
    line = next(
        line for line in output.splitlines() if line.startswith(REPORT_PREFIX)
    )
    entries: dict[str, int] = {}
    for token in line[len(REPORT_PREFIX):].split():
        name, _, count = token.partition("=")
        entries[name] = int(count)
    return entries


def test_per_target_report_prints_the_current_table(tmp_path, capsys):
    """Issue #1869: a normal invocation prints the CURRENT per_target
    table, not the frozen baseline table — a target that only exists in
    the current corpus is reported with its measured count.
    """
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [MODULE_INTERNAL, MODULE_INTERNAL, SEAM_ADAPTER,
                        RUNNER_BOUNDARY],
    })
    baseline = baseline_for(module, tests)
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    # Deliberately stale: "seam" is absent and the journal count is wrong,
    # so a report copied from the baseline cannot match the corpus.
    frozen["per_target"] = {"journal": 99}
    baseline.write_text(json.dumps(frozen), encoding="utf-8")
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0
    current = module.counts(tests)
    assert current["per_target"] == {"journal": 2, "runner": 1, "seam": 1}
    printed = report_entries(capsys.readouterr().out)
    assert printed == current["per_target"]
    # Stable order: descending count, then target name.
    assert list(printed) == ["journal", "runner", "seam"]


def test_per_target_report_is_printed_when_the_gate_fails(tmp_path, capsys):
    """Issue #1869: the hard-gate failure path carries the same report
    next to the aggregate counts and the failure reason.
    """
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [MODULE_INTERNAL, SEAM_ADAPTER],
    })
    baseline = baseline_for(module, tests, internal_total=0, per_target={})
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 1
    output = capsys.readouterr().out
    assert report_entries(output) == module.counts(tests)["per_target"]
    assert "internal=2/0" in output
    assert "patch ratchet FAILED" in output


def test_equal_total_relocation_passes(tmp_path):
    module = load_ratchet()
    baseline = module.counts(
        make_corpus(tmp_path / "base", {"test_one.py": [MODULE_INTERNAL]})
    )
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    relocated = make_corpus(
        tmp_path / "moved", {"test_one.py": ['monkeypatch.setattr(' + 'release, "x", fake)']}
    )
    assert module.main(["patch_ratchet.py", str(relocated)], baseline_file=path) == 0


def test_main_passes_at_the_baseline_and_on_a_decrease(tmp_path, capsys):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [MODULE_INTERNAL]})
    baseline = baseline_for(module, tests, internal_total=5, command_shape_asserts=5)
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 0
    output = capsys.readouterr().out
    assert "internal=1/5" in output
    assert "command[:N]==0/5" in output
    assert "boundary=" in output


def test_main_update_freezes_the_new_metrics(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [MODULE_INTERNAL, SHAPE]})
    baseline = tmp_path / "baseline.json"
    assert module.main(
        ["patch_ratchet.py", "--update", str(tests)], baseline_file=baseline,
    ) == 0
    frozen = json.loads(baseline.read_text(encoding="utf-8"))
    assert frozen["internal_total"] == 1
    assert frozen["boundary_total"] == 0
    assert frozen["command_shape_asserts"] == 1
    assert frozen["per_target"]["journal"] == 1
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 0


def test_main_fails_fast_without_a_baseline_file(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": []})
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=tmp_path / "absent.json",
    ) == 1


def test_main_fails_fast_on_a_malformed_or_old_baseline(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": []})
    for content in (
        "not json at all",
        "[1, 2]",
        '{"internal_total": "many", "command_shape_asserts": 0}',
        '{"internal_total": 1, "command_shape_asserts": 0}',
        '{"internal_total": 1, "boundary_total": 0, '
        '"monkeypatch_setattr_runner": 0, '
        '"monkeypatch_setattr_runner_string": 0, '
        '"command_shape_asserts": 0, "per_target": []}',
        # The pre-#1856 flat shape is intentionally unreadable.
        '{"monkeypatch_setattr_runner": 1, "command_shape_asserts": 1}',
    ):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(content, encoding="utf-8")
        assert module.main(
            ["patch_ratchet.py", str(tests)], baseline_file=baseline,
        ) == 1, content


def test_no_orbi_module_shares_a_name_with_a_stdlib_module():
    """The classification premise (Issue #1868): a target segment that
    names a stdlib module can never be an orbi module, so resolving
    ownership through Python's module list cannot hide an internal
    patch."""
    import sys

    stems = {
        path.stem for path in (REPO_ROOT / "src" / "orbi").glob("*.py")
    }
    assert stems
    assert not stems & set(sys.stdlib_module_names)


def test_frozen_baseline_matches_the_real_corpus():
    """The standing acceptance evidence: the committed baseline equals
    the real repository counts — the internal corpus has not grown."""
    module = load_ratchet()
    assert module.main(["patch_ratchet.py"]) == 0
    baseline = json.loads(
        (TOOLS_DIR / "patch_ratchet_baseline.json").read_text(encoding="utf-8")
    )
    assert baseline == module.counts(module.TESTS_DIR)


def test_wrapped_form_runner_patch_counts_as_boundary(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_wrapped.py": [WRAPPED_RUNNER]})
    counts = module.counts(tests)
    assert counts["monkeypatch_setattr_runner"] == 1
    assert counts["boundary_total"] == 1
    assert counts["internal_total"] == 0


def test_module_qualified_and_near_miss_targets(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_modules.py": [
            MODULE_WRAPPED, MODULE_QUALIFIED, RUNNER_QUALIFIED, FALSE_MODULE,
        ],
    })
    counts = module.counts(tests)
    assert counts["per_target"]["journal"] == 1
    assert counts["per_target"]["scheduler"] == 1
    assert counts["per_target"]["github"] == 1
    assert counts["per_target"]["my_journal_helper"] == 1
    assert counts["internal_total"] == 4


def test_main_rejects_more_than_one_argument(tmp_path):
    module = load_ratchet()
    assert module.main(["patch_ratchet.py", "a", "b"]) == 1


def test_main_rejects_a_missing_tests_directory(tmp_path):
    module = load_ratchet()
    assert module.main(
        ["patch_ratchet.py", str(tmp_path / "missing")],
    ) == 1


def test_string_form_boundary_name_is_boundary(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_string.py": [
            'monkeypatch.setattr("orbi.journal.' + 'run_command", fake)',
        ],
    })
    counts = module.counts(tests)
    assert counts["boundary_total"] == 1
    assert counts["internal_total"] == 0
