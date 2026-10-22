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
A failure that knows its reason at the source carries the record itself
(`BaseBranchMissingError`); the reporter uses it as it is.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess

from orbi import gh_response, language, user_summary

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
    # Legacy only (Issue #1759): nothing in production raises it any
    # more. The runner runs no tests of its own, and a red CI check is
    # routed by the `GateCIFailure` type. It stays in the closed set so
    # an already-written comment still parses; `classify` never produces it.
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
    # The delivery check that failed on the PR head is ALSO failing on the
    # configured base branch (Issue #1825): the PR cannot turn it green, so
    # the fix belongs to the base. Not `retry_safe` — a plain requeue would
    # hit the same red base — but recoverable: `base_ci_red.recover` resumes
    # the SAME delivery once the base check turns green, no relabel.
    "base_ci_red",
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

    def with_outcome(self, outcome: str) -> Failure:
        """The same record for another delivery outcome.

        A failure that decided its reason at the source (Issue #1756)
        carries the record already; the caller only supplies the
        outcome, never a re-derivation from the message.
        """
        return dataclasses.replace(self, outcome=outcome)


class FailureError(ValueError):
    """A failure block is present in the body but cannot be parsed."""


class ReleaseGateWaitTimeout(RuntimeError):
    """A release gate spent its bounded wait budget without a decision.

    The release state machine reports this wait as `blocked`: the budget
    is bounded on purpose, so the next step is a human's, never the
    automatic requeue a transient GitHub failure gets (Issue #1760). The
    classification reads THIS type, never the words of the message.
    """


class GitHubPermissionDenied(RuntimeError):
    """A GitHub 403 with quota left: the credential lacks the permission.

    The release gate receives that status from `gh_response` but reports
    it with its own reader-facing sentence (the one naming the missing
    `Checks:read` scope), so the meaning travels on the TYPE: `classify`
    reads the type, never the sentence — which is why the broad
    `credential` word could be dropped from the text markers without
    losing the permission repair (Issue #1760).
    """


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
# The word markers `pi_process`'s early-exit classifier
# (`_classify_startup_exit`) matches, plus the 401/403 status codes: the
# provider's own words stay the Pi startup classification's (Issue #1465),
# and `failure` reads them from the one regex below.
PROVIDER_AUTH_WORDS = ("unauthorized", "forbidden", "api key")

# The text-only credential markers. GitHub failures no longer read this
# one: they carry the response status (`gh_response`), which is exact,
# while `forbidden` and `credential` were broad enough to match a branch
# or check name inside the command's argv (Issue #1760). The remaining
# markers serve the Pi / model-provider detail, which only gives text
# (Issue #1465): its 401 shapes, its rejected-key sentences and
# `unauthorized`; `forbidden` stays in `PROVIDER_AUTH_WORDS` for
# `pi_process`'s own startup classifier, which is not this regex.
# `api key` is the provider's own missing/rejected-key sentence ("No API
# key found for provider …"), the marker `PROVIDER_AUTH_WORDS` still
# lists; only `incorrect api key` survived #1762, so the plain sentence
# regressed to `unclassified` (Issue #1768). An argv word cannot carry
# it — the space keeps it out of a branch, label or check name.
_CREDENTIAL_MISSING_RE = re.compile(
    r"bad credentials"
    r"|(?:http|status)\s+401\b"
    # A bare `401` only counts in provider-error shape (`401: {...}`): a
    # pytest output line like `test_401_handling ... FAILED` must not be
    # relabelled as a credential problem (Issue #1465).
    r"|(?:^|[\s\"'(])401:"
    r"|authentication (?:failed|required|fails)"
    r"|api key"
    r"|unauthorized",
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
# The remaining text-only transient markers: network-level failures and
# GitHub's own server sentences. This set names no timeout: a timeout is
# decided by `subprocess.TimeoutExpired` or, for a source that gives no
# status and no type, by `_TEXT_TIMEOUT_RE` below. A GitHub request that
# received a response reports its status and never reads that wording; a
# status-less gh transport failure is the one exception (Issue #1768).
_GITHUB_TRANSIENT_RE = re.compile(
    r"http 5\d\d|http 429|rate limit|"
    r"something went wrong while executing your query|"
    r"internal server error|connection reset|connection refused|"
    r"temporary failure in name resolution|network is unreachable",
    re.IGNORECASE,
)
# A non-GitHub subprocess — a Pi session, a Git fetch/push — reports a
# timeout in its own words and gives neither a status nor a timeout type:
# there the wording is all that exists (the same text-only carve-out the
# provider failure reasons keep, Issue #1465/#1760).
_TEXT_TIMEOUT_RE = re.compile(r"timed out|timeout", re.IGNORECASE)


def _timeout_is_text_only(error: BaseException | None) -> bool:
    """Whether only the failure's text can say it was a timeout.

    `subprocess.TimeoutExpired` IS a timeout (decided by type) and a
    GitHub request that received a response reports its status, so
    neither reads the wording; a non-gh subprocess (Pi, `git
    fetch`/`git push`) has only its output. gh's own transport can fail
    before any response exists — a TLS handshake timeout exits 1 with no
    status and no `TimeoutExpired` (cli/cli#1761) — so a structured gh
    failure that reports NO status keeps the wording as its only signal
    (Issue #1768). A response status, when present, still wins.
    """
    command = getattr(error, "cmd", None)
    if not isinstance(command, list):
        return False
    if command[:1] != ["gh"]:
        return True
    return (
        isinstance(error, gh_response.GitHubRequestFailed)
        and error.status is None
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
    "base_ci_red": ("fix_ticket", False),
    "unclassified": ("fix_ticket", False),
}


class BaseBranchMissingError(RuntimeError):
    """The configured base branch does not exist on the remote (Issue #1756).

    Raised by the first delivery step `freeze_base` → `fetch_base_ref`
    only after the fetch failed AND the remote itself answered
    `git ls-remote --heads` with nothing: the fact is decided at the
    source, so no consumer has to read Git's error text — Git localizes
    it (`LANG` / `LC_MESSAGES`), and a text match would classify the
    same failure differently on a non-English host.

    `base_branch` is the CONFIGURED branch and `failure_record` is the
    record the failure report uses as it is; the text classifier never
    runs for this failure. `stderr` keeps the failed fetch's own output
    as evidence: `_failure_detail` reads it into the run details, so the
    reader still sees what Git said without it deciding anything
    (Issues #1749, #1753).
    """

    def __init__(self, base_branch: str, *, stderr: str | None = None):
        super().__init__(
            f"base branch {base_branch!r} does not exist on origin",
        )
        self.base_branch = base_branch
        self.stderr = stderr
        action_code, retry_safe = DISPOSITIONS["base_branch_missing"]
        self.failure_record = Failure(
            reason_code="base_branch_missing",
            action_code=action_code,
            retry_safe=retry_safe,
        )


def classify(detail: str, *, outcome: str = "blocked",
             provider_quota: bool = False, review_budget: bool = False,
             human_decision: bool = False,
             unrecoverable: bool = False,
             error: BaseException | None = None) -> Failure:
    """Map one delivery exception to the machine-readable failure record.

    The caller supplies the type-derived flags (the runner owns the
    exception classes) and, when it has one, the failing exception itself
    (`error`). Its TYPE and the HTTP response status it carries decide
    BEFORE every text marker — a 401, a 429/5xx, a rate-limited 403, a
    permission 403, a transport timeout and a bounded gate wait are read
    from the failure itself, never from words that the command's argv
    could also contain (Issue #1760). Every marker that is not recognized
    falls through to `unclassified`, so the block is never omitted. The
    mapping is the single source both the comment block and the
    reader-facing Action/Reason rendering use.
    """
    if provider_quota:
        reason_code = "provider_quota"
    elif review_budget:
        reason_code = "review_budget_exhausted"
    elif human_decision or isinstance(error, ReleaseGateWaitTimeout):
        # A bounded wait that ran out is a human's decision, never a
        # transient failure to requeue (Issue #1760).
        reason_code = "human_decision_required"
    elif isinstance(error, subprocess.TimeoutExpired):
        # A transport timeout is transient by type, whatever it says.
        reason_code = "github_transient"
    elif gh_response.authentication_required(error):
        reason_code = "credential_missing"
    elif gh_response.transient(error):
        reason_code = "github_transient"
    elif gh_response.status_of(error) == 403 or isinstance(
            error, GitHubPermissionDenied):
        # A 403 with quota left is a permission failure: the same repair
        # as a rejected credential. A caller that re-wraps that status in
        # its own sentence (the release gate's missing Checks:read) keeps
        # it on the type.
        reason_code = "credential_missing"
    elif _WORKFLOW_PERMISSION_RE.search(detail):
        # The explicit App/workflows refusal is an authorization the AI
        # cannot grant: the existing `credential_missing` /
        # `check_credentials` category, a repair required before retry.
        reason_code = "credential_missing"
    elif _CREDENTIAL_MISSING_RE.search(detail):
        reason_code = "credential_missing"
    elif _timeout_is_text_only(error) and _TEXT_TIMEOUT_RE.search(detail):
        reason_code = "github_transient"
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
    Git's raw answer out of the sentence: the remote's own `git ls-remote`
    probe is the evidence the run details carry, never the setting a human
    has to repair (Issues #1749, #1753, #1756). Every other terminal
    failure keeps the generic stopped-delivery sentence.
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
                         issue_number: int | None = None,
                         issue_title: str | None = None,
                         status_url: str | None = None,
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
    # The task identity and the optional fixed project status link sit
    # directly under the state headline, before the PR/Issue rows, the
    # required action and the diagnosis fold (Issue #1826). The required
    # action keeps its own `**Action:**` line right below, so it is not
    # repeated here.
    parts.extend(user_summary.summary_lines(
        status_url=status_url, issue=issue_number, title=issue_title,
        language_code=language_code,
    ))
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
