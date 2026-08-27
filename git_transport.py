"""Git transport contract (Issue #114).

Two authentication channels with distinct responsibilities:

- **Git data operations** (fetch, push — including pushing
  `.github/workflows/*.yml`) go over **SSH**
  (`git@github.com:owner/repo.git`), authenticated by the machine's
  SSH key. A workflow push must never depend on the OAuth App
  `workflow` scope — the HTTPS/OAuth transport that blocked Issue #106.
- **GitHub API operations** (Issue, PR, label, comment, merge) stay on
  the existing `gh` token. SSH is never used as API authentication and
  the `gh` token is never used for git data.

The deployment checkout's single `origin` remote is the transport: a
task worktree created with `git worktree add` shares the main
repository's remote configuration (verified against real git — the
worktree's `git remote -v` and `git config remote.origin.url` are the
main checkout's), so the transport is configured once on the checkout
and every worktree inherits it.

An existing HTTPS remote is never rewritten silently and never read
from a comment or Issue body: only the human-run setup entry
(`muyan-pilot setup`, `migrate=True`) migrates it with the plain
`git remote set-url origin <ssh-url>`; every other path fails fast
with the exact migration command. A failed SSH probe (`git ls-remote`,
verified against the real CLI: exit 0 = reachable and authenticated,
refs listed) fails fast with the structured reason — no HTTPS
fallback, no silent skip.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

GITHUB_HOST = "github.com"
SSH_USER = "git"
# The plain migration command the failure message reports and the
# human-run setup entry performs (verified against the real CLI:
# `git remote set-url origin <url>` rewrites the fetch URL; no
# separate pushurl is set).
MIGRATION_COMMAND = "git remote set-url origin {url}"
# The human-run entry that is authorized to perform the migration.
# Issue #140: the official entry is the installed `muyan-pilot` CLI.
MIGRATION_ENTRY = "muyan-pilot setup"


class TransportError(RuntimeError):
    """The git transport check failed (fail fast, no HTTPS fallback)."""


def ssh_url_for(repo: str) -> str:
    """The SSH URL of one configured `owner/name` source repo.

    `owner/name` → `git@github.com:owner/name.git`. A malformed repo
    name (missing/extra slash, empty segment) fails fast: a guessed
    URL must never be probed.
    """
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise TransportError(
            f"malformed source repo name: {repo!r} "
            "(expected 'owner/name')"
        )
    return f"{SSH_USER}@{GITHUB_HOST}:{repo}.git"


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
    source_repos: list[str],
    *,
    run_command,
    migrate: bool = False,
    probe: bool = True,
) -> dict:
    """Check (and only when authorized, migrate) the git transport.

    Checks, in order: the `origin` remote exists; it points at the
    FIRST configured source repo (the deployment checkout is that
    repo's clone — the worktrees share the single remote); its
    protocol is SSH; when `probe` is on, `git ls-remote <ssh-url>`
    exits 0 (SSH reachable and authenticated). An HTTPS remote of the
    SAME repo is migrated with `git remote set-url origin <ssh-url>`
    ONLY when `migrate` is True (the human-run setup entry); a remote
    pointing at a DIFFERENT repo is never rewritten (the migration
    would re-target the checkout) — it fails with the mismatch scene
    whether or not `migrate` is set. Every other failure carries the
    exact migration command and the setup entry. Any failure raises
    :class:`TransportError` with the concrete reason — no HTTPS
    fallback, no silent skip.
    """
    expected = ssh_url_for(source_repos[0])
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
    if protocol == "https":
        if not migrate:
            raise TransportError(
                f"origin remote is HTTPS ({url}); git data operations "
                f"must use SSH ({expected}). Migrate with: "
                f"{MIGRATION_COMMAND.format(url=expected)} — or run "
                f"`{MIGRATION_ENTRY}` (the human-run setup entry "
                "performs the migration). No automatic rewrite "
                "and no HTTPS fallback."
            )
        run_command(
            ["git", "remote", "set-url", "origin", expected],
            cwd=repo_dir,
        )
        url = expected
        migrated = True
    elif protocol != "ssh":
        raise TransportError(
            f"origin remote protocol is {protocol} ({url}); git data "
            f"operations require SSH ({expected}). Fix the remote "
            f"with: {MIGRATION_COMMAND.format(url=expected)}"
        )
    else:
        migrated = False
    ssh_reachable: bool | None
    if probe:
        try:
            run_command(["git", "ls-remote", expected], cwd=repo_dir)
        except subprocess.CalledProcessError as exc:
            detail = str(exc)
            if exc.stderr:
                detail += f" stderr={exc.stderr.strip()}"
            raise TransportError(
                f"ssh_unreachable: git ls-remote {expected} failed: "
                f"{detail} — SSH is unavailable (check the SSH key / "
                "agent / network). No HTTPS fallback and no silent "
                "skip: fix SSH and retry."
            ) from exc
        except Exception as exc:
            raise TransportError(
                f"ssh_unreachable: git ls-remote {expected} failed: "
                f"{exc} — SSH is unavailable (check the SSH key / "
                "agent / network). No HTTPS fallback and no silent "
                "skip: fix SSH and retry."
            ) from exc
        ssh_reachable = True
    else:
        ssh_reachable = None
    return {
        "remote": "origin",
        "protocol": "ssh",
        "url": url,
        "expected": expected,
        "migrated": migrated,
        "ssh_reachable": ssh_reachable,
    }
