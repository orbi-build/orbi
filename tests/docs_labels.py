"""The ONE definition of a label reference for the docs contracts.

Issue #49 requires that no page mentions a label the runner does not
have: a typo'd or removed label breaks the documented workflow silently.
The three docs contracts (README/AGENTS in `tests/test_docs_labels.py`,
the English pages in `tests/test_docs_site.py`, the Chinese pages in
`tests/test_docs_i18n.py`) each used to scan EVERY `ai-*` token in a
page, so an ordinary English word in prose was read as a label name:
`docs/release-v0.7.53.mdx` quotes a merged PR's title ("Resume: yield
instead of ai-blocking when another executor's run is live"), `ai-blocking`
is not a label, main went red and every pull request's merge gate stopped
(Issue #2313).

A bare `ai-…` word cannot be told apart from prose, so a LABEL REFERENCE
is what the docs really write as a label: the literal name inside a code
span or a fenced code block — ``ai-ready``, ``ai-fix-needed``,
``gh issue edit 3 --add-label ai-ready``. Prose is never scanned, so a
release note that copies a PR title verbatim stays prose. A hand-written
page that names a label the runner does not have still fails.

One definition, shared by the three contracts — never a copy per file.
"""
import re

from orbi.delivery_labels import (
    CONTENT_ONLY_LABEL,
    EPIC_LABEL,
    HUMAN_REVIEW_LABEL,
    LIFECYCLE_STATES,
    NEEDS_DETAIL_LABEL,
    OPS_LABEL,
    RELEASE_LABEL,
)

# Every `ai-*` label the runner has: the delivery lifecycle states
# (`delivery_labels.LIFECYCLE_STATES` is the single source of truth)
# plus the type/routing markers (`ai-epic`, `ai-release`,
# `ai-content-only`, `ai-ops-only`, `ai-human-review`,
# `ai-needs-detail`). `p0`/`bug` order the pickup and are not `ai-*`.
KNOWN_LABELS = frozenset(
    LIFECYCLE_STATES
    | {
        EPIC_LABEL,
        RELEASE_LABEL,
        CONTENT_ONLY_LABEL,
        OPS_LABEL,
        HUMAN_REVIEW_LABEL,
        NEEDS_DETAIL_LABEL,
    }
)

# An `ai-*` token. Only a standalone one counts: the skill directory
# `write-ai-ready-issue` contains `ai-ready-issue`, which is not a label
# (Issue #1552), so the token must not be preceded by a word or hyphen
# character.
_LABEL_TOKEN = re.compile(r"(?<![\w-])ai-[a-z][a-z-]*\b")

# One code span: a run of backticks, its content, the same run again.
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")

# A fenced code block boundary (``` or ~~~, at least three).
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def label_references(text: str) -> set[str]:
    """The label names ONE document really references as labels.

    Code spans and fenced code blocks are scanned; prose is not, because
    a word there (`ai-blocking`) is not a label reference at all. A
    fence-looking line toggles the block state; mistaking a longer
    fence's inner run for a boundary can only end the block early and
    MISS a reference, never invent one.
    """
    found: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        code = line if in_fence else " ".join(
            span.group(2) for span in _CODE_SPAN.finditer(line)
        )
        found.update(_LABEL_TOKEN.findall(code))
    return found


def unknown_labels(text: str) -> set[str]:
    """The labels ONE document references that the runner does not have."""
    return label_references(text) - KNOWN_LABELS
