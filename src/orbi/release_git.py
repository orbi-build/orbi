"""Release machine git writes: the version bump and its base-branch push.

The `prepare_release_version` step of the release state machine, the git
identity every local Git object it writes uses, and the push of the
prepared version commit to the shared base branch. Two independent Runner
instances preparing the same version race on that push, so
`push_prepared_release_version` makes the loser idempotent (Issue #1289)
instead of failing the release ticket on work already landed.

The seams are `orbi.journal.run_command` /
`orbi.journal.run_git_network_command` and the base-sync-locked base fetch
of `orbi.gitops`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from orbi.gitops import fetch_base_ref
from orbi.journal import run_command, run_git_network_command

# Identity used for every local Git object written by the release state
# machine. Cloud sandboxes intentionally do not provide a user Git config.
RELEASE_GIT_IDENTITY = ("Orbi", "orbi@localhost")


def run_git_write(
    args: list[str], cwd: Path,
) -> str | subprocess.CompletedProcess[str]:
    """Run a local Git write with the release machine's stable identity."""
    env = os.environ.copy()
    name, email = RELEASE_GIT_IDENTITY
    env.update({
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    })
    return run_command(args, cwd=cwd, env=env)


# Supported `version_file` declaration values: the ecosystem metadata
# files (written by `prepare_release_version`) plus `none` — skip version
# metadata changes and tag the frozen base HEAD directly.
# The only release tag shape (prepare_release_version has enforced it at
# execution time since v0.3; resolve enforces it at claim time so a bad
# Milestone title fails before the gates burn their wait budgets).
RELEASE_VERSION_TAG_RE = re.compile(r"v([0-9]+(?:\.[0-9]+)+)")

RELEASE_VERSION_FILE_OPTIONS = (
    "pyproject.toml", "package.json", "pom.xml", "build.gradle",
    "build.gradle.kts", "gradle.properties", "Cargo.toml",
    "composer.json", "pubspec.yaml", "none",
)


# Markers Git prints when a push is rejected because the remote ref
# advanced (a concurrent instance pushed first). The rejection alone cannot
# tell "another instance already landed this exact version" from a genuine
# conflict — the fetch-and-compare below distinguishes them (Issue #1289).
_GIT_PUSH_REJECTED_MARKERS = (
    "[rejected]", "fetch first", "non-fast-forward", "updates were rejected",
)


class ReleaseVersionAlreadyLanded(RuntimeError):
    """A concurrent release instance already pushed this version commit.

    Two independent Runner instances may prepare the same version from the
    same frozen base; the loser's plain push to the shared base branch is
    rejected with `(fetch first)`. After a fetch the remote head proves
    content-equivalent to this run's version commit, so the version bump is
    already landed and this run yields instead of failing the ticket
    (Issue #1289).
    """

    def __init__(self, tag: str, base_branch: str, remote_commit: str):
        self.tag = tag
        self.base_branch = base_branch
        self.remote_commit = remote_commit
        super().__init__(
            f"release {tag}: the concurrent release instance already pushed "
            f"the same version commit {remote_commit} to {base_branch}"
        )


def _push_was_rejected(exc: subprocess.CalledProcessError) -> bool:
    """Return whether a `git push` was rejected by an advanced remote ref."""
    stderr = (exc.stderr or "").lower()
    return any(marker in stderr for marker in _GIT_PUSH_REJECTED_MARKERS)


def push_prepared_release_version(
    worktree: Path, tag: str, base_branch: str, repo_dir: Path,
) -> None:
    """Push the prepared version commit to the shared release base.

    Idempotent across concurrent instances (Issue #1289): a plain push is
    the release machine's write to the shared base branch, and two instances
    preparing the same version race on it. When the push is rejected because
    the remote advanced, fetch the base once (under the base-sync lock,
    because the fetch updates the shared remote-tracking ref) and compare
    trees: an identical tree means another instance already landed this
    exact version commit — this run yields (`ReleaseVersionAlreadyLanded`);
    any other remote head is a genuine conflict and the original push
    failure is re-raised.
    """
    try:
        run_git_network_command(
            ["git", "push", "origin", f"HEAD:refs/heads/{base_branch}"],
            cwd=worktree,
        )
        return
    except subprocess.CalledProcessError as exc:
        if not _push_was_rejected(exc):
            raise
        push_error = exc
    local_tree = run_command(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=worktree,
    ).strip()
    fetch_base_ref(repo_dir, base_branch, cwd=worktree)
    remote_tree = run_command(
        ["git", "rev-parse", f"origin/{base_branch}^{{tree}}"], cwd=worktree,
    ).strip()
    if remote_tree != local_tree:
        raise push_error
    remote_commit = run_command(
        ["git", "rev-parse", f"origin/{base_branch}"], cwd=worktree,
    ).strip()
    raise ReleaseVersionAlreadyLanded(tag, base_branch, remote_commit)


def prepare_release_version(worktree: Path, tag: str,
                            base_branch: str,
                            version_file: str = "pyproject.toml",
                            repo_dir: Path | None = None) -> str:
    """Commit the tag's version into the declared metadata source.

    The release tag is the public identity (for example ``v0.3.0``), while
    metadata version fields omit the leading ``v``.  The selected source
    must be structurally recognizable before it is changed; the commit is
    pushed directly to the release base, matching the release docs-sync step.
    A push rejected by a concurrent release instance that already landed the
    same version commit yields (`ReleaseVersionAlreadyLanded`); `repo_dir`
    names the deployment checkout that owns the shared base-sync lock (it
    defaults to the worktree for direct callers).
    """
    match = RELEASE_VERSION_TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError(
            f"release version {tag!r} must be a v-prefixed numeric tag"
        )
    version = match.group(1)
    if version_file not in RELEASE_VERSION_FILE_OPTIONS:
        raise ValueError("release version_file is not supported")
    if version_file == "none":
        return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()
    if version_file in ("package.json", "composer.json"):
        package_json = worktree / version_file
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"release version source {version_file} is not valid JSON"
            ) from exc
        if not isinstance(package_data, dict) or not isinstance(
            package_data.get("version"), str
        ) or not package_data["version"]:
            raise RuntimeError(
                f"release version source {version_file} must contain a non-empty "
                "version field"
            )
        if package_data["version"] != version:
            package_data["version"] = version
            package_json.write_text(
                json.dumps(package_data, indent=2) + "\n", encoding="utf-8",
            )
            run_command(["git", "add", version_file], cwd=worktree)
            run_git_write([
                "git", "commit", "-m", f"chore: prepare release {tag}",
            ], cwd=worktree)
            push_prepared_release_version(
                worktree, tag, base_branch, repo_dir or worktree,
            )
        return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()
    if version_file != "pyproject.toml":
        source = worktree / version_file
        try:
            text = source.read_text(encoding="utf-8")
            if version_file == "pom.xml":
                ET.fromstring(text)
                matches = list(re.finditer(
                    r"<version>\s*([^<\s]+)\s*</version>", text,
                ))
                parents = [m.span() for m in re.finditer(
                    r"<parent\b.*?</parent>", text, re.DOTALL,
                )]
                matches = [m for m in matches if not any(
                    start <= m.start() < end for start, end in parents
                )]
                # Maven projects normally contain additional dependency
                # versions.  The declaration contract selects the first
                # version outside the parent block, not a uniquely occurring
                # version in the whole document.
                pattern = matches[0] if matches else None
                replacement = rf"<version>{version}</version>"
            elif version_file == "Cargo.toml":
                data = tomllib.loads(text)
                current = data.get("package", {}).get("version")
                if not isinstance(current, str) or not current:
                    raise ValueError("missing [package].version")
                pattern = re.search(
                    r"(?ms)^(\[package\][^\[]*?^version\s*=\s*)"
                    r"([\"'])[^\n]+?\2\s*$",
                    text,
                )
                replacement = None
            elif version_file == "pubspec.yaml":
                matches = list(re.finditer(
                    r"(?m)^version\s*:\s*([^#\s]+)", text,
                ))
                pattern = matches[0] if len(matches) == 1 else None
                replacement = f"version: {version}"
            elif version_file == "gradle.properties":
                matches = list(re.finditer(
                    r"(?m)^version\s*=\s*([^#\s]+)", text,
                ))
                pattern = matches[0] if len(matches) == 1 else None
                replacement = f"version={version}"
            else:
                matches = list(re.finditer(
                    r"(?m)^([ \t]*version\s*=\s*)(['\"])([^'\"]+)\2[ \t]*$",
                    text,
                ))
                pattern = matches[0] if len(matches) == 1 else None
                # Groovy accepts either quote style, while Kotlin DSL only
                # accepts double quotes. Preserve the source syntax.
                replacement = (
                    pattern.group(1) + pattern.group(2) + version
                    + pattern.group(2)
                    if pattern is not None else ""
                )
            if pattern is None:
                raise ValueError("version declaration is not uniquely parseable")
            if version_file == "Cargo.toml":
                replacement = pattern.group(1) + f'"{version}"'
            updated = text[:pattern.start()] + replacement + text[pattern.end():]
        except (OSError, ET.ParseError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"release version source {version_file} has no parseable version"
            ) from exc
        if updated != text:
            source.write_text(updated, encoding="utf-8")
            run_command(["git", "add", version_file], cwd=worktree)
            run_git_write([
                "git", "commit", "-m", f"chore: prepare release {tag}",
            ], cwd=worktree)
            push_prepared_release_version(
                worktree, tag, base_branch, repo_dir or worktree,
            )
        return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()
    pyproject = worktree / version_file
    init_file = worktree / "src" / "orbi" / "__init__.py"
    pyproject_text = pyproject.read_text(encoding="utf-8")
    init_text = init_file.read_text(encoding="utf-8")
    py_matches = re.findall(
        r'(?m)^version\s*=\s*"([^"]+)"\s*$', pyproject_text,
    )
    init_matches = re.findall(
        r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', init_text,
    )
    if len(py_matches) != 1 or len(init_matches) != 1:
        raise RuntimeError(
            "release version sources must contain exactly one version "
            "declaration each"
        )
    if py_matches[0] != init_matches[0]:
        raise RuntimeError(
            "release version sources disagree before release preparation"
        )
    updated_pyproject = re.sub(
        r'(?m)^(version\s*=\s*)"[^"]+"(\s*)$',
        rf'\g<1>"{version}"\g<2>', pyproject_text, count=1,
    )
    updated_init = re.sub(
        r'(?m)^(__version__\s*=\s*)"[^"]+"(\s*)$',
        rf'\g<1>"{version}"\g<2>', init_text, count=1,
    )
    if updated_pyproject != pyproject_text:
        pyproject.write_text(updated_pyproject, encoding="utf-8")
        init_file.write_text(updated_init, encoding="utf-8")
        run_command([
            "git", "add", version_file, "src/orbi/__init__.py",
        ], cwd=worktree)
        run_git_write([
            "git", "commit", "-m", f"chore: prepare release {tag}",
        ], cwd=worktree)
        push_prepared_release_version(
            worktree, tag, base_branch, repo_dir or worktree,
        )
    return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()


