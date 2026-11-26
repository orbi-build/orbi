"""GitHub read/write data access at the `gh` command line.

One function per GitHub read/write, with the `gh` argument assembly, the
`--json` field sets, the JSON shape validation, and the read-only retry
of `run_gh_read_command` all in this one leaf — no call site
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
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote

from orbi import checks, gh_response, merge_gate_reads, notify
from orbi.delivery_labels import label_name_set, label_patch
from orbi import delivery_labels
from orbi.journal import LOGGER, event, run_command, single_line
from orbi.milestone_errors import MilestoneReconcileError, classify_milestone_error
from orbi.progress import format_status_comment

GH_READ_MAX_ATTEMPTS = 3
GH_READ_BACKOFF_SECONDS = 1
# Writes are retried only for bounded, server-side failures.  A failed
# request may have reached GitHub, so callers with a natural idempotency
# check (comments) can stop before issuing the request again.
GH_WRITE_MAX_ATTEMPTS = 3
GH_WRITE_BACKOFF_SECONDS = 1
GH_WRITE_MAX_SECONDS = 30
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
# The one wall-clock bound of a creating/closing Issue write (Issue #2394):
# a hung `gh` fails fast instead of stalling the tick.
ISSUE_TIMEOUT_SECONDS = 30


def merge_gate_preflight(repo: str, branch: str) -> list[str]:
    """Classify protection visible to the configured GitHub token.

    The reads and their failure handling live in `orbi.merge_gate_reads`
    (extracted unchanged, Issue #2394); the rule that turns the payloads
    into blocker lines is `orbi.merge_gate.classify_protection`. The `gh`
    read seam is handed in, so the retry and response-status rule stays
    this module's.
    """
    return merge_gate_reads.merge_gate_preflight(
        repo, branch, read=run_gh_read_command,
    )


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
    failure_log_level: int = logging.ERROR,
) -> str:
    """Run one read-only gh command with bounded transient-failure retries.

    A rate limit (a 403 with `x-ratelimit-remaining: 0`), a 429 or a
    GitHub-side 5xx used to crash a whole tick that was only reading.
    Only provably read-only commands (`_is_readonly_gh_command`) whose
    response carries a transient status are retried; a `gh` subcommand
    that prints no status line keeps GitHub's own server sentence
    (`gh_response.transient_command`), so the retry Issue #738 added
    survives for every read. No write path can ever reach this retry
    loop; every retry logs a structured `gh_read_retry` line, and the
    last error is re-raised carrying the response status so the failure
    handling classifies it from the status rather than the text
    (Issue #1760).
    """
    execute = command_runner or run_command
    command = gh_response.include_headers(command)
    attempt = 0
    while True:
        attempt += 1
        try:
            # Forward only the set options: None is run_command's own
            # default, and passing it explicitly would change the call
            # observed by the run_command fakes and probes.
            kwargs: dict[str, object] = {}
            if cwd is not None:
                kwargs["cwd"] = cwd
            if timeout is not None:
                kwargs["timeout"] = timeout
            if failure_log_level != logging.ERROR:
                kwargs["failure_log_level"] = failure_log_level
            return gh_response.response_parts(execute(command, **kwargs))[2]
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            failure = gh_response.failed_request(exc)
            retryable = (
                _is_readonly_gh_command(command)
                and gh_response.transient_command(failure)
            )
            if attempt >= GH_READ_MAX_ATTEMPTS or not retryable:
                raise failure
        delay = GH_READ_BACKOFF_SECONDS * (2 ** (attempt - 1))
        event(
            "gh_read_retry", level=logging.WARNING,
            command=single_line(" ".join(command)), attempt=attempt + 1,
            max_attempts=GH_READ_MAX_ATTEMPTS, delay_seconds=delay,
            stderr=single_line(detail),
        )
        time.sleep(delay)


def run_gh_write_command(
    command: list[str], *, cwd: Path | None = None,
    timeout: int | None = None,
    command_runner: Callable[..., str] | None = None,
    already_applied: Callable[[], bool] | None = None,
) -> str:
    """Run a GitHub write with bounded retries for server-side failures.

    GitHub may execute a mutation and lose the response.  ``already_applied``
    is therefore checked before each retry; it lets comment callers use the
    run marker/body as an idempotency key rather than creating duplicates.
    Permission and validation failures are re-raised immediately, as is the
    exhausted retry — that STRUCTURED failure is what the classifier reads,
    so a transport timeout stays `github_transient` (Issue #2443).
    """
    execute = command_runner or run_command
    command = gh_response.include_headers(command)
    started = time.monotonic()
    for attempt in range(1, GH_WRITE_MAX_ATTEMPTS + 1):
        try:
            kwargs = {}
            if cwd is not None:
                kwargs["cwd"] = cwd
            if timeout is not None:
                kwargs["timeout"] = timeout
            return gh_response.response_parts(execute(command, **kwargs))[2]
        except subprocess.CalledProcessError as exc:
            failure = gh_response.failed_request(exc)
            if not gh_response.transient_command(failure):
                raise failure
            if already_applied is not None:
                try:
                    if already_applied():
                        LOGGER.warning(
                            "gh write response lost after side effect; "
                            "idempotency check found it already applied",
                        )
                        return ""
                except Exception:
                    LOGGER.warning(
                        "gh write idempotency check failed; retrying",
                        exc_info=True,
                    )
            elapsed = time.monotonic() - started
            if attempt >= GH_WRITE_MAX_ATTEMPTS or elapsed >= GH_WRITE_MAX_SECONDS:
                raise failure
            delay = min(
                GH_WRITE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                max(0, GH_WRITE_MAX_SECONDS - elapsed),
            )
            event(
                "gh_write_retry", level=logging.WARNING,
                command=single_line(" ".join(command)),
                attempt=attempt + 1, max_attempts=GH_WRITE_MAX_ATTEMPTS,
                delay_seconds=delay,
                stderr=single_line((exc.stderr or "").strip()),
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

    Every ``gh issue list`` call site shares this single
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
    ], timeout=30)
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


def list_milestones(repo: str, *, state: str = "all",
                    timeout: int | None = None) -> list[dict]:
    try:
        raw = run_gh_read_command([
            "gh", "api", f"repos/{repo}/milestones?state={state}&per_page=100",
            "--paginate", "--slurp",
        ], timeout=timeout, failure_log_level=logging.DEBUG)
    except subprocess.CalledProcessError as exc:
        classify_milestone_error(exc, "list_milestones")
    return parse_paginated_issue_array(raw)


def milestone_by_title(repo: str, title: str, *, state: str = "open",
                       timeout: int | None = None) -> dict | None:
    """The one Milestone whose title is exactly `title` in `state`, or None.

    The ONE title lookup (Issue #2394): `/orbi milestone`, `orbi draft` and
    `orbi suggest` all ask this, so a CLOSED Milestone of the same title is
    never mistaken for a ready one. The list is read paginated and matched
    in Python, so a target beyond the first REST page is found and a title
    carrying a `"` cannot break the query (Issue #1994).
    """
    for milestone in list_milestones(repo, state=state, timeout=timeout):
        if milestone.get("title") == title:
            return milestone
    return None


def milestone_open_issue_count(repo: str, milestone_title: str) -> int:
    """GitHub's own Open-Issue count of one open Milestone (Issue #663).

    Unlike the search-based ready scans it does not depend on the search
    index or a delivery-state label. No match is a failed check, never a
    silent 0 (the caller must not release on it).
    """
    milestone = milestone_by_title(repo, milestone_title, timeout=30)
    if milestone is None:
        raise RuntimeError(f"Milestone {milestone_title!r} not found in {repo}")
    return int(milestone["open_issues"])


def close_milestone(repo: str, number: int) -> None:
    """Close one Milestone by number (the release completion step)."""
    run_command([
        "gh", "api", f"repos/{repo}/milestones/{number}",
        "--method", "PATCH", "-f", "state=closed",
    ], timeout=30)


def close_issue(number: int, *, repo: str, reason: str | None = None,
                comment: str | None = None) -> None:
    """Close one Issue by number (the ONE close, Issue #2394).

    `reason` is `gh issue close --reason`'s documented value set
    (`completed`, `not planned`, `duplicate`); `None` keeps the completed
    default. `comment` posts a receipt in the same write. Closing is
    idempotent, so a transient failure is retried under the one bounded
    timeout; with a comment the retry first proves the close landed, so a
    lost response cannot post the receipt twice.
    """
    command = ["gh", "issue", "close", str(number), "--repo", repo]
    if reason is not None:
        command += ["--reason", reason]
    if comment is not None:
        command += ["--comment", comment]

    def already_closed() -> bool:
        return issue_view(number, "state", repo=repo).get("state") == "CLOSED"

    run_gh_write_command(
        command, timeout=ISSUE_TIMEOUT_SECONDS,
        already_applied=already_closed if comment is not None else None,
    )


def issue_number_from_url(url: str) -> int:
    """The Issue number in one `gh issue create` URL (its last segment)."""
    match = re.search(r"/issues/([0-9]+)$", str(url).strip().rstrip("/"))
    if match is None:
        raise ValueError(f"no issue number in URL: {url!r}")
    return int(match.group(1))


def create_issue(repo: str, *, title: str, body: str, labels: Sequence[str] = (),
                 milestone: str | None = None, extra_args: Sequence[str] = (),
                 cwd: Path | None = None, timeout: int = ISSUE_TIMEOUT_SECONDS,
                 existing: Callable[[], int | None] | None = None,
                 command_runner: Callable[..., str] | None = None) -> int:
    """Create one Issue and return its number (the ONE creation, #2394).

    `labels`, `milestone` and `extra_args` (gh's `--attach` flags) ride
    along in the caller's order. Creation is NEVER blindly retried — a lost
    response may have created the Issue — so it retries only when
    `existing` proves an earlier attempt landed, by returning that number.
    An attachment gh refuses still creates the Issue and prints its URL on
    the failed call, which IS a creation (Issue #2364).
    """
    command = ["gh", "issue", "create", "--repo", repo,
               "--title", title, "--body", body, *extra_args]
    command += [item for label in labels for item in ("--label", label)]
    if milestone is not None:
        command += ["--milestone", milestone]
    created: list[int] = []

    def already_created() -> bool:
        found = existing() if existing is not None else None
        if found is None:
            return False
        created.append(found)
        return True

    execute = command_runner or run_command
    try:
        if existing is None:
            options: dict[str, object] = {"timeout": timeout}
            if cwd is not None:
                options["cwd"] = cwd
            url = execute(command, **options)
        else:
            url = run_gh_write_command(
                command, cwd=cwd, timeout=timeout,
                command_runner=command_runner, already_applied=already_created,
            )
    except subprocess.CalledProcessError as exc:
        url = (exc.stdout or "").strip()
        if not extra_args or not url.startswith("https://"):
            raise
    return created[0] if created else issue_number_from_url(url)


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
    """Read EVERY GitHub check run of one commit (the CI gate evidence).

    GitHub serves 30 runs per page, so an unpaginated read hid check #31
    and the release gate released while it was failing (Issue #1993).
    """
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/commits/{commit}/check-runs",
        "--paginate", "--slurp",
    ])
    return checks.parse_paginated_check_runs(raw)


def branch_head_sha(repo: str, branch: str) -> str:
    """The branch resource's CURRENT head SHA (the live base head).

    The same `branches/{branch}` resource the merge-gate preflight reads.
    A caller that must know where the base branch stands NOW (the
    base-branch-red recovery, Issue #1825) can never use the PR's
    `baseRefOid`: that field is the base snapshot the PR was compared
    against, so a commit landing on the base afterwards stays invisible
    there. A response without a commit SHA fails fast.
    """
    encoded_branch = quote(branch, safe="")
    data = json.loads(run_gh_read_command([
        "gh", "api", f"repos/{repo}/branches/{encoded_branch}",
    ], timeout=30))
    commit = data.get("commit") if isinstance(data, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"branch {branch!r} reports no head sha")
    return sha


def issue_priority(issue: dict) -> str:
    """Return the pickup priority of one issue.

    `p0` when the issue carries the `p0` label, `normal` otherwise.
    The ready/in-flight/resumable scans fetch `labels` (verified
    against `gh issue list --help`: `labels` is a supported JSON
    field, an array of `{name, ...}` nodes), so this is a pure
    function of the scanned issue — no extra gh call. A missing or
    malformed `labels` field fails to `normal` (like the blockedBy
    field fails open): a P0 misread as normal only loses its ordering
    for one run, never the delivery.
    """
    return "p0" if delivery_labels.P0_LABEL in label_name_set(issue) else "normal"


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
    API), so only OPEN blockers actually block: a closed
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


# The repository permissions that mean "this author can write here"
# (Issue #1806). GitHub reports the effective permission of the user on
# the endpoint — `maintain` arrives as `write` and `triage` as `read`
# (https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user)
# — so these two values are the whole write set.
WRITE_PERMISSIONS = frozenset({"admin", "write"})


def repository_permission(login: str, *, repo: str) -> str:
    """The permission GitHub reports for `login` on `repo` (Issue #1806).

    One read of the documented `collaborators/{login}/permission`
    endpoint, which reports the user's repository permission whatever the
    visibility of their organization membership — the field the comment's
    `authorAssociation` cannot answer. A 404 (the login is not visible to
    this token) means no access; any other failed read is a GitHub read
    failure and propagates.
    """
    try:
        raw = run_gh_read_command([
            "gh", "api", f"repos/{repo}/collaborators/{login}/permission",
        ], timeout=30)
    except subprocess.CalledProcessError as exc:
        if gh_response.status_of(exc) == 404:
            return "none"
        raise
    data = json.loads(raw)
    permission = data.get("permission") if isinstance(data, dict) else None
    if not isinstance(permission, str):
        raise ValueError(
            f"repository permission read for {login} on {repo} is malformed"
        )
    return permission


def edit_issue(number: int, *, repo: str, add: str | None = None,
               remove: str | None = None) -> None:
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if add:
        command += ["--add-label", add]
    if remove:
        command += ["--remove-label", remove]
    run_gh_write_command(command)


def set_issue_labels(number: int, *, repo: str,
                     labels: list[str]) -> list[str]:
    """Replace an Issue's WHOLE label set in one write (Issues #2369, #2426).

    GitHub's documented "Set labels for an issue"
    (`PUT /repos/{owner}/{repo}/issues/{issue_number}/labels`,
    https://docs.github.com/en/rest/issues/labels#set-labels-for-an-issue)
    "Removes any previous labels and sets the new labels" — the removals
    come first, so the timeline ends on the new state, and it is ONE
    request: GitHub cannot apply the removal and lose the addition. gh's
    documented array form is repeated `-f labels[]=<name>`.

    A response lost after GitHub executed the request is settled the only
    way it can be: the read after the failure decides (Issue #2426). When
    the Issue holds the intended set the change is done — no second write,
    no compensating write — and the labels the write produced are
    returned. When it does not, the write's own error propagates and the
    Issue is exactly as it was before.
    """
    command = [
        "gh", "api", "--method", "PUT",
        f"repos/{repo}/issues/{number}/labels",
    ]
    for label in labels:
        command += ["-f", f"labels[]={label}"]
    wanted = set(labels)
    run_gh_write_command(
        command,
        already_applied=lambda: set(issue_labels(number, repo=repo)) == wanted,
    )
    return labels


def _replace_labels(number: int, *, repo: str, current: list[str],
                    add: Sequence[str] = (),
                    remove: Sequence[str] = ()) -> list[str]:
    """Replace `current` with `add` in and `remove` out, in ONE write.

    `current` is the Issue's own read: it is both the input of the change
    and the set every label that is not being swapped out (`bug`, `p0`,
    a customer's own label) is carried over from, so a label a caller only
    believes is there is never written back. Returns the set it now holds.
    """
    dropped = set(remove)
    whole = [label for label in current if label not in dropped]
    whole += [label for label in add if label not in whole]
    if whole == current:
        return current
    return set_issue_labels(number, repo=repo, labels=whole)


def swap_issue_labels(number: int, *, repo: str,
                      add: Sequence[str] = (),
                      remove: Sequence[str] = ()) -> list[str]:
    """Add and remove labels in ONE whole-set write (Issue #2426)."""
    return _replace_labels(
        number, repo=repo, current=issue_labels(number, repo=repo),
        add=add, remove=remove,
    )


def apply_label_patch(number: int, *, repo: str, event: str,
                      ready_label: str | None = None) -> list[str] | None:
    """Compute the deterministic label patch for `event` and apply it.

    `ready_label` (the resolved claim label, Issue #1775) and the Issue's
    real labels feed `delivery_labels.label_patch`, the one transition
    decision; the labels are read here because a caller's snapshot
    predates the write and the write replaces the WHOLE set. Every event
    lands as that one replacement (Issue #2426): a delivery-label change is
    all-or-nothing, so after any failure the Issue holds exactly the labels
    it had or exactly the labels the transition intended — never a removal
    without its add. A no-op patch applies nothing and returns None;
    `notify.patch_applied` (Issues #1673, #2287) sends an ordinary
    handoff's message, never a stop's.
    """
    ready_label = delivery_labels.queue_label(ready_label)
    current_labels = issue_labels(number, repo=repo)
    to_add, to_remove = label_patch(event, current_labels, ready_label)
    if not to_add and not to_remove:
        return None
    applied = _replace_labels(
        number, repo=repo, current=current_labels,
        add=to_add, remove=to_remove,
    )
    if applied == current_labels:
        return None
    # Notifying the person is a pure bypass owned by `notify` (Issues
    # #1673/#1691): it decides from the event, the added labels and the
    # current labels, and reads the title lazily through the callable --
    # so a tick with notifications off makes no extra `gh` call.
    notify.patch_applied(
        event, to_add, current_labels,
        repo=repo, number=number,
        read_title=lambda: issue_title(number, repo=repo),
    )
    return applied


def issue_title(number: int, *, repo: str) -> str | None:
    """One Issue title for a notification; None when unreadable (#1673)."""
    try:
        return issue_view(number, "title", repo=repo, timeout=30).get("title")
    except Exception:
        return None


def comment_issue(number: int, *, repo: str, body: str) -> str:
    """Post one comment; return the permalink `gh issue comment` printed.

    gh 2.100.0 prints the created comment's URL on stdout
    (`prShared.createComment`, the deploy host's version); the idempotency
    retry prints nothing, so "" means "posted, permalink unknown" (#2287).
    """
    rendered = format_status_comment(body)
    command = ["gh", "issue", "comment", str(number), "--repo", repo,
               "--body", rendered]

    def already_posted() -> bool:
        return any(
            isinstance(comment, dict) and comment.get("body") == rendered
            for comment in issue_comments(number, repo=repo)
        )

    return run_gh_write_command(command, already_applied=already_posted).strip()


def issue_comment_rest_id(
    number: int, *, repo: str, node_id: str,
) -> int:
    """Resolve a GraphQL Issue-comment node id to its REST numeric id.

    ``gh issue view --json comments`` exposes GraphQL node ids, while the
    REST PATCH route used by ``update_issue_comment`` requires ``id``.
    """
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/issues/{number}/comments",
        "--paginate", "--slurp",
    ], timeout=30)
    data = json.loads(raw)
    pages = data if isinstance(data, list) else []
    comments = (
        [comment for page in pages if isinstance(page, list)
         for comment in page if isinstance(comment, dict)]
        if any(isinstance(page, list) for page in pages)
        else [comment for comment in pages if isinstance(comment, dict)]
    )
    for comment in comments:
        if comment.get("node_id") == node_id:
            comment_id = comment.get("id")
            if isinstance(comment_id, int) and not isinstance(comment_id, bool):
                return comment_id
            break
    raise ValueError(
        f"REST comment id not found for GraphQL node id {node_id!r}"
    )


def update_issue_comment(comment_id: int, *, repo: str, body: str) -> None:
    """Patch one Issue comment in place (Issue #825's repeat counter).

    The route is the one the progress publisher's `_patch_comment` uses:
    PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}. The body
    passes through `format_status_comment` like every posted comment —
    idempotent for a stored body (the stored shape takes the verbatim
    path and the hidden runner marker is refreshed).
    """
    run_gh_write_command([
        "gh", "api", f"repos/{repo}/issues/comments/{comment_id}",
        "--method", "PATCH", "--field", f"body={format_status_comment(body)}",
    ])


def issue_comments(number: int, *, repo: str) -> list[dict]:
    """Return the Issue's comment history (oldest first) from GitHub.

    ``gh issue view --json comments`` returns a top-level object with a
    ``comments`` array; each comment carries its author, which is what
    the trust decision reads (`orbi.comment_trust`, Issue #1806). The
    ``authorAssociation`` field rides along: it is the viewer's view of
    the author's membership and decides nothing.
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
    """Return the PR's conversation comments (oldest first)."""
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


def pr_reviews(number: int, *, repo: str) -> list[dict]:
    """Return formal PR reviews, including their state and body."""
    raw = run_gh_read_command([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "reviews",
    ], timeout=30)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("pr view must be a JSON object")
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("pr reviews must be a JSON array")
    return reviews


def pr_review_comments(number: int, *, repo: str) -> list[dict]:
    """Return inline review comments, including their path and line."""
    raw = run_gh_read_command([
        "gh", "api", f"repos/{repo}/pulls/{number}/comments",
        "--paginate", "--slurp",
    ], timeout=30)
    return parse_paginated_issue_array(raw)



def authenticated_github_login() -> str:
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
    except subprocess.CalledProcessError as exc:
        # `gh auth status` exits 1 whenever ANY configured account fails
        # to authenticate, even when the active account is valid (the
        # stale secondary account of Issue #1074), and on that path it
        # prints the report to stderr. The parsed report below — not the
        # exit status — decides, so the captured streams are the status
        # text; no active account in them raises the same error as an
        # empty report.
        status = "\n".join(
            part.strip() for part in (exc.stdout or "", exc.stderr or ""))
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


def strip_bot_suffix(login: str) -> str:
    """Drop the optional ``[bot]`` suffix from an App login.

    ``gh issue view --json comments`` reads comments through GraphQL and
    reports ``author.login`` without the ``[bot]`` suffix, while REST's
    ``user.login`` keeps it. Both shapes name the same App
    credential, so the suffix is normalized away before comparison.
    """
    return login[:-5] if login.endswith("[bot]") else login


def list_pull_requests(repo: str | None, *, state: str, json_fields: str,
                       limit: int, head: str | None = None,
                       cwd: Path | None = None,
                       timeout: int = RESUME_PR_STATE_TIMEOUT_SECONDS,
                       ) -> list[dict]:
    """Run one `gh pr list` query (the ONE PR listing, Issue #2394).

    Every caller shares this read's timeout, retry and shape check: the
    branch query (`head`), the merge/recovery scans and the orphan sweep.
    Without `repo` the query uses the checkout `cwd` points at.
    """
    command = ["gh", "pr", "list", "--state", state]
    if head is not None:
        command += ["--head", head]
    if repo is not None:
        command += ["--repo", repo]
    command += ["--json", json_fields, "--limit", str(limit)]
    raw = run_gh_read_command(command, cwd=cwd, timeout=timeout)
    prs = json.loads(raw) if raw.strip() else []
    if not isinstance(prs, list):
        raise ValueError("gh pr list returned a non-array payload")
    return prs


_FOREIGN_PRS_JOURNALED: set[tuple[str, str]] = set()
def filter_same_repository_prs(prs: list, branch: str | None = None) -> list:
    """Drop the fork PRs GitHub marks `isCrossRepository` (Issue #2234).

    A fork can reuse a delivery branch name, so a branch/orphan query never
    treats one as this repository's (Issue #2394). `orbi suggest` keeps
    every open PR, forks included, by not calling this.
    """
    foreign = [p for p in prs if isinstance(p, dict) and p.get("isCrossRepository") is True]
    for pr in foreign:
        head = branch if branch is not None else pr.get("headRefName")
        key = (str(head), str(pr.get("url", "")))
        if key not in _FOREIGN_PRS_JOURNALED: event("foreign_pr_ignored", branch=key[0], pr=key[1]); _FOREIGN_PRS_JOURNALED.add(key)
    return [pr for pr in prs if pr not in foreign]


def open_pr_for_branch(repo_dir: Path | None, branch: str, *, repo: str | None = None) -> dict | None:
    """The ONE open PR of one delivery branch, or None (Issue #2394)."""
    prs = filter_same_repository_prs(list_pull_requests(
        repo, state="open", head=branch, limit=20, cwd=repo_dir,
        json_fields="number,url,baseRefName,headRefName,headRefOid,isCrossRepository",
    ), branch)
    if len(prs) > 1:
        raise RuntimeError(f"multiple open PRs for stable delivery branch {branch}")
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
        isinstance(label, dict) and label.get("name") == delivery_labels.IN_PROGRESS_LABEL
        for label in labels
    )


def pr_number(pr_url: str) -> int:
    """Extract the PR number from its URL (the last path segment)."""
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])
