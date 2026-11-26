"""Refuse an unstubbed external command at the test boundary (Issue #2253).

Every GitHub call and every network Git operation Orbi makes funnels through
the one subprocess seam `orbi.journal.run_command` (CONSTITUTION Article 3.4).
The suite replaces that seam with fakes, but a test whose stub no longer
matches the call path — or a test that simply never stubbed a read — used to
fall through to the real `gh` binary, with the contributor's (or the delivery
sandbox's) stored login. On 2026-11-25 that created nine Issues on this
repository from three tests.

This guard wraps the seam for every test:

- a `gh` command (read or write) is recorded and refused before any process
  is started, whatever the call path that reached it;
- a network Git command is refused the same way, while an intentional Git
  operation against a repository inside pytest's temp base is allowed;
- a test that replaces `subprocess.run` itself is exercising the seam's own
  contract below the boundary, so the guard delegates to it;
- the recorded calls also fail the owning test at teardown, so an unexpected
  call the code under test swallowed (a best-effort read) cannot pass.

The message names the command and the repair: stub it through
`monkeypatch.setattr(seam, "run_command", fake)`, or the existing fake for the
operation under test.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

# The process boundary as it exists before any test patches it. A test that
# patches `subprocess.run` (always through the same module object as
# `orbi.journal.subprocess`) has already stubbed below the seam.
_REAL_SUBPROCESS_RUN = subprocess.run

# Git verbs that reach a remote. Everything else (`status`, `commit`,
# `worktree`, `rev-parse`, `tag`, `config`, ...) is a local operation.
NETWORK_GIT_SUBCOMMANDS = frozenset({
    "clone", "fetch", "ls-remote", "pull", "push",
})


class UnstubbedExternalCommand(AssertionError):
    """A test reached the real `gh` or a network Git remote."""


@dataclass(frozen=True)
class ExternalCall:
    """One refused command and why it was refused."""

    command: tuple[str, ...]
    reason: str

    def render(self) -> str:
        return f"{self.reason}: {' '.join(self.command)}"


def process_boundary_is_stubbed() -> bool:
    """Whether a test already replaced `subprocess.run` below the seam."""
    return subprocess.run is not _REAL_SUBPROCESS_RUN


def _executable(command: list[str] | tuple[str, ...]) -> str:
    return Path(str(command[0])).name if command else ""


def git_subcommand(command: list[str] | tuple[str, ...]) -> str | None:
    """The Git verb of one argv, skipping the global options before it."""
    if _executable(command) != "git":
        return None
    arguments = list(command[1:])
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
        elif argument.startswith("-"):
            index += 1
        else:
            return argument
    return None


class ExternalCallGuard:
    """Wrap the subprocess seam with the test suite's network boundary."""

    def __init__(self, delegate, *, temp_root: Path):
        self._delegate = delegate
        self._temp_root = Path(temp_root)
        self.calls: list[ExternalCall] = []
        self._allowed = 0

    @contextmanager
    def allowed(self) -> Iterator[None]:
        """Opt one test into real process execution (Issue #2253).

        Only a test that exercises the subprocess boundary itself — a fake
        executable on PATH, a local bare repository — opts in. Never a test
        that merely forgot to stub the read/write under test.
        """
        self._allowed += 1
        try:
            yield
        finally:
            self._allowed -= 1

    def run(self, command, *args, **kwargs):
        """The seam stand-in: refuse an external command, else delegate."""
        self._refuse(command, kwargs.get("cwd"))
        return self._delegate(command, *args, **kwargs)

    def _refuse(self, command, cwd) -> None:
        reason = self.refusal_reason(command, cwd)
        if reason is None:
            return
        call = ExternalCall(tuple(str(part) for part in command), reason)
        self.calls.append(call)
        raise UnstubbedExternalCommand(self._message(call))

    def refusal_reason(self, command, cwd) -> str | None:
        """Why one command must not run in a test, or None when it may."""
        if self._allowed or not command or process_boundary_is_stubbed():
            return None
        if _executable(command) == "gh":
            return "unstubbed external gh command"
        if (
            git_subcommand(command) in NETWORK_GIT_SUBCOMMANDS
            and not self._is_temporary_repository(command, cwd)
        ):
            return "unstubbed network git command"
        return None

    def _is_temporary_repository(self, command, cwd) -> bool:
        """Whether the command only touches pytest's own temp base.

        A network Git verb is allowed when it runs inside the temp base, or
        when one of its path arguments (the clone source) resolves there: a
        test's intentional local bare repository is not the network.
        """
        root = self._temp_root.resolve()
        if cwd is not None:
            try:
                Path(cwd).resolve().relative_to(root)
                return True
            except ValueError:
                pass
        for argument in command[1:]:
            if str(argument).startswith("-"):
                continue
            try:
                Path(str(argument)).resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            return True
        return False

    def _message(self, call: ExternalCall) -> str:
        return (
            f"{call.render()}\n"
            "No process was started. Stub this call through the subprocess "
            "seam (`monkeypatch.setattr(seam, \"run_command\", fake)`) or the "
            "existing fake for the operation under test: a test may never run "
            "the real `gh` or reach a network Git remote (Issue #2253)."
        )

    def assert_no_unexpected_calls(self) -> None:
        """Fail the owning test even when the call's exception was swallowed."""
        if not self.calls:
            return
        listed = "\n".join(f"- {call.render()}" for call in self.calls)
        pytest.fail(
            "the test reached an unstubbed external command "
            f"({len(self.calls)} call(s)); no process was started:\n"
            f"{listed}",
            pytrace=False,
        )
