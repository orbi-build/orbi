"""The Epic reconciliation wording (Issue #2356).

An Epic no longer always reads as "complete": each closed child is named
with how it ended — delivered by Orbi, closed while blocked, or closed by a
person as completed / as not planned — and the sentence around them says
so. The `ai-merged` / `ai-blocked` labels and GitHub's close reason stay
human prose here (`state_reason` never appears as a raw row).

A leaf module with no imports, like `language_terms_criteria`: the catalog
owners (`language.py`, `language_terms.py`) sit at their size-ratchet
ceilings, so this wording lands beside them and
`language_terms.EXTRA_MESSAGES` merges the map into
`language.MESSAGES` — `language.text` stays the ONE reading point.
"""

EPIC_MESSAGES: dict[str, tuple[str, str]] = {
    # Today's "complete" sentence (`epic.reconciliation`) is kept for a
    # fully delivered Epic only; every other closed Epic states what
    # actually happened instead of claiming completion.
    "epic.reconciliation_closed": (
        "Epic reconciliation{prefix}: all children are closed, but not all "
        "of them were delivered by Orbi; children: {children}; no open "
        "native blockers/dependencies.",
        "Epic 对账{prefix}：所有子项均已关闭，但并非全部由 Orbi 交付；"
        "子项：{children}；没有未关闭的原生阻塞/依赖。",
    ),
    # How one child ended. The label names stay verbatim; the prose moves.
    "epic.child_delivered_pr": (
        "PR #{number} merged by Orbi",
        "PR #{number} 已由 Orbi 合并",
    ),
    "epic.child_delivered_issue": (
        "Issue #{number} delivered by Orbi (ai-merged)",
        "Issue #{number} 由 Orbi 交付（ai-merged）",
    ),
    "epic.child_blocked": (
        "Issue #{number} closed while blocked (ai-blocked)",
        "Issue #{number} 在 ai-blocked 状态下关闭",
    ),
    "epic.child_completed": (
        "Issue #{number} closed by a person as completed",
        "Issue #{number} 由人关闭，标记为已完成",
    ),
    "epic.child_not_planned": (
        "Issue #{number} closed by a person as not planned",
        "Issue #{number} 由人关闭，标记为未计划",
    ),
}
