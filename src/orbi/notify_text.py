"""The fixed text of Orbi's Telegram messages (Issues #1673, #2287).

Split out of `orbi.notify` (Issue #2287) the way the failure vocabulary was
split out of `orbi.language` (Issue #1729): the transport, the enable gate,
the merged-PR watermark and the CLI stay in `notify`, and every fixed
sentence plus every renderer lives here, so the problem alert that quotes a
stop comment can grow without touching the transport module.

Nothing here sends, waits or raises: a message is a pure function of the
facts its caller passes (the label, `owner/repo#N`, the title, the facts the
stop comment recorded and the host `message_language`). `notify` owns the
pure-bypass guard around every call.

Two languages exist and only two: the alert's own labels and sentences follow
the HOST `message_language`, while the reason and next action a stop alert
quotes stay exactly as the stop comment wrote them — in the language the run
rendered that comment with (a repository may set its own `message_language`,
`orbi.repo_config`).
"""
from __future__ import annotations

import dataclasses

from orbi import failure, language
from orbi.delivery_labels import (
    AWAITING_MERGE_LABEL,
    BLOCKED_LABEL,
    NEEDS_DETAIL_LABEL,
    READY_LABEL,
)
from orbi.journal import redact_secrets

# The fixed message texts. The `Orbi · ` prefix is part of the contract.
HEADLINE_EN = "Orbi · Needs your attention: "
HEADLINE_ZH = "Orbi · 需要您处理："
STATUS_EN = {
    BLOCKED_LABEL: "Orbi is stuck and needs you",
    NEEDS_DETAIL_LABEL: "Orbi needs more detail",
    AWAITING_MERGE_LABEL: "Waiting for you to approve the merge",
}
STATUS_ZH = {
    BLOCKED_LABEL: "Orbi 卡住了，需要您处理",
    NEEDS_DETAIL_LABEL: "需要您补充信息",
    AWAITING_MERGE_LABEL: "等您批准合并",
}
# The human-review wait status (Issue #1684): the gate holds the
# delivery until a person confirms the acceptance checklist.
HUMAN_REVIEW_STATUS_EN = "Waiting for your review"
HUMAN_REVIEW_STATUS_ZH = "等您人工验收"
TEST_EN = (
    "Orbi test notification\n"
    "Your notification target works. Future alerts look like this."
)
TEST_ZH = (
    "Orbi 测试通知\n"
    "这条消息确认您的接收配置可用，之后的通知会是这个样子。"
)

# The fixed code-merged texts (Issue #1674): the same `Orbi · ` prefix
# and the `owner/repo#N <title>` line, followed by the PR link.
MERGED_HEADLINE_EN = "Orbi · Code merged: "
MERGED_HEADLINE_ZH = "Orbi · 代码已合并："

# The one catalog key the problem messages end with (`orbi.language`).
HELP_KEY = "help.community"

# The stop alert's own labels (Issue #2287): the reason and the next action
# are quoted from the stop comment, these two words are the alert's.
REASON_LABEL_EN = "Reason:"
REASON_LABEL_ZH = "原因："
ACTION_LABEL_EN = "Next action:"
ACTION_LABEL_ZH = "下一步："
STOP_COMMENT_LABEL_EN = "Stop comment:"
STOP_COMMENT_LABEL_ZH = "停止评论："
# A stop whose comment records no reason says so instead of showing an
# empty or invented line (Issue #2287 acceptance 5).
UNKNOWN_REASON_EN = "the reason is unknown"
UNKNOWN_REASON_ZH = "原因未知"

# Whether Orbi comes back on its own (Issue #2287 acceptance 3). These are
# the two reasons whose `ai-blocked` stop the engine resumes by itself: the
# tick-start `blocked_scan` walks the label for both, `check_permission.recover`
# retries the check read and `base_ci_fix.recover` resumes the SAME delivery,
# so a person repairs the precondition and never relabels (Issue #1918, #1825).
# Every other reason waits for an explicit handoff — including
# `github_transient`, whose stop means the one automatic re-queue of Issue
# #2202 was already spent, and `provider_quota`, which stays terminal on
# purpose (Issue #698).
AUTO_RESUME_REASONS = frozenset({"checks_permission_missing", "base_ci_red"})

RESUME_EN = "Orbi resumes by itself — no relabel is needed."
RESUME_ZH = "Orbi 会自行恢复 —— 无需重新打标签。"
# The handoff sentence names the ONE repair that resumes this stop: the
# catalog's unblock step for a blocked delivery (the same wording every
# failure comment composes, `blocked.unblock_step`), and the claim label for
# a thin ticket whose label is `ai-needs-detail`, not `ai-blocked`.
HANDOFF_BLOCKED_EN = (
    "Orbi does not resume by itself — an explicit handoff is needed: {step}."
)
HANDOFF_BLOCKED_ZH = "Orbi 不会自行恢复 —— 需要您显式交接：{step}。"
HANDOFF_NEEDS_DETAIL_EN = (
    "Orbi does not resume by itself — after you edit the ticket, add "
    "`{ready_label}` back to resume."
)
HANDOFF_NEEDS_DETAIL_ZH = (
    "Orbi 不会自行恢复 —— 补好工单后重新添加 `{ready_label}` 即可继续。"
)


@dataclasses.dataclass(frozen=True)
class StopDetail:
    """What a stop comment records, as its alert quotes it (Issue #2287).

    `reason` and `action` are the comment's own sentences, `None` when the
    comment records none; `resumes` is whether the engine comes back
    without a relabel.
    """

    reason: str | None
    action: str | None
    resumes: bool


def stop_detail(body: str, *, language_code: str) -> StopDetail:
    """The reason, next action and resume rule of one rendered stop comment.

    The reason and the action are read back from the comment's own
    `failure.reason_label` / `failure.action_label` lines in the language the
    comment was written in, so the alert repeats what that comment says —
    never a second rendering of the same reason that could drift from it.
    A stop whose scene rendered no such line yields `None`, and the alert
    then says the reason is unknown (Issue #2287).
    """
    return StopDetail(
        reason=_labelled(
            body, language.text(language_code, "failure.reason_label")),
        action=_labelled(
            body, language.text(language_code, "failure.action_label")),
        resumes=_resumes(body),
    )


def needs_detail_detail(*, ready_label: str, language_code: str) -> StopDetail:
    """The reason and next action a thin-ticket stop comment records.

    The same two catalog entries `clarify.render_comment` renders, in the
    run's language: the pre-flight sentence that says why the ticket is not
    deliverable, and the repair instruction that names the claim label. The
    alert neither lists the missing pieces nor repeats the comment's
    template — the linked comment stays the one place for that.

    A proposal-carrying stop keeps its own command sentence in the linked
    comment; the alert names the repair every such stop needs, so it can be
    read (and acted on) without opening GitHub. A thin-ticket stop is never
    resumed by the engine: it re-judges the ticket only when the person hands
    it back with the claim label.
    """
    return StopDetail(
        reason=language.text(language_code, "clarify.intro"),
        action=language.text(
            language_code, "clarify.repair", ready_label=ready_label),
        resumes=False,
    )


def attention_message(label: str, repo: str, number: int,
                      title: str | None, *, language_code: str) -> str:
    """The fixed "needs you" text of an ORDINARY handoff (`ai-awaiting-merge`).

    It names the state, the Issue and its link and ends there: a handoff is
    not a problem, so it carries no community help line (Issue #1684). The
    two problem stops send `stop_message` instead.
    """
    zh = language_code == "zh"
    return _needs_you_message(
        (STATUS_ZH if zh else STATUS_EN)[label], repo, number, title,
        help_line=False, language_code=language_code,
    )


def stop_message(label: str, repo: str, number: int, title: str | None, *,
                 detail: StopDetail, comment_url: str | None,
                 language_code: str,
                 ready_label: str = READY_LABEL) -> str:
    """The ONE problem alert of a stop that needs a person (Issue #2287).

    The existing headline, status, issue line, Issue link and help line,
    plus the stop comment's permalink, the reason and next action that
    comment records, and the one resume-or-handoff sentence. `ready_label` is
    the claim label a thin ticket's handoff names (the blocked stop composes
    the catalog's own unblock step and ignores it).
    """
    zh = language_code == "zh"
    # A Chinese label ends with its full-width colon, which takes no space
    # after it; the English one keeps the space its colon needs.
    gap = "" if zh else " "
    extra = []
    if comment_url:
        extra.append(
            f"{STOP_COMMENT_LABEL_ZH if zh else STOP_COMMENT_LABEL_EN}"
            f"{gap}{comment_url}"
        )
    reason = detail.reason or (UNKNOWN_REASON_ZH if zh else UNKNOWN_REASON_EN)
    extra.append(
        f"{REASON_LABEL_ZH if zh else REASON_LABEL_EN}{gap}{reason}"
    )
    if detail.action:
        extra.append(
            f"{ACTION_LABEL_ZH if zh else ACTION_LABEL_EN}{gap}{detail.action}"
        )
    extra.append(_resume_line(label, detail, ready_label, language_code))
    return _needs_you_message(
        (STATUS_ZH if zh else STATUS_EN)[label], repo, number, title,
        help_line=True, language_code=language_code, extra=extra,
    )


def human_review_message(repo: str, number: int,
                         title: str | None, *, language_code: str) -> str:
    """The fixed "waiting for your review" text (Issue #1684)."""
    status = (
        HUMAN_REVIEW_STATUS_ZH if language_code == "zh"
        else HUMAN_REVIEW_STATUS_EN
    )
    return _needs_you_message(
        status, repo, number, title, help_line=True,
        language_code=language_code,
    )


def test_message(language_code: str) -> str:
    """The fixed test-notification text in the configured language."""
    return TEST_ZH if language_code == "zh" else TEST_EN


def merged_message(repo: str, pr: dict, *, language_code: str) -> str:
    """The fixed "code merged" text for one merged PR."""
    headline = (
        MERGED_HEADLINE_ZH if language_code == "zh" else MERGED_HEADLINE_EN
    )
    return f"{headline}{repo}#{pr['number']} {pr['title']}\n{pr['url']}"


def _needs_you_message(status: str, repo: str, number: int,
                       title: str | None, *, help_line: bool,
                       language_code: str,
                       extra: list[str] | None = None) -> str:
    """One "needs you" message; a problem message ends with the help line."""
    headline = HEADLINE_ZH if language_code == "zh" else HEADLINE_EN
    body = "\n".join([
        f"{headline}{status}",
        _issue_line(repo, number, title),
        f"https://github.com/{repo}/issues/{number}",
        *(extra or ()),
    ])
    if not help_line:
        return body
    return f"{body}\n{language.text(language_code, HELP_KEY)}"


def _issue_line(repo: str, number: int, title: str | None) -> str:
    """`owner/repo#N [title]` — the title is omitted when it is unknown."""
    line = f"{repo}#{number}"
    return f"{line} {title}" if title else line


def _labelled(body: str, label: str) -> str | None:
    """The text after one rendered label line, or None when it has none.

    The first match wins: the comment writes its reason and action above
    the details fold, so a later copy quoted inside raw evidence can never
    replace them. The quoted text is redacted exactly as the posted comment
    is (`format_status_comment`), so the alert can never echo a credential
    the comment keeps out of GitHub.
    """
    for line in body.splitlines():
        if line.startswith(label):
            text = redact_secrets(line[len(label):].strip())
            if text:
                return text
    return None


def _resumes(body: str) -> bool:
    """Whether the stop's own record says the engine comes back by itself.

    The machine-readable `orbi:failure:v1` block the stop comment carries
    decides the sentence, never the prose beside it. A body whose block is
    present but unreadable says nothing, so it keeps the handoff sentence —
    the safe one to read.
    """
    try:
        record = failure.parse(body)
    except failure.FailureError:
        return False
    return record is not None and record.reason_code in AUTO_RESUME_REASONS


def _resume_line(label: str, detail: StopDetail,
                 ready_label: str, language_code: str) -> str:
    """The one sentence saying whether Orbi resumes by itself."""
    zh = language_code == "zh"
    if detail.resumes:
        return RESUME_ZH if zh else RESUME_EN
    if label == NEEDS_DETAIL_LABEL:
        template = (
            HANDOFF_NEEDS_DETAIL_ZH if zh else HANDOFF_NEEDS_DETAIL_EN
        )
        return template.format(ready_label=ready_label)
    return (HANDOFF_BLOCKED_ZH if zh else HANDOFF_BLOCKED_EN).format(
        step=language.text(language_code, "blocked.unblock_step"),
    )
