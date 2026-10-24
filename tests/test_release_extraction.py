"""The release execution/closeout extraction (Issue #1859).

`process_release` moved out of the 1520-line `orbi.release` into
`orbi.release_run`, and the tag primitives, the GitHub Release publish,
the Milestone close, the Epic sweep and the terminal release comments
into `orbi.release_closeout` — unchanged (Article 3.2: an extraction
changes no behaviour). `orbi.release` keeps the declaration, scope and
gate domain.

These guards keep the extraction real: each new module owns the moved
names, imports no `orbi.runner`, and the source module keeps no second
definition of them. The golden test freezes the visible outcome of the
success path — the success comment bytes and the tag/publish/Milestone
side effects — so a later refactor cannot quietly change them.
"""
import ast
from pathlib import Path

import orbi.release as release
import orbi.release_closeout as release_closeout
import orbi.release_run as release_run
from orbi import config as config_domain
from tests.fakes.bootstrap_release import (
    RELEASE_DECLARATION_BODY, make_release_process_env,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "orbi"

# The execution region the Issue moved out of `orbi.release`.
RUN_NAMES = ("process_release",)

# The closeout region the Issue moved out of `orbi.release`.
CLOSEOUT_NAMES = (
    "MILESTONE_OPEN_RETRY_DELAYS",
    "release_tag_commit",
    "local_release_tag_commit",
    "ensure_release_tag_created",
    "ensure_release_tag_pushed",
    "tag_commit_is_ancestor_of_base",
    "publish_release",
    "close_release_milestone",
    "release_success_closeout",
    "release_failure_closeout",
    "reconcile_release_epics",
)

# The release constants and the gate types stay in the domain module: the
# runner and the config read them from there (Article 3.2 — no duplicated
# constant).
DOMAIN_NAMES = (
    "ROLE_RELEASE",
    "RELEASE_CI_WAIT_SECONDS",
    "RELEASE_DELIVERIES_WAIT_SECONDS",
    "ReleaseDeliveriesWaiting",
    "release_target_milestone",
    "check_release_gates",
)


def _lines(module_name: str) -> int:
    path = PACKAGE_DIR / f"{module_name}.py"
    return len(path.read_text(encoding="utf-8").splitlines())


def _imported_modules(module_name: str) -> set:
    """Every module `module_name` imports, at any scope."""
    path = PACKAGE_DIR / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def test_release_run_owns_the_state_machine_below_the_cap():
    missing = [name for name in RUN_NAMES if not hasattr(release_run, name)]
    assert missing == [], f"release_run must own the moved names: {missing}"
    assert _lines("release_run") < 600


def test_release_closeout_owns_the_tag_publish_and_closeout_below_the_cap():
    missing = [
        name for name in CLOSEOUT_NAMES
        if not hasattr(release_closeout, name)
    ]
    assert missing == [], f"release_closeout must own the moved names: {missing}"
    assert _lines("release_closeout") < 600


def test_new_modules_never_import_the_runner():
    """Article 3.3: the execution and the closeout never reach into the
    runner, and the closeout never reaches back into the domain module."""
    for module_name in ("release_run", "release_closeout"):
        imported = _imported_modules(module_name)
        assert "orbi.runner" not in imported, module_name
    assert "orbi.release" not in _imported_modules("release_closeout")


def test_release_keeps_one_owner_and_stays_below_1000_lines():
    """Issue #1859 acceptance: one owner per moved concept, and the domain
    module is back below the 1000-line cap."""
    # The domain module imports nothing of the moved regions back, so
    # reachability through it would mean a second definition or a
    # re-export (Article 3.2 keeps one owner per concept).
    redefined = [
        name for name in RUN_NAMES + CLOSEOUT_NAMES
        if hasattr(release, name)
    ]
    assert redefined == [], f"release still defines {redefined}"
    assert [name for name in DOMAIN_NAMES if not hasattr(release, name)] == []
    assert _lines("release") < 1000


def test_release_success_path_golden_comment_and_side_effects(monkeypatch):
    """The success path's visible outcome is frozen (Issue #1859).

    The tag push, the GitHub Release publish, the label transitions, the
    Milestone close and the one success comment keep the exact bytes and
    the exact order the pre-extraction state machine produced.
    """
    state = make_release_process_env(monkeypatch)
    issue = {"number": 99, "title": "Release v0.3.0",
             "body": RELEASE_DECLARATION_BODY,
             "labels": [{"name": "ai-ready"}, {"name": "ai-release"}]}
    url = release_run.process_release(
        issue,
        config_domain.RunnerConfig(repo_dir=Path("/r"), base_branch="main"),
        "o/r",
    )
    assert url == "https://github.com/o/r/releases/tag/v0.3.0"
    (comment_number, comment_kwargs), = state["comments"]
    assert comment_number == 99
    assert comment_kwargs["repo"] == "o/r"
    assert comment_kwargs["body"] == (
        "<!-- orbi:run=a1b2c3d4 -->\n"
        "Orbi released: https://github.com/o/r/releases/tag/v0.3.0\n"
        "**#99 Release v0.3.0**\n"
        "- base_branch: main\n"
        "- base_sha: abc123\n"
        "- run_id=a1b2c3d4\n"
        "- priority: normal\n"
        "- tag: v0.3.0\n"
        "- release_commit: abc123\n"
        "\n"
        "<!-- runner=deadbeef -->\n"
        "\n"
        "## Scope (verified item by item)\n"
        "\n"
        "- PR #123 merged (mergeCommit=aaa111)\n"
        "- Issue #124 closed\n"
        "\n"
        "## Pre-release gates\n"
        "\n"
        "- no open Issue carries ai-in-progress / ai-pr-opened / "
        "ai-fix-needed\n"
        "- CI on the release commit: 1 check(s) all success/neutral/skipped\n"
        "\n"
        "## Tests\n"
        "\n"
        "- release tests gated by GitHub Actions CI on the release commit "
        "abc123 (the #268 CI-wait gate; no local test execution)\n"
        "\n"
        "## Milestone\n"
        "\n"
        "- Milestone #5 (https://github.com/o/r/milestone/5) closed after "
        "release v0.3.0 (0 open issues)\n"
        "\n"
        "run_id=a1b2c3d4"
    )
    # Tag: created at the release commit and pushed plainly (never forced).
    commands = [command for command, _ in state["commands"]]
    assert ["git", "push", "origin", "refs/tags/v0.3.0"] in commands
    assert not any("--force" in command or "-f" == command
                   for command in commands)
    # Publish: the tag and its full evidence reach the GitHub Release.
    (published,) = state["published"]
    assert published["repo"] == "o/r"
    assert published["tag"] == "v0.3.0"
    assert published["version"] == "v0.3.0"
    assert published["release_commit"] == "abc123"
    assert published["run_id"] == "a1b2c3d4"
    assert published["issue_number"] == 99
    assert published["scope_evidence"] == [
        "PR #123 merged (mergeCommit=aaa111)",
        "Issue #124 closed",
    ]
    assert published["gate_evidence"] == [
        "no open Issue carries ai-in-progress / ai-pr-opened / ai-fix-needed",
        "CI on the release commit: 1 check(s) all success/neutral/skipped",
    ]
    assert published["test_evidence"] == (
        "release tests gated by GitHub Actions CI on the release commit "
        "abc123 (the #268 CI-wait gate; no local test execution)"
    )
    # Milestone: closed AFTER the Issue close, with the official contract.
    close_milestone = ["gh", "api", "repos/o/r/milestones/5",
                       "--method", "PATCH", "-f", "state=closed"]
    assert commands.index(close_milestone) > commands.index(
        ["gh", "issue", "close", "99", "--repo", "o/r"])
    # Labels: claimed, then ai-merged alone (ai-ready cleared, #1526).
    assert state["edits"] == [
        (99, {"repo": "o/r", "add": "ai-in-progress"}),
        (99, {"repo": "o/r", "add": "ai-merged",
              "remove": "ai-in-progress"}),
        (99, {"repo": "o/r", "remove": "ai-ready"}),
    ]
