"""Issue #1367: Orbi's own `.orbi/` runtime dir must not make the checkout
'not clean', or every `orbi setup` retry after the first one fails.

The tests drive the real `check_checkout` against a REAL git repository:
only the two network commands (`git ls-remote`, `git fetch`) are answered by
the stub, while `git check-ignore`, the `info/exclude` write and the
`git status --porcelain` dirty check are the real CLI. A fake runner cannot
prove that the local exclude actually hides the runtime dir.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from orbi import journal, pilot_setup


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
    )


def _run(command, **kwargs):
    """The `run_command` seam: real git, network commands stubbed.

    The non-network commands go through the REAL `run_command`, so the
    journal lines its non-zero exits emit (the expected `check-ignore`
    probe included) are observable in these tests.
    """
    if "ls-remote" in command:
        return "abc\tHEAD"
    if "fetch" in command:
        return ""
    return journal.run_command(command, **kwargs)


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "git@github.com:orbi-build/orbi.git")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "init")
    # A local `origin/main` so the base comparison needs no network.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _check(repo: Path) -> dict:
    return pilot_setup.check_checkout(
        repo, "main", ["orbi-build/orbi"], run_command=_run,
    )


def _exclude_lines(repo: Path) -> list[str]:
    return (repo / ".git" / "info" / "exclude").read_text(
        encoding="utf-8",
    ).splitlines()


def test_orbi_runtime_dir_keeps_the_checkout_clean_and_is_pinned_once(checkout):
    orbi_dir = checkout / ".orbi"
    (orbi_dir / "slots").mkdir(parents=True)
    (orbi_dir / "base-sync.lock").write_text("", encoding="utf-8")
    (orbi_dir / "slots" / "x").write_text("", encoding="utf-8")

    assert _check(checkout)["clean"] is True
    # A retry re-runs the same check: the entry is not written twice.
    assert _check(checkout)["clean"] is True

    assert _exclude_lines(checkout).count(".orbi/") == 1


def test_modified_tracked_file_still_fails(checkout):
    (checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(pilot_setup.SetupError, match="not clean"):
        _check(checkout)


def test_untracked_root_file_still_fails(checkout):
    (checkout / "notes.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(pilot_setup.SetupError, match="not clean"):
        _check(checkout)


def test_orbi_ignored_by_repository_gitignore_leaves_exclude_untouched(checkout):
    (checkout / ".gitignore").write_text(".orbi/\n", encoding="utf-8")
    _git(checkout, "add", ".gitignore")
    _git(checkout, "commit", "-qm", "ignore .orbi")
    (checkout / ".orbi").mkdir()
    (checkout / ".orbi" / "base-sync.lock").write_text("", encoding="utf-8")
    exclude = checkout / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8")

    assert _check(checkout)["clean"] is True

    assert ".orbi/" not in _exclude_lines(checkout)
    assert exclude.read_text(encoding="utf-8") == before


def test_the_not_ignored_probe_is_not_reported_as_a_failure(checkout, caplog):
    """`git check-ignore` exit 1 is the branch the code handles itself
    ("not ignored yet"), never a failure: a successful `orbi setup` must
    not print the generic `command_failed` line at ERROR for it (the
    #341/#730/#1085 no-false-alarm contract for an expected non-zero
    probe — `run_command`'s `failure_log_level`)."""
    (checkout / ".orbi").mkdir()
    (checkout / ".orbi" / "base-sync.lock").write_text("", encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        assert _check(checkout)["clean"] is True

    assert [
        record for record in caplog.records
        if "command_failed" in record.getMessage()
        and record.levelno >= logging.ERROR
    ] == []
