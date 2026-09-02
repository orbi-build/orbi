"""Docs single source of truth (Issue #231, continues Issue #160).

One contract lives in one authoritative page per language. The config
field table, the CLI command reference and every cross-page mechanism
(the editable `uv tool` CLI install, the git transport boundary, the
timer mechanics, the `model_wait` journal fields, the `Fixes #N` PR
body contract) is DEFINED on exactly one page; the other pages carry
at most a one-sentence summary plus a link and must not restate the
mechanism prose. The English pages live at the `docs/` root and the
Chinese mirror under `docs/zh/` (same slugs), so every rule below is
checked in BOTH languages.

Matching is whitespace-normalized (line wraps do not matter): a
marker names the characteristic phrase of the RESTATED mechanism
prose, so a legitimate one-sentence summary + link never contains it.

These tests fail when a mechanism prose block reappears on a second
page (the duplication this Issue removes), or when the Chinese pages
drift from the English contract (the pre-Issue-#186 "implementer
pushes and opens the PR" wording that the post-#186 Runner closeout
superseded).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
ZH_DIR = DOCS_DIR / "zh"


def norm(text: str) -> str:
    """Collapse every whitespace run to one space (wrap-proof match)."""
    return re.sub(r"\s+", " ", text)


def pages(lang_dir: Path) -> dict[str, str]:
    """slug -> whitespace-normalized page text for one language."""
    return {
        path.stem: norm(path.read_text(encoding="utf-8"))
        for path in sorted(lang_dir.glob("*.mdx"))
    }


EN = pages(DOCS_DIR)
ZH = pages(ZH_DIR)

# (name, marker, allowed slugs) — the marker may appear ONLY on the
# allowed pages (the authoritative definition, or nowhere).
#
# A. Editable `uv tool` CLI install mechanics — defined once in
#    getting-started step 2 ("Why --editable matters"). The operations
#    CLI preamble and the setup step-2 used to restate the same
#    ExecStartPre/sync mechanism.
EDITABLE_INSTALL_MARKERS = (
    "picked up by the next CLI process automatically",  # EN restatement
    "会被下一个 CLI 进程自动取到",  # ZH restatement
)

# B. Git transport boundary (SSH git data vs gh-token API, no HTTPS
#    fallback) — defined once in operations (timer item 2: the
#    pre-start check) + security (the credential framing). The
#    workflow rules bullet used to restate the full two-channel
#    boundary with its label list.
TRANSPORT_MARKERS = (
    "GitHub API operations (Issue, PR, label, comment, merge) stay on the `gh` token",  # EN workflow bullet
    "GitHub API 操作（Issue、PR、label、comment、merge）留在 `gh` token 上",  # ZH workflow bullet
)

# C. Timer mechanics (5-minute cadence, the per-instance start
#    semantics) — defined once in operations "The timers".
#    getting-started step 8 used to restate the cadence and the
#    fast-forward instead of pointing at Operations.
TIMER_MARKERS = (
    "Each timer fires every 5 minutes",  # EN step-8 restatement
    "每个 timer 每 5 分钟触发一次",  # ZH step-8 restatement
)

# D. `model_wait` field semantics (silence between COMPLETE session
#    events, the slow-generation boundary) — defined once in the
#    operations journal field reference. The getting-started config
#    table rows and the troubleshooting row used to restate the same
#    semantics.
MODEL_WAIT_MARKERS = (
    "Pi does not stream token-level progress into the JSONL",  # EN restatement
    "Pi 不向 JSONL 写 token 级进度",  # ZH restatement
)

# E. `Fixes #N` PR body contract — defined once in workflow "PR body
#    contract: Fixes #N". getting-started (verify chain) and
#    contributing (step 5) must keep at most the one-sentence
#    summary + link, never the full contract sentence.
FIXES_MARKERS = (
    "GitHub reads the body (not the PR title) and closes the Issue natively when the PR merges into the default branch",  # EN full contract
    "GitHub 读的是 body（不是 PR title），PR merge 到默认分支时原生关闭 Issue",  # ZH full contract
)

ALL_RULES = (
    ("editable-install", EDITABLE_INSTALL_MARKERS, {"getting-started"}),
    ("git-transport", TRANSPORT_MARKERS, {"operations", "security"}),
    ("timer-mechanics", TIMER_MARKERS, {"operations"}),
    ("model-wait-fields", MODEL_WAIT_MARKERS, {"operations"}),
    ("fixes-contract", FIXES_MARKERS, {"workflow"}),
)


def test_every_mechanism_is_defined_on_exactly_one_page_per_language():
    """No mechanism prose may be restated on a second page: each
    marker may appear only on its authoritative page (or nowhere)."""
    for lang, pages_text in (("en", EN), ("zh", ZH)):
        for name, markers, allowed in ALL_RULES:
            for marker in markers:
                found = sorted(
                    slug for slug, text in pages_text.items() if marker in text
                )
                stray = [slug for slug in found if slug not in allowed]
                assert not stray, (
                    f"[{lang}] {name}: the mechanism prose "
                    f"{marker[:60]!r}... is restated on {stray} — it is "
                    f"defined on {sorted(allowed)} (one sentence + link "
                    f"everywhere else)"
                )


def test_chinese_pages_carry_the_post_186_runner_closeout_contract():
    """Issue #186: the agent stops at the committed delivery — the
    Runner re-fetches the base under the base-sync lock, absorbs an
    advanced base with a plain `git merge`, pushes the task branch and
    opens the PR. The English operations/security pages carry this
    contract; the Chinese mirror must carry the SAME contract (the
    pre-#186 "implementer re-fetches and merges the base / pushes the
    branch and opens the PR" wording is stale and must not survive in
    either language)."""
    # EN: the Runner closeout is stated (the contract anchor).
    assert "the Runner's deterministic closeout" in EN["operations"], (
        "EN operations must state the Runner closeout (Issue #186)"
    )
    # ZH operations: the same post-#186 contract, not the stale one.
    zh_ops = ZH["operations"]
    assert "base-sync" in zh_ops, (
        "ZH operations must state the Runner closeout under the base-sync lock"
    )
    assert "git merge-base --is-ancestor" not in zh_ops, (
        "ZH operations keeps the pre-#186 implementer-side "
        "merge-base verification wording"
    )
    assert "实现者重新 fetch base" not in zh_ops, (
        "ZH operations keeps the pre-#186 implementer re-fetch wording"
    )
    # ZH security: the implementer stops at the committed delivery;
    # the Runner pushes and opens the PR (post-#186), not "the
    # implementer pushes the feature branch and opens a PR".
    zh_sec = ZH["security"]
    assert "只 push 自己的 feature branch" not in zh_sec, (
        "ZH security keeps the pre-#186 implementer-push wording"
    )
    assert "在提交的交付处停止" in zh_sec, (
        "ZH security must state that the implementer stops at the "
        "committed delivery (post-#186)"
    )
    assert "Runner" in zh_sec, (
        "ZH security must name the Runner as the push/PR actor"
    )
