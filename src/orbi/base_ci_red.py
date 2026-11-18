"""The base-branch-red scene (Issue #1825).

A delivery PR can be perfectly ready — review passed, tests pass — and
still be unable to merge, because the check it fails is ALSO failing on
the configured base branch: the PR did not introduce it and no review
round can repair it. Before this module the delivery ended `ai-blocked`
with `human_decision_required`, one line of prose and no way back
without a human relabel.

This module owns that one scene:

* the machine-readable record (`orbi:base-ci-red:v1`) — state, base
  branch, base sha, PR number and every failing check name, rendered and
  parsed with the same deterministic contract as `orbi:failure:v1`;
* the evidence read (`failing_checks`): every failed check on the base
  commit with its run link and first error lines, from the ONE
  `check-runs` response — no annotations/logs API;
* the ONE readable comment (`comment_body`): what the user already has,
  why it paused, the evidence, the fix Issues Orbi opened itself and the
  remaining repair — and no empty evidence section (this is not a
  subprocess failure);
* the in-place edits of that comment (`rewrite_state`, `remind`,
  `note_merged`) and the triage-Issue lookup (`open_triage_issue`);
* the eligibility rule the command and the scan share (`paused_scene`).

The ACTION side lives beside it: `base_ci_fix` opens (or reuses) the ONE
open `ai-ready` + `p0` fix Issue per failing base check — by itself when
the scene is recorded (Issue #2203) and on the `/orbi fix-base` command —
and runs the recovery scan (`recover`) that takes `ai-blocked` back to
`ai-fix-needed` once the base turns green, then `stale` after
`REMINDER_AFTER_DAYS` days.

No new label, no new state, no new action code: the failure rides the
existing `fix_ticket` action on `ai-blocked`, and the recovery reuses
the resumable `ai-fix-needed` path. It is deliberately NOT a daemon or a
queue: one bounded scan per tick, driven by the GitHub Issue's own
comments.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from orbi import comment_trust, failure, language, user_summary
from orbi.comment_kind import (
    COMMENT_KIND_FAILURE, comment_kind, parse_versioned_block,
    versioned_block_pattern,
)
from orbi.comment_rest_id import rest_comment_id
from orbi.failure import redact_local_paths
from orbi.github import (
    comment_issue, commit_check_runs, issue_comments, list_issues,
    update_issue_comment,
)
from orbi.journal import LOGGER, event
from orbi.progress import fenced

SCHEMA_VERSION = 1

# The block's lifecycle. `waiting` is the only state the recovery scan
# picks up; `resuming` and `merged` are the two automatic successes, and
# `stale` is the bounded give-up after the reminder window. A closed set:
# an unknown state in a block is corruption, never a guess.
STATE_WAITING = "waiting"
STATE_RESUMING = "resuming"
STATE_MERGED = "merged"
STATE_STALE = "stale"
STATES = frozenset({STATE_WAITING, STATE_RESUMING, STATE_MERGED, STATE_STALE})

# The failure reason a recovery may act on: only a delivery that paused
# because the BASE was red is recoverable by a green base (Issue #1825).
REASON_BASE_CI_RED = "base_ci_red"

# The one hidden block, next to `orbi:failure:v1` in the same comment. Its
# envelope (single block, JSON object, field diff) is the shared
# `comment_kind.parse_versioned_block`; this module keeps the fields.
_MARKER = "orbi:base-ci-red:v1"
BLOCK_TEMPLATE = f"<!-- {_MARKER} {{payload}} -->"

# How many failing checks' error lines one comment shows, and how many
# lines per check: bounded evidence, never a log dump.
MAX_ERROR_LINES = 5

# The bounded wait: still red after this window, the scan posts ONE
# reminder and stops polling (a human relabel still recovers the Issue).
REMINDER_AFTER_DAYS = 7
REMINDER_AFTER_SECONDS = REMINDER_AFTER_DAYS * 24 * 3600

# The engine-initiated run's own once-per-scene record (Issue #2203):
# `/orbi fix-base` keys its receipt on the command comment it answers, and
# the engine has no such comment — the SCENE is the occurrence, so the key
# is the base-CI-red record itself (the block every scene comment carries).
SCENE_RECEIPT = "orbi-base-ci-red-fix-issues"

# GitHub's non-passing check-run conclusions (the same set the merge gate's
# rollup classifier treats as passed). Public: the base head's verdict is
# re-read in the fix module (Issue #2203).
PASSING_CONCLUSIONS = ("success", "neutral", "skipped")

# The issue search that finds a triage Issue: one bounded page is enough
# for a check name and branch (the title is unique per pair).
_TRIAGE_SEARCH_LIMIT = 20


class BaseCIRedError(ValueError):
    """A `orbi:base-ci-red:v1` block is present but cannot be parsed."""


@dataclasses.dataclass(frozen=True)
class BaseCIRed:
    """One base-branch-red record, exactly as the v1 block holds it.

    `schema` mirrors the marker version, `pr` is the paused delivery PR,
    and `failing_checks` carries the check NAMES the recovery scan
    re-reads on the current base head.
    """

    state: str
    base_branch: str
    base_sha: str
    pr: int
    failing_checks: tuple[str, ...]
    schema: int = SCHEMA_VERSION


@dataclasses.dataclass(frozen=True)
class FailingCheck:
    """One failed base check: its name, run link and first error lines.

    `url` is the check run's `html_url` (GitHub's own run link) and
    `errors` the first bounded lines of the run's `output`; both are
    omitted when GitHub does not provide them.
    """

    name: str
    url: str | None = None
    errors: tuple[str, ...] = ()


_FIELDS = tuple(field.name for field in dataclasses.fields(BaseCIRed))


def _validate(record: BaseCIRed) -> None:
    if type(record.schema) is not int or record.schema != SCHEMA_VERSION:
        raise BaseCIRedError(
            f"base_ci_red schema {record.schema!r} != supported {SCHEMA_VERSION}"
        )
    if record.state not in STATES:
        raise BaseCIRedError(f"unknown base_ci_red state: {record.state!r}")
    if type(record.pr) is not int or isinstance(record.pr, bool):
        raise BaseCIRedError(f"base_ci_red pr must be an int: {record.pr!r}")
    for name in ("base_branch", "base_sha"):
        if not isinstance(getattr(record, name), str) or not getattr(
                record, name):
            raise BaseCIRedError(f"base_ci_red {name} must be a string")
    if not isinstance(record.failing_checks, tuple) or not all(
            isinstance(name, str) and name for name in record.failing_checks):
        raise BaseCIRedError(
            "base_ci_red failing_checks must be a tuple of check names"
        )


def _serialize(payload: dict) -> str:
    """One block payload: compact JSON with the HTML delimiters escaped.

    The block is an HTML comment and the record carries GitHub's own
    text: a check name may hold the comment terminator (a workflow named
    after a shell pipeline) and the base branch is a user string.
    Unescaped, a terminator closes the comment early, the next tick's
    `parse` reads a truncated payload as corruption, and `paused_scene`
    abandons the recovery (Issue #1998). The JSON \\u003c / \\u003e escapes
    decode back to the exact same value, exactly the way the progress
    block serializes its summary (`progress_summary.serialize_summary`).
    """
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e")


def render(record: BaseCIRed) -> str:
    """Render the single hidden block for one record (deterministic)."""
    _validate(record)
    payload = _serialize(
        {
            "schema": record.schema,
            "state": record.state,
            "base_branch": record.base_branch,
            "base_sha": record.base_sha,
            "pr": record.pr,
            "failing_checks": list(record.failing_checks),
        },
    )
    return BLOCK_TEMPLATE.format(payload=payload)


def parse(body: object) -> BaseCIRed | None:
    """Parse the block from one comment body (None when there is none).

    Raises `BaseCIRedError` when a block is present but corrupted — a
    corrupted record is never silently read as "no record".
    """
    data = parse_versioned_block(body, _MARKER, _FIELDS, BaseCIRedError)
    if data is None:
        return None
    checks = data["failing_checks"]
    if not isinstance(checks, list):
        raise BaseCIRedError(
            "orbi:base-ci-red:v1 failing_checks must be an array"
        )
    record = BaseCIRed(
        state=data["state"], base_branch=data["base_branch"],
        base_sha=data["base_sha"], pr=data["pr"],
        failing_checks=tuple(checks), schema=data["schema"],
    )
    _validate(record)
    return record


def with_state(record: BaseCIRed, state: str) -> BaseCIRed:
    """The same record in another lifecycle state."""
    return dataclasses.replace(record, state=state)


def scene_receipt(record: BaseCIRed) -> str:
    """The hidden once-per-scene receipt of the engine-initiated run.

    Keyed on the record itself, never on a comment id: the record IS the
    scene's identity and it is decided before the pause comment exists.
    Only a runner-authored comment carrying it counts (`runner_receipt`),
    so a copied marker opens nothing.
    """
    digest = hashlib.sha256(render(record).encode("utf-8")).hexdigest()[:16]
    return f"<!-- {SCENE_RECEIPT} scene={digest} -->"


# --------------------------------------------------------------------------
# The base evidence: one check-runs read, every failed check
# --------------------------------------------------------------------------

def _error_lines(run: dict) -> tuple[str, ...]:
    """The first bounded error lines of one failed check run.

    GitHub's `output` object is the only source (no annotations/logs
    API): `summary` first, then `text`. Missing output is an empty
    tuple — the comment omits the segment instead of printing noise.
    """
    output = run.get("output")
    if not isinstance(output, dict):
        return ()
    # `text` is the detailed output a developer debugs with; `summary` is
    # the short headline and is read only when there is no text (reading
    # both would print the headline in front of the real error).
    for field in ("text", "summary"):
        value = output.get(field)
        if isinstance(value, str) and value.strip():
            return tuple(
                redact_local_paths(line.rstrip())
                for line in value.splitlines()[:MAX_ERROR_LINES]
            )
    return ()


def failing_checks(repo: str, failed_names: list[str],
                   base_commit: str) -> list[FailingCheck]:
    """Every failed check on `base_commit` among `failed_names`.

    One `commit_check_runs` read; the order follows `failed_names` so the
    comment lists the checks in the order the merge gate saw them.
    """
    runs = commit_check_runs(repo, base_commit)
    by_name: dict[str, dict] = {}
    for run in runs:
        if run.get("status") != "completed":
            continue
        if run.get("conclusion") in PASSING_CONCLUSIONS:
            continue
        by_name.setdefault(str(run.get("name")), run)
    checks: list[FailingCheck] = []
    for name in failed_names:
        run = by_name.get(name)
        if run is None:
            continue
        url = run.get("html_url")
        checks.append(FailingCheck(
            name=name,
            url=url if isinstance(url, str) and url else None,
            errors=_error_lines(run),
        ))
    return checks


def detect(repo: str, failed_names: list[str], base_commit: str | None,
           *, base_branch: str, pr: int) -> tuple[
               BaseCIRed, list[FailingCheck]] | None:
    """The record and evidence when the base shares a failed check.

    `None` when no base commit is known or the base is green on every
    failed name — the caller then keeps its ordinary CI-failure path.
    """
    if not base_commit:
        return None
    checks = failing_checks(repo, failed_names, base_commit)
    if not checks:
        return None
    return (
        BaseCIRed(
            state=STATE_WAITING, base_branch=base_branch,
            base_sha=base_commit, pr=pr,
            failing_checks=tuple(check.name for check in checks),
        ),
        checks,
    )


def repr_name(record: BaseCIRed) -> str:
    """The reader-facing check list of one record (`a`, `b`)."""
    return ", ".join(f"**`{name}`**" for name in record.failing_checks)


# --------------------------------------------------------------------------
# The fix Issue: ONE open Issue per base branch (Issue #2259)
# --------------------------------------------------------------------------

def triage_title(base_branch: str) -> str:
    """The one title format shared by the lookup and the create.

    Keyed on the BASE BRANCH, never on a check (Issue #2259): the fix
    Issue's PR has to turn every red check green, because a PR that
    repairs only its own check stays red on the others and is paused as
    "the base is already red" — so per-check Issues could never merge. The
    CI triage workflow's per-check `CI failure: <check> on branch <branch>`
    titles stay its own record and are not reused here.
    """
    return f"CI failure on branch {base_branch}"


def open_triage_issue(repo: str, base_branch: str) -> dict | None:
    """The OPEN fix Issue of this base branch, or None.

    `state="open"` on purpose: a CLOSED Issue is done and must never be
    reused as the tracker of a new red base. A failed read PROPAGATES: a
    lookup that silently read as "none" would make `/orbi fix-base`
    open a SECOND fix Issue beside the one it could not see.
    """
    prefix = triage_title(base_branch)
    issues = list_issues(
        repo, state="open", search=f'"{prefix}" in:title',
        json_fields="number,url,title", limit=_TRIAGE_SEARCH_LIMIT,
    )
    for issue in issues:
        if str(issue.get("title", "")).startswith(prefix):
            return issue
    return None


def triage_url(repo: str, base_branch: str) -> str | None:
    """The existing OPEN fix Issue's URL (best effort, never fails).

    The failure comment is a bypass: a failed lookup drops the link, it
    never changes the delivery's own outcome.
    """
    try:
        issue = open_triage_issue(repo, base_branch)
    except Exception:
        LOGGER.exception(
            "delivery_ci_triage_lookup_failed repo=%s base_branch=%s",
            repo, base_branch,
        )
        return None
    url = issue.get("url") if issue else None
    return url if isinstance(url, str) and url else None


# --------------------------------------------------------------------------
# The comment: what the user reads
# --------------------------------------------------------------------------

def state_line(record: BaseCIRed, language_code: str) -> str:
    """The timeline line that marks one non-waiting state."""
    if record.state == STATE_RESUMING:
        return language.text(
            language_code, "base_ci_red.state_resuming",
            base_branch=record.base_branch,
        )
    if record.state == STATE_MERGED:
        return language.text(
            language_code, "base_ci_red.state_merged",
            base_branch=record.base_branch,
        )
    return language.text(
        language_code, "base_ci_red.state_stale",
        base_branch=record.base_branch, checks=repr_name(record),
        days=REMINDER_AFTER_DAYS,
    )


def comment_body(record: BaseCIRed, checks: list[FailingCheck], *,
                 failure_record: failure.Failure,
                 language_code: str,
                 issue_number: int | None = None,
                 issue_title: str | None = None,
                 status_url: str | None = None,
                 fix_issues: list[str] | None = None) -> str:
    """The ONE readable base-branch-red comment.

    Order is the user's: what they already have (a ready PR), why it
    paused, the evidence, the fix Issues, the remaining repairs. The hidden
    `orbi:failure:v1` block leads (the resume path reads it there) and
    the `orbi:base-ci-red:v1` block closes the body. An empty evidence
    segment is never rendered: this failure is not a subprocess exit. The
    task identity and the optional project status link every task comment
    carries (Issue #1826) sit right below the ready line.

    `fix_issues` is what the engine-initiated run (Issue #2203) opened or
    reused: those lines are shown, the second repair becomes "wait for
    them" instead of "reply `/orbi fix-base`", and the scene receipt that
    makes the run once per scene closes the body. `None` (the run failed,
    or a caller carries no run) keeps the pre-#2203 comment.
    """
    parts = [
        failure.render(failure_record),
        language.text(language_code, "base_ci_red.ready", pr=record.pr),
    ]
    parts.extend(user_summary.summary_lines(
        status_url=status_url, issue=issue_number, title=issue_title,
        language_code=language_code,
    ))
    paused = language.text(
        language_code, "base_ci_red.paused",
        base_branch=record.base_branch, checks=repr_name(record),
    )
    parts.append(paused)
    # The evidence is ONE tight list: every failed check with its run link,
    # then its first error lines. A segment GitHub does not provide is
    # omitted, never printed empty.
    evidence: list[str] = []
    for check in checks:
        if check.url:
            evidence.append(language.text(
                language_code, "base_ci_red.check_run",
                name=check.name, url=check.url,
            ))
        if check.errors:
            evidence.append(
                language.text(language_code, "base_ci_red.check_errors",
                              name=check.name)
                + "\n" + fenced("\n".join(check.errors))
            )
    if evidence:
        parts.append("\n".join(evidence))
    if fix_issues:
        parts.append(
            language.text(language_code, "base_ci_red.fix_issues")
            + "\n" + "\n".join(f"- {line}" for line in fix_issues)
        )
    parts.extend(_next_steps(record, language_code,
                             automatic=bool(fix_issues)))
    parts.append(render(record))
    if fix_issues:
        parts.append(scene_receipt(record))
    return "\n\n".join(parts)


def _next_steps(record: BaseCIRed, language_code: str, *,
                automatic: bool = False) -> list[str]:
    """The repairs offered while the delivery waits.

    `automatic` is the #2203 variant: the engine already opened the fix
    Issues, so the comment names them under `base_ci_red.fix_issues` and
    offers only the manual repair — it never asks for `/orbi fix-base`,
    which the command's own reply still answers for the scene where that
    engine run failed.
    """
    steps = [
        language.text(language_code, "base_ci_red.next_steps"),
        language.text(
            language_code, "base_ci_red.next_step_fix_base",
            base_branch=record.base_branch,
        ),
    ]
    if not automatic:
        steps.append(
            language.text(language_code, "base_ci_red.next_step_command"),
        )
    return steps


def rewrite_state(body: str, state: str, *, language_code: str) -> str:
    """Re-render one comment body in another state, in place.

    The state line is APPENDED above the block, so the comment keeps a
    short timeline (paused -> green again -> merged) instead of
    rewriting history; the block itself is replaced, never duplicated.

    The block is located by its marker ENVELOPE, never by the exact text
    today's `render` produces: a comment written before #1998 escaped the
    HTML delimiters carries a raw `<`/`>` (a check name such as
    `tests (>=3.10)`) and still parses, so a text-exact replace would
    silently no-op and leave it `waiting` for good.
    """
    record = parse(body)
    if record is None:
        raise BaseCIRedError("cannot rewrite a comment without the block")
    updated = with_state(record, state)
    line = state_line(updated, language_code)
    lines = body.splitlines()
    if state == STATE_MERGED:
        # The delivery landed: the repairs the waiting comment offered are
        # no longer next steps, so the merged comment does not repeat them.
        # Both variants: the comment carries whichever repair the run that
        # wrote it offered, and `record` alone cannot say which.
        offered = {
            text.strip()
            for automatic in (True, False)
            for text in _next_steps(record, language_code,
                                    automatic=automatic)
        }
        # The removed section leaves its separator lines behind: keep one
        # blank line, never a run of them.
        kept: list[str] = []
        for text in lines:
            if text.strip() not in offered and (
                    text.strip() or not kept or kept[-1].strip()):
                kept.append(text)
        lines = kept
    marker_index = next(
        (index for index, text in enumerate(lines)
         if BLOCK_TEMPLATE.split("{")[0].strip() in text),
        len(lines),
    )
    if line.strip() not in (text.strip() for text in lines[:marker_index]):
        lines[marker_index:marker_index] = [line, ""]
    return versioned_block_pattern(_MARKER).sub(
        lambda _match: render(updated), "\n".join(lines), count=1,
    ).rstrip() + "\n"


# --------------------------------------------------------------------------
# The in-place edit of the one comment
# --------------------------------------------------------------------------

def _located(comments: list[dict]) -> tuple[BaseCIRed, dict] | None:
    """The newest comment carrying a valid block, and its record.

    This is the MERGE-NOTE lookup (`note_merged`), which only rewrites
    the comment after the delivery already landed. The recovery gate
    never acts on it: `_latest_trusted_failure` decides (Issue #1833).
    """
    for comment in reversed(comments):
        try:
            record = parse(comment.get("body"))
        except BaseCIRedError:
            LOGGER.warning(
                "unreadable base_ci_red block url=%s", comment.get("url"),
            )
            continue
        if record is not None:
            return record, comment
    return None


def edit_state(repo: str, issue_number: int, comment: dict,
                state: str, *, language_code: str) -> bool:
    """Patch the ONE comment in place; returns whether it was edited."""
    body = comment.get("body")
    if not isinstance(body, str):
        return False
    comment_id = rest_comment_id(comment)
    if comment_id is None:
        LOGGER.warning(
            "base CI red comment has no REST id issue=%s repo=%s",
            issue_number, repo,
        )
        return False
    update_issue_comment(
        comment_id, repo=repo,
        body=rewrite_state(body, state, language_code=language_code),
    )
    return True


def _latest_trusted_failure(
    comments: list[dict], *, repo: str,
    trusted: list[dict] | None = None,
) -> tuple[failure.Failure | None, dict] | None:
    """The Issue's LATEST trusted failure comment, and its record.

    The recovery gate (Issue #1833): a record only authorizes a step
    while the newest TRUSTED failure on the Issue is the failure that
    carries it. Trust is the existing comment rule (repository write
    permission, or the authenticated runner account): a run marker copied
    by a read-only commenter widens nothing.

    The failure comment is identified by its canonical `failure` kind
    marker, never by the mere presence of an `orbi:failure:v1` block: the
    short milestone comment repeats that block (Issue #1322) and is not
    the failure itself, so reading it as one would pause every recovery.

    The selection STOPS at that comment, whatever its own record says: a
    blocked failure whose record is not `base_ci_red` states no
    `base_ci_red` reason (the scene stops carry `human_decision_required`
    since Issue #2245), a stop written before Issue #2245 carries no block
    at all, and a corrupted block is never read as "no reason" either
    (`failure.parse`), so none of them may fall back to an older record it
    superseded. A `None` record is therefore the newest failure's own
    answer: this stop states no machine-readable reason.

    `trusted` is the caller's already-trusted comment list
    (`orbi.blocked_scan.BlockedReads.trusted`): a scan that shares a tick's
    one comments read passes it, so that read pays one trust read instead
    of one per scan (Issue #2218). Without it the list is trusted here.
    """
    if trusted is None:
        trusted = comment_trust.trusted_comments(comments, repo=repo)
    for comment in reversed(trusted):
        body = comment.get("body")
        if comment_kind(body) != COMMENT_KIND_FAILURE:
            continue
        try:
            return failure.parse(body), comment
        except failure.FailureError:
            LOGGER.warning(
                "unreadable orbi:failure:v1 block url=%s",
                comment.get("url"),
            )
            return None, comment
    return None


def paused_scene(
    comments: list[dict], *, repo: str,
    trusted: list[dict] | None = None,
) -> tuple[BaseCIRed, dict] | None:
    """The `waiting` base-CI-red record this Issue pauses on, and its comment.

    The ONE eligibility rule (Issue #1825's trust gate): the newest
    TRUSTED failure on the Issue must be the base-CI one, and the block in
    that same comment must still be `waiting`. The recovery scan and the
    `/orbi fix-base` command's own context both read it from here — the
    terminal spelling acts on exactly the scene the scan would (Issue
    #1930).

    `trusted` is passed straight through to `_latest_trusted_failure`:
    the tick's readiness scan hands over the trust read it already paid.
    """
    located = _latest_trusted_failure(
        comments, repo=repo, trusted=trusted,
    )
    if located is None:
        return None
    failure_record, comment = located
    if (failure_record is None
            or failure_record.reason_code != REASON_BASE_CI_RED):
        return None
    try:
        record = parse(comment.get("body"))
    except BaseCIRedError:
        LOGGER.warning(
            "unreadable base_ci_red block url=%s", comment.get("url"),
        )
        return None
    if record is None or record.state != STATE_WAITING:
        return None
    return record, comment


def post_merged(issue_number: int, repo: str, body: str, *,
                language_code: str) -> None:
    """Post the merged record AND note the outcome in the paused comment.

    Two surfaces of one landed merge: the delivery's own merged comment,
    and the base-branch-red comment edited in place to `merged`. The
    second is a pure bypass — the merge already landed — so a failed edit
    is a log line, never a second failure.
    """
    comment_issue(issue_number, repo=repo, body=body)
    try:
        note_merged(repo, issue_number, language_code=language_code)
    except Exception:
        LOGGER.exception(
            "base_ci_red_merge_note_failed issue=%s repo=%s",
            issue_number, repo,
        )


def remind(repo: str, issue_number: int, record: BaseCIRed,
            comment: dict, *, language_code: str) -> None:
    """One reminder, then the block goes `stale` and polling stops.

    The order is the same as the blocked exit's: the readable comment
    lands first, the state change second, so a boundary failure never
    leaves a stale block with no reason on the Issue.
    """
    stale = with_state(record, STATE_STALE)
    comment_issue(issue_number, repo=repo,
                  body=state_line(stale, language_code))
    edit_state(repo, issue_number, comment, STATE_STALE,
                language_code=language_code)
    event(
        "base_ci_red_stale", issue=issue_number, pr=record.pr,
        base_branch=record.base_branch,
        days=REMINDER_AFTER_DAYS,
    )


def note_merged(repo: str, issue_number: int, *, language_code: str) -> bool:
    """Mark the paused comment merged after the delivery landed.

    The merge is the delivery's own success (the resume flow merged the
    PR); this only tells the reader the outcome in the SAME comment.
    Returns whether the comment changed.
    """
    comments = issue_comments(issue_number, repo=repo)
    located = _located(comments)
    if located is None:
        return False
    record, comment = located
    if record.state not in (STATE_WAITING, STATE_RESUMING):
        return False
    changed = edit_state(repo, issue_number, comment, STATE_MERGED,
                          language_code=language_code)
    if changed:
        event(
            "base_ci_red_merged", issue=issue_number, pr=record.pr,
            base_branch=record.base_branch,
        )
    return changed
