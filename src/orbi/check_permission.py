"""The missing check-read permission scene (Issue #1918).

A delivery PR can be perfectly ready and still unable to move, because the
GitHub App installation has not approved a permission the App now requests:
the check read (`statusCheckRollup`, which carries commit-status contexts)
answers `Resource not accessible by integration` (HTTP 403). Before this
module the Runner retried the same read on every tick without telling
anyone — 472 errors a day, a `crash_loop` health alert and an Issue that
sat `ai-pr-opened` for days.

This module owns that one scene:

* the classification (`is_missing_permission`): GitHub's permission
  refusal on the check read — never a rate limit and never a transient
  read failure;
* the ONE readable comment (`comment_body`): the PR is open, which
  permission the installation has not approved, and the approval page for
  THIS installation — built from the repository owner's account type and
  the deployment's `installation_id`;
* the record (`orbi:check-permission:v1`): the PR and the lifecycle state,
  so a later tick re-tries the same read cheaply instead of posting a second
  comment;
* the recovery scan (`recover`): the read works again -> `ai-blocked`
  becomes `ai-fix-needed` and the existing resume flow absorbs the base
  and merges the PR as usual.

No new label, no new state store, no daemon: `ai-blocked` is the terminal
state every claim scan skips, and this bounded scan (one per tick, in the
same bypass slot as `base_ci_red.recover`) is the only path back.

The installation id is CONFIGURATION, not an API read: no App endpoint
lets an installation token read its own installation
(`GET /repos/{owner}/{repo}/installation` requires a JWT,
`GET /user/installations` a user token — checked against GitHub's REST
description). The account type is GitHub's own rule — the installation's
account is the repository's account — read from the documented
`GET /repos/{owner}/{repo}` -> `owner.type`.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess

from orbi import comment_trust, failure, gh_response, language, user_summary
from orbi.blocked_comment import post_blocked_comment
from orbi.comment_kind import (
    COMMENT_KIND_FAILURE, comment_kind, comment_kind_marker,
    parse_versioned_block,
)
from orbi.comment_rest_id import rest_comment_id
from orbi.delivery_labels import (
    BLOCKED_LABEL, FIX_NEEDED_LABEL, READY_LABEL,
)
from orbi.github import (
    edit_issue, issue_comments, issue_labels, list_issues, pr_number,
    run_gh_read_command, update_issue_comment,
)
from orbi.github_feedback import pr_delivery_rollup
from orbi.journal import LOGGER, event
from orbi.progress import run_marker

SCHEMA_VERSION = 1

# The record's lifecycle: `waiting` is the only state the recovery scan
# acts on, `resumed` is the automatic success. A closed set — an unknown
# state in a block is corruption, never a guess.
STATE_WAITING = "waiting"
STATE_RESUMED = "resumed"
STATES = frozenset({STATE_WAITING, STATE_RESUMED})

# The failure record's reason (closed set, `orbi.failure`) and the App
# permission whose missing approval causes the refused read: the commit
# statuses endpoint the rollup reads (`statuses`), at read level.
REASON_CODE = "checks_permission_missing"
MISSING_PERMISSION = "statuses: read"

# The one hidden block, riding the same envelope as every other versioned
# record (`comment_kind.parse_versioned_block`).
_MARKER = "orbi:check-permission:v1"
BLOCK_TEMPLATE = f"<!-- {_MARKER} {{payload}} -->"
_FIELDS = ("schema", "pr", "state")

# One bounded page per recovered scan: `ai-blocked` Issues are few, and an
# unbounded scan would multiply the per-tick read cost.
_SCAN_LIMIT = 200

# GitHub's own sentence for an App permission refusal. `gh pr view` is a
# GraphQL subcommand and prints NO response status line, so this sentence
# is the only signal the refused read carries (the response status wins
# whenever gh printed one).
_RESOURCE_NOT_ACCESSIBLE_RE = re.compile(
    r"resource not accessible by integration", re.IGNORECASE,
)


class CheckPermissionError(ValueError):
    """An `orbi:check-permission:v1` block is present but cannot be parsed."""


@dataclasses.dataclass(frozen=True)
class CheckPermission:
    """One refused-check-read record, exactly as the v1 block holds it."""

    pr: int
    state: str = STATE_WAITING
    schema: int = SCHEMA_VERSION


def _error(fields: dict) -> CheckPermission:
    """Validate one decoded payload into the record (fail fast)."""
    pr = fields["pr"]
    state = fields["state"]
    schema = fields["schema"]
    if schema != SCHEMA_VERSION:
        raise CheckPermissionError(f"unsupported schema: {schema!r}")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise CheckPermissionError(f"pr must be a positive integer: {pr!r}")
    if state not in STATES:
        raise CheckPermissionError(f"unknown state: {state!r}")
    return CheckPermission(pr=pr, state=state, schema=schema)


def render(record: CheckPermission) -> str:
    """Render the single hidden block for one record (deterministic)."""
    payload = json.dumps(
        {"schema": record.schema, "pr": record.pr, "state": record.state},
        separators=(",", ":"), ensure_ascii=False,
    )
    return BLOCK_TEMPLATE.format(payload=payload)


def parse(body: object) -> CheckPermission | None:
    """Decode the one block of a comment body, or None when there is none."""
    fields = parse_versioned_block(
        body, _MARKER, _FIELDS, CheckPermissionError,
    )
    return None if fields is None else _error(fields)


def with_state(record: CheckPermission, state: str) -> CheckPermission:
    """The same record in another lifecycle state."""
    return dataclasses.replace(record, state=state)


# --------------------------------------------------------------------------
# The classification
# --------------------------------------------------------------------------

def is_missing_permission(error: BaseException) -> bool:
    """Is this failure GitHub's permission refusal on the check read?

    A rate-limited 403 is GitHub's own limit and a rejected credential is a
    credential problem: neither is this scene. The response status decides
    whenever gh printed one; a subcommand that prints none (`gh pr view`)
    keeps GitHub's own sentence, and only an App permission refusal carries
    it (Issue #1760 keeps the status as the primary signal).
    """
    if gh_response.authentication_required(error) or gh_response.transient(
            error):
        return False
    status = gh_response.status_of(error)
    if status is not None:
        return status == 403
    detail = " ".join(
        str(part or "") for part in (
            getattr(error, "stderr", None), getattr(error, "stdout", None),
        )
    )
    return _RESOURCE_NOT_ACCESSIBLE_RE.search(detail) is not None


# --------------------------------------------------------------------------
# Where the account owner approves the permission
# --------------------------------------------------------------------------

def repository_owner_type(repo: str) -> str:
    """`User` or `Organization`: the repository owner's account type.

    GitHub's documented rule for an App installation is that its account is
    the repository's account, and `GET /repos/{owner}/{repo}` reports that
    type with the installation's own token. A missing field is a failed
    read (fail fast), never a guessed account.
    """
    payload = json.loads(run_gh_read_command(["gh", "api", f"repos/{repo}"]))
    owner = payload.get("owner") if isinstance(payload, dict) else None
    account_type = owner.get("type") if isinstance(owner, dict) else None
    if not isinstance(account_type, str) or not account_type:
        raise ValueError(
            f"repository owner type is missing in the read of {repo}"
        )
    return account_type


def approval_url(repo: str, *, installation_id: int | None) -> str:
    """The page where the account owner approves the App's permissions.

    GitHub serves a user-account installation under
    `https://github.com/settings/installations/<id>` and an organization's
    under `https://github.com/organizations/<org>/settings/installations/<id>`.
    Without a configured `installation_id` the account's installation list
    is the page (the same page, one click away) — never a guessed id.
    """
    owner, _, _ = repo.partition("/")
    if repository_owner_type(repo).lower() == "organization":
        page = f"https://github.com/organizations/{owner}/settings/installations"
    else:
        page = "https://github.com/settings/installations"
    return f"{page}/{installation_id}" if installation_id else page


# --------------------------------------------------------------------------
# The comment: what the user reads
# --------------------------------------------------------------------------

def comment_body(record: CheckPermission, *, url: str, run_id: str,
                 issue_number: int, issue_title: str | None,
                 language_code: str,
                 status_url: str | None = None) -> str:
    """The ONE readable comment of this scene.

    The user's order: what they already have (the open PR), why it paused
    (the permission GitHub refused), and the one repair (the approval
    page). The hidden `orbi:failure:v1` block leads (the machine-readable
    reason) and the `orbi:check-permission:v1` record closes the body.
    """
    parts = [
        failure.render(failure.Failure(
            reason_code=REASON_CODE, action_code="check_credentials",
            retry_safe=False, outcome="blocked",
        )),
        language.text(language_code, "check_permission.open", pr=record.pr),
    ]
    parts.extend(user_summary.summary_lines(
        status_url=status_url, issue=issue_number, title=issue_title,
        language_code=language_code,
    ))
    parts.append(language.text(
        language_code, "check_permission.reason", permission=MISSING_PERMISSION,
    ))
    parts.append(language.text(
        language_code, "check_permission.approve", url=url,
    ))
    parts.append(render(record))
    return (
        f"{run_marker(run_id)}\n"
        f"{comment_kind_marker(COMMENT_KIND_FAILURE)}\n"
        + "\n\n".join(parts)
    )


def block(issue_number: int, repo: str, pr_url: str, *, run_id: str,
          installation_id: int | None, language_code: str,
          issue_title: str | None = None,
          status_url: str | None = None,
          ready_label: str = READY_LABEL) -> CheckPermission:
    """Stop the delivery: ONE comment, then `ai-blocked`.

    The comment lands through the shared blocked exit (label patch first,
    then the comment and the community help line), so the terminal state is
    `ai-blocked` ALONE. Called only while the Issue is still resumable, so
    exactly one comment is posted per stop.
    """
    record = CheckPermission(pr=pr_number(pr_url))
    body = comment_body(
        record, url=approval_url(repo, installation_id=installation_id),
        run_id=run_id, issue_number=issue_number, issue_title=issue_title,
        language_code=language_code, status_url=status_url,
    )
    post_blocked_comment(
        issue_number, repo=repo, body=body,
        current_labels=set(issue_labels(issue_number, repo)),
        language_code=language_code, ready_label=ready_label,
    )
    event(
        "checks_permission_blocked", issue=issue_number, pr=record.pr,
        permission=MISSING_PERMISSION,
    )
    return record


def read_rollup(pr_url: str, repo: str, issue: dict, config,
                run_id: str) -> tuple[str | None, list]:
    """The delivery's ONE PR check read, with the permission stop.

    The refusal stops the delivery here (ONE comment, then `ai-blocked`)
    and reports it as `(None, [])` — the caller returns and the tick ends
    cleanly instead of crashing on the same refused read every tick. Every
    other failed read keeps its existing failure handling.
    """
    try:
        return pr_delivery_rollup(pr_url, repo)
    except subprocess.CalledProcessError as exc:
        if not is_missing_permission(exc):
            raise
        block(
            int(issue["number"]), repo, pr_url, run_id=run_id,
            installation_id=config.installation_id,
            language_code=language.of(config),
            issue_title=issue.get("title"), status_url=config.status_url,
            ready_label=config.dispatch_label or READY_LABEL,
        )
        return None, []


# --------------------------------------------------------------------------
# The automatic recovery scan
# --------------------------------------------------------------------------

def _latest_trusted_failure(
    comments: list[dict], *, repo: str,
) -> tuple[failure.Failure | None, dict] | None:
    """The Issue's NEWEST trusted failure comment, and its record.

    The recovery gate (the Issue #1833 rule the base-branch-red scan
    follows): a record only authorizes a step while the newest TRUSTED
    failure on the Issue is the failure that carries it. An older
    `waiting` record a later stop superseded — the account owner
    relabelled the Issue, or a second stop blocked it for another reason —
    must never clear that later `ai-blocked`.

    The failure comment is the canonical `failure` kind marker, never the
    mere presence of an `orbi:failure:v1` block: the short milestone
    comment repeats that block (Issue #1322) and is not a failure.
    """
    for comment in reversed(
            comment_trust.trusted_comments(comments, repo=repo)):
        body = comment.get("body")
        if comment_kind(body) != COMMENT_KIND_FAILURE:
            continue
        try:
            return failure.parse(body), comment
        except failure.FailureError:
            LOGGER.warning(
                "unreadable orbi:failure:v1 block url=%s", comment.get("url"),
            )
            return None, comment
    return None


def _waiting_block(comments: list[dict], *,
                   repo: str) -> tuple[CheckPermission, dict] | None:
    """The `waiting` record of the Issue's OWN permission stop, and its
    comment.

    Trust is the existing comment rule (repository write permission, or the
    authenticated runner account): a copied block from a read-only
    commenter authorizes nothing. The record is read only from the Issue's
    newest trusted failure, and only while that failure is this refusal.
    """
    located = _latest_trusted_failure(comments, repo=repo)
    if located is None:
        return None
    refusal, comment = located
    if refusal is None or refusal.reason_code != REASON_CODE:
        return None
    try:
        record = parse(comment.get("body"))
    except CheckPermissionError:
        LOGGER.warning(
            "unreadable check_permission block url=%s", comment.get("url"),
        )
        return None
    if record is None or record.state != STATE_WAITING:
        return None
    return record, comment


def _edit_state(repo: str, issue_number: int, comment: dict,
                record: CheckPermission) -> None:
    """Patch the ONE comment in place to the record's next state."""
    body = comment.get("body")
    comment_id = rest_comment_id(comment)
    if not isinstance(body, str) or comment_id is None:
        LOGGER.warning(
            "check_permission comment has no REST id issue=%s repo=%s",
            issue_number, repo,
        )
        return
    update_issue_comment(
        comment_id, repo=repo,
        body=body.replace(render(record),
                          render(with_state(record, STATE_RESUMED)), 1),
    )


def recover(repo: str) -> list[int]:
    """Re-try every paused check read once; resume the ones that work.

    One bounded scan per tick, before the resume scan: an `ai-blocked`
    Issue is skipped by every claim scan, so this is the only path back.
    For each Issue whose trusted record is still `waiting`:

    - the read succeeds -> `ai-blocked` becomes `ai-fix-needed` (the
      existing resume flow absorbs the base and merges) and the block says
      `resumed`;
    - the read is refused again -> no comment, no crash: the permission is
      still not approved, and this scan is the cheap retry.

    Returns the Issue numbers that were resumed.
    """
    resumed: list[int] = []
    for issue in list_issues(
        repo, state="open", label=BLOCKED_LABEL,
        json_fields="number,title", limit=_SCAN_LIMIT,
    ):
        number = issue["number"]
        located = _waiting_block(issue_comments(number, repo=repo), repo=repo)
        if located is None:
            continue
        record, comment = located
        pr_url = f"https://github.com/{repo}/pull/{record.pr}"
        try:
            pr_delivery_rollup(pr_url, repo)
        except Exception as exc:
            if is_missing_permission(exc):
                continue
            LOGGER.exception(
                "check_permission_read_failed issue=%s pr=%s repo=%s",
                number, record.pr, repo,
            )
            continue
        edit_issue(number, repo=repo, add=FIX_NEEDED_LABEL,
                   remove=BLOCKED_LABEL)
        _edit_state(repo, number, comment, record)
        event(
            "checks_permission_recovered", issue=number, pr=record.pr,
            permission=MISSING_PERMISSION,
        )
        resumed.append(number)
    return resumed
