"""Orbi release delivery subsystem (Issue #286, moved from `orbi.runner`).

The deterministic release state machine the Runner executes for an
`ai-release` Issue: declaration parsing, scope verification, gates,
version preparation, tag, GitHub Release publish, docs sync, Milestone
close, and the `process_release` orchestration. Its GitHub and git data
access goes through the `orbi.github` / `orbi.gitops` leaves and the
`orbi.journal` seam (Issue #785); the runner-side entry is the dispatch
in `orbi.runner.process_issue`, and `runner` imports this module at
module level for the release constants.
"""
from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import subprocess
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from orbi.delivery_labels import (
    EPIC_LABEL,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_MERGED,
    EVENT_RELEASE_WAITING,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
)
from orbi.progress import (
    ProgressPublisher,
    field_block,
    progress_body,
    run_marker,
)
from orbi.cli_source import refresh_cli_install
from orbi.github import (
    _comment_is_trusted,
    _verify_epic_complete,
    _epic_audit,
    apply_label_patch,
    close_issue,
    close_milestone,
    commit_check_runs,
    comment_issue,
    has_in_progress_label,
    issue_comments,
    issue_priority,
    issue_view,
    list_issues,
    list_milestones,
    milestone_issues,
    milestone_open_issues,
    pr_view,
    release_create,
    release_edit_notes,
    release_view,
)
from orbi.gitops import (
    _is_ancestor,
    acquire_base_sync_lock,
    create_release_worktree,
    freeze_base,
    latest_run_id,
    task_branch,
    worktree_path,
)
from orbi.journal import (
    LOGGER,
    new_run_id,
    run_command,
    run_git_network_command,
    set_active_run,
    set_run_id,
)
from orbi.progress import (
    ProgressPublisher,
    _progress_body,
    _progress_state,
    _run_info_fields,
    _safe_publish,
    field_block,
    progress_body,
    run_marker,
)

if TYPE_CHECKING:
    # Annotation-only: this module imports `orbi.runner` never — the
    # runner imports this module at runtime (Issue #785) — while
    # `process_release` annotates the frozen host config of #790.
    from orbi.runner import RunnerConfig


# --- release-domain constants, scene and gates (moved from
# --- `orbi.runner`, Issue #785: the release contract lives here) --------

# The machine-readable section a release Issue body must carry (Issue
# #98): `- version:`, `- base_branch:` and `- scope:` (or
# `- scope_from_milestone:`). Parsed strictly — a missing or malformed
# declaration fails fast, never guessed. The declaration carries NO
# local test contract (Issue #569): test acceptance is the GitHub
# Actions CI result on the release commit (the #268 CI-wait gate).
# The Pi role of a release run (Issue #41/#82): the delivery state
# machine executes it, never a Pi session.
ROLE_RELEASE = "release"

RELEASE_SECTION = "## Release"
# Release CI wait (Issue #268): the release commit is born from the last
# delivery PR merge, so its CI is almost always still running when the
# gate checks it — a pending check (queued/in_progress) is an
# intermediate state, not a failure. The gate waits for completion up to
# this limit and decides on the FINAL conclusions; a wait timeout is its
# own failure reason, never reported as a CI failure. The TOML field
# `release_ci_wait_seconds` overrides the default (the #228 pattern).
RELEASE_CI_WAIT_SECONDS = 1800
# Release delivery wait (Issue #381): an early release ticket yields the
# slot while other deliveries finish. The limit applies to one gate attempt;
# the next tick retries the same ready release ticket.
RELEASE_DELIVERIES_WAIT_SECONDS = 1800
# Poll cadence while waiting: one `release_waiting_ci` journal line plus
# one progress-comment PATCH per poll — the same 30s GitHub cadence as
# the live progress heartbeat (PI_HEARTBEAT_SECONDS).
RELEASE_CI_POLL_INTERVAL = 30.0


# Supported `version_file` declaration values: the ecosystem metadata
# files (written by `prepare_release_version`) plus `none` — skip version
# metadata changes and tag the frozen base HEAD directly.
RELEASE_VERSION_FILE_OPTIONS = (
    "pyproject.toml", "package.json", "pom.xml", "build.gradle",
    "build.gradle.kts", "gradle.properties", "Cargo.toml",
    "composer.json", "pubspec.yaml", "none",
)


def parse_release_declaration(body: str) -> dict:
    """Strictly parse the `## Release` section of a release Issue body.

    The declaration is the machine-readable contract of a Release task
    (Issue #98) — the only state a release run reads from the Issue
    body (checkboxes are never parsed):

    ```markdown
    ## Release

    - version: v0.3.0
    - base_branch: main
    - scope:
      - #123
      - #124
    ```

    or, instead of the hand-listed `scope`, the scope derived from the
    Milestone whose title is the release version (Issue #253):

    ```markdown
    - scope_from_milestone: v0.3.0
    ```

    `version` is the exact tag name (no spaces) and `base_branch` the
    branch the release commit is frozen from. The declaration carries
    NO local test contract (Issue #569): test acceptance is the GitHub
    Actions CI result on the release commit (the #268 CI-wait gate), so
    `test_command` is not part of the contract — a legacy body that
    still declares it is accepted with the field ignored (one
    `release_test_command_ignored` evidence line at run time) and never
    executed. `scope` lists the Issue/PR numbers verified one by one.
    Optional `version_file` selects a supported ecosystem metadata file
    (the default is `pyproject.toml`) or `none` to skip version metadata
    changes; its existence in the frozen release tree is verified at
    claim time by `verify_release_version_file` (Issue #740).
    Exactly one of `scope` / `scope_from_milestone` must be present:
    both (conflict) or neither fails fast. `scope_from_milestone` is
    the Milestone TITLE (no spaces); its scope is derived later by
    `derive_release_scope_from_milestone`. Every other deviation fails
    fast with the concrete field: missing section, missing or
    duplicated field, unknown key, empty value, empty scope or a
    malformed scope item. No guessing.
    """
    if not isinstance(body, str):
        raise ValueError("release declaration body must be a string")
    lines = body.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines)
            if line.strip() == RELEASE_SECTION
        )
    except StopIteration:
        raise ValueError(
            f"release Issue body is missing the `{RELEASE_SECTION}` "
            "section with version, base_branch and scope or "
            "scope_from_milestone"
        ) from None
    section: list[str] = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith("## "):
            break
        section.append(line)
    fields: dict[str, str] = {}
    scope: list[int] = []
    scope_open = False
    for line in section:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            content = stripped[2:].strip()
            if scope_open and re.fullmatch(r"#\d+", content):
                number = int(content[1:])
                if number < 1:
                    raise ValueError(
                        f"release declaration scope item {content!r} "
                        "must be a positive Issue or PR number"
                    )
                scope.append(number)
                continue
            if scope_open and ":" not in content:
                raise ValueError(
                    f"release declaration scope item {content!r} is "
                    "malformed (expected `  - #N`)"
                )
            # A `- key: value` line closes the scope list (and a
            # duplicated or unknown key is caught below).
            scope_open = False
            key, sep, value = content.partition(":")
            if not sep:
                raise ValueError(
                    f"release declaration field {key.strip()!r} is "
                    "malformed (expected `- key: value`)"
                )
            key = key.strip()
            value = value.strip()
            if key in fields:
                raise ValueError(
                    f"release declaration field {key!r} is duplicated"
                )
            if key == "scope":
                if value:
                    raise ValueError(
                        "release declaration `scope` must be a list of "
                        "`  - #N` items, not an inline value"
                    )
                scope_open = True
                fields["scope"] = ""
            # `test_command` stays a KNOWN key (Issue #569): a legacy
            # body may still declare it — accepted, ignored, never
            # executed.
            elif key in ("version", "base_branch", "test_command",
                         "scope_from_milestone", "version_file"):
                fields[key] = value
            else:
                raise ValueError(
                    f"release declaration has the unknown field {key!r} "
                    "(expected version, base_branch, scope, "
                    "scope_from_milestone or version_file)"
                )
        elif scope_open:
            raise ValueError(
                f"release declaration scope item {stripped!r} is "
                "malformed (expected `  - #N`)"
            )
        else:
            raise ValueError(
                f"release declaration line {stripped!r} is not a "
                "`- key: value` field or a scope item"
            )
    for key in ("version", "base_branch"):
        if key not in fields:
            raise ValueError(
                f"release declaration is missing the `{key}` field"
            )
        if not fields[key]:
            raise ValueError(
                f"release declaration field `{key}` is empty"
            )
    if "scope" in fields and "scope_from_milestone" in fields:
        raise ValueError(
            "release declaration must use exactly one of `scope` or "
            "`scope_from_milestone`, not both"
        )
    if "scope" not in fields and "scope_from_milestone" not in fields:
        raise ValueError(
            "release declaration is missing the `scope` field or the "
            "`scope_from_milestone` field (exactly one of the two)"
        )
    if "scope" in fields and not scope:
        raise ValueError(
            "release declaration `scope` must list at least one "
            "`  - #N` Issue or PR number"
        )
    for key in ("version", "base_branch"):
        if any(ch.isspace() for ch in fields[key]):
            raise ValueError(
                f"release declaration field `{key}` must not contain "
                "spaces"
            )
    if "scope_from_milestone" in fields:
        if not fields["scope_from_milestone"]:
            raise ValueError(
                "release declaration field `scope_from_milestone` is "
                "empty"
            )
        if any(ch.isspace() for ch in fields["scope_from_milestone"]):
            raise ValueError(
                "release declaration field `scope_from_milestone` must "
                "not contain spaces"
            )
    version_file = fields.get("version_file", "pyproject.toml")
    if version_file not in RELEASE_VERSION_FILE_OPTIONS:
        raise ValueError(
            "release declaration field `version_file` is not supported"
        )
    return {
        "version": fields["version"],
        "base_branch": fields["base_branch"],
        # Issue #569: a legacy field, accepted and ignored — never executed.
        "test_command": fields.get("test_command"),
        "scope": scope,
        "scope_from_milestone": fields.get("scope_from_milestone"),
        "version_file": version_file,
    }


def verify_release_version_file(repo_dir: Path, release_commit: str,
                                version_file: str) -> None:
    """Prove the declared `version_file` exists in the frozen base (Issue #740).

    `version_file` is an optional declaration field that silently defaults
    to the Python ecosystem's `pyproject.toml`; a repository without that
    file used to discover the mismatch only at the version-write step —
    after the gates and the scope verification had already run — and
    burned the ticket `ai-blocked` with a bare FileNotFoundError
    (orbi-cloud#246). The check probes the ROOT of the frozen release
    commit's tree (`git ls-tree --name-only` — the exact content the
    version write will see) and runs at claim time, before any gate wait.
    A mismatch fails fast with an actionable message: the supported
    ecosystem files that DO exist at the repository root (the value to
    declare), and `none` for a repository with no version metadata file
    at all.
    """
    if version_file == "none":
        return
    root_entries = set(run_command(
        ["git", "ls-tree", "--name-only", release_commit],
        cwd=repo_dir,
    ).splitlines())
    if version_file in root_entries:
        return
    existing = [
        name for name in RELEASE_VERSION_FILE_OPTIONS
        if name != "none" and name in root_entries
    ]
    raise RuntimeError(
        f"release version_file {version_file!r} does not exist at the "
        f"root of the release tree {release_commit} — supported files "
        f"present at the repository root: "
        f"{', '.join(existing) if existing else '(none)'}; declare one "
        "of those as `- version_file: <file>`, or `- version_file: "
        "none` to skip version metadata changes"
    )


def verify_release_scope(repo: str, scope: list[int], repo_dir: Path,
                         release_commit: str) -> list[str]:
    """Verify every release scope item ONE BY ONE (Issue #98).

    The scope is verified against GitHub, never by parsing Issue-body
    checkboxes: each number is probed as a PR first (`gh pr view` —
    which exits 1 with the real "Could not resolve to a PullRequest"
    error when the number is an Issue, verified against the live CLI)
    and, failing that, as an Issue (`gh issue view`). A PR must be
    `MERGED` and its merge commit must be an ancestor of the frozen
    release commit; an Issue must be `CLOSED` with `stateReason`
    `COMPLETED` (Issue #707): a `NOT_PLANNED` closure (duplicate /
    won't fix) is not a delivery — its evidence line visibly annotates
    the exclusion instead of silently counting the ticket into the
    release. This ancestor check proves
    the scoped PR is actually contained in the tag, rather than merely
    having been merged into some other branch. An item that is neither, a
    PR that is not merged/in the release base, or an Issue that is not
    closed fails fast with the concrete number and state. A real `gh` or
    git failure (auth, rate limit, missing object) is re-raised, never
    misread as "not a PR".
    """
    evidence: list[str] = []
    for number in scope:
        try:
            pr = pr_view(number, "number,state,mergeCommit", repo=repo)
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to a PullRequest" not in (exc.stderr or ""):
                raise
            pr = None
        if pr is not None:
            state = pr.get("state")
            if state != "MERGED":
                raise RuntimeError(
                    f"release scope PR #{number} is not merged "
                    f"(state={state})"
                )
            merge_commit = (pr.get("mergeCommit") or {}).get("oid")
            if not merge_commit:
                raise RuntimeError(
                    f"release scope PR #{number} is merged but has no "
                    "merge commit evidence"
                )
            if not _is_ancestor(merge_commit, release_commit, cwd=repo_dir):
                raise RuntimeError(
                    f"release scope PR #{number} merge commit {merge_commit} "
                    f"is not contained in release commit {release_commit}"
                )
            evidence.append(
                f"PR #{number} merged (mergeCommit={merge_commit})"
            )
            continue
        try:
            issue = issue_view(number, "number,state,stateReason", repo=repo)
        except subprocess.CalledProcessError as exc:
            if "Could not resolve to an Issue" not in (exc.stderr or ""):
                raise
            raise RuntimeError(
                f"release scope item #{number} is neither a PR nor an "
                "Issue"
            ) from exc
        state = issue.get("state")
        if state != "CLOSED":
            raise RuntimeError(
                f"release scope Issue #{number} is not closed "
                f"(state={state})"
            )
        if issue.get("stateReason") == "NOT_PLANNED":
            # Issue #707: a NOT_PLANNED closure (duplicate / won't fix)
            # is not released work — annotate the exclusion visibly
            # instead of silently counting the ticket into the release.
            evidence.append(
                f"Issue #{number} closed (not planned, excluded)"
            )
        else:
            evidence.append(f"Issue #{number} closed")
    return evidence


def derive_release_scope_from_milestone(repo: str,
                                        milestone_title: str) -> tuple[list[int], list[str]]:
    """Derive the release scope from a Milestone (Issue #253).

    The release scope is the Milestone's COMPLETED Issues under the
    Milestone whose title is EXACTLY `milestone_title` (the same
    exact-title rule as `close_release_milestone` — never guessed,
    never fuzzy-matched, never a different Milestone). Pull requests are
    obtained from each scoped Issue's closing references by
    `build_release_changelog`; the REST `/pulls` list endpoint does not
    support milestone filtering:

    - no Milestone with that exact title -> fail fast;
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - open Issues are NEVER part of the scope (unfinished work is a
      human decision point) but are returned as a separate evidence list
      so the release run surfaces them instead of swallowing them.

    Returns (scope numbers sorted ascending, open-item evidence
    strings). A real `gh` failure (auth, rate limit, API error)
    propagates unchanged — a scope that cannot be derived is a failed
    release, never a guessed one.
    """
    milestones = list_milestones(repo)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == milestone_title
    ]
    if not matches:
        raise RuntimeError(
            f"release milestone derivation: no Milestone with the exact "
            f"title {milestone_title!r} in {repo} — never guessed or "
            "fuzzy-matched"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"release milestone derivation: {len(matches)} Milestones "
            f"share the exact title {milestone_title!r} in {repo} — "
            "ambiguous, refusing to guess which to derive from"
        )
    number = matches[0].get("number")

    def issues(state: str) -> list[dict]:
        return milestone_issues(repo, int(number), state)

    closed_issues = [item for item in issues("closed")
                     if "pull_request" not in item]
    open_issues = [item for item in issues("open")
                   if "pull_request" not in item]

    scope = sorted(
        int(item["number"])
        for item in closed_issues
        if isinstance(item.get("number"), int)
    )
    open_evidence = [
        f"open Issue #{item.get('number')} {item.get('title')}"
        for item in open_issues
    ]
    return scope, open_evidence


RELEASE_CHANGELOG_CATEGORIES = (
    "Features", "Reliability and recovery", "Deployment and operations",
    "Observability", "Documentation", "Bug fixes",
)


def release_changelog_category(item: dict) -> str:
    """Classify one live scoped Issue into a stable reader-facing group."""
    labels = item.get("labels")
    label_names = {
        label.get("name", "").lower() for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    } if isinstance(labels, list) else set()
    title = item.get("title")
    text = title.lower() if isinstance(title, str) else ""
    if "documentation" in label_names or any(
        term in text for term in ("documentation", "docs", "readme", "文档")
    ):
        return "Documentation"
    if any(term in text for term in (
        "deploy", "deployment", "systemd", "install", " cli", "ssh",
        "service", "timer", "packaging", "setup",
    )):
        return "Deployment and operations"
    if any(term in text for term in (
        "recovery", "recover", "resume", "timeout", "concurren", "reliab",
        "stale", "dead", "hang", "lock",
    )):
        return "Reliability and recovery"
    if any(term in text for term in (
        "observability", "prometheus", "grafana", "dashboard", "metrics",
        "exporter", "journal", "progress",
    )):
        return "Observability"
    if "bug" in label_names:
        return "Bug fixes"
    return "Features"


RELEASE_ISSUE_EVIDENCE_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue: issueOrPullRequest(number: $number) {
      __typename
      ... on Issue {
        number title body url stateReason
        labels(first: 100) { nodes { name } }
        closedByPullRequestsReferences(first: 100) {
          nodes { number url author { login avatarUrl } }
        }
      }
      ... on PullRequest { number title body url labels(first: 100) { nodes { name } } }
    }
  }
}"""


def build_release_changelog(repo: str, scope: list[int]) -> str:
    """Render deterministic readable notes from live scoped Issue evidence.

    Each scope number is resolved with one `gh api graphql` round trip
    (`issueOrPullRequest`, the same access path `gh issue view` used
    before Issue #772): the Issue fields, labels and closing-PR
    references — now including each PR's author login and avatar so the
    Contributors section costs zero extra API calls.  A title is the
    concise change description; when it is absent, the first non-empty
    body line is usable summary evidence.  A NOT_PLANNED-closed Issue is
    not released work (Issue #707) — it is excluded from the Changelog
    (the Scope evidence annotates the exclusion).  A closing PR's link
    is written only when `gh pr view` reports the PR MERGED: an unmerged
    PR never appears in the release notes, and its author is not a
    contributor.  The `## Contributors` section lists every merged
    closing-PR author exactly once (deduped by login, sorted by login)
    as a linked avatar; a null or malformed author is display evidence,
    not a release judge — it is skipped with a log line and never fails
    the release, and with no contributors at all no section is written.
    Missing or malformed evidence is an unsafe release input and fails
    before a tag or Release is created.
    """
    owner, _, name = repo.partition("/")
    grouped: dict[str, list[tuple[int, str]]] = {
        category: [] for category in RELEASE_CHANGELOG_CATEGORIES
    }
    contributors: dict[str, str] = {}
    for number in scope:
        raw = run_command([
            "gh", "api", "graphql",
            "-f", f"query={RELEASE_ISSUE_EVIDENCE_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}",
            "-F", f"number={number}",
        ], log_command=["gh", "api", "graphql", f"issue={number}"])
        issue = (json.loads(raw).get("data") or {}).get(
            "repository", {},
        ).get("issue") or {}
        # Reshape the GraphQL payload into the field keys the strict
        # evidence validation below has always asserted; the closedBy
        # nodes (number/url/author) pass through raw.  The PullRequest
        # branch carries no closedBy field (schema: Issue-only) and no
        # stateReason — the same shape `gh issue view` produced for a
        # PR number.
        references = issue.get("closedByPullRequestsReferences") or {}
        item = {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "body": issue.get("body"),
            "url": issue.get("url"),
            "labels": [
                {"name": label.get("name")}
                for label in (issue.get("labels") or {}).get("nodes") or []
                if isinstance(label, dict)
            ],
            "closedByPullRequestsReferences": (
                references.get("nodes") or []
                if isinstance(references, dict) else None
            ),
        }
        if issue.get("__typename") == "Issue":
            item["stateReason"] = issue.get("stateReason")
        if item.get("stateReason") == "NOT_PLANNED":
            LOGGER.info(
                "release_changelog_issue_excluded number=%d "
                "reason=NOT_PLANNED", number,
            )
            continue
        issue_url = item.get("url")
        issue_path = f"https://github.com/{repo}/issues/{number}"
        pull_path = f"https://github.com/{repo}/pull/{number}"
        if item.get("number") != number or issue_url not in (issue_path, pull_path):
            raise ValueError(
                f"release changelog Issue #{number} has malformed Issue evidence"
            )
        title = item.get("title")
        body = item.get("body")
        summary = title.strip() if isinstance(title, str) else ""
        if not summary and isinstance(body, str):
            summary = next((line.strip() for line in body.splitlines()
                            if line.strip()), "")
        if not summary or re.fullmatch(r"Issue #\d+ closed", summary, re.I):
            raise ValueError(
                f"release changelog Issue #{number} has no usable title/summary evidence"
            )
        source_kind = "PR" if issue_url == pull_path else "Issue"
        links = [f"[{source_kind} #{number}]({issue_url})"]
        pull_requests = item.get("closedByPullRequestsReferences")
        if not isinstance(pull_requests, list):
            raise ValueError(
                f"release changelog Issue #{number} has malformed PR evidence"
            )
        for pull_request in sorted(pull_requests, key=lambda pr: pr.get("number", 0)
                                   if isinstance(pr, dict) else 0):
            pr_number = pull_request.get("number") if isinstance(pull_request, dict) else None
            pr_url = pull_request.get("url") if isinstance(pull_request, dict) else None
            if (not isinstance(pr_number, int) or pr_number < 1 or
                    pr_url != f"https://github.com/{repo}/pull/{pr_number}"):
                raise ValueError(
                    f"release changelog Issue #{number} has malformed PR evidence"
                )
            # Issue #707: an unmerged PR is not released content — its
            # link never enters the release notes.
            pr_state = pr_view(pr_number, "state", repo=repo).get("state")
            if pr_state != "MERGED":
                LOGGER.info(
                    "release_changelog_pr_link_dropped issue=%d pr=%d "
                    "state=%s", number, pr_number, pr_state,
                )
                continue
            links.append(f"[PR #{pr_number}]({pr_url})")
            author = (pull_request.get("author")
                      if isinstance(pull_request, dict) else None)
            login = author.get("login") if isinstance(author, dict) else None
            avatar = (author.get("avatarUrl")
                      if isinstance(author, dict) else None)
            if isinstance(login, str) and login and \
                    isinstance(avatar, str) and avatar:
                contributors.setdefault(login, avatar)
            else:
                # A ghosted/malformed author is display evidence, never a
                # release judge: skip the contributor, keep the release.
                LOGGER.info(
                    "release_changelog_contributor_skipped issue=%d pr=%d",
                    number, pr_number,
                )
        grouped[release_changelog_category(item)].append(
            (number, f"- {summary} ({'; '.join(links)})")
        )
    sections = ["## Changelog"]
    for category in RELEASE_CHANGELOG_CATEGORIES:
        entries = grouped[category]
        if entries:
            sections.extend(["", f"### {category}", "",
                             *(entry for _, entry in sorted(entries))])
    if contributors:
        avatar_rows = []
        for login in sorted(contributors):
            avatar = contributors[login]
            sized = avatar + ("&s=48" if "?" in avatar else "?s=48")
            avatar_rows.append(
                f'<a href="https://github.com/{login}">'
                f'<img src="{sized}" width="32" height="32" alt="{login}" /></a>'
            )
        sections.extend(["", "## Contributors", "", *avatar_rows])
    return "\n".join(sections)


def check_release_gates(repo: str, base_branch: str, release_commit: str,
                        release_number: int, *,
                        milestone: str | None = None,
                        ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS,
                        delivery_wait_seconds: float = RELEASE_DELIVERIES_WAIT_SECONDS,
                        delivery_waited_seconds: float = 0.0,
                        on_wait: Callable[[str], None] | None = None,
                        on_delivery_wait: Callable[[str], None] | None = None,
                        repo_has_ci: bool = False,
                        ) -> tuple[list[str], bool]:
    """Enforce the pre-release gates (Issue #98) and return their evidence.

    Returns ``(evidence, repo_has_ci)`` — the second element is whether
    any check run was observed on the gated commit, so the release flow
    can feed gate 1's observation (on the frozen base) into gate 2 (on
    the just-pushed version commit) as ``repo_has_ci``.

    Two gates, each checked against GitHub (never against local
    state), each failure raising with the concrete offender:

    1. No open Issue still carries `ai-in-progress`, `ai-pr-opened`
       or `ai-fix-needed` — the release Issue itself is excluded (it
       carries `ai-in-progress` while the release runs). With a
       `milestone`, the check is scoped to that Milestone (Issue #671:
       the documented `gh issue list --milestone <title>` filter), the
       same criterion as the #663 completeness gate: an in-flight Issue
       of another Milestone — or of no Milestone — is not this
       release's delivery and never blocks it. Without a `milestone`
       there is nothing to scope by and the pre-#671 repository-wide
       scan is unchanged.
    2. CI on the release commit is green: every check run reported by
       the GitHub API for the commit is `completed` with a
       `success`/`neutral`/`skipped` conclusion (a failing, cancelled
       or error check fails the gate; no check runs at all is recorded
       as such, not invented — EXCEPT when the caller passes
       ``repo_has_ci=True``, the frozen base already showed checks, so
       this repository runs CI and an empty list on a freshly pushed
       commit means the checks have not REGISTERED yet (GitHub creates
       CheckRuns seconds after the push, Issue #657): the gate then
       polls within the same budget until the first check appears and
       never passes an empty list as "nothing to gate"). A PENDING
       check (queued/in_progress —
       the release commit is born from the last delivery merge, so its
       CI is almost always still running, Issue #268) is not a
       conclusion: the gate polls until every check completes (one
       `release_waiting_ci` journal line per poll, the wait reflected
       through `on_wait`), then decides on the final conclusions.
       Waiting past `ci_wait_seconds` fails with its own timeout
       reason, explicitly distinct from a CI failure.

    Open PRs are deliberately NOT a gate (Issue #608, maintainer ruling
    2026-09-09, final): an open PR is queue state, never a release
    premise. The release contract is the milestone's closed-Issue scope
    check, green full tests and green CI on the release commit —
    whether open PRs exist, how many, or who authored them says nothing
    about the release. A stranded PR is the delivery loop's takeover /
    reconciliation job, not the release gate's.

    A real `gh` failure (auth, rate limit, API error) propagates
    unchanged — a gate that cannot be checked is a failed gate. The one
    exception is GitHub's HTTP 403 for the check-runs query: it is reported
    as a missing Checks:read credential permission so the blocked release is
    actionable.
    """
    evidence: list[str] = []
    open_deliveries: set[int] = set()
    for label in (IN_PROGRESS_LABEL, PR_OPENED_LABEL, FIX_NEEDED_LABEL):
        # Issue #671: the leftover-delivery check is scoped to the
        # release's Milestone, so an unrelated in-flight Issue can
        # no longer block the release indefinitely.
        issues = list_issues(
            repo, label=label, state="open", milestone=milestone,
            json_fields="number", limit=50,
        )
        for item in issues:
            number = int(item["number"])
            if number != release_number:
                open_deliveries.add(number)
    if open_deliveries:
        numbers = sorted(open_deliveries)
        detail = ", ".join(f"Issue #{number}" for number in numbers)
        LOGGER.info(
            "issue=%s release_waiting_deliveries open=%s waited=%ds limit=%ds",
            release_number, numbers, int(delivery_waited_seconds),
            int(delivery_wait_seconds),
        )
        if on_delivery_wait is not None:
            on_delivery_wait(
                f"{detail}; waited {int(delivery_waited_seconds)}s / "
                f"{int(delivery_wait_seconds)}s"
            )
        if delivery_waited_seconds >= delivery_wait_seconds:
            raise RuntimeError(
                f"release gate: waiting for open deliveries {numbers} "
                f"timed out after {int(delivery_wait_seconds)}s "
                "— delivery wait timeout, not a CI failure"
            )
        raise ReleaseDeliveriesWaiting(
            numbers, delivery_waited_seconds, delivery_wait_seconds,
        )
    scope = f" in milestone {milestone!r}" if milestone else ""
    evidence.append(
        f"no open Issue{scope} carries "
        f"{IN_PROGRESS_LABEL} / {PR_OPENED_LABEL} / {FIX_NEEDED_LABEL}"
    )
    def fetch_check_runs() -> list[dict]:
        try:
            return commit_check_runs(repo, release_commit)
        except subprocess.CalledProcessError as error:
            output = "\n".join(
                str(value) for value in (error.stdout, error.stderr)
                if value
            )
            if "403" in output:
                raise RuntimeError(
                    "CI gate could not be evaluated: the credential lacks "
                    "Checks:read permission (GitHub returned HTTP 403 while "
                    "listing check runs)"
                ) from error
            raise

    check_runs = fetch_check_runs()
    waited = 0.0
    while True:
        pending = [
            f"check '{check.get('name')}' is "
            f"{check.get('status')}/{check.get('conclusion')}"
            for check in check_runs if check.get("status") != "completed"
        ]
        if repo_has_ci and not check_runs:
            # Issue #657: an empty list on a just-pushed commit in a CI
            # repository is "not registered yet", not "nothing to gate"
            # — wait for the first check within the same budget; a
            # timeout below fails with the wait-timeout reason.
            pending = [
                "the first check run to register on the just-pushed "
                f"commit {release_commit} (GitHub creates CheckRuns "
                "seconds after the push)"
            ]
        if not pending:
            break
        detail = ", ".join(pending)
        LOGGER.info(
            "issue=%s release_waiting_ci commit=%s pending=%s "
            "waited=%ds limit=%ds",
            release_number, release_commit, detail,
            int(waited), int(ci_wait_seconds),
        )
        if on_wait is not None:
            on_wait(
                f"{detail}; waited {int(waited)}s / "
                f"{int(ci_wait_seconds)}s"
            )
        if waited >= ci_wait_seconds:
            raise RuntimeError(
                f"release gate: waiting for CI on the release commit "
                f"{release_commit} timed out after "
                f"{int(ci_wait_seconds)}s (still pending: {detail}) "
                "— wait timeout, not a CI failure"
            )
        step = min(RELEASE_CI_POLL_INTERVAL, ci_wait_seconds - waited)
        time.sleep(step)
        waited += step
        check_runs = fetch_check_runs()
    for check in check_runs:
        name = check.get("name")
        status = check.get("status")
        conclusion = check.get("conclusion")
        if status != "completed" or conclusion not in (
            "success", "neutral", "skipped",
        ):
            raise RuntimeError(
                f"release gate: CI check '{name}' is {status}/{conclusion} "
                f"on the release commit {release_commit}"
            )
    if check_runs:
        evidence.append(
            f"CI on the release commit: {len(check_runs)} check(s) all "
            "success/neutral/skipped"
            + (f" (waited {int(waited)}s for pending checks)" if waited
               else "")
        )
    else:
        evidence.append(
            f"CI on the release commit: no check runs on "
            f"{release_commit} (nothing to gate)"
        )
    # Issue #608: no open-PR gate — an open PR is queue state, not a
    # release premise (see the docstring for the maintainer ruling).
    return evidence, bool(check_runs)


def prepare_release_version(worktree: Path, tag: str,
                            base_branch: str,
                            version_file: str = "pyproject.toml") -> str:
    """Commit the tag's version into the declared metadata source.

    The release tag is the public identity (for example ``v0.3.0``), while
    metadata version fields omit the leading ``v``.  The selected source
    must be structurally recognizable before it is changed; the commit is
    pushed directly to the release base, matching the release docs-sync step.
    """
    match = re.fullmatch(r"v([0-9]+(?:\.[0-9]+)+)", tag)
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
            run_command([
                "git", "commit", "-m", f"chore: prepare release {tag}",
            ], cwd=worktree)
            run_git_network_command(
                ["git", "push", "origin", f"HEAD:refs/heads/{base_branch}"],
                cwd=worktree,
            )
        return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()
    if version_file not in ("pyproject.toml", "package.json", "composer.json"):
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
            run_command([
                "git", "commit", "-m", f"chore: prepare release {tag}",
            ], cwd=worktree)
            run_git_network_command(
                ["git", "push", "origin", f"HEAD:refs/heads/{base_branch}"],
                cwd=worktree,
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
        run_command([
            "git", "commit", "-m", f"chore: prepare release {tag}",
        ], cwd=worktree)
        run_git_network_command(
            ["git", "push", "origin", f"HEAD:refs/heads/{base_branch}"],
            cwd=worktree,
        )
    return run_command(["git", "rev-parse", "HEAD"], cwd=worktree).strip()


def release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return the commit the tag points to on the remote, or None.

    (Issue #98) `git ls-remote` keeps the existence probe read-only and
    its exit semantics unambiguous: exit 0 with empty output means the
    remote has no such tag (None); ANY non-zero exit is a real failure
    (network/auth) and propagates. The previous `git fetch` probe read
    exit 128 as "missing", but a network failure also exits 128 — a
    transient outage would read as "no tag on the remote", the retry
    would re-create the tag, and a local residue from the failed push
    deadlocked the release ticket (Issue #585). Annotated tags are
    peeled with the `^{}` line ls-remote reports alongside the tag
    object.

    The base-sync lock is kept: `ls-remote` does not write refs, but
    the sibling steps around it do, and the lock orders them against
    task worktrees sharing the checkout's common dir.
    """
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        output = run_git_network_command(
            ["git", "ls-remote", "origin",
             f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            cwd=repo_dir,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    refs: dict[str, str] = {}
    for line in output.splitlines():
        oid, _, ref = line.partition("\t")
        refs[ref.strip()] = oid.strip()
    peeled = refs.get(f"refs/tags/{tag}^{{}}")
    if peeled:
        return peeled
    return refs.get(f"refs/tags/{tag}")


def ensure_release_tag_pushed(repo_dir: Path, tag: str,
                              release_commit: str) -> None:
    """Create the annotated release tag and push it — idempotently.

    本地残留收敛：上次 `git tag` 成功但 push 失败会留下本地 tag，重试
    时远端仍无此 tag，不处理残留的话 `git tag -a` 永远 fatal: tag
    already exists（发布票死锁，Issue #585）。残留指向本次发布提交 →
    跳过重建直接重推；指向别的提交 → fail fast——已有的 tag 永不移动
    或覆盖（与 `release_tag_commit` 的远端侧同一不变量）。
    """
    try:
        local_tag_commit = run_command(
            ["git", "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}^{{commit}}"],
            cwd=repo_dir,
        ).strip()
    except subprocess.CalledProcessError:
        local_tag_commit = None
    if local_tag_commit is None:
        run_command(
            ["git", "tag", "-a", tag, "-m", f"Release {tag}",
             release_commit],
            cwd=repo_dir,
        )
    elif local_tag_commit != release_commit:
        raise RuntimeError(
            f"local tag {tag} already exists and points at "
            f"{local_tag_commit}, not the release commit "
            f"{release_commit} — an existing tag is never moved or "
            "overwritten"
        )
    run_git_network_command(
        ["git", "push", "origin", f"refs/tags/{tag}"],
        cwd=repo_dir,
    )


def tag_commit_is_ancestor_of_base(tag_commit: str, base_commit: str,
                                   repo_dir: Path) -> bool:
    """Compatibility wrapper for the release state machine."""
    return _is_ancestor(tag_commit, base_commit, cwd=repo_dir)


def publish_release(*, repo: str, tag: str, version: str,
                    release_commit: str, changelog: str,
                    scope_evidence: list[str], gate_evidence: list[str], test_evidence: str,
                    run_id: str, issue_number: int) -> str:
    """Create the GitHub Release for the tag — idempotently (Issue #98).

    When a Release for the tag already exists (a restart after a
    successful `gh release create`) its URL is reused, never a second
    Release is created. A legacy existing Release without this changelog
    is upgraded in place; a Release that already contains it is unchanged.
    Otherwise the Release is created from the EXISTING tag (the tag was
    created and pushed by the caller first — `gh release create` never
    creates or moves the tag itself) with notes carrying the full
    verification evidence: version, tag,
    release commit, the per-item scope evidence, the gate evidence, the
    test evidence and the run marker (the same stable machine-readable
    marker every run comment carries).
    """
    notes = "\n".join([
        f"# {version}",
        "",
        changelog,
        "",
        f"- tag: `{tag}`",
        f"- release commit: `{release_commit}`",
        f"- release task: Issue #{issue_number}",
        "",
        "## Scope (verified item by item)",
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        "## Pre-release gates",
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        "## Tests",
        "",
        f"- {test_evidence}",
        "",
        run_marker(run_id),
        f"run_id={run_id}",
    ])
    try:
        release = release_view(repo, tag, fields="tagName,url,body")
        if changelog not in release.get("body", ""):
            release_edit_notes(repo, tag, notes=notes)
        return release["url"]
    except subprocess.CalledProcessError as exc:
        if "not found" not in (exc.stderr or ""):
            raise
    release_create(repo, tag=tag, version=version, notes=notes)
    return release_view(repo, tag, fields="tagName,url")["url"]


# Issue #754: the GitHub issue index is eventually consistent —
# `gh issue close` returning success does not mean the
# `issues?milestone=N&state=open` list reflects it yet, and v0.4.10 read
# a stale non-empty list in the same second as the close. A non-empty
# gate read is re-checked with these backoff delays first; only a list
# that stays non-empty across all of them fails fast (bounded ≈ 10 s
# including the reads — a real unfinished issue must not drag the
# release out).
MILESTONE_OPEN_RETRY_DELAYS = (1.0, 2.0, 4.0)


def close_release_milestone(repo: str, version: str, *, run_id: str | None = None) -> str:
    """Close the Milestone whose title is exactly `version` (Issue #214).

    Runs on the release success path (after the tag is pushed, the
    GitHub Release is published and the release Issue is closed with
    `ai-merged`). The Milestone is matched by EXACT title — never
    guessed, never fuzzy-matched, never a different Milestone:

    - no Milestone with that exact title -> fail fast (the release
      must not be reported as fully successful);
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - already `closed` -> idempotent success (no mutation, no reopen);
    - `open` with open issues -> fail fast with the version, the
      Milestone number/url and the open issue list — but only after
      bounded backoff re-reads (Issue #754: the issue index is
      eventually consistent, so a list read in the same second as the
      release Issue's `gh issue close` can still show it as open);
    - `open` with 0 open issues -> closed via the official REST
      contract `PATCH /repos/{owner}/{repo}/milestones/{number}`
      with `state=closed` (OpenAPI `issues/update-milestone`).

    The list query asks for `state=all`: the default `state=open`
    would hide already-closed Milestones and break the idempotent
    case. Returns a short evidence string for the success path. A
    real `gh` failure (auth, rate limit, API error) propagates
    unchanged — like the release gates, a check that cannot be made
    is a failed check.
    """
    milestones = list_milestones(repo)
    matches = [
        m for m in milestones
        if isinstance(m, dict) and m.get("title") == version
    ]
    if not matches:
        raise RuntimeError(
            f"release {version}: no Milestone with the exact title "
            f"{version!r} in {repo} — the Milestone is missing, never "
            "guessed or fuzzy-matched"
        )
    if len(matches) > 1:
        numbers = ", ".join(
            f"#{m.get('number')} ({m.get('html_url') or m.get('url')})"
            for m in matches
        )
        raise RuntimeError(
            f"release {version}: {len(matches)} Milestones share the "
            f"exact title {version!r} in {repo} — ambiguous, refusing "
            f"to guess which to close: {numbers}"
        )
    milestone = matches[0]
    number = milestone.get("number")
    html_url = milestone.get("html_url") or milestone.get("url")
    if milestone.get("state") == "closed":
        return (
            f"Milestone #{number} ({html_url}) already closed — "
            "idempotent success, nothing to do"
        )
    open_issues = milestone_open_issues(repo, int(number))
    epic_evidence: list[str] = []
    if run_id is not None and open_issues:
        # Every listed Epic is verified from its children and blockers before
        # the authoritative exact-Milestone list is checked again.
        epic_evidence = reconcile_release_epics(repo, int(number), version, run_id)
        open_issues = milestone_open_issues(repo, int(number))
    retries = 0
    while open_issues and retries < len(MILESTONE_OPEN_RETRY_DELAYS):
        # The release Issue close succeeded seconds ago; a non-empty read
        # here is more likely the index lagging (Issue #754) than real
        # unfinished work. Re-read with bounded backoff; the raise below
        # fires only once the list stays non-empty across all retries.
        time.sleep(MILESTONE_OPEN_RETRY_DELAYS[retries])
        retries += 1
        open_issues = milestone_open_issues(repo, int(number))
    if open_issues:
        listing = ", ".join(
            f"#{i.get('number')} {i.get('title')}" for i in open_issues
        )
        raise RuntimeError(
            f"release {version}: Milestone #{number} ({html_url}) still "
            f"has {len(open_issues)} open issue(s) — closing it would hide "
            f"unfinished work; open issues: {listing}"
        )
    close_milestone(repo, int(number))
    epic_suffix = f"; {'; '.join(epic_evidence)}" if epic_evidence else ""
    return (
        f"Milestone #{number} ({html_url}) closed after release "
        f"{version} (0 open issues){epic_suffix}"
    )


RELEASE_DOCS_LATEST_MARKER_EN = " (latest)"
RELEASE_DOCS_LATEST_MARKER_ZH = "（最新）"


def release_docs_page(*, version: str, tag_object: str,
                      release_commit: str, published_at: str,
                      release_url: str, issue_number: int,
                      body: str, language: str) -> str:
    """Build one docs-site Release notes page for a published release.

    (Issue #275) The page content is the published GitHub Release body
    (no changelog re-implementation — #204 owns that) plus the meta the
    existing release pages share: the tag/release-commit mapping, the
    publish time and the release task Issue number. Two mechanical
    adaptations only: the body's own leading `# <version>` heading is
    dropped because the page carries its own title with the `(latest)`
    marker, and HTML comment lines (`<!-- ... -->`, the run markers) are
    dropped because the Mintlify MDX parser rejects them — the visible
    `run_id=` line stays, so the correlation is kept.
    """
    notes = body.strip()
    lines = notes.splitlines()
    if lines and lines[0].strip() == f"# {version}":
        lines = lines[1:]
    lines = [
        line for line in lines
        if not line.strip().startswith("<!--")
    ]
    notes = "\n".join(lines).strip()
    if language == "en":
        title = f"# {version} release (latest)"
        intro = (
            f"`{version}` was published {published_at} as the GitHub "
            f"Release [{version}]({release_url}) (release task: "
            f"Issue #{issue_number})."
        )
        heading = "## Tag state (verified against origin)"
        table = (
            "| Ref | Object | Points at |\n"
            "|---|---|---|\n"
            f"| `{version}` | annotated tag `{tag_object}` "
            f"| commit `{release_commit}` |"
        )
    elif language == "zh":
        title = f"# {version} 发布（最新）"
        intro = (
            f"`{version}` 于 {published_at} 发布为 GitHub Release "
            f"[{version}]({release_url})（release task："
            f"Issue #{issue_number}）。"
        )
        heading = "## Tag 状态（对 origin 验证）"
        table = (
            "| Ref | 对象 | 指向 |\n"
            "|---|---|---|\n"
            f"| `{version}` | 注解 tag `{tag_object}` "
            f"| 提交 `{release_commit}` |"
        )
    else:
        raise ValueError(
            f"release docs page language {language!r} is not supported "
            "(use 'en' or 'zh')"
        )
    return "\n".join([
        title, "",
        intro, "",
        heading, "",
        table, "",
        "## Release notes", "",
        notes, "",
    ])


def current_latest_release_slug(config_text: str) -> str:
    """The first page of the English `Releases` group — the current
    latest release (the groups are latest-first, Issue #154)."""
    config = json.loads(config_text)
    for lang in config["navigation"]["languages"]:
        if lang.get("language") != "en":
            continue
        for group in lang["groups"]:
            if group.get("group") == "Releases":
                pages = group["pages"]
                if not pages:
                    raise RuntimeError(
                        "release docs sync: the Releases group has no "
                        "pages — cannot determine the current latest "
                        "release"
                    )
                return str(pages[0])
    raise RuntimeError(
        "release docs sync: docs.json has no English Releases group — "
        "cannot determine the current latest release"
    )


def update_release_navigation(config_text: str, slug: str) -> tuple[str, bool]:
    """Insert `slug` at the head of both release navigation groups.

    (Issue #275) The `Releases` (en) and `发布` (zh) groups list the
    releases latest-first; a new release goes FIRST in both (the zh
    entries carry the `zh/` prefix). A slug already listed in both
    groups leaves the config untouched (idempotent). Exactly one group
    updated means a broken config — fail fast, never guess.
    """
    config = json.loads(config_text)
    updated = 0
    for lang in config["navigation"]["languages"]:
        for group in lang["groups"]:
            if group.get("group") not in ("Releases", "发布"):
                continue
            pages = group["pages"]
            entry = f"zh/{slug}" if group["group"] == "发布" else slug
            if entry in pages:
                continue
            pages.insert(0, entry)
            updated += 1
    if updated == 0:
        return config_text, False
    if updated != 2:
        raise RuntimeError(
            f"release docs sync: expected exactly two release groups "
            f"(Releases + 发布) but updated {updated} — the docs.json "
            "navigation is not the expected Mintlify i18n layout"
        )
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n", True


def move_latest_marker(worktree: Path, old_slug: str,
                       new_slug: str, *, resume: bool) -> list[str]:
    """Move the `(latest)` title marker off the previous latest page.

    (Issue #275) Only the newest release page may carry the marker:
    ` (latest)` (en) / `（最新）` (zh) is stripped from the previous
    latest page's H1. When the old page already lacks the marker the
    move is only accepted on a resume (`resume=True`: the new page
    already carries the marker — a partial step of an earlier attempt
    already moved it); otherwise the invariant is broken and the step
    fails fast — a broken state is never silently repaired. Returns the
    changed relative paths.
    """
    changed: list[str] = []
    for directory, marker in (("docs", RELEASE_DOCS_LATEST_MARKER_EN),
                              ("docs/zh", RELEASE_DOCS_LATEST_MARKER_ZH)):
        path = worktree / directory / f"{old_slug}.mdx"
        if not path.is_file():
            raise RuntimeError(
                f"release docs sync: previous latest page {path} is "
                "missing — cannot move the (latest) marker"
            )
        text = path.read_text(encoding="utf-8")
        first_line, _, rest = text.partition("\n")
        if marker in first_line:
            path.write_text(
                first_line.replace(marker, "", 1) + "\n" + rest,
                encoding="utf-8",
            )
            changed.append(f"{directory}/{old_slug}.mdx")
            continue
        if resume:
            new_path = worktree / directory / f"{new_slug}.mdx"
            new_first = (
                new_path.read_text(encoding="utf-8").splitlines()[0]
                if new_path.is_file() else ""
            )
            if marker in new_first:
                continue
        raise RuntimeError(
            f"release docs sync: {path} does not carry the (latest) "
            "marker in its title and the move did not happen yet — "
            "the latest-marker invariant is broken, refusing to guess"
        )
    return changed


def sync_release_docs(*, source_repo: str, repo_dir: Path,
                      worktree: Path, base_branch: str, tag: str,
                      release_commit: str, issue_number: int) -> str:
    """Sync the docs-site Release notes for one published release.

    (Issue #275) Release state machine step 8 — runs AFTER the GitHub
    Release exists (step 7) and BEFORE the Milestone is closed (step 9):

    - fetches the published Release (`gh release view`, the same call
      `publish_release` uses) — the page content is that body, no
      changelog re-implementation;
    - generates `docs/release-<tag>.mdx` and
      `docs/zh/release-<tag>.mdx` in the release worktree with the meta
      the existing release pages share (tag/release-commit mapping,
      publish time, release task Issue number);
    - moves the `(latest)` title marker from the previous latest page;
    - inserts the new version at the head of the `Releases`/`发布`
      navigation groups in `docs/docs.json` (both languages), or skips
      this Mintlify-only step when that file is absent;
    - commits exactly those docs paths in the release worktree and
      pushes `HEAD:refs/heads/<base_branch>` under the base-sync lock
      (the release path has no PR — a direct commit to the base branch,
      never a force push).

    Idempotent: an existing page with identical content is neither
    regenerated nor overwritten, and a run with nothing to change
    commits nothing. An existing page with DIFFERENT content fails fast
    (never overwritten). Any failure propagates so the release fails
    fast and enters `ai-blocked` like every other step.
    """
    docs_config = worktree / "docs" / "docs.json"
    if not docs_config.is_file():
        return "docs sync skipped (no Mintlify docs in repo)"

    release = release_view(source_repo, tag,
                           fields="tagName,publishedAt,url,body")
    body = release.get("body")
    if not isinstance(body, str) or not body.strip():
        raise RuntimeError(
            f"release {tag}: the GitHub Release body is empty — the "
            "docs page would be fabricated, refusing"
        )
    published_at = release["publishedAt"]
    release_url = release["url"]
    tag_object = run_command(
        ["git", "rev-parse", f"refs/tags/{tag}"], cwd=repo_dir,
    )
    new_slug = f"release-{tag}"
    en_path = worktree / "docs" / f"{new_slug}.mdx"
    zh_path = worktree / "docs" / "zh" / f"{new_slug}.mdx"
    en_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=published_at, release_url=release_url,
        issue_number=issue_number, body=body, language="en",
    )
    zh_content = release_docs_page(
        version=tag, tag_object=tag_object, release_commit=release_commit,
        published_at=published_at, release_url=release_url,
        issue_number=issue_number, body=body, language="zh",
    )
    # A pre-existing identical page means this is a resume after a
    # partial step — the marker move may then be lenient (it already
    # happened). A page created by THIS run demands the strict move.
    new_page_preexisting = (
        en_path.is_file()
        and en_path.read_text(encoding="utf-8") == en_content
    )
    for path, content in ((en_path, en_content), (zh_path, zh_content)):
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                continue
            raise RuntimeError(
                f"release {tag}: {path} already exists with different "
                "content — an existing release page is never overwritten"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config_text = docs_config.read_text(encoding="utf-8")
    old_slug = current_latest_release_slug(config_text)
    if old_slug != new_slug:
        move_latest_marker(
            worktree, old_slug, new_slug, resume=new_page_preexisting,
        )
    new_config_text, nav_changed = update_release_navigation(
        config_text, new_slug,
    )
    if nav_changed:
        docs_config.write_text(new_config_text, encoding="utf-8")
    expected_paths = [
        f"docs/{new_slug}.mdx",
        f"docs/zh/{new_slug}.mdx",
        "docs/docs.json",
    ]
    if old_slug != new_slug:
        expected_paths += [
            f"docs/{old_slug}.mdx",
            f"docs/zh/{old_slug}.mdx",
        ]
    fd = acquire_base_sync_lock(repo_dir, 300.0)
    try:
        run_command(["git", "add", *expected_paths], cwd=worktree)
        try:
            run_command(["git", "diff", "--cached", "--quiet"],
                        cwd=worktree)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
        else:
            # Nothing staged can only mean the docs commit of a previous
            # run is already part of this tick's frozen base: the release
            # state machine hard-resets the worktree to release_commit in
            # create_release_worktree BEFORE this function runs, so a
            # local docs commit whose push failed cannot survive to this
            # point — the resume regenerates the pages below and takes
            # the normal commit+push path (#623; the #587 world is
            # unreachable, and a stale origin/<base> tracking ref must
            # not be answered with a false "recovered" no-op push).
            return (
                f"docs release notes for {tag} already in sync — "
                "idempotent no-op, nothing committed"
            )
        run_command([
            "git", "commit", "-m",
            f"docs: release notes for {tag} (Issue #{issue_number})",
        ], cwd=worktree)
        run_git_network_command(
            ["git", "push", "origin", f"HEAD:refs/heads/{base_branch}"],
            cwd=worktree,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return (
        f"docs release notes for {tag} committed and pushed to "
        f"{base_branch}"
    )


def release_success_comment_body(run_id: str, run_info: str,
                                 release_url: str, tag: str,
                                 release_commit: str,
                                 scope_evidence: list[str],
                                 gate_evidence: list[str],
                                 test_evidence: str,
                                 docs_evidence: str,
                                 milestone_evidence: str) -> str:
    """The terminal success comment: the full verification evidence.

    (Issue #98) The comment is the auditable record of the release:
    run marker, release URL, version/tag/release commit, the
    per-item scope evidence, the gate evidence, the test evidence, the
    docs-site Release notes evidence (Issue #275) and the Milestone
    evidence (Issue #214: the Milestone whose title is the released
    version is closed on the success path).
    """
    info = _run_info_fields(run_info)
    return "\n".join([
        field_block(run_id, f"Orbi released: {release_url}", {
            **info, "tag": tag, "release_commit": release_commit,
        }),
        "",
        "## Scope (verified item by item)",
        "",
        *(f"- {item}" for item in scope_evidence),
        "",
        "## Pre-release gates",
        "",
        *(f"- {item}" for item in gate_evidence),
        "",
        "## Tests",
        "",
        f"- {test_evidence}",
        "",
        "## Release notes (docs site)",
        "",
        f"- {docs_evidence}",
        "",
        "## Milestone",
        "",
        f"- {milestone_evidence}",
        "",
        f"run_id={run_id}",
    ])


def release_failure_comment_body(run_id: str, run_info: str,
                                 error: str) -> str:
    """The terminal failure comment: the blocked scene (Issue #98).

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker and the concrete
    reason, so the recoverable scene is on GitHub, not only in the
    journal.
    """
    return field_block(
        run_id, "Orbi release failed (ai-blocked)", {
            **_run_info_fields(run_info), "failure": error,
        },
    )


def process_release(issue: dict, config: RunnerConfig,
                   source_repo: str) -> str:
    """Run the deterministic release state machine for one release Issue.

    (Issue #98) A release task NEVER enters the normal `run_pi`
    development path: the Runner executes the state machine itself,
    step by step, each step idempotent so a restart resumes the same
    run (same run id, same worktree) from the top:

    1. Strictly parse the `## Release` declaration from the Issue
       body (version, base_branch, scope or
       scope_from_milestone — exactly one of the two, Issue #253; a
       legacy `test_command` field is ignored with one evidence
       line — the declaration carries no local test contract,
       Issue #569).
    2. Freeze the base — the release commit is exactly
       `origin/<base_branch>` (fetched under the base-sync lock).
    2b. Prove the declared (or defaulted) `version_file` exists at the
       root of the frozen release tree (`verify_release_version_file`,
       Issue #740) — before any gate wait, so a declaration/repo
       mismatch fails fast at claim time with the supported files that
       do exist, never as a late FileNotFoundError after the gates.
    3. Enforce the pre-release gates (`check_release_gates`).
    4. When `scope_from_milestone` is declared, derive the scope from
       the Milestone (`derive_release_scope_from_milestone`): closed
       Issues + merged PRs; open items are surfaced as evidence, never
       released. Then verify the scope item by item
       (`verify_release_scope`).
    Before step 5, prepare the release version in the clean worktree using
       the declared `version_file` (`pyproject.toml` by default,
       `package.json`, or `none`), commit and push when metadata changes.
       The subsequent steps run against that commit.
    5. Test acceptance is the #268 CI-wait gate on the release commit:
       after the version bump the gates re-run against that exact
       commit, and a red or timed-out CI takes the existing recoverable
       failure path. No local test execution (Issue #569).
    6. Tag: the remote tag must not exist or must point EXACTLY at
       the release commit (a mismatch fails — an existing tag is
       never moved); otherwise create an annotated tag at the release
       commit and push it with a plain push (never `--force`).
    7. Publish the GitHub Release (idempotent) with the full
       verification evidence.
    8. Sync the docs-site Release notes (Issue #275): generate
       `docs/release-<version>.mdx` + `docs/zh/release-<version>.mdx`
       from the published Release body, update both navigation groups,
       move the `(latest)` marker, and commit + push those docs changes
       to the base branch directly. Idempotent: identical pages are not
       overwritten; anything else fails fast.
    9. Apply `ai-merged` and close the release Issue (terminal delivery
       transition).
    10. Close the Milestone whose title is EXACTLY the released
        version (Issue #214), then write the success comment (release
        URL, tag, commit, evidence, docs-site Release notes evidence,
        Milestone evidence). Exact title match only; close it only when
        it has 0 open Issues; already-closed is idempotent. A missing
        Milestone or remaining open Issues fails fast. Any failure is
        `ai-blocked` ALONE, with a concrete failure comment, and the
        handled failure returns cleanly so the tick does not crash.
    """
    number = int(issue["number"])
    title = issue["title"]
    run_id = new_run_id()
    set_run_id(run_id)
    if has_in_progress_label(number, source_repo):
        existing_run_id = latest_run_id(
            config.repo_dir, source_repo, number,
        )
        if existing_run_id is not None:
            run_id = existing_run_id
            set_run_id(run_id)
            LOGGER.info(
                "issue=%s release_resuming_run run_id=%s", number, run_id,
            )
    priority = issue_priority(issue)
    started = time.monotonic()
    # Bound before the try: the failure comment needs it even when the
    # declaration parse fails on the very first step.
    run_info = f"run_id={run_id} priority={priority}"
    publisher = ProgressPublisher(
        number, source_repo, run_id, run_command=run_command,
    )
    branch = task_branch(source_repo, number, run_id)
    worktree = worktree_path(
        config.repo_dir, source_repo, number, run_id,
    )
    publish = functools.partial(
        _safe_publish, run_id=run_id, issue=number,
        source_repo=source_repo, role=ROLE_RELEASE,
    )

    def progress() -> dict:
        return _progress_state(
            issue=number, title=title, run_id=run_id, role=ROLE_RELEASE,
            branch=branch, worktree=worktree, started=started,
            pr_url=None, review_round=0, priority=priority,
            activity={},
        )

    def on_delivery_wait(detail: str) -> None:
        state = progress()
        state["phase"] = f"waiting deliveries: {detail}"
        publish(
            action=lambda: publisher.patch(_progress_body(state)),
        )

    def on_ci_wait(detail: str) -> None:
        # Issue #268: the CI wait is reflected in the live progress
        # comment (pure bypass, Issue #79); the gate itself emits the
        # `release_waiting_ci` journal line.
        state = progress()
        state["phase"] = f"waiting CI: {detail}"
        publish(
            action=lambda: publisher.patch(_progress_body(state)),
        )

    release_commit: str | None = None
    declaration: dict | None = None
    open_milestone_evidence: list[str] = []
    try:
        declaration = parse_release_declaration(issue["body"])
        if declaration["test_command"] is not None:
            # Issue #569: a legacy `test_command` line is accepted and
            # ignored with this single evidence line — it is never
            # executed; test acceptance is the CI-wait gate.
            LOGGER.info(
                "issue=%s release_test_command_ignored value=%r "
                "(release tests are gated by GitHub Actions CI on the "
                "release commit)", number, declaration["test_command"],
            )
        base_branch = declaration["base_branch"]
        run_info = (
            f"base_branch={base_branch} run_id={run_id} "
            f"priority={priority}"
        )
        LOGGER.info(
            "issue=%s release_task %s", number, run_info,
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM,
            current_labels={label.get("name") for label in issue.get(
                "labels", []) if isinstance(label, dict)
                and isinstance(label.get("name"), str)},
        )
        set_active_run(
            number, title, branch, str(worktree),
        )
        publish(
            action=lambda: publisher.ensure(progress_body(progress())),
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release started**: {run_info}",
            ),
        )
        release_commit = freeze_base(config.repo_dir, base_branch)
        # Issue #740: the declared (or defaulted) version_file is proven
        # to exist in the frozen release tree BEFORE any gate wait — a
        # mismatch used to surface only at the version-write step, after
        # the gates and the scope verification, and burned the ticket
        # ai-blocked with a bare FileNotFoundError.
        verify_release_version_file(
            config.repo_dir, release_commit,
            declaration["version_file"],
        )
        wait_started: float | None = None
        # Waiting is persisted in the auditable Issue comment, so a later
        # tick can enforce one bounded waiting window without local state.
        for comment in issue_comments(number, repo=source_repo):
            # Only the runner's trusted, structured waiting comments may
            # carry the persisted timer.  A public comment must not be able
            # to inject an old timestamp and turn a recoverable wait into an
            # immediate terminal block (the same trust boundary as resume
            # scenes, Issue #45).
            if not _comment_is_trusted(comment):
                continue
            body = comment.get("body", "")
            if "Orbi release waiting for deliveries" not in body:
                continue
            match = re.search(r"wait_started: ([0-9]+(?:\.[0-9]+)?)", body)
            if match:
                started_at = float(match.group(1))
                wait_started = (
                    started_at if wait_started is None
                    else min(wait_started, started_at)
                )
        release_waited_seconds = (
            max(0.0, time.time() - wait_started)
            if wait_started is not None else 0.0
        )
        run_info = (
            f"base_branch={base_branch} base_sha={release_commit} "
            f"run_id={run_id} priority={priority}"
        )
        # Issue #671: the leftover-delivery gate is scoped to the same
        # Milestone as the #663 completeness gate — the release Issue's own
        # GitHub Milestone, `active_milestone` fallback.
        target_milestone = release_target_milestone(
            issue, config.active_milestone,
        )
        gate_evidence, repo_has_ci = check_release_gates(
            source_repo, base_branch, release_commit, number,
            milestone=target_milestone,
            # Issue #268: the gate waits out pending CI checks on the
            # release commit; the real load_config always provides the
            # key, the module constant stays the fallback for hand-built
            # configs.
            ci_wait_seconds=config.release_ci_wait_seconds,
            delivery_wait_seconds=config.release_deliveries_wait_seconds,
            delivery_waited_seconds=release_waited_seconds,
            on_wait=on_ci_wait,
            on_delivery_wait=on_delivery_wait,
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release gates passed**: "
                f"{'; '.join(gate_evidence)}",
            ),
        )
        if declaration.get("scope_from_milestone") is not None:
            # Issue #253: the scope is derived from the Milestone, then
            # verified item by item exactly like a hand-listed scope.
            derived_scope, open_milestone_evidence = (
                derive_release_scope_from_milestone(
                    source_repo, declaration["scope_from_milestone"],
                )
            )
            if not derived_scope:
                raise RuntimeError(
                    f"release {declaration['version']}: Milestone "
                    f"{declaration['scope_from_milestone']!r} has no "
                    "closed Issue or merged PR — the derived scope is "
                    "empty and a release needs at least one delivery"
                )
            declaration["scope"] = derived_scope
            if open_milestone_evidence:
                LOGGER.warning(
                    "issue=%s release_milestone_open_items "
                    "milestone=%s open_items=%s",
                    number, declaration["scope_from_milestone"],
                    "; ".join(open_milestone_evidence),
                )
        scope_evidence = verify_release_scope(
            source_repo, declaration["scope"], config.repo_dir,
            release_commit,
        )
        if open_milestone_evidence:
            # Open items are NOT released; they are surfaced in the
            # auditable evidence instead of being silently swallowed.
            scope_evidence = scope_evidence + [
                f"NOT released (still open in milestone "
                f"{declaration['scope_from_milestone']}): {item}"
                for item in open_milestone_evidence
            ]
        changelog = build_release_changelog(source_repo, declaration["scope"])
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release scope verified**: "
                f"{'; '.join(scope_evidence)}",
            ),
        )
        worktree = create_release_worktree(
            config.repo_dir, source_repo, number, run_id, release_commit,
        )
        # Version metadata is part of the release commit, not a post-release
        # fix: tests and the tag must identify the exact same commit.
        if declaration["version_file"] == "pyproject.toml":
            # Keep the default invocation compatible with existing callers;
            # the omitted field is the unchanged Python release path.
            release_commit = prepare_release_version(
                worktree, declaration["version"], base_branch,
            )
        else:
            release_commit = prepare_release_version(
                worktree, declaration["version"], base_branch,
                declaration["version_file"],
            )
        # Version preparation creates the commit that will be tagged. Re-run
        # the commit-specific gates so the recorded CI result and final
        # no-open-PR check cover that exact release commit, not the frozen
        # pre-version source commit. The just-pushed commit may hit the
        # CheckRun registration lag, so gate 1's observation (checks on the
        # frozen base = this repository runs CI) forbids the empty-list
        # pass here (Issue #657).
        gate_evidence, _ = check_release_gates(
            source_repo, base_branch, release_commit, number,
            milestone=target_milestone,
            repo_has_ci=repo_has_ci,
            ci_wait_seconds=config.release_ci_wait_seconds,
            delivery_wait_seconds=config.release_deliveries_wait_seconds,
            delivery_waited_seconds=release_waited_seconds,
            on_wait=on_ci_wait,
            on_delivery_wait=on_delivery_wait,
        )
        # The release version changed the packaging inputs after the tick's
        # preflight refresh. Refresh Orbi from its deployment checkout, not
        # the published source worktree: a foreign release (for example a
        # Node-only repo) is not required to carry Orbi's pyproject.toml.
        # The fallback keeps direct callers with legacy hand-built configs
        # compatible; load_config always supplies deploy_home.
        deployment_home = (
            config.deploy_home
            if config.deploy_home is not None
            else config.repo_dir
        )
        refresh_cli_install(
            deployment_home, lock_repo_dir=deployment_home,
            run_command=run_command,
        )
        # Issue #569: there is NO local test execution — the CI-wait gate
        # re-run above already decided the test acceptance on this exact
        # release commit; a red or timed-out CI took the recoverable
        # failure path there.
        test_evidence = (
            f"release tests gated by GitHub Actions CI on the release "
            f"commit {release_commit} (the #268 CI-wait gate; no local "
            "test execution)"
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release tests passed**: {test_evidence}",
            ),
        )
        tag = declaration["version"]
        existing_tag_commit = release_tag_commit(config.repo_dir, tag)
        if existing_tag_commit is not None:
            if existing_tag_commit == release_commit:
                LOGGER.info(
                    "issue=%s release_tag_exists tag=%s commit=%s",
                    number, tag, existing_tag_commit,
                )
            elif tag_commit_is_ancestor_of_base(
                    existing_tag_commit, release_commit,
                    config.repo_dir):
                # Issue #275: the docs-sync step (step 8) pushed the release
                # notes to the base branch, advancing origin/<base> past the
                # tag commit. On a resume the frozen base is the docs commit;
                # the tag commit is the canonical release commit — recover it
                # so the release resumes instead of deadlocking on the tag
                # check.
                LOGGER.info(
                    "issue=%s release_base_advanced_past_tag tag=%s "
                    "tag_commit=%s base_commit=%s",
                    number, tag, existing_tag_commit, release_commit,
                )
                release_commit = existing_tag_commit
            else:
                raise RuntimeError(
                    f"release tag {tag} already exists on the remote "
                    f"and points at {existing_tag_commit}, not the "
                    f"release commit {release_commit} — an existing "
                    "tag is never moved or overwritten"
                )
        else:
            ensure_release_tag_pushed(
                config.repo_dir, tag, release_commit,
            )
            LOGGER.info(
                "issue=%s release_tag_pushed tag=%s commit=%s",
                number, tag, release_commit,
            )
        release_url = publish_release(
            repo=source_repo, tag=tag, version=tag,
            release_commit=release_commit, changelog=changelog,
            scope_evidence=scope_evidence, gate_evidence=gate_evidence,
            test_evidence=test_evidence, run_id=run_id, issue_number=number,
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi released**: {release_url}",
            ),
        )
        docs_evidence = sync_release_docs(
            source_repo=source_repo, repo_dir=config.repo_dir,
            worktree=worktree, base_branch=base_branch, tag=tag,
            release_commit=release_commit, issue_number=number,
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release docs synced**: {docs_evidence}",
            ),
        )
        try:
            apply_label_patch(
                number, repo=source_repo, event=EVENT_MERGED,
                current_labels={IN_PROGRESS_LABEL},
            )
            close_issue(int(number), repo=source_repo)
        except Exception:
            # The tag and GitHub Release are already published at this
            # point. The ai-merged transition and the Issue close are
            # bookkeeping of that irreversible fact — a transient failure
            # here must not fall through to the generic handler and
            # rewrite the published result as ai-blocked (Issue #79:
            # bypass, never a terminal rewrite; same rule as the
            # milestone evidence below).
            LOGGER.exception(
                "issue=%s release_publish_closeout_failed", number,
            )
        try:
            milestone_evidence = close_release_milestone(
                source_repo, tag, run_id=run_id,
            )
        except Exception as exc:
            # The tag and GitHub Release are already published at this point.
            # Milestone closure is evidence only and must not rewrite that
            # irreversible release result as ai-blocked.
            LOGGER.exception(
                "issue=%s release_milestone_evidence_failed", number,
            )
            milestone_evidence = (
                "milestone evidence unavailable: " + str(exc)
            )
        try:
            comment_issue(
                number, repo=source_repo,
                body=release_success_comment_body(
                    run_id, run_info, release_url, tag, release_commit,
                    scope_evidence, gate_evidence, test_evidence,
                    docs_evidence, milestone_evidence,
                ),
            )
        except Exception:
            # Same bypass rule: the success comment is evidence of the
            # already-published release — its failure is logged and must
            # not rewrite the published result as ai-blocked.
            LOGGER.exception(
                "issue=%s release_success_comment_failed", number,
            )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        LOGGER.info(
            "issue=%s run_end release_success tag=%s url=%s "
            "elapsed=%.1fs", number, tag, release_url,
            time.monotonic() - started,
        )
        return release_url
    except ReleaseDeliveriesWaiting as waiting:
        # Issue #381: this is a clean, recoverable tick. Return the release
        # ticket to the ready queue before releasing the caller's slot.
        apply_label_patch(
            number, repo=source_repo, event=EVENT_RELEASE_WAITING,
            current_labels={IN_PROGRESS_LABEL},
        )
        detail = ", ".join(f"#{item}" for item in waiting.issue_numbers)
        comment_issue(
            number, repo=source_repo,
            body=(
                run_marker(run_id) + "\n"
                "Orbi release waiting for deliveries\n"
                f"open_deliveries: {detail}\n"
                f"waited: {int(waiting.waited)}s / "
                f"{int(waiting.limit)}s\n"
                f"wait_started: {time.time()}\n"
                f"run_id={run_id}"
            ),
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        LOGGER.info(
            "issue=%s release_waiting_deliveries_returned open=%s",
            number, waiting.issue_numbers,
        )
        return ""
    except Exception as exc:
        LOGGER.exception("issue=%s release_failed", number)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_BLOCKED,
            current_labels={IN_PROGRESS_LABEL},
        )
        comment_issue(
            number, repo=source_repo,
            body=release_failure_comment_body(run_id, run_info, str(exc)),
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        return ""


class ReleaseDeliveriesWaiting(RuntimeError):
    """Gate 1 found deliveries still in flight; retry next tick."""

    def __init__(self, issue_numbers: list[int], waited: float, limit: float):
        self.issue_numbers = issue_numbers
        self.waited = waited
        self.limit = limit
        super().__init__(
            "release gate: waiting for open deliveries "
            f"{issue_numbers} (waited {int(waited)}s / {int(limit)}s)"
        )


def release_target_milestone(
    issue: dict, active_milestone: str | None = None,
) -> str | None:
    """Return the Milestone a release Issue releases (Issue #663).

    The completeness gate judges the Milestone the release belongs to:
    the Issue's own GitHub Milestone is authoritative, and the configured
    `active_milestone` is the fallback (the release fallback scan is
    already scoped to it). Without any Milestone there is nothing to
    check and the release keeps the pre-#663 behavior, so the function
    returns None and the caller skips the gate.
    """
    milestone = issue.get("milestone")
    if isinstance(milestone, dict):
        title = milestone.get("title")
        if isinstance(title, str) and title:
            return title
    if isinstance(active_milestone, str) and active_milestone:
        return active_milestone
    return None


def reconcile_release_epics(repo: str, milestone_number: int, version: str,
                            run_id: str) -> list[str]:
    """Close only provably complete open Epics in this exact Milestone."""
    issues = milestone_open_issues(repo, milestone_number)
    evidence: list[str] = []
    for listed_epic in issues:
        labels = listed_epic.get("labels", [])
        names = {label.get("name") for label in labels if isinstance(label, dict)}
        if EPIC_LABEL not in names:
            continue
        number = listed_epic.get("number")
        try:
            child_evidence = _verify_epic_complete(repo, listed_epic)
        except (ValueError, json.JSONDecodeError) as exc:
            evidence.append(f"Epic #{number} kept open: {exc}")
            continue
        audit = _epic_audit(child_evidence, version)
        comments = issue_comments(int(number), repo=repo)
        if not any(audit in str(comment.get("body", "")) for comment in comments):
            comment_issue(int(number), repo=repo,
                         body=f"<!-- orbi:run={run_id} -->\n{audit}\nrun_id={run_id}")
        close_issue(int(number), repo=repo)
        evidence.append(f"Epic #{number} closed after verification ({'; '.join(child_evidence)})")
    return evidence
