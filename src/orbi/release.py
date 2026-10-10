"""Orbi release delivery subsystem.

The deterministic release state machine the Runner executes for an
`ai-release` Issue: declaration parsing, scope verification, gates,
version preparation, tag, GitHub Release publish, Milestone
close, and the `process_release` orchestration. Its GitHub and git data
access goes through the `orbi.github` / `orbi.gitops` leaves and the
`orbi.journal` seam; the runner-side entry is the dispatch
in `orbi.runner.process_issue`, and `runner` imports this module at
module level for the release constants.
"""
from __future__ import annotations

import fcntl
import functools
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from orbi import failure
from orbi.delivery_scene import RunContext
from orbi.delivery_labels import (
    EPIC_LABEL,
    EVENT_BLOCKED,
    EVENT_CLAIM,
    EVENT_MERGED,
    EVENT_RELEASE_WAITING,
    FIX_NEEDED_LABEL,
    IN_PROGRESS_LABEL,
    PR_OPENED_LABEL,
    READY_LABEL,
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
from orbi.release_git import (
    RELEASE_VERSION_FILE_OPTIONS,
    RELEASE_VERSION_TAG_RE,
    ReleaseVersionAlreadyLanded,
    prepare_release_version,
    run_git_write,
)
from orbi.release_notes import (
    RELEASE_CHANGELOG_CATEGORIES,
    build_release_changelog,
    previous_release_tag,
    release_changelog_category,
    release_changelog_scope,
    release_range_prs,
)
from orbi.journal import (
    LOGGER,
    event,
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
    # runner imports this module at runtime — while
    # `process_release` annotates the frozen host config of #790.
    from orbi.config import RunnerConfig


# --- release-domain constants, scene and gates (moved from
# --- `orbi.runner`: the release contract lives here) --------

# The optional machine-readable override section on a release Issue (Issue
# #905). When present, `- version:`, `- base_branch:` and `- scope:` (or
# `- scope_from_milestone:`) are parsed strictly. Without the section,
# GitHub's Milestone and repository configuration provide the contract. The
# declaration carries NO
# local test contract: test acceptance is the GitHub
# Actions CI result on the release commit (the #268 CI-wait gate).
# The Pi role of a release run: the delivery state
# machine executes it, never a Pi session.
ROLE_RELEASE = "release"

RELEASE_SECTION = "## Release"
# Release CI wait: the release commit is born from the last
# delivery PR merge, so its CI is almost always still running when the
# gate checks it — a pending check (queued/in_progress) is an
# intermediate state, not a failure. The gate waits for completion up to
# this limit and decides on the FINAL conclusions; a wait timeout is its
# own failure reason, never reported as a CI failure. The TOML field
# `release_ci_wait_seconds` overrides the default (the #228 pattern).
RELEASE_CI_WAIT_SECONDS = 1800
# Release delivery wait: an early release ticket yields the
# slot while other deliveries finish. The limit applies to one gate attempt;
# the next tick retries the same ready release ticket.
RELEASE_DELIVERIES_WAIT_SECONDS = 1800
# Poll cadence while waiting: one `release_waiting_ci` journal line plus
# one progress-comment PATCH per poll — the same 30s GitHub cadence as
# the live progress heartbeat (PI_HEARTBEAT_SECONDS).
RELEASE_CI_POLL_INTERVAL = 30.0


# Issue #831: the copy-paste guidance embedded in the declaration parse
# errors. The example is valid parser input (locked by test) and the
# `version_file` value range quoted in the errors is generated from
# RELEASE_VERSION_FILE_OPTIONS, so the error copy cannot drift from the
# parser's supported set.
RELEASE_DECLARATION_EXAMPLE = (
    f"{RELEASE_SECTION}\n"
    "\n"
    "- version: v1.2.0\n"
    "- base_branch: main\n"
    f"- version_file: {RELEASE_VERSION_FILE_OPTIONS[0]}\n"
    "- scope:\n"
    "  - #53\n"
    "  - #54"
)
_RELEASE_FIELD_EXAMPLES = {
    "version": "`- version: v1.2.0`",
    "base_branch": "`- base_branch: main`",
    "scope": "`- scope:` followed by `  - #53` item lines",
    "scope_from_milestone": "`- scope_from_milestone: v1.2.0`",
    "version_file": "`- version_file: pyproject.toml`",
}


def parse_release_declaration(body: str) -> dict:
    """Parse the optional `## Release` overrides of a release Issue body.

    Without the section this returns an empty override mapping; the release
    resolver obtains the contract from GitHub's Milestone and repository
    configuration. A present section remains the machine-readable contract
    (checkboxes are never parsed):

    ```markdown
    ## Release

    - version: v0.3.0
    - base_branch: main
    - scope:
      - #123
      - #124
    ```

    or, instead of the hand-listed `scope`, the scope derived from the
    Milestone whose title is the release version:

    ```markdown
    - scope_from_milestone: v0.3.0
    ```

    `version` is the exact tag name (no spaces) and `base_branch` the
    branch the release commit is frozen from. The declaration carries
    NO test contract: test acceptance is the GitHub
    Actions CI result on the release commit (the #268 CI-wait gate).
    `scope` lists the Issue/PR numbers verified one by one.
    Optional `version_file` selects a supported ecosystem metadata file
    (the default is `pyproject.toml`) or `none` to skip version metadata
    changes; its existence in the frozen release tree is verified at
    claim time by `verify_release_version_file`.
    Exactly one of `scope` / `scope_from_milestone` must be present:
    both (conflict) or neither fails fast. `scope_from_milestone` is
    the Milestone TITLE (no spaces); its scope is derived later by
    `derive_release_scope_from_milestone`. Every other deviation fails
    fast with the concrete field: missing section, missing or
    duplicated field, unknown key, empty value, empty scope or a
    malformed scope item. No guessing. Every failure message also
    carries the expected form (a copy-pasteable example for a missing
    section) so the user can repair the Issue body without reading the
    parser (Issue #831).
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
        # Issue #905: the GitHub Milestone is the default release contract.
        # An empty mapping is resolved after the frozen tree and repository
        # configuration are available; a present section remains strict for
        # compatibility with existing release tickets.
        return {}
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
                        "must be a positive Issue or PR number "
                        "(expected form: `  - #53`)"
                    )
                scope.append(number)
                continue
            if scope_open and ":" not in content:
                raise ValueError(
                    f"release declaration scope item {content!r} is "
                    "malformed (expected `  - #N` under the `- scope:` "
                    "field, e.g. `  - #53`)"
                )
            # A `- key: value` line closes the scope list (and a
            # duplicated or unknown key is caught below).
            scope_open = False
            key, sep, value = content.partition(":")
            if not sep:
                raise ValueError(
                    f"release declaration field {key.strip()!r} is "
                    "malformed (expected form: `- key: value`, e.g. "
                    "`- version: v1.2.0`)"
                )
            key = key.strip()
            value = value.strip()
            if key in fields:
                raise ValueError(
                    f"release declaration field {key!r} is duplicated "
                    "(keep exactly one; expected form: "
                    f"{_RELEASE_FIELD_EXAMPLES[key]})"
                )
            if key == "scope":
                if value:
                    raise ValueError(
                        "release declaration `scope` must be a list of "
                        "`  - #N` items, not an inline value (expected "
                        f"form: {_RELEASE_FIELD_EXAMPLES['scope']})"
                    )
                scope_open = True
                fields["scope"] = ""
            elif key in ("version", "base_branch",
                         "scope_from_milestone", "version_file"):
                fields[key] = value
            else:
                raise ValueError(
                    f"release declaration has the unknown field {key!r} "
                    "(expected version, base_branch, scope, "
                    "scope_from_milestone or version_file; expected "
                    "form: `- key: value`, e.g. `- version: v1.2.0`)"
                )
        elif scope_open:
            raise ValueError(
                f"release declaration scope item {stripped!r} is "
                "malformed (expected `  - #N` under the `- scope:` "
                "field, e.g. `  - #53`)"
            )
        else:
            raise ValueError(
                f"release declaration line {stripped!r} is not a "
                "`- key: value` field or a scope item (expected form: "
                "`- key: value`, e.g. `- version: v1.2.0`)"
            )
    for key in ("version", "base_branch"):
        if key not in fields:
            raise ValueError(
                f"release declaration is missing the `{key}` field "
                "(expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
        if not fields[key]:
            raise ValueError(
                f"release declaration field `{key}` is empty "
                "(expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
    if "scope" in fields and "scope_from_milestone" in fields:
        raise ValueError(
            "release declaration must use exactly one of `scope` or "
            "`scope_from_milestone`, not both (keep one; expected "
            f"form: {_RELEASE_FIELD_EXAMPLES['scope']} OR "
            f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
        )
    if "scope" not in fields and "scope_from_milestone" not in fields:
        raise ValueError(
            "release declaration is missing the `scope` field or the "
            "`scope_from_milestone` field (exactly one of the two; "
            f"expected form: {_RELEASE_FIELD_EXAMPLES['scope']} OR "
            f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
        )
    if "scope" in fields and not scope:
        raise ValueError(
            "release declaration `scope` must list at least one "
            "`  - #N` Issue or PR number (expected form: "
            f"{_RELEASE_FIELD_EXAMPLES['scope']})"
        )
    for key in ("version", "base_branch"):
        if any(ch.isspace() for ch in fields[key]):
            raise ValueError(
                f"release declaration field `{key}` must not contain "
                "spaces (one token; expected form: "
                f"{_RELEASE_FIELD_EXAMPLES[key]})"
            )
    if "scope_from_milestone" in fields:
        if not fields["scope_from_milestone"]:
            raise ValueError(
                "release declaration field `scope_from_milestone` is "
                "empty (expected form: "
                f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
            )
        if any(ch.isspace() for ch in fields["scope_from_milestone"]):
            raise ValueError(
                "release declaration field `scope_from_milestone` must "
                "not contain spaces (the Milestone TITLE is one token; "
                "expected form: "
                f"{_RELEASE_FIELD_EXAMPLES['scope_from_milestone']})"
            )
    version_file = fields.get("version_file", "pyproject.toml")
    if version_file not in RELEASE_VERSION_FILE_OPTIONS:
        raise ValueError(
            "release declaration field `version_file` is not supported "
            "(supported values: "
            f"{', '.join(RELEASE_VERSION_FILE_OPTIONS)}; expected "
            f"form: {_RELEASE_FIELD_EXAMPLES['version_file']})"
        )
    return {
        "version": fields["version"],
        "base_branch": fields["base_branch"],
        "scope": scope,
        "scope_from_milestone": fields.get("scope_from_milestone"),
        "version_file": version_file,
    }


def resolve_release_declaration(
        issue: dict, overrides: dict, config: RunnerConfig,
        source_repo: str, repo_dir: Path, release_commit: str) -> dict:
    """Build the release contract from GitHub facts and optional overrides.

    The Issue Milestone is the only derived source for the release version and
    scope.  Repository configuration supplies the base branch and the frozen
    tree supplies the version metadata file.  A legacy complete ``## Release``
    declaration still wins field-by-field, but its version must agree with the
    Milestone so a renamed Milestone can never be guessed around.
    """
    milestone = issue.get("milestone")
    milestone_title = milestone.get("title") if isinstance(milestone, dict) else None
    if not isinstance(milestone_title, str) or not milestone_title:
        milestone_title = None
    version = overrides.get("version", milestone_title)
    if version is None:
        raise ValueError(
            "release Issue must have a Milestone; set the Milestone title "
            "to the release version (for example `v0.5.8`)"
        )
    if milestone_title is not None and version != milestone_title:
        raise ValueError(
            f"release version {version!r} does not match the Issue Milestone "
            f"title {milestone_title!r}; rename the Milestone or correct "
            "the `- version:` override"
        )
    if RELEASE_VERSION_TAG_RE.fullmatch(version) is None:
        raise ValueError(
            f"release version {version!r} must be a v-prefixed numeric tag "
            "(for example `v0.5.8`); rename the Milestone or correct the "
            "`- version:` override"
        )

    # config.base_branch is already the fused value: the [[repositories]]
    # entry fallback is applied unconditionally at dispatch, then the
    # repository policy overrides it. Re-reading the raw entry here used
    # to discard the policy layer and freeze the release on the wrong
    # branch.
    base_branch = config.base_branch
    version_file = overrides.get("version_file")
    if version_file is None:
        root_entries = set(run_command(
            ["git", "ls-tree", "--name-only", release_commit], cwd=repo_dir,
        ).splitlines())
        version_file = next(
            (candidate for candidate in RELEASE_VERSION_FILE_OPTIONS
             if candidate != "none" and candidate in root_entries),
            None,
        )
        if version_file is None:
            raise ValueError(
                "release version file could not be detected in the frozen "
                "repository tree; add `- version_file: <supported file>` "
                "or `- version_file: none` to the optional `## Release` "
                "section"
            )

    scope_from_milestone = overrides.get(
        "scope_from_milestone", milestone_title,
    )
    if overrides.get("scope"):
        scope = overrides["scope"]
        scope_from_milestone = None
    else:
        scope = []
        if scope_from_milestone is None:
            raise ValueError(
                "release Issue must have a Milestone or an explicit `scope`"
            )
    return {
        "version": version,
        "base_branch": overrides.get("base_branch", base_branch),
        "scope": scope,
        "scope_from_milestone": scope_from_milestone,
        "version_file": version_file,
    }


def verify_release_version_file(repo_dir: Path, release_commit: str,
                                version_file: str) -> None:
    """Prove the declared `version_file` exists in the frozen base.

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
    """Verify every release scope item ONE BY ONE.

    The scope is verified against GitHub, never by parsing Issue-body
    checkboxes: each number is probed as a PR first (`gh pr view` —
    which exits 1 with the real "Could not resolve to a PullRequest"
    error when the number is an Issue, verified against the live CLI)
    and, failing that, as an Issue (`gh issue view`). A PR must be
    `MERGED` and its merge commit must be an ancestor of the frozen
    release commit; an Issue must be `CLOSED` with `stateReason`
    `COMPLETED`: a `NOT_PLANNED` closure (duplicate /
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
            # A NOT_PLANNED closure (duplicate / won't fix)
            # is not released work — annotate the exclusion visibly
            # instead of silently counting the ticket into the release.
            evidence.append(
                f"Issue #{number} closed (not planned, excluded)"
            )
        else:
            evidence.append(f"Issue #{number} closed")
    return evidence


def derive_release_scope_from_milestone(
        repo: str, milestone_title: str,
        release_issue: int | None = None) -> tuple[list[int], list[str]]:
    """Derive the release scope from a Milestone.

    The release scope is the Milestone's COMPLETED Issues under the
    Milestone whose title is EXACTLY `milestone_title` (the same
    exact-title rule as `close_release_milestone` — never guessed,
    never fuzzy-matched, never a different Milestone). The Milestone
    decides whether a release may run (gates and scope evidence), not
    what its notes say: the Changelog is derived from the tagged commit
    range by `orbi.release_notes` (Issue #1492), so a PR merged after
    the tag never reaches the notes and a PR in the tag that is not on
    the Milestone still does.

    - no Milestone with that exact title -> fail fast;
    - several Milestones with that exact title -> fail fast
      (ambiguous — GitHub allows duplicate titles, so guessing one is
      forbidden);
    - open Issues are NEVER part of the scope (unfinished work is a
      human decision point) but are returned as a separate evidence list
      so the release run surfaces them instead of swallowing them;
    - `release_issue` (the driving release Issue's own number) is
      exempt from that open-item evidence: it is necessarily still
      open while the release runs (it closes after the notes are
      published), so listing it would put a self-referential "NOT
      released" line on an immutable Release page — the same exemption
      the #663 completeness gate applies.

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
        if item.get("number") != release_issue
    ]
    return scope, open_evidence




def check_release_gates(repo: str, release_commit: str,
                        release_number: int, *,
                        milestone: str | None = None,
                        ci_wait_seconds: float = RELEASE_CI_WAIT_SECONDS,
                        delivery_wait_seconds: float = RELEASE_DELIVERIES_WAIT_SECONDS,
                        delivery_waited_seconds: float = 0.0,
                        on_wait: Callable[[str], None] | None = None,
                        on_delivery_wait: Callable[[str], None] | None = None,
                        repo_has_ci: bool = False,
                        ) -> tuple[list[str], bool]:
    """Enforce the pre-release gates and return their evidence.

    Returns ``(evidence, repo_has_ci)`` — the second element is whether
    any check run was observed on the gated commit, so the release flow
    can feed gate 1's observation (on the frozen base) into gate 2 (on
    the just-pushed version commit) as ``repo_has_ci``.

    Two gates, each checked against GitHub (never against local
    state), each failure raising with the concrete offender:

    1. No open Issue still carries `ai-in-progress`, `ai-pr-opened`
       or `ai-fix-needed` — the release Issue itself is excluded (it
       carries `ai-in-progress` while the release runs). With a
       `milestone`, the check is scoped to that Milestone (the documented `gh issue list --milestone <title>` filter), the
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
       CheckRuns seconds after the push): the gate then
       polls within the same budget until the first check appears and
       never passes an empty list as "nothing to gate"). A PENDING
       check (queued/in_progress —
       the release commit is born from the last delivery merge, so its
       CI is almost always still running) is not a
       conclusion: the gate polls until every check completes (one
       `release_waiting_ci` journal line per poll, the wait reflected
       through `on_wait`), then decides on the final conclusions.
       Waiting past `ci_wait_seconds` fails with its own timeout
       reason, explicitly distinct from a CI failure.

    Open PRs are deliberately NOT a gate: an open PR is queue state, never a release
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
        # The leftover-delivery check is scoped to the
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
        event(
            "release_waiting_deliveries", issue=release_number,
            open=numbers, waited=f"{int(delivery_waited_seconds)}s",
            limit=f"{int(delivery_wait_seconds)}s",
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
            # An empty list on a just-pushed commit in a CI
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
        event(
            "release_waiting_ci", issue=release_number, commit=release_commit,
            pending=detail, waited=f"{int(waited)}s",
            limit=f"{int(ci_wait_seconds)}s",
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
    # No open-PR gate — an open PR is queue state, not a
    # release premise (see the docstring for the maintainer ruling).
    return evidence, bool(check_runs)


def release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return the commit the tag points to on the remote, or None.

     `git ls-remote` keeps the existence probe read-only and
    its exit semantics unambiguous: exit 0 with empty output means the
    remote has no such tag (None); ANY non-zero exit is a real failure
    (network/auth) and propagates. The previous `git fetch` probe read
    exit 128 as "missing", but a network failure also exits 128 — a
    transient outage would read as "no tag on the remote", the retry
    would re-create the tag, and a local residue from the failed push
    deadlocked the release ticket. Annotated tags are
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


def local_release_tag_commit(repo_dir: Path, tag: str) -> str | None:
    """Return a local tag's peeled commit, or None when it is absent."""
    try:
        return run_command(
            ["git", "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}^{{commit}}"], cwd=repo_dir,
        ).strip()
    except subprocess.CalledProcessError:
        return None


def ensure_release_tag_created(repo_dir: Path, tag: str,
                               release_commit: str) -> None:
    """Create the annotated release tag locally, idempotently."""
    local_tag_commit = local_release_tag_commit(repo_dir, tag)
    if local_tag_commit is None:
        run_git_write(
            ["git", "tag", "-a", tag, "-m", f"Release {tag}",
             release_commit], cwd=repo_dir,
        )
    elif local_tag_commit != release_commit:
        raise RuntimeError(
            f"local tag {tag} already exists and points at "
            f"{local_tag_commit}, not the release commit {release_commit} "
            "— an existing tag is never moved or overwritten"
        )


def ensure_release_tag_pushed(repo_dir: Path, tag: str,
                              release_commit: str) -> None:
    """Create the annotated release tag and push it — idempotently."""
    ensure_release_tag_created(repo_dir, tag, release_commit)
    run_git_network_command(
        ["git", "push", "origin", f"refs/tags/{tag}"], cwd=repo_dir,
    )


def tag_commit_is_ancestor_of_base(tag_commit: str, base_commit: str,
                                   repo_dir: Path) -> bool:
    """True when the tag commit is an ancestor of the base commit — the
    resume case where an existing tag is behind an advanced base."""
    return _is_ancestor(tag_commit, base_commit, cwd=repo_dir)


def publish_release(*, repo: str, tag: str, version: str,
                    release_commit: str, changelog: str,
                    scope_evidence: list[str], gate_evidence: list[str], test_evidence: str,
                    run_id: str, issue_number: int,
                    attribution_footer: bool = True,
                    attribution_link: str = "https://github.com/orbi-build/orbi",
                    ) -> str:
    """Create the GitHub Release for the tag — idempotently.

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
        *(["", f"Released by [Orbi]({attribution_link})"]
          if attribution_footer else []),
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


# The GitHub issue index is eventually consistent —
# `gh issue close` returning success does not mean the
# `issues?milestone=N&state=open` list reflects it yet, and v0.4.10 read
# a stale non-empty list in the same second as the close. A non-empty
# gate read is re-checked with these backoff delays first; only a list
# that stays non-empty across all of them fails fast (bounded ≈ 10 s
# including the reads — a real unfinished issue must not drag the
# release out).
MILESTONE_OPEN_RETRY_DELAYS = (1.0, 2.0, 4.0)


def close_release_milestone(repo: str, version: str, *, run_id: str | None = None,
                            release_issue: int | None = None) -> str:
    """Close the Milestone whose title is exactly `version`.

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
      bounded backoff re-reads (the issue index is
      eventually consistent, so a list read in the same second as the
      release Issue's `gh issue close` can still show it as open);
    - `open` with 0 open issues -> closed via the official REST
      contract `PATCH /repos/{owner}/{repo}/milestones/{number}`
      with `state=closed` (OpenAPI `issues/update-milestone`).

    `release_issue` is THIS release's own ticket number.
    The gate never counts it as unfinished work: it is being closed by
    this very release, so a stale-open listing of exactly that ticket
    is the known index lag, and waiting out the backoff for
    it would make the milestone close depend on the index refresh
    timing. Every gate read drops it before the 0-open-issues
    judgment; a refusal then names only the REAL leftovers and
    annotates the exclusion. Without `release_issue` the gate is
    unchanged.

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
    excluded_tickets: list[int] = []

    def open_leftovers() -> list[dict]:
        # The gate judges the milestone's REAL unfinished
        # work — never this release's own ticket.
        issues = milestone_open_issues(repo, int(number))
        if release_issue is None:
            return issues
        leftovers = [
            item for item in issues
            if item.get("number") != release_issue
        ]
        if len(leftovers) != len(issues):
            excluded_tickets.append(release_issue)
        return leftovers

    open_issues = open_leftovers()
    epic_evidence: list[str] = []
    if run_id is not None and open_issues:
        # Every listed Epic is verified from its children and blockers before
        # the authoritative exact-Milestone list is checked again.
        epic_evidence = reconcile_release_epics(repo, int(number), version, run_id)
        open_issues = open_leftovers()
    retries = 0
    while open_issues and retries < len(MILESTONE_OPEN_RETRY_DELAYS):
        # The release Issue close succeeded seconds ago; a non-empty read
        # here is more likely the index lagging than real
        # unfinished work. Re-read with bounded backoff; the raise below
        # fires only once the list stays non-empty across all retries.
        time.sleep(MILESTONE_OPEN_RETRY_DELAYS[retries])
        retries += 1
        open_issues = open_leftovers()
    if open_issues:
        listing = ", ".join(
            f"#{i.get('number')} {i.get('title')}" for i in open_issues
        )
        excluded = (
            f" (this release's own ticket #{release_issue} is excluded "
            "from this gate — it is being closed by this release)"
            if release_issue is not None else ""
        )
        raise RuntimeError(
            f"release {version}: Milestone #{number} ({html_url}) still "
            f"has {len(open_issues)} open issue(s) — closing it would hide "
            f"unfinished work; open issues: {listing}{excluded}"
        )
    close_milestone(repo, int(number))
    epic_suffix = f"; {'; '.join(epic_evidence)}" if epic_evidence else ""
    excluded_suffix = (
        f"; release ticket #{release_issue} excluded — it is being "
        "closed by this release"
        if excluded_tickets else ""
    )
    return (
        f"Milestone #{number} ({html_url}) closed after release "
        f"{version} (0 open issues){epic_suffix}{excluded_suffix}"
    )


def release_success_comment_body(run_id: str, run_info: str,
                                 release_url: str, tag: str,
                                 release_commit: str,
                                 scope_evidence: list[str],
                                 gate_evidence: list[str],
                                 test_evidence: str,
                                 milestone_evidence: str) -> str:
    """The terminal success comment: the full verification evidence.

     The comment is the auditable record of the release:
    run marker, release URL, version/tag/release commit, the
    per-item scope evidence, the gate evidence, the test evidence and
    the Milestone evidence (the Milestone whose title is the released
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
        "## Milestone",
        "",
        f"- {milestone_evidence}",
        "",
        f"run_id={run_id}",
    ])


def release_failure_comment_body(run_id: str, run_info: str,
                                 error: BaseException) -> str:
    """The terminal failure comment: the blocked scene.

    A release failure is terminal (`ai-blocked` ALONE, no automatic
    retry): the comment carries the run marker, the same
    machine-readable `orbi:failure:v1` block the delivery path emits
    (`failure.classify`, Issue #1323), and the concrete reason.
    """
    detail = failure._failure_detail(error)
    body = field_block(run_id, "Orbi release failed (ai-blocked)",
                       {**_run_info_fields(run_info), "failure": detail})
    return body.replace("\n", f"\n{failure.render(failure.classify(detail))}\n", 1)


def process_release(issue: dict, config: RunnerConfig,
                   source_repo: str) -> str:
    """Run the deterministic release state machine for one release Issue.

     A release task NEVER enters the normal `run_pi`
    development path: the Runner executes the state machine itself,
    step by step, each step idempotent so a restart resumes the same
    run (same run id, same worktree) from the top:

    1. Parse the optional `## Release` overrides from the Issue body.
       Without that section, the Issue Milestone supplies version and scope,
       while repository configuration supplies the base branch.
    2. Freeze the configured base — the release commit is exactly
       `origin/<base_branch>` (fetched under the base-sync lock).
       A declared version, when present, must match the Milestone title.
    2b. Prove the declared (or defaulted) `version_file` exists at the
       root of the frozen release tree (`verify_release_version_file`) — before any gate wait, so a declaration/repo
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
       failure path. No local test execution.
    6. Tag: the remote tag must not exist or must point EXACTLY at
       the release commit (a mismatch fails — an existing tag is
       never moved; one exception: a tag pointing at an ancestor of
       the frozen base resumes the release by rewinding the release
       commit to the tag); otherwise create an annotated tag at the
       release commit and push it with a plain push (never
       `--force`).
    7. Publish the GitHub Release (idempotent) with the full
       verification evidence.
    8. Apply `ai-merged` and close the release Issue (terminal delivery
       transition).
    9. Close the Milestone whose title is EXACTLY the released
        version, then write the success comment (release
        URL, tag, commit, evidence, Milestone evidence). Exact title
        match only; close it only when it has 0 open Issues;
        already-closed is idempotent. A missing Milestone or remaining
        open Issues fails fast. Any failure is `ai-blocked` ALONE, with
        a concrete failure comment, and the handled failure returns
        cleanly so the tick does not crash.
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
            event(
                "release_resuming_run", issue=number, run_id=run_id,
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

    ctx = RunContext(
        run_id=run_id, issue=number, branch=branch, worktree=worktree,
        source_repo=source_repo,
    )

    def progress() -> dict:
        return _progress_state(
            ctx, title=title, role=ROLE_RELEASE, started=started,
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
        # The CI wait is reflected in the live progress
        # comment (pure bypass); the gate itself emits the
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
        # Fused value: entry fallback + policy override (see
        # resolve_release_declaration — never re-read the raw entry).
        base_branch = declaration.get("base_branch") or config.base_branch
        # The started milestone below and the failure comment
        # read THIS value — base_branch known, base_sha not yet (the
        # post-gate reassignment further down adds base_sha). The journal
        # refactor deleted the assignment and both comments lost the only
        # field naming the frozen branch.
        run_info = f"base_branch={base_branch} run_id={run_id} priority={priority}"
        event("release_task", run_info, issue=number)
        apply_label_patch(
            number, repo=source_repo, event=EVENT_CLAIM,
            current_labels={label.get("name") for label in issue.get(
                "labels", []) if isinstance(label, dict)
                and isinstance(label.get("name"), str)},
        )
        set_active_run(ctx, title)
        publish(
            action=lambda: publisher.ensure(progress_body(progress())),
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi release started**: {run_info}",
            ),
        )
        release_commit = freeze_base(config.repo_dir, base_branch)
        declaration = resolve_release_declaration(
            issue, declaration, config, source_repo, config.repo_dir,
            release_commit,
        )
        base_branch = declaration["base_branch"]
        run_info = f"base_branch={base_branch} base_sha={release_commit} run_id={run_id} priority={priority}"
        # The declared (or defaulted) version_file is proven
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
            # scenes).
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
        # The leftover-delivery gate is scoped to the same
        # Milestone as the #663 completeness gate — the release Issue's own
        # GitHub Milestone, `active_milestone` fallback.
        target_milestone = release_target_milestone(
            issue, config.active_milestone,
        )
        gate_evidence, repo_has_ci = check_release_gates(
            source_repo, release_commit, number,
            milestone=target_milestone,
            # The gate waits out pending CI checks on the
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
            # The scope is derived from the Milestone, then
            # verified item by item exactly like a hand-listed scope.
            # The release Issue itself is exempt from the open-item
            # evidence (Issue #818): it is necessarily still open
            # here and closes only after the notes are published.
            derived_scope, open_milestone_evidence = (
                derive_release_scope_from_milestone(
                    source_repo, declaration["scope_from_milestone"],
                    release_issue=number,
                )
            )
            # An empty derived scope is allowed — a milestone with no
            # deliveries must not block the release (Issue #1384).
            declaration["scope"] = derived_scope
            if open_milestone_evidence:
                event(
                    "release_milestone_open_items", level=logging.WARNING,
                    issue=number, milestone=declaration["scope_from_milestone"],
                    open_items="; ".join(open_milestone_evidence),
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
                repo_dir=config.repo_dir,
            )
        else:
            release_commit = prepare_release_version(
                worktree, declaration["version"], base_branch,
                declaration["version_file"],
                repo_dir=config.repo_dir,
            )
        # Version preparation creates the commit that will be tagged. Re-run
        # the commit-specific gates so the recorded CI result and final
        # no-open-PR check cover that exact release commit, not the frozen
        # pre-version source commit. The just-pushed commit may hit the
        # CheckRun registration lag, so gate 1's observation (checks on the
        # frozen base = this repository runs CI) forbids the empty-list
        # pass here.
        gate_evidence, _ = check_release_gates(
            source_repo, release_commit, number,
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
        # There is NO local test execution — the CI-wait gate
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
                event(
                    "release_tag_exists", issue=number, tag=tag,
                    commit=existing_tag_commit,
                )
            elif tag_commit_is_ancestor_of_base(
                    existing_tag_commit, release_commit,
                    config.repo_dir):
                # A previous attempt pushed the tag and the frozen base
                # advanced past it afterwards. The tag commit is the
                # canonical release commit — recover it so the release
                # resumes instead of deadlocking on the tag check.
                event(
                    "release_base_advanced_past_tag", issue=number, tag=tag,
                    tag_commit=existing_tag_commit, base_commit=release_commit,
                )
                release_commit = existing_tag_commit
            else:
                raise RuntimeError(
                    f"release tag {tag} already exists on the remote "
                    f"and points at {existing_tag_commit}, not the "
                    f"release commit {release_commit} — an existing "
                    "tag is never moved or overwritten"
                )
        # Release notes come from the tagged commit range, never the
        # Milestone Issue list (Issue #1492). Compute them only now that
        # the release commit is final — after the tag-conflict handling
        # above — so a resume for the same version reproduces the same
        # notes byte for byte.
        range_prs = release_range_prs(
            config.repo_dir, source_repo, release_commit, base_branch,
        )
        changelog = build_release_changelog(
            source_repo,
            release_changelog_scope(
                source_repo, range_prs, release_issue=number,
            ),
            range_prs,
        )
        # Publish the tag with a plain push (never `--force`); the annotated
        # object is created locally by `ensure_release_tag_pushed`.
        if existing_tag_commit is None:
            ensure_release_tag_pushed(
                config.repo_dir, tag, release_commit,
            )
            event(
                "release_tag_pushed", issue=number, tag=tag,
                commit=release_commit,
            )
        release_url = publish_release(
            repo=source_repo, tag=tag, version=tag,
            release_commit=release_commit, changelog=changelog,
            scope_evidence=scope_evidence, gate_evidence=gate_evidence,
            test_evidence=test_evidence, run_id=run_id, issue_number=number,
            attribution_footer=config.attribution_footer,
            attribution_link=config.attribution_link,
        )
        publish(
            action=lambda: publisher.milestone(
                f"**Orbi released**: {release_url}",
            ),
        )
        try:
            # A release ticket is claimed through `label:ai-ready` and the
            # claim keeps the ready queue entry, so the real labels at
            # this point are `{ai-ready, ai-in-progress}`. The merged
            # event clears both, so the released Issue ends
            # `ai-merged` ALONE — never `ai-merged` plus a stale
            # `ai-ready` that reads as "still queued" (Issue #1526).
            apply_label_patch(
                number, repo=source_repo, event=EVENT_MERGED,
                current_labels={READY_LABEL, IN_PROGRESS_LABEL},
            )
            close_issue(int(number), repo=source_repo)
        except Exception:
            # The tag and GitHub Release are already published at this
            # point. The ai-merged transition and the Issue close are
            # bookkeeping of that irreversible fact — a transient failure
            # here must not fall through to the generic handler and
            # rewrite the published result as ai-blocked (a
            # bypass — never a terminal rewrite; same rule as the
            # milestone evidence below).
            LOGGER.exception(
                "issue=%s release_publish_closeout_failed", number,
            )
        try:
            milestone_evidence = close_release_milestone(
                source_repo, tag, run_id=run_id, release_issue=int(number),
            )
        except Exception as exc:
            # The tag and GitHub Release are already published at this point.
            # Milestone closure is evidence only and must not rewrite that
            # irreversible release result as ai-blocked. The
            # wording states plainly that the third release criterion was
            # missed — the milestone was NOT closed.
            LOGGER.exception(
                "issue=%s release_milestone_evidence_failed", number,
            )
            milestone_evidence = (
                "milestone NOT closed: " + str(exc)
            )
        try:
            comment_issue(
                number, repo=source_repo,
                body=release_success_comment_body(
                    run_id, run_info, release_url, tag, release_commit,
                    scope_evidence, gate_evidence, test_evidence,
                    milestone_evidence,
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
        event(
            "run_end", issue=number, result="release_success", tag=tag,
            url=release_url,
            elapsed=f"{time.monotonic() - started:.1f}s",
        )
        return release_url
    except ReleaseVersionAlreadyLanded as landed:
        # The version commit is already on the base branch: a concurrent
        # release instance won the push race (Issue #1289). This run yields
        # — the ticket returns to the ready queue and the next tick resumes
        # the release from the landed commit (tag/release) instead of
        # rewriting work that is already done as `ai-blocked`.
        event(
            "claim_yield", issue=number,
            reason="release_version_already_landed",
            base_branch=landed.base_branch,
            remote_commit=landed.remote_commit,
        )
        apply_label_patch(
            number, repo=source_repo, event=EVENT_RELEASE_WAITING,
            current_labels={IN_PROGRESS_LABEL},
        )
        comment_issue(
            number, repo=source_repo,
            body=(
                run_marker(run_id) + "\n"
                "Orbi release yielded: a concurrent release instance "
                "already pushed the same version commit\n"
                f"tag: {landed.tag}\n"
                f"base_branch: {landed.base_branch}\n"
                f"remote_commit: {landed.remote_commit}\n"
                f"run_id={run_id}"
            ),
        )
        publish(
            action=lambda: publisher.finish(progress_body(progress())),
        )
        return ""
    except ReleaseDeliveriesWaiting as waiting:
        # This is a clean, recoverable tick. Return the release
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
        event(
            "release_waiting_deliveries_returned", issue=number,
            open=waiting.issue_numbers,
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
            body=release_failure_comment_body(run_id, run_info, exc),
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
    """Return the Milestone a release Issue releases.

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
