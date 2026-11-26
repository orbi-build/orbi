"""The test suite's own contract (Issue #908).

Four guards keep the suite honest: no test module may bind the same
function name twice at module scope (the later def silently replaces
the earlier one and pytest collects the surviving name once — the
shadowed body never runs), the ONE shared fail-fast git scaffold in
``conftest.py`` must fail loudly on a non-zero exit (previously
asserted by 13 per-file copies of the same test), the fake ``pi``
executable has one owner under ``tests/fakes/``, and the autouse
CLI-install preflight patches the seam once (Issue #2009: the seam
fan-out already reaches every module that binds the name, so the second
patch was dead code the next reader had to explain).
"""
import ast
import shutil
from pathlib import Path

import pytest

from conftest import git
from external_guard import (
    ExternalCallGuard, UnstubbedExternalCommand, git_subcommand,
)
from seam import seam

TESTS_DIR = Path(__file__).resolve().parent


def _duplicate_module_level_defs(tree: ast.Module) -> list[tuple[str, int]]:
    """Module-scope def names bound more than once in one module: the
    later def silently replaces the earlier one, and pytest collects
    the surviving name once — the shadowed body never runs."""
    seen: set[str] = set()
    duplicates: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                duplicates.append((node.name, node.lineno))
            seen.add(node.name)
    return duplicates


def test_no_test_module_defines_a_function_name_twice():
    """Walk the AST of every test module and fail on any duplicate
    module-scope function name (Issue #908: the dict-era
    ``test_resolve_policy_context_files_are_additive`` copy had never
    executed)."""
    offenders = [
        f"{path.name}:{lineno} {name}"
        for path in sorted(TESTS_DIR.rglob("*.py"))
        for name, lineno in _duplicate_module_level_defs(
            ast.parse(path.read_text(encoding="utf-8"), str(path))
        )
    ]
    assert offenders == []


def test_the_walker_reports_a_shadowed_duplicate():
    """The guard's failure branch: a module that binds the same name
    twice is reported with the shadowing def's line."""
    tree = ast.parse(
        "def dup():\n    return 1\n\n\ndef dup():\n    return 2\n",
        "<synthetic>",
    )
    assert _duplicate_module_level_defs(tree) == [("dup", 5)]


def test_git_helper_fails_fast_on_nonzero_exit(tmp_path):
    """The one shared git scaffold (tests/conftest.py) must fail loudly
    on a git error, never pass a broken setup silently."""
    with pytest.raises(AssertionError, match=r"git .* failed rc=128"):
        git(tmp_path, "rev-parse", "no-such-ref")


def test_install_fake_pi_is_defined_once_under_fakes():
    """The fake Pi executable has one owner (Issue #2009): every e2e suite
    imports it from `tests/fakes/`, so a behaviour fix lands once instead of
    in a copy per module."""
    definitions = [
        f"{path.relative_to(TESTS_DIR).as_posix()}:{node.lineno}"
        for path in sorted(TESTS_DIR.rglob("*.py"))
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), str(path))
        )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "install_fake_pi"
    ]
    assert [definition.split(":")[0] for definition in definitions] == [
        "fakes/pi.py",
    ]


def test_the_cli_install_fixture_patches_the_seam_once():
    """`_default_cli_install_preflight` installs the `refresh_cli_install`
    no-op exactly once (Issue #2009): the seam fan-out already reaches every
    module that binds the name, so a second patch is dead code that the next
    reader has to explain."""
    tree = ast.parse(
        (TESTS_DIR / "conftest.py").read_text(encoding="utf-8"),
    )
    fixture = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_default_cli_install_preflight"
    )
    patches = [
        node for node in ast.walk(fixture)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "seam"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "refresh_cli_install"
    ]
    assert len(patches) == 1



def test_external_guard_refuses_an_unstubbed_gh_write_before_any_process(tmp_path):
    """A gh write the test did not stub is refused with the command named
    and no process started (Issue #2253)."""
    started: list = []
    guard = ExternalCallGuard(
        lambda command, **kwargs: started.append(command) or "",
        temp_root=tmp_path,
    )
    with pytest.raises(
        UnstubbedExternalCommand,
        match=r"gh issue create --repo owner\.invalid/repo",
    ):
        guard.run(
            ["gh", "issue", "create", "--repo", "owner.invalid/repo"],
            cwd=tmp_path,
        )
    assert started == []
    assert guard.calls[0].render() == (
        "unstubbed external gh command: gh issue create "
        "--repo owner.invalid/repo"
    )


def test_external_guard_refuses_network_git_but_allows_a_local_repository(
        tmp_path):
    """A network Git verb is refused outside the temp base, while the same
    verb against a temporary local repository is the intentional operation
    the suite allows (Issue #2253)."""
    started: list = []
    guard = ExternalCallGuard(
        lambda command, **kwargs: started.append(command) or "",
        temp_root=tmp_path,
    )
    with pytest.raises(
        UnstubbedExternalCommand,
        match="unstubbed network git command: git fetch origin",
    ):
        guard.run(["git", "fetch", "origin"], cwd=tmp_path.parent)
    repository = tmp_path / "repository"
    repository.mkdir()
    assert guard.run(["git", "status"], cwd=repository) == ""
    assert guard.run(["git", "fetch", "origin"], cwd=repository) == ""
    assert started == [
        ["git", "status"], ["git", "fetch", "origin"],
    ]


def test_external_guard_still_fails_when_the_code_swallows_the_refusal(
        tmp_path):
    """Recording is what makes a best-effort read a failure: the test that
    swallowed the refusal still fails at teardown (Issue #2253)."""
    guard = ExternalCallGuard(lambda *a, **k: "", temp_root=tmp_path)
    try:
        guard.run(["gh", "issue", "list"], cwd=tmp_path)
    except UnstubbedExternalCommand:
        pass  # the code under test's best-effort handler
    with pytest.raises(
        pytest.fail.Exception,
        match="unstubbed external gh command: gh issue list",
    ):
        guard.assert_no_unexpected_calls()


def test_a_seam_stub_intercepts_the_guarded_read(monkeypatch):
    """The control: a properly stubbed read reaches the fake, not the
    guard — the guard only fails the test that forgot the stub (Issue
    #2253)."""
    from orbi import github

    seen: list = []
    monkeypatch.setattr(
        seam, "run_command",
        lambda command, **kwargs: seen.append(command) or "[]",
    )
    assert github.list_issues(
        "owner.invalid/repo", json_fields="number", limit=100,
    ) == []
    assert seen and seen[0][:3] == ["gh", "issue", "list"]


def test_the_offline_sentinel_owns_gh_and_a_private_home(
        offline_external_sentinel):
    """The bypass net: the gh name resolves to the recording sentinel and
    HOME carries no stored login (Issue #2253)."""
    sentinel = offline_external_sentinel
    assert Path(shutil.which("gh")) == sentinel.gh
    assert Path.home() == sentinel.home
    assert not (sentinel.home / ".config" / "gh").exists()



def test_git_subcommand_skips_the_global_options_before_the_verb():
    """The guard reads the Git verb past the options (Issue #2253)."""
    assert git_subcommand(["git", "-C", "/repo", "fetch", "origin"]) == "fetch"
    assert git_subcommand(["git", "-c", "user.name=x", "status"]) == "status"
    assert git_subcommand(["git", "--no-pager", "status"]) == "status"
    assert git_subcommand(["git", "--version"]) is None
    assert git_subcommand([]) is None
    assert git_subcommand(["gh", "fetch"]) is None


def test_external_guard_delegates_when_the_process_boundary_is_stubbed(
        monkeypatch, tmp_path):
    """A test that replaced subprocess.run itself has stubbed below the
    seam, so the guard delegates (Issue #2253)."""
    import subprocess

    started: list = []
    guard = ExternalCallGuard(
        lambda command, **kwargs: started.append(command) or "ok",
        temp_root=tmp_path,
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    assert guard.run(["gh", "issue", "list"], cwd=tmp_path) == "ok"
    assert started == [["gh", "issue", "list"]]


def test_external_guard_delegates_an_empty_command_and_an_opt_in(tmp_path):
    """No command means nothing to refuse, and allowed() opts one test
    into real process execution (Issue #2253)."""
    started: list = []
    guard = ExternalCallGuard(
        lambda command, **kwargs: started.append(command) or "ok",
        temp_root=tmp_path,
    )
    assert guard.run([], cwd=tmp_path) == "ok"
    with guard.allowed():
        assert guard.run(["gh", "issue", "list"], cwd=tmp_path) == "ok"
    assert guard.calls == []
    assert started == [[], ["gh", "issue", "list"]]



def test_external_guard_skips_option_arguments_and_a_missing_cwd(tmp_path):
    """The temp-base walk ignores option arguments, and a call without a
    cwd is not a temporary repository (Issue #2253)."""
    guard = ExternalCallGuard(lambda *a, **k: "", temp_root=tmp_path)
    with pytest.raises(
        UnstubbedExternalCommand,
        match="unstubbed network git command: git fetch -q origin",
    ):
        guard.run(["git", "fetch", "-q", "origin"])


def test_mirror_installed_cli_covers_both_branches(tmp_path):
    """The private HOME mirrors the installed CLI when there is one, and
    does nothing when there is none (Issue #2253)."""
    from external_guard import mirror_installed_cli

    mirror_installed_cli(tmp_path, None)
    assert not (tmp_path / ".local").exists()
    executable = tmp_path / "bin" / "orbi"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    mirror_installed_cli(tmp_path, str(executable))
    assert (tmp_path / ".local" / "bin" / "orbi").resolve() == (
        executable.resolve()
    )


def test_offline_sentinel_failure_reports_a_bypass_only(tmp_path):
    """An absent or empty sentinel log is clean; a recorded bypass fails
    the session with its command (Issue #2253)."""
    from external_guard import (
        assert_no_offline_sentinel_calls, offline_sentinel_failure,
    )

    log = tmp_path / "calls.log"
    assert offline_sentinel_failure(log) is None
    log.write_text("", encoding="utf-8")
    assert offline_sentinel_failure(log) is None
    log.write_text("issue list --repo owner.invalid/repo\n", encoding="utf-8")
    failure = offline_sentinel_failure(log)
    assert failure is not None and "issue list" in failure
    with pytest.raises(pytest.fail.Exception, match="offline"):
        assert_no_offline_sentinel_calls(log)


def test_progress_gh_fake_answers_each_publisher_command(monkeypatch):
    """The publisher fake answers the list/POST/PATCH traffic and fails
    loudly on anything else (Issue #2253)."""
    import json

    from tests.fakes.github import install_progress_gh

    install_progress_gh(monkeypatch, comments=[{"id": 1}])
    assert json.loads(seam.run_command(
        ["gh", "api", "repos/o/r/issues/1/comments"],
    )) == [{"id": 1}]
    assert seam.run_command([
        "gh", "api", "repos/o/r/issues/comments/1",
        "--method", "PATCH", "--field", "body=x",
    ]) == ""
    posted = json.loads(seam.run_command([
        "gh", "api", "repos/o/r/issues/1/comments",
        "--method", "POST", "--field", "body=hello",
    ]))
    assert posted["body"] == "hello"
    raw = json.loads(seam.run_command([
        "gh", "api", "repos/o/r/issues/1/comments",
        "--method", "POST", "--field", "labels=x",
    ]))
    assert raw["body"] == "labels=x"
    assert json.loads(seam.run_command([
        "gh", "issue", "view", "1", "--repo", "o/r", "--json", "comments",
    ])) == {"comments": [{"id": 1}]}
    with pytest.raises(AssertionError, match="unexpected external command"):
        seam.run_command(["gh", "pr", "view", "1"])


def test_empty_issue_comments_fake_delegates_other_commands(monkeypatch):
    """The timeline fake answers the comments read and delegates every
    other command to the seam below it (Issue #2253)."""
    import json

    from tests.fakes.github import install_empty_issue_comments

    install_empty_issue_comments(monkeypatch)
    assert json.loads(seam.run_command([
        "gh", "issue", "view", "1", "--repo", "o/r", "--json", "comments",
    ])) == {"comments": []}
    assert "git version" in seam.run_command(["git", "--version"])
