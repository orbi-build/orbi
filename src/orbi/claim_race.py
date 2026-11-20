"""The fresh-claim race between two Orbi executors (Issue #2298).

One Issue can be served by two Orbi deployments under two GitHub accounts —
an Orbi Cloud sandbox and a self-hosted engine are the observed pair — and
both claim it: the ai-in-progress label each reads is written by the other
side only later (self-hosted claimed at 04:31:45, Cloud at 04:31:52, and
Cloud saw no label). Two guards cover the race, in this order:

- claim_window_yield — the label or the stable branch the OTHER side wrote
  before our claim write (the pre-#2298 guard, moved here unchanged);
- yield_to_another_executor — after both sides posted their own started
  comment and before either runs Pi, the started comments themselves
  (other_executor_live_started).

No new label, no new marker and no state store: GitHub's own comment list is
the state both sides read, and both reach the same answer because they read
the same list. A yield is not a failure — no failure comment, no label
change, no orbi#2292 streak — and no staleness timeout is added: a removed
deployment is a human relabel (orbi#2256), while same-host instances stay
governed by the orbi#1319 claim.lock.

The module is a sibling of the claim domain (Article 3): it imports the
journal seam and the comment readers, never orbi.runner.
"""
from __future__ import annotations

from collections.abc import Callable

from orbi import clarify
from orbi.comment_kind import (
    COMMENT_KIND_FAILURE,
    COMMENT_KIND_MERGED,
    COMMENT_KIND_REQUEUED,
    COMMENT_KIND_STARTED,
    comment_kind,
)
from orbi.comment_trust import trusted_comments
from orbi.config import RunnerConfig
from orbi.github import (
    authenticated_github_login,
    has_in_progress_label,
    strip_bot_suffix,
)
from orbi.gitops import stable_branch_exists
from orbi.journal import event

# The kinds that END an account's own started run. Paired by ACCOUNT, never
# by run marker: a failure comment does not always carry the marker (the
# self-hosted failure on paseo-orbi#61 has none), and another account's
# terminal comment must never end this one's run (Cloud's 05:11 failure on
# paseo-orbi#62 landed while the self-hosted run was still reviewing).
_RUN_END_KINDS = frozenset({
    COMMENT_KIND_FAILURE, COMMENT_KIND_MERGED, COMMENT_KIND_REQUEUED,
})


def _with_comment_author(comment: dict) -> dict:
    """The comment carrying GraphQL's "author" (Issue #2298).

    GitHub names one comment author twice: "author.login" in the GraphQL
    read ("gh issue view --json comments") and "user.login" in the REST
    list the progress publisher reads with complete pagination. Every
    comment reader in orbi reads "author", so the REST shape is renamed
    here once and the trust rule and the scan stay shape-agnostic.
    """
    if "author" in comment or not isinstance(comment.get("user"), dict):
        return comment
    return {**comment, "author": comment["user"]}


def _comment_account(comment: dict) -> str:
    """The account that wrote the trusted comment, [bot] suffix normalized.

    Every comment reaching this point passed comment_is_trusted, which
    refuses a comment without an author login: the account is always there.
    """
    return strip_bot_suffix(comment["author"]["login"])


def other_executor_live_started(
    comments: list[dict], *, repo: str,
) -> dict | None:
    """Another executor's started comment that is still live, or None.

    The race is decided from the comment list, and both sides reach the
    same answer because they read the same list:

    - the list ORDER is the timeline (oldest first). createdAt is only
      second-resolution and two starts in one second are common, so it is
      never compared: a same-second start is resolved by position;
    - a started comment is live from its own position until a LATER
      merged/failure/requeued comment by the SAME account ends it; another
      account's terminal comment never ends it;
    - a started comment posted while ANOTHER account already had a live
      started comment is the yielding side of the race and is never live,
      so a yielded start can never block a later claim by either side;
    - the author must pass the trusted-author rule every comment reader
      uses (comment_trust.trusted_comments): the run marker does not
      authenticate an author, so a copied marker from a drive-by account
      can never stall deliveries.

    The surviving live start of an account other than the local one (the
    active gh credential's login) is returned; None means no other
    executor is running this Issue. Our own account is never "another
    executor": a same-account restart is this deployment's own run. A
    re-read that lags our own post (our started comment missing) leaves
    the other executor's live start standing - the order cannot be
    established, so this side yields; with no other live start there is
    nothing to yield to.

    A pure scan over an already-fetched comment list (either GitHub shape).
    """
    live: dict[str, dict] = {}
    for comment in trusted_comments(
            [_with_comment_author(comment) for comment in comments], repo=repo):
        kind = comment_kind(comment.get("body"))
        if kind != COMMENT_KIND_STARTED and kind not in _RUN_END_KINDS:
            continue
        account = _comment_account(comment)
        if kind in _RUN_END_KINDS:
            live.pop(account, None)
        elif not any(account != other for other in live):
            live[account] = comment
        # else: posted after another account's live start - the yielding
        # side, never live itself.
    if not live:
        # Nobody started: there is nothing to compare and no reason to
        # read the local identity at all (the common, uncontended claim).
        return None
    own = strip_bot_suffix(authenticated_github_login())
    return next(
        (comment for account, comment in live.items() if account != own),
        None,
    )


def claim_window_yield(
    issue: dict, facts, config: RunnerConfig, *, source_repo: str,
    run_id: str,
) -> str | None:
    """The delivery kind a yield needs when the prepare window was claimed.

    "claim-yielded" when another instance claimed this Issue inside the
    window, "needs-detail" when the thin-ticket gate stops it, None when
    the claim may proceed. The pickup scan and the in-progress recheck
    both predate freeze_base (a seconds-long network round trip), so a
    label - or the stable branch - appearing inside that window means
    another instance got there first: yield without a label write, a
    comment or anything else that could interrupt the winner's in-flight
    delivery; the next tick's scan picks work up again on its own. The
    predicate keys on the repository's dispatch label (#527), not the
    default constant - a custom-label repository's tickets never carry
    ai-ready, and the guard must guard them too.
    """
    number = int(issue["number"])
    if (facts.in_progress or facts.dispatch_label not in facts.claim_labels
            or facts.takeover_pr is not None):
        return None
    if has_in_progress_label(number, source_repo):
        event("claim_yield", issue=number, reason="in_progress_label")
        return "claim-yielded"
    if not facts.stable_branch_present and stable_branch_exists(
            config.repo_dir, facts.stable_branch,
    ):
        event("claim_yield", issue=number, reason="stable_branch_appeared")
        return "claim-yielded"
    if config.clarify_thin_tickets and not clarify.enforce(
            issue, config, source_repo, run_id):
        return "needs-detail"
    return None


def yield_to_another_executor(
    *, issue: int, source_repo: str, comments: list[dict],
    retire: Callable[[], None],
) -> bool:
    """Yield this round when another executor already started the Issue.

    Runs AFTER this executor posted its own started comment and BEFORE the
    progress comment and Pi: the loser stops here instead of running. It
    keeps the labels, keeps its own started comment, pushes nothing and
    removes only its own local worktree (retire) - the winner's labels and
    branch are left untouched. A yield is not a failure: no failure
    comment, no label change and no orbi#2292 streak. No staleness timeout
    is added - a removed deployment is a human relabel (orbi#2256) - and
    same-host instances stay governed by the orbi#1319 claim.lock.
    """
    if other_executor_live_started(comments, repo=source_repo) is None:
        return False
    event("claim_yield", issue=issue, reason="other_executor_started")
    retire()
    return True
