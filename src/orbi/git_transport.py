"""Git transport contract (Issue #114, #580).

Two authentication channels with distinct responsibilities:

- **GitHub API operations** (Issue, PR, label, comment, merge) stay on
  the existing `gh` token.
- **Git data operations** (fetch, push — including pushing
  `.github/workflows/*.yml`) go over the delivery checkout's single
  `origin` remote, in ONE of two configured modes (orbi.toml
  `git_transport`):

  - `ssh` (default, the Issue #114 contract): the SCP-style
    `git@github.com:owner/repo.git`, authenticated by the machine's
    SSH key. A workflow push must never depend on the OAuth App
    `workflow` scope — the HTTPS/OAuth transport that blocked Issue
    #106.
  - `https` (Issue #580, the token-only sandbox path): the origin
    stays `https://github.com/owner/repo.git` and the credentials
    come from the `gh` credential helper (`gh auth login --with-token`)
    — no SSH private key anywhere.

The deployment checkout's single `origin` remote is the transport: a
task worktree created with `git worktree add` shares the main
repository's remote configuration (verified against real git — the
worktree's `git remote -v` and `git config remote.origin.url` are the
main checkout's), so the transport is configured once on the checkout
and every worktree inherits it.

A remote on the opposite transport is never rewritten silently and
never read from a comment or Issue body: only the human-run setup
entry (`orbi setup`, `migrate=True`) migrates it with the plain
`git remote set-url origin <expected-url>` (HTTPS→SSH in ssh mode,
SSH→HTTPS in https mode); every other path fails fast with the exact
migration command. A failed reachability probe (`git ls-remote`,
verified against the real CLI: exit 0 = reachable and authenticated,
refs listed) fails fast with the structured reason (`ssh_unreachable`
in ssh mode, `transport_unreachable` in https mode) — no fallback, no
silent skip.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

GITHUB_HOST = "github.com"
SSH_USER = "git"
# The plain migration command the failure message reports and the
# human-run setup entry performs (verified against the real CLI:
# `git remote set-url origin <url>` rewrites the fetch URL; no
# separate pushurl is set).
MIGRATION_COMMAND = "git remote set-url origin {url}"
# The human-run entry that is authorized to perform the migration.
# Issue #140: the official entry is the installed `orbi` CLI.
MIGRATION_ENTRY = "orbi setup"
# The configured transport modes (orbi.toml `git_transport`).
MODES = ("ssh", "https")


class TransportError(RuntimeError):
    """The git transport check failed (fail fast, no fallback)."""


def _validated_repo(repo: str) -> str:
    """One configured `owner/name` source repo name, validated.

    A malformed repo name (missing/extra slash, empty segment) fails
    fast: a guessed URL must never be probed.
    """
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise TransportError(
            f"malformed source repo name: {repo!r} "
            "(expected 'owner/name')"
        )
    return repo


def ssh_url_for(repo: str) -> str:
    """The SSH URL of one configured `owner/name` source repo.

    `owner/name` → `git@github.com:owner/name.git`.
    """
    return f"{SSH_USER}@{GITHUB_HOST}:{_validated_repo(repo)}.git"


def https_url_for(repo: str) -> str:
    """The HTTPS URL of one configured `owner/name` source repo.

    `owner/name` → `https://github.com/owner/name.git` (the https
    transport mode of Issue #580; credentials via the gh credential
    helper).
    """
    return f"https://{GITHUB_HOST}/{_validated_repo(repo)}.git"


def remote_protocol(url: str) -> str:
    """The protocol family of one git remote URL.

    `ssh` for the GitHub SCP-style form (`git@github.com:...`) and the
    `ssh://` scheme, `https`/`http` for the web forms, `other` for
    everything else (local paths, unknown hosts).
    """
    if url.startswith("https://"):
        return "https"
    if url.startswith("http://"):
        return "http"
    if (
        url.startswith(f"{SSH_USER}@{GITHUB_HOST}:")
        or url.startswith(f"ssh://{SSH_USER}@{GITHUB_HOST}/")
    ):
        return "ssh"
    return "other"


def _remote_repo_path(url: str) -> str | None:
    """The `owner/name` path of one GitHub remote URL, or None.

    Accepts the SCP-style SSH form (`git@github.com:owner/name[.git]`),
    the `ssh://` scheme and the `https://`/`http://` web forms; the
    optional `.git` suffix is stripped so a remote configured without
    it still matches the expected repo. A URL on any other host (or a
    local path) has no GitHub repo path: it can never be migrated, a
    rewrite of such a remote would re-target the checkout at a
    guessed destination.
    """
    prefixes = (
        f"{SSH_USER}@{GITHUB_HOST}:",
        f"ssh://{SSH_USER}@{GITHUB_HOST}/",
        f"https://{GITHUB_HOST}/",
        f"http://{GITHUB_HOST}/",
    )
    for prefix in prefixes:
        if url.startswith(prefix):
            path = url[len(prefix):]
            if path.endswith(".git"):
                path = path[: -len(".git")]
            return path or None
    return None


def check_transport(
    repo_dir: Path,
    source_repos: Sequence[str],
    *,
    run_command,
    migrate: bool = False,
    probe: bool = True,
    mode: str = "ssh",
) -> dict:
    """Check (and only when authorized, migrate) the git transport.

    `mode` is the configured transport (`git_transport` in orbi.toml):
    `"ssh"` (default, the exact Issue #114 contract) or `"https"`
    (Issue #580). Checks, in order: the `origin` remote exists; it
    points at the FIRST configured source repo (the deployment
    checkout is that repo's clone — the worktrees share the single
    remote); its protocol matches the mode; when `probe` is on,
    `git ls-remote <expected-url>` exits 0 (the configured transport
    reachable and authenticated — SSH key or gh credential helper). A
    remote on the OPPOSITE transport (HTTPS in ssh mode, SSH in https
    mode) of the SAME repo is migrated with
    `git remote set-url origin <expected-url>` ONLY when `migrate` is
    True (the human-run setup entry); a remote pointing at a DIFFERENT
    repo is never rewritten (the migration would re-target the
    checkout) — it fails with the mismatch scene whether or not
    `migrate` is set. Every other failure carries the exact migration
    command and the setup entry. Any failure raises
    :class:`TransportError` with the concrete reason — no fallback, no
    silent skip.
    """
    if mode not in MODES:
        raise TransportError(
            f"unknown git_transport mode {mode!r} (expected 'ssh' or "
            "'https')"
        )
    url_for = ssh_url_for if mode == "ssh" else https_url_for
    expected = url_for(source_repos[0])
    # The CONFIGURED URL is the transport (verified against the real
    # CLI: `git remote get-url` applies `url.<base>.insteadOf`
    # rewrites and would report the effective data-plane URL; `git
    # config remote.origin.url` returns the configured URL, exit 1
    # when the remote is missing). A local insteadOf rewrite (e.g. in
    # an offline e2e world) stays a data-plane detail, never the
    # transport.
    try:
        url = run_command(["git", "config", "remote.origin.url"],
                          cwd=repo_dir)
    except subprocess.CalledProcessError as exc:
        detail = str(exc)
        if exc.stderr:
            detail += f" stderr={exc.stderr.strip()}"
        raise TransportError(
            f"checkout has no origin remote: {repo_dir} "
            f"({detail})"
        ) from exc
    except Exception as exc:
        raise TransportError(
            f"checkout has no origin remote: {repo_dir} ({exc})"
        ) from exc
    # The repo the remote points at is verified BEFORE any protocol
    # decision or rewrite: a remote that does not point at the first
    # configured source repo is never migrated (the migration would
    # re-target the checkout at a different repository), whether or
    # not `migrate` is authorized.
    repo_path = _remote_repo_path(url)
    if repo_path != source_repos[0]:
        raise TransportError(
            f"origin remote repo mismatch: actual={url} "
            f"(repo={repo_path!r}) expected={expected} "
            f"(repo={source_repos[0]!r}); the deployment checkout "
            "must be a clone of the first configured source repo"
        )
    protocol = remote_protocol(url)
    migrated = False
    if protocol == mode:
        pass
    elif protocol in MODES:
        # The opposite configured transport, same repo: migrate ONLY
        # from the human-run setup entry.
        if not migrate:
            raise TransportError(
                f"origin remote is {protocol.upper()} ({url}); the "
                f"configured git_transport is {mode.upper()} "
                f"({expected}). Migrate with: "
                f"{MIGRATION_COMMAND.format(url=expected)} — or run "
                f"`{MIGRATION_ENTRY}` (the human-run setup entry "
                "performs the migration). No automatic rewrite."
            )
        run_command(
            ["git", "remote", "set-url", "origin", expected],
            cwd=repo_dir,
        )
        url = expected
        migrated = True
    else:
        raise TransportError(
            f"origin remote protocol is {protocol} ({url}); the "
            f"configured git_transport is {mode.upper()} "
            f"({expected}). Fix the remote with: "
            f"{MIGRATION_COMMAND.format(url=expected)}"
        )
    reachable: bool | None
    if probe:
        try:
            run_command(["git", "ls-remote", expected], cwd=repo_dir)
        except subprocess.CalledProcessError as exc:
            detail = str(exc)
            if exc.stderr:
                detail += f" stderr={exc.stderr.strip()}"
            raise TransportError(
                _probe_failure(mode, expected, detail)
            ) from exc
        except Exception as exc:
            raise TransportError(
                _probe_failure(mode, expected, str(exc))
            ) from exc
        reachable = True
    else:
        reachable = None
    return {
        "remote": "origin",
        "protocol": mode,
        "url": url,
        "expected": expected,
        "migrated": migrated,
        # Issue #580 compat: the SSH-specific probe result stays (None
        # in https mode — no SSH probe runs there);
        # `transport_reachable` is the ACTIVE transport's probe result.
        "ssh_reachable": reachable if mode == "ssh" else None,
        "transport_reachable": reachable,
    }


def _probe_failure(mode: str, expected: str, detail: str) -> str:
    """The structured probe-failure reason for the configured mode."""
    if mode == "ssh":
        return (
            f"ssh_unreachable: git ls-remote {expected} failed: "
            f"{detail} — SSH is unavailable (check the SSH key / "
            "agent / network). No HTTPS fallback and no silent "
            "skip: fix SSH and retry."
        )
    return (
        f"transport_unreachable: git ls-remote {expected} failed: "
        f"{detail} — the HTTPS transport is unavailable (check the "
        "gh credentials: `gh auth status` — the credential helper "
        "supplies them). No fallback and no silent skip: fix the "
        "credentials and retry."
    )
