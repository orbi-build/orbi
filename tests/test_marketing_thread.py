"""Launch-thread source contract (Issue #205)."""
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
THREAD = REPO_ROOT / "marketing" / "v0.2.0-launch-thread.md"
ISSUE_NUMBERS = (
    48, 50, 93, 98, 105, 118, 119, 131, 139, 140, 142, 149, 152,
    154, 156, 157, 158, 162, 169, 171, 172, 178, 179, 181, 192, 198,
    201, 197,
)


def test_v020_launch_thread_has_29_copy_ready_posts_and_evidence_links():
    """The Chinese announcement keeps every copy-ready post and evidence URL."""
    text = THREAD.read_text(encoding="utf-8")

    assert "# Orbi v0.2.0 黑灯软件工厂发布 Thread" in text
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert [f"## {number}/29" for number in range(1, 30)] == [
        line.split(" —")[0] for line in headings
    ]
    assert "https://github.com/xqliu/orbi" in text
    for issue in ISSUE_NUMBERS:
        assert f"## {ISSUE_NUMBERS.index(issue) + 2}/29 — #{issue}" in text
        assert f"https://github.com/xqliu/orbi/issues/{issue}" in text
    assert "https://github.com/xqliu/orbi/releases/tag/v0.2.0" in text
