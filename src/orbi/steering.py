"""The steering poll's other two jobs (Issues #1094, #2221).

`pi_session.check_steering` owns WHEN a running session is steered; what a
steering event changes on the ticket is here, so the poll keeps its
bounded size (`tools/size_ratchet.py` records it) and the two effects have
one home:

- `limit_notice` tells the ticket once that the steering budget is spent;
  a bypass, a failed post is journaled and never stops the poll;
- `answer_body` carries out a person's answer to the stop question while
  the session runs (Issue #2221): the body change the answer implies is
  written through the `/orbi amend` logic here, and the restart the poll
  already performs is what makes the next session read it. An answer the
  engine does not carry out at this stage (`cancel`, a close) leaves its
  one receipt and changes nothing.
"""
from __future__ import annotations

import logging

from orbi import answer, language, user_summary
from orbi.github import comment_issue
from orbi.journal import event, issue_context
from orbi.progress import run_marker


def limit_notice(config, issue: dict, source_repo: str) -> None:
    """Tell the ticket once that further corrections no longer restart."""
    notice = user_summary.append_summary(
        language.text(
            language.of(config), "delivery.steering_limit_notice",
        ),
        status_url=config.status_url,
        issue=int(issue["number"]), title=issue.get("title"),
        language_code=language.of(config),
    )
    try:
        comment_issue(
            int(issue["number"]), repo=source_repo,
            body=(
                f"{run_marker(config.run_id)}\n"
                f"{notice}\n"
                f"run_id={config.run_id}"
            ),
        )
    except Exception as exc:
        event(
            "steering_limit_notice_failed", level=logging.WARNING,
            issue=issue_context(source_repo, int(issue["number"])),
            reason=type(exc).__name__,
        )


def answer_body(
    fresh: list[dict], *, issue: dict, repo: str, comments: list[dict],
    body: object, language_code: str,
) -> str | None:
    """The amended body one poll's answers produce, or None."""
    return answer.apply_steering(
        fresh, issue=issue, repo=repo, comments=comments, body=body,
        language_code=language_code,
    )
