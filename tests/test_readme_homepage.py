"""README homepage contract (Issue #241).

The README is the project homepage, not the operations manual: a new
user must find what Orbi is, the shortest runnable path and the docs
entry points on one screen, while every mechanism detail (systemd
deployment, transport, labels/P0/Epic/Release, run_id, model_wait,
review/merge gate, CI/coverage) lives in exactly one place — the docs
site — and the README keeps at most a one-sentence summary plus the
authoritative link.

These tests fail when the README grows back past the homepage budget
(120 lines / 8KB), when a required docs entry link disappears, when a
duplicated mechanism section creeps back in, or when the first screen
stops answering "what is it / how do I start".
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# The homepage budget (Issue #241 acceptance criteria).
MAX_LINES = 120
MAX_BYTES = 8 * 1024

# Required documentation entry links: the docs home plus every core
# topic page, in-repo relative paths (the docs/ directory is the single
# source of truth, docs.orbi.build the hosted site).
REQUIRED_ENTRY_LINKS = (
    "https://docs.orbi.build/",
    "docs/getting-started.mdx",
    "docs/setup.mdx",
    "docs/workflow.mdx",
    "docs/operations.mdx",
    "docs/testing.mdx",
    "docs/contributing.mdx",
    "docs/zh/",
)

# Mechanism detail that belongs to the docs site, not the homepage.
# Each needle is a marker of a section the old README expanded and the
# homepage must NOT carry (a one-sentence summary + the docs link is
# allowed; the mechanism prose is not).
FORBIDDEN_MECHANISM_NEEDLES = (
    # systemd deployment mechanics (docs/operations.mdx owns this)
    "ExecStartPre",
    "unit_drift",
    "repo_sha256=",
    "installed_sha256=",
    "base-sync.lock",
    "flock",
    "capacity_full",
    "OnCalendar",
    # git transport mechanics (docs/operations.mdx / docs/setup.mdx)
    "transport_check_failed",
    "git ls-remote",
    "git remote set-url origin",
    # observability mechanics (docs/operations.mdx)
    "model_wait_dead",
    "PI_MODEL_WAIT_DEAD_SECONDS",
    "run_failed",
    # workflow mechanics (docs/workflow.mdx)
    "REVIEW_VERDICT",
    "--match-head-commit",
    "epic_not_claimed",
    "blockedBy",
    "Muyan Pilot opened PR",
    # label vocabulary beyond the one claim label the quickstart needs
    "ai-in-progress",
    "ai-pr-opened",
    "ai-fix-needed",
    "ai-merged",
    "ai-epic",
    "ai-release",
    "ai-ticket-only",
    "`p0`",
    # label initialization commands (docs/setup.mdx)
    "gh label create",
    # CI / coverage mechanics (docs/testing.mdx)
    "coverage run --branch",
    "coverage_gate.py",
    "diff_coverage_gate.py",
    "GitHub Actions",
    # config-field mechanics (docs/getting-started.mdx)
    "active_milestone",
    "max_concurrency",
    # stale deployment paths (Issue #152 / #103)
    "uv tool upgrade",
    "python3 muyan_pilot.py",
    "cp systemd/muyan-pilot.service",
)


def readme_text() -> str:
    assert README.is_file(), f"missing README: {README}"
    return README.read_text(encoding="utf-8")


def test_readme_stays_within_the_homepage_budget():
    """Issue #241: the README is a homepage — 120 lines / 8KB max, so a
    new user can read it on one screen."""
    text = readme_text()
    lines = text.splitlines()
    assert len(lines) <= MAX_LINES, (
        f"README has {len(lines)} lines, the homepage budget is "
        f"{MAX_LINES}"
    )
    size = len(text.encode("utf-8"))
    assert size <= MAX_BYTES, (
        f"README is {size} bytes, the homepage budget is {MAX_BYTES}"
    )


def test_readme_carries_every_required_docs_entry_link():
    """Issue #241: the docs entry must list the docs home and every core
    topic page (getting-started, setup, workflow, operations, testing,
    contributing, the Chinese entry) so each reader finds the complete
    explanation in one hop."""
    text = readme_text()
    for link in REQUIRED_ENTRY_LINKS:
        assert link in text, f"README is missing the docs entry link: {link}"


def test_readme_does_not_duplicate_docs_mechanism_sections():
    """Issue #241: every mechanism keeps at most a one-sentence summary
    plus the authoritative docs link in the README; the detailed
    sections (systemd/timer internals, transport, label state machine,
    run_id/journal fields, model_wait, review/merge gate, CI/coverage)
    must not come back to the homepage."""
    text = readme_text()
    found = [needle for needle in FORBIDDEN_MECHANISM_NEEDLES if needle in text]
    assert not found, (
        "README duplicates docs mechanism detail: "
        f"{found} — keep a one-sentence summary + the docs link instead"
    )


def test_first_screen_says_what_orbi_is_and_how_to_start():
    """Issue #241: within the first 40 lines a new user must see what
    Orbi is (the GitHub Issue task pool, no database/queue/daemon),
    what it does end to end (Issue → worktree → Pi → PR → review/merge)
    and the runnable quickstart (clone + CLI install + setup + one tick).
    """
    head = "\n".join(readme_text().splitlines()[:40])
    # What it is: the GitHub Issue task pool and the MVP boundary.
    assert "GitHub Issue" in head, (
        "the first screen must say Orbi's task pool is GitHub Issues"
    )
    for boundary in ("数据库", "队列", "daemon"):
        assert boundary in head, (
            f"the first screen must state the MVP boundary (no {boundary})"
        )
    # The end-to-end capability overview: Issue -> worktree -> Pi ->
    # commit/PR -> review/merge.
    assert "worktree" in head, "the first screen must name the worktree"
    assert "Pi" in head, "the first screen must name the Pi development step"
    assert "PR" in head, "the first screen must name the PR delivery"
    assert "审查" in head or "review" in head.lower(), (
        "the first screen must name the independent review"
    )
    assert "merge" in head.lower(), "the first screen must name the merge"
    # The shortest runnable path on the first screen.
    assert "git clone https://github.com/orbi-build/orbi.git" in head, (
        "the first screen must carry the quickstart clone command"
    )
    assert "uv tool install --force --reinstall --editable" in head, (
        "the first screen must carry the CLI install command"
    )
    assert "muyan-pilot setup" in head, (
        "the first screen must carry the one-time setup command"
    )
    assert "bootstrap_runner.py" in head, (
        "the first screen must carry the first-tick command"
    )


def overview_section(text: str) -> str:
    """The capability-overview section (from its heading to the next
    `## ` heading) — the chain check runs inside it, not over the whole
    file (the intro paragraph names `worktree` before the overview).
    """
    match = re.search(r"^## 它能做什么\n(.*?)(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert match is not None, (
        "README is missing the capability overview section (## 它能做什么)"
    )
    return match.group(1)


def test_readme_capability_overview_keeps_the_delivery_chain():
    """Issue #241: the capability overview keeps the delivery chain
    Issue → worktree → Pi → commit/PR → review/merge (as a flow or a
    short list), with the PR body `Fixes #N` contract named — but the
    full state machine stays in docs/workflow.mdx."""
    text = readme_text()
    section = overview_section(text)
    assert "Fixes #" in section, (
        "the capability overview must name the PR body Fixes #N contract"
    )
    # The chain stages appear in the overview, in delivery order.
    chain = [
        "ai-ready",
        "worktree",
        "Pi",
        "PR",
        "审查",
        "merge",
    ]
    positions = []
    for stage in chain:
        pos = section.find(stage)
        assert pos != -1, f"capability overview misses the {stage!r} stage"
        positions.append(pos)
    assert positions == sorted(positions), (
        "the capability overview must present the chain in delivery "
        f"order, got positions {positions}"
    )


def test_readme_keeps_the_license_and_contributing_entries():
    """Issue #241: the homepage keeps the License (Apache-2.0, linked to
    the root LICENSE file) and the contributing entry (the contributing
    docs page plus the in-repo development contract)."""
    text = readme_text()
    assert re.search(r"\]\(LICENSE\)", text), (
        "README must link to the LICENSE file"
    )
    assert "Apache License 2.0" in text, (
        "README must name the Apache License 2.0"
    )
    assert "docs/contributing.mdx" in text, (
        "README must point at the contributing docs"
    )
    assert "AGENTS.md" in text, (
        "README must point at the in-repo development contract"
    )
