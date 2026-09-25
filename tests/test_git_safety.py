"""Runner-side git calls disable repository-selected programs (#1366).

The Runner runs git itself inside worktrees the delivery agent could write
to, so the repository's LOCAL git config can name programs git then runs on
the Runner's behalf — the GitSpawn class: `core.fsmonitor`,
`core.hooksPath`, `diff.external` and the `filter.*` drivers named by a
`.gitattributes`. Every Runner-side call goes through
`orbi.journal.run_git` / `orbi.journal.run_git_network`, which prefix the
`-c` overrides that turn all four off.

Each real-git test below builds that hostile configuration and then shows
two things: `run_git` leaves the marker script unwritten, and the same
repository under a plain `git` invocation DOES write it. The control keeps
the test honest — without it a green assertion could just mean the trap
never worked.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import orbi.journal as journal
from conftest import git
from seam import seam

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

# The `-c` pairs every Runner-side git call must carry, in order.
SAFETY_OVERRIDES = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
]


def _script(path: Path, marker: Path, body: str = "") -> str:
    """A repository-selected program: appends to `marker` when it runs."""
    path.write_text(
        "#!/bin/sh\n"
        f'echo ran >> "{marker}"\n'
        f"{body}\n"
    )
    path.chmod(0o755)
    return str(path)


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "runner@localhost")
    git(repo, "config", "user.name", "Runner")
    return repo


def _capture(monkeypatch, command: list[str], **kwargs) -> list[str]:
    """The argv `run_git` handed to the process boundary."""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **run_kwargs):
        seen.append((list(cmd), run_kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(journal.subprocess, "run", fake_run)
    journal.run_git(command, **kwargs)
    assert len(seen) == 1
    return seen[0][0]


def _capture_call(monkeypatch, command: list[str], **kwargs):
    """The argv and the process kwargs `run_git` handed to the boundary."""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **run_kwargs):
        seen.append((list(cmd), run_kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(journal.subprocess, "run", fake_run)
    journal.run_git(command, **kwargs)
    assert len(seen) == 1
    return seen[0]


# --- the argv contract ------------------------------------------------------


def test_run_git_prefixes_every_call_with_the_safety_overrides(monkeypatch):
    argv = _capture(monkeypatch, ["git", "rev-parse", "HEAD"])
    assert argv == ["git", *SAFETY_OVERRIDES, "rev-parse", "HEAD"]


def test_run_git_forwards_the_cwd_and_kwargs(monkeypatch):
    _, process_kwargs = _capture_call(
        monkeypatch, ["git", "status"], cwd=Path("/x"), timeout=5,
    )
    assert process_kwargs["cwd"] == Path("/x")
    assert process_kwargs["timeout"] == 5


def test_run_git_uses_the_injected_command_runner():
    seen: list[list[str]] = []
    journal.run_git(
        ["git", "rev-parse", "HEAD"],
        command_runner=lambda cmd, **kw: seen.append(list(cmd)) or "",
    )
    assert seen[0] == ["git", *SAFETY_OVERRIDES, "rev-parse", "HEAD"]


def test_run_git_rejects_a_non_git_command():
    with pytest.raises(ValueError, match="run_git expects a git command"):
        journal.run_git(["gh", "release", "view"])
    with pytest.raises(ValueError, match="run_git expects a git command"):
        journal.run_git([])


@pytest.mark.parametrize("verb", ["diff", "log", "show"])
def test_verb_disables_textconv_and_external_diff(monkeypatch, verb):
    argv = _capture(monkeypatch, ["git", verb, "HEAD"])
    assert argv == [
        "git", *SAFETY_OVERRIDES, verb, "--no-textconv", "--no-ext-diff",
        "HEAD",
    ]


def test_status_gets_no_textconv_flags(monkeypatch):
    """`status` rejects them (rc=129), so they stay off every other verb."""
    argv = _capture(monkeypatch, ["git", "status", "--porcelain"])
    assert argv == ["git", *SAFETY_OVERRIDES, "status", "--porcelain"]


def test_global_options_are_skipped_when_locating_the_verb(monkeypatch):
    argv = _capture(
        monkeypatch, ["git", "-C", "/repo", "log", "--oneline"],
    )
    assert argv == [
        "git", *SAFETY_OVERRIDES, "-C", "/repo", "log",
        "--no-textconv", "--no-ext-diff", "--oneline",
    ]


def test_run_git_network_neutralises_and_keeps_the_network_retries():
    seen: list[list[str]] = []
    journal.run_git_network(
        ["git", "fetch", "origin", "main"],
        command_runner=lambda cmd, **kw: seen.append(list(cmd)) or "",
    )
    assert seen[0] == ["git", *SAFETY_OVERRIDES, "fetch", "origin", "main"]


def test_the_retry_classifier_sees_through_the_safety_prefix(monkeypatch):
    """A transient fetch failure still retries: the `-c` overrides must not
    hide the verb from `_is_retryable_git_network_failure`."""
    monkeypatch.setattr(journal.time, "sleep", lambda seconds: None)
    calls: list[list[str]] = []

    def failing(cmd, **kw):
        calls.append(list(cmd))
        raise subprocess.CalledProcessError(
            1, cmd, stderr="fatal: unable to access: Connection timed out",
        )

    with pytest.raises(subprocess.CalledProcessError):
        journal.run_git_network(
            ["git", "fetch", "origin", "main"], command_runner=failing,
        )
    assert len(calls) == journal.GIT_NETWORK_MAX_ATTEMPTS
    assert all("core.fsmonitor=false" in call for call in calls)


# --- the traps themselves, exercised against real git ------------------------


def test_a_configured_fsmonitor_never_runs(tmp_path):
    repo = _new_repo(tmp_path)
    marker = tmp_path / "fsmonitor.marker"
    script = _script(tmp_path / "fsmonitor.sh", marker, "echo version 2")
    git(repo, "config", "core.fsmonitor", script)

    git(repo, "status", "--porcelain")          # control: the trap is live
    assert marker.exists()
    marker.unlink()

    journal.run_git(["git", "status", "--porcelain"], cwd=repo)
    assert not marker.exists()


def test_a_repository_hook_never_runs(tmp_path):
    repo = _new_repo(tmp_path)
    marker = tmp_path / "hook.marker"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    _script(hooks / "pre-commit", marker)
    git(repo, "config", "core.hooksPath", str(hooks))

    (repo / "a.txt").write_text("one\n")
    journal.run_git(["git", "add", "a.txt"], cwd=repo)
    journal.run_git(["git", "commit", "-m", "runner-side"], cwd=repo)
    assert not marker.exists()

    (repo / "a.txt").write_text("two\n")        # control: the hook is live
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "plain")
    assert marker.exists()


def test_a_gitattributes_filter_never_runs(tmp_path):
    repo = _new_repo(tmp_path)
    marker = tmp_path / "filter.marker"
    git(repo, "config", "filter.spy.clean", _script(tmp_path / "clean.sh",
                                                   marker, "cat"))
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "a.txt").write_text("one\n")

    journal.run_git(["git", "add", "a.txt"], cwd=repo)
    assert not marker.exists()

    (repo / "b.txt").write_text("two\n")        # control: the filter is live
    git(repo, "add", "b.txt")
    assert marker.exists()


def test_a_filter_in_a_linked_worktree_is_neutralised(tmp_path):
    """The Runner works in linked worktrees (`gitdir:` pointer, config
    living in the common dir), so the driver lookup must follow it."""
    repo = _new_repo(tmp_path)
    marker = tmp_path / "filter.marker"
    git(repo, "config", "filter.spy.clean", _script(tmp_path / "clean.sh",
                                                   marker, "cat"))
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "attrs")

    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    (worktree / "a.txt").write_text("one\n")

    journal.run_git(["git", "add", "a.txt"], cwd=worktree)
    assert not marker.exists()

    (worktree / "b.txt").write_text("two\n")    # control: the filter is live
    git(worktree, "add", "b.txt")
    assert marker.exists()


# --- the neutraliser's own helpers ------------------------------------------


def test_the_verb_scan_skips_global_options():
    """The scan must see the verb even behind `-c` / `-C` / `--git-dir`."""
    assert journal._git_subcommand_index(
        ["git", "-c", "x=y", "status"]) == 3
    assert journal._git_subcommand_index(
        ["git", "--git-dir=/x", "status"]) == 2
    assert journal._git_subcommand_index(["git", "--", "status"]) == 2
    assert journal._git_subcommand_index(["git", "-c", "x=y"]) is None
    assert journal._git_subcommand_index(["gh", "api"]) is None
    assert journal._git_subcommand_index([]) is None
    assert journal._git_subcommand(["git", "status"]) == "status"


def test_the_cwd_helper_honours_a_dash_c_option(tmp_path):
    assert journal._git_command_cwd(["git", "-C", "/abs", "status"],
                                    None) == "/abs"
    assert journal._git_command_cwd(
        ["git", "-C", "rel", "add", "x"], Path("/repo"),
    ) == Path("/repo/rel")
    assert journal._git_command_cwd(["git", "status"],
                                    Path("/repo")) == Path("/repo")
    assert journal._git_command_cwd(["git", "status"], None) is None


def test_a_dash_c_worktree_filter_is_neutralised(tmp_path):
    """The Runner also calls `git -C <worktree>`, so the driver lookup
    must follow the option instead of the process directory."""
    repo = _new_repo(tmp_path)
    marker = tmp_path / "filter.marker"
    git(repo, "config", "filter.spy.clean", _script(tmp_path / "clean.sh",
                                                   marker, "cat"))
    (repo / ".gitattributes").write_text("*.txt filter=spy\n")
    (repo / "a.txt").write_text("one\n")

    journal.run_git(["git", "-C", str(repo), "add", "a.txt"],
                    cwd=tmp_path)
    assert not marker.exists()


def test_a_git_dir_file_without_a_pointer_is_not_a_repository(tmp_path):
    fake = tmp_path / "x"
    fake.mkdir()
    (fake / ".git").write_text("not a pointer\n")
    assert journal._git_config_files(fake) == ()
    assert journal._gitdir_pointer(fake / ".git", fake) is None
    # An unreadable "file" (a directory) is not a gitdir pointer either.
    assert journal._gitdir_pointer(tmp_path, tmp_path) is None


def test_the_filter_parser_reads_only_exec_keys_of_filter_sections():
    text = (
        "\n"
        "# comment\n"
        '[filter "spy"]\n'
        "    clean = cat\n"
        "    required = true\n"
        "    no equals here\n"
        "[core]\n"
        "    fsmonitor = true\n"
        '[filter "other"]\n'
        "    smudge = cat\n"
    )
    assert journal._filter_drivers(text) == {"spy", "other"}


def test_the_seam_normaliser_tolerates_a_zero_argument_call(monkeypatch):
    """The fan-out fake may also be called with no command at all."""
    seen: list[tuple] = []
    monkeypatch.setattr(seam, "run_command",
                        lambda *args, **kwargs: seen.append(args))
    seam.run_command()
    seam.run_command(["git", "-c", "core.fsmonitor=false", "status"])
    assert seen == [(), (["git", "status"],)]


# --- the repo-wide lint: no module may call git directly ---------------------


def _starts_with_git(node: ast.AST) -> bool:
    """Whether a node is a list literal whose first element is `"git"`."""
    return (
        isinstance(node, ast.List)
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "git"
    )


def _git_argv_names(scope: ast.AST) -> set[str]:
    """Names the scope binds to a git argv (`command = ["git", ...]`)."""
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _starts_with_git(node.value):
            names.update(
                target.id for target in node.targets
                if isinstance(target, ast.Name)
            )
    return names


_EXEC_HELPERS = frozenset({
    "run_command", "run_git_network_command", "run", "check_output",
    "check_call", "call", "Popen",
})


def _is_direct_git_call(node: ast.Call, bound: set[str]) -> bool:
    """Whether one call is a subprocess seam handed a git command."""
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        owner = func.value
        name = (func.attr if isinstance(owner, ast.Name)
                and owner.id == "subprocess" else None)
    else:
        name = None
    if name not in _EXEC_HELPERS or not node.args:
        return False
    first = node.args[0]
    if _starts_with_git(first):
        return True
    return isinstance(first, ast.Name) and first.id in bound


def _scope_calls(scope: ast.AST, bound: set[str]) -> list[int]:
    """Direct git calls of one scope, without entering a nested def."""
    offenders: list[int] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Call) and _is_direct_git_call(node, bound):
            offenders.append(node.lineno)
        stack.extend(ast.iter_child_nodes(node))
    return offenders


def _direct_git_calls(source: str) -> list[int]:
    """Line numbers of direct git calls that bypass `run_git`.

    Every Runner-side git call must go through `run_git` /
    `run_git_network`, so a new call site cannot forget the overrides.
    The direct forms are a literal `["git", ...]` argument and a
    variable the same scope bound to one (`command = ["git", ...]` then
    `run_command(command)`).
    """
    tree = ast.parse(source)
    offenders = _scope_calls(tree, _git_argv_names(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders += _scope_calls(node, _git_argv_names(node))
    return sorted(offenders)


def test_no_runner_module_calls_git_directly_outside_journal():
    """The lint itself (Issue #1366): the only git seam is journal.py."""
    offenders = {
        path.name: _direct_git_calls(path.read_text(encoding="utf-8"))
        for path in sorted(SRC_DIR.glob("*.py"))
        if path.name != "journal.py"
    }
    assert {name: lines for name, lines in offenders.items() if lines} == {}


def test_the_lint_reports_a_direct_git_call():
    """The failure branch: a regenerated direct call site is reported."""
    assert _direct_git_calls(
        'def f(run_command):\n    return run_command(["git", "status"])\n'
    ) == [2]
    assert _direct_git_calls(
        'subprocess.run(["git", "push", "origin", "main"])\n'
    ) == [1]
    assert _direct_git_calls('journal.run_command(["git", "status"])\n') == []
    assert _direct_git_calls('x = run_command(["gh", "api", "-q", "x"])\n') == []
    assert _direct_git_calls('x = run_git(["git", "status"])\n') == []
    assert _direct_git_calls("x = run_command(command)\n") == []
    assert _direct_git_calls("x = run_command([])\n") == []
    # A call through a non-name, non-attribute callee is not a seam.
    assert _direct_git_calls('handlers[0](["git", "status"])\n') == []
    # A variable argv is as direct as a literal one.
    assert _direct_git_calls(
        'def f(run_command):\n'
        '    command = ["git", "rev-list", "--count", "head"]\n'
        '    return run_command(command)\n'
    ) == [3]
    assert _direct_git_calls(
        'def f(run_command):\n'
        '    command = ["gh", "api", "x"]\n'
        '    return run_command(command)\n'
    ) == []
