"""The base-CI-red scene's automatic fix-Issue wording (Issue #2203).

A leaf module with no imports: `language_terms.EXTRA_MESSAGES` merges this
map into `language.MESSAGES`, so `language.text` stays the ONE reading
point. It is a separate file because the catalog owners (`language.py`,
`language_terms.py`) sit at their size-ratchet ceilings: a module that
exists in the baseline may only shrink, so new entries land beside the map
that owns them instead of growing those two.
"""

BASE_CI_FIX_MESSAGES: dict[str, tuple[str, str]] = {
    # The section the pause comment shows instead of asking the person to
    # reply `/orbi fix-base`: the engine already opened (or reused) the
    # fix Issue of this base branch (one per branch, Issue #2259), and it
    # is an ordinary delivery, so the reader has nothing to type.
    "base_ci_red.fix_issues": (
        "🧰 **Fix Issue for the red base** — opened or reused by Orbi "
        "and delivered like any other Issue; no comment needed:",
        "🧰 **基础分支失败的修复 Issue** —— 已由 Orbi 自动开出或复用，"
        "并会像普通 Issue 一样交付；不需要评论：",
    ),
}
