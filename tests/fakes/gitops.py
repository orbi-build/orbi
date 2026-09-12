"""In-memory git fake at the adapter seam (Issue #789).

``FakeGit`` replaces the ``git`` process for the ``orbi.gitops``
adapter: branches (local + ``origin/*``), worktrees, and a commit DAG
for the ancestor checks. The test patches the ONE subprocess seam
(``monkeypatch.setattr(seam, "run_command", fake)``), grows the DAG
and refs with the helpers, runs the real gitops entry points
(``create_worktree``, ``freeze_base``, ``fetch_base_ref``,
``create_release_worktree``, ``_is_ancestor``), and asserts the public
surface (returned paths, branch heads, call records).

Real git semantics the fake preserves:

- ``git merge-base --is-ancestor a b`` exits 0 when `a` is reachable
  from `b`, exits 1 (empty stderr) when it is not, and exits 128 for
  an unknown object — the exact distinction `_is_ancestor` relies on;
- ``git worktree add -b`` on an existing branch exits 255 (the
  Issue #662 orphan-branch reuse contract);
- ``git ls-remote`` prints ``<sha>\\trefs/heads/<name>`` only for
  branches that exist on origin;
- every dispatched command is recorded in ``calls`` so a test can
  assert which data operations ran.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _key(path) -> str:
    return str(Path(path).absolute())


class FakeGit:
    """One repository's git state: DAG, branches, worktrees."""

    def __init__(self, repo_dir, *, base_branch: str = "main"):
        self.repo_dir = Path(repo_dir)
        self.parents: dict[str, list[str]] = {}
        self.local: dict[str, str] = {}
        self.origin: dict[str, str] = {}
        self.worktrees: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.base_branch = base_branch
        self.base_sha = self.commit()
        self.local[base_branch] = self.base_sha
        self.origin[base_branch] = self.base_sha

    # --- state arrangement -------------------------------------------------

    def commit(self, parents: list[str] | None = None) -> str:
        """Grow the DAG with one commit (child of `parents`) and return
        its sha. Deterministic names keep failure output readable."""
        parents = parents or []
        sha = f"c{len(self.parents) + 1:04d}"
        self.parents[sha] = list(parents)
        return sha

    def branch(self, name: str, sha: str) -> None:
        """Create the LOCAL branch `name` at `sha` (no worktree) — the
        orphan-branch scene a SIGKILLed run leaves behind (Issue #662)."""
        self.local[name] = sha

    # --- the git process seam ------------------------------------------------

    def __call__(self, command: list[str], *, cwd=None,
                 timeout=None) -> str:
        self.calls.append(list(command))
        if command[:1] != ["git"] or len(command) < 2:
            self._unsupported(command)
        verb = command[1]
        handler = {
            "ls-remote": self._ls_remote,
            "branch": self._branch_list,
            "fetch": self._fetch,
            "rev-parse": self._rev_parse,
            "merge-base": self._merge_base,
            "worktree": self._worktree,
            "reset": self._reset,
        }.get(verb)
        if handler is None:
            self._unsupported(command)
        return handler(command[2:], cwd=cwd)

    def _ls_remote(self, args: list[str], cwd) -> str:
        if args[:2] != ["--heads", "origin"] or len(args) != 3:
            self._unsupported(["git", "ls-remote", *args])
        ref = args[2]
        name = ref.removeprefix("refs/heads/")
        if name in self.origin and f"refs/heads/{name}" == ref:
            return f"{self.origin[name]}\t{ref}\n"
        return ""

    def _branch_list(self, args: list[str], cwd) -> str:
        if len(args) != 2 or args[0] != "--list":
            self._unsupported(["git", "branch", *args])
        name = args[1]
        return f"  {name}\n" if name in self.local else ""

    def _fetch(self, args: list[str], cwd) -> str:
        if len(args) != 2 or args[0] != "origin":
            self._unsupported(["git", "fetch", *args])
        name = args[1]
        if name not in self.origin:
            self._fail(
                128, f"fatal: couldn't find remote ref refs/heads/{name}"
            )
        return ""

    def _rev_parse(self, args: list[str], cwd) -> str:
        if len(args) != 1:
            self._unsupported(["git", "rev-parse", *args])
        # The adapter only ever reads `origin/<branch>` (freeze_base).
        name = args[0].removeprefix("origin/")
        if name != args[0] and name in self.origin:
            return self.origin[name]
        self._fail(128, f"fatal: ambiguous argument '{args[0]}'")

    def _merge_base(self, args: list[str], cwd) -> str:
        if args[:1] != ["--is-ancestor"] or len(args) != 3:
            self._unsupported(["git", "merge-base", *args])
        ancestor, descendant = args[1], args[2]
        if ancestor not in self.parents or descendant not in self.parents:
            self._fail(
                128, f"fatal: unknown object: {ancestor} or {descendant}"
            )
        if self.is_ancestor(ancestor, descendant):
            return ""
        # Real git answers the normal negative with exit code 1 and no
        # output — the case `_is_ancestor` maps to False.
        self._fail(1, "")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        seen: set[str] = set()
        stack = [descendant]
        while stack:
            sha = stack.pop()
            if sha == ancestor:
                return True
            if sha in seen:
                continue
            seen.add(sha)
            stack.extend(self.parents.get(sha, []))
        return False

    def _worktree(self, args: list[str], cwd) -> str:
        if args[:1] == ["list"]:
            if args[1:] == ["--porcelain"]:
                return self._worktree_list()
            self._unsupported(["git", "worktree", *args])
        if args[:1] != ["add"] or len(args) < 3:
            self._unsupported(["git", "worktree", *args])
        if "-b" in args:
            return self._worktree_add_new(args, cwd)
        return self._worktree_add_existing(args, cwd)

    def _worktree_list(self) -> str:
        blocks = [
            f"worktree {path}\nHEAD {state['head']}\n"
            f"branch refs/heads/{state['branch']}"
            for path, state in self.worktrees.items()
        ]
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def _worktree_add_new(self, args: list[str], cwd) -> str:
        # `git worktree add -b <branch> <path> <start-point>` (fresh
        # branch from the frozen base or from origin/<branch>).
        if len(args) != 5:
            self._unsupported(["git", "worktree", *args])
        _, branch, path, start = args[1:]
        if branch in self.local:
            # git exits 255 on an existing branch (Issue #608/#662).
            self._fail(
                255, f"fatal: a branch named '{branch}' already exists"
            )
        sha = self._start_point(start)
        self.local[branch] = sha
        self._register(path, branch, sha)
        return ""

    def _worktree_add_existing(self, args: list[str], cwd) -> str:
        # `git worktree add [--force] <path> <branch>` (reuse a branch
        # that already exists — the orphan/resume/takeover scenes).
        force = args[1:2] == ["--force"]
        rest = args[2:] if force else args[1:]
        if len(rest) != 2:
            self._unsupported(["git", "worktree", *args])
        path, branch = rest
        if branch not in self.local:
            self._fail(128, f"fatal: invalid reference: {branch}")
        self._register(path, branch, self.local[branch])
        return ""

    def _start_point(self, start: str) -> str:
        if start.startswith("origin/"):
            name = start[len("origin/"):]
            if name in self.origin:
                return self.origin[name]
            self._fail(128, f"fatal: invalid reference: {start}")
        if start in self.parents:
            return start
        self._fail(128, f"fatal: invalid reference: {start}")

    def _register(self, path, branch: str, sha: str) -> None:
        key = _key(path)
        self.worktrees[key] = {"branch": branch, "head": sha}
        Path(key).mkdir(parents=True, exist_ok=True)

    def _reset(self, args: list[str], cwd) -> str:
        if len(args) != 2 or args[0] != "--hard" or cwd is None:
            self._unsupported(["git", "reset", *args])
        target = args[1]
        if target not in self.parents:
            self._fail(128, f"fatal: ambiguous argument '{target}'")
        state = self.worktrees.get(_key(cwd))
        if state is None:
            self._fail(
                128, f"fatal: not a git repository: {_key(cwd)}"
            )
        state["head"] = target
        return ""

    # --- shared helpers -------------------------------------------------------

    def _fail(self, code: int, stderr: str):
        raise subprocess.CalledProcessError(
            code, ["git"], output="", stderr=stderr
        )

    def _unsupported(self, command: list[str]):
        self._fail(
            2, "fake-git: unsupported command: " + " ".join(command)
        )
