#!/usr/bin/env python3
"""Repository-level config-as-code (Issue #527).

A repository may carry a delivery-policy file at ``.github/orbi.toml`` on its
**default branch**. At claim time the Runner reads that file through the
GitHub contents API (never the task branch, never the local worktree) and
applies its whitelisted policy keys **per key** over the host ``orbi.toml``
(delivery decision D3): the repository file wins for the keys it declares,
the host config is the fallback for every key it omits, and there is no
whole-file override.

Only delivery-policy keys are allowed (decision D2). Identity and security
keys — everything that routes credentials or crosses repositories — are
permanently host-only: a repository file that carries one fails the claim
fast with the offending key names. This module owns the strict schema, the
pure per-key merge/diff helpers and the `gh api` read; the decision to block
a claim lives in ``runner.process_issue``.

The read is deliberately fail-open: a repository with no such file (the
contents API 404) behaves exactly as before #527, and an API/network failure
also degrades to the host config — the safe direction, because host-only
keys can never be reached through the repository file.
"""
from __future__ import annotations

import base64
import json
import logging
import tomllib
from pathlib import PurePosixPath
from typing import Callable

from orbi.delivery_labels import LIFECYCLE_STATES, READY_LABEL

LOGGER = logging.getLogger("orbi.bootstrap")

# Decision D1: one location, `.github/` (the GitHub automation-config
# convention shared by CODEOWNERS / dependabot.yml / labeler).
REPO_CONFIG_PATH = ".github/orbi.toml"

# A config file larger than this is not a hand-written policy file; it is
# rejected instead of parsed (the contents API also omits `content` for
# files over 1 MiB, which would otherwise silently drop the policy).
MAX_REPO_CONFIG_BYTES = 64 * 1024

# Decision D2 (`context_files`): a repository-relative context file above
# this cap is not injected into the prompt (the file would dominate the
# context window and the run artifacts).
MAX_REPO_CONTEXT_BYTES = 256 * 1024

# Decision D2: the whitelist v1 — the delivery-policy keys a repository
# file may declare. Everything else is rejected.
POLICY_KEYS = (
    "base_branch",
    "active_milestone",
    "test_command",
    "context_files",
    "dispatch_label",
)

# Decision D2: permanently host-only keys (identity/security). These are a
# subset of "every non-whitelist key is rejected"; they are listed so the
# failure names them explicitly (the credential-routing red line).
HOST_ONLY_KEYS = frozenset({
    # cross-repository / workspace identity.
    "source_repos",
    "repo_dir",
    "deploy_home",
    "workspace_root",
    # prompt injection channel.
    "prompt",
    "prompt_review",
    "skills",
    # credential routing (the red line): provider/model/endpoint/keys.
    "pi_provider",
    "pi_model",
    "pi_thinking",
    "pi_providers",
    "pi_extensions",
    "model_wait_dead_seconds",
    "model_wait_probe_url",
    "model_wait_probe_seconds",
    # host scheduling / transport / recovery semantics.
    "max_concurrency",
    "slot_dir",
    "unit_name",
    "git_transport",
    "auto_next_milestone",
    "allow_stale_runner",
    "release_ci_wait_seconds",
    "mergeable_wait_seconds",
    "release_deliveries_wait_seconds",
    "health_alert_repo",
})


class RepoConfigError(ValueError):
    """A repository config file exists but violates the strict schema."""


def parse_repo_config(text: str, *, source: str = REPO_CONFIG_PATH) -> dict:
    """Parse and strictly validate one repository policy file.

    Returns the validated policy mapping (only whitelisted keys, validated
    values). A TOML error, an unknown key, a host-only key or a wrong type
    raises :class:`RepoConfigError` naming the offending key(s) — the claim
    then fails fast with a readable reason (Issue #527 acceptance).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RepoConfigError(f"{source}: invalid TOML: {exc}") from exc
    host_only = sorted(key for key in data if key in HOST_ONLY_KEYS)
    if host_only:
        raise RepoConfigError(
            f"{source}: host-only key(s) are not allowed: "
            + ", ".join(host_only)
        )
    unknown = sorted(key for key in data if key not in POLICY_KEYS)
    if unknown:
        raise RepoConfigError(
            f"{source}: unknown key(s): " + ", ".join(unknown)
        )
    policy: dict = {}
    for key in POLICY_KEYS:
        if key in data:
            policy[key] = _validate_value(key, data[key], source=source)
    return policy


def _validate_value(key: str, value: object, *, source: str) -> object:
    """Validate one whitelisted value; fail fast with the concrete reason."""
    if key == "context_files":
        if not isinstance(value, list) or not value:
            raise RepoConfigError(
                f"{source}: context_files must be a non-empty array of "
                "repository-relative paths"
            )
        paths: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item:
                raise RepoConfigError(
                    f"{source}: context_files[{index}] must be a non-empty "
                    "string"
                )
            if item.startswith("/") or PurePosixPath(item).is_absolute():
                raise RepoConfigError(
                    f"{source}: context_files[{index}] must be a "
                    f"repository-relative path, got {item!r}"
                )
            if ".." in PurePosixPath(item).parts:
                raise RepoConfigError(
                    f"{source}: context_files[{index}] must stay inside the "
                    f"repository, got {item!r}"
                )
            paths.append(item)
        return paths
    if not isinstance(value, str) or not value:
        raise RepoConfigError(
            f"{source}: {key} must be a non-empty string"
        )
    if key == "dispatch_label" and value in LIFECYCLE_STATES - {READY_LABEL}:
        # Decision D2: the delivery lifecycle labels stay host constants.
        # A claim label that IS one of them would make the ready scan
        # self-contradictory (`label:ai-merged ... -label:ai-merged`) and
        # silently stop claiming — reject it with the concrete reason.
        raise RepoConfigError(
            f"{source}: dispatch_label must not be a delivery lifecycle "
            f"label ({value!r})"
        )
    return value


def resolve_policy(config: dict, policy: dict) -> dict:
    """Apply a validated repository policy over the host config (D3).

    Per key, never whole-file: only a policy key the repository file
    declares overrides the corresponding host value. `context_files` is
    additive under a separate key (`repo_context_files`) because the host
    entries are already-resolved absolute paths; the repository entries
    stay repository-relative and are resolved against the task worktree in
    `run_pi`.
    """
    effective = dict(config)
    if "base_branch" in policy:
        effective["base_branch"] = policy["base_branch"]
    if "active_milestone" in policy:
        effective["active_milestone"] = policy["active_milestone"]
    if "test_command" in policy:
        effective["test_command"] = policy["test_command"]
    if "context_files" in policy:
        effective["repo_context_files"] = list(policy["context_files"])
    if "dispatch_label" in policy:
        effective["dispatch_label"] = policy["dispatch_label"]
    return effective


def _format_value(value: object) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, list):
        return ",".join(str(item) for item in value) or "(none)"
    return str(value)


def policy_diff(old: dict, new: dict) -> str | None:
    """Compact `key=old->new` summary of the changed policy keys.

    ``None`` when no effective policy key changed (the file sha may still
    differ, e.g. a comment-only edit). Spaces are allowed here: the
    summary is rendered as its own comment field, never spliced into the
    space-separated `run_info`.
    """
    parts = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            parts.append(
                f"{key}={_format_value(old.get(key))}"
                f"->{_format_value(new.get(key))}"
            )
    return " ".join(parts) if parts else None


def repo_config_audit(sha: str, policy: dict, *, previous_sha: str | None,
                      previous_policy: dict | None) -> dict:
    """The D4 change-visibility fields for the run comment.

    Always carries nothing (the caller adds `repo_config: <sha>` to the
    run info). When the previous run recorded a different sha, adds the
    `repo_config_changed` marker and, when the previous content was
    readable, the effective policy diff summary.
    """
    fields: dict = {}
    if not previous_sha or previous_sha == sha:
        return fields
    fields["repo_config_changed"] = f"{previous_sha}..{sha}"
    diff = policy_diff(previous_policy or {}, policy)
    if diff:
        fields["repo_config_diff"] = diff
    return fields


def _is_not_found(exc: Exception) -> bool:
    """True when a `gh api` failure is GitHub's 404 for a missing path."""
    stdout = getattr(exc, "stdout", "") or ""
    stderr = getattr(exc, "stderr", "") or ""
    return '"status":"404"' in stdout.replace(" ", "") or "HTTP 404" in stderr


def read_repo_config(repo: str, *, path: str = REPO_CONFIG_PATH,
                     run_command: Callable[..., str]) -> dict | None:
    """Read `<repo>@<default-branch>` `path` through the contents API.

    Returns `{"sha": <file blob sha>, "policy": {...}}` or `None` when the
    repository has no such file (the 404) or the read failed for any other
    transport reason. A file that exists but violates the schema raises
    :class:`RepoConfigError` — the caller blocks the claim.
    """
    endpoint = f"repos/{repo}/contents/{path}"
    try:
        raw = run_command(["gh", "api", endpoint], timeout=30)
    except Exception as exc:
        if not _is_not_found(exc):
            LOGGER.warning(
                "repo_config_read_failed repo=%s path=%s error=%s "
                "(falling back to the host config)",
                repo, path, exc,
            )
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        LOGGER.warning(
            "repo_config_read_failed repo=%s path=%s error=%s "
            "(falling back to the host config)",
            repo, path, exc,
        )
        return None
    if not isinstance(data, dict) or "sha" not in data or "content" not in data:
        # Not a file object (e.g. a directory listing or an empty answer):
        # the repository has no readable policy file.
        return None
    size = data.get("size")
    if isinstance(size, int) and size > MAX_REPO_CONFIG_BYTES:
        raise RepoConfigError(
            f"{path}: file is too large ({size} bytes > "
            f"{MAX_REPO_CONFIG_BYTES})"
        )
    try:
        content = base64.b64decode(data["content"])
    except (ValueError, TypeError) as exc:
        raise RepoConfigError(
            f"{path}: contents API returned undecodable content: {exc}"
        ) from exc
    if len(content) > MAX_REPO_CONFIG_BYTES:
        raise RepoConfigError(
            f"{path}: file is too large ({len(content)} bytes > "
            f"{MAX_REPO_CONFIG_BYTES})"
        )
    text = content.decode("utf-8")
    return {
        "sha": str(data["sha"]),
        "policy": parse_repo_config(text, source=path),
    }


def read_repo_config_at(repo: str, sha: str, *, path: str = REPO_CONFIG_PATH,
                        run_command: Callable[..., str]) -> dict | None:
    """Read the policy of a previous run's config blob for the D4 diff.

    Uses the git blobs API (`/git/blobs/{sha}`), which accepts the file
    blob sha recorded on the previous run comment. Best-effort: any read
    or parse failure returns `None` so the change is still reported, just
    without the diff summary.
    """
    try:
        raw = run_command(
            ["gh", "api", f"repos/{repo}/git/blobs/{sha}"], timeout=30,
        )
        data = json.loads(raw)
    except Exception:
        LOGGER.warning(
            "repo_config_previous_read_failed repo=%s path=%s sha=%s",
            repo, path, sha,
        )
        return None
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return None
    try:
        text = base64.b64decode(data["content"]).decode("utf-8")
        return parse_repo_config(text)
    except (ValueError, TypeError):
        return None


def validate_context_file(worktree, relative: str, *,
                          source: str = REPO_CONFIG_PATH):
    """Resolve one repository `context_files` entry inside the worktree.

    Existence and the size cap are enforced before injection (D2); a
    missing or oversized file raises :class:`RepoConfigError`, which the
    run's failure path reports with the concrete path.
    """
    path = worktree / relative
    if not path.is_file():
        raise RepoConfigError(
            f"{source}: context_files entry {relative!r} does not exist in "
            "the delivery worktree"
        )
    size = path.stat().st_size
    if size > MAX_REPO_CONTEXT_BYTES:
        raise RepoConfigError(
            f"{source}: context_files entry {relative!r} is too large "
            f"({size} bytes > {MAX_REPO_CONTEXT_BYTES})"
        )
    return path
