"""GitHub read/write data access at the `gh` command line.

One function per GitHub read/write, with the `gh` argument assembly, the
`--json` field sets, the JSON shape validation, and the read-only retry
of `run_gh_read_command` all in this one leaf (Issue #785) — no call site
assembles a `gh` command for the shared contracts anymore, and the
extracted modules consume GitHub through these typed functions instead of
importing `runner`.

The only seam is `orbi.journal.run_command` (Article 3.4). The argv each
function emits is byte-identical to the call site it replaces — the
command line is the contract (Article 5.2), including the two distinct
milestone issue-list orders the sweep and the release scope derivation
pin separately.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from orbi.delivery_labels import (
    IN_PROGRESS_LABEL,
    P0_LABEL,
    label_patch,
)
from orbi.journal import LOGGER, event, run_command, single_line
from orbi.progress import (
    RUN_MARKER_PATTERN,
    format_status_comment,
    run_marker,
)

GH_READ_MAX_ATTEMPTS = 3
GH_READ_BACKOFF_SECONDS = 1
# gh prints its own HTTP failures as `HTTP <code>: <text> (<url>)` — the
# transient classes of Issue #738 are the 401 keyring race, the rate
# limits (429 / the API's "rate limit" messages) and GitHub-side 5xx.
GH_TRANSIENT_ERROR_RE = re.compile(
    r"http 401|http 429|http 5\d\d|bad credentials|rate limit",
    re.IGNORECASE,
)
# The read-only gh subcommand surface (the maintainer comment's
# enumerated read table). A verb missing from the set simply gets no
# retry — today's behavior — while a write verb can never classify as
# retryable, so a retried write can never duplicate a side effect.
GH_READ_SUBCOMMANDS = {
    ("issue", "list"), ("issue", "view"),
    ("pr", "list"), ("pr", "view"),
    ("release", "view"),
    ("repo", "view"),
    ("label", "list"),
    ("auth", "status"), ("auth", "token"),
}
RESUME_PR_STATE_TIMEOUT_SECONDS = 30


def _is_readonly_gh_command(command: list[str]) -> bool:
    """Return whether a gh command is provably read-only.

    `gh api` is a read only when it carries no non-GET `--method`/`-X`
    override and no request parameter: per gh's own semantics the
    default method is GET normally and POST if any `-f`/`-F` parameter
    was added, so only an explicit `--method GET` keeps parameters on
    the query string — the flags are gh's read/write semantics, not a
    call-site list. Every other subcommand is a read only when its verb
    is in the fixed read set.
    """
    if command[:1] != ["gh"] or len(command) < 3:
        return False
    if command[1] == "api":
        args = command[2:]
        method_get = False
        has_parameter = False
        for index, argument in enumerate(args):
            if argument in {"-X", "--method"} and index + 1 < len(args):
                if args[index + 1].upper() != "GET":
                    return False
                method_get = True
            elif argument.startswith("--method="):
                if argument.split("=", 1)[1].upper() != "GET":
                    return False
                method_get = True
            elif (
                argument in {"-f", "-F", "--raw-field", "--field"}
                or argument.startswith(("--raw-field=", "--field="))
                or (argument.startswith(("-f", "-F")) and len(argument) > 2)
            ):
                has_parameter = True
        return method_get or not has_parameter
    return (command[1], command[2]) in GH_READ_SUBCOMMANDS


def run_gh_read_command(
    command: list[str], *, cwd: Path | None = None,
    timeout: int | None = None,
    command_runner: Callable[..., str] | None = None,
) -> str:
    """Run one read-only gh command with bounded transient-failure retries.

    Issue #738: a keyring race (HTTP 401), a rate limit (429) or a
    GitHub-side 5xx used to crash a whole tick that was only reading.
    Only provably read-only commands (`_is_readonly_gh_command`) that
    failed with a transient error are retried, so no write path can ever
    reach the retry loop; every retry logs a structured `gh_read_retry`
    line, and the last error is re-raised unchanged so the existing
    failure handling keeps its stderr.
    """
    execute = command_runner or run_command
    attempt = 0
    while True:
        attempt += 1
        try:
            # Forward only the set options: None is run_command's own
            # default, and passing it explicitly would change the call
            # observed by the run_command fakes and probes.
            if timeout is None:
                if cwd is None:
                    return execute(command)
                return execute(command, cwd=cwd)
            if cwd is None:
                return execute(command, timeout=timeout)
            return execute(command, cwd=cwd, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            retryable = (
                _is_readonly_gh_command(command)
                and GH_TRANSIENT_ERROR_RE.search(detail) is not None
            )
            if attempt >= GH_READ_MAX_ATTEMPTS or not retryable:
                raise
        delay = GH_READ_BACKOFF_SECONDS * (2 ** (attempt - 1))
        event(
            "gh_read_retry", level=logging.WARNING,
            command=single_line(" ".join(command)), attempt=attempt + 1,
            max_attempts=GH_READ_MAX_ATTEMPTS, delay_seconds=delay,
            stderr=single_line(detail),
        )
        time.sleep(delay)


def parse_issue_array(raw: str) -> list[dict]:
    """Return the issue array from gh's JSON output."""
    issues = json.loads(raw)
    if not isinstance(issues, list):
        raise ValueError("issue list must be a JSON array")
    return issues


def parse_issue_list(raw: str) -> dict | None:
    """Return the first issue from gh's JSON array, or None when idle."""
    issues = parse_issue_array(raw)
    return issues[0] if issues else None


def parse_paginated_issue_array(raw: str) -> list[dict]:
    """Flatten the JSON array emitted by ``gh api --paginate --slurp``."""
    pages = json.loads(raw)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("paginated issue list must be an array of arrays")
    if any(not isinstance(item, dict) for page in pages for item in page):
        raise ValueError("paginated issue list contains a non-object item")
    return [item for page in pages for item in page]


def list_issues(repo: str, *, state: str | None = None,
                label: str | None = None, milestone: str | None = None,
                search: str | None = None, json_fields: str, limit: int,
                timeout: int | None = None) -> list[dict]:
    """Run one ``gh issue list`` query and return the parsed JSON array.

    Issue #299: every ``gh issue list`` call site shares this single
    command builder. The flag order mirrors the call sites it replaces
    (``--label``/``--state``/``--search`` before ``--json``/``--limit``,
    with ``--milestone`` appended last, exactly as the release gate did),
    so the emitted ``gh`` command is byte-for-byte unchanged.
    """
    command = ["gh", "issue", "list", "--repo", repo]
    if label is not None:
        command += ["--label", label]
    if state is not None:
        command += ["--state", state]
    if search is not None:
        command += ["--search", search]
    command += ["--json", json_fields, "--limit", str(limit)]
    if milestone is not None:
        command += ["--milestone", milestone]
    return parse_issue_array(run_gh_read_command(command, timeout=timeout))


def milestone_open_issues(repo: str, milestone_number: int) -> list[dict]:
    """List open Issues for one exact Milestone number, including all pages."""
    raw = run_gh_read_command([
        "gh", "api",
        f"repos/{repo}/issues?milestone={milestone_number}&state=open&per_page=100",
        "--paginate", "--slurp",
    ])
    return parse_paginated_issue_array(raw)


def milestone_issues(repo: str, milestone_number: int,
                     state: str) -> list[dict]:
    """List Issues of one Milestone in one explicit state, all pages.

    The release scope derivation pins THIS query order
    (`state=` before `milestone=`); the tick sweep reads the open state
    through `milestone_open_issues`, whose order its own contract pins.
    """
    raw = run_gh_read_command([
        "gh", "api",
        f"repos/{repo}/issues?state={state}&milestone={milestone_number}&per_page=100",
        "--paginate", "--slurp",
    ])
    return [item for item in parse_paginated_issue_array(raw)
            if isinstance(item, dict)]


def list_milestones(repo: str, *, timeout: int | None = None) -> list[dict]:
    """List ALL Milestones of the repo (open and closed), all pages.

    ``timeout`` keeps the caller's bound (Issue #95: a network wait is
    a blocking command): the idle milestone-advance sweep bounded this
    read at 30 s before the move into this module.
    """
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/milestones?state=all&per_page=100",
        "--paginate", "--slurp",
    ], timeout=timeout)
    return parse_paginated_issue_array(raw)


def milestone_open_issue_count(repo: str, milestone_title: str) -> int:
    """Return the Open-Issue count of one Milestone (Issue #663).

    A single `gh api` call reads GitHub's own `open_issues` counter —
    the authority on whether the Milestone still has unfinished work.
    Unlike the search-based ready scans it does not depend on the search
    index and it is not affected by a delivery-state label, so an Issue
    temporarily outside the ready queue (`ai-blocked`, `ai-pr-opened`,
    or not yet indexed) still counts. The title-filtered `--jq` is the
    command documented in the Issue, verified against the live API. An
    empty result means the Milestone could not be found: that is a
    failed check, never a silent 0 (the caller must not release on it).
    """
    raw = run_gh_read_command(
        [
            "gh", "api", f"repos/{repo}/milestones",
            "--jq",
            f'.[] | select(.title=="{milestone_title}") | .open_issues',
        ],
        timeout=30,
    )
    if not raw:
        raise RuntimeError(
            f"Milestone {milestone_title!r} not found in {repo}"
        )
    return int(raw)


def close_milestone(repo: str, number: int) -> None:
    """Close one Milestone by number (the release completion step)."""
    run_command([
        "gh", "api", f"repos/{repo}/milestones/{number}",
        "--method", "PATCH", "-f", "state=closed",
    ])


def close_issue(number: int, *, repo: str) -> None:
    """Close one Issue by number (the reconciliation/cleanup step)."""
    run_command(["gh", "issue", "close", str(number), "--repo", repo])


def issue_view(number: int, fields: str, *, repo: str | None = None,
               cwd: Path | None = None, timeout: int | None = None) -> dict:
    """Read one Issue's JSON fields through `gh issue view`.

    Without ``repo`` the query runs in the checkout ``cwd`` points at
    (its `gh` repo context), byte-identical to the repo-free call sites.
    """
    command = ["gh", "issue", "view", str(number)]
    if repo is not None:
        command += ["--repo", repo]
    command += ["--json", fields]
    data = json.loads(run_gh_read_command(command, cwd=cwd, timeout=timeout))
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    return data


def pr_view(number: int, fields: str, *, repo: str | None = None,
            cwd: Path | None = None, timeout: int | None = None) -> dict:
    """Read one PR's JSON fields through `gh pr view` (same repo rule)."""
    command = ["gh", "pr", "view", str(number)]
    if repo is not None:
        command += ["--repo", repo]
    command += ["--json", fields]
    data = json.loads(run_gh_read_command(command, cwd=cwd, timeout=timeout))
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    return data


def commit_check_runs(repo: str, commit: str) -> list:
    """Read one commit's GitHub check runs (the CI gate evidence)."""
    checks = json.loads(run_gh_read_command([
        "gh", "api", f"repos/{repo}/commits/{commit}/check-runs",
        "--jq", ".check_runs",
    ]))
    if not isinstance(checks, list):
        raise ValueError("check-runs response must be an array")
    return checks


def release_view(repo: str, tag: str, fields: str) -> dict:
    """Read one GitHub Release's JSON fields by tag."""
    data = json.loads(run_gh_read_command([
        "gh", "release", "view", tag, "--repo", repo,
        "--json", fields,
    ]))
    if not isinstance(data, dict):
        raise ValueError("release view must be a JSON object")
    return data


def release_edit_notes(repo: str, tag: str, *, notes: str) -> None:
    """Replace one GitHub Release's notes."""
    run_command([
        "gh", "release", "edit", tag, "--repo", repo,
        "--notes", notes,
    ])


def release_create(repo: str, *, tag: str, version: str,
                   notes: str) -> None:
    """Publish one GitHub Release from an existing tag."""
    run_command([
        "gh", "release", "create", tag, "--repo", repo,
        "--verify-tag", "--title", version, "--notes", notes,
    ])


def issue_priority(issue: dict) -> str:
    """Return the pickup priority of one issue (Issue #101).

    `p0` when the issue carries the `p0` label, `normal` otherwise.
    The ready/in-flight/resumable scans fetch `labels` (verified
    against `gh issue list --help`: `labels` is a supported JSON
    field, an array of `{name, ...}` nodes), so this is a pure
    function of the scanned issue — no extra gh call. A missing or
    malformed `labels` field fails to `normal` (like the blockedBy
    field fails open): a P0 misread as normal only loses its ordering
    for one run, never the delivery.
    """
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return "normal"
    for label in labels:
        if isinstance(label, dict) and label.get("name") == P0_LABEL:
            return "p0"
    return "normal"


def epic_issue_with_blockers(repo: str, issue: dict) -> dict:
    """Add the native blocker field omitted by the REST issue listing."""
    if isinstance(issue.get("blockedBy"), dict):
        return issue
    number = issue.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ValueError("Epic number is missing or invalid")
    raw = run_gh_read_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "number,body,labels,blockedBy",
    ])
    details = json.loads(raw)
    if not isinstance(details, dict):
        raise ValueError(f"Epic #{number} details are not an object")
    return details


def open_blocker_numbers(issue: dict) -> list[int]:
    """Return the numbers of the issue's OPEN native GitHub blockers.

    `gh issue list --json blockedBy` (gh 2.94+) carries the native
    dependency relation as `{"nodes": [...], "totalCount": N}`. GitHub
    keeps a relation listed after its blocker closes (the node then
    carries `state: "CLOSED"` and is inert — verified against the live
    API, Issue #54), so only OPEN blockers actually block: a closed
    blocker clears the dependency without any runner-side bookkeeping,
    and the next tick claims the Issue. A node without an explicit
    `state` counts as open (claiming a possibly-blocked Issue costs a
    full run; waiting one tick does not). A missing or malformed field
    means "no known blockers" (fail open): an API shape change must
    never deadlock the queue.
    """
    blocked_by = issue.get("blockedBy")
    if not isinstance(blocked_by, dict):
        return []
    nodes = blocked_by.get("nodes")
    if not isinstance(nodes, list):
        return []
    numbers: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        number = node.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if node.get("state", "OPEN") != "OPEN":
            continue
        numbers.append(number)
    return numbers


def _verify_epic_complete(repo: str, listed_epic: dict) -> list[str]:
    """Verify native sub-issues and blockers; otherwise fail closed.

    Live API shape (measured 2026-09-09 against #305):
    ``GET /repos/{owner}/{repo}/issues/{number}/sub_issues`` returns a JSON
    array (``[]`` for no children), paginates with the standard ``--paginate``
    contract, and Issue items include ``repository.full_name`` and no
    ``pull_request`` key.  The endpoint is paginated with ``per_page=100``;
    a cross-repository item is identified by its ``repository.full_name`` and
    is rejected.  A PR-shaped item is identified by a non-null
    ``pull_request`` field and is checked through the PR endpoint.
    """
    epic = epic_issue_with_blockers(repo, listed_epic)
    number = epic.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ValueError("Epic number is missing or invalid")
    blockers = epic.get("blockedBy")
    # Live API check (Issue #552): `gh issue view --json blockedBy` returns
    # {"blockedBy":{"nodes":[],"totalCount":0}} for zero dependencies.
    # Missing/malformed blockedBy is not equivalent to that empty set.
    if not isinstance(blockers, dict) or not isinstance(blockers.get("nodes"), list):
        raise ValueError("native blocker/dependency state is unavailable")
    for node in blockers["nodes"]:
        if (not isinstance(node, dict)
                or not isinstance(node.get("number"), int)
                or isinstance(node.get("number"), bool)
                or ("state" in node and node["state"] not in {"OPEN", "CLOSED"})):
            raise ValueError("native blocker/dependency state is malformed")
    open_blockers = open_blocker_numbers(epic)
    if open_blockers:
        raise ValueError("open blockers: " + ", ".join(f"#{n}" for n in open_blockers))
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/issues/{number}/sub_issues?per_page=100",
        "--paginate", "--slurp",
    ])
    children = parse_paginated_issue_array(raw)
    if not children:
        raise ValueError("Epic child scope is missing or empty")
    child_evidence: list[str] = []
    for child in children:
        child_number = child.get("number")
        if not isinstance(child_number, int) or isinstance(child_number, bool):
            raise ValueError("Epic child scope contains an invalid number")
        child_repo = child.get("repository")
        if not isinstance(child_repo, dict) or not isinstance(child_repo.get("full_name"), str):
            raise ValueError(f"child #{child_number} repository state is malformed")
        if child_repo["full_name"].lower() != repo.lower():
            raise ValueError(f"cross-repository child #{child_number}")
        if child.get("state") not in {"open", "closed"}:
            raise ValueError(f"child #{child_number} state is malformed")
        kind = "pr" if child.get("pull_request") is not None else "issue"
        child_evidence.append(_epic_child_evidence(repo, kind, child_number))
    return child_evidence


def _epic_child_evidence(repo: str, kind: str, number: int) -> str:
    """Verify one child against GitHub's live Issue/PR state."""
    raw = run_gh_read_command(["gh", "api", f"repos/{repo}/issues/{number}"])
    item = json.loads(raw)
    if not isinstance(item, dict):
        raise ValueError(f"child #{number} response is not an object")
    is_pr = "pull_request" in item
    if kind == "issue" and is_pr:
        raise ValueError(f"child #{number} declared as Issue but is a PR")
    if kind == "pr" and not is_pr:
        raise ValueError(f"child #{number} declared as PR but is an Issue")
    if is_pr:
        pr = json.loads(run_gh_read_command(["gh", "api", f"repos/{repo}/pulls/{number}"]))
        if not isinstance(pr, dict) or pr.get("merged") is not True:
            raise ValueError(f"child PR #{number} is not merged")
        return f"PR #{number} merged"
    if item.get("state") != "closed":
        raise ValueError(f"child Issue #{number} is not closed")
    return f"Issue #{number} closed"


def _epic_audit(child_evidence: list[str], version: str | None = None) -> str:
    prefix = f" for {version}" if version else ""
    return (f"Epic reconciliation{prefix}: complete; "
            f"children: {', '.join(child_evidence)}; "
            "no open native blockers/dependencies.")


# Only comments posted by a repo maintainer are trusted to carry the
# recovery scene: a public comment (authorAssociation=NONE) must never
# steer the runner into an arbitrary local worktree, branch or PR
# (Issue #45 review, BLOCKER). A missing association is never trusted.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({
    "OWNER", "MAINTAINER", "MEMBER", "COLLABORATOR",
})


def edit_issue(number: int, *, repo: str, add: str | None = None,
               remove: str | None = None) -> None:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if add:
        command += ["--add-label", add]
    if remove:
        command += ["--remove-label", remove]
    run_command(command)


def apply_label_patch(number: int, *, repo: str, event: str,
                      current_labels) -> None:
    """Compute the deterministic label patch for `event` and apply it.

    `current_labels` is the Issue's current label names (read once by the
    caller). The patch comes from `delivery_labels.label_patch` — the
    single source of truth for the transition rules (Issue #175) — so the
    same current labels and event always produce the same idempotent
    patch. The patch is applied through `edit_issue`: one call for the
    add plus the first remove, then one call per extra remove (the exact
    same `edit_issue` kwargs the pre-#175 code emitted). A no-op patch
    (nothing to add or remove) applies nothing.
    """
    to_add, to_remove = label_patch(event, current_labels)
    if not to_add and not to_remove:
        return
    first_remove = to_remove[0] if to_remove else None
    kwargs: dict = {"repo": repo}
    if to_add:
        kwargs["add"] = to_add[0]
    if first_remove is not None:
        kwargs["remove"] = first_remove
    edit_issue(number, **kwargs)
    for label in to_remove[1:]:
        edit_issue(number, repo=repo, remove=label)


def comment_issue(number: int, *, repo: str, body: str) -> None:
    run_command(["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", format_status_comment(body)])


def issue_comments(number: int, *, repo: str) -> list[dict]:
    """Return the Issue's comment history (oldest first) from GitHub.

    ``gh issue view --json comments`` returns a top-level object with a
    ``comments`` array; each comment carries the author and the
    ``authorAssociation`` of the viewer, which is how the runner tells
    its own trusted comments apart from public ones (Issue #45).
    """
    raw = run_gh_read_command([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "comments",
    ], timeout=30)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("issue view must be a JSON object")
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("issue comments must be a JSON array")
    return comments


def pr_comments(number: int, *, repo: str) -> list[dict]:
    """Return the PR's comment history (oldest first) from GitHub.

    The PR-side twin of `issue_comments`: a PR's comments are read
    through `gh pr view --json comments` (`gh issue view` rejects PR
    numbers), the same top-level object with a `comments` array — and
    the same 30 s bound (#745 bounded the issue-side read; an
    unbounded comments read is the same hang on the PR side).
    """
    raw = run_gh_read_command([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "comments",
    ], timeout=30)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("pr comments must be a JSON array")
    return comments


def trusted_issue_comments_block(comments: list[dict], limit: int) -> str:
    """Render the {{ISSUE_COMMENTS}} prompt block (Issue #745).

    Only trusted authors enter the task context — the same
    `authorAssociation` trust set as the recovery-scene parser (Issue
    #45; a public repo lets anyone comment, and an unfiltered injection
    would be a prompt-injection surface). The input order is preserved
    (oldest first, the natural timeline read). Over `limit` the OLDEST
    trusted comments are dropped — the newest carry the latest decision
    — and the omission is stated inside the block: the agent must know
    it did not see the full history, never a silent truncation. Zero
    trusted comments produce an explicit marker, not an empty string.
    """
    trusted = [
        comment for comment in comments if _comment_is_trusted(comment)
    ]
    kept = trusted[-limit:]
    omitted = len(trusted) - len(kept)
    if not kept:
        return "(no trusted comments)"
    lines = []
    if omitted:
        noun = "comment" if omitted == 1 else "comments"
        lines.append(
            f"({omitted} older trusted {noun} omitted; showing the "
            f"{len(kept)} most recent)"
        )
    for comment in kept:
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        lines.append(
            f"- {login or 'unknown'} "
            f"({comment.get('authorAssociation') or '-'}) "
            f"at {comment.get('createdAt') or '-'}:\n\n"
            f"{str(comment.get('body') or '').rstrip()}"
        )
    return "\n\n".join(lines)


def _authenticated_github_login() -> str:
    """Return the login represented by the active ``gh`` credential.

    ``gh api installation`` is unavailable with installation tokens, while
    ``gh auth status`` reports the account selected in gh's credential store.
    Read that local status instead of guessing a bot name or making an API
    request that cannot identify this credential shape.
    """
    try:
        # The comments being verified come from github.com.  Restrict the
        # status query to that host so an active account on another configured
        # GitHub Enterprise host cannot be mistaken for this credential.
        status = run_gh_read_command([
            "gh", "auth", "status", "--hostname", "github.com",
        ])
    except Exception as exc:
        raise ValueError(
            "GitHub identity resolution failed: `gh auth status` could not "
            f"read the active account: {exc}; run `gh auth login` or fix "
            "the GitHub credentials"
        ) from exc

    account: str | None = None
    active_account: str | None = None
    for line in status.splitlines():
        match = re.search(r"\baccount\s+(\S+)", line)
        if match:
            account = match.group(1)
        if re.search(r"Active account:\s*true\b", line, re.IGNORECASE):
            if account:
                active_account = account

    if not active_account:
        raise ValueError(
            "GitHub identity resolution failed: `gh auth status` did not "
            "report an active account; run `gh auth login` or select an "
            "active github.com account with `gh auth switch`"
        )
    return active_account


def _strip_bot_suffix(login: str) -> str:
    """Drop the optional ``[bot]`` suffix from an App login.

    ``gh issue view --json comments`` reads comments through GraphQL and
    reports ``author.login`` without the ``[bot]`` suffix, while REST's
    ``user.login`` keeps it (Issue #655). Both shapes name the same App
    credential, so the suffix is normalized away before comparison.
    """
    return login[:-5] if login.endswith("[bot]") else login


def _comment_is_trusted(comment: object) -> bool:
    """True when the comment is from a maintainer or this runner's App bot."""
    if not isinstance(comment, dict):
        return False
    if comment.get("authorAssociation") in TRUSTED_COMMENT_ASSOCIATIONS:
        return True
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str):
        return False
    # A copied run marker is not sufficient: the author must be the account
    # represented by the currently authenticated installation token. The
    # optional `[bot]` suffix is normalized on both sides because GraphQL
    # drops it and REST keeps it (Issue #655).
    return _strip_bot_suffix(login) == _strip_bot_suffix(
        _authenticated_github_login()
    )


def latest_run_marker(comments: list[dict]) -> str:
    """The latest trusted comment's rendered run marker, or "".

    Recovery reports name the run they failed for when any trusted
    comment of the Issue still carries the marker; an empty string when
    none does. A pure scan over the already-fetched comment list.
    """
    for comment in reversed(comments):
        if not _comment_is_trusted(comment):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = RUN_MARKER_PATTERN.search(body)
        if match:
            return run_marker(match.group(1))
    return ""


def open_pr_for_branch(repo_dir: Path, branch: str) -> dict | None:
    """Return the sole open PR for a branch, or None when absent."""
    raw = run_gh_read_command([
        "gh", "pr", "list", "--state", "open", "--head", branch,
        "--json", "number,url,baseRefName,headRefName,headRefOid",
        "--limit", "2",
    ], cwd=repo_dir, timeout=RESUME_PR_STATE_TIMEOUT_SECONDS)
    prs = json.loads(raw) if raw.strip() else []
    if not isinstance(prs, list):
        raise RuntimeError("open PR query must return an array")
    if len(prs) > 1:
        raise RuntimeError(
            f"multiple open PRs for stable delivery branch {branch}"
        )
    return prs[0] if prs else None


def issue_labels(number: int, repo: str) -> list[str]:
    """Return the current label names of one Issue."""
    data = issue_view(number, "labels", repo=repo)
    labels = data.get("labels")
    if not isinstance(labels, list):
        raise ValueError("issue labels must be a JSON array")
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def has_in_progress_label(number: int, repo: str) -> bool:
    """True while the Issue carries `ai-in-progress` (the live claim state).

    Read via `gh issue view` — a direct, strongly consistent read. The
    pre-#658 implementation used `gh issue list --search`, the same
    eventually-consistent index the pickup scan reads, so it could not
    see a label another instance added seconds ago — exactly the moment
    this check exists to catch (the pre-claim race guard).
    """
    labels = issue_view(number, "labels", repo=repo).get("labels")
    if not isinstance(labels, list):
        raise ValueError("issue view labels must be a JSON array")
    return any(
        isinstance(label, dict) and label.get("name") == IN_PROGRESS_LABEL
        for label in labels
    )


def _pr_number(pr_url: str) -> int:
    """Extract the PR number from its URL (the last path segment)."""
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])


def pr_delivery_status(pr_url: str, source_repo: str) -> tuple[str, list[str]]:
    """Return PR state and CI summaries for delivery-wait evidence."""
    number = _pr_number(pr_url)
    data = pr_view(number, "state,statusCheckRollup", repo=source_repo)
    state = data.get("state")
    if state not in ("OPEN", "MERGED", "CLOSED"):
        raise ValueError(f"unexpected PR state: {state!r}")
    rollup = data.get("statusCheckRollup")
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list):
        raise ValueError("pr statusCheckRollup must be a JSON array")
    summaries = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        name = check.get("name", check.get("context", "check"))
        status = check.get("status", check.get("state", "UNKNOWN"))
        conclusion = check.get("conclusion")
        detail = str(status)
        if conclusion:
            detail += f"/{conclusion}"
        summaries.append(f"{name}={detail}")
    return state, summaries


def pr_state(pr_url: str, source_repo: str) -> str:
    """Return a PR's state from the configured source repository."""
    return pr_delivery_status(pr_url, source_repo)[0]
