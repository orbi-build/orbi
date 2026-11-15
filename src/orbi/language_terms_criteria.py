"""The acceptance-criteria wording (Issues #1929, #2206).

The `/orbi amend` command's rejections and the ONE receipt it leaves,
the acceptance heading the appended conditions land under, and the comment
the clarification gate posts when it applies a criteria proposal itself at
the claim. Every entry is display text; the machine contract (the markers,
the labels) stays in English and outside the catalog.

A leaf module with no imports: the catalog owners (`language.py`,
`language_terms.py`) sit at their size-ratchet ceilings, so the
criteria wording lands beside them and `language_terms.EXTRA_MESSAGES`
merges this map into `language.MESSAGES` — `language.text` stays
the ONE reading point.
"""

# The `/orbi amend` command (Issue #1929, typed word renamed by #1975):
# its rejections and the ONE receipt it leaves. Every entry is display
# text; the machine contract (the markers, the labels) stays in English
# and outside the catalog. The heading is display text too: it follows the
# message language (Issue #1971), while the marker that makes the append
# idempotent is the same in every language.
CRITERIA_MESSAGES: dict[str, tuple[str, str]] = {
    "criteria.reason_no_suggestion": (
        "the newest stop comment carries no acceptance-criteria proposal; "
        "edit that comment into a `kind=criteria` section, then reply again",
        "最新一条停止评论里没有验收条件提案；把那条评论改成 `kind=criteria` "
        "段落，然后再回复一次",
    ),
    "criteria.reason_invalid_line": (
        "the acceptance-criteria proposal on the newest stop comment is "
        "invalid at line {line}; fix that line and reply again",
        "最新一条停止评论里的验收条件提案在第 {line} 行无效；修好那一行后再回复一次",
    ),
    "criteria.reason_not_criteria": (
        "the newest stop comment proposes `{kind}`, not acceptance "
        "criteria; `/orbi amend` only adds a `kind=criteria` "
        "proposal",
        "最新一条停止评论的提案是 `{kind}`，不是验收条件；"
        "`/orbi amend` 只添加 `kind=criteria` 提案",
    ),
    "criteria.receipt_title": (
        "**Added the proposed acceptance criteria to this ticket.**",
        "**已把提案中的验收条件加入这张工单。**",
    ),
    "criteria.receipt_item": ("- {text}", "- {text}"),
    "criteria.receipt_done": (
        "They are under `{heading}`; the ticket is queued with "
        "`{label}`.",
        "它们位于 `{heading}` 之下；工单已用 `{label}` 进入队列。",
    ),
    # The heading the appended conditions land under (Issue #1971): a zh
    # repository reads a Chinese section in an otherwise Chinese ticket,
    # exactly like the rest of the stop comment's prose.
    "criteria.acceptance_heading": ("## Acceptance", "## 验收"),
    # The comment the gate posts when it applied a criteria proposal itself
    # (Issue #2206): the conditions are in the ticket now, so editing the
    # ticket changes them — the command path stays for a ticket the gate
    # could not complete.
    "clarify.criteria_appended": (
        "They are under `{heading}`, and this claim continues to delivery. "
        "Editing this ticket changes them.",
        "它们位于 `{heading}` 之下，本次领取会继续交付。"
        "编辑这张工单即可修改它们。",
    ),
}
