"""Behavioral tests for the patch ratchet gate (Issue #789).

The gate freezes the counts of ``monkeypatch.setattr`` calls that name
the runner module and of ``command[:N]`` equality assertions over the
test corpus, and fails a PR that raises either one. These tests drive
the tool exactly the way CI runs it — in-process ``main()`` calls with
the real exit code, plus one test over the REAL repository corpus
proving the committed baseline still matches (the standing "counts do
not rise" evidence).

The fixture literals are built by string concatenation so THIS file's
source never contains the ratcheted patterns — the gate's own tests
must not inflate the counts they freeze.
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

# The runner-patch literal, assembled so it never appears contiguously
# in this file.
PATCH_LINE = 'monkeypatch.setattr(' + 'runner, "run_command", fake)'
# The shape-assert literal — same trick.
SHAPE_LINE = 'assert command[:' + '3] == ["gh", "issue", "view"]'


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


def test_counts_both_patterns_and_excludes_fakes(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {
        "test_one.py": [PATCH_LINE, PATCH_LINE, SHAPE_LINE],
        "sub/test_two.py": [SHAPE_LINE, SHAPE_LINE],
        "fakes/github.py": [PATCH_LINE, SHAPE_LINE],
    })
    assert module.counts(tests) == {
        "monkeypatch_setattr_runner": 2,
        "command_shape_asserts": 3,
    }


def test_main_fails_when_a_count_rises_above_the_baseline(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [PATCH_LINE]})
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "monkeypatch_setattr_runner": 0,
        "command_shape_asserts": 0,
    }), encoding="utf-8")
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 1


def test_main_fails_when_only_the_shape_count_rises(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [SHAPE_LINE]})
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({
        "monkeypatch_setattr_runner": 99,
        "command_shape_asserts": 0,
    }), encoding="utf-8")
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 1


def test_main_passes_at_the_baseline_and_on_a_decrease(tmp_path, capsys):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": [PATCH_LINE]})
    baseline = tmp_path / "baseline.json"
    # Equal counts pass; lower counts pass too (a migration PR lands
    # before the baseline is re-frozen).
    baseline.write_text(json.dumps({
        "monkeypatch_setattr_runner": 1,
        "command_shape_asserts": 0,
    }), encoding="utf-8")
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0
    baseline.write_text(json.dumps({
        "monkeypatch_setattr_runner": 5,
        "command_shape_asserts": 5,
    }), encoding="utf-8")
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0
    # The report prints both real numbers (current/baseline).
    output = capsys.readouterr().out
    assert "monkeypatch.setattr(runner" in output
    assert ")=1/5" in output
    assert "command[:N]==" in output


def test_main_update_freezes_the_current_counts(tmp_path):
    module = load_ratchet()
    tests = make_corpus(
        tmp_path, {"test_one.py": [PATCH_LINE, SHAPE_LINE]},
    )
    baseline = tmp_path / "baseline.json"
    assert module.main(
        ["patch_ratchet.py", "--update", str(tests)],
        baseline_file=baseline,
    ) == 0
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "monkeypatch_setattr_runner": 1,
        "command_shape_asserts": 1,
    }
    # After the freeze the gate passes at the frozen counts.
    assert module.main(
        ["patch_ratchet.py", str(tests)], baseline_file=baseline,
    ) == 0


def test_main_fails_fast_without_a_baseline_file(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": []})
    assert module.main(
        ["patch_ratchet.py", str(tests)],
        baseline_file=tmp_path / "absent.json",
    ) == 1


def test_main_fails_fast_on_a_malformed_baseline(tmp_path):
    module = load_ratchet()
    tests = make_corpus(tmp_path, {"test_one.py": []})
    for content in (
        "not json at all",
        "[1, 2]",
        '{"monkeypatch_setattr_runner": "many", "command_shape_asserts": 0}',
        '{"monkeypatch_setattr_runner": 1}',
    ):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(content, encoding="utf-8")
        assert module.main(
            ["patch_ratchet.py", str(tests)], baseline_file=baseline,
        ) == 1, content


def test_frozen_baseline_matches_the_real_corpus():
    """The standing acceptance evidence: the committed baseline equals
    the real repository counts — the corpus has not grown (Issue #789)."""
    module = load_ratchet()
    assert module.main(["patch_ratchet.py"]) == 0
    baseline = json.loads(
        (TOOLS_DIR / "patch_ratchet_baseline.json").read_text(
            encoding="utf-8"
        )
    )
    assert baseline == module.counts(module.TESTS_DIR)


def test_ratchet_is_wired_into_ci_next_to_the_coverage_gate():
    """The gate runs in the CI workflow, in the same step sequence as
    the coverage gates (the Issue's wiring requirement)."""
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "python3 tools/patch_ratchet.py" in text
    coverage_index = text.index("tools/coverage_gate.py")
    diff_index = text.index("tools/diff_coverage_gate.py origin/main")
    ratchet_index = text.index("python3 tools/patch_ratchet.py")
    assert coverage_index < diff_index < ratchet_index
