"""Behavioral tests for the tiered coverage gate (Issue #234).

The repository contract is tiered, not a single repo-wide 100% number:

- whole repository: line >= 95% AND branch >= 95%, checked SEPARATELY
  (never a single merged percentage);
- changed Python code: 100% line/branch on the lines and branches this
  PR adds or modifies (a doc-only change has no changed Python and
  passes);
- no unjustified `# pragma: no cover` (only the existing, locatable
  `__main__`-guard / defensive-except pragmas are allowed).

These tests construct REAL coverage results (coverage.py data files
built from real fixture modules, the same arc data a real
`coverage run --branch` produces) and REAL git fixtures, then run the
gate scripts exactly the way the CI workflow runs them — the gate's
exit code is the assertion.
"""
import contextlib
import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import coverage

REPO_ROOT = Path(__file__).resolve().parent.parent
GLOBAL_GATE = REPO_ROOT / "coverage_gate.py"
DIFF_GATE = REPO_ROOT / "diff_coverage_gate.py"
PYTHON = sys.executable

# A fixture module with 21 statement lines (1-19, 22, 23) and exactly
# 2 branches (the `if` on line 17: arcs (17, 18) and (17, 19)). The
# module-level arcs are (1, 22), (22, -1), (23, -22) and the straight
# body arcs are (2, 3) ... (16, 17); the returns exit with (18, -1)
# and (19, -1). Verified against the coverage.py 7.14 file reporter
# (`file_reporter.lines()` / `file_reporter.arcs()`).
FIXTURE_MODULE = """\
def core():
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    g = 7
    h = 8
    i = 9
    j = 10
    k = 11
    l = 12
    m = 13
    n = 14
    o = 15
    if a > 0:
        return 1
    return 2


def fallback():
    return 2
"""

# All 21 statements executed, both branches taken: 100% / 100%.
FULL_ARCS = (
    [(1, 22)]
    + [(i, i + 1) for i in range(2, 17)]
    + [(17, 18), (17, 19), (18, -1), (19, -1), (22, -1), (23, -22)]
)
# Both statements d=4 and e=5 missing (arcs (3, 4), (4, 5), (5, 6)
# absent): line = 19/21 = 90.48% (< 95), branch = 100%.
LINE_BELOW_ARCS = (
    [(1, 22)]
    + [(2, 3)]
    + [(i, i + 1) for i in range(6, 17)]
    + [(17, 18), (17, 19), (18, -1), (19, -1), (22, -1), (23, -22)]
)
# All statements executed except line 19 (only the True side of the if
# taken): line = 20/21 = 95.24% (>= 95, the line tier PASSES) but
# branch = 1/2 = 50% (< 95, the branch tier must FAIL on its own —
# the tiers are checked separately, never as a merged percentage).
BRANCH_BELOW_ARCS = (
    [(1, 22)]
    + [(i, i + 1) for i in range(2, 17)]
    + [(17, 18), (18, -1), (22, -1), (23, -22)]
)


def build_coverage_data(tmp_path: Path, module: Path,
                        arcs: tuple[tuple[int, int], ...]) -> Path:
    """Record the given executed arcs for `module` in a coverage data
    file and return the data file path. The arcs are real branch arcs
    of the fixture module (the same shape `coverage run --branch`
    records), so the resulting percentages are the real coverage
    numbers. The data key is the absolute file path — the same shape a
    real run records; `coverage json` reports it repo-relative."""
    cov = coverage.Coverage(branch=True, data_file=tmp_path / ".coverage")
    data = cov.get_data()
    data.add_arcs({str(module.resolve()): [tuple(arc) for arc in arcs]})
    data.write()
    return tmp_path / ".coverage"


def run_gate(gate: Path, *args: str, data_file: Path,
             cwd: Path) -> subprocess.CompletedProcess:
    """Run a gate script the way CI runs it (real subprocess, real
    exit code)."""
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(data_file)
    return subprocess.run(
        [PYTHON, str(gate), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def test_gate_scripts_exist_at_repository_root():
    assert GLOBAL_GATE.is_file(), f"missing global gate: {GLOBAL_GATE}"
    assert DIFF_GATE.is_file(), f"missing diff gate: {DIFF_GATE}"


def test_global_gate_fails_when_line_is_below_95(tmp_path):
    module = tmp_path / "fixture.py"
    module.write_text(FIXTURE_MODULE, encoding="utf-8")
    # line = 19/21 = 90.48% (< 95), branch = 100%.
    data = build_coverage_data(tmp_path, module, LINE_BELOW_ARCS)
    result = run_gate(GLOBAL_GATE, data_file=data, cwd=tmp_path)
    assert result.returncode != 0, (
        f"the gate must fail when line < 95% (line=90.48%), got exit "
        f"{result.returncode}: {result.stdout!r} {result.stderr!r}"
    )
    # The failure names the failing tier with its real number.
    assert "line=90.48%" in result.stdout
    assert "line 90.48% < 95" in result.stdout


def test_global_gate_fails_when_only_branch_is_below_95(tmp_path):
    module = tmp_path / "fixture.py"
    module.write_text(FIXTURE_MODULE, encoding="utf-8")
    # line = 20/21 = 95.24% (>= 95) but branch = 1/2 = 50% (< 95): the
    # gate must fail on the BRANCH tier alone — the line tier passes.
    data = build_coverage_data(tmp_path, module, BRANCH_BELOW_ARCS)
    result = run_gate(GLOBAL_GATE, data_file=data, cwd=tmp_path)
    assert result.returncode != 0, (
        f"the gate must fail when branch < 95% (branch=50%, line=95.24%), "
        f"got exit {result.returncode}: {result.stdout!r} "
        f"{result.stderr!r}"
    )
    assert "line=95.24%" in result.stdout
    assert "branch=50.00%" in result.stdout
    # The line tier passed; only the branch tier failed.
    assert "branch 50.00% < 95" in result.stdout
    assert "line 95.24% < 95" not in result.stdout


def test_global_gate_passes_when_line_and_branch_are_at_least_95(tmp_path):
    module = tmp_path / "fixture.py"
    module.write_text(FIXTURE_MODULE, encoding="utf-8")
    # line = 100%, branch = 100%.
    data = build_coverage_data(tmp_path, module, FULL_ARCS)
    result = run_gate(GLOBAL_GATE, data_file=data, cwd=tmp_path)
    assert result.returncode == 0, (
        f"the gate must pass when line >= 95% AND branch >= 95%, got "
        f"exit {result.returncode}: {result.stdout!r} {result.stderr!r}"
    )
    # The report prints BOTH real numbers (line and branch).
    assert "line=100.00%" in result.stdout
    assert "branch=100.00%" in result.stdout


def test_global_gate_fails_fast_when_no_coverage_data_exists(tmp_path):
    result = run_gate(GLOBAL_GATE, data_file=tmp_path / "absent",
                      cwd=tmp_path)
    assert result.returncode != 0, (
        "a missing coverage data file must fail the gate, not pass "
        f"silently: {result.stdout!r} {result.stderr!r}"
    )


# --- the changed-code (diff) gate -------------------------------------------

DIFF_FIXTURE = """\
def existing():
    return 1
"""


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_git_fixture(tmp_path: Path) -> Path:
    """A real git repo: one base commit on `main`, then a task branch
    (the diff gate's HEAD)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.email", "gate@test", cwd=repo)
    git("config", "user.name", "Gate Test", cwd=repo)
    (repo / "module.py").write_text(DIFF_FIXTURE, encoding="utf-8")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    git("branch", "task", cwd=repo)
    git("checkout", "task", cwd=repo)
    return repo


def coverage_json_for(
        repo: Path,
        arcs: tuple[tuple[int, int], ...],
        module_name: str = "module.py",
) -> None:
    """Build the coverage data the diff gate reads, from real branch
    arcs of the fixture module (the same construction the CI
    `coverage json` step consumes). The data key is the absolute path;
    `coverage json` reports it repo-relative, matching the git diff
    paths."""
    module = (repo / module_name).resolve()
    cov = coverage.Coverage(branch=True, data_file=repo / ".coverage")
    data = cov.get_data()
    data.add_arcs({str(module): [tuple(arc) for arc in arcs]})
    data.write()


def test_diff_gate_fails_on_an_uncovered_changed_python_line(tmp_path):
    repo = make_git_fixture(tmp_path)
    # The task branch adds a function the tests never execute.
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef untested():\n    return 42\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add uncovered line", cwd=repo)
    # Coverage: only the base line 2 is executed; the new lines
    # 5-6 are not. (5 = `def untested():`, 6 = `return 42`.)
    coverage_json_for(repo, [(2, -1)])
    result = run_gate(DIFF_GATE, "main", data_file=repo / ".coverage",
                      cwd=repo)
    assert result.returncode != 0, (
        f"the diff gate must fail when a changed Python line is "
        f"uncovered, got exit {result.returncode}: {result.stdout!r} "
        f"{result.stderr!r}"
    )
    assert "module.py" in result.stdout


def test_diff_gate_fails_on_an_uncovered_changed_branch(tmp_path):
    repo = make_git_fixture(tmp_path)
    # The task branch adds an if with only the True side exercised.
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef partial(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add uncovered branch", cwd=repo)
    # Executed: 2 (base), 5 (def), 6 (if), 7 (return 1); missing line
    # 8 (return 2) and missing arc (6, 8).
    coverage_json_for(repo, [(2, -1), (5, -1), (6, 7), (7, -5)])
    result = run_gate(DIFF_GATE, "main", data_file=repo / ".coverage",
                      cwd=repo)
    assert result.returncode != 0, (
        f"the diff gate must fail when a changed branch is uncovered, "
        f"got exit {result.returncode}: {result.stdout!r} "
        f"{result.stderr!r}"
    )
    assert "module.py" in result.stdout


def test_diff_gate_passes_when_every_changed_python_line_is_covered(tmp_path):
    repo = make_git_fixture(tmp_path)
    # The task branch adds a fully tested function.
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef tested(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add covered function", cwd=repo)
    # Every new statement line executed, both branches covered.
    coverage_json_for(repo, [(2, -1), (5, -1), (6, 7), (6, 8), (7, -5),
                             (8, -5)])
    result = run_gate(DIFF_GATE, "main", data_file=repo / ".coverage",
                      cwd=repo)
    assert result.returncode == 0, (
        f"the diff gate must pass when every changed Python line and "
        f"branch is covered, got exit {result.returncode}: "
        f"{result.stdout!r} {result.stderr!r}"
    )


def test_diff_gate_passes_on_a_doc_only_change(tmp_path):
    repo = make_git_fixture(tmp_path)
    # Only the README changes: no changed Python -> the gate passes
    # without tripping on the (empty) Python diff.
    (repo / "README.md").write_text("# fixture\n\nmore docs\n",
                                    encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "docs only", cwd=repo)
    coverage_json_for(repo, [(2, -1)])
    result = run_gate(DIFF_GATE, "main", data_file=repo / ".coverage",
                      cwd=repo)
    assert result.returncode == 0, (
        f"a doc-only change must not trip the Python diff gate, got "
        f"exit {result.returncode}: {result.stdout!r} {result.stderr!r}"
    )
    assert "doc-only" in result.stdout.lower()


def test_diff_gate_fails_fast_when_the_base_ref_is_unknown(tmp_path):
    repo = make_git_fixture(tmp_path)
    result = run_gate(DIFF_GATE, "no-such-ref",
                      data_file=repo / ".coverage", cwd=repo)
    assert result.returncode != 0, (
        f"an unknown base ref must fail the diff gate, got exit "
        f"{result.returncode}: {result.stdout!r} {result.stderr!r}"
    )


# --- in-process coverage of the gate scripts themselves ---------------------
#
# The subprocess tests above prove the gates' exit codes the way CI
# sees them; the in-process calls below execute the gate code in the
# test process so the changed-code gate (100% on this PR's Python)
# covers it too.


def load_gate_module(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coverage_gate_main_passes_and_fails_in_process(tmp_path):
    module = load_gate_module("coverage_gate")
    fixture = tmp_path / "fixture.py"
    fixture.write_text(FIXTURE_MODULE, encoding="utf-8")
    # Pass: both tiers at 100%.
    good = build_coverage_data(tmp_path, fixture, FULL_ARCS)
    assert module.main(["coverage_gate.py", str(good)]) == 0
    # Fail: the line tier below 95%.
    low_line = build_coverage_data(tmp_path, fixture, LINE_BELOW_ARCS)
    assert module.main(["coverage_gate.py", str(low_line)]) == 1
    # Fail: the branch tier below 95%.
    low_branch = build_coverage_data(tmp_path, fixture, BRANCH_BELOW_ARCS)
    assert module.main(["coverage_gate.py", str(low_branch)]) == 1
    # Fail fast: no coverage data at all.
    assert module.main(["coverage_gate.py", str(tmp_path / "absent")]) == 1


def test_coverage_gate_main_fails_when_the_report_cannot_run(tmp_path):
    module = load_gate_module("coverage_gate")
    # A corrupt data file makes `coverage json` exit non-zero: the gate
    # must fail, not pass on unreadable coverage evidence.
    data = tmp_path / ".corrupt"
    data.write_text("not a coverage data file", encoding="utf-8")
    assert module.main(["coverage_gate.py", str(data)]) == 1


def test_diff_gate_main_in_process(tmp_path, monkeypatch):
    module = load_gate_module("diff_coverage_gate")
    # The in-process `coverage json` must read the fixture repo's own
    # data file, never an outer run's.
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    # Doc-only change: passes without any Python diff.
    (repo / "README.md").write_text("# fixture\n\ndocs\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "docs", cwd=repo)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 0
        # Unknown base ref: git fails -> the gate fails fast.
        assert module.main(["diff_coverage_gate.py", "no-such-ref"]) == 1
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_fails_on_uncovered_change_in_process(tmp_path,
                                                            monkeypatch):
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef untested():\n    return 42\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "uncovered", cwd=repo)
    coverage_json_for(repo, [(2, -1)])
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 1
    finally:
        os.chdir(old_cwd)


def test_diff_gate_changed_python_lines_parses_the_real_diff(tmp_path):
    module = load_gate_module("diff_coverage_gate")
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef untested():\n    return 42\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "change", cwd=repo)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        changed = module.changed_python_lines("main")
    finally:
        os.chdir(old_cwd)
    # The added lines are 3 (blank), 4 (blank), 5 (def), 6 (return).
    assert changed == {"module.py": {3, 4, 5, 6}}


def test_diff_gate_changed_python_lines_counts_context_lines_in_process(
        tmp_path):
    """A diff that MODIFIES an existing file carries context lines;
    the parser must count them when numbering the added lines."""
    module = load_gate_module("diff_coverage_gate")
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        "def existing():\n    return 1\n\n\ndef extra():\n    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "modify", cwd=repo)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        changed = module.changed_python_lines("main")
    finally:
        os.chdir(old_cwd)
    # Two context lines, then the four added lines 3-6.
    assert changed == {"module.py": {3, 4, 5, 6}}


def test_diff_gate_changed_python_lines_ignores_deleted_lines_in_process(
        tmp_path):
    """A diff that DELETES lines: the `-` lines are neither counted
    nor recorded (only additions define the changed lines)."""
    module = load_gate_module("diff_coverage_gate")
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        "def existing():\n    return 2\n\n\ndef extra():\n    return 3\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "modify and delete", cwd=repo)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        changed = module.changed_python_lines("main")
    finally:
        os.chdir(old_cwd)
    # The hunk is `@@ -1,2 +1,6 @@`: context `def existing():`,
    # deleted `    return 1`, added `    return 2`, blank, blank,
    # `def extra():`, `    return 3` -> added lines 2, 3, 4, 5, 6.
    assert changed == {"module.py": {2, 3, 4, 5, 6}}


def test_diff_gate_coverage_files_fails_when_the_report_cannot_run_in_process(
        tmp_path, monkeypatch):
    """`coverage json` exits non-zero without a data file: the gate
    reads None, never a guessed report."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # no .coverage data here
        assert module.coverage_files() is None
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_fails_when_the_coverage_report_cannot_run_in_process(
        tmp_path, monkeypatch):
    """Changed Python + unreadable coverage evidence: fail fast, never
    pass on missing evidence."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef untested():\n    return 42\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "uncovered", cwd=repo)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)  # no .coverage data -> `coverage json` fails
        assert module.main(["diff_coverage_gate.py", "main"]) == 1
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_fails_when_a_changed_file_has_no_coverage_data_in_process(
        tmp_path, monkeypatch):
    """Coverage data exists but not for the changed file: every changed
    line counts as uncovered."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef untested():\n    return 42\n",
        encoding="utf-8",
    )
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "uncovered", cwd=repo)
    # Data for a DIFFERENT module: module.py has no coverage entry.
    coverage_json_for(repo, [(1, -1)], module_name="other.py")
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 1
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_fails_on_an_uncovered_changed_branch_in_process(
        tmp_path, monkeypatch):
    """A missing arc that starts at a changed line fails the gate (the
    same contract the subprocess test pins, exercised in-process)."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef partial(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add uncovered branch", cwd=repo)
    # Executed: 2 (base), 5 (def), 6 (if), 7 (return 1); missing line
    # 8 (return 2) and missing arc (6, 8).
    coverage_json_for(repo, [(2, -1), (5, -1), (6, 7), (7, -5)])
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 1
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_ignores_a_missing_branch_at_an_unchanged_line_in_process(
        tmp_path, monkeypatch):
    """A missing arc that starts at a line the change did NOT touch is
    pre-existing debt: the diff gate reports only changed code, so it
    passes."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    # The base already carries an if with only the True side exercised
    # (missing arc (2, 4), missing line 4); the task branch only adds
    # a fully covered function. `task` is reset onto the debt commit so
    # the diff is exactly the added function (lines 6-10).
    git("checkout", "main", cwd=repo)
    (repo / "module.py").write_text(
        "def existing(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base with debt", cwd=repo)
    git("checkout", "-B", "task", cwd=repo)
    (repo / "module.py").write_text(
        "def existing(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n"
        "\n\ndef tested(flag):\n"
        "    if flag:\n"
        "        return 3\n"
        "    return 4\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add covered function", cwd=repo)
    # Executed: 1 (def), 2 (if), 3 (return 1) — the base debt stays
    # (missing line 4, missing arc (2, 4)); 7-10 (the new function)
    # fully covered, both branches.
    coverage_json_for(repo, [(1, 2), (2, 3), (3, -1), (7, 8), (8, 9),
                             (8, 10), (9, -7), (10, -7)])
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 0
    finally:
        os.chdir(old_cwd)


def test_diff_gate_main_passes_when_every_changed_line_is_covered_in_process(
        tmp_path, monkeypatch):
    """Every changed line and branch covered: the gate passes (the
    success path, exercised in-process)."""
    module = load_gate_module("diff_coverage_gate")
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    repo = make_git_fixture(tmp_path)
    (repo / "module.py").write_text(
        DIFF_FIXTURE + "\n\ndef tested(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add covered function", cwd=repo)
    # Every new statement line executed, both branches covered.
    coverage_json_for(repo, [(2, -1), (5, -1), (6, 7), (6, 8), (7, -5),
                             (8, -5)])
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        assert module.main(["diff_coverage_gate.py", "main"]) == 0
    finally:
        os.chdir(old_cwd)


# --- the pragma rule (Issue #234 requirement 6) -----------------------------

# The only allowed `# pragma: no cover` sites: the `__main__` entry
# guards (never executed by the test suite by definition) and the one
# defensive `except Exception` guard in the CLI install test. Every
# other pragma would hide a branch from the report and is a contract
# violation.
ALLOWED_PRAGMA_FILES = (
    "src/muyan_pilot/runner.py",
    "src/muyan_pilot/cli.py",
    "monitoring/prometheus/muyan-pilot-exporter.py",
    "coverage_gate.py",
    "diff_coverage_gate.py",
    "tests/test_cli_install.py",
)


def find_pragma_offenders(paths, allowed=ALLOWED_PRAGMA_FILES) -> list[str]:
    """Scan the given .py files for unjustified `# pragma: no cover`
    (Issue #234 requirement 6) and return `path: line` offenders."""
    offenders = []
    # The rule-defining test files quote the pragma string to pin the
    # contract; they are not repository source.
    rule_files = {
        Path(__file__).resolve(),
        REPO_ROOT / "tests" / "test_agents_md.py",
    }
    for path in sorted(paths):
        if ".git" in path.parts or "htmlcov" in path.parts:
            continue
        if path in rule_files:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "# pragma: no cover" not in line:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative not in allowed:
                offenders.append(f"{relative}: {line.strip()}")
            elif "if __name__ == \"__main__\"" not in line and (
                    "except Exception" not in line
            ):
                offenders.append(f"{relative}: {line.strip()}")
    return offenders


def test_no_unjustified_pragma_no_cover_in_the_repository():
    offenders = find_pragma_offenders(REPO_ROOT.rglob("*.py"))
    assert not offenders, (
        f"unjustified `# pragma: no cover` (Issue #234: exceptions must "
        f"stay locatable, never hidden): {offenders}"
    )


@contextlib.contextmanager
def _repo_scratch_file(name: str) -> Iterator[Path]:
    """A scratch .py file inside the repository (the pragma scan keys
    on repo-relative paths); removed on exit, even on failure."""
    path = REPO_ROOT / "tests" / name
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_pragma_scan_flags_an_unjustified_pragma():
    """A pragma outside the allowed files is an offender (the scan's
    negative path, exercised against a real scratch file)."""
    with _repo_scratch_file("_scratch_violation.py") as violation:
        violation.write_text(
            "def f():\n    if f():\n        pass  # pragma: no cover\n",
            encoding="utf-8",
        )
        offenders = find_pragma_offenders([violation])
    assert len(offenders) == 1
    assert "tests/_scratch_violation.py" in offenders[0]


def test_pragma_scan_flags_a_disguised_pragma_in_an_allowed_file():
    """An allowed file may only carry the `__main__` guard or the
    defensive `except Exception` pragma — any other line is an
    offender (the scan's second negative path)."""
    # The allowed list is injected so the scratch file's relative path
    # counts as an allowed entry: the scan keys on the relative path,
    # so this exercises the allowed-file branch without touching the
    # real file.
    with _repo_scratch_file("_scratch_gate.py") as scratch:
        scratch.write_text(
            "def f():\n    if f():\n        pass  # pragma: no cover\n",
            encoding="utf-8",
        )
        offenders = find_pragma_offenders(
            [scratch], allowed=("tests/_scratch_gate.py",),
        )
    assert len(offenders) == 1
    assert "tests/_scratch_gate.py" in offenders[0]


def test_pragma_scan_accepts_the_main_guard_and_except_guard():
    """The two justified pragma forms in an allowed file are not
    offenders."""
    with _repo_scratch_file("_scratch_gate.py") as scratch:
        scratch.write_text(
            'if __name__ == "__main__":  # pragma: no cover\n'
            "    sys.exit(main())\n"
            "except Exception:  # pragma: no cover\n"
            "    pass\n",
            encoding="utf-8",
        )
        assert find_pragma_offenders(
            [scratch], allowed=("tests/_scratch_gate.py",),
        ) == []


def test_pragma_scan_skips_generated_directories(tmp_path):
    """Generated directories (`.git`, `htmlcov`) are never scanned,
    even when they carry a pragma (the scan's skip path)."""
    pragma = "def f():\n    if f():\n        pass  # pragma: no cover\n"
    # `.git` (a tmp path suffices: skipped paths are never read).
    dotgit = tmp_path / ".git" / "hooks"
    dotgit.mkdir(parents=True)
    (dotgit / "x.py").write_text(pragma, encoding="utf-8")
    assert find_pragma_offenders([dotgit / "x.py"]) == []
    # `htmlcov` inside the repository (the real generated location).
    generated = REPO_ROOT / "tests" / "htmlcov"
    generated.mkdir(exist_ok=True)
    scratch = generated / "_scratch_generated.py"
    try:
        scratch.write_text(pragma, encoding="utf-8")
        assert find_pragma_offenders([scratch]) == []
    finally:
        scratch.unlink(missing_ok=True)
        generated.rmdir()
