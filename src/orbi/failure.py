"""The failure record: a versioned machine-readable reason block.

Every `Orbi: blocked` / `Orbi: fix needed` failure comment carries ONE
hidden, versioned block next to the run marker:

    <!-- orbi:failure:v1 {"schema": 1, ...} -->

The readable headline and the **Reason** / **Action** lines stay beside
it; a status reader (Orbi Cloud, a dashboard) parses only the block and
never the prose. The module is pure (the `scene` model): render and
parse are deterministic functions of their inputs, with no I/O. A
comment written before the block existed carries no block at all and is
ignored (`parse` returns `None`) instead of failing the resume path.

`reason_code` and `action_code` are CLOSED sets owned by the engine: a
new code is a deliberate contract change here and in `docs/workflow.mdx`
(EN + ZH); `classify` maps an exception to them and an unknown one to
`unclassified`, never to a missing block, so the prose cannot drift.
"""
from __future__ import annotations

import dataclasses
import json
import re

from orbi import language

SCHEMA_VERSION = 1

# The single hidden block: a versioned marker framing a JSON payload
# that mirrors the `Failure` fields exactly (render writes every field).
# The capture is non-greedy to the first `-->` so a truncated or
# mangled payload still matches the marker and fails as corrupted
# instead of being silently ignored.
FAILURE_BLOCK_TEMPLATE = "<!-- orbi:failure:v1 {payload} -->"
_FAILURE_BLOCK_RE = re.compile(
    r"<!--\s*orbi:failure:v1\s+(.*?)-->", re.DOTALL,
)

# Closed sets. A reason says WHY the delivery stopped; an action says
# WHAT the reader (a human or the Cloud) should do. Adding a member is a
# public contract change: update this module AND docs/workflow.mdx
# (EN + ZH) in the same PR.
REASON_CODES = frozenset({
    # A transient GitHub API/server failure (5xx, secondary rate limit,
    # `GraphQL: Something went wrong`): the same operation may succeed.
    "github_transient",
    # The delivery's own tests failed and the code must change.
    "tests_failed",
    # The bounded review/fix budget is spent — a human decision.
    "review_budget_exhausted",
    # An external precondition only a human can decide or repair.
    "human_decision_required",
    # The GitHub/provider credential is missing or rejected.
    "credential_missing",
    # The model provider quota is exhausted (429/RESOURCE_EXHAUSTED).
    "provider_quota",
    # The configured base branch does not exist in the repository
    # (Issue #1749): a repository/config precondition a human repairs.
    "base_branch_missing",
    # Any other runner exception the classifier does not recognize.
    "unclassified",
})
ACTION_CODES = frozenset({
    # Relabel the Issue `ai-ready` (or press retry) — nothing to fix.
    "requeue",
    # Fix the failure (code/ticket) and re-run the Issue.
    "fix_ticket",
    # A human decides the next step (review budget / precondition).
    "decide",
    # Verify and repair the credential, then retry.
    "check_credentials",
    # Retry after the provider quota window resets.
    "wait_quota",
})
OUTCOMES = frozenset({"blocked", "fix_needed"})


@dataclasses.dataclass(frozen=True)
class Failure:
    """One machine-readable failure reason, exactly as the v1 block holds it.

    `outcome` is `blocked` (terminal, human decides) or `fix_needed`
    (the engine retries); `retry_safe` says whether a plain re-queue can
    succeed without a human repair. `schema` mirrors the marker version.
    """

    reason_code: str
    action_code: str
    retry_safe: bool
    outcome: str = "blocked"
    schema: int = SCHEMA_VERSION


class FailureError(ValueError):
    """A failure block is present in the body but cannot be parsed."""


def render(record: Failure) -> str:
    """Render the single hidden v1 block for one failure.

    Deterministic and compact (`schema` first), so a re-render never
    rewrites the comment and the documented example is the real output.
    A code outside the closed set is a programming error and fails fast
    here instead of writing an unreadable comment.
    """
    _validate(record)
    payload = json.dumps(
        {
            "schema": record.schema,
            "outcome": record.outcome,
            "reason_code": record.reason_code,
            "action_code": record.action_code,
            "retry_safe": record.retry_safe,
        },
        separators=(",", ":"),
    )
    return FAILURE_BLOCK_TEMPLATE.format(payload=payload)


def _validate(record: Failure) -> None:
    if type(record.schema) is not int or record.schema != SCHEMA_VERSION:
        raise FailureError(
            f"failure schema {record.schema!r} != supported {SCHEMA_VERSION}"
        )
    if record.outcome not in OUTCOMES:
        raise FailureError(f"unknown failure outcome: {record.outcome!r}")
    if record.reason_code not in REASON_CODES:
        raise FailureError(f"unknown reason_code: {record.reason_code!r}")
    if record.action_code not in ACTION_CODES:
        raise FailureError(f"unknown action_code: {record.action_code!r}")
    if type(record.retry_safe) is not bool:
        raise FailureError(
            f"retry_safe must be a boolean, got {record.retry_safe!r}"
        )


def parse(body: object) -> Failure | None:
    """Parse the failure block from one comment body.

    Returns `None` when the body carries no failure block at all (an old
    comment, or a comment of another kind) — the reader ignores it
    instead of failing. Raises `FailureError` when a block is present
    but corrupted (not JSON, wrong shape, unknown code, or more than one
    block in one body): a corrupted record is never silently read as
    "no reason".
    """
    if not isinstance(body, str):
        return None
    blocks = _FAILURE_BLOCK_RE.findall(body)
    if len(blocks) > 1:
        raise FailureError(
            "multiple orbi:failure:v1 blocks in one comment body"
        )
    if not blocks:
        return None
    return _parse_block(blocks[0])


_FAILURE_FIELDS = tuple(
    field.name for field in dataclasses.fields(Failure)
)


def _parse_block(payload: str) -> Failure:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FailureError(
            f"orbi:failure:v1 block is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise FailureError(
            "orbi:failure:v1 block payload must be a JSON object"
        )
    unknown = sorted(set(data) - set(_FAILURE_FIELDS))
    if unknown:
        raise FailureError(
            f"orbi:failure:v1 block has unknown fields: {unknown}"
        )
    missing = sorted(name for name in _FAILURE_FIELDS if name not in data)
    if missing:
        raise FailureError(
            f"orbi:failure:v1 block is missing fields: {missing}"
        )
    record = Failure(
        reason_code=data["reason_code"],
        action_code=data["action_code"],
        retry_safe=data["retry_safe"],
        outcome=data["outcome"],
        schema=data["schema"],
    )
    _validate(record)
    return record


# --------------------------------------------------------------------------
# The classifier: exception detail -> closed reason/action codes
# --------------------------------------------------------------------------

# Failure-reason markers. The classifier reads only the bounded
# exception detail; the caller (the runner, which owns the exception
# classes) passes the type-derived flags that win over these patterns, so
# a code is never derived from a guess.
_PROVIDER_QUOTA_RE = re.compile(
    r"\b429\b|quota|resource_exhausted|retry in|usage limit has been reached",
    re.IGNORECASE,
)
# The word markers of a provider authentication failure. `pi_process`'s
# early-exit classifier (`_classify_startup_exit`) matches this SAME
# tuple (plus the 401/403 status codes), so the two classifications
# cannot drift into two lists (Issue #1465).
PROVIDER_AUTH_WORDS = ("unauthorized", "forbidden", "api key")

_CREDENTIAL_MISSING_RE = re.compile(
    r"bad credentials"
    r"|(?:http|status)\s+401\b"
    # A bare `401` only counts in provider-error shape (`401: {...}`): a
    # pytest output line like `test_401_handling ... FAILED` must not be
    # relabelled as a credential problem (Issue #1465).
    r"|(?:^|[\s\"'(])401:"
    r"|authentication (?:failed|required|fails)"
    r"|incorrect api key"
    r"|" + "|".join(re.escape(word) for word in PROVIDER_AUTH_WORDS)
    + r"|credential",
    re.IGNORECASE,
)
# The explicit GitHub rejection when a push creates or updates an
# Actions workflow without the App's `workflows` permission (Issue
# #1651). GitHub names the workflow path and the missing permission in
# ONE sentence, so the pattern requires the whole refusal: a generic
# 403, a non-fast-forward rejection or the bare word `workflow` never
# matches.
_WORKFLOW_PERMISSION_RE = re.compile(
    r"refusing to allow (?:a|the) GitHub App to "
    r"(?:create or update|update) workflow "
    r"[`'\"]?[^`'\"\s]+[`'\"]? without "
    r"[`'\"]?workflows[`'\"]? permission",
    re.IGNORECASE,
)

# The reader-facing cause and repair path for that refusal (Issue
# #1651). One canonical explanation feeds the detailed comment, the
# terminal progress scene and the short milestone; only the last
# sentence differs by whether a delivery PR already exists.
_WORKFLOW_PERMISSION_CAUSE_KEY = "failure.workflow_cause"
_TESTS_FAILED_RE = re.compile(
    r"\btests? failed\b|\d+ failed", re.IGNORECASE,
)
# Git's answer to `git fetch origin <branch>` when the remote has no such
# branch (Issue #1749): the two wordings real Git emits, and nothing else —
# a generic fetch failure keeps its existing classification.
_BASE_BRANCH_MISSING_RE = re.compile(
    r"could ?n['o]t find remote ref", re.IGNORECASE,
)
_GITHUB_TRANSIENT_RE = re.compile(
    r"http 5\d\d|http 429|rate limit|"
    r"something went wrong while executing your query|"
    r"internal server error|connection reset|connection refused|"
    r"temporary failure in name resolution|network is unreachable|"
    r"timed out|timeout",
    re.IGNORECASE,
)

# reason_code -> (action_code, retry_safe). `retry_safe` is whether a
# plain re-queue can succeed WITHOUT a human repair: a transient GitHub
# failure or a quota window can, a test failure or a missing credential
# cannot. The sets are closed and pinned to docs/workflow.mdx by tests.
DISPOSITIONS: dict[str, tuple[str, bool]] = {
    "github_transient": ("requeue", True),
    "tests_failed": ("fix_ticket", False),
    "review_budget_exhausted": ("decide", False),
    "human_decision_required": ("decide", False),
    "credential_missing": ("check_credentials", False),
    "provider_quota": ("wait_quota", True),
    "base_branch_missing": ("fix_ticket", False),
    "unclassified": ("fix_ticket", False),
}


def classify(detail: str, *, outcome: str = "blocked",
             provider_quota: bool = False, review_budget: bool = False,
             human_decision: bool = False,
             unrecoverable: bool = False) -> Failure:
    """Map one delivery exception to the machine-readable failure record.

    The caller supplies the type-derived flags (the runner owns the
    exception classes); an explicit type wins over the message markers,
    and every marker that is not recognized falls through to
    `unclassified`, so the block is never omitted. The mapping is the
    single source both the comment block and the reader-facing
    Action/Reason rendering use.
    """
    if provider_quota:
        reason_code = "provider_quota"
    elif review_budget:
        reason_code = "review_budget_exhausted"
    elif human_decision:
        reason_code = "human_decision_required"
    elif _WORKFLOW_PERMISSION_RE.search(detail):
        # The explicit App/workflows refusal is an authorization the AI
        # cannot grant: the existing `credential_missing` /
        # `check_credentials` category, a repair required before retry.
        reason_code = "credential_missing"
    elif _BASE_BRANCH_MISSING_RE.search(detail):
        reason_code = "base_branch_missing"
    elif _CREDENTIAL_MISSING_RE.search(detail):
        reason_code = "credential_missing"
    elif _TESTS_FAILED_RE.search(detail):
        reason_code = "tests_failed"
    elif _GITHUB_TRANSIENT_RE.search(detail):
        reason_code = "github_transient"
    elif _PROVIDER_QUOTA_RE.search(detail):
        reason_code = "provider_quota"
    elif unrecoverable:
        reason_code = "human_decision_required"
    else:
        reason_code = "unclassified"
    action_code, retry_safe = DISPOSITIONS[reason_code]
    return Failure(
        reason_code=reason_code,
        action_code=action_code,
        retry_safe=retry_safe,
        outcome=outcome,
    )


def workflow_permission_refused(detail: str) -> bool:
    """True only for the explicit GitHub App Actions-workflow refusal.

    The reporter uses this to give that one authorization rejection its
    own terminal cause and repair path (Issue #1651); every other push
    failure keeps its existing classification.
    """
    return (
        isinstance(detail, str)
        and _WORKFLOW_PERMISSION_RE.search(detail) is not None
    )


def workflow_permission_cause(
    language_code: str,
) -> str:
    """The one-sentence cause of an Actions-workflow permission refusal."""
    return language.text(language_code, _WORKFLOW_PERMISSION_CAUSE_KEY)


def workflow_permission_action(
    pr_url: str | None = None, *,
    language_code: str,
) -> str:
    """The concrete human repair path for the workflow refusal.

    The no-PR wording restarts the Issue (`ai-ready`); the existing-PR
    wording continues the SAME PR (`ai-fix-needed`). Both first remove
    the terminal `ai-blocked` label and name the App operator only when
    the reader cannot approve the permission update. The refusal is an
    external authorization, so no wording promises an automatic restart
    and none asks the reader to share a token or reinstall the App.
    """
    cause = workflow_permission_cause(language_code)
    if pr_url:
        return language.text(
            language_code, "failure.workflow_action_pr",
            cause=cause, pr_url=pr_url,
        )
    return language.text(
        language_code, "failure.workflow_action_no_pr", cause=cause,
    )


def terminal_action(record: Failure,
                    language_code: str, *,
                    base_branch: str | None = None) -> str:
    """The reader action for the implement-phase terminal handler.

    An unrecognized failure says the cause could not be determined
    instead of implying the Issue text is the problem (Issue #1651); a
    missing base branch names the branch and both repairs — create it, or
    pick an existing one where the repository was set up — plus how to
    restart the Issue (Issue #1749, #1753); every other recognized
    terminal failure keeps the existing generic action.
    """
    if record.reason_code == "unclassified":
        return language.text(language_code, "failure.terminal_unclassified")
    if record.reason_code == "base_branch_missing":
        return language.text(
            language_code, "failure.terminal_base_branch_missing",
            base_branch=base_branch or "-",
        )
    return language.text(language_code, "failure.terminal_generic")


def terminal_reason(detail: str, record: Failure, *,
                    base_branch: str | None, language_code: str) -> str:
    """The reader-facing reason of the implement-phase terminal handler.

    The missing-base-branch failure names the CONFIGURED branch and keeps
    Git's own `couldn't find remote ref <value>` line out of the sentence:
    that line is the evidence the run details carry, never the setting a
    human has to repair (Issue #1749, #1753). Every other terminal failure
    keeps the generic stopped-delivery sentence.
    """
    if record.reason_code == "base_branch_missing":
        return language.text(
            language_code, "failure.base_branch_missing_reason",
            base_branch=base_branch or "-",
        )
    return language.text(
        language_code, "runner.delivery_stopped_reason", detail=detail,
    )


# --------------------------------------------------------------------------
# The comment: bounded detail + the reader-facing hierarchy
# --------------------------------------------------------------------------

def _failure_detail(exc: BaseException) -> str:
    """One-line failure description; keeps bounded subprocess stderr visible."""
    detail = str(exc)
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip() and stderr.strip() not in detail:
        detail = f"{detail} stderr={stderr.strip()[:1000]}"
    return detail


_LOCAL_PATH_RE = re.compile(
    r"(?:/home/[^\s`),]+|/Users/[^\s`),]+|/tmp/[^\s`),]+|"
    r"/workspace/[^\s`),]+|/workspaces/[^\s`),]+)"
)


def _redact_local_paths(value: str) -> str:
    """Keep GitHub comments free of host-specific filesystem paths."""
    return _LOCAL_PATH_RE.sub("local runner path", value)


def _workflow_permission_line(text: str) -> str | None:
    """The GitHub Actions-workflow refusal line inside bounded stderr.

    The decisive line sits immediately BEFORE Git's generic
    `error: failed to push some refs`, so the last-line rule would drop
    it (Issue #1651). Only the explicit App/workflows refusal sentence
    matches; a non-fast-forward rejection or a bare `workflow` mention
    does not.
    """
    for line in text.splitlines():
        if _WORKFLOW_PERMISSION_RE.search(line):
            return line.strip()
    return None


def _failure_summary(reason: str,
                     language_code: str) -> str:
    """Turn a runner exception description into a short reader summary."""
    command_failed = language.text(language_code, "failure.command_failed")
    stderr_match = re.search(r"\s+stderr=(.*)$", reason,
                             flags=re.IGNORECASE | re.DOTALL)
    stderr = stderr_match.group(1).strip() if stderr_match else ""
    summary = re.sub(r"\s+stderr=.*$", "", reason,
                     flags=re.IGNORECASE | re.DOTALL)
    # A CalledProcessError renders the entire argv between ``Command`` and
    # ``returned``. Match that semantic boundary rather than the first closing
    # bracket: an argv value may itself contain ``]``.
    summary = re.sub(
        r"Command\s+.+?\s+returned non-zero exit status\s+\d+",
        command_failed, summary,
        flags=re.IGNORECASE | re.DOTALL,
    )
    summary = re.sub(r"CalledProcessError\([^)]*\)",
                     command_failed, summary)
    summary = _redact_local_paths(summary)
    # Trailing dots are kept: a catalog sentence ("... start from.")
    # reaches the reader as written (Issue #1753), while a trailing
    # separator from exception text is noise.
    summary = re.sub(r"\s+", " ", summary).strip(" ;:")
    if not summary.strip("."):
        summary = command_failed
    if stderr:
        # Setup tools often write informational lines before the provider's
        # concrete error. The final non-empty line is the actionable result
        # — EXCEPT the Actions-workflow refusal, which GitHub prints one
        # line above Git's generic `failed to push some refs` and which
        # the classifier recognizes (Issue #1651).
        refusal_line = _workflow_permission_line(stderr)
        stderr_summary = _redact_local_paths(
            refusal_line if refusal_line
            else stderr.rstrip().rsplit("\n", 1)[-1].strip()
        )
        if stderr_summary and stderr_summary not in summary:
            # The sentence separator never doubles the punctuation the
            # summary already ends with.
            summary = f"{summary.rstrip(' .;:')}: {stderr_summary}"
    return summary[:500]


def _failure_comment_body(*, outcome: str, action: str, reason: str,
                         diagnosis: str, scene: str, evidence: str,
                         pr_url: str | None, issue: str, run_id: str,
                         failure_record: Failure | None = None,
                         language_code: str,
                         ) -> str:
    """Build the reader-facing hierarchy shared by classified failures.

    The hidden `orbi:failure:v1` block is the first line so every
    blocked/fix-needed comment is machine-readable; the readable
    headline, **Action** and **Reason** stay beside it and the bounded
    raw detail stays inside the one Diagnosis `<details>` block
    (`evidence`).
    """
    retrying = outcome == "fix needed"
    if failure_record is None:
        failure_record = Failure(
            reason_code="unclassified", action_code="fix_ticket",
            retry_safe=False,
            outcome="fix_needed" if retrying else "blocked",
        )
    def message(key: str, **fields: object) -> str:
        return language.text(language_code, key, **fields)

    headline = message(
        "failure.headline_fix_needed" if retrying
        else "failure.headline_blocked",
    )
    parts = [render(failure_record), headline]
    if pr_url:
        parts.append(message("failure.pr_line", pr_url=pr_url))
    parts.append(message("failure.issue_line", issue=issue, run_id=run_id))
    if action:
        parts.append(
            f"{message('failure.action_label')} {_redact_local_paths(action)}"
        )
    parts.append(
        f"{message('failure.reason_label')} "
        f"{_failure_summary(reason, language_code)}"
    )
    disposition = message(
        "failure.disposition_needs_fix" if retrying
        else "failure.disposition_failed",
    )
    # The scene's human field labels follow the same language; the
    # correlation row reads the two values through the SAME labels, so the
    # machine lookup shape survives translation.
    branch_match = re.search(
        rf"- {re.escape(message('failure.scene.branch'))}: `([^`]+)`",
        scene,
    )
    session_match = re.search(
        rf"- {re.escape(message('failure.scene.session'))}: `([^`]+)`",
        scene,
    )
    correlation = (
        f"run={run_id} branch={branch_match.group(1) if branch_match else '-'} "
        f"session={session_match.group(1) if session_match else '-'}"
    )
    details = [
        f"<details><summary>{message('failure.diagnosis_summary')}</summary>",
        message("failure.disposition_row", disposition=disposition),
        scene,
        # Stable correlation keys retain the old journal lookup shape while
        # local path fields above remain reader-safe labels.
        message("failure.correlation_row", correlation=correlation),
    ]
    if diagnosis:
        reason_summary = _failure_summary(reason, language_code)
        diagnosis_summary = _failure_summary(diagnosis, language_code)
        if diagnosis_summary == reason_summary or diagnosis_summary in reason_summary:
            details.append(message(
                "failure.detail_row_same", disposition=disposition,
            ))
        else:
            details.append(message(
                "failure.detail_row", detail=diagnosis_summary,
            ))
    if evidence:
        details.append(evidence.lstrip())
    details.append("</details>")
    # The blocked diagnostic's community help line is NOT added here: the
    # ONE blocked exit appends it to every blocked comment (Issue #1730),
    # so this renderer and that exit can never both add it.
    return "\n\n".join(parts + ["\n".join(details)])
