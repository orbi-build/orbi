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
STDLIB_HOSTNAME = 'monkeypatch.setattr(' + 'socket, "gethostname", fake)'
STDLIB_ARGV = 'monkeypatch.setattr(' + 'sys, "argv", fake)'
STDLIB_SERVE = 'monkeypatch.setattr(' + 'HTTPServer, "serve_forever", fake)'


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
        path.write_text(
            "\n".join(["def test_x():", *[f"    {line}" for line in lines]]),
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


def test_per_target_rise_is_reported_but_passes(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [MODULE_INTERNAL]})
    baseline = baseline_for(module, tests, per_target={})
    assert module.main(["patch_ratchet.py", str(tests)], baseline_file=baseline) == 0


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


def test_stdlib_module_targets_are_boundary(tmp_path):
    """A stdlib module target is a boundary fake: shutil.which,
    socket.gethostname and sys.argv must never be hard-capped as
    internal coupling (Issue #1856, option B).
    """
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_stdlib.py": [
            STDLIB_WHICH, STDLIB_HOSTNAME, STDLIB_ARGV, STDLIB_SERVE,
        ],
    })
    counts = module.counts(tests)
    assert counts["boundary_total"] == 4
    assert counts["internal_total"] == 0
    baseline = baseline_for(module, tests, internal_total=0)
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0


def test_per_target_table_is_printed(tmp_path, capsys):
    """Issue #1856 option B asks for the per-target report in the CI
    output: a split PR must see where the coupling sits, not only the
    total.
    """
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [MODULE_INTERNAL, SEAM_ADAPTER],
    })
    baseline = baseline_for(module, tests)
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0
    output = capsys.readouterr().out
    assert "patch ratchet per-target (reported, not gated):" in output
    assert "journal=1" in output
    assert "seam=1" in output


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
