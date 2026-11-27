"""The stop notice of a delivery whose Issue was closed mid-flight.

A person may close an Issue while its delivery is running; the session's
steering poll then stops it where it is (Issue #2468) and the closeout
posts ONE explanatory comment on the closed Issue, opens no PR and ends the
run as `issue_closed`.

What that comment may claim depends on what the stop left behind, and the
delivery's own commit boundary already decides it: only a HEAD past the
frozen base was pushed, so only then does a branch exist that keeps the
work. A session stopped before it committed pushed no branch at all, and
naming one sends the reader looking for a ref that does not exist
(Issue #2475).

This is the leaf that owns both renderings and the choice between them: it
imports nothing, because `language.MESSAGES` merges `CLOSED_ISSUE_MESSAGES`
so `language.text` stays the ONE reading point, and a `from orbi import
language` here would close the import cycle that merge creates
(`tests/test_import_graph.py`). The closeout assembles the comment with it.
"""
from __future__ import annotations

CLOSED_ISSUE_MESSAGES: dict[str, tuple[str, str]] = {
    # The stop after a commit: the delivery branch was pushed and keeps the
    # work for the person who reads the comment.
    "runner.issue_closed": (
        "Orbi delivery stopped: the Issue was closed while the delivery "
        "was in flight; the completed work stays on branch \u0060{branch}\u0060 "
        "and no PR was opened.\nrun_id={run_id}",
        "Orbi 交付已停止：交付进行中该 Issue 被关闭；已完成的工作保留在分支 "
        "\u0060{branch}\u0060 上，没有创建 PR。\nrun_id={run_id}",
    ),
    # The same stop before any commit: nothing was pushed, so no branch is
    # named and the comment says plainly that nothing was kept.
    "runner.issue_closed_nothing_committed": (
        "Orbi delivery stopped: the Issue was closed while the delivery "
        "was in flight; nothing had been committed, so nothing was kept and "
        "no PR was opened.\nrun_id={run_id}",
        "Orbi 交付已停止：交付进行中该 Issue 被关闭；此时还没有产生提交，因此"
        "没有保留任何工作，也没有创建 PR。\nrun_id={run_id}",
    ),
}


def stop_comment_key(committed: bool) -> str:
    """The catalog key of the stop comment a closeout has to post.

    `committed` IS the closeout's push decision: only a committed delivery
    pushed the branch, so only then may the comment name it (Issue #2475).
    """
    if committed:
        return "runner.issue_closed"
    return "runner.issue_closed_nothing_committed"
